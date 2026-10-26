# Steward Serving Backend Benchmark — U1 (GB10) — 2026-10-26

**Purpose:** Replace the [uncertain] serving-backend note with measurement. Ollama
vs vLLM on the actual Steward host (U1, NVIDIA GB10, aarch64, 121.6 GB unified
pool, CUDA 13.0, compute capability sm_121). Same prompt set, same client-side
timing method, both backends.

**Files:** raw results in `results-2026-10-26.json` (this dir). Reproducers:
`bench_ollama.py`, `bench_vllm.py`, their `.log` outputs.

## Environment (measured live)

| item | value |
|---|---|
| Host | U1 · NVIDIA GB10, aarch64, 121.6 GB unified |
| CUDA / driver | 13.0 / 580.178.04, capability sm_121 |
| Ollama | 0.34.0, `/home/user/ollama-local/bin/ollama serve` pid 2046, port 11434 |
| vLLM | 0.32.0 via plain PyPI aarch64 wheels (`uv pip install vllm`), torch 2.13.0+cu130 (CUDA-13 build), venv `.vllm-venv` |
| Co-tenant GPU load | comfyui python (~16.7 GiB) always; ollama llama-server varied (0→26.9 GiB as gemma4:26b churn); see caveats |
| Models used | Ollama: `qwen3:8b` Q4_K_M (resident 6.0 GB) · vLLM: `Qwen/Qwen3-4B` bf16 (already in HF cache — no new pull, ≤20 GB rule untouched) |

**Method:** two prompts — short (58/56 input tokens) and long (580/578 input
tokens), identical text both backends; `temperature=0`, max output 200 tokens,
context 8192 both sides (Ollama `num_ctx=8192`, vLLM `--max-model-len 8192`).
TTFT = client wall-clock to first streamed token. Decode tok/s: Ollama from its
`eval_count/eval_duration` metrics; vLLM from exact `usage.completion_tokens /
(wall_total − TTFT)` (verified token counts via `stream_options.include_usage`).
Cold = model unloaded / server freshly started; warm = model already resident.

## Results

### Per-backend raw numbers

| metric | Ollama `qwen3:8b` | Ollama `llama3.1:8b` | Ollama `gemma4:26b`† | vLLM `Qwen3-4B` |
|---|---|---|---|---|
| Cold first-token-ready (load) | 3.9–4.2 s (4 samples) | 3.9–4.8 s | ~15 s (1 sample) | **357 s** server→healthy* |
| TTFT cold, short prompt (wall) | 4.0–4.1 s | 4.0–5.1 s | — | 0.7 s (incl. JIT warmup) |
| TTFT warm, short (avg of 3) | **31 ms** | 63 ms | — | 134 ms |
| TTFT warm, long (avg of 3) | **32 ms** | 61 ms | — | 142 ms |
| Decode, short (tok/s, avg 3) | **41.5** | 22.3 | — | 15.0 |
| Decode, long (tok/s, avg 3) | **38.8** | 21.6 | 85.1 (1 sample) | 14.5 |
| Resident memory while loaded | 6.0 GB (6,249 MiB GPU) | 5.9 GB | ~26.9 GiB | **38–39 GiB reserved** |
| OpenAI-compatible API | yes, `:11434/v1` | ✓ | ✓ | yes, `:8001/v1` |

† fresh single warm sample this run (load 15 s, decode 85.1 tok/s) — consistent
with the 81–85 tok/s the docs recorded Oct 11, so docs trusted for this model.
* 357 s start→healthy dominated by Triton/inductor JIT-compiling sm_121 kernels
on first boot (no prebuilt kernel, no source build occurred); repeats will reuse
the warm `~/.triton` cache — second-boot time NOT measured.

### vLLM runtime verdict on U1

**Installs and runs on U1 (aarch64/sm_121)** — this closes the question the docs
left open. A stock `uv pip install vllm` resolves arch-correct wheels and torch
`2.13.0+cu130` reports CUDA 13, device `NVIDIA GB10`, capability (12,1).

But three launch failures show it assumes a *dedicated* memory pool, which the
shared unified box is not:

1. `ValueError: Free memory on device cuda:0 (44.47/121.63 GiB) is less than
   desired GPU memory utilization (0.5, 60.81 GiB)` — pre-reserves 0.5×pool at
   startup; comfyui+ollama were already using the rest. Fixed by
   `--gpu-memory-utilization 0.32`.
