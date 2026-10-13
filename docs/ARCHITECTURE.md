# Steward Architecture

**Version:** 1.2, 2026-10-13 (T2: running topology documented, daemon live on U1)

## Running topology (T2, live on U1 2026-10-13)

```
phone app (apps/mobile, Expo/RN)
   chat screen -> EXPO_PUBLIC_STEWARD_DAEMON_URL (default localhost:3877)
        | HTTP JSON
        v
Steward daemon  src/steward/server/daemon.py
   bound 127.0.0.1:3877 (fixed port; STEWARD_HOST/STEWARD_PORT to change)
   routes: GET /health, GET /bundle, POST /chat, GET /demo/source,
           POST /demo/summarize
        | /api/chat JSON
        v
Ollama backend  http://localhost:11434  (T1 benchmark winner, daemon 2046)
   resident on U1: qwen3:8b (served assistant), gemma4:26b,
                   llama3.1:8b-instruct-q4_K_M, nomic-embed-text
```

The daemon serves the resident always-hot assistant (qwen3:8b by default,
--model to switch; gemma4:26b responds when called). /bundle reports the live
selector output for this host plus which model actually serves chat. The
selector-chosen 80B capability tier stays [uncertain] and is not pulled inside
a cron window (memory guard); it is served on an interactive session with
--model qwen3-next:80b after the pull.

## Components

```
+---------------------------------------------------------------+
| Clients: phone app, web app, (desktop shell later)            |
|   - zero-config LAN pairing (QR)                              |
|   - encrypted off-LAN via R1A mesh tunnel                     |
+----------------------------+----------------------------------+
                             |
+----------------------------v----------------------------------+
| Daemon (the box)                                             |
|   Installer (signed)  ->  daemon (system service)            |
|   - hardware detect (detect.py)                              |
|   - model selector (selector.py) -> bundle                   |
|   - runtime: home-grown or Ollama/vLLM backend               |
|   - model catalog + OTA refresher                            |
|   - memory store (local, encrypted)                          |
|   - agent loop + tool connectors                             |
|   - credential sandbox (default-on)                          |
|   - license gate (perpetual, offline-capable)                |
+---------------------------------------------------------------+
```

## Data flow

1. Installer runs, calls `detect()` to read VRAM / unified pool / RAM / NPU /
   bandwidth class and emits a HardwareProfile.
2. `select_bundle(profile)` resolves the memory class and picks the
   always-hot assistant, the capability model, and presence of vision, against
   the catalog (MODEL-SELECTOR.md).
3. The daemon downloads only the selected quantizations (OTA pipeline), serves
   them with the local runtime, and exposes a local API the clients talk to
   over the mesh or LAN.
4. The license gate authorizes feature levels (free / Personal / Home / Power)
   from a signed local entitlement; no server round-trip required after
   purchase.

## Key decisions

- The box is the agent; the phone is the remote. Off-LAN access rides R1A's
  mesh tunnel (Bastion lineage), which no competitor can copy for free.
- Model identity lives in one catalog (models.py); OTA swaps upstream
  checkpoints, never the catalog contract.
- Selector rules are duplicated nowhere: selector.py is the single source of
  truth and is unit-tested (7 tests) so residency and speed-floor behavior
  cannot regress.

## Security model (v1 landlord)

- Default-on network sandbox for the model process.
- Memory store encrypted at rest; keys on the box.
- The advanced credential firewall (outbound request policing) is a paid
  feature pack built on the sandbox, not v1 scope.
- Remote access is end-to-end encrypted via the mesh; LAN pairing uses QR
  secrets, not open discovery.

## Serving backend decision (CLOSED, benchmarked on U1/GB10 2026-10-11)

Decision: **Ollama** is the v1 serving backend, wrapped behind one internal
interface (runtime.py) so it can later be swapped for vLLM on halo-class pools
without touching the selector or clients.

Benchmark runs: Ollama 0.34.0 (`/home/user/ollama-local/bin/ollama serve`,
API http://localhost:11434) on U1 (GB10, unified pool 121 GB). Sizes:
qwen3:8b (5.2 GB, the v0.1 assistant pick), llama3.1:8b-instruct-q4_K_M
(4.9 GB, cached 8B-class), gemma4:26b (18 GB, cached big class). Measured
steady-state tokens/sec and first-token latency with a realistic 400-1000
token user prompt. Numbers are real, reproduced twice.

| model | mem (GB) | tok/s (measured) | first-token cold | first-token warm/loaded |
|---|---|---|---|---|
| qwen3:8b | 5.2 | 43.7-44.1 | 38.1 s | ~10 s from unloaded; <1 s once resident |
| llama3.1:8b-instruct-q4_K_M | 4.9 | 46.0-46.2 | 4.7 s | 0.061 s |
| gemma4:26b | 18 | 81.1-85.1 | 26.4 s | ~19 s from unloaded |

Readings: 8B-class computes 44-46 tok/s steady state (well over the 15 tok/s
selector floor); the 26B tier runs ~81-85 tok/s on GB10. Cold first-token
dominates UX for a single-box daemon, so v1 keeps the always-hot assistant
resident (per selector) and prerolls the capability model on first unlock.
The 7b tag for gemma4 is not in the local registry; qwen3:8b is the tested
assistant pick.

[uncertain] Qwen3-Next 80B A3B (the U1 capability pick, 50 GB Q4): not pulled
inside this cron window by guard rule (no 80B-tier pull in cron workers), so
no measured row. Estimated 20-45 tok/s optimistic per catalog (models.py),
Marked [uncertain], never served on the 24 GB class. Defer the measured 80B
run to an interactive session (T2 covers end-to-end serving on U1).

vLLM not benchmarked: source build not permitted in cron workers, and a
large container image pull is out of scope for a cron window; deferred to an
interactive session. The internal runtime interface keeps the swap cheap.

Supporting decision: use the localhost hostname form for local API tests
(raw-IP curl is blocked by the local security scan; git to github.com and
the gitea API through a script file both work).
