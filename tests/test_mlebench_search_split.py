"""The real MLE-bench path scores the SEARCH on an agent-invisible split and the private answers
ONCE, at finish (doc 52 §5.1 row 3; AIRA₂'s D_search).

Until 2026-09-06 `engine/holdout.py::apply_host_grade` graded every node's submission against the
private test answers and wrote that grade back as `res.metric`, and `build_holdout_idx` returned an
empty partition for the kind — the search hill-climbed the private grade and the champion was a max
over N private draws. `looplab/adapters/mlebench_split.py` carves a pinned slice of the public train
rows out of what the agent sees; the host grades that slice at search time and the champion's public
rows against the private answers once.

The grader children are stubbed (the `mlebench` package is not needed): each stub scores the CSVs it
is handed with an accuracy over one-hot columns and RECORDS which ids it saw, which is the property —
the search never sees a public test id, the private grade never sees a hidden train id, and the
private grade happens exactly once.
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import anyio
import pytest
from pydantic import BaseModel

from looplab.adapters import mlebench_grade, mlebench_split
from looplab.core.errors import ConfigRefusal
from looplab.core.models import Idea
from looplab.engine.orchestrator import Engine
from looplab.events.eventstore import EventStore
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree

CLASSES = ["EAP", "HPL", "MWS"]
TRAIN = [("t1", "the dread shadow horror fear", "HPL"),
         ("t2", "horror dread ancient fear", "HPL"),
         ("t3", "love heart sorrow tears", "MWS"),
         ("t4", "sorrow heart love tears", "MWS"),
         ("t5", "detective analysis logic clue", "EAP"),
         ("t6", "logic analysis detective clue", "EAP")]
TEST = [("e1", "horror dread fear"), ("e2", "love sorrow tears"), ("e3", "detective logic clue")]
# e3 is deliberately mislabelled relative to the keyword rule the solver applies, so the private
# grade (2/3) differs from the search score (1.0) and the two cannot be confused for one another.
PRIVATE = {"e1": "HPL", "e2": "MWS", "e3": "MWS"}


def _csv(header, rows):
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return out.getvalue()


def _rows(text):
    return list(csv.reader(io.StringIO(text)))


def _assets():
    return {"train.csv": _csv(["id", "text", "author"], TRAIN),
            "test.csv": _csv(["id", "text"], TEST),
            "sample_submission.csv": _csv(["id", *CLASSES], [[i, "0.333", "0.333", "0.334"]
                                                             for i, _ in TEST]),
            "description.md": "spooky, in miniature"}


# --------------------------------------------------------------------------- the pure carve


def test_layout_decides_onehot_and_scalar_and_refuses_the_rest():
    lay = mlebench_split.layout(_assets())
    assert (lay.mode, lay.label_col, lay.id_col, lay.target_cols) == ("onehot", "author", "id",
                                                                     tuple(CLASSES))
    scalar = {"train.csv": _csv(["id", "x", "y"], [["a", "1", "2.5"], ["b", "2", "3.5"]]),
              "test.csv": _csv(["id", "x"], [["c", "3"]]),
              "sample_submission.csv": _csv(["id", "y"], [["c", "0"]])}
    assert mlebench_split.layout(scalar).mode == "scalar"
    two = {"train.csv": _csv(["id", "x", "e1", "e2"], [["a", "1", "2", "3"], ["b", "2", "3", "4"]]),
           "test.csv": _csv(["id", "x"], [["c", "3"]]),
           "sample_submission.csv": _csv(["id", "e1", "e2"], [["c", "0", "0"]])}
    assert mlebench_split.layout(two).target_cols == ("e1", "e2")   # nomad's shape: multi-target
    undecidable = {"train.csv": _csv(["id", "x", "label"], [["a", "1", "cat"], ["b", "2", "dog"]]),
                   "test.csv": _csv(["id", "x"], [["c", "3"]]),
                   "sample_submission.csv": _csv(["id", "p_fish", "p_bird"], [["c", "0", "0"]])}
    with pytest.raises(mlebench_split.SplitUndecidable, match="private format"):
        mlebench_split.layout(undecidable)


def test_carve_hides_rows_and_writes_the_answers_in_the_private_format():
    carved = mlebench_split.carve(_assets(), {1, 3})      # t2, t4
    assert carved.hidden_ids == ("t2", "t4")
    train = _rows(carved.assets["train.csv"])
    assert train[0] == ["id", "text", "author"] and [r[0] for r in train[1:]] == ["t1", "t3", "t5", "t6"]
    test = _rows(carved.assets["test.csv"])
    assert test[0] == ["id", "text"]                       # the label column never reaches test.csv
    assert [r[0] for r in test[1:]] == ["e1", "e2", "e3", "t2", "t4"]
    assert test[4] == ["t2", "horror dread ancient fear"]
    sample = _rows(carved.assets["sample_submission.csv"])
    assert [r[0] for r in sample[1:]] == ["e1", "e2", "e3", "t2", "t4"]
    assert sample[4][1:] == ["0.333", "0.333", "0.334"]    # the placeholder row, copied
    answers = _rows(carved.answers_csv)
    assert answers == [["id", *CLASSES], ["t2", "0", "1", "0"], ["t4", "0", "0", "1"]]
    assert carved.assets["description.md"] == "spooky, in miniature"
    # Nothing an agent sees carries the hidden labels.
    assert "t2,horror dread ancient fear,HPL" not in "".join(carved.assets.values())


def test_carve_refuses_id_collisions_and_degenerate_partitions():
    with pytest.raises(mlebench_split.SplitUndecidable, match="no hidden rows"):
        mlebench_split.carve(_assets(), set())
    with pytest.raises(mlebench_split.SplitUndecidable, match="every train row"):
        mlebench_split.carve(_assets(), set(range(6)))
    clash = _assets()
    clash["test.csv"] = _csv(["id", "text"], [("t2", "a public row sharing a train id")])
    with pytest.raises(mlebench_split.SplitUndecidable, match="collide"):
        mlebench_split.carve(clash, {1})


def test_filter_submission_keeps_or_drops_by_id():
    sub = _csv(["id", *CLASSES], [["e1", 1, 0, 0], ["t2", 0, 1, 0], ["e2", 0, 0, 1]])
    kept = _rows(mlebench_split.filter_submission(sub, {"t2"}, keep=True))
    assert kept == [["id", *CLASSES], ["t2", "0", "1", "0"]]
    dropped = _rows(mlebench_split.filter_submission(sub, {"t2"}, keep=False))
    assert [r[0] for r in dropped] == ["id", "e1", "e2"]


# --------------------------------------------------------------------------- through the engine

_SOLVER = '''
import csv, json
rows = list(csv.reader(open("test.csv", newline="", encoding="utf-8")))
header, body = rows[0], rows[1:]
out = [["id", "EAP", "HPL", "MWS"]]
for r in body:
    text = r[1]
    cls = "HPL" if ("horror" in text or "dread" in text) else ("MWS" if ("love" in text or "sorrow" in text) else "EAP")
    out.append([r[0]] + ["1" if c == cls else "0" for c in ("EAP", "HPL", "MWS")])
with open("submission.csv", "w", newline="", encoding="utf-8") as f:
    csv.writer(f).writerows(out)
print(json.dumps({"metric": 0.0}))
'''


class _Task(BaseModel):
    kind: str = "mlebench_real"
    id: str = "spooky"
    goal: str = "predict the author"
    direction: str = "max"          # the stub grader is an accuracy

    def assets(self):
        return _assets()

    def host_grader(self):
        return {"kind": "mlebench", "competition": "spooky", "data_dir": None,
                "submission": "submission.csv", "scorer": "accuracy",
                "predictions": "submission.csv", "timeout": 30.0}

    def gpu_capable(self):
        return False


class _Stub:
    def propose(self, state, parent):
        return Idea(operator="draft", params={})


class _Dev:
    def implement(self, idea):
        return _SOLVER


def _accuracy(submission_text, answers_text):
    truth = {r[0]: r[1:] for r in _rows(answers_text)[1:]}
    hits = total = 0
    for r in _rows(submission_text)[1:]:
        if r[0] in truth:
            total += 1
            hits += int(r[1:].index(max(r[1:], key=float)) == truth[r[0]].index("1"))
    return hits / total if total else None


def _private_answers():
    return _csv(["id", *CLASSES], [[i, *["1" if c == PRIVATE[i] else "0" for c in CLASSES]]
                                   for i, _ in TEST])


@pytest.fixture()
def graders(monkeypatch):
    """Both grader children replaced by accuracy stubs that record the ids they were handed."""
    calls = []

    def search(competition, sub, answers_csv, hidden_ids, data_dir, *, timeout):
        text = mlebench_split.filter_submission(Path(sub).read_text(encoding="utf-8"),
                                                hidden_ids, keep=True)
        calls.append(("search", sorted(r[0] for r in _rows(text)[1:])))
        return _accuracy(text, answers_csv)

    def private(competition, sub, data_dir, *, timeout):
        text = Path(sub).read_text(encoding="utf-8")
        calls.append(("private", sorted(r[0] for r in _rows(text)[1:])))
        score = _accuracy(text, _private_answers())
        return score, {"competition_id": competition, "score": score, "above_median": True}

    monkeypatch.setattr(mlebench_grade, "grade_search_split_in_subprocess", search)
    monkeypatch.setattr(mlebench_grade, "grade_in_subprocess", private)
    return calls


def _engine(rd, **kw):
    return Engine(rd, task=_Task(), researcher=_Stub(), developer=_Dev(),
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=2), **kw)


def test_the_search_sees_the_hidden_slice_and_the_private_answers_once(tmp_path, graders):
    """THE PROTOCOL. MUTATION: grade every node on the private answers again -> the search calls
    vanish, the private calls become one per node, and the champion is a max over private draws."""
    rd = tmp_path / "run"
    engine = _engine(rd, holdout_fraction=0.5)
    hidden = set(engine._search_hidden_ids)
    assert len(hidden) == 3 and hidden <= {"t1", "t2", "t3", "t4", "t5", "t6"}
    state = anyio.run(engine.run)
    assert state.finished
    evaluated = state.evaluated_nodes()
    assert len(evaluated) == 2

    searches = [ids for kind, ids in graders if kind == "search"]
    privates = [ids for kind, ids in graders if kind == "private"]
    assert len(searches) == 2 and all(ids == sorted(hidden) for ids in searches), searches
    assert privates == [["e1", "e2", "e3"]], "the private answers graded once, public rows only"
    # The node metric is the SEARCH score (the rule is perfect on train rows), never the private one.
    assert all(n.metric == pytest.approx(1.0) for n in evaluated)
    best = state.best()
    assert best is not None and best.metric == pytest.approx(1.0)
    assert best.holdout_metric == pytest.approx(2 / 3), "the private grade is the holdout metric"
    rows = [e for e in EventStore(rd / "events.jsonl").read_all() if e.type == "holdout_evaluated"]
    assert len(rows) == 1 and rows[0].data["node_id"] == best.id
    assert rows[0].data["protocol"] == "private_grade" and rows[0].data["n_holdout"] == 3
    assert rows[0].data["metric"] == pytest.approx(2 / 3)
    assert rows[0].data["gap"] == pytest.approx(1 / 3)
    hg = next(e for e in EventStore(rd / "events.jsonl").read_all() if e.type == "host_grading")
    assert hg.data["protocol"] == "search_split" and hg.data["n_hidden"] == 3
    started = next(e for e in EventStore(rd / "events.jsonl").read_all() if e.type == "run_started")
    assert started.data["holdout_fraction"] == pytest.approx(0.5)
    # The medal report exists for the champion alone, written at finish.
    reports = sorted(p.parent.name for p in (rd / "nodes").glob("node_*/mlebench_report.json"))
    assert reports == [f"node_{best.id}"]
    # What the agent saw: the hidden rows are out of train, in test without their label, in sample.
    wd = rd / "nodes" / f"node_{best.id}"
    train = _rows((wd / "train.csv").read_text(encoding="utf-8"))
    assert not ({r[0] for r in train[1:]} & hidden) and len(train) == 4
    test = _rows((wd / "test.csv").read_text(encoding="utf-8"))
    assert {r[0] for r in test[1:]} == {"e1", "e2", "e3"} | hidden and test[0] == ["id", "text"]
    sample = _rows((wd / "sample_submission.csv").read_text(encoding="utf-8"))
    assert {r[0] for r in sample[1:]} == {"e1", "e2", "e3"} | hidden


def test_holdout_fraction_zero_is_the_explicit_legacy_protocol(tmp_path, graders):
    """Every node graded on the private answers, and the log says so — never a silent fallback."""
    rd = tmp_path / "run"
    engine = _engine(rd, holdout_fraction=0.0)
    assert engine._search_hidden_ids == frozenset() and engine._search_answers is None
    state = anyio.run(engine.run)
    assert [k for k, _ in graders] == ["private", "private"]
    assert all(n.metric == pytest.approx(2 / 3) for n in state.evaluated_nodes())
    hg = next(e for e in EventStore(rd / "events.jsonl").read_all() if e.type == "host_grading")
    assert hg.data["protocol"] == "private_per_node" and hg.data["n_hidden"] == 0
    assert not [e for e in EventStore(rd / "events.jsonl").read_all() if e.type == "holdout_evaluated"]
    assert len(list((rd / "nodes").glob("node_*/mlebench_report.json"))) == 2


def test_an_undecidable_layout_refuses_the_run_at_start(tmp_path):
    class _Odd(_Task):
        def assets(self):
            a = _assets()
            a["sample_submission.csv"] = _csv(["id", "p_fish", "p_bird"], [["e1", "0", "0"]])
            return a
    with pytest.raises(ConfigRefusal, match="holdout_fraction=0"):
        Engine(tmp_path / "run", task=_Odd(), researcher=_Stub(), developer=_Dev(),
               sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
               holdout_fraction=0.5)


def test_a_recarve_draws_from_the_original_files(tmp_path):
    """Re-entry rebuilds the partition and carves again: from the public files, never from an
    already-carved train.csv (which would name different rows than the launch did)."""
    engine = _engine(tmp_path / "run", holdout_fraction=0.5)
    once = dict(engine._assets)
    engine._apply_search_split()
    assert engine._assets == once
    assert len(_rows(engine._assets_public["train.csv"])) == 7      # header + 6, untouched
    assert len(_rows(engine._assets["train.csv"])) == 4               # header + 3
    assert engine._build_holdout_idx(0.5, 0) == engine._holdout_idx  # the same rows on rebuild
