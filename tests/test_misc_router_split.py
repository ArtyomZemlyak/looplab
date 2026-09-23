"""`routers/misc.py` holds ROUTES; the protocols it used to host are services it calls (review
2026-09-22, SRV2-13 / doc 50 SR-04).

The review measured the grab-bag router at 2,172 lines (2,223 when this landed) carrying three
things that were not routes: the files-as-truth authoring operation store (receipts, an
interprocess lock, a 4,096-receipt quota, a v1 schema), the paid LLM-health probe with its replay
registry, and the Memory panel's projection. They are `serve/authoring_store.py`,
`serve/llm_probe.py` and `serve/memory_projection.py` now, and the router keeps declarations,
validation and translation.

Two halves, to the bar `tests/test_concept_lens_service.py` set for a serve extraction:

* THE RULE, on the AST. `router_protocol_defects` is the statable form of the review's proposal —
  "misc.py defines no RuntimeError subclass and calls `_interprocess_lock` only inside route
  bodies" — widened to what a protocol is BUILT from: a class that is not a pydantic
  request/response shape, and any durability primitive (a cross-process lock, a durable write or
  fsync, an in-process lock/event registry, a process-keyed secret) the module reaches itself. The
  lock clause is stronger than proposed — misc.py takes no lock anywhere — because after the move no
  route needs one. Driven on synthetic routers, then on the real ones in both directions: the router
  passes and the two moved protocols fail it where they live now.
* THE STATES HTTP CANNOT REACH CHEAPLY, against a stub `srv` with no app: the authoring store's
  replay, CAS conflict, quota and recovery of a prepared receipt; the probe's exactly-once replay,
  its single-flight refusal and its post-attempt fence; the memory view's degraded concept index.
  Before the move the receipt machine had three HTTP requests in the whole suite, all refusals.
"""
from __future__ import annotations

import ast
import inspect
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
from fastapi import HTTPException  # noqa: E402

from looplab.serve import authoring_store, llm_probe, memory_projection  # noqa: E402
from looplab.serve.llm_context import llm_settings  # noqa: E402
from looplab.serve.routers import misc  # noqa: E402
from looplab.serve.settings_store import SettingsStore  # noqa: E402

SERVICES = (authoring_store, llm_probe, memory_projection)


# ------------------------------------------------------------------ the rule

# What the moved protocols were made of: the authoring store took `interprocess_lock`, published
# with `strict_atomic_write_*` / `atomic_write_text`, confirmed with `strict_fsync_parent` /
# `_ensure_strict_parent` and serialized threads on a module `threading.Lock`; the probe kept an
# `OrderedDict` registry under a `Lock`, an `Event` per flight and a `secrets.token_bytes` HMAC key.
PROTOCOL_PRIMITIVES = frozenset({
    "interprocess_lock", "EventStoreLockError",
    "strict_atomic_write_text", "strict_atomic_write_bytes", "atomic_write_text",
    "strict_fsync", "strict_fsync_parent", "_ensure_strict_parent", "durable_no_replace_rename",
    "Lock", "RLock", "Event", "Condition", "Semaphore", "OrderedDict",
    "token_bytes", "hmac",
})


def router_protocol_defects(tree: ast.Module) -> list[str]:
    """Why a module is HOSTING a protocol rather than calling one — empty for a plain router.

    1. A class that is not a pydantic request/response SHAPE: an exception type, a registry, a state
       machine. A shape derives from `BaseModel`, directly or through a shape defined earlier in the
       same module.
    2. A protocol primitive the module reaches ITSELF — imported, called or handed on, as a name or
       an attribute. Comments and docstrings are not code and cannot trip it (or satisfy it).
    """
    defects: list[str] = []
    shapes: set[str] = set()
    classes = sorted((n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)),
                     key=lambda n: n.lineno)
    for node in classes:
        bases = {getattr(b, "id", None) or getattr(b, "attr", None) for b in node.bases}
        if bases and bases <= ({"BaseModel"} | shapes):
            shapes.add(node.name)
        else:
            defects.append(f"class {node.name}({', '.join(sorted(filter(None, bases)))}) is not a "
                           "pydantic request/response shape")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = {a.asname or a.name.split(".")[-1] for a in node.names} | {
                a.name.split(".")[-1] for a in node.names}
        elif isinstance(node, ast.Name):
            names = {node.id}
        elif isinstance(node, ast.Attribute):
            names = {node.attr}
        else:
            continue
        for name in sorted(names & PROTOCOL_PRIMITIVES):
            defects.append(f"line {node.lineno}: reaches the protocol primitive `{name}`")
    return defects


