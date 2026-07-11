"""The in-house Developer half of the repo task (kind="repo"), split out of
`adapters/repo_task.py` (BACKLOG §4 "repo_task split"): `LLMRepoDeveloper` (the tool-loop LLM
developer that authors/patches the repo's files) and `LLMOnboarder` (Phase 3 eval onboarding).
The write-tool half — `RepoWriteTools` (the surface-gated write/edit/delete tool provider whose
writes are COLLECTED, not applied), its stage-input validators and the `_xlsx_to_markdown`
results renderer — moved on to `adapters/repo_write_tools.py` (the tool-vs-persona split,
docs/15 mega-refactor) and is re-imported below, so imports from THIS module keep resolving.

A fresh (non-repair) repo implement runs THREE separately-traced phases — STAGES → PLAN →
IMPLEMENT (see `LLMRepoDeveloper._run`): a mandatory READ-ONLY stages phase declares the ordered
eval pipeline (prep → train → … before the operator's protected `score` cmd) via a `declare_stages`
emit and writes `looplab_stages.json`; the plan phase decomposes the code changes into atomic steps;
the implement phase writes the code those stages run. A repair is a single focused session (no
stages/plan). The dedicated STAGES phase AUTHORS the manifest before implement, but `declare_stages`
DOES remain in `RepoWriteTools` (mega-review D1): a repair whose root cause is a bad stage can FIX the
manifest instead of repeating the identical stage failure until abandon — it refuses only when the
operator declared `cmd.stages`.

The task/spec half (`RepoTask`, `ReferenceSpec`/`EditableSpec`/`EvalSpec`, the researchers and
`NoOpRepoDeveloper`) stays in `repo_task.py`, which re-imports these names at its END for
back-compat — so `looplab.adapters.repo_task` and the flat `looplab.repo_task` alias keep
exporting them, and this module needs nothing from `repo_task` at import time (no cycle).
"""
from __future__ import annotations

from typing import Optional

from looplab.core.models import Idea
from looplab.core.parse import LLMClient
from looplab.tools.patch import SurfacePolicy

# Back-compat + direct use: the write-tool half lives in adapters/repo_write_tools.py (the
# tool-vs-persona split). Re-imported here so existing importers (`from
# looplab.adapters.repo_developer import RepoWriteTools`, repo_task's re-export chain, tests)
# keep resolving; the persona below also calls these directly (`_run` builds RepoWriteTools,
# the stages phase validates with the `_missing_*` pair, `_results_context` renders xlsx).
from looplab.adapters.repo_write_tools import (  # noqa: F401
    RepoWriteTools, _covered_by, _missing_paths_feedback, _missing_stage_input_paths,
    _stage_output_values, _xlsx_to_markdown,
)


# --- LLMRepoDeveloper prompt text, hoisted from the inline literals in `_run` --------------------
# Prompt strings are contracts: these constants started byte-identical to the original inline
# text — only the seams where runtime values were concatenated (the brief, the attention points,
# recipes/results/source sections, the parent/repair details) became constant boundaries. The
# `{note}`/`{already}` placeholders are `.format`-filled at the exact spots the old f-strings
# interpolated; neither template contains any other brace.
# 2026-07-09 (docs/PROMPT_REVIEW.md P1, operator-approved): the checkpoint/training contract was
# REWORKED. The old text simultaneously ordered "train UNCONDITIONALLY / never self-skip" and (in
# the DEFINITION-OF-DONE bullets) "if a valid checkpoint already exists, SKIP training and reuse
# it" — real runs obeyed the latter, picked up a FOREIGN experiment's checkpoint, and looped
# forever scoring it. The contract is now situation-based ARTIFACT rules (one experiment → one
# precisely-addressed artifact chain; warm-start only when the idea names the artifact) and the
# assembled prompt must contain NO instruction anywhere to skip training when a checkpoint exists —
# expensive-step reuse is exclusively the ENGINE's job via the stage manifest.
_REPO_DEV_SYSTEM_INTRO = (
    "You improve an existing experiment repository by WRITING code with the write_file and edit_file "
    "tools (edit_file for changes to existing files, write_file for new ones). You OWN the "
    "implementation: the researcher proposed the experiment CONCEPT and "
    "hyperparameters; YOU decide how to realise it in code — which existing scripts to "
    "orchestrate, the stage structure, and how to compute + read the metric. ")
