"""TaskAdapter seam (ADR-2) + a loader that dispatches on the task `kind` field.
Any object exposing `id`, `goal`, `direction`, and `build_roles()` is a valid task;
optionally `columns()` enables the grounding/profiling pre-phase.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol, runtime_checkable

from .llm import CostAccountant, OpenAICompatibleClient
from .mlebench import MLEBenchTask
from .mlebench_real import MLEBenchRealTask
from .prompts import PromptStore
from .classification import ClassificationTask
from .regression import CodeRegressionTask, RegressionTask
from .repo_task import RepoTask
from .roles import Developer, Researcher
from .timeseries import TimeSeriesTask
from .toytask import ToyTask


@runtime_checkable
class TaskAdapter(Protocol):
    id: str
    goal: str
    direction: str

    def build_roles(self) -> tuple[Researcher, Developer]: ...


_KINDS = {"quadratic": ToyTask, "regression": RegressionTask,
          "code_regression": CodeRegressionTask, "mlebench": MLEBenchTask,
          "mlebench_real": MLEBenchRealTask,
          "repo": RepoTask, "timeseries": TimeSeriesTask,
          "classification": ClassificationTask}


def load_task(path: str | Path) -> TaskAdapter:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    kind = data.get("kind", "quadratic")
    cls = _KINDS.get(kind)
    if cls is None:
        raise ValueError(f"unknown task kind: {kind!r} (known: {sorted(_KINDS)})")
    return cls.model_validate(data)


def _agent_model(backend: str, model: str) -> str:
    """Map our model id to the agent's provider/model string for a local Ollama model."""
    if backend == "aider":
        return f"ollama_chat/{model}"   # aider's ollama provider id
    if backend in ("opencode", "goose", "continue"):
        return f"ollama/{model}"        # provider/model
    return model


def make_llm_client(settings, *, model: str | None = None,
                    base_url: str | None = None) -> OpenAICompatibleClient:
    key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else "local"
    mdl = model or settings.llm_model
    from .llm import reasoning_body
    reasoning = reasoning_body(mdl, getattr(settings, "llm_reasoning", ""),
                               getattr(settings, "llm_reasoning_style", "auto"),
                               getattr(settings, "llm_reasoning_extra", None))
    return OpenAICompatibleClient(
        model=mdl, base_url=base_url or settings.llm_base_url, api_key=key,
        temperature=settings.llm_temperature, accountant=CostAccountant(),
        guided_json=getattr(settings, "llm_guided_json", False),   # H1 constrained decoding
        reasoning=reasoning,                                        # provider-aware thinking toggle
    )


def _set_role_client(obj, client) -> None:
    """H3: point a role (and any wrapped inner/fallback role) at a per-role LLM client. Best-effort —
    objects without a `client` (e.g. an external CLI-agent Developer) are left untouched."""
    if obj is None:
        return
    if hasattr(obj, "client"):
        try:
            obj.client = client
        except Exception:  # noqa: BLE001
            pass
    for attr in ("inner", "fallback"):
        child = getattr(obj, attr, None)
        if child is not None and child is not obj:
            _set_role_client(child, client)


def make_developer_factory(task: TaskAdapter, settings):
    """A7 Strategist support: a callable `factory(backend) -> Developer` that rebuilds just the
    Developer under a different `developer_backend` (e.g. swap in-house LLM <-> agentic coding agent
    live). Reuses `make_roles` so all the validation/patch-gate wiring is identical; returns only the
    developer. Used when the Strategist (or an operator) picks a Developer mode per phase/node."""
    def factory(backend: str):
        b = "default" if backend == "llm" else backend
        s = settings.model_copy(update={"developer_backend": b})
        _researcher, developer = make_roles(task, s)
        return developer
    return factory


