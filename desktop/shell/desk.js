'use strict';

// The decisions behind Quick Chat, the appshot and the tray, kept apart from Electron so that they
// are tested on their own (test/desk.test.js): what the settings are and what makes them valid,
// which shortcuts got registered and why the others did not, what the tray says, which screen an
// appshot is of, and when closing the window only hides it.

const DEFAULTS = Object.freeze({
  // Alt+Space is Option+Space on a Mac: Electron reads Alt as Option there, so one accelerator is
  // the default on every system — the key ChatGPT's Quick Chat and Claude's quick entry taught.
  quickChat: 'Alt+Space',
  appshot: 'Alt+Shift+Space',
  // A region of the screen: S as in select, beside the screen's own appshot on the same modifiers.
  // Windows' snipping tool is Win+Shift+S, which this leaves alone.
  region: 'Alt+Shift+S',
  // Quick Chat with the microphone already listening: V as in voice.
  voice: 'Alt+Shift+V',
  hideToTray: true,
});

// The shortcuts, in the order they are registered, listed and checked against each other.
const SHORTCUTS = Object.freeze(['quickChat', 'appshot', 'region', 'voice']);

const MODIFIERS = new Set(['command', 'cmd', 'control', 'ctrl', 'commandorcontrol', 'cmdorctrl', 'alt', 'option', 'altgr', 'shift', 'super', 'meta']);
const NAMED_KEYS = new Set([
  'space', 'tab', 'backspace', 'delete', 'insert', 'return', 'enter', 'up', 'down', 'left', 'right', 'home', 'end',
  'pageup', 'pagedown', 'escape', 'esc', 'plus', 'printscreen',
  'num0', 'num1', 'num2', 'num3', 'num4', 'num5', 'num6', 'num7', 'num8', 'num9',
  'numdec', 'numadd', 'numsub', 'nummult', 'numdiv',
]);

