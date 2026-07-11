"""Novelty / dedup gate (E1/T5) for the engine — extracted from orchestrator.py as a MIXIN:
`class Engine(NoveltyGateMixin, …)` inherits these methods unchanged, so there is ZERO
call-site churn and `self` here IS the engine. The method bodies are verbatim moves and read
engine attributes freely (`_embedder` / `_idea_vecs` cache / `store` / `researcher` /
`_novelty_*` knobs / `_reflect_client`), exactly as they did inside the class.

Two layers, cheapest first, BEFORE any compute is spent on a proposal: a SEMANTIC/LLM
near-duplicate check (reject + one informed re-propose) and the E1 NUMERIC param-distance nudge.
The heavy tool/agent imports stay method-local (imported from their source modules on use), so a
test monkeypatching `looplab.tools.vectorstore._cosine` / `looplab.agents.agent.agentic_struct`
still intercepts them.

Layering: no runtime import of the orchestrator (TYPE_CHECKING only) and never serve — only
core, events and stdlib (the search/agent/tool deps are lazy, method-local imports)."""
from __future__ import annotations

from typing import Optional

from looplab.core.llm import BudgetExceeded
from looplab.core.models import Idea, NodeStatus, RunState
from looplab.events.types import EV_NOVELTY_REJECTED


