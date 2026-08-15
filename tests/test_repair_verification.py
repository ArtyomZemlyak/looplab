"""The repair-verification rung: did the repair DO what its rationale said it would do?

Drives the REAL inline-repair loop (`Engine._evaluate`) over a real sandbox, because the property
these tests exist for is "the effect landed", not "the call happened" — see CLAUDE.md's tiering.
The measurement that motivated the rung, and the two-tier design it forced, are in
`looplab/engine/repair_verify.py`'s docstring.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import anyio
import pytest

from looplab.adapters.toytask import ToyTask
from looplab.core.models import Idea, NodeStatus
from looplab.engine import crash_repair, evaluate as ev_mod
from looplab.engine.crash_repair import _format_repair_log
from looplab.engine.orchestrator import Engine
from looplab.engine.repair_verify import (INERT_REPAIR_LIMIT, PARAM_OVERRIDE_MIN_PARTS,
                                          REPAIR_INERT, REPAIR_UNMET,
                                          REPAIR_UNSTATED, REPAIR_VERDICTS, REPAIR_VERIFIED,
                                          changed_region, claimed_tokens,
                                          declared_param_overrides, inert_streak, verify_repair)
from looplab.engine.triage import AGENT_TRIAGE_ACTIONS, TRIAGE_ACTIONS, coerce_triage_action
from looplab.engine.metric_salvage import SALVAGE_CAUSE_TRIAGE_ACTION
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.engine import repair_verify
import time
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree
from tests._source_scan import names_read

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"

# Crashes mechanically (a module that does not exist), so the rule/agent triage says "repair".
_BAD = "import definitely_not_a_real_module_zzz\nprint('x')\n"
_GOOD = "import json; print(json.dumps({'metric': 0.1}))\n"


class _Judge:
    """Answers `repair` forever with a rationale the caller chooses — so what STOPS the loop in
    each test below is the rung under test and never the judge."""

    def __init__(self, rationale="fix it"):
        self.rationale = rationale
        self.histories: list[str] = []

    def propose(self, state, parent):
        return Idea(operator="x", params={"x": 1.0, "y": 1.0})

    def triage_crash(self, node, error, attempt, *, state=None, brief="", history="",
                     stages_passed=None, attempts_left=None):
        self.histories.append(history or "")
        return {"action": "repair", "rationale": self.rationale}


class _InertDev:
    """A developer whose every repair returns the code it was given, byte for byte. The measured
    shape: rubertlite-dense-retrieval node 57 made thirteen `read_file` calls and not one write, on
    three consecutive attempts, each one buying a full re-evaluation."""

    def __init__(self):
        self.repair_calls = 0

    def implement(self, idea):
        return _BAD

    def repair(self, idea, code, error):
        self.repair_calls += 1
        return code


class _MovingDev:
    """A developer that really does change the file every attempt (and still fails)."""

    def __init__(self):
        self.repair_calls = 0

    def implement(self, idea):
        return _BAD

    def repair(self, idea, code, error):
        self.repair_calls += 1
        return f"{_BAD}# attempt {self.repair_calls}\n"


# The VERBATIM rationale from `runs/rubertlite-dr-unified-v7` node 0 attempt 2 (seq 476), recovered
# from that run's `spans.jsonl` — the durable row holds only its first 300 characters. It is kept
# whole here because its SHAPE is the defect: every concrete thing it promises to change is written
# after the word "Fix:", at character 300, and the only token before that cut is `nll_cos` — the
# 0.728 baseline it is comparing AGAINST. Truncated, the rung read a rationale that named one
# historical metric and changed something else, and stamped `unmet` on the durable row.
_V7_RATIONALE = (
    "Diverged at step 50/35300, immediately after the only structural change vs the working "
    "10-epoch nll_cos runs (0.728): the newly-added R-Drop auxiliary KL term. The idea (DCL+R-Drop, "
    "the documented 0.8835 recipe) is sound; this is a numerical-stability bug in the new term, not "
    "a flawed approach. Fix: compute the R-Drop KL in stable log-space (F.kl_div with "
    "log_target=True and clamp the target probs to >=1e-7), upcast the KL term to fp32 before "
    "scaling, and lower rdrop_alpha from 0.5 to 0.1 so the auxiliary term cannot destabilize the "
    "DCL base in the first batches. Keep the rest of the proven recipe (nll_cos, lr 1e-3, wd 0.1, "
    "accumulate 2, grad clip 1.0, bs 8k, positive_threshold 1).")


# ---------------------------------------------------------------- the three LIVE v8 verdicts
# All three are VERBATIM from `runs/rubertlite-dr-unified-v8` node 3, recovered whole from that run's
# `spans.jsonl` (the durable rows hold only the first 300 characters, which is what the engine
# process behind that run actually read — it started before the intake cap was raised and a running
# interpreter does not reload its source). They are the entire live evidence for this rung's
# precision: three `unmet` verdicts, hand-audited to ONE true positive.
#
# Attempt 1 — THE TRUE POSITIVE, and the row every change here must leave alone. It promised an edit
# to the node's own stage script and edited the shared library instead: different blast radius.
_V8_TRUE_POSITIVE = (
    "The mine stage (first stage, 0 passed) crashed on its own coverage guardrail "
    "(assert coverage>=0.9, got 0.43), not on R-Drop — node 1's identical mining config passed "
    "and hit 0.7384, so the approach is sound. Fix mine_stage.py (a .py-only edit) to relax the "
    "coverage threshold or fall back to random negatives so the pipeline reaches training and the "
    "R-Drop hypothesis can be tested.")
# …its real change set and the head of the real diff. `mine_stage.py` is nowhere in either.
_V8_TRUE_POSITIVE_CHANGED = ["vectorsearch/data/mine_negatives.py"]
_V8_TRUE_POSITIVE_REGION = (
    "vectorsearch/data/mine_negatives.py\n@@\n+import random\n"
    "+        # joinable, restoring ~100% coverage.\n+        rng = random.Random(self.seed)\n")

# Attempt 2 — THE CITED BASELINE, the residue the module docstring named before it was ever seen
# live. Both tokens the engine convicted on sit inside a clause about NODE 1; the sentence that says
# what THIS repair will do ("Next change: …") names nothing a diff could contain, which is why
# `unstated` is the honest answer and `unmet` was an accusation the text does not support.
_V8_CITED_BASELINE = (
    "The crash is the mine stage's coverage guardrail (0.43 < 0.9), before any R-Drop training ran "
    "— a mechanical mining-coverage issue, not evidence the R-Drop idea is wrong. Node 1's "
    "identical mining config (mining_type=1, n_negatives=2) already passed and reached 0.7384, so "
    "the config can clear the guardrail; the previous repair's change to mine_negatives.py was "
    "never actually applied (engine couldn't verify it), so a genuine targeted fix is still "
    "available. Next change: make mining produce >=2 negatives for >=90% of rows (relax/restore the "
    "mining threshold to node-1 behavior) via a .py-only edit so no finished stages are re-run.")
_V8_CITED_BASELINE_CHANGED = ["mine_stage.py"]
_V8_CITED_BASELINE_REGION = (
    "mine_stage.py\n@@\n+    # silently reused and reproduces the old ~43% coverage forever. "
    "Force a fresh mine here.\n+    shutil.rmtree(out_dir, ignore_errors=True)\n")

# Attempt 4 — THE ABBREVIATION, a shape the docstring did not name at all. The repair did exactly
# what it said; the codebase spells the parameter `gradient_accumulation_steps` and the model wrote
# the colloquial `grad_accum`, so a literal-token extractor convicted a kept promise.
_V8_ABBREVIATION = (
    "CUDA OOM in train caused by R-Drop's two forward passes doubling peak memory at batch 8192 "
    "(node 1 fit the same batch without R-Drop). Fix: halve per-step batch to 4096 and raise "
    "grad_accum to 4 so effective batch stays 8192 while peak memory drops to node-1's "
    "single-forward footprint. R-Drop is a sound idea; this is a mechanical memory fix, not a "
    "rejection.")
_V8_ABBREVIATION_CHANGED = ["vectorsearch/train.py"]
_V8_ABBREVIATION_REGION = (
    "vectorsearch/train.py\n@@\n"
    "+    # per-device batch and double gradient accumulation to keep the SAME effective batch size\n"
    "+    # (8192 x 2 accum = 16384) while fitting in memory. Config is pydantic-mutable, so this is "
    "a\n"
    "+    config.train.training.batch_size = 4096\n"
    "+    config.train.training.gradient_accumulation_steps = 4\n")


class _ClaimingDev:
    """A developer that makes exactly the change the rationale's SECOND half promises (and still
    fails, so the loop runs to the operator's cap rather than to a metric)."""

    def __init__(self):
        self.repair_calls = 0

    def implement(self, idea):
        return _BAD

    def repair(self, idea, code, error):
        self.repair_calls += 1
        return f"{_BAD}rdrop_alpha = 0.1\n"


def _drive(run_dir, dev, judge, *, seed_params=None, **kw):
    """Seed one node and run the REAL repair loop over it (no genesis, no policy search).

    `seed_params` overrides the seeded node's DECLARED `idea.params`. The toy default is the
    benchmark space's bare `x`/`y`, which `declared_param_overrides` excludes by design — the
    override tests need a dotted declaration, i.e. a repo-task-shaped one."""
    kw.setdefault("auto_install_deps", False)
    kw.setdefault("inline_repair", True)
    eng = Engine(run_dir, task=ToyTask.load(TASK), researcher=judge, developer=dev,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1), **kw)
    eng.store.append("run_started",
                     {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft",
                 "params": dict(seed_params) if seed_params else {"x": 1.0, "y": 1.0},
                 "rationale": "seed"},
        "code": dev.implement(None)})

    async def _bounded() -> bool:
        # A hard wall so a regression fails instead of hanging CI — an unbounded inert chain is
        # exactly the shape this rung exists to stop.
        with anyio.move_on_after(180) as scope:
            await eng._evaluate(0, anyio.CapacityLimiter(1), None)
        return scope.cancelled_caught

    assert not anyio.run(_bounded), "the inline-repair loop did not terminate"
    return list(EventStore(Path(run_dir) / "events.jsonl").read_all()), eng


