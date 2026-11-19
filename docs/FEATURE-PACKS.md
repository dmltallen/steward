# Feature Packs and the Single Feature Gate

**Version:** 1.0, 2026-11-19

This is the paid-feature seam: the entitlement model that lets future
releases ship optional, one-time feature packs without rewriting the license
machinery. The seam is built now; no paid feature is built in this document.
Pricing and the shelf live in docs/PRICING.md, not here.

## The entitlement model

Business rule (Don, binding): free to install and use, a perpetual license to
keep using, paid ONE-TIME feature packs for optional major features. Never a
subscription, never a gate on a feature already bought.

Mechanically the model extends the existing offline Ed25519 license envelope
(src/steward/license.py). A license payload may now carry an optional
"signed packs" list:

    {
      "id": "STW-...", "tier": "personal", "machine": "sha256:<hex>",
      "issued": "YYYY-MM-DD",
      "features": ["chat", "retrieval", ...],
      "packs": [
        {"id": "browser_agent", "version": 1},
        {"id": "multi_user", "version": 2, "expires": "YYYY-MM-DD"}
      ]
    }

Each pack has:

- `id`: a registered feature id from the registry in src/steward/features.py.
- `version`: the pack revision the customer bought (vendor data, signed).
- `expires`: exact ISO date, or absent for a perpetual pack.

A pack is additive: it sits on top of whatever tier the license grants, and a
license may carry several packs. Everything about a pack is inside the same
signed payload, so a tampered pack breaks the envelope signature exactly like
a tampered feature list.

License layers:

- src/steward/license.py owns the signed model and verification: parsing,
  validation, `License.has_pack()`, and the packs reported by
  `license status`. A malformed or tampered license resolves to the free
  tier and never crashes the daemon.
- src/steward/features.py owns the registry and the one gate: what feature
  ids exist, which tier each needs, which are packs, and the single
  `enabled()` call that answers "keep this on or off".

## The one-gate rule

Every call site in the product answers one question with one function:

    features.enabled("<feature_id>", lic.active_license(root), config)

The rules, in order of strength:

1. A feature the user bought is never gated off by config, by a lower tier,
   by clock skew, or by a failed pack lookup. "Bought" means granted by the
   license's signed tier or by a signed pack it carries.
2. Unknown feature ids fail closed: a feature added to a signed license
   before this build knows it grants nothing until the build ships the
   registry entry.
3. A malformed or tampered license drops to the free tier and never crashes
   the daemon.

What config may do (and only this): an explicit "disabled_features" opt-out
can switch off the free, default-on built-ins. Config can never turn a paid
feature on and never turns a bought feature off.

The gate pattern is documented as a one-line comment at the one wired
consumer (see below); every future call site copies it.

## What a pack author must do

1. Register the pack in the registry (src/steward/features.py): a stable
   feature id, a human name, `is_pack=True`, `min_tier` set to the base any
   buyer must hold, `default_on=False`. Until this entry exists, a minted
   license holding the pack still stores it (status and Settings show it)
   but the gate grants nothing.
2. At every place the new capability is consumed, call
   `features.enabled("<pack_id>", lic.active_license(root), config)` and
   gate on the result. Prefer gating at the one narrow point the capability
   is exercised, not at every UI toggle.
3. Mint the license offline with the vendor key:

       tools/mint_license.py mint --tier free --local \
           --pack <pack_id> --pack-version 1 --out pack.json

   Pack ids not in the registry are refused at mint time, so a vendor cannot
   mint a pack the current build cannot honor. Use --pack-expires only for
   the rare explicit time-boxed copy; a paid one-time pack is perpetual.
   Bump --pack-version for each new revision you sell.

4. Remember the shelf: price the pack as a one-time add-on, revisit per
   release, and never gate a feature that was free in a previous release.

## The one wired consumer

The seam is proven in exactly one existing consumer: scheduled briefs beyond
the first (src/steward/scheduler.py). `Scheduler.tick()` calls
`features.enabled("scheduled_jobs", ...)` through the same root the jobs live
under, and a free-tier allowance of one scheduled brief is enforced:

- no license file ever activated: legacy behavior is preserved, whatever
  jobs.json holds runs (this is what keeps every pre-existing scheduler
  test and every fresh install unchanged);
- a free-tier license active: exactly one scheduled brief runs, further jobs
  are skipped with a recorded "entitlement required: scheduled_jobs" reason;
- a tier license active: all scheduled jobs run;
- a tampered or malformed license on disk: the free tier applies (one brief),
  never a crash.

That last point is the fail-closed proof: an invalid license file costs the
customer nothing but the one brief, and takes nothing away from the daemon.

## The honest limitation: offline licenses are patchable

An offline-verified license in a shipped binary can be patched by a
determined user. The verification public key is embedded in the binary, the
signature check runs locally on the customer's machine, and a determined
user who controls that machine can dump the binary, find the check, and
patch it out (or swap the embedded public key) so the product trusts a
self-signed license. No offline scheme is unforgeable against a determined
patcher; licensing vendors say so themselves. Steward prices around the
honest majority and keeps the free tier genuinely worth using, so the
incentive to patch stays small. This is named here so nobody reads the
Ed25519 envelope as DRM.

## Backward compatibility

A v1 license file with no "packs" key parses with zero packs and behaves
exactly as before. Every pre-existing test stays green unmodified; the
scheduler runs everything it always ran until a license file appears.
