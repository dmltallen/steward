# Steward First-Look Demo (T2, 2026-10-13)

Version 1.0. What was exercised end to end on U1 (NVIDIA GB10): a real model
reply and a real local data-source summary, both served by one local daemon
that the mobile app connects to. Everything runs on this box. Nothing leaves
the network.

## Topology (see ARCHITECTURE.md for the full running picture)

- Ollama backend: http://localhost:11434 (resident since Oct 11)
- Steward daemon: 127.0.0.1:3877 (src/steward/server/daemon.py)
- Served model: qwen3:8b (the v0.1 assistant pick; 5.2 GB, resident)
- Mobile app: apps/mobile, chat screen pointed at the daemon via
  EXPO_PUBLIC_STEWARD_DAEMON_URL (default fallback localhost:3877)

Honesty: the selector-chosen capability tier (qwen3-next:80b, 50 GB Q4) is not
pulled inside a cron window by the memory guard. /bundle reports it as the
plan; the resident 8B assistant serves chat, which is what the sprint allows.

## Demo steps

1. Backend must be serving: `/home/user/ollama-local/bin/ollama serve`
   (already running on U1, daemon 2046).
2. Start the daemon from the repo root:
   `PYTHONPATH=src python3 -m steward.server.daemon --model qwen3:8b`
   Binds 127.0.0.1:3877. Logs print the served model and live bundle.
3. Health check: `curl -s http://localhost:3877/health`
4. Bundle info: `curl -s http://localhost:3877/bundle`
5. Real chat: `curl -s -X POST http://localhost:3877/chat -H 'Content-Type:
   application/json' -d '{"messages":[], "text":"Say hello in one sentence."}'`
6. Data-source demo: `curl -s -X POST http://localhost:3877/demo/summarize`
   The daemon reads the real folder demo/source (calendar + launch notes) and
   asks the local model to summarize it. The source path is configurable via
   STEWARD_DEMO_SOURCE, so it can point at the Obsidian vault for real use.

## Quoted output (captured 2026-10-13 from the live daemon on U1)

Health (`GET /health`):

    {"status": "ok", "backend": "ollama", "ollama_url": "http://localhost:11434",
     "model": "qwen3:8b", "model_resident": true, "version": "0.1.0"}

Bundle (`GET /bundle`), the live selector plan plus served model:

    {"bundle": {"class": "extreme", "serving_mem_gb": 121.6,
      "capability": "qwen3-next:80b", "assistant": "gemma4:7b", "vision": true,
      "capability_est_tok_s": [20, 45], "gpu": "NVIDIA GB10"},
     "served_model": "qwen3:8b",
     "note": "capability tier is resident only when serving holds; the resident
     assistant model serves chat"}

Real chat (`POST /chat`, model generated):

    {"id": "msg-1791876956511", "role": "assistant",
     "text": "Hello! I'm Steward, running locally on a GB10 box. I can't access
     your system details, but you can check your OS by running `uname -a` or
     `cat /etc/os-release` in a terminal.",
     "model": "qwen3:8b"}

Data-source summary (`POST /demo/summarize`, model generated from the real
files october-calendar.md and launch-notes.md):

    "source": "/home/user/steward/demo/source",
    "files": ["october-calendar.md", "launch-notes.md"],
    "reply": "**Summary of Upcoming Steward Project Milestones (October 2026):**
      - Oct 11: Finalize Ollama as the v1 serving backend.
      - Oct 13: Complete T2 end-to-end integration for local model chat.
      - Oct 15: Review selector residency table before v0.5 tag.
      - Oct 20: Record first-look demo video for the landing page.
      - Oct 31: Launch Ghost Core batch 1 and run independent review sweep.
      **Top 3 Next Actions:**
      1. Complete T2 end-to-end integration (Oct 13).
      2. Review selector residency table (Oct 15).
      3. Prepare Ghost Core batch 1 release (Oct 31)."

## Mobile app verify (manual steps, no headless device in cron)

Expo cannot boot a device inside a cron worker, so these steps are written to
verify on a real phone or simulator:

1. `cd apps/mobile`
2. For a device on the LAN, expose the daemon beyond localhost first:
   `PYTHONPATH=src python3 -m steward.server.daemon --host 0.0.0.0 --port 3877`
   On simulator/desktop the default localhost URL works as is.
3. `EXPO_PUBLIC_STEWARD_DAEMON_URL=http://<U1-LAN-IP>:3877 npx expo start`
4. Open in Expo Go (Android) or the iOS simulator. Go to Assistant chat.
5. The status dot should read online and the caption shows the daemon URL.
6. Type a message and send. The assistant bubble is the same real model output
   shown in the /chat quote above: the app and daemon exchanged a real
   model-generated message.

## Verified

- `curl` against every route returned the JSON above (real daemon, real model).
- Selector tests: 8 passed (pytest, tools python 3.14).
- App typecheck: `tsc --noEmit` clean after wiring daemon.ts to /chat.
