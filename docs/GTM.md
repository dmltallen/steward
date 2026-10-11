# Steward Go-to-Market Outline (B1)

**Version:** 1.0, 2026-10-11

## One-line position

The personal AI you own, served on hardware you already have. No 3,499 box to
buy, no monthly subscription, one-time perpetual license, open models.

## Launch positioning (the anti-Ghost message)

The product is best understood against what it is not, so lead with that
contrast, then land the ownership line.

Draft core message (short form, for posts and replies):

> Steward is the anti-Ghost. Ghost Core sells a 3,499 one-use computer to run
> local models, persistent memory, and phone-first agents. Steward does the
> same software job on the GPU or mini PC you already own. It detects your
> machine, auto-selects the right open models, and serves them to your phone.
> Free to install, one-time perpetual license, no subscription, no hardware
> markup. Your 24 GB card already matches Ghost's model ceiling. A feature is
> exactly one-time money, not a monthly bill.

Supporting claims, each backed by a doc:

- 24 GB is exactly Ghost Core's tier; a ~600 used 3090 matches Core's model
  ceiling at a seventh of the price (HARDWARE-MATRIX.md).
- One-time 79-149 outlives two months of a 200/month Pro subscription
  (PRICING.md).
- No DRAM-supercycle exposure: buyers do not buy memory from us.
- Residency honesty: an 80B MoE needs about 48 GB; we serve it on a machine
  that has it, never over-claiming on a 24 GB box.

House rules for every post: no em dashes, no hype adjectives, lead with the
economics, never attack buyers of alternatives (attack the pricing structure,
not the people), link the docs not a sales page.

## Channels and roles

Ranked by fit for Class B and C buyers. Each channel gets its own post,
never a paste of the same text.

| Channel | Buyers | Role | Primary variant |
|---|---|---|---|
| r/LocalLLaMA | Both; the core local-LLM audience | Launch thread + live answers; the technical early adopters must validate first | Both, split by hardware mention |
| r/hardware | Class B towers (3090/5080 owners) | Value framing: a used 3090 is the Ghost tier at 1/7 the price | v=a ($79) |
| Hacker News (Show HN) | Technical generalists, self-hosting crowd | Show HN on the tool: detect + plan + wizard, MIT, no subscription | Both |
| r/selfhosted, r/homelab | Always-on server owners | 24/7 assistant on a mini PC under 15 W; cost-per-year framing | v=a ($79) |
| GB10 / DGX Spark owners (r/DGXSpark, NVIDIA GB10 threads) | Class C; already bought the 128 GB box | "You own the best local-AI hardware on the market; here is the missing intelligence layer" | v=b ($149) |
| Strix Halo owners (r/Minisforum, r/Beelink, AI Max+ 395 threads) | Class C | Same ownership story for the 96-128 GB mini | v=b ($149) |
| Mac local-LLM guides (Mac mini/Studio owners, r/macmini, r/LocalLLaMA Mac threads) | Class C | Cheap legit 70B on a used Studio; mobile-adjacent setup | v=b ($149) |
| ServeTheHome forums | Homelab + mini-PC reviewers | Written overview + the power/cost story | v=a ($79) |

Each channel link carries the variant query parameter and a source tag
(`?v=a&utm_source=reddit-r-hardware`) so the A/B remains clean and analyzable.

## First 30 days of outreach

Week 0 (days 1-3): pre-verify before posting. Public build must run the whole
detect-to-serve loop on at least one Class B and one Class C machine, and the
landing page must render both variants locally. Record a 30-second screencast
if possible. Nothing public until the install path works; broken first
impressions are permanently expensive in these communities.

Week 1 (days 4-10): soft launch. Post the Show HN and the r/LocalLLaMA thread
with the anti-Ghost framing and the doc links. Answer every comment the same
day, invite the hostile ones in (their objections become fixes). Stand up the
email list so capture has a real backend before any traffic lands.

Week 2 (days 11-17): Class B push. Post to r/hardware and the GPU-owner
threads pointing to v=a (79). Share two value cases: used 3090 at ~600, and a
mini-PC assistant under 15 W idle. Collect objections; log every pricing
response (buyers who say "too cheap is suspicious" are real signal).

Week 3 (days 18-24): Class C push. Post to the GB10, Strix Halo, and Mac
local-LLM communities pointing to v=b (149). Emphasize residency honesty and
the 128 GB tier. Engage with the Mac Studio build posts; undercut nobody's
guide, complement them instead.

Week 4 (days 25-30): consolidation. Read the A/B numbers at the 30-day mark
(min 300 visitors per variant target), post a follow-up transparency thread
with what shipped and what the test found, and hand the analysis to B2 for the
sales one-pager. Record the results and sample sizes in PRICING.md.

## Outreach message templates

Short template (350 chars, forum post lead):

> Steward turns hardware you already own into an always-on personal AI: it
> detects your machine, picks the right open models, and serves them to your
> phone. Free to install, one-time license, no subscription, no hardware
> markup. Your 24 GB card is already Ghost-tier. Details: [link to v variant].

Long template (as the Show HN body): the README narrative, the anti-Ghost
contrast, the docs links, and an honest "still to build" list. Never omit the
honest list; these communities check.

## Anti-Ghost objection handling

- "Why not just run llama.cpp myself?" The difficulty is not running a model,
  it is the no-setup bundle selection, persistent memory, and phone access.
  Steward is that glue; MIT open core, free tier to try.
- "A perpetual license means no updates." No: bought features keep working
  forever, and base OTA model updates are free at the paid tiers. The upgrade
  plan is optional, for priority access, never a gate.
- "Too cheap to be real." Anchor to hardware spend: 79 is 6-8% of the machine
  doing the work; the box you own is the real investment, we just add the
  intelligence layer.
