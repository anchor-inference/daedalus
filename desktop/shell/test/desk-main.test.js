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

function fakeElectron({ sources = [] } = {}) {
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
      this.file = '';
      this.closed = [];
      this.fullScreen = false;
      windows.push(this);
    }
    static getAllWindows() {
      return windows.filter((w) => !w.destroyed);
    }
    loadURL(url) {
      this.webContents.url = url;
    }
    loadFile(file, options) {
      this.file = path.basename(file);
      this.query = options && options.query;
    }
    setAlwaysOnTop() {}
    setVisibleOnAllWorkspaces() {}
    setFullScreen(value) {
      this.fullScreen = value;
    }
    on(name, handler) {
      if (name === 'closed') this.closed.push(handler);
    }
    isDestroyed() {
      return this.destroyed;
    }
    destroy() {
      if (this.destroyed) return;
      this.destroyed = true;
      for (const handler of this.closed) handler();
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
    getContentBounds() {
      return this.options.x === undefined ? this.bounds : { x: this.options.x, y: this.options.y, width: this.options.width, height: this.options.height };
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
  const display = { id: 1, size: { width: 1920, height: 1080 }, scaleFactor: 1, bounds: { x: 0, y: 0, width: 1920, height: 1080 }, workArea: { x: 0, y: 0, width: 1920, height: 1040 } };
  const second = { id: 2, size: { width: 1280, height: 800 }, scaleFactor: 2, bounds: { x: 1920, y: 0, width: 1280, height: 800 }, workArea: { x: 1920, y: 0, width: 1280, height: 800 } };
  const displays = sources.length > 1 ? [display, second] : [display];
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
      screen: { getCursorScreenPoint: () => ({ x: 5, y: 5 }), getDisplayNearestPoint: () => display, getAllDisplays: () => displays },
      shell: { openExternal() {} },
      systemPreferences: { getMediaAccessStatus: () => 'granted' },
      desktopCapturer: { getSources: async () => sources },
    },
    FakeContents,
  };
}

function start(options) {
  const electron = fakeElectron(options);
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
  assert.deepEqual([...electron.shortcuts.keys()].sort(), ['Alt+Shift+S', 'Alt+Shift+Space', 'Alt+Shift+V', 'Alt+Space']);
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
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(userData, 'desk.json'), 'utf8')), { quickChat: 'Ctrl+Shift+K', appshot: 'Alt+Shift+Space', region: 'Alt+Shift+S', voice: 'Alt+Shift+V', hideToTray: false });
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

// A still of a display, as desktopCapturer returns it: what the overlay draws and what is cut from.
function still(name, width, height, steps = []) {
  return {
    isEmpty: () => false,
    getSize: () => ({ width, height }),
    toPNG: () => Buffer.from(`png:${name}:${steps.join('/')}`),
    toJPEG: () => Buffer.from(`jpeg:${name}`),
    resize: (size) => still(name, size.width, size.height, [...steps, `resize ${size.width}x${size.height}`]),
    crop: (rect) => still(name, rect.width, rect.height, [...steps, `crop ${rect.x},${rect.y} ${rect.width}x${rect.height}`]),
  };
}

const twoScreens = [
  { id: 'screen:1', display_id: '1', thumbnail: still('left', 1920, 1080) },
  { id: 'screen:2', display_id: '2', thumbnail: still('right', 2560, 1600) },
];
const settle = () => new Promise((resolve) => setTimeout(resolve, 300));

