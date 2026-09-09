"""The paid scope-report GENERATION protocol and its staleness cache, WITHOUT an ASGI app.

This is the instrument doc 25 SR-02's remaining arm was extracted for. The 548-line
`generate_scope_report_ep` and the ~210-line source-probe cache lived as closures inside
`routers/reports.py::build_router`, so a durable claim ledger, two OS byte-range leases, a paid
provider call and a five-way publication fence were all reachable only through HTTP — and the cache,
whose whole job is to NOT re-read a file, could not be asked how many times it read one.

Every test below calls `serve/scope_generate.py` with a stub `srv` carrying the six attributes the
protocol actually uses (`root`, `reports_dir`, `projects`, `run_membership`, `jobs`, `llm_settings`
+ `make_llm_client`) and a REAL `JobRegistry`. No app, no engine, no router. The properties that
matter here are the ones HTTP makes expensive to observe: how often the cache parses an event log,
what a formerly unreadable source does to a published report's staleness, and that a scope whose
evidence moves mid-flight refuses to publish and spends nothing.
"""
from __future__ import annotations

import ast
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from fastapi import HTTPException

from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.serve import scope_generate, scope_report_store as store
from looplab.serve.jobs import JobRegistry
from looplab.serve.projects import ProjectStore
from looplab.serve.scope_sources import probe_scope_log_sig
from tests._source_scan import iter_trees

TASK_ID = "generate-service"


def _seed_run(root: Path, run_id: str, task_id: str = TASK_ID) -> Path:
    """The same minimal run the HTTP scope tests seed — one `run_started`, no engine work."""
    rd = root / run_id
    rd.mkdir(parents=True, exist_ok=True)
    EventStore(rd / "events.jsonl").append("run_started", {
        "run_id": run_id, "task_id": task_id, "goal": f"goal {task_id}", "direction": "min",
    })
    return rd


def _srv(tmp_path: Path, run_ids: list[str]):
    """Exactly what the generation protocol reads off `AppState`, and nothing else."""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    def _boom_client(*_args, **_kwargs):
        raise RuntimeError("no provider in this test")

    return SimpleNamespace(
        root=tmp_path,
        reports_dir=reports_dir,
        projects=ProjectStore(tmp_path / "projects.json"),
        run_membership=lambda: [
            {"run_id": rid, "task_id": TASK_ID, "project_id": None, "supertask_id": None}
            for rid in run_ids],
        phase=lambda _st, **_kw: "finished",
        jobs=JobRegistry(),
        llm_settings=lambda _rd=None: Settings(),
        make_llm_client=_boom_client,
    )


def _generate(srv, scope_type: str = "task", scope_id: str = TASK_ID, action_id=None) -> dict:
    """Drive the async endpoint body on its own event loop — no TestClient, no app."""
    return anyio.run(
        scope_generate.durable_generate_scope_report,
        srv, scope_generate.ScopeSourceProbes(srv), scope_type, scope_id,
        action_id or str(uuid.uuid4()))


# ------------------------------------------------------------------ the cache, counted

class _CountingCapture:
    """Wrap the real capture so a test can ask how many times a log was actually PARSED."""

    def __init__(self, monkeypatch):
        self.calls: list[str] = []
        real = scope_generate.capture_scope_source

        def counted(root, run_id, **kwargs):
            self.calls.append(run_id)
            return real(root, run_id, **kwargs)

        monkeypatch.setattr(scope_generate, "capture_scope_source", counted)


def test_an_unchanged_source_is_parsed_once_however_many_times_it_is_checked(
        tmp_path, monkeypatch):
    """The cache's entire reason to exist, stated as a count. Through HTTP this property is
    invisible: every GET answers `stale:false` whether it re-read a 30 MB log or not."""
    _seed_run(tmp_path, "stable")
    srv = _srv(tmp_path, ["stable"])
    probes = scope_generate.ScopeSourceProbes(srv)
    log_sig = probe_scope_log_sig(tmp_path, "stable")
    captured = _CountingCapture(monkeypatch)
    expected = scope_generate.capture_scope_source(tmp_path, "stable").revision
    captured.calls.clear()

    verdicts = [probes.revision_is_current("stable", log_sig, expected, 10_000_000)[0]
                for _ in range(5)]

    assert verdicts == [True] * 5
    assert captured.calls == ["stable"], "the cache re-parsed an unchanged log"


