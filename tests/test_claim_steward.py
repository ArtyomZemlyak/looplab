"""AGENTIC claim steward (§22.4) — the LLM counterpart of the operator's manual claim decisions. Pins:
the LLM PROPOSES ratify/reject/pin over the evidence-grounded claims, guardrails validate (only listed
claims, valid decisions, scope-precise), the apply path records through record_claim_decision, and it
degrades to empty (no client / bad output / already-decided) so it never corrupts governance.
"""
from __future__ import annotations

from looplab.engine.claim_steward import (
    apply_claim_curation, curation_is_empty, propose_claim_curation, steward_claims,
)


class _Client:
    def __init__(self, decisions):
        self._d = {"decisions": decisions}

    def complete_tool(self, messages, json_schema):
        return self._d

    def complete_text(self, messages):
        return "{}"


def _claim(statement, *, epistemic="supported", n_support=2, n_oppose=0, scopes=("t",),
           maturity="machine-proposed"):
    return {"statement": statement, "epistemic": epistemic, "n_support": n_support,
            "n_oppose": n_oppose, "scopes": list(scopes), "maturity": maturity}


def test_no_client_is_empty():
    assert curation_is_empty(propose_claim_curation([_claim("x helps")], None))


def test_llm_decisions_validated_against_listed_claims():
    claims = [_claim("hard-neg helps recall"), _claim("distillation is noise", epistemic="inconclusive")]
    client = _Client([
        {"statement": "hard-neg helps recall", "decision": "ratified", "why": "well evidenced"},
        {"statement": "distillation is noise", "decision": "rejected"},
        {"statement": "a claim that was never listed", "decision": "ratified"},   # dropped (unknown)
        {"statement": "hard-neg helps recall", "decision": "bogus"},              # dropped (bad decision)
    ])
    prop = propose_claim_curation(claims, client)
    got = {d["statement"]: d["decision"] for d in prop["decisions"]}
    assert got == {"hard-neg helps recall": "ratified", "distillation is noise": "rejected"}
    # scope is taken from the matching claim (scope-precise governance), not the model
    assert all(d["scope"] == "t" for d in prop["decisions"])


def test_decision_does_not_leak_across_same_worded_claims_in_different_scopes():
    # mega-review regression: two tasks, byte-identical claim text; a decision must route to the SCOPE the
    # LLM named, never collapse to one arbitrary scope.
    claims = [_claim("hard negatives improve recall", scopes=("rubert",)),
              _claim("hard negatives improve recall", scopes=("e5",))]
    client = _Client([{"statement": "hard negatives improve recall", "decision": "ratified", "scope": "e5"}])
    prop = propose_claim_curation(claims, client)
    assert len(prop["decisions"]) == 1 and prop["decisions"][0]["scope"] == "e5"   # routed to e5, not rubert


def test_ambiguous_scope_without_disambiguation_is_skipped():
    # same text in two scopes, LLM gives no scope -> cannot route safely -> skipped (never misrouted)
    claims = [_claim("x helps", scopes=("a",)), _claim("x helps", scopes=("b",))]
    prop = propose_claim_curation(claims, _Client([{"statement": "x helps", "decision": "rejected"}]))
    assert curation_is_empty(prop)


def test_already_decided_claims_are_not_re_litigated():
    claims = [_claim("operator ratified this", maturity="operator-ratified")]
    # even if the model proposes a decision, there is nothing REVIEWABLE -> empty (no re-litigation)
    assert curation_is_empty(propose_claim_curation(claims, _Client(
        [{"statement": "operator ratified this", "decision": "rejected"}])))


def test_bad_output_degrades_to_empty():
    class _Boom:
        def complete_tool(self, m, j):
            raise RuntimeError("boom")

        def complete_text(self, m):
            raise RuntimeError("no")

    assert curation_is_empty(propose_claim_curation([_claim("x helps")], _Boom()))


def test_apply_records_scope_precise_decisions(tmp_path):
    from looplab.engine.claims import load_claim_decisions
    from looplab.engine.claim_key import claim_uid
    rc = apply_claim_curation(str(tmp_path), {"decisions": [
        {"statement": "adapter tuning helps", "decision": "rejected", "scope": "taskA", "why": "overgeneralized"}]})
    assert len(rc["applied"]) == 1 and not rc["skipped"]
    dec = load_claim_decisions(str(tmp_path))
    # keyed by the structured scope-precise claim_uid (and the legacy statement key)
    assert dec[claim_uid("adapter tuning helps", scope="taskA")]["decision"] == "rejected"


def test_steward_end_to_end_apply(tmp_path):
    import orjson
    from looplab.engine.claims import claims_for_memory
    (tmp_path / "lessons.jsonl").write_bytes(orjson.dumps(
        {"statement": "hybrid retrieval helps", "outcome": "supported", "evidence": [1],
         "run_id": "r1", "task_id": "t"}) + b"\n")
    client = _Client([{"statement": "hybrid retrieval helps", "decision": "ratified"}])
    out = steward_claims(str(tmp_path), client, apply=True)
    assert out["receipt"]["applied"]
    # the ratification now shows on the claims read-model (structured, scope-precise)
    got = {c["statement"]: c["maturity"] for c in claims_for_memory(str(tmp_path), structured=True)}
    assert got["hybrid retrieval helps"] == "operator-ratified"


def test_finalize_claim_curation_gating(tmp_path):
    import orjson
    from types import SimpleNamespace
    from looplab.engine.lessons import LessonMemory
    from looplab.core.models import RunState
    (tmp_path / "lessons.jsonl").write_bytes(orjson.dumps(
        {"statement": "reranking helps", "outcome": "supported", "evidence": [1],
         "run_id": "r1", "task_id": "t"}) + b"\n")
    client = _Client([{"statement": "reranking helps", "decision": "ratified"}])

    def _eng(on, auto=False):
        return SimpleNamespace(memory_dir=str(tmp_path), _cross_run_curation=on, _cross_run_curation_auto=auto,
                               researcher=SimpleNamespace(client=client, inner=None, fallback=None), developer=None)
    # off -> nothing
    LessonMemory(_eng(False)).store_claim_curation(RunState(run_id="r", task_id="t"))
    assert not (tmp_path / "claim_curation_log.jsonl").exists()
    # on, auto -> logged AND applied
    LessonMemory(_eng(True, auto=True)).store_claim_curation(RunState(run_id="r", task_id="t"))
    assert (tmp_path / "claim_curation_log.jsonl").exists()
    from looplab.engine.claims import claims_for_memory
    got = {c["statement"]: c["maturity"] for c in claims_for_memory(str(tmp_path), structured=True)}
    assert got["reranking helps"] == "operator-ratified"


def test_cli_claim_steward(tmp_path, monkeypatch):
    import orjson
    from typer.testing import CliRunner
    from looplab.cli import app
    import looplab.cli as cli
    (tmp_path / "lessons.jsonl").write_bytes(orjson.dumps(
        {"statement": "warmup stabilizes training", "outcome": "supported", "evidence": [1],
         "run_id": "r1", "task_id": "t"}) + b"\n")
    monkeypatch.setattr(cli, "make_llm_client", lambda *a, **k: _Client(
        [{"statement": "warmup stabilizes training", "decision": "ratified", "why": "consistent"}]))
    r = CliRunner().invoke(app, ["claim-steward", str(tmp_path)])
    assert r.exit_code == 0 and "ratified" in r.stdout and "dry run" in r.stdout
    r2 = CliRunner().invoke(app, ["claim-steward", str(tmp_path), "--apply"])
    assert r2.exit_code == 0 and "applied 1" in r2.stdout
