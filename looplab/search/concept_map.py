"""Concept-map BUILD — vocabulary consolidation, per-task importance, and the D5 entry point.

Split out of `concept_graph.py` (doc 25 SE-09). This is the TOP of the concept cluster: it imports
the structure (`concept_graph`), the taggers (`concept_tagging`) and the analytics
(`concept_analytics`) and drives them end-to-end, so it is the one module of the five allowed to
depend on the other four.

That direction is why this file exists at all. The review's recommendation named three modules —
structure, tagging, analytics — plus a destination for the projections, and gave the fifth
responsibility (consolidation + `build_concept_map`) no home. Left behind in `concept_graph.py` it
would have made the base module import its own dependents: `concept_graph -> concept_tagging ->
concept_graph` is an ImportError the moment anything imports `concept_tagging` first. The four-way
split as written closes an import cycle inside `search/`; the five-way split does not.
"""
from __future__ import annotations

from typing import Optional

from looplab.core.models import RunState
# The cluster reaches a sibling's FUNCTIONS through the MODULE object, never by name. A
# `from looplab.search.concept_tagging import tag_nodes_heuristic` binds the function OBJECT at
# import time, so `monkeypatch.setattr(concept_tagging, "tag_nodes_heuristic", ...)` would stop
# reaching this module — a seam that worked while caller and callee shared one namespace, and one
# the suite still uses (`tests/test_retro_tag_persist.py` forces a CAS race through it). Same
# hazard CLAUDE.md records for `serve/scope_actions.py` importing store names by value. Types and
# the pure `_normalize_concept_id` wrapper are exempt: nothing patches those.
from looplab.search import concept_analytics, concept_tagging
from looplab.search.concept_graph import Concept, ConceptGraph, _normalize_concept_id


# --------------------------------------------------------------------------- #
# UNIVERSAL importance: derive the "winning region" per task (no hardcoded key list)
# --------------------------------------------------------------------------- #

def derive_reference_concepts(task_goal: str, coverage: dict, *, client, asset_brief: str = "",
                              parser: str = "tool_call", max_items: int = 10) -> list[dict]:
    """UNIVERSAL, task-agnostic 'important-but-uncovered' derivation — the per-RUN replacement for a
    hardcoded `key=True` winning region (which only a curated task pack like dense-retrieval has, and which
    literally encodes the answer for that one case). Given the task goal, the concepts ALREADY explored
    (from `coverage`), and optionally the repo's own prior-art brief (D1 `asset_brief`), ask the model which
    STANDARD high-value method families a strong researcher would try for THIS task that the run has NOT
    touched. Returns `[{concept_id, why}]` for the missing directions — grounded per task, zero domain
    hardcoding. This is what makes the uncovered-region alarm (§21.11) fire on ANY task, not just the one
    curated pack. Impure (one LLM call); best-effort (returns [] on any failure, so it never breaks a
    diagnostic). Grounding it in the D1 brief closes the loop the offline heuristic cannot: importance comes
    from the repo's own evidence + the model's task knowledge, not a maintainer's guess."""
    from pydantic import BaseModel, Field

    from looplab.core.parse import parse_structured

    class _Item(BaseModel):
        concept_id: str = ""
        why: str = ""

    class _Out(BaseModel):
        missing: list[_Item] = Field(default_factory=list)

    explored = sorted(coverage.get("concept_touch", {}) or {})
    system = (
        "You audit an ML research run for BLIND SPOTS. Given the TASK and the method-concepts already "
        "EXPLORED, list the most important method families / research directions for THIS task that are NOT "
        "yet explored — the standard high-value levers a strong researcher would reach for. Judge importance "
        "for the SPECIFIC task, not generically. Each item: `concept_id` as `axis/short-slug` (reuse an "
        "explored axis when one fits), plus a one-line `why`. Only genuinely IMPORTANT and genuinely "
        "UNCOVERED directions — omit anything already in the explored list. Call `emit` once with `missing`."
    )
    user = (f"TASK: {task_goal or '(unspecified)'}\n\nEXPLORED CONCEPTS ({len(explored)}):\n"
            + ("\n".join(f"- {c}" for c in explored) or "(none yet)"))
    if asset_brief:
        user += ("\n\nPRIOR ART in the repo (method families already known to matter for this task — a run "
                 "that has NOT touched these has a blind spot):\n" + asset_brief[:2000])
    try:
        out = parse_structured(client, [{"role": "system", "content": system},
                                        {"role": "user", "content": user}], _Out, parser)
    except Exception:  # noqa: BLE001 — best-effort: no importance signal beats crashing the diagnostic
        # ``[]`` is also a valid successful verdict. The live cadence durably snapshots this
        # failure sentinel and de-duplicates the projection, turning a transient outage into permanent
        # "no important blind spots." Return typed availability/provenance (or raise to the durable caller)
        # and persist empty only after a confirmed provider response parsed successfully.
        return []
    seen = set(explored)
    items: list[dict] = []
    for it in out.missing:
        cid = _normalize_concept_id(it.concept_id)
        if cid and cid not in seen:
            seen.add(cid)
            items.append({"concept_id": cid, "why": (it.why or "").strip()[:160]})
        if len(items) >= max_items:
            break
    return items