def test_a_rewritten_source_is_re_read_and_reported_stale(tmp_path, monkeypatch):
    """The other half of the same property: the cache may never answer for bytes it has not seen.
    The key is stat-derived, so an append changes it and the old entry can never be hit again."""
    _seed_run(tmp_path, "moving")
    srv = _srv(tmp_path, ["moving"])
    probes = scope_generate.ScopeSourceProbes(srv)
    log_sig = probe_scope_log_sig(tmp_path, "moving")
    expected = scope_generate.capture_scope_source(tmp_path, "moving").revision
    assert probes.revision_is_current("moving", log_sig, expected, 10_000_000)[0] is True

    EventStore(tmp_path / "moving" / "events.jsonl").append("run_finished", {"reason": "budget"})
    captured = _CountingCapture(monkeypatch)

    # The caller re-probes first, exactly as the staleness GET does, so the changed identity is what
    # reaches the cache — and the cheap key alone is enough to refuse without a parse.
    fresh_sig = probe_scope_log_sig(tmp_path, "moving")
    assert probes.revision_is_current("moving", fresh_sig, expected, 10_000_000)[0] is False
    assert captured.calls == ["moving"], "a changed source must be re-read, once"


def test_the_two_staleness_rungs_treat_an_unreadable_source_differently_on_purpose(
        tmp_path, monkeypatch):
    """A source that cannot be captured is already stale, so the REVISION rung negative-caches it:
    re-parsing the same bounded-but-large log on every GET cannot improve that answer.

    The OMISSION rung is the deliberate asymmetry. Accessibility is not part of the cheap stat key,
    so a transient lock can clear without changing it — a run recorded as omitted is therefore
    re-opened at every check, because becoming readable makes it new model-visible evidence."""
    _seed_run(tmp_path, "flaky")
    srv = _srv(tmp_path, ["flaky"])
    probes = scope_generate.ScopeSourceProbes(srv)
    log_sig = probe_scope_log_sig(tmp_path, "flaky")
    expected = scope_generate.capture_scope_source(tmp_path, "flaky").revision
    real = scope_generate.capture_scope_source
    readable = False
    parses = 0

    def sometimes(root, run_id, **kwargs):
        nonlocal parses
        parses += 1
        if not readable:
            raise scope_generate.ScopeSourceError("test-only unreadable tail")
        return real(root, run_id, **kwargs)

    monkeypatch.setattr(scope_generate, "capture_scope_source", sometimes)

    # The revision rung: one failed capture, then the negative cache answers.
    assert probes.revision_is_current("flaky", log_sig, expected, 10_000_000)[0] is False
    assert probes.revision_is_current("flaky", log_sig, expected, 10_000_000)[0] is False
    assert parses == 1, "an unreadable revision was re-parsed instead of negative-cached"

    # The omission rung, on a source the cache has never seen succeed: re-opened every time.
    fresh = scope_generate.ScopeSourceProbes(srv)
    probe_receipt = fresh.probe_receipt("flaky", log_sig)[0]
    parses = 0
    assert fresh.omission_is_current("flaky", log_sig, probe_receipt, 10_000_000)[0] is True
    assert fresh.omission_is_current("flaky", log_sig, probe_receipt, 10_000_000)[0] is True
    assert parses == 2, "an omitted source must be re-opened; the stat key cannot see access"

    readable = True
    assert fresh.omission_is_current("flaky", log_sig, probe_receipt, 10_000_000)[0] is False
    assert parses == 3, "a repaired source is new evidence and must make the report stale"


def test_a_probe_receipt_is_stable_for_a_source_that_cannot_be_probed_at_all(tmp_path):
    """`probe_receipt` has to return a digest even with nothing to observe, because the receipt is
    persisted on the report and later compared. A raising probe would make an omitted run
    unrecordable; a random one would make every later comparison say `changed`."""
    srv = _srv(tmp_path, [])
    probes = scope_generate.ScopeSourceProbes(srv)

    receipt, key = probes.probe_receipt("never-existed", ["never-existed", "", 0, 0, 0, 0, 0])

    assert key is None
    assert receipt == probes.probe_receipt("never-existed", ["never-existed", "", 0, 0, 0, 0, 0])[0]
    with pytest.raises(scope_generate.ScopeSourceError):
        probes.probe_key("never-existed", ["never-existed", "", 0, 0, 0, 0, 0])


# ------------------------------------------------------------------ the projections

