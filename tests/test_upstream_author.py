"""The automated upstream author on a LIVE engine (doc 73 §2.5, `engine/upstream_author.py`).

Real CPU SGD through the doc-72 fixture, with only the two model calls replaced: under `upstream_mode:
auto` an engine with nothing to check drafts a proposal from the champion (track 2) or from a node a
repair made run (track 1, first), its critic reviews it, and the draft continues as the lane's own
propose → check → advance — the base moves on the measured gate alone. A declined draft is recorded
and never paid for again; a spend ceiling the worker met stops the run.
"""
from __future__ import annotations

import pytest

from looplab.agents.maintainer import (AUTHOR_CRITIC_REVIEWER, MaintainerCritique, MaintainerDraft,
                                       Maintainer, author_messages)
from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings
from looplab.engine.upstream_author import (AUTHOR_MAX_PER_RUN, author_action_id, author_next,
                                            build_body, upstream_author_setting)
from looplab.engine.upstream_state import active_base
from looplab.events.replay import fold
from tests.test_upstream_lane import GENERAL, fixture
from tests.test_upstream_live_lane import _engine, _serve
from tests.test_upstream_repairs import repair_fixture

_DOC = "MOMENTUM: default 0.0; set MOMENTUM=0.2 in recipe.env.\n"


class _Client:
    pass


def _model(monkeypatch, draft, verdict="pass", calls=None):
    """Replace the two paid calls: the draft for `MaintainerDraft`, the verdict for the critic."""
    import looplab.core.parse as parse

    def fake(client, messages, model, parser="tool_call"):
        if calls is not None:
            calls.append((model.__name__, messages))
        if model is MaintainerDraft:
            return MaintainerDraft.model_validate(draft)
        return MaintainerCritique(verdict=verdict, reason="generalized behind a flag, old default kept")
    monkeypatch.setattr(parse, "parse_structured", fake)


def _live_engine(lane, store):
    store.append("resume", {})
    engine = _engine(lane, store)
    engine.developer = type("Dev", (), {"client": _Client()})()
    return engine


def _champion_draft():
    return {"files": {"train.py": GENERAL, "README.md": _DOC}, "summary": "Momentum through recipe.env",
            "flag": {"name": "MOMENTUM", "default": "0.0", "enabled": "0.2"},
            "documentation_path": "README.md"}


def test_the_switch_has_one_reader_is_on_and_resumes_off():
    assert Settings().upstream_author is True
    assert upstream_author_setting(Settings(upstream_author=False)) is False
    assert upstream_author_setting(object()) is False
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["upstream_author"] is False


def test_the_champion_capability_reaches_the_base_through_the_measured_gate(tmp_path, monkeypatch):
    lane, store, generation, body = fixture(tmp_path)
    calls = []
    _model(monkeypatch, _champion_draft(), calls=calls)
    engine = _live_engine(lane, store)
    _serve(engine)
    events = store.read_all()
    authored, = [e for e in events if e.type == "lane_authored"]
    node = fold(events).nodes[0]
    assert authored.data["action_id"] == author_action_id(node, "champion")
    assert authored.data["track"] == "champion" and authored.data["outcome"] == "drafted"
    proposed, = [e for e in events if e.type == "upstream_proposed"]
    assert proposed.data["action_id"] == authored.data["action_id"]
    assert proposed.data["critic"]["reviewer"] == AUTHOR_CRITIC_REVIEWER
    assert [name for name, _ in calls] == ["MaintainerDraft", "MaintainerCritique"], "two calls"
    assert calls[0][1][0]["content"] == Maintainer.instruction
    advanced, = [e for e in events if e.type == "base_advanced"]
    assert advanced.data["in_engine"] is True
    assert active_base(events, lane.task.seed_base)["selector"] == proposed.data["selector"]
    before = store.path.read_bytes()
    _serve(engine)
    assert store.path.read_bytes() == before, "one draft per source lifecycle"


