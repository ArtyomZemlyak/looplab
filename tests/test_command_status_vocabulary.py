"""The durable command record's lifecycle words live in ONE place, and the browser's copy agrees.

`serve/protocol.py`'s docstring calls itself the home of the string contracts the server, the
terminal client and the React UI share. These seven were not there: they were spelled across
`run_commands`, the control router, both TUI halves, the run-control tool and
`ui/src/commandModel.js`, with no test pinning any copy against another.

The failure this prevents is not a crash. A surface matching on a word the server no longer sends
simply never takes that branch: a command that succeeded renders as still-pending, a rejected one
renders as nothing at all, and the polling machine keeps waiting for a terminal that already
arrived. `run_commands.TERMINAL_STATUSES` existed and covered five of the seven; the two ACTIVE ones
were spelled inline at four sites in that same file.

WHAT STAYS AT THE SURFACES is the MEANING. `commandModel.js` decides that `noop` reads as "already
satisfied" and that `executing` is deliberately pending; this file pins only that both sides use the
same words, partitioned the same way.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from looplab.serve.protocol import (COMMAND_ACTIVE_STATUSES, COMMAND_FAILED_STATUSES,
                                    COMMAND_STATUSES, COMMAND_SUCCEEDED_STATUSES,
                                    COMMAND_TERMINAL_STATUSES)

_JS = pathlib.Path(__file__).resolve().parents[1] / "ui" / "src" / "commandModel.js"


def _js_set(name: str) -> set[str]:
    match = re.search(rf"{name}\s*=\s*new Set\(\[([^\]]*)\]\)", _JS.read_text(encoding="utf-8"))
    assert match, f"{name} is gone or reshaped in commandModel.js; re-point this guard"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def test_the_partition_is_a_partition():
    """Every status is exactly one of active, succeeded or failed.

    MUTATION: put `noop` in both success and failure -> red. A word in two halves is how a surface
    renders one record two ways depending on which check it runs first.
    """
    assert COMMAND_ACTIVE_STATUSES & COMMAND_TERMINAL_STATUSES == set()
    assert COMMAND_SUCCEEDED_STATUSES & COMMAND_FAILED_STATUSES == set()
    assert COMMAND_SUCCEEDED_STATUSES | COMMAND_FAILED_STATUSES == COMMAND_TERMINAL_STATUSES
    assert COMMAND_ACTIVE_STATUSES | COMMAND_TERMINAL_STATUSES == COMMAND_STATUSES
    assert len(COMMAND_STATUSES) == 7


def test_run_commands_derives_rather_than_spelling_its_own():
    """MUTATION: re-inline the frozenset -> the two drift, which is how this started."""
    from looplab.serve.run_commands import TERMINAL_STATUSES

    assert TERMINAL_STATUSES is COMMAND_TERMINAL_STATUSES, (
        "TERMINAL_STATUSES is a COPY, not the shared object — patching one would not be seen by "
        "consumers of the other")


@pytest.mark.parametrize("js_name,py_set", [
    ("COMMAND_SUCCEEDED", COMMAND_SUCCEEDED_STATUSES),
    ("COMMAND_FAILED", COMMAND_FAILED_STATUSES),
    ("COMMAND_PENDING", COMMAND_ACTIVE_STATUSES),
])
def test_the_browser_partitions_the_same_words_the_same_way(js_name, py_set):
    """The cross-language half, and the one nothing could catch before.

    A surface matching on a word the server no longer sends never takes that branch — a succeeded
    command renders as still-pending, and the polling machine waits for a terminal that arrived.

    MUTATION: rename a status on either side -> red, naming the half that drifted.
    """
    if not _JS.exists():
        pytest.skip("the UI command model is not present in this checkout")

    assert _js_set(js_name) == set(py_set), (
        f"{js_name} and its Python counterpart disagree: "
        f"js-only {sorted(_js_set(js_name) - set(py_set))}, "
        f"py-only {sorted(set(py_set) - _js_set(js_name))}")


def test_the_browser_knows_no_status_the_server_cannot_send():
    """Catches a word invented on the client, which no per-set comparison above would see if it
    were added to a set neither side pins."""
    if not _JS.exists():
        pytest.skip("the UI command model is not present in this checkout")
    js_all = _js_set("COMMAND_SUCCEEDED") | _js_set("COMMAND_FAILED") | _js_set("COMMAND_PENDING")

    assert js_all == set(COMMAND_STATUSES), (
        f"the browser's vocabulary is not the server's: {sorted(js_all ^ set(COMMAND_STATUSES))}")


def test_noop_is_a_success_and_rejected_is_not_a_failure_of_the_work():
    """The two distinctions the split exists for, stated so a future merge has to argue with them.

    `noop` means the command was understood and the state it asked for already held — reading it as
    a plain success makes a surface claim it did something. `rejected` means the record never became
    work at all, which is why it is terminal without being an execution failure.
    """
    assert "noop" in COMMAND_SUCCEEDED_STATUSES and "noop" not in COMMAND_FAILED_STATUSES
    assert "rejected" in COMMAND_FAILED_STATUSES and "rejected" in COMMAND_TERMINAL_STATUSES
