#!/usr/bin/env python3
"""Add (or update) an OpenRouter model entry in AlgoTune's `config.yaml`, with its spend limit.

WHY THIS EXISTS. `ALGOTUNE_MODEL_KEY` picks which entry AlgoTuner runs, but it can only pick one
that is THERE -- an unknown key fails at startup. So comparing the two arms on a new model means
adding an entry, and doing that by hand is how a comparison ends up with two arms on two
configurations that nobody wrote down.

AND THE BUDGET RIDES WITH IT, deliberately. `AlgoTuner/main.py` resolves the budget as
`model_info.get("spend_limit", global_config.spend_limit)` -- a PER-MODEL limit wins over the global
one. That is the only lever this repo has over the reference arm's budget, because `campaign.sh`'s
`BUDGET_USD` reaches `LOOPLAB_LLM_BUDGET_USD` and therefore only arm B. Measured 2026-08-21: the
campaign banner printed one budget for both arms while arm A was still running on the `spend_limit`
in `config.yaml` -- so a $1.00 arm-B run could sit beside a $0.02 arm-A run under a log line saying
they matched. Putting the limit in the entry is what makes `--spend-limit` mean the same thing on
both sides.

Idempotent: re-running with the same values is a no-op, and re-running with different ones rewrites
just that entry. Every other key in the file is preserved byte-for-byte -- the file is edited as
TEXT rather than round-tripped through a YAML dumper, which would reflow the comments that carry
this campaign's own deviations (§2 of docs/51).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


# OPEN[model-entry-update-strips-provider-pin] the remedy campaign.sh itself prints silently
# unpins the reference arm's model deployment.
# proof:`present:text = text[:existing.start()]@benchmarks/algotune/patch_model_entry.py`
# REVIEW 2026-08-25 (correctness): the update path below REPLACES an existing entry wholesale with
# `_entry(...)`'s block, and `_entry` never emits a `provider:` pin -- its own docstring argues the
# omission for a Google model. But `campaign.sh::budget_hint` prints this exact command for
# WHATEVER openrouter key is in play, i.e. for the campaign's default deepseek slug too, whose
# entry `setup_algotune.sh` writes WITH the pin (order: siliconflow/fp8, no fallbacks) for the
# measured reason README quotes ("three calls hit two different fp4 providers and returned
# 96/17/96 completion tokens for one prompt; 24 endpoints serve that slug at fp4/fp8/bf16").
# Driven 2026-08-25: running the printed command against the setup-written entry prints
# `updated ... verified: spend_limit=...` and the resulting `extra_body` carries only `reasoning`
# -- the pin is gone, nothing says so, and re-running setup_algotune.sh does NOT restore it (its
# model block is inserted only when the key is absent). So the standard budget-mismatch flow
# leaves arm A on a different deployment per call while arm B stays pinned -- an arms asymmetry in
# the exact variable the pin exists to hold still. Fix direction: update `spend_limit` in place
# instead of replacing the block, or have `_entry` carry over an existing entry's
# `extra_body.provider` subtree (and say when it did).
def _entry(key: str, slug: str, spend_limit: float, effort: str) -> str:
    """The block to write. Mirrors the shape of the entries already in the file.

    `reasoning.effort` is OpenRouter's own spelling and is the ONLY place effort is set for this
    arm -- AlgoTuner sends no `reasoning_effort` of its own, so unlike LoopLab there is nothing
    here for it to collide with (see `core/llm.py::reasoning_body`, which refuses that pair).

    No `provider.order` pin: it exists for the DeepSeek entry because one slug there reached two
    different fp4 providers and returned 96/17/96 completion tokens for one prompt. A Google model
    is served by Google; pinning a provider that has no alternatives would be noise that reads as a
    measurement decision.
    """
    return (
        f'  {key}:\n'
        f'    api_key_env: "OPENROUTER_API_KEY"\n'
        f'    temperature: 0.0\n'
        f'    drop_params: true\n'
        f'    usage:\n'
        f'      include: true\n'
        f'    # PER-MODEL and therefore authoritative: AlgoTuner/main.py resolves\n'
        f'    # `model_info.get("spend_limit", global_config.spend_limit)`. This is the only lever\n'
        f'    # the campaign has over the reference arm\'s budget -- BUDGET_USD reaches arm B alone.\n'
        f'    spend_limit: {spend_limit}\n'
        f'    extra_body:\n'
        f'      reasoning:\n'
        f'        effort: "{effort}"\n'
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--algotune-root", required=True, type=Path)
    ap.add_argument("--slug", required=True,
                    help="OpenRouter model slug, e.g. google/gemini-3.7-flash")
    ap.add_argument("--spend-limit", required=True, type=float,
                    help="USD ceiling for ONE task-arm. Must match the other arm's BUDGET_USD.")
    ap.add_argument("--effort", default="medium", choices=("low", "medium", "high"))
    ap.add_argument("--show", action="store_true", help="Print the resulting entry and exit 0.")
    args = ap.parse_args()

    cfg = args.algotune_root / "AlgoTuner" / "config" / "config.yaml"
    if not cfg.exists():
        print(f"no config at {cfg}", file=sys.stderr)
        return 2
    text = cfg.read_text(encoding="utf-8")
    key = f"openrouter/{args.slug}"
    block = _entry(key, args.slug, args.spend_limit, args.effort)

    # An existing entry runs from its own line to the next line indented by exactly two spaces
    # (a sibling key) or to a dedent. Anchored on the KEY, so a slug that is a prefix of another
    # cannot be clobbered.
    existing = re.search(rf"^  {re.escape(key)}:\n(?:    .*\n|\n)*", text, re.M)
    if existing:
        if existing.group(0) == block:
            print(f"already current: {key}")
            return 0
        text = text[:existing.start()] + block + text[existing.end():]
        print(f"updated: {key}")
    else:
        models = re.search(r"^models:\s*\n", text, re.M)
        if not models:
            print("config.yaml has no `models:` block", file=sys.stderr)
            return 2
        text = text[:models.end()] + block + text[models.end():]
        print(f"added: {key}")

    cfg.write_text(text, encoding="utf-8")

    # Verify by PARSING, never by trusting the splice: a block written at the wrong indentation
    # still looks right in a diff and silently becomes a sibling of `models` instead of a member.
    try:
        import yaml
    except ImportError:
        print("  (pyyaml absent — wrote the entry but could not verify it parses)", file=sys.stderr)
        return 0
    got = (yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}).get("models", {}).get(key)
    if not isinstance(got, dict):
        print(f"  WROTE A BROKEN ENTRY: {key} does not parse as a member of `models`", file=sys.stderr)
        return 1
    if float(got.get("spend_limit", -1)) != args.spend_limit:
        print(f"  spend_limit did not land: {got.get('spend_limit')!r}", file=sys.stderr)
        return 1
    print(f"  verified: spend_limit={got['spend_limit']} effort={args.effort}")
    if args.show:
        print(block)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
