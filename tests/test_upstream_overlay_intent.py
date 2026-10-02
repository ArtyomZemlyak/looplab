"""Promotions migrate older overlays, never erase later scientific reversions."""
import sys

import pytest

from benchmarks._upstream_sgd import SOURCE
from looplab.events.replay import fold
from looplab.runtime.command_eval import run_command_eval
from tests.test_upstream_lane import fixture
from tests.test_upstream_multibase import create, evaluate, materialize, promote, twice


@pytest.mark.parametrize("basis", ["created", "migrated", "second_base", "between_bases"])
def test_prior_promotion_cannot_absorb_a_later_explicit_reversion(tmp_path, basis):
    if basis in ("second_base", "between_bases"):
        before = (lambda lane, store: create(store, 9, {"train.py": SOURCE})) if basis == "between_bases" else None
        lane, store, _, _, _, advanced = twice(tmp_path, before_second=before)
        if before is None:
            create(store, 9, {"train.py": SOURCE})
    else:
        lane, store, generation, body = fixture(tmp_path)
        if basis == "migrated":
            create(store, 9, {"train.py": SOURCE})
        advanced = promote(lane, generation, body, "promoted")
        if basis == "created":
            create(store, 9, {"train.py": SOURCE})
        else:
            materialize(lane, store, 9)
            assert "train.py" not in fold(store.read_all()).nodes[9].files
            store.append("node_repaired", {"node_id": 9, "generation": 0,
                "files": {"train.py": SOURCE}, "deleted": [], "changed": True,
                "rationale": "Explicitly compare the original hardcoded momentum implementation"})
    work, receipt = materialize(lane, store, 9)
    assert receipt["digest"] == advanced[0]["selector"]["digest"]
    node = fold(store.read_all()).nodes[9]
    if basis == "between_bases":
        # The next capability may merge independently or preserve the whole old
        # base on conflict. It may never re-absorb the deliberate earlier reversal.
        assert "MOMENTUM = 0.2" in (work / "train.py").read_text()
        assert "MOMENTUM = 0.2" in node.files["train.py"]
        assert "recipe.env" not in node.files
    else:
        assert (work / "train.py").read_bytes() == SOURCE.encode()
        assert node.files == {"train.py": SOURCE}
    assert (work / "recipe.env").read_bytes() == (tmp_path / "owner" / "recipe.env").read_bytes()
    # A new materialization and real SGD evaluation must preserve this experiment.
    if basis == "between_bases":
        # A syntactic merge cannot certify semantic compatibility: the new rate
        # reader depends on the settings variable deliberately removed here.
        # This must fail as authored, never become a successful generalized train.
        again, _ = materialize(lane, store, 9)
        assert (again / "train.py").read_bytes() == node.files["train.py"].encode()
        result = run_command_eval([sys.executable, "score.py"], str(again), 10,
            lane.task.eval_spec()["metric"], stages=lane.task.eval_spec()["stages"])
        assert result.exit_code != 0 and result.metric is None and "settings" in result.stderr
    else:
        assert evaluate(lane, store, 9) == fold(store.read_all()).nodes[0].metric
