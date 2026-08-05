"""How the inline-repair budget is APPORTIONED, and the missing-library case that made it urgent.

Measured live on `runs/rubert-dr-0805` node 0 (`inline_repair_attempts: 6`, deepseek-v4-flash):

    attempt  what it fixed                                          triage rationale said
    1        pytorch_lightning.utilities.cloud_io gone in PL 2.x     "mechanical import error"
    2        NameError: init_empty_weights (accelerate absent)       "mechanical … version mismatch"
    3        NameError: find_tied_parameters (same cause)            "library version/import bug"
    4        TensorBoardLogger, tensorboard absent                   "mechanical missing-library crash"
    5        replace_sampler_ddp removed in PL 2.x                   "mechanical API break"
    6        validation_epoch_end removed in PL 2.0                  "mechanical … v2 API incompatibility"
    —        DDP find_unused_parameters — a MODELLING question       node_failed, budget exhausted

Every attempt reconciled a year-stale repo with today's site-packages, and the first real research
question arrived with nothing left. Raising the number is not the fix (the same node did 2345
repairs before the bound existed): the fix is that reconciling code with the environment and
changing the experiment are different resources. `inline_repair_attempts` now bounds EACH ledger
(`engine/evaluate.py`), an exemption needs the agent's structured `repair_class`, the engine's own
reading of the traceback (`engine/triage.py::_environment_failure`) and FORWARD PROGRESS to agree,
and the same guards that closed the 2345-repair runaway still bound the whole loop.

Second defect, same area, same run: a missing dependency a library degrades into a NameError is
invisible to `auto_install_deps`, which keys on the traceback naming the module. `transformers`
guards `init_empty_weights` behind `is_accelerate_available()`, so an absent `accelerate` — already
on the install allowlist — surfaced with its name nowhere in the exception. Attempts 2 and 3 both
went to it; the agent diagnosed it correctly both times and could not act.

The errors, the rationales and the settings below are VERBATIM from that run's `events.jsonl` /
`nodes/node_0/train.log`; the tests drive the REAL `_evaluate` loop over them (only the solution
source and the triage verdict are scripted), so they exercise the actual guards.
"""
from __future__ import annotations

import anyio
import pytest

from looplab.adapters.toytask import ToyTask
from looplab.core.models import Idea, NodeStatus
from looplab.engine.orchestrator import Engine
from looplab.engine.triage import (DEFAULT_REPAIR_CLASS, REPAIR_CLASSES, _environment_failure,
                                   _rule_triage, coerce_repair_class)
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.runtime import deps
from looplab.runtime.deps import InstallResult
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree
# The real symbol pool from the 3.5 h / 2345-repair runaway — reused, not re-invented, so the
# bounding proof below is about the same incident that test file documents.
from tests.test_repair_runaway_guard import _REAL_SYMBOLS, _lazy_import_src

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"

_GOOD = "import json; print(json.dumps({'metric': 0.1}))\n"

# --- VERBATIM from `runs/rubert-dr-0805` -------------------------------------------------------
# `_T<n>` is the `error_in` of that run's `node_repaired` #n (the engine's own 500-char stderr
# tail); `_R<n>` is the triage rationale it recorded alongside. `_DDP` is the `node_failed`
# error — the research question the node reached with nothing left to spend.
# attempt 1: pytorch_lightning.utilities.cloud_io gone in PL 2.x
_T1 = ('05/nodes/node_0/train.py", line 23, in <module>\n    from model import BertDSSMExtractCL'
       'S\n  File "/home/jovyan/data/looplab/runs/rubert-dr-0805/nodes/node_0/model.py", line 20'
       ', in <module>\n    from utils import load_by_all_means, load_state_dict_by_all_means\n  '
       'File "/home/jovyan/data/looplab/runs/rubert-dr-0805/nodes/node_0/utils.py", line 6, in <'
       'module>\n    from pytorch_lightning.utilities.cloud_io import get_filesystem\nModuleNotF'
       'oundError: No module named \'pytorch_lightning.utilities.cloud_io\'\n')
_R1 = ('The crash is a mechanical import error: `pytorch_lightning.utilities.cloud_io` no longer'
       ' exists in the installed Lightning version; the fix is to import `get_filesystem` from `'
       'lightning_fabric.utilities.cloud_io`, leaving the fine-tuning idea intact.')
