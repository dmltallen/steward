# Steward Pricing and Licensing

**Version:** 2.1, 2026-11-19 (corrects the Vellum anchor to its real monthly
price and adds the competitor price band measured in docs/research)

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
128 GB owner pays about 3-6%. Competitive anchors: Vellum runs to $200/month
(30, 100 and 200 dollar tiers plus a free self-hosted open-source route),
Perplexity Computer and Claude/ChatGPT Pro also charge $200/month, and the
one-time desktop band runs from $29 to $349 (WinRAR $29, Alfred 34 pounds,
Things $49.99, Fork $60, Affinity ~$170, DaVinci Resolve Studio $295, MultipleChat
$250, Msty Aurum Lifetime $349). A one-time $149
that outlives two months of a Pro subscription and needs no new hardware is an
easy yes for someone already in at $3,000+.

## Acceptance-validation A/B design (active, this sprint)

Goal: test the license price against real buyers of Classes B and C before
locking the shelf. The shelf above is a hypothesis, marked [uncertain] until
this runs.

### The two variants

Two landing-page variants that differ in exactly one element, the price:

- Variant A: Personal license anchored at $79. Served to Class B buyers
  (16-24 GB discrete-GPU owners, the Ghost-class tier). Vehicle: v=a URL.
- Variant B: Personal license anchored at $149. Served to Class C buyers
  (48-128 GB unified-memory workstation owners). Vehicle: v=b URL.

All other copy, layout, and flow are identical on both variants. Only the
price string changes. This keeps the comparison clean: any difference in
behavior is attributable to price, not to page design.

### Serving the split

- `site/index.html?v=a` is the $79 variant, `?v=b` the $149 variant. The
  query parameter sets `window._stewardVariant` on page load; the A/B tab on
  the page is a local demo switcher only and should be hidden behind a query
  key (e.g. `?ab=show`) before external traffic, so real visitors never see
  the other variant and contaminate the test.
- Buyer communities map to one variant each: link Class B forums (r/hardware,
  3090/5080 owners, local-LLM GPU guides) to v=a; link Class C forums (GB10 /
  DGX Spark, Strix Halo, Mac Studio local-LLM guides) to v=b. See GTM.md for
  the channel list.
- Cookie the assigned variant so a buyer who returns keeps their variant.

### Free-to-paid funnel (stages and event names)

Funnel stages, each a trackable event in `site/app.js`:

1. `page_view` on load, tagged with variant, utm source and referrer.
2. `email_submit` on the capture form (pre-purchase interest; stub today,
   wires to a list when a backend exists).
3. `install_start` / `install_done`: fired by the wizard or app on first run.
4. `assistant_first_use`: first assistant turn after install.
5. `price_cta_click`: the pricing call-to-action (purchase intent).
6. `purchase_success`: license activation.

Conversion definitions: free-to-email (step 2 / step 1), free-to-install
(step 4 / step 1), free-to-paid (step 6 / step 1). The target is at least 5%
free-to-paid within 60 days of each cohort's first `page_view`, per PRD
success metrics.

### Event-logging hooks (on the page now)

- `site/app.js` buffers every event into `window.__stewardEvents` as an array
  of objects: `{ e, variant, url, ts, ...props }`.
- A page-wide `steward:event` CustomEvent fires per event so other scripts or
  devtools can watch without code changes.
- To transmit, set `window.STEWARD_EVENTS_ENDPOINT` before app.js runs and the
  logger POSTs newline-delimited JSON to it (`keepalive: true`, offline safe).
  Nothing leaves the page until that endpoint exists and is configured.
- Bundle this hand-rolled logger or swap it for a hosted analytics tag later;
  the event schema above is the contract, not the implementation.

### Decision rule

Run each variant until it has accumulated at least 300 qualified visitors or
45 days, whichever comes first. Winner is chosen on free-to-paid conversion
and revenue per visitor, not on raw CTA clicks (a higher anchor can raise
revenue per sale while lowering click rate). If conversion is close (within
two points on free-to-paid), keep the higher price. Lock the shelf after the
run and record the result and sample sizes in this file.