def _defects(source: str) -> list[str]:
    return router_protocol_defects(ast.parse(inspect.cleandoc(source)))


def test_the_rule_on_synthetic_routers():
    """The truth table, on code nobody can satisfy with a comment."""
    clean = '''
        """A router that mentions interprocess_lock and threading.Lock() only in prose."""
        from pydantic import BaseModel
        from looplab.serve.some_service import do_the_work

        class Request(BaseModel):
            text: str

        class LegacyRequest(Request):          # a shape through a shape is still a shape
            pass

        def build_router(srv):
            def route(body: Request):
                # interprocess_lock(path) would be a defect; this comment is not
                return do_the_work(srv, body)
            return route
    '''
    assert _defects(clean) == []
    assert any("_Failure(RuntimeError)" in d for d in _defects('''
        class _Failure(RuntimeError):
            pass
    '''))
    assert any("class Registry()" in d for d in _defects('''
        class Registry:
            pass
    '''))
    # The review's clause said "only inside route bodies"; the rule here refuses it anywhere.
    assert _defects('''
        from looplab.events.eventstore import interprocess_lock
        def build_router(srv):
            def route():
                with interprocess_lock(srv.path, required=True):
                    return {}
            return route
    ''')
    for body in ("_LOCK = threading.Lock()", "KEY = secrets.token_bytes(32)",
                 "REGISTRY = OrderedDict()", "done = threading.Event()",
                 "from looplab.core.atomicio import strict_atomic_write_text",
                 "import hmac", "handler = atomic_write_text"):
        assert _defects(body), body


def _tree_of(module) -> ast.Module:
    return ast.parse(Path(module.__file__).read_text(encoding="utf-8"))


def test_the_misc_router_hosts_no_protocol():
    """MUTATION: move `_AuthoringFailure` back, or `_AUTHOR_THREAD_LOCK = threading.Lock()`, or a
    `secrets.token_bytes(32)` registry key into `build_router` -> red, naming the line."""
    assert router_protocol_defects(_tree_of(misc)) == []


def test_the_rule_is_not_vacuous_on_the_protocols_it_moved():
    """The other direction, on real code: both moved protocols fail the router rule where they live
    now, so a rule that could not see them would be caught here rather than trusted."""
    store = router_protocol_defects(_tree_of(authoring_store))
    probe = router_protocol_defects(_tree_of(llm_probe))
    assert any("_AuthoringFailure(RuntimeError)" in d for d in store), store
    assert any("`interprocess_lock`" in d for d in store), store
    assert any("`token_bytes`" in d for d in probe) and any("`Event`" in d for d in probe), probe
    # The read model is not a protocol and passes the router rule as it is.
    assert router_protocol_defects(_tree_of(memory_projection)) == []


def test_the_services_define_no_route():
    """Raising `HTTPException` is not serving (the probe does, as the concept-lens service does); a
    router object or a route decorator would make a service a second router."""
    for module in SERVICES:
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "APIRouter" not in source, module.__name__
        decorated = [n.name for n in ast.walk(ast.parse(source))
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                     and n.decorator_list]
        assert not decorated, (module.__name__, decorated)
    # Nor does the authoring store speak HTTP at all: it raises its own failure type and the router
    # owns the ONE translation (`_authoring_http_failure`), with its `Cache-Control` header.
    assert "fastapi" not in Path(authoring_store.__file__).read_text(encoding="utf-8")


def _defined(tree: ast.Module) -> set[str]:
    out = {n.name for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    out |= {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Name)}
    return out


