"""Doc72 acceptance with real Engine, private UI, scoped stdio MCP and actual CPU SGD.

python -m benchmarks.upstream_smoke --out .tmp/new-upstream-proof
Runs only disposable processes/directories. No model calls, owner checkout writes,
implicit retries or user server changes. Stores complete readable acceptance.json.
Use --confirm to repeat the original experiment through Engine after advancement,
checking its original base/implementation and an exact forced-command retry.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
import uvicorn

from benchmarks._upstream_sgd import TRAIN, SOURCE, GENERAL, SCORE
from benchmarks.claude_harness_smoke import wait_for
from benchmarks.external_session_smoke import Client, until
from looplab.adapters.repo_task import RepoTask, EvalSpec
from looplab.engine.run_lifecycle import engine_alive
from looplab.engine.seed_archive import capture_seed_archive
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.harness.mcp_server import HarnessAPI
from looplab.serve.server import make_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--confirm", action="store_true")
    options = parser.parse_args()
    root = options.out.resolve()
    root.mkdir(parents=True, exist_ok=False)
    source, origin, runs = (root / name for name in ("source", "origin", "runs"))
    for p in (source, origin, runs): p.mkdir()
    for name, text in {"train.py": TRAIN, "score.py": SCORE, "recipe.env": "MOMENTUM=0.0\n", "README.md": "Runner\n"}.items():
        (source / name).write_text(text, encoding="utf8")
    original_bytes = {name: (source / name).read_bytes() for name in ("train.py", "score.py")}
    base = capture_seed_archive(source, origin / "base_snapshots")
    history = EventStore(origin / "events.jsonl")
    history.append("run_started", {"run_id": "origin", "task_id": "fixture", "goal": "Seed CPU SGD", "direction": "min"})
    seed = history.append("workspace_seeded", {"node_id": None, "materialized": [], "base_revision": base})
    task = RepoTask(goal="Generalize a real momentum SGD experiment without changing the original default or scorer", direction="min",
        editable_path=str(source), edit_surface=["train.py", "recipe.env", "README.md"],
        seed_base={"run_dir": str(origin), "event_seq": seed.seq, "digest": base["digest"]},
        eval=EvalSpec(command=[sys.executable, "score.py"], timeout=10,
            stages=[{"name": "train", "role": "training", "check": False, "command": [sys.executable, "train.py"], "timeout": 10},
                    {"name": "score", "command": [sys.executable, "score.py"], "timeout": 10}],
            metric={"kind": "stdout_json", "key": "metric"}, scorer_boundary={"files": ["score.py"]}),
        upstream={"repeats": 2, "tests": [{"name": "syntax", "command": [sys.executable, "-m", "py_compile", "train.py"]}],
            "regressions": [{"name": "prior_recipe", "command": [sys.executable, "train.py"], "artifacts": ["predictions.json"]}]})
    task_path = root / "task.json"; task_path.write_text(task.model_dump_json(), encoding="utf8")
    token, owner = secrets.token_hex(32), secrets.token_hex(32)
    before_env = dict(os.environ)
    os.environ.update(LOOPLAB_HARNESS_TOKEN=token, LOOPLAB_UI_TOKEN=owner,
        LOOPLAB_MEMORY_DIR=str(root / "memory"), LOOPLAB_KNOWLEDGE_DIR=str(root / "knowledge"))
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("LOOPLAB_")}
    child_env.update(PYTHONIOENCODING="utf-8", CUDA_VISIBLE_DEVICES="")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    url, rd = f"http://127.0.0.1:{port}", runs / "demo"
    app = make_app(runs)
    owned_children = []
    original_spawn = app.state.looplab.commands.spawn_engine
    def tracked_spawn(*args, **kwargs):
        pid = original_spawn(*args, **kwargs)
        from looplab.serve.engine_proc import _spawned_engines
        if pid in _spawned_engines: owned_children.append(_spawned_engines[pid])
        return pid
    app.state.looplab.commands.spawn_engine = tracked_spawn
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True); thread.start()
    engine, proof = None, {"model_judgment_tested": False, "scope": "short CPU SGD protocol acceptance"}
    engine_log = (root / "engine.log").open("w", encoding="utf8")
    try:
        flags = {"external_harness": True, "deep_research_every": -1, "report_every": 0, "novelty_mode": "off",
            "foresight": False, "track_hypotheses": False, "concept_pivot": False, "concept_run_base": False,
            "cross_run_concepts": False, "cross_run_curation": False, "reflection_priors": False,
            "lessons_every": 0, "comparative_lessons": False, "concurrent_research": False,
            "train_monitor": False, "asha_live": False, "stage_check_tools": False, "eval_deadline_grace_s": 0,
            "memory_dir": str(root / "memory")}
        argv = [sys.executable, "-m", "looplab.cli", "run", str(task_path), "--out", str(rd), "--backend", "toy", "--max-nodes", "4"]
        for key, value in flags.items(): argv += ["-s", f"{key}={str(value).lower() if isinstance(value, bool) else value}"]
        engine = subprocess.Popen(argv, cwd=root, env=child_env, stdout=engine_log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        with HarnessAPI(url, token).client as http:
            ready = wait_for(lambda: http.get("api/runs/demo/state?observe_only=true"),
                lambda response: response.status_code == 200 and response.json()["state"].get("setup_done")).json()
        generation = ready["generation"]
        proof["generation"] = generation

        async def drive():
            args = StdioServerParameters(command=sys.executable, args=["-c", "from looplab.harness.mcp_server import run_stdio;run_stdio()"],
                env={**child_env, "LOOPLAB_HARNESS_URL": url, "LOOPLAB_HARNESS_TOKEN": token})
            async with stdio_client(args) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                client = Client(session, generation)
                assert (await client.call("capabilities", {}))["protocol_version"] == 4
                await client.read("harness-contract")
                await client.progress()
                await client.read("harness-checkpoints")
                await client.call("phase_info", {"phase_id": "implementation"})
                first = {"idea": {"operator": "draft", "footprint": {"gpus": 0}, "rationale": "Measure actual momentum SGD predictions under the protected scorer"},
                         "files": {"train.py": SOURCE, "recipe.env": "MOMENTUM=0.2\n"}}
                await client.command("inject_node", first, "upstream:source")
                node0 = await client.terminal(0)
                await client.commentary("node", "upstream:source-summary", "Исходный SGD с momentum измерен защищённым scorer. Проверю перенос возможности в общий runner; одного результата недостаточно для вывода о повторяемости.", 0)
                await client.command("pause", {}, "upstream:pause")
                await until(lambda: anyio.to_thread.run_sync(lambda: engine_alive(rd)), lambda alive: alive is False)
                await client.call("phases", {"query": "upstream"})
                await client.call("phase_info", {"phase_id": "upstream"})
                status = await client.call("upstream_status", {"run_id": "demo", "expected_generation": generation})
                assert status["status"] == 200 and not status.get("code"), status
                proposal = {"expected_generation": generation, "action_id": "upstream:proposal", "source_node_id": 0,
                    "expected_base_revision": status["body"]["active_base"]["revision"],
                    "hunk_hashes": [r["hunk_hash"] for r in status["body"]["candidates"]["rows"] if r["path"] == "train.py"],
                    "files": {"train.py": GENERAL, "README.md": "MOMENTUM: original default 0.0; enable MOMENTUM=0.2 in recipe.env.\n"},
                    "deleted": [], "recipe_files": {"recipe.env": "MOMENTUM=0.2\n"}, "recipe_deleted": [],
                    "summary": "Shared momentum flag with original default; no per-node runner duplication",
                    "flag": {"name": "MOMENTUM", "default": "0.0", "enabled": "0.2"}, "documentation_path": "README.md",
                    "critic": {"verdict": "pass", "reason": "Fixture-reviewed generic runner, original default and protected scorer preserved", "reviewer": "acceptance code review"}}
                made = await client.call("upstream_propose", {"run_id": "demo", "body": proposal})
                assert made["status"] == 200 and made["body"]["status"] == "succeeded" and not made.get("code"), made
                check = {"expected_generation": generation, "action_id": "upstream:gate", "proposal_id": made["body"]["proposal_id"]}
                checked = await client.call("upstream_check", {"run_id": "demo", "body": check})
                assert checked["body"]["status"] == "succeeded" and not checked.get("code"), checked
                # Explicit lost-ack recovery: same body returns the same bought result.
                assert (await client.call("upstream_check", {"run_id": "demo", "body": check}))["body"] == checked["body"]
                advance = {"expected_generation": generation, "action_id": "upstream:advance", "proposal_id": check["proposal_id"],
                    "expected_base_revision": proposal["expected_base_revision"], "evidence_token": checked["body"]["evidence_token"]}
                advanced = await client.call("upstream_advance", {"run_id": "demo", "body": advance})
                assert advanced["body"]["status"] == "succeeded", advanced
                assert (await client.call("upstream_advance", {"run_id": "demo", "body": advance}))["body"] == advanced["body"]
                await client.command("resume", {}, "upstream:resume")
                await client.progress()
                second = {"idea": {"operator": "improve", "rationale": "Tune shared momentum", "footprint": {"gpus": 0}}, "parent_ids": [0],
                    "parent_generations": {"0": 0}, "files": {"train.py": SOURCE, "recipe.env": "MOMENTUM=0.3\n"}}
                await client.command("inject_node", second, "upstream:next")
                node1 = await client.terminal(1)
                await client.commentary("node", "upstream:next-summary", "Следующий SGD использовал новую записанную базу и отдельный рецепт momentum=0.3. Унаследованный runner поглощён без дублирования. Метрика измерена заново; вывод ограничен этой CPU-задачей.", 1)
                detail = await client.read("nodes/1")
                assert detail["files"] == {"recipe.env": "MOMENTUM=0.3\n"}, detail
                assert node0["metric_provenance"]["base_revision"]["digest"] == base["digest"]
                assert node1["metric_provenance"]["base_revision"]["digest"] == made["body"]["selector"]["digest"]
                if options.confirm:
                    await client.progress()
                    await client.read("harness-checkpoints")
                    await client.call("phases", {"query": "confirmation"})
                    await client.call("phase_info", {"phase_id": "confirmation"})
                    confirm_body = {"node_id": 0, "generation": 0}
                    forced = await client.command("force_confirm", confirm_body, "upstream:confirm")
                    await until(client.state, lambda value: 0 in value["state"].get("confirmed_forced", []))
                    assert await client.command("force_confirm", confirm_body, "upstream:confirm") == forced
                    actual = await client.read("nodes/0")
                    assert actual["files"] == first["files"]
                    assert actual["metric_provenance"] == node0["metric_provenance"]
                    confirmation = [e for e in EventStore(rd / "events.jsonl").read_all() if e.type == "confirm_eval" and e.data["node_id"] == 0]
                    assert len(confirmation) == 3 and all(e.data["metric"] == node0["metric"] for e in confirmation)
                    seeded = [e for e in EventStore(rd / "events.jsonl").read_all() if e.type == "workspace_seeded" and e.data.get("node_id") == 0]
                    assert len(seeded) == 4 and all(e.data["base_revision"]["digest"] == base["digest"] for e in seeded)
                    for seed_id in (1, 2, 3):
                        confirm_work = rd / "confirm" / f"node_0_g0_seed_{seed_id}"
                        assert (confirm_work / "train.py").read_bytes() == SOURCE.encode()
                        assert (confirm_work / "recipe.env").read_bytes() == b"MOMENTUM=0.2\n"
                    proof.update(engine_confirmation_evaluations=3, confirmation_original_base=True,
                        confirmation_original_files=True, confirmation_exact_retry_no_reexecution=True)
                await client.command("run_abort", {"reason": "Completed private upstream acceptance"}, "upstream:finish")
                await client.commentary("run", "upstream:finish-summary", "Прогон явно завершён. Проверены обобщение runner, прежний default, повторные оценки, смена базы и новый эксперимент. Gate учтён отдельно; исходный результат не переписан. Это проверка протокола, не ML benchmark.")
                proof.update(source_metric=node0["metric"], next_metric=node1["metric"], proposal=made["body"], gate=checked["body"], advancement=advanced["body"], effective_next_files=detail["files"], exact_retries_no_reexecution=True)
        anyio.run(drive)
        events = EventStore(rd / "events.jsonl").read_all()
        state = fold(events)
        assert len([e for e in events if e.type == "node_evaluated"]) == 2
        assert len([e for e in events if e.type == "upstream_execution"]) == 7
        assert all((source / name).read_bytes() == raw for name, raw in original_bytes.items())
        from looplab.engine.bundle import export_bundle, verify_bundle
        bundle = root / "bundle"
        before_export = (rd / "events.jsonl").read_bytes()
        export_bundle(rd, bundle)
        assert verify_bundle(bundle) == []
        assert (rd / "events.jsonl").read_bytes() == before_export
        bundled_state = fold(EventStore(bundle / "events.jsonl").read_all())
        assert bundled_state.upstream_base == state.upstream_base
        assert bundled_state.nodes[1].metric_provenance == state.nodes[1].metric_provenance
        proof.update(engine_primary_evaluations=2, explicit_gate_executions=7, replay_finished=state.finished,
            owner_bytes_unchanged=True, export_verified=True, export_no_execution_or_log_write=True, eval_seconds_by_kind=state.eval_seconds_by_kind,
            scorer_sha256=hashlib.sha256(original_bytes["score.py"]).hexdigest())
        (root / "acceptance.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf8")
        print(json.dumps({"passed": True, "acceptance": str(root / "acceptance.json"), "primary_evaluations": 2, "gate_executions": 7}))
    finally:
        server.should_exit = True; thread.join(timeout=10)
        if engine is not None and engine.poll() is None:
            engine.terminate(); engine.wait(timeout=10)
        for process in owned_children:
            if process.poll() is None: process.terminate(); process.wait(timeout=10)
        engine_log.close()
        os.environ.clear(); os.environ.update(before_env)


if __name__ == "__main__": main()
