'use strict';

// Builds the bundled Python interpreter into electron/build/python/.
// Downloads the python-build-standalone asset pinned in runtime.lock.json,
// checks its SHA-256 sum, unpacks it and installs libpff-python as a wheel.
//
// CLI: node runtime/build-runtime.js [--target <key>] [--allow-no-pypff]

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { Readable } = require('node:stream');
const { pipeline } = require('node:stream/promises');
const { spawnSync } = require('node:child_process');

const lock = require('./runtime.lock.json');

const ELECTRON_DIR = path.resolve(__dirname, '..');
const BUILD_DIR = path.join(ELECTRON_DIR, 'build');
const CACHE_DIR = path.join(BUILD_DIR, 'cache');
const PYTHON_DIR = path.join(BUILD_DIR, 'python');

function targetKey(platform, arch) {
  const key = `${platform}-${arch}`;
  if (!Object.prototype.hasOwnProperty.call(lock.targets, key)) {
    throw new Error(
      `Unsupported platform: ${key}. Supported: ${Object.keys(lock.targets).join(', ')}`,
    );
  }
  return key;
}

function pythonBinary(key) {
  return key.startsWith('win32-')
    ? path.join(PYTHON_DIR, 'python.exe')
    : path.join(PYTHON_DIR, 'bin', 'python3');
}

function hashFile(file) {
  return new Promise((resolve, reject) => {
    const hash = crypto.createHash('sha256');
    const stream = fs.createReadStream(file);
    stream.on('error', reject);
    stream.on('data', (chunk) => hash.update(chunk));
    stream.on('end', () => resolve(hash.digest('hex')));
  });
}

async function verifySha256(file, expected) {
  const actual = await hashFile(file);
  if (actual !== expected.toLowerCase()) {
    throw new Error(`SHA-256 mismatch for ${file}: expected ${expected}, computed ${actual}`);
  }
}

async function downloadTo(url, dest) {
  const res = await fetch(url);
  if (!res.ok || !res.body) {
    throw new Error(`Download fehlgeschlagen (${res.status}): ${url}`);
  }
  const partial = `${dest}.part`;
  await pipeline(Readable.fromWeb(res.body), fs.createWriteStream(partial));
  fs.renameSync(partial, dest);
}

async function ensureArchive(entry, release) {
  fs.mkdirSync(CACHE_DIR, { recursive: true });
  const archive = path.join(CACHE_DIR, entry.asset);
  if (fs.existsSync(archive)) {
    try {
      await verifySha256(archive, entry.sha256);
      return archive;
    } catch {
      fs.rmSync(archive, { force: true });
    }
  }
  const url = `https://github.com/astral-sh/python-build-standalone/releases/download/${release}/${encodeURIComponent(entry.asset)}`;
  console.log(`Lade ${entry.asset} ...`);
  await downloadTo(url, archive);
  await verifySha256(archive, entry.sha256);
  return archive;
}

// On Windows, Windows' own tar (bsdtar): the GNU tar from Git Bash reads "D:\..." as a remote host "D".
function tarCommand(platform = process.platform, env = process.env) {
  return platform === 'win32' ? path.win32.join(env.SystemRoot || 'C:\\Windows', 'System32', 'tar.exe') : 'tar';
}

function extractArchive(archive) {
  fs.rmSync(PYTHON_DIR, { recursive: true, force: true });
  // The archive holds a top-level directory python/, which is unpacked into build/.
  const res = spawnSync(tarCommand(), ['-xzf', archive, '-C', BUILD_DIR], { stdio: 'inherit' });
  if (res.error || res.status !== 0) {
    throw new Error(`Entpacken fehlgeschlagen: ${archive}`);
  }
}

function isolatedEnv() {
  const env = { ...process.env, PYTHONNOUSERSITE: '1' };
  delete env.PYTHONPATH;
  delete env.PYTHONHOME;
  return env;
}

function run(python, args) {
  return spawnSync(python, args, { stdio: 'inherit', env: isolatedEnv() });
}

function installPypff(python, key, pythonVersion, allowNoPypff) {
  console.log('Installing libpff-python (wheels only) ...');
  const res = run(python, ['-m', 'pip', 'install', '--only-binary=:all:', 'libpff-python']);
  if (res.status === 0) {
    return true;
  }
  const msg =
    `Could not install libpff-python as a wheel for ${key} / Python ${pythonVersion} ` +
    `(no matching wheel or a network error).`;
  if (allowNoPypff) {
    console.warn(`WARNING: ${msg} Building without pypff (--allow-no-pypff).`);
    return false;
  }
  throw new Error(`${msg} Use --allow-no-pypff to build without pypff.`);
}

function smokeTest(python, expectPypff) {
  if (run(python, ['-c', 'import sys; print(sys.version)']).status !== 0) {
    throw new Error(`Rauchtest fehlgeschlagen: ${python}`);
  }
  if (expectPypff && run(python, ['-c', 'import pypff']).status !== 0) {
    throw new Error('Smoke test failed: "import pypff" fails');
  }
}

function parseArgs(argv) {
  const opts = { target: null, allowNoPypff: false };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--target') {
      opts.target = argv[++i];
      if (!opts.target) throw new Error('--target braucht einen Wert, z. B. linux-x64');
    } else if (arg === '--allow-no-pypff') {
      opts.allowNoPypff = true;
    } else {
      throw new Error(`Unbekanntes Argument: ${arg}`);
    }
  }
  return opts;
}

async function main(argv) {
  const opts = parseArgs(argv);
  const key = opts.target ? targetKey(...opts.target.split('-', 2)) : targetKey(process.platform, process.arch);
  const entry = lock.targets[key];

  const archive = await ensureArchive(entry, lock.release);
  extractArchive(archive);

  const python = pythonBinary(key);
  if (!fs.existsSync(python)) {
    throw new Error(`Interpreter not found after unpacking: ${python}`);
  }

  const hasPypff = installPypff(python, key, lock.python, opts.allowNoPypff);
  smokeTest(python, hasPypff);
  console.log(`Fertig: ${python} (Python ${lock.python}, pypff ${hasPypff ? 'ja' : 'nein'})`);
}

module.exports = { targetKey, tarCommand, verifySha256 };

if (require.main === module) {
  main(process.argv.slice(2)).catch((err) => {
    console.error(err.message);
    process.exit(1);
  });
}
