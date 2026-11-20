# Steward Gate Test Drive

**Version:** 1.0, 2026-10-26

**Who runs this:** Don, on real hardware. This is a human gate, not an agent
build task. An agent prepared this file and confirmed the artifacts below,
then stopped. The stopwatch starts when you begin Part 0 and ends when the
friction log is filled in at Part 8.

**What this proves:** a fresh machine goes from nothing to a working,
paired, reboot-recoverable assistant in 15 minutes, and every place that
friction appears is captured with a severity so we can decide what is fixed
before v1 ships.

**What you will fill in:** the friction log table in Part 8. Every numbered
row gets three things written against it: the seconds it took, a short note
of what happened (expected vs actual), and a severity. Rows you skip get
"skipped" and a reason, never a silent gap.

## Definition of done for this gate

1. You ran the script end to end on at least one machine (U1 is the
   measured leg; see Part 1 for why Windows is not this release).
2. Every step either passed or produced a friction row.
3. Every friction row is marked **fix-now** or **post-v1**.
4. Every **fix-now** row has a follow-up card on the board (created when
   you close the gate).
5. The filled friction log is written back into this file on the
   `wt/gate-testdrive` branch.

## Part 0. Preflight: release artifacts (agent verified 2026-10-26)

Source of truth is the v1.0.0 release on gitea:
`https://github.com/dmltallen/steward/releases/tag/v1.0.0` (release id 26).

| Artifact the card asks about | Where it lives | Size (measured) | Status |
| --- | --- | --- | --- |
| Binary (one-file) | release asset `steward-linux-arm64`, also built by `make build-linux` to `dist/steward` | 48,279,208 bytes (46.04 MiB) | exists, sha256 verified below |
| Debian package | release asset `steward_1.0.0_arm64.deb`, built by `make build-deb` (nfpm) | 47,218,096 bytes (45.03 MiB) | exists, installed and removed in `debian:stable-slim` by the packaging card |
| Install script | release asset `steward-install.sh`, source `scripts/install.sh` | 3,413 bytes | exists, checksum-verified install proven in a container by the packaging card |
| Wizard | not a separate artifact. It is `steward setup` inside the binary; source `src/steward/wizard.py`. States: preflight, verdict, explain, download, runtime, selftest, pair, done (resumable) | n/a | confirmed present in the binary build (S10 merged into v1.0.0) |
| Webapp | not a separate artifact. Served by the daemon at `http://localhost:3877`; source `src/steward/webapp/{index.html,app.js,style.css}`. Screens: chat, files, brief, settings, pair | n/a | confirmed present in the binary build (S5 merged into v1.0.0) |
| Mobile bundle | not shipped as a release asset this version. S6 measured `expo export` bundles at 2.4 MiB HBC per platform (ios and android), built under `apps/mobile/dist`. Device run is manual via Expo Go (`npx expo start`) | 2.4 MiB HBC per platform (S6 measured) | source + dev-run only; see Part 6 note |

Live checksums fetched from the release on 2026-10-26:

```
steward-linux-arm64    ff8489c2f036321be0534185270de2b5cbed083d0c36213c1b6452863173c65a
steward_1.0.0_arm64.deb  e3b24f55acc2b450b25e83a36ec763af2b551e4010050c8c3316815a2ea407c5
steward-install.sh     afd269f32c701f652513aba41c95b9bfb22fba77f7feb9e8a0dc02550fa90f06
```

Every checksum agrees with the SHA256SUMS attached to the release and with
the I3 card's re-download hash comparison. The frozen binary was smoke
tested: `steward version` reports 1.0.0, and license activate/status and
tamper refusal ran inside the one-file binary.

## Part 1. Machine legs

**U1 (linux/arm64, GB10, this box).** The only architecture this release
built. This is the measured leg. Model `qwen3:8b` is already cached by the
system Ollama at `localhost:11434`, so the wizard download step passes
through with no network pull.

