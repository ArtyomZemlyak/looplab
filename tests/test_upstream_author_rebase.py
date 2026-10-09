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
        (tree / name).write_bytes(text.encode("utf8"))  # not `write_text`: CRLF on Windows
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


def test_a_merge_that_cannot_run_is_no_answer_and_is_asked_again(tmp_path):
    """The current base's archive gone (or Git unavailable): no merge, no call — and no VERDICT on
    the source either. It used to be recorded as this base's `rebase_conflict`, which closed the
    source on that base for good over what may be a passing fault; now it is `rebase_unavailable`,
    settled without a row and memoized only until its retry time."""
    from looplab.engine.upstream_author import (PAID_OUTCOMES, UNRECORDED_OUTCOMES, RebaseRetry,
                                                rebase_source)
    lane, store, generation, body = fixture(tmp_path)
    _advance_base(lane, store, tmp_path, train=TRAIN.replace(_TAIL, _MOVED_TAIL))
    events = store.read_all()
    pick = author_next(lane.rd, lane.task, fold(events), events, rebase=True)
    pick["rebase"]["onto"] = {**pick["rebase"]["onto"], "run_dir": str(tmp_path / "gone")}
    out = rebase_source(lane.rd, lane.task, pick)
    assert out["outcome"] == "rebase_unavailable" and out["outcome"] not in PAID_OUTCOMES
    assert out["outcome"] in UNRECORDED_OUTCOMES and out["code"]
    assert isinstance(pick["pending"], RebaseRetry)


def _broken_merge(monkeypatch, exc):
    """Make the three-way merge raise `exc` until the returned switch is flipped."""
    import looplab.engine.upstream_workspace as workspace
    real, state = workspace.rebase_overlay, {"broken": True, "calls": 0}

    def merge(*a, **k):
        state["calls"] += 1
        if state["broken"]:
            raise exc
        return real(*a, **k)
    monkeypatch.setattr(workspace, "rebase_overlay", merge)
    return state


def test_a_git_timeout_writes_no_row_and_the_source_is_drafted_once_git_answers(tmp_path, monkeypatch):
    """Live: a Git timeout during the rebase is NOT the source's durable answer on this base. No
    `lane_authored` row, no call, nothing counted; the pick is passed over until its retry time, then
    merged again — and drafted once Git answers."""
    import looplab.engine.upstream_author as author
    lane, store, generation, body = fixture(tmp_path)
    _advance_base(lane, store, tmp_path, train=TRAIN.replace(_TAIL, _MOVED_TAIL))
    merge = _broken_merge(monkeypatch, UpstreamRefusal("upstream_git_timeout_unavailable", "slow"))
    calls = []
    _model(monkeypatch, _draft(GENERAL.replace(_TAIL, _MOVED_TAIL)), calls=calls)
    engine = _live_engine(lane, store)
    _serve(engine, turns=4)
    assert calls == [] and not [e for e in store.read_all() if e.type == "lane_authored"]
    assert merge["calls"] == 1, "passed over until its retry time, not merged every turn"
    retry, = engine._upstream_serve.author_skipped.values()
    assert isinstance(retry, author.RebaseRetry)
    merge["broken"] = False
    retry.deadline = 0.0                                    # the retry time has come
    _serve(engine)
    authored, = [e.data for e in store.read_all() if e.type == "lane_authored"]
    assert authored["outcome"] == "drafted" and len(calls) == 2


def test_an_unforeseen_rebase_failure_is_recorded_unpaid(tmp_path, monkeypatch):
    """An exception nobody classified, raised where no paid call exists, used to reach the main task
    as the job's error and land as a PAID `failed` draft — counted against `AUTHOR_MAX_PER_RUN`. It
    is an unpaid `skipped` row now, once per base; and a spend stop is still never swallowed."""
    from looplab.core.errors import BudgetExceeded
    from looplab.engine.upstream_author import PAID_OUTCOMES, _attempted, rebase_source
    lane, store, generation, body = fixture(tmp_path)
    _advance_base(lane, store, tmp_path, train=TRAIN.replace(_TAIL, _MOVED_TAIL))
    _broken_merge(monkeypatch, RuntimeError("a bug"))
    calls = []
    _model(monkeypatch, _draft(GENERAL), calls=calls)
    _serve(_live_engine(lane, store), turns=4)
    events = store.read_all()
    row, = [e.data for e in events if e.type == "lane_authored"]
    assert row["outcome"] == "skipped" and row["code"] == "upstream_rebase_failed"
    assert row["outcome"] not in PAID_OUTCOMES and _attempted(events)[1] == 0 and calls == []
    assert author_next(lane.rd, lane.task, fold(events), events, rebase=True) is None, (
        "said once per base")
    _broken_merge(monkeypatch, BudgetExceeded("ceiling"))
    pick = {"node": fold(events).nodes[0], "track": "champion", "archive": None, "events": events,
            "rebase": {"from_digest": "0" * 64,
                       "onto": active_base(events, lane.task.seed_base)["selector"]}}
    with pytest.raises(BudgetExceeded):
        rebase_source(lane.rd, lane.task, pick)


