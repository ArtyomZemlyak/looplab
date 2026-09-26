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

import threading
import time
from typing import Optional
from uuid import uuid4

import anyio

from looplab.core.containment import contain
from looplab.core.llm_broker import in_llm_lane
from looplab.core.models import Idea, durable_idea_payload
from looplab.engine.card_reservation import scored_anchor
# Through the ENGINE's fold seam, not `replay.fold` directly — see `shared.py::engine_fold`.
from looplab.engine.shared import engine_fold as fold
from looplab.events.types import EV_ABLATE
from looplab.runtime.sandbox import GpuPinUnenforceable


# A BOUNDED wait for a probe's eval resource, per probe — the noise floor's bound
# (`noise_floor.py::_NOISE_RESOURCE_TICKS`: 120 ticks of the 0.5 s resource condition) and for the
# same reason. An ablation runs on the MAIN task, and the host GPU-pool lease is one file per OS
# user (`engine/resources.py`), so a co-hosted run can hold it for hours; an unbounded wait here
# would freeze the run loop behind it. A probe that does not get its device inside the bound
# ABSTAINS (see `_timed_ablation_probe`), and the pass stops at the first abstention.
_ABLATION_RESOURCE_TICKS = 120


def _signed_gain(probe_metric: float, base: float, direction: str) -> float:
    """How much BETTER the objective measured with the component removed: `probe - base` on a
    maximized metric, `base - probe` on a minimized one. Positive means the run did better without
    it; negative, that it needed it."""
    return (probe_metric - base) if direction == "max" else (base - probe_metric)


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
        impacts: dict[str, float] = {}
        # THE DIRECTION THE SENSITIVITY THROWS AWAY (doc 67 67.4): `impacts` is MLE-STAR's `|Δ|`,
        # which is how the digest, the UI and the narrative read it — and it cannot tell a component
        # the run is better WITHOUT from one it cannot do without. Recorded beside it, never in its
        # place: `_signed_gain` below, positive when the probe measured the objective BETTER with
        # the component removed. Nothing reads it to decide yet.
        signed_impacts: dict[str, float] = {}
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
                    impacts[p] = abs(res.metric - base)
                    signed_impacts[p] = _signed_gain(res.metric, base, state.direction)
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

        top = max(impacts, key=impacts.get) if impacts else (
            sorted(parent.idea.params)[0] if parent.idea.params else None)
        proposal = self.researcher.propose(state, parent)  # refine only `top`
        if not self._ablation_parent_current(parent_id, generation):
            self._discard_node_build_telemetry()
            return
        new_params = dict(parent.idea.params)
        if top is not None and top in proposal.params:
            new_params[top] = proposal.params[top]
        idea = Idea(operator="refine_block", params=new_params,
                    rationale=f"ablation: refine highest-impact '{top}' (impacts={impacts})",
                    footprint=proposal.footprint,
                    concept_mode="delta", concepts_added=[], concepts_removed=[])
        self._build_refine_block_child(parent, parent_id, generation, idea, state)

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

    @staticmethod
    def _segment_blocks(code: str) -> list[tuple[int, int]]:
        """A0a: split solution code into blank-line-separated paragraph blocks -> (start,end) line
        ranges (end exclusive). Deterministic; the unit of code-block ablation (an ML-pipeline
        component: data prep / feature-eng / model / loss / ensembling tends to be one paragraph)."""
        lines = code.splitlines()
        blocks: list[tuple[int, int]] = []
        i, n = 0, len(lines)
        while i < n:
            if lines[i].strip() == "":
                i += 1
                continue
            j = i
            while j < n and lines[j].strip() != "":
                j += 1
            blocks.append((i, j))
            i = j
        return blocks

    @staticmethod
    def _comment_block(code: str, block: tuple[int, int]) -> str:
        """Neutralize one block by commenting its lines out (the ablation), keeping the rest intact."""
        s, e = block
        lines = code.splitlines()
        for k in range(s, e):
            lines[k] = "# [ablated] " + lines[k]
        return "\n".join(lines) + "\n"

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
        # without the block, so there is no measured objective to sign).
        signed_impacts: dict[str, Optional[float]] = {}
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
                if res.metric is not None and res.exit_code == 0 and not res.timed_out:
                    impacts[str(idx)] = round(abs(res.metric - base), 6)
                    signed_impacts[str(idx)] = round(_signed_gain(res.metric, base, state.direction), 6)
                else:
                    impacts[str(idx)] = None   # removing this block broke the run => essential block
                    signed_impacts[str(idx)] = None
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
        top_src = ""
        if top is not None:
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
