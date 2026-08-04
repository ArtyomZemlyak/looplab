"""LoopLab — autonomous ML/DS research engine.

The package is organised as the subpackage tree the implementation plan
(``06-implementation-plan.md``) always targeted:

    core/      foundation — domain models, config, the LLM layer, parsing, low-level utils
    events/    files-as-truth — append-only event store, fold/replay, projections, exporters
    runtime/   process execution — sandboxes, command evaluation, environment prep
    tools/     agent-facing tools + the retrieval/knowledge plumbing behind them
    agents/    the LLM personas (Researcher/Developer/Strategist/…) and their drive loops
    search/    candidate-selection policies, operators, search-space helpers
    trust/     gates + monitors that keep results honest (anti-hack, leakage, CV, redaction)
    engine/    the orchestrator loop and its cross-run memory
    adapters/  task types the engine can drive (toy, dataset, MLE-bench, repo, …)
    serve/     the UI server, assistant and read-only views over run data

Entry points (``cli``, ``bench``, ``sweep``) stay at the package root so
``python -m looplab.cli`` and the console scripts keep working.

Backward compatibility: every pre-split flat import (``import looplab.models``,
``from looplab.orchestrator import Engine``, ``monkeypatch.setattr("looplab.sandbox.X", …)``)
still resolves — a meta-path finder below lazily aliases ``looplab.<name>`` to its new
canonical location, returning the SAME module object, so patching either path patches both.
"""

import importlib
import importlib.abc
import importlib.util
import sys

__version__ = "0.1.0"

