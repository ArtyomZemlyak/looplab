"""Run-introspection tools (ADR-7 tool protocol): let the Researcher / DeepResearcher read the
search's OWN experiments and data mid-loop — just-in-time retrieval instead of stuffing everything
into the prompt. Two providers expose `.specs()`/`.execute()` like the knowledge/web tools, and are
run-aware via `bind_state(state, parent)` which the agent loop calls each turn.

Every `execute` returns a STRING and soft-fails (never raises) — a junk tool call must not crash the
run. Long output is additionally truncated by the agent layer (4000 chars).
"""
from __future__ import annotations

import csv
import io
import math
from pathlib import Path
from typing import Optional

from looplab.events import digest
from looplab.core.models import NodeStatus, RunState
from looplab.tools._base import fn_spec
from looplab.tools._runcache import RunStateCache


def _is_number(v: str) -> bool:
    """True only if the string parses as a FINITE number. Rejects the 'nan'/'inf'/'infinity'
    sentinels (which float() happily accepts) so a column of textual missing-markers reads as
    categorical — flagging it as needing missing-value handling — instead of numeric with
    NaN/inf-poisoned (and order-dependent) min/max/mean."""
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _clip(text: str, n: int) -> str:
    """Return the LAST `n` chars of `text` (logs are read tail-first — the end is where the error and
    the final metric line live), flagging how much was dropped off the front."""
    if len(text) <= n:
        return text
    return f"…[+{len(text) - n} earlier chars truncated]\n" + text[-n:]




