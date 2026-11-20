# Steward 1.3.0 Release

Date: 2026-11-19. Cut by the release card t_935b6364 with
`tools/cut_release.py --version 1.3.0` in real mode. This file is the
measurable record: every number below is quoted from tool output or a
re-download and hash of the published artifact, never guessed.

## Release and tag

- Release URL:
  `https://github.com/dmltallen/steward/releases/tag/v1.3.0`
- Release name: `v1.3.0`, target commit `master`.
- Tag commit: `141691fa64a807be302e860bbeb880dbc3823d00` (local
  `refs/tags/v1.3.0` and `gitea refs/tags/v1.3.0` both resolve here).
  This is the release commit "release: 1.3.0 publish the signed update
  manifest", which carries `releases/update.json` on master.
- Master at cut time: `ab1451f`, pushed; the release commit `141691f`
  landed on master after publish, then this evidence doc as `3ca5325`
  with doc-only wording fixes `70d39da` and `e344d8e`. Master HEAD ==
  gitea/master == `e344d8e` (verified with `git ls-remote gitea
  refs/heads/master`), and the tag stays on the release commit
  `141691f`.
- Version bump commit: `de234d1` (pyproject.toml and
  `src/steward/__init__.py` to 1.3.0, v1.3.0 CHANGELOG section, README,
  QUICKSTART, ARCHITECTURE 1.7, ROADMAP 1.3, WAVE-STATE, homebrew and
  winget templates). Test-fixture fix: `82c3050`. Release-tool fix:
  `ab1451f`.

## What was cut

`python3 tools/cut_release.py --version 1.3.0` ran to completion in real
mode: preflight, suite, build, artifacts, manifest, publish, commit, tag,
push. The full transcript is quoted in the CHANGELOG v1.3.0 section and
below. The only tool failure encountered was a publish URL construction
bug (the API base doubled the repo path into
`.../don/steward/api/v1/repos/don/steward`, which gitea answers 404).
Fixed in `ab1451f` (`_api_base` splits scheme://host:port from the repo
web base; preflight and publish now build
`https://github.com/api/v1/repos/don/steward/...`), committed and
pushed, then the cut reran clean. This is exactly the "live publish leg
not run end to end" residual risk the 1.3.0 review recorded; the bug was
real and is now fixed and exercised.

## Assets (published, then re-downloaded and hash-verified)

The seven assets from the release, each re-downloaded after publish from
`/releases/download/v1.3.0/<name>` and compared byte-for-byte:

| asset | bytes | sha256 (re-downloaded) | match |
| --- | --- | --- | --- |
| SHA256SUMS | 261 | 73ad1fe92e5e5f0c67c758e8423815549b7f6df176f965c5c1b1e15e76af4ab8 | ok |
| steward-install.sh | 4411 | ca515f3df46d50aa052d64654e41b47fceecba6dd230c9c85b4efba87a28ddbe | ok |
| steward-install.sh.sha256 | 85 | 47c93f4a8dfef1ac1febb20257e06167034e98e9876ad046a5fc535f1205d2db | ok |
| steward-linux-arm64 | 16218520 | c8f1b13cccab13e6ff0513881a505f18669b02187aabc4f1ed7ee5aff5cd7abe | ok |
| steward-linux-arm64.sha256 | 86 | fafed2488b206fe800bb57e073a7875eec95b8de84522e1024b3cef32ada0af9 | ok |
| steward_1.3.0_arm64.deb | 15900432 | 5386317457e9913c0cb0948202fd9a3b01be75b3e23fecbd9b963dcfe4ee29e5 | ok |
| steward_1.3.0_arm64.deb.sha256 | 90 | 674cd8a2af63e1ce9cfb4ff5bd1f82a420e23ac5378e40702990e75c94757713 | ok |

SHA256SUMS on the release matches the re-downloaded hashes row for row,
and each `.sha256` sidecar matches its primary artifact. The cut's own
publish stage printed the same re-read hashes with `ok` on every row.

## Frozen binary

`steward-linux-arm64` from the release, run fresh:

```text
$ ./steward-linux-arm64 version
steward 1.3.0
```

## Signed update manifest

Path on master: `releases/update.json` (committed by the release commit
`141691f`). Envelope `{alg: ed25519, payload, sig}`, signed with the
vendor key at `~/.steward-keys/steward-signing.key` (0o600).

- catalog version: 2026-11-19.1, released 2026-11-19, 9 models
- app version: 1.3.0, release notes URL
  `https://github.com/dmltallen/steward/releases/tag/v1.3.0`
- app payload `linux-aarch64`: url
  `https://github.com/dmltallen/steward/releases/download/v1.3.0/steward-linux-arm64`,
  sha256 `c8f1b13cccab13e6ff0513881a505f18669b02187aabc4f1ed7ee5aff5cd7abe`,
  size 16218520