# attempt 2: accelerate absent -> transformers' guarded symbol is undefined
_T2 = ('mers/modeling_utils.py", line 4333, in from_pretrained\n    model_init_context = cls.get'
       '_init_context(is_quantized, _is_ds_init_called)\n                         ^^^^^^^^^^^^^^'
       '^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n  File "/opt/conda/lib/python3.11/site-package'
       's/transformers/modeling_utils.py", line 3736, in get_init_context\n    init_contexts = ['
       'no_init_weights(), init_empty_weights()]\n                                        ^^^^^^'
       '^^^^^^^^^^^^\nNameError: name \'init_empty_weights\' is not defined\n')
_R2 = ('This is a mechanical transformers/accelerate version mismatch (NameError: init_empty_wei'
       'ghts) in the from_pretrained model-loading path, not a flaw in the NV-Retriever false-ne'
       'gative-masking idea — fix the loading call (remove low_cpu_mem_usage/device_map or ensur'
       'e accelerate import) and re-run.')
# attempt 3: the SAME absent accelerate, one symbol later
_T3 = ('n3.11/site-packages/transformers/modeling_utils.py", line 4659, in _load_pretrained_mode'
       'l\n    missing_keys, unexpected_keys = _find_missing_and_unexpected_keys(\n             '
       '                       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n  File "/opt/conda/lib/python'
       '3.11/site-packages/transformers/modeling_utils.py", line 1352, in _find_missing_and_unex'
       'pected_keys\n    tied_params = find_tied_parameters(model)\n                  ^^^^^^^^^^'
       '^^^^^^^^^^\nNameError: name \'find_tied_parameters\' is not defined\n')
_R3 = ('NameError for find_tied_parameters inside transformers.modeling_utils is a library versi'
       'on/import bug, not a flaw in the loss idea — patch the missing symbol (e.g. `import tran'
       'sformers.modeling_utils as mu; from transformers.utils import find_tied_parameters; mu.f'
       'ind_tied_parameters = find_tied_para')
# attempt 4: tensorboard absent, named only in prose by the library's own error
_T4 = ('orch_lightning/loggers/tensorboard.py", line 96, in __init__\n    super().__init__(\n  F'
       'ile "/opt/conda/lib/python3.11/site-packages/lightning_fabric/loggers/tensorboard.py", l'
       'ine 93, in __init__\n    raise ModuleNotFoundError(\nModuleNotFoundError: Neither `tenso'
       'rboard` nor `tensorboardX` is available. Try `pip install`ing either.\nRequirement \'ten'
       'sorboardX\' not met. HINT: Try running `pip install -U \'tensorboardX\'`\nRequirement \''
       'tensorboard\' not met. HINT: Try running `pip install -U \'tensorboard\'`\n')
_R4 = ("Mechanical missing-library crash: the Lightning trainer uses a TensorBoardLogger but ten"
       "sorboard/tensorboardX aren't installable offline. Fix by switching to CSVLogger (or disa"
       "bling the logger) so training runs without the unavailable dependency — the dense-retrie"
       "val fine-tuning idea itself is sound")
# attempt 5: replace_sampler_ddp removed in PL 2.x
_T5 = ('b/runs/rubert-dr-0805/nodes/node_0/train.py", line 461, in <module>\n    main(get_args()'
       ')\n  File "/home/jovyan/data/looplab/runs/rubert-dr-0805/nodes/node_0/train.py", line 43'
       '6, in main\n    trainer = Trainer(\n              ^^^^^^^^\n  File "/opt/conda/lib/pytho'
       'n3.11/site-packages/pytorch_lightning/utilities/argparse.py", line 70, in insert_env_def'
       'aults\n    return fn(self, **kwargs)\n           ^^^^^^^^^^^^^^^^^^\nTypeError: Trainer.'
       '__init__() got an unexpected keyword argument \'replace_sampler_ddp\'\n')
_R5 = ("Mechanical API break: `replace_sampler_ddp` was removed in the installed PyTorch Lightni"
       "ng version; dropping the arg (single-GPU training doesn't need it) fixes the crash witho"
       "ut changing the approach.")
# attempt 6: validation_epoch_end removed in PL 2.0
_T6 = ('lib/python3.11/site-packages/pytorch_lightning/trainer/configuration_validator.py", line'
       ' 84, in __verify_train_val_loop_configuration\n    raise NotImplementedError(\nNotImplem'
       'entedError: Support for `validation_epoch_end` has been removed in v2.0.0. `LitModel` im'
       'plements this method. You can use the `on_validation_epoch_end` hook instead. To access '
       'outputs, save them in-memory as instance attributes. You can find migration examples in '
       'https://github.com/Lightning-AI/pytorch-lightning/pull/16520.\n')
