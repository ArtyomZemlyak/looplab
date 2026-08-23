"""Engine config: the schema (what settings exist). (I0, ADR-11)

One pydantic-settings class declaring every engine knob, its default, and its docs. The loader —
how a run file / CLI / env becomes a `Settings` — is `looplab.core.appconfig`, which also owns
the config-precedence order. A resolved, secret-masked snapshot is written next to each run for
reproducibility. (No real secrets in P0, but the masking discipline is in place.)
"""
from __future__ import annotations

import logging
import re
import types
import typing
from collections.abc import Mapping
from pathlib import Path

from pydantic import Field, PrivateAttr, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Single sources shared with the LLM resolver — see core/llm.py.
from looplab.core.llm import AGENT_STAGE_KEYS, DEFAULT_HEADER_TIMEOUT_S
from looplab.core.models import FAILURE_REASONS

_LOG = logging.getLogger(__name__)

# Default home for cross-run memory + the knowledge base (shared across every run). Both are ON by
# default now (a user asked for it) so a fresh install accumulates + consults knowledge with no setup;
# set the env var to "" to disable, or to a path to relocate. `~/.looplab/` keeps them out of any one
# project + shared across projects.
_LL_HOME = Path.home() / ".looplab"

# What one `llm_profiles` entry may contain. Deliberately closed: an unrecognized field is a typo the
# operator would otherwise never see fail, and the map is a public artifact (config.snapshot.json,
# HTTP, LOOPLAB_*), so it must never accrete free-form keys.
_PROFILE_FIELDS = frozenset({"model", "base_url", "temperature", "api_key_env"})
# A variable name that `runtime/sandbox.py::SECRET_ENV` will recognize as holding a secret and strip
# from generated code's environment. Duplicated here, not imported: layering forbids `core` from
# importing `runtime`. `tests/test_secret_env_pattern.py` holds the two in agreement.
_SECRET_ENV_NAME = re.compile(
    r"\A(?=[A-Z][A-Z0-9_]{0,63}\Z)(?=.*(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL)).*\Z")


def _is_numeric_annotation(ann) -> bool:
    """True when a field's annotation admits int/float but NOT bool.

    Used by `Settings._reject_bool_for_numeric_fields`: pydantic resolves annotations at class build,
    so `model_fields[...].annotation` is a real type object here even under
    `from __future__ import annotations`. Optionals are numeric when every non-None member is
    (`int | None` yes, `int | str` no), so a boolean can never slip in through a union arm."""
    if ann is bool:
        return False                                  # a bool field legitimately takes a bool
    if ann is int or ann is float:
        return True
    args = [a for a in typing.get_args(ann) if a is not type(None)]
    origin = typing.get_origin(ann)
    if args and origin in (typing.Union, types.UnionType):
        return all(_is_numeric_annotation(a) for a in args)
    return False

# Autonomous roles that may be granted per-setting write access (see Settings.agent_control).
# strategist = A7 meta-controller (run-wide); boss = run-chat operator-proxy (run-wide);
# researcher = per-experiment proposer (per-node sizing, e.g. eval timeout for a heavy model).
AGENT_ROLES = ("strategist", "boss", "researcher")

# A foresight score is computed for every acted-on proposal. Keep this lower than the generic
# verifier's emergency ceiling so a typo cannot multiply paid synchronous calls across a run.
MAX_FORESIGHT_VERIFY_SAMPLES = 8


# Layer-2 parallelism renamed two public settings without removing the durable legacy fields. Keep
# the equivalence in one place: config-source precedence, governance migration, and live strategy
# application must all agree which names describe the same authority/value.
PARALLELISM_ALIASES: dict[str, str] = {
    "max_parallel": "eval_parallel",
    "parallel_build": "llm_parallel",
}


def parallelism_aliases(setting: str) -> tuple[str, ...]:
    """Return every spelling in ``setting``'s alias family, canonical name first."""
    for legacy, canonical in PARALLELISM_ALIASES.items():
        if setting in (legacy, canonical):
            return canonical, legacy
    return (setting,)


def canonicalize_parallelism_source(
    values: dict, *, promote_build_to_llm_parallel: bool = False,
) -> dict:
    """Promote legacy parallel keys *within one config-precedence source*.

    Applying this only after file/CLI/env were flattened would let a lower-priority canonical env
    value shadow a higher-priority legacy file value (or a file canonical value shadow a CLI legacy
    override), so callers with sub-layers normalize each layer independently.

    ``max_parallel``/``eval_parallel`` are true aliases (pure eval width, no extra semantics) and are
    always promoted. ``parallel_build``/``llm_parallel`` are NOT: a positive ``llm_parallel`` also flips
    on the finite shared LLM broker (``orchestrator._startup_llm_total``), while legacy ``parallel_build``
    means "legacy build width, unbounded overlap, no shared total". So promoting a legacy-only
    ``parallel_build`` layer (e.g. just ``LOOPLAB_PARALLEL_BUILD=1`` in env) would silently serialize
    every provider call — contradicting llm_broker.py's docstring, orchestrator's "legacy-only preserves
    unbounded overlap" note, the broker-neutral live-control path, and the configuration.md ``llm_parallel``
    row ("falls back ... without enabling a shared total"). Config/startup loads therefore keep broker
    opt-in tied to an EXPLICITLY-spelled canonical ``llm_parallel``; the orchestrator's own width fallback
    (``llm_parallel if not None else parallel_build``) still carries the legacy build width. Only the
    Strategist live-control path (``canonicalize_strategy_parallelism``) opts into
    ``promote_build_to_llm_parallel``, where ``parallel_build`` is a deliberate full alias of the
    canonical knob (see strategy.py ``_apply_strategy``).
    """
    if not isinstance(values, dict):
        return values
    out = dict(values)
    for legacy, canonical in PARALLELISM_ALIASES.items():
        if canonical == "llm_parallel" and not promote_build_to_llm_parallel:
            continue
        # None is the durable legacy mode for llm_parallel, not a missing value. A normal snapshot
        # contains llm_parallel:null plus parallel_build:1; promoting null to 1 would unexpectedly enable
        # the global one-call broker. Promote aliases only when the canonical key is absent, and pin
        # snapshot round-trip parity. Canonicalize inside each precedence layer, never across the
        # flattened result, so the alias migration cannot invert CLI > file > env precedence.
        if canonical not in out and out.get(legacy) is not None:
            out[canonical] = out[legacy]
    return out


def flatten_parallelism_layers(layers) -> dict:
    """Flatten same-tier precedence layers (LOWEST priority first) and resolve the legacy pair.

    PRECEDENCE REPAIR for the non-promoted `parallel_build`/`llm_parallel` pair. Declining to promote
    in `canonicalize_parallelism_source` is right (a legacy-only layer must not flip on the shared
    broker), but it let a LOWER-priority canonical value survive a HIGHER-priority legacy override:
    file `llm_parallel: 2` + CLI `-s parallel_build=8` flattened to BOTH keys, and the orchestrator
    prefers `llm_parallel`, so the operator's explicit CLI width was silently discarded. When a higher
    layer names the legacy width over a lower layer's canonical one, mask with the LEGACY SENTINEL
    (`llm_parallel=None` is the durable "legacy mode — build width from parallel_build, no shared
    total"), so the higher layer wins with the semantics it asked for.

    The mask lives HERE, not in the per-layer canonicalizer, because only a flatten knows the layer
    ORDER. Stamping it per layer meant any layer naming `parallel_build` emitted `llm_parallel=None`
    even when NO layer in the tier set the canonical key — and since pydantic-settings ranks init
    kwargs above env, that sentinel then silently overrode a `LOOPLAB_LLM_PARALLEL` the operator had
    exported as a deliberate provider rate/cost ceiling. Precedence is per FIELD: a tier that never
    spelled `llm_parallel` has not set it, and it must fall through to env/.env/defaults — which is
    exactly what `build_settings`' docstring promises ("the merged dict wins only where it actually
    sets a value").
    """
    merged: dict = {}
    legacy_at = canonical_at = -1
    for index, layer in enumerate(layers):
        if not isinstance(layer, dict):
            continue
        merged.update(layer)
        if layer.get("parallel_build") is not None:
            legacy_at = index
        if "llm_parallel" in layer:
            canonical_at = index
    # Only when this tier DID set the canonical key, at a strictly lower layer than the legacy one.
    if canonical_at >= 0 and legacy_at > canonical_at:
        merged["llm_parallel"] = None
    return merged

# Settings whose semantics are committed by ``run_started`` and restored from the folded event log
# on every re-entry.  Changing one only in ``config.snapshot.json`` would mix incompatible evidence
# (a different holdout split) or make the live producer disagree with replay's selection policy.
# The HTTP config editor and the engine both consume this one contract; keep deliberately mutable
# controls such as ``trust_gate`` out (that field has its own durable change event).
#
# MEMBERSHIP IS ABOUT THE ``run_started`` RECORD, NOT ABOUT THE FOLDED FIELD OF THE SAME NAME. The two
# stopped being the same thing for ``speculation_depth`` when the AUTO depth became allowed to ratchet
# itself down mid-run: `RunState.speculation_depth` is the EFFECTIVE treatment (pin ∧ every
# `speculation_depth_settled` row) and only `RunState.speculation_depth_pinned` is what run start
# committed. `run_start_pinned_settings` reads the pin for exactly that reason. A field whose value is
# allowed to move after run start and has NO run-start pin of its own does not belong in this set at
# all — that is why ``trust_gate`` and the two settled widths are out.
RUN_START_PINNED_FIELDS = frozenset({
    "card_driven_selection",
    "speculation_depth",
    "holdout_fraction",
    "holdout_select",
    "select_verifier",
    "select_verifier_samples",
    "verifier_ci_tie",
})


# The one pinned field with a LAUNCH-TIME SENTINEL, and therefore the only one whose
# `config.snapshot.json` value is not in the same domain as what ``run_started`` recorded.
# ``speculation_depth = -1`` is AUTO — "let the box decide" — and the pin is what ONE launch, on ONE
# box, resolved that request to. The two do not disagree: re-entry resolves AUTO again and then adopts
# the log (`Engine._require_pinned_speculation_receipt`), exactly as it does for the two AUTO widths.
# Normalizing the sentinel away replaces the operator's standing request with one run's resolution and
# cannot be undone — a PUT restoring ``-1`` comes back 422 as a pinned-field change, while an unrelated
# PUT had already overwritten it. So the sentinel is not drift and is never repaired.
_NO_AUTO_SENTINEL = object()
RUN_START_PINNED_AUTO_SENTINELS: dict[str, object] = {"speculation_depth": -1}


def run_start_pinned_disagreement(field: str, value, pinned) -> bool:
    """Does ``value`` genuinely contradict what ``run_started`` pinned for ``field``?

    THE SINGLE SPELLING of that question, shared by the HTTP config editor's three consumers — the
    read-only overlay's mismatch list, the refuse-list for an attempted change, and the legacy-snapshot
    repair. They were three separate ``!=`` comparisons, which is how the AUTO sentinel came to be
    treated as drift by all three at once (see `RUN_START_PINNED_AUTO_SENTINELS`).
    """
    if value == pinned:
        return False
    return value != RUN_START_PINNED_AUTO_SENTINELS.get(field, _NO_AUTO_SENTINEL)


def run_start_pinned_settings(state) -> dict:
    """Return the effective Settings names committed by a folded run-start record.

    Pre-pin legacy logs have no recorded holdout fraction, so their snapshot remains authoritative
    for both holdout knobs. Verifier fields, however, have always been re-pinned from the legacy-safe
    fold defaults whenever a valid ``run_started`` identity exists.

    Every value here must be one ``run_started`` ACTUALLY CARRIED. That is not a tautology for
    ``speculation_depth``: the folded field of that name is the run's EFFECTIVE treatment, which an
    adaptive `speculation_depth_settled` row is allowed to ratchet down, and reading it here reported
    a depth the run-start record never contained and then wrote it into the snapshot as though it were
    the pin. `RunState.speculation_depth_pinned` is the launch value and is what this contract owns.
    """
    if not getattr(state, "run_id", ""):
        return {}
    values = {
        # Layer 3 changes which queue owns macro-action selection. Once a run starts, replay and
        # resume must keep the same owner even if the snapshot or ambient default changes later.
        "card_driven_selection": bool(getattr(state, "card_driven_selection", False)),
        # Layer 5 overlaps Card production with evaluation only when this pinned treatment is
        # positive. Old logs omit it and therefore retain the exact serial build/eval spine. The PIN,
        # never `state.speculation_depth` — see the docstring.
        "speculation_depth": int(getattr(state, "speculation_depth_pinned", 0)),
        "select_verifier": bool(getattr(state, "select_verifier_tiebreak", False)),
        "select_verifier_samples": int(getattr(state, "select_verifier_samples", 3)),
        "verifier_ci_tie": bool(getattr(state, "verifier_ci_tie", False)),
    }
    holdout_fraction = getattr(state, "holdout_fraction", None)
    if holdout_fraction is not None:
        values.update({
            "holdout_fraction": holdout_fraction,
            "holdout_select": bool(getattr(state, "holdout_select", False)),
        })
    if not values.keys() <= RUN_START_PINNED_FIELDS:
        raise RuntimeError("folded run-start pinned settings contract drifted")
    return values

# --- Run profiles (config-first presets) -------------------------------------------------------
# A `profile` is a NAMED BUNDLE of setting defaults — nothing more. It only fills fields the user
# did NOT set explicitly (via file / --set / LOOPLAB_* env / init-kwargs), so any explicit knob
# always wins (see `_apply_profile`). Most engine intelligence lives behind individual flags. The
# PART IV/V cross-run + concept-graph machinery (concept_pivot, graded_novelty, cross_run_concepts /
# _advisory / _structured_claims / _curation / _read_tools, fingerprint_universal, foresight_verify)
# now ships ON in the product `Settings` default. That is an explicit experimental product choice,
# not a claim that a frozen A/B or workload-wide cost/quality gate has cleared: the bundle can steer
# prompts and candidate generation, change graded-novelty admission / pre-execution choices, and add
# paid LLM work when its client and memory prerequisites are present. The bare-library `EngineOptions`
# default stays lean (see tests/test_options_divergence.py — product side is the aggressive one) so a
# toy `Engine(...)` in a test doesn't fire cross-run LLM work unasked. The trust/confirm/ablate QUALITY
# bundle still ships OFF and is turned on by `thorough`. Every value here is reachable by hand — the
# profile is a convenience, never a hidden mode.
# keep this rationale behavioral and evidence-based; do not relabel read-only storage
# access or proposal-only governance as a no-op for agent behavior, cost, or proposal admission.
#
# `thorough` deliberately touches only QUALITY/TRUST machinery, not spend: it does NOT raise
# max_nodes / eval_parallel (those are cost knobs the user owns). It enables multi-seed confirmation
# (demote seed-lucky leaders), the reward-hack / leakage / critic monitors AND flips them from
# advisory to *gating* (`trust_gate="gate"` — a
# flagged win can no longer be selected as best), ablation-driven refinement, and the prompt cues
# (complexity / budget / failure-reflection / watchdog-reflection). Agentic novelty and the experimental
# Part IV/V bundle are already product defaults, not profile changes. Effect is large; marginal cost is a
# few extra evals.
PROFILES: dict[str, dict] = {
    "default": {},   # explicit no-op alias for "ship-as-is" (== omitting profile)
    "fast":    {},   # today's lean defaults, named for symmetry with `thorough`
    "thorough": {
        "confirm_top_k": 3,
        "confirm_seeds": 3,
        # novelty_gate intentionally NOT enabled here: duplicate-detection is the agentic Researcher's
        # job (it reads past experiments + find_analogous_across_runs and DECIDES), not an algorithmic
        # param-distance / embedding auto-reject that mis-fired (it nudged a good fresh idea onto a
        # twice-dead lineage — live node 61). Search stays as tools; the LLM judges novelty.
        "operator_bandit": True,   # P4: adaptive operator mix over folded yields (GreedyTree)
        "reward_hack_detect": True,
        "code_leakage_detect": True,
        "critic_check": True,
        "trust_gate": "gate",          # detectors stop gating a flagged node from WINNING (not just audit)
        "ablate_every": 3,
        "complexity_cue": True,
        "budget_aware": True,
        "failure_reflection": True,
        "watchdog_reflection": True,   # feed recent live-watchdog (train-monitor/ASHA) flags to proposals
        "reflection_priors": True,     # no-op unless memory_dir is set (cross-run priors)
    },
}


# The shipped agent-governance matrix (who may change which setting at runtime). Defined as a module
# constant so the Engine's direct-construction default matches this shipped default exactly (the
# EngineOptions "Engine() == shipped defaults" invariant) instead of diverging to an all-locked map.
DEFAULT_AGENT_CONTROL: dict[str, list[str]] = {
    "timeout": ["researcher", "strategist"],
    # Layer-2 canonical names are the presented/default governance surface. `_agent_may` falls back
    # to an old snapshot's legacy grant only when its canonical counterpart is absent, so removing
    # duplicate defaults preserves resume authority while an explicit canonical revocation still wins.
    "eval_parallel": ["strategist"],
    "llm_parallel": ["strategist"],
    # Strategy-only pseudo-setting: the closed per-lane allocation inside the canonical LLM total.
    # It is not a launch Settings field; operators pin it through `set_strategy` and the Strategist
    # may reallocate it only while this grant is present.
    "llm_lane_limits": ["strategist"],
    "max_nodes": ["strategist", "boss"],
    "max_eval_seconds": ["strategist", "boss"],
    "policy": ["strategist"],
    # The policy NAME and its PARAMS are gated independently in `_apply_strategy`; grant both by default
    # so the Strategist that may switch to (say) MCTS may also apply the `c`/`eta` it decided — else its
    # tuned params are silently dropped and the recorded `active_strategy` diverges from the live engine.
    "policy_params": ["strategist"],
    "n_seeds": ["strategist"],
    "ablate_every": ["strategist"],
    "merge_mode": ["strategist"],
    "complexity_cue": ["strategist"],
    "ablate_code_blocks": ["strategist"],
    "prefer_sweep": ["strategist"],
    "novelty_stance": ["strategist"],
    "developer": ["strategist"],
    "fidelity": ["strategist", "researcher"],
}


def default_agent_control() -> dict[str, list[str]]:
    """A FRESH deep-ish copy of the shipped governance matrix (each role list copied so a caller can
    mutate its own map without touching the module constant). The SINGLE constructor of the default,
    called by both `Settings.agent_control`'s default_factory AND the Engine's direct-construction
    default — so "Engine() == shipped defaults" can't drift if the copy depth ever needs to change
    (e.g. nested values requiring copy.deepcopy), instead of two verbatim copy expressions."""
    return {k: list(v) for k, v in DEFAULT_AGENT_CONTROL.items()}


# The closed set of `developer_backend` values: "default" (the in-house Developer) plus the external
# coding-agent keys `agents/cli_agent.py::PRESETS` implements. It lives HERE, in the bottom layer,
# because `Settings` validation needs it on every construction and `core` imports nothing above
# itself — validating against the agents registry directly made this the ONE upward import out of
# core in the tree, so an import-time error anywhere in agents/cli_agent.py broke ALL config
# loading (doc 25 XP-04).
#
# The authority is not lost, it is inverted: `agents/cli_agent.py` asserts its PRESETS match this
# set, and `tests/test_developer_backend_registry.py` checks BOTH directions. Adding a preset
# without adding it here is a red test, not a backend that silently downgrades to the default.
DEVELOPER_BACKENDS: tuple[str, ...] = ("default", "aider", "continue", "goose", "opencode")

# Runtime-only ALIASES of a backend above — the extra spellings a LIVE Developer swap accepts, which
# `Settings` deliberately does NOT (`developer_backend="llm"` is still a refusal, because an alias has
# no preset and `test_every_configurable_backend_exists` would then have to admit a silent downgrade).
# It exists because the live-swap vocabulary had THREE homes and they disagreed: `engine/strategy.py`
# offered the Strategist `["default", "llm", *PRESETS]`, `agents/factory.py::make_developer_factory`
# resolved the bare literal `"llm"` to `"default"`, and `agents/strategist.py` held a branch naming a
# fourth spelling (`"agentless"`) that appears in NO list — permanently dead, and had it ever fired,
# `validate_strategy` would have dropped the field with no refusal receipt anywhere (see that
# function's docstring for what a dropped developer costs). One registry, read by both sides.
#
# `engine/strategy.py::_available_developers` builds the offered vocabulary from these two constants
# and `make_developer_factory` resolves through this map; `tests/test_developer_backend_registry.py`
# checks BOTH directions AND scans the tree for a developer literal that is in neither, so a
# reintroduced `"agentless"` arm is a red test rather than an `if` that can never be true.
DEVELOPER_BACKEND_ALIASES: dict[str, str] = {"llm": "default"}


def developer_switch_names() -> tuple[str, ...]:
    """Every name a LIVE Developer swap may be asked for, `default` first (the in-house Developer is
    the fallback every caller degrades to when no factory is wired). Canonical names then aliases."""
    return (*DEVELOPER_BACKENDS, *DEVELOPER_BACKEND_ALIASES)


def _parser_names() -> tuple[str, ...]:
    """The structured-output parser vocabulary, read from the registry that actually resolves it.

    Deferred (not a module-level import) to keep `config` import-light, and sorted so the refusal
    message is stable rather than dependent on registry insertion order."""
    from looplab.core.parse import _ORDER

    return tuple(sorted(_ORDER))


