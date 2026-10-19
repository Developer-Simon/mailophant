'use strict';

/**
 * System-Dateidialog für die Ordner- und Dateiauswahl der Seite. Nur mit MAILOPHANT_NATIVE_PICKER=1 (Flatpak):
 * dort sieht die App ohne Home-Zugriff nur, was über diesen Dialog (das Datei-Portal) gewählt wurde.
 */

const enabled = (env = process.env) => env.MAILOPHANT_NATIVE_PICKER === '1';

/**
 * Optionen für dialog.showOpenDialog.
 * @param {{ kind?: string, title?: string, filterName?: string }} req von der Seite
 */
function pickerOptions(req) {
  const title = typeof req?.title === 'string' ? req.title.slice(0, 200) : undefined;
  if (req?.kind === 'outlook') {
    const name = typeof req.filterName === 'string' ? req.filterName.slice(0, 100) : 'Outlook';
    return { title, properties: ['openFile'], filters: [{ name, extensions: ['pst', 'ost', 'PST', 'OST'] }] };
  }
  return { title, properties: ['openDirectory', 'createDirectory'] };
}

/**
 * Meldet die Handler an. Anfragen anderer Origins als des Servers werden abgelehnt.
 * @param {{ ipcMain: import('electron').IpcMain, dialog: import('electron').Dialog,
 *           isAllowed: (url: string) => boolean, env?: object }} deps
 */
function registerPicker({ ipcMain, dialog, isAllowed, env }) {
  const on = enabled(env);
  ipcMain.handle('native-picker', (e) => on && isAllowed(e.senderFrame?.url ?? ''));
  ipcMain.handle('pick-path', async (e, req) => {
    if (!on || !isAllowed(e.senderFrame?.url ?? '')) return null;
    const win = require('electron').BrowserWindow.fromWebContents(e.sender);
    const result = await dialog.showOpenDialog(win, pickerOptions(req));
    return result.canceled ? null : result.filePaths[0] ?? null;
  });
}

module.exports = { enabled, pickerOptions, registerPicker };