def test_the_champion_track_is_rebased_after_the_repair_tracks_proposal(tmp_path):
    """The rebase guard is per TRACK, like the action ids: the repair track's proposal from a
    lifecycle answered the fix, not the champion's capability of the same lifecycle — keyed per
    lifecycle alone, it closed both. A proposal the author did not write names no track and still
    answers every one."""
    from looplab.engine.upstream_state import node_signature
    lane, store, generation, body = fixture(tmp_path)
    node = fold(store.read_all()).nodes[0]
    repair_id = author_action_id(node, "repair")
    store.append("lane_authored", {"action_id": repair_id, "track": "repair", "source_node_id": 0,
                                   "outcome": "drafted"})
    store.append("upstream_proposed", {"action_id": repair_id, "source_node_id": 0,
                                       "source_signature": node_signature(node)})
    _advance_base(lane, store, tmp_path, train=TRAIN.replace(_TAIL, _MOVED_TAIL))
    events = store.read_all()
    pick = author_next(lane.rd, lane.task, fold(events), events, rebase=True)
    assert pick is not None and pick["track"] == "champion"
    store.append("upstream_proposed", {"action_id": "by-the-operator", "source_node_id": 0,
                                       "source_signature": node_signature(node)})
    events = store.read_all()
    assert author_next(lane.rd, lane.task, fold(events), events, rebase=True) is None


def test_a_turn_reads_the_paid_and_proposed_ledgers_once(tmp_path, monkeypatch):
    """Two stale sources in one turn: the two whole-log scans are made once, not once per source."""
    import looplab.engine.upstream_author as author
    lane, store, generation, body = fixture(tmp_path)
    node = fold(store.read_all()).nodes[0]
    # The repair track paid on an EARLIER base (a rebased row naming it): it reaches the stale-base
    # branch this turn and is passed over there, so both sources ask the two ledgers.
    store.append("lane_authored", {"action_id": "auto-rebase-earlier", "track": "repair",
                                   "source_node_id": 0, "outcome": "declined",
                                   "source_action_id": author_action_id(node, "repair")})
    _advance_base(lane, store, tmp_path, train=TRAIN.replace(_TAIL, _MOVED_TAIL))
    events = store.read_all()
    monkeypatch.setattr(author, "_sources", lambda state, ev, rep=None: [
        ("repair", state.nodes[0]), ("champion", state.nodes[0])])
    scans = []
    for name in ("_paid_lifecycles", "_proposed_lifecycles"):
        real = getattr(author, name)
        monkeypatch.setattr(author, name, lambda ev, _r=real, _n=name: scans.append(_n) or _r(ev))
    pick = author_next(lane.rd, lane.task, fold(events), events, rebase=True)
    assert pick["track"] == "champion", "the repair track was paid for; the champion is rebased"
    assert sorted(scans) == ["_paid_lifecycles", "_proposed_lifecycles"]


def test_a_stale_base_is_refused_before_the_lane_merges(tmp_path, monkeypatch):
    """The CAS is a comparison: a rebased body naming a base the run moved past is refused
    `upstream_base_conflict` before the lane buys its own three-way merge."""
    lane, store, generation, body = fixture(tmp_path)
    _advance_base(lane, store, tmp_path, train=TRAIN.replace(_TAIL, _MOVED_TAIL))
    events = store.read_all()
    pick = author_next(lane.rd, lane.task, fold(events), events, rebase=True)
    from looplab.engine.upstream_author import rebase_source
    assert rebase_source(lane.rd, lane.task, pick) == {"outcome": "ready"}
    merge = _broken_merge(monkeypatch, AssertionError("merged before the CAS"))
    stale = {**body, "expected_base_revision": "f" * 64,
             "hunk_hashes": sorted({r["hunk_hash"] for r in pick["rows"]}),
             "rebase": {"from_digest": pick["rebase"]["from_digest"],
                        "files": pick["source_files"], "deleted": pick["source_deleted"]}}
    with pytest.raises(UpstreamRefusal) as refused:
        lane._propose_admit(events, stale)
    assert refused.value.code == "upstream_base_conflict" and merge["calls"] == 0


