"""The claim CONTEXT PACK and the CR2a cross-run retrieval planner — the top of the subsystem.

Split out of the `claims.py` god-module (doc 25 EM-01) as the counterpart to `claims_health`: where
that module is the leaf everything stands on, this is the consumer nothing else depends on. It
builds the bounded evidence-and-counter-evidence pack a proposing agent reads, plans and executes
cross-run retrieval, and renders the portfolio atlas.

Keeping it here rather than in `claims.py` matters for one specific reason: this is the layer that
decides what an agent SEES. Retrieval quotas, caveat states and the atlas are selection-shaping
policy, and they were previously interleaved with the durable store and the governance ledger — so a
change to what gets surfaced sat in the same file as the code that decides what is true.

`claims.py` re-exports every name here (the `llm.py` / `agent.py` barrel), so both spellings resolve
to the SAME objects and existing imports and monkeypatch seams are unaffected.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from typing import Optional

from looplab.core.text import normalize_text, tokenize

# The shared leaf (see its docstring). Named explicitly rather than star-imported: every name this
# module needs from it is PRIVATE, and `import *` skips underscore names — so a wildcard here would
# resolve at import time and then NameError on the first call.
from looplab.engine.claims_health import (
    _CLAIM_WORD,
    _MAX_CONTEXT_CLAIMS,
    _MAX_RETRIEVAL_CORPUS,
    _MAX_DECISION_SCOPE,
    _MAX_RETRIEVAL_HITS,
    _bounded_claim_projection,
    _claim_source_rows,
    _claim_source_summary,
    _claim_text,
    _epistemic,
    _filter_claim_assessments,
    _filter_claim_source_rows,
    _identity_text,
    _node_ids,
    _research_source_summary,
    _safe_claim_read_health,
    _safe_claim_source_summary,
    _safe_research_source_summary,
    _string_list,
    _unknown_claim_source_summary,
    _valid_claim_source_rows,
    scope_cross_run_sources,
)
from looplab.engine.memory import _CLAIM_STANCES, _NEGATIVE, _filter_capsule_rows, normalize_statement
from looplab.trust.cross_run import (
    cross_run_identity_text,
    cross_run_text,
    sanitize_cross_run_projection,
)

# --------------------------------------------------------------------------- #
# Step 5 (§21.20.5): a BOUNDED context pack for a proposing agent — evidence AND counter-arguments.
# --------------------------------------------------------------------------- #

_CAVEAT_STATES = ("mixed", "refuted", "inconclusive")


def _claim_research_source_summary(claims) -> Optional[dict]:
    """Return one coherent aggregate receipt carried by all rows in an assessment snapshot."""
    carried = _safe_research_source_summary(getattr(claims, "research_source", None))
    if carried is not None:
        return carried
    rows = [row for row in (claims if isinstance(claims, (list, tuple)) else [])
            if isinstance(row, dict)]
    explicit = [_safe_research_source_summary(row.get("research_source")) for row in rows
                if "research_source" in row]
    if not explicit:
        return None
    first = explicit[0]
    if first is not None and len(explicit) == len(rows) and all(item == first for item in explicit[1:]):
        return first
    # A mixed/malformed snapshot is lower-bound evidence. Keep known counts for diagnosis, but fail the
    # completeness gate so no pack or steward can infer an exact positive from incompatible rows.
    base = first or _research_source_summary([])
    unknown = max(1, base["producer_unknown_runs"])
    runs = max(base["producer_runs"], unknown + base["producer_partial_runs"])
    return {
        **base,
        "source_complete": False,
        "producer_receipt_known": False,
        "producer_complete": False,
        "producer_runs": runs,
        "producer_unknown_runs": unknown,
    }


def _claim_claim_source_summary(claims) -> Optional[dict]:
    """Return one coherent lessons+research authority receipt, including for an empty snapshot."""
    carried = _safe_claim_source_summary(getattr(claims, "claim_source", None))
    if carried is not None:
        return carried
    rows = [row for row in (claims if isinstance(claims, (list, tuple)) else [])
            if isinstance(row, dict)]
    explicit = [_safe_claim_source_summary(row.get("claim_source")) for row in rows
                if "claim_source" in row]
    if not explicit:
        return None
    first = explicit[0]
    if first is not None and len(explicit) == len(rows) and all(item == first for item in explicit[1:]):
        return first
    return _unknown_claim_source_summary()


def build_context_pack(claims: list[dict], *, concept_overview: Optional[dict] = None,
                       max_claims: int = 5,
                       _concept_rows: Optional[list[dict]] = None,
                       _research_source: Optional[dict] = None,
                       _claim_source: Optional[dict] = None) -> dict:
    """Assemble a CLAIM-COUNT-bounded cross-run context pack from claim assessments (+ an optional concept
    overview) for a proposing agent (§21.20.5, Step 5). ("Claim-count", not token/byte: the pack caps the
    number of claims + per-claim field lengths; a true serialized-token envelope is the CR2b TODO — see the
    NOTE below.) The design's hard rule is that positive hits must
    never crowd out caveats. Precedence is pinned → ratified → mixed → supported → refuted →
    inconclusive, and a **caveat slot is reserved** whenever it can be filled by replacing the weakest
    non-pinned positive. The hard claim cap is never exceeded; pins beyond it are reported as omitted.
    Pure/deterministic and
    'silent' by construction — it just returns structured data; promoting it to advisory prompt-grounding
    is a separate, gated step (never wired here). No LLM, no I/O."""
    # NOTE: this bounds by CLAIM COUNT + per-claim field caps (below), not a serialized token/byte
    # budget — a true token envelope is the CR2b TODO. `max_claims<1` is normalized to 1.
    max_claims = max(1, min(int(max_claims), _MAX_CONTEXT_CLAIMS))
    # Governance precedence is explicit: rejected is absent; pinned is retention-critical; ratified is the
    # next preference; then evidence ordering. A caveat may replace a non-pinned positive, never a pin.
    live = [c for c in (claims or []) if c.get("maturity") != "operator-rejected"]
    _kept = {"operator-pinned", "operator-ratified"}
    pinned = [c for c in live if c.get("maturity") == "operator-pinned"]
    ratified = [c for c in live if c.get("maturity") == "operator-ratified"]
    rest = [c for c in live if c.get("maturity") not in _kept]
    by_state: dict[str, list] = {"mixed": [], "supported": [], "refuted": [], "inconclusive": []}
    for c in rest:
        by_state.get(c["epistemic"], by_state["inconclusive"]).append(c)
    ordered = (pinned + ratified + by_state["mixed"] + by_state["supported"]
               + by_state["refuted"] + by_state["inconclusive"])
    picked = ordered[:max_claims]
    # Reserved caveat slot: if nothing picked carries a caveat but caveats exist, swap the weakest NON-kept
    # picked (a governance-retained claim is never evicted to make room) for the strongest available caveat —
    # opposition is never crowded out by a full slate of positives (§20.5). Kept caveats count as caveats too.
    if picked and not any(c["epistemic"] in _CAVEAT_STATES for c in picked):
        # Include RATIFIED caveats too: a ratified mixed/refuted/inconclusive claim pushed past max_claims by
        # the ratified block must still be able to fill the reserved slot, or a slate of ratified-supported
        # claims could crowd opposition out — the exact §20.5 rule this slot exists to protect.
        caveats = ([c for c in pinned if c["epistemic"] in _CAVEAT_STATES]
                   + [c for c in ratified if c["epistemic"] in _CAVEAT_STATES]
                   + by_state["mixed"] + by_state["refuted"] + by_state["inconclusive"])
        # Evict the weakest non-pinned positive. Ratification raises priority but may still yield to a
        # caveat; a pin is the explicit retention guarantee and cannot be displaced. If the cutoff is all
        # pins there is no legal victim, so the caveat remains outside this bounded projection.
        victim = next((i for i in range(len(picked) - 1, -1, -1)
                       if picked[i].get("maturity") != "operator-pinned"), None)
        if caveats and victim is not None:
            picked = picked[:victim] + picked[victim + 1:] + [caveats[0]]

    def _slim(c: dict) -> dict:
        # Evidence refs are run-QUALIFIED ("run:node"), so the truncated support/oppose lists stay citable;
        # keep runs/scopes too so a reader can resolve the claim's provenance.
        return {"statement": _claim_text(c.get("statement"), 300), "epistemic": c["epistemic"],
                "maturity": c.get("maturity", "machine-proposed"),
                "claim_uid": c.get("claim_uid", ""), "scope": c.get("scope", ""),
                "evidence_digest": c.get("evidence_digest", ""),
                "decision_fresh": c.get("decision_fresh"),
                "metric": c.get("metric", ""), "polarity": c.get("polarity"),
                "n_support": c["n_support"], "n_oppose": c["n_oppose"],
                "n_unverified": c.get("n_unverified", 0),
                "support": c["support"][:6], "oppose": c["oppose"][:6],
                "unverified": c.get("unverified", [])[:6],
                # Structured polarity contradictions are assertion-level counter-evidence,
                # not entries in ``oppose``. Keep their bounded text or a mixed claim renders as 1↑/0↓
                # with no visible reason for the disagreement.
                "contradicts": _string_list(c.get("contradicts"), maximum=4, item_maximum=300),
                "runs": [_identity_text(value, 500) for value in c.get("runs", [])[:6]],
                "scopes": [_identity_text(value, _MAX_DECISION_SCOPE)
                           for value in c.get("scopes", [])[:6]]}

    pack = {
        "claims": [_slim(c) for c in picked],
        "n_claims_total": len(claims or []),
        "n_contested": sum(1 for c in live if c.get("epistemic") == "mixed"),
        # Pins have highest priority but cannot override the hard prompt-size cap. Surface any overflow
        # explicitly so a bounded advisory never implies that it retained every operator pin.
        "n_pinned_total": len(pinned),
        "n_pinned_omitted": max(0, len(pinned) - sum(
            1 for c in picked if c.get("maturity") == "operator-pinned")),
    }
    research_source = (_safe_research_source_summary(_research_source)
                       if _research_source is not None
                       else _claim_research_source_summary(claims))
    if _research_source is not None and research_source is None:
        research_source = {
            **_research_source_summary([]),
            "source_complete": False,
            "producer_receipt_known": False,
            "producer_complete": False,
            "producer_runs": 1,
            "producer_unknown_runs": 1,
        }
    if research_source is not None:
        pack["research_source"] = research_source
    claim_source = (_safe_claim_source_summary(_claim_source)
                    if _claim_source is not None else _claim_claim_source_summary(claims))
    if _claim_source is not None and claim_source is None:
        claim_source = _unknown_claim_source_summary()
    if claim_source is not None:
        pack["claim_source"] = claim_source
    if concept_overview:
        from looplab.engine.memory import concept_profit_tendencies
        # callers that own the retained capsule snapshot may supply its private pre-cap rows.
        # The pack still emits only `max_claims` labels/tendencies; this prevents the public overview's
        # display cap from becoming a silent analytics cap while keeping the outward prompt bounded.
        row_source = (_concept_rows if _concept_rows is not None
                      else concept_overview.get("concepts"))
        rows = [e for e in (row_source or []) if isinstance(e, dict)]
        source_complete = concept_overview.get("source_complete") is True
        # PART V Phase 1 profit signal: surface concepts with a CONSISTENT, MULTI-RUN rank tendency (advisory
        # only — prompts, never selection). The threshold lives in ONE shared helper so the context pack and
        # the cross_run_atlas tool can never diverge; a concept with mixed/thin evidence appears in neither.
        # consistency also needs a complete denominator. A non-matching partial capsule may
        # have omitted this exact concept and an opposite sign, so retained positive rows remain observable
        # below but cannot support a directional portfolio tendency until every capsule receipt is exact.
        tendency = (concept_profit_tendencies(rows, limit=max_claims) if source_complete
                    else {"helps": [], "hurts": []})
        pack["coverage"] = {
            "n_runs": concept_overview.get("n_runs", 0),
            "n_concepts": concept_overview.get("n_concepts", 0),
            # A hand-built/older overview with no receipt is UNKNOWN, never silently exact.
            "source_complete": source_complete,
            "partial_capsules": concept_overview.get(
                "partial_capsules",
                concept_overview.get("n_runs", 0) if "source_complete" not in concept_overview else 0),
            "source_unknown_capsules": concept_overview.get(
                "source_unknown_capsules",
                concept_overview.get("n_runs", 0) if "source_complete" not in concept_overview else 0),
            "source_concepts_omitted": concept_overview.get("source_concepts_omitted", 0),
            "source_outcomes_omitted": concept_overview.get("source_outcomes_omitted", 0),
            "source_store_complete": concept_overview.get(
                "source_store_complete", source_complete) is True,
            "source_rows_total": concept_overview.get("source_rows_total", 0),
            "source_rows_quarantined": concept_overview.get("source_rows_quarantined", 0),
            "source_malformed_rows": concept_overview.get("source_malformed_rows", 0),
            "source_invalid_capsule_rows": concept_overview.get(
                "source_invalid_capsule_rows", 0),
            "source_duplicate_run_rows": concept_overview.get("source_duplicate_run_rows", 0),
            "top_concepts": [_claim_text(e.get("concept"), 500) for e in rows[:max_claims]],
            # E3: keep the run COUNT (n_helped/n_hurt) in the rendered span — "loss/contrastive (n=7)"
            # vs "(n=2)" tells the Researcher how strong the multi-run tendency is, not just its direction.
            "helps": [f"{_claim_text(c, 480)} (n={int(n)})" for c, n in tendency["helps"]],
            "hurts": [f"{_claim_text(c, 480)} (n={int(n)})" for c, n in tendency["hurts"]],
        }
    return pack


# Deterministic query-INTENT cues (CR2a eligibility). Kept ML-context-safe: ambiguous technique words
# ("negative", "loss") are NOT cues, so "hard negatives for retrieval" reads as neutral EXPLORE, not FAILED.
_INTENT_CUES = {
    "failed":    frozenset("fail failed failing avoid avoided pitfall pitfalls mistake mistakes wrong "
                           "broke broken regress regression hurt hurts degrade degrades harmful useless "
                           "ineffective".split()),
    "contested": frozenset("contested contradict contradiction conflict conflicting disagree disagreement "
                           "controversial controversy debate unclear uncertain".split()),
    "worked":    frozenset("best proven effective recommend recommended success successful reliable robust "
                           "winning champion".split()),
}
# The CONTRADICTION pool for the retrieval quota — claims that carry actual OPPOSITION (mixed=contested,
# refuted=negative verdict). This is DELIBERATELY narrower than build_context_pack's `_CAVEAT_STATES`
# (which also includes `inconclusive`): the context-pack reserves a slot so a clean slate of positives can't
# hide any NON-positive (§21.20.5 coverage), whereas the retrieval quota reserves slots specifically for
# COUNTER-EVIDENCE/contradictions — an inconclusive (no-stance) claim is neither. Two distinct mechanisms,
# not an accidental inconsistency (concept-conformance).
_CAVEAT = frozenset(("mixed", "refuted"))
# Tie-break order for `_classify_intent` when two intents match the same number of cues: the intents that
# RAISE the caveat/contradiction quota win, so a mixed query surfaces counter-evidence rather than burying
# it. `contested` outranks `failed` because it is the narrower, more specific signal of the two.
_INTENT_TIE_RANK = {"contested": 2, "failed": 1, "worked": 0}


def _classify_intent(query: str) -> str:
    """Map a free-text query to a retrieval INTENT (failed / contested / worked / explore) by cue overlap.
    Deterministic, no LLM. `explore` (neutral) when no cue fires — the safe default that reorders nothing."""
    # DEFERRED: `claims.py` imports THIS module to re-export it, so importing back at module
    # scope would cycle. These names live in the ledger/store half of the split (EM-01).
    toks = set(_CLAIM_WORD.findall(str(query or "").casefold()))   # cue match: no NFKC, the cues are ASCII
    scored = [(sum(1 for w in cues if w in toks), name) for name, cues in _INTENT_CUES.items()]
    # An equal cue count is broken CAVEAT-FIRST, not alphabetically. The tie-break used to be the intent
    # NAME, which always resolves to the alphabetically-largest — "worked" > "failed" > "contested" — so a
    # genuinely mixed query ("avoid the failed approach, use the best proven method": one failed cue, one
    # worked cue) classified as "worked", floating positives and leaving the caveat/contradiction quota
    # unraised (only failed/contested raise it). That biased ties toward HIDING counter-evidence, the exact
    # inverse of this module's §21.20.5 caveat-preservation intent. Still total and deterministic.
    best_n, best = max(scored, key=lambda t: (t[0], _INTENT_TIE_RANK.get(t[1], 0)))
    return best if best_n else "explore"


def _eligible(kind: str, meta: dict, intent: str) -> bool:
    """Whether a doc is on-INTENT (a soft priority signal, never a hard exclusion — counter-evidence is
    still returned). Concepts are always eligible; a claim's eligibility depends on its epistemic/maturity."""
    if kind != "claim" or intent == "explore":
        return True
    ep, mat = meta.get("epistemic"), meta.get("maturity")
    if intent == "failed":
        return ep in _CAVEAT
    if intent == "contested":
        return ep == "mixed"
    if intent == "worked":
        return ep == "supported" or mat == "operator-ratified"
    return True


