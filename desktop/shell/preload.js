'use strict';

// What a page in the window may ask of the desktop, and nothing more. The app reads
// window.daedalus.window to know it is in the desktop application (presence), and calls pickFolder
// for a project's root, which a page in a browser cannot do. signIn asks the launcher for a link
// that signs the window's app in, which is how its login screen gets past itself. The launcher's status page reads
// shell to offer the update button. The shell's own message page uses openLog and retry.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('daedalus', {
  window: true,
  shell: true,
  pet: (enabled) => ipcRenderer.invoke('daedalus:pet', !!enabled),
  petState: (state) => ipcRenderer.send('daedalus:pet-state', state),
  onPetHidden: (callback) => { const listener = () => callback(); ipcRenderer.on('daedalus:pet-hidden', listener); return () => ipcRenderer.removeListener('daedalus:pet-hidden', listener); },
  pickFolder: () => ipcRenderer.invoke('daedalus:pick-folder'),
  signIn: () => ipcRenderer.send('daedalus:sign-in'),
  onSignInFailed: (callback) => { const listener = (_event, message) => callback(String(message || '')); ipcRenderer.on('daedalus:sign-in-failed', listener); return () => ipcRenderer.removeListener('daedalus:sign-in-failed', listener); },
  openLog: (file) => ipcRenderer.send('daedalus:open-log', String(file || '')),
  retry: () => ipcRenderer.send('daedalus:retry'),
});
