"""Tests for the install-time compatibility verdict (compat.py) and the
detection profile changes that feed it (detect.py). Deterministic: every
profile is constructed by hand, no real hardware needed.

Covers the verdict matrix required by the S3 spec: 8 GB no-GPU, 16 GB iGPU,
24 GB RTX 3090, 32 GB Apple, 64 GB Apple, 128 GB GB10, 192 GB halo; plus the
residency-safety property (no bundle whose weights exceed 0.80 of the serving
memory) and the plain-English `steward check` output.
"""

import pytest

from steward.detect import HardwareProfile
from steward.compat import verdict
from steward.selector import select_bundle, RESIDENCY_FRACTION


def _cpu(ram_gb, avail=0.0, disk=100.0, kind="cpu"):
    return HardwareProfile(os_name="linux", arch="x86_64", cpu_name="Ryzen 5",
                           gpu_kind=kind, ram_gb=ram_gb,
                           ram_available_gb=avail, disk_free_gb=disk,
                           confidence="/proc/meminfo")


def _discrete(vram_gb, disk=100.0, gpu_name="NVIDIA GeForce RTX 3090"):
    return HardwareProfile(os_name="linux", arch="x86_64", cpu_name="x",
                           gpu_kind="discrete", gpu_name=gpu_name,
                           vram_gb=vram_gb, ram_gb=64.0, ram_available_gb=40.0,
                           disk_free_gb=disk, confidence="nvml")


def _unified(unified_gb, gpu_name, disk=100.0, confidence="apple_sysctl"):
    return HardwareProfile(os_name="darwin" if confidence == "apple_sysctl" else "linux",
                           arch="arm64", cpu_name=gpu_name.lower(),
                           gpu_kind="unified", gpu_name=gpu_name,
                           unified_gb=unified_gb, ram_gb=unified_gb,
                           disk_free_gb=disk, confidence=confidence)


# The required verdict matrix: (name, profile, usable serving, expected state,
# expected tier).
MATRIX = [
    ("8gb no-gpu",       _cpu(8.0, 5.0),         6.0,  "no-fit", "entry"),
    ("16gb igpu",        _cpu(16.0, 10.0, kind="igpu_ram"), 14.0, "no-fit", "mainstream-16"),
    ("24gb rtx3090",     _discrete(24.0),        24.0, "lite",   "mid-24"),
    ("32gb apple",       _unified(32.0, "Apple M5"), 32.0, "lite", "high-32"),
    ("64gb apple",       _unified(64.0, "Apple M1 Max"), 64.0, "full", "super"),
    ("128gb gb10",       _unified(128.0, "NVIDIA GB10", confidence="nvml"), 128.0, "full", "extreme"),
    ("192gb halo",       _unified(192.0, "Gorgon Halo", confidence="nvml"), 192.0, "full", "halo"),
]


@pytest.mark.parametrize("name,profile,usable,state,tier", MATRIX,
                         ids=[m[0] for m in MATRIX])
def test_verdict_matrix(name, profile, usable, state, tier):
    v = verdict(profile, profile.disk_free_gb)
    assert v.serving_mem_gb == usable, "usable serving memory computed wrong"
    assert v.state == state, f"{name}: state, got {v.state}"
    assert v.recommended_tier == tier, f"{name}: tier, got {v.recommended_tier}"
    assert isinstance(v.reasons, list) and len(v.reasons) >= 1
    assert v.min_hardware_sentence.strip()


def test_nofit_gives_specific_sentence():
    # 8 GB no-GPU: usable is 6 GB (RAM minus 2 GB), must state the number
    # and the 16 GB floor.
    v = verdict(_cpu(8.0, 5.0), 100.0)
    assert v.state == "no-fit"
    joined = " ".join(v.reasons)
    assert "6 GB" in joined
    assert "16 GB" in joined
    assert "Minimum machine" in v.min_hardware_sentence
    assert "16 GB" in v.min_hardware_sentence


def test_lite_rules_out_vision_and_multi_model():
    v = verdict(_discrete(24.0), 100.0)
    assert v.state == "lite"
    joined = " ".join(v.reasons)
    assert "light mode" in joined
    # Vision must not ride along on a lite machine.
    b = select_bundle(_discrete(24.0), v)
    assert b.vision is False
    assert b.assistant.id == b.capability.id  # one model only


def test_full_enables_vision_at_40gb():
    for gb in (40.0, 64.0, 128.0, 192.0):
        profile = _unified(gb, f"Machine {int(gb)}", confidence="nvml")
        v = verdict(profile, 100.0)
        assert v.state == "full"
        b = select_bundle(profile, v)
        assert b.vision is True, f"{gb} GB unified should include vision"


def test_vision_and_split_never_below_40gb():
    # Light mode means exactly one model and no image understanding, on
    # unified pools just as much as on discrete cards. The selector must
    # never contradict the verdict's "no vision below 40 GB" rule.
    for gb in (16.0, 24.0, 32.0, 39.0):
        profile = (_unified(gb, "Unified Machine", confidence="nvml")
                   if gb >= 32.0 else _discrete(gb))
        v = verdict(profile, 100.0)
        assert v.state in ("lite", "no-fit")
        b = select_bundle(profile, v)
        assert b.vision is False, f"{gb} GB must not include vision"
        assert b.assistant.id == b.capability.id, \
            f"{gb} GB must run one model, got split assistant"