test('the region shortcut puts an overlay on every display and attaches the cut from the one dragged on', async () => {
  const { electron, desk, from } = start({ sources: twoScreens });
  desk.noteAddress('http://127.0.0.1:48765/app/');
  electron.shortcuts.get('Alt+Shift+S')();
  await settle();
  const overlays = electron.windows.filter((w) => w.file === 'region.html');
  assert.equal(overlays.length, 2);
  assert.deepEqual(overlays.map((w) => [w.options.x, w.options.width]), [[0, 1920], [1920, 1280]]);
  assert.equal(overlays[0].query.hint, 'Drag to select an area · Esc cancels');
  assert.equal(overlays[1].visible, false, 'nothing is shown before its page has drawn the still');
  const image = electron.handlers.get('region:image');
  assert.equal(await image(from(overlays[1].webContents)), `data:image/jpeg;base64,${Buffer.from('jpeg:right').toString('base64')}`);
  assert.equal(await image(from(new electron.FakeContents())), null, 'a page that is no overlay is told nothing');
  electron.listeners.get('region:ready')(from(overlays[1].webContents));
  assert.equal(overlays[1].visible, true);
  // The page names a rectangle; the display is the overlay's own, whatever the page says.
  electron.listeners.get('region:done')(from(overlays[1].webContents), { displayId: 1, rect: { x: 10, y: 20, width: 30, height: 40 } });
  await settle();
  assert.ok(overlays.every((w) => w.destroyed), 'every overlay goes once one answered');
  const quick = electron.windows.find((w) => w.options.frame === false && w.file === '');
  assert.deepEqual(quick.webContents.sent.at(-1), ['quick:shown', { appshot: true }]);
  const [shot] = await electron.handlers.get('quick:pending')(from(quick.webContents));
  assert.equal(shot.ok, true);
  assert.equal(Buffer.from(shot.bytes).toString(), 'png:right:crop 20,40 60x80');
});

test('Esc on the overlay ends the region shortcut where it started', async () => {
  const { electron, desk, from } = start({ sources: twoScreens });
  desk.noteAddress('http://127.0.0.1:48765/app/');
  electron.shortcuts.get('Alt+Shift+S')();
  await settle();
  const overlays = electron.windows.filter((w) => w.file === 'region.html');
  electron.listeners.get('region:done')(from(overlays[0].webContents), null);
  await settle();
  assert.ok(overlays.every((w) => w.destroyed));
  assert.equal(electron.windows.some((w) => w.options.frame === false && w.file === '' && w.visible), false, 'Quick Chat was closed and stays closed');
  // A second selection may start: the capture is no longer busy.
  electron.shortcuts.get('Alt+Shift+S')();
  await settle();
  assert.equal(electron.windows.filter((w) => w.file === 'region.html' && !w.destroyed).length, 2);
});

test("Quick Chat's area button gets the cut back and Quick Chat returns", async () => {
  const { electron, desk, from } = start({ sources: twoScreens });
  desk.noteAddress('http://127.0.0.1:48765/app/');
  electron.shortcuts.get('Alt+Space')();
  const quick = electron.windows.find((w) => w.options.frame === false);
  const asked = electron.handlers.get('quick:region')(from(quick.webContents));
  await settle();
  assert.equal(quick.visible, false, 'Quick Chat is out of the picture while the area is chosen');
  const overlays = electron.windows.filter((w) => w.file === 'region.html');
  electron.listeners.get('region:done')(from(overlays[0].webContents), { rect: { x: 0, y: 0, width: 100, height: 50 } });
  const shot = await asked;
  assert.equal(Buffer.from(shot.bytes).toString(), 'png:left:crop 0,0 100x50');
  assert.equal(quick.visible, true);
  assert.equal(await electron.handlers.get('quick:region')(from(new electron.FakeContents())), null);
});

test('the voice shortcut opens Quick Chat and the page collects the request once', async () => {
  const { electron, desk, from, shown } = start();
  electron.shortcuts.get('Alt+Shift+V')();
  assert.equal(shown(), 1, 'before the app answered, the main window comes forward');
  desk.noteAddress('http://127.0.0.1:48765/app/');
  electron.shortcuts.get('Alt+Shift+V')();
  const quick = electron.windows.find((w) => w.options.frame === false);
  assert.equal(quick.visible, true);
  assert.deepEqual(quick.webContents.sent.at(-1), ['quick:shown', { voice: true }]);
  const voice = electron.handlers.get('quick:voice');
  assert.equal(await voice(from(new electron.FakeContents())), false);
  assert.equal(await voice(from(quick.webContents)), true);
  assert.equal(await voice(from(quick.webContents)), false);
  // Pressed again while Quick Chat is open, it asks again (the page stops the recording).
  electron.shortcuts.get('Alt+Shift+V')();
  assert.equal(quick.visible, true);
  assert.equal(await voice(from(quick.webContents)), true);
});
