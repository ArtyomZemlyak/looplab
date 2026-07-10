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

# A build older than this (seconds) is surfaced to the boss/assistant as possibly STUCK. Generous by
# design: a normal repo build (stages → plan → 8 implement steps) can legitimately run many minutes,
# so only a much longer wall-clock is worth a human's attention.
_STUCK_BUILD_SECONDS = 1200.0


def _client_tokens(client) -> Optional[dict]:
    """Best-effort token usage for ONE chat request. `make_llm_client` mints a fresh client per
    request, so its accountant totals already SUM every sub-call this turn made (the boss tool-loop
    can fire several). Shape matches the UI's `callTok` reader ({prompt, completion, total}). None
    when the client/model doesn't report usage (older local servers) — the UI just omits the badge."""
    acc = getattr(client, "accountant", None)
    if acc is not None and getattr(acc, "total_tokens", 0):
        # `context` = the peak single prompt = the real context-window size, NOT prompt_tokens which
        # SUMS the same context re-sent every tool-loop turn (billed, O(turns²)). The UI shows context.
        return {"prompt": acc.prompt_tokens, "completion": acc.completion_tokens,
                "total": acc.total_tokens, "calls": acc.calls,
                "context": getattr(acc, "peak_prompt", 0) or acc.prompt_tokens}
    u = getattr(client, "_last_usage", None) or {}
    if u:
        return {"prompt": u.get("prompt_tokens", 0), "completion": u.get("completion_tokens", 0),
                "total": u.get("total_tokens", 0), "calls": 1,
                "context": u.get("prompt_tokens", 0)}   # single call → its prompt IS the context
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


def _attention_states(st) -> str:
    """Signal-delivery (§1): the folded run states where a HUMAN intervention is most valuable but
    which the finished/live/stalled status line doesn't cover — paused, awaiting approval, a
    trust-flagged leader, a flagged-leakage task, or a node stuck mid-build. Every fact is already in
    the fold; surfacing it here means the boss/assistant can raise it instead of the operator having
    to spot it in the UI. Empty string when nothing needs attention."""
    lines: list[str] = []
    if getattr(st, "paused", False):
        lines.append("- PAUSED: the run is paused (the engine may be alive but idle) — `resume` to "
                     "continue it.")
    if getattr(st, "awaiting_approval", False):
        lines.append("- AWAITING APPROVAL: a result or eval spec is waiting on a human approve/ratify "
                     "before the run can proceed.")
    try:
        from looplab.events.replay import hard_flagged_ids
        hard = sorted(x for x in hard_flagged_ids(st) if x is not None)
    except Exception:  # noqa: BLE001 - context is best-effort
        hard = []
    if hard:
        ids = ", ".join(str(i) for i in hard[:5])
        lines.append(f"- TRUST FLAG: node(s) {ids} were flagged for a cheating/leakage pattern "
                     f"(trust_gate={getattr(st, 'trust_gate', 'audit')}) — review before trusting the "
                     "leader.")
    lk = getattr(st, "leakage", None)
    if isinstance(lk, dict) and lk.get("leak"):
        lines.append("- DATA LEAKAGE: the grounding leakage scan flagged the task inputs — the metric "
                     "may be inflated.")
    # A mid-build node is surfaced ONLY when the build has been running a long time — a build is set
    # the INSTANT it starts, so flagging every `st.building` would fire on essentially every query
    # during normal active work (noise, not an action item). We use the `started` event timestamp
    # (epoch seconds) to distinguish a genuinely STUCK build; a missing/zero ts skips the check.
    b = getattr(st, "building", None)
    if isinstance(b, dict) and b.get("node_id") is not None and not st.finished:
        started = b.get("started") or 0.0
        try:
            import time
            stuck_s = time.time() - float(started)
        except (TypeError, ValueError):
            stuck_s = 0.0
        if started and stuck_s > _STUCK_BUILD_SECONDS:
            lines.append(f"- STUCK BUILD: node {b.get('node_id')} has been building for "
                         f"{int(stuck_s // 60)} min (Researcher/Developer not yet done) — it may be "
                         "stuck; consider checking its live trace.")
    if not lines:
        return ""
    return "ATTENTION — run states that may need action:\n" + "\n".join(lines)


def _boss_context(st, nid: Optional[int], full: "Path", *, advisory: bool = False) -> str:
    """Richer grounding for the BOSS (action-router + advisory chat): the node brief PLUS the
    experiments digest (top / weakest / failures / themes — the working set) PLUS the latest
    agent-authored report. So the boss decides WITH context (what's been tried, what's winning,
    what failed) instead of just the single best node — and can still reach for the run-tools
    when even this isn't enough.

    `advisory=True` renders the RUN STATUS block for the action-LESS channels (/chat and /command's
    advisory fallback): same facts, but phrased as recommendations for the operator — the
    action-router's imperative "you MUST act: `resume`" wording would otherwise invite a chat reply
    that claims to have resumed a run it has no actions channel to touch (mega-review)."""
    from looplab.events.digest import experiments_digest
    # Run liveness UP FRONT: without it the boss can't tell a stalled run (engine died without
    # finishing — e.g. its only node crashed / never started) from a healthy one, so it tends to
    # only chat. A stalled run almost always needs the boss to ACT (resume + fix), not advise.
    if st.finished:
        status = (("RUN STATUS: finished. More experiments need the node budget raised first — "
                   "recommend the operator extend it; there's no room to run them until then.")
                  if advisory else
                  ("RUN STATUS: finished. Raise the node budget — budget(nodes=N) — before asking "
                   "for more experiments, else there's no room to run them."))
    elif _engine_alive(full):
        status = (("RUN STATUS: live — the engine is running and applies control actions between "
                   "nodes.")
                  if advisory else
                  "RUN STATUS: live — the engine is running and applies your actions between nodes.")
    else:
        status = (("RUN STATUS: STALLED — the engine is NOT running and the run hasn't finished (a "
                   "node likely crashed or never started). You have NO actions channel here — "
                   "RECOMMEND the operator resume the run (and debug/fix the failing node); never "
                   "claim you resumed it yourself.")
                  if advisory else
                  ("RUN STATUS: STALLED — the engine is NOT running and the run hasn't finished (a "
                   "node likely crashed or never started). To make progress you MUST act: `resume` to "
                   "restart the loop, and if a node is failing add a debug/inject step to fix it — "
                   "don't just advise."))
    parts = [status]
    attn = _attention_states(st)          # §1: paused / awaiting-approval / trust-flag / stuck-build
    if attn:
        parts.append(attn)
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
