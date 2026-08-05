"""PART V §22.4 — the claim REVIEW projection and the claim DECISION projection must be the same one.

`claim_evidence_digest` commits the projection's whole-source health receipt (snapshot digest,
producer-run counts, quarantine counters) alongside the claim's own evidence, so the digest is a
property of the PROJECTION, not of the claim. `record_observed_claim_decision` always validates against
`claims_for_memory(..., scope_task=<the decision's scope>)`. Therefore any surface that hands an
operator an `evidence_digest` for `claim-decide` has to project at the SAME scope.

The CLI shipped without that knob: `claims` projected portfolio-wide and had no `--scope` at all, so for
every task-scoped claim — which is every engine-produced claim — the documented
`--structured --json --governance-receipt` review path produced a digest that could never match, and
`claim-decide` exited 2 with `claim_evidence_changed`. Every pre-existing test missed it because their
fixtures seed ONE task (`task_id="t"`), where scope filtering is a no-op.

These tests pin both halves: the workflow must work at matching scope, and it must still REFUSE when the
evidence really moved — including when only the source-COMPLETENESS receipt moved, which is the fence a
"just make the digest scope-independent" fix would have silently removed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from looplab.cli import app


def _lesson(statement, *, run_id, task_id, evidence, outcome="supported"):
    return {"statement": statement, "outcome": outcome, "evidence": evidence,
            "run_id": run_id, "task_id": task_id, "direction": "max"}


def _write(path: Path, rows) -> None:
    path.write_bytes(b"".join(orjson.dumps(row) + b"\n" for row in rows))


def _portfolio(tmp_path: Path) -> Path:
    """A REAL multi-task portfolio: cross-run memory accumulates every run, so more than one task_id
    is the normal shape, not an edge case. One task is the degenerate case that hid this defect."""
    md = tmp_path / "memory"
    md.mkdir(parents=True, exist_ok=True)
    _write(md / "lessons.jsonl", [
        _lesson("hard-neg mining lifts recall", run_id="a1", task_id="task-a", evidence=[1]),
        _lesson("hard-neg mining lifts recall", run_id="a2", task_id="task-a", evidence=[2]),
        _lesson("gradient boosting beats linear here", run_id="b1", task_id="task-b", evidence=[1]),
    ])
    return md


def _review(md: Path, *, scope=None, extra=()):
    argv = ["claims", str(md), "--structured", "--json", "--governance-receipt", *extra]
    if scope is not None:
        argv += ["--scope", scope]
    result = CliRunner().invoke(app, argv)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _decide(md: Path, claim, revision, *, scope, action_id, decision="--ratify", digest=None):
    return CliRunner().invoke(app, [
        "claim-decide", str(md), claim["statement"], decision,
        "--claim-uid", claim["claim_uid"],
        "--evidence-digest", digest if digest is not None else claim["evidence_digest"],
        "--expected-revision", str(revision), "--action-id", action_id, "--scope", scope,
    ])


def _pick(payload, scope):
    rows = [c for c in payload["claims"] if c["scope"] == scope]
    assert rows, f"no claim scoped to {scope!r} in {[c['scope'] for c in payload['claims']]}"
    return rows[0]


def test_portfolio_wide_review_cannot_decide_a_scoped_claim(tmp_path):
    """The reported outage, kept as a regression: a receipt taken from the portfolio-wide projection
    is refused by the scoped validator. This must stay a REFUSAL — the digest is doing its job; what
    was missing was any way to obtain the matching one."""
    md = _portfolio(tmp_path)
    wide = _review(md)
    assert wide["scope"] == ""                      # the receipt names the projection it describes
    claim = _pick(wide, "task-a")

    refused = _decide(md, claim, wide["revision"], scope="task-a", action_id="wide-review")
    assert refused.exit_code == 2
    assert "claim_evidence_changed" in refused.output
    # ...and the refusal now distinguishes "you reviewed the wrong projection" from "evidence moved".
    assert "--scope" in refused.output and "'task-a'" in refused.output
    assert not (md / "claim_decisions.jsonl").exists()


def test_portfolio_wide_receipt_warns_where_it_is_handed_out(tmp_path):
    """The refusal comes two commands later, so the review surface says it up front. On stderr, so the
    JSON on stdout stays machine-parseable."""
    md = _portfolio(tmp_path)
    result = CliRunner().invoke(app, [
        "claims", str(md), "--structured", "--json", "--governance-receipt"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)              # stdout is still pure JSON
    assert payload["scope"] == "" and len(payload["claims"]) == 2
    assert "PORTFOLIO-WIDE" in result.stderr
    assert "task-a" in result.stderr and "task-b" in result.stderr

    scoped = CliRunner().invoke(app, [
        "claims", str(md), "--structured", "--json", "--governance-receipt", "--scope", "task-a"])
    assert scoped.exit_code == 0 and scoped.stderr == ""


def test_documented_workflow_succeeds_at_matching_scope(tmp_path):
    md = _portfolio(tmp_path)
    scoped = _review(md, scope="task-a")
    assert scoped["scope"] == "task-a"
    assert {c["scope"] for c in scoped["claims"]} == {"task-a"}
    claim = _pick(scoped, "task-a")

    accepted = _decide(md, claim, scoped["revision"], scope="task-a", action_id="scoped-review")
    assert accepted.exit_code == 0, accepted.output
    assert "ratified" in accepted.stdout

    listed = CliRunner().invoke(app, ["claims", str(md), "--scope", "task-a"])
    assert listed.exit_code == 0 and "[RATIFIED]" in listed.stdout


def test_same_task_evidence_change_is_still_refused(tmp_path):
    """The property the digest exists for. A fix that made the receipt match by loosening the binding
    would accept this write."""
    md = _portfolio(tmp_path)
    scoped = _review(md, scope="task-a")
    claim = _pick(scoped, "task-a")

    with (md / "lessons.jsonl").open("ab") as stream:
        stream.write(orjson.dumps(
            _lesson("hard-neg mining lifts recall", run_id="a3", task_id="task-a",
                    evidence=[7], outcome="tested")) + b"\n")

    stale = _decide(md, claim, scoped["revision"], scope="task-a", action_id="stale-same-task")
    assert stale.exit_code == 2 and "claim_evidence_changed" in stale.output
    assert not (md / "claim_decisions.jsonl").exists()


def test_source_completeness_change_alone_still_refuses(tmp_path):
    """The sharpest form of the preserved property: the claim's OWN evidence is byte-identical and only
    the whole-source health receipt moved (a row quarantined ⇒ retained evidence is now a lower bound
    whose omitted tail may carry the other side). Dropping `research_source`/`claim_source` from
    `claim_evidence_digest` — the obvious way to make scoped and unscoped digests agree — would ratify
    this write against a source that can no longer support an exact verdict."""
    md = _portfolio(tmp_path)
    (md / "research_claims.jsonl").write_bytes(orjson.dumps({
        "v": 3, "statement": "boosting generalizes on this family", "task_id": "task-a",
        "run_id": "a1", "direction": "max", "node_refs": [{"node_id": 1, "generation": 0}],
    }) + b"\n")
    scoped = _review(md, scope="task-a")
    claim = _pick(scoped, "task-a")

    with (md / "research_claims.jsonl").open("ab") as stream:
        stream.write(b'{"v": 3, "statement": ')       # a truncated row: source becomes PARTIAL

    after = _pick(_review(md, scope="task-a"), "task-a")
    assert after["claim_uid"] == claim["claim_uid"]
    for field in ("support", "oppose", "unverified", "runs", "scopes", "epistemic"):
        assert after[field] == claim[field], field   # the claim's own evidence did not move
    assert after["evidence_digest"] != claim["evidence_digest"]

    refused = _decide(md, claim, scoped["revision"], scope="task-a", action_id="partial-source")
    assert refused.exit_code == 2 and "claim_evidence_changed" in refused.output


def test_unrelated_task_rewrite_does_not_fence_a_scoped_decision(tmp_path):
    """Scoping the review must not over-fence either: another task's rows are not this decision's
    evidence. This is the CLI twin of `test_scoped_claim_decision_ignores_other_task_rewrite...`."""
    md = _portfolio(tmp_path)
    scoped = _review(md, scope="task-a")
    claim = _pick(scoped, "task-a")

    with (md / "lessons.jsonl").open("ab") as stream:
        stream.write(orjson.dumps(
            _lesson("gradient boosting beats linear here", run_id="b2", task_id="task-b",
                    evidence=[9])) + b"\n")

    accepted = _decide(md, claim, scoped["revision"], scope="task-a", action_id="unrelated-rewrite")
    assert accepted.exit_code == 0, accepted.output


def test_receipt_names_its_projection_and_scope_filters_every_joined_store(tmp_path):
    from looplab.engine.concept_capsules import build_concept_capsule

    md = _portfolio(tmp_path)
    _write(md / "concept_capsules.jsonl", [
        build_concept_capsule(
            run_id="a1", task_id="task-a", fingerprint=["task-a"], direction="max",
            concepts=["data/hard-negative-mining"], best_metric=0.9,
            concept_outcomes={"data/hard-negative-mining": 0.9}),
        build_concept_capsule(
            run_id="b1", task_id="task-b", fingerprint=["task-b"], direction="max",
            concepts=["model/gradient-boosting"], best_metric=0.8,
            concept_outcomes={"model/gradient-boosting": 0.8}),
    ])
    assert _review(md)["scope"] == ""
    assert _review(md, scope="task-a")["scope"] == "task-a"

    # `--pack` joins claims AND concepts into one payload. Scoping is per-store, so a `--scope` that
    # narrowed claims but left capsules portfolio-wide would put task-b's concepts beside task-a's
    # claims — the half-scoped join `scope_cross_run_sources` exists to prevent.
    def _pack(*extra):
        result = CliRunner().invoke(app, [
            "claims", str(md), "--pack", "--json", "--structured", *extra])
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)

    wide, scoped = _pack(), _pack("--scope", "task-a")
    assert wide["coverage"]["n_concepts"] == 2 and wide["n_claims_total"] == 2
    assert scoped["coverage"]["n_concepts"] == 1 and scoped["n_claims_total"] == 1
    assert scoped["coverage"]["top_concepts"] == ["data/hard-negative-mining"]
    assert [c["scope"] for c in scoped["claims"]] == ["task-a"]


def test_governance_receipt_is_undecidable_without_structured_identity(tmp_path):
    """`--governance-receipt` without `--structured` emits rows that carry no `claim_uid`/
    `evidence_digest` at all, so `--scope` only matters on the structured path. The receipt says so."""
    md = _portfolio(tmp_path)
    result = CliRunner().invoke(app, [
        "claims", str(md), "--json", "--governance-receipt", "--scope", "task-a"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["structured"] is False and payload["scope"] == "task-a"
    assert "claim_uid" not in payload["claims"][0]
    assert "evidence_digest" not in payload["claims"][0]


def test_cli_and_http_review_projections_are_identical(tmp_path):
    """The two surfaces must not diverge again: the CLI's `--scope` is the HTTP `scope_task` read, and a
    receipt obtained from either must be accepted by the other's decision write."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from looplab.serve.routers.cross_run import _resolved_portfolio_identity
    from looplab.serve.server import make_app

    md = Path(os.environ["LOOPLAB_MEMORY_DIR"])
    md.mkdir(parents=True, exist_ok=True)
    _write(md / "lessons.jsonl", [
        _lesson("hard-neg mining lifts recall", run_id="a1", task_id="task-a", evidence=[1]),
        _lesson("hard-neg mining lifts recall", run_id="a2", task_id="task-a", evidence=[2]),
        _lesson("gradient boosting beats linear here", run_id="b1", task_id="task-b", evidence=[1]),
    ])
    client = TestClient(make_app(tmp_path))
    portfolio_id = _resolved_portfolio_identity(md)[1]

    for scope in ("task-a", "task-b", ""):
        cli = _review(md, scope=scope)
        http = client.get("/api/cross-run/claims", params={"scope_task": scope}).json()
        assert cli["scope"] == http["scope_task"] == scope
        assert cli["revision"] == http["revision"]
        assert ([(c["claim_uid"], c["evidence_digest"], c["scope"]) for c in cli["claims"]]
                == [(c["claim_uid"], c["evidence_digest"], c["scope"]) for c in http["claims"]])

    # a CLI-reviewed receipt is accepted by the HTTP write...
    cli_claim = _pick(_review(md, scope="task-a"), "task-a")
    response = client.post("/api/cross-run/claim-decide", json={
        "statement": cli_claim["statement"], "claim_uid": cli_claim["claim_uid"],
        "evidence_digest": cli_claim["evidence_digest"], "scope": "task-a", "metric": "",
        "decision": "ratified", "note": "", "expected_revision": 0,
        "action_id": "cli-receipt-http-write", "expected_portfolio_id": portfolio_id,
    })
    assert response.status_code == 200, response.text

    # ...and an HTTP-reviewed receipt is accepted by the CLI write, at the same scope.
    http_snapshot = client.get("/api/cross-run/claims", params={"scope_task": "task-b"}).json()
    http_claim = [c for c in http_snapshot["claims"] if c["scope"] == "task-b"][0]
    accepted = _decide(md, http_claim, http_snapshot["revision"], scope="task-b",
                       action_id="http-receipt-cli-write", decision="--pin")
    assert accepted.exit_code == 0, accepted.output

    # both surfaces refuse a portfolio-wide receipt for a scoped claim, with the same code.
    wide_http = client.get("/api/cross-run/claims").json()
    wide_claim = [c for c in wide_http["claims"] if c["scope"] == "task-a"][0]
    assert wide_claim["evidence_digest"] != cli_claim["evidence_digest"]
    response = client.post("/api/cross-run/claim-decide", json={
        "statement": wide_claim["statement"], "claim_uid": wide_claim["claim_uid"],
        "evidence_digest": wide_claim["evidence_digest"], "scope": "task-a", "metric": "",
        "decision": "pinned", "note": "", "expected_revision": wide_http["revision"],
        "action_id": "http-wide-review", "expected_portfolio_id": portfolio_id,
    })
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "claim_evidence_changed"
    refused = _decide(md, wide_claim, wide_http["revision"], scope="task-a",
                      action_id="cli-wide-review", decision="--pin")
    assert refused.exit_code == 2 and "claim_evidence_changed" in refused.output
