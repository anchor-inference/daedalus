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
        "disk.quota": "{owner} uses {size} of disk",
        "disk.quota.body": "The workspace {name} has grown past {limit}{twice}. The largest folders:\n{largest}\n\nIts agent has been asked to delete its throwaway copies; the folder sizes and a clean-up are in the session's details.",
        "disk.quota.twice": ", twice its soft limit",
        "disk.quota.unowned": "No session owns it, so nobody was asked to clean up.",
        "disk.low": "The disk is nearly full: {free} free",
        "disk.low.body": "{free} of {total} is free on the volume that holds the workspaces (the floor is {floor}). The largest workspaces:\n{largest}",
        "disk.cleaned": "Old throwaway folders removed: {size} freed",
        "disk.cleaned.body": "{lines}",
        "launcher.upgrade": "Daedalus {version} is available",
        "launcher.upgrade.current": "This installation's launcher is {version}. Nothing is installed by itself.",
        "launcher.upgrade.command": "Close the launcher and run in a terminal:\n{command}",
        "launcher.upgrade.here": "Press the blue Update button at the bottom left, or install it from Settings → About: the app closes, keeps the data, installs the new version and opens again.",
        "launcher.upgrade.package": "This copy was installed from a package: download the new version's installer from the release page and install it the same way; the data stays where it is.",
        "launcher.upgrade.promise": "It asks first, keeps the data from before the upgrade — on Linux with ext4 as a whole copy of the data folder, elsewhere as a verified backup — and puts the data and the launcher back if the new version does not come up.",
        "launcher.upgrade.notes": "Release notes: {url}",
        "calendar.reminder.timed": "{when}",
        "calendar.reminder.timed.place": "{when} · {place}",
        "calendar.reminder.all_day": "{day}, all day",
        "calendar.reminder.lead": "in {minutes} min",
        "calendar.reminder.lead.now": "now",
        "task.reminder.due": "Due {when}",
        "task.reminder.block": "Planned for {when}",
        "setting.compaction.fallback": "The summary model failed; the session's model summarised instead",
        "setting.compaction.failed": "The history could not be summarised",
        "setting.vision.fallback": "The vision model is not set or failed; the session's model looked instead",
        "setting.vision.failed": "No model could look at the image",
        "setting.search.failed": "Web search failed on every backend",
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
        "disk.quota": "{owner} занимает {size} на диске",
        "disk.quota.body": "Рабочая папка {name} выросла больше {limit}{twice}. Самые большие папки:\n{largest}\n\nАгента попросили удалить свои временные копии; размеры папок и очистка — в сведениях сессии.",
        "disk.quota.twice": ", вдвое больше мягкого лимита",
        "disk.quota.unowned": "Она не принадлежит ни одной сессии, поэтому убрать её никого не попросили.",
        "disk.low": "Диск почти заполнен: свободно {free}",
        "disk.low.body": "На томе с рабочими папками свободно {free} из {total} (порог — {floor}). Самые большие рабочие папки:\n{largest}",
        "disk.cleaned": "Удалены старые временные папки: освобождено {size}",
        "disk.cleaned.body": "{lines}",
        "launcher.upgrade": "Вышел Daedalus {version}",
        "launcher.upgrade.current": "Лаунчер этой установки — {version}. Само ничего не устанавливается.",
        "launcher.upgrade.command": "Закройте лаунчер и выполните в терминале:\n{command}",
        "launcher.upgrade.here": "Нажмите синюю кнопку «Обновить» слева внизу или установите в «Настройки → О системе»: приложение закроется, сохранит данные, поставит новую версию и откроется снова.",
        "launcher.upgrade.package": "Эта копия установлена из пакета: скачайте установщик новой версии со страницы релиза и установите так же; данные останутся на месте.",
        "launcher.upgrade.promise": "Сначала будет вопрос; данные до обновления сохраняются — на Linux с ext4 целой копией папки данных, в остальных случаях проверенной резервной копией, — а если новая версия не поднимется, данные и лаунчер вернутся как были.",
        "launcher.upgrade.notes": "Что нового: {url}",
        "calendar.reminder.timed": "{when}",
        "calendar.reminder.timed.place": "{when} · {place}",
        "calendar.reminder.all_day": "{day}, весь день",
        "calendar.reminder.lead": "через {minutes} мин",
        "calendar.reminder.lead.now": "сейчас",
        "task.reminder.due": "Срок: {when}",
        "task.reminder.block": "Запланировано на {when}",
        "setting.compaction.fallback": "Модель выжимки не сработала; выжимку сделала модель сессии",
        "setting.compaction.failed": "Не удалось сжать историю",
        "setting.vision.fallback": "Модель зрения не задана или не сработала; картинку посмотрела модель сессии",
        "setting.vision.failed": "Ни одна модель не смогла посмотреть картинку",
        "setting.search.failed": "Веб-поиск не ответил ни через один бэкенд",
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