# old flat module name -> its subpackage today (kept in sync by tests/test_package_layout.py)
_LAYOUT = {
    "_base": "tools",
    "_log_index": "serve",   # scaffolding shared by the two incremental log indexes (doc 25 SC-04)
    "_mcp_transport": "tools",
    "_pathsafe": "core",
    "_runcache": "tools",
    "ablation": "engine",
    "advisory_payloads": "core",  # bounded canonical forms for untrusted advisory sidecars (memos/reports)
    "agent": "agents",
    "agents_md": "tools",
    "appconfig": "core",
    "appstate": "serve",
    "attention": "serve",
    "archive": "search",
    "artifacts": "serve",
    "paid_work": "serve",
    "settings_ui_schema": "serve",
    "asset_brief": "tools",   # PART IV D1 bounded local asset/prior-art brief
    "audit": "engine",   # engine audit/trust-emitter mixin
    "concept_graph": "search",   # PART IV D5 pure concept-graph projection used offline and live
    "concept_projection": "search",  # receipt/lifecycle-aware CURRENT membership boundary
    "coverage": "search",
    "graded_novelty": "search",   # PART IV D3 graded novelty + failed-direction re-exam (advisory)
    "lock_in": "search",   # PART IV D7 action-space lock-in detector (offline)
    "novelty_recall": "search",   # PART IV E3 novelty-gate recall / paraphrase-leak diagnostic (offline)
    "research_targeting": "search",   # PART IV D2 axis-structured research targeting (offline)
    "taxonomy_dedup": "search",   # PART IV D4 taxonomy-aware board dedup analysis (offline)
    "cross_run_context": "engine",  # shared skeleton of the live cross-run builders (doc 25 EC-01)
    "crash_repair": "engine",
    "cadence": "engine",     # the shared since-last node-count gate (doc 25 EC-07)
    "claims": "engine",
    "claims_health": "engine",  # ...its source-row/read-health leaf (doc 25 EM-01 split)          # PART IV cross-run Step 4: evidence-grounded claim assessments (read-model)
    "claims_assessments": "engine",  # ...the lessons+research verdict projections (same split)
    "claims_retrieval": "engine",  # ...and its context-pack/retrieval top (same split)
    "claim_key": "engine",       # PART IV cross-run §21.20.13: structured scope+polarity-safe claim key
    "claim_steward": "engine",   # PART IV cross-run §22.4: agentic claim curator (LLM proposes ratify/reject/pin)
    "concept_registry": "engine",# PART IV cross-run CR1a: concept UID + alias resolver (merge/purge/split)
    "concept_steward": "engine", # PART IV cross-run §21.20.13/§22.4: agentic taxonomy curator (LLM proposes)
    "cross_run_index": "engine", # PART IV cross-run Step 1/CR0: run passport + facts, deterministic rebuild
    "task_kinds": "core",       # shared launch/backend defaults used by generated and interactive configs
    "task_facets": "engine",     # PART IV cross-run §21.20.2: agentic task faceting overlay (off the index)
    "governance_health": "engine",  # PART IV cross-run: paid-curation ledger health / fail-closed gates
    "steward_invocation": "engine",  # PART IV cross-run: agentic steward invocation/session bookkeeping
    "concept_tools": "tools",    # PART V Phase 2a: assistant-editable cross-run concept taxonomy (merge/purge/split, gated)
    "cross_run_tools": "tools",  # PART V §22: read-only cross-run knowledge tool for the agent tool-loop
    "assistant": "serve",
    "assistant_commands": "serve",
    "atomicio": "core",
    "best_of_n": "search",
    "card_selection": "search",  # Card-backed candidate election and ownership receipts
    "bg_tasks": "runtime",
    "classification": "adapters",
    "cli_agent": "agents",
    "belief_projection": "events",   # derived belief view over the card board (doc 25 CO-11)
    "comment_projection": "events",
    "command_eval": "runtime",
    "command_observation": "serve",
    "comparison": "core",
    "concepts": "core",       # canonical concept identity + materialization integrity contracts
    "concept_frame": "serve",   # bounded versioned concept frames served to the UI
    "config": "core",
    "confirm": "trust",
    "confirm_phase": "engine",   # engine confirm mixin ("confirm" is taken by trust/confirm.py)
    "context_budget": "core",
    "costs": "engine",
    "critic": "trust",
    "cross_run": "trust",   # cross-run identity/scope-boundary checks among the trust monitors
    "cv": "trust",
    "harden": "trust",
    "dataset_task": "adapters",
    "deep_research": "agents",
    "deps": "runtime",
    "deletion_service": "serve",
    "deletion_transaction": "serve",
    "digest": "events",
    "edit_match": "tools",
    "env_inspect": "tools",
    "errors": "core",
    "eval_dispatch": "engine",
    "fence": "core",           # the shared durable writer-fence protocol (doc 25 CO-01)
    "judge": "trust",         # one structured-judge invocation (doc 25 CT-09)
    "factory": "agents",      # the agent/role composition root (doc 25 RA-01)
    "preflight": "agents",    # the pre-run LLM endpoint/credential reachability check
    "findings": "trust",      # one trust-finding shape + the gate namespaces (doc 25 CT-10)
    "fitness": "core",
    "eval_stages": "engine",
    "eventstore": "events",
    "evaluate": "engine",
    "engine_proc": "serve",
    "finalize": "engine",
    "foresight": "search",
    "gate": "trust",
    "genesis": "engine",
    "gitenv": "core",
    "git_tools": "tools",
    "hardware": "core",
    "hints": "agents",
    "holdout": "engine",
    "hybrid_merge": "search",
    "htmlview": "events",
    "http": "serve",             # the shared control-plane JSON body parser (doc 25 SR-05)
    "jobs": "serve",
    "jsonutil": "core",         # canonical JSON bytes for receipt preimages (doc 25 SE-08)
    "jupyter": "serve",
    "kaggle_dl": "adapters",
    "knowledge_tools": "tools",
    "launch": "serve",
    "leakage": "trust",
    "lesson_guard": "trust",   # PART IV D6 lesson over-generalization guard (advisory)
    "lessons": "engine",
    "lessons_distill": "engine",
    "lessons_priors": "engine",
    "lessons_reconcile": "engine",
    "latebind": "core",
    "literature": "tools",
    "llm": "core",
    # the shared paid-call concurrency boundary is a canonical core module; registering
    # it keeps both the package-layout audit and the supported flat import alias exhaustive.
    "llm_broker": "core",
    "llm_streaming": "core",
    "llm_toolcall": "core",
    "llm_transient": "core",
    "llm_context": "serve",
    "localize": "engine",
    "log_pages": "serve",
    "mcp_tools": "tools",
    "memora": "tools",
    "memory_tools": "tools",
    "memory": "engine",
    "metrics_adapters": "serve",
    "mlebench": "adapters",
    "mlebench_grade": "adapters",
    "mlebench_prep": "adapters",
    "mlebench_real": "adapters",
    "mlflow_export": "events",
    "models": "core",
    "notebook": "events",
    "node_build": "engine",
    "node_evidence": "core",
    "novelty": "engine",
    "numeric": "core",
    "operators": "search",
    "options": "engine",
    "orchestrator": "engine",
    # A Researcher wrapper (a search policy), not a UI component. NOTE this entry only aliases
    # the FLAT `looplab.panel`; `_CompatFinder` refuses dotted names, so `looplab.serve.panel`
    # is deliberately NOT aliased — the in-repo callers were repointed instead.
    "panel": "search",
    "parse": "core",
    "pathsafe": "core",
    "patch": "tools",
    "perm_modes": "tools",
    "policy": "search",
    "profile": "core",
    "projects": "serve",
    "protocol": "serve",
    "prompts": "core",
    "proposal_cues": "engine",
    "proxy": "search",
    # keep the bounded Card DTO boundary canonical; an unregistered serve module fails
    # the package-layout audit and also breaks the supported ``looplab.public_cards`` legacy alias.
    "public_cards": "serve",
    "readmodel": "events",
    "redact": "core",
    "regression": "adapters",
    "replay": "events",
    "repo_developer": "adapters",
    "repo_task": "adapters",
    "repo_write_tools": "adapters",
    "report": "serve",
    "research_cadence": "engine",
    "reset_route": "serve",
    "reset_transaction": "serve",
    "resources": "engine",  # resource-envelope and Card footprint scheduling helpers
    "reposcout": "tools",
    "retrieval": "tools",
    "reviews": "serve",
    "reward_hack": "trust",
    "roles": "agents",
    "run_commands": "serve",
    "run_files": "serve",
    "run_projections": "serve",   # the run-list projections AppState now owns (doc 25 SR-12)
    "run_deletion": "core",
    "run_reset": "core",
    "run_tools": "tools",
    "machine_runs_tools": "tools",
    "sandbox": "runtime",
    "scorer_fidelity": "search",
    "schemas": "serve",
    "scope_report": "serve",
    "scope_report_store": "serve",   # the durable store `routers/reports.py` shed (doc 25 SR-12)
    "scope_sources": "serve",
    "serve_prompts": "serve",   # UI-server prompt strings ("prompts" is taken by core/prompts.py)
    "server": "serve",
    "settings_store": "serve",
    "shell_tools": "tools",
    "span_index": "events",   # derived light span index behind the UI trace views (perf)
    "shared": "engine",     # cross-cluster Engine members (doc 25 ES-14)
    "signal_delivery": "engine",   # §1 signal-delivery registry (docs/14-agent-framework-mega-review)
    "skills": "tools",
    "speculation": "engine",  # durable speculative Card build queue and worker contracts
    "speculation_calibration": "search",
    "speculation_quality": "search",
    "source_identity": "core",    # provenance/source-identity primitives (stdlib-only, used by core)
    "strategist": "agents",
    "strategy": "engine",   # engine strategist-cadence mixin ("strategist" is taken by agents/strategist.py)
    "stuck": "agents",
    "surrogate": "search",
    "tasks": "adapters",
    "text": "core",              # the shared unicode word tokenizer (doc 25 EM-15)
    "timeseries": "adapters",
    "tool_loop": "agents",
    "toytask": "adapters",
    "trace_clear": "serve",      # durable write-ahead trace-clear state machine (doc 25 SR-03)
    "traceview": "events",
    "tracing": "core",
    "train_monitor": "engine",   # per-eval observer + diagnostics + separately opt-in early kill
    "asha_monitor": "engine",    # per-eval ASHA live-curve rank watchdog (advisory + opt-in kill)
    "triage": "engine",
    "widths": "engine",        # the live concurrency-width settling rule (doc 25 ES-09/EC-11)
    "tui": "serve",
    "tui_api": "serve",
    "tui_format": "serve",
    "types": "events",
    "unified_agent": "agents",
    "uibuild": "serve",
    "validate": "core",
    "vectorstore": "tools",
    "verifier": "trust",   # PART IV keystone-B §12 advisory verifier (offline/library)
    "verify": "trust",
    "web": "tools",
    "workspace": "engine",
    "write_tools": "tools",
}


