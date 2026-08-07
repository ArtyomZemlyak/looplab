"""LoopLab installs what the REPO declares — the pins, the default, and the Developer's ask.

The defect these drive out, measured on the shipped corpus: `crash_repair._install_missing` handed
pip a BARE NAME, so `runs/rubert-dr-0804` resolved `transformers` to 5.14.1 against a
`transformers==4.51.0` pin and `pytorch-lightning` to 2.6.5 against `pytorch_lightning==1.5.1`.
`runs/rubert-dr-0807` node 0 then died on the `find_unused_parameters` default Lightning 2.x
flipped, and 7 of its 12 repair attempts are artifacts of an API the repo never asked for. Nothing
in LoopLab read a dependency declaration at all — `-r requirements.txt` appeared only in comments
and prompt strings.

Every engine test here drives the real method and observes the real effect (the exact argv pip was
handed, the event that landed) rather than pinning source text: the whole failure was a call that
happened and did the wrong thing, which a presence pin cannot see.
"""
from __future__ import annotations

from pathlib import Path

from looplab.core.models import Idea
from looplab.engine.eval_dispatch import _MAX_NODE_DEP_SYNCS, EvalDispatchMixin
from looplab.engine.orchestrator import Engine
from looplab.events.eventstore import EventStore
from looplab.events.types import DIAGNOSTIC_EVENTS, EV_DEPS_DECLARED
from looplab.runtime import deps
from looplab.runtime.deps import InstallResult
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree
from looplab.adapters.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"

# The live testbed's own requirements.txt, verbatim in the shapes that matter: a bare name, a hard
# pin, a commented-out alternative pin, an extras requirement (the one that makes `-c` impossible),
# and a distribution whose name does not match its import name.
_LIVE_SHAPES = """\
polars
transformers==4.51.0
# pplx
# transformers==5
sentence-transformers
faiss-gpu-cu12==1.9.0.0
pytorch_lightning==1.5.1
bm25s[full]
"""


class _Stub:
    def propose(self, state, parent):
        return Idea(operator="x", params={"x": 1.0, "y": 1.0})


class _Dev:
    def implement(self, idea):
        return "import json; print(json.dumps({'metric': 0.1}))\n"

    def repair(self, idea, code, error):
        return self.implement(idea)


def _engine(run_dir, **kw):
    return Engine(run_dir, task=ToyTask.load(TASK), researcher=_Stub(), developer=_Dev(),
                  sandbox=SubprocessSandbox(),
                  policy=GreedyTree(n_seeds=1, max_nodes=2, debug_depth=1), **kw)


