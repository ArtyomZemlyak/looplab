"""Doc 79 code identity, review 2026-10-16: the id a log decided never moves, the operator's
repository is only ever READ (whoever owns it, however it is spelled), the seed's modes are carried
only where they are evidence, an overlay is computed only against the run's own commits, and the
git plumbing has one spelling per rule. Real git throughout; each test drives the behaviour."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from looplab.engine.code_identity import (BASE_BRANCH, BASE_REF, _digest, code_status,
                                          injected_code_commit, record_attempt, sync_run)
from looplab.engine.code_repo import CodeRepo, CodeRepoError, is_object_id
from looplab.engine.code_worktree import (OverlayRefusal, add_worktree, describe_worktrees,
                                          overlay_from_patch, overlay_from_worktree)
from looplab.events.eventstore import EventStore
from tests._posix_gates import MODE_BITS
from tests.test_code_identity import (_engine, _git, _history_repo_task, _mode, _repo_task,
                                      _rewrite_rows, _run_from_target_worktree)


def _run(tmp_path, task):
    anyio.run(_engine(tmp_path, task).run)
    rd = tmp_path / "run"
    (rd / "task.snapshot.json").write_text(task.model_dump_json(), encoding="utf-8")   # as the CLI
    return rd


def _restore(rd: Path, to: Path, *, keep_origin: bool = True) -> Path:
    """What a run-mirror restore leaves: everything but the rebuildable repository and worktrees
    (and, with `keep_origin=False`, without the sidecar either)."""
    skip = ["repo.git", "worktrees"] + ([] if keep_origin else ["origin"])
    shutil.copytree(rd, to, ignore=shutil.ignore_patterns(*skip))
    return to


def _fsck_ok(code: CodeRepo) -> bool:
    return subprocess.run(["git", "--git-dir", str(code.path), "fsck", "--no-dangling"],
                          capture_output=True).returncode == 0


def _strip_code_rows(rd: Path) -> None:
    """No `code_snapshot` row was ever appended (a kill between the base and its row, or an
    offline-only sync): each row becomes a type the fold ignores, so the seq stays dense."""
    def strip(row):
        if row["type"] == "code_snapshot":
            row["type"] = "x_code_snapshot_stripped"
        return row
    _rewrite_rows(rd, strip)


# ------------------------------------------------ finding 3 + A6 + S1: a decided id never moves

def test_a_decided_parent_whose_objects_are_gone_keeps_the_same_base_id_as_its_own_cut(tmp_path):
    """The log decided parent X; the rebuild can get neither the operator's repository nor the
    sidecar. It used to write a ROOT base with ANOTHER id — every id in the log then named a commit
    the rebuilt repository cannot produce. Now: the same id, written naming X, as its own shallow
    cut (measured usable: log, fsck), and `code_status` says the history is unavailable."""
    repo, task = _history_repo_task(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").strip()
    rd = _run(tmp_path, task)
    base = CodeRepo(rd).rev(BASE_BRANCH)
    restored = _restore(rd, tmp_path / "restored", keep_origin=False)
    repo.rename(tmp_path / "moved-away")
    sync_run(restored)
    code = CodeRepo(restored)
    assert code.rev(BASE_BRANCH) == base                  # hash-object == fast-import, byte for byte
    assert code.commit_parents(base) == [head] and not code.has_object(head)
    assert code.shallow() == [base] and _fsck_ok(code)
    assert code.git("log", "--format=%H", BASE_BRANCH).stdout.decode().split() == [base]
    row = code_status(restored)["bases"][0]
    assert (row["origin_parent"], row["history"], row["history_reason"]) == (
        head, "unavailable", "origin_objects_missing")
    # A worktree on it works, and a node attempt on it records.
    assert Path(add_worktree(restored, "w")["path"]).joinpath("train.py").is_file()


def test_a_run_with_no_recorded_row_rebuilds_the_same_parented_base_from_its_own_files(tmp_path):
    """A6: `sync_run` appends no event, so a run whose rows were never appended decided nothing in
    the log; the rebuild PROBED the (gone) source and wrote a root before reading the mirrored
    sidecar. The run's own `code/origin/decision.json` — or, for a run built before it existed, a
    sidecar for its start commit — is that decision."""
    repo, task = _history_repo_task(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").strip()
    rd = _run(tmp_path, task)
    base = CodeRepo(rd).rev(BASE_BRANCH)
    _strip_code_rows(rd)
    assert json.loads((rd / "code/origin/decision.json").read_text())["parent"] == head
    first = _restore(rd, tmp_path / "first")
    legacy = _restore(rd, tmp_path / "legacy")
    (legacy / "code/origin/decision.json").unlink()        # built before the decision file
    repo.rename(tmp_path / "moved-away")
    for restored in (first, legacy):
        sync_run(restored)
        code = CodeRepo(restored)
        assert code.rev(BASE_BRANCH) == base, restored.name
        assert code.shallow() == [head] and _fsck_ok(code), restored.name   # the sidecar's cut


def test_a_clean_checkout_sidecar_carries_the_root_tree(tmp_path):
    """S1: seed tree == the operator commit's tree (a clean checkout, nothing untracked). The
    sidecar subtracted the base's ROOT tree too, held the commit alone, and a rebuild without the
    source read it as incomplete (it runs before the base is written) — a root with another id."""
    import sys
    from looplab.adapters.repo_task import EvalSpec, RepoTask
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "train.py").write_text("print('LR: 0.01')\n")
    (repo / "score.py").write_text('import json\nprint(json.dumps({"metric": 1.0}))\n')
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    head = _git(repo, "rev-parse", "HEAD").strip()
    task = RepoTask(id="p", direction="max", editable_path=str(repo), protect=["score.py"],
                    edit_surface=["train.py"], eval=EvalSpec(
                        command=[sys.executable, "score.py"],
                        metric={"kind": "stdout_json", "key": "metric"}))
    rd = _run(tmp_path, task)
    code = CodeRepo(rd)
    base = code.rev(BASE_BRANCH)
    assert code.tree(base) == code.tree(head)              # the clean case
    restored = _restore(rd, tmp_path / "restored")
    (restored / "code/origin/decision.json").unlink()      # the sidecar alone must carry it
    repo.rename(tmp_path / "moved-away")
    sync_run(restored)
    rebuilt = CodeRepo(restored)
    assert rebuilt.rev(BASE_BRANCH) == base
    # The parent came from the sidecar (a root commit: no cut needed), not as a cut at the base.
    assert rebuilt.has_object(head) and rebuilt.shallow() == [] and _fsck_ok(rebuilt)


def test_the_eval_path_fetch_runs_outside_the_repository_lock_on_one_budget(tmp_path, monkeypatch):
    """T2 / finding 8: the first base's history fetch ran under `repo.lock()` (blocking every
    worktree, overlay and sync of the run) with 60 s PER depth. It now runs before the lock, and
    both depths share one `FETCH_BUDGET_S`."""
    from looplab.engine import code_origin
    from looplab.engine.code_repo import _lock_for
    repo, task = _history_repo_task(tmp_path)
    rd = _run(tmp_path, task)
    _strip_code_rows(rd)
    restored = _restore(rd, tmp_path / "restored", keep_origin=False)
    real = CodeRepo.fetch_readonly
    clock = [0.0]
    seen: list = []

    def fetch(self, source, refspecs, **kw):
        lock = _lock_for(self.path)
        seen.append((kw.get("depth"), round(kw["timeout"], 1), lock.acquire(blocking=False)))
        lock.release()
        clock[0] += 29.5                                   # a fetch that spent most of the budget
        raise CodeRepoError("git_timeout", "slow")
    monkeypatch.setattr(code_origin.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(CodeRepo, "fetch_readonly", fetch)
    events = EventStore(restored / "events.jsonl").read_all()
    seed = next(e for e in events if e.type == "workspace_seeded")
    payload = record_attempt(restored, events, node_id=0, generation=0, seed_event_seq=seed.seq,
                             base_digest=_digest(seed.data["base_revision"]), changes={},
                             source="workspace", origin_source=str(repo))
    assert seen == [(code_origin.HISTORY_DEPTH, code_origin.FETCH_BUDGET_S, True)], seen
    assert payload["base"]["origin"]["reason"] == "origin_parent_unavailable"
    monkeypatch.setattr(CodeRepo, "fetch_readonly", real)


# ---------------------------------------------------------------- finding 4: the run's version

def test_a_resumed_version_1_run_stamps_its_new_rows_version_1(tmp_path):
    """A version-1 run resumed under this build wrote new rows as version 2 (`_payload_base`
    stamped the build's version), and the run's own next rebuild then disagreed with itself."""
    from looplab.engine.code_origin import run_version
    _, task = _repo_task(tmp_path)
    rd = _run(tmp_path, task)

    def as_v1(row):
        if row["type"] == "code_snapshot":
            row["data"]["version"] = 1
            row["data"]["status"] = "unavailable"           # ANY status decides the version
        return row
    _rewrite_rows(rd, as_v1)
    events = EventStore(rd / "events.jsonl").read_all()
    seed = next(e for e in events if e.type == "workspace_seeded")
    payload = record_attempt(rd, events, node_id=7, generation=0, seed_event_seq=seed.seq,
                             base_digest=_digest(seed.data["base_revision"]),
                             changes={"recipe.env": (b"SEED=9\n", False)}, source="log",
                             parent_ids=[7, 0])
    assert payload["version"] == 1 and "co_parents" not in payload
    # No row at all: the repository's stamp decides; an unstamped one with bases is version 1.
    code = CodeRepo(rd)
    assert run_version([], 2, code) == 2
    code.git("config", "--unset", "looplab.identityVersion")
    assert run_version([], 2, code) == 1 and run_version([], 2, None) == 2


# ------------------------------------------------------- finding 5: a start must be the run's

def test_a_start_outside_the_runs_commits_is_refused(tmp_path):
    """The repository holds the operator's fetched history since version 2. An overlay from an
    operator commit was diffed against it and materialized over the active base — silently
    reverting the uncommitted edits the base holds."""
    repo, task = _history_repo_task(tmp_path)
    rd = _run(tmp_path, task)
    head = _git(repo, "rev-parse", "HEAD").strip()
    code = CodeRepo(rd)
    assert code.has_object(head)
    for call in (lambda: add_worktree(rd, "op", start=head),
                 lambda: overlay_from_patch(rd, "--- a/x\n+++ b/x\n", start=head[:12])):
        with pytest.raises(OverlayRefusal) as refused:
            call()
        assert refused.value.code == "code_start_not_run_commit"
    base = code.rev(BASE_BRANCH)
    assert add_worktree(rd, "ok", start=base)["start_commit"] == base


# ------------------------------------------------- finding 6: the default location is not a link

def test_a_link_planted_at_the_default_worktree_location_is_refused(tmp_path):
    repo, task = _repo_task(tmp_path)
    rd = _run(tmp_path, task)
    elsewhere = repo / "empty-dir"                       # inside the OPERATOR's source tree
    elsewhere.mkdir()
    (rd / "code" / "worktrees").mkdir(parents=True, exist_ok=True)
    (rd / "code" / "worktrees" / "evil").symlink_to(elsewhere)
    with pytest.raises(OverlayRefusal) as refused:
        add_worktree(rd, "evil")
    assert refused.value.code == "code_worktree_path_refused"
    assert list(elsewhere.iterdir()) == []
    # The whole `code/worktrees` directory redirected: refused too.
    shutil.rmtree(rd / "code" / "worktrees")
    (rd / "code" / "worktrees").symlink_to(elsewhere)
    with pytest.raises(OverlayRefusal):
        add_worktree(rd, "fine")
    assert list(elsewhere.iterdir()) == []


# ---------------------------------------- finding 7: merge parents only for code that is there

def test_a_lifecycle_after_an_eval_reset_keeps_the_injected_head_only_with_the_same_code():
    created = SimpleNamespace(type="node_created", seq=1, data={
        "node_id": 4, "generation": 0, "files": {"a.py": "x\n"}, "code_commit": "a" * 40})
    reset = SimpleNamespace(type="node_reset", seq=2, data={"node_id": 4, "from_stage": "eval"})
    events = [created, reset]
    assert injected_code_commit(events, 4, 0) == "a" * 40
    assert injected_code_commit(events, 4, 1, {"a.py": "x\n"}, []) == "a" * 40
    assert injected_code_commit(events, 4, 1, {"a.py": "rebuilt\n"}, []) is None
    assert injected_code_commit(events, 4, 1) is None              # code unknown: no claim
    sha256 = "b" * 64
    created.data["code_commit"] = sha256
    assert injected_code_commit(events, 4, 0) == sha256


def test_a_dirty_worktree_claims_no_merge_head(tmp_path):
    _, task = _repo_task(tmp_path)
    rd = _run(tmp_path, task)
    wt = Path(add_worktree(rd, "w")["path"])
    (wt / "recipe.env").write_text("SEED=3\n")
    _git(wt, "commit", "-qam", "seed 3")
    clean = overlay_from_worktree(rd, "w")
    assert clean["head_commit"] == _git(wt, "rev-parse", "HEAD").strip()
    (wt / "recipe.env").write_text("SEED=1\n")             # uncommitted revert of the commit
    dirty = overlay_from_worktree(rd, "w")
    assert "head_commit" not in dirty and dirty["head_commit_withheld"] == "uncommitted_changes"


def test_a_recorded_merge_head_missing_from_the_repository_is_not_built_without_it(tmp_path):
    """S4: a rebuild before the agent backup is restored met a `code_commit` it does not hold and
    wrote the attempt WITHOUT its merge parents, the ref pinning that. It is left unbuilt now."""
    _, task = _repo_task(tmp_path)
    rd = _run(tmp_path, task)
    events = EventStore(rd / "events.jsonl").read_all()
    seed = next(e for e in events if e.type == "workspace_seeded")
    payload = record_attempt(rd, events, node_id=9, generation=0, seed_event_seq=seed.seq,
                             base_digest=_digest(seed.data["base_revision"]), changes={},
                             source="log", code_commit="c" * 40)
    assert (payload["status"], payload["reason"]) == ("unavailable", "merge_head_missing")
    assert CodeRepo(rd).rev("refs/heads/nodes/n9") is None


# ---------------------------------------------------------- findings 1 + 2: modes in the export

def test_carry_compares_content_and_resolves_the_mode_apart():
    """Finding 1, the rule as a table: a deletion needs the CONTENT the run deleted (a target
    chmod is no content), both-deleted and bytes-already-there-with-no-mode-to-move are
    `already`, and a mode that must still move is a set of the target's own blob."""
    from looplab.engine.code_export import _carry
    f, x = ("100644", "a"), ("100755", "a")

    def never():
        pytest.fail("no bytes are read for these")
    assert _carry(None, f, None, x, never, never) == ("set", None)            # target chmod'ed it
    assert _carry(None, f, None, ("100644", "b"), never, never) == ("conflict", None)
    assert _carry(None, f, None, None, never, never) == ("already", None)
    assert _carry(None, ("100644", "o"), f, x, never, never) == ("already", None)  # target's mode
    assert _carry(None, f, x, f, never, never) == ("set", ("100755", "a"))     # chmod to carry
    assert _carry(None, f, x, x, never, never) == ("already", None)


@MODE_BITS
def test_a_seed_off_a_filesystem_without_modes_exports_no_mass_chmod(tmp_path):
    """Finding 2: vfat / NTFS / CIFS / 9p / `/mnt/c` report 0755 for every file, and `git init`
    there writes `core.fileMode=false`. The export trusted the seed's bits whenever the TARGET
    tracked modes — `chmod +x` on every file. The SOURCE's answer is asked too."""
    def modeless(src):
        for name in ("train.py", "recipe.env", "score.py"):
            os.chmod(src / name, 0o755)
        _git(src, "config", "extensions.worktreeConfig", "true")
        _git(src, "config", "--worktree", "core.fileMode", "false")   # the source's own config
    target, rd, eng = _run_from_target_worktree(tmp_path, modeless)
    (rd / "task.snapshot.json").write_text(eng.task.model_dump_json(), encoding="utf-8")
    report = export_code_safe(rd, target, "nomass")
    step = report["steps"][0]
    assert step["file_mode"] == {"trusted": False, "target": True, "source": False}, step
    assert step["changed"] == 2, step                       # train.py bytes, score.py added
    for name in ("train.py", "recipe.env", "score.py"):
        assert _mode(target, "nomass", name) == "100644", name


def export_code_safe(rd, target, branch):
    from looplab.engine.code_export import export_code
    report = export_code(rd, target, branch=branch, node="none")
    assert report["status"] == "created", report
    return report


# ------------------------------------------- finding 8 + T1 + S3: foreign branches, read-only

def _foreign_run(tmp_path, monkeypatch, *, history_depth=None):
    """An operator repository with a 5-line recipe and two commits (c1, then c2 changing line 5),
    an uncommitted `train.py` edit, and a run on it."""
    import sys
    from looplab.adapters.repo_task import EvalSpec, RepoTask
    from looplab.engine import code_origin
    if history_depth is not None:
        monkeypatch.setattr(code_origin, "HISTORY_DEPTH", history_depth)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "recipe.env").write_text("A=1\nB=2\nC=3\nD=4\nE=5\n")
    (repo / "train.py").write_text("print('LR: 0.01')\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "c1")
    (repo / "recipe.env").write_text("A=1\nB=2\nC=3\nD=4\nE=50\n")
    _git(repo, "commit", "-qam", "c2")
    (repo / "train.py").write_text("print('LR: 0.02')\n")
    (repo / "score.py").write_text('import json\nprint(json.dumps({"metric": 1.0}))\n')
    task = RepoTask(id="p", direction="max", editable_path=str(repo), protect=["score.py"],
                    edit_surface=["train.py", "recipe.env"], eval=EvalSpec(
                        command=[sys.executable, "score.py"],
                        metric={"kind": "stdout_json", "key": "metric"}))
    return repo, _run(tmp_path, task)


def test_a_branch_forked_before_the_shallow_cut_still_finds_its_merge_base(tmp_path, monkeypatch):
    """T1: version 2 made the run repository SHALLOW at the operator's HEAD; a branch cut from an
    OLDER commit had no merge base there and was refused, though the histories share c1. The merge
    base is found in the foreign repository itself, read-only."""
    from looplab.engine.code_foreign import overlay_from_git
    repo, rd = _foreign_run(tmp_path, monkeypatch, history_depth=1)
    c1 = _git(repo, "rev-parse", "HEAD~1").strip()
    assert CodeRepo(rd).shallow() == [_git(repo, "rev-parse", "HEAD").strip()]
    wt = tmp_path / "claude-wt"
    _git(repo, "worktree", "add", "-q", "-b", "old-fork", str(wt), c1)
    (wt / "recipe.env").write_text("A=10\nB=2\nC=3\nD=4\nE=5\n")
    _git(wt, "commit", "-qam", "agent: A=10")
    out = overlay_from_git(rd, {"repo": str(repo), "ref": "old-fork"})
    assert out["foreign"]["merge_base"] == c1
    assert out["files"] == {"recipe.env": "A=10\nB=2\nC=3\nD=4\nE=50\n"}


def test_a_shallow_clone_and_a_subdirectory_are_foreign_sources_too(tmp_path, monkeypatch):
    """Finding 8 + S3: a `--depth 1` clone (fetching from it without `--update-shallow` was
    rejected, reported as a failed fetch) and `git.repo` naming a SUBDIRECTORY (resolved, then
    the fetch failed) both merge — and neither holds the run repository's lock while fetching."""
    from looplab.engine.code_foreign import overlay_from_git
    from looplab.engine.code_repo import _lock_for
    repo, rd = _foreign_run(tmp_path, monkeypatch)
    clone = tmp_path / "codex-clone"
    _git(tmp_path, "clone", "-q", "--depth", "1", f"file://{repo}", str(clone))
    (clone / "recipe.env").write_text("A=11\nB=2\nC=3\nD=4\nE=50\n")
    _git(clone, "commit", "-qam", "codex: A=11")
    (clone / "sub").mkdir()
    held, done = threading.Event(), threading.Event()
    lock = _lock_for(CodeRepo(rd).path)

    def hold():
        with lock:
            held.set()
            done.wait(120)
    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(10)
    try:
        for asked in (clone, clone / "sub"):
            out = overlay_from_git(rd, {"repo": str(asked), "ref": "HEAD"})
            assert out["files"] == {"recipe.env": "A=11\nB=2\nC=3\nD=4\nE=50\n"}, asked
            assert out["foreign"]["repo"] == str(clone)
    finally:
        done.set()
        holder.join()


# ----------------------------------------------------- finding 9 + S2: the operator's source

def test_a_tilde_source_probes_as_the_path_it_names(tmp_path, monkeypatch):
    from looplab.engine.code_origin import probe
    repo, _ = _repo_task(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").strip()
    monkeypatch.setenv("HOME", str(tmp_path))
    assert probe("~/repo", head) is None
    assert probe(str(repo / "."), head) is None            # a spelling, not a subdirectory


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0,
                    reason="needs root to give the source to another uid")
def test_a_source_owned_by_another_user_is_read_and_its_tree_refused(tmp_path):
    """Finding 9 + S2: hermetic git (global config = /dev/null) refused a source another uid owns
    as "dubious ownership": the probe said `editable_not_git`, a fetch from it failed, and the
    operator's working tree dropped out of the roots a worktree `path` may not enter."""
    from looplab.engine.code_origin import probe
    from looplab.engine.code_worktree import _worktree_location
    repo, task = _history_repo_task(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").strip()
    rd = _run(tmp_path, task)
    (repo / "elsewhere").mkdir()
    for root, dirs, files in os.walk(repo):
        for name in dirs + files:
            os.chown(os.path.join(root, name), 65534, 65534)
    os.chown(repo, 65534, 65534)
    plain = subprocess.run(["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
                           capture_output=True, env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull})
    assert b"dubious ownership" in plain.stderr               # the failure this guards against
    assert probe(str(repo), head) is None
    fresh = CodeRepo(tmp_path / "other-run").ensure()
    fresh.fetch_readonly(repo, [head], depth=1)                # upload-pack reads it too
    assert fresh.has_object(head + "^{tree}")
    with pytest.raises(OverlayRefusal) as refused:
        _worktree_location(rd, "w", str(repo / "elsewhere" / "w"))
    assert refused.value.code == "code_worktree_path_refused"


# ---------------------------------------------------------- finding 10 + 11: plumbing, one rule

def test_one_object_id_rule_and_sha256_shallow_lines(tmp_path):
    assert is_object_id("a" * 40) and is_object_id("b" * 64)
    assert not any(is_object_id(v) for v in ("A" * 40, "a" * 41, "a" * 39, None, 7, "g" * 40))
    code = CodeRepo(tmp_path / "run").ensure()
    (code.path / "shallow").write_text("c" * 64 + "\n")
    code.add_shallow(["d" * 40])
    assert code.shallow() == ["c" * 64, "d" * 40]
    assert not (code.path / "shallow.lock").exists()
    with pytest.raises(CodeRepoError):
        code.write_overlay_commit("refs/heads/x", "", {}, message="m\n", when=0, merges=["e" * 40])


def test_describe_skips_a_worktree_row_without_a_path(tmp_path):
    rd = tmp_path / "run"
    meta = rd / "code" / "worktrees" / ".meta"
    meta.mkdir(parents=True)
    (meta / "torn.json").write_text(json.dumps({"name": "torn", "path": ""}))
    row = describe_worktrees(rd)[0]
    assert (row["exists"], row["head"], row["unreadable"]) == (False, None, "no_path")


def test_a_sidecar_lists_objects_whatever_bytes_a_path_holds(tmp_path):
    """`rev-list --objects` prints `<id> <path>` (the path cut at a newline, NOT at a carriage
    return), and `splitlines` split a path holding `\\r` into a bogus row `pack-objects` refused:
    no sidecar. `--no-object-names` keeps one id per line."""
    from looplab.engine.code_origin import write_sidecar
    code = CodeRepo(tmp_path / "run").ensure()
    parent = code.write_tree_commit("refs/heads/op", [("odd\rname.txt", b"1\n", False),
                                                     ("a.py", b"op\n", False)],
                                    message="op\n", when=1)
    base = code.write_tree_commit(BASE_REF + "f" * 64, [("a.py", b"seed\n", False)],
                                  message="base\n", when=2, parent=parent)
    assert write_sidecar(code, tmp_path / "run", parent, base) == "written"


def test_commit_tree_keeps_the_fixed_identity_ids(tmp_path):
    """The synthetic merge inputs moved from code_worktree's private env to `CodeRepo.commit_tree`:
    the same tree, parents and message still give the same commit id as the literal old call."""
    from looplab.engine.code_repo import run_git
    code = CodeRepo(tmp_path / "run").ensure()
    tree = code.tree(code.write_tree_commit("refs/heads/t", [("a", b"a\n", False)],
                                            message="t\n", when=1))
    old_env = {"GIT_AUTHOR_NAME": "LoopLab", "GIT_AUTHOR_EMAIL": "looplab@invalid",
               "GIT_COMMITTER_NAME": "LoopLab", "GIT_COMMITTER_EMAIL": "looplab@invalid",
               "GIT_AUTHOR_DATE": "@0 +0000", "GIT_COMMITTER_DATE": "@0 +0000"}
    root = run_git("--git-dir", str(code.path), "commit-tree", tree, "-m", "r",
                   env_extra=old_env).stdout.decode().strip()
    child = run_git("--git-dir", str(code.path), "commit-tree", tree, "-m", "c", "-p", root,
                    env_extra=old_env).stdout.decode().strip()
    assert code.commit_tree(tree, (), "r") == root
    assert code.commit_tree(tree, [root], "c") == child
