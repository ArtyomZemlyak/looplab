"""The one metric reader that EXECs runs inside the eval's own environment.

`_read_adapter` `runpy.run_path`s a human-ratified, frozen, agent-written module in a subprocess.
It passed `env=None` to `run_argv`, so that subprocess ran in the ENGINE's environment minus
secret-named vars: no fence marker, no GPU pin, and none of the operator's declared `EvalSpec.env`,
which the task schema promises reaches every stage. `run_argv`'s own comment lists the metric
adapter among what passes through the read fence; it passed through the FUNCTION, not the fence.

ONE PARAMETER BUYS ALL THREE, because `run_argv` derives them from that dict: it reads
`FENCE_DIR_ENV` out of it to prepend the fence directory to `PYTHONPATH`, and
`CUDA_VISIBLE_DEVICES` out of it for the device pin. `None` still means what it always did (build
the default environment), so an eval that declares nothing is byte-identical.

Driven end to end: a real adapter module, a real subprocess, and the value it reports comes from the
environment it was handed. A signature check would pass on a reader that accepted `env` and dropped
it, which is close to the state this fixed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from looplab.runtime.command_eval import METRIC_READERS, read_metric
from looplab.runtime.read_fence import FENCE_DIR_ENV


def _adapter(workdir: Path, body: str, name: str = "LOOPLAB_adapter.py") -> None:
    (workdir / name).write_text(body, encoding="utf-8")


_REPORTS_ENV = """
import os


def read_metric(_workdir):
    return float(os.environ.get("LOOPLAB_TEST_METRIC", "-1"))
"""


def test_the_adapter_sees_the_operators_declared_env(tmp_path):
    """THE DEFECT. MUTATION: pass `env=None` again -> the adapter reads the engine's environment and
    reports -1, while the operator's `EvalSpec.env` promised to reach every stage."""
    _adapter(tmp_path, _REPORTS_ENV)
    spec = {"kind": "adapter"}

    without = read_metric("", str(tmp_path), spec)
    assert without == pytest.approx(-1.0), "no env declared: the historical behaviour"

    with_env = read_metric("", str(tmp_path), spec, env={"LOOPLAB_TEST_METRIC": "0.75"})
    assert with_env == pytest.approx(0.75)


def test_the_declared_env_reaches_it_through_the_public_entry_point(tmp_path):
    """Through `read_metric`, which is what every real call site uses — not by calling the private
    reader directly."""
    _adapter(tmp_path, _REPORTS_ENV)
    assert read_metric("", str(tmp_path), {"kind": "adapter"},
                       env={"LOOPLAB_TEST_METRIC": "2.5"}) == pytest.approx(2.5)


_REPORTS_PYTHONPATH = """
import os


def read_metric(_workdir):
    # 1.0 when the fence directory is first on PYTHONPATH, 0.0 otherwise.
    head = (os.environ.get("PYTHONPATH", "").split(os.pathsep) or [""])[0]
    return 1.0 if head == os.environ.get("LOOPLAB_TEST_EXPECTED_HEAD", "\\x00") else 0.0
"""


def test_the_fence_directory_reaches_the_adapters_pythonpath(tmp_path):
    """`run_argv` prepends `FENCE_DIR_ENV`'s value to `PYTHONPATH`, and it can only see that value
    if the env dict reaches it. This is what makes the read fence cover the adapter at all — it is
    a CPython audit hook installed by a `sitecustomize` on that path.

    MUTATION: pass `env=None` -> the fence directory is nowhere on the child's `PYTHONPATH`, and the
    one reader that execs candidate-lineage code is the one place the fence does not reach.
    """
    fence_dir = tmp_path / "fence"
    fence_dir.mkdir()
    _adapter(tmp_path, _REPORTS_PYTHONPATH)

    reached = read_metric("", str(tmp_path), {"kind": "adapter"}, env={
        FENCE_DIR_ENV: str(fence_dir),
        "LOOPLAB_TEST_EXPECTED_HEAD": str(fence_dir),
    })
    assert reached == pytest.approx(1.0), "the fence directory is not first on PYTHONPATH"


_REPORTS_DEVICES = """
import os


def read_metric(_workdir):
    return float(len([d for d in os.environ.get("CUDA_VISIBLE_DEVICES", "").split(",") if d]))
"""


def test_the_gpu_pin_reaches_it(tmp_path):
    """A metric read that exec's code must not see devices the eval was fenced away from."""
    _adapter(tmp_path, _REPORTS_DEVICES)
    assert read_metric("", str(tmp_path), {"kind": "adapter"},
                       env={"CUDA_VISIBLE_DEVICES": "2"}) == pytest.approx(1.0)


def test_no_env_is_byte_identical_to_the_historical_behaviour(tmp_path):
    """`None` means "build the default environment", exactly as before. An eval that declares
    nothing must not change at all."""
    _adapter(tmp_path, "def read_metric(_w):\n    return 0.5\n")
    assert read_metric("", str(tmp_path), {"kind": "adapter"}) == pytest.approx(0.5)
    assert read_metric("", str(tmp_path), {"kind": "adapter"}, env=None) == pytest.approx(0.5)


def test_a_constraint_reader_gets_it_too(tmp_path):
    """`_violations` reads each constraint through the same dispatch, so an adapter-backed
    constraint is the same exec with the same need. MUTATION: forward the env from `read_metric`
    only -> a constraint's adapter still runs outside the eval's environment, and an unverifiable
    constraint is a VIOLATION, so the node is wrongly excluded from best."""
    from looplab.runtime.command_eval import constraint_violations

    _adapter(tmp_path, _REPORTS_ENV)
    spec = {"name": "c", "kind": "adapter", "max": 1.0}

    # Without the env the adapter reports -1, which satisfies `max: 1.0` — so the property is
    # asserted the other way round: with the env it reports 5.0 and the constraint is violated.
    violated = constraint_violations("", str(tmp_path), [spec], None,
                                     env={"LOOPLAB_TEST_METRIC": "5.0"})
    assert [v["name"] for v in violated] == ["c"]
    assert violated[0]["value"] == pytest.approx(5.0)

    satisfied = constraint_violations("", str(tmp_path), [spec], None,
                                      env={"LOOPLAB_TEST_METRIC": "0.5"})
    assert satisfied == []


def test_the_confinement_guard_still_holds_with_an_env(tmp_path):
    """The env must not become a way around `_confined`: an adapter path that escapes the workdir is
    still refused, whatever environment it would have run in."""
    outside = tmp_path.parent / "escape.py"
    outside.write_text("def read_metric(_w):\n    return 9.0\n", encoding="utf-8")
    assert read_metric("", str(tmp_path), {"kind": "adapter", "path": "../escape.py"},
                       env={"LOOPLAB_TEST_METRIC": "1"}) is None


def test_every_reader_still_answers_without_an_env(tmp_path):
    """The parameter is optional at every entry in the table, so no existing caller breaks."""
    (tmp_path / "m.json").write_text(json.dumps({"metric": 0.25}), encoding="utf-8")
    assert read_metric(json.dumps({"metric": 0.5}), str(tmp_path), {}) == pytest.approx(0.5)
    assert read_metric("M: 0.75", str(tmp_path),
                       {"kind": "stdout_regex", "pattern": r"M: ([0-9.]+)"}) == pytest.approx(0.75)
    assert read_metric("", str(tmp_path),
                       {"kind": "file_json", "path": "m.json", "key": "metric"}) == pytest.approx(0.25)
    assert set(METRIC_READERS) >= {"adapter", "stdout_json", "stdout_regex", "file_json"}
