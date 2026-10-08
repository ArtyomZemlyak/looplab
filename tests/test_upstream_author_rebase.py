"""The automated author REBASES a source the base moved past (doc 73 §4.3, `upstream_author_rebase`).

Real CPU SGD through the doc-72 fixture, with only the two model calls replaced. The champion was
measured on the launch base; the run's base then advanced (here a hand-written advance onto a base
whose only change sits away from the champion's edit). Before, the author skipped such a source for
good — the lane admits a source measured on the CURRENT base only. Now it three-way merges the
source's overlay onto the current base: a clean merge is drafted and the gate re-measures the REBASED
source as its old side; a conflict is recorded and the source is skipped on that base, with no call.
"""
from __future__ import annotations

import pytest

from benchmarks._upstream_sgd import GENERAL, TRAIN
from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings
from looplab.core.errors import UpstreamRefusal
from looplab.engine.seed_archive import capture_seed_archive
from looplab.engine.upstream_author import (author_action_id, author_next, author_rebase_action_id,
                                            author_rebase_setting)
from looplab.engine.upstream_state import active_base
from looplab.events.replay import fold
from tests.test_upstream_author import _DOC, _live_engine, _model
from tests.test_upstream_lane import fixture
from tests.test_upstream_live_lane import _serve

_TAIL = 'print("trained", w)\n'
_MOVED_TAIL = 'print("trained (base v2)", w)\n'


def _advance_base(lane, store, tmp_path, *, train=None, readme="Runner, base v2\n"):
    """Advance the run's base by hand: a captured archive with `train`/`readme`, seeded and named by a
    `base_advanced` row — what an earlier live advance leaves behind."""
    tree = tmp_path / "base_v2"
    tree.mkdir()
    from benchmarks._upstream_sgd import SCORE
    for name, text in {"train.py": train or TRAIN, "score.py": SCORE, "recipe.env": "MOMENTUM=0.0\n",
                       "README.md": readme}.items():
        (tree / name).write_text(text, encoding="utf8")
    base = capture_seed_archive(tree, lane.rd / "base_snapshots")
    seeded = store.append("workspace_seeded", {"node_id": None, "materialized": [], "base_revision": base})
    events = store.read_all()
    previous = active_base(events, lane.task.seed_base)
    selector = {"run_dir": str(lane.rd), "event_seq": seeded.seq, "digest": base["digest"]}
    store.append("base_advanced", {
        "action_id": "manual-advance", "request_hash": "0" * 64, "proposal_id": "up_" + "0" * 24,
        "selector": selector, "from_revision": previous["revision"], "source_node_id": 9,
        "hunk_hashes": [], "flag": {"name": "X", "default": "0", "enabled": "1"},
        "summary": "an earlier capability", "evidence_token": "0" * 64, "gate_seq": seeded.seq})
    return selector


def _draft(train):
    return {"files": {"train.py": train, "README.md": _DOC}, "summary": "Momentum through recipe.env",
            "flag": {"name": "MOMENTUM", "default": "0.0", "enabled": "0.2"},
            "documentation_path": "README.md"}


def test_the_switch_has_one_reader_is_on_and_resumes_off():
    assert Settings().upstream_author_rebase is True
    assert author_rebase_setting(Settings(upstream_author_rebase=False)) is False
    assert author_rebase_setting(object()) is False
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["upstream_author_rebase"] is False


