# Steward tools from chat (F8)

Tools are now reachable from chat, on both surfaces. The server owns the tool
loop and enforces every approval; the web client and the phone app render the
tool steps and the approval prompts. This file states what is reachable today
and what is still not, and documents the SSE contract the clients stream.

## What is reachable now

Both chat entry points run the agent tool loop, which means a reply that is a
tool call executes the tool instead of being returned as raw text with a JSON
blob in it:

* `POST /v1/chat` streams every model delta and every tool step over SSE using
  the frozen contract below. This is the web client path.
* `POST /chat` runs the same loop but returns one JSON object (final model
  text only), the phone friendlier shim. No SSE events are emitted.

The loop is the same library (`run_loop`) with the same guarantees as the
unit tests: schema validation before any handler runs, human approval for
write/exec/net, a 4-hop cap, and denied or timed-out approvals returning a
model-visible denial with no side effect.

### The SSE contract (frozen, do not rename)

Every line is `data: <json>` followed by a blank line; the stream ends with
`data: [DONE]`.

* `{"delta": "..."}` model text as the model generates it
* `{"event":"tool_call","id":"<uuid>","tool":"<name>","args":{...},"risk":"read|write|exec|net"}`
* `{"event":"approval_required","id":"<uuid>","tool":"<name>","args":{...},"risk":"...","expires_at":"<iso8601>"}`
* `{"event":"tool_result","id":"<uuid>","ok":true|false,"summary":"<text>","ms":123}`
* `data: [DONE]`

Notes for client authors:

* `id` is a fresh UUID per tool step; tool_call, approval_required and
  tool_result for the same step carry the same `id`.
* `approval_required` carries one extra field, `approval_id` (the integer key
  of the pending row), so the client can decide it with a direct
  `POST /v1/approvals/{approval_id} {"approve": true|false}` without matching
  rows by hand. It also appears under `GET /v1/approvals` as usual.
* A tool-call reply is never leaked as deltas. The stream holds any reply
  that could still be a lone JSON tool call and replaces it with the
  tool_call event, so no `{"tool": ...}` text ever reaches the delta stream.
  Prose streams incrementally as today.
* `approval_required` pauses the turn. The daemon thread blocks on the
  pending approval for up to the configured timeout (default 120 s); a human
  decides on a separate connection via the existing approvals routes, and the
  stream resumes with tool_result. A deny or a timeout returns `ok:false`
  and the model is told to continue without the tool.

Identifiers: an approval-required tool creates a row in the approvals store
(`<data>/db/approvals.db`), emits approval_required, and only runs after
`POST /v1/approvals/{id}` approves it.

### The sandbox and the config knobs honored

* `fs_roots`: the sandbox for fs.list, fs.read, fs.write and the calendar
  tools is the daemon data root (where db, logs and pair state live), the
  same root the tests and setup use. Sensitive material (.ssh, .env, private
  keys, key suffixes) is refused inside it, traversal and symlink escapes are
  refused, reads are capped and binary files refused.
* `allow_shell`: shell.run is registered so the model sees it, but refuses to
  run unless `allow_shell` is true in `<data>/config.json`; even then it
  needs an approval. The serve CLI passes the existing `Config.allow_shell`.
* http allowlist: http.fetch is registered but its allowlist is empty by
  default, so it refuses every host. No URL is reachable until an operator
  ships a filled allowlist through `create_server(tool_overrides=...)`.
* `HOP_CAP`: the tool chain is capped at 4 executions per turn; a further
  tool request at the cap is refused and the final text returned.
* Approvals persist in `<data>/db/approvals.db` with an auditable JSON-lines
  log of every creation and decision.

No new config keys were added. Tool defaults come from the existing
`ToolSettings`; the daemon only surfaces the already-existing `allow_shell`
knob into it.

## What is still not reachable

* `http.fetch` is unreachable in the running product: the https allowlist has
  no persisted config key yet, so the default is empty and every URL is
  refused. Wiring an allowlist entry point is follow-on work.
* Extra `fs_roots` beyond the data dir are not configurable from the running
  product yet; a request to expand the sandbox needs a config key.
* The web client (src/steward/webapp) renders tool activity inside the chat
  turn and answers approvals in place: each step shows the tool, its risk and
  a one-line argument summary, a running spinner, then an ok or failed result,
  and an approval prompt offers one-tap Approve or Deny with an expiry
  countdown. Wiring lives in `onToolCall`, `onApprovalRequired` and
  `onToolResult` in app.js.
* The phone app (apps/mobile) shows the same tool steps and a bottom-sheet
  approval prompt with Approve, Deny and the expiry countdown, so a
  write/exec/net request can be answered from the phone without the web
  client. The API route (`POST /v1/approvals/{id}`) remains available for
  scripts and tests.
* `calendar.read`/`calendar.write` only reach ICS files inside the data root;
  adding a separate calendar feed location is follow-on work.
