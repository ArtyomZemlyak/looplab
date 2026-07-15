"""Concept graph — the shared coordinate system for the hypothesis/coverage space (PART IV D5, §21.11).

**Keystone A of the PART IV program.** Today a node's only structural label is a single flat
`idea.theme` slug (`roles.py:31`), rolled up one-dimensionally by `theme_rollup`/`coverage.py`. The
`rubertlite` run proved that flat vocabulary is *blind to concentration*: dozens of hyper-narrow slugs
(`dcl-rdrop-ema`, `dcl-rdrop-gc`, …) all belong to ONE branch `loss → contrastive → DCL + R-Drop`, yet
the flat `dominant_theme_frac` the Strategist saw actually FELL 0.67→0.03 over the run — it reported an
increasingly *diverse* search while it collapsed onto one recipe (§21.10).

This module is the validated fix (§21.11): a **bipartite experiment↔concept graph** over a **concept
axis-DAG**. Each experiment carries a SET of concept tags; each concept sits under one or more parent
axes (a DAG, not a tree — `dcl-rdrop` is BOTH `loss/decoupled-contrastive` AND `regularization/r-drop`,
and forcing one parent is exactly what re-fragmented the signal, §21.10 refinement 1). Over that graph,
deterministic analytics surface three signals the flat vocabulary cannot:

  * **top-concept touch-fraction** — the single most-touched concept's share of experiments;
  * **dominant axis-clique share** — the most-common co-occurring AXIS pair's share (the run lived
    inside the tiny `loss × regularization` clique — 0 → 0.27);
  * **count of uncovered key concept-regions** — the decisive *uncovered winning-region* alarm: the
    proven-winning concepts (`negatives/external-mining`, `negatives/false-neg-handling`,
    `distillation/teacher-distill`, `data/*`) had `first_touch = None` across ALL 67 nodes. The graph
    reports that empty region as a STANDING alarm from the first node — earlier and more actionable
    than any concentration threshold (it does not wait for narrowing to accumulate).

Metric guidance (validated, §21.11): use the three signals above, NOT "distinct tag-set count" — the
latter stayed ~0.6 the whole run (each modifier mints a fresh exact set) and is too noisy to be an alarm.

**Discipline (mirrors `search/coverage.py`).** The analytics (`concept_coverage`, `uncovered_regions`,
`concept_report`) are PURE and deterministic over `(RunState, ConceptGraph, tags)` — no I/O, no LLM, no
wall-clock — so a replay recomputes them byte-identically and a historical log is re-measurable offline.
The only impure step is *assigning* the multi-label tags: `tag_nodes_heuristic` is a deterministic,
alias-based (no-LLM) tagger that keys on primary-lever LINEAGE (all `dcl-*` → one family) so the signal
fires early; `tag_nodes_llm` is the richer optional harness that also GROWS the vocabulary. Both return
`{node_id: frozenset[concept_id]}` and feed the same pure analytics.

**Phase 0 scope (early lane, §6.6 / §21.13).** This is an OFFLINE diagnostic — it reads a completed
run's folded state and reports; it does NOT write domain events or touch selection. The lock-in detector
(D7, 1a) and the live Strategist-pivot wiring (2a) read this graph but are separate later phases.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from itertools import combinations
from typing import Optional

from looplab.core.models import RunState


# --------------------------------------------------------------------------- #
# The concept vocabulary + axis-DAG
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Concept:
    """One node in the concept DAG. `axes` are its PARENT axes (one or more — the DAG's multi-parent
    edge set); `aliases` are the surface tokens that map an experiment's text onto this concept at
    LINEAGE granularity (all `dcl-*` modifiers share the one `dcl` family, so the signal keys on the
    primary lever, not the leaf slug — §21.10 refinement 1). `key=True` marks a "winning-region"
    concept: coverage is judged against these for the uncovered-region alarm, but the alarm itself
    reports EVERY uncovered skeleton region, not only the key ones."""
    id: str                                 # canonical, axis-prefixed, e.g. "negatives/external-mining"
    label: str = ""                         # human label (defaults to id)
    axes: tuple[str, ...] = ()              # parent axis ids — DAG multi-membership
    aliases: tuple[str, ...] = ()           # lowercase surface tokens for the heuristic tagger
    key: bool = False                       # part of a known/target winning region (alarm labelling)

    def __post_init__(self):
        # A concept with no explicit axis is its own axis root (defensive; skeletons always set axes).
        if not self.axes:
            object.__setattr__(self, "axes", (self.id.split("/", 1)[0],))
        if not self.label:
            object.__setattr__(self, "label", self.id)


class ConceptGraph:
    """A governed concept vocabulary over an axis-DAG. Seeded from a task-type skeleton and grown
    dynamically as new concepts appear (the fix for "themes are too narrow and too many" is not to
    forbid narrow leaves but to give them PARENTS — §21.6). Membership is many-to-many: an experiment
    tags a SET of concepts, and a concept sits under a SET of axes."""

    def __init__(self, concepts: Optional[list[Concept]] = None, *, task_type: str = ""):
        self.task_type = task_type
        self._concepts: dict[str, Concept] = {}
        for c in concepts or []:
            self.add(c)

    # -- construction / growth ------------------------------------------------
    def add(self, concept: Concept) -> Concept:
        """Register (or, if the id already exists, keep the original — the skeleton wins over a
        dynamically-grown duplicate so a governed key/axis assignment is never clobbered)."""
        return self._concepts.setdefault(concept.id, concept)

    def ensure(self, concept_id: str, *, axes: tuple[str, ...] = (), key: bool = False) -> Concept:
        """Get-or-create a concept id (used by the LLM tagger when it proposes a new concept). A
        grown concept inherits its axis from the id prefix unless one is given; `key` never upgrades an
        existing entry (only the skeleton declares winning regions)."""
        existing = self._concepts.get(concept_id)
        if existing is not None:
            return existing
        return self.add(Concept(id=concept_id, axes=axes, key=key))

    # -- read helpers ---------------------------------------------------------
    def __contains__(self, concept_id: str) -> bool:
        return concept_id in self._concepts

    def get(self, concept_id: str) -> Optional[Concept]:
        return self._concepts.get(concept_id)

    def concepts(self) -> list[Concept]:
        # Deterministic order (id-sorted) so every derived report/analytic is order-stable.
        return [self._concepts[k] for k in sorted(self._concepts)]

    def axes(self) -> list[str]:
        """All distinct parent axes in the graph, sorted — the top level of the DAG."""
        out: set[str] = set()
        for c in self._concepts.values():
            out.update(c.axes)
        return sorted(out)

    def axes_of(self, concept_id: str) -> tuple[str, ...]:
        c = self._concepts.get(concept_id)
        return c.axes if c is not None else (str(concept_id).split("/", 1)[0],)

    def key_concepts(self) -> list[str]:
        return [c.id for c in self.concepts() if c.key]


# --------------------------------------------------------------------------- #
# Task-type skeletons (the seed vocabulary)
# --------------------------------------------------------------------------- #
#
# The dense-retrieval skeleton is the one the `rubertlite` case validated (§21.6 axis list, extended by
# §21.11's `regularization`/`hyperparameter` axes that the DAG needs to express the `loss × regularization`
# clique). `key=True` marks the proven winning region the run never entered — so a replay of `rubertlite`
# fires the uncovered-region alarm on those exact concepts (the §21.11 decisive signal). Aliases are
# LINEAGE families (a modifier like `-ema`/`-gc`/`-swa` still maps to the family) so concentration reads
# the branch, not the leaf.

_DENSE_RETRIEVAL_CONCEPTS: list[Concept] = [
    # ---- loss ----
    Concept("loss/decoupled-contrastive", "Decoupled contrastive loss (DCL)", ("loss",),
            ("dcl", "decoupled contrastive", "decoupled-contrastive", "decoupled loss")),
    Concept("loss/contrastive", "Contrastive / InfoNCE loss", ("loss",),
            ("contrastive", "infonce", "info-nce", "nt-xent", "ntxent")),
    Concept("loss/mnr", "Multiple-negatives-ranking loss", ("loss",),
            ("mnr", "multiple negatives", "multiple-negatives", "multiple negative ranking")),
    Concept("loss/margin-mse", "Margin-MSE distillation loss", ("loss", "distillation"),
            ("margin-mse", "margin mse", "marginmse")),
    Concept("loss/listwise", "Listwise / KL ranking loss", ("loss",),
            ("listwise", "list-wise", "kl loss", "kl-divergence loss", "lambdaloss")),
    Concept("loss/triplet", "Triplet / hinge loss", ("loss",),
            ("triplet", "hinge loss", "margin ranking")),
    # ---- negatives ----
    Concept("negatives/in-batch", "In-batch / cross-batch negatives", ("negatives",),
            ("in-batch negative", "in batch negative", "batch negative", "cross-batch negative",
             "xbm", "memory bank", "gradient cache", "gradcache", "grad-cache")),
    # In-batch hard-negative selection (top-k / threshold on the batch similarity matrix) is a DISTINCT,
    # reachable-but-weak cousin of external mining — the run's node_37/58 lived here. Splitting it out keeps
    # the bare "hard negative mining" phrase (which the run used for its IN-BATCH threshold) OFF the key
    # external-mining concept, so the §21.11 uncovered-region alarm isn't silenced by an in-batch attempt
    # (§21.12 refinement: the granularity separating reachable-from-winning is load-bearing — the offline
    # heuristic over-tagged external-mining onto node_37/58 and falsely reported the winning region covered).
    Concept("negatives/hard-mining-inbatch", "In-batch hard-negative selection (top-k / threshold)",
            ("negatives",),
            ("hard negative mining", "hard-negative mining", "hard neg mining", "hard-neg mining",
             "top-k negative", "topk negative", "threshold negative", "in-batch hard", "mine the hardest")),
    # KEY: genuine EXTERNAL/offline mining only — aliases require an external qualifier (offline / ANN / BM25 /
    # corpus / cross-encoder-mined / "mine negatives"), NOT the bare "hard negative mining" the run used for
    # its in-batch threshold (that lands on `hard-mining-inbatch` above).
    Concept("negatives/external-mining", "External / offline hard-negative mining", ("negatives",),
            ("mined negative", "mined hard neg", "mine negatives", "mine hard negative", "external negative",
             "offline mining", "offline hard negative", "ann mining", "bm25 negative", "teacher-mined",
             "cross-encoder mined", "cross-encoder to mine", "retrieved negative", "corpus-mined",
             "index-mined", "faiss negative", "nv-retriever"), key=True),
    # KEY: DATA-SIDE false-negative filtering/masking only — NOT a mere mention of "false negatives" in a
    # loss-term rationale (node_63's loss-side debiasing was a different, failed implementation, not the
    # data-side direction §21.11 marks unused).
    Concept("negatives/false-neg-handling", "False-negative filtering / denoising", ("negatives",),
            ("false-negative filter", "false negative filter", "false-neg filter", "false-negative filtering",
             "false negative filtering", "false-negative mask", "false negative mask", "false-neg mask",
             "mask false negative", "nv-style", "positive-aware", "positive aware", "denoise negative",
             "denoised negative"), key=True),
    # ---- distillation ----
    # KEY: TEACHER / cross-encoder distillation only — bare "knowledge distillation" / "kd from" ALSO fire on
    # SELF-distillation (node_36) and are dropped, so the key concept reflects the unused external-teacher
    # lever, not the run's self-distill attempts.
    Concept("distillation/teacher-distill", "Cross-encoder / teacher distillation", ("distillation",),
            ("teacher distill", "teacher-distill", "cross-encoder distill", "distill from teacher",
             "distill from the teacher", "distill from a larger", "reranker distill", "teacher checkpoint",
             "margin-mse", "margin mse"), key=True),
    Concept("distillation/self-distill", "Self-distillation from own checkpoints", ("distillation",),
            ("self-distill", "self distill", "self-distillation", "ema teacher")),
    # ---- data ----
    Concept("data/augmentation", "Data augmentation", ("data",),
            ("augment", "augmentation", "back-translation", "backtranslation", "paraphrase",
             "cropping", "span deletion", "eda")),
    Concept("data/synthetic-queries", "Synthetic query / doc generation", ("data",),
            ("synthetic quer", "synthetic data", "generated quer", "query generation", "doc2query",
             "gpl", "pseudo-quer", "llm-generated quer"), key=True),
    Concept("data/curriculum", "Curriculum / sampling / dedup of data", ("data",),
            ("curriculum", "data sampling", "resampl", "dedup", "clean data", "data mixture")),
    # ---- architecture / pooling ----
    Concept("architecture/backbone", "Encoder backbone change", ("architecture",),
            ("backbone", "encoder swap", "bert-large", "roberta", "deberta", "bigger model",
             "model size", "layer count")),
    Concept("pooling/strategy", "Pooling strategy (mean/cls/last)", ("pooling", "architecture"),
            ("mean pooling", "cls pooling", "last-token pooling", "pooling strategy", "attention pooling")),
    Concept("architecture/matryoshka", "Matryoshka / dimensionality", ("architecture",),
            ("matryoshka", "mrl", "embedding dimension", "reduce dimension", "projection head")),
    # ---- regularization ----
    Concept("regularization/r-drop", "R-Drop consistency regularization", ("regularization",),
            ("r-drop", "rdrop", "r drop", "consistency regular")),
    Concept("regularization/ema", "EMA / weight averaging", ("regularization", "training-schedule"),
            ("ema", "exponential moving average", "swa", "weight averaging", "model averaging")),
    Concept("regularization/dropout", "Dropout / weight decay", ("regularization",),
            ("dropout", "weight decay", "l2 regular", "label smoothing")),
    # ---- hyperparameter ----
    Concept("hyperparameter/temperature", "Contrastive temperature", ("hyperparameter", "loss"),
            ("temperature", "tau", "logit scale", "logit-scale")),
    Concept("hyperparameter/batch-size", "Batch size / accumulation", ("hyperparameter",),
            ("batch size", "batch-size", "batchsize", "gradient accumulation", "large batch")),
    Concept("hyperparameter/learning-rate", "Learning rate / schedule", ("hyperparameter",
                                                                         "training-schedule"),
            ("learning rate", "learning-rate", "lr ", "lr=", "warmup", "cosine schedule",
             "scheduler")),
    # ---- training-schedule ----
    Concept("training-schedule/longer", "Longer / multi-stage training", ("training-schedule",),
            ("longer training", "more epoch", "multi-stage", "two-stage", "continue training",
             "extended training")),
    # ---- eval ----
    Concept("eval/metric-tuning", "Eval / retrieval-index tuning", ("eval",),
            ("recall@", "ndcg", "faiss", "index tuning", "retrieval eval", "rerank eval")),
]

# The axis skeleton — every axis that seeds an EMPTY column so the uncovered-region alarm can fire on an
# axis no concept was ever tagged under (e.g. a run that never touches `data` at all). Order = report order.
_DENSE_RETRIEVAL_AXES: tuple[str, ...] = (
    "data", "negatives", "loss", "distillation", "architecture", "pooling",
    "regularization", "hyperparameter", "training-schedule", "eval",
)


def dense_retrieval_skeleton() -> ConceptGraph:
    """The validated dense-retrieval concept skeleton (§21.6/§21.11)."""
    g = ConceptGraph(list(_DENSE_RETRIEVAL_CONCEPTS), task_type="dense-retrieval")
    # Seed the axis roots so an entirely-untouched axis still appears in the coverage frame. A synthetic
    # `<axis>/*` placeholder concept (never key, no aliases -> never heuristically tagged) anchors the
    # axis in `graph.axes()` even before any real concept under it is grown.
    for ax in _DENSE_RETRIEVAL_AXES:
        g.ensure(f"{ax}/*", axes=(ax,))
    return g


# Task-type -> skeleton builder. A generic (axis-only) skeleton is the fallback for task types without a
# curated vocabulary — the graph then grows entirely from the LLM tagger. Kept tiny and additive so new
# task types register one row (mirrors the adapters registry discipline).
_SKELETONS = {
    "dense-retrieval": dense_retrieval_skeleton,
}
# Fuzzy task-id -> registered-skeleton aliases (mirrors asset_brief._LEXICON_ALIASES), so a run whose
# task_id is e.g. "vectorizer" still resolves the dense-retrieval skeleton. Substring match, first hit.
_SKELETON_ALIASES = {
    "dense-retrieval": ("dense-retrieval", "dense_retrieval", "retrieval", "vectorizer", "embedding",
                        "sentence-transformer", "bi-encoder", "biencoder"),
}


def skeleton_for(task_type: str) -> ConceptGraph:
    """Build the seed graph for a task type; a generic empty-but-typed graph when none is curated. Fuzzy:
    an unregistered id is matched against known packs' aliases before falling back to generic."""
    t = (task_type or "").strip().lower()
    if t in _SKELETONS:
        return _SKELETONS[t]()
    for pack, aliases in _SKELETON_ALIASES.items():
        if t and any(a in t for a in aliases):
            return _SKELETONS[pack]()
    return ConceptGraph(task_type=task_type or "")


