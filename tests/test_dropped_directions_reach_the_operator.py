"""The line that says "not silent" is not silent.

Nothing in this package configures logging — there is no `basicConfig` and no `setLevel` anywhere
in `looplab/` — so Python's default applies and the root logger sits at WARNING with no handlers.
An `_LOG.info(...)` therefore reaches nobody, on every run.

MEASURED: the package carries 39 `_LOG.warning` calls and, before this, exactly ONE `_LOG.info` —
the one telling the operator that a memo's recommended directions did not all become board rows,
whose own comment reads "Not silent: the operator reading the log sees a memo whose directions did
not all become cards". On `runs/e5small-dr-unified-v12` nine memos proposed 94 directions, the
board holds NINE, and the console carries ZERO of those lines.
"""
from __future__ import annotations

import logging
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_nothing_configures_logging_so_INFO_reaches_nobody():
    """The premise, asserted rather than assumed. If someone later adds a `basicConfig(INFO)` this
    goes red and the level choice below can be revisited on purpose instead of by accident."""
    configured = []
    for path in (ROOT / "looplab").rglob("*.py"):
        text = path.read_text()
        if re.search(r"\blogging\.basicConfig\b|\b_LOG\.setLevel\b|getLogger\(\)\.setLevel", text):
            configured.append(str(path.relative_to(ROOT)))
    assert not configured, (
        f"logging is configured in {configured} — re-check whether INFO now reaches an operator")
    assert logging.getLogger("looplab.engine.research_cadence").getEffectiveLevel() \
        >= logging.WARNING


def test_the_dropped_directions_line_is_at_a_level_that_SHOWS():
    """Mutation: put it back to `_LOG.info`, or to `debug`, and the operator stops learning that a
    paid memo's directions were refused."""
    src = (ROOT / "looplab/engine/research_cadence.py").read_text()
    # `find`, not `index`: a deleted line must read as a FAILURE carrying this file's own message,
    # not as a ValueError whose traceback says nothing about the property. Caught by the mutation
    # pass that removed the call, and the same lesson the offload guard learned from a StopIteration.
    idx = src.find("recommended direction(s) not registered as")
    assert idx != -1, (
        "the dropped-directions line is gone — an operator now learns nothing when a paid memo's "
        "directions are refused")
    window = src[max(0, idx - 400):idx]
    call = re.findall(r"_LOG\.(debug|info|warning|error|exception|critical)\(", window)
    assert call, "the call that emits this line is gone — re-point this guard"
    assert call[-1] in ("warning", "error"), (
        f"the dropped-directions line is emitted at {call[-1]!r}, which the default root level "
        "(WARNING, unconfigured) discards — the line's own comment says it must not be silent")


def test_the_package_keeps_ONE_operator_level():
    """A second INFO line would be the same defect again. This is a bound that can only shrink:
    the count is asserted at zero, so adding one is a deliberate act with a red test attached."""
    infos = []
    for path in (ROOT / "looplab").rglob("*.py"):
        for n, line in enumerate(path.read_text().split("\n"), 1):
            if "_LOG.info(" in line:
                infos.append(f"{path.relative_to(ROOT)}:{n}")
    assert not infos, (
        "these lines are written at INFO and the default root level discards them — either raise "
        f"them or accept they are for a debugger only:\n  " + "\n  ".join(infos))


def test_the_message_still_names_the_numbers():
    """NON-VACUITY of the level change: a line that shows but says nothing actionable is no better.
    It must carry how many were dropped, of how many, how many were already open, and the cap."""
    src = (ROOT / "looplab/engine/research_cadence.py").read_text()
    idx = src.find("recommended direction(s) not registered as")
    assert idx != -1, "the dropped-directions line is gone — nothing left to check the content of"
    window = src[max(0, idx - 400):idx + 500]
    assert window.count("%d") == 4, "the four numbers are the whole content of this line"
    assert "DEEP_RESEARCH_OPEN_BELIEF_CAP" in window
    assert "the memo and its hint still carry" in window, (
        "the line must say the directions are NOT lost, or an operator reads a refusal as a loss")
