"""What Telegram says about memory: ``/memory``'s short view, ``/remember``'s answer, and the one-line
note under an answer whose turn saved something.

Kept apart from the front so the wording is tested without a bot. The full memory is the app's: the
short view shows the You pages and the counts, and links there for everything else.
"""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from typing import Any

YOU_BULLETS = 6
"""Bullets shown per You page; the rest are a tap away in the app."""
BULLET_CHARS = 160


def _clip(text: str, limit: int = BULLET_CHARS) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def memory_view(listing: Sequence[Mapping[str, Any]], you: Mapping[str, Sequence[str]], status: Mapping[str, Any], link: str) -> str:
    """``/memory``: the state, the You pages' first bullets, how many entries and facts there are, and
    where the rest is. HTML for the chat; every piece of memory text is escaped."""
    entries = len(listing)
    facts = sum(int(e.get("count") or 0) for e in listing)
    state = "⏸ paused — kept, not used and not learning" if status.get("paused") else ("on" if status.get("generate", True) else "on; not learning from chats")
    lines = [f"<b>🧠 Memory</b> · {state}", f"{entries} entr{'y' if entries == 1 else 'ies'} · {facts} fact{'' if facts == 1 else 's'}"]
    for entry in listing:
        if entry.get("section") != "you":
            continue
        bullets = list(you.get(str(entry.get("id")), ()))
        lines.append("")
        lines.append(f"<b>{html.escape(str(entry.get('title') or ''))}</b>")
        if not bullets:
            lines.append("<i>nothing yet</i>")
        lines.extend(f"• {html.escape(_clip(b))}" for b in bullets[:YOU_BULLETS])
        if len(bullets) > YOU_BULLETS:
            lines.append(f"<i>and {len(bullets) - YOU_BULLETS} more</i>")
    others = [e for e in listing if e.get("section") != "you"]
    if others:
        lines.append("")
        lines.append("Also: " + ", ".join(html.escape(str(e.get("title") or "")) for e in others[:12]) + ("…" if len(others) > 12 else ""))
    lines.append("")
    lines.append(f'<a href="{html.escape(link, quote=True)}">Open Memory in the app</a>' if link.startswith("http") else "The full memory: the app → Memory.")
    lines.append("/remember &lt;text&gt; adds a note.")
    return "\n".join(lines)


def remembered(decision: str, entry_title: str | None) -> str:
    where = f" · {entry_title}" if entry_title else ""
    if decision == "created":
        return f"🧠 Saved to memory{where}. Undo it from the app's Memory history."
    if decision == "merged":
        return f"🧠 Memory already had something like it; merged{where}."
    return f"🧠 Memory already holds that{where}."


def saved_footer(titles: Sequence[str]) -> str:
    """The note under an answer whose turn saved something; the agent never says so itself."""
    names = ", ".join(dict.fromkeys(t for t in titles if t))
    return f"🧠 Saved to memory{' · ' + names if names else ''}"


__all__ = ["memory_view", "remembered", "saved_footer"]
