"""The LLM VALUE ESTIMATE the MCTS tree never had (docs/BACKLOG.md §0.1 row 17), as its own engine
MIXIN: `class Engine(…, ValueEstimateMixin)` inherits these methods unchanged, so `self` here IS the
engine exactly as in every other engine mixin.

WHAT WAS WRONG. `search/policy.py::MCTSPolicy` valued a node by folding its subtree's best metric
into a bounded reward, and nothing else. Two candidates with the same metric therefore scored the
same no matter what they DID — a branch nobody has expanded, a branch whose every child regressed,
and a branch that is one obvious edit from a win are one point to the tree — and the only term that
separates them, `c·sqrt(ln N / visits)`, counts visits and cannot read a line of code. ADR-5 §5
("spend the engineering budget on the evaluator, not topology") and the LATS ablation it cites
(removing the value function costs 5× more than switching MCTS→DFS) both say the value signal is
where the leverage is; this is the value signal.

WHAT THIS IS AND IS NOT. It asks one bounded structured question per unestimated candidate — "how
much does this branch still have left, 0 to 1" — and freezes the answer as `node_value_estimated`.
`MCTSPolicy` then reads it as `value_estimate`'s decaying adjustment to the UCB1 value term. It is
NOT a metric, NOT a champion signal, and it is never back-propagated: ADR-5 §4 keeps value tree-like
precisely because multi-parent credit assignment has no canonical rule, so the prior is read off the
candidate it was written for and propagates nowhere.

WHY THE ESTIMATE IS AN EVENT AND NOT A LIVE FIELD. Invariant #4 says state is observed only via the
fold, and #5 that the fold is deterministic — so a model's answer cannot be computed inside it. The
same shape `node_verified` already uses: the paid call happens here, on the main task's cadence, and
the log carries the number, which is what makes a replay expand the same nodes in the same order.

SPANNED, NOT MERELY BEACONED, on `verifier_tiebreak.py::_maybe_verify_ties`' own ground: the
cadences run with no span open and `core/tracing.py::generation` yields a NULL handle when nothing
is being traced, so a paid step that opens none writes its `llm_usage` rows with `trace_id=null` and
becomes real money attributable to nothing. `_op_span` and not `_paid_progress` because
`_paid_progress`'s phase argument is a CLOSED word out of `events/types.py::PROGRESS_PHASES`, whose
own rule is that a listed phase nothing emits renders a step the operator watches and never sees
complete — and this is a cadence step, not a phase of one node's build or evaluation.

Layering: no runtime import of the orchestrator and never `serve` — core, events, search and stdlib
only, with the role parser and the judge kept lazy and method-local as the sibling cadences keep
theirs.
"""
from __future__ import annotations

import contextlib
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from looplab.core.containment import contain
from looplab.core.llm import BudgetExceeded
from looplab.core.llm_broker import in_llm_lane
from looplab.core.models import NodeStatus, RunState
from looplab.events.replay import fold
from looplab.events.types import EV_NODE_VALUE_ESTIMATED

# How many candidates one cadence may pay for. The tree only ever expands ONE node per creation
# boundary, so the estimate has to cover the candidates that could win it — but a run with a wide
# feasible pool must not turn one boundary into a dozen provider calls. Six is the width at which
# the pool is covered on every shipped `n_seeds`/width default while the spend stays bounded; a
# candidate left over is estimated at the next boundary, and until then reads as "nobody asked",
# which `value_estimate` treats as no opinion rather than as an average one.
VALUE_ESTIMATE_CADENCE_CAP = 6
# The rationale is recorded for the reader, not for a consumer: nothing parses it. Bounded at the
# append site so a chatty model cannot grow the event log without limit.
VALUE_ESTIMATE_RATIONALE_CAP = 240


class _BranchValueOut(BaseModel):
    """Closed structured-output vocabulary for one branch's remaining headroom."""

    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")

    headroom: float = Field(ge=0.0, le=1.0)
    why: str = ""

    @field_validator("headroom", mode="before")
    @classmethod
    def _headroom_is_not_boolean(cls, value):
        # `True` validates as 1.0 under pydantic's numeric coercion, which would read as maximal
        # promise from a model that answered a yes/no question it was not asked.
        if isinstance(value, bool):
            raise ValueError("branch headroom must not be boolean")
        return value


