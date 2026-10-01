'use strict';

// The Daedalus desktop application: the window. Everything the operator sees is in a window of this
// process — the launcher's setup and status pages first, the app once it answers — and nothing is
// ever handed to the system's browser except a link that leaves this machine.
//
// What runs Daedalus is the launcher beside this executable (daedalus-desktop, Go): the setup, the
// runtime, the supervisor, the update manager. It is started here as a child with --shell and
// speaks one JSON object per line in each direction (desktop/shell.go has the protocol). Its own
// output goes to its log, not here; it has no console window on any platform.

const { app, BrowserWindow, Menu, Notification, dialog, ipcMain, screen, shell } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');
const policy = require('./policy');

const scheme = 'daedalus';
const isWindows = process.platform === 'win32';

// The Linux sandbox. An AppImage cannot carry the setuid helper Chromium's sandbox falls back to,
// and Ubuntu 24.04 and later forbid the unprivileged user namespaces it uses otherwise, so an
// AppImage there does not start at all unless the sandbox is off. It is turned off only where it
// cannot work: the pages shown here are the launcher's and the app's own, on loopback, and every
// link that leaves this machine opens in the system's browser instead.
if (process.platform === 'linux' && policy.sandboxUnavailable(process.env, readFirstLine, helperIsSetuid, process.execPath)) {
  app.commandLine.appendSwitch('no-sandbox');
}

// Where Electron keeps the window's own state (its caches, where the window was, shell.log). Not its
// default: on macOS that is ~/Library/Application Support/Daedalus itself — the folder the data,
// the runtime and the local state are in — and on Windows the roaming profile, which is no place for
// a browser's caches. Set before anything reads it, the single-instance lock included.
if (process.platform !== 'linux') {
  const base = isWindows ? process.env.LOCALAPPDATA || app.getPath('appData') : app.getPath('appData');
  app.setPath('userData', path.join(base, 'Daedalus', 'Shell'));
}

// The launcher beside this executable: Daedalus.exe and daedalus-desktop.exe in one folder on
// Windows, daedalus and daedalus-desktop on Linux, Contents/MacOS in the bundle. DAEDALUS_ENGINE
// points a development run (`npm start`) at a launcher built from source.
const engineName = isWindows ? 'daedalus-desktop.exe' : 'daedalus-desktop';
const engineExe = process.env.DAEDALUS_ENGINE || path.join(path.dirname(process.execPath), engineName);

let win = null;
let engine = null;
let quitting = false;
let upgrading = false;
let pendingLink = policy.linkFrom(process.argv);
let windowState = null;
// The shell's own few words, in the system's language; read once the application is ready, which
// is when Electron knows it.
let strings = policy.strings('en');

function helperIsSetuid() {
  try {
    const helper = fs.statSync(path.join(path.dirname(process.execPath), 'chrome-sandbox'));
    return helper.uid === 0 && (helper.mode & 0o4000) !== 0;
  } catch {
    return false;
  }
}

function readFirstLine(file) {
  try {
    return fs.readFileSync(file, 'utf8').split('\n')[0].trim();
  } catch {
    return null;
  }
}

// One application per user. A second start — a second click on the icon, a daedalus:// link —
// brings this window forward and hands it the link; it never starts a second launcher.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', (_event, argv) => {
    const link = policy.linkFrom(argv);
    showWindow();
    send({ command: 'focus', link: link || '' });
  });
  // macOS hands links over as an event, before or after the window exists.
  app.on('open-url', (event, url) => {
    event.preventDefault();
    if (engine) {
      showWindow();
      send({ command: 'focus', link: url });
    } else {
      pendingLink = url;
    }
  });
  app.whenReady().then(start);
}

function start() {
  strings = policy.strings(app.getLocale());
  if (isWindows) {
    // The identity the Start menu shortcut carries: notifications are shown as Daedalus, and the
    // window groups with a pinned shortcut on the taskbar.
    app.setAppUserModelId('io.github.anchor-inference.daedalus');
  }
  if (app.isPackaged) {
    // The installers register the scheme; this keeps it pointing at this copy when the
    // application was unpacked by hand, or moved.
    try {
      app.setAsDefaultProtocolClient(scheme);
    } catch {}
  }
  installMenu();
  ipcMain.handle('daedalus:pick-folder', async () => {
    const result = await dialog.showOpenDialog(win, { properties: ['openDirectory', 'createDirectory'] });
    return result.canceled || result.filePaths.length === 0 ? '' : result.filePaths[0];
  });
  createWindow();
  win.loadFile(path.join(__dirname, 'pages', 'starting.html'), { query: { text: strings.starting } });
  startEngine();
}

