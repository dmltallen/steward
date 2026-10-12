# Steward Go-to-Market Outline (B1)

**Version:** 2.0, 2026-10-12 (B2 launch prep: finalized A/B variants, per-channel
outreach message drafts, launch checklist). Base outline from B1 (Oct 11).

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

## B2: Finalized A/B variants (locked 2026-10-12)

Two landing variants that differ in exactly one element, the price, per
docs/PRICING.md decision rule. Both carried in `site/index.html?v=a` and
`site/?v=b`. Build completeness: the demo tab switcher is now hidden unless
`?ab=show` is present (app.js), so real traffic cannot see the other variant.

| Variant | Price | Copy | Served to | Channel tags |
|---|---|---|---|---|
| A | Personal perpetual license $79 one time | "One price. Then it's yours." Same body copy, pricing card shows $79 | Class B: 16-24 GB GPU owners, towers, mini-PC homelab | `?v=a`, `utm_source=r-hardware`, `r-selfhosted`, `st-forums` |
| B | Personal perpetual license $149 one time | Identical body copy, pricing card shows $149 | Class C: 48-128 GB workstation owners (GB10, Strix Halo, Mac) | `?v=b`, `utm_source=reddit-dgxspark`, `strix-halo`, `mac-guides` |

Variant assignment is per channel in the table in B1, cookie the assigned
variant so return visits keep it, and every CTA link carries the variant plus a
source tag for clean A/B analysis. Winner decision rule, funnel event names, and
the min-300-visitors target are in PRICING.md and are final.

## B2: Community outreach message drafts

House rules first: one distinct post per channel, never a paste; lead with the
economics; no hype adjectives; attack the pricing structure, never the people;
link the docs, not a sales page; no em dashes. Each draft below is a first-post
lead the channel answer can expand. Verbatim numbers are anchored in
HARDWARE-MATRIX.md and COMPETITOR.md.

1. r/LocalLLaMA (launch thread). "Steward is the anti-Ghost: it detects your
   machine, auto-selects the right open models, and serves them to your phone
   from hardware you already own. Ghost Core charges $3,499 for a 24 GB GPU box
   to deliver software that runs on a $600 used 3090, which has more bandwidth
   than Core's card. Free to install, one-time $79 license, no subscription.
   Details and the docs: [link v=a]. What breaks first?" Then answer every
   comment, including the hostile ones.
2. r/hardware. "The Ghost Core value test: $3,499 for a 24 GB RTX Pro 4000 box.
   Same 24 GB tier on a used 3090 for about $600, then a one-time $79 license
   adds the assistant layer. Steward does detect + plan + wizard on the box you
   own. Am I wrong about the economics? [link v=a]"
3. Hacker News (Show HN). Title: "Show HN: Steward, the anti-Ghost personal AI
   (MIT, runs on hardware you own)" Body: the README narrative, the 24 GB
   ceiling fact, `steward wizard`, the honest "still to build" list (daemon,
   clients, OTA, license gate), and the docs links. Never omit the honest list.
   Both variants linked [v=a][v=b].
4. r/selfhosted / r/homelab. "An always-on personal AI under 15 W on a mini PC
   you already run: Steward detects the box, picks the bundle, serves your
   phone. Cost per year is the electricity plus a one-time $79. Self-hosted,
   MIT, no cloud account. [link v=a]"
5. NVIDIA GB10 / DGX Spark owners (r/DGXSpark, GB10 threads). "You bought the
   best local-AI hardware on the market. Steward is the missing intelligence
   layer: full 80B MoE tier served locally, phone app, no subscription. One-time
   $149, and residency honest: your 128 GB pool is exactly what an 80B needs.
   [link v=b]"
6. Strix Halo owners (r/Minisforum, r/Beelink, AI Max+ 395 threads). "96-128 GB
   of unified memory is a 70B/80B machine. Steward turns it into an always-on
   assistant reachable from your phone. One-time $149, no subscription, no
   cloud. [link v=b]"
7. Mac local-LLM guides (r/macmini, r/macstudio, LocalLLaMA Mac threads). "The
   cheapest legitimate 70B is a used M1 Max Studio. Steward adds the no-setup
   bundle selection, persistent memory, and phone access on top. One-time $149.
   Complements the existing build guides, does not undercut them. [link v=b]"
8. ServeTheHome forums. Written overview, not a listicle: the power and
   cost-per-year story, the certified hardware matrix link, the honesty rule
   about memory classes. [link v=a]

## B2: Launch checklist

Blocks marked [blocked] are not done yet and gate going public; [warn] are
soft. Source of truth for each gate is the named doc.

1. Landing page renders both variants locally (`python3 -m http.server` from
   site/, `?v=a` and `?v=b`). A/B tab hidden from real traffic. DONE B1 + B2.
2. Full detect-to-serve loop proven on one Class B and one Class C machine
   (docs/ARCHITECTURE.md covers Class C on U1; Class B machine needed)
   [blocked: needs a separate 16-24 GB machine].
3. `steward wizard` prints valid bundles on the same two machines. Wizard
   scaffolded in T1; wizard end-to-end verified on Class B [uncertain].
4. Email capture has a real backend. The form is a stub today; stand up the
   list and set `STEWARD_EVENTS_ENDPOINT` so `page_view` and `email_submit`
   transmit (docs/PRICING.md funnel) [blocked].
5. Serving daemon and mobile chat wired end to end (T2 sprint, Oct 13)
   [blocked].
6. Record a 30-second screencast of the install-to-first-answer path. Skip
   until items 2 and 5 pass [blocked].
7. CTA links carry variant + source tag (r-hardware, r-selfhosted, dgxspark,
   strix-halo, mac-guides, hn) for the A/B split. Cookie assignment implemented
   in app.js [warn: cookie code not written yet].
8. Copy passes the house-rule pass: no hype adjectives, no em dashes, attack
   only the pricing structure. Docs read-through required before first post
   [warn].
9. The honest "still to build" list is current and included in the Show HN
   body, per the community rule. AGENT-BRIEF "Next" is the live list.
10. Send sequence locked: Show HN first, r/LocalLLaMA same day, then Class B
    channels in week 2, Class C in week 3, transparency follow-up at day 30
    with the A/B readout (B1 schedule). Do not post to any channel until items
    2-5 pass; broken first impressions are permanently expensive.

## B2: Timing note

Ghost Core batch 1 ships Oct 31, so the first independent core reviews land in
early November. That window is when Steward must already be a known alternative
in these communities. Treat the review wave as the readout moment for the
competitor synthesis in COMPETITOR.md, not as the reason to launch early before
the blocked gates above pass.
