"""Regression tests for the 2026-07-13 mega-review `-fix` batch. Each test pins a specific finding so a
future change can't silently reintroduce it. Finding ids (F1, F4, …) match the review report."""
import os
import tempfile

from looplab.events.eventstore import Event
from looplab.events.replay import fold
from looplab.events import types as T


# ---------------------------------------------------------------- F1: eval_timeout replay back-compat
def test_eval_timeout_non_positive_coerced_not_rejected():
    """F1: `eval_timeout` is LLM-proposed and its consumer treats <=0 / non-finite as 'unset'. A hard
    gt=0/allow_inf_nan constraint made `Idea(**d["idea"])` raise inside the fold and DROP the node when
    replaying an old log. The coercing validator must map such values to None (both live and replay)."""
    from looplab.core.models import Idea
    for bad in (0, 0.0, -5, float("inf"), float("nan"), "bad"):
        assert Idea(operator="x", rationale="r", eval_timeout=bad).eval_timeout is None, bad
    assert Idea(operator="x", rationale="r", eval_timeout=30).eval_timeout == 30.0
    assert Idea(operator="x", rationale="r", eval_timeout="45").eval_timeout == 45.0


def test_old_node_created_with_zero_eval_timeout_still_folds():
    """F1: a node_created carrying eval_timeout=0 must remain in the fold (invariant 5), not be dropped."""
    ev = Event(seq=1, type=T.EV_NODE_CREATED, ts=0.0, data={
        "node_id": 0, "operator": "seed", "generation": 0,
        "idea": {"operator": "seed", "rationale": "r", "params": {"x": 1}, "eval_timeout": 0}})
    st = fold([ev])
    assert 0 in st.nodes and st.nodes[0].idea.eval_timeout is None


# ------------------------------------------------------------------- F5: node_tombstoned fold totality
def test_node_tombstoned_scalar_node_ids_does_not_crash_fold():
    """F5: a forged node_tombstoned with a truthy SCALAR node_ids must not raise out of the (try/except-less)
    fold loop and brick every replay of the run."""
    for bad in (42, True, 3.14, "abc", {"a": 1}):
        st = fold([Event(seq=1, type=T.EV_NODE_TOMBSTONED, ts=0.0, data={"node_ids": bad})])
        assert st.nodes == {}   # nothing tombstoned, but crucially: no exception
    # a well-formed list still works
    created = Event(seq=1, type=T.EV_NODE_CREATED, ts=0.0, data={
        "node_id": 0, "operator": "seed", "generation": 0,
        "idea": {"operator": "seed", "rationale": "r", "params": {}}})
    tomb = Event(seq=2, type=T.EV_NODE_TOMBSTONED, ts=0.0, data={"node_ids": [0]})
    assert fold([created, tomb]).nodes[0].tombstoned is True


# ----------------------------------------------------------------------- F8: stage-name slug totality
def test_safe_stage_name_rejects_trailing_newline():
    """F8: `$` matches before a trailing newline; the filesystem-safe slug gate must use `\\Z`."""
    from looplab.runtime.command_eval import safe_stage_name
    assert safe_stage_name("train\n") is False
    assert safe_stage_name("train\nmalicious") is False
    assert safe_stage_name("train") is True
    assert safe_stage_name("a\x00b") is False


# ----------------------------------------------------------------- F15: mem-cap parse never crashes
def test_parse_mem_bytes_non_finite_disables_cap_not_crash():
    """F15: `int(float('inf'))` raises OverflowError (not ValueError); a bad cap must return None."""
    from looplab.runtime.sandbox import parse_mem_bytes
    for bad in ("inf", "1e400", "1e400g", "nan", "bad", ""):
        assert parse_mem_bytes(bad) is None, bad
    assert parse_mem_bytes("8g") == 8 * 1024 ** 3


# --------------------------------------------------------------- F20: AST answer-key reader coverage
def test_reward_hack_reader_attrs_cover_non_csv_readers():
    """F20: a variable-path answer-key read via read_json/read_excel/… otherwise slips the AST pass."""
    from looplab.trust.reward_hack import _READER_ATTRS
    for r in ("read_json", "read_excel", "read_pickle", "read_feather", "read_hdf"):
        assert r in _READER_ATTRS


# ------------------------------------------------------------- F4: strategist policy_params governance
def test_default_agent_control_grants_policy_params_to_strategist():
    """F4: the default matrix must grant `policy_params` (not just `policy`) or the Strategist's decided
    params are silently dropped in `_apply_strategy` and the recorded strategy diverges from the engine."""
    from looplab.core.config import DEFAULT_AGENT_CONTROL, default_agent_control
    assert "strategist" in DEFAULT_AGENT_CONTROL.get("policy_params", [])
    assert "strategist" in default_agent_control().get("policy_params", [])


# ------------------------------------------------------------------ F6: CORS preflight under token
def test_options_preflight_not_gated_by_ui_token(monkeypatch):
    """F6: the token middleware gated OPTIONS by path, 401-ing the CORS preflight before CORSMiddleware
    could answer it — breaking every cross-origin API call. OPTIONS must pass through."""
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "sekret")
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app
    client = TestClient(make_app(tempfile.mkdtemp()))
    r = client.options("/api/runs/demo/control",
                       headers={"Origin": "http://localhost:5173",
                                "Access-Control-Request-Method": "POST"})
    assert r.status_code != 401
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"
    # a real mutating request without the token is still gated
    assert client.post("/api/runs/demo/control", json={"etype": "pause", "data": {}}).status_code == 401


