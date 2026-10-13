'use strict';

// desk-main.js against a stand-in for Electron: which window may ask what, what reaches the tray,
// and where Quick Chat's page is loaded from. Nothing here opens a window.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Module = require('node:module');
const policy = require('../policy');

// The tray is made only where a Linux session has a bus to show it on; these tests are about what
// the tray says, so there is always one.
process.env.DBUS_SESSION_BUS_ADDRESS = process.env.DBUS_SESSION_BUS_ADDRESS || 'unix:path=/run/user/1000/bus';
process.env.XDG_CURRENT_DESKTOP = 'KDE';

function fakeElectron() {
  const handlers = new Map();
  const listeners = new Map();
  const windows = [];
  const trays = [];
  const shortcuts = new Map();
  class FakeContents {
    constructor() {
      this.sent = [];
      this.url = '';
    }
    send(channel, value) {
      this.sent.push([channel, value]);
    }
    getURL() {
      return this.url;
    }
  }
  class BrowserWindow {
    constructor(options) {
      this.options = options;
      this.webContents = new FakeContents();
      this.visible = false;
      this.bounds = { x: 0, y: 0, width: options.width, height: options.height };
      this.destroyed = false;
      windows.push(this);
    }
    static getAllWindows() {
      return windows.filter((w) => !w.destroyed);
    }
    loadURL(url) {
      this.webContents.url = url;
    }
    setAlwaysOnTop() {}
    setVisibleOnAllWorkspaces() {}
    on() {}
    isDestroyed() {
      return this.destroyed;
    }
    destroy() {
      this.destroyed = true;
    }
    show() {
      this.visible = true;
    }
    hide() {
      this.visible = false;
    }
    focus() {}
    isVisible() {
      return this.visible;
    }
    isFocused() {
      return this.visible;
    }
    setBounds(bounds) {
      this.bounds = bounds;
    }
    getPosition() {
      return [this.bounds.x, this.bounds.y];
    }
    getMediaSourceId() {
      return `window:${windows.indexOf(this)}:0`;
    }
  }
  class Tray {
    constructor(image) {
      this.image = image;
      this.tooltip = '';
      this.menu = null;
      trays.push(this);
    }
    on() {}
    setImage(image) {
      this.image = image;
    }
    setToolTip(text) {
      this.tooltip = text;
    }
    setTitle() {}
    setContextMenu(menu) {
      this.menu = menu;
    }
  }
  const display = { id: 1, size: { width: 1920, height: 1080 }, scaleFactor: 1, workArea: { x: 0, y: 0, width: 1920, height: 1040 } };
  return {
    handlers,
    listeners,
    windows,
    trays,
    shortcuts,
    module: {
      BrowserWindow,
      Tray,
      Menu: { buildFromTemplate: (template) => template },
      nativeImage: { createFromPath: (file) => path.basename(file) },
      nativeTheme: { shouldUseDarkColors: true },
      globalShortcut: {
        register: (accelerator, callback) => {
          shortcuts.set(accelerator, callback);
          return true;
        },
        unregister: (accelerator) => shortcuts.delete(accelerator),
      },
      ipcMain: {
        handle: (channel, handler) => handlers.set(channel, handler),
        on: (channel, handler) => listeners.set(channel, handler),
      },
      screen: { getCursorScreenPoint: () => ({ x: 5, y: 5 }), getDisplayNearestPoint: () => display, getAllDisplays: () => [display] },
      shell: { openExternal() {} },
      systemPreferences: { getMediaAccessStatus: () => 'granted' },
      desktopCapturer: { getSources: async () => [] },
    },
    FakeContents,
  };
}

function start() {
  const electron = fakeElectron();
  const load = Module._load;
  Module._load = function (request, ...rest) {
    return request === 'electron' ? electron.module : load.call(this, request, ...rest);
  };
  delete require.cache[require.resolve('../desk-main')];
  let deskMain;
  try {
    deskMain = require('../desk-main');
  } finally {
    Module._load = load;
  }
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'desk-'));
  const quitHooks = [];
  const app = { getPath: () => userData, on: (name, hook) => quitHooks.push(hook), quit() {}, focus() {} };
  const main = new electron.module.BrowserWindow({ width: 1180, height: 820 });
  main.webContents.url = 'http://127.0.0.1:48765/app/agents';
  let shown = 0;
  const desk = deskMain({ app, dir: path.resolve(__dirname, '..'), getMain: () => main, showMain: () => shown++, getStrings: () => policy.strings('en'), guard() {}, language: 'en-US' });
  const from = (contents, url) => ({ sender: contents, senderFrame: { url: url || contents.getURL() } });
  return { electron, desk, main, from, userData, shown: () => shown };
}

test('the shortcuts are registered and the tray starts idle', () => {
  const { electron } = start();
  assert.deepEqual([...electron.shortcuts.keys()].sort(), ['Alt+Shift+Space', 'Alt+Space']);
  const tray = electron.trays[0];
  assert.equal(tray.image, 'idle.png');
  assert.equal(tray.tooltip, 'Daedalus — Nothing running');
});