_R6 = ("The crash is a mechanical PyTorch Lightning v2 API incompatibility (validation_epoch_end"
       " removed in v2.0.0, replaced by on_validation_epoch_end); the underlying fine-tuning ide"
       "a is sound, so I'll migrate the hook and re-run in place.")
# …and the first genuine research question: a MODELLING decision about which parameters
# participate in the loss, raised inside torch's DDP.
_DDP = ("er._rebuild_buckets():\n[rank0]:                                    ^^^^^^^^^^^^^^^^^^^^"
        "^^^^^^^^^^^\n[rank0]: RuntimeError: It looks like your LightningModule has parameters th"
        "at were not used in producing the loss returned by training_step. If this is intentional"
        ", you must enable the detection of unused parameters in DDP, either by setting the strin"
        "g value `strategy='ddp_find_unused_parameters_true'` or by setting the flag in the strat"
        "egy with `strategy=DDPStrategy(find_unused_parameters=True)`.\n")

# The six migrations, in the order the run hit them: (marker, stderr tail, triage rationale).
# `marker` is a substring unique to that failure — the scripted triage keys on it exactly as an
# agent keys on the traceback it is reading.
_SEQUENCE = [
    ("cloud_io", _T1, _R1),
    ("init_empty_weights", _T2, _R2),
    ("find_tied_parameters", _T3, _R3),
    ("tensorboardX", _T4, _R4),
    ("replace_sampler_ddp", _T5, _R5),
    ("validation_epoch_end", _T6, _R6),
]
# The research question the node never got to ask, and a second one behind it: a repair here is
# EXPERIMENT work — it changes what the model does, not what the library is called.
_DDP_RATIONALE = ("DDP reports unused parameters — decide whether the pooler/head should "
                  "participate in the loss, or enable find_unused_parameters.")
_SECOND_QUESTION = ("[rank0]: RuntimeError: Expected to have finished reduction in the prior "
                    "iteration before starting a new one.\n")


def _emits(tail: str, installed_flag=None) -> str:
    """A solution whose failure IS the recorded one: it writes the verbatim stderr tail and exits
    non-zero, so the engine's `_failure_reason`/`_normalize_error_sig`/`_environment_failure` all
    see exactly the bytes the live run gave them.

    With `installed_flag` it also behaves like the real thing when the MISSING LIBRARY arrives: the
    injected installer touches that path, and the very same UNCHANGED source then runs clean — which
    is the property the install path has to have (no repair, no new code, just a re-run)."""
    guard = (f"import os\nif os.path.exists({str(installed_flag)!r}):\n"
             "    import json; print(json.dumps({'metric': 0.1})); raise SystemExit(0)\n"
             if installed_flag is not None else "")
    return f"{guard}import sys\nsys.stderr.write({tail!r})\nsys.exit(1)\n"


class _ReplayDev:
    """Ships the NEXT failure in the recorded sequence on every repair (the live node's shape: each
    fix uncovered the next incompatibility), then a working script."""

    def __init__(self, tails: list[str], installed_flag=None):
        self.tails = list(tails)
        self.installed_flag = installed_flag
        self.repair_calls = 0
        self.errors: list[str] = []          # what each repair was handed — the node's real progress

    def implement(self, idea):
        return _emits(self.tails[0], self.installed_flag)

    def repair(self, idea, code, error):
        self.errors.append(error)
        self.repair_calls += 1
        nxt = self.tails[self.repair_calls] if self.repair_calls < len(self.tails) else None
        return _emits(nxt, self.installed_flag) if nxt else _GOOD


class _RegistryWalkDev:
    """The 2345-repair runaway's own developer: every attempt crashes inside the broken lazy-import
    registry naming a DIFFERENT symbol — same exception, same template, same frame. Sources, not
    stderr emitters, because `_lazy_import_src` is the reproduction that file already owns."""

    def __init__(self):
        self.repair_calls = 0
        self.errors: list[str] = []

    def implement(self, idea):
        return _lazy_import_src(_REAL_SYMBOLS[0])

    def repair(self, idea, code, error):
        self.errors.append(error)
        self.repair_calls += 1
        return _lazy_import_src(_REAL_SYMBOLS[self.repair_calls % len(_REAL_SYMBOLS)])


