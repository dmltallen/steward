# Wave state: 1.3.0 integration

Merged by STEWARD-INTEGRATE-A (t_4d4d8af2) into `master`. Gitea is the
source of truth; this file records what merged, how the collisions were
resolved, and what this wave left open. No version bump, no tag, no release
happens here: the release card owns that, and `pyproject.toml` stays at 1.2.0.

## Merged branch tips (all nine, in order)

| # | branch | tip | card |
|---|--------|-----|------|
| 1 | wt/release-tooling | fa551f16a19d4428a9b986493e13fe8801333ecd | t_06cb7249 |
| 2 | wt/ota-hardening | efabe959b7c412c3751eeb0bfb6fe62cd05225c9 | t_fe71b5ff |
| 3 | wt/update-channel | 22848518b0ce68fd2004af16c60518f912b56d86 | t_16b496a9 |
| 4 | wt/feature-packs | fd77d8c099e0ae10cc353be2332e1ed3511f576e | t_7cc78607 |
| 5 | wt/cite-core | 006a2d58e9a34cc7a288dfed7c41da8c1dc39d4f | t_09f6fccb |
| 6 | wt/build-matrix | edd1fdd025a5ecc1b987b858e42341a990473dfe | t_440fb590 |
| 7 | wt/public-repo-bootstrap | acd00b006dfd03bdc7075d6d74f14ac185774bfe | t_f82ed19c |
| 8 | wt/f8-tools-chat | f5f9fb525a791f8dd0ba685b71c317b89a616d44 | t_912b7fde |
| 9 | wt/f8-tools-ui | d89cad2f52583d33f1e58b575ebf1ab58a0dabad | t_9a0b2bcd |

Every tip is an ancestor of `master` (`git merge-base --is-ancestor <tip>
master` verified for all nine). `master` head pushed: 78b1656.

## Collisions resolved by hand

- **docs/RELEASE.md** (release-tooling + update-channel, add/add): both
  branches created it with different content. Kept both intents in one file:
  the release runbook (cut_release flow, version rule, rollback) plus the
  update-ownership contract (one owner per install, managed refusals,
  Homebrew auto_updates rule, release checklist). One factual correction:
  the checklist originally named `src/steward/__init__.py` as the version
  source of truth, but `pyproject.toml` is the enforced single source
  (`test_version_single_source`); the merged text says pyproject and notes
  the enforced fallback.
- **packaging/homebrew/steward.rb** (update-channel + build-matrix): kept
  build-matrix's full cask template structure (version 1.2.0, sha256/url
  placeholders, depends_on, binary, caveats) and re-inserted update-channel's
  `NEVER add auto_updates true` rule block above the sha256 line. Both
  intents present.
- **packaging/homebrew/README.md** (update-channel + build-matrix): merged
  the two into one: "Who owns updates" (update-channel contract) plus
  "Status: the tap does not exist" (build-matrix template state and publish
  steps).
- **src/steward/config.py**: only release-tooling's DEFAULT_OTA_URL comment
  was a real change; f8-tools-chat left config.py untouched (per its card),
  so the predicted key collision did not materialize. No manual edit needed.
- **src/steward/server/daemon.py and cli.py**: each merged branch applied
  cleanly in order (ota-hardening, update-channel, feature-packs CLI packs
  line, f8-tools-chat tool loop). Git auto-merged all of them; the merged
  daemon carries the channel properties, the managed apply refusal, and the
  run_loop chat handlers together. Verified by import + integration tests.
- **.github/**: build-matrix workflows and public-repo-bootstrap templates
  live in disjoint subpaths, so both merged cleanly.

Anything else merged by the 'ort' strategy with no conflict.

## Test count on the merged tree

403 passed in 76.66s (`python -m pytest tests -q` from the merged master via
uv with numpy/zeroconf/cryptography). That is 395 from the merged wave plus 8
new tests added here for the chat auto-citation wiring
(`tests/test_chat_citations.py`).

## What this card added (beyond the merges)

- **F4 chat auto-citation wiring** (deferred here by t_09f6fccb and
  documented in docs/RETRIEVAL.md): the daemon chat path now grounds a turn
  in the data-root retrieval index when it has documents
  (`src/steward/server/daemon.py`: `_ground_chat`, `_append_chat_sources`,
  `resolved_retrieval_index`, `CITE_SCORE_FLOOR=0.5`,
  `CITE_BUDGET_CHARS=7000`). Numbered sources ride the system prompt; a reply
  that cites `[n]` gets a `Sources:` block appended. Fail-closed: no index,
  no documents, a backend error, or nothing above the floor leaves a plain
  assistant chat turn. `create_server` gained a `retrieval_index=` injection
  for tests. 8 new tests lock the grounded and fail-closed paths.
- docs updated to match: docs/RETRIEVAL.md (final paragraph now describes the
  live wiring), docs/TESTDRIVE.md (F4 rows moved from open/post-v1 to fixed
  in 1.3.0).

## Live proof on U1 (quoted in t_4d4d8af2 completion)

All six DONE exercises ran on the merged master with a rebuilt frozen binary
`dist/steward` (version string `steward 1.2.0`) and the merged source
daemon + qwen3:8b:

- (a) `/v1/chat` tool loop: fs.read streamed `tool_call` then `tool_result
  ok:true`; fs.write streamed `approval_required`, approve created the file,
  deny returned `tool_result ok:false "denied by approval (user)"` and left
  no file.
- (b) grounded chat answer from real indexed files: the model answered
  "The Stellar Drive costs $1,299 [2]. Its warranty covers parts and labor
  for five years, but it does not cover accidental damage [1]." and the
  `/chat` shim reply ended with a Sources block resolving to
  `/tmp/steward-corpus/stellar-drive.md` lines 3-4 and
  `stellar-warranty.md` lines 3-4, both read back from disk.
- (c) `steward license status` on a scratch home: `packs: browser_agent v1
  (perpetual)`.
- (d) `steward ota check --manifest` on a tampered manifest (payload.version
  changed): `update manifest rejected: signature does not match the payload
  or the vendor key`, exit 1, no traceback (from the frozen binary).
- (e) `steward update apply` on a staged package-managed install (frozen
  binary placed at a path a fake dpkg-query owns): refused with
  `update is managed by apt; Steward does not replace a package-managed
  binary. Run: sudo apt install --only-upgrade steward`, exit 1, nothing
  staged.
- (f) `tools/sign_release.py sign` then `verify` on a real SHA256SUMS:
  `signature: OK` / `signer: 3c05e05e... (embedded vendor public key)`; a
  tampered SUMS was refused, exit 1.

## What remains open

- No version bump, no tag, no release: the release card owns that.
  `pyproject.toml` is untouched at 1.2.0; gitea tags unchanged (v1.0.0,
  v1.0.1, v1.2.0); no gitea release created.
- Windows / macOS legs of the build matrix and both package templates are
  written but NOT MEASURED on this host (no such build hosts); the freeze
  build here is Linux arm64 only. This is stated per-file in
  docs/BUILD-MATRIX.md and the templates.
- winget and Homebrew channel detection are code-complete but only measured
  through layout/test markers, not a real Windows or Homebrew box.
- macOS signing/notarization is unconfigured (needs an Apple Developer
  account); the code-signing warning ships with the artifact, per repo
  policy.
- `releases/update.json` on master still points at the private gitea raw
  path; anonymous clients get a 404 until hosting is made public (separate
  decision, recorded in docs/RELEASE.md).
- The next card in the wave is the release card; it may now run
  `tools/cut_release.py` against this master, whose dry-run path and the
  signed-manifest contract are already verified.