class RunTools:
    """Read-only view over the live search DAG (the bound `RunState`)."""

    # Logs get a bigger error budget than read_experiment's 300-char slice — but the shared tool loop
    # HEAD-truncates every tool result to 4000 chars (agent.drive_tool_loop), so a larger budget here
    # is not just wasted, it's harmful: content past ~4000 (the error tail — a traceback's exception
    # line lives at the BOTTOM) would be silently cut. Stay under that cap with headroom for the header
    # + section markers, so our own tail-preserving clip is what actually decides what's dropped.
    _LOG_CHARS = 3600

    def __init__(self, max_chars: int = 3500):
        self.max_chars = max_chars
        self.state: Optional[RunState] = None
        self.parent = None

    # The agent loop calls this each turn so the tools see the current run.
    def bind_state(self, state: RunState, parent=None) -> None:
        self.state = state
        self.parent = parent

    def specs(self) -> list[dict]:
        return [
            fn_spec("list_experiments",
                "List experiments tried so far (the search DAG). Use to see what's been done before "
                "proposing. `sort`: best|worst|recent.",
                {"sort": {"type": "string", "enum": ["best", "worst", "recent"]},
                 "limit": {"type": "integer"},
                 "theme": {"type": "string", "description": "filter to one theme slug (optional)"}}),
            fn_spec("read_experiment",
                "Read one experiment's full detail: params, metric, robustness, rationale, failure "
                "reason, extra metrics, and — for a hyperparameter sweep — its trials. `trials` "
                "chooses how many sweep points to return: a number like '20' (a representative sample "
                "spanning best→worst), or 'all' for every trial. Omit for a 10-trial sample.",
                {"node_id": {"type": "integer"},
                 "trials": {"type": "string",
                            "description": "how many sweep trials to include: a number, or 'all'. "
                                           "Default: 10 representative trials (best→worst)."}},
                ["node_id"]),
            fn_spec("read_code",
                "Read the solution code of one experiment (so you can build on or avoid it).",
                {"node_id": {"type": "integer"}}, ["node_id"]),
            fn_spec("read_logs",
                "Read one experiment's EXECUTION LOGS — the captured stdout tail from training/eval "
                "and the FULL error/stderr (not the 300-char summary). Use to see why a node failed, "
                "or what it printed while training, in full.",
                {"node_id": {"type": "integer"}}, ["node_id"]),
            fn_spec("find_analogous",
                "Find experiments most similar to a given one (or to a set of params) by parameter "
                "distance — to see how nearby configs performed before committing.",
                {"node_id": {"type": "integer"},
                 "params": {"type": "object", "description": "param dict to compare instead of a node"},
                 "k": {"type": "integer"}}),
            fn_spec("list_themes",
                "List the experiment themes explored so far with counts and best metric per theme.",
                {}),
        ]

    def execute(self, name: str, args: dict) -> str:
        st = self.state
        if st is None:
            return "(run state unavailable)"
        try:
            if name == "list_experiments":
                return self._list(st, args)
            if name == "read_experiment":
                return self._read(st, int(args.get("node_id")), args.get("trials"))
            if name == "read_code":
                return self._code(st, int(args.get("node_id")))
            if name == "read_logs":
                return self._logs(st, int(args.get("node_id")))
            if name == "find_analogous":
                return self._analogous(st, args)
            if name == "list_themes":
                return self._themes(st)
            return f"(unknown tool: {name})"
        except (KeyError, TypeError, ValueError, ArithmeticError) as e:
            return f"(tool error: {e})"

    # --- implementations ----------------------------------------------------
    def _line(self, n) -> str:
        if n.status is NodeStatus.failed:
            outcome = f"FAILED({n.error_reason or 'error'})"
        else:
            outcome = f"metric={digest.fmt_num(digest.node_metric(n))}"
        theme = f" {{{n.idea.theme}}}" if getattr(n.idea, "theme", None) else ""
        return f"#{n.id} {n.operator} {outcome} {digest.fmt_params(n.idea.params)}{theme}"

    def _list(self, st: RunState, args: dict) -> str:
        sort = (args.get("sort") or "best").lower()
        limit = int(args.get("limit") or 10)
        theme = args.get("theme")
        if sort == "recent":
            nodes = sorted(st.nodes.values(), key=lambda n: n.id, reverse=True)
        else:
            nodes = digest.top_nodes(st, len(st.nodes), worst=(sort == "worst"))
        if theme:
            nodes = [n for n in nodes if getattr(n.idea, "theme", None) == theme]
        nodes = nodes[:limit]
        if not nodes:
            return "(no matching experiments)"
        head = f"{len(nodes)} experiment(s), sort={sort}" + (f", theme={theme}" if theme else "")
        return head + ":\n" + "\n".join(self._line(n) for n in nodes)

    def _read(self, st: RunState, nid: int, trials_arg=None) -> str:
        n = st.nodes.get(nid)
        if n is None:
            return f"(no experiment #{nid})"
        out = [f"experiment #{n.id} — operator={n.operator} status={n.status.value}",
               f"parents={n.parent_ids or '[]'}",
               f"params={n.idea.params}"]
        if n.idea.space:
            out.append(f"sweep_space={n.idea.space}")
        out.append(f"metric={digest.fmt_num(n.metric)}")
        if n.confirmed_mean is not None:
            out.append(f"confirmed={digest.fmt_num(n.confirmed_mean)} "
                       f"±{digest.fmt_num(n.confirmed_std)} ({n.confirmed_seeds} seeds)")
        if n.extra_metrics:
            out.append(f"extra_metrics={n.extra_metrics}")
        if n.violations:
            out.append(f"violations={n.violations}")
        if n.status is NodeStatus.failed:
            out.append(f"failure={n.error_reason}: {(n.error or '')[:300]}")
        if n.trials:
            out.append(self._sweep_view(n, trials_arg, st.direction))
        if n.idea.rationale:
            out.append(f"rationale: {n.idea.rationale.strip()[:400]}")
        text = "\n".join(out)
        return text if len(text) <= self.max_chars else text[:self.max_chars].rstrip() + " …(truncated — ask for fewer trials)"

    @staticmethod
    def _resolve_trial_k(trials_arg, total: int) -> int:
        """How many trials to render. None -> the digest default sample; 'all'/'*'/'-1' -> every
        trial; a number -> that many (representative); anything else -> the default."""
        if trials_arg is None:
            return digest.DEFAULT_TRIAL_K
        s = str(trials_arg).strip().lower()
        if s in ("all", "*", "-1"):
            return total
        try:
            return max(1, int(float(s)))
        except (ValueError, OverflowError):   # 'inf'/'1e999' → int(float()) overflows; fall back
            return digest.DEFAULT_TRIAL_K

    def _sweep_view(self, n, trials_arg, direction: str) -> str:
        """Render a sweep node's trials as `params → metric` lines, best→worst, for the requested
        count (representative sample by default, or all). When the full finite set is shown, any
        no-metric trials are appended so 'all' is genuinely complete."""
        trials = n.trials
        finite = digest.finite_trials(trials)
        k = self._resolve_trial_k(trials_arg, len(trials))
        sel = digest.select_trials(trials, k, direction)
        best = sel[0] if sel else None
        head = f"sweep: {len(trials)} trials" + (f" over {dict(n.idea.space)}" if n.idea.space else "")
        if best:
            head += f"; best {digest.fmt_params(best.params)} metric={digest.fmt_num(best.metric)}"
        n_nometric = len(trials) - len(finite)
        if n_nometric:
            head += f" (+{n_nometric} no-metric)"
        head += (f"\nshowing {len(sel)} of {len(finite)} (best→worst):" if len(sel) < len(finite)
                 else "\ntrials (best→worst):")
        lines = [head] + [f"  {digest.trial_line(t)}" for t in sel]
        if len(sel) >= len(finite):   # complete finite set shown → list the no-metric trials too
            lines += [f"  {digest.fmt_params(t.params)} → (no metric"
                      + (f": {t.error[:60]}" if t.error else "") + ")"
                      for t in trials if t.metric is None or not math.isfinite(t.metric)]
        return "\n".join(lines)

    def _code(self, st: RunState, nid: int) -> str:
        n = st.nodes.get(nid)
        if n is None:
            return f"(no experiment #{nid})"
        if not n.code and not n.files:
            return f"(experiment #{nid} has no code recorded)"
        # Surface the OUTCOME up front so a reader (especially the Developer reusing/merging a prior
        # node) never mistakes a FAILED node's code for a working reference: `n.code`/`n.files` is the
        # exact code that was evaluated, so on a failure it is the version that BROKE — read_logs(#id)
        # carries the stderr/traceback for it.
        if n.status is NodeStatus.failed:
            banner = (f"# ⚠ experiment #{nid} FAILED ({n.error_reason or 'error'}) — the code below is the "
                      f"version that FAILED; call read_logs({nid}) for the error/stderr before reusing it.\n")
        else:
            banner = (f"# experiment #{nid} — metric={digest.fmt_num(digest.node_metric(n))} "
                      "(this is the final evaluated code)\n")
        files = (f"\nother files: {list(n.files)}" if n.files else "")
        return f"{banner}# solution.py of experiment #{nid}\n{n.code[:self.max_chars]}{files}"

    def _logs(self, st: RunState, nid: int) -> str:
        """The node's execution logs: the captured stdout tail (what it printed while training/eval)
        and the FULL error/stderr — not the 300-char failure summary `read_experiment` shows. Logs are
        the whole point of this tool, so they get a larger budget (`_LOG_CHARS`) than a normal read."""
        n = st.nodes.get(nid)
        if n is None:
            return f"(no experiment #{nid})"
        head = f"experiment #{n.id} — operator={n.operator} status={n.status.value}"
        if n.error_reason:
            head += f" · failure={n.error_reason}"
        if n.eval_seconds is not None:
            head += f" · eval={digest.fmt_num(n.eval_seconds)}s"
        out = [head]
        stdout = (n.stdout_tail or "").rstrip()
        error = (n.error or "").rstrip()
        budget = max(self.max_chars, self._LOG_CHARS)
        # Split the budget so a huge stdout can't crowd out the error (and vice-versa): give each the
        # larger half only when the other is short, so a lone log still gets the whole budget.
        if stdout and error:
            half = budget // 2
            out.append("--- stdout (tail) ---\n" + _clip(stdout, max(half, budget - len(error) - 200)))
            out.append("--- error / stderr ---\n" + _clip(error, max(half, budget - len(stdout) - 200)))
        elif stdout:
            out.append("--- stdout (tail) ---\n" + _clip(stdout, budget))
        elif error:
            out.append("--- error / stderr ---\n" + _clip(error, budget))
        else:
            out.append("(no stdout or error captured for this experiment)")
        return "\n".join(out)

    def _analogous(self, st: RunState, args: dict) -> str:
        nid = args.get("node_id")
        if args.get("params"):
            target, exclude = dict(args["params"]), None
        elif nid is not None and int(nid) in st.nodes:
            exclude = int(nid)
            target = st.nodes[exclude].idea.params
        else:
            return "(give a node_id or params to compare)"
        scored = []
        for n in st.nodes.values():
            if n.id == exclude:
                continue
            d = digest.param_distance(target, n.idea.params)
            if d != float("inf"):
                scored.append((d, n))
        scored.sort(key=lambda t: t[0])
        k = int(args.get("k") or 3)
        if not scored:
            return "(no comparable experiments — no shared numeric params)"
        return "nearest by param-distance:\n" + "\n".join(
            f"dist={d:.3f}  {self._line(n)}" for d, n in scored[:k])

    def _themes(self, st: RunState) -> str:
        roll = digest.theme_rollup(st)
        if not roll:
            return "(no themes assigned yet)"
        return "\n".join(
            f"{t}: {d['count']} experiment(s)" +
            (f", best={digest.fmt_num(d['best_metric'])}" if d['best_metric'] is not None else "")
            for t, d in sorted(roll.items(), key=lambda kv: -kv[1]["count"]))


