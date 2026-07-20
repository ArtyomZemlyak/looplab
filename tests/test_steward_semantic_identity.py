from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from looplab.core.models import RunState
from looplab.engine.claim_steward import claim_curation_input_digest, claim_curation_snapshot
from looplab.engine.concept_steward import (
    CONCEPT_CURATION_INPUT_SCHEMA,
    concept_curation_input_digest,
    concept_curation_snapshot,
)
from looplab.engine.lessons import LessonMemory
from looplab.engine.memory import ConceptCapsuleStore, build_concept_capsule
from looplab.engine.task_facets import (
    load_task_facets,
    record_task_facets,
    task_facets_input_digest,
)


class _ToolClient:
    def __init__(self, payload: dict, *, model: str = "review-model") -> None:
        self.payload = payload
        self.model = model
        self.calls = 0

    def complete_tool(self, _messages, _schema):
        self.calls += 1
        return self.payload

    def complete_text(self, _messages):
        return "{}"


def _engine(memory_dir: Path, client=None):
    return SimpleNamespace(
        memory_dir=str(memory_dir),
        _cross_run_curation=True,
        _cross_run_curation_auto=False,
        researcher=SimpleNamespace(client=client, inner=None, fallback=None),
        developer=None,
        task=SimpleNamespace(kind="dataset"),
        settings=SimpleNamespace(llm_model="configured-model"),
    )


def _seed_concept(memory_dir: Path, concept: str = "retrieval/rerank", *, run_id: str = "seed"):
    ConceptCapsuleStore(memory_dir / "concept_capsules.jsonl").add(build_concept_capsule(
        run_id=run_id,
        task_id="task",
        fingerprint=["dataset"],
        direction="max",
        concepts=[concept],
        concept_outcomes={},
    ))


def _seed_claim(memory_dir: Path, statement: str = "reranking helps") -> None:
    (memory_dir / "lessons.jsonl").write_text(json.dumps({
        "statement": statement,
        "outcome": "supported",
        "evidence": [1],
        "run_id": "seed",
        "task_id": "task",
    }) + "\n", encoding="utf-8")


def _claim(statement: str) -> dict:
    return {
        "statement": statement,
        "scope": "task",
        "metric": "ndcg",
        "epistemic": "supported",
        "maturity": "machine-proposed",
        "n_support": 2,
        "n_oppose": 0,
        "support": ["run:1"],
    }


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_digest_identity_uses_visible_projection_and_ignores_hidden_tail():
    visible_concepts = [
        {"concept": f"concept-{i:03d}", "n_runs": 1, "runs": []}
        for i in range(200)
    ]
    concept_a = {"concepts": [*visible_concepts, {"concept": "hidden-a", "n_runs": 1}]}
    concept_b = {"concepts": [*visible_concepts, {"concept": "hidden-b", "n_runs": 999}]}
    assert concept_curation_input_digest(concept_a) == concept_curation_input_digest(concept_b)
    changed_visible = {"concepts": [dict(item) for item in concept_a["concepts"]]}
    changed_visible["concepts"][0]["n_runs"] = 2
    assert concept_curation_input_digest(concept_a) != concept_curation_input_digest(changed_visible)

    visible_claims = [_claim(f"claim-{i:03d}") for i in range(60)]
    claim_a = [*visible_claims, _claim("hidden-a")]
    claim_b = [*visible_claims, _claim("hidden-b")]
    assert claim_curation_input_digest(claim_a) == claim_curation_input_digest(claim_b)
    changed_claim = [dict(item) for item in claim_a]
    changed_claim[0]["statement"] = "visible-change"
    assert claim_curation_input_digest(claim_a) != claim_curation_input_digest(changed_claim)


def test_same_snapshot_across_runs_bills_once_and_visible_change_reopens(tmp_path):
    _seed_concept(tmp_path)
    client = _ToolClient({"merges": [], "splits": [], "purges": []})
    memory = LessonMemory(_engine(tmp_path, client))

    memory.store_concept_curation(RunState(run_id="run-a", task_id="task", last_finish_seq=10))
    memory.store_concept_curation(RunState(run_id="run-b", task_id="task", last_finish_seq=20))
    assert client.calls == 1

    _seed_concept(tmp_path, "retrieval/hybrid", run_id="seed-2")
    memory.store_concept_curation(RunState(run_id="run-c", task_id="task", last_finish_seq=30))
    assert client.calls == 2
    rows = _rows(tmp_path / "concept_curation_log.jsonl")
    assert len(rows) == 2 and len({row["curation_key"] for row in rows}) == 2


