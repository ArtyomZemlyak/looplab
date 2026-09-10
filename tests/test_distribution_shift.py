"""The deterministic distribution-shift detector (docs/BACKLOG.md §15) and its RECORD.

Nothing in the tree compared the deployment distribution against the training one, so a run could
not tell a shifted input from a worse model. `trust/drift.py` is that comparison and
`engine/audit.py::_record_distribution_shift` appends it as a `data_shift` event. Every test here
DRIVES the statistics or the append — none of them reads production source for a literal.
"""
from __future__ import annotations

import json
import random

import pytest

from looplab.adapters.dataset_task import DatasetTask
from looplab.adapters.perception import split_tables
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import DIAGNOSTIC_EVENTS, EV_DATA_SHIFT
from looplab.trust.drift import (KS_ADVISORY, MIN_ROWS, PSI_ADVISORY, category_shift,
                                 column_shift, distribution_shift, ks_statistic, psi,
                                 rows_to_columns)


def _normal(mu: float, sigma: float, n: int, seed: int) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(mu, sigma) for _ in range(n)]


# --------------------------------------------------------------------------- the statistics
def test_the_same_distribution_reads_as_stable():
    a, b = _normal(0.0, 1.0, 400, 1), _normal(0.0, 1.0, 400, 2)
    assert psi(a, b) < 0.1                       # "no significant change" on the standard scale
    assert ks_statistic(a, b) < KS_ADVISORY
    assert distribution_shift({"x": a}, {"x": b})["shift"] is False


def test_a_shifted_mean_is_caught_by_both_numeric_statistics():
    a, b = _normal(0.0, 1.0, 400, 1), _normal(2.0, 1.0, 400, 2)
    assert psi(a, b) >= PSI_ADVISORY
    assert ks_statistic(a, b) >= KS_ADVISORY
    verdict = distribution_shift({"x": a}, {"x": b}, source="unit")
    assert verdict["shift"] is True and verdict["n_shifted"] == 1
    assert verdict["source"] == "unit" and verdict["checked"] is True


def test_why_both_numeric_statistics_are_reported_measured_not_assumed():
    """The first version of this test asserted that D catches a location shift PSI's bins hide. It
    is FALSE at these settings and the ladder below is what said so: past roughly 0.6σ PSI is the
    LARGER of the two, so PSI is what trips the OR in the ordinary case. What D buys is that it is a
    bounded share — PSI is unbounded and its size depends on the binning — so it stays readable
    exactly where PSI stops being a distance."""
    a = _normal(0.0, 1.0, 400, 1)
    ladder = [(s, psi(a, _normal(s, 1.0, 400, 2)), ks_statistic(a, _normal(s, 1.0, 400, 2)))
              for s in (0.0, 0.5, 1.0, 4.0)]
    assert [p for _, p, _ in ladder] == sorted(p for _, p, _ in ladder)      # both grow with the
    assert [d for _, _, d in ladder] == sorted(d for _, _, d in ladder)      # size of the shift
    at = {s: (p, d) for s, p, d in ladder}
    assert at[0.5][0] < PSI_ADVISORY and at[0.5][1] < KS_ADVISORY            # neither fires
    assert at[1.0][0] >= PSI_ADVISORY and at[1.0][1] >= KS_ADVISORY          # both do
    assert at[4.0][0] > 1.0 and at[4.0][1] <= 1.0                            # unbounded vs a share


def test_psi_bins_come_from_the_reference_alone():
    """The edges must not move with the sample being measured, or a shift partly cancels itself.
    Driven: scoring A against B and B against A over the same pair gives DIFFERENT numbers exactly
    because each run cuts its bins from its own reference."""
    a, b = _normal(0.0, 1.0, 300, 3), _normal(0.0, 3.0, 300, 4)
    assert psi(a, b) != psi(b, a)


