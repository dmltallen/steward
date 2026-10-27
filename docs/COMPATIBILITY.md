# Steward Compatibility

What hardware Steward runs on is computed from the machine itself, not
from a vendor list. At install time and on every `steward check` run, the
software probes the machine (`src/steward/detect.py`), computes the usable
serving memory, and returns a verdict. The verdict is the source of truth
for whether Steward fits that machine. There is no hardware to be
certified; a machine is whatever `steward check` says it is on that day.

This document is generated from the verdict model in
`src/steward/compat.py` (the single source of the states and thresholds)
and the tested-examples matrix, and is kept in step with both.

## States and thresholds

The verdict has three states, decided by the usable serving memory. The
two thresholds are 16 GB (below which nothing runs) and 40 GB (at or above
which the full experience is available). Both live in compat.py as
`MIN_LITE_GB = 16.0` and `MIN_FULL_GB = 40.0`.

| Usable serving memory | Verdict | What you get |
| --- | --- | --- |
| under 16 GB | no-fit | Steward cannot run a model with room for context and the operating system. |
| 16 to under 40 GB | lite | One model at a time, no image understanding, a reduced context window. |
| 40 GB and up | full | Image understanding, a longer context window, and on big pools a small always-on assistant next to the main model. |

The 109B model class is reserved for halo-class pools (140 GB and up in
the selector's memory ladder). Below that, the largest capability model is
bounded so it fits the pool with headroom; an 80B class model is the usual
ceiling there.

## How the verdict is computed

`steward check` measures three things:

- **Serving memory.** Video memory (VRAM) on discrete cards, the whole
  unified pool on Apple, NVIDIA GB10 and Strix Halo machines, and system
  RAM minus 2 GB (reserved for the operating system) on machines with no
  discrete GPU. This is the number the verdict is computed from.
- **Free disk** at the data directory. Disk does not flip the state: it is
  advisory. Below 10 GB free the reasons list a warning to free space
  before installing models. Disk footprint is around 5 to 25 GB depending
  on the model tier, plus the index and databases.
- **Which detection path fired**, recorded as a confidence value (`nvml`,
  `apple_sysctl`, `/proc/meminfo`, or `fallback`) so a human can see how
  the machine was read.

The verdict output always includes, in plain English, the reason list and
the minimum-machine sentence for that state.

## Minimum machine sentences

Verbatim from compat.py, one per state. These are what `steward check`
prints.

- **no-fit:** "This machine has only <N> GB of usable memory (<memory
  description>). Steward needs at least 16 GB usable to run a model with
  room for context and the operating system." with the minimum sentence:
  "Minimum machine that works: 16 GB of usable memory. For example a
  discrete graphics card with 16 GB of video memory, or a machine with 18
  GB or more of system RAM and no graphics card."
- **lite:** "Steward runs in light mode here: one model at a time, no
  image understanding, and a reduced context window. The machine has <N>
  GB usable (<memory description>)." with: "Minimum machine that works: 16
  GB of usable memory, which this machine clears with <N> GB. Reaching 40
  GB usable unlocks the full experience with image understanding."
- **full:** "Full mode: image understanding enabled, longer context, and
  room for a small always-on assistant next to the main model. The machine
  has <N> GB usable (<memory description>)." with: "Minimum machine for
  full mode: 40 GB of usable memory, which this machine clears with <N> GB."

In short: a 16 GB discrete card or an 18 GB system-RAM machine without a
card is the floor, and 40 GB is the door to the full experience. As a
buying guide, 32 GB of system RAM is the practical floor for serious local
AI in 2026.

## Tested examples

Every row below is a projection unless the Measured/Projected column says
measured. A projected row means the verdict comes from applying the probe
rules to the listed specification, and no one has yet run `steward check`
on that exact box to confirm it. The one machine measured so far is the
U1 development box (NVIDIA GB10, 121.6 GB usable unified pool), where the
live verdict was full, class extreme, serving 121.6 GB. A row becomes
measured the moment someone runs `steward check` on that hardware and the
output matches the row. Until then, treat every non-U1 row as should run
this, confirm before you promise anyone.

### Class A: always-on mini PCs and AI PCs

| Hardware | Spec | Street price | Expected verdict and tier | Measured/Projected |
| --- | --- | --- | --- | --- |
| Budget mini PC (Beelink/Geekom tier) | 32 GB DDR5, Ryzen 7 / iGPU | $300-500 | lite, high-32, one 32B model Q4 | projected |
| Ryzen AI mini PC (ACEMAGIC F5A, Geekom A9 Max) | Ryzen AI 9 HX 370, 50 TOPS NPU | $500-1,600 | lite, high-32, one 32B model Q4 | projected |
| Always-on server mini PC (Beelink GTR9 / SER) | 32-64 GB DDR5 | $500-800 | 32 GB: lite 32B; 64 GB: full 70B no vision | projected |
| Mac mini M4 base | 16-32 GB unified, 120 GB/s | $599 | 16 GB: lite 14B; 32 GB: lite 32B | projected |