def test_claim_paid_snapshot_fences_evidence_mutation_through_digest(tmp_path, monkeypatch):
    import looplab.engine.claim_steward as steward_module
    from looplab.events.eventstore import _interprocess_lock

    _seed_claim(tmp_path, "evidence before snapshot")
    path = tmp_path / "lessons.jsonl"
    started, landed = Event(), Event()
    original_digest = steward_module.claim_curation_input_digest

    def mutate():
        started.set()
        with _interprocess_lock(Path(str(path) + ".lock"), required=True):
            _seed_claim(tmp_path, "evidence after snapshot")
            landed.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        mutation = None

        def observe_digest(claims, **kwargs):
            nonlocal mutation
            mutation = executor.submit(mutate)
            assert started.wait(5)
            assert not landed.wait(0.1)
            return original_digest(claims, **kwargs)

        monkeypatch.setattr(steward_module, "claim_curation_input_digest", observe_digest)
        claims, digest = claim_curation_snapshot(tmp_path)
        assert mutation is not None
        mutation.result(timeout=5)

    assert landed.is_set()
    assert any(row["statement"] == "evidence before snapshot" for row in claims)
    assert digest == original_digest(claims)


def test_concept_paid_snapshot_fences_capsule_mutation_through_digest(tmp_path, monkeypatch):
    import looplab.engine.concept_steward as steward_module
    from looplab.events.eventstore import _interprocess_lock

    _seed_concept(tmp_path, "retrieval/before")
    path = tmp_path / "concept_capsules.jsonl"
    started, landed = Event(), Event()
    original_digest = steward_module.concept_curation_input_digest
    later = build_concept_capsule(
        run_id="later", task_id="task", fingerprint=["dataset"], direction="max",
        concepts=["retrieval/after"], concept_outcomes={})

    def mutate():
        started.set()
        with _interprocess_lock(Path(str(path) + ".lock"), required=True):
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(later) + "\n")
            landed.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        mutation = None

        def observe_digest(overview, **kwargs):
            nonlocal mutation
            mutation = executor.submit(mutate)
            assert started.wait(5)
            assert not landed.wait(0.1)
            return original_digest(overview, **kwargs)

        monkeypatch.setattr(steward_module, "concept_curation_input_digest", observe_digest)
        overview, digest = concept_curation_snapshot(tmp_path)
        assert mutation is not None
        mutation.result(timeout=5)

    assert landed.is_set()
    assert {row["concept"] for row in overview["concepts"]} == {"retrieval/before"}
    assert digest == original_digest(overview)


def test_claim_snapshot_is_frozen_from_digest_through_proposal(tmp_path, monkeypatch):
    frozen = [_claim("frozen statement")]
    expected_digest = claim_curation_input_digest(frozen)
    _seed_claim(tmp_path, "original disk statement")
    client = _ToolClient({"decisions": []})
    memory = LessonMemory(_engine(tmp_path, client))

    monkeypatch.setattr(
        "looplab.engine.claim_steward.claim_curation_snapshot",
        lambda *_args, **_kwargs: (frozen, expected_digest),
    )

    def mutate_then_return_client():
        _seed_claim(tmp_path, "mutated after snapshot")
        return client

    memory.reflect_client = mutate_then_return_client

    def propose(received, *_args, **_kwargs):
        assert received is frozen
        assert claim_curation_input_digest(received) == expected_digest
        return {"decisions": []}

    monkeypatch.setattr("looplab.engine.claim_steward.propose_claim_curation", propose)
    final = RunState(run_id="run-frozen", task_id="task", last_finish_seq=17)
    memory.store_claim_curation(final)

    terminal = _rows(tmp_path / "claim_curation_log.jsonl")[0]
    claim = json.loads(next((tmp_path / ".curation_invocations").glob("*.json")).read_text())
    assert terminal["input_digest"] == claim["input_digest"] == expected_digest
    assert terminal["curation_key"] == claim["curation_key"]


