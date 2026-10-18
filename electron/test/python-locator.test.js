'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { MIN_PYTHON, probePython, findSystemPython } = require('../src/python-locator');

const okOutput = (v, venv = true) => ({
  stdout: JSON.stringify({ v, venv }) + '\n',
  stderr: '',
});

const enoent = (file) =>
  Object.assign(new Error(`spawn ${file} ENOENT`), { code: 'ENOENT' });

// Fake-Runner: ruft `respond(file, cmdArgs, opts)` auf und gibt dessen Ergebnis oder Fehler zurück.
// Zeichnet die Aufrufe als [file, ...cmdArgs] auf (ohne das Probe-Skript ab `-c`).
function fakeRun(respond) {
  const calls = [];
  const run = async (file, args, opts) => {
    const cmdArgs = args.slice(0, args.indexOf('-c'));
    calls.push([file, ...cmdArgs]);
    return respond(file, cmdArgs, opts);
  };
  return { run, calls };
}

test('MIN_PYTHON ist 3.10', () => {
  assert.deepEqual(MIN_PYTHON, [3, 10]);
});

test('probePython akzeptiert Python 3.12.4', async () => {
  const { run } = fakeRun(() => okOutput([3, 12, 4]));
  const result = await probePython(['python3'], run);
  assert.equal(result.ok, true);
  assert.deepEqual(result.probe.version, [3, 12, 4]);
  assert.deepEqual(result.probe.cmd, ['python3']);
});

test('probePython lehnt Python 3.9.18 mit Hinweis auf 3.10 ab', async () => {
  const { run } = fakeRun(() => okOutput([3, 9, 18]));
  const result = await probePython(['python3'], run);
  assert.equal(result.ok, false);
  assert.match(result.reason, /3\.10/);
});

test('probePython meldet ENOENT als nicht ok', async () => {
  const { run } = fakeRun((file) => {
    throw enoent(file);
  });
  const result = await probePython(['python3'], run);
  assert.equal(result.ok, false);
  assert.equal(typeof result.reason, 'string');
});

test('probePython meldet Timeout (killed) mit Zeitüberschreitung', async () => {
  const { run } = fakeRun(() => {
    throw Object.assign(new Error('Command failed'), { killed: true });
  });
  const result = await probePython(['python3'], run);
  assert.equal(result.ok, false);
  assert.match(result.reason, /Zeitüberschreitung/);
});

test('probePython meldet fehlendes ensurepip mit Hinweis auf python3-venv', async () => {
  const { run } = fakeRun(() => okOutput([3, 12, 4], false));
  const result = await probePython(['python3'], run);
  assert.equal(result.ok, false);
  assert.match(result.reason, /python3-venv/);
});

test('probePython führt cmd mit -c und Timeout 10 s aus', async () => {
  let seen;
  const { run } = fakeRun((file, cmdArgs, opts) => {
    seen = { file, args: cmdArgs, opts };
    return okOutput([3, 12, 4]);
  });
  await probePython(['py', '-3'], run);
  assert.equal(seen.file, 'py');
  assert.deepEqual(seen.args, ['-3']);
  assert.equal(seen.opts.timeout, 10000);
});

test('findSystemPython probiert unter win32 py -3, python, python3 in dieser Reihenfolge', async () => {
  const { run, calls } = fakeRun((file) => {
    if (file === 'py' || file === 'python') throw enoent(file);
    return okOutput([3, 11, 0]);
  });
  const result = await findSystemPython('win32', run);
  assert.deepEqual(calls, [
    ['py', '-3'],
    ['python'],
    ['python3'],
  ]);
  assert.deepEqual(result.python.cmd, ['python3']);
  assert.deepEqual(result.python.version, [3, 11, 0]);
  assert.deepEqual(
    result.rejected.map((r) => r.cmd),
    [['py', '-3'], ['python']],
  );
});

test('findSystemPython probiert unter linux python3, dann python', async () => {
  const { run, calls } = fakeRun((file) => {
    if (file === 'python3') throw enoent(file);
    return okOutput([3, 12, 4]);
  });
  const result = await findSystemPython('linux', run);
  assert.deepEqual(calls, [['python3'], ['python']]);
  assert.deepEqual(result.python.cmd, ['python']);
  assert.equal(result.rejected.length, 1);
  assert.deepEqual(result.rejected[0].cmd, ['python3']);
});

test('findSystemPython liefert null, wenn nichts Geeignetes gefunden wird', async () => {
  const { run } = fakeRun((file) => {
    throw enoent(file);
  });
  const result = await findSystemPython('linux', run);
  assert.equal(result.python, null);
  assert.equal(result.rejected.length, 2);
});