def test_scope_run_ids_and_the_context_digest_answer_different_questions(tmp_path):
    """Membership decides WHICH runs the report covers; the context digest decides whether the
    report's MEANING moved. A relabelled run keeps the same membership and must still invalidate."""
    _seed_run(tmp_path, "one")
    _seed_run(tmp_path, "two")
    srv = _srv(tmp_path, ["one", "two"])

    ids = scope_generate.scope_run_ids(srv, "task", TASK_ID)
    before = scope_generate.scope_context_digest(srv, "task", TASK_ID, ids)
    srv.projects.set_label("two", "the interesting one")
    after = scope_generate.scope_context_digest(srv, "task", TASK_ID, ids)

    assert ids == ["one", "two"]
    assert scope_generate.scope_run_ids(srv, "task", "no-such-task") == []
    assert before != after, "a run label is model-visible evidence and must move the digest"


# ------------------------------------------------------------------ the endpoint body

def test_the_offline_generation_publishes_one_confirmed_record_and_clears_its_fence(tmp_path):
    """The happy path end to end with no provider and no app: a deterministic rollup is published,
    the durable action reaches `done`, and the per-scope fence is cleared so the next UUID may bill."""
    _seed_run(tmp_path, "published")
    srv = _srv(tmp_path, ["published"])
    action_id = str(uuid.uuid4())

    result = _generate(srv, action_id=action_id)

    assert result["ok"] is True and result["authoritative"] is True
    assert result["run_ids"] == ["published"] and result["omitted_runs"] == []
    assert result["action_id"] == action_id
    with store._scope_store_lock(srv.reports_dir):
        receipt = store._read_scope_action_receipt(srv.reports_dir, "task", TASK_ID, action_id)
        fence = store._read_scope_action_fence(srv.reports_dir, "task", TASK_ID)
    assert receipt["status"] == "done"
    assert receipt["result"] == store._scope_action_success(action_id)
    assert fence is None or fence.get("state") != "active"
    # The paid payload is the published record, and it is what a later reader replays from.
    published = json.loads(
        store._scope_report_path(srv.reports_dir, "task", TASK_ID).read_text("utf-8"))
    assert published["action_id"] == action_id


def test_replaying_one_action_id_never_bills_a_second_generation(tmp_path):
    """The durable identity, not the request, is what a paid attempt is keyed on. A lost response
    must be rejoinable by UUID — and rejoining must READ the terminal, never recompute it."""
    _seed_run(tmp_path, "replayed")
    srv = _srv(tmp_path, ["replayed"])
    action_id = str(uuid.uuid4())

    first = _generate(srv, action_id=action_id)
    generated_at = json.loads(
        store._scope_report_path(srv.reports_dir, "task", TASK_ID).read_text("utf-8"))["generated_at"]
    second = _generate(srv, action_id=action_id)
    after = json.loads(
        store._scope_report_path(srv.reports_dir, "task", TASK_ID).read_text("utf-8"))["generated_at"]

    assert first["ok"] is True
    assert second["action_id"] == action_id
    assert after == generated_at, "the replay recomputed and republished a paid report"


def test_evidence_that_moves_after_the_reservation_refuses_to_publish(tmp_path, monkeypatch):
    """The fence the five `_inputs_unchanged` calls exist for. The scope's evidence changes between
    the reservation and the compute, so the job must refuse — and refuse WITHOUT publishing, because
    the record would then assert a synthesis over bytes nobody read."""
    _seed_run(tmp_path, "shifting")
    srv = _srv(tmp_path, ["shifting"])
    real = scope_generate.capture_scope_source

    def append_then_capture(root, run_id, **kwargs):
        # One append, on the first capture only: the reservation's signature is now history.
        if not appended:
            appended.append(True)
            EventStore(Path(root) / run_id / "events.jsonl").append(
                "run_finished", {"reason": "budget"})
        return real(root, run_id, **kwargs)

    appended: list = []
    monkeypatch.setattr(scope_generate, "capture_scope_source", append_then_capture)

    result = _generate(srv)

    assert result["ok"] is False
    assert result["code"] == store._SCOPE_INPUTS_CHANGED["code"]
    assert not list(srv.reports_dir.glob("*.json")), "a refused generation published a record"


