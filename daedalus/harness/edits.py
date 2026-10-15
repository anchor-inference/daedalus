"""Which files a command-line agent's finished tool call wrote, read from the call's own input.

Each CLI names its file tools differently (Claude's ``Edit``, OpenCode's ``edit``, Grok's
``edit_file``), but all of them carry the target in an input key spelled one of a few ways. A tool
whose name says it writes and whose input names a file is counted; a shell command that happens to
write is not, because guessing paths out of a command line would put leases on files nobody touched.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

PATH_KEYS = ("file_path", "filePath", "path", "notebook_path", "target_file")
WRITING_WORDS = ("edit", "write", "patch", "replace", "create")


def edited_paths(tool: str, tool_input: Mapping[str, Any] | None) -> list[str]:
    """The files ``tool`` wrote according to its input; empty for a tool that does not write files."""
    name = tool.lower().rsplit("__", 1)[-1]
    if not tool_input or not any(word in name for word in WRITING_WORDS):
        return []
    for key in PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return [value.strip()]
    return []


__all__ = ["edited_paths"]
