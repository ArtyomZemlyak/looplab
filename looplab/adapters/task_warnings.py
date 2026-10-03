"""Advisory submit warnings shared by the CLI and server launch funnel."""

def submit_warnings(adapter) -> tuple[str, ...]:
    """Everything a VALIDATED task earns at submit that must NOT stop the launch.

    ONE rule, because a submit-time warning is worthless on the surface that does not print it. The
    two below were spelled twice — `serve/launch.py::preflight_start` (which puts them on
    `LaunchPreflight.warnings`, so `/api/start/preflight` and `/api/validate` return them) and
    `cli/run_cmds.py::_report_task_warnings` (which echoes them to stderr) — and the CLI's copy was
    written by hand from the server's, which is how a warning gets added on one surface only
    (doc 27, `three-new-run-planners-no-shared-schema`).

    Both are about the eval `command`'s argv, both are advisory by design, and both are TOTAL over an
    injected dict `adapter`: the helpers isinstance-check a RepoTask and return [] otherwise.

      * `eval_entrypoint_unprotected` — the scorer LoopLab cannot protect. The Developer's prompt
        tells it the scoring cannot be rewritten; when the argv names no in-repo file, nothing
        enforces that, and the same gap cost `runs/rubertlite-dr-unified-v6` 2x GPU per node before
        anyone looked.
      * `eval_source_tree_command_paths` — docs/29 F1c's third piece: an argv token naming the
        editable SOURCE tree absolutely reaches the operator's original rather than the node's copy,
        so no node's edits to it ever take effect.

    What is NOT here is the missing-input-path warning: the launch funnel FAILS CLOSED on a task path
    it cannot stat (`serve/launch.py::_validated_path_fingerprints`), so on that surface the same
    condition is a refusal, not a warning. A rule that means two different things is not one rule.
    """
    from looplab.adapters.repo_task import (eval_entrypoint_unprotected,
                                            eval_source_tree_command_paths)
    return (*eval_entrypoint_unprotected(adapter), *eval_source_tree_command_paths(adapter))
