"""TR-5.1 — Tool registration audit.

Validates registered MCP tool names against a deny-list of dangerous patterns
at server startup. If violations are found, the server refuses to start.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Reason: These patterns represent dangerous tool capabilities that should
# never be exposed through the MCP server. Any tool name containing these
# substrings (case-insensitive) is blocked.
DISALLOWED_TOOL_PATTERNS = [
    "read_any_file",
    "write_any_file",
    "execute_shell",
    "arbitrary_sql",
    "list_directory",
    "run_command",
    "file_system",
    "shell_exec",
]


def audit_registered_tools(tools: list[str]) -> list[str]:
    """Check registered tool names against the deny-list.

    Args:
        tools: List of registered tool names.

    Returns:
        List of violation messages (empty if all tools are allowed).
    """
    violations = []
    for tool_name in tools:
        lower_name = tool_name.lower()
        for pattern in DISALLOWED_TOOL_PATTERNS:
            if pattern in lower_name:
                violations.append(
                    f"DENIED: tool '{tool_name}' matches disallowed pattern '{pattern}'"
                )
    return violations
