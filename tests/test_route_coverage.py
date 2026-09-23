"""Every live HTTP route has a test whose request REACHES it (review 2026-09-22, SRV2-12 / doc 50
SR-09).

The review found three routes no test requested and — the defect under them — nothing asserting the
route SET was covered, so a new route landed green. `docs/guide/api-reference.md` (pinned by
`tests/test_api_reference.py`) makes a route land DOCUMENTED; this makes it land EXERCISED.

The set is derived from `make_app(...).routes` and compared with `tests/data/route_coverage.txt`, a
manifest RECORDED by `tests/_route_recorder.py` from the routes requests actually reached — never
from what a test's text mentions (the recorder's docstring has the measurement: a scan of the test
sources was slower on every check and wrong in both directions). Recorded on 2026-09-23 over the
106 test files that reach the app, it found EIGHT live routes no request reached, not three: the
review's `GET .../cards/{card_id}/trace`, `GET .../deletions/{operation_id}` and
`GET .../sessions/{sid}/fork/{action_id}`, plus `GET /api/projects`, `DELETE /api/projects/{pid}`,
the bodyless `DELETE /api/runs/{run_id}` tombstone, `POST /api/cross-run/concept-split-clear`, and
`GET /api/assistant/progress` — which a test DID name, only to assert the token gate's 401, a
refusal answered before any route runs. Each got a driven HTTP test (the happy path and the refusal
it documents) in the same change, so the backlog below is empty and may only stay so.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from tests import _route_recorder as route_recorder  # noqa: E402
from tests._route_recorder import (  # noqa: E402
    MANIFEST, ROOT, RouteRecorder, _ManifestPlugin, format_manifest, live_app_routes,
    merge_manifest, parse_manifest)

# Routes that have NO test whose request reaches them, each a written-down debt. SHRINK-ONLY: a row
# leaves when its route gets a recorded test, and the ratchet below is the size on the day this
# landed — zero — so a new route cannot enter here; it lands with a test or it lands red.
UNTESTED_ROUTES: frozenset[tuple[str, str]] = frozenset()

REGENERATE = ('python -m pytest -p tests._route_recorder '
              '--route-manifest=tests/data/route_coverage.txt -o addopts="" -q <test file>')


@pytest.fixture(scope="module")
def live():
    return live_app_routes()


@pytest.fixture(scope="module")
def manifest():
    return parse_manifest(MANIFEST.read_text(encoding="utf-8"))


def test_every_live_route_has_a_recorded_request(live, manifest):
    """MUTATION: add a route to any router without a test -> red, naming it and the command that
    records the test once it exists."""
    missing = sorted(live - set(manifest) - UNTESTED_ROUTES, key=lambda r: (r[1], r[0]))
    assert not missing, (
        "no test's request reaches these routes. Add an HTTP test that drives the route (its happy "
        f"path and the refusal it documents), then record it: {REGENERATE}\n  "
        + "\n  ".join(f"{method} {path}" for method, path in missing))


def test_the_manifest_names_only_live_routes_and_test_files_that_exist(live, manifest):
    ghosts = sorted(set(manifest) - live)
    assert not ghosts, f"routes the app no longer serves — re-record: {REGENERATE}\n  {ghosts}"
    for route, files in manifest.items():
        assert files, route
        for name in files:
            assert name.startswith("tests/") and name.endswith(".py"), (route, name)
            assert (ROOT / name).is_file(), f"{route} is credited to a gone test file: {name}"


def test_the_untested_backlog_only_shrinks(live, manifest):
    assert UNTESTED_ROUTES <= live, "a backlog row for a route the app no longer serves"
    assert not (UNTESTED_ROUTES & set(manifest)), "a route with a recorded test leaves the backlog"
    assert len(UNTESTED_ROUTES) <= 0, len(UNTESTED_ROUTES)


def test_the_recorder_credits_the_route_a_request_reached_and_nothing_else(tmp_path, monkeypatch):
    """What makes the manifest honest, driven: a request the token gate refuses reaches no route, a
    literal route is credited under its own template and a catch-all under ITS template (dispatch
    order, not URL text), and a 405 credits nothing."""
    from fastapi.testclient import TestClient

    from looplab.serve.server import make_app

    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "sekret")
    token = {"X-LoopLab-Token": "sekret"}
    recorder = RouteRecorder()
    recorder.install()
    try:
        recorder.current_file = "tests/test_example.py"
        client = TestClient(make_app(tmp_path))
        assert client.get("/api/memory").status_code == 401
        assert not recorder.dispatched, "a refused request reached no route"
        assert client.get("/api/memory", headers=token).status_code == 200
        assert client.get("/api/knowledge", headers=token).status_code == 200
        assert client.get("/api/knowledge/x.md", headers=token).status_code == 405   # PUT-only
    finally:
        recorder.uninstall()
    assert dict(recorder.dispatched) == {
        ("GET", "/api/memory"): {"tests/test_example.py"},
        ("GET", "/api/{kind}"): {"tests/test_example.py"},
    }


def test_a_merge_replaces_only_what_a_completely_run_file_dispatched():
    live = {("GET", "/a"), ("GET", "/b"), ("POST", "/c")}
    old = {("GET", "/a"): {"tests/test_one.py", "tests/test_two.py"},
           ("GET", "/b"): {"tests/test_one.py"},
           ("GET", "/gone"): {"tests/test_two.py"}}
    dispatched = {("GET", "/a"): {"tests/test_one.py"},
                  ("POST", "/c"): {"tests/test_one.py", "tests/test_three.py"}}
    assert merge_manifest(old, dispatched, {"tests/test_one.py"}, live) == {
        ("GET", "/a"): {"tests/test_one.py", "tests/test_two.py"},   # two not re-run: kept
        ("POST", "/c"): {"tests/test_one.py"},                       # three ran in part: no credit
    }, "one ran completely and no longer reaches /b, so /b loses its only credit; /gone is not live"


def test_a_file_run_only_in_part_keeps_its_recorded_rows(tmp_path, monkeypatch):
    """The plugin's own bookkeeping, driven through its hooks: `-k` deselection (or a crash that
    skips teardown) makes a file PARTIAL, and a partial file's silence proves nothing."""
    target = tmp_path / "route_coverage.txt"
    target.write_text(format_manifest({("GET", "/api/projects"): {"tests/test_a.py"}}),
                      encoding="utf-8")
    monkeypatch.setattr(route_recorder, "live_app_routes", lambda: {
        ("GET", "/api/projects"), ("DELETE", "/api/projects/{pid}")})
    plugin = _ManifestPlugin(target)
    ran = [SimpleNamespace(nodeid="tests/test_a.py::one"),
           SimpleNamespace(nodeid="tests/test_b.py::t")]
    plugin.pytest_deselected([SimpleNamespace(nodeid="tests/test_a.py::two")])
    plugin.pytest_collection_finish(SimpleNamespace(items=ran))
    for item in ran:
        plugin.pytest_runtest_logreport(SimpleNamespace(when="teardown", nodeid=item.nodeid))
    plugin.recorder.dispatched[("DELETE", "/api/projects/{pid}")] = {
        "tests/test_a.py", "tests/test_b.py"}
    plugin.pytest_sessionfinish(session=None, exitstatus=0)
    assert parse_manifest(target.read_text(encoding="utf-8")) == {
        ("GET", "/api/projects"): {"tests/test_a.py"},
        ("DELETE", "/api/projects/{pid}"): {"tests/test_b.py"},
    }