_INTENTS = ("failed", "contested", "worked", "explore")

# Document ids are a stable identity for the same searchable statement/concept.  The corpus digest has a
# separate schema because it also commits to aggregate source receipts that do not belong in every doc id.
_RETRIEVAL_DOCUMENT_VERSION = 2
_RETRIEVAL_CORPUS_VERSION = 7
_INTENT_SCORE_BONUS = 0.001
_CAVEAT_SCORE_RATIO = 0.50
_CAVEAT_QUERY_COVERAGE = 0.10


def _retrieval_tokens(text: str) -> frozenset[str]:
    # DEFERRED: `claims.py` imports THIS module to re-export it, so importing back at module
    # scope would cycle. These names live in the ledger/store half of the split (EM-01).
    return frozenset(tokenize(text))


def _lexical_relevance(query: str, text: str) -> tuple[int, float, float]:
    q, d = _retrieval_tokens(query), _retrieval_tokens(text)
    shared = len(q & d)
    coverage = shared / len(q) if q else 0.0
    jaccard = shared / len(q | d) if q or d else 0.0
    return shared, coverage, jaccard


def _json_digest(value, *, length: int = 20) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def _retrieval_doc(kind: str, text: str, meta: dict) -> tuple[str, str, dict]:
    identity = {"v": _RETRIEVAL_DOCUMENT_VERSION, "kind": kind,
                "claim_uid": str(meta.get("claim_uid") or ""),
                "metric": str(meta.get("metric") or ""),
                "text": " ".join(normalize_text(text).split())}
    stable_id = f"{kind[:1]}_{_json_digest(identity, length=16)}"
    return kind, str(text or ""), {**meta, "stable_id": stable_id}


