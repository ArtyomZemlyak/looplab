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
    # Block comments (`/* */`, JSX's `{/* */}`) first, then line comments: either one could carry
    # the pinned text alone (code review of the registry).
    source = re.sub(r"/\*.*?\*/", "", _text("ui/src/AssistantBar.jsx"), flags=re.S).splitlines()
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
# A string is a pointer: `tests/<file>.py::<test>` must be a test function there, and
# `ui/test/<file>::<title>` a `test('<title>'` there (the UI suite runs it in CI) — a bare file
# pointer proved nothing about the criterion (code review of the registry). A tuple is several
# pointers, all held. A callable is checked here.
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
    "EB-18": "ui/test/entryBarrier.test.js::the model screen shows no credential-store vocabulary until its details are opened",
    "EB-19": "ui/test/entryBarrier.test.js::a small portfolio keeps the list plain until asked; five runs show the tools unasked",
    # Withdrawn; what the decision rests on — a finished run lands on its Report — is held here.
    "EB-20": "ui/test/timelineSemantics.test.js::RunView owns one paged timeline shared by Dock and EventExplorer",
    "EB-21": "ui/test/reportPresentation.test.js::the verdict banner opens with the result sentence, its status labels after it (doc 74 EB-21)",
    "EB-22": "ui/test/assistantLanguage.test.js::the full-screen Assistant, which covers the header, carries the one visible language control",
    "EB-23": eb23,
    "EB-24": ("tests/test_offline_demo_spec.py::test_the_server_validates_the_ui_demo_spec",
              "ui/test/entryBarrier.test.js::an empty installation offers the offline demo as a launch card, and opening it writes nothing"),
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


@pytest.mark.parametrize("finding", sorted(k for k, v in ACCEPTANCE.items() if not callable(v)))
def test_a_pointer_names_a_check_that_exists(finding):
    targets = ACCEPTANCE[finding]
    for target in (targets,) if isinstance(targets, str) else targets:
        path, _, name = target.partition("::")
        assert name, f"{finding}: {target} names a file, not the check inside it"
        assert (ROOT / path).is_file(), target
        text = (ROOT / path).read_text(encoding="utf-8")
        if path.endswith(".py"):
            names = {node.name for node in ast.walk(ast.parse(text))
                     if isinstance(node, ast.FunctionDef)}
            assert name in names, f"{finding}: {name} is not a test in {path}"
        else:
            title = re.compile(r"\btest\(\s*(['\"])" + re.escape(name) + r"\1")
            assert title.search(text), f"{finding}: no test titled {name!r} in {path}"


@pytest.mark.parametrize("finding", sorted(k for k, v in ACCEPTANCE.items() if callable(v)))
def test_an_inline_acceptance_holds_on_the_tree(finding, tmp_path, monkeypatch):
    check = ACCEPTANCE[finding]
    params = check.__code__.co_varnames[:check.__code__.co_argcount]
    check(**{name: {"tmp_path": tmp_path, "monkeypatch": monkeypatch}[name] for name in params})



# --- doc 74 §12.2 (the critique) and doc 74 §12.1 (the corrections): their FACTUAL claims --------

def _findings():
    text = DOC.read_text(encoding="utf-8")
    return {m.group(1): m.group(2) + m.group(3) for m in re.finditer(
        r"^#### (EB-\d\d) ([^\n]*)\n(.*?)(?=^#### |^## )", text, re.S | re.M)}


def test_critique_point_6_names_exactly_the_findings_section_12_marked():
    """Point 6 lists the findings doc 74 §12 changed. Hand-counted, it went stale twice (11
    written, 15 true); here the list in the sentence must equal the marks the findings carry."""
    marked = sorted(eb for eb, body in _findings().items()
                    if re.search(r"уточнено|пересмотрено|снято|исправлено|добавлена", body))
    point = re.search(r"^6\. .*?(?=^7\. )", DOC.read_text(encoding="utf-8"), re.S | re.M).group(0)
    stated = re.search(r"\((\d+) из 28: ([^;]+);", point)
    assert stated and int(stated.group(1)) == len(marked), (stated and stated.group(1), marked)
    named = set()
    for part in re.split(r",\s*", stated.group(2).replace("EB-", "")):
        lo, _, hi = part.replace("–", "-").partition("-")
        named.update(f"EB-{n:02d}" for n in range(int(lo), int(hi or lo) + 1))
    assert sorted(named) == marked, (sorted(named), marked)


def test_critique_points_1_to_4_and_9_hold_on_the_tree():
    # 1: the surface the first draft proposed was not added; the demo is a file
    help_text = _help()
    assert not re.search(r"^│ demo ", help_text, re.M) and "--quickstart" not in _help("harness")
    assert (ROOT / "examples" / "demo.yaml").is_file()
    # 2: the two refused proposals are explained in their own tests
    contracts = _text("tests/test_documentation_contracts.py")
    assert "Deliberately a literal, not a derived count" in contracts
    assert "CLAUDE_MD_MAX_BYTES = " in contracts
    # 4: doc 71 is navigated to, not cut — all its sections remain
    doc71 = _text(next(str(p.relative_to(ROOT)) for p in (ROOT / "docs").glob("71-*.md")))
    assert len(re.findall(r"^## ", doc71, re.M)) >= 69 and "Как читать" in doc71[:2000]
    # 9: no package status in doc 74 §7 contradicts doc 74 §12.4 any more
    packages = DOC.read_text(encoding="utf-8").split("## 7. ", 1)[1].split("## 8. ", 1)[0]
    assert "частично" not in packages