test("only the app in the main window reports the tray's status", () => {
  const { electron, main, from } = start();
  const tray = electron.trays[0];
  const report = electron.listeners.get('desk:status');
  const stranger = new electron.FakeContents();
  stranger.url = 'http://127.0.0.1:48765/app/agents';
  report(from(stranger), { running: 5 });
  assert.equal(tray.image, 'idle.png');
  report(from(main.webContents), { running: 2, waiting: 1, companion: { paused: false } });
  assert.equal(tray.image, 'waiting.png');
  assert.equal(tray.tooltip, 'Daedalus — Agents running: 2 · Waiting for you: 1');
  assert.ok(tray.menu.some((item) => item.label === 'Pause the companion'));
});

test('Quick Chat waits for the app, then loads its page from the app’s own origin', () => {
  const { electron, desk, shown } = start();
  electron.shortcuts.get('Alt+Space')();
  assert.equal(shown(), 1, 'before the app answered, the shortcut brings the main window instead');
  desk.noteAddress('http://127.0.0.1:8770/setup');
  electron.shortcuts.get('Alt+Space')();
  assert.equal(shown(), 2, 'the launcher’s page is not the app');
  desk.noteAddress('http://127.0.0.1:48765/app/');
  electron.shortcuts.get('Alt+Space')();
  const quick = electron.windows.find((w) => w.options.frame === false);
  assert.equal(quick.webContents.url, 'http://127.0.0.1:48765/app/quick?lang=en');
  assert.equal(quick.visible, true);
  assert.deepEqual(quick.webContents.sent.at(-1), ['quick:shown', {}]);
  electron.shortcuts.get('Alt+Space')();
  assert.equal(quick.visible, false, 'the shortcut again hides it');
});

test('settings change only from the main window, are saved, and re-register the shortcut', async () => {
  const { electron, desk, main, from, userData } = start();
  desk.noteAddress('http://127.0.0.1:48765/app/');
  electron.shortcuts.get('Alt+Space')();
  const quick = electron.windows.find((w) => w.options.frame === false);
  const set = electron.handlers.get('desk:set-settings');
  assert.equal(desk.keepOnClose(false), true, 'closing hides the window by default');
  assert.equal(desk.keepOnClose(true), false, 'but not while quitting');
  assert.equal(await set(from(quick.webContents), { quickChat: 'Ctrl+Shift+K' }), null);
  const answer = await set(from(main.webContents), { quickChat: 'Ctrl+Shift+K', hideToTray: false });
  assert.equal(answer.settings.quickChat, 'Ctrl+Shift+K');
  assert.equal(answer.report.quickChat.ok, true);
  assert.ok(electron.shortcuts.has('Ctrl+Shift+K'));
  assert.ok(!electron.shortcuts.has('Alt+Space'));
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(userData, 'desk.json'), 'utf8')), { quickChat: 'Ctrl+Shift+K', appshot: 'Alt+Shift+Space', hideToTray: false });
  assert.equal(desk.keepOnClose(false), false);
});

test('"open" from Quick Chat moves the app in the main window without reloading it', () => {
  const { electron, desk, main, from } = start();
  desk.noteAddress('http://127.0.0.1:48765/app/');
  electron.shortcuts.get('Alt+Space')();
  const quick = electron.windows.find((w) => w.options.frame === false);
  const open = electron.listeners.get('quick:open');
  open(from(main.webContents), '/app/agents/x');
  assert.equal(main.webContents.sent.length, 0, 'only Quick Chat may ask');
  open(from(quick.webContents), 'https://example.com/');
  assert.equal(main.webContents.sent.length, 0);
  open(from(quick.webContents), '/app/agents/abc');
  assert.deepEqual(main.webContents.sent.at(-1), ['desk:navigate', '/app/agents/abc']);
  assert.equal(quick.visible, false);
});

test('the appshot shortcut hands its picture to Quick Chat once, when it asks', async () => {
  const { electron, desk, from } = start();
  desk.noteAddress('http://127.0.0.1:48765/app/');
  electron.shortcuts.get('Alt+Shift+Space')();
  await new Promise((resolve) => setTimeout(resolve, 20));
  const quick = electron.windows.find((w) => w.options.frame === false);
  assert.deepEqual(quick.webContents.sent.at(-1), ['quick:shown', { appshot: true }]);
  const pending = electron.handlers.get('quick:pending');
  const first = await pending(from(quick.webContents));
  assert.equal(first.length, 1);
  assert.equal(first[0].error, 'cancelled');
  assert.deepEqual(await pending(from(quick.webContents)), []);
});

test('on plain GNOME, where the icon may draw nothing, closing the window quits', () => {
  process.env.XDG_CURRENT_DESKTOP = 'GNOME';
  try {
    const { desk } = start();
    assert.equal(desk.keepOnClose(false), process.platform !== 'linux');
  } finally {
    process.env.XDG_CURRENT_DESKTOP = 'KDE';
  }
});
