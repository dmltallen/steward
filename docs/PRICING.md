# Steward Pricing and Licensing

**Version:** 1.0, 2026-10-10 (initial)

## Model (Don's direction, refined)

- Free to install and use initially; the assistant works out of the box.
- Perpetual license to use forever: one-time, keeps working offline, no server
  required after purchase.
- Up-charges revisited on major feature updates only: optional paid feature
  packs, never a forced subscription, never a gate on features already bought.

## Shelf

| Tier | One-time license | Unlocks |
|---|---|---|
| Free | $0 | Install, base assistant, one device, phone + web, basic memory, base OTA |
| Personal | $79 | Auto-selected capability tier up to 24 GB (Ghost class), full memory, proactive agent, all OTA at this level |
| Home | $149 | Capability tier up to 128 GB (70B/80B MoE + vision), multi-device |
| Power | $249 | 192 GB halo tier (109B MoE, multi-agent), advanced sandbox |

Optional upgrade plan: $59/yr for priority features and early model access.
Never required; bought features keep working without it.

Major feature packs (optional, $29-49 each, revisited per release):
smart-home connectors, browser agent, credential firewall pro, multi-box mesh,
multi-user. Each is additive; buyers keep everything they own.

## Acceptance analysis (anchored to hardware spend)

| Buyer's hardware | Hardware spend | Modeled WTP band (5-15%) | Recommended license |
|---|---|---|---|
| 16 GB tower (5070 Ti / 5080) | $900-1,500 | $50-225 | Personal $79 |
| 24 GB (used 3090) or Class A mini PC | $500-1,000 | $25-150 | Personal $79 |
| 32 GB (5090 rig) | $2,000-4,300 | $100-650 | Home $149 |
| 48-64 GB (Strix Halo, mini Pro, used Studio) | $1,200-2,200 | $60-330 | Home $149 |
| 96-128 GB (GX10, Mac Studio, DGX Spark) | $2,500-6,700 | $125-1,000 | Home to Power $149-249 |
| 192 GB (Gorgon Halo) | $5,000-8,000 | $250-1,200 | Power $249 |

At $79 a 16 GB tower owner pays about 6-8% of their hardware spend; at $149 a
128 GB owner pays about 3-6%. Competitive anchors: Vellum runs to $200/yr,
Perplexity Computer and Claude/ChatGPT Pro charge $200/month. A one-time $149
that outlives two months of a Pro subscription and needs no new hardware is an
easy yes for someone already in at $3,000+.

## Acceptance validation (open item)

The $79/$149/$249 shelf is a hypothesis (marked [uncertain] until tested). The
validation rule: A/B the license price against real buyers of Classes B and C
before locking; target at least 5% free-to-paid conversion within 60 days, per
PRD success metrics.
