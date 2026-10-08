"""A FULL run directory is ONE rule at all three sites (review 2026-10-08, round 3).

The run directory is the one probed target a candidate can break by itself — its workdir lives under
it — and it is SHARED. Before this rule the three sites that read it disagreed:

* the CONTAINMENT of an engine-side OSError paused with no terminal, so a candidate that filled the
  disk looped pause → resume (the fresh workdir frees the space) → refill, forever;
* the probe AFTER a failed attempt blamed every node that met the full disk, so with
  `max_parallel > 1` the bystanders that died ENOSPC bought a triage told "your writes may have
  filled it";
* the probe BEFORE a repaired attempt's launch paused the whole run as a box fault over the files
  the same lifecycle had itself just been blamed for.

`runtime/infra_probe.py::run_dir_full_blames` is the rule: a full run dir is the box's (pause, node
pending, the withheld row records `fault: run_dir_full`, the pause names the largest workdir) unless
the evidence ties it to the lifecycle — its workdir is the strictly largest AND either dominant on
the filesystem or this lifecycle already met a full run dir (a durable row, so a resume keeps it).
Every engine test here drives the real `_evaluate` chain; the disk is a patched probe write.
"""
from __future__ import annotations

import errno
import os
import shutil

import anyio
import pytest

from _posix_gates import POSIX_ONLY_OS_CALLS
from factories import make_engine
from looplab.events.replay import fold
from looplab.runtime import infra_probe
from looplab.runtime.command_eval import RunResult
from looplab.runtime.infra_probe import (FillBaseline, InfraFault, WorkdirUsage,
                                         candidate_may_have_caused, run_dir_full_blames)

_HUGE_FS = 10 ** 15        # bytes in use on the (patched) filesystem: no node workdir dominates it
_CKPT = 64 * 1024          # what the candidate's checkpoints write into its own workdir per attempt


