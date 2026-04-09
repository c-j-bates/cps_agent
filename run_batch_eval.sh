#!/usr/bin/env bash
#
# Run batch evaluation: configs × models matrix.
#
# Usage (local Ollama):
#   ./run_batch_eval.sh --base-url http://localhost:11435/v1
#   ./run_batch_eval.sh --base-url http://localhost:11435/v1 --limit 5
#
# Usage (DeepSeek cloud API — run from laptop):
#   ./run_batch_eval.sh --provider deepseek-cloud --limit 5
#
# Usage via deploy.sh (remote server with Ollama):
#   SERVER=cbates@gpu2.ihmc.us ./deploy.sh --run-background \
#       "./run_batch_eval.sh --base-url http://localhost:11435/v1"
#
# Named experiments:
#   ./run_batch_eval.sh --provider deepseek-cloud --name minute-cryptic-par1
#   → creates eval_results/minute-cryptic-par1/ and experiment_logs/minute-cryptic-par1/
#
# Resuming / addendum — append results to an existing experiment:
#   ./run_batch_eval.sh --provider deepseek-cloud --name minute-cryptic-par1
#
# Single addendum via run_eval.py:
#   python run_eval.py --dataset ... --config ... --exp-name minute-cryptic-par1
#
# For Ollama providers, models are pulled automatically before evaluation begins.

set -euo pipefail

# ── Parse args ──────────────────────────────────────────────────────────
PROVIDER="deepseek"
BASE_URL=""
LIMIT_FLAG=""
EPOCHS_FLAG=""
EXTRA_FLAGS=""
EXPERIMENT_NAME=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --provider)      PROVIDER="$2"; shift 2 ;;
        --base-url)      BASE_URL="$2"; shift 2 ;;
        --limit)         LIMIT_FLAG="--limit $2"; shift 2 ;;
        --epochs)        EPOCHS_FLAG="--epochs $2"; shift 2 ;;
        --thinking)      EXTRA_FLAGS="$EXTRA_FLAGS --thinking"; shift ;;
        --name)          EXPERIMENT_NAME="$2"; shift 2 ;;
        *)               echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# base-url is required for local/Ollama providers, optional for cloud presets
if [[ -z "$BASE_URL" && "$PROVIDER" == "deepseek" ]]; then
    echo "Error: --base-url is required for local providers (e.g. http://localhost:11435/v1)"
    echo "       For DeepSeek cloud API, use --provider deepseek-cloud"
    exit 1
fi

# Disable Inspect AI's TUI so output is plain text (works in tmux/background)
export INSPECT_DISPLAY=plain
export INSPECT_LOG_LEVEL=info

# ── Configuration ───────────────────────────────────────────────────────
# DATASET="datasets/cryptics_george_ho.csv"
# DATASET="datasets/minute_cryptic.csv"
DATASET="datasets/minute_cryptic_easy.csv"

MODELS=(
    # "deepseek-r1:32b"
    # "deepseek-r1:70b"
    "deepseek-reasoner"
    # "deepseek-v3.1:671b"
)

# Run groups: each group is a set of configs that share a thinking flag.
# Format: "flag|config1|config2|..."
#   --thinking  → model's built-in CoT enabled  (use with baseline)
#   (empty)     → thinking off (default)         (use with prompt-based strategies)
RUN_GROUPS=(
    "--thinking|configs/config_minute_cryptic_baseline.yaml"
    "|configs/config_minute_cryptic_keep_thinking_step_by_step.yaml|configs/config_minute_cryptic_generate_vars.yaml"
)

# ── Pull models (Ollama only) ──────────────────────────────────────────
if [[ -n "$BASE_URL" && "$PROVIDER" != *"-cloud"* ]]; then
    OLLAMA_URL="${BASE_URL%/v1}"
    echo "==> Ensuring all models are pulled at ${OLLAMA_URL} ..."
    for model in "${MODELS[@]}"; do
        python -c "
