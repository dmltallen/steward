"""Tests for automatic bundle selection. Deterministic, no hardware needed."""

from steward.detect import HardwareProfile
from steward.selector import select_bundle, memory_class

UNIFIED_128 = HardwareProfile(os_name="linux", arch="aarch64", cpu_name="NVIDIA GB10",
                              gpu_kind="unified", gpu_name="NVIDIA GB10",
                              unified_gb=128.0, ram_gb=128.0, bandwidth_gb_s=273.0)


def _profile(vram, unified=0.0, kind="discrete"):
    return HardwareProfile(os_name="linux", arch="x86_64", cpu_name="x",
                           gpu_kind=kind, vram_gb=vram, unified_gb=unified,
                           ram_gb=128.0, bandwidth_gb_s=960.0)


def test_memory_classes():
    got = [memory_class(m) for m in (8, 12, 16, 24, 32, 48, 64, 128, 192)]
    assert got == ["entry", "small", "mainstream-16", "mid-24", "high-32",
                   "power", "super", "extreme", "halo"]


def test_ghost_class_matches_ghost():
    # 24 GB discrete should land on the 27B family, exactly Ghost-Core's tier.
    b = select_bundle(_profile(24))
    assert b.class_name == "mid-24"
    assert "qwen3:27b" in b.capability.id or "gemma4:27b" in b.capability.id


def test_16gb_mainstream():
    b = select_bundle(_profile(16))
    assert b.class_name == "mainstream-16"
    assert b.capability.id == "qwen3:14b"


def test_32gb_power():
    b = select_bundle(_profile(32))
    assert b.class_name == "high-32"
    assert b.capability.id == "qwen3:32b"


def test_unified_128_extreme():
    b = select_bundle(UNIFIED_128)
    assert b.class_name == "extreme"
    assert b.capability.id == "qwen3-next:80b"
    assert b.assistant.id in ("qwen3:8b", "gemma4:7b")


def test_vision_only_on_big_pools():
    assert select_bundle(_profile(24)).vision is False
    assert select_bundle(_profile(64)).vision is True


def test_never_empty_bundle():
    for mem in (8, 6, 4):
        b = select_bundle(_profile(mem))
        assert b.capability is not None and b.assistant is not None


def test_wizard_renders_bundle(capsys):
    # First-run wizard must detect hardware, plan, and print a bundle + pulls.
    from steward.cli import main
    rc = main(["wizard"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Chosen bundle" in out
    assert "class:" in out
    assert "ollama pull " in out