def _retrieval_corpus_digest(docs, *, concept_source: dict, research_source: dict,
                             claim_source: dict) -> str:
    canonical = [{"kind": kind, "text": text, "meta": meta}
                 for kind, text, meta in sorted(docs, key=lambda d: d[2]["stable_id"])]
    envelope = {"v": _RETRIEVAL_CORPUS_VERSION, "docs": canonical,
                "concept_source": concept_source, "research_source": research_source,
                "claim_source": claim_source}
    return _json_digest(envelope, length=20)


def _preselect_retrieval_docs(docs, query: str, limit: int):
    """Cheap query-aware cap with one best row per source kind before the expensive hybrid index."""
    cap = max(1, int(limit))
    if len(docs) <= cap:
        return list(docs)
    stats = [_lexical_relevance(query, d[1]) for d in docs]
    ranked = sorted(range(len(docs)),
                    key=lambda i: (-stats[i][0], -stats[i][1], -stats[i][2],
                                   docs[i][2]["stable_id"]))
    selected: list[int] = []
    kinds = sorted({d[0] for d in docs})
    if cap >= len(kinds):
        for kind in kinds:
            selected.append(next(i for i in ranked if docs[i][0] == kind))
    selected_set = set(selected)
    selected.extend(i for i in ranked if i not in selected_set)
    return [docs[i] for i in selected[:cap]]


