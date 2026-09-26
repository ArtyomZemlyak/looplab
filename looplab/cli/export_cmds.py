"""Export / diagnostics commands: `smoke` / `bench` / `export-mlflow` / `export-notebook` / `harden`.

Split verbatim out of the flat `looplab/cli.py` (docs/15 §P5.2). `bench` keeps its lazy
`looplab.bench` import INSIDE the command body — that is what lets `looplab/bench.py` import the
shared `_engine` builder back from `looplab.cli` at module level without an import cycle.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import typer

from looplab.core.atomicio import atomic_write_text
from looplab.core.config import Settings
from looplab.core.latebind import late_bound
from looplab.events.replay import fold
from looplab.cli import _BACKENDS, _choice, _require_run_dir, app


# Late-bound so a test patching `looplab.cli.make_llm_client` (the documented seam, test_cli.py)
# also stubs `smoke` here. See `looplab.core.latebind` for the freeze-at-import hazard.
make_llm_client = late_bound("looplab.cli", "make_llm_client")


@app.command()
def smoke(model: Optional[str] = typer.Option(None, help="Override model id.")):
    """Ping the configured LLM endpoint to verify it's reachable and tool-calling works."""
    settings = Settings()
    if model is not None:
        from looplab.core.llm import apply_llm_model_override
        apply_llm_model_override(settings, model)
    from looplab.core.llm import make_llm_client_for, resolve_llm_target
    target = resolve_llm_target(settings)
    client = make_llm_client_for(settings, factory=make_llm_client)
    typer.echo(f"endpoint={target.base_url} model={target.model}")
    try:
        txt = client.complete_text([{"role": "user", "content": "Reply with one word: ready"}])
        typer.echo(f"text OK: {txt.strip()[:80]!r}")
    except Exception as e:  # noqa: BLE001
        typer.echo(f"text FAILED: {e}")
        raise typer.Exit(1)
    try:
        from looplab.core.models import Idea
        from looplab.core.parse import parse_structured
        idea = parse_structured(
            client,
            [{"role": "user", "content": (
                "Return one experiment with operator try_params and numeric params "
                "x=1.0, y=2.0. Preserve both parameter names and values exactly."
            )}],
            Idea, settings.llm_parser,
        )
        expected = {"x": 1.0, "y": 2.0}
        if not isinstance(idea.params, dict) or any(idea.params.get(key) != value
                                                     for key, value in expected.items()):
            raise ValueError("structured response did not preserve x=1.0 and y=2.0")
        typer.echo(f"structured OK: operator={idea.operator} params={idea.params}")
    except Exception as e:  # noqa: BLE001
        typer.echo(f"structured FAILED: {e}")
        raise typer.Exit(1)


@app.command()
def bench(
    task_files: list[Path] = typer.Argument(..., help="Task JSON files to benchmark end-to-end."),
    out: Path = typer.Option(Path("runs/bench"), help="Output dir for the benchmark runs + report."),
    backend: str = typer.Option("toy", help="Role backend: toy | llm."),
    max_nodes: int = typer.Option(8, help="Node budget per task."),
):
    """Capability self-benchmark — run each task end-to-end and report best-metric / eval-seconds /
    reward-hack flags (a regression test for capability, not just code)."""
    _choice(backend, _BACKENDS, "--backend")
    for tf in task_files:
        if not tf.exists():
            raise typer.BadParameter(f"task file not found: {tf}")
    from looplab.bench import run_benchmark
    settings = Settings()
    settings.backend = backend
    settings.max_nodes = max_nodes
    results = run_benchmark(task_files, settings, out)
    solved = sum(1 for r in results if r.get("finished") and r.get("best_metric") is not None)
    typer.echo(f"benchmark: {solved}/{len(results)} solved  (report: {out / 'benchmark.json'})")
    for r in results:
        if r.get("error"):
            typer.echo(f"  {r['task']}: ERROR {r['error']}")
        else:
            typer.echo(f"  {r['task']}: best={r['best_metric']} nodes={r['nodes']} "
                       f"eval_s={r['eval_seconds']} hacks={r['reward_hack_flags']}")
    # A suite in which a task ERRORED is a failed suite, and must not exit 0 (review 2026-09-22,
    # SCJ-05): it did, even when EVERY task errored, so a CI step or an `&&` chain around
    # `looplab bench` read a broken suite as a pass. Exit 1, the code `run` uses for a run that
    # produced nothing; each error is already printed above and recorded in benchmark.json.
    errored = [r["task"] for r in results if r.get("error")]
    if errored:
        typer.echo(f"benchmark FAILED: {len(errored)}/{len(results)} task(s) errored: "
                   + ", ".join(errored), err=True)
        raise typer.Exit(1)


