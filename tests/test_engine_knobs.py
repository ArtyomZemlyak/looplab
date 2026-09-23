"""Every pure-config knob is DECLARED once, in `engine/knobs.py::EngineKnobs` (review 2026-09-22,
ENG1-03 step 4) — and the declaration is held to what `Engine.__init__` actually lands.

`EngineKnobs` states, per engine attribute, the ONE `EngineOptions` field it is a function of and the
settle rule, as a non-data descriptor: an instance value always wins, and an object of the family that
never ran `Engine.__init__` reads the value a real bare Engine settles to. What this file proves:

  1. the declaration IS what `__init__` lands — driven over the bare and product option sets, every
     field moved alone and every falsy spelling the constructor accepts. While `__init__` still
     spelled each assignment itself (step 4b) this proved the table's transcription; since step 4c
     lands them through `settle_knobs`, it proves that nothing later in `__init__` re-lands a
     declared knob with a different rule — the second copy this module exists to prevent;
  2. the declared set and `EXPLICIT_IN_INIT` together are EXACTLY the attributes the fields land on,
     as `tests/test_engine_options.py::attr_by_field` derives them by driving — so a new knob is a
     `Knob` or an explicit attribute with its reason, and the two maps cannot disagree on a name;
  3. an `Engine.__new__(Engine)` stub reads every declared knob as a real bare Engine settles it —
     the "79 stubs run knob sets no real Engine has" half of the finding;
  4. a knob is still a plain attribute to everything that writes one, and no knob hides a reader
     registered as deliberately "not knowable" (`engine/attribute_sites.py::UNSETTLED_KNOB_DEFAULTS`).
"""
from __future__ import annotations

import dataclasses
import typing

import pytest

from looplab.core.config import Settings
from looplab.engine.attribute_sites import UNSETTLED_KNOB_DEFAULTS
from looplab.engine.knobs import EXPLICIT_IN_INIT, KNOBS, LIBRARY_DEFAULTS, EngineKnobs, Knob
from looplab.engine.options import EngineOptions
from looplab.engine.orchestrator import Engine
from tests.test_engine_knob_defaults import agrees
from tests.test_engine_options import (_NOT_ON_THE_ENGINE, _PERTURBED_BESIDE, _mk_engine,
                                       _perturbed, attr_by_field)


def _option_sets():
    """(label, Engine kwargs): bare, product, each field moved alone, each falsy spelling."""
    hints = typing.get_type_hints(EngineOptions)
    yield "bare", {}
    yield "product", {"options": EngineOptions.from_settings(Settings())}
    for f in dataclasses.fields(EngineOptions):
        if f.name in _NOT_ON_THE_ENGINE:
            continue
        yield (f"moved:{f.name}",
               {**_PERTURBED_BESIDE.get(f.name, {}), f.name: _perturbed(f, hints[f.name])})
        # The falsy spellings reach every `or` fallback and every clamp's lower edge.
        for falsy in (None, 0, "", False):
            yield f"falsy:{f.name}={falsy!r}", {f.name: falsy}


def test_every_declared_knob_is_what_init_lands(tmp_path):
    checked, refused = 0, 0
    for i, (label, kw) in enumerate(_option_sets()):
        try:
            eng = _mk_engine(tmp_path / str(i), **kw)
        except Exception:  # noqa: BLE001 — a value the constructor refuses is not a landing to compare
            refused += 1
            continue
        landed = vars(eng)
        wrong = {name: (landed.get(name, "<absent>"), knob.settled(eng.options))
                 for name, knob in KNOBS.items()
                 if name not in landed or not agrees(landed[name], knob.settled(eng.options))}
        assert not wrong, (f"{label}: the declared settle rule disagrees with what Engine.__init__ "
                           f"lands (landed, declared): {wrong}")
        checked += 1
    # Most of the ~730 sets construct; a handful of falsy spellings are refused (`float(None)`).
    assert checked > 500 and refused < checked, (checked, refused)


