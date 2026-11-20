# Steward landing page (site/)

Self-hosted static landing page. No build step, no dependencies. Open
`index.html` directly in a browser, or serve the folder with any static
server (example: `python3 -m http.server 8080` from this directory).

## Structure

- `index.html` the page, all copy
- `style.css` calm palette and type
- `app.js` variant switching plus the event-logging stub

The page carries the product facts that matter to a visitor as on-page copy,
kept in step with the repo docs: the computed compatibility verdict and the
16/40 GB thresholds (docs/COMPATIBILITY.md), the nothing-leaves-the-box
privacy claims (docs/PRIVACY.md), and the "Linux arm64 today" platform
statement (docs/BUILD-MATRIX.md). If any of those docs changes a number or a
claim, update this page in the same change. The page and the README agree
byte for byte on the financial claim: free to install and use, nothing more.

## No price sheet

The price sheet stays private until the A/B test reports. This page
intentionally carries no price, no tier name and no call to act on a sale;
only the free-to-install sentence and the perpetual-license sentence appear.
Re-enabling the price card later is a copy restore with the exact elements
`app.js` already expects (`#price-amount`, `.price-tab`, `.price-card`,
`.price-cta`, `.test-note`, `.price-switch`). No code change is needed when
the shelf is unlocked.

## A/B gate

The gate mechanism is untouched and lives in `app.js`: `v=a` and `v=b` are
the two price anchors, `window._stewardVariant` is set from `v=` on load, and
the `?ab=show` query key reveals the demo tab switcher so external traffic
can never see or switch the other variant. With the price content absent the
gate is inert, which is the intended state until the test starts.

## Event hooks

Every interaction is buffered into `window.__stewardEvents`. To transmit,
set `window.STEWARD_EVENTS_ENDPOINT` before the script loads and the logger
POSTs newline-delimited JSON there. The email capture is a stub: it validates
and logs only, no backend, nothing leaves the page until an endpoint is
configured.