def cross_run_retrieve(memory_dir, query: str, *, k: int = 8, lessons=None, capsules=None,
                       research_claims=None, scope_task: str = "", contradiction_quota: float = 0.34,
                       max_corpus: int = 2000, structured: bool = False, intent: Optional[str] = None,
                       scope_receipt: Optional[dict] = None,
                       _governance: Optional[dict] = None) -> dict:
    """CR2a retrieval planner (§21.20.5, full CR): RRF-fuse the portfolio's cross-run KNOWLEDGE — claims
    (epistemic state / operator maturity) + concepts (#runs) — over the shipped `HybridRetriever`
    (lexical + BM25 + vector; reuses hybrid_merge, NO new fuser), then shape the ranked recall with:

    - INTENT classification (`failed`/`contested`/`worked`/`explore`) → an eligibility priority so an
      on-intent claim floats up (soft; never hides counter-evidence);
    - a CONTRADICTION QUOTA reserving ~`contradiction_quota` of the k slots for caveat (mixed/refuted)
      claims when they exist, so a positive-heavy recall never buries the counter-evidence (mirrors the
      context pack's caveat slot). `failed`/`contested` intents raise the quota;
    - a bounded corpus (`max_corpus`, truncation REPORTED not silent) + a why-recalled RECEIPT (intent,
      quota, corpus digest, degraded-channel note, per-hit rank).

    Every source is SCOPED before indexing: pass scoped `lessons`/`capsules` plus their aggregate
    `scope_receipt`, and `scope_task` filters the D8 research claims to that task so a task-bound agent
    cannot retrieve another task's claims.
    Operator-rejected claims never enter the corpus. Advisory; pure w.r.t. the passed/loaded stores."""
    # DEFERRED: `claims.py` imports THIS module to re-export it, so importing back at module
    # scope would cycle. These names live in the ledger/store half of the split (EM-01).
    from looplab.engine.claims import claim_assessments, load_claim_lessons, load_research_claims
    from pathlib import Path

    from looplab.engine.governance_health import observed_path_missing, project_governed_sources
    from looplab.engine.memory import (ConceptCapsuleStore, _filter_capsule_rows,
                                       _portfolio_concept_overview_data)
    if _governance is None:
        source_names = []
        if lessons is None:
            source_names.append("lessons.jsonl")
        if research_claims is None:
            source_names.append("research_claims.jsonl")
        if capsules is None:
            source_names.append("concept_capsules.jsonl")
        return project_governed_sources(
            memory_dir,
            lambda governance: cross_run_retrieve(
                memory_dir, query, k=k, lessons=lessons, capsules=capsules,
                research_claims=research_claims, scope_task=scope_task,
                contradiction_quota=contradiction_quota, max_corpus=max_corpus,
                structured=structured, intent=intent, scope_receipt=scope_receipt,
                _governance=governance,
            ),
            include_concepts=True, source_names=source_names,
        )
    base = Path(memory_dir) if memory_dir else None
    if capsules is None:
        cp = base / "concept_capsules.jsonl" if base else None
        capsules = (ConceptCapsuleStore(cp).all()
                    if cp and not observed_path_missing(cp) else [])
    if lessons is None:
        lessons = load_claim_lessons(memory_dir)
    lessons = _valid_claim_source_rows(lessons, research=False)
    # Scope EVERY source before joining. Decisions are a governance overlay; they never grant visibility.
    research = load_research_claims(memory_dir) if research_claims is None else research_claims
    lessons, capsules, research = scope_cross_run_sources(
        task_id=scope_task, lessons=lessons, capsules=capsules, research=research)
    research = _valid_claim_source_rows(research, research=True)
    research_source = _research_source_summary(research)
    governance = _governance
    claims = _filter_claim_assessments(
        claim_assessments(lessons, research_claims=research,
                          decisions=governance["decisions"], structured=structured),
        lambda c: c.get("maturity") != "operator-rejected")
    claim_source = (_safe_claim_source_summary(claims.claim_source)
                    or _claim_source_summary(lessons, research, research_source=research_source))
    overview, concept_rows = _portfolio_concept_overview_data(
        capsules, aliases=governance["aliases"], splits=governance["splits"])
    # source completeness is part of the retrieval corpus, even when a query happens to match
    # only claims or the same retained concept rows.  Aggregate it across every eligible capsule before
    # query preselection so legacy/omitted concepts cannot masquerade as authoritative absence or exact
    # frequency, and a partial->complete transition changes the auditable corpus identity.
    concept_source = {
        "n_capsules": overview["n_runs"],
        "source_complete": overview.get("source_complete") is True,
        "partial_capsules": int(overview.get("partial_capsules", 0) or 0),
        "source_unknown_capsules": int(overview.get("source_unknown_capsules", 0) or 0),
        "source_concepts_omitted": int(overview.get("source_concepts_omitted", 0) or 0),
        "source_outcomes_omitted": int(overview.get("source_outcomes_omitted", 0) or 0),
        # The public overview is independently bounded. Commit both its display omission and the exact
        # retained concept cardinality to the corpus identity so a cap change/tail cannot look identical.
        "concepts_total": len(concept_rows),
        "overview_concepts_omitted": int(overview.get("concepts_omitted", 0) or 0),
        "source_store_complete": overview.get("source_store_complete") is True,
        "source_rows_total": int(overview.get("source_rows_total", 0) or 0),
        "source_rows_quarantined": int(overview.get("source_rows_quarantined", 0) or 0),
        "source_malformed_rows": int(overview.get("source_malformed_rows", 0) or 0),
        "source_invalid_capsule_rows": int(
            overview.get("source_invalid_capsule_rows", 0) or 0),
        "source_duplicate_run_rows": int(overview.get("source_duplicate_run_rows", 0) or 0),
    }
    scope_keys = (
        "scope_unknown_capsules", "scope_fingerprint_unknown_capsules",
        "scope_fingerprint_items_omitted", "scope_direction_unknown_capsules",
    )
    if scope_receipt is None:
        scope_source = {"scope_receipt_known": True, "scope_complete": True,
                        **{key: 0 for key in scope_keys}}
    else:
        source = scope_receipt if isinstance(scope_receipt, dict) else {}
        counts_valid = all(
            isinstance(source.get(key), int) and not isinstance(source.get(key), bool)
            and source.get(key) >= 0 for key in scope_keys
        )
        complete_valid = type(source.get("scope_complete")) is bool
        unknown = source.get("scope_unknown_capsules") if counts_valid else 0
        fingerprint_unknown = source.get("scope_fingerprint_unknown_capsules", 0) if counts_valid else 0
        direction_unknown = source.get("scope_direction_unknown_capsules", 0) if counts_valid else 0
        consistent = (complete_valid and counts_valid
                      and source.get("scope_complete") == (unknown == 0)
                      and fingerprint_unknown + direction_unknown <= unknown)
        scope_source = {
            "scope_receipt_known": consistent,
            # a caller-supplied malformed applicability receipt fails closed. Retrieval may
            # retain its positive documents, but neither an empty result nor a frequency is exact.
            "scope_complete": consistent and source.get("scope_complete") is True,
            **{key: source.get(key) if counts_valid else 0 for key in scope_keys},
        }
    concept_source.update(scope_source)
    docs: list[tuple[str, str, dict]] = []
    for c in claims:
        evidence_digest = _json_digest({"support": c.get("support", []), "oppose": c.get("oppose", []),
                                        "unverified": c.get("unverified", []),
                                        "sources": c.get("sources", []),
                                        "research_source": c.get("research_source"),
                                        "claim_source": c.get("claim_source")})
        docs.append(_retrieval_doc("claim", c["statement"], {
            "epistemic": c["epistemic"], "n_support": c["n_support"],
            "n_oppose": c["n_oppose"], "n_unverified": c.get("n_unverified", 0),
            "contradicts": _string_list(c.get("contradicts"), maximum=4, item_maximum=300),
            "maturity": c.get("maturity"), "claim_uid": c.get("claim_uid", ""),
            "decision_fresh": c.get("decision_fresh"),
            "metric": c.get("metric", ""), "scopes": c.get("scopes", []),
            "research_source": c.get("research_source", research_source),
            "claim_source": c.get("claim_source", claim_source),
            "decision_revision": (c.get("decision") or {}).get("revision"),
            "governance_digest": _json_digest(c.get("decision") or {}),
            "evidence_digest": evidence_digest}))
    # query-aware preselection must see every validated canonical row. Iterating the public
    # top-512 projection made concept #513 look absent with source_complete=true and truncated=0.
    for e in concept_rows:
        docs.append(_retrieval_doc("concept", _claim_text(e.get("concept"), 500), {
            "n_runs": e["n_runs"],
            "runs": [_identity_text(r.get("run_id"), 500) for r in e["runs"][:5]
                     if isinstance(r, dict)],
            "evidence_digest": _json_digest(e["runs"])}))

    n_total = len(docs)
    max_corpus = max(1, min(int(max_corpus), _MAX_RETRIEVAL_CORPUS))
    indexed_docs = _preselect_retrieval_docs(docs, str(query or ""), max_corpus)
    truncated = n_total - len(indexed_docs)
    concepts_indexed = sum(kind == "concept" for kind, _text, _meta in indexed_docs)
    claims_indexed = sum(kind == "claim" for kind, _text, _meta in indexed_docs)
    projection_receipt = {
        "concepts_indexed": concepts_indexed,
        "concepts_omitted": len(concept_rows) - concepts_indexed,
        "claims_total": len(claims),
        "claims_indexed": claims_indexed,
        "claims_omitted": len(claims) - claims_indexed,
    }
    corpus_digest = _retrieval_corpus_digest(
        docs, concept_source=concept_source, research_source=research_source,
        claim_source=claim_source)
    # COST, stated exactly. `max_corpus` bounds the hybrid INDEX (`indexed_docs`) and the
    # `retrieval_digest` below is computed over that bounded set — but this `corpus_digest` covers
    # every row, sorting and serializing each full claim/concept. That is inherent to what it means:
    # a corpus REVISION has to change when any stored row changes, so a bounded sample cannot express
    # it. It is also not the only O(n) term — building `docs` in full is deliberate and load-bearing
    # (see the preselection note above: iterating the public top-512 projection made concept #513 look
    # absent with source_complete=true and truncated=0), so preselection has already visited every row
    # before this runs. One retrieval request is therefore O(all stored evidence) in CPU and memory
    # regardless of `max_corpus`, and portfolio growth scales it.
    # Fixing that is not a local change: it needs the revision PERSISTED and maintained incrementally
    # as rows are written, plus a bounded candidate index that can still emit an honest omission
    # receipt. That is scalability infrastructure over the durable claim/concept stores — tracked work,
    # not a repair — and a partial version (e.g. combining per-row hashes here) would change the
    # receipt value that governance consumers compare while still leaving the request O(n).
    indexed_source = {**concept_source, **projection_receipt}
    retrieval_source = {**indexed_source, "research_source": research_source,
                        "claim_source": claim_source}
    # The AGENT may pass an explicit `intent` (it knows why it is searching — genuinely agentic); otherwise
    # classify deterministically from the query text. An unknown value falls back to classification.
    intent = intent if intent in _INTENTS else _classify_intent(query)
    kk = max(1, min(int(k), _MAX_RETRIEVAL_HITS))
    try:
        base_quota = float(contradiction_quota)
    except (TypeError, ValueError):
        base_quota = 0.34
    if not math.isfinite(base_quota):
        base_quota = 0.34
    base_quota = min(1.0, max(0.0, base_quota))
    q = max(base_quota, 0.5) if intent in ("failed", "contested") else base_quota
    target = min(math.ceil(kk * q), max(0, kk - 1))
    # A why-recalled receipt: corpus revision (content digest), the degraded vector-channel semantics, the
    # classified intent + quota, and (below) the per-hit rank — enough to explain/reproduce a result.
    receipt = {"query": _claim_text(query, 4000), "k": kk, "n_corpus": n_total,
               "n_indexed": len(indexed_docs), "corpus_digest_version": _RETRIEVAL_CORPUS_VERSION,
               "channels": ["lexical", "bm25", "vector"], "intent": intent,
               "vector_channel": "hash_embed(64-bucket bag-of-words; lexical proxy, not semantic)",
               "corpus_digest": corpus_digest,
               "retrieval_digest": _retrieval_corpus_digest(
                   indexed_docs, concept_source=indexed_source,
                   research_source=research_source, claim_source=claim_source),
               "truncated": truncated,
               "preselection": "query-overlap+one-per-source/v1",
               "contradiction_quota": round(base_quota, 3),
               "effective_quota": round(q, 3), "caveat_target": target,
               "caveat_score_ratio": _CAVEAT_SCORE_RATIO,
                "caveat_query_coverage": _CAVEAT_QUERY_COVERAGE,
                "intent_score_bonus": _INTENT_SCORE_BONUS,
                "governance_complete": True,
                "claim_governance_revision": governance["claim_revision"],
                "concept_alias_revision": governance["alias_revision"],
                "concept_split_revision": governance["split_revision"],
                "concept_governance_revision": governance["concept_governance_revision"],
                **retrieval_source}
    if not indexed_docs or not str(query or "").strip():
        return {"results": [], "receipt": {**receipt, "n_hits": 0, "n_caveats": 0}}

    from looplab.search.hybrid_merge import HybridRetriever
    # Retrieve a POOL larger than k so the intent priority + contradiction quota have room to reorder/swap
    # without extra queries; the vector channel is the `hash_embed` bag-of-words (a lexical proxy — declared
    # in the receipt, not passed off as semantic retrieval).
    pool_n = min(len(indexed_docs), max(kk * 4, kk + 12))
    pool = HybridRetriever([t for _, t, _ in indexed_docs]).candidates(str(query), k=pool_n)
    ranked = []
    for rel_rank, (i, score) in enumerate(pool):
        kind, text, meta = indexed_docs[i]
        shared, coverage, jaccard = _lexical_relevance(str(query), text)
        eligible = _eligible(kind, meta, intent)
        # Intent is a bounded tiebreak-like bonus scaled by actual query overlap, never a hard tier that can
        # lift an unrelated "failed" memory above a strongly relevant positive result.
        bonus = (_INTENT_SCORE_BONUS * min(1.0, coverage * 2.0)
                 if intent != "explore" and eligible and shared else 0.0)
        ranked.append({"idx": i, "kind": kind, "text": text, "score": round(float(score), 6),
                       "intent_bonus": round(bonus, 6), "query_overlap": shared,
                       "query_coverage": round(coverage, 4), "query_jaccard": round(jaccard, 4),
                       "rel_rank": rel_rank, **meta})
    ranked.sort(key=lambda h: (-(h["score"] + h["intent_bonus"]), h["rel_rank"], h["stable_id"]))
    picked = ranked[:kk]

    # CONTRADICTION QUOTA: guarantee ~quota of the k slots are caveat (mixed/refuted) claims when the pool
    # has them — swapping the LEAST-relevant non-caveat picks (from the bottom) for the most-relevant unpicked
    # caveats, so the top relevance hit is never displaced and opposition is never crowded out.
    # ceil(k*q) caveat slots, but capped at k-1 so the #1 relevance hit is NEVER evicted (at k=1 the target
    # is 0 — the single slot stays the top hit, as the swap contract promises; mega-review finding).
    have = [h for h in picked if h["kind"] == "claim" and h.get("epistemic") in _CAVEAT]
    if target > len(have):
        picked_ids = {h["idx"] for h in picked}
        top_score = max((h["score"] for h in ranked), default=0.0)
        extra = [h for h in ranked if h["idx"] not in picked_ids
                 and h["kind"] == "claim" and h.get("epistemic") in _CAVEAT
                 and h["query_coverage"] >= _CAVEAT_QUERY_COVERAGE
                 and h["score"] >= top_score * _CAVEAT_SCORE_RATIO]
        need = target - len(have)
        for cav in extra[:need]:
            # Keep the raw relevance winner (rel_rank 0). Quotas reserve relevant counter-evidence, not an
            # unrelated caveat selected solely for its epistemic label. Also NEVER evict an operator-PINNED
            # claim — the "pinned is retained" governance projection applies to EVERY consumer, not just the
            # context pack (concept-conformance: §22.4 / §21.20.5, mirroring build_context_pack).
            victim = next((h for h in reversed(picked)
                           if not (h["kind"] == "claim" and h.get("epistemic") in _CAVEAT)
                           and h["rel_rank"] != 0 and h.get("maturity") != "operator-pinned"), None)
            if victim is None:
                break
            picked[picked.index(victim)] = cav
        picked.sort(key=lambda h: (-(h["score"] + h["intent_bonus"]),
                                   h["rel_rank"], h["stable_id"]))

    n_caveats = sum(1 for h in picked if h["kind"] == "claim" and h.get("epistemic") in _CAVEAT)
    results = [{k2: v for k2, v in h.items() if k2 != "idx"} for h in picked]
    # Report the EFFECTIVE quota actually applied (raised for failed/contested) + the reserved caveat target,
    # so the receipt explains why a contested claim was (or wasn't) surfaced — not just the configured base.
    return {"results": results,
            "receipt": {**receipt, "n_hits": len(results), "n_caveats": n_caveats}}


