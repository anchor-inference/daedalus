'use strict';

// Quick Chat, the appshot and the tray: the parts of the desktop application that work while its
// window is out of sight. The decisions are in desk.js and appshot.js; this file is the Electron
// around them.
//
// Quick Chat is the app's own page /app/quick in a small frameless window of the app's origin, so it
// is signed in by the same cookie as the main window and uses the app's own requests, attachment
// upload and dictation: nothing here talks to the API, and no credential passes through this
// process. The tray learns what is running from the app in the main window (desk:status), which
// already follows the event stream; when the app is not loaded the tray simply says nothing runs.

const fs = require('node:fs');
const path = require('node:path');
const { BrowserWindow, Menu, Tray, desktopCapturer, globalShortcut, ipcMain, nativeImage, nativeTheme, screen, shell, systemPreferences } = require('electron');
const desk = require('./desk');
const appshot = require('./appshot');
const policy = require('./policy');

// How long a hidden Quick Chat is given to leave the screen before the screen is photographed: the
// compositor draws the frame after the hide, and a capture taken at once has the box in it.
const HIDE_SETTLE_MS = 250;

// How long the region overlay's pages are given to draw the still they show. A page that never says
// it is ready (it failed to load) would otherwise leave the capture busy and every later appshot
// refused until the application restarts.
const REGION_READY_MS = 10000;

