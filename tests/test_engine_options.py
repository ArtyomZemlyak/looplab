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

import dataclasses
import functools
import tempfile
import typing
from pathlib import Path

from looplab.adapters.toytask import ToyTask
from looplab.agents.toy_roles import ToyObjectiveDeveloper, ToyResearcher
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
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=3),
                  **kw)


# EngineOptions field -> the Engine attribute it lands on, DERIVED by driving `Engine.__init__`
# (review 2026-09-22, ENG1-03). It was a hand-kept copy — 145 rows beside 146 fields — and CLAUDE.md
# billed every new knob one row here; a copy is only ever as right as its last edit, and the SEVEN
# ROWS note in `test_from_settings_matches_old_cli_kwarg_mapping` measured what a row alone bought.
# Now each field is given a non-default value and the attribute that MOVES is the answer: the one
# declaration is the code that lands the knob, and the derivation is itself the property test over
# every field (`test_every_engine_options_field_is_covered`) — a field wired to nothing moves nothing
# and is NAMED, instead of being compared as its default against itself. It reproduced all 145
# hand-kept rows on the day it replaced them.
#
# The naming notes the hand map carried, which were about the Engine's attribute names rather than
# about the map, kept here:
#   * `eval_parallel`/`llm_parallel` land on `_eval_parallel`/`_llm_parallel`, the settled runtime
#     widths; `max_parallel`/`parallel_build` are the legacy read-through aliases of the same two.
#   * the legacy `novelty_gate` alias has no attribute of its own (review 2026-09-22, CORE-08): what
#     it moves is the mode, forced to "algo".
#   * `metric_salvage` is public: the mixin declares it as a class attribute so a test or a resumed
#     subclass can set it directly. `metric_subject` is public for the same reason — three mixins
#     read it as a plain attribute — and so is `auto_extra_metrics` (`engine/evaluate.py` reads it at
#     the one place the `node_evaluated` payload is built). `landlock` and `syscall_fence` are private
#     beside `_read_fence`, their sibling boundary: settled at construction, read by `resources.py`.
#   * `eval_noise_seeds` keeps its name on the Engine: it is clamped there (0 and 1 both mean off).
#   * digest_char_cap is special-cased: it is stamped onto the RESEARCHER, not the engine.
#
# The only hand-kept data left is a VALUE for each field whose generic non-default is refused or
# settled straight back to the default, and the one field that moves only beside another. A new knob
# needs a row here only when the derivation names it.
_PERTURBED_VALUE = {
    "trust_gate": "gate",           # anything outside audit|gate|block is a ConfigRefusal
    "metric_salvage": "select",     # an unknown mode settles to the default `audit`
    "metric_subject": "require",    # an unknown rung settles to the default `audit`
}
_PERTURBED_BESIDE = {
    # `agent_drives_actions = unified_agent and agent_drives_actions`: inert without the facade.
    "agent_drives_actions": {"unified_agent": True},
}
_NOT_ON_THE_ENGINE = frozenset({"digest_char_cap"})
_VALUE_TYPES = (bool, int, float, str, type(None), tuple, list, dict, set, frozenset)
_ABSENT = object()


def _perturbed(f: dataclasses.Field, hint):
    """A valid NON-default value for one EngineOptions field, derived from its default and type."""
    if f.name in _PERTURBED_VALUE:
        return _PERTURBED_VALUE[f.name]
    default = f.default if f.default is not dataclasses.MISSING else f.default_factory()
    if default is None:                              # Optional[X]: a value of X
        kind = next((a for a in typing.get_args(hint) if a is not type(None)), None)
        kind = typing.get_origin(kind) or kind
        if kind is str:
            return str(Path(tempfile.gettempdir()) / f"looplab-perturbed-{f.name}")
        by_kind = {bool: True, int: 2, float: 60.0, dict: {}}
        assert kind in by_kind, f"no generic non-default for {f.name}: {hint} — add a _PERTURBED_VALUE row"
        return by_kind[kind]
    if isinstance(default, bool):
        return not default
    if isinstance(default, int):
        return default + 2               # +2, not +1: `eval_noise_seeds` reads 1 as off, like 0
    if isinstance(default, float):
        return default + 0.25
    if isinstance(default, str):
        return f"{default}-perturbed" if default else "perturbed"
    if isinstance(default, dict):
        return {"PERTURBED_KNOB": "1"}
    assert isinstance(default, tuple), f"no generic non-default for {f.name}: {default!r}"
    return default[:1]


