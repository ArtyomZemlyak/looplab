"""Role backends (I5, ADR-7). `Researcher` proposes an Idea (params to try);
`Developer` turns an Idea into runnable code. Both are Protocols so an LLM-backed
or external-coding-agent backend drops in with zero orchestrator change.

The Toy* implementations make the P0 loop runnable fully offline (no API keys):
the Researcher is a blind seeded optimizer (random seeds, then hill-climbs around
the current best using only *observed* metrics); the Developer emits a script whose
executed objective is the ground truth the Researcher never sees. This exercises
the real loop (draft -> run -> evaluate -> improve -> select) deterministically.
"""
from __future__ import annotations

import json
import random
from typing import Optional, Protocol

from looplab.core.models import (Idea, IdeaEmission, Node, RunState,
                                 developer_artifact_footprint, normalize_researcher_footprint)
from looplab.core.parse import LLMClient, ParseError, extract_code, parse_structured
from looplab.core.prompts import PromptStore, render
from looplab.core.validate import AgentReport, validate_agent_code


def _attention_points() -> str:
    """Shared environment-awareness cues for the LLM roles (best-effort; never break role building)."""
    try:
        from looplab.core.hardware import operational_attention_points
        return operational_attention_points()
    except Exception:  # noqa: BLE001
        return ""

_CONCEPT_AUTHORING_GUIDANCE = (
    "Always set `concept_mode` explicitly. Default to `concept_mode=\"full\"` with `concepts` as the "
    "exact complete SET of `axis/slug` ids this experiment touches. Use `concept_mode=\"delta\"` only "
    "when the run context explicitly enables delta authoring and supplies the inherited membership; "
    "then put only the change in `concepts_added` and `concepts_removed`. BOTH delta lists may be empty "
    "to inherit unchanged. In delta mode do not "
    "re-state inherited ids in `concepts`. An experiment may touch several concepts; include every "
    "applicable change, reuse existing ids where they fit, and mint a new `axis/slug` only when none "
    "fits. Key on the underlying method/family, not the surface name. ")


_RESEARCHER_CORE = "You are an ML researcher proposing the next experiment as parameters to try. "
# P6/P21 (docs/PROMPT_REVIEW.md): the intra-node sweep OFFER, shared VERBATIM by both researchers
# (`LLMResearcher` here and agent.py's `ToolUsingResearcher`) via `_researcher_capability_suffix`,
# and GATED on capability: only the in-house `LLMDeveloper` honors `idea.space` —
# `CliAgentDeveloper` and `LLMRepoDeveloper` never read it — so `make_roles` sets
# `offer_sweep=False` on those backends and this fragment is dropped rather than promising a
# sweep nobody will run (the engine would stretch the node by sweep_timeout_mult while waiting
# for a `trials` line that never comes).
_SWEEP_OFFER = ("Optionally, when a hyperparameter is cheap to vary and the task data loads "
                "fast, you MAY propose a SWEEP instead of a single point: set `space` to a "
                "small discrete grid {name: [values, ...]} (keep the total grid small, "
                "<= ~12 points; grid values must be NUMERIC — the schema rejects strings). "
                "The Developer then evaluates every grid point in ONE process "
                "(loading the data once), so a sweep is far cheaper than the same points run "
                "as separate nodes. Leave `space` empty for an ordinary single-config "
                "experiment; fixed/shared hyperparameters still go in `params`. ")
# P6: the per-experiment `eval_timeout` ask, shared by both researchers. Scoped HONESTLY: the
# engine consumes `idea.eval_timeout` only on the sandbox (script-solution) eval branch;
# repo/command-eval stages take their timeouts from the stage manifest / the task's cmd spec.
# The repo/command clause used to stop at "leave it null there", which wrongly read as "the
# time limit is not your concern" — repo agents then configured trainings that could not finish
# in the budget and were killed with no metric. It now states the limit is a HARD budget the
# experiment must be SIZED to fit (the live number + prior-node timings arrive via the engine's
# TIME-BUDGET proposal cue, engine/proposal_cues.py).
_EVAL_TIMEOUT_GUIDANCE = (
    "If THIS experiment is genuinely compute-heavy and needs more wall-clock than a "
    "light model — a neural network (CNN/RNN/transformer), a large ensemble, many CV "
    "folds/seeds, or a big grid — set `eval_timeout` to a realistic per-run budget in "
    "SECONDS (e.g. 300-1800). Leave it null for ordinary/light experiments so they use "
    "the run default. (`eval_timeout` sets the budget for script-solution tasks run in the "
    "sandbox; on repo/command tasks the per-stage limit instead comes from the stage manifest / "
    "the task's cmd — leave `eval_timeout` null there. But that per-stage limit is a HARD "
    "wall-clock budget: an experiment that does not finish within it is KILLED with NO metric, "
    "so SIZE the experiment to FIT — estimate total training steps x per-step time and prefer "
    "fewer epochs, a subsample, or a short probe run to measure per-step cost first; a smaller "
    "experiment that COMPLETES beats a bigger one that gets killed.) ")
# Hypothesis-card resource declaration (docs/23, Stage 1b). This is deliberately part of the
# code-owned capability suffix rather than either PromptStore default: both Researcher variants
# append that suffix after rendering an override, so a custom persona cannot hide this contract.
# The Developer may refine the estimate later; the Researcher owns only these quantitative keys.
_FOOTPRINT_GUIDANCE = (
    "Optionally set `footprint` to a JSON object describing this experiment's expected resources: "
    "{`gpus`: <non-negative integer>, `gpu_mem_mib`: <non-negative integer or null>}. Leave "
    "`footprint` null (or omit it) when GPU needs are UNSPECIFIED; unspecified is distinct from "
    "`gpus=1`. Use `gpus=0` only for a deliberately CPU-only experiment, and `gpus=1` only when "
    "the experiment specifically needs one GPU. Do not put `timeout`/`eval_timeout` or authority "
    "and provenance keys such as `proposed_by`, `finalized_by`, or `pinned_by` inside `footprint`; "
    "wall-clock stays in the top-level `eval_timeout`, and the engine/operator own authority fields. ")
# P14: the schema requires `operator` but the engine's policy overwrites it unconditionally
# (orchestrator's node-creation sites) — say so, in BOTH researcher prompts, so the model
# doesn't strategize around a dead field.
_OPERATOR_NOTE = ("The `operator` field is informational (an audit label): the engine's search "
                  "policy decides the node's actual operator. ")


def _researcher_capability_suffix(offer_sweep: bool) -> str:
    """P6: capability prose SHARED by both researchers (`LLMResearcher` here and agent.py's
    `ToolUsingResearcher`) so the two role variants can't drift apart again: the sweep offer
    (only when the active Developer implements `idea.space` — `make_roles` decides, see
    `_SWEEP_OFFER`) + the `eval_timeout` ask + the optional resource-footprint contract."""
    return ((_SWEEP_OFFER if offer_sweep else "") + _EVAL_TIMEOUT_GUIDANCE
            + _FOOTPRINT_GUIDANCE)


