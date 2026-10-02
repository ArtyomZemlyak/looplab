"""Small genuine CPU SGD and an independent protected prediction scorer."""
TRAIN = '''import json
from pathlib import Path
MOMENTUM = 0.0
w, velocity = 0.0, 0.0
for epoch in range(30):
    gradient = sum(2*x*(w*x-2*x) for x in (0.1,0.2,0.3,0.4,0.5))/5
    velocity = MOMENTUM*velocity + gradient
    w -= 0.2*velocity
Path("predictions.json").write_text(json.dumps([w*x for x in (0.15,0.35,0.55)]))
print("trained", w)
'''
SOURCE = TRAIN.replace("MOMENTUM = 0.0", "MOMENTUM = 0.2")
GENERAL = TRAIN.replace("MOMENTUM = 0.0", 'settings = dict(line.split("=",1) for line in Path("recipe.env").read_text().splitlines() if "=" in line)\nMOMENTUM = float(settings.get("MOMENTUM", "0.0"))')
SCORE = '''import json
from pathlib import Path
p = json.loads(Path("predictions.json").read_text())
print(json.dumps({"metric":sum((a-2*x)**2 for a,x in zip(p,(0.15,0.35,0.55)))/3}))
'''