# --------------------------------------------------------------------------- #
# Tagging: experiment -> set of concept ids
# --------------------------------------------------------------------------- #

def _experiment_nodes(state: RunState) -> list:
    """Idea-carrying nodes in id order — the run's experiments, exactly as `coverage.py` counts them
    (failed nodes included: a failed experiment is still effort spent in a region)."""
    return sorted((n for n in state.nodes.values() if getattr(n, "idea", None) is not None),
                  key=lambda n: n.id)


def _node_text(node) -> str:
    """The searchable surface text for a node: theme + rationale + hypothesis + operator + param names.
    Lowercased. This is what the heuristic tagger and the LLM tagger both describe an experiment by."""
    idea = getattr(node, "idea", None)
    parts = [
        getattr(idea, "theme", "") or "",
        getattr(idea, "rationale", "") or "",
        getattr(idea, "hypothesis", "") or "",
        getattr(node, "operator", "") or "",
        " ".join(str(k) for k in (getattr(idea, "params", None) or {})),
        " ".join(str(k) for k in (getattr(idea, "space", None) or {})),
    ]
    return " ".join(parts).lower()


def _alias_index(graph: ConceptGraph, *, allow_plural: bool) -> list[tuple[object, str]]:
    """Pre-compiled (boundary-anchored alias regex, concept_id) pairs. The lookarounds are alnum-
    boundaries (not \\b) because aliases legitimately start/end with a hyphen (`r-drop`), where \\b is
    unreliable. `allow_plural` appends an optional trailing `s` (for natural-language text like lessons /
    hypotheses, so "false negatives" matches the "false negative" alias)."""
    import re as _re
    tail = r"s?(?![a-z0-9])" if allow_plural else r"(?![a-z0-9])"
    idx: list[tuple[object, str]] = []
    for c in graph.concepts():
        for a in c.aliases:
            a = (a or "").strip().lower()
            if a:
                # An alias ending in a NON-alnum char (`recall@`, `lr=`, `nv-`) was authored to sit in FRONT
                # of a value (`recall@100`, `lr=2e-5`); an alnum tail-boundary would forbid the very match it
                # exists for (the digit after `@`/`=` fails the lookahead), silently killing the alias. Only
                # anchor the tail when the alias ends in an alnum char.
                t = tail if a[-1].isalnum() else ""
                idx.append((_re.compile(r"(?<![a-z0-9])" + _re.escape(a) + t), c.id))
    return idx