// validAccelerator accepts what Electron's globalShortcut can take and refuses what would make the
// shell steal a plain key from every other program: a shortcut without a modifier is allowed only
// for the function keys. An empty string is valid too — it means the shortcut is off.
function validAccelerator(value) {
  if (value === '') return true;
  if (typeof value !== 'string' || value.length > 64) return false;
  const parts = value.split('+');
  // "Alt++" names the plus key the way people type it; Electron calls that key Plus.
  if (parts.some((part) => part === '')) return false;
  const key = parts[parts.length - 1].toLowerCase();
  const modifiers = parts.slice(0, -1).map((part) => part.toLowerCase());
  if (modifiers.some((part) => !MODIFIERS.has(part))) return false;
  if (new Set(modifiers).size !== modifiers.length) return false;
  if (MODIFIERS.has(key)) return false;
  const functionKey = /^f([1-9]|1[0-9]|2[0-4])$/.test(key);
  const ordinary = /^[a-z0-9]$/.test(key) || /^[`\-=[\]\\;',./]$/.test(key) || NAMED_KEYS.has(key);
  if (!functionKey && !ordinary) return false;
  return functionKey || modifiers.length > 0;
}

// normaliseSettings reads what was saved, keeping each value that is still valid and putting the
// default in for anything else: a settings file edited by hand, or written by an older shell, must
// never leave Quick Chat without its shortcut because one field was wrong. A default that lands on a
// key the operator already gave another action is left off rather than fought over.
function normaliseSettings(raw) {
  const saved = raw && typeof raw === 'object' ? raw : {};
  const settings = {};
  const chosen = new Set();
  for (const name of SHORTCUTS) {
    if (validAccelerator(saved[name])) {
      settings[name] = saved[name];
      if (saved[name]) chosen.add(saved[name].toLowerCase());
    }
  }
  for (const name of SHORTCUTS) {
    if (name in settings) continue;
    settings[name] = chosen.has(DEFAULTS[name].toLowerCase()) ? '' : DEFAULTS[name];
  }
  settings.hideToTray = typeof saved.hideToTray === 'boolean' ? saved.hideToTray : DEFAULTS.hideToTray;
  return settings;
}

// mergeSettings applies a change from the settings page. An invalid value is an error the page shows,
// not a silent default: the operator typed it and should hear why it was not taken.
function mergeSettings(current, patch) {
  const next = { ...current };
  const errors = {};
  for (const name of SHORTCUTS) {
    if (!(patch && name in patch)) continue;
    if (validAccelerator(patch[name])) next[name] = patch[name];
    else errors[name] = 'invalid';
  }
  if (patch && typeof patch.hideToTray === 'boolean') next.hideToTray = patch.hideToTray;
  // One key cannot do two jobs: the field that was just changed is the one refused, and keeps its value.
  for (const name of SHORTCUTS) {
    if (!next[name] || next[name] === current[name]) continue;
    const clash = SHORTCUTS.some((other) => other !== name && next[other] && next[other].toLowerCase() === next[name].toLowerCase());
    if (clash) {
      errors[name] = 'duplicate';
      next[name] = current[name];
    }
  }
  return { settings: next, errors };
}

// registerShortcuts puts the shortcuts in place, replacing the ones registered before, and says
// for each whether it works. A shortcut another program holds makes register() return false (some
// systems throw instead); either way it is reported, never fatal. `held` is the set of accelerators
// this shell registered last time, which are the only ones it releases: unregisterAll would also
// drop shortcuts some other part of the application may own one day.
function registerShortcuts(globalShortcut, settings, actions, held) {
  for (const accelerator of held) {
    try {
      globalShortcut.unregister(accelerator);
    } catch {}
  }
  held.clear();
  const report = {};
  for (const name of SHORTCUTS) {
    const accelerator = settings[name];
    if (!accelerator) {
      report[name] = { accelerator: '', ok: false, reason: 'off' };
      continue;
    }
    if (!validAccelerator(accelerator)) {
      report[name] = { accelerator, ok: false, reason: 'invalid' };
      continue;
    }
    let ok = false;
    try {
      ok = globalShortcut.register(accelerator, actions[name]) === true;
    } catch {
      ok = false;
    }
    if (ok) held.add(accelerator);
    report[name] = { accelerator, ok, reason: ok ? '' : 'taken' };
  }
  return report;
}

// waylandSession says the desktop is a Wayland one, where a program may not grab a key for the whole
// desktop: Electron asks the desktop's portal instead, which GNOME and KDE answer and others may not,
// and an X11 program under XWayland hears its shortcut only while one of its own windows has focus.
function waylandSession(env) {
  const type = String(env.XDG_SESSION_TYPE || '').toLowerCase();
  if (type === 'wayland') return true;
  if (type === 'x11') return false;
  return Boolean(env.WAYLAND_DISPLAY);
}

// trayNote is what the settings page says about the tray on Linux, where an icon is shown only by a
// desktop that implements StatusNotifierItem. Electron cannot tell whether anything displays it:
// creating one succeeds on plain GNOME and draws nothing. 'gnome' is that case (Ubuntu's GNOME
// ships the AppIndicator extension and shows it); 'unavailable' is no session bus at all.
function trayNote(platform, env) {
  if (platform !== 'linux') return '';
  if (!env.DBUS_SESSION_BUS_ADDRESS) return 'unavailable';
  const desktop = String(env.XDG_CURRENT_DESKTOP || '').toLowerCase();
  if (desktop.includes('gnome') && !desktop.includes('ubuntu') && !desktop.includes('unity')) return 'gnome';
  return '';
}

// hideOnClose decides whether closing the main window only hides it. It never does while the
// application is quitting, and never when there is no tray to bring it back from — a window hidden
// with no icon is found again only by opening the application a second time.
function hideOnClose({ hideToTray, trayReady, quitting }) {
  return Boolean(hideToTray && trayReady && !quitting);
}

function count(value) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? Math.min(Math.floor(n), 999) : 0;
}

// normaliseStatus reads what the app in the main window reports. The page is the app's, on
// loopback, but it is still a page: nothing it sends is trusted to be well-formed.
function normaliseStatus(raw) {
  const status = raw && typeof raw === 'object' ? raw : {};
  const companion = status.companion && typeof status.companion === 'object' ? status.companion : null;
  return {
    running: count(status.running),
    waiting: count(status.waiting),
    companion: companion ? { present: true, paused: Boolean(companion.paused) } : { present: false, paused: false },
  };
}

function fill(text, values) {
  return String(text).replace(/\{(\w+)\}/g, (_, name) => (name in values ? String(values[name]) : `{${name}}`));
}

// trayView is what the icon shows: which picture, the tooltip and the summary line at the top of its
// menu. An approval waiting outranks agents running — it is the one that needs the operator.
function trayView(raw, strings) {
  const status = normaliseStatus(raw);
  const parts = [];
  if (status.running) parts.push(fill(strings.trayRunning, { n: status.running }));
  if (status.waiting) parts.push(fill(strings.trayWaiting, { n: status.waiting }));
  const summary = parts.length ? parts.join(' · ') : strings.trayIdle;
  return {
    icon: status.waiting ? 'waiting' : status.running ? 'running' : 'idle',
    summary,
    tooltip: `Daedalus — ${summary}`,
    // The number beside the icon in the macOS menu bar: what waits for the operator, and nothing else.
    title: status.waiting ? String(status.waiting) : '',
    status,
  };
}

// trayTemplate is the tray's menu. Pause appears only when the app reported a companion: an
// installation without one has nothing to pause, and an item that does nothing is a lie. Stop the
// agent appears only while it holds the screen, first after the summary, where a hurried hand finds
// it; its chord is shown but registered by computer-main.js, and only while the lease lasts.
function trayTemplate(view, strings, settings, actions, computer) {
  const items = [{ label: view.summary, enabled: false }];
  if (computer && computer.active) {
    items.push(
      { label: strings.trayControlling, enabled: false },
      { label: strings.trayStopAgent, accelerator: computer.chord || undefined, registerAccelerator: false, click: actions.stopComputer },
    );
  }
  items.push(
    { type: 'separator' },
    { label: strings.trayOpen, click: actions.open },
    { label: strings.trayQuickChat, accelerator: settings.quickChat || undefined, registerAccelerator: false, click: actions.quickChat },
    { label: strings.trayAppshot, accelerator: settings.appshot || undefined, registerAccelerator: false, click: actions.appshot },
    { label: strings.trayRegion, accelerator: settings.region || undefined, registerAccelerator: false, click: actions.region },
    { label: strings.trayVoice, accelerator: settings.voice || undefined, registerAccelerator: false, click: actions.voice },
  );
  if (view.status.companion.present) {
    items.push({ type: 'separator' });
    items.push(view.status.companion.paused
      ? { label: strings.trayResume, click: actions.resumeCompanion }
      : { label: strings.trayPause, click: actions.pauseCompanion });
  }
  items.push({ type: 'separator' }, { label: strings.trayQuit, click: actions.quit });
  return items;
}

// actionFrom finds the flag a second start carries when the operator bound a system shortcut to it —
// the way round a desktop whose portal refuses global shortcuts: `daedalus --quick-chat`.
function actionFrom(argv) {
  const args = (argv || []).filter((arg) => typeof arg === 'string');
  if (args.includes('--quick-chat')) return 'quickChat';
  if (args.includes('--appshot')) return 'appshot';
  if (args.includes('--region')) return 'region';
  if (args.includes('--voice')) return 'voice';
  // The tray's Quit, for a script or an uninstaller: with closing the window only hiding it, there
  // was no way left to end the application but the tray's own menu.
  if (args.includes('--quit')) return 'quit';
  return null;
}

// quickBounds places Quick Chat on the screen the pointer is on, centred across it and a fifth of the
// way down, where a launcher box is expected. A window narrower than the box keeps a margin.
function quickBounds(workArea, width, height) {
  const w = Math.min(width, Math.max(320, workArea.width - 32));
  const h = Math.min(height, Math.max(120, workArea.height - 32));
  return {
    x: Math.round(workArea.x + (workArea.width - w) / 2),
    y: Math.round(workArea.y + Math.min(workArea.height * 0.2, workArea.height - h - 16)),
    width: w,
    height: h,
  };
}

// The height Quick Chat may ask for: its box alone up to the box with a list of thumbnails or the
// Computer scope picker open.
const QUICK_WIDTH = 680;
const QUICK_MIN_HEIGHT = 96;
const QUICK_MAX_HEIGHT = 640;

function quickHeight(asked) {
  const n = Number(asked);
  if (!Number.isFinite(n)) return QUICK_MIN_HEIGHT;
  return Math.max(QUICK_MIN_HEIGHT, Math.min(QUICK_MAX_HEIGHT, Math.round(n)));
}

// appPath accepts only an address inside the app for Quick Chat's "open": the page may ask the main
// window to show a conversation, and nothing outside /app/ on the app's own origin.
function appPath(value) {
  if (typeof value !== 'string' || !value.startsWith('/app/') || value.includes('\\') || value.startsWith('//')) return null;
  try {
    const url = new URL(value, 'http://127.0.0.1');
    return url.pathname.startsWith('/app/') ? url.pathname + url.search : null;
  } catch {
    return null;
  }
}

// appOrigin is where the app answers, read off an address the main window was sent to: the launcher's
// own pages are on loopback as well, but only the app lives under /app/.
function appOrigin(raw) {
  try {
    const url = new URL(raw);
    if (!(url.protocol === 'http:' || url.protocol === 'https:')) return null;
    if (!['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) return null;
    return url.pathname === '/app' || url.pathname.startsWith('/app/') ? url.origin : null;
  } catch {
    return null;
  }
}

module.exports = {
  DEFAULTS,
  SHORTCUTS,
  QUICK_WIDTH,
  QUICK_MIN_HEIGHT,
  QUICK_MAX_HEIGHT,
  validAccelerator,
  normaliseSettings,
  mergeSettings,
  registerShortcuts,
  waylandSession,
  trayNote,
  hideOnClose,
  normaliseStatus,
  trayView,
  trayTemplate,
  actionFrom,
  quickBounds,
  quickHeight,
  appPath,
  appOrigin,
  fill,
};
