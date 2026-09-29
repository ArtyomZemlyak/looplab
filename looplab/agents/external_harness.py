"""Agent-facing node contract, derived from the actual candidate patch gate."""
from __future__ import annotations


def external_harness_brief(task, repo_spec: dict) -> str:
    """Explain optional phases using the same stage and surface precedence as evaluation."""
    from looplab.tools.patch import SurfacePolicy

    prefixes = [e["name"] for e in repo_spec["editables"] if e["name"] not in (".", "")]
    allowed = SurfacePolicy(repo_spec["edit_surface"], repo_spec["protected_names"],
                            prefixes).check("looplab_stages.json") is None
    declared = bool(getattr(getattr(task, "eval", None), "stages", None))
    if declared:
        stages = ("The operator already declared the evaluation stages. They are authoritative; "
                  "do not create a competing looplab_stages.json.")
    elif allowed:
        stages = ("You may, only if useful, write looplab_stages.json as "
                  '{"stages":[{"name":"train","command":["python","train.py"],'
                  '"timeout":14400,"check":true}]}. These are PRECEDING stages; LoopLab appends '
                  "the operator's scoring command. Inspect that command first: if it already "
                  "trains and scores, do not add a duplicate train stage. Do not put a score "
                  "stage in the manifest.")
    else:
        stages = ("looplab_stages.json is outside the allowed edit surface or protected. Do not "
                  "write it; implement against the operator's existing evaluation command.")
    return ("\n\nNODE BUILD CHOICES: Inspect the task and choose whether a plan helps. A small edit "
            "needs no separate plan. " + stages + " LoopLab executes evaluation and measures "
            "the result; never report a guessed or cached metric as this node's score.")


def external_agent_brief(task, repo_spec: dict | None, backend: str, developer) -> str:
    base = task.agent_brief() if repo_spec else getattr(developer, "brief", "")
    if repo_spec and backend in ("codex", "claude"):
        return base + external_harness_brief(task, repo_spec)
    return base