def _repairs(evs):
    return [e for e in evs if e.type == "node_repaired" and e.data.get("node_id") == 0]


def _terminals(evs):
    return [e for e in evs if e.type in ("node_evaluated", "node_failed")
            and e.data.get("node_id") == 0]


# ------------------------------------------------------------------ the rung, driven for real
def test_a_repair_that_changes_nothing_is_stamped_inert_on_the_durable_row(tmp_path):
    """THE DEFECT, end to end. A repair that returns the code it was handed changed nothing, and
    until 2026-08-13 the engine committed it as a repair and paid for a byte-identical
    re-evaluation. The verdict has to be on the DURABLE row, not only in the process, because it is
    what a resumed judge reads."""
    dev, judge = _InertDev(), _Judge()
    evs, _ = _drive(tmp_path / "inert", dev, judge, inline_repair_attempts=8)

    rows = _repairs(evs)
    assert rows, "expected the loop to make at least one repair"
    assert all(r.data["verified"] == REPAIR_INERT for r in rows), \
        [r.data.get("verified") for r in rows]
    # `changed` and `verified` are two different facts and both are recorded: the empty list says
    # which files moved, the verdict says the engine compared the bytes and concluded none did.
    assert all(r.data["changed"] == [] for r in rows)


def test_two_inert_repairs_in_a_row_stop_the_node_instead_of_buying_another_eval(tmp_path):
    """The bound, and it is the ONLY thing this rung is allowed to decide. Measured cost of not
    having it: rubertlite-dr-unified-v4 node 6 spent two consecutive inert repairs at ~2.7 h of GPU
    each, and rubertlite-dense-retrieval node 57 three."""
    dev, judge = _InertDev(), _Judge()
    evs, _ = _drive(tmp_path / "bound", dev, judge, inline_repair_attempts=8)

    assert len(_repairs(evs)) == INERT_REPAIR_LIMIT, \
        "the streak must stop the node at the limit, not at the operator's much larger cap"
    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].type == "node_failed"
    # The terminal says which bound stopped it, in the engine's own voice — an operator who set 8
    # must not read a terminal implying their cap was reached.
    rationale = terminal[0].data["triage_rationale"]
    assert "changed nothing at all" in rationale and "byte-identical" in rationale
    assert "hard limit" not in rationale
    assert fold(evs).nodes[0].status is NodeStatus.failed


def test_one_inert_repair_is_forgiven_and_a_real_change_clears_the_streak(tmp_path):
    """A developer that genuinely edits the file every attempt is never charged by this rung — it is
    bounded by the operator's cap like any other chain. A bound that fired on a single inert attempt
    would stop nodes whose developer spent one turn budget reading before it edited."""
    dev, judge = _MovingDev(), _Judge()
    evs, _ = _drive(tmp_path / "moving", dev, judge, inline_repair_attempts=4)

    rows = _repairs(evs)
    assert len(rows) == 4, "the operator's cap, not the inert bound, must be what stopped this"
    assert not any(r.data["verified"] == REPAIR_INERT for r in rows)
    assert "hard limit" in _terminals(evs)[0].data["triage_rationale"]


def test_the_inert_streak_survives_a_resume(tmp_path):
    """Invariant #3 for this bound: a budget a resume refunds is not a budget. One durable inert row
    already in the log plus ONE more in this process reaches the limit — so the second process makes
    a single repair, not two. `_durable_repair_ledger` is what carries it."""
    run_dir = tmp_path / "resume"
    dev, judge = _InertDev(), _Judge()
    eng = Engine(run_dir, task=ToyTask.load(TASK), researcher=judge, developer=dev,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 auto_install_deps=False, inline_repair=True, inline_repair_attempts=8)
    eng.store.append("run_started",
                     {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
        "code": _BAD})
    # The row a crashed/paused earlier process left behind.
    eng.store.append("node_repaired", {
        "node_id": 0, "generation": 0, "attempt": 1, "code": _BAD, "files": {}, "deleted": [],
        "error_in": "ModuleNotFoundError: definitely_not_a_real_module_zzz",
        "triage_action": "repair", "rationale": "fix it", "changed": [],
        "verified": REPAIR_INERT, "unmet": [], "stages_passed": 0, "unparseable_repairs": 0})

    async def _bounded() -> bool:
        with anyio.move_on_after(180) as scope:
            await eng._evaluate(0, anyio.CapacityLimiter(1), None)
        return scope.cancelled_caught

    assert not anyio.run(_bounded)
    evs = list(EventStore(run_dir / "events.jsonl").read_all())
    assert len(_repairs(evs)) == 2, "the resumed process must continue the streak, not restart it"
    assert dev.repair_calls == 1
    assert "changed nothing at all" in _terminals(evs)[0].data["triage_rationale"]


def test_a_row_with_no_verdict_breaks_the_streak_rather_than_extending_it():
    """A `node_repaired` written before this column existed, and the `salvage_cause_fix` marker row
    (which never writes one), must not be read as "changed nothing" — that would terminalize a node
    on evidence nobody recorded. Absent is NOT a default."""
    assert inert_streak([{"verified": REPAIR_INERT}, {"verified": REPAIR_INERT}]) == 2
    assert inert_streak([{"verified": REPAIR_INERT}, {"changed": []}]) == 0
    assert inert_streak([{"verified": REPAIR_INERT}, {"verified": REPAIR_VERIFIED},
                         {"verified": REPAIR_INERT}]) == 1        # trailing, not total
    assert inert_streak([]) == 0 and inert_streak(None) == 0


def test_the_durable_ledger_refuses_a_verdict_that_is_not_in_the_registry(tmp_path):
    """`_durable_repair_ledger` admits `verified` only when it is a member of `REPAIR_VERDICTS`, so
    a corrupt or forward-dated row degrades to "no opinion" (which breaks the streak) rather than
    driving a stop on a string nothing in this tree understands."""
    p = tmp_path / "events.jsonl"
    s = EventStore(p)
    for verdict in (REPAIR_INERT, "definitely-not-a-verdict"):
        s.append("node_repaired", {
            "node_id": 0, "generation": 0, "attempt": 1, "code": "", "changed": [],
            "verified": verdict, "unmet": ["x"], "stages_passed": 0})
    _attempts, rows, _unp = ev_mod._durable_repair_ledger(EventStore(p).read_all(), 0, 0)
    assert rows[0]["verified"] == REPAIR_INERT and rows[0]["unmet"] == ["x"]
    assert "verified" not in rows[1] and "unmet" not in rows[1]
    assert inert_streak(rows) == 0


# ------------------------------------------------------------------ what the judge is told
def test_the_judge_history_states_the_two_meaningful_verdicts(tmp_path):
    """`changed: nothing` was already in front of the judge and a live model did once read it
    correctly (rubertlite-dr-unified-v2 node 2 attempt 3), while three of its siblings did not. The
    engine now says it in its own voice — and only for the two verdicts that mean something."""
    inert = _format_repair_log([{"attempt": 1, "error": "boom", "fix": "cut epochs 10 -> 5",
                                 "changed": [], "verified": REPAIR_INERT, "stages_passed": 0}])
    assert "THE ENGINE COMPARED THE BYTES" in inert and "changed no file at all" in inert

    unmet = _format_repair_log([{"attempt": 1, "error": "boom", "fix": "cut epochs 10 -> 5",
                                 "changed": ["train.py"], "verified": REPAIR_UNMET,
                                 "unmet": ["n_epochs"], "stages_passed": 0}])
    assert "could not find what this fix said it would change (n_epochs)" in unmet

    # …and every other row renders byte-identically to what this prompt has always been. Prompt text
    # is a contract: a new fact earns a new sentence, it does not reword the existing ones.
    base = _format_repair_log([{"attempt": 1, "error": "boom", "fix": "f", "changed": ["a.py"],
                                "stages_passed": 0}])
    for verdict in (REPAIR_VERIFIED, REPAIR_UNSTATED):
        assert _format_repair_log([{"attempt": 1, "error": "boom", "fix": "f",
                                    "changed": ["a.py"], "verified": verdict,
                                    "stages_passed": 0}]) == base
    assert _format_repair_log([{"attempt": 1, "error": "boom", "fix": "f", "changed": ["a.py"],
                                "verified": REPAIR_UNMET, "unmet": [],
                                "stages_passed": 0}]) == base       # unmet verdict, nothing to name


def test_the_engines_verdict_actually_reaches_the_live_judge(tmp_path):
    """Tier 1, not a source pin: run the loop and read what the judge was HANDED."""
    dev, judge = _InertDev(), _Judge()
    _drive(tmp_path / "reaches", dev, judge, inline_repair_attempts=8)
    assert any("THE ENGINE COMPARED THE BYTES" in h for h in judge.histories), judge.histories


# --------------------------------------------------- the rung must be shown the WHOLE rationale
def test_a_rationale_is_verified_against_its_whole_text_and_not_its_first_300_chars(tmp_path):
    """Tier 1, driven through the real loop: the verdict on the DURABLE row, for a developer that
    really did make the change the rationale's second half named.

    `crash_repair._triage_crash` capped the model's rationale at 300 chars — the same number as the
    durable sink one layer down — so `verify_repair` was handed a prefix and could not know it. A
    crash rationale is diagnosis-first and fix-second, so the cut landed on that seam: 83 of the 123
    model-authored rationales in `runs/` are stored at exactly the cap, and replaying the 54 whose
    full text survives in `spans.jsonl` moves 5 verdicts. This is the live one.

    KNOW WHAT THIS ONE DOES NOT COVER: `_Judge` is wired straight in as `researcher=`, so the loop
    below never enters `UnifiedAgent.triage_crash` — the shipped implementation of that seam, which
    caps the same string on its way OUT. This test passed over a path production does not take for a
    whole day. `test_the_shipped_triage_seam_hands_the_verifier_the_whole_rationale` below is the
    production-path twin, and it is the one that would have gone red."""
    dev, judge = _ClaimingDev(), _Judge(rationale=_V7_RATIONALE)
    evs, _ = _drive(tmp_path / "whole", dev, judge, inline_repair_attempts=1)

    row = _repairs(evs)[0].data
    assert row["verified"] == REPAIR_VERIFIED, (row["verified"], row.get("unmet"))
    # …and specifically NOT the historical answer, which named the baseline the rationale cited.
    assert row["unmet"] == []
    # The intake bound is an INTAKE bound: the durable column is still bounded by its own sink, so
    # widening what the engine reads did not widen what it writes into the event log.
    assert len(row["rationale"]) <= 300


