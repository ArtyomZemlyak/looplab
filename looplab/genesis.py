"""Genesis on the CLI: turn a plain-text goal into a runnable task — the LLM picks the task `kind`
and authors the inline spec, so the user never has to name a task type.

This is the headless counterpart of the Web UI's "New run" Genesis chat (`server.py /api/genesis`):
same idea — a model reads your words (and any data/repo path you mention) and decides *what kind of
task this is* — exposed for `looplab run --goal "..."` with no `--kind`.

Agentic, like the Web Genesis: when the LLM client can drive tools, this CLI Genesis runs as an AGENT
too — it drives the same read-only filesystem tools (`reposcout.RepoScoutTools`:
list_dir/read_file/find_files) to actually inspect (and VERIFY the paths of) a dataset/repo before
authoring. The one difference from the Web agent is the headless contract (`GENESIS_E2E_RULE`): on
`looplab run --goal` there is no human to answer a follow-up, so the agent is told it runs END-TO-END
and must resolve every ambiguity itself and never ask — it decides and emits a complete task. When the
client can't tool-call (or `agentic=False`), it falls back to a single structured call.

On TOP of the agent, `author_task` ends with a deterministic PATH GATE (`check_paths`): every local
path the authored task references is verified to exist, and a missing one is REFUSED with a clarifying
reply (`path_error`) — *before* any run dir is created. Without this backstop a mistyped/hallucinated
path would create a run that crashes mid-flight, and a re-run would then land in that finished/errored
run and exit at once.

On `kind` itself: it is **not** removed. It is the dispatch key that selects one of nine
``TaskAdapter`` semantics (each a different eval / grader / trust / data model — e.g. a self-reported
``dataset`` metric vs a held-out ``mlebench`` grader vs your own protected ``repo`` eval). Collapsing
those into one type would erase real differences in how a candidate is scored and trusted. What
Genesis removes is the *burden* of naming it: where an LLM is already in the loop, the LLM infers the
kind from the goal instead of making the human pick.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from .parse import parse_structured

# Canonical, CLI-focused guide to choosing a task kind from a plain goal. Kept compact and in one
# place (the richer, repo-scouting variant the Web UI uses lives in server.py's genesis endpoint).
TASK_KIND_GUIDE = (
    "Choose the task KIND that fits the user's goal and author an inline `task` object for it:\n"
    "- dataset — they point at a data file and want a prediction; the agent writes the WHOLE solution "
    'and picks the metric. {"kind":"dataset","goal":"<what to predict>","direction":"max",'
    '"data_path":"<path to their data>"}. The simplest "here is my data, get the best metric" case.\n'
    "- repo — they have an EXISTING code project to improve, with their own way to run/score it. "
    '{"kind":"repo","goal":"...","direction":"max"|"min","editable_path":"<repo path>",'
    '"edit_surface":["**/*.py"],"eval":{"command":["python","train.py"],'
    '"metric":{"kind":"stdout_json","key":"metric"},"timeout":1800}}. Copy any path/command/metric '
    "key they give VERBATIM; never invent a path you weren't given.\n"
    "- mlebench_real — a known Kaggle / MLE-bench competition. "
    '{"kind":"mlebench_real","competition":"<full-kaggle-slug>"}.\n'
    "- quadratic — a pure numeric objective with named variables and bounds, no data or code "
    '(great offline). {"kind":"quadratic","goal":"minimize ...","direction":"min",'
    '"bounds":{"x":[-10,10],"y":[-10,10]}}.\n'
    "- classification / regression / timeseries — tune a fixed model template (knobs, not free code) "
    "for a synthetic/tabular objective.\n"
    "- code_regression / mlebench — the LLM writes a numpy script scored by a held-out grader the "
    "agent can't see (use when an anti-cheat guarantee matters and there is no repo).\n"
    "Rules: author exactly ONE `task`. Set `direction` (max for score/accuracy, min for error/loss). "
    "If the goal is too vague to choose, leave `task` empty and ask ONE clarifying question in `reply`."
)

# How to turn data locations the user mentions in plain words into the task's data fields. The user
# may not pass an explicit path — they just say where things live; author the mounts yourself.
DATA_GUIDE = (
    "DATA: the user may describe where their data lives in plain words — ONE path or SEVERAL, a single "
    "file or a whole folder, possibly in different places. Author it from what they say; don't make "
    "them pre-format it:\n"
    '- A dataset task: put a single file/folder in "data_path", and/or several named locations in '
    '"data" ({"<short_name>":"<abs path>", ...}) when there are multiple. A folder is fine as one '
    "entry — the agent reads what's inside.\n"
    '- A repo task: runtime data goes in "data" ({"<name>":"<abs path>"}) — each is copied to '
    "./<name> in the eval workdir.\n"
    "Use the paths EXACTLY as given (~ and $HOME/$VARS are expanded); never invent a path the user "
    "didn't mention. If they clearly have data but named no location, ask ONE clarifying question in "
    "`reply` instead of guessing."
)


# Headless contract for the agentic CLI genesis: there is NO human in the loop on `looplab run
# --goal`, so the agent must decide everything itself and never stall on a question. This is the one
# difference from the Web genesis agent (which CAN come back with a clarifying question to the chat).
GENESIS_E2E_RULE = (
    "\n\nYOU RUN END-TO-END AND HEADLESS. This is the `looplab run --goal` path: there is NO human to "
    "answer a follow-up. So you MUST NOT ask a clarifying question and MUST NOT leave the task empty — "
    "resolve every ambiguity YOURSELF (which file is the data, how the repo is run/scored) using your "
    "tools, then make the best autonomous decision and emit ONE complete, runnable task. `reply` is a "
    "one-line statement of what you decided, NEVER a question.")

# Tool-usage rule for the agentic genesis: inspect the disk before authoring, and — crucially for the
# path bug — VERIFY every path it puts in the task actually exists, fixing a wrong one by searching.
GENESIS_SCOUT_RULE = (
    "\n\nYou have READ-ONLY filesystem tools on THIS machine: list_dir(path), read_file(path), "
    "find_files(root, pattern). Use them BEFORE you emit:\n"
    "- Confirm EVERY path you put in the task (data_path / data / editable_path / editables / "
    "references) ACTUALLY EXISTS — list_dir or find_files it first. NEVER emit a path you haven't "
    "verified.\n"
    "- If a path the user gave is wrong (doesn't exist), search for the real one nearby (find_files on "
    "its parent / the home dir for the named file or repo) and use THAT. Only if you genuinely cannot "
    "locate it, emit your best guess and state the problem in `reply`.\n"
    "- For a repo: read the README and the entry/eval script to ground the eval command, metric kind+"
    "key, edit_surface and any data mount in what you actually read.\n"
    "Don't just SAY you'll look — look, then call `emit` exactly once.")

# A path-ish token in free text: starts with /, ~, or $VAR. Used to widen the scout's allowed roots to
# whatever locations the user named in the goal, so the agent can actually read+verify them.
_PATHISH = re.compile(r"(?:~|\$[A-Za-z_]\w*|/)[^\s\"'<>|,;]*")


# Kinds whose work is inherently LLM-driven (the agent writes/edits code or reasons over data). When
# Genesis infers one of these and the user didn't choose a backend, the run defaults to backend=llm —
# the offline-optimizable kinds (quadratic/classification/regression/timeseries) stay on their default.
GENERATIVE_KINDS = frozenset({"dataset", "code_regression", "mlebench", "mlebench_real", "repo"})


class _TaskPlan(BaseModel):
    """What Genesis emits for a fresh CLI run: just the inline task (it picks the kind), plus a short
    human-facing line. Engine settings stay on the CLI/file — Genesis only decides *what* to solve."""
    task: dict = {}
    rationale: str = ""
    reply: str = ""


@dataclass
class GenesisResult:
    task: dict = field(default_factory=dict)
    rationale: str = ""
    reply: str = ""
    error: str = ""           # set when the model/endpoint failed (vs an empty task from a vague goal)
    path_error: str = ""      # set when the authored task names a local path that doesn't exist — a
    # third, distinct outcome from `error` (couldn't reach the model) and an empty task (vague goal):
    # the model authored a task fine, but it points at a path the run would crash on. The caller
    # refuses up front instead of spawning a doomed run.

    @property
    def kind(self) -> Optional[str]:
        return self.task.get("kind") if isinstance(self.task, dict) else None


def _expanded(p: str) -> str:
    """~ / $VAR-expand then make absolute — the same resolution the task adapters do at load time, so
    the existence check sees the path the run will actually read."""
    return os.path.abspath(os.path.expanduser(os.path.expandvars(p)))


def _task_local_paths(task: dict) -> list[str]:
    """The on-disk locations an authored task points at that MUST already exist: the data the agent
    reads (`data_path` / `data`), the repo it edits (`editable_path` / `editables[].path`) and runtime
    mounts (`references[].path`). Eval *targets* are deliberately excluded — a repo task may name an
    entry script the agent has yet to write (a documented "the agent creates run.py" flow), so checking
    eval.command paths would false-refuse those."""
    paths: list[str] = []

    def _add(v) -> None:
        if isinstance(v, str) and v.strip():
            paths.append(v)

    _add(task.get("data_path"))
    _add(task.get("editable_path"))
    data = task.get("data")
    if isinstance(data, dict):
        for v in data.values():
            _add(v)
    elif isinstance(data, str):                 # a bare string `data` (single location) is also valid
        _add(data)
    for key in ("editables", "references"):
        items = task.get(key)
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    _add(it.get("path"))
    return paths


def _missing_local_paths(task: dict) -> list[str]:
    """Paths the task references that don't exist on this machine (after ~/$VAR expansion), de-duped
    and preserving the order/spelling the user/model gave so the refusal message echoes their input."""
    seen: set[str] = set()
    missing: list[str] = []
    for p in _task_local_paths(task):
        ep = _expanded(p)
        if ep in seen:
            continue
        seen.add(ep)
        if not os.path.exists(ep):
            missing.append(p)
    return missing


def _scout_roots(goal: str, data: Optional[str], repo: Optional[str]) -> list[Path]:
    """The directories the read-only scout is allowed to browse: the user's home + CWD (the common
    case) PLUS every location the user named — the explicit --data/--repo paths and any path-ish token
    in the goal — so the agent can actually reach and verify a dataset/repo that lives outside home
    (e.g. /mnt/data). RepoScoutTools bounds reads to these roots and refuses anything outside them.

    Two safeguards keep that bound real: the filesystem ROOT (`/`) is never added — it is an ancestor
    of everything, so allowing it would let the scout read any allowlisted file anywhere — and
    GOAL-extracted tokens are only added when they actually exist on disk. The path-ish regex happily
    matches incidental slashes in prose ("req/s", "f(x)=x^2/2"), whose parent is `/`; requiring
    existence (and dropping `/`) stops a stray slash from widening the scope to the whole machine. The
    explicit --data/--repo args are deliberate, so they keep typo-recovery (their parent is added even
    when the exact path is wrong, so the agent can find the real file nearby)."""
    roots: list[Path] = [Path.home(), Path.cwd()]

    def _add(raw: str, *, require_exists: bool) -> None:
        try:
            p = Path(_expanded(raw))
        except (OSError, ValueError):
            return
        for c in (p, p.parent):
            if c == c.parent:               # the filesystem root ('/') — never widen the scope to it
                continue
            if require_exists and not c.exists():
                continue
            roots.append(c)

    for arg in (data, repo):                # explicit paths: deliberate -> keep typo-recovery
        if isinstance(arg, str) and arg.strip():
            _add(arg, require_exists=False)
    for tok in _PATHISH.findall(goal or ""):  # goal prose: only ground paths that REALLY exist
        _add(tok, require_exists=True)
    return roots


def _author_agentic(client, *, sys_prompt: str, user: str, roots: list[Path], settings,
                    parser: str) -> Optional["_TaskPlan"]:
    """Agentic authoring: let genesis actually INSPECT the filesystem (read-only) before authoring, so
    it grounds (and verifies) every path in what's really on disk — the CLI counterpart of the Web
    genesis agent, but told it runs headless and must never ask. Returns the emitted `_TaskPlan`, or
    None when the client can't drive tools at all (caller falls back to a single structured call)."""
    from .agent import drive_tool_loop, loop_opts_from_settings
    from .reposcout import RepoScoutTools
    tools = RepoScoutTools(roots)
    tool_sys = sys_prompt + GENESIS_E2E_RULE + GENESIS_SCOUT_RULE
    emit_spec = {"type": "function", "function": {
        "name": "emit",
        "description": "Emit the final task plan (task, rationale, reply).",
        "parameters": _TaskPlan.model_json_schema()}}

    def _coerce(args) -> "_TaskPlan":
        try:
            return _TaskPlan(**{k: v for k, v in (args or {}).items() if k in _TaskPlan.model_fields})
        except Exception:  # noqa: BLE001 - a junk emit -> empty plan (the gate/kind-check handle it)
            return _TaskPlan()

    def _finalize(args):
        return _coerce(args)

    def _fallback(msgs):
        # The loop ran (the model drove tools) but never called `emit` — force one final structured
        # emit from the accumulated context rather than discard everything it just read.
        try:
            return parse_structured(client, msgs + [{"role": "user",
                                    "content": "Now call `emit` with the final task plan."}],
                                    _TaskPlan, parser)
        except Exception:  # noqa: BLE001 - even a forced emit failed -> empty plan (still usable)
            return _TaskPlan()

    opts = loop_opts_from_settings(settings) if settings is not None else {}
    return drive_tool_loop(
        client, tools,
        [{"role": "system", "content": tool_sys}, {"role": "user", "content": user}],
        emit_spec,
        max_turns=int(getattr(settings, "agent_max_turns", 0) or 0),
        time_budget_s=float(getattr(settings, "agent_time_budget_s", 0.0) or 0.0),
        finalize=_finalize, fallback=_fallback, **opts)


