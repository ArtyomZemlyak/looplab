"""A merge must not silently discard the other parent's commits.

Merge `99438191` resolved a conflict in two TEST files by taking `ours` across the entire tree. Its
result is byte-identical to its first parent, so two commits — a fix (`6df3a4f6`) and the six-test
repair that followed it (`dadbd19c`) — vanished whole, along with the tests that guarded them.
Nothing went red, because a fix and its guards die together: the production code goes back to the
state the tests were written against. It took a hand audit to find, and it was found only because
someone happened to reread the merge.

That shape is cheap to detect and does not need a diff: **a merge whose tree is identical to one
parent's tree, while another parent carries commits that parent does not have.** A real merge
combines two sides, so its tree normally differs from both; a tree equal to one side means the other
side's content was dropped wholesale. A full-history scan of this repo found a SECOND instance with
the same fingerprint — `6982c9cd` (2026-07-19), same empty `# Conflicts:` trailer, same
byte-identical tree, discarding the two training-monitor phase commits with nothing forcing it. It
was repaired 35 minutes later by `33b1aefa`; `99438191` was repaired the next day by `1ee13e14`.
Both repairs are what put those commits' content back at HEAD — the merges themselves are permanent,
so they are listed below as the known baseline rather than being fixable.

Two refinements keep the rule from crying wolf, and both are load-bearing here:

* a discarded commit only counts if it CHANGED something — a commit whose tree equals one of its own
  parents' trees contributed no content, so dropping it dropped nothing. Without this the scan also
  flags `aabe2bda`, whose one "discarded" commit is a no-op merge of a branch that was already
  merged. That is a false positive, and it is pinned below so the filter cannot be removed silently;
* a merge that deliberately keeps one side — a `-s ours` revert of a bad branch — declares itself
  with a `Discards-Parent-Commits:` trailer. There are none in this history today; the trailer exists
  so the next deliberate one does not arrive as a reason to delete this test.

Cost: ONE `git log` invocation for the whole walk, and pure graph work after it. No per-commit
subprocess, no `git diff`, no `git merge-base`.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Fields are \x1f-separated and records \x1e-terminated so a subject or body can contain anything.
_FORMAT = "%H%x1f%T%x1f%P%x1f%ad%x1f%s%x1f%b%x1e"
_GIT_TIMEOUT_S = 120.0

# Opt out for a merge that means to keep one side (a `-s ours` revert). The reason is required so the
# trailer cannot become a reflex.
_DELIBERATE_TRAILER = "Discards-Parent-Commits:"

# The two instances already in this repo's permanent history, with the commit that repaired each.
# A merge cannot be un-made, so these are a frozen baseline: anything NOT in this map is a new one.
_KNOWN = {
    "9943819195311b4e1042bf2de09431a1f7f542f7": "repaired by 1ee13e14 (2026-08-05)",
    "6982c9cd636daf9ad43efbd3c9d0ed1b4eb860f8": "repaired by 33b1aefa (2026-07-19)",
}

# The near-miss the "changed something" filter exists for: `aabe2bda`'s other parent is a merge whose
# tree equals its own second parent's, i.e. a re-merge of an already-merged branch. It contributed no
# content, so nothing was discarded.
_BENIGN_NEAR_MISS = "aabe2bda291caa0068dca391a61c3e1a847993ef"


class _Commit:
    __slots__ = ("sha", "tree", "parents", "date", "subject", "body")

    def __init__(self, record: str):
        sha, tree, parents, date, subject, body = record.split("\x1f")
        self.sha, self.tree, self.parents = sha, tree, parents.split()
        self.date, self.subject, self.body = date, subject, body

    def __repr__(self) -> str:                    # what a failure prints
        return f"{self.sha[:8]} {self.date} {self.subject}"


def _walk(repo: Path) -> dict[str, _Commit]:
    """Every commit reachable from HEAD as {sha: _Commit}, in one `git log`."""
    result = subprocess.run(
        ["git", "-C", str(repo), "log", "--date=short", f"--format={_FORMAT}", "HEAD"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=_GIT_TIMEOUT_S)
    if result.returncode != 0:
        pytest.skip(f"git log unavailable: {result.stderr.strip()[:200]}")
    commits = {}
    for record in result.stdout.split("\x1e"):
        record = record.strip("\n")
        if record.count("\x1f") == 5:
            commit = _Commit(record)
            commits[commit.sha] = commit
    return commits


def _ancestors(root: str, commits: dict[str, _Commit]) -> set[str]:
    seen, stack = set(), [root]
    while stack:
        sha = stack.pop()
        if sha in seen:
            continue
        seen.add(sha)
        commit = commits.get(sha)
        if commit is not None:
            stack.extend(p for p in commit.parents if p not in seen)
    return seen


def _changed_something(commit: _Commit, commits: dict[str, _Commit]) -> bool:
    """False for an empty commit and for a merge that produced one of its parents' trees verbatim."""
    if not commit.parents:
        return True
    return all(commits[p].tree != commit.tree for p in commit.parents if p in commits)


