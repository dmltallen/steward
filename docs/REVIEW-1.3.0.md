# Steward 1.3.0 Review

Date: 2026-11-19
Reviewer: independent review gate, decomposed by the root card t_eafa219e into four
slices (inventory, tool reachability and approvals, citations and feature seam, release
tool / secrets / CI labels / test controls), consolidated here.

## Verdict

**No blocking findings at the reviewed tip (master `457d972`).**

All seven blocking checks were exercised against real code, not eyeballed. Four findings
were raised during the gate: two documentation contradictions and two functional defects.
All four were fixed on master before consolidation and each is locked by tests. None is
open at the tip this report is written against. Details, disposal and the remaining
accepted residual risks are below.

## Scope

- Repo: `/home/user/steward`
- Range reviewed: `v1.2.0..HEAD` = 32 commits at the final tip `457d972`
  (`git describe` = `v1.2.0-32-g457d972`).
- Slice review tips: the citations and feature-seam slice ran at the wave tip `d5f2a90`
  (master `7ffab1f` plus the then-unmerged `wt/cite-wire`); the tool-reachability and
  release slices ran at master `9ea05ef`, where `wt/cite-wire` is merged. Since `9ea05ef`
  only `457d972` landed, and it is docs only (`docs/PUBLIC-LAUNCH-PLAN.md`, 26 insertions /
  21 deletions), touching no code, tests, CI, or the surfaces under review. The three
  slices therefore describe the code at the tip.
- Feature-to-commit map from the inventory slice (t_a9cfaa22): F8 tools = `f5f9fb5` +
  `d89cad2`; F4 citations = `006a2d5` + `78b1656` in master, `d5f2a90` for the client
  wire-up; offline paid feature-pack seam = `fd77d8c`; one-command release tool with signed
  manifest = `fa551f1` + `ed6503d`; CI matrix = `edd1fdd`.

## Baseline at the final tip (fresh runs, this consolidation)

```text
$ cd /home/user/steward && PYTHONPATH=src .venv/bin/python -m pytest -q
422 passed in 86.26s (0:01:26)

$ cd apps/mobile && npx tsc --noEmit
(exit 0, clean)

$ cd apps/mobile && npm test
tests 50
pass 50
fail 0
duration_ms 10559.948073

$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_citation_containment.py tests/test_license.py -q
38 passed in 0.13s
```

`git status` is clean at the tip. No code was edited by the review.

## Blocking checks and evidence

Each check is listed with what was exercised and quoted real output. Full transcripts live
in the per-slice review files attached to t_2f05859a, t_59b172ef and t_580b2d21.

### Check 1: Tool reachability (exercised live through the real /v1/chat SSE path)

Every registered tool is reachable from a chat turn the config allows, and none is
reachable when config disallows it. The registry as the daemon loads it:

```json
{
 "registered_tools": ["calendar.read", "calendar.write", "fs.list", "fs.read", "fs.write", "http.fetch", "shell.run"],
 "risks": {"calendar.read": "read", "calendar.write": "write", "fs.list": "read",
           "fs.read": "read", "fs.write": "write", "http.fetch": "net", "shell.run": "exec"}
}
```

A write tool runs only after a real human approval row; the side effect is confirmed on
disk, not taken on trust:

```json
{"event": "tool_result", "id": "f9c3925c-069f-40dc-8bc2-1a5d9af8ec4d", "ok": true, "summary": "wrote approved.txt (4 bytes)", "ms": 75}
file_exists: true, file_content: "made"
```

Config-disallow cases all refuse and create zero approval rows:

```text
shell.run with allow_shell=false  -> {"ok": false, "summary": "shell.run is disabled by config (allow_shell=false)"}, pending=0
empty fs roots                    -> "no fs roots are configured for the file tools" (list/read/write + calendar.write), pending=0
host not in https allowlist       -> "host 'evil.example.com' is not in the fetch allowlist", server_hit=[] , pending=0
non-https scheme                  -> "http.fetch allows https only, got scheme 'http'", pending=0
```

