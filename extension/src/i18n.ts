// Every word the operator reads in the extension, in Russian and English. Chrome's own `_locales`
// mechanism is used only for the manifest's name and description, which Chrome reads before any
// script runs; everything else comes from here so that one test can prove both dictionaries are
// complete and that no screen carries a sentence of its own.

const en = {
  "brand": "Daedalus",
  "phase.idle": "Not connected",
  "phase.connecting": "Connecting…",
  "phase.connected": "Connected to Daedalus",
  "phase.error": "Connection problem",
  "transport.native": "Daedalus on this computer",
  "transport.server": "Server {server}",
  "transport.off": "No installation chosen",
  "row.transport": "Connection",
  "row.install": "Installation",
  "row.version": "Daedalus version",
  "row.heartbeat": "Last heartbeat",
  "row.tabs": "Agent tabs",
  "tabs.count": "{tabs} in {groups} groups",
  "ago.now": "just now",
  "ago.seconds": "{n} s ago",
  "ago.minutes": "{n} min ago",
  "ago.never": "never",
  "error.not_running": "Daedalus is not running on this computer.",
  "error.not_registered": "No Daedalus app on this computer answers (its connector is not registered: Daedalus → Settings → Environments → Your Chrome). Daedalus on a server? Pair this browser below.",
  "error.pairing_expired": "The pairing has expired or was revoked. Pair this browser again.",
  "error.version_extension": "The versions differ: update the extension.",
  "error.version_daemon": "The versions differ: update Daedalus.",
  "error.busy": "Another Chrome is already connected: {name}.",
  "error.lost": "The connection to Daedalus was lost. Reconnecting…",
  "error.server_unreachable": "The server cannot be reached.",
  "error.replaced": "Another connection from this browser took over.",
  "error.shutdown": "Daedalus is shutting down.",
  "error.unavailable": "This Daedalus does not take a Chrome connection yet. Update Daedalus.",
  "error.bad_server": "Enter the server's address, for example daedalus.example.org.",
  "error.pair_failed": "The code was not accepted. Ask Daedalus for a new one.",
  "check.button": "Check connection",
  "check.waiting": "Checking…",
  "check.ok": "Daedalus confirmed ✓ ({ms} ms)",
  "check.timeout": "Daedalus did not answer within 5 seconds.",
  "check.offline": "Not connected, nothing to check.",
  "notice.checked": "Daedalus checked the connection",
  "groups.title": "The agent's tabs",
  "groups.empty": "The agent has no tabs open.",
  "group.untitled": "Agent",
  "group.tabs": "Tabs: {n}",
  "group.paused": "Paused: the agent waits for you",
  "group.pause": "Pause",
  "group.resume": "Give back",
  "group.stop": "Stop and close",
  "group.prefix": "Daedalus · {title}",
  "settings.title": "Connection",
  "settings.native": "This computer",
  "settings.server": "Server",
  "settings.address": "Server address",
  "settings.code": "One-time code",
  "settings.pair": "Pair",
  "settings.disconnect": "Disconnect",
  "settings.connect_native": "Connect to this computer",
  "settings.name": "Name of this browser",
  "settings.save": "Save",
  "settings.window_mode": "Let the agent work in a tab of my current window",
  "settings.window_hint": "Screenshots need the tab visible, so the agent's own window is the default.",
  "menu.grant": "Let Daedalus use this tab",
  "menu.grant_none": "Let Daedalus use this tab (no agent is working)",
  "menu.grant_to": "Give to: {title}",
  "badge.title": "Daedalus: {state}",
};

export type Key = keyof typeof en;

