"""The three DEVELOPER-BACKEND wirings the composition root composes (doc 25 RA-01).

`make_roles` picks the role backends from config, and three of its branches are not shared setup at
all — they are three different Developers, each with its own gate, its own constructor and its own
reason to be off:

* `in_house_repo_developer`   — the in-house `LLMRepoDeveloper` that edits a repo task's editables.
* `external_cli_developer`    — an external coding agent (ADR-7 presets), validated by
  `ValidatingDeveloper` when `validate_agent` is on.
* `best_of_n_developer`       — the C2 best-of-N wrap around whichever Developer survived the two
  above.

They interleaved with the provider/prompt setup rather than sitting as three separable blocks, which
is why RA-01's remaining half stayed open after the module split: the value is not the line count,
it is that each gate now has a NAME and one place to read it. Every gate returns `None` for "this
backend does not apply", so `make_roles` reads as three questions rather than three multi-clause
`if`s, and a gate can be tested on its own (`tests/test_developer_backend_wiring.py` drives all
three truth tables without building a role).

WHAT IS DELIBERATELY NOT HERE. The sweep offer (`_offer_sweep`), the PromptStore poke, the provider
assembly and the H3 per-role client rebinding stay in `make_roles`: they are SHARED setup that every
backend takes, and moving them would scatter the wiring instead of naming it.

LAYERING, the same rule `agents/providers.py` carries and for the same reason: every `agents`,
`search`, `tools` and `adapters` import below is FUNCTION-LOCAL. `search` imports `agents` at module
scope, so a module-level `looplab.search` import here would close the cycle into an ImportError at
startup — and `agents/factory.py` imports THIS module at module level (for the `_agent_model`
re-export identity `adapters/tasks.py` relies on), which is admissible only while that property
holds. `tests/test_agent_factory_split.py` asserts both halves.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from looplab.core.llm import resolve_llm_target, run_cost_accountant

if TYPE_CHECKING:                      # `adapters.tasks` re-exports through the factory, so a
    from looplab.adapters.tasks import TaskAdapter   # runtime import here would be a cycle.


def _agent_model(backend: str, model: str) -> str:
    """Map our model id to the agent's provider/model string for a local Ollama model."""
    if backend == "aider":
        return f"ollama_chat/{model}"   # aider's ollama provider id
    if backend in ("opencode", "goose", "continue"):
        return f"ollama/{model}"        # provider/model
    return model


def in_house_repo_developer(task: TaskAdapter, settings, client, *, param_search: bool,
                            established):
    """The in-house repo code-writer, or None when this run is not one.

    In-house repo code-writer: a RepoTask ships a NoOp in-house developer because repo editing was
    designed for external coding agents (opencode/aider/…). When none is configured, give the agent
    an in-house LLM developer that reads the repo + AUTHORS the files the eval needs (e.g. the eval
    entrypoint) within the surface, via the shared tool loop — so a repo task runs on JUST the
    in-house LLM. An external coding-agent preset (`external_cli_developer`) still takes precedence
    when requested, which is why the caller applies the two in that order.

    Returning None rather than the unchanged developer is what keeps the caller's `_handoff_dev`
    honest: this is the ONLY Developer that runs stages→plan→implement inside the node's handoff
    scope, so "did this backend apply?" and "does the Researcher owe a handoff brief?" are the same
    question and must not become two.
    """
    from looplab.agents.cli_agent import PRESETS

    if (settings.developer_backend in PRESETS
            or param_search
            or not callable(getattr(task, "repo_spec", None))
            or not task.repo_spec().get("editables")):
        return None
    from looplab.adapters.repo_task import LLMRepoDeveloper
    from looplab.tools.dev_commands import DeveloperCommandRuntime
    from looplab.agents.agent import loop_opts_from_settings as _loop_opts
    return LLMRepoDeveloper(  # C4: plan decomposition + hard per-session backstop
        client, task, parser=settings.llm_parser, loop_opts=_loop_opts(settings), established=established,
        plan_decompose=getattr(settings, "developer_plan_decompose", True),
        plan_min_steps=getattr(settings, "developer_plan_min_steps", 2),
        plan_max_steps=getattr(settings, "developer_plan_max_steps", 8),
        session_max_turns=getattr(settings, "developer_session_max_turns", 500),
        session_time_budget_s=getattr(settings, "developer_session_time_budget_s", 1200.0),
        stage_guidance=bool(getattr(settings, "developer_stage_guidance", True)),
        step_feedback_command=getattr(settings, "developer_step_feedback_command", "") or "",
        cross_run_read_tools=getattr(settings, "cross_run_read_tools", False),   # PART V §22 (dev-scoped)
        memory_dir=getattr(settings, "memory_dir", None),
        # F2 · the PROBE (tools/dev_probe.py). Plain values, not the Settings object, like every
        # knob above: `make_roles` — whose developer-backend wirings live in this module since
        # 2026-09-08 — is the ONE place a setting becomes a role's behaviour.
        probe=getattr(settings, "developer_probe", True),
        probe_timeout_s=getattr(settings, "developer_probe_timeout_s", 60.0),
        probe_confine=getattr(settings, "developer_probe_confine", True), probe_max_calls=getattr(settings, "developer_probe_max_calls", 0),  # noqa: E501
        # Snapshot the eval trust tier here; the role/tool never reads live Settings.
        command_runtime=DeveloperCommandRuntime.from_settings(settings))


