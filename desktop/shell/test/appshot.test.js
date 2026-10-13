'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const appshot = require('../appshot');

function image(name, { empty = false, width = 100, height = 50 } = {}) {
  return {
    isEmpty: () => empty,
    getSize: () => ({ width, height }),
    toPNG: () => Buffer.from(`png:${name}`),
    toDataURL: () => `data:image/png;base64,${Buffer.from(name).toString('base64')}`,
  };
}

const left = { id: 11, size: { width: 1920, height: 1080 }, scaleFactor: 1, workArea: { x: 0, y: 0, width: 1920, height: 1040 } };
const right = { id: 22, size: { width: 1280, height: 800 }, scaleFactor: 2, workArea: { x: 1920, y: 0, width: 1280, height: 800 } };

function deps({ sources = [], cursorOn = right, platform = 'linux', access = ['granted'], windows = [] } = {}) {
  const asked = [];
  const statuses = [...access];
  return {
    asked,
    platform,
    now: () => new Date(2026, 9, 13, 9, 5, 7),
    screen: {
      getCursorScreenPoint: () => ({ x: cursorOn.workArea.x + 10, y: 10 }),
      getDisplayNearestPoint: () => cursorOn,
      getAllDisplays: () => [left, right],
    },
    desktopCapturer: {
      getSources: async (options) => {
        asked.push(options);
        return options.types[0] === 'screen' ? sources : windows;
      },
    },
    systemPreferences: {
      getMediaAccessStatus: () => (statuses.length > 1 ? statuses.shift() : statuses[0]),
    },
  };
}

test('the screen under the pointer is the one photographed, at its own pixels', async () => {
  const d = deps({ sources: [{ id: 'screen:1', display_id: '11', thumbnail: image('left') }, { id: 'screen:2', display_id: '22', thumbnail: image('right', { width: 2560, height: 1600 }) }] });
  const shot = await appshot.captureScreen(d);
  assert.equal(shot.ok, true);
  assert.equal(Buffer.from(shot.bytes).toString(), 'png:right');
  assert.equal(shot.name, 'appshot-20261013-090507.png');
  assert.deepEqual([shot.width, shot.height], [2560, 1600]);
  assert.deepEqual(d.asked[0].thumbnailSize, { width: 2560, height: 1600 });
});

test('without display ids the order of the displays decides, and one screen is that screen', () => {
  const sources = [{ id: 'a', display_id: '', thumbnail: image('a') }, { id: 'b', display_id: '', thumbnail: image('b') }];
  assert.equal(appshot.pickScreenSource(sources, right, [left, right]).id, 'b');
  assert.equal(appshot.pickScreenSource([sources[0]], right, [left, right]).id, 'a');
  assert.equal(appshot.pickScreenSource([], right, [left, right]), null);
  // Ids that name other displays are not guessed past.
  assert.equal(appshot.pickScreenSource([{ id: 'x', display_id: '7', thumbnail: image('x') }, { id: 'y', display_id: '8', thumbnail: image('y') }], right, [left, right]), null);
});

test('a dialog closed on Wayland and an empty picture are reported, not attached', async () => {
  assert.deepEqual(await appshot.captureScreen(deps({ sources: [] })), { ok: false, error: 'cancelled', detail: '' });
  const blank = await appshot.captureScreen(deps({ sources: [{ id: 's', display_id: '22', thumbnail: image('s', { empty: true }) }] }));
  assert.equal(blank.error, 'empty');
  const broken = deps();
  broken.desktopCapturer.getSources = async () => {
    throw new Error('Failed to get sources.');
  };
  assert.deepEqual(await appshot.captureScreen(broken), { ok: false, error: 'failed', detail: 'Failed to get sources.' });
});

test('on a Mac a refused permission stops the capture before it starts', async () => {
  const d = deps({ platform: 'darwin', access: ['denied'], sources: [{ id: 's', display_id: '22', thumbnail: image('s') }] });
  assert.deepEqual(await appshot.captureScreen(d), { ok: false, error: 'permission', detail: '' });
  assert.equal(d.asked.length, 0);
});

test('on a Mac never asked before, the first capture is the question and is not used', async () => {
  const d = deps({ platform: 'darwin', access: ['not-determined', 'not-determined'], sources: [{ id: 's', display_id: '22', thumbnail: image('wallpaper') }] });
  assert.equal((await appshot.captureScreen(d)).error, 'permission');
  assert.equal(d.asked.length, 1);
  const granted = deps({ platform: 'darwin', access: ['granted'], sources: [{ id: 's', display_id: '22', thumbnail: image('s') }] });
  assert.equal((await appshot.captureScreen(granted)).ok, true);
});

test("the window list leaves out the application's own windows and empty ones", async () => {
  const windows = [
    { id: 'window:1:0', name: 'Editor', thumbnail: image('editor') },
    { id: 'window:2:0', name: 'Daedalus', thumbnail: image('own') },
    { id: 'window:3:0', name: 'Minimised', thumbnail: image('min', { empty: true }) },
  ];
  const d = deps({ windows });
  const listed = await appshot.listWindows(d, ['window:2:0']);
  assert.equal(listed.ok, true);
  assert.deepEqual(listed.windows.map((w) => w.name), ['Editor']);
  assert.match(listed.windows[0].thumbnail, /^data:image\/png;base64,/);
  assert.equal(d.asked[0].fetchWindowIcons, false);
});

test('a chosen window is photographed by its id, and one that closed meanwhile is reported', async () => {
  const d = deps({ windows: [{ id: 'window:1:0', name: 'Editor', thumbnail: image('editor') }] });
  const shot = await appshot.captureWindow(d, 'window:1:0');
  assert.equal(Buffer.from(shot.bytes).toString(), 'png:editor');
  // As large as the largest display can show it.
  assert.deepEqual(d.asked[0].thumbnailSize, { width: 2560, height: 1600 });
  assert.equal((await appshot.captureWindow(d, 'window:9:0')).error, 'gone');
  assert.equal((await appshot.captureWindow(d, 'screen:1')).error, 'failed');
});
