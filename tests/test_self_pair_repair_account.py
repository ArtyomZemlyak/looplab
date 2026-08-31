"""The in-node (self) pair's lesson names what the FOLD kept, or it names nothing.

MEASURED 2026-08-30 over every event log on the box (9 runs folded, 23 nodes in the self-pair
population — a METRIC plus `repairs > 0`):

  * carrying `failed_stage` or `error_reason` ... 0 of 23
  * with a SUPERSEDED `Node.stages[]` row ....... 0 of 23
  * with a repair-ledger row naming a reason .... 19 of 23
  * with a repair-ledger row naming paths ....... 22 of 23

The first number is the defect: `_on_node_failed` is the only writer of those two fields and every
reset clears them, so a node that failed, was repaired IN PLACE and then SCORED carries neither —
and the rendering fell through to its placeholders. The durable lesson read "a node whose 'a
failure' stage failed was repaired in place and then scored" and left the run into the shared
`lessons.jsonl`; the judge prompt read "#3 failed its 'eval' stage" and was handed
`code_diff(b.code, a.code)` over `a is b`, i.e. an EMPTY diff, while demanding "the lesson is what
the REPAIR had to change".

The second number retires the fix direction this item was originally filed with. `Node.stages[]`
is folded LAST-WINS BY NAME, so the retry that finally succeeded REPLACED the failing row —
`stage_row_superseded` is false for the entire population, and "read the superseded stage rows"
would have shipped a second empty source.

These tests drive the REAL fold: every state below is `fold(store.read_all())` over events this
engine's own writers emit, so a field the fold cannot produce cannot be fabricated into one. The
predecessor tests handed `select_comparison_pairs` a `SimpleNamespace` with `failed_stage="mine"`
set — a state no log can fold to — which is exactly why nothing was red.
"""
from __future__ import annotations

import pytest

from looplab.engine.lessons_reconcile import (self_pair_cause_phrase,
                                              self_pair_repair_account)
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import (EV_NODE_CREATED, EV_NODE_EVALUATED, EV_NODE_FAILED,
                                  EV_NODE_REPAIRED, EV_STAGE_FINISHED)


def _idea(rationale="tighten the mining loop"):
    from looplab.core.models import Idea
    return Idea(operator="draft", params={"x": 1.0}, rationale=rationale).model_dump()


def _repaired_then_scored(tmp_path, *, repairs, metric=0.5, node_id=0, stages=("mine", "train"),
                          reason="crash", paths=("vectorsearch/train.py",),
                          rationale="raise the mine stage timeout and fix the argv"):
    """A node that failed a stage, was repaired IN PLACE `repairs` times, and then scored.

    Written as EVENTS and folded, because that is the whole point: `failed_stage`/`error_reason`
    are unreachable on this shape and no fixture may pretend otherwise."""
    store = EventStore(tmp_path / "events.jsonl")
    store.append(EV_NODE_CREATED, {"node_id": node_id, "parent_ids": [], "operator": "draft",
                                   "idea": _idea(), "code": "print(1)", "files": {}})
    for i in range(repairs):
        data = {"node_id": node_id, "generation": 0, "attempt": i + 1,
                "code": f"print({i + 2})", "changed": list(paths), "rationale": rationale}
        if reason is not None:
            data["reason"] = reason
        store.append(EV_NODE_REPAIRED, data)
    for name in stages:
        store.append(EV_STAGE_FINISHED, {"node_id": node_id, "generation": 0, "name": name,
                                         "status": "ok", "exit_code": 0, "seconds": 1.0})
    store.append(EV_NODE_EVALUATED, {"node_id": node_id, "generation": 0, "metric": metric})
    return fold(store.read_all())


# ------------------------------------------------------------------ what the fold actually keeps

def test_the_two_fields_the_rendering_used_to_read_are_empty_on_a_real_fold(tmp_path):
    """The defect, reproduced from events rather than asserted. If this ever goes red the fold has
    started keeping a failure field across a successful terminal and the whole rung can be
    revisited — which is the only thing that would make the old rendering correct."""
    st = _repaired_then_scored(tmp_path, repairs=2)
    node = st.nodes[0]
    assert node.metric == 0.5 and node.repairs == 2, "the self-pair population, from the fold"
    assert node.failed_stage is None
    assert not (node.error_reason or "").strip()