def _landed(engine, names: frozenset) -> dict:
    """Every plain-data attribute a constructed Engine holds, plus the read-through properties named
    after a field (`max_parallel`, `parallel_build`) — the only properties a knob can land on."""
    out = {k: v for k, v in vars(engine).items() if isinstance(v, _VALUE_TYPES)}
    for name in names:
        if isinstance(getattr(type(engine), name, None), property):
            out[name] = getattr(engine, name)
    return out


@functools.lru_cache(maxsize=1)
def attr_by_field() -> dict:
    """EngineOptions field -> the Engine attribute it lands on, found by moving each field and looking.

    The attribute that moved is named `<field>` (a public knob) or `_<field>`, preferred in that
    order; otherwise it is the ONE attribute that moved (`novelty_gate` -> `_novelty_mode`,
    `comparative_lessons` -> `_comparative_lessons_on`). A field that moves nothing, or several
    attributes none of which carries its name, fails the derivation BY NAME."""
    hints = typing.get_type_hints(EngineOptions)
    fields = [f for f in dataclasses.fields(EngineOptions) if f.name not in _NOT_ON_THE_ENGINE]
    names = frozenset(n for f in fields for n in (f.name, "_" + f.name))
    out, problems = {}, []
    with tempfile.TemporaryDirectory() as tmp:
        runs = iter(range(10 ** 6))

        def landed(**kw) -> dict:
            return _landed(_mk_engine(Path(tmp) / str(next(runs)) / "run", **kw), names)

        # An attribute that differs between two IDENTICAL constructions is nobody's field.
        first, second = landed(), landed()
        noise = {k for k in set(first) | set(second) if first.get(k, _ABSENT) != second.get(k, _ABSENT)}
        for f in fields:
            beside = _PERTURBED_BESIDE.get(f.name, {})
            base = landed(**beside) if beside else first
            after = landed(**beside, **{f.name: _perturbed(f, hints[f.name])})
            moved = sorted(k for k in set(base) | set(after)
                           if k not in noise and base.get(k, _ABSENT) != after.get(k, _ABSENT))
            named = [a for a in (f.name, "_" + f.name) if a in moved]
            if named:
                out[f.name] = named[0]
            elif len(moved) == 1:
                out[f.name] = moved[0]
            else:
                problems.append(f"{f.name}: moved {moved or 'nothing'}")
    assert not problems, (
        "EngineOptions field(s) whose non-default value does not land on ONE identifiable Engine "
        f"attribute — unwired, or wired somewhere this derivation cannot name: {problems}")
    return out


def test_every_engine_options_field_is_covered(tmp_path):
    """THE PROPERTY OVER EVERY FIELD, driven: each `EngineOptions` field but the researcher-stamped
    `digest_char_cap` moves exactly one identifiable Engine attribute when given a non-default value,
    so no field is wired to nothing and none can dodge the differential comparison below.

    It used to be a coverage check over the hand-kept map, which a field wired to nothing passed as
    long as somebody had typed its row (review 2026-09-22, ENG1-03)."""
    derived = attr_by_field()
    assert set(derived) | _NOT_ON_THE_ENGINE == set(EngineOptions.__dataclass_fields__)
    engine = _mk_engine(tmp_path / "run")
    assert all(hasattr(engine, attr) for attr in derived.values())
    # …and the one field that lands elsewhere lands where it says, so the property is total.
    assert engine.researcher._digest_cap == EngineOptions().digest_char_cap
    assert _mk_engine(tmp_path / "cap", digest_char_cap=7).researcher._digest_cap == 7
    # The two irregular landings the hand map spelled out by hand, re-derived rather than typed.
    assert derived["novelty_gate"] == "_novelty_mode"
    assert derived["comparative_lessons"] == "_comparative_lessons_on"