def test_the_router_holds_no_copy_of_what_moved():
    """A router that re-defined any of these would read as extracted while its own routes still
    called the copy — the failure both earlier serve extractions had to guard against."""
    router = _defined(_tree_of(misc))
    for module in SERVICES:
        moved = {n.name for n in _tree_of(module).body
                 if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        moved |= {t.id for n in _tree_of(module).body if isinstance(n, ast.Assign)
                  for t in n.targets if isinstance(t, ast.Name)}
        assert not (router & moved), (module.__name__, sorted(router & moved))


def test_the_listing_bounds_have_one_binding_a_patch_can_reach():
    """`tests/test_server.py` narrows `_AUTHOR_MAX_FILES` / `_AUTHOR_SKILL_MAX_DEPTH` and swaps
    `_read_author_file_safely` on `authoring_store`. A read of any of them left in the router would
    be a second binding that patch silently misses, so the router must not read them at all — and
    likewise for the memory bounds `tests/test_memory_endpoint.py` narrows."""
    read = {n.id for n in ast.walk(_tree_of(misc)) if isinstance(n, ast.Name)}
    for name in ("_AUTHOR_MAX_FILES", "_AUTHOR_SKILL_SCAN_ENTRIES", "_AUTHOR_SKILL_MAX_DEPTH",
                 "_AUTHOR_MAX_RECEIPTS", "_read_author_file_safely", "_skill_author_candidates",
                 "_MEMORY_TIER_LIMIT", "_MEMORY_SOURCE_BYTES", "_MEMORY_SOURCE_ROWS",
                 "_MEMORY_ROW_BYTES", "_MEMORY_EVIDENCE_MAX"):
        assert name not in read, name


def _route(name: str) -> ast.FunctionDef:
    return next(n for n in ast.walk(_tree_of(misc))
                if isinstance(n, ast.FunctionDef) and n.name == name)


def test_the_probe_and_memory_routes_are_one_delegating_call():
    """What SR-04 asked for, over the AST: `pass  # return llm_health_operation(...)` satisfies a
    source pin while the endpoint does nothing."""
    for name, callee, args in (("llm_health", "llm_health_operation",
                                ["srv", "llm_health_registry", "body"]),
                               ("memory", "memory_view", ["srv"])):
        body = [s for s in _route(name).body
                if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        assert len(body) == 1 and isinstance(body[0], ast.Return), (name, len(body))
        call = body[0].value
        assert call.func.id == callee and [a.id for a in call.args] == args, ast.dump(call)
    # The registry is built ONCE per router build, i.e. per app: the lifetime the closures had.
    build = _route("build_router")
    made = [n for n in ast.walk(build) if isinstance(n, ast.Call)
            and getattr(n.func, "id", "") == "LLMHealthRegistry"]
    assert len(made) == 1 and made[0] in {s.value for s in build.body if isinstance(s, ast.Assign)}


# ------------------------------------------------------------------ the probe, with no app

class _ProbeClient:
    def __init__(self, on_probe=None, error=None):
        self.probes = []
        self.on_probe = on_probe
        self.error = error

    def probe(self, messages, max_tokens):
        self.probes.append(max_tokens)
        if self.on_probe is not None:
            self.on_probe()
        if self.error is not None:
            raise self.error


def _probe_srv(tmp_path, monkeypatch, client):
    """Exactly what the probe reads: the settings store, `llm_settings()`, `make_llm_client`."""
    monkeypatch.setenv("LOOPLAB_LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("LOOPLAB_LLM_MODEL", "probe-model")
    store = SettingsStore(tmp_path)
    built = []

    def make_llm_client(settings, **kwargs):
        built.append(kwargs)
        return client

    srv = SimpleNamespace(settings=store, llm_settings=lambda: llm_settings(store),
                          make_llm_client=make_llm_client)
    return srv, store, built


def _health_body(store, operation_id=None, **extra):
    return llm_probe.LLMHealthRequest(
        expected_settings_revision=store.ui_settings_revision(),
        expected_secret_revision=store.secret_revision(),
        operation_id=operation_id or str(uuid.uuid4()), **extra)


def _refusal(call) -> tuple[int, dict]:
    with pytest.raises(HTTPException) as caught:
        call()
    return caught.value.status_code, caught.value.detail


def test_a_probe_operation_is_paid_once_and_replayed_after(tmp_path, monkeypatch):
    client = _ProbeClient()
    srv, store, built = _probe_srv(tmp_path, monkeypatch, client)
    registry = llm_probe.LLMHealthRegistry()
    body = _health_body(store)

    first = llm_probe.llm_health_operation(srv, registry, body)
    assert first["ok"] is True and first["provider_attempted"] is True
    # The paid call's own contract: one attempt, no retry, four output tokens, nothing cached.
    assert client.probes == [llm_probe._LLM_HEALTH_MAX_TOKENS] == [4]
    assert built[0]["max_retries"] == 0 and built[0]["cache"] is False
    assert built[0]["stream"] is False and built[0]["disable_reasoning"] is True

    assert llm_probe.llm_health_operation(srv, registry, body) == first
    assert llm_probe.llm_health_operation(srv, registry, _health_body(
        store, body.operation_id, replay_only=True)) == first
    assert len(client.probes) == 1, "a replay must never pay again"

    # The same id under another saved-configuration identity is a conflict, not a second probe.
    with store.settings_write_transaction():
        store.write_ui_settings({})
    status, detail = _refusal(lambda: llm_probe.llm_health_operation(
        srv, registry, _health_body(store, body.operation_id)))
    assert status == 409 and detail["code"] == "llm_health_operation_conflict"
    # Reconciliation of an id this registry never saw makes no call: absence stays absence.
    status, detail = _refusal(lambda: llm_probe.llm_health_operation(
        srv, registry, _health_body(store, replay_only=True)))
    assert status == 410 and detail["outcome_unknown"] is True and detail["replay_only"] is True
    assert len(client.probes) == 1


def test_one_probe_is_in_flight_per_app(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def hold():
        entered.set()
        assert release.wait(10), "the test never released the in-flight probe"

    client = _ProbeClient(on_probe=hold)
    srv, store, _built = _probe_srv(tmp_path, monkeypatch, client)
    registry = llm_probe.LLMHealthRegistry()
    first_body = _health_body(store)
    results = []
    leader = threading.Thread(
        target=lambda: results.append(llm_probe.llm_health_operation(srv, registry, first_body)))
    leader.start()
    try:
        assert entered.wait(10), "the leader never reached the provider"
        status, detail = _refusal(
            lambda: llm_probe.llm_health_operation(srv, registry, _health_body(store)))
        assert status == 409 and detail["code"] == "llm_health_in_progress"
        assert detail["provider_attempted"] is False
    finally:
        release.set()
        leader.join(10)
    assert results and results[0]["ok"] is True
    assert len(client.probes) == 1


def test_a_configuration_change_during_the_call_is_a_terminal_ambiguous_outcome(tmp_path,
                                                                             monkeypatch):
    """The post-attempt fence: the provider may have done (and billed) the work, so the outcome is
    TERMINAL and unknown — recorded once, replayed as recorded, never re-probed."""
    holder = {}

    def save_settings_mid_flight():
        with holder["store"].settings_write_transaction():
            holder["store"].write_ui_settings({})

    client = _ProbeClient(on_probe=save_settings_mid_flight)
    srv, store, _built = _probe_srv(tmp_path, monkeypatch, client)
    holder["store"] = store
    registry = llm_probe.LLMHealthRegistry()
    body = _health_body(store)
    status, detail = _refusal(lambda: llm_probe.llm_health_operation(srv, registry, body))
    assert status == 409 and detail["code"] == "llm_configuration_changed_after_attempt"
    assert detail["provider_attempted"] is True and detail["outcome_unknown"] is True
    assert _refusal(lambda: llm_probe.llm_health_operation(srv, registry, body)) == (status, detail)
    assert len(client.probes) == 1


@pytest.mark.parametrize("message,unknown", [
    ("401 authentication error", False),   # the provider declined: nothing was billed
    ("429 rate-limited", False),
    ("provider error", True),              # may have run upstream: the outcome stays unresolved
])
def test_only_a_definitive_rejection_resolves_a_failed_probe(tmp_path, monkeypatch, message,
                                                             unknown):
    client = _ProbeClient(error=RuntimeError(f"{message}; upstream said UPSTREAM-TOKEN-7f3a"))
    srv, store, _built = _probe_srv(tmp_path, monkeypatch, client)
    out = llm_probe.llm_health_operation(srv, llm_probe.LLMHealthRegistry(), _health_body(store))
    assert out["ok"] is False and out["provider_attempted"] is True
    assert out["outcome_unknown"] is unknown
    assert (out.get("code") == "llm_health_provider_outcome_unknown") is unknown
    assert "UPSTREAM-TOKEN" not in str(out), "the provider's own text is never reflected"


# ------------------------------------------------------------------ the authoring store, no app

def _author_srv(tmp_path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    settings = SimpleNamespace(prompt_dir=None, skills_dir=None, knowledge_dir=str(knowledge),
                               memory_dir=None)
    srv = SimpleNamespace(root=tmp_path / "runs", settings=SettingsStore(tmp_path),
                          global_settings=lambda: settings)
    srv.root.mkdir()
    root_id = authoring_store._author_target_root_id(
        authoring_store._configured_author_root(knowledge, "knowledge"))
    return srv, knowledge, root_id


def _write(srv, root_id, text: bytes, *, operation_id=None, expected="missing"):
    return authoring_store._run_author_operation(
        srv, kind="knowledge", name="note.md", operation_id=operation_id or str(uuid.uuid4()),
        text_bytes=text, expected_revision=expected, expected_target_root_id=root_id)


def test_an_authoring_operation_applies_once_and_replays_its_receipt(tmp_path):
    srv, knowledge, root_id = _author_srv(tmp_path)
    op = str(uuid.uuid4())
    first = _write(srv, root_id, b"# one\n", operation_id=op)
    assert first["status"] == "succeeded" and first["ok"] is True
    assert (knowledge / "note.md").read_bytes() == b"# one\n"

    # A lost response retried: the receipt answers, the file is not written again.
    (knowledge / "note.md").write_bytes(b"# edited by hand afterwards\n")
    assert _write(srv, root_id, b"# one\n", operation_id=op) == first
    assert (knowledge / "note.md").read_bytes() == b"# edited by hand afterwards\n"

    # The id is bound to its payload; a different payload under it is refused, not applied.
    with pytest.raises(authoring_store._AuthoringFailure) as caught:
        _write(srv, root_id, b"# two\n", operation_id=op)
    assert caught.value.code == "authoring_operation_conflict"

    # A stale CAS token is a durable CONFLICT receipt, and the file keeps the newer bytes.
    stale = _write(srv, root_id, b"# three\n", expected=first["desired_revision"])
    assert stale["status"] == "conflict" and stale["code"] == "authoring_revision_conflict"
    assert (knowledge / "note.md").read_bytes() == b"# edited by hand afterwards\n"


def test_an_exhausted_quota_refuses_new_ids_and_still_replays_old_ones(tmp_path, monkeypatch):
    """TTL/LRU eviction would let an old timed-out request apply again once its receipt vanished,
    so the store refuses NEW ids at the cap instead. Over HTTP this state costs 4,096 operations."""
    monkeypatch.setattr(authoring_store, "_AUTHOR_MAX_RECEIPTS", 2)
    srv, knowledge, root_id = _author_srv(tmp_path)
    first_op = str(uuid.uuid4())
    first = _write(srv, root_id, b"a\n", operation_id=first_op)
    second = _write(srv, root_id, b"b\n", expected=first["desired_revision"])
    assert second["status"] == "succeeded"

    with pytest.raises(authoring_store._AuthoringFailure) as caught:
        _write(srv, root_id, b"c\n", expected=second["desired_revision"])
    assert caught.value.code == "authoring_receipt_quota_exhausted"
    assert caught.value.status_code == 409 and caught.value.retryable is False
    assert (knowledge / "note.md").read_bytes() == b"b\n"
    assert _write(srv, root_id, b"a\n", operation_id=first_op) == first


def test_a_prepared_receipt_is_observed_read_only_and_completed_by_its_exact_replay(tmp_path):
    """The crash window between publishing the intent and writing the file: the GET observes the
    receipt without advancing it, and only the exact PUT completes it."""
    srv, knowledge, root_id = _author_srv(tmp_path)
    op = str(uuid.uuid4())
    text = b"# recovered\n"
    desired = authoring_store._author_revision(text)
    receipt_path = authoring_store._author_operation_path(srv, op)
    receipt_path.parent.mkdir(parents=True)
    authoring_store._save_author_receipt(receipt_path, authoring_store._new_author_receipt(
        operation_id=op, kind="knowledge", name="note.md", target_root=knowledge.resolve(),
        target_root_id=root_id, expected_revision="missing", desired_revision=desired,
        status_value="prepared"))

    observed = authoring_store._lookup_author_operation(
        srv, kind="knowledge", name="note.md", operation_id=op, expected_target_root_id=root_id,
        expected_revision="missing", desired_revision=desired)
    assert observed["status"] == "prepared" and observed["replayable"] is True
    assert not (knowledge / "note.md").exists(), "an observation must not apply the write"

    done = _write(srv, root_id, text, operation_id=op)
    assert done["status"] == "succeeded" and (knowledge / "note.md").read_bytes() == text

    # An intervening writer between the intent and its replay turns the replay into a conflict
    # receipt instead of clobbering the other writer's bytes.
    other = str(uuid.uuid4())
    authoring_store._save_author_receipt(
        authoring_store._author_operation_path(srv, other), authoring_store._new_author_receipt(
            operation_id=other, kind="knowledge", name="note.md", target_root=knowledge.resolve(),
            target_root_id=root_id, expected_revision=desired,
            desired_revision=authoring_store._author_revision(b"# mine\n"),
            status_value="prepared"))
    (knowledge / "note.md").write_bytes(b"# someone else\n")
    lost = _write(srv, root_id, b"# mine\n", operation_id=other, expected=desired)
    assert lost["status"] == "conflict" and lost["code"] == "authoring_intervening_write"
    assert (knowledge / "note.md").read_bytes() == b"# someone else\n"


# ------------------------------------------------------------------ the memory view, no app

def _memory_srv(tmp_path, run_summaries):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "lessons.jsonl").write_text(
        '{"statement": "cited lesson", "run_id": "run-a"}\n'
        '{"statement": "another", "run_id": "run-b"}\n', encoding="utf-8")
    (memory_dir / "meta_notes.jsonl").write_text('{"note": "no run named"}\n', encoding="utf-8")
    return SimpleNamespace(global_settings=lambda: SimpleNamespace(memory_dir=str(memory_dir)),
                           run_summaries=run_summaries)


def test_the_memory_view_folds_only_the_runs_its_rows_cite(tmp_path):
    asked = []
    srv = _memory_srv(tmp_path, lambda only=None: asked.append(only) or [])
    out = memory_projection.memory_view(srv)
    assert [row["statement"] for row in out["lessons"]] == ["cited lesson", "another"]
    assert asked == [{"run-a", "run-b"}], "the shelf must never fold runs no row mentions"
    assert out["concept_index_available"] is True and out["page"]["partial"] is False


def test_an_unreadable_run_list_degrades_the_shelf_not_the_panel(tmp_path):
    def broken(only=None):
        raise OSError("a half-written run")

    out = memory_projection.memory_view(_memory_srv(tmp_path, broken), run_id="run-a")
    assert [row["statement"] for row in out["lessons"]] == ["cited lesson"]
    assert out["page"]["tiers"]["lessons"]["filtered"] == 1
    assert out["concept_index_available"] is False and out["page"]["partial"] is True
