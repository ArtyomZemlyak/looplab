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

It is also where the reader's VIEW is decided (WP-TOOLS T3, 2026-09-29): which tree a call reads —
the run's starting code, or the node the call works on (`researcher_repo_view_follows_node`) — and
the A5 workspace token that keeps a page read in one tree out of another tree's chain root.
`tests/test_researcher_reads_the_code_it_improves.py` drives both.

CLAIM[repo-reader-lives-outside-factory] It is a MODULE rather than a fourth function in
`factory.py` because that file sits at the ceiling `tests/test_agent_factory_split.py` pins — the
same reason `cli/memory_cmds.py` lives apart from `governance_cmds`. If that ceiling is ever raised,
this module's REASON to exist is gone even though the module still works, which is exactly the kind
of sentence rule 2 exists to catch. decided:`line:agents/factory.py&&520@tests/test_agent_factory_split.py`
"""
from __future__ import annotations

from typing import Optional


def repo_view_follows_node(settings) -> bool:
    """`Settings.researcher_repo_view_follows_node` — its ONE reader (CLAUDE.md: a flag that changes
    a prompt is read through one reader and is OFF wherever it is absent, so a settings stub and a
    run launched before the field keep the starting-code view)."""
    return bool(getattr(settings, "researcher_repo_view_follows_node", False))


def repo_reader_provider(task, settings=None, *, no_parent_view: str = "base",
                         beside=()) -> Optional[object]:
    """`RepoTools` over the task's editable repo(s) — read-only `repo_grep`/`repo_list`/`repo_read`
    — or None when this task has no source a role should be reading.

    The rule is `make_roles`' rule verbatim, in one expression instead of two copies: an editable
    repo, and NOT the `cli_overrides` param-search mode (there is no code to read there — an idea is
    an argv override, and the task's own baseline Developer stays in force).

    Deliberately NOT folded into `factory._shared_providers`: that list also serves the agentic
    Strategist and the unified pilot, and giving THEM a repo reader is a tool-surface change with no
    measurement behind it. This helper is called by the two sites that were always meant to have it.

    WHICH TREE THE READER SHOWS (WP-TOOLS T3). With `settings` switching
    `researcher_repo_view_follows_node` on, the reader follows the node the call works on — the
    parent a proposal improves — instead of always showing the run's starting code (see
    `tools/knowledge_tools.py::RepoTools`). `no_parent_view` is the view of a call with NO parent,
    chosen here at construction because `bind_state(state, None)` cannot tell the two callers
    apart: `"base"` (the starting code) for the Researcher, whose parentless proposal is a draft,
    `"best"` (the incumbent best) for deep research, which reviews the run. `beside` is the provider
    list the reader joins: the `RunTools` in it learn that `read_code` of a repo node — its code IS
    its recorded files, shown by name only — can point at `repo_read(node_id=N, path=…)`. Nothing
    else in that list is touched, and with the flag off nothing at all.
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
    follows = settings is not None and repo_view_follows_node(settings)
    reader = RepoTools(spec["editables"], follow_node=follows, no_parent_view=no_parent_view)
    if follows:
        from looplab.tools.run_tools import RunTools
        for provider in beside or ():
            if isinstance(provider, RunTools):
                provider.repo_read_node_view = True
    return reader


def bound_repo_view(tools) -> Optional[str]:
    """The bound view (`"base"` / `"node:<id>"`) of the node-following repo reader in `tools` — a
    bare provider or a composite, nested or not — or None when there is none (the flag is off)."""
    from looplab.tools.knowledge_tools import RepoTools
    pending = [tools]
    while pending:
        provider = pending.pop()
        if isinstance(provider, RepoTools):
            key = provider.bound_view_key()
            if key is not None:
                return key
        pending.extend(getattr(provider, "providers", None) or ())
    return None


def researcher_workspace_token(tools) -> Optional[str]:
    """The A5 workspace token of the tree the repo reader in `tools` is bound to, or None.

    `agents/established.py` carries a page into a later chain root only when it was read in that
    chain's workspace, and keys a page on `(tool, path)`. That was sound while the Researcher read
    one tree — the starting code — under one stable token; once the reader follows the node, a page
    read over parent 29 is not the file a propose over parent 7 reads. So the token names the VIEW:
    a propose and a deep-research pass over the same tree share its pages, and no other chain is
    handed them as "carried verbatim — do not re-fetch". None (the flag is off) keeps the
    historical token, which the caller spells."""
    view = bound_repo_view(tools)
    if view is None:
        return None
    # The prefix `established.py` renders a page's scope by — function-local, like the provider
    # imports above: this module stays free of the loop machinery `established` imports.
    from looplab.agents.established import VIEW_WORKSPACE_PREFIX
    return f"{VIEW_WORKSPACE_PREFIX}{view}"
