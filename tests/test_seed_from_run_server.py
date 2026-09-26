"""`Settings.seed_from_run` on the SERVER — the critic's re-check of 2026-09-26, finding by finding.

The launch route confined the seed; three other doors did not. A Replay re-seeded the run by
resolving its recorded setting again in the spawned `looplab run`, AFTER the archive — a source
deleted since stranded the run, one Replayed since handed back a different experiment under the same
node id, a config PUT or the server's environment pointed it anywhere (HIGH, then MEDIUM on the
re-check). A Replay now freezes the row the run was born with and its child appends it verbatim. A
saved default seeded every web launch of the server (LOW), and the route's own containment check
admitted a linked `events.jsonl` and the deletion quarantine (LOW).
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from factories import http_run_generation  # noqa: E402
from looplab.cli import app  # noqa: E402
from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.events.replay import fold  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_MEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("LOOPLAB_KNOWLEDGE_DIR", str(tmp_path / "kn"))
    return tmp_path / "runs"


def _run(out, *extra, direction="min", genesis=False):
    return CliRunner().invoke(app, [
        "run", *([] if genesis else ["--no-genesis"]), "--kind", "quadratic",
        "--goal", "min (x-3)^2", "--direction", direction, "--backend", "toy", *extra,
        "--out", str(out)])


def _source_and_seeded(root):
    src, seeded = root / "src", root / "seeded"
    assert _run(src, "--max-nodes", "3").exit_code == 0
    out = _run(seeded, "--max-nodes", "2", "-s", f"seed_from_run={src}")
    assert out.exit_code == 0, out.output
    return src, seeded


def _launch(seed, **task):
    return {"run_id": "web-seeded",
            "task": {"benchmark": "quadratic", "goal": "minimize the objective", "direction": "min",
                     **task},
            "settings": {"seed_from_run": seed}}


def _replacement_spawn(rd, spawns):
    """A `_spawn_engine` stand-in that records the frozen launch and writes the replacement
    generation, as a real Replay child does (`tests/test_server.py::_replacement_spawn`)."""
    from looplab.core.run_reset import RUN_RESET_OPERATION_ENV

    def spawn(args, env=None, **_kwargs):
        spawns.append((list(args), dict(env or {})))
        previous = os.environ.get(RUN_RESET_OPERATION_ENV)
        os.environ[RUN_RESET_OPERATION_ENV] = (env or {}).get(RUN_RESET_OPERATION_ENV, "")
        try:
            EventStore(rd / "events.jsonl").append("run_started", {
                "run_id": rd.name, "task_id": "replacement", "goal": "new", "direction": "min"})
        finally:
            if previous is None:
                os.environ.pop(RUN_RESET_OPERATION_ENV, None)
            else:
                os.environ[RUN_RESET_OPERATION_ENV] = previous
        return 4242
    return spawn


def _birth_row(rd):
    return dict(EventStore(rd / "events.jsonl").read_all()[0].data)


def _frozen_row(env):
    """What the Replay handed its child: `seed_from_run` blank — it resolves nothing — and, when
    the run is re-seeded, the path of the frozen row."""
    from looplab.core.run_reset import RUN_RESET_SEED_ENV

    assert env["LOOPLAB_SEED_FROM_RUN"] == "", "a Replay child never resolves a seed by path"
    path = env.get(RUN_RESET_SEED_ENV)
    if path is None:
        return None
    row = json.loads(open(path, encoding="utf-8").read())
    assert isinstance(row, dict), f"a staged seed is a ROW, never {row!r}"
    return row


# ------------------------------------------------------------------ Replay

def test_a_replay_re_seeds_from_the_birth_row_even_after_the_source_is_gone(root, monkeypatch):
    """HIGH (critic 2026-09-26, driven): the Replay's child resolved the recorded spec after the
    archive, and a deleted source stranded the run. The row the run was born with is frozen before
    the archive and handed over; nothing is resolved, so the source's absence changes nothing."""
    from looplab.serve.routers import control as control_router

    src, seeded = _source_and_seeded(root)
    born = _birth_row(seeded)
    shutil.rmtree(src)
    spawns: list = []
    monkeypatch.setattr(control_router, "_spawn_engine", _replacement_spawn(seeded, spawns))
    with TestClient(make_app(root)) as client:
        assert client.post("/api/runs/seeded/reset").status_code == 200
    (_args, env), = spawns
    assert _frozen_row(env) == born