import urllib.request, urllib.error, json, sys
url = '${OLLAMA_URL}'
model = '${model}'
# Check if already available
try:
    req = urllib.request.Request(url + '/api/show', data=json.dumps({'name': model}).encode(), method='POST')
    req.add_header('Content-Type', 'application/json')
    urllib.request.urlopen(req)
    print(f'  ✓ {model} (already pulled)')
    sys.exit(0)
except urllib.error.HTTPError:
    pass
# Pull it (stream=false so we block until done)
print(f'  Pulling {model} (this may take a while) ...')
try:
    req = urllib.request.Request(url + '/api/pull', data=json.dumps({'name': model, 'stream': False}).encode(), method='POST')
    req.add_header('Content-Type', 'application/json')
    urllib.request.urlopen(req, timeout=3600)
    print(f'  ✓ {model} pulled')
except Exception as e:
    print(f'  WARNING: Could not pull {model}: {e}')
" || echo "  WARNING: Pull check failed for ${model}"
    done
    echo "==> All models ready."
fi

# ── Experiment name ───────────────────────────────────────────────────
# Priority: --name > auto-generated from models+dataset+timestamp
if [[ -z "$EXPERIMENT_NAME" ]]; then
    TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
    MODEL_TAG=$(IFS=_; echo "${MODELS[*]}" | tr ':/' '-')
    DATASET_TAG=$(basename "$DATASET" .csv)
    EXPERIMENT_NAME="${MODEL_TAG}_${DATASET_TAG}_${TIMESTAMP}"
fi
EXP_NAME_FLAG="--exp-name $EXPERIMENT_NAME"
echo "==> Experiment: $EXPERIMENT_NAME"
echo "    Results:    eval_results/$EXPERIMENT_NAME/"
echo "    Logs:       experiment_logs/$EXPERIMENT_NAME/"

# ── Count total runs ───────────────────────────────────────────────────
TOTAL=0
for group in "${RUN_GROUPS[@]}"; do
    IFS='|' read -ra PARTS <<< "$group"
    NUM_CONFIGS=$(( ${#PARTS[@]} - 1 ))  # first element is the flag
    TOTAL=$(( TOTAL + NUM_CONFIGS * ${#MODELS[@]} ))
done
COUNT=0

# ── Run experiments ─────────────────────────────────────────────────────
for group in "${RUN_GROUPS[@]}"; do
    IFS='|' read -ra PARTS <<< "$group"
    THINKING_FLAG="${PARTS[0]}"
    CONFIGS=("${PARTS[@]:1}")

    if [[ -n "$THINKING_FLAG" ]]; then
        echo ""
        echo "════════════════════════════════════════════════════════════════"
        echo "  Group: ${THINKING_FLAG}"
        echo "════════════════════════════════════════════════════════════════"
    else
        echo ""
        echo "════════════════════════════════════════════════════════════════"
        echo "  Group: no thinking (default)"
        echo "════════════════════════════════════════════════════════════════"
    fi

    for config in "${CONFIGS[@]}"; do
        for model in "${MODELS[@]}"; do
            COUNT=$((COUNT + 1))
            config_name=$(basename "$config" .yaml)
            echo ""
            echo "[$COUNT/$TOTAL] $config_name | $model ${THINKING_FLAG:+(${THINKING_FLAG})}"
            echo "----------------------------------------------------------------"

            python run_eval.py \
                --dataset "$DATASET" \
                --config "$config" \
                --provider "$PROVIDER" \
                --model "$model" \
                $EXP_NAME_FLAG \
                ${BASE_URL:+--base-url "$BASE_URL"} \
                $LIMIT_FLAG $EPOCHS_FLAG $EXTRA_FLAGS $THINKING_FLAG \
                || echo "WARNING: Run failed for $config_name / $model"
        done
    done
done

echo ""
echo "================================================================"
echo "All $TOTAL experiments complete."
echo "Results in: eval_results/$EXPERIMENT_NAME/"
echo ""
echo "To analyze:  python analyze_results.py --results-dir eval_results/$EXPERIMENT_NAME"
echo "To add more: ./run_batch_eval.sh ... --name $EXPERIMENT_NAME"
echo "             python run_eval.py ... --exp-name $EXPERIMENT_NAME"
echo "================================================================"