def tag_text(text: str, graph: ConceptGraph, *, allow_plural: bool = False) -> frozenset[str]:
    """The single-source deterministic alias tagger for ONE piece of text — the SET of concepts whose
    aliases appear in it, on alnum boundaries (so `ema` does not fire inside `schema`, `dcl` not inside
    `include`). MULTI-label: text naming both a specific and a generic alias gets BOTH concepts. Used by
    the lesson guard, the idea grader, and the board dedup; the node tagger (`tag_nodes_heuristic`) shares
    the SAME rule via the underlying `_alias_index` (the true single-source seam — `tag_text` wraps it for
    single-text callers). `allow_plural` for natural-language callers."""
    low = (text or "").lower()
    return frozenset(cid for pat, cid in _alias_index(graph, allow_plural=allow_plural)
                     if pat.search(low))


def tag_text_llm(text: str, graph: ConceptGraph, client, *, parser: str = "tool_call",
                 allow_plural: bool = False) -> frozenset[str]:
    """AGENTIC single-TEXT tagger — the LLM counterpart of `tag_text`, shared by the F2 idea-grader and the
    HT hypothesis tagger. The LLM assigns the text the SET of concept ids from the graph's grown vocabulary
    (the SAME rule the node tagger uses, so texts are tagged CONSISTENTLY with the cached node tags), with
    `grow=False`: this text is a PROPOSAL/HYPOTHESIS, not an executed result, so it must NOT mint new
    vocabulary. Degrades to the deterministic `tag_text` on no client / any failure; RESPECTS an empty LLM
    verdict (the model naming nothing = 'fits no known concept', kept empty), but recovers via `tag_text`
    when the model named only UNKNOWN ids. Never raises, never blocks the caller."""
    if client is None:
        return tag_text(text, graph, allow_plural=allow_plural)
    try:
        from pydantic import BaseModel, Field

        from looplab.core.parse import parse_structured

        class TagOut(BaseModel):
            concept_ids: list[str] = Field(default_factory=list)

        known = [c for c in graph.concepts() if not c.id.endswith("/*")]
        # PROMPT CONTRACT (CLAUDE.md): this is a DELIBERATE generalization of F2's experiment-specific
        # tagging prompt so ONE tagger serves both proposed experiments (F2) and hypotheses (HT) — the
        # framing is "research item (proposed experiment or a hypothesis)". Tags shift only negligibly vs
        # the old idea-only wording, and this path is off-by-default + audit-only + can only flip a novelty
        # defer->allow (never a wrong reject), so the change is low-risk and intentional, not a cleanup.
        system = (
            "You tag a machine-learning research item (a proposed experiment or a hypothesis) with the "
            "CONCEPTS it touches, choosing ONLY from the KNOWN VOCABULARY below (do NOT invent ids — this is "
            "not an executed result). Assign every concept that applies (an item usually touches several). "
            "Key on the underlying METHOD/family, not the surface name. Call `emit` once with `concept_ids` "
            "(a subset of the known ids, possibly empty if none fits).\n\nKNOWN VOCABULARY:\n"
            + ("\n".join(f"- {c.id}: {c.label}" for c in known) or "(empty)"))
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": f"ITEM:\n{text}\n\nWhich KNOWN concepts does it touch? "
                                            "Emit their ids."}]
        out = parse_structured(client, msgs, TagOut, parser)
        raw_ids = list(out.concept_ids or [])
        keep = frozenset(cid for cid in (_normalize_concept_id(x) for x in raw_ids) if cid and cid in graph)
        if keep:
            return keep
        # named-only-unknowns -> recover a known alias; named-NOTHING -> respect the empty 'novel' verdict.
        return tag_text(text, graph, allow_plural=allow_plural) if raw_ids else frozenset()
    except Exception:  # noqa: BLE001 — agentic tagging is best-effort; never block the caller
        return tag_text(text, graph, allow_plural=allow_plural)


