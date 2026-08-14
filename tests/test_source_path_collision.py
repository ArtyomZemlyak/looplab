"""The F1c COLLISION check: an absolute source-tree path that names the node's own declared output.

`docs/29-operator-backlog-2026-08-11.md` §F1c. The rule is a contradiction between two things the
AGENT ITSELF authored — its stage manifest's `expect.files` (what this node writes) and a path it
hard-coded into a config (where it then reads) — and it is refused at the tool call, where the model
can act on it for the price of one tool call rather than after the GPU is spent.

Every fixture below is the LITERAL text from the preserved corpus, not a paraphrase:

  * `runs/rubertlite-dr-unified-v6` node 0 — trained twice and threw away ~2.5 GPU-hours;
  * `runs/rubertlite-dr-unified-v6` node 4 and `runs/rubertlite-dr-unified-v2` node 4 — both
    recorded 0.224975, a human's July checkpoint, three weeks apart;
  * `runs/rubertlite-dense-retrieval` node 36 — the measured 1-in-116 LEGITIMATE read of a teacher
    checkpoint that lives INSIDE the editable root. It is the false-positive control and it is a
    live one: the path really is under the root, and a blanket ban really does kill it.

The tests drive the tool provider (a real `RepoWriteTools`, real refusal strings, real staged file
map), never a source pin — per CLAUDE.md a positive pin here would be one comment away from vacuous,
while "the file was not staged" is not.
"""
from __future__ import annotations

import json

import pytest

from looplab.adapters.repo_write_tools import (RepoWriteTools, collision_feedback,
                                               declared_output_paths, manifest_path_collisions,
                                               source_root_targets)

SRC = "/home/jovyan/data/vectorizer-unified"
DR_SRC = "/home/jovyan/data/vectorizer/dense-retrieval"

# v6 node 0's staged manifest, verbatim from its `node_created.files`.
V6N0_MANIFEST = json.dumps({"stages": [{
    "name": "train",
    "command": ["python", "-m", "vectorsearch.train"],
    "timeout": 14400.0,
    "check": True,
    "expect": {
        "files": ["vectorsearch/experiments/unified-mnr-t05-b8192-e10_rubert-tiny-lite/final/"
                  "model.safetensors"],
        "assert": "all 10 epochs completed and a fresh checkpoint saved to final/",
    },
}]})

# ...and line 215 of the `vectorsearch/configs/config.yaml` it staged in the SAME session.
V6N0_CONFIG = (
    "      checkpoint_path: /home/jovyan/data/vectorizer-unified/vectorsearch/experiments/"
    "unified-mnr-t05-b8192-e10_rubert-tiny-lite/final   # Local checkpoint to load instead of "
    "model_path\n")

# v2 node 4: the manifest declares the final DIRECTORY, and the config names exactly it — the
# equal-length case, where neither path is a proper ancestor of the other.
V2N4_MANIFEST = json.dumps({"stages": [
    {"name": "data_prep", "command": ["python", "-m", "vectorsearch.prep"], "timeout": 600.0},
    {"name": "train", "command": ["python", "-m", "vectorsearch.train"], "timeout": 14400.0,
     "expect": {"files": ["vectorsearch/experiments/unified-baseline_rubert-tiny-lite/final"]}},
]})
V2N4_CONFIG = ("      checkpoint_path: /home/jovyan/data/vectorizer-unified/vectorsearch/"
               "experiments/unified-baseline_rubert-tiny-lite/final  # Local checkpoint\n")


def _tools(roots=((".", SRC),), files=None):
    w = RepoWriteTools(["**/*"], [], editables=[{"name": n, "path": p} for n, p in roots])
    w.files = dict(files or {})
    return w


# --- the three defective nodes ---------------------------------------------------------------

def test_v6_node_0_is_refused_at_the_write_that_authored_it():
    """The incident F1c was written about, at the exact moment it happened.

    Its `node_created.files` is `{looplab_stages.json, vectorsearch/configs/config.yaml,
    vectorsearch/test.py}` — the STAGES phase persisted the manifest before the implement phase's
    write tools existed, so the manifest really is in `self.files` when the config is written.
    """
    w = _tools(files={"looplab_stages.json": V6N0_MANIFEST})
    out = w.execute("write_file", {"path": "vectorsearch/configs/config.yaml",
                                   "content": V6N0_CONFIG})
    assert out.startswith("(refused:")
    # It names BOTH declarations, because either one may be the one that is wrong.
    assert "unified-mnr-t05-b8192-e10_rubert-tiny-lite/final" in out
    assert "model.safetensors" in out
    assert "'train'" in out
    # ...and nothing was staged, which is the property, not the message.
    assert "vectorsearch/configs/config.yaml" not in w.files