def test_a_replay_re_seeds_the_experiment_even_after_the_source_was_replayed(root, monkeypatch):
    """MEDIUM (critic 2026-09-26, driven): node ids restart in a replayed source, so the recorded
    `<dir>#<node>` named a different experiment — and the verdict still said `same`. The frozen row
    carries the born-with code whatever the source holds now."""
    from looplab.serve.routers import control as control_router

    src, seeded = _source_and_seeded(root)
    born = _birth_row(seeded)
    shutil.rmtree(src)                               # the source regenerated: same ids, new code
    regenerated = EventStore(src / "events.jsonl")
    (src / "events.jsonl").parent.mkdir(parents=True, exist_ok=True)
    regenerated.append("run_started", {"run_id": "src", "task_id": "toy", "goal": "g",
                                       "direction": "min"})
    for nid in range(born["origin"]["node_id"] + 1):
        regenerated.append("node_created", {
            "node_id": nid, "parent_ids": [], "operator": "draft",
            "idea": {"operator": "draft", "params": {"x": 7.5}, "rationale": "regenerated"},
            "code": "print('REGENERATED')\n"})
        regenerated.append("node_evaluated", {"node_id": nid, "generation": 0, "metric": 20.25,
                                              "violations": []})
    spawns: list = []
    monkeypatch.setattr(control_router, "_spawn_engine", _replacement_spawn(seeded, spawns))
    with TestClient(make_app(root)) as client:
        assert client.post("/api/runs/seeded/reset").status_code == 200
    (_args, env), = spawns
    frozen = _frozen_row(env)
    assert frozen == born and "REGENERATED" not in frozen["code"]


def test_a_replay_never_reads_the_snapshots_spec_nor_the_servers_environment(
        root, tmp_path, monkeypatch):
    """A hand-edited snapshot pointing outside the runs root, and — MEDIUM on the re-check — a
    server whose environment carries `LOOPLAB_SEED_FROM_RUN` for a run whose snapshot predates the
    field: neither reaches the child. A seeded run re-seeds its birth row; an unseeded one nothing."""
    from looplab.serve.routers import control as control_router

    src, seeded = _source_and_seeded(root)
    born = _birth_row(seeded)
    outside = tmp_path / "elsewhere" / "src"
    assert _run(outside, "--max-nodes", "2").exit_code == 0
    snapshot = seeded / "config.snapshot.json"
    snapshot.write_text(json.dumps({**json.loads(snapshot.read_text()),
                                    "seed_from_run": f"{outside}#0"}))
    plain = root / "plain"
    assert _run(plain, "--max-nodes", "2").exit_code == 0
    legacy = json.loads((plain / "config.snapshot.json").read_text())
    legacy.pop("seed_from_run")                  # written before the field existed
    (plain / "config.snapshot.json").write_text(json.dumps(legacy))
    hand = root / "hand"                         # never seeded; its snapshot edited to name one
    assert _run(hand, "--max-nodes", "2").exit_code == 0
    edited = json.loads((hand / "config.snapshot.json").read_text())
    (hand / "config.snapshot.json").write_text(json.dumps({**edited,
                                                           "seed_from_run": f"{src}#0"}))
    monkeypatch.setenv("LOOPLAB_SEED_FROM_RUN", "src")
    spawns: list = []
    for rd in (seeded, plain, hand):
        monkeypatch.setattr(control_router, "_spawn_engine", _replacement_spawn(rd, spawns))
        with TestClient(make_app(root)) as client:
            if rd is plain:
                assert client.get("/api/runs/plain/config").json()["seed_from_run"] == ""
            assert client.post(f"/api/runs/{rd.name}/reset").status_code == 200, rd
    (_a, seeded_env), (_b, plain_env), (_c, hand_env) = spawns
    assert _frozen_row(seeded_env) == born
    assert _frozen_row(plain_env) is None
    assert _frozen_row(hand_env) is None, "a run is re-seeded only with a row it was born with"


