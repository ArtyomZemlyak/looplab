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
    assert json.loads((out / RO_CRATE_METADATA).read_text()) == meta


def test_the_summary_row_carries_what_a_reviewer_reads_first(tmp_path):
    rd = _run(tmp_path)
    export_bundle(rd, tmp_path / "b")
    summary = json.loads((tmp_path / "b" / "summary.json").read_text())
    assert summary["champion"] is not None and summary["best_metric"] is not None
    assert summary["best_metric_caveats"] == [] and summary["mislead_gap"]["gap"] == 0.0
    assert summary["seeds"] == {"confirm_seed_base": 5, "LOOPLAB_EVAL_SEED": "11"}
    claims = json.loads((tmp_path / "b" / "claims.json").read_text())
    assert "memos" in claims and "plan" in claims
    champion = (tmp_path / "b" / "champion" / "solution.py").read_text()
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