def _researcher_system(offer_sweep: bool = True) -> str:
    """Assemble the plain researcher's FULL system prompt (core + capability suffix + operator
    note + emit instruction) — a back-compat/reference assembly. The `researcher_system`
    PromptStore default is `_RESEARCHER_CORE` ALONE: `LLMResearcher.propose` appends the
    concept-authoring/capability fragments AFTER the render() (the same pattern as agent.py's
    `ToolUsingResearcher`), so the composed prompt stays byte-equal to this helper while a
    `researcher_system.md` override can never bypass the code-owned mode contract or `offer_sweep` gate. With
    `offer_sweep=True` this matches the historical `_RESEARCHER_SYSTEM` modulo the verified
    prompt fixes (P21 numeric-grid note, P6 eval_timeout scoping, Stage-1b footprint contract,
    P14 operator note)."""
    return (_RESEARCHER_CORE + _CONCEPT_AUTHORING_GUIDANCE
            + _researcher_capability_suffix(offer_sweep) + _OPERATOR_NOTE
            + "Respond ONLY with the requested structured fields.")


# Appended to the Researcher system prompt when hypothesis tracking is on (P1, default on). Split out
# so the knob can drop it cleanly (the `hypothesis` field then simply stays unset).
_HYPOTHESIS_INSTRUCTION = (
    "Set `hypothesis`: ONE plain-sentence statement of what this experiment TESTS — the belief you "
    "expect the result to support or refute (e.g. \"adding interaction features raises CV accuracy\", "
    "\"a deeper tree overfits this small dataset\"). Reuse the SAME wording when a later experiment "
    "tests the same belief, so the run builds a ledger of what's been learned.")


def _hypothesis_system_suffix(track_hypotheses: bool) -> str:
    """The system-prompt tail that asks for the per-experiment `hypothesis` (P1), or "" when the
    knob is off. Shared VERBATIM by BOTH researchers (`LLMResearcher` here and agent.py's
    `ToolUsingResearcher`) so the `"\\n" + _HYPOTHESIS_INSTRUCTION` splice lives in ONE place."""
    return ("\n" + _HYPOTHESIS_INSTRUCTION) if track_hypotheses else ""


# The "your idea space is the WHOLE experiment / the Developer owns HOW" guidance, as worded for
# LLMResearcher's per-turn USER message (it follows the rationale ask). A SECOND, deliberately
# DIFFERENT wording lives in agent.py's `ToolUsingResearcher._IDEA_SPACE_TOOL` (a system prompt).
# The two are NOT normalized — prompt strings are contracts and the phrasings have drifted — but
# both are named `_IDEA_SPACE_*` so `grep _IDEA_SPACE` surfaces the pair despite the byte drift.
_IDEA_SPACE_PLAIN = ("Your idea space is the whole "
                     "experiment: propose a parameter change OR a structural one "
                     "(architecture, loss, data, training) when that's the stronger "
                     "move — describe non-numeric changes in the rationale. You do not "
                     "write the code yourself (the Developer owns how, and may edit the "
                     "code to realise it), but you ARE free to direct code-level changes.")
_DEVELOPER_SYSTEM = ("You are an expert ML engineer. Output ONLY a single fenced "
                     "```python``` block containing a complete, self-contained script. "
                     # 1.3 consistent evaluation: every candidate must be measured on the SAME
                     # splits/seeds or their scores are incomparable noise (AIRA2: much apparent
                     # 'validation overfitting' was evaluation inconsistency). The engine varies
                     # the env var only in the confirm/holdout phases.
                     "Seed ALL randomness (train/validation splits, CV folds, model init, "
                     "subsampling) from int(os.environ.get('LOOPLAB_EVAL_SEED', '0')) so every "
                     "evaluation is reproducible and comparable across candidates. "
                     # #6: the eval has a STALL watchdog — a stage silent on the pipes for too long
                     # (block-buffered output, a slow-but-quiet loop) is tree-killed before its deadline.
                     "A stage that prints NOTHING to stdout/stderr for a long stretch may be killed early "
                     "as a STALL, so PRINT PERIODIC PROGRESS for any long loop — one flushed line per "
                     "epoch/step (e.g. `print(f'epoch {i} loss={loss}', flush=True)`) — to stay visibly "
                     "alive; a fully silent multi-minute phase risks a false kill. ")


def _developer_footprint_guidance(idea: Idea) -> str:
    """Code-owned prompt suffix for the optional Developer resource finalization marker."""
    proposed = normalize_researcher_footprint(getattr(idea, "footprint", None))
    if proposed is None:
        return ""
    payload = json.dumps(proposed, sort_keys=True, separators=(",", ":"))
    return (
        "\nThe Researcher proposed this resource footprint: " + payload + ". Size the implementation "
        "to that envelope. If the shipped code truly needs different quantities, put exactly one "
        "comment in the first 80 lines of the Python block as `# LOOPLAB_FOOTPRINT: {\"gpus\":N,"
        "\"gpu_mem_mib\":M}` (omit either optional key when unknown). This marker is metadata only; "
        "never put credentials, paths, commands, or prose in it. If the proposal is already accurate, "
        "you may omit the marker."
    )
# Appended to the Developer's system prompt when the Idea carries a `space` (intra-node sweep).
_SWEEP_CONTRACT = (
    "\nThis is an INTRA-NODE SWEEP: evaluate EVERY point of the given grid in ONE process — load "
    "the data ONCE and reuse it across all grid points. Report ALL results by printing, as the "
    "FINAL stdout line, a JSON object: {\"trials\": [{\"params\": {..}, \"metric\": <float>, "
    "\"seconds\": <float>, \"extra_metrics\": {..}}, ...]} — one entry per grid point. IF the "
    "`looplab` package is importable in the eval environment, the easiest way is "
    "`from looplab.sweep import run_sweep` and call run_sweep(space, train_fn) where "
    "train_fn(params, seed) returns the metric (it prints the required line for you); if it is "
    "NOT importable (a bare sandbox image), write the loop yourself — load the data ONCE, then "
    "iterate the grid — or use Optuna/GridSearchCV, always printing that exact final JSON "
    "`trials` line. If the task is host-graded (it asks you to write predictions/submission), "
    "write them for the SINGLE BEST grid point so the host can grade it.")


class Researcher(Protocol):
    def propose(self, state: RunState, parent: Optional[Node]) -> Idea: ...
    # OPTIONAL (Variant-1 Phase 2): a backend MAY expose `propose_batch(state, n) -> list[Idea]` that
    # returns up to N ideas on DISTINCT axes in one pass. The engine probes for it via getattr and, when
    # absent, degrades to N sequential `propose` calls with an avoidance directive (engine `_propose_batch`)
    # — so implementing it is a diversity/latency optimization, never required.


class Developer(Protocol):
    def implement(self, idea: Idea) -> str: ...


