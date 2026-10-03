"""Identify coordinator tool calls that can change project state.

An admitted mutation may await a CLI or network result. Replacement must refuse while it remains
in flight, rather than hold a project lock across that wait or let the former office write later.
"""

from __future__ import annotations

from typing import Any

READ_ONLY = {"peek", "read_staff", "staff_sessions", "harnesses", "plugin_read"}


def mutates(operation: str, arguments: dict[str, Any]) -> bool:
    if operation in READ_ONLY:
        return False
    if operation == "brief":
        return arguments.get("body") is not None
    if operation == "folders":
        return arguments.get("op", "list") != "list"
    if operation == "journal":
        return arguments.get("op", "write") != "read"
    if operation == "team":
        return arguments.get("concurrency") is not None
    if operation == "tasks":
        return arguments.get("op", "list") not in ("list", "get")
    if operation == "review_result":
        return arguments.get("op") != "inspect"
    return True
