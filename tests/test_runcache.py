"""RunStateCache fingerprint freshness (shared cross-run fold cache).

The cache keys a folded RunState by the event log's fingerprint. Truncating mtime to whole seconds
(`int(st_mtime)`) made a same-size rewrite WITHIN one second invisible, so SiblingRunTools/
MachineRunsTools could keep serving an obsolete cross-run evidence prefix. Offline."""
from __future__ import annotations

import os

from looplab.tools._runcache import RunStateCache


def test_sig_detects_a_same_size_subsecond_rewrite(tmp_path):
    rd = tmp_path / "runX"
    rd.mkdir()
    path = rd / "events.jsonl"
    path.write_text('{"seq": 0}\n', encoding="utf-8")     # size is identical across the two utimes

    base = (path.stat().st_mtime_ns // 1_000_000_000) * 1_000_000_000   # floor to a whole second
    early, late = base + 100, base + 800_000_000                        # SAME whole second, diff ns
    assert early // 1_000_000_000 == late // 1_000_000_000              # int(st_mtime) would collide

    os.utime(path, ns=(early, early))
    sig1 = RunStateCache.sig(rd)
    os.utime(path, ns=(late, late))                        # a same-size rewrite within one second
    sig2 = RunStateCache.sig(rd)
    assert sig1 != sig2, "fingerprint must be sub-second aware (nanosecond mtime), not int(st_mtime)"
