"""The assistant's PROPOSE-a-run provider: it records an editable launch card, it launches nothing.

Split out of `machine_runs_tools.py` on 2026-09-08 (doc 25 TO-02). It shares nothing with the
read-only machine-runs view or the run-control verbs but the `.specs()`/`.execute()` shape, and its
~70-line prompt is a contract (CLAUDE.md, "Prompt strings are contracts") — which is precisely why
it was inflating a module three other subsystems also lived in.
"""
from __future__ import annotations

import json
import uuid

from looplab.core.run_proposal import COMPOSABLE_TASK_GUIDE, RunProposal
from looplab.tools._base import fn_spec


class RunLauncherTools:
    """Lets the assistant PROPOSE a new run (the evolution of the Genesis 'New run' flow). It does not
    launch anything itself — it records an editable spec that the UI shows as a launch card, and the
    user starts it via the existing /api/start. So run-creation is one assistant capability rather than
    a separate modal."""

    def __init__(self):
        self.proposals: list[dict] = []

    def bind_state(self, state=None, parent=None) -> None:
        return None

    def specs(self) -> list[dict]:
        return [
            fn_spec("propose_run",
                "Propose a NEW LoopLab run for the user to launch (a run name + a task + optional "
                "settings). The user reviews an editable card and starts it — you do not launch it. "
                "Give EITHER an inline `task` object OR a `task_file` from the catalogue. Put "
                "model/max_nodes/etc. in `settings`. Inline tasks are VALIDATED before card creation "
                "and an invalid one is bounced back to you; task-file cards are resolved and validated "
                "by launch-card preflight.\n"
                + COMPOSABLE_TASK_GUIDE,
                {"run_id": {"type": "string", "description": "short kebab-case name you invent"},
                 "task": {"type": "object", "description": "composable inline task: goal + direction + the fields you have (repo / dataset / cmd{command|stages,metric:{reader,key},timeout} / kaggle). No `kind`."},
                 "task_file": {"type": "string", "description": "a catalogue task path (alternative to task)"},
                 "settings": {"type": "object", "description": "engine overrides, e.g. {\"llm_model\":..,\"max_nodes\":..}"},
                 "rationale": {"type": "string"},
                 "setup_steps": {"type": "array", "items": {"type": "string"},
                                 "description": "operator-facing readiness/adaptation notes; these are not executed automatically"}},
                ["run_id"]),
        ]

    def execute(self, name: str, args: dict) -> str:
        if name != "propose_run":
            return f"(unknown tool: {name})"
        args = args or {}
        rid = str(args.get("run_id") or "").strip()
        if not rid:
            return "(propose_run needs a run_id)"
        task = args.get("task") if isinstance(args.get("task"), dict) else None
        # a model sometimes passes `task` as a JSON STRING — parse it instead of bouncing with a
        # misleading error (the old wording sent agents hunting for a legacy `kind` field)
        if task is None and isinstance(args.get("task"), str) and args["task"].strip().startswith("{"):
            try:
                parsed = json.loads(args["task"])
                task = parsed if isinstance(parsed, dict) else None
            except Exception:  # noqa: BLE001 — fall through to the error below
                task = None
        task_file = args.get("task_file") or None
        if not task and not task_file:
            return ("(propose_run needs an inline composable `task` OBJECT — goal + direction + the "
                    "fields you have (repo / dataset / cmd / kaggle), NO `kind` — or a `task_file`)")
        # VALIDATE before proposing so the card the user sees is actually launchable — an invalid spec
        # (e.g. a repo task with no `eval` and no `onboard`) is bounced BACK to you to fix here, instead
        # of failing only when the user clicks Start (which spawns an engine that dies with no events).
        if task:
            try:
                # DELIBERATE runtime-only upward import (tools -> adapters): validating a task spec
                # inherently needs the adapter registry (_KINDS + model_validate), which cannot move
                # below tools; a constructor-injected validator would add a "silently unvalidated"
                # default. Kept lazy so the import graph stays acyclic at import time.
                from looplab.adapters.tasks import validate_task
                validate_task(task)
            except Exception as e:  # noqa: BLE001
                return (f"(NOT proposed — the task is INVALID: {e}\nFix it and call propose_run again. "
                        "A repo task MUST carry a `cmd` {command|stages, metric:{reader,key}} — point it "
                        "at a file the agent will BUILD if no scorer exists — or set metric.reader "
                        "\"auto\"; `repo` must be an ABSOLUTE path that exists.)")
        # The card SHAPE is `core/run_proposal.py::RunProposal` (doc 27,
        # `three-new-run-planners-no-shared-schema`, closed 2026-09-08). This surface, the TUI's
        # `/api/genesis` card and `looplab run --goal` used to spell the same six fields — plus the
        # setup-step cap and the settings filter — three times over. `core` and not `serve` because
        # `tools/` may not import `serve` (doc 25 XP-03); the schema is data, so it belongs below
        # both. It still VALIDATES nothing on its own: the launch funnel is the one authority, and
        # the `validate_task` bounce above is what this tool owes the model before it proposes.
        spec = RunProposal.from_card(
            {"proposal_id": str(uuid.uuid4()), "run_id": rid, "task": task or {},
             "task_file": task_file, "settings": args.get("settings"),
             "rationale": args.get("rationale"), "setup_steps": args.get("setup_steps")},
            planner="web", normalize_settings=True).card()
        self.proposals.append(spec)
        # describe the proposal by WHAT the composable task carries (there is no `kind` field)
        what = task_file or (task and ("repo" if task.get("repo") else
                                       "kaggle" if (task.get("kaggle") or task.get("competition")) else
                                       "dataset" if (task.get("dataset") or task.get("data")) else
                                       task.get("kind") or "task")) or "a task"
        return (f"(proposed run '{rid}' ({what}) — shown to the user as a launch card; they will start "
                "it. Tell them what you proposed.)")

