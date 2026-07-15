from __future__ import annotations

import re
from typing import Any

COMPILER_DIAGNOSTIC_PATTERN = re.compile(
    r"^(?P<file>.*?):(?P<line>\d+):(?:(?P<column>\d+):)?\s*"
    r"(?P<severity>fatal error|error|warning|note):\s*(?P<message>.*)$"
)


def parse_compiler_diagnostics(stderr: str, *, limit: int = 24) -> list[dict[str, Any]]:
    """Extract compiler diagnostics without depending on particular error messages."""
    diagnostics: list[dict[str, Any]] = []
    for raw_line in stderr.splitlines():
        match = COMPILER_DIAGNOSTIC_PATTERN.match(raw_line.strip())
        if match is None:
            continue
        diagnostic: dict[str, Any] = {
            "file": match.group("file"),
            "line": int(match.group("line")),
            "severity": match.group("severity"),
            "message": match.group("message").strip(),
        }
        if match.group("column") is not None:
            diagnostic["column"] = int(match.group("column"))
        diagnostics.append(diagnostic)
        if len(diagnostics) >= limit:
            break
    return diagnostics
