"""TaskAdapter seam (ADR-2) + a loader for tasks.

The agent/role composition root that used to live in the second half of this file — `make_roles`,
`build_unified_agent`, `_shared_providers`, `build_strategist_tools`, `make_developer_factory` and
their helpers — is now `looplab/agents/factory.py` (doc 25 RA-01). It is re-exported below so every
existing `from looplab.adapters.tasks import make_roles` keeps working, exactly as `make_llm_client`
is re-exported from `core/llm.py`.

A task is COMPOSABLE: `normalize_task` infers the adapter from which capability fields are present
(`repo`/`dataset`/`cmd`/`kaggle`/`benchmark`, with `metric.reader`) rather than a `kind` enum, and maps
them onto the registered adapters — while still accepting the legacy `kind`/`eval`/`onboard`/
`editable_path`/`metric.kind` spelling verbatim (so old snapshots/task files keep working). Any object
exposing `id`, `goal`, `direction`, and `build_roles()` is a valid task; optionally `columns()` enables
the grounding/profiling pre-phase.
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
      the torch/GPU capability brief.
    - `assets() -> dict[str, str]` — {filename: content} staged into each eval workdir; consumed by
      `engine/orchestrator.py` (staging + protected from edits). (Every implementation returns a dict
      and the engine indexes it as one — the contract of record is the dict, not the old `list[str]`.)
    - `columns() -> dict` — tabular schema/profile; consumed by `engine/orchestrator.py` (I1
      grounding pre-phase) and `tools/run_tools.py` (`DataTools`).
    - `leakage_inputs() -> dict` — split/timestamp info for the leakage audit; consumed by
      `engine/orchestrator.py`.
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
    - `make_onboarder(settings)` — RepoTask Phase 3 onboarding proposer; consumed by `cli.py`.
    - `params` (attribute) — CLI-override param space; read by `make_roles` (this module,
      the param-search guard) and `runtime/command_eval.py` (params_style="cli_overrides").
    - `comparison_contract` (attribute) — optional typed scientific comparability identity;
      persisted by launch surfaces and consumed only by cross-run reporting. Third-party adapters
      that do not opt in remain valid TaskAdapter implementations and produce unranked observations.
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
    "llm_roles", "assets", "columns", "leakage_inputs", "host_grader", "data_samples",
    "repo_spec", "agent_brief", "eval_spec", "make_onboarder", "params",
    "comparison_contract",
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


def normalize_task(data: dict) -> dict:
    """Front-end for the COMPOSABLE task schema — the single place old and new spellings converge.

    A task is defined by WHICH capability fields it carries, not a `kind` enum:
      • `repo`     -> an editable codebase (agent edits within it)        [alias: editable_path]
      • `dataset`  -> read-only data mounts (path or {name: path})        [alias: data]
      • `cmd`      -> how to run + score (a command/argv OR a full spec)  [alias: eval]
      • `kaggle`   -> a Kaggle / MLE-bench competition slug               [-> kind=mlebench_real]
      • `benchmark`-> a built-in synthetic task (quadratic/regression/…)  [-> kind=<name>]
    and inside `cmd`/`eval`: `metric.reader` (alias of the old `metric.kind`); reader "auto" folds
    to the onboarding path (the agent writes the metric adapter).

    Returns a canonical dict the registered adapters validate (with an inferred `kind`). Idempotent
    and back-compatible: a legacy `{kind, eval, onboard, editable_path, metric.kind}` dict passes
    through unchanged, so old snapshots / example files / tests keep working. Raises ValueError on
    a task that cannot be a task — a non-argv `cmd`, or no recognizable capability field at all
    (never a silent default to the quadratic toy)."""
    d = dict(data)

    # --- built-in benchmarks: an explicit selector for the internal synthetic tasks ---
    if d.get("benchmark") and not d.get("kind"):
        d["kind"] = d.pop("benchmark")

    # --- kaggle competition -> the mlebench_real adapter (accept the `kaggle` alias or a bare
    #     `competition`; either one, with no explicit kind, IS a competition task) ---
    if d.get("kaggle"):
        # `kaggle` is the composable spelling and WINS over a stale `competition` riding along in the
        # same dict (setdefault kept the old value, so a user editing the Kaggle field in a boss-
        # authored spec launched the DISPLAYED slug's predecessor — the wrong competition).
        d["competition"] = d.pop("kaggle")
    if d.get("competition") and not d.get("kind"):
        d["kind"] = "mlebench_real"

    # --- repo (editable codebase): "do whatever WITHIN it" — default the surface to ALL files ---
    if "repo" in d:
        _repo = d.pop("repo")
        # CONFLICTING aliases are an ERROR, not silent-keep-the-old (arch-review §3 P0-5): {repo: NEW,
        # editable_path: OLD} used to keep OLD because `repo` was only consumed when editable_path was
        # absent — so a user who switched the repo via the composable alias silently ran the old one.
        if d.get("editable_path") and d["editable_path"] != _repo:
            raise ValueError(
                f"conflicting task aliases: repo={_repo!r} and editable_path={d['editable_path']!r} "
                "name different codebases — set exactly one.")
        # Spelling the SAME path under BOTH aliases is not a conflict (the check above passed), and
        # it must mean what `repo` alone means. Keying the composable default off "editable_path was
        # absent" made `{repo: p}` and `{repo: p, editable_path: p}` differ: the second skipped this
        # branch entirely and fell through to RepoTask's much narrower `["**/*.py"]`, so the same
        # task silently got a smaller edit surface for naming its repo twice.
        d["editable_path"] = _repo
        d.setdefault("edit_surface", ["**/*"])   # composable-repo default: full freedom (protect=exceptions)

    # --- dataset: read-only mounts. A bare path -> one mount named "dataset"; a dict -> merged ---
    if "dataset" in d:
        ds = d.pop("dataset")
        existing = d.get("data")
        mounts = dict(existing) if isinstance(existing, dict) else {}
        if isinstance(ds, str) and ds:
            name, i = "dataset", 2               # avoid clobbering an explicit `data` mount of the same name
            while name in mounts:
                name, i = f"dataset{i}", i + 1
            mounts[name] = ds
        elif isinstance(ds, dict):
            # A name spelled in BOTH `data` and `dataset` is a config error: mounts.update would
            # silently shadow one path and every node would evaluate against the wrong source, far
            # from the misconfiguration. (The bare-path branch above can rename because its name is
            # invented; an explicit name collision has no right answer.)
            clash = sorted(set(mounts) & set(ds))
            if clash:
                raise ValueError(
                    f"data mount name(s) declared in BOTH `data` and `dataset`: {', '.join(clash)} — "
                    "the same name would silently shadow one of the paths; rename or drop one side.")
            mounts.update(ds)
        if mounts:
            d["data"] = mounts

    # --- REJECT a stray dotted `cmd.<field>` / `eval.<field>` top-level key with an actionable error.
    #     The docs describe fields in dotted shorthand (`cmd.setup`, `cmd.profiles`, …) meaning "the
    #     field of cmd", and a model — the assistant's propose_run especially — sometimes emits them
    #     LITERALLY as top-level keys instead of nesting. Silently dropping them (the old behavior) lost
    #     the setup/profiles with no signal. Raising a clear message instead lets propose_run bounce it
    #     BACK to the assistant, which re-emits with the field nested — the model self-corrects rather
    #     than shipping a task whose setup never runs. ---
    _stray = sorted(k for k in d if isinstance(k, str)
                    and (k.startswith("cmd.") or k.startswith("eval.")) and len(k) > 4)
    if _stray:
        base, field = _stray[0].split(".", 1)
        raise ValueError(
            f"`{_stray[0]}` is not a valid field — write `{field}` INSIDE the `{base}` object, not as a "
            f"top-level \"{_stray[0]}\" key. e.g. cmd:{{command:[…], metric:{{…}}, {field}:…}}. "
            f"Stray dotted keys: {', '.join(_stray)}.")

    # --- cmd (how to run + score) is the new name for `eval` ---
    # Both present is an authoring conflict: mapping `cmd`->`eval` only "when eval is absent" would
    # SILENTLY drop the composable `cmd` in favor of a stale legacy `eval` (pydantic ignores the
    # leftover unknown `cmd` key) — the exact silent-loss the data/dataset clash check guards against.
    if "cmd" in d and "eval" in d:
        raise ValueError(
            "give EITHER `cmd` (the current name) OR `eval` (the legacy alias) — not both; they are "
            "the same field and specifying both is ambiguous. Keep `cmd` and drop `eval`.")
    if "cmd" in d and "eval" not in d:
        cmd = d.pop("cmd")
        if isinstance(cmd, list):
            d["eval"] = {"command": list(cmd)}
        elif isinstance(cmd, dict):
            d["eval"] = dict(cmd)
        elif cmd:
            # The natural authoring mistake is a shell STRING — dict("python test.py") would raise a
            # cryptic 'dictionary update sequence' ValueError (a 500 on /api/start, a TUI crash).
            # Reject it with an actionable message instead; the engine runs argv with NO shell.
            raise ValueError(
                f"`cmd` must be an argv list ([\"python\",\"test.py\"]) or a spec object "
                f"{{command, metric, timeout}}, got {type(cmd).__name__}: {str(cmd)[:80]!r} — "
                "split a shell string into argv items.")
        # falsy cmd (None/""/{}) -> treated as absent; the repo-task gate below gives the real message

    # --- inside the eval/cmd spec: metric.reader alias + "auto" -> onboarding fold ---
    def _reader_to_kind(spec):
        # A metric-reader dict may spell its reader as `reader` (composable) — map to the engine's
        # `kind`. Applied to EVERY reader (primary metric, multi-objective `metrics`, `constraints`,
        # `cross_check`), so a `reader:`-spelled sub-reader isn't silently read as stdout_json.
        if isinstance(spec, dict) and "reader" in spec and "kind" not in spec:
            spec = dict(spec)
            spec["kind"] = spec.pop("reader")
        # A regex reader needs its regex in `pattern`; an LLM/operator authoring the composable metric
        # naturally puts it in `key` (the field stdout_json/file_json use). Promote key->pattern for regex
        # readers so `{"reader":"stdout_regex","key":"RECALL@100: (...)"}` works instead of crashing the
        # eval with KeyError('pattern').
        if isinstance(spec, dict) and spec.get("kind") in ("stdout_regex", "file_regex") \
                and "pattern" not in spec and spec.get("key"):
            spec = dict(spec)
            spec["pattern"] = spec.pop("key")
        return spec

    ev = d.get("eval")
    if isinstance(ev, dict):
        ev = dict(ev)
        m = ev.get("metric")
        if isinstance(m, dict) and m.get("reader") == "auto":
            # "auto" reader == the agent writes the metric adapter -> the onboarding path. The command
            # becomes the onboard command; `eval` is left None until the onboarder ratifies.
            d.setdefault("onboard", True)
            # A string `command` here would `list(...)` into a per-CHARACTER argv (['p','y','t',…]).
            # The non-auto path is guarded by EvalSpec validation, but this onboard fold sets eval=None
            # and bypasses it — so reject a shell string exactly like the top-level `cmd` branch does.
            # A non-empty string raises; a whitespace-only/empty string is treated as ABSENT (else it
            # would slip past the `.strip()` check yet still be truthy at `if _cmd`, becoming list(' ')).
            _cmd = ev.get("command")
            if isinstance(_cmd, str):
                if _cmd.strip():
                    raise ValueError(
                        "`cmd.command` must be an argv list ([\"python\",\"test.py\"]), not a shell string: "
                        f"{_cmd[:80]!r} — split it into argv items.")
                _cmd = None
            if _cmd and not d.get("onboard_command"):
                d["onboard_command"] = list(_cmd)
            if ev.get("timeout"):
                d.setdefault("onboard_timeout", float(ev["timeout"]))
            d["eval"] = None
            ev = None
        else:
            if isinstance(m, dict):
                ev["metric"] = _reader_to_kind(m)
            if isinstance(ev.get("metrics"), dict):          # multi-objective aux readers
                ev["metrics"] = {k: _reader_to_kind(v) for k, v in ev["metrics"].items()}
            if isinstance(ev.get("constraints"), list):      # constraint readers
                ev["constraints"] = [_reader_to_kind(c) for c in ev["constraints"]]
            if isinstance(ev.get("cross_check"), dict):      # drift cross-check reader
                ev["cross_check"] = _reader_to_kind(ev["cross_check"])
        if ev is not None:
            d["eval"] = ev

    # --- infer the dispatch kind from the fields present (composable -> kind) ---
    if not d.get("kind"):
        if d.get("editable_path") or d.get("editables"):
            d["kind"] = "repo"
        elif d.get("eval") or d.get("onboard"):
            d["kind"] = "repo"            # a bare run+score spec is a (path-less) repo-style task
        elif d.get("data") or d.get("data_path"):
            d["kind"] = "dataset"
        elif d.get("bounds"):
            # the classic kind-less TOY file (examples/toy_task.json predates `kind`): a numeric
            # `bounds` space with no repo/data/cmd IS the toy capability — keep those loading.
            d["kind"] = "quadratic"
        else:
            # NO silent default to the quadratic toy (the guarantee the old /api/start kind-guard
            # gave): a typo'd capability field (repo_path for repo, …) would otherwise validate as a
            # ToyTask and burn the run's nodes/LLM budget optimizing (x-3)^2. An offline toy run says
            # so explicitly (`kind`/`benchmark`: "quadratic").
            raise ValueError(
                "cannot infer the task: no capability field recognized. Give one of `repo` (an "
                "editable codebase), `dataset`/`data` (data mounts), `cmd` (how to run + score), "
                "`kaggle`/`competition` (a competition slug), `benchmark` (a built-in synthetic "
                "task) — or an explicit legacy `kind`.")

    # Per-source permission OBJECTS ({path, mount, edit, …}) are repo-task machinery — mount/edit
    # drive the repo workspace seeding and the write gate. The dataset kind reads data by ABSOLUTE
    # path with no mounts (DatasetTask.data is name -> path), so only the path survives here:
    # flatten the documented object form instead of bouncing it with a pydantic type error.
    if d["kind"] == "dataset" and isinstance(d.get("data"), dict):
        d["data"] = {k: (v.get("path", "") if isinstance(v, dict) else v)
                     for k, v in d["data"].items()}

    return d


def validate_task(data: dict) -> TaskAdapter:
    """Build + validate a task adapter from an in-memory dict (the inline-task / genesis path). Raises
    on an unknown kind OR a kind-specific validation failure (e.g. mlebench_real resolving an unknown
    competition slug) — the SAME validation the engine runs at startup, so callers can reject a bad
    spec synchronously instead of spawning a detached engine that dies before writing any events."""
    data = normalize_task(data)                       # composable/legacy schema -> canonical + inferred kind
    kind = data["kind"]                               # normalize_task guarantees it (or raises)
    cls = _KINDS.get(kind)
    if cls is None:
        raise ValueError(f"unknown task kind: {kind!r} (known: {sorted(_KINDS)})")
    adapter = cls.model_validate(data)
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


def load_task(path: str | Path) -> TaskAdapter:
    # Accepts a bare task file (legacy JSON, or YAML) OR a unified config file — in which case only
    # its `task:` block is validated here (the engine settings are read separately by the CLI). The
    # reader handles JSON/YAML and a BOM from Windows editors.
    from looplab.core.appconfig import load_document
    task, _settings, _out = load_document(Path(path))
    return validate_task(task)


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
