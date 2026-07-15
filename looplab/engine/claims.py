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

import json
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
                          by: str = "operator", at: str = "") -> dict:
    """Persist an OPERATOR verdict on a claim (ratify / reject / pin). Append-only JSONL keyed by the same
    `normalize_statement` identity claims use, so it overlays the machine-proposed assessment. This is the
    governance write of §22.4 — agents never call it; only a human/control-intent does. Returns the record.
    Best-effort atomic append; raises on an invalid decision or missing memory dir (a real operator error)."""
    from pathlib import Path
    if decision not in CLAIM_DECISIONS:
        raise ValueError(f"decision must be one of {CLAIM_DECISIONS}, got {decision!r}")
    s = str(statement or "").strip()
    if not s:
        raise ValueError("empty statement")
    if not memory_dir:
        raise ValueError("no memory_dir")
    # Bound the stored fields so a single operator write can't bloat the shared sidecar (the `key` — the
    # claim identity — is `normalize_statement`, which already caps internally, so the caps don't shift it).
    rec = {"statement": s[:2000], "key": normalize_statement(s), "decision": decision,
           "note": str(note or "")[:4000], "by": str(by or "operator")[:120], "at": str(at or "")}
    path = Path(memory_dir) / "claim_decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    import json as _json

    from looplab.events.eventstore import _interprocess_lock
    # Same interprocess lock the lesson/capsule sidecar stores use: a concurrent operator write (a second
    # UI tab, or the CLI racing the POST) must not interleave/tear this append (CODEX).
    with _interprocess_lock(Path(str(path) + ".lock")):
        with open(path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec) + "\n")
    return rec


def load_claim_decisions(memory_dir) -> dict:
    """The LATEST operator decision per claim key (last write wins). {} when none / unreadable."""
    from pathlib import Path

    from looplab.events.eventstore import read_jsonl_lenient
    if not memory_dir:
        return {}
    path = Path(memory_dir) / "claim_decisions.jsonl"
    if not path.exists():
        return {}
    out: dict = {}
    for r in read_jsonl_lenient(path, loads=json.loads, dicts_only=True):
        k = str(r.get("key") or normalize_statement(r.get("statement", "")))
        if k and r.get("decision") in CLAIM_DECISIONS:
            out[k] = r            # last wins
    return out


def claim_assessments(lessons: list[dict], *, research_claims: Optional[list[dict]] = None,
                      decisions: Optional[dict] = None) -> list[dict]:
    """Project distilled `lessons` (+ optional D8 `research_claims`) into evidence-grounded claim
    assessments. Groups by normalized statement; each claim carries `support`/`oppose` node-id evidence,
    contributing `runs`/`scopes`, and an `epistemic` state. `decisions` (from `load_claim_decisions`)
    overlays an operator `maturity` (`operator-ratified`/`operator-rejected`/`operator-pinned`, else
    `machine-proposed`) — the §22.4 governance overlay. Sorted most-evidenced first. Pure."""
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
    return out


# --------------------------------------------------------------------------- #
# Step 5 (§21.20.5): a BOUNDED context pack for a proposing agent — evidence AND counter-arguments.
# --------------------------------------------------------------------------- #

_CAVEAT_STATES = ("mixed", "refuted", "inconclusive")


