#!/usr/bin/env python3
"""Teach AlgoTuner to reach an OpenAI-compatible gateway through the metering proxy.

`setup_algotune.sh` pins the campaign's OpenRouter entry. This adds a SECOND entry for a box whose
model comes from a corporate gateway instead, and changes nothing about the first: a machine can
carry both and a campaign picks one with `ALGOTUNE_MODEL_KEY`.

Three things make the entry different from the OpenRouter one, and each is forced by what was
measured on the gateway (2026-08-20):

  * `model_name: openai/<model>` -- litellm must treat it as a generic OpenAI endpoint. The base URL
    is NOT written here: it comes from `OPENAI_BASE_URL` at launch, which is what lets `campaign.sh`
    give every task its own meter path (`/m/<arm>/<task>/v1`) and get per-task cost attribution for
    an arm that has no idea it is being metered.
  * No `provider` block. The pin exists to stop OpenRouter silently switching quantization between
    requests; a single-deployment gateway has nothing to switch to, and sending it anyway would put
    a dead parameter in the record where a later reader would take it for a live control.
  * No `reasoning` block. Measured: this model returns no `reasoning_content` channel at all, so an
    effort level would be a claim about a control that does not exist here.

Idempotent. Keeps a `.orig` on first write; `--revert` restores it.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import sys

ENTRY = """  {key}:
    model_name: "openai/{model}"
    api_key_env: "{key_env}"
    temperature: 0.0
    drop_params: true
    usage:
      include: true
    context_length: {ctx}
    max_tokens: {max_tokens}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--algotune-root", required=True)
    ap.add_argument("--key", default="gateway/deepseek-v4-flash",
                    help="config key both arms name; pass it to campaign.sh as ALGOTUNE_MODEL_KEY")
    ap.add_argument("--model", default="deepseek-v4-flash", help="model id the gateway serves")
    ap.add_argument("--key-env", default="LOOPLAB_LLM_API_KEY")
    ap.add_argument("--context-length", type=int, default=131072)
    ap.add_argument("--max-tokens", type=int, default=8192)
    ap.add_argument("--revert", action="store_true")
    args = ap.parse_args()

    cfg = pathlib.Path(args.algotune_root) / "AlgoTuner/config/config.yaml"
    if not cfg.exists():
        print(f"not an AlgoTune checkout: {cfg} missing", file=sys.stderr)
        return 2
    backup = cfg.with_suffix(cfg.suffix + ".gateway.orig")

    if args.revert:
        if not backup.exists():
            print("nothing to revert")
            return 0
        shutil.copy2(backup, cfg)
        print(f"restored {cfg} from {backup.name}")
        return 0

    text = cfg.read_text(encoding="utf-8")
    if f"\n  {args.key}:" in text:
        print(f"already present: {args.key}")
        return 0
    if not backup.exists():
        shutil.copy2(cfg, backup)

    entry = ENTRY.format(key=args.key, model=args.model, key_env=args.key_env,
                         ctx=args.context_length, max_tokens=args.max_tokens)
    new, count = re.subn(r"^models:\s*$", "models:\n" + entry.rstrip(), text, count=1, flags=re.M)
    if count != 1:
        print("FAILED: no `models:` block in config.yaml", file=sys.stderr)
        return 1
    cfg.write_text(new, encoding="utf-8")
    print(f"added model entry {args.key} -> openai/{args.model} (base URL comes from "
          f"OPENAI_BASE_URL at launch)")
    print(f"  backup: {backup.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
