"""Did the path a candidate built actually RUN during its evaluation?

Measured 2026-09-23 on a MiniOneRec inference run. A node added a prefix-cached prefill behind a
self-check. The check died on an attribute transformers 5 removed, a fallback switched the new path
off, and the node scored 1.004 with every list byte-identical to its parent -- the unchanged tree
measured again, and recorded as an idea that does not help. Nothing in the engine could tell "this
change is worthless" from "this change never executed": both finish cleanly and print a number.

So a Developer may DECLARE, per node, lines its new code prints only when the new path is active
(`activation_markers` on its `done`, persisted as `looplab_activation.json` beside
`looplab_stages.json`). After an evaluation that otherwise succeeded, the engine checks that every
declared marker appears in what the evaluation printed. One that does not makes the attempt
`inert_path` (`core/models.py::FAILURE_REASONS`): the metric is withheld, because it measured the
path the node meant to replace, and the node goes to repair with the missing marker named.

WHY A DECLARATION AND NOT A GUESS. `engine/triage.py::_failure_reason` classifies structurally and
never from a failure's own text -- the rules that parsed messages were deleted on 2026-08-20. A
search of the log for "disabled" or "fallback" would be that rule again, with the candidate's own
choice of words deciding the verdict. A declared marker is a CONTRACT, like a stage's `expect`: the
node said what running looks like, and the engine only checks that it happened. A node that declares
nothing is judged exactly as before.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Optional

from looplab.core.node_evidence import open_untrusted_regular, read_bounded_regular_file
from looplab.engine.eval_log_plan import _log_name_key, attempt_byte_floor

ACTIVATION_MANIFEST_NAME = "looplab_activation.json"
MAX_MARKERS = 8
MAX_MARKER_CHARS = 200
# Per file, from its START and from its END: a marker is usually printed at warmup and a log is
# usually short, but a training log is not, and the check must stay bounded whatever the candidate
# printed. The end alone missed a warmup marker followed by 9 MiB of training output (crit_v55 A3,
# driven: a real metric withheld as `inert_path`), so a longer log is read at both ends and only
# its middle goes unread.
_MAX_LOG_BYTES = 8 * 1024 * 1024


def normalize_markers(raw) -> list:
    """The markers a Developer declared, as a clean bounded list; [] for anything unusable.

    Total: runs inside an emit that has already cost minutes. Each marker is stripped, must be a
    non-empty string, is cut to `MAX_MARKER_CHARS` (a marker is a line fragment, not a paragraph),
    and duplicates collapse. At most `MAX_MARKERS` survive."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list = []
    for item in raw:
        if not isinstance(item, str):
            continue
        marker = item.strip()[:MAX_MARKER_CHARS].strip()
        if marker and marker not in out:
            out.append(marker)
        if len(out) >= MAX_MARKERS:
            break
    return out


def manifest_text(markers: Iterable[str]) -> str:
    return json.dumps({"markers": list(markers)}, indent=1)


# The largest activation manifest read. A list of short marker strings; anything bigger is not one.
_MAX_MANIFEST_BYTES = 1 << 20


def read_markers(workdir) -> list:
    """The markers declared in a node's workdir, or [] -- a malformed file is no declaration.

    Read as the CANDIDATE's file it is (`core/node_evidence.py::read_bounded_regular_file`: no link,
    no FIFO, bounded) and parsed for everything `json.loads` raises: a FIFO under this name blocked
    the event loop and a manifest nested past ~1,000 levels raised `RecursionError` out of
    `_eval_settle_outcome` — the node's `engine_error`, the run paused (critic 2026-09-26, driven)."""
    raw = read_bounded_regular_file(Path(workdir) / ACTIVATION_MANIFEST_NAME, _MAX_MANIFEST_BYTES + 1)
    if raw is None or len(raw) > _MAX_MANIFEST_BYTES:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, RecursionError):
        return []
    return normalize_markers(data.get("markers") if isinstance(data, dict) else None)


