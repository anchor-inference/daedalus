'use strict';

// What the shell does while the agent holds the screen, as decisions without Electron in them: the
// launcher says the lease changed ({"event":"computer"}), and from that follow the border around
// what the lease covers, the note saying how to stop, the global Stop shortcut and the tray's Stop
// item. computer-main.js is the Electron around it.
//
// The Stop shortcut is armed only while a lease is held. Held all the time it would take a chord
// from every other program on the machine for a feature most sessions never use, and a press with
// no lease would be a Stop of nothing.

// The chords tried for Stop, in order; the first the system lets this process have is the one. The
// same accelerator on every platform: Alt is Option on a Mac, where Cmd+Option+Esc is Force Quit
// and Ctrl+Option+Esc is free. KDE binds Ctrl+Alt+Esc to xkill, so there is a second choice.
const STOP_CHORDS = ['Control+Alt+Escape', 'Control+Alt+Shift+Escape'];

// The border's thickness in device-independent pixels, the same on all four sides: thin enough to
// read as an outline around what the agent holds, the way a screen recorder marks what it records.
// On Windows and macOS the border is one transparent click-through window over the target with the
// outline drawn inside it (pages/frame.html). It was four solid strip windows there too, and on
// Windows they came out thick on the left and top and spilled off the right and bottom onto the
// next display: the system holds a top-level window to a minimum size whatever its frame, so a
// strip a few pixels wide grew to that minimum. Removing the resizable frame did not lift it. On
// Linux the strips stay: there a transparent window without a compositor is drawn black.
const BORDER = 2;

// The note's window. The pill inside sizes to its words; the window is wide enough for the longest
// translation on one line, and the transparent rest of it lets clicks through.
const NOTE_WIDTH = 560;
const NOTE_HEIGHT = 44;
const NOTE_MARGIN = 10;

// readTargets keeps the rectangles the launcher sent that are whole, finite and not empty.
function readTargets(raw) {
  if (!Array.isArray(raw)) return [];
  const out = [];
  for (const t of raw) {
    if (!t || (t.kind !== 'display' && t.kind !== 'window')) continue;
    const { x, y, w, h } = t;
    if (![x, y, w, h].every(Number.isFinite) || w <= 0 || h <= 0) continue;
    out.push({ kind: t.kind, x, y, width: w, height: h });
  }
  return out;
}

// normaliseComputer reads the launcher's event; anything it cannot read is no lease, so nothing is
// drawn on a guess.
function normaliseComputer(raw) {
  const value = raw && typeof raw === 'object' ? raw : {};
  const active = value.active === true;
  return {
    active,
    paused: active && value.paused === true,
    scope: active && typeof value.scope === 'string' ? value.scope : '',
    targets: active ? readTargets(value.targets) : [],
  };
}

// computerPlan is what to show for a lease. The note is left out on Linux: the X11 backend of the
// desktop daemon finds the window under a point by the window manager's stacking list, where an
// always-on-top note is the topmost window, so a click the model aimed at what lay beneath it would
// be refused as a click on Daedalus. On Windows a click-through window is skipped by the system's
// own hit test, and on macOS the daemon lists only ordinary windows. The border stays everywhere: it
// lies on a display's outermost pixels, or just outside the window the agent works in.
function computerPlan(state, platform) {
  const computer = normaliseComputer(state);
  return {
    hotkey: computer.active,
    border: computer.active,
    note: computer.active && platform !== 'linux',
    frame: computer.active && platform !== 'linux',
    trayStop: computer.active,
    paused: computer.paused,
  };
}

// toDip turns a rectangle in the desktop daemon's units into the shell's device-independent pixels.
// The daemon counts physical pixels on Windows (it is per-monitor DPI aware) and X11, and points on
// macOS, which are already Electron's units. On Windows each display has its own scale and Electron
// lays the displays out in its own way, so only Electron can convert: screenToDipRect, passed in as
// convert. On Linux Electron applies one scale to the whole screen. The result is widened to whole
// pixels, never narrowed, so a border drawn outside it never covers the window's edge.
function toDip(rect, platform, { convert, scale = 1 } = {}) {
  if (platform === 'win32' && typeof convert === 'function') {
    const r = convert({ x: Math.floor(rect.x), y: Math.floor(rect.y), width: Math.ceil(rect.width), height: Math.ceil(rect.height) });
    return { x: r.x, y: r.y, width: r.width, height: r.height };
  }
  const s = platform === 'linux' && scale > 0 ? scale : 1;
  const x0 = Math.floor(rect.x / s);
  const y0 = Math.floor(rect.y / s);
  return { x: x0, y: y0, width: Math.ceil((rect.x + rect.width) / s) - x0, height: Math.ceil((rect.y + rect.height) / s) - y0 };
}

function overlap(a, b) {
  const w = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x);
  const h = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y);
  return w > 0 && h > 0 ? w * h : 0;
}

