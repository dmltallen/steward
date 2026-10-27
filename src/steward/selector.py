"""Automatic model-bundle selection for Steward.

Given a HardwareProfile (see detect.py) and the install-time compatibility
verdict (see compat.py), pick the small always-hot assistant model, the
largest capability model whose Q4 weights fit the serving memory with headroom
and whose optimistic speed clears the floor, and whether a vision model rides
along. This is the "no complicated setup" core: the user never chooses a
model, Steward does.

The verdict decides what the machine can do (state, recommended tier, serving
memory); the selector consumes it as input instead of re-deriving the class.
When no verdict is passed, the selector falls back to deriving the class from
the profile's serving memory so older callers keep working.

Rules (versioned here, single source of truth):
1. The serving memory is VRAM for discrete GPUs, the unified pool for
   Apple/GB10/Strix-Halo-class machines, and system RAM minus 2 GB for
   iGPU/CPU boxes (HardwareProfile.serving_mem_gb).
2. Residency must fit mem * 0.80 (leave room for KV cache, context, memory
   store, browser, and the OS). On a 24 GB card that lands on the 27 B class,
   the same tier Ghost's Core is built on.
3. Capability floor: the model's optimistic tokens/sec (top of tok_range)
   must clear 15 tok/s, the real-time feel threshold.
4. Small discrete boxes (under 40 GB) run one model, the capability model;
   the assistant is that same model. Always-hot split assistants and vision
   are for unified pools and 40 GB+ boxes, where the headroom is real.
5. Vision rides along when the pool is >= 40 GB and at least 6 GB is left
   after the capability and assistant picks.
6. Llama 4 Scout (109B MoE) is restricted to the halo class (160 GB+ pools);
   below that the 80B MoE is the capability ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass

from .detect import HardwareProfile
from .models import Model, all_models, MODELS

SPEED_FLOOR = 15.0           # tok/s, optimistic ceiling must clear this
RESIDENCY_FRACTION = 0.80    # share of serving memory usable for weights
BIG_POOL_HEADROOM_GB = 10.0  # min free after capability on big boxes
VISION_MIN_HEADROOM_GB = 6.0
VISION_THRESHOLD_GB = 40.0
HALO_MEM_GB = 160.0

_FAST_ASSISTANTS = ("qwen3:8b", "gemma4:7b")


@dataclass(frozen=True)
class Bundle:
    capability: Model
    assistant: Model
    vision: bool
    serving_mem_gb: float
    class_name: str

    def describe(self) -> str:
        return (
            f"class: {self.class_name}\n"
            f"capability: {self.capability.id} (Q4 ~{self.capability.q4_gb:g} GB, "
            f"est {self.capability.tok_range[0]}-{self.capability.tok_range[1]} tok/s)\n"
            f"assistant: {'same as capability' if self.assistant.id == self.capability.id else self.assistant.id}\n"
            f"vision: {'yes' if self.vision else 'no'}\n"
            f"serving memory budget: {self.serving_mem_gb:g} GB"
        )


def memory_class(mem_gb: float) -> str:
    if mem_gb < 10:
        return "entry"
    if mem_gb < 14:
        return "small"
    if mem_gb < 20:
        return "mainstream-16"
    if mem_gb < 28:
        return "mid-24"       # Ghost-Core class
    if mem_gb < 34:
        return "high-32"
    if mem_gb < 60:
        return "power"
    if mem_gb < 96:
        return "super"
    if mem_gb < 140:
        return "extreme"
    return "halo"


def _fits(model: Model, budget_gb: float) -> bool:
    return model.q4_gb <= budget_gb


def _clears_floor(model: Model) -> bool:
    return model.tok_range[1] >= SPEED_FLOOR


def _fast_assistants() -> list[Model]:
    return [MODELS[i] for i in _FAST_ASSISTANTS if i in MODELS]


def select_bundle(profile: HardwareProfile, verdict=None) -> Bundle:
    """Pick the bundle. Pass the compat verdict (see compat.py) so the class and
    serving memory come from the verdict; without it, derive both from the
    profile's serving memory (backward compatible)."""
    if verdict is not None:
        mem = verdict.serving_mem_gb
        cls = verdict.recommended_tier
    else:
        mem = profile.serving_mem_gb
        cls = memory_class(mem)
    budget = mem * RESIDENCY_FRACTION

    pool = [m for m in all_models() if _fits(m, budget) and _clears_floor(m)]
    if cls != "halo":
        pool = [m for m in pool if m.id != "llama4:scout"]

    # Below the 40 GB threshold the verdict is lite: one capability model,
    # no always-hot split, no vision. At 40 GB and up the full experience
    # (split assistant, vision) applies, on unified pools and discrete alike.
    lite_mode = mem < VISION_THRESHOLD_GB

    if pool and lite_mode:
        capability = max(pool, key=lambda m: m.params_b)
        assistant = capability
        vision = False
    elif pool:
        capable = [m for m in pool if budget - m.q4_gb >= BIG_POOL_HEADROOM_GB]
        capability = max(capable, key=lambda m: m.params_b) if capable else max(pool, key=lambda m: m.params_b)
        headroom = budget - capability.q4_gb
        if headroom >= 4.0:
            assistant_budget = headroom * 0.6
            fitting = [m for m in _fast_assistants() if m.q4_gb <= assistant_budget]
            assistant = min(fitting, key=lambda m: m.q4_gb) if fitting else capability
            left = headroom - (assistant.q4_gb if assistant.id != capability.id else 0.0)
        else:
            assistant = capability
            left = headroom
        vision = left >= VISION_MIN_HEADROOM_GB
    else:
        capability = all_models()[0]
        assistant = capability
        vision = False

    return Bundle(
        capability=capability,
        assistant=assistant,
        vision=vision,
        serving_mem_gb=mem,
        class_name=cls,
    )