@pytest.mark.parametrize("faults, nominated", [
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
def test_only_a_full_run_dir_alone_is_nominated(faults, nominated):
    assert candidate_may_have_caused(faults) is nominated


@pytest.mark.parametrize("usage, fs_used, met_before, baseline, blamed", [
    ([WorkdirUsage(0, 900)], 1000, False, None, True),                 # dominant on its own
    ([WorkdirUsage(0, 400)], 1000, False, None, False),                # largest, not dominant…
    ([WorkdirUsage(0, 400)], 1000, True, FillBaseline(0, 700), True),  # …until it fills it again
    ([WorkdirUsage(0, 400)], 1000, True, FillBaseline(0, 1200), True),  # net shrink, own growth
    # Review 2026-10-08: the second meeting alone is no evidence — something OUTSIDE the node
    # workdirs (another run, a cache) filled it, or nothing measured the "before":
    ([WorkdirUsage(0, 400)], 1000, True, FillBaseline(0, 100), False),  # fs gained 900, node 400
    ([WorkdirUsage(0, 400)], 1000, True, FillBaseline(400, 700), False),  # the node did not grow
    ([WorkdirUsage(0, 400)], 1000, True, None, False),                 # no baseline: the box's
    ([WorkdirUsage(0, 400)], None, True, FillBaseline(0, 700), False),  # no statvfs: the box's
    ([WorkdirUsage(0, 400)], 1000, True, FillBaseline(None, 700), False),  # an unmeasured before
    ([WorkdirUsage(0, 400)], 1000, False, FillBaseline(0, 700), False),  # a first meeting
    ([WorkdirUsage(0, 400), WorkdirUsage(1, 500)], 1000, True, FillBaseline(0, 700), False),
    ([WorkdirUsage(0, 500), WorkdirUsage(1, 500)], 1000, True, FillBaseline(0, 700), False),  # tie
    ([WorkdirUsage(0, 0)], 1, True, FillBaseline(0, 0), False),        # an empty workdir
    ([WorkdirUsage(1, 900)], 1000, True, FillBaseline(0, 700), False),  # never measured
])
def test_only_evidence_blames_a_lifecycle(usage, fs_used, met_before, baseline, blamed):
    assert run_dir_full_blames(0, usage, fs_used=fs_used, met_before=met_before,
                               baseline=baseline) is blamed


@POSIX_ONLY_OS_CALLS
def test_the_measurement_is_bounded_and_never_follows_a_link_or_opens_a_fifo(tmp_path):
    nodes = tmp_path / "nodes"
    big, small = nodes / "node_1", nodes / "node_0"
    big.mkdir(parents=True)
    small.mkdir()
    (big / "ckpt.bin").write_bytes(b"x" * 300_000)
    (small / "solution.py").write_text("print(1)\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "huge.bin").write_bytes(b"y" * 900_000)
    (small / "link").symlink_to(outside, target_is_directory=True)  # counted as a link, not 900 kB
    os.mkfifo(small / "pipe")                                        # lstat only: never opened
    (nodes / "not_a_node").mkdir()
    usage = {u.node_id: u for u in infra_probe.node_workdir_usage(nodes)}
    assert set(usage) == {0, 1}
    assert usage[1].bytes >= 300_000 and usage[0].bytes < 100_000, usage
    assert infra_probe.largest_workdir(usage.values()).node_id == 1
    for i in range(150):                       # past the per-workdir floor of 64 entries
        (big / f"f{i}").write_text("z")
    cut = {u.node_id: u for u in infra_probe.node_workdir_usage(nodes, max_entries=10)}
    assert cut[1].complete is False, "a walk past its entry budget says it is a lower bound"
    assert "at least" in infra_probe.occupancy_sentence(cut.values())
    assert infra_probe.node_workdir_usage(tmp_path / "missing") == []


# ------------------------------------------------------------------ driven through `_evaluate`

class _Dev:
    last_files: dict = {}
    last_deleted: list = []

    def __init__(self):
        self.errors: list = []

    def repair(self, idea, code, err):
        self.errors.append(err)
        return code + "\n# save fewer checkpoints\n"


def _node(store, node_id: int, code: str) -> None:
    store.append("node_created", {
        "node_id": node_id, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": float(node_id)}, "rationale": "r"},
        "code": code})


def _disk_filling_engine(tmp_path, monkeypatch, *, mount=None, fs_used=_HUGE_FS, fill=True,
                         raise_in_engine=False, external=0):
    """One node whose eval fills the disk (`disk["full"]`) and dies; re-materializing its workdir
    (PREPARE_WORKDIR, which every lifecycle and the pre-launch re-materialization run) frees it."""
    # `written`: what the candidate's checkpoints add to the filesystem's bytes in use, and
    # `external`: what something OUTSIDE the node workdirs (another run, a cache) adds as it fills.
    disk = {"full": False, "written": 0, "external": 0}
    real_touch = infra_probe._touch_writable

    def touch(path):
        if disk["full"]:
            raise OSError(errno.ENOSPC, "No space left on device", path)
        return real_touch(path)

    monkeypatch.setattr(infra_probe, "_touch_writable", touch)
    monkeypatch.setattr(infra_probe, "filesystem_used_bytes",
                        lambda path: fs_used + disk["written"] + disk["external"])
    dev = _Dev()
    eng = make_engine(tmp_path / "run", developer=dev)
    eng._inline_repair = True
    eng._inline_repair_attempts = 2
    eng._inline_repair_reasons = ("crash",)
    if mount is not None:
        eng._repo_spec = {"data": {"corpus": {"path": str(mount), "mount": True}}}
        # The box has been seen WORKING (`engine/evaluate.py::box_seen_working`): a stage ran `ok`
        # on it before, so a declared mount that no longer exists WENT AWAY.
        eng.store.append("stage_finished", {"node_id": 9, "name": "prep", "status": "ok"})
    _node(eng.store, 0, "save 500 checkpoints")
    real_prep = eng._eval_prepare_workdir
    preps: list = []

    def prep(a):
        preps.append(a.attempt)
        disk["full"] = False            # re-materializing the workdir frees the candidate's files
        disk["written"] = 0
        return real_prep(a)

    monkeypatch.setattr(eng, "_eval_prepare_workdir", prep)
    runs: list = []
    triage_facts: list = []

    def fake_run_eval(node, workdir, env=None, profile=None, cancel=None, start_stage=None):
        runs.append(node.code)
        if fill:
            disk["full"] = True         # the CANDIDATE's checkpoints filled the disk
            with open(os.path.join(workdir, "ckpt.bin"), "wb") as fh:
                fh.write(b"\1" * _CKPT)
            disk["written"] += _CKPT
        if external:
            disk["full"] = True         # …or something outside the node workdirs did
            disk["external"] += external
        if mount is not None:
            shutil.rmtree(mount, ignore_errors=True)
        if raise_in_engine:
            # the ENGINE's own write after the candidate filled the disk (a sidecar, a receipt)
            raise OSError(errno.ENOSPC, "No space left on device", str(workdir))
        return RunResult(exit_code=1, stdout="", metric=None, timed_out=False,
                         stderr="OSError: [Errno 28] No space left on device")

    def fake_triage(*args, **kwargs):
        triage_facts.append(kwargs.get("engine_facts") or "")
        return {"action": "repair", "rationale": "save fewer", "failure_kind": "crash"}

    eng._run_eval = fake_run_eval
    eng._triage_crash = fake_triage
    return eng, dev, runs, triage_facts, preps, disk


def _withheld(events):
    return [(e.data["at"], e.data["reason"], e.data.get("fault")) for e in events
            if e.type == "eval_attempt_withheld"]


def test_the_first_full_disk_is_the_box_s_and_names_whose_workdir_holds_it(tmp_path, monkeypatch):
    eng, dev, runs, triage_facts, _preps, _disk = _disk_filling_engine(tmp_path, monkeypatch)
    anyio.run(eng._evaluate, 0, anyio.CapacityLimiter(1), None)
    events = eng.store.read_all()
    st = fold(events)
    assert not dev.errors and not triage_facts, "a first meeting with no evidence blamed the node"
    assert st.paused and st.pause_reason == "infra_unavailable"
    assert st.nodes[0].status.value == "pending"
    assert _withheld(events) == [("decide_repair", "infra_unavailable", "run_dir_full")]
    pause = [e.data for e in events if e.type == "pause"][-1]
    assert "node_0's workdir holds" in pause["detail"], pause["detail"]
    assert pause["fault"] == "run_dir_full" and pause["occupant"]["node_id"] == 0, pause


def test_the_same_lifecycle_filling_it_again_is_repaired_and_never_paused_for_its_own_files(
        tmp_path, monkeypatch):
    """The finder's loop and the contradiction, together: before the fix the resume refilled the disk
    forever (or, blamed, the repaired attempt's launch paused the run over the files that same
    lifecycle had just written). Now the second meeting blames it, the repaired launches re-materialize
    its own workdir, and the chain ends in a terminal inside the repair budget."""
    eng, dev, runs, triage_facts, preps, _disk = _disk_filling_engine(tmp_path, monkeypatch)
    for _cycle in range(4):
        anyio.run(eng._evaluate, 0, anyio.CapacityLimiter(1), None)
        if fold(eng.store.read_all()).nodes[0].status.value != "pending":
            break
        eng.store.append("resume", {})
    events = eng.store.read_all()
    st = fold(events)
    assert st.nodes[0].status.value == "failed", (st.nodes[0].status, len(runs), len(dev.errors))
    assert _cycle == 1, "one box pause per lifecycle, then the evidence"
    assert len(dev.errors) == 2, "the repair budget (2) bounded the chain"
    assert "evidence ties it to this node" in dev.errors[0], dev.errors[0]
    assert "node_0's workdir holds" in dev.errors[0], dev.errors[0]
    assert triage_facts and "already filled it once" in triage_facts[0], triage_facts
    assert _withheld(events) == [("decide_repair", "infra_unavailable", "run_dir_full")], (
        "a repaired launch paused the run over its own lifecycle's files")
    assert len([e for e in events if e.type == "pause"]) == 1
    # 2 lifecycles' PREPARE_WORKDIR + one re-materialization before each repaired launch
    assert len(preps) == 4, preps


def test_a_dominant_workdir_is_blamed_at_its_first_meeting(tmp_path, monkeypatch):
    eng, dev, runs, triage_facts, _preps, _disk = _disk_filling_engine(tmp_path, monkeypatch,
                                                                       fs_used=1)
    anyio.run(eng._evaluate, 0, anyio.CapacityLimiter(1), None)
    events = eng.store.read_all()
    st = fold(events)
    assert not st.paused and st.nodes[0].status.value == "failed"
    assert len(dev.errors) == 2 and "at least half" in dev.errors[0], dev.errors
    assert _withheld(events) == []


def test_a_full_disk_beside_a_dead_mount_is_still_the_box_s(tmp_path, monkeypatch):
    """The control: "a dead mount never blames the candidate" holds whatever else is wrong."""
    mount = tmp_path / "mnt" / "corpus"
    mount.mkdir(parents=True)
    eng, dev, runs, triage_facts, _p, _d = _disk_filling_engine(tmp_path, monkeypatch, mount=mount,
                                                                fs_used=1)
    anyio.run(eng._evaluate, 0, anyio.CapacityLimiter(1), None)
    events = eng.store.read_all()
    st = fold(events)
    assert not dev.errors and not triage_facts
    assert st.paused and st.pause_reason == "infra_unavailable"
    assert st.nodes[0].status.value == "pending"
    assert _withheld(events) == [("decide_repair", "infra_unavailable", None)]


def _with_a_filler_sibling(eng, *, nbytes: int = 2_000_000) -> None:
    """Node 1's workdir holds what fills the disk; node 0 (the one evaluated) is a bystander."""
    _node(eng.store, 1, "write 400 GB of checkpoints")
    filler = eng.run_dir / "nodes" / "node_1"
    filler.mkdir(parents=True, exist_ok=True)
    (filler / "ckpt.bin").write_bytes(b"\0" * nbytes)


def test_a_bystander_is_never_triaged_over_a_sibling_s_full_disk(tmp_path, monkeypatch):
    """`max_parallel > 1`: node 1 filled the shared run dir and node 0 died ENOSPC. Node 0 meets the
    full disk twice — after the resume too — and is never blamed: its workdir is not the largest."""
    eng, dev, runs, triage_facts, _p, _d = _disk_filling_engine(tmp_path, monkeypatch, fs_used=1)
    _with_a_filler_sibling(eng)
    for _cycle in range(2):
        anyio.run(eng._evaluate, 0, anyio.CapacityLimiter(1), None)
        eng.store.append("resume", {})
    events = eng.store.read_all()
    assert not dev.errors and not triage_facts, "a bystander went to triage over a full disk"
    assert fold(events).nodes[0].status.value == "pending"
    assert _withheld(events) == [("decide_repair", "infra_unavailable", "run_dir_full")] * 2
    pause = [e.data for e in events if e.type == "pause"][-1]
    assert pause["occupant"]["node_id"] == 1 and "node_1's workdir holds" in pause["detail"], pause


def test_the_filler_meeting_a_run_its_bystander_paused_settles_instead_of_looping(
        tmp_path, monkeypatch):
    """The race that kept the loop alive: the bystander pauses first, so the filler's DECIDE_REPAIR
    always met a paused run and was withheld unasked. Now it is asked: the evidence ties the disk to
    it (largest, and it met the full disk before), and a paused run buys no repair, so it SETTLES."""
    eng, dev, runs, triage_facts, _p, _d = _disk_filling_engine(tmp_path, monkeypatch)
    eng.store.append("eval_attempt_withheld", {
        "node_id": 0, "generation": 0, "attempt": 0, "at": "decide_repair",
        "reason": "infra_unavailable", "eval_seconds": 1.0, "fault": "run_dir_full"})
    real_run = eng._run_eval

    def run_then_bystander_pauses(*args, **kwargs):
        res = real_run(*args, **kwargs)
        eng.store.append("pause", {"reason": "infra_unavailable", "detail": "node 5 met it first"})
        return res

    eng._run_eval = run_then_bystander_pauses
    anyio.run(eng._evaluate, 0, anyio.CapacityLimiter(1), None)
    events = eng.store.read_all()
    st = fold(events)
    assert st.nodes[0].status.value == "failed", st.nodes[0].status
    assert not dev.errors and not triage_facts, "a paused run bought a repair"
    term = [e.data for e in events if e.type == "node_failed"][-1]
    assert "already filled it once" in term["triage_rationale"], term


# ------------------------------------------------------------------ the containment, same rule

def test_an_engine_write_failing_on_a_disk_the_candidate_filled_ends_in_a_terminal(
        tmp_path, monkeypatch):
    """The critic's repro (`_contain_eval_crash`): the candidate filled the disk and the ENGINE's own
    write raised ENOSPC. Before the fix every cycle paused with no terminal and the resume freed the
    space for the next refill — the node pending forever."""
    eng, dev, runs, _facts, _p, _d = _disk_filling_engine(tmp_path, monkeypatch,
                                                          raise_in_engine=True)
    for _cycle in range(4):
        anyio.run(eng._evaluate, 0, anyio.CapacityLimiter(1), None)
        st = fold(eng.store.read_all())
        if st.nodes[0].status.value != "pending":
            break
        assert st.paused and st.pause_reason == "infra_unavailable", st.pause_reason
        eng.store.append("resume", {})
    events = eng.store.read_all()
    st = fold(events)
    assert _cycle == 1 and st.nodes[0].status.value == "failed", (_cycle, st.nodes[0].status)
    assert st.nodes[0].error_reason == "crash", "the candidate's doing, not an engine_error"
    assert "already filled it once" in st.nodes[0].error, st.nodes[0].error
    assert _withheld(events) == [("before_launch", "infra_unavailable", "run_dir_full")]
    assert len(runs) == 2


def test_a_missing_mount_met_by_the_containment_keeps_the_node_pending(tmp_path):
    """The critic's repro: before the box was ever seen working, the containment filtered a MISSING
    mount out (`admissible_faults`), so the node ended `engine_error` AND the run paused — one node
    per resume. `_materialize` runs before `_ensure_run_setup`, so no setup step can be about to
    create it on this path: the node stays pending and the pause names the mount."""
    eng = make_engine(tmp_path / "run")
    mount = tmp_path / "mnt" / "corpus"          # never created: no run_setup declared to create it
    eng._repo_spec = {"data": {"corpus": {"path": str(mount), "mount": True}}}
    _node(eng.store, 0, "x")

    def fake_run_eval(node, workdir, env=None, profile=None, cancel=None, start_stage=None):
        raise OSError(errno.ENOENT, "No such file or directory", str(mount))

    eng._run_eval = fake_run_eval
    anyio.run(eng._evaluate, 0, anyio.CapacityLimiter(1), None)
    events = eng.store.read_all()
    st = fold(events)
    assert st.nodes[0].status.value == "pending", st.nodes[0].error_reason
    assert st.paused and st.pause_reason == "infra_unavailable", st.pause_reason
    assert _withheld(events) == [("before_launch", "infra_unavailable", None)]


def test_a_disk_filled_from_outside_the_node_workdirs_is_never_blamed_on_the_node(
        tmp_path, monkeypatch):
    """Review 2026-10-08: "largest" ranks only this run's node workdirs, and a disk another run or a
    cache filled still has one — here the run's only node, trivially. Meeting that disk a second
    time blamed it (`met_before` alone): a `crash` and a repair bought on "your writes filled it",
    a broken box ending a node. Now its own growth must account for the space that went: 64 kB of
    checkpoints against 10 MB written elsewhere is the box's, at every meeting.
    MUTATION: let `met_before` blame without the growth clause -> a triage at the second meeting."""
    eng, dev, runs, triage_facts, _p, _d = _disk_filling_engine(tmp_path, monkeypatch,
                                                                external=10_000_000)
    for _cycle in range(2):
        anyio.run(eng._evaluate, 0, anyio.CapacityLimiter(1), None)
        eng.store.append("resume", {})
    events = eng.store.read_all()
    assert len(runs) == 2 and not dev.errors and not triage_facts, (
        "a node was blamed for a disk filled outside its workdir")
    assert fold(events).nodes[0].status.value == "pending"
    assert _withheld(events) == [("decide_repair", "infra_unavailable", "run_dir_full")] * 2