def tag_nodes_heuristic(state: RunState, graph: ConceptGraph) -> dict[int, frozenset[str]]:
    """Deterministic, no-LLM multi-label tagging by lineage-family alias match (pure — safe in replay
    and tests). Each experiment maps to the SET of concepts whose aliases appear in its text; a node
    matching no alias gets the empty set (tracked as `untagged` by the analytics — real effort not yet
    localized). Keys on lineage families, so all `dcl-*` variants land on the one `decoupled-contrastive`
    concept and concentration reads the branch (§21.10 refinement 1)."""
    index = _alias_index(graph, allow_plural=False)
    tags: dict[int, frozenset[str]] = {}
    for n in _experiment_nodes(state):
        low = _node_text(n)
        tags[n.id] = frozenset(cid for pat, cid in index if pat.search(low))
    return tags


def tag_nodes_llm(state: RunState, graph: ConceptGraph, client, *, parser: str = "tool_call",
                  grow: bool = True, tools=None, known_tags=None) -> dict[int, frozenset[str]]:
    """The PRIMARY (intelligent) tagger: ask the LLM to assign each experiment a SET of concept ids from
    the vocabulary — the §21.11 "multi-label tagging by deepseek" — proposing new ones when `grow` and
    GROWING the graph so it works on ANY task, not a hardcoded vocabulary. When read-only run `tools` are
    passed it runs AGENTIC (reads the node's actual code/logs before tagging, via `agentic_struct`,
    mirroring `verify_memo`); otherwise a plain structured call. The alias-based `tag_nodes_heuristic`
    is only the deterministic OFFLINE FALLBACK (used per-node when a call fails, and by tests). Best-
    effort and loop-safe — a failed node degrades to its heuristic tags, never crashing the harness.
    Impure by design (the LLM step); the analytics it feeds stay pure.

    INCREMENTAL (§21.16, Phase 2c): `known_tags` maps node_id -> already-known raw concept ids (from a
    prior cadence, recorded as `node_concepts` events). Those nodes are NOT re-sent to the LLM — their
    tags are reused and their concept ids re-`ensure`d into the graph — so a repeated strategist cadence
    only pays for the NEW nodes' tagging (~O(new) not ~O(all) LLM calls). A node's tags are stable, so
    reuse is exact; consolidation still runs afterwards over the merged set to normalize synonyms."""
    from pydantic import BaseModel, Field

    from looplab.core.parse import parse_structured

    class TagOut(BaseModel):
        concept_ids: list[str] = Field(default_factory=list)

    known_tags = known_tags or {}
    heuristic = tag_nodes_heuristic(state, graph)

    def _ensure_ids(ids) -> frozenset[str]:
        # Re-materialize a reused node's concepts into the graph WITHOUT an LLM call (mirrors the grow
        # branch below): a known id already in the graph is kept; a grown `axis/slug` id is re-ensured so
        # the graph rebuilt this cadence carries it. Ids that can't be placed are dropped (best-effort).
        got: set[str] = set()
        for raw in ids or ():
            cid = _normalize_concept_id(raw)
            if not cid:
                continue
            if cid in graph:
                got.add(cid)
            elif grow and "/" in cid:
                graph.ensure(cid, axes=(cid.split("/", 1)[0],))
                got.add(cid)
        return frozenset(got)
    # The prompt is TASK-AGNOSTIC: the domain vocabulary comes ONLY from the graph (KNOWN AXES / KNOWN
    # VOCABULARY), never hardcoded here — so the same tagger works on any task. The multi-touch guidance is
    # phrased with no domain example (a hardcoded dense-retrieval example would mislead the model on a
    # non-dense-retrieval run and leak a vocabulary the graph may not use).

    def _system() -> str:
        # REBUILT PER NODE from the CURRENT graph: as `grow=True` adds concepts for earlier nodes, later
        # nodes see them in KNOWN VOCABULARY and REUSE them instead of minting synonyms — fewer avoidable
        # duplicates for consolidation to clean up afterward.
        axes = graph.axes()
        return (
            "You tag a machine-learning experiment with the research CONCEPTS it touches, for a coverage "
            "map. Assign the SET of concepts that apply — an experiment usually touches SEVERAL at once "
            "(e.g. a change to the loss AND a regularizer), so tag EVERY concept that applies, not just the "
            "most obvious one. Prefer concepts from the KNOWN VOCABULARY below; only when none fits, propose "
            "a new id of the form `axis/short-slug` using one of the known AXES. Key on the underlying "
            "METHOD (its lineage/family), not the surface name — variants of one method that differ only by "
            "a modifier are the same concept. Call `emit` once with `concept_ids` (the list of ids)."
            f"\n\nKNOWN AXES: {', '.join(axes) or '(none — propose axis/slug ids)'}\n\nKNOWN VOCABULARY:\n"
            + ("\n".join(f"- {c.id}: {c.label}" for c in graph.concepts() if not c.id.endswith("/*"))
               or "(empty — this is a new task type; propose concept ids from scratch as `axis/slug`)")
        )
    tags: dict[int, frozenset[str]] = {}
    for n in _experiment_nodes(state):
        if n.id in known_tags:
            # REUSE a previously-recorded node's tags — no LLM call. Empty recorded tags fall back to the
            # node's heuristic tags (a recorded empty means "the tagger found nothing", still valid).
            reused = _ensure_ids(known_tags[n.id])
            tags[n.id] = reused if reused else heuristic.get(n.id, frozenset())
            continue
        desc = _describe_node(n)
        msgs = [{"role": "system", "content": _system()},   # rebuilt from the current (grown) vocabulary
                {"role": "user", "content": f"EXPERIMENT (node {n.id}):\n{desc}\n\n"
                                            "Which concepts does it touch? Read the node's code/logs "
                                            "first if a tool is available, then emit."}]
        try:
            if tools is not None:
                from looplab.agents.agent import agentic_struct
                out = agentic_struct(client, tools, msgs, TagOut, parser=parser,
                                     loop_opts={"max_turns": 8},
                                     fallback=lambda m: parse_structured(client, m, TagOut, parser))
            else:
                out = parse_structured(client, msgs, TagOut, parser)
            got: set[str] = set()
            for raw in out.concept_ids:
                cid = _normalize_concept_id(raw)
                if not cid:
                    continue
                if cid in graph:
                    got.add(cid)
                # A grown concept is single-axis by construction (the LLM proposes `axis/slug`, so its one
                # parent IS the id prefix); multi-parent DAG membership is for the CURATED skeleton concepts.
                elif grow and "/" in cid:
                    graph.ensure(cid, axes=(cid.split("/", 1)[0],))
                    got.add(cid)
            tags[n.id] = frozenset(got) if got else heuristic.get(n.id, frozenset())
        except Exception:  # noqa: BLE001 — degrade this node to heuristic, never crash the harness
            tags[n.id] = heuristic.get(n.id, frozenset())
    return tags