def test_unavailable_is_retryable_but_cannot_append_after_terminal(tmp_path):
    _seed_concept(tmp_path)
    first = LessonMemory(_engine(tmp_path, None))
    first.store_concept_curation(RunState(
        run_id="run-unavailable", task_id="task", last_finish_seq=1))
    assert _rows(tmp_path / "concept_curation_log.jsonl")[0]["outcome"] == "unavailable"

    client = _ToolClient({"merges": [], "splits": [], "purges": []})
    second = LessonMemory(_engine(tmp_path, client))
    second.store_concept_curation(RunState(
        run_id="run-available", task_id="task", last_finish_seq=2))
    assert client.calls == 1
    before = _rows(tmp_path / "concept_curation_log.jsonl")
    assert [row["outcome"] for row in before] == ["unavailable", "empty"]

    # Simulate a stale no-client process losing the append-lock race to the terminal writer.
    terminal = before[-1]
    appended = first._append_curation_once(
        "concept_curation_log.jsonl",
        RunState(run_id="run-stale", task_id="task", last_finish_seq=3),
        terminal["curation_key"],
        {key: terminal[key] for key in ("input_digest", "input_schema", "model", "parser")},
        {
            "outcome": "unavailable", "auto": False, "auto_requested": False,
            "proposals": {"merges": [], "splits": [], "purges": []}, "receipt": None,
        },
    )
    assert appended is False
    assert _rows(tmp_path / "concept_curation_log.jsonl") == before


def test_legacy_v1_claim_suppresses_only_exact_run_without_v2_materialization(
        tmp_path, monkeypatch):
    _seed_concept(tmp_path)
    client = _ToolClient({"merges": [], "splits": [], "purges": []})
    memory = LessonMemory(_engine(tmp_path, client))
    exact = RunState(run_id="legacy-run", task_id="task", last_finish_seq=7)
    legacy_path = memory._legacy_curation_claim_path("concept_curation_log.jsonl", exact)
    assert legacy_path is not None
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text("{}\n", encoding="utf-8")

    calls: list[int] = []

    def propose(*_args, **_kwargs):
        calls.append(1)
        return {"merges": [], "splits": [], "purges": []}

    monkeypatch.setattr("looplab.engine.concept_steward.propose_concept_curation", propose)
    overview, digest = concept_curation_snapshot(tmp_path)
    assert overview["concepts"]
    semantic_path = memory._curation_claim_path(
        "concept_curation_log.jsonl", memory._portfolio_curation_key("concept", digest))

    memory.store_concept_curation(exact)
    assert calls == []
    assert not semantic_path.exists()
    assert not (tmp_path / "concept_curation_log.jsonl").exists()

    memory.store_concept_curation(RunState(
        run_id="different-run", task_id="task", last_finish_seq=7))
    assert calls == [1] and semantic_path.exists()


@pytest.mark.parametrize("already_governed", [False, True])
def test_legacy_facets_claim_suppresses_fast_terminal_without_v2_materialization(
        tmp_path, already_governed):
    memory = LessonMemory(_engine(tmp_path, _ToolClient({"facets": {"domain": "other"}})))
    memory.reflect_client = lambda: (_ for _ in ()).throw(AssertionError("provider initialized"))
    final = RunState(
        run_id="legacy-facets", task_id="task",
        goal="changed goal" if already_governed else "   ", last_finish_seq=9)
    if already_governed:
        record_task_facets(
            str(tmp_path), task_id="task", facets={"domain": "operator"}, by="operator")
    legacy_path = memory._legacy_curation_claim_path(
        "task_facets_curation_log.jsonl", final)
    assert legacy_path is not None
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text("{}\n", encoding="utf-8")
    semantic_path = memory._curation_claim_path(
        "task_facets_curation_log.jsonl", memory._facets_curation_key("task"))

    memory.store_task_facets(final)

    assert legacy_path.exists()
    assert not semantic_path.exists()
    assert not (tmp_path / "task_facets_curation_log.jsonl").exists()


