## What this does

One or two sentences on the change and why it exists.

## Checklist

- [ ] I ran the test suite and it passes.
      Command used (must be the real command you ran):
      `...`
      Result (quote the summary line, e.g. "N passed in Xs"):
      `...`
- [ ] No secrets, tokens, credentials, or private keys were added anywhere
      in this branch, including history.
- [ ] User-facing copy (docs, UI strings, error messages, issue templates)
      is plain and factual, contains no em dashes, and is English only.
- [ ] I state which surface I exercised and how: for example "CLI
      (steward doctor --bundle ...)", "daemon HTTP routes (paired session,
      ...)", "web app in the browser", "phone app", "update path
      (signed manifest check and apply)", or "tool approvals and sandbox".
- [ ] If this touches a trust boundary (signature verification, licensing,
      pairing or session auth, the sandbox, the update path), the change
      proves the refusal paths too: tampered input, wrong key, missing
      file, or expired state as applicable.

## DCO

- [ ] Every commit in this branch carries a `Signed-off-by:` line.

## Notes for the reviewer

Anything a reviewer should look at closely, or any deliberate omission.
