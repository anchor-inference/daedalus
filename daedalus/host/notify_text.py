"""The words the notification router writes itself, in the operator's language.

A title the router composes ("Naya is waiting for permission") is rendered here, once, on the host,
so the app, a push, the desktop and Telegram all show the same sentence. The alternative — a message
key rendered by every client — would need the same twenty strings in the app's dictionary, in the
service worker (which cannot import it) and in the launcher's. Text a producer or an agent wrote
stays as written; only the router's own sentences come from this table.
"""

from __future__ import annotations

from typing import Any

DEFAULT_LANGUAGE = "en"

TEXT: dict[str, dict[str, str]] = {
    "en": {
        "run.finished": "Finished: {title}",
        "run.failed": "Failed: {title}",
        "run.failed.body": "The run ended with an error; see the session for details.",
        "ask": "{title} asks you",
        "permission": "{title} is waiting for permission",
        "staff.turn": "{name} finished a turn",
        "staff.failed": "{name} ran into an error",
        "staff.review": "Ready for review: {title}",
        "browser.needs_you": "{title} needs you in the browser",
        "browser.open": "Open the browser",
        "burst": "+{count} more",
        "test": "Test notification",
        "test.body": "If you can read this, notifications reach you here.",
        "allow": "Allow",
        "deny": "Deny",
        "open": "Open",
        "telegram.prefix": "🔔",
        "launcher.upgrade": "Daedalus {version} is available",
        "launcher.upgrade.current": "This installation's launcher is {version}. Nothing is installed by itself.",
        "launcher.upgrade.command": "Close the launcher and run in a terminal:\n{command}",
        "launcher.upgrade.promise": "It asks first, keeps the data from before the upgrade — on Linux with ext4 as a whole copy of the data folder, elsewhere as a verified backup — and puts the data and the launcher back if the new version does not come up.",
        "launcher.upgrade.notes": "Release notes: {url}",
    },
    "ru": {
        "run.finished": "Готово: {title}",
        "run.failed": "Ошибка: {title}",
        "run.failed.body": "Запуск завершился ошибкой; подробности в сессии.",
        "ask": "{title}: вопрос к вам",
        "permission": "{title} ждёт разрешения",
        "staff.turn": "{name}: ход завершён",
        "staff.failed": "{name}: ошибка",
        "staff.review": "На проверку: {title}",
        "browser.needs_you": "{title}: нужна ваша помощь в браузере",
        "browser.open": "Открыть браузер",
        "burst": "и ещё {count}",
        "test": "Проверка уведомлений",
        "test.body": "Если вы это читаете, уведомления сюда доходят.",
        "allow": "Разрешить",
        "deny": "Отклонить",
        "open": "Открыть",
        "telegram.prefix": "🔔",
        "launcher.upgrade": "Вышел Daedalus {version}",
        "launcher.upgrade.current": "Лаунчер этой установки — {version}. Само ничего не устанавливается.",
        "launcher.upgrade.command": "Закройте лаунчер и выполните в терминале:\n{command}",
        "launcher.upgrade.promise": "Сначала будет вопрос; данные до обновления сохраняются — на Linux с ext4 целой копией папки данных, в остальных случаях проверенной резервной копией, — а если новая версия не поднимется, данные и лаунчер вернутся как были.",
        "launcher.upgrade.notes": "Что нового: {url}",
    },
}


def language(lang: str | None) -> str:
    """The table's language for a reported one (``ru-RU`` reads as ``ru``); English when unknown."""
    code = (lang or "").split("-")[0].split("_")[0].lower()
    return code if code in TEXT else DEFAULT_LANGUAGE


def render(key: str, lang: str | None = None, **params: Any) -> str:
    """The sentence for ``key``. A key the language lacks falls back to English, never to the key."""
    template = TEXT[language(lang)].get(key) or TEXT[DEFAULT_LANGUAGE][key]
    return template.format(**params)


__all__ = ["DEFAULT_LANGUAGE", "TEXT", "language", "render"]