def _unified_triage_seam(monkeypatch, rationale):
    """The SHIPPED triage seam, wired end to end with no model — returns `(agent, finalized)`.

    `triage_crash` is duck-typed, and every test above answers it with a double wired directly as
    `researcher=`. That is not the shipped path: `Settings.unified_agent` is true by default and
    `agents/factory.py` returns a `UnifiedAgent` as BOTH roles, so in production the verdict is built
    by `UnifiedAgent.triage_crash`'s emit finalizer and only THEN handed to `_ask_triage`. A cap in
    that finalizer is upstream of every engine-side bound, and a test that cannot traverse it cannot
    observe one — which is exactly how a fix that changed nothing shipped with a green suite.

    What is faked here is one thing only: the tool loop, patched on the seam CLAUDE.md documents
    (`looplab.agents.agent.drive_tool_loop`, which `_pilot_emit` resolves at CALL time). Everything
    from the model's emit dict onward — `_finalize`, the verdict coercion, `_ask_triage`, the repair
    commit and `verify_repair` — is production code.
    """
    from looplab.agents import agent as agent_mod
    from looplab.agents.unified_agent import UnifiedAgent

    finalized: list[dict] = []

    def _loop(client, tools, messages, emit_spec, *, finalize, fallback, **_kw):
        # The shape a live provider produces: one well-formed emit carrying the WHOLE rationale.
        out = finalize({"action": "repair", "rationale": rationale})
        finalized.append(out)
        return out

    monkeypatch.setattr(agent_mod, "drive_tool_loop", _loop)
    # `pilot_client` merely has to be non-None: `triage_crash` returns None without one and the
    # engine would fall back to its deterministic rule, i.e. the seam would not run at all.
    agent = UnifiedAgent(researcher=_Judge(), developer=_ClaimingDev(), pilot_client=object())
    return agent, finalized


def test_the_shipped_triage_seam_hands_the_verifier_the_whole_rationale(tmp_path, monkeypatch):
    """Tier 1 through the path production actually takes, and the negative control beside it.

    The 2026-08-13 fix raised the intake bound in `crash_repair._ask_triage` — a bound on what the
    seam RETURNED — while `UnifiedAgent.triage_crash`'s own finalizer went on cutting the same string
    to 300 before returning it. So the truncation survived the fix untouched, and the end-to-end test
    that "proved" otherwise never entered the finalizer. Both layers now read
    `engine/triage.py::TRIAGE_RATIONALE_CAP`."""
    agent, finalized = _unified_triage_seam(monkeypatch, _V7_RATIONALE)
    evs, _ = _drive(tmp_path / "seam", _ClaimingDev(), agent, inline_repair_attempts=1)

    assert finalized, "the shipped finalizer never ran — this test is not on the production path"
    assert finalized[0]["rationale"] == _V7_RATIONALE, "the finalizer truncated before the engine saw it"

    row = _repairs(evs)[0].data
    assert row["verified"] == REPAIR_VERIFIED, (row["verified"], row.get("unmet"))
    assert row["unmet"] == []
    # The durable column is still bounded by its own sink: no durable bytes moved.
    assert len(row["rationale"]) <= 300


def test_the_old_finalizer_cap_reproduces_the_defect_on_the_same_production_path(tmp_path,
                                                                                monkeypatch):
    """THE NEGATIVE CONTROL, and it is what proves the test above traverses the finalizer at all.

    Move the cap back to 300 on `engine/triage.py` — the module the FINALIZER re-reads on every call,
    while `crash_repair` keeps the by-value 2000 it bound at import. That is exactly the pre-fix
    configuration: a wide intake bound applied to a string that was already cut. The same developer
    makes the same change the rationale's second half promises, and the durable verdict flips to
    `unmet` naming `nll_cos` — the 0.728 BASELINE the rationale cited, the only concrete token that
    survives the cut. If the production path did not run through the finalizer, this would still say
    `verified`."""
    from looplab.engine import triage as triage_mod

    monkeypatch.setattr(triage_mod, "TRIAGE_RATIONALE_CAP", 300)
    assert crash_repair._TRIAGE_RATIONALE_CAP == 2000, "only the finalizer's cap may move here"

    agent, finalized = _unified_triage_seam(monkeypatch, _V7_RATIONALE)
    evs, _ = _drive(tmp_path / "control", _ClaimingDev(), agent, inline_repair_attempts=1)

    assert len(finalized[0]["rationale"]) == 300
    row = _repairs(evs)[0].data
    assert row["verified"] == REPAIR_UNMET, (row["verified"], row.get("unmet"))
    assert row["unmet"] == ["nll_cos"], row["unmet"]


def test_the_intake_bound_is_wider_than_every_sink_that_re_bounds_the_same_field():
    """The rule, stated: a cap at the INTAKE silently truncates every reader, while a cap at a SINK
    truncates only that sink's column. The rationale has four sinks and each keeps its own; the
    intake must therefore be strictly the loosest, or it is the one doing the truncating again."""
    assert crash_repair._TRIAGE_RATIONALE_CAP > 300          # the durable/terminal sinks
    assert crash_repair._TRIAGE_RATIONALE_CAP > 200          # `repair_log["fix"]`, the judge history
    # Wide enough, with headroom, for the rationales this repo has actually seen: the 93 recovered
    # from `runs/` run 121-690 characters (median 330, p90 460).
    assert crash_repair._TRIAGE_RATIONALE_CAP >= 2 * 690
    assert claimed_tokens(_V7_RATIONALE[:crash_repair._TRIAGE_RATIONALE_CAP]) == \
        claimed_tokens(_V7_RATIONALE), "the cap must not cut the live instance it was sized against"


def test_truncation_can_only_ever_lose_a_claim_never_invent_one():
    """Why widening the intake is safe in one direction and the bug was in the other. More text can
    only ADD claims, and a claim that was met stays met — so no `verified` can become `unmet` by
    being shown more, while an `unmet`/`unstated` can and does become `verified`."""
    region = "<whole-file solution>\n@@\n+rdrop_alpha = 0.1\n+kl_div(x, y, log_target=True)\n"
    kw = dict(changed=[], code_changed=True, region=region)
    assert verify_repair(_V7_RATIONALE[:300], **kw).verdict == REPAIR_UNMET
    assert verify_repair(_V7_RATIONALE[:300], **kw).unmet == ("nll_cos",)
    assert verify_repair(_V7_RATIONALE, **kw).verdict == REPAIR_VERIFIED
    # The prefix's claims are a SUBSET of the whole text's — that is the whole argument.
    assert set(claimed_tokens(_V7_RATIONALE[:300])) < set(claimed_tokens(_V7_RATIONALE))


def test_a_cited_name_and_a_claimed_name_are_the_same_token_to_this_rung():
    """The residual that SURVIVES the citation demotion, as a truth table. `_CITATION_RE` recognizes
    a clause bound to another NODE/RUN/ATTEMPT and deliberately nothing else, so "unlike the working
    nll_cos runs (0.728)" — a phrasing, not a reference — is still read as a claim. That is the
    narrowness being chosen on purpose (module docstring: the open-ended "vs" / "unlike" / "compared
    with" list would drop the real claim in "unlike the old nll_cos path, which I deleted").
    What matters most is that the row this rung EXISTS for stays red: a repair that promises to
    remove something and then does not is still caught."""
    diff = "<whole-file solution>\n@@\n-old = 1\n+new = 2\n"
    kw = dict(changed=["train.py"], code_changed=True)

    def verdict(rationale, region=diff):
        return verify_repair(rationale, region=region, **kw).verdict

    # 1. A PROMISE this diff does not keep. The case the rung is for — must stay `unmet`.
    assert verdict("I removed the nll_cos path entirely") == REPAIR_UNMET
    # 2. The same promise, kept. `verified`, and the token is why.
    assert verdict("I removed the nll_cos path entirely",
                   "<whole-file solution>\n@@\n-loss = nll_cos(a, b)\n") == REPAIR_VERIFIED
    # 3. A pure CITATION and nothing else concrete. `unmet` here is the residual false positive:
    #    the honest answer is `unstated`, because the rationale promised nothing checkable. It costs
    #    precision in a signal a model reads and stops nothing — `unmet` is never actionable.
    assert verdict("this diverged, unlike the working nll_cos runs (0.728); "
                   "I will make the new term numerically stable") == REPAIR_UNMET
    assert not verify_repair("this diverged, unlike the working nll_cos runs (0.728)",
                             region=diff, **kw).actionable
    # 4. A citation ALONGSIDE a kept promise — the shape the corpus actually contains, and the one
    #    the intake fix restores. One met claim is enough, so the citation costs nothing.
    assert verdict("unlike the working nll_cos runs, I set rdrop_alpha",
                   "<whole-file solution>\n@@\n+rdrop_alpha = 0.1\n") == REPAIR_VERIFIED
    # 5. Prose with no concrete token either way is `unstated`, never `unmet` — "I could not check
    #    this" and "this checked out" stay different facts.
    assert verdict("the approach is sound; this is a mechanical bug") == REPAIR_UNSTATED


# ------------------------------------------- what `unmet` may CONVICT on (the 2026-08-15 rung)
def _verdict(rationale, changed, region):
    return verify_repair(rationale, changed=changed, deleted=[], code_changed=False, region=region)