- signature prefix: `2a7a723c0fb2cb5ed0cb0ff7...`

The cut's manifest stage self-verified the minted file through the
shipped code path before publish:

```text
ok   signature verified by steward.ota.verify_manifest (catalog 2026-11-19.1, app 1.3.0, source linux-aarch64)
ok   manifest app payload sha256 matches steward-linux-arm64
```

The exact bytes served at the baked default source
(`/<raw/branch/master/releases/update.json`) were fetched with repo
access and matched the committed file:

```text
manifest bytes served at baked default: 3847
manifest sha256 (served): ae3e77f2e367de727082213545f6438b9991b2a40650a23c86e7e2028e086621
manifest sha256 (repo   ): ae3e77f2e367de727082213545f6438b9991b2a40650a23c86e7e2028e086621
byte-identical to committed releases/update.json: True
```

## Update check

From a scratch `STEWARD_HOME` with no `STEWARD_OTA_URL` (the baked
default manifest source exercised), against the exact manifest served
at that source:

The 1.3.0 frozen binary, same version installed:

```text
catalog:  embedded -> 2026-11-19.1 (9 models) [AVAILABLE]
app:      1.3.0 -> 1.3.0 (not available)  https://github.com/dmltallen/steward/releases/tag/v1.3.0
source:   /tmp/rel138-verify/manifest-served-1.3.0.json
channel:  unknown
```

The published v1.2.0 binary (downloaded from the v1.2.0 release,
sha256-verified against the v1.2.0 SHA256SUMS) against that same
manifest, proving the 1.2.0 -> 1.3.0 upgrade path:

```text
catalog:  embedded -> 2026-11-19.1 (9 models) [AVAILABLE]
app:      1.2.0 -> 1.3.0 [AVAILABLE]  https://github.com/dmltallen/steward/releases/tag/v1.3.0
source:   /tmp/rel138-verify/manifest-served-1.3.0.json
```

An anonymous stock `steward ota check` (no repo access) against the
baked default URL 404s, by design, because the repo is private:

```text
update check failed: HTTP Error 404: Not Found (source: https://github.com/dmltallen/steward/raw/branch/master/releases/update.json)
```

This is the documented state: the manifest is public-safe but the repo is
not yet public (see docs/PUBLIC-LAUNCH-PLAN.md M2). A client with repo
access, or a future public mirror, resolves the same URL.

## Test suite on the final tree

```text
422 passed in 89.40s (0:01:29)
```

(from `PYTHONPATH=src .venv/bin/python -m pytest -q` on master at
`ab1451f`; same suite re-run by cut_release stage 2 with the repo venv
command). A final re-run on the tree that now holds this evidence doc
(master `3ca5325`) is `422 passed in 83.37s (0:01:23)`. Baseline before
the bump was `422 passed in 83.40s`. Mobile:
`npx tsc --noEmit` clean (exit 0), mobile tests `50 / pass 50 / fail 0`.

The two test files that asserted an app update against the fixture
version 1.3.0 moved their fixtures to 1.4.0 (`82c3050`), because with the
package at 1.3.0 a manifest advertising 1.3.0 is the same version and
`app.available` is correctly False.

## NOT MEASURED (deliberate, with reasons)

- Windows, macOS and linux-amd64 build legs: defined in the CI matrix and
  packaging templates but never run on this host; they carry the labels
  UNVERIFIED / NOT MEASURED / template only (docs/BUILD-MATRIX.md). Only
  Linux arm64 artifacts are published.
- Homebrew cask and winget manifest: templates, version line bumped to
  1.3.0 per the shipper note, placeholder sha256 and example.invalid
  URL; no real Windows or macOS binary backs them.
- macOS signing and notarization: unconfigured (needs an Apple Developer
  account); the code-signing warning ships with the artifact.
- Anonymous install update check: 404 on the private repo (quoted
  above). The manifest is reachable with repo access; public hosting is
  the M2 switch (docs/PUBLIC-LAUNCH-PLAN.md).
- No daemon, chrome process or port was left running by this release.

## Next step for a user upgrading from 1.2.0

A manual install (the in-app updater's channel) runs:

```text
steward update check
steward update apply
```

which downloads `steward-linux-arm64` (sha256-verified against the
manifest), health-probes it with `steward version` (expects
`steward 1.3.0`) and swaps the binary. A package-managed 1.2.0 install
(apt/brew/winget) is told the newer version exists but the manager owns
the file:

```text
app:      1.2.0 -> 1.3.0 [AVAILABLE]
```

For apt: `sudo apt install --only-upgrade steward`. For a manual
install, `steward update apply` (or a fresh `steward-install.sh` run)
gets 1.3.0. Full channel and rollback detail: docs/RELEASE.md.