class Settings(BaseSettings):
    """The engine settings schema (every knob a run accepts).

    Timeout family — six distinct knobs, each owned by a different subsystem:
      - `timeout`:             per-eval wall-clock budget for ONE experiment's evaluation (engine/eval).
      - `max_eval_timeout`:    hard ceiling for a governed per-node Researcher timeout override.
      - `llm_timeout`:         LLM request idle timeout — inter-token stall limit in stream mode
                               (core.llm OpenAICompatibleClient).
      - `llm_header_timeout`:  LLM first-byte (response-headers) window for stream attempts (core.llm).
      - `agent_time_budget_s`: wall-clock ceiling across a tool-using agent loop's turns (agents).
      - `dep_install_timeout`: per-package budget for auto-installing a missing dep (env self-prep).

    Config-source precedence is owned by `looplab.core.appconfig` (the loader) — see its module
    docstring for the one canonical order.
    """
    # Config precedence: see appconfig.py — the loader owns the full order. The `.env` is read
    # so `looplab run`/`looplab ui` pick up LOOPLAB_LLM_BASE_URL etc. without exporting them by hand
    # (keys MUST carry the LOOPLAB_ prefix, same as the env vars). utf-8-sig tolerates a BOM from
    # Windows editors. The test suite disables this (tests/conftest.py) so a dev's real .env in the
    # repo root can't leak into assertions.
    model_config = SettingsConfigDict(
        env_prefix="LOOPLAB_",
        env_file=".env",
        env_file_encoding="utf-8-sig",
        extra="ignore",
        allow_inf_nan=False,
    )

    @classmethod
    def settings_customise_sources(
            cls, settings_cls, init_settings, env_settings, dotenv_settings,
            file_secret_settings):
        """Preserve source precedence across canonical/legacy parallelism names.

        Pydantic ranks init > env > dotenv correctly, but it ranks field names rather than semantic
        alias families. Promoting a legacy value inside each source before the merge makes an old
        explicit snapshot/file value beat a lower-priority canonical env value. ``appconfig`` also
        promotes its file/typed/--set sub-layers before combining them into the init source.
        """
        def _with_parallel_aliases(source):
            def _read():
                values = canonicalize_parallelism_source(source())
                # Credential components are a record, not independently mergeable settings. If a
                # winning source supplies only one half, insert an explicit None for its peer so a
                # lower-priority source can never complete an attacker-controlled mixed pair.
                pair = {"llm_api_key", "llm_api_key_base_url"}
                present = pair.intersection(values)
                if present and present != pair:
                    values = dict(values)
                    values[next(iter(pair - present))] = None
                return values
            return _read

        return (
            _with_parallel_aliases(init_settings),
            _with_parallel_aliases(env_settings),
            _with_parallel_aliases(dotenv_settings),
            _with_parallel_aliases(file_secret_settings),
        )

    # Run profile (config-first preset, see PROFILES above). "default"/"fast" = today's lean
    # defaults; "thorough" turns the built quality/trust machinery ON in one word. A profile only
    # fills fields the user did NOT set — every knob it touches stays individually overridable.
    profile: str = "default"

    # Bounds (review C/⚪): a non-positive node/seed budget or timeout is never valid. Upper bounds are
    # equally load-bearing — the UI /start path writes these straight into the engine's env, and an
    # unbounded `eval_parallel` (or legacy `max_parallel`; e.g. 100000 from a crafted preflight) would
    # fan out that many sandboxes → resource exhaustion. Reject at config time, not mid-run. Ceilings
    # are generous; tests top out near 50.
    # Pydantic's lax mode coerces a JSON boolean into any numeric field (`true` -> 1), so a UI/API type
    # error used to silently collapse a run budget to ONE node instead of returning 422. Rejected for
    # EVERY numeric field by `_reject_bool_for_numeric_fields` below, not just the ones bounded here.
    n_seeds: int = Field(default=3, ge=1, le=1024)
    max_nodes: int = Field(default=8, ge=1, le=1_000_000)
    # legacy evaluation-width input. `eval_parallel` below is canonical and wins when set;
    # keep this field for old env/config/snapshots and direct Engine call sites. A width > 1 admits
    # concurrent evals, whose resource reservations may be CPU-only or span one/more GPUs — there is no
    # one-node/one-GPU promise. `max_parallel=0` is launch-time AUTO (detected GPU count, at least 1).
    max_parallel: int = Field(default=1, ge=0, le=1024)
    # legacy build-width input. `llm_parallel` below is canonical and also controls the
    # shared provider-call total when positive. A width > 1 uses fresh pooled researcher/developer pairs;
    # it says nothing about the later eval's CPU/GPU reservation. Startup `0` derives build fan-out from
    # resolved eval width; a live Strategist/operator zero settles to 1. Without role_factory it clamps to 1.
    parallel_build: int = Field(default=1, ge=0, le=64)
    # Hypothesis-card Kanban re-architecture (docs/23, Layer 2). Two INDEPENDENT concurrency axes,
    # decoupled from each other so cost (LLM work) can be tuned separately from throughput (GPU evals).
    # These are the CANONICAL names going forward; `max_parallel`/`parallel_build` above are kept as thin
    # back-compat aliases (env vars / snapshots / ~100 Engine(...) call sites depend on the exact names,
    # so they cannot be renamed — CLAUDE.md). Resolution (orchestrator.__init__): a NEW name, when set,
    # WINS over its legacy alias; None => fall back to the legacy field => byte-identical to today.
    #   `eval_parallel`: concurrent EVALS (the GPU/experiment consumer). None -> max_parallel; 0 = AUTO
    #     (one experiment per detected GPU). This is the in-Run eval-concurrency ceiling; the resource
    #     scheduler bin-packs declared footprints separately. Sibling local GPU-owning Runs serialize
    #     through the conservative host-pool lease; external execution needs its own admission boundary.
    #   `llm_parallel`: total concurrent LLM provider calls + BUILD fan-out. None -> legacy
    #     parallel_build semantics without a shared total; 0 = startup AUTO (= resolved eval_parallel
    #     for build fan-out, historical research overlap unbounded). A positive canonical value enables
    #     the named-lane broker the Strategist can reallocate at cadence.
    # Defaults changed 2026-08-04 (operator decision): `None` was not a neutral default — it fell back
    # to the LEGACY `max_parallel`/`parallel_build` (both 1), i.e. strictly serial, and for
    # `llm_parallel` it additionally left the shared named-lane broker DISABLED (the broker opts in
    # only on an explicitly-spelled positive canonical value). `0` is the AUTO setting the two axes
    # were built for: one experiment per detected GPU (at least one) and an LLM width derived the same
    # way, with the broker live so the Strategist can reallocate lanes at cadence.
    eval_parallel: int | None = Field(default=0, ge=0, le=1024)
    llm_parallel: int | None = Field(default=0, ge=0, le=64)
    # docs/29 F1 — let the PROPOSALS decide the width, not just the box. With this on, an AUTO
    # `eval_parallel` is re-pinned mid-run to how many of the currently open Cards the box can serve
    # at the `footprint.gpus` they declared (`engine/widths.py::proposal_derived_width`), through a
    # durable `run_width_settled` event so replay and resume reproduce the width instead of
    # re-deriving one off their own hardware (invariant #6). ON, because the box-derived width is
    # measurably wrong in the ordinary case rather than merely coarse: on `rubertlite-dr-unified-v5`
    # AUTO settled `eval_parallel: 2` off two H200s while both Cards declared `{"gpus": 2}`, so the
    # run held both devices for one experiment and went serial at double the per-node cost with
    # `run_started` still claiming a width of 2 (docs/29 F1b). This is the axis that makes that width
    # true. It only ever moves an AUTO axis, never an explicitly spelled one and never one an
    # operator's `budget_extend` owns, and it never widens past the launch pin — so `false` here, an
    # explicit `eval_parallel=N`, or a `budget_extend` each keep today's behaviour byte for byte.
    proposal_width: bool = True
    # The PROMPT half of `proposal_width`, and it shipped saying the opposite of what that axis does.
    # The GPU BUDGET cue (`engine/proposal_cues.py::_gpu_budget_hint_text`) told the Researcher that
    # declaring more than `pool // eval_parallel` "does NOT get this experiment more hardware … the run
    # serialises at the SAME per-experiment cost". Both halves are false against the shipped scheduler:
    # `resources.py::_resource_request_for_node` honours a declared count (declared beats AUTO),
    # `_acquire_gpus` reserves exactly that many devices and `_resource_eval_env` hands the child
    # `CUDA_VISIBLE_DEVICES=0,1` — and the per-experiment cost is precisely what changes, which is the
    # whole reason the choice exists. The v5 incident the wording was written from was a WIDTH defect
    # (the run kept claiming 2 while one node held both cards); `proposal_width` fixed it, and the
    # sentence outlived its cause. So the cue now states what a larger count actually buys and costs
    # and leaves the decision to the role (docs/36): a wider ACTION space, no wider trusted set — the
    # scheduler still clamps to the pool, the metric/champion/selection are untouched, and an operator
    # count named in the task statement still wins. Measured on `runs/e5small-dr-unified-v2`, whose
    # every node declared `{"gpus": 1}`: 4 of its 9 nodes died of `torch.OutOfMemoryError` on ONE
    # H200 (at per-device batch 8192, 2048 and 1750 — the process itself holding 139 of 139.8 GiB),
    # so on that run the second card was not a speed preference at all but the only way to hold the
    # recipe's batch. `false` restores the historical paragraph BYTE FOR BYTE (the `developer_probe` /
    # `memo_verdict_cue` same-position pattern).
    gpu_footprint_cue: bool = True
    # Training-log monitor (I-series watchdog family): a per-eval background observer that tails the live
    # training log while a (often multi-hour) declared stage runs in a worker thread. ON in the product
    # surface (Settings). Its alert event is fold-ignored and cannot directly change lifecycle/champion;
    # with watchdog_reflection on, that alert is nevertheless advice in a later Researcher
    # prompt and can change future proposals. It only fires on the command-eval path with an LLM client;
    # bare-library EngineOptions stays OFF. See test_options_divergence.
    # `_interval_s` is the BASE tick cadence (the effective one adapts to the per-experiment budget).
    train_monitor: bool = True
    train_monitor_interval_s: float = Field(default=600.0, gt=0)
    # Phase 3 — INTERVENTION (opt-in, separate from observation): let the monitor tree-kill a training the
    # LLM judges 'broken' (diverged / silent CPU fallback / not learning) EARLY, instead of burning the
    # whole budget. Off by default — the observer only WATCHES unless this is on. A kill only fires on a
    # 'broken' verdict (a plateau is 'watch', never killed) with confidence >= the threshold; the node then
    # fails normally (reason='monitor_broken'), so replay reconstructs it from that one terminal event.
    # Default flipped 2026-08-04 (operator decision): the monitor's verdict is now allowed to act.
    # It only fires on a 'broken' verdict at confidence >= train_monitor_kill_confidence (0.8);
    # a plateau is 'watch' and is never killed.
    train_monitor_kill: bool = True
    train_monitor_kill_confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    # Let the monitor's judge (and the ASHA judge beside it) QUERY the live logs instead of only being
    # handed a slice of them: `tools/log_tools.py`'s `read_log` + `metric_series`, scoped to the stage
    # logs `eval_log_plan` names. ON by default because the slice was MEASURED insufficient — the
    # digest a judge received on `runs/rubertlite-dr-unified-v7` was ~10 loss values ≈ 30 s of a
    # five-hour run, and it answered "pinned, no learning trend" about a curve that had fallen 4.73.
    # OFF restores the historical single completion byte for byte (`structured_judge` treats
    # `tools=None` as the plain `parse_structured` this always was). The cost it buys is real and
    # bounded: up to `train_monitor._MONITOR_LOOK_TURNS` extra round trips per tick, and a default
    # tool read is a 256 KiB seek (~12 ms) — see that module's measured cost table.
    train_monitor_tools: bool = True
    # Show the live monitor the watched stage's OWN DECLARED CONTRACT (`expect.assert` /
    # `expect.files`) plus the engine's deterministic reading of the schedule the trainer configured
    # (`train_monitor.stage_contract_context`). ON because the bench says this is the class the
    # judge cannot see: over the committed 450-decision corpus it calls a degenerate CURVE `broken`
    # 48 of 53 times (91 %) and a stage that EXITED 0 and was then failed by the engine on its own
    # declaration 2 of 38 times (5 %) — and 13.4 h of the 20.1 h an oracle could still save on that
    # corpus sits in that second class. It costs ZERO extra provider calls: it is text in the user
    # message of a tick the monitor already pays for, spliced at the same position pattern as
    # `stage_context`/`trajectory_context`, and `false` restores the historical message byte for
    # byte. It widens EVIDENCE only (docs/36) — `should_monitor_kill`, `should_monitor_repair`, the
    # kill-eligible roles and the deterministic trajectory veto are untouched. Deliberately NO
    # `LEGACY_CONFIG_SNAPSHOT_DEFAULTS` row, on `memo_verdict_cue`'s ground: it makes no paid call,
    # mounts no intervention, changes no concurrency and no selection policy, so a resumed run gains
    # nothing it did not consent to. (`train_monitor_tools` has a row because it DOES add paid
    # round trips; that is the distinction, not symmetry.)
    train_monitor_contract: bool = True
    # The same permission for the CRASH/TIMEOUT TRIAGE judge — the role whose whole job is "why did
    # this stage die", and the only one still deciding it from a slice. What it is handed is
    # `_eval_failure_text`'s `res.stderr[-500:]`: five hundred CHARACTERS, not the watchdog's 128 KiB.
    # ON by default because that slice was measured insufficient in the sharpest possible way —
    # `runs/rubertlite-dr-unified-v8` node 3 finished all 15 epochs (`10590/10590`, `'epoch': 14.98`)
    # and was killed 20 minutes into a RETRIEVAL bar; the 522-char `error_in` held only that second
    # bar, and the verdict read its elapsed field as training progress ("still in epoch 1 at 31:20").
    # It then prescribed a fix for a problem the node did not have. (Its `n_epochs` 15->8 never
    # landed — `repair_verify` stamped `unmet` on that row and `config.yaml` still says 15 — so the
    # experiment was not altered. This comment projected attempt 6 at 22,096 s into the same
    # 22,000 s ceiling, extrapolated live at 1.798 s/step; THE RUN FALSIFIED THAT. Attempt 6's
    # `train` passed in 19,915.75 s and the node recorded 0.762048 and became v8's champion. What
    # bought the ~2,100 s was neither the epoch cut nor the batch halving but a SECOND edit in the
    # same repair, deleting the in-`train` `test_model()` call — the full-index retrieval then ran
    # as the protected `score` stage's own 3,130.3 s. The diagnosis was still wrong about where the
    # time was going; it is the PROJECTION that is retracted, not the misdiagnosis.)
    # OFF restores the historical ask byte for byte (no `_REPAIR_LOOK_INVITATION`, no extra tools).
    # Cost, measured on node 3's real workdir (mean of 5, warm): the engine-side derivation is
    # 1.35 ms per failed attempt for the whole added path (log plan + attempt-floor snapshot +
    # source map), plus up to `UnifiedAgent._REPAIR_LOOK_TURNS` extra round trips whose tool calls
    # cost 7 ms (a `read_log` tail) to 525 ms (a whole-run hourly scan of the real 10.0 MB log).
    # Paid once per FAILED ATTEMPT, not on a timer like the watchdog's.
    repair_log_tools: bool = True
    # ASHA live-curve watchdog (sibling of the training monitor): reads the latest INTERMEDIATE value of
    # the objective metric off the live log (reusing the eval's OWN metric reader). Finished-endpoint rank
    # remains advisory. An opt-in KILL additionally requires an operator-declared metric.resource_key and
    # enough sibling observations at exactly that resource; absent comparable evidence cannot stop a run.
    # Records a fold-ignored `asha_rank` diagnostic + trace span; the library default is OFF (off == today).
    asha_live: bool = True
    # Opt-in INTERVENTION (separate from the advisory signal): let the watchdog tree-kill a node whose
    # intermediate metric stays below the SAME-RESOURCE bar past a short grace window. Off by default —
    # it only surfaces endpoint rank unless this is on. A kill fails normally (asha_underperforming).
    # Default flipped 2026-08-04 (operator decision). Still narrowly scoped by construction: it acts
    # ONLY for stdout_json metrics that declare an explicit `resource_key`, only past the grace
    # window, and only with `asha_live_min_siblings` finished peers at the SAME resource value.
    asha_live_kill: bool = True
    # The bar sits at `asha_live_quantile` along a WORST->BEST ordering of the finished siblings' finals:
    # 0.5 = the median (a node worse than the median peer flags); a SMALLER value is more conservative —
    # it lowers the bar toward the WORST finished peer, so only a node worse than nearly all peers flags
    # (0.0 = worse than the worst). `min_siblings` is the floor of finished peers required before it ranks
    # at all, so it never acts on too little evidence.
    asha_live_quantile: float = Field(default=0.5, ge=0.0, le=1.0)
    asha_live_min_siblings: int = Field(default=3, ge=1)
    # The rank test above is EVIDENCE, not the decision: once it fires past the grace window an LLM judge
    # sees the live curve, the same-resource sibling distribution + bar, the run's other live metrics and
    # the training monitor's latest health verdict, and answers continue|watch|stop. Only a 'stop' at
    # confidence >= this threshold actually kills — the judge is consulted INSIDE the rank gate, so it can
    # only ever stop FEWER nodes than the bare quantile comparison, never more. No judge (no LLM client, an
    # endpoint failure, an unparseable answer) => no kill, so the watchdog degrades to advisory.
    asha_live_kill_confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    timeout: float = Field(default=30.0, gt=0)
    # Hard ceiling for the Researcher's governed per-node `Idea.eval_timeout`. One hour preserves the
    # existing heavy/neural-net authoring example while staying well below the sandbox's defensive
    # 24-hour subprocess ceiling. This is operator-owned run config: agents may request less or more,
    # but the accepted action is always clamped here after the `agent_control.timeout` permission gate.
    max_eval_timeout: float = Field(default=3600.0, gt=0, le=24 * 3600.0)
    # Intra-node sweep: a sweep node runs a whole grid in one process, so it gets this multiple of
    # `timeout` as its wall-clock budget (solution.py path; RepoTasks use their per-profile timeout).
    sweep_timeout_mult: float = Field(default=8.0, ge=1)
    # Eval STALL watchdog CAP (seconds): a stage that is completely SILENT on stdout/stderr for this
    # long — while still alive and BELOW its wall-clock deadline — is tree-killed early with a STALLED
    # marker (a hung dataloader/deadlock dies in minutes instead of burning a multi-hour timeout). The
    # per-stage window is `min(this, the stage's own timeout)`, so a short scorer's deadline fires first.
    # `0` DISABLES the watchdog entirely (only the hard deadline applies) — for a legitimately quiet
    # non-Python stage (block-buffered stdout, a script that logs only to its own file). Threaded into
    # the eval and surfaced to the Developer so its code can emit periodic progress to stay alive.
    eval_stall_timeout_s: float = Field(default=1800.0, ge=0)
    # Eval DEADLINE GRACE (seconds): the most extra wall clock a live-log judge may buy for a stage
    # that has reached its time budget, ONCE per command. `0` (the default) keeps the unconditional
    # tree-kill this always was, byte-for-byte.
    #
    # It exists because a deadline is a number that cannot see a progress bar. All FIVE
    # `stage_finished.status == "timeout"` rows in `runs/` land within 3.2 s of their own declared
    # ceiling (14400 s x3, 21600 s, 22000 s) and together discarded 24.1 GPU-hours — re-derived
    # 2026-08-15, when v8 added two; the older count of four / 22.0 h included a row whose run
    # directory is no longer on this box. TWO of the five are the shape this rescue does NOT reach
    # and that is worth stating beside the default: v8 nodes 3 and 9 died at ceilings their own
    # manifests set 14000 s and 21600 s BELOW the operator's budget, and node 9 was 73 % done with
    # ~5160 s to go, so a one-shot grace correctly answers NOT_FINISHING and the fix is upstream at
    # the declaration (`adapters/repo_developer.py::_time_budget_note`). And the entire captured record of
    # `rubertlite-dense-retrieval` node 72 — a 12.45-hour node, five repairs, 6 h on its final
    # attempt — ends `100%|##########| 664/664 [00:17<00:00, 38.13it/s]`. At the wall, a stage two
    # seconds from writing its checkpoint and a stage that will never finish present the identical
    # fact (elapsed >= timeout); only something reading the log can separate them.
    #
    # DEFAULT OFF, deliberately, and this is the one place to argue about it: the judge reads the
    # CANDIDATE'S OWN live log, so a solution that prints a convincing progress bar can buy exactly
    # this many seconds. That is a bounded spend and nothing else — it cannot buy a metric (the
    # operator's reader over the protected `score` stage is untouched) and it cannot buy a second
    # grace. Still, it is money, so the operator opts in rather than discovering it on a bill.
    # `runtime/sandbox.py::_granted_grace` is where the clamp is enforced.
    # ON BY DEFAULT SINCE 2026-08-23, as AUTO (`-1`), on the operator's decision. It shipped opt-in
    # on the argument one line up — it is money — and a year of the corpus answered that argument:
    # NINE `stage_finished.status == "timeout"` rows across `runs/` discarded 57.6 GPU-hours, every
    # one of them landing within seconds of its own declared wall. Opt-in put the whole of that loss
    # behind a switch nobody had turned, which is the wrong default for a bounded, one-shot,
    # fail-closed rescue. `-1` rather than a number because the right ceiling is a FRACTION of the
    # stage being rescued, not a constant — see `resolve_deadline_grace` for why 1800 means three
    # different things on this repo's own stages. A resumed pre-2026-08-23 run keeps `0.0` through
    # `LEGACY_CONFIG_SNAPSHOT_DEFAULTS`; the ge bound admits the sentinel and nothing below it.
    eval_deadline_grace_s: float = Field(default=-1.0, ge=-1)
    # RUN-LEVEL DECLARED ENVIRONMENT: variables set for every eval of every node — the per-node
    # `setup`, the single `command`, and every stage on BOTH sandbox tiers, and the solution.py
    # sandbox path for a non-repo task (the composition sits above that branch, not inside it). The run-wide half of the
    # same contract the task's `cmd.env` and a stage's own `env` carry (most specific wins:
    # this < cmd.env < stages[].env), validated by the one shared rule
    # `core/envsafe.py::validate_env_map`. Deliberately NOT the run-level `run_setup`: that runs once
    # in the operator's own source tree before any node exists, so it is not an eval, and a
    # dependency install has its own environment handling in `runtime/deps.py`.
    #
    # WHY IT IS A SETTING AND NOT ONLY A SHELL EXPORT (backlog F1d). `VS_LOCAL_DATA_ROOT` being unset
    # crashed `rubertlite-dr-unified-v6` node 0 in `botocore ListObjects`; the repair was correct and
    # then node 1 hit the identical error, because a node is seeded from the SOURCE repo and never
    # from a sibling. Exporting the variable in the launching shell fixes that and is what the
    # operator had to do — but it leaves the fact that shaped every node's behaviour in someone's
    # terminal history instead of in the run record. Declared here it lands in
    # `config.snapshot.json` AND in `run_started`, so a resume reproduces the same environment
    # (engine invariant #6) and a reader of the log can see what the nodes actually ran under.
    #
    # Reachable as `-s eval_env=NAME=VALUE` (comma-separated for several) as well as the JSON/mapping
    # form a config file or `LOOPLAB_EVAL_ENV` carries — see `_eval_env_map` for why both.
    # SECRETS ARE REFUSED, by the same rule and for the durability reason stated there.
    eval_env: dict[str, str] = Field(default_factory=dict)
    # Sandbox tier (ADR-13): "trusted_local" (subprocess, no Docker) for the CLI;
    # "untrusted" (Docker --network none, shared-kernel runtime) for hosted/multi-tenant UI;
    # "hostile" (untrusted + a true-isolation OCI runtime, gVisor `runsc` by default / Kata) for
    # running untrusted code. See runtime/sandbox.py::make_sandbox for the tier selection.
    trust_mode: str = "trusted_local"
    # Image for the untrusted command-eval tier (RepoTask, Phase 4): the framework's deps
    # should be baked in (the container runs --network none, so a pip setup can't fetch).
    docker_image: str = "python:3.12-slim"
    # Resource caps for the untrusted/hostile Docker tier (passed to `docker run --memory/--cpus`).
    # The default 4g bound protects other tenants from an OOM, but a model-training MLE-bench eval
    # routinely needs more — raise it (e.g. "16g") for such runs. "" disables the cap (unbounded).
    # Ignored by the trusted_local (subprocess) tier.
    sandbox_memory: str = "4g"
    sandbox_cpus: str = ""
    # Container ROOT-FILESYSTEM hardening for the untrusted/hostile Docker tier. The value is the
    # SIZE of the writable tmpfs scratch the container gets in exchange (`/tmp`, `/var/tmp`); "" =
    # OFF = today's behaviour, an image whose whole filesystem — site-packages, /usr/bin, /etc, the
    # interpreter — is writable by root-inside-the-container. Set e.g. "1g" for `docker run
    # --read-only --tmpfs /tmp:rw,exec,nosuid,nodev,size=1g …`.
    #
    # It is OFF by default because it CAN break a legitimate eval and the repo's rule is that
    # hardening only defaults on where it cannot: an `eval.setup` running `apt-get`, anything writing
    # under `$HOME` (the HF hub's lock files, `datasets`' arrow cache, a pip cache), and — unproven,
    # because no box here has a docker daemon to drive it — the NVIDIA container runtime, which
    # writes library symlinks and re-runs `ldconfig` inside the container during `--gpus` setup.
    # `--network none` (this tier's default) already forbids the download half of the cache story,
    # which is why the flag is usable at all with a baked image.
    #
    # What it does NOT touch, deliberately: the `/work` bind is the node's own workdir on the host
    # and stays READ-WRITE — the eval's checkpoints, stage logs and the metric file the engine reads
    # back all travel through it, so a `:ro` there would delete the tier's output channel rather
    # than harden it. Per-source write protection is the separate, finer `edit:false` mount rule
    # (`make_docker_wrap(binds=…)` -> `--mount …,readonly`). Ignored by `trusted_local`, whose
    # process tier has no container filesystem; see `runtime/sandbox.py::readonly_rootfs_argv`.
    sandbox_readonly_rootfs: str = ""
    # Best-effort host-OOM guard for the TRUSTED-LOCAL (subprocess) tier. When set (e.g. "8g"), each
    # eval child gets an RLIMIT_AS address-space cap so a runaway trainer hits MemoryError instead of
    # OOM-killing the whole host (and the engine). POSIX only; a no-op on Windows. Default "" = OFF:
    # RLIMIT_AS bounds VIRTUAL memory, and CUDA/torch reserve tens of GB of virtual without using it,
    # so a default cap would spuriously kill GPU evals — leave it off for those and use the Docker
    # tier's real --memory cgroup bound (sandbox_memory) instead. Ideal for CPU/numpy/sklearn runs.
    sandbox_memory_local: str = ""
    # Best-effort disk-fill guard for the TRUSTED-LOCAL tier (P1-5): an RLIMIT_FSIZE cap on the size of
    # any single file an eval child writes (e.g. "2g"), so a runaway that writes an unbounded file gets
    # SIGXFSZ instead of filling the host disk. POSIX only. Default "" = OFF (large model checkpoints
    # need big files); set it for tasks that write only small artifacts.
    sandbox_fsize_local: str = ""
    # Search policy (ADR-2): "greedy" | "evolutionary" | "mcts" | "asha" | "bohb".
    # `bohb` is a registered policy (`search/policy.py::_REGISTRY` aliases it to the ASHA factory,
    # which BOHB reuses as its racing half) and is advertised by `appconfig.render_template` and the
    # `bohb` template kind; this comment used to omit it and undersell the valid set. The value is
    # not validated at config time, but `make_policy` raises on an unknown name — loud-late rather
    # than silent, unlike `developer_backend` above.
    policy: str = "greedy"
    # Ablation-driven refinement (I7): every N improves, ablate the best to find the
    # highest-impact parameter and refine it. 0 = off. (GreedyTree only.)
    ablate_every: int = 0
    # A0a (MLE-STAR): when ablating, treat each generated pipeline *code block* (not just a
    # numeric param) as an ablation unit — neutralize a block, measure the metric delta, refine
    # the highest-impact block. Off => the classic numeric-param ablation. Config-first knob.
    ablate_code_blocks: bool = False
    # A0b: real merge/ensembling. "mean" = legacy mean-param merge; "ensemble" = the Developer
    # writes a code-recombination ensemble over the two parents' solutions (verified: agent-proposed
    # ensembling 37.9%->43.9%). "auto" (default) = ensemble whenever the Developer actually
    # generates code (LLM/agent backends; declared via `is_code_generating`), mean for the
    # templated/toy developers where a code ensemble is meaningless — the evidence says code
    # recombination is the strongest single merge (removing it costs ~9 pp), so it is the default
    # wherever it can work. Falls back to mean when the Developer can't ensemble.
    merge_mode: str = "auto"
    # A0d (AIRA): inject a dynamic complexity hint into the draft/improve prompt keyed on the
    # node's child count (few children -> keep minimal; many -> escalate to ensembling/HPO).
    complexity_cue: bool = False
    # I1 (CAAFE-style) LLM feature-engineering: instruct the proposer/developer to add
    # semantically-meaningful engineered features (ratios, interactions, aggregations) as code. The
    # existing cross-validation eval + best-selection is the CV GATE — a feature set that doesn't
    # improve CV is never selected (feature engineering is non-universal, so this gate is mandatory).
    # Tabular tasks (those exposing column data). Off by default.
    feature_engineering: bool = False
    # A5: surface the REMAINING eval-compute budget into the proposal prompt so the Researcher can
    # reason over how much compute is left (explore broad while flush, exploit/cheapen when low).
    # No-op unless a max_eval_seconds budget is set.
    budget_aware: bool = False
    # A4 (LATS-style): feed a summary of the most recent FAILED branches (operator + error reason)
    # back into the proposal prompt so the proposer reflects on and avoids repeating them. ON by
    # default: it is SELECTIVE by construction (injects only when recent failures exist — the
    # evidenced form of reflection; reflecting at every step shows no gains, arXiv:2506.12928).
    failure_reflection: bool = True
    # Feed the LIVE-WATCHDOG observations (train-monitor health verdicts + ASHA intermediate-rank flags)
    # of recent experiments back into the proposal prompt, so the Researcher avoids re-proposing a config
    # whose TRAINING was already observed to be weak — even when the watchdog kills are off (default) and
    # the node ran to completion (those diagnostics are fold-ignored, so failure_reflection never sees
    # them). ON by default and SELECTIVE like failure_reflection (injects only when a recent flag exists).
    watchdog_reflection: bool = True
    # C3 deep test-driven repair: hand the Developer the failure taxonomy + a structured "reproduce
    # then fix" directive on debug, not just the raw stderr tail. ON by default (product surface); the
    # conservative library default in EngineOptions stays off, per that module's contract.
    deep_repair: bool = True
    # === Inline crash repair ==============================================================
    # Hybrid in-node crash repair: when an LLM-generated node CRASHES at runtime (mechanical errors
    # — bad import, removed kwarg, typo), the agent triages it and may repair the code IN PLACE within
    # the same eval (cheap; does NOT consume max_nodes and does NOT add a node to the search tree).
    # Semantic failures end the node and the policy proposes fresh work — there is no inter-node
    # debug lane behind this either way since F5 deleted the Debug node (2026-08-13). ON by default;
    # set False to restore the prior behavior (every crash ends its node with no in-place repair).
    inline_repair: bool = True
    # HARD upper limit on in-place repair attempts per node — the operator's backstop, not the
    # primary stopping rule. The primary rule is the crash-triage MODEL: it is consulted once per
    # attempt with this node's whole repair history (what failed, what each fix claimed, which files
    # it touched, how far the pipeline got) and its `abandon` verdict — "I no longer know how to fix
    # this" — stops the node (`engine/evaluate.py`). That call is not an extra cost: the loop already
    # made exactly one triage call per attempt to decide repair-vs-reject.
    #
    # 0 = NO OPERATOR CAP, which is what this shipped as and what an existing run resumes with
    # (`LEGACY_CONFIG_SNAPSHOT_DEFAULTS`, 38 of the 46 preserved run directories). It is no longer
    # the default, because a cap is the only thing that bounds the case where the judge cannot help:
    # it is wrong in the expensive direction, or the endpoint answering it is degraded rather than
    # dead (a dead one is caught — an UNREACHABLE judge stops the node and pauses the run naming the
    # provider; a live one answering something unreadable stops only the node). Under 0 a single node
    # reached 2345 `node_repaired` events over 3.5 h on a dead provider.
    #
    # 0 NO LONGER MEANS UNBOUNDED (2026-08-06). The grandfathering above stands — nobody silently
    # acquires a 12 mid-run — but behind it the engine keeps an absolute ceiling of its own,
    # `engine/evaluate.py::_UNLIMITED_REPAIR_CEILING` (50), and the terminal names which of the two
    # stopped the node. Measured on the shipped loop: with an always-`repair` judge under 0 one node
    # ran 795 repairs / 796 full evals in 45 s with no terminal — i.e. on the incident's OWN snapshot
    # (`runs/rubert-dr-0804` carries 0), this redesign still did not prevent it.
    #
    # WHY 12. It has to clear the longest legitimate chain on record with margin: `runs/rubert-dr-0805`
    # node 0 needed 8 repairs — six PyTorch-Lightning/transformers/accelerate migrations of a repo
    # whose pinned deps were a year stale, and only then two on its actual research question (a DDP
    # `find_unused_parameters` modelling decision). Driving that recorded sequence through the real
    # `_evaluate`, the node reaches its research question and produces a metric at any cap >= 8 and
    # dies at the migrations below that. 12 is that 8 plus room for a chain half again as long, and
    # is deliberately the SAME per-node worst case as the two-ledger apportionment it replaces
    # (2 x 6 = 12 measured exactly), so this redesign removes a dimension without buying more compute
    # than the design it supersedes already admitted. The operator's own ask was "inline repair on,
    # at least 5".
    #
    # SUPERSEDES the environment/experiment apportionment (commit e0ec3a4d), which bounded two
    # ledgers with this one number. A budget is about time and money — a re-eval costs the same
    # whichever kind of mistake preceded it — so `repair_class` and its corroboration are gone.
    #
    # THE DEFAULT IS NOW 0, i.e. NO OPERATOR COUNT CAP (2026-08-13, F8). Everything above stays true
    # about what the number MEANS; what changed is that a number is no longer the transition. The
    # operator's ruling: *"я бы хотел чтобы репейринг у нас был по сути бесконечный, но стопался бы
    # каким-нибудь LLM критиком и самим девелопером, что типа я фиг знает как чинить"* — and it lands
    # with the F5 deletion of the Debug node, because "give up at 12 and open a fresh node" and "give
    # up at 12" are only the same change if nothing replaces the counter.
    #
    # The reasoning for 12 above is sound and is exactly why it cannot be the stop: it is calibrated
    # against the longest chain ANYONE HAD SEEN. A count is blind to the two ways this actually goes
    # wrong — `rubert-dr-0804`'s 369 distinct error signatures on one wall (no counter saw a
    # repetition) and v6 node 5's three rounds of batch-halving on an OOM that never happened (no
    # counter came near its cap). What stops the loop now is a judgment: the triage judge, the
    # Developer's own `(developer stuck: …)`, and `engine/repair_judgment.py`'s critic. Underneath
    # them the floors are unchanged — `_UNLIMITED_REPAIR_CEILING` (50), the eval-time budget,
    # `systemic_failure_stop`, and the LLM money ceiling that raises `BudgetExceeded` at the client.
    #
    # A NON-ZERO VALUE IS STILL A HARD CAP and still wins over every judgment. An operator who wants
    # the old behaviour writes `inline_repair_attempts: 12` and gets it byte-for-byte.
    inline_repair_attempts: int = Field(default=0, ge=0)
    # F8's CRITIC: how many durable repairs this node must already have before a second model is
    # asked whether the chain is circling. 0 = no critic (the loop stops on triage + the floors,
    # exactly as it did before 2026-08-13).
    #
    # 3 rather than 1 because the critic's question is not askable earlier and asking it anyway costs
    # money to be told nothing: with one attempt behind it there is no trajectory to compare, and
    # `agents/unified_agent.py::repair_critic` refuses an empty one outright. 3 is also below both
    # recorded disasters — v6 node 5's batch-halving was visible at three attempts, and 0804's wall
    # was visible long before its 2,345th.
    repair_critic_after: int = Field(default=3, ge=0)
    # Which failure reasons (`engine/triage.py::FAILURE_REASONS`) are eligible for inline repair.
    # Default: ALL of them, since 2026-08-12.
    #
    # It used to be `("crash", "timeout", "oom")` — the mechanical three — on the reasoning that a
    # timeout or OOM means the code was too slow or too memory-hungry for the budget, not that the
    # idea is wrong. That reasoning was right and the CONCLUSION was wrong, because the same is true
    # of every other reason: `no_metric` is a script that ran and printed nothing where it was asked
    # to, `setup` is a dependency that is not installed, `drift` is an implementation that wandered
    # off its own declared spec. None of those is evidence that the HYPOTHESIS is wrong, which is the
    # only thing that should end a node without a repair.
    #
    # The cost of the narrow default, measured: `rubertlite-dr-unified-v5` node 0 trained for 76
    # minutes on two H200s, exited 0, wrote a complete checkpoint and computed recall@100 = 0.743 —
    # then died with `reason: no_metric` and ZERO repair attempts, because the manifest declared the
    # checkpoint one directory over from where the testbed writes it. The repair was a one-line path
    # edit. The gate never opened, and the run lost the node.
    #
    # This is not "repair everything forever": three tighter bounds already govern the spend, and
    # they are the ones doing the real work. `inline_repair_attempts` caps the COUNT,
    # `inline_repair_retrain_cap` caps the EXPENSIVE ones (a repair that forces a full re-train), and
    # the per-attempt triage judge can answer `abandon` at any point. A coarse filter on top of three
    # calibrated ones was not protection — it was a way for a whole class of failure to be dropped
    # without anyone deciding to drop it. Env override expects a JSON array.
    inline_repair_reasons: tuple[str, ...] = FAILURE_REASONS
    # METRIC SALVAGE (`engine/metric_salvage.py`): what happens when a node fails for something other
    # than "the metric is absent" and the operator's OWN declared reader can still find the metric
    # that eval already produced. The case it exists for: v5 node 0 trained 76 minutes, printed
    # `RECALL@100: 0.743250`, and was discarded because its manifest named the checkpoint one
    # directory over. Only DETERMINISTIC rungs are used — the operator's declared reader, re-asked;
    # never a model, because the agent writes the training script and therefore writes the text an
    # extractor would read, which is a route around the protected `score` stage.
    #   "off"    — never (the behaviour before 2026-08-12)
    #   "audit"  — salvage, record the provenance, and attach a `metric_salvaged` VIOLATION: the node
    #              is evaluated and counted (budget, UI, lineage) but excluded from `feasible_nodes`,
    #              so it can never become champion or be bred from on a value nobody measured.
    #              ONE ESCAPE HATCH, and it is not this field's: `metric_salvage_repair` (below,
    #              default True) can buy a Developer fix for the failing declaration and RE-CHECK the
    #              artifact contract against the corrected one. A node that PASSES that re-check is
    #              no longer salvaged — the value was measured after all — so the violation and the
    #              exclusion go with it. That path is restricted to OPERATOR-produced output
    #              (`metric_salvage.recheckable_salvage`), which is what keeps it from being more
    #              permissive than `select`. Set `metric_salvage_repair: false` for the flat rule.
    #   "select" — a salvaged metric competes on equal terms.
    # `audit` is the default deliberately, and the trade-off is real: a salvaged baseline is not bred
    # from until the operator says so.
    metric_salvage: str = "audit"
    # Still fix the DECLARATION that broke, in the same attempt. Never re-evaluates — the whole point
    # is that the experiment already succeeded.
    metric_salvage_repair: bool = True
    # Cost ceiling for in-node repair of a MULTI-STAGE eval: the max number of FULL pipeline re-runs
    # (i.e. re-trains from the first stage) the inline-repair loop may do before abandoning to the
    # inter-node debug operator. A late-stage failure (a broken `score`/eval script that didn't touch
    # `train`) REUSES the completed train checkpoint and re-runs only from the failed stage — that is
    # CHEAP and NOT counted here; only a repair that changes an EARLIER stage's code (train.py/loss.py/…)
    # forces a full re-train, which each costs the whole training time. Without this bound, a repair that
    # keeps changing training code could burn many full trains (`inline_repair_attempts` bounds the
    # COUNT of repairs, not what each one costs). 0 = unlimited (legacy behavior).
    #
    # IT IS SPENT TWO WAYS, and the second exists because the first structurally cannot reach the
    # case that costs the most (2026-08-15, backlog §0.2). As a COUNT it charges only a repair that
    # DISCARDS COMPLETED EARLIER-STAGE WORK (`evaluate.py::_repair_forces_full_retrain`) — a
    # FIRST-stage failure and a single-command eval discard nothing, so they were charged nothing and
    # left to "the attempt budget", which since 2026-08-13 means 50-or-a-judgement. Driven on the
    # no-judge configuration that has neither: a first-stage `RuntimeError` bought 51 full pipeline
    # evaluations, 50 repairs, 0 charges. So the SAME number is also spent in SECONDS
    # (`repair_judgment.repair_redone_work_stop`): a repair chain may spend `(cap + 1)` times what
    # the task DECLARES one full pipeline costs (the sum of the declared stage timeouts, or the
    # single-command timeout), so single-command evals no longer ignore it entirely.
    # NOT "charge the count for a first-stage repair too": measured against
    # `runs/rubertlite-dr-unified-v8` node 3, which failed its first stage three times and PASSED it
    # on the fourth attempt, a per-repair charge at this default abandons that node one repair
    # before the stage it was fixing worked. The count is calibrated for a re-TRAIN (that manifest
    # declares `train` at 22000 s) and a `mine` at 5400 s is not the same unit.
    inline_repair_retrain_cap: int = Field(default=2, ge=0)
    # Environment self-prep: when a solution crashes purely because a KNOWN library isn't installed
    # (ModuleNotFoundError), the engine pip-installs it into the eval interpreter and re-runs — so a
    # torch/XGBoost/CatBoost idea (e.g. a GRU model) actually runs on a fresh box instead of being
    # thrown away as `idea_rejected`. Trusted_local tier only (the untrusted/hostile Docker tiers run
    # --network none). A typo'd or local helper module is NOT installed (that's a code bug to repair).
    auto_install_deps: bool = True
    # Per-package wall-clock budget for an auto-install (seconds). Generous: large wheels like torch
    # can take minutes to download/build.
    dep_install_timeout: float = Field(default=900.0, gt=0)
    # --- Agent governance: who may change a setting at runtime (per-setting allow-list of roles) ---
    # Roles: "strategist" (A7 meta-controller; run-wide), "boss" (run-chat operator-proxy; run-wide),
    # "researcher" (per-experiment proposer; PER-NODE — e.g. set a longer eval timeout for a neural-net
    # idea). A setting ABSENT from this map is LOCKED — no agent may change it, only a human via the
    # snapshot/UI. The sole conditional exception is Strategy-only `card_scoring`: Card mode grants
    # it implicitly without changing the default flag-off snapshot, while an explicit [] revokes it.
    # The gate is ENFORCED at every AGENT seam via `_agent_may` — the strategist's whole
    # control surface (policy/operators/novelty_stance/prefer_sweep/developer/fidelity/timeout/
    # eval_parallel/llm_parallel) is checked in `_apply_strategy`, so removing a role from a knob locks it,
    # not just greys out the UI pill. The boss/max_* grants document boss-relevant knobs, but a
    # `budget_extend` is a HUMAN control intent (the boss action-builder can only emit `add_nodes`), so
    # it is applied as-is — the human always wins via the UI/snapshot. Defaults are conservative: resource/
    # search-shape knobs the agents already reason about; provider infra (llm_model/base_url/api_key,
    # docker_image) stays locked. The UI shows S/B/R pills per setting. `timeout` granted to the
    # researcher IS the "auto" per-node mode — the researcher sizes each experiment's wall-clock
    # (Idea.eval_timeout); the config `timeout` is the fallback for nodes it doesn't size. Env override
    # expects a JSON object. (`fidelity`/`novelty_stance`/`prefer_sweep`/`developer` are governance keys
    # for the strategist's per-node dials — not 1:1 Settings fields, but gated the same way.) The default
    # lives in the module-level DEFAULT_AGENT_CONTROL so the Engine's direct-construction default matches.
    agent_control: dict[str, list[str]] = Field(default_factory=default_agent_control)
    # C1 (Agentless localization): for repo tasks, rank the source files most relevant to the most
    # recent failure (traceback + identifiers) and surface them in the proposal/repair prompt so the
    # Developer edits the right place. Off by default; repo tasks only.
    localize_faults: bool = False
    # A2 surrogate-guided proposal (BO-lite): fit a k-NN surrogate over (params->metric) and propose
    # by acquisition instead of random/hill-climb. Numeric-bounds tasks only; bootstraps via the
    # base Researcher. Off by default. `surrogate_explore` = UCB-style exploration weight.
    surrogate_proposer: bool = False
    surrogate_explore: float = 0.1
    # E2 researcher panel: generate K candidate ideas per proposal and keep the one ranked best by a
    # cheap empirical surrogate over the (params->metric) history (NOT an LLM-judge). 1 = off.
    researcher_panel: int = 1
    # A1 ASHA / successive-halving: reduction factor (keep top 1/eta per rung) and rung budget.
    asha_eta: int = Field(default=3, ge=2)
    # Rung-0 width for ASHA/BOHB (the wide base of cheap drafts). 0 = use n_seeds (default,
    # preserving prior behavior); set >0 to seed a wider/narrower base independent of n_seeds.
    asha_rung_nodes: int = Field(default=0, ge=0)
    # A6 proxy/predictive scoring: cheaply rank a candidate's potential from early-stage signals
    # and skip a full eval for the doomed bottom fraction. 0.0 = off (no candidate skipped).
    proxy_scoring: bool = False
    proxy_kill_fraction: float = Field(default=0.0, ge=0.0, le=0.9)
    # === Novelty / dedup ==================================================================
    # E1 novelty/dedup gate: reject near-duplicate proposals (normalized param-space distance below
    # `novelty_epsilon`) by deterministically nudging them off the duplicate — stops the search
    # wasting evals re-trying the same idea. Audit event `novelty_rejected`. Off by default.
    # Novelty/dedup MODE selector: how a fresh proposal is checked against already-tried experiments.
    #   "off"  — no explicit gate; the agentic Researcher's own read-the-history judgment is trusted.
    #   "algo" — the deterministic gate: numeric param-distance (`novelty_epsilon`) + optional embedding
    #            similarity (`novelty_semantic`). Cheap, no LLM call, but can't explain itself.
    #   "llm"  — an LLM adjudicates (reads the real experiments via tools) whether the idea is a
    #            near-duplicate and, if so, asks the Researcher once more for something different. One
    #            extra LLM call per proposal — highest quality, follows the "let the LLM decide" line.
    novelty_mode: str = "llm"
    # Legacy sub-toggles, honored by the "algo" mode (and back-compat: novelty_gate=True forces "algo"):
    novelty_gate: bool = False
    novelty_epsilon: float = 0.05
    # T5 semantic novelty (active whenever the deterministic gate runs — novelty_mode=algo,
    # novelty_gate=true which is the legacy alias for algo, OR the Strategist novelty stance is
    # "explore"; a no-op only under novelty_mode=llm/off with a non-explore stance): reject a proposal
    # whose idea TEXT embeds within
    # `novelty_semantic_threshold` cosine of an existing node's. OFF BY DEFAULT: novelty is the agentic
    # Researcher's call, made by READING the actual prior experiments (read_experiment /
    # find_analogous_across_runs / list_experiments), NOT an embedding-similarity auto-reject that can't
    # explain itself and mis-fires on paraphrases. Semantic/param search stays available as TOOLS in the
    # Researcher's context; the LLM decides whether an idea is a true duplicate.
    novelty_semantic: bool = False
    novelty_semantic_threshold: float = Field(default=0.92, ge=0.5, le=1.0)
    # INERT SINCE 2026-08-13 (F5) — kept as a field, and deliberately not removed. It bounded the
    # DEBUG-LINEAGE depth: how many times a failing lineage could spawn a fresh node to have another
    # go at the experiment that failed. That node is gone; a failure is repaired inside the one node
    # now, and what bounds THAT is `inline_repair_attempts` plus the judgment in
    # `engine/repair_judgment.py`.
    #
    # The field stays because Settings names are a compatibility surface twice over: `LOOPLAB_<FIELD>`
    # env vars and every `config.snapshot.json` under `runs/` map 1:1 onto them (CLAUDE.md — never
    # nest or rename a field), and `search/speculation_calibration.py` pins `debug_depth` inside the
    # calibrated runtime envelope, so removing it would revoke receipts for a knob that no longer
    # does anything. It is read only by the policy constructors, which now do nothing with it.
    debug_depth: int = Field(default=2, ge=1)
    # P4 operator bandit (GreedyTree): replace the fixed merge/ablate cadences with a
    # deterministic UCB over per-operator yield (Δmetric per eval-second, folded from the run).
    # Off by default (the cadences are well-tested and the bandit has no direct published
    # ablation); `thorough` turns it on.
    operator_bandit: bool = False
    # P1 hypothesis ledger: ask the Researcher to state the one-line hypothesis each experiment tests,
    # register deep-research directions as hypotheses, and track them to a verdict on the board. ON by
    # default. The board is shown to the Researcher and can be foresight-ordered, so it can
    # steer later proposals even though it never directly re-ranks already-evaluated nodes. Set False
    # to drop the prompt nudge + registration.
    track_hypotheses: bool = True
    # E4/M2/M3 reflection-memory -> priors (gradient-free cross-run meta-learning): at run end distill
    # the winner into `<memory_dir>/meta_notes.jsonl` AND structured lessons (incl. NEGATIVE results:
    # tested/abandoned hypotheses + failure themes) into `lessons.jsonl` with a task fingerprint; at run
    # start, inject exact-task notes + fingerprint-matched lessons from SIMILAR tasks into the proposal
    # prompt. ON by default — but a NO-OP until `memory_dir` is set (that's where cross-run memory lives).
    reflection_priors: bool = True
    # M6 comparative lessons (MARS "comparative reflective memory", doc 13 §7 item 2): distill
    # credit-assigned lessons from PAIRS of solutions — which SPECIFIC change made a child beat
    # (or regress from) its parent, and what fixed a failure — on top of the one-shot run-end
    # reflection. At most one extra LLM call per distillation (all pairs batched); offline a
    # deterministic param-diff credit stands in. Gated on reflection_priors + memory_dir like the
    # rest of cross-run memory — and since memory_dir has a default (~/.looplab/memory), this is
    # ACTIVE out of the box; clear memory_dir to disable cross-run memory wholesale.
    comparative_lessons: bool = True
    # M6 mid-run cadences (doc 13 §7 item 5 — the AgentRxiv live-share pattern): every N created
    # nodes, WRITE comparative lessons to the shared cross-run store during the run (lessons_every
    # — one batched LLM call per firing) and RE-READ the store so lessons distilled by CONCURRENT
    # runs reach this run's proposals (lessons_refresh_every; file re-read, no LLM call, skipped
    # when the store is unchanged). ON by default (every 4 nodes) and, like comparative_lessons
    # above, live out of the box because memory_dir defaults on; 0 = off — run-end write /
    # run-start read only, the pre-M6 behavior.
    lessons_every: int = Field(default=4, ge=0)
    lessons_refresh_every: int = Field(default=4, ge=0)
    # B3 output redaction: the HIGH-ENTROPY half of the persisted-tail redactor.
    # **This flag no longer decides whether tails are redacted at all** (backlog C2, 2026-08-14).
    # Known credential SHAPES and the operator's own secret env VALUES are masked on every persisted
    # stdout/stderr tail unconditionally — a `print(api_key)` or a traceback must not land in the
    # durable log on the DEFAULT path, which is what gating the whole pass on an off-by-default flag
    # meant for six weeks. What this flag gates is the ENTROPY pass, which masks a 24+ character
    # base64-ish run above the entropy cutoff: on ML output that also hit legitimate data hashes,
    # model checksums and embedding dumps, and THAT false-positive cost is what kept it opt-in.
    #
    # **ON BY DEFAULT SINCE 2026-08-15 — the operator's call, on the measurement below.** The
    # false-positive cost that justified the OFF default was measured on 2026-08-14 and then
    # REMOVED: over every persisted tail in `runs/` the entropy pass used to change 13 of 744, and
    # all 13 were FALSE — every one a filesystem path inside a traceback collapsing to
    # `File "***REDACTED***.py"`. Zero were credentials.  `core/redact.py::_entropy_candidate`
    # (mixed case AND digits, the composition of a base64 credential and of nothing else in an ML
    # log) plus `_ENTROPY_TOKEN_CHARS` (`/` is a SEPARATOR, not a token character) screen those out.
    # RE-MEASURED 2026-08-15 over the whole preserved corpus at its current size — 1,652 non-blank
    # `stdout_tail`/`stderr_tail`/`error`/`reason` values across 82 event logs, i.e. more than twice
    # the corpus the flip was argued on — the entropy pass changes **0 of 1,652** tails, while the
    # pre-fix rule still changes 18 of the same 1,652 and all 18 are filesystem paths. Masked tokens
    # across ALL durable string values are 5 distinct / 2,347 occurrences (was 138 / 5,289), of
    # which 2,343 are one opaque account identifier.
    #
    # So the default now matches the two things that were already true and out of step with it: the
    # ~30 ALWAYS-ON `redact_persisted_text` boundaries have run this same pass unconditionally since
    # B3 (162,472 `***REDACTED***` markers are already durable in the corpus's `spans.jsonl`), and
    # this module's own docstring asserted the pass was on for six weeks while the field said
    # otherwise. The tail was the ONE durable diagnostic channel that skipped the screen the rest of
    # the tree applies; it no longer is.
    #
    # WHAT AN OPERATOR GIVES UP BY SETTING IT FALSE, stated so the switch stays honest: a
    # high-entropy base64 credential printed by an eval reaches `node_evaluated.stdout_tail` — and
    # therefore `events.jsonl`, the span trace, the UI node detail, an export and a bug report —
    # unmasked, unless a known shape (`_PATTERNS`) or one of this operator's own env VALUES
    # (`redact_env_values`) catches it. Both of those are masked either way, at every tail, whatever
    # this field says. What is NOT recovered by turning it off is a destroyed traceback path: the
    # composition screen is unconditional, so `False` buys back nothing the fix already gave.
    # Deliberately NOT in `LEGACY_CONFIG_SNAPSHOT_DEFAULTS` — see that map's closing section for the
    # three independent reasons. See `core/redact.py::redact_output_tail`.
    redact_output: bool = True
    # B5 reward-hacking detector: a host-side monitor that flags suspicious wins (grader/answer-key
    # access, runtime writes to frozen files, suspiciously-perfect metrics) as a `reward_hack_suspected`
    # audit event in the Trust panel. Off by default. Whether a flag CHANGES selection is governed by
    # `trust_gate` below (default: audit-only, never changes selection).
    reward_hack_detect: bool = False
    # Trust enforcement (T2): what a reward-hack / data-leakage flag actually DOES to selection.
    #   "audit" (default) = surface it in the Trust panel, never change selection (today's behavior);
    #   "gate"  = a flagged node can no longer be selected as BEST **and isn't bred/confirmed from**
    #             (breed_excluded — see replay._apply_trust_gate / models.breedable_nodes), but stays
    #             FEASIBLE so it still counts for diversity/audit — closes the "a hacked/leaky node can
    #             win OR seed its lineage" hole;
    #   "block" = additionally mark it fully INFEASIBLE (removed from feasible_nodes entirely).
    # Deliberately gates only high-precision CHEATING/LEAKAGE signals. Broad `critic:*` findings stay
    # advisory; `critic:hardcoded_metric` is the one narrow high-precision critic exception
    # and gates because it requires a literal metric with no computed assignment anywhere.
    trust_gate: str = "audit"
    # I3 data-centric: static code-leakage scan of each evaluated solution (fit-before-split,
    # fit-on-test) surfaced into the Trust panel alongside reward-hack flags. Off by default.
    code_leakage_detect: bool = False
    # 4.4 sandbox instrumentation (needs reward_hack_detect): after each eval, flag a RUNTIME write
    # to a protected/frozen file (an answer key or grader tampered mid-run) — behavioral evidence a
    # static code scan misses. ON by default; only fires when the reward-hack detector is on.
    workdir_audit: bool = True
    # D8 decoupled research Verifier: check every Deep-Research memo's CLAIMS against their cited
    # evidence (node ids / urls) before recording — synthesis is the documented weak link (Kosmos
    # 57.9% accurate). Deterministic layer always; an LLM rubric pass when the DeepResearcher has a
    # client. The verdict cannot change this run's node/champion, but finalize uses aligned supported
    # verdicts as the evidence gate for persisted D8 cross-run claims; unavailable/misaligned stays
    # unverified. ON by default (no-op with no memo/claims).
    research_verify: bool = True
    # D8's PUSH half: qualify the memo SUMMARY `agents/roles.py::_state_brief` splices into every
    # Researcher / crash-triage / repair-critic prompt with the verifier result the payload already
    # carries (`core/advisory_payloads.py::memo_verdict_cue`). ON by default since 2026-08-16.
    #
    # The verifier checks `memo["claims"]`; it has NEVER looked at `memo["summary"]`, so the pushed
    # field is the one field of the memo nothing checks — and until the same day, the verdicts could
    # not be PULLED either (`read_research_memo` keyed on a `verification["summary"]` no writer
    # emits). Measured over `runs/rubertlite-dr-unified-v8`'s `spans.jsonl`, that line reached 293
    # real prompts (propose 269, triage 20, repair_critic 4) and not one of the 293 whole prompts
    # contains the word `Verifier` or the word `unsupported`, while the memo behind 52 of them
    # records `total_verdicts: 8, unsupported: 8` and opens "climb from the known ~0.88 plateau" —
    # a rounded `rubert-dr-0807` number on a different `engine/eval_contract.py` contract. Over the
    # whole corpus: 100 of 103 memos are pushable, 81 push a decimal number, 76 push a number from a
    # memo carrying a non-`supported` verdict, and 13 push a >=3dp number that is a metric of a
    # provably different-contract run — five runs, not one, so this is a mechanism and not v8's
    # accident.
    #
    # `false` reproduces the historical line BYTE FOR BYTE: the cue is spliced at one position and
    # nothing else moves (the `developer_probe` / `train_monitor_tools` pattern). It ANNOTATES and
    # withholds nothing — an unsupported memo's summary is still delivered in full, because 26 of the
    # 100 pushable memos have no supported verdict at all and 45 of the 45 deterministic verdicts are
    # unsupported about the CITATION, so suppression would drop real findings over bad footnotes.
    # It reaches no metric, champion, selectability decision or violation (docs/36).
    memo_verdict_cue: bool = True
    # C4 independent critic: an execution-free critic of each solution (stub / hardcoded-metric /
    # params-ignored; on host-graded tasks the metric checks become a submission-output check)
    # surfaced in the Trust panel. Broad findings are advisory; `critic:hardcoded_metric` can gate under
    # trust_gate=gate/block. Off by default.
    critic_check: bool = False
    # A7 Strategist (NEW, user-requested): optional LLM/rule meta-controller that picks the search
    # policy/allocator + operator mix + fidelity (+ Developer backend) per situation. Config-first:
    # every choice the Strategist makes is also a direct knob. Defaults to "agent" so the agent adapts
    # the search policy/operators/fidelity per situation; set "off" for static config-driven search.
    # "agent" = a tool-using Strategist that READS the run/data/sibling-runs/KB/memory (B1-guarded)
    # before deciding, instead of the single-shot "llm" call over aggregate stats.
    strategist_backend: str = "agent"          # "off" | "rule" | "llm" | "agent" — default AGENTIC: the
    #   Strategist READS the run/data/sibling-runs/KB/memory with tools before deciding, not a single-shot
    #   call over aggregate stats. "llm" = the old non-agentic single-shot; "rule"/"off" = no LLM.
    strategist_every: int = Field(default=3, ge=1)   # consult cadence (created nodes)
    # Stop the whole RUN when NOTHING has ever worked: this many DISTINCT nodes ended failed and not
    # one has ever produced a metric. It is the run-level companion to `inline_repair_attempts`,
    # which bounds one node's repairs but says nothing about a run whose every node fails for the
    # same reason. The engine's other no-progress guard cannot cover it: `node_failed` is a TERMINAL,
    # so a run that only fails resets that guard every time. Measured on `rubertlite-dr-unified-v2`
    # (2026-08-11): 26 hours and 1,705 provider calls over 6 failed / 0 evaluated nodes, every
    # failure the same environment defect, re-diagnosed from scratch each time.
    #
    # Once ANY node is evaluated the environment/libraries/data are proven and this is off for the
    # rest of the run — a later failure is about that idea, so only that node and its direction stop.
    # 0 disables it entirely. 3 rather than 1: a first node can fail on something a Developer really
    # can repair, and stopping a whole run on one crash would be worse than the grind it prevents.
    systemic_failure_stop: int = Field(default=3, ge=0)
    # PART V (F1): concept CLASSIFIER re-tag + consolidation cadence, DECOUPLED from strategist_every. The
    # LLM concept map is heavier and slower-moving than a strategy consult, so it refreshes on its own
    # (sparser) interval. Researcher-authored idea.concepts still fold immediately at node_created — this
    # only paces the classifier-evidence + consolidation refresh (and the concept-coverage pivot snapshot).
    #
    # 30 -> 5 (2026-08-11). The old default was LONGER THAN EVERY REAL RUN ON THIS CORPUS, so after the
    # seed-boundary firing there was never a second one: `rubert-dr-0807` finished 14 nodes with tags on
    # exactly ONE of them, which is why every concept surface reported `count: 1, best_metric: null`.
    # A re-tag pass is also much cheaper than "heavier and slower-moving" suggests — it skips nodes it
    # has already tagged and is bounded by `_RETAG_CAP`/`_HYP_TAG_CAP` per pass — so the cost scales with
    # NEW nodes, not with run length. 5 gives a 14-node run three passes; raise it if the enrichment lane
    # is the bottleneck, and note that the SEED boundary fires regardless of this value.
    concept_retag_every: int = Field(default=5, ge=1)
    # === Confirmation & holdout ===========================================================
    # Multi-seed confirmation (I12, ADR-15): confirm the top-k under N seeds before
    # finishing. 0 disables (default). Only meaningful when eval has variance.
    confirm_top_k: int = 0
    confirm_seeds: int = 0
    # D1 seed-holdout discipline: the FIRST seed the confirm phase uses. Search evals run with the
    # implicit seed 0 (LOOPLAB_EVAL_SEED unset -> "0"), so a base of 1 keeps every confirm split
    # DISJOINT from anything the search optimized against — the confirm metric is then a
    # generalization signal, not a re-measurement (AIRA: selecting on a signal the search saw
    # overfits by 9-13 pp). Set 0 to restore the legacy overlapping seeds 0..N-1.
    confirm_seed_base: int = Field(default=1, ge=0)
    # D1 holdout-gated promotion (B6, Arbor-style): for host-graded tasks, reserve this fraction of
    # the held-out labels as a FINAL holdout partition the search never sees — every search/confirm
    # eval is scored on the remaining rows only, and at finish the val-top-k are re-scored on the
    # holdout partition (free: the predictions already exist). 0.0 = off (legacy full-label scoring).
    holdout_fraction: float = Field(default=0.25, ge=0.0, le=0.9)
    # Champion selection prefers the HOLDOUT metric among the nodes that have one (the val-top-k).
    # Recorded in run_started so replay applies the same rule; old logs fold to False (legacy pick).
    # The per-node val-holdout `generalization_gap` folds into the Trust panel either way.
    holdout_select: bool = True
    # How many val-leaders get a holdout evaluation at finish (host-graded tasks).
    holdout_top_k: int = Field(default=3, ge=1)
    # R1-c / Part IV — calibrated §12-verifier tie-break in best-selection. When a fresh node's robust
    # metric EXACTLY TIES an already-eligible node, the engine runs the §12 verifier (grounded on each
    # node's realized result, `selection_criteria`) and records a soundness score per node. The tie-break
    # then applies across the WHOLE selection path: the mean pick breaks a robust_metric tie by the score,
    # and — when `holdout_select` is on — the holdout-slot ranking prefers the sounder node so it isn't
    # denied a holdout eval, and the final holdout override breaks a holdout_metric tie by it too. Strictly
    # a TIE-BREAK — it can NEVER move a node ahead of a strictly-better metric (§21.7 advisory-never-
    # overrides). Best-effort: verification is LAZY (only fires on an actual tie) and bounded per cadence,
    # so a large/late tie stays on deterministic metric+id order unless one atomic treatment scores every
    # member. Recorded in run_started so
    # replay applies the same rule; old logs fold to False (legacy pick). Opt-in (default off); needs a
    # reachable LLM client. Enable only after validating calibration offline with `verifier.calibrate`.
    # See trust/verifier.py + core/fitness.py.
    select_verifier: bool = False
    # R1-d (§21.19): widen the verifier tie-break from EXACT-metric to a conservative STATISTICAL tie.
    # The band is capped by both the metric leader's SE and the pooled SE-of-difference, so challenger
    # variance cannot widen it and a significant difference is never a tie (§21.7). Off -> exact only.
    verifier_ci_tie: bool = False
    # Verifier sample count for `select_verifier` (the §12 repeated-sampling expectation). 3 tames the
    # single-shot variance; 1 = single-shot (cheaper, noisier).
    select_verifier_samples: int = Field(default=3, ge=1, le=32)
    # Budget (I13): hard wall-clock ceiling; the run aborts cleanly when exceeded.
    max_seconds: float | None = None
    # Eval-compute budget (#2): hard ceiling on cumulative time spent INSIDE evals (training
    # runs), separate from wall-clock. Survives resume (summed from the event log). The real
    # guard against a silent multi-hour sweep when an eval is a minutes-hours training run.
    max_eval_seconds: float | None = None
    # Cross-run memory (I19, ADR-10): if set, the best result of each run is stored as
    # a case here, and the cases become retrievable knowledge for future runs.
    memory_dir: str | None = Field(default_factory=lambda: str(_LL_HOME / "memory"))
    # HITL (I21, ADR-11): pause for human approval of the final best before finishing.
    require_approval: bool = False
    # Diversity archive (I22): niche bucket width in parameter space.
    archive_resolution: float = 1.0
    # Coverage read-model (narrowing signal): at the strategist cadence, compute a breadth summary
    # (themes / param-niches / theme entropy / dominant-theme fraction) from the folded run, record
    # it as a `coverage_snapshot` audit event, and surface it into the Strategist's decision context.
    # Deterministic, cheap, and purely additive context — no search-behavior change on its own. On by
    # default (like the rest of the always-on situational context). See search/coverage.py.
    coverage_context: bool = True
    # F1i — whether the node-count cadences may fire at a creation decision point that still has
    # evaluations IN FLIGHT. ON, and the default is the fix rather than the historical behaviour:
    # `false` restores a predicate that measurably never became true again. `_should_consult` (the
    # Strategist consult + the coverage snapshot) and `_should_consult_concepts` (the classifier
    # re-tag / consolidation / edge / hypothesis-tag / concept-coverage pass) both opened with
    # `if state.pending_nodes(): return False`, which since backlog F1f made evaluation children
    # outlive the turn that admitted them is a state a GPU run reaches only after its LAST evaluation
    # terminates. Measured over `runs/` 2026-08-18: `rubertlite-dr-unified-v7`, `-v9` and the live
    # `e5small-dr-unified-v2` have ZERO prefixes with nodes and no pending node, and between them
    # recorded zero `strategy_decision`, zero `coverage_snapshot`, zero `concept_coverage_snapshot`
    # and zero classifier `node_concepts`; `-v8`'s single 148-prefix window is the last 8.1 minutes of
    # a 47.6-hour run and holds every cadence firing that run ever made.
    # It costs no extra paid passes: the PACE is unchanged (`cadence_due` + each consumer's `at_node`
    # idempotence still admit one firing per node count) and the two consumers that record no mark on
    # their "nothing changed" path carry an in-process attempted-at-n memo. It reaches no record: the
    # in-flight classifier rows are stamped `at_pending` and `core/models.py::
    # classifier_verified_node_concepts` refuses them, so no mid-eval tag enters graded-novelty
    # admission or any other evidence channel (docs/36). Deliberately NOT in
    # `LEGACY_CONFIG_SNAPSHOT_DEFAULTS`: the fold never reads it, so an already-recorded run replays
    # identically under either value, and pinning a resumed run to `false` would preserve the defect
    # for exactly the multi-hour runs it costs the most. `EngineOptions` keeps it OFF, like every
    # other Part IV/V knob, so a bare `Engine(...)` gains no unasked work.
    cadence_while_evaluating: bool = True
    # PART IV Phase 2a live steering (§21.11/§21.13). When on, the `concept_retag_every` cadence (NOT
    # `strategist_every` — the producer gates on `_should_consult_concepts`, which uses the seed boundary
    # + `concept_retag_every`, default 5) records a
    # concept-graph coverage + uncovered-region snapshot (deterministic heuristic tagger over the
    # task-type skeleton) and, on an `explore` stance, the Researcher's novelty hint names the exact
    # uncovered regions ("0 coverage in {negatives/external-mining, distillation} — go there") instead
    # of the vague "broaden". ON by default in the product Settings (ce4a379), EngineOptions off: it
    # only enriches an already-mode-gated explore hint, never forces exploration, and no-ops for a task
    # with no curated concept skeleton. The snapshot is auditable, but its prompt cue can change future
    # candidate generation; it does not directly rewrite best-metric selection. See search/concept_graph.py.
    # DESIGN NOTE (2026-07-17 critique): now ON unconditionally, but a toy / non-ML task yields EMPTY concept
    # tags while still paying the per-node LLM concept cost (~10 calls/node with the full Part IV/V bundle).
    # Consider AUTO-QUIESCING: if the heuristic tagger returns empty for the first N nodes (no concept signal),
    # skip the LLM concept work for the run — keeps it on for ML tasks, free on toy/degenerate ones.
    concept_pivot: bool = True
    # PART IV Phase 2b — D3 graded novelty into the LIVE novelty gate (§21.4/§21.13). When on and the
    # task has a curated concept skeleton, the novelty gate first grades a proposal over the concept
    # graph: a level-4 "same direction, DIFFERENT implementation" or a level-5 "re-opens a
    # wrongly-abandoned FAILED direction" proposal is ALLOWED through unchanged — the flat LLM/semantic
    # dedup gate can't tell "this DCL tweak" from "the whole DCL branch" and would wrongly reject a
    # legitimate variant or never re-open a sound-but-killed direction (the node_63 archetype). Levels
    # 1/2/3 (identical / near-dup / prior-run) still fall through to the existing gate. Deterministic
    # (heuristic tagger, no LLM), audit event `novelty_graded`, replay-safe. ON by default in the product
    # Settings (ce4a379), EngineOptions off; no-ops for a task with no skeleton. See search/graded_novelty.py.
    graded_novelty: bool = True
    # PART IV Phase 2b — D7 capability-expansion forced-jump DIRECTIVE (§21.8/§21.13, issue #7). When on
    # and the concept-graph cadence detects action-space LOCK-IN (the search has stayed inside one D5
    # branch for a long consecutive streak) on an `explore` stance, the Researcher's novelty hint
    # ESCALATES from "broaden" to "expand the ACTION SPACE — build the missing infra (external mining,
    # a new data pipeline, a different eval), do NOT swap another {locked} lever." The same
    # gate also stamps the proposal's authoritative operator as `expand`; that node competes normally
    # under SearchFitness and its separate operator yield is measured. It does not force a winner.
    # Reads the 2a concept-coverage snapshot, so it needs `concept_pivot` to record one. OPT-IN
    # (default off). See engine/proposal_cues.py + search/lock_in.py.
    capability_expansion: bool = False
    # PART IV cross-run Step 0 (§21.20.12/§21.20.13). Universal task-fingerprint tokenization: drop the
    # ASCII-only `[a-z0-9]` allowlist on goal keywords so a non-Latin goal (Russian, CJK, …) is NOT
    # silently dropped from its cross-run fingerprint (today such a goal keys only on kind/dir/metric and
    # never reaches SIMILAR-task priors/lessons/cases). Uses `[^\W_]+`/`.casefold()` — same splitting as
    # before, any script. ON by default in the product Settings (ce4a379); the bare-library EngineOptions
    # default stays off, so a run pinned to it won't silently re-key a portfolio mid-flight. See engine/memory.py.
    fingerprint_universal: bool = True
    # PART IV cross-run Step 2 (§21.20). Fill graded-novelty's `prior_concepts` from the cross-run
    # ConceptCapsuleStore: at run end write a per-run concept capsule (the shipped `node_concepts` tags +
    # best outcome, keyed by task_fingerprint) to `memory_dir`; when a later SIMILAR run proposes an idea
    # whose concept was tried before, SURFACE the prior outcome as a `cross_run_prior` audit event (never
    # reject — D3 level 3). Audit-only, off the selection path; ON by default in the product Settings
    # (ce4a379), EngineOptions off. Needs a `memory_dir` to share capsules.
    # See engine/memory.py (ConceptCapsuleStore) + engine/novelty.py.
    # PREREQUISITES (this flag is NOT standalone): the WRITE needs per-run concept tags
    # (`node_concepts`, produced by `concept_pivot`/F1 tagging) — no tags => no capsule; the SURFACE path
    # runs inside the graded-novelty precheck, so it also needs `graded_novelty` on. With only this flag +
    # `memory_dir` it persists/surfaces nothing. Enable `concept_pivot` + `graded_novelty` alongside it.
    # A non-Latin (Russian/CJK) portfolio should ALSO set `fingerprint_universal`, else the capsule's goal
    # tokens are dropped and its fingerprint over-matches on kind/dir/metric alone (the novelty direction
    # gate narrows this, but goal-word similarity is still lost).
    cross_run_concepts: bool = True
    # PART V (B): run-base + node-DELTA concept authoring. When on, once the first evaluated node has
    # authored concepts the engine seeds RunState.run_base_concepts from them (a one-shot EV_RUN_CONCEPTS,
    # idempotent because it only fires while the base is empty), and the Researcher's proposal hint tells it
    # to author only `concepts_added`/`concepts_removed` vs the run base + its parent — minimal per-node
    # annotations that inherit down the DAG (a swap propagates). Off -> every node authors its full concept
    # set as before. Product-on; the bare EngineOptions mirror defaults off (test_options_divergence).
    concept_run_base: bool = True
    # PART IV cross-run Step 5 advisory (§21.20.5). Fold the bounded cross-run CONTEXT PACK — evidence-
    # grounded claims with BOTH support and counter-evidence (Step 4) + a portfolio-coverage line (Step 3) —
    # into the Researcher's proposal prompt, exactly like the E4 cross-run prior note. Advisory ONLY: it is
    # prompt-grounding: it can change the Researcher's later candidates, but does not directly rewrite
    # best-metric selection (§21.7). Reads `memory_dir` (lessons.jsonl + concept_capsules.jsonl); "" when
    # empty. ON by explicit experimental product choice; no frozen A/B validation is claimed. The
    # bare-library EngineOptions default stays off (engine/options.py, frozen in
    # test_options_divergence). See engine/proposal_cues.py.
    cross_run_advisory: bool = True
    # PART IV cross-run §21.20.13 (full CR of the lean fuzzy claim merge). Switch the claim read-model to the
    # SCOPE+POLARITY-safe STRUCTURED claim key (engine/claim_key.py): claims from different tasks never
    # merge, opposite polarity ("X helps" vs "X never helps") is surfaced as a CONTRADICTION instead of being
    # collapsed, paraphrase/inflection variants group by exact structured key (no transitive over-merge), and
    # operator governance is scope-precise (a decision in task A cannot reach a same-worded claim in task B).
    # Affects the `cross_run_advisory` context pack only; ON by default in the product Settings (ce4a379);
    # the bare-library EngineOptions default stays off (engine/options.py). See engine/claims.py.
    cross_run_structured_claims: bool = True
    # PART IV cross-run §22.4 (AGENTIC portfolio stewards). At finalize, when an LLM client is available,
    # let the concept and claim stewards review the freshly-updated portfolio and PROPOSE curation. Proposals
    # are LOGGED for operator ratification (via the serve/CLI governance surface); the LLM never mutates fold
    # state. The calls run synchronously during finalize, so they add latency and may add paid inference; a
    # caught steward failure does not prevent terminal completion or trigger a retry.
    # ON by explicit experimental product choice when `memory_dir` + an LLM backend are present; the
    # bare-library EngineOptions default stays off (engine/options.py). See engine/lessons.py.
    cross_run_curation: bool = True
    # Task faceting is a third, independent proposal-only steward. Its ledger and manual CLI/API
    # surfaces remain useful, but no live retrieval, ranking, authorization or UI consumer reads the
    # generated facets today. Do not buy that synchronous finalize-time model call by default. An
    # explicit opt-in preserves the old automatic schedule, and snapshot migration below maps a
    # missing field to True so an already-running treatment does not silently lose work on resume.
    # This flag is latent unless `cross_run_curation` is also enabled. The bare-library
    # EngineOptions default is likewise off.
    task_facets_finalize: bool = False
    # Deprecated compatibility flag. Steward output is untrusted and finalize is proposal-only regardless
    # of this value; retained so old snapshots still validate and logs can disclose that auto was requested.
    cross_run_curation_auto: bool = False
    # PART IV cross-run §22.4 (RATIFICATION). The steward above only PROPOSES, and until this flag nothing
    # applied its proposals: `apply_concept_curation` was deleted on 2026-08-03 (72ae8487/EM-14) for
    # bypassing the CAS discipline, leaving a paid proposer with no consumer. When ON, finalize runs
    # `engine/concept_tidy.py::ratify_concept_merges` right after the steward and applies every still-valid
    # agent-proposed MERGE through the same `record_concept_alias` the operator CLI/HTTP surfaces use —
    # append-only, read-time, reversible by one `concept-alias-clear`, and never splits or purges. Costs no
    # inference (the judgement is already bought and logged) and appends no run events. OFF by default: it
    # is the only path that lets an agent decision change the cross-run taxonomy without a human in the
    # moment, so it is opt-in even though every decision it makes is individually reversible.
    concept_tidy: bool = False
    # Role backend (ADR-7/14): "toy" (offline optimizer) | "llm" (live model). Validated in
    # `_check_enum_fields` alongside the other enum-ish strings — every consumer tests `== "llm"`
    # exactly (cli/__init__.py, adapters/tasks.py), so an untyped `--set`/file/env typo (or a mis-cased
    # "LLM") used to fall through to the OFFLINE toy roles and quietly produce a run that never called
    # the model. Left as `str`, not `Literal`, so old snapshots still validate through the same path.
    # Default flipped 2026-08-04 (operator decision): "llm" is what a real run wants, and the old
    # "toy" default is exactly how a run could silently never call the model (see the live-scenario
    # harness, which forgot backend="llm" and scored a row-count placeholder for three nodes).
    backend: str = "llm"
    # Developer backend (ADR-7): "default" (templated/LLM from the task) or an external
    # CLI coding agent: "opencode" | "aider" | "goose" | "continue". A CLOSED enum ("default" +
    # cli_agent.PRESETS keys), validated loudly in `_check_enum_fields` — an unknown value used to reach
    # adapters/tasks.py (`developer_backend not in PRESETS`) and silently wire the DEFAULT in-house
    # developer, the same silent downgrade the `backend` guard closes.
    developer_backend: str = "default"
    # C2 best-of-N: generate N candidate implementations per node and keep the best by an
    # execution-free reward (static validity + metric-print). 1 = off. In-house LLM developer only
    # (not external agents — ADR-7 cost rule). The top SWE-bench reliability lever for weak models.
    best_of_n: int = 1
    # D10 list-wise best-of-N selection: when the top static-scorers TIE, break the tie with a
    # comparative LLM selection over the candidates presented together (+~3 pts vs pointwise on
    # GAIA, arXiv:2506.12928). The static score stays the primary filter — the LLM is a weak
    # comparative prior, never the sole oracle. ON by default (no-op when best_of_n==1).
    best_of_n_listwise: bool = True
    # === Foresight / predict-before-execute ===============================================
    # FOREAGENT predict-before-execute (arXiv:2601.05930): use the LLM as an IMPLICIT WORLD MODEL to
    # predict — WITHOUT executing — which candidate / hypothesis will score best, primed with a
    # Verified Data Analysis Report (data profile / brief) + the accumulated experiment memory. Master
    # switch, ON by default; a no-op wherever there is nothing to rank (best_of_n==1 and
    # foresight_panel==1) or no LLM client. See `looplab/search/foresight.py`.
    foresight: bool = True
    # Hypothesis foresight breadth: generate K candidate ideas per proposal and keep the one PREDICTED
    # best pre-execution by the world model — it ranks the STRUCTURAL / text ideas the numeric k-NN
    # surrogate (researcher_panel) is blind to, primed with the data profile + memory (the synergy).
    # >1 enables it (LLM backend only; needs `foresight` on); 2 = on by default at modest cost, 1 = off.
    foresight_panel: int = 2
    # AGENTIC foresight: run the ranking (hypothesis-board prioritization + K-idea pick) as a TOOL-USING
    # loop that can pull actual experiment results / data facts before deciding, instead of a one-shot
    # prediction from a pre-baked report. ON by default (needs foresight + a client; falls back to the
    # one-shot predictor on any hiccup). A few extra LLM calls per proposal; set False for the cheaper
    # single-call ranker.
    foresight_agentic: bool = True
    # §1 foresight confidence gate: the minimum predicted confidence at which a predict-before-execute
    # pick is ACTED ON. Below it the ranker abstains — the K-idea panel falls back to the first
    # proposal, best-of-N falls through to the D10 tie-break — instead of committing a low-confidence
    # choice. 0.0 (default) = OFF (act on every pick, the historical behavior); raise toward ~0.5 to
    # make the world model defer when it isn't sure. Pairs with the foresight track record (§1) the
    # predictor is now primed with. Range 0.0-1.0.
    # BOUNDED like its confidence/fraction siblings (train_monitor_kill_confidence,
    # asha_live_quantile). Unbounded, a typo'd 5.0 validated silently into a gate no score can ever
    # clear, so every foresight pick abstained forever with nothing in the snapshot to say why.
    foresight_min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    # PART IV Phase 2c — replace the world model's SELF-REPORTED confidence (measured Pearson≈0 with the
    # realized outcome, §21.12) with a CALIBRATED §12-verifier score. When on and a client is available,
    # after the K-idea ranker picks the predicted-best candidate the §12 verifier scores it (grounded +
    # repeated + criteria-decomposed: `foresight_criteria` — likely to improve the objective, and
    # sound/feasible) and THAT calibrated score becomes the confidence the `foresight_min_confidence` gate
    # and the telemetry use. Degrades to the self-reported confidence without a client or on any verifier
    # error (the telemetry records which source was used). ON by default in the product Settings (ce4a379),
    # EngineOptions off; a few extra LLM calls per acted-on proposal. See looplab/trust/verifier.py + search/foresight.py.
    # REVISIT (cost/quality, not yet resolved): a live classification run measured ~10 LLM calls & ~3.8 min/node
    # on deepseek-v4-flash with the full Part IV/V default bundle; the calibrated verifier is part of that per-node
    # tax. Kept ON deliberately — it raises decision quality (self-reported foresight confidence has ≈0 Pearson
    # with realized outcome), and lowering quality to save calls is the wrong trade. TODO: run a frozen quality/cost
    # A/B (verifier-on vs -off) to quantify the recall@objective gain per extra call before considering a default flip.
    foresight_verify: bool = True
    # Verifier sample count for `foresight_verify` (the §12 repeated-sampling expectation on a no-logprob
    # backend). 3 tames the single-shot variance §21.12 measured; 1 = single-shot (cheaper, noisier).
    # RESUME-critical: this value is persisted in config.snapshot.json, so a hard `le` schema bound would
    # make an old run started with a larger LOOPLAB_FORESIGHT_VERIFY_SAMPLES un-resumable (Settings(**snapshot)
    # would raise on `looplab resume` / finalize-recovery / serve replay). CLAMP into [1, MAX] via a validator
    # instead of rejecting — the runtime cap is preserved without breaking the snapshots/env-compat rule.
    foresight_verify_samples: int = 3

    @field_validator("foresight_verify_samples")
    @classmethod
    def _clamp_foresight_verify_samples(cls, v: int) -> int:
        # Coerce a persisted/env value into [1, MAX] rather than reject (see field note above).
        try:
            v = int(v)
        except (TypeError, ValueError):
            return 3
        return max(1, min(v, MAX_FORESIGHT_VERIFY_SAMPLES))

    # T7 LLM response cache: serve an identical DETERMINISTIC (temperature 0) request from an
    # in-process content-addressed cache instead of re-hitting the model — cuts cost on
    # retry/panel/verify flows. NEVER caches sampling calls (temp>0: best-of-N / panel / novelty
    # re-propose must vary). Off by default (most role calls run at temperature>0 anyway).
    llm_cache: bool = False
    agent_cmd: str | None = None  # override the agent's launcher (path / wrapper)
    # External-agent validation (ADR-7): wrap a CLI-agent Developer with a validator that audits
    # each output (no-op/syntax/crash/timeout), retries with feedback, and falls back to the task's
    # original in-process Developer (LLM writer, deterministic/template, or repo baseline). Off ->
    # use the raw agent output unchecked.
    validate_agent: bool = True
    agent_max_retries: int = 1    # re-prompts of the agent on an invalid result
    # Patch-gated multi-file agent (ADR-7 Rule 3): run the CLI agent in a git worktree and
    # accept only edits whose paths match `agent_surface` (reject-not-strip out-of-surface
    # touches). Lets the agent create helper modules, not just solution.py. Degrades to
    # whole-file readback if git is unavailable.
    agent_patch_gate: bool = True
    agent_surface: list[str] = ["*.py"]   # edit-surface allow-list (globs)
    # Trust policy for an agent-authored eval/metric adapter (RepoTask onboarding, Phase 3):
    # "ratify_freeze" waits for human approval, "autonomous" accepts the proposed spec without that
    # gate, and "ratify_freeze_drift" also enforces the configured independent drift cross-check.
    eval_trust_mode: str = "ratify_freeze"
    # RepoTask node seeding policy (run-wide fallback; a task/editable `seed_mode` overrides):
    # "auto" (default) copies git-tracked files when the editable is a git repo (so a tree bloated
    # with untracked artifacts isn't deep-copied per node), else a full copy; "tracked" forces
    # code-only; "all" forces a full recursive copy (legacy). Genesis authors per-task from the
    # user's words; this is the global default when unspecified.
    seed_mode: str = "auto"
    # Source-tree READ FENCE (`runtime/read_fence.py`): what happens when a node's own process tries
    # to read the operator's editable SOURCE tree. "deny" (default) raises in the child and the
    # refusal — which names the fix — lands in the node's stderr; "warn" logs one line per distinct
    # path to stderr and to `<run>/.looplab-fence/violations.log` and lets the read through; "off"
    # installs nothing. No-op for a non-repo task: with no editable source there is nothing to fence.
    #
    # WHY DENY IS THE DEFAULT, and not warn. The failure this closes does not announce itself.
    # `runs/rubertlite-dr-unified-v6` node 4 trained a genuinely good model (train.log RECALL@100
    # 0.726) and then scored a HUMAN's checkpoint from 2026-07-18 that an absolute path in an
    # editable config pointed at (score.log RECALL@100 0.225) — and that second number is what the
    # run recorded. `expect` passed, because `expect` checks what a stage WRITES and never what it
    # READS. A warn rung would have logged that and the run would have recorded the same corrupt
    # metric, so warn does not close the defect; it only makes it auditable afterwards. It is also
    # exactly the rung already tried and already spent: an advisory note fires on `edit_file` today
    # with this very text, is in node 4's `spans.jsonl` verbatim, and the node committed the path
    # anyway. Warn stays available because it is the honest answer for ONE run while an operator
    # finds out what their pipeline actually reads.
    #
    # The one real false positive is a large untracked in-tree input that `seed_mode: "auto"` does
    # not copy into the workdir: under deny that run fails LOUDLY, in the child, with a message
    # naming all three fixes (`data:` mount, `references:` mount, `seed_mode: "all"`). Loud and
    # wrong is strictly cheaper than silent and wrong — the silent version cost 76 GPU-minutes and a
    # published metric that was off by 3x.
    read_fence: str = "deny"
    # METRIC SUBJECT (`runtime/metric_subject.py`): what a recorded metric must say about WHAT IT IS
    # ABOUT. `eval.metric.subject` names the artifact the number is a claim about; the engine binds
    # it to that artifact's content identity (`core/atomicio.file_identity` + a digest) at the score
    # stage's start and folds the record onto `node_evaluated.metric_provenance`.
    #   "off"     — record nothing (byte-identical to the behaviour before 2026-08-13).
    #   "audit"   — RECORD, always: every evaluated node carries provenance, bound or not. No
    #               violation, no selection effect, and no stage contract.
    #   "require" — the INVERSION, and the only rung that GATES: an UNBOUND metric also gets the
    #               existing `metric_salvaged` violation, so it is counted, visible in the UI and
    #               lineage, and never selectable — and the declared subject additionally becomes a
    #               `needs` entry on the engine-appended `score` stage (`engine/eval_stages.py`), so
    #               a subject the pipeline did not produce refuses before the scorer runs.
    #
    # THE RUNGS ARE ORDERED AND `audit` MUST NOT GATE. The derived `needs` fired for every mode but
    # `off` until 2026-08-14, which made the middle rung HARSHER than the strict one: driven, a
    # declared subject the pipeline did not produce returned `needs_failed` with `metric=None` under
    # `audit` — the node lost its number entirely — while `require` is documented to keep the metric
    # and record a violation against it. It also bought Developer repair attempts on the protected
    # `score` stage (`needs_failed` is in the default `inline_repair_reasons`). Blast radius was nil
    # rather than by luck: the entry is written ONLY for a task that DECLARES `eval.metric.subject`,
    # and 0 of the 113 task snapshots under `runs/` + `examples/` do — including the live
    # `rubertlite-dr-unified-v8`, which runs at this default. A latent contract bug, fixed before it
    # had a corpus.
    #
    # THE NUMBER THAT DECIDES THIS. Across the six repo runs that have an event log, 82 of 83
    # recorded metrics carry NO `metric_provenance` at all (98.8 %) — the one exception is the
    # SALVAGE path, i.e. provenance is written only once something has already failed. On the happy
    # path the engine records which stage ran, how long it took and what number came out, and nothing
    # whatsoever about what the number is about. And 2 of those 83 are provably about bytes the node
    # did not produce, and are the SAME number: v2 node 4 and v6 node 4, three weeks apart, both
    # recorded 0.224975 from the same foreign checkpoint named at the same `config.yaml:215`.
    #
    # WHY `audit` IS THE DEFAULT AND NOT `require`, stated so the next reader does not have to guess.
    # `require` is the rung this design is for, and the record is the half that is free: no shipped
    # task declares a `subject` yet, so flipping the default today would make every node of every
    # existing repo task unbound and therefore unselectable — a run with no champion. That is the
    # exact failure mode doc 35 §4b(2) warns about ("a boundary that makes legitimate work fail is a
    # boundary the Developer spends repair attempts fighting"). `audit` writes the referent from the
    # first node, which is what makes the flip MEASURABLE instead of a guess.
    #
    # THE EVIDENCE THAT WOULD JUSTIFY FLIPPING IT TO `require`: one real repo run, on a task that
    # declares `eval.metric.subject`, in which every evaluated node records `subject_bound: true` —
    # i.e. the audit rung itself reports a zero false-positive rate over a full run. That is directly
    # countable from `node_evaluated.metric_provenance` and needs no new instrumentation, which is
    # the whole reason the record ships first.
    metric_subject: str = "audit"
    # AUTO-CAPTURED EXTRA METRICS: may an undeclared numeric key on the candidate's own stdout JSON
    # line be RECORDED as one of this node's metrics? `runtime/sandbox.py::json_line_extras` takes
    # every such key, so a task printing `{"metric": x, "recall@10": y}` reports both with no config
    # — and a task printing `{"metric": x, "speculation_cuda_probe_v": 1.0}` records a schema
    # VERSION number as a metric. Re-measured 2026-08-14 over the whole corpus (238 logs under
    # `runs/`): 1,642 values, 10 keys, and 1,636 of them are that version number and its three CUDA
    # probe siblings — i.e. the corpus is 99.6 % diagnostics by volume and 6 values of measurement.
    #   true  (default) — capture them, TAGGED in `node_evaluated.extra_metrics_provenance` with the
    #                     channel that produced each: `auto` for the candidate's own stdout,
    #                     `engine` for the keys the engine's OWN spliced probe source declares.
    #   false           — record only the values the candidate did not author: an operator-owned
    #                     `EvalSpec.metrics` reader's, and the engine probe's. The auto ones are
    #                     dropped from the record entirely.
    #
    # WHY ON IS THE DEFAULT, stated so the next reader does not have to guess. The tag, not the
    # gate, is what closes the defect: an operator or a reviewer could not previously tell an
    # agent-authored number from a protected measurement, and now every consumer can. Flipping this
    # to `false` by default would instead DELETE information — every legitimate
    # `train_auc`/`cv_mean_auc` an operator reads today. A run that turns it off records strictly
    # less and CANNOT be un-turned-off after the fact.
    #
    # WHAT `false` NO LONGER DELETES (2026-08-14). It used to take the CUDA-probe proof with it:
    # `search/speculation_quality.py::_validate_cuda_probe_artifact` admits a calibration node by
    # the EXACT key schema on `extra_metrics`, and the gate — which could then only keep `declared`
    # — dropped all four keys, so the flag and the calibration receipt could not both be had. The
    # probe's keys now arrive tagged `engine` and the gate keeps them, because they were never the
    # candidate's numbers. WHAT MAKES THAT TAG TRUE is `engine/eval_dispatch.py`'s grant, applied
    # only when `engine/speculation_gate.py::engine_authored_artifacts` says the engine's own probe
    # splicer authored this run's artifacts — NOT, as it was for a few hours on 2026-08-14, a
    # byte-exact prefix match against the code about to run, which is code the candidate writes and
    # which therefore let a forged pair through this very switch. It is still not a knob to reach
    # for casually — it deletes the auto channel, and the tag is what closes the original defect.
    #
    # IT IS A WRITE-SIDE POLICY AND NOTHING ELSE. The fold never reads it, so it can never change
    # how an already-recorded run replays — which is why it is deliberately NOT pinned in
    # `run_started` (a new unconditional key there would revoke every issued calibration receipt,
    # whose check compares that payload's key SET for equality). Same shape as `metric_subject`: a
    # per-eval recording rung, resolved from live config, affecting only what the NEXT eval writes.
    # Deliberately NOT in `LEGACY_CONFIG_SNAPSHOT_DEFAULTS`: that map exists to stop re-entry
    # CHANGING an old run's treatment, and this field's shipped default is exactly the pre-field
    # behaviour, so a pre-versioning snapshot resumes byte-identically without a row.
    auto_extra_metrics: bool = True
    # LANDLOCK (`runtime/landlock.py`): apply a KERNEL read allow-list to the eval process and
    # everything it spawns. "off" (default) installs nothing; "enforce" derives the allow-list from
    # the operator's declared mounts (`runtime/read_allowlist.py`) and applies it in the child
    # between fork and exec, so a native reader is covered too.
    #
    # WHY IT EXISTS BESIDE `read_fence`, which is not superseded. The shipped fence is an interpreter
    # audit hook and cannot see a read that never reaches Python: measured on the incident's own
    # 92 MB checkpoint, `safetensors.torch.load_file` loads 55 tensors and raises ZERO `open` audit
    # events. The fence's coverage of this incident is a property of `transformers` reading
    # `config.json` first — a third-party library's file ordering — not a property of the fence. The
    # fence STAYS as the message rung: its refusal is a plain non-`OSError` carrying an actionable
    # fix, and Landlock's is an `EACCES` -> `PermissionError` -> `OSError`, which is precisely the
    # shape `except OSError: <fall back>` turns into a silent skip.
    #
    # WHY OFF IS THE DEFAULT. Enforcement and cost are verified on this box (ABI 2; Python `open`, a
    # child `cat` and raw `libc.fopen` all refused; +2.1 %/open, 0.15 ms setup) — but those are
    # `open`/`read` microbenchmarks. NOBODY HAS RUN A LANDLOCK ALLOW-LIST THROUGH A REAL GPU EVAL:
    # torch, CUDA, NCCL, `/dev/nvidia*`, `/dev/shm`, `/sys/class` and the geesefs read surfaces were
    # never exercised, and doc 35 §7.2's author records that their own candidate allow-list omitted
    # `/sys` and `/dev` entirely and "would have failed such a run". A ruleset missing one surface
    # does not degrade, it refuses mid-training.
    #
    # THE EVIDENCE THAT WOULD JUSTIFY FLIPPING IT TO `enforce`: one real GPU eval — a full
    # train+score on the box the runs happen on — completed under `landlock="enforce"` with the
    # derived allow-list, with `looplab landlock-check <run_dir>` reporting zero skipped rules. Until
    # that exists this rung is opt-in and the audit hook is the shipped boundary.
    landlock: str = "off"
    llm_model: str = "qwen3:8b"
    # === LLM / transport ==================================================================
    llm_base_url: str = "http://localhost:11434/v1"  # Ollama OpenAI-compatible endpoint
    llm_temperature: float = 0.6
    # H3 per-role model presets (5090 recipe): optionally run the Researcher and Developer on
    # DIFFERENT models/endpoints (e.g. Developer=Qwen3-Coder-30B for code, a fast model for breadth).
    # Blank = use the shared llm_model/llm_base_url. Generalizes the role-backend swap to per-role.
    researcher_model: str | None = None
    developer_model: str | None = None
    researcher_base_url: str | None = None
    developer_base_url: str | None = None
    # Per-role sampling temperature (§4.1): the Researcher wants breadth (higher temp = more diverse
    # ideas), the Developer wants determinism (lower temp = fewer syntax slips), the Strategist a
    # steady hand. Each overrides the shared `llm_temperature` for that role only; None = use the
    # shared value (byte-identical to before). Flat + nullable, mirroring the per-role model fields.
    researcher_temperature: float | None = None
    developer_temperature: float | None = None
    strategist_temperature: float | None = None
    # The Strategist had per-role TEMPERATURE but no model/endpoint of its own, so in split mode it
    # always ran on the shared llm_model however the other roles were pointed. These close that gap
    # with the same names/shape as the pair above; blank = its profile, else the shared value.
    strategist_model: str | None = None
    strategist_base_url: str | None = None
    # === Connection profiles (multi-provider) =============================================
    # A "profile" is a NAMED connection: {model, base_url, temperature, api_key_env, provider}. Roles
    # point at one by name through `role_profiles`; `llm_profile` is the default for every role that
    # doesn't. All three are EMPTY by default and the resolver short-circuits to the shared
    # llm_model/llm_base_url/llm_api_key path when they are — an operator running one model never
    # needs to know the word "profile".
    #
    # A profile stores `api_key_env`, the NAME of an environment variable, never the key itself. That
    # is ADR-11's "a secret is a reference, not a value" turned from a convention into a data type:
    # the whole profile map is then safe to write into config.snapshot.json, hand back over HTTP and
    # render into LOOPLAB_* — there is nothing in it to mask. It is also what makes per-role
    # CREDENTIALS expressible at all: two roles on one provider can carry different keys, which a
    # key-per-endpoint map could not say.
    llm_profiles: dict[str, dict] = Field(default_factory=dict)
    llm_profile: str | None = None
    role_profiles: dict[str, str] = Field(default_factory=dict)
    # Unified control facade: one engine-facing object implements Researcher + Developer
    # (+ Strategist/pilot) by composing stage-specific clients, backends and local contexts. This is
    # not one shared conversation identity. ON by default — the facade drives the loop (incl. crash
    # triage: repair/abandon/reject_idea) and, via
    # `agent_drives_actions`, picks the next macro action within a pure legal-action gate. Set both
    # to False for the legacy byte-identical split-role behavior. (No-op unless backend="llm".)
    unified_agent: bool = True
    agent_drives_actions: bool = True
    # Layer 3 Card queue ownership. False preserves the existing policy/unified-pilot action path.
    # True is pinned in run_started because changing the selector on resume would mix two search
    # treatments in one run. When both this and agent_drives_actions are true, Card selection wins.
    # Default flipped 2026-08-04 (operator decision): the Card lane is the intended selector now.
    card_driven_selection: bool = True
    # Layer 5 bounded speculative Card buffer. Zero is a hard OFF switch and preserves the
    # historical alternating build/eval spine; positive values are pinned by run_started so a
    # resume cannot silently mix search treatments after a snapshot/default edit.
    #   -1 = AUTO, resolved at startup from the SETTLED concurrency width: one speculative prefetch
    #        per concurrent evaluation lane, i.e. the resolved `eval_parallel` (which is itself
    #        `0` = AUTO = one experiment per detected GPU, at least one), clamped to 1..64. It is the
    #        same shape of knob as `eval_parallel`/`llm_parallel`, but it needs its OWN sentinel
    #        because `0` here already means a hard off-switch and is a run_started-pinned search
    #        treatment — repurposing it would silently turn speculation on for every old snapshot.
    #        The startup resolution happens in `Engine.__init__`, and it is the RESOLVED integer that
    #        run_started pins, so replay/resume never re-derive a treatment from a different box. AUTO
    #        may then ratchet ITSELF down mid-run (`Engine._settle_speculation_depth`); each move is a
    #        durable `speculation_depth_settled` event, and the fold keeps that floor SEPARATE from the
    #        pin (`RunState.speculation_depth_pinned` vs `speculation_depth_settled`) so a resume can
    #        tell "the operator changed the treatment" from "the run narrowed itself".
    #    0 = off.  1..64 = that exact backlog cap (an explicit value always overrides AUTO).
    # THE DEFAULT IS -1 (AUTO) as of 2026-08-05 — the flip below has SHIPPED, and the paragraphs that
    # follow are the history of why it could not before, kept because they are the evidence for the
    # `_card_debuggable_leaf_ids` fix that unblocked it. (This block still read "THE DEFAULT STAYS 0"
    # thirty lines above `default=-1` until 2026-08-06.) Everything around it was ready: AUTO
    # resolution, the run_started pin, the node-budget refund, and the three AUTO settle-to-off rules
    # in `Engine._resolve_speculation_depth`. What used to block it was a defect in the Card
    # DEBUG anchor that only a default could make everyone's problem:
    #
    #   `events/replay.py::_card_debuggable_leaf_ids` disqualified a failed node the moment it had ANY
    #   child. A receipt-bound `debug` Card's own work item IS such a child, so the instant its node
    #   existed the Card's anchor died and it folded to `action_receipt_incomplete`. Nothing noticed
    #   while speculation was off, because the ordinary lane never re-checks a Card after its node
    #   exists. The L5 freshness gate does — that is its whole job — so EVERY speculative debug
    #   prefetch was superseded on sight, and the lane then authored a fresh unselectable Card per
    #   loop turn until the runaway guard tripped with "node creation not converging".
    #
    # Measured on a real 2-GPU run (`runs/rubert-dr-0805`, launched at AUTO): 2 nodes of a 12-node
    # budget, then stuck. Reproduced offline at depth 1 on a task whose first node crashes: 2 nodes
    # and 88 dead Cards, where the identical task with speculation off runs 12/12. Any run whose node
    # FAILED reached this, which on a real repo task is routine.
    #
    # FIXED in two halves that must agree with each other:
    #   (a) a Card's own work item no longer disqualifies its own parent, and ONLY its own — a failed
    #       node with any other child is still closed exactly as before;
    #   (b) a discarded prefetch is not counted as a child at all, because it never spent budget. The
    #       fold reads `core/models.py::is_unevaluated_speculative_discard` — the run's SINGLE answer
    #       to "did this node spend budget", which the Card lane's policy view already reads through
    #       `node_counts_toward_card_budget`. It lives in `core` (re-exported from
    #       `search/card_selection.py`, its historical home) precisely so replay can reach it: a
    #       second copy on the events side is how the two views came to disagree in the first place.
    # Same offline reproduction after the fix: 16 minted / 12 charged / 11 evaluated / normal finish.
    # The flip itself was deliberately landed on its own, after that change.
    #    -1 = AUTO (the default).  0 = off.  1..64 = that exact backlog cap.
    # AUTO ships as the default from 2026-08-05, once that fix made a `debug` prefetch survivable.
    # It resolves at startup to the settled `eval_parallel`, pins the RESOLVED integer, may ratchet
    # itself down once mid-run against its own measurements, and settles straight back to OFF at
    # startup wherever a prefetch cannot pay for itself: a build whose roles
    # call no LLM (there is no provider latency to overlap, and fan-out would cost the offline smoke
    # its byte-reproducible event order), a policy other than `greedy`, and a run with no id.
    # `EngineOptions` deliberately keeps `0` — see `engine/options.py`; a bare Engine must not gain a
    # background LLM producer and the superseded/refund lifecycle just by being constructed.
    speculation_depth: int = Field(default=-1, ge=-1, le=64)
    # Local evidence receipt produced by ``looplab speculation-gate``.  OPTIONAL, and deliberately
    # absent from the curated Settings UI.  It is the calibration BENCHMARK's receipt (scorer
    # fidelity, hit rate, divergence and normalized regret measured on the shipped quadratic toy),
    # not a licence: positive `speculation_depth` runs on any TaskAdapter without one.  When a
    # receipt IS supplied the Engine still revalidates it against the current scorer, implementation,
    # environment, GPU inventory and raw paired-run evidence, and refuses a stale or forged one.
    speculation_gate_receipt: str | None = Field(default=None, max_length=4096)
    # Per-stage model/endpoint overrides for the unified agent. Exact keys are exported as
    # `core.llm.AGENT_STAGE_KEYS`: `propose` (researcher), `implement`/`repair` (developer),
    # `strategy`, `pilot`. Unknown/mis-cased keys fail at every fresh config boundary. Empty = the
    # per-role researcher_*/developer_* models, then the shared llm_model. Explicit map wins. No-op
    # unless `unified_agent` is true.
    agent_stage_models: dict = {}
    agent_stage_base_urls: dict = {}
    llm_parser: str = "tool_call"
    # Reasoning/thinking toggle sent IN the request (providers differ — see `llm.reasoning_body`):
    #   Qwen3 on vLLM/SGLang -> chat_template_kwargs.enable_thinking (bool)
    #   OpenAI/Ollama-v1/DeepSeek -> reasoning_effort (low|medium|high|none)
    # `llm_reasoning`: "" = send nothing (server default — current behavior); off = actively disable;
    #   on = enable at default depth; low|medium|high = enable at that effort.
    # Defaults to "high" so the agent reasons before proposing/repairing/triaging (better code, fewer
    # crashes); set "" to send nothing (server default) or "off" to disable. The model's chain-of-thought
    # is CAPTURED either way (reasoning_content field or inline <think>). NOTE: to get a SEPARATE
    # reasoning_content field from SGLang/vLLM, the server also needs `--reasoning-parser qwen3`.
    llm_reasoning: str = "high"
    llm_reasoning_style: str = "auto"    # auto | qwen | effort | none — how to shape the request param
    llm_reasoning_extra: dict = {}       # raw fields merged into the body (escape hatch, e.g. Anthropic thinking)
    # H1: drive structured calls with the endpoint's constrained decoding (vLLM/SGLang guided_json +
    # response_format json_schema) so weak models can't emit invalid JSON. Off for Ollama (default).
    llm_guided_json: bool = False
    # Stream every LLM request (SSE) and reassemble it, so the request `timeout` is an INTER-TOKEN
    # idle timeout rather than a whole-request deadline: a long-but-alive generation is never cut off
    # (tokens keep resetting the timer), while a genuinely STALLED endpoint (no token for `timeout` s)
    # is detected and retried on a fresh connection — the fix for silent multi-minute hangs. Set False
    # to use one blocking read (old per-op timeout semantics) if an endpoint streams badly.
    llm_stream: bool = True
    # LLM IDLE timeout (seconds). Stream mode: the INTER-TOKEN stall limit — a steady generation is
    # never cut off, only a silent endpoint. Non-stream: bounds the whole read. Raise for endpoints
    # with a long prefill on huge prompts; lower to fail fast on a flaky shared endpoint.
    llm_timeout: float = Field(default=180.0, gt=0)
    # First-byte (response-headers) window for STREAM attempts, seconds: an SSE response sends its
    # headers on admission, so no-headers ≈ a black-holed request — fail over to a fresh connection
    # fast instead of waiting the full idle timeout. Clamped to llm_timeout. Non-stream attempts are
    # NOT bounded by this (their headers arrive only after the whole generation). The default mirrors
    # `DEFAULT_HEADER_TIMEOUT_S` in looplab/core/llm.py is the single source (imported above).
    llm_header_timeout: float = Field(default=DEFAULT_HEADER_TIMEOUT_S, gt=0)
    # Honour HTTP(S)_PROXY / NO_PROXY / SSL_CERT_FILE env vars for LLM calls. Default False: the
    # transport connects DIRECTLY (many internal/OLAP endpoints are reachable only by bypassing an
    # ambient proxy — a picked-up proxy yields "connection refused"). Set true when the endpoint is
    # reachable ONLY via a corporate proxy or needs a custom CA bundle from the environment.
    llm_trust_env: bool = False
    # H4/C2: compact the growing agentic tool-call history once it exceeds this many chars (auto_summary
    # LLM-summarizes the stale middle; else middle-truncate). Sized to the model's real context window so
    # files read earlier STAY in context instead of being compacted away and re-read: ~1,000,000 chars ≈
    # 250k tokens (≈4 chars/token), matching the configured deepseek-v4-flash (≥200k tokens confirmed).
    # The old 120k-char (~30k-token) default was ~8× too aggressive and drove a read-thrash loop. 0 = off.
    context_budget_chars: int = 1_000_000
    # M5: the Researcher's always-on experiments digest budget (chars). 0 = AUTO — scales with the
    # run size (60 chars/node, bounded [1200, 6000]) instead of one flat cap for an 8-node toy run
    # and a 200-node benchmark run alike. Set a positive value to pin it.
    digest_char_cap: int = Field(default=0, ge=0)
    # D11 compression model slot (open_deep_research's four-slot pattern): a dedicated CHEAP model
    # for agent-history summarization (C2 auto-summary), so compression doesn't pay the main
    # model's price. Blank = use each loop's own model (legacy). `compressor_base_url` blank =
    # the shared llm_base_url.
    compressor_model: str | None = None
    compressor_base_url: str | None = None
    # === Agentic tool-loop ================================================================
    # Agentic tool-loop limits — apply to EVERY tool-using agent (the LLM Researcher, the unified
    # agent's pilot + crash-triage, Deep-Research, the run-chat Boss, the genesis repo-scout, and the
    # cross-run report synthesizer). The loop lets the model call read-only tools across turns before
    # emitting its structured result. Both default to UNLIMITED so the agent takes as many turns / as
    # long as it needs — it is never cut off mid-reasoning; set a positive value here (or per-run, or
    # in the UI settings) to bound latency/cost. These used to be hardcoded per call site (e.g. the
    # boss at max_turns=3 / 45s), which silently dropped a slow reasoning model to a no-op reply.
    agent_max_turns: int = 0           # max tool turns before the emit is forced (0 = unlimited)
    agent_time_budget_s: float = 0.0   # wall-clock ceiling across the loop's turns (0 = no cap)
    # The CHAT's own wall clock, separate from the engine roles' above. `serve/assistant.py::run_turn`
    # used to read `agent_time_budget_s` and then apply `or 300.0` — so the documented "0 = no cap"
    # silently became a five-minute ceiling for the assistant and ONLY for the assistant, with no way
    # to express "no cap" for it at all and no way to raise it without also raising every engine
    # role's. That floor exists for a real reason (a stalled shared-LLM call must not leave the chat
    # "thinking" forever), so it stays as the DEFAULT — it just becomes nameable.
    #   None = inherit `agent_time_budget_s` when that is set, else the 300 s default
    #   0    = no cap (the operator's "infinite assistant mode"; pair it with a watch, not a wish)
    #   >0   = that many seconds
    # `serve/assistant.py::assistant_time_budget` is the rule, with a truth table beside it.
    assistant_time_budget_s: float | None = None
    # G · Soft convergence SAFETY NET (not a research cap). Two thresholds on the agent's tool-turn count:
    # at `agent_emit_after` the loop NUDGES the agent once ("you've investigated enough — emit now"); at
    # `agent_emit_force` it FORCES the emit from what was gathered. Set GENEROUSLY — a focused researcher
    # may legitimately read many files/experiments before it can propose a grounded idea; these only catch
    # a genuine runaway that keeps issuing DIFFERENT calls forever (which stuck-detection, keyed on
    # repeats, misses). NB: the old 499-read runaway (node 63) was 79% REDUNDANT re-reads caused by a
    # broken read_file — fixing read_file (pagination) is the real cure; this is just insurance. 0 = off.
    agent_emit_after: int = 300
    agent_emit_force: int = 500
    # B1 · No-progress / stuck detection — the safety net that makes "unlimited turns" safe. The
    # turn/time ceilings above are only BACKSTOPS; this is what actually stops a runaway loop, on the
    # cheapest signal: when the model repeats the SAME tool call (or ping-pongs between two, or keeps
    # hitting the SAME error) with no progress, the loop forces the final emit and finishes. ON by
    # default; reading DIFFERENT files or running ONE long command never trips it. (OpenHands-style.)
    agent_stuck_detection: bool = True
    agent_stuck_repeat: int = 4        # identical calls in a row that count as "stuck" (>=2)
    agent_stuck_alternate: int = 4     # ping-pong cycles between two calls that count as "stuck" (>=2)
    # C1 · Self-plan (TodoWrite-style): expose an `update_plan` tool so a long-running agent keeps its
    # OWN working TODO and re-surfaces it every `agent_plan_reinject_every` turns — keeps the goal in
    # view across a long loop so the agent "writes its own context" (Claude Code style). ON by default.
    agent_self_plan: bool = True
    agent_plan_reinject_every: int = 5
    # C2 · Auto-summary (ON by default): when the tool-loop history grows long, LLM-summarize the
    # stale middle instead of only middle-truncating it. The trigger is `context_budget_chars` if set,
    # else a built-in high-water mark (context_budget.DEFAULT_SUMMARY_CHARS ≈ 120k chars) so short
    # loops are untouched. Falls back to truncation on any summarizer error; one extra model call per
    # over-budget turn.
    agent_auto_summary: bool = True
    # C4 · Repo-developer plan decomposition. A big feature attempted in ONE developer session made a
    # non-converging reasoning model loop indefinitely — it kept writing + exploring without ever
    # emitting `done` (10k+ LLM calls / hours; agentic stuck-detection can't catch VARIED exploration,
    # only literal repeats). Fix: the developer first proposes an ordered plan of ATOMIC steps; a
    # multi-step plan is executed step-by-step, each in a FRESH bounded session with only that step's
    # scope + the files accumulated so far, syntax-validated per write. Every session stays
    # small-context and convergent. ON by default for repo tasks (a 1-step plan == the old single pass).
    developer_plan_decompose: bool = True
    developer_plan_min_steps: int = 2      # a proposed plan with >= this many steps runs step-by-step
    developer_plan_max_steps: int = 8      # cap on plan length (a runaway planner can't spawn 100 steps)
    # Hard per-session backstop for the repo developer — the ONE write-heavy agent that can run away
    # even with stuck-detection on (varied writes/reads never trip the repeat/ping-pong signal). Bounds
    # the plan phase, EACH step, and the single-session fallback. Unlike the global agent_* limits
    # (0 = unlimited, meant for read-only agents), the developer always gets a FINITE ceiling so a
    # model that never emits `done` fails cleanly with the code it wrote so far, instead of looping.
    developer_session_max_turns: int = 500
    developer_session_time_budget_s: float = 1200.0  # 20 min wall-clock per developer session
    # F2 · The Developer's PROBE (`tools/dev_probe.py`): may it RUN a short Python program against
    # the real environment while it authors? ON by default, because the failure it closes is the
    # Developer inventing a workaround for a question it could have answered — the observed shape
    # was "Since I have no shell/install ability, the cleanest repair is a small loguru shim module",
    # i.e. a fake library written instead of one line checking whether the real one imports.
    #
    # It is safe to have ON because of what the probe is, not because of what it is allowed to run:
    # the process cannot write a file anywhere (audit hook + RLIMIT_FSIZE 0), cannot start another
    # program, cannot see a GPU, and cannot read the editable source tree (it carries the same read
    # fence an eval does, always at `deny` — turning `read_fence` off must not open a second door).
    # Its whole world is a temporary directory the engine deletes when the call returns, which is
    # also why it needs no domain event: there is no side effect for invariant #3 to gate.
    # Set False to remove the tool from the Developer's toolset entirely.
    developer_probe: bool = True
    # Per-probe wall clock. Deliberately short: a probe is a QUESTION ("does this import", "does this
    # CSV parse"), and anything that needs longer is a declared eval stage — the surface that has a
    # metric contract, a live log and a repair loop attached. Hard-capped at 300 s in the tool.
    # There is deliberately NO probe COUNT budget: the developer session already carries a finite
    # wall-clock ceiling (`developer_session_time_budget_s`), which a probe spends like any other
    # turn, and a second fixed counter is the shape doc 36 names as the category error.
    developer_probe_timeout_s: float = 60.0
    # Phase-handoff summaries. Each LLM phase in a node build (Researcher·propose → Developer·stages →
    # plan → implement) ends with ONE extra LLM call that distills its transcript — the repo structure
    # it mapped, files/data confirmed, decisions made — into a brief injected into the NEXT phase (even
    # across the role boundary). The next phase TRUSTS that instead of re-reading the same files, which
    # cuts the biggest source of tool-call thrash (stages/plan/implement each re-exploring the repo).
    # One summary call per phase in exchange for many fewer read calls downstream. ON by default.
    phase_handoff_summary: bool = True
    # Observability (ADR-17): capture each LLM call's full prompt + completion into the active
    # span (spans.jsonl) so the UI can show exactly what the model read and wrote per node.
    # Diagnostics only — `replay.fold` never reads spans. Default on for local single-user; set
    # LOOPLAB_TRACE_LLM_IO=0 to suppress (e.g. to keep spans.jsonl small or avoid storing prompts).
    trace_llm_io: bool = True
    # Run-introspection tools (ON by default): make the LLM Researcher (and DeepResearcher) a
    # tool-using agent that can read its OWN experiments (list/read/find-analogous/themes/code) and
    # the task data (schema/profile/asset) mid-loop, instead of seeing only best+parent. Advisory —
    # never changes best-selection. Off = the legacy single-shot Researcher (richer digest still added).
    researcher_tools: bool = True
    # Cross-run introspection: give the agentic Researcher / pilot read-only tools to look at
    # SIBLING runs (same task_id, same run-root) — list them, read an experiment / its code, and
    # find analogous configs across runs — so a run can build on what neighbouring runs already
    # learned instead of rediscovering it. Advisory; never changes best-selection. Needs the run's
    # own dir wired through (no-op for unit-built roles). Off = the legacy single-run view only.
    cross_run_tools: bool = True
    # Read-only access to every run under this configured run-root ACROSS ALL TASKS (not just
    # same-task siblings):
    # enabled reasoning roles get list_all_runs + read_run_code + read_run_experiment so they can read
    # the code + result of any past experiment in that workspace and reuse an approach. Broader than
    # `cross_run_tools` (same-task only); the agent decides when a foreign run is relevant. Advisory;
    # never changes best-selection. It is not machine-wide; needs the run's own dir wired through
    # (no-op for unit-built roles).
    all_runs_tools: bool = True
    # PART V §22 — read-only CROSS-RUN KNOWLEDGE tool for the reasoning roles (Researcher, Strategist,
    # deep-research, and a Developer-scoped variant). Adds `cross_run_prior_attempts` / `cross_run_claims`
    # / `cross_run_atlas` over the §21.20 read-models (concept overview + claim assessments + atlas), read
    # from `memory_dir` (lessons.jsonl + concept_capsules.jsonl). Read-only is a storage-authority
    # boundary, not a behavioral no-op: retrieved content can steer later proposals, and a tool-using
    # reasoning loop can consume additional model calls. The tools never mutate cross-run truth (facts
    # are engine-written, verdicts operator-ratified, §22.4) or directly select a winner. ON by explicit
    # experimental product choice, EngineOptions off; stores only exist once Part-IV features have run.
    cross_run_read_tools: bool = True
    # Agentic retrieval (ADR-16): if set, the LLM Researcher gets grep/kb_search/read
    # tools over this directory of markdown notes and chooses when to use them.
    knowledge_dir: str | None = Field(default_factory=lambda: str(_LL_HOME / "knowledge"))
    # T4 real embeddings: model id for an OpenAI-compatible `/embeddings` endpoint used for
    # kb_search / case retrieval. Blank (default) = the dependency-free lexical `hash_embed` (today's
    # behavior). Set e.g. "nomic-embed-text" (Ollama) or "text-embedding-3-small" for SEMANTIC
    # retrieval; a misconfigured/offline endpoint degrades back to hash_embed at call time (never
    # crashes a run). `embed_base_url` blank = reuse `llm_base_url`; the shared `llm_api_key` is used.
    embed_model: str | None = None
    embed_base_url: str | None = None
    # Harmonic memory (Memora, ICML'26 — idea import). When ON, cross-run cases and knowledge notes are
    # indexed by a short ABSTRACTION + cue ANCHORS instead of their raw text, near-duplicate memories are
    # CONSOLIDATED into one entry under a matching abstraction, and `kb_search`/case retrieval EXPAND
    # through the top hits' anchors to surface related-but-not-similar memories. ON by default. Set
    # `memora=false` to restore the pre-Memora raw-text index. Never a source of truth — abstractions
    # live only in the derived, rebuildable retrieval index (never the event log or canonical cases.jsonl).
    memora: bool = True
    # Write abstractions with the wired chat model (richer than the deterministic lexical fallback). ON
    # by default; results are CACHED by content hash (see `memora_cache`) so a re-built index doesn't
    # re-call the model on unchanged notes/cases, and any endpoint failure degrades to lexical at call
    # time — so an offline box just gets lexical abstractions, never a crash. Set false to force lexical.
    memora_llm: bool = True
    # === Cross-run memory / Memora ========================================================
    memora_anchors: int = 6           # max cue anchors kept per memory
    # Cosine similarity at/above which a new case is CONSOLIDATED into an existing one (same evolving
    # topic) rather than stored as a separate entry. Only used when `memora` is on.
    # BOUNDED like its 0..1 sibling `novelty_semantic_threshold`: consumers compare
    # `cosine(...) >= consolidate_threshold` (engine/memory.py, tools/knowledge_tools.py) and cosine
    # cannot exceed 1.0, so an unbounded typo above 1.0 validated cleanly and silently disabled
    # consolidation forever — while a negative one consolidated everything. Fail loud instead.
    memora_consolidate_threshold: float = Field(default=0.86, ge=0.0, le=1.0)
    # Where the LLM-abstraction cache lives (JSON). Blank (default) derives it from `memory_dir`
    # (`<memory_dir>/memora_cache.json`) or else `knowledge_dir` (`<knowledge_dir>/.memora_cache.json`);
    # with neither set the cache is in-memory only (per process). No-op unless `memora_llm` is on.
    memora_cache: str | None = None
    # E3 literature-grounded ideation (network-OPTIONAL): give the agentic Researcher an arXiv search
    # tool to ground ideas in real techniques. Off by default (egress is unreliable on some boxes);
    # fails gracefully if the network is blocked.
    literature_search: bool = False
    # Deep-Research stage (network-OPTIONAL): give the DeepResearcher a general web search/fetch
    # tool (DuckDuckGo) on top of arXiv to read across results + the web before steering the next
    # batch. Off by default (egress is unreliable on some boxes); fails gracefully when blocked.
    web_search: bool = False
    # Cadence for the Deep-Research stage: run it automatically every N created nodes.
    # **`0` MEANS START IMMEDIATELY, NOT "off"** — this is the one interval knob in `Settings` whose
    # zero is not the off switch, and the rule is stated once in `engine/cadence.py`
    # (`deep_research_window`, which settles `0` to a one-node window and anything NEGATIVE to
    # disabled). Off is spelled `-1` (`cadence.DEEP_RESEARCH_OFF`); there, as at `0` before
    # 2026-08-07, manual `deep_research` and Strategist `request_research` triggers still fire.
    # Default 0 (owner decision 2026-08-07): deep research analyzes the accumulating results and
    # steers the next batch (its directions become hints + open hypotheses), and paired with
    # `concurrent_research` it overlaps the think with the GPU-bound eval — so on the workload the
    # feature exists for it costs LLM tokens and no wall clock, and there is nothing for a start
    # restriction to protect. The old default of 3 was measured to make the stage UNREACHABLE there:
    # `runs/rubert-dr-0804/0805/0807` (1.5-4 h per node) recorded zero `research_attempted` and zero
    # `research_completed` rows each, because three nodes is 5-12 hours away.
    # Independently of the interval, ANY enabled value (i.e. anything but `-1`) also buys ONE
    # run-OPENING think at node-count 0, before the first idea is proposed
    # (`engine/research_cadence.py::_ground_run_start`): the window governs how OFTEN the run
    # re-thinks, never whether its first proposal is grounded. It leaves an `at_node=0` mark, which
    # is what `max(marks, default=0)` already reads as "never fired", so the interval is not
    # re-phased by it.
    deep_research_every: int = 0
    # Overlap a DUE deep-research "think" with the GPU-bound eval instead of running it in its own
    # serial step (the agent is otherwise idle while a node trains). research() computes from a state
    # snapshot; the joined background task records only allowlisted, order-tolerant
    # research/hint/hypothesis events as soon as the memo finishes. ON by default: the LLM is typically
    # remote (no GPU contention with eval), so overlapping hides latency behind long training.
    concurrent_research: bool = True
    # Concurrent-research REPEAT — don't idle a multi-DAY eval. The overlapped "think" fires ONCE per
    # window by default; ON here, it RE-RUNS on an adaptive time cadence for the WHOLE eval window
    # (a two-day training must not leave the reasoning agents idle after one memo). Self-paced: it
    # only records a memo whose CONTENT is new (identical re-runs are skipped, so the log/board don't
    # bloat) and backs off geometrically as the analysis converges, capped so it always re-checks.
    # The appended records never rewrite the current metric champion, but their hints/open hypotheses
    # deliberately steer later proposals and replay reconstructs that advice. The library default is
    # one-shot (== today, byte-identical); this product default turns the repetition on.
    concurrent_research_repeat: bool = True
    # Base cadence (seconds) between repeated concurrent-research passes. Research is EXPENSIVE
    # (multi-turn LLM + web/arXiv), so this is a FLOOR, not a ceiling: the effective cadence is
    # max(this, ~5% of the per-experiment time budget) — a two-day eval is re-researched about hourly,
    # a short eval not at all (the first tick outlasts it). Only matters when the repeat is on.
    concurrent_research_interval_s: float = Field(default=1800.0, gt=0)
    # Per-eval-window backstop on repeated-research LLM calls (0 = cadence-only, no cap). The adaptive
    # cadence + convergence backoff are the primary budget control; this bounds a pathological
    # never-converging window. Past it the loop stops calling the LLM (the health monitor still runs).
    concurrent_research_max_calls: int = Field(default=40, ge=0)
    # Concurrent hypothesis-board CONSOLIDATION during the eval window. Repeated research keeps ADDING
    # near-duplicate directions to the open board (each memo registers up to 5 as open hypotheses), so a
    # long eval's board bloats before the between-nodes merge runs. This dedups/merges it on the SAME
    # background loop so the board stays clean for the next proposal. In Card-driven mode the same merge
    # changes ownership/readiness, so it is deferred to the joined main-task cadence instead. Needs the
    # reflect client + `track_hypotheses`.
    # Only active while `concurrent_research` runs the overlap loop. Library default off (== today).
    concurrent_consolidate: bool = True
    # Cadence for the agent-authored run report: regenerate the conclusion-first narrative every N
    # created nodes (0 = off; it still regenerates on a manual `report_refresh` from the UI). The
    # deterministic report always renders from the node set regardless of this knob.
    report_every: int = 3
    # Agent Skills (I18, ADR-9): dir of SKILL.md the Researcher can list/load as tools.
    skills_dir: str | None = None
    # Prompt store (I18, ADR-8): dir of editable, hot-reloaded role prompt .md files.
    prompt_dir: str | None = None
    # API key as a reference, never serialized as a value (ADR-11). Local servers
    # ignore it; default to a placeholder.
    llm_api_key: SecretStr | None = None
    # Runtime-only endpoint binding for the shared credential. A key is usable only when this
    # canonical URL exactly matches the transport target. It is deliberately absent from run/UI
    # snapshots: the owner-side secret store persists key + binding together, while an operator who
    # supplies LOOPLAB_LLM_API_KEY directly must also supply LOOPLAB_LLM_API_KEY_BASE_URL.
    llm_api_key_base_url: str | None = None
    # Set only by a trusted source-aware resolver after it has selected key+binding as one record.
    # Config files, task payloads and --set cannot populate PrivateAttr.
    _llm_credential_pair_trusted: bool = PrivateAttr(default=False)

    @model_validator(mode="before")
    @classmethod
    def _reject_bool_for_numeric_fields(cls, data):
        """A JSON boolean is a TYPE ERROR for a numeric setting, not the number 1.

        Pydantic runs in lax mode here (`model_config` sets no `strict`), and `isinstance(True, int)`
        is True — so `{"max_nodes": true}` from a UI/API type slip validated cleanly as a budget of
        ONE node, and `{"eval_parallel": true}` collapsed the width to one. The run then looked
        configured and simply did almost nothing, with no 422 and nothing in the snapshot to show why.
        Rejecting at the boundary covers every numeric field at once, including the many that carry no
        `Field(ge=...)` bound of their own to trip over.
        """
        if not isinstance(data, dict):
            return data
        for key, val in data.items():
            if type(val) is bool and _is_numeric_annotation(
                    getattr(cls.model_fields.get(key), "annotation", None)):
                raise ValueError(f"{key} must be a number, not a boolean (got {val!r})")
        return data

    @field_validator("agent_stage_models", "agent_stage_base_urls", mode="before")
    @classmethod
    def _check_agent_stage_map(cls, value, info):
        """Reject stage-map typos at the common Settings boundary.

        A list of pairs is not a map, keys are exact strings rather than values Pydantic may coerce,
        and overrides are nonblank strings.  The resolver uses truthiness for map precedence and
        ``LlmTarget`` values as cache keys, so accepting a blank, mapping or numeric value here would
        otherwise become either a silent fallback or a late unhashable/transport error.
        """
        field = info.field_name
        if not isinstance(value, Mapping):
            raise ValueError(f"{field} must be a mapping of agent stage key -> override")
        non_string = sorted(repr(key) for key in value if not isinstance(key, str))
        if non_string:
            raise ValueError(
                f"{field} keys must be exact strings from AGENT_STAGE_KEYS; invalid key(s): "
                + ", ".join(non_string))
        unknown = sorted(key for key in value if key not in AGENT_STAGE_KEYS)
        if unknown:
            allowed = ", ".join(sorted(AGENT_STAGE_KEYS))
            raise ValueError(
                f"{field} contains unknown agent stage key(s): "
                f"{', '.join(repr(key) for key in unknown)}; allowed: {allowed}")
        invalid_values = sorted(
            repr(key) for key, override in value.items()
            if not isinstance(override, str) or not override.strip()
        )
        if invalid_values:
            raise ValueError(
                f"{field} values must be nonblank strings; invalid stage key(s): "
                + ", ".join(invalid_values))
        return dict(value)

    @field_validator("eval_env", mode="before")
    @classmethod
    def _eval_env_map(cls, value):
        """Accept the run-level declared environment in either spelling, then validate it with the
        ONE shared rule (`runtime/command_eval.validate_env_map`).

        TWO SPELLINGS, because the two surfaces that reach this field are not alike and neither may
        be the awkward one. A config file's `settings:` block and `LOOPLAB_EVAL_ENV` speak
        YAML/JSON, so a MAPPING is the natural form there and is what the snapshot round-trips.
        `--set` is a shell argument split on the FIRST `=` (`appconfig.parse_sets`), so what arrives
        from `-s eval_env=VS_LOCAL_DATA_ROOT=/data/local` is the STRING `VS_LOCAL_DATA_ROOT=/data/local`
        — the exact thing an operator would have typed as a shell export, and the whole point of the
        field is that they should not have to. Requiring JSON there instead
        (`-s eval_env={"VS_LOCAL_DATA_ROOT":"/data/local"}`) would be a quoting puzzle in front of the
        one launch line this exists to make sayable, so the string form is parsed here: comma-separated
        `NAME=VALUE` pairs, each split on its own first `=` so a value may contain more of them.

        Note the field stays FLAT and 1:1 with `LOOPLAB_EVAL_ENV`, as every Settings field must —
        what is nested is the VALUE, exactly as it already is for `llm_profiles`/`role_profiles`.
        """
        from looplab.core.envsafe import validate_env_map
        if value is None or value == "":
            return {}
        if isinstance(value, str):
            parsed: dict = {}
            for item in value.split(","):
                item = item.strip()
                if not item:
                    continue
                if "=" not in item:
                    raise ValueError(
                        "eval_env as a string is comma-separated NAME=VALUE pairs, e.g. "
                        f"VS_LOCAL_DATA_ROOT=/data/local — got {item!r} with no '='")
                name, _, val = item.partition("=")
                parsed[name.strip()] = val
            value = parsed
        clean, err = validate_env_map("eval_env", value)
        if err:
            raise ValueError(err)
        return clean

    @model_validator(mode="before")
    @classmethod
    def _apply_profile(cls, data):
        """Expand `profile` into a bundle of setting defaults BEFORE validation.

        pydantic-settings hands this validator the fully-merged input from ALL explicit sources
        (init kwargs + LOOPLAB_* env + .env), with fields the user left unset simply ABSENT (they
        fall to field defaults afterward). So a profile value is applied only when its key is not
        already present — i.e. any explicit file/CLI/env setting wins over the profile, and the
        profile wins over the bare field default. This makes `thorough` a true convenience preset,
        never an override of what the user asked for."""
        if not isinstance(data, dict):
            return data
        prof = data.get("profile", "default")
        # env may deliver the key as "profile" (env_prefix stripped) — normalize to a str
        prof = str(prof).strip().lower() if prof is not None else "default"
        overrides = PROFILES.get(prof)
        if overrides is None:
            raise ValueError(f"unknown profile {prof!r}; known: {sorted(PROFILES)}")
        for key, val in overrides.items():
            if key not in data:          # explicit source (file/CLI/env) always wins
                data[key] = val
        return data

    # The closed-vocabulary string fields, as a TABLE rather than a hand-written if-chain (doc 25
    # CO-13). Three accretion waves had appended to that chain, each repeating the same
    # "must be a|b|c, got {!r}" message, and a new enum-ish field is exactly the kind of thing that
    # gets added to `Settings` and forgotten here — where forgetting is silent: an out-of-set value
    # does not fail, it falls through whatever consumes the field as a NO-OP. A mis-cased
    # `LOOPLAB_NOVELTY_MODE=LLM` turned the novelty gate off with no diagnostic; a mis-cased
    # `strategist_backend` quietly ran the default; a typo'd `llm_parser` is indistinguishable from
    # asking for the default fallback order.
    #
    # Two entries resolve their vocabulary LAZILY, and for different reasons:
    #   * `developer_backend` is validated against `DEVELOPER_BACKENDS`, which `agents/cli_agent.py`
    #     asserts its PRESETS match — so a typo still fails loud without core executing
    #     agents-package code on every Settings construction (doc 25 XP-04: the one upward import
    #     out of core).
    #   * `llm_parser` is validated against the parser registry itself, imported inside the callable
    #     to keep `config` import-light.
    _ENUM_FIELDS: typing.ClassVar[tuple] = (
        ("trust_gate", ("audit", "gate", "block")),
        ("metric_salvage", ("off", "audit", "select")),
        ("merge_mode", ("auto", "mean", "ensemble")),
        ("novelty_mode", ("off", "algo", "llm")),
        ("strategist_backend", ("off", "rule", "llm", "agent")),
        ("eval_trust_mode", ("ratify_freeze", "autonomous", "ratify_freeze_drift")),
        ("seed_mode", ("auto", "tracked", "all")),
        # Spelled out rather than imported from `runtime/read_fence.py::POLICIES`: `core` imports
        # nothing above itself, and `runtime` imports `core`, so the import would close a cycle.
        # `tests/test_read_fence.py` pins the two spellings equal.
        ("read_fence", ("off", "warn", "deny")),
        # Same reason as `read_fence` above — spelled out rather than imported from
        # `runtime/metric_subject.py::MODES` / `runtime/landlock.py::POLICIES`, because core imports
        # nothing above itself. `tests/test_metric_subject.py` pins all three pairs equal.
        ("metric_subject", ("off", "audit", "require")),
        ("landlock", ("off", "enforce")),
        ("backend", ("toy", "llm")),
        ("developer_backend", lambda: DEVELOPER_BACKENDS),
        ("llm_parser", lambda: _parser_names()),
    )

    @model_validator(mode="after")
    def _check_enum_fields(self):
        for field, vocabulary in self._ENUM_FIELDS:
            allowed = vocabulary() if callable(vocabulary) else vocabulary
            value = getattr(self, field)
            if value not in allowed:
                raise ValueError(f"{field} must be {'|'.join(allowed)}, got {value!r}")
        self._check_member_fields()
        self._check_llm_profiles()
        return self

    # The SET-valued sibling of `_ENUM_FIELDS`: a field whose value is a collection every MEMBER of
    # which must be in a closed vocabulary. The failure mode is the same one that table exists for
    # and is if anything quieter — `inline_repair_reasons=("typo",)` was accepted in full, and the
    # engine's gate is `reason not in self._inline_repair_reasons`, so the setting simply never
    # matched anything and inline repair was OFF for every failure class with nothing anywhere
    # saying so. That is not a hypothetical shape: the whole point of widening this default on
    # 2026-08-12 was that a reason nobody had decided to drop was being dropped silently, and a
    # mistyped narrowing re-creates it one layer up. Env override expects a JSON array, which is
    # exactly where a typo comes from.
    _MEMBER_FIELDS: typing.ClassVar[tuple] = (
        ("inline_repair_reasons", lambda: FAILURE_REASONS),
    )

    def _check_member_fields(self) -> None:
        for field, vocabulary in self._MEMBER_FIELDS:
            allowed = vocabulary() if callable(vocabulary) else vocabulary
            value = getattr(self, field)
            unknown = [v for v in (value or ()) if v not in allowed]
            if unknown:
                raise ValueError(
                    f"{field} contains {unknown!r}, which {'is' if len(unknown) == 1 else 'are'} "
                    f"not {'a ' if len(unknown) == 1 else ''}failure reason"
                    f"{'' if len(unknown) == 1 else 's'}; known: {'|'.join(allowed)}")

    def _check_llm_profiles(self) -> None:
        """Validate the connection-profile maps LOUDLY, at construction.

        Assignment is not validated anywhere in this project, and these two maps are the kind of
        thing a typo makes into a silent no-op: an unknown role name would simply never be read, and
        a reference to a missing profile would fall back to the shared model while the operator
        believed a second provider was in play. Both are errors here instead.

        The `api_key_env` rules exist for a specific reason. It must LOOK like a secret to
        `runtime/sandbox.py::SECRET_ENV`, because that is the filter that strips the operator's keys
        out of the environment handed to generated candidate code — a key parked in a variable named
        `MY_HANDLE` would be invisible to it and ride straight into the sandbox. This pattern is
        deliberately the STRICTER of the two (the sandbox has to recognize whatever already exists;
        this names a variable the operator is about to create), and it is duplicated below rather than
        imported because layering forbids `core` from importing `runtime`.
        `tests/test_secret_env_pattern.py` pins the two together — including the invariant that every
        name accepted here is one the sandbox strips. And a literal key inside a profile is refused
        outright: the profile map is written verbatim into config.snapshot.json and served over HTTP
        precisely because it is supposed to contain no secrets."""
        from looplab.core.llm import LLM_ROLE_KEYS
        if not isinstance(self.llm_profiles, dict):
            raise ValueError("llm_profiles must be a map of profile name -> profile")
        for name, entry in self.llm_profiles.items():
            if not isinstance(entry, dict):
                raise ValueError(f"llm_profiles[{name!r}] must be an object")
            # BEFORE the unknown-field check: none of these is a known field, so checking them
            # afterwards could never fire and someone who pasted a live key was told it was a typo.
            for banned in ("api_key", "key", "token", "secret"):
                if banned in entry:
                    raise ValueError(
                        f"llm_profiles[{name!r}] must not contain {banned!r}: a profile stores "
                        "api_key_env (the NAME of an environment variable), never a key value — "
                        "the whole map is written to config.snapshot.json and served over HTTP")
            unknown = set(entry) - _PROFILE_FIELDS
            if unknown:
                raise ValueError(
                    f"llm_profiles[{name!r}] has unknown field(s) {sorted(unknown)}; "
                    f"known: {sorted(_PROFILE_FIELDS)}")
            env = entry.get("api_key_env")
            if env is not None and not _SECRET_ENV_NAME.match(str(env)):
                raise ValueError(
                    f"llm_profiles[{name!r}].api_key_env={env!r} is not a usable variable name: it "
                    "must be UPPER_SNAKE and contain KEY/SECRET/TOKEN/PASSWORD/PASSWD/CREDENTIAL, so "
                    "the sandbox's secret-name filter strips it from generated code's environment")
            temp = entry.get("temperature")
            if temp is not None and (isinstance(temp, bool) or not isinstance(temp, (int, float))):
                raise ValueError(f"llm_profiles[{name!r}].temperature must be a number")
        if not isinstance(self.role_profiles, dict):
            raise ValueError("role_profiles must be a map of role name -> profile name")
        for role, profile in self.role_profiles.items():
            if role not in LLM_ROLE_KEYS:
                raise ValueError(
                    f"role_profiles has unknown role {role!r}; known: {sorted(LLM_ROLE_KEYS)}")
            if profile not in self.llm_profiles:
                raise ValueError(
                    f"role_profiles[{role!r}] names profile {profile!r}, which is not in llm_profiles")
        if self.llm_profile is not None and self.llm_profile not in self.llm_profiles:
            raise ValueError(
                f"llm_profile={self.llm_profile!r} is not in llm_profiles")

    def masked_snapshot(self) -> dict:
        # Snapshots are a JSON contract (they are written verbatim to config.snapshot.json), not a
        # Python-object dump.  In particular tuple-valued schema fields such as
        # ``inline_repair_reasons`` must become JSON arrays before callers hash the runtime envelope;
        # otherwise the strict speculation-scope canonicalizer sees a different/non-JSON preimage
        # from the one that ``json.dumps`` eventually persists.
        d = self.model_dump(mode="json")
        if self.llm_api_key is not None:
            d["llm_api_key"] = "***"
        else:
            d["llm_api_key"] = None
        d.pop("llm_api_key_base_url", None)
        # The snapshot's own FORMAT version — not a Settings field (it is never settable by env or
        # file, and it describes the document, not the run). `settings_from_snapshot` fails closed on
        # a version it does not understand; see CONFIG_SNAPSHOT_SCHEMA.
        d[CONFIG_SNAPSHOT_SCHEMA_KEY] = CONFIG_SNAPSHOT_SCHEMA
        return d


