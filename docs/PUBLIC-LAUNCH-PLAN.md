# Steward Public Launch Plan

**Version:** 0.1, 2026-11-19
**Status:** planning. Nothing in this document is published or decided unless it says DECIDED.

This document is the working plan for moving Steward from a private Gitea repository to a
publicly distributable product, and for making future paid feature packs possible without
re-architecting the product. It records what is measured today, what is blocked, and the
sequence that clears each blocker.

## 1. What "public" means, and it is three separate switches

| Switch | Today | What it unlocks | Reversible |
|---|---|---|---|
| Source visible (public repo) | private on NAS Gitea | trust, contributions, issue reports, CI on hosted runners | yes |
| Artifacts downloadable anonymously | private repo assets return 404 to strangers (measured 2026-11-19) | the one-line install and the one-click update actually work | yes |
| Product sold (storefront, pricing page, license sales) | not selling | revenue | yes |

Each switch can be flipped independently. This plan flips them in that order, and the
product is not called launched until the second switch is on.

## 2. Measured state at v1.2.0 (release cut 2026-11-19)

- Tag `v1.2.0` at commit `b3ca730`, built by the Gitea tag pipeline, seven assets verified by
  re-download: `steward-linux-arm64` (15,435,752 B, sha256 `321832de...`),
  `steward_1.2.0_arm64.deb` (15,092,884 B), `steward-install.sh`, three `.sha256` sidecars and
  `SHA256SUMS`.
- The signed update manifest exists for the first time: `releases/update.json`, ed25519,
  app 1.2.0, payload `linux-aarch64`, absolute payload URL pointing at the release asset.
  Commit `ed6503d`. Verified with the shipped binary (`ota check` reads it) and with a v1.0.1
  binary.
- Anonymous fetch of both the manifest (`/raw/branch/master/releases/update.json`) and the
  release asset directory returns **404**, because the repository is private. This is the
  single hard blocker on distribution: an unauthenticated install or update cannot work.
- Only Linux arm64 artifacts exist. There is no Windows binary, no macOS binary, no amd64
  build. `packaging/winget` and the brew formula are templates with placeholder hashes.
- Tools are unreachable from chat in 1.2.0 (defect F8); citations do not appear in chat
  replies (F4). Both are being fixed in the 1.3.0 wave.
- No product art exists (no logo, no icon), and the phone app is not in a store.

## 3. Repository and hosting shape