def graph_from_node_concepts(node_concepts, seed_graph: Optional["ConceptGraph"] = None):
    """DETERMINISTICALLY rebuild `(graph, tags)` from recorded `node_concepts` (the LLM tagger's raw ids,
    the Feature-1 §21.16 cache) — NO LLM: the model already assigned these ids, this only re-materializes the
    vocabulary so a later consumer (graded-novelty, coverage) reuses the AGENTIC tags without re-tagging.
    `seed_graph` optionally supplies curated multi-parent DAG structure; grown `axis/slug` ids are ensured
    under their prefix axis. Returns `(graph, {node_id: frozenset(concept_id)})`; ids that can't be placed
    are dropped (best-effort, never raises)."""
    graph = seed_graph if seed_graph is not None else ConceptGraph(task_type="")
    tags: dict[int, frozenset[str]] = {}
    for nid, ids in (node_concepts or {}).items():
        got: set[str] = set()
        for raw in ids or ():
            cid = _normalize_concept_id(raw)
            if not cid:
                continue
            if cid not in graph and "/" in cid:
                graph.ensure(cid, axes=(cid.split("/", 1)[0],))
            if cid in graph:
                got.add(cid)
        try:
            tags[int(nid)] = frozenset(got)
        except (TypeError, ValueError):  # a non-int node id in a malformed cache -> skip
            continue
    return graph, tags


