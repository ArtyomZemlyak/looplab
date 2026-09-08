"""WHO is asking, and the one portfolio-visibility decision (doc 52 row 29).

`serve/assistant.py` used to mount the cross-run providers on one process-wide boolean computed from
Settings, so every caller that reached the owner Assistant received the same unbound portfolio. The
decision is now `serve/principal.py::portfolio_access(principal, settings)`, a property of the party
the request authenticated as. The truth table is driven directly; the stamping is driven through the
real server's middlewares with a recorder standing where the turn runs.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from looplab.serve import principal as P
from looplab.serve.assistant import build_tools
from looplab.serve.server import make_app


def _settings(tmp_path, flag=True):
    return SimpleNamespace(memory_dir=str(tmp_path / "mem"), cross_run_read_tools=flag)


@pytest.mark.parametrize("party,allowed", [
    (P.OWNER_PRINCIPAL, True), (P.LOCAL_PRINCIPAL, True),
    (P.Principal(P.REVIEW, "link-1"), False), (P.ANONYMOUS_PRINCIPAL, False), (None, False),
    ("owner", True), ("review", False), ("bogus", False),
])
def test_portfolio_access_is_a_property_of_the_principal(tmp_path, party, allowed):
    ok, why = P.portfolio_access(party, _settings(tmp_path))
    assert ok is allowed, why
    if not allowed and party is not None and not isinstance(party, str):
        assert party.kind in why


def test_the_storage_and_the_switch_are_still_necessary(tmp_path):
    assert P.portfolio_access(P.OWNER_PRINCIPAL, _settings(tmp_path, flag=False)) == (
        False, "cross_run_read_tools is off")
    assert P.portfolio_access(P.OWNER_PRINCIPAL, SimpleNamespace(cross_run_read_tools=True))[0] is False
    assert P.portfolio_access(P.OWNER_PRINCIPAL, None)[0] is False


def test_a_principal_is_a_closed_vocabulary_and_only_a_review_carries_a_link():
    with pytest.raises(ValueError):
        P.Principal("admin")
    with pytest.raises(ValueError):
        P.Principal(P.OWNER, "link")
    assert P.review_principal({"id": "abc"}) == P.Principal(P.REVIEW, "abc")
    assert P.coerce("local") == P.LOCAL_PRINCIPAL
    assert P.coerce("review") == P.ANONYMOUS_PRINCIPAL, "a bare kind cannot mint a review identity"
    assert P.coerce(None) == P.ANONYMOUS_PRINCIPAL


def test_the_toolset_carries_the_portfolio_only_for_the_owner_plane(tmp_path):
    settings = _settings(tmp_path)
    portfolio = {"CrossRunTools", "ConceptGovernanceTools"}
    for party in (P.OWNER_PRINCIPAL, P.LOCAL_PRINCIPAL):
        kinds = {type(p).__name__ for p in build_tools(tmp_path, mode="auto", settings=settings,
                                                        principal=party).providers}
        assert portfolio <= kinds, party
    for party in (P.Principal(P.REVIEW, "link-1"), P.ANONYMOUS_PRINCIPAL, None):
        kinds = {type(p).__name__ for p in build_tools(tmp_path, mode="auto", settings=settings,
                                                        principal=party).providers}
        assert kinds.isdisjoint(portfolio), party
    # The subagent runs as the same party, never wider.
    sub = next(p for p in build_tools(tmp_path, mode="plan", settings=settings, client=object(),
                                      subagents=True, principal=P.OWNER_PRINCIPAL).providers
               if type(p).__name__ == "SubagentTools")
    assert sub.principal == P.OWNER_PRINCIPAL


def _recording_turn(seen):
    def fake_turn(client, root, history, instruction, mode, **kw):
        seen.append(kw.get("principal"))
        return {"ok": True, "reply": "r", "steps": [], "applied": [], "mode": mode}
    return fake_turn


def test_the_owner_token_holder_runs_a_turn_as_the_owner_principal(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "tok-1")
    seen: list = []
    monkeypatch.setattr("looplab.serve.routers.assistant._assistant_run_turn", _recording_turn(seen))
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s, **_kw: object())
    client = TestClient(make_app(tmp_path))
    headers = {"X-LoopLab-Token": "tok-1"}
    assert client.post("/api/assistant/sessions", json={"title": "t"}).status_code == 401
    sid = client.post("/api/assistant/sessions", json={"title": "t"}, headers=headers).json()["id"]
    r = client.post(f"/api/assistant/sessions/{sid}/message",
                    json={"instruction": "hello", "mode": "plan"}, headers=headers)
    assert r.status_code == 200, r.text
    assert seen == [P.OWNER_PRINCIPAL]


def test_the_local_plane_without_a_token_runs_as_the_local_principal(tmp_path, monkeypatch):
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    seen: list = []
    monkeypatch.setattr("looplab.serve.routers.assistant._assistant_run_turn", _recording_turn(seen))
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s, **_kw: object())
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={"title": "t"}).json()["id"]
    assert client.post(f"/api/assistant/sessions/{sid}/message",
                       json={"instruction": "hello", "mode": "plan"}).status_code == 200
    assert seen == [P.LOCAL_PRINCIPAL]


def test_a_standing_watch_pins_the_party_that_armed_it(tmp_path):
    from looplab.serve.assistant_watch import WatchStore
    store = WatchStore(tmp_path / "watches")
    record = store.arm(session="s", instruction="tell me when it finishes",
                       trigger={"kind": "schedule", "every_s": 600}, principal="owner")
    assert record["principal"] == "owner"
    legacy = store.arm(session="s", instruction="again",
                       trigger={"kind": "schedule", "every_s": 600})
    assert legacy["principal"] == "anonymous", "a record that pins nothing runs with no portfolio"
    assert P.coerce(record["principal"]) == P.OWNER_PRINCIPAL


def test_every_portfolio_mount_in_serve_asks_the_PARTY():
    """`portfolio_access` is "the ONE decision that mounts the portfolio providers", and whether
    the cross-run stores may be read "is a property of the PARTY and never of the process".

    `assistant.py` was converted to it on 2026-09-06; `routers/genesis.py` was not, and went on
    deciding on the two Settings clauses alone with no principal in sight. Nothing reached it from
    a non-owner plane — `POST /api/genesis` is outside `_SAFE_UNAUTH_API` and a review bearer is
    confined to GET/HEAD/OPTIONS on `/api/review*` — so what was wrong was the INVARIANT, one
    router-mount edit from being a live gap. `test_the_toolset_carries_the_portfolio_only_for_the
    _owner_plane` drives `build_tools` and could not see a second mount site.

    AST over the whole package, so a THIRD site cannot appear unguarded: every construction of a
    portfolio provider must sit in a function that also calls `portfolio_access`.
    """
    import ast
    from pathlib import Path

    serve = Path(__file__).resolve().parents[1] / "looplab" / "serve"
    providers = {"CrossRunTools", "ConceptGovernanceTools"}
    offenders = []
    for path in sorted(serve.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            called = {n.func.id for n in ast.walk(fn)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            mounted = providers & called
            if mounted and "portfolio_access" not in called:
                offenders.append(f"{path.name}::{fn.name} mounts {sorted(mounted)}")
    assert not offenders, (
        "a portfolio provider is mounted without asking `portfolio_access` — the decision is a "
        f"property of the party, not of the process: {offenders}")