def test_the_three_live_v8_verdicts_are_one_accusation_and_two_withdrawals():
    """THE MEASUREMENT, as a truth table over the only three `unmet` rows this rung has ever
    produced on a real GPU run. Hand-audited: 1 true positive, 2 false positives of DIFFERENT shapes.
    Being right one time in three is not "some imprecision" in an advisory signal — the judge reads
    it, and a line that is wrong twice in three is a line a reader learns to discount."""
    # 1. THE TRUE POSITIVE. It said it would edit the node's own stage script and edited the shared
    #    library instead. This must survive every rule added here — a missed discrepancy is the
    #    strictly worse error, because a false `verified` is a claim the RECORD makes for the agent.
    true_pos = _verdict(_V8_TRUE_POSITIVE, _V8_TRUE_POSITIVE_CHANGED, _V8_TRUE_POSITIVE_REGION)
    assert true_pos.verdict == REPAIR_UNMET
    assert "mine_stage.py" in true_pos.unmet
    # Its own citation ("node 1's identical mining config passed and hit 0.7384") is real and is
    # correctly ignored: the promise is in the NEXT sentence, so the clause window never reaches it.
    assert not true_pos.actionable   # and it still stops nothing

    # 2. THE CITED BASELINE. Every convicted token was inside a clause about node 1. `unstated`, the
    #    verdict that already means "I could not check this" — never `verified`.
    cited = _verdict(_V8_CITED_BASELINE, _V8_CITED_BASELINE_CHANGED, _V8_CITED_BASELINE_REGION)
    assert cited.verdict == REPAIR_UNSTATED
    assert cited.unmet == ()
    # NOTHING IS DROPPED. The tokens are still on the record as claims; what changed is only whether
    # the engine is willing to accuse the repair of breaking a promise on their strength.
    assert {"mining_type", "n_negatives"} <= set(cited.claims)

    # 3. THE ABBREVIATION. The repair really did halve the batch to 4096 and raise accumulation to 4;
    #    the codebase spells it `gradient_accumulation_steps`.
    abbrev = _verdict(_V8_ABBREVIATION, _V8_ABBREVIATION_CHANGED, _V8_ABBREVIATION_REGION)
    assert abbrev.verdict == REPAIR_VERIFIED
    assert abbrev.unmet == ()


def test_the_abbreviation_rule_cannot_meet_a_file_claim_or_a_one_word_one():
    """The dangerous direction, bounded. This is the only rule here that can reach `verified`, and a
    false `verified` is worse than a false `unmet` — so each bound gets a row."""
    from looplab.engine.repair_verify import _abbreviated_identifier

    # It works, and it works off the DIFF's own identifiers rather than any vocabulary carried here.
    assert _abbreviated_identifier(
        "grad_accum", "+ gradient_accumulation_steps = 4\n") == "gradient_accumulation_steps"
    # A FILE claim never goes through it: a path is the axis "different blast radius" is measured on.
    # KNOW WHAT THIS ONE IS: the `_claim_met` ordering that enforces it is DEFENSIVE, not load-
    # bearing today, and saying so is the point — mutating it away leaves this file green, because a
    # file token cannot be abbreviated at all under the current regexes. The reason is checkable
    # rather than assertable-by-comment, so it is checked: `_FILE_RE` always requires an extension,
    # so every file claim carries a `.`, while `_IDENT_RE` never produces a token containing one —
    # so no candidate part can ever start with `stage.py`. The guard exists so that widening EITHER
    # regex later cannot silently open the path; this assertion is what would go red if one did.
    from looplab.engine.repair_verify import _FILE_RE, _IDENT_RE
    _corpus = " ".join([_V8_TRUE_POSITIVE_REGION, _V8_ABBREVIATION_REGION,
                        "mine_stage_helpers = 1", "train_py_helper = 2", "a.b_c = 3"])
    assert all("." not in t for t in _IDENT_RE.findall(_corpus))
    assert all("." in t for t in _FILE_RE.findall(
        "fix mine_stage.py and vectorsearch/train.py and looplab_stages.json"))
    assert _abbreviated_identifier("mine_stage.py", "+ mine_stage_helpers = 1\n") is None
    v = _verdict("Fix mine_stage.py to relax the threshold", ["mine_negatives.py"],
                 "mine_negatives.py\n@@\n+def mining_stage_helper(x):\n")
    assert v.verdict == REPAIR_UNMET and "mine_stage.py" in v.unmet
    # THE BACK DOOR THAT BOUND EXISTS FOR, and the reason an abbreviation must SHORTEN a part.
    # `_IDENT_RE` extracts the twin identifier `mine_stage` from the file claim `mine_stage.py`, so a
    # whole-part prefix match would meet the twin off a `mine_stage_helper` in the diff, acquit the
    # row on that one met claim, and turn the live true positive into `verified` without ever
    # touching the file rule. `pos_scores` is not `pos_scores_broadcast`; `grad_accum` IS
    # `gradient_accumulation_steps`.
    assert _abbreviated_identifier("mine_stage", "+def mine_stage_helper(x):\n") is None
    assert _abbreviated_identifier("pos_scores", "+ pos_scores_broadcast = 1\n") is None
    # …and the bare identifier `mine_stage` does not match that run's real diff either.
    assert _abbreviated_identifier("mine_stage", _V8_TRUE_POSITIVE_REGION) is None
    # ONE part is never an abbreviation of anything: `accum` must not be met by `accumulator`.
    assert _abbreviated_identifier("accum", "+ accumulator = 0\n") is None
    # A part under three characters is not evidence: `a_b` must not reach `alpha_beta`.
    assert _abbreviated_identifier("a_b", "+ alpha_beta = 1\n") is None
    # The diff's identifier may carry EXTRA trailing parts, never FEWER — a claim about
    # `pos_scores_broadcast` is not met by a diff that only mentions `pos_scores`.
    assert _abbreviated_identifier("pos_scores_broadcast", "+ pos_scores = q @ d.T\n") is None
    # And a prefix that is not part-wise is not a match: parts align positionally or not at all.
    assert _abbreviated_identifier("accum_grad", "+ gradient_accumulation_steps = 4\n") is None


def test_the_plain_substring_match_is_the_older_looser_rule_and_is_unchanged():
    """A residue this change deliberately does NOT touch, pinned so nobody reads the bound above as
    covering it. `_claim_met`'s base test is `token in region`, i.e. a SUBSTRING — so `mine_stage`
    has always been met by a `mine_stage_helper` in the diff, before any abbreviation rule existed.
    That is the extractor's oldest and loosest edge and it is left alone on purpose: tightening it to
    an identifier-boundary match is a change to how EVERY claim is scored, with its own corpus
    replay to pay for, and it is not what the 2026-08-15 measurement was about."""
    assert _verdict("Fix mine_stage.py to relax the threshold", ["mine_negatives.py"],
                    "mine_negatives.py\n@@\n+def mine_stage_helper(x):\n").verdict == REPAIR_VERIFIED


def test_a_citation_may_acquit_but_never_convict_and_never_reaches_verified():
    """The asymmetry the demotion is built on, and the reason a model gains nothing by writing into
    it: steering a claim inside a `node N` clause buys `unstated`, which is itself reported."""
    from looplab.engine.repair_verify import _is_citation_only

    # EVERY occurrence must be inside a citation clause. One real promise about the same token is
    # enough to keep the conviction — otherwise one throwaway "node 1 used nll_cos" would launder it.
    both = "node 1 used nll_cos; I deleted the nll_cos path entirely"
    assert not _is_citation_only("nll_cos", both)
    assert _verdict(both, ["train.py"], "train.py\n@@\n-x = 1\n+x = 2\n").verdict == REPAIR_UNMET
    # A citation ACQUITS when the diff really contains the token — unchanged behaviour, and the
    # reason the demotion cannot cost a `verified`.
    assert _verdict("node 1 already set rdrop_alpha", ["train.py"],
                    "train.py\n@@\n+rdrop_alpha = 0.1\n").verdict == REPAIR_VERIFIED
    # The demotion's ONLY landing place is `unstated`. Nothing in this rung may turn a token the
    # diff does not contain into a pass.
    demoted = _verdict("node 1 already set rdrop_alpha", ["train.py"],
                       "train.py\n@@\n-x = 1\n+x = 2\n")
    assert demoted.verdict == REPAIR_UNSTATED and demoted.unmet == ()
    assert REPAIR_VERIFIED not in {demoted.verdict}
    # The clause window ends at the clause, not the sentence: a promise after the semicolon stands.
    after = "node 1 reached 0.73; I will set rdrop_alpha"
    assert not _is_citation_only("rdrop_alpha", after)
    assert _verdict(after, ["train.py"], "train.py\n@@\n-x = 1\n").verdict == REPAIR_UNMET


