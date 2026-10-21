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
  assert.deepEqual(computer.noteBounds({ x: 0, y: 25, width: 1440, height: 875 }), { x: 440, y: 35, width: 560, height: 44 });
  assert.equal(computer.noteBounds({ x: 0, y: 0, width: 300, height: 600 }).width, 280);
});

test('the border is the same thickness on every side, inside a display and outside a window', () => {
  assert.equal(computer.BORDER, 3);
  const displays = [
    { bounds: { x: 0, y: 0, width: 1536, height: 864 }, workArea: { x: 0, y: 0, width: 1536, height: 824 } },
    { bounds: { x: 1536, y: -200, width: 1280, height: 1024 }, workArea: { x: 1536, y: -200, width: 1280, height: 1024 } },
  ];
  // A display: its own Electron bounds, whatever rounding the conversion left in the target.
  const display = computer.borderRects([{ kind: 'display', x: 1537, y: -199, width: 1279, height: 1023 }], displays);
  assert.deepEqual(display, computer.edgeRects(displays[1].bounds, 3));
  // A window in the middle of a display: the strips lie just outside it, three pixels each way.
  const win = computer.borderRects([{ kind: 'window', x: 100, y: 100, width: 400, height: 300 }], displays);
  assert.deepEqual(win, [
    { x: 97, y: 97, width: 406, height: 3 },
    { x: 97, y: 400, width: 406, height: 3 },
    { x: 97, y: 100, width: 3, height: 300 },
    { x: 500, y: 100, width: 3, height: 300 },
  ]);
  for (const strip of win) assert.equal(Math.min(strip.width, strip.height), 3);
  // A maximised window: the strips come inside rather than off the screen, still three thick.
  const max = computer.borderRects([{ kind: 'window', x: 0, y: 0, width: 1536, height: 824 }], displays);
  assert.deepEqual(max[0], { x: 0, y: 0, width: 1536, height: 3 });
  assert.deepEqual(max[1], { x: 0, y: 824, width: 1536, height: 3 });
  assert.deepEqual(max[2], { x: 0, y: 3, width: 3, height: 821 });
  // Only the targets get a border: one display of two is four strips, not eight; a window on no
  // display (minimised, off screen) none; the same display twice once.
  assert.equal(computer.borderRects([{ kind: 'display', x: 0, y: 0, width: 1536, height: 864 }], displays).length, 4);
  assert.equal(computer.borderRects([{ kind: 'window', x: -32000, y: -32000, width: 160, height: 28 }], displays).length, 0);
  assert.equal(computer.borderRects([{ kind: 'display', ...displays[0].bounds }, { kind: 'display', ...displays[0].bounds }], displays).length, 4);
});

test("the daemon's pixels become Electron's units the way each platform lays out its displays", () => {
  // Windows at 125 %: only Electron knows where each display sits in its own units.
  const asked = [];
  const convert = (rect) => {
    asked.push(rect);
    return { x: Math.floor(rect.x / 1.25), y: Math.floor(rect.y / 1.25), width: Math.ceil(rect.width / 1.25), height: Math.ceil(rect.height / 1.25) };
  };
  assert.deepEqual(computer.toDip({ x: 125.5, y: 250, width: 500, height: 375.2 }, 'win32', { convert }), { x: 100, y: 200, width: 400, height: 301 });
  assert.deepEqual(asked, [{ x: 125, y: 250, width: 500, height: 376 }]);
  // macOS: the daemon's points are Electron's units already.
  assert.deepEqual(computer.toDip({ x: -1440, y: 0, width: 1440, height: 900 }, 'darwin'), { x: -1440, y: 0, width: 1440, height: 900 });
  // Linux with a scale of 1.5 for the whole screen: widened to whole units, never narrowed.
  assert.deepEqual(computer.toDip({ x: 151, y: 0, width: 300, height: 299 }, 'linux', { scale: 1.5 }), { x: 100, y: 0, width: 201, height: 200 });
  assert.deepEqual(computer.toDip({ x: 10, y: 20, width: 30, height: 40 }, 'linux', {}), { x: 10, y: 20, width: 30, height: 40 });
});

test('the note goes to the display the agent works on, and the launcher\'s targets are read with care', () => {
  const primary = { bounds: { x: 0, y: 0, width: 1920, height: 1080 }, workArea: { x: 0, y: 0, width: 1920, height: 1040 } };
  const side = { bounds: { x: -1280, y: 0, width: 1280, height: 1024 }, workArea: { x: -1280, y: 25, width: 1280, height: 999 } };
  assert.equal(computer.noteDisplay([{ kind: 'window', x: -900, y: 100, width: 300, height: 200 }], [primary, side], primary), side);
  assert.equal(computer.noteDisplay([], [primary, side], primary), primary);
  const state = computer.normaliseComputer({ active: true, scope: 'app', targets: [{ kind: 'window', x: 1, y: 2, w: 3, h: 4 }, { kind: 'window', x: 'a', y: 0, w: 1, h: 1 },
    { kind: 'display', x: 0, y: 0, w: 0, h: 10 }, { kind: 'other', x: 0, y: 0, w: 1, h: 1 }, null] });
  assert.deepEqual(state.targets, [{ kind: 'window', x: 1, y: 2, width: 3, height: 4 }]);
  assert.deepEqual(computer.normaliseComputer({ active: false, targets: [{ kind: 'window', x: 1, y: 2, w: 3, h: 4 }] }).targets, []);
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
      this.placed = (this.placed || 0) + 1;
    }
    getBounds() {
      return this.bounds;
    }
    setBackgroundColor(color) {
      this.options.backgroundColor = color;
    }
    setOpacity(value) {
      this.opacity = value;
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
    { bounds: { x: 0, y: 0, width: 1920, height: 1080 }, workArea: { x: 0, y: 0, width: 1920, height: 1040 }, scaleFactor: 1 },
    { bounds: { x: 1920, y: 0, width: 1280, height: 1024 }, workArea: { x: 1920, y: 0, width: 1280, height: 1024 }, scaleFactor: 1 },
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
        // Windows at 100 %: the daemon's pixels are Electron's units.
        screenToDipRect: (window, rect) => ({ ...rect }),
        on: (event, fn) => screenListeners.set(event, fn),
      },
    },
  };
}

