"""Out-of-process driver for the "killed mid-ratification" repro.

Not collected by pytest (leading underscore). Spawned by
``tests/test_concept_ratification.py::test_a_pass_killed_after_its_first_write_completes_on_re_entry``
and made to SIGKILL ITSELF the instant its first alias row is durable. That is the only way to
produce the byte prefix re-entry has to survive — a governance ledger holding a real, fsynced
decision written by a process that never got to its second one, and never wrote a receipt.

A cooperative early ``return`` would prove nothing here: the whole question is whether the stage's
at-least-once shape survives a writer that had no chance to record what it had done.

Usage: ``python -m tests._concept_ratify_crash_driver <memory_dir>``
"""
from __future__ import annotations

import os
import signal
import sys

from looplab.engine import concept_tidy


def main(memory_dir: str) -> None:
    original = concept_tidy.record_concept_alias

    def kill_after_first(*args, **kwargs):
        record = original(*args, **kwargs)
        # The row is fsynced by `_append_governance(require_durable=True)` before this returns, so
        # the kill lands strictly AFTER a durable decision and strictly BEFORE the stage could
        # observe it, receipt it, or move on.
        sys.stdout.flush()
        os.kill(os.getpid(), signal.SIGKILL)
        return record                                  # unreachable

    concept_tidy.record_concept_alias = kill_after_first
    concept_tidy.ratify_concept_merges(memory_dir, at="crash-driver")


if __name__ == "__main__":
    main(sys.argv[1])
