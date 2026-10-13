# Steward daemon (src/steward/server)

A small local HTTP API that serves the Steward model bundle on this host and
fronts it for the mobile app. Backend is Ollama (the T1 benchmark winner) at
http://localhost:11434. Bound to 127.0.0.1 on a fixed port, default 3877.

## Routes

| Method | Path              | Purpose                                        |
|---|---|---|
| GET    | /health           | daemon + backend status, served model           |
| GET    | /bundle           | live selector output for this host              |
| POST   | /chat             | real model-generated reply (see curl below)     |
| GET    | /demo/source      | list files in the data-source folder            |
| POST   | /demo/summarize   | read the data source, summarize via the model   |

/chat body: `{"messages":[{"role":"user","text":"..."}],"text":"current turn"}`.
Reply: `{"id":"...","role":"assistant","text":"<model output>","model":"..."}`,
the exact shape the app client in apps/mobile/src/lib/daemon.ts consumes.

## How to run it

Ollama must be serving first (already running on U1):
`/home/user/ollama-local/bin/ollama serve` on http://localhost:11434.

From the repo root:

```
PYTHONPATH=src python3 -m steward.server.daemon --model qwen3:8b
```

Environment overrides: STEWARD_HOST, STEWARD_PORT, STEWARD_MODEL,
STEWARD_OLLAMA_URL, STEWARD_DEMO_SOURCE (the real local data source for the
demo, default repo/demo/source). A `steward serve` CLI alias exists too.

## Verify

```
curl -s http://localhost:3877/health
curl -s http://localhost:3877/bundle
curl -s -X POST http://localhost:3877/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[], "text":"Say hello in one sentence."}'
curl -s -X POST http://localhost:3877/demo/summarize
```

## Honesty notes

- The daemon serves the always-hot resident assistant (qwen3:8b on U1), per
  the ARCHITECTURE.md decision. The selector-chosen 80B capability tier is not
  resident in a cron window (guard rule), so /bundle reports it as the plan
  and /health reports the model actually serving chat. Swap with --model
  gemma4:26b when big-class serving is wanted and resident.
- No source builds, no model pulls at runtime. The daemon only talks JSON to
  the already-running Ollama daemon. Use the localhost hostname form for local
  API tests; raw-IP terminal commands are blocked by the host security scan.
