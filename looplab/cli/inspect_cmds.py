"""Read-only inspection commands: `replay` / `timings` / `inspect` / `tensorboard`.

Split verbatim out of the flat `looplab/cli.py` (docs/15 §P5.2). All pure folds of the event log
(or viewers over run-dir sidecars) — nothing here mutates a run.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import orjson
import typer

from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.cli import _print_result, _require_run_dir, app


@app.command()
def replay(run_dir: Path = typer.Argument(...)):
    """Pure fold of the event log -> current state (read-only)."""
    store = _require_run_dir(run_dir)
    state = fold(store.read_all())
    typer.echo(orjson.dumps(state.model_dump(mode="json"),
                            option=orjson.OPT_INDENT_2).decode())


@app.command()
def timings(run_dir: Path = typer.Argument(...),
            node: Optional[int] = typer.Option(None, help="only this node id")):
    """Where the wall-clock went, per node: LLM generations vs eval vs repair vs tools (from spans.jsonl
    `duration_s`). Answers 'what is this run actually spending time on right now' at a glance."""
    import json as _json
    from collections import defaultdict
    sp_path = run_dir / "spans.jsonl"
    if not sp_path.exists():
        typer.echo(f"no spans.jsonl at {run_dir} (tracing off or pre-tracing run).")
        raise typer.Exit(2)

    def _cat(sp: dict) -> str:
        k = sp.get("kind")
        if k == "generation":
            return "LLM"
        if k == "tool":
            return "tools"
        if k == "operation":
            nm = str(sp.get("name") or "")
            if "eval" in nm:
                return "eval"
            if "repair" in nm:
                return "repair"
            return f"op:{nm}" if nm else "op"
        return k or "other"

    from looplab.events.eventstore import read_jsonl_lenient
    # skip-and-continue (not iter_jsonl's stop-at-first-bad): a mid-file corrupt span line must
    # cost one span, not truncate every later span out of the report. Keep dicts_only=True (the
    # default): a valid-JSON-but-NON-dict corrupt line (e.g. a bare `123`) must be SKIPPED like any
    # other damaged line — with dicts_only=False it'd survive and the `sp.get(...)` accesses below
    # would raise AttributeError, crashing the whole command (worse than the truncation this avoids).
    spans = read_jsonl_lenient(sp_path, loads=_json.loads, errors="replace")
    # An operation span's recorded duration INCLUDES every nested span (create_node ⊃ implement ⊃
    # stages/plan ⊃ the generations inside them), so summing raw durations counted the nested
    # phases twice or thrice and skewed every percentage. Charge each op its SELF time only
    # (duration minus its DIRECT children); leaf generations/tools keep their full duration.
    child_sum: dict = defaultdict(float)
    for sp in spans:
        if sp.get("parent_id"):
            child_sum[sp["parent_id"]] += float(sp.get("duration_s") or 0.0)
    per_node: dict = defaultdict(lambda: defaultdict(lambda: [0.0, 0]))
    for sp in spans:
        nid = (sp.get("attributes") or {}).get("node_id")
        try:
            nid = int(nid)
        except (TypeError, ValueError):
            continue
        if node is not None and nid != node:
            continue
        dur = float(sp.get("duration_s") or 0.0)
        if sp.get("kind") == "operation":
            dur = max(0.0, dur - child_sum.get(sp.get("span_id") or "", 0.0))
        cell = per_node[nid][_cat(sp)]
        cell[0] += dur
        cell[1] += 1

    for nid in sorted(per_node):
        cats = per_node[nid]
        total = sum(v[0] for v in cats.values()) or 1.0
        typer.echo(f"\nnode {nid} — {round(total/60, 1)} min:")
        for cat, (secs, n) in sorted(cats.items(), key=lambda x: -x[1][0]):
            typer.echo(f"  {cat:10} {round(secs/60, 1):>6} min  ({n} spans, {round(100*secs/total)}%)")


@app.command()
def inspect(run_dir: Path = typer.Argument(...)):
    """Show the resolved config snapshot + the run's best result."""
    snap = run_dir / "config.snapshot.json"
    events = run_dir / "events.jsonl"
    # Tolerate a run that crashed after writing config.snapshot.json but before its first event: still
    # show the config. Only error when the dir is neither — a typo'd path, not a real (if partial) run.
    if not snap.exists() and not events.exists():
        typer.echo(f"no run found at {run_dir} (no config.snapshot.json or events.jsonl).")
        raise typer.Exit(2)
    if snap.exists():
        typer.echo(snap.read_text(encoding="utf-8"))
    if events.exists():
        _print_result(fold(EventStore(events).read_all()))