@app.command(name="export-mlflow")
def export_mlflow(
    run_dir: Path = typer.Argument(..., help="Run dir to export to MLflow."),
    tracking_uri: Optional[str] = typer.Option(None, help="MLflow tracking URI (default: local ./mlruns)."),
    experiment: Optional[str] = typer.Option(None, help="MLflow experiment name."),
):
    """Log the run's champion (params/metrics/solution) to MLflow (needs the optional mlflow pkg)."""
    _require_run_dir(run_dir)
    from looplab.events.mlflow_export import available, export_run_dir
    if not available():
        typer.echo("MLflow not installed: pip install mlflow"); raise typer.Exit(1)
    rid = export_run_dir(run_dir, tracking_uri=tracking_uri, experiment=experiment)
    typer.echo(f"logged to MLflow run {rid}")


@app.command(name="export-bundle")
def export_bundle_cmd(
    run_dir: Path = typer.Argument(..., help="Run dir to bundle."),
    out: Optional[Path] = typer.Option(None, help="Bundle directory (default: `<run>/bundle`)."),
    verify: bool = typer.Option(True, help="Re-check every packaged file against the crate's digests."),
):
    """Package the run for a REVIEWER as an RO-Crate (doc 52 row 23): the event log and trace, the
    launch snapshots, the champion's code off the folded record, every memo's claims, the summary row
    (number, caveats, Mislead pair, seeds) and the audit sidecars, each described with its size and
    SHA-256 in ro-crate-metadata.json. Copies the run's own record; derives nothing but the row."""
    from looplab.engine.bundle import RO_CRATE_METADATA, export_bundle, verify_bundle

    _require_run_dir(run_dir)
    dest = out or (run_dir / "bundle")
    meta = export_bundle(run_dir, dest)
    files = [e for e in meta["@graph"] if e.get("@type") == "File"]
    typer.echo(f"wrote {dest / RO_CRATE_METADATA} ({len(files)} file(s))")
    if verify:
        defects = verify_bundle(dest)
        if defects:
            typer.echo("bundle defects: " + "; ".join(defects))
            raise typer.Exit(1)
        typer.echo("verified: every file matches its recorded size and digest")


@app.command(name="export-notebook")
def export_notebook(
    run_dir: Path = typer.Argument(..., help="Run dir to export the champion from."),
    out: Optional[Path] = typer.Option(
        None, help="Output .ipynb path (default: `<run>/champion.ipynb`)."),
):
    """Export the run's champion solution as a runnable Jupyter notebook (.ipynb)."""
    from looplab.events.notebook import champion_notebook
    store = _require_run_dir(run_dir)
    state = fold(store.read_all())
    champ = state.nodes.get(state.champion) if state.champion is not None else state.best()
    if champ is None:
        typer.echo("no champion/best node to export"); raise typer.Exit(1)
    nb = champion_notebook(state.goal, champ.code, params=champ.idea.params,
                           metric=champ.robust_metric,
                           task_id=state.task_id, run_id=state.run_id)
    dest = out or (run_dir / "champion.ipynb")
    atomic_write_text(dest, json.dumps(nb, indent=1))
    typer.echo(f"wrote {dest}")


# Seconds ONE git call of `export-git` may take. `fast-import` of a long run is the slow one (every
# lifecycle's files, once each); past this the child is killed, everything the export built is
# removed and the command exits 1 — a hung git (a lock another process holds, a filesystem that
# stopped answering) must not hold the command, or leave a half-built repository, forever.
_GIT_TIMEOUT_S = 600.0


