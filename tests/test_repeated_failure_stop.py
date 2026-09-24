"""The repeated-failure floor: a node whose repair did not move its failure stops repairing.

Measured 2026-09-23 on `minionerec-backbones-v7` (eval_parallel=1): node 0 raised
`KeyError: 'history_item_sid'` three times in a row — triage, a repair that edited only
`looplab_stages.json`, the same line again — for ~3 hours while the built node 1 waited for the one
evaluation slot. `engine/repair_judgment.py::repeated_failure_stop` is the rule and
`engine/failure_diagnosis.py::failure_signature` the evidence; this file drives both through the REAL
`Engine` (a subprocess sandbox running a solution that really raises), plus the signature's truth
table and the registry guard.
"""
from __future__ import annotations

import ast
import threading
from pathlib import Path

import anyio
import pytest

from factories import make_engine
from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings, settings_from_snapshot
from looplab.core.models import Idea, NodeStatus
from looplab.engine.evaluate import repair_ledger_row
from looplab.engine.failure_diagnosis import failure_headline, failure_signature, signature_text
from looplab.engine.repair_judgment import (REPAIR_STOP_REASONS, REPAIR_STOP_REPEATED_FAILURE,
                                            REPEATED_FAILURE_REASONS, repeated_failure_stop,
                                            repeated_failure_streak)
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.search.policy import GreedyTree

ROOT = Path(__file__).resolve().parents[1]

# A solution that dies the way v7 node 0 did: a real traceback, a real frame, a quoted key.
_SAME = ("def load_batch(row):\n"
         "    return row['history_item_sid']\n"
         "load_batch({})\n")
_GOOD = "import json; print(json.dumps({'metric': 0.1}))\n"


def _raising(key: str) -> str:
    """A solution that raises a KeyError on `key` — a DIFFERENT failure per distinct key."""
    return f"def load_batch(row):\n    return row[{key!r}]\nload_batch({{}})\n"


class _Judge:
    """Answers `repair` forever, so whatever stops the chain is the floor under test."""

    def __init__(self):
        self.calls = 0

    def propose(self, state, parent):
        return Idea(operator="x", params={"x": 1.0, "y": 1.0})

    def triage_crash(self, node, error, attempt, *, state=None, brief="", history="",
                     stages_passed=None, attempts_left=None):
        self.calls += 1
        return {"action": "repair", "rationale": "fix the stage manifest"}


class _SameFailureDev:
    """Every repair CHANGES the file (so the byte floor cannot fire) and leaves the raising line —
    v7's repairs rewrote `looplab_stages.json` and never touched the code that raised."""

    def __init__(self, first=_SAME):
        self.first = first
        self.repair_calls = 0

    def implement(self, idea):
        return self.first

    def repair(self, idea, code, error):
        self.repair_calls += 1
        return f"{_SAME}# repair {self.repair_calls}: edited the stage manifest\n"


class _MovingFailureDev(_SameFailureDev):
    """Every repair moves the failure to a NEW key — a chain that is changing what fails."""

    def __init__(self):
        super().__init__(first=_raising("key_a"))

    def repair(self, idea, code, error):
        self.repair_calls += 1
        return _raising("key_" + "bcdefghij"[self.repair_calls - 1])


def _drive_one(run_dir, dev, **kw):
    """One seeded node through the REAL repair loop (`Engine._evaluate`), bounded."""
    kw.setdefault("auto_install_deps", False)
    kw.setdefault("inline_repair", True)
    kw.setdefault("inline_repair_attempts", 5)
    judge = _Judge()
    eng = make_engine(run_dir, researcher=judge, developer=dev, n_seeds=1, max_nodes=1, **kw)
    eng.store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
        "code": dev.implement(None)})

    async def _bounded() -> bool:
        with anyio.move_on_after(180) as scope:
            await eng._evaluate(0, anyio.CapacityLimiter(1), None)
        return scope.cancelled_caught

    assert not anyio.run(_bounded), "the inline-repair loop did not terminate"
    return list(EventStore(Path(run_dir) / "events.jsonl").read_all()), judge


def _rows(evs, kind, node_id=0):
    return [e for e in evs if e.type == kind and e.data.get("node_id") == node_id]


