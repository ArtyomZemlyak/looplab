"""Typer CLI (I0/I6, ADR-17): run / resume / inspect / replay.

The engine is a *process*, not a server (ADR-18): `LoopLab run task.json` spawns one
async orchestrator that drives the loop to completion (or crash). `resume` re-enters
the same run dir; `replay`/`inspect` are pure read-only folds of the event log.

Package layout (docs/15 §P5.2): `looplab.cli` is a PACKAGE, not the old flat module — the command
groups live in sibling modules (`run_cmds`, `export_cmds`, `inspect_cmds`, `ui_cmds`) imported at
the bottom so their `@app.command` registrations run. This `__init__` keeps the Typer `app`, the
shared builders (`_engine`, `_load_task`, `_engine_singleton`, …) and re-exports every command, so
`looplab.cli:app` (BOTH console scripts), `python -m looplab.cli` (via `__main__.py`) and every
`looplab.cli.<name>` attribute that tests import or monkeypatch keep resolving exactly as they did
when this was one file.

It also owns the ONE exception boundary every command shares (`_RefusalBoundaryGroup`): a
deliberate refusal (`core/errors.py::OperatorRefusal`) becomes a message + exit
`REFUSAL_EXIT_CODE`, everything else keeps the Rich traceback Typer renders by default.
"""
from __future__ import annotations

import errno
import logging
import os
import sys
import copy
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import typer
from typer.core import TyperGroup

from looplab import __version__
from looplab.core.config import Settings
from looplab.core.errors import EnvironmentRefusal, OperatorRefusal, exception_leaves
from looplab.core.run_deletion import (
    RunDeletionFenceError, RunDeletionStorageError, assert_run_deletion_write_allowed)
from looplab.core.run_reset import (
    RunResetFenceError, RunResetStorageError, assert_run_reset_write_allowed)
from looplab.events.eventstore import INTEGRITY_COMPLETE, EventStore, integrity_sentence
from looplab.engine.options import EngineOptions
from looplab.engine.orchestrator import (
    Engine,
    SPECULATION_CALIBRATION_PROFILE_SETTINGS,
)
from looplab.search.policy import make_policy
from looplab.search.speculation_calibration import speculation_runtime_scope_digest
from looplab.runtime.sandbox import docker_tier_kwargs, make_sandbox
from looplab.adapters.tasks import TaskAdapter, kinds, load_task, make_llm_client, make_roles
from looplab.tools.vectorstore import make_embedder as _make_embedder
from looplab.adapters.tasks import _make_abstractor as _make_lesson_abstractor
_TASK_KINDS = tuple(kinds())