def stale_tagged_nodes(node_ids, at_vocab: dict, *, growth: float = 0.7, cap: int = 20) -> list:
    """B1 (§21.18): pick the items (from `node_ids`) whose tags are STALE — made against a vocabulary
    smaller than `growth`× the LATEST recorded vocabulary size — so they should be re-tagged against the
    grown vocab. `at_vocab` maps id -> vocab-size-at-tag-time (missing -> 0, i.e. oldest, e.g. pre-B1
    events). Returns the `cap` MOST-stale ids (smallest at_vocab first, id as a deterministic tie-break).
    A strict no-op (empty) until the vocabulary has grown at all (max==0). Pure/deterministic.

    ASSUMES a roughly-monotonic vocabulary (the reference is `max(at_vocab)`): a re-tagged node records the
    latest size, so it converges (fresh next round; goes stale again only on >1/growth≈43% growth in one
    step — implausible). The one non-convergent corner is a PERSISTENT >43% regression below an earlier
    consolidation peak (consolidate_concepts is LLM-nondeterministic): then nodes below the stale peak
    re-tag every occurrence — but bounded to `cap`/cadence, gated once per new-node-count by the caller's
    at_node idempotence check, and the fold stays fully deterministic. A cost corner, not a correctness bug."""
    at_vocab = at_vocab or {}
    ids = list(node_ids or [])
    max_vocab = max((at_vocab.get(i, 0) for i in ids), default=0)
    if max_vocab <= 0:
        return []
    threshold = max_vocab * growth
    stale = [i for i in ids if at_vocab.get(i, 0) < threshold]
    stale.sort(key=lambda i: (at_vocab.get(i, 0), i))
    return stale[:cap]


def _normalize_concept_id(raw) -> str:
    s = str(raw or "").strip().lower().replace(" ", "-")
    return s.strip("/")


def _describe_node(node) -> str:
    """A compact, tagging-relevant description of an experiment for the LLM tagger."""
    idea = getattr(node, "idea", None)
    bits = [f"operator={getattr(node, 'operator', '')}"]
    if getattr(idea, "theme", None):
        bits.append(f"theme={idea.theme}")
    if getattr(idea, "hypothesis", None):
        bits.append(f"hypothesis={idea.hypothesis}")
    rat = " ".join((getattr(idea, "rationale", "") or "").split())
    if rat:
        bits.append(f"rationale={rat[:400]}")
    params = getattr(idea, "params", None) or {}
    if params:
        bits.append("params=" + ", ".join(sorted(str(k) for k in params))[:200])
    return " | ".join(bits)


# --------------------------------------------------------------------------- #
# Analytics (pure, deterministic over (state, graph, tags))
# --------------------------------------------------------------------------- #