# ---------------------------------------------------------- F3: headerless SSE + share stay unauth-safe
def test_unauth_api_ok_allows_sse_and_share_not_state():
    """F3/F21: the redacted SSE stream and the intentionally-untokened share route must be servable
    without the UI token (EventSource can't send it); ordinary reads/controls must NOT be."""
    from looplab.serve.server import _unauth_api_ok
    assert _unauth_api_ok("/api/health")
    assert _unauth_api_ok("/api/runs/demo/events")
    assert _unauth_api_ok("/api/assistant/shared/abc123")
    assert not _unauth_api_ok("/api/runs/demo/state")
    assert not _unauth_api_ok("/api/runs/demo/control")
    assert not _unauth_api_ok("/api/runs/demo/nodes/0")


# ------------------------------------------------------ F2: legacy holdout must not wipe incumbents
def _holdout_scenario(*, stamped):
    def idea(op="seed"):
        return {"operator": op, "rationale": "r", "params": {"x": 1}}

    def ev(seq, t, d):
        return Event(seq=seq, type=t, data=d, ts=0.0)
    evs = [
        ev(1, T.EV_RUN_STARTED, {"run_id": "r", "task_id": "tk", "direction": "min", "holdout_select": True}),
        ev(2, T.EV_NODE_CREATED, {"node_id": 0, "operator": "seed", "idea": idea(), "generation": 0}),
        ev(3, T.EV_NODE_EVALUATED, {"node_id": 0, "metric": 1.0, "generation": 0}),
        ev(4, T.EV_NODE_CREATED, {"node_id": 1, "operator": "seed", "idea": idea(), "generation": 0}),
        ev(5, T.EV_NODE_EVALUATED, {"node_id": 1, "metric": 5.0, "generation": 0}),
    ]
    h0, h1 = {"node_id": 0, "metric": 1.1}, {"node_id": 1, "metric": 5.1}
    if stamped:                                   # a modern producer stamps search_epoch + generation
        h0.update({"search_epoch": 0, "generation": 0})
        h1.update({"search_epoch": 0, "generation": 0})
    evs += [ev(6, T.EV_HOLDOUT_EVALUATED, h0), ev(7, T.EV_HOLDOUT_EVALUATED, h1),
            ev(8, T.EV_NODE_CREATED, {"node_id": 2, "operator": "draft", "idea": idea("draft"), "generation": 0})]
    return fold(evs)


def test_legacy_holdout_disclosure_does_not_wipe_incumbents():
    """F2: replaying an old (unstamped) holdout_select log must NOT requeue/wipe surviving incumbents
    when a later candidate lands — that changed the selected best on replay (invariant 5b)."""
    st = _holdout_scenario(stamped=False)
    assert st.holdout_epoch_aware is False
    assert st.nodes[0].metric == 1.0 and st.nodes[1].metric == 5.0   # metrics preserved
    assert st.best_node_id == 0                                       # best unchanged


def test_modern_holdout_disclosure_still_requeues():
    """F2 no-regression: a modern (search_epoch-stamped) disclosure MUST still requeue incumbents onto
    the newly-hidden complement when a later candidate lands."""
    from looplab.core.models import NodeStatus
    st = _holdout_scenario(stamped=True)
    # (holdout_epoch_aware is back to False here — the rotation that event 8 triggers CONSUMES it.)
    assert st.nodes[0].metric is None and st.nodes[0].status is NodeStatus.pending
    assert st.nodes[0].attempt == 1


# ------------------------------------------------------------ F18: env_changed folds once (dedup)
def test_env_changed_is_folded_and_deduped():
    """F18: env_changed now sets a folded flag (like workspace_changed) so the emit is gated on
    `not state.env_changed` — no unbounded re-append across resumes. It must be folded, not diagnostic."""
    from looplab.events.types import DIAGNOSTIC_EVENTS
    import looplab.events.replay as R
    assert T.EV_ENV_CHANGED in R._HANDLERS and T.EV_ENV_CHANGED not in DIAGNOSTIC_EVENTS
    st = fold([Event(seq=1, type=T.EV_ENV_CHANGED, ts=0.0, data={"was": {}, "now": {}})])
    assert st.env_changed is True


# ------------------------------------------------------- F25: /state no longer masks identifiers
def test_public_state_value_keeps_identifiers():
    """F25: the entropy heuristic used to mask legitimate high-entropy identifiers on the public /state.
    A run-slug / content-hash must now pass through; a known key-pattern is still redacted."""
    from looplab.serve.appstate import _public_state_value
    assert _public_state_value("runs/exp_2026_ablation_study_v3") == "runs/exp_2026_ablation_study_v3"
    assert _public_state_value("a" * 40) == "a" * 40
    masked = _public_state_value("sk-ant-api03-" + "x" * 40)             # known key pattern still masked
    assert "***" in masked and "xxxx" not in masked


# ---------------------------------------------------- F22: stale lifecycle lock sweep is safe
def test_sweep_stale_lifecycle_locks_only_old_and_unheld(tmp_path):
    """F22: the startup GC removes only OLD, UNHELD lifecycle lock files — never a fresh one, a held
    one, or a non-lock file."""
    import os
    import time
    from looplab.serve.engine_proc import sweep_stale_lifecycle_locks
    old = tmp_path / ".looplab-lifecycle-aaa.lock"; old.write_text("")
    fresh = tmp_path / ".looplab-lifecycle-bbb.lock"; fresh.write_text("")
    keep = tmp_path / "events.jsonl"; keep.write_text("x")
    past = time.time() - 7200
    os.utime(old, (past, past))
    assert sweep_stale_lifecycle_locks(tmp_path) == 1
    assert not old.exists() and fresh.exists() and keep.exists()
