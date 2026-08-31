# Launching the next e5small run

`eval_env` is an ENGINE SETTING, not a field of the task file. Every task snapshot on this box
carries `eval.env = {}` — v2, v4 and v11 got their data root from the SETTING, and v12 went out
without it because it was launched from a copied task snapshot alone (#147). The result: every v12
node crashes on S3 (`InvalidAccessKeyId`) and pays a triage+repair to rediscover the local corpus.

`core/config.py:699` writes down why it is a setting and not only a shell export, and
`core/models.py:1709` records what it decides: **which corpus a node trained on**. A run whose
corpus is chosen per-node by a repair is not comparable to the 0.793426 champion.

## The command

    cd /home/jovyan/data/looplab
    cp runs/<previous-run>/task.snapshot.json e5small_vNN.json
    python -m looplab.core.claimpin e5small_vNN.json          # must report 0 claim defects
    nvidia-smi                                                # both H200s must be free
    setsid nohup python -m looplab.cli run e5small_vNN.json \
        --out runs/e5small-dr-unified-vNN \
        --backend llm --max-nodes 24 \
        -s eval_env=VS_LOCAL_DATA_ROOT=/home/jovyan/data/dr-local \
        > runs/e5small-dr-unified-vNN.console.log 2>&1 < /dev/null &

`setsid`, because a plain `nohup` dies with the tool timeout. Then verify BOTH before reporting a
launch: a live pid AND a non-empty `events.jsonl`.

## After launch, confirm the corpus rather than assuming it

    python -c "import json; d=json.load(open('runs/e5small-dr-unified-vNN/config.snapshot.json')); print(d['eval_env'])"

must print `{"VS_LOCAL_DATA_ROOT": "/home/jovyan/data/dr-local"}`. If it prints `{}` the run will
still work — each node repairs its way to a corpus — but every node's root has to be checked
individually before any metric is compared to the champion.