def concept_coverage(state: RunState, graph: ConceptGraph,
                     tags: Optional[dict[int, frozenset[str]]] = None) -> dict:
    """The graph coverage read-model — the validated concentration signals (§21.11). Pure and
    deterministic; an empty run yields zeros. When `tags` is omitted, the deterministic heuristic tagger
    is used (so the diagnostic runs with no LLM).

    Keys:
      experiments        - idea-carrying nodes (run's experiments, `coverage.py` denominator)
      tagged             - experiments that received >=1 concept tag
      untagged           - experiments no concept matched (effort not yet localized)
      concepts_touched   - distinct concepts with >=1 touch
      axes_touched       - distinct axes with >=1 touch
      axes_total         - skeleton axes in the graph
      top_concept        - {id, count, frac}: the most-touched concept and its share of TAGGED experiments
      dominant_clique    - {axes:[a,b], count, frac}: the most-common co-occurring AXIS pair and its share
      uncovered_axes     - skeleton axes with 0 touches across the whole run
      uncovered_concepts - real (non-placeholder) skeleton concepts with first_touch == None
      uncovered_key      - KEY (winning-region) concepts uncovered — the standing alarm's payload
      axis_touch         - {axis: experiment-count} rollup (an experiment counts once per axis it touches)
      concept_touch      - {concept_id: experiment-count}
      first_touch        - {concept_id: 0-based experiment index of first touch} (touched concepts only)
    """
    nodes = _experiment_nodes(state)
    if tags is None:
        tags = tag_nodes_heuristic(state, graph)
    n = len(nodes)
    if n == 0:
        return _empty_coverage(graph)

    concept_touch: Counter = Counter()
    axis_touch: Counter = Counter()
    clique_pairs: Counter = Counter()
    first_touch: dict[str, int] = {}
    tagged = 0
    for idx, node in enumerate(nodes):
        cids = tags.get(node.id, frozenset())
        if cids:
            tagged += 1
        node_axes: set[str] = set()
        for cid in cids:
            concept_touch[cid] += 1
            first_touch.setdefault(cid, idx)
            node_axes.update(graph.axes_of(cid))
        for ax in node_axes:
            axis_touch[ax] += 1
        # An axis-clique is a co-occurring AXIS pair on ONE experiment (§21.11): the run lived inside the
        # `loss × regularization` clique. Count unordered pairs so the dominant clique is direction-free.
        for a, b in combinations(sorted(node_axes), 2):
            clique_pairs[(a, b)] += 1

    denom = tagged or n  # fraction over TAGGED experiments (untagged effort isn't ON a concept yet)
    # Deterministic argmax: highest count, ties broken by the SMALLEST key. `Counter.most_common` breaks
    # ties by insertion order — and the counters are filled by iterating each node's `frozenset` of tags,
    # whose order is PYTHONHASHSEED-randomized — so most_common(1) would make `top_concept`/`dominant_clique`
    # non-deterministic on a tie, violating the pure/replay-safe contract. Sorting on (-count, key) fixes it.
    top_cid, top_count = _argmax(concept_touch)
    clique, clique_count = _argmax(clique_pairs)

    all_axes = graph.axes()
    real_concepts = [c.id for c in graph.concepts() if not c.id.endswith("/*")]
    uncovered_axes = [ax for ax in all_axes if axis_touch.get(ax, 0) == 0]
    uncovered_concepts = [cid for cid in real_concepts if cid not in first_touch]
    uncovered_key = [cid for cid in graph.key_concepts() if cid not in first_touch]

    return {
        "experiments": n,
        "tagged": tagged,
        "untagged": n - tagged,
        "concepts_touched": len(concept_touch),
        "axes_touched": len(axis_touch),
        "axes_total": len(all_axes),
        "top_concept": ({"id": top_cid, "count": top_count, "frac": round(top_count / denom, 4)}
                        if top_cid else None),
        "dominant_clique": ({"axes": list(clique), "count": clique_count,
                             "frac": round(clique_count / denom, 4)} if clique else None),
        "uncovered_axes": uncovered_axes,
        "uncovered_concepts": uncovered_concepts,
        "uncovered_key": uncovered_key,
        "axis_touch": dict(sorted(axis_touch.items())),
        "concept_touch": dict(sorted(concept_touch.items())),
        "first_touch": dict(sorted(first_touch.items())),
    }


def _argmax(counter):
    """(key, count) of the max-count entry, ties broken by the smallest key — a DETERMINISTIC argmax
    (unlike `Counter.most_common`, whose tie order follows hash-seed-randomized insertion). (None, 0)
    when empty."""
    if not counter:
        return None, 0
    key = min(counter, key=lambda k: (-counter[k], k))
    return key, counter[key]


def _empty_coverage(graph: ConceptGraph) -> dict:
    all_axes = graph.axes()
    real = [c.id for c in graph.concepts() if not c.id.endswith("/*")]
    return {
        "experiments": 0, "tagged": 0, "untagged": 0, "concepts_touched": 0,
        "axes_touched": 0, "axes_total": len(all_axes),
        "top_concept": None, "dominant_clique": None,
        "uncovered_axes": all_axes, "uncovered_concepts": real,
        "uncovered_key": graph.key_concepts(),
        "axis_touch": {}, "concept_touch": {}, "first_touch": {},
    }


def uncovered_regions(state: RunState, graph: ConceptGraph,
                      tags: Optional[dict[int, frozenset[str]]] = None) -> dict:
    """The decisive *uncovered winning-region* alarm (§21.11) — the single most actionable PART IV
    output. Reports which skeleton regions the search footprint NEVER entered, from the first node, as a
    ready-to-use Strategist pivot directive ("you have 0 coverage in {X} — go there", not "broaden").
    Pure. `fired` is True whenever a KEY winning-region concept is uncovered (or, absent a curated key
    set, whenever an entire axis is untouched)."""
    cov = concept_coverage(state, graph, tags)
    key_uncovered = cov["uncovered_key"]
    axes_uncovered = cov["uncovered_axes"]
    has_key = bool(graph.key_concepts())
    fired = bool(key_uncovered) if has_key else bool(axes_uncovered)
    # The directive names concrete regions: prefer the labelled key concepts; else the empty axes.
    targets = key_uncovered or axes_uncovered
    directive = ""
    if fired and targets:
        directive = ("0 coverage in {" + ", ".join(targets[:6]) + "} across all "
                     f"{cov['experiments']} experiments — direct the next proposals there "
                     "(not just 'broaden').")
    return {
        "fired": fired,
        "experiments": cov["experiments"],
        "uncovered_key": key_uncovered,
        "uncovered_axes": axes_uncovered,
        "directive": directive,
    }


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
        # Renamed -> axis is the canonical id's prefix (id/axis consistent by construction). Not renamed ->
        # keep its own axes (grown concepts are single-axis; skeleton concepts carry their curated axes).
        axes = (cid.split("/", 1)[0],) if c.id in rename else c.axes
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
    new_tags = {nid: frozenset(rename.get(x, x) for x in cids) for nid, cids in (tags or {}).items()}
    return new, new_tags