def test_the_inert_structured_claims_knob_no_longer_reaches_the_engine(tmp_path):
    """Review 2026-09-22, ENG3-08 (doc 25 EM-06, phase 1). `cross_run_structured_claims` was
    relayed Settings -> EngineOptions -> an Engine attribute -> `structured=` on two claim
    projections that ignore it: a live knob naming a projection deleted on 2026-09-08. The relay is
    gone. The Settings field still PARSES — an old snapshot pins it False and `LOOPLAB_*` env may
    set it — and is simply not read; it leaves with the calibration digest in phase 2."""
    import pytest

    assert "cross_run_structured_claims" not in EngineOptions.__dataclass_fields__
    with pytest.raises(TypeError, match="cross_run_structured_claims"):
        _mk_engine(tmp_path / "r", cross_run_structured_claims=True)
    pinned = Settings(cross_run_structured_claims=False)          # a pre-field snapshot's pin
    assert EngineOptions.from_settings(pinned) == EngineOptions.from_settings(Settings())
    assert not hasattr(_mk_engine(tmp_path / "r2"), "_cross_run_structured_claims")


def test_the_legacy_novelty_gate_is_read_once_into_the_mode_and_not_relayed(tmp_path):
    """Review 2026-09-22, CORE-08 (ENG1-03 counted it too). `novelty_gate=True` is the legacy alias
    that forces `_novelty_mode = "algo"` at construction, and it was ALSO stored as `_novelty_gate`,
    which nothing in the tree reads — a relay whose only reader was this file's then hand-kept
    `ATTR_BY_FIELD` row. The alias still works; the map — derived since ENG1-03 — names the attribute
    the knob actually moves."""
    eng = _mk_engine(tmp_path / "r", novelty_gate=True, novelty_mode="llm")
    assert eng._novelty_mode == "algo"
    assert not hasattr(eng, "_novelty_gate")
    assert attr_by_field()["novelty_gate"] == "_novelty_mode"


def test_task_facets_finalize_is_fresh_default_off_and_maps_explicit_opt_in():
    assert Settings().task_facets_finalize is False
    assert EngineOptions().task_facets_finalize is False
    assert EngineOptions.from_settings(
        Settings(task_facets_finalize=True)
    ).task_facets_finalize is True


