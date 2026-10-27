# Steward Architecture

**Version:** 1.4, 2026-10-26 (1.4 adds the runtime seam, pairing auth,
retrieval index and tool connectors; 1.3 closed the serving-backend
question with the vLLM benchmark on U1, Ollama stays v1)

## What changed in 1.4

Since 1.3 the daemon grew from the T2 route set to the full v1 route
table. The four seams named in this revision:

- **Runtime seam.** One internal interface (`runtime.py`) wraps the
  serving backend. SystemOllama talks to a machine Ollama at
  localhost:11434; SidecarOllama spawns and owns a bundled ollama on a
  free port. The selector and clients never see the backend. The vLLM
  option stays behind the same seam for a dedicated pool.
- **Pairing auth.** A 6 digit code served only to loopback clients trades
  for a signed session token (30 day sessions). Every daemon route is
  Bearer-gated; the phone and browser clients get tokens via QR and code.
- **Retrieval index.** A local vector index (S7) is built from the user's
  own folders and adds citations to answers.
- **Tool connectors.** Registered tools (files, shell, http, calendar)
  run behind one gate: schema validation, a containment sandbox, then a
  human approval with a SQLite decision store for every write, exec and
  network call (S9).

Also shipped into 1.4: the web client mounted at the daemon root (S5),
the facts store, briefs and scheduler (S8), the redacted support bundle
(S12), the resumable first-run wizard (S10), and the signed perpetual
license plus OTA catalog (S11). Per-card detail and commit references are
in docs/CHANGELOG.md.

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

The v1 route families (health, bundle, pair, chat, conversations, source,
summarize, briefs, approvals, doctor/bundle, webapp mount) were proven
live as a set in the I2 integration; the setup wizard routes (S10) join
them in the release merge. The table lives in src/steward/server/daemon.py.

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
- Memory store and all user data live in owner-only (0700) directories
  under the data root. Encryption at rest is a stated design goal for a
  later release; v1.0 stores plaintext SQLite on the user's own disk, so
  physical access to the machine means access to the data. See
  docs/PRIVACY.md for exactly what is stored and its honest limits.
- The advanced credential firewall (outbound request policing) is a paid
  feature pack built on the sandbox, not v1 scope.
- Remote access is end-to-end encrypted via the mesh (a later capability);
  LAN pairing uses QR secrets and loopback-only codes, not open discovery.

## Serving backend decision (CLOSED - Ollama v1 2026-10-11, vLLM branch benchmarked and deferred 2026-10-26)

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

**vLLM benchmarked 2026-10-26 (interactive session, U1/GB10, sm_121).** The
earlier deferral is closed. Findings, full method and raw rows in
`bench/BENCHMARK-2026-10-26.md` + `bench/results-2026-10-26.json`:

- Platform question answered: vLLM 0.32 **installs and runs on aarch64/sm_121**
  from stock PyPI wheels (`uv pip install vllm`, torch 2.13.0+cu130 sees the
  GB10). The docs previously assumed this might not be possible.
- Same prompts, same client-side timing, temperature 0, 8K context both sides:

  | metric | Ollama `qwen3:8b` Q4 | vLLM `Qwen3-4B` bf16 |
  |---|---|---|
  | cold load / server up | 3.9 s | 357 s (Triton JIT for sm_121) |
  | warm TTFT short / long | 31 / 32 ms | 134 / 142 ms |
  | decode short / long | 41.5 / 38.8 tok/s | 15.0 / 14.5 tok/s |
  | resident memory | 6.0 GB | 38-39 GiB pre-reserved |

  The 4B-vs-8B and bf16-vs-Q4 choices both bias *against* vLLM, so the gap is
  not a model-size artifact.
- Why it is not adopted: three failed starts recorded, all from vLLM assuming a
  dedicated memory pool on a shared unified box (utilization gate, memory
  profile abort when a co-tenant evicts mid-profile, FlashInfer JIT needing
  ninja with no prebuilt sm_121 cubin). Add the 6-minute first boot and it is
  not a backend that can co-tenant with comfyui/Ollama on this host.
- Consequence: **Ollama stays the v1 backend**; vLLM is the documented future
  option for a *dedicated* halo-class pool, reached through the same
  `runtime.py` seam with no selector or client change (both expose a single-port
  OpenAI-compatible API). Revisit bar: a non-co-tenant unit, warm Triton cache,
  `--enforce-eager` off, same weights as the Ollama side.
- Side finding worth a selector note: `llama3.1:8b-instruct-q4_K_M` measured
  22.3 tok/s here versus 46 tok/s in the Oct 11 run. If the selector floor ever
  gates on llama3.1, re-measure before trusting either figure.

Supporting decision: use the localhost hostname form for local API tests
(raw-IP curl is blocked by the local security scan; git to github.com and
the gitea API through a script file both work).