def test_a_repair_fix_is_drafted_first_and_carries_its_trigger(tmp_path, monkeypatch):
    lane, store, generation, proposal = repair_fixture(tmp_path)
    _model(monkeypatch, {"files": proposal["files"], "summary": "Opt-in sentinel momentum",
                         "flag": proposal["flag"], "documentation_path": "README.md"})
    engine = _live_engine(lane, store)
    pick = author_next(lane.rd, lane.task, fold(store.read_all()), store.read_all())
    assert pick["track"] == "repair" and [r["origin"] for r in pick["rows"]] == ["repair"]
    _serve(engine)
    events = store.read_all()
    authored, = [e for e in events if e.type == "lane_authored"]
    assert authored.data["track"] == "repair" and authored.data["outcome"] == "drafted"
    proposed, = [e for e in events if e.type == "upstream_proposed"]
    assert proposed.data["repair_trigger_nodes"] == [1]
    assert any(e.type == "base_advanced" for e in events), [e.type for e in events][-12:]


def test_a_repair_without_a_declared_trigger_probe_is_not_paid_for(tmp_path):
    lane, store, generation, proposal = repair_fixture(tmp_path)
    lane.task.upstream["repair_probes"] = []
    events = store.read_all()
    skipped = {}
    state = fold(events)
    assert author_next(lane.rd, lane.task, state, events, skipped=skipped) is None, (
        "its only capability hunk needs a probe nobody declared: the lane would refuse the draft")
    pending = sorted(n.id for n in state.pending_nodes())
    assert pending == [1] and set(skipped) == {author_action_id(state.nodes[0], "repair"),
                                               author_action_id(state.nodes[0], "champion")}
    assert all(sig is not None for sig in skipped.values()), (
        "memoized against the pending triggers, not for good: they may settle")


def test_a_declined_draft_is_recorded_and_never_asked_again(tmp_path, monkeypatch):
    lane, store, generation, body = fixture(tmp_path)
    calls = []
    _model(monkeypatch, _champion_draft(), verdict="fail", calls=calls)
    engine = _live_engine(lane, store)
    _serve(engine)
    events = store.read_all()
    authored, = [e for e in events if e.type == "lane_authored"]
    assert authored.data["outcome"] == "declined" and authored.data["reason"]
    assert not [e for e in events if e.type.startswith("upstream_proposal")]
    _serve(engine)
    assert len(calls) == 2, "not paid for again"


def test_the_author_is_off_with_its_switch_and_under_propose(tmp_path, monkeypatch):
    for update in ({"upstream_author": False}, {"upstream_mode": "propose"}):
        root = tmp_path / next(iter(update))
        root.mkdir()
        lane, store, generation, body = fixture(root)
        snapshot = lane.rd / "config.snapshot.json"
        snapshot.write_text(Settings.model_validate_json(snapshot.read_bytes()).model_copy(
            update=update).model_dump_json(), encoding="utf8")
        calls = []
        _model(monkeypatch, _champion_draft(), calls=calls)
        _serve(_live_engine(lane, store))
        assert calls == [] and not [e for e in store.read_all() if e.type == "lane_authored"]


def test_a_spend_ceiling_met_while_drafting_stops_the_run(tmp_path, monkeypatch):
    import anyio

    import looplab.core.parse as parse
    from looplab.core.llm_budget import BudgetExceeded
    from looplab.engine.upstream_serve import serve_upstream_requests
    lane, store, generation, body = fixture(tmp_path)

    def broke(*a, **k):
        raise BudgetExceeded("llm_cost_limit reached")
    monkeypatch.setattr(parse, "parse_structured", broke)
    engine = _live_engine(lane, store)

    async def _drive():
        await serve_upstream_requests(engine, fold(store.read_all()))
        while not engine._upstream_serve.job.done:
            await anyio.sleep(0.02)
        await serve_upstream_requests(engine, fold(store.read_all()))
    with pytest.raises(BudgetExceeded):
        anyio.run(_drive)
    authored, = [e for e in store.read_all() if e.type == "lane_authored"]
    assert authored.data["outcome"] == "failed" and engine._upstream_serve.job is None


