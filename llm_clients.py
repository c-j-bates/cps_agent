"""
LLM client implementations for the tree search agent.

Supports Anthropic Claude and OpenAI. Both conform to the LLMClient protocol
(any callable str -> str).

Usage:
    from llm_clients import create_client

    llm = create_client("claude", model="claude-sonnet-4-20250514")
    llm = create_client("openai", model="gpt-4o")

    # Or construct directly:
    llm = AnthropicClient(model="claude-sonnet-4-20250514")
    llm = OpenAIClient(model="gpt-4o")

    # With tool use (code execution):
    llm = AnthropicClient(
        model="claude-sonnet-4-20250514",
        tools_config={"code_execution": {"enabled": True, "timeout": 30}},
    )

Environment variables:
    ANTHROPIC_API_KEY  - required for Claude
    OPENAI_API_KEY     - required for OpenAI
"""

from __future__ import annotations

import os
import time
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class LLMCallRecord:
    """Token usage from a single LLM client __call__ invocation."""
    input_tokens: int = 0
    output_tokens: int = 0
    tool_rounds: int = 0
    wall_time_seconds: float = 0.0


# Tool definition for Python code execution
_RUN_PYTHON_TOOL = {
    "name": "run_python",
    # "description": "Execute a Python script and return stdout/stderr. Use this only when you need to run code to get a result you cannot reason out directly — e.g. numeric computation, generating large combinatorial outputs, or testing code. Do not use it for analysis, explanation, or reasoning tasks.",
    "description": "Execute a Python script and return stdout/stderr. Look at other user instructions for hints about when to use this tool.",
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python code to execute",
            }
        },
        "required": ["code"],
    },
}

# Map of tool names to their definitions
_AVAILABLE_TOOLS = {
    "code_execution": _RUN_PYTHON_TOOL,
}


def _build_tools_list(tools_config: dict) -> list[dict]:
    """Build the API tools list from a tools_config dict."""
    tools = []
    for tool_key, tool_def in _AVAILABLE_TOOLS.items():
        cfg = tools_config.get(tool_key, {})
        if cfg.get("enabled", False):
            tools.append(tool_def)
    return tools


def _dispatch_tool(tool_name: str, tool_input: dict, tools_config: dict) -> str:
    """Execute a tool call and return the result as a string."""
    if tool_name == "run_python":
        from code_executor import execute_python

        timeout = tools_config.get("code_execution", {}).get("timeout", 30)
        result = execute_python(tool_input["code"], timeout=timeout)

        parts = []
        if result.stdout:
            parts.append(f"stdout:\n{result.stdout}")
        if result.stderr:
            parts.append(f"stderr:\n{result.stderr}")
        if result.timed_out:
            parts.append(f"(timed out after {timeout}s)")
        if not parts:
            parts.append("(no output)")
        return "\n".join(parts)

    return f"Unknown tool: {tool_name}"


