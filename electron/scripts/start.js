'use strict';

// Starts Electron from the project folder. ELECTRON_RUN_AS_NODE (set in some development
// environments, for example) would make Electron run as a plain Node script, so the
// variable is removed for the child process.
const { spawn } = require('node:child_process');
const electron = require('electron');

const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;

// Further arguments (e.g. `npm start -- --smoke`) are passed through to the app.
const child = spawn(electron, ['.', ...process.argv.slice(2)], { stdio: 'inherit', env });
child.on('exit', (code, signal) => {
  process.exit(code ?? (signal ? 1 : 0));
});
