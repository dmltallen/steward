# Steward Web App

The in-box client for Steward: no install, no build step, no CDN, no external
fonts, no network beyond the daemon that serves it. The daemon mounts this
package at the root (one dispatch line in `src/steward/server/daemon.py`:

    if webapp_serve.dispatch(self, method, parts, path):
        return

Files:

    index.html    the single-page app shell (all five screens)
    app.js        client logic: router, streaming chat, markdown-lite renderer,
                  QR pairing code, settings, files search
    style.css     design tokens and styles (light and dark via OS preference,
                  system font stack, mobile first)
    serve.py      the mounting hook: static assets plus a thin Bearer-gated API

## Screens

- Chat: streamed replies render token by token (SSE), markdown-lite with
  strong escaping, conversation list, and live approval prompts with Approve
  and Deny buttons.
- Files: index status (documents, chunks, on-disk size), add and remove
  folders, build index, and search results with citations (source path, char
  offset, score).
- Brief: today's brief plus history. History and the scheduled brief come
  from /v1/briefs (the S8 memory card); until that route exists the screen
  offers "Generate now" through /v1/summarize.
- Settings: model, backend mode, data dir, license state, telemetry shown as
  permanently off, an Updates card (app and daemon versions, "Check for
  updates", auto-check toggle), and a pairing QR that carries the discovery
  URL. The 6 digit pairing code is only ever served to the host itself.
- Pair: enter the 6 digit code to trade it for a 30 day session token. The
  token lives in memory and localStorage; every gated request sends it as a
  Bearer header and a 401 returns here.

## Mounted routes owned by the webapp

    GET  / , /index.html , /app.js , /style.css   static shell (no auth)
    GET  /v1/webapp/info          settings data for the Settings screen
    GET  /v1/files                index status and configured folders
    POST /v1/files/folders        add a folder (persisted to config.json)
    DELETE /v1/files/folders      remove a folder (?path=...)
    POST /v1/files/index          build the embedding index
    POST /v1/files/search         {query, k} -> hits with citations

Every API route above is Bearer-gated like the rest of the daemon. The data
root is derived from the daemon's SQLite path (`<root>/db/steward.db`), so
pointing the daemon at a throwaway home also isolates the client.

## Build and run

The client has no build step. Run the daemon as documented in
`src/steward/server/README.md`, then open `http://<host>:<port>/`.

## Testing and evidence

`tests/test_webapp_routes.py` spins the real daemon with a fake runtime and
asserts route statuses and content types, that no asset references an
external URL (no `http://` or `https://` anywhere in index.html, app.js or
style.css), that the markdown-lite renderer (run in Node) escapes a
`<script>` message so it renders inert, and that the webapp API is
Bearer-gated.

`evidence/webapp-*.png` are headless Chrome renders of each screen at 1280x800
and 390x844, captured against a running daemon with a fake runtime. The
embedding index used by the Files screen was built from three real markdown
documents through the real local Ollama embedder, so the cited search
results are genuine.

Known capture note: on this host, the headless screenshot pipeline currently
appends a solid ~44 px strip at the bottom of every capture (the artifact
reproduces on plain single-color test pages under google-chrome,
chromium-headless-shell and many flag sets, so it is a host renderer quirk,
not the app). Live rendering is verified correct: in the browser the chat
composer is flush with the viewport bottom, every screen lays out cleanly,
streaming renders incrementally ("" then a partial reply then the full
reply as SSE deltas arrive), and the QR encoder was verified 4 for 4 by
rendering its matrices and decoding them back with zbarimg.
