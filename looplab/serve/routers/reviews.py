"""Owner management and the isolated reviewer read namespace."""
from __future__ import annotations

import json
import math
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from looplab.serve.metrics_adapters import read_node_metrics
from looplab.serve.reviews import DEFAULT_TTL_SECONDS, REVIEW_HEADER, ReviewError
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


def _secret_key(name: object) -> bool:
    text = str(name)
    return text.lower() not in _BENIGN_SECRET_KEYS and bool(_SECRET_KEY.search(text))


def _scrub_json(value, *, omit_keys: set[str] | frozenset[str] = frozenset()):
    """Copy a JSON-like value while masking secrets in every nested string/value.

    Key-aware masking matters for values such as ``{"db_password": "ordinary-looking"}``, whose
    value alone has neither a known credential prefix nor enough entropy for ``redact_secrets``.
    Returning fresh containers also ensures review filtering never mutates AppState's shared cache.
    """
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if str(key).lower() in omit_keys:
                continue
            if _secret_key(key):
                out[key] = None if item is None else "***"
            else:
                out[key] = _scrub_json(item, omit_keys=omit_keys)
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
    for index, (raw_tag, raw_series) in enumerate(raw.items()):
        if index >= _MAX_METRIC_SERIES:
            break
        if not isinstance(raw_tag, str) or not isinstance(raw_series, (list, tuple)):
            continue
        tag = redact_secrets(raw_tag)[:256]
        if not tag:
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
            out.setdefault(tag, []).extend(points)
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
    if exc.kind in {"expired", "revoked"}:
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
        record = _record(request)
        response.headers["Cache-Control"] = "no-store"
        return {"mode": "review", **record}

    @router.get("/api/review/state")
    def review_state(request: Request, seq: Optional[int] = None):
        record = _record(request)
        if seq is not None:
            # The review UI has no history scrubber.  Reject arbitrary historical folds instead of
            # giving an untrusted recipient an unbounded cache-key/CPU amplification primitive.
            raise HTTPException(400, "historical snapshots are not available through review links")
        return _scrub_json(srv.state_payload(_run(record)), omit_keys=_SUMMARY_OMIT_KEYS)

    @router.get("/api/review/config")
    def review_config(request: Request):
        record = _record(request)
        rd = _run(record)
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
        st = srv.state(_run(_record(request)))
        return _review_cost(st.llm_cost)

    @router.get("/api/review/nodes/{nid}/metrics")
    def review_node_metrics(nid: int, request: Request):
        rd = _run(_record(request))
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
        record = _record(request)
        _evidence(record)
        if seq is not None:
            raise HTTPException(400, "historical node evidence is not available through review links")
        rd = _run(record)
        st = srv.state(rd)
        node = st.nodes.get(nid)
        if node is None:
            raise HTTPException(404, "no such node at requested sequence" if seq is not None else "no such node")
        out = node.model_dump(mode="json")
        # Evidence is explicit opt-in, but still run the normal secret scrub before disclosure.  Do
        # not attach spans.jsonl: it contains model prompts/tool outputs and is a live sidecar rather
        # than an event-versioned fact.
        for key in ("code", "stdout_tail", "error"):
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
            token, record = srv.reviews.create(
                rd.name, ttl_seconds=body.ttl_seconds, include_evidence=body.include_evidence)
        except ReviewError as exc:
            raise HTTPException(400, str(exc)) from exc
        # The bearer is returned exactly once.  reviews.json contains only its digest.
        return {"ok": True, "token": token, "path": f"review#/{token}", **record}

    @router.get("/api/runs/{run_id}/reviews")
    def list_reviews(run_id: str):
        rd = srv.run_dir(run_id)
        return {"links": srv.reviews.list_for_run(rd.name)}

    @router.delete("/api/runs/{run_id}/reviews/{link_id}")
    def revoke_review(run_id: str, link_id: str):
        rd = srv.run_dir(run_id)
        try:
            record = srv.reviews.revoke(rd.name, link_id)
        except ReviewError as exc:
            raise _http_error(exc) from exc
        return {"ok": True, **record}

    return router
