"""A node that trains must say whether its build changed anything at all.

MEASURED across the three e5small runs that have workspaces: 2 of 47 parent edges are nodes whose
SOURCE is byte-identical to the parent they claimed to modify, each having paid a full train to
re-measure that parent.

    e5small-dr-unified-v12  node 15 -> 10   "adding gradient clipping to node 10's UNCLIPPED
                                             footprint" — node 10's config.yaml:279 already read
                                             `max_grad_norm: 1.0`
    e5small-dr-unified-v11  node 10 ->  0   the same 3->6 epoch extension as node 0, restated ten
                                             nodes later in different words

Nothing catches it. The novelty gate keys on operator + params and on a repo task the experiment IS
the code edit (#152); `_intra_batch_dup` compares only siblings of ONE proposal batch, and these
are five and ten generations apart; none of the thirteen `CARD_BUILD_SKIP_REASONS` is "identical to
its parent"; and `workspace_fingerprint`/`substrate_fingerprint` are both about the OPERATOR's
source tree, not the node's built one.

THE OBVIOUS IMPLEMENTATION WOULD NOT HAVE WORKED, which is why `source_tree_digest` exists rather
than a call to `_dir_fingerprint`: that helper returns the git HEAD for a path inside a repo (every
node workspace is one), which is blind to the uncommitted build edits that ARE the experiment, and
its fallback keys on `mtime_ns`, which always differs between two separately-created workspaces.
"""
from __future__ import annotations

import pathlib
import tempfile
import types

import pytest

from looplab.engine.evaluate import EvaluateMixin
from looplab.engine.workspace import source_tree_digest
from looplab.events.types import (BACKGROUND_APPENDABLE, DIAGNOSTIC_EVENTS, EV_NODE_BUILD_DELTA)


def _tree(**files) -> pathlib.Path:
    root = pathlib.Path(tempfile.mkdtemp())
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


# --------------------------------------------------------------------------- the digest

def test_two_trees_with_the_same_source_agree():
    a = _tree(**{"train.py": "x = 1", "configs/config.yaml": "lr: 1e-3"})
    b = _tree(**{"train.py": "x = 1", "configs/config.yaml": "lr: 1e-3"})
    assert source_tree_digest(a) == source_tree_digest(b) != ""


def test_one_changed_byte_separates_them():
    a = _tree(**{"train.py": "x = 1"})
    b = _tree(**{"train.py": "x = 2"})
    assert source_tree_digest(a) != source_tree_digest(b)


def test_an_added_file_separates_them():
    a = _tree(**{"train.py": "x = 1"})
    b = _tree(**{"train.py": "x = 1", "mine.py": "y = 2"})
    assert source_tree_digest(a) != source_tree_digest(b)


def test_checkpoints_and_caches_are_ignored():
    # `experiments/` holds model checkpoints written by the TRAIN, not by the build — hundreds of MB
    # per node. Hashing them would make every trained node differ from every untrained one and say
    # nothing about the proposal.
    a = _tree(**{"train.py": "x = 1"})
    b = _tree(**{"train.py": "x = 1",
                 "experiments/run/checkpoint-996/README.md": "big",
                 "experiments/run/model.py": "irrelevant",
                 "__pycache__/train.cpython-312.pyc": "junk",
                 ".ipynb_checkpoints/train-checkpoint.py": "editor droppings"})
    assert source_tree_digest(a) == source_tree_digest(b)


def test_the_digest_does_not_depend_on_mtime():
    # The trap `_dir_fingerprint`'s fallback falls into: two separately-created workspaces always
    # have different mtimes, so an mtime-keyed digest fires never.
    import os, time
    a = _tree(**{"train.py": "x = 1"})
    b = _tree(**{"train.py": "x = 1"})
    os.utime(b / "train.py", (0, 0))
    assert source_tree_digest(a) == source_tree_digest(b)


def test_a_missing_or_empty_tree_yields_no_digest():
    assert source_tree_digest(pathlib.Path("/nonexistent/never")) == ""
    assert source_tree_digest(_tree(**{"notes.md": "not source"})) == ""


# --------------------------------------------------------------------------- the receipt

def _engine(run_dir):
    rows: list[tuple[str, dict]] = []
    return types.SimpleNamespace(
        run_dir=run_dir,
        store=types.SimpleNamespace(append=lambda k, d: rows.append((k, d))),
    ), rows


def _node(nid, parents):
    return types.SimpleNamespace(id=nid, attempt=0, parent_ids=list(parents))


def _run_with(child_src, parent_src):
    root = pathlib.Path(tempfile.mkdtemp())
    for nid, body in ((7, child_src), (3, parent_src)):
        d = root / "nodes" / f"node_{nid}"
        d.mkdir(parents=True)
        (d / "train.py").write_text(body)
    return root


def test_an_identical_build_names_the_parent_it_duplicates():
    root = _run_with("x = 1", "x = 1")
    engine, rows = _engine(root)
    assert EvaluateMixin._record_node_build_delta(engine, _node(7, [3])) is True
    kind, data = rows[0]
    assert kind == EV_NODE_BUILD_DELTA
    assert data["identical_to"] == [3]
    assert data["parent_ids"] == [3]
    assert data["node_id"] == 7


def test_a_real_change_is_recorded_with_an_empty_collision_list():
    # Not gated on collision: a run where nothing duplicated must stay distinguishable from a run
    # nobody instrumented, the same reason `belief_admission` is not gated on `dropped`.
    root = _run_with("x = 1", "x = 2")
    engine, rows = _engine(root)
    assert EvaluateMixin._record_node_build_delta(engine, _node(7, [3])) is True
    assert rows[0][1]["identical_to"] == []


def test_a_parentless_node_records_nothing():
    root = _run_with("x = 1", "x = 1")
    engine, rows = _engine(root)
    assert EvaluateMixin._record_node_build_delta(engine, _node(7, [])) is False
    assert rows == []


def test_a_store_that_refuses_the_row_never_costs_the_node():
    root = _run_with("x = 1", "x = 1")

    def angry(*_a, **_k):
        raise RuntimeError("the store refused this row")

    engine = types.SimpleNamespace(run_dir=root,
                                   store=types.SimpleNamespace(append=angry))
    assert EvaluateMixin._record_node_build_delta(engine, _node(7, [3])) is False


def test_the_event_is_diagnostic_and_not_background_appendable():
    assert EV_NODE_BUILD_DELTA in DIAGNOSTIC_EVENTS
    assert EV_NODE_BUILD_DELTA not in BACKGROUND_APPENDABLE
