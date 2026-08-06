"""Concept graph — the shared coordinate system for the hypothesis/coverage space (PART IV D5, §21.11).

**Keystone A of the PART IV program.** The motivating baseline gave a node only a single flat
`idea.theme` slug, rolled up one-dimensionally by `theme_rollup`/`coverage.py`. The
`rubertlite` run proved that flat vocabulary is *blind to concentration*: dozens of hyper-narrow slugs
(`dcl-rdrop-ema`, `dcl-rdrop-gc`, …) all belong to ONE branch `loss → contrastive → DCL + R-Drop`, yet
the flat `dominant_theme_frac` the Strategist saw actually FELL 0.67→0.03 over the run — it reported an
increasingly *diverse* search while it collapsed onto one recipe (§21.10).

This module is the validated fix (§21.11): a **bipartite experiment↔concept graph** over a **concept
axis-DAG**. Each experiment carries a SET of concept tags; each concept sits under one or more parent
axes (a DAG, not a tree — `dcl-rdrop` is BOTH `loss/decoupled-contrastive` AND `regularization/r-drop`,
and forcing one parent is exactly what re-fragmented the signal, §21.10 refinement 1). Over that graph,
deterministic analytics surface three signals the flat vocabulary cannot:

  * **top-concept touch-fraction** — the single most-touched concept's share of TAGGED experiments;
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
Their outputs are part of the live system: durable node memberships feed Strategist coverage cues,
graded novelty and current Card scoring. The helpers remain pure even when their results steer those consumers.
The only impure step is *assigning* the multi-label tags: `tag_nodes_heuristic` is a deterministic,
alias-based (no-LLM) tagger that keys on primary-lever LINEAGE (all `dcl-*` → one family) so the signal
fires early; `tag_nodes_llm` is the richer optional harness that also GROWS the vocabulary. Both return
`{node_id: frozenset[concept_id]}` and feed the same pure analytics.

**Current scope.** The primitives in this module remain pure and never write domain events themselves,
but they are not offline-only: engine-owned producers persist bounded concept memberships and coverage
snapshots, and live consumers use the resulting graph for Strategist coverage cues, graded-novelty
admission, Card scoring, and capability-expansion guidance. Offline inspection calls the same pure
functions over a completed fold; event writing and policy gates stay in their owning engine modules.

**Module map (doc 25 SE-09).** This file was a 1,691-line god-module carrying five separable
responsibilities. It now keeps only the first — the vocabulary DATA STRUCTURE and the curated
skeletons — and the other four live in siblings that import it, never the other way round, so the
cluster is a DAG with this file at the bottom:

  * `concept_tagging.py` — the taggers (`tag_text`/`tag_text_llm`, `tag_nodes_heuristic`/
    `tag_nodes_llm` and its threaded parallel-batch harness, `graph_from_node_concepts`,
    `stale_tagged_nodes`) plus the two surfaces every tagger describes an experiment by,
    `experiment_nodes` and `node_text`;
  * `concept_analytics.py` — the pure analytics (`concept_coverage`, `concept_metrics`,
    `uncovered_regions`, `concept_report`);
  * `concept_lens.py` — the view projections the UI reads (`project_hierarchy`, `project_lens`,
    `derive_lens`, `default_lenses`, `concept_touch_counts`, `node_concept_delta`);
  * `concept_map.py` — LLM vocabulary consolidation, per-task importance, and the `build_concept_map`
    entry that drives all of the above.

There is deliberately NO re-export facade here. A `from looplab.search.concept_graph import
tag_text` that no longer resolves is an ImportError naming both ends; a re-export that still
resolves while `concept_analytics` calls its own bound copy is the silent monkeypatch no-op
CLAUDE.md's back-compat note warns about, and this cluster has live `monkeypatch.setattr` seams on
`tag_nodes_heuristic` and `build_concept_map`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from looplab.core.concepts import normalize_concept_id


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
    # DESIGN NOTE (2026-07-17 critique): a concept's parent is encoded TWICE — implicitly by the id prefix
    # (`a/b`->`a`) and explicitly by this `axes` tuple (cross-links). Keeping the two consistent is exactly
    # what produced review #10 (ancestor materialization), #11 (root self-parent) and #12 (axes_of vs
    # parents_of). Whenever two representations of the same fact must be kept in sync, expect drift.
    # Consider unifying: EITHER id-path is the only hierarchy (cross-links become a separate relates_to
    # edge, not an axis) OR the id is an opaque label and hierarchy lives entirely in parents.
    axes: tuple[str, ...] = ()              # parent axis ids — DAG multi-membership
    aliases: tuple[str, ...] = ()           # lowercase surface tokens for the heuristic tagger
    key: bool = False                       # part of a known/target winning region (alarm labelling)

    def __post_init__(self):
        # A concept with no explicit axis inherits its IMMEDIATE id-prefix as parent, so an arbitrarily
        # deep id (`loss/contrast/dcl/dclx`) sits one level under `loss/contrast/dcl`. A top-level id is
        # its own root. Skeletons always set axes explicitly (incl. cross-axis DAG membership).
        if not self.axes:
            parent = self.id.rsplit("/", 1)[0] if "/" in self.id else self.id
            object.__setattr__(self, "axes", (parent,))
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
        """Get-or-create a concept id (used by the LLM tagger when it proposes a new concept). A grown
        concept inherits its IMMEDIATE-prefix parent from the id unless one is given; `key` never upgrades
        an existing entry (only the skeleton declares winning regions).

        ARBITRARY DEPTH: a multi-level id materializes its whole ANCESTOR CHAIN as concepts, each linked to
        its immediate prefix — `ensure("loss/contrast/dcl/dclx")` also creates `loss/contrast/dcl`,
        `loss/contrast` and `loss`, so the DAG carries every intermediate level (§21.6 "give leaves parents",
        now unbounded). Cross-axis membership is still expressible by passing extra `axes`."""
        existing = self._concepts.get(concept_id)
        if existing is not None:
            return existing
        if "/" in concept_id:
            parent = concept_id.rsplit("/", 1)[0]
            if parent not in self._concepts:
                self.ensure(parent)             # recurse up to the root, materializing each level
            if not axes:
                axes = (parent,)
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
        """All distinct TOP-LEVEL roots (the `seg0` of every concept id / parent), sorted — the top of the
        DAG. Independent of hierarchy depth: `loss/contrast/dcl/dclx` still rolls up to the `loss` axis, so
        coverage grouping is unchanged whether the graph is flat or deep."""
        out: set[str] = set()
        for c in self._concepts.values():
            out.add(c.id.split("/", 1)[0])
            out.update(a.split("/", 1)[0] for a in c.axes)
        return sorted(out)

    def axes_of(self, concept_id: str) -> tuple[str, ...]:
        """The TOP-LEVEL root axis(es) a concept rolls up to (seg0 of the id and of any cross-link parent).
        Used by coverage to group any-depth concept to its top axis."""
        c = self._concepts.get(concept_id)
        if c is None:
            return (str(concept_id).split("/", 1)[0],)
        roots = {c.id.split("/", 1)[0]} | {a.split("/", 1)[0] for a in c.axes}
        return tuple(sorted(roots))

    # -- hierarchy (arbitrary-depth DAG) traversal ----------------------------
    def parents_of(self, concept_id: str) -> tuple[str, ...]:
        """Immediate parent concept ids (the DAG edges: id-prefix parent + any explicit cross-links).
        A top-level root carries itself in `axes` (so `axes()` sees it) but is NOT its own parent — the
        self-reference is filtered out here, matching `ancestors_of`/`descendants_of`."""
        c = self._concepts.get(concept_id)
        if c is None:
            return ()
        return tuple(p for p in c.axes if p != concept_id)

    def children_of(self, concept_id: str) -> list[str]:
        """Immediate children — concepts that name `concept_id` among their parents. Excludes the concept
        itself (a top-level root lists itself in `axes` but is not its own child)."""
        return sorted(c.id for c in self._concepts.values()
                      if concept_id in c.axes and c.id != concept_id)

    def ancestors_of(self, concept_id: str) -> list[str]:
        """All ancestors up every parent path to the roots (deduped, deterministic BFS order)."""
        seen: list[str] = []
        frontier = list(self.parents_of(concept_id))
        while frontier:
            p = frontier.pop(0)
            if p in seen or p == concept_id:
                continue
            seen.append(p)
            frontier.extend(self.parents_of(p))
        return seen

    def descendants_of(self, concept_id: str) -> list[str]:
        """All descendants down every child path (deduped, deterministic BFS order)."""
        seen: list[str] = []
        frontier = self.children_of(concept_id)
        while frontier:
            ch = frontier.pop(0)
            if ch in seen or ch == concept_id:
                continue
            seen.append(ch)
            frontier.extend(self.children_of(ch))
        return seen

    def depth_of(self, concept_id: str) -> int:
        """Longest root->concept path length (0 for a top-level root). Reflects the id nesting."""
        # CYCLE-GUARDED and MEMOIZED, like its `ancestors_of`/`descendants_of` siblings. Curated
        # cross-links are operator data, so a cycle between two concepts is reachable — `parents_of`
        # only drops the DIRECT self-reference — and the bare recursion ran to RecursionError on one.
        # A dense multi-parent DAG also re-walked shared ancestors exponentially. A node currently on
        # the stack contributes no depth (following it would be the cycle), so the answer stays the
        # longest ACYCLIC root path.
        # Cache the depth VALUE of any node whose subtree pruned NO self/cycle edge: such a node's longest
        # acyclic root-path is the same in every traversal context, so it is safe to reuse across the whole
        # walk — turning a dense diamond DAG from 2**depth re-walks into linear. A node whose subtree DID
        # prune a cycle edge stays uncached (its depth is context-bound), so cyclic graphs still terminate
        # and return the same depths as the bare recursion, and acyclic graphs are unchanged.
        memo: dict[str, int] = {}

        def _depth(cid: str, on_path: frozenset) -> tuple[int, bool]:
            # Returns (depth, clean); `clean` is True iff no self/cycle edge was pruned anywhere in cid's
            # subtree, i.e. the depth is context-independent and therefore cacheable.
            if cid in memo:
                return memo[cid], True
            depth, clean = 0, True
            for p in self.parents_of(cid):
                if p == cid or p in on_path:
                    clean = False            # pruned a cycle edge -> this subtree's depth is context-bound
                    continue
                d, child_clean = _depth(p, on_path | {cid})
                depth = max(depth, 1 + d)
                clean = clean and child_clean
            if clean:                        # only nodes proven off every cycle are safely cacheable
                memo[cid] = depth
            return depth, clean

        return _depth(concept_id, frozenset())[0]

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
# Concept identity — the one wrapper every module in this cluster shares
# --------------------------------------------------------------------------- #

def _normalize_concept_id(raw) -> str:
    # search analytics share the core bounded identity contract with replay and serve.
    return normalize_concept_id(raw) or ""