def test_from_settings_matches_old_cli_kwarg_mapping(tmp_path):
    # Non-default values spread across the subsystems (parallelism, budgets, trust, lessons,
    # novelty, holdout, search, repair, confirm).
    settings = Settings(
        max_parallel=3,
        # Spell the CANONICAL widths explicitly. Their product default is now `0` = startup AUTO,
        # which resolves against the detected GPU count — a differential that let both sides AUTO
        # would compare hardware, not the mapping, and would silently shadow the legacy
        # `max_parallel` above (canonical wins over legacy whenever it is set).
        eval_parallel=2,
        llm_parallel=4,
        timeout=7.5,
        max_eval_timeout=90.0,
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
        # Keep a positive depth inert in this mapping-only test. Receipt-backed Card authority is
        # exercised by the dedicated runtime-gate suite, not by the EngineOptions differential.
        card_driven_selection=False,
        speculation_depth=4,
        task_facets_finalize=True,
        # SEVEN ROWS THAT PAID FOR THEMSELVES AND WERE NEVER SPENT (2026-09-08). CLAUDE.md named
        # the (then hand-kept) `ATTR_BY_FIELD` row as the cost that keeps this differential
        # covering a new knob, but
        # a row alone buys nothing here: the comparison is old-kwarg Engine vs options Engine, so a
        # field absent from BOTH blocks, or present at its default in both, is compared as its
        # default against itself. Severing `syscall_fence` and `llm_cost_limit` in the tree left
        # this file green — the run's USD reserve cap disabled, unnoticed. (Their dedicated suites
        # do catch it, which is what keeps this a coverage-attribution defect and not a live one.)
        stage_check_tools=False,
        llm_cost_limit=1.5,
        llm_token_limit=1000,
        model_arms={"cheap": "m@0.5"},
        novelty_literature=True,
        steady_state_build=True,
        syscall_fence="mutators",
        # The four run-level policy knobs a Strategist rebuild now reads off the engine (SCJ-01).
        asha_eta=5,
        asha_rung_nodes=6,
        mcts_cost_weight=0.5,
        mcts_value_weight=0.4,
    )

    # (a) the OLD explicit-kwarg style: the literal Settings->Engine mapping cli.py::_engine used
    # before the collapse (copied from `git show HEAD:looplab/cli.py`, object seams elided).
    old = _mk_engine(
        tmp_path / "old",
        max_parallel=settings.max_parallel,
        timeout=settings.timeout,
        max_eval_timeout=settings.max_eval_timeout,
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
        concurrent_research_repeat=settings.concurrent_research_repeat,
        concurrent_research_interval_s=settings.concurrent_research_interval_s,
        concurrent_research_max_calls=settings.concurrent_research_max_calls,
        concurrent_consolidate=settings.concurrent_consolidate,
        report_every=settings.report_every,
        merge_mode=settings.merge_mode,
        complexity_cue=settings.complexity_cue,
        budget_aware=settings.budget_aware,
        failure_reflection=settings.failure_reflection,
        watchdog_reflection=settings.watchdog_reflection,
        deep_repair=settings.deep_repair,
        inline_repair=settings.inline_repair,
        inline_repair_attempts=settings.inline_repair_attempts,
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
        memo_verdict_cue=settings.memo_verdict_cue,
        lesson_operator_scope=settings.lesson_operator_scope,
        workdir_audit=settings.workdir_audit,
        trace_llm_io=settings.trace_llm_io,
        reflection_priors=settings.reflection_priors,
        comparative_lessons=settings.comparative_lessons,
        lessons_every=settings.lessons_every,
        lessons_refresh_every=settings.lessons_refresh_every,
        track_hypotheses=settings.track_hypotheses,
        surrogate_explore=settings.surrogate_explore,
        unified_agent=settings.unified_agent,
        agent_drives_actions=settings.agent_drives_actions,
        card_driven_selection=settings.card_driven_selection,
        speculation_depth=settings.speculation_depth,
        # Part IV/V flags now ship ON in Settings; pass them through so the old explicit-kwarg
        # mapping reproduces the same engine as from_settings (else old=library-default False).
        concept_pivot=settings.concept_pivot,
        graded_novelty=settings.graded_novelty,
        cross_run_concepts=settings.cross_run_concepts,
        concept_run_base=settings.concept_run_base,
        cross_run_curation=settings.cross_run_curation,
        task_facets_finalize=settings.task_facets_finalize,
        cross_run_advisory=settings.cross_run_advisory,
        cross_run_read_tools=settings.cross_run_read_tools,
        fingerprint_universal=settings.fingerprint_universal,
        # Training monitor ships ON in Settings (advisory); pass it through so the explicit-kwarg engine
        # matches from_settings (else old=library-default False vs new=True). Same for the two watchdog
        # KILLS and the canonical parallelism pair, all of which became product defaults on 2026-08-04
        # while the bare-library EngineOptions deliberately stayed conservative (see
        # tests/test_options_divergence.py for the frozen table of those intended gaps).
        eval_parallel=settings.eval_parallel,
        llm_parallel=settings.llm_parallel,
        train_monitor=settings.train_monitor,
        train_monitor_kill=settings.train_monitor_kill,
        asha_live=settings.asha_live,
        asha_live_kill=settings.asha_live_kill,
        asha_live_quantile=settings.asha_live_quantile,
        asha_live_min_siblings=settings.asha_live_min_siblings,
        # …and the single-command divergence watchdog, ON in Settings and OFF in the bare library
        # for the reason frozen in tests/test_options_divergence.py (kill authority).
        single_command_divergence_watch=settings.single_command_divergence_watch,
        # …and the proposal-derived width (docs/29 F1), ON in Settings and OFF in the bare library
        # for the reason frozen in tests/test_options_divergence.py.
        proposal_width=settings.proposal_width,
        gpu_footprint_cue=settings.gpu_footprint_cue,
        # …and the run-level systemic-failure stop, ON in Settings (3) and OFF in the bare
        # library (0) for the reason frozen in tests/test_options_divergence.py.
        systemic_failure_stop=settings.systemic_failure_stop,
        developer_crash_pause_after=settings.developer_crash_pause_after,
        node_open_budget_floor_usd=settings.node_open_budget_floor_usd,
        # …and F1i's cadence precondition, ON in Settings and OFF in the bare library for the reason
        # frozen in tests/test_options_divergence.py (the product may spend on a Strategist consult
        # or a classifier pass beside a running GPU; a direct `Engine(...)` may not gain that unasked).
        cadence_while_evaluating=settings.cadence_while_evaluating,
        # …and the plan's endgame reserve (doc 52 row 18), ON in Settings (0.2) and 0 in the bare
        # library for the reason frozen in tests/test_options_divergence.py.
        endgame_reserve_frac=settings.endgame_reserve_frac,
        # …and the judges' evidence fence (review 2026-09-22, TAT-02), ON in Settings and OFF in
        # the bare library for the reason frozen in tests/test_options_divergence.py (a prompt flag).
        evidence_envelope=settings.evidence_envelope,
        # …and the repair context as the engine's record (review 2026-09-22, ENG2-14), ON in
        # Settings and OFF in the bare library for the same frozen reason (a prompt flag).
        repair_context_record=settings.repair_context_record,
        # …and the seven above, so the differential compares a NON-DEFAULT value on both sides.
        stage_check_tools=settings.stage_check_tools,
        llm_cost_limit=settings.llm_cost_limit,
        llm_token_limit=settings.llm_token_limit,
        model_arms=settings.model_arms,
        novelty_literature=settings.novelty_literature,
        steady_state_build=settings.steady_state_build,
        syscall_fence=settings.syscall_fence,
        asha_eta=settings.asha_eta,
        asha_rung_nodes=settings.asha_rung_nodes,
        mcts_cost_weight=settings.mcts_cost_weight,
        mcts_value_weight=settings.mcts_value_weight,
    )

    # (b) the NEW single-bundle style.
    new = _mk_engine(tmp_path / "new", options=EngineOptions.from_settings(settings))

    mismatches = {attr: (getattr(old, attr), getattr(new, attr))
                  for attr in attr_by_field().values()
                  if getattr(old, attr) != getattr(new, attr)}
    assert not mismatches, f"old-kwarg vs options engines diverge: {mismatches}"
    # digest_char_cap is stamped onto the researcher, not stored on the engine.
    assert old.researcher._digest_cap == new.researcher._digest_cap == 1234

    # …AND EVERY NON-DEFAULT VALUE ACTUALLY MOVED THE ATTRIBUTE, which the differential above
    # structurally cannot check. Both engines are built through the same `Engine.__init__`, so
    # severing a wiring breaks BOTH sides identically and the comparison still matches: with
    # `self._syscall_fence = "off"` and `self._llm_cost_limit = 0.0` hard-coded in the tree — the
    # syscall fence and the run's USD reserve cap both disabled — this file stayed green. So the
    # (then hand-kept) `ATTR_BY_FIELD` row, which CLAUDE.md named as the cost that keeps a new knob
    # covered here, bought nothing on its own. This is the half that makes it real, and it is TOTAL rather than
    # the six-line spot-check it replaces: every field given a non-default `Settings` value above
    # must leave its engine attribute off the bare-library default.
    #
    # Compared against `EngineOptions()`'s default rather than for equality with the Settings
    # value, because several are coerced on the way in (`model_arms` is parsed, `syscall_fence` is
    # stringified, the widths are settled) — "it moved" is the property a wiring test owns, and
    # what each attribute becomes is its own module's business.
    library = EngineOptions()
    fresh = Settings()
    unmoved = []
    for field, attr in sorted(attr_by_field().items()):
        asked = getattr(settings, field, None)
        if asked == getattr(fresh, field, None):
            continue                       # left at its default above: this rule says nothing
        if not hasattr(library, field):
            continue
        if asked == getattr(library, field):
            # UNDECIDABLE, not skipped for convenience: the fixture asked for a value that HAPPENS
            # to equal the bare-library default (`card_driven_selection=False` against a Settings
            # default of True), so "the attribute holds the library default" cannot tell a wired
            # knob from an unwired one. The differential and the spot-checks below still cover
            # these; what this rule adds is the case where the two defaults differ.
            continue
        if getattr(new, attr, None) == getattr(library, field):
            unmoved.append(f"{field} -> {attr}")
    assert not unmoved, (
        "a non-default Settings value did not reach its engine attribute — the knob is unwired and "
        f"the differential cannot see it (both engines share `__init__`): {unmoved}")

    # Spot-check a few of the deliberately non-default values actually made it through (guards
    # against a both-sides-defaults false pass).
    # `max_parallel`/`parallel_build` are read-through aliases for the canonical widths, so the
    # canonical values above are what they report (the legacy `max_parallel=3` is deliberately shadowed).
    assert new._eval_parallel == 2 and new._llm_parallel == 4
    assert new.max_parallel == 2 and new.parallel_build == 4
    assert new.timeout == 7.5 and new.max_eval_timeout == 90.0
    assert new._policy_name == "evolutionary" and new.max_nodes == 17
    assert new.trust_gate == "gate" and new._holdout_fraction == 0.4
    assert new._inline_repair_attempts == 6 and new._seed_mode == "tracked"
    assert new.lessons_every == 7 and new._novelty_epsilon == 0.2
    assert new.card_driven_selection is False
    assert new.speculation_depth == 4
    assert new._task_facets_finalize is True


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