def test_a_second_uuid_is_refused_while_another_action_holds_the_scope(tmp_path, monkeypatch):
    """One scope, one paid action at a time. The refusal names the UUID that holds it, so the
    operator can observe or abandon that action rather than mint a third identity."""
    _seed_run(tmp_path, "contended")
    srv = _srv(tmp_path, ["contended"])
    holder = str(uuid.uuid4())
    # Seed exactly what a live claiming worker leaves behind, through the store's own writers.
    with store._scope_store_lock(srv.reports_dir):
        lease = store._acquire_scope_action_lease(srv.reports_dir, "task", TASK_ID, holder)
        scope_lease = store._acquire_scope_action_scope_lease(srv.reports_dir, "task", TASK_ID)
        store._write_scope_action_receipt(srv.reports_dir, "task", TASK_ID, {
            "schema": store._SCOPE_ACTION_SCHEMA,
            "scope_identity": store._scope_identity("task", TASK_ID),
            "action_id": holder,
            "generation_identity": "scope-report:" + "a" * 64,
            "job_id": "0" * 15 + "1",
            "status": "running",
            "updated_at": 1,
            "result": None,
        })
        store._write_scope_action_fence(srv.reports_dir, "task", TASK_ID, holder, "active")
    try:
        with pytest.raises(HTTPException) as refused:
            _generate(srv)
    finally:
        scope_lease.release()
        lease.release()

    assert refused.value.status_code == 409
    assert refused.value.detail["action_id"] == holder


def test_the_endpoint_refuses_before_it_can_reserve_anything(tmp_path):
    """The four pre-claim refusals, each of which must happen BEFORE a permanent UUID marker or a
    job slot exists — a rejected request may not leave an orphan identity behind."""
    _seed_run(tmp_path, "refusals")
    srv = _srv(tmp_path, ["refusals"])

    with pytest.raises(HTTPException) as bad_scope:
        _generate(srv, scope_type="bogus")
    with pytest.raises(HTTPException) as no_key:
        anyio.run(scope_generate.durable_generate_scope_report,
                  srv, scope_generate.ScopeSourceProbes(srv), "task", TASK_ID, None)
    with pytest.raises(HTTPException) as bad_key:
        _generate(srv, action_id="not-a-uuid")
    with pytest.raises(HTTPException) as empty:
        _generate(srv, scope_id="a-task-with-no-runs")

    assert bad_scope.value.status_code == 400
    assert no_key.value.status_code == 428      # the key is REQUIRED, not merely malformed
    assert bad_key.value.status_code == 400
    assert empty.value.status_code == 400
    assert not list(srv.reports_dir.glob("**/*.json")), "a refused request left durable state"


# ------------------------------------------------------------------ the seams the move creates

def test_the_generation_module_defines_no_route():
    """The same bar the store and the action protocol are held to: raising `HTTPException` is not
    HTTP-SERVING (`scope_actions.py` and `trace_clear.py` both do it), but an `APIRouter` or a route
    decorator would make this a second router."""
    path, tree = next((p, t) for p, t in iter_trees() if p.name == "scope_generate.py")
    assert "APIRouter" not in path.read_text(encoding="utf-8-sig", errors="replace")
    decorators = {ast.dump(d) for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) for d in n.decorator_list}
    assert not decorators, decorators


def test_the_generate_route_is_one_delegating_call():
    """What SR-02 asked for. Over the AST, because `pass  # return await durable_generate…` would
    satisfy a positive source pin while the endpoint does nothing at all."""
    tree = next(t for path, t in iter_trees()
                if path.name == "reports.py" and "routers" in path.parts)
    route = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == "generate_scope_report")
    body = [s for s in route.body
            if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
    assert len(body) == 1, f"the route kept generation logic: {len(body)} statements"
    assert isinstance(body[0], ast.Return) and isinstance(body[0].value, ast.Await)
    call = body[0].value.value
    assert call.func.id == "durable_generate_scope_report"
    assert [a.id for a in call.args][:2] == ["srv", "probes"]


def test_the_router_holds_no_copy_of_the_projections_or_the_cache():
    """The finding was that both lived inside `build_router`. A router that re-defined either would
    read as extracted while every call still went to its own copy."""
    tree = next(t for path, t in iter_trees()
                if path.name == "reports.py" and "routers" in path.parts)
    defined = {n.name for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    for name in ("_source_probe_key", "_source_probe_receipt", "_revision_is_current",
                 "_omission_is_current", "_scope_run_ids", "_scope_sig", "_run_brief",
                 "_scope_drill", "_scope_context_digest", "_scope_source_sizes"):
        assert name not in defined, f"{name} is still defined in the router"


def test_the_router_and_the_generation_service_resolve_to_the_same_objects():
    """Both modules star-import the store, which binds BY VALUE. The staleness GET and the paid
    generation must be patchable as one seam, so the two copies have to be the same object."""
    from looplab.serve.routers import reports as reports_router

    for name in ("_scope_store_lock", "_read_or_migrate_scope_record", "_scope_report_path",
                 "_SCOPE_TYPES", "_SCOPE_STORAGE_ERROR"):
        assert getattr(reports_router, name) is getattr(store, name), name
        assert getattr(scope_generate, name) is getattr(store, name), name