_REPO_DEV_SYSTEM_BODY = (
    "The repository's key source files are PREVIEWED below (each is TRUNCATED to save space). This is "
    "a preview, NOT the full code — to read a whole file or find an exact symbol/flag/signature, use "
    "the read-only repo scouts: read_file(path) for full content (repo-relative, e.g. train.py), "
    "grep(pattern) to find where something is defined across the repo, find_files(root, pattern) / "
    "list_dir(path) to see what exists. Do NOT write helper/'cat'/'check' scripts. "
    "There is NO shell / bash / run-command tool — you CANNOT execute anything yourself: your ONLY "
    "actions are write_file/edit_file (author code) and the read-only scouts below. The eval runs your "
    "code afterwards. (Calling a 'bash'/'run' tool just wastes a turn — it does not exist.) "
    "ALWAYS use REPO-RELATIVE paths for the scouts (e.g. read_file('train.py'), not an absolute "
    "'/home/…/…' path — those are refused). If a grep/read keeps returning the same content, you "
    "already have it: STOP re-reading and act on what you know. "
    "SCOPE: your read/write tools reach ONLY this repo. Data/model files OUTSIDE it (a dataset or "
    "checkpoint mount named in the task) are NOT readable by your tools here — don't try, and don't "
    "hunt for them; just reference their given path in the CODE you write, which CAN open them at "
    "runtime. Need to know the GPUs? call gpu_info (there is no nvidia-smi — you have no shell). "
    "NEVER GUESS a CLI flag / arg name / config key from the truncated preview — grep or "
    "read_file it first (guessing a flag the script doesn't define is the #1 cause of a crash). "
    "Also GROUND every framework API call in the ACTUAL installed environment with the read-only "
    "inspection "
    "tools, instead of guessing (wrong-version APIs are the #1 cause of failed runs): pkg_info(name) "
    "for a package's exact VERSION (e.g. check pytorch-lightning's version before choosing a Trainer "
    "arg — an arg or an accepted value like precision may differ across versions); py_api(dotted) for "
    "a class/function signature or an Enum's VALID VALUES; read_installed(module) to read an installed "
    "module's source; grep_installed(query, package) to find where an arg is parsed / a value "
    "validated. Also: only pass a CLI flag to a repo script if that flag EXISTS in the script's "
    "argparse — CONFIRM it with grep('add_argument') or read_file before you build the "
    "command; otherwise EDIT the script to add it; never invent a flag. "
    "Your write_file/edit_file results are AUTO-VALIDATED (the file is compiled after every change) — "
    "if you get 'not valid Python — line N: …', fix that line immediately; a rejected edit was NOT "
    "staged. To CHANGE an existing file, use edit_file with a minimal SEARCH/REPLACE hunk "
    "(strongly preferred — never re-write a whole existing file). Author the eval entrypoint "
    "the eval command runs — if the repo does not already ship it (CHECK before rewriting: a seeded "
    "repo's existing, unprotected script may only need edits) — by "
    "calling write_file with a REPO-RELATIVE path and the FULL file content. The entrypoint "
    "must print the metric as the LAST stdout line (a JSON object with the required key). CRITICAL: the "
    "eval command runs `<entrypoint>.py`, so THAT FILE MUST EXIST in the workspace after your edits — a "
    "fresh node starts WITHOUT it (unless the operator PROTECTED an existing scorer, which you must NOT "
    "rewrite). For TRAINING work, WHEN the node's declared pipeline (see the task message) has a separate "
    "`train` stage, the entrypoint here only SCORES, and a fixed eval re-runs without "
    "paying to re-train. When NO train stage is declared, the single entrypoint must orchestrate train→test; "
    "editing only train.py leaves the eval with 'no such file: "
    "<entrypoint>.py'. CRITICAL for a TRAINING task: the entrypoint MUST actually TRAIN a model "
    "for THIS experiment (run the repo's train script with your config → produce a FRESH checkpoint) and "
    "THEN score that model. Do not shortcut by loading a pre-existing/best checkpoint, or by reading a "
    "static results file (a prior run's results_last.csv / *.ckpt is NOT this node's score) — a node that "
    "doesn't train can't test your idea and silently fakes the parent's number. The ARTIFACT rules under "
    "DEFINITION OF DONE below say exactly what your stages may load (only what THIS experiment's own "
    "pipeline produces — or an artifact the idea EXPLICITLY names as a warm-start) and why the training "
    "stage must never self-skip on an existing checkpoint; re-running only the cheap stage after a fix is "
    "the ENGINE's job via the multi-stage pipeline below, NOT a check inside your "
    "script. Ensure the FULL schedule completes (all requested epochs — "
    "the best-val checkpoint of a full run, not an epoch-0/1 checkpoint from a training that never "
    "finished). ALSO include any related "
    "metrics you compute in that SAME JSON "
    "object under their own names (e.g. {\"metric\": <objective>, \"recall@10\": .., \"mrr\": ..}) "
    "— every extra key is recorded and shown alongside the objective; only the required key "
    "drives selection, so report generously. Bake the chosen hyperparameters into the code. Stay within your "
    "editable surface; never write protected or absolute paths. When all files are written and "
    "the eval would succeed, call done.\n\n"
    "TRAIN-THEN-SCORE PIPELINE — the ordered stages are declared in your dedicated STAGES phase and "
    "written to `looplab_stages.json` (when the task message states this node's ACTUAL pipeline, trust "
    "it over any assumption); HERE you implement the CODE those "
    "stages run (e.g. the train.py the `train` stage invokes, the prep.py a `data_prep` stage invokes, the "
    "eval entrypoint the `score` step runs). For reference, a stage is "
    "{name:'train',command:['python','train.py','%params%'],timeout:14400,check:true}; the operator's "
    "`cmd` is APPENDED automatically as the final, protected `score` stage — you CANNOT rewrite how the "
    "run is scored (that's the trust boundary), only add work before it. Stages run in ORDER in the SAME "
    "workdir (artifacts persist: `train` writes a checkpoint the `score` step reads). This is the ONLY "
    "correct way to get 'a failed step is fixed and re-run WITHOUT paying to re-train': the ENGINE reuses "
    "the completed `train` stage's checkpoint and re-runs only what changed (a FRESH node still trains "
    "from scratch — stages are tracked PER NODE, never inherited). Give `train` a GENEROUS `timeout` that "
    "covers the full schedule (epochs × minutes/epoch × 60 — the default is short and would SIGKILL a long "
    "train into an undertrained checkpoint). Put `%params%` inside a stage command to inject THIS node's "
    "hyperparameters as `--key value`, or bake the values into the code yourself. Do NOT hand-roll a "
    "single monolithic entrypoint with a 'skip training if a checkpoint already exists' check: the engine "
    "can't see stage boundaries there, so it can't re-run just the scoring — and the ARTIFACT rules below "
    "explain why such a check silently freezes the metric. `declare_stages` "
    "validates your manifest and reports errors back to you. Without stages, your single entrypoint (the "
    "operator's cmd) runs as one command.\n\n"
    "For a ROUTINE hyperparameter experiment, prefer ORCHESTRATING the repo's EXISTING scripts "
    "via subprocess (`subprocess.run([sys.executable, 'train.py', ...], check=True)`) and map the "
    "proposed hyperparameters onto the scripts' CLI flags (respect each flag's type — e.g. an int "
    "flag needs an int); custom data formats (e.g. pickled classes) usually only deserialize with "
    "the repo's own loaders, so reuse them. BUT you are NOT limited to that: when the experiment's "
    "idea calls for a STRUCTURAL change — a new loss/objective, an architecture tweak, a data or "
    "feature change, a different training procedure — EDIT THE REPO'S SOURCE FILES DIRECTLY with "
    "edit_file (e.g. change the loss in train.py/model.py/loss.py with a minimal SEARCH/REPLACE "
    "hunk), then run the training script unchanged. You may modify ANY editable file (only the "
    "protected files are off-limits); never reject a good idea just because it needs a code change "
    "— implement it. "
    "CRITICAL — do NOT make a structural change by generating an entrypoint that REWRITES or "
    "PATCHES another script's source at RUNTIME (string replacement / re.sub / sed / inserting "
    "lines / regex-editing train.py before running it). That pattern reliably corrupts the file "
    "(IndentationError, repeated keyword args, an inserted arg the parser never sees) and the run "
    "fails. Instead make the change PERSISTENT and REVIEWABLE by editing the actual source file "
    "with edit_file, so the training script on disk already contains your change before it runs. "
    "Use ABSOLUTE paths for inputs that live OUTSIDE the repo (relative `../../...` paths in "
    "the README will not resolve from the eval workdir); mounted inputs appear at ./<name> in "
    "the workdir. When a script already computes + reports the metric (e.g. in a produced "
    "checkpoint filename or a results file), read it from there rather than re-deriving it.\n\n"
    "DEFINITION OF DONE for this node: ONE clean experiment run (exit 0, no errors) that prints "
    "the required metric as the last stdout JSON line.\n"
    "ARTIFACTS — one experiment, one precisely-addressed artifact chain:\n"
    "  • Every artifact this experiment produces (checkpoint, processed dataset, predictions) is "
    "written to a STABLE, EXPERIMENT-LOCAL path inside the eval workdir (e.g. ./ckpt/model.pt) "
    "that your stages declare and share; the TEST/METRIC stage loads EXACTLY the artifact path "
    "the TRAIN stage writes — never a glob over 'whatever *.ckpt is lying around' — and must be "
    "runnable on its OWN against that declared artifact, WITHOUT retraining.\n"
    "  • NEVER load a checkpoint/artifact this experiment's pipeline did not produce: the repo "
    "may ship pretrained weights, and earlier/other experiments' outputs can sit nearby; scoring "
    "one of those silently reports someone else's number. (Only exception: the experiment idea "
    "EXPLICITLY says to warm-start/fine-tune from a NAMED artifact — then load exactly that "
    "named path.)\n"
    "  • Multi-phase training (pretrain → finetune → RL) is fine: each phase is its own stage "
    "writing its OWN artifact path, and the next phase declares which one it consumes.\n"
    "  • Your training stage must not self-skip on 'a checkpoint already exists': the workdir "
    "can contain a partial checkpoint from an interrupted run or a foreign experiment's "
    "artifact, and a skip-if-exists check silently reuses it and freezes the metric (this exact "
    "failure has happened — runs looped scoring a foreign checkpoint). Re-running only the cheap "
    "stage after a fix is the ENGINE's job: it reuses YOUR completed train stage via the stage "
    "manifest, so a downstream bug never costs a retrain.\n"
    "Never silently emit a fake/zero metric to hide an error — fail loudly (non-zero exit) so "
    "the failing stage can be repaired.\n"
    "LOGGING: keep the training framework's logger (e.g. PyTorch Lightning's TensorBoardLogger) "
    "ENABLED and log SEVERAL metrics (the target metric AND related ones — loss, other recalls, "
    "lr), not just the objective; point its log dir at a STABLE path under the workdir so the "
    "curves persist (viewable via `looplab tensorboard <run_dir>`). Also print readable progress "
    "(epoch/step + current metrics) to stdout — it streams to the live eval log.\n\n")