These run under 15 W idle and cost about $15-25 a year to leave on 24/7. A
16 GB base model sits right at the bottom of the lite band; it works, in
light mode.

### Class B: consumer discrete GPUs

| GPU | VRAM | Street price | Expected verdict and tier | Measured/Projected |
| --- | --- | --- | --- | --- |
| RTX 5060 Ti | 16 GB | $429-585 | lite, mainstream-16, 14B | projected |
| RTX 5070 | 12 GB | ~$880 | no-fit (12 GB is under the 16 GB floor) | projected |
| RTX 5070 Ti | 16 GB | $749-979 | lite, mainstream-16, 14B | projected |
| RTX 5080 | 16 GB | $999-1,100 | lite, mainstream-16, 14B | projected |
| Used RTX 3090 | 24 GB | ~$600 | lite, mid-24, 27B (the Ghost-class tier) | projected |
| RTX 5090 | 32 GB | $1,999-4,300 | lite, high-32, 32B | projected |
| AMD RX 9070 XT | 16 GB | ~$600-700 | detected as a CPU box; verdict from system RAM | projected |

24 GB is exactly the Ghost Core tier, so a $600 used 3090 matches that
product's model ceiling at roughly a seventh of the price. Two corrections
that matter: a 12 GB card is no-fit under today's rules, and a 32 GB card
serves the full 32B class but stays in lite mode because it is below the
40 GB full threshold.

AMD and Intel discrete cards are not yet detected as discrete. `detect.py`
recognizes NVIDIA through nvidia-smi, and everything else lands in the CPU
path, so the verdict for an AMD or Intel GPU comes from system RAM. The AMD
row above is computed as a CPU machine with 32 GB RAM, not from the card's
16 GB VRAM. This is a deliberate, honest gap, not a claim that cards are
ignored forever.

### Class C: unified-memory workstations

| Hardware | Pool | Street price | Expected verdict and tier | Measured/Projected |
| --- | --- | --- | --- | --- |
| Mac mini M4 Pro (48 GB) | 48 GB, 273 GB/s | ~$1,999 | full, power, 32B plus assistant and vision | projected |
| Used Mac Studio M1 Max (64 GB) | 64 GB, 400 GB/s | $1,100-1,400 | full, super, Llama 3.3 70B | projected |
| Strix Halo mini PC (Minisforum MS-A2, Beelink GTR9 Pro) | 96-128 GB | ~$1,600-2,200 | full, extreme, 80B MoE | projected |
| GMKtec EVO-X2 (AI Max+ 395) | 128 GB | $2,199-3,649 | full, extreme, 80B MoE | projected |
| ASUS Ascent GX10 / DGX Spark GB10 | 128 GB | $3,000-6,700 | full, extreme, 80B MoE | measured on U1 |
| Mac Studio M5 Max (128 GB) | 128 GB | $2,499+ | full, extreme, 80B MoE | projected |
| Gorgon Halo (Q3 2026) | 192 GB, 160 GB AI-addressable | ~$5,000-8,000 | full, halo, 109B MoE, multi-agent | projected |

One correction versus older lists: a 48 GB Mac mini M4 Pro serves the 32B
class, not 70B. A 70B Q4 needs roughly 40 GB of weights, and residency caps
usable weight memory at 0.80 of the pool (38.4 GB here), so 70B does not
fit with headroom. Steward does not repeat that over-claim at any capacity.

### Class D: popular consumer CPUs

Ryzen 5/7/9 AM5, Ryzen AI 9 HX 370, Ryzen AI Max+ 395 (50 TOPS NPU), Intel
Core Ultra 5/7/9, and the entry Ryzen 5 / Core i5 that power Class A boxes.
The CPU moves speed a little; memory decides the tier.

## Detection coverage

`detect.py` recognizes: discrete NVIDIA by nvidia-smi with a bandwidth
lookup table, NVIDIA GB10 unified pools, Apple Silicon through sysctl with
a per-chip bandwidth table, a CPU-only profile using system RAM minus 2 GB
when no GPU path exists, and a conservative CPU-only fallback on unsupported
systems. Every profile carries a confidence value. Verified live on U1
(GB10, 121.6 GB unified). Run `steward check` on any box before treating a
row as fact.
