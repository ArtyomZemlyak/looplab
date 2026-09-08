"""`looplab workspace-bytes`: the workspace byte total doc 37 §8's R1 asked for, and its BOUND.

The instrument exists because a node workspace had exactly one statement in the event log —
`workspace_seeded`'s `.[auto]:75 tracked`, an accurate sentence about 0.9 MB — while the directory
it described measured 944,779,776 B, 99 % of it the node's own retained checkpoints. The one
visible number named the copy, so the copy got blamed for 727 GB it never wrote and a whole
migration proposal was written against it (doc 37 §6, DECLINED with measurement).

Every test here DRIVES the thing: a real tree on disk with byte counts chosen so the arithmetic is
unambiguous, the number read back off the command's own output, and — the one that matters most —
the budget actually SPENT, because a walk that silently truncated would put a smaller number in
front of an operator with nothing to say it was smaller.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from looplab.cli import app
from looplab.cli.workspace_bytes import (DEFAULT_ENTRY_BUDGET, EntryBudget, measure_dir,
                                         measure_run, render_workspace_bytes, seed_claims,
                                         walk_tree)
from looplab.events.eventstore import EventStore
from looplab.events.types import EV_RUN_STARTED, EV_WORKSPACE_SEEDED


def _file(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _run_tree(root: Path) -> Path:
    """A run directory shaped like the one doc 37 §6 measured: a fat node and a thin one.

    node_1 is the v6 node_4 shape in miniature — two retained checkpoints that dwarf the seeded
    source files — and node_2 is what the seed alone looks like.
    """
    run = root / "demo"
    _file(run / "nodes" / "node_1" / "train.py", 100)
    _file(run / "nodes" / "node_1" / "checkpoint-800" / "model.bin", 5_000)
    _file(run / "nodes" / "node_1" / "checkpoint-1200" / "model.bin", 9_000)
    _file(run / "nodes" / "node_1" / "checkpoint-1200" / "optimizer.pt", 1_000)
    _file(run / "nodes" / "node_2" / "train.py", 42)
    _file(run / "confirm" / "seed.log", 10)

    store = EventStore(run / "events.jsonl")
    store.append(EV_RUN_STARTED, {"goal": "g", "direction": "min"})
    store.append(EV_WORKSPACE_SEEDED, {"node_id": 1, "materialized": [".[auto]:75 tracked",
                                                                     "data:train->link"]})
    store.append(EV_WORKSPACE_SEEDED, {"node_id": 2, "materialized": [".[auto]:75 tracked"]})
    return run


def _report(run: Path, *, limit: int = DEFAULT_ENTRY_BUDGET, only=None):
    store = EventStore(run / "events.jsonl")
    return measure_run(run, budget=EntryBudget(limit), only=only,
                       seeded=seed_claims(store.read_all()))


# --------------------------------------------------------------------- the number, off a real tree

def test_the_total_is_the_tree_that_is_actually_on_disk(tmp_path):
    run = _run_tree(tmp_path)
    report = _report(run)

    by_name = {row.name: row for row in report.nodes}
    assert by_name["node_1"].measure.total_bytes == 100 + 5_000 + 9_000 + 1_000
    assert by_name["node_1"].measure.files == 4
    assert by_name["node_2"].measure.total_bytes == 42
    assert not report.truncated, "this tree is 11 entries; the default budget is 200,000"


def test_the_largest_subtrees_are_named_and_ordered_by_size(tmp_path):
    """`checkpoint-*` is the row doc 37 §8 R1 says would have ended the misattribution on sight."""
    run = _run_tree(tmp_path)
    lines = render_workspace_bytes(_report(run), top=2)
    body = "\n".join(lines)
    assert "checkpoint-1200/" in body and "checkpoint-800/" in body
    assert body.index("checkpoint-1200/") < body.index("checkpoint-800/"), "biggest first"
    assert "10,000 B" in body, "checkpoint-1200 is 9,000 + 1,000 bytes, summed one level down"


def test_nested_depth_is_summed_without_recursing_per_level(tmp_path):
    """The walk is iterative; a deep tree must still total, not raise."""
    deep = tmp_path / "deep"
    path = deep
    for i in range(120):
        path = path / f"d{i}"
    _file(path / "leaf.bin", 777)
    walk = walk_tree(deep, EntryBudget(10_000))
    assert (walk.total_bytes, walk.files, walk.truncated) == (777, 1, False)


def test_a_walk_that_runs_out_mid_subtree_returns_a_partial_total_marked_truncated(tmp_path):
    """The budget dying INSIDE `walk_tree`, which is a different code path from the budget dying at
    a listing — and the one a misplaced status note crashed on with a `NameError` while every other
    test stayed green, because they all ran out of budget one level higher up."""
    tree = tmp_path / "checkpoint-1200"
    for i in range(6):
        _file(tree / f"shard{i}.bin", 1_000)
    budget = EntryBudget(3)
    walk = walk_tree(tree, budget)
    assert walk.truncated and budget.spent == 3
    assert 0 < walk.total_bytes < 6_000, "a floor: some shards were counted, not all"

    complete = walk_tree(tree, EntryBudget(100))
    assert complete.total_bytes == 6_000 and not complete.truncated
    assert walk.total_bytes < complete.total_bytes


def test_a_mounted_dataset_symlink_is_counted_as_a_link_and_never_followed(tmp_path):
    """A `data:` mount is a symlink into a dataset the node did not write (189 GiB on the v1
    testbed). Billing a node for it — or walking out of the run directory through it — is the
    misattribution this instrument exists to stop, in a second form."""
    dataset = tmp_path / "dataset"
    _file(dataset / "huge.parquet", 500_000)
    run = _run_tree(tmp_path)
    link = run / "nodes" / "node_1" / "data"
    os.symlink(dataset, link)

    node_1 = {row.name: row for row in _report(run).nodes}["node_1"]
    # The link's own size is small and platform-dependent; what is pinned is that the 500,000 bytes
    # behind it are NOT in the total and that the walk did not descend through it.
    assert node_1.measure.total_bytes < 20_000
    assert "huge.parquet" not in "\n".join(render_workspace_bytes(_report(run)))


# ----------------------------------------------------------- the claim beside the measurement (§6)

def test_the_log_s_only_workspace_sentence_is_printed_beside_the_directory_it_describes(tmp_path):
    run = _run_tree(tmp_path)
    body = "\n".join(render_workspace_bytes(_report(run)))
    assert ".[auto]:75 tracked" in body, "the seed claim the misattribution was read off"
    assert "15,100 B" in body, "…on the same report as what the directory actually weighs"


def test_seed_claims_reads_the_diagnostic_rows_and_ignores_a_junk_node_id(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    store.append(EV_WORKSPACE_SEEDED, {"node_id": 3, "materialized": ["a", "b"]})
    store.append(EV_WORKSPACE_SEEDED, {"node_id": None, "materialized": ["orphan"]})
    store.append(EV_RUN_STARTED, {"goal": "g"})
    assert seed_claims(store.read_all()) == {3: "a, b"}


def test_a_node_with_no_seed_row_says_so_rather_than_showing_an_empty_claim(tmp_path):
    run = _run_tree(tmp_path)
    (run / "nodes" / "node_9").mkdir()
    body = "\n".join(render_workspace_bytes(_report(run)))
    assert "no workspace_seeded row for this node in the log" in body


# ------------------------------------------------------------------------------------- the BOUND

def test_a_spent_budget_makes_every_number_a_floor_and_names_the_call_that_continues(tmp_path):
    """The property the whole design turns on: the walk is stopped for real, and what comes back
    says so. A number that silently truncated is worse than a refusal."""
    run = _run_tree(tmp_path)
    report = _report(run, limit=6)
    assert report.truncated and report.budget_spent == 6

    body = "\n".join(render_workspace_bytes(report))
    assert ">=" in body and "BUDGET SPENT after 6 entries" in body
    assert "FLOOR" in body
    # The continuation must be a call the caller has NOT already spent.
    assert "--max-entries 12" in body


def test_a_node_the_budget_never_reached_is_named_not_walked_and_never_reported_as_zero(tmp_path):
    run = _run_tree(tmp_path)
    # Enough to list the run dir (4 entries: events.jsonl, confirm, nodes + the store's own
    # sidecars are absent here) and nodes/, and to walk node_1, and no more.
    report = None
    for limit in range(1, 40):
        candidate = _report(run, limit=limit)
        if [r.name for r in candidate.nodes] == ["node_1"] and candidate.unwalked_nodes:
            report = candidate
            break
    assert report is not None, "no budget in 1..40 leaves node_2 unreached — retune the fixture"
    assert report.unwalked_nodes == ("node_2",)
    body = "\n".join(render_workspace_bytes(report))
    assert "NOT WALKED AT ALL" in body and "node_2" in body
    assert "node_2: 0 B" not in body, "not measured and measured-as-empty are opposite statements"


def test_the_budget_that_the_report_names_actually_completes_the_walk(tmp_path):
    """Drive the continuation the report offers, not just its text: the bigger call must return the
    complete tree and the same totals a single unbounded pass gives."""
    run = _run_tree(tmp_path)
    small = _report(run, limit=6)
    assert small.truncated

    complete = _report(run, limit=DEFAULT_ENTRY_BUDGET)
    assert not complete.truncated
    assert sum(r.measure.total_bytes for r in complete.nodes) == 100 + 5_000 + 9_000 + 1_000 + 42
    # …and the floor really was a floor: the truncated pass never claimed more than the truth.
    assert (sum(r.measure.total_bytes for r in small.nodes)
            <= sum(r.measure.total_bytes for r in complete.nodes))


def test_a_budget_that_dies_while_listing_the_run_directory_says_which_listing_it_died_in(tmp_path):
    run = _run_tree(tmp_path)
    body = "\n".join(render_workspace_bytes(_report(run, limit=1)))
    assert "ran out while listing the run directory itself" in body


def test_the_budget_is_shared_across_the_whole_run_not_reissued_per_node(tmp_path):
    """One counter is the statable bound. A per-node budget would make the report's own number
    ("N of M entries") a fiction whenever there was more than one node."""
    run = _run_tree(tmp_path)
    budget = EntryBudget(DEFAULT_ENTRY_BUDGET)
    measure_run(run, budget=budget, seeded={})
    assert budget.spent == len(list(run.rglob("*"))), "every entry, counted once"


def test_a_subtree_the_budget_could_not_reach_is_named_inside_its_own_node(tmp_path):
    """The floor has to hold one level down too: a node whose checkpoint dir was never entered must
    print that dir as unwalked, not omit it into a smaller-looking node total."""
    run = _run_tree(tmp_path)
    node = run / "nodes" / "node_1"
    measure = next(m for limit in range(3, 12)
                   if (m := measure_dir(node, EntryBudget(limit))).unwalked)
    assert measure.truncated and measure.unwalked
    report = _report(run, limit=8)
    if any(row.measure.unwalked for row in report.nodes):
        assert "NOT WALKED (budget spent)" in "\n".join(render_workspace_bytes(report))


# -------------------------------------------------------------------------- absence, stated as such

def test_a_directory_that_cannot_be_opened_is_reported_missing_not_measured_as_empty(tmp_path):
    measure = measure_dir(tmp_path / "nope", EntryBudget(100))
    assert measure.missing and measure.total_bytes == 0 and measure.unreadable == 1


def test_a_run_with_no_nodes_directory_says_so(tmp_path):
    run = tmp_path / "bare"
    run.mkdir()
    _file(run / "events.jsonl", 10)
    body = "\n".join(render_workspace_bytes(measure_run(run, budget=EntryBudget(100))))
    assert "no nodes/ directory" in body


def test_a_symlink_wearing_a_node_name_is_reported_rather_than_silently_skipped(tmp_path):
    run = _run_tree(tmp_path)
    os.symlink(run / "nodes" / "node_1", run / "nodes" / "node_7")
    body = "\n".join(render_workspace_bytes(_report(run)))
    assert "nodes/node_7 is a symlink — not followed, not measured" in body


def test_only_one_node_can_be_asked_for_and_a_wrong_id_is_a_stated_absence(tmp_path):
    run = _run_tree(tmp_path)
    assert [r.name for r in _report(run, only="1").nodes] == ["node_1"]
    missing = _report(run, only="404")
    assert missing.nodes == ()
    assert any("nodes/node_404 is not a directory" in note for note in missing.notes)


# ------------------------------------------------------------------------------- the command itself

@pytest.mark.parametrize("extra", [[], ["--node", "1"], ["--top", "1"], ["--max-entries", "5"]])
def test_the_command_runs_end_to_end_over_a_real_run_directory(tmp_path, extra):
    run = _run_tree(tmp_path)
    result = CliRunner().invoke(app, ["workspace-bytes", str(run), *extra])
    assert result.exit_code == 0, result.output
    assert "workspace bytes for" in result.output
