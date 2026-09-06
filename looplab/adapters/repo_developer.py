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

import math as _math

from typing import Optional

from looplab.core.models import Idea, DEVELOPER_ERROR_PREFIX, DEVELOPER_STUCK_PREFIX
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

# THE PLAN PHASE IS READ-ONLY AND ITS SYSTEM PROMPT SAYS THE OPPOSITE.
#
# `_propose_plan` builds a CompositeTools with no writer, and its user message says so in words --
# "you CANNOT write code yet". The SYSTEM prompt above it opens with "You improve an existing
# experiment repository by WRITING code with the write_file and edit_file tools", and the system
# prompt is the one the model believes.
#
# MEASURED over the 76-run probe corpus on 2026-09-03: `write_file` is called 51 times from the
# `plan` phase and ALL 51 error, against 716 calls from `plan_step` (which does have the tool) and
# 15 from `card_build`. 504 of the 528 `plan` chain-roots (95.5 %) carry a system prompt naming
# `write_file`. So one run in ten spends a turn discovering a contradiction the prompt put there.
#
# WIRED IN 2026-09-04, at the top of `_propose_plan`, once §115's arm closed (§180). Before that
# the function existed and changed nothing, on purpose: it alters what every probe is told, and an
# arm in flight is the wrong time to alter that.
def read_only_intro(system: str) -> str:
    """`system` with the write-tools promise replaced by the truth for a read-only phase.

    Returns the string unchanged when the sentence is not there, so an operator who has overridden
    `repo_developer_system_intro` through the prompt store gets their own text back untouched
    rather than a silently half-rewritten one.
    """
    promise = ("You improve an existing experiment repository by WRITING code with the write_file "
               "and edit_file tools (edit_file for changes to existing files, write_file for new "
               "ones).")
    truth = ("You improve an existing experiment repository by writing code -- but NOT in this "
             "phase: here you can only READ, and write_file and edit_file are not available to "
             "you. They come back in the stage after this one.")
    return system.replace(promise, truth, 1)


# 2026-08-07: the "THAT FILE MUST EXIST in the workspace after your edits" rule below carries exactly
# one carve-out — "unless the operator PROTECTED an existing scorer, which you must NOT rewrite" — and
# that sentence is only TRUE because `engine/workspace.py::seed_protected_files` materializes the
# operator's `protect` entries into every node workdir. It was NOT true before: `seed_mode="auto"`
# seeds git-TRACKED files, so an uncommitted protected scorer was simply absent and this clause is
# what told the model not to check. The model could not have checked usefully anyway — every view it
# 2026-08-12: "the operator PROTECTED" now also covers the DERIVED case — when the operator's `cmd`
# names a file the editable repo already ships, `RepoTask._entrypoint_protect` folds it into that
# repo's `protect`, so it reaches seeding by the same route an explicit entry does and the carve-out
# below stays true without the operator having written anything.
# has of "the repo" (`_repo_context`, `_recipes`, `_results_context` and the `_scout_tools` scouts) is
# rooted at the editable SOURCE, never at the node workdir the eval runs in, so `read_file` answers
# for a filesystem the command never sees. Measured on runs/rubert-dr-0807 node 2: while the score
# stage was dying on `can't open file '<workdir>/looplab_eval.py'`, the repair session's
# `read_file("looplab_eval.py")` returned the file, and its two `write_file("looplab_eval.py")`
# attempts were both refused by the write gate — the loop had no move left. If seeding ever stops
# covering `protect`, this sentence becomes a lie again and the repair loop becomes unbounded.
_REPO_DEV_SYSTEM_BODY_HEAD = (
    "The repository's key source files are PREVIEWED below (each is TRUNCATED to save space). This is "
    "a preview, NOT the full code — to read a whole file or find an exact symbol/flag/signature, use "
    "the read-only repo scouts: read_file(path) for full content (repo-relative, e.g. train.py), "
    "grep(pattern) to find where something is defined across the repo, find_files(root, pattern) / "
    "list_dir(path) to see what exists. ")

# The ONE clause that depends on whether the PROBE is wired (F2, `Settings.developer_probe`). Kept as
# two alternatives spliced at the SAME position rather than a paragraph appended at the end, so a run
# with the probe off gets a byte-identical system prompt to the one this feature found — the composed
# `_REPO_DEV_SYSTEM_BODY` below is still the PromptStore default for `repo_developer_system_body`.
_REPO_DEV_NO_EXECUTION = (
    "Do NOT write helper/'cat'/'check' scripts. "
    "There is NO shell / bash / run-command tool — you CANNOT execute anything yourself: your ONLY "
    "actions are write_file/edit_file (author code) and the read-only scouts below. The eval runs your "
    "code afterwards. (Calling a 'bash'/'run' tool just wastes a turn — it does not exist.) ")

# The replacement when the probe IS wired. The sentence it replaces is the one that produced the
# defect F2 exists to close — a Developer told "you cannot execute anything" concluded, verbatim,
# "Since I have no shell/install ability, the cleanest repair is a small loguru shim module", and
# wrote a fake library rather than spending one line finding out the real one was importable. So the
# WORK-AROUND instruction is the load-bearing half here, not the tool announcement.
_REPO_DEV_PROBE_EXECUTION = (
    "Do NOT write helper/'cat'/'check' scripts into the repo — that is what run_probe is for. "
    "There is still NO shell / bash tool and you cannot run your node's pipeline yourself; the eval "
    "runs your code afterwards. What you DO have is run_probe(code): a short PYTHON program, run "
    "against the REAL environment, to CHECK anything you would otherwise have to guess — whether a "
    "package actually imports HERE, whether a data file parses, what a config resolves to, whether "
    "the code you just staged gets an API right. "
    "PROBE BEFORE YOU WORK AROUND SOMETHING: if you are about to write a shim, a stub, a vendored "
    "copy or a try/except ImportError fallback because you are not sure something exists, probe it "
    "first — it usually does exist, and a shim that shadows a real library is a defect this run pays "
    "for later. A probe OBSERVES and changes nothing: it cannot write files, install packages (there "
    "is no pip here — a genuinely missing dependency is something to REPORT in your summary, not to "
    "work around), start other programs, or use a GPU, and it runs in a copy of the files you have "
    "staged, never in the operator's source tree. Anything that must PRODUCE a file belongs in your "
    "node's files and its declared stages. ")

# Exact commands are a different authority from the free-form probe. The model can select a name,
# but every executable detail comes from the operator-owned task snapshot and the result workspace is
# thrown away. Kept as two variants so a command-enabled/probe-disabled task never promises run_probe.
_REPO_DEV_PINNED_EXECUTION = (
    "Do NOT write helper/'cat'/'check' scripts into the repo. You have NO arbitrary shell and cannot "
    "invent a command or its arguments. What you DO have is run_dev_command(name): select one of "
    "the exact compile/test/lint/data-validation commands the OPERATOR pinned in this task. It runs "
    "against a disposable candidate copy containing the seeded repo plus your currently staged edits. "
    "Use it to verify a change instead of guessing. Changes inside its candidate tree are DISCARDED; "
    "declared mounts retain their task/trust-tier policy. Persist a fix only with "
    "write_file/edit_file. A long training/eval job still belongs in the "
    "node's declared stages. ")

_REPO_DEV_PINNED_AND_PROBE_EXECUTION = (
    _REPO_DEV_PINNED_EXECUTION
    + "For an ad-hoc Python-only question not covered by a pinned command, run_probe(code) is also "
      "available under its stricter observe-only boundary: it cannot write, spawn, install, use a "
      "GPU or read the operator's source tree. PROBE BEFORE inventing a shim/stub/fallback for an "
      "environment fact. ")

_REPO_DEV_SYSTEM_BODY_TAIL = (
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
    "run is scored (that's the trust boundary), only add work before it. That covers the stage AND the "
    "code it runs: when the operator's cmd names a script or module the repo already ships, that file is "
    "PROTECTED and your write/edit tools will refuse it. Stages run in ORDER in the SAME "
    "workdir (artifacts persist: `train` writes a checkpoint the `score` step reads). This is the ONLY "
    "correct way to get 'a failed step is fixed and re-run WITHOUT paying to re-train': the ENGINE reuses "
    "the completed `train` stage's checkpoint and re-runs only what changed (a FRESH node still trains "
    "from scratch — stages are tracked PER NODE, never inherited). Give `train` a GENEROUS `timeout` that "
    "covers the full schedule (epochs × minutes/epoch × 60 — the default is short and would SIGKILL a long "
    "train into an undertrained checkpoint). Put `%params%` inside a stage command to inject THIS node's "
    "hyperparameters as `--key value`, or bake the values into the code yourself. Do NOT hand-roll a "
    "single monolithic entrypoint with a 'skip training if a checkpoint already exists' check: the engine "
    "can't see stage boundaries there, so it can't re-run just the scoring — and the ARTIFACT rules below "
    "explain why such a check silently freezes the metric. AND DO NOT PUT THAT CHECK IN THE SCORER, "
    "EITHER — whether you wrote the scoring entrypoint or the repo shipped it, NEVER make it train, "
    "subprocess out to the training script, or 'produce a checkpoint if one is missing'. The scorer's ONE "
    "job is to score the artifact the TRAIN STAGE produced: if it can retrain, then when its artifact path "
    "is wrong it does not FAIL — it quietly trains a second model and reports a number from a run the "
    "pipeline never measured, on top of the training you already paid for (and, if the config overwrites, "
    "destroying the train stage's artifact). This has happened: a scorer whose checkpoint path pointed "
    "outside the node's workdir found nothing, re-ran training inside the score stage, and cost the run "
    "2x GPU per node for a metric that did not belong to its own pipeline. A scorer that cannot find its "
    "artifact must CRASH — that is a one-line path fix on the next repair; a silent retrain is not "
    "repairable because nothing reports it. THE SAME APPLIES TO A MONOLITH YOU INHERITED: "
    "when the repo's own training entrypoint also mines, prepares or evaluates inside the same process, "
    "do NOT declare it as your only stage. SPLIT AS FAR AS THE REPO ALLOWS — every boundary you declare "
    "is a boundary a repair can restart from, and the LAST one you declare is the most valuable, because "
    "scoring is where a wrong path or a bad metric read shows up and it is the cheapest thing to redo. "
    "If the repo has a separate eval entrypoint that loads a saved checkpoint from disk, declare `train` "
    "and the eval as SEPARATE stages even though training already evaluated: one stage means every "
    "repair re-trains from scratch, which is exactly what `inline_repair_retrain_cap` exists to avoid and "
    "cannot when there is nothing to reuse. A run has already lost 76 minutes of correct GPU training to "
    "a one-character path error in a single-stage manifest.\n"
    "    But do NOT invent a stage for work that is already done: if the mined negatives, the prepared "
    "shards or the built index are ALREADY ON DISK and this experiment does not change them, that is a "
    "reason to have no such stage, not a gap to fill. A stage exists to make a step repeatable and "
    "restartable, never to look thorough. Declare the steps THIS experiment actually performs.\n"
    "    VERIFY the artifact paths you declare rather than reading them off a config field: many repos "
    "COMPOSE the output directory (run name plus model name, a timestamp, a rank suffix), so the path in "
    "the config is NOT the path on disk. Read the code that builds it. Declaring a path you have not "
    "traced fails the stage AFTER it has spent its full runtime, with everything it produced intact and "
    "unusable. `declare_stages` "
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
    "STAGE CHECKS — every stage must be able to FAIL. Exiting 0 is not evidence a stage worked: a "
    "stage that produced 1% of what it was supposed to produce exits 0 exactly like one that "
    "produced 100%, and the next stage then consumes the 1% as if it were complete. This has "
    "happened: a hard-negative mining stage wrote its output file and exited 0 having mined "
    "negatives for 9,364 of 764,676 queries — 1.2% — and the training stage was handed "
    "`--n_hard_negatives 4` as though every query had them. Nothing objected, and the whole node's "
    "result was meaningless. So:\n"
    "  • PRINT the numbers that show the stage did its job — counts, coverage, shapes, ratios "
    "(\"mined hard negatives for 731,203 / 764,676 queries (95.6%)\", \"wrote 512 x 384 embedding "
    "matrix\"). Print them even when they are fine; an unprinted number cannot be checked by "
    "anything, including you on the next attempt.\n"
    "  • ASSERT the ones that must hold, in the stage's own code, and let the assert KILL the stage "
    "(a bare `assert`, or a check that raises / sys.exit(1)). A stage that manufactures the training "
    "signal must fail loudly when it manufactures 1% of it. Assert what would make the NEXT stage's "
    "work meaningless if it were wrong: coverage/row counts against the input size, a non-empty "
    "output, the expected number of files/shards, a shape or dtype the consumer requires. Do NOT "
    "assert result QUALITY (a metric being 'good enough') — that is the search's job, not yours; "
    "assert that the work was DONE.\n"
    "  • DECLARE the same condition on the stage in `looplab_stages.json` via its `expect` field "
    "(see the STAGES phase) so the engine holds the stage to it too. The two are not redundant: the "
    "assert is inside code you may not always be allowed to edit (an operator-PROTECTED script is "
    "off-limits to you), while `expect` is in the manifest and always yours to declare. When a "
    "stage's script is protected, `expect` is the ONLY way to state what its success means — "
    "declare it there.\n"
    "LOGGING: keep the training framework's logger (e.g. PyTorch Lightning's TensorBoardLogger) "
    "ENABLED and log SEVERAL metrics (the target metric AND related ones — loss, other recalls, "
    "lr), not just the objective; point its log dir at a STABLE path under the workdir so the "
    "curves persist (viewable via `looplab tensorboard <run_dir>`). Also print readable progress "
    "(epoch/step + current metrics) to stdout — it streams to the live eval log.\n\n")

# The PromptStore default for `repo_developer_system_body`, composed so the no-probe run's prompt is
# byte-identical to what it has always been. `_system_body()` swaps only the middle clause.
def _repo_dev_system_tail_with_commands() -> str:
    """Specialize the scope sentence without minting a second module-global guidance owner."""
    return _REPO_DEV_SYSTEM_BODY_TAIL.replace(
        "SCOPE: your read/write tools reach ONLY this repo. Data/model files OUTSIDE it (a dataset or "
        "checkpoint mount named in the task) are NOT readable by your tools here — don't try, and don't "
        "hunt for them; just reference their given path in the CODE you write, which CAN open them at "
        "runtime. ",
        "SCOPE: your scout/read/write/probe tools reach ONLY this repo. Data/model files OUTSIDE it "
        "(a dataset or checkpoint mount named in the task) are NOT directly readable through those "
        "tools — don't hunt for them; reference their given path in the CODE you write. A separately "
        "operator-pinned validation command receives the task's declared inputs in its disposable "
        "candidate workspace. ")
_REPO_DEV_SYSTEM_BODY = (_REPO_DEV_SYSTEM_BODY_HEAD + _REPO_DEV_NO_EXECUTION
                         + _REPO_DEV_SYSTEM_BODY_TAIL)
