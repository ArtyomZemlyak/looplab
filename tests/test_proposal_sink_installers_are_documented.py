"""The sink's docstring names every lane that installs it, and the count cannot rot.

`novelty.py::_capture_proposal_events` buffers the FOLDED, authority-bearing proposal rows
(`EV_NOVELTY_REJECTED` / `EV_NOVELTY_GRADED` / `EV_CROSS_RUN_PRIOR`) so a worker thread never
appends them — invariant #1's sole-writer rule, and `speculation.py::_proposal_authority_seq` is
fenced on exactly those rows.

WHY A TEST AND NOT A CAREFUL EDIT. Its docstring said "Layer 5 installs this context" and named only
Layer 5 for as long as that was true. Two more installers landed on 2026-08-30 when both offloaded
proposal lanes moved their paid provider wait onto a worker thread, and the sentence a maintainer
would consult still described a one-installer world — the same drift the two markers this file
closes were filed for, in the docstring of the very mechanism they are about. A sentence nobody
re-derives is wrong the moment the population moves.

So the population is DERIVED here: every `with self._capture_proposal_events()` in `looplab/`, by
AST, and each one's home module must be named in the docstring. Adding a fourth lane without saying
so is a red test rather than a silent lie.
"""
from __future__ import annotations

import ast
import inspect

from _source_scan import iter_trees

from looplab.engine import novelty

def _installer_modules() -> set[str]:
    """Module basenames holding a `_capture_proposal_events()` call, excluding its definition."""
    found = set()
    for path, tree in iter_trees():
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_capture_proposal_events"):
                found.add(path.name)
    return found


def test_the_sink_is_installed_by_more_than_one_lane():
    """The premise. If this ever drops to one the docstring's warning is moot — and so is this
    file — but until then a one-installer sentence is a false one."""
    assert len(_installer_modules()) >= 2, (
        f"only {_installer_modules()} installs the proposal sink; re-read the docstring's claim")


def test_every_installer_module_is_NAMED_in_the_sink_docstring():
    """THE PROPERTY. Mutation: add a `with self._capture_proposal_events()` in a fourth module and
    this goes red until the docstring says so.

    Modules, not call sites: two lanes in one module are one thing to explain, and pinning call
    sites would make an ordinary refactor red for no reader's benefit.
    """
    doc = inspect.getdoc(novelty.NoveltyGateMixin._capture_proposal_events) or ""
    assert doc, "the sink lost its docstring — that is the whole subject of this test"
    missing = {name for name in _installer_modules() if name not in doc}
    assert not missing, (
        f"these modules install the proposal sink and the docstring does not name them: "
        f"{sorted(missing)} — a maintainer reading it would believe the lane does not exist")


def test_the_docstring_states_the_UNCONDITIONAL_publish_the_offload_lanes_use():
    """Layer 5's publish is CONDITIONAL on its own election fence; the offload lanes' is not, and
    the difference is the `bd182357` discard receipt — a refused proposal is when the receipt
    matters most. A docstring describing only the conditional shape would send a new lane the wrong
    way.

    Mutation: drop the sentence and this goes red.
    """
    doc = inspect.getdoc(novelty.NoveltyGateMixin._capture_proposal_events) or ""
    assert "whether or not an idea formed" in doc
    assert "finally" in doc, (
        "and from a `finally`, or a raise from the paid call discards the whole buffer")
