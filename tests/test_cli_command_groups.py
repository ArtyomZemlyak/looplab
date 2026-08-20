"""The CLI command groups stay split by what a command DOES to the world (doc 25 CT-01).

`inspect_cmds.py` had grown to 1701 lines and ~25 commands across three unrelated domains while its
docstring still named four, all "read-only". Two of those domains write: `concept-coverage --persist`
appends events to a run, and a third of the module makes durable cross-run governance writes and
paid LLM steward calls. A reader looking for "what does this command touch" had to read 1700 lines
to find out.

The split is only worth anything if it holds, so this file pins the BOUNDARY rather than the file
sizes: no module may host a command from another module's domain, and each group's docstring has to
keep saying what it mutates.

It also pins the concrete defect the split surfaced — two `settings.llm_parser` reads against a name
bound as `_settings`, i.e. a `NameError` on the agentic path that no offline test could reach.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from looplab.cli import concept_cmds, governance_cmds, inspect_cmds, memory_cmds

_CLI = Path(__file__).resolve().parents[1] / "looplab" / "cli"

# The domain each group owns, by the command names typer registers. Adding a command means adding it
# HERE, in the group whose contract it actually matches — which is the decision the old module let
# everyone skip.
GROUPS = {
    # `landlock-check` is a pure read of one run's own task snapshot plus a forked probe that
    # touches nothing — the same "fold/inspect a single run's sidecars" contract as `inspect`, and
    # the reason it is not in `governance_cmds` (it writes nothing and spends no money) or a new
    # group of its own (one command).
    # `readmodel` DOES write — the named run's own `readmodel.sqlite`. It belongs here and not in
    # `governance_cmds` because what it writes is a DERIVED sidecar of the single run it is pointed
    # at, rebuilt by the same `fold` `replay` prints: it appends no event, spends no money, touches
    # no cross-run store, and nothing in `looplab/` ever reads the file back. The group docstring
    # carries that exception explicitly rather than letting "read-only" quietly become false.
    "inspect_cmds": {"replay", "speculation-gate", "timings", "inspect", "readmodel", "tensorboard",
                     # `stage-dups` is run diagnostics in the strictest sense: it folds nothing but
                     # this run's own `stage_finished` rows, calls no model, writes nothing, and
                     # reads no cross-run store. It sits beside `timings` because it answers the
                     # sibling question — `timings` says where the wall clock went, this says how
                     # much of it produced bytes some other node had already produced.
                     # `parser-stats` is the same contract again, one sidecar over: it reads THIS
                     # run's `spans.jsonl`, tallies the `structured_parse` observations, writes
                     # nothing, calls no model and touches no cross-run store. It belongs beside
                     # `timings` and `stage-dups` because all three answer "what did this run
                     # actually do" from a sidecar the run wrote itself — here, which parser
                     # answered each structured ask, which is the evidence `Settings.llm_parser`
                     # has to be decided on.
                     # `comparability` is the ONE command here that takes MORE THAN ONE run, and
                     # it is still this group's contract rather than a cross-run governance one:
                     # every fact it prints is folded from each named run's OWN event log, it calls
                     # no model, writes nothing, and reads no cross-run store — the same three
                     # clauses that keep `landlock-check` and `stage-dups` here. What it adds is a
                     # REFUSAL over what it read (exit 3 on provably different comparability keys,
                     # 4 on unknown), and a refusal derived from two runs' own logs is a diagnostic,
                     # not a durable claim. It is not in `governance_cmds` for exactly the reason
                     # that group exists: it spends no money and authors no cross-run memory.
                     "landlock-check", "stage-dups", "parser-stats", "comparability"},
    "concept_cmds": {"concept-coverage", "asset-brief", "lock-in", "board-dedup",
                     "research-targets", "novelty-recall", "lesson-guard"},
    "governance_cmds": {"cross-run-concepts", "cross-run-index", "concept-merge", "concept-split",
                        "concept-steward", "concept-ratify", "claim-decide", "task-facets",
                        "task-facets-set", "claim-steward", "cross-run-digest", "cross-run-search",
                        "atlas", "claims"},
    # `memory_cmds` is its own group because the line ceiling below refused to let it be a fourth
    # domain inside `governance_cmds` — which was ALREADY eleven lines under the bound. Its contract
    # is the one governance does not have: every command there RECORDS a decision and adds, this one
    # REMOVES rows whose writing run is gone and decides nothing about their content.
    "memory_cmds": {"memory-orphans"},
}


def _registered(module_name: str) -> set[str]:
    """Command names a module registers, read from its SOURCE — the decorator argument, or the
    function name with underscores mapped the way typer does it."""
    tree = ast.parse((_CLI / f"{module_name}.py").read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if not (isinstance(decorator, ast.Call)
                    and ast.unparse(decorator.func) == "app.command"):
                continue
            explicit = next((kw.value.value for kw in decorator.keywords if kw.arg == "name"), None)
            names.add(explicit or node.name)
    return names


@pytest.mark.parametrize("module_name", sorted(GROUPS))
def test_each_group_registers_exactly_its_own_domain(module_name):
    assert _registered(module_name) == GROUPS[module_name], (
        f"{module_name} drifted from its declared domain; put the command where its contract lives")


def test_the_groups_partition_the_commands_with_no_overlap():
    """Belt and braces on the table above: a command listed twice would satisfy both per-group
    assertions while making the boundary meaningless."""
    seen: dict[str, str] = {}
    for module_name, commands in GROUPS.items():
        for command in commands:
            assert command not in seen, f"{command} claimed by both {seen[command]} and {module_name}"
            seen[command] = module_name


def test_every_group_still_reaches_the_typer_app():
    """The split is only safe if importing the package still registers all 25 commands — a group
    left out of `looplab/cli/__init__`'s import block would vanish from the CLI silently."""
    from looplab.cli import app

    registered = {command.name or command.callback.__name__
                  for command in app.registered_commands}
    for commands in GROUPS.values():
        assert commands <= registered, f"missing from the live app: {sorted(commands - registered)}"