def portfolio_atlas(lessons: list[dict], capsules: list[dict], *, max_items: int = 8,
                    decisions: Optional[dict] = None, research_claims: Optional[list[dict]] = None,
                    aliases: Optional[dict] = None, splits: Optional[dict] = None,
                    structured: bool = False) -> dict:
    """The Research Atlas DATA payload (§21.20 Step 6): one structured bounded observation/mixed-evidence
    view, composing the concept overview (Step 3), the claim
    assessments (Step 4) and the bounded context pack (Step 5). Pure/deterministic — the read-model a
    Research Atlas UI (or an agent) would render; no LLM, no I/O.

    The legacy ``thin_coverage`` field means only "observed in one returned run". It is not a gap or coverage
    assertion: a true CoverageFrame (§20.6, unknown-vs-zero) needs a frozen scope, eligible denominator and
    health contract, which remain deferred full-CR3a work."""
    # DEFERRED: `claims.py` imports THIS module to re-export it, so importing back at module
    # scope would cycle. These names live in the ledger/store half of the split (EM-01).
    from looplab.engine.claims import claim_assessments
    from looplab.engine.memory import _dedup_valid_capsules, _portfolio_concept_overview_data
    max_items = max(1, min(int(max_items), 100))             # route/CLI-independent hard envelope
    source_capsules = capsules if isinstance(capsules, (list, tuple)) else []
    capsules = _dedup_valid_capsules(source_capsules)
    overview, full_concept_rows = _portfolio_concept_overview_data(
        capsules, aliases=aliases, splits=splits)
    # Keep the complete internal sets for exact run totals and the governance evidence digest. Only the
    # outward contradictions/context projections are capped below.
    claims = claim_assessments(lessons, research_claims=research_claims, decisions=decisions,
                               structured=structured, bounded=False)
    research_source = (_safe_research_source_summary(getattr(claims, "research_source", None))
                       or _research_source_summary(
                           _valid_claim_source_rows(research_claims, research=True)))
    claim_source = (_safe_claim_source_summary(getattr(claims, "claim_source", None))
                    or _claim_source_summary(lessons, research_claims,
                                             research_source=research_source))
    # A contradiction the operator REJECTED is no longer live, consistent with build_context_pack and
    # cross_run_claims. Pin priority applies inside the embedded context pack; this human-facing contested
    # summary remains evidence-ordered and independently capped.
    contested = [c for c in claims if c["epistemic"] == "mixed" and c.get("maturity") != "operator-rejected"]
    # Atlas is independently bounded. Derive single-run observations and rank tendencies from
    # every canonical retained row BEFORE its outward cap; the old overview-capped path silently returned
    # `thin_coverage=[]` once 512 more-frequent concepts occupied the entire overview projection.
    thin = [e["concept"] for e in full_concept_rows if e["n_runs"] == 1]
    # Run count spans BOTH sources — capsules AND the runs cited by lessons — so a lesson-only / legacy
    # memory (no opt-in capsules) is not reported as zero runs. The authoritative scoped corpus
    # join (cross_run_index) is the full-CR TODO; this at least unions what the two memory stores know.
    run_ids = {c.get("run_id") for c in capsules if c.get("run_id")}
    for cl in claims:
        run_ids.update(cl.get("runs") or [])
    n_runs = len(run_ids)
    # Keep the embedded context-pack coverage n_runs CONSISTENT with the top-level count (both the union of
    # capsule + lesson-cited runs), so one atlas payload never reports two different run counts — otherwise a
    # lesson-only memory says n_runs>0 at the top but coverage.n_runs==0, the very "zero runs" artifact the
    # union set out to fix.
    pack_overview = {**overview, "n_runs": n_runs}
    explored = full_concept_rows[:max_items]
    thin_coverage = thin[:max_items]
    contradictions = [_bounded_claim_projection(row) for row in contested[:max_items]]
    payload = {
        "n_runs": n_runs, "n_concepts": overview["n_concepts"],
        "n_claims": len(claims), "n_contested": len(contested),
        # the Atlas UI must not infer capsule-source completeness from returned rows or from
        # transport freshness. Keep one small aggregate receipt at the read-model boundary; the embedded
        # context-pack copy remains for agents and backward-compatible consumers.
        "concept_source": {key: overview[key] for key in (
            "source_complete", "partial_capsules", "source_unknown_capsules",
            "source_concepts_omitted", "source_outcomes_omitted",
            "source_store_complete", "source_rows_total", "source_rows_quarantined",
            "source_malformed_rows", "source_invalid_capsule_rows",
            "source_duplicate_run_rows",
        )},
        "research_source": research_source,
        "claim_source": claim_source,
        "explored": explored,                               # what's been tried (concept × runs)
        "explored_total": len(full_concept_rows),
        "explored_omitted": len(full_concept_rows) - len(explored),
        "thin_coverage": thin_coverage,                     # legacy key: observed in one returned run
        "thin_coverage_total": len(thin),
        "thin_coverage_omitted": len(thin) - len(thin_coverage),
        "contradictions": contradictions,
        "contradictions_total": len(contested),
        "contradictions_omitted": len(contested) - len(contradictions),
        "context_pack": build_context_pack(
            claims, concept_overview=pack_overview, max_claims=max_items,
            _concept_rows=full_concept_rows, _research_source=research_source,
            _claim_source=claim_source),
    }
    return sanitize_cross_run_projection(
        payload, max_chars=128_000_000, max_items=128, max_total_items=100_000)


