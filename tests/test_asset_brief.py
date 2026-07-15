"""D1 asset/prior-art brief scanner (PART IV §21.2) — read-only repo scan for the on-disk assets the
`rubertlite` run was blind to.

These lock in that the scanner surfaces the three §21.2 asset classes (result tables, sibling
checkpoints with metrics-in-filenames, hard-neg/distill-capable trainers), reads NOTHING it shouldn't
(ignored dirs pruned; bounded), and distils the exact grounding the §21.10 validation needed — the
proven capability tokens (`n_negatives`/`negatives_path`, mnr, NV false-neg filter, distillation) and
the best on-disk score — into a compact brief. It is offline, deterministic, and writes nothing."""
from __future__ import annotations

from pathlib import Path

from looplab.tools.asset_brief import (AssetBrief, AssetLexicon, agentic_asset_brief, asset_brief,
                                       format_brief, lexicon_for, scan_assets, _extract_metrics,
                                       _extract_table_metrics)


def _rubertlite_repo(root: Path) -> Path:
    """A miniature of the `rubertlite` repo layout: a result table with the proven recipe, sibling
    checkpoints whose filenames carry their metric, and a hard-negative-capable trainer."""
    (root / "data" / "vectorizer" / "dense-retrieval").mkdir(parents=True)
    (root / "data" / "vectorizer" / "results_last.csv").write_text(
        "model,loss,n_negatives,recall@100,notes\n"
        "rubertlite-dcl,dcl,0,0.8835,baseline\n"
        "sibling-hardneg,mnr,15,0.899,teacher-mined hard negatives + NV-0.95 false-neg filter\n")
    (root / "data" / "vectorizer" / "results_last.xlsx").write_bytes(b"PK\x03\x04binary-spreadsheet")
    ck = root / "checkpoints"
    ck.mkdir()
    (ck / "nomic-moe@0.899.safetensors").write_bytes(b"weights")
    (ck / "e5-base@0.90.bin").write_bytes(b"weights")
    (root / "dense-retrieval" / "train.py").parent.mkdir(parents=True, exist_ok=True)
    (root / "dense-retrieval" / "train.py").write_text(
        "def train(cfg):\n"
        "    n_negatives = cfg.get('n_negatives', 0)\n"
        "    negatives_path = cfg.get('negatives_path')\n"
        "    # distill from the cross-encoder teacher with mnr loss\n")
    # a dir that MUST be pruned — its result file must never be read
    (root / ".git").mkdir()
    (root / ".git" / "results.json").write_text('{"recall@100": 0.999}')
    return root


# --------------------------------------------------------------------------- #
# Metric extraction
# --------------------------------------------------------------------------- #

def test_extract_named_metrics_but_not_hyperparameters():
    m = _extract_metrics("val_recall@100=0.899 lr=0.001 seed=42 accuracy: 0.95")
    assert m["val_recall@100"] == 0.899
    assert m["accuracy"] == 0.95
    # lr / seed are NOT metric-family names -> not mistaken for scores
    assert "lr" not in m and "seed" not in m


def test_precision_hyperparameter_is_not_mistaken_for_a_metric():
    # fp16/bf16 mixed-precision config must not read as a `precision` score; a real precision@k stays
    m = _extract_metrics("precision=16 bf16 recall@100=0.9 precision@10=0.82")
    assert "precision" not in m               # precision=16 (fp16) dropped
    assert m["precision@10"] == 0.82          # the real metric kept
    assert m["recall@100"] == 0.9


def test_extract_table_metrics_from_csv():
    text = ("model,recall@100,ndcg@10\n"
            "a,0.88,0.70\n"
            "b,0.899,0.73\n")
    m = _extract_table_metrics(text)
    assert m["recall@100"] == 0.899   # max down the column
    assert m["ndcg@10"] == 0.73


def test_extract_table_metrics_from_markdown():
    text = ("## Results\n\n| model | recall@100 |\n|---|---|\n| base | 0.88 |\n| best | 0.91 |\n")
    m = _extract_table_metrics(text)
    assert m["recall@100"] == 0.91


# --------------------------------------------------------------------------- #
# Scan
# --------------------------------------------------------------------------- #

