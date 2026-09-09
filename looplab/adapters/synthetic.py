"""The shared skeleton behind the SYNTHETIC demo TaskAdapters (doc 25 RA-06).

`toytask` / `regression` / `classification` / `timeseries` / `mlebench` are five stable, offline,
fully-deterministic demo tasks, and they were five copy-paste skeletons of one shape: a pydantic
task model repeating `kind`/`id`/`goal`/`direction`/`comparison_contract` plus a hand-copied
`direction` validator, and a seeded-`random.Random` Researcher whose `propose()` is
"draft random params, else perturb the parent" with only the arithmetic changing. This module holds
the two halves that are genuinely the same — `SyntheticTaskBase` and `PerturbResearcher` — and
nothing else: the data generators, the code templates and the `space_hint` prose stay in their own
adapters, because those are what actually distinguish the tasks from one another.

WHAT IS DELIBERATELY *NOT* HOISTED, and why each omission is a fact rather than an oversight:

* `seed`. Every one of the five declares it, so it looks like the sixth common field — but it sits
  in a DIFFERENT position in each model, and a task model's field ORDER is load-bearing:
  `core/setup_identity.py::setup_config_hash` dumps the task payload WITHOUT sorted keys ("it is the
  model's own field order") and that digest is `run_started.config_hash`, which a resume compares
  against and which `search/speculation_quality.py::_validate_calibration_setup` re-derives by
  rebuilding a `ToyTask` and dumping it. Hoisting `seed` moves it ahead of `bounds`/`n`/`gap` in
  `model_fields`, which silently changes that digest for every already-recorded synthetic run:
  a resumed run refuses its own snapshot and every issued calibration receipt stops validating.
  The five fields this base DOES declare are, in every one of the five models, already the first
  five in exactly this order — which is why hoisting them is a no-op on the wire.
  `tests/test_synthetic_task_base.py` pins both halves.
* `gpu_capable()`. All five return False, but each states a DIFFERENT measured reason for it (a
  fixed template with no code channel; a brief that pins the solution to numpy+stdlib). A base
  docstring could carry only one of them, and the reasoning is the part worth keeping.
* `columns()` / `build_roles()` / `llm_roles()`. Same NAME, entirely different bodies — a base
  cannot factor a convention out of five unrelated implementations, only pretend to.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional, Protocol

from pydantic import BaseModel, field_validator

from looplab.core.comparison import ComparisonContract
from looplab.core.models import Idea, Node, RunState, validate_direction


class SyntheticTaskBase(BaseModel):
    """The five fields every synthetic task model declares first, and the one validator they share.

    Subclasses redeclare `kind`/`id`/`goal` (and `direction` where the objective is maximized) with
    their own defaults; pydantic keeps an overridden field in the BASE's position, so the resulting
    `model_fields` order — and therefore `setup_config_hash` — is byte-identical to the hand-written
    models this replaced.
    """

    kind: str
    id: str
    goal: str
    direction: str = "min"

    @field_validator("direction")
    @classmethod
    def _direction_valid(cls, v):
        return validate_direction(v)
    comparison_contract: ComparisonContract | None = None

    def external_fallback_uses_llm(self) -> bool:
        return False  # the fallback fills a deterministic local template


class Knob(Protocol):
    """One tunable number of a synthetic task, in the two moves `PerturbResearcher` can make.

    `improve` returns `(value, cited)`: the new value, and the PARENT reading the rationale names.
    They are not the same number — an integer axis cites the coerced `int` it walked from while a
    jittered one cites the parent's raw float — and the rationale is what an operator reads to tell
    two sibling nodes apart, so the knob decides its own citation rather than the caller guessing.
    """

    name: str

    def draft(self, rng: random.Random) -> float: ...

    def improve(self, rng: random.Random, params: dict) -> tuple[float, object]: ...


def _clamp(value: float, lo: float | None, hi: float | None) -> float:
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


@dataclass(frozen=True)
class IntWalk:
    """An integer complexity axis: drafted uniformly in [lo, hi], then walked by one of `steps`.

    This is the lever that decides whether a model can represent the target at all (a polynomial
    degree, a seasonal period, a neighbour count), so it moves in whole units and is clamped rather
    than scaled — a fractional degree is not a member of the family the rationale claims.
    """

    name: str
    lo: int
    hi: int
    default: float = 1.0
    steps: tuple[int, ...] = (-1, 0, 1)

    def draft(self, rng: random.Random) -> float:
        return float(rng.randint(self.lo, self.hi))

    def improve(self, rng: random.Random, params: dict) -> tuple[float, object]:
        seen = int(round(params.get(self.name, self.default)))
        return float(_clamp(seen + rng.choice(self.steps), self.lo, self.hi)), seen


@dataclass(frozen=True)
class ScaledChoice:
    """A positive scale knob: drafted from a fixed ladder, then MULTIPLIED by one of `factors`.

    Learning rates and regularization strengths matter on a log scale, so a halve/keep/double walk
    covers the interesting range in a handful of steps where an additive one would not leave the
    order of magnitude it started in.
    """

    name: str
    choices: tuple[float, ...]
    factors: tuple[float, ...] = (0.5, 1.0, 2.0)
    lo: float | None = None
    hi: float | None = None
    ndigits: int = 6
    draft_ndigits: int | None = None
    default: float = 0.0

    def draft(self, rng: random.Random) -> float:
        drawn = rng.choice(self.choices)
        return float(drawn if self.draft_ndigits is None else round(drawn, self.draft_ndigits))

    def improve(self, rng: random.Random, params: dict) -> tuple[float, object]:
        seen = params.get(self.name, self.default)
        return _clamp(round(seen * rng.choice(self.factors), self.ndigits), self.lo, self.hi), seen


@dataclass(frozen=True)
class Carried:
    """Drafted from a fixed ladder, then CARRIED from the parent verbatim on every improve.

    A knob the offline Researcher deliberately does not explore: it still needs a starting value, but
    perturbing everything at once means no node's metric can be attributed to any one change.
    """

    name: str
    choices: tuple[float, ...]
    draft_ndigits: int | None = None
    default: float = 0.0

    def draft(self, rng: random.Random) -> float:
        drawn = rng.choice(self.choices)
        return float(drawn if self.draft_ndigits is None else round(drawn, self.draft_ndigits))

    def improve(self, rng: random.Random, params: dict) -> tuple[float, object]:
        seen = params.get(self.name, self.default)
        return seen, seen


@dataclass(frozen=True)
class Jitter:
    """A bounded continuous knob: drafted uniformly in [lo, hi], then moved by a Gaussian step."""

    name: str
    sigma: float
    lo: float = 0.0
    hi: float = 1.0
    ndigits: int = 3
    default: float = 0.5

    def draft(self, rng: random.Random) -> float:
        # `rng.random()` rather than `rng.uniform(lo, hi)` when the range is the unit interval would
        # be a second spelling of the same draw; `lo + (hi - lo) * rng.random()` IS `uniform`'s body,
        # so one `random()` call is consumed either way and a [0, 1] knob draws exactly `random()`.
        return round(self.lo + (self.hi - self.lo) * rng.random(), self.ndigits)

    def improve(self, rng: random.Random, params: dict) -> tuple[float, object]:
        seen = params.get(self.name, self.default)
        return _clamp(round(seen + rng.gauss(0.0, self.sigma), self.ndigits), self.lo, self.hi), seen


class PerturbResearcher:
    """The offline (`backend=toy`) Researcher every synthetic task shares: draft, then hill-walk.

    `knobs` is the task's parameter space in the order the emitted `Idea.params` must carry it, and
    that order is also the order the knobs draw from `rng` — so a task keeps the exact random stream
    its hand-written Researcher had. The FIRST knob is the one the improve rationale cites, because
    it is by convention the structural lever (degree / alpha / k) an operator scans a node list for.
    """

    def __init__(self, knobs, *, seed: int = 0, draft_rationale: str = "random hyperparameters"):
        self.knobs = tuple(knobs)
        self.rng = random.Random(seed)
        self.draft_rationale = draft_rationale

    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        if parent is None:
            # A dict comprehension evaluates left to right, which is what keeps the draft's draw
            # order equal to `knobs` order and therefore to the pre-collapse Researcher's stream.
            return Idea(operator="draft",
                        params={knob.name: knob.draft(self.rng) for knob in self.knobs},
                        rationale=self.draft_rationale)
        params: dict[str, float] = {}
        cited = ""
        for knob in self.knobs:
            value, seen = knob.improve(self.rng, parent.idea.params)
            params[knob.name] = value
            if not cited:
                cited = f"{knob.name}={seen}"
        return Idea(operator="improve", params=params,
                    rationale=f"perturb node {parent.id} ({cited})")


__all__ = ["SyntheticTaskBase", "PerturbResearcher", "Knob",
           "IntWalk", "ScaledChoice", "Carried", "Jitter"]
