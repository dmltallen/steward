"""Open-model catalog for Steward.

Each entry records what Steward needs to auto-select a bundle: total parameter
count (residency), active parameter count (MoE per-token cost), an estimated
Q4 weight size in GB, and a conservative tokens-per-second range for the class
of hardware it will be served on (sourced from 2026 local-inference guides;
single-DPU and unified-pool machines). Tokens ranges are estimates, not
benchmarks on every silicon; the selector treats the top of the range as the
optimistic ceiling and requires it to clear the speed floor.

New open generations replace catalog entries over time via OTA; the catalog
is the single place model identity is defined for auto-selection, so it stays
stable even as upstream checkpoints move.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Model:
    id: str
    family: str
    params_b: float      # total parameters, billions
    active_b: float      # active parameters per token (== params_b for dense)
    q4_gb: float         # approximate Q4 weights size, GB
    tok_range: tuple     # (low, high) tokens/sec, class-derived estimate
    license: str
    kind: str            # dense | moe
    note: str = ""


MODELS: dict[str, Model] = {
    "gemma4:7b": Model("gemma4:7b", "Gemma 4", 7.6, 7.6, 4.7, (15, 90), "Apache-2.0", "dense",
                       "small always-hot assistant"),
    "qwen3:8b": Model("qwen3:8b", "Qwen 3", 8.0, 8.0, 5.2, (25, 100), "Apache-2.0", "dense",
                      "small always-hot assistant, better tool use than 7B"),
    "qwen3:14b": Model("qwen3:14b", "Qwen 3", 14.0, 14.0, 9.0, (15, 110), "Apache-2.0", "dense",
                       "mainstream 16 GB class"),
    "gemma4:27b": Model("gemma4:27b", "Gemma 4", 26.8, 26.8, 16.0, (13, 50), "Apache-2.0", "dense",
                        "Ghost-Core 24 GB class"),
    "qwen3:27b": Model("qwen3:27b", "Qwen 3", 27.0, 27.0, 17.0, (13, 45), "Apache-2.0", "dense",
                       "Ghost-Core 24 GB class, stronger coding/agent"),
    "qwen3:32b": Model("qwen3:32b", "Qwen 3", 32.0, 32.0, 20.0, (10, 45), "Apache-2.0", "dense",
                       "32 GB class (RTX 5090 single card)"),
    "llama3.3:70b": Model("llama3.3:70b", "Llama 3.3", 70.0, 70.0, 40.0, (8, 38), "Llama Community", "dense",
                          "48-64 GB class, only where it fits with headroom"),
    "qwen3-next:80b": Model("qwen3-next:80b", "Qwen 3 Next", 80.0, 3.0, 50.0, (20, 45), "Apache-2.0", "moe",
                            "80B MoE A3B: cheap per token, big residency; 96-128 GB class"),
    "llama4:scout": Model("llama4:scout", "Llama 4 Scout", 109.0, 17.0, 65.0, (10, 30), "Llama Community", "moe",
                          note="109B A17B MoE, extreme pool class; Q4 size estimate [uncertain]"),
}


def model(id: str) -> Model:
    return MODELS[id]


def all_models() -> list[Model]:
    return list(MODELS.values())
