"""TaskAdapter seam (ADR-2) + a loader that dispatches on the task `kind` field.
Any object exposing `id`, `goal`, `direction`, and `build_roles()` is a valid task;
optionally `columns()` enables the grounding/profiling pre-phase.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from looplab.core.llm import CostAccountant, OpenAICompatibleClient
from looplab.adapters.mlebench import MLEBenchTask
from looplab.adapters.mlebench_real import MLEBenchRealTask
from looplab.core.prompts import PromptStore
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
    - `assets() -> list[str]` — task data filenames staged into each eval workdir; consumed by
      `engine/orchestrator.py` (staging + protected from edits).
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
    """
    id: str
    goal: str
    direction: str

    def build_roles(self) -> tuple[Researcher, Developer]: ...


_KINDS = {"quadratic": ToyTask, "regression": RegressionTask,
          "code_regression": CodeRegressionTask, "mlebench": MLEBenchTask,
          "mlebench_real": MLEBenchRealTask,
          "repo": RepoTask, "timeseries": TimeSeriesTask,
          "classification": ClassificationTask, "dataset": DatasetTask}


def kinds() -> list[str]:
    """The registered task kinds (for UI/validation — e.g. the genesis flow checks an inline task's
    kind before materializing it)."""
    return list(_KINDS)


def validate_task(data: dict) -> TaskAdapter:
    """Build + validate a task adapter from an in-memory dict (the inline-task / genesis path). Raises
    on an unknown kind OR a kind-specific validation failure (e.g. mlebench_real resolving an unknown
    competition slug) — the SAME validation the engine runs at startup, so callers can reject a bad
    spec synchronously instead of spawning a detached engine that dies before writing any events."""
    kind = data.get("kind", "quadratic")
    cls = _KINDS.get(kind)
    if cls is None:
        raise ValueError(f"unknown task kind: {kind!r} (known: {sorted(_KINDS)})")
    return cls.model_validate(data)


def load_task(path: str | Path) -> TaskAdapter:
    # Accepts a bare task file (legacy JSON, or YAML) OR a unified config file — in which case only
    # its `task:` block is validated here (the engine settings are read separately by the CLI). The
    # reader handles JSON/YAML and a BOM from Windows editors.
    from looplab.core.appconfig import load_document
    task, _settings, _out = load_document(Path(path))
    return validate_task(task)


def _agent_model(backend: str, model: str) -> str:
    """Map our model id to the agent's provider/model string for a local Ollama model."""
    if backend == "aider":
        return f"ollama_chat/{model}"   # aider's ollama provider id
    if backend in ("opencode", "goose", "continue"):
        return f"ollama/{model}"        # provider/model
    return model


def make_llm_client(settings, *, model: str | None = None,
                    base_url: str | None = None,
                    timeout: float | None = None) -> OpenAICompatibleClient:
    key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else "local"
    mdl = model or settings.llm_model
    from looplab.core.llm import reasoning_body
    reasoning = reasoning_body(mdl, getattr(settings, "llm_reasoning", ""),
                               getattr(settings, "llm_reasoning_style", "auto"),
                               getattr(settings, "llm_reasoning_extra", None))
    # `timeout` lets a caller bound a UI-side probe (e.g. the health check) well under a proxy's
    # gateway timeout; omitted -> the run-wide `llm_timeout` setting (idle/stall limit, default 180s).
    extra = {"timeout": timeout if timeout is not None
             else float(getattr(settings, "llm_timeout", 180.0) or 180.0)}
    return OpenAICompatibleClient(
        model=mdl, base_url=base_url or settings.llm_base_url, api_key=key,
        temperature=settings.llm_temperature, accountant=CostAccountant(),
        guided_json=getattr(settings, "llm_guided_json", False),   # H1 constrained decoding
        reasoning=reasoning,                                        # provider-aware thinking toggle
        stream=getattr(settings, "llm_stream", True),              # inter-token idle-timeout via SSE
        header_timeout=float(getattr(settings, "llm_header_timeout", 45.0) or 45.0),
        trust_env=bool(getattr(settings, "llm_trust_env", False)),  # direct-connect by default (bypass proxy)
        cache=getattr(settings, "llm_cache", False),               # T7 deterministic-response cache
        **extra,
    )


