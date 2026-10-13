# Steward Roadmap

**Version:** 1.1, 2026-10-13 (T2: daemon + mobile chat + first-look demo done on U1)

## v0.1 Prototype: DONE (Oct 10 2026)

- Hardware detection (`steward doctor`) verified live on U1 GB10: 121.6 GB
  unified, class extreme.
- Auto model selection (`steward plan`): 7 unit tests green across the whole
  memory ladder; 24 GB lands on 27B (Ghost tier), 128 GB on 80B MoE, 192 GB on
  109B MoE.

## v0.5 Minimal product (next)

- DONE (T2 Oct 13): daemon that serves the selected bundle via Ollama and a
  local API (/health, /bundle, /chat, demo routes) on a fixed port.
- DONE (T2 Oct 13): first-wow self-test: daemon summarizes a real local
  calendar/notes folder through the local model (docs/DEMO.md).
- DONE (T2 Oct 13): phone client chat wired end to end (apps/mobile daemon.ts
  hits the real /chat; manual device run steps in docs/DEMO.md).
- Pending: signed installer for Windows x86 and Linux (Debian/Ubuntu), Class A and B.
- Pending: zero-config LAN pairing (QR) for the phone client.
- Pending: web client (site/ static page exists; live daemon binding not yet).
- Pending: license gate (free tier functional; perpetual license issuance).
- CI on gitea: selector tests in `.gitea/workflows/pytest.yaml` (running).

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
