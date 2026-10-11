# Steward 3-Day Sprint Plan (Oct 11-13 2026)

Five scheduled agent sprints, two tracks (technical, business), one per window.
Each cron prompt tells the agent to read THIS file, run only its assigned
sprint, update its status here, update AGENT-BRIEF "Next", and report.

## Schedule

| Sprint | Window (PDT) | Track | Focus |
|---|---|---|---|
| T1 | Oct 11 00:30 | Technical | Serving backend benchmark + CI + installer scaffold |
| B1 | Oct 11 09:00 | Business | Landing page + acceptance-pricing A/B design + GTM outline |
| M | Oct 12 00:30 | Technical (app) | iPhone + Android app scaffold, beautiful and easy |
| B2 | Oct 12 09:00 | Business | Ghost review/sentiment synthesis + sales one-pager + acceptance launch prep |
| T2 | Oct 13 00:30 | Technical | End-to-end integration on U1 + first-look demo |

## T1 (Technical): serving backend + CI + installer scaffold

Goal: turn the v0.1 selector prototype into something that can actually serve
a model, and lock the build with CI.

Deliverables:
- Benchmark Ollama vs a vLLM option on U1 (GB10) for the realistic bundle
  sizes: gemma4:7b or qwen3:8b (already cached candidates), then the largest
  model that can be pulled and served within the run (target Qwen3-Next 80B
  A3B; if the download is too large for the window, record honest partial
  numbers and say so). Measure tokens/sec, first-token latency, and memory.
  Record the decision and numbers in docs/ARCHITECTURE.md and close the
  "serving backend" open item.
- Add `.gitea/workflows/pytest.yaml` so the selector/detect tests run on
  u1-runner (mirror the working don/watchmaster pytest pattern). Verify CI
  state after push via the gitea Actions API and report the run result.
- Scaffold v0.5 installer flow: a first-run CLI (`steward wizard`) that calls
  detect() + plan(), renders the chosen bundle, and prints the two setup
  commands. No OS installers yet; CLI wizard only.

Acceptance: benchmark table in ARCHITECTURE.md with real numbers from U1; CI
workflow file present and one run dispatched; `steward wizard` runs on U1 and
prints a valid bundle for the detected hardware.

## B1 (Business): landing page + pricing validation design + GTM

Goal: a beautiful, self-hostable landing page and a testable pricing plan.

Deliverables:
- `web/` or `site/` static landing page for Steward following the product
  narrative in README and PRICING (owned AI on hardware you have, no
  subscription, no hardware markup). Clean modern design (large type, calm
  palette, one product shot or diagram placeholder, an email capture form
  stub). Self-contained static HTML/CSS/JS, no build step, viewable locally.
- A concrete acceptance-pricing A/B design: two landing variants that change
  only the price (e.g., $79 vs $149 anchor) plus the free-tier conversion
  funnel, ready to point at Class B and C buyer communities. Write the plan
  and the analytics hooks (simple event logging stub) into docs/PRICING.md and
  the page.
- GTM outline in docs/GTM.md: channels (r/LocalLLaMA, r/hardware, HN, GB10 and
  Strix Halo owner forums, Mac local-LLM guides), launch positioning
  (the anti-Ghost message), and the first 30 days of outreach.

Acceptance: landing page renders locally with the two pricing variants
swappable; PRICING.md gains the A/B plan + funnel; GTM.md exists with named
channels and message.

## M (Technical, app): iPhone + Android app scaffold

Goal: a beautiful, easy-to-use mobile app skeleton for iOS and Android,
built with Expo/React Native.

Requirements:
- `apps/mobile/` Expo + React Native (TypeScript) project that runs on both
  iOS and Android.
- A polished design system: theme tokens (colors, typography, spacing,
  corner radius), light and dark modes, native-feeling components (safe
  areas, bottom tabs, haptics where supported). The aesthetic: calm, premium,
  privacy-first, large readable type, the "intelligence you own" brand.
- Screens (skeletons with real layout, working navigation):
  1) Onboarding that explains the pitch in three screens,
  2) Pairing: QR scan stub that would pair to a Steward daemon over LAN,
  3) Assistant chat: message list + composer wired to a stub local API,
  4) Settings / license: free vs perpetual tier states and a stub upgrade path.
- The chat and pairing screens talk to a configurable daemon URL (constant or
  env), so T2 integration can connect it without app surgery.
- Do NOT ship a subscription flow; the product is perpetual license.

Acceptance: `npx expo start` boots the app with all four screens navigable,
design tokens defined in one file, and a README in apps/mobile with run
instructions for iOS (Expo Go / simulator) and Android.

## B2 (Business): Ghost intel + sales one-pager + acceptance launch prep

Goal: turn the review watch and positioning into sales material and launch
instruments.

Deliverables:
- Pull the current Ghost Core review watch state (cron job 09105353e733, and
  `~/.hermes/competitor-watches/ghost-core-reviews.json`) plus community
  sentiment; write the synthesis into docs/COMPETITOR.md: what independent
  reviewers said (if any), what the launch claims hold or fail, and the
  "Steward vs Ghost" comparison table.
- A one-page sales/positioning sheet (docs/SELLING-ONE-PAGER.md) usable for
  outreach: the pitch, the numbers (price vs hardware spend, capability
  ladder), the objection handling (support, setup, model choice).
- Acceptance launch prep: finalize the A/B variant content from B1 (copy +
  price variations), the community outreach message draft, and a launch
  checklist in docs/GTM.md.

Acceptance: COMPETITOR.md and SELLING-ONE-PAGER.md exist and are cited;
GTM.md gains the launch checklist and outreach drafts.

## T2 (Technical): end-to-end integration on U1 + first-looking demo

Goal: wire the pieces into a working local product moment on U1.

Deliverables:
- Daemon: serve the selected bundle via the chosen backend on U1, expose a
  small local API (health, chat, bundle info) at a fixed port on localhost.
- App connection: point the apps/mobile client at the daemon and get the chat
  screen to exchange one message against a real local model (small model is
  fine; the 27B or 80B tier if serving holds).
- First-eleverance demo: make the daemon able to look at one real data source
  (e.g., a local calendar file or a folder of notes) and answer/summarize it
  through the app. Record the demo steps + a screenshot if capturable.
- Update docs/ARCHITECTURE.md (running topology), docs/ROADMAP.md (status),
  and AGENT-BRIEF Next.

Acceptance: localhost API answers a real model-generated message; the mobile
chat screen shows it; the demo steps are documented with quoted output.

## Guardrails (every sprint, binding)

- MEMORY GUARD (added Oct 11 after T1 died to the OOM killer): no source
  builds of C++ inference engines inside cron workers (no llama.cpp/vLLM
  compile; use the installed Ollama or prebuilt wheels); never run a model
  pull and a build concurrently; no 80B-tier pull inside a cron window;
  record honest partials with [uncertain]. Use the localhost hostname form
  for local API tests, not raw IPs (security scan blocks them).
- Read docs/AGENT-BRIEF.md rules before anything: English only, no em dashes,
  no subscriptions, selector single-source, gitea = source of truth, no
  invented model identities, spool discipline.
- Every write is committed and pushed; verify remote HEAD == local HEAD at the
  end (GET /api/v1/repos/don/steward/branches/master, token in
  ~/.git-credentials in the form http://don:<token>@github.com).
- Honesty: if a deliverable cannot finish in the window, land a partial with
  explicit what-is-done/what-is-left and mark [uncertain] where data is
  missing. Never fabricate benchmark numbers, test results, or review quotes.
- Update the sprint row status in this file and the AGENT-BRIEF Next queue.
