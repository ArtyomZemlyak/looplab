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

from looplab.core.errors import ConfigRefusal
from looplab.core.models import RunState
from looplab.engine.triage import _holdout_indices
from looplab.events.types import EV_HOLDOUT_EVALUATED

if TYPE_CHECKING:  # engine type hint only — no runtime import of the orchestrator
    from looplab.engine.orchestrator import Engine


def _candidate_output(workdir, name: str, default: str) -> Optional[Path]:
    """Resolve a CANDIDATE-WRITTEN output file inside `workdir`, or None if it can't be trusted.

    This is a host confused-deputy boundary: the file is named by the task config but WRITTEN by
    untrusted candidate code, and both `Path.resolve()` and `Path.is_file()` follow symlinks. Without
    this guard a candidate could plant `submission.csv -> <data_dir>/prepared/private/test.csv` and
    have the HOST grader read the answer key as the submission — defeating the whole point of
    out-of-process grading ("there is no answer key to read or self-report", see mlebench.py's
    `host_grading`), or point the generic reader at arbitrary host JSON outside the workdir.

    Refuses: an absolute or `..`-bearing configured name, a symlinked final component, a non-regular
    file, and anything whose RESOLVED path escapes this exact attempt workdir (which also covers a
    symlinked intermediate directory, since `resolve()` follows the whole chain before we compare).
    """
    rel = Path(str(name or default))
    if rel.is_absolute() or ".." in rel.parts:
        return None
    wd = Path(workdir).resolve()
    raw = Path(workdir) / rel
    if raw.is_symlink():                       # lstat — does NOT follow, unlike is_file() below
        return None
    resolved = raw.resolve()
    if wd != resolved and wd not in resolved.parents:
        return None
    if not resolved.is_file():
        return None
    return resolved


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
        # into the candidate workdir). The official score replaces any self-report. The medal /
        # above-median report is written to `mlebench_report.json` and read from there by the trust
        # panel and the final report — NOT to `res.extra_metrics`, which is a typed dict[str, float]
        # and feeds the node's Pareto objectives (see the branch's own note below). This comment used
        # to say extra_metrics, sending anyone looking for medal data to the wrong place.
        if g.get("kind") == "mlebench":
            from looplab.adapters.mlebench_grade import (grade_in_subprocess,
                                                         grade_search_split_in_subprocess)
            # Resolve so the grader subprocess (run from the repo root) reads the submission from the
            # node workdir regardless of whether run_dir was relative — but through the confused-deputy
            # guard, so a candidate-planted symlink can't aim the HOST grader at the private answer CSV.
            sub = _candidate_output(workdir, g.get("submission", ""), "submission.csv")
            answers = getattr(self._e, "_search_answers", None)
            if self._e._holdout_idx and answers:
                # THE SEARCH PROTOCOL (doc 52 §5.1 row 3; AIRA₂'s D_search). The number the search
                # sees is the HIDDEN slice's score under the competition's own grader; the private
                # answers are not consulted, no medal report is written, and `holdout_phase` grades
                # the search champion ONCE at finish. Until 2026-09-06 this branch graded every node
                # on the private answers, so the champion was a max over N private draws.
                res.metric = (grade_search_split_in_subprocess(
                    g["competition"], sub, answers, self._e._search_hidden_ids, g.get("data_dir"),
                    timeout=float(g.get("timeout", 300.0))) if sub is not None else None)
                return res
            # LEGACY PROTOCOL — `holdout_fraction=0`, recorded as `host_grading.protocol =
            # "private_per_node"`: every node graded on the private answers. Explicit, never a
            # silent fallback (an undecidable split REFUSES the run instead; see
            # `apply_search_split`).
            metric, report = (None, None)
            if sub is not None:
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
        # Same confused-deputy boundary as the MLE branch above: without the guard a candidate can
        # symlink this at host JSON outside the workdir. (The separate FRESHNESS gap — a clean no-op
        # repair promoting predictions left by an abandoned attempt — needs a per-attempt eval-start
        # fence and is not addressed here.)
        preds_path = _candidate_output(workdir, g.get("predictions", ""), "predictions.json")
        m = None
        if preds_path is not None:
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

    def _public_assets(self) -> dict:
        """The assets as the task handed them over — never the carved ones, or a re-carve on re-entry
        would draw over a shrunken train file and name different rows than the launch did."""
        public = getattr(self._e, "_assets_public", None)
        return public if public is not None else (self._e._assets or {})

    def build_holdout_idx(self, fraction: float, epoch: int = 0) -> frozenset:
        """D1: the reserved holdout partition for a given fraction (+ search epoch, P0-2), or empty
        when holdout doesn't apply (no host grader, non-list labels, or fraction<=0).

        For real MLE-bench the partition is the SEARCH SPLIT over the public train rows (doc 52
        §5.1 row 3): until 2026-09-06 this returned empty for the kind, which is exactly how the
        search came to hill-climb the private grade."""
        if self._e._host_grader is None or float(fraction) <= 0:
            return frozenset()
        if self._e._host_grader.get("kind") == "mlebench":
            from looplab.adapters import mlebench_split
            n = mlebench_split.train_row_count(self._public_assets())
            return _holdout_indices(n, float(fraction), epoch) if n >= 2 else frozenset()
        from looplab.runtime.command_eval import _LABEL_KEYS, _as_list
        yt = _as_list(self._e._host_grader.get("labels"), self._e._host_grader.get("key"), _LABEL_KEYS)
        if isinstance(yt, list) and len(yt) >= 2:
            return _holdout_indices(len(yt), float(fraction), epoch)
        return frozenset()

    def apply_search_split(self) -> None:
        """Carve the pinned partition out of the PUBLIC assets for a real MLE-bench run: what every
        node is materialized from becomes the carved files, and the engine keeps the hidden slice's
        answers (`_search_answers`) and ids (`_search_hidden_ids`) in memory. Called wherever
        `_holdout_idx` is (re)built — `Engine.__init__`, the in-process epoch rebuild, `_reentry_repin`
        — so a reopened run re-carves the epoch's own split from the original files.

        A slice that cannot be carved in the private format is a `ConfigRefusal` at run start, naming
        the two ways out; it is never a silent fall-through to grading on the private answers, because
        that is the defect this exists to end and the log must be able to tell the protocols apart."""
        e = self._e
        g = e._host_grader
        if not g or g.get("kind") != "mlebench":
            return
        if getattr(e, "_assets_public", None) is None:
            e._assets_public = dict(e._assets or {})
        public = e._assets_public
        if not e._holdout_idx:
            e._assets = dict(public)
            e._search_answers = None
            e._search_hidden_ids = frozenset()
            return
        from looplab.adapters import mlebench_split
        try:
            carved = mlebench_split.carve(public, e._holdout_idx)
        except mlebench_split.SplitUndecidable as exc:
            raise ConfigRefusal(
                f"MLE-bench competition {g.get('competition')!r}: the search split cannot be carved "
                f"from the public files ({exc}). The search may not be scored on the private answers "
                "(doc 52 row 3), so either set holdout_fraction=0 to run the explicit legacy "
                "protocol — every node graded on the private answers, recorded as "
                "`host_grading.protocol = private_per_node` — or run a competition whose "
                "train/test/sample_submission layout decides the answers' format.") from exc
        e._assets = carved.assets
        e._search_answers = carved.answers_csv
        e._search_hidden_ids = frozenset(carved.hidden_ids)

    def holdout_topk(self, state: RunState) -> list[int]:
        """The val-leaders that get a holdout evaluation: top-k feasible by the robust search
        metric (confirmed mean when the confirm phase ran, else the single metric). EXCLUDES
        trust-gate-flagged nodes under gate/block — exactly as fold's holdout pick does — so a
        flagged node can't consume a holdout slot the legitimate runner-up needs (else, under
        `gate`, the winner is flagged, fold drops it from the holdout pool, and no clean node ever
        received a holdout eval → the discipline silently no-ops)."""
        from looplab.core.fitness import SearchFitness
        from looplab.events.replay import promotion_eligible_nodes
        # Same ranked-scalar key as the fold's mean pick — `promotion_key` owns the `(robust_metric, id)`
        # tuple (plus the R1-c verifier tie-break slot when `select_verifier` is on) so this holdout-slot
        # ranking can't drift from `_select_best`. Crucially, with the verifier tie-break on, a
        # robust_metric-tied but verifier-PREFERRED leader must not be denied a holdout slot (else the
        # holdout override would pick a winner from a pool that excluded the sound node — the very node the
        # mean pick chose). Byte-identical to `selection_key` when the flag is off. The pool base
        # (feasible_nodes + flagged) is a different-but-agreeing spelling of the same eligibility.
        fit = SearchFitness(state.direction, verifier_tiebreak=state.select_verifier_tiebreak)
        pool = fit.rank_promotion(promotion_eligible_nodes(state))
        if self._e._host_grader is not None and self._e._host_grader.get("kind") == "mlebench":
            # ONE private grade, by protocol: the search champion alone. Grading the top-k and
            # letting `holdout_select` pick among them is the test-selection the split exists to
            # end, one order of magnitude smaller.
            return [n.id for n in pool[:1]]
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
            if g.get("kind") == "mlebench":
                # THE ONE PRIVATE GRADE (doc 52 §5.1 row 3): the search champion's public-test rows
                # against the private answers, once, at finish — its medal report beside it.
                m, gap = self._private_grade(n, state)
                async with self._e._write_lock:
                    self._e.store.append(EV_HOLDOUT_EVALUATED, {
                        "node_id": nid, "generation": n.attempt,
                        "search_epoch": state.search_epoch,
                        "metric": m, "gap": gap,
                        "n_holdout": len(self._e._search_hidden_ids),
                        "protocol": "private_grade"})
                continue
            preds = None
            # Same host confused-deputy boundary as `apply_host_grade` — this file is named by the
            # task config but WRITTEN by untrusted candidate code, and `read_text` follows symlinks.
            # This reader is if anything the more sensitive of the two: under `holdout_select` its
            # score picks the champion on the unseen partition, and it runs at finish where no
            # reward-hack tell re-fires. It was building the path by hand, so a planted
            # `predictions.json -> <data_dir>/prepared/private/test.csv` was refused by the sibling
            # and then read here.
            p = _candidate_output(
                self._e.run_dir / "nodes" / f"node_{nid}",
                g.get("predictions", ""), "predictions.json")
            try:
                preds = (_json.loads(p.read_text(encoding="utf-8-sig", errors="replace"))
                         if p is not None else None)
            except (OSError, ValueError):
                preds = None
            m = self._e._host_score_split(preds, g, holdout=True) if preds is not None else None
            gap = None
            if m is not None and n.metric is not None:
                gap = (n.metric - m) if state.direction == "max" else (m - n.metric)
            async with self._e._write_lock:
                self._e.store.append(EV_HOLDOUT_EVALUATED, {
                    "node_id": nid, "generation": n.attempt,
                    "search_epoch": state.search_epoch,
                    "metric": m, "gap": gap,
                    "n_holdout": len(self._e._holdout_idx)})

    def _private_grade(self, n, state: RunState) -> tuple:
        """Grade ONE node's submission, restricted to the public test ids, against the private
        answers; write its official report beside it. `(metric, gap)`, gap = search score minus
        private grade in the run's direction (how much better the search signal looked)."""
        import json as _json
        import shutil
        import tempfile

        from looplab.adapters.mlebench_grade import grade_in_subprocess
        from looplab.adapters.mlebench_split import filter_submission
        g = self._e._host_grader
        workdir = self._e.run_dir / "nodes" / f"node_{n.id}"
        sub = _candidate_output(workdir, g.get("submission", ""), "submission.csv")
        metric, report = None, None
        if sub is not None:
            tmp = Path(tempfile.mkdtemp(prefix="looplab-private-grade-"))
            try:
                public_only = tmp / "submission.csv"
                try:
                    public_only.write_text(filter_submission(
                        sub.read_text(encoding="utf-8-sig", errors="replace"),
                        self._e._search_hidden_ids, keep=False), encoding="utf-8")
                except (OSError, ValueError):
                    public_only = None
                if public_only is not None:
                    metric, report = grade_in_subprocess(
                        g["competition"], public_only, g.get("data_dir"),
                        timeout=float(g.get("timeout", 300.0)))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        if report is not None:
            try:
                (workdir / "mlebench_report.json").write_text(_json.dumps(report), encoding="utf-8")
            except OSError:
                pass
        gap = None
        if metric is not None and n.metric is not None:
            gap = (n.metric - metric) if state.direction == "max" else (metric - n.metric)
        return metric, gap

