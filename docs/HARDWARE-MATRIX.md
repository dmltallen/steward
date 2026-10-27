# What Steward runs on: computed verdicts and tested examples

**Version:** 2.0, 2026-10-26. Replaces the older "Certified Hardware Matrix"
(1.0). Prices below are Oct 10 2026 street values (the DRAM supercycle is
inflating most; ranges are MSRP to observed street) and exist only as buying
reference. They are examples, not a support list to be on.

Principle: memory decides the model, CPU barely matters, and the software
decides at install time what a given machine can run. Steward probes the
machine (`src/steward/detect.py`), computes the usable serving memory, and
returns a verdict (`src/steward/compat.py`) of no-fit, lite, or full. There is
no hardware to be certified for; a machine is whatever `steward check` says
it is on that day, and the verdict gets recomputed every install and on every
`steward check` run.

## How the verdict is computed

`steward check` measures three things:

- Serving memory: video memory (VRAM) on discrete cards, the whole unified
  pool on Apple, NVIDIA GB10 and Strix Halo machines, and system RAM minus
  2 GB (reserved for the operating system) on machines with no discrete GPU.
- Free disk at the data directory.
- Which detection path fired, which is recorded as a confidence value
  (`nvml`, `apple_sysctl`, `/proc/meminfo`, or `fallback`).

The verdict thresholds:

| Usable serving memory | Verdict | What you get |
|---|---|---|
| under 16 GB | no-fit | Steward cannot run a model with room for context and the OS. |
| 16 to under 40 GB | lite | One model, no image understanding, reduced context. |
| 40 GB and up | full | Image understanding, longer context, a split always-hot assistant on big pools. |

The 109B class is reserved for halo-class pools (140 GB and up in the class
ladder). Everything a machine gets is computed from these numbers, not from a
vendor list.

## Measured versus projected

Every row below is a projection unless its Status says measured. A projected
row means the verdict comes from applying the probe rules to the listed spec,
and no one has yet run `steward check` on that exact box to confirm it. The
one machine Steward has been measured on so far is the U1 development box
(NVIDIA GB10, 121.6 GB usable unified pool), marked measured in Class C. A row
becomes measured the moment someone runs `steward check` on that hardware and
the output matches. Until then, treat every non-U1 tier as "should run this,
confirm before you promise anyone".

## Class A: always-on mini PCs and AI PCs (iGPU and CPU offload)

| Hardware | Spec | Street price | Prospected verdict and tier | Status |
|---|---|---|---|---|
| Budget mini PC (Beelink/Geekom tier) | 32 GB DDR5, Ryzen 7 / iGPU | $300-500 | lite, high-32, one 32B model Q4 | projected |
| Ryzen AI mini PC (ACEMAGIC F5A, Geekom A9 Max) | Ryzen AI 9 HX 370, 50 TOPS NPU | $500-1,600 | lite, high-32, one 32B model Q4 | projected |
| Always-on server mini PC (Beelink GTR9 / SER) | 32-64 GB DDR5 | $500-800 | 32 GB: lite 32B; 64 GB: full 70B no vision | projected |
| Mac mini M4 base | 16-32 GB unified, 120 GB/s | $599 | 16 GB: lite 14B; 32 GB: lite 32B | projected |

32 GB RAM is the practical floor for serious local AI in 2026. These run under
15 W idle and cost $15-25/yr to leave on 24/7. Note that a 16 GB base model
sits right at the bottom of the lite band; it works, in light mode.

## Class B: consumer discrete GPUs (towers)

