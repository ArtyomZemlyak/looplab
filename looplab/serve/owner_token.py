"""Optional owner login for the UI control plane.

Quick start does not mint or require an owner token, including JupyterHub/proxy and
non-loopback deployments. A supplied LOOPLAB_UI_TOKEN always enables owner login.
LOOPLAB_UI_REQUIRE_AUTH=1 explicitly enables login and mints/reuses a private token
when no value was supplied. LOOPLAB_UI_ANONYMOUS=1 remains an explicit opt-out from
automatic minting; it never overrides an operator-supplied token.

An open control plane gives every caller reaching it owner access. For a shared or
public deployment, enable auth/Origin checks or use an authenticated reverse proxy.
The credential file remains under ~/.looplab, outside the run root.
"""
from __future__ import annotations

import errno
import ipaddress
import logging
import os
import secrets
import stat
from pathlib import Path
from typing import Optional

from looplab.core.atomicio import same_file_entry
from looplab.core.errors import EnvironmentRefusal
from looplab.core.pathsafe import is_reparse
from looplab.serve.engine_proc import _on_shared_hub

_log = logging.getLogger("looplab.server")

OWNER_TOKEN_ENV = "LOOPLAB_UI_TOKEN"
OWNER_TOKEN_FILE_ENV = "LOOPLAB_UI_TOKEN_FILE"
OWNER_ANONYMOUS_ENV = "LOOPLAB_UI_ANONYMOUS"
OWNER_REQUIRE_AUTH_ENV = "LOOPLAB_UI_REQUIRE_AUTH"

# Sources, in the order `resolve_owner_token` decides them. The string is what gets logged, and the
# set is closed so a caller can branch on it instead of on a message.
SOURCE_ENV = "env"                    # the operator exported LOOPLAB_UI_TOKEN
SOURCE_FILE = "file"                  # a token minted by an earlier start, reused
SOURCE_MINTED = "minted"              # minted by THIS start
SOURCE_PRIVATE_ORIGIN = "private"     # no token, and no shared origin was detected
SOURCE_ANONYMOUS_DEFAULT = "open"       # no login requested
SOURCE_ANONYMOUS_OPT_OUT = "anonymous"  # no token, shared origin, operator opted out explicitly
OWNER_TOKEN_SOURCES = frozenset({
    SOURCE_ENV, SOURCE_FILE, SOURCE_MINTED, SOURCE_PRIVATE_ORIGIN, SOURCE_ANONYMOUS_DEFAULT, SOURCE_ANONYMOUS_OPT_OUT})

_TOKEN_BYTES = 32

# Hostnames that are the loopback interface under any resolver worth trusting. Anything else that is
# not a literal loopback IP is treated as PUBLISHED — a name this process cannot resolve to an
# interface is not evidence of privacy. This classification controls exposure diagnostics;
# authentication is enabled separately by the operator.
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})


def _is_loopback_bind(bind_host: Optional[str]) -> bool:
    """Does this bind address keep the server on an origin only this box can reach?

    `None` means the caller did not say — every embedded `make_app(...)` (the test suite, an in-
    process ASGI mount) — and is read as loopback, which is byte-for-byte the behaviour those callers
    had before a bind host existed here. An EMPTY string is not the same thing: it is what a socket
    bind reads as "all interfaces", i.e. the same exposure as `0.0.0.0`, so it is published.
    """
    if bind_host is None:
        return True
    host = str(bind_host).strip()
    if not host:
        return False                      # "" == every interface
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]                 # a bracketed IPv6 literal
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A hostname this module cannot classify. `0.0.0.0`/`::` land here as `is_loopback` False
        # anyway; an unresolvable name fails closed for the reason above.
        return False


def on_shared_origin(bind_host: Optional[str] = None) -> bool:
    """Is the control plane published on an origin this deployment does not own?

    TWO witnesses, either of which is sufficient, and they are genuinely different exposures: the
    JupyterHub single-user origin (loopback bind, republished by jupyter-server-proxy onto a host
    every other user's pages also live on) and a non-loopback BIND (published directly to whatever
    can route to this box). The first cannot be seen from the bind address and the second cannot be
    seen from the environment, so neither can stand in for the other.
    """
    return _on_shared_hub() or not _is_loopback_bind(bind_host)


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
    # THE ENTRY IS JUDGED BEFORE THE OPEN, because the open's own guard is POSIX-only: `O_NOFOLLOW`
    # does not exist on Windows (`getattr` answers 0), so there `os.open` FOLLOWS a planted link
    # and the fstat below describes the link's TARGET — a file someone else chose. The fstat must
    # then be the SAME entry, which also closes the swap between the two calls on both platforms
    # (review 2026-09-22, WIN-TOKEN).
    try:
        entry = os.lstat(target)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if is_reparse(entry):
        _refuse_unsafe(target, "is a symbolic link")
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
        if (not stat.S_ISREG(info.st_mode) or is_reparse(info)
                or same_file_entry(info) != same_file_entry(entry)):
            _refuse_unsafe(target, "is not a private regular file")
        # POSIX MODE BITS ARE PRIVACY EVIDENCE ONLY WHERE THEY EXIST. On Windows `st_mode` is
        # synthesized from the read-only attribute alone — 0666 or 0444 for EVERY file, whatever
        # its ACL — so this test refused the very token `_mint_owner_token` had just written: the
        # second `looplab ui` on a shared origin could never start, and `looplab tui` could never
        # read the credential (measured on the Windows CI leg, GitHub Actions run 35785582444:
        # every shared-origin test an `EnvironmentRefusal` "mode 0666"). A test that refuses every
        # file discriminates nothing. There the file's privacy is the ACL of the profile directory
        # `owner_token_path()` puts it under, which is private to its user by default.
        if os.name != "nt" and info.st_mode & 0o077:
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


