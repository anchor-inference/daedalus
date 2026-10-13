'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const desk = require('../desk');
const policy = require('../policy');

test('a shortcut needs a modifier, except a function key, and empty means off', () => {
  for (const ok of ['Alt+Space', 'Alt+Shift+Space', 'CommandOrControl+Shift+K', 'Option+Space', 'F9', 'Ctrl+Alt+/', 'Super+Return', '']) {
    assert.equal(desk.validAccelerator(ok), true, ok);
  }
  for (const bad of ['Space', 'K', 'Alt', 'Alt+', 'Alt++', 'Alt+Alt+K', 'Hyper+K', 'Alt+Space+K', 'Alt+NotAKey', 42, null, 'Alt+' + 'x'.repeat(80)]) {
    assert.equal(desk.validAccelerator(bad), false, String(bad));
  }
});

test('saved settings keep what is valid and fall back to the defaults for the rest', () => {
  assert.deepEqual(desk.normaliseSettings(null), { quickChat: 'Alt+Space', appshot: 'Alt+Shift+Space', hideToTray: true });
  assert.deepEqual(desk.normaliseSettings({ quickChat: 'Ctrl+Shift+J', appshot: 'Space', hideToTray: false }), { quickChat: 'Ctrl+Shift+J', appshot: 'Alt+Shift+Space', hideToTray: false });
  assert.deepEqual(desk.normaliseSettings({ quickChat: '', hideToTray: 'yes' }), { quickChat: '', appshot: 'Alt+Shift+Space', hideToTray: true });
});

test('a change is refused field by field, and one key cannot do both jobs', () => {
  const current = desk.normaliseSettings(null);
  const bad = desk.mergeSettings(current, { quickChat: 'Q', hideToTray: false });
  assert.deepEqual(bad.errors, { quickChat: 'invalid' });
  assert.equal(bad.settings.quickChat, 'Alt+Space');
  assert.equal(bad.settings.hideToTray, false);
  const same = desk.mergeSettings(current, { appshot: 'alt+space' });
  assert.deepEqual(same.errors, { appshot: 'duplicate' });
  assert.equal(same.settings.appshot, 'Alt+Shift+Space');
  const moved = desk.mergeSettings(current, { quickChat: 'Alt+Shift+Space' });
  assert.deepEqual(moved.errors, { quickChat: 'duplicate' });
  assert.equal(moved.settings.quickChat, 'Alt+Space');
  assert.deepEqual(desk.mergeSettings(current, { quickChat: 'Ctrl+Shift+Space' }).errors, {});
});

function fakeShortcuts({ taken = [], throws = [] } = {}) {
  const registered = new Map();
  return {
    registered,
    register(accelerator, callback) {
      if (throws.includes(accelerator)) throw new Error('conversion failure');
      if (taken.includes(accelerator)) return false;
      registered.set(accelerator, callback);
      return true;
    },
    unregister(accelerator) {
      registered.delete(accelerator);
    },
  };
}

test('each shortcut says whether it works, and a taken one is reported, not fatal', () => {
  const shortcuts = fakeShortcuts({ taken: ['Alt+Shift+Space'] });
  const held = new Set();
  let opened = 0;
  const report = desk.registerShortcuts(shortcuts, desk.normaliseSettings(null), { quickChat: () => opened++, appshot: () => {} }, held);
  assert.deepEqual(report.quickChat, { accelerator: 'Alt+Space', ok: true, reason: '' });
  assert.deepEqual(report.appshot, { accelerator: 'Alt+Shift+Space', ok: false, reason: 'taken' });
  shortcuts.registered.get('Alt+Space')();
  assert.equal(opened, 1);
  assert.deepEqual([...held], ['Alt+Space']);
});

test('changing a shortcut releases the old one and leaves nothing behind', () => {
  const shortcuts = fakeShortcuts({ throws: ['Ctrl+F13'] });
  const held = new Set();
  desk.registerShortcuts(shortcuts, desk.normaliseSettings(null), { quickChat() {}, appshot() {} }, held);
  assert.deepEqual([...shortcuts.registered.keys()].sort(), ['Alt+Shift+Space', 'Alt+Space']);
  const report = desk.registerShortcuts(shortcuts, { quickChat: 'Ctrl+F13', appshot: '', hideToTray: true }, { quickChat() {}, appshot() {} }, held);
  assert.deepEqual([...shortcuts.registered.keys()], []);
  assert.equal(report.quickChat.reason, 'taken');
  assert.equal(report.appshot.reason, 'off');
  assert.equal(held.size, 0);
});

test('a Wayland session is told apart from X11', () => {
  assert.equal(desk.waylandSession({ XDG_SESSION_TYPE: 'wayland' }), true);
  assert.equal(desk.waylandSession({ XDG_SESSION_TYPE: 'x11', WAYLAND_DISPLAY: 'wayland-0' }), false);
  assert.equal(desk.waylandSession({ WAYLAND_DISPLAY: 'wayland-0' }), true);
  assert.equal(desk.waylandSession({}), false);
});

test('the tray note names plain GNOME and a session with no bus, and nothing elsewhere', () => {
  const bus = { DBUS_SESSION_BUS_ADDRESS: 'unix:path=/run/user/1000/bus' };
  assert.equal(desk.trayNote('linux', { ...bus, XDG_CURRENT_DESKTOP: 'GNOME' }), 'gnome');
  assert.equal(desk.trayNote('linux', { ...bus, XDG_CURRENT_DESKTOP: 'ubuntu:GNOME' }), '');
  assert.equal(desk.trayNote('linux', { ...bus, XDG_CURRENT_DESKTOP: 'KDE' }), '');
  assert.equal(desk.trayNote('linux', { XDG_CURRENT_DESKTOP: 'KDE' }), 'unavailable');
  assert.equal(desk.trayNote('win32', {}), '');
  assert.equal(desk.trayNote('darwin', {}), '');
});

