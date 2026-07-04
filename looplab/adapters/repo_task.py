"""RepoTask (kind="repo", ADR-7): the R&D agent works inside an EXISTING repo — it edits
experiment code within an allow-listed surface, and success is measured by running the
OPERATOR'S OWN eval command and reading the metric it emits. The agent never authors the
metric (trust boundary): the eval command + its output files are task-owned and protected
from edits (same mechanism that guards the mlebench grader).

Phase 1: ONE editable repo + read-only references mounted for runtime + an explicit
operator-written `eval_spec`. The agent backend (opencode) edits the repo worktree;
offline / on agent failure a NoOp developer leaves the repo at baseline.

See the architecture study (plans/) for the full workspace/eval model and phases.
"""
from __future__ import annotations

import os
import random
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from looplab.core.models import Idea, Node, RunState
from looplab.core.parse import LLMClient
from looplab.agents.roles import LLMResearcher


class ReferenceSpec(BaseModel):
    name: str
    path: str
    mount: bool = False        # copy into the eval workdir (runtime dep) vs context-only


class EditableSpec(BaseModel):
    """One editable repo in a multi-repo workspace (Phase 4). Mounted at `<name>/` under the
    eval workdir; the agent may edit files matching `surface` (globs, relative to the repo)
    and must not overwrite `protect`. The single-repo shorthand `RepoTask.editable_path`
    desugars to one of these mounted at the workspace root (name=".")."""
    name: str                  # subdir under the workspace, e.g. "model" / "data_pipeline"
    path: str                  # the repo to mount + let the agent edit
    surface: list[str] = Field(default_factory=lambda: ["**/*.py"])
    protect: list[str] = Field(default_factory=list)
    # How this repo is materialized into each node's eval workdir (autonomy: Genesis picks from the
    # user's words; "" = fall back to the run-wide Settings.seed_mode). "auto" (the smart default):
    # git-tracked files only when it's a git repo (so a tree bloated with untracked artifacts — model
    # checkpoints, data — is NOT deep-copied per node), else a full copy. "tracked": force git-tracked
    # only. "all": force a full recursive copy (the legacy behavior; use for small repos or when
    # untracked files are needed at eval time).
    seed_mode: str = ""


class EvalSpec(BaseModel):
    """The operator's trusted evaluation (the agent does not author this)."""
    command: list[str]                       # argv, no shell; carries env activation
    cwd: str = "."                           # relative to the node eval workdir
    metric: dict = Field(default_factory=lambda: {"kind": "stdout_json", "key": "metric"})
    params_style: str = "none"               # none | cli_overrides
    timeout: float = 600.0
    # Optional setup command run in the workdir BEFORE the eval each time (e.g.
    # ["pip", "install", "-r", "requirements.txt"]). This is how an e2e Developer's
    # dependency changes take effect reproducibly: the agent edits requirements (in the
    # edit-surface), setup installs them, then the eval runs. Setup failure -> node_failed
    # (stderr fed back to the Developer's repair, so it can fix missing deps).
    setup: list[str] = Field(default_factory=list)
    setup_timeout: float = 600.0
    # Run-level setup: runs ONCE at run start (before the first node), in the editable repo root,
    # NOT per node. Use for a one-time, stable dependency install into the shared interpreter (the
    # autonomy default when deps don't change between experiments) — vs the per-node `setup` above,
    # which reinstalls before EVERY eval (use when the agent edits requirements and each node needs
    # its own deps). Genesis picks which to author from the task's words; both may be set. A failed
    # run_setup aborts the run (env is unusable). Empty -> no run-level setup.
    run_setup: list[str] = Field(default_factory=list)
    run_setup_timeout: float = 1800.0
    # Eval profiles (Phase 2): named override+timeout sets the Researcher selects per node,
    # e.g. {"smoke": {"overrides": ["max_steps=20"], "timeout": 60},
    #       "full":  {"overrides": ["max_steps=2000"], "timeout": 1800}}.
    # Search uses a cheap profile; the confirm phase forces "full".
    profiles: dict[str, dict] = Field(default_factory=dict)
    # Drift cross-check (Phase 4, eval_trust_mode="ratify_freeze_drift"): an INDEPENDENT
    # built-in reader (stdout_json/regex | file_json/regex — never `adapter`) that re-reads
    # the same metric from a source the agent can't forge (e.g. the framework's real stdout).
    # When it can't corroborate the frozen adapter within `drift_tolerance`, the metric is
    # discarded and a `spec_drift` event is recorded. None disables the check.
    cross_check: Optional[dict] = None
    drift_tolerance: float = 1e-6
    # Multi-objective (#5). `metrics`: extra named readers reported alongside the primary
    # (audit/observability), e.g. {"latency_ms": {"kind": "stdout_json", "key": "latency"}}.
    # `constraints`: reader specs with a `max`/`min` bound; a node that violates ANY (or whose
    # constraint value can't be read) is still measured but EXCLUDED from best-selection —
    # "optimize the metric subject to latency_ms <= 100". Operator-owned (trust boundary).
    metrics: dict[str, dict] = Field(default_factory=dict)
    constraints: list[dict] = Field(default_factory=list)

    @field_validator("cross_check")
    @classmethod
    def _cross_check_not_adapter(cls, v):
        from looplab.runtime.command_eval import validate_cross_check
        return validate_cross_check(v)


