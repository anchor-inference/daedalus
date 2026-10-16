'use strict';

// What a page in the window may ask of the desktop, and nothing more. The app reads
// window.daedalus.window to know it is in the desktop application (presence), and calls pickFolder
// for a project's root, which a page in a browser cannot do; reveal shows a folder or a file the host
// has already confined in the system's file manager. signIn asks the launcher for a link
// that signs the window's app in, which is how its login screen gets past itself. The launcher's status page reads
// shell to offer the update button. The shell's own message page uses openLog and retry.
//
// desk is the app's side of Quick Chat, the appshot and the tray (desk-main.js): the settings page
// reads and changes their settings, and the app in the main window reports what runs for the tray and
// hears its menu's commands. quick is Quick Chat's own page: hide, grow, open a conversation in the
// main window, take a picture, collect the voice shortcut's request to dictate. region is the page of
// the region appshot's overlay (pages/region.html): the still it draws on and the rectangle chosen.
// The main process answers each only from the window it belongs to.
const { contextBridge, ipcRenderer } = require('electron');

function listen(channel, callback) {
  const listener = (_event, value) => callback(value);
  ipcRenderer.on(channel, listener);
  return () => ipcRenderer.removeListener(channel, listener);
}

contextBridge.exposeInMainWorld('daedalus', {
  window: true,
  shell: true,
  pet: (enabled) => ipcRenderer.invoke('daedalus:pet', !!enabled),
  petState: (state) => ipcRenderer.send('daedalus:pet-state', state),
  onPetHidden: (callback) => { const listener = () => callback(); ipcRenderer.on('daedalus:pet-hidden', listener); return () => ipcRenderer.removeListener('daedalus:pet-hidden', listener); },
  pickFolder: () => ipcRenderer.invoke('daedalus:pick-folder'),
  reveal: (file) => ipcRenderer.invoke('daedalus:reveal', String(file || '')),
  signIn: () => ipcRenderer.send('daedalus:sign-in'),
  onSignInFailed: (callback) => { const listener = (_event, message) => callback(String(message || '')); ipcRenderer.on('daedalus:sign-in-failed', listener); return () => ipcRenderer.removeListener('daedalus:sign-in-failed', listener); },
  openLog: (file) => ipcRenderer.send('daedalus:open-log', String(file || '')),
  retry: () => ipcRenderer.send('daedalus:retry'),
  desk: {
    settings: () => ipcRenderer.invoke('desk:settings'),
    setSettings: (patch) => ipcRenderer.invoke('desk:set-settings', patch),
    status: (status) => ipcRenderer.send('desk:status', status),
    openScreenSettings: () => ipcRenderer.send('desk:screen-settings'),
    onAction: (callback) => listen('desk:action', (name) => callback(String(name || ''))),
    onNavigate: (callback) => listen('desk:navigate', (where) => callback(String(where || ''))),
  },
  quick: {
    hide: () => ipcRenderer.send('quick:hide'),
    resize: (height) => ipcRenderer.send('quick:resize', Number(height) || 0),
    open: (where) => ipcRenderer.send('quick:open', String(where || '')),
    pending: () => ipcRenderer.invoke('quick:pending'),
    windows: () => ipcRenderer.invoke('quick:windows'),
    capture: (windowId) => ipcRenderer.invoke('quick:capture', windowId ? String(windowId) : ''),
    region: () => ipcRenderer.invoke('quick:region'),
    voice: () => ipcRenderer.invoke('quick:voice'),
    onShown: (callback) => listen('quick:shown', (detail) => callback(detail || {})),
  },
  region: {
    image: () => ipcRenderer.invoke('region:image'),
    ready: () => ipcRenderer.send('region:ready'),
    focus: () => ipcRenderer.send('region:focus'),
    done: (answer) => ipcRenderer.send('region:done', answer || null),
  },
});