def _repo(tmp_path, text=_LIVE_SHAPES, name="requirements.txt") -> Path:
    d = tmp_path / "repo"
    d.mkdir(exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")
    return d


def _declared_rows(run_dir):
    return [e for e in EventStore(Path(run_dir) / "events.jsonl").read_all()
            if e.type == EV_DEPS_DECLARED]


# --------------------------------------------------------------------------- pure: reading a file
def test_parse_requirements_keeps_the_line_verbatim_and_names_what_it_skips():
    pins, directives = deps.parse_requirements(
        "polars\n"
        "transformers==4.51.0   # trailing comment\n"
        "# whole-line comment\n"
        "\n"
        "-r base.txt\n"
        "--index-url https://example.invalid/simple\n"
        "bm25s[full]\n"
        "torch>=2.0,<3 ; python_version >= \"3.9\"\n"
        "pkg @ https://h/p#egg=pkg\n"
        "wrapped==\\\n"
        "1.2.3\n")
    # The LINE, not a re-rendering of it: what pip receives is what the operator wrote.
    assert pins["transformers"] == "transformers==4.51.0"
    assert pins["polars"] == "polars"
    assert pins["bm25s"] == "bm25s[full]"
    assert pins["torch"] == 'torch>=2.0,<3 ; python_version >= "3.9"'
    # A `#` inside a URL is part of the URL — stripping it would corrupt the requirement.
    assert pins["pkg"] == "pkg @ https://h/p#egg=pkg"
    # A continuation is ONE requirement; treating the tail as its own line would invent a
    # distribution named after a version specifier.
    assert pins["wrapped"] == "wrapped==1.2.3"
    assert "1.2.3" not in pins
    # What we do NOT follow is named rather than silently absent.
    assert directives == ["-r base.txt", "--index-url https://example.invalid/simple"]


def test_canonical_names_fold_the_three_separators_but_module_paths_do_not():
    # PEP 503: `-`, `_` and `.` are one character class for a DISTRIBUTION name.
    assert (deps._canon_dist("pytorch_lightning") == deps._canon_dist("PyTorch-Lightning")
            == deps._canon_dist("pytorch.lightning") == "pytorch-lightning")
    # …and that is deliberately NOT `_normal`, which serves dotted MODULE paths, where a dot
    # separates a submodule. A shared normalizer would answer one of the two questions wrongly.
    assert deps._normal("zope.interface") != deps._canon_dist("zope.interface")


def test_find_declaration_reports_what_it_will_not_act_on(tmp_path):
    d = _repo(tmp_path)
    (d / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (d / "environment.yml").write_text("name: x\n", encoding="utf-8")
    decl = deps.find_declaration(d)
    assert decl.found and decl.filename == "requirements.txt"
    assert decl.digest and len(decl.digest) == 12
    # Seeing a pyproject/environment.yml and installing from it are different acts. The receipt has
    # to be able to say "we noticed and left it alone", so an operator can decide.
    assert set(decl.observed) == {"pyproject.toml", "environment.yml"}
    assert "pyproject.toml" not in deps.DECLARATION_FILES


def test_an_empty_root_is_no_repo_not_the_process_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(_repo(tmp_path))          # a cwd that DOES declare dependencies
    assert deps.find_declaration("").found is False
    assert deps.find_declaration(None).found is False


def test_oversized_declaration_is_observed_but_never_installed_from(tmp_path):
    d = tmp_path / "big"
    d.mkdir()
    (d / "requirements.txt").write_text("x\n" * (deps.DECLARATION_MAX_BYTES // 2 + 10),
                                        encoding="utf-8")
    decl = deps.find_declaration(d)
    assert decl.found is False                  # nothing installs from it
    assert decl.observed == ("requirements.txt",)   # …and the receipt does not claim it was absent
    assert deps.declaration_argv(decl) == []


def test_declared_requirement_resolves_both_spellings_and_admits_the_faiss_gap(tmp_path):
    decl = deps.find_declaration(_repo(tmp_path))
    # The import name (`pytorch_lightning`) and the allowlist's pip name (`pytorch-lightning`) both
    # have to reach the same declared line — a repo writes either.
    assert deps.declared_requirement("pytorch_lightning", decl) == "pytorch_lightning==1.5.1"
    assert deps.pip_package("pytorch_lightning") == "pytorch-lightning"
    assert deps.declared_requirement("transformers", decl) == "transformers==4.51.0"
    assert deps.declared_requirement("sentence_transformers", decl) == "sentence-transformers"
    # The documented non-match: `_PIP_NAME` guesses `faiss -> faiss-cpu`, the repo pins
    # `faiss-gpu-cu12`. No name rule connects those without also connecting torch to torchvision,
    # so this answers None ON PURPOSE and the conflict is defused upstream by the `-r` install.
    assert deps.declared_requirement("faiss", decl) is None
    assert deps.declared_requirement("nothing_here", decl) is None
    assert deps.declared_requirement("transformers", None) is None


def test_declaration_argv_is_relative_so_it_resolves_in_the_repo(tmp_path):
    decl = deps.find_declaration(_repo(tmp_path))
    argv = deps.declaration_argv(decl, python="/x/py")
    assert argv == ["/x/py", "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
                    "-r", "requirements.txt"]
    assert str(tmp_path) not in " ".join(argv)   # by BASENAME — it runs in `decl.root`


# ------------------------------------------------- engine: an install must not move a repo's pin
def test_crash_install_uses_the_declared_pin_not_the_bare_name(tmp_path, monkeypatch):
    """THE regression. `pip install pytorch-lightning` against a repo pinning 1.5.1 is how
    rubert-dr-0804 acquired Lightning 2.6.5. The declared LINE must reach pip instead."""
    monkeypatch.setitem(deps._PIP_NAME, "pytorch_lightning", "pytorch-lightning")
    asked: list[str] = []

    def fake_install(package, *, python=None, timeout=None):
        asked.append(package)
        return InstallResult(package=package, ok=True, returncode=0)

    eng = _engine(tmp_path / "run", auto_install_deps=True, dep_installer=fake_install)
    eng._repo_spec = {"editables": [{"name": ".", "path": str(_repo(tmp_path))}]}

    assert eng._install_missing(["pytorch_lightning"]) == ["pytorch-lightning"]
    assert asked == ["pytorch_lightning==1.5.1"], "pip was handed a bare name over a repo pin"
    # The receipt an operator reads: which requirement was used, and what it was declared as.
    rec = eng._drain_dep_receipts(["pytorch-lightning"])
    assert rec[0]["requirement"] == "pytorch_lightning==1.5.1"
    assert rec[0]["declared"] == "pytorch_lightning==1.5.1"
    assert rec[0]["package"] == "pytorch-lightning"
    assert eng._drain_dep_receipts(["pytorch-lightning"]) == []   # drained, never re-reported


def test_an_undeclared_package_still_installs_by_bare_name(tmp_path, monkeypatch):
    """Resolving a pin must not become a REQUIREMENT to have one: a repo that never mentions a
    distribution has said nothing about it, and the fresh-box install this module exists for has to
    keep working. `declared` is None on the receipt, which is how the log says "unpinned"."""
    monkeypatch.setitem(deps._PIP_NAME, "faketestlib", "faketestlib")
    asked: list[str] = []

    def fake_install(package, *, python=None, timeout=None):
        asked.append(package)
        return InstallResult(package=package, ok=True, returncode=0)

    eng = _engine(tmp_path / "run", auto_install_deps=True, dep_installer=fake_install)
    eng._repo_spec = {"editables": [{"name": ".", "path": str(_repo(tmp_path))}]}
    assert eng._install_missing(["faketestlib"]) == ["faketestlib"]
    assert asked == ["faketestlib"]
    assert eng._drain_dep_receipts(["faketestlib"])[0]["declared"] is None


def test_no_repo_means_no_declaration_and_byte_identical_old_behaviour(tmp_path, monkeypatch):
    monkeypatch.setitem(deps._PIP_NAME, "faketestlib", "faketestlib")
    asked: list[str] = []
    eng = _engine(tmp_path / "run", auto_install_deps=True,
                  dep_installer=lambda package, **kw: (asked.append(package)
                                                       or InstallResult(package=package, ok=True,
                                                                        returncode=0)))
    assert eng._repo_deps_root() == ""            # a run dir is not a repo
    assert eng._install_missing(["faketestlib"]) == ["faketestlib"]
    assert asked == ["faketestlib"]


# ----------------------------------------------------------- engine: the run-setup decision table
def _settle(tmp_path, *, repo=True, operator=None, auto=True, trust="trusted_local"):
    # `trust_mode="untrusted"` is set AFTER construction on purpose: building an untrusted Engine
    # needs a docker CLI on PATH (EnvironmentRefusal), and the decision under test reads
    # `self.trust_mode` and `self._auto_install_deps` — which is exactly the pair
    # `orchestrator.__init__` derives (`auto_install_deps and trust_mode == "trusted_local"`).
    eng = _engine(tmp_path / "run", auto_install_deps=auto)
    if trust != "trusted_local":
        eng.trust_mode = trust
        eng._auto_install_deps = False
    eng._repo_spec = ({"editables": [{"name": ".", "path": str(_repo(tmp_path))}]} if repo
                      else {"editables": []})
    cmd = eng._settle_declared_deps(list(operator or []))
    rows = _declared_rows(tmp_path / "run")
    return eng, cmd, (rows[-1].data if rows else None)


def test_a_declaring_repo_installs_itself_with_no_operator_configuration(tmp_path):
    """The product promise: the operator did not write a setup command and does not have to."""
    eng, cmd, row = _settle(tmp_path)
    assert cmd[:2] == [getattr(eng.sandbox, "python", None) or cmd[0], "-m"]
    assert cmd[-3:] == ["--no-input", "-r", "requirements.txt"]
    assert row["action"] == "installed"
    # The pins are ON the receipt, which is what makes a version dispute settleable later.
    assert row["pins"]["pytorch-lightning"] == "pytorch_lightning==1.5.1"
    assert row["pin_count"] == 6 and row["pins_truncated"] is False


def test_an_explicit_operator_run_setup_wins_entirely(tmp_path):
    """Not prepended, not merged, not appended. A prepend double-installs for the operator whose
    command already IS `pip install -r requirements.txt`, and an operator who curated an environment
    against the repo's pins would have no spelling left meaning "do not install these"."""
    own = ["bash", "setup.sh"]
    _eng, cmd, row = _settle(tmp_path, operator=own)
    assert cmd == own
    assert row["action"] == "operator_run_setup"
    assert row["command"] == own
    assert row["pins"], "we still RECORD what the repo declares — we just do not act on it"


def test_auto_install_off_is_the_operator_owning_their_environment(tmp_path):
    _eng, cmd, row = _settle(tmp_path, auto=False)
    assert cmd == [] and row["action"] == "auto_install_disabled"


def test_an_untrusted_tier_refuses_out_loud_rather_than_skipping_silently(tmp_path):
    """A Docker tier runs `--network none` and must not mutate a shared image. "We found your pins
    and could not honour them" is exactly what an operator debugging a version mismatch needs told,
    so it is a RECORDED refusal — and distinct from the operator having switched installs off."""
    _eng, cmd, row = _settle(tmp_path, trust="untrusted")
    assert cmd == [] and row["action"] == "refused_untrusted_tier"
    assert row["pins"]["transformers"] == "transformers==4.51.0"


def test_a_repo_that_declares_nothing_says_so(tmp_path):
    _eng, cmd, row = _settle(tmp_path, repo=False)
    assert cmd == [] and row["action"] == "nothing_declared"
    assert row["pins"] == {} and row["file"] == ""


def test_every_action_the_engine_can_take_is_in_the_declared_vocabulary(tmp_path):
    """A bare string reaches the durable log, so a typo'd literal is invisible to every reader.
    Driven from real `ast.Constant`s, not a substring scan — a comment is not an AST node."""
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(EvalDispatchMixin)))
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    literals = set()
    for name in ("_settle_declared_deps", "_sync_node_deps"):
        for node in ast.walk(fns[name]):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.add(node.value)
    used = literals & set(EvalDispatchMixin.DECLARED_DEPS_ACTIONS)
    # Every action the vocabulary declares is actually reachable, and every action string those two
    # methods pass is in the vocabulary (the runtime `assert` in `_settle_declared_deps` is the
    # other half; this catches the `_sync_node_deps` side, which has no such assert).
    assert used == set(EvalDispatchMixin.DECLARED_DEPS_ACTIONS)


def test_the_receipt_is_diagnostic_and_deduped_on_the_facts(tmp_path):
    eng, _cmd, _row = _settle(tmp_path)
    assert EV_DEPS_DECLARED in DIAGNOSTIC_EVENTS      # the fold must never read it
    eng._deps_declaration = None                      # a resume re-observes the tree
    eng._settle_declared_deps([])
    assert len(_declared_rows(tmp_path / "run")) == 1, "an unchanged re-observation is not news"
    # …but a REWRITTEN declaration is: that row is the audit trail for a Developer's change.
    (tmp_path / "repo" / "requirements.txt").write_text("polars\nnumpy>=2\n", encoding="utf-8")
    eng._deps_declaration = None
    eng._settle_declared_deps([])
    rows = _declared_rows(tmp_path / "run")
    assert len(rows) == 2 and rows[-1].data["pins"]["numpy"] == "numpy>=2"


# ------------------------------------------------------ engine: the Developer's deliberate change
def _sync_engine(tmp_path, monkeypatch):
    """An engine whose node re-sync runs a RECORDED fake `_run_argv` instead of pip."""
    ran: list[tuple] = []

    def fake_run_argv(argv, cwd, timeout, log_path=None):
        ran.append((list(argv), str(cwd)))
        return 0, "", "", False

    monkeypatch.setattr("looplab.runtime.sandbox._run_argv", fake_run_argv)
    eng = _engine(tmp_path / "run", auto_install_deps=True)
    eng._repo_spec = {"editables": [{"name": ".", "path": str(_repo(tmp_path))}]}
    eng._settle_declared_deps([])            # establish the run's baseline
    return eng, ran


def test_an_unchanged_declaration_costs_nothing(tmp_path, monkeypatch):
    """Gate 1, and the overwhelmingly common path: every node inherits the file unchanged."""
    eng, ran = _sync_engine(tmp_path, monkeypatch)
    node_wd = tmp_path / "node0"
    node_wd.mkdir()
    (node_wd / "requirements.txt").write_text(_LIVE_SHAPES, encoding="utf-8")
    eng._sync_node_deps(node_wd)
    assert ran == []
    assert len(_declared_rows(tmp_path / "run")) == 1     # no row either — nothing happened


def test_a_rewritten_declaration_is_reinstalled_and_recorded(tmp_path, monkeypatch):
    """The owner's requirement: the Developer rewrites the requirements file and the deps get
    reinstalled. Before this the edit was inert — nothing re-read the file."""
    eng, ran = _sync_engine(tmp_path, monkeypatch)
    node_wd = tmp_path / "node0"
    node_wd.mkdir()
    (node_wd / "requirements.txt").write_text(_LIVE_SHAPES + "numpy>=2\n", encoding="utf-8")
    eng._sync_node_deps(node_wd)

    assert len(ran) == 1
    argv, cwd = ran[0]
    assert argv[-2:] == ["-r", "requirements.txt"] and cwd == str(node_wd)
    row = _declared_rows(tmp_path / "run")[-1].data
    assert row["action"] == "node_resync" and row["pins"]["numpy"] == "numpy>=2"

    # Gate 2: the same file again is a no-op, and so is reverting to one already installed.
    eng._sync_node_deps(node_wd)
    assert len(ran) == 1


def test_the_run_baseline_never_adopts_a_node_s_speculative_edit(tmp_path, monkeypatch):
    """Otherwise the NEXT node's diff reads against a sibling's guess rather than the source tree,
    and a single node's dependency experiment silently becomes the run's."""
    eng, _ran = _sync_engine(tmp_path, monkeypatch)
    base = eng._declared_deps().digest
    node_wd = tmp_path / "node0"
    node_wd.mkdir()
    (node_wd / "requirements.txt").write_text(_LIVE_SHAPES + "numpy>=2\n", encoding="utf-8")
    eng._sync_node_deps(node_wd)
    assert eng._declared_deps().digest == base


def test_variety_is_capped_and_the_refusal_is_recorded(tmp_path, monkeypatch):
    """Gates 1 and 2 bound REPETITION, not variety: an agent appending a distinct comment every
    node makes a new digest every time. The cap terminates that, and the refusal is a row so it
    reads as a bound being hit rather than the edit being ignored."""
    eng, ran = _sync_engine(tmp_path, monkeypatch)
    node_wd = tmp_path / "node0"
    node_wd.mkdir()
    attempts = _MAX_NODE_DEP_SYNCS + 4
    for i in range(attempts):
        (node_wd / "requirements.txt").write_text(f"{_LIVE_SHAPES}# variation {i}\n",
                                                  encoding="utf-8")
        eng._sync_node_deps(node_wd)
    # The run's own baseline counts as the first of the allowance, so distinct node declarations get
    # `_MAX_NODE_DEP_SYNCS - 1`; every further one is refused, and refused VISIBLY.
    assert len(ran) == _MAX_NODE_DEP_SYNCS - 1
    actions = [r.data["action"] for r in _declared_rows(tmp_path / "run")]
    assert actions.count("node_resync_capped") == attempts - (_MAX_NODE_DEP_SYNCS - 1)
    assert actions[-1] == "node_resync_capped"


def test_a_failed_resync_does_not_abort_the_run(tmp_path, monkeypatch):
    """A failed RUN-setup aborts (the operator's environment never came up). A failed node re-sync
    must not: one node's speculative dependency edit did not take, and its own crash then drives the
    normal repair path."""
    monkeypatch.setattr("looplab.runtime.sandbox._run_argv",
                        lambda argv, cwd, timeout, log_path=None: (1, "", "boom", False))
    eng = _engine(tmp_path / "run", auto_install_deps=True)
    eng._repo_spec = {"editables": [{"name": ".", "path": str(_repo(tmp_path))}]}
    eng._settle_declared_deps([])
    node_wd = tmp_path / "node0"
    node_wd.mkdir()
    (node_wd / "requirements.txt").write_text(_LIVE_SHAPES + "numpy>=2\n", encoding="utf-8")
    eng._sync_node_deps(node_wd)              # must not raise
    assert _declared_rows(tmp_path / "run")[-1].data["action"] == "node_resync_failed"


def test_auto_install_off_disables_the_developer_path_too(tmp_path, monkeypatch):
    """One switch, one meaning: `auto_install_deps=false` is "LoopLab may not change my
    environment", and a node-level edit is no exception."""
    eng, ran = _sync_engine(tmp_path, monkeypatch)
    eng._auto_install_deps = False
    node_wd = tmp_path / "node0"
    node_wd.mkdir()
    (node_wd / "requirements.txt").write_text(_LIVE_SHAPES + "numpy>=2\n", encoding="utf-8")
    eng._sync_node_deps(node_wd)
    assert ran == []


def test_a_named_editable_repo_is_read_under_its_own_subdir(tmp_path, monkeypatch):
    """A multi-repo workspace mounts each editable at its `name` subdir, so the file the agent
    edited is at `<workdir>/<name>/requirements.txt` — reading the workspace root would find
    nothing and silently report the Developer's change as no change."""
    eng, ran = _sync_engine(tmp_path, monkeypatch)
    eng._repo_spec = {"editables": [{"name": "lib", "path": str(tmp_path / "repo")}]}
    node_wd = tmp_path / "node0"
    (node_wd / "lib").mkdir(parents=True)
    (node_wd / "lib" / "requirements.txt").write_text(_LIVE_SHAPES + "numpy>=2\n", encoding="utf-8")
    assert eng._node_deps_root(node_wd) == str(node_wd / "lib")
    eng._sync_node_deps(node_wd)
    assert len(ran) == 1 and ran[0][1] == str(node_wd / "lib")
