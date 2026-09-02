"""The words the ENGINE mints on a terminal are a registry, and a registered word must be minted.

`FAILURE_REASONS` says how an EVALUATION can fail. A node can also end without its evaluation having
failed at all — no GPU on the box, the operator aborted it, the Card behind it was dropped — and
those eleven words were bare literals at every write and read site across roughly eighteen modules,
with no registry and no scan. `TRIAGE_ACTIONS` is the same seam shape and has both.

WHAT THE ABSENCE COST, exactly: two readers spell the benign subset independently — `events/replay.py`
excludes it from the failure-spike count, `serve/attention.py` from the owner alert — and both listed
`cancelled`, which NO terminal writer mints. Each carried a word that could never match, in a set
whose whole job is matching, and neither could tell. `cancelled` is minted in this tree, which is why
it looked plausible: `serve/jobs.py` for a launch and `serve/assistant_watch.py` for a watch. Neither
is a node terminal.

The registry is only half the fix. The other half is that the two readers DERIVE from one shared
subset rather than spelling their own, because a registry both of them ignore is a fourth spelling.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from looplab.core.models import (BENIGN_TERMINAL_REASONS, ENGINE_TERMINAL_REASONS,
                                 FAILURE_REASONS)

LOOPLAB = pathlib.Path(__file__).resolve().parents[1] / "looplab"
_REGISTRY_FILE = LOOPLAB / "core" / "models.py"


def _minted_literals() -> set[str]:
    """Every string constant appearing anywhere in `looplab/` EXCEPT the registry's own definition.

    Excluding the definition is what makes the scan evidence rather than a tautology — without it a
    registered-but-never-used word is found inside the tuple that registers it and counts as its own
    proof. `CARD_BUILD_SKIP_REASONS`' guard records the same trap.
    """
    registry_lines = set()
    src = _REGISTRY_FILE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                and node.target.id in ("ENGINE_TERMINAL_REASONS", "BENIGN_TERMINAL_REASONS")):
            registry_lines.update(range(node.lineno, node.end_lineno + 1))

    found: set[str] = set()
    for path in sorted(LOOPLAB.rglob("*.py")):
        try:
            file_tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(file_tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if path == _REGISTRY_FILE and node.lineno in registry_lines:
                continue
            found.add(node.value)
    return found


def test_the_scan_is_not_vacuous():
    minted = _minted_literals()

    assert len(minted) > 1000, f"the literal scan found only {len(minted)} strings"
    assert "gpu_unavailable" in minted, "the scan cannot see a reason it should obviously find"


@pytest.mark.parametrize("reason", ENGINE_TERMINAL_REASONS)
def test_every_registered_reason_is_actually_minted(reason):
    """A registered word nobody writes is exactly the `cancelled` defect, pre-registered.

    MUTATION: add "cancelled" back to `ENGINE_TERMINAL_REASONS` -> red, naming it.
    """
    assert reason in _minted_literals(), (
        f"{reason!r} is registered as a terminal reason but appears nowhere in looplab/ outside the "
        "registry — either a writer was removed or the word never existed")


def test_the_two_vocabularies_stay_separate():
    """`FAILURE_REASONS` gates `Settings.inline_repair_reasons` and the repair loop. A node the
    engine SUPERSEDED is not a node whose code can be repaired, so merging them would offer repair
    for an outcome no repair can address.

    MUTATION: fold one into the other -> red.
    """
    overlap = set(ENGINE_TERMINAL_REASONS) & set(FAILURE_REASONS)

    assert not overlap, f"a reason is in both vocabularies: {sorted(overlap)}"


def test_benign_is_a_subset_of_the_registry():
    assert BENIGN_TERMINAL_REASONS <= set(ENGINE_TERMINAL_REASONS), (
        f"{sorted(BENIGN_TERMINAL_REASONS - set(ENGINE_TERMINAL_REASONS))} is filtered as benign but "
        "is not a terminal reason at all")


def test_both_readers_derive_the_benign_set_rather_than_spelling_it():
    """THE ACTUAL DEFECT. Two hand-written copies is how one dead word survived in both.

    MUTATION: re-inline either set -> they drift again, and nothing notices until someone counts.
    """
    from looplab.events import replay
    from looplab.serve import attention

    assert set(replay._FAILURE_SPIKE_IGNORED_REASONS) == set(BENIGN_TERMINAL_REASONS)
    assert set(attention._IGNORED_FAILURE_REASONS) == set(BENIGN_TERMINAL_REASONS)
    assert "cancelled" not in replay._FAILURE_SPIKE_IGNORED_REASONS, (
        "no terminal writer mints `cancelled`; it can never match and must not read as if it could")


def test_a_frozen_build_is_benign_to_BOTH_readers():
    """The one word the two sets disagreed about, and the disagreement was the bug.

    `attention.py` already excluded `frozen` with its reason written down — a speculative build
    frozen by a transient pause/stop/budget crossing keeps its Card for a later resume — while the
    failure-spike filter counted it as a failure. One judgement, so one answer.
    """
    from looplab.events import replay
    from looplab.serve import attention

    assert "frozen" in replay._FAILURE_SPIKE_IGNORED_REASONS
    assert "frozen" in attention._IGNORED_FAILURE_REASONS
