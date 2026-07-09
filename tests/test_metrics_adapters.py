"""Node metric-series reader (UI observability). The key regression: a repair-triggered retrain (or
any re-run) in the SAME node workdir writes a NEW PyTorch-Lightning `version_N` dir and leaves the old
ones on disk. The reader must show the NEWEST run's curve, not interleave every version's scalars (both
start at step 0) into a stale-looking zigzag."""
from __future__ import annotations

import pytest

torch_tb = pytest.importorskip("torch.utils.tensorboard")
pytest.importorskip("tensorboard")

from looplab.serve.metrics_adapters import read_node_metrics  # noqa: E402


def _write_run(dirpath, values):
    dirpath.mkdir(parents=True, exist_ok=True)
    w = torch_tb.SummaryWriter(str(dirpath))
    for step, v in enumerate(values):
        w.add_scalar("loss", v, step)
    w.close()


def test_retrain_shows_newest_version_not_the_interleaved_merge(tmp_path):
    # Two successive training runs of the SAME node: version_0 (stale) then version_1 (the retrain),
    # both logging `loss` at steps 0..2. The reader must return ONLY version_1's three points.
    base = tmp_path / "models" / "cross_batch"
    _write_run(base / "version_0", [10.0, 9.0, 8.0])   # stale run
    _write_run(base / "version_1", [5.0, 4.0, 3.0])    # the retrain (newest)
    m = read_node_metrics(str(tmp_path))
    assert "loss" in m
    steps = [p["step"] for p in m["loss"]]
    assert steps == [0, 1, 2]                          # newest run only — NOT 0,0,1,1,2,2 interleaved
    assert [p["value"] for p in m["loss"]] == [5.0, 4.0, 3.0]   # version_1's values, not version_0's


def test_distinct_non_version_dirs_all_survive(tmp_path):
    # Version-collapse must NOT eat genuinely separate logdirs (e.g. train/ vs val/) — those are
    # different purposes, not re-runs, so every tag from both must appear.
    _write_run(tmp_path / "logs" / "train", [1.0, 2.0])
    _write_run(tmp_path / "logs" / "val", [0.5, 0.6])
    m = read_node_metrics(str(tmp_path))
    # both dirs use the tag "loss"; different dirs → their points merge under the one tag (6 total…
    # here 4), the point is neither dir was dropped as a "version".
    assert len(m.get("loss", [])) == 4