// displayOf is the Electron display holding the larger part of a rectangle, as the daemon decides
// where a window is; null when it is on none.
function displayOf(rect, displays) {
  let best = null;
  let area = 0;
  for (const d of displays) {
    const a = overlap(rect, d.bounds);
    if (a > area) {
      best = d;
      area = a;
    }
  }
  return best;
}

// frameRects are the outer edges of the border, one rectangle per target, in device-independent
// pixels. A display's border is that Electron display's own bounds: the daemon's rectangle only picks
// which display, so a rounding in the conversion cannot leave a gap or push the border off the edge.
// A window's border lies just outside the window, so the outline never sits on what the agent
// clicks; where the window meets the edge of its display (maximised, or pushed against it) the
// border comes inside instead of vanishing off the screen.
function frameRects(targets, displays, thickness = BORDER) {
  const frames = [];
  const seen = new Set();
  for (const target of targets) {
    const display = displayOf(target, displays);
    if (!display) continue;
    let outer;
    if (target.kind === 'display') {
      outer = { ...display.bounds };
    } else {
      outer = clamp(
        { x: target.x - thickness, y: target.y - thickness, width: target.width + 2 * thickness, height: target.height + 2 * thickness },
        display.bounds,
      );
    }
    const key = [outer.x, outer.y, outer.width, outer.height].join(',');
    if (outer.width <= 0 || outer.height <= 0 || seen.has(key)) continue;
    seen.add(key);
    frames.push(outer);
  }
  return frames;
}

// borderRects are the four strips of every frame, for Linux, where the border is drawn as solid
// strip windows. Every strip is the same thickness.
function borderRects(targets, displays, thickness = BORDER) {
  return frameRects(targets, displays, thickness).flatMap((outer) => edgeRects(outer, thickness));
}

function clamp(rect, bounds) {
  const x0 = Math.max(rect.x, bounds.x);
  const y0 = Math.max(rect.y, bounds.y);
  const x1 = Math.min(rect.x + rect.width, bounds.x + bounds.width);
  const y1 = Math.min(rect.y + rect.height, bounds.y + bounds.height);
  return { x: x0, y: y0, width: Math.max(0, x1 - x0), height: Math.max(0, y1 - y0) };
}

// noteDisplay is where the note goes: the display of the first target (the window, the leased
// display), else the primary one.
function noteDisplay(targets, displays, primary) {
  for (const target of targets) {
    const display = displayOf(target, displays);
    if (display) return display;
  }
  return primary;
}

// edgeRects are the four strips of a border just inside bounds: top, bottom, left, right. Four
// solid windows rather than one transparent window over the whole area: a transparent window
// without a compositor (plain X11) is drawn black, and that would black out the operator's screen.
function edgeRects(bounds, thickness = BORDER) {
  const t = Math.max(1, Math.min(thickness, Math.floor(bounds.width / 2), Math.floor(bounds.height / 2)));
  const { x, y, width, height } = bounds;
  return [
    { x, y, width, height: t },
    { x, y: y + height - t, width, height: t },
    { x, y: y + t, width: t, height: height - 2 * t },
    { x: x + width - t, y: y + t, width: t, height: height - 2 * t },
  ];
}

// noteBounds puts the note at the top centre of a display's work area, under the menu bar on a Mac
// and clear of a taskbar at the top on Windows.
function noteBounds(workArea, width = NOTE_WIDTH, height = NOTE_HEIGHT) {
  const w = Math.min(width, Math.max(160, workArea.width - 2 * NOTE_MARGIN));
  return {
    x: Math.round(workArea.x + (workArea.width - w) / 2),
    y: Math.round(workArea.y + NOTE_MARGIN),
    width: w,
    height,
  };
}

// chordLabel says a chord the way the platform's own menus do.
function chordLabel(accelerator, platform) {
  if (!accelerator) return '';
  const mac = platform === 'darwin';
  const names = mac
    ? { Control: '⌃', Alt: '⌥', Shift: '⇧', Escape: 'Esc' }
    : { Control: 'Ctrl', Alt: 'Alt', Shift: 'Shift', Escape: 'Esc' };
  return accelerator.split('+').map((part) => names[part] || part).join(mac ? '' : '+');
}

// armStop registers the first chord the system grants and returns it, or '' when every one is taken
// (the tray's item and the app's Stop still work then). A chord another program holds makes
// register return false, and on some systems throw; neither may keep the lease's other signs from
// showing.
function armStop(globalShortcut, handler, chords = STOP_CHORDS) {
  for (const chord of chords) {
    try {
      if (globalShortcut.register(chord, handler)) return chord;
    } catch {}
  }
  return '';
}

function disarmStop(globalShortcut, chord) {
  if (!chord) return;
  try {
    globalShortcut.unregister(chord);
  } catch {}
}

module.exports = {
  STOP_CHORDS, BORDER, NOTE_WIDTH, NOTE_HEIGHT, normaliseComputer, computerPlan, toDip, displayOf, frameRects, borderRects, noteDisplay, edgeRects, noteBounds, chordLabel, armStop, disarmStop,
};
