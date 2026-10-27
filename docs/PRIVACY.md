# Steward Privacy

The short version: Steward keeps everything on your machine. Your chats,
your files, your memory store, your settings and your logs live under one
folder on the computer you install it on. Nothing is sent to a server you
do not run, there is no account, and there is no telemetry.

This document states exactly what is stored, where, what is never sent,
the two outbound calls the software can make, and how to delete everything.

## What is stored, and where

Everything lives under a single data root, `~/.steward` by default. You
can move it at install time or later by setting the environment variable
`STEWARD_HOME` to a directory of your choice. The root and every
subdirectory are created owner-only (mode 0700), so other users on the
same machine cannot read them.

| Location | What is stored |
| --- | --- |
| `<root>/config.json` | Settings: the selected model, backend URL, data paths, retrieval folders. No secrets. |
| `<root>/db/steward.db` | Your conversations and their messages. |
| `<root>/db/` (memory store) | Saved facts and generated briefs. The machine-readable memory the assistant builds from your files and chats. |
| `<root>/db/` (approval store) | Pending and decided tool-approval requests, with the arguments of each request. |
| `<root>/index/` | The local vector index built from the folders you ask Steward to read. Embeddings and source references stay here. |
| `<root>/models/` | Model files downloaded for the serving backend, plus the signed update manifest. |
| `<root>/licenses/` | Your perpetual license entitlement file (`active.json`, mode 0600). |
| `<root>/logs/` | Daemon and runtime logs, rotated at 5 MB. Logs are scrubbed of tokens and keys when a support bundle is built. |

The pairing secret and the signed session tokens that let your phone and
browser talk to the daemon live in the same data root (`pairing.key` next
to the database) and on the paired devices, never in a cloud.

Attachments and media are not copied anywhere: Steward reads files in
place from the folders you point it at and stores only the index and the
facts it extracts.

## What is never sent anywhere

- Your chat messages, questions and replies.
- Your files or document contents (only the local index built from them
  exists on the same machine).
- Your extracted facts, briefs, or approvals.
- Your settings, your hardware profile, or your model choices.
- Any usage, analytics, crash, or telemetry event. The web and mobile
  settings screens show telemetry as off, permanently. There is no
  telemetry code path to turn on.

The web client is a single page served by the daemon on your machine. It
makes zero external requests: no fonts, no scripts, no tracking pixels
from anywhere.

The phone app talks only to the host you pair it with over your local
network. Pairing requires a 6 digit code that only the host machine can
read, and the session token is scoped to that pairing.

## The only outbound calls

Steward can make exactly two kinds of outbound call, and only when you let
it:

1. **Model download.** When the first-run wizard needs a model that is not
   already on the machine, it downloads it from the source in the signed
   manifest. The file is checked against its sha256 hash before it is used,
   and the download is the only time model weights are fetched.
2. **Update checks.** Steward can check a signed update catalog for new
   model generations and refresh its model list. The update targets a
   server you choose or the default catalog address; the catalog is
   signature-checked before anything is applied.

That is the complete list. There is no phone-home beacon, no license
server check after purchase, no market research call, no "improve the
product" traffic. Your bought features keep working with no network at
all.

## How to delete everything

- Delete the data root: `rm -rf ~/.steward` (or your `STEWARD_HOME`
  directory if you moved it). This removes conversations, facts, briefs,
  approvals, the index, models, licenses, and logs.
- Remove the software itself with your package manager: on Debian or
  Ubuntu `sudo apt remove steward`, or delete the `steward` binary in
  `~/.local/bin` if you used the curl installer.
- On each paired phone, clear the app from the device, which removes the
  stored session token from the secure store.
- Nothing else is created anywhere else on the system or on any server.

## Honest limits

- The databases holding your conversations and facts are plain SQLite
  files on your own disk. They are protected by owner-only permissions,
  not encrypted. Encryption at rest is a stated design goal that has not
  shipped yet in this version. If the machine is lost or stolen, the data
  on the disk can be read by whoever has physical access.
- The daemon listens on the local network so your phone can reach it.
  Every route is gated by the pairing token, but any device on the same
  network that holds a valid token can talk to it. Do not expose the
  daemon port to the public internet.
- Optionally reachable from off your home network through an encrypted
  tunnel is a later capability, not part of this release. Nothing about
  the current version routes traffic through a third party.
