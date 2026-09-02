"""Owner management and the isolated reviewer read namespace."""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, SecretStr, StrictBool

from looplab.core.node_evidence import node_attempt
from looplab.serve.http import comment_cursor_error, comment_filter_invalid
from looplab.serve.metrics_adapters import fenced_node_metrics
from looplab.events.comment_projection import (
    CommentCursorError, comments_page, project_comments)
from looplab.events.replay import fold
from looplab.serve.reviews import (
    DEFAULT_TTL_SECONDS, REVIEW_HEADER, ReviewError, exact_review_generation,
    exact_review_request_id, exact_review_token_secret)
from looplab.serve.run_commands import run_generation_token
from looplab.core.redact import redact_secrets


class ReviewCreate(BaseModel):
    # Keep legacy coercion in ReviewStore.create(), but retain the raw JSON type so the recovery
    # contract can require an exact integer and reject bool/string aliases before fingerprinting.
    ttl_seconds: object = DEFAULT_TTL_SECONDS
    # StrictBool, not bool: this flag EXPANDS a public capability boundary (it mints an
    # evidence-scoped bearer), and pydantic's lax coercion accepted "yes"/"on"/"1"/"true" — verified
    # live — so a client that never sent JSON `true` still got the wider token. Only the boolean
    # itself may widen the share.
    include_evidence: StrictBool = False
    # These are manually validated as one envelope. In particular token_secret stays outside a
    # regex-constrained pydantic field: validation errors can echo their offending input.
    expected_generation: object = None
    request_id: object = None
    token_secret: object = None


# Config is a reproducibility file, not automatically a public document.  The review UI consumes
# only these non-secret controls (budget summary + Trust panel); base URLs, model/deployment details,
# repo paths and future settings stay owner-only.
_REVIEW_CONFIG_KEYS = {
    "max_eval_seconds", "trust_mode", "eval_trust_mode", "trust_gate", "reward_hack_detect",
}

# Summary links must not grow a raw-evidence side channel merely because a future folded-state field
# starts carrying one of these payloads.  Evidence links disclose redacted node source through the
# dedicated node route; logs, traces, prompts and artifacts remain excluded for every scope.
_SUMMARY_OMIT_KEYS = {
    "adapter_files", "artifacts", "code", "files", "logs", "messages", "parent_code",
    "prompt", "prompts", "raw_log", "raw_logs", "spans", "system_prompt", "trace",
    # A review link is a capability over ONE run, so it must not disclose the PORTFOLIO.
    # `RunState.cross_run_priors` folds `cross_run_prior` events whose `prior_runs` name sibling
    # run_ids and their similarity — other runs' identities, which a one-run bearer was never
    # granted. (The Card-level `cross_run_prior` metadata rides through the preserved Cards
    # fragment and still needs the review-specific DTO tracked separately.)
    "cross_run_priors",
    # THIRD carrier of the same portfolio disclosure: `Node.origin` ({"run_id","node_id","metric"})
    # is set when the node was SEEDED from an experiment in a sibling run, and the light state
    # projection never trimmed it. It is audit/UI-only (the Dag renders it as a link to that other
    # run), so dropping it costs a reviewer nothing they were granted. Only the EXACT key `origin`
    # is omitted — `research_origin` is WITHIN-run provenance (which deep-research memo steered the
    # proposal) and deliberately survives.
    "origin",
}
_BENIGN_SECRET_KEYS = {
    "tokenizer", "max_tokens", "num_tokens", "n_tokens", "total_tokens", "prompt_tokens",
    "completion_tokens", "tokens",
}
_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|secret|access[_-]?key|token|password|passwd|credential)", re.IGNORECASE)
_MAX_METRIC_SERIES = 64
_MAX_METRIC_POINTS = 5_000
_REVIEW_COST_KEYS = ("cost", "calls", "priced_calls",
                     "prompt_tokens", "completion_tokens", "total_tokens")