def test_a_cleared_seed_replays_unseeded_and_the_ui_can_clear_it(root, monkeypatch):
    """The one per-run edit is CLEARING the seed — by `""`, `null` or the per-run form's blank
    (MEDIUM on the re-check: `null` was a 422 and the form refused a blank) — and a Replay then
    starts unseeded; any other value is refused."""
    from looplab.serve.routers import control as control_router

    _src, seeded = _source_and_seeded(root)
    spawns: list = []
    monkeypatch.setattr(control_router, "_spawn_engine", _replacement_spawn(seeded, spawns))
    with TestClient(make_app(root)) as client:
        generation = http_run_generation(client, "seeded")
        moved = client.put("/api/runs/seeded/config", json={
            "settings": {"seed_from_run": str(root / "elsewhere")},
            "expected_generation": generation})
        assert moved.status_code == 422 and "can't be changed per-run" in moved.text
        cleared = client.put("/api/runs/seeded/config", json={
            "settings": {"seed_from_run": None}, "expected_generation": generation})
        assert cleared.status_code == 200, cleared.text
        assert json.loads((seeded / "config.snapshot.json").read_text())["seed_from_run"] == ""
        assert client.post("/api/runs/seeded/reset").status_code == 200
    (_args, env), = spawns
    assert _frozen_row(env) is None, "a cleared seed is not re-seeded"


def test_a_replay_child_appends_the_frozen_row_and_resolves_nothing(root, monkeypatch):
    """The CHILD half, driven through the real CLI with the Replay's two variables: the first row
    is the frozen one, byte for byte, the snapshot records its spec, and the source — deleted —
    is never looked up."""
    from looplab.core.run_reset import RUN_RESET_OPERATION_ENV, RUN_RESET_SEED_ENV

    src, seeded = _source_and_seeded(root)
    born = _birth_row(seeded)
    shutil.rmtree(src)
    frozen = root / ".frozen-seed.json"
    frozen.write_text(json.dumps(born))
    monkeypatch.setenv(RUN_RESET_OPERATION_ENV, "0" * 8 + "-0000-4000-8000-" + "0" * 12)
    monkeypatch.setenv(RUN_RESET_SEED_ENV, str(frozen))
    child = root / "child"
    out = _run(child, "--max-nodes", "2")
    assert out.exit_code == 0, out.output
    assert "re-seeded (Replay)" in out.output
    first = EventStore(child / "events.jsonl").read_all()[0]
    assert first.type == "inject_node" and dict(first.data) == born
    assert (json.loads((child / "config.snapshot.json").read_text())["seed_from_run"]
            == born["origin"]["run_dir"] + "#" + str(born["origin"]["node_id"]))
    monkeypatch.delenv(RUN_RESET_OPERATION_ENV)      # outside a Replay the variable is inert
    stray = root / "stray"
    assert _run(stray, "--max-nodes", "2").exit_code == 0
    assert EventStore(stray / "events.jsonl").read_all()[0].type != "inject_node"


def test_the_frozen_row_is_immutable_and_named_by_its_operation(root, monkeypatch):
    """The receipt pins the staged row by name and digest, both or neither, and never lets a later
    save move either — the child appends what these name."""
    from looplab.serve.reset_transaction import (
        ResetReceiptError, load_reset_receipt, save_reset_receipt)
    from looplab.serve.routers import control as control_router

    _src, seeded = _source_and_seeded(root)
    spawns: list = []
    monkeypatch.setattr(control_router, "_spawn_engine", _replacement_spawn(seeded, spawns))
    with TestClient(make_app(root)) as client:
        assert client.post("/api/runs/seeded/reset").status_code == 200
    path, = root.glob(".looplab-reset-receipt-*.json")
    receipt = load_reset_receipt(path)
    assert receipt["seed_stage"] == f".looplab-reset-seed-{receipt['id']}.json"
    assert (seeded / receipt["seed_stage"]).is_file()
    original = path.read_bytes()
    # The SHAPE, read on the way in — each case alone, so no other rule answers for it.
    for bad in ({k: v for k, v in receipt.items() if k != "seed_digest"},    # both or neither
                {k: v for k, v in receipt.items() if k != "seed_stage"},
                {**receipt, "seed_stage": ".looplab-reset-seed-other.json"},
                {**receipt, "seed_digest": "not-a-digest"}):
        path.write_text(json.dumps(bad))
        with pytest.raises(ResetReceiptError):
            load_reset_receipt(path)
    path.write_bytes(original)
    # …and IMMUTABLE on the way out: a well-formed digest still may not move.
    with pytest.raises(ResetReceiptError, match="immutable"):
        save_reset_receipt(path, {**receipt, "seed_digest": "0" * 64})
    assert load_reset_receipt(path) == receipt


