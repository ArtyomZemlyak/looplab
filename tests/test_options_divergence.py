"""The deliberate Settings-vs-EngineOptions default divergences, FROZEN (docs/15 §P4.4).

The two default sets encode a real split — `Settings` is the opinionated product surface,
`EngineOptions` the conservative library default (options.py's own docstring) — but only the
Engine-side relationship was test-locked (test_engine_options). Nothing asserted the intended
GAP, so changing a `Settings` default silently shifted it. This table makes every divergence a
deliberate, reviewed edit: add/remove/change a default on either side and the diff below goes
red until the table (and the rationale you owe the reviewer) is updated.

Direction rule the table also documents: the product side is always the MORE aggressive one
(features on, cadences enabled). `novelty_semantic` used to invert that rule — a direct
`Engine(novelty_gate=True)` got embedding dedup the identical product config disabled — and was
realigned in the same change that added this test.
"""
from __future__ import annotations

import dataclasses

from looplab.core.config import Settings
from looplab.engine.options import EngineOptions

# {field: (settings_default, engine_default)} — every INTENDED divergence.
EXPECTED = {
    "agent_drives_actions": (True, False),
    "comparative_lessons": (True, False),
    "concurrent_research": (True, False),
    "debug_depth": (2, 1),
    "deep_repair": (True, False),
    "deep_research_every": (3, 0),
    "failure_reflection": (True, False),
    "lessons_every": (4, 0),
    "lessons_refresh_every": (4, 0),
    "merge_mode": ("auto", "mean"),
    "reflection_priors": (True, False),
    "report_every": (3, 0),
    "unified_agent": (True, False),
    # Part IV/V machinery now ships ON in the product surface (Settings) while the bare-library
    # EngineOptions stays lean, so a toy `Engine(...)` in a test doesn't fire concept/cross-run
    # LLM work unasked. All are audit/advisory-or-proposal (never reject/select), so the product
    # side being the aggressive one keeps the direction rule. Enabled per the owner's decision to
    # run the built Part IV/V features by default on real tasks.
    "concept_pivot": (True, False),
    "graded_novelty": (True, False),
    "cross_run_concepts": (True, False),
    "cross_run_structured_claims": (True, False),
    "cross_run_curation": (True, False),
    "cross_run_advisory": (True, False),
    "cross_run_read_tools": (True, False),
    "fingerprint_universal": (True, False),
}
# Divergent by SHAPE, not a scalar worth freezing: the product default is a non-trivial
# structure; the library default is "off". Assert the shape relationship, not the payload.
STRUCTURAL = {
    "agent_control": lambda sv, ov: isinstance(sv, dict) and sv and ov is None,
    # value is env-dependent (conftest points LOOPLAB_MEMORY_DIR at a tmp dir; the product
    # default is ~/.looplab/memory) — assert the shape: product ON, library OFF.
    "memory_dir": lambda sv, ov: isinstance(sv, str) and sv and ov is None,
}


def _divergences() -> dict:
    s, o = Settings(), EngineOptions()
    out = {}
    for f in (fld.name for fld in dataclasses.fields(EngineOptions)):
        if hasattr(s, f) and getattr(s, f) != getattr(o, f):
            out[f] = (getattr(s, f), getattr(o, f))
    return out


def test_divergence_set_is_exactly_the_frozen_table():
    actual = _divergences()
    assert set(actual) == set(EXPECTED) | set(STRUCTURAL), (
        f"Settings-vs-EngineOptions divergence set changed.\n"
        f"  unexpected: {sorted(set(actual) - set(EXPECTED) - set(STRUCTURAL))}\n"
        f"  vanished:   {sorted((set(EXPECTED) | set(STRUCTURAL)) - set(actual))}\n"
        "A default changed on one side — if intended, update EXPECTED/STRUCTURAL here WITH the "
        "rationale; if not, you just found the silent drift this table exists to catch.")
    for f, pair in EXPECTED.items():
        assert actual[f] == pair, f"{f}: divergence changed {pair} -> {actual[f]}"
    for f, check in STRUCTURAL.items():
        assert check(*actual[f]), f"{f}: structural divergence shape changed: {actual[f]!r}"


def test_no_inverted_divergence():
    # The direction rule: for boolean knobs the PRODUCT side is the aggressive (True) one.
    for f, (sv, ov) in EXPECTED.items():
        if isinstance(sv, bool) and isinstance(ov, bool):
            assert sv and not ov, (
                f"{f}: divergence inverted (Settings={sv}, Engine={ov}) — the library default "
                "must not be MORE aggressive than the product default (the novelty_semantic bug).")
