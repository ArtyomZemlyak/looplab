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
    "agent": "agents",
    "agents_md": "tools",
    "appconfig": "core",
    "appstate": "serve",
    "archive": "search",
    "artifacts": "serve",
    "coverage": "search",
    "assistant": "serve",
    "assistant_commands": "serve",
    "atomicio": "core",
    "best_of_n": "search",
    "bg_tasks": "runtime",
    "classification": "adapters",
    "cli_agent": "agents",
    "command_eval": "runtime",
    "config": "core",
    "confirm": "trust",
    "confirm_phase": "engine",   # engine confirm mixin ("confirm" is taken by trust/confirm.py)
    "context_budget": "core",
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
    "eventstore": "events",
    "engine_proc": "serve",
    "finalize": "engine",
    "foresight": "search",
    "gate": "trust",
    "genesis": "engine",
    "git_tools": "tools",
    "hardware": "core",
    "hints": "agents",
    "holdout": "engine",
    "hybrid_merge": "search",
    "htmlview": "serve",
    "jobs": "serve",
    "jupyter": "runtime",
    "kaggle_dl": "adapters",
    "knowledge_tools": "tools",
    "leakage": "trust",
    "lessons": "engine",
    "literature": "tools",
    "llm": "core",
    "llm_context": "serve",
    "localize": "engine",
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
    "proxy": "runtime",
    "readmodel": "events",
    "redact": "trust",
    "regression": "adapters",
    "replay": "events",
    "repo_developer": "adapters",
    "repo_task": "adapters",
    "report": "serve",
    "reposcout": "tools",
    "retrieval": "tools",
    "reward_hack": "trust",
    "roles": "agents",
    "run_tools": "tools",
    "runs_tools": "tools",
    "sandbox": "runtime",
    "scope_report": "serve",
    "serve_prompts": "serve",   # UI-server prompt strings ("prompts" is taken by core/prompts.py)
    "server": "serve",
    "settings_store": "serve",
    "shell_tools": "tools",
    "signal_delivery": "engine",   # §1 signal-delivery registry (docs/14-agent-framework-mega-review)
    "skills": "tools",
    "strategist": "agents",
    "stuck": "agents",
    "surrogate": "search",
    "tasks": "adapters",
    "timeseries": "adapters",
    "toytask": "adapters",
    "traceview": "serve",
    "tracing": "core",
    "triage": "engine",
    "tui": "serve",
    "types": "events",
    "unified_agent": "agents",
    "uibuild": "serve",
    "validate": "core",
    "vectorstore": "tools",
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