def consolidate_concepts(graph: "ConceptGraph", tags: dict, *, client=None, embed=None,
                         parser: str = "tool_call", known_renames=None) -> tuple:
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
            system = (
                "You consolidate a machine-learning experiment CONCEPT vocabulary that was grown "
                "incrementally and has SYNONYM fragmentation. Merge concepts/axes that mean the SAME thing to "
                "ONE canonical `axis/slug` id (e.g. `data-augmentation/*`≡`augmentation/*`, "
                "`optimizer/*`≡`optimization/*`). Keep genuinely-DIFFERENT methods separate (`mixup`≠`cutmix`; "
                "`teacher-distill`≠`self-distill`). Output ONLY the ids that should CHANGE, as {raw, canonical} "
                "pairs where `canonical` is another id from the list (or a cleaned form of it). Call `emit`."
            )
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
    except Exception:  # noqa: BLE001 — consolidation is best-effort; never break the diagnostic
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
                      parser: str = "tool_call", known_tags=None, known_renames=None) -> dict:
    """THE primary D5 primitive: an LLM agent BUILDS the concept map for a run end-to-end — it GROWS the
    concept vocabulary from the actual experiments (`tag_nodes_llm`, agentic when read-only run `tools` are
    passed, so it reads each node's real code/logs), computes the pure coverage, and DERIVES the
    important-but-uncovered set per task (grounded in the optional D1 `asset_brief`). No hardcoded skeleton or
    `key=True` list is required — `seed_graph` is an OPTIONAL starting vocabulary (e.g. a curated pack for a
    known task type); the default is an EMPTY graph the LLM fills, so this works on ANY task/domain.

    This mirrors `asset_brief.agentic_asset_brief` being THE D1 primitive: the LLM AGENT is the builder, and
    the deterministic alias heuristic is only the no-LLM FALLBACK (used when `client is None`, and then a
    curated `seed_graph` is needed for it to localize anything). Returns
    `{graph, tags, coverage, important_uncovered, mode}`. Impure (LLM) on the primary path; the coverage it
    returns is pure and fold-safe. In the live engine the built tags/graph/importance are recorded as events
    and read deterministically by `fold` (Phase 1/2 wiring) — this primitive is the producer, not the writer."""
    graph = seed_graph if seed_graph is not None else ConceptGraph(
        task_type=getattr(state, "task_id", "") or "")
    if client is None:
        # Deterministic fallback: alias heuristic over whatever seed vocabulary exists (empty -> nothing to
        # localize; a curated seed_graph is required for a useful offline map). No importance derivation.
        tags = tag_nodes_heuristic(state, graph)
        return {"graph": graph, "tags": tags, "raw_tags": tags,
                "coverage": concept_coverage(state, graph, tags),
                "important_uncovered": [], "mode": "offline-heuristic"}
    # `known_tags` lets a repeated cadence reuse already-recorded node tags and only LLM-tag NEW nodes.
    raw = tag_nodes_llm(state, graph, client, parser=parser, tools=tools, grow=True, known_tags=known_tags)
    # CONSOLIDATE the freely-grown vocabulary before measuring, so synonym fragmentation
    # (`augmentation` vs `data-augmentation`) doesn't split the concentration signal (§21.11 follow-up).
    graph, tags, renamed = consolidate_concepts(graph, dict(raw), client=client, parser=parser,
                                                known_renames=known_renames)
    cov = concept_coverage(state, graph, tags)
    important = derive_reference_concepts(task_goal or getattr(state, "goal", "") or "", cov,
                                          client=client, asset_brief=asset_brief, parser=parser)
    # `raw_tags` are the tagger's PRE-consolidation ids (stable per node) — the caller records THESE as
    # `node_concepts` events so a later cadence reuses them and re-derives consolidation/coverage cheaply.
    return {"graph": graph, "tags": tags, "raw_tags": raw, "coverage": cov, "important_uncovered": important,
            "consolidated": renamed, "mode": "agentic" if tools is not None else "llm"}


# --------------------------------------------------------------------------- #
# Human-readable report (for the CLI diagnostic)
# --------------------------------------------------------------------------- #

def concept_report(state: RunState, graph: ConceptGraph,
                   tags: Optional[dict[int, frozenset[str]]] = None) -> str:
    """A compact text diagnostic over the concept graph — the offline CLI's output. Pure."""
    if tags is None:
        tags = tag_nodes_heuristic(state, graph)
    cov = concept_coverage(state, graph, tags)
    lines = [
        f"Concept-graph coverage  (task-type={graph.task_type or 'generic'})",
        f"  experiments: {cov['experiments']}  tagged: {cov['tagged']}  untagged: {cov['untagged']}",
        f"  concepts touched: {cov['concepts_touched']}   axes touched: "
        f"{cov['axes_touched']}/{cov['axes_total']}",
    ]
    tc = cov["top_concept"]
    if tc:
        lines.append(f"  top concept: {tc['id']}  touch-fraction={tc['frac']} ({tc['count']} exps)")
    dc = cov["dominant_clique"]
    if dc:
        lines.append(f"  dominant axis-clique: {dc['axes'][0]} × {dc['axes'][1]}  "
                     f"share={dc['frac']} ({dc['count']} exps)")
    if cov["axis_touch"]:
        lines.append("  per-axis touch: "
                     + ", ".join(f"{ax}={c}" for ax, c in cov["axis_touch"].items()))
    alarm = uncovered_regions(state, graph, tags)
    lines.append("")
    if alarm["fired"]:
        lines.append("  ⚠ UNCOVERED-REGION ALARM")
        lines.append("    " + alarm["directive"])
        if alarm["uncovered_key"]:
            lines.append("    uncovered key regions: " + ", ".join(alarm["uncovered_key"]))
        if alarm["uncovered_axes"]:
            lines.append("    entirely-untouched axes: " + ", ".join(alarm["uncovered_axes"]))
    else:
        # The alarm keys on the WINNING-region (`key`) concepts, so `fired` can be False (all key regions
        # covered) while whole non-key AXES are still untouched — don't claim "all regions covered" then.
        if alarm["uncovered_axes"]:
            lines.append("  uncovered-region alarm: key regions covered, but entirely-untouched axes remain: "
                         + ", ".join(alarm["uncovered_axes"]))
        else:
            lines.append("  uncovered-region alarm: (not fired — all key/axis regions have coverage)")
    return "\n".join(lines)
