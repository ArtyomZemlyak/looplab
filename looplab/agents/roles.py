"""Role backends (I5, ADR-7). `Researcher` proposes an Idea (params to try);
`Developer` turns an Idea into runnable code. Both are Protocols so an LLM-backed
or external-coding-agent backend drops in with zero orchestrator change.

The Toy* implementations make the P0 loop runnable fully offline (no API keys):
the Researcher is a blind seeded optimizer (random seeds, then hill-climbs around
the current best using only *observed* metrics); the Developer emits a script whose
executed objective is the ground truth the Researcher never sees. This exercises
the real loop (draft -> run -> evaluate -> improve -> select) deterministically.
"""
from __future__ import annotations

import random
from typing import Optional, Protocol

from looplab.core.models import Idea, Node, RunState
from looplab.core.parse import LLMClient, ParseError, extract_code, parse_structured
from looplab.core.prompts import PromptStore, render
from looplab.core.validate import AgentReport, validate_agent_code


def _attention_points() -> str:
    """Shared environment-awareness cues for the LLM roles (best-effort; never break role building)."""
    try:
        from looplab.core.hardware import operational_attention_points
        return operational_attention_points()
    except Exception:  # noqa: BLE001
        return ""

_RESEARCHER_CORE = ("You are an ML researcher proposing the next experiment as "
                    "parameters to try. Also set `theme`: a short, reusable lower-case slug "
                    "(e.g. \"loss-fn\", \"architecture\", \"regularization\", \"learning-rate\") "
                    "that groups this experiment with related ones — reuse the SAME slug across "
                    "experiments that explore the same idea. ")
# P6/P21 (docs/PROMPT_REVIEW.md): the intra-node sweep OFFER, shared VERBATIM by both researchers
# (`LLMResearcher` here and agent.py's `ToolUsingResearcher`) via `_researcher_capability_suffix`,
# and GATED on capability: only the in-house `LLMDeveloper` honors `idea.space` —
# `CliAgentDeveloper` and `LLMRepoDeveloper` never read it — so `make_roles` sets
# `offer_sweep=False` on those backends and this fragment is dropped rather than promising a
# sweep nobody will run (the engine would stretch the node by sweep_timeout_mult while waiting
# for a `trials` line that never comes).
_SWEEP_OFFER = ("Optionally, when a hyperparameter is cheap to vary and the task data loads "
                "fast, you MAY propose a SWEEP instead of a single point: set `space` to a "
                "small discrete grid {name: [values, ...]} (keep the total grid small, "
                "<= ~12 points; grid values must be NUMERIC — the schema rejects strings). "
                "The Developer then evaluates every grid point in ONE process "
                "(loading the data once), so a sweep is far cheaper than the same points run "
                "as separate nodes. Leave `space` empty for an ordinary single-config "
                "experiment; fixed/shared hyperparameters still go in `params`. ")
# P6: the per-experiment `eval_timeout` ask, shared by both researchers. Scoped HONESTLY: the
# engine consumes `idea.eval_timeout` only on the sandbox (script-solution) eval branch;
# repo/command-eval stages take their timeouts from the stage manifest / the task's cmd spec.
_EVAL_TIMEOUT_GUIDANCE = (
    "If THIS experiment is genuinely compute-heavy and needs more wall-clock than a "
    "light model — a neural network (CNN/RNN/transformer), a large ensemble, many CV "
    "folds/seeds, or a big grid — set `eval_timeout` to a realistic per-run budget in "
    "SECONDS (e.g. 300-1800). Leave it null for ordinary/light experiments so they use "
    "the run default. (`eval_timeout` applies to script-solution tasks run in the sandbox; "
    "on repo/command tasks the per-stage timeouts come from the stage manifest / the "
    "task's cmd, so leave it null there.) ")
# P14: the schema requires `operator` but the engine's policy overwrites it unconditionally
# (orchestrator's node-creation sites) — say so, in BOTH researcher prompts, so the model
# doesn't strategize around a dead field.
_OPERATOR_NOTE = ("The `operator` field is informational (an audit label): the engine's search "
                  "policy decides the node's actual operator. ")


def _researcher_capability_suffix(offer_sweep: bool) -> str:
    """P6: capability prose SHARED by both researchers (`LLMResearcher` here and agent.py's
    `ToolUsingResearcher`) so the two role variants can't drift apart again: the sweep offer
    (only when the active Developer implements `idea.space` — `make_roles` decides, see
    `_SWEEP_OFFER`) + the `eval_timeout` ask."""
    return (_SWEEP_OFFER if offer_sweep else "") + _EVAL_TIMEOUT_GUIDANCE


