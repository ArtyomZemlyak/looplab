"""Settings config schema: lower-bound validation and run-only settings round-tripping through the
masked snapshot on resume.

Regressions from the code-review rounds (round 5 config lower bounds; audit C1 resume settings)."""
from __future__ import annotations

import json

import pytest

from looplab.core.config import (
    LEGACY_CONFIG_SNAPSHOT_DEFAULTS,
    Settings,
    canonicalize_parallelism_source,
    migrate_config_snapshot,
    settings_from_snapshot,
)


def test_config_lower_bounds():
    from pydantic import ValidationError
    for bad in ({"max_nodes": 0}, {"n_seeds": 0}, {"timeout": 0}, {"max_parallel": -1}):
        with pytest.raises(ValidationError):
            Settings(**bad)
    assert Settings().max_parallel == 1        # default still valid
    assert Settings(max_parallel=0).max_parallel == 0   # 0 is now VALID = AUTO (engine resolves to GPU count)


def test_config_upper_bounds_reject_resource_exhaustion():
    """The UI /start path writes these knobs straight into the engine env, so an unbounded value
    (e.g. max_parallel=100000 from a crafted preflight) would fan out that many sandboxes. Reject at
    the config boundary. Ceilings are generous — realistic runs top out far below."""
    from pydantic import ValidationError
    for bad in ({"max_parallel": 100000}, {"max_parallel": 1025}, {"n_seeds": 2000},
                {"max_nodes": 10**7}):
        with pytest.raises(ValidationError):
            Settings(**bad)
    # Generous ceilings still admit any real run.
    assert Settings(max_parallel=1024, n_seeds=1024, max_nodes=1_000_000).max_parallel == 1024


def test_nonfinite_time_budgets_are_rejected_at_model_boundary():
    from pydantic import ValidationError
    from looplab.core.models import Idea

    for bad in (float("inf"), float("-inf"), float("nan")):
        # Settings are operator config, validated ONCE at the boundary and never reconstructed from an
        # event log — so a non-finite time budget must fail LOUD.
        with pytest.raises(ValidationError):
            Settings(timeout=bad)
        with pytest.raises(ValidationError):
            Settings(max_eval_seconds=bad)
        with pytest.raises(ValidationError):
            Settings(max_eval_timeout=bad)
        # `Idea.eval_timeout`, by contrast, is LLM-authored data that the fold reconstructs via
        # `Idea(**d["idea"])` on EVERY replay. Rejecting there would raise inside the fold and silently
        # DROP the node from an old log (invariant-5 back-compat break, F1). It must fail SAFE instead:
        # coerce a non-finite/non-positive budget to None (== "use the run default"), the same meaning
        # the consumer already honors (`if etv and etv > 0`). Safety intent preserved (no infinite eval
        # timeout reaches the sandbox), replay preserved.
        assert Idea(operator="draft", eval_timeout=bad).eval_timeout is None


def test_max_eval_timeout_is_a_positive_finite_operator_ceiling():
    from pydantic import ValidationError

    assert Settings().max_eval_timeout == 3600.0
    assert Settings(max_eval_timeout=24 * 3600).max_eval_timeout == 24 * 3600
    for bad in (0, -1, 24 * 3600 + 1):
        with pytest.raises(ValidationError):
            Settings(max_eval_timeout=bad)


# C1 — resume reconstructs run-only settings from the snapshot
def test_settings_roundtrip_through_snapshot():
    s = Settings()
    s.require_approval, s.trust_mode, s.confirm_seeds = True, "untrusted", 4
    snap = s.masked_snapshot()
    # The persisted/runtime-scope snapshot is strict JSON-domain, not a Python-mode dump.  Tuple
    # schema fields must already be arrays before json.dumps or digest canonicalization sees them.
    assert snap["inline_repair_reasons"] == ["crash", "timeout", "oom"]
    assert isinstance(snap["inline_repair_reasons"], list)
    json.dumps(snap, allow_nan=False)
    snap.pop("llm_api_key", None)
    s2 = Settings(**snap)
    assert s2.require_approval is True and s2.trust_mode == "untrusted" and s2.confirm_seeds == 4


