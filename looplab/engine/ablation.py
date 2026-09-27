"""Ablation-driven refinement (I7 / A0a, MLE-STAR) for the engine — parameter ablation, code-
block ablation and the `refine_block` child they produce — extracted from orchestrator.py as a
MIXIN: `class Engine(…, AblationMixin)` inherits these methods unchanged, so there is ZERO
call-site churn and `self` here IS the engine. The method bodies are verbatim moves and read
engine attributes freely (store / tracer / run_dir / _write_lock / sandbox / timeout /
researcher / _probe_developer / _implement / _write_assets / _emit_node_created /
_emit_agent_report / _repo_spec / _eval_spec / _ablate_code_blocks), exactly as they did
inside the class.

Layering: no runtime import of the orchestrator (TYPE_CHECKING only) and never serve — only
events, core, `runtime.sandbox` (the `GpuPinUnenforceable` a probe launch can refuse with, the
import `noise_floor.py` has for the same reason) and stdlib."""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional
from uuid import uuid4

import anyio

from looplab.core.code_blocks import (built_as_cut_of, code_blocks, comment_block,
                                     cut_spent)
from looplab.core.containment import contain
from looplab.core.fitness import is_usable_metric
from looplab.core.llm_broker import in_llm_lane
from looplab.core.models import Idea, durable_idea_payload
from looplab.engine.card_reservation import RESERVATION_RACES, scored_anchor
# Through the ENGINE's fold seam, not `replay.fold` directly — see `shared.py::engine_fold`.
from looplab.engine.shared import engine_fold as fold
from looplab.events.types import EV_ABLATE, EV_NODE_BUILDING, EV_NODE_FAILED
from looplab.runtime.sandbox import GpuPinUnenforceable
from looplab.search.policy import simplify_actions


# A BOUNDED wait for a probe's eval resource, per probe — the noise floor's bound
# (`noise_floor.py::_NOISE_RESOURCE_TICKS`: 120 ticks of the 0.5 s resource condition) and for the
# same reason. An ablation runs on the MAIN task, and the host GPU-pool lease is one file per OS
# user (`engine/resources.py`), so a co-hosted run can hold it for hours; an unbounded wait here
# would freeze the run loop behind it. A probe that does not get its device inside the bound
# ABSTAINS (see `_timed_ablation_probe`), and the pass stops at the first abstention.
_ABLATION_RESOURCE_TICKS = 120

_log = logging.getLogger(__name__)

# How many reservation RACES one simplify nomination may lose before it is spent for the process
# (doc 67 67.5). A race is the world moving under the reservation, and the next turn re-decides it —
# the inject path's answer — but the simplify branch `continue`s above the create lane's runaway
# guard, so its retries carry their own bound (`AblationMixin._simplify`).
_SIMPLIFY_RACE_RETRIES = 3


def _signed_gain(probe_metric: float, base: float, direction: str) -> float:
    """How much BETTER the objective measured with the component removed: `probe - base` on a
    maximized metric, `base - probe` on a minimized one. Positive means the run did better without
    it; negative, that it needed it."""
    return (probe_metric - base) if direction == "max" else (base - probe_metric)


# How many probes the refiner's note names, largest effect first. A `params` dict is small in every
# task this engine has run; the bound is there so a wide one cannot turn one cue into a page.
_PROBE_NOTE_MAX = 12


def ablation_probe_note(parent_id: int, top, signed: dict, direction: str) -> str:
    """The probes' results, SIGNED, for the ONE proposal that refines `top` (doc 67 67.4) — the
    `_ablation_probe_hint` cue under `Settings.ablation_probe_hint`; "" when nothing was measured.

    Each probe re-ran the node with one parameter set to 0.0, and `signed` is `_signed_gain` per
    parameter: positive when the run did BETTER without it. The sensitivity `|Δ|` that picks `top`
    cannot say which way a parameter pulls, and the refiner used to be asked for a value blind to
    every one of these numbers — including a probe that beat the node, which is kept nowhere."""
    signed = {name: gain for name, gain in (signed or {}).items() if is_usable_metric(gain)}
    if not signed:
        return ""        # nothing measured against a measured node: nothing to tell the refiner
    order = sorted(signed, key=lambda name: (-abs(signed[name]), str(name)))
    parts = []
    for name in order[:_PROBE_NOTE_MAX]:
        gain = signed[name]
        parts.append(f"{name}: BETTER without it ({gain:+.6g})" if gain > 0 else
                     f"{name}: needed ({gain:+.6g} without it)" if gain < 0 else
                     f"{name}: no measured effect")
    rest = len(order) - _PROBE_NOTE_MAX
    return (f"\nABLATION PROBES ON NODE {parent_id}: each re-ran it with ONE parameter set to 0.0; "
            f"the objective is {direction}imized, and each number is how much better the run did "
            f"without that parameter (negative: worse). " + "; ".join(parts)
            + (f"; and {rest} more" if rest > 0 else "")
            + f". You are refining '{top}' now: choose its value in light of these. A probe that did "
            "better than the node is not kept as a node.")


