"""Settings config schema: lower-bound validation and run-only settings round-tripping through the
masked snapshot on resume.

Regressions from the code-review rounds (round 5 config lower bounds; audit C1 resume settings)."""
from __future__ import annotations

import json

import pytest

from looplab.core.config import (
    LEGACY_CONFIG_SNAPSHOT_DEFAULTS,
    Settings,
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