def test_legacy_snapshot_migration_is_copy_only_and_preserves_historical_effects(monkeypatch):
    raw = {"max_parallel": 4, "backend": "toy", "llm_api_key": "***"}
    original = dict(raw)
    migrated = migrate_config_snapshot(raw)

    assert raw == original
    assert migrated is not raw
    for key, value in LEGACY_CONFIG_SNAPSHOT_DEFAULTS.items():
        assert migrated[key] == value

    # Init kwargs from the migrated snapshot outrank ambient env and keep canonical None meaningful;
    # legacy parallel_build must not accidentally enable the shared LLM broker.
    monkeypatch.setenv("LOOPLAB_EVAL_PARALLEL", "7")
    monkeypatch.setenv("LOOPLAB_LLM_PARALLEL", "5")
    settings = settings_from_snapshot(raw)
    for key, value in LEGACY_CONFIG_SNAPSHOT_DEFAULTS.items():
        assert getattr(settings, key) == value
    assert settings.max_parallel == 4
    assert settings.eval_parallel is None and settings.llm_parallel is None


def test_canonicalize_parallelism_source_keeps_broker_optin_off_by_default():
    """`max_parallel`/`eval_parallel` are true aliases and always promote. `parallel_build`/`llm_parallel`
    are NOT — a positive `llm_parallel` also flips on the shared broker — so a config/startup load must
    NOT promote a legacy-only `parallel_build`; only the Strategist opt-in does."""
    # True alias: always promoted (no broker semantics).
    assert canonicalize_parallelism_source({"max_parallel": 5}) == {
        "max_parallel": 5, "eval_parallel": 5}
    # Legacy build width: NOT promoted at config/startup load (broker stays opt-in), and NOT masked
    # here either. The layer canonicalizer sees ONE layer, so it cannot know whether some other layer
    # in the tier set the canonical key; the precedence mask therefore lives in
    # `flatten_parallelism_layers`, which knows the order (see the test below). Stamping it here made
    # every legacy-only layer emit `llm_parallel: None`, and because pydantic-settings ranks init
    # kwargs above env, that sentinel silently overrode an exported `LOOPLAB_LLM_PARALLEL`.
    assert canonicalize_parallelism_source({"parallel_build": 3}) == {"parallel_build": 3}
    # Strategist opt-in DOES promote parallel_build -> llm_parallel (deliberate full alias).
    assert canonicalize_parallelism_source(
        {"parallel_build": 3}, promote_build_to_llm_parallel=True) == {
            "parallel_build": 3, "llm_parallel": 3}
    # An explicit canonical llm_parallel is always preserved verbatim.
    assert canonicalize_parallelism_source({"llm_parallel": 4}) == {"llm_parallel": 4}


def test_a_higher_priority_legacy_parallel_build_is_not_shadowed_by_a_file_llm_parallel():
    """CLI > file precedence must survive the alias split (the guarantee build_settings documents)."""
    from looplab.core.appconfig import build_settings

    settings = build_settings({"llm_parallel": 2}, {}, {"parallel_build": 8})
    assert settings.parallel_build == 8
    assert settings.llm_parallel is None            # legacy mode: no shared total...
    width = settings.llm_parallel if settings.llm_parallel is not None else settings.parallel_build
    assert width == 8                               # ...and the CLI's width is what actually runs


def test_env_legacy_parallel_build_does_not_enable_the_shared_llm_broker(monkeypatch):
    """The exact bug surface: `LOOPLAB_PARALLEL_BUILD` alone must set the legacy build width WITHOUT
    promoting to canonical `llm_parallel` (which the orchestrator reads as the broker opt-in switch)."""
    monkeypatch.setenv("LOOPLAB_PARALLEL_BUILD", "3")
    settings = Settings()
    assert settings.parallel_build == 3
    assert settings.llm_parallel is None          # NOT promoted -> orchestrator leaves the broker off

    # An explicit canonical llm_parallel still loads (and is the broker opt-in).
    monkeypatch.setenv("LOOPLAB_LLM_PARALLEL", "5")
    assert Settings().llm_parallel == 5

    # The true-alias max_parallel -> eval_parallel promotion is unaffected (no broker semantics).
    monkeypatch.delenv("LOOPLAB_LLM_PARALLEL", raising=False)
    monkeypatch.setenv("LOOPLAB_MAX_PARALLEL", "6")
    assert Settings().eval_parallel == 6