def _researcher_system(offer_sweep: bool = True) -> str:
    """Assemble the plain researcher's FULL system prompt (core + capability suffix + operator
    note + emit instruction) — a back-compat/reference assembly. The `researcher_system`
    PromptStore default is `_RESEARCHER_CORE` ALONE: `LLMResearcher.propose` appends the
    capability fragments AFTER the render() (the same pattern as agent.py's
    `ToolUsingResearcher`), so the composed prompt stays byte-equal to this helper while a
    `researcher_system.md` override can never bypass the code-owned `offer_sweep` gate. With
    `offer_sweep=True` this matches the historical `_RESEARCHER_SYSTEM` modulo the verified
    prompt fixes (P21 numeric-grid note, P6 eval_timeout scoping, P14 operator note)."""
    return (_RESEARCHER_CORE + _researcher_capability_suffix(offer_sweep) + _OPERATOR_NOTE +
            "Respond ONLY with the requested structured fields.")


# Appended to the Researcher system prompt when hypothesis tracking is on (P1, default on). Split out
# so the knob can drop it cleanly (the `hypothesis` field then simply stays unset).
_HYPOTHESIS_INSTRUCTION = (
    "Set `hypothesis`: ONE plain-sentence statement of what this experiment TESTS — the belief you "
    "expect the result to support or refute (e.g. \"adding interaction features raises CV accuracy\", "
    "\"a deeper tree overfits this small dataset\"). Reuse the SAME wording when a later experiment "
    "tests the same belief, so the run builds a ledger of what's been learned.")


def _hypothesis_system_suffix(track_hypotheses: bool) -> str:
    """The system-prompt tail that asks for the per-experiment `hypothesis` (P1), or "" when the
    knob is off. Shared VERBATIM by BOTH researchers (`LLMResearcher` here and agent.py's
    `ToolUsingResearcher`) so the `"\\n" + _HYPOTHESIS_INSTRUCTION` splice lives in ONE place."""
    return ("\n" + _HYPOTHESIS_INSTRUCTION) if track_hypotheses else ""


# The "your idea space is the WHOLE experiment / the Developer owns HOW" guidance, as worded for
# LLMResearcher's per-turn USER message (it follows the rationale ask). A SECOND, deliberately
# DIFFERENT wording lives in agent.py's `ToolUsingResearcher._IDEA_SPACE_TOOL` (a system prompt).
# The two are NOT normalized — prompt strings are contracts and the phrasings have drifted — but
# both are named `_IDEA_SPACE_*` so `grep _IDEA_SPACE` surfaces the pair despite the byte drift.
_IDEA_SPACE_PLAIN = ("Your idea space is the whole "
                     "experiment: propose a parameter change OR a structural one "
                     "(architecture, loss, data, training) when that's the stronger "
                     "move — describe non-numeric changes in the rationale. You do not "
                     "write the code yourself (the Developer owns how, and may edit the "
                     "code to realise it), but you ARE free to direct code-level changes.")
_DEVELOPER_SYSTEM = ("You are an expert ML engineer. Output ONLY a single fenced "
                     "```python``` block containing a complete, self-contained script. "
                     # 1.3 consistent evaluation: every candidate must be measured on the SAME
                     # splits/seeds or their scores are incomparable noise (AIRA2: much apparent
                     # 'validation overfitting' was evaluation inconsistency). The engine varies
                     # the env var only in the confirm/holdout phases.
                     "Seed ALL randomness (train/validation splits, CV folds, model init, "
                     "subsampling) from int(os.environ.get('LOOPLAB_EVAL_SEED', '0')) so every "
                     "evaluation is reproducible and comparable across candidates. ")
# Appended to the Developer's system prompt when the Idea carries a `space` (intra-node sweep).
_SWEEP_CONTRACT = (
    "\nThis is an INTRA-NODE SWEEP: evaluate EVERY point of the given grid in ONE process — load "
    "the data ONCE and reuse it across all grid points. Report ALL results by printing, as the "
    "FINAL stdout line, a JSON object: {\"trials\": [{\"params\": {..}, \"metric\": <float>, "
    "\"seconds\": <float>, \"extra_metrics\": {..}}, ...]} — one entry per grid point. IF the "
    "`looplab` package is importable in the eval environment, the easiest way is "
    "`from looplab.sweep import run_sweep` and call run_sweep(space, train_fn) where "
    "train_fn(params, seed) returns the metric (it prints the required line for you); if it is "
    "NOT importable (a bare sandbox image), write the loop yourself — load the data ONCE, then "
    "iterate the grid — or use Optuna/GridSearchCV, always printing that exact final JSON "
    "`trials` line. If the task is host-graded (it asks you to write predictions/submission), "
    "write them for the SINGLE BEST grid point so the host can grade it.")


class Researcher(Protocol):
    def propose(self, state: RunState, parent: Optional[Node]) -> Idea: ...


