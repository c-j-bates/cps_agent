"""
Run Inspect AI evaluations on problem datasets.

This is a thin CLI wrapper around Inspect's eval(). For most use cases,
you can use Inspect's native CLI instead:

    inspect eval eval_task.py -T dataset_path=datasets/puzzles.csv -T config_path=configs/config.yaml

This script provides a convenience entry point:

    python run_eval.py --dataset datasets/puzzles.csv --config configs/config.yaml
    python run_eval.py --dataset puzzles.csv --limit 5
    python run_eval.py --provider deepseek --model deepseek-r1:70b --base-url http://localhost:11435/v1
"""

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

from inspect_ai import eval as inspect_eval
from inspect_ai._eval.task.epochs import Epochs

from eval_task import problem_eval, bongard_eval

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def get_model_display_name(model: str, thinking: bool | str, provider: str) -> str:
    """Generate a display name for file naming that encodes the thinking level.

    Claude:    claude-opus-4-6-instant  (no thinking)
               claude-opus-4-6-max      (thinking=max)
    DeepSeek:  deepseek-chat            (no thinking)
               deepseek-reasoner        (thinking on)
    """
    provider_lower = (provider or "").lower()

    # DeepSeek: thinking on → deepseek-reasoner, off → deepseek-chat
    if "deepseek" in provider_lower or "deepseek" in model.lower():
        return "deepseek-reasoner" if thinking else "deepseek-chat"

    # Claude / Anthropic: append thinking level or "instant"
    if provider_lower in ("claude", "anthropic") or "claude" in model.lower():
        if thinking:
            level = thinking if isinstance(thinking, str) else "high"
            return f"{model}-{level}"
        return f"{model}-instant"

    # Default fallback
    if thinking:
        level = thinking if isinstance(thinking, str) else "thinking"
        return f"{model}-{level}"
    return model


