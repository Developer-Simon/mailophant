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

// Fake runner: calls `respond(file, cmdArgs, opts)` and returns its result or error.
// Records the calls as [file, ...cmdArgs] (without the probe script from `-c` on).
function fakeRun(respond) {
  const calls = [];
  const run = async (file, args, opts) => {
    const cmdArgs = args.slice(0, args.indexOf('-c'));
    calls.push([file, ...cmdArgs]);
    return respond(file, cmdArgs, opts);
  };
  return { run, calls };
}

test('MIN_PYTHON is 3.10', () => {
  assert.deepEqual(MIN_PYTHON, [3, 10]);
});

test('probePython accepts Python 3.12.4', async () => {
  const { run } = fakeRun(() => okOutput([3, 12, 4]));
  const result = await probePython(['python3'], run);
  assert.equal(result.ok, true);
  assert.deepEqual(result.probe.version, [3, 12, 4]);
  assert.deepEqual(result.probe.cmd, ['python3']);
});

test('probePython rejects Python 3.9.18 and mentions 3.10', async () => {
  const { run } = fakeRun(() => okOutput([3, 9, 18]));
  const result = await probePython(['python3'], run);
  assert.equal(result.ok, false);
  assert.match(result.reason, /3\.10/);
});

test('probePython reports ENOENT as not ok', async () => {
  const { run } = fakeRun((file) => {
    throw enoent(file);
  });
  const result = await probePython(['python3'], run);
  assert.equal(result.ok, false);
  assert.equal(typeof result.reason, 'string');
});

test('probePython reports a timeout (killed) as timed out', async () => {
  const { run } = fakeRun(() => {
    throw Object.assign(new Error('Command failed'), { killed: true });
  });
  const result = await probePython(['python3'], run);
  assert.equal(result.ok, false);
  assert.match(result.reason, /timed out/i);
});

test('probePython reports missing ensurepip and mentions python3-venv', async () => {
  const { run } = fakeRun(() => okOutput([3, 12, 4], false));
  const result = await probePython(['python3'], run);
  assert.equal(result.ok, false);
  assert.match(result.reason, /python3-venv/);
});

test('probePython runs cmd with -c and a 10 s timeout', async () => {
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

test('findSystemPython tries py -3, python, python3 in this order on win32', async () => {
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

test('findSystemPython tries python3, then python on linux', async () => {
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

test('findSystemPython returns null when nothing suitable is found', async () => {
  const { run } = fakeRun((file) => {
    throw enoent(file);
  });
  const result = await findSystemPython('linux', run);
  assert.equal(result.python, null);
  assert.equal(result.rejected.length, 2);
});
