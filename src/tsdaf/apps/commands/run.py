"""Fixed ``tsdaf run`` commands."""

from __future__ import annotations

import sys
from pathlib import Path

from tsdaf.apps.composition import _execution_id_failure, inspect_execution
from tsdaf.contracts.schema import canonical_json_bytes

__all__ = ("run_inspect",)


def run_inspect(execution_id: str) -> int:
    """Verify and inspect one local execution."""

    input_failure = _execution_id_failure(execution_id)
    if input_failure is not None:
        sys.stderr.write(canonical_json_bytes(input_failure).decode("utf-8") + "\n")
        return 1
    code, payload = inspect_execution(
        workspace=Path.cwd() / ".tsdaf-workspace",
        execution_id=execution_id,
    )
    stream = sys.stdout if code == 0 else sys.stderr
    stream.write(canonical_json_bytes(payload).decode("utf-8") + "\n")
    return code
