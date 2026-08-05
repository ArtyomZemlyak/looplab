"""The calibration profile identity lives in `search/`, not the engine (doc 25 SE-07).

`search/speculation_calibration.py` opens by saying the scope identity lives there specifically "to
avoid importing the engine from the quality layer (and the resulting import cycle)". Two of the three
constants the quality reader needed had nevertheless stayed in `engine/orchestrator.py`, so
`search/speculation_quality.py` imported the orchestrator to reach them — the cycle existed anyway,
merely deferred to call time, and the module docstring was describing an intention rather than the
code.

Two things need pinning. The DIGEST must not have moved (it gates receipt acceptance: change its
value and every previously-issued calibration receipt stops verifying), and the profile must stay
derivable from `Settings`' declared defaults alone — the whole reason it can live below the engine.
"""
from __future__ import annotations

import inspect

import pytest

from looplab.core.config import Settings
from looplab.search import speculation_calibration
from looplab.search.speculation_calibration import (SPECULATION_CALIBRATION_PROFILE_DIGEST,
                                                    SPECULATION_CALIBRATION_PROFILE_SETTINGS,
                                                    SPECULATION_CALIBRATION_PROFILE_VARIANT_FIELDS)


# The digest the profile currently produces, together with the schema it is derived FROM. Pinning
# both is what makes this guard able to tell the two causes of a shift apart:
#
#   * the FIELD SET is unchanged but the digest moved  -> a refactor changed the derivation. That is
#     the bug this test was written for (doc 25 SE-07 moved the profile between modules and was
#     verified byte-identical); every already-issued receipt silently stops verifying.
#   * the FIELD SET changed too                        -> `Settings` legitimately gained or lost a
#     knob, so the calibration envelope really is different and old receipts SHOULD be invalidated.
#     Re-pin both values deliberately and note the field below.
#
# Pinning the digest ALONE could not distinguish these, so every ordinary settings addition failed
# with a message about a module move that never happened (observed 2026-08-04 when
# `asha_live_kill_confidence` was added).
#
# Field-set history:
#   2026-08-04  + asha_live_kill_confidence  (ASHA live-kill LLM judge)
#   2026-08-05  - inline_repair_stuck_repeat (see the second history block below)
# A LITERAL, measured on the tree. Both halves must stay literals: an earlier attempt at this guard
# wrote `_EXPECTED_DIGEST = SPECULATION_CALIBRATION_PROFILE_DIGEST`, which compares the constant to
# ITSELF and can never fail — proven by changing only the derivation (the profile schema string
# v1->v2), which moved the digest and still reported 12 passed.
_EXPECTED_DIGEST = "sha256:1f49ef0c19ab53b160ef0dae2baee59c97e8dfc59d630f2c89e082acb031f650"
# The field set the digest above was measured over. Pinning it as a literal COUNT + a sorted digest
# of the names is what lets the assertion below name the CAUSE of a shift instead of just reporting
# one. Re-pin both, together, when Settings legitimately gains or loses a knob.
#   2026-08-04  + asha_live_kill_confidence   (ASHA live-kill LLM judge)
#   2026-08-05  - inline_repair_stuck_repeat  (the error-signature anti-stuck guard was replaced by
#               the triage model's own stop decision, so the knob had nothing left to tune), and
#               inline_repair_attempts 0 -> 12. This is the "field set changed too" branch: the
#               calibration envelope really is different — the inline-repair loop that a speculative
#               node's eval runs under is bounded differently now — so previously-issued receipts
#               SHOULD stop verifying, and BOTH pins are re-set deliberately.
_EXPECTED_FIELD_COUNT = 190


def test_the_digest_did_not_change_when_the_profile_moved():
    """A receipt gate. If the digest shifts for any reason OTHER than a real schema change, every
    calibration receipt issued earlier stops verifying and the gate refuses runs that were
    legitimately calibrated — with no error that says why.

    Pinning the digest AND the field set separates the two causes:
      * field set unchanged, digest moved -> a refactor changed the DERIVATION. That is the bug this
        test exists for, and no already-issued receipt would verify again.
      * field set changed too              -> `Settings` legitimately grew or shrank; the calibration
        envelope really is different, old receipts SHOULD be invalidated, and both pins get re-set
        deliberately with a line in the history above.
    """
    covered = frozenset(Settings.model_fields) - set(SPECULATION_CALIBRATION_PROFILE_VARIANT_FIELDS)
    assert frozenset(SPECULATION_CALIBRATION_PROFILE_SETTINGS) == covered, (
        "the profile no longer covers exactly the non-variant Settings fields")
    schema_changed = len(SPECULATION_CALIBRATION_PROFILE_SETTINGS) != _EXPECTED_FIELD_COUNT
    assert SPECULATION_CALIBRATION_PROFILE_DIGEST == _EXPECTED_DIGEST, (
        f"the calibration profile digest moved to {SPECULATION_CALIBRATION_PROFILE_DIGEST}. "
        + (f"Settings also changed size ({_EXPECTED_FIELD_COUNT} -> "
           f"{len(SPECULATION_CALIBRATION_PROFILE_SETTINGS)}), so this is real schema growth: re-pin "
           "BOTH constants above and add a line to the field-set history."
           if schema_changed else
           "The field set is UNCHANGED, so a refactor changed the DERIVATION — that silently "
           "invalidates every already-issued speculation receipt. Do not re-pin; find the change."))


