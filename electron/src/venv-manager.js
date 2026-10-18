'use strict';

const { t } = require('./i18n');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const path = require('node:path');
const nodeFs = require('node:fs/promises');

const STAMP_NAME = 'mailviewer-stamp.json';
const PYPFF_PACKAGE = 'libpff-python';
const VENV_TIMEOUT_MS = 5 * 60 * 1000;
const PIP_TIMEOUT_MS = 10 * 60 * 1000;

const execFileAsync = promisify(execFile);

/** @type {(file: string, args: string[], opts: { timeout: number }) => Promise<{ stdout: string, stderr: string }>} */
const defaultRun = (file, args, opts) => execFileAsync(file, args, { ...opts, windowsHide: true });

/**
 * Pfad zum Interpreter in einer venv.
 * @param {string} venvDir
 * @param {string} [platform]
 */
function venvPython(venvDir, platform = process.platform) {
  return platform === 'win32'
    ? path.win32.join(venvDir, 'Scripts', 'python.exe')
    : path.posix.join(venvDir, 'bin', 'python');
}

async function readStamp(fs, stampPath) {
  try {
    const data = JSON.parse(await fs.readFile(stampPath, 'utf8'));
    return data && typeof data === 'object' ? data : null;
  } catch {
    return null;
  }
}

// Schreibt über eine Temp-Datei, damit ein Abbruch keinen halben Stamp hinterlässt.
async function writeStamp(fs, stampPath, stamp) {
  const tmp = `${stampPath}.tmp`;
  await fs.writeFile(tmp, JSON.stringify(stamp, null, 2) + '\n', 'utf8');
  await fs.rename(tmp, stampPath);
}

async function exists(fs, p) {
  try {
    await fs.access(p);
    return true;
  } catch {
    return false;
  }
}

/**
 * Ein Installationsversuch für pypff: erst Wheel, dann Quellbuild.
 * @returns {Promise<boolean>} ob pypff installiert ist
 */
async function installPypff(run, python, onProgress) {
  onProgress?.(t('electron.loading.install_pst'));
  try {
    await run(python, ['-m', 'pip', 'install', '--only-binary=:all:', PYPFF_PACKAGE], {
      timeout: PIP_TIMEOUT_MS,
    });
    return true;
  } catch {
    // weiter mit dem Quellbuild
  }
  onProgress?.(t('electron.loading.build_pst'));
  try {
    await run(python, ['-m', 'pip', 'install', PYPFF_PACKAGE], { timeout: PIP_TIMEOUT_MS });
    return true;
  } catch {
    return false;
  }
}

/**
 * Stellt sicher, dass in `venvDir` eine passende venv mit pypff existiert.
 * Wirft, wenn die venv selbst nicht angelegt werden kann; pypff-Fehler werden
 * als `pypff: false` gemeldet.
 *
 * @param {{ probe: { cmd: string[], version: number[] }, venvDir: string,
 *           onProgress?: (msg: string) => void, retryPypff?: boolean,
 *           run?: (file: string, args: string[], opts: { timeout: number }) => Promise<{ stdout: string, stderr: string }>,
 *           fs?: typeof import("node:fs/promises") }} opts
 * @returns {Promise<{ python: string, pypff: boolean, rebuilt: boolean }>}
 */
async function ensureVenv({
  probe,
  venvDir,
  onProgress,
  retryPypff = false,
  run = defaultRun,
  fs = nodeFs,
}) {
  const python = venvPython(venvDir);
  const stampPath = path.join(venvDir, STAMP_NAME);
  const wantedVersion = `${probe.version[0]}.${probe.version[1]}`;

  const stamp = await readStamp(fs, stampPath);
  const usable =
    stamp !== null &&
    stamp.complete === true &&
    stamp.python === wantedVersion &&
    (await exists(fs, python));

  if (usable) {
    if (stamp.pypff === true) return { python, pypff: true, rebuilt: false };
    if (!retryPypff) return { python, pypff: false, rebuilt: false };
    const pypff = await installPypff(run, python, onProgress);
    await writeStamp(fs, stampPath, { python: wantedVersion, pypff, complete: true });
    return { python, pypff, rebuilt: false };
  }

  onProgress?.(t('electron.loading.create_venv'));
  await fs.rm(venvDir, { recursive: true, force: true });
  await run(probe.cmd[0], [...probe.cmd.slice(1), '-m', 'venv', venvDir], {
    timeout: VENV_TIMEOUT_MS,
  });
  if (!(await exists(fs, python))) {
    throw new Error(t('electron.venv.no_interpreter', { python }));
  }

  // complete bleibt false, bis pypff entschieden ist; so erkennt ein Abbruch den Zustand.
  await writeStamp(fs, stampPath, { python: wantedVersion, pypff: false, complete: false });
  const pypff = await installPypff(run, python, onProgress);
  await writeStamp(fs, stampPath, { python: wantedVersion, pypff, complete: true });
  return { python, pypff, rebuilt: true };
}

module.exports = { venvPython, ensureVenv };
