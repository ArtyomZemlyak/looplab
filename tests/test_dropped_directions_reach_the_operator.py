"""The line that says "not silent" is not silent — and what it says is true.

Nothing in this package configures logging: no `basicConfig`, no `dictConfig`, no `setLevel`
anywhere in `looplab/`, so Python's default applies, the root logger sits at WARNING with no
handlers, and `logging.lastResort` is what puts a record on stderr (which `serve/engine_proc.py`
captures into `engine.stderr.log`). An `_LOG.info(...)` therefore reaches nobody, on every run —
and the one line telling the operator that a paid memo's recommended directions did not all become
board rows was written at exactly that level, under a comment reading "Not silent".

DRIVEN, NOT PINNED. CLAUDE.md's guard ladder puts "drive the property" first and "AST, never
substrings" last, and the first cut of this file sat below the bottom rung: it read
`research_cadence.py` as TEXT and asserted a `_LOG.<level>(` spelling near a message literal. That
is not the property — the property is that a record ARRIVES at a level an unconfigured root logger
keeps — and reading source instead of driving it cost the two defects below, because nobody who
only greps for `_LOG.warning(` ever reads the sentence being emitted:

  * the line told the operator "the memo and its hint still carry them", which
    `admit_research_beliefs`' own docstring had already retracted in writing —
    `deep_research_hint_text` carries the first `DEEP_RESEARCH_HINT_DIRECTIONS` and no more, so on
    a memo of ten directions the hint half is simply false;
  * it reported `len(unanswered)` as "%d already open", which is the CAP-OCCUPANCY subset and not
    the dedup universe, so a board of three taken-up directions restated by a memo logged
    "3 of 3 … (0 already open, cap 5)" — nothing open, room for five, all three refused.

So every assertion here is made on a CAPTURED RECORD. No comment can satisfy any of it, and the
message's content is checked where an operator would read it. The house precedent is
`test_memo_keeps_what_validated.py::test_the_REFUSAL_is_recorded_and_not_silent`, whose own
docstring cites `_admissible_beliefs` as the pattern to copy.

The two package-wide sweeps that remain go through `tests/_source_scan.py::iter_sources` /
`iter_trees` rather than a private `rglob`, which is not tidiness: the shared walk decodes
`utf-8-sig` with `errors="replace"` and skips `.ipynb_checkpoints`, and this file's first cut did
neither — so a stale Jupyter autosave holding the PRE-FIX `_LOG.info(` would have failed the INFO
bound with no source change able to make it pass.
"""
from __future__ import annotations

import ast
import logging
import types

from _source_scan import PKG, iter_sources, iter_trees

from looplab.core.claimpin import text_without_markers

from looplab.core.models import Card, CardSelectionProvenance, RunState
from looplab.engine import research_cadence as rc
from looplab.engine.research_cadence import (DEEP_RESEARCH_HINT_DIRECTIONS,
                                             DEEP_RESEARCH_OPEN_BELIEF_CAP,
                                             deep_research_hint_text)

_LOGGER = "looplab.engine.research_cadence"


# --------------------------------------------------------------------------------------------
# Driving `_admissible_beliefs` without an engine. It reads exactly one thing off `self` —
# `self.store.read_all()` — and hands it to the module-level `fold`, so a stub `self` plus the
# documented `research_cadence.fold` seam is the whole harness. `fold` is patched rather than fed a
# synthetic event log because the board this method reasons about is a `RunState`, and building one
# directly is what `test_direction_board_cap.py` already does.
# --------------------------------------------------------------------------------------------

def _direction(cid: str, statement: str) -> Card:
    return Card(id=cid, statement=statement, seed_statement=statement,
                selection_provenance=CardSelectionProvenance())


def _child_of(cid: str, parent: str) -> Card:
    """An experiment answering a direction — what makes that direction TAKEN UP."""
    return Card(id=cid, statement="an experiment", seed_statement="an experiment",
                parent_card_id=parent,
                selection_provenance=CardSelectionProvenance(
                    action_source="card_added", action_owner_count=1))


