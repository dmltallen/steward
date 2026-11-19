"""Tests for the perpetual offline license (src/steward/license.py).

Every test uses a throwaway Ed25519 keypair generated in the fixture: the
tests monkeypatch the module's embedded vendor key, so they never depend on
the real vendor key nor on the actual machine fingerprint. The whole module
skips (with a reason) when the `cryptography` package is absent.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from steward import license as lic  # noqa: E402


@pytest.fixture
def keypair():
    pem, pub = lic.generate_keypair()
    return {"pub": pub, "priv": lic.private_bytes_from_pem(pem)}


@pytest.fixture
def vendor(monkeypatch, keypair):
    """Point the embedded vendor key at the test keypair for this test."""
    monkeypatch.setattr(lic, "VENDOR_PUBLIC_KEY_HEX", keypair["pub"])
    return keypair


def mint(payload, keypair) -> dict:
    return lic.make_envelope(payload, keypair["priv"])


def machine(monkeypatch, value="sha256:testmachine"):
    monkeypatch.setattr(lic, "machine_fingerprint", lambda sources=None: value)


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


def write_doc(tmp_path, doc) -> Path:
    path = tmp_path / "license.json"
    path.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
    return path


def write_active(tmp_path, doc, home: Path) -> None:
    target = lic.license_path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")


# -- verification ------------------------------------------------------------


def test_valid_perpetual_license_verifies(vendor):
    license = lic.verify_document(mint(payload(), vendor), fingerprint="sha256:testmachine")
    assert license.tier == "personal"
    assert license.id == "STW-TEST000000001"
    assert license.is_perpetual is True
    assert license.expires is None
    assert license.has_feature("tool_exec")
    assert license.has_feature("chat")


def test_future_expiry_passes_and_is_not_perpetual(vendor):
    doc = mint(payload(expires="2099-01-01"), vendor)
    license = lic.verify_document(doc, fingerprint="sha256:testmachine", now="2026-10-26")
    assert license.is_perpetual is False
    assert license.expires == "2099-01-01"


def test_expired_license_rejected(vendor):
    doc = mint(payload(expires="2020-01-01"), vendor)
    with pytest.raises(lic.ExpiredLicense):
        lic.verify_document(doc, fingerprint="sha256:testmachine", now="2026-10-26")


def test_tampered_payload_rejected(vendor):
    doc = mint(payload(tier="free", features=sorted(lic.tier_features("free"))), vendor)
    doc["payload"]["tier"] = "power"  # tamper without re-signing
    with pytest.raises(lic.SignatureError):
        lic.verify_document(doc, fingerprint="sha256:testmachine")


def test_tampered_feature_added_rejected(vendor):
    doc = mint(payload(tier="free", features=sorted(lic.tier_features("free"))), vendor)
    doc["payload"]["features"].append("tool_exec")
    with pytest.raises(lic.SignatureError):
        lic.verify_document(doc, fingerprint="sha256:testmachine")


def test_wrong_key_rejected(monkeypatch, keypair):
    # Sign with a second, different key while the embedded key is keypair
    other_pem, other_pub = lic.generate_keypair()
    other = {"pub": other_pub, "priv": lic.private_bytes_from_pem(other_pem)}
    monkeypatch.setattr(lic, "VENDOR_PUBLIC_KEY_HEX", keypair["pub"])
    doc = mint(payload(), other)
    with pytest.raises(lic.SignatureError):
        lic.verify_document(doc, fingerprint="sha256:testmachine")


def test_wrong_machine_rejected(vendor):
    doc = mint(payload(machine="sha256:someotherbox"), vendor)
    with pytest.raises(lic.MachineMismatch):
        lic.verify_document(doc, fingerprint="sha256:testmachine")


def test_malformed_envelope_rejected(vendor):
    # Payload not a JSON object.
    with pytest.raises(lic.MalformedLicense):
        lic.verify_document({"payload": "not-a-dict", "alg": "ed25519", "sig": "x"},
                            fingerprint="sha256:testmachine")
    # Missing signature.
    doc = mint(payload(), vendor)
    del doc["sig"]
    with pytest.raises(lic.MalformedLicense):
        lic.verify_document(doc, fingerprint="sha256:testmachine")
    # Wrong algorithm declared.
    doc = mint(payload(), vendor)
    doc["alg"] = "rsa"
    with pytest.raises(lic.MalformedLicense):
        lic.verify_document(doc, fingerprint="sha256:testmachine")


def test_unknown_tier_rejected(vendor):
    with pytest.raises(lic.MalformedLicense):
        lic.verify_document(mint(payload(tier="bogus"), vendor), fingerprint="sha256:testmachine")


# -- activation, status, gating ----------------------------------------------


def test_activate_stores_owner_only_and_status(tmp_path, vendor, monkeypatch):
    home = tmp_path / "home"
    machine(monkeypatch)
    sign_on = write_doc(tmp_path, mint(payload(), vendor))
    license = lic.activate(sign_on, home)
    target = lic.license_path(home)
    assert target.is_file()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert license.tier == "personal"
    st = lic.status(home)
    assert st["active"] is True
    assert st["tier"] == "personal"
    assert st["license"]["id"] == "STW-TEST000000001"
    assert "tool_exec" in st["features"]


def test_no_active_license_is_free_tier(tmp_path):
    home = tmp_path / "home"
    assert lic.active_license(home) is None
    st = lic.status(home)
    assert st["active"] is False
    assert st["tier"] == "free"
    assert set(st["features"]) == set(lic.FREE_FEATURES)
    assert "no active license" in st["reason"]


def test_free_features_work_without_license(tmp_path):
    home = tmp_path / "home"
    assert lic.enabled("chat", home) is True
    assert lic.enabled("retrieval", home) is True
    assert lic.enabled("daily_brief", home) is True
    assert lic.enabled("tool_exec", home) is False
    assert lic.enabled("unlimited_retrieval_roots", home) is False
    assert lic.enabled("scheduled_jobs", home) is False
    assert lic.enabled("multi_device_pairing", home) is False


def test_config_alone_cannot_enable_paid_features(tmp_path, vendor):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({
        "allow_shell": True,
        "features": ["tool_exec", "unlimited_retrieval_roots", "multi_device_pairing"],
        "paid": True,
        "scheduled_jobs_max": 99,
        "retrieval_dirs": ["/a", "/b", "/c"],
    }), encoding="utf-8")
    # No license at all, config screaming paid features: still the free tier.
    assert lic.enabled("tool_exec", home) is False
    assert lic.enabled("unlimited_retrieval_roots", home) is False
    assert lic.enabled("multi_device_pairing", home) is False
    assert lic.enabled("chat", home) is True
    assert lic.enabled("retrieval", home) is True
    assert lic.enabled("daily_brief", home) is True


def test_free_license_blocks_paid_features(tmp_path, vendor, monkeypatch):
    home = tmp_path / "home"
    machine(monkeypatch)
    doc = mint(payload(tier="free", features=sorted(lic.tier_features("free"))), vendor)
    lic.activate(write_doc(tmp_path, doc), home)
    assert lic.enabled("chat", home) is True
    assert lic.enabled("tool_exec", home) is False
    assert lic.enabled("unlimited_retrieval_roots", home) is False
    assert lic.enabled("multi_device_pairing", home) is False


def test_tier_grants_personal_vs_home(vendor):
    personal = lic.tier_features("personal")
    assert "tool_exec" in personal
    assert "unlimited_retrieval_roots" in personal
    assert "multi_device_pairing" not in personal
    home = lic.tier_features("home")
    assert "multi_device_pairing" in home
    assert personal < home                       # home is a strict superset
    assert home == lic.tier_features("power")    # both carry everything paid


def test_power_license_grants_everything(tmp_path, vendor, monkeypatch):
    home = tmp_path / "home"
    machine(monkeypatch)
    doc = mint(payload(tier="power", features=sorted(lic.tier_features("power"))), vendor)
    lic.activate(write_doc(tmp_path, doc), home)
    for feature in sorted(lic.ALL_FEATURES):
        assert lic.enabled(feature, home) is True


def test_stored_license_re_verified_on_read(tmp_path, vendor, monkeypatch):
    home = tmp_path / "home"
    machine(monkeypatch)
    doc = mint(payload(tier="personal"), vendor)
    lic.activate(write_doc(tmp_path, doc), home)
    assert lic.enabled("tool_exec", home) is True
    # Tamper the stored active.json (add a paid feature, no re-sign).
    stored = lic.license_path(home)
    tampered = json.loads(stored.read_text(encoding="utf-8"))
    tampered["payload"]["features"].append("multi_device_pairing")
    stored.write_text(json.dumps(tampered, sort_keys=True), encoding="utf-8")
    assert lic.active_license(home) is None          # signature broke
    assert lic.enabled("tool_exec", home) is False    # falls back to free
    st = lic.status(home)
    assert st["active"] is False
    assert "SignatureError" in st["reason"]


def test_wrong_machine_license_refused_at_activate(tmp_path, vendor, monkeypatch):
    home = tmp_path / "home"
    machine(monkeypatch, value="sha256:actualbox")
    doc = mint(payload(machine="sha256:notthisbox"), vendor)
    with pytest.raises(lic.MachineMismatch):
        lic.activate(write_doc(tmp_path, doc), home)
    assert not lic.license_path(home).exists()
    assert lic.enabled("tool_exec", home) is False


# -- machine fingerprint -----------------------------------------------------


def test_fingerprint_is_deterministic_and_order_independent():
    a = lic.machine_fingerprint(["mac:aa", "machine-id:xyz"])
    b = lic.machine_fingerprint(["machine-id:xyz", "mac:aa"])
    assert a == b
    assert a.startswith("sha256:")
    assert len(a) == 7 + 64


def test_fingerprint_requires_identity():
    with pytest.raises(lic.FingerprintError):
        lic.machine_fingerprint([])


def test_new_license_id_shape():
    from steward.license import new_license_id
    ident = new_license_id()
    assert ident.startswith("STW-")
    assert len(ident) == 4 + 12
    assert ident.replace("STW-", "").isalnum()
    # two draws differ (collision practically impossible)
    assert ident != new_license_id()


# ---------------------------------------------------------------------------
# Feature packs: the offline entitlement seam on top of the v1 envelope
# ---------------------------------------------------------------------------


def test_v1_license_without_packs_is_unchanged(tmp_path, vendor, monkeypatch):
    """A v1 license (no packs key) parses with zero packs and keeps every
    existing behaviour: this is the backward-compat contract."""
    home = tmp_path / "home"
    machine(monkeypatch)
    license = lic.verify_document(mint(payload(), vendor),
                                  fingerprint="sha256:testmachine")
    assert license.packs == ()
    assert license.has_pack("browser_agent") is False
    assert license.has_feature("tool_exec") is True
    assert license.tier == "personal"
    lic.activate(write_doc(tmp_path, mint(payload(), vendor)), home)
    st = lic.status(home)
    assert st["active"] is True
    assert st["packs"] == []          # v1 status keeps a stable empty packs list


def test_license_with_packs_parses_and_status_reports(tmp_path, vendor, monkeypatch):
    home = tmp_path / "home"
    machine(monkeypatch)
    doc = mint(payload(tier="personal", packs=[
        {"id": "browser_agent", "version": 1},
        {"id": "multi_user", "version": 2, "expires": "2099-01-01"},
    ]), vendor)
    lic.activate(write_doc(tmp_path, doc), home)
    st = lic.status(home)
    assert st["active"] is True
    assert st["tier"] == "personal"
    assert st["packs"] == [
        {"id": "browser_agent", "version": 1, "perpetual": True, "expires": None},
        {"id": "multi_user", "version": 2, "perpetual": False,
         "expires": "2099-01-01"},
    ]
    active = lic.active_license(home)
    assert active is not None
    assert active.has_pack("browser_agent") is True
    assert active.has_pack("multi_user") is True
    assert active.has_pack("smart_home_connectors") is False


def test_pack_expiry_gates_has_pack(vendor):
    lic_ok = lic.verify_document(mint(payload(packs=[
        {"id": "multi_user", "version": 1, "expires": "2099-01-01"}]),
        vendor), fingerprint="sha256:testmachine")
    assert lic_ok.has_pack("multi_user") is True
    assert lic_ok.has_pack("multi_user", today="2100-01-01") is False
    lic_expired = lic.verify_document(mint(payload(packs=[
        {"id": "multi_user", "version": 1, "expires": "2020-01-01"}]),
        vendor), fingerprint="sha256:testmachine")
    assert lic_expired.has_pack("multi_user") is False
    lic_perpetual = lic.verify_document(mint(payload(packs=[
        {"id": "multi_user", "version": 1}]),
        vendor), fingerprint="sha256:testmachine")
    assert lic_perpetual.has_pack("multi_user", today="9999-01-01") is True


def test_empty_packs_list_is_legal(vendor):
    license = lic.verify_document(mint(payload(packs=[]), vendor),
                                  fingerprint="sha256:testmachine")
    assert license.packs == ()


def test_malformed_packs_rejected(vendor):
    cases = [
        "not-a-list",                        # packs must be a list
        [{"version": 1}],                    # missing id
        [{"id": "browser_agent"}],           # missing version
        [{"id": "browser_agent", "version": "1"}],   # version not an int
        [{"id": "browser_agent", "version": 0}],     # version below 1
        [{"id": "browser_agent", "version": 1, "expires": 2026}],  # expiry not a str
    ]
    for packs in cases:
        with pytest.raises(lic.MalformedLicense):
            lic.verify_document(mint(payload(packs=packs), vendor),
                                fingerprint="sha256:testmachine")


def test_tampered_pack_rejected(vendor):
    """Adding or editing a pack without re-signing breaks the envelope."""
    doc = mint(payload(tier="free"), vendor)
    doc["payload"]["packs"] = [{"id": "browser_agent", "version": 1}]
    with pytest.raises(lic.SignatureError):
        lic.verify_document(doc, fingerprint="sha256:testmachine")
    doc = mint(payload(tier="free", packs=[{"id": "multi_user", "version": 1}]),
               vendor)
    doc["payload"]["packs"][0]["version"] = 99
    with pytest.raises(lic.SignatureError):
        lic.verify_document(doc, fingerprint="sha256:testmachine")
