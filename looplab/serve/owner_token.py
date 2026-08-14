"""The owner API credential, and the ONE rule for what an UNSET one means.

`server.py` has always read `LOOPLAB_UI_TOKEN` and, when it is set, default-denied every `/api/*`
request without a matching `X-LoopLab-Token`. The gap this module closes is the DEFAULT: unset meant
unauthenticated, everywhere, including the one deployment the server itself already detects and warns
about — a shared JupyterHub origin, where `jupyter-server-proxy` puts every user's app and every
other proxied page on ONE browser origin, and the same-origin policy is per-origin, not per-path. On
that origin an unauthenticated control plane can be driven by any same-origin page: start a run,
delete a run, edit settings, name a `task_file`. The server logged exactly that and then served it.

WHAT THE DEFAULT IS NOW, and why this shape rather than the two alternatives:

* **Private origin (the default local single-user path): unchanged, still open.** `looplab ui` binds
  `127.0.0.1`; a fresh `pip install` followed by `looplab ui` behaves byte-for-byte as before, with
  no credential to find and no unlock gate. The unauthenticated default there is defence in depth,
  not an open door, and paying a setup cost for it would buy nothing.
* **Shared hub origin, no token set: MINT one and say so.** The server generates a token, stores it
  `0600` under the operator's own home, and logs the path plus the value it just minted. The API is
  then gated exactly as if the operator had exported `LOOPLAB_UI_TOKEN`.
* **Not "bind loopback-only when unset".** It already binds loopback, and that is the exposed
  configuration: jupyter-server-proxy connects to `127.0.0.1` itself and republishes the app on the
  shared public origin. A loopback bind cannot see the difference and so cannot be the boundary.
* **Not "refuse to start".** The hub deployment's whole point is a Launcher tile with no terminal
  (`serve/jupyter.py`); a refusal there is an app that cannot be started at all, and the remedy —
  export an env var — is precisely what the operator has no terminal to do. A minted credential is
  fail-closed AND recoverable: `cat` the file.

COST TO THE OPERATOR, stated plainly. On a shared hub the first start after this change mints a
token, so an already-open browser tab must be unlocked once with it, and any script or TUI calling
`/api/*` needs the header (`serve/tui_api.py` reads the same file, so `looplab tui` keeps working).
`LOOPLAB_UI_ANONYMOUS=1` restores the previous open behaviour for an operator who has a private
origin the detection cannot see — it is deliberately an explicit, logged opt-out and not a silent one.

The token FILE lives under `~/.looplab`, not under the run root: the run root is routinely a shared
or object-backed mount (the deployment guide warns against putting run state on geesefs/S3 at all),
and a per-deployment secret does not belong on a volume whose permission model is not the home's.
"""
from __future__ import annotations

import errno
import logging
import os
import secrets
import stat
from pathlib import Path
from typing import Optional

from looplab.core.errors import EnvironmentRefusal
from looplab.core.pathsafe import is_reparse
from looplab.serve.engine_proc import _on_shared_hub

_log = logging.getLogger("looplab.server")

OWNER_TOKEN_ENV = "LOOPLAB_UI_TOKEN"
OWNER_TOKEN_FILE_ENV = "LOOPLAB_UI_TOKEN_FILE"
OWNER_ANONYMOUS_ENV = "LOOPLAB_UI_ANONYMOUS"

# Sources, in the order `resolve_owner_token` decides them. The string is what gets logged, and the
# set is closed so a caller can branch on it instead of on a message.
SOURCE_ENV = "env"                    # the operator exported LOOPLAB_UI_TOKEN
SOURCE_FILE = "file"                  # a token minted by an earlier start, reused
SOURCE_MINTED = "minted"              # minted by THIS start
SOURCE_PRIVATE_ORIGIN = "private"     # no token, and no shared origin was detected
SOURCE_ANONYMOUS_OPT_OUT = "anonymous"  # no token, shared origin, operator opted out explicitly
OWNER_TOKEN_SOURCES = frozenset({
    SOURCE_ENV, SOURCE_FILE, SOURCE_MINTED, SOURCE_PRIVATE_ORIGIN, SOURCE_ANONYMOUS_OPT_OUT})

_TOKEN_BYTES = 32