class Developer(Protocol):
    def implement(self, idea: Idea) -> str: ...


RESEARCHER_HINT_ATTRS: tuple[str, ...] = (
    "_digest_cap", "_complexity_hint", "_sweep_hint", "_novelty_feedback", "_novelty_hint",
    "_novelty_stance", "_hyp_order")
"""Ephemeral hint attributes communicated to the ACTIVE Researcher via `setattr` and consumed
with `getattr(obj, name, default)`. Writers: the engine (orchestrator.py — `_digest_cap` in
`__init__`, `_complexity_hint`/`_sweep_hint` in `_set_complexity_hint`, `_novelty_hint` +
`_novelty_stance` in `_stamp_novelty_hint`, `_novelty_feedback` in the novelty gate) and the
foresight panel (search/foresight.py `_prioritize_board` sets `_hyp_order` — the predicted
best-first board order — on its wrapped researcher). Readers: `LLMResearcher.propose` (below)
and agent.py's `ToolUsingResearcher.propose` read the text cues and thread `_hyp_order` into
`_state_brief`; the foresight ranker reads `_novelty_stance` (the stance VALUE behind the
`_novelty_hint` prose).

THIS TUPLE IS THE DELIVERY CONTRACT (P2, docs/PROMPT_REVIEW.md): the engine setattrs hints on
the OUTERMOST active researcher, and EVERY wrapper that delegates propose() mirrors ONLY this
registry (plus the non-hint `track_hypotheses` knob) onto its delegate. The forwarding wrappers:
the foresight panel (`search/foresight.py::ForesightPanelResearcher._forward_hints`), the
`UnifiedAgent` facade (`agents/unified_agent.py::UnifiedAgent.propose`), the surrogate wrapper
(`search/surrogate.py::SurrogateResearcher.propose`), and the empirical panel
(`serve/panel.py::PanelResearcher.propose`). An attribute missing here silently dies at the
first wrapper — exactly how board prioritization was dead in the default config. Keep it in
sync with every `setattr(self.researcher, "...")` / `setattr(self.base, "...")` site;
tests/test_hint_forwarding.py scans those sites AND wires the real wrapper chains to enforce it.

Both researchers honor the same cues: `LLMResearcher.propose` and `ToolUsingResearcher.propose`
fold the same `(_complexity_hint, _sweep_hint, _novelty_feedback, _novelty_hint)` cue set into
their prompts (`_digest_cap` is consumed separately as a numeric cap; `_hyp_order` orders the
open-hypothesis board inside `_state_brief`)."""


def forward_hints(src, dst) -> None:
    """Mirror the engine-set ephemeral hints from a wrapper onto its delegate — the ONE owner of
    the `(*RESEARCHER_HINT_ATTRS, "track_hypotheses")` forwarding rule every wrapper shares.

    P2 delivery contract (see RESEARCHER_HINT_ATTRS above): the engine setattrs hints on the
    OUTERMOST active researcher — which may be any of the forwarding wrappers — so each wrapper
    mirrors the registry (plus the non-hint `track_hypotheses` knob, likewise poked onto the
    outermost object; an explicit OFF must not be shadowed) onto its delegate before delegating
    propose(). hasattr-guarded: an attr the engine never set is left untouched on `dst`. Without
    this, a wrapper silently dropped every engine hint on its delegation path. Callers:
    `UnifiedAgent.propose`, `ForesightPanelResearcher._forward_hints`,
    `SurrogateResearcher.propose`, and serve's `PanelResearcher.propose` — one helper, so the
    rule can't drift per-wrapper (tests/test_hint_forwarding.py wires the real chains)."""
    for attr in (*RESEARCHER_HINT_ATTRS, "track_hypotheses"):
        if hasattr(src, attr):
            setattr(dst, attr, getattr(src, attr))


def collect_hint_cues(obj, attrs) -> str:
    """Concatenate the given engine-set hint attributes (a subset of
    `RESEARCHER_HINT_ATTRS`) off `obj` in order, each defaulting to "" when unset — the
    shared rendering pattern the Researcher prompts use. Purely mechanical: byte-identical
    to the per-attribute `getattr(obj, name, "")` concatenation it replaces."""
    return "".join(getattr(obj, name, "") for name in attrs)


# --------------------------------------------------------------------------- #
# Toy backends (offline, deterministic given a seed)
# --------------------------------------------------------------------------- #

_OBJECTIVE_TEMPLATE = '''\
import json, os, random
# Generated solution. The objective below is the toy "ground truth" the
# Researcher optimizes blindly via observed metrics only.
x = {x}
y = {y}
loss = (x - 3.0) ** 2 + (y + 1.0) ** 2
noise = {noise}
if noise:
    # Seeded eval noise: lets the multi-seed confirmation phase (I12) measure
    # variance. LOOPLAB_EVAL_SEED is unset (-> "0") during normal evaluation, so
    # search stays deterministic; the confirm phase varies it across seeds.
    rng = random.Random(int(os.environ.get("LOOPLAB_EVAL_SEED", "0")))
    loss += rng.gauss(0.0, noise)
print(json.dumps({{"metric": loss}}))
'''


