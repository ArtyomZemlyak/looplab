"""Example framework entrypoint driven by CLI overrides (Hydra-style `key=value`). Reads
`x` (tuned hyperparameter; metric max at x=3) and `steps` (eval-profile knob). Emits the
metric as stdout JSON — the operator's trusted eval; the agent only proposes `x`."""
import json
import sys

vals = dict(a.split("=", 1) for a in sys.argv[1:] if "=" in a)
x = float(vals.get("x", 0.0))
steps = int(float(vals.get("steps", 1)))
print(json.dumps({"metric": -((x - 3.0) ** 2), "steps": steps}))