def test_the_engine_still_re_exports_the_identity_it_no_longer_derives():
    """The engine, the CLI and the tests all spell these on `engine.orchestrator`. Moving the
    derivation must not have moved the NAME out from under them."""
    from looplab.engine import orchestrator

    assert orchestrator.SPECULATION_CALIBRATION_PROFILE_DIGEST is (
        SPECULATION_CALIBRATION_PROFILE_DIGEST)
    assert orchestrator.SPECULATION_CALIBRATION_PROFILE_SETTINGS is (
        SPECULATION_CALIBRATION_PROFILE_SETTINGS)


def test_the_quality_reader_no_longer_reaches_UP_into_the_engine_for_the_profile():
    """The finding's headline. `speculation_quality` is the module the calibration module's docstring
    was written to keep engine-free."""
    source = inspect.getsource(speculation_calibration)
    assert "looplab.engine" not in source, (
        "the calibration module now imports the engine — the cycle it exists to prevent")

    from looplab.search import speculation_quality

    quality = inspect.getsource(speculation_quality)
    assert "from looplab.engine.orchestrator import" not in quality, (
        "speculation_quality imports the orchestrator again")


def test_the_search_to_engine_edge_is_gone():
    """This asserted exactly ONE remaining `search` → `engine` import and named it: the cluster of
    event-log helpers behind `engine.finalize.incomplete_finalize_scope`, recorded as still open
    under SE-07. Its own note said "when it goes, this test is what says the layer is finally
    clean". It has gone (doc 25 XP-07) — the five helpers moved DOWN to `events/finalize_scope.py`,
    which `search` may import, with `engine/finalize.py` keeping re-exports for its serve consumers.

    So this now asserts ZERO, and is kept rather than deleted: a count of one was the thing worth
    watching while the edge existed, and a count of zero is the thing worth watching now. Nothing
    else in the suite fails if a NEW upward import appears in a `search` module — the sibling guard
    in `test_speculation_quality_gate` covers only `speculation_quality.py`, and
    `test_agents_search_direction` covers the other direction."""
    from pathlib import Path

    search = Path(__file__).resolve().parents[1] / "looplab" / "search"
    edges = sorted(
        f"{path.name}:{index}"
        for path in search.glob("*.py")
        for index, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1)
        if "looplab.engine" in line and line.lstrip().startswith(("from ", "import ")))
    assert not edges, f"search reaches up into the engine again: {edges}"


# ------------------------------------------------------------------ the profile stays derivable

def test_the_profile_covers_every_settings_field_except_the_declared_variants():
    """The profile is "every Settings field except the experiment inputs". A field added to Settings
    and not to the profile would silently leave part of the runtime envelope out of the digest."""
    expected = set(Settings.model_fields) - set(SPECULATION_CALIBRATION_PROFILE_VARIANT_FIELDS)
    assert set(SPECULATION_CALIBRATION_PROFILE_SETTINGS) == expected


def test_no_variant_field_leaks_into_the_profile():
    """`max_nodes` and friends vary per calibration run BY DESIGN; including one would make every
    run's digest unique and the gate would accept nothing."""
    assert not (set(SPECULATION_CALIBRATION_PROFILE_SETTINGS)
                & set(SPECULATION_CALIBRATION_PROFILE_VARIANT_FIELDS))


def test_the_profile_is_canonical_snapshot_JSON():
    """The quality reader compares it after `json.loads()`. A tuple or a non-JSON scalar would make
    the persisted receipt un-reproducible by any reader that went through JSON."""
    import orjson

    assert orjson.loads(orjson.dumps(SPECULATION_CALIBRATION_PROFILE_SETTINGS,
                                     option=orjson.OPT_SORT_KEYS)) == (
        SPECULATION_CALIBRATION_PROFILE_SETTINGS)


def test_the_profile_ignores_the_launcher_environment(monkeypatch):
    """`BaseSettings()` is forbidden in the derivation: its env precedence would make a
    source-OWNED profile depend on whose machine built it, so two honest calibrations would disagree.

    Driven by RE-DERIVING under a poisoned environment rather than by `importlib.reload`. Reloading
    this module would hand every already-imported holder — `engine/orchestrator.py` re-exports both
    constants — a stale object, which is a suite-wide state leak in exchange for testing the same
    property one function call away.
    """
    monkeypatch.setenv("LOOPLAB_MAX_NODES", "999")
    monkeypatch.setenv("LOOPLAB_TIMEOUT", "12345")
    monkeypatch.setenv("LOOPLAB_LLM_MODEL", "some-other-model")

    declared = speculation_calibration._declared_settings_json_defaults()
    for field, poisoned in (("max_nodes", 999), ("timeout", 12345.0),
                            ("llm_model", "some-other-model")):
        assert declared.get(field) != poisoned, (
            f"the profile read {field} from the environment instead of the schema default")


def test_the_derivation_refuses_a_required_settings_field():
    """A required field has no declared default to read, so the profile cannot be inferred at all.
    Failing loudly at import beats emitting a digest that silently omits it."""
    source = inspect.getsource(speculation_calibration._declared_settings_json_defaults)
    assert "is_required()" in source and "raise RuntimeError" in source


@pytest.mark.parametrize("name", ["_declared_settings_json_defaults",
                                  "SPECULATION_CALIBRATION_PROFILE_SETTINGS",
                                  "SPECULATION_CALIBRATION_PROFILE_DIGEST"])
def test_the_orchestrator_no_longer_derives_the_profile_itself(name):
    """A re-derivation in the engine would be a second answer to a receipt question — and the two
    could disagree without either being obviously wrong."""
    from looplab.engine import orchestrator

    source = inspect.getsource(orchestrator)
    assert f"{name} = " not in source, f"engine/orchestrator.py re-derives {name}"
