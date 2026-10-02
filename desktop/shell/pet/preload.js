'use strict';
const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('pet', {
  onState: (callback) => ipcRenderer.on('pet:state', (_event, state) => callback(state)),
  open: () => ipcRenderer.send('pet:open'),
  hide: () => ipcRenderer.send('pet:hide'),
});