class NoveltyGateMixin:
    """The engine's novelty/dedup gate cluster. See the module docstring for the mixin convention
    (`self` is the Engine)."""

    # -------------------------------------------------------- novelty gate (E1/T5)
    @staticmethod
    def _idea_text(idea) -> str:
        """The semantic identity of a proposal: what it claims to try + why."""
        return " ".join(filter(None, [getattr(idea, "rationale", "") or "",
                                      getattr(idea, "hypothesis", "") or ""])).strip()

    def _idea_vec(self, text: str):
        # Key on the TEXT (not a node_id): the embedding is a pure function of the text, and a
        # `node_reset` re-creates the SAME id with a NEW idea — a node_id-keyed cache then returned the
        # OLD vector and the semantic-novelty gate compared future proposals against a stale idea. The
        # cache is in-memory only (never persisted/replayed), so a per-process `hash(text)` key is safe.
        key = hash(text)
        v = self._idea_vecs.get(key)
        if v is None:
            v = self._embedder(text)
            self._idea_vecs[key] = v
        return v

    def _semantic_duplicate(self, state: RunState, idea: Idea):
        """T5: nearest existing node by idea-TEXT embedding similarity, or None. Only meaningful
        for proposals with real text (LLM ideas); short/empty rationales (toy backends) skip."""
        text = self._idea_text(idea)
        if len(text) < 20:
            return None, 0.0
        from looplab.tools.vectorstore import _cosine
        v = self._embedder(text)
        best_n, best_s = None, 0.0
        for n in state.nodes.values():
            nt = self._idea_text(n.idea)
            if len(nt) < 20:
                continue
            try:
                s = _cosine(v, self._idea_vec(nt))
            except Exception:  # noqa: BLE001 — an embedder hiccup must never block proposing
                continue
            if s > best_s:
                best_n, best_s = n, s
        if best_n is not None and best_s >= self._novelty_semantic_threshold:
            return best_n, best_s
        return None, best_s

    def _llm_novelty_gate(self, state: RunState, idea: Idea, repropose=None) -> Idea:
        """novelty_mode="llm": an LLM (not an embedding/param-distance heuristic) judges whether the
        proposed idea near-duplicates an already-tried experiment — READING the real experiments via
        tools when unsure — and, if it does and a `repropose` callable is given, asks the Researcher once
        more for a meaningfully different idea (surfacing the duplicate's outcome). Loop-safe + best-
        effort: any failure just returns the original idea. Emits the same `novelty_rejected` audit
        event (kind="llm") the algorithmic gate does."""
        if not state.nodes:
            return idea
        try:
            client = self._reflect_client()
        except Exception:  # noqa: BLE001
            client = None
        if client is None:
            return idea
        from pydantic import BaseModel
        from looplab.agents.agent import agentic_struct, CompositeTools
        from looplab.tools.run_tools import RunTools

        class _NoveltyVerdict(BaseModel):
            is_duplicate: bool = False
            near_node_id: Optional[int] = None
            reason: str = ""

        brief = "; ".join(f"#{n.id} {n.operator}: {self._idea_text(n.idea)[:80]}"
                          for n in list(state.nodes.values())[-25:])
        msgs = [{"role": "system",
                 "content": "You judge experiment NOVELTY for an ML research loop. Decide if a PROPOSED "
                            "idea is a near-duplicate of an experiment already tried in THIS run. Read the "
                            "actual experiments (read_experiment / read_code) when unsure. A rewording or a "
                            "trivially-close variant of a tried idea is a DUPLICATE; a genuinely different "
                            "approach, component, loss, data or direction is NOVEL. Prefer NOVEL unless "
                            "clearly a repeat."},
                {"role": "user",
                 "content": f"PROPOSED idea: {self._idea_text(idea)}\n\nAlready tried: {brief}\n\n"
                            "Emit is_duplicate, near_node_id (the tried experiment it duplicates, or null), "
                            "and a one-line reason."}]
        try:
            rt = RunTools()
            rt.bind_state(state, None)
            v = agentic_struct(client, CompositeTools([rt]), msgs, _NoveltyVerdict,
                               loop_opts={"max_turns": 12})
        except Exception:  # noqa: BLE001
            return idea
        if not (v and getattr(v, "is_duplicate", False)
                and isinstance(v.near_node_id, int) and v.near_node_id in state.nodes):
            return idea
        dup = state.nodes[v.near_node_id]
        outcome = (f"it FAILED ({dup.error_reason})" if dup.status is NodeStatus.failed
                   else f"it scored {dup.metric}")
        self.store.append(EV_NOVELTY_REJECTED, {
            # the PROSPECTIVE id this proposal would get — allocated as max+1, NOT len(): on a gapped
            # log (a dropped/malformed node_created) len() points at the wrong slot (audit only).
            "node_id": max(state.nodes, default=-1) + 1, "near_node": dup.id, "kind": "llm",
            "reason": str(v.reason)[:200], "stance": self._novelty_stance,
            "action": "reproposed" if callable(repropose) else "kept"})
        if callable(repropose):
            hint = (f"\nNOVELTY GATE (LLM): your proposal near-duplicates experiment #{dup.id} — "
                    f"{outcome} ({str(v.reason)[:160]}). Propose something MEANINGFULLY DIFFERENT "
                    "(another approach, component or direction), not a rewording.")
            idea = self._repropose_with_feedback(repropose, hint, idea)
        return idea

    def _repropose_with_feedback(self, repropose, hint: str, idea: Idea) -> Idea:
        """One informed re-propose with the duplicate surfaced as a TRANSIENT `_novelty_feedback`
        directive (shared by the LLM and semantic gates — the set/try/finally-restore discipline
        must stay identical in both). `BudgetExceeded` re-raises (the hard budget stop must end the
        run, not be swallowed); any other repropose failure keeps the original idea. The `finally`
        ALWAYS restores the previous feedback, even if repropose() raised: otherwise this transient
        "you are duplicating #N" directive leaks into EVERY later proposal in the run — including
        drafts in unrelated regions — permanently mis-steering the researcher away from a direction
        the operator never banned."""
        prev = getattr(self.researcher, "_novelty_feedback", "")
        setattr(self.researcher, "_novelty_feedback", hint)
        try:
            idea2 = repropose()
            if idea2 is not None:
                idea = idea2
        except BudgetExceeded:
            raise
        except Exception:  # noqa: BLE001 — a repropose failure keeps the original idea
            pass
        finally:
            setattr(self.researcher, "_novelty_feedback", prev)
        return idea

    def _apply_novelty_gate(self, state: RunState, idea: Idea, repropose=None) -> Idea:
        """E1+T5: novelty/dedup gate over fresh proposals, BEFORE any compute is spent.
        Two layers:
        (1) SEMANTIC (T5, ShinkaEvolve `novelty rejection before evaluation`): if the idea TEXT is a
            near-duplicate of an existing node's, reject it — and when a `repropose` callable is
            given, ask the Researcher ONCE more with the duplicate (and its outcome, especially a
            FAILURE) surfaced, so the search learns "you already tried X, it scored Y because Z"
            instead of paying another eval for the same idea.
        (2) NUMERIC (E1 legacy): params within `novelty_epsilon` (normalized L2) of an existing
            node are deterministically nudged off the duplicate.
        Loop-safe (always returns a usable idea) and replay-safe (the final idea lands in
        node_created; the gate is not re-run on replay). Runs when `novelty_gate` is on OR the
        Strategist's novelty stance is "explore" (slice 5): the stance can engage a soft dedup +
        one informed re-propose even when the static gate is off, so novelty pressure follows the
        meta-controller. "balanced"/"exploit" (and gate off) leave this a no-op — exactly as before."""
        mode = getattr(self, "_novelty_mode", "llm")
        # "llm" -> an LLM adjudicates duplication by READING the real experiments (not an embedding/
        # distance heuristic), then re-proposes if it's a dup.
        if mode == "llm":
            return self._llm_novelty_gate(state, idea, repropose)
        # The deterministic "algo" gate below runs when mode is "algo" OR the Strategist's novelty stance
        # is "explore" (the stance can engage a cheap soft dedup + one informed re-propose even when the
        # mode is otherwise off). "off" without explore leaves this a no-op — the Researcher's own
        # read-the-history judgment stands.
        if not (mode == "algo" or self._novelty_stance == "explore"):
            return idea
        import random as _random

        from looplab.events.digest import numeric_params, param_distance

        if self._novelty_semantic:
            dup, sim = self._semantic_duplicate(state, idea)
            if dup is not None:
                outcome = (f"it FAILED ({dup.error_reason}: {(dup.error or '')[:80]})"
                           if dup.status is NodeStatus.failed
                           else f"it scored {dup.metric}")
                self.store.append(EV_NOVELTY_REJECTED, {
                    # prospective id = max+1, not len() (gap-safe; audit only) — see the llm gate above.
                    "node_id": max(state.nodes, default=-1) + 1, "near_node": dup.id, "kind": "semantic",
                    "similarity": round(sim, 4), "stance": self._novelty_stance,
                    "action": "reproposed" if callable(repropose) else "kept"})
                if callable(repropose):
                    hint = (f"\nNOVELTY GATE: your proposal is a near-duplicate of experiment "
                            f"#{dup.id} ('{self._idea_text(dup.idea)[:160]}') — {outcome}. "
                            "Propose something MEANINGFULLY DIFFERENT (another approach, "
                            "component or direction), not a rewording.")
                    idea = self._repropose_with_feedback(repropose, hint, idea)

        params = numeric_params(idea.params)
        if not params:
            return idea

        nearest, mind = None, float("inf")
        for n in state.nodes.values():
            d = param_distance(params, n.idea.params)
            if d < mind:
                mind, nearest = d, n.id
        if mind >= self._novelty_epsilon:
            return idea
        nid = max(state.nodes, default=-1) + 1       # the PROSPECTIVE id (max+1, not len — gap-safe)
        rng = _random.Random(nid * 1009 + 7)        # deterministic per node-slot
        nudged = dict(idea.params)
        for k in params:
            scale = max(abs(params[k]), 1.0) * 0.1
            nudged[k] = round(params[k] + rng.uniform(-1.0, 1.0) * scale, 4)
        self.store.append(EV_NOVELTY_REJECTED, {
            "node_id": nid, "near_node": nearest, "distance": round(mind, 4),
            "stance": self._novelty_stance,
            "original": idea.params, "nudged": nudged})
        out = idea.model_copy()
        out.params = nudged
        out.rationale = (idea.rationale + " [novelty-gate: nudged off a near-duplicate]").strip()
        return out
