"""The question board, as something an agent can ASK for.

THE GAP THIS CLOSES, measured 2026-08-26 over the whole tool surface: 83 `fn_spec` tools and not one
reads the run's questions. The concept tools (`read_concept_tree`, `find_concept_slugs`,
`concept_card`, …) read the TAXONOMY; `read_research_memo` reads the memo that PRODUCED a question.
Neither is the board.

Until now the only channel was PUSH — `agents/roles.py::_state_brief` splices an "OPEN RESEARCH
DIRECTIONS" block with `DIRECTION_ID=` and the contract to file an experiment under one. That works
(v6's card-0 came back carrying a parent) and has three limits: it is bounded by whatever the brief
chooses to include with no way to ask for more; it reaches only the roles the engine pushes it to;
and the same brief also feeds crash-triage and the macro-action chooser, whose replies carry no
`card_id` field at all — that file's own comment says the claim contract "is an instruction that
cannot be followed" for them.

WHO WAS BLIND, and it is not the role you would guess. `RunTools` — which owns `list_experiments`
and `read_experiment` — is built at `agents/factory.py` for the RESEARCHER only. The Developer's
`_scout_tools` builds `RepoScoutTools`, `DevCommandTools`, `DevProbeTools`, `CrossRunTools`,
`MemoryTools` and `EnvInspectTools`, and no `RunTools`; the `read_run_experiment` calls visible in
its `stages`/`plan`/`card_build` phases are a FOREIGN-run reader. So the role that writes an
experiment's code cannot see the question that experiment answers, and the repair path cannot see
whether a sibling experiment under the same question already hit the same wall.

A NARROW PROVIDER RATHER THAN A WIDER GRANT. Handing the Developer `RunTools` wholesale would also
hand it `list_experiments`, `read_code` and the rest — a far larger change in what that role may do
than "let it read the questions". One provider, one tool, wired at both call sites, grants exactly
the reach the gap describes.

IT RECORDS NOTHING AND DECIDES NOTHING. Every field it returns is already on the Card since the
direction edge, the concept envelope and the fold fix (#41 / #52 / #66): the fold is untouched, no
event is written, and no metric, champion, selectability or violation can move on it. It is a
reader, in the sense `tools/log_tools.py` uses the word.
"""
from __future__ import annotations

from typing import Optional

from looplab.core.cards import card_is_direction
from looplab.core.models import RunState
from looplab.tools._base import clip, fit_rows, fn_spec

#: A question's statement is prose an agent wrote; the board is a listing, not a reading surface.
_STATEMENT_CHARS = 240
#: Per question. A direction with two hundred experiments under it is real (`CARD_CHILD_LIMIT` is
#: 256) and printing them all would spend the whole result cap on one row.
_CHILDREN_SHOWN = 8


def _text(value: object, cap: int = _STATEMENT_CHARS) -> str:
    return clip(str(value or "").strip().replace("\n", " "), cap)


def _delta(card) -> str:
    """What this experiment measured, or an honest blank.

    Absent is SAID and never rendered as a zero — an experiment that produced no number and one that
    produced no improvement are different findings, and this listing is read by a role deciding what
    to write next.
    """
    value = getattr(card, "best_delta", None)
    if not isinstance(value, float) or value != value or value in (float("inf"), float("-inf")):
        return "—"
    return f"{value:+g}"


class QuestionBoardTools:
    """One reader over the run's OWN question board. `bind_state` per the provider contract."""

    def __init__(self) -> None:
        self.state: Optional[RunState] = None

    # `parent` is accepted and ignored, exactly as `RunTools` does: the second argument is part of
    # the `bind_state` contract (`tools/_base.py` — a provider implementing the hook without it
    # raises TypeError at dispatch) and this provider has no use for a back-reference.
    def bind_state(self, state: RunState, parent=None) -> None:
        self.state = state

    def specs(self) -> list[dict]:
        return [
            fn_spec("read_questions",
                "The run's open RESEARCH QUESTIONS (broad directions) and the experiments filed "
                "under each — with what those experiments measured. Use it to see which question "
                "the work you are doing answers, and whether a sibling experiment under the same "
                "question already tried what you are about to try. A question is not runnable as "
                "it stands; the experiments under it are.",
                {"question_id": {"type": "string",
                                 "description": "one question's id, for its children in full "
                                                "(omit for the whole board)"}}),
        ]

    # `execute`, NOT `call`. `tools/_base.py::ToolProvider` is a STRUCTURAL protocol — "no provider
    # inherits this" — so a wrong name is checked by nothing at import or construction and surfaces
    # only when a live agent dispatches. Shipped as `call` in b5302649 and measured on the first run
    # that loaded it: the deep-research stage died on dispatch and its memo read
    # "(deep research unavailable: 'QuestionBoardTools' object has no attribute 'execute')" — zero
    # findings, zero directions, zero questions, the run's whole research input gone. Ten unit tests,
    # the neighbourhood suite and a clean import all passed, because none of them dispatched.
    def execute(self, name: str, args: dict) -> str:
        if name != "read_questions":
            return f"unknown tool: {name}"
        state = self.state
        if state is None or not isinstance(getattr(state, "cards", None), dict):
            return "no run state bound"
        cards = state.cards
        questions = [c for c in cards.values() if card_is_direction(c)]
        wanted = str(args.get("question_id") or "").strip()
        if wanted:
            questions = [c for c in questions if str(getattr(c, "id", "")) == wanted]
            if not questions:
                return f"no question with id {wanted!r} on this board"

        children_of: dict[str, list] = {}
        for card in cards.values():
            parent = str(getattr(card, "parent_card_id", "") or "")
            if parent:
                children_of.setdefault(parent, []).append(card)

        rows: list[str] = []
        for question in sorted(questions, key=lambda c: str(getattr(c, "id", ""))):
            qid = str(getattr(question, "id", ""))
            tags = [t for t in (getattr(question, "concept_tags", None) or []) if isinstance(t, str)]
            kids = sorted(children_of.get(qid, []), key=lambda c: str(getattr(c, "id", "")))
            rows.append(f"QUESTION_ID={qid} {_text(getattr(question, 'seed_statement', ''))}")
            if tags:
                rows.append(f"    concepts: {', '.join(tags)}")
            if not kids:
                # The most actionable row on the board, and it says so rather than being omitted for
                # being empty — the same choice the operator's Research ladder makes.
                rows.append("    (no experiment filed under this yet)")
            # The count is the TRUE one even where the shown list is clipped, so an agent reasoning
            # about "has this been tried" is never told a smaller number than the board holds.
            shown = kids[:_CHILDREN_SHOWN] if not wanted else kids
            for kid in shown:
                rows.append(
                    f"    - {getattr(kid, 'id', '')} [{getattr(kid, 'status', '') or '?'}"
                    f"/{getattr(kid, 'verdict', '') or '?'}] delta={_delta(kid)} "
                    f"{_text(getattr(kid, 'seed_statement', ''), 140)}")
            if len(kids) > len(shown):
                rows.append(f"    … {len(kids) - len(shown)} more experiment(s) under this question "
                            f"(read_questions(question_id=\"{qid}\") for all)")

        if not rows:
            # NOT an error and NOT an empty board: before the opening memo is written a run has no
            # questions, and saying "none" as though something were missing would misreport a
            # healthy run's first minutes.
            return ("no research question registered on this board yet — the opening research memo "
                    "has not produced any, so there is nothing to file work under")
        # A LIST header, not a string:  uses a str verbatim and leaves the caller to own
        # its newline, which ran the count into the first question's id.
        return fit_rows([f"open research questions: {len(questions)}"], rows)