**Windows box.** Requested by this gate but not runnable this release. No
Windows binary was built (PyInstaller cannot cross-compile; the README winget
manifest carries placeholder checksums and is marked NOT MEASURED). Record
one friction row (F3) and proceed with U1 only. A Windows run becomes
possible only after a real Windows build exists on a Windows host.

Also note: there is no x86_64 Linux artifact either. Only `linux/arm64`
exists today. An amd64 Linux machine has nothing to install yet (see F2).

## Part 2. The 15-minute stopwatch budget

Total window is 15:00, hard. Each part has a target so you can see a slip
live. If the model had to download (only happens on a machine that has not
cached `qwen3:8b`), add the real download time on top and log it; on U1 it
is cached so the budget holds.

| Part | What | Target | Cumulative |
| --- | --- | --- | --- |
| 0 | Fresh machine state check | 0:30 | 0:30 |
| 1 | Install (deb or one-liner) | 1:00 | 1:30 |
| 2 | Sanity: `steward doctor` | 0:15 | 1:45 |
| 3 | Setup wizard, screen by screen | 4:15 | 6:00 |
| 4 | First chat in the webapp (warm) | 1:00 | 7:00 |
| 5 | First retrieval answer (Files tab) | 1:45 | 8:45 |
| 6 | Pair the phone, first mobile chat | 2:45 | 11:30 |
| 7 | Reboot, assistant comes back warm | 2:00 | 13:30 |
| 8 | Uninstall | 1:00 | 14:30 |
| 9 | Fill the friction log, verdict | 0:30 | 15:00 |

Slippage is expected and is data. If you pass 15:00, stop where you are,
log every unfinished step as a friction row, and that is the verdict for
v1. The number that matters is how far the script was at the line.

## Part 3. The exact script

Run on U1. Stopwatch starts now. Log every step's seconds and any deviation
into Part 8 as you go, not at the end.

### Part 0. Fresh machine state (0:30)

A clean machine means: no Steward data, no Steward binary, port 3877 free.

```sh
uname -m; lsb_release -ds          # expect aarch64, Debian/Ubuntu
test ! -d ~/.steward && echo "no data root (good)"
test ! -e /usr/bin/steward && echo "no install (good)"
ss -ltn | grep 3877 || echo "port 3877 free (good)"
```

What to see: arch `aarch64`, both "good" lines, and no 3877 listener. If any
of these is false, either you are not on a fresh machine (stop and pick a
clean spot, or run with a fresh `STEWARD_HOME=/tmp/steward-gate`) or a
previous gate left something behind. Record what you find.

### Part 1. One-line install (1:00)

**Route A, deb (recommended for the U1 gate, it is what the reboot check
needs).** Download the arm64 deb from the release (the repo is private, so
downloads need your gitea credentials; see F1), then install:

```sh
# in your downloads dir, with your gitea user and token:
curl -u USER:TOKEN -L -o steward_1.0.0_arm64.deb \
  https://github.com/dmltallen/steward/releases/download/v1.0.0/steward_1.0.0_arm64.deb
sha256sum steward_1.0.0_arm64.deb    # must equal e3b24f55... (see Part 0)
sudo apt install ./steward_1.0.0_arm64.deb
systemctl --user enable --now steward
```

What to see: the checksum matches, apt installs, and the systemd user unit
goes active. This is the route that survives reboot.

**Route B, the curl one-liner (as the README promises, and where F1 bites).**

```sh
sh -c "$(curl -fsSL https://github.com/don/steward/raw/branch/master/scripts/install.sh)"
```

What to see on a truly fresh machine right now: a 404, because the repo is
private and every anonymous path (raw script, release asset, release API)
returns 404. Measured on 2026-10-26. That mismatch between the documented
one-liner and reality is F1. For this gate, fetch the install script with
your token, run it locally, and when it fetches the `steward-linux-arm64`
asset it installs to `~/.local/bin/steward` and verifies the checksum. Note
that Route B does not install the systemd unit, so the reboot check only
applies to Route A.