def test_the_historical_corpus_cases_the_docstring_cites_keep_their_verdicts():
    """The regression floor: the rows the module docstring names, replayed as fixtures so a later
    widening of either rule has to move one of them to go green. Every one is verbatim from `runs/`.

    Rules that only ever get tested on the cases they were written for are how an extractor drifts
    into noise — these are the ones they must NOT touch."""
    # rubertlite-dr-unified-v6 node 1 attempt 1: a NEGATED claim ("drop those unsupported args")
    # kept by deleting the `"%params%"` placeholder that passed them. Still `unmet`, and left that
    # way on purpose — a rule for the indirection would have to model the manifest, not the text.
    # What the citation rule DOES do here is drop `train.py`, which sits in the node-1 clause.
    v6 = _verdict(
        "The crash is purely mechanical: node 1 passed three CLI args the harness's train.py "
        "argparse does not define (gradient_accumulation_steps, max_grad_norm, weight_decay). The "
        "fix is to drop those unsupported args and keep the supported set node 0 used "
        "(loss.temperature, batch_size, learning_rate, n_epochs, warmup_ratio), still applying the "
        "symmetric mnsr loss + lr 1e-3 change in the solution code.",
        ["looplab_stages.json"],
        'looplab_stages.json\n@@\n     "python",\n     "-m",\n-    "vectorsearch.train",\n'
        '-    "%params%"\n+    "vectorsearch.train"\n    ],\n-   "gpus": 1,\n')
    assert v6.verdict == REPAIR_UNMET
    assert "train.py" not in v6.unmet, "a token inside the node-1 clause may not convict"
    assert "gradient_accumulation_steps" in v6.unmet, "the promise itself still stands"

    # rubertlite-dense-retrieval node 11 attempt 2: the rationale names the BROKEN component and the
    # repair edits the eval harness. Deliberately left `unmet` — a claim-clause whitelist would
    # demote this whole family, and "you said the bug was in X and did not touch X" is worth saying.
    dense = _verdict(
        "The device mismatch error is a mechanical bug in the NegLogLikelihoodCos_S implementation "
        "— EMA buffers or learnable threshold parameters weren't moved to the correct GPU device. "
        "The idea itself (EMA centering + learnable threshold to stabilize training at sharp "
        "temperature 0.01) is sound and directly addresses the constant-loss failures of nodes 8/9. "
        "Fixing device placement is straightforward.",
        ["looplab_eval.py"],
        "looplab_eval.py\n@@\n+    # CRITICAL: load to CPU first, then move to target device.\n")
    assert dense.verdict == REPAIR_UNMET and "NegLogLikelihoodCos_S" in dense.unmet

    # rubert-dr-0807 node 8 attempt 4: a MIXED row. `cloud_io` / `replace_sampler_ddp` are a history
    # of past fixes; the "Fix:" clause names three mechanisms the diff does not use (it guards the
    # barrier instead). The citations drop out of the reported list, the conviction stands.
    mixed = _verdict(
        "The failures are moving through distinct mechanical PL/DDP API issues (cloud_io, "
        "replace_sampler_ddp, pickle __main__, now process-group-not-initialized), and the InfoNCE "
        "baseline idea is sound. Fix: on a single GPU, disable DDP (set Trainer strategy to "
        "'auto'/'ddp_notebook' or call torch.distributed.init_process_group before any distributed "
        "op) so the group-size call never runs uninitialized.",
        ["train.py"],
        "train.py\n@@\n-            torch.distributed.barrier()\n"
        "+            if torch.distributed.is_initialized():\n"
        "+                torch.distributed.barrier()\n")
    assert mixed.verdict == REPAIR_UNMET and "init_process_group" in mixed.unmet

    # THE NEGATIVE CONTROL. The same three rules over a repair that kept its promise exactly, in the
    # codebase's own spelling, with no citation anywhere: nothing here may make that anything but
    # `verified`, and nothing may make an ordinary broken promise anything but `unmet`.
    assert _verdict("cutting n_epochs 10 -> 5 to fit the budget", ["train.py"],
                    "train.py\n@@\n-n_epochs = 10\n+n_epochs = 5\n").verdict == REPAIR_VERIFIED
    assert _verdict("cutting n_epochs 10 -> 5 to fit the budget", ["train.py"],
                    "train.py\n@@\n-lr = 1e-4\n+lr = 3e-4\n").verdict == REPAIR_UNMET


def test_neither_demotion_can_reach_the_byte_anchored_verdict_or_move_a_stop():
    """The tiering, re-proved against the new rules. `inert` is the ONLY verdict the loop acts on and
    it is decided before the rationale is read, so no wording — citation-shaped, abbreviation-shaped
    or otherwise — can reach it, and `inert_streak` cannot move because of anything written here."""
    for rationale in (_V8_CITED_BASELINE, _V8_ABBREVIATION, _V8_TRUE_POSITIVE,
                      "node 1 used gradient_accumulation_steps = 4"):
        v = verify_repair(rationale, changed=[], deleted=[], code_changed=False,
                          region="+ gradient_accumulation_steps = 4\nmine_stage.py\n")
        assert v.verdict == REPAIR_INERT and v.actionable and v.unmet == ()
    # And the streak reads the byte verdict only: a row demoted from `unmet` to `unstated` breaks a
    # chain exactly the way `unmet` did, because neither is `inert`.
    assert inert_streak([{"verified": REPAIR_INERT}, {"verified": REPAIR_UNSTATED}]) == 0
    assert inert_streak([{"verified": REPAIR_INERT}, {"verified": REPAIR_INERT}]) == 2


def test_the_engine_stamps_the_withdrawn_verdicts_on_the_durable_row(tmp_path):
    """Tier 1, through the REAL loop: the two withdrawals are not a property of a pure function, they
    are what `node_repaired.verified` says and therefore what a resumed judge reads back.

    Driven with the abbreviation case because it is the one that changes what the judge is TOLD: the
    row goes from `unmet ['grad_accum']` — which `_format_repair_log` renders as an accusation — to
    `verified`, which renders as nothing at all."""

    class _AbbrevDev:
        """Makes the change in the CODEBASE's spelling, which is what the rationale abbreviates."""

        def __init__(self):
            self.repair_calls = 0

        def implement(self, idea):
            return _BAD

        def repair(self, idea, code, error):
            self.repair_calls += 1
            return f"{_BAD}gradient_accumulation_steps = 4\nbatch_size = 4096\n"

    evs, _ = _drive(tmp_path / "abbrev", _AbbrevDev(), _Judge(rationale=_V8_ABBREVIATION),
                    inline_repair_attempts=1)
    row = _repairs(evs)[0].data
    assert row["verified"] == REPAIR_VERIFIED, (row["verified"], row.get("unmet"))
    assert row["unmet"] == []
    # The judge is told NOTHING about this row, which is the point — a kept promise earns no note.
    rendered = _format_repair_log([{"attempt": 1, "error": "e", "fix": _V8_ABBREVIATION,
                                    "changed": ["train.py"], "stages_passed": 0,
                                    "verified": row["verified"], "unmet": row["unmet"]}])
    assert "could not find what this fix said it would change" not in rendered


def test_the_true_positive_still_reaches_the_judge_as_an_accusation(tmp_path):
    """The other half of the same tier-1 proof, and the one that must never go quiet: a repair that
    named its own stage script and edited something else is still stamped `unmet` on the durable row
    AND still rendered to the judge in the engine's own voice."""
    evs, _ = _drive(tmp_path / "truepos", _MovingDev(), _Judge(rationale=_V8_TRUE_POSITIVE),
                    inline_repair_attempts=1)
    row = _repairs(evs)[0].data
    assert row["verified"] == REPAIR_UNMET, (row["verified"], row.get("unmet"))
    assert "mine_stage.py" in row["unmet"]
    rendered = _format_repair_log([{"attempt": 1, "error": "e", "fix": _V8_TRUE_POSITIVE,
                                    "changed": ["mine_negatives.py"], "stages_passed": 0,
                                    "verified": row["verified"], "unmet": row["unmet"]}])
    assert "could not find what this fix said it would change" in rendered
    assert "mine_stage.py" in rendered


# ------------------------------------------------------------------ the claim extractor
def test_the_operators_own_instance_reads_as_an_unmet_claim():
    """"cutting epochs 10 -> 5" over a diff that does not touch the epoch count. The verdict is
    `unmet` — evidence, never a stop — because the rationale is text the agent wrote."""
    region = "train.py\n@@\n-    lr = 1e-4\n+    lr = 3e-4\n"
    v = verify_repair("cutting n_epochs 10 -> 5 to fit the budget",
                      changed=["train.py"], region=region)
    assert v.verdict == REPAIR_UNMET and "n_epochs" in v.unmet and not v.actionable

    met = verify_repair("cutting n_epochs 10 -> 5 to fit the budget", changed=["train.py"],
                        region="train.py\n@@\n-    n_epochs = 10\n+    n_epochs = 5\n")
    assert met.verdict == REPAIR_VERIFIED and met.unmet == ()


def test_json_manifest_context_is_why_the_region_carries_context():
    """A stage manifest is JSON, so `--gpus` and its value are on ADJACENT lines. Read with no
    context, seven real `--gpus 2 -> 1` repairs in rubertlite-dr-unified-v4 scored as unmet."""
    old = '{\n "command": [\n  "train.py",\n  "--gpus",\n  "2",\n  "--lr",\n  "0.001"\n ]\n}\n'
    new = old.replace('"2"', '"1"')
    region = changed_region({"looplab_stages.json": old}, {"looplab_stages.json": new}, "", "")
    assert "--gpus" in region
    v = verify_repair("change --gpus 2 to --gpus 1", changed=["looplab_stages.json"], region=region)
    assert v.verdict == REPAIR_VERIFIED


def test_an_empty_change_set_is_decided_on_bytes_and_never_on_the_rationale():
    """The tier that is allowed to stop the loop must be unreachable by wording — including a
    rationale that quotes the diff it did not make."""
    for rationale in ("", "I fixed n_epochs and train.py and --gpus", "vague progress was made",
                      "-    n_epochs = 10\n+    n_epochs = 5"):
        v = verify_repair(rationale, changed=[], deleted=[], code_changed=False,
                          region="n_epochs = 5\ntrain.py\n--gpus")
        assert v.verdict == REPAIR_INERT and v.actionable and v.unmet == ()
    # The whole-file artifact counts as a change even when `files` is empty (a toy/whole-file task).
    assert verify_repair("x", changed=[], code_changed=True, region="").verdict == REPAIR_UNSTATED


def test_prose_is_never_scored_as_a_broken_promise():
    """An over-eager extractor turns this rung into noise the judge learns to ignore, which costs
    more than a missed mismatch. Every name below produced a false `unmet` on the measured corpus."""
    for prose in ("the crash is a mechanical PyTorch in-place operation bug",
                  "Circle Loss is a well-established CVPR 2020 method; the idea is sound",
                  "the NCCL destruction warning indicates a crash during training"):
        assert claimed_tokens(prose) == (), prose
        assert verify_repair(prose, changed=["loss.py"], region="x").verdict == REPAIR_UNSTATED
    # …while real code tokens still extract.
    toks = claimed_tokens("set VS_LOCAL_DATA_ROOT and fix vectorsearch/config.py's tokenize_fn "
                          "to pass 'ddp_spawn'")
    assert {"VS_LOCAL_DATA_ROOT", "vectorsearch/config.py", "tokenize_fn", "ddp_spawn"} <= set(toks)


