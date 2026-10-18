'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const os = require('node:os');
const fs = require('node:fs/promises');
const { venvPython, ensureVenv } = require('../src/venv-manager');

const PROBE = { cmd: ['python3'], version: [3, 12, 4] };
const STAMP = 'mailviewer-stamp.json';
const PIP_WHEEL = ['-m', 'pip', 'install', '--only-binary=:all:', 'libpff-python'];
const PIP_SOURCE = ['-m', 'pip', 'install', 'libpff-python'];

async function setup(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'venv-test-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  return path.join(root, 'venv');
}

// Fake-Runner: legt bei `-m venv` den Interpreter im Temp-Verzeichnis an
// und lässt pip-Aufrufe je nach Vorgabe gelingen oder scheitern.
function makeRun({ venvFails = false, wheelFails = false, sourceFails = false } = {}) {
  const calls = [];
  const run = async (file, args, opts) => {
    calls.push({ file, args, timeout: opts && opts.timeout });
    if (args.includes('venv')) {
      if (venvFails) throw new Error('venv nicht möglich');
      const dir = args[args.length - 1];
      await fs.mkdir(path.dirname(venvPython(dir)), { recursive: true });
      await fs.writeFile(venvPython(dir), '');
      return { stdout: '', stderr: '' };
    }
    if (args.includes('pip')) {
      const wheel = args.includes('--only-binary=:all:');
      if (wheel ? wheelFails : sourceFails) throw new Error('pip fehlgeschlagen');
      return { stdout: '', stderr: '' };
    }
    throw new Error(`unerwarteter Aufruf: ${file} ${args.join(' ')}`);
  };
  return { run, calls };
}

async function readStamp(venvDir) {
  return JSON.parse(await fs.readFile(path.join(venvDir, STAMP), 'utf8'));
}

async function writeStamp(venvDir, stamp) {
  await fs.mkdir(venvDir, { recursive: true });
  await fs.writeFile(path.join(venvDir, STAMP), JSON.stringify(stamp));
}

async function exists(p) {
  try {
    await fs.access(p);
    return true;
  } catch {
    return false;
  }
}

test('venvPython liefert bin/python unter Unix und Scripts\\python.exe unter Windows', () => {
  assert.equal(venvPython('/x/venv', 'linux'), path.posix.join('/x/venv', 'bin', 'python'));
  assert.equal(venvPython('C:\\v', 'win32'), path.win32.join('C:\\v', 'Scripts', 'python.exe'));
});

test('erster Aufruf legt die venv an und installiert pypff per Wheel', async (t) => {
  const venvDir = await setup(t);
  const { run, calls } = makeRun();

  const result = await ensureVenv({ probe: PROBE, venvDir, run });

  assert.deepEqual(result, { python: venvPython(venvDir), pypff: true, rebuilt: true });
  assert.equal(calls.length, 2);
  assert.equal(calls[0].file, 'python3');
  assert.deepEqual(calls[0].args, ['-m', 'venv', venvDir]);
  assert.equal(calls[1].file, venvPython(venvDir));
  assert.deepEqual(calls[1].args, PIP_WHEEL);
  assert.deepEqual(await readStamp(venvDir), { python: '3.12', pypff: true, complete: true });
});

test('zweiter Aufruf mit gleichem Stamp macht keine Aufrufe', async (t) => {
  const venvDir = await setup(t);
  const { run, calls } = makeRun();
  await ensureVenv({ probe: PROBE, venvDir, run });
  calls.length = 0;

  const result = await ensureVenv({ probe: PROBE, venvDir, run });

  assert.deepEqual(result, { python: venvPython(venvDir), pypff: true, rebuilt: false });
  assert.equal(calls.length, 0);
});

test('Stamp mit complete: false führt zu einem Neuaufbau des Ordners', async (t) => {
  const venvDir = await setup(t);
  await writeStamp(venvDir, { python: '3.12', pypff: true, complete: false });
  await fs.mkdir(path.dirname(venvPython(venvDir)), { recursive: true });
  await fs.writeFile(venvPython(venvDir), '');
  await fs.writeFile(path.join(venvDir, 'stale.txt'), 'alt');
  const { run, calls } = makeRun();

  const result = await ensureVenv({ probe: PROBE, venvDir, run });

  assert.equal(result.rebuilt, true);
  assert.equal(await exists(path.join(venvDir, 'stale.txt')), false);
  assert.deepEqual(calls[0].args, ['-m', 'venv', venvDir]);
});

