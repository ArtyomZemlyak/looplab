"""`Settings.seed_from_run` on the SERVER — the critic's re-check of 2026-09-26, finding by finding.

The launch route confined the seed; three other doors did not. A Replay re-seeds the run from its
recorded setting in a spawned `looplab run` that learned only AFTER the archive whether the seed still
resolved — a source deleted since stranded the run, and a seed a config PUT had pointed anywhere was
read unconfined (HIGH). A saved default seeded every web launch of the server (LOW). And the route's
own containment check admitted a linked `events.jsonl` and the deletion quarantine (LOW).
"""
from __future__ import annotations

import json
import os
import shutil

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


def _refused_before_archiving(response, seeded, before, spawns):
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "replay_seed_invalid", detail
    assert "Clear seed_from_run" in detail["remediation"]
    assert not spawns and (seeded / "events.jsonl").read_bytes() == before
    assert not list(seeded.glob("*.reset-*")), "Replay archived the run before refusing it"
    assert not list(seeded.glob(".looplab-reset-task-*")), "the staged task was left behind"
    return detail


# ------------------------------------------------------------------ Replay (HIGH)

def test_a_replay_whose_seed_is_gone_is_refused_while_the_run_is_intact(root, monkeypatch):
    """Driven: the source run is deleted, then the seeded run is Replayed. The refusal comes before
    the archive, so the run stays listed and readable — and editable, since no reset marker is up:
    clearing the seed is the one per-run edit, and a Replay then relaunches it unseeded."""
    from looplab.serve.routers import control as control_router

    src, seeded = _source_and_seeded(root)
    before = (seeded / "events.jsonl").read_bytes()
    shutil.rmtree(src)
    spawns: list = []
    monkeypatch.setattr(control_router, "_spawn_engine", lambda *a, **kw: spawns.append((a, kw)))
    with TestClient(make_app(root)) as client:
        detail = _refused_before_archiving(client.post("/api/runs/seeded/reset"), seeded, before,
                                           spawns)
        assert "is not a run under this server's runs root" in detail["message"]
        assert client.get("/api/runs/seeded/state").status_code == 200
        generation = http_run_generation(client, "seeded")
        moved = client.put("/api/runs/seeded/config", json={
            "settings": {"seed_from_run": str(root / "elsewhere")},
            "expected_generation": generation})
        assert moved.status_code == 422 and "can't be changed per-run" in moved.text
        cleared = client.put("/api/runs/seeded/config", json={
            "settings": {"seed_from_run": ""}, "expected_generation": generation})
        assert cleared.status_code == 200, cleared.text
        monkeypatch.setattr(control_router, "_spawn_engine", _replacement_spawn(seeded, spawns))
        replay = client.post("/api/runs/seeded/reset")
        assert replay.status_code == 200, replay.text
    (_args, env), = spawns
    assert env["LOOPLAB_SEED_FROM_RUN"] == "", "the cleared seed is what the replacement is told"


def test_a_replay_re_seeds_the_node_the_run_was_born_from(root, monkeypatch):
    from looplab.serve.routers import control as control_router

    src, seeded = _source_and_seeded(root)
    champion = fold(EventStore(src / "events.jsonl").read_all()).best()
    spawns: list = []
    monkeypatch.setattr(control_router, "_spawn_engine", _replacement_spawn(seeded, spawns))
    with TestClient(make_app(root)) as client:
        assert client.post("/api/runs/seeded/reset").status_code == 200
    (_args, env), = spawns
    assert env["LOOPLAB_SEED_FROM_RUN"] == f"{src.resolve()}#{champion.id}"


def test_a_replay_never_reads_a_seed_outside_the_runs_root(root, tmp_path, monkeypatch):
    """The recorded setting is the operator's file, and a hand edit (or a config PUT, before it was
    refused) could point it anywhere; the spawned `looplab run` resolves it UNconfined. The Replay
    asks the server's own rule first."""
    from looplab.serve.routers import control as control_router

    _src, seeded = _source_and_seeded(root)
    outside = tmp_path / "elsewhere" / "src"
    assert _run(outside, "--max-nodes", "2").exit_code == 0
    snapshot = seeded / "config.snapshot.json"
    snapshot.write_text(json.dumps({**json.loads(snapshot.read_text()),
                                    "seed_from_run": f"{outside}#0"}))
    before = (seeded / "events.jsonl").read_bytes()
    spawns: list = []
    monkeypatch.setattr(control_router, "_spawn_engine", lambda *a, **kw: spawns.append((a, kw)))
    with TestClient(make_app(root)) as client:
        detail = _refused_before_archiving(client.post("/api/runs/seeded/reset"), seeded, before,
                                           spawns)
    # What was asked is the run's own recorded setting (its config GET shows it); what the server
    # probed for it is not echoed.
    assert f"{outside}#0" in detail["message"] and "looked for" not in detail["message"]


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
