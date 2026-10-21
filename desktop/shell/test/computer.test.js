'use strict';

// The signs of the agent holding the screen: what a lease event turns into (border, note, shortcut,
// tray item), where they go, and computer-main.js against a stand-in for Electron, so it is proven
// that the shortcut lives only as long as the lease and that every window ignores the mouse, never
// takes the focus and asks to be left out of screenshots.

const test = require('node:test');
const assert = require('node:assert/strict');
const computer = require('../computer');
const desk = require('../desk');
const policy = require('../policy');
const startComputer = require('../computer-main');

test('a lease arms everything, no lease nothing, and anything unreadable is no lease', () => {
  assert.deepEqual(computer.computerPlan({ active: true, paused: false, scope: 'window' }, 'win32'), { hotkey: true, border: true, note: true, trayStop: true, paused: false });
  assert.deepEqual(computer.computerPlan({ active: false, paused: true }, 'win32'), { hotkey: false, border: false, note: false, trayStop: false, paused: false });
  for (const junk of [null, undefined, 'yes', { active: 'true' }, { active: 1 }]) {
    assert.equal(computer.computerPlan(junk, 'darwin').border, false);
  }
  assert.equal(computer.computerPlan({ active: true, paused: true }, 'darwin').paused, true);
  // On X11 the daemon would take the note for the window under a point and refuse the click.
  assert.equal(computer.computerPlan({ active: true }, 'linux').note, false);
  assert.equal(computer.computerPlan({ active: true }, 'linux').border, true);
  assert.equal(computer.computerPlan({ active: true }, 'linux').hotkey, true);
});

test("the border is four strips on a display's outermost pixels, and the note sits at the top centre", () => {
  const bounds = { x: -1920, y: 0, width: 1920, height: 1080 };
  assert.deepEqual(computer.edgeRects(bounds, 4), [
    { x: -1920, y: 0, width: 1920, height: 4 },
    { x: -1920, y: 1076, width: 1920, height: 4 },
    { x: -1920, y: 4, width: 4, height: 1072 },
    { x: -4, y: 4, width: 4, height: 1072 },
  ]);
  assert.deepEqual(computer.noteBounds({ x: 0, y: 25, width: 1440, height: 875 }), { x: 490, y: 33, width: 460, height: 34 });
  assert.equal(computer.noteBounds({ x: 0, y: 0, width: 300, height: 600 }).width, 284);
});

test('the Stop chord is said the way each platform says it, and the first free one is taken', () => {
  assert.equal(computer.chordLabel('Control+Alt+Escape', 'win32'), 'Ctrl+Alt+Esc');
  assert.equal(computer.chordLabel('Control+Alt+Escape', 'darwin'), '⌃⌥Esc');
  assert.equal(computer.chordLabel('', 'linux'), '');
  const taken = new Set(['Control+Alt+Escape']);
  const shortcuts = { register: (chord) => !taken.has(chord) };
  assert.equal(computer.armStop(shortcuts, () => {}), 'Control+Alt+Shift+Escape');
  assert.equal(computer.armStop({ register: () => { throw new Error('portal said no'); } }, () => {}), '');
});

test("the tray offers Stop the agent only while it holds the screen, in the system's language", () => {
  const settings = desk.normaliseSettings(null);
  const calls = [];
  const actions = Object.fromEntries(['open', 'quickChat', 'appshot', 'region', 'voice', 'stopComputer', 'quit'].map((name) => [name, () => calls.push(name)]));
  const en = policy.strings('en');
  const idle = desk.trayTemplate(desk.trayView({}, en), en, settings, actions, { active: false, chord: '' });
  assert.ok(!idle.some((item) => item.label === 'Stop the agent'));
  const held = desk.trayTemplate(desk.trayView({}, en), en, settings, actions, { active: true, chord: 'Control+Alt+Escape' });
  assert.deepEqual(held.slice(0, 3).map((item) => item.label), ['Nothing running', 'The agent is controlling the computer', 'Stop the agent']);
  const stop = held[2];
  assert.equal(stop.accelerator, 'Control+Alt+Escape');
  assert.equal(stop.registerAccelerator, false);
  stop.click();
  assert.deepEqual(calls, ['stopComputer']);
  const ru = policy.strings('ru');
  assert.ok(desk.trayTemplate(desk.trayView({}, ru), ru, settings, actions, { active: true }).some((item) => item.label === 'Остановить агента'));
});