class ToyResearcher:
    """Blind seeded optimizer: random seeds, then Gaussian hill-climb around best."""

    def __init__(self, bounds: dict[str, tuple[float, float]], seed: int = 0, step: float = 1.0):
        self.bounds = bounds
        self.step = step
        self.rng = random.Random(seed)

    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        keys = list(self.bounds)
        if parent is None:
            params = {k: round(self.rng.uniform(*self.bounds[k]), 4) for k in keys}
            return Idea(operator="draft", params=params, rationale="random seed point")
        params = {}
        for k in keys:
            lo, hi = self.bounds[k]
            v = parent.idea.params.get(k, 0.0) + self.rng.gauss(0.0, self.step)
            params[k] = round(max(lo, min(hi, v)), 4)
        return Idea(operator="improve", params=params,
                    rationale=f"perturb best node {parent.id} (metric={parent.metric})")


class ToyObjectiveDeveloper:
    """Renders an Idea's params into a runnable script (the objective is fixed here).
    `noise` (>0) injects seeded eval noise so the confirmation phase has variance to
    measure; 0 (default) keeps the objective deterministic."""

    def __init__(self, noise: float = 0.0):
        self.noise = noise

    def implement(self, idea: Idea) -> str:
        return _OBJECTIVE_TEMPLATE.format(
            x=idea.params.get("x", 0.0),
            y=idea.params.get("y", 0.0),
            noise=self.noise,
        )


# --------------------------------------------------------------------------- #
# LLM-backed backends (I2, ADR-7/14). Same Protocols; swap-in needs no loop change.
# Tested against a fake LLMClient (no live calls); go-live needs a model endpoint.
# --------------------------------------------------------------------------- #


def _clamp_fill(idea: Idea, bounds: Optional[dict]) -> Idea:
    """Clamp numeric params into bounds and fill any missing ones with the midpoint, so
    a stray/empty proposal can't crash the objective."""
    if bounds:
        for k, (lo, hi) in bounds.items():
            if k in idea.params:
                idea.params[k] = max(lo, min(hi, float(idea.params[k])))
            else:
                idea.params[k] = (lo + hi) / 2.0
    return idea


def _state_brief(state: RunState, parent: Optional[Node], digest_cap: int = 0,
                 hyp_order: Optional[list[str]] = None) -> str:
    best = state.best()
    lines = [f"Goal: {state.goal}", f"Optimize direction: {state.direction}."]
    if best is not None:
        lines.append(f"Best so far: node {best.id} metric={best.metric} params={best.idea.params}")
    if parent is not None:
        lines.append(f"Refine from node {parent.id}: params={parent.idea.params} metric={parent.metric}")
    # Append the always-on "working set": a compact view of the whole search (top winners, weakest /
    # failures, theme map) so the Researcher proposes with awareness of what's already been tried,
    # not just `best` + `parent`. Depth (full experiments, code, data) lives behind the run tools.
    from looplab.events.digest import experiments_digest, lineage_lessons, sibling_digest
    lines.append(experiments_digest(state, char_cap=digest_cap))
    # M1/A0c operator-scoped memory: draft/improve additionally see their SIBLINGS (diversity
    # pressure — aira-dojo MEM_OPS `sibling`) and, when refining, the LESSONS distilled from the
    # lineage under the refined node (D6 insight backpropagation, Arbor's Backpropagate step).
    lines.append(sibling_digest(state, parent))
    lines.append(lineage_lessons(state, parent))
    # P1: surface OPEN board hypotheses (human "+ Add" / deep-research directions) verbatim.
    # Without this the Researcher never sees them, and evidence only links when an experiment's
    # `hypothesis` matches the statement exactly — so board cards would stay "open" forever.
    open_hyps = [h for h in (state.hypotheses or {}).values()
                 if h.status == "open" and h.evidence == []]
    if open_hyps:
        # FOREAGENT predict-before-execute (search/foresight.py): when the world model has ranked the
        # board by expected payoff (`hyp_order` = hypothesis ids best-first), surface the batch of
        # untested beliefs — which arrives from deep research / a human / the strategist — in that
        # predicted-value order, so the search tests the most promising one first and the [:5] cap now
        # drops the LOWEST-payoff cards, not arbitrary insertion-order ones. No ranking -> insertion
        # order (unchanged). Replay-safe: only the resulting node's `idea.hypothesis` is recorded.
        if hyp_order:
            pos = {hid: i for i, hid in enumerate(hyp_order)}
            open_hyps.sort(key=lambda h: pos.get(h.id, len(hyp_order)))
        lines.append("Untested hypotheses on the board (registered by the operator or deep research"
                     + (", ordered by predicted payoff — best first" if hyp_order else "")
                     + " — none has evidence yet):")
        lines.extend(f'- "{" ".join(h.statement.split())[:200]}"' for h in open_hyps[:5])
        lines.append("If your next experiment tests one of these, copy its statement EXACTLY "
                     "(verbatim, unchanged wording) into `hypothesis` so the evidence links to "
                     "the board card.")
    return "\n".join(l for l in lines if l)


