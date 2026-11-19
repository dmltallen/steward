# Steward Public Launch Plan

**Version:** 0.2, 2026-11-19 (folds in the GitHub release-practice study and the local-AI monetization study; both are committed under docs/research)
**Status:** planning. Nothing here is published or decided unless it says DECIDED.

This document is the working plan for moving Steward from a private Gitea repository to a
publicly distributable product, and for making future paid feature packs possible without
re-architecting the product. It records what is measured today, what the field actually does,
and the sequence that clears each blocker. Section 10 lists the evidence base.

## 1. What "public" means, and it is three separate switches

| Switch | Today | What it unlocks | Reversible |
|---|---|---|---|
| Source visible (public repo) | private on NAS Gitea | trust, contributions, issue reports, CI on hosted runners | yes |
| Artifacts downloadable anonymously | private repo assets return 404 to strangers (measured 2026-11-19) | the one-line install and the one-click update actually work | yes |
| Product sold (storefront, pricing page, license sales) | not selling | revenue | yes |

Each switch can be flipped independently. This plan flips them in that order, and the product
is not called launched until the second switch is on.

## 2. Measured state at v1.2.0 (release cut 2026-11-19)

- Tag `v1.2.0` at commit `b3ca730`, built by the Gitea tag pipeline, seven assets verified by
  re-download: `steward-linux-arm64` (15,435,752 B, sha256 `321832de...`),
  `steward_1.2.0_arm64.deb` (15,092,884 B), `steward-install.sh`, three `.sha256` sidecars and
  `SHA256SUMS`.
- The signed update manifest exists for the first time: `releases/update.json`, ed25519, app
  1.2.0, payload `linux-aarch64`, absolute payload URL. Commit `ed6503d`. Verified with the
  shipped binary and with a v1.0.1 binary.
- Anonymous fetch of the manifest and of the release assets returns **404** because the
  repository is private. This is the single hard blocker on distribution.
- Only Linux arm64 artifacts exist. No Windows, no macOS, no amd64.
- Tools are unreachable from chat in 1.2.0 (F8); chat replies carry no citations (F4). Both are
  in the 1.3.0 wave.
- No product art exists, and the phone app is not in a store.

## 3. Repository and hosting shape