function fakeElectron() {
  const windows = [];
  const registered = new Map();
  const screenListeners = new Map();
  class BrowserWindow {
    constructor(options) {
      this.options = options;
      this.destroyed = false;
      this.shown = false;
      this.ignoresMouse = false;
      this.protected = false;
      this.level = '';
      this.file = '';
      this.once = (event, fn) => event === 'ready-to-show' && (this.ready = fn);
      this.webContents = { setWindowOpenHandler: () => {}, on: () => {} };
      windows.push(this);
    }
    setIgnoreMouseEvents(value) {
      this.ignoresMouse = value;
    }
    setAlwaysOnTop(on, level) {
      this.level = on ? level : '';
    }
    setContentProtection(value) {
      this.protected = value;
    }
    setVisibleOnAllWorkspaces() {}
    setBounds(bounds) {
      this.bounds = bounds;
    }
    showInactive() {
      this.shown = true;
    }
    loadFile(file, options) {
      this.file = file;
      this.query = options && options.query;
    }
    isDestroyed() {
      return this.destroyed;
    }
    destroy() {
      this.destroyed = true;
    }
  }
  const displays = [
    { bounds: { x: 0, y: 0, width: 1920, height: 1080 }, workArea: { x: 0, y: 0, width: 1920, height: 1040 } },
    { bounds: { x: 1920, y: 0, width: 1280, height: 1024 }, workArea: { x: 1920, y: 0, width: 1280, height: 1024 } },
  ];
  return {
    windows,
    registered,
    displays,
    screenListeners,
    electron: {
      BrowserWindow,
      app: { on: () => {} },
      globalShortcut: {
        register: (chord, fn) => {
          registered.set(chord, fn);
          return true;
        },
        unregister: (chord) => registered.delete(chord),
      },
      screen: {
        getAllDisplays: () => displays,
        getPrimaryDisplay: () => displays[0],
        on: (event, fn) => screenListeners.set(event, fn),
      },
    },
  };
}

test('a lease draws the signs on every display and the shortcut stops it; its end takes them all away', () => {
  const fake = fakeElectron();
  const stops = [];
  const changes = [];
  const signs = startComputer({ electron: fake.electron, dir: '/app', getStrings: () => policy.strings('en'), sendStop: () => stops.push('stop'), onChange: (state) => changes.push(state), platform: 'win32' });
  assert.equal(fake.windows.length, 0);

  signs.update({ active: true, paused: false, scope: 'window' });
  const live = fake.windows.filter((w) => !w.destroyed);
  // Four strips on each of two displays, and the note.
  assert.equal(live.length, 9);
  for (const window of live) {
    assert.equal(window.ignoresMouse, true);
    assert.equal(window.protected, true);
    assert.equal(window.level, 'screen-saver');
    assert.equal(window.options.focusable, false);
    assert.equal(window.options.skipTaskbar, true);
    assert.equal(window.options.transparent, false);
  }
  const note = live.find((w) => w.file);
  assert.match(note.file, /pages[\\/]computer\.html$/);
  assert.equal(note.query.text, 'Daedalus is controlling the computer — Ctrl+Alt+Esc to stop');
  assert.deepEqual([...fake.registered.keys()], ['Control+Alt+Escape']);
  assert.deepEqual(changes.at(-1), { active: true, paused: false, scope: 'window', chord: 'Control+Alt+Escape' });

  // The same lease said again (the launcher's answer after a Stop that failed) redraws nothing.
  signs.update({ active: true, paused: false, scope: 'window' });
  assert.equal(fake.windows.length, 9);

  fake.registered.get('Control+Alt+Escape')();
  assert.deepEqual(stops, ['stop']);

  // Paused by the operator's own hand: the signs stay, in the pause's colour and words.
  signs.update({ active: true, paused: true, scope: 'window' });
  const paused = fake.windows.filter((w) => !w.destroyed);
  assert.equal(paused.length, 9);
  assert.ok(paused.every((w) => w.options.backgroundColor === '#c27c0e'));
  assert.equal(paused.find((w) => w.file).query.paused, '1');

  signs.update({ active: false });
  assert.equal(fake.windows.filter((w) => !w.destroyed).length, 0);
  assert.equal(fake.registered.size, 0);
  assert.deepEqual(changes.at(-1), { active: false, paused: false, scope: '', chord: '' });
  // A Stop with no lease is a Stop of nothing and is not sent.
  signs.stop();
  assert.deepEqual(stops, ['stop']);
});

test('a display added while the lease lasts gets its border, and the launcher going away clears everything', () => {
  const fake = fakeElectron();
  const signs = startComputer({ electron: fake.electron, dir: '/app', getStrings: () => policy.strings('ru'), sendStop: () => {}, onChange: () => {}, platform: 'linux' });
  signs.update({ active: true });
  // Linux: the border only (see computerPlan).
  assert.equal(fake.windows.filter((w) => !w.destroyed).length, 8);
  fake.displays.push({ bounds: { x: 3200, y: 0, width: 800, height: 600 }, workArea: { x: 3200, y: 0, width: 800, height: 600 } });
  fake.screenListeners.get('display-added')();
  assert.equal(fake.windows.filter((w) => !w.destroyed).length, 12);
  signs.reset();
  assert.equal(fake.windows.filter((w) => !w.destroyed).length, 0);
  assert.equal(fake.registered.size, 0);
});
