"""The claim-assessment PROJECTIONS — lessons plus research claims into one verdict view.

Split out of the `claims.py` god-module (doc 25 EM-01). This is the layer that turns two independent
durable stores — lesson outcomes and D8 research claims — into the epistemic view everything
downstream reads: `supported`, `refuted`, `mixed`, `inconclusive`, with the run and node ids behind
each. `contested` is only reachable because research claims can oppose a lesson verdict, which is
the whole reason the two stores are folded together here rather than read separately.

It sits between the leaf and the retrieval planner: it reads `claims_health`'s validators and
bounds, and `claims_retrieval` reads it (through the `claims.py` barrel) to build a context pack.
The store and governance-ledger half is BELOW it in file order but ABOVE it in the barrel, so the
two ledger key helpers it needs come in through a deferred import — `claims.py` imports this module
to re-export it, and a module-level import back would cycle at startup.

`claims.py` re-exports every name here, so both spellings resolve to the SAME objects and existing
imports and monkeypatch seams are unaffected.
"""
from __future__ import annotations

import math
from typing import Optional

from looplab.engine.claims_health import (
    _ClaimAssessmentRows,
    _MAX_DECISION_METRIC,
    _MAX_DECISION_SCOPE,
    _bounded_claim_projection,
    _claim_source_summary,
    _claim_text,
    _identity_text,
    _indexable_research_claim,
    _lesson_claim_stance,
    _metric_identity,
    _node_ids,
    _qualify_refs,
    _research_source_summary,
    _research_verification,
    safe_claim_source_summary,
    safe_research_source_summary,
    _source_guarded_epistemic,
    _string_list,
    _valid_claim_source_rows,
    claim_evidence_digest,
    normalize_statement,
    sanitize_cross_run_projection,
)

def _register_incarnation(group: dict, row: dict) -> None:
    """`runs` keeps directory NAMES for display; `run_refs` keeps INCARNATIONS for counting (doc 50
    EK-03; doc 52 row 4). A row with `run_uid` registers that uid, a row without one registers
    `legacy:<name>` — `core/run_identity.py::run_ref`, imported at call time because the claims
    barrel derives its re-export surface from `dir()` (see `claims_health._research_source_summary`).
    The atlas counts runs over this set, so two incarnations of `demo` are two runs there and a
    lesson-only memory is still not reported as zero."""
    from looplab.core.run_identity import run_ref
    ref = run_ref(_identity_text(row.get("run_uid"), 500), _identity_text(row.get("run_id"), 500))
    if ref:
        group.setdefault("run_refs", set()).add(ref)


def _ingest_evidence(lessons, research_claims, resolve, *, weigh=None) -> None:
    """Fold lesson + research rows into their claim groups (doc 25 EM-07).

    The structured (scope+polarity) and lean (normalized-statement) projections differed ONLY in how a
    row finds its group — `resolve(row)` — and in the structured path's evidence weighting, passed as
    `weigh(group, row, refs)`. Everything else was duplicated verbatim. The lean projection is deleted
    (doc 25 EM-06, 2026-09-08) and the structured one is the last caller, but the parametrized seam
    stays: this is the part where a quiet mistake is unrecoverable, and it is drivable on its own
    (`tests/test_evidence_ingestion.py`) precisely because the walk is not inlined into a projection:

    * **Run/scope registration happens even for a NEUTRAL lesson.** A "noted" lesson takes no stance
      but still proves the claim was seen in that run and scope. Skipping it because the stance is
      neutral would silently shrink the run set a reader uses to judge breadth.
    * **Unsupported research is `unverified`, never `oppose`.** "Not established" is not
      counter-evidence, and merging the two would let an uncited claim read as refuted.
    * **The refs stay drillable either way** — an unverified claim keeps its node references so a
      reader can go look, which is the whole reason the third bucket exists.

    A stance-mapping or receipt bug in one copy would have had to be found and fixed in the other.
    """
    for lesson in lessons or []:
        group = resolve(lesson)
        if group is None:
            continue
        if lesson.get("run_id"):
            group["runs"].add(_identity_text(lesson["run_id"], 500))
        _register_incarnation(group, lesson)
        if lesson.get("task_id"):
            group["scopes"].add(_identity_text(lesson["task_id"], _MAX_DECISION_SCOPE))
        refs = _qualify_refs(lesson, _node_ids(lesson.get("evidence")))
        stance = _lesson_claim_stance(lesson)
        if stance == "support":
            group["support"].update(refs)
        elif stance == "oppose":
            group["oppose"].update(refs)
        # "noted"/unknown -> neutral: still registers the run/scope, but takes NO stance.
        if weigh is not None:
            weigh(group, lesson, refs)

    for claim in research_claims or []:
        if not _indexable_research_claim(claim):
            continue
        group = resolve(claim)
        if group is None:
            continue
        if claim.get("run_id"):
            group["runs"].add(_identity_text(claim["run_id"], 500))  # D8 registers run/scope now
        _register_incarnation(group, claim)
        if claim.get("task_id"):
            group["scopes"].add(_identity_text(claim["task_id"], _MAX_DECISION_SCOPE))
        refs = _qualify_refs(claim, _node_ids(claim.get("node_ids")))
        verdict, method, _note = _research_verification(claim)
        group["verification"].add(f"{method}:{verdict}" if method else verdict)
        if verdict == "supported":
            group["support"].update(refs)
        else:
            # unsupported/unclear/cited/legacy-unverified evidence is not counter-evidence; it simply
            # has not established the claim. Keep the refs drillable without promoting them to support.
            group["unverified"].update(refs)
        if weigh is not None:
            weigh(group, claim, refs)
        group["sources"].update(_string_list(claim.get("urls"), maximum=32, item_maximum=2000))