def _memora_cache_path(settings):
    """Where the LLM-abstraction cache lives: an explicit `memora_cache`, else derived from
    `memory_dir` / `knowledge_dir`, else None (in-memory only)."""
    explicit = getattr(settings, "memora_cache", None)
    if explicit:
        return str(explicit)
    if getattr(settings, "memory_dir", None):
        return str(Path(settings.memory_dir) / "memora_cache.json")
    if getattr(settings, "knowledge_dir", None):
        return str(Path(settings.knowledge_dir) / ".memora_cache.json")
    return None


def _make_abstractor(settings):
    """Memora abstractor for the tool-building sites. Returns None unless `memora` is on. When
    `memora_llm` is also on (default), wire a live chat client (via `chat_completer`) so abstractions
    are model-written and CACHED by content hash — degrading to the deterministic lexical abstractor if
    the client can't be built or the endpoint fails at call time. `memora_llm` off = lexical, zero LLM
    calls."""
    if not getattr(settings, "memora", False):
        return None
    from looplab.tools.memora import chat_completer, make_abstractor
    complete = None
    cache_path = None
    if getattr(settings, "memora_llm", False):
        try:
            complete = chat_completer(make_llm_client(settings))
        except Exception:  # noqa: BLE001 — a client we can't build just means lexical abstractions
            complete = None
        cache_path = _memora_cache_path(settings)
    return make_abstractor(settings, complete=complete, cache_path=cache_path)


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


def _shared_providers(task: TaskAdapter, settings, run_dir=None, *, core_only: bool = False):
    """The provider list shared by the Researcher, the agentic Strategist, and the unified agent's
    pilot stage (one assembly instead of three near-identical copies). Ordered exactly as the
    call sites historically built it; each site appends its own extras (RepoTools / WebTools) after.

    - Run-introspection (default on): read the run's OWN experiments + the task data mid-loop.
    - Cross-run: read-only access to SIBLING runs of the same task. Needs the run's own dir;
      off without it (parity).
    - `core_only=True` (the pilot stage) stops there; otherwise the memory/knowledge stack follows:
      knowledge base + past cases, lessons/meta-notes, skills (hand-written + M4 auto-distilled
      under <memory_dir>/skills), and arXiv literature (network-optional)."""
    providers = []
    if getattr(settings, "researcher_tools", True):
        from looplab.tools.run_tools import DataTools, RunTools
        providers.append(RunTools())                        # own experiments + code + themes
        providers.append(DataTools(task))                   # task schema / profile / data
    if run_dir is not None and getattr(settings, "cross_run_tools", True):
        from looplab.tools.run_tools import SiblingRunTools
        providers.append(SiblingRunTools(Path(run_dir).parent, Path(run_dir).name))   # other runs
    if core_only:
        return providers
    cases_path = (str(Path(settings.memory_dir) / "cases.jsonl")
                  if getattr(settings, "memory_dir", None) else None)
    if getattr(settings, "knowledge_dir", None) or cases_path:
        from looplab.tools.knowledge_tools import KnowledgeTools
        from looplab.tools.vectorstore import make_embedder
        providers.append(KnowledgeTools(
            settings.knowledge_dir, cases_path=cases_path,
            embed=make_embedder(settings),                 # KB + memory (T4 embeddings)
            abstract=_make_abstractor(settings),           # harmonic index + anchor-expansion (Memora)
            consolidate_threshold=getattr(settings, "memora_consolidate_threshold", 0.86)))
    if getattr(settings, "memory_dir", None):              # agentic pull of lessons + meta-notes (else injection-only)
        from looplab.tools.memory_tools import MemoryTools
        providers.append(MemoryTools(settings.memory_dir))
    if getattr(settings, "skills_dir", None):
        from looplab.tools.skills import SkillTools
        providers.append(SkillTools(settings.skills_dir))
    # M4 auto-distilled skills: techniques distilled from prior winning runs live under
    # <memory_dir>/skills (provenance: auto). Loaded alongside any hand-written skills_dir.
    if getattr(settings, "memory_dir", None):
        _auto = Path(settings.memory_dir) / "skills"
        if _auto.is_dir():
            from looplab.tools.skills import SkillTools
            providers.append(SkillTools(str(_auto)))
    if getattr(settings, "literature_search", False):       # E3 arXiv grounding (network-optional)
        from looplab.tools.literature import LiteratureTools
        providers.append(LiteratureTools(enabled=True))
    return providers


