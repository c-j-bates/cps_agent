# CPS Agent

Experimental framework for studying creative problem solving (CPS) with LLMs. This project evaluates how different prompting strategies affect an LLM's ability to solve novel problems that require the solver to simultaneously formulate and solve them, i.e. problems where the right approach isn't obvious from the start. An accompanying paper presenting results presents a formalism, from which we derive a novel prompting strategy ("generate-vars") and compare to previous general-purpose prompting strategies.

The core idea: when a problem is genuinely new to the solver, success depends not just on domain knowledge but on how the solver searches the space of possible formulations. We test this by comparing prompting strategies that structure the LLM's search process in different ways, evaluated on cryptic crossword clues and Rosetta Stone-style translation puzzles.

The framework includes a graph-based multi-turn agent, an evaluation pipeline built on [Inspect AI](https://inspect.ai-safety-institute.org.uk/), and a topological data analysis (TDA) module for characterizing how different strategies explore the solution space.


## Setup

### Requirements

- Python 3.11+
- API keys for your chosen LLM provider(s)

### Install

```bash
pip install -r requirements.txt
```

### API keys

Set the appropriate environment variable for your provider:

```bash
export ANTHROPIC_API_KEY="sk-..."    # Claude
export DEEPSEEK_API_KEY="sk-..."     # DeepSeek cloud API
export OPENAI_API_KEY="sk-..."       # OpenAI
```

### Docker (optional, for remote GPU servers)

For running local models (e.g. DeepSeek R1 via Ollama) on a remote server, see [DOCKER_QUICKSTART.md](DOCKER_QUICKSTART.md).


## Project structure

```
configs/               Agent config YAMLs (one per prompting strategy + domain)
datasets/              CSV datasets (problem, solution columns)
agents.py              Agent implementations (MultiTurnAgent, SingleTurnAgent, etc.)
llm_clients.py         LLM client implementations (Anthropic, OpenAI, Ollama, etc.)
eval_task.py           Inspect AI task definition (dataset loader, solver, scorer)
run_experiment.py      Run a single problem (debugging / development)
run_eval.py            Run batch evaluation via Inspect AI
run_batch_eval.sh      Run a matrix of configs x models
analyze_results.py     Generate comparison tables and plots from eval results
tda_analysis/          TDA pipeline for characterizing solution-space exploration
experiment_logger.py   Per-problem conversation logging (.md and .json)
```


## Running experiments

### Single problem (development / debugging)

```bash
# With mock LLM (no API key needed)
python run_experiment.py --config configs/config_minute_cryptic_baseline.yaml

# With a real provider
python run_experiment.py \
    --provider claude \
    --config configs/config_minute_cryptic_generate_vars.yaml \
    --dataset datasets/minute_cryptic_easy.csv \
    --problem-id 3
```

Key flags:
| Flag | Default | Description |
|---|---|---|
| `--provider` | `mock` | `claude`, `openai`, `deepseek`, `deepseek-cloud`, `ollama`, `mock` |
| `--config` | `configs/config.yaml` | Agent config YAML |
| `--dataset` | `datasets/minute_cryptic.csv` | CSV dataset |
| `--problem-id` | first row | Problem ID (from `id` column, or 1-indexed row number) |
| `--model` | from config | Override model name |
| `--thinking` | off | Enable extended thinking (e.g. `--thinking`, `--thinking max`) |
| `--base-url` | from config | OpenAI-compatible API URL (for Ollama, vLLM, etc.) |
| `--list-problems` | | List available problems and exit |

### Batch evaluation

`run_eval.py` wraps [Inspect AI](https://inspect.ai-safety-institute.org.uk/) to evaluate across a full dataset:

```bash
python run_eval.py \
    --dataset datasets/minute_cryptic_easy.csv \
    --config configs/config_minute_cryptic_generate_vars.yaml \
    --provider deepseek-cloud \
    --model deepseek-reasoner \
    --thinking
```

Results are saved to `eval_results/` as JSON files. Per-problem conversation logs go to `experiment_logs/`.

Additional flags beyond those in `run_experiment.py`:
| Flag | Default | Description |
|---|---|---|
| `--limit` | all | Only evaluate the first N problems |
| `--epochs` | `1` | Number of times to sample each problem |
| `--epochs-reducer` | `mean` | How to combine epoch scores: `mean`, `majority_vote`, `any`, `all` |
| `--exp-name` | auto | Experiment name (groups results into a subdirectory) |
| `--max-connections` | `1` | Max concurrent API connections |

### Batch matrix (configs x models)

`run_batch_eval.sh` runs a matrix of prompting strategies and models:

```bash
# Against DeepSeek cloud API
./run_batch_eval.sh --provider deepseek-cloud

# Against local Ollama
./run_batch_eval.sh --base-url http://localhost:11435/v1

# Smoke test
./run_batch_eval.sh --provider deepseek-cloud --limit 2

# Named experiment (for organizing results)
./run_batch_eval.sh --provider deepseek-cloud --name my-experiment
```

Edit the `DATASET`, `MODELS`, and `RUN_GROUPS` arrays at the top of the script to configure which configs and models to run. Run groups control the `--thinking` flag per config group (thinking on for baseline, off for prompt-based strategies).

Results land in `eval_results/<experiment-name>/` and conversation logs in `experiment_logs/<experiment-name>/`.


## Configs and strategies

Each YAML config defines an `agent_class` and a prompt graph. All current configs use the `multi_turn` agent class, which executes a directed graph of prompt nodes.

Two sets of configs exist: `config_minute_cryptic_*.yaml` (domain-specific prompts for cryptic crosswords) and `config_generic_*.yaml` (domain-agnostic versions).

Strategies:
- **baseline** -- Single-turn: present the problem and ask for a solution directly.
- **keep_thinking_step_by_step** -- Multi-turn chain: solve, then keep generating if the answer isn't found.
- **step_back** -- Identify the abstract problem class before solving.
- **self_discover** -- Select reasoning modules from a predefined list, adapt them to the task, then solve using a structured JSON format.
- **plan_and_solve** -- Devise a plan, then carry it out step by step.
- **generate_vars** -- Enumerate the problem's independent variables and their possible values, then systematically generate combinations.

### Config structure

Configs specify the LLM settings (`llm` section) and the prompt graph (`graph` section). The graph defines `nodes` (each with a prompt template), `edges` (transitions between nodes), and `side_channels` (sub-graphs that branch off for answer extraction/checking without affecting the main conversation).

Key settings:
- **`llm.temperature`** -- Sampling temperature (default 0.7 across all configs).
- **`llm.max_tokens`** -- Per-response token limit (65536 for cryptic configs).
- **`max_repeats` on edges** -- Controls how many times a self-loop edge can be traversed. This bounds the number of "keep generating" continuation turns. Currently set to 5 for all multi-turn strategies, meaning the agent gets up to 5 additional attempts after its initial solving phase. The baseline config has no continuation loop (single turn).
- **`side_channels[].check_answer`** -- When `true`, the side channel extracts an answer and checks it against ground truth, enabling early termination if the correct answer is found before `max_repeats` is exhausted.


## Analysis

### Eval results analysis

```bash
python analyze_results.py --results-dir eval_results/my-experiment
```

Reads JSON result files and produces:
- Summary table (accuracy, tokens, wall time, iterations per strategy and model)
- Accuracy comparison bar chart (grouped by strategy)
- Line plots showing solve rate as function of "keep generating" turns allowed
- ...

Output goes to `--output-dir` (default: `analysis_output/`). Requires `matplotlib`.

### TDA analysis

The `tda_analysis` module characterizes *how* different strategies explore the solution space, not just whether they find the answer. It codes each solver's outputs into a faceted idea representation, builds idea trees, computes topological features (via persistent homology), and compares strategies.

```bash
# Full pipeline: coding + TDA features + comparison
python -m tda_analysis experiment_logs/my-experiment --output-dir tda_results/my-experiment

# Just code the solver traces (skip TDA)
python -m tda_analysis experiment_logs/my-experiment --code-only

# Just compute features from previously cached coded traces
python -m tda_analysis experiment_logs/my-experiment --features-only

# Restrict to specific strategies
python -m tda_analysis experiment_logs/my-experiment --strategies baseline generate_vars
```

The pipeline has three stages:

1. **Coding** (`tda_analysis/coding.py`) -- Parses experiment log markdown files and uses an LLM to code each solver turn into a structured idea tree with four facets (parse, mechanism, execution, output).

2. **Feature extraction** (`tda_analysis/features.py`) -- Computes 18 features per (strategy, problem) pair: TDA features from Vietoris-Rips persistent homology, tree-structural features, per-facet entropy, and distance metrics.

3. **Comparison** (`tda_analysis/comparison.py`) -- Aggregates features across problems, produces radar charts and summary tables comparing strategies. Also runs logistic regression (`tda_analysis/regression.py`) to identify which features predict solve success, and gold-standard analysis (`tda_analysis/gold_analysis.py`) to compare strategy coverage against known good solutions.

Additional dependencies for TDA: `ripser`, `persim`, `scipy` (included in `requirements.txt`).