def test_claimed_tokens_is_total_over_anything_a_model_can_emit():
    """It runs inside the attempt loop on model-authored text of arbitrary shape — every input is an
    answer, never a raise."""
    for junk in (None, 3, b"bytes", "", "   ", "\x00" * 10, "```" * 500, "—" * 5000):
        assert isinstance(claimed_tokens(junk), tuple)


# ------------------------------------------------------------------ the registry (CLAUDE.md)
def test_repair_verdicts_is_the_single_spelling_across_its_three_sites():
    """Two-way: every verdict this tree writes/reads is in the registry, and every member of the
    registry is produced by `verify_repair` (a member nothing can mint is a dead literal that will
    be mis-typed by the next reader)."""
    produced = {
        verify_repair("x", changed=[], code_changed=False).verdict,
        verify_repair("", changed=["a.py"], region="a").verdict,
        verify_repair("fix train_bs", changed=["a.py"], region="a").verdict,
        verify_repair("fix train_bs", changed=["a.py"], region="train_bs = 4").verdict,
    }
    assert produced == set(REPAIR_VERDICTS)

    # …and the two READERS resolve the vocabulary through the registry's NAMES rather than
    # re-spelling the strings. AST, not substrings (CLAUDE.md tier 3): what this pins is that the
    # name is read, so a renamed verdict is an ImportError instead of a silently-never-true `==`.
    # Deliberately not a `"inert" not in source` scan — the durable column is spelled `unmet` too,
    # and a negative pin that cannot tell a field name from a verdict value is a pin that will be
    # deleted by whoever it next annoys.
    assert "REPAIR_VERDICTS" in names_read(ev_mod._durable_repair_ledger)
    fmt = names_read(crash_repair._format_repair_log)
    assert {"REPAIR_INERT", "REPAIR_UNMET"} <= fmt, fmt
    # The one place a verdict may be MINTED is `repair_verify` itself.
    minted = {n for n in names_read(ev_mod.EvaluateMixin._evaluate) if n.startswith("REPAIR_")}
    assert not minted, f"_evaluate must take its verdict from verify_repair, not mint one: {minted}"


def test_a_repair_verdict_is_not_a_triage_verdict_in_either_direction():
    """The rule `metric_salvage.SALVAGE_CAUSE_TRIAGE_ACTION` established and this rung inherits: a
    marker the engine mints is not a verdict a model may emit. Merging the two vocabularies breaks
    the emit schema's enum in one direction and makes a reader treat it as exhaustive in the other.

    `node_repaired` now carries BOTH a `triage_action` and a `verified`, which is exactly why the
    separation has to be enforced rather than merely documented."""
    assert not (set(REPAIR_VERDICTS) & set(TRIAGE_ACTIONS))
    assert not (set(REPAIR_VERDICTS) & set(AGENT_TRIAGE_ACTIONS))
    assert SALVAGE_CAUSE_TRIAGE_ACTION not in REPAIR_VERDICTS
    # The enforcement point, not just the vocabulary: no verdict can arrive off the wire as a
    # triage answer. `coerce_triage_action` fails each one closed.
    for verdict in REPAIR_VERDICTS:
        assert coerce_triage_action(verdict) not in REPAIR_VERDICTS
        assert coerce_triage_action(verdict) in TRIAGE_ACTIONS


def test_the_salvage_cause_row_carries_no_verdict_and_so_cannot_extend_a_streak(tmp_path):
    """`_repair_salvaged_cause` writes a `node_repaired` that bought no re-evaluation. It must not
    be read as an inert repair — it is not an attempt at all (`_durable_repair_ledger` already keeps
    it out of the budget for the same reason)."""
    p = tmp_path / "events.jsonl"
    s = EventStore(p)
    s.append("node_repaired", {"node_id": 0, "generation": 0, "attempt": 1, "changed": [],
                               "verified": REPAIR_INERT, "triage_action": "repair"})
    s.append("node_repaired", {"node_id": 0, "generation": 0, "changed": [],
                               "triage_action": SALVAGE_CAUSE_TRIAGE_ACTION})
    _attempts, rows, _unp = ev_mod._durable_repair_ledger(EventStore(p).read_all(), 0, 0)
    assert len(rows) == 2 and "verified" not in rows[1]
    assert inert_streak(rows) == 0


def test_the_verification_columns_are_additive_and_the_fold_ignores_them(tmp_path):
    """Invariant #5. The fold reads code/files/deleted/footprint off `node_repaired`; the two new
    columns must change nothing it computes, and a log written without them must still fold."""
    def _log(extra):
        p = tmp_path / f"events{len(extra)}.jsonl"
        s = EventStore(p)
        s.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
        s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}, "rationale": ""},
                                  "code": _BAD})
        s.append("node_repaired", dict({"node_id": 0, "attempt": 1, "code": _GOOD}, **extra))
        return fold(EventStore(p).read_all())

    bare = _log({})
    stamped = _log({"verified": REPAIR_UNMET, "unmet": ["n_epochs"]})
    assert bare.model_dump() == stamped.model_dump()
    assert stamped.nodes[0].code == _GOOD


@pytest.mark.parametrize("limit_name", ["INERT_REPAIR_LIMIT"])
def test_the_limit_is_a_named_constant_the_loop_actually_reads(limit_name):
    """The bound must be statable — a magic `>= 2` in the middle of the attempt loop is a rule
    nobody reviews. `_evaluate` reads the name, not the number."""
    assert isinstance(INERT_REPAIR_LIMIT, int) and INERT_REPAIR_LIMIT >= 1
    src = inspect.getsource(ev_mod)
    assert limit_name in src


# ============================================================ the declared-parameter override rung
# A DIFFERENT QUESTION FROM EVERY TEST ABOVE, asked of different inputs. `verify_repair` compares the
# rationale against the diff; `declared_param_overrides` compares the RECORD's declared coordinates
# against the committed bytes and never reads a rationale at all. See `repair_verify`'s docstring for
# the incident. Everything below is tier 1 — the real function over the champion's real bytes, and
# the real engine loop for the two columns.

# The champion's REAL declared params, verbatim from `runs/rubertlite-dr-unified-v8`'s
# `node_created` for node 3 (seq 1510). Kept whole rather than reduced to the two keys under test,
# because the rung has to pick those two out of fifteen without convicting on the other thirteen.
_V8_NODE3_PARAMS = {
    "loss.dcl": 1.0, "loss.rdrop_alpha": 0.5, "loss.temperature": 0.05, "loss.thr": 0.1,
    "loss.uniformity_weight": 0.0, "loss.use_batch_centering": 0.0,
    "train.negatives.mining_type": 1.0, "train.negatives.n_negatives": 2.0,
    "train.training.batch_size": 8192.0, "train.training.gradient_accumulation_steps": 2.0,
    "train.training.learning_rate": 0.001, "train.training.max_grad_norm": 1.0,
    "train.training.n_epochs": 15.0, "train.training.warmup_ratio": 0.2,
    "train.training.weight_decay": 0.1,
}

# …and the head of that node's REAL `vectorsearch/train.py` after attempt 4's repair, verbatim
# (lines 1-33 of the file on disk). The comment is kept because it is the evidence for the SYSTEM
# half of this finding — the repair states that it avoided `config.yaml` to keep the completed
# `mine` stage reusable, which is `_safe_reuse_start`'s non-`.py` clause naming itself as the reason
# a comparison-bearing parameter left the declared config.
_V8_NODE3_TRAIN_PY = '''#!/usr/bin/env python
import os
from loguru import logger

from vectorsearch.config import Config


def main():
    os.environ["TOKENIZERS_PARALLELISM"] = "0"

    config = Config()

    # R-Drop adds a second full forward pass through the model (see NLLCosLoss.forward step 9),
    # roughly doubling forward-activation memory vs the DCL-only parent run. At per-device batch
    # 8192 (x4 sentence-features = 32768 sequences) that OOMs even on a 140GB H200. Halve the
    # per-device batch and double gradient accumulation to keep the SAME effective batch size
    # (8192 x 2 accum = 16384) while fitting in memory. Config is pydantic-mutable, so this is a
    # train.py-only change (no config.yaml edit) that leaves the completed `mine` stage reusable.
    config.train.training.batch_size = 4096
    config.train.training.gradient_accumulation_steps = 4

    train_cfg = config.train
    return train_cfg
'''

# The SAME file as it stood before that repair — node 3's `node_created` shipped no `train.py` at
# all, and its siblings (nodes 0/1/4/5/6/8, all declaring the same 8192/2) carry this shape: the
# config object is read and never written. This is the negative control's real source.
_V8_SIBLING_TRAIN_PY = '''#!/usr/bin/env python
from vectorsearch.config import Config


def main():
    config = Config()
    train_cfg = config.train
    return train_cfg
'''


def test_the_champions_real_shape_is_the_rung_this_was_built_for():
    """THE FINDING, on the exact bytes. `runs/rubertlite-dr-unified-v8` node 3 became the run's
    champion at 0.762048 — +0.0236 over the next node, the other four inside a 0.0017 spread —
    while `idea.params` AND the node's own `config.yaml` both say `batch_size 8192` /
    `gradient_accumulation_steps 2` and the code that ran says 4096 / 4. Only the code said so."""
    rows = [o.as_row() for o in declared_param_overrides(
        _V8_NODE3_PARAMS, {"vectorsearch/train.py": _V8_NODE3_TRAIN_PY})]
    assert rows == [
        {"param": "train.training.batch_size", "declared": 8192.0, "code": 4096.0,
         "file": "vectorsearch/train.py", "line": 19},
        {"param": "train.training.gradient_accumulation_steps", "declared": 2.0, "code": 4.0,
         "file": "vectorsearch/train.py", "line": 20},
    ], rows
    # The other THIRTEEN declared parameters are not convicted. A rung that fired on `loss.dcl`
    # because the word appears in a comment would be noise the reader learns to discount.
    assert {r["param"] for r in rows} == {"train.training.batch_size",
                                          "train.training.gradient_accumulation_steps"}


