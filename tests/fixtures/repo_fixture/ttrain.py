"""Tiny stand-in 'training framework' eval entrypoint. Reads the experiment config and
emits a metric both to stdout (JSON) and to a metrics.json file. The metric is maximized
(= 0.0) at x = 3.0, so an agent editing config.json toward x=3 improves it."""
import json

with open("config.json", encoding="utf-8") as f:
    cfg = json.load(f)

metric = -((float(cfg.get("x", 0.0)) - 3.0) ** 2)

with open("metrics.json", "w", encoding="utf-8") as f:
    json.dump({"metric": metric}, f)
print(json.dumps({"metric": metric}))