Bypass attempts all refused. Twenty three alternate spellings of tool names, including the
zero-width space, trailing whitespace, case variants and separators, all fail with
`unknown tool`, no handler runs, no approval row is created, and a planted secret string
never leaks:

```text
refused=23/23 leaks=[]
```

Prose that merely mentions a tool is never executed (`tool_calls: []`); only the fenced
JSON envelope executes. Absolute paths, raw `..` traversal, and symlinks inside the root
that point outside are refused for read, write, list and calendar tools, and a write
through a symlink to a nonexistent target creates no outside file:

```text
"path escapes the configured fs roots: /etc/hostname"
"path escapes the configured fs roots: /home/.../scratch/outside-hiihxmmr/secret-outside.txt"
newtarget_exists: false
```

Sensitive names are refused for both read and write (dotenv, .ssh, keys, pem and key
suffixes, .gnupg, id_ed25519): `refused=12/12`. An approved fetch whose 302 lands on a
non-allowlisted host is refused after approval:

```text
"redirect refused: host 'evil.example.com' is not in the fetch allowlist"
```

An approval decided after expiry fails closed in both arrival orders (decide-first and
wait-first):

```text
{"ok": true, "note": "approval had expired; denied", "approval": {..., "decision": "denied", "reason": "expired", "status": "denied"}}
{"ok": false, "error": "approval already denied"}
target_exists: false
```

### Check 2: Approvals (denial side effects verified on disk, not in response text)

A denial leaves zero side effect. For fs.write the target file is absent, the parent dir
is absent or empty, the row ends `denied` with reason `user`, and the audit log records
both the row creation and the decision. For shell.run the command never ran (no marker
file), and for http.fetch the allowlisted server was never contacted (`hits=[]`).

```text
{"event": "tool_result", "id": "d8c209e8-7655-4e9d-a1d1-187e20601077", "ok": false, "summary": "denied by approval (user)", "ms": 89}
file_exists: false
approval row: decision "denied", reason "user", status "denied"
```

An expired approval fails closed through the daemon HTTP path (row deadline forced to the
past; POST approve returns `denied by approval (expired)`, handler never runs, file
absent). A tool cannot run without an approval row: with approvals disabled, every
write/exec/net tool refuses with `no approval manager is available for {write,write,exec,net}-risk calls` and no file; a tool cannot run while its row is pending; each
write/exec/net call needs its OWN fresh approval row (a second fs.write does not reuse the
first approved row); and `wait()` auto-denies a blocked call on timeout.

```text
"no approval manager is available for write-risk calls"
"no approval manager is available for exec-risk calls"
"no approval manager is available for net-risk calls"
```

Schema and policy failures refuse before any approval row exists:

```text
fs.write missing 'path'       -> "schema validation failed: $: missing required property 'path'", approval_rows=0
fs.write extra property       -> "schema validation failed: $: unexpected property 'evil'", approval_rows=0
shell.run command not a str   -> "schema validation failed: $.command: expected type string, got integer", approval_rows=0
args not a dict               -> "tool arguments must be a JSON object", approval_rows=0
```

Summary for the slice: 30 main cases plus 2 bypass probes plus 5 gate-order probes, all
fail-closed, verdict no blocking findings.

### Check 3: Citations (exercised at the wave tip d5f2a90)

A reply cannot cite a source that was not in the provided context. With 2 sources in
context and markers [3] and [99] in the reply, both are dropped and never reach used or
render:

```text
provided context: 2 sources; numbers = [1, 2]
reply                : 'The flagship costs three thousand [1] but a third source claims otherwise [3] and then 99 [99].'
cited (used)         : [(1, '/tmp/.../notes/alpha.md')]
dropped (blocked)    : [3, 99]
render_sources       : 'Sources:\n[1] /tmp/.../notes/alpha.md line 3: The bazoo flagships cost three thousand.'
```

Even a hallucinated marker under an empty context resolves to nothing:
`extract_citations('The answer is in file [1] and [7].', []) -> {'used': [], 'dropped': []}`.

