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

ACTIVATION_MANIFEST_NAME = "looplab_activation.json"
MAX_MARKERS = 8
MAX_MARKER_CHARS = 200
# Per file, from its END: a marker is usually printed at warmup and a log is usually short, but a
# training log is not, and the check must stay bounded whatever the candidate printed.
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


def read_markers(workdir) -> list:
    """The markers declared in a node's workdir, or [] -- a malformed file is no declaration."""
    try:
        data = json.loads((Path(workdir) / ACTIVATION_MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    return normalize_markers(data.get("markers") if isinstance(data, dict) else None)


def _fresh_logs(workdir, since: Optional[float]) -> list:
    """The tails of the eval's own `*.log` files in `workdir`, newest first. `since` is the attempt's
    start: a log left by an EARLIER attempt in the deliberately reused workdir may hold a marker the
    failing attempt never printed, and must not vouch for it."""
    out = []
    try:
        entries = sorted(Path(workdir).glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return out
    for path in entries:
        try:
            st = path.stat()
            if since is not None and st.st_mtime + 1.0 < float(since):
                continue
            with open(path, "rb") as fh:
                if st.st_size > _MAX_LOG_BYTES:
                    fh.seek(st.st_size - _MAX_LOG_BYTES)
                out.append(fh.read().decode("utf-8", "replace"))
        except OSError:
            continue
    return out


def missing_markers(markers: Iterable[str], *, texts: Iterable[str] = (), workdir=None,
                    since: Optional[float] = None) -> list:
    """The declared markers that appear NOWHERE the evaluation printed: the captured streams in
    `texts`, then the fresh stage logs in `workdir`. Exact substring, case-sensitive -- the node wrote
    the line and named it, so there is nothing to interpret."""
    markers = [m for m in markers if m]
    if not markers:
        return []
    haystacks = [t for t in texts if isinstance(t, str) and t]
    missing = [m for m in markers if not any(m in h for h in haystacks)]
    if missing and workdir is not None and os.path.isdir(str(workdir)):
        logs = _fresh_logs(workdir, since)
        missing = [m for m in missing if not any(m in h for h in logs)]
    return missing

