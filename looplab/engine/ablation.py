"""Ablation-driven refinement (I7 / A0a, MLE-STAR) for the engine — parameter ablation, code-
block ablation and the `refine_block` child they produce — extracted from orchestrator.py as a
MIXIN: `class Engine(…, AblationMixin)` inherits these methods unchanged, so there is ZERO
call-site churn and `self` here IS the engine. The method bodies are verbatim moves and read
engine attributes freely (store / tracer / run_dir / _write_lock / sandbox / timeout /
researcher / _probe_developer / _implement / _write_assets / _emit_node_created /
_emit_agent_report / _repo_spec / _eval_spec / _ablate_code_blocks), exactly as they did
inside the class.

Layering: no runtime import of the orchestrator (TYPE_CHECKING only) and never serve — only
events, core and stdlib."""
from __future__ import annotations

import threading
import time
from typing import Optional
from uuid import uuid4

import anyio

from looplab.core.models import Idea, durable_idea_payload
from looplab.events.replay import fold
from looplab.events.types import EV_ABLATE


class AblationMixin:
    """The engine's ablation cluster. See the module docstring for the mixin convention
    (`self` is the Engine)."""

    def _ablation_parent_current(self, parent_id: int, generation: int) -> bool:
        state = fold(self.store.read_all())
        parent = state.nodes.get(parent_id)
        return (parent is not None and parent.attempt == generation
                and not parent.tombstoned and parent_id not in state.aborted_nodes)

    async def _run_ablation_probe(self, code: str, workdir, parent_id: int, generation: int):
        """Run one off-tree probe while watching the parent lifecycle.

        Ablation used to check for reset/abort only *after* ``sandbox.run`` returned.  A stale
        result could not enter the tree, but an expensive subprocess kept consuming resources all
        the way to its timeout.  The normal evaluation path already has this cooperative kill
        seam; ablation needs the same guarantee because its probes are real sandbox executions.
        """
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
                code, str(workdir), self.timeout, cancel=cancel)

        async with anyio.create_task_group() as tg:
            tg.start_soon(_watch_parent)
            result = await anyio.to_thread.run_sync(_run)
            cancel.set()
            tg.cancel_scope.cancel()
        return result

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
        abl_seconds = 0.0                       # P1-2: sum the probe wall-clock so it's budgeted
        superseded = False
        with self.tracer.span("ablate", new_trace=True, node_id=parent_id):
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
                _t0 = time.monotonic()
                res = await self._run_ablation_probe(
                    code, workdir, parent_id, generation)
                abl_seconds += time.monotonic() - _t0
                if not self._ablation_parent_current(parent_id, generation):
                    superseded = True
                if res.metric is not None and res.exit_code == 0 and not res.timed_out:
                    impacts[p] = abs(res.metric - base)
                if superseded:
                    break
        async with self._write_lock:
            # Record the probes' eval cost on the event so the fold counts it against max_eval_seconds
            # (arch-review §4 P1-2 — ablation used to spend entirely outside the cumulative accounting).
            self.store.append(EV_ABLATE, {
                "parent_id": parent_id, "generation": generation,
                "ablation_id": ablation_id, "impacts": impacts,
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
                    concept_mode="delta", concepts_added=[], concepts_removed=[])
        # §1: a standing operator directive must steer the ablation-produced refine_block code too —
        # this is a real tree-entering node built from an idea, exactly like the improve/merge sites
        # that already thread _directed_idea (the signal_delivery registry lists the Developer as a
        # consumer, so skipping it here would silently drop the directive for every ablation child).
        code = self._implement(self._directed_idea(idea, state), parent)
        if not self._ablation_parent_current(parent_id, generation):
            self._discard_node_build_telemetry()
            return
        # Mint the id AND commit node_created under _id_lock + the node_building-aware ceiling, so an
        # ablation node can't collide with a concurrent parallel build's reserved id (Variant-1).
        with self._id_lock:
            _evs = self.store.read_all()
            node_id = self._node_id_ceiling(_evs, fold(_evs))
            self._emit_node_created(
                node_id=node_id, parent_ids=[parent_id], operator="refine_block",
                idea=durable_idea_payload(idea), code=code,
                files=getattr(self.developer, "last_files", {}) or {},
                parent_generations={str(parent_id): generation})
        if node_id not in fold(self.store.read_all()).nodes:
            self._discard_node_build_telemetry()
            return
        self._emit_agent_report(node_id)
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
        abl_seconds = 0.0                       # P1-2: budget the code-block probes too
        superseded = False
        with self.tracer.span("ablate_code", new_trace=True, node_id=parent_id, blocks=len(blocks)):
            for idx, blk in enumerate(blocks):
                if not self._ablation_parent_current(parent_id, generation):
                    superseded = True
                    break
                ablated = self._comment_block(code, blk)
                workdir = (self.run_dir / "ablate"
                           / f"node_{parent_id}_g{generation}_{ablation_id[:8]}_block_{idx}")
                self._write_assets(workdir)
                _t0 = time.monotonic()
                res = await self._run_ablation_probe(
                    ablated, workdir, parent_id, generation)
                abl_seconds += time.monotonic() - _t0
                if not self._ablation_parent_current(parent_id, generation):
                    superseded = True
                if res.metric is not None and res.exit_code == 0 and not res.timed_out:
                    impacts[str(idx)] = round(abs(res.metric - base), 6)
                else:
                    impacts[str(idx)] = None   # removing this block broke the run => essential block
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
                                         "impacts": impacts,
                                         "mode": "code_blocks", "blocks": len(blocks),
                                         "top_block": top, "eval_seconds": round(abl_seconds, 3),
                                         **({"superseded": True} if superseded else {})})
        if superseded or not self._ablation_parent_current(parent_id, generation):
            return
        top_src = ""
        if top is not None:
            s, e = blocks[int(top)]
            top_src = "\n".join(code.splitlines()[s:e])[:300]
        idea = Idea(operator="refine_block", params=dict(parent.idea.params),
                    rationale=("code-block ablation: refine the highest-impact pipeline block "
                               f"#{top} and keep the rest. Block:\n{top_src}"),
                    concept_mode="delta", concepts_added=[], concepts_removed=[])
        new_code = self._implement(self._directed_idea(idea, state), parent)   # §1: directives (see _ablate)
        if not self._ablation_parent_current(parent_id, generation):
            self._discard_node_build_telemetry()
            return
        # Mint id + commit node_created under _id_lock + the node_building-aware ceiling (Variant-1).
        with self._id_lock:
            _evs = self.store.read_all()
            node_id = self._node_id_ceiling(_evs, fold(_evs))
            self._emit_node_created(
                node_id=node_id, parent_ids=[parent_id], operator="refine_block",
                idea=durable_idea_payload(idea), code=new_code,
                files=getattr(self.developer, "last_files", {}) or {},
                parent_generations={str(parent_id): generation})
        if node_id not in fold(self.store.read_all()).nodes:
            self._discard_node_build_telemetry()
            return
        self._emit_agent_report(node_id)
        self._emit_hypothesis_ranked(node_id, 0)   # consume predictive telemetry for THIS node (see above)
        self._emit_foresight_selected(node_id, 0)
