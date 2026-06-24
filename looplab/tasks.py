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
from .prompts import PromptStore
from .regression import CodeRegressionTask, RegressionTask
from .repo_task import RepoTask
from .roles import Developer, Researcher
from .toytask import ToyTask


@runtime_checkable
class TaskAdapter(Protocol):
    id: str
    goal: str
    direction: str

    def build_roles(self) -> tuple[Researcher, Developer]: ...


_KINDS = {"quadratic": ToyTask, "regression": RegressionTask,
          "code_regression": CodeRegressionTask, "mlebench": MLEBenchTask,
          "repo": RepoTask}


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
    return OpenAICompatibleClient(
        model=model or settings.llm_model, base_url=base_url or settings.llm_base_url, api_key=key,
        temperature=settings.llm_temperature, accountant=CostAccountant(),
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


def make_roles(task: TaskAdapter, settings):
    """Pick role backends from config (ADR-7): toy optimizer or a live LLM. When a
    knowledge_dir is configured, the LLM Researcher is wrapped with the agentic
    retrieval toolset (ADR-16) — same developer, tool-using researcher."""
    if settings.backend != "llm":
        return task.build_roles()
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

    # Tool providers for the agentic Researcher: knowledge + cross-run memory + skills.
    cases_path = str(Path(settings.memory_dir) / "cases.jsonl") if settings.memory_dir else None
    providers = []
    if settings.knowledge_dir or cases_path:
        from .knowledge_tools import KnowledgeTools
        providers.append(KnowledgeTools(settings.knowledge_dir, cases_path=cases_path))
    if settings.skills_dir:
        from .skills import SkillTools
        providers.append(SkillTools(settings.skills_dir))
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
        )
    # H3 per-role model presets: point the Researcher / Developer at their own model/endpoint when
    # configured (e.g. Developer on a strong coding model, Researcher on a fast breadth model).
    if settings.researcher_model or settings.researcher_base_url:
        _set_role_client(researcher, make_llm_client(
            settings, model=settings.researcher_model, base_url=settings.researcher_base_url))
    if settings.developer_model or settings.developer_base_url:
        _set_role_client(developer, make_llm_client(
            settings, model=settings.developer_model, base_url=settings.developer_base_url))
    return researcher, developer
