"""THE RECORD SAYS 8192 AND THE RUN USED 512 — repairing that, without ever overwriting a fact.

WHY
---
`Idea.params` is a PROPOSAL. Under `params_style: "none"` the engine applies nothing — the Developer
realises the idea by EDITING THE REPO — so a deviation is legitimate and expected. What is not
legitimate is that the durable record keeps only the proposal while every reader downstream presents
it as the parameters that produced the metric.

Measured over every run on disk: **457 comparisons, 41 diverged (9.0%), 18 of them on nodes that
produced a metric.** `runs/e5small-dr-unified-v2` node 1 — the 0.793426 champion — is recorded as
batch 8192 / accum 2 / 15 epochs and RAN 512 / 32 / 3. That record is what put 8192 into the v3 task
goal, and v3 died with three nodes and no metric.

THE TWO PROPERTIES THAT MAKE A RECORD REPAIR SAFE
-------------------------------------------------
1. **A live record always wins.** A backfill is a RECONSTRUCTION — it re-reads a workdir long after
   the eval, on a tree the eval may have rewritten — and a reconstruction may never overwrite a
   measurement made while the run was happening. This is also what makes re-running it idempotent by
   CONSTRUCTION rather than by a check that could drift.
2. **Absence is recorded as absence.** "The workdir is gone, so what ran cannot be recovered" and
   "the proposal is what ran" are opposite statements, and the second is the one every reader
   currently makes by default. A row that cannot recover an answer must SAY so and must never fold
   into an empty record, which is a claim.

The fixtures are the runs on this box, folded through the real `fold`, because what is under test is
a join across `Idea.params`, `Node.files`, the workdir on disk and `bind_applied_params` — four
modules, none of them this one.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import EV_APPLIED_PARAMS_BACKFILLED, EV_NODE_EVALUATED
from looplab.maintenance import backfill_applied_params as bf

_RUNS = Path("/home/jovyan/data/looplab/runs")
_V2 = _RUNS / "e5small-dr-unified-v2"
_V8 = _RUNS / "rubertlite-dr-unified-v8"


def _state(run_dir: Path):
    return fold(EventStore(str(run_dir / "events.jsonl")).read_all())


# ---------------------------------------------------------------- the fold rule, driven directly
class _Ev:
    __slots__ = ("type", "data", "ts", "seq")

    def __init__(self, t, d):
        self.type, self.data, self.ts, self.seq = t, d, 0.0, 0


def _fold_with(base_rows, backfill_row):
    return fold(list(base_rows) + [_Ev(EV_APPLIED_PARAMS_BACKFILLED, backfill_row)])


@pytest.fixture(scope="module")
def v8_rows():
    if not _V8.exists():
        pytest.skip("the v8 run is not on this machine")
    return EventStore(str(_V8 / "events.jsonl")).read_all()


def test_a_backfill_lands_on_a_node_that_has_no_record(v8_rows):
    record = {"authority": "committed", "checked": 11, "declared": 15,
              "applied": {"train.training.batch_size": 4096.0}}
    st = _fold_with(v8_rows, {"node_id": 3, "applied_params": record, "read_at": 1.0,
                              "workdir_digest": "d"})
    got = st.nodes[3].metric_provenance["applied_params"]
    assert got["applied"] == {"train.training.batch_size": 4096.0}
    # …and it is FLAGGED. No surface may present a reconstruction as a measurement, and this is how
    # a surface tells them apart.
    assert got["backfilled"] is True and got["backfilled_at"] == 1.0


def _run_with_live_record(tmp_path, applied):
    """A run whose node_evaluated already carries an applied-params record — built from scratch
    rather than spliced onto a real log, because a synthetic terminal appended after a real one does
    not necessarily fold (generation fencing), and a test whose PREMISE silently fails to land is
    the vacuous green this repo has shipped nine times."""
    run = tmp_path / "r"
    (run / "nodes" / "node_0").mkdir(parents=True)
    (run / "nodes" / "node_0" / "cfg.yaml").write_text("train:\n  batch_size: 512\n",
                                                       encoding="utf-8")
    store = EventStore(str(run / "events.jsonl"))
    store.append("run_started", {"goal": "g"})
    store.append("node_created", {"node_id": 0, "operator": "improve", "code": "",
                                  "files": {"cfg.yaml": "train:\n  batch_size: 512\n"},
                                  "idea": {"operator": "improve",
                                           "params": {"train.batch_size": 8192.0},
                                           "rationale": "r"}})
    store.append(EV_NODE_EVALUATED, {"node_id": 0, "generation": 0, "metric": 0.5,
                                     "metric_provenance": {"applied_params": applied}})
    st = fold(EventStore(str(run / "events.jsonl")).read_all())
    assert st.nodes[0].metric_provenance["applied_params"] == applied   # the premise LANDED
    return run


def test_a_live_record_is_never_overwritten(tmp_path):
    """THE safety property. Also the whole of the idempotence: the second pass finds the record the
    first wrote and declines, with no dedup check anywhere."""
    live = {"authority": "resolved", "applied": {"train.batch_size": 8192.0}}
    run = _run_with_live_record(tmp_path, live)
    store = EventStore(str(run / "events.jsonl"))
    store.append(EV_APPLIED_PARAMS_BACKFILLED,
                 {"node_id": 0, "read_at": 2.0,
                  "applied_params": {"authority": "committed", "applied": {"train.batch_size": 1.0}}})
    st = fold(EventStore(str(run / "events.jsonl")).read_all())
    got = st.nodes[0].metric_provenance["applied_params"]
    assert got == live and "backfilled" not in got
    # …and the planner never even proposes a row for a node that already has one.
    assert bf.plan_run(run) == []


def test_the_same_backfill_applied_twice_is_the_same_state(v8_rows):
    """Idempotence stated over the FOLD, not over a counter the writer keeps."""
    row = {"node_id": 3, "read_at": 3.0,
           "applied_params": {"authority": "committed", "applied": {"a": 1.0}}}
    once = _fold_with(v8_rows, row)
    twice = fold(list(v8_rows) + [_Ev(EV_APPLIED_PARAMS_BACKFILLED, row)] * 2)
    assert (once.nodes[3].metric_provenance["applied_params"]
            == twice.nodes[3].metric_provenance["applied_params"])


def test_an_unrecoverable_row_folds_as_absence_and_not_as_an_empty_record(v8_rows):
    """An EMPTY record is a claim — "the configuration said nothing about anything you declared".
    This is the absence of an answer. A reader that could not tell them apart would fall back to the
    proposal and call it fact."""
    st = _fold_with(v8_rows, {"node_id": 3, "applied_params": None, "read_at": 4.0,
                              "unrecoverable": bf.NO_WORKDIR})
    got = st.nodes[3].metric_provenance["applied_params"]
    assert got["unrecoverable"] == bf.NO_WORKDIR and got["backfilled"] is True
    assert "applied" not in got and "checked" not in got


def test_a_row_with_neither_a_record_nor_a_reason_changes_nothing(v8_rows):
    """A malformed/hand-edited row must leave the node exactly as it was, not stamp it 'backfilled'
    with nothing in it — which would read as "we looked and there was nothing"."""
    before = _state(_V8).nodes[3].metric_provenance
    st = _fold_with(v8_rows, {"node_id": 3, "applied_params": None, "unrecoverable": ""})
    assert st.nodes[3].metric_provenance == before


def test_a_row_for_a_node_that_has_no_metric_provenance_is_ignored(v8_rows):
    """This event repairs what a metric SAYS about itself. A node with no metric has nothing to say,
    and inventing a provenance dict for it would create a record where the run made none."""
    missing = max(st_id for st_id in _state(_V8).nodes) + 50
    st = _fold_with(v8_rows, {"node_id": missing, "applied_params": {"authority": "committed"}})
    assert missing not in st.nodes


# ---------------------------------------------------------------- the reading, over the real runs
def test_the_champion_that_put_8192_into_a_task_goal_is_recovered_as_512():
    """The measurement that started all of this, re-derived from the workdir rather than asserted."""
    if not (_V2 / "nodes" / "node_1").is_dir():
        pytest.skip("the v2 node-1 workdir is not on this machine")
    rows = {r["node_id"]: r for r in bf.plan_run(_V2)}
    rec = rows[1]["applied_params"]
    diverged = {d["param"]: (d["declared"], d["applied"]) for d in rec["diverged"]}
    assert diverged["train.training.batch_size"] == (8192.0, 512.0)
    assert diverged["train.training.gradient_accumulation_steps"] == (2.0, 32.0)
    assert diverged["train.training.n_epochs"] == (15.0, 3.0)


def test_two_carriers_that_disagree_are_recorded_as_a_conflict_not_resolved():
    """v8 node 3: the config document says 8192 while the training script assigns 4096, with the
    Developer's reasoning inline — R-Drop's second forward pass OOMs at 8192 even on a 140 GB H200,
    so it halves the batch and doubles accumulation and deliberately leaves the document alone to
    keep the completed `mine` stage reusable. Picking either number would be an invention; the
    record owes the reader BOTH, with file and line."""
    if not (_V8 / "nodes" / "node_3").is_dir():
        pytest.skip("the v8 node-3 workdir is not on this machine")
    rows = {r["node_id"]: r for r in bf.plan_run(_V8)}
    conflicts = {c["param"]: c for c in rows[3]["applied_params"]["conflicts"]}
    readings = {(r["file"], r["line"]): r["applied"]
                for r in conflicts["train.training.batch_size"]["readings"]}
    assert readings[("vectorsearch/configs/config.yaml", 281)] == 8192.0
    assert readings[("vectorsearch/train.py", 31)] == 4096.0
    # …and it is NOT also reported as a settled divergence, which would state one of them as fact.
    assert "train.training.batch_size" not in {d["param"] for d in
                                               (rows[3]["applied_params"].get("diverged") or [])}


def test_a_node_whose_workdir_is_gone_is_reported_as_such(tmp_path):
    """The row that matters most and produces no numbers at all."""
    run = tmp_path / "r"
    (run / "nodes").mkdir(parents=True)
    store = EventStore(str(run / "events.jsonl"))
    store.append("run_started", {"goal": "g"})
    store.append("node_created", {"node_id": 0, "operator": "improve", "code": "",
                                  "files": {"cfg.yaml": "train:\n  batch_size: 1\n"},
                                  "idea": {"operator": "improve",
                                           "params": {"train.batch_size": 8.0},
                                           "rationale": "r"}})
    store.append(EV_NODE_EVALUATED, {"node_id": 0, "generation": 0, "metric": 0.5,
                                     "metric_provenance": {"subject_bound": False}})
    rows = bf.plan_run(run)
    assert [r["unrecoverable"] for r in rows] == [bf.NO_WORKDIR]
    assert rows[0]["applied_params"] is None
    # …and it folds into a record that says so rather than into silence.
    bf.apply_run(run, rows)
    st = fold(EventStore(str(run / "events.jsonl")).read_all())
    assert st.nodes[0].metric_provenance["applied_params"]["unrecoverable"] == bf.NO_WORKDIR


def test_applying_twice_writes_nothing_the_second_time(tmp_path):
    """Idempotence end to end: the second `plan_run` sees the record the first one folded and has
    nothing to propose."""
    run = tmp_path / "r"
    (run / "nodes" / "node_0").mkdir(parents=True)
    # A REAL carrier shape: `declared_numeric_params` requires at least two dotted parts ("a bare
    # `lr` is a word, not a path"), so a fixture keyed `batch` would exercise the rejection path and
    # look like a passing backfill test while backfilling nothing.
    (run / "nodes" / "node_0" / "cfg.yaml").write_text("train:\n  batch_size: 512\n",
                                                       encoding="utf-8")
    store = EventStore(str(run / "events.jsonl"))
    store.append("run_started", {"goal": "g"})
    store.append("node_created", {"node_id": 0, "operator": "improve", "code": "",
                                  "files": {"cfg.yaml": "train:\n  batch_size: 512\n"},
                                  "idea": {"operator": "improve",
                                           "params": {"train.batch_size": 8192.0},
                                           "rationale": "r"}})
    store.append(EV_NODE_EVALUATED, {"node_id": 0, "generation": 0, "metric": 0.5,
                                     "metric_provenance": {"subject_bound": False}})
    first = bf.plan_run(run)
    assert len(first) == 1 and first[0]["applied_params"]["diverged"]
    assert bf.apply_run(run, first) == 1
    assert bf.plan_run(run) == []
    lines = (run / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert sum(1 for ln in lines if json.loads(ln)["type"] == EV_APPLIED_PARAMS_BACKFILLED) == 1


# ---------------------------------------------------------------- the refusal
def test_a_run_a_live_engine_holds_is_refused(tmp_path):
    """A workdir being written to cannot be read as what ran. Asked by CONTENDING for the lock,
    because `engine.lock` is an EMPTY file holding an flock — a first version parsed it for a pid,
    failed on every run, and (failing closed) reported all eight runs as live, including seven
    finished for days."""
    import fcntl

    run = tmp_path / "r"
    run.mkdir()
    (run / "events.jsonl").write_text("", encoding="utf-8")
    lock = run / "engine.lock"
    lock.touch()
    assert bf._lock_is_live(run) is False           # exists, unheld
    with open(lock, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert bf._lock_is_live(run) is True
        out = bf.backfill(tmp_path, dry_run=True)
        assert "SKIPPED" in out and "live engine" in out
    assert bf._lock_is_live(run) is False           # released again


def test_a_dry_run_writes_nothing(tmp_path):
    run = tmp_path / "r"
    (run / "nodes" / "node_0").mkdir(parents=True)
    # A REAL carrier shape: `declared_numeric_params` requires at least two dotted parts ("a bare
    # `lr` is a word, not a path"), so a fixture keyed `batch` would exercise the rejection path and
    # look like a passing backfill test while backfilling nothing.
    (run / "nodes" / "node_0" / "cfg.yaml").write_text("train:\n  batch_size: 512\n",
                                                       encoding="utf-8")
    store = EventStore(str(run / "events.jsonl"))
    store.append("run_started", {"goal": "g"})
    store.append("node_created", {"node_id": 0, "operator": "improve", "code": "",
                                  "files": {"cfg.yaml": "train:\n  batch_size: 512\n"},
                                  "idea": {"operator": "improve",
                                           "params": {"train.batch_size": 8192.0},
                                           "rationale": "r"}})
    store.append(EV_NODE_EVALUATED, {"node_id": 0, "generation": 0, "metric": 0.5,
                                     "metric_provenance": {"subject_bound": False}})
    before = (run / "events.jsonl").read_bytes()
    out = bf.backfill(tmp_path, dry_run=True)
    assert "DRY RUN — nothing was written." in out
    assert (run / "events.jsonl").read_bytes() == before
    # …and the dry run shows the REAL rows, not a summary of them.
    assert "DIVERGED train.batch_size: declared 8192.0, applied 512.0" in out