# --------------------------------------------------------------------------- #
# Vocabulary consolidation — keep a freely-grown graph from FRAGMENTING (§21.11 follow-up)
# --------------------------------------------------------------------------- #

def _apply_consolidation(graph: "ConceptGraph", tags: dict, rename: dict) -> tuple:
    """Rebuild `(graph, tags)` under an id->canonical-id `rename` map: merge concepts that collapse to the
    same canonical id (union their axes + key flag) and rewrite every node's tag set to canonical ids
    (deduped). Pure; identity when `rename` is empty.

    A RENAMED concept takes its axis from its OWN canonical id prefix — NOT a global axis-rename map, which
    is ambiguous when one source axis maps to several targets (`aug/crop→data/crop`, `aug/flip→vision/flip`)
    and would leave a concept whose id prefix disagrees with its stored axes, or silently rewrite a seeded
    axis placeholder's axis. A NON-renamed concept keeps its own axes verbatim (so a seeded axis never
    vanishes because a DIFFERENT concept was merged)."""
    if not rename:
        return graph, tags
    new = ConceptGraph(task_type=graph.task_type)
    for c in graph.concepts():
        cid = rename.get(c.id, c.id)
        # DELIBERATE (design-tension): a RENAMED concept takes its axis from the CANONICAL id's
        # prefix, NOT a union of source+canonical parents. This keeps id/axis CONSISTENT — a global
        # axis-rename is ambiguous when one source axis maps to several targets (`aug/crop→data/crop`,
        # `aug/flip→vision/flip`) and would leave a concept whose id prefix disagrees with its stored axes.
        # A renamed concept is a GROWN one (its parent is the id's IMMEDIATE prefix — one level up, at any
        # depth), so there are no curated multi-parent DAG parents to lose; a non-renamed concept keeps its
        # own axes verbatim (so a seeded multi-parent axis never vanishes because a DIFFERENT concept merged).
        axes = ((cid.rsplit("/", 1)[0],) if "/" in cid else (cid,)) if c.id in rename else c.axes
        existing = new.get(cid)
        merged_axes = tuple(dict.fromkeys((existing.axes if existing else ()) + tuple(axes)))
        merged_key = bool(c.key or (existing.key if existing else False))
        # Prefer the CANONICAL concept's own label (a curated skeleton label must not be overwritten by a
        # merged-away synonym's) — fall back to any label already accumulated, then this concept's.
        canon = graph.get(cid)
        label = (canon.label if canon is not None else (existing.label if existing else c.label))
        # Preserve the tagging VOCABULARY: the rebuilt canonical must carry the aliases of EVERY concept
        # merged into it (its own + each synonym's), or the heuristic tagger (`tag_text`/`tag_nodes_heuristic`)
        # would tag nothing on a consolidated graph (aliases default to `()` on a bare Concept). Merge, dedup.
        merged_aliases = tuple(dict.fromkeys((existing.aliases if existing else ()) + c.aliases))
        # ensure() keeps the first entry, so replace to carry the merged axes/aliases/key deterministically.
        new._concepts[cid] = Concept(id=cid, label=label, axes=merged_axes, aliases=merged_aliases,
                                     key=merged_key)
    # Materialize any missing INTERMEDIATE ancestors of a renamed DEEP id (mirrors ensure()'s chain-build).
    # A rename that DEEPENS an id (`aug/crop` -> `data/augmentation/crop`) would otherwise leave the new
    # levels (`data/augmentation`, `data`) absent, so children_of/tree projection would have gap nodes.
    for cid in list(new._concepts):
        if "/" in cid:
            parent = cid.rsplit("/", 1)[0]
            if parent not in new._concepts:
                new.ensure(parent)   # recursively builds every missing level up to the root
    new_tags = {nid: frozenset(rename.get(x, x) for x in cids) for nid, cids in (tags or {}).items()}
    return new, new_tags


