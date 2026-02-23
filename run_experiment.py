"""
Run the tree search agent on a single problem (for debugging / development).

For batch evaluation across datasets, use Inspect AI instead:
    inspect eval eval_task.py -T dataset_path=datasets/puzzles.csv
    python run_eval.py --dataset datasets/puzzles.csv

Single problem (YAML dataset):
    python run_experiment.py --problem-id life_universe
    python run_experiment.py --provider claude --problem-id minute_cryptic_0

Single problem (CSV dataset):
    python run_experiment.py --dataset datasets/bigbench-rosetta.csv --problem-id 3
    python run_experiment.py --dataset datasets/test_puzzles.csv --problem-id my_id

List available problems:
    python run_experiment.py --list-problems
    python run_experiment.py --dataset datasets/bigbench-rosetta.csv --list-problems
"""

import argparse
import csv
import logging
import re
import yaml
from pathlib import Path

from tree_agent import AgentConfig, SearchAgent
from baseline_agents import create_agent
from llm_clients import create_client, LLMCallRecord
from experiment_logger import ExperimentLogger
from problem_loader import load_problems, make_ground_truth


logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mock LLM (exercises all three phases without API keys)
# ---------------------------------------------------------------------------

class MockLLM:
    def __init__(self):
        self.call_count = 0
        self.usage_log: list[LLMCallRecord] = []

    # Matches context-header lines like "[root] ..." or "[prompt → root] ..."
    _CONTEXT_LINE_RE = re.compile(r"^\[(prompt\s*→\s*)?\w[\w\-]*\]\s")

    @classmethod
    def _extract_instruction(cls, prompt: str) -> str:
        """Extract the instruction (last appended section) from a prompt.

        The agent builds prompts as: context + "\\n\\n" + instruction.
        Context lines are prefixed with "[prompt → name] ..." or "[name] ...".
        The instruction is the trailing section without those prefixes.
        """
        lines = prompt.strip().split("\n")
        # Walk backwards to find where context ends
        instruction_lines = []
        for line in reversed(lines):
            if cls._CONTEXT_LINE_RE.match(line):
                break
            instruction_lines.append(line)
        instruction_lines.reverse()
        return "\n".join(instruction_lines).strip()

    def __call__(self, prompt: str) -> str:
        self.call_count += 1
        self.usage_log.append(LLMCallRecord())
        # Match against just the instruction portion (after context).
        p = self._extract_instruction(prompt).lower()

        # Solution extraction
        if "extract and state the final solution" in p:
            if self.call_count > 16:
                return "42"
            return "7"

        # Leaf check
        if "have they proposed an answer yet" in p:
            return "YES" if self.call_count > 12 else "NO"

        # Revision
        if "none of those ended up working" in p:
            return (
                "1. Try constraint relaxation approach\n"
                "2. Try simulated annealing"
            )

        # Sort
        if "sort the options" in p:
            return "[2, 1, 3]"

        # begin_solve_prompt (check before continuation — the begin_solve
        # call's context includes an earlier "proceed with option" prompt)
        if "begin attempting a solution" in p:
            return (
                "CHOICE POINT\n"
                "We need to pick a representation.\n"
                "1. Binary encoding\n"
                "2. Integer encoding\n"
                "3. Permutation encoding"
            )

        # continuation_prompt
        if "proceed with option" in p:
            return (
                "CHOICE POINT\n"
                "Next, choose the objective function.\n"
                "1. Minimize total cost\n"
                "2. Maximize throughput\n"
                "The answer is 42."
            )

        # Phase 2: search_strategy_prompt
        if "search procedures" in p:
            return (
                "1. Systematic constraint propagation\n"
                "2. Greedy heuristic with backtracking\n"
                "3. Divide and conquer by subproblem"
            )

        # Phase 1: parent_class_prompt (checked last — its keywords appear
        # in the context of all subsequent prompts)
        if "identify the class of problem" in p:
            return (
                "1. Class: combinatorial optimization.\n"
                "2. Dimensions: search space size, constraint structure, objective type.\n"
                "3. Decisions: representation, search method, pruning strategy.\n"
                "4. Generate via: enumerate standard approaches for each dimension."
            )

        # Fallback
        return "1. Default option A\n2. Default option B"


def mock_ground_truth(problem: str, solution: str) -> tuple[bool, str]:
    if "42" in solution:
        return True, ""
    return False, f"Expected 42, got: {solution[:50]}"


# ---------------------------------------------------------------------------
# Config helper
# ---------------------------------------------------------------------------