def _hermetic_git_env(home: str) -> dict:
    """The environment every `export-git` git call runs in: the caller's, minus EVERY `GIT_*`
    variable, with no system or global config and an empty HOME.

    Review 2026-09-26, driven: with `GIT_DIR=victim/.git GIT_WORK_TREE=victim` in the environment —
    what every git HOOK runs with (an absolute `GIT_DIR`; a pre-commit hook also gets
    `GIT_INDEX_FILE=<repo>/.git/index.lock`) — the export exited 0, OUT stayed empty, and the victim
    repository got tag `node-0`, branch `champion`, a moved HEAD, and its tracked, staged and
    uncommitted work destroyed by `reset --hard`: `-C OUT` does not override `GIT_DIR`. Dropping the
    whole family takes `GIT_DIR` / `GIT_WORK_TREE` / `GIT_INDEX_FILE` / `GIT_OBJECT_DIRECTORY` /
    `GIT_CONFIG_PARAMETERS` and `GIT_DEFAULT_HASH` (which moved every commit id) at once, and no
    user or system config — `init.defaultObjectFormat`, `core.hooksPath`, a template dir, a filter —
    is read at all. `LC_ALL=C` keeps git's own words English: the failure line is picked out of them.
    """
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.pop("LANGUAGE", None)
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "HOME": home,
                "XDG_CONFIG_HOME": home, "LC_ALL": "C"})
    return env


def _git_argv(git: str, repo: Path, *args: str) -> list:
    """`git` pointed EXPLICITLY at `repo` — never at whatever the environment or the cwd names — with
    the per-call config no environment can move: no hooks, no fsmonitor daemon, no line-ending
    rewrite of checked-out bytes, and a checkout that refuses what NTFS or HFS+ would read as `.git`
    (`events/git_export.py::safe_tree_path` left those out already; this is the second lock)."""
    return [git, "--git-dir", str(repo / ".git"), "--work-tree", str(repo),
            "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
            "-c", "core.autocrlf=false", "-c", "core.protectNTFS=true", "-c", "core.protectHFS=true",
            *args]


def _git_failure_line(stderr) -> str:
    """The line that says WHY git failed: its `fatal:` (else `error`) line. Not the last line, which
    for fast-import is "dumping crash report to …" — a file the cleanup has already removed."""
    lines = [line.strip() for line in (stderr or b"").decode("utf-8", "replace").splitlines()
             if line.strip()]
    for prefix in ("fatal:", "error"):
        picked = next((line for line in lines if line.startswith(prefix)), None)
        if picked is not None:
            return picked
    return lines[-1] if lines else ""


def _export_git_target_refusal(run_dir: Path, out: Path) -> Optional[str]:
    """Why `export-git` will not build its repository at `out`, or None — each one the operator's to
    change, so each a refusal (exit 2), stated before anything is created anywhere."""
    if out.is_symlink():
        # Dangling or not: the repository would land wherever the link points, which is not what
        # the operator named, and a dangling one would be created THROUGH.
        return (f"{out} is a symbolic link{'' if out.exists() else ' to nothing'} — refusing to "
                "build a repository through it; name a real directory")
    try:
        occupied = out.exists() and (not out.is_dir() or any(out.iterdir()))
    except OSError as exc:
        return f"cannot read {out} ({exc}) — name a directory export-git can create or use"
    if occupied:
        return (f"{out} exists and is not an empty directory — refusing to write into it; name a "
                "new or an empty directory")
    target, run = Path(os.path.abspath(out)).resolve(), run_dir.resolve()
    if target == run or run in target.parents:
        return (f"{out} is inside the run directory {run_dir} — export-git is read-only on the run; "
                "name a directory outside it")
    return None


