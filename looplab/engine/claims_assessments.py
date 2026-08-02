"""The three claim-assessment PROJECTIONS — lessons plus research claims into one verdict view.

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
    _CLAIM_WORD,
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
    _safe_claim_source_summary,
    _safe_research_source_summary,
    _source_guarded_epistemic,
    _string_list,
    _valid_claim_source_rows,
    claim_evidence_digest,
    normalize_statement,
    sanitize_cross_run_projection,
)

def _stmt_tokens(s: str) -> frozenset:
    return frozenset(w for w in _CLAIM_WORD.findall((s or "").casefold()) if len(w) > 2)


def _fuzzy_merge_claims(claims: list[dict], *, threshold: float = 0.6) -> list[dict]:
    """Conservative opt-in paraphrase projection.

    Candidates must share scope, semantic polarity and governance maturity, and every member must clear the
    threshold (complete-link). A bounded token index avoids all-pairs and single-link bridge collapse.
    """
    n = len(claims)
    if n <= 1:
        return claims
    from looplab.engine.claim_key import claim_signature
    toks = [_stmt_tokens(c["statement"]) for c in claims]
    meta = [(tuple(c.get("scopes") or []), claim_signature(c["statement"])["polarity"],
             str(c.get("maturity") or "machine-proposed")) for c in claims]
    groups: list[list[int]] = []
    token_groups: dict[str, set[int]] = {}
    for i, token_set in enumerate(toks):
        candidates = sorted({gid for token in token_set for gid in token_groups.get(token, ())})[:64]
        chosen = None
        for gid in candidates:
            members = groups[gid]
            if len(members) >= 64 or any(meta[j] != meta[i] for j in members):
                continue
            complete = True
            for j in members:
                union, inter = token_set | toks[j], token_set & toks[j]
                if not inter or len(inter) / len(union) < threshold:
                    complete = False
                    break
            if complete:
                chosen = gid
                break
        if chosen is None:
            chosen = len(groups)
            groups.append([])
        groups[chosen].append(i)
        for token in token_set:
            token_groups.setdefault(token, set()).add(chosen)

    out = []
    for idxs in groups:
        members = [claims[i] for i in idxs]
        if len(members) == 1:
            out.append(members[0])
            continue
        sup = sorted({r for m in members for r in m["support"]})
        opp = sorted({r for m in members for r in m["oppose"]})
        unverified = sorted({r for m in members for r in m.get("unverified", [])})
        rep = max(members, key=lambda m: (m["n_support"] + m["n_oppose"], m["statement"]))
        mat = members[0].get("maturity", "machine-proposed")
        research_source = (_safe_research_source_summary(members[0].get("research_source"))
                           or _research_source_summary([]))
        claim_source = (_safe_claim_source_summary(members[0].get("claim_source"))
                        or _claim_source_summary([], [], research_source=research_source))
        out.append({
            "statement": rep["statement"],
            "epistemic": _source_guarded_epistemic(sup, opp, claim_source), "maturity": mat,
            "support": sup, "oppose": opp, "n_support": len(sup), "n_oppose": len(opp),
            "unverified": unverified, "n_unverified": len(unverified),
            "runs": sorted({r for m in members for r in m["runs"]}),
            "scopes": sorted({r for m in members for r in m["scopes"]}),
            "sources": sorted({s for m in members for s in m.get("sources", [])}),
            "verification": sorted({v for m in members for v in m.get("verification", [])}),
            "decision": members[0].get("decision"),
            "merged_from": sorted(m["statement"] for m in members),
            "research_source": research_source,
            "claim_source": claim_source,
        })
    out.sort(key=lambda c: (-(c["n_support"] + c["n_oppose"]), -c["n_oppose"], c["statement"]))
    return out


def _structured_assessments(lessons, research_claims, decisions, *,
                            research_source: Optional[dict] = None,
                            claim_source: Optional[dict] = None) -> list[dict]:
    """The SCOPE+POLARITY-safe structured projection (full CR of the lean fuzzy merge). Identity is the
    `claim_signature` merge_key: (subject stems, scope=task, metric, polarity). Opposite-polarity claims
    sharing a `contra_key` are surfaced as a CONTRADICTION (they never merge, and each is marked contested).
    Governance overlays by the structured `claim_uid` (scope-precise)."""
    from looplab.engine.claim_key import claim_signature, claim_uid
    lessons = _valid_claim_source_rows(lessons, research=False)
    research_claims = _valid_claim_source_rows(research_claims, research=True)
    research_source = (_safe_research_source_summary(research_source)
                       if research_source is not None else _research_source_summary(research_claims))
    if research_source is None:
        research_source = _research_source_summary(research_claims)
    claim_source = (_safe_claim_source_summary(claim_source)
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
                "runs": set(), "scopes": set(), "sources": set(), "verification": set(), "_ev": {}}
        g["_ev"][s] = g["_ev"].get(s, 0)             # candidate representative statements (evidence-weighted)
        return g

    for lz in lessons or []:
        g = _grp(lz.get("statement"), lz.get("task_id"), _metric_identity(lz))
        if g is None:
            continue
        if lz.get("run_id"):
            g["runs"].add(_identity_text(lz["run_id"], 500))
        if lz.get("task_id"):
            g["scopes"].add(_identity_text(lz["task_id"], _MAX_DECISION_SCOPE))
        refs = _qualify_refs(lz.get("run_id"), _node_ids(lz.get("evidence")))
        stance = _lesson_claim_stance(lz)
        if stance == "support":
            g["support"].update(refs)
        elif stance == "oppose":
            g["oppose"].update(refs)
        g["_ev"][_claim_text(lz.get("statement"))] += len(refs)

    for rc in research_claims or []:
        if not _indexable_research_claim(rc):
            continue
        g = _grp(rc.get("statement"), rc.get("task_id"), _metric_identity(rc))
        if g is None:
            continue
        if rc.get("run_id"):
            g["runs"].add(_identity_text(rc["run_id"], 500))  # D8 registers run/scope now
        if rc.get("task_id"):
            g["scopes"].add(_identity_text(rc["task_id"], _MAX_DECISION_SCOPE))
        refs = _qualify_refs(rc.get("run_id"), _node_ids(rc.get("node_ids")))
        verdict, method, _note = _research_verification(rc)
        g["verification"].add(f"{method}:{verdict}" if method else verdict)
        if verdict == "supported":
            g["support"].update(refs)
        else:
            # unsupported/unclear/cited/legacy-unverified evidence is not counter-evidence; it simply has
            # not established the claim.  Keep the refs drillable without promoting them to support.
            g["unverified"].update(refs)
        g["_ev"][_claim_text(rc.get("statement"))] += len(refs)
        g["sources"].update(_string_list(rc.get("urls"), maximum=32, item_maximum=2000))

    # Contradiction map: a contra_key seen with BOTH polarities means two opposite claims about one subject
    # in one scope — the portfolio disagrees with itself at the ASSERTION level (unreachable from a single
    # merged statement). Each such claim is marked contested and carries its opposites' representative text.
    _dec = {"ratified": "operator-ratified", "rejected": "operator-rejected", "pinned": "operator-pinned"}

    def _decision_for(g: dict, rep: str):
        # DEFERRED: `claims.py` imports THIS module to re-export it, so importing the ledger
        # half back at module scope would cycle. These two spell the legacy overlay keys the
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
                return overlay[uid]
            seen.add(uid)
        legacy_key = normalize_statement(rep)
        legacy = overlay.get(legacy_key)
        if (isinstance(legacy, dict) and not str(legacy.get("scope") or "")
                and not str(legacy.get("metric") or "")):
            return legacy
        global_legacy = overlay.get(_global_key(legacy_key))
        if (isinstance(global_legacy, dict) and not str(global_legacy.get("scope") or "")
                and not str(global_legacy.get("metric") or "")):
            return global_legacy
        return None

    prepared = []
    for g in groups.values():
        rep = max(g["_ev"], key=lambda s: (g["_ev"][s], s)) if g["_ev"] else ""
        sup, opp, unverified = sorted(g["support"]), sorted(g["oppose"]), sorted(g["unverified"])
        decision = _decision_for(g, rep)
        if decision is not None:
            decision = sanitize_cross_run_projection(
                decision, max_chars=16_000, max_items=64, max_total_items=256)
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
            "runs": sorted(g["runs"]), "scopes": sorted(g["scopes"]), "sources": sorted(g["sources"]),
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
                      decisions: Optional[dict] = None, fuzzy: bool = False,
                      structured: bool = False, bounded: bool = True) -> list[dict]:
    """Project distilled `lessons` (+ optional D8 `research_claims`) into evidence-grounded claim
    assessments. Groups by normalized statement; each claim carries `support`/`oppose` node-id evidence,
    contributing `runs`/`scopes`, and an `epistemic` state. `decisions` (from `load_claim_decisions`)
    overlays an operator `maturity` (`operator-ratified`/`operator-rejected`/`operator-pinned`, else
    `machine-proposed`) — the §22.4 governance overlay. Sorted most-evidenced first. Pure.

    `structured` (opt-in, the full CR of the lean `fuzzy` merge) switches identity to the SCOPE+POLARITY-safe
    structured claim key (`claim_key.claim_signature`): claims from different tasks never merge, opposite
    polarity ("X helps" vs "X never helps") is a CONTRADICTION not a merge, and paraphrase/inflection
    variants collapse by exact structured key (O(n), no transitive over-merge). Mutually exclusive with the
    lean `fuzzy` path (structured wins)."""
    # DEFERRED for the same reason as `_decision_for` above: `claims.py` imports this module to
    # re-export it, so the ledger half's legacy overlay-key spellings come in per call (EM-01).
    from looplab.engine.claims import _global_key, _scoped_key
    lessons = _valid_claim_source_rows(lessons, research=False)
    research_claims = _valid_claim_source_rows(research_claims, research=True)
    research_source = _research_source_summary(research_claims)
    claim_source = _claim_source_summary(
        lessons, research_claims, research_source=research_source)
    decisions = decisions if isinstance(decisions, dict) else {}
    if structured:
        rows = _structured_assessments(
            lessons, research_claims, decisions,
            research_source=research_source, claim_source=claim_source)
        projected = [_bounded_claim_projection(row) for row in rows] if bounded else rows
        return _ClaimAssessmentRows(
            projected, claim_source=claim_source, research_source=research_source)
    groups: dict[str, dict] = {}

    def _group(stmt: str) -> Optional[dict]:
        s = _claim_text(stmt)
        if not s:
            return None
        # NOTE: identity here is the normalized STATEMENT (the shipped lesson `normalize_statement`
        # key) — it can merge same-worded claims across incompatible scopes and the 160-char cap can
        # collide. A structured semantic claim key (subject/intervention/comparator/scope) is the CR1b TODO
        # (§21.20.13); this lean projection keeps scope/runs as metadata on the claim.
        return groups.setdefault(normalize_statement(s), {
            "statement": s, "support": set(), "oppose": set(),
            "unverified": set(), "runs": set(), "scopes": set(), "sources": set(),
            "verification": set()})

    for lz in lessons or []:
        g = _group(lz.get("statement"))
        if g is None:
            continue
        if lz.get("run_id"):
            g["runs"].add(_identity_text(lz["run_id"], 500))
        if lz.get("task_id"):
            g["scopes"].add(_identity_text(lz["task_id"], _MAX_DECISION_SCOPE))
        refs = _qualify_refs(lz.get("run_id"), _node_ids(lz.get("evidence")))
        stance = _lesson_claim_stance(lz)
        if stance == "support":
            g["support"].update(refs)
        elif stance == "oppose":
            g["oppose"].update(refs)
        # "noted"/unknown -> neutral: still registers the run/scope, but takes NO stance.

    for rc in research_claims or []:
        if not _indexable_research_claim(rc):
            continue
        g = _group(rc.get("statement"))
        if g is None:
            continue
        if rc.get("run_id"):
            g["runs"].add(_identity_text(rc["run_id"], 500))
        if rc.get("task_id"):
            g["scopes"].add(_identity_text(rc["task_id"], _MAX_DECISION_SCOPE))
        refs = _qualify_refs(rc.get("run_id"), _node_ids(rc.get("node_ids")))
        verdict, method, _note = _research_verification(rc)
        g["verification"].add(f"{method}:{verdict}" if method else verdict)
        if verdict == "supported":
            g["support"].update(refs)
        else:
            g["unverified"].update(refs)
        g["sources"].update(_string_list(rc.get("urls"), maximum=32, item_maximum=2000))

    _dec = {"ratified": "operator-ratified", "rejected": "operator-rejected", "pinned": "operator-pinned"}
    out = []
    for key, g in groups.items():
        sup, opp, unverified = sorted(g["support"]), sorted(g["oppose"]), sorted(g["unverified"])
        overlay = decisions
        real_scopes = {str(scope) for scope in g["scopes"] if str(scope)}
        # A statement row spanning multiple tasks cannot safely receive any one task's policy.  For a
        # task-bound row, however, the exact scope-only decision outranks the portfolio-wide fallback.
        d = None
        if len(real_scopes) == 1:
            from looplab.engine.claim_key import claim_uid
            scope = next(iter(real_scopes))
            d = overlay.get(claim_uid(g["statement"], scope=scope, metric=""))
            # Compatibility for a custom lean overlay keyed by normalized statement+scope.
            if d is None:
                d = overlay.get(_scoped_key(key, scope))
        if d is None:
            d = overlay.get(key)
        # The lean projection groups by statement across tasks. A caller-supplied scoped decision may
        # therefore govern this row only when all contributing task scopes are that exact scope; unscoped
        # decisions remain the portfolio-wide fallback. The durable loader normally indexes scoped records
        # by structured UID only, but this guard also keeps custom/preloaded overlays fail-closed.
        if not isinstance(d, dict):
            d = None
        if d is not None:
            _dscope = str(d.get("scope") or "")
            if _dscope:
                if not real_scopes or not real_scopes <= {_dscope}:
                    d = None
        if d is None:
            d = overlay.get(_global_key(key))
        if not isinstance(d, dict):
            d = None
        if d is not None:
            d = sanitize_cross_run_projection(
                d, max_chars=16_000, max_items=64, max_total_items=256)
        out.append({
            "statement": g["statement"],
            "epistemic": _source_guarded_epistemic(sup, opp, claim_source),
            "maturity": _dec.get((d or {}).get("decision"), "machine-proposed"),
            "support": sup, "oppose": opp,
            "n_support": len(sup), "n_oppose": len(opp),
            "unverified": unverified, "n_unverified": len(unverified),
            "runs": sorted(g["runs"]), "scopes": sorted(g["scopes"]),
            "sources": sorted(g["sources"]), "verification": sorted(g["verification"]),
            "decision": d,
            "research_source": research_source,
            "claim_source": claim_source,
        })
    # most-evidenced first (support+oppose), contested claims break ties toward visibility, then statement
    out.sort(key=lambda c: (-(c["n_support"] + c["n_oppose"]), -c["n_oppose"], c["statement"]))
    rows = _fuzzy_merge_claims(out) if fuzzy else out
    projected = [_bounded_claim_projection(row) for row in rows] if bounded else rows
    return _ClaimAssessmentRows(
        projected, claim_source=claim_source, research_source=research_source)