# ------------------------------------------------------------------ driven through the engine
def test_the_same_failure_after_a_repair_stops_the_chain_with_a_named_terminal(tmp_path):
    """THE DEFECT. Two identical failures with a repair between them: no second triage, no second
    repair, and a terminal that says which stop fired and on what."""
    dev = _SameFailureDev()
    evs, judge = _drive_one(tmp_path / "same", dev, inline_repair_same_failure_limit=2)

    assert dev.repair_calls == 1 and len(_rows(evs, "node_repaired")) == 1
    assert judge.calls == 1, "the floor must fire ABOVE the triage call — the judge is the 20 min"
    (terminal,) = [e for e in evs if e.type in ("node_evaluated", "node_failed")]
    assert terminal.type == "node_failed"
    d = terminal.data
    assert d["repair_stop"] == REPAIR_STOP_REPEATED_FAILURE
    assert d["reason"] == "crash", "the floor stops; it never re-classifies the failure"
    assert d["triage_action"] == "abandon"
    sig = d["failure_signature"]
    assert sig["exception"] == "KeyError" and sig["message"] == "'history_item_sid'"
    assert sig["where"].endswith(":load_batch")
    # The repair row carries the signature of the failure IT addressed — the durable half of the
    # streak — and it is the same digest the terminal stopped on.
    assert _rows(evs, "node_repaired")[0].data["failure_signature"]["digest"] == sig["digest"]
    # The next proposal's failure reflection reads the first 90 characters of this rationale
    # (`proposal_cues._cue_failure_reflection`): the signature has to be in them.
    node = fold(evs).nodes[0]
    assert node.status is NodeStatus.failed
    assert node.triage_rationale.startswith("repeated failure: KeyError: 'history_item_sid' at ")
    assert "load_batch" in node.triage_rationale[:90]


def test_a_failure_that_moves_keeps_being_repaired_exactly_as_before(tmp_path):
    """0804's shape: every repair changes WHAT fails. The floor never fires; the operator's cap is
    what stops the chain, and its terminal carries no repair_stop."""
    dev = _MovingFailureDev()
    evs, judge = _drive_one(tmp_path / "moving", dev, inline_repair_attempts=3,
                            inline_repair_same_failure_limit=2)

    assert dev.repair_calls == 3 and len(_rows(evs, "node_repaired")) == 3
    digests = [r.data["failure_signature"]["digest"] for r in _rows(evs, "node_repaired")]
    assert len(set(digests)) == 3
    (terminal,) = _rows(evs, "node_failed")
    assert "repair_stop" not in terminal.data and "failure_signature" not in terminal.data
    assert "hard limit" in terminal.data["triage_rationale"]


def test_off_is_the_old_loop_and_writes_no_new_key(tmp_path):
    """0 = off (and the bare library's default): the same repeating failure runs to the operator's
    cap, and no row gains a column — the golden repair loop stays byte-identical."""
    dev = _SameFailureDev()
    evs, _ = _drive_one(tmp_path / "off", dev, inline_repair_attempts=3)

    assert dev.repair_calls == 3
    assert not any("failure_signature" in e.data for e in evs)
    assert not any("repair_stop" in e.data for e in evs)


def test_a_higher_limit_allows_more_repeats(tmp_path):
    dev = _SameFailureDev()
    evs, _ = _drive_one(tmp_path / "three", dev, inline_repair_same_failure_limit=3)
    assert dev.repair_calls == 2
    assert _rows(evs, "node_failed")[0].data["repair_stop"] == REPAIR_STOP_REPEATED_FAILURE


def test_the_streak_survives_a_resume(tmp_path):
    """Invariant #3: a durable repair row carrying the same signature, left by a dead process,
    plus the current failure is already the streak — the resumed process repairs nothing."""
    run_dir = tmp_path / "resume"
    sig = failure_signature(
        "Traceback (most recent call last):\n"
        '  File "/elsewhere/solution.py", line 3, in <module>\n'
        '  File "/elsewhere/solution.py", line 2, in load_batch\n'
        "KeyError: 'history_item_sid'\n")
    dev, judge = _SameFailureDev(), _Judge()
    eng = make_engine(run_dir, researcher=judge, developer=dev, n_seeds=1, max_nodes=1,
                      auto_install_deps=False, inline_repair=True, inline_repair_attempts=5,
                      inline_repair_same_failure_limit=2)
    eng.store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
        "code": _SAME})
    eng.store.append("node_repaired", {
        "node_id": 0, "generation": 0, "attempt": 1, "code": _SAME, "files": {}, "deleted": [],
        "error_in": "KeyError: 'history_item_sid'", "triage_action": "repair",
        "rationale": "fix it", "changed": ["solution.py"], "stages_passed": 0,
        "unparseable_repairs": 0, "failure_signature": sig})

    async def _bounded() -> bool:
        with anyio.move_on_after(180) as scope:
            await eng._evaluate(0, anyio.CapacityLimiter(1), None)
        return scope.cancelled_caught

    assert not anyio.run(_bounded)
    evs = list(EventStore(run_dir / "events.jsonl").read_all())
    assert dev.repair_calls == 0 and judge.calls == 0
    assert _rows(evs, "node_failed")[0].data["repair_stop"] == REPAIR_STOP_REPEATED_FAILURE


