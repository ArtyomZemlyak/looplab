"""The ONE rule for "may this read-only role open the task's source?".

It lived inline in `factory.py::make_roles` and nowhere else, and `_shared_providers`' own docstring
said "each site appends its own extras (RepoTools / WebTools) after" as though every site did.
`make_deep_researcher` did not, and nothing connected the two — so the phase that MINTS a cold-start
run's first hypotheses was handed a toolset of run/knowledge/memory stores that are all empty by
construction on a cold start, with no way to open the file its own goal told it to read.

Measured over the `runs-armb` campaign (2026-08-20), 20 AlgoTune repo tasks:

    deep-research tool calls                        336
    …that opened a workspace file                     0
    …whose answer was empty by construction      193 (57%)
    tools published by `answered_by_context`       33, EVERY ONE AT ZERO
    memos stating no tool here can read the source   57 of 82 (70%); 17 of 20 tasks said it FIRST memo
    `repo_read` calls `propose` then paid           968 (33% of all its tool calls)

The memo is spliced into every later `propose` prompt, so an ungrounded one is re-paid on every turn
of the phase that costs the most. `tests/test_deep_research_repo_reader.py` drives the property.

CLAIM[repo-reader-lives-outside-factory] It is a MODULE rather than a fourth function in
`factory.py` because that file sits at the ceiling `tests/test_agent_factory_split.py` pins — the
same reason `cli/memory_cmds.py` lives apart from `governance_cmds`. If that ceiling is ever raised,
this module's REASON to exist is gone even though the module still works, which is exactly the kind
of sentence rule 2 exists to catch. decided:`line:agents/factory.py&&520@tests/test_agent_factory_split.py`
"""
from __future__ import annotations

from typing import Optional


def repo_reader_provider(task) -> Optional[object]:
    """`RepoTools` over the task's editable repo(s) — read-only `repo_grep`/`repo_list`/`repo_read`
    — or None when this task has no source a role should be reading.

    The rule is `make_roles`' rule verbatim, in one expression instead of two copies: an editable
    repo, and NOT the `cli_overrides` param-search mode (there is no code to read there — an idea is
    an argv override, and the task's own baseline Developer stays in force).

    Deliberately NOT folded into `factory._shared_providers`: that list also serves the agentic
    Strategist and the unified pilot, and giving THEM a repo reader is a tool-surface change with no
    measurement behind it. This helper is called by the two sites that were always meant to have it.
    """
    repo_spec = getattr(task, "repo_spec", None)
    if not callable(repo_spec) or bool(getattr(task, "params", None)):
        return None
    spec = repo_spec()
    if not (spec and spec.get("editables")):
        return None
    # Function-local, like every other provider import in `factory.py`: `agents` must not grow a
    # module-level dependency that widens the import graph at startup.
    from looplab.tools.knowledge_tools import RepoTools
    return RepoTools(spec["editables"])