DECIDED (recommended, needs Don's confirmation): one public repository for the application,
holding source, packaging, docs, the CI workflows and the *public* part of the update
manifest. Keep out of the public tree, in a private operations repository or the NAS:

- the vendor signing key (`~/.steward-keys/steward-signing.key`, already gitignored, never
  leaves the NAS or a hardware-backed store)
- anything that mints licenses or catalog generations
- pricing experiments, GTM copy that is not ready, customer lists

The manifest itself is public-safe: it is a signed document of version numbers, hashes and
notes. Publishing it is required for the update check to work at all.

Hosting order for artifacts: public GitHub Releases is the primary source, because it is free,
serves anonymously, keeps immutable URLs per tag, and is where the CI runners already live for
other platforms. The daemon's `DEFAULT_OTA_URL` must become the public manifest URL in the same
commit that makes the repository public, or every installed 1.2.0 box keeps pointing at a
private URL that returns 404.

The NAS Gitea stays the working source of truth for development, mirrored to GitHub, so a
GitHub outage or account problem never takes the working repository away. The mirror direction
is Gitea to GitHub on every master push, one-way.

## 4. Release engineering

What exists: tag-triggered pipeline on Gitea that builds the Linux arm64 binary and deb, computes
sidecar hashes and attaches them, plus a hand-verified release cut today. What does not exist:
one command that does the whole cut, a cross-platform build matrix, and an automated manifest.

Three cards on the steward-v1 board carry this: STEWARD-RELEASE-TOOLING (one command,
preflight, manifest, publish, verify), STEWARD-BUILD-MATRIX (GitHub Actions matrix for linux
amd64, linux arm64, windows amd64, macos arm64 plus the winget and brew templates), and
STEWARD-RELEASE-1.3.0 (runs the tool and proves the result a stranger could verify).

Rules the release process must keep, because each one has already bitten:

1. A version is never cut from a dirty tree, and the tag names the commit the artifacts were
   built from. Today's v1.2.0 broke this once in miniature: the tag pipeline built at `b3ca730`
   while a second hand-built set sat in `dist/` from a slightly earlier tree, and both landed on
   the release. The duplicate was removed and the pipeline-built set shipped.
2. Every asset is verified by re-download and hash comparison, never by trusting the upload
   response.
3. The signed manifest is minted from the shipped artifact hashes, never typed.
4. A tampered manifest must be refused with a readable message. Today it exits non-zero with a
   PyInstaller launcher crash (defect card STEWARD-OTA-HARDENING).
5. The CLI and the daemon must resolve the update source identically (same card).

## 5. Trust surface a public project needs on day one

- `LICENSE` (MIT, already present).
- `SECURITY.md` with a private reporting route and a stated response window. No public issue
  tracker for vulnerabilities.
- `CONTRIBUTING.md` with the DCO, no CLA: a one-person project should not carry a CLA's
  administrative weight.
- Issue and pull request templates that ask for `steward doctor --bundle` output (the redacting
  diagnostics bundle already exists and is tested).
- Signed releases: `.sha256` sidecars and `SHA256SUMS` exist. A release signature over
  `SHA256SUMS` with the same ed25519 vendor key would let a downloader verify provenance
  without trusting the host. Not built yet.
- Windows SmartScreen and macOS Gatekeeper will warn on unsigned binaries. An OV or EV code
  signing certificate is the only real fix and it costs money every year; until then the
  install docs must state the warning plainly instead of pretending it will not appear.
- No secrets in the tree: the key stays out, and the CI uses the default token only.

## 6. Paid features: what to build now so future packs are possible

The business model is fixed by Don: free to install and use, a perpetual license to keep using,
optional one-time feature packs, no subscriptions. The entitlement seam shipped as a card in the
1.3.0 wave (STEWARD-FEATURE-SEAM): a pack model inside the existing offline ed25519 license, a
single `enabled()` gate, unknown feature ids fail closed, and a bought feature is never gated
off by config or a failed check.

The honest constraint, stated here so no future plan pretends otherwise: an offline-verified
license inside a binary that ships to the user can be patched by a determined user. There is no
architectural fix; only choices about what to put behind the gate.

Three ways to monetize that survive that constraint, in order of fit for Steward:

1. **Packs whose value is content and upkeep, not code paths.** A pack that ships a curated
   local knowledge base, a maintained set of connectors, or industry prompt and workflow packs
   loses most of its value when copied without updates. The code gate is a courtesy; the
   updates are the product.
2. **Packs for organizations.** Multi-seat administration, audit export, policy templates. The
   buyer is an organization that wants a license on file, not a person who would patch a binary.
3. **Support and update subscriptions.** Explicitly out of scope under Don's rule. Noted and
   rejected, not forgotten.

What must never be gated, because it breaks the promise that makes the free tier worth
installing: chat with a local model, retrieval over the user's own files, pairing a phone, the
diagnostics bundle, and the updater itself. Gating updates or diagnostics would read as
hostage-taking.

Candidate pack shelf for the planning record (no prices committed, none built): Home Automation
connectors, Document Intelligence (OCR, contracts, invoices), Voice (local speech in and out),
Team Pack (multi-seat, audit), Vertical packs built from maintained content.

The pricing shelf itself (Personal $79 / Home $149 / Power $249) stays a hypothesis until an
A/B acceptance test runs. Nothing in the public launch depends on that test.

## 7. Sequence and gates

**M0, internal release (done 2026-11-19).** v1.2.0 tagged, built by CI, assets and manifest
verified. Entry: green suite. Exit: a stranger can verify hashes; the manifest verifies.

**M1, product complete (board: steward-v1, the 1.3.0 wave).** Tools reachable from chat with
approvals, citations in chat, entitlement seam, one-command release, cross-platform CI, OTA
hardening, then integrate, independent review, and the 1.3.0 release. Exit: review gate with no
blocking findings, and an update from 1.2.0 to 1.3.0 proven against published artifacts.

**M2, public repository.** Preconditions: key custody confirmed, no secrets in history (a
history scan, not a working-tree scan), `SECURITY.md` and `CONTRIBUTING.md` present, GitHub
Actions matrix proven green on at least linux amd64 and arm64, and `DEFAULT_OTA_URL` pointed at
the public manifest. Exit: an anonymous `curl` of the install script and of the manifest both
return 200, and a fresh machine installs with one line and no token.

**M3, Windows and macOS.** Blocked on runners that exist only after M2 (GitHub-hosted). Exit:
a real `.exe` and a real macOS binary built by CI, hashes published, install docs matching what
actually exists, SmartScreen and Gatekeeper behavior documented from a real run.

**M4, distribution and sales.** winget PR, brew tap, phone app store submission, storefront and
license minting. Blocked on M2 and on the pricing acceptance test.

## 8. Open decisions for Don

1. Public repo owner: personal GitHub account `redonealpha-don` (already authenticated) or a new
   organization. The AGENT-BRIEF says this is a personal project, not R1A, which argues for the
   personal account or a neutral org, and argues against the R1A org.
2. Repository split as proposed in section 3, or a single public repository with the vendor key
   held outside the tree (simpler, slightly riskier if a future commit sweeps the key in).
3. History rewrite before publication, or publish the existing history (which contains private
   NAS URLs, personal emails and the signing key path, though not the key itself).
4. Whether to publish pricing at launch or keep the shelf private until the A/B test.
5. Product name and trademark check before any storefront.

## 9. What is still unmeasured

- No anonymous install has ever been performed; every install so far used a token.
- No update has ever been applied from a published release to an installed older version; the
  in-repo end-to-end tool proves it for synthesized builds only.
- No Windows or macOS artifact has ever been built.
- No store submission has been attempted for the phone app.
- Conversion at any price point: no data. The shelf is a hypothesis.

Two research studies are commissioned and pending, one on GitHub release practice across
comparable consumer and open-source products, one on how local-AI products monetize and what
buyers accept as packs. Their findings fold into sections 4, 5 and 6 as version 0.2.
