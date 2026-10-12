# Steward landing page (site/)

Self-hosted static landing page. No build step, no dependencies. Open
`index.html` directly in a browser, or serve the folder with any static
server (example: `python3 -m http.server 8080` from this directory).

## Structure

- `index.html` the page (all copy, minus the two price strings)
- `style.css` calm palette and type
- `app.js` variant switching plus the event-logging stub

## A/B pricing variants

The page serves two pricing-test variants that differ only in the price:

- `index.html?v=a` -> $79 anchor (Class B: 16-24 GB GPU owners)
- `index.html?v=b` -> $149 anchor (Class C: 48-128 GB workstations)

`window._stewardVariant` is set from `v=` on load and `app.js` swaps the price
string and fires a `page_view` event. The tab switcher on the page is a demo
control: it is hidden for real visitors and only shown when `?ab=show` is in the
URL, so external traffic can never see or switch to the other variant. Gate
`?ab=show` stays a build-time key; it is not advertised anywhere.

## Event hooks

Every interaction is buffered into `window.__stewardEvents`. To transmit,
set `window.STEWARD_EVENTS_ENDPOINT` before the script loads and the logger
POSTs newline-delimited JSON there. See docs/PRICING.md for the event schema
and funnel. The email capture is a stub: it validates and logs only, no
backend, nothing leaves the page until an endpoint is configured.
