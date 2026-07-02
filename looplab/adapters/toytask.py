"""Toy TaskAdapter (I6, ADR-2). A 2-D continuous minimization the loop can solve
fully offline. Loadable from a JSON file. `build_roles` wires the toy backends;
swapping these for LLM-backed roles is the only change needed to go from toy to real.
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from looplab.core.parse import LLMClient
from looplab.agents.roles import LLMResearcher, ToyObjectiveDeveloper, ToyResearcher


class ToyTask(BaseModel):
    kind: str = "quadratic"
    id: str = "toy_quadratic"
    goal: str = "minimize (x-3)^2 + (y+1)^2"
    direction: str = "min"
    bounds: dict[str, tuple[float, float]] = Field(
        default_factory=lambda: {"x": (-10.0, 10.0), "y": (-10.0, 10.0)}
    )
    seed: int = 0
    step: float = 1.0
    noise: float = 0.0  # eval noise std; >0 enables meaningful multi-seed confirmation

    @classmethod
    def load(cls, path: str | Path) -> "ToyTask":
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))

    def build_roles(self) -> tuple[ToyResearcher, ToyObjectiveDeveloper]:
        return (
            ToyResearcher(self.bounds, seed=self.seed, step=self.step),
            ToyObjectiveDeveloper(noise=self.noise),
        )

    def llm_roles(self, client: LLMClient, parser: str = "tool_call"):
        hint = ("Parameter space: " +
                ", ".join(f"{k} in [{lo}, {hi}]" for k, (lo, hi) in self.bounds.items()) +
                ". Lower metric is better; move toward lower-loss regions using the history.")
        return (LLMResearcher(client, space_hint=hint, bounds=self.bounds, parser=parser),
                ToyObjectiveDeveloper(noise=self.noise))
