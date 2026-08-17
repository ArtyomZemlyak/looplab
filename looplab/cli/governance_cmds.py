"""Cross-run governance and portfolio commands — the half of the old `inspect_cmds` that WRITES.

Split out of `inspect_cmds.py` (doc 25 CT-01): these commands shared nothing with the read-only
run diagnostics except the typer app, and living under a "read-only inspection" docstring hid what
they actually do. Stated plainly here instead:

* DURABLE WRITES to the cross-run memory dir — `concept-merge`, `concept-split`, `claim-decide`,
  `task-facets-set`. Each goes through `_governed_write`, which fails closed on an unavailable
  ledger rather than leaving a half-recorded decision.
* PAID LLM STEWARDS — `concept-steward`, `claim-steward`, `task-facets`. Each spends money, so each
  is fenced at-most-once by a required stable `--action-id` through `_run_cli_steward`.
* READ-ONLY portfolio views over the same sources — `cross-run-concepts`, `cross-run-index`,
  `cross-run-digest`, `cross-run-search`, `atlas`, `claims`. These fail closed through
  `_governance_cli_read`: an unreadable source is UNKNOWN, never an empty portfolio.
"""
from __future__ import annotations

from contextlib import contextmanager

from pathlib import Path
from typing import Optional

import orjson
import typer

from looplab.engine.governance_health import GovernanceLedgerUnavailable
from looplab.events.eventstore import EventStoreLockError
from looplab.cli import _make_llm_client, app
from looplab.core.llm import apply_llm_model_override


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
        # an unreadable source is unknown, never an empty portfolio. Keep the OS path and
        # platform parser text out of CLI output while preserving argument/validation ValueErrors.
        _governance_cli_error(
            GovernanceLedgerUnavailable("cross_run_sources", "storage_unreadable"))


def resolve_memory_source(memory_dir, canonical_name: str, *, missing_is_dir: bool):
    """Resolve a `--memory-dir` argument that may name the DIRECTORY or the FILE itself.

    Returns ``(path, base, source_names, source_paths)`` for `project_governed_sources`, or None when
    the argument is neither a regular file nor a directory (doc 25 CT-05). Three read commands wrote
    this dance out — `cross-run-concepts`, `cross-run-digest` and `claims` — and it is not cosmetic:
    the file-vs-directory split decides which governed SOURCE the read declares, and declaring the
    wrong one silently changes what the governance snapshot locks and reports as complete.

    `canonical` is the interesting bit. When the resolved path IS `base / canonical_name`, the source
    is declared by NAME so the governance layer applies its own health/quarantine bookkeeping to a
    store it knows. When the operator pointed at some other file, it is declared by PATH instead —
    the same locking, but no claim that this is the canonical store.

    `missing_is_dir` is the one behaviour that legitimately differs. `cross-run-concepts` treats a
    non-existent argument as a directory so it can print "no concept capsules at <path>" naming the
    file the operator expected; `claims` refuses instead. Both are right for their command, so the
    caller says which.
    """
    import stat as _stat

    base_path = Path(memory_dir)
    try:
        mode = base_path.stat().st_mode
    except FileNotFoundError:
        if not missing_is_dir:
            return None
        path, base = base_path / canonical_name, base_path
    else:
        if _stat.S_ISREG(mode):
            path, base = base_path, base_path.parent
        elif _stat.S_ISDIR(mode):
            path, base = base_path / canonical_name, base_path
        else:
            return None

    canonical = path.absolute() == (base / canonical_name).absolute()
    return (path, base,
            [canonical_name] if canonical else [],
            [] if canonical else [path.absolute()])


def quarantined_claim_counts(claim_source) -> tuple[int, int]:
    """`(lessons, research)` durable rows quarantined out of one claim-source receipt.

    Three commands render this pair in their own prose; only the extraction was duplicated, and it is
    the half that must not drift — a receipt read with the wrong nesting reports 0 quarantined rows
    and turns "these counts are lower bounds" into a confident exact answer.
    """
    source = claim_source if isinstance(claim_source, dict) else {}
    lessons = ((source.get("lessons") or {}).get("rows_quarantined", 0))
    research = ((source.get("research") or {}).get("rows_quarantined", 0))
    return int(lessons or 0), int(research or 0)


