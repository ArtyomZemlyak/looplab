"""Every word `unresolved` can carry must appear in the shape `bind_applied_params` documents.

MEASURED 2026-08-28. `bind_applied_params`' docstring described the record as

    "unresolved": {key: "absent" | "ambiguous"}

while `rubertlite-dr-unified-v8` holds **12 rows** whose `unresolved` says `conflict` — a third word
this very module defines (`UNRESOLVED_CONFLICT`, applied_params.py:126) with its own argument: two
carriers each answer the coordinate once and disagree, "surfaced, never settled". A reader keying on
the documented pair drops a state the durable record has been emitting for weeks.

WHY A TEST AND NOT JUST A DOC EDIT. The two halves of the vocabulary live in different modules —
`param_carriers` owns `absent`/`ambiguous` (one document's answer) and this module owns `conflict`
(two documents disagreeing) — so nothing structurally forces them to be written down together, and
the drift that happened once will happen again the next time a state is added. This pins the union.

WHAT THIS DELIBERATELY DOES NOT ASK FOR. A fourth word meaning "the carrier names this path and its
value is not a number" — which is what `use_batch_centering: false` and `mining_type: vector` really
are, and which the corpus shows is the MOST COMMON unresolved key (21 and 10 rows). That word is
explicitly REFUSED at `param_carriers.py:89`: "Deliberately only TWO members ... it is not recorded
because the extractors keep numeric leaves only — a slug no input can produce is a dead branch, and
this repo has paid for those." The extractor never emits a path for a non-numeric leaf, so from the
resolver's side the coordinate genuinely IS absent, and a word for a state nothing can produce would
be exactly the dead branch that comment refuses. Changing that means changing the extractor's
contract, not adding a constant.
"""
from __future__ import annotations

import inspect

from looplab.core import param_carriers
from looplab.runtime.applied_params import UNRESOLVED_CONFLICT, bind_applied_params


def _documented_shape() -> str:
    doc = inspect.getdoc(bind_applied_params) or ""
    assert '"unresolved"' in doc, "the record's shape block moved — re-point this test at it"
    return doc


def test_every_unresolved_word_the_code_can_emit_is_in_the_documented_shape():
    """The union, pinned. `param_carriers` owns two of these words and this module owns the third;
    a new state added to either side without a doc line is what this reddens on."""
    doc = _documented_shape()
    for word in (param_carriers.UNRESOLVED_ABSENT,
                 param_carriers.UNRESOLVED_AMBIGUOUS,
                 UNRESOLVED_CONFLICT):
        assert f'"{word}"' in doc, f"`unresolved` can be {word!r} and the shape does not say so"


def test_the_carrier_vocabulary_is_still_exactly_two_words():
    """The other half of the union, so that a word added to `param_carriers` without being
    documented here cannot pass by widening the tuple alone."""
    assert param_carriers.UNRESOLVED_REASONS == (param_carriers.UNRESOLVED_ABSENT,
                                                 param_carriers.UNRESOLVED_AMBIGUOUS)


def test_a_boolean_carrier_value_still_reads_as_absent_and_that_is_the_declined_behaviour():
    """The conflation this test does NOT ask to be fixed, driven so the refusal stays honest.

    A declared coordinate whose carrier value is a YAML boolean resolves to `absent`, indistinguishable
    from a coordinate the carrier never mentions. That is a real loss of information and it is
    DECLINED at param_carriers.py:89 on the ground that the extractor keeps numeric leaves only. If
    someone ever changes the extractor, this test is where the consequence surfaces.
    """
    import pathlib
    import tempfile

    root = pathlib.Path(tempfile.mkdtemp())
    (root / "config.yaml").write_text("loss:\n  use_batch_centering: false\n  temperature: 0.05\n")
    record = bind_applied_params({"loss.use_batch_centering": 0.0, "loss.temperature": 0.05},
                                 str(root), applied_config_glob="*.yaml")
    assert record is not None
    assert record["applied"] == {"loss.temperature": 0.05}
    assert record["unresolved"] == {"loss.use_batch_centering": param_carriers.UNRESOLVED_ABSENT}
    assert record["checked"] == 1 and record["declared"] == 2


def _tmp(files: dict):
    import pathlib
    import tempfile

    root = pathlib.Path(tempfile.mkdtemp())
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def test_an_AMBIGUOUS_document_survives_a_settled_reading_from_another_carrier():
    """THE PROPERTY: `ambiguous` is a fact about the DECLARATION, so no other carrier settles it.

    The document branch already refuses to let a later `absent` overwrite it — "it is a fact about
    the DECLARATION, not about one file" — and until 2026-08-31 the settle loop popped it anyway
    whenever any carrier answered the key once. The record then said the coordinate was cleanly
    answered about a node whose own config defines it at two or more leaves, possibly at two OTHER
    numbers.

    MUTATION: restore the bare `unresolved.pop(key, None)` and this goes red.
    """
    root = _tmp({
        "config.yaml": ("train:\n  training:\n    batch_size: 512\n"
                        "test:\n  training:\n    batch_size: 64\n"),
        "train.py": "config.training.batch_size = 4096\n",
    })
    record = bind_applied_params({"training.batch_size": 8192.0}, str(root),
                                 carriers=("config.yaml", "train.py"))
    assert record is not None, "the .py carrier must answer, or this fixture proves nothing"
    assert record["applied"] == {"training.batch_size": 4096.0}, (
        "the settled reading still rides — this fix withholds nothing")
    assert record["unresolved"] == {"training.batch_size": param_carriers.UNRESOLVED_AMBIGUOUS}, (
        "MUTATION: pop unconditionally and the plural document vanishes from the record")


def test_an_ABSENT_marker_is_still_popped_by_a_settled_reading():
    """The negative control, and the half the narrowing must not eat: a carrier that simply does not
    mention the key says nothing about the declaration, so a later answer settles it clean."""
    root = _tmp({
        "config.yaml": "loss:\n  temperature: 0.05\n",
        "train.py": "config.training.batch_size = 4096\n",
    })
    record = bind_applied_params({"training.batch_size": 8192.0}, str(root),
                                 carriers=("config.yaml", "train.py"))
    assert record is not None
    assert record["applied"] == {"training.batch_size": 4096.0}
    assert "unresolved" not in record or "training.batch_size" not in record["unresolved"], (
        "MUTATION: keep every marker and an ordinary answered coordinate reads as unresolved")