_REPO_DEV_COMMANDS_HEADER = (
    "=== CANONICAL COMMANDS (from the repo README — adapt paths to absolute + your "
    "hyperparameters) ===\n")
_REPO_DEV_RESULTS_HEADER = (
    "=== PAST EXPERIMENTS / RESULTS (the repo's own history — which configs reached which "
    "metric; use it to pick strong hyperparameters and beat the best) ===\n")
_REPO_DEV_SOURCE_HEADER = "=== REPOSITORY SOURCE (PREVIEW — truncated; read_file / grep for full) ===\n"
_REPO_DEV_PARENT_BLOCK = (
    "\n\n=== PARENT SOLUTION (your starting point{note}) ===\n"
    "The files below are this experiment's PARENT — they are already loaded as your "
    "working set and carry over verbatim unless you change them. AMEND them with "
    "edit_file (small SEARCH/REPLACE hunks): change ONLY what this idea requires and "
    "keep everything else as-is. Do NOT rebuild the solution from scratch and do NOT "
    "re-write whole files that only need a small change.\n\n")
_REPO_DEV_REPAIR_BLOCK = (
    "\n\nThe PREVIOUS attempt FAILED — fix ONLY the stage that failed (see the error) with "
    "MINIMAL edit_file hunks on the offending file(s) (re-write a file only if it is beyond patching). "
    "The re-run happens in the SAME workdir; when the node has pipeline stages, the ENGINE decides "
    "what to re-run and reuses a completed train stage's artifact where that is safe — do NOT add "
    "'skip if a checkpoint exists' logic to the code yourself (a partial or foreign artifact would "
    "silently freeze the metric); just repair the failing step. Do not start "
    "over from scratch. Files in this node's working set: {already}.\n"
    "--- eval error (stderr/stdout tail) ---\n")


