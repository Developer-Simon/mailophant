'use strict';

const { test, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { migrateLegacyConfig } = require('../src/legacy-config');

let root;
let appDataDir;
let userDataDir;

beforeEach(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), 'legacy-config-'));
  appDataDir = path.join(root, 'appData');
  userDataDir = path.join(appDataDir, 'Mailophant');
  fs.mkdirSync(appDataDir, { recursive: true });
});

afterEach(() => {
  fs.rmSync(root, { recursive: true, force: true });
});

function writeLegacy(dirName, content) {
  const dir = path.join(appDataDir, dirName);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'config.json'), content);
}

test('kopiert die config.json aus dem Ordner Mail-Backup-Viewer', async () => {
  writeLegacy('Mail-Backup-Viewer', '{"library":"/a"}');

  const source = await migrateLegacyConfig({ appDataDir, userDataDir });

  assert.equal(source, path.join(appDataDir, 'Mail-Backup-Viewer', 'config.json'));
  assert.equal(fs.readFileSync(path.join(userDataDir, 'config.json'), 'utf8'), '{"library":"/a"}');
});

test('überschreibt eine vorhandene config.json nicht', async () => {
  writeLegacy('Mail-Backup-Viewer', '{"library":"/alt"}');
  fs.mkdirSync(userDataDir, { recursive: true });
  fs.writeFileSync(path.join(userDataDir, 'config.json'), '{"library":"/neu"}');

  const source = await migrateLegacyConfig({ appDataDir, userDataDir });

  assert.equal(source, null);
  assert.equal(fs.readFileSync(path.join(userDataDir, 'config.json'), 'utf8'), '{"library":"/neu"}');
});

test('nimmt den ersten vorhandenen Altordner und lässt den zweiten unberührt', async () => {
  writeLegacy('Mail-Backup-Viewer', '{"from":"first"}');
  writeLegacy('mail-backup-viewer', '{"from":"second"}');

  const source = await migrateLegacyConfig({ appDataDir, userDataDir });

  assert.equal(source, path.join(appDataDir, 'Mail-Backup-Viewer', 'config.json'));
  assert.equal(fs.readFileSync(path.join(userDataDir, 'config.json'), 'utf8'), '{"from":"first"}');
  assert.equal(
    fs.readFileSync(path.join(appDataDir, 'mail-backup-viewer', 'config.json'), 'utf8'),
    '{"from":"second"}',
  );
});

test('nimmt den Kleinschreibungs-Ordner, wenn der andere fehlt', async () => {
  writeLegacy('mail-backup-viewer', '{"from":"lower"}');

  const source = await migrateLegacyConfig({ appDataDir, userDataDir });

  assert.equal(source, path.join(appDataDir, 'mail-backup-viewer', 'config.json'));
  assert.equal(fs.readFileSync(path.join(userDataDir, 'config.json'), 'utf8'), '{"from":"lower"}');
});

test('liefert null, wenn es keine Quelle gibt', async () => {
  fs.mkdirSync(path.join(appDataDir, 'Mail-Backup-Viewer'), { recursive: true });

  const source = await migrateLegacyConfig({ appDataDir, userDataDir });

  assert.equal(source, null);
  assert.equal(fs.existsSync(path.join(userDataDir, 'config.json')), false);
});
