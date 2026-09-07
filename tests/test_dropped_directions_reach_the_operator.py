"""The line that says "not silent" is not silent — and what it says is true.

`looplab/cli/__init__.py::_configure_cli_logging` installs WARNING by default (overridable with
`LOOPLAB_LOG_LEVEL`, and a UI-launched engine comes through the same entry point), so an
`_LOG.info(...)` still reaches nobody on a run nobody asked to be verbose — and the one line telling
the operator that a paid memo's directions did not all become board rows was written at exactly that
level, under a comment reading "Not silent". THAT ENTRY POINT DID NOT EXIST when this file was first
written: nothing in `looplab/` configured logging at all, so `logging.lastResort` — fixed at WARNING,
writing the bare message with no level or logger on it — was what put a record on stderr, and the
package had exactly one usable level. The first cut of this file pinned that absence as an
invariant, which made the general fix cost deleting a test; the guards at the bottom hold the fix
instead: logging is configured ONCE, at the entry point, and its default is still WARNING.

DRIVEN, NOT PINNED. CLAUDE.md's guard ladder puts "drive the property" first and "AST, never
substrings" last, and the first cut of this file sat below the bottom rung: it read
`research_cadence.py` as TEXT and asserted a `_LOG.<level>(` spelling near a message literal. That
is not the property — the property is that a record ARRIVES at a level the shipped configuration
keeps — and reading source instead of driving it cost every defect below, because nobody who only
greps for `_LOG.warning(` ever reads the sentence being emitted:

  * the line told the operator "the memo and its hint still carry them", which
    `admit_research_beliefs`' own docstring had already retracted in writing —
    `deep_research_hint_text` carries the first `DEEP_RESEARCH_HINT_DIRECTIONS` and no more, so on
    a memo of ten directions the hint half is simply false;
  * it reported `len(unanswered)` as "%d already open", which is the CAP-OCCUPANCY subset and not
    the dedup universe, so a board of three taken-up directions restated by a memo logged
    "3 of 3 … (0 already open, cap 5)" — nothing open, room for five, all three refused;
  * it called them "recommended direction(s)" while being handed `questions`, i.e. `open_questions`
    whenever the memo filled it — pointing the operator at a field carrying none of the refused
    items, and one that is optional and defaults to `[]`;
  * it reported ONE number, the sum, and then offered the board and the cap to explain it — true of
    two of the four causes and false of the two that are facts about the MEMO, so a memo listing one
    question twice raised an operator-facing alarm about a provably empty board.

So every assertion here is made on a CAPTURED RECORD or on a real subprocess. No comment can satisfy
any of it, and the message's content is checked where an operator would read it. The house precedent
is `test_memo_keeps_what_validated.py::test_the_REFUSAL_is_recorded_and_not_silent`, whose own
docstring cites `_admissible_beliefs` as the pattern to copy.

The package-wide sweep goes through `tests/_source_scan.py::iter_sources` rather than a private
`rglob`, which is not tidiness: the shared walk decodes `utf-8-sig` with `errors="replace"` and skips
`.ipynb_checkpoints`, and this file's first cut did neither — so a stale Jupyter autosave holding the
PRE-FIX `_LOG.info(` would have failed the old INFO bound with no source change able to make it pass.
"""
from __future__ import annotations

import logging
import types

from _source_scan import PKG, iter_sources

from looplab.core.claimpin import text_without_markers

from looplab.core.models import Card, CardSelectionProvenance, RunState
from looplab.engine import research_cadence as rc
from looplab.engine.research_cadence import (DEEP_RESEARCH_HINT_DIRECTIONS,
                                             DEEP_RESEARCH_OPEN_BELIEF_CAP,
                                             deep_research_hint_text)

# DERIVED, never typed. `logging.getLogger(<any name>)` mints a fresh logger inheriting root,
# so a hard-coded string keeps answering after a rename while naming a logger nothing emits
# through — and this repo moves modules (`looplab/__init__.py::_RENAMED` exists for that).
_LOGGER = rc._LOG.name


# --------------------------------------------------------------------------------------------
# Driving `_admissible_beliefs` without an engine. It reads exactly one thing off `self` —
# `self.store.read_all()` — and hands it to the module-level `fold`, so a stub `self` plus the
# documented `research_cadence.fold` seam is the whole harness. `fold` is patched rather than fed a
# synthetic event log because the board this method reasons about is a `RunState`, and building one
# directly is what `test_direction_board_cap.py` already does.
# --------------------------------------------------------------------------------------------