class _CompatLoader(importlib.abc.Loader):
    """Loads `looplab.<old>` by importing its canonical module and aliasing it — the alias and
    the canonical name share ONE module object, so state and monkeypatching stay coherent."""

    def __init__(self, canonical: str):
        self._canonical = canonical
        self._canonical_spec = None

    def create_module(self, spec):
        # `module_from_spec` STAMPS the alias spec onto whatever we return, and we return the
        # canonical module itself — so importing `looplab.sandbox` used to leave
        # `looplab.runtime.sandbox.__spec__.name == "looplab.sandbox"`. Two things then broke:
        # `importlib.reload()` of the canonical module resolved back through THIS loader, whose
        # `exec_module` is a no-op (the reload silently did nothing), and anything reading
        # `__spec__.name` saw the wrong canonical identity. Remember the real spec here and put it
        # back in `exec_module`, which runs after the stamping.
        module = importlib.import_module(self._canonical)
        self._canonical_spec = getattr(module, "__spec__", None)
        return module

    def exec_module(self, module):  # already executed under its canonical name
        if self._canonical_spec is not None:
            module.__spec__ = self._canonical_spec


class _CompatFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        prefix, _, name = fullname.partition(".")
        if prefix != "looplab" or not name or "." in name:
            return None
        sub = _LAYOUT.get(name)
        if sub is None:
            return None
        return importlib.util.spec_from_loader(fullname, _CompatLoader(f"looplab.{sub}.{name}"))


sys.meta_path.append(_CompatFinder())