# Detail is an opt-in source-evidence projection, not a serialized Node passthrough.  Keep an explicit
# allow-list so future model fields (especially logs, prompts, trace data, or host paths) cannot become
# reviewer-visible merely because they were added to ``Node``.
_REVIEW_NODE_KEYS = {
    "id", "parent_ids", "operator", "idea", "code", "files", "deleted", "metric", "status",
    "error_reason", "confirmed_mean", "confirmed_std", "confirmed_seeds", "holdout_metric",
    "generalization_gap", "eval_seconds", "extra_metrics",
    # WITHOUT this key a reviewer sees `extra_metrics` and cannot tell an operator-declared
    # measurement from a number the candidate simply printed — which is the whole defect. It is
    # allow-listed for the same reason the values are: this scope is source EVIDENCE, and a value
    # whose provenance is withheld is evidence presented as stronger than it is. It carries no
    # portfolio identity (`{name: "declared"|"auto"}` over this node's own keys), so it raises none
    # of the disclosure questions `origin` does.
    "extra_metrics_provenance",
    # ...and WHICH WAY IS BETTER on each, for the same reason and by the same rule: a reviewer
    # handed `nDCG_at_100: 0.44` beside `0.41` cannot say which node did better without it, and the
    # answer is not derivable from the key's spelling.
    "extra_metrics_direction",
    # ...and WHETHER THE WHOLE MAP IS A RECONSTRUCTION, by the same rule a third time. The score
    # backfill recovers values from the preserved score log after the run and writes them through
    # the `declared` channel — correctly — so `extra_metrics_provenance` alone tells a reviewer the
    # guarded channel produced them and nothing tells them it was recovered afterwards, at a
    # precision two decimals coarser than the objective. Two nodes that tie on a reconstructed row
    # are not known to be equal, and a reviewer comparing them cannot see that without this. It
    # carries no portfolio identity — a flag, a timestamp and this node's own per-key decimals.
    "extra_metrics_backfill",
    "violations", "feasible", "stages",
    # `repairs` rides with `stages` for the SAME reason `extra_metrics_provenance` rides with
    # `extra_metrics`, one paragraph up: each stage row carries the repair epoch it was recorded in,
    # and without the node's CURRENT epoch to compare against, a reviewer sees a red `train ✗` and
    # cannot tell a live failure from one a later repair already superseded (`core/models.py::
    # stage_row_superseded`). Withholding it presents stale evidence as current, which is the
    # direction this scope may never fail in. It is a within-run count about THIS node — the same
    # category as `attempt` below — and names no sibling run, so it raises none of `origin`'s
    # disclosure questions.
    "repairs",
    # `origin` is deliberately ABSENT: it names a sibling run (see `_SUMMARY_OMIT_KEYS`), and this
    # route's closing `_scrub_json` carries no omit set, so allow-listing it here would disclose the
    # portfolio through the evidence scope even though the summary scope denies it.
    # `forked_from` joins `research_origin` on the WITHIN-run side of that same line: it names a node
    # id, a generation, an observed seq, an idea digest and the Idea field names the operator edited
    # — every one of them about THIS run, which a one-run bearer was already granted through
    # `parent_ids` and `idea`. Withholding it would hide from a reviewer that a human authored this
    # experiment's idea by editing another node's, which is provenance the review exists to carry.
    "failed_stage", "attempt", "research_origin", "forked_from",
}


def _secret_key(name: object) -> bool:
    text = str(name)
    return text.lower() not in _BENIGN_SECRET_KEYS and bool(_SECRET_KEY.search(text))


def _unique_redacted_key(clean_key: str, counts: dict[str, int], occupied) -> str:
    """Keep redacted mapping keys distinct without reintroducing raw or hashed secret material."""
    count = counts.get(clean_key, 0) + 1
    counts[clean_key] = count
    output_key = clean_key if count == 1 else f"{clean_key} [redacted {count}]"
    while output_key in occupied:
        count += 1
        counts[clean_key] = count
        output_key = f"{clean_key} [redacted {count}]"
    return output_key