# The config snapshot's document-format version, written by `masked_snapshot` and enforced by
# `settings_from_snapshot`. Bump it when a snapshot written by this binary would be MISREAD by an
# older one — i.e. when a new field changes paid, concurrency or selection behaviour and silently
# defaulting it would resume the same event history under different semantics. `Settings` is
# `extra="ignore"`, so without this an older binary drops what it does not recognize and resumes
# anyway, with no diagnostic; a version it does not know is now a loud refusal instead.
# Version 1 was the first VERSIONED format. Version 2 adds the explicit
# `task_facets_finalize` treatment bit: an older binary would otherwise ignore that new field and
# resume a fresh default-off run with the historical paid facet call enabled. An absent key still
# means a pre-versioning snapshot and remains readable through `LEGACY_CONFIG_SNAPSHOT_DEFAULTS`.
CONFIG_SNAPSHOT_SCHEMA = 2
CONFIG_SNAPSHOT_SCHEMA_KEY = "config_snapshot_schema"


class ConfigSnapshotVersionError(ValueError):
    """A config snapshot was written by a NEWER binary than the one reading it."""


# Pre-versioned snapshots are full Settings dumps, so absence is a reliable per-field version marker.
# Keep their historical effective behavior when newer product defaults become active: re-entry must not
# silently add paid calls, interventions, concurrency or a different selection policy to an old run.
LEGACY_CONFIG_SNAPSHOT_DEFAULTS: dict[str, object] = {
    "parallel_build": 1,
    "eval_parallel": None,
    "llm_parallel": None,
    "train_monitor": False,
    "train_monitor_interval_s": 600.0,
    "train_monitor_kill": False,
    "train_monitor_kill_confidence": 0.8,
    # An old run resumes WITHOUT the log tools: this table exists so a resume cannot silently add
    # paid calls, and an agentic tick costs up to `_MONITOR_LOOK_TURNS` extra round trips.
    "train_monitor_tools": False,
    # Same rule, same reason, for the triage judge's look: an old run resumes with the historical
    # single ask. It needs its OWN row rather than riding on the one above — they are two independent
    # `Settings` fields, and a snapshot written between the two changes carries `train_monitor_tools`
    # explicitly (so no `setdefault` fires for it) while genuinely predating this one.
    "repair_log_tools": False,
    "asha_live": False,
    "asha_live_kill": False,
    "asha_live_quantile": 0.5,
    "asha_live_min_siblings": 3,
    # Before this operator ceiling existed, the sandbox's 24-hour process limit was authoritative.
    "max_eval_timeout": 24 * 3600.0,
    "watchdog_reflection": False,
    "card_driven_selection": False,
    # docs/29 F1. A run launched before the proposals could move its width never consented to the
    # engine re-pinning one mid-log, and re-entry must not add that treatment to it — the same reason
    # every other concurrency row here is pinned to its historical value.
    "proposal_width": False,
    "speculation_depth": 0,
    "speculation_gate_receipt": None,
    "concurrent_research_repeat": False,
    "concurrent_research_interval_s": 1800.0,
    # Zero means unlimited; 40 was the first bounded default, and is latent while repeat is disabled.
    "concurrent_research_max_calls": 40,
    "concurrent_consolidate": False,
    # The map originally covered only the flags added in the days just before it was written
    # (the entries above are all 2026-07-19..07-21; the map itself landed 07-22). Everything below was
    # added 2026-06-24..07-17 — AFTER `config.snapshot.json` existed (it ships in the initial commit,
    # 2026-06-23) and BEFORE this map did — so a snapshot from that window carries none of these keys
    # and, without an entry here, resumed with today's paid product default ACTIVE. That is precisely
    # the "silently add paid calls ... to an old run" the paragraph above forbids: the resumed half of
    # the run would make predict-before-execute and cross-run LLM calls the original never made, and
    # admit candidates under a different novelty policy.
    # `setdefault` makes every one of these a no-op for a snapshot that DOES carry the key, so only
    # genuinely older runs are affected. Each value is the behaviour before the field existed: absent
    # feature = off, absent cadence = OFF — which for every cadence here is spelled `0`, and for
    # `deep_research_every` alone is spelled `-1` since 2026-08-07 (its `0` now means "start
    # immediately"; see that field and `engine/cadence.py::deep_research_window`). The value is
    # always the same "conservative library" one the frozen Settings-vs-EngineOptions divergence
    # table records for the field. Each was dated against the tree before being listed — e.g.
    # `report_every` (2026-06-25) is 0 because the run-report writer itself did not exist before that
    # commit, so an older run wrote no report to preserve.
    "foresight": False,
    "foresight_agentic": False,
    "foresight_verify": False,
    "concept_pivot": False,
    "graded_novelty": False,
    "cross_run_concepts": False,
    "cross_run_advisory": False,
    "cross_run_structured_claims": False,
    "cross_run_curation": False,
    # Before task faceting gained its own scheduling switch, `cross_run_curation=True` always ran
    # the facet steward. A snapshot that lacks this field must resume that same treatment. This is
    # still latent when the umbrella curation switch above is false, so genuinely older runs do not
    # acquire paid work they never had.
    "task_facets_finalize": True,
    # A run whose snapshot predates the ratification stage never consented to an agent changing the
    # cross-run taxonomy; resuming it must not start doing so. (Same as the live default today, but
    # this map is the place that keeps it true if the default ever flips.)
    "concept_tidy": False,
    "cross_run_read_tools": False,
    "concept_run_base": False,
    "fingerprint_universal": False,
    "memora_llm": False,
    "comparative_lessons": False,
    "lessons_every": 0,
    "lessons_refresh_every": 0,
    "unified_agent": False,
    # -1, not 0: a pre-field snapshot means "this run never ran the Deep-Research cadence", and since
    # 2026-08-07 that is spelled `-1`. Writing `0` here would resume every legacy run into a paid
    # think at EVERY node — the exact "silently add paid calls to an old run" this map exists to stop.
    "deep_research_every": -1,
    "report_every": 0,
    "failure_reflection": False,
    "reflection_priors": False,
    "agent_drives_actions": False,
    "concurrent_research": False,
    "deep_repair": False,
    # `merge_mode` meets all three conditions below, confirmed against the commits: the field was
    # introduced 2026-06-24 (bb421e0f), one day AFTER the cutoff, so a pre-cutoff snapshot carries no
    # such key; it arrived defaulting to "mean" and only later flipped to "auto" (a8b37944,
    # 2026-07-03), which `orchestrator.__init__` resolves to "ensemble" for a code-generating
    # developer — a PAID code-recombination merge producing a DIFFERENT merged solution; and the
    # before-the-field value is pointable and still frozen as the library default in
    # `EngineOptions` / `orchestrator._DEFAULTS`. Without this entry a pre-field snapshot resumed on
    # an LLM backend silently gained ensemble merges it never did.
    "merge_mode": "mean",
    # The one entry of a DIFFERENT kind, and the reason the rule below says "a new field" rather
    # than "a new key": `inline_repair_attempts` is not new — it is a pre-existing field whose
    # DEFAULT changed (0 = unlimited -> 12) on 2026-08-05, after four measured ways the unlimited
    # loop could run without a terminal (see the field's own comment). A full-dump snapshot carries
    # the key, so for those this is inert `setdefault`; what it protects is the pre-versioning
    # snapshot that does NOT, which must resume the run it was written for — an operator who chose
    # unlimited in-node repair keeps it, rather than silently acquiring a cap mid-run and having a
    # node abandon at 12 that the first half of the same run would have kept repairing. It satisfies
    # (c) exactly (0 is pointable at every commit before this one) and satisfies (b) in the mirror
    # direction: re-entry must not silently REMOVE work the run was doing either.
    "inline_repair_attempts": 0,
    # METRIC SALVAGE, both halves, and they satisfy (a)+(b)+(c) as cleanly as any entry here.
    # (a) both fields were added 2026-08-12. (b) `audit` changes the SELECTION POLICY of a resumed
    # run — a node that would have failed becomes `evaluated`, counts against the node budget, and
    # joins the lineage the Researcher breeds and reports from — and `metric_salvage_repair` adds a
    # PAID Developer call the original run never made. (c) the before-the-field behaviour is
    # pointable at every commit before that date and is spelled exactly by `off`/False, which is what
    # `metric_salvage: "off"`'s own comment means by "the behaviour before 2026-08-12".
    # Without these two rows, resuming any of the 38 pre-versioning snapshots turned salvage on
    # mid-run: the first half of the run discarded a node with an unreadable declaration and the
    # second half admitted one, in the same event log, with nobody having chosen either.
    "metric_salvage": "off",
    "metric_salvage_repair": False,
    # THE METRIC SUBJECT BINDING, added 2026-08-13. It satisfies (a)+(b)+(c). (a) the field did not
    # exist. (b) the resumed run acquires a `metric_provenance` record on every node terminal that
    # the first half of its own event log does not have, and — once the live value is `require` — a
    # `metric_salvaged` violation that makes an unbound node UNSELECTABLE plus a derived `needs` on
    # the score stage that can fail it outright and buy inline repair attempts. (c) is `off`, which
    # is exactly what "before 2026-08-13" means. A resumed run keeps the account it was written with;
    # a NEW run gets the record.
    #
    # This row's (b) used to be argued from the `needs` derivation firing at the `audit` rung. That
    # was TRUE of the code and false of the contract, and the code is what moved: since 2026-08-14
    # the derived `needs` belongs to `require` alone (see `Settings.metric_subject`). The entry stays
    # and its value stays `off` — the provenance record is itself an additive change to every
    # terminal, `require` remains reachable on a resume, and re-pinning a LEGACY default would change
    # how an already-recorded run replays, which is the one thing this table exists to prevent.
    "metric_subject": "off",
    # THE DEADLINE GRACE, whose DEFAULT changed (0.0 -> -1 AUTO) on 2026-08-23. Like
    # `inline_repair_attempts` and `repair_reasons` this is a pre-existing field, so (a) cannot
    # apply and (b)+(c) carry it. (b) is paid work in both currencies at once: a resumed run would
    # start making a judge call at EVERY stage deadline the original made none at, and would hand
    # out up to 10% more wall clock per stage — on a nine-hour training, GPU-hours nobody chose,
    # in the same event log whose first half killed on the wall. (c) is `0.0`, pointable at every
    # commit between the field landing (2026-08-13) and this one, and it is exactly what that
    # field's own comment still means by "the unconditional tree-kill, byte for byte".
    "eval_deadline_grace_s": 0.0,
    # THE RUN-LEVEL "nothing has ever worked" STOP, added 2026-08-11 defaulting to 3. It satisfies
    # (a)+(b)+(c): (b) is the strongest form on this table — it does not add work, it TERMINATES the
    # run, so a resumed pre-versioning snapshot stops itself after three failed nodes under a policy
    # its operator never chose, and the run's first half would have kept going. (c) is 0, which is
    # both "off" and the value `tests/test_options_divergence.py::EXPECTED` already records as the
    # conservative library one.
    "systemic_failure_stop": 0,
    # WHICH FAILURES BUY AN IN-NODE REPAIR. A CHANGED default on a pre-existing field, so (a) cannot
    # apply and (b)+(c) carry it, exactly as for `inline_repair_attempts` above. It widened from the
    # mechanical three to ALL of `FAILURE_REASONS` on 2026-08-12 — eleven members at the time of
    # writing (REVIEW mega-review 2026-08-13: this line used to say "all eight"; `diverged`,
    # `stalled` and `needs_failed` were added the same week, and an auditor counting this map's
    # prose against the registry got a wrong count from the map itself) — and (b) is paid work: with
    # `inline_repair_attempts` restored to its historical 0 — which `_effective_repair_cap` reads as
    # the 50-attempt engine ceiling, not as "none" — a resumed node that fails `no_metric`/`setup`/
    # `drift`/`expect_failed`/`check_failed` can buy up to 50 Developer calls AND up to 50 full
    # re-evaluations that the first half of the same run bought ZERO of. On a GPU task where one eval
    # is the 76-minute training this range's own comments cite, that is hours nobody chose.
    # (c) is `("crash", "timeout", "oom")` — PLUS the two verdicts that are RE-CLASSIFICATIONS of
    # those, not new treatments. `triage._failure_reason` returns `diverged`/`stalled` from
    # short-circuits placed ABOVE the `exit_code != 0` branch that used to answer `oom`/`crash` for
    # the same watchdog SIGKILL, so pinning the bare historical tuple would REMOVE behaviour the
    # original run had: a resumed node that the old classifier called `oom` (and repaired) is now
    # called `diverged`, matches nothing, and terminalizes with ZERO repair attempts. This map
    # exists to stop re-entry changing a run's treatment in EITHER direction.
    # `needs_failed` is deliberately NOT here: the `needs` input contract did not exist before this
    # window, so no pre-field node could produce it and there is no historical treatment to preserve.
    "inline_repair_reasons": ("crash", "timeout", "oom", "diverged", "stalled"),
    # THE SOURCE-TREE READ FENCE, added 2026-08-13 defaulting to `deny`. (a) holds. (b) is the
    # strongest kind on this table after `systemic_failure_stop`: it is not extra work, it is an
    # INTERVENTION that can fail a node outright — a repo run whose training script legitimately
    # reads a large untracked in-tree input (the false positive `read_fence`'s own comment
    # documents) ran fine for nodes 0..N and would hard-fail in the child for every node after the
    # resume, with the operator having changed nothing, and each such failure then buys inline
    # Developer repairs the first half of the run never bought. (c) is `off`, pointable at every
    # commit before that date. Note the fence's `warn` rung is NOT the historical value: it writes
    # to stderr, which is the captured `RunResult.stderr` the repair loop reads.
    "read_fence": "off",
    # THE DEVELOPER'S PROBE, added 2026-08-13 defaulting to ON (F2, `tools/dev_probe.py`). It
    # satisfies (a)+(b)+(c), and (b) in TWO independent ways rather than one — which is why it is
    # here despite the probe having no domain event of its own.
    #   * a TOOL the run never had: `run_probe` joins the Developer's toolset at
    #     `adapters/repo_developer.py::_scout_tools`, the one point all four phases compose, so a
    #     resumed node's Developer can spend turns — and therefore money — on probe calls that the
    #     first half of the same run's event log contains zero of. Each probe also LAUNCHES A
    #     SUBPROCESS on the operator's box. It is sandboxed (no write anywhere, no exec, no GPU, its
    #     own always-`deny` read fence, a disposable tempdir) and that is exactly why it needs no
    #     event — but "no side effect" is not "no work", and this map is about work.
    #   * a DIFFERENT PROMPT: `_REPO_DEV_SYSTEM_BODY`'s "you CANNOT execute anything yourself"
    #     clause is spliced as one of two alternatives at the SAME position, so the two values
    #     produce two different Developer system prompts — measured 887 bytes apart, first
    #     divergence at offset 455. A prompt is a contract; resuming a run into the other one is
    #     changing what the agent was told mid-log.
    # (c) is `False`, pointable at every commit before 2026-08-13, and the field's own comment
    # already states that `developer_probe=false` restores the old prompt BYTE FOR BYTE — which is
    # what makes this row a restoration rather than a guess.
    "developer_probe": False,
    # THE REPAIR CRITIC'S CADENCE, added 2026-08-13 defaulting to 3 (F8). (a) holds. (b) is paid
    # work AND an intervention, the two strongest columns at once: from the 4th durable repair on a
    # node, `agents/unified_agent.py::repair_critic` is a SECOND model asked whether the chain is
    # circling, called `@in_llm_lane` on every subsequent repair — and it has the authority to
    # ABANDON the chain, so a resumed node can stop repairing where the first half of its own run
    # would have kept going. Note how it interacts with the `inline_repair_attempts: 0` row above:
    # where THAT row fires it restores the historical unlimited loop, and without this row the same
    # resume would put a paid judge on top of it that the run never had. The two are independent,
    # though — `runs/live_toy` spells `inline_repair_attempts: 1` explicitly, so that row is an inert
    # `setdefault` there while this one still fires, which is exactly why each carries its own value
    # rather than one being inferred from the other.
    # (c) is `0`, and it is pointable rather than guessed — the field's own comment spells it "no
    # critic (the loop stops on triage + the floors, exactly as it did before 2026-08-13)". It is
    # NOT the "magnitudes stay out" case: 0 here is a real OFF, not a disabled magnitude, which is
    # the same distinction that admits `inline_repair_attempts` and `deep_research_every`.
    "repair_critic_after": 0,
    # THE CADENCE PRECONDITION, added 2026-08-18 defaulting to True (backlog F1i). All three
    # conditions hold and (b) is the one that decides it. (a): the field did not exist before, so a
    # pre-field snapshot cannot have expressed a preference. (b): with it on, a resumed run's outer
    # loop reaches the Strategist consult and the concept classifier pass while an evaluation is
    # RUNNING, which is paid work at moments the original half of that run never bought any — and it
    # is an intervention too, since a `strategy_decision` re-wires policy/operators/fidelity/widths
    # for everything proposed after it. (c) is `False`, and it is POINTABLE rather than guessed: it
    # is the literal `if state.pending_nodes(): return False` every one of these consumers carried
    # from 2026-06-24 until this commit, so `false` reproduces the resumed run's own first half
    # exactly.
    # THIS ROW COSTS SOMETHING AND THE COST IS THE POINT. A run resumed from a snapshot written
    # before this field keeps the dead cadence — which on this box is v9's and the live e5small's
    # shape, i.e. exactly the runs the fix exists for. That is deliberate: this map's rule is that
    # re-entry must not silently add paid calls to an old run, and "the new behaviour is better" is
    # the argument it exists to refuse. An operator who wants the fix on a resumed run says so, by
    # setting `cadence_while_evaluating` explicitly (CLI/env/config) — which wins over the snapshot's
    # missing-field default. Every NEW run gets it without asking.
    "cadence_while_evaluating": False,
    # WHAT THIS MAP IS NOT. It is a hand-maintained list of FEATURE switches whose before-the-field
    # value is unambiguous, not a complete partition of `Settings`. Two classes stay out on purpose,
    # because for them a wrong entry is worse than a missing one — it would silently REMOVE behaviour
    # the original run really had:
    #  * knobs that are latent while their parent feature is off — `foresight_panel`,
    #    `foresight_verify_samples`, `memora_anchors`, `holdout_top_k`, `asha_eta`: the parent above
    #    already pins them out of play, and their own before-the-field value is not recoverable;
    #  * magnitudes and depths for behaviour that ALREADY existed — `debug_depth`,
    #    `context_budget_chars`, `developer_plan_max_steps`, `agent_emit_after`: guessing 0/off there
    #    disables working machinery rather than preserving history.
    #
    # AND ONE NAMED EXCLUSION, because it will otherwise be re-litigated every time the field moves:
    # `redact_output`, flipped False -> True on 2026-08-15, gets NO ROW. The case FOR one is real
    # and is the only one worth stating — a resumed pre-flip run would start masking mid-record, so
    # its own log would say two different things about the same kind of value in its two halves.
    # It loses on three independent grounds, any one of which is sufficient:
    #  (a) FAILS OUTRIGHT, and this is the decisive one. `redact_output` is a B3 field that predates
    #      `config.snapshot.json` itself (2026-06-23), so no snapshot format that has ever existed
    #      could omit it. Measured over all 46 preserved run directories on this box: 46 of 46
    #      carry the key explicitly (45 `false`, 1 `true`). `migrate_config_snapshot` is a
    #      `setdefault`, so the row would be a provable NO-OP on every run it was added to protect —
    #      a rule with no reachable case is worse than no rule, because a later reader takes its
    #      presence as evidence the question was live. Contrast `inline_repair_attempts`, the other
    #      CHANGED-default entry here: that one exists precisely FOR the pre-versioning snapshot
    #      that does not carry the key, and such a snapshot exists. For this field it cannot.
    #  (b) FAILS. No paid call, no intervention, no concurrency, no selection policy: this is a
    #      write-side transformation of a durable diagnostic STRING. It cannot fail a node, buy a
    #      repair, or move a champion. The precedent is `auto_extra_metrics`, whose own settings-doc
    #      row states the exemption in these words — write-side only, the fold never reads it, so it
    #      cannot change how an already-recorded run replays and it needs no row here.
    #  (c) IS NOT WELL-FORMED. `False` meant "no tail redaction AT ALL" before 2026-08-14 and means
    #      "no ENTROPY tail redaction" after, because the same change that made the flip cheap moved
    #      known credential SHAPES and the operator's own env VALUES to unconditional at every tail.
    #      So re-entry ALREADY changes the tail's redaction posture on all 46 preserved runs and no
    #      row was added for that — correctly. Pinning the entropy half alone would preserve half of
    #      a posture whose other half is deliberately unpinned, which is not a historical value.
    # The mid-record concern itself is also the smallest version of itself: the entropy pass changes
    # 0 of 1,652 persisted tails across the whole preserved corpus, the ~30 always-on
    # `redact_persisted_text` boundaries have carried this exact pass unconditionally the entire
    # time (so a redaction-posture change mid-corpus is that corpus's normal state, not a novelty),
    # and `***REDACTED***` is a self-describing marker — a reader can SEE that a mask happened,
    # which is not true of any behavioural change this table does protect.
    # A new field belongs here only when it (a) postdates 2026-06-23, (b) defaults to adding paid
    # calls / interventions / concurrency / a different selection policy, and (c) has a historical
    # value you can point at a commit for. When (c) fails, leave it out and say so here. A CHANGED
    # default on a pre-existing field belongs here on (b)+(c) alone — (a) cannot apply — and (c) is
    # never a guess for one, which is why `inline_repair_attempts` above does not contradict the
    # "magnitudes stay out" rule: that rule is about magnitudes whose historical value is unknown.
}


