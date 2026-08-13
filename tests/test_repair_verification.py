"""The repair-verification rung: did the repair DO what its rationale said it would do?

Drives the REAL inline-repair loop (`Engine._evaluate`) over a real sandbox, because the property
these tests exist for is "the effect landed", not "the call happened" — see CLAUDE.md's tiering.
The measurement that motivated the rung, and the two-tier design it forced, are in
`looplab/engine/repair_verify.py`'s docstring.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import anyio
import pytest

from looplab.adapters.toytask import ToyTask
from looplab.core.models import Idea, NodeStatus
from looplab.engine import crash_repair, evaluate as ev_mod
from looplab.engine.crash_repair import _format_repair_log
from looplab.engine.orchestrator import Engine
from looplab.engine.repair_verify import (INERT_REPAIR_LIMIT, REPAIR_INERT, REPAIR_UNMET,
                                          REPAIR_UNSTATED, REPAIR_VERDICTS, REPAIR_VERIFIED,
                                          changed_region, claimed_tokens, inert_streak,
                                          verify_repair)
from looplab.engine.triage import AGENT_TRIAGE_ACTIONS, TRIAGE_ACTIONS, coerce_triage_action
from looplab.engine.metric_salvage import SALVAGE_CAUSE_TRIAGE_ACTION
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
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


def _drive(run_dir, dev, judge, **kw):
    """Seed one node and run the REAL repair loop over it (no genesis, no policy search)."""
    kw.setdefault("auto_install_deps", False)
    kw.setdefault("inline_repair", True)
    eng = Engine(run_dir, task=ToyTask.load(TASK), researcher=judge, developer=dev,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1), **kw)
    eng.store.append("run_started",
                     {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
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
