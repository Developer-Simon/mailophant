'use strict';

// Startet Electron aus dem Projektordner. ELECTRON_RUN_AS_NODE (z. B. in manchen
// Entwicklungsumgebungen gesetzt) würde Electron als reines Node-Skript laufen lassen;
// deshalb wird die Variable für den Kindprozess entfernt.
const { spawn } = require('node:child_process');
const electron = require('electron');

const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;

// Weitere Argumente (z. B. `npm start -- --smoke`) werden an die App durchgereicht.
const child = spawn(electron, ['.', ...process.argv.slice(2)], { stdio: 'inherit', env });
child.on('exit', (code, signal) => {
  process.exit(code ?? (signal ? 1 : 0));
});
