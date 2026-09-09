"""TaskAdapter seam (ADR-2) + a loader for tasks.

The agent/role composition root that used to live in the second half of this file — `make_roles`,
`build_unified_agent`, `_shared_providers`, `build_strategist_tools`, `make_developer_factory` and
their helpers — is now `looplab/agents/factory.py` (doc 25 RA-01). It is re-exported below so every
existing `from looplab.adapters.tasks import make_roles` keeps working, exactly as `make_llm_client`
is re-exported from `core/llm.py`.

A task is COMPOSABLE: `normalize_task` infers the adapter from which capability fields are present
(`repo`/`dataset`/`cmd`/`kaggle`/`benchmark`, with `metric.reader`) rather than a `kind` enum, and maps
them onto the registered adapters — while still accepting the legacy `kind`/`eval`/`onboard`/
`editable_path`/`metric.kind` spelling verbatim (so old snapshots/task files keep working). That
front-end is now `looplab/adapters/task_schema.py` — a dict in, a canonical dict out, no adapter and
no I/O — and is imported (and re-exported) below, so `from looplab.adapters.tasks import
normalize_task` still names the SAME object. Any object exposing `id`, `goal`, `direction`, and
`build_roles()` is a valid task; optionally `columns()` enables the grounding/profiling pre-phase.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from looplab.adapters.mlebench import MLEBenchTask
from looplab.adapters.mlebench_real import MLEBenchRealTask
from looplab.adapters.classification import ClassificationTask
from looplab.adapters.dataset_task import DatasetTask
from looplab.adapters.regression import CodeRegressionTask, RegressionTask
from looplab.adapters.repo_task import RepoTask
from looplab.adapters.task_schema import normalize_task  # noqa: F401 — also a re-export
from looplab.agents.roles import Developer, Researcher
from looplab.adapters.timeseries import TimeSeriesTask
from looplab.adapters.toytask import ToyTask


@runtime_checkable
class TaskAdapter(Protocol):
    """The task seam (ADR-2). REQUIRED surface: `id`, `goal`, `direction` ("min"/"max") and
    `build_roles()` — the members declared below.

    Beyond that, consumers duck-type a set of OPTIONAL hooks (probed with `getattr`/`callable`,
    so an adapter implements only what applies). They are documented here — NOT declared as
    Protocol members, so the `isinstance`/structural check stays exactly "the required four":

    - `llm_roles(client, *, parser=..., runtime_caps=...) -> (Researcher, Developer)` — LLM-backed
      roles; called by `make_roles` (this module) when backend="llm". `core/hardware.py`
      (`task_runtime_caps`) inspects its signature: accepting `runtime_caps` opts the task into
      the torch/GPU capability brief; `external_fallback_uses_llm()` declares its fallback consumer.
    - `assets() -> dict[str, str]` — {filename: content} staged into each eval workdir; consumed by
      `engine/orchestrator.py` (staging + protected from edits). (Every implementation returns a dict
      and the engine indexes it as one — the contract of record is the dict, not the old `list[str]`.)
    - `columns() -> dict` — tabular schema/profile; consumed by `engine/orchestrator.py` (I1
      grounding pre-phase) and `tools/run_tools.py` (`DataTools`).
    - `leakage_inputs() -> dict` — split/timestamp info for the leakage audit; consumed by
      `engine/orchestrator.py`.
    - `shift_inputs() -> dict` — `{"reference": {col: values}, "current": {col: values}, "source":
      str}`: the training sample beside the deployment one, for the ADVISORY distribution-shift
      record (`trust/drift.py`); consumed by `engine/audit.py`. `{}` means "no comparable pair was
      declared", which is recorded as such. An adapter without it falls back to the train/test rows
      `leakage_inputs()` already publishes, so most tasks need not implement it.
    - `host_grader() -> dict` — out-of-process grading spec (labels/grader run host-side, outside
      the sandbox); consumed by `engine/orchestrator.py`.
    - `data_samples() -> dict[str, str]` — raw data samples for tasks that read data by absolute
      path; consumed by `tools/run_tools.py` (`DataTools` fallback).
    - `repo_spec() -> dict` — RepoTask workspace spec (editables/references/protected_names);
      consumed by `engine/orchestrator.py` and `make_roles` (this module).
    - `agent_brief() -> str` — the coding-agent task brief; consumed by `make_roles` (this
      module) and `adapters/repo_task.py` (`LLMRepoDeveloper`).
    - `eval_spec() -> dict` — the operator's trusted eval command/metric; consumed by
      `engine/orchestrator.py` (via `runtime/command_eval.py`).
    - `make_onboarder(settings)` — Repo onboarding; pure companion `onboarder_llm_roles(settings)`
      declares its strict in-process targets without constructing it; both are consumed at run start.
    - `params` (attribute) — CLI-override param space; read by `make_roles` (this module,
      the param-search guard) and `runtime/command_eval.py` (params_style="cli_overrides").
    - `comparison_contract` (attribute) — optional typed scientific comparability identity;
      persisted by launch surfaces and consumed only by cross-run reporting. Third-party adapters
      that do not opt in remain valid TaskAdapter implementations and produce unranked observations.
    - `gpu_capable() -> bool` — whether this task's solution code can use a GPU AT ALL; consumed by
      `engine/resources.py` (`_task_gpu_capable`) as the second input to an UNSPECIFIED footprint.
      ABSENT MEANS CAPABLE: only an adapter that positively returns False opts out, so a third-party
      adapter (or one that has simply never thought about it) keeps the historical reserve-first
      behaviour. Returning False is a claim that no candidate this adapter can produce touches CUDA —
      the shipped synthetic/offline tasks qualify because their Developer is a numpy/stdlib template
      or their brief forbids anything else. Getting it wrong in the False direction lets two
      co-hosted runs double-book a device, so declare it only where the whole task family is provably
      CPU-locked; getting it wrong in the True direction only costs the historical blocking.
    """
    id: str
    goal: str
    direction: str

    def build_roles(self) -> tuple[Researcher, Developer]: ...


# The optional-hook REGISTRY (docs/15 §P4.2): the machine-checked twin of the docstring above.
# `tests/test_task_adapter_contract.py` source-scans every consumer package for
# `getattr(task, "<name>")` / `getattr(self.task, "<name>")` probes and asserts BOTH directions:
# a probe for a name not listed here is a typo'd/undeclared hook (red test), and a listed hook
# with no remaining consumer is registry rot (red test). Renaming a hook on one side alone —
# the historical "the run silently stages/scores nothing" failure — is now a test failure.
TASK_OPTIONAL_HOOKS: tuple[str, ...] = (
    "llm_roles", "assets", "columns", "leakage_inputs", "shift_inputs", "host_grader", "data_samples",
    "repo_spec", "agent_brief", "eval_spec", "make_onboarder", "onboarder_llm_roles", "params",
    "comparison_contract", "external_fallback_uses_llm",
    # Scheduler-facing capability declaration probed by engine/resources.py — registered so an
    # adapter that renames it goes red instead of silently re-acquiring the host GPU pool lease.
    "gpu_capable",
    # RepoTask-specific field probed by the repo Developer's onboarding flow
    # (adapters/repo_developer.py) — registered so a one-sided rename goes red like any hook.
    "onboard_command")


_KINDS = {"quadratic": ToyTask, "regression": RegressionTask,
          "code_regression": CodeRegressionTask, "mlebench": MLEBenchTask,
          "mlebench_real": MLEBenchRealTask,
          "repo": RepoTask, "timeseries": TimeSeriesTask,
          "classification": ClassificationTask, "dataset": DatasetTask}


def kinds() -> list[str]:
    """The registered task kinds (for UI/validation — e.g. the genesis flow checks an inline task's
    kind before materializing it)."""
    return list(_KINDS)


def kinds_for(task_cls: type) -> frozenset[str]:
    """The registered kind names that materialize EXACTLY `task_cls`.

    One question, asked at two different times. `cli/__init__.py::_engine` narrows the calibrated
    speculation lane with `type(task) is ToyTask` — it has the task object. `cli/run_cmds.py`'s
    receipt gate has to ask the same question ~60 lines EARLIER, before `validate_task` has run, so
    all it can read is the `kind` string. Deriving that answer from `_KINDS` rather than writing
    `"quadratic"` at the call site is what keeps the two halves one rule: renaming a kind moves both,
    and a kind that stops mapping to ToyTask stops matching here too. Hand-copying the literal is how
    the early gate silently comes to match nothing while the late gate still works.
    """
    return frozenset(name for name, cls in _KINDS.items() if cls is task_cls)


def validate_task(data: dict, *, existing_run: bool = False) -> TaskAdapter:
    """Build + validate a task adapter from an in-memory dict (the inline-task / genesis path). Raises
    on an unknown kind OR a kind-specific validation failure (e.g. mlebench_real resolving an unknown
    competition slug) — the SAME validation the engine runs at startup, so callers can reject a bad
    spec synchronously instead of spawning a detached engine that dies before writing any events.

    `existing_run=True` marks this as a RE-load of a run that already has history — `resume`,
    `finalize` and read-only inspection rebuild the task from the verbatim `task.snapshot.json` the
    run was started with. It reaches the models as pydantic validation CONTEXT (propagated to nested
    models), where a validator added AFTER that snapshot was written can decline to retroactively
    invalidate it; see `adapters/repo_task.py::_grandfathered` for the one rule that uses it and why.
    The default is the STRICT path, so a new submit surface is fail-closed by omission and only the
    reload sites opt out."""
    data = normalize_task(data)                       # composable/legacy schema -> canonical + inferred kind
    kind = data["kind"]                               # normalize_task guarantees it (or raises)
    cls = _KINDS.get(kind)
    if cls is None:
        raise ValueError(f"unknown task kind: {kind!r} (known: {sorted(_KINDS)})")
    adapter = cls.model_validate(data, context={"existing_run": bool(existing_run)})
    contract = getattr(adapter, "comparison_contract", None)
    if contract is not None and contract.direction != adapter.direction:
        # direction is part of both execution and comparison semantics.  A mismatch
        # cannot be resolved later without silently reversing a ranking, so every launch surface
        # rejects it at the shared task-validation boundary.
        raise ValueError(
            "comparison_contract.direction must match the task direction "
            f"({contract.direction!r} != {adapter.direction!r})")
    # The engine's startup invariant (orchestrator.__init__), pulled UP to submit time so /api/start
    # rejects a bad repo spec with a 400 (and the assistant re-proposes) instead of spawning a detached
    # engine that dies before writing any events — the "click Start, then GET events → 404" trap. Kept
    # here (not on the RepoTask model) so unit tests can still construct a partial RepoTask.
    if kind == "repo" and getattr(adapter, "eval", None) is None and not getattr(adapter, "onboard", False):
        raise ValueError(
            "A repo task has no `cmd` and no auto-metric — every node would be scored with no real "
            "evaluation. Either give a `cmd` (a command + a metric to read), OR set the metric "
            "reader to \"auto\" (with backend=llm) so an onboarder builds the eval entrypoint first.")
    return adapter


def submit_warnings(adapter) -> tuple[str, ...]:
    """Everything a VALIDATED task earns at submit that must NOT stop the launch.

    ONE rule, because a submit-time warning is worthless on the surface that does not print it. The
    two below were spelled twice — `serve/launch.py::preflight_start` (which puts them on
    `LaunchPreflight.warnings`, so `/api/start/preflight` and `/api/validate` return them) and
    `cli/run_cmds.py::_report_task_warnings` (which echoes them to stderr) — and the CLI's copy was
    written by hand from the server's, which is how a warning gets added on one surface only
    (doc 27, `three-new-run-planners-no-shared-schema`).

    Both are about the eval `command`'s argv, both are advisory by design, and both are TOTAL over an
    injected dict `adapter`: the helpers isinstance-check a RepoTask and return [] otherwise.

      * `eval_entrypoint_unprotected` — the scorer LoopLab cannot protect. The Developer's prompt
        tells it the scoring cannot be rewritten; when the argv names no in-repo file, nothing
        enforces that, and the same gap cost `runs/rubertlite-dr-unified-v6` 2x GPU per node before
        anyone looked.
      * `eval_source_tree_command_paths` — docs/29 F1c's third piece: an argv token naming the
        editable SOURCE tree absolutely reaches the operator's original rather than the node's copy,
        so no node's edits to it ever take effect.

    What is NOT here is the missing-input-path warning: the launch funnel FAILS CLOSED on a task path
    it cannot stat (`serve/launch.py::_validated_path_fingerprints`), so on that surface the same
    condition is a refusal, not a warning. A rule that means two different things is not one rule.
    """
    from looplab.adapters.repo_task import (eval_entrypoint_unprotected,
                                            eval_source_tree_command_paths)
    return (*eval_entrypoint_unprotected(adapter), *eval_source_tree_command_paths(adapter))


def load_task(path: str | Path, *, existing_run: bool = False) -> TaskAdapter:
    # Accepts a bare task file (legacy JSON, or YAML) OR a unified config file — in which case only
    # its `task:` block is validated here (the engine settings are read separately by the CLI). The
    # reader handles JSON/YAML and a BOM from Windows editors.
    # `existing_run` forwards to validate_task: pass it when `path` is (or stands in for) a run's own
    # `task.snapshot.json`, so re-entering an existing run can't be refused by a rule added later.
    from looplab.core.appconfig import load_document
    task, _settings, _out = load_document(Path(path))
    return validate_task(task, existing_run=True) if existing_run else validate_task(task)


# Re-export: the factory moved to its dependency-true home (core/llm.py — it only ever needed
# core symbols). Kept importable here because dozens of call sites + tests spell
# `from looplab.adapters.tasks import make_llm_client`.
from looplab.core.llm import (  # noqa: E402,F401
    LlmTarget, client_kwargs_for, make_llm_client, make_llm_client_for,
    resolve_llm_target)

# Re-export: the agent/role composition root moved to `agents/factory.py` (doc 25 RA-01), which is
# its dependency-true home — it wires agents/search/tools and knows nothing about task schemas.
# Kept importable here because dozens of call sites + tests spell
# `from looplab.adapters.tasks import make_roles`.
from looplab.agents.factory import (  # noqa: E402,F401
    _agent_model, _make_abstractor, _memora_cache_path, _set_role_client, _shared_providers,
    build_strategist_tools, build_unified_agent, make_developer_factory, make_roles)