class LLMRepoDeveloper:
    """In-house LLM developer for repo tasks — no external coding agent (opencode/aider/…) required.
    It reads the repo with the read-only scout tools and AUTHORS the file(s) the eval needs with
    `write_file`, driven by the shared agentic tool loop. Repo editing was originally an
    external-agent-only path (the in-house repo developer is a NoOp); this lets a repo task run on
    just the in-house LLM. The written files become the node's `last_files`, which the orchestrator
    materializes on top of the seeded tree and evaluates.

    A fresh implement runs THREE separately-traced phases (see `_run`): STAGES (mandatory, first —
    a read-only phase that declares the ordered eval pipeline around the operator's protected `score`
    cmd, writing `looplab_stages.json`), PLAN (read-only atomic-step decomposition), then IMPLEMENT
    (write the code, one bounded session per step). A REPAIR skips both and runs a single session."""

    # PromptStore handle (docs/15 §P4.7): the intro/body blocks render through it, so an
    # operator's prompt_dir override applies to the REPO developer exactly like it always did to
    # the toy one. A CLASS-level default (not only an __init__ assignment): tests exercise these
    # methods on bare `__new__` instances, and the attr also opts into make_roles' existing
    # post-construction hook (`if hasattr(developer, "prompts"): developer.prompts = prompts`).
    prompts = None

    def __init__(self, client: LLMClient, task, *, parser: str = "tool_call",
                 loop_opts: Optional[dict] = None, plan_decompose: bool = True,
                 plan_min_steps: int = 2, plan_max_steps: int = 8,
                 session_max_turns: int = 500, session_time_budget_s: float = 1200.0,
                 prompts=None):
        self.client = client
        self.task = task
        self.parser = parser
        self.prompts = prompts
        self.loop_opts = dict(loop_opts or {})
        # C4 plan decomposition + hard per-session backstop (see Settings.developer_*).
        self._plan_decompose = plan_decompose
        self._plan_min_steps = max(2, int(plan_min_steps))
        self._plan_max_steps = max(1, int(plan_max_steps))
        self._session_max_turns = int(session_max_turns)
        self._session_time_budget_s = float(session_time_budget_s)
        self.brief = task.agent_brief()
        rs = task.repo_spec()
        self._surface = rs["edit_surface"]
        self._protected = rs["protected_names"]
        self._editables = rs["editables"]
        self._prefixes = [e["name"] for e in self._editables if e["name"] not in (".", "")]
        # Read-only data-mount names (a subset of protected_names, protected defensively) so the
        # write tools can explain a mount refusal honestly — see RepoWriteTools.__init__.
        self._data_mounts = [n for n, s in (rs.get("data") or {}).items()
                             if isinstance(s, dict) and s.get("mount")]
        self.last_files: dict[str, str] = {}
        self.last_deleted: list[str] = []

    # Files most useful to PRELOAD verbatim so the agent authors the entrypoint without fumbling with
    # a (truncating) read tool. Order = priority; the rest of the surface is appended within budget.
    # PROVENANCE / HEURISTIC ONLY: these names (incl. the repo-specific `to_stf.py`/`tokenizing.py`)
    # come from the reference repo LoopLab was first exercised on. They are a soft *ordering* prior,
    # not a requirement — an absent name simply doesn't preload, and the full surface is appended
    # anyway — so the heuristic degrades gracefully on any other repo. Generalize to an
    # `EditableSpec.preload_priority` knob if a task ever needs to override the order.
    _PRELOAD_PRIORITY = ("test.py", "settings.py", "train.py", "to_stf.py", "model.py", "loss.py",
                         "dataset.py", "tokenizing.py", "metrics.py", "inference.py", "README.md")

    def _repo_context(self, per_file: int = 3000, total_budget: int = 30000) -> str:
        """Embed the repo's key source files VERBATIM in the prompt so the agent can author the eval
        entrypoint from them directly — instead of writing throwaway 'cat' scripts to dribble a file
        in through a truncating read tool (the failure mode we hit). Listing first, then prioritized
        full-text files within a char budget."""
        from pathlib import Path as _P
        parts: list[str] = []
        used = 0
        for ed in self._editables:
            root = _P(ed["path"])
            if not root.is_dir():
                continue
            try:
                names = sorted(p.name for p in root.iterdir() if p.is_file())
            except OSError:
                names = []
            parts.append(f"# Repository `{ed['name']}` at {root} — files:\n" + ", ".join(names))
            ordered = [n for n in self._PRELOAD_PRIORITY if n in names] + \
                      [n for n in names if n.endswith((".py", ".yaml", ".yml", ".json"))
                       and n not in self._PRELOAD_PRIORITY]
            for n in ordered:
                if used >= total_budget:
                    break
                fp = root / n
                try:
                    txt = fp.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                snip = txt[:per_file]
                if len(txt) > per_file:
                    snip += f"\n… (+{len(txt) - per_file} more chars truncated)"
                block = f"\n\n--- {ed['name']}/{n} ---\n{snip}"
                parts.append(block)
                used += len(block)
        return "\n".join(parts)

    def _recipes(self, cap: int = 8000) -> str:
        """Pull the repo's canonical run commands from its README so the agent ORCHESTRATES the
        existing train/convert/test scripts instead of reinventing them (and tripping on the pickled
        dataset's custom classes). Lines that ran a repo `.py` script, captured verbatim with the
        nearest preceding label; the budget keeps the most relevant (earliest) ones."""
        import re
        from pathlib import Path as _P
        rows: list[str] = []
        for ed in self._editables:
            try:
                lines = (_P(ed["path"]) / "README.md").read_text(encoding="utf-8",
                                                                 errors="replace").splitlines()
            except OSError:
                continue
            for i, ln in enumerate(lines):
                s = ln.strip()
                # The script-name allow-list is a HEURISTIC (train/test are generic; `to_stf`/
                # `tokenizing` are from the first reference repo — see `_PRELOAD_PRIORITY`). It only
                # decides which README command lines get surfaced as recipes; a repo without these
                # names just yields no recipes here, no failure. Widen the pattern if a new repo's
                # entrypoints are missed.
                if s.startswith("python ") and re.search(r"\b(train|test|to_stf|tokenizing)\.py\b", s):
                    label = ""
                    for j in range(i - 1, max(i - 4, -1), -1):
                        t = lines[j].strip()
                        if t and not t.startswith("python"):
                            label = t
                            break
                    rows.append((f"# {label}\n" if label else "") + s)
        text, used = [], 0
        for r in rows:
            if used + len(r) > cap:
                break
            text.append(r)
            used += len(r)
        return "\n\n".join(text)

    def _results_context(self, cap: int = 9000) -> str:
        """Surface the repo's PAST-EXPERIMENT / results files so the agent grounds its hyperparameter
        choices in the repo's OWN history (which configs reached which metric) — not just the README.
        Matches files whose name looks like results/experiments/benchmark/scores/leaderboard. Text
        files (.md/.csv/.tsv/.txt) go in verbatim; an .xlsx is rendered to a markdown table best-effort
        (openpyxl optional). De-duped by stem, preferring the text version. Empty when there are none."""
        import re
        from pathlib import Path as _P
        pat = re.compile(r"(result|experiment|benchmark|score|leaderboard)", re.I)
        seen: set[str] = set()
        out: list[str] = []
        used = 0
        for ed in self._editables:
            root = _P(ed["path"])
            if not root.is_dir():
                continue
            try:
                files = sorted((p for p in root.iterdir() if p.is_file() and pat.search(p.name)),
                               key=lambda p: (p.suffix.lower() == ".xlsx", p.name))  # text before xlsx
            except OSError:
                files = []
            for fp in files:
                if used >= cap or fp.stem in seen:
                    continue
                ext = fp.suffix.lower()
                text = None
                if ext in (".md", ".csv", ".tsv", ".txt"):
                    try:
                        text = fp.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        text = None
                elif ext == ".xlsx":
                    text = _xlsx_to_markdown(str(fp))
                if text:
                    seen.add(fp.stem)
                    snip = text[:max(0, cap - used)]
                    out.append(f"--- {fp.name} ---\n{snip}")
                    used += len(snip)
        return "\n\n".join(out)

    def _emit_spec(self) -> dict:
        from looplab.tools._base import fn_spec
        return fn_spec("done",
                        "Call once the file(s) are written and the eval command would run and print "
                        "its metric. Briefly summarize what you wrote.",
                        {"summary": {"type": "string"}}, [])

    def _session_opts(self, *, max_turns=None, time_budget=None) -> dict:
        """loop_opts + the HARD per-session ceiling. A developer session ALWAYS gets a finite bound so
        a model that keeps writing/exploring without ever emitting `done` fails cleanly with the code
        it has written, instead of the 10k-call / multi-hour runaway a big task produced."""
        opts = dict(getattr(self, "loop_opts", {}) or {})
        opts["max_turns"] = int(max_turns if max_turns is not None
                                else getattr(self, "_session_max_turns", 500))
        opts["time_budget_s"] = float(time_budget if time_budget is not None
                                      else getattr(self, "_session_time_budget_s", 1200.0))
        return opts

    def _plan_emit_spec(self) -> dict:
        from looplab.tools._base import fn_spec
        return fn_spec("propose_plan",
                        "Propose an ORDERED plan of ATOMIC implementation steps for this experiment. "
                        "Each step is ONE self-contained, independently-verifiable change (e.g. 'add the "
                        "second-stage fine-tune loop to train.py', 'wire the stage-2 hyperparameters', "
                        "'write the eval entrypoint that prints the metric'). Prefer 2-6 SMALL steps; use "
                        "a single step only if the change is genuinely trivial. Do NOT write code here — "
                        "plan only. Call this exactly once when the plan is ready.",
                        {"steps": {"type": "array", "items": {"type": "object", "properties": {
                            "title": {"type": "string", "description": "short imperative title"},
                            "detail": {"type": "string", "description": "concretely what to change and why"}},
                            "required": ["title"]}}},
                        ["steps"])

    def _propose_plan(self, system: str, idea: Idea, write=None) -> list:
        """Plan phase: a READ-ONLY stage — the developer inspects the real code/experiments (it CANNOT
        write here), and its only exit is `propose_plan` (the ordered atomic plan). Returns a list of
        {title, detail}; [] on empty/failure so the caller falls back to one session."""
        from looplab.agents.agent import run_phase, CompositeTools
        from looplab.tools.env_inspect import EnvInspectTools
        params = ", ".join(f"{k}={v}" for k, v in (idea.params or {}).items()) or "(choose sensible values)"
        plan_user = (
            f"Experiment concept (the researcher's idea): {idea.rationale}\nHyperparameters: {params}.\n"
            "This is the PLANNING stage. You can READ and inspect the repo (read_file — it paginates, so "
            "read a file ONCE, don't re-read; grep, find_files, list_dir, pkg_info, py_api, gpu_info) but "
            "you CANNOT write code yet. Actually READ the relevant source (the eval/entry script, the "
            "files you'll change) and any prior experiment you're building on — enough to know EXACTLY "
            "what to change — THEN call propose_plan with an ordered list of ATOMIC, independently-"
            "testable steps, each naming concretely what to change and why. Do NOT guess from the "
            "truncated preview; the implement stage (and update_plan) come next.")
        messages = [{"role": "system", "content": system}, {"role": "user", "content": plan_user}]
        # READ-ONLY toolset: repo scouts + env inspection, but NO write tools — the plan stage's only
        # output is the plan. (This used to be tools=None to force convergence, which made the planner
        # work BLIND off the truncated preview; the read_file pagination fix + emit_after/emit_force
        # convergence backstop now let it read PROPERLY without exploring forever.)
        read_only = CompositeTools([EnvInspectTools()] + self._scout_tools(write))
        try:
            # Full session budget — same contract as every other phase: the soft nudge at
            # agent_emit_after (300) and the forced emit at agent_emit_force (500) ride in via
            # loop_opts, and budget exhaustion salvages a forced emit. The old tight clamp
            # (40 turns / 360s) starved the planner on a big repo the same way it starved the
            # stages phase (read the repo for the whole budget, degrade to []).
            plan = run_phase(
                self.client, read_only, messages, self._plan_emit_spec(),
                label="Developer·plan", next_label="the implement phase",
                finalize=lambda a: (a or {}).get("steps", []), fallback=lambda m: [],
                **self._session_opts())
        except Exception:  # noqa: BLE001 — a failed plan phase just degrades to a single session
            return []
        steps = []
        for s in (plan or [])[: getattr(self, "_plan_max_steps", 8)]:
            if isinstance(s, dict) and (s.get("title") or s.get("detail")):
                steps.append({"title": str(s.get("title", "")).strip(),
                              "detail": str(s.get("detail", "")).strip()})
        return steps

    def _run_step(self, idea: Idea, step: dict, idx: int, total: int, write, system: str,
                  stage_note: str = "") -> str:
        """Execute ONE atomic plan step in a FRESH bounded session, on top of the files accumulated so
        far (carried in `write.files`; syntax is validated per write by the write tool). A step's own
        error never aborts the plan — later steps + the eval still run on whatever got written.
        `stage_note` restates the node's ACTUAL declared pipeline (or its absence) so a step session
        never assumes a train stage the stages phase didn't produce."""
        from looplab.agents.agent import run_phase, CompositeTools
        from looplab.tools.env_inspect import EnvInspectTools
        done_so_far = ", ".join(write.files) or "(none yet)"
        step_user = (
            f"You are implementing a multi-step plan — STEP {idx} of {total}.\n"
            f"Overall experiment: {idea.rationale}\n{stage_note}\n"
            f"THIS STEP — {step['title']}:\n{step.get('detail') or step['title']}\n\n"
            f"Files CURRENTLY in the workspace (the parent solution + whatever earlier steps wrote — read "
            f"any of them with read_file to see their real content, do NOT assume): {done_so_far}\n"
            "Make ONLY the edits THIS step needs with write_file/edit_file — PATCH existing files, don't "
            "regenerate untouched ones — then call done. Do the minimum for this step; later steps handle "
            "the rest. If this is the last step, make sure the eval entrypoint runs end-to-end.")
        messages = [{"role": "system", "content": system}, {"role": "user", "content": step_user}]
        try:
            # implement steps CONSUME the stages/plan briefs, but don't
            # contribute (their writes add length faster than signal, and the last step is terminal) —
            # so the ledger stays the 3 exploration briefs (propose/stages/plan), never K-step bloat.
            run_phase(self.client, CompositeTools([write, EnvInspectTools()] + self._scout_tools(write)),
                      messages, self._emit_spec(), label=f"Developer·implement step {idx}/{total}",
                      handoff=False, finalize=lambda a: (a or {}).get("summary", ""),
                      fallback=lambda m: "", **self._session_opts())
        except Exception as e:  # noqa: BLE001
            return f"(step {idx} error: {e})"
        return ""

    def _scout_tools(self, write=None):
        """Read-only repo scouts (read_file / grep / find_files / list_dir) so the Developer can READ
        the code it is EDITING and VERIFY an exact CLI flag / function signature / config key in the
        ACTUAL source instead of GUESSING it — guessing an arg the embedded (truncated) source didn't
        show is a top cause of a training crash. Reuses the SHARED RepoScoutTools (path-safe +
        secret-filtered), bound to the editable repo roots with repo-relative paths (the SAME paths as
        write_file/edit_file). `write.files` is passed as the STAGED overlay so read/grep see the code
        the Developer is currently writing — not the pristine on-disk repo (reading a parent/merge
        source is a separate, secondary concern)."""
        roots = [e["path"] for e in (getattr(self, "_editables", None) or []) if e.get("path")]
        if not roots:
            return []
        from looplab.tools.reposcout import RepoScoutTools
        overlay = write.files if write is not None else None      # live dict the write tools mutate
        deleted = write.deleted if write is not None else None    # staged deletions hidden from read/grep/list
        # (name, path) per editable — MIRRORS RepoWriteTools._roots so a scout hit is rendered/deduped with
        # the SAME `<name>/rel` key the write tools use in a multi-editable repo (round-trips into an edit).
        named = [(e.get("name") or "", e["path"]) for e in (getattr(self, "_editables", None) or []) if e.get("path")]
        return [RepoScoutTools(roots=roots, default_root=roots[0], overlay=overlay, deleted=deleted,
                               named_roots=named)]

    def _stages_emit_spec(self) -> dict:
        from looplab.tools._base import fn_spec
        return fn_spec("declare_stages",
                        "Declare the ORDERED pipeline stages for this experiment and finish the stages "
                        "phase. Each stage is {name, command:[argv...], timeout?, check?}; they run IN "
                        "ORDER in the same workdir so artifacts (a trained checkpoint, prepared data) "
                        "persist to later stages. Put `%params%` in a command to inject THIS node's "
                        "hyperparameters as `--key value`, or bake the values into the argv yourself. "
                        "Give a long training stage a GENEROUS timeout (seconds).",
                        {"stages": {"type": "array", "items": {"type": "object", "properties": {
                            "name": {"type": "string"},
                            "command": {"type": "array", "items": {"type": "string"}},
                            "timeout": {"type": "number"}, "check": {"type": "boolean"}},
                            "required": ["name", "command"]}}},
                        ["stages"])

    def _cmd_context(self) -> tuple[dict, bool]:
        """The operator's scoring contract (eval_spec) + whether one exists. The stages phase shows it to
        the Developer as IMMUTABLE (the engine appends it as the final protected `score` stage); with no
        cmd the Developer must declare the FULL pipeline including a final scoring stage."""
        ev = {}
        try:
            ev = self.task.eval_spec() or {}
        except Exception:  # noqa: BLE001 — a task without eval_spec (toy/tests) => no cmd, full pipeline
            ev = {}
        # Onboard mode: `eval` is None until the adapter is ratified, but the onboard COMMAND is the scorer
        # (the frozen metric adapter reads ITS output). Treat it as the immutable cmd so the stages phase
        # declares PRECEDING train/prep stages around it — NOT a full pipeline whose own score stage would
        # fight the onboarder's adapter (that broke the onboarding run: finished=False).
        if not ev.get("command") and not ev.get("stages"):
            oc = getattr(self.task, "onboard_command", None)
            if oc:
                ev = {**ev, "command": list(oc)}
        has_cmd = bool(ev.get("command") or ev.get("stages"))
        return ev, has_cmd

    def _operator_stage_list(self) -> list:
        """The validated OPERATOR-declared `cmd.stages` pipeline, or []. Gated on the SAME shared
        validation the engine's _resolve_stages applies at consume time (NOT truthiness): a VALID
        operator list is taken verbatim there and any Developer manifest is IGNORED, while an invalid
        one falls through to the Developer manifest — so 'operator stages exist' here means exactly
        'the engine will run them and ignore looplab_stages.json'. Soft-fails to [] for a bare/
        __new__-constructed dev (unit tests) that carries no task."""
        try:
            ev = self._cmd_context()[0]
        except Exception:  # noqa: BLE001 — no task/eval_spec (toy & unit-test devs) => no operator stages
            return []
        if not ev.get("stages"):
            return []
        from looplab.runtime.command_eval import validate_stages
        return validate_stages(ev["stages"])[0] or []

    def _repair_stage_note(self, op_stages: list, write) -> str:
        """Restate the node's ACTUAL pipeline for a REPAIR session, when it is knowable (P33): the
        system prompt tells the model to trust the task message's pipeline, so a repair message must
        actually carry one where the info exists — operator stages from the eval spec, else the
        Developer manifest riding in the seeded working set. Empty when neither is known (the system
        clause is conditional: 'when the task message states…')."""
        if op_stages:
            chain = " → ".join(str(s.get("name")) for s in op_stages)
            return (f"\nPIPELINE for this node (OPERATOR-declared, runs verbatim): {chain}. "
                    "The stage manifest cannot change it — fix the failing stage's script instead.")
        stages = self._materialized_stage_list(write)
        chain = " → ".join(str(s.get("name")) for s in stages)
        if chain:
            return (f"\nPIPELINE for this node (from its staged looplab_stages.json): {chain} → "
                    "score (operator cmd).")
        return ""

    def _materialized_stage_list(self, write) -> list:
        """The stage pipeline currently MATERIALIZED in the working set's looplab_stages.json — e.g.
        the PARENT's manifest carried over on an improve (base preload). This is exactly what the
        eval's `_resolve_stages` runs when the STAGES phase declares nothing new, so validate it the
        SAME way the eval does (reserved 'score'): an invalid manifest the eval would DROP to the
        single command returns [] here too, keeping the implement prompt in step with the eval."""
        import json as _json
        from looplab.runtime.command_eval import materialized_stages
        try:
            obj = _json.loads(write.files.get("looplab_stages.json", ""))
        except (ValueError, TypeError):
            return []
        # ONE source of truth with the eval's `_resolve_stages`: `materialized_stages` accepts both the
        # wrapped {"stages":[...]} shape declare_stages authors AND a bare top-level JSON list
        # (hand-written / write_file / pre-redesign manifests), and drops an invalid manifest to None
        # exactly as the eval drops it to the single command. Sharing the helper (not a hand-copied
        # parse kept "in lock-step" by comment) is what guarantees the implement prompt advertises the
        # SAME pipeline the eval will run (M7) — they can no longer drift.
        return materialized_stages(obj) or []

    def _stages_user(self, idea: Idea, ev: dict, has_cmd: bool) -> str:
        import json as _json
        params = ", ".join(f"{k}={v}" for k, v in (idea.params or {}).items()) or "(bake sensible values)"
        if has_cmd:
            cmd_desc = _json.dumps(ev.get("stages") or ev.get("command"), ensure_ascii=False)[:800]
            metric = _json.dumps(ev.get("metric"), ensure_ascii=False)[:200]
            contract = (
                f"The operator's SCORING command is FIXED (you may NOT change it): `{cmd_desc}`; it reads "
                f"the metric via {metric}. The engine appends it as the final, protected `score` stage. "
                "Your job: declare the ordered stages that run BEFORE it (do NOT include a `score` stage — "
                "it's reserved), producing whatever that scorer reads (a trained checkpoint, prepared data).")
        else:
            contract = (
                "There is NO operator scoring command — declare the FULL pipeline, INCLUDING a final stage "
                "that runs the evaluation and PRINTS the metric the task's metric reader parses. Name that "
                "final stage e.g. `evaluate` — the name `score` is RESERVED (it always denotes an "
                "engine-appended operator step and will be rejected).")
        return (
            f"Experiment concept (the researcher's idea): {idea.rationale}\nHyperparameters for THIS node: "
            f"{params}.\n\nThis is the STAGES phase (first). {contract}\n\n"
            "READ the repo to ground the stages in the ACTUAL entry scripts/args (read_file paginates — "
            "read a file ONCE; grep, find_files, list_dir, pkg_info, py_api). GOOD PRACTICE: separate "
            "stages for data/feature PREPARATION, TRAINING (a fresh model every node — the pipeline must "
            "not point at another experiment's checkpoint), and TESTING; bake this node's "
            "hyperparameters into the `train` command (or use "
            "`%params%`). Give training a generous timeout. Then call `declare_stages` once. You are NOT "
            "writing code yet — the plan + implement phases come next.")

    def _declare_stages_phase(self, idea: Idea, write, system: str) -> list:
        """Stages phase (MANDATORY, FIRST): a READ-ONLY phase where the Developer studies the repo + the
        operator's cmd and emits `declare_stages` — the ordered pipeline (prep → train → …) that runs
        before the protected `score` step. Writes `looplab_stages.json`. Returns the clean stage list ([]
        on failure — the eval then falls back to just the operator cmd)."""
        from looplab.agents.agent import run_phase, CompositeTools
        from looplab.tools.env_inspect import EnvInspectTools
        from looplab.runtime.command_eval import validate_stages
        import json as _json
        ev, has_cmd = self._cmd_context()
        reserved = ("score",)   # `score` is ALWAYS the engine-appended final stage — consume-side reserves it too
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": self._stages_user(idea, ev, has_cmd)}]
        # scouts read the LIVE overlay (the parent solution on improve/merge), not the pristine repo
        read_only = CompositeTools([EnvInspectTools()] + self._scout_tools(write))

        def _validate(args):                      # bounce a malformed manifest back to the model
            stages = (args or {}).get("stages")
            _, err = validate_stages(stages, reserved=reserved)
            if err:
                return err
            miss = _missing_stage_input_paths(stages)   # a hallucinated non-existent data path → re-declare
            return _missing_paths_feedback(miss) if miss else None

        def _finalize(args):
            clean, _ = validate_stages((args or {}).get("stages"), reserved=reserved)
            # PERSIST a well-formed manifest even if a path still looks missing. The missing-path guard
            # is a RETRYABLE bounce on the `_validate` path (where the model can re-declare a real path);
            # by the time finalize runs the retries are spent. Dropping to [] here would silently degrade
            # the node to the operator's score cmd ALONE — which, on a repo carrying a committed baseline
            # checkpoint, "succeeds" scoring a model this node never trained (a silent stale/forged
            # metric, the worst outcome for the search). A stage pipeline that FileNotFoundErrors at eval
            # is instead LOUD and recoverable — inline repair can fix the path. So ship it, don't hide it.
            if clean:
                write.files["looplab_stages.json"] = _json.dumps({"stages": clean}, indent=1)
                return clean
            return []
        try:
            # Full session budget — the old tight clamp (30 turns / 300s) starved this phase on a
            # big repo: it read for the whole budget, never reached declare_stages, and silently
            # degraded to "no stages declared" (the node then evaluated as a bare single command —
            # observed live). The soft nudge (agent_emit_after=300) / forced emit (agent_emit_force
            # =500) convergence backstop + exhaustion salvage now bound it like every other phase.
            return run_phase(
                self.client, read_only, messages, self._stages_emit_spec(),
                label="Developer·stages", next_label="the plan & implement phases",
                finalize=_finalize, fallback=lambda m: [], validate=_validate,
                **self._session_opts()) or []
        except Exception:  # noqa: BLE001 — a failed stages phase degrades to the operator cmd alone
            return []

    def _run(self, idea: Idea, error: Optional[str] = None,
             base: Optional[dict] = None, base_note: str = "",
             base_deleted: Optional[list] = None) -> str:
        from looplab.agents.agent import run_phase
        from looplab.core import tracing
        # Resolved ONCE for the whole node: operator `cmd.stages` make declare_stages refuse (P12)
        # and drive the stage notes below; data-mount names make mount refusals honest.
        op_stages = self._operator_stage_list()
        write = RepoWriteTools(self._surface, self._protected, self._prefixes, editables=self._editables,
                               operator_stages=bool(op_stages),
                               data_mounts=getattr(self, "_data_mounts", None))
        if base is not None or base_deleted is not None:
            # An EXPLICIT base is the node's OWN solution — the parent's (improve/refine via
            # implement_from) or the failing node's (repair via repair_from). Pre-load it so untouched
            # files carry over verbatim (cumulative diff — the agent PATCHES, doesn't regenerate from
            # the pristine repo) and deletions carry too (else the workdir re-seeds the pristine repo
            # with a deleted file RESTORED). This WINS over `last_files` even for a repair, because the
            # shared developer instance's `last_files` holds whatever node it BUILT LAST — almost never
            # the node being repaired (the create-batch builds every node before any eval).
            write.files = dict(base or {})
            write.deleted = list(base_deleted or [])
        elif error and (self.last_files or self.last_deleted):   # legacy repair (no explicit base):
            write.files = dict(self.last_files)                  # best-effort carry of the last build
            write.deleted = list(self.last_deleted)
        params = ", ".join(f"{k}={v}" for k, v in (idea.params or {}).items()) or "(choose sensible values)"
        from looplab.core.hardware import operational_attention_points
        from looplab.core.prompts import render
        system = (
            render(self.prompts, "repo_developer_system_intro", _REPO_DEV_SYSTEM_INTRO)
            + self.brief + "\n\n"
            + render(self.prompts, "repo_developer_system_body", _REPO_DEV_SYSTEM_BODY)
            + operational_attention_points() + "\n\n"
            + _REPO_DEV_COMMANDS_HEADER + self._recipes() + "\n\n"
            + ((_REPO_DEV_RESULTS_HEADER + _results + "\n\n")
               if (_results := self._results_context()) else "")
            + _REPO_DEV_SOURCE_HEADER + self._repo_context())
        user = (f"Experiment concept (the researcher's idea): {idea.rationale}\nHyperparameters to use: {params}.\n"
                "Design and implement the eval entrypoint (and any edits) now with write_file, then call done.")
        if base:
            cap_each, cap_total, used = 8000, 24000, 0
            parts = []
            for name, body in base.items():
                b = str(body or "")[:cap_each]
                if used + len(b) > cap_total:
                    parts.append(f"--- {name} --- (omitted for space)")
                    continue
                used += len(b)
                parts.append(f"--- {name} ---\n{b}")
            user += (_REPO_DEV_PARENT_BLOCK.format(note=(f"; {base_note}" if base_note else ""))
                     + "\n\n".join(parts))
        if error:
            # {already} lists the files ACTUALLY seeded for THIS repair — `write.files` (repair_from
            # pre-loads the failing node's own files there; the legacy no-base fallback copies
            # last_files into it too), NOT self.last_files, which holds whatever node this shared
            # developer instance built LAST (P11: the prompt named the wrong node's files).
            already = ", ".join(write.files) or "(none)"
            # A repair session gets the node's ACTUAL pipeline restated when knowable (P33) — the
            # system prompt's "trust the task message's pipeline" clause is conditional on it.
            user += self._repair_stage_note(op_stages, write)
            user += _REPO_DEV_REPAIR_BLOCK.format(already=already) + error[:4000]
        # A fresh implement (not a repair) on a real repo runs THREE explicit, separately-traced phases —
        # each its own focused tool-loop + emit so the context stays small and the trace reads cleanly
        # (Developer · stages → plan → implement):
        #   1. STAGES (mandatory, unless the operator declared `eval.stages` or protected the manifest):
        #      declare the ordered eval pipeline (prep → train → …) around the operator's protected
        #      `score` cmd — hardcoding this node's train params / adding a data_prep stage where useful.
        #      The Developer knows the repo; the planner (Genesis) may not.
        #   2. PLAN: decompose the code changes into ATOMIC steps (C4 — bounds a non-converging model).
        #   3. IMPLEMENT: write the code, one bounded session per plan step (each step its own trace block).
        # A REPAIR (error set) OR a bare / __new__-constructed dev (unit tests, no `_editables`) skips
        # straight to a single bounded session — repair is already narrow; the toy dev has no repo to stage.
        is_fresh_repo = error is None and getattr(self, "_editables", None)
        from looplab.agents.agent import CompositeTools
        from looplab.tools.env_inspect import EnvInspectTools
        try:
            operator_stages: list = []
            declared: list = []
            carried_over = False   # M7: declared came from a carried-over parent manifest, not this phase
            manifest_protected = False
            if is_fresh_repo:
                # Skip the STAGES phase when the OPERATOR already declared an `eval.stages` pipeline the
                # engine will actually USE: _resolve_stages takes a VALID operator list verbatim (a
                # Developer manifest would be IGNORED) but falls through to the Developer manifest on an
                # invalid one — `_operator_stage_list` gates on that SAME shared validation, not
                # truthiness. Protecting
                # `looplab_stages.json` is the operator knob that disables Developer pipelines entirely:
                # skip the phase (its manifest could never materialize) instead of burning a full LLM
                # loop whose output workspace-materialization silently drops.
                operator_stages = op_stages
                manifest_protected = SurfacePolicy(
                    None, self._protected, self._prefixes, protected_exact=True,
                    check_escapes=False).check("looplab_stages.json") is not None
                if operator_stages:
                    declared = operator_stages
                elif not manifest_protected:
                    # STAGES is the Developer's own sub-phase (its own trace band, via the phase
                    # stamped on its generations).
                    with tracing.operation("stages"):
                        declared = self._declare_stages_phase(idea, write, system) or []
                    # M7: a DEGRADED stages phase (declared == []) leaves any PARENT manifest carried
                    # over on an improve (base preload) still materialized in write.files, and the
                    # eval's _resolve_stages WILL run it. Recompute `declared` from that materialized
                    # manifest so the implement prompt matches the pipeline the eval actually uses —
                    # otherwise the model is told "no stages, train a FRESH model" while the parent's
                    # prep→train stages run (the model trains twice; the reported metric reflects the
                    # entrypoint's own training, not the declared pipeline).
                    if not declared:
                        declared = self._materialized_stage_list(write)
                        carried_over = bool(declared)
                # Tell the implement sessions what pipeline ACTUALLY exists. The old prompt asserted
                # "your STAGES phase already declared a train stage" unconditionally — after a failed/
                # empty stages phase the model then wrote a score-only entrypoint that scored a stale
                # checkpoint (or crashed on a missing one) instead of training.
                _chain = " → ".join(str(s.get("name")) for s in declared)
                if operator_stages:
                    stage_note = (f"\nPIPELINE for this node (OPERATOR-declared, runs verbatim): "
                                  f"{_chain}. Implement the code those stages run.")
                elif declared:
                    _src = ("carried over from the parent solution — your STAGES phase declared "
                            "nothing new this node" if carried_over else "declared by your STAGES phase")
                    stage_note = (f"\nPIPELINE for this node ({_src}): {_chain} "
                                  "→ score (operator cmd). Implement the code those stages run; the "
                                  "eval entrypoint only SCORES the artifacts the earlier stages produce.")
                else:
                    stage_note = ("\nNO pipeline stages are declared for this node"
                                  + (" (the operator protected looplab_stages.json)"
                                     if manifest_protected else "")
                                  + ": the operator's cmd runs ALONE as a single command. The code it "
                                  "runs must do ALL the work itself when invoked — train a FRESH model, "
                                  "then score it and print the metric (never read a pre-existing "
                                  "checkpoint or a static results file).")
                user += stage_note
            messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
            # Compose the write/edit tools with read-only ENVIRONMENT INTROSPECTION (pkg_info / py_api /
            # read_installed / grep_installed) so the Developer grounds generated code in the ACTUAL
            # installed API/version instead of guessing (the precision='16-mixed'-on-Lightning-1.5 class).
            tools = CompositeTools([write, EnvInspectTools()] + self._scout_tools(write))
            if is_fresh_repo:
                # PLAN is the Developer's second sub-phase (its own trace band). IMPLEMENT runs under
                # the orchestrator's "implement" span (so its generations band there, and non-repo
                # developers keep that band unchanged).
                steps = []
                if getattr(self, "_plan_decompose", False):
                    with tracing.operation("plan"):
                        steps = self._propose_plan(system, idea, write)
                if len(steps) >= getattr(self, "_plan_min_steps", 2):
                    for i, step in enumerate(steps, 1):
                        self._run_step(idea, step, i, len(steps), write, system,
                                       stage_note=stage_note)  # a step error can't abort the plan
                else:
                    # single-session implement is TERMINAL (evaluation reads no brief) → consume the
                    # briefs + read-cache, but no wasted summary call (handoff=False).
                    run_phase(self.client, tools, messages, self._emit_spec(),
                              label="Developer·implement", handoff=False,
                              finalize=lambda a: (a or {}).get("summary", ""),
                              fallback=lambda m: "", **self._session_opts())
            else:
                # repair / toy single session — terminal, so no summary (and repair isn't in a scope
                # anyway when it runs inline during eval; the debug-operator repair gets an empty ledger).
                run_phase(self.client, tools, messages, self._emit_spec(),
                          label=("Developer·repair" if error else "Developer·implement"), handoff=False,
                          finalize=lambda a: (a or {}).get("summary", ""),
                          fallback=lambda m: "", **self._session_opts())
        except Exception as e:  # noqa: BLE001 - never crash the engine on a developer hiccup
            self.last_files = dict(write.files)
            self.last_deleted = list(write.deleted)
            return f"(developer error: {e})"
        self.last_files = dict(write.files)
        self.last_deleted = list(write.deleted)
        return ""

    def implement(self, idea: Idea) -> str:
        return self._run(idea)

    def implement_from(self, idea: Idea, parent) -> str:
        """Improve/refine: start from the PARENT node's solution and patch it (see _run(base=...)).
        Falls back to a from-scratch implement when the parent carries no files AND no deletions
        (e.g. seeded rows)."""
        files = dict(getattr(parent, "files", {}) or {})
        deleted = list(getattr(parent, "deleted", []) or [])
        if not files and not deleted:
            return self._run(idea)
        note = f"parent experiment #{getattr(parent, 'id', '?')}, metric={getattr(parent, 'metric', None)}"
        return self._run(idea, base=files, base_note=note, base_deleted=deleted)

    def repair(self, idea: Idea, code: str, error: str) -> str:
        return self._run(idea, error=error)

    def repair_from(self, idea: Idea, node, error: str) -> str:
        """Repair seeded from the FAILING NODE's OWN files (not the shared developer's `last_files`,
        which holds whatever node it built last — almost never this one). Falls back to the legacy
        last_files carry only when the node has no files (single-file / non-repo)."""
        files = dict(getattr(node, "files", {}) or {})
        deleted = list(getattr(node, "deleted", []) or [])
        if not files and not deleted:
            return self._run(idea, error=error)
        return self._run(idea, error=error, base=files, base_deleted=deleted)