class ValueEstimateMixin:
    """The engine's LLM value-estimate cadence. See the module docstring for the mixin convention
    (`self` is the Engine)."""

    @in_llm_lane("enrichment")
    def _maybe_estimate_node_values(self, state: RunState) -> RunState:
        """Estimate the remaining headroom of every unestimated MCTS candidate, bounded per cadence.

        No-op unless the live policy actually reads the number (`MCTSPolicy.value_weight > 0`),
        which is the ONE gate: a run whose policy cannot use an estimate must not buy one. Also a
        no-op below two candidates — with a single candidate the pick is forced and the estimate
        could not move it, so paying for one would be spending on a decision already made.

        Best-effort in the same sense the tie-break is: no client, an unparseable answer or any
        provider failure just leaves the node unestimated, and an unestimated node is not an average
        one (`search/policy.py::value_estimate` returns the reward untouched for a `None` prior). A
        `BudgetExceeded` is re-raised rather than contained — the operator's spend ceiling ends the
        run, and swallowing it here would let the search keep billing past the limit set to stop it.
        """
        weight = float(getattr(getattr(self, "policy", None), "value_weight", 0.0) or 0.0)
        if weight <= 0.0:
            return state
        candidates = self._value_estimate_candidates(state)
        if len(candidates) < 2:
            return state
        try:
            client = self._reflect_client()
        except Exception as exc:  # noqa: BLE001 — advisory search signal: no client, no estimate
            contain("value_estimate_client", exc)
            client = None
        if client is None:
            return state
        # Process-local FAILURE guard, the same one `_maybe_verify_ties` keeps and for the same
        # reason: a degraded client would otherwise be re-asked about the same (node, generation)
        # at every creation boundary for the rest of the run. In-memory only — the estimate is live,
        # never replayed — so a fresh process on resume may retry once per node, which is bounded.
        attempted = self._value_estimate_attempted
        done = False
        _span = getattr(self, "_op_span", None)
        with (_span("value_estimate") if callable(_span) else contextlib.nullcontext()):
            for node in candidates[:VALUE_ESTIMATE_CADENCE_CAP]:
                key = (node.id, node.attempt)
                if key in attempted:
                    continue
                verdict = self._branch_headroom(state, node, client)
                if verdict is None:
                    attempted.add(key)      # this revision abstains hereafter in this process
                    continue
                self.store.append(EV_NODE_VALUE_ESTIMATED, {
                    "node_id": node.id, "generation": node.attempt,
                    "value": round(verdict["headroom"], 4),
                    "rationale": verdict["why"][:VALUE_ESTIMATE_RATIONALE_CAP],
                })
                done = True
        return fold(self.store.read_all()) if done else state

    def _value_estimate_candidates(self, state: RunState) -> list:
        """The nodes an estimate could still move, lowest id first (a deterministic spend order).

        Exactly the pool `MCTSPolicy` ranks — `breedable_nodes()`, which is already feasible,
        untombstoned and not gate-excluded — minus the ones whose estimate for THIS attempt is
        already frozen in the log. Asking again about a node whose prior is recorded would buy the
        same number twice; a node that has been RESET carries a new attempt and is legitimately
        re-asked, because the code the first answer described is gone.
        """
        return sorted((n for n in state.breedable_nodes() if n.value_prior is None),
                      key=lambda n: n.id)

    def _branch_headroom(self, state: RunState, node, client) -> Optional[dict]:
        """One bounded structured ask: how much does expanding THIS branch still have left?

        Returns `{"headroom": float in [0,1], "why": str}` or None on any failure. The evidence is
        the branch's own record — what the node tried, what it scored, and what its children have
        already got out of it — because "is this lineage spent" is a question about what has been
        tried, and the counter-evidence to a promising-sounding rationale is a child that already
        tried the obvious next thing and regressed.

        The ask is deliberately NOT "is this the best node" — that is the metric's job and the tree
        already has it. Answering the metric back would make the estimate a second, noisier copy of
        a number the policy already reads exactly, which is the failure mode a value function has.
        """
        from looplab.agents.roles import resolve_role_parser
        from looplab.trust.judge import structured_judge

        try:
            parser = resolve_role_parser(getattr(self, "researcher", None),
                                         getattr(self, "developer", None))
            better = "lower is better" if state.direction == "min" else "higher is better"
            children = [c for c in state.nodes.values() if node.id in c.parent_ids
                        and c.status is NodeStatus.evaluated and c.metric is not None]
            tried = "; ".join(
                f"#{c.id} tried {(c.idea.rationale or c.idea.operator or '')[:160]} -> "
                f"metric {c.metric}" for c in sorted(children, key=lambda c: c.id)[:6])
            msgs = [
                {"role": "system", "content":
                 "You judge how much room an experiment lineage still has. Answer with a single "
                 "number `headroom` in [0,1]: 0 means this lineage is spent — the obvious next "
                 "edits have been tried and did not pay — and 1 means it has a lot left. Judge the "
                 "REMAINING room, not how good the result already is: a strong node whose every "
                 "child regressed has little headroom, and a weak node nobody has followed up on "
                 "may have a lot. Do not restate the metric."},
                {"role": "user", "content":
                 f"Goal: {(state.goal or '(unstated)')[:600]}\n"
                 f"Metric direction: {state.direction} ({better}).\n"
                 f"Branch root: experiment #{node.id}, operator {node.idea.operator!r}, "
                 f"metric {node.metric}.\n"
                 f"What it tried: {(node.idea.rationale or '(unstated)')[:1200]}\n"
                 f"Its parameters: {dict(list((node.idea.params or {}).items())[:20])}\n"
                 + (f"Follow-ups already run from it: {tried}\n" if tried else
                    "Follow-ups already run from it: none — nobody has expanded this branch yet.\n")
                 + f"Best metric anywhere in this run so far: "
                   f"{getattr(state.best(), 'metric', None)}\n"},
            ]
            out = structured_judge(client, msgs, _BranchValueOut, parser=parser)
        except BudgetExceeded:
            # The operator's spend ceiling ends the RUN. Contained here it would end only this
            # estimate, and the search would keep billing past the limit that was set to stop it
            # (doc 50 AG-01, the same defect at a selection site).
            raise
        except Exception as exc:  # noqa: BLE001 — advisory search signal: any failure abstains
            contain("value_estimate", exc)
            return None
        if out is None:
            return None
        return {"headroom": float(out.headroom), "why": str(out.why or "")}
