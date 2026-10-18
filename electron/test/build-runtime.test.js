'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { targetKey, tarCommand, verifySha256 } = require('../runtime/build-runtime.js');
const lock = require('../runtime/runtime.lock.json');

test('tarCommand: Windows-tar unter win32, sonst tar aus dem PATH', () => {
  assert.equal(tarCommand('win32', { SystemRoot: 'C:\\Windows' }), 'C:\\Windows\\System32\\tar.exe');
  assert.equal(tarCommand('linux', {}), 'tar');
});

test('targetKey joins platform and arch', () => {
  assert.equal(targetKey('linux', 'x64'), 'linux-x64');
  assert.equal(targetKey('darwin', 'arm64'), 'darwin-arm64');
  assert.equal(targetKey('win32', 'x64'), 'win32-x64');
});

test('targetKey throws for an unsupported combination', () => {
  assert.throws(() => targetKey('freebsd', 'x64'), /freebsd-x64/);
});

test('verifySha256 accepts the matching digest', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'rt-test-'));
  try {
    const file = path.join(dir, 'blob.bin');
    fs.writeFileSync(file, 'hello runtime');
    const digest = crypto.createHash('sha256').update('hello runtime').digest('hex');
    await verifySha256(file, digest);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('verifySha256 throws on mismatch and names both digests', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'rt-test-'));
  try {
    const file = path.join(dir, 'blob.bin');
    fs.writeFileSync(file, 'hello runtime');
    const actual = crypto.createHash('sha256').update('hello runtime').digest('hex');
    const expected = '0'.repeat(64);
    await assert.rejects(verifySha256(file, expected), (err) => {
      assert.match(err.message, new RegExp(expected));
      assert.match(err.message, new RegExp(actual));
      return true;
    });
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('lock file pins one install_only asset with a sha256 per supported target', () => {
  assert.match(lock.python, /^3\.(1[0-9]|[2-9][0-9])\.\d+$/);
  assert.ok(lock.release.length > 0);
  for (const key of ['linux-x64', 'darwin-arm64', 'darwin-x64', 'win32-x64']) {
    const entry = lock.targets[key];
    assert.ok(entry, `missing target ${key}`);
    assert.match(entry.asset, /-install_only\.tar\.gz$/);
    assert.match(entry.asset, new RegExp(`^cpython-${lock.python.replace(/\./g, '\\.')}\\+${lock.release}-`));
    assert.match(entry.sha256, /^[0-9a-f]{64}$/);
  }
});
