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

from looplab.cli import (concept_cmds, corpus_cmds, governance_cmds, inspect_cmds,
                         maintenance_cmds, memory_cmds)

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
                     # `tokens` and `repair-candidates` are the same contract once more, and
                     # both landed WITHOUT being added here — this guard was red on master
                     # for three days, which is the drift it exists to catch. `tokens` folds
                     # this run's own `llm_usage` ledger and reads its own `spans.jsonl`;
                     # `repair-candidates` reads this run's own repair ledger and RANKS,
                     # deciding nothing. Neither calls a model, writes anything, or touches a
                     # cross-run store — the three clauses that keep the rest of this set here.
                     # `edit-types` (doc 52 row 31) is the same contract once more: it folds THIS
                     # run's own log, classifies each node's committed diff against its first
                     # parent with a regex pass, calls no model, writes nothing and reads no
                     # cross-run store. It belongs beside `stage-dups` because both answer "what
                     # did this run actually do" over the run's own record — here, what KIND of
                     # edit each experiment made and how much of it the lineage had already tried.
                     "landlock-check", "stage-dups", "parser-stats", "comparability",
                     # `proxy-accuracy` (doc 52 row 31) is the same again: it folds this run's own
                     # `proxy_scored` rows against the metrics that came back and prints one
                     # number. It writes nothing, spends nothing, and reads no cross-run store —
                     # and it is the number the `proxy_skipped` KILL should be armed on.
                     # `seed-distance` (doc 52 row 31, the last of that row) is `edit-types`'
                     # sibling and shares its three clauses exactly: one fold of THIS run's log, one
                     # regex pass over the same closed edit vocabulary, no model, no write, no
                     # cross-run store. `edit-types` measures each STEP; this measures the whole
                     # walk against the lineage root, which is the question a per-step tally cannot
                     # answer — and nothing in the loop reads either.
                     # `workspace-bytes` (doc 37 §8's R1) is this group's contract with the
                     # subject read off the DISK instead of a sidecar, and that is the same
                     # question rather than a new domain: it is one run's account of ITSELF — what
                     # its own node workspaces weigh, printed beside the only sentence its own log
                     # ever made about them (`workspace_seeded`'s file counts). It calls no model,
                     # writes nothing and reads no cross-run store — the three clauses that keep
                     # `stage-dups`, `landlock-check` and the rest here — so it is not `audit_cmds`
                     # (which may spend money and writes a sidecar), not `maintenance_cmds` (which
                     # appends events) and not `memory_cmds` (the cross-run stores).
                     "tokens", "repair-candidates", "edit-types", "proxy-accuracy",
                     "seed-distance", "workspace-bytes"},
    # `concept-authorship` is this domain's READ side, on `prior-citations`' ground: a pure
    # projection over the fold that compares what each proposer AUTHORED as its node's concepts
    # against the membership the classifier left, calling no model and writing nothing. It is here
    # rather than in `inspect_cmds` because the subject is the concept taxonomy — the same record
    # `concept-coverage` builds and `--persist` writes — and not one run's account of itself.
    "concept_cmds": {"concept-coverage", "asset-brief", "lock-in", "board-dedup",
                     "research-targets", "novelty-recall", "lesson-guard", "concept-authorship"},
    "governance_cmds": {"cross-run-concepts", "cross-run-index", "concept-merge", "concept-split",
                        "concept-steward", "concept-ratify", "claim-decide", "task-facets",
                        "task-facets-set", "claim-steward", "cross-run-digest", "cross-run-search",
                        "atlas", "claims"},
    # `audit_cmds` is the post-run INSTRUMENT group (doc 52 row 22): a command here reads ONE
    # finished run, may spend money on a judge, and writes a sidecar of that run — a RECORD that
    # moves no champion, metric, selection or cross-run store. That is neither `inspect_cmds`
    # (which never spends money) nor `governance_cmds` (which authors cross-run memory and was
    # already at its ceiling), so it is a domain split like `memory_cmds`, not a drift.
    "audit_cmds": {"mlebench-extras", "bait-materialize", "bait-audit"},
    # `memory_cmds` is its own group because the line ceiling below refused to let it be a fourth
    # domain inside `governance_cmds` — which was ALREADY eleven lines under the bound. Its contract
    # is the one governance does not have: every command there RECORDS a decision and adds, this one
    # REMOVES rows whose writing run is gone and decides nothing about their content.
    # `prior-citations` (doc 52 row 17) is the READ side of the same stores: a pure projection over
    # one run's `prior_injected` + `memory_read` rows that reports which pushed lessons its
    # proposals cited — the number the utility rank term and the forgetting rung are keyed on. It
    # writes nothing and calls no model; it is here and not in `inspect_cmds` because that group
    # sits at its own ceiling and the subject is the cross-run store's usefulness, not one run's
    # account of itself.
    "memory_cmds": {"memory-orphans", "prior-citations"},
    # CORPUS INSTRUMENTS. Its own group for the same two reasons `memory_cmds` and `audit_cmds` are,
    # and they point the same way. The DOMAIN first: every command here takes a runs ROOT, folds
    # each run's own event log and reports ONE reading a `docs/BACKLOG.md` marker named as the
    # precondition for a decision it refuses to take on faith — the belief-key disagreement the
    # concept key would merge, the undercut rule's stated trigger, whether ASHA ever had a curve to
    # halve. None of them makes that decision, calls a model, writes a file, appends an event or
    # reads a cross-run store; what distinguishes them from `inspect_cmds` (`comparability`
    # included) is that the subject is the CORPUS rather than any named run's account of itself.
    # THE CEILING SECOND, and it is not the reason but it agrees with it: the two groups whose
    # subject is nearest — `inspect_cmds` (1194 lines against a 1200 cap) and `governance_cmds`
    # (1092 against 1100) — are both at the bound below, whose own stated norm is that an overrun is
    # answered by an extraction or a new home and never by a raise.
    "corpus_cmds": {"belief-key-split", "card-ladder", "asha-rungs"},
    # OFFLINE RECORD REPAIRS. Its own group rather than `governance_cmds` because the subject is a
    # SINGLE run's account of itself — a node whose durable record kept the proposal and lost what
    # actually ran — not the cross-run store. It appends events, so it is not `inspect_cmds` either;
    # the group docstring states that in its first paragraph rather than below a "read-only" claim.
    # `backfill-score-metrics` is the same contract read the other way round: one run's account of
    # itself again, this time a node whose record kept ONE number while its own preserved score.log
    # holds the 36 the stage measured. Same append-only shape, same "a live record always wins"
    # fold rule, same refusal to guess — so it belongs beside its sibling and not in a group of its
    # own.
    "maintenance_cmds": {"backfill-applied-params", "backfill-score-metrics"},
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
    # The corpus group's whole claim is that it touches NOTHING — an instrument that quietly grew a
    # write would be the same drift as a "read-only" module that spent money, so its header states
    # the four things it does not do and this pins them.
    for phrase in ("read-only", "calls a model", "writes a file", "appends an event",
                   "cross-run store"):
        assert phrase in corpus_cmds.__doc__, f"{phrase} missing from the corpus header"


