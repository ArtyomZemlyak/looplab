"""Node-building helpers (idea -> code -> `node_created` payload) — extracted from
orchestrator.py as a MIXIN: `class Engine(NodeBuildMixin, …)` inherits these methods unchanged,
so there is ZERO call-site churn and `self` here IS the engine. Verbatim moves; several are
exercised on bare `Engine.__new__(Engine)` instances by tests, which a mixin preserves.

DELIBERATELY NOT MOVED: `_create_node` / `_rerun_node` / `_create_injected_node` /
`_activate_spec` stay in orchestrator.py — they call the module-global `fold`, which two tests
monkeypatch THROUGH the orchestrator module (`monkeypatch.setattr(orch, "fold", …)`); moving
them would silently detach that seam. This split keeps the fold-callers with the spine and
extracts only the stateless build sub-helpers they call.

Agent-facing deps (`legal_actions`, `_state_brief`, `render_hint_directives`) stay lazy,
method-local imports so monkeypatching through their source modules keeps working."""
from __future__ import annotations

from typing import Optional

from looplab.core.llm_broker import in_llm_lane
from looplab.core.models import Idea, RunState
from looplab.events.types import EV_AGENT_DECISION, EV_NODE_CREATED
from looplab.search.operators import merge_idea

# Sentinel for `_emit_node_created`'s optional payload keys (moved with its only user):
# distinguishes "key not passed" (the key is OMITTED from the event, matching each call site's
# historical payload shape) from a REAL value, including None (e.g. `research_origin=None`
# must still be emitted).
_OMIT = object()