def _emit(monkeypatch, caplog, cards, directions, *, fold_raises: bool = False):
    """Run the real method and return `(admitted, [record, …])` captured at WARNING.

    Capturing AT WARNING is the mutation that matters: put the call back to `_LOG.info` and the
    logger's effective level (WARNING, inherited from an unconfigured root) drops the record before
    any handler sees it, so `records` comes back empty and every test below goes red.
    """
    state = RunState(goal="g", direction="max")
    state.cards = {c.id: c for c in cards}

    def _fold(_events):
        if fold_raises:
            raise RuntimeError("the fold raised — the board is UNKNOWN, not empty")
        return state

    monkeypatch.setattr(rc, "fold", _fold)
    engine = types.SimpleNamespace(store=types.SimpleNamespace(read_all=lambda: []))
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        admitted = rc.ResearchCadenceMixin._admissible_beliefs(engine, directions)
    return admitted, [r for r in caplog.records if r.name == _LOGGER]


def _message(monkeypatch, caplog, cards, directions, **kw) -> str:
    admitted, records = _emit(monkeypatch, caplog, cards, directions, **kw)
    assert records, (
        "no record survived a WARNING capture — the operator learns nothing when a paid memo's "
        "directions are refused. This is what an `_LOG.info` looks like from the outside.")
    assert len(records) == 1, f"one refusal, one line; got {[r.getMessage() for r in records]}"
    assert len(directions) - len(admitted) > 0, "the fixture must actually drop something"
    return records[0].getMessage()


def test_a_dropped_direction_is_reported_at_a_level_that_SHOWS(monkeypatch, caplog):
    """The property, driven. A full board plus one more question is the ordinary refusal."""
    board = [_direction(f"d{i}", f"direction {i}") for i in range(DEEP_RESEARCH_OPEN_BELIEF_CAP)]
    msg = _message(monkeypatch, caplog, board, ["a genuinely new question"])
    assert "not registered as beliefs" in msg
    assert f"cap {DEEP_RESEARCH_OPEN_BELIEF_CAP}" in msg, (
        "the operator cannot act on a refusal whose bound is not named")


def test_the_line_does_NOT_claim_the_hint_still_carries_the_dropped_directions(monkeypatch, caplog):
    """The memo body is the record; the hint is a bounded PUSH and says so in its own docstring.

    NON-VACUITY: the fixture offers more directions than the hint can carry, so a line promising
    the hint carries "them" is provably false about this very call.
    """
    offered = [f"question {i}" for i in range(DEEP_RESEARCH_HINT_DIRECTIONS + 3)]
    assert offered[-1] not in deep_research_hint_text(offered), (
        "the hint bound moved — re-derive this guard against `deep_research_hint_text`, which is "
        "what makes 'the hint still carries them' a falsifiable claim about this very call")
    board = [_direction(f"d{i}", f"direction {i}") for i in range(DEEP_RESEARCH_OPEN_BELIEF_CAP)]
    msg = _message(monkeypatch, caplog, board, offered)
    assert "hint" not in msg.lower(), (
        "the line tells the operator the hint carries the dropped directions; it carries the first "
        f"{DEEP_RESEARCH_HINT_DIRECTIONS} and this memo offered {len(offered)}")
    assert "memo body" in msg, "the line must still name where the directions ARE kept"


def test_the_reported_open_count_can_EXPLAIN_the_refusal(monkeypatch, caplog):
    """The dedup universe and the cap-occupancy set are two populations, and the line names both.

    MUTATION: report `len(unanswered)` alone, as this did, and the message reads "3 of 3 … (0 open,
    cap 5)" — a refusal that contradicts every number offered to explain it.
    """
    statements = ["distil from a teacher", "mine harder negatives", "tune the loss temperature"]
    board = [_direction(f"d{i}", s) for i, s in enumerate(statements)]
    board += [_child_of(f"card-{i}", f"d{i}") for i in range(len(statements))]
    # Restated with different case/spacing: the same normalized belief key, a different string.
    msg = _message(monkeypatch, caplog, board, [s.upper() + "  " for s in statements])
    assert f"{len(statements)} open" in msg, (
        f"all {len(statements)} directions were refused as duplicates of the open board, but the "
        f"line reports: {msg}")
    assert "0 of them unanswered" in msg, (
        "every open direction here has a child, so none of them occupies a cap slot — the line has "
        "to be able to say that, or the cap looks like the reason")


