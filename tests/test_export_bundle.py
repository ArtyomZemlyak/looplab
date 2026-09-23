"""The reviewer bundle (doc 52 row 23): a run's seeds, traces, code, claims and record as an
RO-Crate, every file described with its size and SHA-256 — driven over a real toy run."""
from __future__ import annotations

import hashlib
import json

import anyio

from looplab.engine.bundle import RO_CRATE_METADATA, export_bundle, verify_bundle
from tests.factories import make_engine


def _run(tmp_path):
    eng = make_engine(tmp_path / "run", n_seeds=1, max_nodes=2)
    anyio.run(eng.run)
    # a bare `Engine(...)` writes no launch snapshots (the CLI does); plant the two the bundle copies
    (tmp_path / "run" / "config.snapshot.json").write_text(json.dumps({"confirm_seed_base": 5,
                                                                        "eval_env": {"LOOPLAB_EVAL_SEED": "11"}}))
    (tmp_path / "run" / "task.snapshot.json").write_text(json.dumps({"kind": "quadratic", "id": "toy"}))
    return tmp_path / "run"


def test_the_bundle_packages_the_record_and_describes_every_file(tmp_path):
    rd = _run(tmp_path)
    (rd / "mlebench_extras.json").write_text('{"status": "ok"}')
    meta = export_bundle(rd, tmp_path / "bundle")
    out = tmp_path / "bundle"
    files = {e["@id"]: e for e in meta["@graph"] if e.get("@type") == "File"}
    for name in ("events.jsonl", "config.snapshot.json", "task.snapshot.json", "champion/solution.py",
                 "claims.json", "summary.json", "mlebench_extras.json"):
        assert name in files and (out / name).is_file(), name
    assert (out / "events.jsonl").read_bytes() == (rd / "events.jsonl").read_bytes(), "the log is copied, never rewritten"
    for rel, entity in files.items():
        data = (out / rel).read_bytes()
        assert entity["contentSize"] == len(data) and entity["sha256"] == hashlib.sha256(data).hexdigest()
    root = next(e for e in meta["@graph"] if e["@id"] == "./")
    assert {p["@id"] for p in root["hasPart"]} == set(files)
    assert meta["@context"].startswith("https://w3id.org/ro/crate/1.1")
    assert verify_bundle(out) == []
    assert json.loads((out / RO_CRATE_METADATA).read_text(encoding="utf-8")) == meta


def test_the_summary_row_carries_what_a_reviewer_reads_first(tmp_path):
    rd = _run(tmp_path)
    export_bundle(rd, tmp_path / "b")
    summary = json.loads((tmp_path / "b" / "summary.json").read_text(encoding="utf-8"))
    assert summary["champion"] is not None and summary["best_metric"] is not None
    assert summary["best_metric_caveats"] == [] and summary["mislead_gap"]["gap"] == 0.0
    assert summary["seeds"] == {"confirm_seed_base": 5, "LOOPLAB_EVAL_SEED": "11"}
    claims = json.loads((tmp_path / "b" / "claims.json").read_text(encoding="utf-8"))
    assert "memos" in claims and "plan" in claims
    champion = (tmp_path / "b" / "champion" / "solution.py").read_text(encoding="utf-8")
    assert champion.strip(), "the champion's code, off the folded record"


def test_verify_sees_a_tampered_or_missing_file(tmp_path):
    rd = _run(tmp_path)
    export_bundle(rd, tmp_path / "b")
    (tmp_path / "b" / "champion" / "solution.py").write_text("print('edited after export')\n")
    (tmp_path / "b" / "claims.json").unlink()
    defects = verify_bundle(tmp_path / "b")
    assert any(d.startswith("size mismatch champion/solution.py") or d.startswith("digest mismatch champion/solution.py")
               for d in defects), defects
    assert "missing claims.json" in defects


def test_the_command_writes_and_verifies(tmp_path):
    from typer.testing import CliRunner

    from looplab.cli import app

    rd = _run(tmp_path)
    result = CliRunner().invoke(app, ["export-bundle", str(rd)])
    assert result.exit_code == 0, result.output
    assert "verified: every file matches" in result.output and (rd / "bundle" / RO_CRATE_METADATA).is_file()
    missing = CliRunner().invoke(app, ["export-bundle", str(tmp_path / "none")])
    assert missing.exit_code != 0


# ---- the champion's files are agent-authored names (review 2026-09-22, ENG3-15) ------------------