def test_section_12_1_corrections_hold_on_the_tree():
    # EB-04: the offline template labels its own number (the label the fix must NOT decide by)
    template = _text("looplab/adapters/dataset_task.py")
    assert '"metric_name": "row_count (offline baseline)"' in template
    # EB-06: the runbook is CITED (in the comments of config.py, claimpin.py, seed_from_run.py), so
    # it moved rather than went away; that every such citation still resolves is
    # `tests/test_claim_pins.py::test_no_source_citation_is_dead`'s job — here, that it moved.
    assert (ROOT / "benchmarks" / "NEXT_RUN.md").is_file() and not (ROOT / "NEXT_RUN.md").exists()
    assert (ROOT / "tests" / "data" / "bench-out").is_dir()
    # EB-21: the demo declares the comparison the toy task cannot know by itself
    assert "comparison_contract" in yaml.safe_load(_text("examples/demo.yaml"))["task"]



def test_critique_points_5_7_8_10_and_12_hold(monkeypatch):
    doc = DOC.read_text(encoding="utf-8")
    # 5: every test the point names as having made an acceptance machine-checked exists
    point5 = re.search(r"^5\. .*?(?=^6\. )", doc, re.S | re.M).group(0)
    for rel in re.findall(r"`(tests/[\w/]+\.py)`", point5):
        assert (ROOT / rel).is_file(), rel
    # 7: the corrected baseline holds in the findings — no 53 KB `replay` claim is left outside the
    # review section, which quotes the old value on purpose to say it was wrong
    findings = doc.split("## 12. ", 1)[0]
    assert not re.search(r"replay[^\n]{0,40}53\s?(КБ|015)", findings)
    assert "58 КБ" in findings
    # 8: the §10 position command is the one that matches (awk on the tab `nl` emits)
    reproduce = doc.split("## 10. ", 1)[1].split("## 11. ", 1)[0]
    assert "| nl | awk '$2 ~ /^(run|ui|init|inspect)$/'" in reproduce
    # 10: the premise of the EB-05 correction, DRIVEN: where there is no /proc and no effective uid
    # (Windows), the rung reports itself advisory on every run — not only for root
    import builtins
    import os
    from looplab.runtime import read_fence
    real_open = builtins.open

    def no_proc(path, *args, **kwargs):
        if str(path).startswith("/proc/"):
            raise OSError("no /proc here")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", no_proc)
    monkeypatch.delattr(os, "geteuid", raising=False)
    assert "no effective-uid check" in read_fence._dac_override()
    # 12: §11 names its one exception, and §12 stops at the subsection that closes it
    rules = doc.split("## 11. ", 1)[1].split("## 12. ", 1)[0]
    assert "Одно исключение — §12" in rules
    assert max(int(n) for n in re.findall(r"^### 12\.(\d+)\.", doc, re.M)) == 9


def test_the_user_test_protocol_is_executable_as_written():
    """doc 74 §12.9: the one open item cannot run inside a session, so the doc must hand over a
    protocol a person can run unprepared — participants, tasks, measures, a pass line, a record."""
    protocol = DOC.read_text(encoding="utf-8").split("### 12.9.", 1)[1]
    for part in ("**Участники.**", "**Задания**", "**Измерения**", "**Порог успеха.**",
                 "**Запись.**"):
        assert part in protocol, part



# Every critique point of doc 74 §12.2 → the check that holds its factual claim. Points about HOW
# the doc was written are held by the remedy they prescribe: point 3 (one snapshot) by EB-23's
# check of the resize code; point 11 (done recorded by substance, not acceptance) by this file's
# coverage test, which makes every acceptance a check.
CRITIQUE_CHECKS = {
    1: "test_critique_points_1_to_4_and_9_hold_on_the_tree",
    2: "test_critique_points_1_to_4_and_9_hold_on_the_tree",
    3: "eb23",
    4: "test_critique_points_1_to_4_and_9_hold_on_the_tree",
    5: "test_critique_points_5_7_8_10_and_12_hold",
    6: "test_critique_point_6_names_exactly_the_findings_section_12_marked",
    7: "test_critique_points_5_7_8_10_and_12_hold",
    8: "test_critique_points_5_7_8_10_and_12_hold",
    9: "test_critique_points_1_to_4_and_9_hold_on_the_tree",
    10: "test_critique_points_5_7_8_10_and_12_hold",
    11: "test_every_finding_in_doc_74_has_exactly_one_acceptance_row",
    12: "test_critique_points_5_7_8_10_and_12_hold",
}


def test_every_critique_point_has_a_check():
    critique = DOC.read_text(encoding="utf-8").split("### 12.2.", 1)[1].split("### 12.3", 1)[0]
    points = [int(n) for n in re.findall(r"^(\d+)\. \*\*", critique, re.M)]
    assert points == sorted(CRITIQUE_CHECKS), (points, sorted(CRITIQUE_CHECKS))
    defined = {name for name, value in globals().items() if callable(value)}
    assert not [name for name in CRITIQUE_CHECKS.values() if name not in defined]
