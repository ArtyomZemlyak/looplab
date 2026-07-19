"""BACKLOG §4: EngineOptions — the pure-config Engine knobs as one frozen bundle.

Differential proof that the `options=EngineOptions.from_settings(settings)` path is behavior-
identical to the OLD cli.py::_engine kwarg-by-kwarg passthrough:
  * an Engine built with the pre-refactor literal `kwarg=settings.<field>` mapping (copied verbatim
    from `git show HEAD:looplab/cli.py` before the collapse) and an Engine built via `options=`
    end up with IDENTICAL config attributes, for a Settings with non-defaults spread across
    parallelism / budgets / trust / lessons / novelty / holdout / search;
  * an explicitly passed kwarg (including a falsy one like None) beats the `options` field;
  * `EngineOptions()` defaults reproduce a bare `Engine(...)` exactly (the field defaults mirror
    the legacy signature defaults).
"""
from __future__ import annotations

from looplab.adapters.toytask import ToyTask
from looplab.agents.roles import ToyObjectiveDeveloper, ToyResearcher
from looplab.core.config import Settings
from looplab.engine.options import EngineOptions
from looplab.engine.orchestrator import Engine
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree


def _mk_engine(run_dir, **kw) -> Engine:
    """A minimal toy Engine; **kw carries the config knobs under test."""
    task = ToyTask()
    researcher = ToyResearcher(task.bounds, seed=task.seed, step=task.step)
    developer = ToyObjectiveDeveloper()
    return Engine(run_dir, task=task, researcher=researcher, developer=developer,
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=3), **kw)


# EngineOptions field -> the Engine attribute it lands on (digest_char_cap is special-cased:
# it is stamped onto the researcher, not the engine). Enumerated as a FIXED list on purpose —
# a new knob must be added here for the differential test to keep covering it.
ATTR_BY_FIELD = {
    "max_parallel": "max_parallel",
    "train_monitor": "_train_monitor",
    "train_monitor_interval_s": "_train_monitor_interval_s",
    "train_monitor_kill": "_train_monitor_kill",
    "train_monitor_kill_confidence": "_train_monitor_kill_confidence",
    "timeout": "timeout",
    "sweep_timeout_mult": "sweep_timeout_mult",
    "confirm_top_k": "confirm_top_k",
    "confirm_seeds": "confirm_seeds",
    "confirm_seed_base": "confirm_seed_base",
    "max_seconds": "max_seconds",
    "max_eval_seconds": "max_eval_seconds",
    "memory_dir": "memory_dir",
    "require_approval": "require_approval",
    "archive_resolution": "archive_resolution",
    "eval_trust_mode": "eval_trust_mode",
    "trust_mode": "trust_mode",
    "docker_image": "docker_image",
    "sandbox_memory": "sandbox_memory",
    "sandbox_cpus": "sandbox_cpus",
    "seed_mode": "_seed_mode",
    "n_seeds": "n_seeds",
    "max_nodes": "max_nodes",
    "policy_name": "_policy_name",
    "ablate_every": "_ablate_every",
    "strategist_every": "strategist_every",
    "concept_retag_every": "concept_retag_every",
    "deep_research_every": "deep_research_every",
    "concurrent_research": "concurrent_research",
    "report_every": "report_every",
    "merge_mode": "_merge_mode",
    "complexity_cue": "_complexity_cue",
    "budget_aware": "_budget_aware",
    "failure_reflection": "_failure_reflection",
    "deep_repair": "_deep_repair",
    "localize_faults": "_localize_faults",
    "feature_engineering": "_feature_engineering",
    "ablate_code_blocks": "_ablate_code_blocks",
    "proxy_kill_fraction": "proxy_kill_fraction",
    "reward_hack_detect": "reward_hack_detect",
    "trust_gate": "trust_gate",
    "code_leakage_detect": "_code_leakage_detect",
    "critic_check": "_critic_check",
    "redact_output": "_redact_output",
    "novelty_gate": "_novelty_gate",
    "novelty_epsilon": "_novelty_epsilon",
    "reflection_priors": "_reflection_priors",
    "comparative_lessons": "_comparative_lessons_on",
    "lessons_every": "lessons_every",
    "lessons_refresh_every": "lessons_refresh_every",
    "track_hypotheses": "_track_hypotheses",
    "surrogate_explore": "_surrogate_explore",
    "unified_agent": "unified_agent",
    "agent_drives_actions": "agent_drives_actions",
    "inline_repair": "_inline_repair",
    "inline_repair_attempts": "_inline_repair_attempts",
    "inline_repair_stuck_repeat": "_inline_repair_stuck_repeat",
    "inline_repair_reasons": "_inline_repair_reasons",
    "auto_install_deps": "_auto_install_deps",
    "dep_install_timeout": "_dep_install_timeout",
    "agent_control": "_agent_control",
    "holdout_fraction": "_holdout_fraction",
    "holdout_select": "_holdout_select",
    "holdout_top_k": "_holdout_top_k",
    "debug_depth": "_debug_depth",
    "operator_bandit": "_operator_bandit",
    "novelty_mode": "_novelty_mode",
    "novelty_semantic": "_novelty_semantic",
    "novelty_semantic_threshold": "_novelty_semantic_threshold",
    "research_verify": "_research_verify",
    "workdir_audit": "_workdir_audit",
    "select_verifier": "_select_verifier",
    "verifier_ci_tie": "_verifier_ci_tie",
    "select_verifier_samples": "_select_verifier_samples",
    "coverage_context": "_coverage_context",
    "concept_pivot": "_concept_pivot",
    "graded_novelty": "_graded_novelty",
    "capability_expansion": "_capability_expansion",
    "fingerprint_universal": "_fingerprint_universal",
    "cross_run_concepts": "_cross_run_concepts",
    "concept_run_base": "_concept_run_base",
    "cross_run_advisory": "_cross_run_advisory",
    "cross_run_structured_claims": "_cross_run_structured_claims",
    "cross_run_curation": "_cross_run_curation",
    "cross_run_curation_auto": "_cross_run_curation_auto",
    "cross_run_read_tools": "_cross_run_read_tools",
    "phase_handoff_summary": "_phase_handoff_summary",
    "inline_repair_retrain_cap": "_inline_repair_retrain_cap",
}