def test_a_board_that_could_not_be_READ_is_unknown_and_not_empty(monkeypatch, caplog):
    """The `except` degrades admission to the pre-bound behaviour on purpose; the two empty lists
    it leaves are a FALLBACK, not a measurement. Reporting "0 open" off them tells the operator the
    board was clear when the fold in fact raised."""
    offered = [f"question {i}" for i in range(DEEP_RESEARCH_OPEN_BELIEF_CAP + 2)]
    msg = _message(monkeypatch, caplog, [], offered, fold_raises=True)
    assert "board: unreadable" in msg, f"a fold that raised is reported as a clear board: {msg}"
    assert "0 open" not in msg


# --------------------------------------------------------------------------------------------
# The premise, and the bound that keeps it true. Both sweep the package through the SHARED walk.
# --------------------------------------------------------------------------------------------

# Call-shaped on purpose: what must not come back is the TEXT of a logging configuration, and a
# negative pin is the one place CLAUDE.md keeps substrings ("a commented-out copy is as much of a
# drift risk as a live one"). The `(` and `=` are what keep this file's own prose — and the
# `research_cadence.py` comment naming these very spellings — from falsifying it.
#
# Read through `claimpin.text_without_markers`, which is not decoration: `research_cadence.py`
# carries the same premise as a pinned claim, and that pin's own deciding clause spells one of
# these very literals inside the file this scans. Without the shared marker-stripping rule the pin
# and this guard falsify each other. One implementation, for exactly the reason its docstring
# gives — "so a marker can neither satisfy nor falsify itself, in EITHER index".
_CONFIGURES = ("basicConfig(", "dictConfig(", "fileConfig(", ".setLevel(", "log_level=")

# The ONE site in the package that hands a level to a logging framework, named so this guard SEES
# it instead of missing it. `uvicorn.run(..., log_level="info")` runs uvicorn's default
# LOGGING_CONFIG, which configures the three `uvicorn*` loggers and their level; the ROOT logger is
# left alone and `disable_existing_loggers` is False, so a `looplab.*` record still reaches no
# handler and still inherits the default WARNING. If that ever grows a root handler, this is the
# exemption to delete — and the level choice below can then be revisited on purpose.
_EXEMPT = {"serve/server.py"}


def test_nothing_configures_logging_so_INFO_reaches_nobody():
    """The premise, asserted rather than assumed."""
    configured, scanned = {}, 0
    for path, _text in iter_sources():
        scanned += 1
        rel = str(path.relative_to(PKG))
        text = text_without_markers(path)
        hits = {spelling for spelling in _CONFIGURES if spelling in text}
        if hits and rel not in _EXEMPT:
            configured[rel] = sorted(hits)
    assert scanned > 1, "the package walk found nothing — this guard would pass on an empty tree"
    assert not configured, (
        f"logging is configured in {configured} — re-check whether INFO now reaches an operator")
    assert logging.getLogger(_LOGGER).getEffectiveLevel() >= logging.WARNING


# `logging` itself is in the set because `logging.info(...)` is the module-level shortcut onto the
# ROOT logger — the same invisible line under a different spelling. Matching on the LAST dotted
# segment is what keeps `catalog.info(...)` out of it.
_LOGGER_RECEIVERS = frozenset({"_log", "log", "logger", "_logger", "logging"})


def _receiver(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _info_calls(tree: ast.AST) -> list[int]:
    """Every `<logger>.info(...)` call in *tree*, by line. AST, so a comment cannot produce one."""
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "info"
            and _receiver(node.func.value).rsplit(".", 1)[-1].lower() in _LOGGER_RECEIVERS]


def test_no_operator_line_in_the_package_is_written_at_INFO():
    """A second INFO line would be the same defect again, and the first cut of this guard bounded
    only the literal `_LOG.info(` — so `logger.info(...)`, `logging.info(...)` and `self._log.info(
    ...)` all walked past a test whose message claimed the package held none."""
    assert _info_calls(ast.parse("_LOG.info('a')\nlogger.info('b')\nlogging.info('c')\n"
                                 "self._log.info('d')\ncatalog.info('e')\n")) == [1, 2, 3, 4], (
        "the detector does not detect — every assertion below it would be vacuous")
    infos, scanned = [], 0
    for path, tree in iter_trees():
        scanned += 1
        infos += [f"{path.relative_to(PKG)}:{n}" for n in _info_calls(tree)]
    assert scanned > 1, "the package walk found nothing — this guard would pass on an empty tree"
    assert not infos, (
        "these lines are written at INFO and an unconfigured root logger discards them — either "
        "raise them or accept they are for a debugger only:\n  " + "\n  ".join(infos))