class _ReplayTriage:
    """The crash-triage agent, scripted from the run's own verdicts. `verdicts` maps a marker in the
    error text to the emitted verdict; anything unmatched is the research question's verdict."""

    def __init__(self, verdicts: dict, default: dict):
        self.verdicts = dict(verdicts)
        self.default = dict(default)
        self.calls: list[str] = []

    def propose(self, state, parent):
        return Idea(operator="x", params={"x": 1.0, "y": 1.0})

    def triage_crash(self, node, error, attempt, *, state=None, brief=""):
        self.calls.append(error)
        for marker, verdict in self.verdicts.items():
            if marker in error:
                return dict(verdict)
        return dict(self.default)


def _drive(tmp_path, dev, researcher, **kw):
    """Seed one node and run the REAL repair loop over it. The live settings unless overridden."""
    kw.setdefault("auto_install_deps", False)
    kw.setdefault("inline_repair", True)
    kw.setdefault("inline_repair_attempts", 6)        # the run's own setting
    kw.setdefault("inline_repair_stuck_repeat", 4)    # the run's own setting
    run_dir = tmp_path / "run"
    eng = Engine(run_dir, task=ToyTask.load(TASK), researcher=researcher, developer=dev,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1), **kw)
    eng.store.append("run_started",
                     {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
        "code": dev.implement(None)})

    async def _bounded() -> bool:
        # A hard wall so a REGRESSION fails instead of hanging CI (the pre-guard loop was unbounded).
        with anyio.move_on_after(180) as scope:
            await eng._evaluate(0, anyio.CapacityLimiter(1), None)
        return scope.cancelled_caught

    assert not anyio.run(_bounded), "the inline-repair loop did not terminate"
    return list(EventStore(run_dir / "events.jsonl").read_all()), eng


def _repairs(evs):
    return [e for e in evs if e.type == "node_repaired" and e.data.get("node_id") == 0]


def _classes(evs):
    return [e.data.get("repair_class") for e in _repairs(evs)]


def _terminals(evs):
    return [e for e in evs if e.type in ("node_evaluated", "node_failed")
            and e.data.get("node_id") == 0]


def _env_verdicts() -> dict:
    """The six recorded verdicts, as the agent would emit them under the current contract: the
    rationale is VERBATIM from the run, `repair_class` is the field the schema now asks for."""
    return {marker: {"action": "repair", "rationale": rationale, "repair_class": "environment"}
            for marker, _tail, rationale in _SEQUENCE}


# ------------------------------------------------------- the classifier, on the live tracebacks
def test_the_engine_reads_the_live_tracebacks_the_same_way_the_agent_did():
    """The engine's own corroboration must agree with the six 'mechanical' rationales — and must
    NOT extend that reading to the research question, which is a modelling decision wearing a
    library's voice (a RuntimeError raised inside torch's DDP)."""
    for marker, tail, _rationale in _SEQUENCE:
        assert _environment_failure(tail), marker
    assert not _environment_failure(_DDP)
    assert not _environment_failure(_SECOND_QUESTION)
    # Neither does an ordinary bad experiment: a shape mismatch, an assertion, an OOM.
    assert not _environment_failure("RuntimeError: mat1 and mat2 shapes cannot be multiplied")
    assert not _environment_failure("AssertionError: metric too low")
    assert not _environment_failure("")


def test_repair_class_vocabulary_has_one_spelling():
    """Registry (CLAUDE.md: duck-typed seams are registry-guarded). The agent's emit schema, the
    engine's coercion and the rule path must all speak `REPAIR_CLASSES` — a typo'd literal would
    silently charge every repair to the wrong ledger, which is invisible in a passing run."""
    import inspect

    from looplab.agents import unified_agent

    src = inspect.getsource(unified_agent.UnifiedAgent.triage_crash)
    assert '"enum": list(REPAIR_CLASSES)' in src, (
        "the emit schema must read the enum from the registry, never re-spell it")
    assert "coerce_repair_class(" in src, "the agent's answer must be coerced through the registry"
    assert set(REPAIR_CLASSES) == {"environment", "experiment"}
    assert DEFAULT_REPAIR_CLASS in REPAIR_CLASSES
    # Fail closed in every direction a model can get it wrong.
    for bad in (None, "", "mechanical", "ENVIRONMENTAL", 7, {"a": 1}):
        assert coerce_repair_class(bad) == DEFAULT_REPAIR_CLASS
    assert coerce_repair_class(" Environment ") == "environment"
    # …and the deterministic fallback only ever emits registry members.
    for reason, err in (("crash", _T1), ("crash", _DDP), ("timeout", ""), ("oom", "")):
        assert _rule_triage(reason, err, 1, 6)["repair_class"] in REPAIR_CLASSES
    assert _rule_triage("timeout", "", 1, 6)["repair_class"] == "experiment"   # too slow is OUR bug