def test_a_frozen_row_changed_after_the_commit_is_never_appended(root, monkeypatch):
    from looplab.serve import reset_route
    from looplab.serve.routers import control as control_router

    _src, seeded = _source_and_seeded(root)
    spawns: list = []
    monkeypatch.setattr(control_router, "_spawn_engine", _replacement_spawn(seeded, spawns))
    real = reset_route._frozen_launch

    def tampered(srv, rd, record, **kwargs):
        (rd / record["seed_stage"]).write_text(json.dumps({"forged": True}))
        return real(srv, rd, record, **kwargs)

    monkeypatch.setattr(reset_route, "_frozen_launch", tampered)
    with TestClient(make_app(root)) as client:
        answer = client.post("/api/runs/seeded/reset")
    assert answer.status_code == 425 and "frozen seed row changed" in answer.text, answer.text
    assert spawns == []


@pytest.mark.parametrize("failure", ["staging", "publication"])
def test_a_replay_refused_before_its_commit_leaves_no_staged_row(root, monkeypatch, failure):
    """Staging the row failed after it became visible, or the writer fence could not be published:
    nothing is durable, so neither staged file may stay behind in the run."""
    from looplab.core.run_reset import RunResetStorageError
    from looplab.serve import reset_route

    _src, seeded = _source_and_seeded(root)
    if failure == "staging":
        real = reset_route.strict_atomic_write_bytes

        def write_then_fail(path, data, *args, **kwargs):
            real(path, data, *args, **kwargs)
            if Path(path).name.startswith(".looplab-reset-seed-"):
                raise OSError("parent fsync failed")

        monkeypatch.setattr(reset_route, "strict_atomic_write_bytes", write_then_fail)
        code = "replay_seed_unstaged"
    else:
        def refuse(*_args, **_kwargs):
            raise RunResetStorageError("fence unavailable")

        monkeypatch.setattr(reset_route, "publish_run_reset_marker", refuse)
        code = "reset_outcome_unknown"
    with TestClient(make_app(root)) as client:
        answer = client.post("/api/runs/seeded/reset")
    assert answer.status_code == 503 and code in answer.text, answer.text
    assert (seeded / "events.jsonl").is_file(), "refused before the archive"
    assert sorted(p.name for p in seeded.glob(".looplab-reset-*")) == []


# ------------------------------------------------------------------ the launch route

def test_the_launch_route_opens_a_source_only_by_the_servers_own_run_rule(root):
    """LOW (critic 2026-09-26, driven): the containment check admitted a directory whose
    `events.jsonl` is a link and the deletion quarantine; a NUL answered with a 500."""
    src = root / "src"
    assert _run(src, "--max-nodes", "2").exit_code == 0
    linked = root / "linked"
    linked.mkdir()
    os.symlink(src / "events.jsonl", linked / "events.jsonl")
    quarantine = root / ".looplab-delete-quarantine-0123"
    shutil.copytree(src, quarantine)
    client = TestClient(make_app(root))
    for spec in ("linked", str(linked), quarantine.name, str(quarantine), "src\x00", "/"):
        verdict = client.post("/api/validate", json=_launch(spec)).json()
        assert verdict["ready"] is False and verdict["code"] == "invalid_seed", (spec, verdict)
    assert client.post("/api/validate", json=_launch("src")).json()["ready"] is True