def _structured_assessments(lessons, research_claims, decisions, *,
                            research_source: Optional[dict] = None,
                            claim_source: Optional[dict] = None) -> list[dict]:
    """The SCOPE+POLARITY-safe structured projection, THE DEFAULT (doc 25 EM-06). Identity is the
    `claim_signature` merge_key: (subject stems, scope=task, metric, polarity). Opposite-polarity claims
    sharing a `contra_key` are surfaced as a CONTRADICTION (they never merge, and each is marked contested).
    Governance overlays by the structured `claim_uid` (scope-precise)."""
    from looplab.engine.claim_key import claim_signature, claim_uid
    lessons = _valid_claim_source_rows(lessons, research=False)
    research_claims = _valid_claim_source_rows(research_claims, research=True)
    research_source = (safe_research_source_summary(research_source)
                       if research_source is not None else _research_source_summary(research_claims))
    if research_source is None:
        research_source = _research_source_summary(research_claims)
    claim_source = (safe_claim_source_summary(claim_source)
                    if claim_source is not None else _claim_source_summary(
                        lessons, research_claims, research_source=research_source))
    if claim_source is None:
        claim_source = _claim_source_summary(
            lessons, research_claims, research_source=research_source)
    decisions = decisions if isinstance(decisions, dict) else {}
    groups: dict[str, dict] = {}

    def _grp(statement, scope, metric=""):
        s = _claim_text(statement)
        if not s:
            return None
        sig = claim_signature(
            s, scope=_identity_text(scope, _MAX_DECISION_SCOPE),
            metric=_identity_text(metric, _MAX_DECISION_METRIC))
        if sig["polarity"] == 0:                     # no subject content -> not a claim
            return None
        g = groups.get(sig["merge_key"])
        if g is None:
            g = groups[sig["merge_key"]] = {
                "uid": sig["uid"], "contra_key": sig["contra_key"], "polarity": sig["polarity"],
                "scope": sig["scope"], "metric": sig["metric"],
                "support": set(), "oppose": set(), "unverified": set(),
                "runs": set(), "run_refs": set(), "scopes": set(), "sources": set(),
                "verification": set(), "_ev": {}}
        g["_ev"][s] = g["_ev"].get(s, 0)             # candidate representative statements (evidence-weighted)
        return g

    def _weigh(group, row, refs):
        # Evidence-weighted representative choice: the spelling backed by the most drillable refs wins
        # the group's display statement. Only the structured projection does this — the lean one keys
        # on the normalized statement, so it has no competing spellings to choose between.
        group["_ev"][_claim_text(row.get("statement"))] += len(refs)

    _ingest_evidence(
        lessons, research_claims,
        lambda row: _grp(row.get("statement"), row.get("task_id"), _metric_identity(row)),
        weigh=_weigh)

    # Contradiction map: a contra_key seen with BOTH polarities means two opposite claims about one subject
    # in one scope — the portfolio disagrees with itself at the ASSERTION level (unreachable from a single
    # merged statement). Each such claim is marked contested and carries its opposites' representative text.
    _dec = {"ratified": "operator-ratified", "rejected": "operator-rejected", "pinned": "operator-pinned"}

    def _decision_for(g: dict, rep: str):
        """The decision governing this group AND how it was found: `(decision, resolved_via)`.

        `resolved_via` is the read-time migration receipt (doc 25 EM-06, 2026-09-08). `claim_uid`
        means a structured candidate matched — the identity the write path validates against.
        `legacy_statement_key` / `unscoped_global_key` mean the decision was only reachable through
        the PRE-STRUCTURED normalized-statement namespace, i.e. an old row whose spelling happens to
        normalize onto this group. That was invisible before: an operator saw the same maturity
        overlay either way and could not tell a scope-precise verdict from a statement collision,
        which is precisely the ambiguity the deleted lean projection institutionalized. REPORTED,
        never acted on — the fallback chain itself is unchanged.
        """
        # DEFERRED: `claims.py` imports THIS module to re-export it, so importing the ledger
        # half back at module scope would cycle. This spells the legacy overlay key the
        # governance loader writes (doc 25 EM-01).
        from looplab.engine.claims import _global_key
        overlay = decisions
        candidates = [g["uid"], claim_uid(rep, scope=g["scope"], metric=g["metric"])]
        if g["metric"]:
            candidates.append(claim_uid(rep, scope=g["scope"], metric=""))
        if g["metric"]:
            candidates.append(claim_uid(rep, scope="", metric=g["metric"]))
        candidates.append(claim_uid(rep, scope="", metric=""))
        seen = set()
        for uid in candidates:
            if uid and uid not in seen and isinstance(overlay.get(uid), dict):
                return overlay[uid], "claim_uid"
            seen.add(uid)
        legacy_key = normalize_statement(rep)
        legacy = overlay.get(legacy_key)
        if (isinstance(legacy, dict) and not str(legacy.get("scope") or "")
                and not str(legacy.get("metric") or "")):
            return legacy, "legacy_statement_key"
        global_legacy = overlay.get(_global_key(legacy_key))
        if (isinstance(global_legacy, dict) and not str(global_legacy.get("scope") or "")
                and not str(global_legacy.get("metric") or "")):
            return global_legacy, "unscoped_global_key"
        return None, ""

    prepared = []
    for g in groups.values():
        rep = max(g["_ev"], key=lambda s: (g["_ev"][s], s)) if g["_ev"] else ""
        sup, opp, unverified = sorted(g["support"]), sorted(g["oppose"]), sorted(g["unverified"])
        decision, resolved_via = _decision_for(g, rep)
        if decision is not None:
            decision = sanitize_cross_run_projection(
                decision, max_chars=16_000, max_items=64, max_total_items=256)
            # Stamped AFTER sanitizing: this is the PROJECTION's receipt about its own lookup, not
            # persisted operator text, so it must not be redacted or bounded away as if it were.
            decision = {**decision, "resolved_via": resolved_via}
        prepared.append({"group": g, "statement": rep, "support": sup, "oppose": opp,
                         "unverified": unverified, "decision": decision,
                         "maturity": _dec.get((decision or {}).get("decision"), "machine-proposed")})

    # Keep a governance-independent contradiction map for the evidence digest. The live projection below
    # may hide a rejected opposite, but rejecting it must not make the reviewed proof revision change by
    # itself; only source evidence should age a decision.
    raw_contra: dict[str, dict[int, list]] = {}
    contra: dict[str, dict[int, list]] = {}
    for item in prepared:
        # Bound once per item: both maps key off the SAME group, and leaking `g` out of the first
        # branch would silently carry the previous item's group the moment either condition is
        # relaxed independently of the other.
        g = item["group"]
        if item["support"]:
            raw_contra.setdefault(g["contra_key"], {}).setdefault(g["polarity"], []).append(item)
        if item["maturity"] != "operator-rejected" and item["support"]:
            contra.setdefault(g["contra_key"], {}).setdefault(g["polarity"], []).append(item)

    out = []
    for item in prepared:
        g, rep = item["group"], item["statement"]
        sup, opp, unverified = item["support"], item["oppose"], item["unverified"]
        opposites = ([] if item["maturity"] == "operator-rejected" else
                     [og for pol, gs in contra.get(g["contra_key"], {}).items() if pol != g["polarity"]
                      for og in gs])
        contradicts = sorted({o["statement"] for o in opposites})
        raw_opposites = [og for pol, gs in raw_contra.get(g["contra_key"], {}).items()
                         if pol != g["polarity"] for og in gs]
        raw_contradicts = sorted({o["statement"] for o in raw_opposites})
        row = {
            "statement": rep,
            # a polarity contradiction is the strongest contested signal -> mixed even if this side's own
            # evidence is one-directional (that is exactly what the structured key makes reachable).
            "epistemic": ("mixed" if contradicts and sup
                           else _source_guarded_epistemic(sup, opp, claim_source)),
            "maturity": item["maturity"],
            "support": sup, "oppose": opp, "n_support": len(sup), "n_oppose": len(opp),
            "unverified": unverified, "n_unverified": len(unverified),
            "runs": sorted(g["runs"]), "run_refs": sorted(g.get("run_refs", ())),
            "scopes": sorted(g["scopes"]), "sources": sorted(g["sources"]),
            "verification": sorted(g["verification"]),
            "claim_uid": g["uid"], "scope": g["scope"], "polarity": g["polarity"],
            "metric": g["metric"],
            "decision": item["decision"], "contradicts": contradicts,
            "research_source": research_source,
            "claim_source": claim_source,
        }
        digest_row = {**row,
                      "epistemic": ("mixed" if raw_contradicts and sup
                                     else _source_guarded_epistemic(sup, opp, claim_source)),
                      "contradicts": raw_contradicts}
        row["evidence_digest"] = claim_evidence_digest(digest_row)
        decision_digest = str((item["decision"] or {}).get("evidence_digest") or "")
        row["decision_fresh"] = (decision_digest == row["evidence_digest"] if decision_digest else None)
        out.append(row)
    out.sort(key=lambda c: (-(c["n_support"] + c["n_oppose"]), -c["n_oppose"],
                            0 if c["contradicts"] else 1, c["statement"]))
    return out


