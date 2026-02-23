"""
Run Inspect AI evaluations on problem datasets.

This is a thin CLI wrapper around Inspect's eval(). For most use cases,
you can use Inspect's native CLI instead:

    inspect eval eval_task.py -T dataset_path=datasets/puzzles.csv -T config_path=configs/config.yaml

This script provides a convenience entry point:

    python run_eval.py --dataset datasets/puzzles.csv --config configs/config.yaml
    python run_eval.py --dataset puzzles.csv --limit 5
"""

import argparse
import logging

from inspect_ai import eval as inspect_eval

from eval_task import problem_eval

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


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
        "--log-dir",
        default=None,
        help="Directory for Inspect logs (default: ./logs)",
    )
    args = parser.parse_args()

    task = problem_eval(
        dataset_path=args.dataset,
        config_path=args.config,
    )

    eval_kwargs = {}
    if args.limit is not None:
        eval_kwargs["limit"] = args.limit
    if args.log_dir is not None:
        eval_kwargs["log_dir"] = args.log_dir

    logs = inspect_eval(task, **eval_kwargs)

    # Print summary
    for log in logs:
        if log.results:
            print(f"\nResults:")
            for metric in log.results.scores:
                print(f"  {metric.name}:")
                for k, v in metric.metrics.items():
                    print(f"    {k}: {v.value:.3f}")


if __name__ == "__main__":
    main()
