"""
Sandboxed Python code execution for LLM tool use.

Runs code in a subprocess with a timeout and captures stdout/stderr.
"""

from __future__ import annotations

import sys
import subprocess
import tempfile
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 4000


@dataclass
class ExecutionResult:
    stdout: str
    stderr: str
    return_code: int
    timed_out: bool


def execute_python(code: str, timeout: int = 30) -> ExecutionResult:
    """Run Python code in a subprocess with a timeout.

    Returns an ExecutionResult with stdout, stderr, return code, and
    whether the process timed out.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False
    ) as tmp:
        tmp.write(code)
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return ExecutionResult(
            stdout=result.stdout[:MAX_OUTPUT_CHARS],
            stderr=result.stderr[:MAX_OUTPUT_CHARS],
            return_code=result.returncode,
            timed_out=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"Code execution timed out after {timeout}s")
        return ExecutionResult(
            stdout="",
            stderr=f"Execution timed out after {timeout} seconds.",
            return_code=-1,
            timed_out=True,
        )
    except Exception as e:
        logger.error(f"Code execution error: {e}")
        return ExecutionResult(
            stdout="",
            stderr=str(e),
            return_code=-1,
            timed_out=False,
        )
    finally:
        import os
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