def consolidate_concepts(graph: "ConceptGraph", tags: dict, *, client=None, embed=None,
                         parser: str = "tool_call", known_renames=None, prompts=None) -> tuple:
    """Consolidate a freely-GROWN concept vocabulary so it does not FRAGMENT into synonyms across a run
    (`augmentation` vs `data-augmentation`, `optimizer` vs `optimization`) — the §21.11 follow-up that makes
    the grown graph a STABLE coordinate system on any task. Returns `(graph, tags, rename_map)`.

    Agentic-first: with a `client`, one LLM call canonicalizes the vocabulary (merge synonymous
    concepts/axes to ONE id each; keep genuinely-distinct methods apart — `mixup` ≠ `cutmix`). Deterministic
    FALLBACK (no client): `hybrid_merge.cluster_near_duplicates` over the concept labels (recall-oriented RRF
    clustering) plus an axis-normalization pass; the canonical id per cluster is the shortest existing id.
    Fail-open: any error returns the graph/tags UNCHANGED (never loses information, never raises).

    STABLE / INCREMENTAL (§21.18 B3): `known_renames` (raw->canonical, recorded across cadences) are applied
    verbatim and are AUTHORITATIVE — a decided merge is NEVER re-decided, so the vocabulary stops flapping
    (LLM consolidation is nondeterministic). Only concepts not already covered (neither a known raw nor a
    known canonical) are sent to the model; when there is nothing new to decide, the LLM step is SKIPPED
    entirely. The returned map is the FULL resolved rename (known + new) for the caller to record."""
    known_renames = {str(k): str(v) for k, v in (known_renames or {}).items() if k and v}
    concepts = [c for c in graph.concepts() if not c.id.endswith("/*")]
    if len(concepts) < 2:
        # still honor already-decided renames even on a tiny vocab (keeps a resumed graph consistent)
        if known_renames:
            g2, t2 = _apply_consolidation(graph, tags, known_renames)
            return g2, t2, dict(known_renames)
        return graph, tags, {}
    ids = [c.id for c in concepts]
    rename: dict = dict(known_renames)   # start FIXED on the recorded decisions
    # Only concepts neither already renamed NOR a known canonical target need a fresh decision.
    decided = set(known_renames) | set(known_renames.values())
    undecided = [c for c in concepts if c.id not in decided]
    try:
        if not undecided:
            pass                          # nothing new to consolidate -> skip the LLM/heuristic entirely
        elif client is not None:
            from pydantic import BaseModel, Field

            from looplab.core.parse import parse_structured

            class _Pair(BaseModel):
                raw: str = ""
                canonical: str = ""

            class _Out(BaseModel):
                merges: list[_Pair] = Field(default_factory=list)

            vocab = "\n".join(f"- {c.id}  ({c.label})" for c in concepts)
            # Routed through the PromptStore like every other agent prompt in this codebase
            # (doc 25 SE-10). The DEFAULT is the shipped text byte-for-byte: this consolidation is a
            # different job from `hybrid_merge.agent_merge`'s generic item merge, so it keeps its own
            # prompt rather than adopting `merge_system`. Re-pointing it at `agent_merge` would have
            # swapped the text, which is a behaviour change for a paid agent, not a refactor.
            _CONSOLIDATE_SYSTEM = (
                "You consolidate a machine-learning experiment CONCEPT vocabulary that was grown "
                "incrementally and has SYNONYM fragmentation. Merge concepts/axes that mean the SAME thing to "
                "ONE canonical `axis/slug` id (e.g. `data-augmentation/*`≡`augmentation/*`, "
                "`optimizer/*`≡`optimization/*`). Keep genuinely-DIFFERENT methods separate (`mixup`≠`cutmix`; "
                "`teacher-distill`≠`self-distill`). Output ONLY the ids that should CHANGE, as {raw, canonical} "
                "pairs where `canonical` is another id from the list (or a cleaned form of it). Call `emit`."
            )
            from looplab.core.prompts import render
            system = render(prompts, "concept_consolidate_system", _CONSOLIDATE_SYSTEM)
            out = parse_structured(client, [{"role": "system", "content": system},
                                            {"role": "user", "content": "VOCABULARY:\n" + vocab}], _Out, parser)
            idset = set(ids)
            for p in out.merges:
                raw = _normalize_concept_id(p.raw)
                canon = _normalize_concept_id(p.canonical)
                # `raw not in decided`: a recorded decision is AUTHORITATIVE — freeze BOTH known raws AND
                # known canonicals (`decided` = keys ∪ values). Guarding only the keys would let the model
                # re-canonicalize a known canonical B->C, which `_final` then rewrites A->B into A->C — the
                # exact cross-cadence flap B3 exists to stop. New concepts (not in `decided`) still merge.
                if (raw and canon and raw != canon and raw in idset and "/" in canon
                        and raw not in decided):
                    rename[raw] = canon
        else:
            from looplab.search.hybrid_merge import cluster_near_duplicates
            labels = [f"{c.id} {c.label}" for c in concepts]
            for cluster in cluster_near_duplicates(labels, embed=embed):
                if len(cluster) < 2:
                    continue
                members = [ids[i] for i in cluster]
                canon = min(members, key=lambda s: (len(s), s))  # shortest id = canonical
                for m in members:
                    if m != canon and m not in decided:   # freeze known raws AND canonicals (see above)
                        rename[m] = canon
    except Exception:  # noqa: BLE001 — deriving NEW merges is best-effort; never break the diagnostic
        # A failure to derive new merges must NOT discard the AUTHORITATIVE recorded decisions (B3): still
        # apply + return `known_renames` so the vocabulary stays stable (raw ids don't resurrect). Empty
        # only when there were no known renames either.
        if known_renames:
            g2, t2 = _apply_consolidation(graph, tags, known_renames)
            return g2, t2, dict(known_renames)
        return graph, tags, {}

    # Resolve transitive chains (a->b, b->c => a->c) so the rename is a single canonical hop.
    def _final(x, _seen=None):
        _seen = _seen or set()
        while x in rename and x not in _seen:
            _seen.add(x)
            x = rename[x]
        return x
    # Drop identity entries: a rename CYCLE (a->b, b->a) resolves each id to itself (`_final` fail-safe),
    # and a self-rename would otherwise leak a bogus `a->a` "merge" into the reported map.
    rename = {k: v for k, v in ((k, _final(k)) for k in rename) if k != v}
    g2, t2 = _apply_consolidation(graph, tags, rename)
    return g2, t2, rename