def test_a_saved_or_ambient_seed_never_seeds_a_launch(root, monkeypatch):
    """LOW (critic 2026-09-26): a saved `seed_from_run` made one run the start of every web launch.
    The global store neither saves nor loads one, and the launch takes the seed only from its own
    layers — this server's environment included, which `Settings()` reads under every launch."""
    src = root / "src"
    assert _run(src, "--max-nodes", "2").exit_code == 0
    client = TestClient(make_app(root))
    refused = client.put("/api/settings", json={"settings": {"seed_from_run": "src"}})
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "launch_only_setting"
    assert client.put("/api/settings", json={"settings": {"seed_from_run": "",
                                                          "timeout": 45.0}}).status_code == 200
    stored = json.loads((root / "ui_settings.json").read_text())
    assert "seed_from_run" not in stored
    (root / "ui_settings.json").write_text(json.dumps({**stored, "seed_from_run": "src"}))
    assert "seed_from_run" not in client.get("/api/settings").json()["overrides"]
    monkeypatch.setenv("LOOPLAB_SEED_FROM_RUN", "src")
    body = {k: v for k, v in _launch("").items() if k != "settings"}
    preview = client.post("/api/start/preflight", json=body).json()
    assert preview["ok"] is True and preview["preview"]["settings"]["seed_from_run"] == ""
    assert not any(w.startswith("seeded from run") for w in preview["warnings"])
    # …while the launch's own layer still seeds.
    named = client.post("/api/start/preflight", json=_launch("src")).json()
    assert named["preview"]["settings"]["seed_from_run"].startswith(str(src.resolve()))


def test_a_blank_seed_is_off_on_every_surface(root):
    """MEDIUM (critic 2026-09-26, driven): the web preflight read a whitespace-only seed as off and
    the spawned `looplab run` read the same text as a spec and refused it — after the launch."""
    blank = root / "blank"
    out = _run(blank, "--max-nodes", "2", "-s", "seed_from_run=   ")
    assert out.exit_code == 0, out.output
    assert "inject_node" not in {e.type for e in EventStore(blank / "events.jsonl").read_all()}
    assert json.loads((blank / "config.snapshot.json").read_text())["seed_from_run"] == ""
    preview = TestClient(make_app(root)).post("/api/start/preflight", json=_launch("   ")).json()
    assert preview["ok"] is True and preview["preview"]["settings"]["seed_from_run"] == ""


# ------------------------------------------------------------------ the direction, both surfaces

def test_a_champion_across_the_direction_is_refused_on_both_surfaces(root):
    """LOW (critic 2026-09-26): the refusal's mutants survived — no test drove it through a real
    surface. And with the direction settled by the operator, the CLI refuses BEFORE Genesis: this
    box has no model, so a refusal after it would read as Genesis failing to reach one."""
    src = root / "src"
    assert _run(src, "--max-nodes", "2").exit_code == 0
    for genesis in (False, True):
        out = _run(root / f"max-{genesis}", "--max-nodes", "2", "-s", f"seed_from_run={src}",
                   direction="max", genesis=genesis)
        assert out.exit_code == 2, out.output
        assert "minimizes its metric and this run maximizes it" in out.output
        assert "Genesis" not in out.output
    verdict = TestClient(make_app(root)).post(
        "/api/validate", json=_launch("src", direction="max")).json()
    assert verdict["ready"] is False and verdict["code"] == "invalid_seed"
    assert "minimizes its metric and this run maximizes it" in verdict["message"]


# ------------------------------------------------------------------ a fact of birth (LOW)

def test_a_later_run_of_the_directory_keeps_the_seed_the_run_was_born_from(root):
    """LOW (critic 2026-09-26): `looplab run` on an existing seeded run wrote ITS invocation's value
    into the snapshot a Replay reads — dropping the seed, or pointing it at a run this one was never
    seeded from; and on an unseeded run, inventing one."""
    src, seeded = _source_and_seeded(root)
    born = json.loads((seeded / "config.snapshot.json").read_text())["seed_from_run"]
    assert born.startswith(str(src.resolve()) + "#")
    for extra in ((), ("-s", f"seed_from_run={src}#0")):
        assert _run(seeded, "--max-nodes", "3", *extra).exit_code == 0
        assert json.loads((seeded / "config.snapshot.json").read_text())["seed_from_run"] == born
    plain = root / "plain"
    assert _run(plain, "--max-nodes", "2").exit_code == 0
    assert _run(plain, "--max-nodes", "3", "-s", f"seed_from_run={src}").exit_code == 0
    assert json.loads((plain / "config.snapshot.json").read_text())["seed_from_run"] == ""
    # …and a seed the operator CLEARED stays cleared (critic 2026-09-26: the next `looplab run`
    # wrote the born-with spec back over it, and the Replay seeded again).
    snapshot = seeded / "config.snapshot.json"
    snapshot.write_text(json.dumps({**json.loads(snapshot.read_text()), "seed_from_run": ""}))
    assert _run(seeded, "--max-nodes", "4").exit_code == 0
    assert json.loads(snapshot.read_text())["seed_from_run"] == ""