def build_unified_agent(task: TaskAdapter, settings):
    """Compose the unified self-driving agent from the normal split-role backends.

    The split roles are built with `unified_agent=False` so ALL existing wiring (agentic tools,
    ValidatingDeveloper, best-of-N, H3 per-role models) is reused verbatim — `researcher_model`
    already binds the propose stage and `developer_model` the implement/repair stage. Finer
    `agent_stage_models[...]` overrides rebind a specific stage on top. The strategy stage mirrors
    `make_strategist` (None when strategist_backend="off", preserving split-mode parity); the pilot
    stage gets its own client + read-only run tools for self-driving action choice."""
    from .strategist import make_strategist
    from .unified_agent import UnifiedAgent
    split = settings.model_copy(update={"unified_agent": False})
    researcher, developer = make_roles(task, split)   # H3 per-role models applied inside

    cache: dict = {}
    def client_for(model, base):
        key = (model or settings.llm_model, base or settings.llm_base_url)
        if key not in cache:
            cache[key] = make_llm_client(settings, model=model, base_url=base)
        return cache[key]

    stage_models = settings.agent_stage_models or {}
    stage_urls = settings.agent_stage_base_urls or {}
    def maybe_override(stage, role):
        m, u = stage_models.get(stage), stage_urls.get(stage)
        if (m or u) and role is not None:
            _set_role_client(role, client_for(m, u))
    maybe_override("propose", researcher)
    maybe_override("implement", developer)
    maybe_override("repair", developer)   # repair shares the developer object

    # Strategy stage: mirror cli._engine's strategist wiring exactly (off => None => no strategy
    # events => byte-parity with split mode when agent_drives_actions is also off).
    strat_client = (client_for(stage_models.get("strategy"), stage_urls.get("strategy"))
                    if settings.strategist_backend == "llm" else None)
    strategist = make_strategist(split, client=strat_client, n_seeds=settings.n_seeds)

    # Pilot stage: its own client + read-only run-introspection tools for self-driving the next
    # macro action (only consulted when agent_drives_actions is on, gated by legal_actions).
    pilot_client = client_for(stage_models.get("pilot"), stage_urls.get("pilot"))
    pilot_tools = None
    if getattr(settings, "researcher_tools", True):
        from .run_tools import RunTools
        pilot_tools = RunTools()

    extra_clients = [c for c in (strat_client, pilot_client) if c is not None]
    return UnifiedAgent(researcher=researcher, developer=developer, strategist=strategist,
                        pilot_client=pilot_client, pilot_tools=pilot_tools,
                        stage_clients=extra_clients, prompts=getattr(researcher, "prompts", None))