def test_empty_inputs_do_not_initialize_provider_or_create_paid_claim(tmp_path):
    memory = LessonMemory(_engine(tmp_path, _ToolClient({})))
    memory.reflect_client = lambda: (_ for _ in ()).throw(AssertionError("provider initialized"))

    memory.store_concept_curation(RunState(run_id="empty-c", task_id="task"))
    memory.store_claim_curation(RunState(run_id="empty-q", task_id="task"))
    memory.store_task_facets(RunState(run_id="empty-f", task_id="task", goal="   "))

    assert _rows(tmp_path / "concept_curation_log.jsonl")[0]["outcome"] == "empty"
    assert _rows(tmp_path / "claim_curation_log.jsonl")[0]["outcome"] == "empty"
    assert _rows(tmp_path / "task_facets_curation_log.jsonl")[0]["outcome"] == "empty"
    assert not list((tmp_path / ".curation_invocations").glob("*.json"))


def test_lost_terminal_recovery_uses_original_claim_source_and_model(tmp_path, monkeypatch):
    _seed_concept(tmp_path)
    first_client = _ToolClient(
        {"merges": [], "splits": [], "purges": []}, model="model-a")
    first_engine = _engine(tmp_path, first_client)
    first_engine._cross_run_curation_auto = True
    first = LessonMemory(first_engine)

    def hard_loss(*_args, **_kwargs):
        raise SystemExit("lost after paid call")

    monkeypatch.setattr(first, "_append_curation_once", hard_loss)
    with pytest.raises(SystemExit):
        first.store_concept_curation(RunState(
            run_id="run-a", task_id="task-a", last_finish_seq=11))
    assert first_client.calls == 1
    claim_path = next((tmp_path / ".curation_invocations").glob("*.json"))
    claim = json.loads(claim_path.read_text(encoding="utf-8"))

    second_client = _ToolClient(
        {"merges": [], "splits": [], "purges": []}, model="model-b")
    second = LessonMemory(_engine(tmp_path, second_client))
    second.store_concept_curation(RunState(
        run_id="run-b", task_id="task-b", last_finish_seq=22))

    assert second_client.calls == 0
    terminal = _rows(tmp_path / "concept_curation_log.jsonl")[0]
    assert terminal["outcome"] == "prior_attempt_incomplete_not_replayed"
    for field in (
            "curation_key", "source_key", "run_id", "task_id", "finish_seq",
            "input_digest", "input_schema", "model", "parser"):
        assert terminal[field] == claim[field]
    assert terminal["model"] == "model-a"
    assert terminal["run_id"] == "run-a"
    assert claim["auto_requested"] is terminal["auto_requested"] is True
    assert claim["auto"] is terminal["auto"] is False


def test_facets_lost_terminal_keeps_original_visible_snapshot_across_runs(
        tmp_path, monkeypatch):
    first_client = _ToolClient({"facets": {"domain": "retrieval"}}, model="facets-model-a")
    first = LessonMemory(_engine(tmp_path, first_client))

    def hard_loss(*_args, **_kwargs):
        raise SystemExit("lost after paid call")

    monkeypatch.setattr(first, "_append_curation_once", hard_loss)
    with pytest.raises(SystemExit):
        first.store_task_facets(RunState(
            run_id="facets-run-a", task_id="same-task", goal="rank documents",
            last_finish_seq=31))
    claim_path = next((tmp_path / ".curation_invocations").glob("*.json"))
    claim = json.loads(claim_path.read_text(encoding="utf-8"))
    assert claim["input_digest"] == task_facets_input_digest("rank documents", "dataset")
    # Parser/schema are provenance rather than the once/task identity. A newer retry must close an
    # older paid claim with its original values, even when its own defaults have changed.
    claim["input_schema"] = "finalize-task-facets/v0"
    claim["parser"] = "legacy-tool-call"
    claim_path.write_text(json.dumps(claim, sort_keys=True) + "\n", encoding="utf-8")

    second_client = _ToolClient({"facets": {"domain": "other"}}, model="facets-model-b")
    second = LessonMemory(_engine(tmp_path, second_client))
    second.store_task_facets(RunState(
        run_id="facets-run-b", task_id="same-task", goal="   ",
        last_finish_seq=32))

    assert first_client.calls == 1
    assert second_client.calls == 0
    terminal = _rows(tmp_path / "task_facets_curation_log.jsonl")[0]
    for field in (
            "curation_key", "source_key", "run_id", "task_id", "finish_seq",
            "input_digest", "input_schema", "model", "parser"):
        assert terminal[field] == claim[field]
    assert terminal["input_digest"] != task_facets_input_digest(
        "   ", "dataset")