def _safe_steward_error(exc: Exception, phase: str) -> str:
    """Classify a paid failure without persisting provider text, endpoints, paths, or credentials."""
    from looplab.serve.assistant import safe_assistant_failure

    return f"{phase}:{safe_assistant_failure(exc)['error_kind']}"


@contextmanager
def _governed_write():
    """The refusal contract shared by the four DETERMINISTIC governance writes (doc 25 CT-04).

    `concept-merge`, `concept-split`, `claim-decide` and `task-facets-set` each wrote this same
    four-line block. The two arms are different KINDS of failure and must stay different:

    * a ledger/lock failure goes through `_governance_cli_error`, which withholds the OS path and the
      platform's parser text — an unreadable governance store must not leak filesystem shape into CLI
      output;
    * a `ValueError` is the caller's own argument being wrong (a self-link, a cycle-closing edge, an
      unknown axis) and SHOULD be echoed verbatim, because that text is the whole diagnosis.

    Collapsing the two, in either direction, either hides a real usage error behind a generic
    governance refusal or prints a storage path the redaction boundary exists to keep out.
    """
    try:
        yield
    except (GovernanceLedgerUnavailable, EventStoreLockError) as exc:
        _governance_cli_error(exc)
    except ValueError as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(2)


def _steward_command(memory_dir: Path, kind: str, action_id: str, *, apply: bool,
                     apply_refusal: str, model, preflight, invoke, request: dict) -> dict:
    """The framing every PAID steward command shares, in the order that ordering matters (CT-04).

    `concept-steward`, `task-facets` and `claim-steward` each wrote out these four steps. The
    SEQUENCE is the point, and each step is a fail-closed boundary that has to come before the next:

    1. **Reject the deprecated `--apply` first**, before anything costs money. These commands are
       proposal-only; an operator who passes it is asking for a mutation that will not happen, and
       must learn that without being billed for a proposal they did not want.
    2. **Preflight the governance/audit history**, still before a provider client exists — a paid
       proposal must not run against an invocation log that cannot be read, or corruption becomes a
       way to spend money.
    3. **Resolve the model override** onto a fresh `Settings` via `apply_llm_model_override` rather
       than `settings.llm_model = model`: assignment validation is disabled on `Settings`, so a
       direct write lands a phantom attribute and the override silently does nothing.
    4. **Run the durable at-most-once transaction**, which owns crash/retry fencing.

    The refusal wording, the preflight body, the invocation and the rendering stay at each call site:
    they are what actually differs, and the refusal names the command to use instead.
    """
    if apply:
        typer.echo(apply_refusal)
        raise typer.Exit(2)
    if preflight is not None:
        _governance_cli_read(preflight)
    from looplab.core.config import Settings

    settings = Settings()
    if model:
        apply_llm_model_override(settings, model)
    return _run_cli_steward(
        memory_dir, kind, action_id,
        prepare=lambda: _make_llm_client(settings), invoke=invoke, request=request)


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
        # this is not a retryable provider error. The old process may have paid already;
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

    from looplab.engine.governance_health import observed_path_missing, project_governed_sources
    from looplab.engine.memory import ConceptCapsuleStore, portfolio_concept_overview
    p = Path(memory_dir)

    def _snapshot():
        # A missing argument resolves AS a directory here so the refusal below can name the capsule
        # file the operator expected, rather than echoing back the bare directory they typed.
        resolved = resolve_memory_source(p, "concept_capsules.jsonl", missing_is_dir=True)
        if resolved is None:
            return p, None
        path, base, source_names, source_paths = resolved

        def _project(governance):
            if observed_path_missing(path):
                return None
            # read capsules and apply taxonomy while both policy and evidence locks are held;
            # separate reads can otherwise render a merge against a capsule generation that never coexisted.
            return portfolio_concept_overview(
                ConceptCapsuleStore(path).all(), aliases=governance["aliases"],
                splits=governance["splits"])

        overview = project_governed_sources(
            base, _project, include_concepts=True,
            source_names=tuple(source_names), source_paths=tuple(source_paths),
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
    # capsule-source completeness and this read-model's display cap are independent. The
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
        # load -> build -> save is one interprocess read-modify-write. `save_index` writes atomically,
        # which prevents a TORN file but not a LOST UPDATE: two concurrent indexers both read the old
        # cache and the slower one's replace discards the faster one's work, so the next run re-folds
        # what was already indexed. The cache is explicitly never a source of truth, so the cost is
        # wasted folds and stale receipts rather than wrong answers — a plain interprocess lock around
        # the whole transaction is the proportionate fix (it degrades to a no-op where locking is
        # unavailable, leaving today's behavior).
        from looplab.events.eventstore import _interprocess_lock
        with _interprocess_lock(run_root / ".cross_run_index.lock"):
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
    with _governed_write():
        rec = record_concept_alias(str(memory_dir), from_concept=from_concept, to_concept=to_concept,
                                   at=_dt.datetime.now().isoformat(timespec="seconds"))
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
    with _governed_write():
        rec = record_concept_split(str(memory_dir), from_concept=from_concept, rules=rules, default=default,
                                   at=_dt.datetime.now().isoformat(timespec="seconds"))
    tgts = [r["to"] for r in rec["rules"]] + ([rec["default"]] if rec["default"] else [])
    typer.echo(f"split: '{rec['from']}' -> {{{', '.join(sorted(set(tgts)))}}} ({len(rec['rules'])} rule(s))")


@app.command(name="concept-ratify")
def concept_ratify_cmd(
    memory_dir: Path = typer.Argument(..., help="Cross-run memory dir (holds concept_curation_log.jsonl)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what WOULD be applied; write nothing."),
    # Clamped to the module's own cap below rather than defaulted from it: a Typer default is
    # evaluated at import time, and importing the engine stage there would drag the governance
    # ledgers into every `looplab --help`.
    limit: int = typer.Option(32, help="Max merges to apply in this pass (capped by the stage)."),
    as_json: bool = typer.Option(False, "--json", help="Emit the full result as JSON."),
):
    """PART IV cross-run §22.4 — RATIFY the agentic steward's already-recorded MERGE proposals.

    The steward (`concept-steward`) only proposes, and its proposals are durably logged. This is the
    consumer: it applies every still-valid proposed merge through the SAME append-only, read-time,
    reversible `record_concept_alias` this CLI's `concept-merge` uses — with the proposal's semantic
    payload as the at-most-once `action_id` and a per-decision governance CAS. It costs no inference
    (the judgement was bought at finalize) and never applies a SPLIT or a PURGE, which stay operator
    work. Undo one decision with `concept-alias-clear`; a cleared decision is never re-applied.

    This is also what `Settings.concept_tidy` runs unattended at finalize — one code path, so a dry
    run here previews exactly what the background stage would do."""
    from looplab.engine.concept_tidy import MAX_RATIFICATIONS_PER_PASS as _cap, ratify_concept_merges
    import datetime as _dt

    with _governed_write():
        out = ratify_concept_merges(
            str(memory_dir), at=_dt.datetime.now().isoformat(timespec="seconds"),
            limit=min(int(limit), _cap), dry_run=dry_run)
    if as_json:
        typer.echo(orjson.dumps(out, option=orjson.OPT_INDENT_2).decode())
        return
    verb = "would apply" if dry_run else "applied"
    typer.echo(f"concept ratification: {verb} {len(out['applied'])} merge(s), "
               f"{len(out['skipped'])} not applied")
    for entry in out["applied"]:
        typer.echo(f"  {verb}  '{entry['from']}' -> '{entry['to']}'"
                   + (f"   ({entry['why']})" if entry.get("why") else ""))
    for entry in out["skipped"]:
        typer.echo(f"  skip   '{entry['from']}' -> '{entry['to']}'   [{entry['outcome']}]")
    pending = out["pending"]
    if pending["splits"] or pending["purges"]:
        typer.echo(f"pending operator work: {pending['splits']} proposed split(s), "
                   f"{pending['purges']} proposed purge(s) — this stage never applies either; use "
                   "concept-split / concept-purge after reviewing them.")


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
    from looplab.engine.concept_steward import curation_is_empty, steward_concepts
    from looplab.engine.concept_registry import concept_governance_snapshot
    from looplab.engine.governance_health import read_curation_rows

    def _preflight():
        concept_governance_snapshot(str(memory_dir))
        # a paid proposal is not allowed to run against an unreadable invocation history.
        # Validate that history before even constructing a provider client so corruption cannot spend money.
        read_curation_rows(Path(memory_dir) / "concept_curation_log.jsonl")

    out = _steward_command(
        memory_dir, "concept", action_id, apply=apply, model=model, preflight=_preflight,
        apply_refusal=("error: --apply is deprecated and disabled; concept-steward is proposal-only. "
                       "Run without --apply, review the exact proposal, then use "
                       "concept-merge/concept-split or owner HTTP governance."),
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
    from looplab.engine.claims import ClaimTargetConflict, record_observed_claim_decision
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
    with _governed_write():
        try:
            rec = record_observed_claim_decision(
                str(memory_dir), statement=statement, claim_uid=claim_uid,
                evidence_digest=evidence_digest, decision=picked[0], note=note,
                scope=scope, metric=metric, expected_revision=expected_revision,
                action_id=action_id,
                at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            )
        except ClaimTargetConflict as exc:
            # The refusal is CORRECT and stays a refusal; only the diagnosis was missing. The digest is
            # validated against the `--scope`d projection, so an operator who reviewed with a DIFFERENT
            # `--scope` (in particular the portfolio-wide default, which is what `claims` could only
            # ever produce before it grew `--scope`) sees "evidence changed" when nothing changed. Name
            # the projection so the two causes are distinguishable without reading the engine.
            if exc.code != "claim_evidence_changed":
                raise
            typer.echo(f"error: {exc.code}")
            typer.echo(
                f"the supplied --evidence-digest does not match this claim in the --scope "
                f"{scope!r} projection (its current digest is "
                f"{str(exc.detail.get('current_evidence_digest') or '(claim not projected)')!r}). "
                "A decision is validated against the SAME scoped projection it must be reviewed in: "
                f"re-review with `looplab claims MEMORY_DIR --scope {scope!r} --structured --json "
                "--governance-receipt` and use that receipt's digest. If you already reviewed at this "
                "scope, the lesson/research evidence genuinely changed since review — re-review it "
                "before deciding.")
            raise typer.Exit(2) from exc
    typer.echo(f"recorded: {rec['decision']} — {rec['statement'][:80]}")


@app.command(name="task-facets")
def task_facets_cmd(
    memory_dir: Path = typer.Argument(
        ..., help="Cross-run memory dir; writes paid-call audit to task_facets_curation_log.jsonl."),
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
    fingerprint). PROPOSAL-ONLY, consistent with concept-steward/claim-steward (§22.4): it never changes
    `task_facets.jsonl` or governance by itself. It does durably record the begun/outcome/proposal audit in
    `task_facets_curation_log.jsonl`; review the classification, then record it deterministically with
    `task-facets-set`. Finalize may queue another proposal but cannot ratify it. A stable action id durably
    fences this paid call across crash/retry."""
    from looplab.engine.governance_health import read_curation_rows
    from looplab.engine.task_facets import steward_task_facets, task_facets_input_digest

    out = _steward_command(
        memory_dir, "facets", action_id, apply=apply, model=model,
        apply_refusal=("error: --apply is deprecated and disabled; task-facets is proposal-only. "
                       "Review the classification, then record it with `task-facets-set MEMORY "
                       "TASK_ID --domain ... --language ...`."),
        # task faceting is the third paid steward. Its audit history gets the same pre-client
        # fail-closed boundary as concept/claim stewardship, even though this CLI is proposal-only.
        preflight=lambda: read_curation_rows(
            Path(memory_dir) / "task_facets_curation_log.jsonl"),
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
    with _governed_write():
        rec = record_task_facets(str(memory_dir), task_id=task_id, facets=facets, by="operator",
                                 at=_dt.datetime.now().isoformat(timespec="seconds"))
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
    from looplab.engine.claim_steward import curation_is_empty, steward_claims
    from looplab.engine.claims import claim_governance_revision
    from looplab.engine.governance_health import read_curation_rows

    def _preflight():
        claim_governance_revision(str(memory_dir))
        # fail before provider construction when the paid-call audit trail is unknown.
        read_curation_rows(Path(memory_dir) / "claim_curation_log.jsonl")

    out = _steward_command(
        memory_dir, "claim", action_id, apply=apply, model=model, preflight=_preflight,
        apply_refusal=("error: --apply is deprecated and disabled; claim-steward is proposal-only. "
                       "Run without --apply, review the exact proposal, then use claim-decide or "
                       "owner HTTP governance."),
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

    from looplab.engine.governance_health import observed_path_missing, project_governed_sources
    from looplab.engine.memory import ConceptCapsuleStore, portfolio_digest
    base = Path(memory_dir)
    caps_p = base / "concept_capsules.jsonl"

    def _snapshot():
        # Digest is DIRECTORY-only: it reads the whole governed memory dir, not one named file, so a
        # regular-file argument is a mistake rather than a narrower read.
        resolved = resolve_memory_source(base, "concept_capsules.jsonl", missing_is_dir=True)
        if resolved is None or resolved[1] != base:
            return None

        def _project(governance):
            caps = [] if observed_path_missing(caps_p) else ConceptCapsuleStore(caps_p).all()
            if (not caps
                    and getattr(caps, "source_health", {}).get("source_store_complete") is not False):
                return None
            # digest labels and capsule rows are one governed observation, not adjacent reads.
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
    k: int = typer.Option(8, min=1, max=64, help="How many results (hard range: 1-64)."),
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
        # retrieval counts only the concepts that survived each bounded/legacy capsule.  Keep
        # both positive frequencies and an empty match explicitly lower-bound instead of implying absence.
        typer.echo("  WARNING: concept capsule source is partial; concept matches and run counts describe "
                   "retained records only: "
                   f"{rc.get('source_concepts_omitted', 0)} concept(s) known omitted"
                   + (f"; {rc.get('source_unknown_capsules', 0)} legacy capsule(s) have unknown totals"
                      if rc.get("source_unknown_capsules", 0) else "")
                   + (f"; {rc.get('source_rows_quarantined', 0)} durable row(s) quarantined"
                      if rc.get("source_rows_quarantined", 0) else ""))
    if claim_source.get("source_complete") is not True:
        lesson_bad, research_bad = quarantined_claim_counts(claim_source)
        typer.echo(
            "  WARNING: claim evidence source is partial; retained claim matches/counts are lower bounds "
            "and an empty match is not proof of absence: "
            f"lessons quarantined={lesson_bad}; "
            f"research quarantined={research_bad}"
        )
    for h in r["results"]:
        if h["kind"] == "claim":
            typer.echo(f"  claim [{h['epistemic']} {h['n_support']}↑/{h['n_oppose']}↓] {h['text'][:100]}")
        else:
            count = f"×{h['n_runs']}" if source_complete else f"retained in at least {h['n_runs']} run(s)"
            typer.echo(f"  concept {count}  {h['text']}")


@app.command(name="atlas")
def atlas_cmd(
    memory_dir: Path = typer.Argument(
        ...,
        help="Cross-run memory dir (holds lessons.jsonl and/or research_claims.jsonl; "
             "concept_capsules.jsonl supplies concept coverage).",
    ),
    max_items: int = typer.Option(8, help="Cap per section (explored/contested/thin)."),
    as_json: bool = typer.Option(False, "--json", help="Emit the full Atlas payload as JSON."),
):
    """PART IV cross-run Step 6 (§21.20): the legacy Research Atlas DATA payload — bounded concept
    observations, concepts observed in one returned run, and mixed-evidence claim records. It composes
    the concept overview (Step 3), claim assessments (Step 4), and bounded context pack (Step 5).
    Pure read; the owner React preview is available at ``#/claims``.

    Wire keys such as ``thin_coverage`` and ``contradictions`` are retained for compatibility; they
    do not establish a CoverageFrame, an untried gap, or a proposition-level contradiction verdict.
    """
    from looplab.engine.claims import atlas_for_memory
    base = Path(memory_dir)
    # leaving every source argument unset delegates loading to atlas_for_memory's single
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
        # legacy/bounded capsule rows make Atlas concept counts lower bounds. The human CLI
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
        lesson_bad, research_bad = quarantined_claim_counts(claim_source)
        typer.echo(
            "WARNING: claim evidence source is PARTIAL; retained claims/counts are lower bounds and "
            "absence is not exact "
            f"(lessons quarantined={lesson_bad}; "
            f"research quarantined={research_bad})."
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
    memory_dir: Path = typer.Argument(
        ...,
        help="Cross-run memory dir (lessons.jsonl, research_claims.jsonl, and governance logs), "
             "or a lessons file itself.",
    ),
    top: int = typer.Option(20, help="How many most-evidenced claims to show."),
    contested_only: bool = typer.Option(False, "--contested", help="Show only MIXED (support+oppose) claims."),
    pack: bool = typer.Option(False, "--pack", help="Render the bounded agent context pack (Step 5) instead."),
    fuzzy: bool = typer.Option(False, "--fuzzy", help="Merge paraphrased claims (CR1b, suggestion-grade)."),
    structured: bool = typer.Option(False, "--structured", help="Use the scope+polarity-safe structured "
                                    "claim key (§21.20.13): opposite-polarity claims contradict, not merge."),
    scope: str = typer.Option(
        "", "--scope", help="Project only this task's evidence (the CLI spelling of the HTTP "
        "`/api/cross-run/claims?scope_task=` read). REQUIRED to obtain a usable --governance-receipt "
        "for a task-scoped claim: `claim-decide --scope T` validates against the SAME scoped "
        "projection, so a portfolio-wide receipt can never match it. Empty = portfolio-wide."),
    as_json: bool = typer.Option(False, "--json", help="Emit the full assessments as JSON."),
    governance_receipt: bool = typer.Option(
        False, "--governance-receipt",
        help="With --json, wrap claims with the exact claim-governance revision and reviewed scope "
             "for claim-decide."),
):
    """PART IV cross-run Step 4/5 (§21.20): project distilled lessons into evidence-labelled claim
    records with support and opposition attempt references. Legacy wire states ``supported`` and
    ``refuted`` mean support-only and opposition-only evidence here, not proposition verdicts;
    ``mixed`` means both kinds of reference, and ``inconclusive`` means insufficient evidence.
    ``--pack`` renders the hard-capped agent context pack (pinned → ratified → mixed → support-only
    → opposition-only → insufficient; a caveat may replace the weakest non-pinned positive).
    Reads lessons plus D8 research claims and governance decisions; ``--pack`` additionally joins
    concept capsules, aliases, and splits. ``--scope`` narrows every joined source to one task, which is
    what makes the emitted ``evidence_digest`` usable by ``claim-decide --scope``. No LLM/endpoint."""
    from looplab.engine.claims import (
        _load_claim_source_path, _safe_claim_source_summary,
        _safe_research_source_summary, build_context_pack, claims_for_memory,
        load_research_claims, render_context_pack,
    )
    from looplab.engine.claims_health import scope_cross_run_sources
    from looplab.engine.governance_health import observed_path_missing, project_governed_sources
    from looplab.engine.memory import ConceptCapsuleStore, _portfolio_concept_overview_data

    p = Path(memory_dir)

    def _snapshot():
        # Unlike `cross-run-concepts`, a non-existent argument REFUSES here: the claims read has no
        # useful "no lessons at <path>" answer to give about a directory that is not there.
        resolved = resolve_memory_source(p, "lessons.jsonl", missing_is_dir=False)
        if resolved is None:
            return None
        path, base, lesson_names, source_paths = resolved
        source_names = ["research_claims.jsonl", *lesson_names]
        if pack:
            source_names.append("concept_capsules.jsonl")

        def _project(governance):
            lessons = _load_claim_source_path(path, research=False)
            research = load_research_claims(base)
            # `scope_task` is load-bearing, not a convenience filter. `claim_evidence_digest` commits
            # the projection's WHOLE-SOURCE health receipt (snapshot digest, producer-run counts,
            # quarantine counters) alongside the claim's own evidence, so the digest is a property of
            # the PROJECTION and not of the claim alone. `record_observed_claim_decision` validates
            # against `claims_for_memory(..., scope_task=<the decision's scope>)`, so a review that
            # projected portfolio-wide hands the operator a digest that can never match and every
            # scoped `claim-decide` exits 2 with `claim_evidence_changed` — which is what happened
            # while this command had no `--scope` at all. Review scope must equal decide scope; the
            # HTTP pair (`/api/cross-run/claims?scope_task=` + POST claim-decide `scope`) already did.
            claims = claims_for_memory(
                base, lessons=lessons, research_claims=research, scope_task=scope,
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
                    # scoping is per-STORE (`scope_cross_run_sources` is the access boundary): the pack
                    # joins claims and concepts into ONE payload, so a `--scope` that narrowed the
                    # claims but left the capsules portfolio-wide would put another task's concepts
                    # beside this task's claims — the exact half-scoped join that boundary exists to
                    # prevent. `atlas_for_memory(scope_task=...)` filters all three stores for the same
                    # reason.
                    _lessons, capsules, _research = scope_cross_run_sources(
                        task_id=scope, capsules=ConceptCapsuleStore(caps_path).all())
                    overview, concept_rows = _portfolio_concept_overview_data(
                        capsules, aliases=governance["aliases"],
                        splits=governance["splits"])
                # build the pack before releasing any policy/source lock. Its claims,
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
            # the receipt names the PROJECTION its digests describe, not just the ledger revision.
            # `claim-decide --scope` must be given this exact value or its scoped re-projection
            # produces a different `evidence_digest` and refuses. The HTTP claims response has
            # always echoed `scope_task` for the same reason.
            "scope": scope,
        } if governance_receipt else claims)
        if governance_receipt and not scope:
            # Say so HERE, where the unusable receipt is handed out, rather than leaving the operator to
            # discover it as a `claim_evidence_changed` refusal two commands later. Same discipline as the
            # partial-source WARNINGs below: a receipt must not overstate what it can be used for. On
            # STDERR so the JSON on stdout stays machine-parseable.
            scoped_claims = sorted({str(c.get("scope") or "") for c in claims if c.get("scope")})
            if scoped_claims:
                typer.echo(
                    "WARNING: this receipt describes the PORTFOLIO-WIDE projection, but "
                    f"{len(scoped_claims)} task scope(s) appear in it "
                    f"({', '.join(scoped_claims[:5])}{', …' if len(scoped_claims) > 5 else ''}). "
                    "`claim-decide --scope T` validates against the projection scoped to T, so these "
                    "evidence digests will be refused. Re-run with `--scope T` to obtain a decidable "
                    "receipt for one task.", err=True)
        typer.echo(orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode())
        return
    if research_source.get("source_complete") is not True:
        typer.echo(
            "WARNING: D8 research-claim source is partial/unknown; retained evidence is a lower bound and "
            "exact one-sided states are withheld."
        )
    if claim_source.get("source_complete") is not True:
        lesson_bad, research_bad = quarantined_claim_counts(claim_source)
        typer.echo(
            "WARNING: claim evidence stores are partial; retained claims/counts are lower bounds and "
            "absence is not exact "
            f"(lessons quarantined={lesson_bad}; "
            f"research quarantined={research_bad})."
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


@app.command(name="memory-orphans")
def memory_orphans_cmd(
    memory_dir: Path = typer.Argument(..., help="Cross-run memory dir (holds cases.jsonl, lessons.jsonl, …)."),
    runs_root: Path = typer.Option(Path("runs"), "--runs-root",
                                   help="The run root that decides which runs still exist."),
    apply: bool = typer.Option(False, "--apply",
                               help="Actually purge. Without it, nothing is written. IRREVERSIBLE."),
    limit: int = typer.Option(25, "--limit", help="How many contributing runs to list."),
    as_json: bool = typer.Option(False, "--json", help="Emit the survey as JSON."),
):
    """Report — and only with `--apply`, remove — cross-run memory rows whose run no longer exists.

    NOT run automatically by anything, and deliberately so. The five cross-run stores are SHARED and
    the purge is irreversible; a run's deletion cascades into them only when the operator asks for
    that cascade, and a store full of rows from runs that were removed outside the UI (a `rm -rf`, a
    temp dir, a worktree) has no deletion to hang off at all. This is the deliberate sweep for that
    case, and it shows you the whole answer before it writes anything.

    Attribution is the cascade's, not a new one: a row is this run's by `run_uid` when it carries
    one and by `run_id` only when it does not — so a uid-less row whose directory NAME still exists
    is never proposed, because it is indistinguishable from that live run's own legacy row. And the
    removal runs through `purge_attributable_memory` once per contributing run, so every tier
    predicate still protects shared evidence: a lesson consolidated with another run's support, a
    capsule whose concepts governance merged, and a claim another run's curation decision was
    computed over all survive their writer. The writing run being gone does not make its
    contribution to a surviving row somebody else's to discard.

    If any surviving run's event log cannot be read, the survey says `blind` and refuses to call a
    uid-carrying row orphaned at all — an unknown uid must never read as an absent one.
    """
    from looplab.serve.memory_cascade import orphan_survey, purge_attributable_memory

    survey = orphan_survey(memory_dir, runs_root)
    if as_json and not apply:
        typer.echo(orjson.dumps(survey, option=orjson.OPT_INDENT_2).decode())
        raise typer.Exit(0)
    if not survey["available"]:
        typer.echo(f"{memory_dir}: no such cross-run store")
        raise typer.Exit(1)
    typer.echo(f"{survey['memory_dir']} (runs root {survey['runs_root']})")
    typer.echo(f"  {survey['orphan_rows']} row(s) whose run is gone · "
               f"{survey['live_rows']} row(s) belonging to a run that still exists (untouched)")
    if survey["blind"]:
        typer.echo(f"  BLIND: could not read the identity of {len(survey['unreadable_runs'])} "
                   f"surviving run(s) — {', '.join(survey['unreadable_runs'][:5])}. "
                   f"Rows naming a run_uid are NOT being called orphaned.")
    for store in survey["stores"]:
        if store.get("unreadable"):
            typer.echo(f"  {store['store']:<18} UNREADABLE")
        else:
            typer.echo(f"  {store['store']:<18} orphan {store['orphan_rows']:>4}   "
                       f"live {store['live_rows']:>4}")
    typer.echo(f"\n  {len(survey['identities'])} contributing run(s) no longer on disk:")
    for identity in survey["identities"][:limit]:
        uid = identity["run_uid"] or "(no uid — pre-2026-08-11 run)"
        typer.echo(f"    {identity['rows']:>4} rows  {identity['run_id']:<24} {uid}")
    if len(survey["identities"]) > limit:
        typer.echo(f"    … and {len(survey['identities']) - limit} more")
    if not apply:
        typer.echo("\nNothing was written. Re-run with --apply to purge. THIS IS IRREVERSIBLE.")
        raise typer.Exit(0)
    deleted = kept = 0
    failures: list[dict] = []
    for identity in survey["identities"]:
        receipt = purge_attributable_memory(memory_dir, identity["run_id"], identity["run_uid"])
        deleted += receipt["deleted"]
        kept += receipt["kept"]
        failures.extend(receipt["failures"])
    typer.echo(f"\npurged {deleted} row(s); {kept} row(s) kept by a tier rule; "
               f"{len(failures)} failure(s)")
    for failure in failures[:20]:
        typer.echo(f"  FAILED {failure.get('store')}: {failure.get('error')}")
    raise typer.Exit(1 if failures else 0)