### Part 2. Sanity (0:15)

```sh
steward doctor
```

What to see: data dir, hardware line (linux aarch64, serving memory 121.6 GB
verdict full), bundle assistant `qwen3:8b`, backend system-ollama ok with
models cached. Same numbers as Part 0 arch, never guessed.

### Part 3. Setup wizard (4:15)

```sh
steward setup
```

Watch every state and note any screen that does not look like the appendix
or hangs:

1. **preflight** the wizard reports serving memory 121.6 GB, free disk, and
   the detection path (`nvml` on U1).
2. **verdict** prints `full` with the measured memory number, never a guess.
3. **explain** states the model tier that lands here and the expected
   tokens per second against the 15 tok/s floor.
4. **download** passes through with no pull because `qwen3:8b` is cached.
   On a machine without the model this is where real download time lands;
   expect a progress line, sha256 check, and resume controls.
5. **runtime** shows "system-ollama at localhost:11434" and starts or finds
   the backend.
6. **selftest** sends one short chat (expect the word hello or equivalent)
   and, if a notes folder is configured, one note summary from it. The
   reply must appear on screen before you move on. This is the same check
   the app runs at boot.
7. **pair** prints the 6 digit code and the `steward://pair?host=...&port=3877`
   payload. Copy the code, you need it in Part 6. Loopback only, never on a
   remote screen.
8. **done** the wizard closes with the webapp address printed
   (`http://localhost:3877`).

Then confirm the daemon unit really serves it:

```sh
curl -s http://localhost:3877/health
```

What to see: `"ok": true`, `"served_model": "qwen3:8b"`, model list includes
`qwen3:8b`.

### Part 4. First chat, in the webapp (1:00)

Open `http://localhost:3877` in a browser (use the exact address the wizard
printed). The Chat screen shows a conversation list on the left. A fresh
install offers an automatic Hello exchange so the screen is never empty.

Type and send: `What am I running on, in one line?`

What to see: your question in a dark bubble, the reply streaming token by
token, then a couple of sentences about the machine (the wizard's selftest
has already loaded the model, so this reply is warm and should start within
a second or two). The conversation appears in the list. This message is
stored only under `~/.steward` (or your `STEWARD_HOME`).

### Part 5. First retrieval answer (1:45)

Give Steward a real folder to index, then ask it a question whose answer is
in that folder.

1. Make a scratch notes folder with two short markdown files, for example
   a file containing `Next review is Thursday at 10am with the vendor.`.
2. In the webapp go to the **Files** tab (`#files`). In the add-folder box
   enter that folder path and submit. The status line should list it as an
   indexed folder.
3. Click build index and wait for it to finish.
4. In the search box type a question that the file answers, for example
   `When is the next review?`, and submit.

What to see: one or more hits, each rendered with a snippet and a citation
line `Source <path> at char N - score 0.xxxx`. That hit with its citation is
the first retrieval answer. The chat layer also answers from the index: a
chat turn is grounded in the same index and, when a reply cites `[n]`, the
reply ends with a `Sources:` block naming the exact files and lines (F4, wired
in the daemon chat path).

### Part 6. Pair the phone (2:45)

The phone leg is a dev-run path this release: there is no distributed app
bundle, so it runs through Expo on the host. If Expo tooling is not already
on the host, this is the step most likely to slip the budget; run it, and if
it cannot finish in the window, skip it with a friction row and a reason.

1. On the host: `cd apps/mobile && npm install && npx expo start`, and on
   the phone install Expo Go and scan the terminal QR (Android), or open the
   app in a simulator.
2. Open the Steward app, pass the three onboarding slides, land on Pair.
3. On the Pair screen enter the host's LAN IP (the one the wizard printed)
   and the 6 digit code from Part 3, or scan the QR shown by the daemon.
   Camera permission must be granted; manual entry always works.

