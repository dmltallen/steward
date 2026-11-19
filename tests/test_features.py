"""Tests for the feature registry and the single gate (steward/features.py),
plus its one wired consumer: the scheduler's scheduled-briefs gate.

The gate contract under test:
  - a bought feature (tier grant or held pack) is never gated off by config,
    by a lower tier, by clock skew, or by a failed pack check;
  - unknown feature ids fail closed;
  - a malformed or tampered license resolves to the free tier and never
    crashes the daemon (exercised through the scheduler consumer);
  - v1 licenses with no packs behave exactly as before.

skips when the cryptography package is absent, like test_license.py.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from steward import features as feat  # noqa: E402
from steward import license as lic  # noqa: E402
from steward.scheduler import JobStore, Scheduler  # noqa: E402


@pytest.fixture
def keypair():
    pem, pub = lic.generate_keypair()
    return {"pub": pub, "priv": lic.private_bytes_from_pem(pem)}


@pytest.fixture
def vendor(monkeypatch, keypair):
    monkeypatch.setattr(lic, "VENDOR_PUBLIC_KEY_HEX", keypair["pub"])
    return keypair


def mint(payload, keypair) -> dict:
    return lic.make_envelope(payload, keypair["priv"])


@pytest.fixture
def machine(monkeypatch):
    monkeypatch.setattr(lic, "machine_fingerprint",
                        lambda sources=None: "sha256:testmachine")
    return "sha256:testmachine"


def payload(machine="sha256:testmachine", tier="personal", **over) -> dict:
    base = {
        "id": "STW-TEST000000001",
        "tier": tier,
        "machine": machine,
        "issued": "2026-10-26",
        "features": sorted(lic.tier_features(tier)),
    }
    base.update(over)
    return base


def write_active(tmp_path, doc, home: Path) -> Path:
    target = lic.license_path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# The gate: fail-closed and grant rules
# ---------------------------------------------------------------------------


def test_unknown_feature_id_fails_closed(vendor):
    assert feat.enabled("no_such_feature", None) is False
    lic_state = lic.verify_document(mint(payload(), vendor),
                                    fingerprint="sha256:testmachine")
    assert feat.enabled("no_such_feature", lic_state) is False
    assert feat.known("no_such_feature") is False


def test_free_tier_defaults_on_and_paid_features_off():
    assert feat.enabled("chat", None) is True
    assert feat.enabled("retrieval", None) is True
    assert feat.enabled("daily_brief", None) is True
    assert feat.enabled("unlimited_retrieval_roots", None) is False
    assert feat.enabled("scheduled_jobs", None) is False
    assert feat.enabled("tool_exec", None) is False
    assert feat.enabled("multi_device_pairing", None) is False
    assert feat.enabled("browser_agent", None) is False


def test_personal_license_grants_paid_tier_features(vendor, machine):
    lic_state = lic.verify_document(mint(payload(tier="personal"), vendor),
                                    fingerprint=machine)
    for fid in ("unlimited_retrieval_roots", "scheduled_jobs", "tool_exec"):
        assert feat.enabled(fid, lic_state) is True
    assert feat.enabled("multi_device_pairing", lic_state) is False


def test_bought_feature_survives_broken_config(vendor, machine):
    """The core promise: config can never take a bought feature away."""
    lic_state = lic.verify_document(mint(payload(tier="personal"), vendor),
                                    fingerprint=machine)
    hostile = {
        "disabled_features": [
            "tool_exec", "scheduled_jobs", "unlimited_retrieval_roots", "chat"],
        "features": [],
        "paid": False,
        "tier": "free",
    }
    assert feat.enabled("tool_exec", lic_state, hostile) is True
    assert feat.enabled("scheduled_jobs", lic_state, hostile) is True
    assert feat.enabled("chat", lic_state, hostile) is True
    # A config that is not a mapping at all (None, a string) changes nothing.
    assert feat.enabled("tool_exec", lic_state, "not-a-config") is True
    assert feat.enabled("tool_exec", lic_state, {}) is True


def test_config_cannot_enable_unbought_feature(vendor, machine, monkeypatch):
    home = machine
    current = lic.verify_document(mint(payload(tier="free"), vendor),
                                  fingerprint=home)
    greedy = {"features": ["tool_exec"], "paid": True, "allow_shell": True}
    assert feat.enabled("tool_exec", current, greedy) is False
    assert feat.enabled("scheduled_jobs", current, greedy) is False


def test_config_can_disable_default_on_only():
    # The one config lever: an explicit opt-out of a free default-on feature.
    conf = {"disabled_features": ["chat"]}
    assert feat.enabled("chat", None, conf) is False
    assert feat.enabled("retrieval", None, conf) is True
    assert feat.enabled("daily_brief", None, conf) is True
    # A bought (personal tier) chat cannot be config-disabled either.
    # Covered by test_bought_feature_survives_broken_config.


def test_pack_grants_pack_feature_and_is_never_config_gated(vendor, machine):
    doc = mint(payload(tier="personal",
                       packs=[{"id": "browser_agent", "version": 1}]),
               vendor)
    lic_state = lic.verify_document(doc, fingerprint=machine)
    hostile = {"disabled_features": ["browser_agent"]}
    assert feat.enabled("browser_agent", lic_state, hostile) is True
    assert feat.enabled("browser_agent", lic_state, {}) is True
    # Not held: off no matter the config.
    assert feat.enabled("smart_home_connectors", lic_state, hostile) is False
    assert feat.enabled("browser_agent", None) is False


def test_pack_is_additive_to_a_tier(vendor, machine):
    """A Personal license plus a pack keeps both grants: pack add-on is not a
    replacement and does not need a higher tier to fire."""
    doc = mint(payload(tier="personal",
                       packs=[{"id": "multi_user", "version": 2}]),
               vendor)
    lic_state = lic.verify_document(doc, fingerprint=machine)
    assert feat.enabled("tool_exec", lic_state) is True       # tier grant
    assert feat.enabled("multi_user", lic_state) is True      # pack grant
    assert feat.enabled("browser_agent", lic_state) is False  # not held


def test_expired_pack_refuses_but_tier_grant_survives(vendor, machine):
    doc = mint(payload(tier="personal", packs=[
        {"id": "multi_user", "version": 1, "expires": "2020-01-01"}]),
               vendor)
    lic_state = lic.verify_document(doc, fingerprint=machine)
    assert lic_state.has_pack("multi_user") is False          # expired
    assert feat.enabled("multi_user", lic_state) is False
    # The failed pack check does not take the bought tier features away.
    assert feat.enabled("tool_exec", lic_state) is True
    assert feat.enabled("scheduled_jobs", lic_state) is True


def test_multiple_packs_all_held(vendor, machine):
    doc = mint(payload(tier="home", packs=[
        {"id": "browser_agent", "version": 1},
        {"id": "multi_box_mesh", "version": 3},
        {"id": "credential_firewall", "version": 1, "expires": "2099-01-01"},
    ]), vendor)
    lic_state = lic.verify_document(doc, fingerprint=machine)
    for fid in ("browser_agent", "multi_box_mesh", "credential_firewall"):
        assert feat.enabled(fid, lic_state) is True
    assert feat.enabled("smart_home_connectors", lic_state) is False


def test_registry_declares_announced_packs():
    for fid in ("multi_box_mesh", "multi_user", "browser_agent",
                "credential_firewall", "smart_home_connectors"):
        assert feat.is_pack(fid) is True
    assert feat.is_pack("tool_exec") is False
    assert feat.is_pack("no_such_pack") is False


# ---------------------------------------------------------------------------
# describe(): the honest Settings picture
# ---------------------------------------------------------------------------


def test_describe_no_license_is_honest_free_tier():
    d = feat.describe(None)
    assert d["tier"] == "free"
    assert d["license"] is None
    assert d["packs_held"] == []
    by_id = {f["id"]: f for f in d["features"]}
    assert by_id["chat"]["enabled"] is True
    assert by_id["chat"]["grant"] == "default"
    assert by_id["tool_exec"]["enabled"] is False
    assert by_id["tool_exec"]["grant"] == "none"
    assert by_id["browser_agent"]["enabled"] is False


def test_describe_shows_tier_packs_and_grants(vendor, machine):
    doc = mint(payload(tier="personal", packs=[
        {"id": "browser_agent", "version": 1},
        {"id": "multi_user", "version": 2, "expires": "2020-01-01"},
    ]), vendor)
    lic_state = lic.verify_document(doc, fingerprint=machine)
    d = feat.describe(lic_state)
    assert d["tier"] == "personal"
    assert d["license"]["id"] == "STW-TEST000000001"
    held = {p["id"]: p for p in d["packs_held"]}
    assert set(held) == {"browser_agent", "multi_user"}
    assert held["browser_agent"]["active"] is True
    assert held["multi_user"]["active"] is False      # expired: reported honestly
    by_id = {f["id"]: f for f in d["features"]}
    assert by_id["tool_exec"]["enabled"] is True and by_id["tool_exec"]["grant"] == "tier"
    assert by_id["browser_agent"]["enabled"] is True and by_id["browser_agent"]["grant"] == "pack"
    assert by_id["multi_user"]["enabled"] is False and by_id["multi_user"]["grant"] == "none"
    assert by_id["chat"]["grant"] == "tier"           # tier grant wins over default


# ---------------------------------------------------------------------------
# The one wired consumer: scheduler scheduled briefs beyond the first
# ---------------------------------------------------------------------------

T_MON = datetime(2026, 10, 26, 7, 0).timestamp()


def make_jobs():
    return [
        {"name": "brief", "schedule": "daily:07:00"},
        {"name": "extra", "schedule": "daily:07:01"},
    ]


def build_scheduler(tmp_path, now_ts, calls, jobs=None, store=None):
    store = store or JobStore(db_path=tmp_path / "db" / "scheduler.db",
                              jobs_path=tmp_path / "jobs.json",
                              now=lambda: now_ts)
    scheduler = Scheduler(store=store, now=lambda: now_ts,
                          jobs=jobs or make_jobs(),
                          runner=lambda j, n: (
                              calls.append(j["name"]), {"detail": "ok"})[1])
    names = [j["name"] for j in (jobs or make_jobs())]
    with store._connect() as conn:  # make every job due exactly now
        conn.execute("UPDATE jobs SET next_run=? WHERE name IN (%s)"
                     % ",".join("?" * len(names)), [now_ts] + names)
    return scheduler


def test_scheduler_no_license_runs_everything_legacy(tmp_path):
    calls = []
    scheduler = build_scheduler(tmp_path, T_MON, calls)
    summary = scheduler.tick()
    assert summary["ran"] == 2
    assert summary["skipped"] == 0
    assert sorted(calls) == ["brief", "extra"]


def test_scheduler_free_license_caps_at_one_brief(tmp_path, vendor, machine):
    home = tmp_path
    write_active(tmp_path, mint(payload(tier="free"), vendor), home)
    calls = []
    scheduler = build_scheduler(tmp_path, T_MON, calls)
    summary = scheduler.tick()
    assert summary["ran"] == 1
    assert summary["skipped"] == 1
    assert calls == ["brief"]                     # second is the skipped one
    hist = scheduler.store.history("extra")
    assert hist and hist[0]["status"] == "skipped"
    assert "entitlement required" in hist[0]["detail"]


def test_scheduler_personal_license_runs_both(tmp_path, vendor, machine):
    write_active(tmp_path, mint(payload(tier="personal"), vendor), tmp_path)
    calls = []
    scheduler = build_scheduler(tmp_path, T_MON, calls)
    summary = scheduler.tick()
    assert summary["ran"] == 2
    assert summary["skipped"] == 0
    assert sorted(calls) == ["brief", "extra"]


def test_scheduler_tampered_license_fails_closed(tmp_path, vendor, machine):
    target = write_active(tmp_path, mint(payload(tier="personal"), vendor),
                          tmp_path)
    tampered = json.loads(target.read_text(encoding="utf-8"))
    tampered["payload"]["features"].append("multi_device_pairing")
    target.write_text(json.dumps(tampered), encoding="utf-8")
    assert lic.active_license(tmp_path) is None   # signature broke
    calls = []
    scheduler = build_scheduler(tmp_path, T_MON, calls)
    summary = scheduler.tick()                     # never raises
    assert summary["ran"] == 1
    assert summary["skipped"] == 1
    assert calls == ["brief"]


def test_scheduler_malformed_license_fails_closed(tmp_path):
    target = lic.license_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{ definitely not json", encoding="utf-8")
    calls = []
    scheduler = build_scheduler(tmp_path, T_MON, calls)   # never raises
    summary = scheduler.tick()
    assert summary["ran"] == 1
    assert summary["skipped"] == 1
    assert calls == ["brief"]


def test_scheduler_entitlements_flag_true_enforces_without_license(tmp_path):
    # Strict mode: no license at all still caps at the free allowance.
    calls = []
    store = JobStore(db_path=tmp_path / "db" / "scheduler.db",
                     jobs_path=tmp_path / "jobs.json", now=lambda: T_MON)
    scheduler = Scheduler(store=store, now=lambda: T_MON,
                          jobs=make_jobs(), entitlements=True,
                          runner=lambda j, n: (
                              calls.append(j["name"]), {"detail": "ok"})[1])
    with store._connect() as conn:
        conn.execute("UPDATE jobs SET next_run=? WHERE name IN (?, ?)",
                     [T_MON, "brief", "extra"])
    summary = scheduler.tick()
    assert summary["ran"] == 1
    assert summary["skipped"] == 1
    assert calls == ["brief"]


def test_scheduler_entitlements_flag_false_disables_enforcement(tmp_path, vendor, machine):
    write_active(tmp_path, mint(payload(tier="free"), vendor), tmp_path)
    calls = []
    store = JobStore(db_path=tmp_path / "db" / "scheduler.db",
                     jobs_path=tmp_path / "jobs.json", now=lambda: T_MON)
    scheduler = Scheduler(store=store, now=lambda: T_MON,
                          jobs=make_jobs(), entitlements=False,
                          runner=lambda j, n: (
                              calls.append(j["name"]), {"detail": "ok"})[1])
    with store._connect() as conn:
        conn.execute("UPDATE jobs SET next_run=? WHERE name IN (?, ?)",
                     [T_MON, "brief", "extra"])
    summary = scheduler.tick()
    assert summary["ran"] == 2
    assert sorted(calls) == ["brief", "extra"]