def discarding_merges(commits: dict[str, _Commit]) -> dict[str, tuple[_Commit, list[_Commit]]]:
    """{merge sha: (merge, commits it discarded)} — see the module docstring for the rule."""
    found: dict[str, tuple[_Commit, list[_Commit]]] = {}
    for merge in commits.values():
        if len(merge.parents) < 2:
            continue
        if any(line.strip().startswith(_DELIBERATE_TRAILER)
               for line in merge.body.splitlines()):
            continue
        matching = [p for p in merge.parents
                    if p in commits and commits[p].tree == merge.tree]
        if not matching:
            continue
        # More than one parent can match (both sides ended at identical content). Read it the
        # charitable way — the side that leaves the least discarded — so a tie is never a finding.
        best: list[_Commit] | None = None
        for taken in matching:
            kept = _ancestors(taken, commits)
            dropped = [commits[sha]
                       for other in merge.parents if other != taken
                       for sha in sorted(_ancestors(other, commits) - kept)
                       if sha in commits and _changed_something(commits[sha], commits)]
            if best is None or len(dropped) < len(best):
                best = dropped
        if best:
            found[merge.sha] = (merge, best)
    return found


@pytest.fixture(scope="module")
def history() -> dict[str, _Commit]:
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    shallow = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--is-shallow-repository"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=_GIT_TIMEOUT_S)
    if shallow.stdout.strip() == "true":
        # `actions/checkout` clones depth=1 by default, which leaves no history to check. The guard
        # is a no-op there rather than a false pass on an empty walk; give CI `fetch-depth: 0` to
        # make it live.
        pytest.skip("shallow clone: no merge history to scan")
    return _walk(REPO)


def _report(merge: _Commit, dropped: list[_Commit]) -> str:
    lines = [f"  {merge!r}",
             f"    tree is identical to a parent's; {len(dropped)} commit(s) went in and came out "
             f"with nothing:"]
    lines += [f"      {commit!r}" for commit in dropped]
    return "\n".join(lines)


def test_no_merge_silently_discards_the_other_parents_commits(history):
    """The guard. It names what was dropped, because that is the only thing a reviewer can act on —
    the merge itself is permanent, so the repair is always "re-apply these commits"."""
    found = discarding_merges(history)
    new = {sha: value for sha, value in found.items() if sha not in _KNOWN}
    assert not new, (
        "a merge took one parent's tree whole and dropped the other parent's work "
        "(nothing goes red for this: a fix and the tests guarding it die together)\n"
        + "\n".join(_report(merge, dropped) for merge, dropped in new.values())
        + f"\n\nIf this was deliberate, put a `{_DELIBERATE_TRAILER} <reason>` trailer in the merge "
          "message. Otherwise re-apply the commits above.")


def test_every_baselined_merge_is_still_in_the_history(history):
    """The baseline is not an ignore-list to grow. If a sha here is gone — a rebase, a fresh clone
    with different ids — it is stale and must be removed, not carried."""
    for sha in _KNOWN:
        assert sha in history, f"baselined merge {sha[:8]} is no longer reachable from HEAD"


@pytest.mark.parametrize("sha,expected", [
    ("9943819195311b4e1042bf2de09431a1f7f542f7",
     ["6df3a4f6ffe65c16856c0c5df76bac45c9c9564d", "dadbd19cd9f159cd91bb88695eb33bccf9593e6d"]),
    ("6982c9cd636daf9ad43efbd3c9d0ed1b4eb860f8",
     ["12a0b6ee89d9f991ff48ca371a03e6eb394c9daf", "ed71903397f725384550427e4b164b106b7f77f7"]),
])
def test_the_rule_recognises_both_known_instances(history, sha, expected):
    """Non-vacuity, against the two merges that motivated the rule: it must find them, and it must
    name exactly the commits each one dropped. A guard whose detector silently stopped matching
    would keep passing forever."""
    found = discarding_merges(history)
    assert sha in found, f"{sha[:8]} is no longer recognised as a discarding merge"
    _merge, dropped = found[sha]
    assert sorted(commit.sha for commit in dropped) == sorted(expected), [repr(c) for c in dropped]