def test_snapshot_migration_never_overrides_explicit_modern_values():
    explicit = {
        "parallel_build": 3,
        "eval_parallel": 2,
        "llm_parallel": 4,
        "train_monitor": True,
        "asha_live": True,
        "max_eval_timeout": 7200.0,
        "watchdog_reflection": True,
        "card_driven_selection": True,
        "speculation_depth": 2,
        "speculation_gate_receipt": "receipt.json",
        "concurrent_research_repeat": True,
        "concurrent_research_max_calls": 9,
        "concurrent_consolidate": True,
    }
    settings = settings_from_snapshot(explicit)
    for key, value in explicit.items():
        assert getattr(settings, key) == value


def test_novelty_mode_rejected_when_out_of_set():
    """Architecture review: an out-of-set novelty_mode (a mis-cased env value, or 'on') used to fall
    through as a silent NO-OP that disabled the gate. It must fail loudly at config time like
    trust_gate/merge_mode."""
    from pydantic import ValidationError
    for good in ("off", "algo", "llm"):
        assert Settings(novelty_mode=good).novelty_mode == good
    for bad in ("LLM", "on", "algorithm", ""):
        with pytest.raises(ValidationError):
            Settings(novelty_mode=bad)


def test_remaining_enum_fields_rejected_when_out_of_set():
    """arch-review §5 P3: strategist_backend / eval_trust_mode / seed_mode were accepted with any
    string at construction and only fail-safe/later-loud downstream. Fail loudly at config time."""
    from pydantic import ValidationError
    assert Settings().strategist_backend and Settings().eval_trust_mode and Settings().seed_mode  # defaults valid
    for field, good, bad in (
        ("strategist_backend", "rule", "Agent"),
        ("eval_trust_mode", "autonomous", "freeze"),
        ("seed_mode", "tracked", "full")):
        assert getattr(Settings(**{field: good}), field) == good
        with pytest.raises(ValidationError):
            Settings(**{field: bad})


def test_a_boolean_is_a_type_error_for_a_numeric_setting():
    """`{"max_nodes": true}` must be a 422, not a budget of ONE node.

    Pydantic validates Settings in lax mode and `isinstance(True, int)` is True, so a UI/API type slip
    used to validate cleanly and silently collapse the run budget (or the eval width) to 1. The run
    then looked configured and did almost nothing, with nothing in the snapshot to show why. The guard
    covers EVERY numeric field, including the many with no `Field(ge=...)` bound of their own.
    """
    import pytest as _pytest
    from looplab.core.config import Settings

    for field in ("max_nodes", "n_seeds", "eval_parallel", "timeout", "sweep_timeout_mult"):
        with _pytest.raises(Exception) as exc:
            Settings(**{field: True})
        assert "must be a number, not a boolean" in str(exc.value), field

    Settings(max_nodes=5, n_seeds=2)                      # real numbers still validate
    assert Settings(cross_run_curation=False).cross_run_curation is False   # bool fields untouched


def test_an_unknown_backend_is_rejected_instead_of_downgrading_to_toy():
    """A `backend` typo must fail loudly, not silently run offline toy roles.

    Every consumer tests `settings.backend == "llm"` exactly (cli/__init__.py, adapters/tasks.py), so
    an untyped `--set`/file/env value — including a mis-cased "LLM" — fell through to the OFFLINE toy
    optimizer. The user got a complete run that never called the model and no diagnostic anywhere.
    Same fail-loud contract the neighbouring enum-ish fields already have.
    """
    import pytest as _pytest
    from looplab.core.config import Settings

    for bad in ("typo", "LLM", "openai", ""):
        with _pytest.raises(Exception) as exc:
            Settings(backend=bad)
        assert "backend must be toy|llm" in str(exc.value), bad
    assert Settings(backend="llm").backend == "llm" and Settings().backend == "toy"