# --------------------------------------------------------------- the run, replayed end to end
def test_the_six_migrations_leave_the_research_budget_intact(tmp_path):
    """The headline: the recorded sequence, driven through the real loop. All six reconciliations
    are charged to the environment ledger, so the DDP question arrives with the full six-attempt
    experiment budget — and the node then spends TWO of them on real research and produces a
    metric, which the live run never got to do."""
    tails = [t for _m, t, _r in _SEQUENCE] + [_DDP, _SECOND_QUESTION]
    dev = _ReplayDev(tails)
    triage = _ReplayTriage(_env_verdicts(),
                           {"action": "repair", "rationale": _DDP_RATIONALE,
                            "repair_class": "experiment"})
    evs, _ = _drive(tmp_path, dev, triage)

    classes = _classes(evs)
    assert classes == ["environment"] * 6 + ["experiment"] * 2
    # The node REACHED the research question — it is not merely that the counters look nicer.
    assert any("find_unused_parameters" in e for e in dev.errors)
    assert any("finished reduction" in e for e in dev.errors)
    # 8 repairs under `inline_repair_attempts: 6`: the migrations never touched the budgeted ledger,
    # and 4 experiment attempts are still unspent when the node succeeds.
    assert len(classes) == 8
    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].type == "node_evaluated"
    st = fold(evs)
    assert st.nodes[0].status is NodeStatus.evaluated and st.nodes[0].metric == 0.1


def test_without_the_class_the_same_sequence_dies_at_the_research_question(tmp_path):
    """The incident itself, and the proof that the classification is what changed it: an agent that
    fills no `repair_class` (the previous contract) charges all six migrations to the one pool, and
    the node terminalizes on the DDP question with the budget gone — `node_failed`, reason crash,
    exactly what `runs/rubert-dr-0805` recorded."""
    tails = [t for _m, t, _r in _SEQUENCE] + [_DDP, _SECOND_QUESTION]
    dev = _ReplayDev(tails)
    unclassified = {marker: {"action": "repair", "rationale": rationale}
                    for marker, _t, rationale in _SEQUENCE}
    triage = _ReplayTriage(unclassified, {"action": "repair", "rationale": _DDP_RATIONALE})
    evs, _ = _drive(tmp_path, dev, triage)

    assert _classes(evs) == ["experiment"] * 6            # one pool, spent on the environment
    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].type == "node_failed"
    assert "find_unused_parameters" in terminal[0].data["error"]     # died AT the research question
    assert fold(evs).nodes[0].status is NodeStatus.failed


def test_an_exemption_needs_the_engine_to_agree_with_the_agent(tmp_path):
    """The agent's word is not enough. A triage that labels EVERY failure 'environment' — the
    obvious way to buy unlimited repairs, and a plausible way for a model to be sloppy — gets
    nothing on failures the engine does not read as environment reconciliation. These MOVE every
    attempt (a different research error each time), so nothing else bounds them: the node stops
    because the experiment budget is spent, and says so."""
    # A genuinely different research failure each attempt (word names, not digits — the signature
    # normalizer absorbs digits), so every signature is new and the anti-stuck guard never fires:
    # the only thing left that can stop this node is the budget.
    tails = [f"[rank0]: RuntimeError: the {w} head still produces no gradient in training_step\n"
             for w in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel")]
    dev = _ReplayDev(tails)
    triage = _ReplayTriage({}, {"action": "repair", "rationale": "mechanical, honest",
                                "repair_class": "environment"})
    evs, _ = _drive(tmp_path, dev, triage, inline_repair_attempts=2)

    assert _classes(evs) == ["experiment", "experiment"]   # the claim bought no exemption at all
    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].type == "node_failed"
    assert terminal[0].data["triage_action"] == "abandon"
    assert "experiment attempt(s)" in terminal[0].data["triage_rationale"]


