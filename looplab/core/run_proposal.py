"""`RunProposal` — the ONE shape of a proposed-but-not-yet-launched run.

Doc 27 ("Three planning stacks") measured three New-run planners that shared task-adapter validation
and the backend default but nothing else: the Web main menu authors a card with the owner
Assistant's `propose_run`, the TUI asks the server's agentic `/api/genesis` job, and
`looplab run --goal` calls `engine/genesis.py`. Three planners is not the defect — they are three
different ways to AUTHOR a plan (a tool call, a server job, one CLI model call) and each earns its
existence. The defect was that each then spelled the PROPOSAL, the `/api/start` body, the run-id
slug and the settings filter for itself, so a move of the task schema had to be repaired in three
places and a card built by one surface could not be read by another.

This module is that shared schema, and it is deliberately the only new spelling: it does not
validate, it does not decide readiness and it does not launch. "Is this launchable" has exactly one
answer and it is the server's — `serve/launch.py::preflight_start`, asked as a verdict through
`serve/launch.py::validate_launch` (`POST /api/validate`) and as a refusal through `/api/start`.
`validate_proposal` there takes one of these and hands it to that same funnel, so a client that
holds a `RunProposal` never hand-builds a body to ask about.

It lives in `core/` because all three surfaces must reach it and they sit in three different
packages: `serve/` (the genesis card and the TUI's launch body), `tools/` (the Web assistant's
`propose_run`, which may not import `serve` — doc 25 XP-03) and `cli/`. It imports only
`core/config.py`, for the one thing a settings filter cannot make up: which field names exist.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from looplab.core.config import Settings

# The launch-settings field policy, defined here because three surfaces filter by it and one of them
# (`tools/`) may not import `serve`. `serve/settings_store.py` re-exports both under their historical
# private names, so it stays the ONE definition rather than the second one.
LAUNCH_SETTING_FIELDS: frozenset[str] = frozenset(Settings.model_fields)
# Runtime-only and never carried in a proposal, a UI settings document or a run snapshot: only the
# key is an HTTP-writeable secret, and the server derives and persists its endpoint binding beside it.
LAUNCH_SECRET_FIELDS: frozenset[str] = frozenset({"llm_api_key", "llm_api_key_base_url"})

# WHO authored a proposal. Provenance for the operator and for a reader of a saved card — never
# authority: every planner's output goes through the same launch funnel and earns the same refusals.
PROPOSAL_PLANNERS: tuple[str, ...] = ("cli", "tui", "web")

# A card's operator-facing readiness notes are bounded; a planner that emits 200 of them is not
# giving the operator a checklist, it is giving them a wall.
MAX_SETUP_STEPS = 12

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug_run_id(value: Any) -> str:
    """The run-id normaliser: lowercase kebab, at most 40 chars.

    One spelling. `serve/tui_format.py::slug` and `serve/routers/genesis.py::_normalize_genesis`
    each had their own and carried a comment saying they must stay in step — which is the shape of a
    rule that eventually will not. They differed already: the genesis copy stripped ONE leading and
    ONE trailing dash (`re.sub(r"(^-|-$)", "", …)`) where the TUI stripped all of them, so
    `"--a--"` slugged to `"-a-"` on one surface and `"a"` on the other. The strip-all reading wins:
    a leading dash in a run id is a filesystem hazard the launch funnel refuses, so producing one
    was never the better answer.
    """
    return _SLUG_RE.sub("-", str(value or "").lower()).strip("-")[:40]


def normalize_launch_settings(settings: Any, *, prior: Any = None) -> dict:
    """The settings a launch CARD may carry: known, non-secret, non-null, later layer wins.

    `prior` is the card being refined — a planner that emits `{settings: {max_nodes: 50}}` must tweak
    the operator's tuned card, not replace it. Deliberately NOT applied by `start_body()`: an unknown
    or secret key must reach the server, which names it in a 422 the operator can act on, rather than
    being dropped here into a launch that silently ignored what they asked for.
    """
    merged = {**_as_mapping(prior), **_as_mapping(settings)}
    return {key: value for key, value in merged.items()
            if key in LAUNCH_SETTING_FIELDS and key not in LAUNCH_SECRET_FIELDS
            and value is not None}


def _as_mapping(value: Any) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _clean_steps(value: Any) -> tuple[str, ...]:
    steps = value if isinstance(value, (list, tuple)) else ()
    return tuple(step for step in (str(item or "").strip() for item in steps) if step)[:MAX_SETUP_STEPS]


@dataclass(frozen=True)
class RunProposal:
    """One proposed run, in the shape every planner emits and every launch surface reads.

    `task` and `task_file` are the two task SOURCES and the launch funnel accepts exactly one
    nonempty; this class does not enforce that, on purpose — a half-authored card is a normal
    intermediate state of an editable launch card, and the refusal belongs to the one funnel that
    also has the run root, the saved settings and the filesystem to refuse against.
    """
    run_id: str = ""
    task: dict = field(default_factory=dict)
    task_file: str = ""
    settings: dict = field(default_factory=dict)
    rationale: str = ""
    setup_steps: tuple[str, ...] = ()
    proposal_id: str = ""
    planner: str = ""

    @classmethod
    def from_card(cls, card: Any, *, planner: str = "", draft: Any = None,
                  normalize_settings: bool = False) -> "RunProposal":
        """Read a loose card dict — a planner's emit, a stored proposal, a UI draft — into the shape.

        `draft` is the card being REFINED: a field the planner omitted is kept from it. That merge
        used to live only in `serve/routers/genesis.py`, where a partial emit like
        `{settings: {max_nodes: 50}}` had to tweak the operator's tuned card rather than wipe its
        task and name; every other surface that grew a refine turn would have had to rediscover it.
        """
        card = _as_mapping(card)
        prior = _as_mapping(draft)
        task = _as_mapping(card.get("task")) or _as_mapping(prior.get("task"))
        task_file = str(card.get("task_file") or prior.get("task_file") or "")
        settings = (normalize_launch_settings(card.get("settings"), prior=prior.get("settings"))
                    if normalize_settings
                    else {**_as_mapping(prior.get("settings")), **_as_mapping(card.get("settings"))})
        steps = _clean_steps(card.get("setup_steps")) or _clean_steps(prior.get("setup_steps"))
        return cls(
            run_id=str(card.get("run_id") or prior.get("run_id") or ""),
            task=task,
            task_file=task_file,
            settings=settings,
            rationale=str(card.get("rationale") or prior.get("rationale") or ""),
            setup_steps=steps,
            proposal_id=str(card.get("proposal_id") or prior.get("proposal_id") or ""),
            planner=str(planner or card.get("planner") or prior.get("planner") or ""),
        )

    def with_run_id(self, run_id: Any) -> "RunProposal":
        """The same proposal under a different name — what a de-duplicating planner needs.

        Naming is where the surfaces legitimately differ: the genesis card walks `-2`, `-3` past run
        dirs that already hold an events log, while the TUI and the CLI take the operator's word and
        let `/api/start` refuse a collision with a 409. That loop needs the run root, so it stays
        with the router; the SHAPE it produces comes back through here.
        """
        return RunProposal(run_id=str(run_id or ""), task=self.task, task_file=self.task_file,
                           settings=self.settings, rationale=self.rationale,
                           setup_steps=self.setup_steps, proposal_id=self.proposal_id,
                           planner=self.planner)

    def with_settings(self, settings: Any) -> "RunProposal":
        """The same proposal with a replaced settings block (the genesis card's backend hint)."""
        return RunProposal(run_id=self.run_id, task=self.task, task_file=self.task_file,
                           settings=_as_mapping(settings), rationale=self.rationale,
                           setup_steps=self.setup_steps, proposal_id=self.proposal_id,
                           planner=self.planner)

    def slug_name(self, *fallbacks: Any) -> str:
        """The proposal's own name as a slug, else the first fallback that slugs to anything.

        The candidates a planner reaches for when the model named nothing — a competition, a kind, a
        task file's stem — differ per surface, so they are passed in; picking the first NONEMPTY slug
        is the part that is the same everywhere and was written twice.
        """
        for candidate in (self.run_id, *fallbacks):
            slug = slug_run_id(candidate)
            if slug:
                return slug
        return ""

    def card(self) -> dict:
        """The editable launch card: what a planner returns and a launch surface renders and edits.

        `proposal_id` and `planner` appear only when set, so a card that never had them keeps
        exactly the keys it had — a card is stored, sent over HTTP and re-read by clients that
        predate this shape.
        """
        card: dict[str, Any] = {
            "run_id": self.run_id,
            "task": dict(self.task),
            "task_file": self.task_file,
            "settings": dict(self.settings),
            "rationale": self.rationale,
            "setup_steps": list(self.setup_steps),
        }
        if self.proposal_id:
            card["proposal_id"] = self.proposal_id
        if self.planner:
            card["planner"] = self.planner
        return card

    def start_body(self, chat: Optional[Iterable[Mapping[str, Any]]] = None) -> dict:
        """The ONE `/api/start` body — the same body `/api/validate` and `/api/start/preflight` take.

        A catalogue `task_file` WINS over an inline task, because the funnel refuses a body carrying
        both and a card that has picked a file has picked a file. `run_id` is slugified here rather
        than at the field, so an operator editing a card sees what they typed and the body carries
        what a run directory can be called. Nothing else is filtered: an unknown or secret setting
        travels to the server and comes back as a 422 naming the key.
        """
        body: dict[str, Any] = {"run_id": slug_run_id(self.run_id), "settings": dict(self.settings)}
        if self.task_file:
            body["task_file"] = self.task_file
        else:
            body["task"] = dict(self.task)
        turns = [{"role": str(turn["role"]), "content": str(turn.get("content", ""))}
                 for turn in (chat or [])
                 if isinstance(turn, Mapping) and turn.get("role") in {"user", "assistant"}]
        if turns:                       # carry the planning chat into the run's history
            body["chat"] = turns
        return body

    def lines(self) -> list[str]:
        """The proposal flattened into the plain lines an operator reads before spending tokens.

        The TUI's proposal panel and launch summary render these; `looplab run --goal` prints them
        under its `Genesis -> kind=…` line, so the CLI announces the plan it is about to run in the
        same words the other two surfaces use.
        """
        out: list[str] = [f"run name : {self.run_id or '—'}"]
        task = self.task
        if self.task_file:
            out.append(f"task     : {str(self.task_file).split('/')[-1]}  (from the catalogue)")
        elif task.get("kind"):
            label = task["kind"]
            if task.get("kind") == "mlebench_real" and task.get("competition"):
                label += f" · {task['competition']}"
            out.append(f"task     : {label}")
            if task.get("goal"):
                out.append(f"goal     : {task['goal']}")
            if task.get("editable_path"):
                out.append(f"repo     : {task['editable_path']}")
        elif task:
            # A COMPOSABLE (kind-less) genesis task — Genesis proposes these with NO `kind`, so the
            # branches above skip them; still surface the substance of the run (goal + capabilities),
            # not just the run-name/settings, so the operator sees what they're about to spend
            # tokens on.
            if task.get("goal"):
                out.append(f"goal     : {task['goal']}")
            if task.get("direction"):
                out.append(f"direction: {task['direction']}")
            for lbl, key in (("repo", "editable_path"), ("repo", "repo"), ("data", "data_path"),
                             ("dataset", "dataset"), ("cmd", "cmd"), ("competition", "competition")):
                if task.get(key):
                    out.append(f"{lbl:<9}: {task[key]}")
        knobs = [(k, self.settings[k]) for k in ("llm_model", "max_nodes", "n_seeds", "policy")
                 if self.settings.get(k) is not None]
        if knobs:
            out.append("settings : " + ", ".join(f"{k}={v}" for k, v in knobs))
        if self.rationale:
            out.append(f"why      : {self.rationale}")
        for i, step in enumerate(self.setup_steps, 1):
            out.append(f"  step {i}. {step}")
        return out


def proposal_for_run_dir(out: Path, task: Any, settings: Any, *, rationale: str = "",
                         planner: str = "cli") -> RunProposal:
    """The proposal a CLI-shaped launch is about to run: the run DIRECTORY is the run's name.

    `looplab run` resolves `--out` rather than a run id, so the name it would launch under is that
    directory's own. Settings arrive as a `Settings` object (or a dump of one) and are reduced to the
    launch-card view — no secret ever reaches a rendered plan.
    """
    if isinstance(settings, Mapping):
        dump: dict = dict(settings)
    elif hasattr(settings, "model_dump"):
        dump = settings.model_dump(mode="json")
    else:
        dump = {}
    return RunProposal(run_id=Path(out).name, task=_as_mapping(task),
                       settings=normalize_launch_settings(dump), rationale=str(rationale or ""),
                       planner=planner)
