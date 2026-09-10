"""The paid concept-lens subsystem, driven WITHOUT an ASGI app (doc 25 SR-04).

SR-04's complaint was never the line count either. It was that a paid claim ledger, a second
STRICTER recovery fold, a provider worker and four operator commands lived as module helpers and
`build_router` closures inside `routers/runs.py` — a file named for the run read model — so every
branch of the crash-recovery machine was reachable only by building the whole app, seeding a run and
racing HTTP. `serve/concept_lens_service.py` makes them callable; this file is the instrument.

Each test seeds a real `events.jsonl` through `EventStore` and calls the service with a stub `srv`
carrying exactly what the protocol reads (`run_dir`, `commands`, `jobs`, `llm_settings`). No app, no
engine, no router. The states below are the ones HTTP cannot construct on demand: two overlapping
claims on one generation, a terminal whose digest disagrees with the claim it answers, a terminal
that precedes its own claim, and a derived receipt that no longer validates.

`tests/test_concept_lens_durability.py` keeps the end-to-end HTTP contracts; nothing here replaces
them. What is here is the half those tests could only reach by accident.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response

from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import (
    EV_CONCEPT_LENS_COMPLETED, EV_CONCEPT_LENS_FAILED, EV_CONCEPT_LENS_STARTED,
)
from looplab.serve import concept_frame, concept_lens_service as lens
from looplab.serve.protocol import RUN_GENERATION_FIELD
from looplab.serve.run_commands import run_generation_token
from tests._source_scan import iter_trees

ID_A = "a" * 64
ID_B = "b" * 64
DIGEST_A = "1" * 64
DIGEST_B = "2" * 64


def _seed_run(root: Path) -> Path:
    """One run with a concept edge, so the ConceptFrame core has something to project."""
    rd = root / "demo"
    rd.mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {
        "run_id": "demo", "task_id": "toy", "goal": "g", "direction": "max"})
    store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": "r",
                 "concepts": ["agents/orchestrator", "llm/gpt"]},
    })
    store.append("node_evaluated", {"node_id": 0, "metric": 0.9})
    store.append("concept_edge", {"edges": [{
        "src": "agents/orchestrator", "rel": "uses", "dst": "llm/gpt",
        "confidence": 1.0, "provenance": "asserted",
    }]})
    return rd


def _lens_pack() -> list[dict]:
    from looplab.search.concept_lens import default_lenses

    return default_lenses()


def _core(rd: Path, lens_pack: list[dict]) -> dict:
    """The router's materializer, minus its two caches — the same bounded core it hands in."""
    events = EventStore(rd / "events.jsonl").read_all()
    return concept_frame.build_core(
        fold(events), run_id="demo", lens_pack=lens_pack,
        generation=run_generation_token(events), requested_seq=None,
        captured_seq=max(e.seq for e in events), max_seq=max(e.seq for e in events),
        source_divergence=None)


class _Commands:
    """The four `srv.commands` members the lens protocol calls, and nothing else."""

    def __init__(self, generation: str):
        self.generation = generation
        self.sequenced = 0

    @contextmanager
    def sequence(self, _rd):
        self.sequenced += 1
        yield

    def validate_paths(self, rd):
        return rd

    def run_generation(self, _rd):
        return self.generation

    def generation_fence(self, rd):
        return rd, self.generation


class _Jobs:
    """A JobRegistry stub that can be told a claim has, or has not, a live process receipt."""

    def __init__(self, live: dict | None = None):
        self.live = live or {}
        self.inline_wait = 0.0

    def rejoin(self, identity):
        return self.live.get(identity)

    def get(self, job_id):
        for receipt in self.live.values():
            if receipt.get("job_id") == job_id:
                return receipt
        return None


def _srv(rd: Path, generation: str, *, jobs: _Jobs | None = None):
    return SimpleNamespace(
        run_dir=lambda _run_id: rd,
        commands=_Commands(generation),
        jobs=jobs or _Jobs(),
        llm_settings=lambda _rd=None: SimpleNamespace(),
    )