def resolve_owner_token(bind_host: Optional[str] = None) -> tuple[Optional[str], str]:
    """Return `(token, source)` — the owner credential this server will enforce, and where it is from.

    `bind_host` is the address this server is being published on (`serve(host=…)`), or None when the
    caller is embedding the app and there is no bind — see `_is_loopback_bind`. It is what makes
    diagnostics classify a private/shared deployment; authentication itself is now opt-in.

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
    if str(os.environ.get(OWNER_ANONYMOUS_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}:
        return None, SOURCE_ANONYMOUS_OPT_OUT
    if str(os.environ.get(OWNER_REQUIRE_AUTH_ENV, "")).strip().lower() not in {"1", "true", "yes", "on"}:
        return None, SOURCE_ANONYMOUS_DEFAULT if on_shared_origin(bind_host) else SOURCE_PRIVATE_ORIGIN
    path = owner_token_path()
    token = read_owner_token_file(path)
    source = SOURCE_FILE
    if not token:
        token = _mint_owner_token(path)
        source = SOURCE_MINTED
    os.environ[OWNER_TOKEN_ENV] = token
    return token, source


def _origin_phrase(bind_host: Optional[str]) -> str:
    """WHICH exposure this decision is about. The two witnesses are different deployments and the
    operator's next move differs, so the line has to name the one that actually fired rather than
    telling a `--host 0.0.0.0` operator about jupyter-server-proxy."""
    if not on_shared_origin(bind_host):
        return "a private/local bind"
    if _on_shared_hub():
        return "a SHARED JupyterHub origin (jupyter-server-proxy)"
    return (f"a PUBLISHED (non-loopback) bind address {str(bind_host or '').strip() or '0.0.0.0'}, "
            f"reachable by anything that can route to this host")


def log_owner_token_decision(token: Optional[str], source: str,
                             bind_host: Optional[str] = None) -> None:
    """One startup line per decision. The minted VALUE is printed exactly once — at the moment it is
    created — because that console is the operator's only way to learn a credential they never
    chose; a reused one names only its file."""
    path = owner_token_path()
    origin = _origin_phrase(bind_host)
    if source == SOURCE_ENV:
        _log.warning(
            "LoopLab UI is on %s. %s "
            "is a PER-DEPLOYMENT owner secret, NOT per-user identity. It is no longer embedded in "
            "HTML, but a shared origin is still not RBAC: use a private origin or authenticated "
            "reverse proxy for per-user isolation. See docs/guide/deployment.md (Shared JupyterHub).",
            origin, OWNER_TOKEN_ENV)
    elif source == SOURCE_MINTED:
        _log.warning(
            "LoopLab UI is on %s with no %s set, so the control plane "
            "FAILS CLOSED: a token was generated and stored at %s (mode 0600). Unlock the UI with "
            "it, or read it back with `cat %s`. Token: %s",
            origin, OWNER_TOKEN_ENV, path, path, token)
    elif source == SOURCE_FILE:
        _log.warning(
            "LoopLab UI is on %s with no %s set; the control plane is gated "
            "by the token stored at %s (mode 0600). Read it with `cat %s`.",
            origin, OWNER_TOKEN_ENV, path, path)
    elif source == SOURCE_ANONYMOUS_DEFAULT:
        _log.info("LoopLab UI login is disabled by default on %s; enable %s=1 to require a token.",
                  origin, OWNER_REQUIRE_AUTH_ENV)
    elif source == SOURCE_ANONYMOUS_OPT_OUT:
        _log.warning(
            "LoopLab UI is on %s and %s is set: the control plane "
            "(start/delete runs, edit configs, shell-executing experiments) is UNAUTHENTICATED and "
            "reachable by any same-origin page. Unset LOOPLAB_UI_ANONYMOUS and enable LOOPLAB_UI_REQUIRE_AUTH=1 to require login; for real isolation "
            "serve each user from a PRIVATE origin. See docs/guide/deployment.md.",
            origin, OWNER_ANONYMOUS_ENV)


__all__ = [
    "OWNER_ANONYMOUS_ENV", "OWNER_REQUIRE_AUTH_ENV", "OWNER_TOKEN_ENV", "OWNER_TOKEN_FILE_ENV", "OWNER_TOKEN_SOURCES",
    "SOURCE_ANONYMOUS_OPT_OUT", "SOURCE_ENV", "SOURCE_FILE", "SOURCE_MINTED",
    "SOURCE_PRIVATE_ORIGIN", "SOURCE_ANONYMOUS_DEFAULT", "log_owner_token_decision", "on_shared_origin", "owner_token_path",
    "read_owner_token_file", "resolve_owner_token",
]