def claim_assessments(lessons: list[dict], *, research_claims: Optional[list[dict]] = None,
                      decisions: Optional[dict] = None,
                      structured: bool = True, bounded: bool = True) -> list[dict]:
    """Project distilled `lessons` (+ optional D8 `research_claims`) into evidence-grounded claim
    assessments. Each claim carries `support`/`oppose` node-id evidence, contributing `runs`/`scopes`,
    and an `epistemic` state. `decisions` (from `load_claim_decisions`) overlays an operator `maturity`
    (`operator-ratified`/`operator-rejected`/`operator-pinned`, else `machine-proposed`) — the §22.4
    governance overlay. Sorted most-evidenced first. Pure.

    `structured` is a RETIRED keyword (doc 25 EM-06, 2026-09-08). There is ONE claim identity — the
    SCOPE+POLARITY-safe structured claim key (`claim_key.claim_signature`): claims from different tasks
    never merge, opposite polarity ("X helps" vs "X never helps") is a CONTRADICTION not a merge, and
    paraphrase/inflection variants collapse by exact structured key (O(n), no transitive over-merge).
    Both values of the keyword project it."""
    # THE ONE IDENTITY, and the overlay key its governance decisions arrive under — the table doc 25
    # EM-06 asked for, now that there is no longer anything to select between:
    #
    #   identity                                overlay key
    #   ------------------------------------    -------------------------------------------------
    #   claim_key.claim_signature (scope +      structured `claim_uid`, then the pre-structured
    #   polarity safe)                          normalized-statement keys as an explicitly UNSCOPED
    #                                           fallback that REPORTS itself (`_decision_for`)
    #
    # THE LEAN READ PATH (`structured=False`) IS DELETED, and `_scoped_key` — the only namespace it
    # was the last reader of — with it. No durable decision became unreachable, and that is a
    # property of the ledger READER rather than a hope: `claims.py::_validate_claim_decision_row`
    # calls a missing/empty/oversized/sanitizes-to-empty `statement` `invalid_record`, and
    # `read_governance_rows` RAISES on that instead of projecting the readable subset — so every row
    # `load_claim_decisions` can see carries a statement, hence a structured `claim_uid`, hence an
    # index at it. `_scoped_key` was always a SECOND index on a row that already had its UID; it was
    # never any row's only key. What a caller still projecting lean loses is the CROSS-TASK MERGE,
    # which is the finding's own complaint arriving as a behaviour change — and an operator reviewing
    # under it could never be decided on anyway: a lean row carried no `claim_uid` and no
    # `evidence_digest`, and `record_claim_decision` validates against the structured projection.
    #
    # WHY THE KEYWORD SURVIVES THE PATH IT SELECTED. `fuzzy=` was deleted outright, because a
    # silently-accepted `fuzzy=True` would have read as "paraphrases still merge". `structured=`
    # cannot follow it yet: `EngineOptions.cross_run_structured_claims` reaches
    # `claim_context_pack` through `engine/proposal_cues.py` and `engine/strategy.py`, and
    # `LEGACY_CONFIG_SNAPSHOT_DEFAULTS` pins that field False — so a RESUMED pre-field run still
    # passes `structured=False`, and refusing it would abort that run over a read-model preference.
    # Accepting it is safe only because both values now name the SAME projection. Removing the
    # keyword is what is left of EM-06, and it is a Settings-field retirement (config, options, the
    # settings catalogue, `serve/routers/cross_run.py`, `tools/cross_run_tools.py`), not a claims
    # change.
    lessons = _valid_claim_source_rows(lessons, research=False)
    research_claims = _valid_claim_source_rows(research_claims, research=True)
    research_source = _research_source_summary(research_claims)
    claim_source = _claim_source_summary(
        lessons, research_claims, research_source=research_source)
    decisions = decisions if isinstance(decisions, dict) else {}
    rows = _structured_assessments(
        lessons, research_claims, decisions,
        research_source=research_source, claim_source=claim_source)
    projected = [_bounded_claim_projection(row) for row in rows] if bounded else rows
    return _ClaimAssessmentRows(
        projected, claim_source=claim_source, research_source=research_source)