@app.command(name="export-git")
def export_git(
    run_dir: Path = typer.Argument(..., help="Run dir whose node DAG to export."),
    out: Path = typer.Argument(..., help="Directory for the new git repository (absent or empty)."),
):
    """Export the run's node DAG as a GIT REPOSITORY: one commit per node lifecycle, its parents the
    exact parent lifecycles it was built from, its own files as the tree, the metric and the receipts
    that decide whether it counts as `Looplab-*` trailers (doc 67 67.15, `events/git_export.py`).
    Each node's current lifecycle is tag `node-<id>`, one a reset superseded `node-<id>.g<gen>`;
    branch `champion` is the fold's best and is checked out, branch `promoted` the operator's promote
    alias when there is one. Read-only on the run; the export is a projection of the log, never read
    back. The task's base tree is not in the log, so a commit holds only the files the node itself
    wrote. Git runs hermetically — no GIT_* variable and no user or system config reaches it — and
    the repository is built beside OUT and moved into place only once it is whole."""
    import shutil
    import subprocess
    import tempfile
    import uuid

    from looplab.cli import log_integrity_from
    from looplab.core.atomicio import rmtree_readonly_aware
    from looplab.engine.champion_caveats import champion_metric_caveats
    from looplab.events.git_export import CHAMPION_BRANCH, fast_import_stream

    def refuse(message: str):
        # On STDERR, as the exit-code table says of every refusal (`2`: one message, on stderr).
        typer.echo(message, err=True)
        raise typer.Exit(2)

    git = shutil.which("git")
    if git is None:
        refuse("git is not installed (not on PATH) — install git to export a run as a repository")
    store = _require_run_dir(run_dir)     # also states an incomplete log on stderr, and continues
    refusal = _export_git_target_refusal(run_dir, out)
    if refusal:
        refuse(refusal)
    events = store.read_all()
    state = fold(events)
    if not state.nodes:
        # Not an empty repository: one with no commits exports nothing, and would read as a run
        # whose DAG was exported and found empty. `export-sft` refuses its own "nothing" the same way.
        refuse(f"{run_dir} has no nodes yet — there is no DAG to export")
    # A corrupt log folds its readable prefix, exactly as `export-bundle` and `export-notebook` do;
    # `_require_run_dir` already said so on stderr, and every commit says so too (the repository's
    # reader never sees this terminal).
    export = fast_import_stream(events, state, champion_caveats=champion_metric_caveats(state),
                                log_integrity=log_integrity_from(store))
    out = Path(os.path.abspath(out))
    # A SIBLING of OUT, so the last step is a rename within one filesystem and nothing is ever
    # half-built AT out: a failure after `git init` used to leave objects, tags, sometimes a
    # checkout and a `fast_import_crash_*` there, exit 1, and then refuse the re-run because OUT
    # was "not an empty directory". `mkdir` rather than `mkdtemp`, so the repository gets the
    # umask's mode and not a private 0700.
    stage = out.parent / f".{out.name}.{uuid.uuid4().hex[:12]}.export-git"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        stage.mkdir()
    except OSError as exc:
        refuse(f"cannot create the repository beside {out} ({exc}) — name a writable location")
    step, failure, leftover = "git init", "", ""
    try:
        with tempfile.TemporaryDirectory(prefix="looplab-export-git-home-") as home:
            env = _hermetic_git_env(home)

            def run(*args, stdin=None):
                return subprocess.run(_git_argv(git, stage, *args), input=stdin, check=True,
                                      capture_output=True, env=env, timeout=_GIT_TIMEOUT_S)

            run("init", "--quiet", "--template=", "--object-format=sha1",
                f"--initial-branch={CHAMPION_BRANCH}")
            step = "git fast-import"
            run("fast-import", "--quiet", "--done", stdin=export.stream)
            # BEFORE success is reported: a tree a receiving host's `fsck` refuses
            # (`receive.fsckObjects`) is a failed export, whatever fast-import accepted.
            step = "git fsck --strict"
            run("fsck", "--strict", "--no-progress")
            if export.champion is not None:
                step = "the checkout of branch champion"
                run("reset", "--hard", "--quiet")
        step = "the move into place"
        if out.is_dir():
            out.rmdir()   # the EMPTY directory named; os.rename cannot replace one on Windows
        os.rename(stage, out)
    except subprocess.CalledProcessError as exc:
        failure = f"{step} failed: {_git_failure_line(exc.stderr) or f'exit {exc.returncode}'}"
    except subprocess.TimeoutExpired:
        failure = f"{step} did not finish within {int(_GIT_TIMEOUT_S)} s"
    except OSError as exc:
        failure = f"{step} failed: {exc}"
    finally:
        # Every way out but a completed move — a git failure, a timeout, Ctrl-C — removes what was
        # built. Read-only-aware: git writes its pack files 0444, which Windows will not unlink.
        if stage.exists():
            try:
                rmtree_readonly_aware(stage)
            except OSError as exc:
                leftover = f"; the partial build at {stage} could not be removed ({exc})"
    if failure:
        typer.echo(f"export-git: {failure} — nothing was written to {out}{leftover}", err=True)
        raise typer.Exit(1)
    parts = [f"exported {len(state.nodes)} node(s) of {run_dir} to {out}: {export.commits} "
             f"commit(s), each node's current lifecycle as tag node-<id>"]
    if export.superseded:
        parts.append(f"{export.superseded} superseded lifecycle(s) as node-<id>.g<generation>")
    parts.append(f"branch champion = node {export.champion}, checked out"
                 if export.champion is not None else "no champion yet, so nothing is checked out")
    if export.promoted is not None:
        parts.append(f"branch promoted = node {export.promoted}")
    if export.skipped_paths:
        parts.append(f"{export.skipped_paths} path(s) left out, counted as Looplab-Skipped-Paths")
    typer.echo("; ".join(parts))