What to see: the phone holds a signed session token in the OS secure store,
the host reports the pair in `/health` or the Pair screen, and the same
conversation from Part 4 appears on the phone. Send one short chat from the
phone; the reply streams on both surfaces.

### Part 7. Reboot check, comes back warm (2:00)

```sh
systemctl reboot
```

After the machine is back up and you are logged in:

```sh
systemctl --user is-active steward      # expect active
curl -s http://localhost:3877/health    # ok true, served_model qwen3:8b
```

Then open `http://localhost:3877` again. What to see, the definition of
"warm":

- No reinstall and no re-run of `steward setup`.
- The webapp loads immediately and the Conversation list from Part 4 is
  still there (messages persist in SQLite; the pairing key survives restart,
  so the phone does not need to re-pair).
- One short chat answers. The first token after a reboot can take up to the
  measured cold-load time (qwen3:8b on U1 measured 38.1 s cold first token
  in the architecture doc), then it streams at the measured 43 to 44 tok/s.
  A slow first reply right after reboot is expected and is not a defect;
  record the actual seconds.

If the service is not active or the webapp does not load, that is a
show-stopper row (fix-now).

### Part 8. Uninstall (1:00)

Restore the machine to the Part 0 state and remove all data.

```sh
systemctl --user disable --now steward
sudo apt remove steward                # Route A
# Route B instead: rm -f ~/.local/bin/steward
rm -rf ~/.steward                      # or your STEWARD_HOME
```

On the phone, clear the Steward app, which removes the stored session token
from the secure store.

Then verify the fresh state again:

```sh
test ! -e /usr/bin/steward && echo "binary removed"
test ! -d ~/.steward && echo "data removed"
ss -ltn | grep 3877 || echo "port 3877 free"
```

What to see: all three good lines. Nothing else was installed anywhere on
the system (see docs/PRIVACY.md for the complete delete contract).

### Part 9. Verdict and friction log

Fill the table below, mark every non-passing row fix-now or post-v1, and
either close the gate here or hand back the fix-now list.

## Part 8. Friction log (fills during the run)

Severity legend:
- **blocker** could not continue past the step
- **high** worked only with a manual workaround
- **medium** confusing, wrong, or slow enough to matter
- **low** cosmetic
- **none** indistinguishable from intended

| # | Step | What happened (expected vs actual) | Seconds | Severity | fix-now / post-v1 | Follow-up card |
| --- | --- | --- | --- | --- | --- | --- |
| F1 | 1, one-line install | Measured 2026-10-26: every anonymous URL (raw script, release asset, release API) returns 404 because the repo is private. The README one-liner therefore fails on any machine with no gitea token. Worked only with authenticated download. A real buyer on a fresh machine cannot install. | seen | high | fix-now | required before v1 launch |
| F2 | 1, install snippet | README and docs/QUICKSTART.md name `steward_1.0.0_amd64.deb`, but this release built only `steward_1.0.0_arm64.deb` (U1 is arm64; no amd64 artifact exists). The snippet would fail or 404 here. | seen | medium | fix-now | required (correct filename, or build amd64 too) |
| F3 | whole script, Windows box | Windows leg of this gate is not runnable this release: no Windows binary exists, winget manifest is a template with placeholder checksums, NOT MEASURED. DONE for the gate needs only one machine, so U1 stands. | n/a | high (for the promise) | post-v1 | optional, only if a Windows ship is tracked now |
| F4 | 5, chat retrieval | Wired in the integrate card: a chat turn grounds in the retrieval index and a reply that cites `[n]` ends with a `Sources:` block resolving the exact files and lines (fail-closed; no index leaves a plain assistant turn). Upstream a chat citation is proven measured on the merged tree. | seen | low | fixed-now | recommended |
| 1 | 0 | | | | | |
| 2 | 1 | | | | | |
| 3 | 2 | | | | | |
| 4 | 3 | | | | | |
| 5 | 4 | | | | | |
| 6 | 5 | | | | | |
| 7 | 6 | | | | | |
| 8 | 7 | | | | | |
| 9 | 8 | | | | | |

