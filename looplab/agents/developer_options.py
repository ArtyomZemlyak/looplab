"""Read and preflight developer operator declarations before building paid role clients.

These helpers select no model and execute no candidate. Core/engine/search readers remain
function-local so the composition root can import this leaf without closing dependency cycles.
"""
from __future__ import annotations


def developer_prompt_truths_enabled(settings) -> bool:
    """`Settings.prompt_truths_developer` (review 2026-09-22, Q-1), the ONE reader for both Developers
    it reaches — the repo Developer built below and the dataset brief `make_roles` asks the task for.
    An object without the field (a pre-field snapshot's restore fills it OFF) reads OFF."""
    return bool(getattr(settings, "prompt_truths_developer", False))


def _hypotheses_enabled(settings) -> bool:
    """`Settings.diagnosis_hypotheses`, read through the ONE reader that owns it.

    Function-local import because that reader lives beside the coercion it pairs with
    (`engine/failure_diagnosis.py::hypotheses_enabled`) and `agents` may reach `engine` only inside
    a call — the documented deferred edge, not a new module-level dependency.
    """
    from looplab.engine.failure_diagnosis import hypotheses_enabled

    return hypotheses_enabled(settings)


def native_pilot_preflight(task, settings, *, param_search=False):
    """Validate the opt-in task/runtime before constructing paid reasoning roles."""
    from looplab.agents.cli_agent import PRESETS
    pilot = getattr(settings, "best_of_n_pilot", None)
    assets = {}
    if pilot is not None:
        from looplab.core.errors import ConfigRefusal
        if (not 2 <= settings.best_of_n <= 8 or settings.developer_backend in PRESETS or param_search
                or getattr(settings, "external_harness", False)):
            raise ConfigRefusal('native pilot requires2..8 in-process code candidates; unsupported mode cannot silently skip it')
        for hook in ('repo_spec', 'eval_spec', 'host_grader'):
            value = getattr(task, hook, None)
            if callable(value) and value():
                raise ConfigRefusal('native pilot supports code-only script tasks without repository commands or host grading')
        asset_hook = getattr(task, 'assets', None)
        assets = asset_hook() if callable(asset_hook) else {}
        from looplab.search.best_of_n_pilot import asset_manifest, require_pilot_runtime
        require_pilot_runtime()
        asset_manifest(assets)  # refuse unsupported assets before paid generation
    return assets


def best_of_n_developer(task, settings, developer, *, param_search: bool):
    """`developer` wrapped for best-of-N, or None when the wrap does not apply.

    C2 best-of-N: wrap the in-house LLM developer to generate N candidates and keep the best by an
    execution-free reward. Skipped for external coding agents (cost rule) and the no-edit param mode.
    """
    from looplab.agents.cli_agent import PRESETS

    pilot = getattr(settings, "best_of_n_pilot", None)
    from looplab.agents.developer_options import native_pilot_preflight
    assets = native_pilot_preflight(task, settings, param_search=param_search)
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
                            min_confidence=getattr(settings, "foresight_min_confidence", 0.0),
                            pilot=pilot, pilot_assets=assets)
