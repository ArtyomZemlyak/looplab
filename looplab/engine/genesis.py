"""Genesis on the CLI: turn a plain-text goal into a runnable task — the LLM picks the task `kind`
and authors the inline spec, so the user never has to name a task type.

This is the headless counterpart of the Web UI's "New run" Genesis chat
(`serve/routers/genesis.py /api/genesis`): same idea — a model reads your words (and any data/repo
path you mention) and decides *what kind of task this is* — exposed for `looplab run --goal "..."`
with no `--kind`. Both paths share `default_backend` (over `GENERATIVE_KINDS`) below to default
`backend=llm` when the authored task needs a code-writing agent and no backend was chosen explicitly.

On `kind` itself: it is **not** removed. It is the dispatch key that selects one of nine
``TaskAdapter`` semantics (each a different eval / grader / trust / data model — e.g. a self-reported
``dataset`` metric vs a held-out ``mlebench`` grader vs your own protected ``repo`` eval). Collapsing
those into one type would erase real differences in how a candidate is scored and trusted. What
Genesis removes is the *burden* of naming it: where an LLM is already in the loop, the LLM infers the
kind from the goal instead of making the human pick.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel

from looplab.agents.agent import agentic_struct
from looplab.core.parse import parse_structured

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


# How to author a `repo` task for maximum autonomy — the optional knobs that let the run adapt to the
# environment. Author them from the user's WORDS (they may say "use all GPUs", "install deps once",
# "the repo is huge, only touch code"); when unsaid, prefer the sensible defaults noted here.
REPO_AUTONOMY_GUIDE = (
    "REPO TASK AUTONOMY KNOBS (optional; author from the user's words, else use the default):\n"
    "- seed_mode (\"auto\"|\"tracked\"|\"all\"): how the editable repo is copied into each experiment "
    "workdir. Default \"auto\" = copy only git-tracked source when it's a git repo (so a tree bloated "
    "with untracked model checkpoints/data is NOT deep-copied). Use \"all\" only for a small repo or "
    "when untracked files are needed at eval time. If the repo is large, keep \"auto\"/\"tracked\".\n"
    "- Data/large inputs that live OUTSIDE the repo go in `data` ({name: abs_path}) — they are mounted "
    "(symlinked) at ./<name>, never deep-copied. Prefer this for multi-GB datasets/pretrained models.\n"
    "- Dependencies: put a ONE-TIME install in `eval.run_setup` (runs once at run start into the shared "
    "interpreter — the default when deps are stable) OR a per-experiment install in `eval.setup` (runs "
    "before EVERY eval — use when the agent edits requirements and each node needs its own). Don't set "
    "either if deps are already present.\n"
    "- Hyperparameters: only set `params` bounds when the user wants a specific bounded search; "
    "otherwise LEAVE `params` EMPTY and let the coding agent estimate sane values from the model size, "
    "available GPU memory and any README recipe — and use ALL available GPUs by default.\n"
    "- Put operational guidance the agent needs (use all GPUs, expected metric, which script to run) in "
    "the task `goal` in plain words — the coding agent reads it.\n")


# Kinds whose work is inherently LLM-driven (the agent writes/edits code or reasons over data). When
# Genesis infers one of these and the user didn't choose a backend, the run defaults to backend=llm —
# the offline-optimizable kinds (quadratic/classification/regression/timeseries) stay on their default.
GENERATIVE_KINDS = frozenset({"dataset", "code_regression", "mlebench", "mlebench_real", "repo"})


def default_backend(kind: Optional[str], *, chosen: bool) -> Optional[str]:
    """The ONE kind→backend defaulting rule, shared by every launch surface — cli.py's genesis path
    and the web UI's /api/start + genesis card (`serve/routers/control.py::_defaults_backend_llm`) —
    so the rule can't drift into per-surface copies. Returns "llm" when `kind` is generative and the
    user chose no backend; None means "leave the configured default". Each caller keeps only its own
    surface-specific `chosen` detection (CLI flag/file/env/.env vs merged launch settings + saved UI
    defaults). Why it matters: Settings.backend defaults to "toy", which on a repo/dataset task gives
    NoOpRepoDeveloper — every node silently re-evaluates the unchanged baseline (no error, just a
    flat run) — so a generative kind with no explicit choice must default to the code-writing
    backend."""
    return "llm" if (not chosen and kind in GENERATIVE_KINDS) else None


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

    @property
    def kind(self) -> Optional[str]:
        return self.task.get("kind") if isinstance(self.task, dict) else None


def _scout_tools(data: Optional[str], repo: Optional[str]):
    """Read-only filesystem scout over the paths the user actually NAMED (a --data location and/or a
    --repo), so Genesis can GROUND the task it authors in the real files (README, entry/eval script,
    a data file's header) instead of guessing. Returns None when the user named NO on-disk path (a
    pure goal→plan generation, e.g. a `quadratic` objective): with nothing concrete to read,
    `agentic_struct` then just delegates to the single-shot `parse_structured`.

    Deliberately NOT RunTools/DataTools: Genesis runs BEFORE a run or a TaskAdapter exists, so there
    are no experiments to introspect and no constructed task to profile — the only concrete thing to
    read at bootstrap is the filesystem the user pointed at. Never raises — a bad/absent path degrades
    to no tools rather than crashing bootstrap (the run must always be authorable)."""
    import os
    from pathlib import Path
    named = [p for p in (repo, data) if p]
    if not named:
        return None
    try:
        from looplab.agents.agent import CompositeTools
        from looplab.tools.reposcout import RepoScoutTools
        roots = [Path.home()]
        for p in named:
            fp = Path(os.path.expanduser(p))
            roots += [fp, fp.parent]           # the path itself AND its parent, so a sibling is visible
        primary = Path(os.path.expanduser(named[0]))
        default_root = str(primary if primary.is_dir() else primary.parent)
        return CompositeTools([RepoScoutTools(roots, default_root=default_root)])
    except Exception:  # noqa: BLE001 - scouting is best-effort; a scout we can't build must not block
        return None


def author_task(goal: str, *, client, kinds: tuple[str, ...], data: Optional[str] = None,
                repo: Optional[str] = None, direction: Optional[str] = None,
                kind: Optional[str] = None, draft: Optional[dict] = None,
                parser: str = "tool_call", memory_dir=None,
                cross_run_read_tools: bool = False) -> GenesisResult:
    """Ask the model to author an inline task from a plain goal. With `kind=None` it also CHOOSES the
    kind; with `kind` set it is CONSTRAINED to that kind and only fills the rest (the user pinned the
    type, Genesis does the rest within it). `draft` is an existing task dict (e.g. from a config file)
    to refine in place rather than discard. Returns a GenesisResult; on a vague goal the task may be
    empty with a clarifying `reply`, and on a model/endpoint failure `error` is set (so the caller can
    tell 'reach the model' apart from 'your goal was too vague')."""
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
    try:
        from looplab.core.hardware import operational_attention_points
        _attn = "\n\n" + operational_attention_points()
    except Exception:  # noqa: BLE001
        _attn = ""
    sys_prompt = (
        "You bootstrap a new autonomous-ML run from the user's goal. Decide the TASK and author it as "
        "an inline `task` object.\n\n" + kind_rule + "\n\n" + DATA_GUIDE + "\n\n" + REPO_AUTONOMY_GUIDE +
        f"\n\nRegistered task kinds: {list(kinds)}." + _attn)
    # AGENTIC grounding: when the user named a real on-disk path (a --data location or a --repo), give
    # the model a read-only filesystem scout so it authors the task from what's REALLY there (the
    # README/entry-script/eval for a repo, the data file's header/schema for a dataset) instead of a
    # promise — the same repo-scouting the Web UI's Genesis chat does. With no named path it stays
    # None (pure goal→plan) and `agentic_struct` degrades to the single-shot parse_structured.
    tools = _scout_tools(data, repo)
    has_filesystem_scout = tools is not None
    # PART V §22 — give Genesis the read-only CROSS-RUN knowledge (portfolio-wide, unbound): so it can plan
    # scope/settings informed by what related runs already tried, and DISCLOSE prior art, instead of blind.
    if memory_dir and cross_run_read_tools:
        from types import SimpleNamespace
        from looplab.agents.agent import CompositeTools
        from looplab.tools.cross_run_tools import CrossRunTools
        crt = CrossRunTools(memory_dir, role="researcher")
        # No task passport exists yet. Bind the provider to the operator's goal/direction; an empty or vague
        # scope fails closed rather than exposing the machine-wide portfolio to an agent prompt.
        crt.bind_state(SimpleNamespace(task_id="", goal=goal, direction=direction or ""))
        tools = CompositeTools([tools, crt]) if tools is not None else crt
        sys_prompt += (
            "\n\nYou also have READ-ONLY CROSS-RUN tools over past runs: cross_run_atlas() (what's been "
            "explored / where the gaps are / what's contradictory), cross_run_prior_attempts(idea) and "
            "cross_run_claims(query) — consult them to ground the task's scope in prior art and steer "
            "toward under-explored or unresolved directions. Advisory: cite, never treat as settled truth.")
    if has_filesystem_scout:
        sys_prompt += (
            "\n\nYou have READ-ONLY tools to inspect this machine BEFORE you author the task: "
            "list_dir(path), read_file(path), find_files(root, pattern). The user pointed you at a real "
            "path on disk — ACTUALLY use them first: list the repo/data directory, read a repo's README "
            "and entry/eval script, and sample a data file's header for a dataset, so the task (its eval "
            "command, metric, edit_surface, data fields) is grounded in what's really there. Never "
            "invent a path, command, or column you didn't see. Then emit the task once.")
    user = f"Goal: {goal}{hint_block}"
    messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}]
    try:
        plan = agentic_struct(client, tools, messages, _TaskPlan, parser=parser,
                              loop_opts={"max_turns": 15},
                              fallback=lambda m: parse_structured(client, m, _TaskPlan, parser))
    except Exception as e:  # noqa: BLE001 - transport/parse failure: report it as an ERROR, distinct
        # from a vague goal (which parses fine but returns an empty task). The caller surfaces the two
        # differently — "reach the model" vs "your goal was too vague".
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
    # Normalize BEFORE the --data fill so a repo path the model gave under an alias (repo_path ->
    # editable_path) is already in place and the fill doesn't install the data CSV as the editable.
    if task and task.get("kind") == "repo":
        _normalize_repo_task(task)
    # A user-given --data path the model dropped: put it where this kind expects it, so an explicit
    # path is never silently lost.
    if task and data and not (task.get("data_path") or task.get("editable_path") or task.get("data")):
        task["editable_path" if task.get("kind") == "repo" else "data_path"] = data
    return GenesisResult(task=task, rationale=plan.rationale, reply=plan.reply)


def _normalize_repo_task(task: dict) -> None:
    """Coerce a repo task the model authored in a LOOSE shape into the canonical RepoTask schema —
    the model reliably picks the right VALUES (paths, seed_mode, run_setup) but often uses near-miss
    field names/types (repo_path, eval-as-string, metric-as-string, string setup commands). Fixing
    them here means an autonomy-authored task validates without a second round-trip. In place; only
    fills/renames — never overwrites a field already in canonical form."""
    import shlex
    def _as_list(v):
        if isinstance(v, str):
            try:
                return shlex.split(v)
            except ValueError:
                return v.split()   # unbalanced quotes: fall back to a naive split, never crash Genesis
        return list(v) if isinstance(v, (list, tuple)) else v
    # editable repo path under any of the common aliases
    if not task.get("editable_path"):
        for alias in ("repo_path", "repo", "path", "editable"):
            if isinstance(task.get(alias), str) and task[alias]:
                task["editable_path"] = task.pop(alias)
                break
    if not task.get("direction") and isinstance(task.get("optimization_direction"), str):
        task["direction"] = task.pop("optimization_direction")
    # eval: a bare string command, or a dict with loose command/metric/setup shapes
    ev = task.get("eval")
    if isinstance(ev, str):
        ev = {"command": _as_list(ev)}
        task["eval"] = ev
    if isinstance(ev, dict):
        if isinstance(ev.get("command"), str):
            ev["command"] = _as_list(ev["command"])
        for k in ("setup", "run_setup"):
            if isinstance(ev.get(k), str):
                ev[k] = _as_list(ev[k])
        # metric as a bare key string -> the stdout_json reader the repo eval prints
        m = ev.get("metric")
        if isinstance(m, str):
            ev["metric"] = {"kind": "stdout_json", "key": m}
        elif m is None and isinstance(task.get("metric"), str):
            ev["metric"] = {"kind": "stdout_json", "key": task["metric"]}
    # a top-level metric string -> stash it on the eval so it isn't lost, creating a minimal eval
    # dict when the model authored none.
    if isinstance(task.get("metric"), str):
        if isinstance(task.get("eval"), dict):
            task["eval"].setdefault("metric", {"kind": "stdout_json", "key": task["metric"]})
        elif task.get("eval") is None:
            task["eval"] = {"metric": {"kind": "stdout_json", "key": task["metric"]}}
    task.pop("metric", None)
