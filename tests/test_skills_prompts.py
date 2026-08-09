"""I18: prompt store (hot-reload), skills (progressive disclosure), AGENTS.md."""
from __future__ import annotations

import json
import re
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


def test_auto_skill_visibility_fails_closed_and_has_explicit_candidate_inspection(tmp_path):
    manual_dir = tmp_path / "manual"
    auto_dir = tmp_path / "auto"
    manual_dir.mkdir()
    auto_dir.mkdir()

    (manual_dir / "manual.md").write_text(
        "---\nname: manual\ndescription: operator-authored\nstatus: candidate\n---\nmanual body",
        encoding="utf-8")
    (manual_dir / "shared.md").write_text(
        "---\nname: shared\ndescription: manual wins\n---\nmanual shared body",
        encoding="utf-8")
    (auto_dir / "candidate.md").write_text(
        "---\nname: candidate\ndescription: one-run draft\nprovenance: AUTO\nstatus: Candidate\n"
        "---\ncandidate body",
        encoding="utf-8")
    (auto_dir / "promoted.md").write_text(
        "---\nname: promoted\ndescription: cross-task result\nprovenance: auto\nstatus: promoted\n"
        "---\npromoted body",
        encoding="utf-8")
    (auto_dir / "malformed.md").write_text(
        "---\nname: malformed\ndescription: unknown lifecycle\nprovenance: auto\nstatus: reviewed\n"
        "---\nmalformed body",
        encoding="utf-8")
    (auto_dir / "shared.md").write_text(
        "---\nname: shared\ndescription: auto loses\nprovenance: auto\nstatus: promoted\n"
        "---\nauto shared body",
        encoding="utf-8")

    default = SkillLibrary([manual_dir, auto_dir])
    assert set(default.skills) == {"manual", "promoted", "shared"}
    assert default.skills["manual"].provenance is None
    assert default.skills["manual"].status == "candidate"  # status cannot hide a manual skill
    assert default.skills["promoted"].provenance == "auto"
    assert default.skills["promoted"].status == "promoted"

    default_tools = SkillTools([manual_dir, auto_dir])
    assert default_tools.execute("use_skill", {"name": "manual"}) == "manual body"
    assert default_tools.execute("use_skill", {"name": "shared"}) == "manual shared body"
    promoted = default_tools.execute("use_skill", {"name": "promoted"})
    assert promoted.startswith("UNTRUSTED_MEMORY_AUTO_SKILL ")
    assert "promoted body" in promoted
    default_listing = default_tools.execute("list_skills", {})
    assert "candidate" not in default_listing
    assert "promoted: UNTRUSTED_MEMORY_AUTO_SKILL " in default_listing
    assert "provenance='auto' status='promoted' cross-task result" in default_listing
    # Human-authored rows retain the historical byte shape.
    assert "manual: operator-authored" in default_listing

    inspection = SkillLibrary(
        [manual_dir, auto_dir], include_auto_candidates=True)
    assert "candidate" in inspection.skills
    assert inspection.skills["candidate"].provenance == "auto"
    assert inspection.skills["candidate"].status == "candidate"
    assert "malformed" not in inspection.skills  # opt-in is candidates, not unknown auto states
    candidate_tools = SkillTools(
        [manual_dir, auto_dir], include_auto_candidates=True)
    assert "candidate: UNTRUSTED_MEMORY_AUTO_SKILL " in candidate_tools.execute(
        "list_skills", {})
    candidate = candidate_tools.execute("use_skill", {"name": "candidate"})
    assert candidate.startswith("UNTRUSTED_MEMORY_AUTO_SKILL ")
    assert "candidate body" in candidate


def test_auto_skill_becomes_visible_after_same_file_is_promoted(tmp_path):
    from looplab.engine.memory import write_auto_skill

    first = write_auto_skill(
        tmp_path, "Cache feature transforms", "reuse the fitted transform",
        ["goal:first", "kind:classification"], "task-a")
    assert first is not None
    assert "status: candidate" in first.read_text(encoding="utf-8")
    assert not SkillLibrary(tmp_path).skills
    assert SkillLibrary(tmp_path, include_auto_candidates=True).skills

    second = write_auto_skill(
        tmp_path, "Cache feature transforms", "reuse the fitted transform",
        ["goal:second", "kind:timeseries"], "task-b")
    assert second == first
    assert "status: promoted" in second.read_text(encoding="utf-8")
    skills = SkillLibrary(tmp_path).skills
    assert len(skills) == 1
    name, promoted = next(iter(skills.items()))
    assert name.startswith("auto-cache-feature-transforms-")
    assert promoted.status == "promoted"


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


