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

# WHERE SNAPSHOTS LAND. A property of this machine -- /home/jovyan/data is the pod's persistent
# mount and /var/tmp is not -- so it is declared here with the other machine facts rather than
# hardcoded in `snapshot.sh`. It used to be hardcoded there and reachable no other way, so a
# `BENCH_ROOT` pointed at a scratch tree still wrote into this rotation; on 2026-08-31 that put a
# snapshot of a synthetic box among the real ones.
export SNAPSHOT_DEST="${SNAPSHOT_DEST:-/home/jovyan/data/looplab-bench/snapshots}"

# The meter. Start it with benchmarks/meter/start_meter.sh; campaign.sh gives each task-arm its own
# path under it, so cost lands per task without either framework knowing.
export METER_BASE="${METER_BASE:-http://127.0.0.1:8801}"
export METER_PORT="${METER_PORT:-8801}"
export METER_RPM="${METER_RPM:-45}"          # under the endpoint's published 50/min for this key
# WHICH CORES THE METER MAY USE. Not a lane: the meter is infrastructure every lane talks to, so a
# lane it shares is a lane whose timings include somebody else's proxy.
#
# Under the regime the finished campaign actually ran -- `lanes=4 cores_per_lane=22`, which every
# `campaign-final/*.done` marker records beside its own cpu list -- the lanes are 0-10+48-58,
# 11-21+59-69, 22-32+70-80 and 33-43+81-91, i.e. 44 of this box's 48 physical cores, and the four
# left over are 44-47 with their siblings 92-95. That is what they are free FOR. Note that this is
# NOT the shipped default the header above describes (20 lanes x 2 cpus, which occupies physical
# cores 0-19 and their siblings); the lane count and width are the operator's `LANES` /
# `CORES_PER_LANE`, so the profile has to name which regime a core range is stated against.
#
# 44-47+92-95 is off the lanes in BOTH, and that is not asserted here in prose:
# `tests/test_the_meter_is_pinned_off_the_lanes.py` runs `campaign.sh`'s own lane planner against
# this box's real `thread_siblings_list` for each regime and asserts the pin is disjoint from every
# lane it produces.
#
# It lives here rather than in start_meter.sh because it is a fact about this box's core layout, and
# it lives in a FILE rather than in a driver because that is how it was lost: on 2026-08-29 the
# pinning was done by run_final.sh, which was never committed and went with /var/tmp when the
# container restarted. The meter then came back on 0-95 -- measured at 0.0 % CPU, so nothing was
# actually spoiled, but it was one busy proxy away from contaminating a lane and nothing would have
# said so.
export METER_CPUS="${METER_CPUS:-44-47,92-95}"
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

# PIP В VENV АРЕНЫ — БЕЗ НЕГО КОМПИЛЯЦИЯ НЕ ЗАСЧИТЫВАЕТСЯ.
#
# `AlgoTune/scripts/evaluate_results.py:266` запускает `python -m pip install . --no-deps
# --force-reinstall --no-cache-dir` над каталогом кандидата, как только там появился `setup.py`.
# Venv арены создан `uv`, а `uv venv` pip НЕ кладёт — и вся ветка отвечает
# `Setup install failed: ... No module named pip`, что оценщик превращает в
# `no_speedup{reason: compilation_failed}` и `speedup: 0.0`.
#
# ЦЕНА, ИЗМЕРЕННАЯ 2026-08-28 по корпусу из 19 прогонов: восемь независимых прогонов написали
# `.pyx` + `setup.py`, получили эту ошибку и УДАЛИЛИ своё расширение через 0.2-2.4 минуты. 35 из
# 35 вызовов `delete_file` во всём корпусе — это `.pyx` и `setup.py`. Кто пробился (dsFB, sol10)
# получил 204-261 на train и 207/259 на тесте; кто удалил — 27.2-48.8. Разрыв 5-9x при шуме 10%.
# Опубликованный чемпион бенчмарка (Gemini 3.1) поставляется именно как `.pyx` + `setup.py`, то
# есть сломана НАША среда, а не замысел арены.
#
# Ставится ТОЛЬКО pip, из встроенного в ensurepip колеса. `python -m ensurepip` притащил бы ещё
# setuptools 65.5.0 поверх стоящего 84.0.0 — откат посреди живых оценок, чего делать нельзя.
# OPEN[box-profile-pip-repair-noops-on-undefined-root] the default python path expands through an
# undefined variable, so the pip repair this function exists for silently returns without looking.
# proof:`present:$ROOT/AlgoTune@benchmarks/box-jhub-l40s.sh`
# REVIEW 2026-08-30 (correctness): the profile defines BENCH_ROOT and ALGOTUNE_ROOT and never ROOT,
# so the default is `/AlgoTune/.venv/bin/python`, `[ -x ]` fails, `return 0` — and the guard whose
# own comment records the cost of a pip-less arena venv (every `.pyx`+`setup.py` candidate scored
# `compilation_failed`/0.0; a 5-9x champion gap) does nothing after any container restart rebuilds
# the venv. Nothing says so, because the miss path is the silent success path. `$ALGOTUNE_ROOT` is
# the intended spelling.
_algotune_ensure_pip() {
  local py="${1:-$ROOT/AlgoTune/.venv/bin/python}"
  [ -x "$py" ] || return 0
  "$py" -m pip --version >/dev/null 2>&1 && return 0
  local whl
  whl=$("$py" -c "import ensurepip,os,glob;d=os.path.join(os.path.dirname(ensurepip.__file__),'_bundled');print((glob.glob(os.path.join(d,'pip-*.whl'))+[''])[0])" 2>/dev/null)
  [ -n "$whl" ] || { echo "[box] НЕТ pip и нет встроенного колеса — компиляция будет засчитываться как 0.0" >&2; return 1; }
  "$py" "$whl/pip" install --no-index --no-deps "$whl" >/dev/null 2>&1 \
    && echo "[box] pip доставлен в venv арены (без setuptools)" \
    || echo "[box] НЕ УДАЛОСЬ поставить pip — компиляция будет засчитываться как 0.0" >&2
}
_algotune_ensure_pip
