#!/usr/bin/env bash
#
# Deploy cps_agent to a remote server.
#
# Usage:
#   ./deploy.sh                  # sync code + rebuild image
#   ./deploy.sh --full           # also start Ollama + pull a model
#   ./deploy.sh --run "..."      # sync, rebuild, then run a command
#
# Examples:
#   ./deploy.sh
#   ./deploy.sh --full
#   ./deploy.sh --run "python run_experiment.py --provider deepseek --problem-id 3"
#   ./deploy.sh --run-background "..."          # survives disconnect, auto-cleans when done
#   ./deploy.sh --run-background-persist "..."  # survives disconnect, stays open for inspection
#
# Configuration: edit these or set as environment variables before running.
#   SERVER       - ssh host (e.g. user@10.0.0.1)
#   REMOTE_DIR   - path on server where code lives
#   OLLAMA_MODEL - model to pull on --full setup
#   OLLAMA_PORT  - host port for Ollama (default: 11434, change if port is taken)

set -euo pipefail

# ── Config (override via env vars) ──────────────────────────────────────
SERVER="${SERVER:?Set SERVER, e.g. SERVER=user@myhost ./deploy.sh}"
REMOTE_DIR="${REMOTE_DIR:-~/cps_agent}"
OLLAMA_MODEL="${OLLAMA_MODEL:-}"
OLLAMA_PORT="${OLLAMA_PORT:-11434}"

# ── Parse args ──────────────────────────────────────────────────────────
FULL_SETUP=false
RUN_CMD=""
RUN_BG=false
BG_PERSIST=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --full)   FULL_SETUP=true; shift ;;
        --run)    RUN_CMD="$2"; shift 2 ;;
        --run-background) RUN_CMD="$2"; RUN_BG=true; shift 2 ;;
        --run-background-persist) RUN_CMD="$2"; RUN_BG=true; BG_PERSIST=true; shift 2 ;;
        *)        echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# ── Step 1: Sync code to server ─────────────────────────────────────────
echo "==> Syncing code to ${SERVER}:${REMOTE_DIR} ..."
rsync -avz --delete \
    --exclude '.venv/' \
    --exclude '.git/' \
    --exclude '.claude/' \
    --exclude '__pycache__/' \
    --exclude '.DS_Store' \
    --exclude '.env' \
    --exclude 'experiment_logs/' \
    --exclude 'outputs/' \
    --exclude 'logs/' \
    --exclude 'eval_results/' \
    ./ "${SERVER}:${REMOTE_DIR}/"

# ── Step 2: Rebuild the cps_agent image on the server ────────────────────
echo "==> Rebuilding Docker image on server ..."
ssh "${SERVER}" "cd ${REMOTE_DIR} && docker compose build cps_agent"

# ── Step 3 (optional): Full first-time setup ─────────────────────────────
if $FULL_SETUP; then
    echo "==> Starting Ollama container (host port ${OLLAMA_PORT}) ..."
    ssh "${SERVER}" "cd ${REMOTE_DIR} && OLLAMA_PORT=${OLLAMA_PORT} docker compose up -d ollama"

    echo "==> Waiting for Ollama to be ready ..."
    ssh "${SERVER}" "
        for i in \$(seq 1 30); do
            curl -sf http://localhost:${OLLAMA_PORT}/api/tags >/dev/null 2>&1 && break
            sleep 2
        done
    "

    # Pull model(s) if specified. Batch scripts also pull their own models,
    # so this is mainly for one-off runs or pre-warming.
    if [[ -n "${OLLAMA_MODEL:-}" ]]; then
        echo "==> Pulling model: ${OLLAMA_MODEL} ..."
        ssh "${SERVER}" "docker exec cps_agent-ollama-1 ollama pull ${OLLAMA_MODEL}"
        echo "==> Ollama ready with ${OLLAMA_MODEL} on port ${OLLAMA_PORT}."
    else
        echo "==> Ollama ready on port ${OLLAMA_PORT}. Models will be pulled on demand."
    fi
fi

# ── Step 4 (optional): Run a command ─────────────────────────────────────
if [[ -n "$RUN_CMD" ]]; then
    if $RUN_BG; then
        SESSION="cps_$(date +%H%M%S)"
        echo "==> Running in tmux session '${SESSION}': ${RUN_CMD}"
        if $BG_PERSIST; then
            ssh "${SERVER}" "tmux new-session -d -s ${SESSION} 'cd ${REMOTE_DIR} && docker compose run --rm cps_agent ${RUN_CMD}; echo; echo \"==> Done (press enter to close)\"; read'"
        else
            ssh "${SERVER}" "tmux new-session -d -s ${SESSION} 'cd ${REMOTE_DIR} && docker compose run --rm cps_agent ${RUN_CMD}'"
        fi
        echo "==> Experiment launched. To attach:"
        echo "      ssh ${SERVER} -t 'tmux attach -t ${SESSION}'"
    else
        echo "==> Running: ${RUN_CMD}"
        ssh "${SERVER}" "cd ${REMOTE_DIR} && docker compose run --rm cps_agent ${RUN_CMD}"
    fi
fi

echo "==> Done."
