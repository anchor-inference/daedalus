"""Which of a desktop action's consequences the operator must approve, judged from the window it lands in.

The word lists are the browser daemon's (``browserd/internal/sensitive/sensitive.go``), English and
Russian, ported once: a purchase, a message sent, something deleted, terms accepted. The browser reads
them off the element a click lands on; the desktop has no element yet (``element.at`` is not served),
so they are read off the title of the window under the point and the name of its app. That is coarser
— every click in a window titled "Delete account" asks — and deliberately so: as the browser's lists
say, a false alarm costs the operator a tap and a purchase nobody asked about costs money.

Keys are judged by what they do where they land: Enter (and Ctrl/Cmd+Enter) sends in a mail or
messaging app, Shift+Delete and Cmd+Backspace delete for good in those and in a file manager, and
typed text with a line break presses Enter.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

PURCHASE = "purchase"
SEND = "send"
DESTROY = "destroy"
ACCEPT = "accept"

WORDS: dict[str, tuple[str, ...]] = {
    PURCHASE: (
        "buy", "buy now", "pay", "pay now", "place order", "checkout", "check out", "purchase",
        "donate", "book now", "complete order", "confirm order", "confirm purchase", "add payment", "payment", "billing",
        "купить", "оплатить", "оплата", "оформить заказ", "заказать", "забронировать", "пожертвовать",
        "подтвердить заказ", "подтвердить оплату",
    ),
    SEND: (
        "send", "post", "publish", "share", "reply", "submit", "tweet", "comment", "retweet", "repost",
        "отправить", "опубликовать", "поделиться", "ответить", "комментировать", "запостить",
    ),
    DESTROY: (
        "delete", "remove", "cancel subscription", "unsubscribe", "close account", "delete account", "revoke",
        "erase", "discard", "deactivate", "terminate",
        "удалить", "отменить подписку", "отписаться", "закрыть счёт", "закрыть счет", "удалить аккаунт", "отозвать",
        "стереть", "деактивировать",
    ),
    ACCEPT: (
        "accept", "agree", "i agree", "accept all", "allow all", "consent", "i accept",
        "принять", "согласен", "согласна", "соглашаюсь", "принять все", "разрешить все",
    ),
}

REFUSALS = (
    "reject", "decline", "deny", "refuse", "only necessary", "necessary only", "essential only",
    "only essential", "only required", "required only", "strictly necessary", "necessary cookies", "essential cookies",
    "without accepting", "don't accept", "do not accept",
    "отклонить", "отказаться", "только необходимые", "только обязательные", "только технические", "без принятия",
    "без согласия", "не принимать", "не согласен",
)
"""Answers to a consent prompt that agree to nothing, though they hold an accepting word."""

SUBSCRIPTIONS = ("subscribe", "subscribe now", "start subscription", "start trial", "подписаться", "оформить подписку")
"""A purchase only beside a price: a newsletter's "Subscribe" costs nothing."""

PRICE = re.compile(r"([$€£¥₽]\s*\d|\d\s*([$€£¥₽]|usd|eur|gbp|руб|р\.)|per month|per year|/\s*(mo|month|yr|year)\b|в месяц|в год|/\s*мес)", re.IGNORECASE)


def _cyrillic(word: str) -> bool:
    return any("а" <= ch <= "я" or ch == "ё" for ch in word.lower())


def _phrase(words: str) -> str:
    parts = words.split()
    out = []
    for i, part in enumerate(parts):
        quoted = re.escape(part)
        # The last Russian word of a phrase matches with any ending (купить, купите, оплатите): its
        # stem carries the meaning, so its last two letters are dropped and any letters may follow.
        if _cyrillic(part) and i == len(parts) - 1 and len(part) >= 5:
            quoted = re.escape(part[:-2]) + r"[^\W\d_]*"
        out.append(quoted)
    return r"\s+".join(out)


def _compile(words: Iterable[str]) -> re.Pattern[str]:
    # A word edge is anything but a letter or a digit, spelt out because the edge must hold for
    # Cyrillic as well; the underscore counts as an edge, as in the daemon's pattern.
    return re.compile(r"(?:^|[\W_])(" + "|".join(_phrase(w) for w in words) + r")(?:$|[\W_])", re.IGNORECASE)


_PATTERNS = {kind: _compile(words) for kind, words in WORDS.items()}
_REFUSAL = _compile(REFUSALS)
_SUBSCRIPTION = _compile(SUBSCRIPTIONS)