def test_an_environment_repair_that_stops_moving_the_failure_is_charged(tmp_path):
    """Forward progress is the price of the second ledger. The same environment failure over and
    over is not reconciliation work, whatever it is labelled: only its FIRST sighting is exempt,
    and the rest come out of the experiment budget until the anti-stuck guard ends the node."""
    dev = _ReplayDev([_T5] * 9)                     # one signature, forever
    triage = _ReplayTriage({}, {"action": "repair", "rationale": "same API break again",
                                "repair_class": "environment"})
    evs, _ = _drive(tmp_path, dev, triage, inline_repair_attempts=6)

    assert _classes(evs) == ["environment", "experiment", "experiment"]
    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].type == "node_failed"
    assert "stuck" in terminal[0].data["triage_rationale"]      # the 4th sighting ends it


def test_reaching_a_later_stage_is_progress_even_when_the_error_repeats(tmp_path):
    """The second shape of forward progress, and the one a signature cannot express: the SAME
    missing-module failure recurs while the PIPELINE gets further, because the next stage imports
    the same absent module. The node is visibly moving, so its reconciliation keeps drawing on the
    environment ledger — and the per-signature stuck guard still ends it on the 4th sighting."""
    from looplab.runtime.sandbox import RunResult

    def _staged(*done):
        # `done` = the stages that PASSED before this attempt's failure (depth), then one failure.
        stages = [{"name": n, "status": "ok", "exit_code": 0, "seconds": 0.1} for n in done]
        stages.append({"name": "next", "status": "fail", "exit_code": 1, "seconds": 0.1})
        return RunResult(exit_code=1, stdout="", stderr=_T1, metric=None, timed_out=False,
                         stages=stages, failed_stage="next")

    results = [_staged(), _staged("data_prep"), _staged("data_prep", "train"),
               _staged("data_prep", "train", "score")]
    dev = _ReplayDev([_GOOD])                    # the source is irrelevant: the eval is stubbed
    triage = _ReplayTriage({}, {"action": "repair", "rationale": _R1,
                                "repair_class": "environment"})
    run_dir = tmp_path / "run"
    eng = Engine(run_dir, task=ToyTask.load(TASK), researcher=triage, developer=dev,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 auto_install_deps=False, inline_repair=True, inline_repair_attempts=6,
                 inline_repair_stuck_repeat=4,
                 inline_repair_retrain_cap=0)     # the retrain cap is a COST bound, not the subject
    eng._run_eval = lambda *a, **kw: results.pop(0)
    eng.store.append("run_started",
                     {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
        "code": _GOOD})
    anyio.run(eng._evaluate, 0, anyio.CapacityLimiter(1), None)

    evs = list(EventStore(run_dir / "events.jsonl").read_all())
    # One signature throughout — attempts 2 and 3 are exempt only because the pipeline advanced.
    assert _classes(evs) == ["environment"] * 3
    terminal = _terminals(evs)
    assert len(terminal) == 1 and "stuck" in terminal[0].data["triage_rationale"]


# --------------------------------------------------- the pathological case must stay bounded
@pytest.mark.parametrize("attempts", [0, 6])
def test_the_2345_repair_runaway_stays_bounded_when_every_repair_claims_environment(
        tmp_path, attempts):
    """The runaway this bound exists for, driven with the exemption maximally abused: the real
    symbol pool from `runs/rubert-dr-0804` (2345 repairs / 3.5 h on ONE node), every failure a
    ModuleNotFoundError — which the engine DOES read as environment — and a triage that claims
    'environment' every time. It stays bounded because an exemption also needs the failure to MOVE:
    those 2345 failures normalize to one signature, so exactly one attempt is ever exempt and the
    anti-stuck guard still terminalizes the node. Parametrized over both budget modes, including
    `inline_repair_attempts = 0` (UNLIMITED), where the guard is the only bound."""
    dev = _RegistryWalkDev()
    triage = _ReplayTriage({}, {"action": "repair", "rationale": "mechanical import error",
                                "repair_class": "environment"})
    evs, _ = _drive(tmp_path, dev, triage, inline_repair_attempts=attempts)

    classes = _classes(evs)
    assert len(classes) == 3                        # the 4th sighting of the one signature stops it
    assert classes.count("environment") == 1        # only the first sighting moved anything forward
    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].type == "node_failed"
    assert "stuck" in terminal[0].data["triage_rationale"]


