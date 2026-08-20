"""THE CARRIER RULE: the declared-vs-applied guard must read what DECIDES the value.

`engine/repair_verify.py::declared_param_overrides` was written to answer "does this node's own code
contradict its declared `Idea.params`" and read only `.py` files. On the task family this box runs,
the deciding artifact is a YAML document — so the guard was handed
`vectorsearch/configs/config.yaml` (17,706 bytes, holding `batch_size: 512`) and answered `()` about
a champion recorded at `batch_size: 8192`. A FALSE CLEAN, not a miss.

EVERY POSITIVE ASSERTION HERE HAS A NEGATIVE CONTROL BESIDE IT, because the defect being fixed IS a
vacuous green and a test suite that only ever asks "does it fire?" reproduces it one layer up. For
each rule the file drives the input that makes it fire AND the nearest input that must not:

    the carrier rule        a YAML carrier diverges  /  the same YAML agreeing
                            /  the same bytes under a suffix that is not a carrier
    the ambiguity refusal   a suffix naming ONE leaf  /  the same suffix naming TWO
    the YAML-1.1 repair     a plain `5e-3`  /  a QUOTED `"0.005"`
    the applied record      a divergence  /  agreement (`checked` proves it looked)
                            /  nothing checked at all (`checked == 0`)
    the resolved tier       a unique match binds  /  two matches refuse and SAY so
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import time

import pytest

from looplab.core import param_carriers
from looplab.engine.champion_caveats import (CHAMPION_CAVEAT_PARAMS_OVERRIDDEN,
                                             applied_params_diverged)
from looplab.engine.repair_verify import (PARAM_OVERRIDE_MIN_PARTS, declared_param_overrides)
from looplab.runtime import applied_params

# The v2 champion's shape, reduced to what the rule needs. The declaration is the Researcher's
# proposal; the YAML is what the Developer committed, comments and all — including the reasoning,
# which this rung must never read and which is here precisely so that a rung that started reading it
# would have something to trip over.
_CHAMPION_PARAMS = {
    "train.training.batch_size": 8192.0,
    "train.training.gradient_accumulation_steps": 2.0,
    "train.training.n_epochs": 15.0,
    "loss.temperature": 0.05,
}
_CHAMPION_YAML = """\
train:
  loss:
    temperature: 0.05
  training:
    n_epochs: 3          # cut from 15: 3x703 steps x ~10.8s/it ~ 6.3h fits the 10h budget
    batch_size: 512      # halved again to fit H200 under R-Drop's 8 concurrent forwards
    gradient_accumulation_steps: 32   # effective batch 16384 (512 x 32)
adapter:
  training:
    n_epochs: 80
    batch_size: 128
"""
_AGREEING_YAML = """\
train:
  loss:
    temperature: 0.05
  training:
    n_epochs: 15
    batch_size: 8192
    gradient_accumulation_steps: 2
adapter:
  training:
    n_epochs: 80
    batch_size: 128
