"""Golden-log replay gate (docs/15 §P5.1).

`tests/data/golden_run_events.jsonl` is a REAL offline run's event log (the quadratic smoke,
`--no-genesis --kind quadratic`); `golden_run_state.json` is the byte-stable `fold(...)` output
captured as the current baseline. Any change to `fold` (or to a model default a folded field
depends on) that alters the produced `RunState` for an existing log — the exact regression class
the dispatch-table refactor must not introduce — turns this red.

AN ADDITIVE **EVENT** FIELD KEEPS IT GREEN; AN ADDITIVE **MODEL** FIELD DOES NOT, and this
paragraph said both did until 2026-08-27. The golden LOG carries only what its writer wrote, so a
new event key is simply absent from it — but the comparison is against `model_dump()`, which emits
every field the model declares, so a new `RunState`/`Node`/`Idea`/memo field appears in `got` at its
default and in the checked-in snapshot not at all. That is a real difference and this test is right
to report it; what it is NOT is a fold semantics change, and believing the sentence above is why the
snapshot went stale rather than being regenerated in the change that added the field.

So READ THE DIFF BEFORE REGENERATING. Additions with no value changes (`got` has a key the snapshot
lacks, and every shared leaf is equal) are the additive case and the snapshot is simply behind. A
changed VALUE on a shared key is the regression this file exists to catch, and regenerating over one
would erase the only thing that noticed. `git diff --stat` on the snapshot is the cheap check: pure
insertions is the first case, any deletion is the second.

If this fails INTENTIONALLY (a deliberate fold semantics change), regenerate the snapshot in
the same change and say why in the commit:
    python - <<'PY'
    import orjson
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold
    d = fold(EventStore('tests/data/golden_run_events.jsonl').read_all()).model_dump(mode="json")
    open('tests/data/golden_run_state.json', 'wb').write(
        orjson.dumps(d, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))
    PY

Regenerations so far, each one an ADDITIVE model field (the case the paragraph above names), with
the diff that justified it:

* 2026-09-08 — `RunState.eval_noise_floor` + `RunState.eval_noise_seed_results` (`core/models.py`,
  doc 52 row 11: the eval NOISE FLOOR's summary and its per-seed resume memo). The diff was 2
  insertions and 0 deletions — `"eval_noise_floor": null` and `"eval_noise_seed_results": {}`, the
  defaults a log with no `eval_noise_*` row folds to — and no shared leaf changed, so this log
  still folds byte-identically to what it folded before and no recorded run is invalidated.
* 2026-09-08 — `Node.value_prior` (`core/models.py`, docs/BACKLOG.md §0.1 row 17: the MCTS value
  estimate the LLM freezes onto the node). The diff was 8 insertions and 0 deletions — one
  `"value_prior": null` per node, no shared leaf changed — so the fold produces byte-identically
  what it produced before for this log and nothing recorded under the old snapshot is invalidated.
"""
from __future__ import annotations

from pathlib import Path

import anyio
import orjson

from looplab.adapters.toytask import ToyTask
from looplab.core.models import NodeStatus
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from tests.factories import TOY_TASK, make_engine

_DATA = Path(__file__).parent / "data"


def test_golden_log_folds_to_the_checked_in_state():
    evs = EventStore(_DATA / "golden_run_events.jsonl").read_all()
    assert evs, "golden log missing/empty"
    got = fold(evs).model_dump(mode="json")
    want = orjson.loads((_DATA / "golden_run_state.json").read_bytes())
    assert got == want


def test_golden_log_fold_is_idempotent_and_prefix_stable():
    evs = EventStore(_DATA / "golden_run_events.jsonl").read_all()
    a, b = fold(evs), fold(evs)
    assert a.model_dump(mode="json") == b.model_dump(mode="json")   # no hidden state across calls
    # every prefix folds without error (resume replays prefixes constantly)
    for i in range(1, len(evs) + 1):
        fold(evs[:i])


# ------------------------------------------------------------ the 2-wide parallel-build golden
# Doc 22 phase 4 specified "a new golden for a 2-wide parallel-build run (ids monotonic, one
# terminal per node, deterministic replay)" and it was never added (doc 52 row 27). Its own status
# note says why a checked-in LOG would be wrong: the fan-out's byte order is DELIBERATELY
# nondeterministic (two builds append their own rows), so a golden log would over-pin the one thing
# the seam does not promise. What the seam promises is pinned instead, the way that note lays out:
#   (a) the fold of a real 2-wide run keeps the fan-out invariants — ids reserved serially and
#       dense, each node's `node_created` after its own `node_building`, exactly one terminal;
#   (b) the fold is deterministic: twice over one read, and over a second `EventStore` open;
#   (c) two runs with the same scripted roles fold to ONE state once the run-identity and
#       wall-clock fields are masked, while their logs are free to differ in order — the
#       order-tolerance the build fan-out (CLAUDE.md invariant 1) actually promises;
# and a checked-in golden PROJECTION — the order-independent view of the search's result — plays
# the role the serial golden STATE plays: a fold or policy change that alters what a 2-wide run
# finds turns it red. Regenerate it in the same change and say why:
#     python - <<'PY'
#     import orjson
#     from tests.test_golden_replay import _parallel_run, _projection
#     from looplab.events.replay import fold
#     import tempfile, pathlib
#     engine = _parallel_run(pathlib.Path(tempfile.mkdtemp()) / "run")
#     open("tests/data/golden_parallel_projection.json", "wb").write(orjson.dumps(
#         _projection(fold(engine.store.read_all())), option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))
#     PY
# `tests/test_strategist.py::test_parallel_build_replays_deterministically_and_records_fanout` pins
# the cost guardrail (the `parallel_build_batch` span) on the same construction.

