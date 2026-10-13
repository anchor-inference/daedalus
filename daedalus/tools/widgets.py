"""``Widget`` — an interactive element inside the answer: a calculator, sliders, a small chart, a checklist.

A tool rather than a fenced block the renderer recognises. The tool's arguments are checked before
anything is drawn (a document too large, data that is not JSON, a title missing) and the refusal
reaches the model while it can still fix it; a fenced block would be found only by the reader, and a
broken one would sit in the answer as a wall of markup in Telegram and in a shared link, where the
call is folded away and only its title is said. The transcript keeps the document in the call's
arguments, which is where the app reads it from.
"""

from __future__ import annotations

from typing import Any

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.host.widgets import WidgetError, check_data, check_html, clean_title, network_notes
from daedalus.stores.files import human_size
from daedalus.tools import search_hint, tool_group
from daedalus.tools._common import call_id, error, ok


@tool_group("widgets")
@search_hint(
    "interactive widget in the answer calculator sliders compare chart checklist converter form inline html mini app "
    "интерактивный виджет в ответе калькулятор ползунки сравнение график чеклист конвертер форма"
)
@tool(
    name="Widget",
    description=(
        "Put an interactive widget into your answer: one small HTML document (CSS and JavaScript inline) that the app "
        "draws inside the conversation. Use it when interaction genuinely helps the operator — a calculator, a "
        "comparison with sliders, a small chart they can hover, a checklist they tick, a unit converter — never for "
        "text, a table or code, which the answer says better. The widget has NO network: no CDN, no fetch, no external "
        "fonts or images (data: URLs work), so write plain JavaScript and inline SVG or canvas. It is a sandboxed frame "
        "that fits its height to the content; keep it compact and phone-friendly (360 px wide). The app's theme is in CSS "
        "variables: --bg, --surface-1, --surface-2, --fg, --fg-2, --muted, --line, --accent, --accent-fg, --ok, --warn, "
        "--bad, --font, --mono, --radius-md; use them and the widget follows light and dark. In its scripts a global "
        "`widget` offers: widget.data (the JSON passed as data), widget.storage.get(key) / set(key, value) / delete(key) / "
        "keys() (promises; what is stored survives a reload), widget.ask(text) (puts a follow-up message "
        "into the operator's composer for them to send; it never sends by itself), widget.theme and widget.onTheme(fn). "
        "title: a few words shown above it and wherever it cannot be drawn (Telegram, a shared link). html: at most "
        "128 KB. data: any JSON up to 64 KB, for the numbers the widget shows. The operator can open it as a custom page."
    ),
)
async def widget(context: ToolContext, title: str, html: str, data: Any = None) -> ToolResult:
    try:
        name = clean_title(title)
        document = check_html(html)
        check_data(data)
    except WidgetError as exc:
        return error(context, str(exc))
    size = human_size(len(document.encode("utf-8")))
    lines = [
        f"The widget {name!r} ({size}) is in the conversation now, after this step. What the operator enters into it is "
        "kept with it (widget.storage); widget.ask hands you their follow-up only when they send it. Telegram and a shared "
        "link show its title with a link to the app instead.",
    ]
    wanted = network_notes(document)
    if wanted:
        lines.append(
            f"It has no network, and the document uses {', '.join(wanted)}: that part will not load. Inline the script or "
            "style, draw with SVG or canvas, and pass numbers as data; call Widget again with the fixed document."
        )
    lines.append("Do not repeat what the widget shows in your answer: say in a sentence what it is for.")
    return ok(context, "\n".join(lines), widget_id=call_id(context), title=name)


TOOLS = [widget]

__all__ = ["TOOLS"]
