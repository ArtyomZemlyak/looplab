"""The owner's check before `Settings.landlock` defaults to `enforce` (BACKLOG #7 residue): the
GPU smoke runs every surface a GPU eval touches under the run's derived allow-list. On a box (or an
interpreter) without torch/CUDA those steps are SKIPPED with a reason — never reported `ok` — and
the filesystem steps still prove the ruleset applies; a refused surface fails the check."""
from __future__ import annotations

import json
import sys

import pytest

from looplab.runtime import landlock, read_allowlist
from looplab.engine.landlock_smoke import STEPS, run_smoke

_NO_LANDLOCK = landlock.unavailable_reason()


@pytest.mark.skipif(_NO_LANDLOCK is not None, reason=str(_NO_LANDLOCK))
def test_the_smoke_reports_every_surface_and_never_calls_a_skip_ok(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "x.csv").write_text("a\n")
    allow = read_allowlist.derive(workdir=None, run_dir=str(tmp_path),
                                  repo_spec={"data": {"d": {"path": str(data)}}})
    env = {k: v for k, v in __import__("os").environ.items()}
    env["CUDA_VISIBLE_DEVICES"] = ""                       # never touch a GPU from the suite
    out = run_smoke(allow, python=sys.executable, mounts=[str(data)], env=env)
    assert set(out["steps"]) == set(STEPS), out
    assert out["steps"]["workdir_write"]["status"] == "ok"
    assert out["steps"]["mount_reads"]["status"] == "ok"
    assert out["steps"]["cuda_matmul"]["status"] in ("skipped", "ok")
    if out["steps"]["import_torch"]["status"] == "skipped":
        assert all(out["steps"][s]["status"] == "skipped"
                   for s in ("cuda_matmul", "dataloader_shm", "distributed_init"))
    assert out["ok"] is True, json.dumps(out)[:2000]


@pytest.mark.skipif(_NO_LANDLOCK is not None, reason=str(_NO_LANDLOCK))
def test_a_mount_the_ruleset_omits_fails_the_smoke(tmp_path):
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    (hidden / "x.csv").write_text("a\n")
    (tmp_path / "run").mkdir()
    allow = [(p, m) for p, m in read_allowlist.derive(workdir=None, run_dir=str(tmp_path / "run"))
             if not (str(hidden) + "/").startswith(p.rstrip("/") + "/")]
    out = run_smoke(allow, python=sys.executable, mounts=[str(hidden)],
                    env={**__import__("os").environ, "CUDA_VISIBLE_DEVICES": ""})
    assert out["steps"]["mount_reads"]["status"] == "failed" and out["ok"] is False
