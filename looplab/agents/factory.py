"""The composition root for the agent/role system (doc 25 RA-01).

Split out of `adapters/tasks.py`, whose docstring said "TaskAdapter seam (ADR-2) + a loader for
tasks" while more than half its lines were LLM/agent wiring with no task-adapter content:
`make_roles` and its developer-backend branches, `build_unified_agent`, `_shared_providers`,
`build_strategist_tools`, `make_developer_factory`, the abstractor and the per-role client rebinding.
That half imports `agents/`, `search/` and `tools/` heavily; the task half imports adapters and
validates dicts. Two modules, one file.

`adapters/tasks.py` re-exports every public name here, because dozens of call sites and tests spell
`from looplab.adapters.tasks import make_roles` — the same treatment `make_llm_client` already got
when it moved to its dependency-true home.

LAYERING: every import of `agents`, `search` and `tools` below is deliberately FUNCTION-LOCAL, as it
was before the move. `search` imports `agents` at module scope, so a module-level `looplab.search`
import here would close the cycle into an ImportError at startup — guarded by
`tests/test_agents_search_direction.py`.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from looplab.core.llm import make_llm_client, make_llm_client_for, resolve_llm_target
from looplab.core.prompts import PromptStore

if TYPE_CHECKING:                      # `adapters.tasks` re-exports from HERE, so a runtime import
    from looplab.adapters.tasks import TaskAdapter   # would be a cycle; annotations are strings.


def _agent_model(backend: str, model: str) -> str:
    """Map our model id to the agent's provider/model string for a local Ollama model."""
    if backend == "aider":
        return f"ollama_chat/{model}"   # aider's ollama provider id
    if backend in ("opencode", "goose", "continue"):
        return f"ollama/{model}"        # provider/model
    return model


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
            complete = chat_completer(make_llm_client_for(
                settings, factory=make_llm_client))
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


def _shared_providers(task: TaskAdapter, settings, run_dir=None, *, core_only: bool = False,
                      cross_run: bool = True, role: str = "researcher"):
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
    if run_dir is not None and getattr(settings, "all_runs_tools", True):
        from looplab.tools.run_tools import AllRunsTools
        providers.append(AllRunsTools(Path(run_dir).parent, Path(run_dir).name))   # ANY run, any task
    if cross_run and getattr(settings, "memory_dir", None) \
            and getattr(settings, "cross_run_read_tools", False):
        from looplab.tools.cross_run_tools import CrossRunTools   # PART V §22 — read-only cross-run knowledge
        # Built BEFORE the `core_only` return: `core_only` means "skip the heavy memory/KB providers",
        # not "skip cross-run evidence", and the unified pilot (the only core_only caller) is named in
        # the documented CrossRunTools audience. Folding them together silently denied the pilot the
        # Part-V tools its own contract promises. `role` is a parameter for the same reason: this
        # constructor also serves the STRATEGIST, and a hard-coded "researcher" made
        # `_role_lessons` filter every developer-tagged production lesson out of its claims/Atlas/
        # search (an unknown role deliberately sees all roles — cross_run_tools.py:324).
        providers.append(CrossRunTools(settings.memory_dir, role=role, audience="run"))
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
    # Skills: hand-written (skills_dir) + M4 auto-distilled (<memory_dir>/skills) in ONE SkillTools
    # over BOTH dirs — two separate providers would each register list_skills/use_skill and the second
    # shadows the first (the hand-written library becomes unreachable). Hand-written wins a name clash.
    _skill_dirs: list[str] = []
    if getattr(settings, "skills_dir", None):
        _skill_dirs.append(str(settings.skills_dir))
    if getattr(settings, "memory_dir", None):
        _auto = Path(settings.memory_dir) / "skills"
        if _auto.is_dir():
            _skill_dirs.append(str(_auto))
    if _skill_dirs:
        from looplab.tools.skills import SkillTools
        providers.append(SkillTools(_skill_dirs))
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
    providers = _shared_providers(task, settings, run_dir, role="strategist")
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
    researcher, developer = make_roles(
        task, split, run_dir, _developer_role="implement")   # H3/stage target applied inside

    from looplab.core.llm import resolve_llm_target

    cache: dict = {}
    def client_for(*, role):
        """The client for one stage, deduped on its RESOLVED target.

        `LlmTarget` is the cache key rather than a locally-built (model, base_url) pair: two stages
        can share an endpoint and differ only in temperature or in their profile's CREDENTIAL, and
        collapsing those would hand one stage the other's key and mis-attribute its spend.

        Resolution goes through `resolve_llm_target`, not a second copy of the precedence ladder. The
        hand-rolled one this replaces never consulted `role_profiles`, so a profile bound to a stage
        name validated, passed the startup credential check, and was then never read — in the DEFAULT
        configuration, since `unified_agent` is on."""
        target = resolve_llm_target(settings, role=role)
        if target not in cache:
            # Through THIS module's `make_llm_client` name: it is a documented monkeypatch seam.
            cache[target] = make_llm_client_for(
                settings, role=role, factory=make_llm_client)
        return cache[target]

    # A stage keeps the client `make_roles` already gave its role unless it resolves somewhere else.
    # (The ambient target itself is not needed: every comparison below is stage-vs-ROLE, so the
    # `shared = resolve_llm_target(settings)` that used to sit here was a dead store — surfaced by
    # the doc 25 RA-01 split, and pre-existing.)
    t_propose = resolve_llm_target(settings, role="propose")
    t_implement = resolve_llm_target(settings, role="implement")
    t_repair = resolve_llm_target(settings, role="repair")
    if researcher is not None and t_propose != resolve_llm_target(settings, role="researcher"):
        _set_role_client(researcher, client_for(role="propose"))
    if developer is not None and t_implement != resolve_llm_target(settings, role="developer"):
        _set_role_client(developer, client_for(role="implement"))
    # The repair stage gets its OWN Developer exactly when it resolves somewhere else than implement.
    # Both stages used to share one mutable object, so the second `_set_role_client` overwrote the
    # first: setting both stages ran BOTH on the repair model, and setting only `repair` dragged the
    # untouched implement stage along with it. A rebuild via `make_roles` (not `copy.copy`) is what
    # keeps them independent — a shallow copy would share the mutable audit state the orchestrator
    # reads after every call. Equal targets keep the historical single-object path byte for byte.
    repair_developer = None
    if t_repair != t_implement:
        repair_developer = make_roles(
            task, split, run_dir, _developer_role="repair")[1]
        _set_role_client(repair_developer, client_for(role="repair"))

    # Strategy stage: mirror cli._engine's strategist wiring exactly (off => None => no strategy
    # events => byte-parity with split mode when agent_drives_actions is also off) — INCLUDING
    # strategist_temperature, and now `strategist_model`/`strategist_base_url` and a strategy profile,
    # which the hand-built call here used to ignore. Since unified mode is the DEFAULT, that made
    # those settings no-ops for almost everyone while the split-mode path honoured them.
    strat_client = (client_for(role="strategy")
                    if settings.strategist_backend in ("llm", "agent") else None)
    strat_tools = (build_strategist_tools(task, split, run_dir)
                   if strat_client is not None and settings.strategist_backend == "agent" else None)
    strategist = make_strategist(split, client=strat_client, n_seeds=settings.n_seeds, tools=strat_tools)

    # Pilot stage: its own client + read-only run-introspection tools for self-driving the next
    # macro action (only consulted when agent_drives_actions is on, gated by legal_actions).
    # The pilot has no per-role fields of its own, so it resolves stage map > profile > shared.
    pilot_client = client_for(role="pilot")
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
                        repair_developer=repair_developer,
                        pilot_client=pilot_client, pilot_tools=pilot_tools,
                        stage_clients=extra_clients, prompts=getattr(researcher, "prompts", None),
                        agent_max_turns=getattr(settings, "agent_max_turns", 0),
                        agent_time_budget_s=getattr(settings, "agent_time_budget_s", 0.0),
                        loop_opts=loop_opts_from_settings(settings))   # B1 stuck + C1 plan + C2 summary


