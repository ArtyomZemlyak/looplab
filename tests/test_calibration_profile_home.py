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


# The value measured on the pre-move tree. It is not a "current output" snapshot — it is the receipt
# identity that already-issued calibration evidence was signed against.
_DIGEST_BEFORE_THE_MOVE = (
    "sha256:5515fda7a9a526b945b3f032f1a1669ffb850a6a5e42d99c7916adc370772d6a")


def test_the_digest_did_not_change_when_the_profile_moved():
    """A receipt gate. If the digest shifts, every calibration receipt issued before the move stops
    verifying and the gate refuses runs that were legitimately calibrated — with no error that says
    why. The move was verified byte-identical; this keeps it that way."""
    assert SPECULATION_CALIBRATION_PROFILE_DIGEST == _DIGEST_BEFORE_THE_MOVE


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


def test_the_search_to_engine_edge_is_down_to_the_one_named_exception():
    """Kept explicit rather than assumed: the last `search` → `engine` import is
    `engine.finalize.incomplete_finalize_scope`, which is a different move (a cluster of event-log
    helpers with five serve consumers) and is recorded as still open under SE-07. When it goes, this
    test is what says the layer is finally clean."""
    from pathlib import Path

    search = Path(__file__).resolve().parents[1] / "looplab" / "search"
    edges = sorted(
        f"{path.name}:{index}"
        for path in search.glob("*.py")
        for index, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1)
        if "looplab.engine" in line and line.lstrip().startswith(("from ", "import ")))
    assert len(edges) == 1, f"search -> engine imports changed: {edges}"
    assert edges[0].startswith("speculation_quality.py:")


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