def make_roles(task: TaskAdapter, settings):
    """Pick role backends from config (ADR-7): toy optimizer or a live LLM. When a
    knowledge_dir is configured, the LLM Researcher is wrapped with the agentic
    retrieval toolset (ADR-16) — same developer, tool-using researcher."""
    if settings.backend != "llm":
        return task.build_roles()
    # Unified self-driving agent: one object plays both roles. Built from the split roles (flag
    # off) so the rest of this function's wiring is reused, then composed behind one identity.
    if getattr(settings, "unified_agent", False):
        agent = build_unified_agent(task, settings)
        return agent, agent
    client = make_llm_client(settings)
    researcher, developer = task.llm_roles(client, parser=settings.llm_parser)

    # External coding-agent Developer (ADR-7): an external CLI agent writes/repairs the
    # solution code, reusing the task's brief. Tool-agnostic via cli_agent presets.
    from .cli_agent import PRESETS
    # A cli_overrides hyperparameter-search RepoTask (`params` set) is a NO-code-edit mode:
    # the experiment varies via CLI overrides, not edits, so the baseline (NoOp) developer
    # from build_roles/llm_roles is correct — do NOT wire the editing agent even if a
    # developer_backend preset was requested (that would conflate tuning with code edits).
    _param_search = bool(getattr(task, "params", None)) and callable(getattr(task, "repo_spec", None))
    if settings.developer_backend in PRESETS and not _param_search:
        from .cli_agent import CliAgentDeveloper, opencode_config
        agent_model = _agent_model(settings.developer_backend, settings.llm_model)
        # Drop a self-contained provider config in the agent's workdir so OpenCode talks
        # to the local Ollama endpoint and never fetches the external model registry.
        workdir_files = {}
        if settings.developer_backend == "opencode":
            workdir_files["opencode.json"] = opencode_config(
                settings.llm_base_url, agent_model)
        # RepoTask: the agent edits an existing repo (seed_dir) within its edit-surface;
        # the validator runs in repo_mode and the fallback is the task's baseline developer.
        repo_spec_fn = getattr(task, "repo_spec", None)
        repo_spec = repo_spec_fn() if callable(repo_spec_fn) else None
        brief = task.agent_brief() if repo_spec else getattr(developer, "brief", "")
        surface = repo_spec["edit_surface"] if repo_spec else settings.agent_surface
        # Phase 4: seed all editable repos into the agent's worktree (each at its subdir).
        seed_dirs = repo_spec["editables"] if repo_spec else None
        llm_developer = developer  # in-house Developer (LLM, or baseline for repo): fallback
        agent_developer = CliAgentDeveloper(
            model=agent_model,
            base_url=settings.llm_base_url, brief=brief,
            spec=PRESETS[settings.developer_backend],
            cmd_override=([settings.agent_cmd] if settings.agent_cmd else None),
            workdir_files=workdir_files,
            patch_gate=(settings.agent_patch_gate or bool(repo_spec)),
            surface=surface, seed_dirs=seed_dirs,
            protect=(repo_spec["protected_names"] if repo_spec else None),
            editable_prefixes=([e["name"] for e in repo_spec["editables"]
                                if e["name"] not in (".", "")] if repo_spec else None))
        if settings.validate_agent:
            from .roles import ValidatingDeveloper
            developer = ValidatingDeveloper(
                agent_developer, fallback=llm_developer,
                max_retries=settings.agent_max_retries, repo_mode=bool(repo_spec))
        else:
            developer = agent_developer

    # Hot-reloadable prompt store (I18, ADR-8).
    prompts = PromptStore(settings.prompt_dir) if settings.prompt_dir else None
    if prompts is not None:
        researcher.prompts = prompts
        if hasattr(developer, "prompts"):
            developer.prompts = prompts

    # Tool providers for the agentic Researcher: run-introspection + knowledge + memory + skills.
    cases_path = str(Path(settings.memory_dir) / "cases.jsonl") if settings.memory_dir else None
    providers = []
    # Run-introspection (default on): let the Researcher read its OWN experiments + the task data
    # mid-loop instead of optimizing blind. This alone makes the Researcher a tool-using agent.
    if getattr(settings, "researcher_tools", True):
        from .run_tools import DataTools, RunTools
        providers.append(RunTools())
        providers.append(DataTools(task))
    if settings.knowledge_dir or cases_path:
        from .knowledge_tools import KnowledgeTools
        providers.append(KnowledgeTools(settings.knowledge_dir, cases_path=cases_path))
    if settings.skills_dir:
        from .skills import SkillTools
        providers.append(SkillTools(settings.skills_dir))
    if getattr(settings, "literature_search", False):   # E3 arXiv grounding (network-optional)
        from .literature import LiteratureTools
        providers.append(LiteratureTools(enabled=True))
    # RepoTask code-edit mode (item #3): give the Researcher read-only grep/list/read over the
    # editable repo(s) so it proposes changes from the actual code, not blind. Skipped for the
    # cli_overrides param-search mode (no code to read) and non-repo tasks.
    rs_fn = getattr(task, "repo_spec", None)
    rs = rs_fn() if callable(rs_fn) else None
    if rs and rs.get("editables") and not _param_search:
        from .knowledge_tools import RepoTools
        providers.append(RepoTools(rs["editables"]))
    if providers:
        from .agent import CompositeTools, ToolUsingResearcher
        tools = providers[0] if len(providers) == 1 else CompositeTools(providers)
        researcher = ToolUsingResearcher(
            client, tools,
            space_hint=getattr(researcher, "space_hint", ""),
            bounds=getattr(researcher, "bounds", None),
            parser=settings.llm_parser, prompts=prompts,
            context_budget_chars=getattr(settings, "context_budget_chars", 0),   # H4
        )
    # C2 best-of-N: wrap the in-house LLM developer to generate N candidates and keep the best by an
    # execution-free reward. Skipped for external coding agents (cost rule) and the no-edit param mode.
    if (settings.best_of_n > 1 and settings.developer_backend not in PRESETS
            and not _param_search):
        from .best_of_n import BestOfNDeveloper
        developer = BestOfNDeveloper(developer, n=settings.best_of_n)
    # H3 per-role model presets: point the Researcher / Developer at their own model/endpoint when
    # configured (e.g. Developer on a strong coding model, Researcher on a fast breadth model).
    if settings.researcher_model or settings.researcher_base_url:
        _set_role_client(researcher, make_llm_client(
            settings, model=settings.researcher_model, base_url=settings.researcher_base_url))
    if settings.developer_model or settings.developer_base_url:
        _set_role_client(developer, make_llm_client(
            settings, model=settings.developer_model, base_url=settings.developer_base_url))
    return researcher, developer
