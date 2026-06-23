"""Framework entrypoint driven by CLI overrides (Hydra-style `key=value`), for the
cli_overrides / eval-profile mode. Reads `x` (a tuned hyperparameter, metric max at x=3)
and `steps` (an eval-profile knob). Emits the metric as stdout JSON."""
import json
import sys

vals = {}
for a in sys.argv[1:]:
    if "=" in a:
        k, v = a.split("=", 1)
        vals[k] = v

x = float(vals.get("x", 0.0))
steps = int(float(vals.get("steps", 1)))
metric = -((x - 3.0) ** 2)
print(json.dumps({"metric": metric, "steps": steps}))
