"""Per-surface size budgets for the two docs every behaviour change has to edit (review 2026-09-22,
TST-07), held SHRINK-ONLY: the violators on the day this landed are listed, and a new one is red.

WHY, measured on 2026-09-23. CLAUDE.md makes two surfaces part of every change — the settings table
in `docs/guide/configuration.md` (one row per `Settings` field, with its correct default) and the
process diagram `docs/infographic/agent-architecture.html` (a `B` block map and an `E` edge list) —
and both had grown by appending, with no ceiling anywhere:

* the diagram is 193 KB, and 168,204 characters of it are the 668 strings of its `B`/`E` data; 75
  of those strings are over 600 characters, in 26 boxes, and the longest single bullet is 5,593
  (`e_sal`);
* configuration.md is 268 KB; its 246 settings rows hold 205,379 characters, and 44 rows are over
  1,500 — the longest, `auto_install_deps`, is 7,332.

A box bullet or a table cell is where a reader looks for the RULE; the measurement, the incident and
the alternatives refused belong in the module docstring or a numbered doc — the split CLAUDE.md's own
byte budget draws. The two numbers are the review's: a diagram string <= 600 characters, a settings
row <= 1,500.

THE SHAPE is `tests/data/containment_unreviewed.txt`'s, with a size on each row. A row is
`<key> <count> <longest>` — the box or edge (`edge:<from>-><to>`) or the settings field, how many of
its strings/rows are over budget, and the longest — and both numbers are CEILINGS: a listed key may
shrink, never gain an over-budget string or grow its longest one; an unlisted key over budget is red;
a listed key back within budget is a STALE row and red, so the file lists only live violators; and
the number of rows may only fall. Lowering a ceiling after shortening a string is encouraged, and
raising one is the one edit this file exists to make visible in review.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIAGRAM = ROOT / "docs" / "infographic" / "agent-architecture.html"
CONFIG_DOC = ROOT / "docs" / "guide" / "configuration.md"
DIAGRAM_BACKLOG = ROOT / "tests" / "data" / "diagram_strings_over_budget.txt"
SETTINGS_BACKLOG = ROOT / "tests" / "data" / "settings_rows_over_budget.txt"
DIAGRAM_STRING_BUDGET = 600
SETTINGS_ROW_BUDGET = 1_500
# The row counts the day the budgets landed. Lower them as rows go; never raise them.
DIAGRAM_BACKLOG_CEILING = 26
SETTINGS_BACKLOG_CEILING = 44

_JS_TOKEN = re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'|`(?:[^`\\]|\\.)*`'
                       r"|//[^\n]*|/\*[\s\S]*?\*/")
_JS_ESCAPE = re.compile(r"\\u[0-9a-fA-F]{4}|\\x[0-9a-fA-F]{2}|\\.")
# Who a literal belongs to: `box("id", …)`, a `stack(…)` item `{id:"…", …}`, or `edge("from", side,
# "to", …)`. A literal is attributed to the nearest definition that OPENS before it.
_OWNER = re.compile(r'(?:\bbox\(|\bid:)"([A-Za-z_0-9]+)"'
                    r'|\bedge\("([A-Za-z_0-9]+)",\s*"[^"]*",\s*"([A-Za-z_0-9]+)"')
# `tests/test_config_docs_sync.py::test_no_ghost_rows_for_removed_fields` reads a settings row the
# same way: a table line whose first cell opens with a backticked field name.
_SETTINGS_ROW = re.compile(r"^\|\s*`([a-z][a-z0-9_]+)`")


def _diagram_data(html: str) -> str:
    """The inline script up to the renderer: the block map, the edge list and the helpers that
    build them — the same cut `tests/test_architecture_diagram_geometry.py` evaluates."""
    script = re.search(r"<script[^>]*>([\s\S]*?)</script>", html).group(1)
    lines = script.split("\n")
    cut = next((i for i, ln in enumerate(lines) if 'svg.setAttribute("viewBox"' in ln), len(lines))
    return "\n".join(lines[:cut])


def diagram_strings(html: str) -> list[tuple[str, int]]:
    """(owner, rendered length) for every string literal in the diagram's data.

    Read from the SOURCE, not by running the script, so the guard needs no Node: measured against
    the block map the geometry test evaluates, the two agree string for string (75 over budget, in
    the same 26 boxes, with the same longest). That holds only while every rendered string is ONE
    literal, which `test_the_diagram_data_holds_each_string_as_one_literal` keeps true.
    """
    data = _diagram_data(html)
    owners = [(m.start(), m.group(1) or f"edge:{m.group(2)}->{m.group(3)}")
              for m in _OWNER.finditer(data)]
    out: list[tuple[str, int]] = []
    for m in _JS_TOKEN.finditer(data):
        tok = m.group(0)
        if tok.startswith(("//", "/*")):
            continue
        owner = next((name for pos, name in reversed(owners) if pos <= m.start()), "<preamble>")
        out.append((owner, len(_JS_ESCAPE.sub("x", tok[1:-1]))))
    return out


def settings_rows(text: str) -> list[tuple[str, int]]:
    """(field, characters) for every settings-table row of configuration.md."""
    return [(m.group(1), len(line)) for line in text.splitlines()
            if (m := _SETTINGS_ROW.match(line))]


def over_budget(items: list[tuple[str, int]], budget: int) -> dict[str, tuple[int, int]]:
    """{key: (how many over budget, the longest)} for the keys with anything over budget."""
    agg: dict[str, tuple[int, int]] = {}
    for key, n in items:
        if n > budget:
            count, longest = agg.get(key, (0, 0))
            agg[key] = (count + 1, max(longest, n))
    return agg


def read_rows(path: Path) -> list[tuple[str, int, int]]:
    """The `<key> <count> <longest>` rows of a budget backlog; a `#` line is prose, not a row."""
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, count, longest = line.split()
            rows.append((key, int(count), int(longest)))
    return rows


def budget_problems(over: dict[str, tuple[int, int]], rows: list[tuple[str, int, int]],
                    budget: int) -> list[str]:
    """The whole rule, statable: what is wrong with `over` (measured now) against `rows` (listed)."""
    listed = {key: (count, longest) for key, count, longest in rows}
    out = []
    for key, (count, longest) in sorted(over.items()):
        if key not in listed:
            out.append(f"NEW  {key}: {count} over {budget} (longest {longest}) — shorten it to "
                       f"<= {budget}, putting its story in a module docstring or a numbered doc")
            continue
        was_count, was_longest = listed[key]
        if count > was_count or longest > was_longest:
            out.append(f"GREW {key}: {count} over budget, longest {longest} (ceiling {was_count}, "
                       f"{was_longest}) — a listed violator may shrink, never grow")
    for key in sorted(set(listed) - set(over)):
        out.append(f"STALE {key}: nothing of it is over {budget} any more — delete its row")
    return out


# ------------------------------------------------------------------ the two guards


def test_no_diagram_string_grows_past_its_budget():
    """MUTATION: add a 601-character bullet to any box, or lengthen `e_sal`'s 5,593-character one,
    or shorten every over-budget bullet of `spine` without deleting its row -> red."""
    over = over_budget(diagram_strings(DIAGRAM.read_text(encoding="utf-8")), DIAGRAM_STRING_BUDGET)
    problems = budget_problems(over, read_rows(DIAGRAM_BACKLOG), DIAGRAM_STRING_BUDGET)
    assert not problems, (f"{DIAGRAM.relative_to(ROOT)} strings against the "
                          f"{DIAGRAM_STRING_BUDGET}-character budget "
                          f"({DIAGRAM_BACKLOG.relative_to(ROOT)}):\n  " + "\n  ".join(problems))


def test_no_settings_row_grows_past_its_budget():
    """MUTATION: add a 1,501-character row, lengthen `auto_install_deps`'s, or bring
    `concept_run_base` under the budget without deleting its row -> red."""
    over = over_budget(settings_rows(CONFIG_DOC.read_text(encoding="utf-8")), SETTINGS_ROW_BUDGET)
    problems = budget_problems(over, read_rows(SETTINGS_BACKLOG), SETTINGS_ROW_BUDGET)
    assert not problems, (f"{CONFIG_DOC.relative_to(ROOT)} settings rows against the "
                          f"{SETTINGS_ROW_BUDGET:,}-character budget "
                          f"({SETTINGS_BACKLOG.relative_to(ROOT)}):\n  " + "\n  ".join(problems))


def test_the_budget_backlogs_only_shrink():
    """One row per key, and no more rows than on the day the budgets landed."""
    for path, ceiling in ((DIAGRAM_BACKLOG, DIAGRAM_BACKLOG_CEILING),
                          (SETTINGS_BACKLOG, SETTINGS_BACKLOG_CEILING)):
        keys = [key for key, _, _ in read_rows(path)]
        assert len(keys) == len(set(keys)), f"{path.name}: duplicate rows"
        assert len(keys) <= ceiling, (path.name, len(keys), ceiling)


def test_the_diagram_data_holds_each_string_as_one_literal():
    """The measurement reads LITERALS; a bullet assembled from pieces (`"…" + "…"`, a `${}`
    template) would be measured piece by piece and pass at any length. With every literal and
    comment blanked out, no `+` may touch a string and no template may interpolate. Measured when
    this landed: none does — the three raw-text matches are strings that OPEN with a plus sign
    (`"+ lineage prior fixes"`, `"+ weak half …"`, the `"+ cards"` edge label)."""
    data = _diagram_data(DIAGRAM.read_text(encoding="utf-8"))
    code = _JS_TOKEN.sub(lambda m: "" if m.group(0).startswith(("//", "/*")) else '"S"', data)
    joined = re.findall(r'"S"\s*\+|\+\s*"S"', code)
    assert not joined, "a diagram string is assembled with `+`; write it as one literal"
    templates = [m.group(0) for m in _JS_TOKEN.finditer(data)
                 if m.group(0).startswith("`") and "${" in m.group(0)]
    assert not templates, "a diagram string interpolates a template; write it as one literal"


# ------------------------------------------------------------------ the rule and the reader, driven


def test_the_measurement_attributes_each_string_to_its_box_or_edge():
    """A `box(…)` title/bullet, a `stack` item's bullet and an edge label land on their OWN owner,
    and an escape is one character — so a row names the box to shorten, not a neighbour."""
    long_a, long_b, long_e = "a" * 601, "b" * 599 + '\\"', "e" * 650
    html = ("<script>\nconst B={},E=[];\n"
            f'box("alpha",0,0,1,1,"Title","t",C.r,["{long_a}","short"]);\n'
            f'stack(0,1,0,[\n {{id:"beta",h:1,t:"T",tag:"",c:C.r,sub:["{long_b}"]}},\n]);\n'
            f'edge("alpha","b","beta","t",{{label:"{long_e}"}});\n'
            'svg.setAttribute("viewBox","0 0 1 1");\n'
            f'box("after_the_cut",0,0,1,1,"{"z" * 900}");\n</script>')
    over = over_budget(diagram_strings(html), 600)
    assert over == {"alpha": (1, 601), "edge:alpha->beta": (1, 650)}, over
    assert ("beta", 600) in diagram_strings(html), "an escape counts as ONE rendered character"


def test_the_budget_rule_names_new_grown_and_stale_rows():
    """The truth table of `budget_problems`: listed and shrunk passes; an unlisted key, a listed key
    with one more over-budget string or a longer longest, and a listed key back under budget each
    fail, each with its own verb."""
    rows = [("kept", 2, 900), ("shrunk", 2, 900), ("fixed", 1, 700)]
    assert budget_problems({"kept": (2, 900), "shrunk": (1, 650)}, rows[:2], 600) == []
    problems = budget_problems(
        {"kept": (3, 900), "shrunk": (2, 901), "brand_new": (1, 601)}, rows, 600)
    assert [p.split()[0:2] for p in problems] == [
        ["NEW", "brand_new:"], ["GREW", "kept:"], ["GREW", "shrunk:"], ["STALE", "fixed:"]], problems


def test_a_settings_row_is_read_the_way_the_ghost_row_guard_reads_it():
    """Only a table line whose FIRST cell opens with a backticked field is a settings row; prose,
    headers and other tables are not measured."""
    text = ("| Field | Env | Default | What |\n|---|---|---|---|\n"
            f"| `alpha_knob` | `LOOPLAB_ALPHA_KNOB` | `1` | {'x' * 1600} |\n"
            f"Prose mentioning `alpha_knob` {'y' * 2000}\n"
            f"| {'not a field' * 200} | row |\n")
    rows = settings_rows(text)
    assert [key for key, _ in rows] == ["alpha_knob"], rows
    assert over_budget(rows, 1_500) == {"alpha_knob": (1, rows[0][1])}
