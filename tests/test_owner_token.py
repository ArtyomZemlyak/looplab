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


def test_a_minted_token_is_private_and_reused_by_the_next_start(tmp_path, monkeypatch):
    """A credential regenerated on every restart is one the operator cannot keep, and two workers on
    one box that disagree about it 401 the tab that unlocked against the other."""
    _hub(monkeypatch)
    make_app(tmp_path)
    path = owner_token.owner_token_path()
    first = path.read_text(encoding="utf-8").strip()

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    monkeypatch.delenv(owner_token.OWNER_TOKEN_ENV, raising=False)   # a fresh process, same box
    make_app(tmp_path / "second-root")

    assert owner_token.read_owner_token_file() == first


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