def test_no_stage_row_survives_from_the_superseded_attempt(tmp_path):
    """Why the originally-prescribed source is empty: the fold is last-wins BY NAME, so the retry
    that succeeded overwrote the row that failed."""
    from looplab.core.models import stage_row_superseded

    store = EventStore(tmp_path / "events.jsonl")
    store.append(EV_NODE_CREATED, {"node_id": 0, "parent_ids": [], "operator": "draft",
                                   "idea": _idea(), "code": "c", "files": {}})
    store.append(EV_STAGE_FINISHED, {"node_id": 0, "generation": 0, "name": "train",
                                     "status": "fail", "exit_code": 1, "seconds": 3.0})
    store.append(EV_NODE_REPAIRED, {"node_id": 0, "generation": 0, "attempt": 1, "code": "c2",
                                    "reason": "crash", "changed": ["train.py"]})
    store.append(EV_STAGE_FINISHED, {"node_id": 0, "generation": 0, "name": "train",
                                     "status": "ok", "exit_code": 0, "seconds": 9.0})
    store.append(EV_NODE_EVALUATED, {"node_id": 0, "generation": 0, "metric": 0.7})
    node = fold(store.read_all()).nodes[0]
    assert [s["status"] for s in node.stages] == ["ok"], "the failing row is gone, not superseded"
    assert not any(stage_row_superseded(s, node) for s in node.stages)


# ---------------------------------------------------------------------- the account, and silence

def test_the_account_comes_off_the_repair_ledger(tmp_path):
    st = _repaired_then_scored(tmp_path, repairs=2, reason="timeout",
                               paths=("looplab_stages.json", "vectorsearch/train.py"))
    acct = self_pair_repair_account(st, st.nodes[0])
    assert acct["reasons"] == ["timeout"], "de-duplicated: two repairs for one cause is one cause"
    assert acct["paths"] == ["looplab_stages.json", "vectorsearch/train.py"]
    assert acct["repairs"] == 2 and len(acct["rationales"]) == 2
    assert self_pair_cause_phrase(acct) == "failed with 'timeout'"


def test_a_ledger_with_no_reason_buys_silence_and_not_a_placeholder(tmp_path):
    """The 4 of 23 corpus nodes whose rows predate the `reason` column. Naming a cause nobody
    recorded is the defect; naming none is the fix."""
    st = _repaired_then_scored(tmp_path, repairs=1, reason=None)
    acct = self_pair_repair_account(st, st.nodes[0])
    assert acct["reasons"] == [] and acct["paths"] == ["vectorsearch/train.py"]
    assert self_pair_cause_phrase(acct) == "failed"
    assert "None" not in self_pair_cause_phrase(acct)


def test_another_nodes_repairs_are_not_this_nodes_account(tmp_path):
    """`repair_ledger` is CROSS-NODE by design — it exists so a later Developer learns what a
    SIBLING fixed — so the filter is the whole correctness of the read."""
    store = EventStore(tmp_path / "events.jsonl")
    for nid in (0, 1):
        store.append(EV_NODE_CREATED, {"node_id": nid, "parent_ids": [], "operator": "draft",
                                       "idea": _idea(), "code": "c", "files": {}})
    store.append(EV_NODE_REPAIRED, {"node_id": 0, "generation": 0, "attempt": 1, "code": "a",
                                    "reason": "oom", "changed": ["mine.py"]})
    store.append(EV_NODE_REPAIRED, {"node_id": 1, "generation": 0, "attempt": 1, "code": "b",
                                    "reason": "not_learning", "changed": ["loss.py"]})
    for nid in (0, 1):
        store.append(EV_NODE_EVALUATED, {"node_id": nid, "generation": 0, "metric": 0.4 + nid})
    st = fold(store.read_all())
    assert self_pair_repair_account(st, st.nodes[0])["reasons"] == ["oom"]
    assert self_pair_repair_account(st, st.nodes[1])["reasons"] == ["not_learning"]


