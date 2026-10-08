"""A candidate that fills the disk is the CANDIDATE's failure, not the box's (review 2026-10-08).

Driven by the finder's repro: a candidate wrote checkpoints until ENOSPC; the probe after that failure
found the run directory full, paused the run and withheld the attempt; the resume's fresh workdir
freed the space and the same code filled it again — a pause loop with the node pending forever and
the Developer never told. `infra_probe.candidate_may_have_caused` is the rule: a failed attempt whose
box check finds ONLY the run directory full takes the ordinary repair path, with the probe's sentence
as evidence; any other fault beside it keeps the whole answer the box's.
"""
from __future__ import annotations

import errno
import shutil

import anyio
import pytest

from factories import make_engine
from looplab.events.replay import fold
from looplab.runtime import infra_probe
from looplab.runtime.command_eval import RunResult
from looplab.runtime.infra_probe import InfraFault, candidate_may_have_caused


@pytest.mark.parametrize("faults, blamed", [
    ([InfraFault("run_dir", "/r", "ENOSPC")], True),
    ([InfraFault("run_dir", "/r", "EDQUOT")], True),
    ([], False),                                                       # nothing to attribute
    ([InfraFault("run_dir", "/r", "EROFS")], False),                   # a remount, not a write
    ([InfraFault("run_dir", "/r", "ENOTCONN")], False),
    ([InfraFault("run_dir", "/r", "timeout")], False),
    ([InfraFault("mount", "/d", "ENOSPC")], False),                    # only run_dir is written
    ([InfraFault("run_dir", "/r", "ENOSPC"), InfraFault("mount", "/d", "ENOENT")], False),
    ([InfraFault("run_dir", "/r", "ENOSPC"), InfraFault("interpreter", "/py", "missing")], False),
])
def test_only_a_full_run_dir_alone_may_be_the_candidate_s_doing(faults, blamed):
    assert candidate_may_have_caused(faults) is blamed


class _Dev:
    last_files: dict = {}
    last_deleted: list = []

    def __init__(self):
        self.errors: list = []

    def repair(self, idea, code, err):
        self.errors.append(err)
        return code + "\n# save fewer checkpoints\n"


def _disk_filling_engine(tmp_path, monkeypatch, *, mount=None):
    disk = {"full": False}
    real_touch = infra_probe._touch_writable

    def touch(path):
        if disk["full"]:
            raise OSError(errno.ENOSPC, "No space left on device", path)
        return real_touch(path)

    monkeypatch.setattr(infra_probe, "_touch_writable", touch)
    dev = _Dev()
    eng = make_engine(tmp_path / "run", developer=dev)
    eng._inline_repair = True
    eng._inline_repair_attempts = 2
    eng._inline_repair_reasons = ("crash",)
    if mount is not None:
        eng._repo_spec = {"data": {"corpus": {"path": str(mount), "mount": True}}}
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": "r"},
        "code": "save 500 checkpoints"})
    real_prep = eng._eval_prepare_workdir

    def prep(a):
        disk["full"] = False            # re-materializing the workdir frees the candidate's files
        return real_prep(a)

    monkeypatch.setattr(eng, "_eval_prepare_workdir", prep)
    runs: list = []
    triage_facts: list = []

    def fake_run_eval(node, workdir, env=None, profile=None, cancel=None, start_stage=None):
        runs.append(node.code)
        disk["full"] = True             # the CANDIDATE's checkpoints filled the disk
        if mount is not None:
            shutil.rmtree(mount, ignore_errors=True)
        return RunResult(exit_code=1, stdout="", metric=None, timed_out=False,
                         stderr="OSError: [Errno 28] No space left on device")

    def fake_triage(*args, **kwargs):
        triage_facts.append(kwargs.get("engine_facts") or "")
        return {"action": "repair", "rationale": "save fewer", "failure_kind": "crash"}

    eng._run_eval = fake_run_eval
    eng._triage_crash = fake_triage
    return eng, dev, runs, triage_facts


def test_a_candidate_that_fills_the_disk_is_triaged_and_repaired(tmp_path, monkeypatch):
    eng, dev, runs, triage_facts = _disk_filling_engine(tmp_path, monkeypatch)
    anyio.run(eng._evaluate, 0, anyio.CapacityLimiter(1), None)
    events = eng.store.read_all()
    assert len(dev.errors) == 1, "the Developer was never told the eval filled the disk"
    assert "run directory" in dev.errors[0] and "ENOSPC" in dev.errors[0], dev.errors[0]
    assert triage_facts and "ENOSPC" in triage_facts[0], triage_facts
    withheld = [(e.data["at"], e.data["reason"]) for e in events
                if e.type == "eval_attempt_withheld"]
    assert ("decide_repair", "infra_unavailable") not in withheld, withheld
    # The repaired attempt's launch still meets a full disk (the first attempt's files are in the
    # reused workdir) — that probe, before anything of the attempt ran, is the box's, and pauses.
    assert withheld == [("before_launch", "infra_unavailable")], withheld
    assert len([e for e in events if e.type == "node_repaired"]) == 1


def test_the_disk_fill_loop_ends_in_a_terminal(tmp_path, monkeypatch):
    """The finder's loop, three resumes long: before the fix every cycle paused at DECIDE_REPAIR,
    the Developer was asked nothing and the node stayed pending forever."""
    eng, dev, runs, _facts = _disk_filling_engine(tmp_path, monkeypatch)
    for _cycle in range(4):
        anyio.run(eng._evaluate, 0, anyio.CapacityLimiter(1), None)
        if fold(eng.store.read_all()).nodes[0].status.value != "pending":
            break
        eng.store.append("resume", {})
    st = fold(eng.store.read_all())
    assert st.nodes[0].status.value == "failed", (st.nodes[0].status, len(runs), len(dev.errors))
    assert len(dev.errors) == 2, "the repair budget (2) bounded the chain"


def test_a_full_disk_beside_a_dead_mount_is_still_the_box_s(tmp_path, monkeypatch):
    """The control: "a dead mount never blames the candidate" holds whatever else is wrong."""
    mount = tmp_path / "mnt" / "corpus"
    mount.mkdir(parents=True)
    eng, dev, runs, triage_facts = _disk_filling_engine(tmp_path, monkeypatch, mount=mount)
    anyio.run(eng._evaluate, 0, anyio.CapacityLimiter(1), None)
    events = eng.store.read_all()
    st = fold(events)
    assert not dev.errors and not triage_facts
    assert st.paused and st.pause_reason == "infra_unavailable"
    assert st.nodes[0].status.value == "pending"
    assert [(e.data["at"], e.data["reason"]) for e in events
            if e.type == "eval_attempt_withheld"] == [("decide_repair", "infra_unavailable")]
