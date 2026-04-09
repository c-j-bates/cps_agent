# Docker Quickstart

## Architecture

```
Your laptop                          gpu2 server
┌────────────┐    rsync + ssh       ┌──────────────────────────────────┐
│  cps_agent/ │ ──────────────────> │  ~/cps_agent/                    │
│  (source)   │    deploy.sh        │                                  │
└────────────┘                      │  ┌────────────┐  ┌───────────┐  │
                                    │  │ cps_agent   │─>│  Ollama   │  │
                                    │  │ (Docker)    │  │  (Docker) │  │
                                    │  │ Python 3.11 │  │  GPU      │  │
                                    │  └────────────┘  └───────────┘  │
                                    └──────────────────────────────────┘
```

Two Docker containers:
- **cps_agent** — your Python code. Rebuilt every time you deploy.
- **ollama** — serves local models (DeepSeek, Llama, etc.) on the GPU. Runs persistently.

They communicate over `localhost` (host networking).

**Alternatively**, you can run experiments locally against the **DeepSeek cloud API** — no
Docker or GPU server needed. See [Running with DeepSeek cloud API](#running-with-deepseek-cloud-api) below.


## First-time setup

```bash
# 1. Start Ollama and pull a model (only need to do this once)
SERVER=cbates@gpu2.ihmc.us OLLAMA_PORT=11435 OLLAMA_MODEL=deepseek-r1:70b ./deploy.sh --full

# 2. Create a .env file on the server (only needed for Claude/OpenAI runs)
ssh cbates@gpu2.ihmc.us "touch ~/cps_agent/.env"
# or, if you need API keys:
scp .env cbates@gpu2.ihmc.us:~/cps_agent/.env
```


## Everyday workflow

### Deploy code changes (no run)
```bash
SERVER=cbates@gpu2.ihmc.us ./deploy.sh
```
This rsyncs your code and rebuilds the Docker image. Fast — rsync only sends
diffs, Docker only rebuilds the code layer (deps are cached).

### Deploy + run (foreground)
```bash
SERVER=cbates@gpu2.ihmc.us ./deploy.sh --run "python run_experiment.py \
    --provider deepseek --model deepseek-r1:70b \
    --base-url http://localhost:11435/v1 \
    --config configs/config_minute_cryptic_baseline.yaml \
    --dataset datasets/minute_cryptic.csv --problem-id 1"
```
Output streams to your terminal. **Dies if your connection drops.**

### Deploy + run (background, auto-cleanup)
```bash
SERVER=cbates@gpu2.ihmc.us ./deploy.sh --run-background "python run_experiment.py ..."
```
Launches in a tmux session on the server. **Survives disconnects.**
Session is automatically destroyed when the experiment finishes.

### Deploy + run (background, persist for inspection)
```bash
SERVER=cbates@gpu2.ihmc.us ./deploy.sh --run-background-persist "python run_experiment.py ..."
```
Same as above, but the tmux session **stays open after finishing** so you can
attach later and see the output. You close it manually by pressing Enter.


## Working on the server directly

```bash
ssh cbates@gpu2.ihmc.us
cd ~/cps_agent

# Drop into a bash shell inside the container
docker compose run --rm cps_agent bash

# Now you're inside the container at /app:
python run_experiment.py --provider deepseek --model deepseek-r1:70b \
    --base-url http://localhost:11435/v1 --problem-id 1
```

Use tmux if you want the session to survive disconnects:
```bash
tmux new -s myexperiment
docker compose run --rm cps_agent bash
# ... run stuff ...
# Ctrl-B, D to detach

# Later, reattach:
tmux attach -t myexperiment
```


## Checking on things

```bash
# SSH to server first, then:

docker compose ps                    # what's running?
docker compose logs ollama           # Ollama logs (GPU errors, model loading)
docker ps                            # all containers (including other users')

# tmux sessions (from --run-background)
tmux ls                              # list sessions
tmux attach -t cps_171423            # attach to a session
# Ctrl-B, D to detach without killing it

# What models does Ollama have?
curl -s http://localhost:11435/api/tags | python3 -m json.tool
```


## Where things live

| What | Where (on server) | Persists across runs? |
|---|---|---|
| Your code | `~/cps_agent/` | Yes (rsync'd from laptop) |
| Ollama models | Docker volume `cps_agent_ollama_data` | Yes |
| Experiment logs | `~/cps_agent/experiment_logs/` | Yes (volume mount) |
| Outputs | `~/cps_agent/outputs/` | Yes (volume mount) |
| API keys | `~/cps_agent/.env` | Yes (excluded from rsync) |


## Config reference

Set as env vars before `./deploy.sh`:

| Variable | Default | What |
|---|---|---|
| `SERVER` | (required) | SSH host, e.g. `cbates@gpu2.ihmc.us` |
| `REMOTE_DIR` | `~/cps_agent` | Path on server |
| `OLLAMA_PORT` | `11434` | Host port for Ollama (use `11435` on gpu2 since `11434` is taken) |
| `OLLAMA_MODEL` | (none) | Model to pull with `--full` (omit to pull on demand) |


## Eval pipeline flags

### `run_eval.py`

| Flag | Default | What |
|---|---|---|
| `--dataset` | `datasets/test_puzzles.csv` | Path to CSV dataset |
| `--config` | `configs/config.yaml` | Agent config YAML |
| `--provider` | (from config) | LLM provider: `deepseek` (local Ollama), `deepseek-cloud`, `claude`, `openai` |
| `--model` | (from config) | Model name override (e.g. `deepseek-r1:70b`, `deepseek-reasoner`) |
| `--base-url` | (from config) | OpenAI-compatible API URL (not needed for cloud presets) |
| `--thinking` | off | Enable model thinking/CoT (e.g. `--thinking` or `--thinking high`) |
| `--timeout` | (none for local) | Per-request timeout in seconds |
| `--limit` | (all) | Only run the first N problems |
| `--epochs` | `1` | Number of times to sample each problem |
| `--epochs-reducer` | `mean` | How to combine epoch scores: `mean`, `majority_vote`, `any`, `all` |
| `--experiment-log-dir` | `experiment_logs` | Where per-problem .md/.json conversation logs go (set to `''` to disable) |
| `--results-dir` | `eval_results` | Where aggregate results JSON goes |

### `run_batch_eval.sh`

| Flag | Default | What |
|---|---|---|
| `--provider` | `deepseek` | LLM provider (e.g. `deepseek`, `deepseek-cloud`) |
| `--base-url` | (required for local) | Ollama API URL (not needed for cloud presets) |
| `--limit N` | (all) | Pass `--limit N` to each `run_eval.py` call |
| `--epochs N` | `1` | Pass `--epochs N` to each `run_eval.py` call |
| `--thinking` | off | Enable thinking for all runs (normally controlled per-group in the script) |

Examples:
```bash
# Remote server with Ollama — smoke test
./run_batch_eval.sh --base-url http://localhost:11435/v1 --limit 2 --epochs 3

# DeepSeek cloud API — run from laptop (no --base-url needed)
./run_batch_eval.sh --provider deepseek-cloud --limit 5
```


## Inspect AI display settings

`run_batch_eval.sh` sets these by default, but you can override them:

```bash
export INSPECT_DISPLAY=plain   # plain text output (use "full" for interactive TUI)
export INSPECT_LOG_LEVEL=info  # console log verbosity (debug, info, warning, error)
```

If running manually inside the container, set these before your `python run_eval.py` command.


## Running with DeepSeek cloud API

No Docker or GPU server needed — runs directly on your laptop against DeepSeek's hosted API.

### Setup

1. Create an account at [platform.deepseek.com](https://platform.deepseek.com) and add credits
2. Generate an API key and set it:
   ```bash
   export DEEPSEEK_API_KEY="sk-..."
   ```

### Model names

DeepSeek's API uses two model IDs (they point to the latest version automatically):

| Model ID | What |
|---|---|
| `deepseek-chat` | DeepSeek-V3 (general, non-reasoning) |
| `deepseek-reasoner` | DeepSeek-R1 (reasoning/thinking model) |

### Single experiment

```bash
python run_experiment.py \
    --provider deepseek-cloud \
    --model deepseek-reasoner \
    --config configs/config_minute_cryptic_baseline.yaml \
    --dataset datasets/minute_cryptic.csv \
    --problem-id 2 \
    --thinking
```

### Batch evaluation

```bash
# Full batch (all configs × models defined in run_batch_eval.sh)
./run_batch_eval.sh --provider deepseek-cloud

# Smoke test with 1 problem
./run_batch_eval.sh --provider deepseek-cloud --limit 1
```

The batch script runs **thinking models on baseline config** and **non-thinking on prompt-based
configs** (keep_thinking_step_by_step, generate_vars). Edit the `MODELS` and `RUN_GROUPS`
arrays in `run_batch_eval.sh` to customize.


## Troubleshooting

**Port already allocated**
Someone else is using that port. Pick a different `OLLAMA_PORT`:
```bash
OLLAMA_PORT=11435 ./deploy.sh --full
```

**Connection refused when running experiments**
Ollama container probably isn't running:
```bash
ssh cbates@gpu2.ihmc.us "cd ~/cps_agent && OLLAMA_PORT=11435 docker compose up -d ollama"
```

**`.env not found`**
Create it on the server: `ssh cbates@gpu2.ihmc.us "touch ~/cps_agent/.env"`

**Want to pull a new model?**
```bash
ssh cbates@gpu2.ihmc.us "docker exec cps_agent-ollama-1 ollama pull deepseek-r1:8b"
```
