"""Eval entrypoint that FAILS (nonzero exit) unless config.json provides `needed_x` — used
to exercise the error-feedback repair loop (the agent must fix the config to make it run)."""
import json
import sys

try:
    with open("config.json", encoding="utf-8") as f:
        x = float(json.load(f)["needed_x"])
except Exception as e:  # noqa: BLE001
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
print(json.dumps({"metric": -((x - 3.0) ** 2)}))