class NodeBuildMixin:
    """The engine's node-building helper cluster. See the module docstring for the mixin
    convention (`self` is the Engine)."""

    def _ensemble_idea(self, parents) -> Idea:
        """A0b: an ensembling/recombination merge — instruct the Developer to combine the parents'
        solutions (stack/average predictions) rather than mean-averaging params. Carries the mean
        params as a safe payload so a Toy/baseline Developer degrades to the legacy mean-merge."""
        base = merge_idea(parents)
        descr = "; ".join(
            f"node {p.id} (metric={p.metric}, params={p.idea.params})"
            + (f": {p.idea.rationale[:120]}" if p.idea.rationale else "")
            for p in parents)
        base.rationale = ("Ensemble/recombine the top solutions into one stronger pipeline "
                          "(e.g. average or stack their predictions, or merge their best components). "
                          f"Parents — {descr}.")
        return base

    @in_llm_lane("build")
    def _agent_next_actions(self, state: RunState) -> list[dict]:
        """Self-driving action selection (Step 5). The unified agent picks the next macro action
        from the pure legal-action gate; forced phases (evaluate-pending / budget / seed) give it
        no discretion. Records an audit-only `agent_decision` (never read by best-selection); the
        chosen action then flows through the SAME bucket logic as the policy path. Falls back to the
        policy's own recommendation on any malformed/abstaining choice — the agent can never escape
        `legal`, so 'follow the right pipeline' is a structural invariant, not prompt obedience."""
        from looplab.search.policy import legal_actions
        # Honor a live node-budget extension (set on self.policy.max_nodes in the run loop) so the
        # agent path and the pure-policy path agree on when the search is allowed to keep going.
        legal = legal_actions(state, self.policy, max_nodes=self.policy.max_nodes)
        if len(legal) <= 1:
            return legal                       # finish ([]), forced evaluate/seed, or single option
        if {a["kind"] for a in legal} == {"evaluate"}:
            return legal                       # forced: evaluate all pending, no discretion
        recommended = next(iter(self.policy.next_actions(state)), None)
        chooser = getattr(self.researcher, "choose_action", None)
        if not callable(chooser):              # defensive: agent_drives_actions implies unified
            return self.policy.next_actions(state)
        from looplab.agents.roles import _state_brief
        from looplab.agents.hints import render_hint_directives
        try:
            brief = _state_brief(state, None)
        except Exception:  # noqa: BLE001 - a brief is advisory; never block on it
            brief = ""
        # Signal-delivery (§1): the pilot picks the next macro action, so a standing operator
        # directive must reach it too — else it can choose an action that fights the directive.
        brief += render_hint_directives(state.pending_hints)
        choice = chooser(state, legal, recommended, brief=brief)
        idx = choice.get("index", -1) if isinstance(choice, dict) else -1
        chosen = legal[idx] if isinstance(idx, int) and 0 <= idx < len(legal) else \
            (recommended if recommended is not None else legal[0])

        def _summ(a: Optional[dict]) -> Optional[dict]:
            if not a:
                return None
            return {"kind": a.get("kind"), "parent_id": a.get("parent_id"),
                    "parent_ids": a.get("parent_ids"), "node_id": a.get("node_id")}

        self.store.append(EV_AGENT_DECISION, {
            "at_node": len(state.nodes),
            "chosen": _summ(chosen),
            "legal": [_summ(a) for a in legal],
            "recommended": _summ(recommended),
            "rationale": (choice.get("rationale", "") if isinstance(choice, dict) else "")[:500],
        })
        return [chosen]

    @in_llm_lane("build")
    def _implement(self, idea, parent=None) -> str:
        """Route an implement through `implement_from(idea, parent)` when the Developer supports it
        and a parent exists — so an IMPROVE/REFINE starts from the parent's actual solution (its
        code/files) and patches it, instead of regenerating everything from the pristine baseline
        (which loses the parent's accumulated edits and burns tokens re-deriving them). Falls back
        to the plain `implement(idea)` for developers that don't take a parent (draft, offline)."""
        impl_from = getattr(self.developer, "implement_from", None)
        if parent is not None and callable(impl_from):
            return impl_from(idea, parent)
        return self.developer.implement(idea)

    def _directed_idea(self, idea, state: RunState):
        """Signal-delivery (§1): fold the active operator directives into the idea HANDED TO THE
        DEVELOPER so a standing directive ("use only sklearn") steers the CODE that gets written,
        not only the proposal (the Researcher already renders directives; the Developer never saw
        them). Returns a COPY with the rendered directive block appended to `rationale` — the field
        every Developer backend renders — so it reaches the innermost developer through ANY wrapper
        chain (the copy rides the data, not a forwarded attribute that a wrapper could drop). The
        ORIGINAL idea, recorded in `node_created`, is untouched, so the audit rationale stays the
        Researcher's own. Nothing to add -> the idea is returned unchanged (identity).

        Also carries the DEVELOPER's own cross-run code-fix lessons (§role-split): the Developer only
        ever sees ITS lessons ("a node failing with X was fixed by …" on similar tasks) — never the
        Researcher's R&D lessons, which ride the proposal prompt instead. Most useful on the repair
        path (`_repair` routes through here), where "what fixed this crash class" is exactly relevant."""
        from looplab.agents.hints import render_hint_directives
        blocks = [b for b in (render_hint_directives(state.pending_hints),
                              self._dev_prior_note_text.strip()) if b]
        if not blocks:
            return idea
        di = idea.model_copy(deep=True)
        di.rationale = ((di.rationale or "") + "\n" + "\n".join(blocks)).strip()
        return di

    @in_llm_lane("build")
    def _repair(self, node, err: str, state: Optional[RunState] = None) -> str:
        """Route a repair through `repair_from(idea, node, error)` when the Developer supports it, so
        the fix is seeded from the FAILING NODE's OWN files — not the shared developer's `last_files`,
        which holds whatever node it built last (a batch builds every node before any eval, so
        `last_files` is almost never the node being repaired). Falls back to `repair(idea, code, err)`.

        §1: when `state` is given, standing operator directives are folded into the idea so the REPAIRED
        code honors them too (consistency with the four build sites); without it the raw idea is used."""
        idea = self._directed_idea(node.idea, state) if state is not None else node.idea
        rf = getattr(self.developer, "repair_from", None)
        if callable(rf):
            return rf(idea, node, err)
        return self.developer.repair(idea, node.code, err)

    def _emit_node_created(self, *, node_id: int, parent_ids: list, operator: str, idea: dict,
                           code: str, files: dict, deleted=_OMIT, research_origin=_OMIT,
                           source=_OMIT, origin=_OMIT, generation=_OMIT,
                           parent_generations=_OMIT, cross_run_receipt=_OMIT) -> None:
        """The single `node_created` emitter for all four creation sites (`_create_node`,
        `_create_injected_node`, `_ablate`, `_ablate_code`). Optional keys default to the
        `_OMIT` sentinel and are LEFT OUT of the payload when not passed — never None-filled —
        so every site emits EXACTLY its historical payload shape (key set AND key order),
        byte-identical event data. Known quirk kept for replay compatibility: the two ablate
        sites emit NO `deleted` key at all (`_create_node` always emits it, `_create_injected_node`
        emits `deleted` + `source` + `origin` but no `research_origin`) — the fold reads every
        optional key with a default, so do not "normalize" the shapes here."""
        data = {"node_id": node_id, "parent_ids": parent_ids, "operator": operator,
                "idea": idea, "code": code, "files": files}
        for k, v in (("deleted", deleted), ("research_origin", research_origin),
                     ("source", source), ("origin", origin), ("generation", generation),
                     ("parent_generations", parent_generations),
                     ("cross_run_receipt", cross_run_receipt)):
            if v is not _OMIT:
                data[k] = v
        self.store.append(EV_NODE_CREATED, data)