def test_a_source_the_base_moved_past_is_rebased_and_reaches_the_base_on_its_gate(tmp_path, monkeypatch):
    lane, store, generation, body = fixture(tmp_path)
    moved = TRAIN.replace(_TAIL, _MOVED_TAIL)
    selector = _advance_base(lane, store, tmp_path, train=moved)
    events = store.read_all()
    node = fold(events).nodes[0]
    active = active_base(events, lane.task.seed_base)
    assert author_next(lane.rd, lane.task, fold(events), events) is None, "rebasing off: skipped, as before"
    pick = author_next(lane.rd, lane.task, fold(events), events, rebase=True)
    assert pick["action_id"] == author_rebase_action_id(node, "champion", active["revision"])
    assert pick["rows"] is None and pick["source_action_id"] == author_action_id(node, "champion")

    calls = []
    _model(monkeypatch, _draft(GENERAL.replace(_TAIL, _MOVED_TAIL)), calls=calls)
    _serve(_live_engine(lane, store))
    events = store.read_all()
    authored, = [e.data for e in events if e.type == "lane_authored"]
    assert authored["outcome"] == "drafted" and authored["action_id"] == pick["action_id"]
    assert authored["source_action_id"] == pick["source_action_id"] and authored["rebased_from"]
    context = calls[0][1][1]["content"]
    assert "REBASED" in context and "base v2" in context, "the draft reads the CURRENT base"
    proposed, = [e.data for e in events if e.type == "upstream_proposed"]
    assert proposed["old_selector"] == selector and proposed["rebased_from"] == authored["rebased_from"]
    gate, = [e.data for e in events if e.type == "upstream_gate_finished"]
    assert gate["result"]["passed"] is True, gate["result"]["checks"]
    assert "rebased" in gate["result"]["scope"]
    eq, = [c for c in gate["result"]["checks"] if c["kind"] == "equivalence"]
    assert eq["profile"] == "full" and eq["source_reproduced"] is True, (
        "the REBASED source reproduced the score the source measured on its older base")
    advances = [e.data for e in events if e.type == "base_advanced"]
    assert advances[-1]["in_engine"] is True and advances[-1]["proposal_id"] == proposed["proposal_id"]
    before = store.path.read_bytes()
    _serve(_live_engine(lane, store), turns=3)
    assert len(calls) == 2 and store.path.read_bytes().count(b'"lane_authored"') == before.count(
        b'"lane_authored"'), "one draft per source lifecycle, across a fresh engine"


def test_a_conflicting_rebase_is_recorded_skipped_and_never_paid_for(tmp_path, monkeypatch):
    lane, store, generation, body = fixture(tmp_path)
    # The new base rewrote the very line the champion changed: no clean three-way merge.
    _advance_base(lane, store, tmp_path, train=TRAIN.replace("MOMENTUM = 0.0", "MOMENTUM = 0.0  # v2"))
    calls = []
    _model(monkeypatch, _draft(GENERAL), calls=calls)
    _serve(_live_engine(lane, store))
    rows = [e.data for e in store.read_all() if e.type == "lane_authored"]
    row, = rows
    assert row["outcome"] == "rebase_conflict" and row["code"] == "upstream_rebase_conflict"
    assert row["conflicts"] == ["train.py"] and "did not apply cleanly in train.py" in row["reason"]
    assert calls == [], "a conflict costs no call"
    assert not [e for e in store.read_all() if e.type.startswith("upstream_proposal")]
    _serve(_live_engine(lane, store), turns=3)
    assert len([e for e in store.read_all() if e.type == "lane_authored"]) == 1, "said once per base"


def test_a_resumed_run_without_the_field_keeps_skipping(tmp_path, monkeypatch):
    lane, store, generation, body = fixture(tmp_path)
    snapshot = lane.rd / "config.snapshot.json"
    snapshot.write_text(Settings.model_validate_json(snapshot.read_bytes()).model_copy(
        update={"upstream_author_rebase": False}).model_dump_json(), encoding="utf8")
    _advance_base(lane, store, tmp_path, train=TRAIN.replace(_TAIL, _MOVED_TAIL))
    calls = []
    _model(monkeypatch, _draft(GENERAL), calls=calls)
    _serve(_live_engine(lane, store), turns=4)
    assert calls == [] and not [e for e in store.read_all() if e.type == "lane_authored"]


def test_a_lifecycle_already_paid_for_is_not_rebased(tmp_path):
    lane, store, generation, body = fixture(tmp_path)
    events = store.read_all()
    node = fold(events).nodes[0]
    store.append("lane_authored", {"action_id": author_action_id(node, "champion"), "track": "champion",
                                   "source_node_id": 0, "outcome": "declined"})
    _advance_base(lane, store, tmp_path, train=TRAIN.replace(_TAIL, _MOVED_TAIL))
    events = store.read_all()
    assert author_next(lane.rd, lane.task, fold(events), events, rebase=True) is None