"""
_CARRIER = "vectorsearch/configs/config.yaml"


# --------------------------------------------------------------------------------------------
# THE RULE
# --------------------------------------------------------------------------------------------

def test_a_yaml_carrier_that_contradicts_the_declaration_is_reported():
    """THE PROPERTY. The rung reads the file that DECIDES the value, whatever its format."""
    rows = {r.param: r for r in declared_param_overrides(_CHAMPION_PARAMS, {_CARRIER: _CHAMPION_YAML})}
    assert set(rows) == {"train.training.batch_size", "train.training.gradient_accumulation_steps",
                         "train.training.n_epochs"}, rows
    assert rows["train.training.batch_size"].declared == 8192.0
    assert rows["train.training.batch_size"].code == 512.0
    assert rows["train.training.n_epochs"].code == 3.0
    assert rows["train.training.gradient_accumulation_steps"].code == 32.0
    # The row names the file and the LINE, so the caveat is actionable and not an accusation.
    assert all(r.path == _CARRIER for r in rows.values())
    assert rows["train.training.n_epochs"].line == 5, "1-based, the way an editor counts"
    # `loss.temperature` AGREES (0.05 both sides) and is therefore absent — silence is the answer
    # for a coordinate the carrier confirms, which is what makes the three rows above mean something.
    assert "loss.temperature" not in rows


def test_the_same_working_set_agreeing_produces_nothing():
    """NEGATIVE CONTROL for the rule above. Without it the test is satisfied by a rung that
    reports every declared key it can find, which is not a guard at all."""
    assert declared_param_overrides(_CHAMPION_PARAMS, {_CARRIER: _AGREEING_YAML}) == ()


def test_the_carrier_is_the_format_and_not_the_words_in_the_file():
    """NEGATIVE CONTROL for the DISPATCH. The identical bytes under a suffix this rung cannot parse
    yield nothing — so what fires the guard is the parse, never a substring scan for `batch_size`
    (the pre-filter is a cost optimisation and must never be the thing deciding)."""
    assert declared_param_overrides(_CHAMPION_PARAMS, {"notes.txt": _CHAMPION_YAML}) == ()
    assert declared_param_overrides(_CHAMPION_PARAMS, {"config.yaml.bak": _CHAMPION_YAML}) == ()


def test_a_json_carrier_is_read_by_the_same_rule():
    """JSON is a structured document too, and the rule is about the FORMAT rather than a file list."""
    doc = json.dumps({"train": {"training": {"n_epochs": 4}}})
    rows = declared_param_overrides({"train.training.n_epochs": 15.0}, {"cfg.json": doc})
    assert [(r.param, r.code) for r in rows] == [("train.training.n_epochs", 4.0)]
    ok = json.dumps({"train": {"training": {"n_epochs": 15}}})
    assert declared_param_overrides({"train.training.n_epochs": 15.0}, {"cfg.json": ok}) == ()


def test_an_unparseable_document_is_silence_and_never_a_guess():
    """An agent may commit anything. A parse error is not evidence about a parameter — the same rule
    the `.py` side already applies to a `SyntaxError`."""
    assert declared_param_overrides(_CHAMPION_PARAMS, {_CARRIER: "train:\n  - [unclosed\n"}) == ()
    assert declared_param_overrides(_CHAMPION_PARAMS, {"cfg.json": "{not json"}) == ()


# --------------------------------------------------------------------------------------------
# AMBIGUITY IS REFUSED, NEVER GUESSED — both directions
# --------------------------------------------------------------------------------------------

def test_a_suffix_naming_exactly_one_leaf_resolves():
    """The POSITIVE half. 40 % of the corpus's resolvable declarations are this shape: the carrier
    nests one level deeper than the coordinate the Researcher writes."""
    rows = declared_param_overrides({"loss.temperature": 0.05}, {_CARRIER: _CHAMPION_YAML[:60]})
    assert [(r.param, r.code) for r in rows] == [], "0.05 agrees — see the next case for a divergence"
    diverging = "train:\n  loss:\n    temperature: 0.01\n"
    rows = declared_param_overrides({"loss.temperature": 0.05}, {_CARRIER: diverging})
    assert [(r.param, r.code) for r in rows] == [("loss.temperature", 0.01)]


def test_a_suffix_naming_two_leaves_is_refused_and_not_tie_broken():
    """THE REFUSAL, and it is the half a guess would silently pass. `training.n_epochs` names both
    `train.training.n_epochs` (3) and `adapter.training.n_epochs` (80) in the champion's own file;
    neither is the answer and no tie-break is admissible."""
    assert declared_param_overrides({"training.n_epochs": 15.0}, {_CARRIER: _CHAMPION_YAML}) == ()
    # …and the identical declaration DOES resolve once the second leaf is gone, which is what proves
    # the refusal above is the ambiguity rule and not the declaration being unreadable.
    one_leaf = "train:\n  training:\n    n_epochs: 3\n"
    assert [r.code for r in declared_param_overrides({"training.n_epochs": 15.0},
                                                     {_CARRIER: one_leaf})] == [3.0]


def test_a_full_path_beats_a_longer_one_that_ends_in_it():
    """A declaration that names the WHOLE path has said which leaf it means, so it is not an
    ambiguity question at all — even when longer paths also end in those parts."""
    doc = "train:\n  training:\n    n_epochs: 3\nouter:\n  train:\n    training:\n      n_epochs: 99\n"
    rows = declared_param_overrides({"train.training.n_epochs": 15.0}, {_CARRIER: doc})
    assert [r.code for r in rows] == [3.0], "the exact path wins outright"


def test_a_bare_name_is_still_a_word_and_not_a_path():
    """Unchanged by the carrier extension: `PARAM_OVERRIDE_MIN_PARTS` is applied to the DECLARATION
    before any carrier is opened, so a bare `batch_size` — three leaves in the real config — never
    reaches the resolver at all."""
    assert PARAM_OVERRIDE_MIN_PARTS == 2
    assert declared_param_overrides({"batch_size": 8192.0}, {_CARRIER: _CHAMPION_YAML}) == ()


# --------------------------------------------------------------------------------------------
# WHAT COUNTS AS A NUMBER IN A DOCUMENT
# --------------------------------------------------------------------------------------------

def test_a_plain_scalar_pyyaml_leaves_as_a_string_is_still_a_number():
    """PyYAML implements YAML **1.1**, whose float regex needs a `.` in the mantissa, so `5e-3`
    composes as the STRING '5e-3' — while YAML 1.2, `float()` and every pydantic `float` field read
    0.005. Taking the resolver's word for it dropped two of the forty-one real divergences on this
    box, both on `rubertlite-dr-unified-v8` node 12, which RECORDED a metric (0.761400)."""
    import yaml
    assert yaml.safe_load("t: 5e-3")["t"] == "5e-3", "the resolver quirk this repairs, pinned"
    rows = declared_param_overrides({"train.loss.temperature": 0.05},
                                    {_CARRIER: "train:\n  loss:\n    temperature: 5e-3\n"})
    assert [(r.param, r.code) for r in rows] == [("train.loss.temperature", 0.005)]


def test_a_quoted_scalar_is_a_string_and_is_not_coerced():
    """NEGATIVE CONTROL for the repair above, and the bound on it. An author who wrote `"0.005"`
    wrote a string; a rung that read it as a number would be resolving on the document's behalf."""
    quoted = 'train:\n  loss:\n    temperature: "0.005"\n'
    assert declared_param_overrides({"train.loss.temperature": 0.05}, {_CARRIER: quoted}) == ()
    # …and JSON is never coerced at all, because JSON's type system is exact.
    assert declared_param_overrides({"a.b": 1.0}, {"c.json": '{"a": {"b": "2"}}'}) == ()


