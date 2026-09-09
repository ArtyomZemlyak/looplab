"""Cross-run aggregate report routes. On-demand portfolio reports over a SET of runs (a project
folder, a task, or a super-task) — ONE generator, three scope axes. Persisted under
<run-root>/reports/ with a run-set fingerprint so the UI can flag staleness; an agent reads every
accepted run through a bounded/redacted brief and bounded drill projection, then synthesizes.
Bodies are verbatim moves from
`serve/server.py::make_app` (BACKLOG §4).

What is left here is what doc 25 SR-02 asked for: endpoint wiring plus the staleness GET. The three
subsystems underneath it each have their own module — the durable STORE is
`serve/scope_report_store.py` (SR-12), the paid ACTION protocol is `serve/scope_actions.py` (SR-02),
and the scope projections, the source-probe cache and the paid GENERATION protocol are
`serve/scope_generate.py` (SR-02's remaining arm). Every route below is a delegating call except the
staleness GET, which is the one piece of case analysis the router owns.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from looplab.serve.scope_sources import MAX_SCOPE_TOTAL_EVENT_BYTES, ScopeSourceError

# The durable scope-report STORE moved to `serve/scope_report_store.py` (doc 25 SR-12):
# ~1 400 lines of path validation, receipts, leases, fences and record migration with no
# HTTP dependency, which `routers/genesis.py` could otherwise only reach by importing this
# router's privates. Star-imported so `reports.<name>` keeps resolving for `build_router`
# and for the tests that spell it that way — but see that module's docstring: a star import
# binds BY VALUE, so a monkeypatch seam belongs THERE, not here.
from looplab.serve.scope_report_store import *  # noqa: F401,F403

# The paid ACTION protocol above that store — reconciliation, abandon, and the two action
# endpoints' bodies — is `serve/scope_actions.py` (doc 25 SR-02). Imported by NAME, not star:
# these are the router's own call sites, and a star import here would re-export the store's
# names a second time under a different binding.
from looplab.serve.scope_actions import (
    durable_abandon_scope_action,
    get_scope_action,
)

# The scope PROJECTIONS, the source-probe staleness cache and the paid GENERATION protocol are
# `serve/scope_generate.py`. The four projections below are what the staleness GET joins on; the
# router holds no copy of the generation state machine at all.
from looplab.serve.scope_generate import (
    ScopeSourceProbes,
    durable_generate_scope_report,
    scope_context_digest,
    scope_label,
    scope_run_ids,
    scope_sig,
)


def build_router(srv) -> APIRouter:
    router = APIRouter()
    _reports_dir = srv.reports_dir
    # The cache lives as long as the app, not the request: it exists precisely so that a stable GET
    # reuses one parsed revision instead of rebuilding every Event object (see `ScopeSourceProbes`).
    probes = ScopeSourceProbes(srv)
    # action observation has its own namespace because scope ids are opaque paths. A
    # suffix route under ``/scope-report/...`` would steal a legitimate scope such as
    # ``family/actions/<uuid>`` from the catch-all report GET. The expected scope stays explicit in
    # the query and is verified against the durable receipt before any result is disclosed.
    @router.get("/api/scope-report-actions/{action_id}")
    def get_scope_report_action(scope_type: str, scope_id: str, action_id: str):
        return get_scope_action(srv, scope_type, scope_id, action_id)

    @router.post("/api/scope-report-actions/{action_id}/abandon")
    def abandon_scope_report_action(scope_type: str, scope_id: str, action_id: str):
        """Explicitly release an indeterminate paid-action fence without erasing its identity.

        Abandon is intentionally never automatic: after a process crash the provider outcome cannot
        be proven. The old UUID remains a durable tombstone, and only an explicit new UUID may bill a
        new attempt. A process-local running worker always wins the race and makes abandon a conflict.
        """
        return durable_abandon_scope_action(srv, scope_type, scope_id, action_id)

    # scope ids are opaque persisted identities, so the route must preserve legal
    # task/project ids containing ``/`` instead of truncating or rejecting them at the HTTP boundary.
    @router.get("/api/scope-report/{scope_type}/{scope_id:path}")
    def get_scope_report(scope_type: str, scope_id: str):
        if scope_type not in _SCOPE_TYPES:
            raise HTTPException(400, "bad scope type")
        cur_ids = scope_run_ids(srv, scope_type, scope_id)
        publication_quarantined = False
        try:
            with _scope_store_lock(_reports_dir):
                rec = _read_or_migrate_scope_record(
                    _reports_dir, scope_type, scope_id)
                if (rec is not None
                        and not _action_bound_scope_record_is_confirmed(
                            _reports_dir, rec, scope_type, scope_id)):
                    # Never expose uncommitted paid prose, but keep the endpoint usable after reload:
                    # a safe logical-missing projection preserves the Generate affordance. The old
                    # canonical bytes stay quarantined until a later confirmed action replaces them.
                    publication_quarantined = True
                    rec = None
        except _ScopeReportStorageConflict as exc:
            raise HTTPException(409, _SCOPE_STORAGE_ERROR) from exc
        if rec is None:
            response = {"exists": False, "run_count": len(cur_ids),
                        "label": scope_label(srv, scope_type, scope_id)}
            if publication_quarantined:
                response.update({
                    "quarantined": True,
                    "stale": True,
                    **_SCOPE_PUBLICATION_UNCONFIRMED,
                })
            return response
        added = sorted(set(cur_ids) - set(rec.get("run_ids", [])))
        rec, legacy_authority = _public_scope_record(rec)
        current_sig = scope_sig(srv, cur_ids)
        stale_reason = "report_authority_upgrade" if legacy_authority else None
        stale = legacy_authority
        source_revisions = rec.get("source_revisions")
        omitted_runs = rec.get("omitted_runs")
        omitted_source_probes = rec.get("omitted_source_probes")
        expected_context = rec.get("context_digest")
        if rec.get("context_schema") != _SCOPE_CONTEXT_SCHEMA:
            # Schema 1 digested the workspace-global projects store; digestless records predate even
            # that receipt. Neither can prove the new scope-local semantic slice, so retire them with
            # an explicit one-time migration reason instead of claiming that this scope changed.
            stale = True
            stale_reason = stale_reason or "report_format_upgrade"
        elif (not isinstance(expected_context, str)
                or _RUN_GENERATION_RE.fullmatch(expected_context) is None):
            stale = True
            stale_reason = stale_reason or "report_format_upgrade"
        elif scope_context_digest(srv, scope_type, scope_id, cur_ids) != expected_context:
            stale = True
            stale_reason = stale_reason or "scope_context_changed"
        if current_sig != rec.get("sig"):
            stale = True
            stale_reason = stale_reason or "scope_evidence_changed"
        if not stale and not isinstance(source_revisions, list):
            # Pre-v2 records did not bind task/config snapshots or the full event prefix.
            stale = True
            stale_reason = "report_source_receipt_upgrade"
        elif not stale:
            try:
                remaining = MAX_SCOPE_TOTAL_EVENT_BYTES
                sig_by_id = {row[0]: row for row in current_sig}
                revision_by_id = {row["run_id"]: row for row in source_revisions}
                omitted = set(omitted_runs or ())
                if (not isinstance(omitted_source_probes, dict)
                        or set(omitted_source_probes) != omitted):
                    stale = True
                for run_id in rec.get("run_ids", []):
                    if stale:
                        break
                    if run_id in revision_by_id:
                        matches, consumed = probes.revision_is_current(
                            run_id, sig_by_id[run_id], revision_by_id[run_id], remaining)
                    elif run_id in omitted:
                        matches, consumed = probes.omission_is_current(
                            run_id, sig_by_id[run_id], omitted_source_probes[run_id], remaining)
                    else:
                        matches, consumed = False, 0
                    remaining -= consumed
                    if not matches:
                        stale = True
                        stale_reason = "scope_evidence_changed"
                        break
            except ScopeSourceError:
                stale = True
                stale_reason = "scope_evidence_changed"
        return {**rec, "exists": True, "stale": stale,
                "stale_reason": stale_reason,
                "current_run_count": len(cur_ids), "added": added}

    @router.post("/api/scope-report/{scope_type}/{scope_id:path}/generate")
    async def generate_scope_report(
            scope_type: str, scope_id: str,
            idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        """Generate (or regenerate) the cross-run report for a scope. On-demand only — the agent reads
        a bounded/redacted projection of at most ``MAX_SCOPE_REPORT_RUNS`` runs and may request a
        bounded node drill. Degrades to a metrics rollup offline. Runs as a BACKGROUND JOB: reading +
        synthesizing over many runs can outlast a UI proxy's gateway timeout, so a slow synthesis hands
        back a job_id the UI polls (a fast/offline one still returns inline within the wait — no 504)."""
        return await durable_generate_scope_report(
            srv, probes, scope_type, scope_id, idempotency_key)

    return router