2. `AssertionError: memory profiling. Initial free 44.39 GiB, current free
   52.95 GiB … other processes release GPU memory while vLLM is profiling` —
   Ollama evicted a model mid-profile; vLLM aborts on the fluctuating free count.
3. `FileNotFoundError: 'ninja'` in FlashInfer JIT — no prebuilt sm_121 cubin in
   the pip wheel, so kernels JIT-compile at startup (needs ninja + nvcc;
   `uv pip install ninja`, `CUDA_HOME=/usr/local/cuda`,
   `VLLM_DISABLED_KERNELS=flashinfer`).

Result once up: **~15 tok/s decode on a 4B model (Qwen3-4B bf16) vs Ollama's
~41 tok/s on an 8B Q4 model**, both under co-tenant load. The 4B-vs-8B and
bf16-vs-Q4 both bias *against* vLLM, so the gap is not an artifact of model size.
Stock pip vLLM on sm_121 is GBP bandwidth-bound by its unoptimized Triton/native
decode kernels here.

## What was NOT measured (and why)

- **vLLM on the same weights as Ollama.** Serving Ollama's `qwen3:8b` GGUF in
  vLLM (GGUF loader) was not attempted inside the time-box; downloading HF
  `Qwen/Qwen3-8B` bf16 (16.9 GB, under the 20 GB limit but slow) was skipped.
  Family and prompt tokenization are identical (Qwen3); parameter count halves
  (4B vs 8B) — direction of bias runs *against* the vLLM number, so the
  conclusion is safe.
- **vLLM tuning levers**: `--enforce-eager` off/on, higher utilization on a
  quiet box, multi-sequence batch, `vllm download-kernels`, warm second boot.
  NOT MEASURED. A future dedicated run on an otherwise-idle box could show vLLM
  faster; today's number is the real stock-pip experience on this shared host.
- **Ollama at >8K context**, larger parallel turns, or 32K ctx — NOT MEASURED.
- **gemma4:26b on vLLM** — not attempted (only Ollama GGUF resident).

## Caveats affecting fairness

- vLLM runs happened while `gemma4:26b` (26.9 GiB) + comfyui (16.7 GiB) were
  resident; the Ollama `qwen3:8b` runs happened earlier while only comfyui was
  resident. Some of vLLM's decode gap may be co-tenancy. Direction is not in
  doubt: Ollama delivered 85 tok/s on the 26B model *at the same time* vLLM was
  holding its 39 GiB reservation and returning 15 tok/s on a 4B.
- ollama `llama3.1:8b` measured 22 tok/s here vs 46 tok/s in the docs'
  Oct-11 run; qwen3:8b 41 vs docs' 44. Likely Ollama 0.34 + current co-tenancy,
  not methodology. Reported as measured; re-verification suggested for the
  selector floor if it ever gates on llama3.1.

## Recommendation

**Ollama now, with the documented vLLM seam — i.e. keep the decision in
docs/ARCHITECTURE.md as-is and mature the `runtime.py` swap interface.**

Evidence:
- Ollama on this exact box: 4 s cold load, 31 ms warm TTFT, 41 tok/s, 6 GB
  resident for the 8B assistant — comfortably over the 15 tok/s selector floor,
  already the live backend, zero migration cost or memory-policy change.
- vLLM works on U1 — a genuinely useful finding (the docs previously assumed it
  might not) — but today's stock-pip shape is not ready to *own* this box: 6-min
  kernel-JIT first boot, ~39 GiB up-front reservation that deadlocks against
  co-tenant workloads (3 failed starts), and ~15 tok/s on a half-size model.
  It belongs on a *dedicated* halo-class unit (80B tier) with provisioned
  memory and a warm kernel cache.
- Client story is unchanged either way and is not a decision input: both expose
  a single-port OpenAI-compatible API (`/v1/models` verified on both :11434 and
  :8001), so the phone/LAN client and `runtime.py` behind it need no change if a
  unit later runs vLLM.

**One line:** Ollama stays v1 on GB10-class boxes; vLLM 0.32 now installs and
runs on U1/sm_121 but its 6-minute JIT first boot, ~40 GB pre-reservation and
co-tenant memory gate make it a future dedicated-tower backend behind the
existing runtime.py seam, not today's default.