def _stub_engine():
    """A cadence stand-in that carries the REAL `_record_belief_admission`.

    `_admissible_beliefs` gained a collaborator (the durable admission receipt, 86d30e41), and a
    bare namespace no longer satisfies it. Binding the real method rather than stubbing a no-op
    keeps these tests exercising the receipt's own try/except: a store that cannot append must
    cost a diagnostic row and never the memo's directions, which is exactly what this file is
    here to protect.
    """
    engine = types.SimpleNamespace(
        store=types.SimpleNamespace(read_all=lambda: [], append=lambda *a, **k: None))
    engine._record_belief_admission = (
        rc.ResearchCadenceMixin._record_belief_admission.__get__(engine))
    return engine


def _direction(cid: str, statement: str) -> Card:
    return Card(id=cid, statement=statement, seed_statement=statement,
                selection_provenance=CardSelectionProvenance())


def _child_of(cid: str, parent: str) -> Card:
    """An experiment answering a direction — what makes that direction TAKEN UP."""
    return Card(id=cid, statement="an experiment", seed_statement="an experiment",
                parent_card_id=parent,
                selection_provenance=CardSelectionProvenance(
                    action_source="card_added", action_owner_count=1))


def _emit(monkeypatch, caplog, cards, directions, *, fold_raises: bool = False,
          channel: str | None = None, engine=None):
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
    engine = engine or _stub_engine()
    kw = {} if channel is None else {"channel": channel}
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        admitted = rc.ResearchCadenceMixin._admissible_beliefs(engine, directions, **kw)
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
    assert "recommended direction(s)" in msg, (
        "no channel passed is the FALLBACK channel — a memo that drew no distinction really\n"
        "is offering `recommended_directions`, and that reading must not move")


def test_the_line_names_the_memo_FIELD_the_refused_items_ARE_in(monkeypatch, caplog):
    """The noun is not decoration — it is the only thing telling the operator where to look.

    `_admissible_beliefs` has been handed `questions` since the 2026-08-27 channel split
    (`open_questions` when the memo filled it, `recommended_directions` otherwise) while its
    message said "recommended direction(s)" about both. On the ordinary memo shape that
    points at the field holding NONE of the refused items — and `recommended_directions` is
    optional and defaults to `[]`, so it routinely points at an empty list. Promoting the
    line to WARNING is what made a wrong noun cost something: the operator now reads it.
    """
    board = [_direction(f"d{i}", f"direction {i}") for i in range(DEEP_RESEARCH_OPEN_BELIEF_CAP)]
    msg = _message(monkeypatch, caplog, board, ["a genuinely new question"],
                   channel="open question")
    assert "open question(s)" in msg, msg
    assert "recommended direction" not in msg, (
        "the refused items came from `open_questions`; naming `recommended_directions` sends "
        f"the operator to a field that carries none of them: {msg}")


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


def test_the_line_says_WHY_each_direction_was_dropped(monkeypatch, caplog):
    """One number was four causes, and the account offered — the board and the cap — is true of two.

    MUTATION: report the sum alone, as this did, and a memo that merely repeated itself raises an
    operator-facing alarm naming a board the same line proves is empty.
    """
    msg = _message(monkeypatch, caplog, [], ["mine harder negatives", "MINE HARDER NEGATIVES"])
    assert "the memo repeated itself" in msg, msg
    assert "already on the board" not in msg and "no room" not in msg, (
        f"the board refused nothing and the cap never bound; naming them is the false alarm: {msg}")
    assert "0 open" in msg, "the line still has to say what the board held"


def test_the_board_and_the_cap_are_named_when_they_ARE_the_reason(monkeypatch, caplog):
    """The complement, so the test above cannot be satisfied by dropping the causes altogether."""
    board = [_direction(f"d{i}", f"direction {i}") for i in range(DEEP_RESEARCH_OPEN_BELIEF_CAP)]
    msg = _message(monkeypatch, caplog, board, ["direction 0", "a genuinely new question"])
    assert "already on the board" in msg and "no room" in msg, msg