def test_v6_node_4_and_v2_node_4_are_refused_including_the_equal_path_case():
    """Both nodes that recorded 0.224975. v2 node 4's config and manifest name the SAME directory,
    so the rule cannot be "the config names a strict ancestor of a declared file"."""
    v6n4 = json.dumps({"stages": [{
        "name": "train", "command": ["python", "-m", "vectorsearch.train"], "timeout": 14400.0,
        "expect": {"files": [
            "vectorsearch/experiments/unified-baseline_rubert-tiny-lite/final/model.safetensors",
            "vectorsearch/experiments/unified-baseline_rubert-tiny-lite/final/modules.json"]}}]})
    for manifest in (v6n4, V2N4_MANIFEST):
        w = _tools(files={"looplab_stages.json": manifest})
        out = w.execute("write_file", {"path": "vectorsearch/configs/config.yaml",
                                       "content": V2N4_CONFIG})
        assert out.startswith("(refused:"), manifest
        assert "vectorsearch/configs/config.yaml" not in w.files


def test_an_edit_that_introduces_the_line_is_refused_and_one_that_does_not_is_not():
    """`edit_file` reads only the REPLACEMENT, exactly as the advisory note does: refusing an
    unrelated hunk over a line the repo already carried is a refusal the model cannot satisfy."""
    w = _tools(files={"looplab_stages.json": V6N0_MANIFEST,
                      "vectorsearch/configs/config.yaml": "a: 1\nb: 2\n"})
    bad = w.execute("edit_file", {"path": "vectorsearch/configs/config.yaml",
                                  "search": "b: 2", "replace": V6N0_CONFIG.strip()})
    assert bad.startswith("(refused:")
    assert w.files["vectorsearch/configs/config.yaml"] == "a: 1\nb: 2\n"
    ok = w.execute("edit_file", {"path": "vectorsearch/configs/config.yaml",
                                 "search": "a: 1", "replace": "a: 3"})
    assert not ok.startswith("(refused:")
    assert w.files["vectorsearch/configs/config.yaml"].startswith("a: 3")


# --- the false-positive control, and what a blanket ban costs ---------------------------------

def test_node_36s_teacher_checkpoint_is_not_a_collision():
    """`runs/rubertlite-dense-retrieval` node 36: `--teacher_checkpoint <editable root>/models/
    rubertlite-20e-v7/last.ckpt`, in a run declaring no mount at all. Legitimate distillation, and
    the one node in 116 that a careless rule kills.

    Two independent reasons it cannot fire, and BOTH are tested, because the first is an accident of
    that node's manifest and the second is the property: its train stage declares no `expect` at
    all, and even given one, `models/rubertlite-20e-v7` and `models/rubertlite_run` differ at their
    second component. A shared top directory is not a collision.
    """
    argv_line = (f'"--teacher_checkpoint", "{DR_SRC}/models/rubertlite-20e-v7/last.ckpt"')
    no_expect = json.dumps({"stages": [{"name": "train", "command": ["python", "train.py"],
                                        "timeout": 14400.0}]})
    with_expect = json.dumps({"stages": [{
        "name": "train", "command": ["python", "train.py"], "timeout": 14400.0,
        "expect": {"files": ["models/rubertlite_run/last.ckpt"]}}]})
    for manifest in (no_expect, with_expect):
        w = _tools(roots=((".", DR_SRC),), files={"looplab_stages.json": manifest})
        out = w.execute("write_file", {"path": "train_cfg.py", "content": argv_line})
        assert not out.startswith("(refused:"), manifest
        assert w.files["train_cfg.py"] == argv_line
    # And the advisory NOTE still fires — the operator-facing signal is not lost, only the refusal.
    assert "NOTE:" in out