def test_a_constant_or_tiny_column_abstains_instead_of_guessing():
    assert psi([1.0] * 50, [1.0] * 50) == 0.0            # a constant column that stayed constant
    tiny = column_shift("x", [1.0, 2.0, 3.0], [9.0, 9.0, 9.0])
    assert tiny["checked"] is False and tiny["shifted"] is False
    assert str(MIN_ROWS) in tiny["reason"]


def test_a_column_that_changed_type_is_a_schema_difference_not_a_shift():
    ref = [float(i) for i in range(MIN_ROWS + 5)]
    cur = [f"cat{i % 3}" for i in range(MIN_ROWS + 5)]
    out = column_shift("x", ref, cur)
    assert out["checked"] is False and out["shifted"] is False and "dtype differs" in out["reason"]


def test_categorical_shift_separates_reweighting_from_a_new_category():
    ref = ["a"] * 60 + ["b"] * 40
    reweighted = ["a"] * 10 + ["b"] * 90
    tvd, unseen = category_shift(ref, reweighted)
    assert tvd == pytest.approx(0.5) and unseen == 0.0
    tvd2, unseen2 = category_shift(ref, ["a"] * 50 + ["c"] * 50)
    assert unseen2 == pytest.approx(0.5)                 # `c` is a schema change, and says so
    assert column_shift("k", ref, ["a"] * 50 + ["c"] * 50)["shifted"] is True
    assert tvd2 > 0.0


def test_an_id_like_categorical_abstains():
    ref = [f"id{i}" for i in range(200)]
    cur = [f"id{i}" for i in range(200, 400)]            # every value unseen, and meaninglessly so
    out = column_shift("id", ref, cur)
    assert out["checked"] is False and out["shifted"] is False and "id-like" in out["reason"]


def test_a_column_present_on_one_side_only_is_reported_not_dropped():
    v = _normal(0.0, 1.0, 100, 5)
    out = distribution_shift({"x": v, "y": v}, {"x": v, "z": v})
    assert out["only_reference"] == ["y"] and out["only_current"] == ["z"]
    assert out["n_columns"] == 1                         # only `x` was comparable


def test_nan_cells_do_not_silently_clear_a_shift():
    """A NaN poisons every statistic (`abs(NaN) >= threshold` is False), which would report a
    shifted column as stable. They are dropped as missing data, the way `core/profile.py` treats
    them, so the shift in the remaining rows still reads."""
    a = _normal(0.0, 1.0, 300, 6)
    b = _normal(3.0, 1.0, 300, 7) + [float("nan")] * 5
    assert column_shift("x", a, b)["shifted"] is True


def test_rows_to_columns_is_positional_and_tolerates_ragged_rows():
    cols = rows_to_columns([[1, 2], [3, 4], [5]])
    assert cols == {"c0": [1, 3, 5], "c1": [2, 4]}
    assert rows_to_columns([{"a": 1}, {"a": 2}]) == {"a": [1, 2]}


# --------------------------------------------------------------------------- the readers + the record
def _write_split(tmp_path, cur_rows):
    d = tmp_path / "data"
    d.mkdir()
    rng = random.Random(11)
    (d / "train.csv").write_text(
        "x,label\n" + "".join(f"{rng.gauss(0.0, 1.0):.4f},a\n" for _ in range(200)),
        encoding="utf-8")
    (d / "test.csv").write_text("x,label\n" + "".join(cur_rows), encoding="utf-8")
    return d


def test_split_tables_pairs_train_with_the_deployment_table(tmp_path):
    rng = random.Random(12)
    d = _write_split(tmp_path, [f"{rng.gauss(0.0, 1.0):.4f},a\n" for _ in range(200)])
    pair = split_tables(str(d))
    assert pair is not None and pair[0].endswith("train.csv") and pair[1].endswith("test.csv")
    # A single-file mount is ONE sample by construction, and a directory that names its splits some
    # other way is not guessed at.
    assert split_tables(str(d / "train.csv")) is None
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "part-1.csv").write_text("x\n1\n", encoding="utf-8")
    assert split_tables(str(tmp_path / "other")) is None


