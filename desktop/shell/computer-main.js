'use strict';

// The signs on the desktop while the agent holds it: a coloured border around every display, a note
// at the top saying how to stop, and the global Stop shortcut. The decisions are in computer.js;
// this file is the Electron around them. The launcher tells the shell when the lease changes
// ({"event":"computer"}) and carries the Stop to the desktop daemon ({"command":"computer-stop"}),
// so nothing here holds a token or talks to an API.
//
// Every window here is out of the agent's reach twice over. The daemon never acts on the shell's
// own process (the launcher puts it on the protected list), and each window ignores the mouse,
// never takes the focus and stays off the taskbar. Content protection keeps them out of the
// daemon's screenshots where the system supports it (Windows 10 2004 and later, macOS): a model
// shown a red frame and a note it can do nothing about would read them as part of the task.

const path = require('node:path');
const computer = require('./computer');

module.exports = function startComputer({ electron, dir, getStrings, sendStop, onChange, platform = process.platform }) {
  const { BrowserWindow, globalShortcut, screen } = electron;
  let state = computer.normaliseComputer(null);
  let chord = '';
  let windows = [];
  // What the windows on screen were drawn for; a lease event that changes none of it redraws nothing.
  let drawn = '';

  function baseOptions(bounds, color) {
    return {
      ...bounds,
      show: false,
      frame: false,
      transparent: false,
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
      backgroundColor: color,
      title: 'Daedalus',
      webPreferences: { contextIsolation: true, sandbox: true, nodeIntegration: false },
    };
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
    windows.push(window);
    return window;
  }

  function clear() {
    for (const window of windows) {
      if (!window.isDestroyed()) window.destroy();
    }
    windows = [];
    drawn = '';
  }

  function noteText(strings) {
    if (state.paused) return chord ? fill(strings.computerPaused, chord) : strings.computerPausedTray;
    return chord ? fill(strings.computerNote, chord) : strings.computerNoteTray;
  }

  function fill(text, value) {
    return String(text).replace('{chord}', computer.chordLabel(value, platform));
  }

  function draw() {
    const plan = computer.computerPlan(state, platform);
    const displays = plan.border ? screen.getAllDisplays() : [];
    const key = JSON.stringify([plan, chord, displays.map((d) => [d.bounds, d.workArea])]);
    if (key === drawn) return;
    clear();
    if (!plan.border) return;
    drawn = key;
    const color = plan.paused ? '#c27c0e' : '#d93a3f';
    for (const display of displays) {
      for (const rect of computer.edgeRects(display.bounds)) {
        const strip = decorate(new BrowserWindow(baseOptions(rect, color)));
        // Set again once the window exists: on Windows a window created on a display whose scale
        // differs from the primary one is sized by the primary's scale first.
        strip.setBounds(rect);
        strip.showInactive();
      }
    }
    if (plan.note) {
      const area = screen.getPrimaryDisplay().workArea;
      const note = decorate(new BrowserWindow(baseOptions(computer.noteBounds(area), color)));
      note.once('ready-to-show', () => {
        if (!note.isDestroyed()) note.showInactive();
      });
      note.loadFile(path.join(dir, 'pages', 'computer.html'), { query: { text: noteText(getStrings()), paused: plan.paused ? '1' : '0' } });
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
    onChange({ ...state, chord });
  }

  electron.app.on('will-quit', () => computer.disarmStop(globalShortcut, chord));

  // A display plugged in, unplugged or rescaled while the agent holds the screen gets its border.
  for (const event of ['display-added', 'display-removed', 'display-metrics-changed']) {
    screen.on(event, () => {
      if (state.active) draw();
    });
  }

  return {
    update,
    stop,
    state: () => ({ ...state, chord }),
    // The launcher went away: no daemon, so no lease, whatever was last said.
    reset: () => update(null),
  };
};