def author_task(goal: str, *, client, kinds: tuple[str, ...], data: Optional[str] = None,
                repo: Optional[str] = None, direction: Optional[str] = None,
                kind: Optional[str] = None, draft: Optional[dict] = None,
                parser: str = "tool_call", check_paths: bool = True,
                agentic: bool = True, settings=None) -> GenesisResult:
    """Ask the model to author an inline task from a plain goal. With `kind=None` it also CHOOSES the
    kind; with `kind` set it is CONSTRAINED to that kind and only fills the rest (the user pinned the
    type, Genesis does the rest within it). `draft` is an existing task dict (e.g. from a config file)
    to refine in place rather than discard. Returns a GenesisResult; on a vague goal the task may be
    empty with a clarifying `reply`, and on a model/endpoint failure `error` is set (so the caller can
    tell 'reach the model' apart from 'your goal was too vague').

    `agentic` (default on): when the `client` can drive tools, genesis runs as an AGENT — it inspects
    the filesystem read-only (RepoScoutTools) and verifies/locates every path before authoring, told
    explicitly that it runs HEADLESS and must never ask a question (GENESIS_E2E_RULE). When the client
    can't drive tools (or `agentic=False`), it falls back to a single structured call. `settings`
    carries the agent-loop limits/options (unlimited by default).

    `check_paths` (default on) is the deterministic BACKSTOP under the agent: every local path the
    authored task references is verified to exist and a missing one is REFUSED (returning `path_error`
    + a clarifying `reply`, no task) — so even if the agent emits an unverified/mistyped path it can't
    create a run dir that crashes mid-flight. Programmatic callers that only want the authoring logic
    (no disk) can pass `check_paths=False` (and `agentic=False`)."""
    hints = []
    if data:
        hints.append(f"The user named a data/input path: {data} (use it; add others they mention).")
    if repo:
        hints.append(f"The user's repository is at: {repo}")
    if direction:
        hints.append(f"Optimization direction: {direction}")
    if draft:
        hints.append("Refine this existing task draft in place, keeping fields the user didn't ask to "
                     f"change:\n{json.dumps(draft)[:1200]}")
    hint_block = ("\n" + "\n".join(hints)) if hints else ""
    if kind:
        kind_rule = (f"The user has PINNED the task kind to `{kind}` — author a task of EXACTLY that "
                     f"kind (do not switch kinds); fill in everything else from the goal.")
    else:
        kind_rule = TASK_KIND_GUIDE
    sys_prompt = (
        "You bootstrap a new autonomous-ML run from the user's goal. Decide the TASK and author it as "
        "an inline `task` object.\n\n" + kind_rule + "\n\n" + DATA_GUIDE +
        f"\n\nRegistered task kinds: {list(kinds)}.")
    user = f"Goal: {goal}{hint_block}"
    messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}]
    plan = None
    # Agentic first: if the client can drive tools, let genesis scout the disk (and verify paths)
    # before authoring. Any failure here (e.g. the client can't tool-call at all) drops to the single
    # structured call below, so a tool-less/offline client still works.
    if agentic and callable(getattr(client, "chat", None)):
        try:
            plan = _author_agentic(client, sys_prompt=sys_prompt, user=user,
                                   roots=_scout_roots(goal, data, repo), settings=settings,
                                   parser=parser)
        except Exception:  # noqa: BLE001 - tool loop unsupported/failed -> single-shot fallback
            plan = None
    if plan is None:
        try:
            plan = parse_structured(client, messages, _TaskPlan, parser)
        except Exception as e:  # noqa: BLE001 - transport/parse failure: report it as an ERROR,
            # distinct from a vague goal (which parses fine but returns an empty task). The caller
            # surfaces the two differently — "reach the model" vs "your goal was too vague".
            return GenesisResult(error=str(e))
    task = plan.task if isinstance(plan.task, dict) else {}
    # Honor the pin: the kind the user gave wins over whatever the model emitted.
    if task and kind:
        task["kind"] = kind
    # The model may forget a direction/goal we already know — fill the obvious gaps so the result
    # validates without a second round-trip.
    if task and direction and not task.get("direction"):
        task["direction"] = direction
    if task and "goal" not in task:
        task["goal"] = goal
    # A user-given --data path the model dropped: put it where this kind expects it, so an explicit
    # path is never silently lost.
    if task and data and not (task.get("data_path") or task.get("editable_path") or task.get("data")):
        task["editable_path" if task.get("kind") == "repo" else "data_path"] = data
    # Path gate (the "Genesis can check the path and refuse" ask): a task that points at a location
    # that doesn't exist would create a run dir and then crash with a path error mid-run — and a
    # re-run would land in that finished/errored dir and exit at once. Catch it here and refuse with a
    # clarifying reply, BEFORE any run is created, instead of handing back a doomed task.
    if check_paths and task:
        missing = _missing_local_paths(task)
        if missing:
            joined = ", ".join(missing)
            return GenesisResult(
                rationale=plan.rationale,
                path_error=f"path(s) not found on this machine: {joined}",
                reply=(f"I couldn't find {joined} on this machine. Point the goal/--data at a path "
                       "that exists (an absolute path is safest; ~ and $VARS are expanded), then "
                       "re-run."))
    return GenesisResult(task=task, rationale=plan.rationale, reply=plan.reply)