# --------------------------------------------------------------------------- #
# The PRIMARY D5 entry: the LLM agent BUILDS the whole concept map (agentic-first)
# --------------------------------------------------------------------------- #

def build_concept_map(state: RunState, task_goal: str = "", *, client=None, tools=None,
                      seed_graph: Optional[ConceptGraph] = None, asset_brief: str = "",
                      parser: str = "tool_call", known_tags=None, known_renames=None,
                      max_workers: int = 8) -> dict:
    """THE primary D5 primitive: an LLM agent BUILDS the concept map for a run end-to-end — it GROWS the
    concept vocabulary from the actual experiments (`tag_nodes_llm`, agentic when read-only run `tools` are
    passed, so it reads each node's real code/logs), computes the pure coverage, and DERIVES the
    important-but-uncovered set per task (grounded in the optional D1 `asset_brief`). No hardcoded skeleton or
    `key=True` list is required — `seed_graph` is an OPTIONAL starting vocabulary (e.g. a curated pack for a
    known task type); the default is an EMPTY graph the LLM fills, so this works on ANY task/domain.

    This mirrors `asset_brief.agentic_asset_brief` being THE D1 primitive: the LLM AGENT is the builder, and
    the deterministic alias heuristic is only the no-LLM FALLBACK (used when `client is None`, and then a
    curated `seed_graph` is needed for it to localize anything). Returns
    `{graph, tags, raw_tags, raw_tag_modes, coverage, important_uncovered, mode}`. Impure (LLM) on the
    primary path; the coverage it returns is pure and fold-safe. In the live engine the built
    tags/graph/importance are recorded as events
    and read deterministically by `fold` (Phase 1/2 wiring) — this primitive is the producer, not the writer."""
    graph = seed_graph if seed_graph is not None else ConceptGraph(
        task_type=getattr(state, "task_id", "") or "")
    if client is None:
        # Deterministic fallback: alias heuristic over whatever seed vocabulary exists (empty -> nothing to
        # localize; a curated seed_graph is required for a useful offline map). No importance derivation.
        tags = concept_tagging.tag_nodes_heuristic(state, graph)
        return {"graph": graph, "tags": tags, "raw_tags": tags,
                "raw_tag_modes": {nid: "offline-heuristic" for nid in tags},
                "coverage": concept_analytics.concept_coverage(state, graph, tags),
                "important_uncovered": [], "mode": "offline-heuristic"}
    # `known_tags` lets a repeated cadence reuse already-recorded node tags and only LLM-tag NEW nodes.
    raw_tag_modes: dict[int, str] = {}
    raw = concept_tagging.tag_nodes_llm(state, graph, client, parser=parser, tools=tools,
                                        grow=True, known_tags=known_tags,
                                        max_workers=max_workers, producer_modes=raw_tag_modes)
    # CONSOLIDATE the freely-grown vocabulary before measuring, so synonym fragmentation
    # (`augmentation` vs `data-augmentation`) doesn't split the concentration signal (§21.11 follow-up).
    graph, tags, renamed = consolidate_concepts(graph, dict(raw), client=client, parser=parser,
                                                known_renames=known_renames)
    cov = concept_analytics.concept_coverage(state, graph, tags)
    important = derive_reference_concepts(task_goal or getattr(state, "goal", "") or "", cov,
                                          client=client, asset_brief=asset_brief, parser=parser)
    # `raw_tags` are the tagger's PRE-consolidation ids (stable per node) — the caller records THESE as
    # `node_concepts` events so a later cadence reuses them and re-derives consolidation/coverage cheaply.
    return {"graph": graph, "tags": tags, "raw_tags": raw, "raw_tag_modes": raw_tag_modes,
            "coverage": cov, "important_uncovered": important,
            "consolidated": renamed, "mode": "agentic" if tools is not None else "llm"}
