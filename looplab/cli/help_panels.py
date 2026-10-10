"""The `looplab --help` layout: which panel each command is listed under, in what order, and the
one-line summary the command list shows.

WHY THIS EXISTS (doc 74 EB-08). Typer lists commands in REGISTRATION order, and registration order is
the import order of the command-group modules in `cli/__init__.py` — chosen for import safety, not
for a reader. Measured 2026-10-09: `looplab --help` was 74 commands in one 34 KB panel that opened
with `mlebench-extras` and `bait-materialize`; `run` was 63rd and `ui` 72nd, and 29 of the summary
lines cited internal documents ("doc 52 row 22", "§21.11"). A newcomer looking for how to start had
to scroll past the maintainers' research instruments to find it.

WHAT IT DOES NOT CHANGE. The command-group MODULES stay split by what a command does to the world
(`tests/test_cli_command_groups.py`); this table only decides presentation. Each command's full
docstring — provenance, measurements, doc citations — is still what `looplab <command> --help`
prints; only the one-line summary in the command LIST comes from here.

ONE ROW PER COMMAND, enforced two ways by `tests/test_cli_help_panels.py`: every registered command
has exactly one row (a new command without a row is red, so it cannot silently land at the bottom of
"Commands"), and every row names a registered command (a renamed command cannot leave a dead row).
"""
from __future__ import annotations