class RepoResearcher:
    """Minimal proposer for repo tasks: a draft, then improve-from-best. Params are free-
    form (the agent edits code from the rationale); numeric params are optional."""

    def __init__(self, seed: int = 0):
        self.rng = random.Random(seed)

    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        if parent is None:
            return Idea(operator="draft", params={},
                        rationale="Establish a working baseline change to the experiment.")
        return Idea(operator="improve", params=dict(parent.idea.params),
                    rationale=(f"Improve on node {parent.id} (metric={parent.metric}). "
                               "Make one focused change to raise the eval metric."))


class RepoParamResearcher:
    """Hyperparameter proposer for the cli_overrides framework mode (Phase 2): random
    within bounds, then Gaussian hill-climb around the best. Tags each Idea with the cheap
    `smoke` eval profile for search (the confirm phase upgrades the leaders to `full`)."""

    def __init__(self, bounds: dict, seed: int = 0, step: float = 0.3):
        self.bounds = bounds
        self.rng = random.Random(seed)
        self.step = step

    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        keys = list(self.bounds)
        if parent is None:
            params = {k: round(self.rng.uniform(*self.bounds[k]), 6) for k in keys}
            return Idea(operator="draft", params=params, rationale="random hyperparameters",
                        eval_profile="smoke")
        params = {}
        for k in keys:
            lo, hi = self.bounds[k]
            v = parent.idea.params.get(k, (lo + hi) / 2) + self.rng.gauss(0.0, (hi - lo) * self.step)
            params[k] = round(max(lo, min(hi, v)), 6)
        return Idea(operator="improve", params=params, eval_profile="smoke",
                    rationale=f"perturb node {parent.id} (params={parent.idea.params})")


class NoOpRepoDeveloper:
    """Baseline developer: makes no edits (empty file set). Used offline and as the agent's
    fallback, so a failed/absent agent leaves the repo unmodified and the eval measures the
    baseline rather than poisoning the search."""

    def __init__(self) -> None:
        self.last_files: dict[str, str] = {}

    def implement(self, idea: Idea) -> str:
        self.last_files = {}
        return ""

    def repair(self, idea: Idea, code: str, error: str) -> str:
        return ""