def owner_token_path() -> Path:
    """Where a minted owner token is stored. `LOOPLAB_UI_TOKEN_FILE` overrides for a deployment that
    keeps secrets elsewhere (and for the suite, which must never touch the developer's real home)."""
    override = os.environ.get(OWNER_TOKEN_FILE_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".looplab" / "ui-token"


def _refuse_unsafe(path: Path, why: str) -> None:
    raise EnvironmentRefusal(
        f"the LoopLab owner token file {path} {why}. It holds the control-plane credential for this "
        f"deployment: remove it (a new token is minted on the next start), or set "
        f"{OWNER_TOKEN_ENV} to supply your own.")


def read_owner_token_file(path: Optional[Path] = None) -> Optional[str]:
    """Return a stored owner token, or None when there is none.

    Descriptor-first for the same reason `core/trace_files.py` is: this file is a credential, so it
    must be a private REGULAR file and not a link someone else planted pointing at a file they can
    read. A file that exists but is group/world-readable is an `EnvironmentRefusal` rather than a
    silent downgrade — a credential the box has already published is not one to keep using.
    """
    target = owner_token_path() if path is None else path
    try:
        fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:      # O_NOFOLLOW refused it: the name is a symlink
            _refuse_unsafe(target, "is a symbolic link")
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or is_reparse(info):
            _refuse_unsafe(target, "is not a private regular file")
        if info.st_mode & 0o077:
            _refuse_unsafe(target, f"is readable by others (mode {info.st_mode & 0o777:04o})")
        raw = os.read(fd, 4096)
    finally:
        os.close(fd)
    token = raw.decode("utf-8", "replace").strip()
    return token or None


def _mint_owner_token(path: Path) -> str:
    """Write a fresh token `0600`, or adopt one a concurrent start wrote first."""
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                     0o600)
    except FileExistsError:
        # Another `looplab ui` on this box won the race; both must serve the SAME credential or the
        # operator's unlocked tab starts 401ing against whichever process it reaches.
        existing = read_owner_token_file(path)
        if existing:
            return existing
        raise EnvironmentRefusal(
            f"cannot mint a LoopLab owner token: {path} exists but is empty. Remove it, or set "
            f"{OWNER_TOKEN_ENV} to supply your own.")
    except OSError as exc:
        raise EnvironmentRefusal(
            f"cannot write the LoopLab owner token to {path}: {exc}. Set {OWNER_TOKEN_ENV} to supply "
            f"your own, or {OWNER_TOKEN_FILE_ENV} to choose a writable location.") from exc
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    return token


def resolve_owner_token() -> tuple[Optional[str], str]:
    """Return `(token, source)` — the owner credential this server will enforce, and where it is from.

    A minted token is exported into `os.environ[LOOPLAB_UI_TOKEN]` on purpose: four other places ask
    that variable whether the control plane is credentialed (`serve/reviews.py` refuses to create a
    read-only share from an anonymous owner plane, `serve/jupyter.py` picks the un-framed Launcher
    target, `serve/tui_api.py` sends the header, and the `_require_token` middleware itself). A
    minted token that only the middleware knew about would leave those four answering "anonymous"
    about a server that is not, which is worse than either state on its own.
    """
    env_token = os.environ.get(OWNER_TOKEN_ENV)
    if env_token:
        return env_token, SOURCE_ENV
    if not _on_shared_hub():
        return None, SOURCE_PRIVATE_ORIGIN
    if str(os.environ.get(OWNER_ANONYMOUS_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}:
        return None, SOURCE_ANONYMOUS_OPT_OUT
    path = owner_token_path()
    token = read_owner_token_file(path)
    source = SOURCE_FILE
    if not token:
        token = _mint_owner_token(path)
        source = SOURCE_MINTED
    os.environ[OWNER_TOKEN_ENV] = token
    return token, source


def log_owner_token_decision(token: Optional[str], source: str) -> None:
    """One startup line per decision. The minted VALUE is printed exactly once — at the moment it is
    created — because that console is the operator's only way to learn a credential they never
    chose; a reused one names only its file."""
    path = owner_token_path()
    if source == SOURCE_ENV:
        _log.warning(
            "LoopLab UI is on a SHARED JupyterHub origin (jupyter-server-proxy). %s "
            "is a PER-DEPLOYMENT owner secret, NOT per-user identity. It is no longer embedded in "
            "HTML, but a shared origin is still not RBAC: use a private origin or authenticated "
            "reverse proxy for per-user isolation. See docs/guide/deployment.md (Shared JupyterHub).",
            OWNER_TOKEN_ENV)
    elif source == SOURCE_MINTED:
        _log.warning(
            "LoopLab UI is on a SHARED JupyterHub origin with no %s set, so the control plane "
            "FAILS CLOSED: a token was generated and stored at %s (mode 0600). Unlock the UI with "
            "it, or read it back with `cat %s`. Token: %s",
            OWNER_TOKEN_ENV, path, path, token)
    elif source == SOURCE_FILE:
        _log.warning(
            "LoopLab UI is on a SHARED JupyterHub origin with no %s set; the control plane is gated "
            "by the token stored at %s (mode 0600). Read it with `cat %s`.",
            OWNER_TOKEN_ENV, path, path)
    elif source == SOURCE_ANONYMOUS_OPT_OUT:
        _log.warning(
            "LoopLab UI is on a SHARED JupyterHub origin and %s is set: the control plane "
            "(start/delete runs, edit configs, shell-executing experiments) is UNAUTHENTICATED and "
            "reachable by any same-origin page. Unset it to fail closed, and for real isolation "
            "serve each user from a PRIVATE origin. See docs/guide/deployment.md.",
            OWNER_ANONYMOUS_ENV)


__all__ = [
    "OWNER_ANONYMOUS_ENV", "OWNER_TOKEN_ENV", "OWNER_TOKEN_FILE_ENV", "OWNER_TOKEN_SOURCES",
    "SOURCE_ANONYMOUS_OPT_OUT", "SOURCE_ENV", "SOURCE_FILE", "SOURCE_MINTED",
    "SOURCE_PRIVATE_ORIGIN", "log_owner_token_decision", "owner_token_path",
    "read_owner_token_file", "resolve_owner_token",
]