def test_the_launch_record_is_one_frozen_bundle_that_later_rewrites_do_not_touch(tmp_path):
    """Review 2026-09-22, ENG1-03 step 4a. What an Engine was LAUNCHED with had no home once
    `__init__` returned: the kwarg-over-options resolution was a closure, and 17 knob attributes are
    rewritten after construction (a Strategist swap, a control override, a re-entry pin), so the
    launch value survived only where nothing had overwritten it. `engine.options` is that value, once,
    frozen — resolved exactly as `_opt` resolves (explicit kwarg > options field > default)."""
    import pytest
    from types import SimpleNamespace

    opts = EngineOptions(timeout=99.0, max_nodes=50)
    eng = _mk_engine(tmp_path / "a", options=opts, timeout=3.5, memory_dir=None)
    assert eng.options == dataclasses.replace(opts, timeout=3.5, memory_dir=None)
    assert opts.timeout == 99.0, "the caller's bundle is not the record and is never rewritten"
    with pytest.raises(dataclasses.FrozenInstanceError):
        eng.options.timeout = 1.0
    eng.timeout = 7.0                        # what `_apply_strategy`/a control override does
    assert eng.timeout == 7.0 and eng.options.timeout == 3.5
    assert _mk_engine(tmp_path / "bare").options == EngineOptions()
    # A duck-typed bundle still resolves field by field, as the closure did.
    duck = SimpleNamespace(**{f.name: getattr(opts, f.name) for f in dataclasses.fields(EngineOptions)})
    assert _mk_engine(tmp_path / "duck", options=duck, timeout=3.5).options == eng.options


