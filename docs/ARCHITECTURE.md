# Steward Architecture

**Version:** 1.0, 2026-10-10 (initial)

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

## Serving backend decision (open item)

Prototype does not serve models yet. For v1 pick the backend with the best
state of the art on N-class certified hardware: likely Ollama (Ubuntu/Windows
mini PCs, Apple Silicon, GB10) or a vLLM container on GB10/Halo-class pools,
wrapped behind one internal interface so the choice can change without
touching the selector or clients. Decide by benchmark on certified devices
before v1 freeze. Marked [uncertain] until benchmarked.