F1 to F4 are pre-seeded from real measurement or verifiable repo facts so
you do not have to rediscover them. Add rows 1 to 9 for how each part
actually went; leave unrun parts as "skipped" with a reason.

## Honest limits (NOT MEASURED)

- Windows and x86_64 Linux installs: NOT MEASURED, no artifacts exist this
  release. Only linux/arm64 was built.
- On-device mobile run: NOT MEASURED on a rig; device behavior is documented
  in apps/mobile/README.md and run manually here.
- A live model download inside the wizard: NOT MEASURED in this gate because
  qwen3:8b is already cached on U1; if you run the wizard on a machine
  without the model, record the real download time.
- The 15-minute line on a cold-download machine: NOT MEASURED, the budget
  assumes cached models on the measured leg.
- vLLM, subscription offers, and anything from the no-go list: out of scope
  for this gate, unchanged.

---

# Part 9. Machine run inside the private environment (agent executed, 2026-10-27)

Don will not publish the repo until the market and customer plan exist, so the
machine-executable parts of this gate were run by the agent against the private
release and the source tree on U1. Everything below is measured, not asserted.
What remains for the human pass is judgement: how it feels, the phone on a real
handset, and the Windows and macOS legs that were never built.

## 9.1 Results

| Part | What ran | Measured | Verdict |
| --- | --- | --- | --- |
| 0 | release assets re-downloaded from gitea with a token | binary sha256 `ff8489c2...` and deb `e3b24f55...` both match SHA256SUMS | pass |
| 1 | install via the authenticated one-liner (`STEWARD_GITEA_TOKEN`) into a clean prefix | 15 s including the 48 MB download and checksum | pass after F1 fix |
| 2 | `steward doctor`, `steward check` | correct machine, verdict full, 121.6 GB usable, 346.5 GB free, bundle extreme | pass |
| 3 | `steward setup --model qwen3:8b --source <notes>` | 16 s end to end with a cached model; self-test chat and a real note summary | pass |
| 4 | pairing | code issued, `POST /v1/pair` returned an 88-char token, every protected route refused it before pairing (401) | pass |
| 5 | chat streaming after the latency fix | first delta 0.084 s, turn 1.5 s (was 17.0 s to first token, 23.7 s per turn) | pass after F7 fix |
| 6 | retrieval | 2 files indexed in 0.09 s, search returned the right file with chunk ordinal and offsets | pass |
| 7 | brief | `steward brief` produced a real brief from the indexed notes | pass after F9 fix |
| 7b | tools | library present with approval records, but the chat path never calls the tool loop | **fail, F8** |
| 8 | license | mint for this machine, activate, status; a tampered copy (tier raised to power) refused with exit 1 | pass |
| 8b | OTA | status reports the embedded catalog (9 models) after F10; the check resolved the released default with `STEWARD_OTA_URL` unset | pass (fixed after 1.2.0) |
| 8c | diagnostics | bundle written with members bundle.json, system.txt, versions, redacted config, log tail | pass |
| 8d | service | systemd user unit failed with the 1.0.0 binary because `serve --port` was rejected (F5); re-verified after the 1.0.1 rebuild | pass after F5 fix |
| 9 | uninstall | prefix removal leaves the data root; documented as intended | pass |

## 9.2 Friction log (machine run)