function shellLogFile() {
  const dir = path.join(app.getPath('userData'), 'logs');
  fs.mkdirSync(dir, { recursive: true });
  return path.join(dir, 'shell.log');
}

function startEngine() {
  const args = ['--shell'];
  if (pendingLink) args.push(pendingLink);
  pendingLink = null;
  let log = null;
  try {
    log = fs.createWriteStream(shellLogFile(), { flags: 'a' });
    log.write(`\n${new Date().toISOString()} starting ${engineExe}\n`);
  } catch {}
  try {
    // windowsHide: the launcher is a console program, and without it Windows gives it a console
    // window of its own. Hidden, it still has a console — one nobody sees — and every console
    // program it starts (git, uv, the supervisor) shares that one instead of opening its own.
    engine = spawn(engineExe, args, {
      cwd: path.dirname(engineExe),
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    });
  } catch (err) {
    fatal(String(err && err.message ? err.message : err), '');
    return;
  }
  const child = engine;
  child.on('error', (err) => {
    if (child === engine) fatal(`${strings.engineMissing}\n${engineExe}\n${err.message}`, '');
  });
  readline.createInterface({ input: child.stdout }).on('line', (line) => {
    const event = policy.parseEvent(line);
    if (event) handle(event);
  });
  // Standard error carries what the launcher says before its log is open, and a Go panic.
  child.stderr.on('data', (chunk) => log && log.write(chunk));
  child.on('exit', (code, signal) => {
    if (log) log.end(`${new Date().toISOString()} the launcher exited (${code === null ? signal : code})\n`);
    if (child !== engine) return;
    engine = null;
    if (quitting || upgrading) return;
    if (!lastFatal) fatal(strings.engineExited, '');
  });
}

function send(command) {
  if (!engine || !engine.stdin.writable) return;
  engine.stdin.write(JSON.stringify(command) + '\n');
}

let lastFatal = null;

function handle(event) {
  switch (event.event) {
    case 'show':
      showWindow();
      win.loadURL(event.url);
      break;
    case 'focus':
      showWindow();
      if (event.url) win.loadURL(event.url);
      break;
    case 'notify':
      notify(event);
      break;
    case 'upgrade':
      upgrade(event.data);
      break;
    case 'fatal':
      lastFatal = event;
      fatal(event.message, event.log);
      break;
  }
}

function notify(event) {
  if (!Notification.isSupported()) return;
  const note = new Notification({ title: event.title || 'Daedalus', body: event.body || '' });
  note.on('click', () => {
    showWindow();
    if (event.url && policy.isInternal(event.url)) win.loadURL(event.url);
  });
  note.show();
}

// The launcher stopped before there was anything to show, or went away: the window says why and
// where its log is, instead of a terminal nobody has.
function fatal(message, logPath) {
  showWindow();
  win.loadFile(path.join(__dirname, 'pages', 'message.html'), {
    query: { title: strings.fatalTitle, text: message || '', log: logPath || '', open: strings.openLog, retry: strings.retry },
  });
}

// Only the shell's own message page may ask for these: a page on loopback is the launcher's or the
// app's, and neither has a reason to.
function fromOwnPage(event) {
  return Boolean(event.senderFrame && policy.isOwnFile(event.senderFrame.url, __dirname));
}

ipcMain.on('daedalus:open-log', (event, file) => {
  if (fromOwnPage(event) && typeof file === 'string' && file) shell.showItemInFolder(file);
});
ipcMain.on('daedalus:retry', (event) => {
  if (!fromOwnPage(event)) return;
  lastFatal = null;
  if (engine) return;
  win.loadFile(path.join(__dirname, 'pages', 'starting.html'), { query: { text: strings.starting } });
  startEngine();
});

