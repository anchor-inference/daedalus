"""``SettingsGet`` and ``SettingsChange`` — the agent reads and changes Daedalus's own settings.

Host tools because the configuration is the host's: the state directory is sealed from sessions,
and a change must pass the same validation, revision and approval as a save from the settings screen.
The work is in :mod:`daedalus.extensions.agent_settings`; these only carry the call there.
"""

from __future__ import annotations

import json
from typing import Any

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.host.agent_settings import ChangeRefused, result_text
from daedalus.tools import search_hint, tool_group
from daedalus.tools._common import call_id, error, ok, services_for

READ_HINT = (
    "settings configuration config setting option value current what is set show read limits model language "
    "настройки настройка конфиг параметр значение текущие показать посмотреть какие лимиты модель язык"
)
CHANGE_HINT = (
    "change settings configuration set switch turn on off enable disable answer language compaction navigation rail "
    "изменить измени поменять поменяй поставь включи выключи настройки язык ответа сжатие навигация"
)


def _hook(context: ToolContext, name: str):  # type: ignore[no-untyped-def]
    manager = services_for(context).extra.get("manager")
    return manager.service_hooks.get(name) if manager is not None else None


@tool_group("settings")
@search_hint(READ_HINT)
@tool(
    name="SettingsGet",
    description=(
        "Read Daedalus's own settings. Without section: every section with a line on what it is and whether a change "
        "of it applies at once or after the operator's Apply. With section: its current values (credentials masked), "
        "each field's type, limits, default and meaning, the paths only the operator may change, and the revision."
    ),
)
async def settings_get(context: ToolContext, section: str = "") -> ToolResult:
    hook = _hook(context, "settings_get")
    if hook is None:
        return error(context, "settings are not available here")
    try:
        found = await hook(session_id=context.session_id, section=section.strip() or None)
    except ChangeRefused as exc:
        return error(context, str(exc))
    return ok(context, json.dumps(found, ensure_ascii=False, indent=1))


@tool_group("settings")
@search_hint(CHANGE_HINT)
@tool(
    name="SettingsChange",
    description=(
        "Change Daedalus's own settings. changes: an object of sections with only the fields to change, merged into "
        "the current settings ({\"compaction\": {\"auto_ratio\": 0.6}}; null resets a field to its default; a list is "
        "replaced whole). reason: one or two sentences for the operator. The whole result is validated first. A "
        "change only of sections the operator lets you apply is saved at once; any other waits for their Apply on a "
        "card in this chat and a notification, and the outcome reaches you as a message. The answer lists each "
        "change with when it takes effect (next_step, reconnect, restart). Credentials, policy, self-change and the "
        "other operator-only settings are refused."
    ),
)
async def settings_change(context: ToolContext, changes: dict[str, Any], reason: str) -> ToolResult:
    hook = _hook(context, "settings_change")
    if hook is None:
        return error(context, "settings are not available here")
    try:
        record = await hook(session_id=context.session_id, tool_call_id=call_id(context), changes=changes, reason=reason)
    except ChangeRefused as exc:
        return error(context, str(exc))
    return ok(context, result_text(record))


TOOLS = [settings_get, settings_change]

__all__ = ["TOOLS"]