def test_foreign_paid_claim_fails_closed_without_current_run_ambiguity(tmp_path, monkeypatch):
    _seed_concept(tmp_path)
    first = LessonMemory(_engine(
        tmp_path, _ToolClient({"merges": [], "splits": [], "purges": []}, model="model-a")))

    def hard_loss(*_args, **_kwargs):
        raise SystemExit("lost after paid call")

    monkeypatch.setattr(first, "_append_curation_once", hard_loss)
    with pytest.raises(SystemExit):
        first.store_concept_curation(RunState(
            run_id="run-a", task_id="task", last_finish_seq=1))
    claim_path = next((tmp_path / ".curation_invocations").glob("*.json"))
    claim = json.loads(claim_path.read_text(encoding="utf-8"))
    claim["model"] = "sk-" + ("a" * 48)
    claim_path.write_text(json.dumps(claim, sort_keys=True) + "\n", encoding="utf-8")

    retry_client = _ToolClient(
        {"merges": [], "splits": [], "purges": []}, model="model-b")
    retry = LessonMemory(_engine(tmp_path, retry_client))
    retry.store_concept_curation(RunState(
        run_id="run-b", task_id="task", last_finish_seq=2))

    assert retry_client.calls == 0
    rows = _rows(tmp_path / "concept_curation_log.jsonl")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "error"
    assert rows[0]["curation_key"].startswith("concept:diagnostic:v2:")
    assert rows[0]["run_id"] == "run-b"
    assert "sk-" + ("a" * 48) not in json.dumps(rows)
    assert all(row.get("outcome") != "prior_attempt_incomplete_not_replayed" for row in rows)


def test_already_governed_facets_are_checked_before_provider_and_once_per_task(tmp_path):
    record_task_facets(
        str(tmp_path), task_id="task", facets={"domain": "retrieval"}, by="operator")
    memory = LessonMemory(_engine(tmp_path, _ToolClient({"facets": {"domain": "other"}})))
    memory.reflect_client = lambda: (_ for _ in ()).throw(AssertionError("provider initialized"))

    memory.store_task_facets(RunState(run_id="run-a", task_id="task", goal="goal-a"))
    memory.store_task_facets(RunState(run_id="run-b", task_id="task", goal="goal-b"))

    rows = _rows(tmp_path / "task_facets_curation_log.jsonl")
    assert len(rows) == 1 and rows[0]["outcome"] == "already-governed"
    assert rows[0]["proposals"]["facets"] == load_task_facets(str(tmp_path))["task"]
    assert not list((tmp_path / ".curation_invocations").glob("*.json"))


def test_facets_are_billed_once_per_exact_task_id(tmp_path):
    client = _ToolClient({"facets": {"domain": "retrieval"}})
    memory = LessonMemory(_engine(tmp_path, client))
    memory.store_task_facets(RunState(run_id="run-a", task_id="task-a", goal="goal-a"))
    memory.store_task_facets(RunState(run_id="run-b", task_id="task-a", goal="changed goal"))
    memory.store_task_facets(RunState(run_id="run-c", task_id="task-b", goal="goal-a"))
    assert client.calls == 2
    rows = _rows(tmp_path / "task_facets_curation_log.jsonl")
    assert len(rows) == 2 and len({row["curation_key"] for row in rows}) == 2


def test_claim_and_terminal_record_exact_source_and_effective_provenance(tmp_path):
    _seed_concept(tmp_path)
    client = _ToolClient({"merges": [], "splits": [], "purges": []}, model="model-exact")
    memory = LessonMemory(_engine(tmp_path, client))
    final = RunState(run_id="run-exact", task_id="task-exact", last_finish_seq=23)
    memory.store_concept_curation(final)

    terminal = _rows(tmp_path / "concept_curation_log.jsonl")[0]
    claim = json.loads(next((tmp_path / ".curation_invocations").glob("*.json")).read_text())
    for row in (claim, terminal):
        assert row["run_id"] == "run-exact"
        assert row["task_id"] == "task-exact"
        assert row["finish_seq"] == 23
        assert row["model"] == "model-exact"
        assert row["parser"] == "tool_call_once"
        assert row["input_schema"] == CONCEPT_CURATION_INPUT_SCHEMA
    assert claim["source_key"] == terminal["source_key"]
    assert claim["curation_key"] == terminal["curation_key"]
    assert claim["input_digest"] == terminal["input_digest"]