def test_no_group_is_a_god_module_again():
    """The finding's own measure. Not a style rule — 1701 lines is how three domains hid in one.

    `inspect_cmds` gets its OWN cap and the other groups keep 1100 — master's shape, kept, because
    this branch's single global `< 1163` bought every other group 63 lines of slack it had not
    argued for. The NUMBER is master's 1250 re-derived against the merged file rather than carried:
    both sides raised it on their own copy (this branch 1100 -> 1163 against a 1162-line file,
    master 1100 -> 1250 against a 1197-line one), so neither literal was measured on the tree it
    now guards.

    HOW IT GOT HERE, both halves being real work in-domain: `looplab tokens` grew its per-card and
    per-build tables (1f49adfb 1053 -> 1124, ad374925 -> 1148 — this guard was RED on master for a
    day, which is the drift it exists to catch), then ec60fed2's `occupancy` command and the
    2026-08-29 review annotations took master to 1197; this branch's own additions bring the merged
    file to 1224. That is run diagnostics doing its job, not a second domain moving in.

    THE MERGE OVERRAN IT AND THE ANSWER WAS THE EXTRACTION, not a fourth raise. The merged file
    measured 1237 against a cap of 1225, and the paragraph this one replaces had already named the
    unit to move: the `tokens` command's rendering half — the per-card and per-build table echoes —
    is now `cli/token_report.py::echo_card_and_build_tables`, beside `events/token_spend.py`'s pure
    folds it renders. That took the file to 1183 and the cap DOWN to 1200 rather than up. Banking
    the slack instead would have been a cap that stopped being consulted, the exact trade
    `test_agent_factory_split.py` refuses next door — and the cap coming down with the extraction is
    what keeps the next overrun a real question rather than a formality.

    AND AGAIN, WHICH IS THE POINT: `workspace-bytes` (doc 37 §8's R1) arrived on 2026-09-08 against
    a file with six lines of headroom, and the answer was the second extraction rather than the
    first raise — `edit-types`' body moved VERBATIM to `run_report.py::echo_edit_types`, beside the
    rendering already there, and the new command's own walk lives in `cli/workspace_bytes.py`. So
    `inspect_cmds.py` keeps the contract (the decorator, the signature, the docstring the CLI
    reference is written against) and none of the arithmetic: 1194 -> 1154 lines, and the cap comes
    down with it a second time, to 1175.
    """
    caps = {"inspect_cmds": 1175}
    for module_name in GROUPS:
        lines = len((_CLI / f"{module_name}.py").read_text(encoding="utf-8").splitlines())
        assert lines < caps.get(module_name, 1100), f"{module_name} is back to {lines} lines"


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