def test_every_engine_options_field_is_covered():
    """The attribute map + digest_char_cap must cover EngineOptions exactly, so a new field can't
    silently dodge the differential comparison below."""
    assert set(ATTR_BY_FIELD) | {"digest_char_cap"} == set(EngineOptions.__dataclass_fields__)


def test_from_settings_matches_old_cli_kwarg_mapping(tmp_path):
    # Non-default values spread across the subsystems (parallelism, budgets, trust, lessons,
    # novelty, holdout, search, repair, confirm).
    settings = Settings(
        max_parallel=3,
        timeout=7.5,
        sweep_timeout_mult=2.0,
        confirm_top_k=2,
        confirm_seeds=4,
        confirm_seed_base=5,
        holdout_fraction=0.4,
        holdout_select=False,
        holdout_top_k=5,
        max_seconds=123.0,
        memory_dir=str(tmp_path / "mem"),
        trust_gate="gate",
        reward_hack_detect=True,
        novelty_gate=True,
        novelty_epsilon=0.2,
        lessons_every=7,
        lessons_refresh_every=9,
        track_hypotheses=False,
        n_seeds=5,
        max_nodes=17,
        policy="evolutionary",
        digest_char_cap=1234,
        inline_repair_attempts=6,
        seed_mode="tracked",
    )

    # (a) the OLD explicit-kwarg style: the literal Settings->Engine mapping cli.py::_engine used
    # before the collapse (copied from `git show HEAD:looplab/cli.py`, object seams elided).
    old = _mk_engine(
        tmp_path / "old",
        max_parallel=settings.max_parallel,
        timeout=settings.timeout,
        sweep_timeout_mult=settings.sweep_timeout_mult,
        confirm_top_k=settings.confirm_top_k,
        confirm_seeds=settings.confirm_seeds,
        confirm_seed_base=settings.confirm_seed_base,
        holdout_fraction=settings.holdout_fraction,
        holdout_select=settings.holdout_select,
        holdout_top_k=settings.holdout_top_k,
        max_seconds=settings.max_seconds,
        max_eval_seconds=settings.max_eval_seconds,
        memory_dir=settings.memory_dir,
        require_approval=settings.require_approval,
        archive_resolution=settings.archive_resolution,
        eval_trust_mode=settings.eval_trust_mode,
        trust_mode=settings.trust_mode,
        docker_image=settings.docker_image,
        seed_mode=settings.seed_mode,
        n_seeds=settings.n_seeds,
        max_nodes=settings.max_nodes,
        policy_name=settings.policy,
        ablate_every=settings.ablate_every,
        strategist_every=settings.strategist_every,
        concept_retag_every=settings.concept_retag_every,
        deep_research_every=settings.deep_research_every,
        concurrent_research=settings.concurrent_research,
        report_every=settings.report_every,
        merge_mode=settings.merge_mode,
        complexity_cue=settings.complexity_cue,
        budget_aware=settings.budget_aware,
        failure_reflection=settings.failure_reflection,
        deep_repair=settings.deep_repair,
        inline_repair=settings.inline_repair,
        inline_repair_attempts=settings.inline_repair_attempts,
        inline_repair_stuck_repeat=settings.inline_repair_stuck_repeat,
        inline_repair_reasons=settings.inline_repair_reasons,
        auto_install_deps=settings.auto_install_deps,
        dep_install_timeout=settings.dep_install_timeout,
        agent_control=settings.agent_control,
        localize_faults=settings.localize_faults,
        feature_engineering=settings.feature_engineering,
        ablate_code_blocks=settings.ablate_code_blocks,
        proxy_kill_fraction=settings.proxy_kill_fraction,
        reward_hack_detect=settings.reward_hack_detect,
        trust_gate=settings.trust_gate,
        code_leakage_detect=settings.code_leakage_detect,
        critic_check=settings.critic_check,
        redact_output=settings.redact_output,
        novelty_gate=settings.novelty_gate,
        novelty_epsilon=settings.novelty_epsilon,
        novelty_semantic=settings.novelty_semantic,
        novelty_semantic_threshold=settings.novelty_semantic_threshold,
        debug_depth=settings.debug_depth,
        operator_bandit=settings.operator_bandit,
        digest_char_cap=settings.digest_char_cap,
        research_verify=settings.research_verify,
        workdir_audit=settings.workdir_audit,
        reflection_priors=settings.reflection_priors,
        comparative_lessons=settings.comparative_lessons,
        lessons_every=settings.lessons_every,
        lessons_refresh_every=settings.lessons_refresh_every,
        track_hypotheses=settings.track_hypotheses,
        surrogate_explore=settings.surrogate_explore,
        unified_agent=settings.unified_agent,
        agent_drives_actions=settings.agent_drives_actions,
        # Part IV/V flags now ship ON in Settings; pass them through so the old explicit-kwarg
        # mapping reproduces the same engine as from_settings (else old=library-default False).
        concept_pivot=settings.concept_pivot,
        graded_novelty=settings.graded_novelty,
        cross_run_concepts=settings.cross_run_concepts,
        concept_run_base=settings.concept_run_base,
        cross_run_structured_claims=settings.cross_run_structured_claims,
        cross_run_curation=settings.cross_run_curation,
        cross_run_advisory=settings.cross_run_advisory,
        cross_run_read_tools=settings.cross_run_read_tools,
        fingerprint_universal=settings.fingerprint_universal,
    )

    # (b) the NEW single-bundle style.
    new = _mk_engine(tmp_path / "new", options=EngineOptions.from_settings(settings))

    mismatches = {attr: (getattr(old, attr), getattr(new, attr))
                  for attr in ATTR_BY_FIELD.values()
                  if getattr(old, attr) != getattr(new, attr)}
    assert not mismatches, f"old-kwarg vs options engines diverge: {mismatches}"
    # digest_char_cap is stamped onto the researcher, not stored on the engine.
    assert old.researcher._digest_cap == new.researcher._digest_cap == 1234
    # Spot-check a few of the deliberately non-default values actually made it through (guards
    # against a both-sides-defaults false pass).
    assert new.max_parallel == 3 and new.timeout == 7.5
    assert new._policy_name == "evolutionary" and new.max_nodes == 17
    assert new.trust_gate == "gate" and new._holdout_fraction == 0.4
    assert new._inline_repair_attempts == 6 and new._seed_mode == "tracked"
    assert new.lessons_every == 7 and new._novelty_epsilon == 0.2