def test_source_identity_never_falls_back_between_run_task_or_finish():
    states = [
        RunState(run_id="run", task_id="task", last_finish_seq=1),
        RunState(run_id="run", task_id="task", last_finish_seq=2),
        RunState(run_id="run", task_id="other", last_finish_seq=1),
        RunState(run_id="other", task_id="task", last_finish_seq=1),
        RunState(run_id="", task_id="run", last_finish_seq=1),
    ]
    keys = {LessonMemory._curation_source_key(state) for state in states}
    assert len(keys) == len(states)


def test_v2_run_never_creates_a_run_keyed_legacy_lock(tmp_path):
    # Regression: `_interprocess_lock` opens (creates) a `<name>.lock` and never unlinks it. The legacy
    # (v1) claim path is keyed by the UNIQUE run_id, so acquiring its lock unconditionally accreted one
    # orphan lock per run in `.curation_invocations/` forever — an unbounded disk/inode leak. A v2-only
    # run (no pre-existing legacy claim) must not create that lock at all.
    memory = LessonMemory(_engine(tmp_path, _ToolClient({})))
    memory.reflect_client = lambda: (_ for _ in ()).throw(AssertionError("provider initialized"))

    for run in ("v2-a", "v2-b", "v2-c"):
        final = RunState(run_id=run, task_id="task", goal="   ")
        legacy = memory._legacy_curation_claim_path("concept_curation_log.jsonl", final)
        memory.store_concept_curation(final)
        assert legacy is not None
        assert not Path(str(legacy) + ".lock").exists(), run
    # And no run-keyed legacy lock survives across the three distinct runs.
    scratch = tmp_path / ".curation_invocations"
    lock_digests = {p.name for p in scratch.glob("*.json.lock")} if scratch.exists() else set()
    for run in ("v2-a", "v2-b", "v2-c"):
        legacy = memory._legacy_curation_claim_path(
            "concept_curation_log.jsonl", RunState(run_id=run, task_id="task"))
        assert Path(legacy).name + ".lock" not in lock_digests, run


def test_prune_curation_scratch_bounds_orphan_locks(tmp_path, monkeypatch):
    import os
    import time

    from looplab.engine import lessons as _lessons

    memory = LessonMemory(_engine(tmp_path))
    scratch = tmp_path / ".curation_invocations"
    scratch.mkdir(parents=True)
    monkeypatch.setattr(_lessons, "_CURATION_SCRATCH_MAX_ENTRIES", 8)
    old = time.time() - (_lessons._CURATION_SCRATCH_MIN_AGE_S + 3600)

    # 20 OLD orphan locks (no `.json` claim) — all prunable back down to the cap.
    for i in range(20):
        p = scratch / f"orphan{i:02d}.json.lock"
        p.write_text("", encoding="utf-8")
        os.utime(p, (old, old))
    # A FRESH orphan lock — an in-flight decision on another process may still hold it; keep it.
    fresh = scratch / "fresh.json.lock"
    fresh.write_text("", encoding="utf-8")
    # A lock PAIRED with a live recovery claim — never pruned regardless of age.
    paired_claim = scratch / "paired.json"
    paired_claim.write_text("{}", encoding="utf-8")
    paired_lock = scratch / "paired.json.lock"
    paired_lock.write_text("", encoding="utf-8")
    os.utime(paired_lock, (old, old))

    memory._prune_curation_scratch(scratch)

    remaining = {p.name for p in scratch.iterdir()}
    assert fresh.name in remaining          # too young to prune
    assert paired_claim.name in remaining   # durable recovery claim untouched
    assert paired_lock.name in remaining    # paired with a live claim
    assert len(remaining) <= _lessons._CURATION_SCRATCH_MAX_ENTRIES + 2  # + the two intentional keeps
