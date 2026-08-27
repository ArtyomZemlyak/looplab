"""The Developer writes the launcher, so the Developer has to be told how many devices exist.

THE DEFECT, measured on `runs/e5small-dr-unified-v6` node 0 — the first evaluation on code carrying
every fix of 2026-08-25. Two declarations, both authored by that run, both on disk:

    node_created.idea.footprint            {"gpus": 1}
    nodes/node_0/looplab_stages.json       train.command =
        ["accelerate","launch","--num_processes","2","--multi_gpu","-m","vectorsearch.train"]

`engine/resources.py::_acquire_gpus` fences `CUDA_VISIBLE_DEVICES` to exactly the granted devices,
so rank 1 asked for a device outside the fence and died:

    [rank1]: torch.AcceleratorError: CUDA error: invalid device ordinal   (train.log:463)

Cost: `mine` 356.5 s + `train` 171.6 s, then an INERT repair that carried the right diagnosis and
changed nothing, then the identical `train` failure again at 172.0 s — 62.7 minutes of wall clock
from the first failure to a working manifest, on a run whose first node did not start until t+131.7m.

WHY A NOTE AND NOT A REFUSAL, and the order matters. `engine/proposal_cues.py::_stamp_gpu_budget_hint`
puts the GPU cue on the RESEARCHER; grepping the Developer prompts before this landed finds no
mention of gpus, devices or CUDA at all. The role that writes `--num_processes N` was never told N.
Refusing first would tell it "no" without telling it what to write — the `runtime/deps.py` rule,
where free text may NOMINATE and only a probe DECIDES. A static manifest check (`procs >
footprint.gpus`, which on the 7-stage corpus refuses exactly the one defective node and zero
legitimate ones) is a reasonable SECOND rung and is deliberately not this change.

Every assertion below has an input that makes it fail; the mutations are named in the messages.
"""
from __future__ import annotations

from looplab.adapters.repo_developer import LLMRepoDeveloper
from looplab.core.models import Idea


def _dev() -> LLMRepoDeveloper:
    # `__new__` on purpose: the note is a pure function of the idea, and constructing a real
    # Developer would drag in a task, a workspace and a provider for a string.
    return LLMRepoDeveloper.__new__(LLMRepoDeveloper)


def _idea(footprint=None) -> Idea:
    kwargs = {"operator": "draft", "hypothesis": "a concrete experiment"}
    if footprint is not None:
        kwargs["footprint"] = footprint
    return Idea(**kwargs)


def test_the_note_states_the_declared_device_count():
    """The count, and — since 2026-08-27 — the fact that it is a CEILING rather than a grant.

    This asserted "EXACTLY N", which is what the note said and what it could not know: the granting
    authority is `engine/resources.py::_resource_request_for_node`, which applies an operator
    `resource_pin` and CLAMPS to the detected pool, so a declared 4 on a two-GPU box is granted 2.
    Neither input is reachable from an adapter. The ceiling is what the Developer needs anyway —
    the failure being prevented is starting MORE processes than the fence admits.
    """
    note = _dev()._gpu_footprint_note(_idea({"gpus": 2}))
    assert "DECLARED 2 GPUs AND WILL GET AT MOST THAT MANY" in note, (
        "MUTATION: return '' unconditionally and this goes red — which is the state that let v6 "
        "author a 2-process launcher against a 1-GPU fence")
    assert "--num_processes" in note, (
        "the note must name the FLAG the role actually writes; 'you get 2 GPUs' with no connection "
        "to the launcher is the advice the Researcher already had and the Developer could not use")


def test_a_single_device_reads_as_singular_and_names_the_failure_it_prevents():
    note = _dev()._gpu_footprint_note(_idea({"gpus": 1}))
    assert "DECLARED 1 GPU AND" in note, "MUTATION: drop the plural branch -> '1 GPUs'"
    assert "GPUs" not in note.split("DECLARED 1 GPU AND")[0]
    # The consequence, in the words the log will actually print, so the role can connect the
    # instruction to the crash it is avoiding.
    assert "invalid device ordinal" in note