def test_repairs_going_in_circles_are_still_cut_off(tmp_path):
    """A node whose repairs oscillate (fix A breaks B, fix B breaks A) never repeats a signature
    back-to-back, and every failure here is environment-shaped and labelled 'environment' — so the
    exemption is available on every FIRST sighting. It must still end: two signatures buy two
    exempt attempts, the rest are charged, and the per-signature stuck ledger closes the node."""
    dev = _ReplayDev([_T5, _T6] * 8)                # A B A B … , never twice in a row
    triage = _ReplayTriage({}, {"action": "repair", "rationale": "mechanical API break",
                                "repair_class": "environment"})
    evs, _ = _drive(tmp_path, dev, triage, inline_repair_attempts=6)

    classes = _classes(evs)
    assert classes.count("environment") == 2        # one per distinct signature, then never again
    assert len(classes) == 6                        # bounded: A's 4th sighting ends the node
    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].type == "node_failed"
    assert "stuck" in terminal[0].data["triage_rationale"]


def test_one_terminal_and_a_fold_that_ignores_the_new_field(tmp_path):
    """Invariants #2 and #5 across the new exit: exactly one terminal per node, and `repair_class`
    is ADDITIVE on `node_repaired` — a log written before the field existed folds identically, so
    the field is audit only and can never change replayed state."""
    tails = [t for _m, t, _r in _SEQUENCE] + [_DDP, _SECOND_QUESTION]
    dev = _ReplayDev(tails)
    evs, _ = _drive(tmp_path, dev, _ReplayTriage(_env_verdicts(),
                                                 {"action": "repair", "rationale": _DDP_RATIONALE,
                                                  "repair_class": "experiment"}))

    assert len(_terminals(evs)) == 1
    before = fold(evs)
    assert fold(evs).model_dump() == before.model_dump()      # deterministic
    assert all("repair_class" in e.data for e in _repairs(evs))
    for e in _repairs(evs):                                   # an OLD log: no such key
        e.data.pop("repair_class")
    assert fold(evs).model_dump() == before.model_dump()


# ------------------------------------------------- the missing library the traceback never names
def test_a_dependency_degraded_into_a_nameerror_is_installed_not_hand_patched(tmp_path,
                                                                              monkeypatch):
    """Attempts 2 and 3 of the live run. `transformers` guards `init_empty_weights` behind
    `is_accelerate_available()`, so the absent distribution's name appears NOWHERE in the exception
    and `_prepare_env` (which keys on the traceback) sees nothing installable. The agent named it —
    that naming is now admissible evidence — so the engine installs `accelerate` and re-runs
    WITHOUT spending a repair attempt on either ledger."""
    installed: list[str] = []
    flag = tmp_path / "accelerate.installed"

    def fake_install(package, *, python=None, timeout=None):
        installed.append(package)
        flag.write_text("ok")                        # the library is now importable by the solution
        return InstallResult(package=package, ok=True, returncode=0)

    # This box HAS accelerate; the live one did not. Probe the recorded absence instead.
    monkeypatch.setattr(deps, "is_present", lambda m, **kw: m != "accelerate")

    dev = _ReplayDev([_T2, _T3], installed_flag=flag)   # T3 is the SAME cause, one symbol later
    triage = _ReplayTriage(
        {"init_empty_weights": {"action": "repair", "rationale": _R2,
                                "repair_class": "environment",
                                "missing_dependency": "accelerate"}},
        {"action": "repair", "rationale": "", "repair_class": "experiment"})
    evs, _ = _drive(tmp_path, dev, triage, auto_install_deps=True, dep_installer=fake_install)

    assert installed == ["accelerate"]
    dep_events = [e for e in evs if e.type == "deps_installed"]
    assert len(dep_events) == 1
    assert dep_events[0].data["packages"] == ["accelerate"] and dep_events[0].data["source"] == "triage"
    assert dev.repair_calls == 0                     # an install is not a repair: NO attempt spent
    assert _repairs(evs) == []                       # …so BOTH live attempts 2 and 3 are given back
    st = fold(evs)
    assert st.nodes[0].status is NodeStatus.evaluated and st.nodes[0].metric == 0.1


