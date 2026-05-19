"""
LLM client implementations for the tree search agent.

Supports Anthropic Claude, OpenAI, and any OpenAI-compatible API (Ollama,
vLLM, etc.). All conform to the LLMClient protocol (any callable str -> str).

Usage:
    from llm_clients import create_client

    llm = create_client("claude", model="claude-sonnet-4-20250514")
    llm = create_client("openai", model="gpt-4o")

    # Local models via Ollama:
    llm = create_client("deepseek", model="deepseek-r1:8b")
    llm = create_client("ollama", model="llama3")

    # Any OpenAI-compatible endpoint:
    llm = create_client("openai", model="my-model",
                         base_url="http://localhost:8000/v1")

    # Or construct directly:
    llm = AnthropicClient(model="claude-sonnet-4-20250514")
    llm = OpenAIClient(model="gpt-4o")
    llm = OpenAIClient(model="deepseek-r1:8b",
                        base_url="http://localhost:11434/v1")

    # With tool use (code execution):
    llm = AnthropicClient(
        model="claude-sonnet-4-20250514",
        tools_config={"code_execution": {"enabled": True, "timeout": 30}},
    )

Environment variables:
    ANTHROPIC_API_KEY  - required for Claude
    OPENAI_API_KEY     - required for OpenAI
    OLLAMA_API_KEY     - optional for Ollama (not needed for local)
    VLLM_API_KEY       - optional for vLLM (not needed for local)
"""

from __future__ import annotations

import os
import re
import time
import logging
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger(__name__)


class LLMClient(Protocol):
    """Protocol for LLM callables: str -> str."""
    def __call__(self, prompt: str) -> str: ...

# Track one-shot warnings so they only print once per process.
_warned_once: set[str] = set()


class LLMResponse(str):
    """LLM response that carries optional reasoning/thinking content.

    Behaves as a plain string (the final answer text) so all existing code
    that treats responses as strings works unchanged.  Access .reasoning
    for the model's chain-of-thought, and .full_text for the combined
    version with <thinking> tags (used in multi-turn context chains).
    """

    def __new__(cls, text: str, reasoning: str = ""):
        instance = super().__new__(cls, text)
        instance.reasoning = reasoning
        return instance

    @property
    def full_text(self) -> str:
        """Text with reasoning wrapped in <thinking> tags, for context chains."""
        if self.reasoning:
            return f"<thinking>\n{self.reasoning}\n</thinking>\n\n{str(self)}"
        return str(self)


