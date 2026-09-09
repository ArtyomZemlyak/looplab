"""Three rules that two or more readers must agree on, each now spelled once.

* EV-11 — which concept provenance tiers a child may INHERIT through. `_materialize_concept_deltas`
  consults it from two passes over the same log (the Kahn topological walk and the cycle fallback);
  if those disagree, one event log folds to different memberships depending only on whether the node
  graph happened to contain a cycle. That is a replay-determinism break with no error anywhere.
* EM-13 — what counts as a citable node id. `_valid_node_source` is the fence and `_node_ids` is the
  reader; the fence's whole job is to quarantine a row whose element the reader would DROP, so a rule
  that widens on one side only turns "quarantined" into "complete but missing evidence".
* EM-15 — the unicode word tokenizer. Four modules had declared it independently, and each of them
  feeds a PERSISTED identity (a task fingerprint, a concept key, a claim uid, a retrieval token set),
  so a unicode fix applied to three of them re-keys three stores and silently strands the fourth.
"""
from __future__ import annotations

import inspect
import re

from _source_scan import iter_sources

import pytest

from looplab.core.models import (NODE_CONCEPT_PROVENANCE_AUTHORED,
                                 NODE_CONCEPT_PROVENANCE_CLASSIFIER,
                                 NODE_CONCEPT_PROVENANCE_OFFLINE_HEURISTIC,
                                 NODE_CONCEPT_PROVENANCE_OPERATOR,
                                 NODE_CONCEPT_PROVENANCE_UNTRUSTED)
from looplab.core.text import WORD_RE, normalize_text, tokenize
from looplab.engine.claims_health import _node_ids, _parse_node_id, _valid_node_source
from looplab.events import card_ledger, replay


# ------------------------------------------------------------------ EV-11: inheritable provenance

def test_the_inheritable_tiers_are_exactly_the_exact_set_producers():
    assert replay._INHERITABLE_CONCEPT_PROVENANCE == frozenset({
        NODE_CONCEPT_PROVENANCE_AUTHORED,
        NODE_CONCEPT_PROVENANCE_CLASSIFIER,
        NODE_CONCEPT_PROVENANCE_OPERATOR,
        NODE_CONCEPT_PROVENANCE_OFFLINE_HEURISTIC,
    })


def test_untrusted_is_NOT_inheritable_but_IS_displayable():
    """The one tier where the two sets legitimately differ. A low-trust display taxonomy may be shown
    on a card; inheriting a child's membership through it would launder it into an exact statement."""
    assert NODE_CONCEPT_PROVENANCE_UNTRUSTED not in replay._INHERITABLE_CONCEPT_PROVENANCE
    assert NODE_CONCEPT_PROVENANCE_UNTRUSTED in card_ledger._CARD_NODE_CONCEPT_PROVENANCE


def test_the_card_set_is_DERIVED_from_the_inheritable_one():
    """Not merely equal today — derived, so a new tier added to the inheritable set cannot be
    forgotten on the display side."""
    assert card_ledger._CARD_NODE_CONCEPT_PROVENANCE == (
        replay._INHERITABLE_CONCEPT_PROVENANCE | {NODE_CONCEPT_PROVENANCE_UNTRUSTED})


def test_both_materialization_passes_read_the_same_constant():
    """The finding's actual risk. Two inline copies inside one function, one per pass, is how the
    topological path and the cycle path start disagreeing about the same log."""
    source = inspect.getsource(replay._materialize_concept_deltas)
    assert source.count("_INHERITABLE_CONCEPT_PROVENANCE") == 2, (
        "a materialization pass stopped consulting the shared set")
    assert "NODE_CONCEPT_PROVENANCE_CLASSIFIER," not in source, (
        "an inline provenance set literal came back inside the materializer")


# ------------------------------------------------------------------ EM-13: one node-id rule

@pytest.mark.parametrize("value,expected", [
    (0, 0), (7, 7), ("7", 7), ("  7  ", 7),
    (-1, None), ("-1", None), (True, None), (False, None),
    (1.0, None), ("x", None), ("", None), (None, None), ([], None), ("1" * 25, None),
])
def test_the_shared_parser_answers_each_element_once(value, expected):
    assert _parse_node_id(value) == expected


def test_a_bool_is_not_node_one():
    """`True` is an `int` subclass. `isinstance(x, int)` would cite node 1 for a JSON `true`, and the
    citation would look exactly like a real one."""
    assert _parse_node_id(True) is None
    assert _node_ids([True, 2]) == [2]
    assert _valid_node_source([True]) is False