const windowLease = (extra = {}) => ({ active: true, paused: false, scope: 'window', targets: [{ kind: 'window', x: 2100, y: 100, w: 600, h: 400 }], ...extra });

test('a lease draws the signs around its target only and the shortcut stops it; its end takes them all away', () => {
  const fake = fakeElectron();
  const stops = [];
  const changes = [];
  const signs = startComputer({ electron: fake.electron, dir: '/app', getStrings: () => policy.strings('en'), sendStop: () => stops.push('stop'), onChange: (state) => changes.push(state), platform: 'win32', fadeMs: 0 });
  assert.equal(fake.windows.length, 0);

  signs.update(windowLease());
  const live = fake.windows.filter((w) => !w.destroyed);
  // Four strips around the one window on the second display, and the note; nothing on the first.
  assert.equal(live.length, 5);
  for (const window of live) {
    assert.equal(window.ignoresMouse, true);
    assert.equal(window.protected, true);
    assert.equal(window.level, 'screen-saver');
    assert.equal(window.options.focusable, false);
    assert.equal(window.options.skipTaskbar, true);
    // No resizable frame, so Windows imposes no minimum size on a 3-pixel strip.
    assert.equal(window.options.thickFrame, false);
    assert.equal(window.options.minWidth, 0);
    assert.equal(window.options.minHeight, 0);
  }
  const strips = live.filter((w) => !w.file);
  assert.ok(strips.every((w) => w.options.transparent === false && w.bounds.x >= 1920));
  assert.deepEqual(strips[0].bounds, { x: 2097, y: 97, width: 606, height: 3 });
  const note = live.find((w) => w.file);
  assert.match(note.file, /pages[\\/]computer\.html$/);
  assert.equal(note.query.text, 'Daedalus is controlling the computer — Ctrl+Alt+Esc to stop');
  // At the top centre of the window's display, not the primary one.
  assert.ok(note.bounds.x > 1920 && note.bounds.y === 10);
  assert.deepEqual([...fake.registered.keys()], ['Control+Alt+Escape']);
  assert.deepEqual(changes.at(-1), { active: true, paused: false, scope: 'window', chord: 'Control+Alt+Escape' });

  // The same lease said again (the launcher's answer after a Stop that failed) redraws nothing.
  signs.update(windowLease());
  assert.equal(fake.windows.length, 5);

  // The window moved: the same strips follow it, none made anew.
  signs.update(windowLease({ targets: [{ kind: 'window', x: 2200, y: 150, w: 600, h: 400 }] }));
  assert.equal(fake.windows.length, 5);
  assert.deepEqual(strips[0].bounds, { x: 2197, y: 147, width: 606, height: 3 });

  fake.registered.get('Control+Alt+Escape')();
  assert.deepEqual(stops, ['stop']);

  // Paused by the operator's own hand: the signs stay, in the pause's colour and words.
  signs.update(windowLease({ paused: true, targets: [{ kind: 'window', x: 2200, y: 150, w: 600, h: 400 }] }));
  const paused = fake.windows.filter((w) => !w.destroyed);
  assert.equal(paused.length, 5);
  assert.ok(paused.filter((w) => !w.file).every((w) => w.options.backgroundColor === '#c27c0e'));
  assert.equal(paused.find((w) => w.file).query.paused, '1');

  signs.update({ active: false });
  assert.equal(fake.windows.filter((w) => !w.destroyed).length, 0);
  assert.equal(fake.registered.size, 0);
  assert.deepEqual(changes.at(-1), { active: false, paused: false, scope: '', chord: '' });
  // A Stop with no lease is a Stop of nothing and is not sent.
  signs.stop();
  assert.deepEqual(stops, ['stop']);
});

test('the whole desktop is bordered display by display, a display added gets its own, and the launcher going away clears everything', () => {
  const fake = fakeElectron();
  const signs = startComputer({ electron: fake.electron, dir: '/app', getStrings: () => policy.strings('ru'), sendStop: () => {}, onChange: () => {}, platform: 'linux', fadeMs: 0 });
  const everyDisplay = () => fake.displays.map((d) => ({ kind: 'display', x: d.bounds.x, y: d.bounds.y, w: d.bounds.width, h: d.bounds.height }));
  signs.update({ active: true, scope: 'desktop', targets: everyDisplay() });
  // Linux: the border only (see computerPlan).
  assert.equal(fake.windows.filter((w) => !w.destroyed).length, 8);
  fake.displays.push({ bounds: { x: 3200, y: 0, width: 800, height: 600 }, workArea: { x: 3200, y: 0, width: 800, height: 600 } });
  signs.update({ active: true, scope: 'desktop', targets: everyDisplay() });
  assert.equal(fake.windows.filter((w) => !w.destroyed).length, 12);
  // One display of three leased: four strips, on that display.
  signs.update({ active: true, scope: 'display', targets: [everyDisplay()[2]] });
  const live = fake.windows.filter((w) => !w.destroyed);
  assert.equal(live.length, 4);
  assert.ok(live.every((w) => w.bounds.x >= 3200));
  signs.reset();
  assert.equal(fake.windows.filter((w) => !w.destroyed).length, 0);
  assert.equal(fake.registered.size, 0);
});