def _make_cli_streams_total() -> None:
    """Prevent help/error rendering from crashing on a legacy text encoding.

    Typer's Rich help writes directly to ``sys.stdout``/``sys.stderr``.  On Windows those streams can
    still be strict cp1252 even though our command descriptions legitimately contain arrows and other
    Unicode notation.  Keep the host's chosen encoding (so a legacy console does not receive forced
    UTF-8 bytes), but make unrepresentable glyphs degrade to a replacement instead of turning
    ``looplab --help`` into a traceback.  Redirected/captured streams without ``reconfigure`` are left
    untouched.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            # Embedders may expose a closed, detached or immutable stream.  CLI execution must remain
            # usable there; Click/Rich will apply whatever policy that host supplied.
            continue


# THE ONE PLACE LOOPLAB CONFIGURES LOGGING, and until 2026-09-01 there was none — which made the
# level of all 39 `_LOG.warning` sites in the package a property of Python's defaults rather than a
# choice anybody had made. The cost was not theoretical: an `_LOG.info` reached NOBODY on every run
# ever recorded (root sits at WARNING with no handlers, so `logging.lastResort` is what puts a
# record on stderr and it is fixed at WARNING), so the package had exactly ONE usable level and an
# author with a genuinely informational line had to inflate it to WARNING or bury it where nothing
# could ever show it. One line per site, promoted forever, instead of one decision here.
#
# The DEFAULT IS WARNING, so this changes what nobody asked for: the same records reach stderr, and
# an INFO line still reaches nobody unless an operator asks for it. What it adds is that asking is
# now possible, and that a record arrives with its level and logger on it — `lastResort` writes the
# bare message, indistinguishable from a stray `print` or a library's own output and ungreppable by
# level.
LOG_LEVEL_ENV = "LOOPLAB_LOG_LEVEL"
DEFAULT_LOG_LEVEL = "WARNING"


def _configure_cli_logging() -> None:
    """Configure logging when the CLI is invoked — never on import, never for an embedder.

    Same shape and same reasoning as `_make_cli_streams_total` beside it: a library embedding
    LoopLab owns its own logging, and a module that configures logging at import time takes that
    away from every host that merely imported it. `basicConfig` is additionally a NO-OP when the
    root logger already has a handler, so an embedder that configured logging first keeps it even
    on the path where its own code then calls into this CLI — the host wins, without a probe here
    that could disagree with the one `basicConfig` makes internally.

    A deployment env var rather than a `Settings` field or a `--verbose` flag, for exactly the
    reason `LOOPLAB_TRACEBACK` is one (see its comment): it is a property of the shell you are
    debugging in, it must work on EVERY command including the ones with no settings surface at all,
    and it must never be snapshotted into a run. It reaches a UI-launched engine too — those are
    spawned as `python -m looplab.cli` (`serve/engine_proc.py`), so they come through here and
    inherit the exporting shell's environment.

    An UNPARSEABLE value degrades to the default and says so, rather than raising: this runs before
    `super().__call__`, i.e. outside `_RefusalBoundaryGroup`, so a raise here is not a refusal an
    operator reads — it is a raw traceback at exit 1, the presentation that boundary exists to
    remove. Degrading keeps the CLI usable, and the complaint is visible because the default level
    that carries it has just been installed.
    """
    raw = os.environ.get(LOG_LEVEL_ENV, "").strip()
    level = logging.getLevelName(raw.upper()) if raw else DEFAULT_LOG_LEVEL
    # `getLevelName` answers the STRING "Level <x>" for anything it does not know, so a non-int
    # result is exactly "this is not a level" — the documented way to ask, and the reason this is
    # not a hand-written name table that would drift from the stdlib's.
    unknown = raw and not isinstance(level, int)
    logging.basicConfig(level=DEFAULT_LOG_LEVEL if unknown else level,
                        format="%(levelname)s %(name)s: %(message)s")
    if unknown:
        logging.getLogger(__name__).warning(
            "%s=%r is not a logging level; using %s", LOG_LEVEL_ENV, raw, DEFAULT_LOG_LEVEL)


class _TotalOutputTyper(typer.Typer):
    """Typer entry point that configures output only when the CLI is actually invoked."""

    def __call__(self, *args, **kwargs):
        _make_cli_streams_total()
        _configure_cli_logging()
        return super().__call__(*args, **kwargs)


# A deliberate refusal exits 2 — the code every other refusal on this CLI already uses
# (`typer.BadParameter`, `_require_run_dir`, `_require_healthy_log`). 1 stays what it has always
# meant on a program that lets an exception reach the interpreter: LoopLab crashed. So the pair is
# machine-readable, and `looplab run … || retry` scripts can tell "fix your flags" from "file a bug".
REFUSAL_EXIT_CODE = 2

# Escape hatch for whoever has to debug a refusal that is firing when it should not: the message is
# the whole story for an operator, but a maintainer sometimes needs the frames that produced it.
# A deployment env var rather than a `Settings` field / flag, for the same reason as
# `LOOPLAB_ALLOW_UNLOCKED_WRITER`: it is a property of the shell you are debugging in, must work on
# EVERY command (including the ones with no settings surface at all), and must never be snapshotted
# into a run.
TRACEBACK_ENV = "LOOPLAB_TRACEBACK"


def deliberate_refusals(exc: BaseException) -> list[BaseException]:
    """The refusals inside `exc`, or an EMPTY list when anything in it is not one.

    Not just `isinstance`, because anyio wraps whatever escapes a task group in an `ExceptionGroup`
    — even a lone exception (anyio 4). `Engine.run` therefore delivers a MID-RUN refusal grouped
    while the identical refusal raised at the preflight boundary arrives bare, and taking the bare
    case only made the same `LLMError` read as a one-line refusal before the loop and as a
    137-line traceback inside it. That grouping is a transport artifact of structured concurrency,
    not a difference in kind, so it must not decide the presentation.

    FAIL CLOSED ON A MIXED GROUP. `split` partitions the leaves; one non-refusal leaf and the whole
    group is returned as "not a refusal" and keeps its traceback. A concurrent batch that hit a
    dead endpoint AND tripped a real bug is a BUG report — printing the half that reads nicely
    would hide the half that matters.
    """
    if isinstance(exc, BaseExceptionGroup):
        refusals, others = exc.split(OperatorRefusal)
        if others is not None or refusals is None:
            return []
        return list(_exception_leaves(refusals))
    return [exc] if isinstance(exc, OperatorRefusal) else []


# Flattening an exception group is `core/errors.py::exception_leaves` — the module every layer that
# has to ask "what actually failed in there" may import. This name is kept as the local spelling so
# the existing call sites and their monkeypatch seams read unchanged.
_exception_leaves = exception_leaves


def refusal_report(exc: BaseException) -> str:
    """Render a deliberate refusal as the message an operator can act on.

    Everything a refusal knows is in its own sentence — that is the entry bar for carrying
    `OperatorRefusal` at all — so the body is `str(exc)` and nothing else. What is NOT free is the
    explicitly CHAINED cause: `raise LLMError("LLM base URL is malformed") from exc` puts the only
    concrete detail (which part is malformed) in `__cause__`, and printing just the head would drop
    it. Follow `__cause__` only — never the implicit `__context__`, which is whatever exception
    happened to be in flight and is noise far more often than not.

    Bounded at three links, and a cause already quoted in the text above it is skipped: most sites
    interpolate theirs (`f"LLM request to {url} failed: {exc}"`), and repeating it would make the
    refusal look like the traceback this exists to replace.
    """
    lines = [f"Refused: {exc}"]
    seen = {id(exc)}
    cause = exc.__cause__
    while cause is not None and id(cause) not in seen and len(lines) <= 3:
        seen.add(id(cause))
        text = str(cause).strip()
        if text and not any(text in line for line in lines):
            lines.append(f"  caused by {type(cause).__name__}: {text}")
        cause = cause.__cause__
    return "\n".join(lines)


class _RefusalBoundaryGroup(TyperGroup):
    """The ONE place a deliberate refusal stops being an exception and becomes an operator message.

    WHY HERE. Typer's `pretty_exceptions_enable` defaults to True, so anything that reached the
    interpreter was rendered as a Rich traceback with the message on the last line: a refused
    `-s policy=mcts -s speculation_depth=1` printed 42 frames, a refused width re-entry 33, and the
    LLM credential preflight 43. Turning the pretty traceback OFF instead would have been the wrong
    fix in the other direction — a genuine bug would then print a bare Python traceback and a
    refusal would still print one. The split has to be by exception TYPE, and it has to be one
    handler rather than a `try` at each raise site, because the raise sites are in `core`, `engine`,
    `search` and `runtime`, none of which may know about Typer.

    WHY THE GROUP'S `invoke` and not `Typer.__call__`. `__call__` runs only under the real console
    script; `typer.testing.CliRunner` (which the suite uses ~60 times) goes straight to
    `get_command(app).main(...)`. A boundary in `__call__` would therefore be invisible to every
    test that could prove it works. `invoke` is on the path of both.

    WHAT IT MUST NOT DO — swallow. `except Exception` is wide because `deliberate_refusals` needs
    to look INSIDE an exception group, but the default action is a bare `raise`: only a value whose
    every leaf carries `OperatorRefusal` is ever rendered as a message, and that marker is only
    worn by exceptions raised on purpose about the operator's own input (`core/errors.py` states
    the bar). Everything else — a plain `ValueError`/`RuntimeError` from the very same modules, a
    mixed group, Click's own `UsageError`/`Exit` — is re-raised with its traceback and its exit
    code untouched, because a bug printed as a tidy one-line message is far worse than a refusal
    printed as a traceback. `LOOPLAB_TRACEBACK=1` re-raises the refusals too.
    """

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except Exception as exc:  # noqa: BLE001 - classified below; anything unrecognized re-raises
            refusals = deliberate_refusals(exc)
            if not refusals or _truthy_env(TRACEBACK_ENV):
                raise
            reports: list[str] = []
            for refusal in refusals:            # a concurrent batch can refuse N times identically
                report = refusal_report(refusal)
                if report not in reports:
                    reports.append(report)
            typer.echo("\n".join(reports), err=True)
            # `from exc`, not a bare raise: the refusal stays the recorded CAUSE of the Exit, so
            # nothing is discarded even on the path that stops printing it. `LOOPLAB_TRACEBACK=1`
            # above is the supported way to get the frames themselves.
            raise typer.Exit(REFUSAL_EXIT_CODE) from exc


# rich_markup_mode="markdown" (not the Typer default "rich"): in "rich" mode square brackets are
# parsed as console style tags, so help text like `pip install 'looplab[ui]'` silently renders as
# `pip install 'looplab'` — the [ui]/[dev]/[otel] extra names vanish. Markdown mode keeps the pretty
# help panels but treats brackets literally, so install hints stay correct.
app = _TotalOutputTyper(
    cls=_RefusalBoundaryGroup,
    add_completion=False,
    rich_markup_mode="markdown",
    help="LoopLab — autonomous ML/DS research engine. Give it a goal; it invents -> implements -> "
         "tests -> improves candidate solutions in a loop and returns the best *verified* result.",
)

# Accepted choices for the role/developer backends, surfaced in errors so a typo gets a clear list
# instead of silently degrading (e.g. `--backend ll` would otherwise run the offline `toy` backend).
_BACKENDS = ("toy", "llm")
_DEV_BACKENDS = ("default", "opencode", "aider", "goose", "continue")


def _version_cb(value: bool) -> None:
    if value:
        typer.echo(f"LoopLab {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version_cb, is_eager=True,
        help="Show the LoopLab version and exit."),
) -> None:
    """LoopLab CLI: run / resume / inspect / replay a research loop. See `looplab COMMAND --help`.

    Run bare `looplab` (no command) to open the terminal control plane (the `tui` command) — a
    chat-first dashboard to start, watch and steer runs."""
    if ctx.invoked_subcommand is None:
        # Bare `looplab` -> the terminal control plane. `looplab --help` / `--version` are handled by
        # Typer's eager options before we get here, so this fires only on a genuine no-arg invocation.
        from looplab.serve.tui import main as tui_main
        raise typer.Exit(tui_main(None, os.environ.get("LOOPLAB_RUN_ROOT", "runs")))


def _choice(value: str, choices: tuple[str, ...], param: str) -> str:
    """Validate `value` against `choices`, raising a clear BadParameter (lists the valid options)
    instead of letting an unknown value silently fall through to a degraded default."""
    if value not in choices:
        raise typer.BadParameter(f"{value!r} is not valid for {param}; choose one of: "
                                 f"{', '.join(choices)}")
    return value


def _load_task(task_file: Path, *, existing_run: bool = False) -> TaskAdapter:
    """Load a task JSON with friendly CLI errors: a missing file or malformed/invalid task becomes a
    one-line BadParameter (exit 2), not a raw multi-frame Python traceback dumped at the user.

    `existing_run=True` when the file is the snapshot of a run that already has history (`resume`,
    `finalize`, wrap-up recovery): a validation rule added after that snapshot was written must not
    make the run unresumable. See `adapters/repo_task.py::_grandfathered`."""
    try:
        return load_task(task_file, existing_run=existing_run)
    except FileNotFoundError:
        raise typer.BadParameter(f"task file not found: {task_file}")
    except (ValueError, KeyError, TypeError) as e:
        raise typer.BadParameter(f"could not load task {task_file}: {e}")


_RUN_DIR_HINT = "Pass the run directory created by `looplab run --out <dir>`."


def _require_healthy_log(store: EventStore, run_dir: Path) -> None:
    """Fail closed BEFORE appending when the event log has a MID-FILE divergence (arch-review §3
    P0-4): a corrupt complete line followed by valid records. Appending would grow a durable tail
    behind the boundary that fold can never see. Direct the operator to `repair-log` and exit, rather
    than warn-and-continue (which silently dropped the tail and grew the invisible one)."""
    div = store.divergence
    if div:
        typer.echo(
            f"events.jsonl in {run_dir} is corrupted at line {div['corrupt_line']} — "
            f"{div['dropped_lines']} later record(s) are on disk but DROPPED on replay (an invisible "
            f"tail). Refusing to resume. Run `looplab repair-log {run_dir}` to back up and truncate "
            f"the log to its last valid boundary, then resume.", err=True)
        raise typer.Exit(2)


def _require_run_dir(run_dir: Path, *, hint: str = _RUN_DIR_HINT,
                     healthy: bool = False) -> EventStore:
    """Open a run's event log, erroring clearly if the dir has none. Without this guard a typo'd path
    folds to an empty state and the read commands print a blank `run=` line with exit 0 — looking like
    a real but empty run rather than a wrong path.

    `hint` is the second sentence — what the operator should do instead — because `resume` can say
    something sharper than the generic advice ("use `run` to start one").

    `healthy=True` additionally runs the mid-file divergence check (doc 25 CT-13). Every MUTATING
    caller pairs the two, and pairing them by hand is how they drifted: three commands re-derived the
    existence check inline, two of them with a shorter message, and each re-opened its own EventStore
    on the very path this function had just validated. Read commands leave it False (they only ever
    fold the recoverable prefix), and `repair-log` MUST leave it False — a corrupt log is its input.

    `healthy=False` is NOT silent, and that is the 2026-08-14 change. "They only ever fold the
    recoverable prefix" was true and was exactly the defect: `replay`, both exports and all six
    concept commands printed a fold of 20 records for `runs/rubertlite-dense-retrieval` — a run whose
    log holds 1,624 — with no indication that 1,603 durable records were behind the boundary. The
    divergence was already computed by the `EventStore` constructor on the line above; it just had no
    reader. A read command now STATES it on stderr and continues (the prefix is still the honest
    answer to "what can be replayed", and `repair-log`/inspection must stay reachable on a log that
    is itself the evidence), while a MUTATING command keeps failing closed exactly as before.
    """
    if not (run_dir / "events.jsonl").exists():
        typer.echo(f"no run found at {run_dir} (no events.jsonl). {hint}")
        raise typer.Exit(2)
    store = EventStore(run_dir / "events.jsonl")
    if healthy:
        _require_healthy_log(store, run_dir)
    else:
        _echo_log_integrity(store, run_dir)
    return store


def _echo_log_integrity(store: EventStore, run_dir: Path) -> None:
    """Print the incomplete-record statement for a READ command, or nothing. Derived from the store's
    own byte scan — never re-derived here, so the CLI and the HTTP surfaces cannot disagree about the
    same file. Goes to stderr so a caller piping `looplab replay` into `jq` still gets clean JSON on
    stdout AND a human running it still cannot miss that the JSON describes a fraction of the run."""
    note = integrity_sentence(log_integrity_from(store), run_label=f"{run_dir}")
    if note:
        typer.echo(note, err=True)


def log_integrity_from(store: EventStore) -> dict:
    """The shared receipt shape, from a store that has ALREADY scanned (no second read of the file).
    `EventStore.__init__` calls `log_divergence`, so this is free at every CLI call site."""
    div = store.divergence
    if div is None:
        return dict(INTEGRITY_COMPLETE)
    return {"complete": False, "good_records": div.get("good_records"),
            "corrupt_line": div.get("corrupt_line"), "dropped_lines": div.get("dropped_lines")}


def load_run_settings(run_dir, *, strict: bool, require_snapshot: bool = False) -> Settings:
    """Load a run's `config.snapshot.json` into Settings — the ONE answer to "which settings does
    this command actually run with" (doc 25 CT-08).

    There used to be three: a strict loader in `run_cmds`, two verbatim inline prologues that called
    it, and an independent re-implementation in `inspect_cmds` that re-parsed the JSON itself and
    fell back silently. Same file, three failure semantics, chosen by which command you happened to
    type.

    `strict` names the two that are actually legitimate, and both matter:

    * ``strict=True`` (run / resume / finalize) — a corrupt or hand-edited snapshot is an
      OPERATOR-FACING input error. Every failure maps to a one-line `BadParameter` naming the file,
      because a raw JSONDecodeError/ValidationError traceback tells the operator nothing about WHICH
      file to fix. Falling back to ambient Settings here would silently drop run-only flags
      (require_approval, trust_mode, confirm_*, eval_trust_mode, backend, …) — e.g. finishing a
      paused not-yet-approved run without any approval.
    * ``strict=False`` (read-only diagnostics) — the snapshot supplies endpoint/model PROVENANCE so a
      diagnostic reaches the endpoint recorded for that run. An absent or unreadable snapshot must
      not stop someone from reading an old or partially-written run, so it degrades to ambient.

    `require_snapshot=True` additionally REFUSES an absent snapshot on a run that already has an
    event log, and exactly one caller asks for it: `resume`. That is the 2026-09-03 fix, and the
    paragraph it replaces ("an absent snapshot is ambient Settings under BOTH modes: `strict` is
    about corruption, not about requiring the file") was the defect its own bullet above describes.
    With the file absent, `resume` took a fresh `Settings()`, whose `require_approval` is `False`
    and was read LIVE (`engine/orchestrator.py::Engine.__init__`, gated in the search spine), so a
    paused approval-pending run could be continued to completion with no approval — by deleting one
    file. `trust_mode`, `eval_trust_mode`, `confirm_*` and `backend` degrade the same way, silently.
    Since 2026-09-06 `require_approval` is also PINNED in `run_started` (invariant #6), which closes
    the snapshot-EDIT half of the same defect for every run started since; this refusal still
    covers the other settings, and every log written before the pin.

    WHY ONLY `resume`, and this is the narrowing that matters: the bypass lives in the SEARCH SPINE,
    and `finalize` and the finalization recovery do not run it — they wrap up a run that has already
    stopped. A run predating `config.snapshot.json` is a real, supported thing
    (`tests/test_finalization_recovery.py::test_cli_finalize_accepts_explicit_task_file_for_legacy_run`
    is named for it), and refusing to FINALIZE one would make an old run permanently unfinishable
    over a gate it can no longer reach. Same asymmetry as `adapters/repo_task.py`'s grandfathering:
    refuse where the operator is about to spend something, grandfather where they are only closing
    the books.

    The discriminator is the run's own EVENT LOG, not the run directory: a bare `--out` path with no
    log is a fresh run whose snapshot has not been written yet (this function is called before the
    engine writes one), and refusing there would break `run` itself. `run_dir=None` is the same case.

    Callers that need the file to exist for a REASON OF THEIR OWN still check and say so
    (`_finalization_recovery_inputs` names both snapshots in one message); this refusal is about the
    settings, so it names the settings that would have been lost.
    """
    snap = Path(run_dir) / "config.snapshot.json" if run_dir is not None else None
    if snap is None or not snap.exists():
        if require_snapshot and snap is not None and (Path(run_dir) / "events.jsonl").exists():
            raise typer.BadParameter(
                f"{snap} is missing, but {run_dir} holds an events.jsonl — this run was started with "
                "settings that are no longer on disk. Continuing would silently run it on defaults "
                "(require_approval, trust_mode, eval_trust_mode, confirm_*, backend, ...), which can "
                "finish an approval-pending run with no approval. Restore the snapshot from a backup, "
                "or copy one from another run of the same task and edit it.")
        return Settings()
    if strict:
        return _settings_from_config_snapshot(snap)
    try:
        return _settings_from_config_snapshot(snap)
    except Exception:  # noqa: BLE001 — any snapshot issue -> ambient fallback for a read-only path
        return Settings()


def _make_llm_client(settings):
    """Late-bound `make_llm_client` so a test patching `looplab.cli.make_llm_client` also stubs these
    diagnostics. Unlike the other shims this one COMPOSES the seam rather than forwarding to it — the
    diagnostics go through `make_llm_client_for`, which wants the builder as a factory argument.

    Lives here rather than in a command group because BOTH groups that reach an endpoint need it:
    the Part IV diagnostics (`concept_cmds`) and the paid cross-run stewards (`governance_cmds`).
    Each group imports it into its own namespace, so a test still patches the module that CALLS it.
    """
    from looplab.core.latebind import late_bound
    from looplab.core.llm import make_llm_client_for
    return make_llm_client_for(settings, factory=late_bound("looplab.cli", "make_llm_client"))


def _settings_for_run(run_dir=None, model=None):
    """Load the run's launch Settings snapshot so a diagnostic sends run code/logs to the same
    endpoint recorded for that run, not a possibly different ambient endpoint. Falls back to ambient
    Settings when the snapshot is absent/unreadable; ``model`` is the only explicit override.

    This helper needs endpoint/model provenance, not the seven event-pinned selection-treatment fields;
    the effective per-run config API owns that latter overlay.

    The ambient fallback is `strict=False` on the SHARED loader (doc 25 CT-08), not a second parse:
    this used to re-read and re-validate the JSON itself, so the same file had two decision tables
    depending on whether you typed a lifecycle command or a diagnostic.
    """
    from looplab.core.llm import apply_llm_model_override

    settings = load_run_settings(run_dir, strict=False)
    if model is not None:
        apply_llm_model_override(settings, model)
    return settings


def _settings_from_config_snapshot(config_snap: Path) -> Settings:
    """Load `config.snapshot.json` into Settings, mapping every failure to a one-line BadParameter.

    A corrupt or hand-edited snapshot is an operator-facing input error, not an internal fault: raw
    JSONDecodeError/ValidationError tracebacks tell the operator nothing about WHICH file to fix.
    Shared by `run`'s finalization recovery and by `resume`/`finalize`, which read the same file.
    """
    import json

    from pydantic import ValidationError

    from looplab.core.config import settings_from_snapshot

    try:
        config_data = json.loads(config_snap.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(
            f"cannot load original config snapshot {config_snap}: {exc}") from exc
    if not isinstance(config_data, dict):
        raise typer.BadParameter(
            f"cannot load original config snapshot {config_snap}: expected a JSON object")
    try:
        return settings_from_snapshot(config_data)
    except ValidationError as exc:
        raise typer.BadParameter(
            f"cannot load original config snapshot {config_snap}: {exc}") from exc


def _truthy_env(name: str) -> bool:
    """A LOOPLAB_* deployment env var read as a boolean (1/true/yes/on, case-insensitive). Not a
    Settings field on purpose: it's a property of the FILESYSTEM/deployment, not the run, so it must
    NOT be snapshotted into run_started (a run moved to a lock-capable disk should re-evaluate)."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _assert_run_deletion_namespace_available(run_dir: Path) -> None:
    """Refuse before mkdir so a fenced, quarantined run name is never recreated as an empty shell.

    `EnvironmentRefusal`, like every other refusal `_engine_singleton` and its helpers raise: the
    operator's command is not wrong, the storage under the run dir they named is not in a state that
    can safely hold a second writer, and the message already names both the fact and the remedy. It
    subclasses `RuntimeError` (the historical base at all eight sites), so nothing that catches or
    asserts on `RuntimeError` changed meaning — the marker only lets `_RefusalBoundaryGroup` print
    these as the one line they are instead of a Rich traceback at exit 1.
    """
    try:
        assert_run_deletion_write_allowed(run_dir)
    except RunDeletionFenceError as exc:
        raise EnvironmentRefusal(
            f"Cannot materialize {run_dir}: deletion {exc.operation_id} is unresolved. "
            "Retry or observe that exact deletion first.") from exc
    except RunDeletionStorageError as exc:
        raise EnvironmentRefusal(
            f"Cannot materialize {run_dir}: deletion ownership cannot be verified.") from exc


@contextmanager
def _engine_singleton(run_dir: Path):
    """Hold an exclusive OS lock on <run_dir>/engine.lock for the engine's whole lifetime, so a second
    `run`/`resume` on the SAME dir can't spawn a concurrent loop (two engines folding+appending the one
    events.jsonl corrupts the log / double-spends the budget). The UI's agentic chat now auto-reopens+
    resumes a finished run per message, so two tabs acting at once is a real race this closes. Yields
    True when the lock was acquired (run), False when another engine already holds it (caller no-ops).
    The OS frees the lock when the process exits (even on crash), so there's no stale-lock problem.
    Where file locking is UNAVAILABLE (FUSE/S3 mounts) single-writer can't be enforced, so it fails
    CLOSED with an actionable error unless LOOPLAB_ALLOW_UNLOCKED_WRITER=1 opts into the risk.
    Every refusal raised here is an `EnvironmentRefusal` so that actionable error is what the
    operator actually SEES — see `_assert_run_deletion_namespace_available` for why that type."""
    # Keep the post-lock check below too: this first check protects the namespace before mkdir, while
    # the second closes publication races before the engine can write.
    _assert_run_deletion_namespace_available(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    f = open(run_dir / "engine.lock", "a+")
    acquired = True
    try:
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    # Only the documented CONTENTION errnos mean "a live engine holds the byte".
                    # Treating every OSError that way turned a permission error, an invalid handle or
                    # an unsupported network filesystem into a phantom "already running" engine that
                    # silently no-ops — the operator sees nothing happen and no reason why. The POSIX
                    # branch below already draws exactly this distinction; mirror it, including the
                    # LOOPLAB_ALLOW_UNLOCKED_WRITER escape hatch for a knowingly-degraded mount.
                    if exc.errno in (getattr(errno, "EDEADLOCK", 36), errno.EACCES):
                        acquired = False    # byte held by a live engine -> caller no-ops
                    elif _truthy_env("LOOPLAB_ALLOW_UNLOCKED_WRITER"):
                        pass   # explicit operator override -> single-writer assumption, run anyway
                    else:
                        acquired = False   # never acquired the lock -> skip the unlock in `finally`
                        raise EnvironmentRefusal(
                            f"Cannot enforce a single writer of the append-only event log: locking "
                            f"{run_dir}/engine.lock failed ({type(exc).__name__}: {exc}). Two runs "
                            f"here could corrupt events.jsonl. Move the run dir to a local disk (see "
                            f"LOOPLAB_RUN_ROOT), or set LOOPLAB_ALLOW_UNLOCKED_WRITER=1 if you "
                            f"guarantee only one engine writes this run dir.") from exc
            else:
                import fcntl
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    acquired = False    # genuinely HELD by a live engine (EWOULDBLOCK) -> caller no-ops
                except OSError as exc:
                    # flock UNSUPPORTED on this filesystem (FUSE/S3 like geesefs, some NFS) raises
                    # ENOTSUP/EINVAL — NOT a held lock, so single-writer CANNOT be enforced here. Two
                    # engines (or the UI server + engine) writing the one events.jsonl unlocked can
                    # interleave appends into a torn line / mint duplicate seq numbers and corrupt the
                    # log (P1-12, doc 17 §6.3). Fail CLOSED by default — but LOUDLY and ACTIONABLY, not
                    # the old silent phantom-"already running" exit that just failed closed with no
                    # explanation. A single operator on such a mount who guarantees one engine per run
                    # dir sets LOOPLAB_ALLOW_UNLOCKED_WRITER=1 to knowingly degrade to a no-op.
                    if _truthy_env("LOOPLAB_ALLOW_UNLOCKED_WRITER"):
                        pass   # explicit operator override -> single-writer assumption, run anyway
                    else:
                        acquired = False   # never acquired the lock -> skip the unlock in `finally`
                        raise EnvironmentRefusal(
                            f"Cannot enforce a single writer of the append-only event log: file "
                            f"locking is unavailable on the filesystem holding {run_dir}/engine.lock "
                            f"({type(exc).__name__}: {exc}). Two runs here could corrupt events.jsonl. "
                            f"Move the run dir to a local disk (see LOOPLAB_RUN_ROOT), or set "
                            f"LOOPLAB_ALLOW_UNLOCKED_WRITER=1 if you guarantee only one engine writes "
                            f"this run dir.") from exc
        except OSError as exc:
            # Anything NOT raised by the two inner handlers above lands here — essentially `f.seek(0)`
            # on the Windows branch. It still fails CLOSED (an unexplained lock failure must not let a
            # second writer into the append-only log), but it no longer does so SILENTLY: a bare
            # `acquired = False` was indistinguishable from "another engine holds it", which is
            # exactly the phantom-"already running" outcome the inner handlers were rewritten to
            # avoid. Say what actually happened so the operator can tell the two apart.
            typer.echo(f"could not acquire {run_dir}/engine.lock — treating the run as already "
                       f"owned ({type(exc).__name__}: {exc})", err=True)
            acquired = False
        if acquired:
            try:
                # Reset publishes its owner while holding the same event/config writer locks, then
                # releases engine.lock for the exact child. Check immediately after acquisition so
                # a waiting direct resume cannot steal that handoff; the matching child is admitted
                # by its inherited operation id.
                assert_run_reset_write_allowed(run_dir)
                assert_run_deletion_write_allowed(run_dir)
            except RunResetFenceError as exc:
                raise EnvironmentRefusal(
                    f"Cannot start an engine for {run_dir}: Replay {exc.operation_id} is unresolved. "
                    "Retry or observe that exact Replay operation first.") from exc
            except RunResetStorageError as exc:
                raise EnvironmentRefusal(
                    f"Cannot start an engine for {run_dir}: Replay ownership cannot be verified.") from exc
            except RunDeletionFenceError as exc:
                raise EnvironmentRefusal(
                    f"Cannot start an engine for {run_dir}: deletion {exc.operation_id} is unresolved. "
                    "Retry or observe that exact deletion first.") from exc
            except RunDeletionStorageError as exc:
                raise EnvironmentRefusal(
                    f"Cannot start an engine for {run_dir}: deletion ownership cannot be verified.") from exc
        yield acquired
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        f.close()


def _foresight_panel_applies(settings, researcher) -> bool:
    """Whether the FOREAGENT predict-before-execute panel should wrap this researcher.

    The two call sites spelled this guard out with ONE clause of difference — the non-unified branch
    also tested `backend == "llm"`, which the unified branch gets for free from `_unified` itself.
    Written twice with a difference, it read as though the two paths were checking different things
    (doc 25 CT-15); folding the clause in here is equivalent and says plainly that they are not.

    Yields to an explicitly-configured numeric `researcher_panel > 1`, so opting into the k-NN panel
    is never silently overridden by this default. Needs a client — a bare surrogate wrapper exposes
    none, and falls through.
    """
    return bool(
        getattr(settings, "foresight", True)
        and settings.backend == "llm"
        and getattr(settings, "foresight_panel", 2) > 1
        and settings.researcher_panel <= 1
        and getattr(researcher, "client", None) is not None)


def _wrap_with_foresight_panel(researcher, settings, ftools):
    """Build the foresight panel around *researcher*. ONE constructor call, deliberately.

    Five getattr-defaulted kwargs written out in two branches is exactly the shape that grows a
    sixth in only one of them, and a drift there would silently change unified-vs-plain behaviour
    with nothing to catch it (doc 25 CT-15).
    """
    from looplab.search.foresight import ForesightPanelResearcher
    return ForesightPanelResearcher(
        researcher, k=settings.foresight_panel, tools=ftools,
        min_confidence=getattr(settings, "foresight_min_confidence", 0.0),
        verify_score=getattr(settings, "foresight_verify", False),
        verify_samples=getattr(settings, "foresight_verify_samples", 3))


def _apply_speculation_calibration_profile(settings: Settings) -> None:
    """Force the source-owned offline measurement profile before any role/client is built."""
    for field, value in SPECULATION_CALIBRATION_PROFILE_SETTINGS.items():
        if field not in settings.__class__.model_fields:
            raise RuntimeError(f"calibration profile references unknown Settings field {field!r}")
        setattr(settings, field, copy.deepcopy(value))


def _make_calibration_roles(task: TaskAdapter, settings: Settings, run_dir: Path):
    researcher, developer = make_roles(task, settings, run_dir)
    # These flags are deliberately not Settings/env/UI knobs.  Engine validates their exact concrete
    # role types and values, including every pair returned by role_factory.
    setattr(researcher, "calibration_concepts", True)
    setattr(developer, "calibration_gpu_probe", True)
    return researcher, developer


def _engine(run_dir: Path, task: TaskAdapter, settings: Settings,
            crash_after: Optional[int], *, speculation_gate_calibration: bool = False,
            wrap_up_only: bool = False) -> Engine:
    from looplab.core.llm import validate_bound_profiles
    from looplab.core.tracing import set_llm_capture
    from looplab.agents.reachability import llm_consumer_plan
    from looplab.agents.preflight import (credential_free_wrap_up_settings,
                                          preflight_role_endpoints, wrap_up_credential_warning,
                                          wrap_up_endpoint_warning)
    # Everything this entry point may lose to a missing model, in the order the two gates run. A
    # wrap-up entry collects instead of raising (see below) and the commands close by naming it.
    _degradations: list[str] = []
    # Fail here, before any role is built, when a role is bound to a connection profile whose
    # credential variable is unset. Every CLI path (run/resume, and the UI, which spawns them)
    # funnels through this constructor, so this is the one gate that catches it — and catching it at
    # the first paid call instead would mean discovering the missing key partway into a run. Optional
    # offline consumers (embedding/Memora) retain their own hash/lexical fallback and are not strict.
    #
    # …unless this entry point can only COMPLETE a wrap-up (`wrap_up_only`, below), which cannot
    # propose and therefore cannot degrade. The refusal then costs the operator every artifact that
    # needs no model and strands the run at `finalization_pending` forever — the identical dead end
    # the endpoint probe's own two policies were written to remove, reached one gate earlier. So
    # warn, and swap in a credential-free copy for the roles: an unusable credential fails at client
    # CONSTRUCTION, not at request time, so the warning alone would just move the same `LLMError` a
    # few lines down into `make_roles` (measured: `finalize` still exited 1 with zero artifacts).
    _credential_plan = llm_consumer_plan(task, settings)
    if wrap_up_only:
        _credential_warning = wrap_up_credential_warning(
            settings, consumer_roles=_credential_plan.roles,
            external_fallback_roles=_credential_plan.external_fallback_roles,
            trusted_in_process_roles=_credential_plan.trusted_in_process_roles)
        if _credential_warning:
            typer.echo(_credential_warning, err=True)
            _degradations.append(_credential_warning)
            # Local rebinding ONLY — the caller keeps the settings it publishes into
            # config.snapshot.json, so the operator's real (merely mis-bound) credential
            # configuration survives for the command that fixes it. (The shared key is popped from
            # snapshots anyway; a profile's `api_key_env` is NOT, and losing it would turn one
            # degraded wrap-up into a permanently uncredentialed run.) Everything below therefore
            # reads the copy, including the in-place calibration-profile mutation — which cannot
            # coincide with this branch: that lane requires a FRESH run dir, and a fresh dir is
            # never wrap-up-only.
            settings = credential_free_wrap_up_settings(settings)
    else:
        validate_bound_profiles(
            settings, consumer_roles=_credential_plan.roles,
            external_fallback_roles=_credential_plan.external_fallback_roles,
            trusted_in_process_roles=_credential_plan.trusted_in_process_roles)
    # The calibrated lane is the toy benchmark and its replays ONLY. A receipt supplied alongside a
    # real Dataset/Repo/Command workload used to drag the whole run into the offline measurement
    # profile (backend="toy", every optional subsystem off) — a run that could not do the operator's
    # work at all. Since a receipt no longer authorizes anything on a real workload (Engine admits
    # positive speculation_depth without one), it must not rewrite that run's settings either; the
    # Engine still revalidates the receipt itself and refuses a stale/forged one.
    from looplab.adapters.toytask import ToyTask
    narrow_speculation_runtime = bool(
        speculation_gate_calibration
        or (settings.speculation_gate_receipt and type(task) is ToyTask))
    if narrow_speculation_runtime:
        # A public receipt authorizes only the offline source-owned profile that produced it. The
        # helper intentionally preserves max_nodes, treatment depth and receipt placement.
        _apply_speculation_calibration_profile(settings)
    if settings.speculation_gate_receipt:
        # Snapshots must retain the receipt's launch identity even when a later resume starts from a
        # different cwd. Engine repeats the normalization for direct library callers.
        settings.speculation_gate_receipt = str(
            Path(settings.speculation_gate_receipt).expanduser().resolve())
    # Capture LLM prompts/completions into spans (UI per-node trace) unless disabled. Diagnostics
    # only; never read by replay.fold. Honors LOOPLAB_TRACE_LLM_IO via Settings.
    # This sets the PROCESS-WIDE DEFAULT — for LLM calls outside any run's tracer (Genesis before the
    # Engine exists, the owner Assistant). The run's own decision travels separately as the
    # `trace_llm_io` EngineOptions knob, which binds it to that Engine's Tracer, so a second run
    # started later in this process cannot flip the policy out from under this one.
    set_llm_capture(settings.trace_llm_io)
    # The runtime-scope primitive is intentionally bounded to the calibration/public-receipt lane
    # (`max_nodes <= 64`).  Ordinary CLI runs may use the product's much larger node budgets and must
    # never cross this rollout-only validator.
    runtime_scope_sha256 = (
        speculation_runtime_scope_digest(settings.masked_snapshot())
        if narrow_speculation_runtime else None
    )
    # …and fail here, still before any role is built, when a required role's endpoint is UNREACHABLE
    # rather than merely uncredentialed. Same gate, same reason, one step later: `settings` is final
    # only after the calibration profile above has been applied, so the probe tests the endpoints the
    # roles will actually use. Every LLM role degrades silently on a transport failure — a dead
    # endpoint otherwise yields N identical empty fallback nodes, a flat metric and finished=True.
    # See `agents/preflight.py::preflight_role_endpoints` for the measurement.
    #
    # `wrap_up_only` says this entry point cannot start new work — it can only complete a wrap-up
    # that is already owed (`finalize`, and the `run`/`resume` boundaries `is_wrap_up` recognizes).
    # There the refusal's own reason does not apply (nothing proposes after the terminal boundary)
    # and refusing costs the operator every artifact that needs no model, so the same probe WARNS
    # instead, naming what the missing endpoint degrades. The result is recorded on the Engine as a
    # CLI-owned annotation: the engine never reads it, the command reads it to keep its closing line
    # honest — and `run_cmds._run_engine_guarded` re-checks the `wrap_up_only` promise against the
    # folded log before entering the loop, so a boundary that moves under a warn-path engine refuses
    # instead of proposing.
    #
    # THE CALLER OWES ONE THING FOR THIS FLAG TO MEAN ANYTHING: it must be decided against the same
    # state as the decision to LIFT the run. `resume` derived it from a fold taken BEFORE the
    # handoff wait — a wait whose whole purpose is to let the previous owner finish the wrap-up,
    # i.e. to produce exactly the transition that invalidates it — and then lifted on a re-fold
    # taken after. Both now happen under the singleton lock, from one fold (`cli/run_cmds.py`).
    _endpoint_plan = llm_consumer_plan(task, settings)
    if not wrap_up_only:
        preflight_role_endpoints(settings, consumer_roles=_endpoint_plan.roles)
    elif not _degradations:
        # `not _degradations`: the credential gate has already answered "this wrap-up has no model".
        # A probe now would necessarily run credential-free (that is what the roles below will use),
        # so it would measure a configuration this run never had and could only repeat the one
        # warning the operator has already been given.
        _endpoint_warning = wrap_up_endpoint_warning(
            settings, consumer_roles=_endpoint_plan.roles)
        if _endpoint_warning:
            typer.echo(_endpoint_warning, err=True)
            _degradations.append(_endpoint_warning)
    role_builder = (_make_calibration_roles if narrow_speculation_runtime else make_roles)
    researcher, developer = role_builder(task, settings, run_dir)
    # Agentic-foresight tools: run-introspection (own experiments) + data facts, so the ranker can
    # PULL actual results before deciding. None when foresight_agentic is off -> the one-shot ranker.
    _ftools = None
    if getattr(settings, "foresight_agentic", True) and getattr(settings, "foresight", True):
        try:
            from looplab.agents.agent import CompositeTools
            from looplab.tools.run_tools import DataTools, RunTools
            _ftools = CompositeTools([RunTools(), DataTools(task)])
        except Exception:  # noqa: BLE001 — introspection tools are optional; degrade to one-shot ranking
            _ftools = None
    # Unified mode: researcher IS developer (one agent). Skip the researcher-only wrappers
    # (surrogate/panel) — they would re-wrap `researcher` without re-wrapping `developer`, so the
    # two handles would diverge mid-run (R1). The unified agent owns its own ideation machinery.
    _unified = settings.unified_agent and settings.backend == "llm"
    if not _unified:
        # A2 surrogate-guided proposer: wrap the base Researcher when the task exposes numeric bounds
        # (it bootstraps via the wrapped Researcher and delegates on non-numeric spaces). A3 BOHB =
        # ASHA racing + the surrogate, so `policy=bohb` auto-enables it.
        if settings.surrogate_proposer or settings.policy == "bohb":
            from looplab.search.surrogate import SurrogateResearcher
            # Constructed whether or not the TASK declares bounds. Only the built-in benchmarks
            # declare them, so the old `if _bounds:` gate meant this setting silently did nothing on
            # repo and dataset tasks — the ones real operators run — while the settings table said it
            # was on. The wrapper self-gates: with no bounds and no usable history it delegates to the
            # wrapped Researcher exactly as before, and once the run has enough evaluated numeric
            # params it learns the ranges from them.
            researcher = SurrogateResearcher(getattr(researcher, "bounds", None) or {},
                                             fallback=researcher,
                                             explore=settings.surrogate_explore)
        # FOREAGENT predict-before-execute for HYPOTHESES: rank K candidate ideas with the LLM world
        # model primed with the data profile + experiment memory — it compares the structural / text
        # ideas the numeric surrogate can't. ON by default for the LLM backend.
        if _foresight_panel_applies(settings, researcher):
            researcher = _wrap_with_foresight_panel(researcher, settings, _ftools)
        # E2 researcher panel: generate K ideas and keep the best by the empirical surrogate.
        elif settings.researcher_panel > 1:
            from looplab.search.panel import PanelResearcher
            researcher = PanelResearcher(researcher, k=settings.researcher_panel)
    elif _foresight_panel_applies(settings, researcher):
        # Foresight in UNIFIED mode: the wrappers above are skipped because they'd re-wrap only the
        # researcher handle, but ForesightPanelResearcher now DELEGATES its whole developer surface to
        # the wrapped agent (__getattr__), so wrapping the single unified agent and using it for BOTH
        # handles keeps them identical — predict-before-execute + hypothesis-board prioritization work,
        # implement/repair pass straight through. (Numeric surrogate/panel stay researcher-only, so
        # they remain unified-skipped; only the client-based foresight is safe to share.)
        researcher = _wrap_with_foresight_panel(researcher, settings, _ftools)
        developer = researcher
    # RepoTask onboarding (Phase 3): if the task can propose its own eval spec, build the
    # onboarder (Researcher proposes + Developer writes the adapter).
    mk = getattr(task, "make_onboarder", None)
    onboarder = mk(settings) if callable(mk) else None
    # A7 Strategist (optional adaptive meta-control) + live Developer-backend swap factory.
    from looplab.agents.strategist import make_strategist
    from looplab.adapters.tasks import make_developer_factory
    from looplab.core.llm import make_llm_client_for
    if _unified:
        # The unified control facade IS the strategist handle: its `.decide()` delegates to the
        # strategy-stage backend it built internally (None when strategist_backend="off"). It is
        # one engine-facing object over stage-local clients/contexts; replay stays unchanged (the
        # engine still records/replays `strategy_decision`).
        strategist = researcher
    else:
        # Resolved for the `strategist` role: until `strategist_model`/`strategist_base_url`
        # existed this call passed only a temperature, so the Strategist was pinned to the shared
        # `llm_model` no matter how the other roles were pointed. Blank fields resolve back to the
        # shared values, so a run that sets neither is unchanged.
        # `factory=make_llm_client` is this MODULE's name, which tests and operator tooling
        # monkeypatch; building through core's own binding instead would route past that seam.
        strat_client = (make_llm_client_for(settings, role="strategist",
                                            factory=make_llm_client)
                        if settings.backend == "llm" and settings.strategist_backend in ("llm", "agent")
                        else None)
        from looplab.adapters.tasks import build_strategist_tools
        # Only build the (KB-indexing) strategist toolset when an agent strategist will actually use
        # it — i.e. a client is wired. Without one, make_strategist falls back to RuleStrategist and
        # the toolset would be built (paying KnowledgeTools' vector-index cost) only to be discarded.
        strat_tools = (build_strategist_tools(task, settings, run_dir)
                       if strat_client is not None and settings.strategist_backend == "agent" else None)
        strategist = make_strategist(settings, client=strat_client, n_seeds=settings.n_seeds,
                                     tools=strat_tools)
    # Deep-Research stage (Phase 2): reachable only with an LLM backend. Reuses the run's LLM client;
    # tools (arXiv/web/knowledge) are wired from config inside make_deep_researcher. None when off.
    from looplab.agents.deep_research import make_deep_researcher
    # Deep-Research is researcher-flavored (breadth-seeking ideation), so it honors researcher_temperature.
    deep_researcher = (make_deep_researcher(
        settings, client=make_llm_client_for(
            settings, role="researcher", factory=make_llm_client), task=task, run_dir=run_dir)
                       if settings.backend == "llm" else None)
    # Agent-authored run report (Workstream A): reachable only with an LLM backend; reuses the run's
    # LLM client. None in toy mode -> the UI shows the deterministic report only.
    from looplab.serve.report import make_report_writer
    report_writer = (make_report_writer(
        settings, client=make_llm_client_for(settings, factory=make_llm_client))
                     if settings.backend == "llm" else None)
    dev_factory = make_developer_factory(task, settings) if settings.backend == "llm" else None
    proxy_scorer = None
    if settings.proxy_scoring or settings.proxy_kill_fraction > 0:
        from looplab.search.proxy import ProxyScorer
        proxy_scorer = ProxyScorer(kill_fraction=settings.proxy_kill_fraction)
    # Every pure-config Settings→Engine knob travels as ONE bundle (BACKLOG §4); only the built
    # OBJECTS (roles, sandbox, policy, strategist, scorers, …) and genuinely CLI-specific values
    # (crash_after comes from a CLI flag, not Settings) remain explicit kwargs.
    engine = Engine(
        run_dir,
        task=task,
        researcher=researcher,
        developer=developer,
        # `docker_tier_kwargs(settings)` is the ONE Settings->container translation the three Docker
        # surfaces share (see its docstring); the two `*_local` caps are spelled here because they
        # belong to the SUBPROCESS tier and have no container to describe.
        sandbox=make_sandbox(settings.trust_mode, **docker_tier_kwargs(settings),
                             mem_local=settings.sandbox_memory_local,
                             fsize_local=settings.sandbox_fsize_local),
        policy=make_policy(settings.policy, n_seeds=settings.n_seeds,
                           max_nodes=settings.max_nodes, ablate_every=settings.ablate_every,
                           eta=settings.asha_eta,     # forwarded to ASHA (greedy/mcts/evo ignore it)
                           rung_nodes=settings.asha_rung_nodes,
                           debug_depth=settings.debug_depth,
                           operator_bandit=settings.operator_bandit),
        options=EngineOptions.from_settings(settings),
        crash_after=crash_after,
        # Maintainer-only bootstrap path for producing the paired evidence that the public positive
        # speculation path requires.  The CLI admits this flag only for a fresh, bounded offline
        # quadratic run with a real GPU requirement; Engine keeps the seam private so Settings,
        # snapshots, environment variables and the Web UI cannot turn it on accidentally.
        _speculation_gate_calibration=speculation_gate_calibration,
        _speculation_runtime_scope_sha256=runtime_scope_sha256,
        onboarder=onboarder,
        strategist=strategist,
        deep_researcher=deep_researcher,
        report_writer=report_writer,
        developer_factory=dev_factory,
        developer_name=(getattr(dev_factory, "initial_backend", "default")
                        if dev_factory is not None else "default"),
        # a fresh wired pair per concurrent build prevents mutable role state from being
        # shared when the settled build width is >1 (canonical llm_parallel; legacy parallel_build).
        # This is LLM/build isolation, not proof of the later evaluation's CPU/GPU allocation.
        role_factory=(lambda: role_builder(task, settings, run_dir)),
        proxy_scorer=proxy_scorer,
        embedder=_make_embedder(settings),
        # Memora synergy: harmonic recall over the cross-run lessons tier (same abstractor Memora
        # uses for the case/KB index; shares its content-hash cache). None when memora is off; a
        # dead LLM endpoint degrades to the deterministic lexical abstractor inside make_abstractor.
        lesson_abstractor=_make_lesson_abstractor(settings),
    )
    # CLI-owned annotations, never read by the engine. `wrap_up_degradation_warning` is what the
    # wrap-up commands close with, so their last line says whether the artifacts they just wrote
    # were produced without a model; `wrap_up_only` is the promise this engine was built on, which
    # `run_cmds._run_engine_guarded` re-checks against the log before driving the loop.
    engine.wrap_up_degradation_warning = "\n".join(_degradations) or None
    engine.wrap_up_only = wrap_up_only
    return engine


def _print_result(state) -> None:
    best = state.best()
    typer.echo(f"run={state.run_id} task={state.task_id} finished={state.finished}")
    typer.echo(f"nodes={len(state.nodes)} evaluated={len(state.evaluated_nodes())}")
    if best is not None:
        m = best.robust_metric
        ms = f"{m:.6g}" if m is not None else "n/a"
        typer.echo(f"BEST node {best.id}: metric={ms} params={best.idea.params}")


def _exit_nonzero_if_the_run_produced_nothing(state, run_dir, *, wrap_up_only: bool) -> None:
    """A run that FINISHED having evaluated nothing is a failure, and must not exit 0.

    Measured (`/tmp/ll-s4/run`): five real HTTP 429s during the first card build left the run stuck
    at 0 nodes in 95 seconds. It printed `finished=True`, wrote a report whose own text said "No
    experiments have been evaluated yet — the run has 0 nodes", and exited 0 — so every wrapper, CI
    step and `&&` chain around it read that as success.

    FINISHED only. A paused/stopped run (`looplab stop`, the `developer_crash` breaker, the
    proposal-path provider breaker) is deliberately excluded: it is resumable, not failed, and those
    auto-pause paths rely on exit 0 meaning "frozen, come back to it". `finished` is the fold's own
    `run_finished` flag, which none of them sets — so the exclusion is the predicate, not a second
    list of reasons to keep in sync. Nor does this touch the fatal-error path: `_run_engine_guarded`
    re-raises, so a crash never reaches `_print_result` at all.

    A WRAP-UP-ONLY entry point is excluded too, and that one is not free: `run`/`resume` landing on a
    boundary they may only complete (`run_cmds.is_wrap_up`) are forbidden from proposing at all
    (`_refuse_wrap_up_engine_that_could_propose`), so "this run evaluated nothing" is a fact about a
    run that ended before the command started, not about what the command did. Exit 1 there would
    also collide with the meaning that path already has: a non-zero exit on a wrap-up is "the wrap-up
    was REFUSED and wrote no artifact", which is exactly the behaviour `7b11e7ad` removed (see
    docs/guide/cli-reference.md#finalize). Six tests hold that line: five assert `exit_code == 0` on
    a wrap-up (`test_wrap_up_policy_boundaries`, `test_finalization_recovery`), and
    `test_stop_finalize_resume` calls `resume` as a plain function, where a `typer.Exit` raised here
    escapes as an exception rather than an exit status.

    Both signals are checked, and the exit needs BOTH to be empty: `best()` reads `best_node_id`
    rather than the node table, so a run that somehow published a champion without a live evaluated
    node still exits 0 rather than calling a reported result a failure.
    """
    if wrap_up_only or not getattr(state, "finished", False):
        return
    if state.evaluated_nodes() or state.best() is not None:
        return
    typer.echo(
        f"run {run_dir} finished with no evaluated experiments"
        + (f" (reason={state.stop_reason})" if state.stop_reason else "")
        + " — there is nothing to report on. Check the run's `run_finished` reason and the last "
          "`card_build_done` / `node_failed` rows in events.jsonl.", err=True)
    raise typer.Exit(code=1)


# Command registration: importing the command-group modules runs their `@app.command` decorators
# against the `app` above. This block MUST stay at the bottom — the groups import the shared
# builders back from this (still-initializing) package, which is safe only because everything they
# need is already defined by this point.
from looplab.cli import (concept_cmds, export_cmds, governance_cmds,  # noqa: E402,F401
                         maintenance_cmds, memory_cmds,
                         inspect_cmds, run_cmds, ui_cmds)

# Back-compat re-exports: when `looplab/cli.py` was one flat module, every command was an attribute
# of `looplab.cli` (tests call `cli.stop(...)`/`cli.finalize(...)` directly; tools import `app`).
# Keep that attribute surface identical after the package split.
from looplab.cli.run_cmds import approve, finalize, init, resume, run, stop  # noqa: E402,F401
from looplab.cli.export_cmds import (bench, export_mlflow, export_notebook,  # noqa: E402,F401
                                     harden, smoke)
from looplab.cli.inspect_cmds import inspect, replay, tensorboard, timings  # noqa: E402,F401
from looplab.cli.ui_cmds import build_ui, tui, ui  # noqa: E402,F401