def build_context_pack(claims: list[dict], *, concept_overview: Optional[dict] = None,
                       max_claims: int = 5) -> dict:
    """Assemble a token-BOUNDED cross-run context pack from claim assessments (+ an optional concept
    overview) for a proposing agent (§21.20.5, Step 5). The design's hard rule is that positive hits must
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
    live = [c for c in (claims or []) if c.get("maturity") != "operator-rejected"]
    ratified = [c for c in live if c.get("maturity") == "operator-ratified"]
    rest = [c for c in live if c.get("maturity") != "operator-ratified"]
    by_state: dict[str, list] = {"mixed": [], "supported": [], "refuted": [], "inconclusive": []}
    for c in rest:
        by_state.get(c["epistemic"], by_state["inconclusive"]).append(c)
    # ratified first, then contested (counter-argument), then supported, then remaining caveats.
    ordered = (ratified + by_state["mixed"] + by_state["supported"]
               + by_state["refuted"] + by_state["inconclusive"])
    picked = ordered[:max_claims]
    # Reserved caveat slot: if nothing picked carries a caveat but caveats exist, swap the weakest picked
    # (last, since `ordered` is strongest-first) for the strongest available caveat — opposition is never
    # crowded out by a full slate of positives.
    if picked and not any(c["epistemic"] in _CAVEAT_STATES for c in picked):
        # Include RATIFIED caveats too: a ratified mixed/refuted/inconclusive claim pushed past max_claims by
        # the ratified block must still be able to fill the reserved slot, or a slate of ratified-supported
        # claims could crowd opposition out — the exact §20.5 rule this slot exists to protect (CODEX).
        caveats = ([c for c in ratified if c["epistemic"] in _CAVEAT_STATES]
                   + by_state["mixed"] + by_state["refuted"] + by_state["inconclusive"])
        if caveats:
            picked = picked[:-1] + [caveats[0]]

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
        "n_contested": len(by_state["mixed"]) + sum(1 for c in ratified if c["epistemic"] == "mixed"),
    }
    if concept_overview:
        pack["coverage"] = {
            "n_runs": concept_overview.get("n_runs", 0),
            "n_concepts": concept_overview.get("n_concepts", 0),
            "top_concepts": [e["concept"] for e in (concept_overview.get("concepts") or [])[:max_claims]],
        }
    return pack


def portfolio_atlas(lessons: list[dict], capsules: list[dict], *, max_items: int = 8,
                    decisions: Optional[dict] = None) -> dict:
    """The Research Atlas DATA payload (§21.20 Step 6): one structured "what's been explored / where the
    thin spots are / what's contradictory" view, composing the concept overview (Step 3), the claim
    assessments (Step 4) and the bounded context pack (Step 5). Pure/deterministic — the read-model a
    Research Atlas UI (or an agent) would render; no LLM, no I/O.

    "Thin" is a lean gap proxy — concepts explored in only ONE run (single-run evidence). A true coverage
    frame (§20.6, unknown-vs-zero) is the deferred full-CR3a; this deliberately reports thin-coverage, not
    a false "never tried" (which needs a reference universe)."""
    from looplab.engine.memory import portfolio_concept_overview
    max_items = max(1, int(max_items))                       # normalize (CODEX): 0/negative -> at least 1
    overview = portfolio_concept_overview(capsules)
    claims = claim_assessments(lessons, decisions=decisions)
    # A contradiction the operator REJECTED is no longer a live contradiction — honor the verdict here too,
    # consistent with build_context_pack / cross_run_claims which also drop operator-rejected (CODEX).
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


def render_context_pack(pack: dict) -> str:
    """Render a context pack as a compact, bounded text block for a proposing agent (the advisory form).
    Deterministic; leads with contested evidence so the agent sees counter-arguments, not only positives."""
    if not pack.get("claims") and not pack.get("coverage"):
        return ""
    _mark = {"supported": "✓", "refuted": "✗", "mixed": "⚖", "inconclusive": "·"}
    lines = [f"Cross-run evidence ({pack.get('n_claims_total', 0)} claims, "
             f"{pack.get('n_contested', 0)} contested) — prior experiments, with counter-evidence:"]
    for c in pack.get("claims", []):
        lines.append(f"  {_mark.get(c['epistemic'], '?')} [{c['n_support']}↑/{c['n_oppose']}↓] "
                     f"{c['statement'][:120]}")
    cov = pack.get("coverage")
    if cov:
        top = ", ".join(cov.get("top_concepts", [])[:6])
        lines.append(f"Portfolio coverage: {cov.get('n_runs', 0)} run(s), {cov.get('n_concepts', 0)} "
                     f"concept(s){'; most-explored: ' + top if top else ''}.")
    return "\n".join(lines)