def test_the_recipe_never_carries_a_code_path_the_patch_owns(tmp_path):
    lane, store, generation, body = fixture(tmp_path)
    events = store.read_all()
    pick = author_next(lane.rd, lane.task, fold(events), events)
    draft = MaintainerDraft.model_validate({**_champion_draft(),
                                            "recipe_overrides": {"train.py": "x", "recipe.env": "y"}})
    out = build_body(pick, draft, MaintainerCritique(verdict="pass", reason="ok"), generation=generation)
    assert out["recipe_files"] == {"recipe.env": "MOMENTUM=0.2\n"}, "train.py is the base's now"
    assert out["hunk_hashes"] == sorted({r["hunk_hash"] for r in pick["rows"]})
    assert out["expected_base_revision"] == active_base(events, lane.task.seed_base)["revision"]


def test_the_run_stops_paying_after_its_cap(tmp_path):
    lane, store, generation, body = fixture(tmp_path)
    for i in range(AUTHOR_MAX_PER_RUN):
        store.append("lane_authored", {"action_id": f"auto-author-{i}", "track": "repair",
                                       "source_node_id": 9, "outcome": "declined"})
    events = store.read_all()
    assert author_next(lane.rd, lane.task, fold(events), events) is None


def test_the_draft_prompt_names_its_track():
    assert "FIX" in author_messages("repair", "ctx")[1]["content"]
    assert "CHAMPION" in author_messages("champion", "ctx")[1]["content"]


def test_the_state_payload_carries_the_live_lane_only_where_it_exists(tmp_path, monkeypatch):
    """`upstream_live` (doc 73 §2.5): the UI's one read of the mode, the queue and the author rows."""
    pytest.importorskip("fastapi")
    from looplab.serve.server import make_app
    lane, store, generation, body = fixture(tmp_path)
    _model(monkeypatch, _champion_draft(), verdict="fail")
    _serve(_live_engine(lane, store))
    srv = make_app(tmp_path).state.looplab
    state = srv.state_payload(lane.rd)["state"]
    live = state["upstream_live"]
    assert live["mode"] == "auto" and live["reason"] == "" and live["author"] is True
    assert [r["outcome"] for r in live["authored"]] == ["declined"] and live["authored_total"] == 1
    assert live["queue"] == {"pending": 0, "total": 0, "rows": []}
    plain = tmp_path / "plain"
    plain.mkdir()
    from looplab.events.eventstore import EventStore
    EventStore(plain / "events.jsonl").append("run_started", {"run_id": "p", "task_id": "t",
                                                             "goal": "g", "direction": "min"})
    assert "upstream_live" not in srv.state_payload(plain)["state"], "every other payload keeps its shape"


# ------------------------------------------------------------------ critic 2026-10-08 regressions
def test_the_author_runs_under_a_real_engines_span(tmp_path, monkeypatch):
    """BLOCKER: `_paid_progress` refuses a stage outside `PROGRESS_STAGES`, so on a REAL engine every
    draft failed before its first call. The SimpleNamespace stand-in hid it; this drives
    `_author_work` through `make_engine`'s own `_op_span`."""
    from factories import make_engine
    from looplab.engine.upstream_serve import _author_work
    lane, store, generation, body = fixture(tmp_path)
    _model(monkeypatch, _champion_draft())
    real = make_engine(tmp_path / "engine-run")
    events = store.read_all()
    pick = author_next(lane.rd, lane.task, fold(events), events)
    stand_in = type("E", (), {"developer": type("D", (), {"client": _Client()})(), "task": lane.task,
                              "run_dir": lane.rd, "_op_span": real._op_span,
                              "_redact": real._redact})()
    out = _author_work(stand_in, pick, generation)
    assert out["outcome"] == "drafted", out


def test_nothing_is_drafted_while_a_lane_claim_is_unresolved(tmp_path, monkeypatch):
    lane, store, generation, body = fixture(tmp_path)
    store.append("upstream_gate_started", {"action_id": "stuck", "proposal_id": "up_x",
                                           "request_hash": "h", "input_identity": "i"})
    calls = []
    _model(monkeypatch, _champion_draft(), calls=calls)
    _serve(_live_engine(lane, store), turns=3)
    assert calls == [] and not [e for e in store.read_all() if e.type == "lane_authored"]