def test_halo_keeps_109b_ceiling():
    halo = _unified(192.0, "Gorgon Halo", confidence="nvml")
    v = verdict(halo, 100.0)
    assert v.recommended_tier == "halo"
    assert any("109B" in r for r in v.reasons)
    b = select_bundle(halo, v)
    # The halo class is the only place the 109B MoE is allowed.
    assert b.capability.id == "llama4:scout"

    # Just below halo, the 109B must not be chosen.
    extreme = _unified(128.0, "NVIDIA GB10", confidence="nvml")
    b2 = select_bundle(extreme, verdict(extreme, 100.0))
    assert b2.capability.id != "llama4:scout"


def test_disk_reasons():
    v_low = verdict(_unified(64.0, "M"), 3.0)
    assert any("free on the data drive" in r and "free up space" in r for r in v_low.reasons)
    v_none = verdict(_unified(64.0, "M"), 0.0)
    assert any("could not be measured" in r for r in v_none.reasons)
    v_ok = verdict(_unified(64.0, "M"), 100.0)
    assert any("enough for model weights" in r for r in v_ok.reasons)


# --- residency safety: no bundle whose weights exceed 0.80 of serving memory ---

@pytest.mark.parametrize("name,profile,usable,state,tier", MATRIX,
                         ids=[m[0] for m in MATRIX])
def test_residency_safety(name, profile, usable, state, tier):
    v = verdict(profile, profile.disk_free_gb)
    b = select_bundle(profile, v)
    budget = usable * RESIDENCY_FRACTION
    assert b.capability.q4_gb <= budget, (
        f"{name}: capability {b.capability.id} q4 {b.capability.q4_gb} "
        f"exceeds {RESIDENCY_FRACTION} of serving memory ({budget} GB)")
    assert b.assistant.q4_gb <= budget, (
        f"{name}: assistant {b.assistant.id} q4 {b.assistant.q4_gb} "
        f"exceeds {RESIDENCY_FRACTION} of serving memory ({budget} GB)")


def test_bundle_class_matches_verdict_tier():
    # The selector must take the verdict's class as input, not re-derive it.
    profile = _discrete(24.0)
    v = verdict(profile, 100.0)
    b = select_bundle(profile, v)
    assert b.class_name == v.recommended_tier == "mid-24"
    assert b.serving_mem_gb == v.serving_mem_gb == 24.0


# --- detect() profile additions ---

def test_cpu_only_profile_when_no_nvidia(monkeypatch):
    import steward.detect as det
    monkeypatch.setattr(det, "_run", lambda *a, **k: "")
    p = det._linux_nvidia_profile("AMD Ryzen 5", 32.0, 20.0, 50.0)
    assert p.gpu_kind == "cpu"
    assert p.confidence == "/proc/meminfo"
    assert p.disk_free_gb == 50.0
    assert p.ram_available_gb == 20.0
    assert p.serving_mem_gb == 30.0  # 32 GB RAM minus 2 GB for the OS


def test_discrete_profile_vram(monkeypatch):
    import steward.detect as det
    monkeypatch.setattr(
        det, "_run",
        lambda cmd, **k: "NVIDIA GeForce RTX 3090, 24576 MiB"
        if cmd and cmd[0] == "nvidia-smi" else "")
    p = det._linux_nvidia_profile("x", 64.0, 40.0, 100.0)
    assert p.gpu_kind == "discrete"
    assert p.vram_gb == 24.0
    assert p.serving_mem_gb == 24.0
    assert p.bandwidth_gb_s == 936.0
    assert p.confidence == "nvml"


def test_gb10_unified_profile(monkeypatch):
    import steward.detect as det
    monkeypatch.setattr(
        det, "_run",
        lambda cmd, **k: "NVIDIA GB10, 130000 MiB"
        if cmd and cmd[0] == "nvidia-smi" else "")
    p = det._linux_nvidia_profile("Grace", 128.0, 100.0, 200.0)
    assert p.gpu_kind == "unified"
    assert p.unified_gb == 128.0
    assert p.serving_mem_gb == 128.0
    assert p.confidence == "nvml"


def test_igpu_and_cpu_share_the_ram_minus_two_rule():
    assert _cpu(16.0, kind="igpu_ram").serving_mem_gb == 14.0
    assert _cpu(8.0).serving_mem_gb == 6.0
    assert _cpu(1.0).serving_mem_gb == 0.0  # clamped at zero, never negative


def test_detect_runs_safely_anywhere():
    # On any host (CI or otherwise) detect() must return a sane profile with
    # the new fields populated, without raising.
    from steward.detect import detect
    p = detect()
    assert p.confidence in ("nvml", "apple_sysctl", "/proc/meminfo", "fallback")
    assert isinstance(p.disk_free_gb, float) and p.disk_free_gb >= 0
    assert p.serving_mem_gb >= 0


def test_disk_free_walks_up_to_existing_ancestor(tmp_path):
    # The data root may not exist yet at install time; disk free must be
    # measured on the nearest existing ancestor (same filesystem).
    from unittest import mock
    from types import SimpleNamespace
    import steward.detect as det

    missing = tmp_path / "a" / "b" / "c"
    assert not missing.exists()
    with mock.patch("steward.detect.shutil.disk_usage") as du:
        du.return_value = SimpleNamespace(free=1234 * (1024 ** 3))
        got = det._disk_free_gb(missing)
    assert got == 1234.0
    called_path = du.call_args[0][0]
    assert str(called_path) == str(tmp_path)


# --- steward check prints the verdict in plain English ---

def test_check_command_prints_verdict(capsys):
    from steward.cli import main
    rc = main(["check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Steward check" in out
    assert "verdict:" in out
    assert "reasons:" in out
    assert "Minimum machine" in out
    assert "bundle:" in out
    # The check output must stay free of em dashes.
    assert "\u2014" not in out and "\u2013" not in out