def make_roles(task: TaskAdapter, settings, run_dir=None, *, _developer_role: str = "developer"):
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
    client = make_llm_client_for(settings, factory=make_llm_client)
    # Honest runtime brief: when the engine will auto-install deps (and trust permits), tell tasks
    # that support it they MAY use torch/xgboost/etc. + the real hardware — so a neural-net idea
    # isn't silently downgraded to sklearn. `task_runtime_caps` returns None for offline/synthetic
    # tasks (locked to numpy+stdlib), so only capable tasks (e.g. MLEBenchReal) get the kwarg.
    from looplab.core.hardware import detect_gpu, task_runtime_caps
    # Fallbacks MATCH the Settings defaults (arch-review §5 P3): a real Settings always carries the
    # field, so these only bite an incremental/mock settings — where the conservative-but-DIFFERENT
    # value (False) silently diverged from the shipped default (auto_install_deps=True).
    _auto_install = bool(getattr(settings, "auto_install_deps", True)) and \
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
    # P25 (docs/PROMPT_REVIEW.md): does a run_phase-BASED Developer follow the Researcher? Only the
    # in-house LLMRepoDeveloper runs stages→plan→implement phases inside the node's handoff scope
    # and READS the Researcher's handoff brief; CliAgentDeveloper and the single-shot LLMDeveloper
    # never do, so the Researcher skips the per-node summary LLM call for them (handoff=False).
    _handoff_dev = False
    if (settings.developer_backend not in PRESETS
            and not _param_search
            and callable(getattr(task, "repo_spec", None))
            and task.repo_spec().get("editables")):
        from looplab.adapters.repo_task import LLMRepoDeveloper
        from looplab.agents.agent import loop_opts_from_settings as _loop_opts
        _handoff_dev = True
        developer = LLMRepoDeveloper(  # C4: plan decomposition + hard per-session backstop
            client, task, parser=settings.llm_parser, loop_opts=_loop_opts(settings),
            plan_decompose=getattr(settings, "developer_plan_decompose", True),
            plan_min_steps=getattr(settings, "developer_plan_min_steps", 2),
            plan_max_steps=getattr(settings, "developer_plan_max_steps", 8),
            session_max_turns=getattr(settings, "developer_session_max_turns", 500),
            session_time_budget_s=getattr(settings, "developer_session_time_budget_s", 1200.0),
            cross_run_read_tools=getattr(settings, "cross_run_read_tools", False),   # PART V §22 (dev-scoped)
            memory_dir=getattr(settings, "memory_dir", None))

    # External coding-agent Developer (ADR-7): an external CLI agent writes/repairs the
    # solution code, reusing the task's brief. Tool-agnostic via cli_agent presets.
    # An external coding-agent preset also stays off for a param-search run (see _param_search above):
    # do NOT wire the editing agent even if a developer_backend preset was requested.
    if settings.developer_backend in PRESETS and not _param_search:
        from looplab.agents.cli_agent import CliAgentDeveloper, opencode_config
        # An EXTERNAL coding agent carries its own `.model`/`.host` — it has no role `.client` for
        # `_set_role_client` to rebind (that helper explicitly skips clientless objects, naming this
        # very case). So the developer-stage overrides applied further down never reached it and the
        # agent silently ran on the shared `llm_model`/`llm_base_url` while the operator saw
        # `developer_model` accepted. Resolve them HERE, at the constructor that actually owns them.
        dev_target = resolve_llm_target(settings, role=_developer_role)
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
        llm_developer = developer  # in-house Developer (LLM, or baseline for repo): fallback
        agent_developer = CliAgentDeveloper(
            model=agent_model,
            base_url=dev_base_url, brief=brief,
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
    # P6/P21 (docs/PROMPT_REVIEW.md): offer the intra-node sweep ONLY when the active Developer
    # actually implements `idea.space` — the in-house LLMDeveloper on script-solution (non-repo)
    # tasks. CliAgentDeveloper (external CLI presets) and LLMRepoDeveloper never read `idea.space`:
    # a sweep proposed there yields a node the engine stretches by sweep_timeout_mult while waiting
    # for a `trials` line that never comes. Repo/command-eval tasks (anything with a repo_spec,
    # incl. the cli_overrides param-search mode) score via the task cmd, so they never sweep either.
    _offer_sweep = settings.developer_backend not in PRESETS and not callable(rs_fn)
    try:
        researcher.offer_sweep = _offer_sweep     # plain LLMResearcher path (ctor default is True)
    except Exception:  # noqa: BLE001 — duck-typed researchers without settable attrs are fine
        pass
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
            context_budget_chars=getattr(settings, "context_budget_chars", None),   # H4
            max_turns=getattr(settings, "agent_max_turns", 0),                   # 0 = unlimited
            time_budget_s=getattr(settings, "agent_time_budget_s", 0.0),         # 0 = no cap
            loop_opts=loop_opts_from_settings(settings),     # B1 stuck + C1 self-plan + C2 summary
            offer_sweep=_offer_sweep,      # P6/P21: sweep offer only where idea.space is honored
            handoff=_handoff_dev,          # P25: summary call only for the run_phase repo Developer
        )
    # C2 best-of-N: wrap the in-house LLM developer to generate N candidates and keep the best by an
    # execution-free reward. Skipped for external coding agents (cost rule) and the no-edit param mode.
    if (settings.best_of_n > 1 and settings.developer_backend not in PRESETS
            and not _param_search):
        from looplab.search.best_of_n import BestOfNDeveloper
        developer = BestOfNDeveloper(developer, n=settings.best_of_n,
                                     listwise=getattr(settings, "best_of_n_listwise", True),
                                     parser=getattr(settings, "llm_parser", "tool_call"),
                                     foresight=getattr(settings, "foresight", True),
                                     direction=getattr(task, "direction", "min"),
                                     goal=getattr(task, "goal", ""),
                                     min_confidence=getattr(settings, "foresight_min_confidence", 0.0))
    # H3 per-role model presets + §4.1 per-role temperature: point the Researcher / Developer at their
    # own model/endpoint AND/OR sampling temperature when configured (e.g. Developer on a strong coding
    # model at a low temp, Researcher on a fast breadth model at a higher temp). A temperature-only
    # override still rebuilds the client (else it would silently no-op); model/base_url stay shared.
    # The test is "does this role resolve anywhere other than the client built above?" — one
    # comparison covering the per-role fields, a temperature-only override (which used to need its own
    # clause to avoid being a silent no-op), and a connection PROFILE with its own endpoint and
    # credential. The baseline is the resolved default target: `llm_profile` moves ordinary calls as
    # one connection, while an explicit per-role profile/field still causes the corresponding rebind.
    base = resolve_llm_target(settings)
    for _role, _obj in (("researcher", researcher), (_developer_role, developer)):
        _target = resolve_llm_target(settings, role=_role)
        if _target != base:
            # Built through THIS module's `make_llm_client` — a documented monkeypatch seam that a
            # helper calling core's own binding would route straight past.
            _set_role_client(_obj, make_llm_client_for(
                settings, role=_role, factory=make_llm_client))
    return researcher, developer