def test_a_bool_is_not_a_number_on_either_side():
    """`True` is `isinstance(int)`; comparing it to a declared `1.0` would report an agreement — or a
    divergence — that nobody wrote."""
    assert declared_param_overrides({"a.flag": 1.0}, {"c.yaml": "a:\n  flag: true\n"}) == ()
    assert declared_param_overrides({"a.flag": 0.0}, {"c.yaml": "a:\n  flag: false\n"}) == ()


def test_a_sequence_index_is_not_a_configuration_coordinate():
    """Nothing declares `layers.3.width`, and minting positional paths would put noise in front of
    the suffix rule for a declaration that can never reach it."""
    assert param_carriers.yaml_numeric_paths("a:\n  - 1\n  - 2\n") == {}
    assert param_carriers.json_numeric_paths('{"a": [1, 2]}') == {}


def test_a_recursive_anchor_terminates():
    """`yaml.compose` builds a GRAPH: an anchor referenced from itself is a cycle, and a naive walk
    of it does not return. Bounded here rather than discovered in an eval worker."""
    doc = "a: &x\n  b: 1\n  c: *x\n"
    paths = param_carriers.yaml_numeric_paths(doc)
    assert paths.get(("a", "b")) == (1.0, 2)


# --------------------------------------------------------------------------------------------
# THE PYTHON PATH IS UNCHANGED — the two families are matched by different rules on purpose
# --------------------------------------------------------------------------------------------

def test_python_source_still_reports_every_matching_assignment():
    """A Python target's path is rooted at whatever local the code bound, so the tree is INCOMPLETE
    and two assignments matching one declared suffix are two assignments — not one ambiguous
    declaration. This is the case that would silently disappear if the document rule were applied to
    both families."""
    src = ("cfg.train.training.n_epochs = 3\n"
           "other.train.training.n_epochs = 7\n")
    rows = declared_param_overrides({"train.training.n_epochs": 15.0}, {"t.py": src})
    assert sorted(r.code for r in rows) == [3.0, 7.0]
    # …while the SAME shape in a document is the ambiguity refusal.
    doc = ("cfg:\n  train:\n    training:\n      n_epochs: 3\n"
           "other:\n  train:\n    training:\n      n_epochs: 7\n")
    assert declared_param_overrides({"train.training.n_epochs": 15.0}, {"t.yaml": doc}) == ()


