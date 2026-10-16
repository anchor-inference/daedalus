'use strict';

// The appshot: a picture of the screen under the pointer, or of one window, taken through Electron's
// desktopCapturer and handed to Quick Chat as a PNG to attach. Electron's modules are passed in
// (`deps`: screen, desktopCapturer, systemPreferences, platform) so that what is chosen and what is
// refused is tested without a display (test/appshot.test.js).
//
// macOS asks the operator before any program may read the screen, and until they allow it every
// capture comes back as the desktop picture alone — no error, just an empty-looking screen. So the
// permission is read before the capture and again after it (the first capture is what makes macOS
// ask), and a capture taken without it is reported as the permission, never attached.

// The order desktopCapturer lists screens in is the order screen.getAllDisplays() lists displays,
// which is the fallback when a source carries no display_id (some Linux setups leave it empty).
function pickScreenSource(sources, display, displays) {
  if (!Array.isArray(sources) || sources.length === 0) return null;
  const byId = sources.find((source) => source.display_id && String(source.display_id) === String(display.id));
  if (byId) return byId;
  const index = (displays || []).findIndex((item) => item.id === display.id);
  if (index >= 0 && index < sources.length && sources.every((source) => !source.display_id)) return sources[index];
  return sources.length === 1 ? sources[0] : null;
}

// The size the thumbnail is asked for: the display's own pixels, so the picture is as sharp as the
// screen. desktopCapturer scales down to fit and never up, so asking for more costs nothing.
function nativeSize(display) {
  const scale = display.scaleFactor || 1;
  return { width: Math.round(display.size.width * scale), height: Math.round(display.size.height * scale) };
}

function screenAccess(deps) {
  if (deps.platform !== 'darwin' || !deps.systemPreferences) return 'granted';
  try {
    return deps.systemPreferences.getMediaAccessStatus('screen');
  } catch {
    return 'unknown';
  }
}

function fileName(now) {
  const pad = (n) => String(n).padStart(2, '0');
  return `appshot-${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}.png`;
}

function picture(image, now) {
  const size = image.getSize();
  return { ok: true, name: fileName(now), type: 'image/png', bytes: new Uint8Array(image.toPNG()), width: size.width, height: size.height };
}

function refused(error, detail) {
  return { ok: false, error, detail: detail || '' };
}

async function guarded(deps, take) {
  const before = screenAccess(deps);
  if (before === 'denied' || before === 'restricted') return refused('permission');
  let result;
  try {
    result = await take();
  } catch (err) {
    return refused('failed', String((err && err.message) || err));
  }
  // The capture just made is what asks on a Mac whose operator was never asked; what it returned
  // before they answered is the wallpaper, so it is not used.
  if (deps.platform === 'darwin' && screenAccess(deps) !== 'granted') return refused('permission');
  return result;
}

// captureScreen photographs the display the pointer is on.
async function captureScreen(deps) {
  return guarded(deps, async () => {
    const point = deps.screen.getCursorScreenPoint();
    const display = deps.screen.getDisplayNearestPoint(point);
    const displays = deps.screen.getAllDisplays();
    const sources = await deps.desktopCapturer.getSources({ types: ['screen'], thumbnailSize: nativeSize(display) });
    // A Wayland desktop asks through its portal, and an operator who closed that dialog sent nothing.
    if (!sources || sources.length === 0) return refused('cancelled');
    const source = pickScreenSource(sources, display, displays);
    if (!source || source.thumbnail.isEmpty()) return refused('empty');
    return picture(source.thumbnail, deps.now ? deps.now() : new Date());
  });
}

// listWindows offers the other programs' windows as small thumbnails to choose from. The shell's
// own windows are left out by their media source ids: Quick Chat would otherwise offer itself.
async function listWindows(deps, ownIds) {
  return guarded(deps, async () => {
    const sources = await deps.desktopCapturer.getSources({ types: ['window'], thumbnailSize: { width: 320, height: 200 }, fetchWindowIcons: false });
    const own = new Set(ownIds || []);
    const windows = (sources || [])
      .filter((source) => !own.has(source.id) && !source.thumbnail.isEmpty())
      .map((source) => ({ id: source.id, name: String(source.name || ''), thumbnail: source.thumbnail.toDataURL() }));
    return { ok: true, windows };
  });
}

// captureWindow photographs the window chosen from that list, at up to the largest display's size.
async function captureWindow(deps, id) {
  if (typeof id !== 'string' || !id.startsWith('window:')) return refused('failed', 'not a window');
  return guarded(deps, async () => {
    const largest = deps.screen.getAllDisplays().reduce((best, display) => {
      const size = nativeSize(display);
      return size.width * size.height > best.width * best.height ? size : best;
    }, { width: 1280, height: 800 });
    const sources = await deps.desktopCapturer.getSources({ types: ['window'], thumbnailSize: largest, fetchWindowIcons: false });
    const source = (sources || []).find((item) => item.id === id);
    if (!source) return refused('gone');
    if (source.thumbnail.isEmpty()) return refused('empty');
    return picture(source.thumbnail, deps.now ? deps.now() : new Date());
  });
}