DECIDED (recommended, needs Don's confirmation): one public repository for the application,
holding source, packaging, docs, the CI workflows and the public half of the update manifest.
Keep out of the public tree, in a private operations repository or the NAS:

- the vendor signing key (`~/.steward-keys/steward-signing.key`, already gitignored, never
  leaves the NAS or a hardware-backed store)
- anything that mints licenses or catalog generations
- pricing experiments, GTM copy that is not ready, customer lists

The update manifest is public-safe and must be public: it is a signed document of version
numbers, hashes and notes, and the update check cannot work without it.

**Manifest as a release asset, not only a repo file.** Jan publishes `latest.json` (version,
pub_date, per-platform Ed25519 signature and URL) as an asset on every release, and Tauri
requires that signature and documents that it "cannot be disabled" (study 1, findings 1 and 11).
That is the design Steward already has, so publishing the manifest as a release asset next to
`releases/update.json` costs nothing and gives every version one immutable URL set. DECIDED for
M2.

**Asset assembly pattern** (Ollama, study 1 finding 2): one release per version, multi-asset,
`sha256sum.txt` generated last and uploaded last with `--clobber`, a required-artifact list
checked before the release is announced, and releases created as drafts first. DECIDED.

Hosting order: public GitHub Releases is the primary artifact and manifest source. The NAS
Gitea stays the development source of truth, mirrored one-way to GitHub on every master push,
so an account or hosting problem never takes the working repository away. `DEFAULT_OTA_URL`
must switch to the public manifest URL in the same commit that makes the repository public, or
every installed 1.2.0 box keeps pointing at a private URL that returns 404.

## 4. Release engineering

Exists today: a tag-triggered Gitea pipeline that builds the Linux arm64 binary and deb, writes
sidecar hashes and attaches them, plus the v1.2.0 release cut and verified by hand. Does not
exist: one command that cuts a release, a cross-platform matrix, or an automated manifest.
Cards: STEWARD-RELEASE-TOOLING, STEWARD-BUILD-MATRIX, STEWARD-RELEASE-1.3.0.

Rules, each of which has already bitten or is documented in the field:

1. Never cut from a dirty tree; the tag names the commit the artifacts came from. v1.2.0 broke
   this once in miniature: the pipeline built at `b3ca730` while a hand build from a slightly
   earlier tree was staged, and both landed on one release. The mixed set was deleted and the
   pipeline-built set shipped.
2. Verify every asset by re-download and hash, never by trusting the upload response.
3. Mint the manifest from shipped hashes, never by hand.
4. A tampered manifest must be refused with a readable line (card STEWARD-OTA-HARDENING).
5. CLI and daemon resolve the update source identically (same card).
6. **Build natively per OS on hosted runners, never cross-compile.** Observed labels in the
   field: Ollama uses `linux`, `linux-arm64`, `windows`, `macos-26-xlarge`; Jan uses
   `windows-2025`, `macos-latest`, `ubuntu-22.04` through reusable workflow templates (study 1,
   findings 2 and 3).
7. **One update owner per install channel.** The Ollama in-app updater fighting a Homebrew
   `auto_updates` cask is a documented open bug (study 1 finding 7). Steward must detect a
   package-manager install, let the manager own updates instead of replacing its own binary,
   and never claim `auto_updates` in a cask it publishes. Card STEWARD-UPDATE-CHANNEL.
8. **Pinned release tooling.** The Codecov 2021 compromise exfiltrated months of CI secrets and
   was caught only by a customer's SHA-256 check (study 1 finding 10). Release CI pins
   third-party actions and scripts by checksum, and signing material stays behind a GitHub
   Environment, never in the tree.

## 5. Trust surface a public project needs on day one

- `LICENSE` (MIT, present).
- `SECURITY.md` routing reports to GitHub Security Advisories with private vulnerability
  reporting enabled, and documenting the update verification story there: what the Ed25519
  requirement protects and what it does not.
- `CONTRIBUTING.md` with the DCO and no CLA. Node.js embeds the full DCO text and is a usable
  template (study 1 finding 9).
- Typed `.github/ISSUE_TEMPLATE/` that asks for `steward doctor --bundle` output; the redacting
  diagnostics bundle already exists and is tested.
- Secret scanning and push protection enabled before publication (free on public repos), plus a
  history scan: a clean working tree does not mean a clean history (study 1 finding 10).
- Signed provenance: `.sha256` sidecars and `SHA256SUMS` exist. Signing `SHA256SUMS` with the
  same ed25519 vendor key lets a downloader verify provenance without trusting the host. Card
  STEWARD-PUBLIC-REPO-BOOTSTRAP.
- **Code signing reality, stated plainly.** Without a Windows certificate every first run shows
  SmartScreen "Unknown publisher" on every version, and enterprise policy can block the
  installer outright; EV certificates stopped bypassing SmartScreen in 2024 and reputation
  builds only over weeks and hundreds of installs (study 1 finding 5). Azure Trusted Signing at
  about 9.99 USD per month is the realistic non-Store option. On macOS, distribution effectively
  requires Developer ID plus notarization since Catalina; an unnotarized release ships with an
  "Open Anyway" instruction (study 1 finding 6). Either buy both identities and keep one
  publisher identity forever, or document the warnings honestly. Do not promise a clean first
  run that will not happen.

## 6. Paid features: what the field proves, and what to build now

The business model is fixed by Don: free to install and use, a perpetual license to keep using,
optional one-time feature packs, no subscriptions. The entitlement seam is in the 1.3.0 wave
(STEWARD-FEATURE-SEAM): packs inside the existing offline ed25519 license, one `enabled()` gate,
unknown ids fail closed, a bought feature never gated off by config.

What the evidence says, in order of how much it constrains the build:

1. **Nobody in this niche charges for the local core.** LM Studio (free local, 20 and 100 USD
   per month cloud tiers), Ollama (local always unlimited, cloud tiers), Raycast (10 USD per
   month Pro), Jan, GPT4All, AnythingLLM and Open WebUI all keep local inference free and sell
   hosted services or additive features (study 2 finding 1). Paywalling local chat would be the
   single largest deviation from the field.
2. **AnythingLLM Pro is the closest working comp.** An MIT desktop app selling a proprietary
   key that only lifts daily caps on system-wide agent features, with the stated promise that
   whatever is free today stays free forever and Pro is purely additive. It gates no chat, no
   retrieval, no agents (study 2 finding 2, re-verified against the live page 2026-11-19).
3. **Only sell new substantive capability.** The evidence-backed safe list: multi-box mesh,
   multi-user and team controls, a browser agent, an advanced sandbox or credential firewall,
   smart-home connectors, an optional hosted off-LAN relay (the Obsidian Sync shape, monthly and
   completely optional), and priority support plus early model access as the 59 USD per year
   line. Never cosmetics, never trimmings, never a feature that was free in the previous release
   (study 2 finding 3 and section 6).
4. **The backlash envelope is defined.** Microsoft paywalling AI inside Notepad and Paint became
   the defining "charging inside an app you already own" story, and Plex's 200 percent lifetime
   price hike plus steering pressure produced a community revolt after years of goodwill (study
   2 finding 5). Rule: never gate local features, never hike a bought shelf, never remove
   something that was free.
5. **The offline-license problem is settled, not solvable.** No credible vendor stops a
   determined patcher, and licensing vendors say so themselves. Winners price around the honest
   majority and separate "worth paying for" (additive capability, upkeep, hosting, teams) from
   "must work free" (local inference, chat, memory). Patterns in use: signed license files
   (Steward's model), honor systems (Sublime, WinRAR), a 30-day offline grace against a server
   (JetBrains), and a light periodic check-in (MultipleChat's 14-day model) if anti-sharing is
   ever needed on a one-time tier (study 2 finding 6 and section 3).
6. **The shelf has headroom.** The one-time desktop band runs from 29 USD (WinRAR) through 49.99
   (Things), 60 (Fork), about 170 (Affinity), 295 (DaVinci Resolve Studio) and 250 (MultipleChat)
   to 349 (Msty Aurum Lifetime, which coexists with a 149 USD per year tier on the same
   product). The 79/149/249 shelf sits inside that band, one to two orders below Ghost's 3,499
   USD hardware anchor (study 2 findings 3, 4 and 7).
7. **Store rules force the architecture.** iOS 3.1.1 bans license keys or QR codes that unlock
   in-app features and requires IAP; Google Play requires Play Billing for in-app sales of
   functionality. Both sanction the clean answer: a consumption-only client. Apple 3.1.4 allows
   features that depend on hardware the user already owns to unlock without IAP, and Google's
   consumption-only FAQ describes exactly a client that accesses content paid for elsewhere.
   DECIDED: licensing is enforced only in the desktop daemon, purchases happen only on the
   desktop or the website, and the phone app never sells anything (study 2 finding 10 and
   section 6.3). Plan demo and pairing access for both store reviews.
8. **A documentation defect this study caught:** PRICING.md said Vellum "runs to $200/yr"; the
   live page says 30, 100 and 200 USD per month with a free self-hosted open-source route
   (verified 2026-11-19). Corrected in PRICING.md v2.1, which strengthens rather than weakens
   the anti-subscription argument.

What must never be gated, because it breaks the promise that makes the free tier worth
installing: install, local inference, chat, retrieval over the user's own files, memory, model
updates, phone and web pairing, the diagnostics bundle and the updater itself.

## 7. Sequence and gates

**M0, internal release (done 2026-11-19).** v1.2.0 tagged, built by CI, assets and manifest
verified. Exit: a stranger can verify hashes; the manifest verifies.

**M1, product complete (board: steward-v1, the 1.3.0 wave).** Tools reachable from chat with
approvals, citations in chat, entitlement seam, one-command release, cross-platform CI,
install-channel-aware updates, OTA hardening, and the public-repo bootstrap docs, then
integrate, independent review, and the 1.3.0 release. Exit: review gate with no blocking
findings and an update from 1.2.0 to 1.3.0 proven against published artifacts.

**M2, public repository.** Preconditions: vendor key custody confirmed, full history scanned for
secrets, secret scanning and push protection enabled, `SECURITY.md` and `CONTRIBUTING.md` live,
the GitHub Actions matrix green on at least linux amd64 and arm64, the manifest published as a
release asset, and `DEFAULT_OTA_URL` switched to the public manifest. Exit: anonymous curl of the
install script and of the manifest both return 200, and a fresh machine installs in one line
with no token.

**M3, Windows and macOS.** Needs the hosted runners that exist only after M2, plus a decision on
code signing (Azure Trusted Signing and an Apple Developer ID, or documented warnings). Exit: a
real `.exe` and a real macOS binary built by CI, hashes published, install docs matching what
exists, and SmartScreen and Gatekeeper behavior documented from a real run.

**M4, distribution and sales.** winget and Homebrew PRs (fork-and-PR, AV-scanned installers,
silent install and uninstall, immutable URLs with pinned hashes), phone store submissions as
consumption-only clients, storefront and license minting. Blocked on M2 and the pricing A/B.

## 8. Open decisions for Don

1. Public repo owner: personal `redonealpha-don` (already authenticated) or a new organization.
   The AGENT-BRIEF says personal project, not R1A, which argues against the R1A org.
2. Repository split as in section 3, or a single public repository with the key held outside the
   tree.
3. History rewrite before publication, or publish the existing history after a secrets scan.
4. Whether to buy code-signing identities (about 10 USD per month for Azure Trusted Signing plus
   an Apple Developer membership) before M3, or ship with documented warnings.
5. Whether the optional off-LAN relay is a paid hosted add-on at launch or later. It is the one
   recurring line the field supports without breaking the no-subscription rule, because it buys
   running infrastructure rather than gating the product.
6. Publish pricing at launch or hold the shelf until the A/B test.
7. Product name and trademark check before any storefront.

## 9. What is still unmeasured

- No anonymous install has ever been performed; every install so far used a token.
- No update has ever been applied from a published release to an installed older version; the
  in-repo end-to-end tool proves it for synthesized builds only.
- No Windows or macOS artifact has ever been built.
- No store submission has been attempted.
- Conversion at any price point: no published data exists for one-time desktop local-AI
  licensing. The nearest benchmark is a SaaS survey median of about 8 percent freemium
  conversion, and Steward's free tier requires no card, so the 5 percent target is a hypothesis
  and the A/B test is the only measurement (study 2 finding 8, marked uncertain).

## 10. Evidence base

Both studies are committed in this repository and carry a URL and a date per claim.

- `docs/research/github-release-practice-study.md` (2026-11-19, 44 sources): release asset
  practice, per-platform build automation, update mechanisms in the wild, day-one repository
  hygiene, winget and Homebrew requirements, and documented release-process failures. Primary
  evidence from live releases, the workflow files in ollama/ollama and janhq/jan, and Microsoft
  and Apple documentation.
- `docs/research/local-ai-monetization-study.md` (2026-11-19, 88 sources): how peer local-AI
  products charge, what packs buyers accept, the offline-license problem, the one-time price
  band, and the Apple and Google store rules for a LAN-server client.

Single-source items flagged in those studies: MultipleChat's 14-day check-in and its 250 USD
price come from its own site, which was session-flagged when re-checked on 2026-11-19, so treat
that datapoint as vendor-claimed rather than verified. Revenue-scale figures for Obsidian,
AnythingLLM and Msty are unpublished and marked uncertain in the study.
