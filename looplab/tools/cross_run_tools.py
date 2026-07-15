"""Cross-run READ tool (PART V §22): agentic, read-only access to the §21.20 cross-run knowledge so any
reasoning role (Researcher, Strategist, Developer, deep-research) can ASK — mid-loop, on demand —

  - `cross_run_prior_attempts(idea)`  : has this concept been tried across runs, and how did it go?
  - `cross_run_claims(query, contested): what does the accumulated evidence support vs contradict?
  - `cross_run_atlas()`               : the portfolio map — explored / thin / contradictory.

Pure reads over `memory_dir` (`lessons.jsonl` + `concept_capsules.jsonl`) via the shipped read-models
(`claim_assessments`, `portfolio_concept_overview`, `portfolio_atlas`). ADVISORY ONLY: an agent may CITE
what it finds, but this tool exposes NO mutation — cross-run truth is written only by the engine (facts) or
ratified by the operator (§22.4). `role` scopes the claim stream so the Developer sees dev-routed lessons
(mirroring the role-routed cross-run LESSONS), not the R&D claim stream.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from looplab.tools._base import fn_spec

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _toks(s: str) -> set[str]:
    return {w for w in _WORD.findall((s or "").casefold()) if len(w) > 2}


class CrossRunTools:
    """Read-only cross-run knowledge for the tool-loop. `role` ∈ {"researcher","developer"} scopes the
    claims to that role's lessons (+ shared/untagged); anything else sees all. Never raises from execute."""

    def __init__(self, memory_dir: str | Path | None, *, role: str = "researcher"):
        self.dir = Path(memory_dir) if memory_dir else None
        self.role = str(role or "researcher")
        self._task_id = ""
        self._scope_terms: set[str] = set()

    def bind_state(self, state, parent=None) -> None:
        """Learn the CURRENT run's scope so queries reach SIMILAR tasks, not the whole portfolio (the
        live-test leak fix): scope = same `task_id` OR a shared goal keyword. When the tool is used
        UNBOUND (CLI/human), no scope is set and every row passes — the human wants portfolio-wide."""
        if state is None:
            return
        self._task_id = str(getattr(state, "task_id", "") or "")
        self._scope_terms = _toks(getattr(state, "goal", "") or "")

    def _in_scope(self, row: dict) -> bool:
        """True when the row belongs to the bound run's scope (same task, or overlapping goal terms), or
        when the tool is unbound. Goal terms come from the row's `fingerprint` bare tokens (the kind:/dir:/
        metric: prefixed tokens are excluded). Exact `task_id` always passes — robust even when a legacy
        (ASCII) fingerprint dropped a non-Latin goal's keywords."""
        if not self._task_id and not self._scope_terms:
            return True                                        # unbound -> portfolio-wide
        if self._task_id and str(row.get("task_id") or "") == self._task_id:
            return True
        fp = row.get("fingerprint")
        if isinstance(fp, list) and self._scope_terms:
            row_terms = {t for t in fp if isinstance(t, str) and ":" not in t}
            if row_terms & self._scope_terms:
                return True
        return False

    def specs(self) -> list[dict]:
        if not self.dir:
            return []
        return [
            fn_spec("cross_run_prior_attempts",
                "Check whether an idea's CONCEPTS were already explored in earlier runs across the "
                "portfolio, and with what outcome — so you don't re-invent a settled result (or can "
                "deliberately extend/replicate it). Surfaces prior runs, never rejects.",
                {"idea": {"type": "string", "description": "The idea / technique / concept to look up "
                          "(e.g. 'hard-negative mining', 'distillation')."}},
                ["idea"]),
            fn_spec("cross_run_claims",
                "What the ACCUMULATED cross-run evidence suggests: claims with support vs opposition and "
                "an epistemic state (supported / refuted / mixed / inconclusive). `contested` shows only "
                "the claims the portfolio disagrees with itself on — the highest-signal ones to resolve.",
                {"query": {"type": "string", "description": "Keywords to filter claims (blank = top "
                           "claims by evidence)."},
                 "contested": {"type": "boolean", "description": "Only mixed (support+oppose) claims."}},
                []),
            fn_spec("cross_run_atlas",
                "A portfolio MAP: which concepts have been explored (and in how many runs), which are "
                "thinly explored (a single run — a gap to consider), and which claims are contradictory. "
                "Use it to pick an under-explored or unresolved direction.",
                {}, []),
        ]

    def _load(self, fname: str) -> list[dict]:
        from looplab.events.eventstore import read_jsonl_lenient
        p = self.dir / fname
        return read_jsonl_lenient(p, loads=json.loads, dicts_only=True) if p.exists() else []

    def _role_lessons(self) -> list[dict]:
        """Lessons visible to this role AND in scope: the role's own + shared/untagged (mirrors the
        role-routed cross-run lesson priors), scoped to the bound run's task (portfolio-wide when unbound).
        An unknown role sees every role."""
        lessons = [lz for lz in self._load("lessons.jsonl") if self._in_scope(lz)]
        if self.role not in ("researcher", "developer"):
            return lessons
        return [lz for lz in lessons if str(lz.get("role") or "") in ("", self.role)]

    def _scoped_capsules(self) -> list[dict]:
        from looplab.engine.memory import ConceptCapsuleStore
        p = self.dir / "concept_capsules.jsonl"
        caps = ConceptCapsuleStore(p).all() if p.exists() else []
        return [c for c in caps if self._in_scope(c)]

    def execute(self, name: str, args: dict) -> str:
        # ToolProvider contract: execute NEVER raises (a junk arg must read as a tool error, not crash
        # the agent phase — drive_tool_loop does not guard tools.execute).
        try:
            return self._execute(name, args or {})
        except Exception as e:  # noqa: BLE001
            return f"(cross-run tool error: {e})"

    def _execute(self, name: str, args: dict) -> str:
        if not self.dir:
            return "(no cross-run memory configured)"
        from looplab.engine.claims import claim_assessments, portfolio_atlas
        from looplab.engine.memory import portfolio_concept_overview

        if name == "cross_run_prior_attempts":
            qt = _toks(str(args.get("idea") or ""))
            ov = portfolio_concept_overview(self._scoped_capsules())
            # rank concepts by keyword overlap with the idea (fall back to most-explored)
            scored = sorted(ov["concepts"],
                            key=lambda e: (-(len(qt & _toks(e["concept"])) if qt else 0), -e["n_runs"]))
            hits = [e for e in scored if (not qt) or (qt & _toks(e["concept"]))][:6]
            if not hits:
                return "(no prior runs recorded these concepts yet)"
            lines = []
            for e in hits:
                runs = ", ".join(
                    f"{r['run_id']}" + (f"={r['metric']:g}" if isinstance(r.get("metric"), (int, float))
                                        and not isinstance(r.get("metric"), bool) else "")
                    for r in e["runs"][:5])
                lines.append(f"'{e['concept']}' — tried in {e['n_runs']} run(s): {runs}")
            return "TRIED BEFORE (surface, not a block):\n" + "\n".join(lines)

        if name == "cross_run_claims":
            claims = claim_assessments(self._role_lessons())
            if args.get("contested"):
                claims = [c for c in claims if c["epistemic"] == "mixed"]
            qt = _toks(str(args.get("query") or ""))
            if qt:
                claims = [c for c in claims if qt & _toks(c["statement"])]
            claims = claims[:8]
            if not claims:
                return "(no matching cross-run claims yet)"
            mark = {"supported": "supported", "refuted": "refuted", "mixed": "CONTESTED",
                    "inconclusive": "inconclusive"}
            return "\n".join(
                f"[{mark.get(c['epistemic'], '?')}: {c['n_support']} for / {c['n_oppose']} against] "
                f"{c['statement']}" for c in claims)

        if name == "cross_run_atlas":
            atlas = portfolio_atlas(self._role_lessons(), self._scoped_capsules())
            lines = [f"Portfolio: {atlas['n_runs']} run(s), {atlas['n_concepts']} concept(s), "
                     f"{atlas['n_claims']} claim(s), {atlas['n_contested']} contested."]
            if atlas["explored"]:
                lines.append("Most explored: "
                             + ", ".join(f"{e['concept']}(×{e['n_runs']})" for e in atlas["explored"][:6]))
            if atlas["thin_coverage"]:
                lines.append("Thin (1 run — a gap): " + ", ".join(atlas["thin_coverage"][:8]))
            if atlas["contradictions"]:
                lines.append("Contradictory: "
                             + "; ".join(c["statement"][:80] for c in atlas["contradictions"][:4]))
            return "\n".join(lines)

        return f"(unknown cross-run tool: {name})"
