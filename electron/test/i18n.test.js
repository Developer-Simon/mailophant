'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { resolveLanguage, readSetting, init, t, currentLanguage } = require('../src/i18n');

function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'mbv-i18n-'));
}

test('resolveLanguage folgt Einstellung, dann Systemsprache', () => {
  const av = ['en', 'de'];
  assert.equal(resolveLanguage('de', ['en-US'], av), 'de');
  assert.equal(resolveLanguage(null, ['de-CH'], av), 'de');
  assert.equal(resolveLanguage(null, ['fr-FR', 'de-DE'], av), 'en');
  assert.equal(resolveLanguage('fr', ['de-AT'], av), 'de');
  assert.equal(resolveLanguage(42, ['de'], av), 'de');
  assert.equal(resolveLanguage(null, [], av), 'en');
  assert.equal(resolveLanguage(null, undefined, av), 'en');
});

test('readSetting verträgt fehlende und beschädigte Dateien', () => {
  const dir = tmpDir();
  const file = path.join(dir, 'config.json');
  assert.equal(readSetting(file), null);
  fs.writeFileSync(file, '{nope');
  assert.equal(readSetting(file), null);
  fs.writeFileSync(file, '{"language": 42}');
  assert.equal(readSetting(file), null);
  fs.writeFileSync(file, '{"language": "de"}');
  assert.equal(readSetting(file), 'de');
});

test('t setzt Parameter ein und fällt auf Englisch zurück', () => {
  const dir = tmpDir();
  fs.writeFileSync(path.join(dir, 'languages.json'), '[{"code":"en","name":"English"},{"code":"de","name":"Deutsch"}]');
  fs.writeFileSync(path.join(dir, 'en.json'), JSON.stringify({ a: 'A {x}', only_en: 'Only English', n: { one: '{count} item', other: '{count} items' } }));
  fs.writeFileSync(path.join(dir, 'de.json'), JSON.stringify({ a: 'Ä {x}', n: { one: '{count} Ding', other: '{count} Dinge' } }));
  assert.equal(init({ dir, setting: 'de', preferred: [] }), 'de');
  assert.equal(currentLanguage(), 'de');
  assert.equal(t('a', { x: '{x}' }), 'Ä {x}'); // values are not expanded again
  assert.equal(t('only_en'), 'Only English');
  assert.equal(t('n', { count: 1 }), '1 Ding');
  assert.equal(t('unknown_key'), 'unknown_key');
  assert.equal(t('a'), 'Ä {x}'); // unknown placeholder stays
  assert.equal(init({ dir, setting: null, preferred: ['fr'] }), 'en');
});

test('Menütexte gibt es in allen Sprachen', () => {
  init({ setting: 'de', preferred: [] });
  assert.equal(t('electron.menu.tools'), 'Werkzeuge');
  init({ setting: 'en', preferred: [] });
  assert.equal(t('electron.menu.tools'), 'Tools');
  init({ setting: 'en', preferred: [] });
});
