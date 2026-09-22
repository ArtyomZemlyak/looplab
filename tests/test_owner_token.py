"""What an UNSET `LOOPLAB_UI_TOKEN` means, driven with real requests.

The token middleware has been in the tree for a long time; what was open is the DEFAULT. Unset meant
unauthenticated everywhere, including on the shared JupyterHub origin the server itself detects and
warns about — where jupyter-server-proxy puts every user's app on ONE browser origin and any
same-origin page could therefore drive the control plane (start/delete runs, edit settings, name a
`task_file`). `serve/owner_token.py` splits that one answer in two: a private origin stays open
byte-for-byte, a shared origin fails closed by minting a credential.

Every assertion below goes through `make_app` + a real request, because the property is "the request
is refused", not "the module computed a string".
"""
from __future__ import annotations

import os
import stat

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.core.errors import OperatorRefusal  # noqa: E402
from looplab.serve import owner_token  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402
from _posix_gates import MODE_BITS  # noqa: E402


def _hub(monkeypatch):
    monkeypatch.setenv("JUPYTERHUB_SERVICE_PREFIX", "/user/alice/")


def test_private_origin_without_a_token_stays_open(tmp_path, monkeypatch):
    """The historical local single-user path. A fresh `pip install` + `looplab ui` must not acquire
    an unlock gate: there is no shared origin, and paying that cost would buy nothing."""
    client = TestClient(make_app(tmp_path))

    assert client.get("/api/runs").status_code == 200
    assert owner_token.resolve_owner_token() == (None, owner_token.SOURCE_PRIVATE_ORIGIN)
    assert not owner_token.owner_token_path().exists()


def test_shared_hub_without_a_token_mints_one_and_denies_by_default(tmp_path, monkeypatch):
    """The defect, closed: on the shared origin an unset token no longer means anonymous."""
    _hub(monkeypatch)
    client = TestClient(make_app(tmp_path))

    assert client.get("/api/runs").status_code == 401
    minted = owner_token.read_owner_token_file()
    assert minted, "a shared-hub server must fail closed with a credential the operator can read"
    assert client.get("/api/runs", headers={"X-LoopLab-Token": minted}).status_code == 200
    # The liveness probe stays open — a monitor must not need the owner credential.
    assert client.get("/api/health").status_code == 200


def test_a_minted_token_is_reused_by_the_next_start(tmp_path, monkeypatch):
    """A credential regenerated on every restart is one the operator cannot keep, and two workers on
    one box that disagree about it 401 the tab that unlocked against the other."""
    _hub(monkeypatch)
    make_app(tmp_path)
    path = owner_token.owner_token_path()
    first = path.read_text(encoding="utf-8").strip()

    monkeypatch.delenv(owner_token.OWNER_TOKEN_ENV, raising=False)   # a fresh process, same box
    make_app(tmp_path / "second-root")

    assert owner_token.read_owner_token_file() == first


@MODE_BITS
def test_a_minted_token_is_mode_0600(tmp_path, monkeypatch):
    _hub(monkeypatch)
    make_app(tmp_path)
    assert stat.S_IMODE(owner_token.owner_token_path().stat().st_mode) == 0o600


def test_a_windows_token_file_is_not_refused_for_mode_bits_windows_cannot_express(
        tmp_path, monkeypatch):
    """On Windows `st_mode` reads 0666 for EVERY writable file, whatever its ACL, so the POSIX
    "readable by others" test refused the token the server had just minted and the second start on
    a shared origin could never come up (review 2026-09-22, WIN-TOKEN; six red tests on the Windows
    CI leg, each "mode 0666"). Driven here with that platform's answer: the file carries 0666 and
    the read happens with `os.name == "nt"` — it must return the token, not refuse it."""
    path = tmp_path / "ui-token"
    path.write_text("minted-token", encoding="utf-8")
    os.chmod(path, 0o666)
    with monkeypatch.context() as m:
        m.setattr(os, "name", "nt")
        token = owner_token.read_owner_token_file(path)
    assert token == "minted-token"