def build_strategist_tools(task: TaskAdapter, settings, run_dir=None):
    """Read-only toolset for the agentic Strategist (`strategist_backend="agent"`): its OWN run
    (experiments/code/themes) + the task data + SIBLING runs + the knowledge base & memory of past
    cases (+ skills / literature / web when enabled). Mirrors the Researcher's providers so the
    Strategist can ground its meta-decision in what actually happened. Returns a CompositeTools (or a
    lone provider), or None when nothing is available."""
    providers = _shared_providers(task, settings, run_dir)
    if getattr(settings, "web_search", False):              # web search/fetch (network-optional)
        from looplab.tools.web import WebTools
        providers.append(WebTools(enabled=True))
    if not providers:
        return None
    if len(providers) == 1:
        return providers[0]
    from looplab.agents.agent import CompositeTools
    return CompositeTools(providers)


def build_unified_agent(task: TaskAdapter, settings, run_dir=None):
    """Compose the unified self-driving agent from the normal split-role backends.

    The split roles are built with `unified_agent=False` so ALL existing wiring (agentic tools,
    ValidatingDeveloper, best-of-N, H3 per-role models) is reused verbatim — `researcher_model`
    already binds the propose stage and `developer_model` the implement/repair stage. Finer
    `agent_stage_models[...]` overrides rebind a specific stage on top. The strategy stage mirrors
    `make_strategist` (None when strategist_backend="off", preserving split-mode parity); the pilot
    stage gets its own client + read-only run tools for self-driving action choice."""
    from looplab.agents.strategist import make_strategist
    from looplab.agents.unified_agent import UnifiedAgent
    split = settings.model_copy(update={"unified_agent": False})
    researcher, developer = make_roles(task, split, run_dir)   # H3 per-role models applied inside

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
                    if settings.strategist_backend in ("llm", "agent") else None)
    strat_tools = (build_strategist_tools(task, split, run_dir)
                   if strat_client is not None and settings.strategist_backend == "agent" else None)
    strategist = make_strategist(split, client=strat_client, n_seeds=settings.n_seeds, tools=strat_tools)

    # Pilot stage: its own client + read-only run-introspection tools for self-driving the next
    # macro action (only consulted when agent_drives_actions is on, gated by legal_actions).
    pilot_client = client_for(stage_models.get("pilot"), stage_urls.get("pilot"))
    pilot_tools = None
    if getattr(settings, "researcher_tools", True):
        # The pilot self-drives the next action AND triages crashes; give it BOTH run-introspection
        # and the task data, so triage can judge whether a crash is a code bug or a wrong idea by
        # consulting the real schema/columns (e.g. a reference to a column that doesn't exist).
        # Cross-run: let the pilot look at sibling runs of the same task (read-only) so it can choose
        # to import a winning experiment from a neighbour. Needs the run's own dir; no-op without it.
        from looplab.agents.agent import CompositeTools
        pilot_tools = CompositeTools(_shared_providers(task, settings, run_dir, core_only=True))

    extra_clients = [c for c in (strat_client, pilot_client) if c is not None]
    from looplab.agents.agent import loop_opts_from_settings
    return UnifiedAgent(researcher=researcher, developer=developer, strategist=strategist,
                        pilot_client=pilot_client, pilot_tools=pilot_tools,
                        stage_clients=extra_clients, prompts=getattr(researcher, "prompts", None),
                        agent_max_turns=getattr(settings, "agent_max_turns", 0),
                        agent_time_budget_s=getattr(settings, "agent_time_budget_s", 0.0),
                        loop_opts=loop_opts_from_settings(settings))   # B1 stuck + C1 plan + C2 summary


