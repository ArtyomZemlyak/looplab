"""Read-only inspection commands: `replay` / `timings` / `inspect` / `tensorboard`.

Split verbatim out of the flat `looplab/cli.py` (docs/15 §P5.2). Read-only over a run: pure folds of
the event log, viewers over run-dir sidecars, and (the Part IV concept/novelty diagnostics) offline
analyses that may invoke an LLM to tag/grade — but nothing here mutates a run.
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
    # Agentic-BY-DEFAULT (the agentic-first concept, §21.13/§21.15): the map is LLM-built unless the caller
    # passes --offline, so this path DOES send node code/logs to the configured endpoint by default. The
    # cost/privacy contract is stated up front in each command's --offline help + docs/guide/cli-reference.md
    # ("Agentic by default … sends node code/logs … pass --offline for the local heuristic"). `asset-brief`
    # keeps the inverse (--llm opt-in) because ITS agentic path is a much heavier full tool-loop.
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
    from looplab.search.concept_graph import tag_text, tag_text_llm
    from looplab.search.taxonomy_dedup import dedup_report
    store = _require_run_dir(run_dir)
    state = fold(store.read_all())
    m = _concept_map_for(state, task_type or state.task_id or "", offline=offline, model=model)
    # dedup works over HYPOTHESIS tags (keyed by hypothesis id), NOT the node tags in m["tags"] (keyed by
    # node id). HT (§21.18) hypothesis-tag precedence:
    #   --offline           -> force the deterministic tag_text heuristic (bypass any recorded cache);
    #   recorded cache covers the board -> use it (tags=None -> dedup_analysis reads hypothesis_concepts);
    #   otherwise + a client -> tag the board LIVE agentically against the agent-built graph;
    #   else                 -> tag_text heuristic.
    hyps = list((state.hypotheses or {}).values())
    cache = getattr(state, "hypothesis_concepts", None) or {}
    board_cached = any(h.id in cache for h in hyps)     # cache covers at least one CURRENT-board hypothesis
    tags, label = None, "heuristic"
    if offline:
        tags = {h.id: tag_text(h.statement, m["graph"], allow_plural=True) for h in hyps}
        label = "heuristic (--offline)"
    elif board_cached:
        label = "recorded/agentic"                      # dedup_analysis reads the cache (per-item fallback)
    elif hyps:
        client = None
        try:
            from looplab.core.config import Settings
            settings = Settings()
            if model is not None:
                settings.llm_model = model
            client = _make_llm_client(settings)
        except Exception as e:  # noqa: BLE001 — no endpoint => heuristic tags, noted
            typer.echo(f"(no LLM endpoint: {e}; using the heuristic hypothesis tagger)")
        if client is not None:
            tags = {h.id: tag_text_llm(h.statement, m["graph"], client, allow_plural=True) for h in hyps}
            label = "live-agentic"
    typer.echo(dedup_report(state, m["graph"], tags=tags))
    typer.echo(f"\n  (concept graph built by: {m['mode']}; hypothesis tags: {label})")


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


@app.command(name="novelty-recall")
def novelty_recall_cmd(
    run_dir: Path = typer.Argument(..., help="Run dir whose proposals to check for leaked paraphrases."),
    offline: bool = typer.Option(False, "--offline", help="Only cluster candidate near-dup pairs (no "
                                                          "paraphrase-vs-variant adjudication — that needs "
                                                          "the LLM)."),
    max_pairs: int = typer.Option(60, "--max-pairs", help="Call-budget knob: adjudicate at most this many "
                                                          "of the most-similar candidate pairs with the LLM "
                                                          "(each pair = one call). Lower it to cap cost/data."),
    model: Optional[str] = typer.Option(None, help="Override model id."),
):
    """PART IV E3 (§21.12): the novelty-gate RECALL diagnostic. Surfaces near-duplicate proposal pairs that
    BOTH executed and the LLM judges TRUE paraphrases the gate should have deduplicated (the "сколько шлака"
    / wasted-compute question), and estimates the gate's recall against what it caught. Offline (`--offline`)
    only clusters candidates; the LLM adjudicates paraphrase vs legitimate variant by default."""
    from looplab.search.novelty_recall import novelty_recall_report
    store = _require_run_dir(run_dir)
    state = fold(store.read_all())
    client = None
    parser = "tool_call"
    # Agentic-by-default (§21.13); the cost is BOUNDED and TUNABLE: at most `--max-pairs` LLM calls (the
    # most-similar candidate pairs), each sending two truncated idea texts. `--offline` skips the LLM
    # entirely (candidate clusters only); docs/guide/cli-reference.md states the send-by-default contract.
    if not offline:
        from looplab.core.config import Settings
        settings = Settings()
        if model is not None:
            settings.llm_model = model
        try:
            client = _make_llm_client(settings)
            parser = settings.llm_parser
        except Exception as e:  # noqa: BLE001 — no endpoint => candidates only, noted
            typer.echo(f"(no LLM endpoint: {e}; showing candidate pairs only)")
    typer.echo(novelty_recall_report(state, client=client, parser=parser, max_pairs=max_pairs))


@app.command(name="lesson-guard")
def lesson_guard_cmd(
    run_dir: Path = typer.Argument(..., help="Run dir whose distilled lessons to audit."),
    model: Optional[str] = typer.Option(None, help="Override model id."),
):
    """PART IV D6/E4 (§21.7/§21.12): audit the run's distilled lessons. Flags lessons that OVER-GENERALIZE a
    single failed implementation into a whole sound direction (the node_63 pattern), and scans for
    mutually-CONTRADICTORY lesson pairs. Advisory / LLM-backed (needs a reachable endpoint)."""
    from looplab.trust.lesson_guard import contradiction_scan, guard_lessons
    store = _require_run_dir(run_dir)
    state = fold(store.read_all())
    from looplab.core.config import Settings
    settings = Settings()
    if model is not None:
        settings.llm_model = model
    try:
        client = _make_llm_client(settings)
    except Exception as e:  # noqa: BLE001 — this diagnostic is LLM-only
        typer.echo(f"lesson-guard needs a reachable LLM endpoint: {e}")
        raise typer.Exit(1)
    # A cheap DETERMINISTIC skeleton graph (no LLM) so the taxonomy attachment (which concept a lesson
    # over-generalizes) is populated instead of always empty — inferred from the run's task_id; None-safe.
    graph = None
    try:
        from looplab.search.concept_graph import skeleton_for
        sk = skeleton_for(state.task_id or "")
        graph = sk if sk.concepts() else None
    except Exception:  # noqa: BLE001 — taxonomy attach is best-effort enrichment, never blocks the guard
        graph = None
    g = guard_lessons(state, client=client, parser=settings.llm_parser, graph=graph)
    # Constructing a client does NOT prove a sample succeeded: guard_lessons reports adjudicated=False when
    # NOTHING actually scored (no client, or a wired client whose every verify sample failed/abstained), so
    # say INCONCLUSIVE rather than printing a false "0 flagged / all clean".
    if not g.get("adjudicated", True):
        typer.echo(f"Lesson over-generalization guard  ({g['n_lessons']} lessons) — "
                   "verifier could not grade any lesson; results INCONCLUSIVE.")
    else:
        typer.echo(f"Lesson over-generalization guard  ({g['n_lessons']} lessons, {g['n_flagged']} flagged)")
        for f in g["findings"]:
            if f.get("flagged"):
                typer.echo(f"  ⚠ over-generalizes: {str(f.get('statement', ''))[:100]}")
                typer.echo(f"      rescoped: {str(f.get('rescope_hint', ''))[:120]}")
    c = contradiction_scan(state, client=client, parser=settings.llm_parser)
    # Be HONEST about the scan's methodology and degraded states: it grades pairs with a single sample and
    # bounds the pair count, so surface truncation, and — critically — DON'T print "0 pairs" as a clean
    # bill of health when nothing was actually judged (adjudicated=False => total endpoint failure).
    if not c.get("adjudicated", True):
        typer.echo(f"\nContradiction scan  ({c['n_lessons']} lessons) — verifier could not grade any pair; "
                   "INCONCLUSIVE (not 'no contradictions').")
    else:
        note = "  [truncated: only the first pairs were scanned]" if c.get("truncated") else ""
        judged = f", {c['n_judged']} pairs judged" if "n_judged" in c else ""
        typer.echo(f"\nContradiction scan  ({c['n_lessons']} lessons{judged}, "
                   f"{len(c['contradictions'])} contradictory pairs, 1 sample/pair){note}")
        for pair in c["contradictions"][:6]:
            typer.echo(f"  ⚠ A: {str(pair.get('a', ''))[:80]}")
            typer.echo(f"    B: {str(pair.get('b', ''))[:80]}  (score {pair.get('score')})")


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


@app.command(name="cross-run-concepts")
def cross_run_concepts_cmd(
    memory_dir: Path = typer.Argument(..., help="Cross-run memory dir (holds concept_capsules.jsonl), or "
                                                "the capsules file itself."),
    top: int = typer.Option(20, help="How many most-explored concepts to show."),
    as_json: bool = typer.Option(False, "--json", help="Emit the full overview as JSON."),
):
    """PART IV cross-run Step 3 (§21.20): portfolio overview over the per-run CONCEPT capsules written when
    `cross_run_concepts` is on. Shows which concepts have been explored across the portfolio and in which
    runs — each with its OWN outcome (raw metrics are NOT compared across tasks). Pure read; no endpoint."""
    from looplab.engine.memory import ConceptCapsuleStore, portfolio_concept_overview
    p = Path(memory_dir)
    path = p if p.is_file() else p / "concept_capsules.jsonl"
    if not path.exists():
        typer.echo(f"no concept capsules at {path} (run with cross_run_concepts on to populate)")
        raise typer.Exit(1)
    ov = portfolio_concept_overview(ConceptCapsuleStore(path).all())
    if as_json:
        typer.echo(orjson.dumps(ov, option=orjson.OPT_INDENT_2).decode())
        return
    typer.echo(f"Cross-run portfolio: {ov['n_runs']} run(s), {ov['n_concepts']} concept(s)")
    for e in ov["concepts"][: max(0, top)]:
        def _fmt(r: dict) -> str:
            m = r.get("metric")
            return f"{r['run_id']}" + (f"={m:g}" if isinstance(m, (int, float)) and not isinstance(m, bool) else "")
        runs = ", ".join(_fmt(r) for r in e["runs"][:6])
        more = "" if len(e["runs"]) <= 6 else f" (+{len(e['runs']) - 6} more)"
        typer.echo(f"  {e['n_runs']:2d}×  {e['concept']}   [{runs}{more}]")


@app.command(name="cross-run-index")
def cross_run_index_cmd(
    run_root: Path = typer.Argument(..., help="Directory holding run subdirs (each with events.jsonl)."),
    as_json: bool = typer.Option(False, "--json", help="Emit the full run-facts index as JSON."),
):
    """PART IV cross-run Step 1 / CR0 (§21.20.3): build the portfolio index — each run's PASSPORT (scope)
    + FACTS (attempts/measurements) — by folding every `<run_root>/*/events.jsonl` (the migration over
    existing runs). Pure/deterministic: rebuilding from scratch yields the same index. No LLM/endpoint."""
    from looplab.engine.cross_run_index import rebuild_index_from_run_root
    idx = rebuild_index_from_run_root(run_root)
    if not idx:
        typer.echo(f"no runs with events.jsonl under {run_root}")
        raise typer.Exit(1)
    if as_json:
        typer.echo(orjson.dumps(idx, option=orjson.OPT_INDENT_2).decode())
        return
    typer.echo(f"Cross-run index: {len(idx)} run(s)")
    for f in idx:
        sc = f["scope"]
        best = f["best"]
        bm = f"best={best['metric']:g}" if best and isinstance(best.get("metric"), (int, float)) else "best=—"
        typer.echo(f"  {f['run_id']:20s} [{sc['task_id']}/{sc['direction']}/{sc['metric'] or '—'}]  "
                   f"{f['n_attempts']:2d} attempt(s)  {bm}")


@app.command(name="atlas")
def atlas_cmd(
    memory_dir: Path = typer.Argument(..., help="Cross-run memory dir (holds lessons.jsonl + "
                                                "concept_capsules.jsonl)."),
    max_items: int = typer.Option(8, help="Cap per section (explored/contested/thin)."),
    as_json: bool = typer.Option(False, "--json", help="Emit the full Atlas payload as JSON."),
):
    """PART IV cross-run Step 6 (§21.20): the Research Atlas DATA view — one 'what's been explored / where
    it's thin / what's contradictory' payload composing the concept overview (Step 3), claim assessments
    (Step 4) and the bounded context pack (Step 5). Pure read; the React screen is a later visual layer."""
    from looplab.engine.claims import portfolio_atlas
    from looplab.engine.memory import ConceptCapsuleStore
    from looplab.events.eventstore import read_jsonl_lenient
    base = Path(memory_dir)
    lessons_p, caps_p = base / "lessons.jsonl", base / "concept_capsules.jsonl"
    lessons = read_jsonl_lenient(lessons_p, loads=orjson.loads, dicts_only=True) if lessons_p.exists() else []
    caps = ConceptCapsuleStore(caps_p).all() if caps_p.exists() else []
    if not lessons and not caps:
        typer.echo(f"no cross-run memory at {base} (need lessons.jsonl and/or concept_capsules.jsonl)")
        raise typer.Exit(1)
    atlas = portfolio_atlas(lessons, caps, max_items=max_items)
    if as_json:
        typer.echo(orjson.dumps(atlas, option=orjson.OPT_INDENT_2).decode())
        return
    typer.echo(f"Research Atlas: {atlas['n_runs']} run(s), {atlas['n_concepts']} concept(s), "
               f"{atlas['n_claims']} claim(s), {atlas['n_contested']} contested")
    if atlas["explored"]:
        typer.echo("Explored (concept × #runs):")
        for e in atlas["explored"]:
            typer.echo(f"  {e['n_runs']:2d}×  {e['concept']}")
    if atlas["thin_coverage"]:
        typer.echo("Thin (explored once): " + ", ".join(atlas["thin_coverage"]))
    if atlas["contradictions"]:
        typer.echo("Contradictions (portfolio disagrees):")
        for c in atlas["contradictions"]:
            typer.echo(f"  ⚖ [{c['n_support']}↑/{c['n_oppose']}↓] {c['statement'][:100]}")


@app.command(name="claims")
def claims_cmd(
    memory_dir: Path = typer.Argument(..., help="Cross-run memory dir (holds lessons.jsonl), or the "
                                                "lessons file itself."),
    top: int = typer.Option(20, help="How many most-evidenced claims to show."),
    contested_only: bool = typer.Option(False, "--contested", help="Show only MIXED (support+oppose) claims."),
    pack: bool = typer.Option(False, "--pack", help="Render the bounded agent context pack (Step 5) instead."),
    as_json: bool = typer.Option(False, "--json", help="Emit the full assessments as JSON."),
):
    """PART IV cross-run Step 4/5 (§21.20): project the distilled lessons into evidence-grounded CLAIMS —
    each with support vs oppose node-id evidence and an epistemic state (supported/refuted/mixed/
    inconclusive). Contested (`mixed`) claims are where the portfolio disagrees with itself. `--pack`
    renders the bounded agent context pack (contested-first, caveat slot reserved). Pure read of
    `<memory_dir>/lessons.jsonl` (unifies with the D8 claim shape); no LLM/endpoint."""
    from looplab.engine.claims import build_context_pack, claim_assessments, render_context_pack
    from looplab.engine.memory import ConceptCapsuleStore, portfolio_concept_overview
    from looplab.events.eventstore import read_jsonl_lenient
    p = Path(memory_dir)
    path = p if p.is_file() else p / "lessons.jsonl"
    if not path.exists():
        typer.echo(f"no lessons at {path}")
        raise typer.Exit(1)
    lessons = read_jsonl_lenient(path, loads=orjson.loads, dicts_only=True)
    claims = claim_assessments(lessons)
    if pack:
        # compose with the concept overview (Step 3) from the same memory dir when present
        base = p if p.is_dir() else p.parent
        caps_path = base / "concept_capsules.jsonl"
        overview = (portfolio_concept_overview(ConceptCapsuleStore(caps_path).all())
                    if caps_path.exists() else None)
        cp = build_context_pack(claims, concept_overview=overview, max_claims=top)
        typer.echo(orjson.dumps(cp, option=orjson.OPT_INDENT_2).decode() if as_json
                   else (render_context_pack(cp) or "(empty context pack)"))
        return
    if contested_only:
        claims = [c for c in claims if c["epistemic"] == "mixed"]
    if as_json:
        typer.echo(orjson.dumps(claims, option=orjson.OPT_INDENT_2).decode())
        return
    _mark = {"supported": "✓", "refuted": "✗", "mixed": "⚖", "inconclusive": "·"}
    typer.echo(f"Claims ({len(claims)} shown{' — contested only' if contested_only else ''}): "
               "✓ supported  ✗ refuted  ⚖ mixed  · inconclusive")
    for c in claims[: max(0, top)]:
        typer.echo(f"  {_mark.get(c['epistemic'], '?')} [{c['n_support']}↑/{c['n_oppose']}↓] "
                   f"{c['statement'][:100]}")