def test_the_SAME_sentence_is_not_repeated_for_the_life_of_the_run(monkeypatch, caplog):
    """Research repeats on a timer, and once the board fills every later memo is refused whole — so
    the unbounded form prints one WARNING per tick into the stream carrying the package's genuine
    degradations. Bounded on the rendered CONTENT and not on the call site, which is what keeps it
    from hiding anything: any number that moves is a different sentence and speaks."""
    board = [_direction(f"d{i}", f"direction {i}") for i in range(DEEP_RESEARCH_OPEN_BELIEF_CAP)]
    engine = _stub_engine()
    said = []
    for _ in range(3):
        caplog.clear()
        _, records = _emit(monkeypatch, caplog, board, ["a sixth question"], engine=engine)
        said.append([r.getMessage() for r in records])
    assert len(said[0]) == 1 and said[1] == [] and said[2] == [], (
        f"the identical refusal was printed on every tick: {said}")

    # …and a refusal that reads DIFFERENTLY speaks again, which is the whole difference between
    # this and a once-per-site bound. Same board, a memo that now also restates one of its rows:
    # a different count and a different cause, so a different sentence.
    caplog.clear()
    _, records = _emit(monkeypatch, caplog, board, ["a sixth question", "direction 0"],
                       engine=engine)
    assert len(records) == 1, "a changed refusal reported nothing — the bound is hiding a change"
    assert "already on the board" in records[0].getMessage()


# --------------------------------------------------------------------------------------------
# The premise — which MOVED, and the move is the general fix. There used to be no logging
# configuration anywhere in `looplab/`, so the level of every site in the package was Python's
# default rather than anyone's choice: `lastResort` (fixed at WARNING, and writing the bare message
# with no level or logger on it) was what put a record on stderr, INFO reached nobody on any run,
# and an author with a genuinely informational line had to inflate it to WARNING — level inflation
# in the one stream carrying real degradations — or bury it where nothing could show it. The first
# cut of this file pinned that absence as an invariant, which made the general fix cost deleting a
# test. `cli/__init__.py::_configure_cli_logging` is the fix; what these two guards hold now is that
# it stays ONE decision and that its default did not quietly change under the line above.
# --------------------------------------------------------------------------------------------

# Call-shaped on purpose: what must not spread is the TEXT of a logging configuration, and a
# negative pin is the one place CLAUDE.md keeps substrings ("a commented-out copy is as much of a
# drift risk as a live one"). The `(` and `=` are what keep this file's own prose — and the
# `research_cadence.py` comment naming these very spellings — from falsifying it.
#
# Read through `claimpin.text_without_markers`, which is not decoration: `research_cadence.py`
# carries the level premise as a pinned claim, and that pin's deciding clause spells one of these
# literals. Without the shared marker-stripping rule the pin and this guard falsify each other. One
# implementation, for exactly the reason its docstring gives — "so a marker can neither satisfy nor
# falsify itself, in EITHER index".
_CONFIGURES = ("basicConfig(", "dictConfig(", "fileConfig(", ".setLevel(", "log_level=")

# THE ONE SITE ALLOWED TO CONFIGURE LOGGING, and it is required to — see the non-vacuity assertion.
_ENTRY_POINT = "cli/__init__.py"

# The one OTHER site that hands a level to a logging framework, named so this guard SEES it instead
# of missing it. `uvicorn.run(..., log_level="info")` runs uvicorn's default LOGGING_CONFIG, which
# configures the three `uvicorn*` loggers and their level; the ROOT logger is left alone and
# `disable_existing_loggers` is False, so it neither grants nor withholds anything from a `looplab.*`
# record. If it ever grows a root handler, this is the exemption to delete.
_EXEMPT = {"serve/server.py"}


