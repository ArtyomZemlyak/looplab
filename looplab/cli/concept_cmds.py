"""Part IV concept / novelty diagnostics: `concept-coverage`, `asset-brief`, `lock-in`,
`board-dedup`, `research-targets`, `novelty-recall`, `lesson-guard`.

Split out of `inspect_cmds.py` (doc 25 CT-01), which had accumulated three unrelated command
domains behind a docstring naming four commands.

These are offline analyses over ONE run that may invoke an LLM to tag/grade — agentic by default,
with an `--offline` heuristic fallback, so by default they send node code and logs to the endpoint
the run was pinned to. Read-only over the run EXCEPT `concept-coverage --persist`, which retro-tags
a finished run by appending generation-fenced `EV_NODE_CONCEPTS` under the engine lock.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from looplab.core.concepts import MAX_MATERIALIZED_CONCEPTS, normalize_concept_id
from looplab.core.models import (NODE_CONCEPT_PROVENANCE_CLASSIFIER,
                                 NODE_CONCEPT_PROVENANCE_OPERATOR,
                                 NODE_CONCEPT_PROVENANCE_UNTRUSTED,
                                 node_concept_event_provenance)
from looplab.events.eventstore import EventStoreConcurrencyError
from looplab.events.replay import fold
from looplab.events.types import EV_FINALIZE_STEP, EV_NODE_CONCEPTS, EV_RUN_FINISHED
from looplab.cli import (_engine_singleton, _make_llm_client, _require_run_dir,
                         _settings_for_run, app)


def _persist_node_concepts(store, raw_tags, mode: str, vocab_size: int, *,
                           expected_last_seq: int | None = None,
                           require_lock: bool = False,
                           node_modes: Optional[dict[int | str, str]] = None) -> int:
    """A2 (retro-tag): append `EV_NODE_CONCEPTS` per node so the built tags FOLD into
    `state.node_concepts` and the UI (ConceptChipBar/ConceptView) + cross-run readers see them —
    otherwise `concept-coverage` computes exactly these tags and throws them away after printing.

    This is the one MUTATING affordance in this module, gated behind `--persist`, and is intended for
    FINISHED runs (retro-tagging a run created before Phase 0, or refreshing a stale map). Events carry
    exact producer provenance and are generation-fenced (`generation == node.attempt`). Offline heuristic
    membership is display-only; agentic/LLM membership is classifier evidence. Same-source replay is a
    no-op, while an agentic replay upgrades identical heuristic ids once. Operator edits still win.
    `node_modes` preserves per-node fallback provenance for mixed batches. Classifier `[]` is durable
    known-empty evidence; empty heuristic output stays absent. Every persisted row is canonicalized,
    deduplicated, and lexically capped to the replay membership bound.
    Returns the number of nodes tagged."""
    default_provenance = node_concept_event_provenance({"mode": mode})
    if default_provenance == NODE_CONCEPT_PROVENANCE_UNTRUSTED:
        raise ValueError(f"unsupported node-concept producer mode: {mode!r}")
    events = store.read_all()
    tail = events[-1].seq if events else -1
    if expected_last_seq is not None and tail != expected_last_seq:
        raise EventStoreConcurrencyError(store.path, expected_last_seq, tail)
    # Fold inside the mutation transaction, from the events this call just read under the lock. The
    # caller's pre-analysis state can be minutes old after an agentic build and must never choose
    # provenance/idempotency on its own — which is why this takes no `state` parameter at all
    # (doc 25 CT-11): accepting one and then unconditionally discarding it invited a reader to
    # believe the caller's fold mattered here.
    state = fold(events)
    known = dict(getattr(state, "node_concepts", {}) or {})
    provenance = dict(getattr(state, "node_concept_provenance", {}) or {})
    count = 0
    for nid, ft in (raw_tags or {}).items():
        nid = int(nid)
        node = state.nodes.get(nid)
        if node is None:
            continue
        # preserve mixed-batch provenance through retro-tag persistence too; a heuristic
        # fallback row must remain display-only even when the command's default mode is agentic.
        modes = node_modes or {}
        row_mode = modes.get(nid, modes.get(str(nid), mode))
        requested_provenance = node_concept_event_provenance({"mode": row_mode})
        if requested_provenance == NODE_CONCEPT_PROVENANCE_UNTRUSTED:
            raise ValueError(f"unsupported node-concept producer mode: {row_mode!r}")
        raw_ids = list(ft)
        normalized = [normalize_concept_id(c) for c in raw_ids]
        if (requested_provenance == NODE_CONCEPT_PROVENANCE_CLASSIFIER
                and (any(cid is None for cid in normalized)
                     or len(raw_ids) > MAX_MATERIALIZED_CONCEPTS)):
            # retro-tagging is the same evidence boundary as the live cadence. A malformed
            # or over-wide classifier row is at most a bounded display fallback, never a trusted subset.
            row_mode = "offline-heuristic"
            requested_provenance = node_concept_event_provenance({"mode": row_mode})
        ids = sorted({cid for cid in normalized if cid})[:MAX_MATERIALIZED_CONCEPTS]
        # A successful classifier `[]` is durable known-empty evidence and prevents endless re-tagging.
        # An empty heuristic fallback says nothing independently, so keep it absent/pending.
        if not ids and requested_provenance != NODE_CONCEPT_PROVENANCE_CLASSIFIER:
            continue
        current_provenance = provenance.get(nid)
        if current_provenance == NODE_CONCEPT_PROVENANCE_OPERATOR:
            continue
        # A coarse fallback can fill an authored/empty display, but it must never replace or downgrade
        # reviewed classifier evidence. Conversely, identical agentic ids append once when the current
        # receipt is heuristic: value equality cannot stand in for provenance equality.
        if (current_provenance == NODE_CONCEPT_PROVENANCE_CLASSIFIER
                and requested_provenance != NODE_CONCEPT_PROVENANCE_CLASSIFIER):
            continue
        if known.get(nid) == ids and current_provenance == requested_provenance:
            continue
        event = store.append(
            EV_NODE_CONCEPTS,
            {"node_id": nid, "concepts": ids, "mode": row_mode,
             "at_vocab": int(vocab_size), "generation": node.attempt},
            expected_last_seq=tail,
            require_lock=require_lock,
        )
        tail = event.seq
        known[nid] = ids
        provenance[nid] = requested_provenance
        count += 1
    return count


def _retro_tag_finished(events, state) -> bool:
    """Whether a folded run is at a durable, quiescent terminal boundary.

    ``finished`` flips at ``run_finished`` before the engine performs its finalization checklist.
    Modern runs additionally require the exact ``finalization_finished`` acknowledgement and no
    recoverable scoped checklist. Markerless legacy finishes remain compatible because replay
    explicitly maps that historical protocol to ``finalized_finish_seq == last_finish_seq``.
    """
    if (not getattr(state, "finished", False)
            or getattr(state, "last_finish_seq", -1) < 0
            or state.resume_pending()
            or state.finalization_pending()):
        return False
    finish = next(
        (event for event in events
         if event.seq == state.last_finish_seq and event.type == EV_RUN_FINISHED),
        None,
    )
    if finish is None or state.finalized_finish_seq != state.last_finish_seq:
        return False
    finish_data = finish.data or {}
    if "finalize_scope" in finish_data:
        scope = finish_data.get("finalize_scope")
        if not isinstance(scope, str) or not scope:
            return False
        # ``incomplete_finalize_scope`` intentionally forgets a scope invalidated by a
        # later foreign event. Absence from that recovery queue is therefore not proof that the modern
        # terminal checklist completed; the accepted finish itself must have its durable success marker.
        if not any(
            event.type == EV_FINALIZE_STEP
            and (event.data or {}).get("scope") == scope
            and (event.data or {}).get("step") == "complete"
            for event in events
        ):
            return False
    from looplab.engine.finalize import incomplete_finalize_scope
    return incomplete_finalize_scope(events) is None


def _optional_client(run_dir, model, fallback: str, *, unavailable: str = "no LLM endpoint"):
    """``(settings, client | None)`` for a diagnostic that DEGRADES without an endpoint (doc 25 CT-14).

    Five commands wrote this block out, differing only in the fallback wording. Two things it keeps
    doing, both easy to lose in a copy:

    * the settings come from the RUN's pinned endpoint (`config.snapshot.json`) with ambient settings
      as the fallback, so a diagnostic sends node code and logs where the run was pinned rather than
      wherever the operator's shell happens to point;
    * an unreachable endpoint is NOTED, not swallowed. A silent `client = None` makes a heuristic
      result indistinguishable from an agentic one in the printed output.

    `lesson-guard` deliberately does NOT use this: it is LLM-only and EXITS 1 rather than degrading,
    which is a different contract, not a different message.
    """
    settings = _settings_for_run(run_dir, model)
    try:
        return settings, _make_llm_client(settings)
    except Exception as e:  # noqa: BLE001 — no endpoint => degrade to the offline path, noted
        typer.echo(f"({unavailable}: {e}; {fallback})")
        return settings, None


def _run_tools_for(state):
    """Read-only run tools bound to `state` for AGENTIC tagging/briefing.
    None on any failure -> the caller runs the plain (non-agentic) LLM path."""
    from looplab.tools.run_tools import readonly_run_tools
    return readonly_run_tools(state)


def _concept_map_for(state, resolved_type, *, offline, model=None, repo=None, run_dir=None):
    """Shared PART IV D5 build — AGENTIC by default (the LLM agent grows the graph, tags, derives the
    per-task importance; `build_concept_map`), the deterministic alias heuristic only as the `--offline`
    fallback. Returns {graph, tags, important_uncovered, mode, brief}. This is what makes every Phase-1
    diagnostic (lock-in / board-dedup / research-targets) agentic-first and universal, not
    heuristic-hardcoded (§21.13/§21.15 correction)."""
    from looplab.search.concept_graph import skeleton_for
    from looplab.search.concept_map import build_concept_map
    from looplab.search.concept_tagging import tag_nodes_heuristic
    seed = skeleton_for(resolved_type)
    seed = seed if seed.concepts() else None

    # Agentic-BY-DEFAULT (the agentic-first concept, §21.13/§21.15): the map is LLM-built unless the caller
    # passes --offline, so this path DOES send node code/logs to the configured endpoint by default. The
    # cost/privacy contract is stated up front in each command's --offline help + docs/guide/cli-reference.md
    # ("Agentic by default … sends node code/logs … pass --offline for the local heuristic"). `asset-brief`
    # keeps the inverse (--llm opt-in) because ITS agentic path is a much heavier full tool-loop.
    if not offline:
        # `settings`, not `_settings`: the agentic branch below reads `settings.llm_parser`.
        # The underscore made it look unused, and the read raised NameError the moment an
        # endpoint WAS reachable — every offline test takes the `client is None` path, so
        # nothing ever went red (found by the doc 25 CT-01 split).
        settings, client = _optional_client(
            run_dir, model, "using the offline heuristic fallback")
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
    jobs: int = typer.Option(
        8, "--jobs", "-j", help="Concurrent node-tagging LLM calls (the agentic build tags each experiment "
                                "independently; retro-tagging a large finished run is ~O(nodes) sequential "
                                "otherwise). 1 = sequential. Quality is unchanged — the vocabulary still "
                                "grows between batches and consolidation dedups synonyms."),
    persist: bool = typer.Option(
        False, "--persist", help="RETRO-TAG: append the built tags as generation-fenced EV_NODE_CONCEPTS "
                                 "events so they FOLD into node_concepts and show in the UI. Agentic tags "
                                 "become eligible for replay-derived cross-run indexes; this command does "
                                 "not rebuild finalized capsule memory. --offline tags are display-only. "
                                 "Requires a fully finalized, non-running FINISHED run."),
):
    """PART IV D5 (§21.11): the concept-graph coverage + uncovered-region diagnostic. **The LLM agent builds
    the map** by default — it grows the concept vocabulary from the actual experiments (reading each node's
    code/logs), computes the coverage, and derives the important-but-uncovered directions per task (universal:
    no hardcoded winning region; grounded in `--repo`'s prior-art brief when given). `--offline` forces the
    deterministic alias-heuristic fallback (needs a curated `--task-type` pack, no importance derivation)."""
    from looplab.search.concept_analytics import concept_report
    from looplab.search.concept_graph import skeleton_for
    from looplab.search.concept_map import build_concept_map
    from looplab.search.concept_tagging import tag_nodes_heuristic
    store = _require_run_dir(run_dir)
    snapshot_events = store.read_all()
    state = fold(snapshot_events)
    snapshot_tail = snapshot_events[-1].seq if snapshot_events else -1
    if persist:
        if not _retro_tag_finished(snapshot_events, state):
            typer.echo("refusing --persist: the run is not at a fully finalized FINISHED boundary. "
                       "Wait for terminal wrap-up to complete; stopped, finalizing, or resume-pending "
                       "runs cannot be retro-tagged.")
            raise typer.Exit(code=2)
        # ``finished=True`` precedes terminal write-out. Probe the same singleton the engine owns so a
        # still-live driver cannot race the expensive analysis; reacquire it for the actual CAS below.
        try:
            with _engine_singleton(run_dir) as available:
                if not available:
                    typer.echo("refusing --persist: the finished run's engine is still writing terminal "
                               "artifacts; wait for engine.lock to be released.")
                    raise typer.Exit(code=2)
        except typer.Exit:
            raise
        except RuntimeError as exc:
            typer.echo(f"refusing --persist: cannot prove exclusive run ownership: {exc}")
            raise typer.Exit(code=2) from exc
    resolved_type = task_type or state.task_id or ""
    # A curated pack is only a SEED / starting vocabulary the agent expands (like agentic_asset_brief's
    # seed_scan); None => the LLM builds the graph from scratch (works on any task).
    seed = skeleton_for(resolved_type)
    seed = seed if seed.concepts() else None

    def _persist_exact(raw_tags, mode: str, vocab_size: int,
                       node_modes: Optional[dict[int | str, str]] = None) -> int:
        """Commit tags only against the exact finished snapshot the analysis inspected."""
        try:
            with _engine_singleton(run_dir) as owned:
                if not owned:
                    typer.echo("refusing --persist: the engine reacquired engine.lock while concept tags "
                               "were being built; discard this stale analysis and retry after it exits.")
                    raise typer.Exit(code=2)
                current_events = store.read_all()
                current = fold(current_events)
                if not _retro_tag_finished(current_events, current):
                    typer.echo("refusing --persist: the run left its finalized FINISHED boundary while "
                               "concept tags were being built; discard this stale analysis and retry.")
                    raise typer.Exit(code=2)
                current_tail = current_events[-1].seq if current_events else -1
                if current_tail != snapshot_tail:
                    typer.echo("refusing --persist: events.jsonl changed while concept tags were being "
                               "built; re-run against the new exact snapshot.")
                    raise typer.Exit(code=2)
                return _persist_node_concepts(
                    store,
                    raw_tags,
                    mode,
                    vocab_size,
                    expected_last_seq=snapshot_tail,
                    require_lock=True,
                    node_modes=node_modes,
                )
        except typer.Exit:
            raise
        except EventStoreConcurrencyError as exc:
            typer.echo(f"refusing --persist: events.jsonl changed during the CAS append: {exc}")
            raise typer.Exit(code=2) from exc
        except RuntimeError as exc:
            typer.echo(f"refusing --persist: cannot prove exclusive durable mutation: {exc}")
            raise typer.Exit(code=2) from exc

    client = None
    if not offline:
        # `settings`, not `_settings`: the agentic branch below reads `settings.llm_parser`.
        # The underscore made it look unused, and the read raised NameError the moment an
        # endpoint WAS reachable — every offline test takes the `client is None` path, so
        # nothing ever went red (found by the doc 25 CT-01 split).
        settings, client = _optional_client(
            run_dir, model, "using the offline heuristic fallback")

    if client is None:
        # Deterministic FALLBACK. Needs a curated seed to localize anything.
        graph = seed or skeleton_for(resolved_type)
        if not graph.concepts():
            typer.echo(f"note: no curated concept pack for task-type '{resolved_type or 'unknown'}', so the "
                       "offline heuristic can't tag experiments. Drop --offline to let the agent build the "
                       "graph, or pass --task-type <known-pack> (e.g. dense-retrieval).")
        tags = tag_nodes_heuristic(state, graph)
        typer.echo(concept_report(state, graph, tags))
        if graph.concepts():
            typer.echo("\nnote: --offline alias tagging is coarse (over-reports coverage on semantically-"
                       "ambiguous concepts). Drop --offline for the agentic, code-reading build + per-task "
                       "importance.")
        if persist:
            n = _persist_exact(tags, "offline-heuristic", len(graph.concepts()))
            typer.echo(f"\n  persisted {n} node_concepts events (offline-heuristic) -> this run now shows "
                       "concepts in the UI. (coarse; re-run without --offline for code-read tags.)")
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
                             parser=settings.llm_parser, max_workers=jobs)
    typer.echo(concept_report(state, cmap["graph"], cmap["tags"]))
    typer.echo(f"\n  (built by the LLM agent — mode={cmap['mode']}, "
               f"{len(cmap['graph'].concepts())} concepts grown)")
    if persist:
        # raw_tags are the tagger's pre-consolidation ids (what the live cadence records); the fold
        # re-derives consolidation/coverage from them, so persisting these keeps parity with a live run.
        n = _persist_exact(cmap.get("raw_tags"), cmap.get("mode", "agentic"),
                           len(cmap["graph"].concepts()), cmap.get("raw_tag_modes"))
        typer.echo(f"  persisted {n} node_concepts events -> this run now shows concepts in the UI and "
                   "exposes classifier tags to replay-derived indexes. Existing finalized capsule memory "
                   "is not rebuilt by this command.")
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
        # asset-brief sweeps a repo, not a run directory, so there is no config snapshot to resolve
        # here: `run_dir=None` starts from ambient settings with the explicit --model override on top.
        _settings, client = _optional_client(
            None, model, "using the offline scan", unavailable="--llm unavailable")
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
    m = _concept_map_for(state, task_type or state.task_id or "", offline=offline, model=model,
                         run_dir=run_dir)
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
    from looplab.search.concept_tagging import tag_text, tag_text_llm
    from looplab.search.taxonomy_dedup import dedup_report
    store = _require_run_dir(run_dir)
    state = fold(store.read_all())
    m = _concept_map_for(state, task_type or state.task_id or "", offline=offline, model=model,
                         run_dir=run_dir)
    # dedup works over HYPOTHESIS tags (keyed by hypothesis id), NOT the node tags in m["tags"] (keyed by
    # node id). HT (§21.18) hypothesis-tag precedence:
    #   --offline           -> force the deterministic tag_text heuristic (bypass any recorded cache);
    #   recorded cache covers the board -> use it (tags=None -> dedup_analysis reads hypothesis_concepts);
    #   otherwise + a client -> tag the board LIVE agentically against the agent-built graph;
    #   else                 -> tag_text heuristic.
    # 1 card = 1 hypothesis: tag the single Card board. Read the DISPLAY `statement` (== the old
    # Hypothesis.statement, including a merged card's consolidated wording). A plain research card's id IS
    # its seed-statement hash (the old hypothesis id), but a migrated native card-N is not — so the
    # `hypothesis_concepts` cache is joined by BOTH keys (see `hypothesis_concept_cache_keys` below).
    hyps = list(state.research_cards())
    cache = getattr(state, "hypothesis_concepts", None) or {}
    # Cache-covered if ANY current-board card's tags are recorded under its id OR its seed-statement hash
    # (peer review): a migrated native `card-N` card kept legacy tags under the statement hash, so the
    # id-only test wrongly declared the board uncached and bought a redundant live tagging pass.
    from looplab.core.models import hypothesis_concept_cache_keys
    board_cached = any(any(key in cache for key in hypothesis_concept_cache_keys(h)) for h in hyps)
    tags, label = None, "heuristic"
    if offline:
        tags = {h.id: tag_text(h.statement, m["graph"], allow_plural=True) for h in hyps}
        label = "heuristic (--offline)"
    elif board_cached:
        label = "recorded/agentic"                      # dedup_analysis reads the cache (per-item fallback)
    elif hyps:
        _settings, client = _optional_client(
            run_dir, model, "using the heuristic hypothesis tagger")
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
    max_pairs: int = typer.Option(60, "--max-pairs", min=0, max=100000,
                                  help="Call-budget knob: adjudicate at most this many of the most-similar "
                                       "candidate pairs with the LLM (each pair = one call). Lower it to cap "
                                       "cost/data. Bounded non-negative: a negative value would slice the "
                                       "whole internal pool."),
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
        settings, client = _optional_client(run_dir, model, "showing candidate pairs only")
        if client is not None:
            parser = settings.llm_parser
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
    settings = _settings_for_run(run_dir, model)
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