def _agent_champion(tmp_path, files):
    """A toy run whose champion is a node an agent wrote `files` for — appended with a metric that
    wins on the run's own direction, so `state.best()` is exactly that node."""
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold

    rd = _run(tmp_path)
    store = EventStore(rd / "events.jsonl")
    state = fold(store.read_all())
    best = state.best().metric
    winning = best - 1.0 if state.direction == "min" else best + 1.0
    node_id = max(state.nodes) + 1
    store.append("node_created", {"node_id": node_id, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft"}, "code": "print('champion')",
                                  "files": files})
    store.append("node_evaluated", {"node_id": node_id, "generation": 0, "metric": winning,
                                    "eval_seconds": 1.0, "extra_metrics": {}, "stdout_tail": "",
                                    "trials": [], "violations": []})
    assert fold(store.read_all()).best().id == node_id
    return rd


def test_a_champion_file_can_never_land_outside_its_directory(tmp_path):
    rd = _agent_champion(tmp_path, {"ok/helper.py": "fine", "../escape.py": "x",
                                    "sub/../../escape2.py": "x", "/abs.py": "x",
                                    "ok\\..\\..\\escape3.py": "x"})
    out = tmp_path / "bundle"
    meta = export_bundle(rd, out)
    files = {e["@id"] for e in meta["@graph"] if e.get("@type") == "File"}
    assert (out / "champion" / "ok" / "helper.py").read_text() == "fine"
    assert "champion/ok/helper.py" in files
    for leaked in ("escape.py", "escape2.py", "escape3.py", "abs.py"):
        assert not (tmp_path / leaked).exists() and not (out / leaked).exists(), leaked
    assert not any("escape" in f or "abs.py" in f for f in files), files
    assert verify_bundle(out) == []


def test_a_solution_py_key_never_overwrites_the_champions_code(tmp_path):
    """The crate called `champion/solution.py` "the champion's code, off the folded record" and then
    overwrote it with a FILE of that name — while listing both."""
    rd = _agent_champion(tmp_path, {"solution.py": "print('not the champion')", "b.py": "b"})
    out = tmp_path / "bundle"
    meta = export_bundle(rd, out)
    assert (out / "champion" / "solution.py").read_text() == "print('champion')"
    ids = [e["@id"] for e in meta["@graph"] if e.get("@type") == "File"]
    assert ids.count("champion/solution.py") == 1, ids
    assert verify_bundle(out) == []


def test_a_champion_directory_that_is_a_link_is_refused_not_followed(tmp_path):
    import os

    import pytest

    rd = _agent_champion(tmp_path, {"a.py": "a"})
    out, elsewhere = tmp_path / "bundle", tmp_path / "elsewhere"
    out.mkdir()
    elsewhere.mkdir()
    try:
        os.symlink(elsewhere, out / "champion", target_is_directory=True)
    except (OSError, NotImplementedError):          # a Windows box without the symlink privilege
        pytest.skip("this host cannot create a directory symlink")
    with pytest.raises(ValueError, match="resolves outside the bundle"):
        export_bundle(rd, out)
    assert list(elsewhere.iterdir()) == [], "nothing was written through the link"


def test_a_linked_component_inside_the_champion_is_not_written_through(tmp_path):
    """The escape a LEXICAL check cannot see: `link/x.py` has no `..` and is not absolute, but `link`
    is a symlink out of the bundle. Resolved containment (`core/pathsafe.py::contained_member`)
    refuses it; the check this replaced wrote through it."""
    import os

    import pytest

    rd = _agent_champion(tmp_path, {"link/x.py": "leaked", "a.py": "a"})
    out, elsewhere = tmp_path / "bundle", tmp_path / "elsewhere"
    (out / "champion").mkdir(parents=True)
    elsewhere.mkdir()
    try:
        os.symlink(elsewhere, out / "champion" / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create a directory symlink")
    export_bundle(rd, out)
    assert not (elsewhere / "x.py").exists(), "a champion file was written through a link"
    assert (out / "champion" / "a.py").read_text() == "a"


def test_contained_member_truth_table(tmp_path):
    from looplab.core.pathsafe import contained_member

    root = tmp_path / "root"
    root.mkdir()
    assert contained_member(root, "a/b.py") == (root / "a" / "b.py").resolve()
    assert contained_member(root, "a\\b.py") == (root / "a" / "b.py").resolve()
    for bad in ("", "..", "../x", "a/../../x", "/etc/passwd", "a\\..\\..\\x", "."):
        assert contained_member(root, bad) is None, bad
