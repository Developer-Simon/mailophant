'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { enabled, pickerOptions, registerPicker } = require('../src/native-picker');

test('enabled only with MAILOPHANT_NATIVE_PICKER=1', () => {
  assert.equal(enabled({ MAILOPHANT_NATIVE_PICKER: '1' }), true);
  assert.equal(enabled({}), false);
});

test('pickerOptions: folder, or Outlook file with a filter', () => {
  assert.deepEqual(pickerOptions({ title: 'Wählen' }), { title: 'Wählen', properties: ['openDirectory', 'createDirectory'] });
  const file = pickerOptions({ kind: 'outlook', filterName: 'Outlook-Dateien' });
  assert.deepEqual(file.properties, ['openFile']);
  assert.deepEqual(file.filters, [{ name: 'Outlook-Dateien', extensions: ['pst', 'ost', 'PST', 'OST'] }]);
});

function harness(env, allowed = true) {
  const handlers = {};
  const ipcMain = { handle: (name, fn) => { handlers[name] = fn; } };
  const calls = [];
  const dialog = { showOpenDialog: async (win, opts) => { calls.push(opts); return { canceled: false, filePaths: ['/run/user/1000/doc/X/Mail'] }; } };
  registerPicker({ ipcMain, dialog, isAllowed: () => allowed, env });
  return { handlers, calls };
}

test('switched off: no dialog', async () => {
  const { handlers, calls } = harness({});
  const e = { senderFrame: { url: 'http://127.0.0.1:1/' } };
  assert.equal(await handlers['native-picker'](e), false);
  assert.equal(await handlers['pick-path'](e, {}), null);
  assert.equal(calls.length, 0);
});

test('foreign origin: no dialog', async () => {
  const { handlers, calls } = harness({ MAILOPHANT_NATIVE_PICKER: '1' }, false);
  const e = { senderFrame: { url: 'https://example.com/' } };
  assert.equal(await handlers['native-picker'](e), false);
  assert.equal(await handlers['pick-path'](e, {}), null);
  assert.equal(calls.length, 0);
});
