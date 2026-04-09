"""
Experiment logger that writes a Markdown log file during agent runs.

Records every LLM call (prompt, response, token usage, timing) incrementally
for crash resilience, then appends a tree structure and summary at the end.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO, TYPE_CHECKING

if TYPE_CHECKING:
    from agents import AgentConfig, Node
    from llm_clients import LLMCallRecord

logger = logging.getLogger(__name__)


@dataclass
class ExperimentLogger:
    log_dir: str | Path = "experiment_logs"
    log_path: Path | None = field(default=None, init=False)
    _file: TextIO | None = field(default=None, init=False, repr=False)
    _call_count: int = field(default=0, init=False)

    def initialize(
        self,
        problem_id: str,
        problem: str,
        config: "AgentConfig",
        provider: str,
        model: str,
        config_path: str = "",
        dataset_path: str = "",
        thinking: bool | str = False,
    ) -> Path:
        """Create the log directory and file, write the header.

        Returns the path to the log file.
        """
        log_dir = Path(self.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_id = problem_id.replace("/", "_").replace(" ", "_")
        filename = f"{timestamp}_{safe_id}.md"
        self.log_path = log_dir / filename

        self._file = open(self.log_path, "w")
        self._write("# Experiment Log\n\n")

        # Metadata
        self._write("## Metadata\n")
        self._write(f"- **Problem ID**: {problem_id}\n")
        self._write(f"- **Timestamp**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        self._write(f"- **Provider**: {provider}\n")
        self._write(f"- **Model**: {model}\n")
        self._write(f"- **Thinking**: {thinking}\n")
        if config_path:
            self._write(f"- **Config**: {config_path}\n")
        if dataset_path:
            self._write(f"- **Dataset**: {dataset_path}\n")
        agent_class = config.agent_class or "multi_turn"
        self._write(f"- **Agent class**: {agent_class}\n")
        self._write("\n")

        # Problem statement
        self._write("## Problem Statement\n\n")
        self._write(f"```\n{problem}\n```\n\n")

        # LLM calls header
        self._write("## LLM Calls (Chronological)\n\n")
        self._flush()

        logger.info(f"Experiment log: {self.log_path}")
        return self.log_path

    def log_llm_call(
        self,
        node_name: str,
        purpose: str,
        prompt: str,
        response: str,
        usage: "LLMCallRecord | None" = None,
        reasoning: str = "",
    ) -> None:
        """Append a single LLM call entry. Written immediately for crash safety."""
        if self._file is None:
            return

        self._call_count += 1
        n = self._call_count

        self._write(f"### Call {n}: {node_name} ({purpose})\n\n")

        if usage:
            parts = [f"{usage.input_tokens:,} in / {usage.output_tokens:,} out"]
            if usage.wall_time_seconds > 0:
                parts.append(f"{usage.wall_time_seconds:.1f}s")
            if usage.tool_rounds > 1:
                parts.append(f"{usage.tool_rounds} tool rounds")
            self._write(f"**Tokens**: {' | '.join(parts)}\n\n")

        self._write("**Prompt**:\n\n")
        self._write(f"````\n{prompt}\n````\n\n")
        if reasoning:
            self._write("**Reasoning/Thinking**:\n\n")
            self._write(f"````\n{reasoning}\n````\n\n")
        self._write("**Response**:\n\n")
        self._write(f"````\n{response}\n````\n\n")
        self._write("---\n\n")
        self._flush()

    def log_tree(self, root: "Node | None") -> None:
        """Write the final tree structure."""
        if self._file is None or root is None:
            return

        self._write("## Tree Structure\n\n")
        self._write("```\n")
        self._write_tree_node(root, indent=0)
        self._write("```\n\n")
        self._flush()

    def _write_tree_node(self, node: "Node", indent: int) -> None:
        prefix = "  " * indent
        phase_tag = node.phase.value
        status = node.status.name
        rev_info = " [REV]" if node.is_revision else ""

        tokens_info = ""
        if node.input_tokens or node.output_tokens:
            tokens_info = f" [{node.input_tokens:,}in/{node.output_tokens:,}out"
            if node.wall_time_seconds > 0:
                tokens_info += f" {node.wall_time_seconds:.1f}s"
            tokens_info += "]"

        content_preview = node.content[:80].replace("\n", " ") if node.content else ""
        self._write(
            f"{prefix}{node.name} ({phase_tag}|{status}){rev_info}{tokens_info}: "
            f"{content_preview}...\n"
        )
        for child in node.children.values():
            self._write_tree_node(child, indent + 1)

    def log_summary(
        self,
        solution: str | None,
        usage_log: "list[LLMCallRecord]",
    ) -> None:
        """Write the token summary table."""
        if self._file is None:
            return

        self._write("## Result\n\n")
        if solution:
            self._write(f"**Solution found**: {solution}\n\n")
        else:
            self._write("**No solution found** within limits.\n\n")

        total_input = sum(r.input_tokens for r in usage_log)
        total_output = sum(r.output_tokens for r in usage_log)
        total_tokens = total_input + total_output
        total_time = sum(r.wall_time_seconds for r in usage_log)

        self._write("## Token Summary\n\n")
        self._write("| Metric | Value |\n")
        self._write("|--------|-------|\n")
        self._write(f"| Total LLM calls | {len(usage_log)} |\n")
        self._write(f"| Total input tokens | {total_input:,} |\n")
        self._write(f"| Total output tokens | {total_output:,} |\n")
        self._write(f"| Total tokens | {total_tokens:,} |\n")
        self._write(f"| Wall clock time | {total_time:.1f}s |\n")
        self._write("\n")
        self._flush()

    def log_json_tree(self, tree_data: dict) -> Path | None:
        """Write a JSON tree file alongside the Markdown log."""
        if self.log_path is None:
            return None
        json_path = self.log_path.with_suffix(".json")
        with open(json_path, "w") as f:
            json.dump(tree_data, f, indent=2, ensure_ascii=False)
        logger.info(f"JSON tree log: {json_path}")
        return json_path

    def finalize(self) -> None:
        """Flush and close the log file."""
        if self._file is not None:
            self._file.close()
            self._file = None
            logger.info(f"Experiment log finalized: {self.log_path}")

    def _write(self, text: str) -> None:
        if self._file is not None:
            self._file.write(text)

    def _flush(self) -> None:
        if self._file is not None:
            self._file.flush()
