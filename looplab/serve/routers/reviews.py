"""Owner management and the isolated reviewer read namespace."""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from looplab.serve.metrics_adapters import read_node_metrics
from looplab.serve.reviews import (
    DEFAULT_TTL_SECONDS, REVIEW_HEADER, ReviewError, exact_review_generation)
from looplab.trust.redact import redact_secrets


class ReviewCreate(BaseModel):
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    include_evidence: bool = False


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
}
_BENIGN_SECRET_KEYS = {
    "tokenizer", "max_tokens", "num_tokens", "n_tokens", "total_tokens", "prompt_tokens",
    "completion_tokens", "tokens",
}
_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|secret|access[_-]?key|token|password|passwd|credential)", re.IGNORECASE)
_MAX_METRIC_SERIES = 64
_MAX_METRIC_POINTS = 5_000
_REVIEW_COST_KEYS = ("cost", "calls", "prompt_tokens", "completion_tokens", "total_tokens")
# Detail is an opt-in source-evidence projection, not a serialized Node passthrough.  Keep an explicit
# allow-list so future model fields (especially logs, prompts, trace data, or host paths) cannot become
# reviewer-visible merely because they were added to ``Node``.
_REVIEW_NODE_KEYS = {
    "id", "parent_ids", "operator", "idea", "code", "files", "deleted", "metric", "status",
    "error_reason", "confirmed_mean", "confirmed_std", "confirmed_seeds", "holdout_metric",
    "generalization_gap", "eval_seconds", "extra_metrics", "violations", "feasible", "stages",
    "failed_stage", "attempt", "origin", "research_origin",
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


def _scrub_json(value, *, omit_keys: set[str] | frozenset[str] = frozenset()):
    """Copy a JSON-like value while masking secrets in every nested key/string/value.

    Key-aware masking matters for values such as ``{"db_password": "ordinary-looking"}``, whose
    value alone has neither a known credential prefix nor enough entropy for ``redact_secrets``.
    Keys are output too: source filenames, parameter names, and metric names can themselves contain
    a credential. Redacted-key collisions receive a deterministic suffix instead of silently
    overwriting one another.
    Returning fresh containers also ensures review filtering never mutates AppState's shared cache.
    """
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
                out[output_key] = _scrub_json(item, omit_keys=omit_keys)
        return out
    if isinstance(value, list):
        return [_scrub_json(item, omit_keys=omit_keys) for item in value]
    if isinstance(value, tuple):
        return [_scrub_json(item, omit_keys=omit_keys) for item in value]
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
    defaults = {"cost": 0.0, "calls": 0, "total_tokens": 0}
    if not isinstance(raw, dict):
        return defaults
    out = {}
    for key in _REVIEW_COST_KEYS:
        if key not in raw or isinstance(raw[key], bool):
            continue
        try:
            number = float(raw[key])
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(number) and number >= 0:
            out[key] = int(number) if key != "cost" else number
    return {**defaults, **out}


def _http_error(exc: ReviewError) -> HTTPException:
    if exc.kind == "not_found":
        return HTTPException(404, str(exc))
    if exc.kind in {"expired", "revoked", "generation"}:
        return HTTPException(410, str(exc))
    return HTTPException(401, str(exc))


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
        with _bound_run(request) as (_record_value, rd):
            if seq is not None:
                # The review UI has no history scrubber.  Reject arbitrary historical folds instead
                # of giving an untrusted recipient an unbounded cache-key/CPU amplification primitive.
                raise HTTPException(400, "historical snapshots are not available through review links")
            return _scrub_json(srv.state_payload(rd), omit_keys=_SUMMARY_OMIT_KEYS)

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
            try:
                metrics = _review_metrics(read_node_metrics(str(node_dir)))
            except Exception:  # noqa: BLE001 - observability must not take down a review
                metrics = {}
            return {"metrics": metrics}

    @router.get("/api/review/nodes/{nid}")
    def review_node(nid: int, request: Request, seq: Optional[int] = None):
        """Opt-in evidence projection: source/results, redacted, never live trace sidecars."""
        with _bound_run(request) as (record, rd):
            _evidence(record)
            if seq is not None:
                raise HTTPException(400, "historical node evidence is not available through review links")
            st = srv.state(rd)
            node = st.nodes.get(nid)
            if node is None:
                raise HTTPException(404, "no such node at requested sequence" if seq is not None else "no such node")
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
            out["annotations"] = st.annotations.get(nid, [])
            out["confirm_seeds_detail"] = st.confirm_seed_results.get(nid, {})
            if node.parent_ids:
                parent = st.nodes.get(node.parent_ids[0])
                if parent is not None:
                    out["parent_code"] = redact_secrets(parent.code or "")
                    out["parent_id_diffed"] = parent.id
            out["trace"] = {"nodes": [], "rollup": {}, "summary": {}}
            return _scrub_json(out)

    @router.post("/api/runs/{run_id}/reviews")
    def create_review(run_id: str, body: ReviewCreate):
        if not getattr(srv, "owner_auth_enabled", False):
            raise HTTPException(
                409, "read-only sharing requires LOOPLAB_UI_TOKEN so the owner control plane is not anonymous")
        # Validate existence and traversal before persisting a capability.
        rd = srv.run_dir(run_id)
        try:
            with srv.commands.sequence(rd):
                rd = srv.commands.validate_paths(rd)
                token, record = srv.reviews.create(
                    rd.name, generation=srv.commands.run_generation(rd),
                    ttl_seconds=body.ttl_seconds, include_evidence=body.include_evidence)
        except ReviewError as exc:
            raise HTTPException(409 if exc.kind == "generation" else 400, str(exc)) from exc
        # The bearer is returned exactly once.  reviews.json contains only its digest.
        return {"ok": True, "token": token, "path": f"review#/{token}", **record}

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
            record = srv.reviews.revoke(rd.name, link_id)
        except ReviewError as exc:
            raise _http_error(exc) from exc
        return {"ok": True, **record}

    return router
