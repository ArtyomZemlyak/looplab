"""PART IV cross-run Step 1 / CR0 (§21.20.3) — scope_profile / run_facts + the deterministic-rebuild gate.

Pins the run PASSPORT + FACTS contracts and the CR0 acceptance property (§21.20.10): the index is a PURE
projection of the append-only logs, so rebuilding it from scratch — twice, in any run order — yields a
byte-identical result. No new source of truth; everything folds from events + task.snapshot.json.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import orjson

from looplab.engine.cross_run_index import (
    build_index, build_index_incremental, load_index, rebuild_index_from_run_root, run_facts,
    run_source_digest, save_index, scope_profile,
)
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


def _canon(obj) -> bytes:
    return orjson.dumps(obj, option=orjson.OPT_SORT_KEYS)


# --------------------------------------------------------------------------- #
# Passport
# --------------------------------------------------------------------------- #

def test_scope_profile_is_universal_and_deterministic():
    a = scope_profile(task_id="t", kind="dataset", direction="max",
                      goal="плотный поиск по русским отзывам", metric="recall")
    b = scope_profile(task_id="t", kind="dataset", direction="max",
                      goal="плотный поиск по русским отзывам", metric="recall")
    assert a == b                                             # deterministic
    assert a["task_id"] == "t" and a["direction"] == "max" and a["metric"] == "recall"
    assert "русским" in a["goal_terms"]                      # universal: Cyrillic not dropped
    assert a["fingerprint"] == b["fingerprint"]


# --------------------------------------------------------------------------- #
# Facts
# --------------------------------------------------------------------------- #

def _run(tmp_path, run_id="r1", task_id="t"):
    s = EventStore(tmp_path / f"{run_id}.jsonl")
    s.append("run_started", {"run_id": run_id, "task_id": task_id, "goal": "dense retrieval", "direction": "max"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {"lr": 0.1}, "theme": "x"}})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.80})
    s.append("node_created", {"node_id": 1, "parent_ids": [0], "operator": "improve",
                              "idea": {"operator": "improve", "params": {"lr": 0.2}, "theme": "x"}})
    s.append("node_evaluated", {"node_id": 1, "metric": 0.85})
    s.append("node_concepts", {"node_id": 1, "concepts": ["hard-neg"], "mode": "llm"})
    return s


def test_run_facts_projects_attempts_and_best(tmp_path):
    st = fold(_run(tmp_path).read_all())
    f = run_facts(st, kind="dataset", metric="recall")
    assert f["run_id"] == "r1" and f["n_attempts"] == 2
    assert [a["node_id"] for a in f["attempts"]] == [0, 1]    # node-id order (deterministic)
    assert f["attempts"][1]["operator"] == "improve" and f["attempts"][1]["metric"] == 0.85
    assert f["attempts"][1]["concepts"] == ["hard-neg"]
    assert f["best"] == {"node_id": 1, "metric": 0.85} and f["scope"]["kind"] == "dataset"


def test_run_facts_is_deterministic(tmp_path):
    st = fold(_run(tmp_path).read_all())
    assert _canon(run_facts(st, kind="dataset", metric="recall")) == \
        _canon(run_facts(st, kind="dataset", metric="recall"))


def test_run_facts_excludes_researcher_authored_concept_claims(tmp_path):
    s = EventStore(tmp_path / "authored.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    s.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "concepts": ["claimed/breakthrough"]},
    })

    facts = run_facts(fold(s.read_all()), kind="dataset", metric="recall")

    assert facts["attempts"][0]["concepts"] == []


# --------------------------------------------------------------------------- #
# CR0 gate: rebuild from scratch == itself, order-independent
# --------------------------------------------------------------------------- #

def _make_run_dir(root, run_id, task_id, goal, direction, metric_kind, nodes):
    d = root / run_id
    d.mkdir(parents=True)
    s = EventStore(d / "events.jsonl")
    s.append("run_started", {"run_id": run_id, "task_id": task_id, "goal": goal, "direction": direction})
    for nid, params, metric in nodes:
        s.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": params, "theme": "x"}})
        s.append("node_evaluated", {"node_id": nid, "metric": metric})
    (d / "task.snapshot.json").write_bytes(orjson.dumps({"kind": "dataset", "metric": {"name": metric_kind}}))


def test_rebuild_is_deterministic_and_order_independent(tmp_path):
    root = tmp_path / "runs"
    _make_run_dir(root, "rb", "t2", "goal two", "min", "rmse", [(0, {"a": 1.0}, 0.5)])
    _make_run_dir(root, "ra", "t1", "goal one", "max", "recall", [(0, {"a": 1.0}, 0.8), (1, {"a": 2.0}, 0.9)])
    first = rebuild_index_from_run_root(root)
    second = rebuild_index_from_run_root(root)
    assert _canon(first) == _canon(second)                   # rebuild == rebuild (the CR0 gate)
    assert [f["run_id"] for f in first] == ["ra", "rb"]      # sorted by run_id -> order-independent
    assert first[0]["scope"]["metric"] == "recall" and first[0]["best"]["metric"] == 0.9


def test_build_index_sorts_regardless_of_input_order(tmp_path):
    sa = fold(_run(tmp_path, run_id="aaa").read_all())
    sb = fold(_run(tmp_path, run_id="bbb").read_all())
    idx1 = build_index([(sa, "dataset", "recall"), (sb, "dataset", "recall")])
    idx2 = build_index([(sb, "dataset", "recall"), (sa, "dataset", "recall")])
    assert _canon(idx1) == _canon(idx2)                      # input order doesn't change the index


def test_empty_run_root_is_empty(tmp_path):
    (tmp_path / "runs").mkdir()
    assert rebuild_index_from_run_root(tmp_path / "runs") == []


# --------------------------------------------------------------------------- #
# Incremental rebuild (full-CR §21.20.13): digest cache + skip receipts + persistence
# --------------------------------------------------------------------------- #

def test_source_digest_changes_when_the_log_changes(tmp_path):
    root = tmp_path / "runs"
    _make_run_dir(root, "ra", "t1", "goal", "max", "recall", [(0, {"a": 1.0}, 0.8)])
    d1 = run_source_digest(root / "ra")
    assert d1.startswith("s_") and run_source_digest(root / "ra") == d1     # stable
    EventStore(root / "ra" / "events.jsonl").append("node_created", {"node_id": 9, "parent_ids": []})
    assert run_source_digest(root / "ra") != d1                             # log changed -> digest changed
    assert run_source_digest(tmp_path / "nope") == ""                       # no log -> ""


def test_source_digest_streams_large_inputs(tmp_path, monkeypatch):
    run = tmp_path / "large"
    run.mkdir()
    event_bytes = b"x" * (2 * 1024 * 1024 + 17)
    snapshot_bytes = b'{"kind":"dataset"}'
    (run / "events.jsonl").write_bytes(event_bytes)
    (run / "task.snapshot.json").write_bytes(snapshot_bytes)
    expected = hashlib.sha1(event_bytes + b"\x00snapshot\x00" + snapshot_bytes).hexdigest()

    def _whole_file_read_is_a_bug(self):
        raise AssertionError(f"read_bytes materialised {self}")

    monkeypatch.setattr(Path, "read_bytes", _whole_file_read_is_a_bug)
    assert run_source_digest(run) == "s_" + expected


def test_incremental_reuses_unchanged_and_rebuilds_changed(tmp_path):
    root = tmp_path / "runs"
    _make_run_dir(root, "ra", "t1", "goal one", "max", "recall", [(0, {"a": 1.0}, 0.8)])
    _make_run_dir(root, "rb", "t2", "goal two", "min", "rmse", [(0, {"a": 1.0}, 0.5)])
    first = build_index_incremental(root)
    assert sorted(first["receipts"]["built"]) == ["ra", "rb"] and not first["receipts"]["cached"]
    # nothing changed -> a second pass with the prior cache reuses BOTH, folds nothing
    second = build_index_incremental(root, prior=first)
    assert sorted(second["receipts"]["cached"]) == ["ra", "rb"] and not second["receipts"]["built"]
    assert _canon(second["index"]) == _canon(first["index"])               # identical index
    # change ra -> only ra re-folds; rb stays cached
    EventStore(root / "ra" / "events.jsonl").append("node_created",
        {"node_id": 1, "parent_ids": [0], "operator": "improve",
         "idea": {"operator": "improve", "params": {}, "theme": "x"}})
    EventStore(root / "ra" / "events.jsonl").append("node_evaluated", {"node_id": 1, "metric": 0.9})
    third = build_index_incremental(root, prior=second)
    assert third["receipts"]["built"] == ["ra"] and third["receipts"]["cached"] == ["rb"]
    ra = [f for f in third["index"] if f["run_id"] == "ra"][0]
    assert ra["n_attempts"] == 2                                            # the new node is reflected


def test_incremental_mode_flip_invalidates_unchanged_sources(tmp_path):
    root = tmp_path / "runs"
    _make_run_dir(root, "ra", "t1", "goal", "max", "recall", [(0, {"a": 1.0}, 0.8)])
    legacy = build_index_incremental(root, universal=False)
    assert legacy["fp_mode"] == "legacy"

    universal = build_index_incremental(root, prior=legacy, universal=True)
    assert universal["receipts"]["built"] == ["ra"]
    assert universal["receipts"]["cached"] == []
    assert universal["index"][0]["scope"]["fp_mode"] == "universal"


def test_incremental_rejects_old_or_different_projector_cache(tmp_path):
    root = tmp_path / "runs"
    _make_run_dir(root, "ra", "t1", "goal", "max", "recall", [(0, {"a": 1.0}, 0.8)])
    current = build_index_incremental(root)

    old = {"runs": current["runs"]}
    assert build_index_incremental(root, prior=old)["receipts"]["built"] == ["ra"]

    wrong_projector = dict(current)
    wrong_projector["projector_v"] += 1
    assert build_index_incremental(root, prior=wrong_projector)["receipts"]["built"] == ["ra"]


def test_incremental_rejects_corrupted_cached_facts(tmp_path):
    root = tmp_path / "runs"
    _make_run_dir(root, "ra", "t1", "goal", "max", "recall", [(0, {"a": 1.0}, 0.8)])
    current = build_index_incremental(root)
    tampered = orjson.loads(orjson.dumps(current))
    tampered["runs"]["ra"]["facts"]["run_id"] = "CACHE-INJECTED-RUN"

    rebuilt = build_index_incremental(root, prior=tampered)

    assert rebuilt["receipts"]["built"] == ["ra"] and rebuilt["receipts"]["cached"] == []
    assert [row["run_id"] for row in rebuilt["index"]] == ["ra"]


def test_incremental_matches_full_rebuild(tmp_path):
    root = tmp_path / "runs"
    _make_run_dir(root, "rb", "t2", "goal two", "min", "rmse", [(0, {"a": 1.0}, 0.5)])
    _make_run_dir(root, "ra", "t1", "goal one", "max", "recall", [(0, {"a": 1.0}, 0.8), (1, {"a": 2.0}, 0.9)])
    assert _canon(build_index_incremental(root)["index"]) == _canon(rebuild_index_from_run_root(root))


def test_torn_run_becomes_a_skip_receipt_not_a_gap(tmp_path):
    root = tmp_path / "runs"
    _make_run_dir(root, "ok", "t1", "goal", "max", "recall", [(0, {"a": 1.0}, 0.8)])
    bad = root / "bad"
    bad.mkdir()
    (bad / "events.jsonl").write_bytes(b'{"this is not valid json\n')   # a genuinely corrupt complete line
    res = build_index_incremental(root)
    assert [f["run_id"] for f in res["index"]] == ["ok"]                # the good run still indexes
    assert any(s["dir"] == "bad" for s in res["receipts"]["skipped"])   # the torn run is REPORTED, not silent


def test_valid_prefix_with_a_corrupt_complete_record_is_skipped(tmp_path):
    root = tmp_path / "runs"
    _make_run_dir(root, "damaged", "t1", "goal", "max", "recall", [(0, {"a": 1.0}, 0.8)])
    with open(root / "damaged" / "events.jsonl", "ab") as f:
        f.write(b"{not-json}\n")

    result = build_index_incremental(root)

    assert result["index"] == [] and result["receipts"]["built"] == []
    assert any(row["dir"] == "damaged" and "corrupt complete event record" in row["reason"]
               for row in result["receipts"]["skipped"])


def test_unreadable_digest_skips_only_that_run(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    _make_run_dir(root, "good", "t1", "goal", "max", "recall", [(0, {"a": 1.0}, 0.8)])
    _make_run_dir(root, "blocked", "t2", "goal", "max", "recall", [(0, {"a": 2.0}, 0.7)])
    blocked_log = root / "blocked" / "events.jsonl"
    original_open = Path.open

    def selective_open(path, *args, **kwargs):
        if path == blocked_log and args and "r" in str(args[0]):
            raise PermissionError("simulated unreadable run")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", selective_open)
    result = build_index_incremental(root)
    assert [row["run_id"] for row in result["index"]] == ["good"]
    assert any(row["dir"] == "blocked" and "PermissionError" in row["reason"]
               for row in result["receipts"]["skipped"])


def test_save_and_load_index_round_trip(tmp_path):
    root = tmp_path / "runs"
    _make_run_dir(root, "ra", "t1", "goal", "max", "recall", [(0, {"a": 1.0}, 0.8)])
    res = build_index_incremental(root)
    cache = tmp_path / "idx.json"
    save_index(cache, res)
    reloaded = load_index(cache)
    persisted = orjson.loads(cache.read_bytes())
    assert {"v", "projector_v", "scope_v", "fp_mode", "runs"} <= set(persisted)
    # the reloaded cache serves as a prior that reuses everything (no re-fold)
    again = build_index_incremental(root, prior=reloaded)
    assert again["receipts"]["cached"] == ["ra"] and _canon(again["index"]) == _canon(res["index"])
    assert load_index(tmp_path / "absent.json") is None


def test_load_index_rejects_pre_contract_cache(tmp_path):
    cache = tmp_path / "old-index.json"
    cache.write_bytes(orjson.dumps({"v": 1, "runs": {}}))
    assert load_index(cache) is None


def test_load_index_rejects_stale_schema_version(tmp_path):
    # mega-review regression: a cache written under a different schema version must force a clean rebuild.
    import orjson as _oj
    p = tmp_path / "idx.json"
    p.write_bytes(_oj.dumps({"v": 999, "runs": {"ra": {"digest": "s_x", "facts": {"run_id": "ra"}}}}))
    assert load_index(p) is None                     # incompatible version -> None (forces full rebuild)


def test_cli_cross_run_index_incremental(tmp_path):
    from typer.testing import CliRunner
    from looplab.cli import app
    root = tmp_path / "runs"
    _make_run_dir(root, "ra", "t1", "goal one", "max", "recall", [(0, {"a": 1.0}, 0.8)])
    r1 = CliRunner().invoke(app, ["cross-run-index", str(root), "--incremental"])
    assert r1.exit_code == 0 and "1 built" in r1.stdout
    r2 = CliRunner().invoke(app, ["cross-run-index", str(root), "--incremental"])
    assert r2.exit_code == 0 and "1 cached" in r2.stdout    # the sidecar cache was reused


def test_cli_cross_run_index(tmp_path):
    from typer.testing import CliRunner
    from looplab.cli import app
    root = tmp_path / "runs"
    _make_run_dir(root, "ra", "t1", "goal one", "max", "recall", [(0, {"a": 1.0}, 0.8)])
    res = CliRunner().invoke(app, ["cross-run-index", str(root)])
    assert res.exit_code == 0 and "1 run(s)" in res.stdout and "ra" in res.stdout and "best=0.8" in res.stdout
    res2 = CliRunner().invoke(app, ["cross-run-index", str(root), "--json"])
    assert res2.exit_code == 0 and orjson.loads(res2.stdout)[0]["run_id"] == "ra"


def test_cli_cross_run_index_empty_is_clean_error(tmp_path):
    from typer.testing import CliRunner
    from looplab.cli import app
    (tmp_path / "runs").mkdir()
    res = CliRunner().invoke(app, ["cross-run-index", str(tmp_path / "runs")])
    assert res.exit_code == 1 and "no runs" in res.stdout