| GPU | VRAM | Bandwidth | Street price | Prospected verdict and tier | Status |
|---|---|---|---|---|---|
| RTX 5060 Ti | 16 GB | 448 GB/s | $429-585 | lite, mainstream-16, 14B | projected |
| RTX 5070 | 12 GB | 448 GB/s | ~$880 | no-fit (12 GB is under the 16 GB floor) | projected |
| RTX 5070 Ti | 16 GB | 896 GB/s | $749-979 | lite, mainstream-16, 14B | projected |
| RTX 5080 | 16 GB | 960 GB/s | $999-1,100 | lite, mainstream-16, 14B | projected |
| Used RTX 3090 | 24 GB | 936 GB/s | ~$600 | lite, mid-24, 27B (Ghost tier) | projected |
| RTX 5090 | 32 GB | 1,792 GB/s | $1,999-4,300 | lite, high-32, 32B | projected |
| AMD RX 9070 XT | 16 GB | ~644 GB/s | ~$600-700 | detected as CPU box; verdict from system RAM | projected |

24 GB is exactly Ghost Core's tier. A $600 used 3090 matches Core's model
ceiling at a seventh of the price. Two corrections versus the old list: a
12 GB card is no-fit under today's rules, and a 32 GB card serves the full
32B class but stays in lite mode (no image understanding until 40 GB).

AMD and Intel discrete cards are not yet detected as discrete: `detect.py`
recognizes NVIDIA through nvidia-smi and everything else lands in the CPU
path, so the verdict for an AMD or Intel GPU comes from system RAM. The AMD
row above is computed as a CPU machine with 32 GB RAM, not from the card's
16 GB VRAM. This is a deliberate, honest gap, not a claim the card is ignored
in the future.

## Class C: unified-memory workstations (power tier)

| Hardware | Pool | Street price | Prospected verdict and tier | Status |
|---|---|---|---|---|
| Mac mini M4 Pro (48 GB) | 48 GB, 273 GB/s | ~$1,999 | full, power, 32B plus assistant and vision | projected |
| Used Mac Studio M1 Max (64 GB) | 64 GB, 400 GB/s | $1,100-1,400 | full, super, Llama 3.3 70B (cheapest legit 70B) | projected |
| Strix Halo mini PC (Minisforum MS-A2, Beelink GTR9 Pro) | 96-128 GB | ~$1,600-2,200 | full, extreme, 80B MoE | projected |
| GMKtec EVO-X2 (AI Max+ 395) | 128 GB | $2,199-3,649 | full, extreme, 80B MoE | projected |
| ASUS Ascent GX10 / DGX Spark GB10 | 128 GB | $3,000-6,700 | full, extreme, 80B MoE | measured on U1 |
| Mac Studio M5 Max (128 GB) | 128 GB | $2,499+ | full, extreme, 80B MoE | projected |
| Gorgon Halo (Q3 2026) | 192 GB, 160 GB AI-addressable | ~$5,000-8,000 | full, halo, 109B MoE, multi-agent | projected |

One correction versus the old list: the 48 GB Mac mini M4 Pro serves the 32B
class, not 70B. A 70B Q4 needs 40 GB of weights, and residency caps usable
weight memory at 0.80 of the pool (38.4 GB here), so 70B does not fit with
headroom. That is the same undercut Ghost is known for on its 24 GB card and
Steward will not repeat it at any capacity.

## Class D: popular consumer CPUs (config guidance)

Ryzen 5/7/9 AM5, Ryzen AI 9 HX 370, Ryzen AI Max+ 395 (50 TOPS NPU), Intel
Core Ultra 5/7/9 (Lunar Lake/Arrow Lake), entry Ryzen 5 / Core i5 for Class A
boxes. The CPU moves speed a little; memory decides the tier.

## Detection coverage

`detect.py` recognizes: discrete NVIDIA (by nvidia-smi, with a bandwidth
lookup table), NVIDIA GB10 (UMA, unified pool), Apple Silicon (sysctl, chip
bandwidth table), a CPU-only profile when no GPU path exists (system RAM
minus 2 GB for serving), and falls back to a conservative CPU-only profile on
unsupported systems. Every profile carries a `confidence` value so a human
can see how the machine was read. Verified live on U1 (GB10, 121.6 GB
unified). Run `steward check` on any other box before treating a row as fact.