# Fields that differ between two runs of the same search: the run's identity, the archive scope
# digest keyed on it, and every wall-clock measurement. Everything else must be equal.
_VOLATILE = {"run_id", "run_uid", "finalize_scope", "eval_seconds", "total_eval_seconds",
             "eval_seconds_by_kind"}


def _parallel_run(run_dir):
    """A real 2-wide fan-out over the toy task: `parallel_build=2` with a role factory, so two
    builds really run on their own (researcher, developer) pairs — the same construction the
    strategist test uses, and the one whose fan-out span proves the width."""
    task = ToyTask.load(TOY_TASK)
    engine = make_engine(run_dir, task=task, n_seeds=3, max_nodes=8)
    engine.parallel_build = 2
    engine.role_factory = task.build_roles
    anyio.run(engine.run)
    return engine


def _strip_volatile(value):
    if isinstance(value, dict):
        return {k: _strip_volatile(v) for k, v in value.items() if k not in _VOLATILE}
    if isinstance(value, list):
        return [_strip_volatile(v) for v in value]
    return value


def _projection(state) -> dict:
    """The order-independent view of what the search found."""
    return {
        "nodes": {str(n.id): {
            "parent_ids": list(n.parent_ids), "operator": n.operator,
            "params": dict(n.idea.params) if n.idea is not None else None,
            "metric": n.metric, "status": n.status.value, "feasible": n.feasible,
        } for n in sorted(state.nodes.values(), key=lambda n: n.id)},
        "best_node_id": state.best_node_id,
        "finished": state.finished,
        "stop_reason": state.stop_reason,
    }


def test_a_two_wide_parallel_build_run_keeps_the_fan_out_invariants(tmp_path):
    engine = _parallel_run(tmp_path / "run")
    events = engine.store.read_all()
    building = [(i, e.data["node_id"]) for i, e in enumerate(events) if e.type == "node_building"]
    created = [(i, e.data["node_id"]) for i, e in enumerate(events) if e.type == "node_created"]
    # The run really fanned out: two ids were reserved before either build landed.
    first_landing = created[0][0]
    assert sum(1 for i, _ in building if i < first_landing) >= 2, "no concurrent build in the log"
    reserved = [nid for _, nid in building]
    assert reserved == sorted(reserved) and len(set(reserved)) == len(reserved), "serial reservation"
    landed = sorted(nid for _, nid in created)
    assert landed == list(range(len(landed))), "ids are dense and unique"
    reserved_at = {nid: i for i, nid in building}
    assert all(reserved_at[nid] < i for i, nid in created), "a node lands after its own reservation"
    terminals: dict[int, int] = {}
    for e in events:
        if e.type in ("node_evaluated", "node_failed"):
            terminals[e.data["node_id"]] = terminals.get(e.data["node_id"], 0) + 1
    assert terminals == {nid: 1 for nid in landed}, "exactly one terminal per node"
    state = fold(events)
    assert state.finished and set(state.nodes) == set(landed)
    assert all(n.status in (NodeStatus.evaluated, NodeStatus.failed) for n in state.nodes.values())
    # Deterministic replay: twice over one read, and over a second open of the same file.
    once = fold(events).model_dump(mode="json")
    assert once == fold(events).model_dump(mode="json")
    assert once == fold(EventStore(tmp_path / "run" / "events.jsonl").read_all()).model_dump(mode="json")
    for i in range(1, len(events) + 1):
        fold(events[:i])


def test_two_two_wide_runs_fold_to_one_state_though_their_logs_may_differ_in_order(tmp_path):
    a = _parallel_run(tmp_path / "a")
    b = _parallel_run(tmp_path / "b")
    folded_a = fold(a.store.read_all()).model_dump(mode="json")
    folded_b = fold(b.store.read_all()).model_dump(mode="json")
    assert _strip_volatile(folded_a) == _strip_volatile(folded_b)
    # The masked fields really are the only ones that may differ: the run's own identity.
    assert folded_a["run_id"] != folded_b["run_id"] and folded_a["run_uid"] != folded_b["run_uid"]


def test_the_parallel_golden_projection_is_the_checked_in_one(tmp_path):
    engine = _parallel_run(tmp_path / "run")
    got = _projection(fold(engine.store.read_all()))
    want = orjson.loads((_DATA / "golden_parallel_projection.json").read_bytes())
    assert got == want, "a 2-wide toy run no longer finds what the golden projection records"