class _FirstBuildFailsDev:
    """The FIRST node built raises the same KeyError forever; every other node scores."""

    def __init__(self):
        self._lock = threading.Lock()
        self.builds = 0
        self.repair_calls = 0

    def implement(self, idea):
        with self._lock:
            self.builds += 1
            return _SAME if self.builds == 1 else _GOOD

    def repair(self, idea, code, error):
        self.repair_calls += 1
        return f"{_SAME}# repair {self.repair_calls}\n"


@pytest.mark.parametrize("limit, repairs", [(2, 1), (0, 4)])
def test_the_stopped_node_frees_the_one_eval_slot_for_the_next_node(tmp_path, limit, repairs):
    """THE COST THE FLOOR EXISTS FOR, through `Engine.run` at eval_parallel=1: the repeating node
    ends after ONE repair and the other node is evaluated; the control (floor off) spends the whole
    operator cap on the same line first."""
    dev = _FirstBuildFailsDev()
    eng = make_engine(tmp_path / f"run{limit}", developer=dev, researcher=_Judge(),
                      policy=GreedyTree(n_seeds=2, max_nodes=2), eval_parallel=1,
                      auto_install_deps=False, inline_repair=True, inline_repair_attempts=4,
                      inline_repair_same_failure_limit=limit)
    state = anyio.run(eng.run)
    evs = list(EventStore(tmp_path / f"run{limit}" / "events.jsonl").read_all())

    failed = [n for n in state.nodes.values() if n.status is NodeStatus.failed]
    scored = [n for n in state.nodes.values() if n.status is NodeStatus.evaluated]
    assert len(failed) == 1 and len(scored) == 1, [(n.id, n.status) for n in state.nodes.values()]
    bad, good = failed[0].id, scored[0].id
    assert len(_rows(evs, "node_repaired", bad)) == repairs == dev.repair_calls
    term = _rows(evs, "node_failed", bad)[0]
    assert (term.data.get("repair_stop") == REPAIR_STOP_REPEATED_FAILURE) is (limit > 0)
    # One slot: the two evaluations never overlap, whichever was dispatched first.
    starts = {e.data["node_id"]: e.seq for e in evs if e.type == "node_eval_started"}
    ends = {e.data["node_id"]: e.seq for e in evs if e.type in ("node_failed", "node_evaluated")}
    first, second = sorted((bad, good), key=lambda n: starts[n])
    assert ends[first] < starts[second]


# ------------------------------------------------------------------ the signature, as a table
_TB = ("stage 'train' failed:\nTraceback (most recent call last):\n"
       '  File "{root}/node_0/train.py", line 88, in <module>\n    main()\n'
       '  File "{root}/node_0/train.py", line {line}, in load_batch\n'
       "    x = row['history_item_sid']\n"
       "KeyError: 'history_item_sid'\n")


def test_incidental_bytes_do_not_make_two_identical_failures_differ():
    a = failure_signature(_TB.format(root="/runs/v7", line=42), "train")
    b = failure_signature(_TB.format(root="C:\\runs\\v8", line=57), "train")
    assert a == b and a["where"] == "train.py:load_batch" and a["stage"] == "train"
    oom = "RuntimeError: CUDA out of memory. Tried to allocate {} GiB at 0x{} in /tmp/{}/x.py"
    assert (failure_signature(oom.format("2.00", "7ffe12", "abc"))["digest"]
            == failure_signature(oom.format("8.50", "91aa00", "zzz"))["digest"])


def test_what_the_bug_is_made_of_still_separates_two_failures():
    base = failure_signature(_TB.format(root="/r", line=1), "train")
    assert failure_signature(_TB.format(root="/r", line=1).replace("history_item_sid", "user_id"),
                             "train")["digest"] != base["digest"]           # a different key
    assert failure_signature(_TB.format(root="/r", line=1), "score")["digest"] != base["digest"]
    moved = _TB.format(root="/r", line=1).replace("in load_batch", "in collate")
    assert failure_signature(moved, "train")["digest"] != base["digest"]    # a different place
    assert failure_signature("layer_12 failed\nValueError: bad shape layer_12")["message"] \
        == "bad shape layer_12", "a number inside an identifier is not an incidental number"


def test_no_exception_line_is_no_signature_and_never_a_stop():
    for text in ("", None, "killed", "exit=-9 timed_out=True", "retrying after ValueError"):
        assert failure_signature(text) is None
    assert repeated_failure_stop(signature=None, repair_log=[{"failure_signature": None}],
                                 limit=2, reason="crash") is None


