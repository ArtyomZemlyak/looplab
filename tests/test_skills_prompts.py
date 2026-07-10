"""I18: prompt store (hot-reload), skills (progressive disclosure), AGENTS.md."""
from __future__ import annotations

import json
from pathlib import Path

import anyio

from looplab.agents.agent import CompositeTools
from looplab.tools.agents_md import generate_agents_md
from looplab.events.eventstore import EventStore
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.core.prompts import PromptStore, _strip_frontmatter, render
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.tools.skills import SkillLibrary, SkillTools
from looplab.adapters.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]


# ---- prompt store ----
def test_prompt_store_hot_reload_and_default(tmp_path):
    store = PromptStore(str(tmp_path))
    # missing file -> default (with $var rendering)
    assert render(store, "sys", "hello $who", who="world") == "hello world"
    # file overrides; frontmatter stripped; re-read each call (hot reload)
    f = tmp_path / "sys.md"
    f.write_text("---\nname: sys\n---\nFIRST $who", encoding="utf-8")
    assert store.get("sys", default="def", who="W") == "FIRST W"
    f.write_text("SECOND $who", encoding="utf-8")
    assert store.get("sys", default="def", who="W") == "SECOND W"


def test_no_store_uses_default():
    assert render(None, "sys", "default $x", x="1") == "default 1"


# ---- skills ----
def test_skill_library_and_tools(tmp_path):
    (tmp_path / "a.md").write_text(
        "---\nname: cv\ndescription: how to cross-validate\n---\nBody: do K-fold.",
        encoding="utf-8")
    lib = SkillLibrary(str(tmp_path))
    assert "cv" in lib.skills and lib.skills["cv"].description == "how to cross-validate"

    tools = SkillTools(str(tmp_path))
    names = {f["function"]["name"] for f in tools.specs()}
    assert names == {"list_skills", "use_skill"}
    # progressive disclosure: list is cheap (name+desc), body loaded on demand
    listing = tools.execute("list_skills", {})
    assert "cv: how to cross-validate" in listing and "K-fold" not in listing
    assert "K-fold" in tools.execute("use_skill", {"name": "cv"})
    assert "no such skill" in tools.execute("use_skill", {"name": "nope"}).lower()


def test_example_skill_loads():
    lib = SkillLibrary(str(ROOT / "examples" / "skills"))
    assert "cross_validation" in lib.skills


# ---- composite tools ----
def test_composite_tools_routing(tmp_path):
    (tmp_path / "s.md").write_text("---\nname: s\ndescription: d\n---\nbody", encoding="utf-8")
    from looplab.tools.knowledge_tools import KnowledgeTools
    (tmp_path / "note.md").write_text("a knowledge note about trees", encoding="utf-8")
    comp = CompositeTools([KnowledgeTools(str(tmp_path)), SkillTools(str(tmp_path))])
    names = {f["function"]["name"] for f in comp.specs()}
    assert {"kb_search", "grep", "list_skills", "use_skill"} <= names
    assert "s: d" in comp.execute("list_skills", {})
    assert comp.execute("nonexistent", {}).startswith("(unknown tool")


# ---- AGENTS.md ----
def test_agents_md_content():
    md = generate_agents_md(ToyTask.load(ROOT / "examples" / "toy_task.json"))
    assert "AGENTS.md" in md and "metric" in md and "minimize" in md


def test_engine_writes_agents_md(tmp_path):
    task = ToyTask.load(ROOT / "examples" / "toy_task.json")
    r, d = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=4))
    anyio.run(eng.run)
    assert (tmp_path / "run" / "AGENTS.md").exists()


# --- prompts: frontmatter stripping vs Markdown horizontal rules ---------------------------------

def test_frontmatter_keeps_markdown_horizontal_rules():
    """`---` used as Markdown rules must NOT be treated as a frontmatter fence (Section A vanished)."""
    t = "Rules for the researcher.\n---\nSection A: do X.\n---\nSection B: do Y.\n"
    out = _strip_frontmatter(t)
    assert "Section A" in out and "Section B" in out


def test_frontmatter_strips_real_leading_block():
    fm = "---\ntitle: x\nauthor: y\n---\nBody starts here.\n"
    assert _strip_frontmatter(fm).strip() == "Body starts here."


def test_skilltools_execute_never_raises_on_junk_arg():
    from looplab.tools.skills import SkillTools
    import tempfile
    st = SkillTools(tempfile.mkdtemp())
    assert st.execute("use_skill", {"name": ["not", "hashable"]}).startswith("(")   # tool error, no raise
    assert st.execute("bogus", {}).startswith("(unknown tool")