class AblationMixin:
    """The engine's ablation cluster. See the module docstring for the mixin convention
    (`self` is the Engine)."""

    def _ablation_parent_current(self, parent_id: int, generation: int) -> bool:
        state = fold(self.store.read_all())
        parent = state.nodes.get(parent_id)
        return (parent is not None and parent.attempt == generation
                and not parent.tombstoned and parent_id not in state.aborted_nodes)

    async def _reserve_ablation_probe(self, parent_id: int, generation: int) -> Optional[dict]:
        """The eval resource ONE probe launches under, or None — the parent's lifecycle moved
        during the wait, or the bounded wait (`_ABLATION_RESOURCE_TICKS`) ran out.

        Reserved for the PARENT: a probe is the parent's own program with one parameter neutralised
        or one block commented out, so it needs what the parent's eval needed, under the parent's
        Card pin. The pin is read ONCE, as `noise_floor.py::_run_noise_seed` reads it and for its
        reason — the wait is bounded, and a re-pin landing inside it is honoured by the next probe's
        reservation instead of by a whole-log fold per tick.
        """
        state = fold(self.store.read_all())
        parent = state.nodes.get(parent_id)
        if parent is None:
            return None
        pin = self._card_resource_pin_for_node(state, parent)
        for _ in range(_ABLATION_RESOURCE_TICKS):
            if not self._ablation_parent_current(parent_id, generation):
                return None
            reservation = await self._wait_reserve_node_resources(
                parent, resource_pin=pin, wait_once=True)
            if reservation is not None:
                return reservation
        return None

    async def _run_ablation_probe(self, code: str, workdir, parent_id: int, generation: int, *,
                                  reservation: dict):
        """Run one off-tree probe while watching the parent lifecycle.

        Ablation used to check for reset/abort only *after* ``sandbox.run`` returned.  A stale
        result could not enter the tree, but an expensive subprocess kept consuming resources all
        the way to its timeout.  The normal evaluation path already has this cooperative kill
        seam; ablation needs the same guarantee because its probes are real sandbox executions.

        FENCED LIKE EVERY OTHER EVAL LAUNCH (review 2026-09-22, ENG2-10). Until then this called
        ``self.sandbox.run(code, workdir, timeout, cancel=…)`` with NO env, so a probe — the
        candidate's own code, a real subprocess — ran without the read fence, the kernel rungs
        (`landlock`, `syscall_fence`), the GPU pin and the declared `eval_env`; and because the
        fence's WRITE rule is what keeps a candidate out of the run record, a probe could append to
        the `events.jsonl` two directories above its workdir. The env is now built the way the
        solution path's own eval builds it (`evaluate.py`'s `a.eval_env`, then
        `eval_dispatch.py::_run_eval`'s `_declared_eval_env`): the reservation's pinned env
        through `_resource_eval_env`, which stamps the fence markers, with the run- and task-level
        declared environment on top. `reservation` is REQUIRED so no caller can launch a probe
        without having decided what it runs on; `_timed_ablation_probe` owns it and releases it.

        Returns None when the launch was REFUSED for a GPU pin the runtime cannot enforce: that
        ends this probe, never the run (ablation runs on the main task, where a raise would abort
        the loop and re-raise on every resume), and the caller reads None as "never ran".
        """
        try:
            env = self._declared_eval_env(
                self._resource_eval_env(reservation, inherit_host=True), self._eval_spec)
        except GpuPinUnenforceable as exc:
            contain("ablation_probe_unpinnable", exc)
            return None
        cancel = threading.Event()

        async def _watch_parent() -> None:
            while not cancel.is_set():
                current = await anyio.to_thread.run_sync(
                    self._ablation_parent_current, parent_id, generation)
                if not current:
                    cancel.set()
                    return
                # 1.0s, not 0.1s (F26): each check re-folds the whole event log; 10x/s per probe was
                # O(total-events) CPU scaling with run length. First check runs before the sleep, so
                # the ~1s supersede-cancel latency never delays a fresh probe.
                await anyio.sleep(1.0)

        def _run():
            return self.sandbox.run(
                code, str(workdir), self.timeout, env, cancel=cancel)

        async with anyio.create_task_group() as tg:
            tg.start_soon(_watch_parent)
            try:
                result = await anyio.to_thread.run_sync(_run)
            except GpuPinUnenforceable as exc:
                # The Docker tiers refuse a pin they cannot enforce AT LAUNCH, i.e. here. Caught
                # INSIDE the task group, around the await, for the reason `confirm_phase.py`
                # records: a handler outside it would never match the ExceptionGroup anyio wraps a
                # task-group body error in.
                contain("ablation_probe_unpinnable", exc)
                result = None
            cancel.set()
            tg.cancel_scope.cancel()
        return result

    @in_llm_lane("build")
    async def _ablate(self, parent_id: int, *, expected_generation: Optional[int] = None) -> None:
        """Ablation-driven refinement (I7, MLE-STAR): probe each parameter's impact by
        setting it to a neutral baseline (0.0) and re-running, then create a
        `refine_block` child that refines only the highest-impact parameter."""
        state = fold(self.store.read_all())
        parent = state.nodes.get(parent_id)
        if parent is None or parent.tombstoned or parent_id in state.aborted_nodes:
            return
        generation = parent.attempt
        if expected_generation is not None and expected_generation != generation:
            return
        ablation_id = uuid4().hex
        # Ablation probes run via the solution.py sandbox path (self.sandbox.run on generated
        # code) and seed only assets — they do NOT mount the editable repo or apply node files.
        # For a RepoTask (command-eval) that path is wrong (the repo tree is absent and the
        # baseline developer emits no code), so ablation is a no-op there. Skip cleanly.
        if self._repo_spec or self._eval_spec:
            # Still emit an (empty) ablate event so an operator `force_ablate` request is marked
            # done — otherwise the forced-ablate gate, which waits for an ablate event for this
            # parent, never closes and the loop spins forever on repo/eval-spec runs. The POLICY
            # cadence no longer proposes ablate here (the engine stamps policy.ablation_capable
            # False for repo/eval-spec runs — see orchestrator init), so this path is now reached
            # only via an explicit operator force_ablate; the empty event closes that gate.
            self.store.append(EV_ABLATE, {"parent_id": parent_id, "generation": generation,
                                         "ablation_id": ablation_id,
                                         "impacts": {},
                                         "skipped": "repo_or_eval_spec"})
            return
        # A0a (MLE-STAR): ablate generated *pipeline code blocks*, not just numeric params — the
        # verified higher-leverage refinement. Only when configured AND the parent has real code.
        if self._ablate_code_blocks and parent.code.strip():
            await self._ablate_code(parent_id, generation, ablation_id)
            return
        base = parent.metric if parent.metric is not None else 0.0
        # None per probe when the parent has no measured metric (below): `|probe - 0.0|` is the
        # probe's own magnitude, not an impact (critic 2026-09-26, driven: a forced ablation of a
        # pending parent recorded `{'x': 11.25, 'y': 7.25}` and "refined the highest-impact 'x'").
        impacts: dict[str, Optional[float]] = {}
        # THE DIRECTION THE SENSITIVITY THROWS AWAY (doc 67 67.4): `impacts` is MLE-STAR's `|Δ|`,
        # which is how the digest, the UI and the narrative read it — and it cannot tell a component
        # the run is better WITHOUT from one it cannot do without. Recorded beside it, never in its
        # place: `_signed_gain` below, positive when the probe measured the objective BETTER with
        # the component removed. None for a parent with no measured metric (an operator
        # `force_ablate` on a pending or failed node): the `0.0` the sensitivity falls back to is no
        # measurement, and a sign against it is invented (critic 2026-09-26, driven).
        signed_impacts: dict[str, Optional[float]] = {}
        measured_base = is_usable_metric(parent.metric)
        abl_seconds = 0.0                       # P1-2: sum the probe wall-clock so it's budgeted
        superseded = False
        with self.tracer.span(
                "ablate", new_trace=True, node_id=parent_id, generation=generation):
            for p in sorted(parent.idea.params):
                if not self._ablation_parent_current(parent_id, generation):
                    superseded = True
                    break
                ablated = parent.idea.model_copy(deep=True)
                ablated.params[p] = 0.0
                workdir = (self.run_dir / "ablate"
                           / f"node_{parent_id}_g{generation}_{ablation_id[:8]}_{p}")
                self._write_assets(workdir)
                code = await anyio.to_thread.run_sync(self._probe_developer.implement, ablated)
                if not self._ablation_parent_current(parent_id, generation):
                    superseded = True
                    break
                res, seconds, current = await self._timed_ablation_probe(
                    code, workdir, parent_id, generation)
                abl_seconds += seconds
                if not current:
                    superseded = True
                if res is None:
                    # The probe NEVER RAN (see `_timed_ablation_probe`): it says nothing about `p`,
                    # and every later probe would wait out the same bound on the same pool. The
                    # pass stops with what it measured (ENG2-10).
                    break
                if res.metric is not None and res.exit_code == 0 and not res.timed_out:
                    impacts[p] = abs(res.metric - base) if measured_base else None
                    signed_impacts[p] = (_signed_gain(res.metric, base, state.direction)
                                         if measured_base else None)
                if superseded:
                    break
        async with self._write_lock:
            # Record the probes' eval cost on the event so the fold counts it against max_eval_seconds
            # (arch-review §4 P1-2 — ablation used to spend entirely outside the cumulative accounting).
            self.store.append(EV_ABLATE, {
                "parent_id": parent_id, "generation": generation,
                "ablation_id": ablation_id, "impacts": impacts,
                "signed_impacts": signed_impacts,
                "eval_seconds": round(abl_seconds, 3),
                **({"superseded": True} if superseded else {})})

        if superseded or not self._ablation_parent_current(parent_id, generation):
            return

        measured = {name: value for name, value in impacts.items() if value is not None}
        top = max(measured, key=measured.get) if measured else (
            sorted(parent.idea.params)[0] if parent.idea.params else None)
        # The refiner SEES ITS PROBES under `Settings.ablation_probe_hint` (doc 67 67.4): stamped
        # for this one call and cleared after it, so no later proposal inherits a stale note. OFF,
        # nothing is written and the prompt is the historical one, byte for byte.
        note = (ablation_probe_note(parent_id, top, signed_impacts, state.direction)
                if self._ablation_probe_hint else "")
        if note:
            self._stamp_ablation_probe_hint(note)
        try:
            proposal = self.researcher.propose(state, parent)  # refine only `top`
        finally:
            if note:
                self._stamp_ablation_probe_hint("")
        if not self._ablation_parent_current(parent_id, generation):
            self._discard_node_build_telemetry()
            return
        new_params = dict(parent.idea.params)
        if top is not None and top in proposal.params:
            new_params[top] = proposal.params[top]
        # The historical rationale, byte for byte, whenever the PARENT was measured — even when no
        # probe was (critic 2026-09-26: keyed on the probes, a measured parent whose probes all
        # crashed was described as having no metric). Only an unmeasured parent is said to be one.
        rationale = (f"ablation: refine highest-impact '{top}' (impacts={impacts})" if measured_base
                     else f"ablation: node {parent_id} has no measured metric, so no parameter's "
                          f"impact could be measured; refine '{top}'")
        idea = Idea(operator="refine_block", params=new_params, rationale=rationale,
                    footprint=proposal.footprint,
                    concept_mode="delta", concepts_added=[], concepts_removed=[])
        self._build_refine_block_child(parent, parent_id, generation, idea, state)

    def _stamp_ablation_probe_hint(self, note: str) -> None:
        """Set `_ablation_probe_hint` (RESEARCHER_HINT_ATTRS) on the active Researcher. Contained like
        every cue stamp (`proposal_cues.py::_stamp_gpu_budget_hint`) — a role that rejects attribute
        writes must not fail an ablation over a prompt cue — but by NAME: rejecting a write raises
        one of these two, and nothing else here can raise at all."""
        try:
            setattr(self.researcher, "_ablation_probe_hint", note)
        except (AttributeError, TypeError):
            pass

    async def _timed_ablation_probe(self, source: str, workdir, parent_id: int, generation: int):
        """Run ONE off-tree ablation probe and report `(result, seconds, parent_still_current)`.

        The wall-clock comes BACK rather than being accumulated in place because it is budgeted on
        the `ablate` event (P1-2): a probe whose seconds are dropped spends entirely outside
        `max_eval_seconds`. Both loops also have to re-check the parent immediately after the probe —
        it is the longest thing either does, so it is where a supersede is most likely to land.

        `_write_assets` deliberately stays at the call sites: `_ablate` stages the workdir BEFORE
        asking its probe developer to implement the ablated idea, and pulling it in here would move
        that staging after an LLM call for no reason other than symmetry.

        THE PROBE'S EVAL RESOURCE is reserved here, BEFORE the clock starts, and released exactly
        once in a `finally` after it stops (review 2026-09-22, ENG2-10) — the order
        `noise_floor.py::_run_noise_seed` has, because the seconds returned are charged against
        `max_eval_seconds` and a wait for a device is not evaluation. `result` is None when the
        probe NEVER RAN — no resource inside the bound, the parent moved during the wait, or a pin
        the runtime cannot enforce — and `seconds` is then 0.0. That None is a different fact from
        a probe that ran and printed no metric, and both loops keep them apart: code-block ablation
        reads the second as "removing this block broke the run".
        """
        res, seconds = None, 0.0
        reservation = await self._reserve_ablation_probe(parent_id, generation)
        if reservation is not None:
            try:
                started = time.monotonic()
                res = await self._run_ablation_probe(source, workdir, parent_id, generation,
                                                     reservation=reservation)
                seconds = time.monotonic() - started
            finally:
                self._release_gpus(reservation.get("gpu_ids"))
        return (res, seconds, self._ablation_parent_current(parent_id, generation))

    def _build_refine_block_child(self, parent, parent_id: int, generation: int, idea, state) -> None:
        """Reserve → implement → emit the ONE `refine_block` child an ablation produces.

        Identical for both ablation modes (doc 25 EC-06): `_ablate` and `_ablate_code` differ only in
        how they SCORE and how they build `idea`, and everything from the reservation onward was
        verbatim in both. That tail carries three abandon paths, and each one has to do TWO things —
        fail or discard the reservation AND drop the developer telemetry. A second copy is exactly
        where one half of one of those pairs goes missing without anything noticing.
        """
        _anchor_id, _anchor_attempt = scored_anchor(state)
        reservation = self._reserve_node_build(
            {
                "kind": "refine_block",
                "parent_id": parent_id,
                "parent_generations": {str(parent_id): generation},
            },
            idea,
            # One fold for both halves of the score fence (card_reservation.scored_anchor).
            scored_against=_anchor_id,
            scored_against_attempt=_anchor_attempt,
            source="engine",
            # `retry_attach` stays OFF (its default). An ablation child is `refine_block`, which the
            # attach resolver refuses anyway — but the flag is a per-call-site AUTHORITY, not a
            # prediction about the operator, and a site that cannot commit an attach must never ask
            # for one. Keeping it off here means renaming/widening the operator later cannot quietly
            # file an engine-authored probe under the Researcher's card.
        )
        if reservation is None:
            self._discard_node_build_telemetry()
            return
        node_id = reservation.node_id
        idea = reservation.idea.model_copy(deep=True)
        # §1: a standing operator directive must steer the ablation-produced refine_block code too —
        # this is a real tree-entering node built from an idea, exactly like the improve/merge sites
        # that already thread _directed_idea (the signal_delivery registry lists the Developer as a
        # consumer, so skipping it here would silently drop the directive for every ablation child).
        built = self._implement_result(
            self._directed_idea(idea.model_copy(deep=True), state), parent, state=state)
        code = built.code                     # the envelope's, never the instance's (doc 52 row 12)
        idea, footprint_finalized = self._finalize_developer_footprint(
            idea, self.developer, code, footprint=built.last_footprint)
        if not self._ablation_parent_current(parent_id, generation):
            self._fail_reserved_build(
                node_id=node_id, card_id=reservation.card_id, generation=0,
                error="parent lifecycle changed while building", reason="superseded")
            self._discard_node_build_telemetry()
            return
        self._emit_node_created(
            node_id=node_id, parent_ids=[parent_id], operator="refine_block",
            idea=durable_idea_payload(idea), code=code,
            files=dict(built.last_files),
            eval_start_boundary=True,
            parent_generations={str(parent_id): generation},
            **({"footprint_finalized": True} if footprint_finalized else {}))
        if node_id not in fold(self.store.read_all()).nodes:
            self._fail_reserved_build(
                node_id=node_id, card_id=reservation.card_id, generation=0,
                error="ablation node creation was rejected during replay", reason="superseded")
            self._discard_node_build_telemetry()
            return
        self._emit_agent_report(node_id, report=built.last_report,
                                audit_extra=built.audit_extra)
        # consume predictive telemetry for THIS node (propose/implement above set it) so it can't leak
        # onto the next created node — same rule as _create_node / _rerun_node.
        self._emit_hypothesis_ranked(node_id, 0)
        self._emit_foresight_selected(node_id, 0)

    def _stamp_simplify(self, state=None) -> None:
        """Tell the policy whether it may nominate simplifications (doc 67 67.5,
        `Settings.ablation_simplify`) and which ones this process declined to build — the
        `ablation_capable` pattern: facts the policy's view does not carry, stamped on the policy at
        launch, on every policy rebuild and whenever they change, read through `getattr`.

        With `state` — the WHOLE fold, once per selection turn (`orchestrator.py::_select_actions`)
        — also which (parent, lifecycle, block) a `simplified` receipt already spent: the Card lane
        hands the policy a view without tombstoned, gated or discarded nodes, where a hidden
        simplification spends nothing (`search/policy.py::simplify_actions`)."""
        self.policy.simplify_ablated = bool(self._ablation_simplify)
        self.policy.simplify_refused = frozenset(self._simplify_refused)
        if state is not None and self._ablation_simplify:
            # Keyed on the parent's CURRENT lifecycle for every cut that is still its program minus
            # that block, or was built as it — the policy's own `taken` rule
            # (`core/code_blocks.py::cut_spent`).
            self.policy.simplify_spent = frozenset(
                (parent.id, parent.attempt, node.simplified["block"])
                for node in state.nodes.values() if isinstance(node.simplified, dict)
                for parent in (state.nodes.get(node.simplified["parent_id"]),)
                if parent is not None and cut_spent(parent, node))

    async def _simplify(self, action: dict) -> None:
        """Build the ONE `simplify` child a recorded code-block ablation nominated (doc 67 67.5).

        The node IS the program the probe ran: the parent's code with pipeline block #`block`
        commented out, re-derived from the parent's own code by the same deterministic pair the
        probe used (`_segment_blocks`, `_comment_block`) — so no Developer is asked, no model is
        paid, and the directive steering a Developer has nothing to steer (the parent was built
        under it). It is reserved and evaluated the ordinary way: the probe only NOMINATED it, and
        whether it stands is the selector's named rule (`events/replay_selection.py::simpler_tie`).

        Re-checked against a fresh fold (`search/policy.py::simplify_actions`, the policy's own
        rule), so a lifecycle a reset replaced, or a block another simplification already spent,
        builds nothing. EVERY way it declines to build is a stamped refusal (`_refuse_simplify`), so
        the policy never proposes that nomination again in this process: a silent return here, on
        a nomination the policy's view kept making, spun the loop (critic 2026-09-27, driven: 1507
        turns in 10 s under the Card lane). The one exception is a reservation RACE
        (`card_reservation.py::RESERVATION_RACES`): the world moved under the reservation and the
        next turn re-decides it, as it does for an operator's inject — `_SIMPLIFY_RACE_RETRIES`
        times, because this branch `continue`s above the create lane's runaway guard and so carries
        its own bound: every turn it takes spends a nomination or one of its retries."""
        state = fold(self.store.read_all())
        parent_id, block = action.get("parent_id"), action.get("block")
        parent = (state.nodes.get(parent_id)
                  if isinstance(parent_id, int) and not isinstance(parent_id, bool) else None)
        if parent is None:
            return      # no node the policy could nominate for again: it nominates from the fold's
        nominated = next((a for a in simplify_actions(state, parent,
                                                      refused=self._simplify_refused)
                          if a["block"] == block), None)
        if nominated is None:
            self._refuse_simplify(parent_id, parent.attempt, block,
                                  "not a nomination on the whole fold (spent, refused or superseded)")
            return
        generation, ablation_id = parent.attempt, nominated["ablation_id"]
        blocks = self._segment_blocks(parent.code)
        if not 0 <= block < len(blocks):
            self._refuse_simplify(parent_id, generation, block, "the parent's code has no such block")
            return
        start, end = blocks[block]
        removed = "\n".join(parent.code.splitlines()[start:end])[:300]
        idea = Idea(operator="simplify", params=dict(parent.idea.params),
                    hypothesis=f"Node {parent_id} without its pipeline block #{block}",
                    rationale=(f"simplify: a code-block ablation ({ablation_id[:8]}) measured node "
                               f"{parent_id} no worse without pipeline block #{block}; this is that "
                               f"program, the block commented out. Block:\n{removed}"),
                    footprint=parent.idea.footprint,
                    concept_mode="delta", concepts_added=[], concepts_removed=[])
        anchor_id, anchor_attempt = scored_anchor(state)
        refusal: list = []
        reservation = self._reserve_node_build(
            {"kind": "simplify", "parent_id": parent_id,
             "parent_generations": {str(parent_id): generation}},
            idea, scored_against=anchor_id, scored_against_attempt=anchor_attempt,
            source="engine", refusal=refusal)
        if reservation is None:
            code = refusal[0] if refusal else None
            key = (parent_id, generation, block)
            # Counted at ONE node count: the bound exists for a loop turning without progress, and a
            # node landing between two races is progress — three transient races hours apart spent
            # the nomination for the rest of the process (critic 2026-09-27, NIT).
            races, at = self._simplify_races.get(key, (0, None))
            if at != len(state.nodes):
                races = 0
            if code in RESERVATION_RACES and races < _SIMPLIFY_RACE_RETRIES:
                self._simplify_races[key] = (races + 1, len(state.nodes))
                _log.info("simplify: node %s without block #%s lost a reservation race (%s); "
                          "the next turn re-decides it", parent_id, block, code)
                return
            self._refuse_simplify(parent_id, generation, block,
                                  f"no reservation ({code or 'refused'})")
            return
        node_id = reservation.node_id
        if not self._ablation_parent_current(parent_id, generation):
            self._fail_reserved_build(
                node_id=node_id, card_id=reservation.card_id, generation=0,
                error="parent lifecycle changed while building", reason="superseded")
            return
        self._emit_node_created(
            node_id=node_id, parent_ids=[parent_id], operator="simplify",
            idea=durable_idea_payload(reservation.idea), code=self._comment_block(
                parent.code, blocks[block]),
            files=dict(parent.files), eval_start_boundary=True,
            parent_generations={str(parent_id): generation},
            simplified={"parent_id": parent_id, "generation": generation, "block": block,
                        "ablation_id": ablation_id})
        if node_id not in fold(self.store.read_all()).nodes:
            self._fail_reserved_build(
                node_id=node_id, card_id=reservation.card_id, generation=0,
                error="simplify node creation was rejected during replay", reason="superseded")
            self._refuse_simplify(parent_id, generation, block, "its node_created was rejected")

    def _rebuild_simplification(self, node) -> None:
        """A `node_reset` of a `simplify` node from "propose" or "implement" (doc 67 67.5): its
        program is its receipt — the parent's code with one block commented out, and the parent's
        files — so it is RE-DERIVED here and no Developer is asked. The Developer rebuild it used to
        get paid a model call, and the row it landed carried no receipt, so the block it had spent
        was nominated again (critic 2026-09-27). A receipt whose parent's program no longer gives
        the cut it was built as — a changed program, or one a reset emptied — rebuilds nothing: the
        node fails `superseded`, as a first build over a moved parent does. A re-derived cut names
        the parent's CURRENT lifecycle. Its own node's rows only (invariant 1: this runs in the
        rerun's build worker)."""
        state = fold(self.store.read_all())
        current = state.nodes.get(node.id)
        if (current is None or current.attempt != node.attempt or current.tombstoned
                or node.id in state.aborted_nodes):
            return
        receipt = current.simplified
        parents = list(current.parent_ids)
        parent = state.nodes.get(parents[0]) if len(parents) == 1 else None
        spans = code_blocks(parent.code or "") if parent is not None else []
        # The parent's CURRENT program still gives the cut this node was built as — not its
        # lifecycle: a re-measurement of the same program moves the attempt and not the cut, and
        # failing the reset there `superseded` threw away a cut that was still exactly the
        # parent minus its block (critic 2026-09-27, NIT) — the policy's own rule for what a cut
        # of this program is (`core/code_blocks.py::built_as_cut_of`).
        if (not isinstance(receipt, dict) or parent is None or receipt["parent_id"] != parent.id
                or not built_as_cut_of(parent, current) or parent.tombstoned
                or parent.id in state.aborted_nodes or not 0 <= receipt["block"] < len(spans)):
            self.store.append(EV_NODE_FAILED, {
                "node_id": current.id, "generation": current.attempt,
                "error": "a simplification whose parent's program no longer gives its cut cannot "
                         "be re-derived", "reason": "superseded", "eval_seconds": 0.0})
            return
        self.store.append(EV_NODE_BUILDING, {
            "node_id": current.id, "generation": current.attempt, "operator": current.operator,
            "parent_ids": parents,
            **({"card_id": current.idea.card_id} if current.idea.card_id else {})})
        self._emit_node_created(
            node_id=current.id, parent_ids=parents, operator="simplify",
            idea=durable_idea_payload(current.idea),
            code=comment_block(parent.code, spans[receipt["block"]]), files=dict(parent.files),
            eval_start_boundary=True, generation=current.attempt,
            parent_generations={str(parent.id): parent.attempt},
            simplified={**receipt, "generation": parent.attempt})

    def _refuse_simplify(self, parent_id: int, generation: int, block: int, why: str) -> None:
        """Spend one nomination for this process (see `_simplify`) and SAY so: a nomination the run
        could not build must not read as one it never made."""
        self._simplify_refused.add((parent_id, generation, block))
        self._stamp_simplify()
        _log.warning("simplify: node %s (lifecycle %s) without block #%s was not built: %s",
                     parent_id, generation, block, why)

    @staticmethod
    def _segment_blocks(code: str) -> list[tuple[int, int]]:
        """A0a: the unit of code-block ablation, `core/code_blocks.py::code_blocks` — ONE spelling,
        which the simplify nomination and the fold's receipt check read too (doc 67 67.5); kept as
        this seam because tests patch it."""
        return code_blocks(code)

    @staticmethod
    def _comment_block(code: str, block: tuple[int, int]) -> str:
        """Neutralize one block by commenting its lines out (the ablation), keeping the rest intact —
        `core/code_blocks.py::comment_block`, the cut the fold checks a `simplified` receipt against."""
        return comment_block(code, block)

    @in_llm_lane("build")
    async def _ablate_code(self, parent_id: int, generation: int, ablation_id: str) -> None:
        """A0a code-block ablation → targeted refinement (MLE-STAR, 64% MLE-bench-Lite). Score each
        generated code block's contribution by neutralizing it and measuring the metric delta (a
        block whose removal BREAKS the pipeline is maximally essential), then refine only the
        highest-impact block. Replay-safe: probes are off-tree; only the `ablate` audit event +
        the `refine_block` child enter the log."""
        state = fold(self.store.read_all())
        parent = state.nodes.get(parent_id)
        if (parent is None or parent.tombstoned or parent.attempt != generation
                or parent_id in state.aborted_nodes):
            return
        code = parent.code
        base = parent.metric if parent.metric is not None else 0.0
        blocks = self._segment_blocks(code)
        impacts: dict[str, Optional[float]] = {}
        # Signed beside the sensitivity, as in `_ablate`; None where `impacts` is None (the run broke
        # without the block, so there is no measured objective to sign), and for a parent with no
        # measured metric.
        signed_impacts: dict[str, Optional[float]] = {}
        measured_base = is_usable_metric(parent.metric)
        abl_seconds = 0.0                       # P1-2: budget the code-block probes too
        superseded = False
        with self.tracer.span(
                "ablate_code", new_trace=True, node_id=parent_id, generation=generation,
                blocks=len(blocks)):
            for idx, blk in enumerate(blocks):
                if not self._ablation_parent_current(parent_id, generation):
                    superseded = True
                    break
                ablated = self._comment_block(code, blk)
                workdir = (self.run_dir / "ablate"
                           / f"node_{parent_id}_g{generation}_{ablation_id[:8]}_block_{idx}")
                # The parent's helper files, as its own eval has them (task assets still win, so
                # they are written after): a probe without them crashed on the first import of one,
                # and measured every block of a multi-file node "essential" — and the `simplify` node
                # a probe nominates is exactly this program, files included (doc 67 67.5, critic
                # 2026-09-27).
                self._write_node_files(parent, workdir)
                self._write_assets(workdir)
                res, seconds, current = await self._timed_ablation_probe(
                    ablated, workdir, parent_id, generation)
                abl_seconds += seconds
                if not current:
                    superseded = True
                if res is None:
                    # NEVER RAN — which is not "removing this block broke the run" (the None
                    # impact below, ranked MOST essential). Recording it that way would elect a
                    # block nobody measured; the pass stops with what it measured (ENG2-10).
                    break
                ran = res.metric is not None and res.exit_code == 0 and not res.timed_out
                if ran and measured_base:
                    impacts[str(idx)] = round(abs(res.metric - base), 6)
                    signed_impacts[str(idx)] = round(
                        _signed_gain(res.metric, base, state.direction), 6)
                elif not ran:
                    impacts[str(idx)] = None   # removing this block broke the run => essential block
                    signed_impacts[str(idx)] = None
                # (A block the run SURVIVED without, under a parent with no measured metric, is not
                # recorded: there is no delta to rank it by, and None here means ESSENTIAL. Only
                # the blocks whose removal broke the run are known, and they alone can win.)
                if superseded:
                    break

        # Rank: a None (the pipeline broke without it) is the most essential; else the largest delta.
        def _rank(item):
            _k, v = item
            return (1, float("inf")) if v is None else (0, v)
        top = max(impacts.items(), key=_rank)[0] if impacts else None
        async with self._write_lock:
            self.store.append(EV_ABLATE, {"parent_id": parent_id, "generation": generation,
                                         "ablation_id": ablation_id,
                                         "impacts": impacts, "signed_impacts": signed_impacts,
                                         "mode": "code_blocks", "blocks": len(blocks),
                                         "top_block": top, "eval_seconds": round(abl_seconds, 3),
                                         **({"superseded": True} if superseded else {})})
        if superseded or not self._ablation_parent_current(parent_id, generation):
            return
        if top is None:
            # Nothing is known about any block — an unmeasured parent whose blocks all survived, or
            # a measured one whose FIRST probe never ran (the ENG2-10 stop above) — so there is
            # nothing to refine: no paid child for "block #None" (critic 2026-09-26, both parents).
            # The `ablate` row above still closes an operator's forced-ablate gate.
            return
        s, e = blocks[int(top)]
        top_src = "\n".join(code.splitlines()[s:e])[:300]
        # Card identity prefers ``hypothesis`` over the richer rationale and requires that seed
        # statement to be bounded printable text.  ``top_src`` is deliberately multiline code, so
        # using the rationale as the implicit statement makes reservation fail and leaves the
        # ablation cadence permanently due.  Keep the diagnostic code in rationale while giving the
        # work item a stable one-line identity.
        idea = Idea(operator="refine_block", params=dict(parent.idea.params),
                    hypothesis=("Refine the highest-impact pipeline block "
                                f"#{top} from node {parent_id}"),
                    rationale=("code-block ablation: refine the highest-impact pipeline block "
                               f"#{top} and keep the rest. Block:\n{top_src}"),
                    footprint=parent.idea.footprint,
                    concept_mode="delta", concepts_added=[], concepts_removed=[])
        self._build_refine_block_child(parent, parent_id, generation, idea, state)
