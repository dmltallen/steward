# Steward Changelog

All notable changes to Steward, newest first. Versioning follows release
tags on the `don/steward` gitea repo, which is the source of truth. Commit
hashes below resolve against that repo.

Conventions in force for every entry: English only, no em dashes, no
subscription language, no fabricated numbers. Anything not measured is
marked NOT MEASURED with the reason.

## [1.0.0] - 2026-10-26

The first public release. Steward turns a computer you already own into an
always-on personal AI assistant: it detects the machine, computes what it
can run, auto-selects and serves open models through a local Ollama
backend, and is reachable from a web app in the browser and a phone app.
Install to first answer is a guided wizard; the assistant, your chats and
your files stay on the box; the license is perpetual, not a subscription.

Version note: the package version line was 0.2.0 through the sprint series
and is set to 1.0.0 by the release card (I3) on the tagged tree.

### Foundations (October 10 to 13 prototype sprints)

- `0c5908f` v0.1 prototype: hardware detection (`steward doctor`) and the
  auto model selector (`steward plan`), plus the opening product docs.
- `6ac8626` three-day sprint plan and agent brief, `2e0e1e9` cron
  memory-safety guard after a worker OOM kill.
- `97566dd` T1: serving backend benchmarked, Ollama chosen for v1, pytest
  CI workflow added, `steward wizard` first-run scaffold.
- `1630b52` B1: self-hosted landing page in site/, acceptance-pricing A/B
  design, GTM outline.
- `a8cd414` M: Expo/React Native mobile app scaffold for iOS and Android
  with a shared design token system.
- `d11d9b6` B2: Ghost competitor synthesis, sales one-pager, GTM launch
  prep with the A/B tab gated behind `?ab=show`.
- `d14a86b` T2: local daemon on 127.0.0.1:3877 serving the selected bundle
  via Ollama with a real local chat and data-source summary (docs/DEMO.md),
  mobile chat wired end to end.

### Product cards (landed for v1.0.0)

- `25149c6` vLLM versus Ollama benchmarked on U1, serving decision closed
  with real numbers (Ollama stays v1; vLLM deferred to a dedicated-tower
  option behind the same runtime seam). Full method in bench/.
- `bdd9857` S1 core: the runtime seam, Ollama sidecar and system backend,
  config and data paths (single data root under ~/.steward).
- `6328473` S3 compat: computed install-time verdict (no-fit, lite, full)
  replaces the certified hardware list; `steward check` explains any
  machine in plain English. This is the basis of docs/COMPATIBILITY.md.
- `38d3cfe` S2 daemon: streaming SSE chat, LAN pairing auth with a
  loopback-only 6 digit code, mDNS discovery, conversation store.
- `ca49179` `9b36257` S4 packaging: Linux one-file binary, Debian package,
  winget and Homebrew templates, checksum-verified curl installer, release
  CI.
- `5cb60a1` S7 retrieval: local embedding index built from your own
  folders, with citations on every hit.
- `1ec242e` I1 integration: wave 1 (S2, S3, S4, S7) merged with a green
  suite.
- `c7dbc83` `813405c` S8 memory: facts store, morning brief, user job
  scheduler with catch-up and awake windows, brief quality fixes.
- `fab073b` S9 tools: approved tool connectors behind one gate with a
  sandbox (realpath containment, https exact-host allowlist) and a SQLite
  approval store; every write, exec and network call waits on a human
  decision or auto-denies.
- `638c1c0` S12 diagnostics: redacted support bundle (`steward doctor
  --bundle`), stdlib log rotation at 5 MB, secret-pattern redaction.
- `b26c8e6` S6 mobile: real pairing (camera QR and manual 6 digit code,
  token in the secure store), streaming chat with stop and copy, offline
  cache of the last 20 messages, license screen. No subscription anywhere.
- `3270d5b` S5 webapp: the daemon-served single-page client (Chat, Files,
  Brief, Settings, Pair) with no external requests and a locked-down content
  security policy.
- `f67cf38` I2 integration: wave 2 (S5, S6, S8, S9, S12) merged and the
  route table wired with zero conflicts; all route families proven live.
- `9bd0ad3` S10 wizard: first run in one screen via `steward setup`, a
  resumable preflight, verdict, explain, download, runtime, selftest, pair
  machine with a resumable and verified model downloader (Range resume,
  sha256 gate, cancel).
- `c13ad52` S11 license and OTA: Ed25519-signed offline perpetual licenses
  (free, personal, home, power) that keep working without a connection, and
  a signed model catalog update path with health-probe rollback.
- S13 docs, this change: ship-facing README, quickstart, privacy and
  compatibility guides, release changelog, landing-page fact pass, real
  webapp screenshots under docs/images/, and dated version bumps
  (PRD 2.0, ARCHITECTURE 1.4, ROADMAP 1.2, all 2026-10-26).

### Measured at this release

- Full Python suite on the merged tree at S13: 220 passed in 38.01s.
- 60-day sprint suite counts are reported per card: 132 passed at S10
  (wizard + download), 136 passed + 1 skipped at S11 (license + OTA).
- Webapp screenshots in docs/images/ are real captures of the running
  webapp served by the daemon on a real Ollama backend (qwen3:8b), at
  1280x800 and 390x844.
- NOT MEASURED: Windows and macOS binaries (no build host on this project;
  the winget and Homebrew manifests are templates with placeholder
  checksums). Site traffic and free-to-paid conversion (the launch A/B run
  has not happened yet).