class SiblingRunTools:
    """Read-only view over SIBLING runs — other runs of the SAME task under the same run-root — so a
    run can build on what neighbouring runs already learned instead of rediscovering it. Same
    `.specs()`/`.execute()`/`bind_state()` shape as RunTools; every `execute` returns a string and
    soft-fails (a junk tool call must never crash the run).

    Sibling `RunState`s are folded from disk on demand and cached by each event log's (size, mtime)
    fingerprint, so repeated turns don't re-fold unchanged runs. Reading one sibling's experiment/code
    delegates to an internal `RunTools` bound to that sibling — the same reader the in-run agent uses."""

    def __init__(self, run_root, self_run_id: str = "", max_chars: int = 3500):
        self.run_root = Path(run_root)
        self.self_run_id = self_run_id
        self.task_id = ""
        self.max_chars = max_chars
        # Traversal-guarded, (size, mtime)-fingerprinted fold cache — shared with RunsTools.
        self._runs = RunStateCache(self.run_root)
        self._reader = RunTools(max_chars=max_chars)

    # The agent loop calls this each turn; we use it to learn our OWN run_id + task_id from the live
    # state (so we never list ourselves, and only surface same-task siblings) without extra wiring.
    def bind_state(self, state: Optional[RunState] = None, parent=None) -> None:
        if state is not None:
            if getattr(state, "run_id", ""):
                self.self_run_id = state.run_id
            if getattr(state, "task_id", ""):
                self.task_id = state.task_id

    def specs(self) -> list[dict]:
        return [
            fn_spec("list_sibling_runs",
                "List OTHER runs of the same task (siblings) with their best metric, node count and "
                "phase — so you can see what neighbouring runs achieved before proposing.", {}),
            fn_spec("read_sibling_experiment",
                "Read one experiment of a SIBLING run in full detail (params, metric, rationale, "
                "failure, sweep trials). Use a run_id from list_sibling_runs.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"},
                 "trials": {"type": "string", "description": "how many sweep trials: a number, or 'all'"}},
                ["run_id", "node_id"]),
            fn_spec("read_sibling_code",
                "Read the solution code of one experiment of a SIBLING run (to reproduce or build on "
                "it — pair with an `import` action to seed it into this run).",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"}},
                ["run_id", "node_id"]),
            fn_spec("find_analogous_across_runs",
                "Find experiments ACROSS sibling runs most similar to a set of params, by parameter "
                "distance — to see how a nearby config performed elsewhere.",
                {"params": {"type": "object", "description": "param dict to compare against"},
                 "k": {"type": "integer"}}, ["params"]),
        ]

    def execute(self, name: str, args: dict) -> str:
        try:
            if name == "list_sibling_runs":
                return self._list_runs()
            if name == "read_sibling_experiment":
                return self._read(args.get("run_id"), int(args.get("node_id")), args.get("trials"))
            if name == "read_sibling_code":
                return self._code(args.get("run_id"), int(args.get("node_id")))
            if name == "find_analogous_across_runs":
                return self._analogous(args)
            return f"(unknown tool: {name})"
        except (KeyError, TypeError, ValueError, ArithmeticError) as e:
            return f"(tool error: {e})"

    # --- internals -----------------------------------------------------------
    def _state(self, run_id: Optional[str]) -> Optional[RunState]:
        return self._runs.state(run_id)

    def _sibling_ids(self) -> list[str]:
        """Run ids under run_root, excluding self, restricted to the same task_id when we know ours."""
        cand = self._runs.run_ids()
        out = []
        for rid in cand:
            if rid == self.self_run_id:
                continue
            if self.task_id:
                st = self._state(rid)
                if st is None or st.task_id != self.task_id:
                    continue
            out.append(rid)
        return out

    def _list_runs(self) -> str:
        ids = self._sibling_ids()
        if not ids:
            return "(no sibling runs of this task)"
        lines = []
        for rid in ids:
            st = self._state(rid)
            if st is None:
                continue
            best = st.best()
            phase = "finished" if st.finished else "running"
            lines.append(f"{rid}: best={digest.fmt_num(best.metric) if best else '—'} "
                         f"({st.direction}) · {len(st.nodes)} nodes · {phase}"
                         + (f" · best=#{best.id}" if best else ""))
        head = f"{len(lines)} sibling run(s) of task {self.task_id or '?'}:"
        return head + "\n" + "\n".join(lines) if lines else "(no sibling runs of this task)"

    def _read(self, run_id, nid: int, trials_arg=None) -> str:
        st = self._state(run_id)
        if st is None:
            return f"(no such sibling run: {run_id!r})"
        self._reader.bind_state(st, None)
        return f"run {run_id} · " + self._reader.execute(
            "read_experiment", {"node_id": nid, "trials": trials_arg})

    def _code(self, run_id, nid: int) -> str:
        st = self._state(run_id)
        if st is None:
            return f"(no such sibling run: {run_id!r})"
        self._reader.bind_state(st, None)
        return f"# from run {run_id}\n" + self._reader.execute("read_code", {"node_id": nid})

    def _analogous(self, args: dict) -> str:
        target = args.get("params")
        if not isinstance(target, dict) or not target:
            return "(give a params dict to compare against)"
        scored = []
        for rid in self._sibling_ids():
            st = self._state(rid)
            if st is None:
                continue
            for n in st.nodes.values():
                d = digest.param_distance(target, n.idea.params)
                if d != float("inf"):
                    scored.append((d, rid, n))
        scored.sort(key=lambda t: t[0])
        k = int(args.get("k") or 5)
        if not scored:
            return "(no comparable experiments across siblings — no shared numeric params)"
        return "nearest across sibling runs (by param-distance):\n" + "\n".join(
            f"dist={d:.3f}  run {rid} {self._reader._line(n)}" for d, rid, n in scored[:k])