def test_a_library_that_names_the_package_only_in_prose_is_installed(tmp_path, monkeypatch):
    """Attempt 4. Lightning re-raises its own missing-dependency error naming the package in prose
    (``Neither `tensorboard` nor `tensorboardX` is available``), which the canonical
    `No module named 'X'` scan cannot parse — so the allowlist entry alone was never enough. Here
    the traceback and the rationale name it INDEPENDENTLY, which is the join, and no structured
    field is needed."""
    installed: list[str] = []
    flag = tmp_path / "tensorboard.installed"

    def fake_install(package, *, python=None, timeout=None):
        installed.append(package)
        flag.write_text("ok")
        return InstallResult(package=package, ok=True, returncode=0)

    monkeypatch.setattr(deps, "is_present", lambda m, **kw: m not in ("tensorboard", "tensorboardX"))
    dev = _ReplayDev([_T4], installed_flag=flag)
    triage = _ReplayTriage({}, {"action": "repair", "rationale": _R4,
                                "repair_class": "environment"})
    evs, _ = _drive(tmp_path, dev, triage, auto_install_deps=True, dep_installer=fake_install)

    assert installed == ["tensorboard", "tensorboardX"]
    assert dev.repair_calls == 0                     # not the live outcome: a whole attempt on a logger
    assert [e.data["packages"] for e in evs if e.type == "deps_installed"] == [
        ["tensorboard", "tensorboardX"]]
    assert fold(evs).nodes[0].status is NodeStatus.evaluated


def test_the_install_path_fails_closed(tmp_path, monkeypatch):
    """What must NEVER be installed. The research question is not an unresolved-name failure, so a
    triage naming a package on it installs nothing — the traceback and the triage have to point at
    the missing library JOINTLY, and here the traceback points at a modelling decision."""
    installed: list[str] = []

    def fake_install(package, *, python=None, timeout=None):
        installed.append(package)
        return InstallResult(package=package, ok=True, returncode=0)

    monkeypatch.setattr(deps, "is_present", lambda m, **kw: False)   # pretend nothing is installed
    dev = _ReplayDev([_DDP, _DDP, _DDP])
    triage = _ReplayTriage({}, {"action": "repair", "rationale": "try installing accelerate",
                                "repair_class": "environment", "missing_dependency": "accelerate"})
    evs, _ = _drive(tmp_path, dev, triage, auto_install_deps=True, dep_installer=fake_install,
                    inline_repair_attempts=2)

    assert installed == []
    assert not [e for e in evs if e.type == "deps_installed"]


def test_install_candidates_are_jointly_evidenced():
    """The pure contract, on the live evidence. Each condition is load-bearing on its own."""
    # A: the structured field supplies a name the traceback CANNOT contain …
    assert deps.triage_install_candidates("accelerate", _R2, _T2) == ["accelerate", "transformers"]
    # … but only while the rationale is about THIS traceback (an unrelated one authorizes nothing).
    assert deps.triage_install_candidates("accelerate", "the learning rate looks too high", _T2) == []
    # B: the traceback and the rationale name it independently (no structured field at all).
    assert deps.triage_install_candidates("", _R4, _T4) == ["tensorboard", "tensorboardX"]
    # Free prose can never mint a candidate by itself: attempt 1's rationale says "the installed
    # Lightning version" while the traceback names `pytorch_lightning` — `lightning` is a DIFFERENT
    # distribution and installing it would churn the environment the node is trying to reconcile.
    assert deps.triage_install_candidates("", _R1, _T1) == ["pytorch_lightning"]
    # Shape gate: a failure that is not unresolved-name shaped installs nothing, whatever is claimed.
    assert deps.triage_install_candidates("torch", "install torch", _DDP) == []
    assert deps.triage_install_candidates("torch", "install torch", _T5) == []
    # Allowlist gate: an off-list name is a code bug (a typo'd or local module), never an install.
    assert deps.triage_install_candidates("my_local_helper_zzz", _R2, _T2) == ["transformers"]


def test_is_present_probes_the_interpreter_and_fails_closed():
    """The last condition, and the one no agent can influence: only what is provably ABSENT is
    installed. Any doubt answers 'present', so the failure mode is a missed install, never a
    surprise one."""
    assert deps.is_present("sys") and deps.is_present("json")
    assert not deps.is_present("definitely_not_a_real_module_zzz")
    assert deps.is_present("")                       # not a name -> no install
    assert deps.is_present("not an identifier!")
    assert deps.is_present("json", python="/nonexistent/python-zzz")   # launch failure -> present
