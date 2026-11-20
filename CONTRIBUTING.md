# Contributing to Steward

Thanks for considering a contribution. This project uses the Developer
Certificate of Origin (DCO), not a CLA, and asks for issue reports that a
maintainer can actually run.

## Developer Certificate of Origin

Every contributor certifies, with a sign-off line on each commit, that the
contribution may be submitted under the project license (MIT). The full
text:

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am permitted
    to submit under a different license), as indicated in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

### The sign-off line

Append a sign-off line to every commit message:

```
Signed-off-by: Your Full Name <you@example.com>
```

The line is your certification of the DCO terms. How it is checked: every
pushed commit must end with a matching `Signed-off-by:` line naming the
author. A commit without it will be rejected or bounced back with a request
to amend or rebase. Use `git commit -s` to add it automatically. There is no
CLA and no additional contributor agreement.

## Filing a bug report

A report that a maintainer can reproduce and diagnose in minutes contains:

- Steps to reproduce, numbered, from a known state.
- Expected behavior and actual behavior.
- `steward version`, the operating system, and the architecture.
- The output of `steward doctor --bundle /tmp/steward-bundle.zip`, attached.

The bundle is your evidence, and it is safe to share. It is redacted before
it is written: session tokens (`sv1.*`), `Bearer` values, `sk-*` API keys
and secret-shaped key/value assignments are replaced with `[REDACTED]`,
home paths and hostnames are hashed by default, and no license blobs or keys
are ever included. You are welcome to open the zip before attaching it and
confirm nothing you consider private survived. If a detail of the bundle is
part of the bug, say which member of the bundle matters.

## Running the tests

The suite lives in `tests/` and runs with pytest. From a checkout:

```
uv run --with pytest --with numpy --with zeroconf --with cryptography \
    python -m pytest tests -q
```

If you have the local virtualenv (used by the Makefile for builds), the
shorter form works too:

```
.venv/bin/python -m pytest tests -q
```

New code ships with its own tests. A change that touches signature
verification, licensing, or any trust boundary must prove refusal paths
(tampered input, wrong key, missing file), not just the happy path.

## Working on the repository

- Branches are isolated. Make your change on a branch, and open the pull
  request with a checklist (see the pull request template).
- One-file-one-owner: when a change is being worked on in parallel, each
  contributor owns the files they are editing, and a file has one owner at a
  time. If your work needs a file another person owns, coordinate on the
  issue or pull request first instead of editing it concurrently. This keeps
  parallel branches from colliding on the same file. The build workflow
  (`src/steward/`) is owned by the maintainers; propose workflow or release
  tooling changes explicitly rather than editing them silently.
- English only in code, docs, issues and commits. No em dashes anywhere in
  user-facing copy (a plain hyphen or a comma is the house style).
- Never fabricate a number, a hash, or a result. If a check was not run,
  say it was not run. "Measured" means the output is quoted from a real run.

## Community expectations

This project is maintained by one person, in the evenings, alongside a day
job. The honest expectation is:

- Issues are read as they arrive, but a reply or a fix can take days. If
  your report is urgent, it is a security issue (see SECURITY.md) or it is
  not actually urgent. There is no paid support and no SLA.
- The maintainer triages issues and Discussions together, usually once a
  week. Triage means labeling, merging duplicates, and saying "wontfix" with
  a reason, not promising dates.
- The fastest paths to a merged change are the `good first issue` candidates
  in docs/GOOD-FIRST-ISSUES.md and any `help wanted` label. A pull request
  that changes code ships with a test; one that changes copy carries no em
  dash and no invented claim.
- Discussions is the surface for conversation and long-form ideas; issues
  are for tracked work. The maintainer closes question issues with a pointer
  to Discussions or a doc.

## Code of conduct

Participating means agreeing to the Contributor Covenant, see
CODE_OF_CONDUCT.md. Reports are handled through the private route in
SECURITY.md.