def make_roles(task: TaskAdapter, settings, run_dir=None):
    """Pick role backends from config (ADR-7): toy optimizer or a live LLM. When a
    knowledge_dir is configured, the LLM Researcher is wrapped with the agentic
    retrieval toolset (ADR-16) — same developer, tool-using researcher.

    `run_dir` (the live run's directory) is threaded through purely to enable the cross-run sibling
    tools; it is None for unit-built roles and the developer-only `make_developer_factory` rebuild, so
    those paths get the legacy single-run view (byte-parity)."""
    if settings.backend != "llm":
        return task.build_roles()
    # Unified self-driving agent: one object plays both roles. Built from the split roles (flag
    # off) so the rest of this function's wiring is reused, then composed behind one identity.
    if getattr(settings, "unified_agent", False):
        agent = build_unified_agent(task, settings, run_dir)
        return agent, agent
    client = make_llm_client(settings)
    # Honest runtime brief: when the engine will auto-install deps (and trust permits), tell tasks
    # that support it they MAY use torch/xgboost/etc. + the real hardware — so a neural-net idea
    # isn't silently downgraded to sklearn. `task_runtime_caps` returns None for offline/synthetic
    # tasks (locked to numpy+stdlib), so only capable tasks (e.g. MLEBenchReal) get the kwarg.
    from looplab.core.hardware import detect_gpu, task_runtime_caps
    _auto_install = bool(getattr(settings, "auto_install_deps", False)) and \
        getattr(settings, "trust_mode", "trusted_local") == "trusted_local"
    _caps = task_runtime_caps(task, auto_install=_auto_install,
                              gpu=detect_gpu() if _auto_install else None)
    _kw = {"parser": settings.llm_parser}
    if _caps is not None:
        _kw["runtime_caps"] = _caps
    researcher, developer = task.llm_roles(client, **_kw)

    # In-house repo code-writer: a RepoTask ships a NoOp in-house developer because repo editing was
    # designed for external coding agents (opencode/aider/…). When none is configured, give the agent
    # an in-house LLM developer that reads the repo + AUTHORS the files the eval needs (e.g. the eval
    # entrypoint) within the surface, via the shared tool loop — so a repo task runs on JUST the
    # in-house LLM. An external coding-agent preset (below) still takes precedence when requested.
    from looplab.agents.cli_agent import PRESETS
    # A cli_overrides hyperparameter-search RepoTask (`params` set) is a NO-code-edit mode: the
    # experiment varies via CLI overrides, not edits, so the baseline (NoOp) developer is correct.
    # Compute the guard BEFORE either developer branch so the in-house editor isn't wired for a
    # param-search run (which would inject agent-authored code into every eval and perturb the metric).
    _param_search = bool(getattr(task, "params", None)) and callable(getattr(task, "repo_spec", None))
    if (settings.developer_backend not in PRESETS
            and not _param_search
            and callable(getattr(task, "repo_spec", None))
            and task.repo_spec().get("editables")):
        from looplab.adapters.repo_task import LLMRepoDeveloper
        from looplab.agents.agent import loop_opts_from_settings as _loop_opts
        developer = LLMRepoDeveloper(  # C4: plan decomposition + hard per-session backstop
            client, task, parser=settings.llm_parser, loop_opts=_loop_opts(settings),
            plan_decompose=getattr(settings, "developer_plan_decompose", True),
            plan_min_steps=getattr(settings, "developer_plan_min_steps", 2),
            plan_max_steps=getattr(settings, "developer_plan_max_steps", 8),
            session_max_turns=getattr(settings, "developer_session_max_turns", 500),
            session_time_budget_s=getattr(settings, "developer_session_time_budget_s", 1200.0))

    # External coding-agent Developer (ADR-7): an external CLI agent writes/repairs the
    # solution code, reusing the task's brief. Tool-agnostic via cli_agent presets.
    # An external coding-agent preset also stays off for a param-search run (see _param_search above):
    # do NOT wire the editing agent even if a developer_backend preset was requested.
    if settings.developer_backend in PRESETS and not _param_search:
        from looplab.agents.cli_agent import CliAgentDeveloper, opencode_config
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
            from looplab.agents.roles import ValidatingDeveloper
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
    # Run-introspection (default on): let the Researcher read its OWN experiments + the task data
    # mid-loop instead of optimizing blind. This alone makes the Researcher a tool-using agent.
    # Cross-run introspection: read-only access to SIBLING runs of the same task so the Researcher can
    # build on a neighbouring run's experiments. Needs the run's own dir; off without it (parity).
    providers = _shared_providers(task, settings, run_dir)
    # RepoTask code-edit mode (item #3): give the Researcher read-only grep/list/read over the
    # editable repo(s) so it proposes changes from the actual code, not blind. Skipped for the
    # cli_overrides param-search mode (no code to read) and non-repo tasks.
    rs_fn = getattr(task, "repo_spec", None)
    rs = rs_fn() if callable(rs_fn) else None
    if rs and rs.get("editables") and not _param_search:
        from looplab.tools.knowledge_tools import RepoTools
        providers.append(RepoTools(rs["editables"]))
    # `researcher_tools` is the master switch for the tool-using Researcher: an explicit opt-out yields
    # a PLAIN LLMResearcher even when other tool sources (knowledge_dir — now on by default — cross-run,
    # skills) are configured, so the flag's meaning stays "no tool loop", not just "no run-introspection".
    if providers and getattr(settings, "researcher_tools", True):
        from looplab.agents.agent import CompositeTools, ToolUsingResearcher, loop_opts_from_settings
        tools = providers[0] if len(providers) == 1 else CompositeTools(providers)
        researcher = ToolUsingResearcher(
            client, tools,
            space_hint=getattr(researcher, "space_hint", ""),
            bounds=getattr(researcher, "bounds", None),
            parser=settings.llm_parser, prompts=prompts,
            context_budget_chars=getattr(settings, "context_budget_chars", 0),   # H4
            max_turns=getattr(settings, "agent_max_turns", 0),                   # 0 = unlimited
            time_budget_s=getattr(settings, "agent_time_budget_s", 0.0),         # 0 = no cap
            loop_opts=loop_opts_from_settings(settings),     # B1 stuck + C1 self-plan + C2 summary
        )
    # C2 best-of-N: wrap the in-house LLM developer to generate N candidates and keep the best by an
    # execution-free reward. Skipped for external coding agents (cost rule) and the no-edit param mode.
    if (settings.best_of_n > 1 and settings.developer_backend not in PRESETS
            and not _param_search):
        from looplab.search.best_of_n import BestOfNDeveloper
        developer = BestOfNDeveloper(developer, n=settings.best_of_n,
                                     listwise=getattr(settings, "best_of_n_listwise", True),
                                     parser=getattr(settings, "llm_parser", "tool_call"),
                                     foresight=getattr(settings, "foresight", True))
    # H3 per-role model presets: point the Researcher / Developer at their own model/endpoint when
    # configured (e.g. Developer on a strong coding model, Researcher on a fast breadth model).
    if settings.researcher_model or settings.researcher_base_url:
        _set_role_client(researcher, make_llm_client(
            settings, model=settings.researcher_model, base_url=settings.researcher_base_url))
    if settings.developer_model or settings.developer_base_url:
        _set_role_client(developer, make_llm_client(
            settings, model=settings.developer_model, base_url=settings.developer_base_url))
    return researcher, developer
