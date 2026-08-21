# Box profile: the JupyterHub pod with the L40S. Source it, do not run it.
#
#   source benchmarks/box-jhub-l40s.sh
#   benchmarks/meter/start_meter.sh                 # once per boot
#   ARM=A benchmarks/algotune/campaign.sh
#
# Everything here is a property of THIS machine, kept out of campaign.sh so the campaign script
# stays the same file on every box. What each line is answering:
#
# WHERE THE WORK LIVES.  /home/jovyan/data is geesefs -- an S3-backed FUSE mount. It cannot host
# this benchmark: `uv venv` fails on it outright (`Operation not supported (os error 95)` -- no
# flock), and every timed run here is dominated by process spawn and imports, which is exactly what
# a network filesystem is worst at. The runtime therefore lives on the container's own disk
# (/var/tmp, overlay/xfs, ~110 GB free) and the git checkouts are pushed, not trusted to stay.
#
# CPU.  `nproc` is 96 but the cgroup quota is `cpu.max = 9000000 100000`, i.e. 90 CPUs of
# throughput. 20 lanes x 2 cores = 40, comfortably inside it, so lane pinning means what it says.
# (docs/50 called 8 cores the handicap of the original box; this one has the opposite problem --
# enough cores that the campaign is one round instead of seven.)
#
# LLM.  A corporate LiteLLM gateway, not OpenRouter. Three measured consequences, all handled by
# the metering proxy (benchmarks/meter/proxy.py) rather than by editing either framework:
#   1. it reports usage TOKENS but no `usage.cost` (its own x-litellm-response-cost-original is 0.0
#      for this model group), and BOTH arms enforce their budget by reading `usage.cost`;
#   2. it publishes `x-litellm-key-rpm-limit: 50` (team 150) and enforces it -- a 20-concurrent
#      burst measured 9 x HTTP 429, and sequential calls kept 429-ing until the window rolled;
#   3. it CACHES: the identical prompt at temperature 0 returned in 0.0 s with 400 completion
#      tokens (28,886 tok/s). Real generation on this endpoint measures ~96 tok/s.
#
# MODEL.  `deepseek-v4-flash` on the gateway, priced from the pinned OpenRouter list price of
# `deepseek/deepseek-v4-flash-0731` (benchmarks/meter/pricing.json). It exposes no reasoning channel
# and no provider choice, so the campaign's `provider` pin and `reasoning.effort` are set empty
# here: a dead parameter left in the record reads like a live control.

export BENCH_ROOT="${BENCH_ROOT:-/var/tmp/looplab-bench}"
export ALGOTUNE_ROOT="$BENCH_ROOT/AlgoTune"
export CAMPAIGN_OUT="${CAMPAIGN_OUT:-$BENCH_ROOT/campaign}"
export CAMPAIGN_WS="${CAMPAIGN_WS:-$BENCH_ROOT/looplab_ws}"
export CAMPAIGN_RUNS="${CAMPAIGN_RUNS:-$BENCH_ROOT/camp-runs}"

# The meter. Start it with benchmarks/meter/start_meter.sh; campaign.sh gives each task-arm its own
# path under it, so cost lands per task without either framework knowing.
export METER_BASE="${METER_BASE:-http://127.0.0.1:8801}"
export METER_PORT="${METER_PORT:-8801}"
export METER_RPM="${METER_RPM:-45}"          # under the endpoint's published 50/min for this key
export METER_LOG="${METER_LOG:-$BENCH_ROOT/meter/meter.jsonl}"

export LOOPLAB_LLM_MODEL="${LOOPLAB_LLM_MODEL:-deepseek-v4-flash}"
export ALGOTUNE_MODEL_KEY="${ALGOTUNE_MODEL_KEY:-gateway/deepseek-v4-flash}"
export LOOPLAB_LLM_REASONING_EXTRA='{}'
export LOOPLAB_LLM_STREAM="${LOOPLAB_LLM_STREAM:-1}"   # metered either way; see proxy.py

# The key (and the upstream URL) live in the AlgoTune checkout's .env, which campaign.sh sources.
if [ -f "$ALGOTUNE_ROOT/.env" ]; then
  set -a; . "$ALGOTUNE_ROOT/.env"; set +a
fi

# No proxy for the loopback meter: this box exports http_proxy for the outside world, and a client
# that honours it would send localhost traffic into it.
export NO_PROXY="${NO_PROXY:-},127.0.0.1,localhost"
export no_proxy="${no_proxy:-},127.0.0.1,localhost"

echo "box: jhub-l40s | bench root $BENCH_ROOT | model $LOOPLAB_LLM_MODEL via ${METER_BASE}"