def _make_llm_client(*args, **kwargs):
    """Late-bound `make_llm_client` (mirrors export_cmds) so a test patching `looplab.cli.make_llm_client`
    also stubs these diagnostics — a frozen `from looplab.cli import make_llm_client` would not."""
    from looplab import cli
    return cli.make_llm_client(*args, **kwargs)


def _run_tools_for(state):
    """Read-only run tools bound to `state` for AGENTIC tagging/briefing (mirrors trust.verify._verify_tools).
    None on any failure -> the caller runs the plain (non-agentic) LLM path."""
    try:
        from looplab.agents.agent import CompositeTools
        from looplab.tools.run_tools import RunTools
        rt = RunTools()
        rt.bind_state(state, None)
        return CompositeTools([rt])
    except Exception:  # noqa: BLE001 — no tools => degrade to the plain structured call
        return None


def _concept_map_for(state, resolved_type, *, offline, model=None, repo=None):
    """Shared PART IV D5 build — AGENTIC by default (the LLM agent grows the graph, tags, derives the
    per-task importance; `build_concept_map`), the deterministic alias heuristic only as the `--offline`
    fallback. Returns {graph, tags, important_uncovered, mode, brief}. This is what makes every Phase-1
    diagnostic (lock-in / board-dedup / research-targets) agentic-first and universal, not
    heuristic-hardcoded (§21.13/§21.15 correction)."""
    from looplab.search.concept_graph import (build_concept_map, skeleton_for, tag_nodes_heuristic)
    seed = skeleton_for(resolved_type)
    seed = seed if seed.concepts() else None
    if not offline:
        from looplab.core.config import Settings
        settings = Settings()
        if model is not None:
            settings.llm_model = model
        try:
            client = _make_llm_client(settings)
        except Exception as e:  # noqa: BLE001 — no endpoint => heuristic fallback, noted
            typer.echo(f"(no LLM endpoint: {e}; using the offline heuristic fallback)")
            client = None
        if client is not None:
            brief = ""
            if repo is not None and Path(repo).exists():
                try:
                    from looplab.tools.asset_brief import asset_brief as _ab
                    brief = _ab(str(repo), client=client, task_type=resolved_type or None)
                except Exception as e:  # noqa: BLE001 — grounding optional
                    typer.echo(f"(asset-brief grounding skipped: {e})")
            cmap = build_concept_map(state, task_goal=getattr(state, "goal", "") or "", client=client,
                                     tools=_run_tools_for(state), seed_graph=seed, asset_brief=brief,
                                     parser=settings.llm_parser)
            cmap["brief"] = brief
            return cmap
    graph = seed or skeleton_for(resolved_type)
    return {"graph": graph, "tags": tag_nodes_heuristic(state, graph), "important_uncovered": [],
            "mode": "offline-heuristic", "brief": ""}


