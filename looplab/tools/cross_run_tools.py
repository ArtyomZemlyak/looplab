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
from looplab.trust.cross_run import cross_run_text, same_live_direction, valid_live_direction

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_LOG = logging.getLogger(__name__)
_TOOL_NAMES = frozenset({
    "cross_run_prior_attempts", "cross_run_claims", "cross_run_atlas", "cross_run_search",
    "cross_run_concept_map", "similar_runs", "find_concept_slugs", "concept_card",
})


def _slug_norm(s: str) -> str:
    """Separator/case-insensitive concept key so `r-drop`, `rdrop`, `R_Drop` collapse to one bucket —
    the whole point of the fuzzy slug search (an agent writing `rdrop` must still find `regularization/
    r-drop`). Unicode concept vocabularies remain searchable instead of collapsing to an empty key."""
    normalized = unicodedata.normalize("NFKC", str(s or "")).casefold()
    return "".join(char for char in normalized if char.isalnum())
_TOOL_UNAVAILABLE = "(cross-run tool unavailable)"
_MAX_TOOL_RESULT_CHARS = 16_000


def _toks(s: str) -> set[str]:
    text = unicodedata.normalize("NFKC", str(s or "")).casefold()
    return {w for w in _WORD.findall(text) if len(w) > 2}


def _safe_text(value, limit: int) -> str:
    """Bound one persisted field for an agent prompt and collapse control/newline injection surfaces."""
    return cross_run_text(
        value, max_chars=limit, single_line=True, entropy=True).strip()


def _partial_source_warning(view: dict) -> str:
    """Compact trust receipt for a bounded or legacy capsule projection."""
    if view.get("source_complete") is True:
        return ""
    known = (f"{view.get('source_concepts_omitted', 0)} concept(s), "
             f"{view.get('source_outcomes_omitted', 0)} outcome(s) known omitted")
    unknown = int(view.get("source_unknown_capsules", 0) or 0)
    quarantined = int(view.get("source_rows_quarantined", 0) or 0)
    details = [known]
    if unknown:
        details.append(f"{unknown} legacy capsule(s) have unknown totals")
    if quarantined:
        details.append(
            f"{quarantined} durable row(s) quarantined as malformed, invalid, or duplicate")
    suffix = " Absence and retained frequencies are not exact." if quarantined else ""
    return "WARNING: PARTIAL capsule source (" + "; ".join(details) + ")." + suffix


def _partial_scope_warning(view: dict) -> str:
    """Compact receipt for capsules whose applicability could not be classified exactly."""
    if view.get("scope_complete") is True:
        return ""
    unknown = int(view.get("scope_unknown_capsules", 0) or 0)
    fingerprint_unknown = int(view.get("scope_fingerprint_unknown_capsules", 0) or 0)
    direction_unknown = int(view.get("scope_direction_unknown_capsules", 0) or 0)
    omitted = int(view.get("scope_fingerprint_items_omitted", 0) or 0)
    return ("WARNING: PARTIAL capsule applicability scope "
            f"({unknown} capsule(s) unclassified; {fingerprint_unknown} fingerprint receipt(s) "
            f"unknown; {omitted} fingerprint item(s) known omitted; "
            f"{direction_unknown} direction(s) unknown). Retained scope counts are lower bounds; "
            "absence is not proof that no applicable record exists.")


def _partial_claim_source_warning(claim_source: dict, research_source: dict) -> str:
    lessons = claim_source.get("lessons") if isinstance(claim_source.get("lessons"), dict) else {}
    research = claim_source.get("research") if isinstance(claim_source.get("research"), dict) else {}
    details = [
        f"lessons quarantined={int(lessons.get('rows_quarantined', 0) or 0)}",
        f"research rows quarantined={int(research.get('rows_quarantined', 0) or 0)}",
    ]
    if isinstance(research_source, dict):
        details.append(
            f"D8 claims known omitted={int(research_source.get('producer_claims_omitted', 0) or 0)}")
        unknown = int(research_source.get("producer_unknown_runs", 0) or 0)
        if unknown:
            details.append(f"D8 receipt-unknown runs={unknown}")
    return ("WARNING: PARTIAL claim evidence source (" + "; ".join(details)
            + "). Retained evidence is a lower bound; one-sided states and absence are not exact.")


