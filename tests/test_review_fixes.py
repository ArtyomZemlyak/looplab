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