def test_a_node_whose_code_agrees_with_its_declaration_gains_nothing():
    """THE NEGATIVE CONTROL, and it is a real one: node 3's siblings declare the IDENTICAL fifteen
    parameters and ship a `train.py` that only reads the config. Silence is the whole point — if
    this rung answered on them it would fire on every node in the run and mean nothing."""
    assert declared_param_overrides(
        _V8_NODE3_PARAMS, {"vectorsearch/train.py": _V8_SIBLING_TRAIN_PY}) == ()
    # And an assignment that AGREES with the declaration is equally silent: writing the declared
    # number into code is what the Developer is asked to do, not a divergence.
    agreeing = _V8_SIBLING_TRAIN_PY + "    config.train.training.batch_size = 8192\n"
    assert declared_param_overrides(_V8_NODE3_PARAMS,
                                    {"vectorsearch/train.py": agreeing}) == ()


def test_the_repair_that_introduced_it_is_the_one_attributed():
    """`baseline_files` separates 'this attempt did it' from 'it was already there'. Node 3's
    attempt 5 rewrote the SAME file (it also promised an epoch cut, which never landed) and must not
    be charged a second time for lines attempt 4 wrote — a repair history that accused every later
    attempt of the same divergence would be unreadable."""
    before, after = {}, {"vectorsearch/train.py": _V8_NODE3_TRAIN_PY}
    assert len(declared_param_overrides(_V8_NODE3_PARAMS, after, baseline_files=before)) == 2
    # Attempt 5: same file rewritten, same two assignments already present.
    again = _V8_NODE3_TRAIN_PY + "    logger.info(config)\n"
    assert declared_param_overrides(_V8_NODE3_PARAMS, {"vectorsearch/train.py": again},
                                    baseline_files=after) == ()
    # …while the WHOLE-NODE question still answers, which is what `champion_caveats` asks.
    assert len(declared_param_overrides(_V8_NODE3_PARAMS,
                                        {"vectorsearch/train.py": again})) == 2


def test_a_bare_name_declaration_is_never_convicted():
    """`PARAM_OVERRIDE_MIN_PARTS`. A dotted key is a path into a config object; `lr` is a word, and
    any local of that name in any file would meet it. The corpus has exactly one instance of the
    shape this refuses — `rubertlite-dense-retrieval` node 36 declares a bare `distill_alpha=0.5`
    and `train.py:117` assigns 0.0 inside a missing-teacher fallback branch — and refusing it is the
    decision, not an oversight: it is conditional code, so 'the declaration was overridden' is not
    a fact the bytes support."""
    assert PARAM_OVERRIDE_MIN_PARTS >= 2
    src = "def f(cfg):\n    self.distill_alpha = 0.0\n    lr = 0.5\n"
    assert declared_param_overrides({"distill_alpha": 0.5, "lr": 0.001}, {"t.py": src}) == ()
    # The same value under a DOTTED declaration is checkable and is answered — but the WHOLE
    # declared suffix has to match, so `model.distill_alpha` is NOT met by `self.distill_alpha`.
    # That is what makes the suffix rule safe: only the receiver's own name is ignored.
    assert declared_param_overrides({"model.distill_alpha": 0.5}, {"t.py": src}) == ()
    assert len(declared_param_overrides({"self.distill_alpha": 0.5}, {"t.py": src})) == 1


def test_the_target_is_parsed_and_not_pattern_matched():
    """NON-VACUITY, in the direction that produces a false claim about a real run. The whole
    false-positive family here is text that LOOKS like an assignment — a comment, a docstring, a
    string literal, an `==` comparison — and `ast` does not have any of them. A regex would convict
    on all four."""
    decoys = (
        "# config.train.training.batch_size = 4096\n"
        's = "config.train.training.batch_size = 4096"\n'
        'DOC = """\nconfig.train.training.batch_size = 4096\n"""\n'
        "if config.train.training.batch_size == 4096:\n    pass\n")
    assert declared_param_overrides(_V8_NODE3_PARAMS, {"t.py": decoys}) == ()
    # A subscript path is the same declaration reached by key, and IS read.
    assert len(declared_param_overrides(
        _V8_NODE3_PARAMS,
        {"t.py": 'cfg["train"]["training"]["batch_size"] = 4096\n'})) == 1


def test_a_computed_value_is_not_resolved_and_a_broken_file_is_not_evidence():
    """Two fail-closed edges. A non-literal right-hand side would need a second evaluator to
    compare, and constant-folding agent code is not this rung's job; a file that does not parse is
    an agent writing something, not a statement about a parameter. Both answer silence."""
    computed = "BS = 8192\nconfig.train.training.batch_size = BS // 2\n"
    assert declared_param_overrides(_V8_NODE3_PARAMS, {"t.py": computed}) == ()
    assert declared_param_overrides(_V8_NODE3_PARAMS, {"t.py": "def (:\n"}) == ()
    # `True` is `isinstance(int)`; reporting it as agreeing with a declared 1.0 would be the record
    # asserting something nobody wrote.
    assert declared_param_overrides({"a.flag": 1.0}, {"t.py": "cfg.a.flag = True\n"}) == ()


def test_this_rung_cannot_be_reached_or_evaded_by_anything_the_agent_WROTE_ABOUT_it():
    """THE TRUST LINE, driven rather than asserted. `unmet` is decided over model-authored text and
    is therefore visible-but-not-trusted; this rung reads the DECLARATION and the BYTES, so the same
    bytes must produce the same answer under any rationale at all — including one that denies the
    change and one that is empty."""
    files = {"vectorsearch/train.py": _V8_NODE3_TRAIN_PY}
    base = declared_param_overrides(_V8_NODE3_PARAMS, files)
    assert len(base) == 2
    # The function's signature carries no rationale to give it, which is the structural half.
    assert "rationale" not in inspect.signature(declared_param_overrides).parameters
    # And the behavioural half: `verify_repair` moves under these two texts, this does not.
    denying = verify_repair("I did not touch batch_size or gradient_accumulation_steps.",
                            changed={"vectorsearch/train.py"}, region="")
    claiming = verify_repair("halve per-step batch to 4096 and raise grad_accum to 4",
                            changed={"vectorsearch/train.py"},
                            region=_V8_ABBREVIATION_REGION)
    assert denying.verdict != claiming.verdict, "precondition: the text rung really does move here"
    assert declared_param_overrides(_V8_NODE3_PARAMS, files) == base


# ------------------------------------------------- the two columns, driven through the real engine
class _OverridingDev:
    """A developer whose repair writes an imperative override of a DECLARED parameter into the
    whole-file artifact — node 3's shape, reduced to what the toy sandbox will run. It still fails,
    so the loop runs to the operator's cap rather than to a metric."""

    def implement(self, idea):
        return _BAD

    def repair(self, idea, code, error):
        return (_BAD + "class _C:\n    pass\n"
                "config = _C()\nconfig.train = _C()\nconfig.train.training = _C()\n"
                "config.train.training.batch_size = 4096\n")


def test_the_engine_stamps_the_override_on_the_durable_row_and_the_judge_reads_it(tmp_path):
    """THE PROPERTY, end to end through the real loop: a repair moves a declared coordinate, the
    engine derives that from its own bytes, writes it on the durable `node_repaired` row, and the
    NEXT attempt's judge is told. Nothing here is hand-written into the log."""
    dev, judge = _OverridingDev(), _Judge()
    evs, _ = _drive(tmp_path / "override", dev, judge, inline_repair_attempts=2,
                    seed_params={"train.training.batch_size": 8192.0})

    rows = _repairs(evs)
    assert rows, "expected the loop to make at least one repair"
    overrides = rows[0].data.get("param_overrides")
    assert overrides, f"no param_overrides column: {rows[0].data.keys()}"
    assert overrides[0]["param"] == "train.training.batch_size"
    assert overrides[0]["declared"] == 8192.0 and overrides[0]["code"] == 4096.0
    # …and it reaches the live judge's history, which is where the next repair decision is made.
    history = "\n".join(judge.histories)
    assert "THE EXPERIMENT'S OWN RECORD DECLARES" in history
    assert "train.training.batch_size is declared 8192.0" in history


def test_a_repair_that_moves_no_declared_coordinate_writes_no_column_at_all(tmp_path):
    """NEGATIVE CONTROL for the column. An absent key and an empty list are different facts — the
    first says nobody looked (an old row), the second would say the engine looked and found none —
    and the judge history must render byte-identically to what it always did on such a row."""
    dev, judge = _MovingDev(), _Judge()
    evs, _ = _drive(tmp_path / "clean", dev, judge, inline_repair_attempts=2,
                    seed_params={"train.training.batch_size": 8192.0})

    rows = _repairs(evs)
    assert rows and all("param_overrides" not in r.data for r in rows)
    assert "THE EXPERIMENT'S OWN RECORD DECLARES" not in "\n".join(judge.histories)