def test_the_declaration_and_the_derivation_name_the_same_attributes():
    derived = attr_by_field()
    for name, knob in KNOBS.items():
        assert derived.get(knob.field) == name, (
            f"{name} = Knob({knob.field!r}) but driving `{knob.field}` moves "
            f"{derived.get(knob.field)!r}")
    landing = set(derived.values())
    assert set(KNOBS).isdisjoint(EXPLICIT_IN_INIT), set(KNOBS) & set(EXPLICIT_IN_INIT)
    assert set(KNOBS) | set(EXPLICIT_IN_INIT) == landing, {
        "a field lands on an attribute that is neither a Knob nor explicit (declare it, or say why "
        "not in EXPLICIT_IN_INIT)": sorted(landing - set(KNOBS) - set(EXPLICIT_IN_INIT)),
        "declared but no field lands there": sorted((set(KNOBS) | set(EXPLICIT_IN_INIT)) - landing)}
    for name, why in EXPLICIT_IN_INIT.items():
        assert len(why.split()) >= 5, f"{name}: say why it is not a Knob: {why!r}"


def test_a_stub_that_never_ran_init_reads_the_knob_set_a_real_bare_engine_has(tmp_path):
    """Before step 4 an `Engine.__new__(Engine)` stub raised AttributeError on every knob it had not
    been handed — and a blind handler upstream turned that into a different path — or answered a
    `getattr` default. Now it reads what `Engine(...)` settles to."""
    real = vars(_mk_engine(tmp_path / "real"))
    stub = Engine.__new__(Engine)
    wrong = {name: (getattr(stub, name), real[name]) for name in KNOBS
             if not agrees(getattr(stub, name), real[name])}
    assert not wrong, f"a stub reads a knob a real bare Engine does not have: {wrong}"
    # …and a stub handed its own record reads THAT, through the same rule.
    configured = Engine.__new__(Engine)
    configured.options = EngineOptions(train_monitor=True, asha_live_min_siblings=0)
    assert configured._train_monitor is True and configured._asha_live_min_siblings == 1


def test_a_knob_is_still_a_plain_attribute_to_everything_that_writes_it(tmp_path):
    eng = _mk_engine(tmp_path / "eng", timeout=3.0)
    eng._merge_mode = "ensemble"                   # what `_apply_strategy` does
    eng._train_monitor = True
    assert eng._train_monitor is True and "_train_monitor" in vars(eng)
    stub = Engine.__new__(Engine)
    stub._repair_log_tools = False                 # what a test double does
    assert stub._repair_log_tools is False
    del stub._repair_log_tools                     # …and the library value comes back
    assert stub._repair_log_tools is True
    # A stub's read is kept on the instance: one dict, mutated in place, as on a real engine.
    stub._eval_env["X"] = "1"
    assert stub._eval_env == {"X": "1"} and LIBRARY_DEFAULTS.eval_env == {}
    assert isinstance(EngineKnobs.__dict__["_train_monitor"], Knob)
    assert Engine._train_monitor is EngineKnobs.__dict__["_train_monitor"], "class access: the Knob"


def test_the_researcher_is_stamped_from_the_landed_knobs(tmp_path):
    """Four knobs are threaded onto the RESEARCHER by `Engine.__init__`, each inside a blind `try`
    (a toy role may refuse an attribute) — so a stamp that raised, say on a knob local that step 4c
    removed, would fail silently and the prompt would keep its default. Measured on a throwaway copy:
    exactly that mutation left the knob tests green. Driven here, both ways."""
    off = _mk_engine(tmp_path / "off", track_hypotheses=False, memo_verdict_cue=False,
                     gpu_footprint_cue=False, digest_char_cap=7).researcher
    assert (off.track_hypotheses, off._memo_verdict_cue, off._gpu_footprint_cue, off._digest_cap) \
        == (False, False, False, 7)
    on = _mk_engine(tmp_path / "on").researcher
    assert (on.track_hypotheses, on._memo_verdict_cue, on._gpu_footprint_cue, on._digest_cap) \
        == (True, True, True, 0)


def test_no_knob_hides_a_reader_registered_as_not_knowable():
    """A registered reader turns a MISSING knob into "say nothing" / "fail closed"; a descriptor
    would answer an `Engine.__new__` stub with the library value and retire that sentinel quietly."""
    sentinel_attrs = {key.rsplit("::", 1)[-1] for key in UNSETTLED_KNOB_DEFAULTS}
    assert not sentinel_attrs & set(KNOBS), sentinel_attrs & set(KNOBS)


@pytest.mark.parametrize("name", sorted(KNOBS))
def test_the_knob_table_names_a_real_field(name):
    assert KNOBS[name].field in EngineOptions.__dataclass_fields__
    assert KNOBS[name].name == name
