"""How far a candidate moved from the SEED PROGRAM, and why that is not what `edit-types` answers.

`edit-types` (row 31) measures each parent->child STEP. A run of twelve one-line tuning steps and a
run that rewrote the training loop once produce comparable per-step tallies and are opposite runs,
so nothing in LoopLab could say how far the thing carrying a metric had travelled from where the run
started — the `no-distance-from-seed-signal` marker in doc 52.

These tests drive the property that separates the two instruments: DISPLACEMENT against the lineage
ROOT, so a change and its undo CANCEL. A test suite that only checked "the numbers are non-zero"
would be satisfied by summing the per-step tallies, which is the implementation this one is not.
"""
from __future__ import annotations

import ast

import pytest
from typer.testing import CliRunner

from looplab.cli import app
from looplab.events.eventstore import EventStore
from looplab.search.seed_distance import (COSMETIC_TYPES, RESIDUE_TYPES, STRUCTURAL_TYPES,
                                          TUNING_TYPES, render_seed_distances, run_seed_distances,
                                          seed_distance)
from looplab.tools.node_diff import EDIT_TYPES

from tests._source_scan import PKG, iter_trees


class _N:
    """The duck-typed node `tools/node_diff.py` reads, so the fixture is the one it already owns."""

    def __init__(self, nid, files=None, parents=(), metric=None):
        self.id, self.files = nid, files or {}
        self.parent_ids = list(parents)
        self.idea = type("I", (), {"params": {}})()
        self.attempt, self.status, self.metric_provenance = 0, "ok", None
        self.metric = metric


class _S:
    def __init__(self, *nodes, direction="min"):
        self.nodes = {n.id: n for n in nodes}
        self.direction = direction


_SEED = "import torch\nlr = 1e-3\nbatch = 32\n"


def test_the_bands_partition_the_shared_edit_vocabulary():
    """The one judgement this module adds on top of `node_diff.EDIT_TYPES`, checked both ways.

    A tenth edit type landing in no band would silently shrink every share's denominator. The module
    asserts this at import; this test states it so the failure names the type rather than an
    ImportError deep in a CLI.
    """
    bands = TUNING_TYPES + STRUCTURAL_TYPES + COSMETIC_TYPES + RESIDUE_TYPES
    assert sorted(bands) == sorted(EDIT_TYPES)
    assert len(bands) == len(set(bands)), "an edit type is claimed by two bands"


def test_a_change_and_its_undo_cancel_because_the_reference_is_the_seed():
    """THE property, and the one a per-step sum cannot have.

    Node 2 restores the seed byte for byte through a node that had changed it. `edit-types` sees two
    real edits along that chain; the distance from the seed is zero — and the re-introduction count
    is what keeps the journey visible rather than lost.
    """
    state = _S(
        _N(0, files={"train.py": _SEED}),
        _N(1, files={"train.py": "import torch\nlr = 5e-4\nbatch = 32\n"}, parents=(0,)),
        _N(2, files={"train.py": _SEED}, parents=(1,)),
    )
    row = seed_distance(state, 2)
    assert row["seed_node_id"] == 0 and row["depth"] == 2
    assert row["recoverable"] is True
    assert row["files"] == 0 and row["lines"] == 0 and row["substantive"] == 0
    assert row["tuning_share"] is None, "no substantive movement has no share to take"
    assert row["reintroduced"] == 1, (
        "the path was thrown away with the displacement — a lineage cycling in place must stay "
        "visible as a re-introduction")


def test_the_movement_is_split_into_tuning_and_structure():
    """A tuning-only descendant and a structural one, over the SAME seed and the same depth."""
    tuned = _S(
        _N(0, files={"train.py": _SEED}),
        _N(1, files={"train.py": "import torch\nlr = 5e-4\nbatch = 64\n"}, parents=(0,)),
    )
    restructured = _S(
        _N(0, files={"train.py": _SEED}),
        _N(1, files={"train.py": "import torch\nimport numpy as np\n"
                                 "def train():\n    lr = 1e-3\n    batch = 32\n"}, parents=(0,)),
    )
    tuning_row = seed_distance(tuned, 1)
    assert tuning_row["tuning"] == 4 and tuning_row["structural"] == 0
    assert tuning_row["tuning_share"] == 1.0
    other_row = seed_distance(restructured, 1)
    assert other_row["structural"] >= 2, other_row
    assert other_row["tuning_share"] is not None and other_row["tuning_share"] < 1.0


def test_reformatting_is_not_movement():
    """`logging`/`comment`/`whitespace` count in `lines` and in NO share denominator.

    A node that added forty print statements has not moved, and a share taken over the total would
    say it moved and did not tune — the way a churn signal lies.
    """
    state = _S(
        _N(0, files={"train.py": _SEED}),
        _N(1, files={"train.py": "import torch\n# tuned down\nprint('start')\n"
                                 "lr = 5e-4\nbatch = 32\n"}, parents=(0,)),
    )
    row = seed_distance(state, 1)
    assert row["cosmetic"] == 2, row
    assert row["substantive"] == row["tuning"] + row["structural"] + row["other"]
    assert row["lines"] == row["substantive"] + row["cosmetic"]
    assert row["tuning_share"] == 1.0, "the comment and the print diluted the tuning share"


