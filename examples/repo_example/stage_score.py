"""Score the artifact produced by ``stage_train.py``; the optimum is x=3."""
import json


with open("stage_artifact.json", encoding="utf-8") as stream:
    x = float(json.load(stream)["x"])
print(json.dumps({"metric": -((x - 3.0) ** 2)}))
