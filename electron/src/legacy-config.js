'use strict';

const path = require('node:path');
const nodeFs = require('node:fs/promises');
const { constants: fsConstants } = require('node:fs');

/**
 * Former userData folders (below appData), checked in this order.
 * The first folder that contains a config.json wins.
 */
const LEGACY_DIR_NAMES = ['Mail-Backup-Viewer', 'mail-backup-viewer'];

/**
 * @param {string} file
 * @param {typeof nodeFs} fs
 * @returns {Promise<boolean>}
 */
async function fileExists(file, fs) {
  try {
    const stat = await fs.stat(file);
    return stat.isFile();
  } catch (err) {
    if (err && (err.code === 'ENOENT' || err.code === 'ENOTDIR')) return false;
    throw err;
  }
}

/**
 * Takes over the config.json from a former userData folder if the current one has none yet.
 * Nothing is ever overwritten or deleted; the venv is deliberately not copied.
 * @param {{ appDataDir: string, userDataDir: string, fs?: typeof nodeFs }} options
 * @returns {Promise<string | null>} source path of the copied file, or null if nothing was copied.
 */
async function migrateLegacyConfig({ appDataDir, userDataDir, fs = nodeFs }) {
  const target = path.join(userDataDir, 'config.json');
  if (await fileExists(target, fs)) return null;

  for (const dirName of LEGACY_DIR_NAMES) {
    const source = path.join(appDataDir, dirName, 'config.json');
    if (!(await fileExists(source, fs))) continue;
    await fs.mkdir(userDataDir, { recursive: true });
    // COPYFILE_EXCL: fails instead of overwriting if the file was created in the meantime.
    await fs.copyFile(source, target, fsConstants.COPYFILE_EXCL);
    return source;
  }
  return null;
}

module.exports = { migrateLegacyConfig, LEGACY_DIR_NAMES };