_REPO_DEV_SYSTEM_BODY_WITH_PROBE = (_REPO_DEV_SYSTEM_BODY_HEAD + _REPO_DEV_PROBE_EXECUTION
                                    + _REPO_DEV_SYSTEM_BODY_TAIL)
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
# --- the between-steps MEASUREMENT (doc 53 item 10, the LoopLab half) ------------------------
#
# MEASURED, 2026-08-27, over the eleven AlgoTune model probes in `/var/tmp/looplab-bench/
# model-probes/*/runs/*/run/spans.jsonl`. The engine already evaluates EVERY node it builds --
# `node_created` and `node_evaluated` are 1:1 in every probe (12/12 on `sol10`, 11/11 on
# `gpt56luna`; the two shortfalls are runs the spend ceiling cut mid-eval). What the loop does NOT
# do is let the role that WRITES the code see a number while it is still writing. The parent block
# below carries `metric=` and reaches only the single-session fallback: across 1,055 `plan_step`
# generations and 296 `plan` generations in that corpus, the string "PARENT SOLUTION" appears
# ZERO times. So on the DEFAULT path (`developer_plan_decompose`) every writing session is blind.
#
# The model's own answer to being blind is what this costs: of 116 attributed plan steps, 30 (26 %)
# are titled as a measurement and nothing else -- "Run eval_train and verify speedup", "Measure
# once with the real evaluator", "Run the real evaluator on the train split" -- and 21 of those 30
# WROTE NOTHING AT ALL (`noop`). Those 30 steps spent **317 LLM calls** (median 7 per step) and
# **5,762 s**, i.e. 30 % of all plan-step generations and 36 % of all plan-step wall clock, to buy a
# subprocess that takes 40 s (`run_dev_command`, n=76, median 39.6 s, p90 45.8 s). A whole bounded
# session -- system prompt, repo preview, tool loop -- is being spent to press a button.
#
# So the command is run BETWEEN steps, by the engine, and its output is handed to the next step.
# Three properties are deliberate:
#   * it runs OUTSIDE `run_phase`, so it spends no part of `developer_session_time_budget_s` (1200 s)
#     -- the step sessions it sits between are median 58.9 s / p90 296.3 s and are not squeezed;
#   * it does NOT run after the LAST step, which has no consumer: the node goes straight to the
#     engine's own evaluation, which is the number that counts. 72 of the 116 steps are non-final;
#   * it is a PROMPT input and nothing else. `DevCommandTools` runs in a disposable candidate tree
#     it deletes on return, so this cannot write a node file, cannot become `last_files`, and cannot
#     reach `node_evaluated.metric`. The reported speedup and the champion still come from
#     `engine/evaluate.py` alone -- see `tests/test_developer_step_feedback.py`.
#
# OFF unless the operator names the command (`Settings.developer_step_feedback_command`). Not
# timidity and not a guess-avoidance ritual: it CHANGES WHAT THE AGENT IS SHOWN, which is the
# measurement, exactly as `make_task.py --full-context` does (doc 53 item 10), and the arm-B numbers
# already on disk were produced without it. Choosing the command by heuristic was rejected for the
# reason `_grader_packages` gives one paragraph up -- a fence that guesses refuses the real thing.
#
# The output cap. Measured over the 81 `run_dev_command` results in the probe corpus: median 782
# chars, p90 2,541, max 2,614 -- so 6,000 clips nothing that has actually been produced and bounds a
# command whose stderr runs away. `DevCommandTools` already caps the raw streams at 64 KB.
_STEP_FEEDBACK_CAP = 6000
# What the reference agent tells its model before EVERY message and we told ours never: how much of
# the run's money is left. `AlgoTuner/utils/message_writer.py:1442` renders "You have sent N messages
# and have used up $X. You have $Y remaining." and `format_message_with_budget` puts it FIRST, and the
# effect is visible in the arm-A logs -- 112 messages landing on $0.9952 of $1.0000. Ours flew blind:
# 0 of 317 `plan_step` prompts in dsFB3 carried any spend figure, and the ceiling arrives as a node
# CRASH ("LLM spend ceiling reached: $1.0024 of the $1.0000") that throws that node's work away.
# Overshoot measured across finished probes: $1.002 to $1.091.
_REPO_DEV_BUDGET_LINE = (
    "BUDGET: ${spent:.4f} of ${limit:.4f} spent, ${remaining:.4f} left ({pct:.0f} % gone). Every "
    "message you send spends it, and NOTHING you write after it runs out is measured -- the step is "
    "lost, not saved. Spend what is left on the edit most likely to move the number; if little "
    "remains, make this step small and finish it.\n\n")
_REPO_DEV_STEP_FEEDBACK_BLOCK = (
    "\n\n=== MEASUREMENT OF THE WORK SO FAR (run for you, automatically) ===\n"
    "The operator's `{name}` command was run on your working set as it stands after the previous "
    "step. You did not spend a turn on it and you do not need to run it yourself -- it runs again "
    "after every step that changes a file, so do NOT spend a step on measuring. Read the numbers "
    "below and let them decide what this step does.\n{output}\n")
# The measured starting point, for the sessions that actually write code. `implement_from` already
# computes it (`parent experiment #N, metric=M`) and the plan/step prompts dropped it on the floor.
_REPO_DEV_BASELINE_LINE = (
    "\nMEASURED STARTING POINT: {note}. That is the number your edits have to beat; a change that "
    "does not move it is not an improvement.\n")
_REPO_DEV_REPAIR_BLOCK = (
    "\n\nThe PREVIOUS attempt FAILED — fix ONLY the stage that failed (see the error) with "
    "MINIMAL edit_file hunks on the offending file(s) (re-write a file only if it is beyond patching). "
    "The re-run happens in the SAME workdir; when the node has pipeline stages, the ENGINE decides "
    "what to re-run and reuses a completed train stage's artifact where that is safe — do NOT add "
    "'skip if a checkpoint exists' logic to the code yourself (a partial or foreign artifact would "
    "silently freeze the metric); just repair the failing step. Do not start "
    "over from scratch. Files in this node's working set: {already}.\n"
    "IF THE REAL CAUSE IS AN EARLIER STAGE, SAY SO. A stage that exited 0 was counted successful, but "
    "exit 0 only means it did not crash — an earlier stage can 'succeed' having produced a fraction "
    "of what it should have (this has happened: a mining stage exited 0 with hard negatives for 1.2% "
    "of the queries, and training then failed or trained on nonsense). When the error you are looking "
    "at is the SYMPTOM and an earlier stage is the CAUSE, do two things in THIS repair: (1) EDIT that "
    "earlier stage's script so it does the job properly and fails loudly when it cannot, and (2) pass "
    "`rollback_stage: \"<that stage's name>\"` to `done`. The engine then re-runs the pipeline from "
    "that stage, throwing away its bad output and everything built on it. Both parts are required — "
    "naming a stage you did not change is refused, because re-running it unchanged would produce the "
    "same bytes. You get ONE rollback per stage on this node, and it costs the same budget a forced "
    "full re-train does, so name a stage when you have evidence in the log, not on a hunch. If the "
    "earlier stage's script is one you are not allowed to edit, you cannot fix it — say that in your "
    "summary instead of guessing.\n"
    "--- eval error (stderr/stdout tail) ---\n")


# THE FENCE IS INHERITED, NEVER RE-DECLARED — spliced into every Developer prompt by
# `_gpu_devices_note`, with or without a declared footprint. See that method's docstring for the
# `e5small-dr-unified-v11` node 3 measurement this sentence exists for.
_FENCE_INHERITANCE_NOTE = (
    "\n\nYOUR GPU FENCE ARRIVES IN THE ENVIRONMENT AND YOU MUST INHERIT IT, NEVER SET IT. The "
    "engine pins this node's devices by setting `CUDA_VISIBLE_DEVICES` before your stages run, and "
    "a child process that assigns that variable itself REPLACES the fence instead of composing "
    "with it: the ordinals it names are then PHYSICAL, so the child can land on a device another "
    "experiment is already training on. That failure is silent — it looks like an out-of-memory in "
    "YOUR run, caused by a process you did not start — and it destroys the sibling's work as well "
    "as your own. To fan out across the devices you were given, let every child INHERIT the "
    "environment and index logically from 0 (`cuda:0`, `cuda:1`, … up to the count you were "
    "granted); never write `CUDA_VISIBLE_DEVICES` into a subprocess env, and never assume a device "
    "ordinal you did not derive from `torch.cuda.device_count()`."
)


def plan_step_attribution(steps, observed, shipped) -> dict:
    """Reconcile the PLAN against the ARTEFACT and return the record of the difference.

    The plan is a PROPOSAL: `_propose_plan` runs BEFORE a byte is written and its steps are
    advisory — a step session may legitimately do something else (on `runs-B/discrete_log` the plan
    phase MEASURED the card's Pollard-rho hypothesis losing to BSGS and planned the opposite, which
    is the loop working), do nothing at all, or overwrite what an earlier step wrote. That is by
    design and is not what this function is for. What it is for is that the difference used to leave
    NO TRACE: `_run_step` returns "" on success, every step's writes land in one flat `write.files`
    map with no author, and the durable record (`node_created.files` + the card's `idea.rationale`,
    written before the repo was read) therefore presents a proposal as if it described the artefact.
    Measured on the 20-task `runs-B` corpus at 2026-08-26: 63 of 70 builds ran a plan phase, ZERO of
    those plans appear anywhere in `events.jsonl`, and of the 46 builds whose plan actually drove
    execution (113 steps) ALL 46 contained at least one step that wrote nothing (46 steps, only 7
    of them explained by the existing `plan_steps_failed` span) or a file finished by a LATER step
    than the one the plan says produces it (22 rewrites across 18 builds).

    So: RECORD, don't prevent. `observed` is one `{"wrote": [...], "deleted": [...], "error": str}`
    per executed step, in plan order, diffed from the working set before and after that step;
    `shipped` is the final working set. The result names, for every step, what it actually changed
    and whether it superseded an earlier step — and, for every shipped file, the step that last
    wrote it (`authors`) or the fact that no step touched it (`unattributed`: it came from the
    parent/base preload, not from this plan). That is what lets a later reader attribute an eval
    failure to the step that caused it, which is the whole point of decomposing into steps.
    """
    author: dict = {}
    rows: list = []
    noop: list = []
    superseding: list = []
    cut: list[int] = []
    for index, step in enumerate(steps, 1):
        obs = observed[index - 1] if index - 1 < len(observed) else {}
        wrote = list(obs.get("wrote") or [])
        removed = list(obs.get("deleted") or [])
        # "Superseded" is about AUTHORSHIP inside this plan, not about the repo: a path an EARLIER
        # step of THIS plan already wrote and this one has now replaced. A path inherited from the
        # base preload has no step author yet, so the first step to touch it is its author, not a
        # superseder.
        over = sorted({p for p in wrote if p in author})
        for p in wrote:
            author[p] = index
        for p in removed:
            author.pop(p, None)
        row = {"step": index, "title": str(step.get("title") or "")[:160], "wrote": wrote}
        if removed:
            row["deleted"] = removed
        if over:
            row["superseded"] = over
            superseding.append(index)
        if not wrote and not removed:
            # A step that ran to completion and changed nothing. Distinct from an ERRORED step
            # (`plan_steps_failed` already names those): this one reported success, so nothing
            # downstream could tell that the plan's stated work never happened.
            row["noop"] = True
            noop.append(index)
        if obs.get("error"):
            row["error"] = str(obs["error"])[:300]
        # WHICH BOUND ENDED THIS STEP'S SESSION, and until 2026-08-31 nothing durable said.
        # `_note_session_budget` stores the kind on the developer, and the ONLY place it was ever
        # snapshotted into a row is the node-REPAIR path in `engine/evaluate.py`. A plan step is not
        # a repair, so a step cut by turns, wall clock or money left no trace at all: measured over
        # all 22 run trees on this box, the field appears zero times.
        #
        # That became urgent the day a MONEY ceiling was added to these sessions (`_step_cost_ceiling`):
        # a bound whose firing cannot be observed is a bound nobody can trust or tune.
        if obs.get("cutoff"):
            row["cutoff"] = str(obs["cutoff"])[:32]
            # The two numbers that make the cut readable. Omitted when absent rather than written
            # as null: this row is what lands in the durable span, and an always-present
            # "cutoff_spend": null makes a step whose spend was UNKNOWABLE (no accountant) look
            # identical to one that spent nothing.
            if obs.get("cutoff_seconds") is not None:
                row["cutoff_seconds"] = round(float(obs["cutoff_seconds"]), 1)
            if obs.get("cutoff_detail"):
                row["cutoff_spend"] = str(obs["cutoff_detail"])[:120]
            cut.append(index)
        rows.append(row)
    return {"total": len(steps), "steps": rows, "noop_steps": noop,
            "cut_steps": cut,
            "superseding_steps": superseding,
            "authors": {p: author[p] for p in sorted(author)},
            "unattributed": sorted(p for p in (shipped or {}) if p not in author)}


