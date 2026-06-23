"""Example experiment 'framework' entrypoint (the operator's eval). Reads config.json and
emits a metric (max at x=3.0). An R&D agent edits config.json (the edit-surface) to raise
it; this file (the eval) is protected from edits."""
import json

with open("config.json", encoding="utf-8") as f:
    x = float(json.load(f).get("x", 0.0))
metric = -((x - 3.0) ** 2)
with open("metrics.json", "w", encoding="utf-8") as f:
    json.dump({"metric": metric}, f)
print(json.dumps({"metric": metric}))
