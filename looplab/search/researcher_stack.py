"""The researcher-WRAPPER stack, composed by ONE rule (review 2026-09-22, SCJ-02).

The Researcher a run proposes with is rarely the role `make_roles` built. At launch it is wrapped in
up to two layers: the k-NN SURROGATE proposer (`surrogate.py`, under `surrogate_proposer` or
`policy=bohb`) and then EITHER the FOREAGENT foresight panel (`foresight.py`, on by default for the
LLM backend) OR the empirical k-NN panel (`panel.py`, `researcher_panel > 1`). Until this module
that stack was derived THREE times, by three different rules, and driven through the real
`cli/__init__.py::_engine` they disagreed:

* the LAUNCH (`_engine`) built the surrogate unconditionally — it learns its ranges from the run's
  own history, and the CLI's own comment called the old `if _bounds:` gate the defect;
* the mid-run BOHB switch (`engine/strategy.py::_ensure_surrogate`) still wrapped only `if bounds:`,
  and skipped on the raw `unified_agent` FLAG — which a toy/templated run carries by default (`True`)
  with two separate role objects, so it is not unified at all. Measured: on a dataset task (no
  declared bounds) and on every `--backend toy` run the launch rule wrapped and the switch did not,
  so a Strategist's `bohb` there was bare ASHA;
* the POOLED pairs (`orchestrator.py::_build_role_pairs`, the Layer-5 producer's lease) were the bare
  `make_roles` pair, so the speculative producer — AUTO-on for a greedy LLM run — proposed on an
  unwrapped role: under `surrogate_proposer` the primary proposed through the surrogate and the
  producer, one lane over, ignored it.

Now `wrap_researcher` composes the stack for the launch pair, `with_surrogate` is the surrogate
layer's own rule (which `wrap_researcher`, the mid-run switch and the pool all share), and
`pooled_researcher` states what a pooled pair carries.

WHICH LAYERS A POOLED PAIR CARRIES: the FREE ones, never the PAID ones.

* The two PANELS are paid, and wrapping a pooled researcher in either is a SPEND change no flag has
  authorised. The foresight panel asks the wrapped researcher `foresight_panel` (2) times and adds
  an idea-ranking call, plus a board-prioritisation call once two beliefs are open — at least three
  paid calls where the producer pays one — and the k-NN panel multiplies the proposal by
  `researcher_panel`. The producer's product is a PREFETCH the freshness gate discards whenever the
  board moves (the refund covers the node slot, never the proposal's tokens). And the receipt could
  not be kept: the raw-stage producer runs in a worker, where the board-wide `hypothesis_ranked` /
  `card_ranked` registers must not be appended, so `speculation.py::_prepare_raw_card_stage`
  discards role telemetry in its `finally` — a panel there would PAY for a ranking whose
  `foresight_selected` / `hypothesis_ranked` row is never written. Under the shipped
  `unified_agent=True` the foresight panel is the launch stack's ONLY layer (the free layers are
  unified-skipped, below), so there the pooled pair is the bare facade, exactly as before.
* The SURROGATE is free, and it is applied. Below its warm-up — and forever on a task whose params
  never carry numbers — it delegates the SAME single call to the wrapped researcher, with the SAME
  hints (`agents/roles.py::forward_hints` mirrors `RESEARCHER_HINT_ATTRS` onto it first), so the
  prompt is byte-identical. Past warm-up it proposes a numeric point and makes no call at all: a
  call REMOVED, which is the one direction CLAUDE.md exempts from a flag. The novelty gate still
  adjudicates each proposal once, as it does on the primary lane when the operator turned the
  surrogate on. `tests/test_researcher_stack.py` drives both halves.

A pooled surrogate draws from its OWN seed (`seed` below). The surrogate is deterministic given
(seed, history), so a second instance on the primary's seed re-proposes the primary lane's point
whenever the two lanes sit at the same draw over the same history — right at warm-up, typically —
and the two lanes would stop being independent proposers.

`pooled_researcher` decides off the PRIMARY's live chain rather than off `Settings`, deliberately: a
pooled pair outlives a Strategist switch, and a launch-time closure cannot follow the primary into
BOHB mid-run. The engine applies it to every pair it mints, and `_ensure_surrogate` re-applies it to
the pairs already minted when the switch lands.

Keep `forward_hints` semantics: the engine stamps hints on the OUTERMOST wrapper, whichever handle
that is, and every wrapper mirrors them inward. Nothing here stamps a hint.
"""
from __future__ import annotations

