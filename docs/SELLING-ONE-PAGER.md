# Steward Sales One-Pager

**Version:** 1.0, 2026-10-12 (B2 sprint). One page for outreach use.
Every number anchors to docs/HARDWARE-MATRIX.md and docs/PRICING.md.

## The pitch

Steward turns the computer you already own into an always-on personal AI. It
detects your machine, picks the right open models for its memory, and serves
them to your phone and desktop. No $3,499 one-use box to buy, no monthly
subscription, no hardware markup. Free to install, one-time perpetual license,
open models, MIT source.

Ghost Core sells a 24 GB GPU box for $3,499 to deliver a software experience
(local models, persistent memory, phone-first agent) that open-weight software
already provides on hardware people own. Steward is the anti-Ghost: the same
ownership story, zero hardware markup, and honest about what memory each model
really needs.

## The numbers

Your hardware does the heavy lifting; the license is a small fraction of it.

| Your hardware | Hardware spend | Steward license | License as % of spend |
|---|---|---|---|
| 16 GB tower (5070 Ti / 5080) | $900-1,500 | Personal $79 | 5-9% |
| 24 GB used 3090 (Ghost class) | ~$600 | Personal $79 | ~13% |
| 32 GB 5090 rig | $2,000-4,300 | Home $149 | 3-7% |
| 48-64 GB Strix Halo / used Studio | $1,200-2,200 | Home $149 | 6-12% |
| 96-128 GB GB10 / GX10 / Mac Studio | $2,500-6,700 | Home $149 | 2-6% |
| 192 GB halo tier | $5,000-8,000 | Power $249 | 3-5% |

Head to head with Ghost:

- Ghost Core: $3,499 one-time, 24 GB RTX Pro 4000 at 432 GB/s, plus OTA service
  tied to Ghost, and batch 2 already flagged to cost more because the GPU price
  doubled on them (founder's own X post, Oct 5).
- Steward on a used 3090: ~$600 hardware you own, same 24 GB tier at 936 GB/s,
  plus $79 once. About a sixth of the money for the same model ceiling and more
  bandwidth.
- One-time $79 outlives two months of a $200/month cloud Pro subscription. At
  $149 it is under 6% of a $3,000 box and it never renews.

## The capability ladder

| Tier | One-time | What it serves |
|---|---|---|
| Free | $0 | Install, base assistant, one device, basic memory, base OTA |
| Personal | $79 | Up to 24 GB: the Ghost-class ceiling (27-31B) |
| Home | $149 | Up to 128 GB: 70B and 80B MoE plus vision, multi-device |
| Power | $249 | Up to 192 GB: 109B MoE, multi-agent, advanced sandbox |

Model ceilings are honest: memory decides the model. An 80B MoE needs about
48 GB and is served on a machine that has it, never over-claimed on a 24 GB box.

## Objection handling

- **"Setup is a pain; I just want it to work."** That is the product. `steward
  wizard` detects the machine, auto-selects the bundle, and prints the two setup
  commands. No model research, no trial and error. Ghost's answer to the same
  want is an extra $3,499 box and a shipping delay; Steward does it on what is
  already in the room.
- **"A small company product has no support."** MIT open core, real docs, and
  the communities the product is posted in (r/LocalLLaMA, r/selfhosted) answer
  fast, in public, and stay true. Support is not a locked chat with a vendor.
  Perpetual means bought features keep working with no forced upsell; the
  optional $59/yr upgrade plan is for priority access and is never a gate.
- **"Which model do I pick?"** You do not pick. Steward keys the bundle to the
  detected memory class, so the hard choice is made for you, and OTA keeps the
  generation current. Want to override? The catalog is open.
- **"Why not just run llama.cpp myself?"** The hard part is not running one
  model, it is effortless bundle selection, persistent memory, and phone access.
  Steward is that glue. Free tier to try it before paying a cent.
- **"Too cheap to be real."** Anchor to spend, not price. $79 is a few percent
  of the hardware already doing the work. The box is the investment; Steward is
  the intelligence layer on top.

## Rules that keep it credible

One-time money for features, never a subscription. No invented model identities
or benchmark numbers; anything not measured is marked [uncertain] in the docs.
Never attack buyers of alternatives, attack the pricing structure.
