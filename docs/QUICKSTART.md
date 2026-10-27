# Steward Quickstart

Ten steps from download to your first answer, with what each screen
should show. Full detail lives in the README and the other guides in
docs/. Screenshots of the actual web client are in docs/images/.

The first-run flow is the `steward setup` wizard: it detects your machine,
computes what it can run, downloads the model if needed, starts the
backend, runs a self-test, and pairs your phone. You do not choose a
model, and you do not open a terminal beyond the install and the wizard.

## 1. Download Steward

Go to the release page for your platform and grab the package, or use the
install one-liner for your OS from the README.

What you should see: the release assets (Linux `.deb` and one-file binary,
plus template manifests for Windows and macOS) and the SHA256SUMS file
next to them. Windows and macOS binaries are not built yet for this
release, so those downloads do not exist and are marked NOT MEASURED in
the packaging notes.

## 2. Install

On Debian or Ubuntu, install the package and start it as your own user
service:

```
sudo apt install ./steward_1.0.0_amd64.deb
systemctl --user enable --now steward
```

With the curl installer, a single command fetches the matching binary,
verifies its sha256 checksum, and installs it to `~/.local/bin`:

```
sh -c "$(curl -fsSL <install-script-url>)"
```

What you should see: the checksum verification pass, the install path,
and a `steward setup` prompt as the next command.

## 3. Start the setup wizard

Run `steward setup`. The wizard is a plain-text, step-by-step flow, and it
resumes from where it stopped if you interrupt it (a half-finished
download or a mid-run crash picks up again on restart).

What you should see: a welcome screen that shows the model pick (defaults
to the resident assistant for your machine, for example `qwen3:8b`) and
the home directory Steward will use.

## 4. Preflight

The wizard measures your hardware and free disk.

What you should see: a preflight state listing the usable serving memory
(for example 121.6 GB unified on a GB10), the free disk at the data
directory, and the detection path used (`nvml`, `apple_sysctl`,
`/proc/meminfo`, or `fallback`). This is where a machine far under the 16
GB floor is caught.

## 5. Verdict

The wizard states what your machine can run.

What you should see: one of three results. `full` (40 GB and up: image
understanding, longer context, split assistant on big pools), `lite`
(16 to under 40 GB: one model, no image understanding, reduced context),
or `no-fit` (under 16 GB: Steward stops here with the minimum-machine
sentence from docs/COMPATIBILITY.md). The verdict is always printed with
the measured memory number, never guessed.

## 6. Explain

The wizard gives the plain-language reasoning behind the verdict.

What you should see: why the memory class is what it is (VRAM on a
discrete card, the whole unified pool on Apple and GB10 machines, system
RAM minus 2 GB on a CPU box), the model tier that lands on your machine,
and the expected tokens per second against the 15 tok/s floor.

## 7. Download

If the model you need is not already on the machine, the wizard downloads
it. This is the only outbound call in setup.

What you should see: a progress line, the sha256 check on completion, and
cancel and resume controls. Interrupting and restarting resumes the
download from the last byte (`.part` file) instead of starting over. If
the model is already cached, this step passes through with no download.

## 8. Runtime and self-test

The wizard finds or starts the serving backend, then proves it works.

What you should see: a runtime state (for example "system-ollama at
localhost:11434" when your machine already runs Ollama), then a selftest
that sends a short chat and, if a data folder is configured, summarizes a
real document from it. You should see the reply text appear before moving
on. This is the same check the app does at boot, so the answer here means
everything downstream will work.

## 9. Pair your phone

The wizard finishes by giving you a pairing code and a QR payload.

What you should see: a 6 digit code, the pair payload in the form
`steward://pair?host=...&port=...`, and the instruction to open the phone
app and scan or type the code. The code is only ever shown on the host
machine. After pairing, the phone holds a signed session token and the
conversation list shows on both surfaces.

## 10. First answer

Open the web app in the browser at `http://localhost:3877` (the address
the wizard prints), type your first question, and send it.

What you should see: the Chat screen with a conversation list on the left,
your question in a dark bubble, and the assistant's reply streaming in
token by token. A fresh install also offers an automatic "Hello" exchange
so the first screen is never empty. The same conversation is reachable
from the paired phone. Your question and the reply are stored only in the
data directory on your machine (see docs/PRIVACY.md).

## If something is wrong

- `steward check` prints the verdict and reasons on any machine without
  running the full setup.
- `steward doctor` reports the hardware, bundle, backend mode, model and
  data directory.
- `steward doctor --bundle` writes a redacted support archive you can send
  to get help without leaking tokens, paths or keys.
