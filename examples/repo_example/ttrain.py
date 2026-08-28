"""Example experiment 'framework' entrypoint (the operator's eval). Reads config.json and
emits a metric (max at x=3.0). An R&D agent edits config.json (the edit-surface) to raise
it; this file (the eval) is protected from edits.

It also writes `resolved_config.json` — the coordinates THIS PROCESS actually settled on, after
defaults and any override it applies. That file is what `eval.metric.applied_config_glob` points at,
and it is the only thing that can catch the case the committed config cannot: a declaration and a
committed carrier that AGREE WITH EACH OTHER and are both wrong about what executed. Measured on
`rubertlite-dr-unified-v8` node 8 — declaration and committed config both say `n_epochs: 15`, the
config the process resolved says 8, and that node recorded a metric. Without a resolved carrier no
reading of committed bytes can ever caveat it.
"""
import json

with open("config.json", encoding="utf-8") as f:
    declared = json.load(f)
x = float(declared.get("x", 0.0))
# The resolved config: this process's OWN structure, which is not the operator's edit surface.
# `config.json` is flat because that is what a human edits; the trainer resolves it into
# `train.x`, and that nesting is the point. A declared coordinate must have at least two dotted
# parts (`declared_numeric_params`: "a bare `lr` is a word, not a path"), so the flat committed
# carrier can never answer a legal declaration and the resolved one can. Real trainers do exactly
# this — the operator edits one shape and the framework settles another.
resolved = {"train": {"x": x}}
metric = -((x - 3.0) ** 2)
with open("metrics.json", "w", encoding="utf-8") as f:
    json.dump({"metric": metric}, f)
with open("resolved_config.json", "w", encoding="utf-8") as f:
    json.dump(resolved, f)
print(json.dumps({"metric": metric}))