class LLMResearcher:
    """Proposes an `Idea` via structured output (tool_call default, baml fallback).

    `space_hint` describes the task's parameter space in the prompt; `bounds` clamps
    (and fills missing) numeric params so a small model's stray proposal can't crash
    the objective — quality robustness, not a correctness crutch."""

    def __init__(self, client: LLMClient, space_hint: str = "",
                 bounds: Optional[dict] = None, parser: str = "tool_call",
                 prompts: Optional[PromptStore] = None, track_hypotheses: bool = True,
                 offer_sweep: bool = True):
        self.client = client
        self.space_hint = space_hint
        self.bounds = bounds
        self.parser = parser
        self.prompts = prompts
        self.track_hypotheses = track_hypotheses   # P1: ask for the per-experiment hypothesis (default on)
        # P6: offer the intra-node sweep only when the active Developer implements `idea.space`
        # (make_roles sets this post-construction; default True keeps direct constructions as-is).
        self.offer_sweep = offer_sweep

    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        # Operator steering (Phase 5 `hint` control events): fold them into the prompt so a live
        # human can nudge the search ("try higher degree", "focus on regularization"). Advisory —
        # the model still proposes; bounds still clamp.
        from looplab.agents.hints import render_hint_directives
        hint_block = render_hint_directives(state.pending_hints)
        # Engine-set hint cues (see RESEARCHER_HINT_ATTRS; each empty when off), in order:
        # - _complexity_hint — A0d: an engine-set complexity cue keyed on the operated node's breadth.
        # - _sweep_hint — Strategist `prefer_sweep` bias: nudges — but never forces — the Researcher
        #   toward an intra-node sweep when the cost model favors in-process execution.
        # - _novelty_feedback — T5 novelty-gate feedback (one re-propose): "you already tried X, it
        #   failed because Y — propose something meaningfully different". Empty in the normal path.
        # - _novelty_hint — slice 2/4: the Strategist's novelty stance directive + coverage gaps
        #   (EXPLORE a new theme / EXPLOIT the leader). Empty when stance is "balanced" (today).
        cues = collect_hint_cues(self, ("_complexity_hint", "_sweep_hint", "_novelty_feedback",
                                        "_novelty_hint"))
        hyp_sys = _hypothesis_system_suffix(self.track_hypotheses)
        messages = [
            {"role": "system",
             # P6: the capability suffix (sweep offer — gated on the active Developer — +
             # eval_timeout), the operator note, and the emit instruction are appended AFTER the
             # render() — the SAME code-owned pattern as agent.py's ToolUsingResearcher. A
             # `researcher_system.md` PromptStore override replaces only the CORE persona, so an
             # override can never desync the capability prose from what the backend actually
             # implements (pre-fix the suffix was baked INSIDE the render default and an override
             # bypassed the offer_sweep gate). The assembled default is byte-equal to
             # `_researcher_system(offer_sweep)`.
             "content": render(self.prompts, "researcher_system", _RESEARCHER_CORE)
                        + _researcher_capability_suffix(getattr(self, "offer_sweep", True))
                        + _OPERATOR_NOTE
                        + "Respond ONLY with the requested structured fields." + hyp_sys
                        + "\n\n" + _attention_points()},
            {"role": "user", "content": _state_brief(state, parent,
                                                     digest_cap=getattr(self, "_digest_cap", 0),
                                                     hyp_order=getattr(self, "_hyp_order", None))
                                        + "\n" + self.space_hint +
                                        hint_block + cues +
                                        "\nPropose the next Idea (operator, params, rationale"
                                        + (", hypothesis" if self.track_hypotheses else "") +
                                        # P6: don't re-offer the sweep in the user turn when the
                                        # active Developer can't run one (system prompt gates too).
                                        ("; optionally a `space` grid for a sweep"
                                         if getattr(self, "offer_sweep", True) else "") + "). The "
                                        "`rationale` is your conclusion the operator reads: in 1-3 "
                                        "sentences state WHY this experiment next and WHAT you expect "
                                        "it to learn/improve given the results so far — not a "
                                        "restatement of the params. " + _IDEA_SPACE_PLAIN
                                        + (" The `hypothesis` is the one-line belief this experiment "
                                           "tests (reuse wording across experiments that test the same "
                                           "belief)." if self.track_hypotheses else "")},
        ]
        # Small models occasionally emit unparseable output; retry, then fall back to a
        # safe default so one bad response never crashes the run.
        idea: Optional[Idea] = None
        last: Optional[Exception] = None
        for _ in range(2):
            try:
                idea = parse_structured(self.client, messages, Idea, self.parser)
                break
            except ParseError as e:
                last = e
        if idea is None:
            idea = Idea(operator="draft", params={}, rationale=f"fallback (parse failed: {last})")
        return _clamp_fill(idea, self.bounds)


