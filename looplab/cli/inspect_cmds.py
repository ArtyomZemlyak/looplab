"""Read-only inspection commands: `replay` / `timings` / `inspect` / `tensorboard`.

Split verbatim out of the flat `looplab/cli.py` (docs/15 §P5.2). Read-only over a run: pure folds of
the event log, viewers over run-dir sidecars, and (the Part IV concept/novelty diagnostics) offline
analyses that may invoke an LLM to tag/grade. Read-only EXCEPT for `concept-coverage --persist`, which
retro-tags a run by appending generation-fenced `EV_NODE_CONCEPTS` (the one opt-in mutation; see
`_persist_node_concepts`).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import orjson
import typer

from looplab.core.concepts import MAX_MATERIALIZED_CONCEPTS, normalize_concept_id
from looplab.engine.governance_health import GovernanceLedgerUnavailable
from looplab.core.models import (NODE_CONCEPT_PROVENANCE_CLASSIFIER,
                                 NODE_CONCEPT_PROVENANCE_OPERATOR,
                                 NODE_CONCEPT_PROVENANCE_UNTRUSTED,
                                 node_concept_event_provenance)
from looplab.events.eventstore import EventStore, EventStoreConcurrencyError, EventStoreLockError
from looplab.events.replay import fold
from looplab.events.types import EV_FINALIZE_STEP, EV_NODE_CONCEPTS, EV_RUN_FINISHED
from looplab.cli import _engine_singleton, _print_result, _require_run_dir, app


def _governance_cli_error(exc: GovernanceLedgerUnavailable | EventStoreLockError):
    ledger = exc.ledger if isinstance(exc, GovernanceLedgerUnavailable) else "governance"
    reason = exc.reason if isinstance(exc, GovernanceLedgerUnavailable) else "lock_unavailable"
    typer.echo(
        f"governance unavailable: ledger={ledger}, reason={reason}; "
        "repair the ledger before retrying",
        err=True,
    )
    raise typer.Exit(2) from exc


def _governance_cli_read(project):
    """Fail closed with a bounded operator-facing message, never a poisoned row/traceback."""
    try:
        return project()
    except (GovernanceLedgerUnavailable, EventStoreLockError) as exc:
        _governance_cli_error(exc)
    except OSError:
        # CODEX AGENT: an unreadable source is unknown, never an empty portfolio. Keep the OS path and
        # platform parser text out of CLI output while preserving argument/validation ValueErrors.
        _governance_cli_error(
            GovernanceLedgerUnavailable("cross_run_sources", "storage_unreadable"))


def _safe_steward_error(exc: Exception, phase: str) -> str:
    """Classify a paid failure without persisting provider text, endpoints, paths, or credentials."""
    from looplab.serve.assistant import safe_assistant_failure

    return f"{phase}:{safe_assistant_failure(exc)['error_kind']}"


def _run_cli_steward(memory_dir: Path, kind: str, action_id: str, *, prepare, invoke,
                     request: Optional[dict] = None) -> dict:
    """Run/replay the shared durable paid-steward transaction and map its closed states to CLI errors."""
    from datetime import datetime, timezone

    from looplab.engine.steward_invocation import run_steward_invocation

    if not action_id:
        typer.echo("error: --action-id is required for durable paid-call recovery")
        raise typer.Exit(2)
    try:
        record, replayed = _governance_cli_read(lambda: run_steward_invocation(
            memory_dir, kind, action_id, actor="local-operator",
            at=datetime.now(timezone.utc).isoformat(), prepare=prepare, invoke=invoke,
            safe_error=_safe_steward_error, request=request,
        ))
    except ValueError as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(2) from exc
    invocation_id = str(record.get("action_id") or record.get("invocation_id") or "")
    if record.get("action") == "steward-invocation-begun":
        # CODEX AGENT: this is not a retryable provider error. The old process may have paid already;
        # preserving the identity as ambiguous is the only fail-closed crash/restart outcome.
        typer.echo(
            "steward invocation outcome is unknown; the same --action-id will not call the model again. "
            "Review the ambiguous attempt before intentionally choosing a new action id."
        )
        raise typer.Exit(2)
    if record.get("outcome") == "error":
        typer.echo(f"steward failed ({record.get('error') or 'unknown_failure'})")
        raise typer.Exit(1)
    return {
        "proposals": record.get("proposals") or {},
        "receipt": record.get("receipt"),
        "invocation": {
            "action_id": invocation_id, "revision": record.get("revision"),
            "outcome": record.get("outcome"), "replayed": replayed,
        },
    }


def _echo_cli_invocation(output: dict) -> None:
    invocation = output["invocation"]
    suffix = " (replayed)" if invocation.get("replayed") else ""
    typer.echo(
        f"(invocation {invocation['action_id']} @ revision {invocation['revision']}{suffix})")


def _persist_node_concepts(store, state, raw_tags, mode: str, vocab_size: int, *,
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
    # CODEX AGENT: re-fold inside the mutation transaction. The caller's pre-analysis state can be
    # minutes old after an agentic build and must never choose provenance/idempotency on its own.
    state = fold(events)
    known = dict(getattr(state, "node_concepts", {}) or {})
    provenance = dict(getattr(state, "node_concept_provenance", {}) or {})
    count = 0
    for nid, ft in (raw_tags or {}).items():
        nid = int(nid)
        node = state.nodes.get(nid)
        if node is None:
            continue
        # CODEX AGENT: preserve mixed-batch provenance through retro-tag persistence too; a heuristic
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
            # CODEX AGENT: retro-tagging is the same evidence boundary as the live cadence. A malformed
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
        # CODEX AGENT: ``incomplete_finalize_scope`` intentionally forgets a scope invalidated by a
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
    """Show the raw launch config snapshot + the run's current folded best result.

    Five holdout/verifier settings are committed by ``run_started`` and can therefore differ from
    an old or hand-edited snapshot. The owner config API overlays those effective folded values;
    this diagnostic deliberately prints the on-disk snapshot verbatim for inspection.
    """
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


def _settings_for_run(run_dir=None, model=None):
    """Load the run's launch Settings snapshot so a diagnostic sends run code/logs to the same
    endpoint recorded for that run, not a possibly different ambient endpoint. Falls back to ambient
    Settings when the snapshot is absent/unreadable; ``model`` is the only explicit override.

    This helper needs endpoint/model provenance, not the five event-pinned holdout/verifier fields;
    the effective per-run config API owns that latter overlay.
    """
    from looplab.core.config import Settings
    settings = None
    try:
        snap = (Path(run_dir) / "config.snapshot.json") if run_dir is not None else None
        if snap is not None and snap.exists():
            import json
            data = json.loads(snap.read_text(encoding="utf-8"))
            data.pop("llm_api_key", None)   # masked in the snapshot; re-read from env/default
            settings = Settings(**data)
    except Exception:  # noqa: BLE001 — any snapshot issue -> ambient fallback
        settings = None
    if settings is None:
        settings = Settings()
    if model is not None:
        settings.llm_model = model
    return settings


def _concept_map_for(state, resolved_type, *, offline, model=None, repo=None, run_dir=None):
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
        # Use the RUN's pinned endpoint (config.snapshot.json), not ambient Settings, so a diagnostic
        # sends node code/logs where the run was pinned (CODEX #1); ambient fallback + `model` override.
        settings = _settings_for_run(run_dir, model)
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
    from looplab.search.concept_graph import (build_concept_map, concept_report, skeleton_for,
                                              tag_nodes_heuristic)
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
                    current,
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
        settings = _settings_for_run(run_dir, model)
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
        # asset-brief sweeps a repo, not a run directory, so there is no config snapshot to resolve here.
        # Start from ambient settings and layer the explicit --model override on top.
        settings = _settings_for_run(None, model)
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
    from looplab.search.concept_graph import tag_text, tag_text_llm
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
            settings = _settings_for_run(run_dir, model)
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
        settings = _settings_for_run(run_dir, model)
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
    import stat

    from looplab.engine.governance_health import observed_path_missing, project_governed_sources
    from looplab.engine.memory import ConceptCapsuleStore, portfolio_concept_overview
    p = Path(memory_dir)

    def _snapshot():
        try:
            mode = p.stat().st_mode
        except FileNotFoundError:
            path, base = p / "concept_capsules.jsonl", p
        else:
            if stat.S_ISREG(mode):
                path, base = p, p.parent
            elif stat.S_ISDIR(mode):
                path, base = p / "concept_capsules.jsonl", p
            else:
                return p, None

        canonical = path.absolute() == (base / "concept_capsules.jsonl").absolute()

        def _project(governance):
            if observed_path_missing(path):
                return None
            # CODEX AGENT: read capsules and apply taxonomy while both policy and evidence locks are held;
            # separate reads can otherwise render a merge against a capsule generation that never coexisted.
            return portfolio_concept_overview(
                ConceptCapsuleStore(path).all(), aliases=governance["aliases"],
                splits=governance["splits"])

        overview = project_governed_sources(
            base, _project, include_concepts=True,
            source_names=(("concept_capsules.jsonl",) if canonical else ()),
            source_paths=(() if canonical else (path.absolute(),)),
        )
        return path, overview

    path, ov = _governance_cli_read(_snapshot)
    if ov is None:
        typer.echo(f"no concept capsules at {path} (run with cross_run_concepts on to populate)")
        raise typer.Exit(1)
    if as_json:
        typer.echo(orjson.dumps(ov, option=orjson.OPT_INDENT_2).decode())
        return
    typer.echo(f"Cross-run portfolio: {ov['n_runs']} run(s), {ov['n_concepts']} concept(s)")
    if ov.get("source_complete") is not True:
        typer.echo("  WARNING: capsule source is partial: "
                   f"{ov.get('source_concepts_omitted', 0)} concept(s), "
                   f"{ov.get('source_outcomes_omitted', 0)} outcome(s) known omitted"
                   + (f"; {ov.get('source_unknown_capsules', 0)} legacy capsule(s) have unknown totals"
                      if ov.get("source_unknown_capsules", 0) else "")
                   + (f"; {ov.get('source_rows_quarantined', 0)} durable row(s) quarantined"
                      if ov.get("source_rows_quarantined", 0) else ""))
    # CODEX AGENT: capsule-source completeness and this read-model's display cap are independent. The
    # headline is an exact retained total, so text mode must disclose when its backing row projection is not.
    if ov.get("concepts_omitted"):
        typer.echo(f"  Bounded overview omitted {ov['concepts_omitted']} concept row(s); "
                   "use --json for projection receipts.")
    typer.echo("  (rank = RAW per-concept +better/~neutral/-worse-half sign counts across its runs; "
               "advisory, relative rank not causal profit)")
    for e in ov["concepts"][: max(0, top)]:
        def _fmt(r: dict) -> str:
            m = r.get("metric")
            return f"{r['run_id']}" + (f"={m:g}" if isinstance(m, (int, float)) and not isinstance(m, bool) else "")
        runs = ", ".join(_fmt(r) for r in e["runs"][:6])
        more = "" if len(e["runs"]) <= 6 else f" (+{len(e['runs']) - 6} more)"
        h, nu, t = e.get("n_helped", 0), e.get("n_neutral", 0), e.get("n_hurt", 0)
        profit = f"  +{h}/~{nu}/-{t}" if (h + nu + t) else ""
        typer.echo(f"  {e['n_runs']:2d}×  {e['concept']}   [{runs}{more}]{profit}")


@app.command(name="cross-run-index")
def cross_run_index_cmd(
    run_root: Path = typer.Argument(..., help="Directory holding run subdirs (each with events.jsonl)."),
    as_json: bool = typer.Option(False, "--json", help="Emit the full run-facts index as JSON."),
    incremental: bool = typer.Option(False, "--incremental", help="Reuse a persisted source-digest cache "
                                     "(<run_root>/.cross_run_index.json); only re-fold CHANGED runs and "
                                     "report built/cached/skipped receipts."),
):
    """PART IV cross-run Step 1 / CR0 (§21.20.3): build the portfolio index — each run's PASSPORT (scope)
    + FACTS (attempts/measurements) — by folding every `<run_root>/*/events.jsonl` (the migration over
    existing runs). Pure/deterministic: rebuilding from scratch yields the same index. With `--incremental`
    an on-disk cache skips unchanged runs and torn runs surface as explicit skip receipts. No LLM/endpoint."""
    from looplab.engine.cross_run_index import (
        build_index_incremental, load_index, rebuild_index_from_run_root, save_index,
    )
    if incremental:
        cache = run_root / ".cross_run_index.json"
        res = build_index_incremental(run_root, prior=load_index(cache))
        idx = res["index"]
        if idx:
            save_index(cache, res)
        rc = res["receipts"]
        if not as_json:
            skipped = f", {len(rc['skipped'])} skipped" if rc["skipped"] else ""
            typer.echo(f"(incremental: {len(rc['built'])} built, {len(rc['cached'])} cached{skipped})")
            for s in rc["skipped"]:
                typer.echo(f"  skip {s['dir']}: {s['reason']}")
    else:
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


@app.command(name="concept-merge")
def concept_merge_cmd(
    memory_dir: Path = typer.Argument(..., help="Cross-run memory dir (where concept_aliases.jsonl lives)."),
    from_concept: str = typer.Argument(..., help="The concept slug to merge away (or purge)."),
    to_concept: str = typer.Argument("", help="The canonical slug it becomes. Empty = PURGE (tombstone)."),
):
    """PART IV cross-run CR1a (§22.4) — the OPERATOR concept governance write: MERGE one concept slug into
    another (they become one across all cross-run views) or PURGE it (empty target → dropped from views).
    Non-destructive + reversible: append-only `concept_aliases.jsonl`, applied at READ time; the raw per-run
    tags are never rewritten. A self-link or cycle-closing edge is rejected. For the inverse (one coarse
    concept → several finer ones) use `concept-split`."""
    from looplab.engine.concept_registry import record_concept_alias
    import datetime as _dt
    try:
        rec = record_concept_alias(str(memory_dir), from_concept=from_concept, to_concept=to_concept,
                                   at=_dt.datetime.now().isoformat(timespec="seconds"))
    except (GovernanceLedgerUnavailable, EventStoreLockError) as e:
        _governance_cli_error(e)
    except ValueError as e:
        typer.echo(f"error: {e}")
        raise typer.Exit(2)
    if rec["to"]:
        typer.echo(f"merged: '{rec['from']}' -> '{rec['to']}'")
    else:
        typer.echo(f"purged: '{rec['from']}'")


@app.command(name="concept-split")
def concept_split_cmd(
    memory_dir: Path = typer.Argument(..., help="Cross-run memory dir (where concept_splits.jsonl lives)."),
    from_concept: str = typer.Argument(..., help="The coarse concept slug to split."),
    rule: list[str] = typer.Option([], "--rule", help="A re-tag rule 'target:term1,term2' — a run whose "
                                   "sibling concepts contain ANY term is re-tagged to target. Repeatable "
                                   "(ordered, first match wins)."),
    default: str = typer.Option("", "--default", help="Fallback target when no rule matches (else the "
                                "original slug is kept)."),
):
    """PART IV cross-run (§21.20.13) — the OPERATOR concept SPLIT: declare one coarse concept really covers
    several finer ones, RE-TAGGED per each run's OWN sibling concepts. Non-destructive + reversible:
    append-only `concept_splits.jsonl`, applied at READ time; raw per-run tags are never rewritten.
    Example: `concept-split MEM data/augmentation --rule 'data/hard-negative-mining:hard,negative' \\
    --rule 'data/synonym-aug:synonym,eda' --default data/augmentation`."""
    from looplab.engine.concept_registry import record_concept_split
    import datetime as _dt
    rules = []
    for spec in rule:
        target, _, terms = spec.partition(":")
        rules.append({"to": target.strip(), "when_any": [t.strip() for t in terms.split(",") if t.strip()]})
    try:
        rec = record_concept_split(str(memory_dir), from_concept=from_concept, rules=rules, default=default,
                                   at=_dt.datetime.now().isoformat(timespec="seconds"))
    except (GovernanceLedgerUnavailable, EventStoreLockError) as e:
        _governance_cli_error(e)
    except ValueError as e:
        typer.echo(f"error: {e}")
        raise typer.Exit(2)
    tgts = [r["to"] for r in rec["rules"]] + ([rec["default"]] if rec["default"] else [])
    typer.echo(f"split: '{rec['from']}' -> {{{', '.join(sorted(set(tgts)))}}} ({len(rec['rules'])} rule(s))")


@app.command(name="concept-steward")
def concept_steward_cmd(
    memory_dir: Path = typer.Argument(..., help="Cross-run memory dir (holds concept_capsules.jsonl)."),
    apply: bool = typer.Option(False, "--apply", help="DEPRECATED compatibility option; rejected before "
                               "any LLM call. The steward is proposal-only."),
    model: Optional[str] = typer.Option(None, help="Override model id."),
    max_proposals: int = typer.Option(12, help="Max curation proposals."),
    as_json: bool = typer.Option(False, "--json", help="Emit proposals + receipt as JSON."),
    action_id: str = typer.Option(
        "", "--action-id", help="Required stable id for at-most-once paid-call recovery."),
):
    """PART IV cross-run §21.20.13 / §22.4 — the AGENTIC taxonomy steward: an LLM reviews the cross-run
    concept graph and PROPOSES a curation (merge duplicate slugs / split conflated ones / purge noise).
    Proposal-only: review the exact output, then record selected operations through `concept-merge`,
    `concept-split`, or owner HTTP governance. The deprecated `--apply` option is rejected before any paid
    LLM call or mutation. A stable action id durably fences the paid call across crash/retry. Needs a
    reachable LLM."""
    if apply:
        typer.echo("error: --apply is deprecated and disabled; concept-steward is proposal-only. "
                   "Run without --apply, review the exact proposal, then use concept-merge/concept-split "
                   "or owner HTTP governance.")
        raise typer.Exit(2)
    from looplab.core.config import Settings
    from looplab.engine.concept_steward import curation_is_empty, steward_concepts
    from looplab.engine.concept_registry import concept_governance_snapshot
    from looplab.engine.governance_health import read_curation_rows

    def _preflight():
        concept_governance_snapshot(str(memory_dir))
        # CODEX AGENT: a paid proposal is not allowed to run against an unreadable invocation history.
        # Validate that history before even constructing a provider client so corruption cannot spend money.
        read_curation_rows(Path(memory_dir) / "concept_curation_log.jsonl")

    _governance_cli_read(_preflight)
    settings = Settings()
    if model:
        settings.llm_model = model   # the field is `llm_model`; model_copy(update={"model":...}) wrote a
        #                              phantom attr and silently kept the default endpoint (--model no-op)
    out = _run_cli_steward(
        memory_dir, "concept", action_id,
        prepare=lambda: _make_llm_client(settings),
        invoke=lambda client: steward_concepts(
            str(memory_dir), client, apply=False, max_proposals=max_proposals,
            raise_on_failure=True),
        request={
            "surface": "cli", "model": model or "", "max_proposals": max_proposals,
        },
    )
    if as_json:
        typer.echo(orjson.dumps(out, option=orjson.OPT_INDENT_2).decode())
        return
    prop = out["proposals"]
    if curation_is_empty(prop):
        typer.echo("steward: no curation proposed (graph already clean)")
        _echo_cli_invocation(out)
        return
    typer.echo(f"steward proposals — {len(prop['merges'])} merge(s), {len(prop['splits'])} split(s), "
               f"{len(prop['purges'])} purge(s):")
    for m in prop["merges"]:
        typer.echo(f"  merge  '{m['from_concept']}' -> '{m['to_concept']}'"
                   + (f"   ({m['why']})" if m.get("why") else ""))
    for s in prop["splits"]:
        typer.echo(f"  split  '{s['from_concept']}' -> {{{', '.join(r['to'] for r in s['rules'])}}}")
    for p in prop["purges"]:
        typer.echo(f"  purge  '{p['from_concept']}'")
    typer.echo("(proposal only — review the exact proposal above; apply selected changes with "
               "concept-merge/concept-split or owner HTTP governance)")
    _echo_cli_invocation(out)


@app.command(name="claim-decide")
def claim_decide_cmd(
    memory_dir: Path = typer.Argument(..., help="Cross-run memory dir (where claim_decisions.jsonl lives)."),
    statement: str = typer.Argument(..., help="The claim statement to decide on (matched by normalized text)."),
    ratify: bool = typer.Option(False, "--ratify", help="Operator RATIFIES the claim (surfaced first)."),
    reject: bool = typer.Option(False, "--reject", help="Operator REJECTS it (dropped from agent context)."),
    pin: bool = typer.Option(False, "--pin", help="Operator PINS it (kept, marked operator-pinned)."),
    note: str = typer.Option("", help="Optional rationale recorded with the decision."),
    scope: str = typer.Option("", "--scope", help="Task scope for the structured claim key (a decision is "
                              "scope-precise: it won't reach a same-worded claim in another task)."),
    metric: str = typer.Option("", "--metric", help="Metric qualifier from the reviewed claim."),
    claim_uid: str = typer.Option(
        "", "--claim-uid", help="Required stable UID from the reviewed structured claim."),
    evidence_digest: str = typer.Option(
        "", "--evidence-digest", help="Required evidence digest from the reviewed claim."),
    expected_revision: Optional[int] = typer.Option(
        None, "--expected-revision", min=0,
        help="Required claim-governance revision observed before this decision."),
    action_id: str = typer.Option(
        "", "--action-id", help="Required stable id for idempotent lost-response retry."),
):
    """PART V §22.4 — the OPERATOR governance write: ratify / reject / pin the exact live cross-run claim
    snapshot identified by UID, evidence digest and ledger revision. Agents can only read + cite. The
    append is idempotent by action id and rejected if the target/evidence/policy changed since review."""
    from looplab.engine.claims import record_observed_claim_decision
    picked = [d for d, on in (("ratified", ratify), ("rejected", reject), ("pinned", pin)) if on]
    if len(picked) != 1:
        typer.echo("choose exactly one of --ratify / --reject / --pin")
        raise typer.Exit(2)
    missing = [name for name, value in (
        ("--claim-uid", claim_uid), ("--evidence-digest", evidence_digest),
        ("--expected-revision", expected_revision), ("--action-id", action_id),
    ) if value is None or value == ""]
    if missing:
        typer.echo("error: required governance receipt option(s): " + ", ".join(missing))
        raise typer.Exit(2)
    import datetime as _dt
    try:
        rec = record_observed_claim_decision(
            str(memory_dir), statement=statement, claim_uid=claim_uid,
            evidence_digest=evidence_digest, decision=picked[0], note=note,
            scope=scope, metric=metric, expected_revision=expected_revision,
            action_id=action_id,
            at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        )
    except (GovernanceLedgerUnavailable, EventStoreLockError) as e:
        _governance_cli_error(e)
    except ValueError as e:
        typer.echo(f"error: {e}")
        raise typer.Exit(2)
    typer.echo(f"recorded: {rec['decision']} — {rec['statement'][:80]}")


@app.command(name="task-facets")
def task_facets_cmd(
    memory_dir: Path = typer.Argument(..., help="Cross-run memory dir (where task_facets.jsonl lives)."),
    goal: str = typer.Argument(..., help="The task goal to classify."),
    kind: str = typer.Option("", "--kind", help="Task kind (dataset/repo/...) — a hint for the classifier."),
    apply: bool = typer.Option(False, "--apply", help="DEPRECATED compatibility option; rejected before any "
                               "paid call — task-facets is PROPOSAL-ONLY."),
    model: Optional[str] = typer.Option(None, help="Override model id."),
    action_id: str = typer.Option(
        "", "--action-id", help="Required stable id for at-most-once paid-call recovery."),
):
    """PART IV cross-run §21.20.2 — AGENTIC task FACETING: an LLM PROPOSES a task's facets
    (domain/language/modality/interaction/objective) so the system can recognize when two differently-worded
    tasks are the same KIND of problem. An advisory OVERLAY (never touches the deterministic passport
    fingerprint). PROPOSAL-ONLY, consistent with concept-steward/claim-steward (§22.4 — the agentic steward
    only proposes; it never writes cross-run state): review the classification, then record it deterministically
    with `task-facets-set` (or let the engine record it at finalize under `cross_run_curation`). A stable
    action id durably fences its paid call across crash/retry."""
    from looplab.core.config import Settings
    from looplab.engine.governance_health import read_curation_rows
    from looplab.engine.task_facets import steward_task_facets, task_facets_input_digest
    if apply:
        typer.echo("error: --apply is deprecated and disabled; task-facets is proposal-only. Review the "
                   "classification, then record it with `task-facets-set MEMORY TASK_ID --domain ... "
                   "--language ...`.")
        raise typer.Exit(2)
    # CODEX AGENT: task faceting is the third paid steward. Its audit history gets the same pre-client
    # fail-closed boundary as concept/claim stewardship, even though this CLI remains proposal-only.
    _governance_cli_read(
        lambda: read_curation_rows(Path(memory_dir) / "task_facets_curation_log.jsonl"))
    settings = Settings()
    if model:
        settings.llm_model = model
    out = _run_cli_steward(
        memory_dir, "facets", action_id,
        prepare=lambda: _make_llm_client(settings),
        invoke=lambda client: {
            "proposals": {
                "task_id": "",
                "facets": steward_task_facets(
                    str(memory_dir), client, task_id="", goal=goal, kind=kind,
                    apply=False, raise_on_failure=True)["facets"],
            },
            "receipt": None,
        },
        request={
            "surface": "cli", "model": model or "",
            "input_digest": task_facets_input_digest(goal, kind),
        },
    )
    facets = out["proposals"].get("facets") or {}
    if not facets:
        typer.echo("task-facets: none classified")
        _echo_cli_invocation(out)
        return
    for ax, v in facets.items():
        typer.echo(f"  {ax:12} {v}")
    typer.echo("(proposal — record with `task-facets-set MEMORY TASK_ID --<axis> <value> ...`)")
    _echo_cli_invocation(out)


@app.command(name="task-facets-set")
def task_facets_set_cmd(
    memory_dir: Path = typer.Argument(..., help="Cross-run memory dir (where task_facets.jsonl lives)."),
    task_id: str = typer.Argument(..., help="Task id to record facets under."),
    domain: str = typer.Option("", "--domain"),
    language: str = typer.Option("", "--language"),
    modality: str = typer.Option("", "--modality"),
    interaction: str = typer.Option("", "--interaction"),
    objective: str = typer.Option("", "--objective"),
):
    """PART IV cross-run §21.20.2 / §22.4 — the OPERATOR facet write (deterministic, no LLM): record a task's
    facets by hand, the ratify half of the propose/ratify split (task-facets PROPOSES, this RECORDS).
    Append-only, last-write-wins per task_id; empty axes are dropped."""
    from looplab.engine.task_facets import record_task_facets
    import datetime as _dt
    facets = {"domain": domain, "language": language, "modality": modality,
              "interaction": interaction, "objective": objective}
    facets = {k: v for k, v in facets.items() if v}
    if not facets:
        typer.echo("error: give at least one facet axis (e.g. --domain information-retrieval)")
        raise typer.Exit(2)
    try:
        rec = record_task_facets(str(memory_dir), task_id=task_id, facets=facets, by="operator",
                                 at=_dt.datetime.now().isoformat(timespec="seconds"))
    except (GovernanceLedgerUnavailable, EventStoreLockError) as e:
        _governance_cli_error(e)
    except ValueError as e:
        typer.echo(f"error: {e}")
        raise typer.Exit(2)
    typer.echo(f"recorded facets for task '{task_id}': {rec['facets']}")


@app.command(name="claim-steward")
def claim_steward_cmd(
    memory_dir: Path = typer.Argument(..., help="Cross-run memory dir (holds lessons.jsonl)."),
    apply: bool = typer.Option(False, "--apply", help="DEPRECATED compatibility option; rejected before "
                               "any LLM call. The steward is proposal-only."),
    model: Optional[str] = typer.Option(None, help="Override model id."),
    max_proposals: int = typer.Option(10, help="Max decision proposals."),
    as_json: bool = typer.Option(False, "--json", help="Emit proposals + receipt as JSON."),
    action_id: str = typer.Option(
        "", "--action-id", help="Required stable id for at-most-once paid-call recovery."),
):
    """PART IV cross-run §22.4 — the AGENTIC CLAIM steward: an LLM reviews the evidence-grounded claims and
    PROPOSES operator decisions (ratify well-evidenced / reject contradicted-or-noise / pin load-bearing).
    Proposal-only: review the exact output, then record selected decisions through `claim-decide` or owner
    HTTP governance. The deprecated `--apply` option is rejected before any paid LLM call or mutation.
    Scope-precise via the structured claim key; a stable action id durably fences the paid call across
    crash/retry. Needs a reachable LLM."""
    if apply:
        typer.echo("error: --apply is deprecated and disabled; claim-steward is proposal-only. "
                   "Run without --apply, review the exact proposal, then use claim-decide or owner HTTP "
                   "governance.")
        raise typer.Exit(2)
    from looplab.core.config import Settings
    from looplab.engine.claim_steward import curation_is_empty, steward_claims
    from looplab.engine.claims import claim_governance_revision
    from looplab.engine.governance_health import read_curation_rows

    def _preflight():
        claim_governance_revision(str(memory_dir))
        # CODEX AGENT: fail before provider construction when the paid-call audit trail is unknown.
        read_curation_rows(Path(memory_dir) / "claim_curation_log.jsonl")

    _governance_cli_read(_preflight)
    settings = Settings()
    if model:
        settings.llm_model = model   # the field is `llm_model`; model_copy(update={"model":...}) wrote a
        #                              phantom attr and silently kept the default endpoint (--model no-op)
    out = _run_cli_steward(
        memory_dir, "claim", action_id,
        prepare=lambda: _make_llm_client(settings),
        invoke=lambda client: steward_claims(
            str(memory_dir), client, apply=False, max_proposals=max_proposals,
            raise_on_failure=True),
        request={
            "surface": "cli", "model": model or "", "max_proposals": max_proposals,
        },
    )
    if as_json:
        typer.echo(orjson.dumps(out, option=orjson.OPT_INDENT_2).decode())
        return
    prop = out["proposals"]
    if curation_is_empty(prop):
        typer.echo("claim-steward: no decisions proposed")
        _echo_cli_invocation(out)
        return
    typer.echo(f"claim-steward proposals — {len(prop['decisions'])} decision(s):")
    for d in prop["decisions"]:
        typer.echo(f"  {d['decision']:9} {d['statement'][:80]}" + (f"   ({d['why']})" if d.get("why") else ""))
    typer.echo("(proposal only — review the exact proposal above; apply selected decisions with "
               "claim-decide or owner HTTP governance)")
    _echo_cli_invocation(out)


@app.command(name="cross-run-digest")
def cross_run_digest_cmd(
    memory_dir: Path = typer.Argument(..., help="Cross-run memory dir (holds concept_capsules.jsonl)."),
    as_json: bool = typer.Option(False, "--json", help="Emit the full digest as JSON."),
):
    """PART IV cross-run Step 7 (§21.20.11, GATED): a recursive summary — concepts grouped by AXIS prefix
    into clusters with rollup counts. Deterministic inspector DATA; NOT wired into any prompt until it
    beats the flat baseline on the benchmark corpus (the hierarchy gate). Honors concept aliases. No LLM."""
    import stat

    from looplab.engine.governance_health import observed_path_missing, project_governed_sources
    from looplab.engine.memory import ConceptCapsuleStore, portfolio_digest
    base = Path(memory_dir)
    caps_p = base / "concept_capsules.jsonl"

    def _snapshot():
        try:
            if not stat.S_ISDIR(base.stat().st_mode):
                return None
        except FileNotFoundError:
            pass

        def _project(governance):
            caps = [] if observed_path_missing(caps_p) else ConceptCapsuleStore(caps_p).all()
            if (not caps
                    and getattr(caps, "source_health", {}).get("source_store_complete") is not False):
                return None
            # CODEX AGENT: digest labels and capsule rows are one governed observation, not adjacent reads.
            return portfolio_digest(
                caps, aliases=governance["aliases"], splits=governance["splits"])

        return project_governed_sources(
            base, _project, include_concepts=True,
            source_names=("concept_capsules.jsonl",),
        )

    dg = _governance_cli_read(_snapshot)
    if dg is None:
        typer.echo(f"no concept capsules at {memory_dir}")
        raise typer.Exit(1)
    if as_json:
        typer.echo(orjson.dumps(dg, option=orjson.OPT_INDENT_2).decode())
        return
    typer.echo(f"Cross-run digest: {dg['n_axes']} axis-cluster(s), {dg['n_concepts']} concept(s)")
    if dg.get("source_complete") is not True:
        typer.echo("  WARNING: capsule source is partial; digest describes returned observations only")
    if dg.get("axes_omitted") or dg.get("concepts_omitted"):
        typer.echo("  NOTE: bounded digest omitted "
                   f"{dg.get('axes_omitted', 0)} axis-cluster(s) and "
                   f"{dg.get('concepts_omitted', 0)} concept label(s); use --json for receipts")
    for a in dg["axes"]:
        typer.echo(f"  {a['n_concepts']:2d} concept(s) / {a['n_runs']:2d} run(s)  {a['axis']}/  "
                   f"[{', '.join(c.split('/', 1)[-1] for c in a['concepts'][:5])}]")


@app.command(name="cross-run-search")
def cross_run_search_cmd(
    memory_dir: Path = typer.Argument(..., help="Cross-run memory dir."),
    query: str = typer.Argument(..., help="Free-text query (idea / technique / question)."),
    k: int = typer.Option(8, help="How many results."),
    as_json: bool = typer.Option(False, "--json", help="Emit the full result + receipt as JSON."),
):
    """PART IV cross-run CR2a (§21.20.5): relevance-ranked hybrid SEARCH over the cross-run knowledge
    (claims + concepts) via the shipped lexical+BM25+vector RRF retriever, with a why-recalled receipt.
    Operator-rejected claims are excluded. Pure read; no endpoint."""
    from looplab.engine.claims import cross_run_retrieve
    r = _governance_cli_read(lambda: cross_run_retrieve(str(memory_dir), query, k=k))
    if as_json:
        typer.echo(orjson.dumps(r, option=orjson.OPT_INDENT_2).decode())
        return
    rc = r["receipt"]
    source_complete = rc.get("source_complete") is True
    claim_source = rc.get("claim_source") if isinstance(rc.get("claim_source"), dict) else {}
    trunc = f", {rc['truncated']} dropped" if rc.get("truncated") else ""
    typer.echo(f"cross-run search '{query}' — {rc['n_hits']}/{rc['n_corpus']}{trunc} "
               f"[intent={rc.get('intent', '?')}, {rc.get('n_caveats', 0)} caveat(s) reserved] "
               f"(channels: {', '.join(rc['channels'])})")
    if not source_complete:
        # CODEX AGENT: retrieval counts only the concepts that survived each bounded/legacy capsule.  Keep
        # both positive frequencies and an empty match explicitly lower-bound instead of implying absence.
        typer.echo("  WARNING: concept capsule source is partial; concept matches and run counts describe "
                   "retained records only: "
                   f"{rc.get('source_concepts_omitted', 0)} concept(s) known omitted"
                   + (f"; {rc.get('source_unknown_capsules', 0)} legacy capsule(s) have unknown totals"
                      if rc.get("source_unknown_capsules", 0) else "")
                   + (f"; {rc.get('source_rows_quarantined', 0)} durable row(s) quarantined"
                      if rc.get("source_rows_quarantined", 0) else ""))
    if claim_source.get("source_complete") is not True:
        lesson_bad = ((claim_source.get("lessons") or {}).get("rows_quarantined", 0))
        research_bad = ((claim_source.get("research") or {}).get("rows_quarantined", 0))
        typer.echo(
            "  WARNING: claim evidence source is partial; retained claim matches/counts are lower bounds "
            "and an empty match is not proof of absence: "
            f"lessons quarantined={int(lesson_bad or 0)}; "
            f"research quarantined={int(research_bad or 0)}"
        )
    for h in r["results"]:
        if h["kind"] == "claim":
            typer.echo(f"  claim [{h['epistemic']} {h['n_support']}↑/{h['n_oppose']}↓] {h['text'][:100]}")
        else:
            count = f"×{h['n_runs']}" if source_complete else f"retained in at least {h['n_runs']} run(s)"
            typer.echo(f"  concept {count}  {h['text']}")


@app.command(name="atlas")
def atlas_cmd(
    memory_dir: Path = typer.Argument(..., help="Cross-run memory dir (holds lessons.jsonl + "
                                                "concept_capsules.jsonl)."),
    max_items: int = typer.Option(8, help="Cap per section (explored/contested/thin)."),
    as_json: bool = typer.Option(False, "--json", help="Emit the full Atlas payload as JSON."),
):
    """PART IV cross-run Step 6 (§21.20): the legacy Research Atlas DATA payload — bounded concept
    observations, concepts observed in one returned run, and mixed-evidence claim records. It composes
    the concept overview (Step 3), claim assessments (Step 4), and bounded context pack (Step 5).
    Pure read; the owner React preview is available at ``#/atlas``.

    Wire keys such as ``thin_coverage`` and ``contradictions`` are retained for compatibility; they
    do not establish a CoverageFrame, an untried gap, or a proposition-level contradiction verdict.
    """
    from looplab.engine.claims import atlas_for_memory
    base = Path(memory_dir)
    # CODEX AGENT: leaving every source argument unset delegates loading to atlas_for_memory's single
    # policy+evidence transaction. Preloading here defeated that boundary and created hybrid Atlases.
    atlas = _governance_cli_read(lambda: atlas_for_memory(base, max_items=max_items))
    claim_source = atlas.get("claim_source") if isinstance(atlas.get("claim_source"), dict) else {}
    concept_source_for_empty = (
        atlas.get("concept_source") if isinstance(atlas.get("concept_source"), dict) else {})
    lesson_rows = ((claim_source.get("lessons") or {}).get("rows_total", 0))
    research_rows = ((claim_source.get("research") or {}).get("rows_total", 0))
    capsule_rows = concept_source_for_empty.get("source_rows_total", 0)
    if (lesson_rows == research_rows == capsule_rows == 0
            and claim_source.get("source_complete") is True
            and concept_source_for_empty.get("source_complete") is True):
        typer.echo(f"no cross-run memory at {base} (need lessons, capsules, and/or research claims)")
        raise typer.Exit(1)
    if as_json:
        typer.echo(orjson.dumps(atlas, option=orjson.OPT_INDENT_2).decode())
        return
    typer.echo(f"Research Atlas: {atlas['n_runs']} run(s), {atlas['n_concepts']} concept(s), "
               f"{atlas['n_claims']} claim record(s), {atlas['n_contested']} mixed-evidence")
    concept_source = atlas.get("concept_source")
    if not isinstance(concept_source, dict):
        context_pack = atlas.get("context_pack") if isinstance(atlas.get("context_pack"), dict) else {}
        concept_source = (context_pack.get("coverage")
                          if isinstance(context_pack.get("coverage"), dict) else {})
    if concept_source.get("source_complete") is not True:
        # CODEX AGENT: legacy/bounded capsule rows make Atlas concept counts lower bounds. The human CLI
        # must carry the same receipt as JSON/UI/agent consumers instead of printing retained rows as exact.
        unknown = int(concept_source.get("source_unknown_capsules", 0) or 0)
        typer.echo(
            "WARNING: concept capsule source is PARTIAL; Atlas concept observations/counts are retained "
            "lower bounds only ("
            f"{int(concept_source.get('source_concepts_omitted', 0) or 0)} concept(s), "
            f"{int(concept_source.get('source_outcomes_omitted', 0) or 0)} outcome(s) known omitted"
            + (f"; {unknown} legacy capsule(s) have unknown totals" if unknown else "")
            + (f"; {int(concept_source.get('source_rows_quarantined', 0) or 0)} durable row(s) quarantined"
               if concept_source.get("source_rows_quarantined", 0) else "")
            + ").")
    if claim_source.get("source_complete") is not True:
        lesson_bad = ((claim_source.get("lessons") or {}).get("rows_quarantined", 0))
        research_bad = ((claim_source.get("research") or {}).get("rows_quarantined", 0))
        typer.echo(
            "WARNING: claim evidence source is PARTIAL; retained claims/counts are lower bounds and "
            "absence is not exact "
            f"(lessons quarantined={int(lesson_bad or 0)}; "
            f"research quarantined={int(research_bad or 0)})."
        )
    if atlas["explored"]:
        typer.echo("Concept observations (concept × returned runs):")
        for e in atlas["explored"]:
            typer.echo(f"  {e['n_runs']:2d}×  {e['concept']}")
    if atlas["thin_coverage"]:
        typer.echo("Observed in one returned run (not an untried-gap claim): "
                   + ", ".join(atlas["thin_coverage"]))
    if atlas["contradictions"]:
        typer.echo("Mixed-evidence claim records (support and opposition references):")
        for c in atlas["contradictions"]:
            typer.echo(f"  ⚖ [{c['n_support']}↑/{c['n_oppose']}↓] {c['statement'][:100]}")
    projection_omitted = (
        int(atlas.get("explored_omitted", 0) or 0),
        int(atlas.get("thin_coverage_omitted", 0) or 0),
        int(atlas.get("contradictions_omitted", 0) or 0),
    )
    if any(projection_omitted):
        typer.echo("Bounded projection omitted: "
                   f"{projection_omitted[0]} concept observation(s), "
                   f"{projection_omitted[1]} single-run observation(s), "
                   f"{projection_omitted[2]} mixed-evidence record(s).")


@app.command(name="claims")
def claims_cmd(
    memory_dir: Path = typer.Argument(..., help="Cross-run memory dir (holds lessons.jsonl), or the "
                                                "lessons file itself."),
    top: int = typer.Option(20, help="How many most-evidenced claims to show."),
    contested_only: bool = typer.Option(False, "--contested", help="Show only MIXED (support+oppose) claims."),
    pack: bool = typer.Option(False, "--pack", help="Render the bounded agent context pack (Step 5) instead."),
    fuzzy: bool = typer.Option(False, "--fuzzy", help="Merge paraphrased claims (CR1b, suggestion-grade)."),
    structured: bool = typer.Option(False, "--structured", help="Use the scope+polarity-safe structured "
                                    "claim key (§21.20.13): opposite-polarity claims contradict, not merge."),
    as_json: bool = typer.Option(False, "--json", help="Emit the full assessments as JSON."),
    governance_receipt: bool = typer.Option(
        False, "--governance-receipt",
        help="With --json, wrap claims with the exact claim-governance revision for claim-decide."),
):
    """PART IV cross-run Step 4/5 (§21.20): project distilled lessons into evidence-labelled claim
    records with support and opposition attempt references. Legacy wire states ``supported`` and
    ``refuted`` mean support-only and opposition-only evidence here, not proposition verdicts;
    ``mixed`` means both kinds of reference, and ``inconclusive`` means insufficient evidence.
    ``--pack`` renders the hard-capped agent context pack (pinned → ratified → mixed → support-only
    → opposition-only → insufficient; a caveat may replace the weakest non-pinned positive). Pure read of
    `<memory_dir>/lessons.jsonl` (unifies with the D8 claim shape); no LLM/endpoint."""
    from looplab.engine.claims import (
        _load_claim_source_path, _safe_claim_source_summary,
        _safe_research_source_summary, build_context_pack, claims_for_memory,
        load_research_claims, render_context_pack,
    )
    from looplab.engine.governance_health import observed_path_missing, project_governed_sources
    from looplab.engine.memory import ConceptCapsuleStore, _portfolio_concept_overview_data
    import stat

    p = Path(memory_dir)

    def _snapshot():
        try:
            mode = p.stat().st_mode
        except FileNotFoundError:
            return None
        if stat.S_ISREG(mode):
            path, base = p, p.parent
        elif stat.S_ISDIR(mode):
            path, base = p / "lessons.jsonl", p
        else:
            return None

        canonical_lessons = path.absolute() == (base / "lessons.jsonl").absolute()
        source_names = ["research_claims.jsonl"]
        source_paths = []
        if canonical_lessons:
            source_names.append("lessons.jsonl")
        else:
            source_paths.append(path.absolute())
        if pack:
            source_names.append("concept_capsules.jsonl")

        def _project(governance):
            lessons = _load_claim_source_path(path, research=False)
            research = load_research_claims(base)
            claims = claims_for_memory(
                base, lessons=lessons, research_claims=research,
                decisions=governance["decisions"], fuzzy=fuzzy, structured=structured)
            research_source = _safe_research_source_summary(
                getattr(claims, "research_source", None)) or {}
            claim_source = _safe_claim_source_summary(
                getattr(claims, "claim_source", None)) or {}
            context_pack = None
            if pack:
                caps_path = base / "concept_capsules.jsonl"
                if observed_path_missing(caps_path):
                    overview, concept_rows = None, None
                else:
                    overview, concept_rows = _portfolio_concept_overview_data(
                        ConceptCapsuleStore(caps_path).all(), aliases=governance["aliases"],
                        splits=governance["splits"])
                # CODEX AGENT: build the pack before releasing any policy/source lock. Its claims,
                # taxonomy, source receipts and decisions must all describe the same durable era.
                context_pack = build_context_pack(
                    claims, concept_overview=overview, max_claims=top,
                    _concept_rows=concept_rows, _research_source=research_source)
            return {
                "path": path, "lessons": lessons, "research": research, "claims": claims,
                "research_source": research_source, "claim_source": claim_source,
                "context_pack": context_pack,
                "claim_revision": governance["claim_revision"],
            }

        return project_governed_sources(
            base, _project, include_concepts=pack,
            source_names=source_names, source_paths=source_paths,
        )

    snapshot = _governance_cli_read(_snapshot)
    if snapshot is None:
        # Reject before selecting a parent directory; otherwise an explicit missing file
        # silently reads an unrelated sibling research_claims.jsonl and reports success for the wrong input.
        typer.echo(f"cross-run memory path does not exist or is not a file/directory: {p}")
        raise typer.Exit(1)
    path = snapshot["path"]
    lessons, research, claims = snapshot["lessons"], snapshot["research"], snapshot["claims"]
    research_source, claim_source = snapshot["research_source"], snapshot["claim_source"]
    if (not lessons and not research and claim_source.get("source_complete") is True):
        typer.echo(f"no lessons at {path}")
        raise typer.Exit(1)
    if pack:
        cp = snapshot["context_pack"]
        typer.echo(orjson.dumps(cp, option=orjson.OPT_INDENT_2).decode() if as_json
                   else (render_context_pack(cp) or "(empty context pack)"))
        return
    if contested_only:
        claims = [c for c in claims if c["epistemic"] == "mixed"]
    if as_json:
        payload = ({
            "claims": claims,
            "revision": snapshot["claim_revision"],
            "structured": structured,
        } if governance_receipt else claims)
        typer.echo(orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode())
        return
    if research_source.get("source_complete") is not True:
        typer.echo(
            "WARNING: D8 research-claim source is partial/unknown; retained evidence is a lower bound and "
            "exact one-sided states are withheld."
        )
    if claim_source.get("source_complete") is not True:
        lesson_bad = ((claim_source.get("lessons") or {}).get("rows_quarantined", 0))
        research_bad = ((claim_source.get("research") or {}).get("rows_quarantined", 0))
        typer.echo(
            "WARNING: claim evidence stores are partial; retained claims/counts are lower bounds and "
            "absence is not exact "
            f"(lessons quarantined={int(lesson_bad or 0)}; "
            f"research quarantined={int(research_bad or 0)})."
        )
    _mark = {"supported": "✓", "refuted": "✗", "mixed": "⚖", "inconclusive": "·"}
    _mat = {"operator-ratified": "RATIFIED", "operator-rejected": "REJECTED",
            "operator-pinned": "PINNED"}

    def _maturity_label(claim) -> str:
        label = _mat.get(claim.get("maturity"))
        if not label:
            return ""
        freshness = {True: "CURRENT", False: "STALE EVIDENCE", None: "FRESHNESS UNKNOWN"}.get(
            claim.get("decision_fresh"), "FRESHNESS UNKNOWN")
        return f" [{label}] [{freshness}]"
    typer.echo(f"Claim records ({len(claims)} shown{' — mixed-evidence only' if contested_only else ''}): "
               "✓ support-only  ✗ opposition-only  ⚖ mixed evidence  · insufficient evidence")
    for c in claims[: max(0, top)]:
        typer.echo(f"  {_mark.get(c['epistemic'], '?')}{_maturity_label(c)} "
                   f"[{c['n_support']}↑/{c['n_oppose']}↓] {c['statement'][:100]}")
