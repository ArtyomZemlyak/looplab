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

from typing import Optional

import anyio

from looplab.core.models import Idea
from looplab.events.replay import fold
from looplab.events.types import EV_ABLATE


class AblationMixin:
    """The engine's ablation cluster. See the module docstring for the mixin convention
    (`self` is the Engine)."""

    async def _ablate(self, parent_id: int) -> None:
        """Ablation-driven refinement (I7, MLE-STAR): probe each parameter's impact by
        setting it to a neutral baseline (0.0) and re-running, then create a
        `refine_block` child that refines only the highest-impact parameter."""
        state = fold(self.store.read_all())
        parent = state.nodes[parent_id]
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
            self.store.append(EV_ABLATE, {"parent_id": parent_id, "impacts": {},
                                         "skipped": "repo_or_eval_spec"})
            return
        # A0a (MLE-STAR): ablate generated *pipeline code blocks*, not just numeric params — the
        # verified higher-leverage refinement. Only when configured AND the parent has real code.
        if self._ablate_code_blocks and parent.code.strip():
            await self._ablate_code(parent_id)
            return
        base = parent.metric if parent.metric is not None else 0.0
        impacts: dict[str, float] = {}
        with self.tracer.span("ablate", new_trace=True, node_id=parent_id):
            for p in sorted(parent.idea.params):
                ablated = parent.idea.model_copy(deep=True)
                ablated.params[p] = 0.0
                workdir = self.run_dir / "ablate" / f"node_{parent_id}_{p}"
                self._write_assets(workdir)
                code = await anyio.to_thread.run_sync(self._probe_developer.implement, ablated)
                res = await anyio.to_thread.run_sync(
                    self.sandbox.run, code, str(workdir), self.timeout)
                if res.metric is not None and res.exit_code == 0 and not res.timed_out:
                    impacts[p] = abs(res.metric - base)
        async with self._write_lock:
            self.store.append(EV_ABLATE, {"parent_id": parent_id, "impacts": impacts})

        top = max(impacts, key=impacts.get) if impacts else (
            sorted(parent.idea.params)[0] if parent.idea.params else None)
        proposal = self.researcher.propose(state, parent)  # refine only `top`
        new_params = dict(parent.idea.params)
        if top is not None and top in proposal.params:
            new_params[top] = proposal.params[top]
        idea = Idea(operator="refine_block", params=new_params,
                    rationale=f"ablation: refine highest-impact '{top}' (impacts={impacts})")
        # §1: a standing operator directive must steer the ablation-produced refine_block code too —
        # this is a real tree-entering node built from an idea, exactly like the improve/merge sites
        # that already thread _directed_idea (the signal_delivery registry lists the Developer as a
        # consumer, so skipping it here would silently drop the directive for every ablation child).
        code = self._implement(self._directed_idea(idea, state), parent)
        node_id = max(fold(self.store.read_all()).nodes, default=-1) + 1
        self._emit_node_created(
            node_id=node_id, parent_ids=[parent_id], operator="refine_block",
            idea=idea.model_dump(mode="json"), code=code,
            files=getattr(self.developer, "last_files", {}) or {})
        self._emit_agent_report(node_id)
        # consume predictive telemetry for THIS node (propose/implement above set it) so it can't leak
        # onto the next created node — same rule as _create_node / _rerun_node.
        self._emit_hypothesis_ranked(node_id)
        self._emit_foresight_selected(node_id)

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

    async def _ablate_code(self, parent_id: int) -> None:
        """A0a code-block ablation → targeted refinement (MLE-STAR, 64% MLE-bench-Lite). Score each
        generated code block's contribution by neutralizing it and measuring the metric delta (a
        block whose removal BREAKS the pipeline is maximally essential), then refine only the
        highest-impact block. Replay-safe: probes are off-tree; only the `ablate` audit event +
        the `refine_block` child enter the log."""
        state = fold(self.store.read_all())
        parent = state.nodes[parent_id]
        code = parent.code
        base = parent.metric if parent.metric is not None else 0.0
        blocks = self._segment_blocks(code)
        impacts: dict[str, Optional[float]] = {}
        with self.tracer.span("ablate_code", new_trace=True, node_id=parent_id, blocks=len(blocks)):
            for idx, blk in enumerate(blocks):
                ablated = self._comment_block(code, blk)
                workdir = self.run_dir / "ablate" / f"node_{parent_id}_block_{idx}"
                self._write_assets(workdir)
                res = await anyio.to_thread.run_sync(
                    self.sandbox.run, ablated, str(workdir), self.timeout)
                if res.metric is not None and res.exit_code == 0 and not res.timed_out:
                    impacts[str(idx)] = round(abs(res.metric - base), 6)
                else:
                    impacts[str(idx)] = None   # removing this block broke the run => essential block

        # Rank: a None (the pipeline broke without it) is the most essential; else the largest delta.
        def _rank(item):
            _k, v = item
            return (1, float("inf")) if v is None else (0, v)
        top = max(impacts.items(), key=_rank)[0] if impacts else None
        async with self._write_lock:
            self.store.append(EV_ABLATE, {"parent_id": parent_id, "impacts": impacts,
                                         "mode": "code_blocks", "blocks": len(blocks),
                                         "top_block": top})
        top_src = ""
        if top is not None:
            s, e = blocks[int(top)]
            top_src = "\n".join(code.splitlines()[s:e])[:300]
        idea = Idea(operator="refine_block", params=dict(parent.idea.params),
                    rationale=("code-block ablation: refine the highest-impact pipeline block "
                               f"#{top} and keep the rest. Block:\n{top_src}"))
        new_code = self._implement(self._directed_idea(idea, state), parent)   # §1: directives (see _ablate)
        node_id = max(fold(self.store.read_all()).nodes, default=-1) + 1
        self._emit_node_created(
            node_id=node_id, parent_ids=[parent_id], operator="refine_block",
            idea=idea.model_dump(mode="json"), code=new_code,
            files=getattr(self.developer, "last_files", {}) or {})
        self._emit_agent_report(node_id)
        self._emit_hypothesis_ranked(node_id)   # consume predictive telemetry for THIS node (see above)
        self._emit_foresight_selected(node_id)
