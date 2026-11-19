"""The feature registry and the single feature gate.

One `enabled(feature_id, license_state, config)` call answers the question
everywhere in the product. The rules, in order of strength:

1. A feature the user bought is never gated off by config, by a lower tier,
   by clock skew, or by a failed pack lookup. "Bought" means granted by the
   license's signed tier or by a signed pack it carries.
2. Unknown feature ids fail closed (return False). A feature added to a
   signed license before this binary knows it grants nothing until the
   binary ships the registry entry.
3. A malformed or tampered license is resolved to None (the free tier) by
   the license layer before it reaches this gate, so a bad file never
   crashes the daemon and never grants.

Position of the gate in the stack: features.py sits on top of license.py.
license.active_license() is the only place a License is produced; this module
reads the verified result and never re-verifies or reads files itself.

What config may do (and only this): the free, default-on built-ins can be
switched OFF by an explicit config opt-out ("disabled_features" list). A
license-granted feature ignores config entirely, and config can never turn a
paid feature on.

Registry entries carry:
    id          stable feature id, also minted into licenses and packs
    name        human name shown in Settings
    min_tier    lowest tier whose signed grant includes the feature
    is_pack     True when granted by a held pack, never by a tier
    default_on  on at its floor without any paid grant

The pack ids below mirror the announced pack shelf (docs/PRICING.md). They
are entitlement contracts for features that have not shipped. A minted
license may hold them (status and Settings show them) but no consumer grants
anything until a future release builds the feature and calls enabled() here.
That is the seam the rest of this module was built for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from . import license as _lic


@dataclass(frozen=True)
class FeatureSpec:
    """One row of the registry: the entitlement contract for a feature."""

    id: str
    name: str
    min_tier: str = "free"
    is_pack: bool = False
    default_on: bool = False


_TIER_ORDER = _lic.TIER_ORDER

FEATURES: dict[str, FeatureSpec] = {
    # Free, default-on built-ins. These are the promise that makes the free
    # tier worth installing (docs/PUBLIC-LAUNCH-PLAN.md section 6).
    "chat": FeatureSpec("chat", "Chat assistant", min_tier="free", default_on=True),
    "retrieval": FeatureSpec("retrieval", "Local retrieval", min_tier="free", default_on=True),
    "daily_brief": FeatureSpec("daily_brief", "Daily brief", min_tier="free", default_on=True),
    # Paid tier features (license.py TIER_FEATURES is the grant source).
    "unlimited_retrieval_roots": FeatureSpec(
        "unlimited_retrieval_roots", "Unlimited retrieval roots", min_tier="personal"),
    "scheduled_jobs": FeatureSpec(
        "scheduled_jobs", "Scheduled jobs beyond the first", min_tier="personal"),
    "tool_exec": FeatureSpec(
        "tool_exec", "Tool execution", min_tier="personal"),
    "multi_device_pairing": FeatureSpec(
        "multi_device_pairing", "Multi-device pairing", min_tier="home"),
    # Paid feature packs. Registered now so future releases only build the
    # feature; the entitlement machinery is this seam.
    "multi_box_mesh": FeatureSpec("multi_box_mesh", "Multi-box mesh", is_pack=True),
    "multi_user": FeatureSpec("multi_user", "Multi-user and team controls", is_pack=True),
    "browser_agent": FeatureSpec("browser_agent", "Browser agent", is_pack=True),
    "credential_firewall": FeatureSpec(
        "credential_firewall", "Credential firewall pro", is_pack=True),
    "smart_home_connectors": FeatureSpec(
        "smart_home_connectors", "Smart-home connectors", is_pack=True),
}


def known(feature_id: str) -> bool:
    """True when the id is in the registry. Unknown ids still fail closed."""
    return feature_id in FEATURES


def is_pack(feature_id: str) -> bool:
    """True when the id is a registered pack (mint_license validates with this)."""
    spec = FEATURES.get(feature_id)
    return bool(spec is not None and spec.is_pack)


def _tier_grants(license_: _lic.License, min_tier: str, feature_id: str) -> bool:
    """Does the license's signed tier and feature list grant this feature?

    Both must agree: the signed tier must sit at or above the feature's
    minimum, and the signed features list must actually contain the id. A
    weird vendor payload claiming a high tier without listing the feature
    still fails closed.
    """
    if license_.tier not in _TIER_ORDER:
        return False
    floor = _TIER_ORDER.get(min_tier, _TIER_ORDER["free"])
    if _TIER_ORDER[license_.tier] < floor:
        return False
    return feature_id in license_.features


def _config_disables(config: Any, feature_id: str) -> bool:
    """The one config lever: an explicit opt-out of a default-on feature.

    Only consulted for features that are NOT license-granted. A config value
    can never turn a bought feature off and never turns anything on.
    """
    if config is None:
        return False
    value = None
    if isinstance(config, Mapping):
        value = config.get("disabled_features")
    else:
        value = getattr(config, "disabled_features", None)
    if isinstance(value, (list, tuple, set, frozenset)):
        return feature_id in value
    return False


def enabled(feature_id: str, license_state: _lic.License | None,
            config: Any = None) -> bool:
    """The single gate. One call answers 'keep this on or off' everywhere.

    license_state is the verified active license, or None for the free tier.
    config is the runtime configuration (may be a steward.config.Config or
    any mapping; it is only read for the default-on opt-out above).
    """
    spec = FEATURES.get(feature_id)
    if spec is None:
        return False  # unknown ids fail closed

    if spec.is_pack:
        # A pack is granted by the signed license itself. No config, no tier,
        # no clock (for a perpetual pack) can take it away. A failed pack
        # lookup only affects THIS pack feature, never the tier grant.
        return bool(license_state is not None
                    and license_state.has_pack(feature_id))

    if license_state is not None and _tier_grants(license_state, spec.min_tier, feature_id):
        return True  # bought by tier: never gated off by config

    if spec.default_on:
        return not _config_disables(config, feature_id)

    return False


def describe(license_state: _lic.License | None, config: Any = None) -> dict:
    """Honest full picture for the Settings UI.

    Reports the active tier, every pack the license holds (with expiry and
    whether it is currently active), and each known feature with its state
    and its grant source. Nothing here guesses: a feature that is not on is
    reported as off with its grant.
    """
    lic = license_state
    tier = lic.tier if lic is not None else "free"
    packs_held = [{
        "id": p.id, "version": p.version,
        "perpetual": p.is_perpetual, "expires": p.expires,
        "active": p.is_active(_lic.today_iso()),
    } for p in (lic.packs if lic is not None else ())]

    features = []
    for fid in sorted(FEATURES):
        spec = FEATURES[fid]
        features.append({
            "id": fid,
            "name": spec.name,
            "is_pack": spec.is_pack,
            "min_tier": spec.min_tier,
            "enabled": enabled(fid, lic, config),
            "grant": _grant(fid, lic),
        })

    return {
        "tier": tier,
        "license": ({
            "id": lic.id, "perpetual": lic.is_perpetual,
            "expires": lic.expires,
        } if lic is not None else None),
        "packs_held": packs_held,
        "features": features,
    }


def _grant(feature_id: str, lic: _lic.License | None) -> str:
    """Where an on feature gets its grant from: one of
    tier, pack, default, none."""
    spec = FEATURES.get(feature_id)
    if spec is None:
        return "none"
    if spec.is_pack:
        return "pack" if (lic is not None and lic.has_pack(feature_id)) else "none"
    if lic is not None and _tier_grants(lic, spec.min_tier, feature_id):
        return "tier"
    if spec.default_on:
        return "default"
    return "none"
