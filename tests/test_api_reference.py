"""The HTTP API reference is GENERATED from the app's own schema and pinned (doc 52 row 25).

The review counted 140 routes, 22 with a response model, 110 templates named in no guide page, and
nothing asserting the route SET was covered — so a route landed green and undocumented. The page
`docs/guide/api-reference.md` is what `python -m looplab.serve.api_reference` writes, and these
tests compare the checked-in page against the LIVE `make_app(...).openapi()`: a new, renamed or
deprecated route is a red test until the page is regenerated. Compared, not grepped — every row
below is derived from the schema.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from looplab.serve.api_reference import (BEGIN, END, PAGE, current_spec, generated_block,  # noqa: E402
                                         render_routes, route_rows)


@pytest.fixture(scope="module")
def spec():
    return current_spec()


def test_the_page_is_what_the_generator_writes_for_the_live_app(spec):
    assert PAGE.is_file(), "docs/guide/api-reference.md is missing — run `python -m looplab.serve.api_reference`"
    page = PAGE.read_text(encoding="utf-8")
    assert BEGIN in page and END in page
    assert generated_block(page) == render_routes(route_rows(spec)), (
        "docs/guide/api-reference.md is stale against the live OpenAPI schema — a route was added, "
        "renamed, deprecated or re-documented; run `python -m looplab.serve.api_reference`")


def test_the_route_triple_set_is_pinned_by_the_page(spec):
    """`(method, path, deprecated)` for every route, read back off the page's own table rows and
    compared with the schema — the triple the review asked to pin, independent of the prose."""
    page = PAGE.read_text(encoding="utf-8")
    rows = re.findall(r"^\| `([A-Z]+)` \| `([^`]+)` \| .*? \| (?:`[^`]*`|—) \| (yes|) \|$", page, re.M)
    documented = {(m, p, d == "yes") for m, p, d in rows}
    live = {(r["method"], r["path"], r["deprecated"]) for r in route_rows(spec)}
    assert documented == live, {"undocumented": sorted(live - documented), "ghost": sorted(documented - live)}
    assert len(live) >= 130, "the surface shrank below the review's count — a router fell off the app"


def test_the_generator_prefers_the_handlers_own_docstring():
    spec = {"paths": {"/x": {"get": {"summary": "Read X", "description": "Return the bounded X.\nMore."},
                             "post": {"summary": "Write X", "deprecated": True,
                                      "responses": {"200": {"content": {"application/json": {
                                          "schema": {"$ref": "#/components/schemas/XOut"}}}}}}}}}
    rows = route_rows(spec)
    assert rows[0]["summary"] == "Return the bounded X." and rows[0]["model"] == ""
    assert rows[1]["summary"] == "*Write X* (no docstring)" and rows[1]["deprecated"] is True
    assert rows[1]["model"] == "XOut"
    block = render_routes(rows)
    assert block.startswith(BEGIN) and block.endswith(END) and "| `POST` | `/x` |" in block and "| yes |" in block