class AllRunsTools:
    """Read-only view over EVERY run on this machine — ACROSS ALL TASKS, not just same-task siblings —
    so the Developer/Researcher can read the code + result of ANY past experiment anywhere when it
    wants to reuse or learn from an approach. Where `SiblingRunTools` restricts to the current task,
    this deliberately does NOT filter by task: it just gives the agent the capability, and the agent
    decides when a foreign run is relevant. Same `.specs()`/`.execute()`/`bind_state()` shape as the
    other providers; every `execute` returns a string and soft-fails (a junk call must never crash the
    loop). Runs are folded on demand and cached by each event log's (size, mtime) fingerprint (shared
    RunStateCache), and reading one run's experiment/code delegates to an internal `RunTools` bound to
    it — the SAME reader the in-run agent uses, so the output format is identical."""

    def __init__(self, run_root, self_run_id: str = "", max_chars: int = 3500):
        self.run_root = Path(run_root)
        self.self_run_id = self_run_id
        self.max_chars = max_chars
        self._runs = RunStateCache(self.run_root)   # traversal-guarded, (size,mtime)-fingerprinted
        self._reader = RunTools(max_chars=max_chars)

    def bind_state(self, state: Optional[RunState] = None, parent=None) -> None:
        # Learn our OWN run_id so we never list/read ourselves (own experiments already come via RunTools).
        if state is not None and getattr(state, "run_id", ""):
            self.self_run_id = state.run_id

    def specs(self) -> list[dict]:
        return [
            fn_spec("list_all_runs",
                "List EVERY run on this machine, ACROSS ALL TASKS (not just same-task siblings), with "
                "its task, best metric, node count and phase — so you can find a run whose code you "
                "want to read or reuse. Broader than list_sibling_runs.", {}),
            fn_spec("read_run_code",
                "Read the solution code (solution + files) of ONE node in ANY run on this machine — to "
                "reuse or learn from how it was implemented. Use a run_id from list_all_runs; pair with "
                "read_run_experiment to check that node's result first.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"}},
                ["run_id", "node_id"]),
            fn_spec("read_run_experiment",
                "Read ONE node of ANY run in detail: params, metric, rationale/idea, failure, sweep "
                "trials — so you can judge whether its approach is worth reading the code for.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"},
                 "trials": {"type": "string", "description": "how many sweep trials: a number, or 'all'"}},
                ["run_id", "node_id"]),
        ]

    def execute(self, name: str, args: dict) -> str:
        try:
            if name == "list_all_runs":
                return self._list_runs()
            if name == "read_run_code":
                return self._code(args.get("run_id"), int(args.get("node_id")))
            if name == "read_run_experiment":
                return self._read(args.get("run_id"), int(args.get("node_id")), args.get("trials"))
            return f"(unknown tool: {name})"
        except (KeyError, TypeError, ValueError, ArithmeticError) as e:
            return f"(tool error: {e})"

    # --- internals -----------------------------------------------------------
    def _state(self, run_id: Optional[str]) -> Optional[RunState]:
        return self._runs.state(run_id)

    def _all_ids(self) -> list[str]:
        """Every run id under run_root EXCEPT self (own experiments already reachable via RunTools)."""
        return [rid for rid in self._runs.run_ids() if rid != self.self_run_id]

    def _list_runs(self) -> str:
        lines = []
        for rid in self._all_ids():
            st = self._state(rid)
            if st is None:
                continue
            best = st.best()
            phase = "finished" if st.finished else "running"
            lines.append(f"{rid} [{st.task_id or '?'}]: best={digest.fmt_num(best.metric) if best else '—'} "
                         f"({st.direction}) · {len(st.nodes)} nodes · {phase}"
                         + (f" · best=#{best.id}" if best else ""))
        return (f"{len(lines)} run(s) on this machine (across all tasks):\n" + "\n".join(lines)
                ) if lines else "(no other runs on this machine)"

    def _code(self, run_id, nid: int) -> str:
        st = self._state(run_id)
        if st is None:
            return f"(no such run: {run_id!r})"
        self._reader.bind_state(st, None)
        return f"# from run {run_id}\n" + self._reader.execute("read_code", {"node_id": nid})

    def _read(self, run_id, nid: int, trials_arg=None) -> str:
        st = self._state(run_id)
        if st is None:
            return f"(no such run: {run_id!r})"
        self._reader.bind_state(st, None)
        return f"run {run_id} · " + self._reader.execute(
            "read_experiment", {"node_id": nid, "trials": trials_arg})