def test_an_UNSTATED_footprint_says_nothing_rather_than_guessing():
    """A role told "some GPUs" is worse off than one told nothing."""
    assert _dev()._gpu_footprint_note(_idea()) == "", (
        "MUTATION: default the count to 1 when the footprint is absent and this goes red — a "
        "guessed fence is a claim the engine never made")
    assert _dev()._gpu_footprint_note(_idea({})) == ""
    assert _dev()._gpu_footprint_note(_idea({"gpus": None})) == ""


class _Carrier:
    """Anything with a `.footprint`, which is all `_gpu_footprint_note` requires.

    It reads `getattr(idea, "footprint", None)` and is documented TOTAL over junk, so its guards
    have to be exercised through a carrier that can actually deliver the value. An `Idea` cannot:
    `normalize_researcher_footprint` refuses `{"gpus": true}` and leaves `Idea.footprint` **None**,
    so a test written against `Idea` passes without the guard ever running. Mine did, and the
    mutation harness is what caught it — relaxing `type(raw) is int` to `isinstance(raw, int)`
    failed ZERO tests until this class existed.
    """

    def __init__(self, footprint):
        self.footprint = footprint


def test_a_BOOL_is_not_a_device_count():
    """`isinstance(True, int)` is True in Python, so this needs an explicit refusal.

    A footprint of `{"gpus": true}` states no count at all; announcing "1 GPU" about it would be
    the engine inventing a fence width out of a flag.
    """
    assert _dev()._gpu_footprint_note(_Carrier({"gpus": True})) == "", (
        "MUTATION: relax `type(raw) is int` to `isinstance(raw, int)` and this goes red")
    # And the guard must not have become a blanket refusal on the way.
    assert "DECLARED 3 GPUs" in _dev()._gpu_footprint_note(_Carrier({"gpus": 3}))


def test_the_model_ALSO_refuses_a_bool_footprint_one_layer_up():
    """Two rungs, and knowing which one fires matters: this one is why the guard looked untested."""
    assert _idea({"gpus": True}).footprint is None, (
        "if this ever starts passing a bool through, the note's own guard is the only thing left")


def test_a_ZERO_or_NEGATIVE_count_states_nothing():
    assert _dev()._gpu_footprint_note(_idea({"gpus": 0})) == ""
    assert _dev()._gpu_footprint_note(_idea({"gpus": -1})) == ""


def test_it_is_TOTAL_over_junk_because_a_prompt_cue_must_never_fail_a_build():
    """Same contract as `_time_budget_note`: a bare/unit-test dev with no idea states nothing."""
    dev = _dev()
    for junk in (None, "an idea", 7, object()):
        assert dev._gpu_footprint_note(junk) == ""


def test_BOTH_prompt_phases_carry_it_and_the_repair_path_is_the_one_that_matters():
    """The stages phase authors the launcher; a REPAIR session skips that phase entirely.

    v6's launcher bug had to be fixed from the implement/repair path, which is reached without ever
    re-entering `_stages_user` — so a note spliced only into the stages phase would have been absent
    from the exact session that fixed it. Driven by AST rather than a substring: `called_names`
    resolves real `ast.Call` nodes, and comments are not AST nodes.
    """
    import ast
    import inspect

    from looplab.adapters import repo_developer

    source = inspect.getsource(repo_developer)
    tree = ast.parse(source)
    callers = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "_gpu_footprint_note"):
                callers.add(node.name)
    assert "_stages_user" in callers, "the phase that authors the launcher must be told"
    assert len(callers) >= 2, (
        f"the repair/implement path must carry it too; callers found: {sorted(callers)}")


def test_it_rides_BESIDE_the_time_budget_note_in_both_places():
    """The two are twins — same defect, different axis — and must not drift apart.

    `_time_budget_note`'s own docstring records the identical shape: "until now only the Researcher
    was ever told the number". Wherever one is spliced, so is the other.
    """
    import ast
    import inspect

    from looplab.adapters import repo_developer

    tree = ast.parse(inspect.getsource(repo_developer))
    for name in ("_stages_user",):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        attrs = {i.func.attr for i in ast.walk(fn)
                 if isinstance(i, ast.Call) and isinstance(i.func, ast.Attribute)}
        assert {"_time_budget_note", "_gpu_footprint_note"} <= attrs, (
            f"{name} must carry both notes; found {sorted(attrs & {'_time_budget_note', '_gpu_footprint_note'})}")