class LLMDeveloper:
    """Writes (and repairs) a complete runnable solution script. `brief` carries the
    task's I/O contract (where to read data, what metric to print). `repair` powers the
    error-feedback debug operator: it gets the failing code + stderr and fixes it."""

    # T8/A0b: this Developer generates real code, so merge_mode="auto" resolves to the
    # code-recombination ensemble merge (the verified strongest operator) instead of mean-params.
    is_code_generating = True

    def __init__(self, client: LLMClient, brief: str = "",
                 prompts: Optional[PromptStore] = None):
        self.client = client
        self.brief = brief
        self.prompts = prompts

    def implement(self, idea: Idea) -> str:
        system = (render(self.prompts, "developer_system", _DEVELOPER_SYSTEM) + self.brief
                  + "\n\n" + _attention_points())
        # Render whatever params the task's Researcher proposed (task-agnostic): degree/lam
        # for regression, k for mlebench, etc. — hardcoding names dropped the value on
        # tasks that use a different hyperparameter.
        params = ", ".join(f"{k}={v}" for k, v in idea.params.items()) or "(model defaults)"
        if idea.space:
            # Intra-node sweep: render the grid and append the trials-reporting contract to the
            # system prompt so the Developer runs every point in one process and reports them all.
            system += _SWEEP_CONTRACT
            grid = "; ".join(f"{k} in {v}" for k, v in idea.space.items())
            fixed = f" Fixed/shared params: {params}." if idea.params else ""
            user = (f"Run an intra-node sweep over the grid: {grid}.{fixed} {idea.rationale}").strip()
        else:
            user = (f"Experiment concept (the researcher's idea): {idea.rationale}\n"
                    f"Parameters: {params}.\n"
                    "You own the implementation: design and write the solution code that realises "
                    "this concept.").strip()
        return extract_code(self.client.complete_text(
            [{"role": "system", "content": system}, {"role": "user", "content": user}]))

    def repair(self, idea: Idea, code: str, error: str) -> str:
        # P8: the hardware/operational cues reach repair too (a timeout/oom repair NEEDS the real
        # GPU/CPU picture to size the cheaper retry) — appended after the render() calls, same as
        # the implement path.
        system = (render(self.prompts, "developer_repair_prefix", "You are an expert Python debugger. ") +
                  render(self.prompts, "developer_system", _DEVELOPER_SYSTEM) + self.brief
                  + "\n\n" + _attention_points())
        user = ("The script below failed. Return a corrected, complete script that runs "
                "and prints the required JSON metric line.\n\n--- SCRIPT ---\n" + code +
                "\n\n--- ERROR (stderr tail) ---\n" + error)
        # Include the idea rationale — the ValidatingDeveloper folds the validator's rejection feedback
        # into it on each retry, so without this the retry re-sends a byte-identical prompt and
        # deterministically re-fails, burning every attempt.
        if idea is not None and getattr(idea, "rationale", ""):
            user += "\n\n--- ADDITIONAL GUIDANCE ---\n" + idea.rationale
        return extract_code(self.client.complete_text(
            [{"role": "system", "content": system}, {"role": "user", "content": user}]))


# --------------------------------------------------------------------------- #
# Wrapper-forwarding mixin: the Developer-wrapper contract, documented once
# --------------------------------------------------------------------------- #