def test_a_committed_base_model_under_the_root_is_not_a_collision():
    """`rubert-dr-0807` nodes 0/1/3 read `<editable root>/models/converted/e5-small-v1.1.1` — a
    committed base model. A blanket ban refuses all three; the collision rule spares all three,
    because a base model is not something the node declares it WRITES."""
    manifest = json.dumps({"stages": [{
        "name": "train", "command": ["python", "train.py"], "timeout": 14400.0,
        "expect": {"files": ["models/rubertlite_run/last.ckpt"]}}]})
    w = _tools(roots=((".", DR_SRC),), files={"looplab_stages.json": manifest})
    out = w.execute("write_file", {"path": "cfg.py",
                                   "content": f"BASE = '{DR_SRC}/models/converted/e5-small-v1.1.1'"})
    assert not out.startswith("(refused:")


def test_a_source_path_for_a_different_experiment_is_not_a_collision():
    """`rubertlite-dr-unified-v2` node 0: its config carries the repo's own committed default
    (`unified-baseline_…`) while its manifest declares `qwen3_hn_rubert-tiny-lite`. Two different
    experiments, so it is not a contradiction and the check must stay quiet."""
    manifest = json.dumps({"stages": [{
        "name": "train", "command": ["python", "-m", "vectorsearch.train"], "timeout": 14400.0,
        "expect": {"files":
                   ["vectorsearch/experiments/qwen3_hn_rubert-tiny-lite/final/model.safetensors"]}}]})
    w = _tools(files={"looplab_stages.json": manifest})
    assert not w.execute("write_file", {"path": "vectorsearch/configs/config.yaml",
                                        "content": V2N4_CONFIG}).startswith("(refused:")


# --- ordering: the two directions the write side alone cannot see -----------------------------

def test_declare_stages_refuses_when_an_already_staged_file_collides():
    """The REPAIR order, which the write-side check structurally cannot reach: the failing node's
    config arrives in the seeded working set and is never handed to a write tool."""
    w = _tools(files={"vectorsearch/configs/config.yaml": V6N0_CONFIG})
    out = w.execute("declare_stages", {"stages": json.loads(V6N0_MANIFEST)["stages"]})
    assert out.startswith("(refused:")
    assert "vectorsearch/configs/config.yaml" in out
    assert "The stages were NOT declared." in out
    assert "looplab_stages.json" not in w.files


def test_declare_stages_accepts_the_same_manifest_when_nothing_collides():
    w = _tools(files={"vectorsearch/configs/config.yaml": "checkpoint_path: vectorsearch/x\n"})
    out = w.execute("declare_stages", {"stages": json.loads(V6N0_MANIFEST)["stages"]})
    assert out.startswith("declared 1 preceding stage")
    assert "looplab_stages.json" in w.files


def test_with_no_manifest_the_write_side_degrades_to_the_advisory_note():
    """§F1c asked that the check "degrade gracefully exactly where it matters most". With no
    declaration there is nothing to contradict, so it says nothing — and the note still fires."""
    w = _tools()
    out = w.execute("write_file", {"path": "vectorsearch/configs/config.yaml",
                                   "content": V6N0_CONFIG})
    assert not out.startswith("(refused:")
    assert "NOTE:" in out


# --- the rule itself --------------------------------------------------------------------------

def test_the_comparison_is_by_component_not_by_string_prefix():
    """The single property that makes the rule false-positive-free. `models/rubertlite-20e-v7`
    shares the string prefix `models/rubertlite` with `models/rubertlite_run` and is refused by a
    naive `startswith`; compared as components it is not a collision."""
    manifest = json.dumps({"stages": [{"name": "t", "command": ["x"],
                                       "expect": {"files": ["models/rubertlite_run/last.ckpt"]}}]})
    assert manifest_path_collisions(f"{SRC}/models/rubertlite-20e-v7/last.ckpt",
                                    manifest, [(".", SRC)]) == []
    assert manifest_path_collisions(f"{SRC}/models/rubertlite_run",
                                    manifest, [(".", SRC)]) != []


def test_a_named_editable_is_compared_in_the_workdir_frame():
    """A non-root editable mounts at `wd/<name>`, so its manifest paths carry that prefix. Without
    the translation the check would silently never fire in a multi-editable task."""
    manifest = json.dumps({"stages": [{"name": "t", "command": ["x"],
                                       "expect": {"files": ["repo_b/out/final/model.bin"]}}]})
    assert manifest_path_collisions(f"/srv/repo_b/out/final", manifest,
                                    [("repo_b", "/srv/repo_b")]) != []
    # ...and the same text under the ROOT editable is not this node's declared output.
    assert manifest_path_collisions(f"/srv/repo_b/out/final", manifest,
                                    [(".", "/srv/repo_b")]) == []


