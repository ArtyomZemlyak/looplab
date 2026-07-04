"""Host-side grading + D1 holdout-gated promotion for the engine (extracted from
orchestrator.py): the B1+ out-of-process grade applied to every eval's predictions file, the
per-partition scoring split, the deterministic holdout partition builder, and the end-of-run
holdout phase that re-scores the val-top-k on the reserved unseen rows.

`HoldoutGrader` wraps the engine instance (`self._e`) rather than owning copies of its state:
the method bodies are verbatim moves from the Engine, reading the engine's knobs/store/grader
through `self._e` and calling sibling cluster methods through the Engine's thin delegators
(so a test monkeypatching e.g. `engine._host_score_split` still intercepts every internal
call). The holdout-owned MUTABLE state (`_holdout_idx`, `_holdout_fraction`, `_holdout_select`,
`_holdout_top_k`) deliberately stays on the Engine: `__init__` and `run()`'s resume block
assign it directly (and tests read `eng._holdout_idx`), so plain attributes are lower churn
than lessons-style property indirection.

Layering: this module must not import the orchestrator (TYPE_CHECKING only) and never imports
serve — it touches only engine.triage, events, core, runtime/adapters (lazily) and stdlib."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

from looplab.core.models import RunState
from looplab.engine.triage import _holdout_indices
from looplab.events.types import EV_HOLDOUT_EVALUATED

if TYPE_CHECKING:  # engine type hint only — no runtime import of the orchestrator
    from looplab.engine.orchestrator import Engine


class HoldoutGrader:
    """The engine's host-grading + holdout cluster. See the module docstring for the
    `self._e` (engine handle) convention."""

    def __init__(self, engine: "Engine") -> None:
        self._e = engine

    def graded_output_name(self) -> Optional[str]:
        """The filename the candidate must write for out-of-process grading (the file
        `_apply_host_grade` scores), or None when grading is in-workdir. Single source of truth
        for the host-grader output name so the host-grading audit event and the critic's
        submission-output check resolve it identically and can't drift."""
        hg = self._e._host_grader
        if not hg:
            return None
        # Mirror `_apply_host_grade` EXACTLY so the name can't drift: real MLE-bench scores the
        # `submission` file; every other host grader scores the `predictions` file.
        if hg.get("kind") == "mlebench":
            return hg.get("submission", "submission.csv")
        return hg.get("predictions", "predictions.json")

    def apply_host_grade(self, res, workdir):
        """B1+ out-of-process grading: read the candidate's predictions file from its workdir and score
        it on the HOST against the held-out labels (held in engine memory, never on the candidate FS).
        Overrides `res.metric`; missing/malformed predictions -> no metric (the node fails, so a
        candidate that doesn't actually produce predictions can't pass)."""
        import json as _json
        from looplab.runtime.command_eval import host_score
        g = self._e._host_grader
        # Real MLE-bench: the candidate writes submission.csv; mle-bench's REAL grader scores it
        # out-of-process against private/test.csv answers (in the mle-bench data dir, never copied
        # into the candidate workdir). The official score replaces any self-report; the medal/
        # above-median report rides along in extra_metrics for the trust panel + final report.
        if g.get("kind") == "mlebench":
            from looplab.adapters.mlebench_grade import grade_in_subprocess
            # Resolve so the grader subprocess (run from the repo root) reads the submission from the
            # node workdir regardless of whether run_dir was relative.
            sub = (Path(workdir) / g.get("submission", "submission.csv")).resolve()
            metric, report = (None, None)
            if sub.is_file():
                metric, report = grade_in_subprocess(
                    g["competition"], sub, g.get("data_dir"),
                    timeout=float(g.get("timeout", 300.0)))
            res.metric = metric
            # The official medal/above-median report is a STRUCTURED dict, not a scalar — it must NOT
            # go into extra_metrics (typed dict[str, float]; the UI treats each value as a numeric
            # Pareto objective). Persist it as a per-node artifact instead: files-as-truth, inspectable.
            if report is not None:
                try:
                    (Path(workdir) / "mlebench_report.json").write_text(
                        _json.dumps(report), encoding="utf-8")
                except OSError:
                    pass
            return res
        preds_path = Path(workdir) / g.get("predictions", "predictions.json")
        m = None
        if preds_path.is_file():
            from looplab.runtime.sandbox import _to_float
            try:
                preds = _json.loads(preds_path.read_text(encoding="utf-8-sig", errors="replace"))
                # D1 holdout: when a holdout partition is reserved, the SEARCH signal is the score
                # on the complement rows only — the holdout rows are scored exactly once, at
                # finish, for the val-top-k (see _holdout_phase). No partition => legacy full score.
                if self._e._holdout_idx:
                    m = self._e._host_score_split(preds, g, holdout=False)
                else:
                    # .get (not g["labels"]): a host_grader() dict missing labels yields metric None
                    # (node fails) rather than an uncaught KeyError that would crash the eval worker.
                    # _to_float: a non-finite (NaN/Inf) host score reads as None so an untrusted candidate
                    # can't self-elect champion via a crafted prediction (mirrors command_eval/sweep paths).
                    m = _to_float(host_score(g.get("scorer", "rmse"), preds, g.get("labels"), key=g.get("key")))
            except (ValueError, OSError):
                m = None
        res.metric = m
        return res

    def host_score_split(self, preds, g: dict, *, holdout: bool) -> Optional[float]:
        """D1: score predictions on ONE side of the holdout partition — the search side
        (complement) for every regular/confirm eval, the holdout side once at finish. Length
        mismatch or an empty side yields None (the node fails / gets no holdout metric), the
        same contract as host_score itself."""
        from looplab.runtime.command_eval import _LABEL_KEYS, _PRED_KEYS, _as_list, host_score
        from looplab.runtime.sandbox import _to_float
        yp = _as_list(preds, g.get("key"), _PRED_KEYS)
        yt = _as_list(g.get("labels"), g.get("key"), _LABEL_KEYS)
        if not isinstance(yp, list) or not isinstance(yt, list) or len(yp) != len(yt):
            return None
        keep = (lambda i: i in self._e._holdout_idx) if holdout else \
               (lambda i: i not in self._e._holdout_idx)
        yp2 = [v for i, v in enumerate(yp) if keep(i)]
        yt2 = [v for i, v in enumerate(yt) if keep(i)]
        if not yt2:
            return None
        return _to_float(host_score(g.get("scorer", "rmse"), yp2, yt2))

    def build_holdout_idx(self, fraction: float) -> frozenset:
        """D1: the reserved holdout partition for a given fraction, or empty when holdout doesn't
        apply (no host grader, real MLE-bench, non-list labels, or fraction<=0)."""
        if (self._e._host_grader is None or self._e._host_grader.get("kind") == "mlebench"
                or float(fraction) <= 0):
            return frozenset()
        from looplab.runtime.command_eval import _LABEL_KEYS, _as_list
        yt = _as_list(self._e._host_grader.get("labels"), self._e._host_grader.get("key"), _LABEL_KEYS)
        if isinstance(yt, list) and len(yt) >= 2:
            return _holdout_indices(len(yt), float(fraction))
        return frozenset()

    def holdout_topk(self, state: RunState) -> list[int]:
        """The val-leaders that get a holdout evaluation: top-k feasible by the robust search
        metric (confirmed mean when the confirm phase ran, else the single metric). EXCLUDES
        trust-gate-flagged nodes under gate/block — exactly as fold's holdout pick does — so a
        flagged node can't consume a holdout slot the legitimate runner-up needs (else, under
        `gate`, the winner is flagged, fold drops it from the holdout pool, and no clean node ever
        received a holdout eval → the discipline silently no-ops)."""
        from looplab.events.replay import flagged_node_ids
        flagged = flagged_node_ids(state)

        def _key(n):
            return ((n.confirmed_mean if n.confirmed_mean is not None else n.metric), n.id)
        pool = sorted((n for n in state.feasible_nodes() if n.id not in flagged),
                      key=_key, reverse=(state.direction == "max"))
        return [n.id for n in pool[: self._e._holdout_top_k]]

    def holdout_pending(self, state: RunState) -> bool:
        if not (self._e._holdout_idx and self._e._host_grader is not None):
            return False
        return any(nid not in state.holdout_evaluated_ids for nid in self._e._holdout_topk(state))

    async def holdout_phase(self, state: RunState) -> None:
        """D1 holdout-gated promotion: re-score the val-top-k's EXISTING predictions on the
        reserved holdout partition (no re-training — free), emit `holdout_evaluated` per node.
        The fold then (a) surfaces the val-holdout generalization gap in the Trust panel and
        (b) under holdout_select picks the champion by the unseen signal among these leaders.
        Replay/resume-safe: gated per node on holdout_evaluated_ids; an event is emitted even
        when the predictions file is gone (metric None) so the gate always closes."""
        import json as _json
        g = self._e._host_grader
        for nid in self._e._holdout_topk(state):
            if nid in state.holdout_evaluated_ids:
                continue
            n = state.nodes[nid]
            preds = None
            p = self._e.run_dir / "nodes" / f"node_{nid}" / g.get("predictions", "predictions.json")
            try:
                preds = _json.loads(p.read_text(encoding="utf-8-sig", errors="replace"))
            except (OSError, ValueError):
                preds = None
            m = self._e._host_score_split(preds, g, holdout=True) if preds is not None else None
            gap = None
            if m is not None and n.metric is not None:
                gap = (n.metric - m) if state.direction == "max" else (m - n.metric)
            async with self._e._write_lock:
                self._e.store.append(EV_HOLDOUT_EVALUATED, {
                    "node_id": nid, "metric": m, "gap": gap,
                    "n_holdout": len(self._e._holdout_idx)})