# --------------------------------------------------------------------------- snapshot format version
def test_a_snapshot_from_a_newer_build_is_refused_rather_than_silently_downgraded():
    """`Settings` is extra="ignore", so a snapshot written by a newer LoopLab used to load with every
    unrecognized control dropped — the run resumed on the same event history under different paid,
    concurrency and selection semantics, with nothing to show why. This build cannot know what it
    would be dropping, so refusing is the only honest answer."""
    from looplab.core.config import (CONFIG_SNAPSHOT_SCHEMA, CONFIG_SNAPSHOT_SCHEMA_KEY,
                                     ConfigSnapshotVersionError, Settings, settings_from_snapshot)
    snap = Settings(llm_model="pinned", max_nodes=7).masked_snapshot()
    assert snap[CONFIG_SNAPSHOT_SCHEMA_KEY] == CONFIG_SNAPSHOT_SCHEMA
    restored = settings_from_snapshot(snap)
    assert restored.llm_model == "pinned" and restored.max_nodes == 7

    with pytest.raises(ConfigSnapshotVersionError, match="newer LoopLab"):
        settings_from_snapshot({**snap, CONFIG_SNAPSHOT_SCHEMA_KEY: CONFIG_SNAPSHOT_SCHEMA + 1})
    # A corrupt marker is unreadable, never "assume the oldest format".
    for bad in ("1", -1, True, 1.5):
        with pytest.raises(ConfigSnapshotVersionError, match="malformed"):
            settings_from_snapshot({**snap, CONFIG_SNAPSHOT_SCHEMA_KEY: bad})


def test_a_pre_versioning_snapshot_still_resumes_unchanged():
    """Absence means a snapshot written before the marker existed; that historical contract — legacy
    defaults, not today's product defaults — must be untouched."""
    from looplab.core.config import (CONFIG_SNAPSHOT_SCHEMA_KEY, LEGACY_CONFIG_SNAPSHOT_DEFAULTS,
                                     Settings, settings_from_snapshot)
    snap = Settings(llm_model="old-run").masked_snapshot()
    legacy = {k: v for k, v in snap.items() if k != CONFIG_SNAPSHOT_SCHEMA_KEY}
    for field in LEGACY_CONFIG_SNAPSHOT_DEFAULTS:
        legacy.pop(field, None)
    restored = settings_from_snapshot(legacy)
    assert restored.llm_model == "old-run"
    for field, historical in LEGACY_CONFIG_SNAPSHOT_DEFAULTS.items():
        assert getattr(restored, field) == historical, field


def test_the_document_marker_does_not_move_the_speculation_runtime_scope():
    """The marker describes the FILE, not the run. Letting it into the digest would invalidate every
    calibration receipt earned before it existed."""
    from looplab.core.config import CONFIG_SNAPSHOT_SCHEMA_KEY, Settings
    from looplab.search.speculation_calibration import speculation_runtime_scope_digest
    snap = Settings(max_nodes=8).masked_snapshot()
    without = {k: v for k, v in snap.items() if k != CONFIG_SNAPSHOT_SCHEMA_KEY}
    assert speculation_runtime_scope_digest(snap) == speculation_runtime_scope_digest(without)


def test_legacy_parallel_build_layer_does_not_mask_an_env_llm_parallel(monkeypatch):
    """A tier that never spelled `llm_parallel` has not SET it — env must still supply it.

    Precedence is per FIELD (CLI > file > env > defaults). The mask that makes a higher layer's
    legacy width beat a LOWER layer's canonical one used to be stamped per layer, so any layer
    naming `parallel_build` emitted `llm_parallel: None` — and since pydantic-settings ranks init
    kwargs above env, that sentinel silently discarded a `LOOPLAB_LLM_PARALLEL` the operator had
    exported as a deliberate provider rate/cost ceiling, switching the shared broker off.
    """
    from looplab.core.appconfig import build_settings
    from looplab.core.config import flatten_parallelism_layers

    monkeypatch.setenv("LOOPLAB_LLM_PARALLEL", "4")
    assert build_settings({}, {}, {}).llm_parallel == 4          # baseline: env is honoured
    assert build_settings({"parallel_build": 2}, {}, {}).llm_parallel == 4, (
        "a legacy-only layer masked the operator's env-set canonical broker budget")
    monkeypatch.delenv("LOOPLAB_LLM_PARALLEL")

    # The inversion the mask exists for is unchanged: a HIGHER layer's legacy width still beats a
    # LOWER layer's canonical value, because that tier really did set both keys.
    assert build_settings({"llm_parallel": 2}, {}, {"parallel_build": 8}).llm_parallel is None
    assert build_settings({"llm_parallel": 2}, {}, {}).llm_parallel == 2

    # Same rule at the helper level, lowest-priority layer first.
    assert flatten_parallelism_layers([{"llm_parallel": 2}, {"parallel_build": 8}]) == {
        "llm_parallel": None, "parallel_build": 8}
    assert flatten_parallelism_layers([{"parallel_build": 8}, {"llm_parallel": 2}]) == {
        "parallel_build": 8, "llm_parallel": 2}
    assert "llm_parallel" not in flatten_parallelism_layers([{"parallel_build": 8}])