class RepoTask(BaseModel):
    kind: str = "repo"
    id: str = "repo_task"
    goal: str = ""
    direction: str = "max"                    # "min" | "max"
    seed: int = 0

    editable_path: str = ""                   # the repo the agent may modify (mounts at root)
    edit_surface: list[str] = Field(default_factory=lambda: ["**/*.py"])
    protect: list[str] = Field(default_factory=list)   # paths the agent must NOT overwrite
    seed_mode: str = ""                        # "" -> Settings.seed_mode | auto | tracked | all
    #  (how the root editable_path is materialized per node; see EditableSpec.seed_mode)
    # Multi-repo workspace (Phase 4): additional editable repos, each mounted at its `name`
    # subdir with its own surface/protect. Use this (optionally with editable_path for a
    # root repo) to let the agent edit across several repos in one experiment.
    editables: list[EditableSpec] = Field(default_factory=list)
    references: list[ReferenceSpec] = Field(default_factory=list)
    data: dict[str, str] = Field(default_factory=dict)  # name -> abs path, copied into eval wd
    eval: Optional[EvalSpec] = None            # operator-given eval; None when onboard=True
    # cli_overrides hyperparameter space (Phase 2): name -> (lo, hi). When set with
    # eval.params_style="cli_overrides", the Researcher tunes these and they become CLI
    # overrides on the eval command — driving an existing framework with NO code edits
    # (developer_backend stays "default" -> the NoOp baseline developer).
    params: dict[str, tuple[float, float]] = Field(default_factory=dict)
    # Onboarding (Phase 3): the agent proposes the trusted eval. The operator gives the
    # framework's command; the Developer writes a metric `adapter` for its tracker; a human
    # ratifies it. `eval` is then left None until ratified.
    onboard: bool = False
    onboard_command: list[str] = Field(default_factory=list)
    onboard_timeout: float = 600.0

    @field_validator("direction")
    @classmethod
    def _direction_valid(cls, v):
        if v not in ("min", "max"):     # silently treating typos as 'minimize' flips the objective
            raise ValueError(f"direction must be 'min' or 'max', got {v!r}")
        return v

    @model_validator(mode="after")
    def _expand_repo_paths(self):
        """Expand ~ and $ENV in every filesystem path the task points at, so a natural
        `editable_path: "~/data/vectorizer/dense-retrieval"` (or "$HOME/…") actually resolves —
        without this the `~` is treated as a literal directory and the repo mounts/scout tools come
        up EMPTY. Runs once; the resolved absolute path is what gets recorded + mounted. (A repo task
        is inherently tied to a local path, so resuming on a DIFFERENT machine/user whose home differs
        already needs editable_path re-pointed — the absolute snapshot just makes that explicit instead
        of silently re-resolving `~` to a different repo.)"""
        exp = lambda p: os.path.expanduser(os.path.expandvars(p)) if isinstance(p, str) and p else p
        self.editable_path = exp(self.editable_path)
        for e in self.editables:
            e.path = exp(e.path)
        for r in self.references:
            r.path = exp(r.path)
        self.data = {k: exp(v) for k, v in self.data.items()}
        return self

    @field_validator("editables")
    @classmethod
    def _names_distinct_and_safe(cls, v):
        seen = set()
        for e in v:
            if e.name in (".", "") or "/" in e.name or "\\" in e.name or ".." in e.name:
                raise ValueError(f"editable name must be a simple subdir, got {e.name!r}")
            if e.name in seen:
                raise ValueError(f"duplicate editable name: {e.name!r}")
            seen.add(e.name)
        return v

    @model_validator(mode="after")
    def _at_least_one_editable(self):
        if not self.editable_path and not self.editables:
            raise ValueError("RepoTask needs an editable source: set `editable_path` "
                             "and/or `editables`.")
        # reference/data mount names are used directly as `wd / name` in _seed_workspace, so
        # they must be safe simple subdir names and not collide with each other or an editable.
        used = {e.name for e in self.editables}
        for label, name in ([("reference", r.name) for r in self.references]
                            + [("data", k) for k in self.data]):
            if not name or "/" in name or "\\" in name or ".." in name.split("/"):
                raise ValueError(f"{label} mount name must be a simple subdir, got {name!r}")
            if name in used:
                raise ValueError(f"mount name collision: {name!r} used by more than one source")
            used.add(name)
        return self

    # ------- TaskAdapter hooks -------
    def assets(self) -> dict[str, str]:
        return {}                              # repo/data are tree-mounted, not flat assets

    def _editable_mounts(self) -> list[dict]:
        """Normalize the single-repo shorthand + the multi `editables` into one list of
        {name, path, surface, protect, seed_mode}. name="." mounts at the workspace root.
        seed_mode is per-repo; "" defers to the run-wide Settings.seed_mode at seed time."""
        out: list[dict] = []
        if self.editable_path:
            out.append({"name": ".", "path": self.editable_path,
                        "surface": list(self.edit_surface), "protect": list(self.protect),
                        "seed_mode": self.seed_mode})
        for e in self.editables:
            out.append({"name": e.name, "path": e.path,
                        "surface": list(e.surface), "protect": list(e.protect),
                        "seed_mode": (e.seed_mode or self.seed_mode)})
        return out

    def eval_spec(self) -> dict:
        return self.eval.model_dump() if self.eval else {}

    def make_onboarder(self, settings):
        """Build the onboarder (Phase 3) when `onboard` is set and a live LLM is available;
        otherwise None (offline runs inject one in tests, or use an explicit eval)."""
        if not self.onboard or settings.backend != "llm":
            return None
        from looplab.adapters.tasks import make_llm_client
        repo_path = self.editable_path or (self.editables[0].path if self.editables else "")
        return LLMOnboarder(make_llm_client(settings), repo_path, self.goal,
                            self.direction, self.onboard_command, self.onboard_timeout)

    @staticmethod
    def _normp(p: str) -> str:
        """Canonicalize a protected path to match git-diff paths (forward slashes, no leading
        './') so exact-string membership in `_write_node_files` actually fires for operator
        entries like './secret.py' or 'src\\secret.py'.

        Deliberately NOT `tools.patch.SurfacePolicy` (the write-gate value object): this is the
        read-side *builder* of the protected-name list, not a path gate — it normalizes operator
        input and never rejects, and its rules differ from the write gates' canonicalizers
        (RepoWriteTools._safe_rel also strips whitespace and rejects `~`/`..`; patch._escapes
        rejects rather than normalizes). The names built here are what SurfacePolicy consumers
        then match against."""
        p = str(p).replace("\\", "/")
        while p.startswith("./"):
            p = p[2:]
        return p

    def _eval_protected(self) -> list[str]:
        """Files the metric is READ from — the agent must not be able to forge the score by
        writing them. Covers a metric FILE (`file_json`/`file_regex`) AND an agent-authored
        `adapter` module, across EVERY reader the eval uses: the primary `metric`, the extra
        `metrics`, each `constraints` reader, and the drift `cross_check` (review C1 — secondary
        reader paths were left unprotected, so constraint/aux/drift values could be forged).
        Namespaced under the eval `cwd` so the protected name matches the workspace-relative path
        the agent edits."""
        if not self.eval:
            return []
        cwd = self._normp((self.eval.cwd or ".").strip()).strip("/")
        pre = "" if cwd in (".", "") else cwd + "/"
        out: list[str] = []

        def _add(reader) -> None:
            if not isinstance(reader, dict):
                return
            kind = reader.get("kind", "")
            if (kind.startswith("file_") or kind == "adapter") and reader.get("path"):
                out.append(self._normp(pre + reader["path"]))

        _add(self.eval.metric)
        for r in (self.eval.metrics or {}).values():
            _add(r)
        for c in (self.eval.constraints or []):
            _add(c)
        _add(self.eval.cross_check)
        seen: set[str] = set()
        return [p for p in out if not (p in seen or seen.add(p))]   # dedupe, keep order

    def _protected_names(self) -> list[str]:
        """Files the agent's edits may never overwrite, namespaced across all editable repos:
        each repo's `protect` (prefixed by its subdir) + the eval metric file. The onboarding
        adapter is added at ratification time."""
        names: list[str] = []
        for ed in self._editable_mounts():
            pre = "" if ed["name"] in (".", "") else ed["name"].rstrip("/") + "/"
            names += [self._normp(pre + p) for p in ed["protect"]]
        return names + self._eval_protected()

    def agent_brief(self) -> str:
        """Instruction for the editing agent (used by make_roles as the CliAgentDeveloper
        brief): the goal, the editable surface, the protected files, and the eval command.
        Phase 4: when several editable repos are mounted, the surface is namespaced per repo
        subdir so the agent knows where it may edit across the workspace."""
        goal = "maximize" if self.direction == "max" else "minimize"
        surf_globs: list[str] = []
        for ed in self._editable_mounts():
            pre = "" if ed["name"] in (".", "") else ed["name"].rstrip("/") + "/"
            surf_globs += [pre + g for g in ed["surface"]]
        surf = ", ".join(surf_globs)
        prot = ", ".join(self._protected_names()) or "(none)"
        cmd = " ".join(self.eval.command) if self.eval else "(proposed during onboarding)"
        setup = " ".join(self.eval.setup) if (self.eval and self.eval.setup) else ""
        deps = (f" Dependencies are installed before each eval by `{setup}`, so to add a "
                "package, edit the requirements file it reads (if it is in your allowed "
                "paths).") if setup else ""
        return (f"You are improving an existing experiment repository to {goal} the eval "
                f"metric. Goal: {self.goal}\n"
                f"You may ONLY edit files matching: {surf}. Do NOT modify (the operator "
                f"runs the evaluation): {prot}. The eval is run as: `{cmd}`.{deps} Make one "
                f"focused change to make the eval succeed and improve the metric, then stop.")

    def repo_spec(self) -> dict:
        mounts = self._editable_mounts()
        surface: list[str] = []
        for ed in mounts:
            pre = "" if ed["name"] in (".", "") else ed["name"].rstrip("/") + "/"
            surface += [pre + g for g in ed["surface"]]
        return {
            "editables": mounts,                         # Phase 4: every editable repo + mount
            "edit_surface": surface,                     # namespaced union over all repos
            "protected_names": self._protected_names(),
            "references": [r.model_dump() for r in self.references],
            "data": dict(self.data),
            # Back-compat single-seed hint (first editable): consumers that still seed one dir.
            "editable_path": mounts[0]["path"] if mounts else "",
        }

    def _bounds(self) -> dict:
        return {k: (float(lo), float(hi)) for k, (lo, hi) in self.params.items()}

    def build_roles(self):                     # offline: no edits (baseline / param search)
        if self.params:                        # cli_overrides hyperparameter search
            return (RepoParamResearcher(self._bounds(), seed=self.seed), NoOpRepoDeveloper())
        return (RepoResearcher(seed=self.seed), NoOpRepoDeveloper())

    def llm_roles(self, client: LLMClient, parser: str = "tool_call"):
        """When `params` is set: an LLM hyperparameter proposer over the bounds (framework
        cli_overrides mode). Otherwise a free-form code-edit proposer; the editing agent is
        wired in make_roles from repo_spec, and this NoOp developer is the validator's
        baseline fallback."""
        if self.params:
            goal = "maximize" if self.direction == "max" else "minimize"
            hint = (f"Propose hyperparameters to {goal} the eval metric, within bounds: "
                    + ", ".join(f"{k} in [{lo}, {hi}]" for k, (lo, hi) in self._bounds().items()))
            return (LLMResearcher(client, space_hint=hint, bounds=self._bounds(),
                                  parser=parser), NoOpRepoDeveloper())
        hint = ("You are improving an existing experiment repository. Propose the next "
                "concrete change to try (as a short rationale); leave params empty unless "
                "the experiment exposes numeric knobs.")
        return (LLMResearcher(client, space_hint=hint, bounds=None, parser=parser),
                NoOpRepoDeveloper())


# Back-compat: the Developer half (RepoWriteTools, the in-house LLM developer, onboarding, the
# xlsx renderer) moved to adapters/repo_developer.py (BACKLOG §4 "repo_task split"). Re-imported
# HERE, at the END of the module — after every spec/task definition above is bound — so existing
# imports from `looplab.adapters.repo_task` (and the flat `looplab.repo_task` alias) keep
# resolving, without a circular-import trap if repo_developer ever imports a spec from here.
from looplab.adapters.repo_developer import (  # noqa: E402,F401
    LLMOnboarder, LLMRepoDeveloper, RepoWriteTools, _xlsx_to_markdown,
)