def test_a_mixed_working_set_reports_both_carriers():
    """The real shape: a repo whose values live in a YAML and whose repair moved one into a `.py`."""
    files = {_CARRIER: _CHAMPION_YAML, "vectorsearch/train.py": "cfg.loss.temperature = 0.01\n"}
    rows = declared_param_overrides(_CHAMPION_PARAMS, files)
    assert {r.path for r in rows} == {_CARRIER, "vectorsearch/train.py"}
    assert [r.code for r in rows if r.path.endswith(".py")] == [0.01]


def test_the_repair_baseline_attribution_works_across_carriers():
    """`baseline_files` narrows the answer to what THIS repair introduced, and it has to do that for
    a document exactly as it does for source — otherwise every later attempt re-reports a divergence
    the first one made and the judge's history accuses each of a line none of them wrote."""
    before = {_CARRIER: _CHAMPION_YAML}
    after = {_CARRIER: _CHAMPION_YAML.replace("n_epochs: 3 ", "n_epochs: 2 ")}
    rows = declared_param_overrides(_CHAMPION_PARAMS, after, baseline_files=before)
    assert [(r.param, r.code) for r in rows] == [("train.training.n_epochs", 2.0)], \
        "batch_size/accum were already diverging before this repair and are not its doing"
    # NEGATIVE CONTROL: a repair that changed nothing is charged with nothing.
    assert declared_param_overrides(_CHAMPION_PARAMS, before, baseline_files=before) == ()


def test_the_rung_never_reads_a_rationale():
    """The trust boundary, held by SIGNATURE and by AST rather than by a comment: no agent text can
    summon or evade a row. The champion's YAML carries its reasoning in comments beside every value
    and the rung's answer must be identical without them."""
    assert "rationale" not in inspect.signature(declared_param_overrides).parameters
    stripped = "\n".join(line.split("#")[0].rstrip() for line in _CHAMPION_YAML.splitlines())
    assert ([r.as_row() for r in declared_param_overrides(_CHAMPION_PARAMS, {_CARRIER: _CHAMPION_YAML})]
            == [r.as_row() for r in declared_param_overrides(_CHAMPION_PARAMS, {_CARRIER: stripped})])


def test_the_carrier_suffixes_are_derived_and_not_hand_listed():
    """AST, not a substring: `repair_verify._carrier_kind` must dispatch through
    `param_carriers.is_document_carrier` rather than growing its own copy of the suffix table — two
    lists of file extensions is exactly the drift `core/param_carriers.py` exists to prevent."""
    from looplab.engine import repair_verify
    tree = ast.parse(inspect.getsource(repair_verify._carrier_kind))
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "is_document_carrier" in called
    for suffix in param_carriers.DOCUMENT_SUFFIXES:
        assert repair_verify._carrier_kind("x" + suffix) == repair_verify._CARRIER_DOCUMENT
    assert repair_verify._carrier_kind("x.py") == repair_verify._CARRIER_PYTHON
    assert repair_verify._carrier_kind("x.txt") is None


# --------------------------------------------------------------------------------------------
# THE APPLIED RECORD — bound at the metric read, against a real workdir
# --------------------------------------------------------------------------------------------

def _workdir(tmp_path, **files):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return str(tmp_path)


def test_the_applied_record_reports_the_divergence_and_what_it_read(tmp_path):
    wd = _workdir(tmp_path, **{_CARRIER: _CHAMPION_YAML})
    rec = applied_params.bind_applied_params(_CHAMPION_PARAMS, wd, carriers=[_CARRIER])
    assert rec["authority"] == applied_params.APPLIED_COMMITTED
    assert rec["resolved_refused"] == "not_declared"
    assert rec["applied"]["train.training.batch_size"] == 512.0
    assert {r["param"] for r in rec["diverged"]} == {
        "train.training.batch_size", "train.training.gradient_accumulation_steps",
        "train.training.n_epochs"}
    assert all(r["file"] == _CARRIER for r in rec["diverged"])
    # The record says WHICH BYTES it read, so a later reader can tell whether the file moved.
    assert rec["carriers"][0]["path"] == _CARRIER and rec["carriers"][0]["digest"]
    assert applied_params.applied_divergence_note(rec).startswith("3 of the 4")