@dataclass
class AnthropicClient:
    """Claude via the Anthropic Python SDK.

    When tools_config enables code_execution, the client will pass tool
    definitions to the API and handle the tool_use/tool_result loop
    internally, returning the final text response.
    """

    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 4096
    temperature: float = 0.7
    system_prompt: str = ""
    api_key: str | None = None
    tools_config: dict = field(default_factory=dict)
    _client: object = field(default=None, repr=False, init=False)
    _tools: list = field(default=None, repr=False, init=False)

    MAX_TOOL_ROUNDS: int = 10

    def __post_init__(self):
        try:
            import anthropic
        except ImportError:
            raise ImportError("pip install anthropic")
        key = self.api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ValueError("ANTHROPIC_API_KEY not set")
        self._client = anthropic.Anthropic(api_key=key)
        self._tools = _build_tools_list(self.tools_config)
        self.usage_log: list[LLMCallRecord] = []

    def __call__(self, prompt: str) -> str:
        start = time.monotonic()
        total_input = 0
        total_output = 0
        rounds = 0

        messages = [{"role": "user", "content": prompt}]

        kwargs: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": messages,
        }
        if self.system_prompt:
            kwargs["system"] = self.system_prompt
        if self._tools:
            kwargs["tools"] = self._tools

        # Tool-use loop: call API, handle tool_use blocks, repeat
        for round_num in range(self.MAX_TOOL_ROUNDS):
            response = self._client.messages.create(**kwargs)
            total_input += response.usage.input_tokens
            total_output += response.usage.output_tokens
            rounds = round_num + 1
            logger.debug(
                f"Anthropic [{self.model}] round={round_num} "
                f"stop_reason={response.stop_reason} "
                f"tokens: in={response.usage.input_tokens} out={response.usage.output_tokens}"
            )

            print(prompt)
            print(response.content[0].text)
            breakpoint()

            if response.stop_reason != "tool_use":
                # Final response — extract text
                text = "".join(
                    block.text for block in response.content
                    if block.type == "text"
                )
                self.usage_log.append(LLMCallRecord(
                    input_tokens=total_input,
                    output_tokens=total_output,
                    tool_rounds=rounds,
                    wall_time_seconds=time.monotonic() - start,
                ))
                return text

            # Handle tool_use blocks
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    logger.info(
                        f"Tool call: {block.name}({block.input})"
                    )
                    result_text = _dispatch_tool(
                        block.name, block.input, self.tools_config
                    )
                    logger.info(f"Tool result: {result_text[:200]}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    })

            messages.append({"role": "user", "content": tool_results})
            kwargs["messages"] = messages

        # Exhausted tool rounds — return whatever text we have
        logger.warning(f"Exhausted {self.MAX_TOOL_ROUNDS} tool rounds")
        text = "".join(
            block.text for block in response.content
            if block.type == "text"
        )
        self.usage_log.append(LLMCallRecord(
            input_tokens=total_input,
            output_tokens=total_output,
            tool_rounds=rounds,
            wall_time_seconds=time.monotonic() - start,
        ))
        return text


@dataclass
class OpenAIClient:
    """OpenAI chat completions via the OpenAI Python SDK."""

    model: str = "gpt-4o"
    max_tokens: int = 4096
    temperature: float = 0.7
    system_prompt: str = ""
    api_key: str | None = None
    tools_config: dict = field(default_factory=dict)
    _client: object = field(default=None, repr=False, init=False)

    def __post_init__(self):
        try:
            import openai
        except ImportError:
            raise ImportError("pip install openai")
        key = self.api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ValueError("OPENAI_API_KEY not set")
        self._client = openai.OpenAI(api_key=key)
        self.usage_log: list[LLMCallRecord] = []

    def __call__(self, prompt: str) -> str:
        start = time.monotonic()

        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": prompt})

        response = self._client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            messages=messages,
        )
        text = response.choices[0].message.content or ""
        logger.debug(f"OpenAI [{self.model}] tokens: "
                      f"in={response.usage.prompt_tokens} out={response.usage.completion_tokens}")
        self.usage_log.append(LLMCallRecord(
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            wall_time_seconds=time.monotonic() - start,
        ))
        return text


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_PROVIDERS = {
    "claude": AnthropicClient,
    "anthropic": AnthropicClient,
    "openai": OpenAIClient,
    "gpt": OpenAIClient,
}


def create_client(
    provider: str,
    model: str | None = None,
    **kwargs,
) -> AnthropicClient | OpenAIClient:
    """
    Factory for LLM clients.

    Args:
        provider: "claude"/"anthropic" or "openai"/"gpt"
        model: Override the default model name.
        **kwargs: Passed to the client constructor (temperature, max_tokens,
                  system_prompt, api_key, tools_config).
    """
    provider = provider.lower().strip()
    cls = _PROVIDERS.get(provider)
    if cls is None:
        raise ValueError(
            f"Unknown provider {provider!r}. Options: {list(_PROVIDERS.keys())}"
        )
    if model is not None:
        kwargs["model"] = model
    return cls(**kwargs)
