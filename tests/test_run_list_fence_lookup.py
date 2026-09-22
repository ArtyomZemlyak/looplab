"""The run list asks "is this run being deleted?" once per LISTING, not once per run.

Review 2026-09-22, SRV2-02. `serve/run_projections.py::run_summaries` called
`load_run_deletion_fence(rd)` for every run, and the fence protocol's accelerator,
`core/fence.py::_warm_directory_lookup`, `scandir`s the fence's directory before its authoritative
`lstat` — and the fence lives in the RUN ROOT. So a warm `GET /api/runs` over N runs read the root
listing N times (up to 4,096 entries each): O(N²) in directory entries, measured 2.33 s for a warm
list over 2,000 runs. The prefetch is right for ONE lookup (it is what makes an absent-marker probe
cheap on a FUSE mount) and wrong for a batch that has just listed that very directory itself.
"""
from __future__ import annotations

import os
import uuid

import pytest

pytest.importorskip("fastapi")

from looplab.core import run_deletion                              # noqa: E402
from looplab.events.eventstore import EventStore                    # noqa: E402
from looplab.serve.server import make_app                           # noqa: E402

_GEN = "a" * 64


def _seed(root, count: int) -> list[str]:
    names = [f"r{index:03d}" for index in range(count)]
    for name in names:
        EventStore(root / name / "events.jsonl").append(
            "run_started", {"run_id": name, "task_id": "t", "goal": "g", "direction": "min"})
    return names


def _fence(run_dir) -> None:
    run_deletion.publish_run_deletion_fence(
        run_dir, operation_id=str(uuid.uuid4()), expected_generation=_GEN, expected_seq=0,
        receipt_name="receipt.json")


def test_a_warm_run_list_reads_the_root_listing_at_most_once(tmp_path, monkeypatch):
    """MUTATION: call `load_run_deletion_fence(rd)` for every run again -> 50 root scans."""
    names = _seed(tmp_path, 50)
    srv = make_app(tmp_path).state.looplab
    assert [row["run_id"] for row in srv.run_summaries()] == names       # cold: fills the cache

    scans: list = []
    real_scandir = os.scandir

    def counting_scandir(path="."):
        scans.append(path)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", counting_scandir)
    rows = srv.run_summaries()

    assert [row["run_id"] for row in rows] == names
    assert len(scans) <= 1, f"a warm run list scanned directories {len(scans)} times"


def test_a_run_whose_fence_is_in_the_listing_is_still_decided_by_the_fence(tmp_path):
    """The listing only decides WHICH runs are worth an authoritative look. A published fence still
    hides its run, and a fence file that cannot be read still fails CLOSED (the run is left out),
    exactly as the per-run lookup did."""
    names = _seed(tmp_path, 3)
    srv = make_app(tmp_path).state.looplab
    _fence(tmp_path / names[1])
    assert [row["run_id"] for row in srv.run_summaries()] == [names[0], names[2]]

    # An unreadable fence — a DIRECTORY where the marker belongs — is an unknown fence, never
    # permission to show the run.
    unreadable = run_deletion.run_deletion_fence_path(tmp_path / names[2])
    unreadable.mkdir()
    assert [row["run_id"] for row in srv.run_summaries()] == [names[0]]


def test_a_fence_published_for_a_listed_run_hides_it_on_the_next_list(tmp_path):
    """The cache must not outlive the fence: a run already summarized (and cached) disappears from
    the very next list once its deletion fence exists."""
    names = _seed(tmp_path, 2)
    srv = make_app(tmp_path).state.looplab
    assert [row["run_id"] for row in srv.run_summaries()] == names
    _fence(tmp_path / names[0])
    assert [row["run_id"] for row in srv.run_summaries()] == [names[1]]