class WrapsDeveloper:
    """Forwarding half of the Developer-WRAPPER contract (`ValidatingDeveloper`,
    `BestOfNDeveloper`, `UnifiedAgent`). A wrapper composes an inner Developer and must stay
    transparent to every duck-typed probe the engine/factories make against `developer`:

    - `inner` (plain attribute, set by the wrapper's ``__init__``): the wrapped Developer.
      The engine's ablation probe reads ``getattr(developer, "inner", developer)`` to bypass
      wrapper retry/fallback/best-of-N machinery (``orchestrator._probe_developer``), so
      `inner` must always be the raw developer a probe should hit.
    - `brief` / `is_code_generating` / `client` / `prompts` / `last_report`: read-through
      (and, for `client`/`prompts`, hasattr-guarded write-through) to the wrapped developer —
      `make_roles` pokes `prompts`, H3 per-role rewiring pokes `client`, T8/A0b
      merge_mode="auto" resolution reads `is_code_generating`, and the orchestrator reads
      `last_report` for the `agent_validated` audit event.
    - `last_files` / `last_deleted`: per-call audit attributes the orchestrator reads AFTER
      implement/repair. Wrappers own them as plain attributes: either mirrored from the
      wrapped developer via `_sync_audit()`, or set by the wrapper's own logic (e.g.
      best-of-N's chosen candidate, the validator's fell-back handling).
    - `audit_extra()`: wrapper-specific audit fields merged into the `agent_validated` event.

    Delegation target: `_wrapped` (defaults to `inner`). `UnifiedAgent` overrides it — its
    delegate is `self.developer` (possibly itself a wrapper) while its `inner` exposes the
    fully-unwrapped probe developer.

    A wrapper whose semantics for a member differ from these defaults keeps that member local
    (e.g. `ValidatingDeveloper`'s unconditional `prompts` setter, its agent-vs-fallback
    `is_code_generating`/`last_report`, and `UnifiedAgent`'s locally-held `prompts` handle).
    """

    @property
    def _wrapped(self):
        return self.inner

    # forward the hooks make_roles / the engine poke at, to the wrapped developer
    @property
    def brief(self) -> str:
        return getattr(self._wrapped, "brief", "")

    # T8/A0b: capability follows the wrapped developer (merge_mode="auto" resolution)
    @property
    def is_code_generating(self) -> bool:
        return bool(getattr(self._wrapped, "is_code_generating", False))

    @property
    def client(self):
        return getattr(self._wrapped, "client", None)

    @client.setter
    def client(self, value) -> None:        # H3 per-role client rewiring reaches the inner developer
        if hasattr(self._wrapped, "client"):
            self._wrapped.client = value

    @property
    def prompts(self):
        return getattr(self._wrapped, "prompts", None)

    @prompts.setter
    def prompts(self, value) -> None:
        if hasattr(self._wrapped, "prompts"):
            self._wrapped.prompts = value

    @property
    def last_report(self):
        return getattr(self._wrapped, "last_report", None)

    def audit_extra(self) -> dict:
        fn = getattr(self._wrapped, "audit_extra", None)
        return fn() if callable(fn) else {}

    def _sync_audit(self) -> None:
        """Mirror the wrapped developer's per-call file audit onto this wrapper."""
        self.last_files = getattr(self._wrapped, "last_files", {}) or {}
        self.last_deleted = getattr(self._wrapped, "last_deleted", []) or []


# --------------------------------------------------------------------------- #
# Validating wrapper (ADR-7): audit how an external coding agent performed
# --------------------------------------------------------------------------- #


