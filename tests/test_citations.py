"""Tests for the citation layer (src/steward/citations.py).

The layer is dependency-free and testable with plain hit dicts plus, for line
resolution, real files on disk. No model and no network are needed. The tests
assert the properties DONE requires:

  select_context numbers the best hits 1..n, never exceeds the budget once a
  hit is in and never drops the top hit; build_prompt is exact text that asks
  for [n] citations and turns honest on zero hits; extract_citations resolves
  markers to path + line range + snippet, de-duplicates, and drops and reports
  out-of-range markers; render_sources emits the markdown source list.
"""

from __future__ import annotations

from pathlib import Path

from steward.citations import (
    MARKER_RE,
    NO_HITS_INSTRUCTION,
    build_prompt,
    extract_citations,
    line_range,
    render_sources,
    select_context,
)


def hit(path: str = "notes/a.md", text: str = "Some passage text.\n",
        offset: int = 0, score: float = 0.5) -> dict:
    """A hit in the exact shape RetrievalIndex.search returns."""
    return {
        "path": path,
        "ordinal": 0,
        "offset": offset,
        "score": score,
        "snippet": " ".join(text.split()),
        "text": text,
        "chunk_id": "chunk-000",
    }


def write_file(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# -- select_context ------------------------------------------------------------


def test_select_context_best_first_with_stable_numbering():
    hits = [
        hit("a.md", "alpha ", score=0.3),
        hit("b.md", "bravo ", score=0.9),
        hit("c.md", "charlie ", score=0.6),
    ]
    selected = select_context(hits, budget_chars=1000)
    assert [h["path"] for h in selected] == ["b.md", "c.md", "a.md"]
    assert [h["number"] for h in selected] == [1, 2, 3]
    assert selected[0]["score"] == 0.9


def test_select_context_respects_budget_and_never_drops_top_hit():
    spends = hit("spends.md", "x" * 600, score=0.9)
    cheapo = hit("cheapo.md", "short ", score=0.4)
    # The best hit alone overflows the budget yet must still be kept.
    selected = select_context([cheapo, spends], budget_chars=100)
    assert [h["path"] for h in selected] == ["spends.md"]
    # With room for both, best-first fit returns the best then the cheaper one.
    selected = select_context([cheapo, spends], budget_chars=700)
    assert [h["path"] for h in selected] == ["spends.md", "cheapo.md"]
    assert sum(len(h["text"]) for h in selected) <= 700
    # With only 603 chars of budget the second hit no longer fits after the
    # 600-char top hit, so greedy selection stops.
    selected = select_context([cheapo, spends], budget_chars=603)
    assert [h["path"] for h in selected] == ["spends.md"]


def test_select_context_empty_and_zero_budget():
    assert select_context([], budget_chars=1000) == []
    always_kept = select_context([hit("a.md", "text ", score=0.5)], budget_chars=0)
    assert [h["path"] for h in always_kept] == ["a.md"]


def test_select_context_does_not_mutate_input():
    hits = [hit("a.md", "alpha ", score=0.3), hit("b.md", "bravo ", score=0.9)]
    snapshot = [dict(h) for h in hits]
    selected = select_context(hits, budget_chars=1000)
    assert [dict(h) for h in hits] == snapshot, "input hits must be untouched"
    assert not any("number" in h for h in hits), "hit dicts must not grow a number"
    assert selected[0]["path"] == "b.md", "selected copies re-rank freely"
    assert selected[0]["number"] == 1
    assert "number" in selected[1]


# -- build_prompt -------------------------------------------------------------


def test_build_prompt_exact_text_with_numbered_sources():
    selected = select_context(
        [hit("notes/topic.md", "The spec ships with a v1.0.0 release.\n",
             score=0.9)], budget_chars=4000)
    prompt = build_prompt("When does the spec ship?", selected)
    expected = (
        "Answer the question using ONLY the numbered sources below. Base every "
        "claim on a source and cite it as [n] where n is the source number. If "
        "part of the question is not covered by the sources, say so plainly; "
        "never invent an answer. Do not use knowledge outside these sources.\n\n"
        "Sources:\n"
        "[1] notes/topic.md\nThe spec ships with a v1.0.0 release.\n"
        "\nQuestion: When does the spec ship?"
    )
    assert prompt == expected


def test_build_prompt_no_hits_returns_honest_instruction():
    prompt = build_prompt("How do I reset the router?", [])
    assert NO_HITS_INSTRUCTION in prompt
    assert "nothing in the files covers it" in prompt
    assert "Do not invent an answer" in prompt
    assert "Do not use source markers" in prompt
    assert prompt.endswith("Question: How do I reset the router?")


def test_build_prompt_numbers_hits_without_explicit_number():
    hits = [hit("a.md", "alpha ", score=0.5), hit("b.md", "bravo ", score=0.9)]
    prompt = build_prompt("Which one?", [hits[1], hits[0]])
    assert "[1] b.md" in prompt and "[2] a.md" in prompt


# -- line_range ---------------------------------------------------------------


def test_line_range_boundaries():
    source = "line1\nline2\nline3\n"
    assert line_range(source, 0, 6) == (1, 1)   # a newline terminates its line
    assert line_range(source, 6, 6) == (2, 2)
    assert line_range(source, 12, 5) == (3, 3)  # no trailing newline, still line 3
    assert line_range(source, 0, 12) == (1, 2)  # two full lines, one range
    assert line_range(source, 0, 0) == (0, 0)


def test_marker_regex_only_matches_bracket_digits():
    reply = "See [1] and [12] but not [x] or [1.5] or plain brackets."
    numbers = [int(m.group(1)) for m in MARKER_RE.finditer(reply)]
    assert numbers == [1, 12]


# -- extract_citations --------------------------------------------------------


def test_extract_citations_resolves_to_source_lines_and_dedupes(tmp_path):
    chunk = "First line of the passage.\nSecond line of the passage.\n"
    note = write_file(tmp_path, "notes/topic.md",
                      "# Title\n\n" + chunk)
    offset = note.read_text(encoding="utf-8").index("First line")
    selected = select_context(
        [hit(str(note), chunk, offset=offset, score=0.9)], budget_chars=10000)

    result = extract_citations(
        "The flagship ships at three thousand [1] and the rival halves that "
        "price [1].",
        selected)
    assert result["dropped"] == []
    assert len(result["used"]) == 1
    cite = result["used"][0]
    assert cite["number"] == 1
    assert cite["path"] == str(note)
    assert (cite["line_start"], cite["line_end"]) == (3, 4)
    assert "First line" in cite["snippet"]
    assert cite["count"] == 2, "a repeated marker resolves once with its count"
    assert cite["text"] == chunk


def test_extract_citations_drops_out_of_range_markers(tmp_path):
    a = write_file(tmp_path, "notes/a.md", "# A\n\nFirst passage.\n")
    b = write_file(tmp_path, "notes/b.md", "# B\n\nSecond passage.\n")
    atext = a.read_text(encoding="utf-8")
    btext = b.read_text(encoding="utf-8")
    hits = [
        hit(str(a), "First passage.\n", offset=atext.index("First"), score=0.9),
        hit(str(b), "Second passage.\n", offset=btext.index("Second"), score=0.7),
    ]
    selected = select_context(hits, budget_chars=10000)

    result = extract_citations("Claim one [1] and claim three [3] plus [99].",
                               selected)
    assert [c["number"] for c in result["used"]] == [1]
    assert result["dropped"] == [3, 99], \
        "markers outside 1..len(selected) are reported as dropped, never cited"


def test_extract_citations_missing_file_reports_no_lines():
    selected = select_context([hit("nowhere.md", "vanished text.\n",
                                   offset=5, score=0.8)], budget_chars=1000)
    result = extract_citations("It is gone [1].", selected)
    cite = result["used"][0]
    assert cite["line_start"] is None and cite["line_end"] is None
    assert cite["offset"] == 5
    assert "vanished" in cite["snippet"]


# -- render_sources -----------------------------------------------------------


def test_render_sources_renders_markdown_source_list():
    used = [
        {"number": 1, "path": "notes/alpha.md", "line_start": 3, "line_end": 4,
         "offset": 0, "snippet": "The bazoo flagship costs  three thousand.",
         "count": 1},
        {"number": 2, "path": "notes/beta.md", "line_start": 7, "line_end": 7,
         "offset": 2, "snippet": "The rival ships for half.", "count": 1},
    ]
    rendered = render_sources(used)
    assert rendered.startswith("Sources:\n")
    assert "[1] notes/alpha.md lines 3-4: The bazoo flagship costs three " \
           "thousand." in rendered, "stray whitespace inside a snippet collapses"
    assert "[2] notes/beta.md line 7: The rival ships for half." in rendered


def test_render_sources_empty_and_unresolvable_line():
    assert render_sources([]) == ""
    used = [{"number": 1, "path": "gone.md", "offset": 42,
             "snippet": "no line info", "count": 1}]
    rendered = render_sources(used)
    assert "[1] gone.md at char 42: no line info" in rendered


# -- full pipeline ------------------------------------------------------------


def test_full_pipeline_select_prompt_extract_render(tmp_path):
    a = write_file(tmp_path, "notes/alpha.md",
                   "# Alpha\n\nThe bazoo flagship costs three thousand.\n")
    b = write_file(tmp_path, "notes/beta.md",
                   "# Beta\n\nThe rival ships for half the price.\n")
    atext = a.read_text(encoding="utf-8")
    btext = b.read_text(encoding="utf-8")
    hits = [
        hit(str(a), "The bazoo flagship costs three thousand.\n",
            offset=atext.index("The bazoo"), score=0.9),
        hit(str(b), "The rival ships for half the price.\n",
            offset=btext.index("The rival"), score=0.7),
    ]
    selected = select_context(hits, budget_chars=5000)
    prompt = build_prompt("What does the flagship cost?", selected)
    assert "[1]" in prompt and "[2]" in prompt

    # The reply shape a grounded model is asked to produce: each claim carries
    # the marker of the source that backs it.
    reply = ("The flagship costs three thousand [1] and the comparison note "
             "says the rival is half that price [2].")
    result = extract_citations(reply, selected)
    assert result["dropped"] == []
    assert [c["number"] for c in result["used"]] == [1, 2]
    assert {c["path"] for c in result["used"]} == {str(a), str(b)}

    # The second hit points at line 3 of beta.md (heading, blank, passage).
    beta_cite = result["used"][1]
    assert beta_cite["path"] == str(b)
    rendered = render_sources(result["used"])
    assert str(a) in rendered and str(b) in rendered
    assert rendered.startswith("Sources:\n")