@pytest.mark.parametrize("host,expected", [
    (f'checkpoint_path: {SRC}/a/b/final   # trailing comment', f"{SRC}/a/b/final"),
    (f'"ckpt": "{SRC}/a/b/final",', f"{SRC}/a/b/final"),
    (f"CKPT = Path('{SRC}/a/b/final')", f"{SRC}/a/b/final"),
    (f"--ckpt={SRC}/a/b/final\n", f"{SRC}/a/b/final"),
])
def test_the_path_token_is_read_out_of_every_host_the_corpus_spells_it_in(host, expected):
    """YAML scalar, JSON string, Python literal, argv. The stop-set has to end the token at each
    host's own punctuation without eating the `-`/`_`/`.` an experiment directory contains."""
    got = source_root_targets(host, ".", SRC)
    assert [s for s, _ in got] == [expected]


def test_an_unparseable_or_expectless_manifest_yields_no_declaration_and_no_refusal():
    for text in ("", "{", "not json", json.dumps({"stages": [{"name": "t", "command": ["x"]}]})):
        assert declared_output_paths(text) == []
        assert manifest_path_collisions(f"{SRC}/anything/at/all", text, [(".", SRC)]) == []


@pytest.mark.parametrize("text", [
    "5", "true", "1.5", "null", '"hi"',                 # a parseable NON-object document
    '{"stages": 7}', '{"stages": 1.5}', '{"stages": true}',   # parseable, `stages` not a list
    '{"stages": {"a": {"name": "t"}}}',
    '{"stages": [{"name": "t", "expect": 7}]}',         # `expect` not an object
    '{"stages": [{"name": "t", "expect": {"files": 3}}]}',    # `files` not a list
])
def test_a_parseable_but_wrongly_shaped_manifest_is_silence_and_not_a_crash(text):
    """The sibling above covers UNPARSEABLE text. This covers the harder half: JSON that parses
    fine and is the wrong SHAPE.

    The manifest is model-authored and `looplab_stages.json` is an accepted hand-written Developer
    surface, so `declare_stages`' validation is not on this path. `declared_output_paths` is then
    called from `_write` on every SUBSEQUENT write in the session, and `tool_loop._run_tool_call`
    does not contain a tool exception — so a raise here surfaces as a Developer CRASH, which is a
    node terminal plus a run-level auto-pause. Driven through the real tool, not the helper alone:
    the manifest write must succeed, the next write must return a string, and nothing may raise.
    """
    assert declared_output_paths(text) == []
    assert manifest_path_collisions(f"{SRC}/anything/at/all", text, [(".", SRC)]) == []
    w = _tools()
    assert w.execute("write_file", {"path": "looplab_stages.json", "content": text}).startswith("wrote")
    assert w.execute("write_file", {"path": "train.py", "content": "print(1)\n"}).startswith("wrote")


def test_the_refusal_names_both_ways_out():
    """A refusal naming only the path is answered with a different absolute path. It has to say that
    the DECLARATION is equally movable, and it has to name the mount channel — which is the same
    sentence `read_allowlist.refusal_hint` and the read fence's own refusal give."""
    text = collision_feedback([(f"{SRC}/a/final", "train", "a/final/model.safetensors")], where="c.yaml")
    assert "RELATIVE to the eval workdir" in text
    assert "`data:`/`references:` mount" in text
    assert "Nothing was staged." in text


# --- F1c's third piece: the operator's own argv -----------------------------------------------

def test_an_eval_command_naming_the_source_tree_warns_at_submit_time():
    from looplab.adapters.repo_task import RepoTask, eval_source_tree_command_paths
    task = RepoTask(goal="g", direction="max", editable_path="/srv/repo",
                    eval={"command": ["python", "score.py", "--cfg", "/srv/repo/configs/base.yaml"],
                          "metric": {"kind": "stdout_regex", "pattern": "M: ([0-9.]+)"}})
    warnings = eval_source_tree_command_paths(task)
    assert len(warnings) == 1
    assert "/srv/repo/configs/base.yaml" in warnings[0]
    assert "its OWN materialized copy" in warnings[0]
    # A relative token is the correct spelling and says nothing.
    ok = RepoTask(goal="g", direction="max", editable_path="/srv/repo",
                  eval={"command": ["python", "score.py", "--cfg", "configs/base.yaml"],
                        "metric": {"kind": "stdout_regex", "pattern": "M: ([0-9.]+)"}})
    assert eval_source_tree_command_paths(ok) == []
