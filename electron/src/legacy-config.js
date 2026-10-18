'use strict';

const path = require('node:path');
const nodeFs = require('node:fs/promises');
const { constants: fsConstants } = require('node:fs');

/**
 * Frühere userData-Ordner (unterhalb von appData), in dieser Reihenfolge geprüft.
 * Der erste Ordner, der eine config.json enthält, gewinnt.
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
 * Übernimmt die config.json aus einem früheren userData-Ordner, falls im aktuellen noch keine liegt.
 * Es wird nie überschrieben und nie etwas gelöscht; das venv wird bewusst nicht kopiert.
 * @param {{ appDataDir: string, userDataDir: string, fs?: typeof nodeFs }} options
 * @returns {Promise<string | null>} Quellpfad der kopierten Datei oder null, wenn nichts kopiert wurde.
 */
async function migrateLegacyConfig({ appDataDir, userDataDir, fs = nodeFs }) {
  const target = path.join(userDataDir, 'config.json');
  if (await fileExists(target, fs)) return null;

  for (const dirName of LEGACY_DIR_NAMES) {
    const source = path.join(appDataDir, dirName, 'config.json');
    if (!(await fileExists(source, fs))) continue;
    await fs.mkdir(userDataDir, { recursive: true });
    // COPYFILE_EXCL: schlägt fehl statt zu überschreiben, falls die Datei inzwischen angelegt wurde.
    await fs.copyFile(source, target, fsConstants.COPYFILE_EXCL);
    return source;
  }
  return null;
}

module.exports = { migrateLegacyConfig, LEGACY_DIR_NAMES };
