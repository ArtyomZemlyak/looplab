"""`Settings.seed_from_run` (doc 67 67.2): a NEW run's first experiment is a prior run's node.

Rounds were chained by hand and each started from scratch. The launch form imports the prior
champion (or a named node) as an operator inject the engine serves before its first creation turn,
with the evaluation-contract receipt beside it. Driven through the real CLI on real toy runs, and on
crafted source logs for the refusals a toy run cannot reach; the critic pass of 2026-09-26 (two HIGH,
six MEDIUM, each driven) is pinned finding by finding.
"""
from __future__ import annotations

import json
import os

import pytest
from typer.testing import CliRunner

from looplab.cli import app
from looplab.core.errors import ConfigRefusal
from looplab.core.models import Idea, durable_idea_payload
from looplab.engine.seed_from_run import resolve_seed, seed_intent, seed_verdict
from looplab.events.eventstore import EventStore
from looplab.events.node_import import node_import_payload
from looplab.events.replay import fold

_RUN = ["run", "--no-genesis", "--kind", "quadratic", "--goal", "min (x-3)^2", "--direction", "min",
        "--backend", "toy"]


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_MEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("LOOPLAB_KNOWLEDGE_DIR", str(tmp_path / "kn"))
    return tmp_path


def _run(out, *extra, genesis=False):
    head = _RUN if not genesis else [a for a in _RUN if a != "--no-genesis"]
    return CliRunner().invoke(app, [*head, *extra, "--out", str(out)])


def _source(tmp_path):
    src = tmp_path / "runs" / "prior"
    assert _run(src, "--max-nodes", "5").exit_code == 0
    return src, fold(EventStore(src / "events.jsonl").read_all())


def _crafted(root, name, *nodes, direction="min", extra=(), eval_env=None, holdout_fraction=None):
    """A source run written row by row: each node is a dict (`id`, `operator`, `code`, `files`,
    `deleted`, `metric`) — the shapes a toy run cannot produce."""
    rd = root / name
    rd.mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": name, "task_id": "toy", "goal": "g",
                                 "direction": direction,
                                 **({"eval_env": eval_env} if eval_env else {}),
                                 **({"holdout_fraction": holdout_fraction}
                                    if holdout_fraction is not None else {})})
    for node in nodes:
        operator = node.get("operator", "draft")
        store.append("node_created", {
            "node_id": node["id"], "parent_ids": [], "operator": operator,
            "idea": durable_idea_payload(Idea(operator=operator, params={"x": 0.5}, rationale="r")),
            "code": node.get("code", "print(1)\n"),
            **({"files": node["files"]} if "files" in node else {}),
            **({"deleted": node["deleted"]} if "deleted" in node else {})})
        if node.get("metric") is not None:
            store.append("node_evaluated", {"node_id": node["id"], "generation": 0,
                                            "metric": node["metric"], "violations": []})
    for etype, data in extra:
        store.append(etype, data)
    return rd


def test_the_prior_champion_is_the_new_runs_first_experiment(isolated):
    src, prior = _source(isolated)
    champion = prior.best()
    new = isolated / "runs" / "next"
    out = _run(new, "--max-nodes", "3", "-s", f"seed_from_run={src}")
    assert out.exit_code == 0, out.output
    assert f"seeded from run prior #{champion.id}" in out.output
    events = EventStore(new / "events.jsonl").read_all()
    assert events[0].type == "inject_node", "the intent precedes the engine's own setup"
    state = fold(events)
    seed = state.nodes[0]
    assert seed.origin["run_id"] == "prior" and seed.origin["node_id"] == champion.id
    assert seed.origin["seed_from_run"] is True and seed.origin["eval_contract"] == "unknown"
    assert seed.origin["source_attempt"] == champion.attempt
    assert seed.origin["run_dir"] == str(src.resolve())
    assert seed.idea.params == champion.idea.params and seed.code == champion.code
    # Evaluated HERE, under this run's protocol; the source's number is provenance only.
    assert seed.metric is not None and seed.origin["metric"] == champion.robust_metric
    assert state.injects_done == 1
    # The setting is recorded as what it resolved to, so a Replay seeds the same node.
    snapshot = json.loads((new / "config.snapshot.json").read_text())
    assert snapshot["seed_from_run"] == f"{src.resolve()}#{champion.id}"