from looplab.core.evidence import envelope_enabled
from looplab.search.foresight import ForesightPanelResearcher
from looplab.search.panel import PanelResearcher
from looplab.search.surrogate import SurrogateResearcher

# The layers that multiply a proposal into several paid calls. A pooled researcher never carries
# one (module docstring); `tests/test_researcher_stack.py` subtracts exactly these from the primary's
# chain and requires the pooled chain to be what is left.
PAID_LAYERS: tuple[type, ...] = (ForesightPanelResearcher, PanelResearcher)


def researcher_chain(researcher) -> list:
    """Every link of a researcher's wrapper chain, OUTERMOST first.

    The links carry different names — panels hold `.base`, SurrogateResearcher `.fallback`, the
    roles/unified wrappers `.inner` — so the walk follows all three (`seen` guards the
    self-referential `inner` unified_agent.py builds)."""
    chain, seen, link = [], set(), researcher
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        chain.append(link)
        link = (getattr(link, "base", None) or getattr(link, "inner", None)
                or getattr(link, "fallback", None))
    return chain


def shares_one_agent(researcher, developer) -> bool:
    """Is the researcher the developer — the unified facade (R1), possibly behind a foresight proxy?

    Asked of the OBJECTS, because that is what R1 is about: re-wrapping `researcher` alone would
    leave `developer` on the old object, and the two handles would diverge mid-run. The raw
    `unified_agent` flag is not the same question — `make_roles` honours it only with
    `backend=llm`, so a toy/templated run carries `unified_agent=True` (the shipped default) with
    two separate role objects. For every pair the CLI builds the two answers agree exactly where
    the flag is honoured, which is why the launch keeps its behaviour."""
    return researcher is not None and researcher is developer


def surrogate_requested(settings) -> bool:
    """Does the launch ask for the surrogate proposer: `surrogate_proposer`, or `policy=bohb`."""
    return bool(settings.surrogate_proposer or settings.policy == "bohb")


def with_surrogate(researcher, developer, *, explore: float, seed: int = 0):
    """THE surrogate layer's one rule — at launch, on every pooled pair, and on a mid-run BOHB switch.

    Returns `researcher` itself when the layer does not apply — the unified facade (R1), or a chain
    that already holds a surrogate — so every caller can re-apply it idempotently.

    Constructed whether or not the TASK declares bounds. Only the built-in benchmarks declare them,
    so the old `if _bounds:` gate meant this setting silently did nothing on repo and dataset tasks —
    the ones real operators run — while the settings table said it was on. The wrapper self-gates:
    with no bounds and no usable history it delegates to the wrapped Researcher exactly as before,
    and once the run has enough evaluated numeric params it learns the ranges from them. Declared
    bounds are read off whichever link of the chain carries them."""
    if shares_one_agent(researcher, developer):
        return researcher
    # "if it isn't already" has to mean the WHOLE wrapper chain, not just the outermost handle.
    # The cli builds the surrogate FIRST and wraps a panel around it, so `researcher_panel > 1`
    # combined with `surrogate_proposer`/`policy=bohb` starts the run as `Panel(Surrogate(base))` —
    # and `_apply_strategy` calls the mid-run switch on EVERY strategy application whose policy is
    # bohb, including a params-only change to a run that was already bohb. An outermost-only
    # isinstance therefore re-wrapped into `Surrogate(Panel(Surrogate(base)))`, demoting the
    # operator's configured panel to the outer surrogate's bootstrap path.
    chain = researcher_chain(researcher)
    if any(isinstance(link, SurrogateResearcher) for link in chain):
        return researcher
    bounds = next((b for b in (getattr(link, "bounds", None) for link in chain) if b), None)
    return SurrogateResearcher(bounds or {}, fallback=researcher, explore=explore, seed=seed)


def pooled_researcher(primary, researcher, developer, *, explore: float, seed: int):
    """What a POOLED pair's researcher carries: the primary's FREE layers, none of its PAID ones.

    Decided off the primary's LIVE chain (see the module docstring for why not off `Settings`). The
    surrogate is today's only free layer; `seed` is the pooled instance's own draw stream and must
    differ from the primary's `0`."""
    if any(isinstance(link, SurrogateResearcher) for link in researcher_chain(primary)):
        return with_surrogate(researcher, developer, explore=explore, seed=seed)
    return researcher


