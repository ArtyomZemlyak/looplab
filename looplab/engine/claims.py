"""PART IV cross-run Step 4 (§21.20) — evidence-grounded CLAIM assessments.

A pure read-model that projects the ALREADY-SHIPPED memory into verifiable claims: a distilled lesson
already carries `{statement, outcome, evidence:[node_ids], run_id, task_id}` (a verdict + its grounding
nodes), and a D8 deep-research memo carries `claims:[{statement, node_ids, urls}]`. This module UNIFIES
those two shapes (it does not fork a third): it groups by normalized statement and records support vs
oppose evidence refs plus an epistemic state, so the loop/UI can ask "what does the accumulated evidence
suggest, and what contradicts it?" — the §21.20.5 claim idea in lean form.

Deliberately pure/deterministic and off any live path: no new store, no LLM, no I/O. The verdict→stance
mapping reuses the shipped lesson vocabulary (`memory._NEGATIVE` / "supported"); a "noted"/unknown verdict
is neutral (it takes no stance), exactly as on the lesson read/write paths.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Optional

from looplab.engine.memory import _NEGATIVE, normalize_statement


def _node_ids(raw) -> list:
    """Evidence node-id refs from a lesson's `evidence` or a claim's `node_ids`: ints kept as ints,
    numeric strings coerced, everything else dropped (a URL/source belongs in `sources`, not evidence)."""
    out = []
    for x in raw or []:
        if isinstance(x, bool):
            continue
        if isinstance(x, int):
            out.append(x)
        elif isinstance(x, str) and x.strip().lstrip("-").isdigit():
            out.append(int(x))
    return out


def _qualify_refs(run_id, node_ids) -> list[str]:
    """Run-QUALIFY evidence refs so (r1,node0) and (r2,node0) never collapse: a bare node id is run-local.
    "?" marks a ref whose run is unknown (e.g. a D8 claim without a run_id)."""
    r = str(run_id or "?")
    return [f"{r}:{n}" for n in node_ids]


def _epistemic(support, oppose) -> str:
    """The evidence's current verdict on a claim. 'mixed' when both sides exist (a scoped disagreement,
    never newest-wins); 'inconclusive' when only neutral/unknown evidence remains — distinct from a
    supported/refuted claim (§21.20.1: absence is not failure)."""
    if support and oppose:
        return "mixed"
    if support:
        return "supported"
    if oppose:
        return "refuted"
    return "inconclusive"


# --------------------------------------------------------------------------- #
# Operator claim DECISIONS (§22.4) — the ONLY write to cross-run MEANING an actor other than the engine
# may make. Append-only, keyed by normalized statement, overlaid on the machine-proposed assessment.
# --------------------------------------------------------------------------- #

CLAIM_DECISIONS = ("ratified", "rejected", "pinned")


def record_claim_decision(memory_dir, *, statement: str, decision: str, note: str = "",
                          by: str = "operator", at: str = "", scope: str = "", metric: str = "") -> dict:
    """Persist an OPERATOR verdict on a claim (ratify / reject / pin). Append-only JSONL, keyed BOTH by the
    legacy `normalize_statement` (so the lean projection still overlays) AND by a structured `claim_uid`
    (scope+polarity-precise, so a decision in task A never reaches a same-worded claim in task B — CODEX).
    `scope` (task id) / `metric` qualify the structured key. This is the §22.4 governance write — agents
    never call it. Returns the record. Durable locked+fsynced append; raises on an invalid decision or
    missing memory dir (a real operator error)."""
    from pathlib import Path

    from looplab.engine.claim_key import claim_uid
    from looplab.engine.concept_registry import _append_governance
    if decision not in CLAIM_DECISIONS:
        raise ValueError(f"decision must be one of {CLAIM_DECISIONS}, got {decision!r}")
    s = str(statement or "").strip()
    if not s:
        raise ValueError("empty statement")
    if not memory_dir:
        raise ValueError("no memory_dir")
    # Bound the stored free-text fields so a single operator write can't bloat the shared sidecar. Identity —
    # the legacy `key` (normalize_statement, capped internally at 160) AND the structured `claim_uid`
    # (scope+polarity-precise) — is computed from the FULL statement/scope/metric, so the display caps never
    # shift which claim the decision overlays (CODEX).
    rec = {"statement": s[:2000], "key": normalize_statement(s),
           "claim_uid": claim_uid(s, scope=scope, metric=metric), "scope": str(scope or ""),
           "metric": str(metric or ""), "decision": decision, "note": str(note or "")[:4000],
           "by": str(by or "operator")[:120], "at": str(at or "")}
    path = Path(memory_dir) / "claim_decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    _append_governance(path, rec)      # locked, fsynced append (shared governance write, CODEX)
    return rec


def _global_key(legacy_key: str) -> str:
    """A DISTINCT index (in the same decisions dict) for the last SCOPE-LESS decision on a statement, so
    a later scoped decision that overwrites the plain legacy key can't hide the portfolio-wide verdict
    from the structured fallback. The control-char prefix can't collide with a normalize_statement key
    or a claim_uid. The dict is only ever read via `.get(key)`, never iterated, so extra keys are safe."""
    return "\x00global\x00" + legacy_key


def load_claim_decisions(memory_dir) -> dict:
    """The LATEST operator decision, indexed by its legacy statement key, its structured `claim_uid`, AND
    (for scope-less decisions) a distinct global key — so the lean projection overlays by normalized
    statement, the structured projection overlays by uid, and the structured fallback can still find a
    GLOBAL verdict after a later scoped decision overwrote the legacy key. Last write wins per key.
    {} when none / unreadable."""
    from pathlib import Path

    from looplab.events.eventstore import read_jsonl_lenient
    if not memory_dir:
        return {}
    path = Path(memory_dir) / "claim_decisions.jsonl"
    if not path.exists():
        return {}
    out: dict = {}
    for r in read_jsonl_lenient(path, loads=json.loads, dicts_only=True):
        if r.get("decision") not in CLAIM_DECISIONS:
            continue
        k = str(r.get("key") or normalize_statement(r.get("statement", "")))
        if k:
            out[k] = r            # last wins (legacy statement key) — the LEAN path (caller passes scoped
            #                       lessons, so scope isolation comes from the input, not this lookup)
            # ALSO index the last GLOBAL (scope-less) decision under a DISTINCT key. The structured
            # fallback needs the portfolio-wide verdict, but a LATER scoped ratify/reject/pin on the same
            # statement overwrites out[k] last-wins — so without this, a global decision silently stopped
            # applying to every OTHER scope once any scoped decision on that statement was recorded.
            if not str(r.get("scope") or ""):
                out[_global_key(k)] = r
        uid = str(r.get("claim_uid") or "")
        if uid:
            out[uid] = r          # structured scope+polarity key
    return out


# --------------------------------------------------------------------------- #
# D8 research claims persisted cross-run (§21.20 / CR1b) — so a deep-research memo's evidence-backed
# claims survive their run and can CONTEST/support lesson verdicts (contested is otherwise unreachable
# from newest-verdict-wins lessons alone). Written at finalize; read by the claim assessments callers.
# --------------------------------------------------------------------------- #

def record_research_claims(memory_dir, *, run_id: str, task_id: str, claims) -> int:
    """Upsert (by run_id) a run's D8 research claims into `research_claims.jsonl`. Each row:
    {run_id, task_id, statement, node_ids, urls}. Append-with-replace so a re-run doesn't double-count.
    Returns how many rows were written. Best-effort atomicity via the shared whole-file writer."""
    from pathlib import Path

    from looplab.events.eventstore import _interprocess_lock, read_jsonl_lenient, write_jsonl_atomic
    if not memory_dir:
        return 0
    rid = str(run_id or "")
    rows = []
    for c in claims or []:
        stmt = str((c.get("statement") if isinstance(c, dict) else "") or "").strip()
        if not stmt:
            continue
        rows.append({"run_id": rid, "task_id": str(task_id or ""), "statement": stmt,
                     "node_ids": list(c.get("node_ids") or []), "urls": list(c.get("urls") or [])})
    path = Path(memory_dir) / "research_claims.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Hold the same interprocess lock the case/capsule/decision sidecar stores use — and RE-READ inside it —
    # so two runs sharing memory_dir don't clobber each other's D8 claims in this whole-file read-modify-write
    # (write_jsonl_atomic is crash-atomic, NOT concurrency-safe; last-writer-wins would silently drop the
    # loser's claims and under-count `contested`) (CODEX). This closes the unlocked read-modify-replace the
    # review flagged: two finalizers reading E then replacing with E+r1 / E+r2 would erase each other.
    with _interprocess_lock(Path(str(path) + ".lock")):
        existing = read_jsonl_lenient(path, loads=json.loads, dicts_only=True) if path.exists() else []
        kept = [r for r in existing if str(r.get("run_id") or "") != rid]   # drop this run's old rows
        write_jsonl_atomic(path, kept + rows, dumps=json.dumps)
    return len(rows)


def load_research_claims(memory_dir) -> list[dict]:
    """The persisted cross-run D8 research claims (statement + run-qualified node evidence). [] when none."""
    from pathlib import Path

    from looplab.events.eventstore import read_jsonl_lenient
    if not memory_dir:
        return []
    path = Path(memory_dir) / "research_claims.jsonl"
    return read_jsonl_lenient(path, loads=json.loads, dicts_only=True) if path.exists() else []


def _scoped_research(memory_dir, scope_task: str) -> list[dict]:
    """D8 research claims, filtered to a bound task when `scope_task` is set (mega-review: a task-bound tool
    must not read another task's research claims). Portfolio-wide when scope_task is "" (CLI/unbound)."""
    research = load_research_claims(memory_dir)
    st = str(scope_task or "")
    return [r for r in research if str(r.get("task_id") or "") == st] if st else research


def claims_for_memory(memory_dir, *, lessons=None, fuzzy: bool = False,
                      structured: bool = False, scope_task: str = "") -> list[dict]:
    """Convenience: `claim_assessments` over a memory dir — lessons.jsonl (or a pre-filtered `lessons`) +
    the persisted D8 research claims + the operator-decision overlay. One call so every read path applies
    research claims AND decisions consistently. `fuzzy` (opt-in) merges paraphrased claims (CR1b);
    `structured` (opt-in) uses the scope+polarity-safe structured claim key (the full CR); `scope_task`
    filters the D8 research claims to the bound task so a task-scoped caller does not re-read another task's
    research claims (mega-review) — the decisions overlay is applied scope-safely by `claim_assessments`."""
    from pathlib import Path

    from looplab.events.eventstore import read_jsonl_lenient
    base = Path(memory_dir) if memory_dir else None
    if lessons is None:
        lp = base / "lessons.jsonl" if base else None
        lessons = read_jsonl_lenient(lp, loads=json.loads, dicts_only=True) if (lp and lp.exists()) else []
    return claim_assessments(lessons, research_claims=_scoped_research(memory_dir, scope_task),
                             decisions=load_claim_decisions(memory_dir), fuzzy=fuzzy, structured=structured)


def atlas_for_memory(memory_dir, *, lessons=None, capsules=None, max_items: int = 8,
                     structured: bool = False, scope_task: str = "") -> dict:
    """Convenience: `portfolio_atlas` over a memory dir with EVERY overlay loaded — lessons + D8 research
    claims + operator decisions + concept aliases + splits. One call so every atlas surface is consistent.
    `structured` keeps the claim projection consistent with the researcher advisory; `scope_task` filters
    the D8 research claims to the bound task so a task-scoped caller does not surface another task's
    claims/contradictions (mega-review)."""
    from pathlib import Path

    from looplab.engine.concept_registry import load_concept_aliases, load_concept_splits
    from looplab.engine.memory import ConceptCapsuleStore
    from looplab.events.eventstore import read_jsonl_lenient
    base = Path(memory_dir) if memory_dir else None
    if lessons is None:
        lp = base / "lessons.jsonl" if base else None
        lessons = read_jsonl_lenient(lp, loads=json.loads, dicts_only=True) if (lp and lp.exists()) else []
    if capsules is None:
        cp = base / "concept_capsules.jsonl" if base else None
        capsules = ConceptCapsuleStore(cp).all() if (cp and cp.exists()) else []
    return portfolio_atlas(lessons, capsules, max_items=max_items, structured=structured,
                           decisions=load_claim_decisions(memory_dir),
                           research_claims=_scoped_research(memory_dir, scope_task),
                           aliases=load_concept_aliases(memory_dir),
                           splits=load_concept_splits(memory_dir))


_CLAIM_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _stmt_tokens(s: str) -> frozenset:
    return frozenset(w for w in _CLAIM_WORD.findall((s or "").casefold()) if len(w) > 2)


def _fuzzy_merge_claims(claims: list[dict], *, threshold: float = 0.6) -> list[dict]:
    """CR1b (lean, opt-in): merge claims whose STATEMENTS are genuine PARAPHRASES into one. Identity is a
    strict token-JACCARD test (>= `threshold`) via union-find — deliberately NOT the hybrid top-k
    clustering, which blobs a HOMOGENEOUS corpus (every claim shares the task's vocabulary) into one giant
    cluster (verified: it collapsed 73 distinct rubert claims into 1). Jaccard 0.6 requires most words to
    match, so distinct techniques stay separate and only near-identical restatements merge. Suggestion-grade
    -> opt-in; the structured semantic claim key is the full CR1b. Unions evidence/runs/scopes/sources,
    keeps the most-evidenced statement as the label, carries an operator maturity if any member had one."""
    n = len(claims)
    if n <= 1:
        return claims
    toks = [_stmt_tokens(c["statement"]) for c in claims]
    parent = list(range(n))

    # CODEX AGENT: token-set overlap is not a safe paraphrase or governance identity. For example,
    # "dropout improves model generalization" and "dropout never improves model generalization" clear
    # 0.6 and merge; a ratified member and a rejected member then inherit whichever non-machine maturity
    # happens to appear first below. Add semantic polarity/qualifier checks and define decision conflicts
    # explicitly before allowing this projection to combine operator-controlled claims.

    def _find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    # CODEX AGENT: all-pairs comparison is O(n^2), and union-find turns the threshold into single-linkage:
    # A~B and B~C merge A with C even when A/C are below 0.6 (reproduced with two one-token substitutions).
    # Use a bounded candidate index, then require every member to satisfy a representative/complete-link
    # invariant; otherwise a large or bridge-heavy portfolio can both stall the CLI and over-collapse claims.
    for i in range(n):
        if not toks[i]:
            continue
        for j in range(i + 1, n):
            if not toks[j]:
                continue
            inter = len(toks[i] & toks[j])
            if inter and inter / len(toks[i] | toks[j]) >= threshold:
                parent[_find(i)] = _find(j)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(_find(i), []).append(i)

    out = []
    for idxs in groups.values():
        members = [claims[i] for i in idxs]
        if len(members) == 1:
            out.append(members[0])
            continue
        sup = sorted({r for m in members for r in m["support"]})
        opp = sorted({r for m in members for r in m["oppose"]})
        rep = max(members, key=lambda m: (m["n_support"] + m["n_oppose"], m["statement"]))
        mat = next((m["maturity"] for m in members if m.get("maturity") != "machine-proposed"),
                   "machine-proposed")
        out.append({
            "statement": rep["statement"], "epistemic": _epistemic(sup, opp), "maturity": mat,
            "support": sup, "oppose": opp, "n_support": len(sup), "n_oppose": len(opp),
            "runs": sorted({r for m in members for r in m["runs"]}),
            "scopes": sorted({r for m in members for r in m["scopes"]}),
            "sources": sorted({s for m in members for s in m.get("sources", [])}),
            "merged_from": sorted(m["statement"] for m in members),
        })
    out.sort(key=lambda c: (-(c["n_support"] + c["n_oppose"]), -c["n_oppose"], c["statement"]))
    return out


def _structured_assessments(lessons, research_claims, decisions) -> list[dict]:
    """The SCOPE+POLARITY-safe structured projection (full CR of the lean fuzzy merge). Identity is the
    `claim_signature` merge_key: (subject stems, scope=task, metric, polarity). Opposite-polarity claims
    sharing a `contra_key` are surfaced as a CONTRADICTION (they never merge, and each is marked contested).
    Governance overlays by the structured `claim_uid` (scope-precise)."""
    from looplab.engine.claim_key import claim_signature
    groups: dict[str, dict] = {}

    def _grp(statement, scope):
        s = str(statement or "").strip()
        if not s:
            return None
        sig = claim_signature(s, scope=str(scope or ""))
        if sig["polarity"] == 0:                     # no subject content -> not a claim
            return None
        g = groups.get(sig["merge_key"])
        if g is None:
            g = groups[sig["merge_key"]] = {
                "uid": sig["uid"], "contra_key": sig["contra_key"], "polarity": sig["polarity"],
                "scope": sig["scope"], "support": set(), "oppose": set(), "runs": set(),
                "scopes": set(), "sources": set(), "_ev": {}}
        g["_ev"][s] = g["_ev"].get(s, 0)             # candidate representative statements (evidence-weighted)
        return g

    for lz in lessons or []:
        g = _grp(lz.get("statement"), lz.get("task_id"))
        if g is None:
            continue
        if lz.get("run_id"):
            g["runs"].add(str(lz["run_id"]))
        if lz.get("task_id"):
            g["scopes"].add(str(lz["task_id"]))
        refs = _qualify_refs(lz.get("run_id"), _node_ids(lz.get("evidence")))
        outcome = str(lz.get("outcome") or "")
        if outcome == "supported":
            g["support"].update(refs)
        elif outcome in _NEGATIVE:
            g["oppose"].update(refs)
        g["_ev"][str(lz.get("statement") or "").strip()] += len(refs)

    for rc in research_claims or []:
        g = _grp(rc.get("statement"), rc.get("task_id"))
        if g is None:
            continue
        if rc.get("run_id"):
            g["runs"].add(str(rc["run_id"]))         # D8 rows DO register their run/scope now (CODEX)
        if rc.get("task_id"):
            g["scopes"].add(str(rc["task_id"]))
        refs = _qualify_refs(rc.get("run_id"), _node_ids(rc.get("node_ids")))
        g["support"].update(refs)
        g["_ev"][str(rc.get("statement") or "").strip()] += len(refs)
        for u in (rc.get("urls") or []):
            if isinstance(u, str) and u:
                g["sources"].add(u)

    # Contradiction map: a contra_key seen with BOTH polarities means two opposite claims about one subject
    # in one scope — the portfolio disagrees with itself at the ASSERTION level (unreachable from a single
    # merged statement). Each such claim is marked contested and carries its opposites' representative text.
    contra: dict[str, dict[int, list]] = {}
    for g in groups.values():
        contra.setdefault(g["contra_key"], {}).setdefault(g["polarity"], []).append(g)

    _dec = {"ratified": "operator-ratified", "rejected": "operator-rejected", "pinned": "operator-pinned"}
    out = []
    for g in groups.values():
        rep = max(g["_ev"], key=lambda s: (g["_ev"][s], s)) if g["_ev"] else ""
        sup, opp = sorted(g["support"]), sorted(g["oppose"])
        opposites = [og for pol, gs in contra.get(g["contra_key"], {}).items() if pol != g["polarity"]
                     for og in gs]
        contradicts = sorted({max(o["_ev"], key=lambda s: (o["_ev"][s], s)) for o in opposites if o["_ev"]})
        d = (decisions or {}).get(g["uid"])
        if d is None:
            # A SCOPE-LESS operator decision (the default `claim-decide` with no --scope) applies to EVERY
            # scope of that statement. Read it from the DISTINCT global index — not the plain legacy key —
            # so a LATER scoped decision on the same statement (which overwrites the legacy key last-wins)
            # can't hide the portfolio-wide verdict here (mega-review finding: global-then-scoped silently
            # dropped the global decision for every other scope). A scoped decision only ever reaches its
            # own scope, via its uid above.
            d = (decisions or {}).get(_global_key(normalize_statement(rep)))
        out.append({
            "statement": rep,
            # a polarity contradiction is the strongest contested signal -> mixed even if this side's own
            # evidence is one-directional (that is exactly what the structured key makes reachable).
            "epistemic": "mixed" if contradicts else _epistemic(sup, opp),
            "maturity": _dec.get((d or {}).get("decision"), "machine-proposed"),
            "support": sup, "oppose": opp, "n_support": len(sup), "n_oppose": len(opp),
            "runs": sorted(g["runs"]), "scopes": sorted(g["scopes"]), "sources": sorted(g["sources"]),
            "claim_uid": g["uid"], "polarity": g["polarity"], "contradicts": contradicts,
        })
    out.sort(key=lambda c: (-(c["n_support"] + c["n_oppose"]), -c["n_oppose"],
                            0 if c["contradicts"] else 1, c["statement"]))
    return out


def claim_assessments(lessons: list[dict], *, research_claims: Optional[list[dict]] = None,
                      decisions: Optional[dict] = None, fuzzy: bool = False,
                      structured: bool = False) -> list[dict]:
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
    if structured:
        return _structured_assessments(lessons, research_claims, decisions)
    groups: dict[str, dict] = {}

    def _group(stmt: str) -> Optional[dict]:
        s = str(stmt or "").strip()
        if not s:
            return None
        # NOTE (CODEX): identity here is the normalized STATEMENT (the shipped lesson `normalize_statement`
        # key) — it can merge same-worded claims across incompatible scopes and the 160-char cap can
        # collide. A structured semantic claim key (subject/intervention/comparator/scope) is the CR1b TODO
        # (§21.20.13); this lean projection keeps scope/runs as metadata on the claim.
        return groups.setdefault(normalize_statement(s), {
            "statement": s, "support": set(), "oppose": set(),
            "runs": set(), "scopes": set(), "sources": set()})

    def _qualify(run_id, node_ids) -> list[str]:
        # Run-QUALIFY evidence refs so (r1,node0) and (r2,node0) never collapse (CODEX): a bare node id is
        # run-local. "?" marks a ref whose run is unknown (e.g. a D8 claim without a run_id).
        r = str(run_id or "?")
        return [f"{r}:{n}" for n in node_ids]

    for lz in lessons or []:
        g = _group(lz.get("statement"))
        if g is None:
            continue
        if lz.get("run_id"):
            g["runs"].add(str(lz["run_id"]))
        if lz.get("task_id"):
            g["scopes"].add(str(lz["task_id"]))
        outcome = str(lz.get("outcome") or "")
        refs = _qualify(lz.get("run_id"), _node_ids(lz.get("evidence")))
        # NOTE (CODEX): the lesson OUTCOME is a verdict on the action, mapped here to support/oppose of the
        # STATEMENT. A negative-effect statement whose verdict confirms the regression ("changing X hurt")
        # is therefore filed as oppose; an explicit claim POLARITY (and honoring a consolidated row's
        # `evidence_count`) is the CR1b TODO. Lean mapping: supported→support, {tested/abandoned/failed/
        # refuted}→oppose, noted/unknown→neutral.
        if outcome == "supported":
            g["support"].update(refs)
        elif outcome in _NEGATIVE:
            g["oppose"].update(refs)
        # "noted"/unknown -> neutral: still registers the run/scope, but takes NO stance.

    for rc in research_claims or []:
        g = _group(rc.get("statement"))
        if g is None:
            continue
        # CODEX AGENT: unlike lessons above, D8 rows never add their run_id/task_id to `runs`/`scopes`;
        # a D8-only Atlas therefore reports zero runs and loses the metadata needed for task filtering.
        # The row also has no persisted verifier stance, so the mere presence of an integer citation is
        # counted as support without resolving the node/generation. Preserve provenance + verification and
        # model unresolved/cited evidence separately from verified positive support.
        # A D8 memo claim CITES the experiments it rests on -> support evidence. URLs are external sources.
        # NOTE (CODEX): citation is not verification — a full claim requires a verified verdict + resolvable
        # run-qualified evidence (CR1b TODO). This unification is exercised via the API/callers that pass
        # `research_claims`; the shipped CLIs read lessons only (they do not fabricate D8 claims).
        g["support"].update(_qualify(rc.get("run_id"), _node_ids(rc.get("node_ids"))))
        for u in (rc.get("urls") or []):
            if isinstance(u, str) and u:
                g["sources"].add(u)

    _dec = {"ratified": "operator-ratified", "rejected": "operator-rejected", "pinned": "operator-pinned"}
    out = []
    for key, g in groups.items():
        sup, opp = sorted(g["support"]), sorted(g["oppose"])
        d = (decisions or {}).get(key)
        # SCOPE GUARD (mega-review): a SCOPED decision (scope != "") governs ONLY a claim whose contributing
        # task scopes are wholly within that scope — otherwise a reject/ratify scoped to task A leaks onto a
        # same-worded claim in task B (this lean projection groups by statement across tasks, and it is the
        # DEFAULT read path for tools/serve/retrieval). A scope-LESS decision still applies everywhere.
        if d is not None:
            _dscope = str(d.get("scope") or "")
            if _dscope:
                _real = {s for s in g["scopes"] if s}
                if not _real or not _real <= {_dscope}:
                    d = None
        if d is None:
            # Fall back to the DISTINCT global index: a scope-LESS decision applies to EVERY scope, and it
            # must survive a LATER scoped decision that overwrote the plain legacy key last-wins — the same
            # global-then-scoped bug the structured path fixed, which the lean path shared (a global reject
            # was silently dropped for other scopes once any scoped decision on that statement landed).
            d = (decisions or {}).get(_global_key(key))
        # CODEX AGENT: note/by/at are discarded here, so API/CLI/Atlas/tools cannot explain who changed the
        # claim or why after the one write response is gone. Carry the current decision record (and expose
        # append-only history) rather than reducing an auditable governance action to one maturity string.
        out.append({
            "statement": g["statement"],
            "epistemic": _epistemic(sup, opp),
            "maturity": _dec.get((d or {}).get("decision"), "machine-proposed"),
            "support": sup, "oppose": opp,
            "n_support": len(sup), "n_oppose": len(opp),
            "runs": sorted(g["runs"]), "scopes": sorted(g["scopes"]),
            "sources": sorted(g["sources"]),
        })
    # most-evidenced first (support+oppose), contested claims break ties toward visibility, then statement
    out.sort(key=lambda c: (-(c["n_support"] + c["n_oppose"]), -c["n_oppose"], c["statement"]))
    return _fuzzy_merge_claims(out) if fuzzy else out


# --------------------------------------------------------------------------- #
# Step 5 (§21.20.5): a BOUNDED context pack for a proposing agent — evidence AND counter-arguments.
# --------------------------------------------------------------------------- #

_CAVEAT_STATES = ("mixed", "refuted", "inconclusive")


def build_context_pack(claims: list[dict], *, concept_overview: Optional[dict] = None,
                       max_claims: int = 5) -> dict:
    """Assemble a CLAIM-COUNT-bounded cross-run context pack from claim assessments (+ an optional concept
    overview) for a proposing agent (§21.20.5, Step 5). ("Claim-count", not token/byte: the pack caps the
    number of claims + per-claim field lengths; a true serialized-token envelope is the CR2b TODO — see the
    NOTE below.) The design's hard rule is that positive hits must
    never crowd out caveats: contested (`mixed`) claims come first, and a **caveat slot is reserved** so at
    least one mixed/refuted/inconclusive claim is included whenever one exists. Pure/deterministic and
    'silent' by construction — it just returns structured data; promoting it to advisory prompt-grounding
    is a separate, gated step (never wired here). No LLM, no I/O."""
    # NOTE (CODEX): this bounds by CLAIM COUNT + per-claim field caps (below), not a serialized token/byte
    # budget — a true token envelope is the CR2b TODO. `max_claims<1` is normalized to 1.
    max_claims = max(1, int(max_claims))
    # Operator governance (§22.4): a claim the operator REJECTED is dropped from the agent's context (the
    # human overruled it); a RATIFIED claim is surfaced FIRST (the human vouched for it), ahead of even the
    # contested ones. `machine-proposed` claims keep the default contested-first ordering.
    # CODEX AGENT: `operator-pinned` receives no retention priority, so a successful pin can fall outside
    # max_claims. Conversely, with max_claims=1 the caveat swap below can evict the ratified claim that this
    # comment promises is surfaced first. Define explicit pin/ratify/caveat precedence (and an unpin/clear
    # transition) before treating these writes as enforceable governance semantics.
    live = [c for c in (claims or []) if c.get("maturity") != "operator-rejected"]
    # operator-RATIFIED ("vouched for") and operator-PINNED ("always keep visible") both get retention +
    # front priority, so a pinned/ratified claim is never evicted by max_claims or the caveat swap
    # (mega-review: pinned had no retention). ratified leads, then pinned, then contested-first.
    _kept = ("operator-ratified", "operator-pinned")
    ratified = [c for c in live if c.get("maturity") == "operator-ratified"]
    pinned = [c for c in live if c.get("maturity") == "operator-pinned"]
    rest = [c for c in live if c.get("maturity") not in _kept]
    by_state: dict[str, list] = {"mixed": [], "supported": [], "refuted": [], "inconclusive": []}
    for c in rest:
        by_state.get(c["epistemic"], by_state["inconclusive"]).append(c)
    # ratified + pinned first, then contested (counter-argument), then supported, then remaining caveats.
    ordered = (ratified + pinned + by_state["mixed"] + by_state["supported"]
               + by_state["refuted"] + by_state["inconclusive"])
    picked = ordered[:max_claims]
    # Reserved caveat slot: if nothing picked carries a caveat but caveats exist, swap the weakest NON-kept
    # picked (a governance-retained claim is never evicted to make room) for the strongest available caveat —
    # opposition is never crowded out by a full slate of positives (§20.5). Kept caveats count as caveats too.
    if picked and not any(c["epistemic"] in _CAVEAT_STATES for c in picked):
        caveats = ([c for c in (ratified + pinned) if c["epistemic"] in _CAVEAT_STATES]
                   + by_state["mixed"] + by_state["refuted"] + by_state["inconclusive"])
        # Evict the weakest NON-kept positive if there is one (so a governance-retained claim is protected);
        # but if EVERY pick is kept, surface opposition anyway by evicting the last (a ratified caveat must
        # still be able to displace a ratified positive — §20.5 opposition wins). All picks here are
        # non-caveat (the guard above), so any evicted claim is a positive.
        victim = next((i for i in range(len(picked) - 1, -1, -1)
                       if picked[i].get("maturity") not in _kept), len(picked) - 1)
        if caveats:
            picked = picked[:victim] + picked[victim + 1:] + [caveats[0]]

    def _slim(c: dict) -> dict:
        # Evidence refs are run-QUALIFIED ("run:node"), so the truncated support/oppose lists stay citable;
        # keep runs/scopes too so a reader can resolve the claim's provenance (CODEX).
        return {"statement": c["statement"][:300], "epistemic": c["epistemic"],
                "maturity": c.get("maturity", "machine-proposed"),
                "n_support": c["n_support"], "n_oppose": c["n_oppose"],
                "support": c["support"][:6], "oppose": c["oppose"][:6],
                "runs": c.get("runs", [])[:6], "scopes": c.get("scopes", [])[:6]}

    pack = {
        "claims": [_slim(c) for c in picked],
        "n_claims_total": len(claims or []),
        # count contested across BOTH pools — a ratified claim is pulled out of by_state but is still mixed.
        # count contested across ALL pools — a ratified/pinned claim is pulled out of by_state but still mixed.
        "n_contested": len(by_state["mixed"]) + sum(1 for c in (ratified + pinned) if c["epistemic"] == "mixed"),
    }
    if concept_overview:
        pack["coverage"] = {
            "n_runs": concept_overview.get("n_runs", 0),
            "n_concepts": concept_overview.get("n_concepts", 0),
            "top_concepts": [e["concept"] for e in (concept_overview.get("concepts") or [])[:max_claims]],
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
_CAVEAT = frozenset(("mixed", "refuted"))     # claims that carry counter-evidence — the contradiction pool


def _classify_intent(query: str) -> str:
    """Map a free-text query to a retrieval INTENT (failed / contested / worked / explore) by cue overlap.
    Deterministic, no LLM. `explore` (neutral) when no cue fires — the safe default that reorders nothing."""
    toks = set(_CLAIM_WORD.findall(str(query or "").casefold()))
    scored = [(sum(1 for w in cues if w in toks), name) for name, cues in _INTENT_CUES.items()]
    best_n, best = max(scored, key=lambda t: (t[0], t[1]))
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


def cross_run_retrieve(memory_dir, query: str, *, k: int = 8, lessons=None, capsules=None,
                       scope_task: str = "", contradiction_quota: float = 0.34,
                       max_corpus: int = 2000, structured: bool = False, intent: Optional[str] = None) -> dict:
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

    Every source is SCOPED before indexing: pass scoped `lessons`/`capsules`, and `scope_task` filters the
    D8 research claims to that task so a task-bound agent cannot retrieve another task's claims (CODEX).
    Operator-rejected claims never enter the corpus. Advisory; pure w.r.t. the passed/loaded stores."""
    from pathlib import Path

    from looplab.engine.concept_registry import (load_concept_aliases, load_concept_splits)
    from looplab.engine.memory import ConceptCapsuleStore, portfolio_concept_overview
    from looplab.events.eventstore import read_jsonl_lenient
    base = Path(memory_dir) if memory_dir else None
    if capsules is None:
        cp = base / "concept_capsules.jsonl" if base else None
        capsules = ConceptCapsuleStore(cp).all() if (cp and cp.exists()) else []
    if lessons is None:
        lp = base / "lessons.jsonl" if base else None
        lessons = read_jsonl_lenient(lp, loads=json.loads, dicts_only=True) if (lp and lp.exists()) else []
    # Scope EVERY source (CODEX): the D8 research claims are filtered to `scope_task` so a bound agent never
    # retrieves another task's claims; decisions are a global governance overlay (they only ever REMOVE via
    # rejection). When no scope is given, behaviour is portfolio-wide (back-compat).
    research = load_research_claims(memory_dir)
    if scope_task:
        research = [r for r in research if str(r.get("task_id") or "") == scope_task]
    claims = [c for c in claim_assessments(lessons, research_claims=research,
                                           decisions=load_claim_decisions(memory_dir), structured=structured)
              if c.get("maturity") != "operator-rejected"]
    overview = portfolio_concept_overview(capsules, aliases=load_concept_aliases(memory_dir),
                                          splits=load_concept_splits(memory_dir))
    docs: list[tuple[str, str, dict]] = []
    for c in claims:
        docs.append(("claim", c["statement"], {"epistemic": c["epistemic"], "n_support": c["n_support"],
                                               "n_oppose": c["n_oppose"], "maturity": c.get("maturity")}))
    for e in overview["concepts"]:
        docs.append(("concept", e["concept"],
                     {"n_runs": e["n_runs"], "runs": [r["run_id"] for r in e["runs"][:5]]}))

    n_total = len(docs)
    truncated = max(0, n_total - max(1, int(max_corpus)))
    if truncated:
        docs = docs[:max(1, int(max_corpus))]     # bounded; truncation is REPORTED below, never silent
    # The AGENT may pass an explicit `intent` (it knows why it is searching — genuinely agentic); otherwise
    # classify deterministically from the query text. An unknown value falls back to classification.
    intent = intent if intent in _INTENTS else _classify_intent(query)
    kk = max(1, int(k))                            # normalize k (0/-1 -> 1) before it reaches the receipt
    # A why-recalled receipt: corpus revision (content digest), the degraded vector-channel semantics, the
    # classified intent + quota, and (below) the per-hit rank — enough to explain/reproduce a result.
    corpus_digest = hashlib.sha1("\x1f".join(f"{d[0]}:{d[1]}" for d in docs).encode("utf-8")).hexdigest()[:16]
    receipt = {"query": str(query or ""), "k": kk, "n_corpus": n_total,
               "channels": ["lexical", "bm25", "vector"], "intent": intent,
               "vector_channel": "hash_embed(64-bucket bag-of-words; lexical proxy, not semantic)",
               "corpus_digest": corpus_digest, "truncated": truncated,
               "contradiction_quota": round(float(contradiction_quota), 3)}
    if not docs or not str(query or "").strip():
        return {"results": [], "receipt": {**receipt, "n_hits": 0, "n_caveats": 0}}

    from looplab.search.hybrid_merge import HybridRetriever
    # Retrieve a POOL larger than k so the intent priority + contradiction quota have room to reorder/swap
    # without extra queries; the vector channel is the `hash_embed` bag-of-words (a lexical proxy — declared
    # in the receipt, not passed off as semantic retrieval).
    pool_n = min(len(docs), max(kk * 4, kk + 12))
    pool = HybridRetriever([t for _, t, _ in docs]).candidates(str(query), k=pool_n)
    ranked = [{"idx": i, "kind": docs[i][0], "text": docs[i][1], "score": round(float(s), 4),
               "rel_rank": r, **docs[i][2]} for r, (i, s) in enumerate(pool)]
    # INTENT eligibility: a STABLE re-sort that floats on-intent docs up while preserving relevance order
    # within each tier (explore => every doc eligible => order unchanged; test-safe).
    ranked.sort(key=lambda h: (0 if _eligible(h["kind"], h, intent) else 1, h["rel_rank"]))
    picked = ranked[:kk]

    # CONTRADICTION QUOTA: guarantee ~quota of the k slots are caveat (mixed/refuted) claims when the pool
    # has them — swapping the LEAST-relevant non-caveat picks (from the bottom) for the most-relevant unpicked
    # caveats, so the top relevance hit is never displaced and opposition is never crowded out.
    q = 0.5 if intent in ("failed", "contested") else float(contradiction_quota)
    # ceil(k*q) caveat slots, but capped at k-1 so the #1 relevance hit is NEVER evicted (at k=1 the target
    # is 0 — the single slot stays the top hit, as the swap contract promises; mega-review finding).
    target = min(int(kk * max(0.0, q) + 0.999), max(0, kk - 1))
    have = [h for h in picked if h["kind"] == "claim" and h.get("epistemic") in _CAVEAT]
    if target > len(have):
        picked_ids = {h["idx"] for h in picked}
        extra = [h for h in ranked if h["idx"] not in picked_ids
                 and h["kind"] == "claim" and h.get("epistemic") in _CAVEAT]
        need = target - len(have)
        for cav in extra[:need]:
            # evict the last (least-relevant) non-caveat pick; if none remain, stop (never drop a caveat)
            victim = next((h for h in reversed(picked) if not (h["kind"] == "claim"
                          and h.get("epistemic") in _CAVEAT)), None)
            if victim is None:
                break
            picked[picked.index(victim)] = cav
        picked.sort(key=lambda h: (0 if _eligible(h["kind"], h, intent) else 1, h["rel_rank"]))

    n_caveats = sum(1 for h in picked if h["kind"] == "claim" and h.get("epistemic") in _CAVEAT)
    results = [{k2: v for k2, v in h.items() if k2 != "idx"} for h in picked]
    # Report the EFFECTIVE quota actually applied (raised for failed/contested) + the reserved caveat target,
    # so the receipt explains why a contested claim was (or wasn't) surfaced — not just the configured base.
    return {"results": results, "receipt": {**receipt, "n_hits": len(results), "n_caveats": n_caveats,
                                            "effective_quota": round(q, 3), "caveat_target": target}}


def portfolio_atlas(lessons: list[dict], capsules: list[dict], *, max_items: int = 8,
                    decisions: Optional[dict] = None, research_claims: Optional[list[dict]] = None,
                    aliases: Optional[dict] = None, splits: Optional[dict] = None,
                    structured: bool = False) -> dict:
    """The Research Atlas DATA payload (§21.20 Step 6): one structured "what's been explored / where the
    thin spots are / what's contradictory" view, composing the concept overview (Step 3), the claim
    assessments (Step 4) and the bounded context pack (Step 5). Pure/deterministic — the read-model a
    Research Atlas UI (or an agent) would render; no LLM, no I/O.

    "Thin" is a lean gap proxy — concepts explored in only ONE run (single-run evidence). A true coverage
    frame (§20.6, unknown-vs-zero) is the deferred full-CR3a; this deliberately reports thin-coverage, not
    a false "never tried" (which needs a reference universe)."""
    from looplab.engine.memory import portfolio_concept_overview
    max_items = max(1, int(max_items))                       # normalize (CODEX): 0/negative -> at least 1
    overview = portfolio_concept_overview(capsules, aliases=aliases, splits=splits)
    claims = claim_assessments(lessons, research_claims=research_claims, decisions=decisions,
                               structured=structured)
    # A contradiction the operator REJECTED is no longer a live contradiction — honor the verdict here too,
    # consistent with build_context_pack / cross_run_claims which also drop operator-rejected (CODEX). (The
    # companion CODEX note — that `operator-pinned` has no retention semantics in these top-8 slices, so a
    # successful pin can still fall outside the cut — stays a deferred follow-up: pin-priority ordering would
    # shift context-pack contents and is out of scope for this integration.)
    contested = [c for c in claims if c["epistemic"] == "mixed" and c.get("maturity") != "operator-rejected"]
    thin = [e["concept"] for e in overview["concepts"] if e["n_runs"] == 1]
    # Run count spans BOTH sources — capsules AND the runs cited by lessons — so a lesson-only / legacy
    # memory (no opt-in capsules) is not reported as zero runs (CODEX). The authoritative scoped corpus
    # join (cross_run_index) is the full-CR TODO; this at least unions what the two memory stores know.
    run_ids = {c.get("run_id") for c in capsules if c.get("run_id")}
    for cl in claims:
        run_ids.update(cl.get("runs") or [])
    n_runs = len(run_ids)
    # Keep the embedded context-pack coverage n_runs CONSISTENT with the top-level count (both the union of
    # capsule + lesson-cited runs), so one atlas payload never reports two different run counts — otherwise a
    # lesson-only memory says n_runs>0 at the top but coverage.n_runs==0, the very "zero runs" artifact the
    # union set out to fix (CODEX).
    pack_overview = {**overview, "n_runs": n_runs}
    return {
        "n_runs": n_runs, "n_concepts": overview["n_concepts"],
        "n_claims": len(claims), "n_contested": len(contested),
        "explored": overview["concepts"][:max_items],        # what's been tried (concept × runs)
        "thin_coverage": thin[:max_items],                   # explored only once — thin evidence (lean gap)
        "contradictions": contested[:max_items],             # where the portfolio disagrees with itself
        "context_pack": build_context_pack(claims, concept_overview=pack_overview, max_claims=max_items),
    }


def _safe_text(s, limit: int = 120) -> str:
    """Sanitize UNTRUSTED memory text (claim statements / concept slugs — LLM/repo-derived) before it enters
    an agent prompt: strip control chars + collapse newlines/whitespace to a single space, then bound the
    length. Prevents newline/control-char prompt-injection through the cross-run advisory pack (mega-review)."""
    t = re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", str(s or ""))
    return re.sub(r"\s+", " ", t).strip()[:limit]


def render_context_pack(pack: dict) -> str:
    """Render a context pack as a compact, bounded text block for a proposing agent (the advisory form).
    Deterministic; leads with contested evidence so the agent sees counter-arguments, not only positives.
    All memory-derived text is sanitized (control chars/newlines stripped) — quoted DATA, not instructions
    (mega-review prompt-injection hardening)."""
    if not pack.get("claims") and not pack.get("coverage"):
        return ""
    _mark = {"supported": "✓", "refuted": "✗", "mixed": "⚖", "inconclusive": "·"}
    lines = [f"Cross-run evidence ({pack.get('n_claims_total', 0)} claims, "
             f"{pack.get('n_contested', 0)} contested) — prior experiments, with counter-evidence:"]
    for c in pack.get("claims", []):
        lines.append(f"  {_mark.get(c['epistemic'], '?')} [{c['n_support']}↑/{c['n_oppose']}↓] "
                     f"{_safe_text(c['statement'])}")
    cov = pack.get("coverage")
    if cov:
        top = ", ".join(_safe_text(t, 60) for t in cov.get("top_concepts", [])[:6])
        lines.append(f"Portfolio coverage: {cov.get('n_runs', 0)} run(s), {cov.get('n_concepts', 0)} "
                     f"concept(s){'; most-explored: ' + top if top else ''}.")
    return "\n".join(lines)