def test_explicit_kwarg_beats_options_field(tmp_path):
    opts = EngineOptions(timeout=99.0, max_nodes=50, memory_dir=str(tmp_path / "mem"))
    # No explicit kwarg -> the options field applies.
    e1 = _mk_engine(tmp_path / "a", options=opts)
    assert e1.timeout == 99.0 and e1.max_nodes == 50
    # An explicitly passed kwarg wins over the same options field...
    e2 = _mk_engine(tmp_path / "b", options=opts, timeout=3.5)
    assert e2.timeout == 3.5 and e2.max_nodes == 50
    # ... including a FALSY explicit value (None must not fall through to the options field —
    # that is the whole point of the _UNSET sentinel over `kwarg or options.field`).
    e3 = _mk_engine(tmp_path / "c", options=opts, memory_dir=None)
    assert e3.memory_dir is None


def test_default_options_reproduce_bare_engine(tmp_path):
    bare = _mk_engine(tmp_path / "bare")
    dflt = _mk_engine(tmp_path / "dflt", options=EngineOptions())
    for attr in ("max_parallel", "timeout", "sweep_timeout_mult", "confirm_top_k",
                 "_holdout_fraction", "_merge_mode", "trust_gate", "_novelty_epsilon",
                 "_inline_repair_stuck_repeat", "_track_hypotheses", "lessons_every",
                 "_debug_depth", "memory_dir", "_seed_mode"):
        assert getattr(bare, attr) == getattr(dflt, attr), attr
