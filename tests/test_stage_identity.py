"""STAGE IDENTITY — the reuse key and the output identity, driven end to end.

WHAT THESE TESTS ARE CALIBRATED AGAINST, because none of them is a hypothetical. The request was
"`mine` recomputes per node even when its configuration is byte-identical to a sibling's; key the
reuse on the node's DECLARED mining parameters". Re-measured over `runs/` on 2026-08-17:

    34 `mine` runs, 18.03 h;  17 ok at a mean of 46.7 min;  7 `reused`, every one WITHIN one node
    the DECLARED-params key: 7 hits, **4 WRONG** (v8 nodes 3/4/8 all declare
      `{mining_type: 1, n_negatives: 2}` and mined `13db4477…` / `34e9ca5e…` / `c665f57b…`)
    this CONTENT key:        1 hit,  0 wrong,  0.64 h

So the properties below are not "the cache works": they are the three the request asked to be
proved, and the point of proving them on a key that pays almost nothing is that a key which paid
more would have to be looser, and looser is measurably 57 % wrong here.

Tier 1 throughout (CLAUDE.md's guard-test ladder): a REAL `run_command_eval` over a real workdir with
real stage scripts, and the recorded rows read back. Nothing here pins a source substring.
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

from looplab.engine.eval_stages import EvalStagesMixin
from looplab.runtime.command_eval import (STAGE_INPUT_KEY, STAGE_KEY_REASON, STAGE_OUTPUTS_KEY,
                                          run_command_eval)
from looplab.runtime.stage_identity import (KEY_REASONS, REUSE_REFUSALS, declared_outputs,
                                            reuse_refusal, stage_input_key, workdir_content)

_M = {"kind": "stdout_json", "key": "metric"}

# A miner that reads a config, writes a deterministic artifact, and prints the metric. Deterministic
# on purpose: the whole question is whether two workdirs that key the same really produce the same
# bytes, and a nondeterministic producer could not answer it either way.
_MINER = (
    "import json, pathlib\n"
    "cfg = json.load(open('config.json'))\n"
    "pathlib.Path('negatives.txt').write_text('n=%d' % cfg['n_negatives'])\n"
    "print(json.dumps({'metric': 1.0}))\n"
)


def _stage_key(workdir, stages, index=0, scope="scope-A", cwd=None):
    reachable = EvalStagesMixin._stage_reachable_files(stages[:index + 1], workdir)
    return stage_input_key(stages, index, workdir, scope=scope, reachable=reachable, cwd=cwd)


def _make_workdir(tmp_path, name, *, n_negatives=2, miner=_MINER, extra=None):
    wd = tmp_path / name
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "mine.py").write_text(miner)
    (wd / "config.json").write_text(json.dumps({"n_negatives": n_negatives}))
    for rel, text in (extra or {}).items():
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return wd


def _stages():
    return [{"name": "mine", "command": [sys.executable, "mine.py"], "timeout": 60.0,
             "expect": {"files": ["negatives.txt"]}}]


def _run(wd, stages=None, **kw):
    stages = stages or _stages()

    def _key_fn(stgs, i):
        return _stage_key(str(wd), stgs, i, **kw)
    return run_command_eval([sys.executable, "mine.py"], str(wd), 60.0, _M,
                            stages=stages, stage_key_fn=_key_fn)


# --------------------------------------------------------------------------------------------
# THE KEY: identical inputs agree, ANY component disagrees
# --------------------------------------------------------------------------------------------

def test_two_workdirs_with_identical_inputs_mint_the_same_key_and_produce_the_same_bytes(tmp_path):
    """The hit case, and it is proved BOTH ways in one test on purpose. A key that agreed while the
    artifacts differed is the wrong hit this whole design is bounded by, so the assertion is not just
    `k_a == k_b` — it is that the two stages, run for real, wrote byte-identical output."""
    a, b = _make_workdir(tmp_path, "a"), _make_workdir(tmp_path, "b")
    ka, ra = _stage_key(str(a), _stages())
    kb, _ = _stage_key(str(b), _stages())
    assert ra == "" and ka and ka == kb
    _run(a), _run(b)
    assert (a / "negatives.txt").read_bytes() == (b / "negatives.txt").read_bytes()


# The same miner with a workdir-local import, so the closure has something real in it. Used by the
# tests that are about WHICH `.py` files the key covers.
_MINER_WITH_IMPORT = "import helper\n" + _MINER


@pytest.mark.parametrize("mutate,label", [
    (lambda wd: (wd / "config.json").write_text(json.dumps({"n_negatives": 4})),
     "a NON-.py file the stage reads — the blind spot `_safe_reuse_start` fails closed on, and the "
     "one the import closure structurally cannot see"),
    (lambda wd: (wd / "mine.py").write_text(_MINER_WITH_IMPORT + "# a comment\n"),
     "the entry script itself"),
    (lambda wd: (wd / "helper.py").write_text("X = 2\n"),
     "a module the entry script really IMPORTS, i.e. a member of the closure"),
    (lambda wd: (wd / "helper.py").unlink(),
     "a DELETED closure member — recorded as `absent` rather than dropped, so a vanished module "
     "moves the key instead of shrinking the set it is taken over"),
    (lambda wd: (wd / "unrelated.json").write_text('{"unread": true}'),
     "a NON-.py file NOTHING declares and nothing is known to read — in the key because 'not "
     "declared' means 'undeclared', never 'unread'"),
])
def test_any_change_to_a_keyed_input_changes_the_key(tmp_path, mutate, label):
    """A node whose key differs in ANY component recomputes. Parametrized over the five shapes that
    matter, and the FIRST one is the reason the key carries every non-`.py` file verbatim rather than
    the import closure alone: `config.json` never appears in any import, and on the real corpus that
    is exactly the file (`vectorsearch/configs/config.yaml`) that separates nodes whose closures
    agree."""
    kw = dict(miner=_MINER_WITH_IMPORT, extra={"helper.py": "X = 1\n", "unrelated.json": "{}"})
    a = _make_workdir(tmp_path, "a", **kw)
    b = _make_workdir(tmp_path, "b", **kw)
    stages = _stages()
    assert EvalStagesMixin._stage_reachable_files(stages, str(a)) == {"mine.py", "helper.py"}, (
        "the closure must really hold both or the import cases below prove nothing")
    before_a, _ = _stage_key(str(a), stages)
    before_b, _ = _stage_key(str(b), stages)
    assert before_a == before_b, "the fixtures must start equal or the test proves nothing"
    mutate(b)
    after_b, _ = _stage_key(str(b), stages)
    assert after_b != before_b, label


def test_a_py_file_OUTSIDE_the_import_closure_is_deliberately_not_in_the_key(tmp_path):
    """THE ONE PLACE THIS KEY RESTS ON THE CLOSURE'S OWN REACH, stated rather than discovered later.

    `.py` files are keyed by the transitive import closure of the entry script — the same bound
    `_safe_reuse_start` already trusts, used in the same direction — so a `.py` the stage cannot
    import does not move the key. A dynamic `importlib`/`exec`/`runpy` load escapes that, which is
    the same exposure `_safe_reuse_start` has, with the same consequence.

    The alternative was DRIVEN, not assumed: digesting every `.py` in the workdir takes the corpus
    yield from one correct hit to zero, because v8 nodes 3 and 10 mined a byte-identical parquet and
    differ only in `train.py` / `loss.py` / `collator.py` / `samplers.py` — none of which a `mine`
    stage imports. Being stricter there is not free; it costs the instrument its ability to tell
    "no duplication" from "the key is too tight"."""
    wd = _make_workdir(tmp_path, "a", extra={"train.py": "EPOCHS = 15\n"})
    assert "train.py" not in EvalStagesMixin._stage_reachable_files(_stages(), str(wd))
    before, _ = _stage_key(str(wd), _stages())
    (wd / "train.py").write_text("EPOCHS = 8\n")
    assert _stage_key(str(wd), _stages())[0] == before
    # …while the whole-workdir map it is derived from DOES see it, so the exclusion is a decision
    # made in the key and not an accident of what the walk reached.
    assert "train.py" in workdir_content(str(wd), exclude=set())[0]


def test_the_declared_argv_and_the_declared_contract_are_both_in_the_key(tmp_path):
    """Two components that are NOT files: the stage's resolved argv and its `expect`.

    `expect.assert` is in the key for a reason that is easy to get wrong — a reuse skips the SUCCESS
    CONTRACT along with the command, so a node declaring a different success condition inheriting a
    sibling's pass would be a stage held to a contract nobody checked, which is the exact defect
    `expect` was built to end (`runs/rubert-dr-0807`'s 1.2 %-coverage `mine`)."""
    wd = _make_workdir(tmp_path, "a")
    base, _ = _stage_key(str(wd), _stages())
    argv_changed = [dict(_stages()[0], command=[sys.executable, "mine.py", "--flag"])]
    assert _stage_key(str(wd), argv_changed)[0] != base
    assert_changed = [dict(_stages()[0],
                           expect={"files": ["negatives.txt"], "assert": "coverage over 90%"})]
    assert _stage_key(str(wd), assert_changed)[0] != base
    # …and the TIMEOUT is deliberately out: it bounds how long the stage may run, never what it
    # computes, and a stage that hit it never completes and so can never be a reuse source.
    assert _stage_key(str(wd), [dict(_stages()[0], timeout=9999.0)])[0] == base


def test_the_scope_binds_a_key_to_one_run_and_two_runs_never_share_one(tmp_path):
    """Everything OUTSIDE the workdir is uncovered — the dataset mount, the model cache,
    site-packages, the interpreter — so a key that could match across runs would assert a
    comparability this derivation cannot support. `scope` is what makes that impossible."""
    wd = _make_workdir(tmp_path, "a")
    assert _stage_key(str(wd), _stages(), scope="run-A")[0] != \
        _stage_key(str(wd), _stages(), scope="run-B")[0]


# --------------------------------------------------------------------------------------------
# FAIL-CLOSED: no key at all, for each documented reason
# --------------------------------------------------------------------------------------------

def test_an_entry_point_naming_installed_code_yields_no_key_and_therefore_can_never_hit(tmp_path):
    """`_safe_reuse_start`'s opacity clause, held identically. `python -m pip` / `-m pytest` /
    `-m torch.distributed.run` name code this key does not cover AT ALL — everything outside the
    workdir is the stated residual — so the whole-workdir content map proves nothing about what such
    a stage computes."""
    wd = _make_workdir(tmp_path, "a")
    opaque = [{"name": "mine", "command": ["python", "-m", "pip"], "timeout": 60.0}]
    key, reason = _stage_key(str(wd), opaque)
    assert key is None and reason == "opaque_entry" and reason in KEY_REASONS


def test_a_shell_wrapper_whose_string_merely_ENDS_in_py_yields_no_key(tmp_path):
    """THE SECOND OPACITY CLAUSE, and it exists because the first one does not fire here. This test
    was written expecting `opaque_entry` and got a KEY: `_stage_reachable_files` credits any argv
    token ending in `.py` as a script, so `sh -c "python mine.py"` produces a closure holding the
    phantom `python mine.py` — it answers "bounded" about a shell string it never parsed. That is
    harmless in `_safe_reuse_start`, where a phantom only ever ADDS to a set used to REFUSE, and it
    is not harmless in a key, which must never claim to have bounded an entry point it did not find.
    The clause lives in the key and NOT in the closure, because the closure is the live reuse
    decision on a running run and its reach must not move for an instrument's sake."""
    wd = _make_workdir(tmp_path, "a")
    wrapped = [{"name": "mine", "command": ["/bin/sh", "-c", "python mine.py"], "timeout": 60.0}]
    reachable = EvalStagesMixin._stage_reachable_files(wrapped, str(wd))
    assert reachable == {"python mine.py"}, "the quirk this clause covers, re-derived not assumed"
    key, reason = _stage_key(str(wd), wrapped)
    assert key is None and reason == "unresolved_entry" and reason in KEY_REASONS


def test_a_non_default_cwd_yields_no_key(tmp_path):
    """The other clause taken verbatim from `_safe_reuse_start`: under a subdir cwd the stage's
    script paths and the workdir-relative digest map resolve against different bases."""
    wd = _make_workdir(tmp_path, "a")
    key, reason = _stage_key(str(wd), _stages(), cwd="sub")
    assert key is None and reason == "non_default_cwd"


def test_a_file_that_will_not_read_yields_no_key_rather_than_a_key_over_a_smaller_set(tmp_path):
    """The clause `_safe_reuse_start` does NOT have, and needs to be here because this predicate
    reasons over CONTENT where that one reasons over names: an unreadable name is still a name, but
    an unreadable file silently skipped is a key over a smaller set than it claims — which is exactly
    how two different workdirs come to share one."""
    if os.name == "nt" or os.getuid() == 0:                  # chmod is not a permission fence there
        pytest.skip("needs POSIX permissions and a non-root user")
    wd = _make_workdir(tmp_path, "a")
    locked = wd / "secret.bin"
    locked.write_bytes(b"x")
    os.chmod(locked, 0o000)
    try:
        key, reason = _stage_key(str(wd), _stages())
        assert key is None and reason == "unreadable_workdir"
    finally:
        os.chmod(locked, 0o600)


def test_the_stages_own_output_is_out_of_its_own_key_but_an_earlier_stages_output_is_in(tmp_path):
    """The exclusion is FORWARD-looking, and that is not cosmetic. A repaired node's workdir
    deliberately persists, so a previous attempt's artifact is sitting there while the stage is about
    to re-run; digesting it would make two runs of an identical pipeline key differently purely
    because one had failed later on. An EARLIER stage's output is the opposite case — it is this
    stage's INPUT and must be in the key."""
    stages = [{"name": "mine", "expect": {"files": ["negatives.txt"]}, "command": ["python", "a.py"]},
              {"name": "train", "expect": {"files": ["model.bin"]}, "command": ["python", "b.py"]}]
    assert declared_outputs(stages, at=0) == {"negatives.txt", "model.bin"}
    assert declared_outputs(stages, at=1) == {"model.bin"}
    wd = _make_workdir(tmp_path, "a")
    (wd / "negatives.txt").write_text("stale from attempt 1")
    content, _ = workdir_content(str(wd), exclude=declared_outputs(stages, at=0))
    assert "negatives.txt" not in content and "config.json" in content
    content_for_train, _ = workdir_content(str(wd), exclude=declared_outputs(stages, at=1))
    assert "negatives.txt" in content_for_train


# --------------------------------------------------------------------------------------------
# THE RECORD: what a real run writes on its stage rows
# --------------------------------------------------------------------------------------------

def test_a_real_run_records_the_key_and_the_output_identity_on_the_stage_row(tmp_path):
    """Driven through the real `run_command_eval`, not a double: the row is what every later reader
    — the reporter, a future cache, an operator — actually gets."""
    wd = _make_workdir(tmp_path, "a")
    res = _run(wd)
    assert res.exit_code == 0
    row = res.stages[0]
    assert row["status"] == "ok"
    assert str(row[STAGE_INPUT_KEY]).startswith("stage1:")
    outs = row[STAGE_OUTPUTS_KEY]
    assert [o["path"] for o in outs] == ["negatives.txt"]
    assert outs[0]["bound"] and outs[0]["digest_mode"] == "sha256"
    assert outs[0]["digest"] and outs[0]["identity"]


def test_the_key_on_the_row_is_the_one_the_stage_ran_UNDER_not_the_one_it_left_behind(tmp_path):
    """The key is derived BEFORE the command, and that ordering is what makes it a key at all.

    THE FIXTURE HAS TO WRITE AN UNDECLARED FILE OR THIS TEST PROVES NOTHING, which is how it was
    first written and why it is worth saying: a stage's OWN `expect.files` are excluded from its key
    (`declared_outputs`), so on a stage that writes only what it declares the before- and after-keys
    are equal and moving the derivation to after the command changes no assertion. Real miners write
    undeclared side files — a coverage stat, a cache, a `.pkl` nobody named — and those ARE in the
    key, because 'not declared' means 'undeclared' and never 'not written'.

    The consequence is worth knowing rather than hiding: a stage that leaves an undeclared file
    behind keys DIFFERENTLY on its second attempt, so a repaired node stops being a cross-node reuse
    source. That is the conservative direction — it can only ever refuse a hit."""
    miner = _MINER + "pathlib.Path('coverage.stat').write_text('0.93')\n"
    wd = _make_workdir(tmp_path, "a", miner=miner)
    before, _ = _stage_key(str(wd), _stages())
    res = _run(wd)
    after, _ = _stage_key(str(wd), _stages())
    assert (wd / "coverage.stat").exists(), "the undeclared side file is the whole point"
    assert after != before, "an undeclared output IS in the key — otherwise this test is vacuous"
    assert res.stages[0][STAGE_INPUT_KEY] == before


def test_a_stage_with_no_declared_artifact_records_no_output_identity(tmp_path):
    """Absence is a fact, not an omission: a stage that declared nothing has nothing anybody could
    later prove is still the artifact a key names, so it can never be a reuse source."""
    wd = _make_workdir(tmp_path, "a")
    stages = [dict(_stages()[0])]
    stages[0].pop("expect")
    res = _run(wd, stages=stages)
    assert res.exit_code == 0 and STAGE_OUTPUTS_KEY not in res.stages[0]
    assert reuse_refusal(res.stages[0].get(STAGE_INPUT_KEY), res.stages[0], str(wd)) == "no_outputs"


def test_an_opaque_stage_records_its_reason_instead_of_a_key(tmp_path):
    wd = _make_workdir(tmp_path, "a")
    stages = [{"name": "mine", "command": ["python", "-m", "pip"], "timeout": 60.0}]
    res = run_command_eval(["/bin/sh"], str(wd), 60.0, _M, stages=stages,
                           stage_key_fn=lambda s, i: _stage_key(str(wd), s, i))
    row = res.stages[0]
    assert STAGE_INPUT_KEY not in row and row[STAGE_KEY_REASON] == "opaque_entry"


# --------------------------------------------------------------------------------------------
# THE DECISION: a stale or tampered artifact is REFUSED, not reused
# --------------------------------------------------------------------------------------------

def test_a_matching_key_over_an_intact_artifact_is_the_only_state_that_permits_reuse(tmp_path):
    wd = _make_workdir(tmp_path, "a")
    res = _run(wd)
    row = res.stages[0]
    assert reuse_refusal(row[STAGE_INPUT_KEY], row, str(wd)) is None


@pytest.mark.parametrize("tamper,expected", [
    (lambda wd: (wd / "negatives.txt").unlink(), "output_missing"),
    (lambda wd: (wd / "negatives.txt").write_text("n=99"), "output_identity_changed"),
])
def test_a_tampered_or_replaced_artifact_is_refused(tmp_path, tamper, expected):
    """STRICTER THAN THE ARTIFACT CONTRACT, and this is the half the request insisted on.
    `verify_stage_artifacts` proves an artifact exists, is non-empty and was written after the stage
    began — the rule that caught v8 node 5's "skip if the file exists" — and it is satisfied by ANY
    later write. A reuse needs more: the bytes must be the ones the key NAMES."""
    wd = _make_workdir(tmp_path, "a")
    row = _run(wd).stages[0]
    assert reuse_refusal(row[STAGE_INPUT_KEY], row, str(wd)) is None
    tamper(wd)
    got = reuse_refusal(row[STAGE_INPUT_KEY], row, str(wd))
    assert got == expected and got in REUSE_REFUSALS


def test_a_same_size_rewrite_that_restores_the_mtime_is_still_refused(tmp_path):
    """The case `expect`'s freshness rule cannot see AT ALL and the stat tuple alone might not on
    every platform: an artifact overwritten with different bytes of the SAME LENGTH, with its mtime
    put back. This is the `metric_subject` incident's own shape (two 92,174,712-byte
    `model.safetensors`, byte-identical in SIZE) turned on the reuse decision, and it is why
    `reuse_refusal` compares the DIGEST and not only `file_identity`."""
    wd = _make_workdir(tmp_path, "a")
    row = _run(wd).stages[0]
    art = wd / "negatives.txt"
    st = art.stat()
    art.write_bytes(b"X" * st.st_size)
    os.utime(art, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert art.stat().st_size == st.st_size and art.stat().st_mtime_ns == st.st_mtime_ns
    assert reuse_refusal(row[STAGE_INPUT_KEY], row, str(wd)) in (
        "output_identity_changed", "output_digest_changed")


def test_a_differing_key_is_refused_before_any_artifact_is_looked_at(tmp_path):
    wd = _make_workdir(tmp_path, "a")
    row = _run(wd).stages[0]
    assert reuse_refusal("stage1:" + "0" * 64, row, str(wd)) == "key_differs"
    assert reuse_refusal(None, row, str(wd)) == "no_key"
    assert reuse_refusal(row[STAGE_INPUT_KEY], None, str(wd)) == "no_candidate"


def test_the_cross_node_case_end_to_end_hit_and_miss(tmp_path):
    """THE PROPERTY THE REQUEST ASKED FOR, in one place: a second node whose key is identical may
    reuse the first's artifact; a second node that differs in one non-`.py` input may not.

    Both nodes really run, so the assertion is against what mining actually produced and not against
    the key's own opinion of itself."""
    first = _make_workdir(tmp_path, "node0", n_negatives=2)
    twin = _make_workdir(tmp_path, "node1", n_negatives=2)
    other = _make_workdir(tmp_path, "node2", n_negatives=4)
    src = _run(first).stages[0]
    for wd, may_reuse in ((twin, True), (other, False)):
        key, _ = _stage_key(str(wd), _stages())
        # The candidate's artifact lives in the SOURCE workdir, which is where a cache would copy
        # from — so the refusal check is asked against `first`, exactly as a cache would ask it.
        refusal = reuse_refusal(key, src, str(first))
        assert (refusal is None) is may_reuse, (wd.name, refusal)
        _run(wd)
    assert (twin / "negatives.txt").read_bytes() == (first / "negatives.txt").read_bytes()
    assert (other / "negatives.txt").read_bytes() != (first / "negatives.txt").read_bytes()


# --------------------------------------------------------------------------------------------
# THE REGISTRY halves stay closed
# --------------------------------------------------------------------------------------------

def test_every_reason_a_key_or_a_reuse_can_be_refused_is_in_its_closed_vocabulary():
    """Both slugs ride on a durable stage row and into the reporter, and a reader deciding whether a
    cache is worth having has to be able to tell 'no sibling ran this' from 'one did and its artifact
    has been overwritten since'. A free-text reason cannot carry that."""
    assert len(set(KEY_REASONS)) == len(KEY_REASONS)
    assert len(set(REUSE_REFUSALS)) == len(REUSE_REFUSALS)
    assert not set(KEY_REASONS) & set(REUSE_REFUSALS), (
        "the two answer different questions — 'this stage has no key' is not 'this reuse was "
        "refused' — and one vocabulary would let a reporter print the wrong remedy")


# --------------------------------------------------------------------------------------------
# THE ENGINE SEAM: the injection is real, and it carries this run's identity
# --------------------------------------------------------------------------------------------

def test_the_engine_builds_a_working_key_function_bound_to_its_own_run(tmp_path):
    """`Engine._stage_key_fn` is the LAYERING seam: the key needs `_stage_reachable_files`, which
    lives in `looplab/engine/`, and `runtime` imports nothing above `core`. This drives the real
    engine method rather than pinning its text, because what has to hold is that the callable it
    returns produces a usable key and that two DIFFERENT runs never mint the same one.

    The scope is the run directory plus `setup_config_hash(task)` — the same digest
    `run_started.config_hash` carries — so a resume of one run keeps its keys while a second run over
    an identical workdir does not inherit them. That asymmetry is the whole point: everything outside
    the workdir (the mount table, the model cache, site-packages) is uncovered, and only the run's own
    identity stands in for it."""
    from tests.factories import make_engine
    wd = _make_workdir(tmp_path, "node0")
    eng_a = make_engine(tmp_path / "runA")
    eng_b = make_engine(tmp_path / "runB")
    fn_a, fn_b = eng_a._stage_key_fn(str(wd)), eng_b._stage_key_fn(str(wd))
    key_a, reason_a = fn_a(_stages(), 0)
    key_b, _ = fn_b(_stages(), 0)
    assert reason_a == "" and key_a and key_a != key_b
    # And the same engine over the same workdir is stable — a key that moved per call could never hit.
    assert eng_a._stage_key_fn(str(wd))(_stages(), 0)[0] == key_a
    # The non-default `cwd` clause survives the injection (it is the caller's argument, not the key's).
    assert eng_a._stage_key_fn(str(wd), cwd="sub")(_stages(), 0) == (None, "non_default_cwd")


def test_an_instrument_failure_never_takes_down_an_eval(tmp_path):
    """The containment rule, driven. A stage-identity derivation walks a whole workdir on a geesefs
    mount; if it raises, the eval must still run and the row must still be written — an instrument
    that can kill a five-hour training is worse than no instrument. Same reasoning as
    `_monitor_asha`'s per-tick containment, and the opposite failure to
    `tests/test_asha_monitor.py::_AshaStub`'s: there the swallow HID a broken watchdog, so the row
    records the reason rather than silently carrying nothing."""
    wd = _make_workdir(tmp_path, "a")

    def _explodes(stages, index):
        raise RuntimeError("geesefs said no")
    res = run_command_eval([sys.executable, "mine.py"], str(wd), 60.0, _M,
                           stages=_stages(), stage_key_fn=_explodes)
    assert res.exit_code == 0 and res.stages[0]["status"] == "ok"
    assert STAGE_INPUT_KEY not in res.stages[0]
    assert res.stages[0][STAGE_KEY_REASON] == "unreadable_workdir"
    # The OUTPUT identity is independent of the key and must still be there — losing it would make
    # the duplication report blind for a reason that has nothing to do with the artifacts.
    assert res.stages[0][STAGE_OUTPUTS_KEY][0]["bound"]


# --------------------------------------------------------------------------------------------
# THE REPORTER: both numbers, over a run's own recorded rows
# --------------------------------------------------------------------------------------------

def _run_dir_with_stage_rows(tmp_path, rows):
    """A minimal run directory whose event log carries `stage_finished` rows verbatim."""
    from looplab.events.eventstore import EventStore
    rd = tmp_path / "run"
    rd.mkdir(parents=True, exist_ok=True)
    store = EventStore(str(rd / "events.jsonl"))
    store.append("run_started", {"run_id": "r"})
    for row in rows:
        store.append("stage_finished", row)
    return rd


def _stage_dups(run_dir):
    from typer.testing import CliRunner
    from looplab.cli import app
    res = CliRunner().invoke(app, ["stage-dups", str(run_dir)])
    assert res.exit_code == 0, res.output
    return res.output


def test_the_reporter_separates_observed_duplication_from_would_be_reuse(tmp_path):
    """The two reports answer different questions and on the corpus they DISAGREED, which is why the
    command prints both rather than letting one stand in for the other: the declared-parameter
    grouping that looked like 12 duplicated `mine` stages is 4 groups of provably identical output.

    Here: three stages produced the same bytes (observed duplication) while only two of them share an
    input key (what a cache would actually have hit)."""
    outs = [{"path": "n.parquet", "bound": True, "digest_mode": "sha256", "digest": "aa",
             "identity": [1, 2, 3]}]
    rd = _run_dir_with_stage_rows(tmp_path, [
        {"node_id": 0, "name": "mine", "status": "ok", "seconds": 2300.0,
         "stage_input_key": "k1", "stage_outputs": outs},
        {"node_id": 1, "name": "mine", "status": "ok", "seconds": 2200.0,
         "stage_input_key": "k1", "stage_outputs": outs},
        {"node_id": 2, "name": "mine", "status": "ok", "seconds": 2100.0,
         "stage_input_key": "k2", "stage_outputs": outs},
    ])
    out = _stage_dups(rd)
    assert "3 stages produced the same bytes (nodes 0, 1, 2)" in out
    assert "total duplicated: 1.19 h" in out                      # 2200 + 2100 seconds
    assert "cross-node reuse key: 1 candidate hit(s), 0 of them WRONG, 0.61 h" in out


def test_the_reporter_names_a_wrong_hit_and_prints_the_count_even_when_it_is_zero(tmp_path):
    """A key collision whose artifacts differ is THE number that decides whether a cache may ship, so
    it is printed per occurrence AND as a total — and the total is printed at zero, because "no wrong
    hits over 12 candidates" and "no candidates at all" are different states with the same headline.
    """
    a = [{"path": "n.parquet", "bound": True, "digest_mode": "sha256", "digest": "aa",
          "identity": [1, 2, 3]}]
    b = [{"path": "n.parquet", "bound": True, "digest_mode": "sha256", "digest": "bb",
          "identity": [1, 2, 4]}]
    rd = _run_dir_with_stage_rows(tmp_path, [
        {"node_id": 0, "name": "mine", "status": "ok", "seconds": 100.0,
         "stage_input_key": "k1", "stage_outputs": a},
        {"node_id": 1, "name": "mine", "status": "ok", "seconds": 100.0,
         "stage_input_key": "k1", "stage_outputs": b},
    ])
    out = _stage_dups(rd)
    assert "WRONG HIT: mine node 1 would have reused node 0's artifact" in out
    assert "1 candidate hit(s), 1 of them WRONG" in out
    assert "none." in out                                   # and NO duplication is claimed


def test_a_run_from_before_the_instrument_reports_unrecorded_and_never_no_duplication(tmp_path):
    """A row written before 2026-08-17 carries neither field. Reporting that as "no duplication"
    would be the same failure the whole change is about — a missing check reading as a passing one —
    so the absence is named and counted."""
    rd = _run_dir_with_stage_rows(tmp_path, [
        {"node_id": 0, "name": "mine", "status": "ok", "seconds": 2300.0},
        {"node_id": 1, "name": "mine", "status": "ok", "seconds": 2200.0},
    ])
    out = _stage_dups(rd)
    assert "with an input key: 0" in out and "unrecorded=2" in out
    assert "0 candidate hit(s), 0 of them WRONG" in out


def test_an_unbound_output_is_never_treated_as_a_comparable_fingerprint(tmp_path):
    """A stage that completed but whose declared artifact was stale/missing at the instant the
    contract was held names nothing anybody may compare, so it joins no duplication group and can
    source no reuse. Two such rows must not read as "these produced the same bytes"."""
    unbound = [{"path": "n.parquet", "bound": False, "reason": "stale"}]
    rd = _run_dir_with_stage_rows(tmp_path, [
        {"node_id": 0, "name": "mine", "status": "ok", "seconds": 100.0,
         "stage_input_key": "k1", "stage_outputs": unbound},
        {"node_id": 1, "name": "mine", "status": "ok", "seconds": 100.0,
         "stage_input_key": "k1", "stage_outputs": unbound},
    ])
    out = _stage_dups(rd)
    assert "produced the same bytes" not in out
    # …but the key still MATCHED, and a candidate hit over unnameable output is a WRONG hit.
    assert "1 candidate hit(s), 1 of them WRONG" in out


# --------------------------------------------------------------------------------------------
# THE COST BOUNDS: a key we cannot afford to compute is a key we do not mint
# --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("attr,value,expected", [
    ("MAX_KEYED_FILES", 1, "too_many_files"),
    ("MAX_KEYED_BYTES", 8, "too_many_bytes"),
])
def test_a_workdir_over_either_ceiling_gets_no_key_rather_than_a_truncated_one(
        tmp_path, monkeypatch, attr, value, expected):
    """Both ceilings fail CLOSED, and that is the whole point of having them at all: a map over "the
    first N files" or "the first N bytes" is not a cheaper key, it is a key two different workdirs
    can share. The BYTES one is the one that actually binds on this box — the per-file
    `SAMPLE_ABOVE` ceiling bounds nothing about a workdir holding two hundred 200 MB shards, and the
    largest real repo-task workdir here digests 1,017 MB across 144 files.

    Driven through the real derivation with the constant lowered, rather than by building a 4 GiB
    fixture: what has to hold is that crossing the bound produces `(None, <reason>)`, not that this
    box can write four gigabytes."""
    import looplab.runtime.stage_identity as si
    wd = _make_workdir(tmp_path, "a")
    assert _stage_key(str(wd), _stages())[0], "the fixture must key normally first"
    monkeypatch.setattr(si, attr, value)
    key, reason = _stage_key(str(wd), _stages())
    assert key is None and reason == expected and reason in KEY_REASONS


def test_the_byte_ceiling_is_checked_before_the_digest_not_after(tmp_path, monkeypatch):
    """Crossing the ceiling must cost at most the ceiling. If the running total were accumulated
    from what was already digested, a 50 GB workdir would be read whole and only then refused —
    which is the cost this bound exists to prevent, paid in full plus a refusal."""
    import looplab.runtime.stage_identity as si
    wd = _make_workdir(tmp_path, "a")
    (wd / "big.bin").write_bytes(b"x" * 200_000)
    digested: list = []
    real = si._sha256
    monkeypatch.setattr(si, "_sha256", lambda p, n: (digested.append(str(p)), real(p, n))[1])
    monkeypatch.setattr(si, "MAX_KEYED_BYTES", 1000)
    assert _stage_key(str(wd), _stages()) == (None, "too_many_bytes")
    assert not any(f.endswith("big.bin") for f in digested), (
        "the 200 KB file was digested despite blowing a 1 KB ceiling — the check is in the wrong place")
