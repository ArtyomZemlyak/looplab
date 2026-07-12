"""Pre-run BOSS routes: /api/research (topic brief) and /api/genesis (goal → editable run spec).
Handler bodies are verbatim moves from `serve/server.py::make_app` (BACKLOG §4). The genesis job
store is now the app-wide `JobRegistry` (shared with /api/jobs) — the poll endpoint keeps its
byte-identical response shape, including the `progress` field the scout loop streams."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

import anyio
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from looplab.core.config import Settings
from looplab.serve.assistant import safe_assistant_failure
from looplab.serve.protocol import JOB_DONE, JOB_RUNNING, JOB_UNKNOWN
from looplab.serve.serve_prompts import RESEARCH_BRIEF_SYSTEM, genesis_system
from looplab.serve.settings_store import _ALLOWED_FIELDS, _SECRET_FIELDS
from looplab.serve.routers.control import _defaults_backend_llm
from looplab.serve.routers.reports import _prior_learnings_index


class _GenesisSpec(BaseModel):
    """The BOSS's proposed plan for a brand-new run, bootstrapped from a one-line goal (there is no run
    yet, so this is the pre-run counterpart of the per-message _Plan). The UI shows it as an editable spec card and
    only launches on confirm — creating a run spends real tokens, so we propose-then-go, never silently
    auto-start. The task is EITHER an inline COMPOSABLE `task` object — NO `kind`, just the capability
    fields (e.g. {"goal":"...","direction":"max","repo":"/abs/path","cmd":{"command":["python","test.py"],
    "metric":{"reader":"stdout_json","key":"metric"}}}, or a bare {"competition":"<slug>"} for Kaggle) —
    OR a path to an existing catalogue `task_file`; `settings` carries only the engine overrides
    the goal implies (model, node budget, policy…), the rest fall back to the UI defaults."""
    run_id: str = ""          # invented kebab-case run name (we slugify + de-dup server-side)
    task: dict = {}           # inline COMPOSABLE task JSON (no `kind`) when authoring a fresh task
    task_file: str = ""       # OR a path from the catalogue (preferred when one matches)
    settings: dict = {}       # engine overrides only (llm_model, max_nodes, n_seeds, policy, …)
    rationale: str = ""       # one-line why-this-plan
    reply: str = ""           # conversational message to show in the genesis chat
    # Adaptation checklist: concrete steps the operator must take to make their target ready (chiefly
    # for kind="repo" — expose a metric, pin deps, choose the edit surface, protect the grader…). The
    # UI renders these as a to-do list under the spec card. Empty for a ready-to-run catalogue task.
    setup_steps: list[str] = []


def build_router(srv) -> APIRouter:
    router = APIRouter()
    root = srv.root

    # ------------------------------------------------------------------ pre-research a topic
    @router.post("/api/research")
    async def research(request: Request):
        """Best-effort LLM brief for a research topic, to prime a run. Optionally saved as a
        knowledge note (markdown) so the agentic-retrieval Researcher can read it (ADR-16).
        Degrades cleanly when no model endpoint is reachable."""
        body = await request.json()
        topic = (body.get("topic") or "").strip()
        if not topic:
            raise HTTPException(400, "topic is required")
        s = Settings(**{k: v for k, v in srv.settings.load_ui_settings().items()
                        if k in {"llm_model", "llm_base_url", "llm_temperature", "llm_api_key"}})
        try:
            from looplab.adapters.tasks import make_llm_client
            client = make_llm_client(s)
            msgs = [
                {"role": "system", "content": RESEARCH_BRIEF_SYSTEM},
                {"role": "user", "content": f"Research topic for an autonomous ML run:\n\n{topic}"},
            ]
            # Offload the blocking completion to a worker thread: this is an `async def` handler, so a
            # bare client.complete_text() would block the event loop (up to the 180s client timeout +
            # retries) and stall EVERY other client — including the live SSE streams — until it returns.
            text = await anyio.to_thread.run_sync(lambda: client.complete_text(msgs))
        except Exception as e:  # noqa: BLE001 - offline / no model -> soft fail
            return {"ok": False, "error": str(e), "model": s.llm_model, "base_url": s.llm_base_url}
        saved = None
        if body.get("save"):
            kd = srv.settings.load_ui_settings().get("knowledge_dir") or Settings().knowledge_dir
            if kd:
                d = Path(kd); d.mkdir(parents=True, exist_ok=True)
                slug = "".join(c if c.isalnum() else "-" for c in topic.lower())[:48].strip("-") or "topic"
                fp = d / f"research-{slug}.md"
                fp.write_text(f"# Research brief: {topic}\n\n{text}\n", encoding="utf-8")
                saved = str(fp)
        return {"ok": True, "text": text, "model": s.llm_model, "saved": saved}

    # ------------------------------------------------------------------ genesis (pre-run BOSS)
    def _normalize_genesis(spec: "_GenesisSpec", draft: dict) -> dict:
        """Turn the boss's raw proposal into a launch-ready, editable card: slugify + de-dup the run_id
        against existing run dirs, keep only known non-secret setting overrides, and invent a name when
        the model didn't give one. A REFINE turn (an existing draft) merges: fields the model omitted
        are kept from the prior card — a partial emit like {settings:{max_nodes:50}} must tweak the
        user's tuned spec, not wipe its task/name."""
        draft = draft if isinstance(draft, dict) else {}

        def _slug(s):
            return re.sub(r"(^-|-$)", "", re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()))[:40]

        task = spec.task if isinstance(spec.task, dict) and spec.task else (draft.get("task") or {})
        task_file = spec.task_file or draft.get("task_file") or ""
        base = (_slug(spec.run_id) or _slug(draft.get("run_id")) or _slug(task.get("competition"))
                or _slug(task.get("kind")) or _slug(Path(task_file).stem if task_file else "") or "run")
        run_id, n = base, 2
        # A name is "taken" only when it holds a REAL run (events.jsonl) — matches /api/start's 409 — so
        # a leftover empty dir (e.g. a validation-failed materialization) doesn't force a -2 suffix.
        while (root / run_id / "events.jsonl").exists():
            run_id, n = f"{base}-{n}", n + 1
        merged_settings = {**(draft.get("settings") or {}), **(spec.settings or {})}
        settings = {k: v for k, v in merged_settings.items()
                    if k in _ALLOWED_FIELDS and k not in _SECRET_FIELDS and v is not None}
        # CLI parity (mega-review P10): show the backend this run WILL launch with. The AUTHORITATIVE
        # default now lives in /api/start (`routers/control.py::_defaults_backend_llm` — the one
        # funnel every launch goes through), so this card-level injection is DISPLAY-ONLY sugar: the
        # operator sees the inferred `backend=llm` on the editable spec card and can override it
        # BEFORE confirming, instead of the default appearing silently at launch. Delegating to the
        # SAME shared predicate means the card can never disagree with what /api/start actually
        # spawns, and task_file (catalogue) cards are covered too. Broad best-effort guard: the card
        # must RENDER even when this hint fails (the predicate's excepts are narrowed to the task
        # read/normalize — anything else, e.g. a broken settings store, would otherwise 500 the whole
        # plan); a missing hint just means the default appears at launch, where /api/start keeps the
        # narrow-except semantics as the authoritative gate.
        try:
            if _defaults_backend_llm(task, task_file, settings, srv.settings.load_ui_settings()):
                settings["backend"] = "llm"
        except Exception:  # noqa: BLE001 - display-only sugar; /api/start re-applies the real rule
            pass
        steps = [str(s).strip() for s in (spec.setup_steps or []) if str(s).strip()][:12] \
            or list(draft.get("setup_steps") or [])
        return {"run_id": run_id, "task": task, "task_file": task_file,
                "settings": settings, "rationale": spec.rationale or draft.get("rationale") or "",
                "setup_steps": steps}

    # Genesis runs an AGENTIC, multi-turn tool loop (the boss reads the repo before planning). Done
    # synchronously that can outlast a UI proxy's gateway timeout (it 504'd behind JupyterHub). So the
    # POST runs the loop as a background JOB and waits briefly: a fast model finishes inside the inline
    # wait and the spec comes back in the one request (no polling, no added latency); a slow one hands
    # back a job_id the UI polls. No step cap is imposed for speed — the agent decides how long it needs.
    # (The job store is the app-wide JobRegistry — srv.jobs — shared with /api/jobs.)
    # seconds the POST waits inline before handing back a job_id (env-tunable; 0 = always async)
    _GENESIS_INLINE_WAIT = float(os.environ.get("LOOPLAB_GENESIS_INLINE_WAIT", "8.0"))

    @router.post("/api/genesis")
    async def genesis(request: Request):
        """Pre-run BOSS: turn a one-line goal into an editable run spec (name + task + key settings).
        No run exists yet, so this grounds on the task catalogue + registered kinds + the current default
        settings and PROPOSES a plan the UI shows as an editable card — we launch on confirm via
        /api/start, never here (creating a run spends real tokens). Refinement turns pass the prior
        `draft` so the boss edits it in place. Degrades cleanly when no model is reachable."""
        from looplab.adapters.tasks import kinds
        body = await request.json()
        msgs = body.get("messages") or []
        instruction = (body.get("instruction") or "").strip()
        draft = body.get("draft") or {}
        catalogue = srv.list_tasks_fn().get("tasks", [])
        cat_lines = "\n".join(
            f"- {t['name']} (kind={t['kind']}, path={t['path']}): {str(t.get('goal', ''))[:90]}"
            for t in catalogue[:40]) or "(none)"
        defaults = srv.settings.resolved_settings()
        key_defaults = {k: defaults.get(k) for k in
                        ("llm_model", "llm_base_url", "llm_temperature", "max_nodes", "n_seeds", "policy")}
        sys_prompt = genesis_system(kinds(), key_defaults, cat_lines)
        prior = _prior_learnings_index(srv.reports_dir)
        if prior:
            sys_prompt += ("\nPrior cross-run learnings (portfolio reports from earlier runs — use them "
                           "to pick a better task / model / settings, and reference them in your reply "
                           "when relevant):\n" + prior + "\n")
        if draft:
            sys_prompt += ("\nThe user is refining this current draft — edit it in place, keeping the "
                           "fields they didn't ask to change:\n" + json.dumps(draft)[:1200])
        try:
            from looplab.engine.genesis import REPO_AUTONOMY_GUIDE
            from looplab.core.hardware import operational_attention_points
            sys_prompt += "\n\n" + REPO_AUTONOMY_GUIDE + "\n\n" + operational_attention_points()
        except Exception:  # noqa: BLE001 - env-awareness is additive; never block genesis
            pass
        convo = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in msgs)
        user = (f"Goal: {instruction}" if instruction else "") + (f"\n\nConversation:\n{convo}" if convo else "")
        from looplab.core.parse import parse_structured
        _soft = {"run_id": "", "task": {}, "task_file": "", "settings": {}, "rationale": "", "setup_steps": []}
        gset = srv.llm_settings(None)   # carries the agent-loop limits (unlimited by default)
        try:
            client = srv.make_llm_client(gset)
        except Exception as e:  # noqa: BLE001 - offline / no model -> soft fail with a usable message
            failure = safe_assistant_failure(e)
            return {"ok": False, "error": failure["error"], "error_kind": failure["error_kind"],
                    "spec": _soft, "reply": "The model provider is unavailable. Check Settings and retry; you can still use the manual form."}
        base_msgs = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}]

        def _plan_agentic(on_step=None) -> Optional["_GenesisSpec"]:
            """AGENTIC: let the boss actually INSPECT the repo on disk (read-only) before authoring the
            spec — so a repo task is grounded in the real README / entry-script / results, not a
            promise. Returns None when the model can't drive tools (caller does a single structured call)."""
            from looplab.agents.agent import drive_tool_loop, loop_opts_from_settings
            from looplab.tools.reposcout import RepoScoutTools
            tools = RepoScoutTools([Path.home(), root, root.parent])
            tool_sys = sys_prompt + (
                "\n\nYou have READ-ONLY tools to inspect this machine: list_dir(path), read_file(path), "
                "find_files(root, pattern), grep(pattern, root). When the user points you at a repo (an editable_path or a path "
                "they mention), ACTUALLY use them BEFORE emitting: list the repo, read its README for the "
                "train/run command, read the eval/entry script (e.g. test.py) to see how the metric is "
                "printed AND what arguments / config file it accepts, note results/requirements/data and "
                "config files — then ground the eval command, metric reader + key/pattern, edit_surface, any data mount "
                "and (if it's argument- or config-driven) the params_style/config choice in what you read. "
                "If there is NO entry/train script yet, say so and plan for the agent to write it (command "
                "-> a file inside edit_surface). Don't just SAY you'll look — look, then call `emit` once.")
            emit_spec = {"type": "function", "function": {
                "name": "emit", "description": "Emit the final run plan (run_id, task, settings, "
                "setup_steps, reply, rationale).", "parameters": _GenesisSpec.model_json_schema()}}
            box: dict = {}
            def _fin(args):
                try:
                    box["c"] = _GenesisSpec(**{k: v for k, v in (args or {}).items()
                                               if k in _GenesisSpec.model_fields})
                except Exception:  # noqa: BLE001 - junk emit -> empty spec (still returns a usable card)
                    box["c"] = _GenesisSpec()
                return box["c"]
            def _fb(msgs):
                # Loop ran but the model never called `emit` (drove tools without finalizing, OR ignored
                # tools). Finalize from the ACCUMULATED messages (which carry what it read) rather than
                # discarding that and making the caller fire a fresh single-shot plan — saves a whole
                # extra LLM round-trip and keeps the repo context the model just gathered.
                if box.get("c"):
                    return box["c"]
                try:
                    box["c"] = parse_structured(client, msgs + [{"role": "user",
                                "content": "Now emit the final plan. Either set task_file to a "
                                "catalogue entry, OR author a complete inline COMPOSABLE `task` — "
                                "goal + direction + the capability fields you have (repo / dataset / "
                                "cmd / competition), NO `kind` — never leave the task "
                                "empty. Write a clear two-to-three-sentence `reply`."}], _GenesisSpec,
                                defaults.get("llm_parser", "tool_call"))
                except Exception:  # noqa: BLE001 - even a forced emit failed -> blank (usable) card
                    box["c"] = _GenesisSpec()
                return box["c"]
            try:
                # The AGENT decides how many reads/turns it needs — limits are CONFIG-DRIVEN
                # (Settings.agent_max_turns / agent_time_budget_s) and default to UNLIMITED, not the
                # old hardcoded 1000-turn / 600s ceiling. The endpoint runs this in a background job,
                # so a long scout never blocks the HTTP request / trips a proxy timeout; set a positive
                # cap in settings only if you want to bound a pathological model that never emits.
                drive_tool_loop(client, tools, [{"role": "system", "content": tool_sys},
                                                {"role": "user", "content": user}],
                                emit_spec, max_turns=getattr(gset, "agent_max_turns", 0),
                                time_budget_s=getattr(gset, "agent_time_budget_s", 0.0),
                                finalize=_fin, fallback=_fb, on_step=on_step,
                                **loop_opts_from_settings(gset))     # B1 stuck (+ C1/C2 if configured)
            except Exception:  # noqa: BLE001 - the model/endpoint can't drive tools AT ALL -> single-shot
                return None
            return box.get("c")

        def _compute_plan(on_step=None) -> dict:
            """The whole agentic plan (runs in a worker thread): scout the repo + emit, with the legacy
            single structured call as the fallback when the model can't drive tools. Returns the final
            response dict the UI consumes (inline or via the poll endpoint)."""
            try:
                spec = _plan_agentic(on_step)
                if spec is None:    # tool loop unsupported -> plain structured call (legacy single-shot)
                    spec = parse_structured(client, base_msgs, _GenesisSpec,
                                            defaults.get("llm_parser", "tool_call"))
            except Exception as e:  # noqa: BLE001 - planning failed -> soft fail with a usable message
                return {"ok": False, "error": str(e), "spec": _soft,
                        "reply": f"Couldn't plan this ({e}). You can still use the manual form."}
            return {"ok": True, "spec": _normalize_genesis(spec, draft),
                    "reply": spec.reply or "Here's a plan — tweak the card and launch."}

        def _compute(set_progress) -> dict:
            def _on_step(ev: dict) -> None:
                # Turn a raw tool event into a short human line so the UI can show what the boss is doing
                # instead of an opaque spinner (the "it just thinks for ages" complaint is opacity, not
                # latency — we add NO budget/cap here). Best-effort; this only annotates the running job.
                tool = (ev or {}).get("tool", "")
                arg = str((ev or {}).get("arg", ""))
                short = arg.rsplit("/", 1)[-1] if arg else ""
                label = ({"read_file": f"reading {short}", "list_dir": f"listing {short or 'the repo'}",
                          "find_files": f"searching {short or 'files'}"}.get(tool)
                         or (f"{tool} {short}".strip() if tool else "scouting the repo"))
                set_progress({"label": label, "step": int((ev or {}).get("turn", 0)) + 1})

            # Guard the WHOLE plan: an exception outside _compute_plan's own try (e.g. a parse
            # returning None → AttributeError in normalize) must still produce a done result, or the
            # poll endpoint reports "running" forever and the UI waits out its full timeout blind.
            try:
                return _compute_plan(_on_step)
            except Exception as e:  # noqa: BLE001 - surface as a job error, never a wedged job
                return {"ok": False, "error": f"planning failed: {e}"}

        # Adaptive fast-path — the shared srv.jobs.run_as_job spawn+inline-wait (same funnel as the
        # assistant/boss/report routes): a quick model finishes inside the inline wait and the spec is
        # returned in THIS request (no polling round-trips, no added latency for a normal environment);
        # a slow one returns a job_id the UI polls (no 504). `with_progress` threads the scout-step
        # annotations into the job record the /api/genesis/{job_id} poll surfaces.
        return await srv.jobs.run_as_job(_compute, inline_wait=_GENESIS_INLINE_WAIT, with_progress=True)

    @router.get("/api/genesis/{job_id}")
    def genesis_job(job_id: str):
        """Poll a pending genesis plan (the agentic loop runs in the background so a slow model doesn't
        504 behind a proxy). `running` until the boss finishes; then the full plan; `unknown` if the
        job expired/was evicted (the UI should re-POST)."""
        j = srv.jobs.get(job_id)
        if not j:
            return {"status": JOB_UNKNOWN}
        if j.get("status") != JOB_DONE:
            # Carry the latest scout step so the UI can show "reading README.md…" instead of an
            # opaque spinner while a slow boss inspects the repo (transparency, not a time cap).
            return {"status": JOB_RUNNING, "progress": j.get("progress")}
        return {**j["result"], "status": JOB_DONE}

    return router
