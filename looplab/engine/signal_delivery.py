"""Signal-delivery registry (§1 of docs/14-agent-framework-mega-review-2026-07-10.md).

The engine computes rich, expensive signals — trust flags, LLM crash-triage verdicts, foresight
predictions, deep-research memos, per-operator yields, operator directives, run states — and each is
only useful if it reaches the agent (or human) that can act on it. The recurring failure mode ("the
signal is folded but nothing injects it into a prompt") is the same class the hint registry
(`roles.RESEARCHER_HINT_ATTRS`) already turned into a test-enforced invariant. This module
generalizes that discipline to EVERY delivered signal.

Each signal must cross four links (see the review doc):
  L1 fold     — the event is folded into `RunState` (a field or list), additively, reader-defaulted.
  L2 carry    — it reaches the prompt-assembly layer via a channel: `push` (engine setattr / prompt
                append), `pull` (a tool the agent may call), or `context` (a folded-state brief).
  L3 inject   — a documented render site turns it into the consumer's prompt. Most signals have ONE;
                a signal MAY expose a secondary channel (e.g. the deep-research memo has both the
                `read_research_memo` pull tool and a `_state_brief` résumé). Every real wiring point is
                listed in `call_sites` and asserted present, so none can be silently deleted.
  L4 close    — (learning signals only) the realized outcome is folded back so the next injection
                carries the track record.

`SIGNALS` is the single source of truth. `tests/test_signal_delivery.py` asserts (a) every entry is
well-formed and its `inject` symbol is importable+callable, and (b) a per-signal probe shows the
signal's content actually reaching the rendered output. Adding a delivered signal here without a
probe in that test FAILS the suite — so "the signal silently stopped being delivered" is a red test,
not the next review's finding.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SignalRoute:
    name: str
    produced_by: str        # where the signal is computed / emitted
    folded_into: str        # the RunState field (or derivation) it lands in — L1
    channel: str            # "push" | "pull" | "context" — L2
    inject: str             # "module:function" that RENDERS it into a prompt — L3
    consumer: str           # the agent/human that reads it
    # L3 CALL SITES: (repo-relative file, literal substring) pairs that MUST be present — the actual
    # place the producer/consumer wires the injection in. `tests/test_signal_delivery.py` asserts each
    # substring is in the file, so DELETING a call site (the "folded but no longer injected" §1 failure
    # mode) turns the suite red — the source scan is the real enforcement (a rendering function that
    # nothing calls would still pass the isolated-probe check). Mirrors tests/test_hint_forwarding.py.
    call_sites: tuple = ()
    closes_loop: bool = False   # L4: does an outcome feed back (learning signal)?


# The delivered signals. Keep in sync with the injection sites; the test enforces it.
SIGNALS: tuple[SignalRoute, ...] = (
    SignalRoute(
        name="trust_flags",
        produced_by="trust.reward_hack/leakage/critic scans (orchestrator._evaluate trust scan)",
        folded_into="RunState.reward_hacks",
        channel="push",
        inject="looplab.events.digest:trust_reflection",
        consumer="Researcher (via _set_complexity_hint -> _complexity_hint)",
        call_sites=(("looplab/engine/orchestrator.py", "trust_reflection(state)"),)),
    SignalRoute(
        name="triage_rationale",
        produced_by="orchestrator._triage_crash (LLM crash-triage verdict)",
        folded_into="Node.triage_rationale",
        channel="context",
        inject="looplab.events.digest:_node_line",
        consumer="Researcher (experiments_digest + failure-reflection hint)",
        call_sites=(("looplab/events/replay.py", "n.triage_rationale ="),
                    ("looplab/events/digest.py", "triage_rationale"))),
    SignalRoute(
        name="foresight_calibration",
        produced_by="foresight_selected events + node outcomes",
        folded_into="RunState.foresight_selected",
        channel="context",
        inject="looplab.search.foresight:foresight_scoreboard",
        consumer="Foresight world model (via _memory_brief)",
        call_sites=(("looplab/search/foresight.py", "foresight_scoreboard(state)"),
                    ("looplab/events/replay.py", "EV_FORESIGHT_SELECTED")),
        closes_loop=True),
    SignalRoute(
        name="deep_research_memo",
        produced_by="orchestrator._record_deep_research (deep-research memo)",
        folded_into="RunState.research",
        channel="pull",                                # dual channel: the tool (pull) + a state-brief résumé (context)
        inject="looplab.tools.run_tools:RunTools._research_memo",
        consumer="Researcher (read_research_memo tool + _state_brief résumé)",
        call_sites=(("looplab/tools/run_tools.py", "read_research_memo"),
                    ("looplab/agents/roles.py", "read_research_memo"))),
    SignalRoute(
        name="operator_yields",
        produced_by="search.policy.operator_yields (folded from the DAG)",
        folded_into="derived: operator_yields(state) -> StrategyContext.operator_yields",
        channel="context",
        inject="looplab.agents.strategist:_fmt_operator_yields",
        consumer="Strategist (_strategist_brief)",
        call_sites=(("looplab/engine/strategy.py", "operator_yields(state)"),  # _strategy_ctx: StrategyCadenceMixin
                    ("looplab/agents/strategist.py", "_fmt_operator_yields(ctx.operator_yields)"))),
    SignalRoute(
        name="operator_directives",
        produced_by="hint control events",
        folded_into="RunState.pending_hints",
        channel="push",
        inject="looplab.agents.hints:render_hint_directives",
        consumer="Researcher, Strategist, pilot, crash-triage, Developer",
        call_sites=(("looplab/engine/orchestrator.py", "render_hint_directives(state.pending_hints)"),
                    ("looplab/engine/orchestrator.py", "self._directed_idea("),
                    # the ablation-produced refine_block is a real tree-entering Developer node too, so
                    # its directive threading is enforced here (it was silently bypassed before).
                    ("looplab/engine/ablation.py", "self._directed_idea("))),
    SignalRoute(
        name="run_states",
        produced_by="control/eval events (pause/approval/build/leakage/trust)",
        folded_into="RunState.paused/awaiting_approval/building/leakage/reward_hacks",
        channel="context",
        inject="looplab.serve.llm_context:_attention_states",
        consumer="boss/assistant (human intervention)",
        call_sites=(("looplab/serve/llm_context.py", "_attention_states(st)"),)),
)


def resolve_inject(route: SignalRoute):
    """Import and return the `inject` callable named on a route ("module:function" or
    "module:Class.method"). Raises on a missing/renamed symbol — that IS the enforcement."""
    import importlib
    mod_name, _, qual = route.inject.partition(":")
    obj = importlib.import_module(mod_name)
    for part in qual.split("."):
        obj = getattr(obj, part)
    return obj
