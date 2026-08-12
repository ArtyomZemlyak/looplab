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
from pathlib import Path

from looplab.core.config import Settings
from looplab.engine.options import EngineOptions

# {field: (settings_default, engine_default)} — every INTENDED divergence.
EXPECTED = {
    "agent_drives_actions": (True, False),
    "comparative_lessons": (True, False),
    "concurrent_research": (True, False),
    "concurrent_research_repeat": (True, False),
    "concurrent_consolidate": (True, False),
    "debug_depth": (2, 1),
    "deep_repair": (True, False),
    # Deep Research (owner decision 2026-08-07): the product surface ships `0`, which for THIS knob
    # alone means START IMMEDIATELY — the zero-width `cadence_due` window settled by
    # `engine/cadence.py::deep_research_window`, i.e. due at the first node and every node after.
    # The bare library ships `-1` = OFF, which is what `0` used to spell here, so a direct
    # `Engine(...)` still gains no cadence-driven paid think. The divergence therefore INVERTED its
    # numeric direction (3 > 0 became 0 > -1) while keeping the product-more-aggressive rule the
    # table exists to enforce; ints, so `test_no_inverted_divergence` cannot check it mechanically.
    # Why the product default moved at all: the cadence counts NODES while the feature is phrased
    # around TIME, and on `runs/rubert-dr-0804/0805/0807` (1.5-4 h per node, `deep_research_every=3`
    # and `concurrent_research=true` in every snapshot) it fired ZERO times — no `research_attempted`
    # and no `research_completed` row in any of the three, because a first think was 5-12 hours away.
    # Every run in the corpus where it DID fire has sub-second evals.
    "deep_research_every": (0, -1),
    "failure_reflection": (True, False),
    "lessons_every": (4, 0),
    "lessons_refresh_every": (4, 0),
    "merge_mode": ("auto", "mean"),
    "reflection_priors": (True, False),
    "report_every": (3, 0),
    "watchdog_reflection": (True, False),
    # Training-log monitor: ON in the product surface (advisory observer of the live training log; never
    # touches node selection or replay, no-ops without an LLM client / on the solution.py path), OFF in the
    # bare-library EngineOptions so a direct `Engine(...)` in a test does no unasked LLM work.
    "train_monitor": (True, False),
    "asha_live": (True, False),
    # The two watchdog KILLS became product defaults on 2026-08-04 (operator decision): the observers
    # are now allowed to act on what they already saw, so a diverged training / a hopeless curve stops
    # burning a multi-hour budget. They stay OFF in bare-library `EngineOptions`, because a direct
    # `Engine(...)` must never gain the power to terminate a caller's eval that a plain construction
    # did not ask for. Both remain narrow BY CONSTRUCTION, not by their default: the train monitor acts
    # only on a 'broken' verdict at confidence >= train_monitor_kill_confidence (a plateau is 'watch'
    # and is never killed), and ASHA acts only for stdout_json metrics declaring an explicit
    # `resource_key`, past the grace window, with `asha_live_min_siblings` finished same-resource peers.
    "train_monitor_kill": (True, False),
    "asha_live_kill": (True, False),
    "unified_agent": (True, False),
    # Layer 3 Card queue owns macro-action selection in the product surface (2026-08-04): the Card lane
    # is the intended selector, and it wins over `agent_drives_actions` when both are on. The bare
    # library keeps the legacy policy/unified-pilot action path, so a toy `Engine(...)` still runs the
    # historical spine. The value is pinned in `run_started`, so a resume cannot mix two treatments.
    "card_driven_selection": (True, False),
    # Speculative Card prefetch (2026-08-05): the product surface ships `-1` = AUTO, resolved once at
    # startup to the settled `eval_parallel` and pinned as the RESOLVED integer; the bare library keeps
    # `0` = off. Same reasoning as the two watchdog kills and `card_driven_selection` above — a direct
    # `Engine(...)` must not acquire a background LLM producer, a second leased role pair, or the
    # superseded/refund lifecycle merely by being constructed. Ints, so the direction rule below cannot
    # check this one mechanically either.
    "speculation_depth": (-1, 0),
    # Layer-2 parallelism (2026-08-04): the product surface ships startup AUTO (`0` = one experiment per
    # detected GPU, and an LLM/build width derived from that settled eval width); the bare library keeps
    # `None`, which falls back to the legacy `max_parallel`/`parallel_build` (both 1) and is therefore
    # byte-identical serial behaviour for a direct `Engine(...)`. Product-more-aggressive, same as every
    # bool row above — these are ints, so the direction rule below cannot check them mechanically.
    # `0` deliberately does NOT enable the finite shared LLM broker: that opt-in stays tied to an
    # explicitly-spelled POSITIVE canonical `llm_parallel` (tests/test_llm_broker.py).
    "eval_parallel": (0, None),
    "llm_parallel": (0, None),
    # Part IV/V machinery ships ON in the product surface (Settings) while bare-library
    # EngineOptions stays lean, so a toy `Engine(...)` does not fire concept/cross-run LLM work
    # unasked. The flags have heterogeneous effects — prompt steering, proposal admission,
    # read-only retrieval, and proposal-only governance — and some can add paid model work.
    # Product-on is an explicit experimental owner decision, not an inferred validation result.
    "concept_pivot": (True, False),
    "graded_novelty": (True, False),
    "cross_run_concepts": (True, False),
    "concept_run_base": (True, False),
    "cross_run_structured_claims": (True, False),
    "cross_run_curation": (True, False),
    "cross_run_advisory": (True, False),
    "cross_run_read_tools": (True, False),
    "fingerprint_universal": (True, False),
    # ADR-17 LLM-I/O capture: the product surface ships it ON (the UI's per-node trace is the whole
    # point of a run). The library side declares NOTHING (None) rather than False — a bare
    # `Engine(...)` must keep deferring to the process-wide `set_llm_capture` default, which is what
    # it did before the knob existed and what the tracing seam tests toggle.
    "trace_llm_io": (True, None),
    # The run-level "nothing has ever worked, stop the run" bound. The product ships it ON (3)
    # because an operator watching a UI must not have a run grind for 26 hours over the same
    # environment defect. The library declares 0 = OFF: a bare `Engine(...)` must not acquire a
    # NEW terminal it never had, and every embedding caller decides its own stopping policy.
    "systemic_failure_stop": (3, 0),
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
        elif isinstance(sv, bool):
            # A boolean product knob whose library side is NOT a bool may only be `None` = "declare
            # nothing, defer to the process-wide default" (trace_llm_io). Without this branch such a
            # pair slipped past the rule above entirely, so an inverted non-bool default would land
            # unchallenged — the exact silence this table exists to break.
            assert ov is None, (
                f"{f}: boolean product default with a non-bool, non-None library default "
                f"({ov!r}) — the direction rule cannot be checked, so this divergence is unreviewed.")


def test_part_iv_v_default_rationale_discloses_behavior_and_cost():
    source = (Path(__file__).parents[1] / "looplab" / "core" / "config.py").read_text(encoding="utf-8")
    # (This used to also require a `# CODEX AGENT:` review annotation somewhere in the file. That is
    # bookkeeping about an OPEN finding, not about the rationale this test is named for, and it goes
    # stale the moment the finding is fixed — as the snapshot-versioning one now is.)
    assert "explicit experimental product choice" in source
    assert "change graded-novelty admission" in source
    assert "paid LLM work" in source
    assert "after the frozen A/B cleared it" not in source
    assert "proposal-only, so safe to default on" not in source
    assert "it is audit/advisory/proposal-only" not in source


def test_curation_rationale_discloses_synchronous_finalize_latency():
    root = Path(__file__).parents[1]
    config = (root / "looplab" / "core" / "config.py").read_text(encoding="utf-8")
    finalize = (root / "looplab" / "engine" / "finalize.py").read_text(encoding="utf-8")
    assert "calls run synchronously during finalize" in config
    assert "calls run synchronously" in finalize
    assert "never blocks/" not in config
    assert "never blocks finalization" not in finalize


def test_part_iv_comments_distinguish_fold_storage_from_live_steering():
    root = Path(__file__).parents[1]
    engine = root / "looplab" / "engine"
    # The concept cadence left `engine/strategy.py` in doc 25 EC-09, and these comments went with it.
    # Both files are read, and the NEGATIVE pins cover both: the retracted claim ("audit-only, so the
    # flag being off by default is harmless") is about the concept pivot, so it would come back in
    # `concept_cadence.py` — but the split is recent enough that a revert could land it in either.
    cadence = (engine / "concept_cadence.py").read_text(encoding="utf-8")
    strategy = (engine / "strategy.py").read_text(encoding="utf-8")
    # Both POSITIVE pins live on `tag_text_llm`, which left `search/concept_graph.py` for
    # `search/concept_tagging.py` in doc 25 SE-09. The NEGATIVE pins keep reading BOTH files for the
    # same reason the cadence pair above does: the split is recent, so a revert of the retracted
    # claims could land in either module.
    tagging = (root / "looplab" / "search" / "concept_tagging.py").read_text(encoding="utf-8")
    graph = (root / "looplab" / "search" / "concept_graph.py").read_text(encoding="utf-8")
    assert "product `Settings` is ON" in cadence
    assert "can steer later proposals" in cadence
    assert "change admission" in tagging
    assert "configured live-client call is synchronous" in tagging
    assert "`_concept_pivot` being OFF by default" not in cadence
    assert "`_concept_pivot` being OFF by default" not in strategy
    assert "off-by-default + audit-only" not in graph + tagging
    assert "Never raises, never blocks the caller" not in graph + tagging