def _safe_text(s, limit: int = 120) -> str:
    """Sanitize UNTRUSTED memory text (claim statements / concept slugs — LLM/repo-derived) before it enters
    an agent prompt: strip control chars + collapse newlines/whitespace to a single space, then bound the
    length. Prevents newline/control-char prompt-injection through the cross-run advisory pack (mega-review)."""
    return _claim_text(s, limit)


def render_context_pack(pack: dict) -> str:
    """Render a context pack as a compact, bounded text block for a proposing agent (the advisory form).
    Deterministic; retains mixed evidence so the agent sees counter-arguments, not only positives.
    All memory-derived text is sanitized (control chars/newlines stripped) — quoted DATA, not instructions
    (mega-review prompt-injection hardening)."""
    if (not pack.get("claims") and not pack.get("coverage")
            and not pack.get("research_source") and not pack.get("claim_source")):
        return ""
    _mark = {"supported": "✓", "refuted": "✗", "mixed": "⚖", "inconclusive": "·"}
    lines = [f"Cross-run evidence ({pack.get('n_claims_total', 0)} claim records, "
             f"{pack.get('n_contested', 0)} mixed-evidence) — bounded observations, with counter-evidence:"]
    if pack.get("n_pinned_omitted", 0):
        lines.append(
            f"  WARNING: {int(pack['n_pinned_omitted'])} operator-pinned claim(s) omitted by the "
            "hard context limit; consult the full claims ledger.")
    research_source = _safe_research_source_summary(pack.get("research_source"))
    if research_source is not None and research_source["source_complete"] is not True:
        lines.append(
            "  WARNING: D8 research-claim source is PARTIAL/UNKNOWN "
            f"({research_source['producer_partial_runs']} capped run(s); "
            f"{research_source['producer_claims_omitted']} claim(s) known omitted"
            + (f"; {research_source['producer_unknown_runs']} legacy/malformed run receipt(s)"
               if research_source["producer_unknown_runs"] else "")
            + "); retained evidence is a lower bound and exact one-sided states are withheld.")
    claim_source = _safe_claim_source_summary(pack.get("claim_source"))
    if claim_source is None and "claim_source" in pack:
        lines.append(
            "  WARNING: claim evidence source receipt is malformed/unknown; exact one-sided states and "
            "absence are withheld.")
    elif claim_source is not None and claim_source["read_complete"] is not True:
        lessons_bad = claim_source["lessons"]["rows_quarantined"]
        research_bad = claim_source["research"]["rows_quarantined"]
        lines.append(
            "  WARNING: claim evidence stores are PARTIAL "
            f"(lessons quarantined={lessons_bad}; research quarantined={research_bad}); "
            "retained evidence is a lower bound and absence is not exact.")
    for c in pack.get("claims", []):
        statement = _safe_text(c.get("statement"), 120)
        contradicts = "; ".join(
            repr(_safe_text(value, 160))
            for value in (c.get("contradicts") or [])[:3])
        maturity = str(c.get("maturity") or "machine-proposed")
        policy = ""
        if maturity in {"operator-ratified", "operator-pinned"}:
            freshness = {True: "current", False: "stale-evidence", None: "unknown"}.get(
                c.get("decision_fresh"), "unknown")
            # operator policy persists until clear, but its evidence fence can age.
            # Surface both axes so retention priority never masquerades as a fresh ratification.
            policy = (f"; operator_policy={maturity.removeprefix('operator-')}; "
                      f"decision_freshness={freshness}")
        lines.append(f"  {_mark.get(c['epistemic'], '?')} [{c['n_support']}↑/{c['n_oppose']}↓] "
                     f"UNTRUSTED_MEMORY={statement!r}"
                     + policy + (f"; contradicts={contradicts}" if contradicts else ""))
    cov = pack.get("coverage")
    if cov:
        if cov.get("source_complete") is not True:
            lines.append(
                "  WARNING: concept capsule source is PARTIAL "
                f"({int(cov.get('partial_capsules', 0))} capsule(s); "
                f"{int(cov.get('source_concepts_omitted', 0))} concept(s) and "
                f"{int(cov.get('source_outcomes_omitted', 0))} outcome(s) known omitted"
                + (f"; {int(cov.get('source_unknown_capsules', 0))} legacy capsule(s) have unknown totals"
                   if cov.get("source_unknown_capsules", 0) else "")
                + (f"; {int(cov.get('source_rows_quarantined', 0))} durable row(s) were quarantined"
                   if cov.get("source_rows_quarantined", 0) else "")
                + "); "
                "coverage describes returned observations only; directional tendencies are withheld.")
        top = ", ".join(repr(_safe_text(x, 100))
                        for x in cov.get("top_concepts", [])[:6])
        lines.append(f"Bounded live concept observations (not coverage): {cov.get('n_runs', 0)} returned "
                     f"run(s), {cov.get('n_concepts', 0)} concept(s)"
                     f"{'; UNTRUSTED_MEMORY_CONCEPTS=' + top if top else ''}.")
        # Phase 1 profit signal: a direction-normalized RANK tendency across similar runs — which concepts
        # tended to land in the better vs worse half of their run's own field. ADVISORY — a prior rank
        # tendency, never causal proof, never a rule, and never a selection input; weigh but do not obey.
        helps = ", ".join(repr(_safe_text(x, 100)) for x in (cov.get("helps") or [])[:6])
        hurts = ", ".join(repr(_safe_text(x, 100)) for x in (cov.get("hurts") or [])[:6])
        if helps or hurts:
            # concept slugs are persisted, LLM-originated data. Keep the explicit trust
            # marker on rank tendencies just as on the coverage line and the sibling cross-run tool;
            # repr quoting alone does not tell a proposing model that the span is inert memory.
            parts = ([f"tended to RANK BETTER UNTRUSTED_MEMORY={helps}"] if helps else []) + (
                [f"tended to RANK WORSE UNTRUSTED_MEMORY={hurts}"] if hurts else [])
            lines.append("Cross-run concept rank tendency (better/worse half of each run vs its sibling "
                         "concepts; advisory, NOT a rule — consider toward the first, scrutinize the "
                         "second): " + "; ".join(parts) + ".")
    return "\n".join(lines)