def test_a_seeded_launch_is_a_started_run_for_the_web_start_record(isolated):
    """HIGH (critic 2026-09-26, driven): the seed's row at seq 0 made `has_first_run_started` refuse
    the log, so every seeded web launch was recorded `failed_after_spawn` with its spend unknown."""
    from looplab.serve.start_record import has_first_run_started

    src, _prior = _source(isolated)
    new = isolated / "runs" / "seeded"
    assert _run(new, "--max-nodes", "2", "-s", f"seed_from_run={src}").exit_code == 0
    assert EventStore(new / "events.jsonl").read_all()[0].type == "inject_node"
    assert has_first_run_started(new) is True


def test_the_inject_is_appended_after_the_snapshots_it_is_judged_against(isolated, monkeypatch):
    """A crash between the two leaves a log whose snapshots exist (`resume` re-enters it and serves
    the seed once), never an intent with no task record behind it."""
    import looplab.cli.run_cmds as run_cmds

    src, _prior = _source(isolated)
    published = []
    real_publish = run_cmds._publish_run_snapshots
    monkeypatch.setattr(run_cmds, "_publish_run_snapshots",
                        lambda *a, **k: (published.append(True), real_publish(*a, **k))[1])
    real_append = EventStore.append
    order = []

    def _append(self, etype, data, *a, **k):
        if etype == "inject_node":
            order.append(bool(published))
        return real_append(self, etype, data, *a, **k)

    monkeypatch.setattr(EventStore, "append", _append)
    assert _run(isolated / "runs" / "ordered", "--max-nodes", "2",
                "-s", f"seed_from_run={src}").exit_code == 0
    assert order == [True]


def test_a_named_node_a_sibling_id_and_a_rerun_that_never_re_seeds(isolated):
    src, prior = _source(isolated)
    other = next(n for n in prior.nodes.values() if n.id != prior.best().id)
    new = isolated / "runs" / "named"
    # `prior` alone is a SIBLING run id under the new run's own runs root (the web UI's ids).
    assert _run(new, "--max-nodes", "2", "-s", f"seed_from_run=prior#{other.id}").exit_code == 0
    first = fold(EventStore(new / "events.jsonl").read_all())
    assert first.nodes[0].origin["node_id"] == other.id
    # A `run` on the SAME directory continues it and does not import a second time.
    again = _run(new, "--max-nodes", "3", "-s", f"seed_from_run=prior#{other.id}")
    assert again.exit_code == 0, again.output
    assert "ignored: this run directory already has events" in again.output
    events = EventStore(new / "events.jsonl").read_all()
    assert sum(1 for e in events if e.type == "inject_node") == 1


def test_an_existing_run_is_continued_even_once_its_seed_source_is_gone(isolated):
    """MEDIUM (critic 2026-09-26, driven): the seed was resolved before `run` knew the directory
    already held a run, so re-running a seeded run whose source had since been removed was REFUSED
    ("no run directory"), the log unchanged. Nothing is resolved for an existing run."""
    import shutil

    src, _prior = _source(isolated)
    new = isolated / "runs" / "cont"
    assert _run(new, "--max-nodes", "2", "-s", f"seed_from_run={src}").exit_code == 0
    shutil.rmtree(src)
    again = _run(new, "--max-nodes", "3", "-s", f"seed_from_run={src}")
    assert again.exit_code == 0, again.output
    assert "ignored: this run directory already has events" in again.output


def test_a_mistyped_seed_costs_no_genesis_call(isolated, monkeypatch):
    """LOW (critic 2026-09-26): Genesis — a paid call — ran before the seed was resolved, so a
    mistyped seed with `--goal` still paid for it. It is resolved first now."""
    import looplab.engine.genesis as genesis

    monkeypatch.setattr(genesis, "author_task", lambda *a, **k: pytest.fail("Genesis was called"))
    new = isolated / "runs" / "nogenesis"
    out = _run(new, "--max-nodes", "2", "-s", "seed_from_run=nowhere", genesis=True)
    assert out.exit_code == 2, out.output
    assert "seed_from_run: no run directory at 'nowhere'" in out.output
    assert not new.exists()


@pytest.mark.parametrize("spec,words", [
    ("nowhere", "no run directory at 'nowhere'"),
    ("prior#99", "has no node #99"),
    ("prior#x", "is not an integer id"),
    ("#3", "empty — name a run directory"),
])
def test_a_bad_seed_is_refused_before_anything_is_created(isolated, spec, words):
    _source(isolated)
    new = isolated / "runs" / "refused"
    out = _run(new, "--max-nodes", "2", "-s", f"seed_from_run={spec}")
    assert out.exit_code == 2, out.output
    assert words in out.output
    assert not new.exists(), "refused at launch, before the run directory was created"


