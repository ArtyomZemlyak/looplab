"""RepoTask (kind="repo", ADR-7): the R&D agent works inside an EXISTING repo — it edits
experiment code within an allow-listed surface, and success is measured by running the
OPERATOR'S OWN eval command and reading the metric it emits. The agent never authors the
metric (trust boundary): the eval command, the FILE that command executes when the repo
already ships it (`entrypoint_candidates` / `_entrypoint_protect`) and its output files are
task-owned and protected from edits (same mechanism that guards the mlebench grader).

Phase 1: ONE editable repo + read-only references mounted for runtime + an explicit
operator-written `eval_spec`. The agent backend (opencode) edits the repo worktree;
offline / on agent failure a NoOp developer leaves the repo at baseline.

See the architecture study (plans/) for the full workspace/eval model and phases.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping
import json
import os
from pathlib import Path
import random
import re
from typing import Optional

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from looplab.core.comparison import ComparisonContract
from looplab.core.models import Idea, Node, RunState, validate_direction
from looplab.core.parse import LLMClient
from looplab.agents.roles import LLMResearcher


class ReferenceSpec(BaseModel):
    name: str
    path: str
    # NOT a copy: a mounted reference is exposed as a read-only symlink in the local tier
    # (`engine/workspace.py::_link_input`) and a read-only bind mount under Docker
    # (`engine/eval_dispatch.py`). Solution code may READ it; a write is refused.
    mount: bool = False        # expose read-only in the eval workdir (runtime dep) vs context-only


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


class DataSpec(BaseModel):
    """One data input with PER-SOURCE permissions (composable data model). The ORIGINAL is read-only
    by default (the agent may always read it and produce DERIVED data in its own workdir); the flags
    relax or restrict that. A bare string path in `data`/`dataset` desugars to `DataSpec(path=…)` with
    all defaults (everything allowed EXCEPT editing the original)."""
    path: str
    mount: bool = True         # (1) read-only symlink at ./<name> (default) | False = copy INTO the workdir
    edit: bool = False         # (2) may the agent modify the ORIGINAL in place (through its mount)?
    #                              default False (protect original). A mount:false copy is always
    #                              node-local and writable — edits there can't reach the original.
    copy_modify: bool = True   # (3) may the agent copy it and modify the copy?          (advisory)
    preprocess: bool = True    # (4) may it preprocess/augment it into a training set?    (advisory)
    extend: bool = True        # (5) may it extend / expand the data?                     (advisory)
    # (3)-(5) are ADVISORY: they shape the agent brief's allow-list (_data_brief) but no gate
    # enforces them mechanically — only `mount` and `edit` have enforced semantics (the write gate
    # + the untrusted tier's read-only binds). Documented in docs/guide/tasks.md.

    @model_validator(mode="after")
    def _mount_edit_consistent(self):
        # `edit:true` means "the agent modifies the data", but a mount:true source is a read-only SYMLINK
        # to the original: the agent's build-time writes to ./<name> resolve OUTSIDE the workdir and are
        # dropped by the materializer (they'd escape the sandbox), so the edit would silently no-op — the
        # recurring footgun. COERCE the combination to `mount:false` (a writable per-node COPY, which is
        # exactly what edit:true wants) rather than REJECT it: rejecting broke `resume`/`finalize` of
        # pre-existing runs whose verbatim task.snapshot.json legally carried mount:true+edit:true (it was
        # valid AND documented before) — coercion preserves the "agent may modify the data" intent and
        # loads every historical snapshot. (mount defaults True, so `{edit:true}` alone hits this too.)
        # The write gate protects genuine (edit:false) mounts defensively — see _protected_names.
        if self.mount and self.edit:
            self.mount = False
        return self


# What a pathless `file_json`/`file_regex` reader COSTS in each of EvalSpec's four reader slots —
# each one MEASURED before it was written down, because they are not the same failure and a message
# naming the wrong symptom sends the operator hunting the wrong bug. `runtime/command_eval.py` owns
# the other half of the message (which readers need a path + the corrected example) and is the only
# spelling of it; only the consequence sentence is per-slot, and this is its only spelling.
_PATHLESS_COST = {
    # `_read_file` returns None -> the node has no metric at all.
    "metric": "Without `path` the reader returns nothing and every node fails no_metric.",
    # `run_command_eval`'s `declared` comprehension drops a reader that returns None, so the extra
    # metric is simply absent from the node's report — the quietest of the four.
    "metrics": ("Without `path` this extra reader is silently dropped: the node reports no value "
                "under that name and nothing anywhere says why."),
    # `_violations` counts an UNREADABLE value as a violation ("an unverifiable constraint is a
    # violation, never a silent pass"), so the bound can never be satisfied.
    "constraints": ("Without `path` the bound can never be verified, and an unverifiable constraint "
                    "counts as a VIOLATION — every node is marked infeasible and excluded from "
                    "best-selection, so the run finishes with no best node."),
    # `_drift(primary, None, tol)` is True by construction ("can't corroborate -> drift").
    "cross_check": ("Without `path` the cross reader corroborates nothing and the drift check fails "
                    "closed — under eval_trust_mode=\"ratify_freeze_drift\" EVERY node's metric is "
                    "discarded as drift and the run finishes with no best node."),
}


def eval_reader_path_errors(task_or_spec) -> list[str]:
    """Every "this reader can never read anything" problem in an EvalSpec, one message per reader.

    Walks `EvalSpec.readers()` (the single enumeration of the reader slots) and asks
    `runtime/command_eval.metric_spec_path_error` — the authority that lives beside the reader table
    deciding the fact — about each one. Returns [] for a usable spec.

    It asks `host_score_labels_error` in the same walk, and for the same reason the path rule is
    asked of all four slots rather than only `metric`: a `host_score` in `constraints` holds the
    operator's answer key exactly as much as one in `metric` does. Only that rule's SPEC-LOCAL halves
    are decidable here (labels absent, labels relative) — "inside the workspace" needs a run
    directory, which a spec being authored does not have yet, and is `eval_workspace_conflicts`.

    Accepts the TASK as well as the spec, and unwraps it HERE rather than at the call site: a
    getattr probe for `eval` on a task would read as a duck-typed TaskAdapter hook and trip the
    two-way `TASK_OPTIONAL_HOOKS` registry scan (tests/test_task_adapter_contract.py) — correctly,
    because it is not one. `eval` is a declared FIELD of exactly one adapter, so an isinstance says
    that precisely, and every other kind (toy/dataset/mlebench) yields [] with no readers to check.

    Shared by the submit-time refusal below and `cli/run_cmds.py::resume`, which reports the SAME
    text as a warning for a run that was started before the refusal existed (see `_readers_usable`).
    """
    from looplab.runtime.command_eval import host_score_labels_error, metric_spec_path_error
    spec = task_or_spec.eval if isinstance(task_or_spec, RepoTask) else task_or_spec
    if not isinstance(spec, EvalSpec):
        return []
    out: list[str] = []
    for label, slot, reader in spec.readers():
        err = metric_spec_path_error(reader, consequence=_PATHLESS_COST[slot])
        if err:
            out.append(f"{label}: {err}")
        labels_err = host_score_labels_error(reader)
        if labels_err:
            out.append(f"{label}: {labels_err}")
    return out


def eval_workspace_conflicts(task_or_spec, workspace_root) -> list[str]:
    """The reader problems that can only be seen once the RUN DIRECTORY is known, one per reader.

    Today that is exactly one: a `host_score` answer key that resolves inside the workspace. It is
    not checkable by `EvalSpec`'s own validator — a spec is authored before a run dir exists — and it
    is not checkable at score time either, because the only safe thing a reader can do inside an eval
    worker is score nothing (the raise it used to do left the node with no terminal event and the run
    re-dying on every resume). So it is asked HERE, by the launch surface, on a directory that has
    not been created yet.

    Separate from `eval_reader_path_errors` rather than a mode of it: the two have different
    AUDIENCES. That one is re-asked on `resume` as a warning for runs started before it existed;
    this one is a launch-time refusal, and re-asking it on resume would refuse a run whose evidence
    is already on disk.
    """
    from looplab.runtime.command_eval import host_score_labels_error
    spec = task_or_spec.eval if isinstance(task_or_spec, RepoTask) else task_or_spec
    if not isinstance(spec, EvalSpec) or workspace_root is None:
        return []
    out: list[str] = []
    for label, _slot, reader in spec.readers():
        err = host_score_labels_error(reader, workspace_root=workspace_root)
        if err:
            out.append(f"{label}: {err}")
    return out


def entrypoint_candidates(command) -> list[str]:
    """The cwd-relative file path(s) an eval `command` argv would actually EXECUTE, or [].

    Two forms, the ones an operator's `cmd` actually takes:
      * `python -m pkg.mod`        -> ["pkg/mod.py", "pkg/mod/__main__.py"]
      * `python path/to/score.py`  -> ["path/to/score.py"]
    Both spellings of `-m` are returned because either one can be what the module name resolves to;
    the caller keeps whichever EXISTS. The argv is read the way CPython reads it — interpreter flags
    up to the first non-flag token, which is the script — so `-m` wins over a later `.py` token
    (`python -m pkg.mod --out x.py` executes the module), and a `-m` AFTER the script is the
    script's own argument (`python train.py -m eval` executes `train.py`, not `eval.py`).

    A transparent prefix that names the interpreter VERBATIM is read through — `srun python
    score.py`, `env FOO=1 python -m pkg.mod`, `nohup python score.py` — because from that token on
    the argv is the same `<interpreter> [flags] <target>` grammar.

    Deliberately narrow and purely syntactic — it never touches the filesystem, so its truth table
    can be stated. Anything else returns []: a shell wrapper (`bash run.sh`), a bare binary, an
    installed console script, a launcher that does NOT name an interpreter (`torchrun … score.py`,
    `accelerate launch …`, `deepspeed …`, whose own flag grammar decides which token is the script),
    `python -c '...'`, the combined `python -mpkg.mod` spelling, an absolute or escaping path. []
    is NOT "nothing to protect"; it is "this argv does not say which file the score stage runs", and
    `eval_entrypoint_unprotected` turns that into a submit-time warning naming the command. Guessing
    wider would freeze a file the operator never meant to freeze, which costs the Developer a whole
    repair attempt on a refusal it cannot act on — the same cost `_declared_deps_brief` exists to
    avoid on the other side.

    The `.py`-token rule is deliberately the SAME rule `engine/eval_stages.py` already applies to a
    stage command (`_stage_reachable_files` / `_unrunnable_protected_scripts`): a workdir-relative
    `.py` token that is not a flag. That file treats `python -m pkg` as OPAQUE and refuses stage
    reuse over it; here the same argv is RESOLVABLE, because a module name maps to a file path
    without having to bound what the process reads.
    """
    argv = [t for t in (command or []) if isinstance(t, str)]
    # Both forms below read the argv as `<interpreter> [flags] <target>`, which is only true when the
    # interpreter is the argv HEAD. Under a wrapper — `bash run_eval.sh --cfg configs/base.py`,
    # `./run.sh --metrics-out metrics.py` — the first non-flag `.py` token is an ARGUMENT, not the
    # executed file, and returning it did two wrong things at once: `_entrypoint_protect` froze a file
    # the operator never meant to freeze (costing the Developer a repair attempt on a refusal it
    # cannot act on), and `eval_entrypoint_unprotected` fell silent for exactly the wrapper case that
    # warning exists to name. The docstring above already says `bash run.sh` returns []; this is what
    # makes that true. Say nothing rather than guess.
    # Find the interpreter. Usually argv[0], but a transparent prefix that carries the interpreter
    # VERBATIM — `srun python score.py`, `env FOO=1 python score.py`, `nohup python -m pkg.mod` —
    # is still a `<interpreter> [flags] <target>` argv from that token on, so parse from there.
    # Deliberately NOT extended to launchers that do not name an interpreter (`torchrun … score.py`,
    # `accelerate launch …`, `deepspeed …`): their own flag grammar decides which of `--nproc_per_node
    # 2 score.py` is the script, we do not know it, and a wrong guess freezes a file the operator
    # never named. Those keep returning [] and `eval_entrypoint_unprotected` names them at submit
    # time, which is the honest outcome rather than a confident wrong one.
    start = -1
    for i, tok in enumerate(argv):
        head = tok.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if re.match(r"^(?:python|pypy)\d*(?:\.\d+)?(?:\.exe)?$", head):
            start = i
            break
    if start < 0:
        return []
    # Read the interpreter's OWN flags, and stop at the first token that is neither a flag nor a
    # flag's argument — that token is the script, and everything after it belongs to the program.
    # Scanning the whole argv instead made `python train.py -m eval` resolve to `eval.py` (measured)
    # while CPython runs `train.py`: `_entrypoint_protect` froze a file the operator never named,
    # the REAL scorer stayed editable, and because the result was non-empty the unprotected-scorer
    # warning never fired. Every cost the wrapper rule above exists to avoid, at once.
    _TAKES_ARG = ("-W", "-X", "-Q", "--check-hash-based-pycs")
    i = start + 1
    while i < len(argv):
        tok = argv[i]
        if tok == "-m":
            mod = argv[i + 1] if i + 1 < len(argv) else ""
            if not mod or not re.match(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$", mod):
                return []          # `-m` with a non-module argument: say nothing rather than guess
            stem = mod.replace(".", "/")
            return [stem + ".py", stem + "/__main__.py"]
        if tok == "-c" or tok == "-":
            return []              # no file is executed / stdin: nothing this can name
        if tok in _TAKES_ARG:
            i += 2
            continue
        if tok.startswith("--"):
            i += 1
            continue
        if tok.startswith("-") and len(tok) > 1:
            # A short-option cluster. `-um pkg` really is `-u -m pkg`, and the combined `-mpkg.mod`
            # spelling is documented as opaque — so any cluster carrying `m` or `c` is opaque too
            # rather than silently parsed one way and executed the other.
            if "m" in tok[1:] or "c" in tok[1:]:
                return []
            i += 1
            continue
        # The first non-flag token: this is the script CPython executes.
        if not tok.endswith(".py"):
            return []
        rel = tok.replace("\\", "/")
        while rel.startswith("./"):
            rel = rel[2:]
        if os.path.isabs(rel) or ".." in rel.split("/"):
            return []              # outside the workdir: not a name `protect` can express
        return [rel]
    return []


def eval_entrypoint_unprotected(task) -> list[str]:
    """The submit-time warning for an eval `command` whose scorer could not be protected, or [].

    The trust boundary the Developer's own prompt states — "the operator's `cmd` is APPENDED
    automatically as the final, protected `score` stage — you CANNOT rewrite how the run is scored"
    — was only ever true of the STAGE. The FILE that stage runs sat in the edit surface, and on
    `runs/rubertlite-dr-unified-v6` (cmd `python -m vectorsearch.test`, `protect: []`,
    `edit_surface: ["**/*"]`) the Developer edited `vectorsearch/test.py` to shell out to
    `vectorsearch.train` when it found no checkpoint — the exact "train-then-score if no artifact
    exists" pattern the same prompt forbids two paragraphs earlier. The train stage had already
    produced one; the scorer's `checkpoint_path` pointed at an absolute path in the SOURCE repo
    rather than the node's workdir, so it re-trained inside the scorer (with `overwrite: true`
    destroying the train stage's artifact) and the run paid 2x GPU per node to report a number the
    pipeline never measured. `RepoTask._entrypoint_protect` makes the sentence true wherever the
    argv names a file; this names the residue, at the one moment the operator can still act.

    It fires ONLY when the argv names no in-repo entrypoint at all (`entrypoint_candidates() == []`).
    An entrypoint that resolves but does not EXIST in the source is deliberately silent: that is the
    designed flow this adapter is mostly used for — the Developer AUTHORS the eval entrypoint (see
    `_REPO_DEV_SYSTEM_BODY`, and `PROTECTED_SCRIPT_MISSING`, which tells an operator to DROP a
    protect entry so the Developer may author it). Warning there would fire on the majority of repo
    tasks, and a warning that always fires is read as noise.

    A warning, not a refusal: an unresolvable command is legitimate (a wrapper that activates an
    env, a console script), the run is still perfectly runnable, and the operator may know the
    scorer is unreachable from the edit surface anyway. What is NOT acceptable is silence, because
    the prompt promises the operator something the gate is not enforcing.
    """
    if not isinstance(task, RepoTask) or task.eval is None or not task.eval.command:
        return []
    if not task.eval.protect_entrypoint:
        return []                  # the operator handed the scorer to the Developer on purpose
    if entrypoint_candidates(task.eval.command):
        return []
    cmd = " ".join(str(t) for t in task.eval.command)
    return [
        f"cmd.command `{cmd}` names no in-repo entrypoint file, so LoopLab cannot protect the code "
        "the score stage runs (a shell wrapper, a bare binary, an installed console script or "
        f"`python -c` looks like this). The score STAGE is always the operator's; the FILES it runs "
        "are frozen only when the command names them (`python -m pkg.mod` or `python score.py`). As "
        "written, the Developer's edit surface may still contain whatever this command ends up "
        "executing — list those files in `protect` if the scoring code must be frozen."]


def _grandfathered(info: ValidationInfo) -> bool:
    """True when this task document is being RE-loaded for a run that ALREADY EXISTS.

    `resume`/`finalize` rebuild the task by re-validating the verbatim `task.snapshot.json` the run
    was started with, so a validator that raises retroactively makes every pre-existing run
    unresumable — the same trap that made `DataSpec._mount_edit_consistent` coerce instead of reject.
    A new refusal therefore has to distinguish "someone is submitting this spec" from "this spec is
    already the recorded truth of a run in progress", and the pydantic validation CONTEXT is that
    signal: `adapters/tasks.py::validate_task(..., existing_run=True)` sets it, direct construction
    (`EvalSpec(...)`, `RepoTask(**d)`) never does, so the strict path is the DEFAULT and only the
    reload sites opt out. CLAUDE.md invariant #6 is the same principle for settings: what the run
    recorded when it started wins on resume.
    """
    ctx = getattr(info, "context", None)
    return bool(isinstance(ctx, dict) and ctx.get("existing_run"))


class EvalSpec(BaseModel):
    """The operator's trusted evaluation (the agent does not author this)."""
    command: list[str] = Field(default_factory=list)   # argv, no shell; carries env activation
    # Operator-declared ordered pipeline (data_prep → train → …); when set these ARE the canonical
    # stages the engine runs — the Developer implements the scripts, not the structure, and its
    # looplab_stages.json is IGNORED (see Engine._resolve_stages). Each stage is
    # {name, command:[argv], timeout?, check?}; the LAST stage's stdout carries the metric. When
    # empty, the single `command` runs (and the Developer MAY declare PRECEDING stages before it).
    # Validated by the SAME shared rules as the declare_stages tool
    # (runtime/command_eval.validate_stages); a stage named 'score' is allowed HERE — the operator
    # owns scoring, the reservation only guards Developer manifests.
    stages: list[dict] = Field(default_factory=list)
    # The DECLARED ENVIRONMENT for this task's eval — applied to `setup`, to the single `command`,
    # and to EVERY stage, with a stage's own `env` overlaying it (most specific wins). This is where
    # a fact about the TASK belongs: `VS_LOCAL_DATA_ROOT=/home/jovyan/data/dr-local` says where this
    # repo's data lives, which is true of the repo and not of the engine, and stating it here means
    # every node inherits it instead of each one spending a repair attempt rediscovering it (backlog
    # F1d). It rides in `task.snapshot.json` verbatim, so a resume re-validates and re-applies the
    # SAME environment the run's results were produced under. Operator-authored, like the rest of
    # this model — the Developer has no surface for it, by design. Secrets are REFUSED; see
    # `core/envsafe.py::validate_env_map`, the ONE rule all three declaring levels go through.
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = "."                           # relative to the node eval workdir
    # Freeze the FILE the `command` executes, when the argv names one that the editable source
    # actually ships (`python -m pkg.mod`, `python score.py` — see `entrypoint_candidates`). ON by
    # default, because that is what the Developer's own prompt already promises the operator ("the
    # operator's `cmd` is APPENDED automatically as the final, protected `score` stage — you CANNOT
    # rewrite how the run is scored"), and only the STAGE was ever protected.
    #
    # It is a DEFAULT rather than an unconditional rule because the opposite arrangement is a
    # first-class, documented flow: on most repo tasks the Developer AUTHORS the eval entrypoint,
    # and `eval_stages.PROTECTED_SCRIPT_MISSING` tells an operator to drop a protect entry so it
    # can. A repo that HAPPENS to ship a file at the scorer's path (a committed `looplab_eval.py`
    # from an earlier run) would otherwise silently stop being authorable with no way to say so.
    #
    # The off-switch is deliberately NOT "the operator listed their own `protect`". `protect` only
    # ADDS, so an operator who protects `data/**` would silently give up the scorer freeze that
    # `protect` is the very vocabulary for — the surprising direction. This field says the thing
    # itself, and the run's `task.snapshot.json` records the decision.
    #
    # The run's `task.snapshot.json` records the decision FOR RUNS STARTED AFTER THIS FIELD EXISTED.
    # A pre-field snapshot simply has no key, so a resumed run picks the `True` default up and its
    # edit surface TIGHTENS mid-run: later nodes refuse edits earlier nodes were allowed, and
    # `seed_protected_files` starts materializing a file earlier nodes never received. That is
    # deliberate and is the one direction worth taking without asking — the resumed run gets the fix
    # rather than keeping the hole it was started with, and the cost is a refusal the Developer can
    # act on rather than a corrupt metric nobody can see. It is pinned by
    # `tests/test_eval_entrypoint_protection.py::test_an_existing_runs_snapshot_reloads_with_the_freeze`.
    #
    # Contrast `config.py::LEGACY_CONFIG_SNAPSHOT_DEFAULTS`, which pins the OPPOSITE answer at the
    # Settings layer (`read_fence` is listed there). The two differ on what a wrong answer costs: an
    # unexpected read-fence denial FAILS the node outright and buys paid repairs, while an
    # unexpected entrypoint freeze refuses one edit and says why. Tightening is safe when the run
    # can still make progress; it is not when the run stops.
    protect_entrypoint: bool = True
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
    # The boundary below deliberately keeps the values as dicts for public/back-compat access, but
    # gives them a closed schema: only argv-string overrides and a finite positive timeout. Search
    # uses a declared cheap profile; the confirm phase requests "full" (base eval when absent).
    profiles: dict[str, dict] = Field(default_factory=dict)
    # Drift cross-check (Phase 4, eval_trust_mode="ratify_freeze_drift"): an INDEPENDENT
    # built-in reader (stdout_json/regex | file_json/regex — never `adapter`) that re-reads
    # the same metric from a source the agent can't forge (e.g. the framework's real stdout).
    # When it can't corroborate the frozen adapter within `drift_tolerance`, the metric is
    # discarded and a `spec_drift` event is recorded. None disables the check.
    cross_check: Optional[dict] = None
    # Validated below as a finite non-negative number: `NaN` makes every drift comparison False
    # (fail-OPEN — the drift cross-check silently stops corroborating), and a negative tolerance
    # rejects even identical metrics. See `_finite_non_negative_tolerance`.
    drift_tolerance: float = 1e-6
    # Multi-objective (#5). `metrics`: extra named readers reported alongside the primary
    # (audit/observability), e.g. {"latency_ms": {"kind": "stdout_json", "key": "latency"}}.
    # `constraints`: reader specs with a `max`/`min` bound; a node that violates ANY (or whose
    # constraint value can't be read) is still measured but EXCLUDED from best-selection —
    # "optimize the metric subject to latency_ms <= 100". Operator-owned (trust boundary).
    metrics: dict[str, dict] = Field(default_factory=dict)
    constraints: list[dict] = Field(default_factory=list)

    @field_validator("profiles", mode="before")
    @classmethod
    def _valid_profiles(cls, value, info: ValidationInfo):
        """Reject new profile shapes that the dispatcher would reinterpret or crash on.

        ``build_command`` intentionally accepts a plain mapping because it also consumes durable
        dict snapshots, so the operator-authored task boundary is where the stronger contract must
        live. Existing runs are different: before this validator, ``dict[str, dict]`` accepted extra
        profile metadata, tuple/string ``overrides`` and non-positive/unparseable ``timeout`` values.
        Their snapshots must keep reaching the same defensive runtime normalization they used when
        recorded; rejecting or cleaning them now would make a run unresumable or change its treatment.
        ``_grandfathered`` therefore preserves the raw nested values only on the explicit reload path.

        Keep the field's historical ``dict[str, dict]`` API rather than replacing values with nested
        model instances; valid callers that index ``eval.profiles[name]["timeout"]`` keep working.
        """
        import math

        if _grandfathered(info):
            return value

        if not isinstance(value, Mapping):
            raise ValueError("eval.profiles must be an object keyed by profile name")

        clean: dict[str, dict] = {}
        allowed = frozenset({"overrides", "timeout"})
        for name, raw_profile in value.items():
            # Validate before pydantic's dict[str, ...] coercion so 1 never quietly becomes "1".
            if not isinstance(name, str) or not name.strip():
                raise ValueError("eval profile names must be non-empty strings")
            if not isinstance(raw_profile, Mapping):
                raise ValueError(f"eval profile {name!r} must be an object")

            unknown = [key for key in raw_profile if key not in allowed]
            if unknown:
                rendered = ", ".join(sorted((repr(key) for key in unknown)))
                raise ValueError(
                    f"eval profile {name!r} has unsupported keys: {rendered}; "
                    "allowed keys are 'overrides' and 'timeout'")

            profile = dict(raw_profile)
            if "overrides" in profile:
                overrides = profile["overrides"]
                if (not isinstance(overrides, list)
                        or any(not isinstance(token, str) for token in overrides)):
                    raise ValueError(
                        f"eval profile {name!r} overrides must be a list of strings")
            if "timeout" in profile:
                timeout = profile["timeout"]
                numeric = not isinstance(timeout, bool) and isinstance(timeout, (int, float))
                try:
                    seconds = float(timeout) if numeric else float("nan")
                except (TypeError, ValueError, OverflowError):
                    seconds = float("nan")
                if not math.isfinite(seconds) or seconds <= 0:
                    raise ValueError(
                        f"eval profile {name!r} timeout must be a finite positive number")
            clean[name] = profile
        return clean

    @field_validator("drift_tolerance")
    @classmethod
    def _finite_non_negative_tolerance(cls, v):
        # A NaN tolerance makes `abs(a - b) <= tol` False for EVERY pair, so the Phase-4 drift
        # cross-check silently stops corroborating (fail-open on the exact trust boundary it exists
        # to enforce); a negative one rejects even identical metrics. Reject at task admission —
        # `EvalSpec` is operator-authored, so this is a clear config error, not untrusted input.
        import math
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
            raise ValueError("drift_tolerance must be a finite number")
        if float(v) < 0:
            raise ValueError("drift_tolerance must be >= 0")
        return float(v)

    @field_validator("constraints")
    @classmethod
    def _finite_constraint_bounds(cls, v):
        # `check_constraints` compares a read float against `c["max"]`/`c["min"]` directly, and
        # `_evaluate` catches only GpuPinUnenforceable — so a string bound raises TypeError
        # ("'>' not supported between 'float' and 'str'") that escapes the eval worker, cancels the
        # whole eval batch with NO terminal event for the node, and re-raises on every resume,
        # wedging the run permanently. A bool is an int in Python and would silently mean 0/1.
        import math
        for c in (v or []):
            if not isinstance(c, dict):
                raise ValueError("each constraint must be an object")
            for bound in ("min", "max"):
                b = c.get(bound)
                if b is None:
                    continue
                if isinstance(b, bool) or not isinstance(b, (int, float)) or not math.isfinite(float(b)):
                    raise ValueError(
                        f"constraint {c.get('name', '?')!r} {bound} must be a finite number")
        return v

    @field_validator("metric")
    @classmethod
    def _valid_metric_kind(cls, v):
        # `kind` is HOW to READ the metric, not the optimization direction — catch the common mix-up of
        # putting "max"/"min" here (that goes in the task's `direction`). An unknown kind makes the metric
        # unreadable → every node fails with no_metric, so reject it at submit with a clear message.
        # The reader table in `runtime/command_eval.py` is the authority; validating against a local
        # copy is how the kinds came to be enumerated in three places (doc 25 RA-04/RA-05).
        # A KNOWN kind is not yet a USABLE spec — the `path` half of that moved to `_readers_usable`
        # below, which applies it to ALL FOUR reader slots instead of only this one.
        from looplab.runtime.command_eval import METRIC_READERS
        _KINDS = set(METRIC_READERS)
        if isinstance(v, dict):
            k = v.get("kind", "stdout_json")
            if k not in _KINDS:
                raise ValueError(
                    f"eval.metric.kind {k!r} is not a metric reader. Use one of {sorted(_KINDS)} (HOW to "
                    "read the printed metric, e.g. stdout_json). The max/min DIRECTION belongs in the "
                    "task's `direction`, not here.")
        return v

    @field_validator("metric")
    @classmethod
    def _valid_metric_subject(cls, v):
        """`metric.subject` — WHAT THE NUMBER IS ABOUT — validated by the shared path-shape rule.

        The operator's field, on the operator's protected `score` stage, and deliberately not
        something the agent authors: the subject is what the metric MEANS, and the trust boundary
        `engine/metric_salvage.py` states is that the agent writes the training script and therefore
        writes the very text an extractor would read.

        Same `_validate_rel_paths` that `expect.files` and `needs` already go through, so a path
        shape one declaration accepts is never a shape another silently reinterprets — and the
        containment is re-decided at eval time against the REAL workdir by `_confined`, because a
        symlink the candidate plants at eval time is invisible from a manifest.

        Absent is ACCEPTED here and is not a submit-time refusal. Whether an unbound metric is a
        violation is `Settings.metric_subject`'s rung, and refusing at submit would make every task
        file written before this shipped unloadable — including the `task.snapshot.json` of a run
        being RESUMED, which `_grandfathered` reloads without re-validating precisely because a
        recorded run must keep reaching the treatment it had.
        """
        # The cap became a REQUIRED argument upstream on 2026-08-14. A subject is an
        # output-side declaration, so it takes `expect.files`' cap rather than `needs`'.
        from looplab.runtime.command_eval import (MAX_STAGE_EXPECT_FILES,
                                                  _validate_rel_paths)
        if isinstance(v, dict) and v.get("subject") is not None:
            clean, err = _validate_rel_paths("score", "subject", v["subject"],
                                                MAX_STAGE_EXPECT_FILES)
            if err is not None:
                raise ValueError(
                    err.replace("stage 'score'", "eval.metric")
                    + " `subject` names the artifact the metric is a CLAIM ABOUT (the checkpoint, "
                      "the predictions file), relative to the node's eval workdir.")
            v = dict(v)
            v["subject"] = clean
        return v

    @field_validator("cross_check")
    @classmethod
    def _cross_check_not_adapter(cls, v):
        from looplab.runtime.command_eval import validate_cross_check
        return validate_cross_check(v)

    def readers(self) -> list[tuple[str, str, dict]]:
        """Every metric-reader spec this eval declares, as `(label, slot, spec)` — THE enumeration of
        "where a reader can appear", so a rule about readers is applied to all four slots or to none.

        It was not: the submit-time `path` check covered only the primary `metric`, and the same
        pathless `file_json` in `metrics` / `constraints` / `cross_check` still validated, started a
        run, and failed it three different quiet ways (`_PATHLESS_COST`). `_eval_protected` had
        already had to hand-write this same walk for the write-gate's protected-name list.

        `label` is what the operator wrote in the task file (`eval.constraints[0]`), so a refusal can
        name the field; `slot` is the FIELD, for looking up a per-slot rule. Order matches the order
        `run_command_eval` reads them in, and is what `_eval_protected`'s dedupe preserves."""
        out: list[tuple[str, str, dict]] = []
        if isinstance(self.metric, dict):
            out.append(("eval.metric", "metric", self.metric))
        for name, r in (self.metrics or {}).items():
            if isinstance(r, dict):
                out.append((f"eval.metrics[{name!r}]", "metrics", r))
        for i, c in enumerate(self.constraints or []):
            if isinstance(c, dict):
                nm = c.get("name")
                out.append((f"eval.constraints[{i}]" + (f" (name={nm!r})" if nm else ""),
                            "constraints", c))
        if isinstance(self.cross_check, dict):
            out.append(("eval.cross_check", "cross_check", self.cross_check))
        return out

    @model_validator(mode="after")
    def _readers_usable(self, info: ValidationInfo):
        """Refuse a reader that can never read anything — or that grades against the wrong file — in
        ANY of the four slots, at SUBMIT time.

        The second half is `host_score`'s held-out labels (`host_score_labels_error`). It belongs
        with the first because the SYMPTOM is identical: at score time the reader returns None, every
        node fails `no_metric`, and the run finishes with no best node — the reader used to RAISE
        there, which killed the run outright with no terminal event for the node, so the check had to
        move somewhere it can refuse instead of crash. This validator sees a spec without a run
        directory, so it applies only the two spec-local rules (labels absent, labels relative); the
        "inside the workspace" half needs a run root and is asked by the launch surface that has one.

        Deliberately NOT inside `validate_cross_check`: that predicate is ALSO the runtime guard
        `run_command_eval` calls per eval, so a raise there would abort the eval worker mid-run with
        no terminal event for the node — the failure mode `_dig`/`_regex_metric`/`_confined` each have
        a comment about. Submit time is the only place this can refuse without changing runtime
        behaviour, and a model validator is the one seam every submit surface (CLI, `/api/start`,
        Genesis, the agent-facing run tools) already goes through.

        The reload escape hatch is `_grandfathered`: `resume`/`finalize` re-validate the run's own
        `task.snapshot.json`, and a run that already exists must stay resumable even when the spec it
        recorded is one we would now refuse. Resume reports these as warnings instead (see
        `cli/run_cmds.py::resume`), so the diagnosis is not lost — only the refusal is.
        """
        if _grandfathered(info):
            return self
        errs = eval_reader_path_errors(self)
        if errs:
            # ALL of them, not the first: a spec that mis-authors `metrics` AND `constraints` the same
            # way (the natural case — the author had one wrong idea about `key`, not four) would
            # otherwise cost one submit round-trip per slot.
            raise ValueError("\n".join(errs))
        return self

    @field_validator("stages")
    @classmethod
    def _stages_valid(cls, v):
        # Reject a malformed operator pipeline at SUBMIT time with the shared stage rules — without
        # this, a bad `cmd.stages` entry silently vanished (pydantic ignored the unknown key entirely
        # before this field existed) and the Developer's manifest quietly took over the pipeline.
        if not v:
            return v
        from looplab.runtime.command_eval import validate_stages
        # `allow_env=True`: THIS is the operator's own pipeline (the same reason no name is reserved
        # here), and a stage `env` is operator-only. The Developer's two surfaces — the
        # `declare_stages` tool and a hand-written `looplab_stages.json` — keep the fail-closed
        # default and are refused.
        clean, err = validate_stages(v, allow_env=True)   # no reserved names: the operator owns `score`
        if err:
            raise ValueError(f"cmd.stages invalid: {err}")
        return clean

    @field_validator("env")
    @classmethod
    def _env_valid(cls, v):
        # The eval-wide half of the DECLARED ENVIRONMENT, validated by the SAME shared rule as the
        # per-stage block and the run-level setting (`core/envsafe.py::validate_env_map`) — three
        # levels that get MERGED must agree about what a legal declaration is, or an operator's
        # variable would change meaning depending on which line they wrote it on. Imported from
        # its HOME rather than through `command_eval`'s re-export: this is not a stage, and
        # `adapters` may import `core` directly.
        if not v:
            return {}
        from looplab.core.envsafe import validate_env_map
        clean, err = validate_env_map("cmd/eval `env`", v)
        if err:
            raise ValueError(err)
        return clean

    @model_validator(mode="after")
    def _command_or_stages(self):
        # The documented cmd shape is {"command" | "stages", ...}: one of the two must actually run.
        if not self.command and not self.stages:
            raise ValueError("cmd/eval needs a `command` (argv list) or a `stages` pipeline "
                             "— nothing would run otherwise.")
        return self


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
    within bounds, then Gaussian hill-climb around the best. It selects the conventional cheap
    `smoke` profile only when the composed task declares one (the confirm phase still requests
    `full`, which resolves to the base eval when absent)."""

    def __init__(self, bounds: dict, seed: int = 0, step: float = 0.3,
                 eval_profiles: Collection[str] | None = None):
        self.bounds = bounds
        self.rng = random.Random(seed)
        self.step = step
        # ``None`` preserves the historical direct-construction behavior. Product composition passes
        # the task's exact profile keys, including an explicit empty view when it declares none.
        self.eval_profile = "smoke" if eval_profiles is None or "smoke" in eval_profiles else None

    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        keys = list(self.bounds)
        if parent is None:
            params = {k: round(self.rng.uniform(*self.bounds[k]), 6) for k in keys}
            return Idea(operator="draft", params=params, rationale="random hyperparameters",
                        eval_profile=self.eval_profile)
        params = {}
        for k in keys:
            lo, hi = self.bounds[k]
            v = parent.idea.params.get(k, (lo + hi) / 2) + self.rng.gauss(0.0, (hi - lo) * self.step)
            params[k] = round(max(lo, min(hi, v)), 6)
        return Idea(operator="improve", params=params, eval_profile=self.eval_profile,
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
    comparison_contract: ComparisonContract | None = None
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
    # name -> data input. A value may be a bare abs-path string (all-default permissions) or a
    # DataSpec {path, mount, edit, copy_modify, preprocess, extend}. Mounted read-only at ./<name> unless
    # its spec says otherwise. See DataSpec for the per-source permission semantics.
    data: dict[str, DataSpec] = Field(default_factory=dict)

    @field_validator("data", mode="before")
    @classmethod
    def _coerce_data(cls, v):
        """Accept a bare path string per source (name -> "/abs/path") as shorthand for a DataSpec with
        all defaults, alongside the full object form (name -> {path, mount, edit, …})."""
        if isinstance(v, dict):
            return {k: ({"path": val} if isinstance(val, str) else val) for k, val in v.items()}
        return v
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
        return validate_direction(v)

    @model_validator(mode="after")
    def _expand_repo_paths(self):
        """Expand ~ and $ENV in every filesystem path the task points at, so a natural
        `editable_path: "~/data/vectorizer/dense-retrieval"` (or "$HOME/…") actually resolves —
        without this the `~` is treated as a literal directory and the repo mounts/scout tools come
        up EMPTY. Runs once; the resolved absolute path is what gets recorded + mounted. (A repo task
        is inherently tied to a local path, so resuming on a DIFFERENT machine/user whose home differs
        already needs editable_path re-pointed — the absolute snapshot just makes that explicit instead
        of silently re-resolving `~` to a different repo.)"""
        def exp(p):
            return os.path.expanduser(os.path.expandvars(p)) if isinstance(p, str) and p else p
        self.editable_path = exp(self.editable_path)
        for e in self.editables:
            e.path = exp(e.path)
        for r in self.references:
            r.path = exp(r.path)
        for spec in self.data.values():
            spec.path = exp(spec.path)
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

    def _declared_editable_mounts(self) -> list[dict]:
        """Normalize the single-repo shorthand + the multi `editables` into one list of
        {name, path, surface, protect, seed_mode}. name="." mounts at the workspace root.
        seed_mode is per-repo; "" defers to the run-wide Settings.seed_mode at seed time.

        The operator's DECLARED lists verbatim. `_editable_mounts` is what every consumer calls;
        this exists only so the entrypoint derivation below has mounts to resolve against without
        calling back into its own caller."""
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

    def _entrypoint_protect(self, mounts: list[dict]) -> dict[str, list[str]]:
        """`{editable name -> [source-relative paths]}` for the file the eval `command` EXECUTES,
        when the argv names one AND that editable actually ships it. `{}` otherwise.

        This is what makes the Developer prompt's trust-boundary sentence true. The score STAGE was
        always protected; the FILE it runs was in the edit surface, so on a task with
        `protect: []` + `edit_surface: ["**/*"]` the Developer could — and did, on
        `runs/rubertlite-dr-unified-v6` — edit the scorer to retrain when it found no checkpoint.
        See `eval_entrypoint_unprotected` for what that cost.

        THE FILE MUST EXIST IN THE SOURCE, and that condition is the whole safety argument, not an
        optimization. A repo task's normal shape is that the Developer AUTHORS the eval entrypoint
        the operator's cmd names; protecting a path that is not there would refuse the one write
        that makes such a task runnable, and `_unrunnable_protected_scripts` would then refuse every
        node for a file nothing can create. Existing-in-the-source is exactly the line between "the
        operator's scorer" and "the file the agent is supposed to write".

        The result is folded into the owning editable's `protect`, NOT appended to
        `_protected_names()` directly, because `protect` is read by two consumers that must agree:
        the write gates (via `_protected_names`) and `engine/workspace.py::seed_protected_files`,
        which materializes protected files into every node workdir whatever the seed mode says. An
        entrypoint frozen for writes but absent from the workdir under `seed_mode="auto"` is the
        failure `tests/test_repo_protected_seed.py` was written for; routing through `protect` means
        it cannot come back through this door.

        Scope: the entrypoint FILE only — deliberately NOT what it imports. A scorer's import
        closure is unbounded (it is the repo's own model/data/config modules; on the live task
        `vectorsearch.test` reaches most of `vectorsearch/`), so freezing it would freeze the repo
        and leave the Developer nothing to change — the adapter would stop being able to run an
        experiment at all. The residual hole is real and is stated rather than papered over: a
        scorer that reads its checkpoint path from an editable config can still be pointed
        somewhere else.

        What closes THAT is `runtime/read_fence.py`. This docstring used to name the stage `expect`
        contract and the Developer prompt instead, and both were measured NOT to close it: `expect`
        checks what a stage WRITES and never what it READS, so `runs/rubertlite-dr-unified-v6`
        node 4 passed its artifact contract while scoring a HUMAN's checkpoint that an absolute path
        in exactly such an editable config named (its own train.log records RECALL@100 0.726; the
        score.log the run recorded, 0.225) — and the prompt-level advisory that fires on that very
        edit is in the node's `spans.jsonl` verbatim and was committed over anyway. The fence is a
        read-side mechanism rather than a wider protect list precisely because the protect list is
        the thing this docstring explains cannot be widened.
        """
        if self.eval is None or not self.eval.protect_entrypoint:
            return {}
        cands = entrypoint_candidates(self.eval.command)
        if not cands:
            return {}
        # The argv is resolved relative to the eval `cwd` (the command's working directory, and for
        # `-m` also sys.path[0]); `protect` names are relative to the OWNING editable's root. Same
        # join `_eval_protected` makes on the other side of the same namespace.
        cwd = self._normp((self.eval.cwd or ".").strip()).strip("/")
        pre = "" if cwd in (".", "") else cwd + "/"
        out: dict[str, list[str]] = {}
        for cand in cands:
            ws = self._normp(pre + cand)
            # Named mounts first: a named editable owns `name/...` outright, and only what no named
            # mount claims can belong to the root repo (the same ownership rule `_in_surface` uses).
            for ed in sorted(mounts, key=lambda m: m["name"] in (".", "")):
                nm = str(ed["name"]).rstrip("/")
                if nm in (".", ""):
                    rel = ws
                elif ws.startswith(nm + "/"):
                    rel = ws[len(nm) + 1:]
                else:
                    continue
                try:
                    exists = (Path(ed["path"]) / rel).is_file()
                except OSError:            # unreadable source -> nothing we can claim to protect
                    exists = False
                if exists:
                    out.setdefault(ed["name"], []).append(rel)
                    break                  # one file, one owner
        return out

    def _editable_mounts(self) -> list[dict]:
        """`_declared_editable_mounts` plus the DERIVED protection of the eval entrypoint (see
        `_entrypoint_protect`), folded into the owning repo's `protect` so every consumer of a
        mount — the write gates, the agent brief, workspace seeding — sees one list and cannot
        disagree about what the operator owns."""
        mounts = self._declared_editable_mounts()
        derived = self._entrypoint_protect(mounts)
        for ed in mounts:
            ed["protect"] = ed["protect"] + [p for p in derived.get(ed["name"], [])
                                             if p not in ed["protect"]]
        return mounts

    def eval_spec(self) -> dict:
        return self.eval.model_dump() if self.eval else {}

    def onboarder_llm_roles(self, settings) -> frozenset[str]:
        """Pure consumer declaration matching `make_onboarder`'s activation gate exactly."""
        return (frozenset({"developer"})
                if self.onboard and getattr(settings, "backend", "toy") == "llm"
                else frozenset())

    def make_onboarder(self, settings):
        """Build the onboarder (Phase 3) when `onboard` is set and a live LLM is available;
        otherwise None (offline runs inject one in tests, or use an explicit eval)."""
        if not self.onboard or settings.backend != "llm":
            return None
        from looplab.adapters.tasks import make_llm_client
        from looplab.core.llm import make_llm_client_for
        from looplab.core.prompts import PromptStore
        repo_path = self.editable_path or (self.editables[0].path if self.editables else "")
        return LLMOnboarder(make_llm_client_for(
            settings, role="developer", factory=make_llm_client), repo_path, self.goal,
                            self.direction, self.onboard_command, self.onboard_timeout,
                            prompts=(PromptStore(settings.prompt_dir)
                                     if getattr(settings, "prompt_dir", None) else None))

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
        the agent edits.

        The "every reader the eval uses" walk is `EvalSpec.readers()` — the one enumeration, so this
        list and the submit-time path check (`_readers_usable`) cannot come to disagree about which
        slots exist. `str(path)` because a non-string `path` reaches here on the RELOAD path
        (grandfathered, see `_grandfathered`) and `pre + 123` used to raise TypeError out of
        `repo_spec()`, i.e. out of `Engine.__init__` — a crash instead of a resumable run."""
        if not self.eval:
            return []
        cwd = self._normp((self.eval.cwd or ".").strip()).strip("/")
        pre = "" if cwd in (".", "") else cwd + "/"
        out: list[str] = []
        for _label, _slot, reader in self.eval.readers():
            kind = reader.get("kind", "")
            if (kind.startswith("file_") or kind == "adapter") and reader.get("path"):
                out.append(self._normp(pre + str(reader["path"])))
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
        # Defense-in-depth (the DataSpec validator already coerces mount:true+edit:true → mount:false): a
        # MOUNTED source is a read-only symlink to the original, so the agent's build-time writes under
        # ./<name> can't reach it anyway (the materializer drops workdir escapes) — protect it so the write
        # TOOL refuses VISIBLY instead of silently no-opping. A `mount:false` source is a PHYSICAL per-node
        # copy the brief calls "a writable copy" (copy+modify/preprocess/extend) — protecting it made
        # copy-in mode unusable (every write under ./<name> was refused), so only mounts guard.
        for name, spec in self.data.items():
            if spec.mount:
                nm = name.rstrip("/")
                names += [self._normp(nm), self._normp(nm + "/**")]
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
        # A stages-only cmd (the composable pipeline form) has no single command line to show —
        # render the declared stage chain instead of an empty ``.
        if self.eval and self.eval.command:
            cmd = " ".join(self.eval.command)
        elif self.eval and self.eval.stages:
            cmd = ("the operator-declared stage pipeline: "
                   + " → ".join(str(s.get("name", "?")) for s in self.eval.stages))
        else:
            cmd = "(proposed during onboarding)"
        setup = " ".join(self.eval.setup) if (self.eval and self.eval.setup) else ""
        deps = (f" Dependencies are installed before each eval by `{setup}`, so to add a "
                "package, edit the requirements file it reads (if it is in your allowed "
                "paths).") if setup else self._declared_deps_brief()
        return (f"You are improving an existing experiment repository to {goal} the eval "
                f"metric. Goal: {self.goal}\n"
                f"You may ONLY edit files matching: {surf}. Do NOT modify (the operator "
                f"runs the evaluation): {prot}. The eval is run as: `{cmd}`.{deps}"
                f"{self._data_brief()} Make one "
                f"focused change to make the eval succeed and improve the metric, then stop.")

    def _declared_deps_brief(self) -> str:
        """The dependency sentence for a repo whose deps LoopLab installs itself.

        Two facts the agent cannot discover and repeatedly got wrong without them. (1) The repo's
        declared versions ARE installed now — before this, a run's environment was whatever the
        crash-time bare-name installer happened to resolve, so the Developer was routinely writing
        code against a library version the repo never asked for and then "fixing" the repo to match
        it. On `runs/rubert-dr-0807` that migration ran the wrong way for 7 of node 0's 12 repair
        attempts. (2) Whether it may CHANGE them. The direct `RepoTask` edit_surface default is
        `["**/*.py"]`, which does not match `requirements.txt`, while the composable `repo:` default
        is `["**/*"]`, which does — so the same sentence is true for one task and a lie for the
        other. Telling an agent to edit a file the write gate will refuse costs a whole repair
        attempt on the refusal, which is exactly what the old sentence's "(if it is in your allowed
        paths)" parenthetical was papering over.

        Silent when the repo declares nothing we act on: an absent sentence is correct there, and a
        "there is no requirements.txt" sentence would invite the agent to create one."""
        from looplab.runtime import deps
        from looplab.tools.patch import SurfacePolicy
        mounts = self._editable_mounts()
        if not mounts:
            return ""
        ed = mounts[0]
        decl = deps.find_declaration(ed["path"])
        if not decl.found:
            return ""
        pre = "" if ed["name"] in (".", "") else ed["name"].rstrip("/") + "/"
        rel = pre + decl.filename
        # Ask the REAL write gate, not a glob comparison of our own: `SurfacePolicy` is what will
        # actually refuse the agent's write, and a second implementation of "is this in the surface"
        # is how a brief starts promising something the gate denies. Constructed exactly as
        # `RepoWriteTools._gate` constructs it (`protected_exact=True`, `check_escapes=False`,
        # namespaced prefixes) — the same object with different flags answers a different question.
        surf: list[str] = []
        prefixes: list[str] = []
        for m in mounts:
            p = "" if m["name"] in (".", "") else m["name"].rstrip("/") + "/"
            surf += [p + g for g in m["surface"]]
            if p:
                prefixes.append(p.rstrip("/"))
        writable = SurfacePolicy(surf, self._protected_names(), prefixes,
                                 protected_exact=True, check_escapes=False).check(rel) is None
        if writable:
            return (f" Dependencies come from `{rel}`, which is installed for you before the eval — "
                    "the versions it pins are the versions you have. To use a new library or a "
                    f"different version, edit `{rel}`; it is re-installed when you change it.")
        return (f" Dependencies come from `{rel}`, which is installed for you before the eval — the "
                "versions it pins are the versions you have. You may NOT edit it, so write code "
                "against those versions rather than migrating to a newer API.")

    def _data_brief(self) -> str:
        """Tell the agent what it may do with each mounted data source (per-source permissions)."""
        if not self.data:
            return ""
        parts = []
        for name, s in self.data.items():
            where = f"./{name}" + (" (read-only mount)" if s.mount else " (a writable copy)")
            allow = [w for w, on in (("edit-in-place", s.edit), ("copy+modify", s.copy_modify),
                                     ("preprocess/augment", s.preprocess), ("extend", s.extend)) if on]
            # The "don't edit" warning applies only to a MOUNTED original: a mount:false copy is
            # node-local and writable (matching _protected_names, which no longer guards copies). A
            # mounted source is always edit:false (mount+edit is coerced to a copy), so `not s.mount`
            # alone decides — a writable copy needs no warning.
            deny = ("" if not s.mount
                    else " — do NOT edit the original; write any derived data elsewhere in the workdir")
            parts.append(f"{where}: may {', '.join(allow) or 'read'}{deny}")
        return " Data inputs: " + "; ".join(parts) + "."

    def repo_spec(self) -> dict:
        mounts = self._editable_mounts()
        surface: list[str] = []
        for ed in mounts:
            pre = "" if ed["name"] in (".", "") else ed["name"].rstrip("/") + "/"
            surface += [pre + g for g in ed["surface"]]
        # A WRITABLE data source must be editable regardless of the repo's edit_surface, else a narrow
        # surface like ["src/**"] refuses writes the brief promised. The only agent-writable data is a
        # `mount:false` per-node COPY (the agent may "copy+modify, preprocess, extend" it); a mounted
        # source is a read-only symlink the agent can't write. This is the exact COMPLEMENT of
        # `_protected_names` (which guards every mount), so each source is either surfaced or protected.
        # Surface BOTH the bare name and the `/**` tree: a single-FILE copy materializes at ./<name>
        # (not ./<name>/…), and the surface matcher has no "dir/** matches dir" special case (that lives
        # on the protect side only), so `name/**` alone would refuse writes to a file-typed copy.
        for name, spec in self.data.items():
            if not spec.mount:
                nm = name.rstrip("/")
                surface += [nm, nm + "/**"]
        return {
            "editables": mounts,                         # Phase 4: every editable repo + mount
            "edit_surface": surface,                     # namespaced union over all repos
            "protected_names": self._protected_names(),
            "references": [r.model_dump() for r in self.references],
            "data": {name: spec.model_dump() for name, spec in self.data.items()},
            # Back-compat single-seed hint (first editable): consumers that still seed one dir.
            "editable_path": mounts[0]["path"] if mounts else "",
        }

    def _bounds(self) -> dict:
        return {k: (float(lo), float(hi)) for k, (lo, hi) in self.params.items()}

    def build_roles(self):                     # offline: no edits (baseline / param search)
        if self.params:                        # cli_overrides hyperparameter search
            profile_names = self.eval.profiles.keys() if self.eval is not None else ()
            return (RepoParamResearcher(
                self._bounds(), seed=self.seed, eval_profiles=profile_names), NoOpRepoDeveloper())
        return (RepoResearcher(seed=self.seed), NoOpRepoDeveloper())

    def _eval_profile_researcher_hint(self) -> str:
        """Render only the named eval profiles this task can actually execute.

        ``Idea.eval_profile`` is a real command-dispatch input, but the generic Researcher cannot
        advertise it: most task adapters do not have named eval profiles at all.  Keep the contract
        task-local and derive every selectable name from the operator-owned ``EvalSpec`` so a task
        with ``preview``/``audit`` profiles is never prompted with conventional-but-absent
        ``smoke``/``full`` names.

        Unknown names remain tolerated at the durable/runtime boundary for compatibility (where
        ``build_command`` deliberately resolves them to the base command), but they are not part of
        the proposal action space and therefore are explicitly discouraged here.
        """
        if self.eval is None or not self.eval.profiles:
            return ""

        from looplab.runtime.sandbox import MAX_TIMEOUT_S, finite_timeout

        rows: list[str] = []
        for name, spec in self.eval.profiles.items():
            details: list[str] = []
            overrides = spec.get("overrides")
            if overrides:
                details.append("appends argv overrides " + json.dumps(
                    overrides, ensure_ascii=False, separators=(",", ":"), default=str))
            else:
                details.append("uses the base argv")
            # This is the same normalization/cap used by build_command, so the Researcher sees the
            # budget it will actually receive rather than an ineffective over-cap declaration.
            timeout = finite_timeout(spec.get("timeout", self.eval.timeout), 600.0)
            details.append(f"effective runtime timeout {timeout:g} seconds")
            rows.append(f"- {json.dumps(name, ensure_ascii=False)}: " + "; ".join(details))

        default = (f"omitting it (or emitting null) selects the declared "
                   f"{json.dumps('smoke')} profile"
                   if "smoke" in self.eval.profiles
                   else "omitting it (or emitting null) uses the base eval command and timeout")
        return ("\nEvaluation profile contract (operator-owned): set the optional `eval_profile` "
                "field only to one of these exact declared names:\n"
                + "\n".join(rows)
                + f"\nFor search, {default}. An unknown explicit name safely uses the base eval "
                  "command and timeout, but is not a valid named profile: never invent one. A "
                  "profile changes only the declared eval argv/timeout, not the objective or your "
                  f"edit permissions. Reported timeouts are effective after the runtime cap of "
                  f"{MAX_TIMEOUT_S:g} seconds.")

    def llm_roles(self, client: LLMClient, parser: str = "tool_call"):
        """When `params` is set: an LLM hyperparameter proposer over the bounds (framework
        cli_overrides mode). Otherwise a free-form code-edit proposer; the editing agent is
        wired in make_roles from repo_spec, and this NoOp developer is the validator's
        baseline fallback."""
        if self.params:
            goal = "maximize" if self.direction == "max" else "minimize"
            hint = (f"Propose hyperparameters to {goal} the eval metric, within bounds: "
                    + ", ".join(f"{k} in [{lo}, {hi}]" for k, (lo, hi) in self._bounds().items())
                    + self._eval_profile_researcher_hint())
            return (LLMResearcher(client, space_hint=hint, bounds=self._bounds(),
                                  parser=parser), NoOpRepoDeveloper())
        hint = ("You are improving an existing experiment repository. Propose the next "
                "concrete change to try (as a short rationale); leave params empty unless "
                "the experiment exposes numeric knobs."
                + self._eval_profile_researcher_hint())
        return (LLMResearcher(client, space_hint=hint, bounds=None, parser=parser),
                NoOpRepoDeveloper())

    def external_fallback_uses_llm(self) -> bool:
        return False  # validation deliberately falls back to the unmodified repo baseline


# Back-compat: the Developer half (RepoWriteTools, the in-house LLM developer, onboarding, the
# xlsx renderer) moved to adapters/repo_developer.py (BACKLOG §4 "repo_task split"). Re-imported
# HERE, at the END of the module — after every spec/task definition above is bound — so existing
# imports from `looplab.adapters.repo_task` (and the flat `looplab.repo_task` alias) keep
# resolving, without a circular-import trap if repo_developer ever imports a spec from here.
from looplab.adapters.repo_developer import (  # noqa: E402,F401
    LLMOnboarder, LLMRepoDeveloper, RepoWriteTools, _xlsx_to_markdown,
)