def migrate_config_snapshot(data: dict) -> dict:
    """Return an effective copy of one legacy/current snapshot without rewriting evidence bytes."""
    if not isinstance(data, dict):
        raise TypeError("config snapshot must be a JSON object")
    migrated = dict(data)
    for key, value in LEGACY_CONFIG_SNAPSHOT_DEFAULTS.items():
        migrated.setdefault(key, value)
    return migrated


_AGENT_STAGE_MAP_FIELDS = ("agent_stage_models", "agent_stage_base_urls")


def _filter_unknown_snapshot_agent_stages(data: dict) -> dict:
    """Filter obsolete stage keys/values from an effective resume copy, warning in stable order.

    Fresh inputs are strict, but a historical snapshot may contain a stage name a later binary no
    longer knows. Refusing that run would break re-entry; accepting the key silently would recreate
    the original typo drift.  The same compatibility rule applies to values accepted by the old
    unconstrained ``dict`` fields: blank values already meant "fall back", while non-string values
    had no valid provider meaning and could crash only when the stage was reached. Filter only this
    narrow nested vocabulary from a copy and leave the snapshot dict/file untouched as evidence.
    Non-mapping values remain errors at the Settings boundary rather than being repaired here.
    """
    effective = dict(data)
    for field in _AGENT_STAGE_MAP_FIELDS:
        raw = effective.get(field)
        if not isinstance(raw, Mapping):
            continue
        unknown = sorted(repr(key) for key in raw if key not in AGENT_STAGE_KEYS)
        known = {key: value for key, value in raw.items() if key in AGENT_STAGE_KEYS}
        invalid_values = sorted(
            repr(key) for key, value in known.items()
            if not isinstance(value, str) or not value.strip()
        )
        if unknown or invalid_values:
            effective[field] = {
                key: value for key, value in known.items()
                if isinstance(value, str) and value.strip()
            }
        if unknown:
            _LOG.warning(
                f"config snapshot {field} contains unknown agent stage key(s): "
                f"{', '.join(unknown)}; ignoring them for resume compatibility; "
                "snapshot evidence was not rewritten"
            )
        if invalid_values:
            _LOG.warning(
                f"config snapshot {field} contains non-string/blank override value(s) for stage "
                f"key(s): {', '.join(invalid_values)}; ignoring them for resume compatibility; "
                "snapshot evidence was not rewritten"
            )
    return effective