# (panel, [(command, summary), ...]) in display order. The first panel is what a newcomer needs.
HELP_PANELS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Start here", (
        ("init", "Write a documented looplab.yaml to edit and run."),
        ("run", "Start a run from a config/task file or from --goal."),
        ("ui", "Serve the web UI with Assistant (needs the [ui] extra)."),
        ("tui", "Terminal control plane: start, watch and steer runs by chat."),
        ("inspect", "Show a run's best result, why it stopped, trust and comparability."),
        ("resume", "Continue a stopped or crashed run from its event log."),
        ("stop", "Stop a run without the end-of-run wrap-up; it stays resumable."),
        ("smoke", "Check that the configured LLM endpoint answers and can call tools."),
    )),
    ("Run control", (
        ("finalize", "Stop a run and write its end-of-run report, lessons and costs."),
        ("approve", "Approve what a paused run is waiting on (human in the loop)."),
        ("replay", "Rebuild a run's state from its event log and print it (read-only)."),
        ("upstream-auto", "Turn the automatic code-upstream switch on or off."),
        ("repair-log", "Repair a corrupted line in the middle of a run's event log."),
    )),
    ("External coding agent", (
        ("harness", "Print the external-agent capability contract as JSON."),
        ("harness-mcp", "Serve the UI's API to a coding agent over stdio MCP."),
    )),
    ("Export", (
        ("export-notebook", "Export the best solution as a runnable Jupyter notebook."),
        ("export-mlflow", "Log the best solution's params, metrics and code to MLflow."),
        ("export-bundle", "Package a run for a reviewer as an RO-Crate bundle."),
        ("export-git", "Export the experiment tree as a git repository, one commit per node."),
        ("export-sft", "Export the run's model turns as outcome-labelled SFT rows."),
        ("tensorboard", "Serve TensorBoard over the nodes' training logs."),
    )),
    ("Diagnostics (one run)", (
        ("timings", "Where the wall-clock time went, per node and per run."),
        ("tokens", "Where the model tokens went, by phase."),
        ("readmodel", "Rebuild or check a run's derived SQLite read model."),
        ("comparability", "Whether two runs' best scores may be ranked against each other."),
        ("repair-candidates", "Files the run's nodes repeatedly had to fix, ranked."),
        ("stage-dups", "Stage work duplicated across nodes."),
        ("parser-stats", "How the structured-output parser behaved, per role."),
        ("edit-types", "What kind of edit each experiment made and which paid off."),
        ("proxy-accuracy", "How accurate the pre-evaluation kill proxy was."),
        ("seed-distance", "How far each experiment moved from its seed program."),
        ("workspace-bytes", "Disk weight of the node workspaces versus what the log claims."),
        ("landlock-check", "Print and test the kernel read allow-list a run would get."),
        ("speculation-gate", "Run the scorer-fidelity and search-quality gates for speculation."),
    )),
    ("Maintenance", (
        ("build-ui", "Build the React UI bundle that `looplab ui` serves."),
        ("evaluate-track", "Run a declared evaluation track over finished nodes."),
        ("import-metrics", "Import metrics measured after the run, beside the live ones."),
        ("backfill-applied-params", "Record what parameters each node actually ran with."),
        ("backfill-score-metrics", "Recover score-stage metrics an older log did not keep."),
        ("memory-orphans", "Find (and with --apply remove) memory rows of deleted runs."),
        ("memory-fingerprints", "Find (and with --apply repair) lesson rows readers cannot see."),
        ("reap-service-files", "Find (and with --apply remove) leftovers of finished operations."),
    )),
    ("Cross-run memory and governance", (
        ("cross-run-index", "Build the portfolio index over every run."),
        ("cross-run-concepts", "Overview of concepts across runs."),
        ("cross-run-search", "Search the cross-run claims and concepts."),
        ("cross-run-digest", "Summarize cross-run concepts grouped by axis."),
        ("atlas", "Print the legacy Research Atlas data payload."),
        ("claims", "Project distilled lessons into evidence-labelled claims."),
        ("claim-decide", "Ratify, reject or pin one cross-run claim."),
        ("claim-steward", "Ask a model to propose decisions on claims (paid)."),
        ("concept-merge", "Merge one concept into another across runs."),
        ("concept-split", "Split a coarse concept into finer ones."),
        ("concept-ratify", "Ratify the concept steward's recorded merge proposals."),
        ("concept-steward", "Ask a model to propose a concept curation (paid)."),
        ("task-facets", "Ask a model to propose a task's facets (paid)."),
        ("task-facets-set", "Record a task's facets by hand."),
        ("prior-citations", "Whether the cross-run priors a run was shown reached its proposals."),
    )),
    ("Research instruments (maintainers)", (
        ("bench", "Run tasks end to end and report a capability benchmark."),
        ("concept-coverage", "Concept-graph coverage and uncovered regions of a run."),
        ("asset-brief", "Prior-art and available-assets brief for a task repo."),
        ("lock-in", "Detect runs of experiments stuck on one lever."),
        ("board-dedup", "Hypothesis-board redundancy analysis."),
        ("research-targets", "Important but uncovered research directions."),
        ("novelty-recall", "Near-duplicate proposals the novelty gate let through."),
        ("concept-authorship", "How much of each proposer's own concept survived classification."),
        ("lesson-guard", "Audit distilled lessons for over-generalization."),
        ("belief-key-split", "Where text and concept belief keys disagree across runs."),
        ("card-ladder", "The direction-to-experiment ladder over a runs root."),
        ("asha-rungs", "Whether runs produced rung curves ASHA could have used."),
        ("fidelity-agreement", "Whether cheap evaluations rank candidates like full ones."),
        ("harden", "Harden the reward-hack evaluator with a hacker-fixer-solver loop."),
        ("mlebench-extras", "MLE-bench rule-violation and plagiarism checks for a finished run."),
        ("bait-materialize", "Write the three BAIT tasks for the hack-rate benchmark."),
        ("bait-audit", "Score a BAIT-task run's nodes for taking the planted shortcut."),
    )),
)


def command_name(info) -> str:
    """The name Typer registers a command under — the same rule `looplab.cli` and the doc tests use."""
    return info.name or info.callback.__name__.replace("_", "-")


def apply_help_panels(app) -> None:
    """Order `app.registered_commands` by `HELP_PANELS` and set each command's panel and summary.

    Presentation only: no command is added, removed or renamed. A registered command with no row
    keeps its place after the table's commands and Typer's default panel, which the guard test
    refuses — so the fallback exists only so a missing row can never break `looplab` itself."""
    order = {}
    for panel, rows in HELP_PANELS:
        for name, summary in rows:
            order[name] = (len(order), panel, summary)
    commands = list(app.registered_commands)
    for info in commands:
        row = order.get(command_name(info))
        if row is not None:
            _, info.rich_help_panel, info.short_help = row
    commands.sort(key=lambda info: order.get(command_name(info), (len(order),))[0])
    app.registered_commands[:] = commands
