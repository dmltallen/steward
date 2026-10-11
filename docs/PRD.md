# Steward Product Requirements (PRD)

**Version:** 1.0, 2026-10-10 (initial)

## Vision

An always-on personal AI assistant that runs entirely on the user's own
hardware, requires no technical setup, costs a one-time license instead of a
$3,499 box or a monthly subscription, and ships the ownership and privacy
story Ghost Core sells without the hardware markup.

## Personas

1. **The box buyer (primary).** People who already bought an AI-capable
   machine: a GB10 twin, a Strix Halo mini PC, a 24 GB used RTX card, a Mac
   Studio. They spent $600-6,000 on hardware and expect the software to be the
   product, quickly. They value ownership, privacy, and zero recurring fees.
2. **The already-on server owner (secondary).** Home-lab people running a NAS
   or mini PC 24/7 who want a personal AI on the box that is already humming.
3. **The curious power user (tertiary).** Developers who want local models but
   are tired of hand-installing runtimes and juggling quantization.

## Jobs to be done

- Turn the user's existing hardware into a personal AI in under 15 minutes,
  no terminal.
- Know the user's calendar, files, and devices; act proactively and privately.
- Never send life data to a third party, and prove it.
- Let the user own the assistant forever for a one-time license, buying only
  optional major feature packs.

## Hard requirements (v1)

- R1. Detection: identify serving memory class and model family with no user
  input, with a human-readable `doctor` result.
- R2. Auto-selection: pick an always-hot assistant plus a capability model
  (plus vision where the pool allows) using the memory ladder in
  MODEL-SELECTOR.md; never require the user to choose a model.
- R3. Speed floor: never auto-select a capability model whose optimistic
  tokens/sec cannot clear 15 tok/s on the detected hardware class.
- R4. No-setup install: signed installer, silent daemon, QR-code phone pairing,
  guided 2-minute self-test that demonstrates the first wow.
- R5. Clients: native phone app and web app, LAN zero-config and encrypted
  off-LAN access via the R1A mesh tunnel.
- R6. Ownership: perpetual license gate; bought features keep working without
  an internet connection or a subscription.
- R7. OTA: model catalog refresh and re-selection on hardware or OS changes;
  no user action.

## Non-goals (v1)

- Selling or designing hardware. Steward runs on certified existing hardware.
- A full credential firewall (rides a later paid feature pack; v1 ships a
  default-on sandbox for model network access).
- Smart-home integration and browser agent (paid feature packs).
- Windows/macOS/iOS App Store distribution in v1 (installer first, stores
  later), per the certified hardware list that masks OS variance.

## Success metrics

- Install to first wow: under 15 minutes for 80% of certified hardware.
- Acceptance: at least 5% conversion from free to the $79/$149 perpetual
  license within 60 days (hypothesis; measure before locking price).
- Zero model downloads past the fitting budget: selector never over-claims
  residency (the 80B-must-fit-24GB class of error is forbidden).
- Diagnostics: `doctor` correctly classifies the top certified devices in CI.

## Risks (tracked)

- Support load on hardware we did not design (mitigated by the certified list).
- Apple/OS policy for on-device agents shaping the phone client.
- Commoditization by NVIDIA, Apple, and Perplexity; speed and polish win, not
  breadth.
- One-time pricing underfunding OTA serving; the optional upgrade plan is the
  honest mitigation and is never a gate on bought features.
