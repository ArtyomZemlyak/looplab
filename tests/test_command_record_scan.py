"""One walk of a run's durable command records (doc 25 SC-13).

Five scanners in `RunCommandService` each opened the command directory, globbed `cmd_*.json`, applied
a symlink policy and loaded the record. The symlink policies had ALREADY diverged between them, and
that is the part worth removing rather than the line count: a symlinked `cmd_*.json` is an attempt to
make one run's command file point at another's, so a scanner that forgets the check reads a record it
does not own and answers a liveness question about the wrong run.

The two surviving policies are now NAMED, because they are opposites and both are correct for their
caller — which is exactly why an unnamed third could appear without anyone noticing.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from looplab.serve.run_commands import RunCommandService


class _Srv:
    def __init__(self, root: Path):
        self.root = root


def _service(tmp_path: Path) -> tuple[RunCommandService, Path]:
    service = RunCommandService.__new__(RunCommandService)
    service.srv = _Srv(tmp_path)
    # `_directory` resolves through `validate_paths`, whose own 404/allow-list behaviour has its own
    # tests. This file is about the WALK; stub the resolution so a failure here means the walk broke.
    service.validate_paths = lambda rd: Path(rd)
    run_dir = tmp_path / "run"
    (run_dir / ".commands").mkdir(parents=True)
    return service, run_dir


def _record(run_dir: Path, command_id: str, **fields) -> Path:
    path = run_dir / ".commands" / f"{command_id}.json"
    path.write_text(json.dumps({"id": command_id, **fields}), encoding="utf-8")
    return path


def _scan(service, run_dir, policy):
    return list(service._scan_command_records(run_dir, on_symlink=policy))


# ------------------------------------------------------------------ the walk itself

def test_a_missing_command_directory_yields_nothing_rather_than_raising(tmp_path):
    """A run with no commands yet is ordinary, not an error — every caller's `if not exists` guard
    said so, and the shared walk has to keep saying it."""
    service = RunCommandService.__new__(RunCommandService)
    service.srv = _Srv(tmp_path)
    service.validate_paths = lambda rd: Path(rd)
    assert _scan(service, tmp_path / "never-created", "refuse") == []


def test_each_durable_record_is_yielded_with_its_loaded_content(tmp_path):
    service, run_dir = _service(tmp_path)
    _record(run_dir, "cmd_" + "a" * 32, status="accepted")
    _record(run_dir, "cmd_" + "b" * 32, status="succeeded")
    seen = {path.stem: record for path, record in _scan(service, run_dir, "refuse")}
    assert set(seen) == {"cmd_" + "a" * 32, "cmd_" + "b" * 32}
    assert seen["cmd_" + "a" * 32]["status"] == "accepted"


def test_an_unrelated_file_in_the_directory_is_not_scanned(tmp_path):
    """The glob is the boundary: the same directory holds `.cmd_*.executing` claims and
    `.activity_*.json` heartbeats, which are different file classes with their own rules."""
    service, run_dir = _service(tmp_path)
    _record(run_dir, "cmd_" + "a" * 32, status="accepted")
    (run_dir / ".commands" / ".cmd_x.executing").write_text("{}", encoding="utf-8")
    (run_dir / ".commands" / "notes.json").write_text("{}", encoding="utf-8")
    assert [path.stem for path, _ in _scan(service, run_dir, "refuse")] == ["cmd_" + "a" * 32]


def test_a_malformed_record_is_yielded_as_None_not_skipped(tmp_path):
    """Skipping it would erase the evidence. Every caller decides for itself what an unreadable
    record means; the walk's job is to surface it, not to judge it."""
    service, run_dir = _service(tmp_path)
    path = run_dir / ".commands" / ("cmd_" + "a" * 32 + ".json")
    path.write_text("{not json", encoding="utf-8")
    assert _scan(service, run_dir, "refuse") == [(path, None)]


# ------------------------------------------------------------------ the two symlink policies

def test_refuse_rejects_the_whole_request_on_a_planted_link(tmp_path):
    """A planted link means the answer to "what is the state of this command" cannot be trusted at
    all, so the four scanners that answer that question refuse rather than guess."""
    service, run_dir = _service(tmp_path)
    target = _record(run_dir, "cmd_" + "b" * 32, status="accepted")
    (run_dir / ".commands" / ("cmd_" + "a" * 32 + ".json")).symlink_to(target)
    with pytest.raises(HTTPException) as info:
        _scan(service, run_dir, "refuse")
    assert info.value.status_code == 409


def test_unreadable_counts_the_link_instead_of_refusing(tmp_path):
    """The OPPOSITE policy, and also correct: `_active_command_ids` is a fail-CLOSED liveness census
    feeding a destructive mutation's safety check. Refusing there would let a planted link BLOCK the
    check; counting the link as active trips it, which is the safe direction."""
    service, run_dir = _service(tmp_path)
    target = _record(run_dir, "cmd_" + "b" * 32, status="succeeded")
    link = run_dir / ".commands" / ("cmd_" + "a" * 32 + ".json")
    link.symlink_to(target)
    seen = dict(_scan(service, run_dir, "unreadable"))
    assert seen[link] is None, "a link must not be READ; its content is not this run's"
    assert len(seen) == 2


def test_the_liveness_census_treats_a_planted_link_as_active(tmp_path):
    """End to end through the real caller: the id shows up as active, so a destructive mutation
    sees an unresolved command and fails closed."""
    service, run_dir = _service(tmp_path)
    target = _record(run_dir, "cmd_" + "b" * 32, status="succeeded")
    (run_dir / ".commands" / ("cmd_" + "a" * 32 + ".json")).symlink_to(target)
    assert "cmd_" + "a" * 32 in service._active_command_ids(run_dir)
    assert "cmd_" + "b" * 32 not in service._active_command_ids(run_dir), (
        "a genuinely terminal record is not active")


# ------------------------------------------------------------------ every scanner uses it

@pytest.mark.parametrize("scanner", ["_active_command_ids", "_unresolved_equivalent",
                                     "_pending_finalize_record", "_active_record",
                                     "_unresolved_terminal_record"])
def test_no_scanner_re_derives_the_walk(scanner):
    source = inspect.getsource(getattr(RunCommandService, scanner))
    assert "_scan_command_records(" in source, f"{scanner} walks the directory itself"
    assert 'glob("cmd_*.json")' not in source, f"{scanner} re-globs the command directory"
    assert "must not be a symlink" not in source, f"{scanner} re-derives the symlink policy"


def test_the_symlink_refusal_exists_in_exactly_two_places():
    """`_path` guards a SINGLE named record on the way to reading it; the walk guards every record it
    enumerates. Two checks, one message — a third would mean a scanner grew its own again."""
    source = inspect.getsource(RunCommandService)
    assert source.count("run command record must not be a symlink") == 2


def test_the_cross_run_restart_sweep_keeps_its_own_THIRD_policy():
    """Not folded in, deliberately. The sweep walks EVERY run looking for a stranded restart; a 409
    over one planted link would abort the sweep for every other run, so it skips the link instead.
    Named here so the third policy is a decision rather than an omission."""
    source = inspect.getsource(RunCommandService)
    sweep = source[source.index("candidates = list(self.srv.root.iterdir())"):]
    assert "if path.is_symlink() or not _COMMAND_ID_RE.fullmatch(path.stem):\n                        continue" in sweep
