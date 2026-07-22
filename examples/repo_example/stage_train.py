"""Materialize the tuned stage parameter for the following protected score stage."""
import argparse
import json


parser = argparse.ArgumentParser()
parser.add_argument("--x", type=float, required=True)
args = parser.parse_args()
with open("stage_artifact.json", "w", encoding="utf-8") as stream:
    json.dump({"x": args.x}, stream)
print(json.dumps({"trained_x": args.x}))