def _fresh_logs(workdir, since: Optional[float], snapshot=None, engine_logs=None) -> list:
    """What THIS attempt wrote to the `*.log` files in `workdir` — both ends of each — newest first.

    `snapshot` is the attempt-start cursor set (`engine/eval_log_plan.py::snapshot_training_logs`) and
    `attempt_byte_floor` the one boundary the watchdogs and the repair judge already read at: a stage
    that RE-RAN appends to its log (`runtime/sandbox.py::_tee_drain`), so an earlier attempt's marker
    line sits in a file this attempt also wrote, fresh by mtime, and it vouched for a path the scored
    run never took (critic crit_v51 F1b / crit_v52 F2, driven: the scored checkpoint written by a
    fallback that printed no marker). A log whose boundary cannot be established is not read — no
    credit, the refusing direction. `since` (the attempt's start) still drops a whole log last written
    before it; with no snapshot it is the only floor, and the appended case is then credited.
    A stage this attempt REUSED wrote nothing, so its earlier markers are not credited either way —
    doc 69, 69.10b.

    `engine_logs` names the logs the ENGINE appends to — the case-folded basenames of this attempt's
    `EvalLogPlan.roles`: `setup.log`, one `<stage>.log` per resolved stage, or `eval.log` — and only
    those are read past the cursor. The engine never truncates one, so a boundary that still matches
    is taken as an append — which a CANDIDATE writing a file of the same name defeats: a
    `logging.basicConfig(filename="train.log", filemode="w")` under a stage named `train` shares the
    engine's log, its byte-identical rewrite matches the old boundary, and the marker it printed
    before the old end is not credited (crit_v56 F3, driven end to end: `inert_path` where the
    candidate's own `own.log` scores). No size, identity or probe tells that rewrite from an append;
    it stays open in doc 69 69.10b. A log the CANDIDATE writes is read whole from the `since` floor: rewritten in place
    ('w' mode, same inode) with deterministic output, its bytes at the old boundary match, the probe
    reads the rewrite as an append, and the marker this attempt really printed was dropped — a real
    metric withheld as `inert_path` (crit_v55 A1, driven). The cost is the old one for that file: a
    candidate that APPENDS to its own log across attempts gets its earlier line credited, and the
    marker contract is the stage's stdout and stderr, which the engine's own logs carry. None =
    every log is taken as the engine's."""
    out = []

    def _mtime(path) -> float:
        # PER ENTRY (critic 2026-09-30, crit_v51 F7): the key stat-ed every entry inside the
        # listing's one `try`, so a single dangling `latest.log` link emptied the whole listing and
        # every declared marker read as missing. An entry that cannot be stat-ed sorts last and is
        # skipped by the loop's own `stat` below.
        try:
            return path.stat().st_mtime
        except OSError:
            return float("-inf")

    try:
        entries = sorted(Path(workdir).glob("*.log"), key=_mtime, reverse=True)
    except OSError:
        return out
    for path in entries:
        try:
            st = path.stat()
            if since is not None and st.st_mtime + 1.0 < float(since):
                continue
            # The candidate's own file: no link, no FIFO — `x.log` as a FIFO blocked this read on
            # the event loop (critic 2026-09-26, driven) — and the size bound read off the SAME entry.
            with open_untrusted_regular(path) as fh:
                size = os.fstat(fh.fileno()).st_size
                cursor = snapshot is not None and (engine_logs is None
                                                   or _log_name_key(path.name) in engine_logs)
                floor = attempt_byte_floor(fh, path, snapshot) if cursor else 0
                if floor is None:
                    continue
                fh.seek(floor)
                if size - floor <= 2 * _MAX_LOG_BYTES:
                    out.append(fh.read(2 * _MAX_LOG_BYTES).decode("utf-8", "replace"))
                    continue
                head = fh.read(_MAX_LOG_BYTES)
                fh.seek(size - _MAX_LOG_BYTES)
                out.append(head.decode("utf-8", "replace"))
                out.append(fh.read(_MAX_LOG_BYTES).decode("utf-8", "replace"))
        except OSError:
            continue
    return out


def missing_markers(markers: Iterable[str], *, texts: Iterable[str] = (), workdir=None,
                    since: Optional[float] = None, snapshot=None, engine_logs=None) -> list:
    """The declared markers that appear NOWHERE the evaluation printed: the captured streams in
    `texts`, then what this attempt wrote to the logs in `workdir` (`_fresh_logs`, which says what
    `snapshot` and `engine_logs` bound). Exact substring, case-sensitive -- the node wrote the line
    and named it, so there is nothing to interpret."""
    markers = [m for m in markers if m]
    if not markers:
        return []
    haystacks = [t for t in texts if isinstance(t, str) and t]
    missing = [m for m in markers if not any(m in h for h in haystacks)]
    if missing and workdir is not None and os.path.isdir(str(workdir)):
        logs = _fresh_logs(workdir, since, snapshot, engine_logs)
        missing = [m for m in missing if not any(m in h for h in logs)]
    return missing

