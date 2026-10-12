# Steward Competitive Synthesis: Ghost Core

**Version:** 1.0, 2026-10-12 (B2 sprint)
**Sources check date:** 2026-10-12. Watch: `~/.hermes/competitor-watches/ghost-core-reviews.json`
(state cutoff 2026-10-10, held no reportable events). Light web sweep done in B2.

## Executive finding

There are **no independent hands-on reviews of an actual Core unit yet.** Batch 1
is sold out and ships October 31, 2026 (TechCrunch, CellCog, Ghost product page).
Everything written so far is either press re-reporting of the October 5 launch or
community commentary without a unit. Under the watch's materiality threshold
(genuine third-party test of a real unit), the watch correctly reported zero
events. This sprint adds the surrounding signal: community sentiment in exactly
the audience Steward targets, and a claims audit.

Do not fabricate review quotes or benchmark numbers below; every quote is from a
real fetched source and every vendor number is labeled as a vendor claim.

## What independent reviewers said

None with a unit, because none shipped yet. The closest things to independent
tests are:

- r/LocalLLM thread on the launch (Oct 9-10), the same buyer audience as
  Steward's GTM channels. Strong skeptical sentiment. Verified quotes:
  - "Sounds impossible for that price, i am very skeptical" (u/notwim).
  - "I simply dont see how they can possibly be making money with this. The cost
    of the hardware is simply too much. I feel like this could be some kind of
    rugpull or scam." (u/juggarjew).
  - "This is 100% vaporware or a straight up scam." (u/Rangizingo).
  - "We've reached peak AI psychosis. Guy screwing commodity PC Hardware together
    now counts as startup." (u/FullstackSensei).
  - "Like all this is, is a software layer or some custom linux and that's about
    it." (u/SnooPeripherals8750).
  - "I feel like a single RTX 3090 could outperform this thing, except in terms
    of power consumption." (u/vegavil).
  - "$3500 USD for a system that still can't comfortably fit anything larger
    than Qwen 3.8 27b at q4_m." (u/GeorgeTheGeorge).
- A YouTube comparison "Ghost AI Computer or a Used RTX 3090 Build? I Priced..."
  exists (title only verified; content not reviewed by B2) [uncertain].
- Product Hunt listing: launcher post only, no independent reviews, 8 followers.

## Launch claims audit (hold / fail)

| Ghost launch claim | Verdict | Evidence |
|---|---|---|
| Subscription-free, one-time $3,499, "keep running if Ghost shuts down" | Holds as stated, partial | No sub at purchase (Product Hunt, TechRadar). But OTA model updates come from Ghost servers, so new-model updates stop if Ghost dies (CellCog Q3). Box-runs-offline is a company claim, no unit test [uncertain] |
| Credential firewall protects logins for agent autonomy | Fails as launched | "A credential firewall is still being built" (CellCog), "still being built" per founder. Did not ship with batch 1 as of Oct 5 docs |
| Existing hardware is "a terrible experience on many levels" (Javaid to TechCrunch) | Disputed, unverified | Directly contradicted by the r/LocalLLM homelab audience that runs these exact configs daily; "single RTX 3090 could outperform this thing" |
| 24 GB VRAM is the model ceiling; runs Qwen 3.8 27B class | Holds spec-wise | RTX Pro 4000 SFF 24 GB; community math shows the practical ceiling is Qwen 3.8 27B at q4_m. Exactly matches Steward's tier claim: 24 GB = Ghost class |
| Performance: Qwen 3.8-Next at 30-35 tok/s | Vendor claim only | Founder's X post quoted in the r/LocalLLM thread ("unsloth 2 bit quant", RAM offloaded). Not independently benchmarked. 2-bit + DDR5 offload is the admission that 24 GB alone is the limit [uncertain] |
| 770 AI TOPS | Vendor claim only | Ghost's own figure (CellCog), unverified |
| Batch 2 will cost more | Holds as a statement | Founder on X: GPU price doubled since supply lock at ~$1,700 (verified via CellCog and the r/LocalLLM quote) |

## Steward vs Ghost

| Dimension | Ghost Core | Steward |
|---|---|---|
| Price | $3,499 one-time (batch 1); batch 2 likely more | Free install; one-time $79 / $149 / $249 |
| Hardware | Bundled RTX Pro 4000 SFF 24 GB, Ryzen 5 7600, 64 GB DDR5, 1 TB NVMe | Runs on hardware you own (16 GB GPU to 192 GB workstation) |
| Model ceiling | 24 GB VRAM class (Qwen 3.8 27B q4_m; 2-bit Qwen 3.8-Next offloaded to RAM) | Same 24 GB tier at $79; 70B/80B MoE at $149; 109B MoE at $249 |
| Memory bandwidth | 432 GB/s GPU | e.g. used 3090: 24 GB at 936 GB/s for ~$600 (more bandwidth, ~1/6 the price) |
| Setup | None, you buy a box and wait for shipping | `steward wizard`: detect + plan + bundle install, auto-selected |
| Subscription | None, but OTA model service tied to Ghost | None anywhere; $59/yr upgrade plan optional, never a gate |
| Credential isolation | Promised, firewall not shipped in batch 1 | Roadmap feature pack (credential firewall pro), not yet built [uncertain] |
| Residency honesty | N/A, no claim | An 80B MoE needs about 48 GB; Steward never claims it on a 24 GB box |
| Independence | Closed hardware + software, company-run OTA | MIT source, self-hosted, offline by construction |
| Reviewability | No unit in reviewers' hands until Oct 31 | Internal only so far; nothing public yet |

## Implications for positioning

1. The community is already making Steward's argument. "Buy the box" is read as
   a rugpull-level value problem by the exact buyers Steward targets. Lead with
   the economics, not a side-by-side takedown.
2. Ghost's own numbers (24 GB ceiling, RAM offload, 2-bit quant) validate
   Steward's tier claim: the 24 GB class is Ghost tier. A used 3090 at ~$600 has
   more bandwidth than Core's GPU.
3. Credential firewall is unshipped on both sides; never imply Steward ships it
   today. Keep it on the roadmap truthfully.
4. The Oct 31 shipping window is the timing cue: review verdicts land in early
   November. The A/B test and outreach should be live before then so Steward is
   a known alternative when the first Core reviews hit.

## Sources

- TechCrunch, Oct 5 2026, "At 19, Ghost founder raises $11 million":
  https://techcrunch.com/2026/10/05/at-19-ghost-founder-raises-11-million-to-build-a-3499-computer-for-your-personal-ai
- CellCog, Oct 5 2026, "Ghost Core: $3,499 Personal AI Computer and $11M Seed":
  https://cellcog.ai/blog/ghost-core-personal-ai-computer
- r/LocalLLM launch thread (Oct 9-10): https://www.reddit.com/r/LocalLLM/comments/1wygdsf/
- Product Hunt, Ghost Core: https://www.producthunt.com/products/ghost-core
- TechRadar: https://www.techradar.com/ai-platforms-assistants/teen-founder-reveals-personal-ai-computer-for-running-local-agents-usd3-499-mac-mini-rival-runs-on-nvidia-rtx-pro-4000-gpu-and-promises-to-be-a-subscription-free-brain-in-a-box
- YouTube, "Ghost AI Computer or a Used RTX 3090 Build?" (title only) [uncertain]
- Steward internal anchors: docs/HARDWARE-MATRIX.md, docs/PRICING.md, docs/GTM.md
