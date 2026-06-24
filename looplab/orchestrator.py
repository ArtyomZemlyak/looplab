"""Engine / control loop (I6, ADR-12/18). anyio structured concurrency:
node *creation* is sequential & deterministic; node *evaluation* fans out under a
CapacityLimiter. State is always a fresh fold of the log (files-as-truth); resume
is just re-entering this loop on an existing run dir — pending nodes get re-evaluated
idempotently, and node ids are a monotonic count so reruns never duplicate.

A crash can be injected (for the resume test) via `crash_after`: hard-exit after N
node_evaluated events have been written, simulating `kill -9` mid-run.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Optional

import anyio
import orjson

from .agents_md import generate_agents_md
from .archive import DiversityArchive
from .cv import cv_summary
from .eventstore import EventStore, iter_jsonl
from .gate import one_se_better
from .htmlview import render_html
from .leakage import target_leakage, temporal_leakage, train_test_contamination
from .memory import JsonlCaseLibrary
from .models import Idea, NodeStatus, RunState
from .operators import merge_idea
from .policy import SearchPolicy, available_policies, make_policy
from .strategist import (
    StrategyContext,
    failure_rate,
    improves_since_best,
    is_numeric_space,
    run_phase,
    validate_strategy,
)
from .profile import profile_dataset
from .readmodel import build_readmodel
from .replay import fold
from .roles import Developer, Researcher
from .sandbox import Sandbox
from .tracing import JsonlSpanExporter, Tracer


def _dir_fingerprint(path) -> str:
    """git HEAD SHA if `path` is (inside) a git repo, else a sha256 over sorted
    (relpath, size, mtime_ns) — cheap and deterministic, catches edits/adds/removes without
    reading file contents. A missing path fingerprints as 'absent'."""
    import subprocess
    p = Path(path)
    if not p.exists():
        return "absent"
    try:
        r = subprocess.run(["git", "-C", str(p), "rev-parse", "HEAD"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0 and r.stdout.strip():
            return "git:" + r.stdout.strip()
    except OSError:
        pass
    if p.is_file():
        st = p.stat()
        return f"file:{st.st_size}:{st.st_mtime_ns}"
    h = hashlib.sha256()
    for f in sorted(p.rglob("*")):
        if f.is_file() and ".git" not in f.parts:
            st = f.stat()
            h.update(f.relative_to(p).as_posix().encode())
            h.update(f"{st.st_size}:{st.st_mtime_ns}".encode())
    return "hash:" + h.hexdigest()[:16]


def _shallow_fingerprint(path) -> str:
    """Cheap signature for large/immutable mounts (data, references): git HEAD if it's a git
    repo, else a single os.scandir of the TOP level (entry count + max mtime) — O(top-level),
    never a recursive walk. Catches add/remove/replace at the root; deep edits to immutable
    inputs aren't the resume-drift concern (the editable repos are, and those are deep-hashed)."""
    import subprocess
    p = Path(path)
    if not p.exists():
        return "absent"
    try:
        r = subprocess.run(["git", "-C", str(p), "rev-parse", "HEAD"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0 and r.stdout.strip():
            return "git:" + r.stdout.strip()
    except OSError:
        pass
    if p.is_file():
        st = p.stat()
        return f"file:{st.st_size}:{st.st_mtime_ns}"
    n, newest = 0, 0
    with os.scandir(p) as it:
        for e in it:
            n += 1
            try:
                newest = max(newest, e.stat(follow_symlinks=False).st_mtime_ns)
            except OSError:
                pass
    return f"dir:{n}:{newest}"


def _failure_reason(res) -> str:
    """Classify why an eval produced no usable metric, so the audit trail distinguishes a
    crash from a timeout from a missing-deps setup failure from a drift rejection from a clean
    run that simply printed no metric. Ordered most-specific first."""
    if getattr(res, "drift", None) is not None:
        return "drift"
    if res.timed_out:
        return "timeout"
    if (res.stderr or "").startswith("setup failed:"):
        return "setup"
    if res.exit_code != 0:
        return "crash"
    return "no_metric"          # exit 0 but no parseable metric emitted


class Engine:
    def __init__(
        self,
        run_dir: str | os.PathLike,
        *,
        task,
        researcher: Researcher,
        developer: Developer,
        sandbox: Sandbox,
        policy: SearchPolicy,
        max_parallel: int = 1,   # single experiment at a time; > 1 = backlog parallel seam
        timeout: float = 30.0,
        crash_after: Optional[int] = None,
        confirm_top_k: int = 0,
        confirm_seeds: int = 0,
        max_seconds: Optional[float] = None,
        max_eval_seconds: Optional[float] = None,
        memory_dir: Optional[str] = None,
        require_approval: bool = False,
        archive_resolution: float = 1.0,
        onboarder=None,
        eval_trust_mode: str = "ratify_freeze",
        trust_mode: str = "trusted_local",
        docker_image: str = "python:3.12-slim",
        # --- A7 Strategist + richer-operator knobs (config-first; defaults == today's behavior) ---
        n_seeds: int = 3,
        max_nodes: int = 8,
        policy_name: str = "greedy",
        ablate_every: int = 0,
        strategist=None,            # Optional[Strategist]; None => static config policy (default)
        strategist_every: int = 3,
        developer_factory=None,     # Optional[Callable[[str], Developer]] for live backend swap
        merge_mode: str = "mean",        # A0b: "mean" | "ensemble"
        complexity_cue: bool = False,    # A0d: breadth-keyed prompt hint
        budget_aware: bool = False,      # A5: surface remaining eval budget into the prompt
        ablate_code_blocks: bool = False,  # A0a: ablate pipeline code blocks, not just params
        proxy_scorer=None,          # A6: Optional[ProxyScorer] early-signal candidate gate
        proxy_kill_fraction: float = 0.0,
        reward_hack_detect: bool = False,   # B5: flag suspicious wins (audit-only)
    ):
        self.run_dir = Path(run_dir)
        self.task = task
        self.researcher = researcher
        self.developer = developer
        self.sandbox = sandbox
        self.policy = policy
        # A7 Strategist: the policy is now hot-swappable, so the engine keeps the knobs needed to
        # rebuild it (n_seeds/max_nodes/ablate_every) + the meta-controller + operator-mix state.
        self.n_seeds = n_seeds
        self.max_nodes = max_nodes
        self._policy_name = policy_name
        self._ablate_every = ablate_every
        self.strategist = strategist
        self.strategist_every = max(1, strategist_every)
        self.developer_factory = developer_factory
        self._developer_name = "default"
        self._merge_mode = merge_mode
        self._complexity_cue = complexity_cue
        self._budget_aware = budget_aware
        self._ablate_code_blocks = ablate_code_blocks
        self.proxy_scorer = proxy_scorer
        self.proxy_kill_fraction = proxy_kill_fraction
        self.reward_hack_detect = reward_hack_detect
        self._strategy_fidelity: Optional[str] = None   # None => use the Idea's own profile
        self.max_parallel = max_parallel
        self.timeout = timeout
        self.crash_after = crash_after
        self.confirm_top_k = confirm_top_k
        self.confirm_seeds = confirm_seeds
        self.max_seconds = max_seconds
        self.max_eval_seconds = max_eval_seconds
        self.memory_dir = memory_dir
        self.require_approval = require_approval
        self.archive_resolution = archive_resolution
        # RepoTask onboarding (Phase 3): `onboarder()` -> a proposed {eval_spec,
        # adapter_files, goal}; ratified per `eval_trust_mode` then frozen+trusted.
        self.onboarder = onboarder
        self.eval_trust_mode = eval_trust_mode
        # Sandbox tier for the command-eval path (ADR-13, Phase 4): "untrusted" wraps each
        # eval in `docker run --network none` (real isolation for an arbitrary framework);
        # "trusted_local" runs it directly. The solution.py path uses self.sandbox instead.
        self.trust_mode = trust_mode
        self.docker_image = docker_image
        self._drift_warned = False   # one-shot guard for the #8 drift-coverage warning
        # Fail loud at START, not mid-sweep: the untrusted tier needs docker, so verify it once
        # here instead of re-discovering (and re-scanning PATH) on every eval's make_docker_wrap.
        if trust_mode == "untrusted":
            import shutil as _sh
            if not _sh.which("docker"):
                raise RuntimeError(
                    "trust_mode='untrusted' needs the docker CLI to sandbox evals, but it was "
                    "not found on PATH. Install Docker or use trust_mode='trusted_local'.")
        self._spec_activated = False
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.store = EventStore(self.run_dir / "events.jsonl")
        self._write_lock = anyio.Lock()
        # Tracing (I14): nested, correlated spans -> spans.jsonl (files-as-truth), bridged to
        # OpenTelemetry when the SDK is configured. Diagnostics only; never drives state.
        self.tracer = Tracer(JsonlSpanExporter(self.run_dir / "spans.jsonl"),
                             run_id=self.run_dir.name)
        # Task assets (e.g. the dataset) materialized into each node's sandbox workdir.
        assets = getattr(task, "assets", None)
        self._assets: dict = assets() if callable(assets) else {}
        # RepoTask (ADR-7): an existing repo the agent edits + a command-based eval.
        rs = getattr(task, "repo_spec", None)
        self._repo_spec: dict = rs() if callable(rs) else {}
        es = getattr(task, "eval_spec", None)
        self._eval_spec: dict = es() if callable(es) else {}
        # Fail loudly: a repo task with no trusted eval AND no onboarder would silently
        # evaluate every node via the empty solution.py path. Require one or the other.
        if self._repo_spec and not self._eval_spec and onboarder is None:
            raise ValueError(
                "RepoTask has no eval and no onboarder: set `onboard: true` with "
                "backend=llm (so an onboarder is built), or provide `eval` in the task.")

    def _write_assets(self, workdir) -> None:
        if not self._assets:
            return
        from pathlib import Path as _P
        wd = _P(workdir)
        wd.mkdir(parents=True, exist_ok=True)
        for name, content in self._assets.items():
            (wd / name).write_text(content, encoding="utf-8")

    def _write_node_files(self, node, workdir) -> None:
        """Materialize a multi-file solution's helper files (ADR-7 patch-gated agent)
        into the eval workdir. Skipped: `solution.py` (the sandbox writes it from
        `node.code`) and any **task-asset name** — an agent must never be able to
        overwrite a task-owned file (e.g. the private `grader.py` answer key) via an
        in-surface `*.py` edit. Paths are surface-gated (no escapes) by the developer; we
        re-check defensively. Call BEFORE `_write_assets` so task assets always win."""
        from pathlib import Path as _P
        files = getattr(node, "files", None) or {}
        deleted = getattr(node, "deleted", None) or []
        if not files and not deleted:
            return
        # Case-insensitive protected match (defense-in-depth): the surface gate uses fnmatch and
        # NTFS is case-insensitive, so a case-variant name (Ttrain.PY) would otherwise dodge the
        # freeze and overwrite the real metric/grader/eval file on Windows.
        import os as _os
        protected = {_os.path.normcase(n) for n in
                     ("solution.py", *self._assets, *self._repo_spec.get("protected_names", []))}
        wd = _P(workdir).resolve()
        wd.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            if _os.path.normcase(str(name).replace("\\", "/")) in protected:
                continue
            target = (wd / name).resolve()
            if wd not in target.parents:        # defense-in-depth: never escape workdir
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        # Apply accepted deletions (the agent removed an in-surface file). Skip protected names
        # and never escape the workdir; missing is fine (idempotent).
        for name in deleted:
            if _os.path.normcase(str(name).replace("\\", "/")) in protected:
                continue
            target = (wd / name).resolve()
            if wd not in target.parents:
                continue
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass

    # ----------------------------------------------------------------- public
    async def run(self) -> RunState:
        state = fold(self.store.read_all())
        if not state.run_id:
            cfg_hash = hashlib.sha256(
                orjson.dumps(self.task.model_dump(mode="json"))
            ).hexdigest()[:12]
            self.store.append(
                "run_started",
                {
                    "run_id": self.run_dir.name,
                    "task_id": self.task.id,
                    "goal": self.task.goal,
                    "direction": self.task.direction,
                    "config_hash": cfg_hash,
                    # Reproducibility (item #4): pin the editable repo(s)+data fingerprint at
                    # start so a resume can tell whether the source workspace changed underneath.
                    "workspace": self._workspace_fingerprint(),
                },
            )
            # AGENTS.md (I18): task/contract context for coding-agent backends.
            (self.run_dir / "AGENTS.md").write_text(
                generate_agents_md(self.task), encoding="utf-8")
            # Grounding pre-phase (I16): profile the dataset if the task exposes one.
            cols = getattr(self.task, "columns", None)
            if callable(cols):
                self.store.append("data_profiled", {"columns": profile_dataset(cols())})
            # Leakage-first grounding (I9): if the task exposes split/feature/target/time
            # data and a leak is detected, refuse to run — don't produce results on
            # leaky data.
            if self._leakage_blocks():
                self.store.append("run_finished", {"reason": "leakage"})
        elif self._repo_spec and state.workspace and not state.workspace_changed:
            # Resume (item #4): the editable workspace is copied fresh each node, so if the
            # operator's repo changed since the run started, later nodes silently evaluate a
            # DIFFERENT codebase. Record it instead of pretending the run is reproducible.
            now = self._workspace_fingerprint()
            if now != state.workspace:
                self.store.append("workspace_changed", {"was": state.workspace, "now": now})

        entry_finished = fold(self.store.read_all()).finished  # resuming a done run?
        # A7 Strategist: re-apply the last-decided strategy on (re)entry so a resumed run continues
        # with it WITHOUT re-consulting the Strategist (the decision lives in the event log).
        _entry = fold(self.store.read_all())
        if _entry.active_strategy:
            self._apply_strategy(_entry.active_strategy)
        start = time.time()
        while True:
            state = fold(self.store.read_all())
            if state.finished:
                break
            # Live operator control (UI intervention via the event log). The UI appends a
            # control event; the engine — sole writer of domain events — reads the intent here
            # and writes the effect. `run_abort` terminates (resumable=no); `pause` breaks
            # WITHOUT finishing (a later `resume` event + re-entering run() continues), the same
            # files-as-truth shape as the HITL approval gate below.
            if state.stop_requested:
                self.store.append("run_finished", {"reason": "aborted"})
                break
            if state.paused:
                break
            # Onboarding pre-phase (Phase 3, ADR-7): the agent proposes a trusted eval
            # spec + metric adapter; a human ratifies it once (or autonomous auto-confirms);
            # then it's frozen + protected and the optimization loop trusts it.
            if self.onboarder is not None and not state.spec_confirmed:
                if state.proposed_spec is None:
                    with self.tracer.span("onboard", new_trace=True):
                        proposal = self.onboarder()
                    self.store.append("spec_proposed", proposal)
                    continue
                if self.eval_trust_mode == "autonomous":
                    self.store.append("spec_approved", {})   # no human gate
                    continue
                if not state.spec_approval_requested:
                    self.store.append("spec_approval_requested",
                                      {"eval": state.proposed_spec.get("eval_spec")})
                break  # pause for `LoopLab approve` (ratify_freeze)
            if self.onboarder is not None and not self._spec_activated:
                self._activate_spec(state.proposed_spec)
            # Drift coverage (#8): ratify_freeze_drift only corroborates the metric if a
            # cross_check reader exists. An adapter metric (agent-authored reader) with no
            # cross_check would make the drift guard a SILENT no-op exactly where it matters
            # most — surface it loudly once instead of pretending the metric is corroborated.
            if (self.eval_trust_mode == "ratify_freeze_drift" and self._eval_spec
                    and not self._drift_warned):
                self._drift_warned = True
                _m = self._eval_spec.get("metric", {})
                if _m.get("kind") == "adapter" and not self._eval_spec.get("cross_check"):
                    self.store.append("drift_unavailable", {
                        "reason": "ratify_freeze_drift selected but the adapter metric has no "
                                  "cross_check; the agent-authored reader is trusted WITHOUT "
                                  "independent corroboration. Add eval.cross_check (a built-in "
                                  "reader) to enable the drift guard."})
            # Effective budgets: an operator may raise (or lower) them live via a `budget_extend`
            # control event (folded into state.budget_overrides), e.g. "keep going for 600s more".
            max_s = state.budget_overrides.get("max_seconds", self.max_seconds)
            max_es = state.budget_overrides.get("max_eval_seconds", self.max_eval_seconds)
            # Budget (I13): per-invocation wall-clock ceiling (resets on each resume).
            if max_s is not None and (time.time() - start) >= max_s:
                self.store.append("run_finished", {"reason": "time_budget"})
                break
            # Eval-compute budget (#2): cumulative time spent inside evals across the whole run
            # (persisted via the event log, so it survives resume — unlike wall-clock). Stops
            # the silent multi-hour sweep that real training runs can produce.
            if (max_es is not None
                    and state.total_eval_seconds >= max_es):
                self.store.append("run_finished", {"reason": "eval_budget"})
                break

            # Operator-forced steering (Phase 5), one per iteration then re-fold. Each is gated on
            # the domain event it produces (fork_done / an ablate event / node_confirmed), so a
            # resume never repeats it — deterministic under replay.
            if len(state.fork_requests) > state.forks_done:
                req = state.fork_requests[state.forks_done]
                pid = req.get("from_node_id")
                if pid in state.nodes:
                    self._create_node({"kind": "improve", "parent_id": pid})  # operator-seeded branch
                self.store.append("fork_done", {"from_node_id": pid})         # always advance the gate
                continue
            # Operator-authored experiment (manual tree edit): the human hand-adds a node (an idea
            # + optional parent + optional ready-made code). Materialize it into a real pending node;
            # the policy then evaluates it next (pending nodes are scheduled first). Gated on
            # `inject_done` so a resume never re-creates it — deterministic under replay.
            if len(state.inject_requests) > state.injects_done:
                req = state.inject_requests[state.injects_done]
                self._create_injected_node(req)
                self.store.append("inject_done", {"idx": state.injects_done})
                continue
            forced_ablate = next((p for p in state.ablate_requests
                                  if p in state.nodes
                                  and not any(a.get("parent_id") == p for a in state.ablations)), None)
            if forced_ablate is not None:
                await self._ablate(forced_ablate)
                continue
            forced_confirm = next((n for n in state.confirm_requests
                                   if n in state.nodes
                                   and state.nodes[n].status is NodeStatus.evaluated
                                   and n not in state.confirmed_forced), None)
            if forced_confirm is not None:
                await self._confirm_node(state.nodes[forced_confirm])
                continue

            # A7 Strategist: adapt the search machinery (policy/operators/fidelity/Developer) before
            # the policy proposes the next actions. No-op when strategist is off (== today).
            state = self._maybe_consult_strategist(state)

            actions = self.policy.next_actions(state)
            if not actions:
                # Optional multi-seed confirmation pass (I12) before finishing:
                # re-evaluate the top-k under several seeds and record robust metrics.
                if (self.confirm_top_k > 0 and self.confirm_seeds > 0
                        and not self._already_confirmed(state)):
                    await self._confirm_phase(state)
                    continue
                # HITL gate (I21, ADR-11): pause for human approval of the final best.
                # Approval flows through the event log (a UI/human appends
                # `approval_granted`); the engine, sole writer of domain events, reads it.
                if self.require_approval and not state.approved:
                    if not state.awaiting_approval:
                        best = state.best()
                        self.store.append("approval_requested", {
                            "node_id": best.id if best else None,
                            "metric": best.metric if best else None})
                    break  # awaiting approval -> stop without finishing
                self.store.append("run_finished", {})
                break

            ablates = [a for a in actions if a["kind"] == "ablate"]
            if ablates:
                for a in ablates:
                    await self._ablate(a["parent_id"])
                continue

            evals = [a for a in actions if a["kind"] == "evaluate"]
            creates = [a for a in actions
                       if a["kind"] in ("draft", "improve", "debug", "merge")]

            if creates:
                for a in creates:
                    if "_scores" in a:   # policy exposed candidate scores (MCTS UCB1) -> surface "why"
                        self.store.append("policy_decision",
                                          {"scores": a["_scores"], "chosen": a.get("_chosen")})
                    if a.get("_rung") is not None:   # A1 ASHA: surface the successive-halving promotion
                        self.store.append("rung_promoted",
                                          {"rung": a["_rung"], "survivors": a.get("_promoted", [])})
                    self._create_node(a)  # sequential -> deterministic ids/proposals
                continue

            # Single experiment at a time is the base mode: run evals sequentially and
            # deterministically. Concurrent fan-out (the task-group below) is a backlog
            # seam — opt in with max_parallel > 1.
            if self.max_parallel <= 1:
                limiter = anyio.CapacityLimiter(1)
                for a in evals:
                    cur = fold(self.store.read_all())
                    # Operator stopped this specific node (`node_abort`): skip the eval and record
                    # the effect as a node_failed reason="aborted" (cooperative pre-eval skip; a
                    # mid-eval kill of an in-flight subprocess is the deferred v2). An aborted node
                    # keeps no metric, so replay excludes it from best-selection.
                    if a["node_id"] in cur.aborted_nodes:
                        n = cur.nodes.get(a["node_id"])
                        if n is not None and n.status is NodeStatus.pending:
                            self.store.append("node_failed", {
                                "node_id": a["node_id"], "error": "aborted by operator",
                                "reason": "aborted", "eval_seconds": 0.0})
                        continue
                    # Re-check the eval-compute budget BEFORE each eval (not just per loop
                    # iteration), so a multi-eval batch can't overshoot by a whole batch (#2/#25).
                    if (max_es is not None and cur.total_eval_seconds >= max_es):
                        break
                    await self._evaluate(a["node_id"], limiter)
            else:
                limiter = anyio.CapacityLimiter(self.max_parallel)
                cur = fold(self.store.read_all())
                async with anyio.create_task_group() as tg:
                    for a in evals:
                        if a["node_id"] in cur.aborted_nodes:
                            n = cur.nodes.get(a["node_id"])
                            if n is not None and n.status is NodeStatus.pending:
                                self.store.append("node_failed", {
                                    "node_id": a["node_id"], "error": "aborted by operator",
                                    "reason": "aborted", "eval_seconds": 0.0})
                            continue
                        tg.start_soon(self._evaluate, a["node_id"], limiter)

        # Finalize only on real completion (not when paused for approval / idempotent
        # resume of a done run).
        if not entry_finished and fold(self.store.read_all()).finished:
            cur = fold(self.store.read_all())
            self.store.append("budget", {                       # budget summary (I13 + #2)
                "elapsed_s": round(time.time() - start, 3),
                "eval_s": round(cur.total_eval_seconds, 3),
                "nodes": len(cur.nodes),
            })
            self.store.append("diversity_archive",              # diversity archive (I22)
                              DiversityArchive(self.archive_resolution).summary(cur))
            self._emit_llm_cost()                               # LLM cost/tokens roll-up (UI)
            self._store_case(fold(self.store.read_all()))       # cross-run memory (I19)

        final = build_readmodel(self.store.read_all(), self.run_dir / "readmodel.sqlite")
        # UI projection (ADR-17): join the research tree (events) to its execution detail
        # (spans) -> trace.json for the React UI + an inline span tree in the static HTML.
        from .traceview import build_trace_view, load_spans
        tv = build_trace_view(final, load_spans(self.run_dir / "spans.jsonl"))
        (self.run_dir / "trace.json").write_bytes(orjson.dumps(tv))
        (self.run_dir / "tree.html").write_text(render_html(final, tv), encoding="utf-8")
        return final

    def _emit_llm_cost(self) -> None:
        """Best-effort LLM cost/token roll-up for the UI cost panel. Duck-types the role graph
        (researcher/developer may be wrapped by ToolUsingResearcher/ValidatingDeveloper) to find
        every CostAccountant, dedupes by identity, and emits one `llm_cost` event. Local models
        have no $ price (spent=0.0) but tokens are the real cost signal. Skips silently for the
        offline/toy backend (no client, no accountant) — never breaks a run."""
        try:
            seen: dict[int, object] = {}
            stack = [self.researcher, self.developer]
            while stack:
                obj = stack.pop()
                if obj is None:
                    continue
                acc = getattr(obj, "accountant", None)
                if acc is not None and id(acc) not in seen:
                    seen[id(acc)] = acc
                for attr in ("client", "inner", "fallback", "researcher", "developer", "tools"):
                    child = getattr(obj, attr, None)
                    if child is not None and child is not obj:
                        stack.append(child)
            if not seen:
                return
            accs = list(seen.values())
            if not any(getattr(a, "calls", 0) for a in accs):
                return  # no LLM calls actually happened (e.g. toy run) — nothing to report
            self.store.append("llm_cost", {
                "cost": round(sum(getattr(a, "spent", 0.0) for a in accs), 6),
                "calls": sum(getattr(a, "calls", 0) for a in accs),
                "prompt_tokens": sum(getattr(a, "prompt_tokens", 0) for a in accs),
                "completion_tokens": sum(getattr(a, "completion_tokens", 0) for a in accs),
                "total_tokens": sum(getattr(a, "total_tokens", 0) for a in accs),
            })
        except Exception:  # noqa: BLE001 - cost telemetry must NEVER abort run finalization
            return

    # ----------------------------------------------------------- A7 Strategist
    @staticmethod
    def _strategy_core(s: Optional[dict]) -> dict:
        """The decision-relevant subset of a Strategy (ignores rationale/source) — used to detect a
        REAL change so the engine doesn't re-record/re-apply an identical strategy every iteration."""
        if not s:
            return {}
        return {k: s.get(k) for k in ("policy", "policy_params", "developer", "operators", "fidelity")}

    def _available_developers(self) -> list[str]:
        from .cli_agent import PRESETS
        names = ["default", "llm", *PRESETS]
        return names if self.developer_factory is not None else names[:1]

    def _strategy_ctx(self, state: RunState) -> StrategyContext:
        max_es = state.budget_overrides.get("max_eval_seconds", self.max_eval_seconds)
        rem = (max_es - state.total_eval_seconds) if max_es is not None else None
        defaults = {"policy": self._policy_name, "operators": {"ablate_every": self._ablate_every}}
        if max_es:
            defaults["_budget_frac"] = max(0.0, (rem or 0.0) / max_es)
        return StrategyContext(
            node_count=len(state.nodes),
            phase=run_phase(state, self.n_seeds),
            eval_budget_remaining=rem,
            failure_rate=failure_rate(state),
            improves_since_best=improves_since_best(state),
            is_numeric_space=is_numeric_space(state),
            available_policies=available_policies(),
            available_developers=self._available_developers(),
            defaults=defaults,
        )

    def _should_consult(self, state: RunState) -> bool:
        """Bounded, deterministic cadence: only at a creation decision point (no pending evals),
        at the seed boundary or every `strategist_every` created nodes."""
        if state.pending_nodes():
            return False
        n = len(state.nodes)
        if n == 0:
            return False
        return n == self.n_seeds or n % self.strategist_every == 0

    def _record_strategy(self, strat: dict, state: RunState,
                         ctx: Optional[StrategyContext] = None) -> None:
        self.store.append("strategy_decision", {
            "strategy": strat,
            "at_node": len(state.nodes),
            "ctx": (ctx.model_dump(include={"phase", "eval_budget_remaining", "failure_rate"})
                    if ctx is not None else None),
        })
        self._apply_strategy(strat)

    def _apply_strategy(self, strat: dict) -> None:
        """Rebuild the live search machinery from a Strategy (pure wiring, no events). Policies share
        the action vocabulary and are pure, so swapping between loop iterations is safe; the Developer
        is swapped only between sequential _create_node calls."""
        ops = strat.get("operators") or {}
        if "ablate_every" in ops:
            self._ablate_every = int(ops["ablate_every"])
        if "merge_mode" in ops:
            self._merge_mode = ops["merge_mode"]
        if "complexity_cue" in ops:
            self._complexity_cue = bool(ops["complexity_cue"])
        if "ablate_code_blocks" in ops:
            self._ablate_code_blocks = bool(ops["ablate_code_blocks"])
        pol = strat.get("policy")
        if pol:
            try:
                self.policy = make_policy(pol, n_seeds=self.n_seeds, max_nodes=self.max_nodes,
                                          ablate_every=self._ablate_every,
                                          **(strat.get("policy_params") or {}))
                self._policy_name = pol
            except (ValueError, TypeError):
                pass    # keep the current policy on a bad spec (validate_strategy already whitelisted)
        fid = strat.get("fidelity")
        if fid in ("smoke", "full"):
            self._strategy_fidelity = fid
        elif fid == "adaptive":
            self._strategy_fidelity = None
        dev = strat.get("developer")
        if dev and self.developer_factory is not None and dev != self._developer_name:
            try:
                self.developer = self.developer_factory(dev)
                self._developer_name = dev
            except Exception:  # noqa: BLE001 — a bad backend swap must never abort the run
                pass

    def _maybe_consult_strategist(self, state: RunState) -> RunState:
        """Operator override first (HITL parity), then the bounded-cadence Strategist consult.
        Records a `strategy_decision` and re-folds only when the strategy actually changes."""
        # Operator-pinned strategy (set_strategy control event) always wins over the Strategist.
        if (state.pending_strategy
                and self._strategy_core(state.pending_strategy) != self._strategy_core(state.active_strategy)):
            ctx = self._strategy_ctx(state)
            strat = validate_strategy({**state.pending_strategy, "source": "operator"}, ctx)
            if strat:
                strat.setdefault("rationale", "operator-pinned strategy")
                self._record_strategy(strat, state, ctx)
                return fold(self.store.read_all())
        if self.strategist is not None and self._should_consult(state):
            ctx = self._strategy_ctx(state)
            strat = validate_strategy(self.strategist.decide(state, ctx), ctx)
            if strat and self._strategy_core(strat) != self._strategy_core(state.active_strategy):
                self._record_strategy(strat, state, ctx)
                return fold(self.store.read_all())
        return state

    def _set_complexity_hint(self, state: RunState, parent) -> None:
        """Inject the engine-computed proposal cues into the next prompt: A0d (breadth-keyed
        complexity) + A5 (remaining eval budget). No-op unless the respective knob is on; harmless on
        Toy roles. Both flow via the single `_complexity_hint` attribute both Researchers read."""
        hint = ""
        if self._complexity_cue:
            nc = (sum(1 for n in state.nodes.values() if parent.id in n.parent_ids)
                  if parent is not None else len([n for n in state.nodes.values() if not n.parent_ids]))
            level = ("a minimal baseline" if nc < 2 else "a moderate approach" if nc < 4
                     else "an advanced approach (ensembling / HPO / feature-engineering)")
            hint += (f"\nComplexity guidance: this branch already has {nc} sibling experiment(s); "
                     f"propose {level}.")
        if self._budget_aware:
            max_es = state.budget_overrides.get("max_eval_seconds", self.max_eval_seconds)
            if max_es:
                rem = max(0.0, max_es - state.total_eval_seconds)
                frac = rem / max_es if max_es else 1.0
                stance = ("explore broadly — plenty of budget" if frac > 0.5 else
                          "be selective — budget is over half spent" if frac > 0.2 else
                          "exploit the leader with cheap experiments — budget nearly spent")
                hint += (f"\nBudget guidance: {rem:.0f}s of {max_es:.0f}s eval budget remain "
                         f"({frac:.0%}); {stance}.")
        try:
            setattr(self.researcher, "_complexity_hint", hint)
        except Exception:  # noqa: BLE001
            pass

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

    # ---------------------------------------------------------------- private
    def _create_node(self, action: dict) -> None:
        state = fold(self.store.read_all())
        node_id = len(state.nodes)  # monotonic across the whole run -> unique
        kind = action["kind"]
        with self.tracer.span("create_node", new_trace=True, node_id=node_id, operator=kind):
            if kind == "draft":
                self._set_complexity_hint(state, None)   # A0d breadth-keyed complexity cue
                with self.tracer.span("propose"):
                    idea = self.researcher.propose(state, None)
                idea.operator = "draft"        # operator is authoritative from the policy,
                parents: list[int] = []        # not whatever label the LLM returns
                with self.tracer.span("implement"):
                    code = self.developer.implement(idea)
            elif kind == "merge":
                parents = list(action["parent_ids"])
                # A0b: real ensembling (code recombination) when configured/Strategist-selected;
                # else the legacy mean-param merge. Toy/baseline developers degrade to mean.
                pnodes = [state.nodes[i] for i in parents]
                idea = (self._ensemble_idea(pnodes) if self._merge_mode == "ensemble"
                        else merge_idea(pnodes))
                with self.tracer.span("implement"):
                    code = self.developer.implement(idea)
            elif kind == "debug":
                parent = state.nodes[action["parent_id"]]
                parents = [parent.id]
                repair = getattr(self.developer, "repair", None)
                # Error-feedback debug: hand the failure back to the Developer to fix. Fires for
                # whole-file solutions (parent.code), multi-file edits (parent.files), AND any
                # repo task (self._repo_spec) even when a prior attempt fell back to the empty
                # baseline — so an e2e agent can fix runtime errors / missing deps from the
                # error alone (it edits requirements and the eval's setup step re-installs them).
                if callable(repair) and parent.error and (parent.code or parent.files
                                                          or self._repo_spec):
                    idea = parent.idea.model_copy()
                    idea.operator = "debug"
                    with self.tracer.span("repair", parent_id=parent.id):
                        code = repair(parent.idea, parent.code, parent.error)
                else:
                    with self.tracer.span("propose"):
                        idea = self.researcher.propose(state, parent)
                    idea.operator = "debug"
                    with self.tracer.span("implement"):
                        code = self.developer.implement(idea)
            else:  # improve
                parent = state.nodes[action["parent_id"]]
                self._set_complexity_hint(state, parent)   # A0d breadth-keyed complexity cue
                with self.tracer.span("propose"):
                    idea = self.researcher.propose(state, parent)
                idea.operator = "improve"
                parents = [parent.id]
                with self.tracer.span("implement"):
                    code = self.developer.implement(idea)
            self.store.append(
                "node_created",
                {
                    "node_id": node_id,
                    "parent_ids": parents,
                    "operator": idea.operator,
                    "idea": idea.model_dump(mode="json"),
                    "code": code,
                    "files": getattr(self.developer, "last_files", {}) or {},
                    "deleted": getattr(self.developer, "last_deleted", []) or [],
                },
            )
        self._emit_agent_report(node_id)

    def _create_injected_node(self, req: dict) -> None:
        """Materialize an operator-authored experiment (`inject_node` control event) into a real
        pending node. The operator supplies an idea (operator label, params, rationale, optional
        theme) and optionally a parent and ready-made code. If no code is given, the Developer
        implements the idea — so a human can describe an experiment and let the agent build it.
        The new node enters the search as `pending`; the policy evaluates it next.

        Manual injection deliberately bypasses the policy's proposal step — the human IS the
        researcher here — but everything downstream (eval, confirmation, best-selection, lineage)
        is identical to an agent-authored node, so a hand-added winner can be selected as best."""
        state = fold(self.store.read_all())
        node_id = len(state.nodes)
        idea_d = dict(req.get("idea") or {})
        idea_d.setdefault("operator", "manual")
        # Coerce params to floats defensively (a manual form may send strings); drop unparseable.
        raw_params = idea_d.get("params") or {}
        params: dict[str, float] = {}
        for k, v in raw_params.items():
            try:
                params[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        idea_d["params"] = params
        idea = Idea(**idea_d)
        parent_id = req.get("parent_id")
        parents = [parent_id] if parent_id is not None and parent_id in state.nodes else []
        with self.tracer.span("create_node", new_trace=True, node_id=node_id,
                              operator=idea.operator, source="manual"):
            code = req.get("code")
            if not code:
                with self.tracer.span("implement"):
                    code = self.developer.implement(idea)
            self.store.append(
                "node_created",
                {
                    "node_id": node_id,
                    "parent_ids": parents,
                    "operator": idea.operator,
                    "idea": idea.model_dump(mode="json"),
                    "code": code,
                    "files": ({} if req.get("code") else getattr(self.developer, "last_files", {})) or {},
                    "source": "manual",
                },
            )
        if not req.get("code"):
            self._emit_agent_report(node_id)

    def _activate_spec(self, proposal: dict) -> None:
        """Make the ratified onboarding proposal the trusted eval (Phase 3): the eval_spec
        drives `_run_eval`, and the metric adapter is written into every eval workdir as a
        task asset AND added to the protected set so the optimization agent can't edit it
        (freeze + surface-exclude)."""
        if not proposal:
            return
        self._eval_spec = proposal.get("eval_spec", {})
        adapters = proposal.get("adapter_files", {})
        self._assets = {**self._assets, **adapters}        # frozen: written into every wd
        protected = list(self._repo_spec.get("protected_names", []))
        protected += list(adapters)                        # agent may never overwrite them
        self._repo_spec = {**self._repo_spec, "protected_names": protected}
        self._spec_activated = True

    def _workspace_fingerprint(self) -> dict:
        """A per-source fingerprint of the editable repos + mounted data (item #4): the git
        HEAD SHA when the source is a git repo, else a cheap content signature over
        (relpath, size, mtime). Used to detect that the operator's source changed between a
        run's start and a resume. {} for non-repo tasks."""
        if not self._repo_spec:
            return {}
        srcs: dict[str, str] = {}
        # Editable repos are the drift-detection TARGET (the agent edits them, the search
        # continues over them) and are small code trees -> deep content fingerprint. Data and
        # reference mounts are typically large + immutable inputs -> cheap shallow signature, so
        # the fingerprint never walks a multi-GB dataset on every (re)start.
        for ed in self._repo_spec.get("editables", []):
            srcs[f"editable:{ed['name']}"] = _dir_fingerprint(ed["path"])
        for name, src in self._repo_spec.get("data", {}).items():
            srcs[f"data:{name}"] = _shallow_fingerprint(src)
        for ref in self._repo_spec.get("references", []):
            if ref.get("mount"):
                srcs[f"ref:{ref['name']}"] = _shallow_fingerprint(ref["path"])
        return srcs

    def _seed_workspace(self, workdir) -> None:
        """RepoTask (ADR-7): materialize the editable repo tree(s) into the eval workdir, plus
        any runtime-mounted reference repos and data files. Phase 4: each editable repo is
        mounted at its own subdir (name=".") -> workspace root). The agent's `Node.files` edits
        are applied on top by `_write_node_files`; task assets win last. No-op for non-repo
        tasks."""
        if not self._repo_spec:
            return
        import shutil
        from pathlib import Path as _P
        wd = _P(workdir)
        wd.mkdir(parents=True, exist_ok=True)
        ignore = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".venv", "node_modules")
        for ed in self._repo_spec.get("editables", []):
            dst = wd if ed["name"] in (".", "") else wd / ed["name"]
            shutil.copytree(ed["path"], dst, dirs_exist_ok=True, ignore=ignore)
        for ref in self._repo_spec.get("references", []):
            if ref.get("mount"):                 # runtime dependency -> copy in read context
                dst = wd / ref["name"]
                shutil.copytree(ref["path"], dst, dirs_exist_ok=True, ignore=ignore)
        for name, src in self._repo_spec.get("data", {}).items():
            sp = _P(src)
            if sp.is_dir():
                shutil.copytree(sp, wd / name, dirs_exist_ok=True, ignore=ignore)
            elif sp.is_file():
                (wd / name).write_bytes(sp.read_bytes())

    def _run_eval(self, node, workdir, env=None, profile=None, cancel=None):
        """Eval dispatcher: RepoTask runs the operator's command + reads its metric;
        otherwise the classic solution.py sandbox path. Both return a `RunResult`, so all
        downstream metric/exit/timeout checks are identical.

        Phase 2: the command is built with an eval profile (smoke/full — `profile` arg, else
        the Researcher's `idea.eval_profile`) and, when params_style=cli_overrides, the
        node's params as `key=value` overrides."""
        if self._eval_spec:
            from . import command_eval
            es = self._eval_spec
            prof = profile or (node.idea.eval_profile if node is not None else None)
            # A7 Strategist fidelity override: when the active strategy pins smoke/full and the node
            # didn't request a profile, use the strategy's. An explicit `profile` arg (confirm=full)
            # always wins. "adaptive" leaves _strategy_fidelity None => the Idea's own profile.
            if prof is None and self._strategy_fidelity in ("smoke", "full"):
                prof = self._strategy_fidelity
            params = node.idea.params if node is not None else {}
            cmd, timeout = command_eval.build_command(es, params, prof)
            root = str(Path(workdir).resolve())               # repo/workdir root
            cwd = str((Path(workdir) / es.get("cwd", ".")).resolve())
            # untrusted tier (Phase 4): sandbox the eval in docker, mounting the workspace
            # root so the cwd subdir + host metric reading line up. Fails loudly w/o docker.
            wrap = (command_eval.make_docker_wrap(root, self.docker_image)
                    if self.trust_mode == "untrusted" else None)
            return command_eval.run_command_eval(
                cmd, cwd, timeout, es["metric"], env,
                setup=es.get("setup") or None, setup_timeout=es.get("setup_timeout", 600.0),
                setup_cwd=root,                               # deps install at the repo root
                cross_check=es.get("cross_check"),            # Phase 4 drift cross-check …
                drift_tolerance=float(es.get("drift_tolerance", 1e-6)),
                enforce_drift=(self.eval_trust_mode == "ratify_freeze_drift"),
                wrap=wrap,
                metrics=es.get("metrics") or None,            # #5 multi-objective …
                constraints=es.get("constraints") or None,
                tracer=self.tracer,                           # child spans: setup/command/read
                cancel=cancel)                                # operator mid-eval node_abort
        return self.sandbox.run(node.code, str(workdir), self.timeout, env, cancel=cancel)

    def _emit_agent_report(self, node_id: int) -> None:
        """External-agent audit (ADR-7): if the Developer validated its output (a
        `ValidatingDeveloper`), record the verdict as an `agent_validated` event so each
        node carries a trail of how the external coding agent performed. No-op for
        plain developers (no `last_report`).

        Safe because node *creation* (`_create_node` / `_ablate`) is awaited sequentially
        in the main loop and never inside the parallel `evals` task group, so the shared
        `developer.last_report` set just above always belongs to `node_id`."""
        report = getattr(self.developer, "last_report", None)
        if report is not None:
            data = {"node_id": node_id, **report.summary()}
            extra = getattr(self.developer, "audit_extra", None)
            if callable(extra):
                data.update(extra())
            self.store.append("agent_validated", data)

    @property
    def _probe_developer(self):
        """Developer used for ablation *probes* (I7): the raw inner developer, bypassing
        any ValidatingDeveloper's retry/fallback. Probes are a measurement harness, not a
        shipped step — routing them through validation would (a) substitute the LLM
        fallback mid-measurement, corrupting impact numbers, and (b) multiply expensive
        external-agent calls by len(params) per ablation (ADR-7 cost rule)."""
        return getattr(self.developer, "inner", self.developer)

    async def _evaluate(self, node_id: int, limiter: anyio.CapacityLimiter) -> None:
        async with limiter:
          with self.tracer.span("evaluate", new_trace=True, node_id=node_id) as sp:
            state = fold(self.store.read_all())
            node = state.nodes[node_id]
            sp.set("operator", node.operator)
            # A6 proxy/predictive scoring: cheaply predict this candidate's metric from the observed
            # history and skip a full eval for the doomed bottom fraction (cost lever). Deterministic
            # + replay-safe: the skip is recorded as node_failed reason="proxy_skipped" and a
            # proxy_scored audit event. OFF by default (kill_fraction=0 -> never skips).
            if self.proxy_scorer is not None and self.proxy_kill_fraction > 0:
                pred = self.proxy_scorer.score(state, node)
                if pred is not None:
                    skip = self.proxy_scorer.should_skip(state, node, pred)
                    sp.set_many(proxy_score=round(pred, 6), proxy_skipped=skip)
                    async with self._write_lock:
                        self.store.append("proxy_scored",
                                          {"node_id": node_id, "score": round(pred, 6), "skipped": skip})
                        if skip:
                            self.store.append("node_failed", {
                                "node_id": node_id,
                                "error": "skipped by proxy scorer (predicted in the doomed bottom fraction)",
                                "reason": "proxy_skipped", "eval_seconds": 0.0})
                            self._maybe_crash()
                    if skip:
                        return
            workdir = self.run_dir / "nodes" / f"node_{node_id}"
            self._seed_workspace(workdir)           # RepoTask: editable repo tree (ADR-7) …
            self._write_node_files(node, workdir)   # … agent edits on top …
            self._write_assets(workdir)             # … task assets win any name collision
            _t0 = time.time()
            # Mid-eval per-node intervention (v2): a watcher polls the log while the eval runs in a
            # worker thread; if the operator appends `node_abort` for THIS node, it sets the cancel
            # Event, which tree-kills the in-flight subprocess (sandbox._run_argv). v1's pre-eval
            # skip only catches not-yet-started nodes — this kills a running one.
            import threading
            cancel = threading.Event()
            aborted = False
            async with anyio.create_task_group() as _tg:
                def _abort_seen() -> bool:   # lightweight raw scan — no full fold each tick
                    for o in iter_jsonl(self.store.path):
                        if o.get("type") == "node_abort" and o.get("data", {}).get("node_id") == node_id:
                            return True
                    return False
                async def _watch():
                    nonlocal aborted
                    while True:
                        await anyio.sleep(0.3)
                        if cancel.is_set():
                            return
                        if await anyio.to_thread.run_sync(_abort_seen):
                            aborted = True
                            cancel.set()
                            return
                _tg.start_soon(_watch)
                res = await anyio.to_thread.run_sync(
                    self._run_eval, node, str(workdir), None, None, cancel
                )
                cancel.set()                  # eval finished on its own …
                _tg.cancel_scope.cancel()     # … stop the watcher now (no poll-interval latency)
            dur = round(time.time() - _t0, 3)            # eval wall-clock (cost accounting #2)
            ok = res.metric is not None and res.exit_code == 0 and not res.timed_out
            if aborted and not ok:                       # killed mid-eval by the operator (and the
                async with self._write_lock:             # eval didn't already finish cleanly first)
                    self.store.append("node_failed", {
                        "node_id": node_id, "error": "aborted by operator (killed mid-eval)",
                        "reason": "aborted", "eval_seconds": dur})
                    self._maybe_crash()
                return
            sp.set_many(eval_seconds=dur, exit_code=res.exit_code, timed_out=res.timed_out,
                        metric=res.metric, ok=ok)
            if res.violations:
                sp.set("violations", len(res.violations))
            if res.drift is not None:
                sp.set("drift", True)
            async with self._write_lock:
                if res.drift is not None:               # Phase 4: uncorroborated metric (audit)
                    self.store.append("spec_drift", {"node_id": node_id, **res.drift})
                if ok:
                    self.store.append(
                        "node_evaluated",
                        {"node_id": node_id, "metric": res.metric,
                         "stdout_tail": res.stdout[-500:], "eval_seconds": dur,
                         "extra_metrics": res.extra_metrics or {},   # #5 multi-objective
                         "violations": res.violations or []},
                    )
                    # B5 reward-hacking detector (audit-only): flag a suspicious win without ever
                    # changing selection. Runs on the evaluated node's code + metric vs the frozen set.
                    if self.reward_hack_detect:
                        from .reward_hack import detect_reward_hacks
                        protected = set(self._repo_spec.get("protected_names", [])) | set(self._assets)
                        sigs = detect_reward_hacks(node.code, res.metric, state.direction,
                                                   protected_names=protected, stdout=res.stdout)
                        if sigs:
                            self.store.append("reward_hack_suspected",
                                              {"node_id": node_id, "signals": sigs})
                else:
                    err = res.stderr[-500:] or (
                        f"metric drift: {res.drift}" if res.drift is not None else
                        f"exit={res.exit_code} timed_out={res.timed_out} no_metric"
                    )
                    reason = _failure_reason(res)
                    sp.set("error_reason", reason)
                    self.store.append("node_failed", {"node_id": node_id, "error": err,
                                                      "reason": reason, "eval_seconds": dur})
                self._maybe_crash()

    @staticmethod
    def _already_confirmed(state: RunState) -> bool:
        return state.confirmed_done  # gated on completion, not on partial progress

    async def _confirm_phase(self, state: RunState) -> None:
        """Re-run the top-k evaluated nodes under `confirm_seeds` seeds. Selection picks
        the robust winner (best confirmed MEAN), demoting any seed-lucky leader; the
        variance gate records whether that demotion is statistically significant.

        Resume-safe: nodes already confirmed (from an earlier crashed attempt) are
        reused, and a `best_confirmed` event is ALWAYS emitted to mark completion — so a
        confirm pass where every seed run fails can't loop forever."""
        # Only confirm FEASIBLE leaders (#5): spending the expensive full-profile seed budget
        # on a constraint-violating node is wasted, and it must never be promoted to best.
        evaluated = sorted(state.feasible_nodes(), key=lambda n: (n.metric, n.id),
                           reverse=(state.direction == "max"))
        topk = evaluated[: self.confirm_top_k]
        if not topk:
            async with self._write_lock:
                self.store.append("best_confirmed", {"node_id": None, "significant": False})
            return

        summaries: list[dict] = []
        for nd in topk:
            if nd.confirmed_mean is not None:  # reuse a prior (crashed) attempt's result
                # Use the REAL seed count from that attempt, not confirm_seeds — some
                # seeds may have failed, and inflating n shrinks the SE in the variance
                # gate, overstating significance.
                summaries.append({"node_id": nd.id, "mean": nd.confirmed_mean,
                                  "std": nd.confirmed_std or 0.0,
                                  "n": nd.confirmed_seeds or self.confirm_seeds})
                continue
            # Per-seed resume (#0): reuse seeds already run in a prior (crashed) attempt instead
            # of re-executing every expensive full-profile seed. `done` maps seed -> metric|None.
            done = state.confirm_seed_results.get(nd.id, {})
            scores: list[float] = [m for m in done.values() if m is not None]
            for s in range(self.confirm_seeds):
                if s in done:                         # already evaluated this seed earlier
                    continue
                workdir = self.run_dir / "confirm" / f"node_{nd.id}_seed_{s}"
                self._seed_workspace(workdir)         # RepoTask: editable repo tree (ADR-7) …
                self._write_node_files(nd, workdir)   # … agent edits on top …
                self._write_assets(workdir)           # … task assets win any collision
                # Confirmation uses the FULL eval profile (robust check on the leaders),
                # regardless of the cheaper profile the Researcher used during search.
                _t0 = time.time()
                # Keep the per-seed events INSIDE the span so they carry its trace/span id
                # (events<->spans UI join), consistent with the _evaluate path.
                with self.tracer.span("confirm_seed", new_trace=True, node_id=nd.id, seed=s):
                    res = await anyio.to_thread.run_sync(
                        self._run_eval, nd, str(workdir), {"LOOPLAB_EVAL_SEED": str(s)}, "full",
                    )
                    valid = res.metric is not None and res.exit_code == 0 and not res.timed_out
                    async with self._write_lock:            # confirm-seed eval cost (#2) + memo (#0)
                        self.store.append("confirm_eval", {
                            "node_id": nd.id, "seed": s,
                            "eval_seconds": round(time.time() - _t0, 3),
                            "metric": res.metric if valid else None})
                        if res.drift is not None:           # Phase 4: drop + audit drifted seeds
                            self.store.append("spec_drift", {"node_id": nd.id, "seed": s, **res.drift})
                if valid:
                    scores.append(res.metric)
            if scores:
                summ = cv_summary(scores)
                summaries.append({"node_id": nd.id, **summ})
                async with self._write_lock:
                    self.store.append("node_confirmed", {
                        "node_id": nd.id, "mean": summ["mean"],
                        "std": summ["std"], "seeds": len(scores),
                    })

        if summaries:
            chooser = min if state.direction == "min" else max
            robust = chooser(summaries, key=lambda s: (s["mean"], s["node_id"]))
            leader = next((s for s in summaries if s["node_id"] == topk[0].id), robust)
            significant = robust["node_id"] != leader["node_id"] and one_se_better(
                robust["mean"], leader["mean"], robust["std"], robust["n"],
                state.direction, incumbent_std=leader["std"], incumbent_n=leader["n"])
            chosen = robust["node_id"]
        else:
            chosen, significant = topk[0].id, False  # all seeds failed -> keep leader
        async with self._write_lock:
            self.store.append("best_confirmed", {"node_id": chosen, "significant": significant})

    async def _confirm_node(self, nd) -> None:
        """Operator-forced multi-seed confirmation of ONE node (force_confirm). Records the per-seed
        results (for the UI Metrics/Trust tabs) + a `confirm_done` gate, but deliberately does NOT
        emit `node_confirmed` — that would put this node into the robust-selection pool and could
        promote an otherwise-worse node to best. So a forced confirm informs the operator without
        altering deterministic best-selection. Replay-safe (gated on confirm_done + per-seed memo)."""
        state = fold(self.store.read_all())
        seeds = max(self.confirm_seeds, 3)
        done = state.confirm_seed_results.get(nd.id, {})
        for s in range(seeds):
            if s in done:
                continue
            workdir = self.run_dir / "confirm" / f"node_{nd.id}_seed_{s}"
            self._seed_workspace(workdir)
            self._write_node_files(nd, workdir)
            self._write_assets(workdir)
            _t0 = time.time()
            with self.tracer.span("confirm_seed", new_trace=True, node_id=nd.id, seed=s):
                res = await anyio.to_thread.run_sync(
                    self._run_eval, nd, str(workdir), {"LOOPLAB_EVAL_SEED": str(s)}, "full")
                valid = res.metric is not None and res.exit_code == 0 and not res.timed_out
                async with self._write_lock:
                    self.store.append("confirm_eval", {
                        "node_id": nd.id, "seed": s, "eval_seconds": round(time.time() - _t0, 3),
                        "metric": res.metric if valid else None})
                    if res.drift is not None:
                        self.store.append("spec_drift", {"node_id": nd.id, "seed": s, **res.drift})
        async with self._write_lock:
            self.store.append("confirm_done", {"node_id": nd.id})   # fulfill the request (gate)

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
            # parent, never closes and the loop spins forever on repo/eval-spec runs.
            self.store.append("ablate", {"parent_id": parent_id, "impacts": {},
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
            self.store.append("ablate", {"parent_id": parent_id, "impacts": impacts})

        top = max(impacts, key=impacts.get) if impacts else (
            sorted(parent.idea.params)[0] if parent.idea.params else None)
        proposal = self.researcher.propose(state, parent)  # refine only `top`
        new_params = dict(parent.idea.params)
        if top is not None and top in proposal.params:
            new_params[top] = proposal.params[top]
        idea = Idea(operator="refine_block", params=new_params,
                    rationale=f"ablation: refine highest-impact '{top}' (impacts={impacts})")
        code = self.developer.implement(idea)
        node_id = len(fold(self.store.read_all()).nodes)
        self.store.append("node_created", {
            "node_id": node_id, "parent_ids": [parent_id], "operator": "refine_block",
            "idea": idea.model_dump(mode="json"), "code": code,
            "files": getattr(self.developer, "last_files", {}) or {}})
        self._emit_agent_report(node_id)

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
            self.store.append("ablate", {"parent_id": parent_id, "impacts": impacts,
                                         "mode": "code_blocks", "blocks": len(blocks),
                                         "top_block": top})
        top_src = ""
        if top is not None:
            s, e = blocks[int(top)]
            top_src = "\n".join(code.splitlines()[s:e])[:300]
        idea = Idea(operator="refine_block", params=dict(parent.idea.params),
                    rationale=("code-block ablation: refine the highest-impact pipeline block "
                               f"#{top} and keep the rest. Block:\n{top_src}"))
        new_code = self.developer.implement(idea)
        node_id = len(fold(self.store.read_all()).nodes)
        self.store.append("node_created", {
            "node_id": node_id, "parent_ids": [parent_id], "operator": "refine_block",
            "idea": idea.model_dump(mode="json"), "code": new_code,
            "files": getattr(self.developer, "last_files", {}) or {}})
        self._emit_agent_report(node_id)

    def _maybe_crash(self) -> None:
        if self.crash_after is None:
            return
        n_eval = sum(1 for e in self.store.read_all() if e.type == "node_evaluated")
        if n_eval >= self.crash_after:
            os._exit(137)  # simulate kill -9 (no cleanup, no run_finished)

    def _leakage_blocks(self) -> bool:
        """Leakage-first gate (I9): run the detectors on whatever split/feature/target/
        timestamp data the task exposes via `leakage_inputs()`. Emit a verdict; return
        True (abort) if a hard leak is found. Tasks without the method are skipped."""
        fn = getattr(self.task, "leakage_inputs", None)
        if not callable(fn):
            return False
        inp = fn() or {}
        verdicts = []
        if "train_rows" in inp and "test_rows" in inp:
            verdicts.append(train_test_contamination(inp["train_rows"], inp["test_rows"]))
        if "features" in inp and "target" in inp:
            verdicts.append(target_leakage(inp["features"], inp["target"]))
        if "train_timestamps" in inp and "test_timestamps" in inp:
            verdicts.append(temporal_leakage(inp["train_timestamps"], inp["test_timestamps"]))
        leak = any(v.get("leak") for v in verdicts)
        self.store.append("data_leakage", {"leak": leak, "verdicts": verdicts})
        return leak

    def _store_case(self, final: RunState) -> None:
        """Cross-run memory (I19): persist the best result as a retrievable case."""
        if not self.memory_dir:
            return
        best = final.best()
        if best is None:
            return
        lib = JsonlCaseLibrary(Path(self.memory_dir) / "cases.jsonl")
        lib.add({
            "task_id": final.task_id,
            "goal": final.goal,
            "direction": final.direction,
            "params": best.idea.params,
            "metric": best.confirmed_mean if best.confirmed_mean is not None else best.metric,
            "rationale": best.idea.rationale,
        })
