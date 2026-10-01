'use strict';

// The decisions the shell makes, kept apart from Electron so that they are tested on their own
// (test/policy.test.js): which addresses stay in the window, what a line from the launcher means,
// where a window may reopen, and when the Linux sandbox cannot work.

const path = require('node:path');
const { fileURLToPath } = require('node:url');

const loopback = new Set(['127.0.0.1', 'localhost', '[::1]']);

// isInternal: the launcher's pages and the app, which are on loopback and nowhere else.
function isInternal(raw) {
  try {
    const url = new URL(raw);
    return (url.protocol === 'http:' || url.protocol === 'https:') && loopback.has(url.hostname);
  } catch {
    return false;
  }
}

// isOwnFile: the shell's own pages (starting, message), and nothing else on the disk.
function isOwnFile(raw, dir) {
  try {
    const url = new URL(raw);
    if (url.protocol !== 'file:') return false;
    const file = path.resolve(fileURLToPath(url));
    const pages = path.resolve(dir, 'pages') + path.sep;
    return file.startsWith(pages);
  } catch {
    return false;
  }
}

// isExternal: what may be handed to the system — a web page or a mail address. A file:, a
// javascript: or a custom scheme a page tries to open is not followed anywhere.
function isExternal(raw) {
  try {
    const url = new URL(raw);
    return url.protocol === 'https:' || url.protocol === 'http:' || url.protocol === 'mailto:';
  } catch {
    return false;
  }
}

function linkFrom(argv) {
  return (argv || []).find((arg) => typeof arg === 'string' && arg.toLowerCase().startsWith('daedalus://')) || null;
}

// parseEvent reads one line of the launcher's protocol. Anything that is not an event — a line a
// library printed to the wrong stream — is ignored, not shown.
function parseEvent(line) {
  try {
    const event = JSON.parse(line);
    return event && typeof event === 'object' && typeof event.event === 'string' ? event : null;
  } catch {
    return null;
  }
}

const defaultBounds = { width: 1180, height: 820 };

// restoreBounds puts the window back where it was, when that is still on a screen: a window saved on
// a monitor that has since been unplugged would open off every screen there is.
function restoreBounds(saved, workAreas) {
  const bounds = saved && saved.bounds;
  if (!bounds || ![bounds.x, bounds.y, bounds.width, bounds.height].every(Number.isFinite)) {
    return { bounds: { ...defaultBounds }, maximized: false };
  }
  const width = Math.max(480, Math.min(bounds.width, 8000));
  const height = Math.max(400, Math.min(bounds.height, 8000));
  const visible = (workAreas || []).some((area) => {
    const overlapX = Math.min(bounds.x + width, area.x + area.width) - Math.max(bounds.x, area.x);
    const overlapY = Math.min(bounds.y + height, area.y + area.height) - Math.max(bounds.y, area.y);
    return overlapX >= 120 && overlapY >= 80;
  });
  if (!visible) return { bounds: { width, height }, maximized: Boolean(saved.maximized) };
  return { bounds: { x: bounds.x, y: bounds.y, width, height }, maximized: Boolean(saved.maximized) };
}

// sandboxUnavailable says that Chromium's sandbox cannot start on this Linux machine: unprivileged
// user namespaces are forbidden (Ubuntu 24.04 does it through AppArmor) and the setuid helper it
// would fall back to is not there — an AppImage cannot carry one, and a folder unpacked by hand has
// it without the setuid bit. The .deb keeps the sandbox: it installs an AppArmor profile that allows
// the namespaces where they are restricted, and the setuid helper where there are none.
function sandboxUnavailable(env, readFirstLine, helperIsSetuid, execPath) {
  // The .deb's own AppArmor profile gives the application under /opt the namespaces it needs.
  if (!env.APPIMAGE && String(execPath || '').startsWith('/opt/')) return false;
  const restricted =
    readFirstLine('/proc/sys/kernel/apparmor_restrict_unprivileged_userns') === '1' ||
    readFirstLine('/proc/sys/kernel/unprivileged_userns_clone') === '0' ||
    readFirstLine('/proc/sys/user/max_user_namespaces') === '0';
  if (!restricted) return false;
  if (env.APPIMAGE) return true;
  return !(helperIsSetuid && helperIsSetuid());
}

const dictionary = {
  en: {
    starting: 'Starting Daedalus…',
    fatalTitle: 'Daedalus could not start',
    engineMissing: 'The part of Daedalus that runs it is missing from the installation; install Daedalus again.',
    engineExited: 'The part of Daedalus that runs it stopped unexpectedly. Its log says why.',
    openLog: 'Show the log',
    retry: 'Try again',
    upgrading: 'Installing the new version of Daedalus. It opens again by itself when it is done — usually within a few minutes.',
    view: 'View',
  },
  ru: {
    starting: 'Daedalus запускается…',
    fatalTitle: 'Daedalus не смог запуститься',
    engineMissing: 'В установке нет части Daedalus, которая его запускает; установите Daedalus заново.',
    engineExited: 'Часть Daedalus, которая его запускает, неожиданно остановилась. Причина — в её журнале.',
    openLog: 'Показать журнал',
    retry: 'Попробовать снова',
    upgrading: 'Ставится новая версия Daedalus. Когда всё будет готово, он откроется сам — обычно в течение нескольких минут.',
    view: 'Вид',
  },
};

function strings(locale) {
  return String(locale || '').toLowerCase().startsWith('ru') ? dictionary.ru : dictionary.en;
}

module.exports = { isInternal, isOwnFile, isExternal, linkFrom, parseEvent, restoreBounds, sandboxUnavailable, strings, dictionary };
