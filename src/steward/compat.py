"""Install-time compatibility verdict: what this machine can actually run.

Kills the certified-hardware framing. There is no support list to be on; at
install time Steward reads the machine (detect.py), computes how much memory
can actually serve model weights, and returns a verdict: full, lite, or
no-fit. The selector (selector.py) consumes this verdict as its input and
never re-derives the class on its own.

Memory rule (single source of truth lives in HardwareProfile.serving_mem_gb,
detect.py): discrete GPUs use VRAM, unified pools (Apple, NVIDIA GB10, Strix
Halo) use the whole pool, iGPU and CPU-only boxes use system RAM minus 2 GB
reserved for the operating system.

State rules:
- Below 16 GB usable is no-fit, with a sentence stating the measured number.
- 16 to under 40 GB is lite: one model, no image understanding, reduced
  context.
- 40 GB and up is full: image understanding enabled.
- Halo class (memory_class "halo") keeps the 109B model ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass

from .detect import HardwareProfile
from .selector import memory_class

MIN_LITE_GB = 16.0   # below this usable serving memory: no-fit
MIN_FULL_GB = 40.0   # at or above this: full mode with image understanding

# Disk is advisory, not a state flip: download can happen later or to a
# different drive. Below this free-space line we warn in the reasons.
MIN_DISK_WARN_GB = 10.0

STATE_NO_FIT = "no-fit"
STATE_LITE = "lite"
STATE_FULL = "full"


@dataclass(frozen=True)
class Verdict:
    state: str                  # "full" | "lite" | "no-fit"
    reasons: list               # plain-English, one sentence each
    recommended_tier: str       # selector memory class, e.g. "mid-24", "halo"
    min_hardware_sentence: str  # the minimum machine that would work
    serving_mem_gb: float       # usable memory the verdict was computed from


def verdict(profile: HardwareProfile, disk_free_gb: float) -> Verdict:
    """Decide what this machine can run, from the profile and free disk.

    disk_free_gb is the free space in GB at the data directory (detect.py
    measures it at the data root; callers may pass a different number).
    """
    mem = profile.serving_mem_gb
    tier = memory_class(mem)
    reasons: list = []

    if profile.gpu_kind == "discrete" and profile.gpu_name:
        memory_desc = f"{profile.vram_gb:g} GB of VRAM on the {profile.gpu_name}"
    elif profile.gpu_kind == "unified":
        memory_desc = f"{profile.unified_gb:g} GB of unified memory"
    elif profile.gpu_kind in ("igpu_ram", "cpu"):
        memory_desc = (f"{profile.ram_gb:g} GB of system RAM, of which {mem:g} GB can "
                       "serve model weights (2 GB is reserved for the operating system)")
    else:
        memory_desc = "no usable serving memory was detected"

    if mem < MIN_LITE_GB:
        state = STATE_NO_FIT
        reasons.append(
            f"This machine has only {mem:g} GB of usable memory ({memory_desc}). "
            f"Steward needs at least 16 GB usable to run a model with room for "
            f"context and the operating system."
        )
        sentence = (
            "Minimum machine that works: 16 GB of usable memory. For example a "
            "discrete graphics card with 16 GB of video memory, or a machine "
            "with 18 GB or more of system RAM and no graphics card."
        )
    elif mem < MIN_FULL_GB:
        state = STATE_LITE
        reasons.append(
            f"Steward runs in light mode here: one model at a time, no image "
            f"understanding, and a reduced context window. The machine has "
            f"{mem:g} GB usable ({memory_desc})."
        )
        sentence = (
            f"Minimum machine that works: 16 GB of usable memory, which this "
            f"machine clears with {mem:g} GB. Reaching 40 GB usable unlocks the "
            f"full experience with image understanding."
        )
    else:
        state = STATE_FULL
        reasons.append(
            f"Full mode: image understanding enabled, longer context, and room "
            f"for a small always-on assistant next to the main model. The "
            f"machine has {mem:g} GB usable ({memory_desc})."
        )
        sentence = (
            f"Minimum machine for full mode: 40 GB of usable memory, which this "
            f"machine clears with {mem:g} GB."
        )

    if disk_free_gb <= 0:
        reasons.append("Free disk space could not be measured at the data directory.")
    elif disk_free_gb < MIN_DISK_WARN_GB:
        reasons.append(
            f"Only {disk_free_gb:g} GB is free on the data drive. Model weights "
            f"need several GB each, so free up space before installing models."
        )
    else:
        reasons.append(
            f"The data drive has {disk_free_gb:g} GB free, enough for model weights."
        )

    if tier == "halo":
        reasons.append(
            "Halo class machine: this is one of the largest pools Steward "
            "supports, so the largest 109B model class is on the table."
        )
    elif mem >= MIN_FULL_GB:
        reasons.append(
            "Below the halo class, the biggest capability model is bounded to "
            "fit the pool with headroom; the 80B MoE is the usual ceiling here."
        )

    return Verdict(
        state=state,
        reasons=reasons,
        recommended_tier=tier,
        min_hardware_sentence=sentence,
        serving_mem_gb=mem,
    )