# ------------------------------------------------------------------ critic 2026-09-26, re-check

def test_the_locator_is_the_servers_whole_run_rule(root, tmp_path):
    """Each clause of `serve/launch.py::server_seed_locator`, driven (its mutants survived): a
    source whose `.commands` is a link is refused (`validate_paths`), and a runs root reached
    through a symlink admits an absolute spec under the real root (the parent is RESOLVED)."""
    src = root / "src"
    assert _run(src, "--max-nodes", "2").exit_code == 0
    linked = root / "linkedcmds"
    shutil.copytree(src, linked)
    shutil.rmtree(linked / ".commands", ignore_errors=True)
    os.symlink(tmp_path, linked / ".commands")
    client = TestClient(make_app(root))
    verdict = client.post("/api/validate", json=_launch("linkedcmds")).json()
    assert verdict["ready"] is False and verdict["code"] == "invalid_seed", verdict
    alias = tmp_path / "alias-root"
    os.symlink(root, alias)
    via_alias = TestClient(make_app(alias))
    # Spelled either way, through either root: only a RESOLVED parent is the same directory.
    for server in (via_alias, client):
        for spec in (str(src), str(alias / "src")):
            verdict = server.post("/api/validate", json=_launch(spec)).json()
            assert verdict["ready"] is True, (spec, verdict)


def test_a_task_files_settings_layer_seeds_a_web_launch(root):
    """A launch fact comes from the launch's OWN layers — the body's settings and the task file's
    `settings:` block both (the task-file half's mutant survived)."""
    src = root / "src"
    assert _run(src, "--max-nodes", "2").exit_code == 0
    task_file = root / "seeded-task.json"
    task_file.write_text(json.dumps({
        "task": {"benchmark": "quadratic", "goal": "minimize the objective", "direction": "min"},
        "settings": {"seed_from_run": "src"}}))
    body = {"run_id": "from-file", "task_file": str(task_file)}
    preview = TestClient(make_app(root)).post("/api/start/preflight", json=body).json()
    assert preview["ok"] is True, preview
    assert preview["preview"]["settings"]["seed_from_run"].startswith(str(src.resolve()) + "#")


def test_the_settings_page_never_shows_the_servers_ambient_seed(root, monkeypatch):
    """NIT (critic 2026-09-26): with `LOOPLAB_SEED_FROM_RUN` in the server's environment the global
    settings GET showed it as the resolved value and the default — a value no launch uses, which
    the form then echoed into a refused save."""
    monkeypatch.setenv("LOOPLAB_SEED_FROM_RUN", "src")
    client = TestClient(make_app(root))
    body = client.get("/api/settings").json()
    assert body["settings"]["seed_from_run"] == "" and body["defaults"]["seed_from_run"] == ""
    assert client.put("/api/settings", json={"settings": body["settings"]}).status_code == 200


def test_the_pre_genesis_direction_check_reads_only_a_settled_direction(root, monkeypatch):
    """LOW (critic 2026-09-26, driven): mlebench_real's `direction: auto` was compared as a
    direction ("this run auto it"). Only `min`/`max` is settled before the adapter runs."""
    import looplab.cli.run_cmds as run_cmds

    asked = []

    def spy(spec, out, *, direction=None, locate=None):
        asked.append(direction)
        raise SystemExit(0)

    monkeypatch.setattr(run_cmds, "resolve_seed", spy)
    for direction in ("auto", "max"):
        task_file = root / f"task-{direction}.json"
        root.mkdir(parents=True, exist_ok=True)
        task_file.write_text(json.dumps({"task": {"kind": "quadratic", "goal": "g",
                                                  "direction": direction}}))
        CliRunner().invoke(app, ["run", str(task_file), "--no-genesis", "--backend", "toy",
                                 "-s", "seed_from_run=src", "--out", str(root / direction)])
    assert asked == [None, "max"], asked
