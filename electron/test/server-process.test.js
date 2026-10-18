'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const os = require('node:os');
const { PREFERRED_PORT, ServerStartError, parsePort, startServer } = require('../src/server-process');

const APP_ROOT = path.resolve(__dirname, '..', '..');
const CONFIG = path.join(os.tmpdir(), 'mailviewer-test-config.json');
const SERVING = (port) => `Serving on http://127.0.0.1:${port}/ (Ctrl+C to stop)`;

// Fake-"python": `node -e <body> --`. Die Argumente nach `--` (also `-m mailviewer --port ...`)
// landen in process.argv ab Index 1 und werden vom Skript ausgewertet.
const fake = (body) => [process.execPath, '-e', `const args = process.argv.slice(1);\n${body}`, '--'];
const ARGV_PORT = "const port = args[args.indexOf('--port') + 1];";
const KEEP_ALIVE = 'setInterval(() => {}, 1000);';

test('PREFERRED_PORT ist 8025', () => {
  assert.equal(PREFERRED_PORT, 8025);
});

test('parsePort erkennt die Serving-Zeile', () => {
  assert.equal(parsePort(SERVING(8025)), 8025);
  assert.equal(parsePort('Serving on http://127.0.0.1:51234/'), 51234);
});

test('parsePort liefert null für andere Zeilen', () => {
  assert.equal(parsePort('Traceback (most recent call last):'), null);
  assert.equal(parsePort('error: [Errno 98] Address already in use'), null);
  assert.equal(parsePort(''), null);
});

test('startServer löst mit Port und URL aus der Serving-Zeile auf', async (t) => {
  const python = fake(`${ARGV_PORT}
setTimeout(() => process.stderr.write('Serving on http://127.0.0.1:' + port + '/ (Ctrl+C to stop)\\n'), 100);
${KEEP_ALIVE}`);
  const server = await startServer({ python, appRoot: APP_ROOT, configPath: CONFIG, timeoutMs: 5000 });
  t.after(() => server.stop());
  assert.equal(server.port, PREFERRED_PORT);
  assert.equal(server.url, `http://127.0.0.1:${PREFERRED_PORT}/`);
});

test('startServer übergibt -m mailviewer, --port, --config und extraArgs', async (t) => {
  const expected = `-m mailviewer --port ${PREFERRED_PORT} --config ${CONFIG} --cli x`;
  const python = fake(`if (args.join(' ') !== ${JSON.stringify(expected)}) process.exit(3);
process.stderr.write('${SERVING(PREFERRED_PORT)}\\n');
${KEEP_ALIVE}`);
  const server = await startServer({
    python, appRoot: APP_ROOT, configPath: CONFIG, extraArgs: ['--cli', 'x'], timeoutMs: 5000,
  });
  t.after(() => server.stop());
  assert.equal(server.port, PREFERRED_PORT);
});

test('startServer setzt PYTHONUNBUFFERED, PYTHONUTF8 und PYTHONPATH', async (t) => {
  const python = fake(`const ok = process.env.PYTHONUNBUFFERED === '1' && process.env.PYTHONUTF8 === '1'
  && process.env.PYTHONPATH === ${JSON.stringify(APP_ROOT)};
if (!ok) process.exit(4);
process.stderr.write('${SERVING(PREFERRED_PORT)}\\n');
${KEEP_ALIVE}`);
  const server = await startServer({ python, appRoot: APP_ROOT, configPath: CONFIG, timeoutMs: 5000 });
  t.after(() => server.stop());
  assert.equal(server.port, PREFERRED_PORT);
});

test('startServer isoliert die Umgebung: PYTHONNOUSERSITE=1, PYTHONHOME entfernt', async (t) => {
  const python = fake(`if (process.env.PYTHONHOME !== undefined) process.exit(5);
if (process.env.PYTHONNOUSERSITE !== '1') process.exit(6);
process.stderr.write('${SERVING(PREFERRED_PORT)}\\n');
${KEEP_ALIVE}`);
  const server = await startServer({
    python, appRoot: APP_ROOT, configPath: CONFIG, timeoutMs: 5000,
    env: { PYTHONHOME: '/nicht/vorhanden' },
  });
  t.after(() => server.stop());
  assert.equal(server.port, PREFERRED_PORT);
});

test('startServer scheitert sofort mit stderrTail, ohne timeoutMs abzuwarten', async () => {
  const python = fake(`process.stderr.write('boom\\n'); process.exit(1);`);
  const started = Date.now();
  await assert.rejects(
    startServer({ python, appRoot: APP_ROOT, configPath: CONFIG, timeoutMs: 10000 }),
    (err) => {
      assert.ok(err instanceof ServerStartError);
      assert.ok(err instanceof Error);
      assert.match(err.stderrTail, /boom/);
      return true;
    },
  );
  assert.ok(Date.now() - started < 5000, 'darf nicht bis timeoutMs warten');
});

test('startServer scheitert nach timeoutMs, wenn nichts ausgegeben wird', async () => {
  const python = fake(KEEP_ALIVE);
  const started = Date.now();
  await assert.rejects(
    startServer({ python, appRoot: APP_ROOT, configPath: CONFIG, timeoutMs: 300 }),
    (err) => err instanceof ServerStartError,
  );
  assert.ok(Date.now() - started < 3000);
});

test('startServer wiederholt mit --port 0, wenn 8025 belegt ist', async (t) => {
  const python = fake(`${ARGV_PORT}
if (port === '${PREFERRED_PORT}') {
  process.stderr.write('OSError: [Errno 98] Address already in use\\n');
  process.exit(1);
}
process.stderr.write('Serving on http://127.0.0.1:4321/ (Ctrl+C to stop)\\n');
${KEEP_ALIVE}`);
  const server = await startServer({ python, appRoot: APP_ROOT, configPath: CONFIG, timeoutMs: 5000 });
  t.after(() => server.stop());
  assert.equal(server.port, 4321);
  assert.equal(server.url, 'http://127.0.0.1:4321/');
});

test('stop() beendet den Prozess vollständig und ist idempotent', async (t) => {
  const python = fake(`${ARGV_PORT}
process.stderr.write('${SERVING(PREFERRED_PORT)}\\n');
${KEEP_ALIVE}`);
  const server = await startServer({ python, appRoot: APP_ROOT, configPath: CONFIG, timeoutMs: 5000 });
  t.after(() => server.stop());
  assert.equal(typeof server.pid, 'number');
  await server.stop();
  assert.throws(() => process.kill(server.pid, 0));
  await server.stop();
});