# Duck-typed OUTPUT attributes the engine reads off the ACTIVE Developer/Researcher after a
# call (docs/15 §P4.3) — the mirror of RESEARCHER_HINT_ATTRS for the outbound direction. The
# engine reads them with `getattr(..., default)`, so a one-sided rename historically failed
# SILENTLY (an empty node shipped with no diagnostic; the pilot quietly reverted to the static
# policy). `tests/test_role_output_contract.py` source-scans BOTH sides against these tuples:
# every consumer getattr and every producer assignment must use exactly these names, and every
# delegating wrapper (ValidatingDeveloper, best-of-N, the foresight panel) must forward them.
DEVELOPER_OUTPUT_ATTRS: tuple[str, ...] = (
    "last_files", "last_deleted", "last_footprint",
    # CLI-agent (ADR-7) developer outputs: the validation report the engine's audit emitter
    # reads (engine/audit.py `_emit_agent_report`), and the seed/process/patch evidence the
    # ValidatingDeveloper's checks consume. Surfaced by the contract test's own first run —
    # the original census had missed all four.
    "last_report", "last_seed", "last_run", "last_patch")
RESEARCHER_ACTION_ATTRS: tuple[str, ...] = ("choose_action",)

RESEARCHER_HINT_ATTRS: tuple[str, ...] = (
    "_digest_cap", "_complexity_hint", "_sweep_hint", "_novelty_feedback", "_novelty_hint",
    "_novelty_stance", "_hyp_order", "_steering_context")
"""Ephemeral hint attributes communicated to the ACTIVE Researcher via `setattr` and consumed
with `getattr(obj, name, default)`. Writers: the engine (`_digest_cap` in orchestrator.py
`__init__`; `_complexity_hint`/`_sweep_hint` in engine/proposal_cues.py `_set_complexity_hint`;
`_novelty_hint` + `_novelty_stance` in proposal_cues.py `_stamp_novelty_hint`;
`_novelty_feedback` in engine/novelty.py's gate) and the
foresight panel (search/foresight.py `_prioritize_board` sets `_hyp_order` — the predicted
best-first board order — on its wrapped researcher). Readers: `LLMResearcher.propose` (below)
and agent.py's `ToolUsingResearcher.propose` read the text cues and thread `_hyp_order` into
`_state_brief`; the foresight ranker reads `_novelty_stance` (the stance VALUE behind the
`_novelty_hint` prose).

THIS TUPLE IS THE DELIVERY CONTRACT (P2, docs/PROMPT_REVIEW.md): the engine setattrs hints on
the OUTERMOST active researcher, and EVERY wrapper that delegates propose() mirrors ONLY this
registry (plus the non-hint `track_hypotheses` knob) onto its delegate. The forwarding wrappers:
the foresight panel (`search/foresight.py::ForesightPanelResearcher._forward_hints`), the
`UnifiedAgent` facade (`agents/unified_agent.py::UnifiedAgent.propose`), the surrogate wrapper
(`search/surrogate.py::SurrogateResearcher.propose`), and the empirical panel
(`serve/panel.py::PanelResearcher.propose`). An attribute missing here silently dies at the
first wrapper — exactly how board prioritization was dead in the default config. Keep it in
sync with every `setattr(self.researcher, "...")` / `setattr(self.base, "...")` site;
tests/test_hint_forwarding.py scans those sites AND wires the real wrapper chains to enforce it.

Both researchers honor the same cues: `LLMResearcher.propose` and `ToolUsingResearcher.propose`
fold the same `(_complexity_hint, _sweep_hint, _novelty_feedback, _novelty_hint)` cue set into
their prompts (`_digest_cap` is consumed separately as a numeric cap; `_hyp_order` orders the
open-hypothesis board inside `_state_brief`)."""


def forward_hints(src, dst) -> None:
    """Mirror the engine-set ephemeral hints from a wrapper onto its delegate — the ONE owner of
    the `(*RESEARCHER_HINT_ATTRS, "track_hypotheses")` forwarding rule every wrapper shares.

    P2 delivery contract (see RESEARCHER_HINT_ATTRS above): the engine setattrs hints on the
    OUTERMOST active researcher — which may be any of the forwarding wrappers — so each wrapper
    mirrors the registry (plus the non-hint `track_hypotheses` knob, likewise poked onto the
    outermost object; an explicit OFF must not be shadowed) onto its delegate before delegating
    propose(). hasattr-guarded: an attr the engine never set is left untouched on `dst`. Without
    this, a wrapper silently dropped every engine hint on its delegation path. Callers:
    `UnifiedAgent.propose`, `ForesightPanelResearcher._forward_hints`,
    `SurrogateResearcher.propose`, and serve's `PanelResearcher.propose` — one helper, so the
    rule can't drift per-wrapper (tests/test_hint_forwarding.py wires the real chains)."""
    for attr in (*RESEARCHER_HINT_ATTRS, "track_hypotheses"):
        if hasattr(src, attr):
            setattr(dst, attr, getattr(src, attr))


def collect_hint_cues(obj, attrs) -> str:
    """Concatenate the given engine-set hint attributes (a subset of
    `RESEARCHER_HINT_ATTRS`) off `obj` in order, each defaulting to "" when unset — the
    shared rendering pattern the Researcher prompts use. Purely mechanical: byte-identical
    to the per-attribute `getattr(obj, name, "")` concatenation it replaces."""
    return "".join(getattr(obj, name, "") for name in attrs)


# --------------------------------------------------------------------------- #
# Toy backends (offline, deterministic given a seed)
# --------------------------------------------------------------------------- #

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

# Source-owned, exact GPU proof embedded only in maintainer calibration artifacts.  The rollout gate
# compares the shipped code prefix byte-for-byte with this constant and validates the four numeric
# metrics emitted on the objective's final JSON line.
SPECULATION_CUDA_PROBE_VERSION = 1
SPECULATION_CUDA_PROBE_ALLOC_BYTES = 4096
SPECULATION_CUDA_PROBE_DEVICE_ORDINAL = 0
SPECULATION_CUDA_PROBE_DEVICE_COUNT_METRIC = "device_count"
SPECULATION_CUDA_PROBE_EXTRA_METRIC_KEYS = (
    "speculation_cuda_probe_v",
    SPECULATION_CUDA_PROBE_DEVICE_COUNT_METRIC,
    "alloc_bytes",
    "device_ordinal",
)
SPECULATION_CUDA_PROBE_STATIC_EXTRA_METRICS = (
    ("speculation_cuda_probe_v", SPECULATION_CUDA_PROBE_VERSION),
    ("alloc_bytes", SPECULATION_CUDA_PROBE_ALLOC_BYTES),
    ("device_ordinal", SPECULATION_CUDA_PROBE_DEVICE_ORDINAL),
)