def test_a_negative_id_is_rejected_by_the_fence_AND_dropped_by_the_reader():
    """The phantom-ref case the rule exists for: a node id INDEXES the node table, so `-1` gets
    run-qualified into an authoritative-looking `run:-1` — a citation to a node that cannot exist, in
    a row the health receipt would otherwise call complete."""
    assert _valid_node_source([-1]) is False
    assert _node_ids([-1, 3]) == [3]


def test_the_24_char_bound_is_the_same_on_both_sides():
    """A widened bound on one side only is exactly the drift the single-sourcing prevents: the reader
    would keep an id the fence rejects, or the fence would pass one the reader silently drops."""
    long_but_numeric = "1" * 25
    assert _valid_node_source([long_but_numeric]) is False
    assert _node_ids([long_but_numeric]) == []


def test_the_fence_rejects_exactly_what_the_reader_would_drop():
    """The invariant stated directly, over a corpus that mixes both."""
    corpus = [0, 7, "7", -1, "-1", True, 1.5, "x", "", None, [], "1" * 25, 2 ** 70, str(2 ** 70)]
    for value in corpus:
        kept = _node_ids([value]) != []
        assert _valid_node_source([value]) is kept, f"fence and reader disagree on {value!r}"


def test_a_bare_int_is_accepted_as_a_one_element_list():
    assert _valid_node_source(3) is True
    assert _node_ids(3) == [3]


def test_an_over_long_list_is_still_rejected_by_the_fence_only():
    """The LENGTH bound belongs to the fence, not to the per-element rule — the reader's job is to
    read what survived, and truncating there would hide a row that should have been quarantined."""
    from looplab.engine.claims_health import _MAX_SOURCE_EVIDENCE

    too_many = list(range(_MAX_SOURCE_EVIDENCE + 1))
    assert _valid_node_source(too_many) is False
    assert len(_node_ids(too_many)) == len(too_many)


def test_neither_side_re_derives_the_rule():
    fence = inspect.getsource(_valid_node_source)
    reader = inspect.getsource(_node_ids)
    for name, source in (("fence", fence), ("reader", reader)):
        assert "_parse_node_id" in source, f"the {name} does not use the shared parser"
        assert "isdigit()" not in source, f"the {name} re-derives the numeric-string rule"


# ------------------------------------------------------------------ EM-15: one tokenizer

def test_the_underscore_is_a_SEPARATOR():
    """`train_loss` is two tokens. That is what lets an identifier-shaped statement match a prose one;
    treating `_` as a word character would key them apart."""
    assert tokenize("train_loss") == ["train", "loss"]


def test_every_script_tokenizes():
    """No alphabet allowlist — a Cyrillic or CJK goal must not reduce to nothing and key every such
    task to the same empty fingerprint."""
    assert tokenize("градиент шум") == ["градиент", "шум"]
    assert tokenize("学習率 warmup") == ["学習率", "warmup"]


def test_casefold_not_lower():
    """`.lower()` leaves ß alone while `.casefold()` maps it to `ss`; a store keyed by one and read by
    the other would split a single concept by script alone."""
    assert tokenize("STRAßE") == tokenize("strasse")


def test_nfkc_folds_compatibility_forms():
    """Two honest spellings of the same statement must produce one key."""
    assert tokenize("ﬁt") == tokenize("fit")
    assert tokenize("ＡＢＣ") == ["abc"]


def test_normalize_text_is_the_tokenizers_own_first_half():
    """Exposed separately because `claim_key` needs BOTH the tokens and the normalized string — it
    matches the "n't" contraction against the string. Re-deriving it there would be a fifth copy."""
    assert tokenize("Don't ﬁt") == WORD_RE.findall(normalize_text("Don't ﬁt"))


@pytest.mark.parametrize("module,attr", [
    ("looplab.engine.memory", "_WORD_UNICODE"),
    ("looplab.engine.concept_registry", "_WORD"),
    ("looplab.engine.claims_health", "_CLAIM_WORD"),
])
def test_every_identity_module_uses_the_SAME_compiled_object(module, attr):
    """Identity, not equality: a separately-compiled twin with the same pattern would pass an equality
    check today and drift on the next unicode fix."""
    import importlib

    assert getattr(importlib.import_module(module), attr) is WORD_RE


def test_no_module_re_compiles_the_pattern():
    from pathlib import Path

    root = Path(replay.__file__).resolve().parents[1]
    offenders = [
        f"{path.relative_to(root)}:{index}"
        for path, source in iter_sources(root)
        for index, line in enumerate(source.split("\n"), 1)
        if r'compile(r"[^\W_]+"' in line and path.name != "text.py"
    ]
    assert not offenders, f"the unicode tokenizer was re-declared at {offenders}"