def test_scan_finds_all_three_asset_classes(tmp_path):
    # with the dense-retrieval lexicon opted in, the winning recipe's capabilities are named (§21.10)
    brief = scan_assets(_rubertlite_repo(tmp_path), task_type="dense-retrieval")
    assert brief.results and brief.checkpoints and brief.configs
    for cap in ("hard-negative-mining", "false-negative-filtering", "distillation", "mnr-loss"):
        assert cap in brief.capabilities


def test_generic_scan_finds_assets_but_no_hardcoded_capabilities(tmp_path):
    # NO task_type -> universal scan: it still finds result tables / checkpoints / configs and their
    # metrics, but names NO domain capabilities (nothing dense-retrieval-specific is hardcoded in).
    brief = scan_assets(_rubertlite_repo(tmp_path))
    assert brief.results and brief.checkpoints and brief.configs
    assert brief.capabilities == []                       # no domain vocabulary without a lexicon
    assert brief.best_known and brief.best_known["value"] >= 0.899   # metrics are still universal


def test_lexicon_for_resolves_pack_and_aliases():
    assert lexicon_for("dense-retrieval").capability_patterns                    # exact
    assert lexicon_for("some-vectorizer-task").capability_patterns               # fuzzy alias
    assert lexicon_for("tabular-regression").capability_patterns == {}           # unknown -> generic
    assert lexicon_for(None).capability_patterns == {}


def test_explicit_lexicon_overrides_task_type(tmp_path):
    lex = AssetLexicon(task_type="custom", capability_patterns={"my-cap": r"train\("})
    brief = scan_assets(_rubertlite_repo(tmp_path), lexicon=lex)
    assert "my-cap" in brief.capabilities                 # a caller-supplied pack works on any repo


def test_scan_extracts_checkpoint_filename_metrics(tmp_path):
    brief = scan_assets(_rubertlite_repo(tmp_path))
    paths = {f.path for f in brief.checkpoints}
    assert any("nomic-moe@0.899" in p for p in paths)
    got = {round(v, 3) for f in brief.checkpoints for v in f.metrics.values()}
    assert 0.899 in got and 0.9 in got


def test_scan_surfaces_best_on_disk_result(tmp_path):
    brief = scan_assets(_rubertlite_repo(tmp_path))
    # the strongest parsed score across the CSV table (0.899) and checkpoints (0.90)
    assert brief.best_known is not None
    assert brief.best_known["value"] >= 0.899


def test_scan_notes_unparsed_spreadsheet(tmp_path):
    brief = scan_assets(_rubertlite_repo(tmp_path))
    xlsx = [f for f in brief.results if f.path.endswith(".xlsx")]
    assert xlsx and "not parsed" in xlsx[0].detail


def test_scan_prunes_ignored_dirs(tmp_path):
    brief = scan_assets(_rubertlite_repo(tmp_path))
    # the .git/results.json (0.999) must never be read — best_known must not come from it
    assert all(".git" not in f.path for f in brief.results)
    assert brief.best_known["value"] != 0.999


def test_best_known_skips_lower_is_better_metrics(tmp_path):
    # a max over an error metric would call the WORST rmse the best — error metrics are excluded
    (tmp_path / "metrics.csv").write_text("model,rmse,r2\na,12.5,0.88\nb,9.1,0.91\n")
    brief = scan_assets(tmp_path)
    assert brief.best_known["metric"] == "r2" and brief.best_known["value"] == 0.91


def test_best_known_normalizes_percent_vs_fraction_across_sources(tmp_path):
    # one file reports fractions, another percents (same metric) — the real 0.95 must out-rank 91.2%
    (tmp_path / "results_a.csv").write_text("model,recall@100\nbest_frac,0.95\n")
    (tmp_path / "results_b.csv").write_text("model,recall@100\nother_pct,91.2\n")
    brief = scan_assets(tmp_path)
    assert brief.best_known["value"] == 0.95 and brief.best_known["source"] == "results_a.csv"


def test_scan_missing_repo_is_graceful(tmp_path):
    brief = scan_assets(tmp_path / "does-not-exist")
    assert isinstance(brief, AssetBrief)
    assert brief.results == [] and brief.notes


def test_scan_respects_file_cap(tmp_path):
    for i in range(50):
        (tmp_path / f"results_{i}.csv").write_text("model,recall@100\na,0.5\n")
    brief = scan_assets(tmp_path, max_files=10)
    assert brief.truncated is True


