'use strict';

// Brücke zur Seite: nur der System-Dateidialog, siehe native-picker.js.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('mailophant', {
  nativePicker: () => ipcRenderer.invoke('native-picker'),
  pickPath: (req) => ipcRenderer.invoke('pick-path', req),
});