def test_agreement_and_nothing_checked_are_DIFFERENT_records(tmp_path):
    """THE VACUOUS-GREEN GUARD, and it is the whole reason `checked` exists. A record carrying only
    `diverged: []` cannot tell "every declared coordinate was compared and they all agree" from "no
    carrier answered a single one of them" — the exact confusion that let the shipped rung report
    clean about a champion it had never parsed."""
    agree_wd = _workdir(tmp_path / "a", **{_CARRIER: _AGREEING_YAML})
    agreed = applied_params.bind_applied_params(_CHAMPION_PARAMS, agree_wd, carriers=[_CARRIER])
    assert agreed["diverged"] == [] and agreed["checked"] == 4 and agreed["declared"] == 4

    silent_wd = _workdir(tmp_path / "b", **{_CARRIER: "unrelated:\n  key: 1\n"})
    silent = applied_params.bind_applied_params(_CHAMPION_PARAMS, silent_wd, carriers=[_CARRIER])
    assert silent is None, "no coordinate answered — absence, never an empty record"

    partial_wd = _workdir(tmp_path / "c", **{_CARRIER: "train:\n  training:\n    n_epochs: 15\n"})
    partial = applied_params.bind_applied_params(_CHAMPION_PARAMS, partial_wd, carriers=[_CARRIER])
    assert partial["diverged"] == [] and partial["checked"] == 1 and partial["declared"] == 4
    assert set(partial["unresolved"]) == {"train.training.batch_size", "loss.temperature",
                                          "train.training.gradient_accumulation_steps"}
    assert set(partial["unresolved"].values()) == {param_carriers.UNRESOLVED_ABSENT}


def test_the_resolved_tier_binds_on_a_unique_match_and_outranks_the_committed_one(tmp_path):
    """The config the eval process itself WROTE is the stronger source, and this is the case only it
    can see: the committed carrier AGREES with the declaration while the resolved one does not —
    `rubertlite-dr-unified-v8` node 8's real shape."""
    wd = _workdir(tmp_path, **{
        _CARRIER: _AGREEING_YAML,
        "vectorsearch/experiments/dclthr/final/config.yaml":
            "train:\n  training:\n    n_epochs: 8\n    batch_size: 4096\n"})
    rec = applied_params.bind_applied_params(
        _CHAMPION_PARAMS, wd, carriers=[_CARRIER],
        applied_config_glob="vectorsearch/experiments/*/final/config.yaml")
    assert rec["authority"] == applied_params.APPLIED_RESOLVED
    assert "resolved_refused" not in rec
    assert {(r["param"], r["applied"]) for r in rec["diverged"]} == {
        ("train.training.n_epochs", 8.0), ("train.training.batch_size", 4096.0)}
    # NEGATIVE CONTROL: without the declaration the same workdir reports the committed carrier, which
    # agrees — so the `resolved` rows above are the pattern doing work and not the file merely being
    # present on disk.
    plain = applied_params.bind_applied_params(_CHAMPION_PARAMS, wd, carriers=[_CARRIER])
    assert plain["authority"] == applied_params.APPLIED_COMMITTED and plain["diverged"] == []


def test_two_matches_are_refused_and_the_refusal_is_on_the_record(tmp_path):
    """28 of the 52 real nodes hold more than one `**/final/config.yaml`, and on 8 of them the
    matches DISAGREE — the training stage and the scoring stage each resolved their own. A pattern
    that picked one would record a number nobody chose."""
    wd = _workdir(tmp_path, **{
        _CARRIER: _CHAMPION_YAML,
        "exp/a/final/config.yaml": "train:\n  training:\n    n_epochs: 8\n",
        "exp/b/final/config.yaml": "train:\n  training:\n    n_epochs: 1\n"})
    rec = applied_params.bind_applied_params(_CHAMPION_PARAMS, wd, carriers=[_CARRIER],
                                             applied_config_glob="exp/*/final/config.yaml")
    assert rec["authority"] == applied_params.APPLIED_COMMITTED
    assert rec["resolved_refused"] == "ambiguous"
    assert rec["resolved_refused"] in applied_params.RESOLVED_REFUSALS
    # It FELL BACK rather than going silent: the committed carrier's own divergence still rides.
    assert {r["param"] for r in rec["diverged"]} >= {"train.training.n_epochs"}
    assert rec["diverged"][0]["applied"] == 512.0 or any(
        r["applied"] == 3.0 for r in rec["diverged"])