def test_the_lane_recomputes_the_merge_it_admits(tmp_path):
    """A proposer's `rebase` is not taken on trust: a merged overlay that is not the lane's own
    three-way merge is refused, and so is a `rebase` on a source measured on the current base."""
    lane, store, generation, body = fixture(tmp_path)
    store.append("resume", {})
    with pytest.raises(UpstreamRefusal) as current:
        lane._propose_admit(store.read_all(), {**body, "rebase": {
            "from_digest": "0" * 64, "files": {}, "deleted": []}})
    assert current.value.code == "upstream_rebase_invalid"
    _advance_base(lane, store, tmp_path, train=TRAIN.replace(_TAIL, _MOVED_TAIL))
    events = store.read_all()
    pick = author_next(lane.rd, lane.task, fold(events), events, rebase=True)
    from looplab.engine.upstream_author import rebase_source
    assert rebase_source(lane.rd, lane.task, pick) == {"outcome": "ready"}
    assert pick["source_files"]["train.py"].endswith(_MOVED_TAIL), "the merge kept the base's change"
    active = active_base(events, lane.task.seed_base)
    hunks = sorted({r["hunk_hash"] for r in pick["rows"]})
    honest = {**body, "expected_base_revision": active["revision"], "hunk_hashes": hunks,
              "files": {"train.py": GENERAL.replace(_TAIL, _MOVED_TAIL), "README.md": _DOC},
              "rebase": {"from_digest": pick["rebase"]["from_digest"],
                         "files": pick["source_files"], "deleted": pick["source_deleted"]}}
    ctx = lane._propose_admit(events, honest)
    assert ctx["rebase"]["from_digest"] == pick["rebase"]["from_digest"]
    forged = {**honest, "rebase": {**honest["rebase"],
                                   "files": {**pick["source_files"], "recipe.env": "MOMENTUM=0.9\n"}}}
    with pytest.raises(UpstreamRefusal) as changed:
        lane._propose_admit(events, {**forged, "action_id": "forged"})
    assert changed.value.code == "upstream_rebase_changed"
    without = {k: v for k, v in honest.items() if k != "rebase"}
    with pytest.raises(UpstreamRefusal) as conflict:
        lane._propose_admit(events, {**without, "action_id": "plain"})
    assert conflict.value.code == "upstream_base_conflict", "without a rebase the old rule stands"


def test_a_merge_that_cannot_run_is_an_unpaid_answer_not_a_paid_failure(tmp_path):
    """The current base's archive gone (or Git unavailable): no merge, no call — recorded as this
    base's `rebase_conflict`, never as a `failed` draft that counts against the run's paid cap."""
    from looplab.engine.upstream_author import PAID_OUTCOMES, rebase_source
    lane, store, generation, body = fixture(tmp_path)
    _advance_base(lane, store, tmp_path, train=TRAIN.replace(_TAIL, _MOVED_TAIL))
    events = store.read_all()
    pick = author_next(lane.rd, lane.task, fold(events), events, rebase=True)
    pick["rebase"]["onto"] = {**pick["rebase"]["onto"], "run_dir": str(tmp_path / "gone")}
    out = rebase_source(lane.rd, lane.task, pick)
    assert out["outcome"] == "rebase_conflict" and out["outcome"] not in PAID_OUTCOMES
    assert out["code"] and out["conflicts"] == []


def test_a_rebased_source_that_nominates_nothing_writes_nothing_and_is_not_asked_again(tmp_path, monkeypatch):
    """A source whose only edits are a recipe (no capability hunk) merges cleanly and nominates
    nothing: memoized against the base and the pending triggers like the native path, no row, no
    call, and the next turn does not merge it again."""
    import looplab.engine.upstream_author as author
    lane, store, generation, body = fixture(tmp_path, source_files={"recipe.env": "MOMENTUM=0.2\n"})
    _advance_base(lane, store, tmp_path, train=TRAIN.replace(_TAIL, _MOVED_TAIL))
    merges = []
    real = author.rebase_source
    monkeypatch.setattr(author, "rebase_source", lambda *a, **k: merges.append(1) or real(*a, **k))
    calls = []
    _model(monkeypatch, _draft(GENERAL), calls=calls)
    engine = _live_engine(lane, store)
    _serve(engine, turns=6)
    assert merges == [1] and calls == []
    assert not [e for e in store.read_all() if e.type == "lane_authored"]
    assert list(engine._upstream_serve.author_skipped.values()) and all(
        v is not None for v in engine._upstream_serve.author_skipped.values()), "memoized, not for good"