def _claim(store: EventStore, identity: str, generation: str, digest: str, input_seq: int = 0):
    return store.append(EV_CONCEPT_LENS_STARTED, {
        "lens_request_id": identity, "generation": generation,
        "request_digest": digest, "input_seq": input_seq,
    })


# ----------------------------------------------------------- the strict recovery fold

def test_the_recovery_fold_fails_closed_on_every_shape_it_cannot_disambiguate(tmp_path):
    """Recovery has no browser receipt to disambiguate damaged data with, so it refuses rather than
    guessing. Four independently damaging shapes, each of which a plain fold would absorb: a second
    claim on one identity, a terminal whose digest disagrees with its claim, a terminal at or before
    its claim's seq, and a non-integer sequence. Through HTTP, seeding any of them means writing
    events by hand anyway — but then asserting on a 409 body instead of the fold's own verdict."""
    rd = _seed_run(tmp_path)
    store = EventStore(rd / "events.jsonl")
    generation = run_generation_token(store.read_all())
    _claim(store, ID_A, generation, DIGEST_A)
    _claim(store, ID_A, generation, DIGEST_A)          # the same identity claimed twice

    claims, terminals, unresolved, conflict = lens.lens_recovery_ledger(
        store.read_all(), generation)

    assert conflict is True
    assert unresolved == {ID_A} and not terminals
    assert claims[ID_A]["request_digest"] == DIGEST_A   # the FIRST claim is what survives


def test_a_terminal_whose_digest_disagrees_with_its_claim_is_a_conflict_not_a_result(tmp_path):
    """The property that makes the ledger evidence of paid work rather than of a forged receipt: a
    terminal is only ever the answer to the exact claim whose digest it carries."""
    rd = _seed_run(tmp_path)
    store = EventStore(rd / "events.jsonl")
    generation = run_generation_token(store.read_all())
    _claim(store, ID_A, generation, DIGEST_A)
    store.append(EV_CONCEPT_LENS_COMPLETED, {
        "lens_request_id": ID_A, "generation": generation, "request_digest": DIGEST_B,
        "outcome": "declined", "reason": "declined",
    })

    _claims, terminals, unresolved, conflict = lens.lens_recovery_ledger(
        store.read_all(), generation)

    assert conflict is True
    assert not terminals, "a digest-mismatched terminal was accepted as this claim's answer"
    assert unresolved == {ID_A}, "and the claim it does NOT answer stays unresolved"


def test_the_recovery_fold_ignores_another_generation_entirely(tmp_path):
    """A previous generation's paid history is not this generation's ambiguity. Conflating them
    would make every reset look like a ledger that needs repair."""
    rd = _seed_run(tmp_path)
    store = EventStore(rd / "events.jsonl")
    generation = run_generation_token(store.read_all())
    _claim(store, ID_A, "f" * 64, DIGEST_A)
    _claim(store, ID_B, generation, DIGEST_B)

    claims, _terminals, unresolved, conflict = lens.lens_recovery_ledger(
        store.read_all(), generation)

    assert conflict is False
    assert set(claims) == {ID_B} and unresolved == {ID_B}


def test_a_resolved_claim_leaves_nothing_unresolved(tmp_path):
    """The ordinary shape, so the three refusals above are known not to be the fold's only answer."""
    rd = _seed_run(tmp_path)
    store = EventStore(rd / "events.jsonl")
    generation = run_generation_token(store.read_all())
    started = _claim(store, ID_A, generation, DIGEST_A)
    store.append(EV_CONCEPT_LENS_COMPLETED, {
        "lens_request_id": ID_A, "generation": generation, "request_digest": DIGEST_A,
        "outcome": "declined", "reason": "declined",
    })

    claims, terminals, unresolved, conflict = lens.lens_recovery_ledger(
        store.read_all(), generation)

    assert conflict is False and not unresolved
    assert set(terminals) == {ID_A}
    assert claims[ID_A]["started_seq"] == started.seq


# ----------------------------------------------------------- the identities