def test_a_repaired_champion_is_asked_for_its_fix_and_for_its_capability():
    from types import SimpleNamespace

    from looplab.engine.upstream_author import _sources
    node = SimpleNamespace(id=4, attempt=0, tombstoned=False, status=SimpleNamespace(value="evaluated"))
    state = SimpleNamespace(nodes={4: node}, best_node_id=4)
    events = [SimpleNamespace(type="node_repaired", data={"node_id": 4, "generation": 0})]
    assert [(t, n.id) for t, n in _sources(state, events)] == [("repair", 4), ("champion", 4)]


def test_a_draft_retained_before_a_crash_is_proposed_without_paying_again(tmp_path, monkeypatch):
    from looplab.engine.upstream_author import retain_draft
    from looplab.engine.upstream_serve import upstream_live_view
    lane, store, generation, body = fixture(tmp_path)
    events = store.read_all()
    pick = author_next(lane.rd, lane.task, fold(events), events)
    draft = MaintainerDraft.model_validate(_champion_draft())
    drafted = build_body(pick, draft, MaintainerCritique(verdict="pass", reason="ok"), generation=generation)
    retain_draft(lane.rd, drafted)
    store.append("lane_authored", {"action_id": pick["action_id"], "track": "champion",
                                   "source_node_id": 0, "outcome": "drafted"})
    calls = []
    _model(monkeypatch, _champion_draft(), calls=calls)
    _serve(_live_engine(lane, store))
    assert calls == [], "the paid draft was on disk"
    events = store.read_all()
    assert [e.data["action_id"] for e in events if e.type == "upstream_proposed"] == [pick["action_id"]]
    assert any(e.type == "base_advanced" for e in events)
    assert upstream_live_view(lane.rd, events)["configured"] is False, "the engine said what it armed"


def test_a_draft_the_lane_refuses_is_recorded_refused(tmp_path, monkeypatch):
    """A drafted row alone would read "proposed" in the UI; the lane's ADMIT refusal (here a base the
    run no longer holds) is recorded on its own row, and a re-entry does not propose it again."""
    import looplab.engine.upstream_author as author
    lane, store, generation, body = fixture(tmp_path)
    real = author.build_body
    monkeypatch.setattr(author, "build_body", lambda *a, **k: {**real(*a, **k),
                                                              "expected_base_revision": "0" * 64})
    calls = []
    _model(monkeypatch, _champion_draft(), calls=calls)
    engine = _live_engine(lane, store)
    _serve(engine, turns=6)
    rows = [e.data for e in store.read_all() if e.type == "lane_authored"]
    assert [r["outcome"] for r in rows] == ["drafted", "refused"], rows
    assert rows[-1]["code"] == "upstream_base_conflict"
    _serve(engine, turns=3)
    assert len(calls) == 2 and len([e for e in store.read_all() if e.type == "lane_authored"]) == 2


def test_a_draft_that_cannot_absorb_the_capability_does_not_buy_the_critic(tmp_path, monkeypatch):
    lane, store, generation, body = fixture(tmp_path)
    calls = []
    _model(monkeypatch, {"files": {"README.md": _DOC}, "summary": "docs only",
                         "flag": {"name": "MOMENTUM", "default": "0.0", "enabled": "0.2"},
                         "documentation_path": "README.md"}, calls=calls)
    _serve(_live_engine(lane, store))
    row, = [e.data for e in store.read_all() if e.type == "lane_authored"]
    assert row["outcome"] == "rejected" and row["code"] == "upstream_capability_not_absorbed"
    assert [name for name, _ in calls] == ["MaintainerDraft"], "one call, not two"
    assert not [e for e in store.read_all() if e.type.startswith("upstream_proposal")]


