"""doc 74 §12.8 as a test: every entry-barrier finding's acceptance, checked on the tree.

The doc's own critique (doc 74 §12.2 point 11) was that items were recorded "done" by the
substance of the change rather than by the acceptance the doc itself set — a literal pass found
four. A table in a
document cannot stop that recurring; this registry can. Every `EB-NN` heading in doc 74 must have
exactly one row here, and every row either checks the criterion inline or names the test that does
(which must exist). A finding added to the doc without a check, or a check whose test was renamed
away, is red.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from looplab.cli import app

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "74-entry-barrier-inspection-2026-10-09.md"
ENTRY_PAGES = ["README.md", "docs/index.md", "docs/guide/index.md", "docs/guide/installation.md",
               "docs/guide/quickstart.md", "docs/guide/cli-walkthrough.md"]


def _text(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def _help(*args):
    return re.sub(r"\x1b\[[0-9;]*m", "", CliRunner().invoke(app, [*args, "--help"],
                                                            terminal_width=120).output)


# --- inline checks: one per criterion the doc states (doc 74 §5, tabled in doc 74 §12.8) --------

def eb01():
    readme = _text("README.md")
    assert len(readme.split()) <= 1000
    first = next(i for i, line in enumerate(readme.splitlines(), 1)
                 if re.match(r"(looplab|python -m pip|pip|git clone) ", line))
    assert first <= 40, f"first command on line {first}"


def eb02():
    assert len(_text("docs/guide/installation.md").split()) <= 500
    installs = [line for line in _text("README.md").splitlines()
                if re.match(r"\s*(python -m )?pip install", line) and "[dev" not in line]
    assert len(installs) == 1, installs          # `[dev,ui]` is the contributors' install


def eb03():
    for rel in ENTRY_PAGES:
        assert "2026-08-04" not in _text(rel), rel
    demo = yaml.safe_load(_text("examples/demo.yaml"))
    assert demo["settings"]["backend"] == "toy"    # runs with no flag; the run itself: eb07's test


def eb06():
    root_files = [p for p in ROOT.iterdir() if p.is_file()]
    bench = re.compile(r"(e5small_v\d+|rubert_run_\d+)\.json$")
    assert not [p.name for p in root_files if bench.match(p.name)]
    assert not (ROOT / "NEXT_RUN.md").exists() and not (ROOT / "bench-out").exists()
    personal = [p.name for p in root_files if p.suffix in {".json", ".md"}
                and "/home/" in p.read_text(encoding="utf-8", errors="replace")]
    assert not personal, personal


def eb09():
    assert "Maintainer note" not in _help("run")


def eb10(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "demo.yaml").write_text(_text("examples/demo.yaml"), encoding="utf-8")
    assert CliRunner().invoke(app, ["run", "demo.yaml"]).exit_code == 0
    out = CliRunner().invoke(app, ["inspect", "runs/demo"]).output.splitlines()
    assert len(out) <= 20 and out[0].startswith("run=")
    assert "max_nodes" in CliRunner().invoke(app, ["inspect", "runs/demo", "--config"]).output


def eb12():
    home = _text("docs/index.md")
    cards = home.split("## Start here", 1)[1].split("## ", 1)[0]
    links = re.findall(r"\*\*\[[^\]]+\]\(guide/([a-z-]+)\.md\)\*\*", cards)
    assert links == ["installation", "quickstart", "cli-walkthrough", "ui"], links
    assert "offline in one command" not in cards          # the stale Quickstart card (doc 74 §5)
    assert "examples/demo.yaml" in cards
    assert "examples/demo.yaml" in _text("docs/guide/cli-walkthrough.md")


def eb13():
    class _Loader(yaml.SafeLoader):
        pass
    _Loader.add_multi_constructor("", lambda loader, suffix, node: None)
    nav = yaml.load(_text("mkdocs.yml"), Loader=_Loader)["nav"]
    groups = [next(iter(item)) for item in nav if isinstance(next(iter(item.values())), list)]
    user = [g for g in groups if g != "Design records"]
    assert len(user) <= 4 and user[0] == "Get started", groups


def eb14():
    head = _text(next(str(p.relative_to(ROOT)) for p in (ROOT / "docs").glob("71-*.md")))[:2000]
    assert "Как читать" in head
    row = next(line for line in _text("docs/00-INDEX.md").splitlines()
               if "[71" in line or "71-" in line)
    assert "ещё не реализ" not in row


def eb15():
    assert not re.search(r"\d[\d,\s]{3,}\s*(collected\s+)?tests", _text("README.md"))


def eb16():
    sections = _text("AGENTS.md").split("\n## ")
    assert sections[1].startswith("First run") and len(sections[1].split()) <= 200


def eb17(tmp_path, monkeypatch):
    assert re.search(r"^## Start here", _text("docs/guide/configuration.md"), re.M)
    monkeypatch.chdir(tmp_path)
    assert CliRunner().invoke(app, ["init"]).exit_code == 0
    active = [line for line in (tmp_path / "looplab.yaml").read_text(encoding="utf-8").splitlines()
              if line.strip() and not line.lstrip().startswith("#")]
    assert len(active) <= 12, active


def eb23():
    # Dropped because the panel is resizable and keeps an explicit width (doc 74 §12.6). Read from
    # the CODE with comments removed — a guard a comment could satisfy proves nothing (CLAUDE.md).
    source = _text("ui/src/AssistantBar.jsx").splitlines()
    code = "\n".join(line.split("//", 1)[0] for line in source)
    assert re.search(r'className="asst-resize" role="separator"', code)
    assert re.search(r"onKeyDown=\{resizeWithKeys\}", code)
    assert re.search(r"storageSet\('ll\.asstW', `user:\$\{width\}`\)", code)


def eb25():
    from tests.test_documentation_contracts import CLAUDE_MD_MAX_BYTES
    assert len((ROOT / "CLAUDE.md").read_bytes()) <= CLAUDE_MD_MAX_BYTES


def eb27():
    assert (ROOT / "tests" / "README.md").is_file()
    assert sum("tests/README.md" in line for line in _text("CLAUDE.md").splitlines()) >= 1


def eb28():
    readme = _text("README.md")
    assert "one-command" not in readme.lower() and re.search(r"GPU with about \d+ GB", readme)


# --- the registry: every EB-NN in doc 74 → its check ----------------------------------------------
# A string is a pointer: `tests/<file>.py::<test>` must be a test function there; `ui/test/<file>`
# must exist (the UI suite runs it in CI). A callable is checked here.
ACCEPTANCE = {
    "EB-01": eb01,
    "EB-02": eb02,
    "EB-03": eb03,
    "EB-04": "tests/test_offline_baseline_note.py::test_an_offline_dataset_run_names_its_score_a_baseline_in_run_and_inspect",
    "EB-05": "tests/test_entry_page_budgets.py::test_installation_names_the_read_fence_warning_a_windows_or_root_run_prints",
    "EB-06": eb06,
    "EB-07": "tests/test_documentation_contracts.py::test_the_examples_map_names_every_example_and_the_demo_runs_offline",
    "EB-08": "tests/test_cli_help_panels.py::test_help_opens_with_the_start_here_panel_in_table_order",
    "EB-09": eb09,
    "EB-10": eb10,
    "EB-11": "tests/test_entry_page_budgets.py::test_start_in_assistant_keeps_the_steps_and_names_no_code",
    "EB-12": eb12,
    "EB-13": eb13,
    "EB-14": eb14,
    "EB-15": eb15,
    "EB-16": eb16,
    "EB-17": eb17,
    "EB-18": "ui/test/entryBarrier.test.js",
    "EB-19": "ui/test/entryBarrier.test.js",
    "EB-20": "ui/test/mountRunView.test.js",
    "EB-21": "ui/test/comparabilityRefusalRender.test.js",
    "EB-22": "ui/test/assistantLanguage.test.js",
    "EB-23": eb23,
    "EB-24": "tests/test_offline_demo_spec.py::test_the_server_validates_the_ui_demo_spec",
    "EB-25": eb25,
    "EB-26": "tests/test_documentation_contracts.py::test_index_mentions_every_numbered_document",
    "EB-27": eb27,
    "EB-28": eb28,
}


def test_every_finding_in_doc_74_has_exactly_one_acceptance_row():
    headings = re.findall(r"^#### (EB-\d\d) ", DOC.read_text(encoding="utf-8"), re.M)
    assert len(headings) == len(set(headings)), "a finding heading is duplicated"
    assert sorted(headings) == sorted(ACCEPTANCE), (
        f"missing: {sorted(set(headings) - set(ACCEPTANCE))}; "
        f"stale: {sorted(set(ACCEPTANCE) - set(headings))}")


@pytest.mark.parametrize("finding", sorted(k for k, v in ACCEPTANCE.items() if isinstance(v, str)))
def test_a_pointer_names_a_check_that_exists(finding):
    target = ACCEPTANCE[finding]
    path, _, name = target.partition("::")
    assert (ROOT / path).is_file(), target
    if name:
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        assert name in names, f"{finding}: {name} is not a test in {path}"


@pytest.mark.parametrize("finding", sorted(k for k, v in ACCEPTANCE.items() if callable(v)))
def test_an_inline_acceptance_holds_on_the_tree(finding, tmp_path, monkeypatch):
    check = ACCEPTANCE[finding]
    params = check.__code__.co_varnames[:check.__code__.co_argcount]
    check(**{name: {"tmp_path": tmp_path, "monkeypatch": monkeypatch}[name] for name in params})