def _extract_results_json(log, args) -> dict:
    """Extract a structured results dict from an Inspect log + CLI args."""
    config_name = Path(args.config).stem  # e.g. "config_minute_cryptic_baseline"

    result = {
        "timestamp": datetime.now().isoformat(),
        "config_path": args.config,
        "config_name": config_name,
        "dataset": args.dataset,
        "provider": args.provider or "config_default",
        "model": args.model or "config_default",
        "thinking": args.thinking if args.thinking else False,
        "base_url": args.base_url or "",
        "epochs": args.epochs,
        "epochs_reducer": args.epochs_reducer if args.epochs > 1 else None,
        "eval_status": log.status,
        "eval_error": str(log.error) if getattr(log, "error", None) else None,
        "metrics": {},
        "samples": [],
    }

    # Extract aggregate metrics
    if log.results:
        for score_group in log.results.scores:
            for k, v in score_group.metrics.items():
                result["metrics"][k] = v.value

    # Extract per-sample results
    if log.samples:
        for sample in log.samples:
            sample_data = {
                "id": sample.id,
                "input": sample.input[:200] if isinstance(sample.input, str) else str(sample.input)[:200],
                "target": sample.target,
                "answer": None,
                "correct": False,
                "error": sample.error.message if getattr(sample, "error", None) else None,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "wall_time_seconds": 0,
                "llm_calls": 0,
                "graph_steps": 0,
                "terminated_early": False,
            }

            # Get per-scorer answer/correctness. For single-scorer tasks this
            # populates the top-level `answer`/`correct` fields (legacy
            # shape). For multi-scorer tasks (e.g. bongard_eval has both
            # nl_rule_scorer and python_rule_scorer) we ALSO record each
            # scorer's output under sample_data["scores"][scorer_name].
            if sample.scores:
                sample_data["scores"] = {}
                for scorer_name, score in sample.scores.items():
                    sample_data["scores"][scorer_name] = {
                        "answer": score.answer,
                        "correct": score.value == "C",
                        "explanation": score.explanation,
                    }
                    # Also populate the legacy top-level fields from the
                    # first scorer (kept for backward compat with analyze_results.py
                    # callers that expect a single `correct`/`answer`).
                    if "answer" not in sample_data or sample_data["answer"] is None:
                        sample_data["answer"] = score.answer
                        sample_data["correct"] = score.value == "C"

            # Get per-sample metrics from metadata
            metadata = sample.metadata or {}
            for key in ["input_tokens", "output_tokens", "total_tokens",
                        "wall_time_seconds", "llm_calls", "graph_steps",
                        "terminated_early"]:
                if key in metadata:
                    sample_data[key] = metadata[key]

            # Bongard: also surface the intermediate NL rule (from
            # extract_rule side-channel node) for easier post-hoc review
            if "nl_rule" in metadata:
                sample_data["nl_rule"] = metadata["nl_rule"]

            # Capture solver-level errors stored in metadata
            if "solver_error" in metadata:
                sample_data["solver_error"] = metadata["solver_error"]

            result["samples"].append(sample_data)

    # Compute aggregates from samples
    samples = result["samples"]
    if samples:
        result["aggregate"] = {
            "num_samples": len(samples),
            "num_correct": sum(1 for s in samples if s["correct"]),
            "accuracy": sum(1 for s in samples if s["correct"]) / len(samples),
            "total_input_tokens": sum(s["input_tokens"] for s in samples),
            "total_output_tokens": sum(s["output_tokens"] for s in samples),
            "total_tokens": sum(s["total_tokens"] for s in samples),
            "mean_tokens_per_sample": sum(s["total_tokens"] for s in samples) / len(samples),
            "total_wall_time": sum(s["wall_time_seconds"] for s in samples),
            "total_llm_calls": sum(s["llm_calls"] for s in samples),
            "mean_graph_steps": sum(s["graph_steps"] for s in samples) / len(samples),
        }

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Run problem evaluation via Inspect AI"
    )
    parser.add_argument(
        "--dataset",
        default="datasets/test_puzzles.csv",
        help="Path to CSV dataset (columns: problem, solution)",
    )
    parser.add_argument(
        "--config",
        default="configs/config.yaml",
        help="Path to agent config YAML",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of samples to evaluate",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the LLM model name from the config",
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="Override the LLM provider (e.g. deepseek, ollama, claude, openai)",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Base URL for OpenAI-compatible API (e.g. http://localhost:11435/v1)",
    )
    parser.add_argument(
        "--epochs",
        type=int,
        default=1,
        help="Number of times to sample each problem (default: 1)",
    )
    parser.add_argument(
        "--epochs-reducer",
        default="mean",
        choices=["mean", "majority_vote", "any", "all"],
        help="How to combine scores across epochs (default: mean)",
    )
    parser.add_argument(
        "--thinking",
        nargs="?", const=True, default=False,
        metavar="EFFORT",
        help="Enable extended thinking / reasoning (off by default). "
             "For Anthropic: adaptive mode with optional effort "
             "(low, medium, high, max). "
             "For Ollama: passes think=true to the API so models "
             "like DeepSeek R1 and QwQ return reasoning chains.",
    )
    parser.add_argument(
        "--timeout",
        type=float, default=None,
        help="Per-request timeout in seconds for LLM API calls "
             "(default: 600s). Increase for large local models.",
    )
    parser.add_argument(
        "--log-dir",
        default=None,
        help="Directory for Inspect logs (default: ./logs)",
    )
    parser.add_argument(
        "--exp-name",
        default=None,
        help="Experiment name — creates a subdirectory under both "
             "eval_results/ and experiment_logs/ to group related runs "
             "(e.g. --exp-name minute-cryptic-par1)",
    )
    parser.add_argument(
        "--results-dir",
        default="eval_results",
        help="Base directory for JSON result files (default: eval_results)",
    )
    parser.add_argument(
        "--experiment-log-dir",
        default="experiment_logs",
        help="Base directory for per-problem .md and .json conversation logs "
             "(default: experiment_logs, set to '' to disable)",
    )
    parser.add_argument(
        "--display",
        default="plain",
        choices=["plain", "rich", "textual"],
        help="Terminal display mode: 'plain' for simple text output, "
             "'rich' for panel-based UI, 'textual' for full TUI "
             "(default: plain)",
    )
    parser.add_argument(
        "--max-connections", type=int, default=1,
        help="Max concurrent API connections (default: 1). Lower values "
             "flush progress to log more frequently, reducing lost work "
             "if the job is interrupted.",
    )
    args = parser.parse_args()

    # Apply --exp-name as subdirectory under both output dirs
    results_dir = Path(args.results_dir)
    log_base = Path(args.experiment_log_dir) if args.experiment_log_dir else None
    if args.exp_name:
        results_dir = results_dir / args.exp_name
        if log_base:
            log_base = log_base / args.exp_name

    # Build a descriptive per-run subdirectory for experiment logs
    run_log_dir = ""
    effective_provider = args.provider or "config_default"
    effective_model = args.model or "default"
    display_model = get_model_display_name(
        effective_model, args.thinking, effective_provider,
    ).replace(":", "_").replace("/", "_")
    if log_base:
        config_name = Path(args.config).stem
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_subdir = f"{timestamp}_{config_name}_{display_model}"
        run_log_dir = str(log_base / run_subdir)

    # Auto-select the task factory based on config filename.
    # Bongard configs (config_bongard_*.yaml) use bongard_eval, which
    # replaces the literal-match scorer with an LLM-as-judge NL scorer
    # against `state.metadata["nl_rule"]`.
    config_stem = Path(args.config).stem
    if config_stem.startswith("config_bongard"):
        task = bongard_eval(
            dataset_path=args.dataset,
            config_path=args.config,
            model_override=args.model or "",
            thinking=args.thinking,
            provider_override=args.provider or "",
            base_url=args.base_url or "",
            experiment_log_dir=run_log_dir,
            timeout=args.timeout,
        )
    else:
        task = problem_eval(
            dataset_path=args.dataset,
            config_path=args.config,
            model_override=args.model or "",
            thinking=args.thinking,
            provider_override=args.provider or "",
            base_url=args.base_url or "",
            experiment_log_dir=run_log_dir,
            timeout=args.timeout,
        )

    eval_kwargs = {}
    if args.limit is not None:
        eval_kwargs["limit"] = args.limit
    if args.log_dir is not None:
        eval_kwargs["log_dir"] = args.log_dir
    if args.epochs > 1:
        eval_kwargs["epochs"] = Epochs(args.epochs, reducer=args.epochs_reducer)
    if args.display is not None:
        eval_kwargs["display"] = args.display
    if args.max_connections is not None:
        eval_kwargs["max_connections"] = args.max_connections

    logs = inspect_eval(task, **eval_kwargs)

    # Print summary and save results JSON
    for log in logs:
        if log.results:
            print(f"\nResults:")
            for metric in log.results.scores:
                print(f"  {metric.name}:")
                for k, v in metric.metrics.items():
                    print(f"    {k}: {v.value:.3f}")

        # Write results JSON
        results = _extract_results_json(log, args)
        if run_log_dir:
            results["experiment_log_dir"] = run_log_dir

        results_dir.mkdir(parents=True, exist_ok=True)

        config_name = Path(args.config).stem
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        results_path = results_dir / f"{ts}_{config_name}_{display_model}.json"

        with open(results_path, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nResults JSON:      {results_path}")
        if run_log_dir:
            print(f"Conversation logs: {run_log_dir}/")


if __name__ == "__main__":
    main()