class CrossRunTools:
    """Read-only cross-run knowledge for the tool-loop. `role` ∈ {"researcher","developer"} scopes the
    claims to that role's lessons (+ shared/untagged); anything else sees all. Never raises from execute."""

    def __init__(self, memory_dir: str | Path | None, *, role: str = "researcher"):
        self.dir = Path(memory_dir) if memory_dir else None
        self.role = str(role or "researcher")
        self._task_id = ""
        self._run_id = ""
        self._direction = ""
        self._scope_terms: set[str] = set()
        self._concepts: set[str] = set()      # E2: the current run's concept set (for similar_runs overlap)
        self._concept_projection_status = "complete"
        self._concept_projection_reasons: tuple[str, ...] = ()
        self._bound = False
        self._capsule_scope_receipt = {
            "scope_complete": True,
            "scope_unknown_capsules": 0,
            "scope_fingerprint_unknown_capsules": 0,
            "scope_fingerprint_items_omitted": 0,
            "scope_direction_unknown_capsules": 0,
        }

    def bind_state(self, state, parent=None) -> None:
        """Learn the current run's direction and scope before any agent query.

        Lessons/capsules may match an exact task or the strict related-goal fingerprint predicate;
        v3 D8 rows carry no goal fingerprint and therefore pass only by exact task. An unbound
        CLI/human provider remains portfolio-wide.
        """
        if state is None:
            return
        self._bound = True
        self._task_id = str(getattr(state, "task_id", "") or getattr(state, "id", "") or "")
        self._run_id = str(getattr(state, "run_id", "") or "")
        direction = str(getattr(state, "direction", "") or "")
        self._direction = direction if direction in ("min", "max") else ""
        self._scope_terms = _toks(getattr(state, "goal", "") or "")
        # E2: use the same strict CURRENT projection as the run tools. Historical tombstones/aborts and
        # unresolved delta fallbacks must not authorize cross-run overlap or masquerade as known-empty.
        from looplab.search.concept_projection import current_concept_projection
        projection = current_concept_projection(state)
        self._concept_projection_status = projection.status
        self._concept_projection_reasons = projection.reasons
        self._concepts = {
            concept
            for concepts in projection.trusted_memberships.values()
            for concept in concepts
        }
        # Agentic facets are intentionally not loaded into this visibility predicate. They are
        # untrusted advisory metadata reserved for a future post-scope ranking experiment.

    def _concept_projection_note(self) -> str:
        reasons = ",".join(self._concept_projection_reasons) or "unspecified"
        return (f"[{self._concept_projection_status.upper()} current_concept_projection "
                f"reasons={reasons}]")

    def _in_scope(self, row: dict, *, source: str = "generic") -> bool:
        """True for compatible direction plus exact task or strict related-goal fingerprint.

        Goal terms come from bare fingerprint tokens; rows without that fingerprint (including v3 D8)
        are exact-task-only. Facets are not a visibility input.
        """
        if not self._bound:
            return True                                        # unbound -> portfolio-wide
        # CODEX AGENT: a bound provider is agent-facing. Exact task identity cannot override missing or
        # malformed polarity provenance; only an explicitly unbound human/CLI audit stays portfolio-wide.
        if not same_live_direction(self._direction, row.get("direction")):
            return False
        if self._task_id and str(row.get("task_id") or "") == self._task_id:
            return True
        fp = row.get("fingerprint")
        if isinstance(fp, list) and self._scope_terms:
            if source == "capsule":
                from looplab.engine.memory import _capsule_fingerprint_scope_complete
                # CODEX AGENT: only an exact persisted fingerprint may grant related-task visibility.
                # A legacy/trimmed capsule remains available through exact task identity or unbound audit.
                if not _capsule_fingerprint_scope_complete(row):
                    return False
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
                "A bounded live portfolio summary: observed concepts and their returned run counts, a "
                "direction-normalized RANK tendency (which concepts consistently landed in the better vs "
                "worse half of their run across similar runs — advisory, not a rule), concepts observed in "
                "one returned run, and mixed-evidence claim records. It has no frozen scope or coverage "
                "denominator, so one-run observations are not proof of a gap.",
                {}, []),
            fn_spec("cross_run_concept_map",
                "A cross-run concept MAP over the caller-visible snapshot: task-family + objective-direction "
                "scoped for a bound agent, portfolio-wide only for an unbound owner/CLI caller — which "
                "concepts appear most, their path hierarchy (is_a), and which concept PAIRS reliably "
                "co-occur across MULTIPLE runs (evidence of related directions). Use it to see the big "
                "picture of what the portfolio has explored and how concepts relate, before proposing. "
                "Advisory map, never a rule. The edge receipt declares when edges cover only the bounded "
                "retained-node projection and edges touching pruned nodes are unknown.",
                {}, []),
            fn_spec("cross_run_search",
                "Relevance-ranked SEARCH over all cross-run knowledge (claims + explored concepts) — a "
                "hybrid lexical+BM25+hash-vector query (the vector is a lexical proxy, not semantic). Use "
                "it to find whatever the caller-visible snapshot retains about a "
                "free-text idea, when a specific concept lookup isn't enough. Set `intent` to bias results: "
                "'failed'/'contested' surface counter-evidence and mixed records, 'worked' biases toward "
                "support-labelled observations, and 'explore' is neutral.",
                {"query": {"type": "string", "description": "Free-text query (idea, technique, question)."},
                 "intent": {"type": "string", "enum": ["worked", "failed", "contested", "explore"],
                            "description": "Why you're searching — biases eligibility + contradiction quota."}},
                ["query"]),
            fn_spec("similar_runs",
                "The prior runs MOST similar to THIS one by shared concepts (Jaccard overlap of concept "
                "sets), ranked within the bound task family and objective direction. Operator concept "
                "aliases, splits, and purges are applied before comparison. Use it to find which past "
                "experiments explored the same directions before you propose — then read_run / "
                "cross_run_concept_map to dig into a specific one. Advisory.",
                {"limit": {"type": "integer", "description": "How many similar runs to return (default 10)."}},
                []),
            fn_spec("find_concept_slugs",
                "BEFORE minting a concept slug, search the EXISTING shared vocabulary (this run + all prior "
                "runs' concept capsules) for an equivalent one and REUSE it — do NOT create a near-duplicate "
                "(e.g. `rdrop` when `regularization/r-drop` already exists); consistent slugs are what make "
                "cross-run priors match. Matching is separator/case-insensitive (r-drop == rdrop == r_drop) "
                "plus Unicode-aware fuzzy matching. Operator aliases, splits, and purges are applied before "
                "matching. Call with no query to list the known concept AXES as an overview.",
                {"query": {"type": "string", "description": "The concept you intend to author (any spelling), "
                                                            "e.g. 'rdrop' or 'decoupled contrastive'. Omit to "
                                                            "list axes."},
                 "scope": {"type": "string", "enum": ["all", "own", "cross", "global"],
                           "description": "Where to search: own=this run, cross=prior runs sharing a concept "
                                          "with this one in its same-direction task family, global=the whole "
                                          "world concept map "
                                          "(use it to hunt SYNERGY from other directions), all=everything ranked "
                                          "own→cross→global. Default all."},
                 "limit": {"type": "integer", "description": "Max matches (default 12)."}},
                []),
            fn_spec("concept_card",
                "DECODE a concept slug and see its evidence at a glance: what it is (axis / name + known "
                "alternative spellings), its cross-run TRACK RECORD in your task family (how many runs used "
                "it and whether it ranked in the better or worse half of those runs), how many runs used it "
                "globally, which "
                "concepts it is usually paired with, and any lessons that mentioned it. Use it when a slug "
                "from find_concept_slugs / the concept map is cryptic, or before betting a node on a "
                "technique — to learn what the portfolio already knows about it. Advisory, never a rule.",
                {"slug": {"type": "string", "description": "The concept slug to decode (any spelling — "
                          "'rdrop' resolves to 'regularization/r-drop'). A full `axis/name` or a bare name "
                          "both work."}},
                ["slug"]),
        ]

    def _load(self, fname: str) -> list[dict]:
        from looplab.events.eventstore import read_jsonl_lenient
        p = self.dir / fname
        return read_jsonl_lenient(p, loads=json.loads, dicts_only=True) if p.exists() else []

    def _role_lessons(self) -> list[dict]:
        """Lessons visible to this role AND in scope: the role's own + shared/untagged (mirrors the
        role-routed cross-run lesson priors), scoped to the bound run's task (portfolio-wide when unbound).
        An unknown role sees every role."""
        from looplab.engine.claims import load_claim_lessons, _filter_claim_source_rows
        lessons = _filter_claim_source_rows(
            load_claim_lessons(self.dir),
            lambda lz: self._in_scope(lz, source="lesson"), research=False)
        if self.role not in ("researcher", "developer"):
            return lessons
        return _filter_claim_source_rows(
            lessons, lambda lz: str(lz.get("role") or "") in ("", self.role), research=False)

    def _partition_capsules(self, caps: list[dict]) -> tuple[list[dict], list[dict], dict]:
        """Partition capsules into scope-eligible, applicability-unknown, and known-ineligible rows."""
        from looplab.engine.memory import _capsule_rows

        if not self._bound:
            receipt = {
                "scope_complete": True,
                "scope_unknown_capsules": 0,
                "scope_fingerprint_unknown_capsules": 0,
                "scope_fingerprint_items_omitted": 0,
                "scope_direction_unknown_capsules": 0,
            }
            return _capsule_rows(caps, source=caps), _capsule_rows((), source=caps), receipt

        from looplab.engine.memory import (
            _capsule_completeness,
            _capsule_fingerprint_scope_complete,
        )
        eligible: list[dict] = []
        unknown: list[dict] = []
        fingerprint_unknown = fingerprint_omitted = direction_unknown = 0
        current_direction_valid = valid_live_direction(self._direction)
        for capsule in caps:
            persisted_direction = capsule.get("direction")
            if not current_direction_valid or not valid_live_direction(persisted_direction):
                unknown.append(capsule)
                direction_unknown += 1
                continue
            if persisted_direction != self._direction:
                continue
            if self._task_id and str(capsule.get("task_id") or "") == self._task_id:
                eligible.append(capsule)
                continue
            if not _capsule_fingerprint_scope_complete(capsule):
                # CODEX AGENT: failing closed at the visibility gate is not permission to erase the row
                # from the denominator. Its related-task applicability is UNKNOWN, so every bound absence
                # and frequency surface must carry this aggregate receipt.
                unknown.append(capsule)
                meta = _capsule_completeness(
                    capsule, "fingerprint", len(capsule.get("fingerprint") or []))
                fingerprint_unknown += int(meta is None or meta[0] is None)
                fingerprint_omitted += int(meta[1] or 0) if meta is not None else 0
                continue
            if self._in_scope(capsule, source="capsule"):
                eligible.append(capsule)
        receipt = {
            "scope_complete": not unknown,
            "scope_unknown_capsules": len(unknown),
            "scope_fingerprint_unknown_capsules": fingerprint_unknown,
            "scope_fingerprint_items_omitted": fingerprint_omitted,
            "scope_direction_unknown_capsules": direction_unknown,
        }
        # CODEX AGENT: scope filtering cannot erase file/schema quarantine health. A damaged row has no
        # trustworthy task/direction with which to prove it was ineligible, so every absence stays partial.
        return (_capsule_rows(eligible, source=caps),
                _capsule_rows(unknown, source=caps), receipt)

    def _all_capsules(self) -> list[dict]:
        """Every stored capsule, de-duplicated to ONE row per run id — the SAME contract the Atlas/advisory
        read-models get through `portfolio_concept_overview` (which calls `_dedup_valid_capsules`). The store
        upserts by run_id so its own file is unique, but a memory dir assembled from CONCATENATED shards
        (multiple machines' portfolio memory — the reason cross-run memory exists) can carry the same run
        twice; the agent-facing tools must collapse those too, or they double-count a run in similar_runs /
        find_concept_slugs / concept_card. Sharing one dedup helper keeps every capsule consumer consistent."""
        from looplab.engine.memory import ConceptCapsuleStore, _dedup_valid_capsules
        p = self.dir / "concept_capsules.jsonl"
        return _dedup_valid_capsules(ConceptCapsuleStore(p).all()) if p.exists() else []

    def _scoped_capsules(self) -> list[dict]:
        caps = self._all_capsules()
        eligible, _unknown, receipt = self._partition_capsules(caps)
        self._capsule_scope_receipt = receipt
        return eligible

    def _role_research_claims(self) -> list[dict]:
        """D8 is researcher evidence, never developer memory; bound rows are exact-task-only."""
        from looplab.engine.claims import (_claim_source_rows, _filter_claim_source_rows,
                                           load_research_claims)
        if self.role == "developer":
            return _claim_source_rows([], research=True)
        return _filter_claim_source_rows(
            load_research_claims(self.dir),
            lambda r: self._in_scope(r, source="research"), research=True)

    def execute(self, name: str, args: dict) -> str:
        # ToolProvider contract: execute NEVER raises (a junk arg must read as a tool error, not crash
        # the agent phase — drive_tool_loop does not guard tools.execute).
        try:
            result = self._execute(name, args or {})
            # CODEX AGENT: this final boundary covers every current/future tool branch, including a
            # malformed legacy value that bypasses a field-level formatter. Tool results are persisted
            # in traces and fed back to the model, so they are never allowed to carry raw credentials.
            return cross_run_text(
                result, max_chars=_MAX_TOOL_RESULT_CHARS, single_line=False, entropy=True)
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
        from looplab.engine.memory import _portfolio_concept_overview_data

        if name == "cross_run_prior_attempts":
            idea = args.get("idea")
            if not isinstance(idea, str) or not idea.strip():
                return "(cross-run tool error: idea must be a non-empty string)"
            if len(idea) > 4000:
                return "(cross-run tool error: idea exceeds 4000 characters)"
            qt = _toks(idea)
            scoped_capsules = self._scoped_capsules()
            scope_receipt = self._capsule_scope_receipt
            ov, concept_rows = _portfolio_concept_overview_data(
                scoped_capsules, aliases=load_concept_aliases(self.dir),
                splits=load_concept_splits(self.dir))
            # rank concepts by keyword overlap with the idea (fall back to most-explored)
            # CODEX AGENT: the six-row tool envelope is the only result cap. Search the full retained
            # canonical set first so a query for public-overview row #513 cannot yield an exact-looking miss.
            scored = sorted(concept_rows,
                            key=lambda e: (-(len(qt & _toks(e["concept"])) if qt else 0), -e["n_runs"]))
            hits = [e for e in scored if (not qt) or (qt & _toks(e["concept"]))][:6]
            if not hits:
                if (ov.get("source_complete") is not True
                        or scope_receipt.get("scope_complete") is not True):
                    lines = ["(no retained scope-eligible prior run records these concepts; partial "
                             "source/scope is not proof that no applicable run exists)"]
                    if ov.get("source_complete") is not True:
                        lines.append(_partial_source_warning(ov))
                    if scope_receipt.get("scope_complete") is not True:
                        lines.append(_partial_scope_warning(scope_receipt))
                    return "\n".join(lines)
                return "(no prior runs recorded these concepts yet)"
            lines = [_partial_source_warning(ov)] if ov.get("source_complete") is not True else []
            if scope_receipt.get("scope_complete") is not True:
                lines.append(_partial_scope_warning(scope_receipt))
            for e in hits:
                runs = ", ".join(
                    f"{_safe_text(r.get('run_id'), 100)!r}" +
                    (f"={r['metric']:g}" if isinstance(r.get("metric"), (int, float))
                     and not isinstance(r.get("metric"), bool) else "")
                    for r in e["runs"][:5])
                count_label = (f"tried in {e['n_runs']} run(s)"
                               if scope_receipt.get("scope_complete") is True
                               else f"retained in at least {e['n_runs']} scope-eligible run(s)")
                lines.append(f"UNTRUSTED_MEMORY_CONCEPT={_safe_text(e.get('concept'), 160)!r} — "
                             f"{count_label}: {runs}")
            return "TRIED BEFORE (untrusted persisted data; surface, not a block):\n" + "\n".join(lines)

        if name == "cross_run_claims":
            from looplab.engine.claims import (_filter_claim_assessments, claims_for_memory,
                                               _safe_claim_source_summary,
                                               _safe_research_source_summary)
            claims = claims_for_memory(
                self.dir, lessons=self._role_lessons(),
                research_claims=self._role_research_claims(), structured=True)
            claim_source = _safe_claim_source_summary(getattr(claims, "claim_source", None)) or {}
            research_source = _safe_research_source_summary(
                getattr(claims, "research_source", None)) or {}
            claims = _filter_claim_assessments(
                claims, lambda c: c.get("maturity") != "operator-rejected")  # honor operator verdicts
            contested = args.get("contested", False)
            if not isinstance(contested, bool):
                return "(cross-run tool error: contested must be a boolean)"
            if contested:
                claims = _filter_claim_assessments(
                    claims, lambda c: c["epistemic"] == "mixed")
            query = args.get("query", "")
            if not isinstance(query, str):
                return "(cross-run tool error: query must be a string)"
            if len(query) > 4000:
                return "(cross-run tool error: query exceeds 4000 characters)"
            qt = _toks(query)
            if qt:
                claims = _filter_claim_assessments(
                    claims, lambda c: bool(qt & _toks(c["statement"])))
            kept_claim_ids = {id(c) for c in claims[:8]}
            claims = _filter_claim_assessments(
                claims, lambda c: id(c) in kept_claim_ids)
            if not claims:
                if claim_source.get("source_complete") is not True:
                    return ("(no retained matching cross-run claims; partial source is not proof of absence)\n"
                            + _partial_claim_source_warning(claim_source, research_source))
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

            lines = ([_partial_claim_source_warning(claim_source, research_source)]
                     if claim_source.get("source_complete") is not True else [])
            lines.extend(_claim_line(c) for c in claims)
            return "\n".join(lines)

        if name == "cross_run_atlas":
            from looplab.engine.claims import atlas_for_memory
            scoped_capsules = self._scoped_capsules()
            scope_receipt = self._capsule_scope_receipt
            atlas = atlas_for_memory(self.dir, lessons=self._role_lessons(),
                                     capsules=scoped_capsules,
                                     research_claims=self._role_research_claims(), structured=True)
            lines = [f"Bounded live projection: {atlas['n_runs']} run(s), {atlas['n_concepts']} concept(s), "
                     f"{atlas['n_claims']} claim record(s), {atlas['n_contested']} mixed-evidence."]
            claim_source = atlas.get("claim_source") if isinstance(atlas.get("claim_source"), dict) else {}
            research_source = (atlas.get("research_source")
                               if isinstance(atlas.get("research_source"), dict) else {})
            if claim_source.get("source_complete") is not True:
                lines.append(_partial_claim_source_warning(claim_source, research_source))
            if atlas["explored"]:
                lines.append("Most explored: "
                             + ", ".join(f"UNTRUSTED_MEMORY={_safe_text(e.get('concept'), 120)!r}"
                                         f"(×{e['n_runs']})" for e in atlas["explored"][:6]))
            # PART V Phase 1: use the context pack's tendency projection, which was computed from the
            # FULL overview before ``explored`` was display-capped. This keeps the assistant and the
            # Researcher on one eligible population as well as one threshold: a qualifying concept
            # ranked ninth by frequency must not disappear only from this twin surface.
            coverage = (atlas.get("context_pack") or {}).get("coverage") or {}
            if coverage.get("source_complete") is not True:
                lines.append(_partial_source_warning(coverage))
            if scope_receipt.get("scope_complete") is not True:
                lines.append(_partial_scope_warning(scope_receipt))
            helps = (coverage.get("helps") or []) if scope_receipt.get("scope_complete") is True else []
            hurts = (coverage.get("hurts") or []) if scope_receipt.get("scope_complete") is True else []
            if helps or hurts:
                seg = []
                if helps:
                    seg.append("tended to RANK BETTER: " + ", ".join(
                        f"UNTRUSTED_MEMORY={_safe_text(c, 140)!r}" for c in helps[:6]))
                if hurts:
                    seg.append("tended to RANK WORSE: " + ", ".join(
                        f"UNTRUSTED_MEMORY={_safe_text(c, 140)!r}" for c in hurts[:6]))
                lines.append("Cross-run rank tendency (better/worse half of each run; advisory, not a rule): "
                             + "; ".join(seg))
            if atlas["thin_coverage"]:
                lines.append("Observed in one returned run (not a coverage gap): "
                             + ", ".join(f"UNTRUSTED_MEMORY={_safe_text(x, 120)!r}"
                                         for x in atlas["thin_coverage"][:8]))
            if atlas["contradictions"]:
                lines.append("Mixed-evidence claim records: "
                             + "; ".join(f"UNTRUSTED_MEMORY={_safe_text(c.get('statement'), 160)!r}"
                                         for c in atlas["contradictions"][:4]))
            projection_omitted = (
                int(atlas.get("explored_omitted", 0) or 0),
                int(atlas.get("thin_coverage_omitted", 0) or 0),
                int(atlas.get("contradictions_omitted", 0) or 0),
            )
            if any(projection_omitted):
                lines.append("Bounded Atlas projection omitted: "
                             f"{projection_omitted[0]} concept observation(s), "
                             f"{projection_omitted[1]} single-run observation(s), "
                             f"{projection_omitted[2]} mixed-evidence record(s).")
            return "\n".join(lines)

        if name == "cross_run_concept_map":
            # PART V Phase 4/5: the caller-visible cross-run concept graph. Scoped to this run's task family when
            # bound; portfolio-wide for an unbound (assistant/CLI) caller — the same _scoped_capsules the
            # atlas uses. Aliases/splits honor the operator/steward taxonomy governance.
            from looplab.engine.concept_registry import load_concept_aliases, load_concept_splits
            from looplab.engine.memory import portfolio_concept_graph
            scoped_capsules = self._scoped_capsules()
            scope_receipt = self._capsule_scope_receipt
            graph = portfolio_concept_graph(scoped_capsules,
                                            aliases=load_concept_aliases(self.dir),
                                            splits=load_concept_splits(self.dir))
            # EXCLUDE the 0-run structural spine (materialized ancestor path prefixes) from the "explored"
            # display — they are hierarchy scaffolding, not concepts any run touched.
            explored = [e for e in graph["concepts"] if e.get("n_runs", 0) >= 1]
            if not explored:
                if (graph.get("source_complete") is not True
                        or scope_receipt.get("scope_complete") is not True):
                    lines = ["(no retained scope-eligible cross-run concepts; partial source/scope is not "
                             "proof of absence)"]
                    if graph.get("source_complete") is not True:
                        lines.append(_partial_source_warning(graph))
                    if scope_receipt.get("scope_complete") is not True:
                        lines.append(_partial_scope_warning(scope_receipt))
                    return "\n".join(lines)
                return "(no cross-run concepts yet)"
            n_explored = int(graph.get("n_explored_concepts", len(explored)) or 0)
            map_label = "Task-family concept map" if self._bound else "Portfolio-wide concept map"
            if len(explored) < n_explored:
                lines = [f"{map_label}: showing {len(explored)} of {n_explored} explored concept(s) "
                         f"across {graph['n_runs']} run(s)."]
            else:
                lines = [f"{map_label}: {n_explored} explored concept(s) "
                         f"across {graph['n_runs']} run(s)."]
            if graph.get("source_complete") is not True:
                lines.append(_partial_source_warning(graph))
            if scope_receipt.get("scope_complete") is not True:
                lines.append(_partial_scope_warning(scope_receipt))
            if graph.get("edge_source_complete") is not True:
                lines.append("WARNING: PARTIAL edge source — hierarchy/co-occurrence edges cover only "
                             f"{graph.get('edge_source_nodes_included', 0)} retained top graph node(s); "
                             f"{graph.get('edge_source_nodes_pruned', 0)} node(s) were pruned and edges "
                             "touching them are UNKNOWN.")
            # the is_a hierarchy, surfaced as its top axes (the coarse structure of the map)
            axes = sorted({str(e.get("concept") or "").split("/", 1)[0] for e in explored} - {""})
            if axes:
                lines.append("Axes: " + ", ".join(f"UNTRUSTED_MEMORY={_safe_text(a, 80)!r}" for a in axes[:12]))
            lines.append("Most explored: " + ", ".join(
                f"UNTRUSTED_MEMORY={_safe_text(e.get('concept'), 120)!r}(×{e.get('n_runs', 0)})"
                for e in explored[:12]))
            cooc = [e for e in graph["edges"] if e.get("rel") == "co_occurs"]
            if cooc:
                lines.append("Concept pairs that co-occur across runs: " + "; ".join(
                    f"UNTRUSTED_MEMORY={_safe_text(e.get('src'), 80)!r}+UNTRUSTED_MEMORY="
                    f"{_safe_text(e.get('dst'), 80)!r}(×{e.get('n_runs', 0)})" for e in cooc[:8]))
            # Concepts use the exact full retained-snapshot total. Edge omissions stay exact only inside the
            # declared edge-source projection; the warning above separately keeps out-of-projection edges UNKNOWN.
            hidden_c = max(0, n_explored - 12)
            hidden_e = max(0, len(cooc) - 8) + graph.get("edges_omitted", 0)
            if hidden_c or hidden_e:
                edge_label = ("known retained-projection co-occurrence pair(s)"
                              if graph.get("edge_source_complete") is not True
                              else "more co-occurrence pair(s)")
                lines.append(f"(+{hidden_c} more concept(s), {hidden_e} {edge_label} not shown)")
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
            scoped_capsules = self._scoped_capsules()
            scope_receipt = self._capsule_scope_receipt
            r = cross_run_retrieve(self.dir, query, lessons=self._role_lessons(),
                                   capsules=scoped_capsules,
                                   research_claims=self._role_research_claims(),
                                   intent=intent, structured=True,
                                   scope_receipt=scope_receipt)
            hits = r["results"][:8]
            rc = r.get("receipt") or {}
            source_complete = rc.get("source_complete") is True
            scope_complete = rc.get("scope_complete") is True
            claim_source = rc.get("claim_source") if isinstance(rc.get("claim_source"), dict) else {}
            research_source = (rc.get("research_source")
                               if isinstance(rc.get("research_source"), dict) else {})
            claim_complete = claim_source.get("source_complete") is True
            fully_complete = source_complete and scope_complete and claim_complete
            if not hits:
                lines = [("(no retained cross-run knowledge matched; partial source/scope is not proof "
                          "that no matching concept exists or that no matching claim exists)")
                         if not fully_complete
                         else "(no cross-run knowledge matched)"]
                if not source_complete:
                    # CODEX AGENT: a legacy/capped capsule may have omitted the matching concept entirely;
                    # an empty retrieval is only absence from retained records, never proof of novelty.
                    lines.append(_partial_source_warning(rc))
                if not scope_complete:
                    lines.append(_partial_scope_warning(rc))
                if not claim_complete:
                    lines.append(_partial_claim_source_warning(claim_source, research_source))
                lines.append(f"[receipt corpus={_safe_text(rc.get('corpus_digest'), 40)} "
                             f"intent={_safe_text(rc.get('intent'), 20)} hits=0 "
                             f"source_complete={str(source_complete).lower()} "
                             f"scope_complete={str(scope_complete).lower()} "
                             f"claim_source_complete={str(claim_complete).lower()}]")
                return "\n".join(lines)
            lines = [_partial_source_warning(rc)] if not source_complete else []
            if not scope_complete:
                lines.append(_partial_scope_warning(rc))
            if not claim_complete:
                lines.append(_partial_claim_source_warning(claim_source, research_source))
            for h in hits:
                if h["kind"] == "claim":
                    contradicts = "; ".join(
                        repr(_safe_text(statement, 180))
                        for statement in (h.get("contradicts") or [])[:3])
                    lines.append(f"[claim {h['epistemic']}: {h['n_support']}↑/{h['n_oppose']}↓; "
                                 f"score={h.get('score')}] UNTRUSTED_MEMORY={_safe_text(h['text'], 160)!r}"
                                 + (f"; contradicts={contradicts}" if contradicts else ""))
                else:
                    count = (f"×{h['n_runs']} run(s)" if source_complete and scope_complete
                             else f"retained in at least {h['n_runs']} run(s)")
                    lines.append(f"[concept {count}; score={h.get('score')}] "
                                 f"UNTRUSTED_MEMORY={_safe_text(h['text'], 120)!r}")
            lines.append(f"[receipt corpus={_safe_text(rc.get('corpus_digest'), 40)} "
                         f"intent={_safe_text(rc.get('intent'), 20)} hits={rc.get('n_hits', len(hits))} "
                         f"source_complete={str(source_complete).lower()} "
                         f"scope_complete={str(scope_complete).lower()} "
                         f"claim_source_complete={str(claim_complete).lower()}]")
            return "\n".join(lines)

        if name == "similar_runs":
            try:
                limit = int(args.get("limit") or 10)
            except (TypeError, ValueError):
                limit = 10
            limit = max(1, min(limit, 50))
            from looplab.engine.concept_registry import (canonicalize_concepts,
                                                         concept_governance_snapshot)

            # CODEX AGENT: visibility and identity are one retrieval boundary. A bound model may compare
            # only `_scoped_capsules()` (task family + compatible direction), and both sides must use the
            # SAME locked taxonomy snapshot so an alias/split/purge cannot change just one Jaccard operand.
            taxonomy = concept_governance_snapshot(self.dir)
            aliases, splits = taxonomy["aliases"], taxonomy["splits"]
            mine = set(canonicalize_concepts(
                sorted(self._concepts), aliases=aliases, splits=splits))
            caps = self._scoped_capsules()
            scope_receipt = self._capsule_scope_receipt
            from looplab.engine.memory import _capsule_source_summary, _filter_capsule_rows
            prior_caps = _filter_capsule_rows(
                caps, lambda cap: (not self._run_id
                                   or str(cap.get("run_id") or "") != self._run_id))
            source_summary = _capsule_source_summary(prior_caps)
            scope = "bound_task_family" if self._bound else "portfolio"
            direction = self._direction if self._bound else "any"

            def _receipt(*, matched: int, returned: int) -> str:
                return (f"[receipt scope={scope} direction={direction or 'invalid'} "
                        f"eligible_capsules={len(prior_caps)} matched={matched} returned={returned} "
                        f"scope_complete={str(scope_receipt.get('scope_complete') is True).lower()} "
                        f"scope_unknown_capsules={scope_receipt.get('scope_unknown_capsules', 0)} "
                        f"taxonomy_revision={taxonomy['governance_revision']}]")

            if self._concept_projection_status == "unavailable":
                return ("(current run concepts are UNAVAILABLE; similar_runs cannot infer overlap from "
                        "a fallback empty set)\n" + self._concept_projection_note() + "\n"
                        + _receipt(matched=0, returned=0))
            partial_note = (self._concept_projection_note() + "\n"
                            if self._concept_projection_status == "partial" else "")
            if not mine and self._concept_projection_status == "partial":
                return (partial_note
                        + "(no reliable current-run concepts remain; this is not a complete zero)\n"
                        + _receipt(matched=0, returned=0))
            if not mine:
                return ("(this run has no concepts yet after canonical taxonomy governance — "
                        "similar_runs ranks by shared concept overlap)\n"
                        + _receipt(matched=0, returned=0))
            ranked = []
            for cap in prior_caps:
                rid = str(cap.get("run_id") or "")
                if not rid:
                    continue
                theirs = set(canonicalize_concepts(
                    cap.get("concepts") or [], aliases=aliases, splits=splits))
                shared = mine & theirs
                if not shared:
                    continue
                jac = len(shared) / len(mine | theirs)
                ranked.append((jac, len(shared), rid, sorted(shared)))
            if not ranked and self._concept_projection_status == "partial":
                lines = [partial_note.rstrip(),
                         "(no prior run shares a reliable concept with this one among retained capsules)"]
                if source_summary.get("source_complete") is not True:
                    lines.append(_partial_source_warning(source_summary))
                if scope_receipt.get("scope_complete") is not True:
                    lines.append(_partial_scope_warning(scope_receipt))
                lines.append(_receipt(matched=0, returned=0))
                return "\n".join(lines)
            if not ranked:
                lines = ["(no prior run shares a retained concept with this one)"]
                if source_summary.get("source_complete") is not True:
                    lines.append(_partial_source_warning(source_summary))
                if scope_receipt.get("scope_complete") is not True:
                    lines[0] = ("(no scope-eligible prior run shares a retained concept with this one; "
                                "partial scope is not proof of absence)")
                    lines.append(_partial_scope_warning(scope_receipt))
                lines.append(_receipt(matched=0, returned=0))
                return "\n".join(lines)
            ranked.sort(key=lambda x: (-x[0], -x[1], x[2]))
            returned = min(len(ranked), limit)
            lines = ([partial_note.rstrip()] if partial_note else [])
            lines.append(f"{returned} prior run(s) most similar by shared concepts (advisory):")
            if source_summary.get("source_complete") is not True:
                lines.append(_partial_source_warning(source_summary))
            if scope_receipt.get("scope_complete") is not True:
                lines.append(_partial_scope_warning(scope_receipt))
            for jac, n, rid, shared in ranked[:limit]:
                preview = ", ".join(
                    f"UNTRUSTED_MEMORY_CONCEPT={_safe_text(concept, 160)!r}"
                    for concept in shared[:8]) + ("…" if len(shared) > 8 else "")
                lines.append(f"  UNTRUSTED_MEMORY_RUN={_safe_text(rid, 100)!r}: "
                             f"{n} shared ({jac:.0%}) — {preview}")
            lines.append("Dig into one with cross_run_concept_map / cross_run_search.")
            lines.append(_receipt(matched=len(ranked), returned=returned))
            return "\n".join(lines)

        if name == "find_concept_slugs":
            import difflib
            from collections import Counter
            raw_limit = args.get("limit")
            if raw_limit is None:
                limit = 12
            elif isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
                return "(cross-run tool error: limit must be an integer)"
            else:
                limit = raw_limit
            limit = max(1, min(limit, 50))
            raw_query = args.get("query")
            if raw_query is not None and not isinstance(raw_query, str):
                return "(cross-run tool error: query must be a string)"
            if isinstance(raw_query, str) and len(raw_query) > 256:
                return "(cross-run tool error: query exceeds 256 characters)"
            query = (raw_query or "").strip()
            raw_scope = args.get("scope")
            if raw_scope is not None and not isinstance(raw_scope, str):
                return "(cross-run tool error: scope must be a string)"
            want = (raw_scope or "all").strip().lower()
            if want not in ("all", "own", "cross", "global"):
                return "(cross-run tool error: scope must be all, own, cross, or global)"
            # Vocabulary = every slug in prior concept capsules (available from node 0) + this run's live
            # concepts (which only appear after the first evaluated node). Each slug gets a SCOPE:
            #   own    — in THIS run's concept set
            #   cross  — in a prior run that shares >=1 concept with this one (same direction)
            #   global — only in unrelated prior runs (the wider world map; hunt cross-direction synergy here)
            from looplab.engine.concept_registry import (canonicalize_concepts,
                                                         concept_governance_snapshot)
            from looplab.engine.memory import _capsule_source_summary, _filter_capsule_rows

            # CODEX AGENT: identity, cross-run visibility, and display trust are one boundary. Resolve every
            # operand through ONE governance snapshot; only a same-direction task-family capsule can make a
            # run "cross", while the explicitly requested global tier remains the broader synergy surface.
            taxonomy = concept_governance_snapshot(self.dir)
            aliases, splits = taxonomy["aliases"], taxonomy["splits"]
            caps = self._all_capsules()
            prior_caps = _filter_capsule_rows(
                caps, lambda cap: (not self._run_id
                                   or str(cap.get("run_id") or "") != self._run_id))
            source_summary = _capsule_source_summary(prior_caps)
            scoped_caps, unknown_scope_caps, scope_receipt = self._partition_capsules(prior_caps)
            mine = set(canonicalize_concepts(
                sorted(self._concepts), aliases=aliases, splits=splits))
            canonical_by_capsule: dict[int, set[str]] = {
                id(cap): set(canonicalize_concepts(
                    cap.get("concepts") or [], aliases=aliases, splits=splits))
                for cap in prior_caps
            }
            cross_run_ids: set[str] = set()
            for cap in scoped_caps:
                rid = str(cap.get("run_id") or "")
                if rid and mine & canonical_by_capsule[id(cap)]:
                    cross_run_ids.add(rid)
            unknown_scope_run_ids = {
                str(cap.get("run_id") or "") for cap in unknown_scope_caps
                if str(cap.get("run_id") or "")
            }
            vocab: dict[str, dict] = {}
            for cap in prior_caps:
                rid = str(cap.get("run_id") or "")
                if not rid:
                    continue
                bucket = ("cross_runs" if rid in cross_run_ids else
                          "unknown_runs" if rid in unknown_scope_run_ids else "global_runs")
                for concept in canonical_by_capsule[id(cap)]:
                    meta = vocab.setdefault(
                        concept, {"cross_runs": set(), "global_runs": set(),
                                  "unknown_runs": set(), "own": False})
                    meta[bucket].add(rid)
            for concept in mine:
                vocab.setdefault(
                    concept, {"cross_runs": set(), "global_runs": set(),
                              "unknown_runs": set(), "own": False})["own"] = True

            def _scope(meta: dict) -> str:
                if meta["own"]:
                    return "own"
                if meta["cross_runs"]:
                    return "cross"
                if meta["unknown_runs"]:
                    return "unknown"
                return ("global" if self._concept_projection_status == "complete"
                        else "unknown")

            def _run_count(meta: dict) -> int:
                return ((1 if meta["own"] else 0)
                        + len(meta["cross_runs"] | meta["global_runs"] | meta["unknown_runs"]))

            def _receipt(*, candidates: int, returned: int) -> str:
                direction = self._direction if self._bound else "any"
                return (f"[receipt requested_scope={want} direction={direction or 'invalid'} "
                        f"prior_capsules={len(prior_caps)} scoped_capsules={len(scoped_caps)} "
                        f"scope_complete={str(scope_receipt.get('scope_complete') is True).lower()} "
                        f"scope_unknown_capsules={scope_receipt.get('scope_unknown_capsules', 0)} "
                        f"candidates={candidates} returned={returned} "
                        f"taxonomy_revision={taxonomy['governance_revision']}]")

            concept_dependent_scope = want in {"own", "cross"}
            capsule_scope_uncertain = (want in {"all", "cross", "global"}
                                       and scope_receipt.get("scope_complete") is not True)
            if concept_dependent_scope and self._concept_projection_status == "unavailable":
                return (f"(current run concepts are UNAVAILABLE; scope '{want}' cannot be computed from "
                        "a fallback empty set)\n" + self._concept_projection_note() + "\n"
                        + _receipt(candidates=0, returned=0))
            partial_note = (self._concept_projection_note() + "\n"
                            if ((concept_dependent_scope
                                 and self._concept_projection_status == "partial")
                                or (want in {"all", "global"}
                                    and self._concept_projection_status != "complete")) else "")
            if want != "all":
                if want == "global":
                    # CODEX AGENT: prior vocabulary stays usable; only its relationship to this
                    # run is unknown until current membership materializes.
                    vocab = {s: m for s, m in vocab.items()
                             if not m["own"] and not m["cross_runs"]}
                else:
                    vocab = {s: m for s, m in vocab.items() if _scope(m) == want}
            if not vocab:
                message = (f"(no concept slugs in scope '{want}'"
                           + ("; this run has no concepts yet" if want == "own" else "") + ")")
                lines = ([partial_note.rstrip()] if partial_note else [])
                lines.append(message)
                if want != "own" and source_summary.get("source_complete") is not True:
                    lines.append(_partial_source_warning(source_summary))
                if capsule_scope_uncertain:
                    lines.append(_partial_scope_warning(scope_receipt))
                lines.append(_receipt(candidates=0, returned=0))
                return "\n".join(lines)
            if not query:
                by_axis = Counter(s.split("/", 1)[0] for s in vocab)
                lines = [f"Known concept AXES in scope '{want}' ({len(vocab)} slugs) — "
                         "search within one: find_concept_slugs('<your concept>'):"]
                ordered_axes = sorted(by_axis.items(), key=lambda item: (-item[1], item[0]))
                # CODEX AGENT: the validated response limit applies to the no-query axis listing too; many
                # one-off axes must not bypass the tool's hard output bound.
                lines += [f"  UNTRUSTED_MEMORY_AXIS={_safe_text(axis, 80)!r} ({count} slugs)"
                          for axis, count in ordered_axes[:limit]]
                if partial_note:
                    lines.insert(0, partial_note.rstrip())
                if want != "own" and source_summary.get("source_complete") is not True:
                    lines.append(_partial_source_warning(source_summary))
                if capsule_scope_uncertain:
                    lines.append(_partial_scope_warning(scope_receipt))
                # Candidate/returned units must match: this branch returns axes, while the heading already
                # reports the underlying slug vocabulary size.
                lines.append(_receipt(candidates=len(ordered_axes), returned=min(len(ordered_axes), limit)))
                return "\n".join(lines)
            qn = _slug_norm(query)
            _rank = {"own": 0, "cross": 1, "global": 2, "unknown": 2}
            scored = []
            for slug, meta in vocab.items():
                sn, ln = _slug_norm(slug), _slug_norm(slug.split("/")[-1])
                if qn and (qn == sn or qn == ln):
                    score = 1.0
                elif qn and (qn in sn or sn in qn):
                    score = 0.9
                else:
                    score = max(difflib.SequenceMatcher(None, qn, sn).ratio(),
                                difflib.SequenceMatcher(None, qn, ln).ratio())
                if score >= 0.55:
                    scored.append((_rank[_scope(meta)], -score, slug, meta))
            if (not scored and partial_note
                    and self._concept_projection_status == "partial"):
                lines = [partial_note.rstrip(),
                         f"No reliable existing slug matches {_safe_text(query, 256)!r} in scope '{want}'. "
                         "Because the current projection is PARTIAL, this is not proof that the slug is new."]
                if want != "own" and source_summary.get("source_complete") is not True:
                    lines.append(_partial_source_warning(source_summary))
                if capsule_scope_uncertain:
                    lines.append(_partial_scope_warning(scope_receipt))
                lines.append(_receipt(candidates=0, returned=0))
                return "\n".join(lines)
            if not scored:
                if ((want != "own" and source_summary.get("source_complete") is not True)
                        or capsule_scope_uncertain):
                    warnings = []
                    if want != "own" and source_summary.get("source_complete") is not True:
                        warnings.append(_partial_source_warning(source_summary))
                    if capsule_scope_uncertain:
                        warnings.append(_partial_scope_warning(scope_receipt))
                    return (f"No RETAINED slug matches {_safe_text(query, 256)!r} in scope '{want}'; "
                            "the capsule source/scope is partial, so this is not proof the concept is new.\n"
                            + "\n".join(warnings) + "\n"
                            + _receipt(candidates=0, returned=0))
                return (f"No existing slug matches {_safe_text(query, 256)!r} in scope '{want}' — "
                        "it looks NEW. Mint it as `axis/name` (reuse an existing AXIS if one fits; call "
                        "with no query to list axes).\n" + _receipt(candidates=0, returned=0))
            # Sort by the ORDERABLE prefix only (rank, -score, slug). The 4th tuple element is a `meta`
            # dict — never put it in the comparison key: `scored.sort()` would raise TypeError the moment
            # the first three tie, and execute() would swallow it into "(cross-run tool unavailable)".
            scored.sort(key=lambda t: (t[0], t[1], t[2]))   # own < cross < global; then best match first

            _label = {"own": "this run", "cross": "cross-run", "global": "global map",
                      "unknown": "relation to current run unknown"}
            order = ("own→cross→global" if (self._concept_projection_status == "complete"
                                             and scope_receipt.get("scope_complete") is True)
                     else "own→cross→unknown")
            lines = [f"Existing slugs matching {_safe_text(query, 256)!r} "
                     f"({order}) — REUSE the closest, don't respell "
                     "(call concept_card('<slug>') to decode one + see its track record):"]
            if partial_note:
                lines.insert(0, partial_note.rstrip())
            if want != "own" and source_summary.get("source_complete") is not True:
                lines.append(_partial_source_warning(source_summary))
            if capsule_scope_uncertain:
                lines.append(_partial_scope_warning(scope_receipt))
            for _r, negscore, slug, meta in scored[:limit]:
                lines.append(f"  [{_label[_scope(meta)]}] "
                             f"UNTRUSTED_MEMORY_CONCEPT={_safe_text(slug, 160)!r} "
                             f"[{_run_count(meta)} run(s)] match={-negscore:.0%}")
            lines.append(_receipt(candidates=len(scored), returned=min(len(scored), limit)))
            return "\n".join(lines)

        if name == "concept_card":
            import difflib
            raw_slug = args.get("slug")
            if not isinstance(raw_slug, str) or not raw_slug.strip():
                return "(cross-run tool error: slug must be a non-empty string)"
            if len(raw_slug) > 256:
                return "(cross-run tool error: slug exceeds 256 characters)"
            slug_in = raw_slug.strip()
            from looplab.engine.concept_registry import (canonicalize_concepts,
                                                         concept_governance_snapshot,
                                                         normalize_key, resolve_slug)
            from looplab.engine.memory import (_capsule_source_summary,
                                               _portfolio_concept_overview_data,
                                               _filter_capsule_rows, concept_profit_tendencies,
                                               portfolio_concept_graph)

            taxonomy = concept_governance_snapshot(self.dir)
            aliases, splits = taxonomy["aliases"], taxonomy["splits"]
            caps = self._all_capsules()
            prior_caps = _filter_capsule_rows(
                caps, lambda c: (not self._run_id
                                 or str(c.get("run_id") or "") != self._run_id))
            source_summary = _capsule_source_summary(prior_caps)
            scoped_caps, _unknown_scope_caps, scope_receipt = self._partition_capsules(prior_caps)
            # The DECODE vocabulary is GLOBAL (a concept means the same thing everywhere — the user's
            # "world concept map"); the trustworthy relative-rank TENDENCY below is task-family scoped.
            mine = set(canonicalize_concepts(sorted(self._concepts), aliases=aliases, splits=splits))
            # CODEX AGENT: keep the per-capsule canonical set. Besides avoiding repeated governance work,
            # this lets a card compute the requested concept's counts from every matching run instead of
            # looking it up in portfolio_concept_overview's intentionally display-capped top 512 rows.
            canonical_caps = [
                (cap, set(canonicalize_concepts(
                    cap.get("concepts") or [], aliases=aliases, splits=splits)))
                for cap in prior_caps
            ]
            global_vocab: set[str] = set(mine)
            for _cap, concepts in canonical_caps:
                global_vocab |= concepts

            # Resolve the input to a known canonical concept. Exact-canonical first; then, only for an
            # ESSENTIALLY-EXACT respelling (`rdrop` -> `regularization/r-drop`), decode it directly. A weaker
            # fuzzy neighbour is NOT rendered as an authoritative card (that would print CNN's whole track
            # record for `nn`); it is offered as a ranked "did you mean" list, mirroring find_concept_slugs,
            # so the agent picks the exact slug. Purge is checked BEFORE any fuzzy step: a slug whose alias
            # chain ends at a tombstone is deliberately retired and must never resolve to a live look-alike.
            cc = canonicalize_concepts([slug_in], aliases=aliases, splits=splits)
            canon = None
            resolution = "exact"
            if cc and cc[0] in global_vocab:
                canon = cc[0]
            elif normalize_key(slug_in) in aliases and resolve_slug(slug_in, aliases) is None:
                return (f"Concept {_safe_text(slug_in, 120)!r} has been PURGED from the taxonomy — "
                        "do not reuse it; mint a fresh `axis/name` if you need the idea.")
            else:
                qn = _slug_norm(slug_in)
                scored = []
                # Stable lexical traversal (set order is process-randomized) so equal-score spellings
                # resolve to ONE card on every worker. Scoring mirrors find_concept_slugs exactly:
                # exact-normalized = 1.0, substring = 0.9, else the SequenceMatcher ratio (surface >= 0.55).
                for s in sorted(global_vocab):
                    sn, ln = _slug_norm(s), _slug_norm(s.split("/")[-1])
                    if qn and (qn == sn or qn == ln):
                        score = 1.0
                    elif qn and (qn in sn or sn in qn):
                        score = 0.9
                    else:
                        score = max(difflib.SequenceMatcher(None, qn, sn).ratio(),
                                    difflib.SequenceMatcher(None, qn, ln).ratio())
                    if score >= 0.55:
                        scored.append((score, s))
                scored.sort(key=lambda t: (-t[0], t[1]))
                if scored and scored[0][0] >= 0.97:
                    canon, resolution = scored[0][1], "fuzzy"       # essentially the same slug -> decode it
                elif scored:
                    lines = [f"No exact concept card for {_safe_text(slug_in, 120)!r}; the closest existing "
                             "slug(s) — call concept_card again with the exact one you mean:"]
                    for sc, s in scored[:5]:
                        lines.append(f"  UNTRUSTED_MEMORY_CONCEPT={_safe_text(s, 160)!r} match={sc:.0%}")
                    return "\n".join(lines)
            if canon is None:
                from looplab.core.models import valid_concept_id
                if not valid_concept_id(slug_in):
                    return (f"No concept card for {_safe_text(slug_in, 120)!r}; that text is not a valid "
                            "concept slug. Search descriptive prose with find_concept_slugs, then reuse its "
                            "canonical `axis/name` result.")
                if source_summary.get("source_complete") is not True:
                    return (f"No RETAINED concept card for {_safe_text(slug_in, 120)!r}; the capsule source "
                            "is partial, so this is not proof the concept is new.\n"
                            + _partial_source_warning(source_summary))
                return (f"No concept card for {_safe_text(slug_in, 120)!r} — no run has used it, so it "
                        "looks NEW. Mint it as `axis/name` (call find_concept_slugs with no query to reuse "
                        "an existing axis).")

            axis, _, cname = canon.partition("/")
            lines = [f"CONCEPT CARD: UNTRUSTED_MEMORY_CONCEPT={_safe_text(canon, 160)!r}"
                     + ("" if resolution == "exact"
                        else f"  (you asked for {_safe_text(slug_in, 80)!r} — resolved by fuzzy match)")]
            lines.append(f"  axis={_safe_text(axis, 80)!r}"
                         + (f"  name={_safe_text(cname, 120)!r}" if cname else "  (no axis prefix)"))

            # Alternative spellings: every alias SOURCE whose chain resolves to this canonical.
            alt = sorted({src for src in aliases
                          if resolve_slug(src, aliases) == canon
                          and normalize_key(src) != normalize_key(canon)})
            if alt:
                lines.append("  also seen as: "
                             + ", ".join(f"UNTRUSTED_MEMORY={_safe_text(a, 80)!r}" for a in alt[:6]))

            # Track record: SCOPED overview = the trustworthy tendency; global count = portfolio context.
            scoped_ids = {id(capsule) for capsule in scoped_caps}
            scoped_canon_caps = [
                c for c, concepts in canonical_caps
                if canon in concepts and id(c) in scoped_ids]
            global_canon_caps = [c for c, concepts in canonical_caps if canon in concepts]
            # CODEX AGENT: absence/completeness denominators include every eligible capsule, not only rows
            # where this concept survived the bounded source projection. Matching rows still own the
            # observed metrics/signs, but a non-matching partial row may have omitted this exact concept.
            scoped_source_summary = _capsule_source_summary(scoped_caps)
            _scoped_overview, scoped_rows = _portfolio_concept_overview_data(
                scoped_canon_caps, aliases=aliases, splits=splits)
            row = next((r for r in scoped_rows if r["concept"] == canon), None)
            _global_overview, global_rows = _portfolio_concept_overview_data(
                global_canon_caps, aliases=aliases, splits=splits)
            # CODEX AGENT: card lookup is exact-key aggregation, not a display projection. Resolve the
            # requested row from the helper's complete retained aggregate, never its bounded first value.
            grow = next((r for r in global_rows if r["concept"] == canon), None)
            scoped_complete = (scoped_source_summary.get("source_complete") is True
                               and scope_receipt.get("scope_complete") is True)
            global_complete = source_summary.get("source_complete") is True
            if scoped_source_summary.get("source_complete") is not True:
                lines.append("  task-family " + _partial_source_warning(scoped_source_summary))
            if scope_receipt.get("scope_complete") is not True:
                lines.append("  task-family " + _partial_scope_warning(scope_receipt))
            if row:
                track_label = ("track record (your task family)" if scoped_complete
                               else "track record (returned task-family observations)")
                run_label = "run(s)" if scoped_complete else "retained run(s)"
                lines.append(f"  {track_label}: {row['n_runs']} {run_label} — "
                             f"ranked better {row['n_helped']} / middle {row['n_neutral']} / "
                             f"ranked worse {row['n_hurt']}")
                _sym = {1: "ranked-better", 0: "middle", -1: "ranked-worse"}
                for r in row["runs"][:6]:
                    m = r.get("metric")
                    lines.append(f"    - run {_safe_text(r.get('run_id'), 60)!r} "
                                 f"[{_sym.get(r.get('sign'), 'no-signal')}]"
                                 + (f" metric={m}" if m is not None else "")
                                 + f" ({_safe_text(r.get('direction'), 8)})")
            if canon in mine:
                lines.append("  NOTE: THIS run is already using this concept.")
            if global_complete:
                lines.append(f"  globally used in {grow['n_runs'] if grow else 0} prior run(s) "
                             "across the whole portfolio.")
            else:
                lines.append(f"  globally RETAINED in {grow['n_runs'] if grow else 0} prior run(s) "
                             "across returned portfolio records.")
                lines.append("  global " + _partial_source_warning(source_summary))

            # Use only the bound task-family row: global usage is context, never permission to let an
            # incompatible task reverse the actionable tendency shown beside the scoped track record.
            tend = concept_profit_tendencies([row] if row else [])
            # CODEX AGENT: an omitted matching row/sign can reverse a consistency claim. Keep the retained
            # observations visible above, but emit a directional tendency only for an exact scoped source.
            if scoped_complete and any(c == canon for c, _ in tend["helps"]):
                lines.append("  cross-run tendency: consistently RANKED BETTER within comparable runs "
                             "(advisory relative rank, not causal proof).")
            elif scoped_complete and any(c == canon for c, _ in tend["hurts"]):
                lines.append("  cross-run tendency: consistently RANKED WORSE within comparable runs — "
                             "only revisit with a specific new hypothesis for why it would differ here.")

            # Co-occurrence: concepts this one is usually paired with (scoped graph).
            graph = portfolio_concept_graph(scoped_canon_caps, aliases=aliases, splits=splits)
            partners: list[tuple] = []
            for e in graph["edges"]:
                if e.get("rel") != "co_occurs":
                    continue
                if e.get("src") == canon:
                    partners.append((e.get("dst"), e.get("n_runs", 0)))
                elif e.get("dst") == canon:
                    partners.append((e.get("src"), e.get("n_runs", 0)))
            partners.sort(key=lambda kv: (-kv[1], str(kv[0])))
            if partners:
                pair_label = "usually paired with" if scoped_complete else "paired in retained records with"
                lines.append(f"  {pair_label}: " + ", ".join(
                    f"UNTRUSTED_MEMORY={_safe_text(c, 80)!r}(×{n})" for c, n in partners[:6]))
            if graph.get("edge_source_complete") is not True:
                lines.append("  co-occurrence coverage: PARTIAL retained-node projection; "
                             f"{graph.get('edge_source_nodes_pruned', 0)} graph node(s) were pruned and "
                             "partners outside the projection are UNKNOWN.")

            # Lessons that mention it (free-text pros/cons) — match the name token in the statement.
            name_terms = _toks(cname or canon)
            notes = [lz for lz in self._role_lessons()
                     if name_terms and name_terms <= _toks(str(lz.get("statement") or ""))]
            if notes:
                lines.append("  what runs noted:")
                for lz in notes[:3]:
                    out = _safe_text(str(lz.get("outcome") or "noted"), 20)
                    lines.append(f"    [{out}] "
                                 f"UNTRUSTED_MEMORY={_safe_text(lz.get('statement'), 200)!r}")

            lines.append("  (No authored prose/paper overview yet — this card is assembled from cross-run "
                         "evidence; deep-research summarization is future work.)")
            lines.append(f"  [receipt scope={'bound_task_family' if self._bound else 'portfolio'} "
                         f"eligible_prior_runs={len(scoped_caps)} matching_scoped_runs="
                         f"{len(scoped_canon_caps)} matching_global_runs={len(global_canon_caps)} "
                         f"task_source_complete="
                         f"{str(scoped_source_summary.get('source_complete') is True).lower()} "
                         f"task_scope_complete="
                         f"{str(scope_receipt.get('scope_complete') is True).lower()} "
                         f"task_scope_unknown_capsules={scope_receipt.get('scope_unknown_capsules', 0)} "
                         f"global_source_complete={str(global_complete).lower()} "
                         f"cooccurrence_source_complete="
                         f"{str(graph.get('edge_source_complete') is True).lower()} "
                         f"cooccurrence_nodes_pruned={graph.get('edge_source_nodes_pruned', 0)} "
                         f"taxonomy_revision={taxonomy['governance_revision']}]")
            return "\n".join(lines)

        return "(unknown cross-run tool)"