def test_a_run_cannot_seed_itself_or_from_a_withdrawn_node(isolated):
    src, prior = _source(isolated)
    with pytest.raises(ConfigRefusal, match="this run's own directory"):
        resolve_seed(str(src), src)
    store = EventStore(src / "events.jsonl")
    store.append("node_tombstoned", {"node_ids": [prior.best().id]})
    with pytest.raises(ConfigRefusal, match="tombstoned"):
        resolve_seed(f"{src}#{prior.best().id}", isolated / "runs" / "x")


def test_an_aborted_node_and_a_run_with_no_champion_are_refused(isolated):
    runs = isolated / "runs"
    aborted = _crafted(runs, "aborted", {"id": 0, "metric": 1.0},
                       extra=[("node_abort", {"node_id": 0, "generation": 0})])
    with pytest.raises(ConfigRefusal, match="node #0 of run aborted is aborted"):
        resolve_seed(f"{aborted}#0", runs / "x")
    pending = _crafted(runs, "pending", {"id": 0})
    with pytest.raises(ConfigRefusal, match="has no champion to seed from"):
        resolve_seed(str(pending), runs / "x")


def test_a_corrupt_source_log_is_refused_rather_than_read_as_a_prefix(isolated):
    """MEDIUM (critic 2026-09-26, driven): the fold stops at the first corrupt line, so a corrupt
    source silently seeded an EARLIER node as its "champion" — where `looplab resume` refuses the
    same log."""
    runs = isolated / "runs"
    src = _crafted(runs, "corrupt", {"id": 0, "metric": 5.0}, {"id": 1, "metric": 1.0})
    lines = (src / "events.jsonl").read_text().splitlines(keepends=True)
    lines[2] = '{"v": 1, "seq": 2, "ts": 1.0, "type": "node_evalu\n'   # a torn MIDDLE record
    (src / "events.jsonl").write_text("".join(lines))
    with pytest.raises(ConfigRefusal, match="is corrupted at line 3"):
        resolve_seed(str(src), runs / "x")


def test_what_the_engine_would_refuse_is_refused_at_launch(isolated):
    """MEDIUM (critic 2026-09-26, driven): a `debug` champion was reported as seeded, then the
    engine dropped it with `inject_failed`. The engine's own inject validation is asked at launch."""
    runs = isolated / "runs"
    src = _crafted(runs, "dbg", {"id": 0, "operator": "debug", "metric": 1.0})
    with pytest.raises(ConfigRefusal, match="cannot be injected: debug nodes were removed"):
        resolve_seed(str(src), runs / "x")


def test_nothing_to_import_is_refused_and_a_node_that_only_deletes_is_not(isolated):
    runs = isolated / "runs"
    empty = _crafted(runs, "empty", {"id": 0, "code": "", "metric": 1.0})
    with pytest.raises(ConfigRefusal, match="there is nothing to import"):
        resolve_seed(str(empty), runs / "x")
    deletes = _crafted(runs, "deletes", {"id": 0, "code": "", "deleted": ["old.py"], "metric": 1.0})
    assert resolve_seed(str(deletes), runs / "x").payload["deleted"] == ["old.py"]


@pytest.mark.parametrize("name", ["../escape.py", "/etc/passwd", "CON.txt", "dir./x.py", "c:x"])
def test_a_file_name_that_could_leave_a_node_workspace_is_refused(isolated, name):
    """The server's own rule (`events/node_import.py::portable_relative_name`), not a weaker copy
    (critic 2026-09-26: reserved device names and trailing dots passed the old one)."""
    runs = isolated / "runs"
    src = _crafted(runs, "unsafe", {"id": 0, "files": {name: "x"}, "metric": 1.0})
    with pytest.raises(ConfigRefusal, match="outside a node workspace"):
        resolve_seed(str(src), runs / "x")


def test_a_windows_separator_is_carried_in_its_portable_spelling(isolated):
    runs = isolated / "runs"
    src = _crafted(runs, "win", {"id": 0, "files": {"pkg\\mod.py": "x = 1\n"}, "metric": 1.0})
    assert resolve_seed(str(src), runs / "x").payload["files"] == {"pkg/mod.py": "x = 1\n"}