def test_a_resolved_config_older_than_the_attempt_is_stale_and_not_elected(tmp_path):
    """Freshness is enforced on the RESOLVED tier only. A config the eval wrote is by definition this
    attempt's; one that predates the attempt is a previous attempt's leftover in a reused workdir."""
    wd = _workdir(tmp_path, **{
        _CARRIER: _AGREEING_YAML,
        "exp/a/final/config.yaml": "train:\n  training:\n    n_epochs: 8\n"})
    old = time.time() - 10_000
    os.utime(os.path.join(wd, "exp/a/final/config.yaml"), (old, old))
    rec = applied_params.bind_applied_params(_CHAMPION_PARAMS, wd, carriers=[_CARRIER],
                                             applied_config_glob="exp/*/final/config.yaml",
                                             since=time.time())
    assert rec["authority"] == applied_params.APPLIED_COMMITTED
    assert rec["resolved_refused"] == "stale"
    # …and the COMMITTED carrier is deliberately not held to that floor: it is staged BEFORE the
    # attempt by construction, so a freshness rule there would refuse every one of them.
    assert rec["checked"] == 4


def test_a_missing_pattern_falls_back_and_never_raises(tmp_path):
    wd = _workdir(tmp_path, **{_CARRIER: _CHAMPION_YAML})
    rec = applied_params.bind_applied_params(_CHAMPION_PARAMS, wd, carriers=[_CARRIER],
                                             applied_config_glob="nowhere/*/config.yaml")
    assert rec["resolved_refused"] == "missing" and rec["checked"] == 4
    # A workdir that is not there at all is absence, not an exception.
    assert applied_params.bind_applied_params(
        _CHAMPION_PARAMS, str(tmp_path / "gone"), carriers=[_CARRIER]) is None


def test_the_two_declaration_filters_admit_exactly_the_same_keys():
    """`runtime` may not import `engine`, so the ≥2-parts / finite / not-bool rule is stated twice.
    Two copies of one rule is this repo's most-measured drift, so they are pinned AGAINST each other
    rather than each against a literal."""
    cases = {"train.training.n_epochs": 15.0, "lr": 0.1, "a.b": True, "c.d": float("nan"),
             "e.f": float("inf"), "g.h": "512", "i.j": -2, "": 1.0, "..": 1.0}
    mine = set(applied_params.declared_numeric_params(cases))
    # The engine-side filter is not exported, so it is exercised THROUGH the guard: a key it admits
    # is one that can produce a row against a carrier that names exactly that path and disagrees.
    theirs = set()
    for key, value in cases.items():
        parts = [p for p in key.split(".") if p]
        if not parts:
            continue
        nested = -999_999.0                       # a value no case declares, so a row means "admitted"
        for part in reversed(parts):
            nested = {part: nested}
        if declared_param_overrides({key: value}, {"c.json": json.dumps(nested)}):
            theirs.add(key)
    assert mine == theirs == {"train.training.n_epochs", "i.j"}


def test_the_champion_caveat_fires_on_both_witnesses():
    """One slug, two witnesses: the byte comparison and the applied record answer the same operator
    question and a second slug would let a reader who saw one believe the other had been asked."""
    assert applied_params_diverged({"applied_params": {"diverged": [{"param": "a.b"}]}}) is True
    assert applied_params_diverged({"applied_params": {"diverged": [], "checked": 9}}) is False
    # READER-SIDE DEFAULTS (invariant #5): every metric recorded before this shipped has no key.
    assert applied_params_diverged({}) is False
    assert applied_params_diverged(None) is False
    assert applied_params_diverged({"applied_params": "not a dict"}) is False