const ru: Record<Key, string> = {
  "brand": "Daedalus",
  "phase.idle": "Не подключено",
  "phase.connecting": "Подключение…",
  "phase.connected": "Подключено к Daedalus",
  "phase.error": "Проблема с подключением",
  "transport.native": "Daedalus на этом компьютере",
  "transport.server": "Сервер {server}",
  "transport.off": "Установка не выбрана",
  "row.transport": "Подключение",
  "row.install": "Установка",
  "row.version": "Версия Daedalus",
  "row.heartbeat": "Последний сигнал",
  "row.tabs": "Вкладки агента",
  "tabs.count": "{tabs} в группах: {groups}",
  "ago.now": "только что",
  "ago.seconds": "{n} с назад",
  "ago.minutes": "{n} мин назад",
  "ago.never": "ни разу",
  "error.not_running": "Daedalus не запущен на этом компьютере.",
  "error.not_registered": "Приложение Daedalus на этом компьютере не отвечает (коннектор не зарегистрирован: Daedalus → Настройки → Окружения → Ваш Chrome). Daedalus на сервере? Привяжите браузер ниже.",
  "error.pairing_expired": "Привязка истекла или отозвана. Привяжите браузер заново.",
  "error.version_extension": "Версии различаются: обновите расширение.",
  "error.version_daemon": "Версии различаются: обновите Daedalus.",
  "error.busy": "Уже подключён другой Chrome: {name}.",
  "error.lost": "Связь с Daedalus потеряна. Переподключение…",
  "error.server_unreachable": "Сервер недоступен.",
  "error.replaced": "Подключение перехватило другое соединение из этого браузера.",
  "error.shutdown": "Daedalus завершает работу.",
  "error.unavailable": "Этот Daedalus пока не принимает подключение Chrome. Обновите Daedalus.",
  "error.bad_server": "Введите адрес сервера, например daedalus.example.org.",
  "error.pair_failed": "Код не принят. Получите в Daedalus новый.",
  "check.button": "Проверить подключение",
  "check.waiting": "Проверяем…",
  "check.ok": "Daedalus подтвердил ✓ ({ms} мс)",
  "check.timeout": "Daedalus не ответил за 5 секунд.",
  "check.offline": "Нет подключения, проверять нечего.",
  "notice.checked": "Daedalus проверил подключение",
  "groups.title": "Вкладки агента",
  "groups.empty": "У агента нет открытых вкладок.",
  "group.untitled": "Агент",
  "group.tabs": "Вкладок: {n}",
  "group.paused": "Пауза: агент ждёт вас",
  "group.pause": "Пауза",
  "group.resume": "Вернуть агенту",
  "group.stop": "Остановить и закрыть",
  "group.prefix": "Daedalus · {title}",
  "settings.title": "Подключение",
  "settings.native": "Этот компьютер",
  "settings.server": "Сервер",
  "settings.address": "Адрес сервера",
  "settings.code": "Одноразовый код",
  "settings.pair": "Привязать",
  "settings.disconnect": "Отключить",
  "settings.connect_native": "Подключить к этому компьютеру",
  "settings.name": "Имя этого браузера",
  "settings.save": "Сохранить",
  "settings.window_mode": "Пусть агент работает во вкладке моего текущего окна",
  "settings.window_hint": "Для снимков экрана вкладка должна быть видна, поэтому по умолчанию у агента своё окно.",
  "menu.grant": "Разрешить Daedalus эту вкладку",
  "menu.grant_none": "Разрешить Daedalus эту вкладку (агент не работает)",
  "menu.grant_to": "Отдать: {title}",
  "badge.title": "Daedalus: {state}",
};

export const DICTIONARIES: Record<"en" | "ru", Record<Key, string>> = { en, ru };

/** The dictionary for Chrome's interface language: Russian for any `ru` locale, English otherwise. */
export function language(): "en" | "ru" {
  let ui = "en";
  try {
    ui = chrome.i18n.getUILanguage();
  } catch {
    // Outside an extension page (a unit test without the fake) there is no `chrome.i18n`.
  }
  return ui.toLowerCase().startsWith("ru") ? "ru" : "en";
}

export function t(key: Key, vars: Record<string, string | number> = {}): string {
  const text = DICTIONARIES[language()][key];
  return text.replace(/\{(\w+)\}/g, (whole, name: string) => (name in vars ? String(vars[name]) : whole));
}

export function isKey(key: string): key is Key {
  return key in en;
}
