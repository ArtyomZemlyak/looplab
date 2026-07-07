"""Miscellaneous routes: UI default settings (+ the secret store), the task catalogue, LLM health,
the GPU monitor, files-as-truth authoring and the memory viewer. Bodies are verbatim moves from
`serve/server.py::make_app` (BACKLOG §4).

ORDER IS LOAD-BEARING inside this router and for its placement: the generic authoring route
`GET /api/{kind}` full-matches ANY single-segment /api GET, so every such literal route must be
registered before it — this router therefore registers settings/tasks/health/gpu first, is included
LAST among the /api routers by `make_app`, and keeps `/api/memory` after `/api/{kind}` exactly as
the original inline registration order had it."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from looplab.core.config import Settings
from looplab.events.eventstore import iter_jsonl
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
        body = await request.json()
        incoming = body.get("settings", body) or {}
        # Keep only known, non-secret fields whose value differs from the engine default — the file
        # stays a small, readable diff rather than a full mirror of every Settings field. Diff
        # against the PROFILE-expanded defaults: the form echoes the expanded snapshot back, and
        # diffing against bare defaults would persist every profile value as an explicit override
        # (a one-way ratchet the profile selector could never undo) while dropping an explicit
        # knob that happens to equal the bare default (breaking "explicit knob wins").
        try:
            base = Settings(profile=incoming.get("profile") or "default").model_dump()
        except Exception:  # noqa: BLE001 — unknown profile: fall back to bare defaults
            base = Settings().model_dump()
        # Fields the form merely ECHOES from the previous resolved snapshot are not user edits:
        # when the profile changes, those echoes must fall away with the old profile, not stick.
        prev = store.resolved_settings()
        overrides = {}
        for k, v in incoming.items():
            if k not in _ALLOWED_FIELDS or k in _SECRET_FIELDS or v is None or base.get(k) == v:
                continue
            if k != "profile" and prev.get(k) == v and incoming.get("profile") != prev.get("profile"):
                continue                       # unchanged echo of the old profile's expansion
            overrides[k] = v
        store.write_ui_settings(overrides)
        return {"ok": True, "settings": store.resolved_settings(), "overrides": overrides}

    @router.put("/api/settings/secret")
    async def put_secret(request: Request):
        """Store (or clear) a secret credential securely. The value is written owner-only to
        secrets.json (never ui_settings.json / a run snapshot) and applied to the server + spawned
        engines as env. The response only reports whether a value is now set — never the value."""
        body = await request.json()
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
    def _f(s: str) -> Optional[float]:
        try:
            return float(s)
        except (TypeError, ValueError):
            return None

    @router.get("/api/gpu")
    def gpu():
        try:
            q = ("--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,"
                 "power.draw")
            out = subprocess.run(["nvidia-smi", q, "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=4)
            if out.returncode != 0 or not out.stdout.strip():
                return {"available": False}
            gpus = []
            for line in out.stdout.strip().splitlines():
                p = [c.strip() for c in line.split(",")]
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
        s = Settings()
        out = {"dir": None, "cases": [], "lessons": [], "notes": []}
        if not s.memory_dir:
            return out
        md = Path(s.memory_dir)
        out["dir"] = str(md)
        for f in sorted(md.glob("*.jsonl")) if md.exists() else []:
            rows = list(iter_jsonl(f))
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
        s = Settings()
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
