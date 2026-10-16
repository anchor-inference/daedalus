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

// A picture that remembers what was done to it, for the region appshot's resize and cut.
function picture(name, width, height, steps = []) {
  return {
    isEmpty: () => false,
    getSize: () => ({ width, height }),
    toPNG: () => Buffer.from(`png:${name}:${steps.join('/')}`),
    resize: (size) => picture(name, size.width, size.height, [...steps, `resize ${size.width}x${size.height}`]),
    crop: (rect) => picture(name, rect.width, rect.height, [...steps, `crop ${rect.x},${rect.y} ${rect.width}x${rect.height}`]),
  };
}

const wide = { ...left, bounds: { x: 0, y: 0, width: 1920, height: 1080 } };
// A HiDPI display to the right: 1280×800 points, 2560×1600 pixels.
const sharp = { ...right, bounds: { x: 1920, y: 0, width: 1280, height: 800 } };

function regionDeps(sources) {
  const d = deps({ sources });
  d.screen.getAllDisplays = () => [wide, sharp];
  return d;
}

test('every display is photographed once, each brought to its own pixel size', async () => {
  // One request for both, at the largest pixel size; the 1080p screen comes back scaled up to fit it.
  const d = regionDeps([
    { id: 'screen:1', display_id: '11', thumbnail: picture('left', 2560, 1440) },
    { id: 'screen:2', display_id: '22', thumbnail: picture('right', 2560, 1600) },
  ]);
  const taken = await appshot.captureDisplays(d);
  assert.equal(taken.ok, true);
  assert.deepEqual(d.asked[0].thumbnailSize, { width: 2560, height: 1600 });
  assert.deepEqual(taken.shots.map((shot) => [shot.display.id, shot.image.getSize().width, shot.image.getSize().height]), [[11, 1920, 1080], [22, 2560, 1600]]);
  // A single source with no display id stands for one display, never for both.
  const lone = await appshot.captureDisplays(regionDeps([{ id: 'screen:0', display_id: '', thumbnail: picture('only', 1920, 1080) }]));
  assert.equal(lone.shots.length, 1);
});

test('a selection on a HiDPI display is cut at its pixels', async () => {
  const d = regionDeps([
    { id: 'screen:1', display_id: '11', thumbnail: picture('left', 1920, 1080) },
    { id: 'screen:2', display_id: '22', thumbnail: picture('right', 2560, 1600) },
  ]);
  let offered = null;
  const shot = await appshot.captureRegion(d, async (shots) => {
    offered = shots.length;
    return { displayId: 22, rect: { x: 100, y: 50, width: 200, height: 100 }, offset: { x: 0, y: 0 } };
  });
  assert.equal(offered, 2);
  assert.equal(shot.ok, true);
  assert.equal(Buffer.from(shot.bytes).toString(), 'png:right:crop 200,100 400x200');
  assert.deepEqual([shot.width, shot.height], [400, 200]);
  assert.equal(shot.name, 'appshot-20261013-090507.png');
});

test('Esc, a click without a drag and a refused capture are not pictures', async () => {
  const sources = [{ id: 'screen:1', display_id: '11', thumbnail: picture('left', 1920, 1080) }];
  assert.deepEqual(await appshot.captureRegion(regionDeps(sources), async () => null), { ok: false, error: 'dismissed', detail: '' });
  const click = await appshot.captureRegion(regionDeps(sources), async () => ({ displayId: 11, rect: { x: 5, y: 5, width: 0, height: 0 }, offset: { x: 0, y: 0 } }));
  assert.equal(click.error, 'dismissed');
  let asked = false;
  const closed = await appshot.captureRegion(regionDeps([]), async () => { asked = true; return null; });
  assert.equal(closed.error, 'cancelled');
  assert.equal(asked, false, 'the overlay is never shown without pictures to draw on');
  const mac = regionDeps(sources);
  mac.platform = 'darwin';
  mac.systemPreferences.getMediaAccessStatus = () => 'denied';
  assert.equal((await appshot.captureRegion(mac, async () => { asked = true; return null; })).error, 'permission');
  assert.equal(asked, false);
});

test('the cut is clamped to the picture, rounded outwards, and follows a drag in any direction', () => {
  const bounds = { width: 1280, height: 800 };
  const image = { width: 1920, height: 1200 };
  // 150 %: a selection on fractional pixels keeps every pixel it touches.
  assert.deepEqual(appshot.cropRect({ x: 10.2, y: 10.2, width: 10, height: 10 }, bounds, image), { x: 15, y: 15, width: 16, height: 16 });
  // Dragged up and to the left, and past the edge.
  assert.deepEqual(appshot.cropRect({ x: 1300, y: 900, width: -100, height: -200 }, bounds, image), { x: 1800, y: 1050, width: 120, height: 150 });
  assert.equal(appshot.cropRect({ x: 2000, y: 0, width: 50, height: 50 }, bounds, image), null);
  // A window placed short of the display's corner: the page's pixels move with it, unscaled.
  assert.deepEqual(appshot.selectionOnDisplay({ x: 64, y: 40, width: 64, height: 40 }, { x: 0, y: 27 }), { x: 64, y: 67, width: 64, height: 40 });
  assert.equal(appshot.selectionOnDisplay({ x: 'a', y: 0, width: 1, height: 1 }, { x: 0, y: 0 }), null);
  assert.equal(appshot.selectionOnDisplay({ x: 0, y: 0, width: 1, height: 1 }, null), null);
});
