"""Run-directory durability commands: `run-mirror` / `run-mirror-status` / `run-mirror-restore`.

What each touches: `run-mirror` READS the run directories under `--src` and writes ONLY under
`--dst` (the mirrored copies and one manifest per run) plus its own state file under
`<src>/.looplab-mirror/`; it appends no event, takes no lock the engine takes and calls no model.
`run-mirror-status` reads the state file and the source only. `run-mirror-restore` writes ONE new run
directory under `--src` (refusing an existing one) from the mirror. See `events/run_mirror.py`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import typer

from looplab.cli import CliRefusal, app

_DST_ENV = "LOOPLAB_RUN_MIRROR_DST"


def _config(src: Path, dst: Optional[Path], **kw):
    from looplab.events.run_mirror import MirrorConfig

    dst = dst or (Path(os.environ[_DST_ENV]) if os.environ.get(_DST_ENV) else None)
    if dst is None:
        raise CliRefusal(f"--dst is required (or set {_DST_ENV})")
    try:
        return MirrorConfig(src_root=src.expanduser(), dst_root=dst.expanduser(), **kw)
    except ValueError as exc:
        raise CliRefusal(str(exc)) from exc


@app.command(name="run-mirror")
def run_mirror_cmd(
    src: Path = typer.Option(Path("runs"), "--src", help="The run root to mirror (e.g. the tmpfs "
                                                         "/dev/shm/looplab-runs)."),
    dst: Optional[Path] = typer.Option(None, "--dst", help=f"Durable destination root (default "
                                                           f"${_DST_ENV})."),
    loop: bool = typer.Option(False, "--loop", help="Keep mirroring every --interval seconds."),
    interval: float = typer.Option(300.0, "--interval", help="Seconds between passes with --loop."),
    max_mbps: float = typer.Option(16.0, "--max-mbps", help="Read/write budget, MiB per second."),
    max_files_per_s: float = typer.Option(20.0, "--max-files-per-s",
                                          help="Destination file operations per second."),
    max_file_mb: float = typer.Option(256.0, "--max-file-mb",
                                      help="Larger files are skipped (named in the manifest)."),
    op_timeout: float = typer.Option(120.0, "--op-timeout",
                                     help="Seconds before a destination call counts as stuck."),
    run: list[str] = typer.Option([], "--run", help="Only this run (repeatable)."),
    exclude: list[str] = typer.Option([], "--exclude", help="Glob over run-relative paths to skip "
                                                            "(repeatable), e.g. 'nodes/*/ckpt/*'."),
    exit_on_stuck: bool = typer.Option(False, "--exit-on-stuck", help="Exit 3 when a destination "
                                       "call hangs (for a supervisor that restarts the mirror)."),
) -> None:
    """Copy tmpfs run directories to durable storage, throttled and crash-safe.

    Only changed files are copied; an append-only `*.jsonl` is cut at its last newline; each run
    gets a manifest with every file's SHA-256, which `run-mirror-restore` verifies. Never lists the
    destination during a pass and never blocks an engine: a hung destination call stops the pass."""
    from looplab.events.run_mirror import mirror_loop

    cfg = _config(src, dst, interval_s=interval, max_bytes_per_s=max_mbps * 1024 * 1024,
                  max_files_per_s=max_files_per_s, max_file_bytes=int(max_file_mb * 1024 * 1024),
                  op_timeout_s=op_timeout, runs=tuple(run), exclude=tuple(exclude))
    code = mirror_loop(cfg, passes=None if loop else 1, exit_on_stuck=exit_on_stuck,
                       log=lambda line: typer.echo(line, err=True))
    if code:
        raise typer.Exit(code)


@app.command(name="run-mirror-status")
def run_mirror_status_cmd(
    src: Path = typer.Option(Path("runs"), "--src", help="The mirrored run root."),
    dst: Optional[Path] = typer.Option(None, "--dst", help=f"Destination root (default ${_DST_ENV})."),
) -> None:
    """Show the mirror's last pass, a stuck destination, recent errors and each run's lag
    (read-only; reads the state file and the source, never the destination)."""
    from looplab.events.run_mirror import mirror_status

    typer.echo(json.dumps(mirror_status(_config(src, dst)), ensure_ascii=False, indent=1))


@app.command(name="run-mirror-restore")
def run_mirror_restore_cmd(
    run: Optional[str] = typer.Argument(None, help="Mirrored run to restore (omit with --all-missing)."),
    src: Path = typer.Option(Path("runs"), "--src", help="The run root to restore into."),
    dst: Optional[Path] = typer.Option(None, "--dst", help=f"Destination root (default ${_DST_ENV})."),
    as_name: Optional[str] = typer.Option(None, "--as", help="Restore under another run name."),
    all_missing: bool = typer.Option(False, "--all-missing",
                                     help="Restore every mirrored run absent from --src."),
    no_verify: bool = typer.Option(False, "--no-verify", help="Skip the SHA-256 check."),
) -> None:
    """Restore a mirrored run directory (after a pod restart wiped tmpfs), verified against its
    manifest; refuses to overwrite an existing run. Then `looplab resume <run>`."""
    from looplab.events.run_mirror import restore_missing, restore_run

    cfg = _config(src, dst)
    if all_missing:
        out = restore_missing(cfg.dst_root, cfg.src_root, verify=not no_verify,
                              runs=[run] if run else ())
    else:
        if not run:
            raise CliRefusal("name a run, or pass --all-missing")
        try:
            out = restore_run(cfg.dst_root, run, cfg.src_root, as_name=as_name,
                              verify=not no_verify)
        except (OSError, RuntimeError, ValueError) as exc:
            raise CliRefusal(str(exc)) from exc
    typer.echo(json.dumps(out, ensure_ascii=False, indent=1))
