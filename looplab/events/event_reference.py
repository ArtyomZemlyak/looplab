"""The event reference, GENERATED from the payload contract (doc 52 row 30).

`docs/guide/event-reference.md` is written by this module and pinned by
`tests/test_event_payload_contract.py` against `types.py::EVENT_PAYLOAD_KEYS`: an event type that
lands without a regenerated page is a red test, so the run log's vocabulary cannot grow
undocumented — the state ten of the types were in until this page existed. Nothing here is
hand-written; edit the contract row.

    python -m looplab.events.event_reference            # rewrite docs/guide/event-reference.md
    python -m looplab.events.event_reference --check    # exit 1 when the page is stale
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from looplab.events.types import ALL_EVENT_TYPES, DIAGNOSTIC_EVENTS, EVENT_PAYLOAD_KEYS

PAGE = Path(__file__).resolve().parents[2] / "docs" / "guide" / "event-reference.md"
BEGIN, END = "<!-- generated: event types -->", "<!-- /generated -->"


def rows() -> list[dict]:
    """`[{type, summary, folded, stored_whole, required, optional}, …]` in type order."""
    out = []
    for etype in sorted(ALL_EVENT_TYPES):
        contract = EVENT_PAYLOAD_KEYS[etype]
        out.append({"type": etype, "summary": contract.summary,
                    "folded": etype not in DIAGNOSTIC_EVENTS,
                    "stored_whole": contract.stored_whole,
                    "required": list(contract.required), "optional": list(contract.optional)})
    return out


def _keys(names: list[str]) -> str:
    return ", ".join(f"`{n}`" for n in names) if names else "—"


def render_rows(table: list[dict]) -> str:
    folded = sum(1 for r in table if r["folded"])
    whole = sum(1 for r in table if r["stored_whole"])
    keys = sum(len(r["required"]) + len(r["optional"]) for r in table)
    out = [BEGIN, ""]
    out.append(f"{len(table)} event types — {folded} folded into `RunState`, {len(table) - folded} "
               f"diagnostic; {keys} declared payload keys; {whole} types whose whole payload is "
               f"stored by the fold.")
    out.append("")
    out.append("| type | fold | records | required keys | optional keys |")
    out.append("|---|---|---|---|---|")
    for r in table:
        fold = "folded" if r["folded"] else "diagnostic"
        if r["stored_whole"]:
            fold += " · whole"
        summary = r["summary"].replace("|", "\\|")
        out.append(f"| `{r['type']}` | {fold} | {summary} | {_keys(r['required'])} | "
                   f"{_keys(r['optional'])} |")
    out.append("")
    out.append(END)
    return "\n".join(out)


def render_page() -> str:
    head = """# Event reference

**Generated** from `looplab/events/types.py::EVENT_PAYLOAD_KEYS` by
`python -m looplab.events.event_reference` and pinned by `tests/test_event_payload_contract.py`
(doc 52 row 30). Every row of `events.jsonl` carries one of these types; the columns say whether
`replay.fold` reads it, what the payload records, and which keys it carries.

Read the columns like this:

* **folded** — `replay.fold` reduces it into `RunState`, so it is part of the replayable run state
  (engine invariant #4). **diagnostic** — the fold ignores it by design: an audit, activity-feed or
  observability row that never changes what the search decides.
* **whole** — the handler keeps the payload OBJECT rather than named keys, so every key a writer
  puts there reaches `RunState` and every projection over it. A key added to one of those types is
  a new field of the UI's data model.
* **required** is what a writer must write today (each one is checked against every literal writer);
  **optional** is everything else. Neither is a claim about OLD rows: a log written before a key
  existed does not carry it, and the fold defaults both halves — which is engine invariant #5, and
  is proved by folding every type here with an empty payload.

The event type itself is the contract's identity and is never renamed or reused; see the head of
`looplab/events/types.py` for the four steps that add one.

"""
    return head + render_rows(rows()) + "\n"


def generated_block(text: str) -> str:
    m = re.search(re.escape(BEGIN) + r".*?" + re.escape(END), text, re.S)
    return m.group(0) if m else ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m looplab.events.event_reference")
    parser.add_argument("--check", action="store_true", help="exit 1 when the page is stale")
    args = parser.parse_args(argv)
    page = render_page()
    if args.check:
        stale = (not PAGE.is_file()
                 or generated_block(PAGE.read_text(encoding="utf-8")) != generated_block(page))
        print("stale" if stale else "current")
        return 1 if stale else 0
    PAGE.write_text(page, encoding="utf-8")
    print(f"wrote {PAGE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