def external_cli_developer(task: TaskAdapter, settings, developer, *, param_search: bool,
                           developer_role: str = "developer"):
    """The external coding-agent Developer wrapping `developer` as its fallback, or None.

    External coding-agent Developer (ADR-7): an external CLI agent writes/repairs the
    solution code, reusing the task's brief. Tool-agnostic via cli_agent presets.
    An external coding-agent preset also stays off for a param-search run (see `param_search`):
    do NOT wire the editing agent even if a developer_backend preset was requested.

    `developer` is the task-owned Developer this branch REPLACES, and it is passed in rather than
    read back off the caller because it is also this branch's own fallback: `ValidatingDeveloper`
    degrades to it when the agent's output fails validation, so the two roles of that one object
    (what was there before, what catches a bad attempt) are the same object by construction.
    """
    from looplab.agents.cli_agent import PRESETS

    if settings.developer_backend not in PRESETS or param_search:
        return None
    from looplab.agents.cli_agent import CliAgentDeveloper, opencode_config
    # An EXTERNAL coding agent carries its own `.model`/`.host` — it has no role `.client` for
    # `_set_role_client` to rebind (that helper explicitly skips clientless objects, naming this
    # very case). So the developer-stage overrides applied further down never reached it and the
    # agent silently ran on the shared `llm_model`/`llm_base_url` while the operator saw
    # `developer_model` accepted. Resolve them HERE, at the constructor that actually owns them.
    dev_target = resolve_llm_target(settings, role=developer_role)
    dev_base_url = dev_target.base_url
    agent_model = _agent_model(settings.developer_backend, dev_target.model)
    # Drop a self-contained provider config in the agent's workdir so OpenCode talks
    # to the local Ollama endpoint and never fetches the external model registry.
    workdir_files = {}
    if settings.developer_backend == "opencode":
        workdir_files["opencode.json"] = opencode_config(dev_base_url, agent_model)
    # RepoTask: the agent edits an existing repo (seed_dir) within its edit-surface;
    # the validator runs in repo_mode and the fallback is the task's baseline developer.
    repo_spec_fn = getattr(task, "repo_spec", None)
    repo_spec = repo_spec_fn() if callable(repo_spec_fn) else None
    brief = task.agent_brief() if repo_spec else getattr(developer, "brief", "")
    surface = repo_spec["edit_surface"] if repo_spec else settings.agent_surface
    # Phase 4: seed all editable repos into the agent's worktree (each at its subdir).
    seed_dirs = repo_spec["editables"] if repo_spec else None
    # Preserve the task-owned original Developer as validation fallback: it may be an LLM
    # writer, a deterministic/template implementation, or the repo baseline.
    original_developer = developer
    agent_developer = CliAgentDeveloper(
        model=agent_model,
        base_url=dev_base_url, brief=brief,
        spec=PRESETS[settings.developer_backend],
        cmd_override=([settings.agent_cmd] if settings.agent_cmd else None),
        timeout=settings.agent_timeout, workdir_files=workdir_files,
        patch_gate=(settings.agent_patch_gate or bool(repo_spec)),
        surface=surface, seed_dirs=seed_dirs,
        protect=(repo_spec["protected_names"] if repo_spec else None),
        editable_prefixes=([e["name"] for e in repo_spec["editables"]
                            if e["name"] not in (".", "")] if repo_spec else None),
        # THE RUN'S OWN ACCOUNTANT, like every in-process role (doc 27
        # `external-cli-usage-is-unpriced`). The external agent has no `.client` for
        # `_set_role_client` to rebind — the same asymmetry the model/base-url resolution above
        # exists for — so its ledger entry has to be wired at the constructor too, or the role
        # that WRITES THE CODE is the one role missing from `llm_usage` and `looplab tokens`.
        # Its invocations land unpriced (`calls` without `priced_calls`), which is the honest
        # shape: the tokens are spent inside the child process.
        accountant=run_cost_accountant(settings))
    if settings.validate_agent:
        from looplab.agents.roles import ValidatingDeveloper
        return ValidatingDeveloper(
            agent_developer, fallback=original_developer,
            max_retries=settings.agent_max_retries, repo_mode=bool(repo_spec))
    return agent_developer


def best_of_n_developer(task: TaskAdapter, settings, developer, *, param_search: bool):
    """`developer` wrapped for best-of-N, or None when the wrap does not apply.

    C2 best-of-N: wrap the in-house LLM developer to generate N candidates and keep the best by an
    execution-free reward. Skipped for external coding agents (cost rule) and the no-edit param mode.
    """
    from looplab.agents.cli_agent import PRESETS

    if settings.best_of_n <= 1 or settings.developer_backend in PRESETS or param_search:
        return None
    from looplab.search.best_of_n import BestOfNDeveloper, refuse_unrankable_best_of_n
    refuse_unrankable_best_of_n(developer, settings.best_of_n)  # C5/C2 · docs/BACKLOG.md §0.18
    return BestOfNDeveloper(developer, n=settings.best_of_n,
                            listwise=getattr(settings, "best_of_n_listwise", True),
                            parser=getattr(settings, "llm_parser", "tool_call"),
                            foresight=getattr(settings, "foresight", True),
                            direction=getattr(task, "direction", "min"),
                            goal=getattr(task, "goal", ""),
                            min_confidence=getattr(settings, "foresight_min_confidence", 0.0))
