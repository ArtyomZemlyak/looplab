"""The leak checker must answer about the box it is run on, not about one box it was written on.

THE DEFECT, closed 2026-09-08 (REVIEW 2026-08-25). Three hardcodes, each defeating the tool's one
job -- "run before a start and after a reset; a non-zero exit means something would have carried
over" -- anywhere but the original machine:

  1. `ROOT=/var/tmp/looplab-bench`, ignoring `BENCH_ROOT`, which every other script here honours.
  2. Fixed directory-name lists (`campaign-paired campaign-armb ...`, `runs-A runs-B ...`), so a
     campaign under the SHIPPED defaults -- `campaign/` and `camp-runs/`, per `campaign.sh` and
     `box-jhub-l40s.sh` -- was invisible. Section 1 printed "чисто" while that campaign's `.done`
     markers would make the next run skip every task: the precise leak the section exists for.
     `snapshot.sh` documents the same hardcoded-name defect having already caused an incident.
  3. Section 3 counted every file without the literal `r3` as stale, a token one box used once,
     instead of the generation `patch_baseline_cache.py` really writes.

WHAT IS TESTED HERE. The script is RUN against synthetic bench roots and its output is read. The
sections are asserted by what they say about directories that exist and about directories that do
not -- a checker that prints "чисто" about a path it never looked at is the failure one level up,
and it is the one a source pin could not tell from a fix.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ALGOTUNE = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune"
CHECK_LEAKS = ALGOTUNE / "check_leaks.sh"


def _run(root: Path, **env_extra: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, BENCH_ROOT=str(root), **env_extra)
    # The campaign's own variables are what a sourced profile sets; an unset one must not make the
    # script fall back to another box's tree.
    for name in ("CAMPAIGN_OUT", "CAMPAIGN_RUNS", "CAMPAIGN_WS", "ALGOTUNE_ROOT",
                 "ALGOTUNE_BASELINE_CACHE_DIR", "METER_LOG"):
        env.pop(name, None)
        if name in env_extra:
            env[name] = env_extra[name]
    return subprocess.run(["bash", str(CHECK_LEAKS)], capture_output=True, text=True, timeout=300,
                          env=env)


def _section(out: str, number: int) -> str:
    """Just the lines of section `number`, so an assertion cannot be satisfied by another one."""
    blocks = re.split(r"^== ", out, flags=re.M)
    for block in blocks:
        if block.startswith(f"{number}. "):
            return block
    raise AssertionError(f"section {number} is missing from:\n{out}")


def test_a_campaign_under_the_shipped_default_names_is_found(tmp_path):
    """THE ITEM. `campaign/` and `camp-runs/` are what `box-jhub-l40s.sh` exports and what the
    hardcoded list did not contain, so this is exactly the campaign that read as clean."""
    root = tmp_path / "bench"
    (root / "campaign").mkdir(parents=True)
    (root / "campaign" / "B-svm.done").write_text("wall=2100 rc=0\n", encoding="utf-8")
    (root / "camp-runs" / "svm" / "memory").mkdir(parents=True)
    (root / "camp-runs" / "svm" / "memory" / "lessons.jsonl").write_text("{}\n", encoding="utf-8")
    proc = _run(root)
    assert "ПРОПУСТИТ" in _section(proc.stdout, 1), proc.stdout
    assert "непустых файлов памяти" in _section(proc.stdout, 2), proc.stdout
    assert proc.returncode != 0, "a marker that would make the next run skip a task is a leak"


def test_a_directory_that_does_not_exist_is_not_reported_as_clean(tmp_path):
    """The failure this whole file is about: an answer about a path nobody looked at. An empty
    bench root must SAY there is nothing to look at."""
    root = tmp_path / "bench"
    root.mkdir()
    proc = _run(root)
    for number in (1, 2, 4):
        assert "нет таких каталогов" in _section(proc.stdout, number), (number, proc.stdout)
        assert "чисто" not in _section(proc.stdout, number)
    assert str(root) in proc.stdout, "the sentence must name the root it looked under"


def test_the_campaign_variables_are_honoured_even_outside_the_bench_root(tmp_path):
    """`CAMPAIGN_OUT` may point anywhere -- `box-jhub-l40s.sh` says so in as many words -- and a
    sweep that only globs under $BENCH_ROOT would miss the markers actually being written."""
    root = tmp_path / "bench"
    root.mkdir()
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    (elsewhere / "A-svm.done").write_text("wall=2100 rc=0\n", encoding="utf-8")
    proc = _run(root, CAMPAIGN_OUT=str(elsewhere))
    assert "маркеров — ПРОПУСТИТ" in _section(proc.stdout, 1), proc.stdout


def test_the_cache_generation_comes_from_the_patch_that_writes_the_names(tmp_path):
    """Section 3's staleness test is DERIVED. `patch_baseline_cache.py` keys a cache
    `<task>__<subset>__lane{N}<gen>.json`, and its own comment says why the generation token exists
    (from 2026-08-24 a lane is whole physical cores, so an earlier reference is from another
    machine). A literal `r3` here goes wrong the day that bumps -- in the direction that reports
    every CURRENT file as a leak, which is what the review measured.
    """
    root = tmp_path / "bench"
    times = root / ".baseline_times"
    times.mkdir(parents=True)
    (times / "svm__train__lane22r3.json").write_text("{}", encoding="utf-8")
    (times / "svm__train__w22x1r2.json").write_text("{}", encoding="utf-8")   # an older ruler
    proc = _run(root)
    section = _section(proc.stdout, 3)
    assert "2 файлов, из них не-r3: 1" in section, section
    assert proc.returncode != 0

    # ...and the token itself is read out of the patch, not typed: the same function against a
    # patch that says `r9` must answer `r9`.
    src = CHECK_LEAKS.read_text(encoding="utf-8")
    body = re.search(r"^current_generation\(\) \{.*?^\}$", src, re.M | re.S)
    assert body, "check_leaks.sh no longer defines current_generation()"
    fake = tmp_path / "fake_patch_dir"
    fake.mkdir()
    (fake / "patch_baseline_cache.py").write_text(
        '    ("lane regime key", \'f"__lane{_ll_lane}r9"\'),\n', encoding="utf-8")
    out = subprocess.run(["bash", "-c", f'HERE="{fake}"\n{body.group(0)}\ncurrent_generation'],
                         capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "r9", (out.stdout, out.stderr)


def test_the_generation_the_patch_writes_today_is_the_one_the_checker_uses():
    """Both halves of the join, against the real files: whatever the patch mints, the checker asks
    for. Driven rather than compared to a literal, so a bump needs no edit here either."""
    src = CHECK_LEAKS.read_text(encoding="utf-8")
    body = re.search(r"^current_generation\(\) \{.*?^\}$", src, re.M | re.S).group(0)
    out = subprocess.run(["bash", "-c", f'HERE="{ALGOTUNE}"\n{body}\ncurrent_generation'],
                         capture_output=True, text=True, timeout=60)
    token = out.stdout.strip()
    assert token, "the generation could not be derived from patch_baseline_cache.py at all"
    patch = (ALGOTUNE / "patch_baseline_cache.py").read_text(encoding="utf-8")
    assert f"__lane{{_ll_lane}}{token}" in patch or f"__lane{{{{_ll_lane}}}}{token}" in patch
