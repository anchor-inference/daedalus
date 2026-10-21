'use strict';

// The signs on the desktop while the agent holds it: a thin coloured border around what the lease
// covers (the window, the app's windows, the display, every display), a note at the top of that
// display saying how to stop, and the global Stop shortcut. The decisions are in computer.js; this
// file is the Electron around them. The launcher tells the shell when the lease or its target
// changes ({"event":"computer"}, twice a second at most while a window moves) and carries the Stop
// to the desktop daemon ({"command":"computer-stop"}), so nothing here holds a token or talks to an
// API.
//
// Every window here is out of the agent's reach twice over. The daemon never acts on the shell's
// own process (the launcher puts it on the protected list), and each window ignores the mouse,
// never takes the focus and stays off the taskbar. Content protection keeps them out of the
// daemon's screenshots where the system supports it (Windows 10 2004 and later, macOS): a model
// shown a red frame and a note it can do nothing about would read them as part of the task.

const path = require('node:path');
const computer = require('./computer');

// How long the signs take to appear and to go. Short enough that a Stop looks immediate.
const FADE_MS = 160;
const FADE_STEP_MS = 16;

module.exports = function startComputer({ electron, dir, getStrings, sendStop, onChange, platform = process.platform, fadeMs = FADE_MS }) {
  const { BrowserWindow, globalShortcut, screen } = electron;
  let state = computer.normaliseComputer(null);
  let chord = '';
  // The strips on screen, reused as the target moves so a window being dragged does not make the
  // border blink; the note; and windows fading out after the lease ended.
  let strips = [];
  let note = null;
  let noteKey = '';
  let leaving = [];
  let drawn = '';

  function baseOptions(bounds) {
    return {
      ...bounds,
      show: false,
      frame: false,
      // Without the resizable frame Windows holds a window to no minimum size; with it, a 3-pixel
      // strip was widened to the system's minimum track size and the border came out lopsided.
      thickFrame: false,
      resizable: false,
      movable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      focusable: false,
      skipTaskbar: true,
      alwaysOnTop: true,
      hasShadow: false,
      enableLargerThanScreen: true,
      useContentSize: true,
      minWidth: 0,
      minHeight: 0,
      title: 'Daedalus',
      webPreferences: { contextIsolation: true, sandbox: true, nodeIntegration: false },
    };
  }

  // A strip is a solid window, never a transparent one: without a compositor (plain X11) a
  // transparent window is drawn black.
  function stripOptions(bounds, color) {
    return { ...baseOptions(bounds), transparent: false, backgroundColor: color };
  }

  // The note is transparent around its rounded pill. It is never shown on Linux (computerPlan), so
  // the black-window problem of an X server without a compositor does not reach it.
  function noteOptions(bounds) {
    return { ...baseOptions(bounds), transparent: true, backgroundColor: '#00000000' };
  }

  function decorate(window) {
    window.setIgnoreMouseEvents(true);
    window.setAlwaysOnTop(true, 'screen-saver');
    try {
      window.setContentProtection(true);
    } catch {}
    if (platform === 'darwin') {
      try {
        window.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
      } catch {}
    }
    window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    window.webContents.on('will-navigate', (event) => event.preventDefault());
    return window;
  }

  // place sets a window's bounds and checks them. On Windows a window moved onto a display whose
  // scale differs is first resized by the scale of the display it left, so a second setBounds is
  // what lands it where it was asked.
  function place(window, rect) {
    window.setBounds(rect);
    if (typeof window.getBounds !== 'function') return;
    const b = window.getBounds();
    if (b.x !== rect.x || b.y !== rect.y || b.width !== rect.width || b.height !== rect.height) window.setBounds(rect);
  }

  function setOpacity(window, value) {
    if (window.isDestroyed() || typeof window.setOpacity !== 'function') return;
    try {
      window.setOpacity(value);
    } catch {}
  }

  // fade takes windows from one opacity to another and then calls done. Linux ignores opacity on
  // most window managers; the windows then simply appear and go.
  function fade(windows, from, to, done) {
    if (fadeMs <= 0) {
      for (const w of windows) setOpacity(w, to);
      if (done) done();
      return;
    }
    const started = Date.now();
    for (const w of windows) setOpacity(w, from);
    const timer = setInterval(() => {
      const t = Math.min(1, (Date.now() - started) / fadeMs);
      for (const w of windows) setOpacity(w, from + (to - from) * t);
      if (t >= 1) {
        clearInterval(timer);
        if (done) done();
      }
    }, FADE_STEP_MS);
  }

  function show(window) {
    setOpacity(window, 0);
    window.showInactive();
    fade([window], 0, 1);
  }

  function destroy(window) {
    if (!window.isDestroyed()) window.destroy();
  }

  // leave fades every sign out and forgets it, so a new lease arriving during the fade draws its
  // own windows rather than fighting over these.
  function leave() {
    const going = [...strips, ...(note ? [note] : [])].filter((w) => !w.isDestroyed());
    strips = [];
    note = null;
    noteKey = '';
    drawn = '';
    if (!going.length) return;
    leaving.push(...going);
    fade(going, 1, 0, () => {
      for (const w of going) destroy(w);
      leaving = leaving.filter((w) => !going.includes(w));
    });
  }

  function clearAll() {
    for (const w of [...strips, ...leaving, ...(note ? [note] : [])]) destroy(w);
    strips = [];
    leaving = [];
    note = null;
    noteKey = '';
    drawn = '';
  }

  function noteText(strings) {
    if (state.paused) return chord ? fill(strings.computerPaused, chord) : strings.computerPausedTray;
    return chord ? fill(strings.computerNote, chord) : strings.computerNoteTray;
  }

  function fill(text, value) {
    return String(text).replace('{chord}', computer.chordLabel(value, platform));
  }

  function layoutStrips(rects, color) {
    for (let i = 0; i < rects.length; i += 1) {
      const existing = strips[i];
      if (existing && !existing.isDestroyed()) {
        if (existing.stripColor !== color) {
          existing.setBackgroundColor(color);
          existing.stripColor = color;
        }
        place(existing, rects[i]);
        continue;
      }
      const strip = decorate(new BrowserWindow(stripOptions(rects[i], color)));
      strip.stripColor = color;
      place(strip, rects[i]);
      strips[i] = strip;
      show(strip);
    }
    // Fewer strips than before (a window of the app closed): the rest go at once, since the ones
    // left on screen still say the agent holds it.
    for (const extra of strips.splice(rects.length)) destroy(extra);
  }

  function layoutNote(bounds, text, paused) {
    const key = JSON.stringify([text, paused]);
    if (note && !note.isDestroyed()) {
      place(note, bounds);
      if (key !== noteKey) {
        noteKey = key;
        note.loadFile(path.join(dir, 'pages', 'computer.html'), { query: { text, paused: paused ? '1' : '0' } });
      }
      return;
    }
    const window = decorate(new BrowserWindow(noteOptions(bounds)));
    note = window;
    noteKey = key;
    place(window, bounds);
    window.once('ready-to-show', () => {
      if (!window.isDestroyed() && note === window) show(window);
    });
    window.loadFile(path.join(dir, 'pages', 'computer.html'), { query: { text, paused: paused ? '1' : '0' } });
  }

  function draw() {
    const plan = computer.computerPlan(state, platform);
    if (!plan.border) {
      leave();
      return;
    }
    const displays = screen.getAllDisplays();
    const primary = screen.getPrimaryDisplay();
    const convert = platform === 'win32' && typeof screen.screenToDipRect === 'function' ? (rect) => screen.screenToDipRect(null, rect) : null;
    const targets = state.targets.map((t) => ({ kind: t.kind, ...computer.toDip(t, platform, { convert, scale: primary.scaleFactor }) }));
    const rects = computer.borderRects(targets, displays);
    const color = plan.paused ? '#c27c0e' : '#d93a3f';
    const area = computer.noteDisplay(targets, displays, primary).workArea;
    const key = JSON.stringify([rects, color, plan.note, chord, area]);
    if (key === drawn) return;
    drawn = key;
    layoutStrips(rects, color);
    if (plan.note) {
      layoutNote(computer.noteBounds(area), noteText(getStrings()), plan.paused);
    } else if (note) {
      destroy(note);
      note = null;
      noteKey = '';
    }
  }

  function stop() {
    if (state.active) sendStop();
  }

  function update(raw) {
    const next = computer.normaliseComputer(raw);
    if (next.active && !chord) chord = computer.armStop(globalShortcut, stop);
    if (!next.active && chord) {
      computer.disarmStop(globalShortcut, chord);
      chord = '';
    }
    state = next;
    draw();
    onChange({ active: state.active, paused: state.paused, scope: state.scope, chord });
  }

  electron.app.on('will-quit', () => {
    computer.disarmStop(globalShortcut, chord);
    clearAll();
  });

  // A display plugged in, unplugged or rescaled while the agent holds the screen moves the border
  // with it: the conversion from the daemon's pixels changes with the scale.
  for (const event of ['display-added', 'display-removed', 'display-metrics-changed']) {
    screen.on(event, () => {
      if (state.active) {
        drawn = '';
        draw();
      }
    });
  }

  return {
    update,
    stop,
    state: () => ({ active: state.active, paused: state.paused, scope: state.scope, chord }),
    // The launcher went away: no daemon, so no lease, whatever was last said.
    reset: () => update(null),
  };
};
