# Steward

The personal AI you own, served on hardware you already have.

Steward turns a certified set of hardware (specific workstations, the popular
consumer GPUs, and mainstream CPUs) into an always-on personal AI assistant:
it detects the machine, automatically selects and serves the right open models,
and is reachable from your phone and computer. No complicated setup. Free to
install and use, perpetual license to keep using, paid upgrades only for major
new feature packs.

## Why it exists

Ghost Core sells a $3,499 one-use computer to deliver a software experience
(local models, persistent memory, credential isolation, phone-first agent) that
open-weight software already provides on hardware people own for a tiny
fraction of that price. Steward is the anti-Ghost: same ownership story, zero
hardware markup, better economics, and no DRAM-supercycle exposure. A $600 used
24 GB GPU or a $3,000 GB10 twin that buyers already own becomes the brains;
Steward supplies the intelligence layer and the dead-simple setup.

## Current state (v0.1 prototype)

Working and tested:

- `steward doctor` detects the serving hardware: GPU (discrete vs unified),
  VRAM or unified pool size, CPU, approximate memory bandwidth. Verified live
  on U1 (NVIDIA GB10, 121.6 GB unified).
- `steward plan` auto-selects the model bundle (always-hot assistant +
  capability model + optional vision) from an open-model catalog keyed to
  the detected memory class. 7 selector unit tests green, and the selection
  matrix matches the design: 24 GB lands on the 27 B class (Ghost's tier),
  128 GB lands on the 80 B MoE, 192 GB lands on the 109 B MoE.

Still to build: the installer, the serving daemon, phone and web clients,
zero-config pairing, the license gate, and OTA model refresh.

## Quick start (prototype)

```bash
git clone https://github.com/dmltallen/steward.git
cd steward
PYTHONPATH=src python3 -m steward.cli doctor   # what hardware will serve me?
PYTHONPATH=src python3 -m steward.cli plan     # which models will Steward pick?
PYTHONPATH=src python3 -m pytest tests/        # run the selector tests
```

## Repo layout

```
docs/
  AGENT-BRIEF.md       standing brief for agent and cron work on this repo
  PRD.md               product requirements
  ARCHITECTURE.md      component design
  HARDWARE-MATRIX.md   the certified hardware list and pricing
  MODEL-SELECTOR.md    auto model-selection ladder and rules
  PRICING.md           licensing model and acceptance pricing
  ROADMAP.md           milestones
src/steward/
  detect.py            hardware detection
  models.py            open-model catalog
  selector.py          automatic bundle selection (the no-setup core)
  cli.py               doctor / plan commands
tests/                 selector and detection tests
```

## Docs and project record

- Vault: `Research/Personal AI Assistant App Spec.md` (spec),
  `Research/Ghost Core Competitive Undercut Analysis.md` (positioning),
  `Topics/Steward.md` (live topic state)
- NAS durable docs and artifacts: `/mnt/nas/Dad/Special Projects/Steward/`
- Source of truth: `don/steward` on NAS gitea (port 222 SSH / 3200 HTTP)

## License

MIT (open core; the product business model is separate from the repo license).