def test_each_group_docstring_says_what_it_mutates():
    """The original module's docstring said "read-only inspection" above a third of a file that made
    durable writes and spent money. Each header now has to carry its own answer."""
    assert "Read-only EXCEPT" in inspect_cmds.__doc__
    assert "--persist" in concept_cmds.__doc__ and "appending" in concept_cmds.__doc__
    for phrase in ("DURABLE WRITES", "PAID LLM STEWARDS", "READ-ONLY"):
        assert phrase in governance_cmds.__doc__, f"{phrase} missing from the governance header"


def test_no_group_is_a_god_module_again():
    """The finding's own measure. Not a style rule — 1701 lines is how three domains hid in one."""
    for module_name in GROUPS:
        lines = len((_CLI / f"{module_name}.py").read_text(encoding="utf-8").splitlines())
        assert lines < 1100, f"{module_name} is back to {lines} lines"


# ------------------------------------------------------- the defect the split surfaced

@pytest.mark.parametrize("command", ["_concept_map_for", "concept_coverage"])
def test_the_agentic_branch_reads_the_settings_it_actually_bound(command):
    """`settings, client = _optional_client(...)` then `parser=settings.llm_parser`.

    Both sites bound the pair to `_settings` — the underscore that means "deliberately unused" — and
    then read plain `settings` a few lines down inside the `client is not None` branch. There is no
    module-level `settings`, so the AGENTIC path (the DEFAULT for these commands) raised
    `NameError: name 'settings' is not defined` the moment an endpoint was actually reachable. Every
    offline test takes the `client is None` branch, so nothing went red for two releases.

    Pinned as a name-resolution property rather than by running the LLM path: the function must not
    read any free variable that its own body never binds.
    """
    # `symtable` is the interpreter's OWN scope analysis, so this is exactly the resolution the
    # bytecode will perform — not a hand-rolled approximation that would miss comprehension scopes,
    # walrus targets or closure cells and go quietly green.
    import builtins
    import symtable

    source = (_CLI / "concept_cmds.py").read_text(encoding="utf-8")
    top = symtable.symtable(source, "concept_cmds.py", "exec")

    def _find(table, name):
        for child in table.get_children():
            if child.get_name() == name:
                return child
            found = _find(child, name)
            if found is not None:
                return found
        return None

    table = _find(top, command)
    assert table is not None, f"{command} not found in the module symbol table"

    resolvable = set(vars(concept_cmds)) | set(vars(builtins))
    escaping = sorted(symbol.get_name() for symbol in table.get_symbols()
                      if symbol.is_global() and symbol.get_name() not in resolvable)
    assert escaping == [], (
        f"{command} resolves {escaping} as a module global, and the module has no such name — a "
        "NameError waiting for the branch that reaches it")


def test_no_diagnostic_discards_the_settings_it_then_uses():
    """The narrow grep form of the same thing, so a future copy of the block is caught at review
    time rather than by the AST walk above: a site that underscores the binding must not name it."""
    source = (_CLI / "concept_cmds.py").read_text(encoding="utf-8")
    for chunk in source.split("_settings, client = _optional_client(")[1:]:
        # everything up to the next blank-line-separated top-level statement is that call's branch
        branch = chunk.split("\ndef ")[0]
        assert "settings.llm_parser" not in branch, (
            "a site bound `_settings` and then read `settings` — the exact NameError CT-01 found")