def test_the_spec_grammar(isolated):
    """Surrounding whitespace is not part of the spec; a trailing `#<integer>` names a node when the
    text before it is a run, else the whole text is the path — so a run whose name holds a `#` stays
    addressable (critic 2026-09-26); and the canonical spec resolves to the same node again."""
    runs = isolated / "runs"
    plain = _crafted(runs, "plain", {"id": 0, "metric": 2.0}, {"id": 1, "metric": 1.0})
    hashed = _crafted(runs, "odd#name", {"id": 0, "metric": 3.0})
    assert resolve_seed(f"  {plain}  ", runs / "x").node_id == 1
    assert resolve_seed(f"{plain} #0", runs / "x").node_id == 0
    seed = resolve_seed(str(hashed), runs / "x")
    assert seed.run_dir == hashed.resolve() and seed.node_id == 0 and not seed.named
    again = resolve_seed(seed.canonical_spec, runs / "x")
    assert (again.run_dir, again.node_id, again.named) == (seed.run_dir, 0, True)


def test_the_receipt_is_the_sources_resolved_identity(isolated):
    """LOW (critic 2026-09-26): the receipt recorded the path's last component AS TYPED — a symlink
    recorded `latest`, and a run outside the runs root a bare name the DAG link and the portfolio
    map read as a sibling run."""
    runs = isolated / "runs"
    src = _crafted(runs, "prior", {"id": 0, "metric": 1.0})
    os.symlink(src, runs / "latest")
    task = {"kind": "quadratic", "direction": "min"}
    facts = dict(direction="min", eval_env={}, holdout_fraction=None)
    payload, _v, _n = seed_intent(resolve_seed(str(runs / "latest"), runs / "new"),
                                  runs / "new", task, **facts)
    assert payload["origin"]["run_id"] == "prior"
    assert payload["origin"]["run_dir"] == str(src.resolve())
    elsewhere = _crafted(isolated / "archive", "prior", {"id": 0, "metric": 1.0})
    payload, _v, _n = seed_intent(resolve_seed(str(elsewhere), runs / "new"),
                                  runs / "new", task, **facts)
    assert "run_id" not in payload["origin"], "not a sibling: nothing may link it as one"
    assert payload["origin"]["run_dir"] == str(elsewhere.resolve())


def test_the_receipt_names_a_different_evaluation_contract(isolated):
    """Two runs whose tasks declare different eval commands: the receipt says `different` and names
    the facet, the SOURCE's side first (`engine/eval_contract.py::contract_notice`)."""
    src, _prior = _source(isolated)
    snap = json.loads((src / "task.snapshot.json").read_text())
    (src / "task.snapshot.json").write_text(json.dumps(
        {**snap, "eval": {"command": ["python", "score_v1.py"],
                          "metric": {"kind": "stdout_json", "key": "loss"}}}))
    new = isolated / "runs" / "new"
    task = {**snap, "eval": {"command": ["python", "score_v2.py"],
                             "metric": {"kind": "stdout_json", "key": "loss"}}}
    facts = dict(direction="min", eval_env={}, holdout_fraction=0.25)
    seed = resolve_seed(str(src), new)
    payload, verdict, note = seed_intent(seed, new, task, **facts)
    assert verdict == "different" and "eval command" in note
    assert note.index("score_v1.py") < note.index("score_v2.py"), "the source's command first"
    assert payload["origin"]["eval_contract"] == "different"
    assert payload["origin"]["eval_contract_note"] == note
    same = seed_intent(seed, new, json.loads((src / "task.snapshot.json").read_text()), **facts)
    assert same[1] == "same" and "eval_contract_note" not in same[0]["origin"]


def test_the_champion_is_never_taken_across_the_scale(isolated):
    """MEDIUM (critic 2026-09-26, driven): a max-direction source seeded into a min-direction run
    recorded `same`, the seed being the worst node there. The champion pick is refused; a named
    node is the operator's own pick and its receipt says `different`, and why."""
    runs = isolated / "runs"
    src = _crafted(runs, "maxrun", {"id": 0, "metric": 1.0}, {"id": 1, "metric": 9.0},
                   direction="max")
    with pytest.raises(ConfigRefusal, match="maximizes its metric and this run minimizes it"):
        resolve_seed(str(src), runs / "x", direction="min")
    seed = resolve_seed(f"{src}#1", runs / "x", direction="min")
    verdict, note = seed_verdict(seed, {"kind": "quadratic"}, direction="min", eval_env={},
                                 holdout_fraction=None)
    assert verdict == "different" and "DIFFERENT DIRECTION" in note


