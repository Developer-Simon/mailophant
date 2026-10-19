'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const {
  bundledPython,
  resolveRuntime,
  RuntimeUnavailableError,
} = require('../src/resolve-runtime');

const RESOURCES = '/opt/app/resources';
const USER_DATA = '/home/u/.config/mailviewer';
const SYS_PROBE = { cmd: ['python3'], version: [3, 12, 1] };
const VENV_PY = path.posix.join(USER_DATA, 'venv', 'bin', 'python');
const BUNDLED = path.posix.join(RESOURCES, 'python', 'bin', 'python3');
const NO_VENV_REASON = 'venv/ensurepip fehlt (unter Debian/Ubuntu: Paket python3-venv installieren)';

// Fakes: kein Netzwerk, kein echtes Python.
function fakes({ found = SYS_PROBE, ensureError = null, bundledExists = false, runError = null } = {}) {
  const calls = { ensure: [], run: [] };
  const deps = {
    find: async () => ({
      python: found ? { cmd: found.cmd, version: found.version } : null,
      rejected: found ? [] : [{ cmd: ['python3'], reason: NO_VENV_REASON }],
    }),
    ensure: async (opts) => {
      calls.ensure.push(opts);
      if (ensureError) throw ensureError;
      return { python: VENV_PY, pypff: true, rebuilt: true };
    },
    exists: async (p) => {
      assert.equal(p, BUNDLED);
      return bundledExists;
    },
    run: async (file, args, opts) => {
      calls.run.push({ file, args, opts });
      if (runError) throw runError;
      return { stdout: '', stderr: '' };
    },
  };
  return { deps, calls };
}

const resolve = (deps, extra = {}) =>
  resolveRuntime({ userDataDir: USER_DATA, resourcesDir: RESOURCES, deps, ...extra });

test('bundledPython liefert den Pfad je Plattform', () => {
  assert.equal(bundledPython(RESOURCES, 'linux'), path.posix.join(RESOURCES, 'python', 'bin', 'python3'));
  assert.equal(bundledPython('C:\\app\\resources', 'win32'), path.win32.join('C:\\app\\resources', 'python', 'python.exe'));
});

test('System-Python vorhanden und ensure gelingt -> mode system, python ist [venvPython]', async () => {
  const { deps, calls } = fakes();
  const result = await resolve(deps);
  assert.deepEqual(result, { python: [VENV_PY], mode: 'system', pypff: true });
  assert.equal(calls.ensure.length, 1);
  assert.equal(calls.ensure[0].venvDir, path.join(USER_DATA, 'venv'));
  assert.deepEqual(calls.ensure[0].probe, SYS_PROBE);
  assert.equal(calls.run.length, 0, 'bei System-Modus wird kein pypff-Check nötig');
});

test('System-Python vorhanden, ensure wirft, Bundled existiert -> mode bundled', async () => {
  const { deps } = fakes({ ensureError: new Error('venv kaputt'), bundledExists: true });
  const result = await resolve(deps);
  assert.deepEqual(result, { python: [BUNDLED], mode: 'bundled', pypff: true });
});

test('kein System-Python, Bundled existiert -> mode bundled', async () => {
  const { deps, calls } = fakes({ found: null, bundledExists: true });
  const result = await resolve(deps);
  assert.deepEqual(result, { python: [BUNDLED], mode: 'bundled', pypff: true });
  assert.equal(calls.ensure.length, 0);
  assert.deepEqual(calls.run[0].file, BUNDLED);
  assert.deepEqual(calls.run[0].args, ['-c', 'import pypff']);
});

test('Bundled ohne pypff -> pypff false, kein Fehler', async () => {
  const { deps } = fakes({
    found: null,
    bundledExists: true,
    runError: Object.assign(new Error('ModuleNotFoundError'), { code: 1 }),
  });
  const result = await resolve(deps);
  assert.deepEqual(result, { python: [BUNDLED], mode: 'bundled', pypff: false });
});

test('nichts vorhanden -> RuntimeUnavailableError mit Ablehnungsgrund', async () => {
  const { deps } = fakes({ found: null, bundledExists: false });
  await assert.rejects(resolve(deps), (err) => {
    assert.ok(err instanceof RuntimeUnavailableError);
    assert.equal(err.name, 'RuntimeUnavailableError');
    assert.match(err.message, /python3-venv/);
    assert.match(err.message, /python3/);
    assert.match(err.message, /full/);
    return true;
  });
});

test('MAILOPHANT_PYTHON gesetzt -> dieser Interpreter ohne venv, mode bundled, pypff geprüft', async () => {
  const { deps, calls } = fakes();
  deps.env = { MAILOPHANT_PYTHON: '/usr/bin/python3' };
  const result = await resolve(deps);
  assert.deepEqual(result, { python: ['/usr/bin/python3'], mode: 'bundled', pypff: true });
  assert.equal(calls.ensure.length, 0);
  assert.deepEqual(calls.run.map((c) => c.file), ['/usr/bin/python3']);
});

test('MAILOPHANT_PYTHON ohne pypff -> pypff false', async () => {
  const { deps } = fakes({ runError: new Error('No module named pypff') });
  deps.env = { MAILOPHANT_PYTHON: '/usr/bin/python3' };
  assert.equal((await resolve(deps)).pypff, false);
});
