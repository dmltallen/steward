# Steward Security Posture

One page for a reviewer. Steward is a local daemon (web app and phone app
clients on the same LAN) that stores everything on the host machine and
makes only two kinds of outbound calls: the initial model download and
signed update checks. Below, every trust boundary, what protects it, and the
residual risk it carries. Nothing here is marketing; each line reflects the
code in `src/steward/` (see the module docstrings, which are the source).

## 1. Pairing and session auth

What it is: a phone or other client on the LAN pairs with the daemon once
and then holds a session token for every protected request.

What protects it: pairing uses a fresh random 6-digit code from the OS
CSPRNG. The code is printed at startup and served only on loopback
(`GET /pair/code`), so it cannot be scraped from the LAN. The code is
exchanged via `POST /v1/pair {code, device}` for a stateless HMAC-SHA256
token (`sv1.<exp>.<device>.<nonce>.<sig>`, 30 day TTL) signed with a
per-install key persisted at `<data>/db/pairing.key`, mode 0600, so tokens
survive restarts. Code comparison and signature checks are constant-time.
Failed pairing is rate limited per IP: 5 failures per 5 minutes refuses
until the oldest failure ages out. Device label inside the token is
identification only, never a secret.

Residual risk: the code is only 6 digits, so protect the pairing window the
same way you protect the console that printed the code. Anyone who can
perform the pairing flow during a window on your LAN obtains a valid token;
tokens are tied to the LAN, not to TLS identity. The token's device label is
self-declared (an attacker who already has a token can echo another label).

## 2. LAN exposure of the daemon

What it is: the daemon binds 0.0.0.0 by default (port 3877) so a phone on
the LAN can reach it. mDNS advertises `steward-<port>._steward._tcp.local`
with version, port, model and paired count.

What protects it: everything except `GET /health` and `GET /v1/bundle` is
gated behind a valid Bearer session token; the pairing code endpoint and
the first-run wizard routes are loopback-only by design (section 1).
Setting `STEWARD_HOST=127.0.0.1` makes the daemon loopback-only. There is no
account and no WAN exposure by design; the update source is the only server
the daemon talks to.

Residual risk: the LAN listener is plain HTTP, not TLS, so a same-LAN
attacker can read traffic; the token is the protection, which is why tokens
are long, random and stateless. On an untrusted network, set
`STEWARD_HOST=127.0.0.1` or isolate the host. Anyone with physical access to
the host or its data root already owns the data regardless.

## 3. Tool approvals and sandbox

What it is: when the model wants to use a tool (files, HTTP, shell or
scheduled work), the write/exec/net paths do not run until a human approves
them, and the actions are confined to configured roots.

What protects it: a mutating tool call creates a pending row in a SQLite
approval store (`<data>/db/approvals.db`, WAL) and blocks up to a timeout
for a human decision; there is no path that runs without approval, and an
expired or missing decision auto-denies. Every creation and decision is
appended to an audit log with a canonical args hash. Paths are checked with
realpath containment (symlinks followed) against configured fs roots, and
sensitive material (`.ssh`, `.env`, private keys, and the daemon's own
pairing key and approvals store) is refused by name regardless of location.
Network fetches are https only to an exact allowlisted host, with no
credentials in the URL.

Residual risk: the sandbox is policy, not OS isolation. There is no
seccomp, chroot, or capability drop around tool execution; the containment
is the path and URL checks. A user who approves an exec request authorizes
exactly the command the model asked to run, and Steward honors that approval
as far as policy allows. A bug in the path or URL checks is a sandbox escape
up to the privileges of the daemon process.

## 4. Update verification (OTA)

What it is: updates come as a JSON manifest (catalog generation plus
per-platform app payloads, each with sha256 and size) signed with the vendor
Ed25519 key; the daemon and the CLI use the same verification code.

What protects it: the manifest is verified against a vendor public key that
is embedded in the Steward binary. A manifest that fails signature, shape,
or checksum checks is refused before anything is staged or installed. Staged
payloads are re-hashed against the signed manifest, the swap commits with a
single atomic pointer rename, and a health probe of the new bundle runs
before the swap is accepted; on failure the previous catalog and binary are
restored. An unreachable or refused update source never stops the daemon
from starting; it records a status instead. The update source is
`STEWARD_OTA_URL` if set, else the released default, resolved identically by
CLI and daemon.

Residual risk: the verification runs in code the user controls. A user who
can patch the Steward binary can disable the check; this is provenance, not
DRM or attestation, and it does not stop a determined local attacker who has
write access to the executable. It protects everyone else against a
compromised, spoofed or tampered update channel. Code-signing identities
(Windows and macOS) are not yet held; see docs/PUBLIC-LAUNCH-PLAN.md M3.

## 5. License and feature entitlement verification

What it is: a perpetual offline license is a JSON envelope signed with the
same vendor Ed25519 key, carrying tier, machine fingerprint, issued date and
features; feature gating lives only in the license module.

What protects it: the envelope is verified (signature against the embedded
vendor key, then machine fingerprint against hardware identity, then
expiry) before it is stored or used. The active license is re-verified from
disk on every check. A tampered, wrong-machine, or expired file is refused
and the free tier applies; the feature gate never consults config, so config
alone cannot enable a paid feature.

Residual risk: the same honest limit as updates. This is an offline
honor-model entitlement, not DRM: a determined user who can patch the
binary can bypass the gate. Nobody credible ships stronger for offline
licensing, and Steward prices around that fact (see docs/PRICING.md).

## Summary of residual risk

Steward's protections assume the threat model on the box is the honest
majority and the threat model on the network is malicious. Its residual
exposures are: the 6-digit pairing code and unauthenticated LAN traffic, the
policy-only tool sandbox, and the fundamental fact that local verification
code can be removed by a determined local user. None of these are hidden;
the relevant source is small, auditable, and documented in the module
docstrings that back this page.
