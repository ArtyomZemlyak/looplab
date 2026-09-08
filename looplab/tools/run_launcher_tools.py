"""The assistant's PROPOSE-a-run provider: it records an editable launch card, it launches nothing.

Split out of `machine_runs_tools.py` on 2026-09-08 (doc 25 TO-02). It shares nothing with the
read-only machine-runs view or the run-control verbs but the `.specs()`/`.execute()` shape, and its
~70-line prompt is a contract (CLAUDE.md, "Prompt strings are contracts") — which is precisely why
it was inflating a module three other subsystems also lived in.
"""
from __future__ import annotations

import json
import uuid

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
                "A task is COMPOSABLE — there is NO `kind`. You describe what you HAVE and the engine "
                "infers the task. Always give `goal` and `direction` (EXACTLY \"max\" or \"min\"), then "
                "add the capability fields that apply:\n"
                "IMPORTANT — the `goal` is the ONLY task text the coding agent (Developer) reads; the "
                "`rationale` and any knowledge you save are NOT reliably in its context. So put EVERY "
                "developer-critical setup detail IN THE GOAL: required CLI flags (e.g. a `--flag` that is "
                "mandatory or the run crashes), a known-good baseline command to start from, data quirks "
                "(label conventions, formats), exact paths that exist. If you discovered a must-have flag "
                "or command while exploring, it belongs in the goal, not just the summary you show me.\n"
                "• `repo`: ABSOLUTE path to an editable codebase that EXISTS on disk — the agent may edit "
                "ANY file within it (protect exceptions with `protect:[...]`).\n"
                "• `dataset`: read-only data/model weights that live OUTSIDE the repo, as "
                "{\"<mount>\":\"<ABSOLUTE path>\"} (a bare path is mounted as ./dataset). They appear at "
                "./<mount> in the workdir. A repo that trains but has NO dataset mounts fails every node "
                "with file-not-found — DISCOVER the paths from the repo (README, configs, script defaults) "
                "+ the user's message, VERIFY each exists, and if a required path is unknown ASK in "
                "`reply` (never omit/guess).\n"
                "• `cmd`: HOW to run + score one experiment. Either a bare argv "
                "([\"python\",\"test.py\"]) or an object {command:[...], metric:{reader,...}, timeout}. "
                "`metric.reader` is one of stdout_json / stdout_regex / file_json / file_regex — HOW to "
                "read the printed metric. For stdout_json/file_json give `key` (the JSON field, e.g. "
                "\"recall\"); for stdout_regex/file_regex give `pattern` (a regex whose group 1 is the "
                "number, e.g. \"RECALL@100: ([0-9.]+)\") — NOT `key`; add `path` for the file_* readers. Set "
                "`reader:\"auto\"` ONLY for the narrow case where a training COMMAND already runs and you "
                "just need the agent to write the metric reader.\n"
                "• `kaggle`: a Kaggle / MLE-bench competition slug (the official grader scores a "
                "submission — no `cmd` needed).\n"
                "`cmd` IS A CONTRACT — the command that runs + the reader that reads its metric. It is the "
                "SCORING step, NOT the trainer: training is a SEPARATE stage the agent declares at run time "
                "(its `declare_stages` tool), and the engine runs it BEFORE `cmd`. WHAT the agent may EDIT "
                "is a SEPARATE, independent decision — `edit_surface` (globs the agent may edit; default = "
                "the WHOLE repo) minus `protect` (exceptions). The file `cmd` runs is NOT auto-protected, "
                "so decide edit-scope explicitly:\n"
                "  • `cmd` points at an OPERATOR-owned scorer the agent must not tamper with (e.g. the "
                "framework's test.py) → add that file to `protect` (the agent then adds a train stage before "
                "it; your protected cmd scores the freshly-trained model).\n"
                "  • `cmd` points at a file the agent must BUILD → leave it editable (a protected file can't "
                "be created).\n"
                "  • NO existing scorer anywhere → point `cmd` at an entrypoint the agent will BUILD "
                "(e.g. [\"python\",\"looplab_eval.py\"]) and leave it editable — a repo task ALWAYS "
                "carries a `cmd` (or metric.reader \"auto\"); say in the goal what it must train and "
                "print.\n"
                "In every case say each node must actually TRAIN a fresh model and score THAT model — never "
                "read a pre-existing checkpoint or a static results file (results_last.csv is a PRIOR run's "
                "output, not a score). If training happens, set `cmd.timeout` GENEROUSLY (seconds): training "
                "runs minutes-to-hours but the default is 600s, which SIGKILLs it mid-first-epoch into an "
                "undertrained model — size it to the full schedule (often 7200-14400s).\n"
                "OPTIONAL fields (the engine honors them — reach for them when the task needs it): "
                "`edit_surface`:[globs] restricts what the agent may edit (default: the WHOLE repo); "
                "a `setup`:[argv] field INSIDE `cmd` runs before each eval (write it nested: "
                "`cmd`:{command, metric, setup:[\"pip\",\"install\",\"-r\",\"requirements.txt\"]} — NOT a "
                "top-level \"cmd.setup\" key); a `profiles` field INSIDE `cmd` "
                "({smoke:{overrides,timeout},full:{…}}) gives a cheap search eval + a full "
                "confirm eval; `params`:{name:[lo,hi]} + a `%params%` token in a command tunes numeric "
                "hyperparameters with NO code edit; `editables`:[{name,path,surface}] mounts several "
                "editable repos. Per-source DATA permissions: a `dataset`/`data` value may be an object "
                "{path, mount(read-only symlink vs copy-in), edit, copy_modify, preprocess, extend} — "
                "default is read-only with copy/preprocess/extend allowed, so the agent can derive/augment "
                "a training set but not touch the original. To let it MODIFY the data, set mount:false (a "
                "writable per-node copy); a mounted original is read-only, so mount:true+edit:true is "
                "auto-converted to a writable copy.",
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
        steps = [str(step).strip() for step in (args.get("setup_steps") or [])
                 if str(step).strip()][:12]
        spec = {"proposal_id": str(uuid.uuid4()),
                "run_id": rid, "task": task or {}, "task_file": task_file,
                "settings": args.get("settings") if isinstance(args.get("settings"), dict) else {},
                "rationale": str(args.get("rationale") or ""), "setup_steps": steps}
        self.proposals.append(spec)
        # describe the proposal by WHAT the composable task carries (there is no `kind` field)
        what = task_file or (task and ("repo" if task.get("repo") else
                                       "kaggle" if (task.get("kaggle") or task.get("competition")) else
                                       "dataset" if (task.get("dataset") or task.get("data")) else
                                       task.get("kind") or "task")) or "a task"
        return (f"(proposed run '{rid}' ({what}) — shown to the user as a launch card; they will start "
                "it. Tell them what you proposed.)")

