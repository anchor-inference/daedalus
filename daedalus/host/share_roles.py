"""The model roles a live link can set and narrow: what the share dialog offers the operator.

A role is one place a session's work reaches a model: the conversation itself, its subagents, the reading
of pictures, translation and so on. For each, the dialog shows the value the session would use now and the
presets, accounts and efforts the operator may put in its place or leave open to the visitor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from daedalus.app import Application


async def describe(app: Application, session_id: str) -> list[dict[str, Any]]:
    """Every role of the session, in the order the dialog lists them.

    Each is ``{"id", "label", "pickable", "current": {"preset", "effort", "account"}, "presets": [{"id",
    "label", "provider", "provider_name", "model", "billing", "efforts": [...], "accounts": [{"id",
    "name"}]}]}``. ``pickable`` says the visitor's own screen has a control for the role, so leaving it
    open means something; ``billing`` is ``subscription`` or ``metered``, which decides whether the money
    limit or the weekly share applies to it."""
    return []


__all__ = ["describe"]