def test_the_legacy_ascii_fingerprint_variant_is_deliberately_NOT_shared():
    """`memory.py::_WORD_ASCII` is a VERSIONED compatibility contract — the pre-unicode fingerprint
    mode a running portfolio must still match — not a fourth copy of this rule. Folding it in would
    silently re-key every stored fingerprint mid-flight."""
    from looplab.engine import memory

    assert memory._WORD_ASCII is not WORD_RE
    assert memory._WORD_ASCII.pattern == "[a-z0-9]+"


def test_the_claim_uid_still_keys_the_same_statements_together():
    """An end-to-end guard on the shared pipeline: the tokenizer feeds a PERSISTED uid, so a change
    that re-keyed equivalent spellings would strand every stored decision."""
    from looplab.engine.claim_key import claim_uid

    assert claim_uid("Hard-neg mining HELPS recall") == claim_uid("hard-neg mining helps recall")
    assert claim_uid("hard-neg mining helps recall") != claim_uid("recall helps hard-neg mining")


def test_the_word_pattern_itself_did_not_change():
    assert WORD_RE.pattern == r"[^\W_]+" and bool(WORD_RE.flags & re.UNICODE)


# ------------------------------------------------------------------ SC-03: one run-child rule
#
# Canonical run-path validation was implemented at least six ways. The first pass shared the LEAVES
# (`is_reparse`, `WINDOWS_RESERVED`, `filesystem_identity`); the six full validators kept their own
# copy of the COMPOSITION and had already drifted — two re-spelled `is_reparse` inline out of the
# mode bit and the Windows attribute, one omitted the junction probe its siblings make, one compared
# `normcase(abspath(...))` where `filesystem_identity` also folds macOS normalization.
#
# `pathsafe.validate_run_child` is that composition; what stays per-caller is the VOCABULARY, which
# is why the copies survived. Both halves are driven below: one physical defect, five callers, five
# different refusals.

@pytest.mark.parametrize("name,default,strict", [
    ("run1", None, None),
    ("run.2026-08-01", None, None),
    ("", "absent", "absent"),
    (None, "absent", "absent"),
    (7, "absent", "absent"),
    (".", "not_a_plain_name", "not_a_plain_name"),
    ("..", "not_a_plain_name", "not_a_plain_name"),
    ("a/b", "not_a_plain_name", "not_a_plain_name"),
    ("a\\b", "not_a_plain_name", "not_a_plain_name"),
    ("a\x00b", "not_a_plain_name", "not_a_plain_name"),
    # Everything below is admitted at READ time and refused where a run is CREATED or DESTROYED.
    ("x" * 256, None, "filesystem_ambiguous"),
    (" run", None, "filesystem_ambiguous"),
    ("run ", None, "filesystem_ambiguous"),
    ("run.", None, "filesystem_ambiguous"),
    ("run:1", None, "filesystem_ambiguous"),
    ("run\x01", None, "filesystem_ambiguous"),
    ("run\x7f", None, "filesystem_ambiguous"),
    ("CON", None, "filesystem_ambiguous"),
    ("com1.log", None, "filesystem_ambiguous"),
])
def test_the_two_name_tiers_admit_exactly_what_their_callers_admitted(name, default, strict):
    """The tiers are a DECISION, not an accident: the read paths must keep opening a directory the
    CLI created out of band, while the paths that create or destroy one refuse every
    filesystem-ambiguous spelling. Strict is a superset — a name the read tier refuses can never be
    accepted by the writer."""
    from looplab.core.pathsafe import run_child_name_defect

    assert run_child_name_defect(name) == default
    assert run_child_name_defect(name, strict=True) == strict
    if default is not None:
        assert strict is not None, "the strict tier must refuse everything the default tier does"


def test_the_containment_half_inspects_no_entry_and_the_full_half_does(tmp_path):
    """`must_exist=False` is a DIFFERENT question, not a weaker one: `launch` is about a run that
    does not exist yet, and what an existing entry there means is its own conflict policy. Proven by
    the case that separates them — a symlink is accepted by one and refused by the other."""
    from looplab.core.pathsafe import validate_run_child

    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)

    assert validate_run_child(tmp_path, "never-created", must_exist=False).defect is None
    assert validate_run_child(tmp_path, "link", must_exist=False).defect is None
    assert validate_run_child(tmp_path, "never-created").defect == "missing"
    assert validate_run_child(tmp_path, "link").defect == "indirect"
    assert validate_run_child(tmp_path, "real").path == (tmp_path / "real").resolve()