A no-hits turn never invents a file. Sub-floor retrieval streams an empty sources list and
the prompt carries the honest no-coverage instruction:

```text
build_prompt([])      : "A search of the user's own files returned no passage that covers this question. Answer that nothing in the files covers it. Do not invent an answer and do not use outside knowledge. Do not use source markers.\n\nQuestion: What is the airspeed velocity of an unladen swallow?"
retrieval hits (sub-floor score 0.1, floor 0.5): scores = [0.1]
sources event : [{"event":"sources","sources":[]}]
```

The path-leak case was the subject of a blocking finding; its disposal is under Blocking
findings below. At the final tip the reviewers scenario is closed: a hit outside the
configured roots emits `sources == []` and the reply stays ungrounded.

### Check 4: Offline feature seam (four cases exercised, all pass at disposal)

A bought pack survives a broken config (config can never take a signed grant away):

```text
enabled('browser_agent', lic, hostile dict                                ) = True
enabled('browser_agent', lic, None                                        ) = True
enabled('browser_agent', lic, {}                                          ) = True
enabled('browser_agent', lic, a bare string                               ) = True
enabled('browser_agent', lic, config claims 'features':['browser_agent']  ) = True
```

A bought pack survives a clock change. A perpetual pack stays active at 2036 and 2099; a
pack with a signed expiry ends on time and never drags the tier grant down:

```text
clock set to 2036-01-01: verify_document(now=2036-01-01) -> tier=personal (license perpetual) | has_pack(today=2036-01-01) = True | enabled = True
clock set to 2099-12-31: verify_document(now=2099-12-31) -> tier=personal (license perpetual) | has_pack(today=2099-12-31) = True | enabled = True
time-limited pack multi_user (signed expiry 2026-11-01), clock 2026-11-20: enabled(multi_user)=False enabled(scheduled_jobs/tier)=True
```

A bought pack survives a tampered license file after the fix (see Blocking findings): a
signed-envelope tamper fails closed to the free tier with no crash, and a malformed
non-JSON active.json now also resolves to the free tier instead of raising.

An unknown feature id fails closed, on the free tier and on a top-tier power license alike:

```text
enabled('no_such_feature', free=None) = False | enabled('no_such_feature', power) = False | known = False
enabled('free_lunch', free=None) = False | enabled('free_lunch', power) = False | known = False
```

The gate cannot be skipped by editing config alone:

```text
config edited to claim paid: {"features": ["tool_exec","scheduled_jobs","multi_device_pairing","browser_agent","multi_box_mesh"], "paid": true, "allow_shell": true, "tier": "power"}
  enabled('tool_exec', free-tier license, greedy config) = False
  enabled('browser_agent', free-tier license, greedy config) = False
  enabled('chat', None, {'disabled_features':['chat']}) = False  (the ONE legal config lever: opt-out of a default-on free feature)
```

### Check 5: Release tool (tools/cut_release.py exercised in dry-run on an isolated clone)

Every refusal is proven live with exit=1:

```text
dirty tree        -> "cut refused: working tree is not clean:" (lists the untracked probe)
version mismatch  -> "cut refused: pyproject.toml version 1.2.0 does not match --version 9.9.9; bump it first"
existing tag      -> "cut refused: tag v1.2.0 already exists locally at d38dc5053f42; a release must be a new version"
failed test run   -> "cut refused: test suite failed (exit 1); fix before cutting a release"
```

Assets with a mismatched hash cannot be published. The publisher asset set is SHA256SUMS
plus its detached Ed25519 signature; after tampering one hash line, verification flips:

```text
$ python3 tools/sign_release.py verify --sums SHA256SUMS
signature: OK
signer:    3c05e05ea195d286aa124d6687728a1d9954025b7cb7533f39ee977fd14631bd (embedded vendor public key)
# tamper: sed -i 's/arm64/amd64/' SHA256SUMS
$ python3 tools/sign_release.py verify --sums SHA256SUMS
signature: INVALID
refusing: SHA256SUMS.sig does not verify against the embedded vendor public key ... (the file or the signature is tampered)
exit=1
```

