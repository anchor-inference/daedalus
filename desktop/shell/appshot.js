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

module.exports = { pickScreenSource, nativeSize, screenAccess, captureScreen, listWindows, captureWindow, fileName };