def test_generic_readme_without_metrics_is_ignored(tmp_path):
    (tmp_path / "README.md").write_text("# My project\n\nA cool library. No results here.\n")
    brief = scan_assets(tmp_path)
    assert brief.results == []   # a plain README is not prior-art


def test_readme_with_result_table_is_included(tmp_path):
    (tmp_path / "README.md").write_text(
        "# Model\n\n## Results\n\n| model | recall@100 |\n|---|---|\n| ours | 0.92 |\n")
    brief = scan_assets(tmp_path)
    assert any("README" in f.path for f in brief.results)
    assert brief.best_known and brief.best_known["value"] == 0.92


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def test_format_brief_is_compact_and_grounded(tmp_path):
    text = asset_brief(_rubertlite_repo(tmp_path), task_type="dense-retrieval")
    assert "PRIOR ART & AVAILABLE ASSETS" in text
    assert "hard-negative-mining" in text
    assert "Best on-disk result" in text
    assert "nomic-moe@0.899" in text


def test_format_brief_empty_repo(tmp_path):
    text = format_brief(scan_assets(tmp_path))
    assert "none found" in text


def test_format_brief_lists_results_in_deterministic_order(tmp_path):
    # os.walk append order is filesystem-dependent; the rendered brief must be path-sorted + stable
    for nm in ("results_z.csv", "results_a.csv", "results_m.csv"):
        (tmp_path / nm).write_text("model,recall@100\nx,0.5\n")
    text = format_brief(scan_assets(tmp_path), max_items=2)
    shown = [ln.split("·")[1].split("[")[0].strip() for ln in text.splitlines() if "·" in ln]
    assert shown == sorted(shown)   # the first max_items are the path-sorted head, deterministically


def test_scan_writes_nothing(tmp_path):
    repo = _rubertlite_repo(tmp_path)
    before = sorted(p.name for p in repo.rglob("*"))
    scan_assets(repo)
    after = sorted(p.name for p in repo.rglob("*"))
    assert before == after   # read-only: the scan created/modified/deleted nothing


# --------------------------------------------------------------------------- #
# Agentic path (the PRIMARY route) — an LLM explores the repo with read-only tools
# --------------------------------------------------------------------------- #

import json


def _tool_call(name, args):
    return {"content": "", "tool_calls": [
        {"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}}]}


class _FakeChatClient:
    """Scripts agent-loop turns (chat) + records the tool results it was fed."""
    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.turns = []

    def chat(self, messages, tools, tool_choice="auto"):
        self.turns.append(list(messages))
        return self.scripted.pop(0)

    def complete_text(self, messages):
        return ""


class _BadChatClient:
    def chat(self, messages, tools, tool_choice="auto"):
        raise RuntimeError("boom")

    def complete_text(self, messages):
        return ""


def test_agentic_brief_falls_back_to_scan_without_client(tmp_path):
    repo = _rubertlite_repo(tmp_path)
    text = agentic_asset_brief(repo, client=None, task_type="dense-retrieval")
    assert text == format_brief(scan_assets(repo, task_type="dense-retrieval"))


def test_agentic_brief_reads_repo_then_writes_brief(tmp_path):
    repo = _rubertlite_repo(tmp_path)
    client = _FakeChatClient([
        _tool_call("grep", {"pattern": "n_negatives"}),   # turn 1: the agent inspects the trainer
        _tool_call("answer", {"text": "Best on-disk: recall@100=0.899 via teacher-mined hard "
                                      "negatives + NV-0.95 filter; reuse the hardneg trainer."}),
    ])
    text = agentic_asset_brief(repo, client=client, task_type="dense-retrieval",
                               loop_opts={"self_plan": False, "stuck_detection": False,
                                          "auto_summary": False}, seed_scan=False)
    assert "0.899" in text and "hard neg" in text.lower()
    assert client.turns                                   # the agent loop actually ran
    # the grep tool result was fed back before the agent emitted
    assert any(m.get("role") == "tool" for m in client.turns[-1])


def test_agentic_brief_degrades_on_client_failure(tmp_path):
    repo = _rubertlite_repo(tmp_path)
    text = agentic_asset_brief(repo, client=_BadChatClient(), task_type="dense-retrieval",
                               seed_scan=False)
    # a failing agent loop degrades to the deterministic brief, never crashes
    assert "PRIOR ART & AVAILABLE ASSETS" in text
