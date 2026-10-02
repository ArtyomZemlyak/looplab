# Resumable training (sft_resume.py): every SFT_RESUME_EVERY_MIN minutes the full training state goes
# to the durable mount; a re-run of the SAME node (same work dir, same recipe, same training code)
# continues from it after a container restart instead of starting over. The key is stable across
# attempts (RUN_ID is a timestamp, it is not). Deleted after a successful training. Not in a canary.
if [[ "${LOOPLAB_CANARY:-0}" != "1" && "${SFT_RESUME_EVERY_MIN:-60}" != "0" ]]; then
    RESUME_KEY="$(
        { realpath "$MINIONEREC_DIR"; echo "$BACKBONE"
          for f in looplab/experiment.env "$CFG" sft.py sft_*.py prepare_sft_data.py; do
              [[ -f "$f" ]] && { echo "== $f"; cat "$f"; }
          done; true; } | sha256sum | cut -c1-16
    )"
    export SFT_RESUME_DIR="${SFT_RESUME_DIR:-${RUN_BASE}/resume/${RUN_KIND}/${RESUME_KEY}_${BACKBONE}}"
    export SFT_RESUME_EVERY_MIN="${SFT_RESUME_EVERY_MIN:-60}"
fi

echo "=== node ${RUN_ID}: backbone ${BACKBONE} (${MODEL}) ==="
echo "resume checkpoints: ${SFT_RESUME_DIR:-off}${SFT_RESUME_DIR:+ (every ${SFT_RESUME_EVERY_MIN} min)}"
echo "outputs: ${OUTPUT_ROOT}  results: ${EVAL_RESULTS_ROOT}  datasets: ${SFT_DATASET_CACHE_ROOT}"
echo "checkpoints: ${SFT_CHECKPOINTS}${SFT_CHECKPOINT_DIR:+ (${SFT_CHECKPOINT_DIR})}  keep final model after eval: ${SFT_KEEP_FINAL_MODEL}"
# Incremental fine-tuning (experiment.env; TRAINING_BACKBONES.md "Incremental fine-tuning study"):
#   SFT_MODE=finetune + SFT_FINETUNE_FROM=<complete SFT checkpoint>: start from that model (its
#     SID-extended tokenizer) instead of the backbone's base weights; new optimizer, new schedule;
#   SFT_TRAIN_DATA_ROOT: the data root the prep/train stages read (default: the box's DATA_ROOT);
#     scripts/prepare_incremental_data.py writes the new-events roots (ft_<window>[_replay<k>]);
#   SFT_CONFIG_FILE: an explicit config/*.yaml instead of the backbone's;
#   SFT_SKIP_TRAIN=1: no prep, no training: score SFT_FINETUNE_FROM as is (the "no fine-tune" arm).
SFT_MODE="${SFT_MODE:-train}"
SFT_FINETUNE_FROM="${SFT_FINETUNE_FROM:-}"
SFT_SKIP_TRAIN="${SFT_SKIP_TRAIN:-0}"
[[ -n "${SFT_CONFIG_FILE:-}" ]] && CFG="$SFT_CONFIG_FILE"
SFT_ENV=(SFT_CONFIG="$CFG" SFT_REFERENCE_MODEL="$REF" SFT_RUN_NAME="$RUN_ID" SFT_MODE="$SFT_MODE")
[[ "$SFT_MODE" == "finetune" ]] && SFT_ENV+=(SFT_FINETUNE_FROM="$SFT_FINETUNE_FROM")
if [[ -n "${SFT_TRAIN_DATA_ROOT:-}" ]]; then
    SFT_ENV+=(DATA_ROOT="$SFT_TRAIN_DATA_ROOT")
    # A new-events root is small (~100k users, ~5k valid users) and read as a stream (SAMPLE=-1):
    # sharded conversion leaves most of the 64 workers with ZERO examples and their Arrow writers fail
    # (SchemaInferenceError), and any SFT_EVAL_SAMPLE above the valid users crashes frame.sample().
    # So: one conversion process, and the whole valid file (-2) unless the node sets these itself.
    SFT_ENV+=(SFT_DATASET_NUM_PROC="${SFT_DATASET_NUM_PROC:-1}")
    _valid_rows=$(( $(wc -l < "${SFT_TRAIN_DATA_ROOT}/valid_sequence/groceries.csv") - 1 ))
    if [[ "${SFT_EVAL_SAMPLE:-0}" -gt "$_valid_rows" ]]; then
        echo "SFT_EVAL_SAMPLE=${SFT_EVAL_SAMPLE} > ${_valid_rows} valid users in ${SFT_TRAIN_DATA_ROOT}: using the whole valid file (-2)"
        SFT_EVAL_SAMPLE=-2; export SFT_EVAL_SAMPLE
    fi
fi

T0=$(date +%s)
if [[ "$SFT_SKIP_TRAIN" == "1" ]]; then
    [[ -f "${SFT_FINETUNE_FROM}/config.json" ]] || { echo "SFT_SKIP_TRAIN=1 needs a complete SFT_FINETUNE_FROM checkpoint (got '${SFT_FINETUNE_FROM}')" >&2; exit 2; }
    echo "=== SFT_SKIP_TRAIN=1: no prep / train, scoring ${SFT_FINETUNE_FROM} as is ==="
    CKPT_DIR="$SFT_FINETUNE_FROM"