def test_the_signature_names_the_line_the_headline_shows():
    """One ranking rule for both, so the floor never stops over a line the Developer was not shown."""
    text = ("RuntimeError: x\nTraceback (most recent call last):\n"
            '  File "a.py", line 1, in f\nKeyError: \'history_item_sid\' and a longer message\n'
            "torch.distributed.elastic.multiprocessing.errors.ChildFailedError: \n")
    sig = failure_signature(text)
    first = failure_headline(text).split(" | ")[0]
    assert first.startswith(f"{sig['exception']}:") and first[len(sig["exception"]) + 1:].strip() \
        == sig["message"]
    # …and the frame is the innermost one printed ABOVE that line, not the file's first frame.
    assert sig["where"] == "a.py:f"
    plain = "Traceback (most recent call last):\n  File \"a.py\", line 1, in f\nKeyError: 'k'\n"
    assert signature_text(failure_signature(plain)) == "KeyError: 'k' at a.py:f"


def test_a_redactor_that_raises_yields_no_signature():
    def boom(_):
        raise RuntimeError("x")
    assert failure_signature("KeyError: 'k'", redact=boom) is None


# ------------------------------------------------------------------ the rule, as a table
def _sig(d="d1"):
    return {"exception": "KeyError", "message": "'k'", "where": "a.py:f", "stage": "", "digest": d}


def test_the_streak_is_trailing_and_a_row_without_a_signature_breaks_it():
    assert repeated_failure_streak(_sig(), []) == 1
    assert repeated_failure_streak(_sig(), [{"failure_signature": _sig()}]) == 2
    assert repeated_failure_streak(_sig(), [{"failure_signature": _sig()}, {"changed": []}]) == 1
    assert repeated_failure_streak(_sig(), [{"failure_signature": _sig()},
                                            {"failure_signature": _sig("d2")},
                                            {"failure_signature": _sig()}]) == 2
    assert repeated_failure_streak(None, [{"failure_signature": _sig()}]) == 0


def test_the_rule_fires_only_on_a_crash_with_a_positive_limit_after_a_repair():
    log = [{"failure_signature": _sig()}]
    assert repeated_failure_stop(signature=_sig(), repair_log=log, limit=2, reason="crash")
    assert repeated_failure_stop(signature=_sig(), repair_log=log, limit=0, reason="crash") is None
    assert repeated_failure_stop(signature=_sig(), repair_log=[], limit=2, reason="crash") is None
    assert repeated_failure_stop(signature=_sig(), repair_log=log, limit=3, reason="crash") is None
    for engine_final in ("timeout", "not_learning", "diverged", "no_metric", "expect_failed"):
        assert repeated_failure_stop(signature=_sig(), repair_log=log, limit=2,
                                     reason=engine_final) is None
    assert REPEATED_FAILURE_REASONS == ("crash",)
    msg = repeated_failure_stop(signature=_sig(), repair_log=log, limit=2, reason="crash")
    assert len(msg) < 300 and msg.startswith("repeated failure: KeyError: 'k' at a.py:f")


def test_the_ledger_row_reads_the_signature_back_and_absent_stays_absent():
    row = repair_ledger_row({"attempt": 1, "failure_signature": _sig()}, attempts=1)
    assert row["failure_signature"]["digest"] == "d1"
    assert "failure_signature" not in repair_ledger_row({"attempt": 1}, attempts=1)
    assert "failure_signature" not in repair_ledger_row(
        {"attempt": 1, "failure_signature": {"exception": "KeyError"}}, attempts=1)


# ------------------------------------------------------------------ the setting and the registry
def test_the_setting_ships_on_refuses_one_and_a_pre_field_resume_keeps_the_old_loop():
    assert Settings().inline_repair_same_failure_limit == 2
    assert Settings(inline_repair_same_failure_limit=0).inline_repair_same_failure_limit == 0
    with pytest.raises(ValueError):
        Settings(inline_repair_same_failure_limit=1)
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["inline_repair_same_failure_limit"] == 0
    snap = Settings().masked_snapshot()
    snap.pop("inline_repair_same_failure_limit")
    assert settings_from_snapshot(snap).inline_repair_same_failure_limit == 0
    assert settings_from_snapshot(Settings().masked_snapshot()).inline_repair_same_failure_limit == 2


def test_every_repair_stop_the_writer_spells_is_registered():
    """The registry's guard: `evaluate.py` writes `repair_stop` only from a registered constant."""
    src = (ROOT / "looplab" / "engine" / "evaluate.py").read_text(encoding="utf-8")
    written = []
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Subscript)
                and isinstance(node.targets[0].slice, ast.Constant)
                and node.targets[0].slice.value == "repair_stop"):
            written.append(node.value)
    assert written, "the terminal writer no longer stamps repair_stop"
    for value in written:
        assert isinstance(value, ast.Name) and value.id == "REPAIR_STOP_REPEATED_FAILURE", \
            "spell the stop from the registry constant, never a literal"
    assert REPAIR_STOP_REPEATED_FAILURE in REPAIR_STOP_REASONS
    assert len(set(REPAIR_STOP_REASONS)) == len(REPAIR_STOP_REASONS)