def test_a_symlinked_token_is_refused_where_open_cannot_refuse_links(tmp_path, monkeypatch):
    """Windows has no `O_NOFOLLOW`, so there the open FOLLOWS a planted link and reads a file
    somebody else chose. The entry must be judged before the open. Driven with the flag taken away
    and the target made private enough to pass every later check, so only the link rule can
    refuse it."""
    elsewhere = tmp_path / "somebody-elses-file"
    elsewhere.write_text("not-the-token", encoding="utf-8")
    os.chmod(elsewhere, 0o600)
    path = tmp_path / "ui-token"
    path.symlink_to(elsewhere)
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    with pytest.raises(OperatorRefusal, match="symbolic link"):
        owner_token.read_owner_token_file(path)


def test_an_exported_token_still_wins_over_the_file(tmp_path, monkeypatch):
    _hub(monkeypatch)
    monkeypatch.setenv(owner_token.OWNER_TOKEN_ENV, "operator-chose-this")
    client = TestClient(make_app(tmp_path))

    assert client.get("/api/runs", headers={"X-LoopLab-Token": "operator-chose-this"}).status_code == 200
    assert client.get("/api/runs").status_code == 401
    assert not owner_token.owner_token_path().exists()   # nothing was minted over the operator


def test_the_anonymous_opt_out_is_explicit_and_restores_the_open_plane(tmp_path, monkeypatch):
    """The escape hatch for an operator whose origin is private in a way the detection cannot see.
    It must be something they turn ON — never the effect of forgetting a variable."""
    _hub(monkeypatch)
    monkeypatch.setenv(owner_token.OWNER_ANONYMOUS_ENV, "1")
    client = TestClient(make_app(tmp_path))

    assert client.get("/api/runs").status_code == 200
    assert not owner_token.owner_token_path().exists()


@MODE_BITS
def test_a_world_readable_token_file_is_a_typed_refusal(tmp_path, monkeypatch):
    """A credential the box has already published is not one to keep serving. It is an
    `OperatorRefusal`, so `looplab ui` prints one line at exit 2 instead of 42 frames."""
    _hub(monkeypatch)
    path = owner_token.owner_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("leaked-token", encoding="utf-8")
    os.chmod(path, 0o644)

    with pytest.raises(OperatorRefusal) as exc:
        make_app(tmp_path)
    assert str(path) in str(exc.value)


def test_a_symlinked_token_file_is_refused_rather_than_followed(tmp_path, monkeypatch):
    """The same rule the task-file reader keeps: a credential is read through `O_NOFOLLOW`, so a
    planted link cannot redirect the read (or, on the write path, the write)."""
    _hub(monkeypatch)
    path = owner_token.owner_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    elsewhere = tmp_path / "somebody-elses-file"
    elsewhere.write_text("not-the-token", encoding="utf-8")
    path.symlink_to(elsewhere)

    with pytest.raises(OperatorRefusal):
        make_app(tmp_path)


def test_the_tui_client_finds_the_minted_token(tmp_path, monkeypatch):
    """`looplab tui` talks to the same server as the same OS user. A fail-closed default that the
    shipped client cannot satisfy is an outage, not a boundary."""
    _hub(monkeypatch)
    make_app(tmp_path)
    minted = owner_token.read_owner_token_file()
    monkeypatch.delenv(owner_token.OWNER_TOKEN_ENV, raising=False)

    from looplab.serve.tui_api import Api

    assert Api("http://127.0.0.1:8765").token == minted


def test_the_startup_line_names_the_file_and_prints_a_minted_token_once(tmp_path, monkeypatch, caplog):
    """The operator never chose this credential, so the console is their only way to learn it — and
    the file path is what makes it recoverable after the log scrolls away."""
    _hub(monkeypatch)
    with caplog.at_level("WARNING", logger="looplab.server"):
        make_app(tmp_path)
    minted = owner_token.read_owner_token_file()
    text = caplog.text

    assert minted and minted in text
    assert str(owner_token.owner_token_path()) in text


