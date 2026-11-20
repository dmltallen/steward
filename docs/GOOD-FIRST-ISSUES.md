# Good first issues

These are not open issues yet. They are seeded candidates, written with the
problem, the scope, and the acceptance criteria a maintainer can check
against. To work one, open an issue from the feature template, reference this
document, and start on a branch. The maintainer will add the `good first
issue` label when the issue is confirmed.

The house rules in CONTRIBUTING.md apply to all of them: DCO sign-off on
every commit, English only, no em dashes anywhere in copy, and never a
fabricated number or result.

## Candidate 1: house style linter test

Problem: the project's rules say user-facing copy is English only and never
contains an em dash, and it is a pull-request checklist item. There is no
automated check, so the rule depends on review attention and is easy to miss.

Scope: a pytest test in `tests/` that walks the markdown files (README.md,
docs/, CONTRIBUTING.md, CODE_OF_CONDUCT.md, .github/) and fails on either an
em dash character (U+2014) or a clearly non-English word. Keep the
non-English detection scoped and boring: a small allowlist for names and
command tokens, not a model.

Acceptance criteria:

- The test passes on the current tree with zero false positives.
- It fails on a committed file that contains an em dash.
- It is fast (under 2 seconds) and needs no new third-party dependency.

## Candidate 2: optional landing page event receiver

Problem: the landing page (site/app.js) buffers interaction events into
`window.__stewardEvents` and only transmits them if
`window.STEWARD_EVENTS_ENDPOINT` is set before the script loads. Nothing in
the repo implements a receiver, so self-hosters who want the funnel have to
build one themselves.

Scope: a small, self-contained receiver that accepts newline-delimited JSON
POSTs from the landing page and appends them to a log file or SQLite table.
It must be opt-in (only run when configured), add no third-party runtime
dependency, and never contact any outside service. The event schema in the
landing page README is the contract.

Acceptance criteria:

- Starting the receiver and loading the landing page with
  `STEWARD_EVENTS_ENDPOINT` set produces an entry per event.
- Without the endpoint set, nothing is sent and nothing is written.
- The receiver is documented in docs/ with a two-line run example, and it
  ships with a test.

## Candidate 3: manifest checksum verification test

Problem: the winget and Homebrew manifests in packaging/ exist, but their
checksums and URLs are placeholders because no Windows or macOS binary has
been built yet. Nothing proves those placeholders can be spotted quickly
when a real build lands.

Scope: a test that parses the manifests, extracts the declared download URL
and hash for each, and fails with a clear message when the URL is not a real
published asset or the hash is a placeholder. The test documents the current
state as known-unbuilt and is a tripwire for the first real build.

Acceptance criteria:

- The test passes on the current tree and states plainly that every entry is
  a placeholder.
- It fails when a placeholder is replaced by a value that does not match a
  published asset in the repository.
- No network call is made by the test itself.