def test_a_dataset_task_publishes_the_pair_its_mount_declares(tmp_path):
    rng = random.Random(13)
    d = _write_split(tmp_path, [f"{rng.gauss(4.0, 1.0):.4f},b\n" for _ in range(200)])
    task = DatasetTask(data_path=str(d), goal="g", direction="max")
    inp = task.shift_inputs()
    assert set(inp["reference"]) == {"x", "label"} and "train.csv vs test.csv" in inp["source"]
    verdict = distribution_shift(inp["reference"], inp["current"], source=inp["source"])
    assert verdict["shift"] is True and verdict["n_shifted"] == 2   # both the number and the label


def test_the_engine_records_the_shift_and_the_fold_ignores_it(tmp_path):
    """The record itself: a real setup append, folded. `data_shift` is diagnostic — it must move no
    `RunState` field — and the row must carry every key the payload contract declares."""
    from looplab.events.types import EVENT_PAYLOAD_KEYS
    from tests.factories import make_engine

    class _Task:
        kind, id, goal, direction = "toy", "t", "g", "min"

        def shift_inputs(self):
            rng = random.Random(14)
            return {"reference": {"x": [rng.gauss(0.0, 1.0) for _ in range(200)]},
                    "current": {"x": [rng.gauss(5.0, 1.0) for _ in range(200)]},
                    "source": "unit"}

    eng = make_engine(tmp_path / "run", task=_Task(), researcher=object(), developer=object())
    eng._record_distribution_shift()
    rows = [e for e in EventStore(tmp_path / "run" / "events.jsonl").read_all()
            if e.type == EV_DATA_SHIFT]
    assert len(rows) == 1
    payload = rows[0].data
    assert payload["shift"] is True and payload["source"] == "unit"
    assert set(EVENT_PAYLOAD_KEYS["data_shift"].required) <= set(payload)
    assert EV_DATA_SHIFT in DIAGNOSTIC_EVENTS
    before = json.dumps(fold([]).model_dump(mode="json"), sort_keys=True, default=str)
    after = json.dumps(fold(rows).model_dump(mode="json"), sort_keys=True, default=str)
    assert before == after                          # the fold does not read it, and cannot


def test_a_task_that_declares_no_pair_records_nothing(tmp_path):
    """"Not compared" and "compared, no shift" are different facts; only the second is evidence."""
    from tests.factories import make_engine

    class _Task:
        kind, id, goal, direction = "toy", "t", "g", "min"

        def shift_inputs(self):
            return {}

    eng = make_engine(tmp_path / "run", task=_Task(), researcher=object(), developer=object())
    eng._record_distribution_shift()
    assert not [e for e in EventStore(tmp_path / "run" / "events.jsonl").read_all()
                if e.type == EV_DATA_SHIFT]


def test_the_leakage_split_is_the_fallback_source(tmp_path):
    """Most adapters publish no table pair, but the ones with an in-memory split already publish
    `train_rows`/`test_rows` for the leakage gate — compared positionally, no new hook needed."""
    from tests.factories import make_engine

    class _Task:
        kind, id, goal, direction = "toy", "t", "g", "min"

        def leakage_inputs(self):
            rng = random.Random(15)
            return {"train_rows": [[rng.gauss(0.0, 1.0), 1.0] for _ in range(200)],
                    "test_rows": [[rng.gauss(6.0, 1.0), 1.0] for _ in range(200)]}

    eng = make_engine(tmp_path / "run", task=_Task(), researcher=object(), developer=object())
    eng._record_distribution_shift()
    rows = [e for e in EventStore(tmp_path / "run" / "events.jsonl").read_all()
            if e.type == EV_DATA_SHIFT]
    assert len(rows) == 1 and rows[0].data["shift"] is True
    assert "leakage_inputs" in rows[0].data["source"]
    shifted = [c for c in rows[0].data["columns"] if c.get("shifted")]
    assert [c["column"] for c in shifted] == ["c0"]     # the constant second column is not "shift"
