"""The HTTP API reference, GENERATED from the app's own OpenAPI schema (doc 52 row 25).

`docs/guide/api-reference.md` is written by this module and pinned by `tests/test_api_reference.py`
against `make_app(...).openapi()`: a route that lands without a regenerated page is a red test, so
the surface cannot grow undocumented. Rows carry `(method, path, deprecated)` — the triple the review
asked to pin — plus the route's summary (the handler's docstring, first line) and its declared
response model, grouped by path prefix. Nothing here is hand-written; edit the handlers' docstrings.

    python -m looplab.serve.api_reference            # rewrite docs/guide/api-reference.md
    python -m looplab.serve.api_reference --check    # exit 1 when the page is stale
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

PAGE = Path(__file__).resolve().parents[2] / "docs" / "guide" / "api-reference.md"
METHODS = ("get", "post", "put", "delete", "patch")
BEGIN, END = "<!-- generated: api routes -->", "<!-- /generated -->"


def route_rows(spec: dict) -> list[dict]:
    """`[{method, path, summary, deprecated, model}, …]` in path order, then method order."""
    rows = []
    for path, ops in sorted((spec.get("paths") or {}).items()):
        for method in METHODS:
            op = ops.get(method)
            if not isinstance(op, dict):
                continue
            ok = ((op.get("responses") or {}).get("200") or {}).get("content") or {}
            schema = ((ok.get("application/json") or {}).get("schema") or {})
            ref = schema.get("$ref") or ""
            # The handler's DOCSTRING first line when it has one; FastAPI's auto-summary (the
            # function name, title-cased) only for a handler that says nothing about itself, which
            # the page then shows as such — the cure is a docstring on the handler, not an edit here.
            described = (op.get("description") or "").strip().split("\n")[0].strip()
            summary = described or f"*{(op.get('summary') or '').strip()}* (no docstring)"
            rows.append({"method": method.upper(), "path": path, "summary": summary,
                         "deprecated": bool(op.get("deprecated")), "model": ref.rsplit("/", 1)[-1]})
    return rows


def _group(path: str) -> str:
    """The first two concrete segments — `/api/runs`, `/api/memory` … — so the page reads by area."""
    parts = [p for p in path.split("/") if p and not p.startswith("{")]
    return "/" + "/".join(parts[:2]) if parts else "/"


def render_routes(rows: list[dict]) -> str:
    out = [BEGIN, ""]
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(_group(row["path"]), []).append(row)
    out.append(f"{len(rows)} routes on {len({r['path'] for r in rows})} paths; "
               f"{sum(1 for r in rows if r['deprecated'])} deprecated; "
               f"{sum(1 for r in rows if r['model'])} with a declared response model.")
    out.append("")
    for group, members in sorted(groups.items()):
        out.append(f"### `{group}`")
        out.append("")
        out.append("| method | path | summary | response model | deprecated |")
        out.append("|---|---|---|---|---|")
        for r in members:
            summary = r["summary"].replace("|", "\\|")
            out.append(f"| `{r['method']}` | `{r['path']}` | {summary} | "
                       f"{('`' + r['model'] + '`') if r['model'] else '—'} | {'yes' if r['deprecated'] else ''} |")
        out.append("")
    out.append(END)
    return "\n".join(out)


def render_page(spec: dict) -> str:
    head = """# HTTP API reference

**Generated** from the server's own OpenAPI schema by `python -m looplab.serve.api_reference` and
pinned by `tests/test_api_reference.py` (doc 52 row 25): a route that lands without a regenerated
page is a red test, so the surface cannot grow undocumented. Every row is `(method, path,
deprecated)` plus the handler's docstring first line and its declared response model — edit the
handler, not this page. The live schema is served at `/openapi.json`; the interactive form at
`/docs`. Refusal codes are `serve/http.py::REFUSALS` (`docs/guide/ui.md`), and the control
vocabulary a client may append is `serve/protocol.py::CONTROL_EVENTS`.

"""
    return head + render_routes(route_rows(spec)) + "\n"


def current_spec() -> dict:
    from looplab.serve.server import make_app

    return make_app(Path(tempfile.mkdtemp(prefix="looplab-api-ref-"))).openapi()


def generated_block(text: str) -> str:
    m = re.search(re.escape(BEGIN) + r".*?" + re.escape(END), text, re.S)
    return m.group(0) if m else ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m looplab.serve.api_reference")
    parser.add_argument("--check", action="store_true", help="exit 1 when the page is stale")
    args = parser.parse_args(argv)
    page = render_page(current_spec())
    if args.check:
        stale = not PAGE.is_file() or generated_block(PAGE.read_text(encoding="utf-8")) != generated_block(page)
        print("stale" if stale else "current")
        return 1 if stale else 0
    PAGE.write_text(page, encoding="utf-8")
    print(f"wrote {PAGE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