def foresight_panel_applies(settings, researcher) -> bool:
    """Whether the FOREAGENT predict-before-execute panel should wrap this researcher.

    The two call sites spelled this guard out with ONE clause of difference — the non-unified branch
    also tested `backend == "llm"`, which the unified branch gets for free from `_unified` itself.
    Written twice with a difference, it read as though the two paths were checking different things
    (doc 25 CT-15); folding the clause in here is equivalent and says plainly that they are not.

    Yields to an explicitly-configured numeric `researcher_panel > 1`, so opting into the k-NN panel
    is never silently overridden by this default. Needs a client — a bare surrogate wrapper exposes
    none, and falls through.

    Moved here from `cli/__init__.py` (review 2026-09-22, SCJ-02) with the rest of the stack.
    """
    return bool(
        getattr(settings, "foresight", True)
        and settings.backend == "llm"
        and getattr(settings, "foresight_panel", 2) > 1
        and settings.researcher_panel <= 1
        and getattr(researcher, "client", None) is not None)


def with_foresight_panel(researcher, settings, tools):
    """Build the foresight panel around *researcher*. ONE constructor call, deliberately.

    Five getattr-defaulted kwargs written out in two branches is exactly the shape that grows a
    sixth in only one of them, and a drift there would silently change unified-vs-plain behaviour
    with nothing to catch it (doc 25 CT-15).
    """
    return ForesightPanelResearcher(
        researcher, k=settings.foresight_panel, tools=tools,
        min_confidence=getattr(settings, "foresight_min_confidence", 0.0),
        verify_score=getattr(settings, "foresight_verify", False),
        verify_samples=getattr(settings, "foresight_verify_samples", 3),
        # The agentic ranker's run tools return the candidates' own code: fenced when the run's
        # envelope is on (review 2026-09-22, TAT-02), through the ONE Settings reader.
        evidence_envelope=envelope_enabled(settings))


def wrap_researcher(researcher, developer, *, settings, tools=None):
    """Compose the LAUNCH researcher stack around the pair `make_roles` built.

    Returns `(researcher, developer)`: in unified mode the foresight panel wraps the ONE agent and
    both handles become the panel. `tools` is the agentic ranker's toolset (None -> the one-shot
    ranker). Pooled pairs do not come through here — `pooled_researcher` is their rule."""
    # Unified mode: researcher IS developer (one agent). Skip the researcher-only wrappers
    # (surrogate/panel) — they would re-wrap `researcher` without re-wrapping `developer`, so the
    # two handles would diverge mid-run (R1). The unified agent owns its own ideation machinery.
    if shares_one_agent(researcher, developer):
        # Foresight in UNIFIED mode: the wrappers below are skipped because they'd re-wrap only
        # the researcher handle, but ForesightPanelResearcher now DELEGATES its whole developer
        # surface to the wrapped agent (__getattr__), so wrapping the single unified agent and using
        # it for BOTH handles keeps them identical — predict-before-execute + hypothesis-board
        # prioritization work, implement/repair pass straight through. (Numeric surrogate/panel stay
        # researcher-only, so they remain unified-skipped; only the client-based foresight is safe
        # to share.)
        if foresight_panel_applies(settings, researcher):
            researcher = developer = with_foresight_panel(researcher, settings, tools)
        return researcher, developer
    # A2 surrogate-guided proposer: wrap the base Researcher (it bootstraps via the wrapped
    # Researcher and delegates on non-numeric spaces). A3 BOHB = ASHA racing + the surrogate, so
    # `policy=bohb` auto-enables it.
    if surrogate_requested(settings):
        researcher = with_surrogate(researcher, developer, explore=settings.surrogate_explore)
    # FOREAGENT predict-before-execute for HYPOTHESES: rank K candidate ideas with the LLM world
    # model primed with the data profile + experiment memory — it compares the structural / text
    # ideas the numeric surrogate can't. ON by default for the LLM backend.
    if foresight_panel_applies(settings, researcher):
        researcher = with_foresight_panel(researcher, settings, tools)
    # E2 researcher panel: generate K ideas and keep the best by the empirical surrogate.
    elif settings.researcher_panel > 1:
        researcher = PanelResearcher(researcher, k=settings.researcher_panel,
                                     explore=settings.surrogate_explore)
    return researcher, developer