module.exports = function startDesk({ app, dir, getMain, showMain, getStrings, guard, language }) {
  const settingsFile = path.join(app.getPath('userData'), 'desk.json');
  let settings = desk.normaliseSettings(readJSON(settingsFile));
  const held = new Set();
  let report = {};
  let origin = null;
  let quick = null;
  let quickOrigin = null;
  let quickHeight = desk.QUICK_MIN_HEIGHT;
  let capturing = false;
  // Pictures taken by the appshot shortcut before the page could hear of them: it asks on load.
  let pending = [];
  // The voice shortcut's request to start dictation, kept for the page to collect: a page loaded by
  // this very press has not yet registered for quick:shown when the event is sent.
  let voiceAsked = false;
  // The region overlay while it is up: one window per display, and how to end it.
  let region = null;
  let tray = null;
  let trayIcon = '';
  let status = desk.normaliseStatus(null);
  const env = process.env;
  const deps = { screen, desktopCapturer, systemPreferences, platform: process.platform };
  const actions = {
    open: () => showMain(),
    quickChat: () => showQuick(),
    appshot: () => void appshotNow(),
    region: () => void regionNow(),
    voice: () => voiceNow(),
    pauseCompanion: () => tellMain('desk:action', 'pause-companion'),
    resumeCompanion: () => tellMain('desk:action', 'resume-companion'),
    quit: () => app.quit(),
  };

  function readJSON(file) {
    try {
      return JSON.parse(fs.readFileSync(file, 'utf8'));
    } catch {
      return null;
    }
  }

  function save() {
    try {
      fs.mkdirSync(path.dirname(settingsFile), { recursive: true });
      fs.writeFileSync(settingsFile, JSON.stringify(settings, null, 2));
    } catch {}
  }

  function register() {
    report = desk.registerShortcuts(globalShortcut, settings, { quickChat: toggleQuick, appshot: actions.appshot, region: actions.region, voice: actions.voice }, held);
  }

  function fromMain(event) {
    const main = getMain();
    return Boolean(main && !main.isDestroyed() && event.sender === main.webContents && event.senderFrame && policy.isInternal(event.senderFrame.url));
  }

  function fromQuick(event) {
    return Boolean(quick && !quick.isDestroyed() && event.sender === quick.webContents);
  }

  function tellMain(channel, payload) {
    const main = getMain();
    if (main && !main.isDestroyed()) main.webContents.send(channel, payload);
  }

  // The app's origin, from every address the main window is sent to. A change of port (the launcher
  // moved the app) drops the Quick Chat page of the old one; the next shortcut loads the new.
  function noteAddress(url) {
    const found = desk.appOrigin(url);
    if (found) origin = found;
  }

  function ensureQuick() {
    if (!origin) return null;
    if (quick && !quick.isDestroyed() && quickOrigin === origin) return quick;
    if (quick && !quick.isDestroyed()) quick.destroy();
    quickOrigin = origin;
    quick = new BrowserWindow({
      width: desk.QUICK_WIDTH,
      height: quickHeight,
      frame: false,
      resizable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      alwaysOnTop: true,
      skipTaskbar: true,
      show: false,
      title: 'Daedalus',
      backgroundColor: nativeTheme.shouldUseDarkColors ? '#101114' : '#f6f6f4',
      webPreferences: { preload: path.join(dir, 'preload.js'), contextIsolation: true, sandbox: true, nodeIntegration: false },
    });
    // Over a full-screen program and on every desktop, the way a launcher box is expected to be.
    quick.setAlwaysOnTop(true, 'floating');
    if (process.platform === 'darwin') quick.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
    guard(quick.webContents);
    quick.on('closed', () => {
      quick = null;
    });
    quick.loadURL(`${origin}/app/quick?lang=${language.startsWith('ru') ? 'ru' : 'en'}`);
    return quick;
  }

  function place(window) {
    const display = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
    window.setBounds(desk.quickBounds(display.workArea, desk.QUICK_WIDTH, quickHeight));
  }

  // Before the app has answered there is no page to show: the shortcut brings the main window
  // forward instead, which says what is happening.
  function showQuick(extra) {
    const window = ensureQuick();
    if (!window) {
      showMain();
      return;
    }
    place(window);
    // A Mac does not hand the keyboard to a window of a program that is not the active one.
    if (process.platform === 'darwin') app.focus({ steal: true });
    window.show();
    window.focus();
    window.webContents.send('quick:shown', extra || {});
  }

  function toggleQuick() {
    if (quick && !quick.isDestroyed() && quick.isVisible() && quick.isFocused()) quick.hide();
    else showQuick();
  }

  function settle() {
    return new Promise((resolve) => setTimeout(resolve, HIDE_SETTLE_MS));
  }

  // capture takes a picture with Quick Chat out of the way and puts it back afterwards.
  async function capture(take) {
    if (capturing) return { ok: false, error: 'busy', detail: '' };
    capturing = true;
    const visible = Boolean(quick && !quick.isDestroyed() && quick.isVisible());
    try {
      if (visible) {
        quick.hide();
        await settle();
      }
      return await take();
    } finally {
      capturing = false;
    }
  }

  async function appshotNow() {
    if (!origin) {
      showMain();
      return;
    }
    const result = await capture(() => appshot.captureScreen(deps));
    pending.push(result);
    showQuick({ appshot: true });
  }

  // The region shortcut, like the appshot's, ends in Quick Chat with the picture attached. An Esc
  // ends it where it started: Quick Chat comes back only if it was open before.
  async function regionNow() {
    if (!origin) {
      showMain();
      return;
    }
    const wasOpen = Boolean(quick && !quick.isDestroyed() && quick.isVisible());
    const result = await capture(() => appshot.captureRegion(deps, chooseRegion));
    if (!result.ok && result.error === 'dismissed') {
      if (wasOpen) bringBackQuick();
      return;
    }
    pending.push(result);
    showQuick({ appshot: true });
  }

  function voiceNow() {
    if (!origin) {
      showMain();
      return;
    }
    voiceAsked = true;
    showQuick({ voice: true });
  }

  function bringBackQuick() {
    if (!quick || quick.isDestroyed()) return;
    place(quick);
    quick.show();
    quick.focus();
  }

  // chooseRegion puts an overlay on every display, each showing the still taken of it, and answers
  // with the rectangle the operator drags on one of them, or null for Esc. The display a rectangle
  // belongs to is the overlay that reported it, never a value from the page. A selection stays on
  // one display: a drag across two would be a picture stitched from two scales.
  function chooseRegion(shots) {
    return new Promise((resolve, reject) => {
      const overlays = [];
      let settled = false;
      let timer = null;
      const end = (settle) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        region = null;
        for (const overlay of overlays) {
          if (!overlay.window.isDestroyed()) overlay.window.destroy();
        }
        settle();
      };
      region = {
        overlays,
        finish: (answer) => end(() => resolve(answer)),
        ready: () => {
          if (overlays.every((overlay) => overlay.ready)) clearTimeout(timer);
        },
      };
      timer = setTimeout(() => end(() => reject(new Error('the selection overlay did not open'))), REGION_READY_MS);
      const pointer = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
      const hint = getStrings().regionHint;
      for (const shot of shots) {
        const bounds = shot.display.bounds;
        const window = new BrowserWindow({
          x: bounds.x,
          y: bounds.y,
          width: bounds.width,
          height: bounds.height,
          frame: false,
          resizable: false,
          movable: false,
          minimizable: false,
          maximizable: false,
          // Full screen is how a Linux window manager lets a window over its panels; on a Mac it
          // would be a new Space with its animation, and the bounds already cover the display.
          fullscreenable: process.platform === 'linux',
          alwaysOnTop: true,
          skipTaskbar: true,
          show: false,
          hasShadow: false,
          enableLargerThanScreen: true,
          title: 'Daedalus',
          backgroundColor: '#000000',
          webPreferences: { preload: path.join(dir, 'preload.js'), contextIsolation: true, sandbox: true, nodeIntegration: false },
        });
        // JPEG for the picture under the selection: a 4K PNG takes long enough to encode that the
        // overlay would open late. The cut itself is taken from the lossless still, not from this.
        const overlay = { window, bounds, displayId: shot.display.id, focus: shot.display.id === pointer.id, ready: false, background: `data:image/jpeg;base64,${shot.image.toJPEG(90).toString('base64')}` };
        overlays.push(overlay);
        window.setAlwaysOnTop(true, 'screen-saver');
        if (process.platform === 'darwin') window.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
        guard(window.webContents);
        // An overlay closed by anything but its own answer — the system, a crash — is an Esc: the
        // others must not stay up over the screen with nobody to dismiss them.
        window.on('closed', () => region && region.overlays === overlays && region.finish(null));
        window.loadFile(path.join(dir, 'pages', 'region.html'), { query: { hint } });
      }
    });
  }

  function overlayOf(event) {
    if (!region) return null;
    return region.overlays.find((overlay) => !overlay.window.isDestroyed() && event.sender === overlay.window.webContents) || null;
  }

  // An overlay is shown once its page has drawn the still, so the display never flashes black.
  // The bounds are set again after showing: Windows sizes a window placed on a display of another
  // scale by the scale of the display it was created on, until it is moved there once.
  function showOverlay(overlay) {
    overlay.window.show();
    overlay.window.setBounds(overlay.bounds);
    if (process.platform === 'linux') overlay.window.setFullScreen(true);
    if (overlay.focus) overlay.window.focus();
  }

  function ownSourceIds() {
    return BrowserWindow.getAllWindows().map((window) => {
      try {
        return window.getMediaSourceId();
      } catch {
        return '';
      }
    });
  }

  function icon(name) {
    return nativeImage.createFromPath(path.join(dir, 'tray', `${name}.png`));
  }

  function createTray() {
    if (desk.trayNote(process.platform, env) === 'unavailable') return;
    try {
      tray = new Tray(icon('idle'));
    } catch {
      tray = null;
      return;
    }
    trayIcon = 'idle';
    // Windows opens the window on a click and keeps the menu for the right button; macOS opens the
    // menu on a click by itself, and Linux's indicators send no click at all.
    if (process.platform === 'win32') tray.on('click', () => showMain());
    updateTray();
  }

  function updateTray() {
    if (!tray) return;
    const strings = getStrings();
    const view = desk.trayView(status, strings);
    // Setting the same picture again makes some Linux indicators flicker; it is set only on a change.
    if (view.icon !== trayIcon) {
      tray.setImage(icon(view.icon));
      trayIcon = view.icon;
    }
    tray.setToolTip(view.tooltip);
    if (process.platform === 'darwin') tray.setTitle(view.title);
    tray.setContextMenu(Menu.buildFromTemplate(desk.trayTemplate(view, strings, settings, actions)));
  }

  // A tray that may draw nothing (plain GNOME) is no tray to hide the window into: the window would
  // vanish with no icon to bring it back from, and the operator would take it for a quit.
  function trayShown() {
    return Boolean(tray) && desk.trayNote(process.platform, env) === '';
  }

  function info(errors) {
    return {
      settings,
      defaults: desk.DEFAULTS,
      report,
      errors: errors || {},
      platform: process.platform,
      wayland: process.platform === 'linux' && desk.waylandSession(env),
      tray: { ready: trayShown(), note: desk.trayNote(process.platform, env) },
      screen: appshot.screenAccess(deps),
    };
  }

  ipcMain.handle('desk:settings', (event) => (fromMain(event) || fromQuick(event) ? info() : null));
  ipcMain.handle('desk:set-settings', (event, patch) => {
    if (!fromMain(event)) return null;
    const merged = desk.mergeSettings(settings, patch && typeof patch === 'object' ? patch : {});
    settings = merged.settings;
    save();
    register();
    updateTray();
    return info(merged.errors);
  });
  ipcMain.on('desk:status', (event, next) => {
    if (!fromMain(event)) return;
    status = desk.normaliseStatus(next);
    updateTray();
  });
  ipcMain.on('desk:screen-settings', (event) => {
    if (!(fromMain(event) || fromQuick(event)) || process.platform !== 'darwin') return;
    shell.openExternal('x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture');
  });
  ipcMain.on('quick:hide', (event) => {
    if (fromQuick(event)) quick.hide();
  });
  ipcMain.on('quick:resize', (event, height) => {
    if (!fromQuick(event)) return;
    quickHeight = desk.quickHeight(height);
    const [x, y] = quick.getPosition();
    quick.setBounds({ x, y, width: desk.QUICK_WIDTH, height: quickHeight });
  });
  // "Open" after a send: the conversation in the main window. The app there moves without a reload
  // when it is the app; anything else in the window (the launcher's page) is replaced by the address.
  ipcMain.on('quick:open', (event, wanted) => {
    if (!fromQuick(event)) return;
    const where = desk.appPath(wanted);
    if (!where || !origin) return;
    quick.hide();
    showMain();
    const main = getMain();
    if (!main || main.isDestroyed()) return;
    if (desk.appOrigin(main.webContents.getURL()) === origin) main.webContents.send('desk:navigate', where);
    else main.loadURL(origin + where);
  });
  ipcMain.handle('quick:pending', (event) => {
    if (!fromQuick(event)) return [];
    const taken = pending;
    pending = [];
    return taken;
  });
  ipcMain.handle('quick:windows', (event) => (fromQuick(event) ? appshot.listWindows(deps, ownSourceIds()) : null));
  ipcMain.handle('quick:capture', async (event, windowId) => {
    if (!fromQuick(event)) return null;
    const result = await capture(() => (windowId ? appshot.captureWindow(deps, String(windowId)) : appshot.captureScreen(deps)));
    bringBackQuick();
    return result;
  });
  ipcMain.handle('quick:region', async (event) => {
    if (!fromQuick(event)) return null;
    const result = await capture(() => appshot.captureRegion(deps, chooseRegion));
    bringBackQuick();
    return result;
  });
  ipcMain.handle('quick:voice', (event) => {
    if (!fromQuick(event)) return false;
    const asked = voiceAsked;
    voiceAsked = false;
    return asked;
  });
  ipcMain.handle('region:image', (event) => {
    const overlay = overlayOf(event);
    return overlay ? overlay.background : null;
  });
  ipcMain.on('region:ready', (event) => {
    const overlay = overlayOf(event);
    if (!overlay || overlay.ready) return;
    overlay.ready = true;
    showOverlay(overlay);
    region.ready();
  });
  // The pointer moved onto another display's overlay: it takes the keyboard, so Esc is heard there.
  ipcMain.on('region:focus', (event) => {
    const overlay = overlayOf(event);
    if (overlay && overlay.ready) overlay.window.focus();
  });
  ipcMain.on('region:done', (event, answer) => {
    const overlay = overlayOf(event);
    if (!overlay) return;
    if (!(answer && typeof answer === 'object' && answer.rect)) return region.finish(null);
    const content = overlay.window.getContentBounds();
    region.finish({ displayId: overlay.displayId, rect: answer.rect, offset: { x: content.x - overlay.bounds.x, y: content.y - overlay.bounds.y } });
  });

  app.on('will-quit', () => {
    for (const accelerator of held) {
      try {
        globalShortcut.unregister(accelerator);
      } catch {}
    }
  });

  register();
  createTray();

  return {
    noteAddress,
    run: (name) => (actions[name] ? actions[name]() : undefined),
    keepOnClose: (quitting) => desk.hideOnClose({ hideToTray: settings.hideToTray, trayReady: trayShown(), quitting }),
  };
};
