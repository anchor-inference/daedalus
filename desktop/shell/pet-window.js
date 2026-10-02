'use strict';
const path = require('node:path');
const { BrowserWindow, ipcMain, screen } = require('electron');

/** An optional companion, owned by the application; no credentials or application DOM enter it. */
module.exports = function companion(getMain, showMain, language) {
  let pet = null;
  let state = { state:'idle', reduced:false };
  const fromMain = (event) => event.sender === getMain()?.webContents;
  const fromPet = (event) => event.sender === pet?.webContents;
  const hide = () => { if (pet) { pet.close(); pet = null; } };
  ipcMain.handle('daedalus:pet', (event, enabled) => {
    if (!fromMain(event)) return false;
    if (!enabled) { hide(); return false; }
    if (!pet) {
      const area = screen.getPrimaryDisplay().workArea;
      pet = new BrowserWindow({ width:180, height:250, x:area.x + area.width - 196, y:area.y + area.height - 266, transparent:true, frame:false, resizable:false, alwaysOnTop:true, skipTaskbar:true, show:false, webPreferences:{ preload:path.join(__dirname, 'pet', 'preload.js'), contextIsolation:true, sandbox:true, nodeIntegration:false } });
      pet.webContents.setWindowOpenHandler(() => ({ action:'deny' }));
      pet.webContents.on('will-navigate', (e) => e.preventDefault());
      pet.once('ready-to-show', () => { pet?.showInactive(); pet?.webContents.send('pet:state', state); });
      pet.on('closed', () => { pet = null; getMain()?.webContents.send('daedalus:pet-hidden'); });
      pet.loadFile(path.join(__dirname, 'pet', 'index.html'), { query:{ lang:language.startsWith('ru') ? 'ru' : 'en' } });
    }
    return true;
  });
  ipcMain.on('daedalus:pet-state', (event, next) => {
    if (!fromMain(event)) return;
    state = { state:['idle','running','waiting','failed'].includes(next?.state) ? next.state : 'idle', reduced:!!next?.reduced };
    pet?.webContents.send('pet:state', state);
  });
  ipcMain.on('pet:open', (event) => { if (fromPet(event)) showMain(); });
  ipcMain.on('pet:hide', (event) => { if (fromPet(event)) hide(); });
  return hide;
};