class ValidatingDeveloper(WrapsDeveloper):
    """Wrap a Developer and validate how it performed before the orchestrator spends a
    sandbox evaluation on its output (see `validate.py`).

    On an invalid result it re-prompts the *inner* developer with the failure folded
    into the Idea's rationale (a cheap correction loop), up to `max_retries` times. If it
    still can't produce valid code it falls back to `fallback` (typically the in-house
    `LLMDeveloper`, the known-good path) — so a flaky external agent degrades to a
    working developer instead of poisoning the search with a no-op/broken node.

    `last_report` holds the `AgentReport` for the most recent call; the orchestrator logs
    it as an `agent_validated` event, giving a per-node audit trail of the agent.

    Tool-agnostic: any Developer works as `inner`. If `inner` exposes `last_run` /
    `last_seed` (as `CliAgentDeveloper` does), the report also includes process-level
    checks (launched / not-timed-out / exit) and the no-op (`modified_seed`) check.
    """

    # `last_report` is genuinely LOCAL state — it always describes the EXTERNAL AGENT (even
    # when we fall back) — so shadow the mixin's live forwarder with a plain attribute.
    last_report: Optional[AgentReport] = None

    def __init__(self, inner, *, fallback=None, max_retries: int = 1,
                 metric_key: str = "metric", repo_mode: bool = False):
        self.inner = inner
        self.fallback = fallback
        self.max_retries = max_retries
        self.metric_key = metric_key
        # repo_mode: validate the agent's changed-FILE set (RepoTask), and treat the
        # fallback (a baseline / no-op developer) as always shippable — running the
        # unmodified repo is a valid result, not a failure.
        self.repo_mode = repo_mode
        # Audit of the most recent call. `last_report` always describes the EXTERNAL
        # AGENT (even when we fall back) — that's what we're auditing; the fallback's
        # validity is recorded separately in `last_shipped_ok`.
        self.last_report: Optional[AgentReport] = None
        self.last_attempts: int = 0
        self.last_fell_back: bool = False
        self.last_shipped_ok: bool = False
        self.last_files: dict[str, str] = {}   # multi-file output of the shipped attempt
        self.last_deleted: list[str] = []      # accepted in-surface deletions of the shipped attempt

    # T8/A0b: code-generation capability follows the INNER developer (the fallback is LLM anyway)
    # — kept local: unlike the mixin's forwarder, the fallback's capability counts too.
    @property
    def is_code_generating(self) -> bool:
        return bool(getattr(self.inner, "is_code_generating", False)
                    or getattr(self.fallback, "is_code_generating", False))

    # forward the prompt hook make_roles pokes at, to the wrapped developer — kept local:
    # the setter is UNCONDITIONAL (it must create the attribute on an inner that lacks one),
    # unlike the mixin's hasattr-guarded write-through.
    @property
    def prompts(self):
        return getattr(self.inner, "prompts", None)

    @prompts.setter
    def prompts(self, value) -> None:
        self.inner.prompts = value

    def _report(self, code: str, *, agent: bool) -> AgentReport:
        """Validate `code`. `agent=True` pulls the inner agent's process signal + seed
        (no-op detection) + patch-gate verdict; `agent=False` (fallback output) does
        static checks only."""
        return validate_agent_code(
            code,
            seed=getattr(self.inner, "last_seed", None) if agent else None,
            run=getattr(self.inner, "last_run", None) if agent else None,
            patch=getattr(self.inner, "last_patch", None) if agent else None,
            files=(getattr(self.inner, "last_files", {}) or {}) if (agent and self.repo_mode)
                  else None,
            metric_key=self.metric_key,
        )

    def _record(self, report: AgentReport, *, attempts: int, fell_back: bool,
                shipped_ok: bool) -> None:
        self.last_report = report
        self.last_attempts = attempts
        self.last_fell_back = fell_back
        self.last_shipped_ok = shipped_ok
        # Multi-file output only when the agent itself shipped; the LLM fallback is
        # single-file (its code goes to solution.py via node.code).
        self.last_files = ({} if fell_back
                           else dict(getattr(self.inner, "last_files", {}) or {}))
        self.last_deleted = ([] if fell_back
                             else list(getattr(self.inner, "last_deleted", []) or []))

    def _attempt_loop(self, idea: Idea, call, fallback_call=None) -> str:
        """Run `call(idea)` (implement or repair), validate, retry-with-feedback up to
        `max_retries`, then fall back via `fallback_call` (defaults to the fallback's
        implement). Records the agent audit on every path."""
        code, report = "", AgentReport()
        attempt = idea
        attempts = 0
        for _ in range(self.max_retries + 1):
            attempts += 1
            code = call(attempt)
            report = self._report(code, agent=True)
            if report.ok:
                self._record(report, attempts=attempts, fell_back=False, shipped_ok=True)
                return code
            attempt = idea.model_copy(deep=True)   # re-prompt with the failure as a hint
            attempt.rationale = (
                idea.rationale +
                f"\n[validator] the previous attempt was rejected: {report.feedback()}. "
                "Fix this in your changed files (the solution script / the repo files you edited).").strip()
        if self.fallback is not None:              # exhausted retries -> known-good path
            fb = (fallback_call or (lambda: self.fallback.implement(idea)))()
            # In repo mode the fallback is the baseline (no-op) developer — running the
            # unmodified repo is always a valid shippable result.
            fb_ok = True if self.repo_mode else self._report(fb, agent=False).ok
            # last_report still describes the AGENT (it failed); shipped code is the LLM's
            self._record(report, attempts=attempts, fell_back=True, shipped_ok=fb_ok)
            return fb
        self._record(report, attempts=attempts, fell_back=False, shipped_ok=report.ok)
        return code

    def audit_extra(self) -> dict:
        """Wrapper-specific audit fields merged into the `agent_validated` event so the
        log shows whether the agent succeeded, how many tries it took, and whether we had
        to fall back to the LLM developer."""
        return {"attempts": self.last_attempts, "fell_back": self.last_fell_back,
                "shipped_ok": self.last_shipped_ok}

    def implement(self, idea: Idea) -> str:
        return self._attempt_loop(idea, self.inner.implement)

    def repair(self, idea: Idea, code: str, error: str) -> str:
        inner_repair = getattr(self.inner, "repair", None)
        if not callable(inner_repair):
            return self.implement(idea)
        # Fall back to the fallback's repair (preserving the error-feedback) when it has
        # one, else its implement — never lose the debug context on the fallback path.
        fb_repair = getattr(self.fallback, "repair", None) if self.fallback else None
        fb_call = (lambda: fb_repair(idea, code, error)) if callable(fb_repair) else None
        return self._attempt_loop(idea, lambda i: inner_repair(i, code, error), fb_call)
