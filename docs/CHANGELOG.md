# Steward Changelog

## v1.2.0 (2026-10-28, released 2026-11-19)

Updates now reach the person, not just the daemon: the web client and the
phone app check on open, show a plain-language notice with the new version
and release notes, and install with one click. The daemon replaces itself
with the applied build and the clients reconnect on their own.

- **One-click update in the web client.** On load (auto-check on by default)
  the client asks `GET /v1/update/check`; when a newer signed build is
  available a non-modal notice appears with the version, notes/notes-url and
  an Update button. `POST /v1/update/apply` streams real download progress
  (SSE `progress`/`phase`/`result` events), and after an applied app swap
  the daemon re-execs the freshly installed binary in place (same serve
  flags) while the client shows a reconnecting state and lands back in Chat
  with history intact.
- **Settings updates card (web + phone).** "Check for updates", current app
  and daemon versions, and an auto-check toggle (default on). Auto-check off
  means the client makes no check request at all.
- **Daemon restart seam.** `steward serve` installs an in-place `os.execv`
  restart after a successful app apply (the route reports `restart_intent`
  and the manual `steward update apply` command as the never-a-dead-screen
  fallback); tests and embedded daemons observe the intent without exec'ing.
- **Phone (apps/mobile).** Same notice on foreground and in Settings. The app
  is not in a store yet, so the install path is a signed APK download with
  sha256 verification against the daemon-verified manifest, falling back to a
  copyable link when verification fails. Store distribution is the 1.1 route;
  `apps/mobile/src/lib/install.ts` and `buildInstallAction` are shaped so the
  store path drops in without rewriting the screens.
- **Frozen binary serves the webapp.** The PyInstaller spec now bundles the
  daemon-served SPA assets, so an installed `steward` binary serves the UI.

Proof (docs/images/s15-*): the real notice (`s15-notice-1280x800.png`), the
live download progress and reconnect states (`s15-update-progress-*`,
`s15-update-reconnect-*`), and Settings after the swap showing app and daemon
version 1.2.0 (`s15-settings-1.2.0-*`). `tools/e2e_update.py` runs the full
old->new frozen-build upgrade plus a tampered-payload refusal end to end.

## v1.1.0 (2026-10-28)

The signed update envelope now carries the Steward app itself, and the
daemon checks and applies updates on demand instead of riding winget/brew/
apt with `STEWARD_OTA_URL` set by hand.

- **Manifest schema v2.** The signed OTA envelope (same Ed25519 vendor key
  as licenses and the v1 catalog) may now carry an `app` block: version,
  released, notes or changelog URL, a minimum source version, and per-
  platform payloads with a sha256 and size. v1 manifests verify unchanged.
  Schema is documented in `src/steward/ota.py` and `docs/ARCHITECTURE.md`.
- **App payloads download into staging** through the existing resumable,
  cancelable, checksum-enforced downloader (`steward.download`), never a
  second downloader. A tampered manifest or a tampered payload is refused
  at download and at apply, before anything is written.
- **Atomic apply with the health-probe rollback model.** A verified payload
  is installed to an immutable `<data>/ota/app/` file and the `current.json`
  pointer commits catalog + app in one atomic rename. The health probe runs
  the new binary with `steward version`; on failure the pointer is restored
  to the previous value and the new app binary is removed, so the old
  binary still answers. A kill mid-swap leaves the old install usable
  (proven through the `_kill_hooks` seam).
- **Daemon routes.** `GET /v1/update/check` and `POST /v1/update/apply`
  (bearer-required like every other mutating/data route). The check also
  runs once at daemon start in a background thread and records its status;
  an unreachable source is a normal start with a recorded status, never a
  failed unit. No timer, no systemd unit, no separate updater process.
- **CLI parity.** `steward update check` and `steward update apply`, feeding
  off the same check/apply functions as the daemon routes; `--source` and
  `--no-app` override the default.
- **Released default source.** `config.ota_url` defaults to
  `https://github.com/dmltallen/steward/raw/branch/master/releases/update.json`;
  `STEWARD_OTA_URL` overrides it. `tools/mint_manifest.py` builds the signed
  manifest deterministically from built artifacts for the release flow to
  publish.

## v1.0.1 (2026-10-27)

Test-drive fixes, all found by running the private-environment drive on U1.

- **First token at 0.084s instead of 17s.** The assistant now asks the backend
  for no hidden reasoning by default (`think: false`) and keeps the model warm
  between turns (`keep_alive: 30m`). Measured on U1 with the same prompt:
  first visible token 17.0s with thinking on, 0.11s with it off; turn total
  23.7s down to 1.5s. Operators can turn thinking back on.
- **`steward serve` accepts its own flags.** `--port`, `--model`, `--source`,
  `--host`, `--ollama-url` were rejected with "unrecognized arguments" even
  though the daemon supported them, which also broke any systemd unit using
  them.
- **`steward setup --keep-daemon` now leaves a daemon running.** It previously
  printed that the daemon was left running while the in-process thread died
  with the setup process. It now hands off to a detached `steward serve`,
  verifies /health, and prints the pid and log path or an honest fallback.
- **`steward brief`** generates today's brief on demand and prints it; the
  daemon gained `POST /v1/briefs/generate` for the web client. Previously a
  brief could only appear on a schedule.
- **Install script private-repo support (F1).** `STEWARD_GITEA_TOKEN` is sent on
  the release API and both downloads, with an explicit private-repo hint
  instead of a bare failure.
- **Arch-aware install docs (F2).** README and QUICKSTART no longer name an
  amd64 deb that this release did not build.
- **`steward ota status` reports the embedded catalog size** instead of 0.

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