@app.command(name="export-sft")
def export_sft(
    run_dir: Path = typer.Argument(..., help="Run dir to export the trajectories from."),
    out: Optional[Path] = typer.Option(None, help="Output .jsonl (default: `<run>/sft.jsonl`)."),
    only_successful: bool = typer.Option(
        False, help="Keep only turns whose node produced a usable metric and stayed feasible."),
    op: Optional[str] = typer.Option(None, help="Keep only this operation (propose, implement, …)."),
):
    """Export this run's model turns as EXECUTION-GROUNDED SFT rows (read-only, no model).

    Frontis-MA1 (39.39 -> 60.61 %) and SandMLE (+20-67 % relative) train operators from exactly the
    corpus `spans.jsonl` already holds — the messages a role was handed and what it answered — and
    LoopLab exported MLflow and a notebook only. What makes the corpus worth anything is the
    GROUNDING: every row carries the outcome of the node the turn belongs to, so a consumer can
    train on what worked rather than on what was said.

    The text is the run's own trace projection: capture-time redaction and projection caps already
    applied, `input_partial` carried through where the chain could not be reconstructed. This copies
    that record; it does not re-read a prompt from anywhere.
    """
    from looplab.events.traceview import hydrate_inputs, load_spans

    store = _require_run_dir(run_dir)
    spans_path = run_dir / "spans.jsonl"
    if not spans_path.exists():
        typer.echo(f"no spans.jsonl in {run_dir} — this run was traced with tracing off, so there "
                   "are no turns to export.")
        raise typer.Exit(2)
    state = fold(store.read_all())
    spans = hydrate_inputs(load_spans(spans_path))
    generations = [s for s in spans if s.get("kind") == "generation"]
    rows, skipped_no_output, skipped_ungrounded = [], 0, 0
    for span in generations:
        attributes = span.get("attributes") or {}
        if op and str(attributes.get("op") or "") != op:
            continue
        messages = attributes.get("input")
        completion = attributes.get("output")
        # A turn with no answer is not a training example. It is also not a defect: a budget cut, a
        # transport failure and a refusal all end a generation with an input and nothing after it.
        if not isinstance(messages, list) or not messages or not completion:
            skipped_no_output += 1
            continue
        node_id = attributes.get("node_id")
        node = state.nodes.get(node_id) if isinstance(node_id, int) else None
        outcome = {"node_id": node_id, "metric": None, "status": None, "feasible": None,
                   "error_reason": None}
        if node is not None:
            status = getattr(node, "status", "")
            outcome = {"node_id": node.id, "metric": node.metric,
                       # The VALUE, not the enum's repr: a corpus row that says
                       # `NodeStatus.evaluated` makes every consumer parse Python's spelling of a
                       # fact this file exists to hand over as data.
                       "status": str(getattr(status, "value", status) or ""),
                       "feasible": bool(node.feasible),
                       # `error_reason` is the node's own field name for why it failed — the
                       # vocabulary `FAILURE_REASONS` holds. There is no `reason` on a Node, and a
                       # `getattr(node, "reason", None)` would have written a silent `null` into
                       # every row of a corpus whose whole value is the outcome.
                       "error_reason": getattr(node, "error_reason", None)}
        if only_successful:
            # THE GROUNDING IS THE POINT, so the filter is the outcome and not the absence of an
            # error: a node that failed for an unrelated reason after a good proposal is still a
            # turn nobody should train on as if it had worked.
            if node is None or node.metric is None or not node.feasible:
                skipped_ungrounded += 1
                continue
        rows.append({
            "messages": messages,
            "completion": completion,
            "op": attributes.get("op"),
            "model": attributes.get("model"),
            "phase": attributes.get("phase"),
            "run_id": state.run_id,
            "task_id": state.task_id,
            "direction": state.direction,
            # Carried, not dropped: a reader must be able to tell a complete retained projection
            # from a truncated one, which is the same rule `hydrate_inputs` stamps it for.
            **({"input_partial": True} if attributes.get("input_partial") else {}),
            "outcome": outcome,
        })
    dest = out or (run_dir / "sft.jsonl")
    atomic_write_text(dest, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    typer.echo(f"wrote {dest}: {len(rows)} turn(s) from {len(generations)} generation span(s)")
    if skipped_no_output:
        typer.echo(f"  {skipped_no_output} generation(s) had no answer to learn from (budget cut, "
                   "transport failure, refusal) — not an error, and not a training example")
    if skipped_ungrounded:
        typer.echo(f"  {skipped_ungrounded} turn(s) dropped by --only-successful: their node "
                   "produced no usable metric or was flagged infeasible")
    grounded = sum(1 for row in rows if row["outcome"]["metric"] is not None)
    typer.echo(f"  {grounded} of {len(rows)} turn(s) are joined to a node that produced a metric; "
               "the rest carry the outcome they have (a failure, or no node at all — a run-level "
               "turn such as the Strategist's).")


@app.command()
def harden(
    memory_dir: Path = typer.Argument(..., help="Memory dir; the exploit suite lives at "
                                                "<memory_dir>/exploits.jsonl."),
    rounds: int = typer.Option(1, help="Hacker/fixer iterations."),
):
    """4.3 · Harden the reward-hack evaluator via a hacker-fixer-solver loop (arXiv:2606.08960).

    Grows a persisted exploit ruleset: a hacker proposes eval exploits, a fixer turns each one the
    current detector MISSES into a durable regex, and a solver guardrail rejects any rule that would
    flag an honest solution. Every future run with this memory_dir + reward_hack_detect loads the
    suite, so each discovered exploit stays guarded. Deterministic seed corpus (offline); no model."""
    from looplab.trust.harden import ExploitSuite, harden as _harden
    path = memory_dir / "exploits.jsonl"
    memory_dir.mkdir(parents=True, exist_ok=True)
    suite = ExploitSuite.load(path)
    # Honest baselines the solver guardrail protects (a fix must never flag these).
    legit = [
        "import json\nimport numpy as np\nX=json.load(open('train.json'))['X']\n"
        "pred=[0]*len(X)\njson.dump(pred, open('predictions.json','w'))",
        "from sklearn.ensemble import RandomForestClassifier\nm=RandomForestClassifier().fit(Xtr,ytr)\n"
        "json.dump(m.predict(Xte).tolist(), open('predictions.json','w'))",
    ]
    res = _harden(suite, legit_solutions=legit, rounds=rounds)
    suite.save(path)
    typer.echo(f"hardened: +{len(res['added'])} rules ({', '.join(res['added']) or 'none new'}); "
               f"caught={res['caught']} escaped={res['escaped']} "
               f"blocked_legit={len(res['blocked_legit'])}; suite now {len(suite.patterns)} rules "
               f"at {path}")
