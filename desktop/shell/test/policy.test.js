'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const policy = require('../policy');

test('the launcher and the app stay in the window; nothing else does', () => {
  for (const url of ['http://127.0.0.1:8770/setup', 'http://localhost:48765/app/', 'http://[::1]:8770/']) {
    assert.equal(policy.isInternal(url), true, url);
  }
  for (const url of ['https://github.com/anchor-inference/daedalus', 'http://127.0.0.1.example.com/', 'file:///etc/passwd', 'javascript:alert(1)', 'not a url']) {
    assert.equal(policy.isInternal(url), false, url);
  }
});

test('only web pages and mail addresses are handed to the system', () => {
  assert.equal(policy.isExternal('https://example.com/'), true);
  assert.equal(policy.isExternal('mailto:someone@example.com'), true);
  for (const url of ['file:///C:/Windows/System32/calc.exe', 'javascript:alert(1)', 'ms-settings:', 'daedalus://open/x', '']) {
    assert.equal(policy.isExternal(url), false, url);
  }
});

test("the shell's own pages are its pages folder and nothing beside it", () => {
  const dir = path.resolve('/opt/Daedalus/resources/app.asar');
  assert.equal(policy.isOwnFile(pathToFileURL(path.join(dir, 'pages', 'message.html')).href, dir), true);
  assert.equal(policy.isOwnFile(pathToFileURL(path.join(dir, 'main.js')).href, dir), false);
  assert.equal(policy.isOwnFile(pathToFileURL(path.join(dir, 'pages', '..', '..', 'x.html')).href, dir), false);
  assert.equal(policy.isOwnFile('http://127.0.0.1:8770/', dir), false);
});

test('a link is found wherever the system put it in the arguments', () => {
  assert.equal(policy.linkFrom(['Daedalus.exe', '--flag', 'daedalus://open/abc']), 'daedalus://open/abc');
  assert.equal(policy.linkFrom(['Daedalus.exe', 'DAEDALUS://open/abc']), 'DAEDALUS://open/abc');
  assert.equal(policy.linkFrom(['Daedalus.exe']), null);
});

test("a line that is not one of the launcher's events is ignored", () => {
  assert.deepEqual(policy.parseEvent('{"event":"show","url":"http://127.0.0.1:1/"}'), { event: 'show', url: 'http://127.0.0.1:1/' });
  for (const line of ['', 'hello', '[]', '{"url":"x"}', '{"event":3}']) {
    assert.equal(policy.parseEvent(line), null, line);
  }
});

test('a window reopens where it was only when that is still on a screen', () => {
  const screen = [{ x: 0, y: 0, width: 1920, height: 1080 }];
  assert.deepEqual(policy.restoreBounds(null, screen), { bounds: { width: 1180, height: 820 }, maximized: false });
  const there = policy.restoreBounds({ bounds: { x: 100, y: 50, width: 1000, height: 700 }, maximized: true }, screen);
  assert.deepEqual(there, { bounds: { x: 100, y: 50, width: 1000, height: 700 }, maximized: true });
  const unplugged = policy.restoreBounds({ bounds: { x: 3000, y: 50, width: 1000, height: 700 } }, screen);
  assert.deepEqual(unplugged.bounds, { width: 1000, height: 700 });
  assert.deepEqual(policy.restoreBounds({ bounds: { x: 0, y: 0, width: 10, height: 10 } }, screen).bounds, { x: 0, y: 0, width: 480, height: 400 });
});

test('the Linux sandbox is turned off only where it cannot start', () => {
  const restricted = (file) => (file.endsWith('apparmor_restrict_unprivileged_userns') ? '1' : null);
  const open = () => null;
  assert.equal(policy.sandboxUnavailable({}, open, () => false, '/home/someone/Daedalus/daedalus'), false);
  assert.equal(policy.sandboxUnavailable({ APPIMAGE: '/home/someone/Daedalus.AppImage' }, restricted, () => false, '/tmp/.mount_x/daedalus'), true);
  // The .deb's AppArmor profile allows what the restriction forbids.
  assert.equal(policy.sandboxUnavailable({}, restricted, () => false, '/opt/Daedalus/daedalus'), false);
  // A folder unpacked by hand keeps the sandbox when its helper is setuid, and loses it when not.
  assert.equal(policy.sandboxUnavailable({}, restricted, () => true, '/home/someone/Daedalus/daedalus'), false);
  assert.equal(policy.sandboxUnavailable({}, restricted, () => false, '/home/someone/Daedalus/daedalus'), true);
});

test('every message exists in both languages', () => {
  assert.deepEqual(Object.keys(policy.dictionary.ru).sort(), Object.keys(policy.dictionary.en).sort());
  assert.equal(policy.strings('ru-RU'), policy.dictionary.ru);
  assert.equal(policy.strings('en-GB'), policy.dictionary.en);
  assert.equal(policy.strings(undefined), policy.dictionary.en);
});