test('Stamp-Version 3.11 mit Probe 3.13 führt zu einem Neuaufbau', async (t) => {
  const venvDir = await setup(t);
  await writeStamp(venvDir, { python: '3.11', pypff: true, complete: true });
  await fs.mkdir(path.dirname(venvPython(venvDir)), { recursive: true });
  await fs.writeFile(venvPython(venvDir), '');
  const { run, calls } = makeRun();

  const result = await ensureVenv({
    probe: { cmd: ['python3'], version: [3, 13, 0] },
    venvDir,
    run,
  });

  assert.equal(result.rebuilt, true);
  assert.deepEqual(calls[0].args, ['-m', 'venv', venvDir]);
  assert.deepEqual(await readStamp(venvDir), { python: '3.13', pypff: true, complete: true });
});

test('Wheel schlägt fehl, Quellbuild gelingt: pypff true', async (t) => {
  const venvDir = await setup(t);
  const { run, calls } = makeRun({ wheelFails: true });

  const result = await ensureVenv({ probe: PROBE, venvDir, run });

  assert.deepEqual(result, { python: venvPython(venvDir), pypff: true, rebuilt: true });
  assert.deepEqual(calls[1].args, PIP_WHEEL);
  assert.deepEqual(calls[2].args, PIP_SOURCE);
  assert.equal(calls[2].timeout, 10 * 60 * 1000);
  assert.deepEqual(await readStamp(venvDir), { python: '3.12', pypff: true, complete: true });
});

test('Wheel und Quellbuild schlagen fehl: kein Wurf, pypff false im Stamp', async (t) => {
  const venvDir = await setup(t);
  const { run } = makeRun({ wheelFails: true, sourceFails: true });

  const result = await ensureVenv({ probe: PROBE, venvDir, run });

  assert.deepEqual(result, { python: venvPython(venvDir), pypff: false, rebuilt: true });
  assert.deepEqual(await readStamp(venvDir), { python: '3.12', pypff: false, complete: true });
});

test('Folgeaufruf bei pypff false macht keinen pip-Aufruf', async (t) => {
  const venvDir = await setup(t);
  await ensureVenv({ probe: PROBE, venvDir, run: makeRun({ wheelFails: true, sourceFails: true }).run });
  const { run, calls } = makeRun();

  const result = await ensureVenv({ probe: PROBE, venvDir, run });

  assert.deepEqual(result, { python: venvPython(venvDir), pypff: false, rebuilt: false });
  assert.equal(calls.length, 0);
});

test('retryPypff: true unternimmt genau einen Installationsversuch', async (t) => {
  const venvDir = await setup(t);
  await ensureVenv({ probe: PROBE, venvDir, run: makeRun({ wheelFails: true, sourceFails: true }).run });
  const { run, calls } = makeRun({ wheelFails: true, sourceFails: true });

  const result = await ensureVenv({ probe: PROBE, venvDir, run, retryPypff: true });

  assert.deepEqual(result, { python: venvPython(venvDir), pypff: false, rebuilt: false });
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[0].args, PIP_WHEEL);
  assert.deepEqual(calls[1].args, PIP_SOURCE);
});

test('retryPypff: true mit Erfolg aktualisiert den Stamp auf pypff true', async (t) => {
  const venvDir = await setup(t);
  await ensureVenv({ probe: PROBE, venvDir, run: makeRun({ wheelFails: true, sourceFails: true }).run });
  const { run, calls } = makeRun();

  const result = await ensureVenv({ probe: PROBE, venvDir, run, retryPypff: true });

  assert.deepEqual(result, { python: venvPython(venvDir), pypff: true, rebuilt: false });
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].args, PIP_WHEEL);
  assert.deepEqual(await readStamp(venvDir), { python: '3.12', pypff: true, complete: true });
});

test('venv-Anlage schlägt fehl: ensureVenv wirft', async (t) => {
  const venvDir = await setup(t);
  const { run } = makeRun({ venvFails: true });

  await assert.rejects(ensureVenv({ probe: PROBE, venvDir, run }), /venv nicht möglich/);
});

test('onProgress meldet die Schritte', async (t) => {
  const venvDir = await setup(t);
  const { run } = makeRun();
  const messages = [];

  await ensureVenv({ probe: PROBE, venvDir, run, onProgress: (m) => messages.push(m) });

  assert.deepEqual(messages, ['Creating Python environment …', 'Installing PST support …']);
});