def test_the_full_rule_refuses_every_shape_that_is_not_a_direct_child_directory(tmp_path):
    from looplab.core.pathsafe import validate_run_child

    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "nodes").mkdir()
    (tmp_path / "afile").write_text("x", encoding="utf-8")

    assert validate_run_child(tmp_path, "afile").defect == "not_a_directory"
    # The descendant case this rule exists for: `real/nodes` is sandbox-WRITABLE, so an events.jsonl
    # a candidate wrote there must never be addressable as a run.
    assert validate_run_child(tmp_path, "real/nodes").defect == "not_a_plain_name"
    assert validate_run_child(tmp_path, tmp_path / "real" / "nodes").defect == "outside_root"
    assert validate_run_child(tmp_path, tmp_path).defect == "outside_root"


def _serve_root(tmp_path):
    """A real app over a run root holding one good run, one symlink to it, and one plain file."""
    from looplab.events.eventstore import EventStore
    from looplab.serve.server import make_app

    (tmp_path / "good").mkdir()
    EventStore(tmp_path / "good" / "events.jsonl").append(
        "run_started", {"run_id": "good", "task_id": "t", "goal": "g", "direction": "min"})
    (tmp_path / "link").symlink_to(tmp_path / "good", target_is_directory=True)
    (tmp_path / "afile").write_text("x", encoding="utf-8")
    return make_app(tmp_path).state.looplab


def test_one_defect_five_callers_five_preserved_vocabularies(tmp_path):
    """The whole point of SC-03's second half. A run id that resolves through a SYMLINK is one
    physical defect, and every validator that judges the entry must now agree it is a defect — while
    each keeps answering in its own protocol, which is exactly why the six copies survived the first
    pass. Driven through the real callers, not asserted about their source."""
    from fastapi import HTTPException

    from looplab.serve import deletion_service, launch

    srv = _serve_root(tmp_path)
    root = srv.root.resolve()

    assert srv.run_dir("good") == root / "good"                       # precondition: the run opens

    with pytest.raises(HTTPException) as read:                        # AppState.run_dir
        srv.run_dir("link")
    assert read.value.status_code == 404

    with pytest.raises(HTTPException) as generation:                  # run_generation_if_present
        srv.commands.run_generation_if_present(root / "link")
    assert generation.value.status_code == 404

    with pytest.raises(HTTPException) as delete:                      # _strict_existing_run
        deletion_service._strict_existing_run(srv, "link")
    assert delete.value.status_code == 404
    assert delete.value.detail["code"] == "run_not_found"

    with pytest.raises(HTTPException) as start:                       # safe_run_dir
        launch.safe_run_dir(root, "link")
    assert start.value.status_code == 409, "launch answers the SAME defect as a name conflict"
    assert start.value.detail["code"] == "run_path_conflict"


def test_the_command_service_still_owns_the_containment_half(tmp_path):
    """`validate_paths` deliberately takes the containment half only — it runs on a path
    `AppState.run_dir` already judged, and a run may legitimately be mid-creation. What it DOES own
    is the direct-child rule, and a node workspace is the descendant it must refuse."""
    from fastapi import HTTPException

    srv = _serve_root(tmp_path)
    root = srv.root.resolve()
    (root / "good" / "nodes").mkdir()

    assert srv.commands.validate_paths(root / "good") == root / "good"
    with pytest.raises(HTTPException) as exc:
        srv.commands.validate_paths(root / "good" / "nodes")
    assert exc.value.status_code == 404


@pytest.mark.parametrize("rel", ["serve/appstate.py", "serve/reset_route.py",
                                 "serve/run_commands.py", "serve/deletion_service.py",
                                 "serve/launch.py"])
def test_no_run_validator_re_derives_the_composition(rel):
    """A NEGATIVE pin, which stays a substring on purpose: what must not come back is the TEXT of
    the copies — the inline reparse flag and the hand-rolled junction probe that `validate_run_child`
    now owns. `core/pathsafe.py` is the one place either may appear."""
    from _source_scan import PKG

    source = (PKG / rel).read_text(encoding="utf-8-sig")
    assert "FILE_ATTRIBUTE_REPARSE_POINT" not in source, f"{rel} re-spells the reparse flag"
    assert 'getattr(requested, "is_junction"' not in source, f"{rel} re-spells the junction probe"