def test_a_merge_whose_other_side_added_nothing_is_not_flagged(history):
    """The false positive the "changed something" filter exists for. `aabe2bda` matches the tree half
    of the fingerprint, and its other parent does carry a unique commit — but that commit is a merge
    whose tree equals one of its own parents', a re-merge of a branch that was already in. Dropping
    it dropped no content. Without the filter this is a permanent red test for a healthy merge."""
    if _BENIGN_NEAR_MISS not in history:
        pytest.skip("near-miss merge is not in this history")
    assert _BENIGN_NEAR_MISS not in discarding_merges(history)


# --- the rule itself, on a repo built for the purpose ------------------------------------------
#
# The tests above are pinned to this repo's history, so they skip on a shallow CI checkout — exactly
# where a new bad merge would most like to land. These build the shapes directly and always run.

def _git(repo: Path, *args: str) -> str:
    # Inherit PATH (git is not always in /usr/bin) but never the developer's identity, hooks or
    # templates: a `commit.gpgsign` or an `init.templateDir` in ~/.gitconfig would otherwise decide
    # whether this test can run at all.
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
           "HOME": str(repo)}
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace",
                            timeout=_GIT_TIMEOUT_S, env=env)
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture(scope="module")
def _origin(tmp_path_factory):
    """One repo with a `main`/`side` divergence, built once.

    The merges below cost ~2 git invocations each because of this. Building a repo per test cost
    eight, and the point of the rule is that it is CHEAP to check.
    """
    repo = tmp_path_factory.mktemp("merge-history")
    _git(repo, "init", "-q", "-b", "main", ".")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "branch", "side-origin")
    (repo / "main.txt").write_text("main\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main work")
    _git(repo, "checkout", "-q", "side-origin")
    (repo / "side.txt").write_text("side\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the fix a merge is about to eat")
    _git(repo, "branch", "main-origin", "main")
    return repo


@pytest.fixture
def diverged(_origin, request):
    """`(repo, side_branch)` on a per-test branch PAIR off the shared divergence.

    A merge cannot be undone, so each test needs its own history — but `_walk` follows HEAD, so a
    private branch is isolation enough, and the tests that merge INTO the side branch get their own
    copy of that too.
    """
    tag = "".join(ch if ch.isalnum() else "-" for ch in request.node.name)[:60]
    _git(_origin, "checkout", "-q", "-B", f"side-{tag}", "side-origin")
    _git(_origin, "checkout", "-q", "-B", f"main-{tag}", "main-origin")
    return _origin, f"side-{tag}"


def test_an_ours_merge_that_eats_a_commit_is_caught(diverged):
    repo, side = diverged
    _git(repo, "merge", "-q", "-s", "ours", side, "-m", "Merge branch 'side'")
    found = discarding_merges(_walk(repo))
    assert len(found) == 1, found
    _merge, dropped = next(iter(found.values()))
    assert [commit.subject for commit in dropped] == ["the fix a merge is about to eat"]


def test_a_real_merge_of_the_same_two_sides_is_not_caught(diverged):
    repo, side = diverged
    _git(repo, "merge", "-q", "--no-ff", side, "-m", "Merge branch 'side'")
    assert discarding_merges(_walk(repo)) == {}


def test_a_declared_ours_merge_is_allowed(diverged):
    repo, side = diverged
    _git(repo, "merge", "-q", "-s", "ours", side,
         "-m", f"Revert the side branch\n\n{_DELIBERATE_TRAILER} side was abandoned")
    assert discarding_merges(_walk(repo)) == {}


def test_a_re_merge_of_an_already_merged_branch_is_not_caught(diverged):
    """The `aabe2bda` shape, built rather than found: `side` is merged, `main` is merged back into
    `side`, then `side` is merged again. The last merge's tree equals `main`'s and the other parent
    does carry a unique commit — the whole tree half of the fingerprint — but that commit is a merge
    that introduced no content. Only the "changed something" filter tells these apart."""
    repo, side = diverged
    _git(repo, "merge", "-q", "--no-ff", side, "-m", "Merge branch 'side'")
    head = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    _git(repo, "checkout", "-q", side)
    _git(repo, "merge", "-q", "--no-ff", head, "-m", "Merge branch 'main' into side")
    _git(repo, "checkout", "-q", head)
    _git(repo, "merge", "-q", "--no-ff", side, "-m", "Merge branch 'side' again")
    assert discarding_merges(_walk(repo)) == {}
