# Security Policy

Steward is an always-on local assistant: a daemon on your own machine, a web
app in the browser and a phone app, all on the same LAN. This page states
what is in scope for security reports, how to report privately, how long a
response takes, and how updates are verified. It stays factual; nothing here
promises more than the code delivers.

## Supported versions

Only the latest released version is supported for security fixes. A fix is
shipped in the next release.

Older releases are not patched individually. Updates are free, never require
a subscription, and are delivered by the built-in updater, so the supported
position is simple: run the latest release and you get the fixes. If you
cannot update, treat the older version as unsupported.

## Reporting a vulnerability

Report security issues privately through GitHub Security Advisories. The
repository must have private vulnerability reporting enabled (the setting
named "Report a vulnerability" on the Security tab), which is what lets a
reporter file a private advisory that is not public until the maintainers
acknowledge and disclose it.

Do not open a public issue for a suspected vulnerability. Public posts about
security issues may be closed without comment so that users who have not yet
updated are not put at risk.

What a good report contains:

- What is affected, including the Steward version (`steward version`) and
  the operating system and architecture.
- Steps to reproduce, from a known starting state; proof-of-concept code or
  a crafted input is ideal.
- What you expected and what actually happened, plus the security impact you
  think it has.
- The diagnostics bundle from `steward doctor --bundle /tmp/bundle.zip`.
  The bundle is redacted before it is written to disk: session tokens, API
  keys, bearer values and secret-shaped values are scrubbed, home paths and
  hostnames are hashed, and no license blobs are included. Open it yourself
  before sending it if you want to confirm.

### Response window

- Acknowledgment: within 3 business days.
- Initial assessment and coordinated disclosure plan: within 7 calendar
  days.

We prefer coordinated disclosure and will normally give you a change window
so the fix can ship before the report becomes public.

## Scope

In scope for security reports:

- The daemon, including its HTTP server, its LAN pairing flow and session
  authentication, and the persistence it relies on.
- The update path: manifest verification, payload download and checksum
  checks, the atomic swap and rollback.
- The tool approval flow and the file and network sandbox for tool
  execution.
- License and feature entitlement verification.

Not in scope:

- The model files themselves and their contents. Models are third-party
  open weights; a model producing surprising output is a property of the
  model, not a Steward bug.
- The local backend runtime (for example Ollama) it is wired to. An issue in
  the backend belongs in that project; report it upstream, and state the
  backend version in your Steward report.
- Vulnerabilities in the phone or web app that only matter when the attacker
  already has physical access to the host, or already holds a valid session
  token.
- An attacker with physical access to the machine that runs Steward, or
  read access to its data root. The data root is protected by the OS
  account, not by Steward.

## How updates are verified

Every Steward release publishes a signed update manifest. The manifest is a
JSON document that carries the release version, a model catalog generation,
and per-platform payload entries, each with a sha256 checksum and size. It
is wrapped in the same Ed25519 envelope that signs licenses, verified
against a vendor public key that is embedded in the Steward binary.

What the signature protects against:

- A tampered or forged manifest at the update source. A manifest whose
  signature, shape or checksums do not verify is refused, and nothing is
  staged or installed.
- A tampered or swapped payload during download. The payload sha256 is
  inside the signed manifest, so a payload that does not match is refused
  before it is staged.
- An update source that is not Steward. Spoofing the manifest requires the
  vendor private key, which never leaves the signing machine and never
  appears in the repository.

The honest limit: the check runs in code on a machine the user controls. A
user who can patch the Steward binary can remove the verification. This is
provenance, not DRM, and it does not stop a determined local attacker; it
protects everyone else from a compromised, spoofed or tampered update
channel. The same caveat applies to license verification.
