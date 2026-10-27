# Steward daemon (src/steward/server)

The local API a phone talks to on the LAN. It fronts the model backend (Ollama
through the `steward.runtime` seam from S1, never urllib to Ollama directly)
with a hardened HTTP surface: pairing first, everything gated, conversations
kept in SQLite.

Bind: `0.0.0.0` by default (so a phone on the LAN can reach it), port 3877.
Set `STEWARD_HOST=127.0.0.1` to keep it loopback-only. Discovery is mDNS:
`steward-<port>._steward._tcp.local.` with a TXT record of version, port,
model, paired_count.

## Auth model and pairing flow

Only two routes are open without a token: `/health` and `/v1/bundle`
(read-only status). Everything else requires `Authorization: Bearer <token>`.

How a device gets a token:

1. On startup the daemon prints a fresh 6-digit pairing code and the pairing
   URL / QR payload (`steward://pair?host=<ip>&port=<port>`). The code is
   also served at `GET /pair/code`, but only to loopback clients, so a
   stranger on the LAN cannot scrape it.
2. The client sends `POST /v1/pair` with `{"code": "123456", "device": "My
   Phone"}`. On success it receives a signed session token.
3. The token is stateless HMAC-SHA256 (`sv1.<exp>.<device>.<nonce>.<sig>`),
   valid for 30 days, signed with a key persisted once at
   `<data>/db/pairing.key` (owner-only 0o600). Because the key persists,
   tokens survive daemon restarts; rebooting never force-repairs a device.
4. Failed pairing is rate-limited per client IP: 5 attempts per 5 minutes,
   after which `POST /v1/pair` returns 429 until the window rolls off.
   Success resets the counter.

The code is valid for the life of the daemon run and rotates on restart.
`/health` reports `paired`, the number of distinct device labels that have
ever paired this run.

## Routes

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | daemon + backend status, served model, paired count |
| GET | `/v1/bundle` | none | live selector output for this host (canonical) |
| GET | `/bundle` | none | shim for `/v1/bundle`, one-release compatibility |
| GET | `/pair/code` | none (loopback only) | current 6-digit pairing code |
| POST | `/v1/pair` | none (rate-limited) | `{code, device}` -> signed session token |
| POST | `/v1/chat` | Bearer | SSE stream of model deltas, ends with `data: [DONE]` |
| POST | `/chat` | Bearer | JSON shim for `/v1/chat` (old app contract) |
| GET | `/v1/source` | Bearer | list files in the data-source folder (canonical) |
| GET | `/demo/source` | Bearer | shim for `/v1/source` |
| POST | `/v1/summarize` | Bearer | read data source, summarize through the model |
| POST | `/demo/summarize` | Bearer | shim for `/v1/summarize` |
| GET | `/v1/conversations` | Bearer | list conversations, newest first, with counts |
| POST | `/v1/conversations` | Bearer | create a conversation `{title?}` |
| GET | `/v1/conversations/{id}` | Bearer | conversation + its messages |
| DELETE | `/v1/conversations/{id}` | Bearer | delete conversation and its messages |
| POST | `/v1/conversations/{id}/messages` | Bearer | append `{role?, content}` |

### POST /v1/chat (streaming)

Body: `{"messages": [{"role", "text"}], "text": "current turn"}` (the T2 app
shape) or `{"messages": [{"role", "content"}]}`, plus optional
`"conversation_id"`. Reply is `text/event-stream`:

```
data: {"delta": "Hello "}

data: {"delta": "from "}

...

data: [DONE]
```

The connection closes right after `[DONE]`. When `conversation_id` names a
live conversation, the user turn and the full assistant reply are written to
the messages table. Unknown conversation ids are a 404 before any model call.

### POST /chat (JSON shim)

Same body, but a single JSON reply in the v0.1 shape the app client consumes:
`{"id", "role": "assistant", "text", "model"}`. Now Bearer-gated. Kept for
one release; new clients should use `/v1/chat`.

## Conversation store

SQLite at `<data>/db/steward.db`, WAL mode, `busy_timeout` 5000. Tables:

* `conversations(id, title, created_at)`
* `messages(id, conversation_id, role, content, created_at, model)`

Every operation opens a short-lived connection, so concurrent handler threads
are safe. `created_at` is ISO-8601 UTC. `DELETE /v1/conversations/{id}`
cascades to its messages.

## Discovery

On startup the daemon publishes `steward-<port>._steward._tcp.local.` via
zeroconf under the hostname, advertising the primary LAN address plus
loopback, with TXT: `version`, `port`, `model`, `paired_count`. The TXT is
refreshed after every successful pairing. If zeroconf is missing the daemon
warns and runs without mDNS. LAN phones need multicast (mDNS) on their
subnet; that is the normal case on a flat home or office LAN, and some
multi-bridge or containerized hosts restrict it.

## Run it

Ollama must be serving or a sidecar binary must be available (runtime seam
handles both). From the repo root:

```
PYTHONPATH=src python3 -m steward.server.daemon --model qwen3:8b
```

Environment overrides: `STEWARD_HOST`, `STEWARD_PORT`, `STEWARD_MODEL`,
`STEWARD_OLLAMA_URL`, `STEWARD_DEMO_SOURCE` (real data source for the demo,
default repo/demo/source). A `steward serve` CLI alias exists too.
`--no-mdns` disables advertisement.

## Files

* `auth.py` pairing code, HMAC session tokens, per-IP pair rate limiting
* `store.py` SQLite conversation and message store
* `mdns.py` zeroconf advertisement with TXT record
* `daemon.py` the HTTP server, routes, auth gating, SSE streaming
* `README.md` this file

## Test

```
uv run --with pytest --with zeroconf --no-project python -m pytest tests
```

The daemon suite spins real servers on port 0 with a fake runtime and asserts
the auth matrix, incremental SSE, pairing lockout, conversation persistence
across a restart, and the mDNS TXT record via an actual browse.