def load_problems_from_csv(path: str | Path) -> dict[str, dict]:
    """Load problems from a CSV file.

    If the CSV has an 'id' column, use that as the problem ID.
    Otherwise, assign 1-indexed row numbers (matching Inspect AI's auto_id).
    """
    problems: dict[str, dict] = {}
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        has_id_col = "id" in (reader.fieldnames or [])
        for i, row in enumerate(reader, start=1):
            pid = row["id"].strip() if has_id_col else str(i)
            if pid in problems:
                raise ValueError(f"Duplicate problem id: {pid!r}")
            problems[pid] = {
                "problem": row["problem"].strip(),
                "solution": row["solution"].strip(),
            }
    return problems


def create_client_from_config(
    config: AgentConfig,
    provider_override: str | None = None,
):
    """Create an LLM client from an AgentConfig, passing through tools_config."""
    llm_cfg = config.llm_config
    provider = provider_override or llm_cfg.get("provider", "claude")
    return create_client(
        provider=provider,
        model=llm_cfg.get("model"),
        temperature=llm_cfg.get("temperature", 0.7),
        max_tokens=llm_cfg.get("max_tokens", 4096),
        system_prompt=llm_cfg.get("system_prompt", ""),
        tools_config=config.tools_config,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    default_problems = str(Path(__file__).parent / "problems.yaml")

    parser = argparse.ArgumentParser(description="Tree search agent (single problem)")
    parser.add_argument(
        "--provider",
        choices=["claude", "openai", "mock"],
        default="mock",
    )
    parser.add_argument("--config", default="configs/config.yaml")
    parser.add_argument("--problems", default=default_problems,
                        help="Path to problems YAML dataset")
    parser.add_argument("--dataset",
                        help="Path to a CSV dataset (overrides --problems). "
                             "If the CSV has an 'id' column, --problem-id matches it; "
                             "otherwise --problem-id is a 1-indexed row number.")
    parser.add_argument("--problem-id",
                        help="ID of the problem to solve (from the dataset)")
    parser.add_argument("--list-problems", action="store_true",
                        help="List available problems and exit")
    parser.add_argument("--log-dir", default="experiment_logs",
                        help="Directory for experiment log files (default: experiment_logs)")
    args = parser.parse_args()

    # Load the problem dataset
    if args.dataset:
        problems = load_problems_from_csv(args.dataset)
    else:
        problems = load_problems(args.problems)

    if args.list_problems:
        print("Available problems:")
        for pid, entry in problems.items():
            preview = entry["problem"][:70].replace("\n", " ")
            print(f"  {pid:20s}  {preview}...")
        return

    # Single problem mode
    problem_id = args.problem_id or next(iter(problems))
    if problem_id not in problems:
        parser.error(
            f"Unknown problem id: {problem_id!r}. "
            f"Available: {', '.join(problems)}"
        )

    entry = problems[problem_id]
    problem = entry["problem"]
    ground_truth = make_ground_truth(entry["solution"])

    config = AgentConfig.from_yaml(args.config)

    if args.provider == "mock":
        llm = MockLLM()
    else:
        llm = create_client_from_config(config, provider_override=args.provider)

    # Set up experiment logger
    llm_cfg = config.llm_config
    model = llm_cfg.get("model", "mock") if args.provider != "mock" else "mock"
    exp_logger = ExperimentLogger(log_dir=args.log_dir)
    exp_logger.initialize(
        problem_id=problem_id,
        problem=problem,
        config=config,
        provider=args.provider,
        model=model,
        config_path=args.config,
    )

    agent = create_agent(config, llm, ground_truth, exp_logger=exp_logger)
    solution = agent.solve(problem)

    # Write tree structure and summary to log
    if hasattr(agent, "root"):
        exp_logger.log_tree(agent.root)
    usage_log = getattr(llm, "usage_log", [])
    exp_logger.log_summary(solution, usage_log)
    exp_logger.finalize()

    print("\n" + "=" * 60)
    if solution:
        print(f"SOLUTION FOUND: {solution}")
    else:
        print("NO SOLUTION FOUND within limits.")

    if hasattr(agent, "print_tree"):
        print("\nTree structure:")
        print("=" * 60)
        agent.print_tree()

    if hasattr(llm, "call_count"):
        print(f"\nTotal LLM calls: {llm.call_count}")

    print(f"\nExperiment log: {exp_logger.log_path}")


if __name__ == "__main__":
    main()