def test_a_missing_file_set_is_not_a_node_that_never_moved():
    """The headline property of the module this one is built on, one level up."""
    state = _S(
        _N(0, files={"train.py": _SEED}),
        _N(1, files={}, parents=(0,)),
    )
    row = seed_distance(state, 1)
    assert row["recoverable"] is False and row["lines"] == 0
    report = run_seed_distances(state)
    assert report["measured"] == 0 and report["unreadable"] == 1
    assert "NOT measured" in "\n".join(render_seed_distances(report))


def test_a_seed_is_its_own_reference_and_is_not_a_measured_row():
    state = _S(_N(0, files={"train.py": _SEED}))
    row = seed_distance(state, 0)
    assert row["depth"] == 0 and row["recoverable"] is True and row["lines"] == 0
    report = run_seed_distances(state)
    assert report["seeds"] == 1 and report["measured"] == 0 and report["unreadable"] == 0
    assert "no node in this run has a readable seed" in "\n".join(render_seed_distances(report))


def test_there_is_no_such_node():
    assert seed_distance(_S(_N(0, files={"train.py": _SEED})), 7) is None


@pytest.mark.parametrize("direction,metrics,expect_improved", [
    ("min", (1.0, 0.5), 1),
    ("min", (0.5, 1.0), 0),
    ("max", (0.5, 1.0), 1),
    ("max", (1.0, 0.5), 0),
])
def test_the_gain_against_the_seed_is_direction_aware(direction, metrics, expect_improved):
    """Which nodes count as having improved decides which tuning share the run reports."""
    seed_metric, child_metric = metrics
    state = _S(
        _N(0, files={"train.py": _SEED}, metric=seed_metric),
        _N(1, files={"train.py": "import torch\nlr = 5e-4\nbatch = 64\n"}, parents=(0,),
           metric=child_metric),
        direction=direction,
    )
    report = run_seed_distances(state)
    assert report["direction"] == direction
    assert report["improved"] == expect_improved
    assert (report["improved_tuning_share"] is not None) is bool(expect_improved)
    assert (report["regressed_tuning_share"] is not None) is (not expect_improved)


def test_the_instrument_reads_a_real_run_and_states_what_it_measured(tmp_path):
    """`looplab seed-distance` over a real log, through the fold — no model, no write."""
    rd = tmp_path / "run"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    files = [_SEED,
             "import torch\nlr = 5e-4\nbatch = 32\n",
             "import torch\nlr = 5e-4\nbatch = 64\n"]
    for index, text in enumerate(files):
        store.append("node_created", {"node_id": index, "parent_ids": [index - 1] if index else [],
                                      "operator": "improve", "files": {"train.py": text},
                                      "code": text,
                                      "idea": {"operator": "improve", "params": {}, "rationale": ""}})
        store.append("node_evaluated", {"node_id": index, "metric": 1.0 - 0.1 * index})

    result = CliRunner().invoke(app, ["seed-distance", str(rd)])
    assert result.exit_code == 0, result.output
    assert "distance from the seed program over 2 descendant node(s)" in result.output
    assert "1 seed(s) are their own reference; direction=min" in result.output
    # node 2 moved two hyperparameters from the seed and node 1 one, which is the whole point of
    # measuring against the ROOT: a per-step table reports one edit for each.
    assert "tuning%" in result.output and "100%" in result.output
    assert "improved (2 node(s)): 100%" in result.output
    assert "not a test of that" in result.output, (
        "the instrument stated a field result as if this run had tested it")
    assert not (rd / "seed_distance.json").exists(), "a read-only instrument wrote a sidecar"


def _seed_distance_importers(pkg=PKG) -> set[str]:
    """Files under *pkg* holding a real IMPORT edge to `search/seed_distance.py`.

    *pkg* is a parameter so the guard's own teeth can be checked by running it over a COPY of the
    tree with an import injected — never over the real one.

    Every shape that creates the edge, at module scope or function-local (the CLI's is the latter):
    `import looplab.search.seed_distance`, `from looplab.search.seed_distance import ...`,
    `from looplab.search import seed_distance`, and the relative spellings of the last two — which
    is why the alias NAMES are read beside the module, and why a relative `from . import x` (whose
    `node.module` is None) still answers.
    """
    found: set[str] = set()
    for path, tree in iter_trees(pkg):
        edges: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                edges.add(node.module or "")
                edges.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                edges.update(alias.name for alias in node.names)
        if any("seed_distance" in edge.split(".") for edge in edges):
            found.add(path.name)
    return found


def test_nothing_in_the_loop_reads_the_signal():
    """It is an INSTRUMENT, and doc 17 §11's own warning ("novel != good") is why.

    A distance maximised is a run rewarded for churn. The one importer is the CLI; a selection,
    gate or proposal path importing this module is the change that needs the measurement first.

    AST over the import statements, and not the substring scan this replaced (2026-09-08): that one
    answered `looplab/__init__.py`, which imports nothing here — it carries the `_LAYOUT`
    back-compat map, a table of module-name STRINGS in which EVERY module of the package appears by
    construction. Excluding that one file by name was the alternative and it is the weaker guard:
    the map is not the only place a name can be written without an import (a docstring, a
    `render(prompts, ...)` key, a comment naming the instrument), so the scan kind was wrong rather
    than the file. Reading the `import` / `from ... import` nodes themselves keeps the teeth exactly
    where they belong — a genuine reader in the loop, however deferred, is still an import.
    """
    assert _seed_distance_importers() == {"inspect_cmds.py"}, sorted(_seed_distance_importers())