# ------------------------------------------------------------------ the OTHER shared origin
def test_a_published_bind_fails_closed_exactly_like_the_hub(tmp_path, monkeypatch):
    """`looplab ui --host 0.0.0.0` publishes the control plane — start/delete runs, edit settings,
    shell-executing experiments — to everything that can route to the box.

    The fail-closed decision was keyed on two JupyterHub env variables alone, so this invocation
    answered `private` and served all of it unauthenticated; the module's own argument for that
    ("it already binds loopback, and that is the exposed configuration") is true of the hub and
    simply false here. The property is "published on an origin this deployment does not own", and
    the bind host is the second witness of it.
    """
    client = TestClient(make_app(tmp_path, bind_host="0.0.0.0"))

    assert client.get("/api/runs").status_code == 401
    minted = owner_token.read_owner_token_file()
    assert minted
    assert client.get("/api/runs", headers={"X-LoopLab-Token": minted}).status_code == 200
    assert client.get("/api/health").status_code == 200


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.1.5", "::1", "[::1]", "localhost", None])
def test_a_private_bind_is_still_open_and_mints_nothing(tmp_path, host, monkeypatch):
    """The historical local single-user path, and the embedded one: `make_app(root)` with no bind
    claims no exposure and must behave byte-for-byte as before."""
    client = TestClient(make_app(tmp_path, bind_host=host))

    assert client.get("/api/runs").status_code == 200
    assert owner_token.resolve_owner_token(host) == (None, owner_token.SOURCE_PRIVATE_ORIGIN)
    assert not owner_token.owner_token_path().exists()


@pytest.mark.parametrize("host", ["0.0.0.0", "", "::", "10.1.2.3", "looplab.example.com"])
def test_every_non_loopback_bind_is_treated_as_published(host):
    """Including the empty string (a socket bind reads it as every interface) and a hostname this
    process cannot classify — the fail-closed direction is the whole point."""
    assert owner_token.on_shared_origin(host) is True
    assert owner_token.on_shared_origin("127.0.0.1") is False


def test_the_anonymous_opt_out_still_works_on_a_published_bind(tmp_path, monkeypatch, caplog):
    """The escape hatch is unchanged and stays EXPLICIT and logged — and the line must name the
    exposure that actually fired, not tell a `--host 0.0.0.0` operator about jupyter-server-proxy."""
    monkeypatch.setenv(owner_token.OWNER_ANONYMOUS_ENV, "1")
    with caplog.at_level("WARNING", logger="looplab.server"):
        client = TestClient(make_app(tmp_path, bind_host="0.0.0.0"))

    assert client.get("/api/runs").status_code == 200
    assert not owner_token.owner_token_path().exists()
    assert "UNAUTHENTICATED" in caplog.text and "0.0.0.0" in caplog.text
    assert "JupyterHub" not in caplog.text


def test_the_published_bind_decision_is_LOGGED_with_its_minted_token(tmp_path, monkeypatch, caplog):
    """A server that fails closed and says nothing is one nobody can unlock: the minted value is
    printed exactly once, at the moment it is created, and the branch that decides whether to log
    must be the SAME predicate that decided to mint."""
    with caplog.at_level("WARNING", logger="looplab.server"):
        make_app(tmp_path, bind_host="0.0.0.0")

    minted = owner_token.read_owner_token_file()
    assert minted and minted in caplog.text
    assert str(owner_token.owner_token_path()) in caplog.text


def test_serve_hands_the_bind_host_to_the_app(monkeypatch, tmp_path):
    """The value has to actually TRAVEL: `looplab ui --host` -> `serve(host=…)` -> `make_app`."""
    from looplab.serve import server as server_mod

    seen = {}
    monkeypatch.setattr(server_mod, "make_app", lambda root, **kw: seen.update(root=root, **kw))
    monkeypatch.setitem(__import__("sys").modules, "uvicorn", type("U", (), {
        "run": staticmethod(lambda app, **kw: seen.update(uvicorn_host=kw.get("host")))})())

    server_mod.serve(tmp_path, host="0.0.0.0", port=1)

    assert seen["bind_host"] == "0.0.0.0" and seen["uvicorn_host"] == "0.0.0.0"