The real publish path carries the same guard before touching a tag: re-download every
uploaded asset and compare sha256 (tools/cut_release.py 572-584), self-verify the minted
manifest against the built artifact before publishing (466-469). The live publish leg
needs the LAN gitea, so those checks were verified by reading plus the byte-for-byte
equivalent exercised above.

The minted manifest verifies through the existing OTA path, and a tampered payload is
refused:

```text
$ python3 - <<'PY'
from steward import ota
doc = ota.fetch_document("/home/user/steward/releases/update.json")
p = ota.verify_manifest(doc)
print("signature verified (no exception); catalog=%s app=%s platforms=%s" % (p["version"], p["app"]["version"], ",".join(sorted(p["app"]["payloads"]))))
PY
signature verified (no exception); catalog=2026-11-19.1 app=1.2.0 platforms=linux-aarch64
```

A fresh manifest minted with the real vendor key also verifies through
`steward.ota.verify_manifest`, and staging a payload with one flipped byte is refused:

```text
2) tampered payload (first byte now 0xac); manifest hash unchanged
   REFUSED as expected -> ChecksumMismatch: checksum mismatch for http://127.0.0.1:39055/steward-linux-aarch64:
   expected 1569e3e44ed4..., downloaded 322f7a08a811... The .part file was discarded; setup resumes from a clean download.
```

### Check 6: Secrets and packaging / honest CI labels

No private key, license blob or token in any committed file or workflow. The full-history
scan finds no PEM private key blocks, no token literals (workflows reference
`secrets.GITEA_TOKEN` / `github.token` only, never a literal value), no committed license
blob, no `.git-credentials`, and no key-material filenames tracked. The vendor signing key
lives outside the repo at `~/.steward-keys/steward-signing.key` (0o600), is gitignored
(hard-coded ignores in .gitignore), and its public half is the embedded constant in
`src/steward/license.py` line 246; the embedded public key equals the public half of the
on-disk key. The only committed signed envelope is the intended update manifest
`releases/update.json`.

The CI files cannot be mistaken for a working Windows build. The gitea workflow defines a
single Linux job and says so up front; the GitHub workflow labels its legs UNVERIFIED
("no GitHub repository exists yet, so this workflow has never run") and notes signing is
deliberately not configured; the Makefile `build-win` target, the winget and homebrew
templates and docs/BUILD-MATRIX.md all carry the literal labels TEMPLATE ONLY, NOT
MEASURED, never run, with zeroed hashes and example.invalid URLs. BUILD-MATRIX.md states
the rule in its opening line: "Read the Status column literally: a row that says template
only or NOT MEASURED is not a working install, no matter what the README promises."

### Check 7: Control-removal test spot checks (green to red to green, isolated clones)

Both tests below are live, non-vacuous checks wired to the real controls.

Spot-check 1, delete the pyproject version line (the same field cut_release validates):

```text
baseline: 1 passed in 0.01s
after deleting pyproject version line:
  tests/test_version.py:15: AssertionError   -> 1 failed in 0.01s
after git checkout -- pyproject.toml:
  1 passed in 0.01s
```

Spot-check 2, delete the browser_agent pack registry entry:

```text
baseline: 1 passed in 0.04s
after deleting the registry line:
  >           assert feat.is_pack(fid) is True
  E           AssertionError: assert False is True
  tests/test_features.py:194: AssertionError   -> 1 failed
after git checkout -- src/steward/features.py:
  1 passed in 0.01s
```

## Blocking findings and disposition

Four findings were raised during the gate. All four are fixed and locked on master; none
is open at the tip.

1. Documentation, docs/TOOLS.md lines 85-91 (root review attempt 1). It claimed the web
   client does not render tool events and the phone app has no approval UI, after merge
   `d89cad2` shipped exactly those. Fixed at `7ffab1f`; every factual claim in the new
   text was re-checked against code at the tip.
2. Documentation, docs/TESTDRIVE.md 9.3 item 3 (root review attempt 2). It presented the
   F8 decision as still open while the same file says F8 is fixed in 1.3.0. Fixed at
   `7ffab1f` (docs sweep confirmed it was the only remaining instance).
