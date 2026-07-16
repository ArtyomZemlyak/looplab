"""Miscellaneous routes: UI default settings (+ the secret store), the task catalogue, LLM health,
the GPU monitor, files-as-truth authoring and the memory viewer. Bodies are verbatim moves from
`serve/server.py::make_app` (BACKLOG §4).

ORDER IS LOAD-BEARING inside this router and for its placement: the generic authoring route
`GET /api/{kind}` full-matches ANY single-segment /api GET, so every such literal route must be
registered BEFORE it — this router therefore registers settings/tasks/health/gpu AND `/api/memory`
first (memory before `/api/{kind}`, else it's swallowed as an unknown kind → 404, the empty-Memory-
panel bug), and is included LAST among the /api routers by `make_app`."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from looplab.core.config import Settings
from looplab.events.eventstore import read_jsonl_lenient
from looplab.serve.settings_store import _ALLOWED_FIELDS, _SECRET_ENV, _SECRET_FIELDS


def build_router(srv) -> APIRouter:
    router = APIRouter()
    store = srv.settings

    # ------------------------------------------------------------------ settings (UI defaults)
    # The engine has no settings server (ADR-18); these are UI-chosen DEFAULTS for new runs,
    # persisted at <run-root>/ui_settings.json and applied to a spawned run as LOOPLAB_* env.
    @router.get("/api/settings")
    def get_settings():
        s = Settings()                        # build once — each Settings() now also reads .env off disk
        defaults = s.model_dump()
        defaults.pop("llm_api_key", None)
        return {"settings": store.resolved_settings(s), "overrides": store.load_ui_settings(),
                "defaults": defaults}

    @router.put("/api/settings")
    async def put_settings(request: Request):
        try:
            body = await request.json()
        except Exception as exc:  # malformed JSON is a client error, never a server traceback
            raise HTTPException(400, "settings payload must be valid JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "settings payload must be a JSON object")
        incoming = body.get("settings", body)
        if not isinstance(incoming, dict):
            raise HTTPException(400, "settings must be a JSON object")
        # Keep only known, non-secret fields whose value differs from the engine default — the file
        # stays a small, readable diff rather than a full mirror of every Settings field. Diff
        # against the PROFILE-expanded defaults: the form echoes the expanded snapshot back, and
        # diffing against bare defaults would persist every profile value as an explicit override
        # (a one-way ratchet the profile selector could never undo) while dropping an explicit
        # knob that happens to equal the bare default (breaking "explicit knob wins").
        # Atomic rename prevents torn JSON but cannot protect this larger load→merge→write cycle:
        # two concurrent disjoint PUTs must observe one another instead of losing the first rename.
        with store.ui_settings_transaction():
            current = store.load_ui_settings()
            prev = store.resolved_settings()
            candidate = dict(current)
            for k, v in incoming.items():
                if k not in _ALLOWED_FIELDS or k in _SECRET_FIELDS:
                    continue
                if k == "agent_control" and isinstance(v, dict):
                    # Governance is a nested sparse PATCH too. Start from the resolved map so
                    # the first customization retains shipped defaults; sparse edits from stale tabs
                    # then merge by governed setting instead of replacing one another wholesale.
                    old_control = prev.get("agent_control")
                    merged_control = dict(old_control) if isinstance(old_control, dict) else {}
                    for setting_key, roles in v.items():
                        if roles is None:
                            merged_control.pop(setting_key, None)
                        else:
                            merged_control[setting_key] = roles
                    candidate[k] = merged_control
                elif v is None:
                    candidate.pop(k, None)
                else:
                    candidate[k] = v
            profile = candidate.get("profile") or "default"
            try:
                base = Settings(profile=profile).model_dump()
            except Exception:  # noqa: BLE001 — unknown profile: fall back to bare defaults
                base = Settings().model_dump()
            # Fields the form merely ECHOES from the previous resolved snapshot are not user edits:
            # when the profile changes, those echoes must fall away with the old profile, not stick.
            overrides = {}
            profile_changed = "profile" in incoming and profile != prev.get("profile")
            for k, v in candidate.items():
                if k not in _ALLOWED_FIELDS or k in _SECRET_FIELDS:
                    continue
                if k == "profile":
                    if v != Settings.model_fields["profile"].default:
                        overrides[k] = v
                    continue
                if base.get(k) == v:
                    continue
                if profile_changed and k in incoming and k not in current and prev.get(k) == v:
                    continue                       # unchanged echo of the old profile's expansion
                overrides[k] = v
            try:
                Settings(**overrides)
            except Exception as exc:  # noqa: BLE001 - reject before persisting a poison configuration
                raise HTTPException(422, f"invalid settings: {exc}") from exc
            # PATCH-like contract: omission preserves opaque overrides; explicit null/default removes one.
            store.write_ui_settings(overrides)
            return {"ok": True, "settings": store.resolved_settings(), "overrides": overrides}

    @router.put("/api/settings/secret")
    async def put_secret(request: Request):
        """Store (or clear) a secret credential securely. The value is written owner-only to
        secrets.json (never ui_settings.json / a run snapshot) and applied to the server + spawned
        engines as env. The response only reports whether a value is now set — never the value."""
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "secret payload must be valid JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "secret payload must be a JSON object")
        key = body.get("key")
        if key not in _SECRET_ENV:
            raise HTTPException(400, f"unknown secret {key!r} (known: {sorted(_SECRET_ENV)})")
        value = body.get("value")
        if value is not None and not isinstance(value, str):
            raise HTTPException(400, "value must be a string (or null to clear)")
        store.store_secret(key, (value or "").strip())
        return {"ok": True, "key": key, "set": bool((value or "").strip())}

    # ------------------------------------------------------------------ task catalogue
    @router.get("/api/tasks")
    def list_tasks():
        """Discover runnable task JSON files (the `examples/` catalogue by default, plus any in the
        run-root) so the launch dialog can offer a pick-list instead of a raw path."""
        repo = Path(__file__).resolve().parents[3]   # routers/ is one level deeper than server.py was
        dirs = [repo / "examples", srv.root]
        env_dir = os.environ.get("LOOPLAB_TASKS_DIR")
        if env_dir:
            dirs.insert(0, Path(env_dir))
        seen, out = set(), []
        for d in dirs:
            if not d.exists():
                continue
            for p in sorted(d.glob("*.json")):
                rp = str(p.resolve())
                if rp in seen:
                    continue
                seen.add(rp)
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(data, dict) or not ("goal" in data or "id" in data):
                    continue
                out.append({"path": rp, "name": p.name,
                            "kind": data.get("kind", "quadratic"),
                            "id": data.get("id"), "goal": data.get("goal", ""),
                            "direction": data.get("direction")})
        return {"tasks": out}

    # Late-bind the catalogue for the genesis boss (it grounds its plan on the task list), breaking
    # the route-calls-route dependency between this router and `routers/genesis.py`.
    srv.list_tasks_fn = list_tasks

    @router.get("/api/health")
    def health():
        """P1-3 zero-model liveness: the ONE /api/ route that stays open without a UI token, so a
        monitor can probe process reachability WITHOUT the X-LoopLab-Token AND without triggering a
        billable model completion (that is /api/llm/health, which stays token-gated under deny-default).
        Pure process-liveness — never touches the LLM, a run, or any sensitive state."""
        return {"ok": True, "service": "looplab"}

    @router.get("/api/llm/health")
    def llm_health():
        """Liveness self-test for the configured LLM endpoint (the UI equivalent of `LoopLab
        smoke`): pings the model with a one-word prompt. Never raises — returns reachability so
        the UI can warn before a run launches against a dead endpoint."""
        s = srv.llm_settings()
        info = {"model": s.llm_model, "base_url": s.llm_base_url}
        try:
            # Bound the probe well under any proxy gateway timeout: a reachable-but-hanging endpoint
            # (queued model, heartbeat-only body) must NOT make the health check itself 504 — the very
            # thing it exists to warn about. (Connection-refused already fails fast.) Env-tunable.
            hc_timeout = float(os.environ.get("LOOPLAB_HEALTHCHECK_TIMEOUT", "10.0"))
            client = srv.make_llm_client(s, timeout=hc_timeout)
            txt = client.complete_text([{"role": "user", "content": "Reply with one word: ready"}])
            return {"ok": True, "text": (txt or "").strip()[:80], **info}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e), **info}

    # ------------------------------------------------------------------ GPU monitor
    @router.get("/api/gpu")
    def gpu():
        try:
            from looplab.core.hardware import query_nvidia_smi
            from looplab.core.parse import to_float as _f
            rows = query_nvidia_smi(
                "name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                timeout=4)
            if rows is None:
                return {"available": False}
            gpus = []
            for p in rows:
                if len(p) >= 6:
                    gpus.append({"name": p[0], "util": _f(p[1]), "mem_used": _f(p[2]),
                                 "mem_total": _f(p[3]), "temp": _f(p[4]), "power": _f(p[5])})
            return {"available": True, "gpus": gpus}
        except Exception:  # noqa: BLE001 - no GPU / no nvidia-smi -> soft fail
            return {"available": False}

    @router.get("/api/memory")
    def memory():
        # Cross-run memory dir holds several tiers in separate .jsonl files — SPLIT them by filename so
        # the UI can show cases / lessons / notes each with their own shape. MUST be declared BEFORE the
        # `/api/{kind}` catch-all below, else it's swallowed as an unknown kind (→ 404, the reason the
        # Memory panel was silently empty). `cases` stays populated for back-compat.
        s = srv.global_settings()
        out = {"dir": None, "cases": [], "lessons": [], "notes": []}
        if not s.memory_dir:
            return out
        md = Path(s.memory_dir)
        out["dir"] = str(md)
        for f in sorted(md.glob("*.jsonl")) if md.exists() else []:
            # These are MUTABLE stores (rewritten/compacted in place), so skip-and-continue: one
            # damaged line must not hide the rest of the Memory panel (iter_jsonl would stop there).
            # cases.jsonl is stdlib-json-written (NaN literals possible); lessons/notes are orjson —
            # stdlib json parses BOTH (a superset), so one parser is safe on this read-only panel.
            rows = read_jsonl_lenient(f, loads=json.loads)
            nm = f.name.lower()
            if "lesson" in nm:
                out["lessons"].extend(rows)
            elif "note" in nm or "meta" in nm:
                out["notes"].extend(rows)
            else:                                    # cases.jsonl (or any other tier) → cases
                out["cases"].extend(rows)
        return out

    # ------------------------------------------------------------------ authoring (files-as-truth)
    def _author_dir(kind: str) -> Optional[Path]:
        s = srv.global_settings()
        m = {"prompts": s.prompt_dir, "skills": s.skills_dir, "knowledge": s.knowledge_dir}
        d = m.get(kind)
        return Path(d) if d else None

    @router.get("/api/{kind}")
    def list_author(kind: str):
        if kind not in ("prompts", "skills", "knowledge"):
            raise HTTPException(404, "unknown kind")
        d = _author_dir(kind)
        if d is None or not d.exists():
            return {"dir": (str(d) if d else None), "files": []}
        files = [{"name": p.name, "text": p.read_text(encoding="utf-8", errors="replace")}
                 for p in sorted(d.glob("*.md"))]
        return {"dir": str(d), "files": files}

    @router.put("/api/{kind}/{name}")
    async def write_author(kind: str, name: str, request: Request):
        if kind not in ("prompts", "skills", "knowledge"):
            raise HTTPException(404, "unknown kind")
        d = _author_dir(kind)
        if d is None:
            raise HTTPException(400, f"no {kind} dir configured (set LOOPLAB_{kind.upper()}_DIR)")
        d.mkdir(parents=True, exist_ok=True)
        target = (d / name).resolve()
        if d.resolve() not in target.parents:    # path-traversal guard
            raise HTTPException(400, "bad name")
        body = await request.body()
        target.write_text(body.decode("utf-8"), encoding="utf-8")  # engine hot-reloads on next run
        return {"ok": True, "name": name}

    return router
