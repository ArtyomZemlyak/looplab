"""The solver arm A shipped, extracted from its campaign log by a rule anyone can re-run.

§393. `arm_a_retimed.json` records a `solver_sha256` per task and its `_source` describes the
extraction in prose: "the text after `FILE IN CODE DIR solver.py:` up to the first log line". Re-run
that prose on 2026-09-09 and all four hashes DIFFER -- while all four line counts (73, 30, 77, 32)
match exactly. The text was byte-identical apart from a trailing newline.

A provenance hash whose derivation is prose can only ever accuse: it cannot tell "the source moved"
from "you extracted it differently", and the difference between those two is the whole point of
recording it. The rule lives here now, with the four recorded hashes as its test.

The rule, exactly: take the text after the LAST `FILE IN CODE DIR solver.py:` marker, cut at the
first log line after it, strip surrounding blank lines, and end with exactly one newline.
"""
from __future__ import annotations

import hashlib
import os
import re

MARKER = "FILE IN CODE DIR solver.py:"
# A log line, not solver source: an ISO date, a level prefix, or a bare clock.
_LOG_LINE = re.compile(r"^\d{4}-\d{2}-\d{2} |^INFO - |^\d{2}:\d{2}:\d{2}", re.M)
SNAPSHOT = ("/home/jovyan/data/looplab-bench/snapshots-KEEP-campaign-20260829/"
            "20260829-191124/campaign-final")


def shipped_solver(task: str, snapshot: str = SNAPSHOT) -> tuple:
    """`(text, sha12)` for the solver AlgoTuner shipped on `task`, or `(None, None)`.

    The LAST marker, not the first: a campaign that re-wrote `solver.py` logs the file more than
    once, and what arm A shipped is what it ended with.
    """
    path = os.path.join(snapshot, f"A-{task}.log")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            txt = fh.read()
    except OSError:
        return None, None
    at = txt.rfind(MARKER)
    if at < 0:
        return None, None
    body = txt[at + len(MARKER):]
    end = _LOG_LINE.search(body)
    src = (body[:end.start()] if end else body).strip("\n") + "\n"
    return src, hashlib.sha256(src.encode()).hexdigest()[:12]
