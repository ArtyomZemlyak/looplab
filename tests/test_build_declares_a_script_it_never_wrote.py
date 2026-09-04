"""A stage whose entry point the build never wrote costs a node two repairs and then the node.

MEASURED ON v13, which lost two of four nodes to exactly this:
    node 0  `mine`                exit 1 in 0.505 s  "No module named vectorsearch.mine_stage"
    node 2  `teacher_embeddings`  exit 1 in 0.209 s  "can't open file '.../teacher_embeddings.py'"
Each then bought TWO repair sessions of ~30 minutes — all four `inert`, all four `changed: []`,
`budget_exhausted: time` — and died. The stage cost half a second; the CASCADE cost ~2 h per node.

I REJECTED THIS CHECK TWO CYCLES AGO with "it saves 0.5 s against a 47-minute cost". That reasoned
about the stage's own duration and was wrong: the sub-second failure is what TRIGGERS the cascade.

THREE SITES WERE ELIMINATED BY THE TREE'S OWN REASONING before this one:
  * submit time — `repo_task.eval_entrypoint_unprotected` is DELIBERATELY silent on a resolvable
    but absent entrypoint, because "the Developer AUTHORS the eval entrypoint" is the designed flow;
  * the `declare_stages` tool — the stages phase runs with READ-ONLY tools, so the script cannot
    exist yet when the manifest is declared;
  * the stage runner — too late: the node exists and the repair cascade is the cost.
Only the IMPLEMENT emit has both the manifest and the final file ledger.
"""
from __future__ import annotations

import json

from looplab.adapters.repo_task import entrypoint_candidates
from looplab.engine.repair_verify import build_declared_script_never_written


def _manifest(*commands):
    return json.dumps({"stages": [{"name": "s%d" % i, "command": c}
                                  for i, c in enumerate(commands)]})


def test_the_v13_case_is_refused():
    out = build_declared_script_never_written(
        _manifest(["python", "teacher_embeddings.py"]), {"train.py": "x"})
    assert "teacher_embeddings.py" in out
    assert "never wrote" in out


def test_a_script_the_session_DID_write_is_left_alone():
    assert build_declared_script_never_written(
        _manifest(["python", "teacher_embeddings.py"]),
        {"teacher_embeddings.py": "print(1)\n"}) == ""


def test_the_module_form_is_NEVER_refused():
    """`python -m vectorsearch.mine_stage` and `python -m pytest` are IDENTICAL to the resolver —
    two candidates each, neither local — because that is how INSTALLED code looks. Refusing here
    would reject every legitimate installed-module stage, which is why `eval_stages` already treats
    the form as opaque. v13 node 0 is therefore NOT caught, and that is the correct trade."""
    for mod in ("vectorsearch.mine_stage", "pytest", "torch.distributed.run"):
        assert build_declared_script_never_written(
            _manifest(["python", "-m", mod]), {}) == ""


def test_the_contract_this_rule_leans_on_is_asserted_here():
    """`len(cands) == 1` IS the script form, per `entrypoint_candidates`' documented contract: it
    returns BOTH `-m` spellings and exactly one path for a script. If that contract ever changes,
    this rule silently widens — so the contract is pinned where the rule can see it."""
    assert len(entrypoint_candidates(["python", "-m", "pkg.mod"])) == 2
    assert len(entrypoint_candidates(["python", "score.py"])) == 1


def test_an_opaque_command_is_left_alone():
    """A shell wrapper, a bare binary, `python -c`, a launcher whose flag grammar decides which
    token is the script — the resolver answers [] and this rule must not invent a target."""
    for cmd in (["bash", "run.sh"], ["python", "-c", "print(1)"],
                ["torchrun", "--nproc_per_node", "2", "score.py"], ["./scorer"]):
        assert build_declared_script_never_written(_manifest(cmd), {}) == ""


def test_several_missing_scripts_are_all_named_but_bounded():
    cmds = [["python", "s%d.py" % i] for i in range(9)]
    out = build_declared_script_never_written(_manifest(*cmds), {})
    assert "s0.py" in out and "s5.py" in out
    assert "s6.py" not in out, "the message names at most six"


def test_a_malformed_manifest_answers_rather_than_raising():
    """It runs inside an emit path that has already cost minutes; a broken manifest is a different
    rung's problem and must not become an exception here."""
    for bad in ("", "not json", "[]", json.dumps({"stages": "nope"}),
                json.dumps({"stages": [None, 7, {"command": None}]})):
        assert build_declared_script_never_written(bad, {}) == ""


def test_the_bounce_names_the_cost_so_the_model_can_act():
    out = build_declared_script_never_written(_manifest(["python", "a.py"]), {})
    assert "before emitting" in out
    assert "repair" in out


def test_THE_BUILD_PATH_ACTUALLY_CALLS_IT():
    """A rule with no reader records nothing — the exact trap this repo already carries one of
    (`OPEN[researcher-questions-not-appended]`: a carrier shipped and no engine path reads it).

    The implement emit's validator is a closure inside `_run`, so this drives the SOURCE: the
    `not error` branch must reach the rule, spend the one shot, and RETURN the refusal. A mutation
    that neuters the branch (`if not error:` -> `if False:`) leaves the rule perfectly tested and
    completely dead, and that mutant survived until this test existed."""
    import inspect

    from looplab.adapters import repo_developer
    src = inspect.getsource(repo_developer)
    branch = src.index("if not error:")
    call = src.index("build_declared_script_never_written(", branch)
    spend = src.index("_bounced.append(True)", branch)
    ret = src.index("return build_refusal", branch)
    assert branch < call < spend < ret, (
        "the build branch must call the rule, spend its one shot, then return the refusal")
    # and the SECOND argument must be the write LEDGER itself, never anything the model said about
    # itself and never an empty stand-in. `"write.files" in window` was the first spelling of this
    # assertion and a mutant passing `{}` for the ledger SURVIVED it — the manifest argument already
    # contains that substring, so the check never looked at the argument it was about.
    window = src[call:call + 260]
    assert ", write.files)" in window, "the ledger must be the second argument, not a stand-in"


def test_the_one_shot_is_SHARED_with_the_repair_rung():
    """`_bounced` is one list for both paths on purpose: a session gets ONE bounce, whichever rung
    fires. Two independent shots would spend the session arguing instead of editing — the reason
    the repair rung states for being one-shot in the first place."""
    import inspect

    from looplab.adapters import repo_developer
    src = inspect.getsource(repo_developer)
    assert src.count("_bounced = []") == 1, "one shot per session, not one per rung"
    assert src.count("_bounced.append(True)") == 2, "both rungs spend the SAME shot"