3. Functional, citations path leak (slice t_2f05859a). The SSE sources event could emit,
   and the daemon read off disk, a path outside the configured retrieval roots; a
   stale index could keep serving out-of-root hits after config roots narrow. Fixed at
   `240cbe3` (containment roots injected into `DaemonContext`, hits dropped unless their
   resolved path sits inside a configured root, at the grounding step and again in the
   sources step; `9ed0146` forwards `retrieval_roots` through `create_server`). Locked by
   five containment tests in tests/test_citation_containment.py plus tests/test_chat_sources.py
   pinning retrieval roots; the containment set was verified to redden with the filter
   removed.
4. Functional, corrupt license (slice t_2f05859a). A malformed non-JSON active.json raised
   JSONDecodeError out of `active_license()` instead of resolving to the free tier.
   Fixed at `240cbe3` (`verify_file` and `activate` now raise `MalformedLicense` for
   unreadable, non-JSON and non-object files; `active_license` catches `LicenseError` and
   `OSError`). Locked by six tests in tests/test_license.py. Reproduced before the fix
   with `printf 'not json at all' > $STEWARD_HOME/licenses/active.json` then
   `steward license status` (traceback), and after the fix the command reports the free
   tier.

The functional fix commit `240cbe3` adds 11 tests (5 containment, 6 license). The lock
files pass at the tip: `38 passed in 0.13s` for test_citation_containment.py plus
test_license.py, and the full suite is `422 passed in 86.26s` as shown in the baseline.

## Residual risks (accepted)

- An offline license in a shipped binary is patchable. Feature-pack gates and OTA
  manifests are verified offline against a vendor Ed25519 public key compiled into the
  application. A user who can patch the shipped binary can replace that embedded key,
  mint their own signed licenses and updates, and the patched binary will accept them.
  There is no online activation, license server or playback check to detect this, because
  the paid-feature seam is required to work with no connectivity. This is accepted: the
  seal deters casual tampering, it is not DRM against a determined attacker who can modify
  the binary. Mitigations in place: the private key never leaves the NAS or a protected
  release host, is gitignored and never committed (0o600), and the full git history is
  scanned for secrets before any public release. The embedded key must be treated as
  public from the moment a binary ships; beyond that point the integrity of a distributed
  copy is enforced by code signing and by checksum verification of the artifact, not by
  the license layer.
- Only the Linux arm64 build is measured and gated. Windows, macOS and linux-amd64 CI legs
  are defined but are labeled UNVERIFIED / NOT MEASURED / template only and have never run.
  A Windows or macOS native build has not been exercised end to end as of this review.
- The tools/cut_release.py preflight gates have no dedicated automated test file; they are
  proven by this review and by the fact that each failure stops the cut fail-closed.
- The live publish leg of cut_release (upload to the LAN gitea with the re-download hash
  compare) was not run end to end in the review; the guard lines were verified by reading
  and by the byte-for-byte equivalent tamper exercises on the signing path.
- A release cut must not run concurrently with a merge into master. One transient red run
  in tests/test_citation_containment.py was observed while the cite-wire merge landed
  mid-run; the stable tree passes 422/422. Because a cut fails closed on any suite
  failure, an overlapping merge makes a cut refuse (safe direction), but the run itself is
  not meaningful.
- The daemon defaults for shell and http are empty by design: with no explicit server
  override, `allow_shell=false` and an empty fetch allowlist, so a fresh install can reach
  only the read-risk tools. This is fail-closed, not a defect.
- Read-risk tools (fs.list, fs.read, calendar.read) intentionally skip approvals; their
  boundary is path containment plus the sensitive-name refusal, not a human prompt. This
  is a design choice recorded in the tool docs, accepted as residual.

## Repo state at end of review

`/home/user/steward` is on master at `457d972`, `git status` clean. This review file
(`docs/REVIEW-1.3.0.md`) is the only change made by the review. No code was edited.