def empty_build_refusal(*, error, base, base_deleted, files, deleted) -> str:
    """The refusal for a BUILD that wrote nothing, or "" when there is something to evaluate.

    Hoisted out of `_run`'s exit rather than left inline, on CLAUDE.md's tier-2 ground: the rule
    decides whether a node exists, no call site could reach it to state it, and a rule nobody can
    state is a rule nobody reviews. Its truth table is `tests/test_empty_build_guard.py`.

    Measured 2026-08-20 on an AlgoTune `discrete_log` run: the implement phase spent 19 generations
    calling `run_probe` 24 times, `read_file` 8 and `grep` 7 -- and `write_file`/`edit_file` ZERO
    times. The session ended on its own wall budget, `_run` returned "" (no error), and the engine
    committed a node whose `node_created.files` is `{}`. Its `solver.py` was the untouched template
    (`raise NotImplementedError`), the evaluation ran honestly, and the run recorded `speedup: 0.0`
    after 195 paid calls and $0.18.

    The wasted evaluation is not the cost. A real 0.0 is EVIDENCE -- an idea that was tried and did
    not work, which the next Researcher turn reads and builds on -- and this one is an empty box
    wearing its clothes. Nothing downstream could tell them apart.

    The cause is a missing forcing function rather than a confused model: probing is cheap and
    commits to nothing while writing commits, so with no bound the safe move is always one more
    probe. `agent_emit_after`/`agent_emit_force` are TURN counts (300/500) and that session ended at
    19, so neither was ever in play. Fixing the INCENTIVE belongs upstream in the prompt and the
    emit contract; this rung keeps the failure visible and cheap in the meantime.

    Scoped to a FRESH build. `implement_from` / `repair_from` pre-load the working set from a base,
    so an unchanged set there is a NO-OP EDIT -- a different fact, already judged one rung over by
    `engine/repair_verify.py`'s `inert` verdict and bounded by INERT_REPAIR_LIMIT. Convicting it
    here too would charge one event under two vocabularies that mean different things: "nothing was
    built" and "nothing was CHANGED".
    """
    if error is not None or base is not None or base_deleted is not None:
        return ""
    # The manifest is not a candidate. `declare_stages` writes `looplab_stages.json` -- the
    # DECLARATION of how to evaluate an experiment -- through a different tool from the
    # `write_file`/`edit_file` that produce the experiment itself, so a working set holding only it
    # is a build that planned an evaluation and never wrote the thing to evaluate.
    #
    # This clause is the 2026-08-21 correction, and the run that forced it is the reason the rule
    # cannot just be "did anything get written". A Gemini-3.7-flash run reached FIVE nodes -- the
    # first arm-B run ever to evaluate anything -- and every one carried exactly one file, a
    # 200-290 byte manifest declaring a single stage `python -c "print('Ready')"` with
    # `expect.assert: "Check solver environment readiness"`. `solver.py` was the untouched
    # `raise NotImplementedError` template in all five. Each evaluated honestly in 12-17 s and
    # recorded 0.0, at $0.63 of a $1.00 budget.
    #
    # So the empty-set check passed a DECOY: a file that satisfies "something was written" while
    # containing no implementation. A rule keyed on the count of files is one filename away from
    # being satisfied by any placeholder, which is why this one is keyed on WHICH file and names
    # the manifest from its own writer (`repo_write_tools.STAGES_MANIFEST`) rather than repeating
    # the literal.
    #
    # RESIDUAL, stated rather than hidden: a fresh build whose genuine intent is "run the repo's
    # existing code under a different pipeline" is refused here too. Nothing on this corpus does
    # that -- a fresh build exists to produce a candidate, and re-declaring a pipeline over pristine
    # code measures the baseline, not an experiment -- but it is the case that would need an
    # exemption if one ever appears, and it should arrive as a declaration rather than as a
    # loosening of this predicate.
    from looplab.adapters.repo_write_tools import STAGES_MANIFEST
    authored = [name for name in (files or {}) if name != STAGES_MANIFEST]
    if not authored and not deleted:
        only_manifest = bool(files)
        # SPELLED AS "STUCK", NOT AS A CRASH, since 2026-08-28. The docstring above already says
        # why: the cause is "a missing forcing function rather than a confused model" and the
        # session ended on its own wall budget with a live provider. `DEVELOPER_ERROR_PREFIX` routes
        # to the provider circuit breaker and PAUSES the run (`core/models.py::DEVELOPER_STUCK_PREFIX`), which ended
        # dsNew2 at 2 evaluated nodes of 3 and qwen38f at its first — 2 of 106 nodes on the corpus,
        # both of them run-ending, on a gateway that answered every call. The node still dies; the
        # run no longer does. `engine/orchestrator.py` gained the matching branch in the same change.
        return (f"{DEVELOPER_STUCK_PREFIX} the implement session ended having written "
                + ("only the stage manifest" if only_manifest else "nothing at all")
                + ", so there is no candidate to evaluate -- "
                + ("declaring how to run an experiment is not writing one; " if only_manifest else "")
                + "the workdir would hold the untouched template.)")
    return ""


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
                 stage_guidance: bool = True,
                 prompts=None, cross_run_read_tools: bool = False, memory_dir=None,
                 probe: bool = False, probe_timeout_s: float = 60.0,
                 probe_confine: bool = True, probe_max_calls: int = 0, command_runtime=None,
                 step_feedback_command: str = ""):
        self.client = client
        self.task = task
        self.parser = parser
        self.prompts = prompts
        # PART V §22: read-only cross-run knowledge, ROLE-SCOPED to the developer (repair/impl lessons).
        self._cross_run_read_tools = bool(cross_run_read_tools)
        self._cross_run_memory_dir = memory_dir
        self._memory_state = None
        # Coerced at the BOUNDARY (doc 25 AG-01) so an unknown option name raises here, in the
        # ctor, rather than surviving as dead weight in a dict until the drive call swallows it.
        from looplab.agents.loop_options import LoopOptions
        self.loop_opts = LoopOptions.coerce(loop_opts)
        # C4 plan decomposition + hard per-session backstop (see Settings.developer_*).
        self._plan_decompose = plan_decompose
        self._plan_min_steps = max(2, int(plan_min_steps))
        self._plan_max_steps = max(1, int(plan_max_steps))
        self._session_max_turns = int(session_max_turns)
        self._session_time_budget_s = float(session_time_budget_s)
        # False drops the stage-pipeline block from the system prompt (`_drop_stage_guidance`).
        # True is the default and keeps the historical text byte for byte.
        self._stage_guidance = bool(stage_guidance)
        # F2 · the PROBE (tools/dev_probe.py). The ctor default is OFF while `Settings.developer_probe`
        # is ON, deliberately: `make_roles` is the operator's knob and passes the setting, and the ~170
        # direct `LLMRepoDeveloper(...)`/`__new__` constructions in the suite are not asking for a live
        # execution surface. A default of True here would silently add a subprocess-launching tool to
        # every one of them.
        self._probe = bool(probe)
        self._probe_timeout_s = float(probe_timeout_s)
        self._probe_confine = bool(probe_confine)
        # 0 = uncapped, the shipped behaviour; see `Settings.developer_probe_max_calls` and §190.
        self._probe_max_calls = max(0, int(probe_max_calls or 0))
        # ONE counter for the whole run, not one per phase -- `_scout_tools` builds a fresh probe
        # provider every phase, and §189's effect is measured per RUN. See `_probe_call_counter`.
        self._probe_calls = {"n": 0}
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
        # F2 · the probe's read fence is derived from the task's OWN repo spec, by the same
        # `read_fence.fence_inputs` that hands the engine its eval fence. Bound from the `rs` already
        # read above, once per node build rather than once per phase.
        self._dev_commands = list(rs.get("developer_commands") or [])
        self._command_runtime = command_runtime
        # The operator-pinned command the plan loop runs BETWEEN steps ("" = the feature is off and
        # the prompts are byte-identical to what they have always been). Stored as a NAME, resolved
        # against `_dev_commands` at use time: a name the task does not pin is silently no feedback,
        # never an invented command -- `DevCommandTools` would refuse it anyway, and turning that
        # refusal into a prompt block would teach the model that the measurement is broken.
        self._step_feedback_command = str(step_feedback_command or "").strip()
        self._probe_repo_spec = rs if (probe or self._dev_commands) else None
        self.last_files: dict[str, str] = {}
        self.last_deleted: list[str] = []
        self.last_footprint: dict | None = None
        # The suspect EARLIER stage this call's repair blamed, "" when it blamed none. Per-CALL like
        # every other `last_*` output and registered in `agents/roles.py::DEVELOPER_OUTPUT_ATTRS` for
        # the same reason they are: the engine reads it with `getattr(developer, ..., "")`, so a
        # rename here would not fail — it would silently mean "no rollback was ever requested", i.e.
        # the feature quietly ceasing to exist with every test still green.
        self.last_rollback_stage: str = ""

    def bind_state(self, state, parent=None) -> None:
        """Bind cross-run developer tools to the full live run identity.

        Binding the provider to ``Task`` looked scoped but omitted ``run_id``/``run_uid``, disabling
        the self-run fence and letting cadence lessons come back as purported prior evidence.
        """
        self._memory_state = state

    # Files most useful to PRELOAD verbatim so the agent authors the entrypoint without fumbling with
    # a (truncating) read tool. Order = priority; the rest of the surface is appended within budget.
    # PROVENANCE / HEURISTIC ONLY: these names (incl. the repo-specific `to_stf.py`/`tokenizing.py`)
    # come from the reference repo LoopLab was first exercised on. They are a soft *ordering* prior,
    # not a requirement — an absent name simply doesn't preload, and the full surface is appended
    # anyway — so the heuristic degrades gracefully on any other repo. Generalize to an
    # `EditableSpec.preload_priority` knob if a task ever needs to override the order.
    _PRELOAD_PRIORITY = ("test.py", "settings.py", "train.py", "to_stf.py", "model.py", "loss.py",
                         "dataset.py", "tokenizing.py", "metrics.py", "inference.py", "README.md")
    # Ceiling on the recursive "files:" listing. Generous for a real project, small enough that a
    # data-heavy or vendored tree cannot crowd out the previews the same prompt block is for.
    _MAX_LISTED_FILES = 400

    def _repo_context(self, per_file: int = 3000, total_budget: int = 30000) -> str:
        """Embed the repo's key source files VERBATIM in the prompt so the agent can author the eval
        entrypoint from them directly — instead of writing throwaway 'cat' scripts to dribble a file
        in through a truncating read tool (the failure mode we hit). Listing first, then prioritized
        full-text files within a char budget."""
        from pathlib import Path as _P
        parts: list[str] = []
        used = 0
        # RECURSIVE, because the system prompt promises "The repository's key source files are
        # PREVIEWED below" and a src/-layout repo has none of them at the top level: the old
        # `root.iterdir()` gave that repo an EMPTY preview and a "files:" line naming no source at
        # all. Bounded so a large tree can't flood the prompt — noise directories are skipped (the
        # same set the repo scout uses) and the walk stops at `_MAX_LISTED_FILES`, with the
        # truncation disclosed rather than silently narrowing what the agent believes exists.
        import os
        from looplab.tools.reposcout import _SKIP_DIRS

        for ed in self._editables:
            root = _P(ed["path"])
            if not root.is_dir():
                continue
            names, truncated = [], False
            try:
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames[:] = sorted(d for d in dirnames
                                         if d not in _SKIP_DIRS and not d.startswith("."))
                    base = _P(dirpath)
                    for fn in sorted(filenames):
                        if len(names) >= self._MAX_LISTED_FILES:
                            truncated = True
                            break
                        names.append((base / fn).relative_to(root).as_posix())
                    if truncated:
                        break
            except OSError:
                names = []
            listing = ", ".join(names) + (
                f" … (+more, listing capped at {self._MAX_LISTED_FILES})" if truncated else "")
            parts.append(f"# Repository `{ed['name']}` at {root} — files:\n" + listing)
            # Priority is matched by BASENAME (train.py counts wherever it lives) but still spends the
            # budget in _PRELOAD_PRIORITY order, exactly as the top-level-only version did; the path
            # breaks ties so two same-named files in different packages stay deterministic. Everything
            # else keeps the walk order, which is itself sorted, so the preview is stable across runs.
            _rank = {name: i for i, name in enumerate(self._PRELOAD_PRIORITY)}
            _base = lambda n: n.rsplit("/", 1)[-1]                       # noqa: E731 — local key fn
            ordered = sorted((n for n in names if _base(n) in _rank),
                             key=lambda n: (_rank[_base(n)], n)) + \
                      [n for n in names if n.endswith((".py", ".yaml", ".yml", ".json"))
                       and _base(n) not in _rank]
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

    def _repair_emit_spec(self) -> dict:
        """The repair session's `done`, which carries ONE extra field the build sessions must not
        have: `rollback_stage`.

        A SEPARATE spec rather than an optional property on `_emit_spec` because the field is only
        answerable in a repair — a fresh build has no failed stage to blame — and a schema property
        that is inert wherever it appears is a property a model will eventually fill in anyway. The
        build path keeps `_emit_spec` byte-identical, so nothing about a fresh implement changes.
        """
        from looplab.tools._base import fn_spec
        return fn_spec("done",
                        "Call once the repair is written and the eval would run. Briefly summarize "
                        "what you changed.",
                        {"summary": {"type": "string"},
                         # The Developer's ONLY way to say "the stage that broke is not the stage
                         # that is wrong". Everything the engine does with it is in
                         # `engine/eval_stages.py::_rollback_start`; the two things the model has to
                         # know are stated here because the tool description is what it reads at the
                         # moment of answering: it must have CHANGED that stage, and it gets one
                         # attempt per stage.
                         "rollback_stage": {"type": "string", "description":
                                            "Leave EMPTY unless the failure you just repaired was "
                                            "CAUSED by an EARLIER pipeline stage that already "
                                            "'succeeded' — e.g. a data/mining stage that exited 0 "
                                            "having produced a fraction of what it should have, so "
                                            "training was fed a broken input. Then name that earlier "
                                            "stage and the engine will re-run the pipeline FROM it, "
                                            "discarding its output and everything after. You must "
                                            "have EDITED that stage's script (or something it "
                                            "imports) in THIS repair, or the rollback is refused — "
                                            "re-running it unchanged would produce the same bytes. "
                                            "You get ONE rollback per stage per node, and it costs "
                                            "the same budget a forced full re-train does, so use it "
                                            "when you have evidence, not on a hunch."}}, [])

    # THE STAGE GUIDANCE IS CUT OUT BY TEXT, NOT BY RESTRUCTURING THE LITERAL.
    #
    # 5,001 source characters about GPU training, checkpoints, shards and `train.py`, addressed to a
    # role that on a single-stage task has nothing to declare. MEASURED 2026-08-28 over six probes:
    # `declare_stages` was called ZERO times while the rendered block sat in every
    # `plan`/`plan_step`/`card_build` system prompt, costing 4.8-6.0 % of a $1 run ($0.057 of $1.013
    # on dsFix1, then $0.049, $0.060, $0.080) -- about a sixth of a node at the measured $0.35/node.
    #
    # Cut by slicing between two sentinels rather than by hoisting the text into its own constant:
    # three attempts at the hoist broke the adjacent-string literal, and a surgical slice cannot.
    # If either sentinel ever stops matching the body returns UNCHANGED, which is the safe direction
    # -- an operator gets the historical prompt, not a mangled one.
    #
    # DEFAULT ON. `_system_body`'s contract is that `developer_probe=False` reproduces the historical
    # prompt BYTE FOR BYTE via `LEGACY_CONFIG_SNAPSHOT_DEFAULTS`, so a resumed pre-2026-08-13 run
    # keeps the prompt its first half ran under. Only an explicit setting turns it off.
    _STAGE_GUIDANCE_OPEN = "TRAIN-THEN-SCORE PIPELINE"
    _STAGE_GUIDANCE_CLOSE = "For a ROUTINE hyperparameter experiment"

    def _drop_stage_guidance(self, body: str) -> str:
        """`body` without the stage-pipeline advice, or `body` unchanged when it cannot be located."""
        if getattr(self, "_stage_guidance", True):
            return body
        start = body.find(self._STAGE_GUIDANCE_OPEN)
        end = body.find(self._STAGE_GUIDANCE_CLOSE, start + 1) if start >= 0 else -1
        if start < 0 or end < 0:
            return body
        return body[:start] + body[end:]

    def _system_body(self, render) -> str:
        """The system body, with the one clause that depends on whether the PROBE is wired.

        SAME PromptStore key either way (`repo_developer_system_body`), different DEFAULT — so an
        operator's `prompt_dir` override still replaces the whole body exactly as it always did, and
        a run with `developer_probe=False` gets a byte-identical prompt to the one this feature
        found. The clause is spliced at its original position rather than appended, because the text
        it replaces asserts the opposite ("you CANNOT execute anything yourself") and two paragraphs
        contradicting each other is worse than either one alone: that assertion is what the observed
        failure quoted back at itself before writing a fake loguru."""
        if getattr(self, "_dev_commands", None):
            clause = (_REPO_DEV_PINNED_AND_PROBE_EXECUTION if getattr(self, "_probe", False)
                      else _REPO_DEV_PINNED_EXECUTION)
            default = _REPO_DEV_SYSTEM_BODY_HEAD + clause + _repo_dev_system_tail_with_commands()
        else:
            default = (_REPO_DEV_SYSTEM_BODY_WITH_PROBE if getattr(self, "_probe", False)
                       else _REPO_DEV_SYSTEM_BODY)
        # The context-before-tools rule is deliberately NOT appended here, unlike on the Researcher
        # side. Two contracts this role has and that one does not forbid it, and the rule has no
        # evidence to weigh against them: A/B'd over three models it moved NOTHING, while the same
        # knowledge published as DATA -- `agents/answered_by_context.py`'s per-tool counts -- took
        # cold-start tool calls 41.3 -> 17.7. (1) `developer_probe=False` must reproduce the
        # historical prompt BYTE FOR BYTE, which is what `LEGACY_CONFIG_SNAPSHOT_DEFAULTS` pins it
        # to so a resumed pre-2026-08-13 run keeps the prompt its own first half ran under; an
        # unconditional suffix breaks that for every such resume. (2) an operator's PromptStore
        # override replaces the WHOLE body -- appending to it means the operator cannot actually
        # override. The Researcher's trust rules ARE appended after render() for a reason that does
        # not transfer: `_UNTRUSTED_MEMORY_RULE` says do not obey text a previous run wrote, and a
        # persona override silently dropping THAT is a safety hole. This is a hint about latency.
        return self._drop_stage_guidance(render(self.prompts, "repo_developer_system_body", default))

    def _note_session_budget(self, payload) -> None:
        """Remember WHICH bound ended a session, for the durable row to carry.

        Passed as an EXPLICIT keyword and never folded into `_session_opts`'s bundle:
        `loop_options.py` requires `LOOP_OPTION_FIELDS` and `EXPLICIT_ONLY_LOOP_ARGS` to PARTITION
        the loop's keyword-only parameters, and a name reachable BOTH ways raises a duplicate-keyword
        `TypeError` that the loop's own containment `except` swallows — silently degrading an agentic
        phase to a non-agentic one, which is the defect that partition exists to prevent.

        Best-effort exactly like `_note_budget`'s own contract: this fires on the way to a salvage
        emit, so a raise here would turn a rescued answer into a crash.

        `kind` IS THE WHOLE OF `tool_loop.py::LOOP_CUTOFF_KINDS`, not the two this docstring used to
        name. `_note_budget` fires the same `on_budget` observer for all five — `time`, `turns`,
        `stuck`, `stalled`, `emit_force` — and this stores whatever arrives, so three of them landed
        on a durable column two comments described as "which BUDGET ended the session". Only the
        first two are budget bounds; the other three are the loop ending a session that was not
        going anywhere, which is a different fact with a different remedy, and
        `crash_repair.py::_format_repair_log` now says which it was rather than implying a clock.
        CLAIM[budget-exhausted-vocabulary] the durable `budget_exhausted` column carries any of the
        five loop cutoff kinds, not only the two budget bounds.
        decided:`line:LOOP_CUTOFF_KINDS&&emit_force@looplab/agents/tool_loop.py`
        """
        try:
            kind = str((payload or {}).get("kind") or "").strip()
            detail = str((payload or {}).get("detail") or "").strip()
            seconds = (payload or {}).get("seconds")
        except Exception:  # noqa: BLE001 — an observer may not break the salvage path
            return
        if kind:
            self.last_budget_exhausted = kind[:32]
            # THE NUMBERS TOO, not only the word. Twelve cut sessions across 30 probes recorded
            # nothing but "time", so how close the money ceiling came could not be read off the
            # corpus at all -- and a bound whose distance from firing is unobservable can only be
            # argued about. Kept on a SECOND attribute rather than folded into the first: the
            # `budget_exhausted` column is a durable vocabulary other code branches on
            # (see the `budget-exhausted-vocabulary` claim above), and widening it to carry prose would
            # break every reader that compares it to a kind.
            self.last_budget_facts = {"kind": kind[:32], "seconds": seconds, "detail": detail[:200]}

    def _session_opts(self, *, max_turns=None, time_budget=None, cost_budget=None):
        """loop_opts + the HARD per-session ceiling. A developer session ALWAYS gets a finite bound so
        a model that keeps writing/exploring without ever emitting `done` fails cleanly with the code
        it has written, instead of the 10k-call / multi-hour runaway a big task produced.

        `.replace()`, not `.with_defaults()`: the ceiling is the point — it must WIN over whatever
        the configured bundle carries, which is exactly what the old `opts[...] = ...` assignment did.
        """
        from looplab.agents.loop_options import LoopOptions
        return LoopOptions.coerce(getattr(self, "loop_opts", None)).replace(
            max_turns=int(max_turns if max_turns is not None
                          else getattr(self, "_session_max_turns", 500)),
            time_budget_s=float(time_budget if time_budget is not None
                                else getattr(self, "_session_time_budget_s", 1200.0)),
            # 0.0 = off, and that is the default: only the plan-step caller passes one, because it
            # is the only session measured eating a run. See `_step_cost_ceiling`.
            cost_budget_usd=float(cost_budget or 0.0))

    def _step_cost_ceiling(self) -> float:
        """The most ONE plan step may spend, or 0.0 (= off) when there is no budget to divide.

        The other two ceilings on a step session are turns (500) and wall clock (1200 s), and what
        actually ends a run is money. Measured 2026-08-31 across 7 AlgoTune probes, the most
        expensive single step as a share of what remained when it started:

            remPde   66 %   remPde2  49 %   accPde  32 %   remDL2  72 %
            remEE    18 %   remEE2    8 %   accEE    7 %

        remPde's step ran 72 generations and was cut by the 1200 s wall at 1212 s -- the wall did
        its job, and 48 % of the dollar was already gone. On edge_expansion the same wall never bit
        (worst step 8-9 %), so seconds do not stand in for dollars across tasks.

        HALF OF WHAT REMAINS, but never less than a FIFTH OF THE WHOLE RUN. The second clause is
        what keeps this from punishing a legitimate late step: remDL2's 72 % was its LAST step
        spending $0.1679 of a $0.2322 remainder, which is a run finishing properly, and half-of-
        remaining alone would have cut it. Against the seven measured steps this bites exactly one --
        remPde's runaway -- and leaves every other one untouched, which is the whole of what it is
        for. Seven runs is a thin basis for a constant and that is why it is a ratio of two numbers
        the run already knows rather than a dollar figure typed in.

        A cut step is not a lost step: the loop salvages an emit from whatever the session wrote.
        """
        acct = getattr(getattr(self, "client", None), "accountant", None)
        if acct is None:
            return 0.0
        try:
            limit = float(getattr(acct, "limit", None) or 0.0)
            spent = float(getattr(acct, "spent", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        if limit <= 0 or not _math.isfinite(limit) or not _math.isfinite(spent) or spent < 0:
            return 0.0
        return max(0.5 * max(0.0, limit - spent), 0.2 * limit)

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

    def _propose_plan(self, system: str, idea: Idea, write=None, baseline_note: str = "") -> list:
        """Plan phase: a READ-ONLY stage — the developer inspects the real code/experiments (it CANNOT
        write here), and its only exit is `propose_plan` (the ordered atomic plan). Returns a list of
        {title, detail}; [] on empty/failure so the caller falls back to one session."""
        from looplab.agents.agent import run_phase, CompositeTools
        from looplab.tools.env_inspect import EnvInspectTools
        # §153 measured what the unwired version cost: `write_file` was called 51 times from this
        # phase and ALL 51 errored, while 504 of 528 `plan` chain-roots carried a system prompt
        # naming it. Held back while §115's arm ran; that arm closed at 24 probes in §180.
        system = read_only_intro(system)
        params = ", ".join(f"{k}={v}" for k, v in (idea.params or {}).items()) or "(choose sensible values)"
        # THE PHASE THAT DECIDES HOW MANY STEPS TO BUY COULD NOT SEE THE PRICE.
        #
        # `_run_step` has carried `_budget_note()` since it was written, so every INDIVIDUAL step is
        # told what is left -- 72.8 % of `plan_step` generations in the corpus carry a money figure.
        # The phase that chooses how many of those steps to write carried none: `plan` is 0 of 2,236.
        # That is the wrong way round. A step told "little remains, make this step small" can only
        # shrink the step it is already in; the plan is where the COUNT is decided, and the count is
        # what the money actually buys.
        #
        # Measured over the 8 probes on this box (3,071 generations, $11.7552): `plan` is 16.2 % of
        # spend, second only to `plan_step` (34.8 %) and `propose` (19.2 %). And the failure it
        # feeds is on record -- `remPde` spent 74 % of its dollar before a single node existed, on
        # 103 `plan_step` generations against 34 proposals, then produced one plain-Python node
        # where every other probe on that task carried a numba kernel. A planner that knew it had
        # 26 cents left would not have planned that.
        #
        # The same note, deliberately, not a second wording: two roles told one budget in two
        # formats is a defect this file already names one layer down.
        plan_user = (
            f"{self._budget_note()}"
            f"Experiment concept (the researcher's idea): {idea.rationale}\nHyperparameters: {params}.\n"
            "This is the PLANNING stage. You can READ and inspect the repo (read_file — it paginates, so "
            "read a file ONCE, don't re-read; grep, find_files, list_dir, pkg_info, py_api, gpu_info) but "
            "you CANNOT write code yet. Actually READ the relevant source (the eval/entry script, the "
            "files you'll change) and any prior experiment you're building on — enough to know EXACTLY "
            "what to change — THEN call propose_plan with an ordered list of ATOMIC, independently-"
            "testable steps, each naming concretely what to change and why. Do NOT guess from the "
            "truncated preview; the implement stage (and update_plan) come next.")
        # 26 % of the 116 attributed plan steps in the probe corpus are a measurement and nothing
        # else, and 21 of those wrote no file at all -- 317 LLM calls and 5,762 s spent pressing a
        # button. When the engine presses it between steps, say so HERE, where the steps are chosen:
        # a planner that does not know the measurement is free will keep buying it with a session.
        auto_measured = self._step_feedback_command_name()
        if auto_measured:
            plan_user += (
                "\n\nDo NOT plan a step whose only job is to measure or verify: after EVERY step "
                f"that changes a file the engine runs the operator's `{auto_measured}` command for "
                "you and hands the result to the next step. Every step you plan should CHANGE "
                "something; the numbers arrive on their own.")
        if baseline_note:
            plan_user += _REPO_DEV_BASELINE_LINE.format(note=baseline_note)
        # READ-ONLY toolset: repo scouts + env inspection, but NO write tools — the plan stage's only
        # output is the plan. (This used to be tools=None to force convergence, which made the planner
        # work BLIND off the truncated preview; the read_file pagination fix + emit_after/emit_force
        # convergence backstop now let it read PROPERLY without exploring forever.)
        #
        # Composed BEFORE the messages, because the user turn names what this toolset already holds
        # (`agents/answered_by_context.py`). Composition only reads each provider's `specs()`, so the
        # reorder costs nothing and changes no dispatch.
        # NO `answered_by_context` HERE, deliberately. It was spliced in and measured INERT: the
        # block is built from providers' optional `inventory()` hook, and none of this toolset's
        # providers (`EnvInspectTools`, `RepoScoutTools`, `DevCommandTools`, `DevProbeTools`)
        # implements it -- so it rendered "" at every call while its comment claimed the user turn
        # "names what this toolset already holds". A count-publishing block cannot express "how much
        # is under this repo path" anyway; giving the scouts a real inventory is the fix, and until
        # one exists the honest state is no block rather than an empty string and a false comment.
        read_only = CompositeTools([EnvInspectTools(self._grader_packages())] + self._scout_tools(write))
        messages = [{"role": "system", "content": system}, {"role": "user", "content": plan_user}]
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

    def _step_feedback(self, write, *, index: int = 0) -> str:
        """Run the operator-pinned feedback command on the working set and return its rendered output.

        Returns "" for every reason a caller might want a reason for -- no command named, the name is
        not one the task pinned, no command runtime, the runner raised -- because this is an EXTRA
        rung and a build must never fail over it. The step it feeds simply gets no measurement block,
        which is the pre-2026-08-27 behaviour.

        It goes through `DevCommandTools` rather than a private `subprocess` call so the argv, the
        trust tier, the disposable candidate, the secret screen and the receipts are the SAME ones
        `run_dev_command` gets. A second spelling of "run the operator's command" is the shape doc 25
        SE-08 names: the tool would be hardened and this path would not.
        """
        name = self._step_feedback_command_name()
        if not name:
            return ""
        from looplab.core import tracing
        from looplab.tools.dev_commands import DevCommandTools
        try:
            tools = DevCommandTools(getattr(self, "_probe_repo_spec", None),
                                    runtime=getattr(self, "_command_runtime", None), staged=write)
            with tracing.operation("step_feedback", index=int(index), command=name):
                result = tools.execute_result("run_dev_command", {"name": name})
        except Exception:  # noqa: BLE001 — an extra rung never breaks the build it is helping
            return ""
        text = str(getattr(result, "content", "") or "")
        # OPEN[step-feedback-keeps-the-head-of-the-output] when the cap binds it keeps the START of
        # a command's output and drops the END — the half this module everywhere else treats as the
        # one a reader must not lose.
        # proof:`present:text[:_STEP_FEEDBACK_CAP]@looplab/adapters/repo_developer.py`
        # REVIEW 2026-08-30 (consistency): the measured corpus (median 782, max 2,614 chars) makes
        # the 6,000 cap inert today; the day a runaway command hits it, the failure text at the
        # tail is what vanishes. `_clip(keep="tail")` / `stream_tails` are the house rule and one
        # import away.
        return text[:_STEP_FEEDBACK_CAP]

    def _budget_note(self) -> str:
        """The run's remaining LLM spend, worded for the session that is spending it, or "".

        Returns "" for every reason a caller might want one -- no client, no accountant, no limit,
        a non-finite or unparseable figure -- because this is an EXTRA rung and a build must never
        fail over it. A run with no `llm_budget_usd` gets a byte-identical prompt to before.
        """
        acct = getattr(getattr(self, "client", None), "accountant", None)
        if acct is None:
            return ""
        try:
            limit = float(getattr(acct, "limit", None) or 0.0)
            spent = float(getattr(acct, "spent", 0.0) or 0.0)
        except (TypeError, ValueError):
            return ""
        if limit <= 0 or not _math.isfinite(limit) or not _math.isfinite(spent) or spent < 0:
            return ""
        return _REPO_DEV_BUDGET_LINE.format(
            spent=spent, limit=limit, remaining=max(0.0, limit - spent),
            pct=min(100.0, 100.0 * spent / limit))

    def _step_feedback_command_name(self) -> str:
        """The pinned command this developer may auto-run between steps, or "" when there is none.

        Resolution is against the TASK's own `developer_commands`, not against the setting alone, so
        an operator who names `eval_train` on a run whose tasks do not pin it gets silence rather
        than a refusal block in every step prompt."""
        name = str(getattr(self, "_step_feedback_command", "") or "").strip()
        if not name:
            return ""
        for raw in (getattr(self, "_dev_commands", None) or ()):
            row = raw.model_dump() if hasattr(raw, "model_dump") else dict(raw)
            if str(row.get("name") or "") == name:
                return name
        return ""

    def _run_step(self, idea: Idea, step: dict, idx: int, total: int, write, system: str,
                  stage_note: str = "", baseline_note: str = "", feedback: str = "") -> str:
        """Execute ONE atomic plan step in a FRESH bounded session, on top of the files accumulated so
        far (carried in `write.files`; syntax is validated per write by the write tool). A step's own
        error never aborts the plan — later steps + the eval still run on whatever got written.
        `stage_note` restates the node's ACTUAL declared pipeline (or its absence) so a step session
        never assumes a train stage the stages phase didn't produce."""
        from looplab.agents.agent import run_phase, CompositeTools
        from looplab.tools.env_inspect import EnvInspectTools
        done_so_far = ", ".join(write.files) or "(none yet)"
        step_user = (
            f"{self._budget_note()}"
            f"You are implementing a multi-step plan — STEP {idx} of {total}.\n"
            f"Overall experiment: {idea.rationale}\n{stage_note}\n"
            f"THIS STEP — {step['title']}:\n{step.get('detail') or step['title']}\n\n"
            f"Files CURRENTLY in the workspace (the parent solution + whatever earlier steps wrote — read "
            f"any of them with read_file to see their real content, do NOT assume): {done_so_far}\n"
            "Make ONLY the edits THIS step needs with write_file/edit_file — PATCH existing files, don't "
            "regenerate untouched ones — then call done. Do the minimum for this step; later steps handle "
            "the rest. If this is the last step, make sure the eval entrypoint runs end-to-end.")
        # The two measured facts this session used to be denied: what the parent SCORED (computed by
        # `implement_from`, and reaching only the single-session fallback until 2026-08-27) and what
        # the LAST step's edit did to that score. Appended, not spliced, so a run with neither is
        # byte-identical to the old prompt.
        if baseline_note:
            step_user += _REPO_DEV_BASELINE_LINE.format(note=baseline_note)
        if feedback:
            step_user += _REPO_DEV_STEP_FEEDBACK_BLOCK.format(
                name=self._step_feedback_command_name() or "the operator's evaluation",
                output=feedback)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": step_user}]
        try:
            # implement steps CONSUME the stages/plan briefs, but don't
            # contribute (their writes add length faster than signal, and the last step is terminal) —
            # so the ledger stays the 3 exploration briefs (propose/stages/plan), never K-step bloat.
            run_phase(self.client, CompositeTools([write, EnvInspectTools(self._grader_packages())] + self._scout_tools(write)),
                      messages, self._emit_spec(), label=f"Developer·implement step {idx}/{total}",
                      handoff=False, finalize=lambda a: (a or {}).get("summary", ""),
                      fallback=lambda m: "", on_budget=self._note_session_budget,
                      **self._session_opts(cost_budget=self._step_cost_ceiling()))
        except Exception as e:  # noqa: BLE001
            return f"(step {idx} error: {e})"
        return ""

    def _probe_call_counter(self) -> dict:
        """The run-scoped probe tally handed to every `DevProbeTools` this developer builds.

        Lazy because ~170 tests construct this class through `__new__` without running `__init__`;
        a missing attribute there would turn a cap into an AttributeError at the first probe."""
        counter = getattr(self, "_probe_calls", None)
        if not isinstance(counter, dict):
            counter = {"n": 0}
            self._probe_calls = counter
        return counter

    def _scout_tools(self, write=None):
        """Read-only repo scouts (read_file / grep / find_files / list_dir) so the Developer can READ
        the code it is EDITING and VERIFY an exact CLI flag / function signature / config key in the
        ACTUAL source instead of GUESSING it — guessing an arg the embedded (truncated) source didn't
        show is a top cause of a training crash. Reuses the SHARED RepoScoutTools (path-safe +
        secret-filtered), bound to the editable repo roots with repo-relative paths (the SAME paths as
        write_file/edit_file). `write.files` is passed as the STAGED overlay so read/grep see the code
        the Developer is currently writing — not the pristine on-disk repo (reading a parent/merge
        source is a separate, secondary concern).

        F2 — this is also where the PROBE joins, and it joins HERE rather than at the four phase call
        sites for the same reason the scouts do: every phase asks the same kind of question. A stages
        or plan phase that cannot check whether a library imports declares a pipeline around a
        library that isn't there, which is the same defect one phase later. The probe carries no
        write capability of its own, so adding it to the two READ-ONLY phases does not make them
        writing phases — that is a property of the boundary, not of the toolset it is composed into
        (`tools/dev_probe.py`)."""
        extra = []
        if getattr(self, "_dev_commands", None):
            from looplab.tools.dev_commands import DevCommandTools
            extra.append(DevCommandTools(getattr(self, "_probe_repo_spec", None),
                                         runtime=getattr(self, "_command_runtime", None),
                                         staged=write))
        if getattr(self, "_probe", False):
            from looplab.tools.dev_probe import DevProbeTools
            # `write` is the live RepoWriteTools: the probe replicates its staged `files` into its own
            # disposable cwd, so a probe can import/parse what this node has written so far. One-way —
            # the probe cannot write, so nothing it does can flow back into the build.
            extra.append(DevProbeTools(getattr(self, "_probe_repo_spec", None),
                                       timeout_s=getattr(self, "_probe_timeout_s", 60.0),
                                       confine_reads=getattr(self, "_probe_confine", True),
                                       max_calls=getattr(self, "_probe_max_calls", 0),
                                       counter=self._probe_call_counter(),
                                       # THE SAME declaration `EnvInspectTools` is built with, as
                                       # directories: a grader fenced in one provider of this
                                       # toolset and readable in the next was the route that
                                       # opened under pressure (2026-08-30 review).
                                       protect_roots=self._grader_roots(),
                                       staged=write))
        # PART V §22 — the Developer's read-only cross-run knowledge (dev-routed lessons: what code
        # change fixed a crash across runs). Advisory only; role-scoped so it doesn't see the R&D claims.
        if getattr(self, "_cross_run_read_tools", False) and getattr(self, "_cross_run_memory_dir", None):
            from looplab.tools.cross_run_tools import CrossRunTools
            tool = CrossRunTools(self._cross_run_memory_dir, role="developer", audience="run")
            state = getattr(self, "_memory_state", None)
            # …AND the lessons ledger itself, role-scoped. Until 2026-08-23 the Developer could read
            # what the prior renderer PUSHED at it and nothing more: `search_lessons` lives in
            # `MemoryTools`, `MemoryTools` is composed in `agents/factory._shared_providers`, and the
            # Developer assembles its own toolset here. Measured on `runs/e5small-dr-unified-v4`:
            # across 10,455 tool calls `search_lessons` fired 10 times — 9 in `propose`, 1 in
            # `deep_research`, ZERO in `card_build`/`plan`/`stages`/`inline_repair`. So the role that
            # writes the code could not look up the lesson its current failure matches, and node 8
            # repeated the stage failure node 6 had already diagnosed and fixed.
            # `role="developer"` keeps meta-notes out — the same line the prior renderer draws.
            from looplab.tools.memory_tools import MemoryTools
            lessons_tool = MemoryTools(self._cross_run_memory_dir, role="developer")
            if state is not None:
                lessons_tool.bind_state(state)
            extra.append(lessons_tool)
            if state is not None:
                # Agent-facing providers are task-bound before use. Unbound reads remain an explicit
                # human/CLI portfolio capability, never an accidental agent default — `audience="run"`
                # is what enforces that: with no task to bind, this provider answers nothing rather
                # than falling back to the whole portfolio.
                tool.bind_state(state)
            extra.append(tool)
        # THE QUESTION BOARD, for the role that writes the code an experiment runs. Measured
        # 2026-08-26: this scout set had no reader for it at all — not `RunTools` (Researcher-only),
        # and `read_run_experiment` here is a FOREIGN-run reader. So the Developer could not see the
        # question its experiment answers, and the repair path could not see whether a sibling under
        # the same question had already hit the same wall. A narrow provider rather than granting
        # `RunTools` wholesale, which would also hand over `list_experiments`, `read_code` and the
        # rest — a much larger change in what this role may do.
        #
        # ABOVE the `if not roots: return extra` below, and that placement is the point: a
        # developer with no editable roots still repairs, still reasons about what to write, and
        # still needs to know which question its work answers. Attaching the board after that
        # early return coupled 'may I read the questions' to 'do I own source to edit', which are
        # unrelated — and a behavioural test caught it where a source pin would not have.
        from looplab.tools.question_board import QuestionBoardTools
        board = QuestionBoardTools()
        # `_memory_state`, NOT `_state` — the attribute this class actually holds, and the same one
        # the lessons and cross-run tools are bound from twenty lines up. Binding a name that does
        # not exist would leave the provider answering "no run state bound" on every call, i.e.
        # shipped INERT, which is the failure this tree has paid for more than once.
        state = getattr(self, "_memory_state", None)
        if state is not None:
            board.bind_state(state)
        extra.append(board)
        roots = [e["path"] for e in (getattr(self, "_editables", None) or []) if e.get("path")]
        if not roots:
            return extra
        from looplab.tools.reposcout import RepoScoutTools
        overlay = write.files if write is not None else None      # live dict the write tools mutate
        deleted = write.deleted if write is not None else None    # staged deletions hidden from read/grep/list
        # (name, path) per editable — MIRRORS RepoWriteTools._roots so a scout hit is rendered/deduped with
        # the SAME `<name>/rel` key the write tools use in a multi-editable repo (round-trips into an edit).
        named = [(e.get("name") or "", e["path"]) for e in (getattr(self, "_editables", None) or []) if e.get("path")]
        return extra + [RepoScoutTools(roots=roots, default_root=roots[0], overlay=overlay, deleted=deleted,
                                       named_roots=named)]

    def _stages_emit_spec(self) -> dict:
        from looplab.tools._base import fn_spec
        return fn_spec("declare_stages",
                        "Declare the ORDERED pipeline stages for this experiment and finish the stages "
                        "phase. Each stage is {name, command:[argv...], timeout?, check?, needs?, "
                        "expect?, role?}; they "
                        "run IN ORDER in the same workdir so artifacts (a trained checkpoint, prepared "
                        "data) persist to later stages. Put `%params%` in a command to inject THIS node's "
                        "hyperparameters as `--key value`, or bake the values into the argv yourself. "
                        "Give a long training stage a GENEROUS timeout (seconds). Give every stage BOTH "
                        "its `needs` (the files it READS) and its `expect` (what it produces and what "
                        "its success MEANS) — exit code 0 alone does not tell the engine a stage did its "
                        "job, and a stage that starts without its input can only fail expensively.",
                        {"stages": {"type": "array", "items": {"type": "object", "properties": {
                            "name": {"type": "string"},
                            "command": {"type": "array", "items": {"type": "string"}},
                            "timeout": {"type": "number"}, "check": {"type": "boolean"},
                            # WHICH stage is the training loop. Described in the schema for the same
                            # reason `needs`/`expect` are — this is where a model reliably reads a
                            # field's shape — and phrased as what it BUYS the declarer, because the
                            # only thing it can buy is being stopped.
                            "role": {"type": "string", "enum": ["training"], "description":
                                     "Set to 'training' on the ONE stage that runs the training loop "
                                     "(omit it everywhere else, and omit it entirely if no stage "
                                     "does). It lets the live watchdog END that stage early when it "
                                     "is provably broken — a frozen loss, a collapsed gradient, a "
                                     "diverged run — instead of burning the whole timeout on a model "
                                     "that has stopped learning. Without it the watchdog still reads "
                                     "and reports, but cannot stop anything. Declare it only where "
                                     "it is true: it is read as YOUR statement about which stage's "
                                     "loss curve means something."},
                            # The INPUT half of the contract, described here for the same reason
                            # `expect` is: the schema is where a model reliably reads a field's shape.
                            "needs": {"type": "array", "items": {"type": "string"}, "description":
                                      "Workdir-relative paths this stage READS and cannot run without "
                                      "(e.g. ['data/train.parquet','ckpt/model.pt']). The engine "
                                      "checks them BEFORE the stage starts and refuses to run it if "
                                      "one is missing or empty — naming the earlier stage that "
                                      "declared it as an output, if any. Name what an EARLIER stage "
                                      "produces and what the workdir must already contain; do NOT "
                                      "name a path outside the workdir."},
                            # The per-stage SUCCESS CONTRACT. The schema is the one place a model
                            # reliably reads the field's shape, so both halves are described HERE as
                            # well as in the phase prompt — and `assert` says what it is NOT, because
                            # the near-miss ("the metric should beat 0.85") is a quality judgement the
                            # search owns and the checker will refuse to enforce.
                            "expect": {"type": "object", "description":
                                       "What this stage must have produced for the pipeline to "
                                       "continue. The engine enforces it after the stage exits 0.",
                                       "properties": {
                                           "files": {"type": "array", "items": {"type": "string"},
                                                     "description":
                                                     "Workdir-relative paths this stage WRITES (e.g. "
                                                     "['hard_negs.pkl','ckpt/model.pt']). Each must "
                                                     "exist, be non-empty, and be written by THIS run "
                                                     "of the stage — a leftover from a previous "
                                                     "attempt fails the stage."},
                                           "assert": {"type": "string", "description":
                                                      "ONE line stating what this stage's success "
                                                      "MEANS, checkable against what the stage prints "
                                                      "— e.g. 'negatives.parquet written with at "
                                                      "least n_negatives negatives on every row' or "
                                                      "'all 30 epochs completed and a checkpoint "
                                                      "saved'. State the WORK, never the result "
                                                      "quality: 'the metric beats 0.85' is not a "
                                                      "stage condition (the search ranks results, "
                                                      "this does not). Assert what the stage "
                                                      "CONTROLS; a numeric bar you have not measured "
                                                      "on this data is a guess that fails correct "
                                                      "artifacts — print such a quantity instead."}}}},
                            "required": ["name", "command"]}}},
                        ["stages"])

    def _grader_packages(self) -> tuple:
        """Installed packages the env inspector must refuse -- `EvalSpec.protect_packages`.

        Read through `_cmd_context`'s own source (the task's `eval_spec`) rather than stashed at
        construction, so a task whose spec is resolved late still fences. Total and quiet: a task
        with no eval_spec, or an adapter that raises, fences NOTHING, which is the historical
        behaviour and the right default -- a fence that guesses would refuse a real dependency.
        """
        try:
            ev = self.task.eval_spec() or {}
        except Exception:  # noqa: BLE001 - same contract as _cmd_context: no spec => no fence
            return ()
        names = ev.get("protect_packages") or ()
        return tuple(str(n) for n in names if str(n).strip())

    def _grader_roots(self) -> dict:
        """`_grader_packages` as DIRECTORIES, for the probe -- `dev_probe.grader_package_roots`.

        Resolved here, at composition, and not inside the probe: the probe is handed a spec and
        must not go looking for a task, while this class already reads the task's `eval_spec` for
        the inspector one line over. Same total-and-quiet contract -- no spec, no fence."""
        from looplab.tools.dev_probe import grader_package_roots
        return grader_package_roots(self._grader_packages())

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

    def _eval_time_budget(self):
        """The operator's per-eval wall-clock ceiling in seconds, or None. ONE resolution for the two
        things this role does with it — the note it is TOLD (`_time_budget_note`) and the bound its
        `declare_stages` is HELD TO (`RepoWriteTools(time_budget=…)`). Separately spelled, the prompt
        could announce one ceiling while the refusal enforced another, which is the F1h defect with
        the roles swapped. Soft-fails to None for a bare/`__new__`-constructed dev carrying no task."""
        from looplab.runtime.command_eval import eval_spec_time_budget
        try:
            return eval_spec_time_budget(self._cmd_context()[0])
        except Exception:  # noqa: BLE001 — a bare/unit-test dev with no task states no budget
            return None

    def _gpu_footprint_note(self, idea) -> str:
        """How many GPUs THIS node will actually get, for the role that writes the launcher.

        `_time_budget_note` one axis over, and the same defect: the RESEARCHER declares the footprint
        and is told the budget (`engine/proposal_cues.py::_stamp_gpu_budget_hint` sets
        `_gpu_budget_hint` on it, and `_gpu_footprint_cue` rides its prompts), while THIS role — the
        one that writes `accelerate launch --num_processes N` into a stage command — was told
        nothing about device count at all. Grep the Developer prompts before this landed: not one
        mention of gpus, devices or CUDA.

        MEASURED on `runs/e5small-dr-unified-v6` node 0, the run this note was written from. The
        node declared `footprint {"gpus": 1}`; its own train stage was authored as
        `accelerate launch --num_processes 2 --multi_gpu -m vectorsearch.train`;
        `engine/resources.py::_acquire_gpus` fenced `CUDA_VISIBLE_DEVICES` to the one granted
        device, and rank 1 died with `torch.AcceleratorError: CUDA error: invalid device ordinal`.
        Cost: `mine` 356.5 s + `train` 171.6 s + a second identical `train` 172.0 s after an INERT
        repair, and 62.7 minutes of wall clock from the first failure to a working manifest — to
        discover a number that fits in one sentence.

        SIX OF SEVEN nodes on this box get it right, so this is a rare miss and the note is a rung,
        not a gate. It only ever ADDS a fact the role could not otherwise know; a static refusal
        (`procs > footprint.gpus`) is a reasonable SECOND rung and is deliberately not here, because
        refusing before informing tells the Developer "no" without telling it what to write — the
        `runtime/deps.py` rule, where text may NOMINATE and only a probe DECIDES.

        UNCONDITIONAL, with no `Settings` flag, following `_time_budget_note` rather than
        `gpu_footprint_cue`: a note spliced into the DEVELOPER prompt makes no provider call, spends
        nothing, kills nothing and moves no selection, so it has nothing a legacy-snapshot default
        would need to hold back. Empty when the footprint states no integer count — a role told
        "some GPUs" is worse off than one told nothing, and this must never guess.

        IT READS THE DECLARATION AND NOT THE GRANT, and the sentence now says so. The granting
        authority is `engine/resources.py::_resource_request_for_node`, which resolves an operator
        `resource_pin` through `core/cards.py::effective_card_footprint` and then CLAMPS to the
        detected pool — so a footprint of 4 on a two-GPU box is granted 2 while this read still says
        4, reproducing the exact `invalid device ordinal` this note exists to prevent with the
        engine's own prose as the cause. Neither input is reachable from an adapter: the pin is the
        Card's and the pool is the Engine's `_gpu_ids`. Stating the declaration and naming what can
        reduce it keeps every case the note was written for (all 132 recorded footprints on this box
        are `{"gpus": 1}`, so the clamp bites on none of them) without asserting a number this side
        cannot know. The exact fix is a stamp from the engine, the shape
        `proposal_cues.py::_stamp_gpu_budget_hint` already uses for the Researcher.
        CLAIM[developer-gpu-note-is-the-declaration] the device count in the Developer prompt is the
        DECLARED footprint, not the grant — the grant clamps it to the detected pool and applies any
        operator resource pin.
        decided:`line:def _resource_request_for_node&&resource_pin@looplab/engine/resources.py`

        THE SECOND FAILURE MODE, added 2026-08-30, and it is the one everything above could not have
        prevented. That text is about asking for MORE devices than the fence holds, which fails
        loudly (`invalid device ordinal`) on the node's own process. The other way out of the fence
        is to OVERWRITE it, and that fails SILENTLY on somebody else's node.

        Measured on the live `e5small-dr-unified-v11`, node 3. It declared NO footprint, and at the
        settled `eval_parallel` 2 (`strategy_decision at_node=3`) the undeclared branch of
        `_resource_request_for_node` grants exactly ONE device and pins it, so
        `CUDA_VISIBLE_DEVICES` was fenced. Its own `run_train.py` then spawned two seed sub-runs
        with `env["CUDA_VISIBLE_DEVICES"] = "0"` and `"1"` — ABSOLUTE physical ordinals that REPLACE
        the inherited fence rather than composing with it — while its own comment asserted the
        opposite ("the mining encoder hardcodes cuda:0, which maps to the fenced device inside each
        subprocess"), which is true only for an INHERITED fence. Three attempts, three
        `torch.OutOfMemoryError`s, each naming two processes resident on GPU 0: 113.44 + 19.85 GiB,
        113.45 + 19.85 GiB, then 57.23 + 81.98 GiB. A 113 GiB process is a full SIBLING training
        (node 2, then node 4, ran across the same window), not node 3's own. The stage ended
        `timeout` exit -9 at 5,079 s and the node terminalised `not_learning` 40 minutes later.

        THE RULE ALREADY EXISTS AND COVERS THE WRONG CHANNEL. `core/envsafe.py::ENGINE_OWNED_ENV`
        refuses a DECLARED `CUDA_VISIBLE_DEVICES` at all three declarer levels, and its own comment
        gives exactly this reason — "a declared one would hand a node its siblings' devices while
        the host pool lease still says otherwise". What the operator may DECLARE is fenced; what the
        agent's own code ASSIGNS at runtime is not, and cannot be by any rung inside the
        interpreter: a process cannot stop its own children from setting an environment variable.
        So the rung is a SENTENCE, matching `75332e97`'s inform-not-refuse precedent above.

        `_FENCE_INHERITANCE_NOTE` IS SPLICED UNCONDITIONALLY, unlike the count sentence below,
        because it carries NO NUMBER: "inherit the fence, never set it" is true at one device, at
        four, and — the case that matters — when the footprint declares nothing at all, which is
        exactly the state the count sentence stays silent for and exactly the state node 3 was in.
        Stating it only for a declared footprint would leave the measured incident uncovered by the
        note written for it.
        """
        gpus = None
        try:
            footprint = getattr(idea, "footprint", None)
            if isinstance(footprint, dict):
                raw = footprint.get("gpus")
                # `isinstance(True, int)` is True, so a bool has to be refused explicitly — a
                # footprint of `{"gpus": true}` would otherwise announce "1 GPU" about a
                # declaration that states no count at all.
                if type(raw) is int and raw > 0:
                    gpus = raw
        except Exception:  # noqa: BLE001 — a bare/unit-test dev carrying no idea states no footprint
            return ""
        if gpus is None:
            return _FENCE_INHERITANCE_NOTE
        return _FENCE_INHERITANCE_NOTE + (
                f"\n\nTHIS NODE IS DECLARED {gpus} GPU{'' if gpus == 1 else 's'} AND WILL GET AT MOST "
                f"THAT MANY. `CUDA_VISIBLE_DEVICES` is fenced to the granted "
                f"{'device' if gpus == 1 else 'devices'} before your stages run, so a launcher that "
                "starts more processes than that dies on the first one that asks for a device "
                "outside the fence (`CUDA error: invalid device ordinal`), after the earlier stages "
                f"have already been paid for. Size every `--num_processes` / `--nproc_per_node` / "
                f"`--gpus` to {gpus} or fewer, and put the per-device batch size where it fits.")

    def _time_budget_note(self) -> str:
        """The operator's per-eval WALL-CLOCK budget, for the role that actually spends it (docs/29 F1h).

        The Researcher picks epochs; THIS role picks the batch size, writes the loop and declares the
        `train` stage's `timeout` — so it is the pair that decides wall clock, and until now only the
        Researcher was ever told the number (`engine/proposal_cues.py::_cue_experiment_time_budget`).
        Measured on `rubertlite-dr-unified-v7` node 0: told "give `train` a GENEROUS timeout" against a
        budget it could not see, the Developer declared `timeout: 172800` — 48 h against the operator's
        21600 s — and paced a schedule at 7 h 50 m. `_run_stages` still takes a declared stage timeout
        as a FALLBACK-replacement and never a clamp, so nothing at the WALL refuses the generous leash;
        what refuses it since 2026-08-14 is `declare_stages`, at authoring time, while the Developer can
        still act (`command_eval.stage_time_budget_refusal`). The note is the rung above that refusal and
        is the reason most manifests never reach it.

        Derived from `command_eval.eval_spec_time_budget`, the SAME rule the engine quotes to the
        Researcher, because two roles sizing one schedule between them must be given one number. Empty
        when the task declares no eval spec at all (an onboarding/toy dev has no budget to state).

        THE NUMBER WAS SHARED AND THE SEMANTICS WERE NOT, which is the F1h defect one level in — and
        it is what this note said wrong until 2026-08-15. It announced the budget as a POOL: "one
        evaluation of this node gets {span}, END TO END: every stage you declare plus the protected
        scoring step". Nothing in the engine implements that. `_run_stages` takes each stage's own
        ceiling (`finite_timeout(_stg.get("timeout", timeout), timeout)`) with no accumulator and no
        cross-stage deadline, and `engine/eval_stages.py::_resolve_stages` appends `score` with the
        operator's OWN `score_timeout` — a fresh copy of the budget, on top of everything preceding.
        The gate the same role is held to says so in as many words (`stages_over_time_budget`: the
        rule is per STAGE and the sum rule is refused, because `score` runs at the operator's number
        on top of whatever precedes it), and so does `docs/guide/configuration.md`. Only the two
        prompts disagreed, and they disagreed in the direction that costs GPU time.

        MEASURED, 2026-08-15, over every `stage_finished` in `runs/`: **51 stage rows ran LONGER than
        their own run's entire per-eval budget** and not one was killed for it — 45 nodes of
        `rubertlite-dense-retrieval` each spent a single `train` stage at 1.1x-6.0x a 3600 s budget
        and were SCORED, and v7 nodes 0/1 ran 29389 s and 29184 s against 21600 s. A pool that 51
        rows walk through is not a pool.

        WHAT THE FICTION COST, measured on `rubertlite-dr-unified-v8` — the FIRST run to evaluate
        under the authoring gate (merged 2026-08-14 12:06 UTC; v8's engine loaded its source at
        16:25). It is also the only run in the corpus whose manifests all sit BELOW the budget:
        sum-of-declared/budget of 0.60, 0.70, 0.83, 0.86, 0.89, 0.90, 0.90, 0.95. That is not
        under-declaration, it is PARTITION — the role divided a pool it had been told existed, and
        every second it reserved for the scorer or for a sibling stage is a second the one stage that
        can overrun did not get. Node 9 gave `mine` 7200 s (it used 2349.6) and `train` 14400 s,
        holding 14400 s back for a `score` stage that takes ~3100 s on this task and is charged to
        none of it. Its `train` died at 14402.67 s, 73 % through, ~5160 s short, with 21600 s of
        ceiling it was allowed to declare and did not.

        AND THE COST IS NOT GPU-HOURS, IT IS THE EXPERIMENT. All five `stage_finished.status
        ="timeout"` rows in `runs/` were answered by a repair that made the EXPERIMENT smaller —
        `n_epochs` 10->5 (v6 n5), 15->8 (v8 n3), 10->6 (v8 n9), fewer epochs / a val subset (v2 n3),
        fewer ANCE negatives (dense-retrieval n72) — and **zero of the five raised the stage's own
        ceiling**. Across all 87 `node_repaired` rows carrying a change set, a stage `timeout` was
        raised exactly ONCE (v2 node 0, 14400 -> 21600) and that raise went ABOVE the operator's
        budget, i.e. the gate would refuse it today. The role has never once spent ceiling it was
        entitled to, because the note told it the ceiling was already spoken for.

        WHAT THIS NOTE DOES **NOT** REACH, stated because the brief that asked for it overstated the
        scope. Only TWO of those five timeouts had headroom to raise into (v8 n3 at 22000 and v8 n9
        at 14400, against 36000); the other three declared a ceiling EQUAL to the budget (v2 n3, v6
        n5, both 14400) or ALREADY ABOVE it (dense-retrieval n72, 21600 against 3600), so no wording
        here could have saved them — for those the budget itself was the bound. And an under-declared
        ceiling is not the only pressure that shrinks an experiment. Re-derived through
        `repair_verify.declared_param_overrides` over all 2,484 `node_repaired` rows at 2026-08-15
        22:26 (v8 LIVE — this population is still growing, so quote the instant with the number):
        FOUR rows on THREE nodes, all v8, and they split two-and-two. TIME pressure: v8 n3 a5 (the
        batch/grad-accum reshuffle, effective batch preserved, science-neutral) and v8 n9 a1
        (`n_epochs` 10->6, NOT neutral). MEMORY pressure: v8 n3 a4 (OOM, neutral) and v8 n8 a2 (OOM,
        `batch_size` 8192->4096 AND `n_epochs` 15->8, NOT neutral — the epoch cut rides along with a
        memory fix and is justified nowhere). So on that population the two pressures are TIED at one
        science-altering instance each, this note reaches n9 and not n8, and nothing about a legible
        budget would have helped n8.

        THE RUNG THAT ALREADY CATCHES A SHRINK, and why this is not redundant with it. The stage
        `expect.assert` fires on exactly this: v8 node 8's `train` returned `check_failed —
        declared_condition_violated: training stopped at epoch 7.99, not the declared 15 epochs`.
        Measured 2026-08-15 over every `looplab_stages.json` in `runs/`: 143 declared stages, 34
        (24 %) carry an `assert` at all, 23 (16 %) name a quantity a shrink would falsify — but 16 of
        those 23 are v8, i.e. on the CURRENT configuration nearly every `train`/`mine` stage carries
        one, and the rung is not self-defeating (of 6 assert edits across 47 node manifest series,
        ZERO weakened a declared quantity; v8 node 0's went 2 epochs -> 15). What that rung cannot do
        is make the wrong move cheap: it fires at the stage boundary, so node 8 spent **14,105.1 s**
        (3.9 GPU-h) discovering that its own repair had broken its own contract, and node 9's
        repaired manifest still asserts "all 10 epochs completed" against code that now runs 6 — so
        the shrink it chose is not merely incomparable, it is queued for the identical verdict. This
        note is the rung ABOVE it, where the choice is made and costs nothing, and the assert is the
        backstop for when it is made anyway.
        """
        from looplab.runtime.command_eval import format_time_budget
        budget = self._eval_time_budget()
        if budget is None:
            return ""
        span = format_time_budget(budget)
        return (
            # PER STAGE, not a pool: the semantics the gate enforces and `_run_stages` implements.
            # The old "end to end: every stage you declare plus the protected scoring step" is the
            # sentence the docstring above measures; it must not come back.
            f"\n\nWALL-CLOCK BUDGET — {span} PER STAGE. That is the ceiling for each stage "
            "separately, not a pool divided between them: every stage you declare runs on its own "
            "clock, and the protected scoring step then runs under the operator's own copy of the "
            "same number, on top of yours and charged to nothing you declare. So do NOT hold time "
            "back for the scorer or for the other stages — for each stage, declare the time THAT "
            "stage actually needs, up to the budget. The schedule and the batch size YOU choose are "
            "what decide whether it fits, so estimate before you commit (total_steps x per-step time) "
            "and cut the SCHEDULE only if the stage cannot fit its own ceiling — fewer epochs or "
            "steps, a subsample, a larger batch if the memory allows. A stage still running at ITS "
            "declared wall is killed with NO metric, and every "
            "GPU-hour it spent is discarded: a shorter run that REPORTS A NUMBER beats a longer one that "
            "reports nothing. A stage `timeout` longer than the budget is not more budget — it only "
            "removes the guard, and a stage that outlives the budget spends GPU-hours the run was never "
            "planned around. "
            # Spliced 2026-08-14 beside the sentence it makes true: the clause above states why a long
            # leash is not more budget, and this states that declaring one is now REFUSED rather than
            # merely unwise. The rest of the note is untouched — it is a contract, not prose.
            "`declare_stages` REFUSES a stage `timeout` above the budget, so declare the time you "
            "actually estimate a stage needs and cut the schedule to fit. "
            # Spliced 2026-08-15. The five timeout repairs in the corpus all cut the experiment and
            # none raised the ceiling, twice with hours of unclaimed ceiling sitting there — so the
            # move has to be NAMED, not merely permitted. See this method's docstring.
            "IF A STAGE WAS KILLED PURELY BY WALL CLOCK — real progress, no stall, no divergence — "
            "and its declared `timeout` was BELOW the budget, then the first fix is the CEILING and "
            "not the science: re-declare that stage nearer the budget and re-run it unchanged. "
            "Cutting epochs, data or steps changes what the experiment MEASURES and makes this node "
            "incomparable with its siblings, so spend the ceiling you were given before you spend "
            "the comparison. "
            "Do not shrink the experiment past the point where it answers the "
            "researcher's question; shrink the schedule, and say in your notes what you cut.")

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
        # `allow_env=True`: these are the OPERATOR's stages, read here only to describe the
        # pipeline to the Developer. Validating them under the Developer's own fail-closed rule
        # would drop the whole operator pipeline out of the prompt the moment the operator
        # declared an `env`, and the agent would be told it may author stages the engine will
        # ignore (M7 — this reader and `_resolve_stages` must accept the same thing).
        return validate_stages(ev["stages"], allow_env=True)[0] or []

    def _stage_note(self, operator_stages, declared, carried_over, manifest_protected) -> str:
        """The three-way pipeline note the implement sessions read (doc 25 RA-07).

        Moved VERBATIM out of `_run`'s middle. This is PROMPT TEXT, so the bytes are the contract:
        CLAUDE.md forbids rewording one as part of a refactor, and the wording here is load-bearing
        for a reason the comment below records — the old prompt asserted a train stage
        unconditionally, and after an empty STAGES phase the model wrote a score-only entrypoint that
        scored a stale checkpoint. `tests/test_repo_stage_note.py` pins all three variants byte-for-
        byte, so a "tidy-up" of this wording is a red test rather than a silently different agent.
        """
        # Tell the implement sessions what pipeline ACTUALLY exists. The old prompt asserted
        # "your STAGES phase already declared a train stage" unconditionally — after a failed/
        # empty stages phase the model then wrote a score-only entrypoint that scored a stale
        # checkpoint (or crashed on a missing one) instead of training.
        _chain = " → ".join(str(s.get("name")) for s in declared)
        if operator_stages:
            return (f"\nPIPELINE for this node (OPERATOR-declared, runs verbatim): "
                    f"{_chain}. Implement the code those stages run.")
        if declared:
            _src = ("carried over from the parent solution — your STAGES phase declared "
                    "nothing new this node" if carried_over else "declared by your STAGES phase")
            return (f"\nPIPELINE for this node ({_src}): {_chain} "
                    "→ score (operator cmd). Implement the code those stages run; the "
                    "eval entrypoint only SCORES the artifacts the earlier stages produce.")
        return ("\nNO pipeline stages are declared for this node"
                + (" (the operator protected looplab_stages.json)"
                   if manifest_protected else "")
                + ": the operator's cmd runs ALONE as a single command. The code it "
                "runs must do ALL the work itself when invoked — train a FRESH model, "
                "then score it and print the metric (never read a pre-existing "
                "checkpoint or a static results file).")

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
            "`%params%`). Give training a generous timeout."
            # docs/29 F1h: spliced AFTER the generous-timeout ask, never instead of it — "generous"
            # without a ceiling is what produced a 48-hour `train` stage on a 6-hour budget.
            + self._time_budget_note()
            # The device count, at the SAME splice position and for the same reason one axis over:
            # this is the phase that authors the launcher command, so it is the phase that has to
            # know how many devices exist.
            + self._gpu_footprint_note(idea) + "\n\n"
            "GIVE EVERY STAGE ITS `needs` — the workdir-relative files it READS and cannot run without: "
            "what an earlier stage produces, plus whatever the seeded workdir must already contain. The "
            "engine checks them BEFORE the stage starts, so a pipeline whose stages disagree about where "
            "a file lives fails in one second with both declarations named, instead of after the "
            "training. This is not paperwork — it is the defect that has cost this project the most: a "
            "run once trained a good model for 76 minutes and then scored a DIFFERENT experiment's "
            "checkpoint, because the scorer read a path the trainer never wrote to, and every contract "
            "passed. Write the same path string the stage's own code will open. If a stage genuinely "
            "reads nothing but the repo it was seeded with, omit `needs` — an empty declaration is "
            "worse than none.\n\n"
            "YOU MAY NOT DECLARE `env`, AND YOU SHOULD NOT BAKE ONE INTO THE REPO EITHER. The "
            "environment a stage runs in is the OPERATOR's to set (they have `cmd.stages[].env`, "
            "`cmd.env` and the run's `eval_env`), and a stage `env` from you is refused. If a stage "
            "needs a variable the environment does not have, the right fix in YOUR code is to read it "
            "with a documented default; adding `os.environ.setdefault(...)` at import to make one node "
            "work is not, because the next node is seeded from the SOURCE repo and will hit the "
            "identical failure — that has happened, and it cost one repair attempt per node. Say in "
            "your notes which variable was missing so the operator can declare it once for the run.\n\n"
            "GIVE EVERY STAGE AN `expect` — this is what makes a stage's success mean something. A stage "
            "that exits 0 has proved nothing: a mining stage that covered 1.2% of the queries exits 0 "
            "exactly like one that covered 100%, and the next stage consumed the 1.2% as if it were "
            "whole (this happened, and the node's whole result was meaningless). `expect` has two parts "
            "and you should usually give both:\n"
            "  • `files`: the workdir-relative paths this stage WRITES. The engine checks after the "
            "stage that each exists, is non-empty, and was written by THIS run of the stage.\n"
            "  • `assert`: ONE line stating what this stage's success MEANS, phrased so it can be "
            "checked against what the stage PRINTS — 'negatives.parquet written with at least "
            "n_negatives negatives on every row', 'all 30 epochs completed and a best-val checkpoint "
            "saved', 'embeddings written for every document in the corpus'. State the WORK, not the "
            "result quality: 'recall beats 0.85' is not a stage condition — the search ranks results, "
            "this only asks whether the stage did its job.\n"
            # THE EXAMPLE HERE IS LOAD-BEARING AND THIS ONE WAS MEASURED WRONG. It used to read "hard
            # negatives mined for at least 90% of the training queries", and that exact sentence is
            # in 28 of the 33 numeric asserts this corpus has ever produced — the model was not
            # inventing a bar, it was COPYING the one it was shown, which is why 6 different runs
            # converged on the same number. On this data the true figure is 41.8 % (908,121 of
            # 2,170,069): `add_negatives` inner-joins mined ids to product names and drops the rest
            # BY DESIGN, and the champion (0.7934) was trained on exactly that. So the shipped
            # example refused the recipe that produced this box's best result — verified on
            # e5small-dr-unified-v8 node 1, which mined a valid 2,732,976-row parquet, failed its own
            # gate, and was abandoned after two repairs with the engine's own diagnostician calling
            # it `check_false_positive` and being right.
            #
            # THE REPLACEMENT IS NOT A SMALLER NUMBER, IT IS A DIFFERENT KIND OF CLAIM: every row
            # carrying its n_negatives is a property the stage CONTROLS and can guarantee; the share
            # of queries that survive a downstream join is an OUTCOME of the data it does not. A
            # stage that mines 1 % still fails this loudly, which is the whole point of `expect` two
            # paragraphs up. The sentence below says the rule outright, because an example alone is
            # what got copied last time.
            "A numeric bar you have NOT measured on THIS data is a guess, and a guess in an `assert` "
            "fails stages whose artifact is correct. Assert the property the stage CONTROLS (every "
            "row has its negatives; the checkpoint exists; the file covers the ids it claims) and "
            "PRINT the quantity you do not control (coverage, survival rate, class balance) as "
            "information. If you genuinely need a bar on an outcome, measure it first — read the "
            "code that produces the number, or run the smallest probe that answers it — and say in "
            "the assert what you measured it against.\n"
            "Write the `assert` against a number the stage will actually print; the implement phase then "
            "has to make the stage print it (and assert it in code). Declare `expect` for EVERY stage "
            "you can, and especially for any stage whose script you will NOT be able to edit later "
            "because the operator protected it — there, the manifest is the only place its success "
            "condition can be stated at all.\n\n"
            "MARK THE TRAINING STAGE. Put `\"role\": \"training\"` on the ONE stage that runs the "
            "training loop — the stage whose loss curve means something — and omit it everywhere "
            "else (omit it entirely if no stage trains). It is what lets the live watchdog END that "
            "stage early when it is provably broken: a loss frozen for hours, a collapsed gradient, "
            "a diverged run. Without it the watchdog still reads the log and still reports, but "
            "cannot stop anything, and a model that stopped learning in its first hour burns the "
            "whole timeout and scores zero — which is exactly what happened to a node that ran all "
            "57,600 steps after its loss froze, while the watchdog said 'broken' thirty-one times. "
            "Nothing else changes for you: it does not alter how the stage runs, and it is spent "
            "only against your own stage.\n\n"
            "Then call `declare_stages` once. You are NOT "
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
        # scouts read the LIVE overlay (the parent solution on improve/merge), not the pristine repo.
        # Composed first so the user turn can name what it already holds — see the plan phase above.
        # NO `answered_by_context` HERE, deliberately. It was spliced in and measured INERT: the
        # block is built from providers' optional `inventory()` hook, and none of this toolset's
        # providers (`EnvInspectTools`, `RepoScoutTools`, `DevCommandTools`, `DevProbeTools`)
        # implements it -- so it rendered "" at every call while its comment claimed the user turn
        # "names what this toolset already holds". A count-publishing block cannot express "how much
        # is under this repo path" anyway; giving the scouts a real inventory is the fix, and until
        # one exists the honest state is no block rather than an empty string and a false comment.
        read_only = CompositeTools([EnvInspectTools(self._grader_packages())] + self._scout_tools(write))
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": self._stages_user(idea, ev, has_cmd)}]

        def _validate(args):                      # bounce a malformed manifest back to the model
            stages = (args or {}).get("stages")
            _, err = validate_stages(stages, reserved=reserved)
            if err:
                return err
            # The OPERATOR's wall-clock ceiling. Bounced here as well as at the write tool because
            # this phase reaches `declare_stages` through the EMIT SPEC and never through the tool,
            # exactly like the F1c collision below — and this is the site that matters most: it is the
            # one moment the Developer can still cut the schedule without having spent a GPU-second.
            over = write.stage_budget_refusal(stages)
            if over is not None:
                # RECORDED, not only spoken. The refusal lives in the model's context for one session
                # and then is gone, while the number the Developer ASKED for is the only evidence an
                # operator whose budget is genuinely too small will ever get: on v7 node 0 the node
                # really did need ~27960 s against a 21600 s budget, and a refusal nobody can read
                # afterwards leaves that operator with a run that keeps under-delivering for no
                # stated reason. Zero-work marker span, the same shape as `plan_steps_failed` below.
                from looplab.runtime.command_eval import record_stages_over_time_budget
                record_stages_over_time_budget(
                    stages, self._eval_time_budget(), at="declare", enforced=True)
                return over
            miss = _missing_stage_input_paths(stages)   # a hallucinated non-existent data path → re-declare
            if miss:
                return _missing_paths_feedback(miss)
            # F1c: an already-seeded file (the PARENT's config on an improve/merge) that names this
            # declaration's own output absolutely, in the editable SOURCE tree. This phase reaches
            # `declare_stages` through the emit spec and not through the write tool, so the tool's
            # own copy of this check never runs here — and a merge node authoring a fresh manifest
            # over a carried-over config is exactly the shape that cost v2 node 4 and v6 node 4 their
            # metrics. Bounced, so the model re-declares while it still can.
            return write.manifest_collision_refusal(stages)

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
                on_budget=self._note_session_budget, **self._session_opts()) or []
        except Exception:  # noqa: BLE001 — a failed stages phase degrades to the operator cmd alone
            return []

    def _run(self, idea: Idea, error: Optional[str] = None,
             base: Optional[dict] = None, base_note: str = "",
             base_deleted: Optional[list] = None) -> str:
        from looplab.agents.agent import run_phase
        from looplab.core import tracing
        # Cleared per CALL, before anything can fail: this developer instance is SHARED across
        # concurrent `_evaluate` tasks (see the `repaired_files` snapshot note in evaluate.py), so a
        # stale value left by a sibling node's repair would make THIS node re-run an expensive stage
        # nobody asked it to. Every early return below (including the `except` that mints the
        # developer-error sentinel) therefore leaves it "" rather than the previous call's answer.
        self.last_rollback_stage = ""
        # WHICH BOUND ENDED THE SESSION, or "" for one that finished on its own terms. Cleared here
        # for the same shared-instance reason as the line above — a sibling node's exhaustion must
        # never be read as this node's.
        #
        # `tool_loop.py` has computed and announced this since it was written ("TELL SOMEONE …
        # presenting a cut-short investigation as a finished one is how 'the assistant hangs around
        # 40 tool uses and then something odd comes back' reads to an operator who was never told the
        # turn ran out of wall clock") and NOTHING SUBSCRIBED: `on_budget` appeared outside
        # `tool_loop.py` only in `loop_options.py`'s registry. Measured over `runs/`, pairing each
        # `inline_repair` session with its own verdict: 12 of the 12 `inert` repairs in the whole
        # corpus ran past `session_time_budget_s`, and 0 of the 65 that finished inside it are inert.
        # So `inert` — "the change set was empty" — has been doing double duty as an undiagnosed
        # proxy for "ran out of clock", and the two have opposite remedies.
        #
        # The same failure is already on the record one phase over: the `stages` session's own
        # comment describes reading "for the whole budget, never reached declare_stages, and silently
        # degraded to 'no stages declared' … observed live". That was fixed by widening the clamp;
        # this records it instead, because the repair bound is not obviously wrong (median repair =
        # 151 s, 13 % of it) and a bound nobody can see the effect of cannot be argued about.
        self.last_budget_exhausted = ""
        # Resolved ONCE for the whole node: operator `cmd.stages` make declare_stages refuse (P12)
        # and drive the stage notes below; data-mount names make mount refusals honest.
        op_stages = self._operator_stage_list()
        write = RepoWriteTools(self._surface, self._protected, self._prefixes, editables=self._editables,
                               operator_stages=bool(op_stages),
                               data_mounts=getattr(self, "_data_mounts", None),
                               time_budget=self._eval_time_budget())
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
            + self._system_body(render)
            + operational_attention_points() + "\n\n"
            + _REPO_DEV_COMMANDS_HEADER + self._recipes() + "\n\n"
            + ((_REPO_DEV_RESULTS_HEADER + _results + "\n\n")
               if (_results := self._results_context()) else "")
            + _REPO_DEV_SOURCE_HEADER + self._repo_context())
        user = (f"Experiment concept (the researcher's idea): {idea.rationale}\nHyperparameters to use: {params}.\n"
                "Design and implement the eval entrypoint (and any edits) now with write_file, then call done."
                # docs/29 F1h: the STAGES phase declares the leash, but the CODE written here is where the
                # schedule and the batch size are actually chosen — and a repair session (which skips the
                # stages phase entirely) reaches this path and nothing else.
                + self._time_budget_note()
                # A repair session skips the stages phase entirely and reaches ONLY this path, which
                # is exactly where v6's launcher bug had to be fixed — so the count rides here too.
                + self._gpu_footprint_note(idea))
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
        # THE REPAIR SESSION'S ANSWER, kept rather than discarded (F8). This method's return value is
        # a SENTINEL CHANNEL on the repo path — the artifact travels on `last_files`, so `""` means
        # "the files are the answer" and a non-empty string is a verdict the engine reads
        # (`DEVELOPER_ERROR_PREFIX` was the only one). `engine/evaluate.py` appends
        # `developer_stuck_contract(...)` to EVERY inline repair ask, including this one, and the
        # model can answer it only through its `done` summary — which was fed to `run_phase` and
        # then dropped, so `is_developer_stuck(new_code)` was asked about `""` and the declaration
        # never reached the engine. The node fell to the INERT rung instead, which costs one more
        # full evaluation before `INERT_REPAIR_LIMIT` abandons it (~2.7 h on the v4 node 6 link).
        repair_verdict = ""
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
                # The binding stays: `_run_step` below is passed `stage_note=stage_note`, so
                # dropping it here would be an UnboundLocalError on the plan path (pyflakes caught
                # exactly this on the first draft of the extraction).
                stage_note = self._stage_note(operator_stages, declared, carried_over,
                                              manifest_protected)
                user += stage_note
            messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
            # Compose the write/edit tools with read-only ENVIRONMENT INTROSPECTION (pkg_info / py_api /
            # read_installed / grep_installed) so the Developer grounds generated code in the ACTUAL
            # installed API/version instead of guessing (the precision='16-mixed'-on-Lightning-1.5 class).
            tools = CompositeTools([write, EnvInspectTools(self._grader_packages())] + self._scout_tools(write))
            if is_fresh_repo:
                # PLAN is the Developer's second sub-phase (its own trace band). IMPLEMENT runs under
                # the orchestrator's "implement" span (so its generations band there, and non-repo
                # developers keep that band unchanged).
                steps = []
                if getattr(self, "_plan_decompose", False):
                    with tracing.operation("plan"):
                        steps = self._propose_plan(system, idea, write, baseline_note=base_note)
                if len(steps) >= getattr(self, "_plan_min_steps", 2):
                    # A step error deliberately can't abort the plan — later steps and the eval still
                    # run on whatever got written. But it must not vanish either: discarded, a later
                    # eval failure could never be attributed to the step that broke. Collect them and
                    # stamp ONE span so the trace says which steps failed and why.
                    #
                    # The SAME argument applies to a step that does not error: the plan is a
                    # proposal, the artefact is the truth, and until this loop diffed the working set
                    # around each step nothing recorded which step actually produced which shipped
                    # file (or that a step produced nothing at all). Each step now gets its own
                    # `plan_step` trace band — which is what the phase list above has claimed since
                    # it was written, and was not true: `_run_step` calls `run_phase`, which opens no
                    # operation span, so all K sessions collapsed into one band with no ordinal — and
                    # the reconciliation is stamped as `plan_steps` (see `plan_step_attribution`).
                    step_errors: list = []
                    observed: list = []
                    # The measurement the NEXT step is handed. Empty for step 1 (nothing has been
                    # edited yet) and re-emptied after every step, so a stale number from two steps
                    # ago can never be presented as this step's result.
                    feedback = ""
                    for i, step in enumerate(steps, 1):
                        before, before_deleted = dict(write.files), set(write.deleted)
                        # Cleared per step, not per plan: `last_budget_exhausted` is sticky on the
                        # developer, so without this a single cut step would mark every later one.
                        self.last_budget_exhausted = ""
                        self.last_budget_facts = {}
                        with tracing.operation("plan_step", index=i, total=len(steps),
                                               title=str(step.get("title") or "")[:120]):
                            note = self._run_step(idea, step, i, len(steps), write,
                                                  system, stage_note=stage_note,
                                                  baseline_note=base_note, feedback=feedback)
                        step_cutoff = str(getattr(self, "last_budget_exhausted", "") or "").strip()
                        # Compare CONTENT, not just presence: `edit_file` patches in place, and a
                        # step that rewrote a file byte-for-byte changed nothing and must not be
                        # credited with authoring it.
                        observed.append({
                            "wrote": sorted(p for p, body in write.files.items()
                                            if before.get(p) != body),
                            "deleted": sorted(set(write.deleted) - before_deleted),
                            "cutoff": step_cutoff,
                            # What the cut step had spent and how long it ran. Empty for a step
                            # that finished on its own terms, which is most of them.
                            "cutoff_seconds": (getattr(self, "last_budget_facts", {}) or {}).get("seconds"),
                            "cutoff_detail": (getattr(self, "last_budget_facts", {}) or {}).get("detail") or "",
                            "error": note})
                        if note:
                            step_errors.append(note)
                        # Measure only when this step actually CHANGED the working set, and never
                        # after the last step: the final artefact goes straight to the engine's own
                        # evaluation, so a run here would have no reader and would cost 40 s.
                        feedback = ""
                        if i < len(steps) and (observed[-1]["wrote"] or observed[-1]["deleted"]):
                            feedback = self._step_feedback(write, index=i)
                    with tracing.operation(
                            "plan_steps",
                            **plan_step_attribution(steps, observed, write.files)):
                        pass
                    if step_errors:
                        with tracing.operation("plan_steps_failed", failed=len(step_errors),
                                               total=len(steps),
                                               detail="; ".join(step_errors)[:600]):
                            pass
                else:
                    # single-session implement is TERMINAL (evaluation reads no brief) → consume the
                    # briefs + read-cache, but no wasted summary call (handoff=False).
                    run_phase(self.client, tools, messages, self._emit_spec(),
                              label="Developer·implement", handoff=False,
                              finalize=lambda a: (a or {}).get("summary", ""),
                              fallback=lambda m: "", on_budget=self._note_session_budget,
                      **self._session_opts())
            else:
                # repair / toy single session — terminal, so no summary (and repair isn't in a scope
                # anyway when it runs inline during eval; the debug-operator repair gets an empty ledger).
                # A REPAIR additionally gets `rollback_stage` on its `done` (see `_repair_emit_spec`),
                # captured into `last_rollback_stage` from the emit args — the engine reads it off the
                # developer exactly as it reads `last_files`, through the DEVELOPER_OUTPUT_ATTRS seam.
                def _finish(a):
                    if error:
                        self.last_rollback_stage = str((a or {}).get("rollback_stage", "")).strip()[:64]
                    return (a or {}).get("summary", "")

                # A REPAIR THAT DESCRIBES AN EDIT IT NEVER MADE gets ONE chance to actually make it.
                # Measured on v8 node 1: 51 minutes, 108 tool calls, zero writes, and an emit naming
                # two files it had "changed" — the diagnosis correct, the application absent, and the
                # node abandoned after the second such attempt. The byte fact comes from the write
                # tool's own ledger, never from the summary; the rule is in
                # `engine/repair_verify.py::repair_claimed_without_writing` beside the claim
                # vocabulary it reuses. This CANNOT reach the `inert` verdict, which stays decided on
                # bytes with the rationale unread so that no wording can steer the one verdict the
                # loop acts on.
                _files_before = dict(write.files)
                _deleted_before = list(write.deleted)
                _bounced = []

                def _validate_repair(args):
                    # ONE-SHOT. A second bounce would spend the session arguing instead of editing,
                    # and the model has already been told exactly what to do; `agent_emit_force`
                    # bounds the loop but must not be what stops this.
                    #
                    # The shot is only spent where it can be SPENT: `drive_tool_loop` calls a
                    # validator on a forced emit only when a turn remains to act on the refusal
                    # (`_accept_forced(may_retry=…)`). On the terminal salvages the emit is accepted
                    # unvalidated instead — rejecting there dropped the summary and `rollback_stage`
                    # on the floor and left `repair_verdict` empty, which is how a rung meant to buy
                    # one more edit came to cost the whole repair record.
                    if not error or _bounced:
                        return None
                    # ONE place decides "did this session write anything", and it is the `wrote`
                    # parameter the rule's own docstring says owns it. Testing it here and then
                    # passing the literal `False` stated the byte fact twice and left the parameter
                    # unreachable outside tests — so a second caller reading that docstring would
                    # get a different answer from the one the repair loop gets.
                    from looplab.engine.repair_verify import repair_claimed_without_writing
                    refusal = repair_claimed_without_writing(
                        (args or {}).get("summary", ""),
                        wrote=(write.files != _files_before
                               or write.deleted != _deleted_before))
                    if not refusal:
                        return None                     # claimed nothing concrete — a legitimate
                    _bounced.append(True)               # "no change needed" answer is left alone
                    return refusal

                repair_verdict = run_phase(
                          self.client, tools, messages,
                          self._repair_emit_spec() if error else self._emit_spec(),
                          label=("Developer·repair" if error else "Developer·implement"), handoff=False,
                          finalize=_finish, validate=_validate_repair,
                          # THE ONE CALLER THAT OPTS IN. On an exit with no turn left, bouncing this
                          # summary only drops it and falls to the `lambda m: ""` below — which
                          # discards `rollback_stage` and leaves `repair_verdict` empty, so
                          # `is_developer_stuck` can never fire. Accepting an unverified summary is
                          # strictly better, and the durable `inert`/`unmet` verdicts grade it on
                          # BYTES downstream. No other caller may have this: the stages session's
                          # `validate` is the operator's wall budget and manifest-collision fence.
                          terminal_salvage=True,
                          fallback=lambda m: "", on_budget=self._note_session_budget,
                      **self._session_opts())
        except Exception as e:  # noqa: BLE001 - never crash the engine on a developer hiccup
            self.last_files = dict(write.files)
            self.last_deleted = list(write.deleted)
            from looplab.core.models import developer_artifact_footprint
            self.last_footprint = developer_artifact_footprint(
                idea.footprint, "", self.last_files)
            return f"{DEVELOPER_ERROR_PREFIX} {e})"
        self.last_files = dict(write.files)
        self.last_deleted = list(write.deleted)
        from looplab.core.models import developer_artifact_footprint
        self.last_footprint = developer_artifact_footprint(
            idea.footprint, "", self.last_files)
        # A REPAIR ONLY, and only the exact sentinel. The build-time `implement` path has nothing to
        # be stuck about and never carries the contract, so its summary stays discarded and a fresh
        # implement is byte-identical. Everything else a repair summary might say is prose about an
        # edit that already travelled on `last_files`; returning it would be handed to
        # `_repair_provider_failure` as "not a program" and charge the provider-failure counter,
        # which is the OPPOSITE verdict (it pauses the run over a provider that is answering fine).
        from looplab.core.models import is_developer_stuck
        if error is not None and is_developer_stuck(repair_verdict):
            return repair_verdict
        # A BUILD that wrote NOTHING did not build anything, and must not become a node.
        #
        # Measured 2026-08-20 on an AlgoTune `discrete_log` run: the implement phase spent 19
        # generations calling `run_probe` 24 times, `read_file` 8 and `grep` 7 -- and `write_file` /
        # `edit_file` ZERO times. The session ended on its own time budget, `_run` returned "" (no
        # error), `last_files` was `{}`, and the engine committed a node whose `node_created.files`
        # is the empty dict. Its `solver.py` was therefore the untouched template, `raise
        # NotImplementedError`; the evaluation ran honestly and recorded `speedup: 0.0` after 195
        # paid calls and $0.18. Nothing in the loop could tell that apart from an experiment that
        # was tried and failed -- which is the expensive half, because a real 0.0 is EVIDENCE and
        # this one is an empty box wearing its clothes.
        #
        # The cause is a missing forcing function, not a confused model: probing is cheap and
        # commits to nothing while writing commits, so with no bound the safe move is always one
        # more probe. `agent_emit_after`/`agent_emit_force` are TURN counts (300/500) and this
        # session ended at 19, so neither was ever in play; the session's wall budget cut it first.
        # Fixing the incentive belongs upstream in the prompt and the emit contract. This is the
        # rung that keeps its failure VISIBLE and cheap in the meantime.
        #
        # Scoped to a FRESH build: `implement_from` and `repair_from` pre-load `write.files` from a
        # base, so an unchanged working set there is a NO-OP EDIT and a different fact, already
        # judged one rung over by `engine/repair_verify.py`'s `inert` verdict and its
        # INERT_REPAIR_LIMIT. Convicting it here too would double-charge the same event under two
        # vocabularies.
        refusal = empty_build_refusal(error=error, base=base, base_deleted=base_deleted,
                                      files=write.files, deleted=write.deleted)
        if refusal:
            return refusal
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

    def __init__(self, client, repo_path, goal, direction, command, timeout, prompts=None):
        self.client = client
        self.repo_path = repo_path
        self.goal = goal
        self.direction = direction
        self.command = command
        self.timeout = timeout
        self.prompts = prompts

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
            from looplab.core.prompts import render
            code = extract_code(self.client.complete_text(
                [{"role": "system", "content": render(
                    self.prompts, "repo_onboarder_system", self._SYS)},
                 {"role": "user", "content": user}]))
        except Exception as e:  # noqa: BLE001 — propose a stub; human will reject/fix
            code = f"def read_metric(workdir):\n    raise RuntimeError({str(e)!r})\n"
        return {
            "eval_spec": {"command": list(self.command),
                          "metric": {"kind": "adapter", "path": "LOOPLAB_adapter.py"},
                          "params_style": "none", "timeout": self.timeout},
            "adapter_files": {"LOOPLAB_adapter.py": code},
            "goal": self.goal,
        }
