"""Doc 52 row 17 (doc 51 §6): the repo task exposes the perception hooks (`columns`, `data_samples`),
so on the family every real GPU run uses the grounding pre-phase fires (`data_profiled`,
`RunState.data_profile`), foresight's report is primed, and `DataTools` is no longer blind.
Every read is bounded and touches only the declared `data:` mounts.
"""
from __future__ import annotations

import json
import os

import anyio

from looplab.adapters import perception
from looplab.adapters.repo_task import RepoTask
from looplab.core.models import RunState
from looplab.core.profile import profile_dataset
from looplab.search.foresight import verified_report
from looplab.tools.run_tools import DataTools


def _data(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "README.txt").write_text("about the data\n", encoding="utf-8")
    (raw / "other.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    with (raw / "train.csv").open("w", encoding="utf-8") as f:
        f.write("id,x,y,label,label\n")
        for i in range(10_000):
            f.write(f"{i},{i * 0.5},{'' if i % 4 == 0 else i},{'cat' if i % 2 else 'dog'},{i}\n")
    rows = tmp_path / "rows.jsonl"
    rows.write_text("\n".join(json.dumps({"q": f"query {i}", "n": i}) for i in range(5)) + "\n",
                    encoding="utf-8")
    blob = tmp_path / "model.safetensors"
    blob.write_bytes(b"\x00\x01binary" * 100)
    return raw, rows, blob


def _task(**data):
    return RepoTask(id="t", goal="g", direction="max", editable_path=os.getcwd(),
                    eval={"command": ["true"], "metric": {"kind": "stdout_json", "key": "m"}},
                    data=data)


def test_columns_reads_the_primary_table_of_each_mount_bounded_and_prefixed(tmp_path):
    raw, rows, blob = _data(tmp_path)
    task = _task(raw=str(raw), rows=str(rows), ckpt=str(blob))
    cols = task.columns()
    assert list(cols)[:5] == ["raw:id", "raw:x", "raw:y", "raw:label", "raw:label_2"], \
        "train* wins inside the directory, the header is de-duplicated, keys carry the mount"
    assert len(cols["raw:x"]) == perception.SAMPLE_ROWS, "rows are bounded, not the 10k on disk"
    assert cols["raw:x"][3] == 1.5 and cols["raw:id"][3] == 3, "cells are coerced"
    assert cols["raw:y"][0] == "" and cols["raw:label"][1] == "cat"
    assert cols["rows:q"] == [f"query {i}" for i in range(5)] and cols["rows:n"] == [0, 1, 2, 3, 4]
    assert not any(k.startswith("ckpt:") for k in cols), "a binary mount profiles nothing"
    profile = profile_dataset(cols)
    assert profile["raw:x"]["dtype"] == "numeric" and profile["raw:label"]["dtype"] == "categorical"
    assert profile["raw:y"]["high_missing"] is False and profile["raw:y"]["n_missing"] == 0
    assert "DATA PROFILE" in verified_report(data_profile=profile), "foresight is primed"


def test_columns_caps_tables_and_columns_and_skips_what_it_cannot_read(tmp_path):
    mounts = {}
    for t in range(perception.MAX_TABLES + 2):
        f = tmp_path / f"t{t}.csv"
        f.write_text(",".join(f"c{c}" for c in range(20)) + "\n" + ",".join("1" for _ in range(20)) + "\n",
                     encoding="utf-8")
        mounts[f"m{t}"] = str(f)
    mounts["missing"] = str(tmp_path / "nope")
    big = tmp_path / "big.json"
    big.write_text("[" + ",".join('{"k": 1}' for _ in range(10)) + "]", encoding="utf-8")
    mounts["big"] = str(big)
    task = _task(**mounts)
    cols = task.columns()
    assert len(cols) == perception.MAX_COLUMNS
    assert {k.split(":")[0] for k in cols} == {"m0", "m1", "m2", "m3"}, "MAX_TABLES tables, in order"
    assert perception.tabular_columns(str(big), 5, max_json_bytes=8) == {}, "an oversize .json is skipped"
    assert perception.tabular_columns(str(big), 5) == {"k": [1] * 5}


def test_data_samples_previews_each_mount_and_never_slurps(tmp_path):
    raw, rows, blob = _data(tmp_path)
    samples = _task(raw=str(raw), rows=str(rows), ckpt=str(blob), missing=str(tmp_path / "x")).data_samples()
    assert samples["raw/"].startswith("directory: 3 entries\nREADME.txt\nother.csv\ntrain.csv")
    assert samples["train.csv"].startswith("id,x,y,label,label\n0,0.0,")
    assert len(samples["train.csv"]) <= perception.SAMPLE_CHARS and samples["train.csv"].endswith("\n")
    assert samples["rows.jsonl"].startswith('{"q": "query 0"')
    assert samples["model.safetensors"].startswith("(binary file, ") and "bytes)" in samples["model.safetensors"]
    assert "missing" not in "".join(samples)


def test_a_task_with_no_data_profiles_nothing():
    assert _task().columns() == {} and _task().data_samples() == {}


def test_data_tools_serve_the_repo_task(tmp_path):
    raw, rows, _ = _data(tmp_path)
    task = _task(raw=str(raw), rows=str(rows))
    tools = DataTools(task)
    schema = tools.execute("data_schema", {})
    assert "raw:x" in schema and "rows:q" in schema
    listing = tools.execute("read_asset", {})
    assert "train.csv" in listing and "raw/" in listing
    assert "0,0.0," in tools.execute("read_asset", {"name": "train.csv"})
    state = RunState(goal="g")
    state.data_profile = profile_dataset(task.columns())
    tools.bind_state(state)
    assert "raw:x" in tools.execute("data_profile", {})


def test_the_engine_profiles_a_repo_task_at_setup(tmp_path):
    from tests.test_repo_task import _EditConfigDev, _task as _fixture_task
    from looplab.engine.orchestrator import Engine
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree

    raw, _, _ = _data(tmp_path)
    t = _fixture_task(data={"raw": str(raw)})
    researcher, _ = t.build_roles()
    engine = Engine(tmp_path / "run", task=t, researcher=researcher, developer=_EditConfigDev(),
                    sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1))
    state = anyio.run(engine.run)
    assert state.finished
    rows = [e.data for e in engine.store.read_all() if e.type == "data_profiled"]
    assert len(rows) == 1 and rows[0]["columns"]["raw:x"]["dtype"] == "numeric"
    assert state.data_profile == rows[0]["columns"], "folded, so a resume and foresight see it"
