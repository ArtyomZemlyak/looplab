"""Record which HTTP route each in-process request REACHED — the collector behind the route-coverage
manifest `tests/data/route_coverage.txt` (review 2026-09-22, SRV2-12 / doc 50 SR-09).

A pytest plugin, inert unless loaded by name. Regenerating the manifest is one command, and it may
be run over any subset of test files — a file that runs COMPLETELY has its old attributions
replaced by what it dispatched this time, and every other file's rows are left exactly as they were:

    python -m pytest -p tests._route_recorder --route-manifest=tests/data/route_coverage.txt \\
        -o addopts="" -q tests/test_projects.py

WHY A RECORDER AND NOT A SCAN OF THE TEST SOURCES — measured on 2026-09-23 over the 79 test files
that use `TestClient` (2,108 tests): the recorder spent 0.33 s recording 3,283 dispatched requests
inside a 906.6 s run, and is exact by construction, because it reads the route Starlette dispatched
to (`scope["route"]`) after the fact. An AST scan of `tests/` for request call sites, resolved
through the app's own router, took 10.8 s to parse 1,115 files (plus 3.3 s to build the app) on
EVERY check and was wrong both ways on the same tree: it missed 5 of the 129 routes the suite really
dispatched (URLs built through a variable, a `(verb, path)` table or `getattr(client, verb)`) and
counted 1 it never did (`GET /api/assistant/progress`, requested only to assert the token gate's
401 — a refusal answered before any route runs).

WHAT COUNTS. `(method, route.path)` of the route the request was dispatched to, when that route
accepts the method. A request a middleware refuses never reaches a route and records nothing; a 405
names a route that does not accept the method and records nothing; a static mount or a test's own
throwaway app is not a live `make_app` route and never reaches the manifest.
"""
from __future__ import annotations

import collections
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tests" / "data" / "route_coverage.txt"
HEADER = (
    "# Every live HTTP route, with the test files whose requests REACHED it — recorded, never\n"
    "# hand-written. Regenerate (any subset of files; see tests/_route_recorder.py):\n"
    "#   python -m pytest -p tests._route_recorder --route-manifest=tests/data/route_coverage.txt"
    " -o addopts=\"\" -q <files>\n"
    "# Checked by tests/test_route_coverage.py against make_app(...).routes.\n"
)

Route = tuple[str, str]


def live_routes(app) -> set[Route]:
    """Every `(method, path template)` the app serves, derived from `app.routes`.

    FastAPI 0.13x leaves each `include_router` as an `_IncludedRouter` placeholder, so a flat
    `app.routes` shows one entry per ROUTER, not per route; the walk descends into
    `original_router`."""
    from fastapi.routing import APIRoute

    out: set[Route] = set()

    def walk(routes):
        for route in routes:
            if isinstance(route, APIRoute):
                out.update((method, route.path) for method in route.methods)
            elif hasattr(route, "original_router"):
                walk(route.original_router.routes)

    walk(app.routes)
    return out


def live_app_routes() -> set[Route]:
    from looplab.serve.server import make_app

    with tempfile.TemporaryDirectory() as root:
        return live_routes(make_app(Path(root)))


def parse_manifest(text: str) -> dict[Route, set[str]]:
    rows: dict[Route, set[str]] = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        route, _tab, files = line.partition("\t")
        method, _space, path = route.partition(" ")
        rows[(method, path)] = set(files.split())
    return rows


def format_manifest(rows: dict[Route, set[str]]) -> str:
    ordered = sorted(rows.items(), key=lambda row: (row[0][1], row[0][0]))    # by path, then method
    body = "".join(f"{method} {path}\t{' '.join(sorted(files))}\n"
                   for (method, path), files in ordered if files)
    return HEADER + body


def merge_manifest(old: dict[Route, set[str]], dispatched: dict[Route, set[str]],
                   complete_files: set[str], live: set[Route]) -> dict[Route, set[str]]:
    """The manifest after a session: every COMPLETELY-run file's attributions are replaced by what
    it dispatched now; a file the session ran only in part (or not at all) keeps its old rows, since
    its absence proves nothing; a route that is no longer live is dropped."""
    merged = {route: {f for f in files if f not in complete_files}
              for route, files in old.items() if route in live}
    for route, files in dispatched.items():
        if route in live:
            merged.setdefault(route, set()).update(f for f in files if f in complete_files)
    return {route: files for route, files in merged.items() if files}


class RouteRecorder:
    """Wraps `FastAPI.__call__` at CLASS level, so every app a test builds is seen, whatever client
    drove it (`TestClient`, `httpx.ASGITransport`, a raw ASGI call)."""

    def __init__(self) -> None:
        self.current_file = "<unattributed>"
        self.dispatched: dict[Route, set[str]] = collections.defaultdict(set)
        self._original = None

    def install(self) -> None:
        import fastapi

        original = fastapi.FastAPI.__call__
        recorder = self

        async def _recording_call(app, scope, receive, send):
            try:
                await original(app, scope, receive, send)
            finally:
                if scope.get("type") == "http":
                    route = scope.get("route")
                    method = scope.get("method")
                    path = getattr(route, "path", None)
                    if path is not None and method in (getattr(route, "methods", None) or ()):
                        recorder.dispatched[(method, path)].add(recorder.current_file)

        self._original = original
        fastapi.FastAPI.__call__ = _recording_call

    def uninstall(self) -> None:
        import fastapi

        if self._original is not None:
            fastapi.FastAPI.__call__ = self._original
            self._original = None


def _test_file(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


class _ManifestPlugin:
    def __init__(self, target: Path) -> None:
        self.target = target
        self.recorder = RouteRecorder()
        self.collected: collections.Counter = collections.Counter()
        self.finished: collections.Counter = collections.Counter()
        self.partial: set[str] = set()

    def pytest_deselected(self, items):
        self.partial.update(_test_file(item.nodeid) for item in items)

    def pytest_collection_finish(self, session):
        self.collected.update(_test_file(item.nodeid) for item in session.items)

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item):
        self.recorder.current_file = _test_file(item.nodeid)

    def pytest_runtest_logreport(self, report):
        if report.when == "teardown":
            self.finished[_test_file(report.nodeid)] += 1

    def pytest_sessionstart(self, session):
        self.recorder.install()

    def pytest_sessionfinish(self, session, exitstatus):
        self.recorder.uninstall()
        complete = {f for f, n in self.collected.items()
                    if f not in self.partial and self.finished[f] == n}
        old = (parse_manifest(self.target.read_text(encoding="utf-8"))
               if self.target.exists() else {})
        merged = merge_manifest(old, self.recorder.dispatched, complete, live_app_routes())
        self.target.write_text(format_manifest(merged), encoding="utf-8")


def pytest_addoption(parser):
    parser.addoption("--route-manifest", default=None,
                     help="merge the routes this session's requests reached into this manifest")


def pytest_configure(config):
    target = config.getoption("--route-manifest")
    if target:
        config.pluginmanager.register(_ManifestPlugin(Path(target)), "route-manifest-recorder")