def test_agents_md_uses_the_repo_contract_instead_of_the_script_contract():
    class RepoLike:
        id = "repo-demo"
        goal = "Fix the parser"
        direction = "max"

        def repo_spec(self):
            return {"editable": []}

        def agent_brief(self):
            return "Edit only src/parser.py; run pytest -q tests/test_parser.py."

    md = generate_agents_md(RepoLike())
    assert "Repository-task contract" in md
    assert "Edit only src/parser.py" in md
    assert "not the self-contained script/JSON-line task" in md
    assert "MUST print exactly one final line" not in md
    assert "repository-owned `AGENTS.md` remains" in md


def test_real_repo_task_agents_md_does_not_invent_the_synthetic_runtime(tmp_path):
    from looplab.adapters.repo_task import RepoTask

    task = RepoTask(id="repo-runtime", goal="Improve the Node service", editable_path=str(tmp_path))
    md = generate_agents_md(task)

    assert "Operator-declared repository evaluation environment" in md
    assert "task-specific brief and evaluation configuration are authoritative" in md
    assert "Python standard library + numpy. No network access." not in md


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


# ---- the Authoring panel's "how do I turn this on" hint ----
# `skills_dir` and `prompt_dir` are the only two knowledge directories with NO default, so for both
# the very first thing an operator sees is an empty tab plus an env-var name. That name used to be
# DERIVED as `LOOPLAB_${kind.toUpperCase()}_DIR`, which is right for skills/knowledge and wrong for
# prompts: the flat settings field is `prompt_dir`, singular, so `LOOPLAB_PROMPTS_DIR` is read by
# nothing. Following the hint therefore left the tab exactly as empty as before — which reads as
# "prompts are not a thing in this product". The mapping is data in panels.jsx now; this drives it.
_AUTHORING_ENV_LINE = re.compile(r"const AUTHORING_KIND_ENV = \{(?P<body>[^}]*)\}")
_AUTHORING_ENV_PAIR = re.compile(r"(?P<kind>\w+):\s*'(?P<env>LOOPLAB_[A-Z_]+)'")


def _authoring_kind_env() -> dict[str, str]:
    source = (ROOT / "ui" / "src" / "panels.jsx").read_text(encoding="utf-8")
    match = _AUTHORING_ENV_LINE.search(source)
    assert match, "panels.jsx no longer declares AUTHORING_KIND_ENV — the hint is derived again?"
    return {m.group("kind"): m.group("env") for m in _AUTHORING_ENV_PAIR.finditer(match.group("body"))}


def test_every_env_var_the_authoring_panel_prints_actually_configures_that_kind(monkeypatch, tmp_path):
    """Set the variable the UI tells you to set; the directory the authoring route reads must move.

    Driven end to end rather than pinned as a string pair: it goes through the real `Settings`
    loader (so it also fails if the flat `LOOPLAB_<FIELD>` convention is ever broken) and then
    through `serve/routers/misc.py::_current_author_directory`, which is the function the
    `GET /api/{kind}` listing resolves its root with.
    """
    from looplab.core.config import Settings
    from looplab.serve.routers.misc import _current_author_directory

    kinds = _authoring_kind_env()
    assert set(kinds) == {"prompts", "skills", "knowledge"}, kinds

    for kind, env in kinds.items():
        target = tmp_path / kind
        target.mkdir()
        monkeypatch.setenv(env, str(target))
        settings = Settings()

        class _Srv:
            def global_settings(self):
                return settings

        resolved = _current_author_directory(_Srv(), kind)
        assert resolved == target, (
            f"the Authoring panel tells an operator to set {env} for `{kind}`, but the "
            f"{kind} authoring route resolved {resolved!r} instead — the hint is a no-op")
        monkeypatch.delenv(env)
