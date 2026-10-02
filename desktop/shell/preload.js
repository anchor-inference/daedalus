'use strict';

// What a page in the window may ask of the desktop, and nothing more. The app reads
// window.daedalus.window to know it is in the desktop application (presence), and calls pickFolder
// for a project's root, which a page in a browser cannot do. The launcher's status page reads
// shell to offer the update button. The shell's own message page uses openLog and retry.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('daedalus', {
  window: true,
  shell: true,
  pet: (enabled) => ipcRenderer.invoke('daedalus:pet', !!enabled),
  petState: (state) => ipcRenderer.send('daedalus:pet-state', state),
  onPetHidden: (callback) => { const listener = () => callback(); ipcRenderer.on('daedalus:pet-hidden', listener); return () => ipcRenderer.removeListener('daedalus:pet-hidden', listener); },
  pickFolder: () => ipcRenderer.invoke('daedalus:pick-folder'),
  openLog: (file) => ipcRenderer.send('daedalus:open-log', String(file || '')),
  retry: () => ipcRenderer.send('daedalus:retry'),
});
