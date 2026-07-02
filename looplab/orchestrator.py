"""Engine / control loop (I6, ADR-12/18). anyio structured concurrency:
node *creation* is sequential & deterministic; node *evaluation* fans out under a
CapacityLimiter. State is always a fresh fold of the log (files-as-truth); resume
is just re-entering this loop on an existing run dir — pending nodes get re-evaluated
idempotently, and node ids are a monotonic count so reruns never duplicate.

A crash can be injected (for the resume test) via `crash_after`: hard-exit after N
node_evaluated events have been written, simulating `kill -9` mid-run.
"""
from __future__ import annotations

import functools
import hashlib
import os
import sys
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
    run that simply printed no metric. Ordered most-specific first. (The "idea_rejected" reason
    is NOT classified here — it is set by `_evaluate` when the crash-triage agent judges the idea
    fundamentally wrong, which then steers `debug_action` away from that lineage.)"""
    if getattr(res, "drift", None) is not None:
        return "drift"
    if res.timed_out:
        return "timeout"
    if (res.stderr or "").startswith("setup failed:"):
        return "setup"
    if res.exit_code != 0:
        # OOM-kill: on a memory-capped pod (a JupyterHub cgroup limit) the kernel SIGKILLs a too-big
        # eval — exit -9 (POSIX, Python returns -signal) or 137 (128+9) — with little/no Python
        # traceback. Distinguish it from an ordinary crash so it's triaged as REPAIRABLE (reduce
        # memory: batch/model size, subsample) instead of a silent abandon that recurs on every heavy
        # eval. Heuristic: the SIGKILL signature with no real traceback in stderr (a timeout-kill is
        # also SIGKILL but `res.timed_out` already returned "timeout" above, so it never reaches here).
        if res.exit_code in (-9, 137) and "Traceback" not in (res.stderr or ""):
            return "oom"
        return "crash"
    return "no_metric"          # exit 0 but no parseable metric emitted


# Env-prep: max auto-install + re-run rounds per node before giving up (a re-run can reveal a
# *second* missing lib; bound it so an odd install state can't loop). The `_dep_failed` cache
# already prevents re-attempting the same uninstallable module.
_MAX_DEP_ROUNDS = 6

# Mechanical-failure signatures: a crash whose stderr matches one of these is almost always a
# code/runtime defect (bad import, removed/renamed API, typo) — repairable in place from the
# traceback alone. Used by the deterministic crash-triage fallback when no LLM agent is wired.
_MECHANICAL_MARKERS = (
    "ImportError", "ModuleNotFoundError", "NameError", "AttributeError", "SyntaxError",
    "IndentationError", "TypeError", "unexpected keyword argument", "has no attribute",
    "is not defined", "no attribute",
)


def _rule_triage(reason: str, error: str, attempt: int, max_attempts: int) -> dict:
    """Deterministic crash-triage fallback (no LLM): repair a clear MECHANICAL crash — or a TIMEOUT
    (too slow, not a wrong idea -> reduce compute) — while attempts remain, otherwise abandon.
    Conservatively NEVER returns "reject_idea" — killing a whole idea lineage is a strong call
    reserved for the LLM agent, so the rule path stays safe with the unified agent off (it only ever
    repairs obvious mechanical crashes / timeouts or abandons)."""
    err = error or ""
    if reason in ("timeout", "oom") and attempt <= max_attempts:
        why = ("timeout — reduce compute to fit the budget (rule-based)" if reason == "timeout"
               else "OOM-killed — reduce memory: batch/model size or subsample to fit the pod limit (rule-based)")
        return {"action": "repair", "rationale": why}
    mechanical = any(s in err for s in _MECHANICAL_MARKERS)
    if reason == "crash" and mechanical and attempt <= max_attempts:
        return {"action": "repair", "rationale": "mechanical crash (rule-based)"}
    return {"action": "abandon", "rationale": "non-mechanical failure or attempts exhausted (rule-based)"}


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
        sweep_timeout_mult: float = 8.0,  # intra-node sweep nodes get this × the single-eval budget
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
        seed_mode: str = "auto",    # RepoTask node seeding fallback: auto|tracked|all (per-editable overrides)
        # --- A7 Strategist + richer-operator knobs (config-first; defaults == today's behavior) ---
        n_seeds: int = 3,
        max_nodes: int = 8,
        policy_name: str = "greedy",
        ablate_every: int = 0,
        strategist=None,            # Optional[Strategist]; None => static config policy (default)
        strategist_every: int = 3,
        deep_researcher=None,       # Optional[DeepResearcher]; None => Deep-Research stage off
        deep_research_every: int = 0,  # run the stage every N created nodes (0 = manual/strategist only)
        concurrent_research: bool = False,  # overlap a due research "think" with the GPU-bound eval (opt-in)
        report_writer=None,         # Optional[ReportWriter]; None => agent report off (deterministic only)
        report_every: int = 0,      # regenerate the run report every N created nodes (0 = manual only)
        developer_factory=None,     # Optional[Callable[[str], Developer]] for live backend swap
        merge_mode: str = "mean",        # A0b: "mean" | "ensemble"
        complexity_cue: bool = False,    # A0d: breadth-keyed prompt hint
        budget_aware: bool = False,      # A5: surface remaining eval budget into the prompt
        failure_reflection: bool = False,  # A4: reflect on recent failed branches in the prompt
        deep_repair: bool = False,       # C3: structured failure-taxonomy repair context
        localize_faults: bool = False,   # C1: surface fault-localized files for repo tasks
        feature_engineering: bool = False,  # I1: CV-gated feature-engineering directive
        ablate_code_blocks: bool = False,  # A0a: ablate pipeline code blocks, not just params
        proxy_scorer=None,          # A6: Optional[ProxyScorer] early-signal candidate gate
        proxy_kill_fraction: float = 0.0,
        reward_hack_detect: bool = False,   # B5: flag suspicious wins
        trust_gate: str = "audit",          # T2: audit|gate|block — what a hack/leak flag does to selection
        code_leakage_detect: bool = False,  # I3: static code-leakage scan per node
        critic_check: bool = False,         # C4: execution-free critic per node
        redact_output: bool = False,        # B3: redact secrets from persisted output tails
        novelty_gate: bool = False,         # E1: dedup near-duplicate proposals
        novelty_epsilon: float = 0.05,
        reflection_priors: bool = False,    # E4: cross-run meta-review priors
        surrogate_explore: float = 0.1,     # A2/A3: explore weight for a lazily-wired BOHB surrogate
        unified_agent: bool = False,        # one agent plays Researcher+Developer(+Strategist)
        agent_drives_actions: bool = False,  # agent picks the next macro action (within a legal gate)
        inline_repair: bool = True,          # hybrid: triage + repair a crashed node IN PLACE (no new node)
        inline_repair_attempts: int = 0,     # max in-place repair retries per node (0 = UNLIMITED)
        inline_repair_stuck_repeat: int = 4, # abandon when the SAME error repeats this many times in a row
        inline_repair_reasons: tuple = ("crash", "timeout", "oom"),  # reasons eligible for inline repair
        auto_install_deps: bool = True,      # pip-install a missing KNOWN lib + re-run (trusted_local only)
        dep_install_timeout: float = 900.0,  # per-package install wall-clock budget (seconds)
        dep_installer=None,                  # Optional[Callable] install hook (test seam; default = deps.install)
        agent_control: Optional[dict] = None,  # per-setting allow-list of roles that may change it (governance)
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
        # The policy's OWN node budget is the base a live add_nodes override extends — NOT self.max_nodes
        # (the engine default can differ from a passed-in policy's, e.g. in tests). Tracked separately so
        # the override is applied idempotently (absolute set per iteration) without compounding, and
        # re-captured on a strategy-driven policy swap below.
        self._base_max_nodes = getattr(policy, "max_nodes", max_nodes)
        self._policy_name = policy_name
        self._ablate_every = ablate_every
        self.strategist = strategist
        self.strategist_every = max(1, strategist_every)
        self.deep_researcher = deep_researcher
        self.deep_research_every = max(0, deep_research_every)
        self.concurrent_research = concurrent_research
        self.report_writer = report_writer
        self.report_every = max(0, report_every)
        self.developer_factory = developer_factory
        self._developer_name = "default"
        self._merge_mode = merge_mode
        self._complexity_cue = complexity_cue
        self._prefer_sweep = False   # A7: Strategist-set bias toward intra-node sweeps (audit-driven)
        self._budget_aware = budget_aware
        self._failure_reflection = failure_reflection
        self._deep_repair = deep_repair
        # Hybrid in-node crash repair (triage + inline repair). See Settings.inline_repair.
        self._inline_repair = inline_repair
        self._inline_repair_attempts = max(0, int(inline_repair_attempts))   # 0 = unlimited
        self._inline_repair_stuck_repeat = max(2, int(inline_repair_stuck_repeat))
        self._inline_repair_reasons = tuple(inline_repair_reasons or ("crash",))
        # Environment self-prep (deps.py): auto-install a missing KNOWN library and re-run, instead
        # of letting the crash-triage agent reject the idea. Trusted_local tier ONLY — the Docker
        # tiers run --network none and must not mutate a shared image. `_dep_attempted` records every
        # module we've already run pip for THIS run (one attempt per module: success => now present
        # forever; failure => won't change on retry), so an offline/misnamed package can't loop.
        # `_dep_lock` serializes pip + that set across parallel evals (pip is not concurrency-safe).
        self._auto_install_deps = bool(auto_install_deps) and trust_mode == "trusted_local"
        self._dep_install_timeout = float(dep_install_timeout)
        self._dep_installer = dep_installer        # None => deps.install (real pip)
        self._dep_attempted: set[str] = set()
        import threading as _threading
        self._dep_lock = _threading.Lock()
        # Agent governance (Settings.agent_control): per-setting allow-list of which roles may change it
        # at runtime. A setting absent from the map is LOCKED (no agent). Enforced at the strategist /
        # boss / researcher seams via `_agent_may`. None => the conservative defaults are off (locked).
        self._agent_control: dict = dict(agent_control or {})
        self._localize_faults = localize_faults
        self._feature_engineering = feature_engineering
        self._ablate_code_blocks = ablate_code_blocks
        self.proxy_scorer = proxy_scorer
        self.proxy_kill_fraction = proxy_kill_fraction
        self.reward_hack_detect = reward_hack_detect
        self.trust_gate = trust_gate if trust_gate in ("audit", "gate", "block") else "audit"
        self._code_leakage_detect = code_leakage_detect
        self._critic_check = critic_check
        self._redact_output = redact_output
        self._novelty_gate = novelty_gate
        self._novelty_epsilon = novelty_epsilon
        self._reflection_priors = reflection_priors
        self._surrogate_explore = surrogate_explore
        # Unified self-driving agent: in unified mode `researcher is developer` (one object plays
        # both roles); `agent_drives_actions` additionally lets it pick the next macro action.
        self.unified_agent = unified_agent
        self.agent_drives_actions = unified_agent and agent_drives_actions
        self._prior_note_text = ""   # E4: cross-run meta-review prior, loaded at run start
        self._strategy_fidelity: Optional[str] = None   # None => use the Idea's own profile
        self.max_parallel = max_parallel
        self.timeout = timeout
        self.sweep_timeout_mult = max(1.0, sweep_timeout_mult)
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
        self._seed_mode = seed_mode or "auto"   # run-wide fallback for per-editable seeding
        self._run_setup_done = False             # run-level (once) dependency setup guard
        import threading as _threading2
        self._run_setup_lock = _threading2.Lock()   # _run_eval runs on parallel worker threads; the
        #   check-then-set on _run_setup_done races without this, launching run_setup (pip) N times
        self._drift_warned = False   # one-shot guard for the #8 drift-coverage warning
        # Fail loud at START, not mid-sweep: the untrusted tier needs docker, so verify it once
        # here instead of re-discovering (and re-scanning PATH) on every eval's make_docker_wrap.
        if trust_mode in ("untrusted", "hostile"):
            import shutil as _sh
            if not _sh.which("docker"):
                raise RuntimeError(
                    f"trust_mode={trust_mode!r} needs the docker CLI to sandbox evals, but it was "
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
        self.task_has_columns = callable(getattr(task, "columns", None))   # I1: tabular task?
        # Out-of-process / host-side grading (B1+, general): a task may expose `host_grader()` ->
        # {"predictions": <file>, "scorer": <name>, "labels": <held-out answer key>, "key"?: ...}. When
        # present, the candidate (a separate sandbox process) writes ONLY predictions; the host (this
        # engine process) scores them — the labels live in engine memory and never touch the candidate
        # FS or the event log. Works for ANY solution.py-path task, not just MLEBench.
        hg = getattr(task, "host_grader", None)
        self._host_grader: Optional[dict] = hg() if callable(hg) else None
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
        def _protected_after_resolve(target) -> bool:
            # Check the RESOLVED relative path against the protected set, not the raw name: a name like
            # "sub/../grader.py" passes a raw-string compare yet resolves to wd/grader.py and would
            # overwrite the protected grader otherwise.
            try:
                rel = target.relative_to(wd).as_posix()
            except ValueError:
                return False
            return _os.path.normcase(rel) in protected
        for name, content in files.items():
            if _os.path.normcase(str(name).replace("\\", "/")) in protected:
                continue
            target = (wd / name).resolve()
            if wd not in target.parents:        # defense-in-depth: never escape workdir
                continue
            if _protected_after_resolve(target):
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
            if _protected_after_resolve(target):
                continue
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass

    # ----------------------------------------------------------------- public
    async def run(self) -> RunState:
        state = fold(self.store.read_all())
        if not state.run_id:
            # SETUP PHASE (task + data), made an explicit, ONLINE-watchable phase: the pre-node work
            # (fingerprint the workspace, hash data provenance, profile columns, write AGENTS.md) is
            # otherwise silent between run_started and the first node. `setup_started` +/ `setup_step`
            # + `setup_finished` events land in the activity feed live, and a `setup` span (node_id=-1)
            # captures the trace so the UI's Setup pseudo-node shows what happened. fold ignores these
            # (forward-compat), so they're pure observability.
            _su_t0 = time.time()
            self.store.append("setup_started",
                              {"phase": "task+data", "repo": bool(self._repo_spec),
                               "goal": (self.task.goal or "")[:200]})
            def _su_step(step: str, **detail):
                self.store.append("setup_step", {"step": step, **detail})
            with self.tracer.span("setup", new_trace=True, node_id=-1) as _su:
                def _ev(name, **kv):
                    if _su is not None:
                        _su.event(name, **kv)
                cfg_hash = hashlib.sha256(
                    orjson.dumps(self.task.model_dump(mode="json"))
                ).hexdigest()[:12]
                # Reproducibility (item #4): pin the editable repo(s)+data fingerprint at start so a
                # resume can tell whether the source workspace changed underneath.
                _ev("workspace_fingerprint")
                wf = self._workspace_fingerprint()
                _su_step("workspace fingerprint", sources=list(wf.keys()))
                self.store.append(
                    "run_started",
                    {
                        "run_id": self.run_dir.name,
                        "task_id": self.task.id,
                        "goal": self.task.goal,
                        "direction": self.task.direction,
                        "config_hash": cfg_hash,
                        "workspace": wf,
                        # T2 trust enforcement: recorded here so the pure fold applies the same
                        # gate on replay/resume (config isn't available to `replay.fold`). Absent in
                        # old logs -> "audit" -> byte-identical legacy selection.
                        "trust_gate": self.trust_gate,
                    },
                )
                # AGENTS.md (I18): task/contract context for coding-agent backends. Runtime line is
                # honest about libs/hardware — capable tasks get the auto-install capability sentence,
                # offline/synthetic tasks stay numpy+stdlib (task_runtime_caps returns None for those).
                from .hardware import detect_gpu, task_runtime_caps
                _md_caps = task_runtime_caps(self.task, auto_install=self._auto_install_deps,
                                             gpu=detect_gpu() if self._auto_install_deps else None)
                (self.run_dir / "AGENTS.md").write_text(
                    generate_agents_md(self.task, runtime_caps=_md_caps), encoding="utf-8")
                _ev("agents_md"); _su_step("wrote AGENTS.md")
                # D4 data provenance: pin a content hash of every task asset/dataset into the run so a
                # result is tied to the exact data (repo tasks also pin via `workspace`). Reproducibility.
                prov = {name: hashlib.sha256(
                            c.encode("utf-8") if isinstance(c, str) else bytes(c)).hexdigest()[:16]
                        for name, c in (self._assets or {}).items()}
                if prov:
                    self.store.append("data_provenance", {"assets": prov})
                    _ev("data_provenance", n=len(prov)); _su_step("data provenance", assets=list(prov))
                # Out-of-process host-side grading active: record WHICH scorer + how many held-out labels
                # (NEVER the labels themselves — the log is readable). Surfaced in the Trust panel.
                if self._host_grader is not None:
                    hg = self._host_grader
                    evt = {
                        "scorer": hg.get("scorer", "rmse"),
                        "predictions": self._graded_output_name()}
                    if hg.get("kind") == "mlebench":          # real MLE-bench: answers live in the
                        evt["competition"] = hg.get("competition")   # mle-bench data dir, never here —
                        # so there is no in-memory label list to count; n_labels=0 would mislead the Trust
                        # panel into "nothing held out". Omit it; `competition` signals host-held answers.
                    else:
                        evt["n_labels"] = len(hg.get("labels") or [])
                    self.store.append("host_grading", evt)
                # Grounding pre-phase (I16): profile the dataset if the task exposes one.
                cols = getattr(self.task, "columns", None)
                if callable(cols):
                    self.store.append("data_profiled", {"columns": profile_dataset(cols())})
                    _ev("data_profiled"); _su_step("data profiled")
                # Leakage-first grounding (I9): if the task exposes split/feature/target/time
                # data and a leak is detected, refuse to run — don't produce results on leaky data.
                if self._leakage_blocks():
                    self.store.append("run_finished", {"reason": "leakage"})
            self.store.append("setup_finished", {"seconds": round(time.time() - _su_t0, 3)})
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
        self._prior_note_text = self._load_reflection_priors()   # E4: cross-run meta-learned priors
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
            # Boss (run-chat) resource retune: a `budget_extend` may carry timeout / max_parallel. Apply
            # to self.* (read fresh per eval / per batch) only when the matrix grants the boss — so the
            # operator can e.g. give the run more per-eval time or more parallelism mid-flight.
            _bo = state.budget_overrides
            if "timeout" in _bo and self._agent_may("boss", "timeout"):
                try: self.timeout = max(0.1, float(_bo["timeout"]))
                except (TypeError, ValueError): pass
            if "max_parallel" in _bo and self._agent_may("boss", "max_parallel"):
                try: self.max_parallel = max(1, int(_bo["max_parallel"]))
                except (TypeError, ValueError): pass
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
                try:
                    self._create_injected_node(req)
                except Exception as e:  # noqa: BLE001 - a malformed operator/API inject must not
                    # crash-loop the engine: without advancing the gate, every resume replays the same
                    # bad request and dies again, leaving the run unrecoverable. Record + skip it.
                    self.store.append("inject_failed",
                                      {"idx": state.injects_done, "error": str(e)[:500]})
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

            # Deep-Research stage (Phase 2): a "go think hard" step over all results + the web that
            # writes a memo to steer the next batch. Fires on a manual request, a cadence, or a
            # Strategist `request_research`. No-op when the stage is off. Replay-safe (gated).
            state = self._maybe_deep_research(state)

            # Run report (conclusion-first, agent-authored): regenerate on a node-count cadence so the
            # Report grows with the search. Audit-only sidecar; no-op when off. Replay-safe (gated on
            # the report's at_node). The deterministic report renders regardless.
            state = self._maybe_refresh_report(state)

            # Effective node budget: a `budget_extend` with add_nodes (e.g. "give the run 10 more
            # nodes") raises the policy's max_nodes so a reopened/resumed run keeps proposing
            # experiments instead of immediately re-finishing. Applied HERE — AFTER any in-loop policy
            # swap (strategist / set_strategy above, which rebuilds the policy un-extended) and right
            # before action selection — so the override is never dropped on a swap iteration. Floored
            # at the current node count so a stale/negative delta can't shrink the gate below work done.
            self.policy.max_nodes = max(
                len(state.nodes),
                self._base_max_nodes + int(state.budget_overrides.get("add_nodes", 0) or 0))

            # Action selection: the pure policy decides, UNLESS the unified agent self-drives —
            # in which case it picks one action from the policy-derived legal-action gate (so the
            # pipeline stays disciplined no matter what the agent chooses).
            actions = (self._agent_next_actions(state) if self.agent_drives_actions
                       else self.policy.next_actions(state))
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
                # Final report on clean completion: the confirm pass just ran, so the champion +
                # robustness are settled — this is the definitive report (it reflects post-confirmation
                # state a same-at_node cadence report wouldn't). Skip only when the cadence is off
                # (report_every=0 = manual-only), so "manual only" stays truly call-free.
                if self.report_writer is not None and self.report_every > 0:
                    state = self._write_report(state, trigger="finish")
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
                    if "_scores" in a:   # policy exposed candidate scores -> surface "why this node"
                        self.store.append("policy_decision",
                                          {"scores": a["_scores"], "chosen": a.get("_chosen"),
                                           "reason": a.get("_reason")})
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
                # Concurrency seam (opt-in, default off): overlap a DUE deep-research "think" with the
                # GPU-bound eval — the agent is otherwise idle while the node trains. _compute_deep_research
                # is pure compute on the `state` snapshot (no event-log writes, span skipped), so the
                # engine stays the SOLE writer: the memo is recorded from THIS (main) task after the evals.
                # Only a real win when the LLM is remote (no GPU contention); needs live-run validation.
                rtrig = self._due_research_trigger(state) if self.concurrent_research else None
                rbox: dict = {}
                async with anyio.create_task_group() as tg:
                    if rtrig is not None:
                        async def _bg_research(snap=state, trig=rtrig):
                            rbox["memo"] = await anyio.to_thread.run_sync(
                                functools.partial(self._compute_deep_research, snap, trig, trace=False))
                        tg.start_soon(_bg_research)
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
                # Record the overlapped memo now — main task is the sole writer, AFTER the eval events.
                if rbox.get("memo") is not None:
                    self._record_deep_research(rbox["memo"], trigger=rtrig, manual=False)
            else:
                # G3 distributed/parallel eval: fan out under a CapacityLimiter (worker pool). The
                # eval-budget guard the review flagged for this path: cap the number STARTED so an
                # over-budget run launches at most ~max_parallel more evals, not the whole batch.
                limiter = anyio.CapacityLimiter(self.max_parallel)
                cur = fold(self.store.read_all())
                started = 0
                async with anyio.create_task_group() as tg:
                    for a in evals:
                        if a["node_id"] in cur.aborted_nodes:
                            n = cur.nodes.get(a["node_id"])
                            if n is not None and n.status is NodeStatus.pending:
                                self.store.append("node_failed", {
                                    "node_id": a["node_id"], "error": "aborted by operator",
                                    "reason": "aborted", "eval_seconds": 0.0})
                            continue
                        # Budget guard (parallel path): cap each fan-out batch to the worker-pool size.
                        # `cur` is folded ONCE before this loop and never changes mid-batch (the evals
                        # join only at the task-group exit), so a budget check on cur here is dead — the
                        # real enforcement is the per-iteration outer guard (it re-folds and finishes the
                        # run once total_eval_seconds >= max_es). Capping the batch to max_parallel bounds
                        # the overshoot to ~one batch instead of launching the whole `evals` list at once.
                        if started >= self.max_parallel:
                            break
                        tg.start_soon(self._evaluate, a["node_id"], limiter)
                        started += 1

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
            self._write_reflection_note(fold(self.store.read_all()))   # E4 cross-run meta-review prior

        # The SQLite read-model is a DERIVED, rebuildable cache that nothing in-process reads (the UI
        # folds events.jsonl / reads trace.json). On a FUSE/S3 run dir (JupyterHub geesefs) sqlite's
        # byte-range locks are unsupported and the write can raise `database is locked` / `disk I/O
        # error` — which must NOT abort an otherwise-finished run. Build best-effort; the run state we
        # actually need comes from the event fold regardless.
        try:
            final = build_readmodel(self.store.read_all(), self.run_dir / "readmodel.sqlite")
        except Exception as e:  # noqa: BLE001 - derived cache; a FUSE sqlite failure must not kill finalize
            final = fold(self.store.read_all())
            try:
                self.store.append("readmodel_skipped", {"error": str(e)[:300]})
            except Exception:  # noqa: BLE001 - even the audit note is best-effort
                pass
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
                for attr in ("client", "inner", "fallback", "researcher", "developer",
                             "strategist", "tools"):
                    child = getattr(obj, attr, None)
                    if child is not None and child is not obj:
                        stack.append(child)
                # Unified agent: per-stage clients (strategy/pilot) not on the attr graph above.
                for c in (getattr(obj, "stage_clients", None) or []):
                    if c is not None and c is not obj:
                        stack.append(c)
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
        return {k: s.get(k) for k in ("policy", "policy_params", "developer", "operators", "fidelity", "request_research")}

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
        # Mean per-node eval cost so far — the cost signal the Strategist uses to bias toward an
        # intra-node sweep (amortizing data load / warm-up pays off when each eval is expensive).
        ev = [n.eval_seconds for n in state.nodes.values() if n.eval_seconds]
        avg_es = (sum(ev) / len(ev)) if ev else None
        return StrategyContext(
            node_count=len(state.nodes),
            phase=run_phase(state, self.n_seeds),
            eval_budget_remaining=rem,
            failure_rate=failure_rate(state),
            improves_since_best=improves_since_best(state),
            is_numeric_space=is_numeric_space(state),
            avg_eval_seconds=avg_es,
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

    def _ensure_surrogate(self) -> None:
        """Wrap the Researcher in a SurrogateResearcher if it isn't already (idempotent). Used when a
        mid-run strategy switch turns BOHB on: BOHB is ASHA's racing schedule PLUS the surrogate
        proposer, and the proposer is only wired at startup for policy=bohb/surrogate_proposer — so a
        Strategist switching to bohb would otherwise run bare ASHA. Needs numeric bounds; if the
        Researcher (or its inner/fallback) exposes none, this is a no-op (bohb degrades to ASHA)."""
        from .surrogate import SurrogateResearcher
        # Unified mode: re-wrapping `self.researcher` here would desync it from `self.developer`
        # (the same agent object) — the cli already skips the startup surrogate wrap for the same
        # reason (R1). A mid-run switch to bohb degrades to bare ASHA, which is acceptable.
        if self.unified_agent or isinstance(self.researcher, SurrogateResearcher):
            return
        bounds = (getattr(self.researcher, "bounds", None)
                  or getattr(getattr(self.researcher, "inner", None), "bounds", None)
                  or getattr(getattr(self.researcher, "fallback", None), "bounds", None))
        if bounds:
            self.researcher = SurrogateResearcher(bounds, fallback=self.researcher,
                                                  explore=self._surrogate_explore)

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
        if "prefer_sweep" in ops:
            self._prefer_sweep = bool(ops["prefer_sweep"])
        # Resource budgets the Strategist may retune live (gated by the governance matrix). self.timeout
        # is read fresh per eval and self.max_parallel rebuilds the CapacityLimiter each batch, so a
        # mid-run change takes effect on the next node without any rewiring.
        if "timeout" in strat and self._agent_may("strategist", "timeout"):
            try:
                self.timeout = max(0.1, float(strat["timeout"]))
            except (TypeError, ValueError):
                pass
        if "max_parallel" in strat and self._agent_may("strategist", "max_parallel"):
            try:
                self.max_parallel = max(1, int(strat["max_parallel"]))
            except (TypeError, ValueError):
                pass
        pol = strat.get("policy")
        if pol:
            try:
                # Strip the names make_policy takes as explicit kwargs: a policy_params entry like
                # {"n_seeds": 4} would otherwise raise "multiple values for keyword argument",
                # silently dropping the whole switch (recorded decision diverging from live policy).
                pp = {k: v for k, v in (strat.get("policy_params") or {}).items()
                      if k not in ("n_seeds", "max_nodes", "ablate_every")}
                self.policy = make_policy(pol, n_seeds=self.n_seeds, max_nodes=self.max_nodes,
                                          ablate_every=self._ablate_every, **pp)
                self._base_max_nodes = getattr(self.policy, "max_nodes", self.max_nodes)  # new base for the live override
                # A3 BOHB = ASHA racing + the surrogate proposer. make_policy only builds the racing
                # half; wire the surrogate now so a mid-run switch to bohb isn't bare ASHA.
                if pol == "bohb":
                    self._ensure_surrogate()
                self._policy_name = pol
            except (ValueError, TypeError):
                pass    # keep the current policy on a bad spec (validate_strategy already whitelisted)
        fid = strat.get("fidelity")
        if fid in ("smoke", "full"):
            self._strategy_fidelity = fid
        elif fid == "adaptive":
            self._strategy_fidelity = None
        dev = strat.get("developer")
        # Unified mode: researcher IS developer (one agent). A live developer-backend swap would
        # replace `self.developer` with a different object, desyncing it from `self.researcher` (and
        # the factory, still seeing unified_agent=True, would build a whole new agent). The unified
        # agent owns its own implement stage — skip the swap rather than fracture the identity (R1).
        if dev and self.developer_factory is not None and dev != self._developer_name \
                and not self.unified_agent:
            try:
                self.developer = self.developer_factory(dev)
                self._developer_name = dev
            except Exception:  # noqa: BLE001 — a bad backend swap must never abort the run
                pass

    def _maybe_consult_strategist(self, state: RunState) -> RunState:
        """Operator/boss pin first (HITL parity), then the bounded-cadence Strategist consult.
        Records a `strategy_decision` and re-folds only when the strategy actually changes.

        An operator/boss `set_strategy` pin owns ONLY the fields it names (policy / policy_params /
        fidelity); those stay in force for the rest of the run (until re-pinned), while the
        autonomous Strategist keeps tuning everything else. The pin is MERGED onto the live strategy
        (not reset to the bare pin) and re-asserted only when a pinned field actually drifts — that,
        plus overlaying the pinned fields onto the Strategist's own decision below, is what stops the
        pin and the Strategist from thrashing (the old "reset to bare pin on any divergence"
        oscillated the policy every consult and dropped the Strategist's fidelity/operators)."""
        pin = state.pending_strategy or {}
        raw_pin = {k: pin[k] for k in ("policy", "policy_params", "fidelity")
                   if pin.get(k) is not None}
        consulting = self.strategist is not None and self._should_consult(state)
        active_core = self._strategy_core(state.active_strategy)
        # Cheap pre-check (no ctx/validate): a pin "drifts" if a raw pinned field differs from what's
        # active. For an INVALID pin this is a false alarm (it can never become active), so we still
        # validate below before acting on it.
        pin_drift = bool(raw_pin) and any(active_core.get(k) != v for k, v in raw_pin.items())
        if not pin_drift and not consulting:
            return state
        ctx = self._strategy_ctx(state)
        # Validate the pin against the SAME whitelist the engine applies, keeping only the pinned
        # fields that survive. The boss `strategy` action carries free-text policy/fidelity (server
        # `_Action.policy/fidelity`, unvalidated), so an out-of-whitelist value would otherwise be
        # overlaid RAW onto the recorded strategy below — diverging from the live policy that
        # make_policy silently rejects — and, never matching active_strategy, re-assert (and starve
        # the autonomous Strategist + spam the log) on every consult. Dropping it here makes an
        # invalid pin a harmless no-op.
        vpin = validate_strategy({**raw_pin, "source": "operator"}, ctx) if raw_pin else None
        pin_fields = {k: vpin[k] for k in raw_pin if vpin and k in vpin}
        # 1. Re-assert the pin only if a VALID pinned field isn't currently in force (merge onto active).
        if pin_fields and any(active_core.get(k) != v for k, v in pin_fields.items()):
            strat = validate_strategy({**(state.active_strategy or {}), **pin_fields,
                                       "source": "operator"}, ctx)
            if strat:
                strat.setdefault("rationale", "operator-pinned strategy")
                self._record_strategy(strat, state, ctx)
                return fold(self.store.read_all())
        # 2. Bounded-cadence Strategist consult — but the pin wins over it for the pinned fields.
        if consulting:
            strat = validate_strategy(self.strategist.decide(state, ctx), ctx)
            if strat:
                strat.update(pin_fields)   # pinned (validated) policy/fidelity are non-negotiable
                if self._strategy_core(strat) != self._strategy_core(state.active_strategy):
                    self._record_strategy(strat, state, ctx)
                    return fold(self.store.read_all())
        return state

    # ----------------------------------------------------------------- Deep-Research stage (P2)
    def _maybe_deep_research(self, state: RunState) -> RunState:
        """Run the Deep-Research stage when there's demand, then re-fold. Three triggers, each gated
        for replay safety: a MANUAL `deep_research` control event (counter gate), a CADENCE
        (`deep_research_every`, once per node-count), or a Strategist `request_research` decided at
        this node-count. No-op when the stage is off or already served. Records `research_completed`
        (audit-only sidecar) and feeds the memo's directions back as a standing hint."""
        n = len(state.nodes)
        # Manual: serve outstanding requests first, regardless of node-count (operator asked now).
        if len(state.research_requests) > state.research_served:
            return self._run_deep_research(state, trigger="manual", manual=True)
        # Auto triggers only at a creation decision point (no pending evals), never re-firing at a
        # node-count already researched (the at_node gate makes resume a no-op).
        if state.pending_nodes() or n == 0 or self._already_researched_at(state, n):
            return state
        if self.deep_research_every and n % self.deep_research_every == 0:
            return self._run_deep_research(state, trigger="cadence", manual=False)
        hist = state.strategy_history
        if (hist and hist[-1].get("at_node") == n
                and (hist[-1].get("strategy") or {}).get("request_research")):
            return self._run_deep_research(state, trigger="strategist", manual=False)
        return state

    @staticmethod
    def _already_researched_at(state: RunState, n: int) -> bool:
        return any((m or {}).get("at_node") == n for m in state.research)

    def _run_deep_research(self, state: RunState, *, trigger: str, manual: bool) -> RunState:
        """Execute one Deep-Research step (serial path) and record it, then re-fold. Always records a
        `research_completed` event (even with no model wired, so a manual request's gate advances and
        the loop doesn't spin)."""
        memo = self._compute_deep_research(state, trigger)
        self._record_deep_research(memo, trigger=trigger, manual=manual)
        return fold(self.store.read_all())

    def _compute_deep_research(self, state: RunState, trigger: str, *, trace: bool = True):
        """PURE compute: run one Deep-Research step and RETURN the memo WITHOUT writing the event log,
        so it can run in a worker thread concurrently with an eval while the engine stays the sole
        writer. Best-effort — never raises (a crash/None model yields a stub so the gate still advances).
        `trace=False` skips the span: the tracer is not safe to write from the concurrent worker."""
        from .models import ResearchMemo
        if self.deep_researcher is None:
            return ResearchMemo(at_node=len(state.nodes), trigger=trigger,
                                summary="(deep research unavailable: no model configured)")
        try:
            if trace:
                with self.tracer.span("deep_research", new_trace=True, trigger=trigger):
                    return self.deep_researcher.research(state, trigger=trigger)
            return self.deep_researcher.research(state, trigger=trigger)
        except Exception as exc:  # noqa: BLE001 — advisory sidecar must never kill the run
            return ResearchMemo(at_node=len(state.nodes), trigger=trigger,
                                summary=f"(deep research failed: {exc})")

    def _record_deep_research(self, memo, *, trigger: str, manual: bool) -> None:
        """Append the memo to the event log (engine = sole writer; called only from the main task)."""
        self.store.append("research_completed", {
            "memo": memo.model_dump(mode="json"),
            "at_node": memo.at_node, "trigger": trigger, "served_manual": manual})
        # Steer the next proposals: surface the memo's directions as a standing operator hint (the
        # same channel the Researcher already reads), so deep research actually informs planning.
        if memo.recommended_directions:
            self.store.append("hint", {
                "text": "deep-research directions: " + "; ".join(memo.recommended_directions[:5]),
                "source": "deep_research"})

    def _due_research_trigger(self, state: RunState) -> str | None:
        """Is an AUTO deep-research trigger (cadence/strategist) due at the current node-count? Used by
        the concurrent-research seam to overlap the "think" with an in-flight eval. Mirrors the auto
        triggers in _maybe_deep_research but WITHOUT the no-pending gate (we overlap with pending evals
        on purpose). Manual requests stay on the serial path; the at_node gate (a memo recorded at this
        node-count) keeps the serial path from re-firing after the concurrent memo lands."""
        if self.deep_researcher is None:
            return None
        n = len(state.nodes)
        if n == 0 or self._already_researched_at(state, n):
            return None
        if self.deep_research_every and n % self.deep_research_every == 0:
            return "cadence"
        hist = state.strategy_history
        if (hist and hist[-1].get("at_node") == n
                and (hist[-1].get("strategy") or {}).get("request_research")):
            return "strategist"
        return None

    def _maybe_refresh_report(self, state: RunState) -> RunState:
        """Regenerate the agent-authored run report on a node-count cadence, then re-fold. No-op when
        the writer is off, when there's nothing evaluated yet, or when the report is already current
        for this node-count (the `at_node` gate makes resume a no-op). Best-effort sidecar."""
        if self.report_writer is None or self.report_every <= 0:
            return state
        if state.pending_nodes() or not state.evaluated_nodes():
            return state
        n = len(state.nodes)
        last = (state.report or {}).get("at_node")
        if n == 0 or last == n:                       # nothing new / already current (resume-safe)
            return state
        # Fire once at least `report_every` NEW nodes have accumulated since the last report. Using a
        # since-last threshold (not `n % report_every == 0`) means a failed/merge/ablate node-count
        # jump can't step over the only multiple and silently skip the whole window.
        if n - (last or 0) < self.report_every:
            return state
        return self._write_report(state, trigger="cadence")

    def _write_report(self, state: RunState, *, trigger: str) -> RunState:
        """Generate one run report and record it as a `report_generated` event, then re-fold. Never
        raises — the writer itself degrades to a minimal report on any failure."""
        if self.report_writer is None:
            return state
        with self.tracer.span("report", new_trace=True, trigger=trigger):
            content = self.report_writer.generate(state, trigger=trigger)
        self.store.append("report_generated", {
            "content": content, "at_node": content.get("at_node"), "trigger": trigger})
        return fold(self.store.read_all())

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
        if self._failure_reflection:
            fails = sorted((n for n in state.nodes.values()
                            if n.status is NodeStatus.failed and n.error_reason),
                           key=lambda n: n.id, reverse=True)[:3]
            if fails:
                summ = "; ".join(f"node {n.id} ({n.error_reason}): {(n.error or '')[:60]}" for n in fails)
                hint += f"\nReflection — recent failures to avoid repeating: {summ}."
        if self._localize_faults and self._repo_spec.get("editables"):
            fails = sorted((n for n in state.nodes.values()
                            if n.status is NodeStatus.failed and n.error),
                           key=lambda n: n.id, reverse=True)
            if fails:
                from .localize import localize
                roots = [e["path"] for e in self._repo_spec["editables"]]
                loc = localize(fails[0].error, roots,
                               idea_text=(parent.idea.rationale if parent is not None else ""))
                if loc:
                    files = ", ".join(item["file"] for item in loc[:3])
                    hint += f"\nFault localization — likely files to edit: {files}."
        if self._feature_engineering and (self.task_has_columns or self._assets):
            hint += ("\nFeature engineering: propose 1-2 semantically-meaningful engineered features "
                     "(ratios, interactions, aggregations, domain transforms) as code. The eval's "
                     "cross-validation gates them — KEEP a feature only if it improves CV; drop any "
                     "that don't (feature engineering is non-universal).")
        hint += self._prior_note_text   # E4: cross-run meta-learned prior (empty unless enabled)
        try:
            setattr(self.researcher, "_complexity_hint", hint)
        except Exception:  # noqa: BLE001
            pass
        # A7 `prefer_sweep`: nudge — never force — the Researcher toward an intra-node sweep when the
        # Strategist's cost model favors in-process execution. Cleared when the flag is off, so a one-
        # time bias doesn't persist after the Strategist moves on.
        sweep_hint = ("\nStrategy bias: evals here are costly and the space is numeric — STRONGLY "
                      "consider a SWEEP (set `space` to a small grid) so many configs share one "
                      "data load." if self._prefer_sweep else "")
        try:
            setattr(self.researcher, "_sweep_hint", sweep_hint)
        except Exception:  # noqa: BLE001
            pass

    def _load_reflection_priors(self) -> str:
        """E4: load prior-run meta-review notes for THIS task from the cross-run memory and format
        them as a proposal-prompt prior (gradient-free meta-learning). Empty unless enabled + present."""
        if not (self._reflection_priors and self.memory_dir):
            return ""
        path = Path(self.memory_dir) / "meta_notes.jsonl"
        if not path.exists():
            return ""
        notes: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                o = orjson.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if o.get("task_id") == self.task.id and o.get("note"):
                notes.append(str(o["note"]))
        if not notes:
            return ""
        return "\nPrior-run insights for this task (meta-learned): " + " | ".join(notes[-3:])

    def _write_reflection_note(self, final: RunState) -> None:
        """E4: distill a one-line meta-review of this run (what won) into the cross-run memory, so the
        NEXT run of this task warm-starts from it. Appends to <memory_dir>/meta_notes.jsonl."""
        if not (self._reflection_priors and self.memory_dir):
            return
        best = final.best()
        if best is None:
            return
        note = (f"best metric {best.metric:.4g} via op '{best.operator}' params {best.idea.params}; "
                f"{len(final.nodes)} nodes, {len(final.evaluated_nodes())} evaluated")
        p = Path(self.memory_dir) / "meta_notes.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(orjson.dumps({"task_id": final.task_id, "note": note}).decode() + "\n")

    def _apply_novelty_gate(self, state: RunState, idea: Idea) -> Idea:
        """E1: if a fresh proposal's numeric params are within `novelty_epsilon` (normalized L2) of an
        existing node, nudge it off the duplicate deterministically and record a `novelty_rejected`
        audit event. Loop-safe (always returns a usable idea) and replay-safe (the nudged params land
        in node_created; the gate is not re-run on replay). No-op unless `novelty_gate` is on."""
        if not self._novelty_gate:
            return idea
        import random as _random

        from .digest import param_distance
        params = {k: float(v) for k, v in idea.params.items() if isinstance(v, (int, float))}
        if not params:
            return idea

        nearest, mind = None, float("inf")
        for n in state.nodes.values():
            d = param_distance(params, n.idea.params)
            if d < mind:
                mind, nearest = d, n.id
        if mind >= self._novelty_epsilon:
            return idea
        nid = len(state.nodes)
        rng = _random.Random(nid * 1009 + 7)        # deterministic per node-slot
        nudged = dict(idea.params)
        for k in params:
            scale = max(abs(params[k]), 1.0) * 0.1
            nudged[k] = round(params[k] + rng.uniform(-1.0, 1.0) * scale, 4)
        self.store.append("novelty_rejected", {
            "node_id": nid, "near_node": nearest, "distance": round(mind, 4),
            "original": idea.params, "nudged": nudged})
        out = idea.model_copy()
        out.params = nudged
        out.rationale = (idea.rationale + " [novelty-gate: nudged off a near-duplicate]").strip()
        return out

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

    def _agent_next_actions(self, state: RunState) -> list[dict]:
        """Self-driving action selection (Step 5). The unified agent picks the next macro action
        from the pure legal-action gate; forced phases (evaluate-pending / budget / seed) give it
        no discretion. Records an audit-only `agent_decision` (never read by best-selection); the
        chosen action then flows through the SAME bucket logic as the policy path. Falls back to the
        policy's own recommendation on any malformed/abstaining choice — the agent can never escape
        `legal`, so 'follow the right pipeline' is a structural invariant, not prompt obedience."""
        from .policy import legal_actions
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
        from .roles import _state_brief
        try:
            brief = _state_brief(state, None)
        except Exception:  # noqa: BLE001 - a brief is advisory; never block on it
            brief = ""
        choice = chooser(state, legal, recommended, brief=brief)
        idx = choice.get("index", -1) if isinstance(choice, dict) else -1
        chosen = legal[idx] if isinstance(idx, int) and 0 <= idx < len(legal) else \
            (recommended if recommended is not None else legal[0])

        def _summ(a: Optional[dict]) -> Optional[dict]:
            if not a:
                return None
            return {"kind": a.get("kind"), "parent_id": a.get("parent_id"),
                    "parent_ids": a.get("parent_ids"), "node_id": a.get("node_id")}

        self.store.append("agent_decision", {
            "at_node": len(state.nodes),
            "chosen": _summ(chosen),
            "legal": [_summ(a) for a in legal],
            "recommended": _summ(recommended),
            "rationale": (choice.get("rationale", "") if isinstance(choice, dict) else "")[:500],
        })
        return [chosen]

    def _triage_crash(self, state: RunState, node, error: str, attempt: int,
                      reason: str = "crash") -> dict:
        """Decide what to do with a just-failed node BEFORE spending another eval:
        {"action": "repair"|"abandon"|"reject_idea", "rationale": str}. Base mode: the unified
        agent decides (it can consult the run via its pilot tools — read_code / find_analogous —
        to judge whether nearby configs also fail, i.e. whether the IDEA is wrong vs the code).
        Falls back to a deterministic rule when no LLM triage agent is wired (unified_agent off),
        which never rejects an idea — so the feature is safe without an agent.

        `reason` (crash|timeout) is surfaced to both paths so a timeout is triaged as "too slow ->
        reduce compute" rather than mis-read as a wrong idea (a missing KNOWN lib never reaches here
        — env-prep installs it and re-runs first)."""
        # Tag the failure kind so the LLM agent (and the rule's marker scan) see crash vs timeout.
        tagged = f"[failure kind: {reason}]\n{error}"
        fn = getattr(self.researcher, "triage_crash", None)
        if callable(fn):
            try:
                from .roles import _state_brief
                try:
                    brief = _state_brief(state, None)
                except Exception:  # noqa: BLE001 - a brief is advisory; never block on it
                    brief = ""
                out = fn(node, tagged, attempt, state=state, brief=brief)
                if isinstance(out, dict) and out.get("action") in ("repair", "abandon", "reject_idea"):
                    return {"action": out["action"], "rationale": str(out.get("rationale", ""))[:300]}
            except Exception:  # noqa: BLE001 - agent triage is best-effort; fall through to the rule
                pass
        # 0 = unlimited attempts -> pass a large cap so the rule path keeps repairing mechanical
        # crashes (the anti-stuck guard, not a count, stops a genuinely stuck node).
        cap = self._inline_repair_attempts or 10**9
        return _rule_triage(reason, error, attempt, cap)

    def _repair_error_context(self, reason: str, error: str) -> str:
        """Error context handed to Developer.repair(). A timeout gets an explicit cost-reduction
        directive (the code was too slow, not wrong — shrink it to fit the budget). With deep_repair
        (C3) a crash is enriched with the failure taxonomy + a 'reproduce then fix' directive; else
        the raw tail. Shared by the inter-node debug operator and the inline (in-node) repair loop."""
        if reason == "timeout":
            # Don't quote a specific budget here: the wall-clock varies by node kind (a sweep node gets
            # timeout×sweep_timeout_mult; a RepoTask uses its own per-profile timeout), so a hardcoded
            # self.timeout would be misleading. The directive — cut compute — is what matters.
            return ("[failure kind: timeout]\n" + error + "\n"
                    "The script exceeded its evaluation time budget and was killed before it produced a "
                    "metric. The IDEA is fine — it was just too slow. Return a corrected, complete script "
                    "that finishes WELL within the budget by reducing compute: fewer estimators/boosting "
                    "rounds, fewer epochs, fewer CV folds or seeds, early stopping, a smaller/lighter "
                    "model, capped n_jobs, or a subsample — keep the approach, cut the cost.")
        if reason == "oom":
            # The OOM-kill usually leaves NO Python traceback (the kernel SIGKILLs the process — that's
            # how _failure_reason recognised it), so a "diagnose the root cause" directive has nothing
            # to read. Give the actionable memory-reduction directive instead, mirroring the timeout one.
            return ("[failure kind: oom]\n" + error + "\n"
                    "The script was KILLED by the out-of-memory killer — it exceeded the available "
                    "RAM/VRAM (e.g. a JupyterHub pod's cgroup memory limit) before producing a metric, "
                    "typically with no Python traceback. The IDEA is fine — it was just too "
                    "memory-hungry. Return a corrected, complete script that fits in LESS memory: a "
                    "smaller batch size, a lighter/smaller model, fewer features or a subsample of the "
                    "rows, gradient accumulation instead of one large batch, lower precision "
                    "(float16/bfloat16), or freeing large intermediates — keep the approach, cut the "
                    "memory.")
        if self._deep_repair:
            return (f"[failure kind: {reason or 'unknown'}]\n{error}\n"
                    "Diagnose the root cause; if it's unclear, add a tiny reproduction/"
                    "assert near the failure, then return a corrected, complete script.")
        return error

    def _prepare_env(self, stderr: str) -> list[str]:
        """Environment self-prep: pip-install the KNOWN libraries a crash reports as missing, into
        the eval interpreter, so the engine can re-run instead of rejecting the idea. Returns the
        pip packages successfully installed (empty => nothing to do / install failed -> normal
        triage). Trusted_local only (gated by the caller via `self._auto_install_deps`).

        Per-package so a partial failure only stops the bad name; `_dep_attempted` + `_dep_lock`
        make it install-once-per-module and concurrency-safe (pip mutates one shared env)."""
        from . import deps
        # Parse the missing KNOWN libs BEFORE taking the lock — a crash with nothing to install (the
        # common case, and every non-dep crash) must not block on `_dep_lock` while another eval holds
        # it through a multi-minute pip install (max_parallel>1). Only contend for the lock when there
        # is real installable work.
        candidates = [m for m in deps.missing_modules(stderr) if deps.is_installable(m)]
        if not candidates:
            return []
        with self._dep_lock:
            mods = [m for m in candidates if m not in self._dep_attempted]  # re-check inside the lock
            if not mods:
                return []
            python = getattr(self.sandbox, "python", sys.executable)
            installer = self._dep_installer or deps.install
            installed: list[str] = []
            for mod in mods:
                self._dep_attempted.add(mod)    # one pip attempt per module per run (success or fail)
                pkg = deps.pip_package(mod)
                try:
                    with self.tracer.span("install_dep", package=pkg):
                        res = installer(pkg, python=python, timeout=self._dep_install_timeout)
                except Exception:  # noqa: BLE001 - a misbehaving installer must degrade to "not installed",
                    res = None     # not crash the eval; the node then flows to normal triage/repair.
                if getattr(res, "ok", False):
                    installed.append(pkg)
            return installed

    # ---------------------------------------------------------------- private
    def _create_node(self, action: dict) -> None:
        state = fold(self.store.read_all())
        node_id = max(state.nodes, default=-1) + 1  # monotonic across the whole run -> unique
        kind = action["kind"]
        with self.tracer.span("create_node", new_trace=True, node_id=node_id, operator=kind):
            if kind == "draft":
                self._set_complexity_hint(state, None)   # A0d breadth-keyed complexity cue
                with self.tracer.span("propose"):
                    idea = self.researcher.propose(state, None)
                idea = self._apply_novelty_gate(state, idea)   # E1 dedup near-duplicate proposals
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
                    # C3 deep test-driven repair (when enabled): failure taxonomy + a structured
                    # "reproduce then fix" directive, not just the raw stderr tail. Depth is already
                    # bounded by debug_depth.
                    err = self._repair_error_context(parent.error_reason, parent.error)
                    with self.tracer.span("repair", parent_id=parent.id):
                        code = repair(parent.idea, parent.code, err)
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
                idea = self._apply_novelty_gate(state, idea)   # E1 dedup near-duplicate proposals
                idea.operator = "improve"
                parents = [parent.id]
                with self.tracer.span("implement"):
                    code = self.developer.implement(idea)
            # 💡 deep-research provenance: tag the first couple of nodes created right after a research
            # memo (its directions are the active steering) so the UI can show WHERE research landed in
            # the tree. Audit/UI only — never affects search. Coarse-but-honest (temporal proximity).
            research_origin = None
            if state.research:
                _m = state.research[-1]
                _ra = _m.get("at_node")
                if _ra is not None and _ra <= node_id < _ra + 2:
                    research_origin = {"at_node": _ra, "trigger": _m.get("trigger")}
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
                    "research_origin": research_origin,
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
        node_id = max(state.nodes, default=-1) + 1
        idea_d = dict(req.get("idea") or {})
        idea_d.setdefault("operator", "manual")
        # Coerce params to floats defensively (a manual form may send strings); drop unparseable.
        raw_params = idea_d.get("params") or {}
        if not isinstance(raw_params, dict):
            raw_params = {}   # a non-dict params (e.g. "lr=0.1") would AttributeError on .items()
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
                    # Honour explicit files/deleted on the request (a cross-run `import` ships the
                    # sibling's full multi-file solution); else use the Developer's last build, and
                    # only when the Developer actually implemented (no ready-made code was supplied).
                    "files": (req.get("files")
                              or ({} if req.get("code") else getattr(self.developer, "last_files", {}))) or {},
                    "deleted": req.get("deleted") or [],
                    "source": "manual",
                    # Cross-run provenance: present when this inject SEEDED from a sibling run's
                    # experiment (an `import` action). None for an ordinary operator-authored inject.
                    "origin": req.get("origin"),
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
        sp = (self.tracer.span("seed_workspace") if self.tracer is not None
              else __import__("contextlib").nullcontext(None))
        with sp as _h:
            seeded: list[str] = []
            for ed in self._repo_spec.get("editables", []):
                dst = wd if ed["name"] in (".", "") else wd / ed["name"]
                mode = (ed.get("seed_mode") or self._seed_mode or "auto")
                n = self._seed_repo_tree(ed["path"], dst, ignore, mode)
                seeded.append(f"{ed['name']}[{mode}]:{'copytree' if n < 0 else str(n)+' tracked'}")
            for ref in self._repo_spec.get("references", []):
                if ref.get("mount"):             # runtime dependency -> symlink read-only input
                    self._link_input(ref["path"], wd / ref["name"])
                    seeded.append(f"ref:{ref['name']}->link")
            for name, src in self._repo_spec.get("data", {}).items():
                self._link_input(src, wd / name)
                seeded.append(f"data:{name}->link")
            if _h is not None:
                _h.set_many(materialized=", ".join(seeded))
            # Observability: surface WHAT got materialized into this node's workdir (the "data setup"
            # step) in the activity feed — which editable trees were seeded (tracked vs full copy) and
            # which data/reference inputs were mounted. node_id parsed from the workdir name.
            try:
                nid = int(str(wd.name).split("_")[-1])
            except (ValueError, IndexError):
                nid = None
            self.store.append("workspace_seeded", {"node_id": nid, "materialized": seeded})

    def _seed_repo_tree(self, src, dst, ignore, mode: str = "auto") -> int:
        """Materialize an editable repo's *source* into the node workdir under a seeding `mode`:
        - "auto" (default) / "tracked": copy the git-TRACKED files (the real code surface — fast,
          deterministic) so a working tree bloated with untracked artifacts (model checkpoints,
          datasets — often many GB) is NOT deep-copied into every node. "auto" silently falls back
          to a full copy when `src` is not a git repo; "tracked" also falls back (there's nothing
          else to copy) but is the explicit "code only" intent.
        - "all": force a full recursive copytree (legacy behavior) — use for small repos or when
          untracked files are needed at eval time.
        Returns the number of tracked files copied, or -1 when a full copytree was used."""
        import shutil
        import subprocess
        from pathlib import Path as _P
        src = _P(src); dst = _P(dst)
        tracked = None
        if mode != "all":
            # Ask git directly (no `.git`-at-root check): the editable repo is often a SUBDIR of a
            # larger git repo whose `.git` lives in a parent, so `(src/'.git').exists()` is False even
            # though `git -C src ls-files` correctly lists the files tracked under src. Use it whenever
            # git returns a non-empty tracked set; otherwise (non-git / nothing tracked) fall back.
            try:
                out = subprocess.run(["git", "-C", str(src), "ls-files", "-z"],
                                     capture_output=True, text=True, timeout=120)
                if out.returncode == 0:
                    files = [p for p in out.stdout.split("\0") if p]
                    if files:
                        tracked = files
            except Exception:
                tracked = None                   # git missing / not a repo -> copytree fallback
        if tracked is None:
            shutil.copytree(src, dst, dirs_exist_ok=True, ignore=ignore)
            return -1
        dst.mkdir(parents=True, exist_ok=True)
        n = 0
        for rel in tracked:
            s = src / rel
            if s.is_dir() or not s.exists():     # submodule dir / deleted-but-tracked path
                continue
            d = dst / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
            n += 1
        return n

    def _link_input(self, src, dst) -> None:
        """Mount a large, read-only task input (dataset / reference repo) into the node workdir as a
        SYMLINK rather than a deep copy: these are immutable inputs the eval reads, not the agent's
        edit target, so per-node copies just burn wall-clock + disk (acute on an S3-backed FUSE
        mount). Idempotent (resume / re-seed); falls back to a copy if the symlink can't be made."""
        import os as _os
        import shutil
        from pathlib import Path as _P
        src = _P(src); dst = _P(dst)
        if dst.is_symlink() or dst.exists():
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            _os.symlink(src, dst, target_is_directory=src.is_dir())
            return
        except OSError:
            pass
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        elif src.is_file():
            shutil.copy2(src, dst)

    def _agent_may(self, role: str, setting: str) -> bool:
        """Governance gate (Settings.agent_control): may `role` (strategist|boss|researcher) change
        `setting` at runtime? A setting absent from the map is LOCKED for everyone. Pure + cheap —
        called at each agent seam so the matrix is the single source of truth."""
        return role in (self._agent_control.get(setting) or ())

    def _ensure_run_setup(self) -> None:
        """Run the eval's RUN-LEVEL `run_setup` exactly ONCE, before the first eval — e.g. a one-time
        dependency install into the shared interpreter (the autonomy default when deps are stable
        across experiments). Distinct from per-node `setup`, which reinstalls before EVERY eval. Runs
        in the first editable repo's SOURCE dir so `-r requirements.txt` resolves; output streams to
        `run_setup.log`. A non-zero/timed-out run_setup ABORTS the run (the env would be unusable).
        Only in trusted_local (an untrusted/docker eval is a fresh container — use per-node `setup`).
        No-op when `run_setup` is unset. The guard is set BEFORE running so a crash can't retry-loop."""
        if self._run_setup_done:
            return
        # Serialize the check-then-set: parallel eval worker threads would otherwise all see
        # _run_setup_done == False and launch pip (not concurrency-safe) N times into one interpreter.
        with self._run_setup_lock:
            if self._run_setup_done:
                return
            cmd = list((self._eval_spec or {}).get("run_setup") or [])
            if not cmd or self.trust_mode != "trusted_local":
                self._run_setup_done = True
                return
            self._run_setup_done = True
            self._do_run_setup(cmd)

    def _do_run_setup(self, cmd: list) -> None:
        from .sandbox import _run_argv
        eds = (self._repo_spec or {}).get("editables", [])
        cwd = eds[0]["path"] if eds else str(self.run_dir)
        to = float((self._eval_spec or {}).get("run_setup_timeout", 1800.0))
        self.store.append("run_setup_started", {"command": cmd, "cwd": cwd})
        log = str(Path(self.run_dir) / "run_setup.log")
        rc, out, err, timed = _run_argv(cmd, cwd, to, log_path=log)
        self.store.append("run_setup_finished",
                          {"exit_code": rc, "timed_out": timed, "stderr_tail": (err or "")[-2000:]})
        if rc != 0 or timed:
            raise RuntimeError(f"run_setup failed (exit={rc}, timed_out={timed}); see {log}\n"
                               + (err or out or "")[-500:])

    def _sandbox_cwd(self, workdir, cwd_spec) -> str:
        """Resolve the eval `cwd` against the node's sandbox workdir. A relative cwd joins the
        workdir (the conventional case). An ABSOLUTE cwd that points inside an editable repo's
        *source* is remapped onto the node workdir, so the eval runs in the sandboxed copy (with
        the agent's edits + the seeded tree) instead of the shared original repo — `Path(wd)/'/abs'`
        would otherwise collapse to '/abs', silently bypassing the sandbox. An absolute cwd that is
        not under any editable source is trusted as given (e.g. an external tool dir)."""
        from pathlib import Path as _P
        wd = _P(workdir).resolve()
        p = _P(cwd_spec)
        if not p.is_absolute():
            return str((wd / cwd_spec).resolve())
        ap = p.resolve()
        for ed in (self._repo_spec or {}).get("editables", []):
            src = _P(ed["path"]).resolve()
            base = wd if ed["name"] in (".", "") else wd / ed["name"]
            try:
                rel = ap.relative_to(src)
            except ValueError:
                continue
            return str((base / rel).resolve())
        return str(ap)

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
            self._ensure_run_setup()             # one-time run-level dep install (before the first eval)
            prof = profile or (node.idea.eval_profile if node is not None else None)
            # A7 Strategist fidelity override: when the active strategy pins smoke/full and the node
            # didn't request a profile, use the strategy's. An explicit `profile` arg (confirm=full)
            # always wins. "adaptive" leaves _strategy_fidelity None => the Idea's own profile.
            if prof is None and self._strategy_fidelity in ("smoke", "full"):
                prof = self._strategy_fidelity
            params = node.idea.params if node is not None else {}
            cmd, timeout = command_eval.build_command(es, params, prof)
            root = str(Path(workdir).resolve())               # repo/workdir root
            cwd = self._sandbox_cwd(workdir, es.get("cwd", "."))
            # untrusted tier (Phase 4): sandbox the eval in docker, mounting the workspace
            # root so the cwd subdir + host metric reading line up. Fails loudly w/o docker.
            wrap = (command_eval.make_docker_wrap(
                        root, self.docker_image,
                        runtime=("runsc" if self.trust_mode == "hostile" else None))
                    if self.trust_mode in ("untrusted", "hostile") else None)
            res = command_eval.run_command_eval(
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
                cancel=cancel,                                # operator mid-eval node_abort
                log_dir=root)                                 # live setup.log/eval.log in the node workdir
        else:
            # Intra-node sweep nodes run a whole grid in one process, so they need ~N× the
            # single-eval budget. `sweep_timeout_mult` scales the wall-clock for sweep nodes only;
            # _kill_tree + the mid-eval cancel watcher still bound a runaway. (The RepoTask path
            # gets its per-profile timeout from build_command above.)
            timeout = self.timeout
            if node is not None and node.idea.is_sweep:
                timeout = self.timeout * self.sweep_timeout_mult
            # Researcher-sized per-node budget (e.g. a neural-net / large-ensemble idea that needs longer
            # than the run default) — honored ONLY when the governance matrix grants the researcher the
            # `timeout` setting; otherwise the run-wide budget stands. This is the "auto" per-node mode.
            etv = getattr(node.idea, "eval_timeout", None) if node is not None else None
            if etv and etv > 0 and self._agent_may("researcher", "timeout"):
                timeout = float(etv)
            res = self.sandbox.run(node.code, str(workdir), timeout, env, cancel=cancel)
        # Intra-node sweep: if the solution reported a grid of trials, collapse them into the node's
        # scalar `metric` (the best feasible trial under the task direction) so fold/best-selection/
        # improve are untouched. Done BEFORE host grading so a host grader still has the final say on
        # the best trial's predictions file. The full trial list rides along on `res.trials`.
        if res.trials:
            self._apply_sweep_best(res)
        # Out-of-process host-side grading (general): override the (ignored) self-reported metric with
        # the HOST's score of the candidate's predictions. Applied for BOTH the command-eval and the
        # sandbox path, so a task that exposes host_grader() is always host-scored — and so EVERY
        # sandbox-path eval (normal AND the multi-seed confirm pass, both call _run_eval) is graded
        # the same way. host_grader takes precedence: its score replaces any self-reported metric.
        if self._host_grader is not None:
            res = self._apply_host_grade(res, workdir)
        return res

    def _apply_sweep_best(self, res):
        """Collapse an intra-node sweep's `res.trials` into the node's scalar `metric`: pick the
        best trial that produced a usable (finite) metric, under the task direction. Keeping
        `metric` a single number means fold, best-selection, confirm and `improve` treat a sweep
        node like any other; the trials are audit/UI only. No usable trial -> no metric (the node
        fails like an empty run, so a sweep where every config crashed can't pass)."""
        from .sandbox import _to_float
        scored = [(t, _to_float(t.get("metric"))) for t in (res.trials or [])]
        scored = [(t, m) for t, m in scored if m is not None]
        if not scored:
            res.metric = None
            return
        chooser = min if self.task.direction == "min" else max
        best_t, best_m = chooser(scored, key=lambda tm: tm[1])
        res.metric = best_m
        extra = best_t.get("extra_metrics") or {}
        if extra:
            res.extra_metrics = {**(res.extra_metrics or {}), **extra}

    def _graded_output_name(self) -> Optional[str]:
        """The filename the candidate must write for out-of-process grading (the file
        `_apply_host_grade` scores), or None when grading is in-workdir. Single source of truth
        for the host-grader output name so the host-grading audit event and the critic's
        submission-output check resolve it identically and can't drift."""
        hg = self._host_grader
        if not hg:
            return None
        # Mirror `_apply_host_grade` EXACTLY so the name can't drift: real MLE-bench scores the
        # `submission` file; every other host grader scores the `predictions` file.
        if hg.get("kind") == "mlebench":
            return hg.get("submission", "submission.csv")
        return hg.get("predictions", "predictions.json")

    def _apply_host_grade(self, res, workdir):
        """B1+ out-of-process grading: read the candidate's predictions file from its workdir and score
        it on the HOST against the held-out labels (held in engine memory, never on the candidate FS).
        Overrides `res.metric`; missing/malformed predictions -> no metric (the node fails, so a
        candidate that doesn't actually produce predictions can't pass)."""
        import json as _json
        from .command_eval import host_score
        g = self._host_grader
        # Real MLE-bench: the candidate writes submission.csv; mle-bench's REAL grader scores it
        # out-of-process against private/test.csv answers (in the mle-bench data dir, never copied
        # into the candidate workdir). The official score replaces any self-report; the medal/
        # above-median report rides along in extra_metrics for the trust panel + final report.
        if g.get("kind") == "mlebench":
            from .mlebench_grade import grade_in_subprocess
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
            from .sandbox import _to_float
            try:
                preds = _json.loads(preds_path.read_text(encoding="utf-8-sig", errors="replace"))
                # .get (not g["labels"]): a host_grader() dict missing labels yields metric None
                # (node fails) rather than an uncaught KeyError that would crash the eval worker.
                # _to_float: a non-finite (NaN/Inf) host score reads as None so an untrusted candidate
                # can't self-elect champion via a crafted prediction (mirrors command_eval/sweep paths).
                m = _to_float(host_score(g.get("scorer", "rmse"), preds, g.get("labels"), key=g.get("key")))
            except (ValueError, OSError):
                m = None
        res.metric = m
        return res

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
            # Hybrid crash repair: each attempt runs the eval (with the mid-eval abort watcher) and,
            # if it CRASHES, the agent triages it and may repair the code IN PLACE and re-run — all
            # within this one node (no new tree node, no max_nodes spent). At most
            # `inline_repair_attempts` repairs; then the node fails normally and stays eligible for the
            # budgeted inter-node debug operator. Exactly ONE terminal event (node_evaluated/node_failed)
            # is emitted at the end so first_terminal budget accounting and resume re-entry are intact;
            # only NON-terminal `node_repaired` events are written mid-loop.
            import threading
            attempt = 0
            dep_rounds = 0                   # env-prep auto-install + re-run rounds (separate from repair attempts)
            total_eval = 0.0                 # summed subprocess wall-clock across all attempts (cost)
            triage_outcome = None            # ("abandon"|"reject_idea", rationale) for the terminal event
            err = ""
            reason = "crash"
            stuck_sig = None; stuck_n = 0    # anti-stuck: consecutive identical-error signatures
            while True:
                _t0 = time.time()
                # Mid-eval per-node intervention (v2): a watcher polls the log while the eval runs in a
                # worker thread; if the operator appends `node_abort` for THIS node, it sets the cancel
                # Event, which tree-kills the in-flight subprocess (sandbox._run_argv). v1's pre-eval
                # skip only catches not-yet-started nodes — this kills a running one.
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
                total_eval = round(total_eval + (time.time() - _t0), 3)   # cumulative eval cost (#2)
                ok = res.metric is not None and res.exit_code == 0 and not res.timed_out
                if aborted and not ok:                       # killed mid-eval by the operator (and the
                    async with self._write_lock:             # eval didn't already finish cleanly first)
                        self.store.append("node_failed", {
                            "node_id": node_id, "error": "aborted by operator (killed mid-eval)",
                            "reason": "aborted", "eval_seconds": total_eval})
                        self._maybe_crash()
                    return
                if ok:
                    break
                reason = _failure_reason(res)
                err = self._redact(res.stderr[-500:]) or (
                    f"metric drift: {res.drift}" if res.drift is not None else
                    f"exit={res.exit_code} timed_out={res.timed_out} no_metric"
                )
                # Environment self-prep (deps.py): a crash that is purely a missing KNOWN library is
                # not a bad idea — install it (trusted_local only) and re-run BEFORE the crash-triage
                # agent can reject the idea. This is what lets torch/XGBoost/CatBoost (e.g. a GRU
                # model) run on a fresh box instead of dying as `idea_rejected`. Bounded by
                # _MAX_DEP_ROUNDS + the `_dep_failed` cache; does NOT consume a repair attempt (env
                # prep is not a code fix), and the unchanged node is simply re-evaluated.
                if (self._auto_install_deps and reason == "crash" and dep_rounds < _MAX_DEP_ROUNDS):
                    installed = await anyio.to_thread.run_sync(self._prepare_env, res.stderr)
                    if installed:
                        dep_rounds += 1
                        async with self._write_lock:
                            self.store.append("deps_installed", {
                                "node_id": node_id, "packages": installed, "round": dep_rounds})
                        continue   # re-run now that the library is present (no repair attempt spent)
                # Anti-stuck: when the SAME error recurs with no progress, stop (even under unlimited
                # repair) so the agent doesn't loop forever on an unfixable failure.
                _sig = " ".join((err or "").strip().split())[-160:]
                stuck_n = (stuck_n + 1) if _sig and _sig == stuck_sig else 1
                stuck_sig = _sig
                # Inline-repair gate: feature on, repairable reason, a Developer that can repair, and
                # something to repair (whole-file code, multi-file edits, or a repo). The attempt CAP is
                # skipped when unlimited (_inline_repair_attempts == 0); the anti-stuck guard bounds it.
                if (not self._inline_repair
                        or reason not in self._inline_repair_reasons
                        or (self._inline_repair_attempts and attempt >= self._inline_repair_attempts)
                        or stuck_n >= self._inline_repair_stuck_repeat
                        or not callable(getattr(self.developer, "repair", None))
                        or not (node.code or node.files or self._repo_spec)):
                    if stuck_n >= self._inline_repair_stuck_repeat and self._inline_repair:
                        triage_outcome = ("abandon", f"same error repeated {stuck_n}x — stuck, abandoning")
                    break
                triage = self._triage_crash(state, node, err, attempt + 1, reason=reason)
                action = triage.get("action", "repair")
                if action == "abandon":
                    triage_outcome = ("abandon", triage.get("rationale", ""))
                    break
                if action == "reject_idea":   # the idea itself is wrong -> mark the lineage; steer to a new idea
                    reason = "idea_rejected"
                    triage_outcome = ("reject_idea", triage.get("rationale", ""))
                    break
                # action == "repair": fix the code in place and re-eval (no new node, no budget spent).
                with self.tracer.span("inline_repair", node_id=node_id, attempt=attempt + 1):
                    new_code = self.developer.repair(
                        node.idea, node.code, self._repair_error_context(reason, err))
                attempt += 1
                async with self._write_lock:
                    self.store.append("node_repaired", {
                        "node_id": node_id, "attempt": attempt, "code": new_code,
                        "files": getattr(self.developer, "last_files", {}) or {},
                        "deleted": getattr(self.developer, "last_deleted", []) or [],
                        "error_in": err, "triage_action": "repair",
                        "rationale": str(triage.get("rationale", ""))[:300]})
                node = fold(self.store.read_all()).nodes[node_id]   # node.code now == repaired code
                self._write_node_files(node, workdir)               # re-materialize before re-eval
                # loop -> re-run the eval with the corrected code
            sp.set_many(eval_seconds=total_eval, exit_code=res.exit_code, timed_out=res.timed_out,
                        metric=res.metric, ok=ok, repair_attempts=attempt)
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
                         "stdout_tail": self._redact(res.stdout[-500:]), "eval_seconds": total_eval,
                         "extra_metrics": res.extra_metrics or {},   # #5 multi-objective
                         "violations": res.violations or [],
                         # Intra-node sweep: the whole grid's per-trial results, carried on the ONE
                         # node_evaluated event (the sweep is a single atomic eval — eval_seconds is
                         # the whole-sweep wall-clock; per-trial seconds are audit-only). [] normally.
                         "trials": res.trials or []},
                    )
                    # B5 reward-hacking detector + I3 code-leakage scan (audit-only): flag a
                    # suspicious win / leaky pipeline without ever changing selection. Both surface in
                    # the Trust panel via the same reward_hack_suspected event.
                    sigs = []
                    # Scan the WHOLE solution surface, not just solution.py — a patch-gated multi-file
                    # agent can hide answer-key access / leakage / the real computation in an in-surface
                    # helper module that solution.py imports. Concatenate node.files so the reward-hack /
                    # leakage / critic scans cover the imported code too (not only the clean entrypoint).
                    scan_src = node.code + "".join(
                        f"\n\n# --- {fn} ---\n{src}" for fn, src in (node.files or {}).items()
                        if str(fn).replace("\\", "/").lower() != "solution.py")
                    if self.reward_hack_detect:
                        from .reward_hack import detect_reward_hacks
                        protected = set(self._repo_spec.get("protected_names", [])) | set(self._assets)
                        sigs += detect_reward_hacks(scan_src, res.metric, state.direction,
                                                    protected_names=protected, stdout=res.stdout)
                    if self._code_leakage_detect and scan_src:
                        from .leakage import code_leakage_scan
                        for f in code_leakage_scan(scan_src)["flags"]:
                            sigs.append({"signal": "data_leakage:" + f["signal"],
                                         "detail": f"line {f['line']}: {f['code']}"})
                    if self._critic_check and scan_src:
                        from .critic import critique
                        # Host-graded tasks (MLE-bench &c.) score a submission file out-of-process,
                        # so the critic's in-code `metric` checks don't apply — hand it the expected
                        # submission filename so it checks the right output contract instead.
                        sub_file = self._graded_output_name()
                        for c in critique(node.idea, scan_src, submission_file=sub_file):
                            sigs.append({"signal": "critic:" + c["issue"], "detail": c["detail"]})
                    if sigs:
                        self.store.append("reward_hack_suspected",
                                          {"node_id": node_id, "signals": sigs})
                else:
                    # `err`/`reason` were computed in the attempt loop (reason may be "idea_rejected"
                    # if the crash-triage agent judged the idea fundamentally wrong).
                    sp.set("error_reason", reason)
                    data = {"node_id": node_id, "error": err, "reason": reason,
                            "eval_seconds": total_eval}
                    if triage_outcome is not None:
                        data["triage_action"], data["triage_rationale"] = (
                            triage_outcome[0], str(triage_outcome[1])[:300])
                    self.store.append("node_failed", data)
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
        node_id = max(fold(self.store.read_all()).nodes, default=-1) + 1
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
        node_id = max(fold(self.store.read_all()).nodes, default=-1) + 1
        self.store.append("node_created", {
            "node_id": node_id, "parent_ids": [parent_id], "operator": "refine_block",
            "idea": idea.model_dump(mode="json"), "code": new_code,
            "files": getattr(self.developer, "last_files", {}) or {}})
        self._emit_agent_report(node_id)

    def _redact(self, text: str) -> str:
        """B3: mask secrets in an output tail before it is persisted, when redaction is enabled."""
        if not self._redact_output or not text:
            return text
        from .redact import redact_secrets
        return redact_secrets(text)

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
