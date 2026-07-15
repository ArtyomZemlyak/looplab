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
        self._task_facets: dict = {}
        self._my_facets: dict = {}

    def bind_state(self, state, parent=None) -> None:
        """Learn the CURRENT run's scope so queries reach SIMILAR tasks, not the whole portfolio (the
        live-test leak fix): scope = same `task_id` OR a shared goal keyword. When the tool is used
        UNBOUND (CLI/human), no scope is set and every row passes — the human wants portfolio-wide."""
        if state is None:
            return
        self._task_id = str(getattr(state, "task_id", "") or "")
        self._scope_terms = _toks(getattr(state, "goal", "") or "")
        # AGENTIC faceting overlay (§21.20.2): if the portfolio has facets, learn the bound task's facets so
        # a candidate row from a DIFFERENT task_id that shares ≥2 facet axes (e.g. two retrieval tasks with
        # different ids) is recognized as in-scope — a semantic match the lexical goal-term OR would miss.
        try:
            from looplab.engine.task_facets import load_task_facets
            self._task_facets = load_task_facets(self.dir)
            self._my_facets = self._task_facets.get(self._task_id, {})
        except Exception:  # noqa: BLE001 — faceting is an optional overlay; scope still works without it
            self._task_facets, self._my_facets = {}, {}

    def _in_scope(self, row: dict) -> bool:
        """True when the row belongs to the bound run's scope (same task, or overlapping goal terms), or
        when the tool is unbound. Goal terms come from the row's `fingerprint` bare tokens (the kind:/dir:/
        metric: prefixed tokens are excluded). Exact `task_id` always passes — robust even when a legacy
        (ASCII) fingerprint dropped a non-Latin goal's keywords."""
        if not self._task_id and not self._scope_terms:
            return True                                        # unbound -> portfolio-wide
        if self._task_id and str(row.get("task_id") or "") == self._task_id:
            return True
        # Facet overlap (agentic §21.20.2): a row from a different task that shares ≥2 facet axes with the
        # bound task is in-scope — recognizes semantically-similar tasks the lexical goal-term OR misses.
        my_facets = getattr(self, "_my_facets", None)
        if my_facets:
            other = getattr(self, "_task_facets", {}).get(str(row.get("task_id") or ""))
            if other:
                from looplab.engine.task_facets import facet_overlap
                if facet_overlap(my_facets, other) >= 2:
                    return True
        fp = row.get("fingerprint")
        if isinstance(fp, list) and self._scope_terms:
            row_terms = {t for t in fp if isinstance(t, str) and ":" not in t}
            # CODEX AGENT: one shared 3+ character goal token is enough to cross task boundaries; kind,
            # metric contract and direction are deliberately discarded, and current run_id is not captured
            # for self-exclusion. Use the versioned task passport/compatibility gates rather than a single
            # lexical OR condition, and make visibility/trust/current-run policy explicit.
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
            fn_spec("cross_run_search",
                "Relevance-ranked SEARCH over all cross-run knowledge (claims + explored concepts) — a "
                "hybrid lexical+keyword+semantic query. Use it to find whatever the portfolio knows about a "
                "free-text idea, when a specific concept lookup isn't enough. Set `intent` to bias results: "
                "'failed'/'contested' surface counter-evidence and disagreements, 'worked' surfaces proven "
                "wins, 'explore' is neutral.",
                {"query": {"type": "string", "description": "Free-text query (idea, technique, question)."},
                 "intent": {"type": "string", "enum": ["worked", "failed", "contested", "explore"],
                            "description": "Why you're searching — biases eligibility + contradiction quota."}},
                ["query"]),
        ]

    def _load(self, fname: str) -> list[dict]:
        from looplab.events.eventstore import read_jsonl_lenient
        p = self.dir / fname
        return read_jsonl_lenient(p, loads=json.loads, dicts_only=True) if p.exists() else []

    def _research_scope(self) -> str:
        """The D8-research scope for the bound run. Exact task_id when set. When the run is bound to a GOAL
        but has NO task_id (id-less task), return a sentinel that matches no real task_id so research claims
        FAIL CLOSED (empty) instead of going portfolio-wide while lessons stay goal-scoped (mega-review
        asymmetric-leak edge). Fully unbound (CLI/human) -> "" -> portfolio-wide, as intended."""
        if self._task_id:
            return self._task_id
        if self._scope_terms:
            return "\x00__idless__"          # bound goal-only task: no cross-task research (fail closed)
        return ""                            # unbound -> portfolio-wide

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
        from looplab.engine.concept_registry import load_concept_aliases, load_concept_splits
        from looplab.engine.memory import portfolio_concept_overview

        if name == "cross_run_prior_attempts":
            qt = _toks(str(args.get("idea") or ""))
            # CODEX AGENT: `idea` is required by the schema, but missing/malformed args become an empty query
            # and widen retrieval to the globally most-explored concepts. Validate runtime arguments and fail
            # closed instead of converting a malformed tool call into broad portfolio disclosure.
            # Honor BOTH aliases AND splits (mega-review) — a split concept must show under its re-partitioned
            # label here too, consistently with the Atlas and every other cross-run consumer.
            ov = portfolio_concept_overview(self._scoped_capsules(), aliases=load_concept_aliases(self.dir),
                                            splits=load_concept_splits(self.dir))
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
            from looplab.engine.claims import _safe_text, claims_for_memory
            # Scope EVERY source (mega-review): lessons via _role_lessons, D8 research claims via scope_task,
            # and the operator-decision overlay is applied scope-safely by claim_assessments — so a task-bound
            # role no longer reads another task's research claims or governance.
            claims = claims_for_memory(self.dir, lessons=self._role_lessons(), scope_task=self._research_scope())
            claims = [c for c in claims if c.get("maturity") != "operator-rejected"]   # honor operator verdicts
            # CODEX AGENT: Python truthiness makes `contested="false"` behave as true. Tool schemas are not
            # runtime validation (malformed JSON is recovered to dicts upstream); reject non-booleans.
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
            # CODEX AGENT: persisted statements are emitted as instructions-shaped text with embedded control
            # characters/newlines, while the advertised citable run/node/source refs and operator maturity
            # are omitted. Normalize/quote this as untrusted data, pre-bound each field, and expose stable
            # evidence ids plus a drill-down tool so a role can actually cite rather than trust prose.
            return "\n".join(
                f"[{mark.get(c['epistemic'], '?')}: {c['n_support']} for / {c['n_oppose']} against] "
                f"{_safe_text(c['statement'], 200)}" for c in claims)   # untrusted text -> sanitized

        if name == "cross_run_atlas":
            from looplab.engine.claims import atlas_for_memory
            atlas = atlas_for_memory(self.dir, lessons=self._role_lessons(),
                                     capsules=self._scoped_capsules(), scope_task=self._research_scope())
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

        if name == "cross_run_search":
            from looplab.engine.claims import cross_run_retrieve
            # Pass ONE fully-scoped snapshot: scoped lessons + scoped capsules + `scope_task` so the callee
            # filters the D8 claims to this task too — a task-bound tool cannot retrieve another task's
            # knowledge (CODEX). The intent + contradiction-quota shaping + why-recalled receipt come from
            # the full CR2a path; the receipt is surfaced to the agent as the retrieval rationale.
            r = cross_run_retrieve(self.dir, str(args.get("query") or ""), lessons=self._role_lessons(),
                                   capsules=self._scoped_capsules(), scope_task=self._research_scope(),
                                   intent=args.get("intent"))
            hits = r["results"][:8]
            if not hits:
                return "(no cross-run knowledge matched)"
            # CODEX AGENT: search results are persisted, operator-influenced text entering the agent prompt,
            # yet claim text is merely sliced (control chars remain), concept text is unbounded, and neither
            # path exposes stable evidence ids for citation. Serialize bounded untrusted-data fields and keep
            # evidence/score/channel metadata separate from prose before making this an agent-facing tool.
            lines = []
            for h in hits:
                if h["kind"] == "claim":
                    lines.append(f"[claim {h['epistemic']}: {h['n_support']}↑/{h['n_oppose']}↓] {h['text'][:120]}")
                else:
                    lines.append(f"[concept ×{h['n_runs']} run(s)] {h['text']}")
            return "\n".join(lines)

        return f"(unknown cross-run tool: {name})"