| # | Where | What happened | Severity | Status |
| --- | --- | --- | --- | --- |
| F1 | install one-liner | every anonymous URL 404s on a private repo, so a fresh machine cannot install | high | **fixed in 1.0.1** (`STEWARD_GITEA_TOKEN`, explicit hint) |
| F2 | install docs | README and QUICKSTART named an amd64 deb this release never built | medium | **fixed in 1.0.1** (arch-aware) |
| F3 | machine legs | no Windows or macOS binary exists; winget and brew manifests are templates | high for the promise | post-v1 (needs an amd64/Windows build host) |
| F4 | chat vs retrieval | retrieval answers live in the Files pane; chat replies do not cite them | low | **fixed in 1.3.0** (chat grounding in the daemon chat path, Sources block appended) |
| F5 | `steward serve` | `--port`, `--model`, `--source`, `--host`, `--ollama-url` rejected as unrecognized, which also broke any unit file passing flags | high | **fixed in 1.0.1** |
| F6 | `setup --keep-daemon` | printed that the daemon was left running while the in-process thread died with setup | medium | **fixed in 1.0.1** (detached handoff verified by /health) |
| F7 | chat latency | first visible token at 17.0 s because the model's hidden reasoning streamed before any content; turn 23.7 s | critical for the first-run impression | **fixed in 1.0.1** (`think` off by default, `keep_alive` 30m; now 0.084 s first token, 1.5 s turn) |
| F8 | tools | `run_loop` exists with approvals, allowlists and sandbox tests, but no daemon route or chat path invokes it, so tool use is unreachable in the product | high | **fixed in 1.3.0** (the daemon chat path runs the tool loop and streams tool_call, approval_required and tool_result; the web client and the phone render the steps and approve in place) |
| F9 | briefs | a brief could only appear on a schedule; nothing let a person ask for one | medium | **fixed in 1.0.1** (`steward brief`, `POST /v1/briefs/generate`) |
| F10 | `ota status` | reported "models: 0" while the embedded catalog holds 9 | low | **fixed in 1.0.1** |
| F11 | `/health` | reported daemon version 0.2.0 while the package was 1.0.0 | low | **fixed in 1.0.1** (reads the package version) |

## 9.3 What only Don can do

1. Judge the feel: setup screen pacing, webapp at 1280x800 and 390x844, whether
   the brief reads like something you would trust.
2. Pair a real phone against a real daemon, with the app from `npx expo start`.
3. Decide whether F8 (tools reachable from chat) is fix-now or a 1.1 headline,
   since it changes the chat contract and the approval UX.
4. Say whether the Windows leg matters before the market plan, which decides
   whether we spend a day on a Windows build host next.

## 9.4 Frozen 1.0.1 re-verification (release id 27)

Everything above was re-run against the rebuilt artifacts, not the source tree.

| Check | Result |
| --- | --- |
| Clean install from the v1.0.1 release with a token | 7 s, checksum verified, `steward 1.0.1` |
| `steward serve --help` in the frozen binary | lists `--host --port --model --source --ollama-url` |
| systemd user unit, cold start | 522 ms to `/health` 200 |
| systemd user unit, service restart | 537 ms to `/health` 200, model still resident |
| `/health` version string | `1.0.1` (was `0.2.0` on the old binary) |
| first chat token, frozen binary | 0.076 s and 0.111 s across two turns, 1.2 s per turn |
| deb install in `debian:stable-slim` | installs, `steward 1.0.1`, unit file present |
| deb removal | clean: binary gone after `dpkg -r` (F12 fixed, md5sums were missing) |
| release assets | 6: binary, binary sha256, deb, deb sha256, install script, SHA256SUMS |

F12 was found here and fixed in the same pass: the `dpkg-deb` fallback built the
package without `DEBIAN/md5sums`, so `dpkg -r` left `/usr/bin/steward` behind on
a machine. The Makefile now generates the manifest and normalises the unit file
mode to 0644.

Remaining open after this run: **F3** (no Windows or macOS binary). F8 and F4
are now closed: the F8 wave made tools reachable from chat (daemon tool loop)
and the integrate card wired chat auto-citation (a reply that cites `[n]`
ends with a `Sources:` block). The
OTA check source-resolution gap is closed (fixed after 1.2.0): `steward ota
check` now resolves the released default exactly like the daemon, so a stock
install needs no manifest and no `STEWARD_OTA_URL`. The released default is
still a private gitea URL, so an unauthenticated fetch returns 404; making it
publicly reachable is a hosting decision, separate from source resolution.