def test_a_folded_run_with_a_yaml_carrier_champion_carries_the_caveat(tmp_path):
    """DRIVE THE PROPERTY, end to end: a real event log, folded, through the real projection. This is
    the assertion that was FALSE before this change — the shipped guard answered `[]` about a
    champion whose own committed config contradicted three of its declared coordinates."""
    from looplab.engine.champion_caveats import champion_metric_caveats
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold
    from looplab.events.types import EV_NODE_CREATED, EV_NODE_EVALUATED, EV_RUN_STARTED

    store = EventStore(str(tmp_path / "events.jsonl"))
    store.append(EV_RUN_STARTED, {"goal": "g", "direction": "max"})
    store.append(EV_NODE_CREATED, {"node_id": 1, "parent_ids": [], "operator": "draft",
                                   "idea": {"operator": "draft", "params": _CHAMPION_PARAMS,
                                            "rationale": "r"},
                                   "code": "pass\n", "files": {_CARRIER: _CHAMPION_YAML}})
    store.append(EV_NODE_EVALUATED, {"node_id": 1, "generation": 0, "metric": 0.793426})
    state = fold(store.read_all())
    assert state.best().id == 1
    assert champion_metric_caveats(state) == [CHAMPION_CAVEAT_PARAMS_OVERRIDDEN]

    # NEGATIVE CONTROL on the same path: an agreeing carrier publishes an UNQUALIFIED number, so the
    # caveat above is the comparison firing and not the projection appending a slug unconditionally.
    clean = EventStore(str(tmp_path / "clean.jsonl"))
    clean.append(EV_RUN_STARTED, {"goal": "g", "direction": "max"})
    clean.append(EV_NODE_CREATED, {"node_id": 1, "parent_ids": [], "operator": "draft",
                                   "idea": {"operator": "draft", "params": _CHAMPION_PARAMS,
                                            "rationale": "r"},
                                   "code": "pass\n", "files": {_CARRIER: _AGREEING_YAML}})
    clean.append(EV_NODE_EVALUATED, {"node_id": 1, "generation": 0, "metric": 0.793426})
    assert champion_metric_caveats(fold(clean.read_all())) == []


def test_the_applied_record_rides_on_the_terminal_and_folds(tmp_path):
    """`metric_provenance` is folded and unknown TOP-LEVEL event keys are not, which is why the
    record is merged onto it rather than given a key of its own."""
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold
    from looplab.events.types import EV_NODE_CREATED, EV_NODE_EVALUATED, EV_RUN_STARTED
    record = {"authority": "committed", "declared": 4, "checked": 4,
              "applied": {"train.training.n_epochs": 3.0},
              "diverged": [{"param": "train.training.n_epochs", "declared": 15.0, "applied": 3.0}]}
    store = EventStore(str(tmp_path / "events.jsonl"))
    store.append(EV_RUN_STARTED, {"goal": "g", "direction": "max"})
    store.append(EV_NODE_CREATED, {"node_id": 1, "parent_ids": [], "operator": "draft",
                                   "idea": {"operator": "draft", "params": _CHAMPION_PARAMS,
                                            "rationale": "r"},
                                   "code": "pass\n", "files": {}})
    store.append(EV_NODE_EVALUATED, {"node_id": 1, "generation": 0, "metric": 0.5,
                                     "metric_provenance": {"applied_params": record}})
    state = fold(store.read_all())
    assert state.nodes[1].metric_provenance["applied_params"]["checked"] == 4
    assert applied_params_diverged(state.nodes[1].metric_provenance) is True


def test_the_engine_binds_it_at_the_metric_read():
    """AST tier-3 residue, and it is used for exactly what tier 3 can prove: that the bind is wired
    into the eval dispatch at all. What it CANNOT prove is that the call executes — the two
    end-to-end tests above are what hold that, per the ladder."""
    from looplab.engine import eval_dispatch
    src = inspect.getsource(eval_dispatch)
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "bind_applied_params"]
    assert len(calls) == 1, "one bind site, in the eval spec branch"
    kwargs = {k.arg for k in calls[0].keywords}
    assert {"carriers", "applied_config_glob", "since"} <= kwargs


@pytest.mark.parametrize("suffix", [".yaml", ".yml", ".json"])
def test_every_registered_document_suffix_actually_parses(suffix):
    """A suffix in the registry that no extractor handles would make `_carrier_kind` promise a parse
    that `document_numeric_paths` then answers `{}` to — a silent, permanent false clean for every
    file with that extension."""
    text = ('{"a": {"b": 3}}' if suffix == ".json" else "a:\n  b: 3\n")
    paths = param_carriers.document_numeric_paths("f" + suffix, text)
    assert paths[("a", "b")][0] == 3.0