def test_the_override_column_is_additive_and_the_fold_ignores_it(tmp_path):
    """Invariant #5, same shape as the verification columns' own test one screen up."""
    def _log(extra):
        p = tmp_path / f"ev{len(extra)}.jsonl"
        s = EventStore(p)
        s.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
        s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}, "rationale": ""},
                                  "code": _BAD})
        s.append("node_repaired", dict({"node_id": 0, "attempt": 1, "code": _GOOD}, **extra))
        return fold(EventStore(p).read_all())

    bare = _log({})
    stamped = _log({"param_overrides": [{"param": "a.b", "declared": 1.0, "code": 2.0,
                                         "file": "t.py", "line": 3}]})
    assert bare.model_dump() == stamped.model_dump()
    # …and it survives the resume path the judge reads it back through.
    p = tmp_path / "durable.jsonl"
    s = EventStore(p)
    s.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": ""},
                              "code": _BAD})
    s.append("node_repaired", {"node_id": 0, "generation": 0, "attempt": 1, "changed": ["t.py"],
                               "verified": REPAIR_VERIFIED, "triage_action": "repair",
                               "param_overrides": [{"param": "a.b", "declared": 1.0, "code": 2.0,
                                                    "file": "t.py", "line": 3}]})
    _attempts, rows, _unp = ev_mod._durable_repair_ledger(EventStore(p).read_all(), 0, 0)
    assert rows[0]["param_overrides"][0]["param"] == "a.b"
    assert "THE EXPERIMENT'S OWN RECORD DECLARES" in _format_repair_log(rows)


def test_a_non_finite_coordinate_is_dropped_on_both_sides():
    """It is REACHABLE and it would break this rung twice. `search/archive.py` carries the same guard
    and names the routes (a `1e309` literal JSON-folds straight to `inf`; NaN is agent-supplied).
    `nan != anything` is True, so a NaN declaration would report a divergence against ANY assignment;
    and the value then rides on a durable event, where `json.dumps` writes a bare `NaN`/`Infinity`
    that is not JSON and that every browser reader of the log fails to parse. Silence is the right
    answer for a coordinate that cannot be compared — and every row this rung DOES emit must survive
    a strict JSON round trip, which is the half a type check alone would not catch."""
    assert declared_param_overrides({"a.b": float("nan")}, {"t.py": "cfg.a.b = 2\n"}) == ()
    assert declared_param_overrides({"a.b": float("inf")}, {"t.py": "cfg.a.b = 2\n"}) == ()
    # …and from the CODE side, where `1e400` is how a literal becomes `inf`.
    assert declared_param_overrides({"a.b": 1.0}, {"t.py": "cfg.a.b = 1e400\n"}) == ()
    rows = [o.as_row() for o in declared_param_overrides({"a.b": 1.0}, {"t.py": "cfg.a.b = 2\n"})]
    assert json.loads(json.dumps(rows, allow_nan=False)) == rows


class _RepeatOverridingDev:
    """Writes the override ONCE and then keeps editing around it — the shape that exposes whether
    the attribution has a prior for the artifact that has no path."""

    def __init__(self):
        self.n = 0

    def implement(self, idea):
        return _BAD

    def repair(self, idea, code, error):
        self.n += 1
        head = (_BAD + "class _C:\n    pass\n"
                "config = _C()\nconfig.train = _C()\nconfig.train.training = _C()\n"
                "config.train.training.batch_size = 4096\n")
        return head + f"# attempt {self.n}\n"


def test_the_whole_file_artifact_is_attributed_by_the_same_rule_as_a_path(tmp_path):
    """THE ARTIFACT THAT HAS NO PATH still needs a prior, and without `baseline_code` it has none —
    so every later attempt would re-report an override the FIRST one introduced and the judge's
    history would accuse each of them of a line none of them wrote. A code-artifact task has no
    `files` at all, so this is the ONLY attribution path those tasks have."""
    dev, judge = _RepeatOverridingDev(), _Judge()
    evs, _ = _drive(tmp_path / "repeat", dev, judge, inline_repair_attempts=3,
                    seed_params={"train.training.batch_size": 8192.0})

    rows = _repairs(evs)
    assert len(rows) >= 2, "precondition: the loop made more than one repair"
    assert rows[0].data.get("param_overrides"), \
        "the attempt that introduced it must carry the column"
    later = [r for r in rows[1:] if "param_overrides" in r.data]
    assert not later, f"a later attempt re-reported an override it did not write: {later}"
    # …and the unit rule underneath it, stated directly.
    before = "config.train.training.batch_size = 4096\n"
    after = before + "# something else\n"
    assert declared_param_overrides({"train.training.batch_size": 8192.0}, {},
                                    code=after, baseline_code=before) == ()
    assert len(declared_param_overrides({"train.training.batch_size": 8192.0}, {},
                                        code=after, baseline_code="")) == 1


def test_the_two_per_token_scans_are_derived_once_per_verification(monkeypatch):
    """Both scans were rebuilt PER CLAIMED TOKEN over inputs that do not change across the loop.

    `_abbreviated_identifier` ran `set(_IDENT_RE.findall(region))` over the whole 40 KB
    `changed_region` for every token `claimed_tokens` produced (uncapped — the intake bound is a
    2,000-character rationale, which yields tens to low hundreds), and `_is_citation_only` rebuilt
    `_citation_clauses(rationale)` for every UNMET one. That is quadratic in exactly the two numbers
    that grow together, on a rung the loop runs once per repair, in a run where one node recorded
    2,345 of them.

    Counted rather than timed: what the fix has to hold is that each derivation happens ONCE, which a
    wall-clock assertion could not state and a source pin could not observe. The verdict is compared
    against the un-instrumented answer in the same breath, because a "fix" that stopped scanning
    would also satisfy the count.
    """
    from looplab.engine import repair_verify as rv

    # Many tokens, none of them met by the region, so BOTH loops run to their full length: the unmet
    # comprehension over every claim, then the citation demotion over every one of those.
    tokens = " ".join(f"alpha_beta_{n}" for n in range(60))
    rationale = f"Fix: rewrite {tokens} and drop lr_schedule_warmup"
    region = "trainer.py\n-    old = 1\n+    new = 2\n" + ("# padding line\n" * 400)
    expected = verify_repair(rationale, changed=["trainer.py"], region=region)

    idents, clauses = [], []
    real_idents, real_clauses = rv.region_identifiers, rv._citation_clauses

    def _idents(value):
        idents.append(value)
        return real_idents(value)

    def _clauses(value):
        clauses.append(value)
        return real_clauses(value)

    monkeypatch.setattr(rv, "region_identifiers", _idents)
    monkeypatch.setattr(rv, "_citation_clauses", _clauses)
    out = verify_repair(rationale, changed=["trainer.py"], region=region)

    assert (out.verdict, out.claims, out.unmet) == (expected.verdict, expected.claims, expected.unmet)
    assert out.claims and len(out.claims) > 50, "the loops did not run to any interesting length"
    assert len(idents) == 1, f"the diff was re-scanned {len(idents)} times for one verification"
    assert len(clauses) == 1, f"the rationale was re-clause-scanned {len(clauses)} times"


def test_the_hoisted_scans_are_still_callable_on_their_own():
    """Both helpers keep deriving their own input when a caller has none — the truth-table tests
    below and any future caller drive them directly, and a required parameter would make the hoist a
    contract change rather than a shared computation."""
    from looplab.engine.repair_verify import _abbreviated_identifier, _is_citation_only

    region = "config.py\n-    gradient_accumulation_steps = 1\n+    gradient_accumulation_steps = 4\n"
    assert _abbreviated_identifier("grad_accum", region) == "gradient_accumulation_steps"
    assert _is_citation_only("nll_cos", "node 1 used nll_cos") is True


def test_the_triage_providers_construction_does_not_park_the_engine_loop(tmp_path):
    """`repair_log_tools` GLOBS the workdir and then `open`s + byte-probes every stage log this
    attempt wrote. It was a plain ARGUMENT to `self._triage_crash(...)` inside `async def _evaluate`,
    so all of that ran on the engine's event loop — and on the geesefs/S3 mounts a run root usually
    lives on, a directory lookup that MISSES costs 105-950 ms (`core/fence.py::_warm_directory_lookup`
    measured it). One node's crash triage therefore stalled every concurrent eval, both watchdogs and
    the run loop, once per repair attempt.

    Same class as `train_monitor._monitor_training`'s (fixed 2026-08-15), at the one site where the
    CONSUMER is not itself offloaded — so the hand-off has to be its own rather than a closure inside
    an existing one.

    Driven through the real `_evaluate`: the production construction is replaced by one that blocks
    for a fixed time, and a sibling task on the same loop counts its own ticks while that is in
    flight. On the loop, the sibling records nothing across the block; off it, the sibling keeps
    running. No source pin can tell those apart.
    """
    import time

    from looplab.engine import evaluate as _ev

    blocked_for = 0.25
    calls: list[float] = []

    def _slow_provider(engine, workdir, log_plan=None, log_snapshot=None):
        calls.append(time.monotonic())
        time.sleep(blocked_for)          # blocking, exactly as a cold-mount glob + open is
        return None                      # the historical "no tools" answer: nothing else changes

    dev, judge = _MovingDev(), _Judge()
    eng = Engine(tmp_path / "run", task=ToyTask.load(TASK), researcher=judge, developer=dev,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 auto_install_deps=False, inline_repair=True, inline_repair_attempts=2)
    eng.store.append("run_started",
                     {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
        "code": dev.implement(None)})

    ticks: list[float] = []

    async def _main():
        import anyio as _anyio

        done = _anyio.Event()

        async def _heartbeat():
            while not done.is_set():
                ticks.append(time.monotonic())
                await _anyio.sleep(0.01)

        async with _anyio.create_task_group() as tg:
            tg.start_soon(_heartbeat)
            with _anyio.move_on_after(180):
                await eng._evaluate(0, _anyio.CapacityLimiter(1), None)
            done.set()

    original = _ev.repair_log_tools
    _ev.repair_log_tools = _slow_provider
    try:
        anyio.run(_main)
    finally:
        _ev.repair_log_tools = original

    assert calls, "the repair loop never built a triage provider — the test proved nothing"
    # The loop must have made progress DURING each block. One tick per 10 ms of a 250 ms block is
    # ~25; require a handful so a slow CI box cannot make a genuinely-blocked loop look responsive.
    for started in calls:
        during = [t for t in ticks if started < t < started + blocked_for]
        assert len(during) >= 3, (
            f"the event loop made {len(during)} tick(s) while the triage provider was being built — "
            "the construction is running on the loop")