SPECULATION_CUDA_PROBE_CODE_PREFIX = '''\
# LOOPLAB_FOOTPRINT: {"gpus":1}
import ctypes as _looplab_ctypes
import os as _looplab_os
import sys as _looplab_sys

_looplab_cuda_probe_v = 1
_looplab_cuda_alloc_bytes = 4096
_looplab_cuda_device_ordinal = 0
if _looplab_os.name == "nt" and _looplab_sys.platform == "win32":
    _looplab_cuda = _looplab_ctypes.WinDLL("nvcuda.dll")
elif _looplab_sys.platform.startswith("linux"):
    _looplab_cuda = _looplab_ctypes.CDLL("libcuda.so.1")
else:
    raise RuntimeError("speculation calibration requires Windows or Linux CUDA Driver API")

def _looplab_cuda_symbol(*_looplab_names):
    for _looplab_name in _looplab_names:
        try:
            return getattr(_looplab_cuda, _looplab_name)
        except AttributeError:
            pass
    raise RuntimeError("CUDA driver is missing required symbol " + _looplab_names[0])

def _looplab_cuda_bind(_looplab_names, _looplab_argtypes):
    _looplab_function = _looplab_cuda_symbol(*_looplab_names)
    _looplab_function.restype = _looplab_ctypes.c_int
    _looplab_function.argtypes = _looplab_argtypes
    return _looplab_function

def _looplab_cuda_check(_looplab_result, _looplab_operation):
    if int(_looplab_result) != 0:
        raise RuntimeError(
            _looplab_operation + " failed with CUDA result " + str(int(_looplab_result)))

_looplab_cu_init = _looplab_cuda_bind(("cuInit",), [_looplab_ctypes.c_uint])
_looplab_cu_device_count = _looplab_cuda_bind(
    ("cuDeviceGetCount",), [_looplab_ctypes.POINTER(_looplab_ctypes.c_int)])
_looplab_cu_device_get = _looplab_cuda_bind(
    ("cuDeviceGet",),
    [_looplab_ctypes.POINTER(_looplab_ctypes.c_int), _looplab_ctypes.c_int])
_looplab_cu_ctx_create = _looplab_cuda_bind(
    ("cuCtxCreate_v2", "cuCtxCreate"),
    [_looplab_ctypes.POINTER(_looplab_ctypes.c_void_p),
     _looplab_ctypes.c_uint, _looplab_ctypes.c_int])
_looplab_cu_mem_alloc = _looplab_cuda_bind(
    ("cuMemAlloc_v2", "cuMemAlloc"),
    [_looplab_ctypes.POINTER(_looplab_ctypes.c_uint64), _looplab_ctypes.c_size_t])
_looplab_cu_mem_free = _looplab_cuda_bind(
    ("cuMemFree_v2", "cuMemFree"), [_looplab_ctypes.c_uint64])
_looplab_cu_ctx_destroy = _looplab_cuda_bind(
    ("cuCtxDestroy_v2", "cuCtxDestroy"), [_looplab_ctypes.c_void_p])

_looplab_cuda_count = _looplab_ctypes.c_int()
_looplab_cuda_device = _looplab_ctypes.c_int()
_looplab_cuda_context = _looplab_ctypes.c_void_p()
_looplab_cuda_pointer = _looplab_ctypes.c_uint64()
_looplab_cuda_failure = None
_looplab_cuda_cleanup_failures = []
try:
    _looplab_cuda_check(_looplab_cu_init(0), "cuInit")
    _looplab_cuda_check(
        _looplab_cu_device_count(_looplab_ctypes.byref(_looplab_cuda_count)),
        "cuDeviceGetCount")
    if _looplab_cuda_count.value <= 0:
        raise RuntimeError("speculation calibration requires a CUDA-visible device")
    _looplab_cuda_check(
        _looplab_cu_device_get(
            _looplab_ctypes.byref(_looplab_cuda_device), _looplab_cuda_device_ordinal),
        "cuDeviceGet")
    _looplab_cuda_check(
        _looplab_cu_ctx_create(
            _looplab_ctypes.byref(_looplab_cuda_context), 0, _looplab_cuda_device),
        "cuCtxCreate")
    if not _looplab_cuda_context.value:
        raise RuntimeError("cuCtxCreate returned a null context")
    _looplab_cuda_check(
        _looplab_cu_mem_alloc(
            _looplab_ctypes.byref(_looplab_cuda_pointer), _looplab_cuda_alloc_bytes),
        "cuMemAlloc")
    if not _looplab_cuda_pointer.value:
        raise RuntimeError("cuMemAlloc returned a null pointer")
except Exception as _looplab_cuda_caught:
    _looplab_cuda_failure = _looplab_cuda_caught
finally:
    if _looplab_cuda_pointer.value:
        try:
            _looplab_cuda_check(
                _looplab_cu_mem_free(_looplab_cuda_pointer), "cuMemFree")
        except Exception as _looplab_cuda_cleanup_caught:
            _looplab_cuda_cleanup_failures.append(str(_looplab_cuda_cleanup_caught))
    if _looplab_cuda_context.value:
        try:
            _looplab_cuda_check(
                _looplab_cu_ctx_destroy(_looplab_cuda_context), "cuCtxDestroy")
        except Exception as _looplab_cuda_cleanup_caught:
            _looplab_cuda_cleanup_failures.append(str(_looplab_cuda_cleanup_caught))
if _looplab_cuda_failure is not None:
    _looplab_cuda_suffix = (
        "; cleanup: " + "; ".join(_looplab_cuda_cleanup_failures)
        if _looplab_cuda_cleanup_failures else "")
    raise RuntimeError(
        "speculation calibration CUDA proof failed: "
        + str(_looplab_cuda_failure) + _looplab_cuda_suffix) from _looplab_cuda_failure
if _looplab_cuda_cleanup_failures:
    raise RuntimeError(
        "speculation calibration CUDA cleanup failed: "
        + "; ".join(_looplab_cuda_cleanup_failures))
_looplab_cuda_device_count_value = int(_looplab_cuda_count.value)

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


# --------------------------------------------------------------------------- #
# LLM-backed backends (I2, ADR-7/14). Same Protocols; swap-in needs no loop change.
# Tested against a fake LLMClient (no live calls); go-live needs a model endpoint.
# --------------------------------------------------------------------------- #


def _clamp_fill(idea: Idea, bounds: Optional[dict]) -> Idea:
    """Clamp numeric params into bounds and fill any missing ones with the midpoint, so
    a stray/empty proposal can't crash the objective. A SWEPT dimension (present in `idea.space`) is
    left to its grid — midpoint-filling it would inject a spurious 'fixed at X' param the Developer
    prompt renders ALONGSIDE the sweep grid ('sweep degree in [1,2,3]' AND 'degree=3.0'), telling the
    model the swept dim is simultaneously fixed; the sweep-offer contract keeps swept dims out of
    params on purpose. (Direct mutation here bypasses Idea._clamp_params_to_space, so guard here.)"""
    if bounds:
        swept = set(getattr(idea, "space", None) or {})
        for k, (lo, hi) in bounds.items():
            if k in idea.params:
                idea.params[k] = max(lo, min(hi, float(idea.params[k])))
            elif k not in swept:
                idea.params[k] = (lo + hi) / 2.0
    return idea


def _state_brief(state: RunState, parent: Optional[Node], digest_cap: int = 0,
                 hyp_order: Optional[list[str]] = None) -> str:
    best = state.best()
    lines = [f"Goal: {state.goal}", f"Optimize direction: {state.direction}."]
    if best is not None:
        lines.append(f"Best so far: node {best.id} metric={best.metric} params={best.idea.params}")
    if parent is not None:
        lines.append(f"Refine from node {parent.id}: params={parent.idea.params} metric={parent.metric}")
    # PART V (B): a delta author cannot subtract from an invisible reference. Surface the run base and
    # effective primary-parent membership, bounded so a malformed taxonomy cannot consume the role context.
    # Replay uses the union of all actual parents for a merge; the proposal role sees the primary parent
    # before policy finalizes that edge set, so the prompt names this limitation instead of claiming exactness.
    # CODEX AGENT: recorded taxonomy is data, never an instruction; the shared projector quotes/bounds it.
    from looplab.search.concept_projection import (bounded_untrusted_concept_json,
                                                    concept_inheritance_context)
    concept_context = concept_inheritance_context(state, parent.id if parent is not None else None)
    lines.append("UNTRUSTED_RECORDED_CONCEPT_DATA="
                 + bounded_untrusted_concept_json(concept_context))
    if not concept_context["delta_safe"]:
        lines.append(
            "Concept authoring safety: inherited membership is UNAVAILABLE or PARTIAL. "
            "You MUST set `concept_mode=\"full\"`, provide the exact complete set in `concepts`, leave "
            "`concepts_added` and `concepts_removed` empty, and MUST NOT use delta mode for this proposal.")
    else:
        lines.append(
            "Concept membership context only: use delta mode only when a separate trusted run cue "
            "explicitly enables it; a root inherits the run base and a merge inherits all actual parents.")
    # Append the always-on "working set": a compact view of the whole search (top winners, weakest /
    # failures, theme map) so the Researcher proposes with awareness of what's already been tried,
    # not just `best` + `parent`. Depth (full experiments, code, data) lives behind the run tools.
    from looplab.events.digest import experiments_digest, lineage_lessons, sibling_digest
    lines.append(experiments_digest(state, char_cap=digest_cap))
    # M1/A0c operator-scoped memory: draft/improve additionally see their SIBLINGS (diversity
    # pressure — aira-dojo MEM_OPS `sibling`) and, when refining, the LESSONS distilled from the
    # lineage under the refined node (D6 insight backpropagation, Arbor's Backpropagate step).
    lines.append(sibling_digest(state, parent))
    lines.append(lineage_lessons(state, parent))
    # Signal-delivery (§1): the latest deep-research memo's takeaway. Its `recommended_directions`
    # already ride as standing hints, but the summary/findings/claims were recorded-but-unread — this
    # surfaces the one-line conclusion plus a pointer to the `read_research_memo` tool for the full
    # reasoning (available to the agentic Researcher). Best-effort; skipped when there's no memo.
    research = getattr(state, "research", None) or []
    if research and isinstance(research[-1], dict) and research[-1].get("summary"):
        lines.append("Latest deep-research takeaway: "
                     + " ".join(str(research[-1]["summary"]).split())[:300]
                     # channel-neutral: a plain researcher has no tools, so state that the depth is
                     # recorded rather than commanding a `read_research_memo` call it can't make.
                     + " (full findings/claims are recorded; the read_research_memo tool returns them).")
    # P1: surface OPEN board hypotheses (human "+ Add" / deep-research directions) verbatim.
    # Without this the Researcher never sees them, and evidence only links when an experiment's
    # `hypothesis` matches the statement exactly — so board cards would stay "open" forever.
    # 1 card = 1 hypothesis: read the single Card board directly (open, no evidence yet). Card fields
    # shadow the old Hypothesis (verdict == status, id/evidence identical); `seed_statement == statement`
    # only until an operator edit or a merge diverges them.
    # This feed DELIBERATELY shows the immutable `seed_statement` (not the display `statement`) and asks
    # the model to copy it EXACTLY: evidence links only when the built node's `idea.hypothesis` matches the
    # card's SEED — `_derive_cards` bridges `hypothesis_id(seed)` to the owning card id via
    # owner_by_statement (that hash EQUALS the card id only for a legacy hypothesis-shadow card, NOT for a
    # native `card-N`). Copying an edited/merged `statement` would hash elsewhere, so no card owns it and it
    # gains no evidence. Consequence (by design): an operator statement edit changes render/analysis/
    # selection (which read `statement`) but NOT the seed the proposal feed asks the model to test — a
    # display/analysis edit, not a re-seed of the linkable research direction.
    # Distinct untested BELIEFS, not raw work-item cards (peer review): two cards that reuse the exact
    # hypothesis wording are ONE belief, surfaced once so the model does not re-read a duplicate.
    open_hyps = state.open_research_beliefs()
    if open_hyps:
        # FOREAGENT predict-before-execute (search/foresight.py): when the world model has ranked the
        # board by expected payoff (`hyp_order` = hypothesis ids best-first), surface the batch of
        # untested beliefs — which arrives from deep research / a human / the strategist — in that
        # predicted-value order, so the search tests the most promising one first and the [:5] cap now
        # drops the LOWEST-payoff cards, not arbitrary insertion-order ones. No ranking -> insertion
        # order (unchanged). Replay-safe: only the resulting node's `idea.hypothesis` is recorded.
        if hyp_order:
            pos = {hid: i for i, hid in enumerate(hyp_order)}
            open_hyps.sort(key=lambda h: pos.get(h.id, len(hyp_order)))
        lines.append("Untested hypotheses on the board (registered by the operator or deep research"
                     + (", ordered by predicted payoff — best first" if hyp_order else "")
                     + " — none has evidence yet):")
        lines.extend(f'- "{" ".join(h.seed_statement.split())[:200]}"' for h in open_hyps[:5])
        lines.append("If your next experiment tests one of these, copy its statement EXACTLY "
                     "(verbatim, unchanged wording) into `hypothesis` so the evidence links to "
                     "the board card.")
    return "\n".join(line for line in lines if line)


class LLMResearcher:
    """Proposes an `Idea` via structured output (tool_call default, baml fallback).

    `space_hint` describes the task's parameter space in the prompt; `bounds` clamps
    (and fills missing) numeric params so a small model's stray proposal can't crash
    the objective — quality robustness, not a correctness crutch."""

    def __init__(self, client: LLMClient, space_hint: str = "",
                 bounds: Optional[dict] = None, parser: str = "tool_call",
                 prompts: Optional[PromptStore] = None, track_hypotheses: bool = True,
                 offer_sweep: bool = True):
        self.client = client
        self.space_hint = space_hint
        self.bounds = bounds
        self.parser = parser
        self.prompts = prompts
        self.track_hypotheses = track_hypotheses   # P1: ask for the per-experiment hypothesis (default on)
        # P6: offer the intra-node sweep only when the active Developer implements `idea.space`
        # (make_roles sets this post-construction; default True keeps direct constructions as-is).
        self.offer_sweep = offer_sweep

    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        # Operator steering (Phase 5 `hint` control events): fold them into the prompt so a live
        # human can nudge the search ("try higher degree", "focus on regularization"). Advisory —
        # the model still proposes; bounds still clamp.
        from looplab.agents.hints import render_hint_directives
        hint_block = render_hint_directives(state.pending_hints)
        # Engine-set hint cues (see RESEARCHER_HINT_ATTRS; each empty when off), in order:
        # - _complexity_hint — A0d: an engine-set complexity cue keyed on the operated node's breadth.
        # - _sweep_hint — Strategist `prefer_sweep` bias: nudges — but never forces — the Researcher
        #   toward an intra-node sweep when the cost model favors in-process execution.
        # - _novelty_feedback — T5 novelty-gate feedback (one re-propose): "you already tried X, it
        #   failed because Y — propose something meaningfully different". Empty in the normal path.
        # - _novelty_hint — slice 2/4: the Strategist's novelty stance directive + coverage gaps
        #   (EXPLORE a new theme / EXPLOIT the leader). Empty when stance is "balanced" (today).
        cues = collect_hint_cues(self, ("_complexity_hint", "_sweep_hint", "_novelty_feedback",
                                        "_novelty_hint"))
        hyp_sys = _hypothesis_system_suffix(self.track_hypotheses)
        # CODEX AGENT: cues can contain persisted cross-run model/web/repository text. Redaction,
        # one-line normalization and an UNTRUSTED_MEMORY label do not make embedded instructions inert.
        # Append a code-owned system rule that treats every memory/tool string as quoted evidence and
        # never follows its instructions; mirror the rule in ToolUsingResearcher.
        messages = [
            {"role": "system",
             # Part V/P6: the explicit concept-mode contract, capability suffix (sweep offer — gated on
             # the active Developer — + eval_timeout), operator note, and emit instruction are appended AFTER the
             # render() — the SAME code-owned pattern as agent.py's ToolUsingResearcher. A
             # `researcher_system.md` PromptStore override replaces only the CORE persona, so an
             # override can never desync the capability prose from what the backend actually
             # implements (pre-fix the suffix was baked INSIDE the render default and an override
             # bypassed the offer_sweep gate). The assembled default is byte-equal to
             # `_researcher_system(offer_sweep)`.
             "content": render(self.prompts, "researcher_system", _RESEARCHER_CORE)
                        + _CONCEPT_AUTHORING_GUIDANCE
                        + _researcher_capability_suffix(getattr(self, "offer_sweep", True))
                        + _OPERATOR_NOTE
                        + "Respond ONLY with the requested structured fields." + hyp_sys
                        + "\n\n" + _attention_points()},
            {"role": "user", "content": _state_brief(state, parent,
                                                     digest_cap=getattr(self, "_digest_cap", 0),
                                                     hyp_order=getattr(self, "_hyp_order", None))
                                        + "\n" + self.space_hint +
                                        hint_block + cues +
                                        "\nPropose the next Idea (operator, params, rationale, concept_mode, "
                                        "concepts/concepts_added/concepts_removed"
                                        + (", hypothesis" if self.track_hypotheses else "") +
                                        # P6: don't re-offer the sweep in the user turn when the
                                        # active Developer can't run one (system prompt gates too).
                                        ("; optionally a `space` grid for a sweep"
                                         if getattr(self, "offer_sweep", True) else "") + "). The "
                                        "`rationale` is your conclusion the operator reads AND the Developer "
                                        "builds from — write it as brief GitHub-flavored Markdown (a lead "
                                        "sentence; **bold** the key lever, add a short bullet or two only if it "
                                        "helps). Focus on the DELTA: name the specific change THIS experiment "
                                        "makes and the intuition for why it should help — and SPECIFY that change "
                                        "completely enough for the Developer to build it (a structural change is "
                                        "often built from scratch, so include the essential setup it needs). Do "
                                        "NOT pad it with the parent's motivation or repeat reasoning you already "
                                        "wrote on earlier experiments — say what is NEW here, not the shared "
                                        "story. Keep it to ~1-3 sentences. " + _IDEA_SPACE_PLAIN
                                        + (" The `hypothesis` is the one-line belief this experiment "
                                           "tests (reuse wording across experiments that test the same "
                                           "belief)." if self.track_hypotheses else "")},
        ]
        # Small models occasionally emit unparseable output (the common case: a non-numeric `params`
        # value, which `Idea.params: dict[str, float]` rejects). Retry — but fold the parse error back
        # into the prompt first, so the retry ISN'T byte-identical (which deterministically re-fails);
        # then fall back to a safe default so one bad response never crashes the run.
        idea: Optional[Idea] = None
        last: Optional[Exception] = None
        for _attempt in range(2):
            try:
                # CODEX AGENT: modern model output must choose full vs delta explicitly. The durable
                # Idea reader stays tolerant for historical/future logs, so writers cross this boundary.
                parsed = parse_structured(self.client, messages, IdeaEmission, self.parser)
                # Preserve the long-standing injectable parser seam used by custom integrations/test
                # doubles: the real parser returns IdeaEmission, while a trusted adapter may return Idea.
                idea = parsed.to_idea() if isinstance(parsed, IdeaEmission) else Idea.model_validate(parsed)
                break
            except ParseError as e:
                last = e
                messages = messages + [{"role": "user", "content":
                    f"Your last response could not be parsed ({str(e)[:180]}). Emit the Idea again with "
                    "NUMERIC `params` only (put any non-numeric/structural change in `rationale`), a "
                    "valid `operator`, and a `rationale`."}]
        if idea is None:
            idea = Idea(operator="draft", params={}, rationale=f"fallback (parse failed: {last})")
        return _clamp_fill(idea, self.bounds)


class LLMDeveloper:
    """Writes (and repairs) a complete runnable solution script. `brief` carries the
    task's I/O contract (where to read data, what metric to print). `repair` powers the
    error-feedback debug operator: it gets the failing code + stderr and fixes it."""

    # T8/A0b: this Developer generates real code, so merge_mode="auto" resolves to the
    # code-recombination ensemble merge (the verified strongest operator) instead of mean-params.
    is_code_generating = True

    def __init__(self, client: LLMClient, brief: str = "",
                 prompts: Optional[PromptStore] = None):
        self.client = client
        self.brief = brief
        self.prompts = prompts
        self.last_footprint: dict | None = None

    def implement(self, idea: Idea) -> str:
        system = (render(self.prompts, "developer_system", _DEVELOPER_SYSTEM) + self.brief
                  + _developer_footprint_guidance(idea) + "\n\n" + _attention_points())
        # Render whatever params the task's Researcher proposed (task-agnostic): degree/lam
        # for regression, k for mlebench, etc. — hardcoding names dropped the value on
        # tasks that use a different hyperparameter.
        params = ", ".join(f"{k}={v}" for k, v in idea.params.items()) or "(model defaults)"
        if idea.space:
            # Intra-node sweep: render the grid and append the trials-reporting contract to the
            # system prompt so the Developer runs every point in one process and reports them all.
            system += _SWEEP_CONTRACT
            grid = "; ".join(f"{k} in {v}" for k, v in idea.space.items())
            fixed = f" Fixed/shared params: {params}." if idea.params else ""
            user = (f"Run an intra-node sweep over the grid: {grid}.{fixed} {idea.rationale}").strip()
        else:
            user = (f"Experiment concept (the researcher's idea): {idea.rationale}\n"
                    f"Parameters: {params}.\n"
                    "You own the implementation: design and write the solution code that realises "
                    "this concept.").strip()
        code = extract_code(self.client.complete_text(
            [{"role": "system", "content": system}, {"role": "user", "content": user}]))
        self.last_footprint = developer_artifact_footprint(idea.footprint, code)
        return code

    def repair(self, idea: Idea, code: str, error: str) -> str:
        # P8: the hardware/operational cues reach repair too (a timeout/oom repair NEEDS the real
        # GPU/CPU picture to size the cheaper retry) — appended after the render() calls, same as
        # the implement path.
        system = (render(self.prompts, "developer_repair_prefix", "You are an expert Python debugger. ") +
                  render(self.prompts, "developer_system", _DEVELOPER_SYSTEM) + self.brief
                  + _developer_footprint_guidance(idea) + "\n\n" + _attention_points())
        user = ("The script below failed. Return a corrected, complete script that runs "
                "and prints the required JSON metric line.\n\n--- SCRIPT ---\n" + code +
                "\n\n--- ERROR (stderr tail) ---\n" + error)
        # Include the idea rationale — the ValidatingDeveloper folds the validator's rejection feedback
        # into it on each retry, so without this the retry re-sends a byte-identical prompt and
        # deterministically re-fails, burning every attempt.
        if idea is not None and getattr(idea, "rationale", ""):
            user += "\n\n--- ADDITIONAL GUIDANCE ---\n" + idea.rationale
        repaired = extract_code(self.client.complete_text(
            [{"role": "system", "content": system}, {"role": "user", "content": user}]))
        self.last_footprint = developer_artifact_footprint(idea.footprint, repaired)
        return repaired


# --------------------------------------------------------------------------- #
# Wrapper-forwarding mixin: the Developer-wrapper contract, documented once
# --------------------------------------------------------------------------- #


class WrapsDeveloper:
    """Forwarding half of the Developer-WRAPPER contract (`ValidatingDeveloper`,
    `BestOfNDeveloper`, `UnifiedAgent`). A wrapper composes an inner Developer and must stay
    transparent to every duck-typed probe the engine/factories make against `developer`:

    - `inner` (plain attribute, set by the wrapper's ``__init__``): the wrapped Developer.
      The engine's ablation probe reads ``getattr(developer, "inner", developer)`` to bypass
      wrapper retry/fallback/best-of-N machinery (``orchestrator._probe_developer``), so
      `inner` must always be the raw developer a probe should hit.
    - `brief` / `is_code_generating` / `client` / `prompts` / `last_report`: read-through
      (and, for `client`/`prompts`, hasattr-guarded write-through) to the wrapped developer —
      `make_roles` pokes `prompts`, H3 per-role rewiring pokes `client`, T8/A0b
      merge_mode="auto" resolution reads `is_code_generating`, and the orchestrator reads
      `last_report` for the `agent_validated` audit event.
    - `last_files` / `last_deleted` / `last_footprint`: per-call output attributes the orchestrator
      reads AFTER implement/repair. Wrappers own them as plain attributes: either mirrored from the
      wrapped developer via `_sync_audit()`, or set by the wrapper's own logic (e.g.
      best-of-N's chosen candidate, the validator's fell-back handling).
    - `audit_extra()`: wrapper-specific audit fields merged into the `agent_validated` event.

    Delegation target: `_wrapped` (defaults to `inner`). `UnifiedAgent` overrides it — its
    delegate is `self.developer` (possibly itself a wrapper) while its `inner` exposes the
    fully-unwrapped probe developer.

    A wrapper whose semantics for a member differ from these defaults keeps that member local
    (e.g. `ValidatingDeveloper`'s unconditional `prompts` setter, its agent-vs-fallback
    `is_code_generating`/`last_report`, and `UnifiedAgent`'s locally-held `prompts` handle).
    """

    @property
    def _wrapped(self):
        return self.inner

    # forward the hooks make_roles / the engine poke at, to the wrapped developer
    @property
    def brief(self) -> str:
        return getattr(self._wrapped, "brief", "")

    # T8/A0b: capability follows the wrapped developer (merge_mode="auto" resolution)
    @property
    def is_code_generating(self) -> bool:
        return bool(getattr(self._wrapped, "is_code_generating", False))

    @property
    def client(self):
        return getattr(self._wrapped, "client", None)

    @client.setter
    def client(self, value) -> None:        # H3 per-role client rewiring reaches the inner developer
        if hasattr(self._wrapped, "client"):
            self._wrapped.client = value

    @property
    def prompts(self):
        return getattr(self._wrapped, "prompts", None)

    @prompts.setter
    def prompts(self, value) -> None:
        if hasattr(self._wrapped, "prompts"):
            self._wrapped.prompts = value

    @property
    def last_report(self):
        return getattr(self._wrapped, "last_report", None)

    def audit_extra(self) -> dict:
        fn = getattr(self._wrapped, "audit_extra", None)
        return fn() if callable(fn) else {}

    def _sync_audit(self) -> None:
        """Mirror the wrapped developer's per-call files and resource estimate onto this wrapper."""
        self.last_files = getattr(self._wrapped, "last_files", {}) or {}
        self.last_deleted = getattr(self._wrapped, "last_deleted", []) or []
        self.last_footprint = getattr(self._wrapped, "last_footprint", None)


# --------------------------------------------------------------------------- #
# Validating wrapper (ADR-7): audit how an external coding agent performed
# --------------------------------------------------------------------------- #


class ValidatingDeveloper(WrapsDeveloper):
    """Wrap a Developer and validate how it performed before the orchestrator spends a
    sandbox evaluation on its output (see `validate.py`).

    On an invalid result it re-prompts the *inner* developer with the failure folded
    into the Idea's rationale (a cheap correction loop), up to `max_retries` times. If it
    still can't produce valid code it falls back to `fallback` (typically the in-house
    `LLMDeveloper`, the known-good path) — so a flaky external agent degrades to a
    working developer instead of poisoning the search with a no-op/broken node.

    `last_report` holds the `AgentReport` for the most recent call; the orchestrator logs
    it as an `agent_validated` event, giving a per-node audit trail of the agent.

    Tool-agnostic: any Developer works as `inner`. If `inner` exposes `last_run` /
    `last_seed` (as `CliAgentDeveloper` does), the report also includes process-level
    checks (launched / not-timed-out / exit) and the no-op (`modified_seed`) check.
    """

    # `last_report` is genuinely LOCAL state — it always describes the EXTERNAL AGENT (even
    # when we fall back) — so shadow the mixin's live forwarder with a plain attribute.
    last_report: Optional[AgentReport] = None

    def __init__(self, inner, *, fallback=None, max_retries: int = 1,
                 metric_key: str = "metric", repo_mode: bool = False):
        self.inner = inner
        self.fallback = fallback
        self.max_retries = max_retries
        self.metric_key = metric_key
        # repo_mode: validate the agent's changed-FILE set (RepoTask), and treat the
        # fallback (a baseline / no-op developer) as always shippable — running the
        # unmodified repo is a valid result, not a failure.
        self.repo_mode = repo_mode
        # Audit of the most recent call. `last_report` always describes the EXTERNAL
        # AGENT (even when we fall back) — that's what we're auditing; the fallback's
        # validity is recorded separately in `last_shipped_ok`.
        self.last_report: Optional[AgentReport] = None
        self.last_attempts: int = 0
        self.last_fell_back: bool = False
        self.last_shipped_ok: bool = False
        self.last_files: dict[str, str] = {}   # multi-file output of the shipped attempt
        self.last_deleted: list[str] = []      # accepted in-surface deletions of the shipped attempt
        self.last_footprint: Optional[dict] = None  # resource estimate of the attempt that shipped

    # T8/A0b: code-generation capability follows the INNER developer (the fallback is LLM anyway)
    # — kept local: unlike the mixin's forwarder, the fallback's capability counts too.
    @property
    def is_code_generating(self) -> bool:
        return bool(getattr(self.inner, "is_code_generating", False)
                    or getattr(self.fallback, "is_code_generating", False))

    # forward the prompt hook make_roles pokes at, to the wrapped developer — kept local:
    # the setter is UNCONDITIONAL (it must create the attribute on an inner that lacks one),
    # unlike the mixin's hasattr-guarded write-through.
    @property
    def prompts(self):
        return getattr(self.inner, "prompts", None)

    @prompts.setter
    def prompts(self, value) -> None:
        self.inner.prompts = value

    def _report(self, code: str, *, agent: bool) -> AgentReport:
        """Validate `code`. `agent=True` pulls the inner agent's process signal + seed
        (no-op detection) + patch-gate verdict; `agent=False` (fallback output) does
        static checks only."""
        return validate_agent_code(
            code,
            seed=getattr(self.inner, "last_seed", None) if agent else None,
            run=getattr(self.inner, "last_run", None) if agent else None,
            patch=getattr(self.inner, "last_patch", None) if agent else None,
            files=(getattr(self.inner, "last_files", {}) or {}) if (agent and self.repo_mode)
                  else None,
            metric_key=self.metric_key,
        )

    def _record(self, report: AgentReport, *, attempts: int, fell_back: bool,
                shipped_ok: bool) -> None:
        self.last_report = report
        self.last_attempts = attempts
        self.last_fell_back = fell_back
        self.last_shipped_ok = shipped_ok
        # Multi-file output only when the agent itself shipped; the LLM fallback is
        # single-file (its code goes to solution.py via node.code).
        self.last_files = ({} if fell_back
                           else dict(getattr(self.inner, "last_files", {}) or {}))
        self.last_deleted = ([] if fell_back
                             else list(getattr(self.inner, "last_deleted", []) or []))
        # This output must describe the implementation that actually ships. In particular, a
        # rejected agent attempt must not leak its resource estimate onto fallback code.
        shipped = self.fallback if fell_back else self.inner
        self.last_footprint = getattr(shipped, "last_footprint", None)

    def _attempt_loop(self, idea: Idea, call, fallback_call=None) -> str:
        """Run `call(idea)` (implement or repair), validate, retry-with-feedback up to
        `max_retries`, then fall back via `fallback_call` (defaults to the fallback's
        implement). Records the agent audit on every path."""
        code, report = "", AgentReport()
        attempt = idea
        attempts = 0
        for _ in range(self.max_retries + 1):
            attempts += 1
            code = call(attempt)
            report = self._report(code, agent=True)
            if report.ok:
                self._record(report, attempts=attempts, fell_back=False, shipped_ok=True)
                return code
            attempt = idea.model_copy(deep=True)   # re-prompt with the failure as a hint
            attempt.rationale = (
                idea.rationale +
                f"\n[validator] the previous attempt was rejected: {report.feedback()}. "
                "Fix this in your changed files (the solution script / the repo files you edited).").strip()
        if self.fallback is not None:              # exhausted retries -> known-good path
            fb = (fallback_call or (lambda: self.fallback.implement(idea)))()
            # In repo mode the fallback is the baseline (no-op) developer — running the
            # unmodified repo is always a valid shippable result.
            fb_ok = True if self.repo_mode else self._report(fb, agent=False).ok
            # last_report still describes the AGENT (it failed); shipped code is the LLM's
            self._record(report, attempts=attempts, fell_back=True, shipped_ok=fb_ok)
            return fb
        self._record(report, attempts=attempts, fell_back=False, shipped_ok=report.ok)
        return code

    def audit_extra(self) -> dict:
        """Wrapper-specific audit fields merged into the `agent_validated` event so the
        log shows whether the agent succeeded, how many tries it took, and whether we had
        to fall back to the LLM developer."""
        return {"attempts": self.last_attempts, "fell_back": self.last_fell_back,
                "shipped_ok": self.last_shipped_ok}

    def implement(self, idea: Idea) -> str:
        return self._attempt_loop(idea, self.inner.implement)

    def repair(self, idea: Idea, code: str, error: str) -> str:
        inner_repair = getattr(self.inner, "repair", None)
        if not callable(inner_repair):
            return self.implement(idea)
        # Fall back to the fallback's repair (preserving the error-feedback) when it has
        # one, else its implement — never lose the debug context on the fallback path.
        fb_repair = getattr(self.fallback, "repair", None) if self.fallback else None
        fb_call = (lambda: fb_repair(idea, code, error)) if callable(fb_repair) else None
        return self._attempt_loop(idea, lambda i: inner_repair(i, code, error), fb_call)

    def implement_from(self, idea: Idea, parent) -> str:
        """Parent-aware implement, forwarded through the validation retry loop (arch-review §4 P1-9):
        without exposing this, the engine's `getattr(developer, 'implement_from')` capability probe
        saw only the validator's plain `implement` and regenerated the child from the pristine baseline
        (losing the parent's accumulated edits). Degrades to `implement` when the inner has no
        parent-aware path."""
        impl_from = getattr(self.inner, "implement_from", None)
        if not callable(impl_from):
            return self.implement(idea)
        return self._attempt_loop(idea, lambda i: impl_from(i, parent))

    def repair_from(self, idea: Idea, node, error: str) -> str:
        """Node-aware repair, forwarded like `implement_from` — seed the fix from the FAILING node's own
        files. Falls back to the fallback's repair_from/repair, preserving the error feedback."""
        rf = getattr(self.inner, "repair_from", None)
        if not callable(rf):
            return self.repair(idea, getattr(node, "code", ""), error)
        fb_rf = getattr(self.fallback, "repair_from", None) if self.fallback else None
        fb_call = (lambda: fb_rf(idea, node, error)) if callable(fb_rf) else None
        return self._attempt_loop(idea, lambda i: rf(i, node, error), fb_call)