def config_snapshot_schema(data: dict) -> int:
    """The snapshot's declared format version. 0 = pre-versioning (the key did not exist yet).

    A malformed value is treated as UNREADABLE rather than as 0: a snapshot whose own version marker
    is corrupt cannot be assumed to be the oldest format."""
    raw = data.get(CONFIG_SNAPSHOT_SCHEMA_KEY, 0) if isinstance(data, dict) else 0
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ConfigSnapshotVersionError(
            f"config snapshot has a malformed {CONFIG_SNAPSHOT_SCHEMA_KEY}: {raw!r}")
    return raw


def settings_from_snapshot(data: dict) -> Settings:
    """Build re-entry Settings from a masked snapshot under its historical missing-field contract.

    Fails closed on a snapshot written by a NEWER binary. `Settings` is `extra="ignore"`, so such a
    snapshot would otherwise load with every unrecognized control silently dropped — the run would
    resume on the same event history under different paid/concurrency/selection semantics, with
    nothing to show why. Refusing is the only honest answer: this binary cannot know what it is
    dropping. Older and pre-versioning snapshots keep loading exactly as before, with one narrow
    compatibility exception: obsolete unified-agent stage keys and historically accepted
    non-string/blank override values are warned and removed from the effective copy while the
    snapshot evidence remains untouched."""
    found = config_snapshot_schema(data)
    if found > CONFIG_SNAPSHOT_SCHEMA:
        raise ConfigSnapshotVersionError(
            f"this run's config.snapshot.json declares format v{found}, but this build understands "
            f"at most v{CONFIG_SNAPSHOT_SCHEMA}. It was written by a newer LoopLab; resuming here "
            "would silently drop settings this build does not know. Upgrade LoopLab to resume it.")
    migrated = _filter_unknown_snapshot_agent_stages(migrate_config_snapshot(data))
    migrated.pop("llm_api_key", None)
    migrated.pop("llm_api_key_base_url", None)
    migrated.pop(CONFIG_SNAPSHOT_SCHEMA_KEY, None)   # a document marker, never a Settings field
    return Settings(**migrated)