// The region appshot works on a still: every display is photographed first, the overlay shows those
// pictures under the selection, and the rectangle is cut out of the very picture the operator drew on.
// Capturing after the selection instead would need a transparent overlay (which a Linux desktop
// without a compositor draws black) and would photograph whatever moved in the meantime.

// largestNativeSize is what one getSources call asks for so that every display comes back at least
// as sharp as itself: the call takes one size for all the screens it returns.
function largestNativeSize(displays) {
  return displays.reduce((best, display) => {
    const size = nativeSize(display);
    return { width: Math.max(best.width, size.width), height: Math.max(best.height, size.height) };
  }, { width: 1, height: 1 });
}

// captureDisplays photographs every display, each at its own pixel size. A smaller display answered at
// the largest one's size was scaled up to fit it; it is brought back down so the cut-out is the
// screen's own pixels and not an enlargement of them. One source never stands for two displays: the
// one-source fallback of pickScreenSource would otherwise show the same picture on every screen.
async function captureDisplays(deps) {
  return guarded(deps, async () => {
    const displays = deps.screen.getAllDisplays();
    const sources = await deps.desktopCapturer.getSources({ types: ['screen'], thumbnailSize: largestNativeSize(displays) });
    if (!sources || sources.length === 0) return refused('cancelled');
    const used = new Set();
    const shots = [];
    for (const display of displays) {
      const source = pickScreenSource(sources, display, displays);
      if (!source || used.has(source.id) || source.thumbnail.isEmpty()) continue;
      used.add(source.id);
      const native = nativeSize(display);
      const size = source.thumbnail.getSize();
      const image = size.width > native.width ? source.thumbnail.resize({ width: native.width, height: native.height, quality: 'best' }) : source.thumbnail;
      shots.push({ display, image });
    }
    if (shots.length === 0) return refused('empty');
    return { ok: true, shots };
  });
}

// selectionOnDisplay turns the rectangle the overlay reports, in its page's CSS pixels, into display
// points relative to the display's top-left corner. The overlay's page is the shell's own at zoom 1,
// so a CSS pixel is a point; what can differ is where the window actually sits. A window manager may
// place it a little short of the display (Xvfb without one makes it 1919 wide on a 1920 screen), and
// scaling by the page's size then stretched every cut by a pixel. `offset` is the window's content
// origin measured from the display's origin when the answer came.
function selectionOnDisplay(rect, offset) {
  const numbers = [rect && rect.x, rect && rect.y, rect && rect.width, rect && rect.height, offset && offset.x, offset && offset.y];
  if (!numbers.every((n) => typeof n === 'number' && Number.isFinite(n))) return null;
  return { x: rect.x + offset.x, y: rect.y + offset.y, width: rect.width, height: rect.height };
}

// cropRect is the selection in the picture's own pixels. The ratio of the picture to the display is
// measured, not taken from scaleFactor: desktopCapturer may hand back a picture of any size that fits
// what was asked, and on a 150 % display the two differ by rounding. The edges are rounded outwards,
// so a selection that touches a pixel keeps it, and clamped to the picture.
function cropRect(selection, bounds, imageSize) {
  if (!selection || !(bounds.width > 0) || !(bounds.height > 0)) return null;
  const sx = imageSize.width / bounds.width;
  const sy = imageSize.height / bounds.height;
  const left = Math.max(0, Math.min(selection.x, selection.x + selection.width));
  const top = Math.max(0, Math.min(selection.y, selection.y + selection.height));
  const right = Math.min(bounds.width, Math.max(selection.x, selection.x + selection.width));
  const bottom = Math.min(bounds.height, Math.max(selection.y, selection.y + selection.height));
  const x = Math.max(0, Math.floor(left * sx));
  const y = Math.max(0, Math.floor(top * sy));
  const width = Math.min(imageSize.width, Math.ceil(right * sx)) - x;
  const height = Math.min(imageSize.height, Math.ceil(bottom * sy)) - y;
  return width >= 1 && height >= 1 ? { x, y, width, height } : null;
}

// captureRegion photographs the displays, lets `choose` (the overlay) ask the operator for a
// rectangle, and cuts it out. `choose` gets the shots and answers { displayId, rect, offset } or
// null. Esc, and a click without a drag, are 'dismissed': the operator changed their mind, which
// Quick Chat says nothing about.
async function captureRegion(deps, choose) {
  const taken = await captureDisplays(deps);
  if (!taken.ok) return taken;
  let chosen;
  try {
    chosen = await choose(taken.shots);
  } catch (err) {
    return refused('failed', String((err && err.message) || err));
  }
  if (!chosen) return refused('dismissed');
  const shot = taken.shots.find((item) => String(item.display.id) === String(chosen.displayId));
  if (!shot) return refused('failed', 'the display is gone');
  const selection = selectionOnDisplay(chosen.rect, chosen.offset);
  const rect = cropRect(selection, shot.display.bounds, shot.image.getSize());
  if (!rect) return refused('dismissed');
  return picture(shot.image.crop(rect), deps.now ? deps.now() : new Date());
}

module.exports = {
  pickScreenSource,
  nativeSize,
  screenAccess,
  captureScreen,
  listWindows,
  captureWindow,
  fileName,
  largestNativeSize,
  captureDisplays,
  selectionOnDisplay,
  cropRect,
  captureRegion,
};
