'use strict';

const { t } = require('./i18n');
const { spawn, execFile } = require('node:child_process');
const readline = require('node:readline');

const PREFERRED_PORT = 8025;
const DEFAULT_TIMEOUT_MS = 15000;
const STOP_GRACE_MS = 3000;
const TAIL_LINES = 20;
// Serving line from mailviewer/server.py (main), written to stderr.
const SERVING_RE = /^Serving on http:\/\/127\.0\.0\.1:(\d+)\//;
const ADDR_IN_USE_RE = /Address already in use|WinError 10048|EADDRINUSE/;

class ServerStartError extends Error {
  /**
   * @param {string} message
   * @param {string} stderrTail last lines of the server output
   */
  constructor(message, stderrTail) {
    super(message);
    this.name = 'ServerStartError';
    this.stderrTail = stderrTail;
  }
}

/**
 * Reads the port from the serving line.
 * @param {string} line
 * @returns {number | null}
 */
function parsePort(line) {
  const match = SERVING_RE.exec(String(line).trim());
  if (!match) return null;
  const port = Number(match[1]);
  return port > 0 && port < 65536 ? port : null;
}

function isExited(child) {
  return child.exitCode !== null || child.signalCode !== null;
}

/**
 * Stops the process: SIGTERM, SIGKILL after STOP_GRACE_MS; taskkill /T /F on Windows.
 * @param {import('node:child_process').ChildProcess} child
 * @param {Promise<void>} exited resolves on the process's 'close'
 */
async function terminate(child, exited) {
  if (isExited(child)) {
    await exited;
    return;
  }
  if (process.platform === 'win32') {
    await new Promise((resolve) => {
      execFile('taskkill', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true }, () => resolve());
    });
    child.kill();
    await exited;
    return;
  }
  child.kill('SIGTERM');
  const forceTimer = setTimeout(() => child.kill('SIGKILL'), STOP_GRACE_MS);
  await exited;
  clearTimeout(forceTimer);
}

/**
 * One start attempt with the given port. Always resolves, never throws.
 * @returns {Promise<
 *   { ok: true, child: import('node:child_process').ChildProcess, exited: Promise<void>, port: number } |
 *   { ok: false, tail: string, code: number | null, signal: string | null, timedOut: boolean, error: Error | null }
 * >}
 */
function launch(opts, port) {
  const { python, appRoot, configPath, extraArgs = [], env = {}, timeoutMs = DEFAULT_TIMEOUT_MS } = opts;
  const [file, ...prefix] = python;
  const args = [...prefix, '-m', 'mailviewer', '--port', String(port), '--config', configPath, ...extraArgs];

  // PYTHONNOUSERSITE: no packages from ~/.local; PYTHONHOME: don't pick up a foreign stdlib.
  const childEnv = {
    ...process.env,
    ...env,
    PYTHONUNBUFFERED: '1',
    PYTHONUTF8: '1',
    PYTHONPATH: appRoot,
    PYTHONNOUSERSITE: '1',
  };
  for (const key of Object.keys(childEnv)) {
    if (key.toUpperCase() === 'PYTHONHOME') delete childEnv[key];
  }

  return new Promise((resolve) => {
    const child = spawn(file, args, {
      cwd: appRoot,
      env: childEnv,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });
    const exited = new Promise((res) => child.once('close', () => res()));

    const tail = [];
    let settled = false;
    let timer = null;

    const finish = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };
    const onPortLine = (line) => {
      const found = parsePort(line);
      if (found !== null) finish({ ok: true, child, exited, port: found });
    };

    readline.createInterface({ input: child.stderr }).on('line', (line) => {
      tail.push(line);
      if (tail.length > TAIL_LINES) tail.shift();
      onPortLine(line);
    });
    readline.createInterface({ input: child.stdout }).on('line', onPortLine);

    child.once('error', (error) => {
      finish({ ok: false, tail: tail.join('\n'), code: null, signal: null, timedOut: false, error });
    });
    child.once('close', (code, signal) => {
      finish({ ok: false, tail: tail.join('\n'), code, signal, timedOut: false, error: null });
    });

    timer = setTimeout(() => {
      finish({ ok: false, tail: tail.join('\n'), code: null, signal: null, timedOut: true, error: null });
      terminate(child, exited);
    }, timeoutMs);
  });
}

function startError(result, timeoutMs) {
  let message;
  if (result.timedOut) {
    message = t('electron.server.timeout', { ms: timeoutMs });
  } else if (result.error) {
    message = t('electron.server.start_failed', { detail: result.error.message });
  } else {
    message = t('electron.server.exited', { code: result.code ?? result.signal });
  }
  return new ServerStartError(message, result.tail);
}

function makeHandle(ready) {
  const { child, exited, port } = ready;
  let stopping = null;
  return {
    port,
    url: `http://127.0.0.1:${port}/`,
    pid: child.pid,
    stop: () => {
      if (!stopping) stopping = terminate(child, exited);
      return stopping;
    },
  };
}

/**
 * Starts `python -m mailviewer --port <p> --config <configPath> ...extraArgs`.
 * PREFERRED_PORT first; if that port is taken, a second attempt with `--port 0`.
 * @param {{ python: string[], appRoot: string, configPath: string, extraArgs?: string[],
 *           env?: NodeJS.ProcessEnv, timeoutMs?: number }} opts
 * @returns {Promise<{ port: number, url: string, pid: number | undefined, stop: () => Promise<void> }>}
 */
async function startServer(opts) {
  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const first = await launch({ ...opts, timeoutMs }, PREFERRED_PORT);
  if (first.ok) return makeHandle(first);

  if (!first.timedOut && !first.error && ADDR_IN_USE_RE.test(first.tail)) {
    const second = await launch({ ...opts, timeoutMs }, 0);
    if (second.ok) return makeHandle(second);
    throw startError(second, timeoutMs);
  }
  throw startError(first, timeoutMs);
}

module.exports = { PREFERRED_PORT, ServerStartError, parsePort, startServer };
