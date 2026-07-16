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
import logging
import re
import unicodedata
from pathlib import Path

from looplab.tools._base import fn_spec

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_LOG = logging.getLogger(__name__)
_TOOL_NAMES = frozenset({
    "cross_run_prior_attempts", "cross_run_claims", "cross_run_atlas", "cross_run_search",
})
_TOOL_UNAVAILABLE = "(cross-run tool unavailable)"


def _toks(s: str) -> set[str]:
    text = unicodedata.normalize("NFKC", str(s or "")).casefold()
    return {w for w in _WORD.findall(text) if len(w) > 2}


def _safe_text(value, limit: int) -> str:
    """Bound one persisted field for an agent prompt and collapse control/newline injection surfaces."""
    text = " ".join(str(value or "").split())
    return "".join(ch for ch in text if ch.isprintable())[:limit]


class CrossRunTools:
    """Read-only cross-run knowledge for the tool-loop. `role` ∈ {"researcher","developer"} scopes the
    claims to that role's lessons (+ shared/untagged); anything else sees all. Never raises from execute."""

    def __init__(self, memory_dir: str | Path | None, *, role: str = "researcher"):
        self.dir = Path(memory_dir) if memory_dir else None
        self.role = str(role or "researcher")
        self._task_id = ""
        self._direction = ""
        self._scope_terms: set[str] = set()
        self._bound = False

    def bind_state(self, state, parent=None) -> None:
        """Learn the current run's direction and scope before any agent query.

        Lessons/capsules may match an exact task or the strict related-goal fingerprint predicate;
        v2 D8 rows carry no goal fingerprint and therefore pass only by exact task. An unbound
        CLI/human provider remains portfolio-wide.
        """
        if state is None:
            return
        self._bound = True
        self._task_id = str(getattr(state, "task_id", "") or getattr(state, "id", "") or "")
        direction = str(getattr(state, "direction", "") or "")
        self._direction = direction if direction in ("min", "max") else ""
        self._scope_terms = _toks(getattr(state, "goal", "") or "")
        # Agentic facets are intentionally not loaded into this visibility predicate. They are
        # untrusted advisory metadata reserved for a future post-scope ranking experiment.

    def _in_scope(self, row: dict) -> bool:
        """True for compatible direction plus exact task or strict related-goal fingerprint.

        Goal terms come from bare fingerprint tokens; rows without that fingerprint (including v2 D8)
        are exact-task-only. Facets are not a visibility input.
        """
        if not self._bound:
            return True                                        # unbound -> portfolio-wide
        row_direction = str(row.get("direction") or "")
        if self._direction and row_direction in ("min", "max") and row_direction != self._direction:
            return False
        if self._task_id and str(row.get("task_id") or "") == self._task_id:
            return True
        fp = row.get("fingerprint")
        if isinstance(fp, list) and self._scope_terms:
            row_terms = {t for t in fp if isinstance(t, str) and ":" not in t}
            shared = row_terms & self._scope_terms
            # A single generic word ("model", "retrieval", "training") is not a security scope.  Similar
            # cross-task transfer requires at least two salient terms covering half of the smaller side;
            # exact task ids remain authoritative. Agent-proposed facets currently affect neither this
            # visibility predicate nor retrieval order.
            if len(shared) >= 2 and len(shared) / max(1, min(len(row_terms), len(self._scope_terms))) >= 0.5:
                return True
        return False

    def specs(self) -> list[dict]:
        if not self.dir:
            return []
        return [
            fn_spec("cross_run_prior_attempts",
                "Check whether an idea's CONCEPTS were observed in earlier runs across the "
                "portfolio, and with what recorded outcome — so you can inspect, extend or replicate the "
                "prior observation. It is not proof that a result is settled and never rejects.",
                {"idea": {"type": "string", "description": "The idea / technique / concept to look up "
                          "(e.g. 'hard-negative mining', 'distillation')."}},
                ["idea"]),
            fn_spec("cross_run_claims",
                "A bounded machine projection of accumulated claim references: support-only, opposition-only, "
                "mixed, or insufficient evidence. `contested` selects mixed-evidence records; it does not "
                "establish proposition truth or independent replication.",
                {"query": {"type": "string", "description": "Keywords to filter claims (blank = top "
                           "claims by evidence)."},
                 "contested": {"type": "boolean", "description": "Only mixed (support+oppose) claims."}},
                []),
            fn_spec("cross_run_atlas",
                "A bounded live portfolio summary: observed concepts and their returned run counts, concepts "
                "observed in one returned run, and mixed-evidence claim records. It has no frozen scope or "
                "coverage denominator, so one-run observations are not proof of a gap.",
                {}, []),
            fn_spec("cross_run_search",
                "Relevance-ranked SEARCH over all cross-run knowledge (claims + explored concepts) — a "
                "hybrid lexical+keyword+semantic query. Use it to find whatever the portfolio knows about a "
                "free-text idea, when a specific concept lookup isn't enough. Set `intent` to bias results: "
                "'failed'/'contested' surface counter-evidence and mixed records, 'worked' biases toward "
                "support-labelled observations, and 'explore' is neutral.",
                {"query": {"type": "string", "description": "Free-text query (idea, technique, question)."},
                 "intent": {"type": "string", "enum": ["worked", "failed", "contested", "explore"],
                            "description": "Why you're searching — biases eligibility + contradiction quota."}},
                ["query"]),
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

    def _role_research_claims(self) -> list[dict]:
        """D8 is researcher evidence, never developer memory; bound rows are exact-task-only."""
        if self.role == "developer":
            return []
        return [r for r in self._load("research_claims.jsonl") if self._in_scope(r)]

    def execute(self, name: str, args: dict) -> str:
        # ToolProvider contract: execute NEVER raises (a junk arg must read as a tool error, not crash
        # the agent phase — drive_tool_loop does not guard tools.execute).
        try:
            return self._execute(name, args or {})
        except Exception as exc:  # noqa: BLE001
            # Storage/parser exceptions can contain credentialed URLs, provider hosts and
            # absolute paths. Tool results become prompt/event material, so they are allow-listed;
            # observability records only a stable operation/category, never the exception string.
            tool = name if isinstance(name, str) and name in _TOOL_NAMES else "unknown"
            if isinstance(exc, OSError):
                failure = "storage"
            elif isinstance(exc, (ValueError, TypeError, KeyError)):
                failure = "invalid_data"
            else:
                failure = "internal"
            try:
                _LOG.warning("cross-run tool unavailable: tool=%s failure=%s", tool, failure)
            except Exception:  # noqa: BLE001 - observability must not violate the never-raise contract
                pass
            return _TOOL_UNAVAILABLE

    def _execute(self, name: str, args: dict) -> str:
        if not self.dir:
            return "(no cross-run memory configured)"
        from looplab.engine.concept_registry import load_concept_aliases, load_concept_splits
        from looplab.engine.memory import portfolio_concept_overview

        if name == "cross_run_prior_attempts":
            idea = args.get("idea")
            if not isinstance(idea, str) or not idea.strip():
                return "(cross-run tool error: idea must be a non-empty string)"
            if len(idea) > 4000:
                return "(cross-run tool error: idea exceeds 4000 characters)"
            qt = _toks(idea)
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
                    f"{_safe_text(r.get('run_id'), 100)!r}" +
                    (f"={r['metric']:g}" if isinstance(r.get("metric"), (int, float))
                     and not isinstance(r.get("metric"), bool) else "")
                    for r in e["runs"][:5])
                lines.append(f"UNTRUSTED_MEMORY_CONCEPT={_safe_text(e.get('concept'), 160)!r} — "
                             f"tried in {e['n_runs']} run(s): {runs}")
            return "TRIED BEFORE (untrusted persisted data; surface, not a block):\n" + "\n".join(lines)

        if name == "cross_run_claims":
            from looplab.engine.claims import claims_for_memory
            claims = claims_for_memory(self.dir, lessons=self._role_lessons(),
                                       research_claims=self._role_research_claims(), structured=True)
            claims = [c for c in claims if c.get("maturity") != "operator-rejected"]   # honor operator verdicts
            contested = args.get("contested", False)
            if not isinstance(contested, bool):
                return "(cross-run tool error: contested must be a boolean)"
            if contested:
                claims = [c for c in claims if c["epistemic"] == "mixed"]
            query = args.get("query", "")
            if not isinstance(query, str):
                return "(cross-run tool error: query must be a string)"
            if len(query) > 4000:
                return "(cross-run tool error: query exceeds 4000 characters)"
            qt = _toks(query)
            if qt:
                claims = [c for c in claims if qt & _toks(c["statement"])]
            claims = claims[:8]
            if not claims:
                return "(no matching cross-run claims yet)"
            mark = {"supported": "supported", "refuted": "refuted", "mixed": "CONTESTED",
                    "inconclusive": "inconclusive"}
            def _claim_line(c):
                refs = (c.get("support") or [])[:4] + (c.get("oppose") or [])[:4]
                evidence = "[" + ", ".join(repr(_safe_text(ref, 120)) for ref in refs) + "]"
                contradicts = "; ".join(
                    repr(_safe_text(statement, 180)) for statement in (c.get("contradicts") or [])[:3])
                return (f"[{mark.get(c['epistemic'], '?')}: {c['n_support']} for / "
                        f"{c['n_oppose']} against] "
                        f"UNTRUSTED_MEMORY={_safe_text(c['statement'], 240)!r}; "
                        f"UNTRUSTED_MEMORY_EVIDENCE={evidence}; maturity={_safe_text(c.get('maturity'), 40)!r}"
                        + (f"; contradicts={contradicts}" if contradicts else ""))

            return "\n".join(_claim_line(c) for c in claims)

        if name == "cross_run_atlas":
            from looplab.engine.claims import atlas_for_memory
            atlas = atlas_for_memory(self.dir, lessons=self._role_lessons(),
                                     capsules=self._scoped_capsules(),
                                     research_claims=self._role_research_claims(), structured=True)
            lines = [f"Bounded live projection: {atlas['n_runs']} run(s), {atlas['n_concepts']} concept(s), "
                     f"{atlas['n_claims']} claim record(s), {atlas['n_contested']} mixed-evidence."]
            if atlas["explored"]:
                lines.append("Most explored: "
                             + ", ".join(f"UNTRUSTED_MEMORY={_safe_text(e.get('concept'), 120)!r}"
                                         f"(×{e['n_runs']})" for e in atlas["explored"][:6]))
            if atlas["thin_coverage"]:
                lines.append("Observed in one returned run (not a coverage gap): "
                             + ", ".join(f"UNTRUSTED_MEMORY={_safe_text(x, 120)!r}"
                                         for x in atlas["thin_coverage"][:8]))
            if atlas["contradictions"]:
                lines.append("Mixed-evidence claim records: "
                             + "; ".join(f"UNTRUSTED_MEMORY={_safe_text(c.get('statement'), 160)!r}"
                                         for c in atlas["contradictions"][:4]))
            return "\n".join(lines)

        if name == "cross_run_search":
            from looplab.engine.claims import cross_run_retrieve
            query = args.get("query")
            if not isinstance(query, str) or not query.strip():
                return "(cross-run tool error: query must be a non-empty string)"
            if len(query) > 4000:
                return "(cross-run tool error: query exceeds 4000 characters)"
            intent = args.get("intent")
            if intent is not None and not isinstance(intent, str):
                return "(cross-run tool error: intent must be a string)"
            if intent is not None and intent not in {"worked", "failed", "contested", "explore"}:
                return "(cross-run tool error: intent must be worked, failed, contested, or explore)"
            # Pass one fully-scoped snapshot. `_in_scope` already applies exact-task or bounded fingerprint
            # transfer to every source; applying an additional exact scope_task filter here would silently
            # discard the intentionally-related rows.
            r = cross_run_retrieve(self.dir, query, lessons=self._role_lessons(),
                                   capsules=self._scoped_capsules(),
                                   research_claims=self._role_research_claims(),
                                   intent=intent, structured=True)
            hits = r["results"][:8]
            if not hits:
                return "(no cross-run knowledge matched)"
            lines = []
            for h in hits:
                if h["kind"] == "claim":
                    contradicts = "; ".join(
                        repr(_safe_text(statement, 180))
                        for statement in (h.get("contradicts") or [])[:3])
                    lines.append(f"[claim {h['epistemic']}: {h['n_support']}↑/{h['n_oppose']}↓; "
                                 f"score={h.get('score')}] UNTRUSTED_MEMORY={_safe_text(h['text'], 160)!r}"
                                 + (f"; contradicts={contradicts}" if contradicts else ""))
                else:
                    lines.append(f"[concept ×{h['n_runs']} run(s); score={h.get('score')}] "
                                 f"UNTRUSTED_MEMORY={_safe_text(h['text'], 120)!r}")
            rc = r.get("receipt") or {}
            lines.append(f"[receipt corpus={_safe_text(rc.get('corpus_digest'), 40)} "
                         f"intent={_safe_text(rc.get('intent'), 20)} hits={rc.get('n_hits', len(hits))}]")
            return "\n".join(lines)

        return f"(unknown cross-run tool: {name})"
