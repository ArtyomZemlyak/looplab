"""The coordinates say WHAT PIPELINE they are claimed about — the merge-node blind spot.

`bind_applied_params` answers "what did the configuration say your declared `Idea.params` were
worth", and every reader takes that to mean the number was PRODUCED at those coordinates. That
inference needs a pipeline that could have consumed them, and nothing on the record said what the
pipeline was.

MEASURED over every `events.jsonl` on this box: of the TWELVE `node_evaluated` rows carrying an
`applied_params` record, **FOUR ran no training stage at all** — `e5small-dr-unified-v4` nodes 7,
11 and 13 and `e5small-dr-unified-v10` node 3, each a `merge` + `score` pipeline that averages two
parents' weights and scores the average. Their declared params are `search/operators.py::merge_idea`'s
arithmetic mean of the parents' declarations, their workdir still carries the committed
`config.yaml`, and the rung dutifully reports divergences on `batch_size` / `learning_rate` /
`n_epochs` for a node that ran zero epochs at no batch size.

AND IT REACHES A CHAMPION: v4 node 13 is 0.793411, the second-best number on this box, and
`champion_metric_caveats` raises `params_overridden` on it citing `config.yaml:265`'s 2048 against a
declared 4096. The conclusion is arguably right for a merge node and the evidence is spurious.

This change RECORDS and does not DECIDE — no caveat moves, nothing is gated. Every assertion below
has an input that makes it FAIL; the mutations are named in the messages.
"""
from __future__ import annotations

import json

import pytest

from looplab.runtime.applied_params import bind_applied_params

_CONFIG = """train:
  training:
    batch_size: 2048
    n_epochs: 1
"""


@pytest.fixture()
def workdir(tmp_path):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    return tmp_path


_DECLARED = {"train.training.batch_size": 4096.0, "train.training.n_epochs": 3.0}
_CARRIERS = ("configs/config.yaml",)


def test_the_record_names_the_pipeline_it_is_about(workdir):
    """The defect: a merge node's record reported divergences with nothing saying the pipeline
    trained nothing. Mutation: drop the `stages` write and the four merge-node records on this box
    become indistinguishable from a real training node's."""
    record = bind_applied_params(_DECLARED, str(workdir), carriers=_CARRIERS,
                                 pipeline_stages=["merge", "score"])
    assert record is not None
    assert record["stages"] == ["merge", "score"], (
        "the record must name the pipeline: without it a reader cannot tell a divergence a "
        "training run produced from one about a node that trained nothing")
    assert [d["param"] for d in record["diverged"]] == [
        "train.training.batch_size", "train.training.n_epochs"], (
        "and the divergences are UNCHANGED — this change records, it does not decide; suppressing "
        "them here would be a selection decision made without the measurement it needs")


def test_the_pipeline_is_recorded_in_the_engine_s_own_order(workdir):
    """Mutation: sort the names, and `mine -> train -> score` reads as `mine -> score -> train`,
    which is a different pipeline. Order is what makes a stage list readable as a pipeline."""
    record = bind_applied_params(_DECLARED, str(workdir), carriers=_CARRIERS,
                                 pipeline_stages=["mine", "train", "score"])
    assert record["stages"] == ["mine", "train", "score"]


def test_a_record_with_no_pipeline_carries_no_stages_key(workdir):
    """ABSENT, never an empty list. Invariant #5: every log written before today has no `stages`,
    and a reader must default that to silence rather than to "this node ran no stages" — which is
    exactly the false claim the key exists to prevent. Mutation: write `[]` unconditionally and
    every preserved run gains a record asserting it ran nothing."""
    record = bind_applied_params(_DECLARED, str(workdir), carriers=_CARRIERS)
    assert record is not None
    assert "stages" not in record, (
        "an un-passed pipeline is UNKNOWN, and unknown is said by absence — the same rule "
        "`checked` follows one field up")


def test_blank_and_missing_stage_names_are_dropped_not_rendered(workdir):
    """A single-command eval has one stage entry whose `name` is None, and `[None]` would render as
    a pipeline of one anonymous stage. Mutation: keep the falsy entries and the record claims a
    stage nobody can name."""
    record = bind_applied_params(_DECLARED, str(workdir), carriers=_CARRIERS,
                                 pipeline_stages=[None, "", "  ", "score"])
    assert record["stages"] == ["score"]


def test_the_record_is_json_serializable_with_the_new_key(workdir):
    """It rides on a durable `node_evaluated` row, so it must survive the event store's dump.
    Mutation: put the raw stage MAPPINGS on the record instead of their names and this raises."""
    record = bind_applied_params(_DECLARED, str(workdir), carriers=_CARRIERS,
                                 pipeline_stages=["merge", "score"])
    assert json.loads(json.dumps(record))["stages"] == ["merge", "score"]
