"""The budget remedy the campaign prints must not unpin the reference arm's deployment.

THE DEFECT, driven 2026-08-25 and closed 2026-09-08. `campaign.sh::budget_hint` prints
`patch_model_entry.py --slug <whatever openrouter key is in play> --spend-limit <BUDGET_USD>`
whenever the two arms' budgets disagree, and that script's update path REPLACED the existing entry
wholesale with a block `_entry()` builds from its arguments. `_entry` emits no `provider:` pin --
its docstring argues the omission for a Google model, which has no alternative deployments -- so
running the printed command against the campaign's own deepseek entry, which `setup_algotune.sh`
writes WITH the pin, deleted it. Nothing said so, `setup_algotune.sh` does not restore it (its model
block is inserted only when the key is ABSENT), and the result is an arms asymmetry in exactly the
variable the pin exists to hold still: arm A on a different provider per call while arm B stays
pinned. The measurement behind the pin is in this repo's README -- three calls hit two different fp4
providers and returned 96/17/96 completion tokens for one prompt, with 24 endpoints serving that
slug at fp4/fp8/bf16.

HOW THIS IS TESTED. By RUNNING the script over a config carrying the pin, and reading the file it
wrote -- not by pinning `carried_provider`'s source, which a comment would satisfy. The fixture
entry is EXTRACTED from `setup_algotune.sh` rather than retyped, so a change to the pin the setup
writes is a change to what this test proves survives.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="the script verifies its own splice by parsing")

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "benchmarks" / "algotune" / "patch_model_entry.py"
SETUP = ROOT / "benchmarks" / "algotune" / "setup_algotune.sh"
SLUG = "deepseek/deepseek-v4-flash-0731"
KEY = f"openrouter/{SLUG}"


def _setup_written_entry() -> str:
    """The pinned model block `setup_algotune.sh` really writes, out of the script itself."""
    found = re.search(r'^MODEL = """(.*?)"""$', SETUP.read_text(encoding="utf-8"), re.M | re.S)
    assert found, "setup_algotune.sh no longer writes a MODEL block; re-point this fixture"
    block = found.group(1)
    assert "provider:" in block and "allow_fallbacks: false" in block, (
        "the setup's own entry has lost the pin this test exists to protect")
    return block


def _config(tmp_path: Path, entry: str) -> Path:
    cfg = tmp_path / "AlgoTuner" / "config" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    # A SECOND ENTRY, because the anchored replacement is half the property: an update must not
    # reach past its own key.
    cfg.write_text("models:\n" + entry + "  other/model:\n    api_key_env: \"X\"\n",
                   encoding="utf-8")
    return cfg


def _run(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(PATCH), "--algotune-root", str(tmp_path),
                           "--slug", SLUG, "--spend-limit", "1.0", *extra],
                          capture_output=True, text=True, timeout=120)


def _models(cfg: Path) -> dict:
    return (yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}).get("models", {})


def test_updating_the_spend_limit_keeps_the_provider_pin(tmp_path):
    """The whole item: the budget lands AND the deployment stays where it was."""
    cfg = _config(tmp_path, _setup_written_entry())
    proc = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    entry = _models(cfg)[KEY]
    assert float(entry["spend_limit"]) == 1.0, "the remedy must still do what it is printed for"
    provider = entry["extra_body"]["provider"]
    assert provider["order"] == ["siliconflow/fp8"], entry
    assert provider["allow_fallbacks"] is False, (
        "without `allow_fallbacks: false` the order is a preference, not a pin")
    # The effort this invocation asked for still wins: the pin is carried, the block is not.
    assert entry["extra_body"]["reasoning"]["effort"] == "medium"


def test_it_says_that_it_carried_the_pin(tmp_path):
    """A pin that moves silently is the same defect one level down."""
    _config(tmp_path, _setup_written_entry())
    proc = _run(tmp_path)
    assert "provider pin: carried over" in proc.stdout, proc.stdout
    assert "provider=pinned" in proc.stdout, "the verification line reads the file, so it says so too"


def test_an_entry_with_no_pin_is_reported_as_having_none(tmp_path):
    """The falsifier for the sentence above: it must not be printed over an unpinned entry."""
    unpinned = ('  openrouter/deepseek/deepseek-v4-flash-0731:\n'
                '    api_key_env: "OPENROUTER_API_KEY"\n'
                '    extra_body:\n'
                '      reasoning:\n'
                '        effort: medium\n')
    cfg = _config(tmp_path, unpinned)
    proc = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "provider pin: none in the previous entry" in proc.stdout, proc.stdout
    assert "provider" not in _models(cfg)[KEY]["extra_body"], "it may not invent a pin either"


def test_the_neighbouring_entry_is_untouched_and_the_file_still_parses(tmp_path):
    cfg = _config(tmp_path, _setup_written_entry())
    assert _run(tmp_path).returncode == 0
    assert _models(cfg)["other/model"] == {"api_key_env": "X"}


def test_a_second_run_is_a_no_op(tmp_path):
    """Idempotence survives the carry-over: the block it compares against must be the one it
    would write, pin included, or every re-run rewrites the file and reports an update."""
    _config(tmp_path, _setup_written_entry())
    assert _run(tmp_path).returncode == 0
    again = _run(tmp_path)
    assert again.returncode == 0 and "already current" in again.stdout, again.stdout