def test_run_level_facts_outside_the_contract_make_same_unknown(isolated):
    """MEDIUM (critic 2026-09-26): `eval_env` — the corpus root `NEXT_RUN.md` sets — and the holdout
    split are outside the task contract; a difference is named and `same` is no longer claimed."""
    runs = isolated / "runs"
    src = _crafted(runs, "envrun", {"id": 0, "metric": 1.0},
                   eval_env={"VS_LOCAL_DATA_ROOT": "/data/a"}, holdout_fraction=0.25)
    task = {"kind": "repo", "cmd": ["python", "score.py"]}
    (src / "task.snapshot.json").write_text(json.dumps(task))
    seed = resolve_seed(str(src), runs / "x")
    facts = dict(direction="min", eval_env={"VS_LOCAL_DATA_ROOT": "/data/a"},
                 holdout_fraction=0.25)
    assert seed_verdict(seed, task, **facts) == ("same", "")
    verdict, note = seed_verdict(seed, task, **{**facts, "eval_env": {"VS_LOCAL_DATA_ROOT": "/b"}})
    assert verdict == "unknown" and "eval_env (VS_LOCAL_DATA_ROOT)" in note
    verdict, note = seed_verdict(seed, task, **{**facts, "holdout_fraction": 0.1})
    assert verdict == "unknown" and "holdout_fraction (0.25 there, 0.1 here)" in note


def test_the_server_import_and_the_launch_form_import_the_same_snapshot(isolated):
    """ONE spelling: the server's `import` action and the seed both build their payload with
    `events/node_import.py::node_import_payload` — the launch form adds only its receipt."""
    src, prior = _source(isolated)
    champion = prior.best()
    built = node_import_payload(prior, champion.id, "prior")
    seeded = resolve_seed(str(src), isolated / "runs" / "y").payload
    assert seeded == built
    assert built["origin"] == {"run_id": "prior", "node_id": champion.id,
                               "metric": champion.robust_metric,
                               "source_attempt": champion.attempt}
    assert built["idea"]["rationale"].endswith(f"imported from run prior #{champion.id}")


# ------------------------------------------------------------------ the web start route

@pytest.fixture
def web(isolated):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from looplab.serve.server import make_app

    root = isolated / "runs"
    src = root / "src1"
    assert _run(src, "--max-nodes", "4").exit_code == 0
    return TestClient(make_app(root)), root, src


def _launch(seed):
    return {"run_id": "web-seeded",
            "task": {"benchmark": "quadratic", "goal": "minimize the objective", "direction": "min"},
            "settings": {"seed_from_run": seed}}


@pytest.mark.parametrize("spec", ["nowhere", "/etc", "../runs/src1", "src1#999"])
def test_the_web_route_refuses_a_seed_at_validation(web, spec):
    """MEDIUM (critic 2026-09-26, driven): the start route answered `ready: true` for these, and the
    refusal surfaced only in the spawned engine's stderr — after the run name was taken. LOW: it
    also reached any host path; the route admits only a run of its own runs root."""
    client, root, _src = web
    verdict = client.post("/api/validate", json=_launch(spec)).json()
    assert verdict["ready"] is False and verdict["code"] == "invalid_seed", verdict
    assert verdict["field_errors"] == {"settings.seed_from_run": verdict["message"]}
    assert "/etc" not in verdict["message"] or spec == "/etc", "no probed host path is echoed"
    assert not (root / "web-seeded").exists()


def test_the_web_route_pins_the_seed_it_showed(web):
    """A sibling id or an absolute path inside the runs root is admitted, rewritten to the canonical
    `<run dir>#<node>` — so the spawned `looplab run` seeds exactly the node the preview named — and
    the verdict rides the preview as a warning line, where a web operator sees it."""
    client, _root, src = web
    champion = fold(EventStore(src / "events.jsonl").read_all()).best()
    for spec in ("src1", str(src)):
        body = client.post("/api/start/preflight", json=_launch(spec)).json()
        assert body["ok"] is True, body
        assert body["preview"]["settings"]["seed_from_run"] == f"{src.resolve()}#{champion.id}"
        assert any(w.startswith(f"seeded from run src1 #{champion.id}") for w in body["warnings"])