class LLMOnboarder:
    """Phase 3 onboarder: the operator gives the framework's command; the Developer writes a
    metric `adapter` (read_metric(workdir)->float) that extracts the metric from whatever
    tracker/logs the run produced (TensorBoard / MLflow / metrics file / stdout). Returns a
    proposal that a human ratifies (then it's frozen + protected). Writing the adapter code
    is the Developer's job — onboarding reuses the same role, not a bespoke agent."""

    _SYS = ("You write a single Python module that reads the FINAL evaluation metric a "
            "training run produced. Output ONLY one ```python``` block defining "
            "`read_metric(workdir: str) -> float`.")

    def __init__(self, client, repo_path, goal, direction, command, timeout):
        self.client = client
        self.repo_path = repo_path
        self.goal = goal
        self.direction = direction
        self.command = command
        self.timeout = timeout

    def _context(self) -> tuple[str, str]:
        """Repo listing + the contents of a few small text files (the entrypoint, configs)
        so the Developer can see the actual metric shape it must read."""
        from pathlib import Path as _P
        import itertools
        root = _P(self.repo_path)
        _skip = {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache", ".pytest_cache"}
        # Bound the walk: `rglob("*")` on a large repo (e.g. a checked-in dataset) fully materializes
        # every path — cap it. And guard every stat/read_text with OSError: one permission-denied file
        # would otherwise crash Phase-3 onboarding at run start.
        def _is_file_safe(p) -> bool:
            try:
                return p.is_file()
            except OSError:
                return False
        try:
            walked = itertools.islice(
                (p for p in root.rglob("*") if _skip.isdisjoint(p.parts)), 5000)
            files = [p for p in walked if _is_file_safe(p)]
        except OSError:
            files = []
        listing = "\n".join(str(p.relative_to(root)) for p in files[:60])
        snippets, exts = [], (".py", ".json", ".yaml", ".yml", ".cfg", ".toml", ".txt")
        for p in files:
            try:
                if p.suffix in exts and p.stat().st_size < 4000:
                    snippets.append(f"--- {p.relative_to(root)} ---\n"
                                    + p.read_text(encoding="utf-8", errors="replace")[:2000])
            except OSError:
                continue
            if len(snippets) >= 6:
                break
        return listing, "\n\n".join(snippets)

    def __call__(self) -> dict:
        from looplab.core.parse import extract_code
        cmd = " ".join(self.command) or "(the project's training command)"
        listing, snippets = self._context()
        user = (f"Repository files:\n{listing}\n\nKey file contents:\n{snippets}\n\n"
                f"The training command `{cmd}` runs in the work directory. Goal: {self.goal} "
                f"({self.direction}imize). Write `read_metric(workdir)` that, AFTER the run, "
                "returns the final metric by reading what the framework wrote FOR THIS RUN (match the "
                "metric key/format you see in the files above — e.g. a JSON like "
                '{"metric": <float>}). Read ONLY the CURRENT run\'s freshly-written output; NEVER read a '
                "pre-existing/committed results file or a prior run's checkpoint (e.g. results_last.csv is "
                "a PRIOR run's output, not this run's score). Prefer stdlib; if you use an optional tracker lib "
                "(tensorboard/mlflow), import it INSIDE a try/except and fall back. Return a "
                "float; on any problem return a clearly-bad value so the run is not rewarded.")
        try:
            code = extract_code(self.client.complete_text(
                [{"role": "system", "content": self._SYS}, {"role": "user", "content": user}]))
        except Exception as e:  # noqa: BLE001 — propose a stub; human will reject/fix
            code = f"def read_metric(workdir):\n    raise RuntimeError({str(e)!r})\n"
        return {
            "eval_spec": {"command": list(self.command),
                          "metric": {"kind": "adapter", "path": "LOOPLAB_adapter.py"},
                          "params_style": "none", "timeout": self.timeout},
            "adapter_files": {"LOOPLAB_adapter.py": code},
            "goal": self.goal,
        }