def test_a_lens_identity_binds_the_run_the_generation_and_the_key(tmp_path):
    """Three inputs, and changing any one must change the identity — otherwise a receipt from one
    run, generation or browser tab could authorize paid work in another."""
    rd = _seed_run(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    key = "x" * 32
    base = lens.lens_identity(rd, "a" * 64, key)

    assert base != lens.lens_identity(other, "a" * 64, key)
    assert base != lens.lens_identity(rd, "b" * 64, key)
    assert base != lens.lens_identity(rd, "a" * 64, "y" * 32)
    assert base == lens.lens_identity(rd, "a" * 64, key)


def test_the_prompt_digest_is_keyed_so_the_event_log_is_not_a_prompt_oracle():
    """A plain prompt hash would let anyone who can read the diagnostic log dictionary-test common
    prompts. The HMAC key is the browser receipt, which is never logged — so equality survives a
    restart while the digest tells a reader nothing about the prompt."""
    prompt = "group by usage"
    one = lens.lens_prompt_digest("k" * 32, prompt)
    two = lens.lens_prompt_digest("j" * 32, prompt)

    assert one != two, "the same prompt under two keys produced one digest"
    assert one == lens.lens_prompt_digest("k" * 32, prompt)
    assert one != lens.lens_prompt_digest("k" * 32, prompt + " ")


def test_a_low_entropy_or_control_bearing_receipt_is_refused():
    for bad in ("", "short", "with space" + "0" * 20, "tab\t" + "0" * 20, "x" * 513):
        with pytest.raises(HTTPException) as refused:
            lens.lens_idempotency_key(bad)
        assert refused.value.status_code == 400
    assert lens.lens_idempotency_key("z" * 16) == "z" * 16


# ----------------------------------------------------------- the bounded terminal projection

def test_a_derived_terminal_that_no_longer_validates_reads_as_uncertain_never_as_a_lens(tmp_path):
    """The receipt says paid work produced a lens; the spec in it is unusable. Publishing a frame
    anyway would show the operator a lens nobody derived, so the only honest answer is ambiguous —
    and it keeps the terminal's seq, because the operator needs the receipt coordinate."""
    rd = _seed_run(tmp_path)
    lens_pack = _lens_pack()
    core = _core(rd, lens_pack)
    store = EventStore(rd / "events.jsonl")
    terminal = store.append(EV_CONCEPT_LENS_COMPLETED, {
        "lens_request_id": ID_A, "generation": core[RUN_GENERATION_FIELD],
        "request_digest": DIGEST_A, "outcome": "derived",
        "spec": {"name": "usage", "rels": "not-a-list"},
    })

    projected = lens.lens_terminal_response(terminal, core, lens_pack, ID_A)

    assert projected["ok"] is False
    assert projected["code"] == "concept_lens_uncertain" and projected["ambiguous"] is True
    assert projected["seq"] == terminal.seq


def test_an_abandoned_terminal_never_claims_the_provider_was_not_billed(tmp_path):
    """Abandonment resolves the FENCE, not the charge. The response has to say so: provider outcome
    and billing are `unknown`, because after a crash they are genuinely unknowable."""
    rd = _seed_run(tmp_path)
    lens_pack = _lens_pack()
    core = _core(rd, lens_pack)
    store = EventStore(rd / "events.jsonl")
    terminal = store.append(EV_CONCEPT_LENS_COMPLETED, {
        "lens_request_id": ID_A, "generation": core[RUN_GENERATION_FIELD],
        "request_digest": DIGEST_A, "outcome": "abandoned", "reason": "operator_abandoned",
    })

    projected = lens.lens_terminal_response(terminal, core, lens_pack, ID_A)

    assert projected["abandoned"] is True and projected["resolved"] is True
    assert projected["provider_outcome"] == "unknown" and projected["billing_status"] == "unknown"


def test_an_unrecognised_error_kind_on_a_failed_terminal_is_coerced_not_echoed(tmp_path):
    """`_CONCEPT_LENS_SAFE_ERROR_KINDS` is an allow-list because the kind reaches the browser. A
    forged or future kind becomes `provider_error` rather than passing through."""
    rd = _seed_run(tmp_path)
    lens_pack = _lens_pack()
    core = _core(rd, lens_pack)
    store = EventStore(rd / "events.jsonl")
    terminal = store.append(EV_CONCEPT_LENS_FAILED, {
        "lens_request_id": ID_A, "generation": core[RUN_GENERATION_FIELD],
        "request_digest": DIGEST_A, "error_kind": "<script>whatever</script>",
    })

    projected = lens.lens_terminal_response(terminal, core, lens_pack, ID_A)

    assert projected["error_kind"] == "provider_error"
    assert projected["code"] == "concept_lens_failed" and projected["reason"] == "no_model"


def test_a_derived_spec_is_renamed_off_a_shipped_lens_and_refused_when_structurally_wrong():
    """Two rules of `validated_derived_lens`: a model may not silently redefine a shipped lens, and
    the retired string `root` is the ONE legacy field replay still tolerates."""
    lens_pack = _lens_pack()
    inputs = {"edges": [], "concept_ids": []}
    shipped = {item["name"] for item in lens_pack if isinstance(item, dict)}
    collide = lens.validated_derived_lens(
        {"name": sorted(shipped)[0], "rels": ["uses"]}, lens_pack, inputs)

    assert collide is not None and collide[0] not in shipped

    assert lens.validated_derived_lens(
        {"name": "usage", "rels": ["uses"], "root": "legacy"}, lens_pack, inputs) is not None
    assert lens.validated_derived_lens(
        {"name": "usage", "rels": ["uses"], "root": {"structured": True}}, lens_pack, inputs) is None
    assert lens.validated_derived_lens(
        {"name": "usage", "rels": "uses"}, lens_pack, inputs) is None


# ----------------------------------------------------------- the recovery command, no app

def test_recovery_reports_an_orphan_a_terminal_and_a_conflict_from_the_same_seeded_states(tmp_path):
    """The whole point of the extraction, in one test: three durable states that HTTP can only reach
    by racing a real worker, each read straight out of the command."""
    rd = _seed_run(tmp_path)
    store = EventStore(rd / "events.jsonl")
    generation = run_generation_token(store.read_all())
    lens_pack = _lens_pack()
    materialize = lambda *_a, **_k: _core(rd, lens_pack)   # noqa: E731
    started = _claim(store, ID_A, generation, DIGEST_A)

    # (1) a claim with no live process receipt: an orphan the operator may resolve.
    orphan = lens.durable_recover_concept_lens_receipt(
        _srv(rd, generation), "demo", Response(), generation, materialize_core=materialize)
    assert orphan["state"] == "orphaned"
    assert orphan["request_id"] == ID_A and orphan["started_seq"] == started.seq
    assert "request_digest" not in orphan and "prompt" not in orphan

    # (2) the same claim, but this process still has a running worker for it.
    live = _Jobs({ID_A: {"job_id": "0" * 16, "status": "running"}})
    running = lens.durable_recover_concept_lens_receipt(
        _srv(rd, generation, jobs=live), "demo", Response(), generation,
        materialize_core=materialize)
    assert running["state"] == "running" and running["job_id"] == "0" * 16

    # (3) resolved: the projection carries the terminal itself.
    store.append(EV_CONCEPT_LENS_COMPLETED, {
        "lens_request_id": ID_A, "generation": generation, "request_digest": DIGEST_A,
        "outcome": "declined", "reason": "declined",
    })
    resolved = lens.durable_recover_concept_lens_receipt(
        _srv(rd, generation), "demo", Response(), generation, materialize_core=materialize)
    assert resolved["state"] == "terminal"
    assert resolved["terminal"]["reason"] == "declined"

    # (4) a second overlapping claim: recovery disables itself rather than pick one.
    _claim(store, ID_B, generation, DIGEST_B)
    _claim(store, ID_B, generation, DIGEST_B)
    conflicted = lens.durable_recover_concept_lens_receipt(
        _srv(rd, generation), "demo", Response(), generation, materialize_core=materialize)
    assert conflicted["state"] == "conflict"
    assert conflicted["code"] == "concept_lens_recovery_conflict"


def test_recovery_refuses_a_generation_that_is_not_the_run_s_own(tmp_path):
    """Both halves of the fence, which `assert_lens_generation` keeps separate on purpose: the
    CALLER may be stale, and so may the server's own prepared projection."""
    rd = _seed_run(tmp_path)
    generation = run_generation_token(EventStore(rd / "events.jsonl").read_all())
    lens_pack = _lens_pack()
    materialize = lambda *_a, **_k: _core(rd, lens_pack)   # noqa: E731

    with pytest.raises(HTTPException) as malformed:
        lens.durable_recover_concept_lens_receipt(
            _srv(rd, generation), "demo", Response(), "not-a-generation",
            materialize_core=materialize)
    with pytest.raises(HTTPException) as moved:
        lens.durable_recover_concept_lens_receipt(
            _srv(rd, generation), "demo", Response(), "c" * 64, materialize_core=materialize)

    assert malformed.value.status_code == 400
    assert moved.value.status_code == 409
    assert moved.value.detail["code"] == "run_generation_changed"


# ----------------------------------------------------------- the seams the move creates

def test_the_lens_service_defines_no_route():
    """The bar every extracted serve service is held to: raising `HTTPException` is not HTTP-serving
    (`scope_actions.py` and `trace_clear.py` both do it), but a router object or a route decorator
    would make this a second router."""
    path, tree = next((p, t) for p, t in iter_trees() if p.name == "concept_lens_service.py")
    assert "APIRouter" not in path.read_text(encoding="utf-8-sig", errors="replace")
    decorators = {ast.dump(d) for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for d in n.decorator_list}
    assert not decorators, decorators


def test_each_of_the_four_lens_routes_is_one_delegating_call():
    """What SR-04 asked for. Over the AST, because `pass  # return await durable_…` satisfies a
    positive source pin while the endpoint does nothing at all."""
    tree = next(t for path, t in iter_trees()
                if path.name == "runs.py" and "routers" in path.parts)
    routes = {n.name: n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name, callee in (("derive_concept_lens", "durable_derive_concept_lens"),
                         ("recover_concept_lens_receipt", "durable_recover_concept_lens_receipt"),
                         ("abandon_recovered_concept_lens",
                          "durable_abandon_recovered_concept_lens"),
                         ("abandon_concept_lens", "durable_abandon_concept_lens")):
        body = [s for s in routes[name].body
                if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        assert len(body) == 1, f"{name} kept ledger case analysis: {len(body)} statements"
        call = body[0].value
        call = call.value if isinstance(call, ast.Await) else call
        assert call.func.id == callee
        assert [a.id for a in call.args][:2] == ["srv", "run_id"]
        # The materializer is HANDED IN, never re-derived: the unpaid `/concepts` GET shares it.
        assert [kw.arg for kw in call.keywords] == ["materialize_core"]


def test_the_router_holds_no_copy_of_the_lens_subsystem():
    """A router that re-defined any of these would read as extracted while every call still went to
    its own copy — the failure mode both earlier serve extractions had to guard against."""
    tree = next(t for path, t in iter_trees()
                if path.name == "runs.py" and "routers" in path.parts)
    defined = {n.name for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    for name in ("_concept_lens_identity", "_concept_lens_prompt_digest",
                 "_concept_lens_recovery_ledger", "_validated_derived_lens",
                 "_concept_lens_uncertain", "_concept_lens_terminal_response",
                 "_record_concept_lens_terminal", "_run_concept_lens_worker",
                 "_assert_lens_generation", "_concept_lens_json_body"):
        assert name not in defined, f"{name} is still defined in the router"
    assigned = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name)}
    assert "_CONCEPT_LENS_LEDGER" not in assigned, "a second PaidLedgerSpec for one protocol"


def test_the_materializer_stays_with_the_read_model_that_shares_it():
    """The one piece deliberately NOT moved. `GET /concepts` is unpaid and is its other consumer, so
    duplicating it here would give the paid and unpaid paths two different bounded folds."""
    tree = next(t for path, t in iter_trees()
                if path.name == "runs.py" and "routers" in path.parts)
    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "_materialize_concept_core" in defined
    service = next(t for path, t in iter_trees() if path.name == "concept_lens_service.py")
    assert not any(isinstance(n, ast.FunctionDef) and n.name == "_materialize_concept_core"
                   for n in ast.walk(service))