def test_logging_is_configured_ONCE_and_at_the_ENTRY_POINT():
    """One decision, not one per call site — which is the whole content of the general fix.

    Thirty-nine warning sites were reachable and every other level was not, because nobody had
    decided anything; the remedy is a single `basicConfig` where the CLI is invoked, not a level
    argued afresh at each site. A second configuring site anywhere in the package is that decision
    being taken twice, and the two will disagree.
    """
    configured, scanned = {}, 0
    for path, _text in iter_sources():
        scanned += 1
        rel = str(path.relative_to(PKG))
        text = text_without_markers(path)
        hits = {spelling for spelling in _CONFIGURES if spelling in text}
        if hits and rel not in _EXEMPT:
            configured[rel] = sorted(hits)
    assert scanned > 1, "the package walk found nothing — this guard would pass on an empty tree"
    assert _ENTRY_POINT in configured, (
        f"{_ENTRY_POINT} no longer configures logging — every level in the package is back to being "
        "Python's default, and an INFO line reaches nobody again")
    assert set(configured) == {_ENTRY_POINT}, (
        f"logging is configured outside the entry point: {configured} — one decision, one place")

    # …and nothing of OURS overrides it. `getEffectiveLevel()` was the wrong probe twice over: it
    # reads a level pytest itself lowers (`--log-cli-level=INFO` sets root to INFO and turned this
    # red for a debugging flag, with a message blaming the package), and it answers WARNING for a
    # logger nobody emits through. What the premise needs is that no logger on our chain sets a
    # level or attaches a handler, which is what leaves every record to the ONE decision above — and
    # a handler is the half that decides delivery once one exists.
    chain = [_LOGGER]
    while "." in chain[-1]:
        chain.append(chain[-1].rsplit(".", 1)[0])
    for name in chain:
        owned = logging.getLogger(name)
        assert owned.level == logging.NOTSET, f"{name} sets its own level"
        assert not owned.handlers, f"{name} has a handler of its own"


# DRIVEN IN A SUBPROCESS, THROUGH THE REAL ENTRY POINT. Two reasons, and the second is what a
# mutation pass found: logging is process-global, so configuring it inside the pytest process would
# install a handler on the session's root logger and change what every later test sees — and a probe
# that calls `_configure_cli_logging()` DIRECTLY still passes when the entry point stops calling it,
# which is the whole failure mode ("the call is present in the text" vs "the call runs", CLAUDE.md's
# note on what tier 3 does not prove). So this builds a real `_TotalOutputTyper`, registers one
# command and invokes it: the level arrives the way it arrives for `looplab run`, and for a
# UI-launched engine, which `serve/engine_proc.py` spawns as `python -m looplab.cli`.
_PROBE = """
import logging
from looplab.cli import _TotalOutputTyper

app = _TotalOutputTyper()

@app.command()
def noop():
    _log = logging.getLogger("looplab.engine.research_cadence")
    _log.warning("a real degradation")
    _log.info("routine chatter")

try:
    app([])
except SystemExit:
    pass
"""


def _stderr_of(level: "str | None") -> str:
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if k != "LOOPLAB_LOG_LEVEL"}
    if level is not None:
        env["LOOPLAB_LOG_LEVEL"] = level
    done = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True, text=True, env=env)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stderr


def test_the_DEFAULT_still_shows_a_WARNING_and_still_hides_an_INFO():
    """The default is what the line above rests on, so the knob must not have moved it.

    This is what `research_cadence.py` pins under the slug `cli-logs-at-warning-by-default`
    (spelled without its marker on purpose — a slug names exactly ONE claim, and writing the
    token here declares a second): adding a level knob changed what an operator MAY ask for and
    deliberately nothing about what they get without asking. If this fails, the comment
    justifying that line's level is false.
    """
    out = _stderr_of(None)
    assert "a real degradation" in out, "the default hides the records the package actually emits"
    assert "routine chatter" not in out, (
        "INFO now reaches an operator who did not ask — the deep-research line's level was chosen "
        "on the premise that it does not")
    assert "WARNING looplab.engine.research_cadence" in out, (
        "the record arrives without its level and logger, i.e. as `lastResort` wrote it: "
        "indistinguishable from a stray print and ungreppable by level")


def test_an_operator_CAN_ask_for_INFO_which_is_what_makes_the_level_a_choice():
    """The other half: an informational line is now a deployment choice rather than dead text.

    Without this the package still has exactly one usable level and the fix is cosmetic.
    """
    assert "routine chatter" in _stderr_of("INFO")


def test_an_unusable_LEVEL_degrades_and_says_so_rather_than_killing_the_CLI():
    """It runs before `super().__call__`, i.e. outside `_RefusalBoundaryGroup`, so a raise here is
    not a refusal an operator reads — it is a raw traceback at exit 1, the presentation that
    boundary exists to remove. Degrading silently would be the other failure: a shell exporting a
    typo'd level would run at a level it did not ask for and never learn."""
    out = _stderr_of("verbose")
    assert "LOOPLAB_LOG_LEVEL" in out and "not a logging level" in out, out
    assert "a real degradation" in out, "the degradation dropped the records it fell back to keeping"
    assert "routine chatter" not in out
