"""Hardware detection for Steward.

Produces a HardwareProfile describing the machine the assistant will run on.
The profile drives the install-time compatibility verdict (compat.py) and the
model-bundle selection (selector.py). Detection is deliberately conservative:
unknown fields default to zero rather than guessing, and every case it cannot
recognize lands in gpu_kind = "cpu" (CPU-only, no GPU path) so the verdict
reports what the memory can actually hold and the selector falls back to the
smallest safe bundle.
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class HardwareProfile:
    os_name: str
    arch: str
    cpu_name: str = ""
    npu_tops: float = 0.0
    gpu_kind: str = "none"  # discrete | unified | igpu_ram | cpu | none
    gpu_name: str = ""
    vram_gb: float = 0.0
    ram_gb: float = 0.0
    ram_available_gb: float = 0.0
    unified_gb: float = 0.0
    disk_free_gb: float = 0.0
    bandwidth_gb_s: float = 0.0  # best effort, GB/s for the serving memory
    confidence: str = "fallback"  # nvml | apple_sysctl | /proc/meminfo | fallback
    notes: list = field(default_factory=list)

    @property
    def serving_mem_gb(self) -> float:
        """Usable serving memory in GB, the number every decision starts from.

        Rule (single source, consumed by compat.py and selector.py): discrete
        GPUs use VRAM, unified pools (Apple, NVIDIA GB10, Strix Halo) use the
        whole unified pool, iGPU and CPU-only boxes use system RAM minus 2 GB
        reserved for the operating system.
        """
        if self.gpu_kind == "unified":
            return self.unified_gb
        if self.gpu_kind == "discrete":
            return self.vram_gb
        if self.gpu_kind in ("igpu_ram", "cpu"):
            return max(self.ram_gb - 2.0, 0.0)
        return 0.0

    @property
    def usable_mem_gb(self) -> float:
        """Backward-compatible alias for serving_mem_gb."""
        return self.serving_mem_gb


def _run(cmd: list[str], timeout: int = 5) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def _mem_total_gb_linux() -> float:
    out = _run(["cat", "/proc/meminfo"])
    m = re.search(r"MemTotal:\s+(\d+) kB", out)
    return round(int(m.group(1)) / 1024 / 1024, 1) if m else 0.0


def _mem_available_gb_linux() -> float:
    out = _run(["cat", "/proc/meminfo"])
    m = re.search(r"MemAvailable:\s+(\d+) kB", out)
    return round(int(m.group(1)) / 1024 / 1024, 1) if m else 0.0


def _disk_free_gb(path: Path) -> float:
    """Free space on the filesystem that holds the data directory, in GB.

    The data root may not exist yet at install time; walk up to the nearest
    existing ancestor, which sits on the same filesystem, so the measurement
    still reports the disk the data dir will live on.
    """
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
        return round(free / (1024 ** 3), 1)
    except OSError:
        return 0.0


def _cpu_name_linux() -> str:
    out = _run(["lscpu"])
    m = re.search(r"Model name:\s*(.+)", out)
    return m.group(1).strip() if m else ""

# Apple memory bandwidth by chip family, GB/s (ref tables, 2026 guides).
_APPLE_BW = {
    "m1": 68, "m2": 100, "m3": 100, "m4": 120,
    "m1 pro": 200, "m2 pro": 200, "m3 pro": 150, "m4 pro": 273,
    "m1 max": 400, "m2 max": 400, "m3 max": 400, "m4 max": 546,
    "m1 ultra": 800, "m2 ultra": 800, "m3 ultra": 819,
    "m5": 153,
}


def _apple_profile(disk_free_gb: float = 0.0) -> HardwareProfile:
    chip = (_run(["sysctl", "-n", "machdep.cpu.brand_string"]) or "apple").lower()
    membytes = _run(["sysctl", "-n", "hw.memsize"])
    ram_gb = round(int(membytes) / 1e9, 1) if membytes.isdigit() else 0.0
    bw = next((v for k, v in _APPLE_BW.items() if k in chip), 100.0)
    return HardwareProfile(
        os_name="darwin",
        arch="arm64",
        cpu_name=chip,
        gpu_kind="unified",
        gpu_name=chip,
        unified_gb=ram_gb,
        ram_gb=ram_gb,
        disk_free_gb=disk_free_gb,
        bandwidth_gb_s=bw,
        confidence="apple_sysctl",
        notes=["Apple unified memory; the pool is the serving memory.",
               "Available RAM not exposed via sysctl; reported as 0, does not affect decisions."],
    )


def _linux_nvidia_profile(cpu_name: str, ram_gb: float, ram_avail: float,
                          disk_free_gb: float) -> HardwareProfile:
    smi = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"])
    if not smi:
        return HardwareProfile(
            os_name="linux", arch=platform.machine(), cpu_name=cpu_name,
            ram_gb=ram_gb, ram_available_gb=ram_avail,
            gpu_kind="cpu", disk_free_gb=disk_free_gb,
            confidence="/proc/meminfo",
            notes=["No NVIDIA GPU detected by nvidia-smi; running CPU-only. "
                   "An on-chip GPU, if present, uses the same system RAM."],
        )
    name, _, mem = smi.partition(",")
    vram_mb = 0.0
    m = re.search(r"(\d+)\s*MiB", mem)
    if m:
        vram_mb = float(m.group(1))
    vram_gb = round(vram_mb / 1024, 1)
    is_gb10 = "gb10" in name.lower()
    profile = HardwareProfile(
        os_name="linux", arch=platform.machine(), cpu_name=cpu_name,
        ram_gb=ram_gb, ram_available_gb=ram_avail, gpu_name=name.strip(),
        disk_free_gb=disk_free_gb, confidence="nvml",
    )
    if is_gb10:
        # UMA platform: the unified pool is the serving memory (121 GB usable on U1).
        profile.gpu_kind = "unified"
        profile.unified_gb = min(ram_gb, 128.0)
        profile.bandwidth_gb_s = 273.0  # GB10 LPDDR5X, approximate
        profile.notes = ["NVIDIA GB10 UMA platform detected."]
    else:
        profile.gpu_kind = "discrete"
        profile.vram_gb = vram_gb
        bw_lookup = {
            "4090": 1008, "4080": 717, "4070 ti": 672, "4060 ti": 288, "4060": 272,
            "3090": 936, "5080": 960, "5070 ti": 896, "5070": 448, "5060": 448,
            "5090": 1792,
        }
        for k, v in bw_lookup.items():
            if k in name.lower():
                profile.bandwidth_gb_s = v
                break
        profile.notes = [f"Discrete GPU; VRAM {vram_gb} GB is the serving memory."]
    return profile


def _npu_estimate(cpu_name: str) -> float:
    name = cpu_name.lower()
    if "ryzen ai 9 hx 370" in name or "ryzen ai 9 hx 365" in name or "ryzen ai max" in name:
        return 50.0
    if "core ultra" in name:
        return 40.0
    return 0.0


def detect(data_dir: Path | None = None) -> HardwareProfile:
    """Detect the running machine into a HardwareProfile.

    data_dir selects which filesystem to measure free disk on; it defaults to
    the Steward data root (paths.data_root). No directory is created here.
    """
    from . import paths  # local import keeps detect a stdlib-only leaf otherwise

    base = data_dir if data_dir is not None else paths.data_root()
    disk_free = _disk_free_gb(base)

    system = platform.system().lower()
    arch = platform.machine().lower()
    if system == "darwin" and arch.startswith("arm"):
        return _apple_profile(disk_free)
    cpu_name = _cpu_name_linux() if system == "linux" else platform.processor() or ""
    ram_gb = _mem_total_gb_linux() if system == "linux" else 0.0
    ram_avail = _mem_available_gb_linux() if system == "linux" else 0.0
    if system == "linux":
        return _linux_nvidia_profile(cpu_name, ram_gb, ram_avail, disk_free)
    return HardwareProfile(
        os_name=system, arch=arch, cpu_name=cpu_name,
        ram_gb=ram_gb, gpu_kind="cpu", disk_free_gb=disk_free,
        confidence="fallback",
        notes=["Unsupported OS for full detection; treating as CPU-only."],
    )