def test_legacy_snapshot_resume_does_not_switch_on_paid_work_the_run_never_did():
    """`LEGACY_CONFIG_SNAPSHOT_DEFAULTS` reaches back past the week the map was written.

    The map's stated rule is that re-entry "must not silently add paid calls, interventions,
    concurrency or a different selection policy to an old run", and absence of a key in a
    pre-versioned snapshot is the per-field version marker. But the map only listed flags added
    2026-07-19..07-21, while foresight/concept_pivot/graded_novelty/the cross_run_* bundle/
    memora_llm/comparative_lessons/the lesson+research+report cadences were added 2026-06-24..07-17 —
    after `config.snapshot.json` existed (2026-06-23) and before the map did. A snapshot from that
    window therefore resumed with today's paid product defaults ACTIVE: predict-before-execute and
    cross-run LLM calls the original run never made, and a different novelty admission policy.

    This pins the fields the map DOES claim; it is not a completeness assertion (see the map's own
    "WHAT THIS MAP IS NOT" note — latent knobs and magnitudes stay out by design).
    """
    from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, settings_from_snapshot

    era_snapshot = {"max_nodes": 8, "direction": "min", "backend": "toy"}
    resumed = settings_from_snapshot(era_snapshot)
    for field in ("foresight", "foresight_agentic", "foresight_verify", "concept_pivot",
                  "graded_novelty", "cross_run_concepts", "cross_run_advisory",
                  "cross_run_structured_claims", "cross_run_curation", "cross_run_read_tools",
                  "concept_run_base", "fingerprint_universal", "memora_llm", "comparative_lessons",
                  "unified_agent", "failure_reflection", "reflection_priors",
                  "agent_drives_actions", "concurrent_research", "deep_repair"):
        assert getattr(resumed, field) is False, f"{field} switched on for a legacy run"
    for field in ("lessons_every", "lessons_refresh_every", "deep_research_every", "report_every"):
        assert getattr(resumed, field) == 0, f"{field} cadence switched on for a legacy run"
    # ...while today's product defaults are the opposite, which is what made this a live gap.
    live = Settings()
    assert live.foresight is True and live.concept_pivot is True and live.lessons_every == 4

    # A snapshot that DOES carry the keys is untouched — `setdefault`, never an override.
    modern = Settings(foresight=True, concept_pivot=True, lessons_every=4).masked_snapshot()
    kept = settings_from_snapshot(modern)
    assert kept.foresight is True and kept.concept_pivot is True and kept.lessons_every == 4

    # Deliberate omissions: guessing these would REMOVE behaviour an old run really had.
    assert "debug_depth" not in LEGACY_CONFIG_SNAPSHOT_DEFAULTS
    assert "foresight_panel" not in LEGACY_CONFIG_SNAPSHOT_DEFAULTS


def test_foresight_min_confidence_is_bounded_like_its_siblings():
    """The field is a 0..1 confidence, but was declared without a bound while every sibling knob
    (train_monitor_kill_confidence, asha_live_quantile) carries one. A typo'd 5.0 validated silently
    into a gate no score can ever clear, so every foresight pick abstained forever — with nothing in
    the config snapshot to say why."""
    from looplab.core.config import Settings

    assert Settings(foresight_min_confidence=0.0).foresight_min_confidence == 0.0
    assert Settings(foresight_min_confidence=1.0).foresight_min_confidence == 1.0
    for bad in (5.0, -0.1, 1.5):
        with pytest.raises(Exception):
            Settings(foresight_min_confidence=bad)