def test_the_prompts_fence_candidate_text_while_the_envelope_is_on():
    from looplab.agents.maintainer import critic_messages
    draft = MaintainerDraft.model_validate(_champion_draft())
    plain = critic_messages("champion", "ctx", draft)
    fenced = critic_messages("champion", "ctx", draft, evidence_label="UNTRUSTED_RUN_EVIDENCE")
    assert "UNTRUSTED" not in plain[0]["content"] + plain[1]["content"]
    assert fenced[1]["content"].count("UNTRUSTED_RUN_EVIDENCE") >= 4, "the context AND the draft"
    assert "quoted evidence" in fenced[0]["content"]


def test_the_kill_switch_from_the_cli_and_the_inspect_lines(tmp_path, monkeypatch):
    """doc 73 §4.3: the switch had no `looplab` command and `inspect` printed neither it nor what a cap
    held back. Both read and write the same rows the UI does."""
    from typer.testing import CliRunner

    from looplab.cli import app
    lane, store, generation, body = fixture(tmp_path)
    runner = CliRunner()
    off = runner.invoke(app, ["upstream-auto", str(lane.rd), "off", "--reason", "  reviewing "])
    assert off.exit_code == 0, off.output
    row, = [e.data for e in store.read_all() if e.type == "upstream_auto_set"]
    assert row == {"enabled": False, "reason": "reviewing"}, "stripped, as the control intake does"
    assert fold(store.read_all()).upstream_auto_paused is True
    calls = []
    _model(monkeypatch, _champion_draft(), calls=calls)
    _serve(_live_engine(lane, store), turns=3)
    assert calls == [], "the switch holds the author"
    shown = runner.invoke(app, ["inspect", str(lane.rd)])
    assert "upstream automation: mode auto" in shown.output and "switch: OFF" in shown.output, shown.output
    assert "no engine serving it now" in shown.output, "the armed mode of an engine that is gone"
    assert runner.invoke(app, ["upstream-auto", str(lane.rd), "on"]).exit_code == 0
    assert fold(store.read_all()).upstream_auto_paused is False


def test_the_cli_reason_is_normalized_like_the_control_intake_and_refusals_go_to_stderr(tmp_path, monkeypatch):
    """Critic 2026-10-08: the CLI kept a blank or padded reason the `/commands` intake strips, and a
    Replay/deletion fence on the log escaped as a traceback."""
    from typer.testing import CliRunner

    from looplab.cli import app
    from looplab.core.run_reset import RunResetFenceError
    from looplab.events.eventstore import EventStore
    lane, store, generation, body = fixture(tmp_path)
    runner = CliRunner()
    assert runner.invoke(app, ["upstream-auto", str(lane.rd), "off", "--reason", "   "]).exit_code == 0
    assert runner.invoke(app, ["upstream-auto", str(lane.rd), "on", "--reason", "  back  "]).exit_code == 0
    rows = [e.data for e in store.read_all() if e.type == "upstream_auto_set"]
    assert rows == [{"enabled": False}, {"enabled": True, "reason": "back"}]

    def fenced(self, *a, **k):
        raise RunResetFenceError("Replay op_123 is unresolved")
    monkeypatch.setattr(EventStore, "append", fenced)
    refused = runner.invoke(app, ["upstream-auto", str(lane.rd), "off"])
    assert refused.exit_code == 2, refused.output
    assert "Replay op_123 is unresolved" in refused.stderr and "Traceback" not in refused.output


# ------------------------------------------------------------------------------- critic 2026-10-08
def test_the_precheck_asks_the_lanes_own_rules_edit_surface_included(tmp_path, monkeypatch):
    """A draft that writes outside the edit surface (the declared scorer) is refused by the lane's
    `checked_overlay` — the precheck used to restate a subset of the rules and missed it, so the
    critic was paid for a draft the lane refused."""
    lane, store, generation, body = fixture(tmp_path)
    calls = []
    draft = _champion_draft()
    draft["files"] = {**draft["files"], "score.py": "print('rigged')\n"}
    _model(monkeypatch, draft, calls=calls)
    _serve(_live_engine(lane, store))
    row, = [e.data for e in store.read_all() if e.type == "lane_authored"]
    assert row["outcome"] == "rejected" and row["code"] == "upstream_patch_forbidden", row
    assert [name for name, _ in calls] == ["MaintainerDraft"], "the critic is not bought"


