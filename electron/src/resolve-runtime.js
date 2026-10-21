'use strict';

const { t } = require('./i18n');
const path = require('node:path');
const nodeFs = require('node:fs/promises');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const { findSystemPython } = require('./python-locator');
const { ensureVenv } = require('./venv-manager');

const PYPFF_CHECK_TIMEOUT_MS = 30 * 1000;

const execFileAsync = promisify(execFile);

/** @type {(file: string, args: string[], opts: { timeout: number }) => Promise<{ stdout: string, stderr: string }>} */
const defaultRun = (file, args, opts) => execFileAsync(file, args, { ...opts, windowsHide: true });

async function defaultExists(p) {
  try {
    await nodeFs.access(p);
    return true;
  } catch {
    return false;
  }
}

/**
 * Path to the bundled Python.
 * @param {string} resourcesDir
 * @param {string} [platform]
 */
function bundledPython(resourcesDir, platform = process.platform) {
  return platform === 'win32'
    ? path.win32.join(resourcesDir, 'python', 'python.exe')
    : path.posix.join(resourcesDir, 'python', 'bin', 'python3');
}

class RuntimeUnavailableError extends Error {
  /**
   * @param {{ cmd: string[], reason: string }[]} rejected all rejection reasons
   */
  constructor(rejected) {
    const lines = rejected.map((r) => `  - ${r.cmd.join(' ')}: ${r.reason}`);
    const details = lines.length > 0 ? lines.join('\n') : `  ${t('electron.runtime.none_found')}`;
    super(t('electron.runtime.unavailable', { details, hint: t('electron.runtime.full_hint') }));
    this.name = 'RuntimeUnavailableError';
    this.rejected = rejected;
  }
}

/**
 * Checks whether pypff can be imported in the bundled Python.
 * @returns {Promise<boolean>}
 */
async function checkPypff(run, python) {
  try {
    await run(python, ['-c', 'import pypff'], { timeout: PYPFF_CHECK_TIMEOUT_MS });
    return true;
  } catch {
    return false;
  }
}

/**
 * Picks the interpreter: MAILOPHANT_PYTHON if set (Flatpak: the runtime's Python, pypff in the package),
 * otherwise system Python with a venv, otherwise the bundled one.
 * @param {{ userDataDir: string, resourcesDir: string, onProgress?: (m: string) => void,
 *           deps?: { find?: Function, ensure?: Function, exists?: Function, run?: Function, env?: object } }} opts
 * @returns {Promise<{ python: string[], mode: 'system' | 'bundled', pypff: boolean }>}
 */
async function resolveRuntime({ userDataDir, resourcesDir, onProgress, deps = {} }) {
  const find = deps.find ?? (() => findSystemPython());
  const ensure = deps.ensure ?? ensureVenv;
  const exists = deps.exists ?? defaultExists;
  const run = deps.run ?? defaultRun;
  const env = deps.env ?? process.env;

  if (env.MAILOPHANT_PYTHON) {
    const fixed = env.MAILOPHANT_PYTHON;
    return { python: [fixed], mode: 'bundled', pypff: await checkPypff(run, fixed) };
  }

  const rejected = [];

  const found = await find();
  if (found.python) {
    try {
      const venv = await ensure({
        probe: found.python,
        venvDir: path.join(userDataDir, 'venv'),
        onProgress,
      });
      return { python: [venv.python], mode: 'system', pypff: venv.pypff };
    } catch (err) {
      rejected.push(...(found.rejected ?? []));
      rejected.push({
        cmd: found.python.cmd,
        reason: `venv fehlgeschlagen: ${err && err.message ? err.message : err}`,
      });
    }
  } else {
    rejected.push(...(found.rejected ?? []));
  }

  const bundled = bundledPython(resourcesDir);
  if (await exists(bundled)) {
    const pypff = await checkPypff(run, bundled);
    return { python: [bundled], mode: 'bundled', pypff };
  }

  throw new RuntimeUnavailableError(rejected);
}

module.exports = { bundledPython, resolveRuntime, RuntimeUnavailableError };