@app.command(name="concept-coverage")
def concept_coverage(
    run_dir: Path = typer.Argument(..., help="Run dir whose event log to fold and diagnose."),
    task_type: Optional[str] = typer.Option(
        None, help="Curated concept pack to SEED the LLM's build with (e.g. dense-retrieval) — a starting "
                   "vocabulary the agent verifies/expands. Default: inferred from the run's task_id; the "
                   "agent builds from scratch when no pack matches."),
    offline: bool = typer.Option(
        False, "--offline", help="Skip the LLM and use only the deterministic alias heuristic over the "
                                 "curated seed pack (a fast, coarse fallback that needs a curated pack and "
                                 "cannot derive per-task importance). Default is the agentic build."),
    model: Optional[str] = typer.Option(None, help="Override model id."),
    repo: Optional[Path] = typer.Option(
        None, help="Task repo to ground the per-task uncovered-region derivation with a D1 prior-art brief."),
):
    """PART IV D5 (§21.11): the concept-graph coverage + uncovered-region diagnostic. **The LLM agent builds
    the map** by default — it grows the concept vocabulary from the actual experiments (reading each node's
    code/logs), computes the coverage, and derives the important-but-uncovered directions per task (universal:
    no hardcoded winning region; grounded in `--repo`'s prior-art brief when given). `--offline` forces the
    deterministic alias-heuristic fallback (needs a curated `--task-type` pack, no importance derivation)."""
    from looplab.search.concept_graph import (build_concept_map, concept_report, skeleton_for)
    store = _require_run_dir(run_dir)
    state = fold(store.read_all())
    resolved_type = task_type or state.task_id or ""
    # A curated pack is only a SEED / starting vocabulary the agent expands (like agentic_asset_brief's
    # seed_scan); None => the LLM builds the graph from scratch (works on any task).
    seed = skeleton_for(resolved_type)
    seed = seed if seed.concepts() else None

    client = None
    if not offline:
        from looplab.core.config import Settings
        settings = Settings()
        if model is not None:
            settings.llm_model = model
        try:
            client = _make_llm_client(settings)
        except Exception as e:  # noqa: BLE001 — no endpoint => fall back to the offline heuristic, note it
            typer.echo(f"(no LLM endpoint: {e}; using the offline heuristic fallback)")

    if client is None:
        # Deterministic FALLBACK. Needs a curated seed to localize anything.
        graph = seed or skeleton_for(resolved_type)
        if not graph.concepts():
            typer.echo(f"note: no curated concept pack for task-type '{resolved_type or 'unknown'}', so the "
                       "offline heuristic can't tag experiments. Drop --offline to let the agent build the "
                       "graph, or pass --task-type <known-pack> (e.g. dense-retrieval).")
        typer.echo(concept_report(state, graph, None))
        if graph.concepts():
            typer.echo("\nnote: --offline alias tagging is coarse (over-reports coverage on semantically-"
                       "ambiguous concepts). Drop --offline for the agentic, code-reading build + per-task "
                       "importance.")
        return

    # PRIMARY: the LLM agent builds the whole map (grows vocab, tags agentically, derives importance).
    brief_text = ""
    if repo is not None:
        try:
            from looplab.tools.asset_brief import asset_brief as _asset_brief
            brief_text = _asset_brief(str(repo), client=client, task_type=resolved_type or None)
        except Exception as e:  # noqa: BLE001 — grounding is optional; derive from task+coverage alone
            typer.echo(f"(asset-brief grounding skipped: {e})")
    cmap = build_concept_map(state, task_goal=state.goal or "", client=client,
                             tools=_run_tools_for(state), seed_graph=seed, asset_brief=brief_text,
                             parser=settings.llm_parser)
    typer.echo(concept_report(state, cmap["graph"], cmap["tags"]))
    typer.echo(f"\n  (built by the LLM agent — mode={cmap['mode']}, "
               f"{len(cmap['graph'].concepts())} concepts grown)")
    typer.echo("  IMPORTANT-BUT-UNCOVERED (derived per task — universal, no hardcoded winning region):")
    if cmap["important_uncovered"]:
        for m in cmap["important_uncovered"]:
            typer.echo(f"    · {m['concept_id']}: {m['why']}")
    else:
        typer.echo("    (none surfaced — coverage looks complete for this task, or derivation was "
                   "unavailable)")


@app.command(name="asset-brief")
def asset_brief_cmd(
    repo: Path = typer.Argument(..., help="Task repo to sweep for prior art & on-disk assets."),
    task_type: Optional[str] = typer.Option(
        None, help="Task family (e.g. dense-retrieval) to name domain capabilities. Default: generic."),
    llm: bool = typer.Option(
        False, "--llm", help="Use the agentic brief (an LLM explores the repo with read-only tools) "
                             "instead of the offline heuristic scan. Needs a reachable endpoint."),
    model: Optional[str] = typer.Option(None, help="Override model id for --llm."),
):
    """PART IV D1 (§21.2): the seed-time prior-art & available-assets brief for a task repo — the
    on-disk result tables, sibling checkpoints (metrics in filenames), and reusable trainer capabilities
    the search would otherwise miss. Offline heuristic scan by default; `--llm` runs the agentic sweep."""
    from looplab.tools.asset_brief import asset_brief
    if not repo.exists():
        typer.echo(f"no such repo: {repo}")
        raise typer.Exit(2)
    client = None
    if llm:
        from looplab.core.config import Settings
        settings = Settings()
        if model is not None:
            settings.llm_model = model
        try:
            client = _make_llm_client(settings)
        except Exception as e:  # noqa: BLE001 — degrade to the offline scan
            typer.echo(f"(--llm unavailable: {e}; using the offline scan)")
    typer.echo(asset_brief(repo, client=client, task_type=task_type))


@app.command(name="lock-in")
def lock_in(
    run_dir: Path = typer.Argument(..., help="Run dir whose event log to fold and diagnose."),
    task_type: Optional[str] = typer.Option(None, help="Curated concept pack to SEED the agent's build."),
    threshold: int = typer.Option(5, help="Consecutive same-lever nodes that trip the alarm."),
    offline: bool = typer.Option(False, "--offline", help="Use the deterministic heuristic instead of the "
                                                          "agentic build (default is the LLM agent build)."),
    model: Optional[str] = typer.Option(None, help="Override model id."),
):
    """PART IV D7 (§21.8): the action-space lock-in detector. Reports the longest run of CONSECUTIVE
    experiments confined to one axis-region (the 'same-lever streak' the flat coverage signal is blind to)
    and fires when it exceeds `threshold`. The LLM agent builds the concept tags by default (`--offline`
    forces the heuristic). Deterministic detection; never touches selection."""
    from looplab.search.lock_in import lock_in_report
    store = _require_run_dir(run_dir)
    state = fold(store.read_all())
    m = _concept_map_for(state, task_type or state.task_id or "", offline=offline, model=model)
    typer.echo(lock_in_report(state, m["graph"], tags=m["tags"], streak_threshold=threshold))
    typer.echo(f"\n  (concept tags built by: {m['mode']})")


