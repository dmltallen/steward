# Steward Certified Hardware Matrix

**Version:** 1.0, 2026-10-10 (initial). Prices are Oct 10 2026 street values
(DRAM supercycle inflating most; ranges are MSRP to observed street). The
selector auto-serves the "model tier" column; nothing here is a user choice.

Principle: memory decides the model, CPU barely matters. The certified list
exists to bound support: these are the known-good examples for each class.

## Class A: Always-on mini PCs / AI PCs (NPU and iGPU offload)

| Hardware | Spec | Street price | Auto-served tier |
|---|---|---|---|
| Budget mini PC (Beelink/Geekom tier) | 32 GB DDR5, Ryzen 7 / iGPU | $300-500 | 7-9B Q4 (15-25 tok/s) |
| Ryzen AI mini PC (ACEMAGIC F5A, Geekom A9 Max) | Ryzen AI 9 HX 370, 50 TOPS NPU | $500-1,600 | 7-13B Q4 |
| Always-on server mini PC (Beelink GTR9 / SER) | 32-64 GB DDR5 | $500-800 | 13-26B |
| Mac mini M4 base | 16-32 GB unified, 120 GB/s | $599 | 7-13B Q4 |

32 GB RAM is the practical floor for serious local AI in 2026. These run under
15 W idle and cost $15-25/yr to leave on 24/7.

## Class B: Consumer discrete GPUs (towers)

| GPU | VRAM | Bandwidth | Street price | Auto-served tier |
|---|---|---|---|---|
| RTX 5060 Ti | 16 GB | 448 GB/s | $429-585 | 7-13B (~60 tok/s) |
| RTX 5070 | 12 GB | 448 GB/s | ~$880 | 14B (~90 tok/s) |
| RTX 5070 Ti | 16 GB | 896 GB/s | $749-979 | 14-27B (~80-110 tok/s) |
| RTX 5080 | 16 GB | 960 GB/s | $999-1,100 | 27B (~45-130 tok/s) |
| Used RTX 3090 | 24 GB | 936 GB/s | ~$600 | 27-31B (Ghost tier, 40-50 tok/s) |
| RTX 5090 | 32 GB | 1,792 GB/s | $1,999-4,300 | 32B full (~45 tok/s) |
| AMD RX 9070 XT | 16 GB | ~644 GB/s | ~$600-700 | 14-27B |

24 GB is exactly Ghost Core's tier. A $600 used 3090 matches Core's model
ceiling at a seventh of the price.

## Class C: Unified-memory workstations (power tier)

| Hardware | Pool | Street price | Auto-served tier |
|---|---|---|---|
| Mac mini M4 Pro (48 GB) | 48 GB, 273 GB/s | ~$1,999 | 70B Q4 |
| Used Mac Studio M1 Max (64 GB) | 64 GB, 400 GB/s | $1,100-1,400 | 70B Q4 (cheapest legit 70B) |
| Strix Halo mini PC (Minisforum MS-A2, Beelink GTR9 Pro) | 96-128 GB | ~$1,600-2,200 | 70B + 80B MoE |
| GMKtec EVO-X2 (AI Max+ 395) | 128 GB | $2,199-3,649 | 70B + 80B MoE |
| ASUS Ascent GX10 / DGX Spark GB10 | 128 GB, 1,000 TOPS | $3,000-6,700 | 70B + 80B MoE |
| Mac Studio M5 Max (128 GB) | 128 GB | $2,499+ | 70B + 80B MoE |
| Gorgon Halo (Q3 2026) | 192 GB, 160 GB AI-addressable | ~$5,000-8,000 | 109B MoE, multi-agent |

## Class D: Popular consumer CPUs (config guidance)

Ryzen 5/7/9 AM5, Ryzen AI 9 HX 370, Ryzen AI Max+ 395 (50 TOPS NPU), Intel
Core Ultra 5/7/9 (Lunar Lake/Arrow Lake), entry Ryzen 5 / Core i5 for Class A
boxes. The CPU moves speed a little; memory decides the model.

## Detection coverage

`detect.py` recognizes: discrete NVIDIA (by nvidia-smi, with a bandwidth
lookup table), NVIDIA GB10 (UMA, unified pool), Apple Silicon (sysctl, chip
bandwidth table), iGPU/NPU boxes (CPU-name match for NPU TOPS), and falls back
to system-RAM offload when nothing else resolves. Verified live on U1
(GB10, 121.6 GB unified).