def test_speculation_depth_is_bounded_and_default_run_start_bytes_stay_legacy(tmp_path):
    off = _mk_engine(tmp_path / "spec-off")
    assert off.speculation_depth == 0
    assert "speculation_depth" not in off._run_start_pinned_values()

    enabled = _mk_engine(tmp_path / "spec-on", speculation_depth=5)
    assert enabled.speculation_depth == 5
    # Depth without Card authority is inert config, not a durable speculation prefix. Persisting it
    # would force the fail-closed re-entry guard to treat an ordinary run as receipt-backed evidence.
    assert "speculation_depth" not in enabled._run_start_pinned_values()

    clamped = _mk_engine(tmp_path / "spec-clamped", speculation_depth=999)
    assert clamped.speculation_depth == 64


def test_default_options_reproduce_bare_engine(tmp_path):
    bare = _mk_engine(tmp_path / "bare")
    dflt = _mk_engine(tmp_path / "dflt", options=EngineOptions())
    for attr in ("max_parallel", "timeout", "max_eval_timeout", "sweep_timeout_mult", "confirm_top_k",
                 "_holdout_fraction", "_merge_mode", "trust_gate", "_novelty_epsilon",
                 "_inline_repair_attempts", "_track_hypotheses", "lessons_every",
                 "_debug_depth", "memory_dir", "_seed_mode"):
        assert getattr(bare, attr) == getattr(dflt, attr), attr


