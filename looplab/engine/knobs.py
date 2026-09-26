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

  * on a real Engine the attribute is in the INSTANCE dict — `settle_knobs`, the first thing
    `Engine.__init__` does after resolving `self.options`, lands all of them there — and an instance
    attribute always wins over a non-data descriptor, so every read, and every later rewrite
    (`_apply_strategy`, a control override, a re-entry pin, a test's `eng._x = ...`), behaves exactly
    as the plain attribute it was. The 130 `_opt` locals and 130 assignments that used to land them,
    spread over `__init__`, are gone (step 4c); their comments moved here verbatim;
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

    Declared in `Engine.__init__`'s old landing order. The comments beside the declarations moved
    here VERBATIM from `Engine.__init__` when the assignments did (review 2026-09-22, ENG1-03 step
    4c), so "here", "above" and "below" in them point into that body; each knob's field in
    `engine/options.py` carries the rest of its why."""

    # --- the search budget and its cadences
    n_seeds = Knob("n_seeds")
    _policy_name = Knob("policy_name")
    _ablate_every = Knob("ablate_every")
    strategist_every = Knob("strategist_every", lambda v: max(1, v))
    concept_retag_every = Knob("concept_retag_every", lambda v: max(1, v))
    # STORED RAW: 0 is OFF here (every other interval knob reads 0 that way too), so a clamp
    # would turn "never stop the run for me" into "stop after one failure".
    systemic_failure_stop = Knob("systemic_failure_stop")
    # STORED RAW as well; `_developer_crash_pause_due` settles a junk value to the historical 1.
    developer_crash_pause_after = Knob("developer_crash_pause_after")
    # The node-OPEN floor under the spend ceiling (`_refuse_node_open_below_floor`). Raw: 0 and
    # junk both read as OFF there, and a positive value is only ever compared, never clamped.
    node_open_budget_floor_usd = Knob("node_open_budget_floor_usd")
    # STORED RAW, deliberately — this was `max(0, deep_research_every)` until 2026-08-07, and
    # under the new spelling that clamp is exactly backwards: `0` now means "start immediately"
    # and OFF is NEGATIVE, so it would have converted every spelled-off knob into a paid think at
    # every node. The whole settling rule is stated once, in
    # `engine/cadence.py::deep_research_window`, and applied at the two gates that read this
    # attribute — so `-1` (off), a junk value (off) and `0` (immediate) all mean here exactly
    # what the operator wrote, and the diagnostics that echo the knob do not lie about it.
    # (Hence `_opt` inline: with no transform left, the local it used to be resolved into buys
    # nothing — `tests/test_source_scan_helper.py` is the guard that says so.)
    deep_research_every = Knob("deep_research_every")
    concurrent_research = Knob("concurrent_research")
    # Repeated concurrent research (don't idle a multi-day eval): the overlapped think re-runs on
    # an adaptive time cadence for the whole window instead of once. Off in the library default
    # (one-shot == today); the product turns it on. Interval floors the budget-derived pace;
    # max_calls is a per-window LLM backstop. See _spawn_research / _research_overlap_loop.
    _concurrent_research_repeat = Knob("concurrent_research_repeat", bool)
    _concurrent_research_interval_s = Knob("concurrent_research_interval_s",
                                           lambda v: max(1.0, float(v or 1800.0)))
    _concurrent_research_max_calls = Knob("concurrent_research_max_calls",
                                          lambda v: max(0, int(v or 0)))
    # Overlap the hypothesis-board consolidation with the eval too (dedup the board the repeated
    # research keeps filling). Off in the library default (== today); product turns it on.
    _concurrent_consolidate = Knob("concurrent_consolidate", bool)
    report_every = Knob("report_every", lambda v: max(0, v))
    _endgame_reserve_frac = Knob("endgame_reserve_frac", lambda v: float(v or 0.0))
    # doc 52 row 19: the model ARMS the bandit may route a build to — `{arm: (model, cost)}`;
    # the configured Developer model is the implicit `default` arm. Inert without
    # `operator_bandit`, which is the policy's knob, and without a declared arm.
    _model_arms = Knob("model_arms", _parse_model_arms)
    # --- proposal cues and repair
    _complexity_cue = Knob("complexity_cue")
    _budget_aware = Knob("budget_aware")
    # Q-3: the node-budget line in the proposal prompt (`proposal_cues._cue_node_budget`).
    _node_budget_cue = Knob("node_budget_cue", bool)
    # Q-3: the fitted proposal brief. Read by three cues here and stamped per proposal onto the
    # Researcher as `_brief_fit` (`proposal_cues._stamp_brief_switches`) for `roles._state_brief`.
    _propose_brief_fit = Knob("propose_brief_fit", bool)
    # doc 67 67.1: the board's verdict support, stamped per proposal onto the Researcher as
    # `_verdict_support` (`proposal_cues._stamp_brief_switches`) for `roles._state_brief`.
    _card_verdict_support = Knob("card_verdict_support", bool)
    # doc 67 67.4: the ablation refiner's probes, stamped onto the Researcher as
    # `_ablation_probe_hint` for the ONE refine proposal (`engine/ablation.py::_ablate`).
    _ablation_probe_hint = Knob("ablation_probe_hint", bool)
    _failure_reflection = Knob("failure_reflection")
    _watchdog_reflection = Knob("watchdog_reflection")
    _deep_repair = Knob("deep_repair")
    # The repair context as the engine's own record (review 2026-09-22, ENG2-14 / ES2-05). Read by
    # `shared.py::repair_context_record`, the one place the repair path learns it.
    _repair_context_record = Knob("repair_context_record", bool)
    # Hybrid in-node crash repair (triage + inline repair). See Settings.inline_repair.
    _inline_repair = Knob("inline_repair")
    _inline_repair_attempts = Knob("inline_repair_attempts",
                                   lambda v: max(0, int(v)))   # 0 = no operator cap
    # F8: how many durable repairs before the CRITIC is asked whether the chain is
    # circling. It is a cadence, not a bound — the critic can only stop, never extend.
    _repair_critic_after = Knob("repair_critic_after", lambda v: max(0, int(v)))
    # The repeated-failure floor: how many identical failure signatures in a row, across repairs,
    # end the chain (`repair_judgment.repeated_failure_stop`). 0 = off.
    _inline_repair_same_failure_limit = Knob("inline_repair_same_failure_limit",
                                             lambda v: max(0, int(v)))
    _inline_repair_reasons = Knob("inline_repair_reasons", lambda v: tuple(v or ("crash",)))
    _inline_repair_retrain_cap = Knob("inline_repair_retrain_cap", lambda v: max(0, int(v)))
    _dep_install_timeout = Knob("dep_install_timeout", float)
    # Agent governance (Settings.agent_control): per-setting allow-list of which roles may change it
    # at runtime. A setting absent from the map is LOCKED (no agent). Enforced at the strategist /
    # boss / researcher seams via `_agent_may`. `None` (a bare Engine(...) with no options) resolves
    # to the SHIPPED default matrix — so a directly-constructed engine behaves like a real CLI run
    # (the EngineOptions "Engine() == shipped defaults" invariant); pass an explicit `{}` to lock
    # every knob against the agents.
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
    # The other run-level POLICY knobs, held here so a Strategist rebuild hands the new policy
    # the values the launch did (`search/policy.py::policy_knobs`, review 2026-09-22 SCJ-01).
    # Raw on purpose: the policy factories coerce and clamp, as they do for the launch's kwargs.
    _asha_eta = Knob("asha_eta")
    _asha_rung_nodes = Knob("asha_rung_nodes")
    _mcts_cost_weight = Knob("mcts_cost_weight")
    _mcts_value_weight = Knob("mcts_value_weight")
    _research_verify = Knob("research_verify", bool)
    # D8 PUSH half: `roles._state_brief` renders the latest memo's SUMMARY into every Researcher,
    # crash-triage and repair-critic prompt, and the verifier never checks a summary — so the cue
    # carries the memo's own CLAIM tally beside it. Threaded exactly like `_digest_cap` above:
    # setattr on the researcher (registry `roles.RESEARCHER_HINT_ATTRS`, so every wrapper mirrors
    # it) for the two propose paths, and an engine attribute for the three call sites that are
    # engine methods (`crash_repair._ask_triage`/`_ask_repair_critic`, `node_build._choose_action`).
    _memo_verdict_cue = Knob("memo_verdict_cue", bool)
    # Read at ONE place, `lessons_priors.py::operator_scoped_prior` — the engine attribute exists
    # so a build worker can ask without reaching for Settings (doc 52 §4.3). Off = the Developer
    # prior is the run-wide text, byte for byte.
    _lesson_operator_scope = Knob("lesson_operator_scope", bool)
    _workdir_audit = Knob("workdir_audit", bool)
    # ADR-17 capture policy for THIS run's tracer (below). None = declare nothing and let the
    # process-wide `set_llm_capture` default decide, exactly as before this knob existed.
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
    # docs/29 F1: whether the PROPOSALS may re-pin this run's width (`_settle_proposal_width`).
    _proposal_width = Knob("proposal_width", bool)
    # …and whether the cue that ASKS for those footprints states what a larger one really does.
    # TWO deliveries, because the same false claim was in two prompts: the engine's own GPU BUDGET
    # cue (`proposal_cues._gpu_budget_hint_text`, an engine attribute) AND the code-owned
    # `roles._FOOTPRINT_GUIDANCE` suffix both said a larger count buys nothing. Threaded onto the
    # researcher exactly like `_memo_verdict_cue` (registry `roles.RESEARCHER_HINT_ATTRS`, so every
    # wrapper mirrors it) — the two propose paths read it there, and an UNSTAMPED role keeps the
    # historical clause, which is what makes `false` and "no engine at all" the same prompt.
    _gpu_footprint_cue = Knob("gpu_footprint_cue", bool)
    _cross_run_read_tools = Knob("cross_run_read_tools", bool)
    _phase_handoff_summary = Knob("phase_handoff_summary", bool)
    _reflection_priors = Knob("reflection_priors")
    # M6 comparative lessons: credit-assigned pair distillation (run-end and, when the
    # cadences are set, mid-run into/from the SHARED cross-run store — the live-share seam).
    _comparative_lessons_on = Knob("comparative_lessons")
    lessons_every = Knob("lessons_every", lambda v: max(0, v))
    lessons_refresh_every = Knob("lessons_refresh_every", lambda v: max(0, v))
    _track_hypotheses = Knob("track_hypotheses")
    _surrogate_explore = Knob("surrogate_explore")
    unified_agent = Knob("unified_agent")
    # The Card authority wins when both opt-in selectors are enabled. Letting the
    # free-form agent arm pre-empt it would silently bypass the atomic existing-work claim below.
    card_driven_selection = Knob("card_driven_selection", bool)
    # B1's forced exploitation sits ABOVE the authority order rather than inside one selector:
    # it is the same rule whichever picker is enabled, and an arm that measured it only on the
    # Card path would be measuring the Card path. Clamped to [0, 1) -- a quantile of 1.0 would
    # name an empty top slice and read as "off" while looking like the strongest setting there
    # is, which is the shape of defect this file keeps finding in its own knobs.
    exploit_strong_node_quantile = Knob("exploit_strong_node_quantile",
                                        lambda v: min(0.999, max(0.0, float(v or 0.0))))
    # B2's read side: the propose prior gains the measured regime block. The ledger is written
    # either way; this decides only whether a model is shown it.
    regime_prior = Knob("regime_prior", bool)
    # --- spend caps, evaluation and its watchdogs
    _llm_cost_limit = Knob("llm_cost_limit")
    _llm_token_limit = Knob("llm_token_limit")
    max_eval_timeout = Knob("max_eval_timeout")
    # Eval stall watchdog cap (seconds); 0 disables. Threaded into command_eval and surfaced to the
    # Developer so its code can emit periodic progress to avoid a false silence-kill.
    eval_stall_timeout_s = Knob("eval_stall_timeout_s", float)
    # The single-command path's deterministic divergence stop, DECLARED here because
    # `eval_dispatch._run_eval` used to read it through a `getattr(..., False)` on a name nothing
    # ever assigned — the exact silent-typo shape `tests/test_engine_attribute_sites.py` refuses.
    _single_command_divergence_watch = Knob("single_command_divergence_watch", bool)
    # Most extra wall clock a live-log judge may buy for a stage at its deadline, ONCE per
    # command. 0 (default) = the historical unconditional tree-kill. See
    # `Settings.eval_deadline_grace_s` for the 22.0 discarded GPU-hours and for why it is opt-in.
    eval_deadline_grace_s = Knob("eval_deadline_grace_s", float)
    # F1d RUN-LEVEL DECLARED ENVIRONMENT. Copied, never aliased: `EngineOptions` is frozen but
    # its dict is not, and `_repin_declared_env` REPLACES this on a resume with what
    # `run_started` recorded (invariant #6) — mutating the caller's Settings dict from here
    # would rewrite the launch config object a UI process may still be serving.
    _eval_env = Knob("eval_env", lambda v: dict(v or {}))
    # The eval canary's switch (`engine/eval_canary.py`); inert without a task `eval.canary`.
    _eval_canary = Knob("eval_canary", bool)
    _train_monitor = Knob("train_monitor", bool)
    _train_monitor_interval_s = Knob("train_monitor_interval_s")
    _train_monitor_kill = Knob("train_monitor_kill", bool)
    _train_monitor_kill_confidence = Knob("train_monitor_kill_confidence")
    # Whether the monitor/ASHA judges may LOOK (tools/log_tools.py) instead of only being handed
    # a slice. Read by `train_monitor.monitor_log_tools`, the ONE place the two watchdogs build
    # their provider, so both honour one switch.
    _train_monitor_tools = Knob("train_monitor_tools", bool)
    # Whether the monitor is shown the watched stage's own declared contract and the engine's
    # live schedule reading. Read by `_monitor_training`, the ONE place that builds the tick's
    # user message. Its own switch and not `train_monitor_tools`': that one buys paid round
    # trips, this one buys nothing but two sentences on a call already being made.
    _train_monitor_contract = Knob("train_monitor_contract", bool)
    # Whether the CRASH/TIMEOUT TRIAGE judge may LOOK at the dead eval's stage logs instead of
    # diagnosing from `_eval_failure_text`'s 500-char stderr tail. Read by
    # `train_monitor.repair_log_tools`, the ONE place the repair path builds its provider — the
    # same shape as the line above, and deliberately its own switch: the watchdog's tools are paid
    # on a TIMER up to ~200 times per node, this one is paid once per failed attempt.
    _repair_log_tools = Knob("repair_log_tools", bool)
    # Whether the INTER-STAGE CHECKER may LOOK at the checked stage's own log instead of judging
    # from `run.out[-4000:]`. Read by `train_monitor.stage_check_tools`, the ONE place
    # `eval_stages._stage_check_fn` builds its provider — the fourth gate over the one
    # `_log_query_tools` derivation, and its own switch: this judge is paid once per checked
    # stage on the eval-blocking path, and it is the one that can end a node (doc 52 row 9).
    _stage_check_tools = Knob("stage_check_tools", bool)
    # The untrusted-evidence FENCE on those judges' tool results — the checker above, both
    # watchdog judges and the LLM novelty adjudicator (review 2026-09-22, TAT-02). Read by
    # `shared.py::judge_evidence_kwargs`, the one place a judge learns its fence.
    _evidence_envelope = Knob("evidence_envelope", bool)
    # ASHA live-curve rank watchdog and its kill: both ON in the product `Settings`, both OFF in this
    # library's `EngineOptions` (off == today).
    _asha_live = Knob("asha_live", bool)
    _asha_live_kill = Knob("asha_live_kill", bool)
    _asha_live_quantile = Knob("asha_live_quantile", float)
    _asha_live_min_siblings = Knob("asha_live_min_siblings", lambda v: max(1, int(v)))
    # Minimum confidence the LLM stop-verdict needs before the rank flag may actually kill. The judge
    # is consulted only INSIDE the rank gate, so this can only ever narrow the stop set.
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
    # Sandbox tier for the command-eval path (ADR-13, Phase 4): "untrusted" wraps each
    # eval in `docker run --network none` (real isolation for an arbitrary framework);
    # "trusted_local" runs it directly. The solution.py path uses self.sandbox instead.
    trust_mode = Knob("trust_mode")
    docker_image = Knob("docker_image")
    # Resource caps for the untrusted/hostile command-eval Docker tier (make_docker_wrap).
    # Mirror the solution.py DockerSandbox tier so both untrusted tiers bound memory/cpu.
    sandbox_memory = Knob("sandbox_memory")
    sandbox_cpus = Knob("sandbox_cpus")
    # Container root-filesystem hardening for that same tier ("" = off; see
    # `Settings.sandbox_readonly_rootfs`). Threaded into `make_docker_wrap` beside mem/cpus.
    sandbox_readonly_rootfs = Knob("sandbox_readonly_rootfs")
    _seed_mode = Knob("seed_mode", lambda v: v or "auto")   # run-wide fallback for per-editable seeding
    # Source-tree READ FENCE policy (off|warn|deny) — read by `engine/resources.py`, which
    # materializes the fence lazily on the first eval and stamps its marker into the child env.
    # It is the counterpart to `_seed_mode`: seeding decides what a node's copy CONTAINS, this
    # decides that the copy is the only place the node may read from.
    _read_fence = Knob("read_fence", lambda v: v or "deny")
    # METRIC SUBJECT rung (off|audit|require) — read by `engine/eval_dispatch.py` (which hands
    # `run_command_eval` the declared subject), by `engine/eval_stages.py` (which derives the
    # protected score stage's `needs` from it) and by `engine/evaluate.py` (which folds the
    # record onto the terminal and, under `require`, mints the violation). Settled through the
    # module's own vocabulary so an unknown rung from another binary's snapshot degrades to the
    # conservative one rather than silently to the strictest.
    metric_subject = Knob("metric_subject", settle_metric_subject_mode)
    # AUTO-CAPTURED EXTRA METRICS (see `Settings.auto_extra_metrics`). Read by
    # `engine/evaluate.py` at the ONE place the `node_evaluated` payload is built, which is the
    # only place an undeclared number can enter the record. A WRITE-side rung only: the fold
    # never consults it, so it can never change how an already-recorded run replays.
    auto_extra_metrics = Knob("auto_extra_metrics", bool)
    # Kernel read ALLOW-LIST (off|enforce). Read by `engine/resources.py`, which derives the
    # allow-list from the operator's declared mounts and stamps it into the child env; the
    # boundary itself is applied in the child, between fork and exec.
    _landlock = Knob("landlock", lambda v: str(v or "off"))
    # The syscall policy (`runtime/seccomp.py`), stamped beside the allow-list by
    # `engine/resources.py::_fenced_env`; applied in the child by an exec'd launcher.
    _syscall_fence = Knob("syscall_fence", lambda v: str(v or "off"))
    # --- confirmation, the noise floor, the holdout and the verifier tie-break
    confirm_seed_base = Knob("confirm_seed_base", lambda v: max(0, int(v)))
    # THE EVAL NOISE FLOOR (doc 52 row 11). Coerced the way `confirm_seed_base` above is, and
    # the `< 2` clamp is the SETTING's stated rule rather than a silent one: a single repeat has
    # no spread, so 1 is off exactly as 0 is, and `_noise_floor_due` never has to re-decide it.
    eval_noise_seeds = Knob("eval_noise_seeds", _noise_floor_repeats)
    _holdout_select = Knob("holdout_select", bool)
    _holdout_top_k = Knob("holdout_top_k", lambda v: max(1, int(v)))
    _select_verifier = Knob("select_verifier", bool)
    _verifier_ci_tie = Knob("verifier_ci_tie", bool)
    _select_verifier_samples = Knob("select_verifier_samples", lambda v: max(1, int(v)))
    # The FRACTION defines the split every search metric is scored against, so it must be pinned
    # in the event log (like trust_gate / holdout_select) — on resume the recorded value is
    # re-used (see run()), so a changed live setting can't silently make pre/post-resume metrics
    # incomparable. `_build_holdout_idx` rebuilds the partition from a fraction.
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


def settle_knobs(engine) -> None:
    """Land every declared knob on *engine* from `engine.options`, in the INSTANCE dict.

    Called once, at the top of `Engine.__init__`, so a constructed Engine holds each knob exactly as
    the assignments this replaced did — same values, a fresh container per engine, and the instance
    value winning over the descriptor for every later read and rewrite."""
    options = engine.options
    for name, knob in KNOBS.items():
        engine.__dict__[name] = knob.settled(options)