def test_the_precheck_refuses_a_recipe_colliding_with_shared_code_by_case(tmp_path):
    from looplab.core.errors import UpstreamRefusal
    from looplab.engine.upstream import validate_shared_patch
    from looplab.engine.upstream_author import precheck_draft
    lane, store, generation, body = fixture(tmp_path)
    pick = author_next(lane.rd, lane.task, fold(store.read_all()), store.read_all())
    draft = MaintainerDraft.model_validate(_champion_draft())
    passed = build_body(pick, draft, MaintainerCritique(verdict="pass", reason="r"),
                        generation=generation)
    assert precheck_draft(pick, passed, lane.task) is None
    collide = {**passed, "recipe_files": {**passed["recipe_files"], "TRAIN.PY": "x = 1\n"}}
    with pytest.raises(UpstreamRefusal) as lane_says:
        validate_shared_patch(lane.task.upstream, collide, {"train.py"})
    assert lane_says.value.code == "upstream_capability_not_absorbed", "NTFS writes one onto the other"
    assert precheck_draft(pick, collide, lane.task) is not None


def test_an_unreadable_base_file_skips_the_source_never_reads_as_empty(tmp_path):
    from looplab.engine.upstream_author import _read
    archive = tmp_path / "archive"
    (archive / "pkg").mkdir(parents=True)
    (archive / "train.py").write_text("x = 1\n", encoding="utf8")
    (archive / "link.py").symlink_to(archive / "train.py")
    assert _read(archive, "train.py") == "x = 1\n"
    assert _read(archive, "added.py") == "", "a file the source ADDED has no base text"
    assert _read(archive, "pkg") is None, "a directory is not an empty file"
    assert _read(archive, "link.py") is None, "nor is a link the bounded reader refuses"


def test_the_maintainers_summary_and_flag_values_go_through_the_redaction_funnel(tmp_path, monkeypatch):
    lane, store, generation, body = fixture(tmp_path)
    draft = _champion_draft()
    draft["summary"] = "Momentum through recipe.env; token=hunter2"
    draft["flag"] = {"name": "MOMENTUM", "default": "0.0", "enabled": "0.2-hunter2"}
    _model(monkeypatch, draft)
    engine = _live_engine(lane, store)
    engine._redact = lambda text: str(text).replace("hunter2", "[REDACTED]")
    _serve(engine)
    proposed, = [e.data for e in store.read_all() if e.type == "upstream_proposed"]
    assert "hunter2" not in proposed["summary"] and "[REDACTED]" in proposed["summary"]
    assert proposed["flag"]["enabled"] == "0.2-[REDACTED]" and proposed["flag"]["name"] == "MOMENTUM"


def test_a_source_with_nothing_to_nominate_is_not_reread_on_every_new_node(tmp_path, monkeypatch):
    """The memo's key for an UNREPAIRED source is the base revision alone: a pending node appearing
    cannot give it a capability hunk, so it is not re-derived (a whole-log fold and per-hunk repair
    replays) on every node."""
    import looplab.engine.upstream_state as upstream_state
    lane, store, generation, body = fixture(tmp_path, source_files={"recipe.env": "MOMENTUM=0.2\n"})
    store.append("resume", {})
    real, calls = upstream_state.upstream_candidates, []

    def counted(*a, **k):
        calls.append(k.get("source_node_id"))
        return real(*a, **k)
    monkeypatch.setattr(upstream_state, "upstream_candidates", counted)
    memo = {}
    assert author_next(lane.rd, lane.task, fold(store.read_all()), store.read_all(), skipped=memo) is None
    assert calls == [0], "a recipe-only champion nominates nothing"
    store.append("node_created", {"node_id": 1, "operator": "improve", "parent_ids": [0],
                                  "idea": {"operator": "improve"}, "files": {"recipe.env": "MOMENTUM=0.3\n"}})
    assert author_next(lane.rd, lane.task, fold(store.read_all()), store.read_all(), skipped=memo) is None
    assert calls == [0], "a new pending node changes nothing for an unrepaired source"
