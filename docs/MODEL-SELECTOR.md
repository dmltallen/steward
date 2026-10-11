# Steward Model Selector

**Version:** 1.0, 2026-10-10 (initial). Canonical implementation lives in
`src/steward/selector.py` and the catalog in `src/steward/models.py`. This
document explains the ladder and rules; the code is the source of truth.

## Memory classes

| Usable memory | Class | Capability model | Always-hot | Vision | Est. speed |
|---|---|---|---|---|---|
| <10 GB | entry | 7B | same | no | 15-90 tok/s |
| 10-14 GB | small | 8B | same | no | 25-100 tok/s |
| 14-20 GB | mainstream-16 | 14B | same | no | 15-110 tok/s |
| 20-28 GB | mid-24 | 27B (Ghost class) | same | no | 13-50 tok/s |
| 28-34 GB | high-32 | 32B | same | no | 10-45 tok/s |
| 34-60 GB | power | 70B | 7-8B | yes* | 8-38 tok/s |
| 60-96 GB | super | 70B or 80B MoE | 7-8B | yes | 20-45 tok/s |
| 96-140 GB | extreme | 80B MoE | 7-8B | yes | 20-45 tok/s |
| 140+ GB | halo | 109B MoE, multi-agent | 7-8B | yes | 10-30 tok/s |

\* Vision requires the pool be at least 40 GB and at least 6 GB left after the
capability and assistant picks.

## Rules (versioned)

1. Serving memory = VRAM (discrete), unified pool (Apple/GB10/Strix Halo),
   system RAM (iGPU/NPU boxes).
2. Residency must fit memory x 0.80 (KV cache, context, memory store, OS).
3. Optimistic tokens/sec must clear the 15 tok/s real-time floor.
4. Small discrete boxes (<40 GB) run one model: the capability model is also
   the assistant. Always-hot split and vision only on big pools and unified
   machines where headroom is real.
5. Llama 4 Scout (109B MoE) is restricted to the halo class; the 80B MoE is
   the ceiling for everything below it.
6. Re-select on any hardware change and on every OTA update; the user may
   override from the dropdown but never has to.

## Ghost-comparison note (why the ladder matters)

Ghost Core ships 27B/31B models on a 24 GB card, which is the "mid-24" row:
Steward matches that tier exactly on any 24 GB discrete card (used 3090, RTX
PRO 4000). Ghost also lists an 80B MoE model on that same 24 GB card; an 80B
MoE at Q4 needs roughly 48 GB, so that claim is the one reviewers will likely
probe and Steward must not repeat the over-claim. At 48 GB and up Steward
leaves Ghost's capability ceiling behind entirely. Known-good reference: the
r/LocalLLaMA dual-RTX-PRO-4000 run of Qwen3-Next-80B-A3B at 43.77 tok/s.