def test_the_old_side_hash_reads_deletions_as_a_set():
    """The lane admits a body's merged deletions in any order and hashes its own SORTED merge onto
    the proposal; the gate re-hashed the manifest's list as sent, so an honest body that listed its
    deletions in another order was refused `upstream_rebase_changed` at its check."""
    from types import SimpleNamespace

    from looplab.engine.upstream_gate import old_side_overlay
    from looplab.engine.upstream_state import digest
    files = {"train.py": "x = 1\n"}
    proposal = {"rebased_from": "a" * 64,
                "source_overlay_hash": digest({"files": files, "deleted": ["a.py", "b.py"]})}
    source = SimpleNamespace(files={}, deleted=[])
    manifest = {"rebase": {"from_digest": "a" * 64, "files": files, "deleted": ["b.py", "a.py"]}}
    assert old_side_overlay(source, proposal, manifest) == (files, ["a.py", "b.py"])
    forged = {"rebase": {**manifest["rebase"], "deleted": ["b.py"]}}
    with pytest.raises(UpstreamRefusal) as changed:
        old_side_overlay(source, proposal, forged)
    assert changed.value.code == "upstream_rebase_changed"


def test_an_oversized_rebase_is_refused_before_the_paid_draft(monkeypatch):
    """A rebased body carries its merged overlay twice by contract (`rebase.files` and the recipe),
    so a source whose recipe alone is over half the request bound could only be refused by the
    lane's normalizer AFTER the draft was paid for. The known part is bounded before any call."""
    from types import SimpleNamespace

    from looplab.engine.upstream_author import author_draft, body_floor_bytes
    from looplab.engine.upstream_state import RETAINED_REQUEST_MAX_BYTES
    big = "w = 0  # " + "x" * (RETAINED_REQUEST_MAX_BYTES // 2 + 1024) + "\n"
    node = SimpleNamespace(id=0, files={"data.py": big, "train.py": "t\n"}, deleted=[],
                           idea=SimpleNamespace(rationale=""))
    pick = {"node": node, "track": "champion", "rows": [{"path": "train.py", "hunk_hash": "0" * 64}],
            "rebase": {"from_digest": "0" * 64}, "source_files": dict(node.files),
            "source_deleted": [], "action_id": "auto-rebase-x", "revision": "0" * 64}
    assert body_floor_bytes(pick) > RETAINED_REQUEST_MAX_BYTES
    assert body_floor_bytes({**pick, "rebase": None}) < RETAINED_REQUEST_MAX_BYTES, (
        "the same recipe on a source measured on the current base fits: only the rebase doubles it")
    calls = []
    _model(monkeypatch, _draft(GENERAL), calls=calls)
    engine = SimpleNamespace(developer=SimpleNamespace(client=object()), task=None)
    out = author_draft(engine, pick, generation="0" * 64)
    assert out["outcome"] == "skipped" and out["code"] == "upstream_request_too_large"
    assert calls == [], "refused before the first paid call"


def test_a_rebased_canary_gate_says_it_did_not_compare_the_merge(tmp_path):
    """Under `canary` (and the repair waiver) nothing compares the MERGED source with the score the
    source measured — only `full` asks `source_reproduced`. The result's scope says so, rather than
    letting the rebase read as re-verified."""
    from tests.test_upstream_canary_gate import _ENV_LOOP, _LOOP, _canary_run
    from looplab.engine.upstream_author import rebase_source
    from looplab.engine.upstream_gate import REBASED_UNCOMPARED
    lane, store, generation, body = _canary_run(tmp_path)
    _advance_base(lane, store, tmp_path,
                  train=TRAIN.replace(_LOOP, _ENV_LOOP).replace(_TAIL, _MOVED_TAIL))
    events = store.read_all()
    pick = author_next(lane.rd, lane.task, fold(events), events, rebase=True)
    assert rebase_source(lane.rd, lane.task, pick) == {"outcome": "ready"}
    active = active_base(events, lane.task.seed_base)
    rebased = {**body, "expected_base_revision": active["revision"],
               "hunk_hashes": sorted({r["hunk_hash"] for r in pick["rows"]}),
               "files": {"train.py": GENERAL.replace(_LOOP, _ENV_LOOP).replace(_TAIL, _MOVED_TAIL),
                         "README.md": _DOC},
               "rebase": {"from_digest": pick["rebase"]["from_digest"],
                          "files": pick["source_files"], "deleted": pick["source_deleted"]}}
    made = lane.propose(rebased)
    checked = lane.check({"expected_generation": generation, "action_id": "rebased-canary-check",
                          "proposal_id": made["proposal_id"]})
    result = checked["result"]
    eq = result["checks"][-1]
    assert eq["profile"] == "canary" and "source_reproduced" not in eq
    assert result["scope"].endswith(REBASED_UNCOMPARED), result["scope"]


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