# Deeper than any real engine event payload, far under the interpreter recursion limit: a
# pathologically/adversarially nested value on this untrusted read surface must truncate, not 500.
_MAX_SCRUB_DEPTH = 40


def _scrub_json(value, *, omit_keys: set[str] | frozenset[str] = frozenset(), _depth: int = 0):
    """Copy a JSON-like value while masking secrets in every nested key/string/value.

    Key-aware masking matters for values such as ``{"db_password": "ordinary-looking"}``, whose
    value alone has neither a known credential prefix nor enough entropy for ``redact_secrets``.
    Keys are output too: source filenames, parameter names, and metric names can themselves contain
    a credential. Redacted-key collisions receive a deterministic suffix instead of silently
    overwriting one another.
    Returning fresh containers also ensures review filtering never mutates AppState's shared cache.
    """
    if _depth >= _MAX_SCRUB_DEPTH:
        # Collapse an over-deep subtree to a bounded marker rather than recursing into a
        # RecursionError (which would surface as a 500 on the read-only reviewer GET).
        return "***(nested too deep)***"
    if isinstance(value, dict):
        out = {}
        key_counts: dict[str, int] = {}
        for key, item in value.items():
            if str(key).lower() in omit_keys:
                continue
            clean_key = redact_secrets(str(key))
            output_key = _unique_redacted_key(clean_key, key_counts, out)
            if _secret_key(key):
                out[output_key] = None if item is None else "***"
            else:
                out[output_key] = _scrub_json(item, omit_keys=omit_keys, _depth=_depth + 1)
        return out
    if isinstance(value, list):
        return [_scrub_json(item, omit_keys=omit_keys, _depth=_depth + 1) for item in value]
    if isinstance(value, tuple):
        return [_scrub_json(item, omit_keys=omit_keys, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        return redact_secrets(value)
    return value


def _review_metrics(raw) -> dict[str, list[dict]]:
    """Allow only bounded finite scalar series; drop adapter-specific strings/paths/extras."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[dict]] = {}
    tag_counts: dict[str, int] = {}
    for index, (raw_tag, raw_series) in enumerate(raw.items()):
        if index >= _MAX_METRIC_SERIES:
            break
        if not isinstance(raw_tag, str) or not isinstance(raw_series, (list, tuple)):
            continue
        clean_tag = redact_secrets(raw_tag)[:256]
        if not clean_tag:
            continue
        points = []
        for point in raw_series[-_MAX_METRIC_POINTS:]:
            if not isinstance(point, dict):
                continue
            try:
                step = int(point["step"])
                value = float(point["value"])
                wall_time = float(point["wall_time"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if abs(step) > 2**63 - 1 or not math.isfinite(value) or not math.isfinite(wall_time):
                continue
            points.append({"step": step, "value": value, "wall_time": wall_time})
        if points:
            tag = _unique_redacted_key(clean_tag, tag_counts, out)
            out[tag] = points
            if len(out[tag]) > _MAX_METRIC_POINTS:
                out[tag] = out[tag][-_MAX_METRIC_POINTS:]
    for points in out.values():
        points.sort(key=lambda point: point["step"])
    return out


def _review_cost(raw) -> dict:
    # ABSENT IS NOT ZERO. `RunState.llm_cost` is `None` until a roll-up is actually written (an
    # offline/toy run, or one that has not finalized, never writes one — 13 of the 46 runs under
    # `runs/` are in that state), and the zero-filled body alone is byte-identical to a finished run
    # that genuinely made no provider call. `format.js::costPricing` reads exactly that shape as
    # `calls <= 0` and prints "$0 — No model calls were made, so nothing was spent": a confident
    # claim about money nobody measured, shown to the one party who cannot cross-check it against
    # the live run. The owner twin `routers/runs.py::run_cost` already carries `recorded` for this
    # reason; this is the same fact on the read-only route. The zeros stay, so any existing
    # arithmetic client is byte-for-byte unaffected.
    defaults = {"cost": 0.0, "calls": 0, "total_tokens": 0}
    if not isinstance(raw, dict):
        return {**defaults, "recorded": False}
    out = {}
    for key in _REVIEW_COST_KEYS:
        if key not in raw or isinstance(raw[key], bool):
            continue
        try:
            number = float(raw[key])
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(number) and number >= 0:
            # Saturate the integer counters at the same 63-bit ceiling every other cost sanitizer
            # uses (replay._llm_counter, _review_metrics' step guard); a corrupt token field must
            # not become a ~1000-digit bigint in the public review projection.
            out[key] = min(int(number), 2**63 - 1) if key != "cost" else number
    # Keyed on `out`, not on `raw` being a dict: a payload that survives none of the checks above
    # (empty, or every field corrupt/negative/non-finite) told us nothing, and reporting it as a
    # recorded roll-up of zero is the same lie one branch up. `recorded` is stamped AFTER the merge
    # and is deliberately NOT in `_REVIEW_COST_KEYS`, so it stays this projection's own verdict —
    # a hand-edited `llm_cost` carrying that key can never assert that a roll-up exists.
    return {**defaults, **out, "recorded": bool(out)}


def _http_error(exc: ReviewError) -> HTTPException:
    if exc.kind == "not_found":
        return HTTPException(404, str(exc))
    if exc.kind in {"expired", "revoked", "generation"}:
        return HTTPException(410, str(exc))
    return HTTPException(401, str(exc))


def _create_error(code: str, message: str, *, status: int, **safe) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message, **safe})


def _recovery_envelope(body: ReviewCreate) -> dict | None:
    names = ("expected_generation", "request_id", "token_secret")
    # Presence, not value, selects the recovery contract: three explicit JSON nulls must be rejected
    # below and can never silently downgrade into the legacy random-create path.
    fields_set = getattr(body, "model_fields_set", getattr(body, "__fields_set__", set()))
    supplied = tuple(name in fields_set for name in names)
    if not any(supplied):
        return None
    if not all(supplied):
        raise _create_error(
            "invalid_review_create_envelope",
            "Review-link recovery fields must be supplied together.", status=400)
    if type(body.ttl_seconds) is not int or type(body.include_evidence) is not bool:
        raise _create_error(
            "invalid_review_create_envelope",
            "Review-link recovery settings are invalid.", status=400)
    generation = exact_review_generation(body.expected_generation)
    request_id = exact_review_request_id(body.request_id)
    if generation is None or request_id is None or type(body.token_secret) is not str:
        raise _create_error(
            "invalid_review_create_envelope",
            "Review-link recovery fields are invalid.", status=400)
    # SecretStr prevents accidental repr/log interpolation below. Parsing remains manual so malformed
    # secret material is never reflected through FastAPI's validation-error payload.
    protected_secret = SecretStr(body.token_secret)
    token_secret = exact_review_token_secret(protected_secret.get_secret_value())
    if token_secret is None:
        raise _create_error(
            "invalid_review_create_envelope",
            "Review-link recovery fields are invalid.", status=400)
    return {
        "expected_generation": generation,
        "request_id": request_id,
        "token_secret": token_secret,
    }


def _recovery_store_error(exc: ReviewError) -> HTTPException:
    if exc.kind == "conflict":
        safe = {}
        link_id = exc.metadata.get("link_id")
        if isinstance(link_id, str):
            safe["existing_link_id"] = link_id
        return _create_error(
            "review_idempotency_conflict",
            "This review create identity is already bound to another request.",
            status=409, **safe)
    if exc.kind == "generation_conflict":
        safe = {}
        for key in ("expected_generation", "current_generation"):
            value = exact_review_generation(exc.metadata.get(key))
            if value is not None:
                safe[key] = value
        return _create_error(
            "review_generation_changed",
            "The run changed before the review link could be created.", status=409, **safe)
    if exc.kind == "storage":
        return _create_error(
            "review_store_unavailable",
            "Review-link storage is temporarily unavailable.", status=503)
    return _create_error(
        "invalid_review_create_envelope",
        "Review-link recovery settings are invalid.", status=400)


def build_router(srv) -> APIRouter:
    router = APIRouter()

    def _record(request: Request) -> dict:
        try:
            return srv.reviews.resolve(request.headers.get(REVIEW_HEADER, ""))
        except ReviewError as exc:
            raise _http_error(exc) from exc

    def _run(record: dict):
        # The run id comes only from the resolved capability, never from reviewer input.
        rd = srv.run_dir(str(record["run_id"]))
        # Do not let a hand-crafted run whose event log is a symlink turn this one-run capability
        # into a file-read primitive outside the run directory.
        events = (rd / "events.jsonl").resolve()
        if rd not in events.parents:
            raise HTTPException(404, "no such run")
        return rd

    def _generation_gone() -> HTTPException:
        return HTTPException(
            410, "this review link belongs to a run generation that is no longer available")

    def _assert_generation(rd, expected: str) -> None:
        try:
            current = srv.commands.run_generation(rd)
        except HTTPException as exc:
            if exc.status_code == 404:
                raise _generation_gone() from exc
            raise
        if (exact_review_generation(expected) is None
                or exact_review_generation(current) is None or current != expected):
            raise _generation_gone()

    @contextmanager
    def _bound_run(request: Request):
        """Generation-check immediately before and after a projection without serializing the read.

        Slow folds and metrics adapters must not hold the exclusive command sequencer. A reset/delete
        may therefore win while a projection is assembled, but the second short check converts that
        raced projection to 410 before it can be returned.
        """
        record = _record(request)
        expected = record.get("generation")
        try:
            rd = _run(record)
        except HTTPException as exc:
            if exc.status_code == 404:
                raise _generation_gone() from exc
            raise
        def validate_bound_generation():
            nonlocal rd
            with srv.commands.sequence(rd):
                try:
                    rd = srv.commands.validate_paths(rd)
                except HTTPException as exc:
                    if exc.status_code == 404:
                        raise _generation_gone() from exc
                    raise
                _assert_generation(rd, expected)

        validate_bound_generation()
        try:
            yield record, rd
        finally:
            # Also catches a reset/delete or an out-of-band replacement that completed while the
            # projection was being assembled. The sequencer is held only for this validation.
            validate_bound_generation()

    def _run_file(rd, name: str):
        path = (rd / name).resolve()
        if rd not in path.parents:
            raise HTTPException(404, "run resource is unavailable")
        return path

    def _evidence(record: dict) -> None:
        if "evidence" not in set(record.get("scopes") or []):
            raise HTTPException(403, "this review link does not include source evidence")

    @router.get("/api/review")
    def review_manifest(request: Request, response: Response):
        """Resolve the credential carried by the tokenless review SPA.

        The middleware already validated and scoped this request.  Resolve again here rather than
        trusting request-local mutable state so the manifest also observes a revoke/expiry that races
        with request dispatch.
        """
        with _bound_run(request) as (record, _rd):
            response.headers["Cache-Control"] = "no-store"
            return {"mode": "review", **record}

    @router.get("/api/review/state")
    def review_state(request: Request, seq: Optional[int] = None):
        """Return the review-safe state with the same bounded Cards fragment as owner/SSE state."""
        with _bound_run(request) as (_record_value, rd):
            if seq is not None:
                # The review UI has no history scrubber.  Reject arbitrary historical folds instead
                # of giving an untrusted recipient an unbounded cache-key/CPU amplification primitive.
                raise HTTPException(400, "historical snapshots are not available through review links")
            # ``state_payload`` already computed Cards and their exact completeness receipt via the
            # bounded, secret-redacted public projection (identical to owner/SSE state). A second
            # generic scrub would mutate secret-looking Card keys/values WITHOUT recomputing
            # ``cards_projection``, so the review response could claim exact/complete coverage of data
            # it silently redacted. Preserve the canonical Cards fragment verbatim through the scrub —
            # owner/SSE never re-scrub it either — so the completeness receipt stays truthful.
            # `audience="review"` is where "one run" becomes structural rather than a promise: the
            # Card projection is built WITHOUT the members that name sibling runs, so the receipt
            # preserved below describes exactly what this bearer receives. (Its top-level twin,
            # `RunState.cross_run_priors`, is dropped by `_SUMMARY_OMIT_KEYS` — same disclosure, two
            # carriers.) Narrowing at projection time is what keeps the receipt honest: scrubbing the
            # finished DTO would leave it certifying data the response no longer contains.
            payload = srv.state_payload(rd, audience="review")
            inner = payload.get("state")
            preserved = ({key: inner[key] for key in ("cards", "cards_projection") if key in inner}
                         if isinstance(inner, dict) else {})
            scrubbed = _scrub_json(payload, omit_keys=_SUMMARY_OMIT_KEYS)
            if preserved and isinstance(scrubbed.get("state"), dict):
                scrubbed["state"].update(preserved)
            return scrubbed

    @router.get("/api/review/comments")
    def review_comments(request: Request, limit: int = Query(100, ge=1, le=100),
                        cursor: Optional[str] = None,
                        node_id: Optional[int] = Query(None, ge=0),
                        node_generation: Optional[int] = Query(None, ge=0),
                        include_resolved: bool = True):
        """Current, redacted comments only; review capabilities never expose prior revisions."""
        with _bound_run(request) as (_record_value, rd):
            if (node_id is None) != (node_generation is None):
                raise comment_filter_invalid()
            events = srv.events(rd)
            generation = run_generation_token(events)
            comments, _history = project_comments(events)
            try:
                payload = comments_page(
                    comments, generation=generation, limit=limit, cursor=cursor,
                    node_id=node_id, node_generation=node_generation,
                    include_resolved=include_resolved)
            except CommentCursorError as exc:
                raise comment_cursor_error(exc) from exc
            for comment in payload["comments"]:
                comment["editable"] = False
            # Current-only is still untrusted free-form text: use the same recursive scrub as every
            # other review projection, including credential-shaped mapping keys and values.
            return _scrub_json(payload)

    @router.get("/api/review/config")
    def review_config(request: Request):
        with _bound_run(request) as (_record_value, rd):
            snap = _run_file(rd, "config.snapshot.json")
            if snap.exists():
                try:
                    data = json.loads(snap.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    raise HTTPException(500, "the run configuration could not be read")
                if not isinstance(data, dict):
                    raise HTTPException(500, "the run configuration could not be read")
                return _scrub_json({key: data[key] for key in _REVIEW_CONFIG_KEYS if key in data})
            # Never substitute the current process Settings for an old run: that would cross the run
            # boundary and disclose present-day deployment configuration to a legacy review link.  A
            # 404 also lets the Trust panel say coverage is unknown instead of mistaking `{}` for an
            # authoritative configuration in which every detector was disabled.
            raise HTTPException(404, "this run has no reviewable configuration snapshot")

    @router.get("/api/review/cost")
    def review_cost(request: Request):
        with _bound_run(request) as (_record_value, rd):
            return _review_cost(srv.state(rd).llm_cost)

    @router.get("/api/review/nodes/{nid}/metrics")
    def review_node_metrics(nid: int, request: Request):
        with _bound_run(request) as (_record_value, rd):
            node_dir = (rd / "nodes" / f"node_{nid}").resolve()
            if rd not in node_dir.parents:
                raise HTTPException(404, "node metrics are unavailable")
            # Fence on the attempt receipt exactly as the owner route (runs.py node_metrics) does.
            # A reset REUSES the node directory, so its metric sidecar still holds the previous
            # attempt's points; without this gate a reviewer got the superseded series mixed with
            # (or instead of) the current one — the known-stale data the owner endpoint already
            # refuses to serve. Legacy attempt-zero runs predate receipts and stay readable; a later
            # attempt whose exact marker is missing yields NO series rather than old evidence.
            # Unlike the owner route this does not 409 on a concurrent reset: a reviewer is a
            # read-only observer, so the honest answer to "which attempt is this?" is an empty
            # series, not an error the review UI has no way to resolve.
            current_attempt = node_attempt(srv.state(rd), nid)
            try:
                metrics = _review_metrics(fenced_node_metrics(node_dir, current_attempt))
            except Exception:  # noqa: BLE001 - observability must not take down a review
                metrics = {}
            return {"metrics": metrics}

    @router.get("/api/review/nodes/{nid}")
    def review_node(nid: int, request: Request,
                    seq: Optional[int] = Query(None, ge=0),
                    expected_generation: Optional[str] = None):
        """Opt-in evidence projection: source/results, redacted, never live trace sidecars."""
        with _bound_run(request) as (record, rd):
            _evidence(record)
            bound_generation = exact_review_generation(record.get("generation"))
            if bound_generation is None:
                raise _generation_gone()
            request_generation = exact_review_generation(expected_generation)
            if expected_generation is not None and request_generation is None:
                raise HTTPException(400, {
                    "code": "invalid_run_generation",
                    "message": "Review evidence requires a canonical run generation.",
                    "remediation": "Use the generation returned by the review state response.",
                })
            if request_generation is not None and request_generation != bound_generation:
                raise HTTPException(409, {
                    "code": "run_generation_changed",
                    "message": "The requested evidence belongs to a different run generation.",
                    "remediation": "Reload the review state before requesting solution evidence.",
                })
            if seq is not None:
                if request_generation is None:
                    raise HTTPException(400, {
                        "code": "historical_generation_required",
                        "message": "Snapshot-bound review evidence requires the exact run generation.",
                        "remediation": "Use the generation returned by the review state response.",
                    })
                # Review links intentionally have no history browser. Permit only the exact latest
                # state sequence the reviewer could have observed, then fold that captured event list
                # so an append racing the response cannot relabel newer code as the requested snapshot.
                events = srv.events(rd)
                current_seq = events[-1].seq if events else -1
                if seq != current_seq:
                    raise HTTPException(409, {
                        "code": "review_snapshot_changed",
                        "message": "The run advanced after this review snapshot was displayed.",
                        "remediation": "Wait for the review state to refresh and retry.",
                    })
                st = fold(events)
            else:
                st = srv.state(rd)
            node = st.nodes.get(nid)
            if node is None:
                raise HTTPException(404, "no such node at requested sequence"
                                    if seq is not None else "no such node")
            dumped = node.model_dump(mode="json")
            out = {key: dumped[key] for key in _REVIEW_NODE_KEYS if key in dumped}
            # Keep the same short failure summary already present in the light state projection; the
            # unbounded captured process output remains excluded below.
            # Redact BEFORE truncating: a secret straddling byte 160 would otherwise have its tail
            # cut, leaving a prefix too short for the pattern/entropy rules to catch (fragment leak).
            out["error"] = redact_secrets(str(dumped.get("error") or ""))[:160]
            # Evidence is explicit opt-in, but still run the normal secret scrub before disclosure.
            # stdout_tail is captured process output, not source evidence, and is intentionally absent.
            # Do not attach spans.jsonl either: it contains model prompts/tool outputs and is a live
            # sidecar rather than an event-versioned fact.
            for key in ("code",):
                if isinstance(out.get(key), str):
                    out[key] = redact_secrets(out[key])
            if isinstance(out.get("files"), dict):
                out["files"] = {name: redact_secrets(body) if isinstance(body, str) else body
                                for name, body in out["files"].items()}
            out["confirm_seeds_detail"] = st.confirm_seed_results.get(nid, {})
            if node.parent_ids:
                parent = st.nodes.get(node.parent_ids[0])
                if parent is not None:
                    out["parent_code"] = redact_secrets(parent.code or "")
                    out["parent_id_diffed"] = parent.id
            out["trace"] = {"nodes": [], "rollup": {}, "summary": {}}
            out["run_generation"] = bound_generation
            if seq is not None:
                out["historical_seq"] = seq
                out["historical_generation"] = bound_generation
            return _scrub_json(out)

    @router.post("/api/runs/{run_id}/reviews")
    def create_review(run_id: str, body: ReviewCreate, response: Response):
        if not getattr(srv, "owner_auth_enabled", False):
            raise HTTPException(
                409, "read-only sharing requires LOOPLAB_UI_TOKEN so the owner control plane is not anonymous")
        recovery = _recovery_envelope(body)
        # Validate existence and traversal before persisting a capability.
        rd = srv.run_dir(run_id)
        try:
            with srv.commands.sequence(rd):
                rd = srv.commands.validate_paths(rd)
                current_generation = srv.commands.run_generation(rd)
                if recovery is None:
                    token, record = srv.reviews.create(
                        rd.name, generation=current_generation,
                        ttl_seconds=body.ttl_seconds, include_evidence=body.include_evidence)
                    replayed = False
                    status = None
                else:
                    token, record, replayed = srv.reviews.create_or_replay(
                        rd.name, generation=current_generation,
                        ttl_seconds=body.ttl_seconds,
                        include_evidence=body.include_evidence,
                        **recovery)
                    status = srv.reviews.status(
                        record, current_generation=current_generation)
        except ReviewError as exc:
            if recovery is not None:
                raise _recovery_store_error(exc) from exc
            if exc.kind == "storage":
                raise _create_error(
                    "review_store_unavailable",
                    "Review-link storage is temporarily unavailable.", status=503) from exc
            raise HTTPException(409 if exc.kind == "generation" else 400, str(exc)) from exc
        if recovery is None:
            # Preserve the legacy body and 200 response for callers without a recovery envelope.
            return {"ok": True, "token": token, "path": f"review#/{token}", **record}
        if replayed and status != "active":
            # The exact operation is immutable: expiry, revocation, and generation replacement never
            # mint or reveal a fresh bearer. A generic success handler therefore cannot copy a dead URL.
            raise _create_error(
                "review_replay_terminal",
                "The original review link is no longer active.", status=410,
                kind=status, existing_link_id=record["id"],
                generation=record["generation"], expires_at=record["expires_at"],
                revoked_at=record["revoked_at"])
        response.status_code = 200 if replayed else 201
        return {
            "ok": True,
            "token": token,
            "path": f"review#/{token}",
            **record,
            "status": status,
            "replayed": replayed,
        }

    @router.get("/api/runs/{run_id}/reviews")
    def list_reviews(run_id: str):
        rd = srv.run_dir(run_id)
        current = srv.commands.run_generation(rd)
        links = srv.reviews.list_for_run(rd.name)
        for link in links:
            if link.get("status") == "active" and link.get("generation") != current:
                link["status"] = "stale"
        return {"links": links}

    @router.delete("/api/runs/{run_id}/reviews/{link_id}")
    def revoke_review(run_id: str, link_id: str):
        rd = srv.run_dir(run_id)
        try:
            with srv.commands.sequence(rd):
                rd = srv.commands.validate_paths(rd)
                record = srv.reviews.revoke(rd.name, link_id)
        except ReviewError as exc:
            if exc.kind == "storage":
                raise _create_error(
                    "review_store_unavailable",
                    "Review-link storage is temporarily unavailable.", status=503) from exc
            raise _http_error(exc) from exc
        return {"ok": True, **record}

    return router
