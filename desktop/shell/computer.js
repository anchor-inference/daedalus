'use strict';

// What the shell does while the agent holds the screen, as decisions without Electron in them: the
// launcher says the lease changed ({"event":"computer"}), and from that follow the border around
// each display, the note saying how to stop, the global Stop shortcut and the tray's Stop item.
// computer-main.js is the Electron around it.
//
// The Stop shortcut is armed only while a lease is held. Held all the time it would take a chord
// from every other program on the machine for a feature most sessions never use, and a press with
// no lease would be a Stop of nothing.

// The chords tried for Stop, in order; the first the system lets this process have is the one. The
// same accelerator on every platform: Alt is Option on a Mac, where Cmd+Option+Esc is Force Quit
// and Ctrl+Option+Esc is free. KDE binds Ctrl+Alt+Esc to xkill, so there is a second choice.
const STOP_CHORDS = ['Control+Alt+Escape', 'Control+Alt+Shift+Escape'];

// The border's thickness in device-independent pixels: enough to be seen at the edge of the eye,
// thin enough to sit on a display's outermost pixels where nothing is ever aimed.
const BORDER = 4;

// The note's size. Wide enough for the longest translation on one line at the system's font.
const NOTE_WIDTH = 460;
const NOTE_HEIGHT = 34;
const NOTE_MARGIN = 8;

// normaliseComputer reads the launcher's event; anything it cannot read is no lease, so nothing is
// drawn on a guess.
function normaliseComputer(raw) {
  const value = raw && typeof raw === 'object' ? raw : {};
  const active = value.active === true;
  return {
    active,
    paused: active && value.paused === true,
    scope: active && typeof value.scope === 'string' ? value.scope : '',
  };
}

// computerPlan is what to show for a lease. The note is left out on Linux: the X11 backend of the
// desktop daemon finds the window under a point by the window manager's stacking list, where an
// always-on-top note is the topmost window, so a click the model aimed at what lay beneath it would
// be refused as a click on Daedalus. On Windows a click-through window is skipped by the system's
// own hit test, and on macOS the daemon lists only ordinary windows. The border stays everywhere: it
// lies on the outermost pixels of a display, where nothing is ever aimed.
function computerPlan(state, platform) {
  const computer = normaliseComputer(state);
  return {
    hotkey: computer.active,
    border: computer.active,
    note: computer.active && platform !== 'linux',
    trayStop: computer.active,
    paused: computer.paused,
  };
}

// edgeRects are the four strips of a display's border, in its own bounds: top, bottom, left, right.
// Four solid windows rather than one transparent window over the whole display: a transparent window
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

module.exports = { STOP_CHORDS, BORDER, NOTE_WIDTH, NOTE_HEIGHT, normaliseComputer, computerPlan, edgeRects, noteBounds, chordLabel, armStop, disarmStop };