@app.command(name="board-dedup")
def board_dedup(
    run_dir: Path = typer.Argument(..., help="Run dir whose hypothesis board to analyze."),
    task_type: Optional[str] = typer.Option(None, help="Curated concept pack to SEED the agent's build."),
    offline: bool = typer.Option(False, "--offline", help="Use the deterministic heuristic instead of the "
                                                          "agentic build (default is the LLM agent build)."),
    model: Optional[str] = typer.Option(None, help="Override model id."),
):
    """PART IV D4 (§21.5): taxonomy-aware hypothesis-board dedup analysis. Surfaces the dominant
    within-concept redundancy (merge aggressively) and cross-branch look-alikes a blind merge would wrongly
    collapse (keep distinct). Agentic tags by default (`--offline` forces the heuristic); merges nothing."""
    from looplab.search.taxonomy_dedup import dedup_report
    store = _require_run_dir(run_dir)
    state = fold(store.read_all())
    m = _concept_map_for(state, task_type or state.task_id or "", offline=offline, model=model)
    typer.echo(dedup_report(state, m["graph"], tags=m["tags"]))
    typer.echo(f"\n  (concept tags built by: {m['mode']})")


@app.command(name="research-targets")
def research_targets_cmd(
    run_dir: Path = typer.Argument(..., help="Run dir whose coverage to turn into research targets."),
    task_type: Optional[str] = typer.Option(None, help="Curated concept pack to SEED the agent's build."),
    asset_repo: Optional[Path] = typer.Option(
        None, help="Task repo to ground the derived importance + queries in the D1 asset brief."),
    offline: bool = typer.Option(False, "--offline", help="Use the deterministic heuristic + axis targets "
                                                          "only (no LLM-derived importance)."),
    model: Optional[str] = typer.Option(None, help="Override model id."),
):
    """PART IV D2 (§21.3): axis-structured deep-research targets from the coverage map. The LLM agent
    derives the per-task IMPORTANT-but-uncovered directions (universal — no hardcoded winning region) as the
    top targets, then uncovered axes, failed directions re-framed as 'research a different implementation',
    and under-covered axes. `--offline` drops to deterministic axis targets only. Produces targets, runs no
    research."""
    from looplab.search.research_targeting import targeting_report
    store = _require_run_dir(run_dir)
    state = fold(store.read_all())
    m = _concept_map_for(state, task_type or state.task_id or "", offline=offline, model=model,
                         repo=asset_repo)
    typer.echo(targeting_report(state, m["graph"], tags=m["tags"],
                                important_uncovered=m["important_uncovered"], asset_brief=m.get("brief", "")))
    typer.echo(f"\n  (targets built by: {m['mode']})")


@app.command()
def tensorboard(
    run_dir: Path = typer.Argument(..., help="Run dir; its nodes/ hold each experiment's training logs."),
    port: int = typer.Option(6006, help="Port to serve on."),
    host: str = typer.Option(
        "127.0.0.1",
        help="Bind address. Defaults to localhost — TensorBoard has NO auth, so an experiment's "
             "training logs (and any secret a script printed into them) must not be exposed on all "
             "interfaces by default. Pass --host 0.0.0.0 explicitly to bind all interfaces."),
):
    """Serve TensorBoard over a run's per-node training logs — online curves for ALL metrics the
    training framework logged (loss, recall@k, grad norms, lr, …), one comparable run per experiment.
    RepoTask training scripts (e.g. PyTorch Lightning's TensorBoardLogger) write event files under each
    node's workdir; this points TensorBoard at nodes/ so every node shows up."""
    import shutil
    import subprocess
    import sys
    logdir = run_dir / "nodes"
    if not logdir.exists():
        logdir = run_dir
    exe = shutil.which("tensorboard")
    cmd = ([exe] if exe else [sys.executable, "-m", "tensorboard.main"]) + \
          ["--logdir", str(logdir), "--port", str(port), "--host", host]
    typer.echo(f"Serving TensorBoard for {run_dir} on http://{host}:{port}  (logdir={logdir})")
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        pass
