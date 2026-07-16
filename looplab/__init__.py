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
    "_mcp_transport": "tools",
    "_pathsafe": "core",
    "_runcache": "tools",
    "ablation": "engine",
    "advisory_payloads": "core",  # bounded canonical forms for untrusted advisory sidecars (memos/reports)
    "agent": "agents",
    "agents_md": "tools",
    "advisory_payloads": "core",  # bounded/redacted memo + report projections at durable/read boundaries
    "appconfig": "core",
    "appstate": "serve",
    "attention": "serve",
    "archive": "search",
    "artifacts": "serve",
    "asset_brief": "tools",   # PART IV D1 seed-time asset/prior-art brief (offline)
    "audit": "engine",   # engine audit/trust-emitter mixin
    "concept_graph": "search",   # PART IV D5 concept-graph coverage diagnostic (offline)
    "coverage": "search",
    "graded_novelty": "search",   # PART IV D3 graded novelty + failed-direction re-exam (advisory)
    "lock_in": "search",   # PART IV D7 action-space lock-in detector (offline)
    "novelty_recall": "search",   # PART IV E3 novelty-gate recall / paraphrase-leak diagnostic (offline)
    "research_targeting": "search",   # PART IV D2 axis-structured research targeting (offline)
    "taxonomy_dedup": "search",   # PART IV D4 taxonomy-aware board dedup analysis (offline)
    "crash_repair": "engine",
    "claims": "engine",          # PART IV cross-run Step 4: evidence-grounded claim assessments (read-model)
    "claim_key": "engine",       # PART IV cross-run §21.20.13: structured scope+polarity-safe claim key
    "claim_steward": "engine",   # PART IV cross-run §22.4: agentic claim curator (LLM proposes ratify/reject/pin)
    "concept_registry": "engine",# PART IV cross-run CR1a: concept UID + alias resolver (merge/purge/split)
    "concept_steward": "engine", # PART IV cross-run §21.20.13/§22.4: agentic taxonomy curator (LLM proposes)
    "cross_run_index": "engine", # PART IV cross-run Step 1/CR0: run passport + facts, deterministic rebuild
    "task_facets": "engine",     # PART IV cross-run §21.20.2: agentic task faceting overlay (off the index)
    "cross_run_tools": "tools",  # PART V §22: read-only cross-run knowledge tool for the agent tool-loop
    "assistant": "serve",
    "assistant_commands": "serve",
    "atomicio": "core",
    "best_of_n": "search",
    "bg_tasks": "runtime",
    "classification": "adapters",
    "cli_agent": "agents",
    "comment_projection": "events",
    "command_eval": "runtime",
    "command_observation": "serve",
    "config": "core",
    "confirm": "trust",
    "confirm_phase": "engine",   # engine confirm mixin ("confirm" is taken by trust/confirm.py)
    "context_budget": "core",
    "costs": "engine",
    "critic": "trust",
    "cv": "trust",
    "harden": "trust",
    "dataset_task": "adapters",
    "deep_research": "agents",
    "deps": "runtime",
    "digest": "events",
    "edit_match": "tools",
    "env_inspect": "tools",
    "errors": "core",
    "eval_dispatch": "engine",
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
    "jobs": "serve",
    "jupyter": "runtime",
    "kaggle_dl": "adapters",
    "knowledge_tools": "tools",
    "launch": "serve",
    "leakage": "trust",
    "lesson_guard": "trust",   # PART IV D6 lesson over-generalization guard (advisory)
    "lessons": "engine",
    "lessons_distill": "engine",
    "lessons_priors": "engine",
    "lessons_reconcile": "engine",
    "literature": "tools",
    "llm": "core",
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
    "notebook": "runtime",
    "node_build": "engine",
    "novelty": "engine",
    "operators": "search",
    "options": "engine",
    "orchestrator": "engine",
    "panel": "serve",
    "parse": "core",
    "patch": "tools",
    "perm_modes": "tools",
    "policy": "search",
    "profile": "core",
    "projects": "serve",
    "protocol": "serve",
    "prompts": "core",
    "proposal_cues": "engine",
    "proxy": "runtime",
    "readmodel": "events",
    "redact": "trust",
    "regression": "adapters",
    "replay": "events",
    "repo_developer": "adapters",
    "repo_task": "adapters",
    "repo_write_tools": "adapters",
    "report": "serve",
    "research_cadence": "engine",
    "reposcout": "tools",
    "retrieval": "tools",
    "reviews": "serve",
    "reward_hack": "trust",
    "roles": "agents",
    "run_commands": "serve",
    "run_tools": "tools",
    "machine_runs_tools": "tools",
    "sandbox": "runtime",
    "schemas": "serve",
    "scope_report": "serve",
    "serve_prompts": "serve",   # UI-server prompt strings ("prompts" is taken by core/prompts.py)
    "server": "serve",
    "settings_store": "serve",
    "shell_tools": "tools",
    "span_index": "events",   # derived light span index behind the UI trace views (perf)
    "signal_delivery": "engine",   # §1 signal-delivery registry (docs/14-agent-framework-mega-review)
    "skills": "tools",
    "source_identity": "trust",   # provenance/source-identity checks among the trust monitors
    "strategist": "agents",
    "strategy": "engine",   # engine strategist-cadence mixin ("strategist" is taken by agents/strategist.py)
    "stuck": "agents",
    "surrogate": "search",
    "tasks": "adapters",
    "timeseries": "adapters",
    "tool_loop": "agents",
    "toytask": "adapters",
    "traceview": "events",
    "tracing": "core",
    "triage": "engine",
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

    def create_module(self, spec):
        return importlib.import_module(self._canonical)

    def exec_module(self, module):  # already executed under its canonical name
        pass


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