class DataTools:
    """Read the concrete task data — schema, column profiling, and asset samples — so the Researcher
    proposes from the REAL data rather than guessing. Degrades gracefully for tasks with no dataset
    (e.g. toy/repo tasks). Uses the documented TaskAdapter surface (`columns`/`assets`), plus the
    optional `data_samples()` hook as a fallback for tasks that read their data by absolute path and
    expose `assets()=={}` (the `dataset` kind) — so their on-disk data is still visible here."""

    def __init__(self, task, max_chars: int = 3500):
        self.task = task
        self.max_chars = max_chars
        self.state: Optional[RunState] = None

    def bind_state(self, state: RunState, parent=None) -> None:
        self.state = state

    def specs(self) -> list[dict]:
        return [
            fn_spec("data_schema", "Show the task's data schema — column names, types, and a couple of "
                "sample values — so you propose from the real fields.", {}),
            fn_spec("data_profile", "Per-column statistics of the task data — missing fraction, numeric "
                "min/max/mean, and categorical cardinality (derived from the training table).", {}),
            fn_spec("read_asset", "Read a sample of a task data asset (e.g. train/test). Omit `name` to "
                "list available assets.", {"name": {"type": "string"}}),
        ]

    def execute(self, name: str, args: dict) -> str:
        try:
            if name == "data_schema":
                return self._schema()
            if name == "data_profile":
                return self._profile()
            if name == "read_asset":
                return self._asset(args.get("name"))
            return f"(unknown tool: {name})"
        except Exception as e:  # noqa: BLE001 — data reads are best-effort
            return f"(tool error: {e})"

    def _columns(self) -> Optional[dict]:
        fn = getattr(self.task, "columns", None)
        return fn() if callable(fn) else None

    def _assets(self) -> dict:
        fn = getattr(self.task, "assets", None)
        assets = (fn() if callable(fn) else {}) or {}
        if assets:
            return assets
        # Fallback for tasks that read their data by absolute path and expose assets()=={} (the
        # `dataset` kind): preview the on-disk data as bounded head samples so read_asset /
        # data_schema / data_profile aren't blind. Read-only — NOT materialized into the sandbox.
        sampler = getattr(self.task, "data_samples", None)
        if callable(sampler):
            try:
                return sampler() or {}
            except Exception:  # noqa: BLE001 — previews are best-effort
                return {}
        return {}

    _PROFILE_ROWS = 5000          # cap on retained rows (bounds the parse + the sample size)
    _MAX_TABLE_CHARS = 4_000_000  # cap on the text actually parsed, so neither the StringIO copy
                                  # nor the parse scales with a multi-hundred-MB table

    def _primary_table(self):
        """Pick the most representative training table among the CSV/TSV assets (prefer
        ``train*``, else the first one) and parse a bounded prefix into at most ``_PROFILE_ROWS``
        rows — a ``.tsv`` table is split on tabs. Returns ``(name, header, rows)`` or ``None`` when
        no parseable CSV/TSV asset exists — so schema/profile can derive a real view from the actual
        data even when the task declares no structured ``columns()``. Only the first
        ``_MAX_TABLE_CHARS`` are wrapped/parsed, so a huge file isn't copied whole here. (The task's
        ``assets()`` still materializes each file once upstream — that read is outside this read-only
        tool's control.)"""
        tables = {n: v for n, v in self._assets().items()
                  if isinstance(v, str) and n.lower().endswith((".csv", ".tsv"))}
        if not tables:
            return None
        name = next((n for n in tables if n.lower().startswith("train")), None) or sorted(tables)[0]
        delim = "\t" if name.lower().endswith(".tsv") else ","
        try:
            reader = csv.reader(io.StringIO(tables[name][:self._MAX_TABLE_CHARS]), delimiter=delim)
            header = next(reader, None)
            if not header:
                return None
            rows = []
            for i, r in enumerate(reader):
                if i >= self._PROFILE_ROWS:
                    break
                rows.append(r)
            return name, header, rows
        except (csv.Error, ValueError):
            return None

    def _schema(self) -> str:
        cols = self._columns()
        if cols:
            lines = [f"{len(cols)} column(s):"]
            for name, vals in list(cols.items())[:40]:
                sample = [v for v in (vals[:3] if isinstance(vals, list) else [])]
                dtype = "numeric" if sample and all(isinstance(v, (int, float)) for v in sample) else "categorical"
                lines.append(f"  {name} ({dtype}) e.g. {sample}")
            return "\n".join(lines)[:self.max_chars]
        # Fallback: derive the schema from the training table itself (CSV header + sampled values),
        # so a task that exposes no explicit columns() (e.g. mlebench_real) still gets a real schema.
        tbl = self._primary_table()
        if not tbl:
            return "(this task exposes no structured schema — try read_asset or data_profile)"
        name, header, rows = tbl
        lines = [f"schema inferred from {name} ({len(header)} columns, {len(rows)} rows sampled):"]
        for ci, col in enumerate(header[:60]):
            samples = [r[ci] for r in rows[:50] if ci < len(r) and r[ci] != ""]
            dtype = "numeric" if samples and all(_is_number(v) for v in samples) else "categorical"
            lines.append(f"  {col} ({dtype}) e.g. {samples[:3]}")
        return "\n".join(lines)[:self.max_chars]

    def _profile(self) -> str:
        prof = getattr(self.state, "data_profile", None) if self.state else None
        if prof:
            lines = ["column profile:"]
            for name, p in list(prof.items())[:40]:
                if not isinstance(p, dict):
                    continue
                bits = [f"dtype={p.get('dtype')}", f"missing={p.get('missing_frac')}"]
                if p.get("dtype") == "numeric":
                    bits += [f"min={p.get('min')}", f"max={p.get('max')}", f"mean={p.get('mean')}"]
                else:
                    bits.append(f"unique={p.get('n_unique')}")
                lines.append(f"  {name}: " + " ".join(str(b) for b in bits))
            return "\n".join(lines)[:self.max_chars]
        # Fallback: profile the training table on the fly (count/missing + numeric min/max/mean or
        # categorical cardinality) when the run recorded no profile — real per-column stats, cheaply.
        tbl = self._primary_table()
        if not tbl:
            return "(no data profile recorded for this run)"
        name, header, rows = tbl
        lines = [f"column profile from {name} ({len(rows)} rows sampled):"]
        for ci, col in enumerate(header[:60]):
            # A row too short to reach this column counts as MISSING (denominator = all sampled rows),
            # so a frequently-truncated trailing column in a ragged CSV isn't reported as fully
            # populated based only on the few rows long enough to include it.
            present = [r[ci] for r in rows if ci < len(r) and r[ci] != ""]
            missing = (1 - len(present) / len(rows)) if rows else 0.0
            nums = [float(v) for v in present if _is_number(v)]
            if present and len(nums) == len(present):             # every present value is finite-numeric
                mean = sum(nums) / len(nums)
                lines.append(f"  {col}: numeric missing={missing:.2f} "
                             f"min={digest.fmt_num(min(nums))} max={digest.fmt_num(max(nums))} "
                             f"mean={digest.fmt_num(mean)}")
            else:
                lines.append(f"  {col}: categorical missing={missing:.2f} unique={len(set(present))}")
        return "\n".join(lines)[:self.max_chars]

    def _asset(self, name: Optional[str]) -> str:
        assets = self._assets()
        if not assets:
            return "(this task has no data assets)"
        if not name:
            return "available assets: " + ", ".join(assets)
        if name not in assets:
            return f"(no asset '{name}'; available: {', '.join(assets)})"
        return f"--- {name} (truncated) ---\n{str(assets[name])[:self.max_chars]}"
