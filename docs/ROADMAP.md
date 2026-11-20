# Steward Roadmap

**Version:** 1.3, 2026-11-19 (v1.0 contents finalized, what moved to 1.1 is
explicit, and v1.3.0 released: tools in chat, citations with structured
sources, offline feature packs, one-command release tool, CI matrix,
install-channel-aware updates, OTA hardening, public-repo bootstrap)

## v0.1 Prototype: DONE (Oct 10 2026)

- Hardware detection (`steward doctor`) verified live on U1 GB10: 121.6 GB
  unified, class extreme.
- Auto model selection (`steward plan`): 7 unit tests green across the whole
  memory ladder; 24 GB lands on 27B (Ghost tier), 128 GB on 80B MoE, 192 GB on
  109B MoE. Selection still covers this today via the same selector.

## v0.5 Minimal product: DONE (T2 and the Oct 26 sprint series)

- Daemon that serves the selected bundle via Ollama and a local API on a
  fixed port (T2, then the full route table in I1 and I2).
- First-wow self-test: the daemon summarizes a real local folder through
  the local model (docs/DEMO.md).
- Phone client chat wired end to end (T2, expanded by S6).
- Signed Linux installer: one-file binary, Debian package, checksum-verified
  curl installer (S4).
- Zero-config LAN pairing (QR and 6 digit code) for the phone and web
  clients (S2, S5, S6).
- Web client served by the daemon (S5).
- License gate: free tier functional and perpetual licenses for personal,
  home and power tiers (S11).
- CI on gitea: selector, then the full suite in `.gitea/workflows/pytest.yaml`.

## v1.0 Launch: this release

What v1.0 contains, each with its commit in docs/CHANGELOG.md:

- Computed install-time verdict (no-fit, lite, full) that replaces any
  certified hardware list, plus `steward check` and the compatibility
  guide (S3).
- The full daemon route table with streaming chat, pairing auth,
  conversations, briefs, approvals and diagnostics (S2, S5, S8, S9, S12,
  I1, I2).
- Local retrieval with citations on hits (S7), a facts store, briefs and
  a scheduler (S8).
- Approved tool connectors with a containment sandbox and a human approval
  store (S9).
- The web client (Chat, Files, Brief, Settings, Pair) mounted at the
  daemon root (S5) and the phone app with real pairing, streaming chat and
  an offline cache (S6).
- A resumable first-run wizard from detection to a paired phone (S10).
- Ed25519-signed perpetual licenses and a signed OTA model catalog (S11).
- Linux packaging: one-file binary, Debian package, curl installer (S4).
- The ship-facing docs: README, quickstart, privacy, compatibility,
  changelog, landing page (S13).

Class C power tier (GB10 twins, Strix Halo, Mac unified pools) is served
by the same software; only the model selection differs, and the one
measured machine is the U1 GB10 at 121.6 GB.

## Moved to 1.1 (not in v1.0)

Deliberately not shipped in v1.0, tracked for the next release:

- Dedicated-tower vLLM backend on a halo-class pool that does not co-tenant
  (benchmarked 2026-10-26 in bench/BENCHMARK-2026-10-26.md, behind the
  same runtime seam).
- Browser agent feature pack (optional paid pack).
- App Store submission for the phone client (installer ships first).
- Windows and macOS binaries and their store manifests (templates exist,
  NOT MEASURED).
- Mesh off-LAN access via the R1A tunnel.
- Credential firewall pro (process-isolated outbound policing).
- Multi-box mesh and multi-user.
- Encryption at rest for the local data stores (design goal; see
  docs/PRIVACY.md).

## Done means

- Install to first wow under 15 minutes on 80% of machines in the guided
  quickstart path.
- Acceptance: >= 5% free to perpetual conversion within 60 days of v1.0
  (the A/B run in docs/PRICING.md).
- Selector and verdict never over-claim residency (tested in CI).
- gitea is the source of truth; every release tagged with artifacts.