@dataclass
class LLMCallRecord:
    """Token usage from a single LLM client __call__ invocation.

    ``channel`` is stamped by the caller (typically MultiTurnAgent._log_call)
    so token totals can later be filtered by channel. Values:
      - ``"main_channel"`` for main-graph nodes
      - ``"side:<sc_id>"`` (possibly nested as ``side:a/side:b``) for side
        channels
      - ``""`` for records not produced by an agent (e.g. coding/analysis
        calls). Treated as main-channel-equivalent by downstream filters.
    """
    input_tokens: int = 0
    output_tokens: int = 0
    tool_rounds: int = 0
    wall_time_seconds: float = 0.0
    channel: str = ""


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

    When thinking is set, extended thinking is enabled via adaptive mode
    (for Opus 4.6+). Temperature cannot be set when thinking is enabled.

    The thinking field accepts either a bool (True → adaptive with default
    "high" effort) or an effort string ("low", "medium", "high", "max").
    """

    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 4096
    temperature: float = 0.7
    system_prompt: str = ""
    api_key: str | None = None
    tools_config: dict = field(default_factory=dict)
    thinking: bool | str = False  # False=off, True=adaptive, str=effort level
    _client: object = field(default=None, repr=False, init=False)
    _tools: list = field(default=None, repr=False, init=False)

    MAX_TOOL_ROUNDS: int = 10
    MAX_API_RETRIES: int = 5
    RETRY_BASE_DELAY: float = 5.0  # seconds; doubles each retry

    def _call_api_with_retry(self, kwargs: dict, use_stream: bool):
        """Call the Anthropic API with retry on transient errors."""
        import httpx
        import anthropic

        for attempt in range(self.MAX_API_RETRIES):
            try:
                if use_stream:
                    with self._client.messages.stream(**kwargs) as stream:
                        return stream.get_final_message()
                else:
                    return self._client.messages.create(**kwargs)
            except (
                httpx.RemoteProtocolError,
                httpx.ReadError,
                httpx.ConnectError,
                anthropic.APIStatusError,
            ) as e:
                # Don't retry on genuine client errors (auth, validation).
                # Mid-stream server errors arrive as APIStatusError with
                # status_code=200 but error body containing 'api_error' or
                # 'overloaded_error' — these ARE retryable.
                if isinstance(e, anthropic.APIStatusError):
                    body = getattr(e, 'body', None) or {}
                    err_type = ""
                    if isinstance(body, dict):
                        err_type = (body.get('error') or {}).get('type', '')
                    retryable_types = {'api_error', 'overloaded_error'}
                    is_server_error = (
                        e.status_code >= 500
                        or e.status_code == 429
                        or err_type in retryable_types
                    )
                    if not is_server_error:
                        raise
                if attempt == self.MAX_API_RETRIES - 1:
                    raise
                logger.warning(
                    f"Anthropic API error (attempt {attempt + 1}/{self.MAX_API_RETRIES}): {e}"
                )
                try:
                    answer = input(
                        f"  Retry this request? [Y/n/q] "
                    ).strip().lower()
                except EOFError:
                    # Non-interactive (e.g. piped stdin) — auto-retry
                    answer = "y"
                if answer in ("n", "q", "quit", "no"):
                    raise

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
            "messages": messages,
        }
        if self.thinking:
            kwargs["thinking"] = {"type": "adaptive"}
            # temperature must not be set when thinking is enabled (defaults to 1)
            effort = self.thinking if isinstance(self.thinking, str) else None
            if effort:
                kwargs["output_config"] = {"effort": effort}
        else:
            kwargs["temperature"] = self.temperature
        if self.system_prompt:
            kwargs["system"] = self.system_prompt
        if self._tools:
            kwargs["tools"] = self._tools

        # Always use streaming to avoid Anthropic SDK's 10-min timeout
        use_stream = True

        # Tool-use loop: call API, handle tool_use blocks, repeat
        for round_num in range(self.MAX_TOOL_ROUNDS):
            response = self._call_api_with_retry(kwargs, use_stream)
            total_input += response.usage.input_tokens
            total_output += response.usage.output_tokens
            rounds = round_num + 1
            logger.debug(
                f"Anthropic [{self.model}] round={round_num} "
                f"stop_reason={response.stop_reason} "
                f"tokens: in={response.usage.input_tokens} out={response.usage.output_tokens}"
            )

            # DEBUG ######
            # print(prompt)
            # for block in response.content:
            #     if block.type == "thinking":
            #         print(f"[thinking] {block.thinking}...")
            #     elif block.type == "text":
            #         print("[text block]")
            #         print(block.text)
            # breakpoint()  # DON'T EVER DELETE ME!
            ###############

            if response.stop_reason != "tool_use":
                # Final response — extract all blocks (thinking + text)
                # Anthropic allows interleaved thinking/text, so keep
                # everything inline with <thinking> tags for demarcation.
                parts = []
                for block in response.content:
                    if block.type == "thinking":
                        parts.append(f"<thinking>\n{block.thinking}\n</thinking>")
                    elif block.type == "text":
                        parts.append(block.text)
                text = "\n\n".join(parts)
                self.usage_log.append(LLMCallRecord(
                    input_tokens=total_input,
                    output_tokens=total_output,
                    tool_rounds=rounds,
                    wall_time_seconds=time.monotonic() - start,
                ))
                return LLMResponse(text)

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
    """OpenAI chat completions via the OpenAI Python SDK.

    Works with any OpenAI-compatible API by setting base_url.
    This includes local servers (Ollama, vLLM) and open source model
    providers that expose the OpenAI chat completions format.

    When thinking is not None, the Ollama "think" parameter is passed
    via extra_body so that thinking models (DeepSeek R1, QwQ, etc.)
    return their reasoning chain in message.reasoning.

    Three modes:
      None  — don't send the parameter (model default behaviour)
      True  — send think=true  (enable reasoning)
      False — send think=false (suppress reasoning on models that
              default to thinking, like R1 and QwQ)
      str   — send think=<level> ("low"/"medium"/"high") for models
              like gpt-oss that support tiered reasoning

    Examples:
        # Standard OpenAI
        OpenAIClient(model="gpt-4o")

        # DeepSeek via Ollama
        OpenAIClient(model="deepseek-r1:8b",
                     base_url="http://localhost:11434/v1")

        # DeepSeek via Ollama with thinking enabled
        OpenAIClient(model="deepseek-r1:8b",
                     base_url="http://localhost:11434/v1",
                     thinking=True)

        # vLLM local server
        OpenAIClient(model="deepseek-ai/DeepSeek-R1",
                     base_url="http://localhost:8000/v1")
    """

    model: str = "gpt-4o"
    max_tokens: int = 4096
    temperature: float = 0.7
    system_prompt: str = ""
    api_key: str | None = None
    base_url: str | None = None
    api_key_env: str = "OPENAI_API_KEY"
    tools_config: dict = field(default_factory=dict)
    thinking: bool | str = False  # False=off, True=enable, str=effort level
    timeout: float | None = None  # Per-request timeout in seconds (None = SDK default of 600s)
    max_api_retries: int = 5
    retry_base_delay: float = 5.0
    _client: object = field(default=None, repr=False, init=False)

    def __post_init__(self):
        try:
            import openai
        except ImportError:
            raise ImportError("pip install openai")
        key = self.api_key or os.environ.get(self.api_key_env) or os.environ.get("OPENAI_API_KEY")
        if not key:
            if self.base_url:
                # Local servers (Ollama, vLLM) typically don't require auth
                key = "not-needed"
            else:
                raise ValueError(f"{self.api_key_env} not set")
        client_kwargs = {"api_key": key}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        # Set timeout: explicit value if provided, otherwise no timeout
        # for local servers (Ollama/vLLM have unpredictable latency),
        # SDK default (600s) for cloud APIs.
        if self.timeout is not None:
            import httpx
            client_kwargs["timeout"] = httpx.Timeout(self.timeout, connect=10.0)
        elif self.base_url:
            import httpx
            client_kwargs["timeout"] = httpx.Timeout(None, connect=10.0)
        self._client = openai.OpenAI(**client_kwargs)
        self.usage_log: list[LLMCallRecord] = []

        # Warn once if using deprecated DeepSeek model aliases. The names
        # `deepseek-chat` and `deepseek-reasoner` now silently route to
        # `deepseek-v4-flash` (non-thinking and thinking respectively) and
        # are scheduled for removal on 2026-07-24.
        if (self.base_url and "deepseek.com" in self.base_url
                and self.model in ("deepseek-chat", "deepseek-reasoner")):
            warn_key = f"deepseek_legacy_{self.model}"
            if warn_key not in _warned_once:
                _warned_once.add(warn_key)
                logger.warning(
                    f"DeepSeek model '{self.model}' is a deprecated alias "
                    f"(retires 2026-07-24) and now routes to deepseek-v4-flash. "
                    f"Update configs to 'deepseek-v4-flash' or 'deepseek-v4-pro'."
                )

    def _call_with_retry(self, kwargs: dict):
        """Call OpenAI-compatible API with retry on transient errors."""
        import httpx
        import openai

        for attempt in range(self.max_api_retries):
            try:
                response = self._client.chat.completions.create(**kwargs)
            except (
                httpx.RemoteProtocolError,
                httpx.ReadError,
                httpx.ConnectError,
                httpx.ReadTimeout,
                openai.APIStatusError,
                openai.APIConnectionError,
            ) as e:
                if isinstance(e, openai.APIStatusError):
                    if e.status_code < 500 and e.status_code != 429:
                        raise
                if attempt == self.max_api_retries - 1:
                    raise
                delay = self.retry_base_delay * (2 ** attempt)
                logger.warning(
                    f"OpenAI API error (attempt {attempt + 1}/{self.max_api_retries}): {e}  "
                    f"Retrying in {delay:.0f}s..."
                )
                time.sleep(delay)
                continue

            if response is None or not getattr(response, "choices", None):
                if attempt == self.max_api_retries - 1:
                    raise RuntimeError(
                        f"OpenAI API returned empty response after {self.max_api_retries} attempts"
                    )
                delay = self.retry_base_delay * (2 ** attempt)
                logger.warning(
                    f"OpenAI API returned empty response (attempt {attempt + 1}/{self.max_api_retries}).  "
                    f"Retrying in {delay:.0f}s..."
                )
                time.sleep(delay)
                continue

            return response

        raise RuntimeError("Unreachable")

    def __call__(self, prompt: str) -> str:
        start = time.monotonic()

        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": prompt})

        kwargs: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": messages,
        }
        # Thinking control — two different mechanisms depending on provider:
        #
        # DeepSeek cloud API (api.deepseek.com):
        #   extra_body={"thinking": {"type": "enabled"/"disabled"}}
        #
        # Ollama OpenAI-compatible endpoint:
        #   extra_body={"reasoning_effort": "none"/"low"/"medium"/"high"}
        #
        is_deepseek_cloud = self.base_url and "deepseek.com" in self.base_url
        if is_deepseek_cloud:
            if self.thinking:
                kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
            else:
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        else:
            # Ollama / other OpenAI-compatible endpoints
            if isinstance(self.thinking, str):
                effort = self.thinking
            elif self.thinking:
                effort = "high"
            else:
                effort = "none"
            kwargs["extra_body"] = {"reasoning_effort": effort}

        # Cap max_tokens to provider limits to avoid 400 errors.
        # DeepSeek V4 (deepseek-v4-flash, deepseek-v4-pro): max output 384K
        # for both thinking and non-thinking modes.
        # Ollama / other local: no known hard cap, skip capping.
        if is_deepseek_cloud and kwargs.get("max_tokens"):
            cap = 384_000
            if kwargs["max_tokens"] > cap:
                warn_key = f"deepseek_cap_{cap}"
                if warn_key not in _warned_once:
                    _warned_once.add(warn_key)
                    logger.warning(
                        f"max_tokens={kwargs['max_tokens']} exceeds DeepSeek V4 "
                        f"output limit of {cap}; capping to {cap} "
                        f"(Prompt: ...{prompt[-50:]})"
                    )
                kwargs["max_tokens"] = cap

        response = self._call_with_retry(kwargs)
        msg = response.choices[0].message
        text = msg.content or ""

        # Extract reasoning/thinking from the response.
        # Providers put it in different places:
        #   Ollama OpenAI-compat: msg.model_extra["reasoning"] (Pydantic extra field)
        #   DeepSeek direct API:  msg.reasoning_content
        #   OpenAI o-series:      not exposed
        # Fallback: parse <think>...</think> tags out of content (older Ollama).
        extras = getattr(msg, "model_extra", None) or {}
        reasoning = (
            extras.get("reasoning")
            or extras.get("reasoning_content")
            or getattr(msg, "reasoning_content", None)
            or ""
        )
        # Older Ollama embeds <think> tags directly in content
        if not reasoning and "<think>" in text:
            m = re.search(r"<think>(.*?)</think>", text, re.DOTALL)
            if m:
                reasoning = m.group(1).strip()
                text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()
        usage = getattr(response, "usage", None)
        if usage:
            logger.debug(f"OpenAI [{self.model}] tokens: "
                          f"in={usage.prompt_tokens} out={usage.completion_tokens}")
            self.usage_log.append(LLMCallRecord(
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
                wall_time_seconds=time.monotonic() - start,
            ))
        else:
            logger.debug(f"OpenAI [{self.model}] response had no usage info")
        # print(LLMResponse(text, reasoning).full_text)
        # breakpoint()  # DON'T EVER DELETE ME!
        return LLMResponse(text, reasoning)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_PROVIDERS = {
    "claude": AnthropicClient,
    "anthropic": AnthropicClient,
    "openai": OpenAIClient,
    "gpt": OpenAIClient,
}

# Presets for OpenAI-compatible providers (local and remote).
# Each maps to OpenAIClient with pre-filled defaults.
_PROVIDER_PRESETS: dict[str, dict] = {
    "deepseek": {
        "base_url": "http://localhost:11434/v1",
        "api_key_env": "OLLAMA_API_KEY",
        "default_model": "deepseek-r1",
    },
    "deepseek-cloud": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-v4-flash",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "api_key_env": "OLLAMA_API_KEY",
        "default_model": "llama3",
    },
    "vllm": {
        "base_url": "http://localhost:8000/v1",
        "api_key_env": "VLLM_API_KEY",
        "default_model": "default",
    },
}


def create_client(
    provider: str,
    model: str | None = None,
    thinking: bool | str = False,
    **kwargs,
) -> AnthropicClient | OpenAIClient:
    """
    Factory for LLM clients.

    Args:
        provider: "claude"/"anthropic", "openai"/"gpt", or any preset name
                  (e.g. "deepseek", "ollama", "vllm"). Any unrecognised
                  provider name with a base_url kwarg is treated as an
                  OpenAI-compatible endpoint.
        model: Override the default model name.
        thinking: Control thinking / reasoning.
                  False → disable thinking (default; overrides model defaults
                          like R1's built-in chain-of-thought).
                  True  → enable thinking (Anthropic adaptive / Ollama think=true).
                  str   → effort level (Anthropic: "low"/"medium"/"high"/"max";
                          Ollama gpt-oss: "low"/"medium"/"high").
        **kwargs: Passed to the client constructor (temperature, max_tokens,
                  system_prompt, api_key, base_url, api_key_env, tools_config).
    """
    provider = provider.lower().strip()

    # Check built-in providers first
    cls = _PROVIDERS.get(provider)
    if cls is not None:
        if model is not None:
            kwargs["model"] = model
        kwargs["thinking"] = thinking
        # Strip OpenAI-specific kwargs that AnthropicClient doesn't accept
        if cls is AnthropicClient:
            kwargs.pop("base_url", None)
            kwargs.pop("api_key_env", None)
            kwargs.pop("timeout", None)
        return cls(**kwargs)

    # Check presets (all map to OpenAIClient)
    preset = _PROVIDER_PRESETS.get(provider)
    if preset is not None:
        kwargs.setdefault("base_url", preset["base_url"])
        kwargs.setdefault("api_key_env", preset["api_key_env"])
        if model is not None:
            kwargs["model"] = model
        elif "model" not in kwargs:
            kwargs["model"] = preset["default_model"]
        kwargs["thinking"] = thinking
        return OpenAIClient(**kwargs)

    # Unknown provider — if base_url is provided, treat as OpenAI-compatible
    if kwargs.get("base_url"):
        if model is not None:
            kwargs["model"] = model
        kwargs["thinking"] = thinking
        return OpenAIClient(**kwargs)

    raise ValueError(
        f"Unknown provider {provider!r}. Built-in: {list(_PROVIDERS.keys())}. "
        f"Presets: {list(_PROVIDER_PRESETS.keys())}. "
        f"Or pass base_url for a custom OpenAI-compatible endpoint."
    )
