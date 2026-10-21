'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { findSystemPython } = require('../src/python-locator');
const { resolveRuntime } = require('../src/resolve-runtime');
const http = require('node:http');
const net = require('node:net');
const { startServer } = require('../src/server-process');
const { runSmoke } = require('../src/smoke');

const APP_ROOT = path.resolve(__dirname, '..', '..');

/** A free port that stops accepting connections right after. */
async function deadUrl() {
  const srv = net.createServer();
  await new Promise((resolve) => srv.listen(0, '127.0.0.1', resolve));
  const { port } = srv.address();
  await new Promise((resolve) => srv.close(resolve));
  return `http://127.0.0.1:${port}/`;
}

// Only when a system Python is present; otherwise the test is skipped.
const probeResult = findSystemPython().then((r) => r.python);

test('server with system Python: GET / and GET /api/config', async (t) => {
  const probe = await probeResult;
  if (!probe) {
    t.skip('no system Python found');
    return;
  }
  if (probe.cmd.length !== 1) {
    t.skip('the fake ensure only handles single-element commands');
    return;
  }

  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'mailviewer-int-'));
  t.after(() => fs.rmSync(userDataDir, { recursive: true, force: true }));
  const configPath = path.join(userDataDir, 'config.json');

  // Pass the real system Python through, but ensure without creating a venv or installing pypff.
  const runtime = await resolveRuntime({
    userDataDir,
    resourcesDir: path.join(userDataDir, 'no-resources'),
    deps: {
      find: async () => ({ python: probe, rejected: [] }),
      ensure: async ({ probe: p }) => ({ python: p.cmd[0], pypff: false, rebuilt: false }),
      exists: async () => false,
      run: async () => {
        throw new Error('no network in tests');
      },
    },
  });
  assert.equal(runtime.mode, 'system');
  assert.deepEqual(runtime.python, [probe.cmd[0]]);

  const server = await startServer({
    python: runtime.python,
    appRoot: APP_ROOT,
    configPath,
    timeoutMs: 15000,
  });
  t.after(() => server.stop());

  const page = await fetch(server.url);
  assert.equal(page.status, 200);
  assert.match(await page.text(), /<title/);

  const config = await fetch(`${server.url}api/config`);
  assert.equal(config.status, 200);
  const body = await config.json();
  assert.equal(body.configured, false);

  assert.equal(await runSmoke(server.url), true);

  await server.stop();
});

test('runSmoke: a dead port gives false', async () => {
  assert.equal(await runSmoke(await deadUrl()), false);
});

test('runSmoke: status ≠ 200 or no <title gives false', async (t) => {
  const srv = http.createServer((req, res) => {
    if (req.url === '/ok') {
      res.writeHead(200, { 'content-type': 'text/html' });
      res.end('<html><body>no title</body></html>');
    } else {
      res.writeHead(500, { 'content-type': 'text/html' });
      res.end('<title>Fehler</title>');
    }
  });
  await new Promise((resolve) => srv.listen(0, '127.0.0.1', resolve));
  t.after(() => srv.close());
  const { port } = srv.address();
  assert.equal(await runSmoke(`http://127.0.0.1:${port}/ok`), false);
  assert.equal(await runSmoke(`http://127.0.0.1:${port}/fehler`), false);
});
