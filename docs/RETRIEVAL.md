# Steward Retrieval and Citation

Steward answers questions from the user's own files. This guide covers the
two halves of that promise: the retrieval index that finds the passages, and
the citation layer that makes a chat answer traceable to the exact source
passage. The retrieval index and its on-disk format are unchanged by the
citation layer; the layer sits on top and never rewrites the index.

## Retrieval

Everything is local. The pipeline lives in `src/steward/retrieval/`:

1. `chunk_document()` splits a text file into stable, literal markdown
   chunks: each ATX heading, each blank-line-separated paragraph, and long
   paragraphs split with overlapping boundaries.
2. `OllamaEmbedder` embeds each chunk through the local Ollama backend
   (`nomic-embed-text` by default, pulled once if absent, under the 20 GB
   guard).
3. `RetrievalIndex` stores the vectors in SQLite at `<data>/db/index.db`
   and searches them with cosine similarity plus MMR-lite dedup, accelerated
   by numpy with a pure-python fallback.

Indexing is incremental: a file is re-indexed only when its mtime or size
changed, deleted files are pruned, and ignored names and binary extensions
are never scanned.

`search(query, k=8)` returns hits with the shape the citation layer consumes:

```
path       source file path
ordinal    chunk ordinal in the file
offset     char offset of the chunk inside the file
score      cosine score of the hit
snippet    200-char one-line snippet
text       the full chunk text, a literal slice of source[offset:offset+len]
chunk_id   content-derived stable id
```

Because `text` is a literal slice of the source file, the offset always
points at exactly the passage the hit returns. That is what makes a citation
trustworthy: resolving the hit back to its file, offset and line range
produces the model's actual source text.

## Citation layer

`src/steward/citations.py` is a small, dependency-free module (stdlib only,
no third-party imports) that turns hits into a grounded chat answer:

- `select_context(hits, budget_chars)` ranks hits best first and greedily
  keeps the ones that fit a character budget, numbering them `[1]..[n]`.
  The top hit is always kept even when it alone overflows the budget, so a
  non-empty hit list never yields an empty context. The hits are returned as
  copies with a `number` key; the inputs are never mutated. Ties keep their
  input order, so identical inputs always number identically.
- `build_prompt(question, selected)` returns the exact prompt text that asks
  the model to answer only from the numbered sources, cite every claim as
  `[n]`, and say plainly when a part of the question is not covered.
- `extract_citations(reply, selected)` finds the `[n]` markers the reply
  actually used and resolves each to source path, line range and snippet,
  de-duplicated (a marker used twice resolves once, with its occurrence
  count) and ordered by marker number.
- `render_sources(used)` renders the resolved list as markdown-safe plain
  text that the web and phone clients show as-is: one line per source with
  path, line range and a one-line snippet.
- `line_range(source, offset, length)` computes the 1-based inclusive
  `(start, end)` lines of any character span. A trailing newline terminates
  the line it closes, so a chunk that ends in a newline never spills into
  the next line.

### Honesty guarantees

The module never claims a citation that was not in the provided context.

- `extract_citations` only ever resolves markers that point inside
  `1..len(selected)`. A marker outside that range cannot resolve to any
  provided source, is never reported as a citation, and is returned under
  `dropped` for the caller to log.
- `build_prompt` with zero hits returns an instruction to answer honestly
  that nothing in the user's files covers the question, to not invent an
  answer, and to not use source markers.
- `render_sources([])` renders an empty string: when the reply cited
  nothing, there is nothing to list.

### Line numbers at render time

Line numbers are computed from the current file on disk at
`extract_citations` time, using the same utf-8-lossy decode the index uses.
If the file has been edited or is no longer readable, the citation falls
back to the stored char offset. The snippet and text always come from the
index snapshot (the literal source slice), so a citation never points at a
passage the model did not see.

## Example

```python
from steward.citations import (
    select_context, build_prompt, extract_citations, render_sources,
)

hits = index.search("what does the flagship cost", k=8)
selected = select_context(hits, budget_chars=5000)
if not selected:
    prompt = build_prompt(question, [])     # honest no-hits instruction
else:
    prompt = build_prompt(question, selected)
    # send prompt to the model; the reply cites each claim as [n]
    result = extract_citations(reply, selected)
    sources = render_sources(result["used"])  # one markdown line per used source
    for n in result["dropped"]:
        log("dropped out-of-range citation marker [%d]" % n)
```

The daemon wiring that runs this end to end (search, prompt, model call,
resolution, delivery) lives in the chat path of `src/steward/server/daemon.py`
(`_ground_chat` + `_resolve_chat_sources` in the tool-loop turn): a `/v1/chat`
turn is grounded in the data-root retrieval index when the request opts in
(request field `cite`, default on) and the index has documents. The resolved
citations arrive on the chat stream as a final SSE payload after the reply
text and before `[DONE]`:

```
data: {"event":"sources","sources":[{"n":1,"path":"...","start_line":3,"end_line":19,"snippet":"..."}]}
```

and as a `sources` array in the `/chat` shim reply object. The reply text
itself carries only the `[n]` markers the model used; the web and phone
clients render the compact `Sources` disclosure from that payload, and the
phone stores it with the message so a cached answer still shows where it came
from.

When retrieval returns nothing (documents exist but nothing scores above the
caller-side floor), the turn is grounded with the honest no-coverage
instruction and the payload carries an empty list; there is never a
fabricated source. It is fail-closed: no index, no documents, a backend
error, or the request opting out leaves the turn as a plain assistant chat
with an empty sources payload. The `cite` request field is the opt-in seam
for a future gate on `steward.features.enabled("retrieval", ...)`: disabling
it per turn is a boolean the clients can already send.

