"""The Engine's pure-config KNOBS, declared once: attribute, `EngineOptions` field, settle rule.

Review 2026-09-22, ENG1-03 step 4. `EngineOptions` has one field per engine knob, and before this
module each knob's landing on the engine was spelled only as a pair of statements inside the 880-line
`Engine.__init__` — `x = _opt("x")` near the top, `self._x = bool(x)` a few hundred lines further
down — so where a knob lands, and what it settles to, was a fact recorded nowhere a reader, a test or a
double could ask. Its consequences were measured in step 2: 34 `getattr(<engine>, "<knob>", default)`
reads carried their own copy of the default, and the doubles that reach them (`Engine.__new__` stubs,
duck-typed engines) ran a knob set no real Engine has.

HOW. `EngineKnobs` is a class of the `Engine` family whose body declares one `Knob` per engine
attribute that is a function of ONE `EngineOptions` field alone:

    _train_monitor = Knob("train_monitor", bool)

A `Knob` is a NON-DATA descriptor (no `__set__`), and that is the whole compatibility argument:

  * on a real Engine the attribute is in the INSTANCE dict (`Engine.__init__` lands it), and an
    instance attribute always wins over a non-data descriptor — so every read, and every later
    rewrite (`_apply_strategy`, a control override, a re-entry pin, a test's `eng._x = ...`), behaves
    exactly as the plain attribute it was;
  * on an object of the family that never ran `Engine.__init__` — an `Engine.__new__(Engine)`
    stub — a read settles the field from the object's own `options` when it has one, else from the
    library defaults `EngineOptions()`, and keeps the value on the instance. A stub therefore reads
    the knob set a real bare `Engine(...)` has: not an AttributeError that a blind handler turns into
    a different path, and not a `getattr` default nobody keeps in step.

`tests/test_engine_knobs.py` holds the declaration to what `Engine.__init__` lands, over the bare and
product option sets, every field moved alone and every falsy spelling the constructor accepts; and
it holds the declared set plus `EXPLICIT_IN_INIT` to be EXACTLY the attributes the fields land on
(`tests/test_engine_options.py::attr_by_field`), so a new knob is either declared here or explicit
with its reason — never a silent third thing.

`EXPLICIT_IN_INIT` is every knob attribute that is NOT a `Knob`, and why: it reads more than its own
field, or the box, the task or the Developer, or the process's working directory; or it is already
declared at class level elsewhere in the family (`tests/test_engine_member_homes.py` allows one home
per name); or its reader is registered in `engine/attribute_sites.py::UNSETTLED_KNOB_DEFAULTS`, where
a MISSING value deliberately means "not knowable" — a descriptor would answer a stub with the library
value instead and silently retire the sentinel.

This module imports only leaves (`core`, `engine/options.py`, and the two settle rules it names).
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from looplab.core.config import default_agent_control
from looplab.engine.options import EngineOptions
from looplab.runtime.metric_subject import settle_mode as settle_metric_subject_mode

# The library defaults a stub settles from. Shared, because every settle rule below either returns an
# immutable scalar or builds a NEW container (`dict(...)`, `tuple(...)`, `parse_model_arms`,
# `default_agent_control()`), so no stub can reach into another's value through it.
LIBRARY_DEFAULTS = EngineOptions()


class Knob:
    """A non-data descriptor: the engine attribute ONE `EngineOptions` field settles to."""

    __slots__ = ("field", "settle", "name")

    def __init__(self, field: str, settle: Optional[Callable[[Any], Any]] = None):
        self.field = field
        self.settle = settle
        self.name = ""

    def __set_name__(self, owner, name: str) -> None:
        self.name = name

    def settled(self, options) -> Any:
        """What `options.<field>` settles to — the rule `Engine.__init__` applies to that field."""
        value = getattr(options, self.field)
        return value if self.settle is None else self.settle(value)

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        # Reached only when the instance holds no value of its own: an object of the family that
        # never ran `Engine.__init__`. Kept on the instance, so the attribute is stable once read —
        # a stub that mutates `_eval_env` mutates ONE dict, as it would on a real engine.
        value = self.settled(obj.__dict__.get("options") or LIBRARY_DEFAULTS)
        obj.__dict__[self.name] = value
        return value

    def __repr__(self) -> str:
        return f"Knob({self.field!r})"


def _parse_model_arms(value):
    from looplab.search.policy import parse_model_arms   # search is heavier than this leaf needs
    return parse_model_arms(value)


def _noise_floor_repeats(value) -> int:
    """`eval_noise_seeds` as `Engine.__init__` settles it: 0 and 1 both mean OFF (one repeat has no
    spread), so `engine/noise_floor.py::_noise_floor_due` never re-decides it."""
    repeats = max(0, int(value))
    return repeats if repeats >= 2 else 0


class EngineKnobs:
    """One `Knob` per engine attribute that is a pure function of ONE `EngineOptions` field.

    Declared in `Engine.__init__`'s landing order. The WHY of each knob — what it gates, why its
    default is what it is, what reads it — is the comment beside its assignment in `Engine.__init__`
    and its field in `engine/options.py`; the rule here is only the settle."""

    # --- the search budget and its cadences
    n_seeds = Knob("n_seeds")
    _policy_name = Knob("policy_name")
    _ablate_every = Knob("ablate_every")
    strategist_every = Knob("strategist_every", lambda v: max(1, v))
    concept_retag_every = Knob("concept_retag_every", lambda v: max(1, v))
    systemic_failure_stop = Knob("systemic_failure_stop")                  # STORED RAW: 0 is off
    developer_crash_pause_after = Knob("developer_crash_pause_after")      # settled by its reader
    node_open_budget_floor_usd = Knob("node_open_budget_floor_usd")
    deep_research_every = Knob("deep_research_every")                      # STORED RAW: -1 is off
    concurrent_research = Knob("concurrent_research")
    _concurrent_research_repeat = Knob("concurrent_research_repeat", bool)
    _concurrent_research_interval_s = Knob("concurrent_research_interval_s",
                                           lambda v: max(1.0, float(v or 1800.0)))
    _concurrent_research_max_calls = Knob("concurrent_research_max_calls",
                                          lambda v: max(0, int(v or 0)))
    _concurrent_consolidate = Knob("concurrent_consolidate", bool)
    report_every = Knob("report_every", lambda v: max(0, v))
    _endgame_reserve_frac = Knob("endgame_reserve_frac", lambda v: float(v or 0.0))
    _model_arms = Knob("model_arms", _parse_model_arms)
    # --- proposal cues and repair
    _complexity_cue = Knob("complexity_cue")
    _budget_aware = Knob("budget_aware")
    _failure_reflection = Knob("failure_reflection")
    _watchdog_reflection = Knob("watchdog_reflection")
    _deep_repair = Knob("deep_repair")
    _inline_repair = Knob("inline_repair")
    _inline_repair_attempts = Knob("inline_repair_attempts", lambda v: max(0, int(v)))  # 0 = no cap
    _repair_critic_after = Knob("repair_critic_after", lambda v: max(0, int(v)))
    _inline_repair_reasons = Knob("inline_repair_reasons", lambda v: tuple(v or ("crash",)))
    _inline_repair_retrain_cap = Knob("inline_repair_retrain_cap", lambda v: max(0, int(v)))
    _dep_install_timeout = Knob("dep_install_timeout", float)
    # `None` (a bare `Engine(...)`) resolves to the SHIPPED governance matrix; `{}` locks every knob.
    _agent_control = Knob("agent_control",
                          lambda v: dict(v) if v is not None else default_agent_control())
    _localize_faults = Knob("localize_faults")
    _feature_engineering = Knob("feature_engineering")
    _ablate_code_blocks = Knob("ablate_code_blocks")
    # --- trust and novelty
    proxy_kill_fraction = Knob("proxy_kill_fraction")
    reward_hack_detect = Knob("reward_hack_detect")
    trust_gate = Knob("trust_gate")          # REFUSED unless audit|gate|block — by `Engine.__init__`
    _code_leakage_detect = Knob("code_leakage_detect")
    _critic_check = Knob("critic_check")
    _redact_output = Knob("redact_output")
    _novelty_epsilon = Knob("novelty_epsilon")
    _novelty_semantic = Knob("novelty_semantic", bool)
    _novelty_semantic_threshold = Knob("novelty_semantic_threshold", float)
    _debug_depth = Knob("debug_depth", lambda v: max(1, int(v)))
    _operator_bandit = Knob("operator_bandit", bool)
    # The run-level policy knobs a Strategist rebuild hands the new policy — raw, the factories clamp.
    _asha_eta = Knob("asha_eta")
    _asha_rung_nodes = Knob("asha_rung_nodes")
    _mcts_cost_weight = Knob("mcts_cost_weight")
    _mcts_value_weight = Knob("mcts_value_weight")
    _research_verify = Knob("research_verify", bool)
    _memo_verdict_cue = Knob("memo_verdict_cue", bool)
    _lesson_operator_scope = Knob("lesson_operator_scope", bool)
    _workdir_audit = Knob("workdir_audit", bool)
    # None = declare nothing and defer to the process-wide capture default.
    _trace_llm_io = Knob("trace_llm_io", lambda v: None if v is None else bool(v))
    # --- Part IV/V, cross-run memory and the prompt surface
    _coverage_context = Knob("coverage_context", bool)
    _cadence_while_evaluating = Knob("cadence_while_evaluating", bool)
    _concept_pivot = Knob("concept_pivot", bool)
    _graded_novelty = Knob("graded_novelty", bool)
    _novelty_literature = Knob("novelty_literature", bool)
    _steady_state_build = Knob("steady_state_build", bool)
    _capability_expansion = Knob("capability_expansion", bool)
    _fingerprint_universal = Knob("fingerprint_universal", bool)
    _cross_run_concepts = Knob("cross_run_concepts", bool)
    _concept_run_base = Knob("concept_run_base", bool)
    _cross_run_advisory = Knob("cross_run_advisory", bool)
    _cross_run_curation = Knob("cross_run_curation", bool)
    _task_facets_finalize = Knob("task_facets_finalize", bool)
    _cross_run_curation_auto = Knob("cross_run_curation_auto", bool)
    _concept_tidy = Knob("concept_tidy", bool)
    _proposal_width = Knob("proposal_width", bool)
    _gpu_footprint_cue = Knob("gpu_footprint_cue", bool)
    _cross_run_read_tools = Knob("cross_run_read_tools", bool)
    _phase_handoff_summary = Knob("phase_handoff_summary", bool)
    _reflection_priors = Knob("reflection_priors")
    _comparative_lessons_on = Knob("comparative_lessons")
    lessons_every = Knob("lessons_every", lambda v: max(0, v))
    lessons_refresh_every = Knob("lessons_refresh_every", lambda v: max(0, v))
    _track_hypotheses = Knob("track_hypotheses")
    _surrogate_explore = Knob("surrogate_explore")
    unified_agent = Knob("unified_agent")
    card_driven_selection = Knob("card_driven_selection", bool)
    # Clamped to [0, 1): a quantile of 1.0 names an empty top slice.
    exploit_strong_node_quantile = Knob("exploit_strong_node_quantile",
                                        lambda v: min(0.999, max(0.0, float(v or 0.0))))
    regime_prior = Knob("regime_prior", bool)
    # --- spend caps, evaluation and its watchdogs
    _llm_cost_limit = Knob("llm_cost_limit")
    _llm_token_limit = Knob("llm_token_limit")
    max_eval_timeout = Knob("max_eval_timeout")
    eval_stall_timeout_s = Knob("eval_stall_timeout_s", float)
    _single_command_divergence_watch = Knob("single_command_divergence_watch", bool)
    eval_deadline_grace_s = Knob("eval_deadline_grace_s", float)
    _eval_env = Knob("eval_env", lambda v: dict(v or {}))          # copied, never aliased
    _train_monitor = Knob("train_monitor", bool)
    _train_monitor_interval_s = Knob("train_monitor_interval_s")
    _train_monitor_kill = Knob("train_monitor_kill", bool)
    _train_monitor_kill_confidence = Knob("train_monitor_kill_confidence")
    _train_monitor_tools = Knob("train_monitor_tools", bool)
    _train_monitor_contract = Knob("train_monitor_contract", bool)
    _repair_log_tools = Knob("repair_log_tools", bool)
    _stage_check_tools = Knob("stage_check_tools", bool)
    _evidence_envelope = Knob("evidence_envelope", bool)
    _asha_live = Knob("asha_live", bool)
    _asha_live_kill = Knob("asha_live_kill", bool)
    _asha_live_quantile = Knob("asha_live_quantile", float)
    _asha_live_min_siblings = Knob("asha_live_min_siblings", lambda v: max(1, int(v)))
    _asha_live_kill_confidence = Knob("asha_live_kill_confidence")
    sweep_timeout_mult = Knob("sweep_timeout_mult", lambda v: max(1.0, v))
    confirm_top_k = Knob("confirm_top_k")
    confirm_seeds = Knob("confirm_seeds")
    max_seconds = Knob("max_seconds")
    max_eval_seconds = Knob("max_eval_seconds")
    memory_dir = Knob("memory_dir")
    require_approval = Knob("require_approval")
    archive_resolution = Knob("archive_resolution")
    # --- the sandbox tier and the fences
    eval_trust_mode = Knob("eval_trust_mode")
    trust_mode = Knob("trust_mode")
    docker_image = Knob("docker_image")
    sandbox_memory = Knob("sandbox_memory")
    sandbox_cpus = Knob("sandbox_cpus")
    sandbox_readonly_rootfs = Knob("sandbox_readonly_rootfs")
    _seed_mode = Knob("seed_mode", lambda v: v or "auto")
    _read_fence = Knob("read_fence", lambda v: v or "deny")
    metric_subject = Knob("metric_subject", settle_metric_subject_mode)   # junk -> the conservative rung
    auto_extra_metrics = Knob("auto_extra_metrics", bool)
    _landlock = Knob("landlock", lambda v: str(v or "off"))
    _syscall_fence = Knob("syscall_fence", lambda v: str(v or "off"))
    # --- confirmation, the noise floor, the holdout and the verifier tie-break
    confirm_seed_base = Knob("confirm_seed_base", lambda v: max(0, int(v)))
    eval_noise_seeds = Knob("eval_noise_seeds", _noise_floor_repeats)
    _holdout_select = Knob("holdout_select", bool)
    _holdout_top_k = Knob("holdout_top_k", lambda v: max(1, int(v)))
    _select_verifier = Knob("select_verifier", bool)
    _verifier_ci_tie = Knob("verifier_ci_tie", bool)
    _select_verifier_samples = Knob("select_verifier_samples", lambda v: max(1, int(v)))
    _holdout_fraction = Knob("holdout_fraction", float)


# attribute -> its Knob, read off the class body so the table cannot drift from the declaration.
KNOBS: dict[str, Knob] = {name: knob for name, knob in vars(EngineKnobs).items()
                          if isinstance(knob, Knob)}

# Every knob attribute that is NOT a `Knob`, and why (see the module docstring).
EXPLICIT_IN_INIT: dict[str, str] = {
    "_auto_install_deps": "reads `trust_mode` as well: installing is a trusted_local-tier act only",
    "_eval_parallel": "settled against the BOX and the task (AUTO = one experiment per GPU) and "
                      "rewritten live by a Strategist, a control override or a re-entry pin",
    "_llm_parallel": "resolved against the settled eval width (AUTO), then rewritten live",
    "max_parallel": "a read-through property over `_eval_parallel`",
    "parallel_build": "a read-through property over `_llm_parallel`",
    "_merge_mode": "`auto` resolves by the Developer's capability (`is_code_generating`)",
    "_novelty_mode": "two fields: the legacy `novelty_gate` alias forces `algo`",
    "agent_drives_actions": "two fields: inert without `unified_agent`",
    "speculation_depth": "AUTO (-1) resolves against the settled eval width, the roles and the "
                         "policy, and the admission envelope reads the resolved integer",
    "speculation_gate_receipt": "resolved to an absolute path against the working directory",
    "metric_salvage": "a class attribute of `EvaluateMixin` already, settled by its own rule",
    "metric_salvage_repair": "a class attribute of `EvaluateMixin` already",
    "max_nodes": "`orchestrator.py::_hard_node_reservation_limit` fails CLOSED on an object "
                 "missing it (UNSETTLED_KNOB_DEFAULTS)",
    "timeout": "`shared.py::effective_eval_time_budget` says nothing for an object missing it "
               "(UNSETTLED_KNOB_DEFAULTS)",
}
