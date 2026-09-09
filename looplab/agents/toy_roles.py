"""The offline toy role backends (doc 25 AG-02).

The P0 loop runs fully offline (no API keys) on these two: the Researcher is a blind seeded
optimizer (random seeds, then hill-climbs around the current best using only *observed* metrics);
the Developer emits a script whose executed objective is the ground truth the Researcher never
sees. That exercises the real loop (draft -> run -> evaluate -> improve -> select) deterministically.

WHY THEY LEFT `roles.py`. The finding's evidence is that a module about LLM role backends carried
the toy objective template, the wrapper stack and (until 2026-08-05) a ctypes CUDA probe, so every
reader of one paid for the other three. The toy pair is the most separable of them: it shares no
name with the LLM roles, and its only production consumers are `adapters/toytask.py` (which builds
it) and `engine/speculation_gate.py` (which admits the calibration envelope by EXACT type).

NO re-export from `roles.py`, deliberately, unlike the other three modules of this split. The
calibration envelope identifies these two classes by their dotted path
(`search/speculation_calibration.py::SPECULATION_RUNTIME_ROLES_DESCRIPTOR`), so a second live
spelling of the same class would be a second answer to "which implementation ran"; every importer
names this module instead.
"""
from __future__ import annotations

import random
from typing import Optional

from looplab.core.calibration import SPECULATION_CUDA_PROBE_CODE_PREFIX
from looplab.core.models import Idea, Node, RunState, developer_artifact_footprint



_OBJECTIVE_TEMPLATE = '''\
import json, os, random
# Generated solution. The objective below is the toy "ground truth" the
# Researcher optimizes blindly via observed metrics only.
x = {x}
y = {y}
loss = (x - 3.0) ** 2 + (y + 1.0) ** 2
noise = {noise}
if noise:
    # Seeded eval noise: lets the multi-seed confirmation phase (I12) measure
    # variance. LOOPLAB_EVAL_SEED is unset (-> "0") during normal evaluation, so
    # search stays deterministic; the confirm phase varies it across seeds.
    rng = random.Random(int(os.environ.get("LOOPLAB_EVAL_SEED", "0")))
    loss += rng.gauss(0.0, noise)
print(json.dumps({{"metric": loss}}))
'''


_OBJECTIVE_METRIC_LINE = 'print(json.dumps({"metric": loss}))\n'
_CALIBRATION_OBJECTIVE_METRIC_LINE = '''print(json.dumps({
    "metric": loss,
    "speculation_cuda_probe_v": _looplab_cuda_probe_v,
    "device_count": _looplab_cuda_device_count_value,
    "alloc_bytes": _looplab_cuda_alloc_bytes,
    "device_ordinal": _looplab_cuda_device_ordinal,
}))
'''


class ToyResearcher:
    """Blind seeded optimizer: random seeds, then Gaussian hill-climb around best."""

    def __init__(self, bounds: dict[str, tuple[float, float]], seed: int = 0, step: float = 1.0,
                 *, calibration_concepts: bool = False):
        self.bounds = bounds
        self.seed = seed
        self.step = step
        self.rng = random.Random(seed)
        # Maintainer-only speculation calibration.  Default-off is important: the ordinary ToyTask
        # event bytes and search trajectory stay unchanged.  The calibration envelope is validated by
        # Engine before this flag is trusted as evidence.
        self.calibration_concepts = bool(calibration_concepts)

    def _calibration_fields(self, operator: str) -> dict:
        if not self.calibration_concepts:
            return {}
        # A small source-owned taxonomy gives the coverage gate real, trusted authored membership
        # instead of letting a concept-free toy run make the coverage ratio vacuously pass.
        return {
            "concept_mode": "full",
            "concepts": [f"operator/{operator}", "objective/quadratic", "space/two-dimensional"],
            # Calibration candidates must cross the real GPU resource admission path.  The paired
            # Developer independently finalizes the same one-GPU requirement in its artifact.
            "footprint": {"gpus": 1},
        }

    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        keys = list(self.bounds)
        if parent is None:
            params = {k: round(self.rng.uniform(*self.bounds[k]), 4) for k in keys}
            return Idea(operator="draft", params=params, rationale="random seed point",
                        **self._calibration_fields("draft"))
        params = {}
        for k in keys:
            lo, hi = self.bounds[k]
            v = parent.idea.params.get(k, 0.0) + self.rng.gauss(0.0, self.step)
            params[k] = round(max(lo, min(hi, v)), 4)
        return Idea(operator="improve", params=params,
                    rationale=f"perturb best node {parent.id} (metric={parent.metric})",
                    **self._calibration_fields("improve"))


class ToyObjectiveDeveloper:
    """Renders an Idea's params into a runnable script (the objective is fixed here).
    `noise` (>0) injects seeded eval noise so the confirmation phase has variance to
    measure; 0 (default) keeps the objective deterministic."""

    def __init__(self, noise: float = 0.0, *, calibration_gpu_probe: bool = False):
        self.noise = noise
        # Default-off for byte-compatible ToyTask behavior.  Engine admits this probe only inside the
        # strict offline calibration profile and requires a visible GPU before any run event is written.
        self.calibration_gpu_probe = bool(calibration_gpu_probe)
        self.last_footprint: dict | None = None

    def implement(self, idea: Idea) -> str:
        code = _OBJECTIVE_TEMPLATE.format(
            x=idea.params.get("x", 0.0),
            y=idea.params.get("y", 0.0),
            noise=self.noise,
        )
        if self.calibration_gpu_probe:
            if not code.endswith(_OBJECTIVE_METRIC_LINE):
                raise RuntimeError("Toy objective metric line no longer matches calibration contract")
            code = (SPECULATION_CUDA_PROBE_CODE_PREFIX
                    + code[:-len(_OBJECTIVE_METRIC_LINE)]
                    + _CALIBRATION_OBJECTIVE_METRIC_LINE)
        self.last_footprint = developer_artifact_footprint(idea.footprint, code)
        return code