def test_a_previous_LIFECYCLE_s_repairs_are_not_this_one_s_account(tmp_path):
    """The generation filter. `node_reset` opens a new lifecycle whose repair budget starts at zero
    (`Node.repairs = 0`), and the ledger deliberately KEEPS the abandoned generation's rows — it is
    a cross-node/cross-lifecycle record of what the run had to fix. Attributing them to the
    lifecycle that actually scored would credit this metric to repairs it never contained."""
    store = EventStore(tmp_path / "events.jsonl")
    store.append(EV_NODE_CREATED, {"node_id": 0, "parent_ids": [], "operator": "draft",
                                   "idea": _idea(), "code": "c", "files": {}})
    store.append(EV_NODE_REPAIRED, {"node_id": 0, "generation": 0, "attempt": 1, "code": "a",
                                    "reason": "oom", "changed": ["abandoned.py"]})
    store.append(EV_NODE_FAILED, {"node_id": 0, "generation": 0, "reason": "oom"})
    store.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "eval"})
    store.append(EV_NODE_REPAIRED, {"node_id": 0, "generation": 1, "attempt": 1, "code": "b",
                                    "reason": "check_failed", "changed": ["kept.py"]})
    store.append(EV_NODE_EVALUATED, {"node_id": 0, "generation": 1, "metric": 0.6})
    st = fold(store.read_all())
    assert st.nodes[0].attempt == 1 and st.nodes[0].repairs == 1
    acct = self_pair_repair_account(st, st.nodes[0])
    assert acct["reasons"] == ["check_failed"] and acct["paths"] == ["kept.py"]


# --------------------------------------------------------------- the two renderings, end to end

def _engine(tmp_path, **kw):
    from pathlib import Path

    from looplab.adapters.toytask import ToyTask
    from looplab.engine.orchestrator import Engine
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree

    task = ToyTask.load(Path(__file__).resolve().parents[1] / "examples" / "toy_task.json")
    r, d = task.build_roles()
    return Engine(tmp_path / "eng", task=task, researcher=r, developer=d,
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=5),
                  memory_dir=str(tmp_path / "mem"), comparative_lessons=True, **kw)


def _self_pair_lesson(eng, st):
    lessons, pairs = eng._comparative_lessons(st, [])
    rows = [lz for lz in lessons if lz.get("evidence") == [0, 0]]
    assert pairs and rows, "the self pair must be offered and distilled"
    return rows[0]


@pytest.mark.parametrize("placeholder", ["a failure", "'eval'"])
def test_the_offline_lesson_carries_no_placeholder(tmp_path, placeholder):
    st = _repaired_then_scored(tmp_path, repairs=2, reason="expect_failed")
    lesson = _self_pair_lesson(_engine(tmp_path), st)
    assert placeholder not in lesson["statement"]


def test_the_offline_lesson_names_the_recorded_cause_and_files(tmp_path):
    st = _repaired_then_scored(tmp_path, repairs=2, reason="expect_failed",
                               paths=("run_mine.py",))
    lesson = _self_pair_lesson(_engine(tmp_path), st)
    assert "expect_failed" in lesson["statement"] and "run_mine.py" in lesson["statement"]
    assert lesson["role"] == "developer"


def test_the_offline_lesson_says_only_repaired_when_no_cause_was_recorded(tmp_path):
    st = _repaired_then_scored(tmp_path, repairs=1, reason=None)
    lesson = _self_pair_lesson(_engine(tmp_path), st)
    assert "failed with" not in lesson["statement"]
    assert "repaired in place" in lesson["statement"]


def test_the_judge_prompt_gets_the_repair_account_the_empty_diff_never_carried(tmp_path,
                                                                               monkeypatch):
    """`code_diff(b.code, a.code)` is `a` against ITSELF here, so the block that demanded 'what the
    REPAIR had to change' showed the judge nothing. The ledger is that answer."""
    class _Fake:
        def __init__(self):
            self.prompts = []

        def complete_text(self, messages):
            self.prompts.append(messages[-1]["content"])
            return "P1 [GOOD] raise the stage timeout before shrinking the schedule\n"

    st = _repaired_then_scored(tmp_path, repairs=3, reason="timeout",
                               paths=("looplab_stages.json",),
                               rationale="raise the train stage timeout to 22000s")
    eng = _engine(tmp_path)
    fake = _Fake()
    monkeypatch.setattr(eng, "_reflect_client", lambda: fake)
    lessons, _ = eng._comparative_lessons(st, [])
    prompt = fake.prompts[0]
    assert "in-node repair" in prompt and "repaired 3 time(s) in place" in prompt
    assert "failed with 'timeout'" in prompt
    assert "'eval' stage" not in prompt
    assert "looplab_stages.json" in prompt
    assert "raise the train stage timeout to 22000s" in prompt
    assert lessons and lessons[0]["role"] == "developer"