def test_the_salvage_policy_reaches_the_engine_from_settings(tmp_path):
    """THE WIRING, driven end to end — `Settings -> EngineOptions -> settle_mode -> Engine`.

    Nothing asked the CONSTRUCTOR for a salvage mode: `tests/test_metric_salvage.py` set the
    attribute after construction, and the differential above compares two engines that both take the
    class default. Measured: commenting out the two assignments in `orchestrator.py::__init__` left
    all 29 cases in those two files green while `Engine(metric_salvage="off")` silently ran `audit` —
    i.e. an operator who switched the feature OFF still got salvaged nodes, and the run's own
    config snapshot said otherwise.
    """
    for mode in ("off", "audit", "select"):
        eng = _mk_engine(tmp_path / f"s-{mode}",
                         options=EngineOptions.from_settings(Settings(metric_salvage=mode)))
        assert eng.metric_salvage == mode, "the Settings value never reached the engine"
    # The explicit keyword beats the bundle, like every other knob.
    assert _mk_engine(tmp_path / "kw", metric_salvage="off",
                      options=EngineOptions.from_settings(Settings(metric_salvage="select"))
                      ).metric_salvage == "off"
    # …and the repair half, which is a PAID Developer call per salvaged node.
    assert _mk_engine(tmp_path / "rep-off",
                      options=EngineOptions.from_settings(
                          Settings(metric_salvage_repair=False))).metric_salvage_repair is False
    assert _mk_engine(tmp_path / "rep-on").metric_salvage_repair is True


def test_an_unrecognised_salvage_mode_settles_at_the_engine_boundary(tmp_path):
    """`settle_mode` is applied by `Engine.__init__`, not by the reader — a library caller, a
    resumed snapshot from a newer binary, or a Strategist can hand over junk, and junk must never
    mean the PERMISSIVE rung. The bare `Engine(...)` path has no Settings validator in front of it,
    so this is the only place that holds."""
    assert _mk_engine(tmp_path / "junk", metric_salvage="SELECT").metric_salvage == "audit"
    assert _mk_engine(tmp_path / "none", metric_salvage=None).metric_salvage == "audit"
