# Steward Roadmap

**Version:** 1.0, 2026-10-10 (initial). Dates are targets, not commitments.

## v0.1 Prototype: DONE (Oct 10 2026)

- Hardware detection (`steward doctor`) verified live on U1 GB10: 121.6 GB
  unified, class extreme.
- Auto model selection (`steward plan`): 7 unit tests green across the whole
  memory ladder; 24 GB lands on 27B (Ghost tier), 128 GB on 80B MoE, 192 GB on
  109B MoE.

## v0.5 Minimal product (next)

- Signed installer for Windows x86 and Linux (Debian/Ubuntu) on Class A and B.
- Daemon that downloads and serves the selected bundle (serving backend
  decided by benchmark, see ARCHITECTURE open item).
- Phone + web clients over zero-config LAN pairing (QR).
- License gate (free tier functional; perpetual license issuance).
- Self-test that demonstrates the first wow (calendar or file action).
- CI on gitea: selector tests + detect tests in `.gitea/workflows/pytest.yaml`.

## v1.0 Launch

- Class C power tier (GB10 twins, Strix Halo, Mac unified pools).
- OTA model catalog refresh and re-selection.
- Optional upgrade plan and first paid feature pack (browser agent).
- App Store submission for the phone client (assessed; installer ships first).

## v1.5

- Mesh off-LAN access via R1A tunnel.
- Credential firewall pro (process-isolated outbound policing).
- Multi-box mesh and multi-user.

## Done means

- Install to first wow under 15 minutes on 80% of certified hardware.
- Acceptance: >= 5% free to perpetual conversion within 60 days of v1.0.
- Selector never over-claims residency (tested in CI).
- gitea is the source of truth; every release tagged with artifacts.