test('closing hides the window only with a tray to come back from, and never while quitting', () => {
  assert.equal(desk.hideOnClose({ hideToTray: true, trayReady: true, quitting: false }), true);
  assert.equal(desk.hideOnClose({ hideToTray: true, trayReady: false, quitting: false }), false);
  assert.equal(desk.hideOnClose({ hideToTray: false, trayReady: true, quitting: false }), false);
  assert.equal(desk.hideOnClose({ hideToTray: true, trayReady: true, quitting: true }), false);
});

test('the tray shows what waits before what runs, in both languages', () => {
  const en = policy.strings('en');
  const ru = policy.strings('ru');
  assert.deepEqual(desk.trayView(null, en), { icon: 'idle', summary: 'Nothing running', tooltip: 'Daedalus — Nothing running', title: '', status: { running: 0, waiting: 0, companion: { present: false, paused: false } } });
  const busy = desk.trayView({ running: 2, waiting: 1 }, en);
  assert.equal(busy.icon, 'waiting');
  assert.equal(busy.summary, 'Agents running: 2 · Waiting for you: 1');
  assert.equal(busy.title, '1');
  assert.equal(desk.trayView({ running: 3 }, ru).summary, 'Агентов в работе: 3');
  assert.equal(desk.trayView({ running: 3 }, ru).icon, 'running');
  // A page's word is not trusted to be a count.
  assert.equal(desk.trayView({ running: 'lots', waiting: -4 }, en).icon, 'idle');
});

test("the tray's menu offers Pause only when there is a companion to pause", () => {
  const en = policy.strings('en');
  const settings = desk.normaliseSettings(null);
  const calls = [];
  const actions = Object.fromEntries(['open', 'quickChat', 'appshot', 'pauseCompanion', 'resumeCompanion', 'quit'].map((name) => [name, () => calls.push(name)]));
  const labels = (status) => desk.trayTemplate(desk.trayView(status, en), en, settings, actions).filter((item) => item.label).map((item) => item.label);
  assert.deepEqual(labels({}), ['Nothing running', 'Open Daedalus', 'Quick Chat', 'Appshot', 'Quit Daedalus']);
  assert.deepEqual(labels({ companion: { paused: false } }), ['Nothing running', 'Open Daedalus', 'Quick Chat', 'Appshot', 'Pause the companion', 'Quit Daedalus']);
  assert.ok(labels({ companion: { paused: true } }).includes('Resume the companion'));
  const menu = desk.trayTemplate(desk.trayView({ companion: {} }, en), en, settings, actions);
  assert.equal(menu.find((item) => item.label === 'Quick Chat').accelerator, 'Alt+Space');
  // The menu shows the key but must not register it a second time: the global shortcut owns it.
  assert.equal(menu.find((item) => item.label === 'Quick Chat').registerAccelerator, false);
  menu.find((item) => item.label === 'Pause the companion').click();
  assert.deepEqual(calls, ['pauseCompanion']);
});

test('every word of the tray is in both dictionaries', () => {
  assert.deepEqual(Object.keys(policy.dictionary.ru).sort(), Object.keys(policy.dictionary.en).sort());
});

test('a second start can ask for Quick Chat or an appshot', () => {
  assert.equal(desk.actionFrom(['daedalus', '--quick-chat']), 'quickChat');
  assert.equal(desk.actionFrom(['Daedalus.exe', '--appshot']), 'appshot');
  assert.equal(desk.actionFrom(['daedalus', 'daedalus://open/x']), null);
});

test('Quick Chat opens centred on the screen it was asked from, and stays on it', () => {
  assert.deepEqual(desk.quickBounds({ x: 1920, y: 0, width: 2560, height: 1400 }, 680, 160), { x: 1920 + 940, y: 280, width: 680, height: 160 });
  const small = desk.quickBounds({ x: 0, y: 0, width: 500, height: 300 }, 680, 560);
  assert.equal(small.width, 468);
  assert.ok(small.y + small.height <= 300);
  assert.equal(desk.quickHeight(10), desk.QUICK_MIN_HEIGHT);
  assert.equal(desk.quickHeight(9000), desk.QUICK_MAX_HEIGHT);
  assert.equal(desk.quickHeight('x'), desk.QUICK_MIN_HEIGHT);
});

test('"open" goes only to an address inside the app', () => {
  assert.equal(desk.appPath('/app/agents/abc'), '/app/agents/abc');
  assert.equal(desk.appPath('/app/agents/abc?with=d'), '/app/agents/abc?with=d');
  for (const bad of ['https://example.com/app/', '//example.com/app/', '/api/sessions', '/app/../api/x', '/app\\..\\x', null]) {
    assert.equal(desk.appPath(bad), null, String(bad));
  }
});

test("the app's origin is read off addresses under /app on loopback only", () => {
  assert.equal(desk.appOrigin('http://127.0.0.1:48765/app/agents'), 'http://127.0.0.1:48765');
  assert.equal(desk.appOrigin('http://localhost:8765/app'), 'http://localhost:8765');
  assert.equal(desk.appOrigin('http://127.0.0.1:8770/setup'), null);
  assert.equal(desk.appOrigin('https://example.com/app/'), null);
  assert.equal(desk.appOrigin('file:///app/x'), null);
});