// Installing a newer release replaces this application's own files, which cannot happen while it
// runs. So the launcher is asked to stop, the launcher's `upgrade` is started on its own, and this
// process exits; `upgrade` protects the data, puts the new version in place, starts and checks it
// — or puts both back — and then opens the application again (--from-app), which reports how it
// went. Nothing is downloaded or trusted here: `upgrade` checks the release's signature by the
// project's key and the archive's checksum before anything is replaced.
async function upgrade(data) {
  if (upgrading || !data) return;
  upgrading = true;
  if (Notification.isSupported()) {
    new Notification({ title: 'Daedalus', body: strings.upgrading }).show();
  }
  await stopEngine();
  const options = { cwd: path.dirname(engineExe), stdio: 'ignore', windowsHide: true };
  // Detached elsewhere, so the upgrade outlives this process's session. Not on Windows: a detached
  // console program there has no console at all, and every console program it starts would open a
  // visible window of its own. A child on Windows outlives its parent anyway.
  if (!isWindows) options.detached = true;
  try {
    const child = spawn(engineExe, ['upgrade', '--yes', '--from-app', '--data', data], options);
    child.unref();
  } catch (err) {
    upgrading = false;
    fatal(String(err.message || err), '');
    return;
  }
  quitting = true;
  app.exit(0);
}

// stopEngine asks the launcher to stop and waits for it: in native mode it stops the agent first,
// which may take as long as a run in flight needs to settle.
function stopEngine() {
  return new Promise((resolve) => {
    if (!engine) return resolve();
    const child = engine;
    const timer = setTimeout(() => {
      try {
        child.kill();
      } catch {}
      resolve();
    }, 60000);
    child.once('exit', () => {
      clearTimeout(timer);
      resolve();
    });
    send({ command: 'quit' });
    try {
      child.stdin.end();
    } catch {}
  });
}

app.on('before-quit', (event) => {
  if (quitting) return;
  quitting = true;
  if (!engine) return;
  event.preventDefault();
  saveWindowState();
  stopEngine().then(() => app.exit(0));
});

app.on('window-all-closed', () => app.quit());

function windowStateFile() {
  return path.join(app.getPath('userData'), 'window.json');
}

function createWindow() {
  windowState = policy.restoreBounds(readJSON(windowStateFile()), screen.getAllDisplays().map((d) => d.workArea));
  win = new BrowserWindow({
    ...windowState.bounds,
    minWidth: 480,
    minHeight: 400,
    title: 'Daedalus',
    show: false,
    autoHideMenuBar: true,
    backgroundColor: '#101114',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    },
  });
  if (windowState.maximized) win.maximize();
  win.once('ready-to-show', () => win.show());
  guard(win.webContents);
  win.on('close', saveWindowState);
}

// guard keeps every page in this window on this machine. The launcher's pages and the app are on
// loopback; anything else a page tries to open or navigate to is the operator following a link out,
// and that goes to the system's browser.
function guard(contents) {
  contents.on('will-navigate', (event, url) => {
    if (policy.isInternal(url) || policy.isOwnFile(url, __dirname)) return;
    event.preventDefault();
    openOutside(url);
  });
  contents.setWindowOpenHandler(({ url }) => {
    if (policy.isInternal(url)) {
      return {
        action: 'allow',
        overrideBrowserWindowOptions: {
          autoHideMenuBar: true,
          webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, sandbox: true },
        },
      };
    }
    openOutside(url);
    return { action: 'deny' };
  });
  contents.on('did-create-window', (child) => guard(child.webContents));
}

function openOutside(url) {
  if (policy.isExternal(url)) shell.openExternal(url);
}

function showWindow() {
  if (!win || win.isDestroyed()) createWindow();
  if (win.isMinimized()) win.restore();
  if (!win.isVisible()) win.show();
  win.focus();
}

function saveWindowState() {
  if (!win || win.isDestroyed()) return;
  try {
    const state = { bounds: win.getNormalBounds(), maximized: win.isMaximized() };
    fs.mkdirSync(path.dirname(windowStateFile()), { recursive: true });
    fs.writeFileSync(windowStateFile(), JSON.stringify(state));
  } catch {}
}

function readJSON(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return null;
  }
}

// The menu. On macOS it is where the keyboard shortcuts live — without an Edit menu ⌘C and ⌘V do
// nothing in the window — so it is the standard one. Elsewhere the bar is hidden (Alt shows it)
// and carries the same edit, zoom and reload commands, so their shortcuts work.
function installMenu() {
  const template = [
    ...(process.platform === 'darwin' ? [{ role: 'appMenu' }] : []),
    { role: 'editMenu' },
    {
      label: strings.view,
      submenu: [
        { role: 'reload' },
        { role: 'forceReload' },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
    { role: 'windowMenu' },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}
