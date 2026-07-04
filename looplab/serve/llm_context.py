"""LLM grounding helpers for the UI-side agents (chat / boss / genesis / reports): per-run
settings resolution, the run/node context briefs, and best-effort token accounting. Extracted
verbatim from `serve/server.py` (BACKLOG §4); `llm_settings` takes the app's `SettingsStore`
explicitly where the closure used to capture it."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from looplab.core.config import Settings
from looplab.serve.engine_proc import _engine_alive
from looplab.serve.settings_store import SettingsStore


def _client_tokens(client) -> Optional[dict]:
    """Best-effort token usage for ONE chat request. `make_llm_client` mints a fresh client per
    request, so its accountant totals already SUM every sub-call this turn made (the boss tool-loop
    can fire several). Shape matches the UI's `callTok` reader ({prompt, completion, total}). None
    when the client/model doesn't report usage (older local servers) — the UI just omits the badge."""
    acc = getattr(client, "accountant", None)
    if acc is not None and getattr(acc, "total_tokens", 0):
        return {"prompt": acc.prompt_tokens, "completion": acc.completion_tokens,
                "total": acc.total_tokens, "calls": acc.calls}
    u = getattr(client, "_last_usage", None) or {}
    if u:
        return {"prompt": u.get("prompt_tokens", 0), "completion": u.get("completion_tokens", 0),
                "total": u.get("total_tokens", 0), "calls": 1}
    return None


def llm_settings(store: SettingsStore, rd: Optional[Path] = None) -> "Settings":
    """Settings for the UI-side LLM calls (chat/command/suggest/report). ONE source of truth per
    run: when the run has a `config.snapshot.json`, its llm_model/base_url/temperature WIN — so
    chat (and the action-router) speak with the SAME model the run was launched with, which keeps
    the conversation reproducible and the trace honest even if the UI server's own env points at a
    different model. Falls back to the UI's saved LLM overrides + env when there's no snapshot (or
    for a run-less call). The api_key is NEVER read from the snapshot (it's masked there) — it
    always comes from the server env."""
    # The agentic tool-loop limits ride along so the UI-side agents (boss/genesis/scope-report)
    # honor the same per-run / global caps as the engine agents — unlimited by default.
    _keys = ("llm_model", "llm_base_url", "llm_temperature",
             "agent_max_turns", "agent_time_budget_s")
    over = {k: v for k, v in store.load_ui_settings().items()
            if k in _keys and v is not None}
    if rd is not None:
        try:
            cfg = json.loads((rd / "config.snapshot.json").read_text(encoding="utf-8"))
            for k in _keys:
                if cfg.get(k) is not None:
                    over[k] = cfg[k]
        except (OSError, json.JSONDecodeError, ValueError):
            pass   # no/!readable snapshot -> keep the UI/env defaults
    return Settings(**over)


def _node_context(st, nid: Optional[int], full: "Path") -> str:
    """A compact textual brief of the run (+ one focused experiment) to ground an LLM chat:
    goal, direction, best-so-far, and — when a node is selected — its idea/metric/code/error."""
    best = st.best()
    lines = [f"Run goal: {st.goal or st.task_id}", f"Optimization direction: {st.direction}",
             f"Nodes so far: {len(st.nodes)} ({len(st.evaluated_nodes())} evaluated)."]
    if best is not None:
        lines.append(f"Best node #{best.id}: metric={best.metric} "
                     f"params={best.idea.params} operator={best.operator}")
    if nid is not None and nid in st.nodes:
        n = st.nodes[nid]
        lines += ["", f"--- Focused experiment: node #{n.id} ---",
                  f"operator={n.operator} status={n.status} metric={n.metric} "
                  f"feasible={n.feasible}",
                  f"params={n.idea.params}", f"rationale: {n.idea.rationale}"]
        if n.error:
            lines.append(f"error ({n.error_reason}): {n.error[:400]}")
        if n.code:
            lines.append("solution.py:\n```python\n" + n.code[:2400] + "\n```")
    return "\n".join(lines)


def _boss_context(st, nid: Optional[int], full: "Path") -> str:
    """Richer grounding for the BOSS (action-router + advisory chat): the node brief PLUS the
    experiments digest (top / weakest / failures / themes — the working set) PLUS the latest
    agent-authored report. So the boss decides WITH context (what's been tried, what's winning,
    what failed) instead of just the single best node — and can still reach for the run-tools
    when even this isn't enough."""
    from looplab.events.digest import experiments_digest
    # Run liveness UP FRONT: without it the boss can't tell a stalled run (engine died without
    # finishing — e.g. its only node crashed / never started) from a healthy one, so it tends to
    # only chat. A stalled run almost always needs the boss to ACT (resume + fix), not advise.
    if st.finished:
        status = ("RUN STATUS: finished. Raise the node budget — budget(nodes=N) — before asking "
                  "for more experiments, else there's no room to run them.")
    elif _engine_alive(full):
        status = "RUN STATUS: live — the engine is running and applies your actions between nodes."
    else:
        status = ("RUN STATUS: STALLED — the engine is NOT running and the run hasn't finished (a "
                  "node likely crashed or never started). To make progress you MUST act: `resume` to "
                  "restart the loop, and if a node is failing add a debug/inject step to fix it — "
                  "don't just advise.")
    parts = [status]
    try:
        from looplab.core.hardware import operational_attention_points
        parts.append(operational_attention_points())
    except Exception:  # noqa: BLE001 - env-awareness is additive
        pass
    parts.append(_node_context(st, nid, full))
    dg = experiments_digest(st)
    if dg:
        parts.append(dg)
    # st.report is the _ReportOut dump (headline/verdict/champion_summary + lists) — NOT a 'content'
    # string — so stitch the high-signal fields into a readable brief. (A legacy/plain-string
    # report, or a {'content': ...} shape, is used as-is.)
    rep = getattr(st, "report", None)
    rtext = ""
    if isinstance(rep, str):
        rtext = rep
    elif isinstance(rep, dict):
        inner = rep.get("content")
        if isinstance(inner, str):
            rtext = inner                                  # legacy plain-string content
        else:
            src = inner if isinstance(inner, dict) else rep   # nested dict, or the _ReportOut dump
            segs = [str(src[k]) for k in ("headline", "verdict", "champion_summary") if src.get(k)]
            for k in ("what_worked", "what_didnt", "next_directions", "caveats"):
                v = src.get(k)
                if v:                                      # a malformed report may store a str/non-list
                    items = v if isinstance(v, (list, tuple)) else [v]
                    segs.append(f"{k.replace('_', ' ')}: " + "; ".join(str(x) for x in items))
            rtext = "\n".join(segs)
    if rtext:
        parts.append("\nLatest run report (agent-authored):\n" + rtext[:1800])
    return "\n".join(parts)