def label_kinds(label: str) -> list[str]:
    """The kinds the words of ``label`` name, sorted: the daemon's rules for a clicked element's name."""
    text = (label or "").lower()
    kinds: set[str] = set()
    for kind, pattern in _PATTERNS.items():
        found = pattern.search(text)
        if found is None and kind == PURCHASE and PRICE.search(text):
            found = _SUBSCRIPTION.search(text)
        if found is None:
            continue
        if kind == ACCEPT and _REFUSAL.search(text):
            continue
        kinds.add(kind)
    return sorted(kinds)


MAIL_APPS = ("mail", "thunderbird", "outlook", "evolution", "geary", "mailspring", "spark", "airmail", "kmail", "betterbird", "почта")
MESSAGING_APPS = (
    "telegram", "slack", "discord", "whatsapp", "signal", "teams", "element", "skype", "messages", "messenger",
    "viber", "wechat", "zoom", "mattermost", "rocket.chat", "zulip", "imessage", "line", "threema", "wire",
)
FILE_MANAGERS = ("finder", "explorer", "nautilus", "dolphin", "thunar", "nemo", "pcmanfm", "caja", "files", "konqueror", "total commander", "doublecmd", "проводник")
"""Apps by a word of their name, bundle id, executable or window class: the places a key sends or
destroys rather than edits."""


def _app_words(app: dict[str, object]) -> str:
    # The executable by its base name only: a Windows path runs through "Program Files", which would
    # make every program a file manager.
    exe = re.split(r"[\\/]", str(app.get("exe_path") or ""))[-1]
    fields = (app.get("name"), app.get("bundle_id"), exe, app.get("wm_class"), app.get("id"))
    return " ".join(str(f) for f in fields if f).lower()


def _is(app: dict[str, object], names: tuple[str, ...]) -> bool:
    text = _app_words(app)
    return any(re.search(r"(?:^|[\W_])" + re.escape(name) + r"(?:$|[\W_])", text) for name in names)


def app_kind(app: dict[str, object]) -> str:
    """``mail``, ``messaging``, ``files`` or ``""`` for an app as deskd describes it."""
    if _is(app, MAIL_APPS):
        return "mail"
    if _is(app, MESSAGING_APPS):
        return "messaging"
    if _is(app, FILE_MANAGERS):
        return "files"
    return ""


_SENDING_CHORDS = {("enter",), ("ctrl", "enter"), ("super", "enter")}
_DESTROYING_CHORDS = {("shift", "delete"), ("super", "backspace"), ("super", "delete")}
_MODIFIERS = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt", "option": "alt", "shift": "shift", "super": "super", "cmd": "super", "meta": "super", "win": "super", "command": "super"}
_NAMED = {"return": "enter", "kp_enter": "enter", "enter": "enter", "delete": "delete", "del": "delete", "backspace": "backspace"}


def chords(keys: str) -> list[tuple[str, ...]]:
    """Each chord of a ``key`` action (``"ctrl+Return shift+Delete"``) as its modifiers then its key,
    in the daemon's names, with case, ``_`` and ``-`` ignored as the daemon ignores them."""
    out: list[tuple[str, ...]] = []
    for chord in (keys or "").split():
        parts = [p.lower().replace("_", "").replace("-", "") for p in chord.split("+") if p]
        if not parts:
            continue
        mods = sorted({_MODIFIERS[p] for p in parts[:-1] if p in _MODIFIERS})
        last = parts[-1]
        key = _NAMED.get(last, _MODIFIERS.get(last, last))
        out.append((*mods, key))
    return out


def key_kinds(keys: str, app: dict[str, object]) -> list[str]:
    """The kinds a ``key`` action has in ``app``: sending in mail and messaging, destroying there and in
    a file manager."""
    kind = app_kind(app)
    if not kind:
        return []
    found: set[str] = set()
    for chord in chords(keys):
        if kind in ("mail", "messaging") and chord in _SENDING_CHORDS:
            found.add(SEND)
        if chord in _DESTROYING_CHORDS:
            found.add(DESTROY)
    return sorted(found)


def typed_kinds(text: str, app: dict[str, object]) -> list[str]:
    """Typed text with a line break presses Enter, which sends in a mail or messaging app."""
    return [SEND] if ("\n" in text or "\r" in text) and app_kind(app) in ("mail", "messaging") else []


WORDS_FOR = {
    PURCHASE: "buy or pay",
    SEND: "send or publish something",
    DESTROY: "delete or cancel something",
    ACCEPT: "accept terms or consent",
}


__all__ = [
    "ACCEPT",
    "DESTROY",
    "FILE_MANAGERS",
    "MAIL_APPS",
    "MESSAGING_APPS",
    "PURCHASE",
    "SEND",
    "WORDS",
    "WORDS_FOR",
    "app_kind",
    "chords",
    "key_kinds",
    "label_kinds",
    "typed_kinds",
]
