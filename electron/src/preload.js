'use strict';

// Bridge to the page: only the system file dialog, see native-picker.js.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('mailophant', {
  nativePicker: () => ipcRenderer.invoke('native-picker'),
  pickPath: (req) => ipcRenderer.invoke('pick-path', req),
});
