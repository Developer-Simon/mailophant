'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { installHint } = require('../src/install-hint');

test('installHint im Systemmodus verweist auf das Werkzeuge-Menü', () => {
  const text = installHint('system');
  assert.match(text, /pypff/);
  assert.match(text, /Werkzeuge/);
  assert.match(text, /PST-Unterstützung erneut installieren/);
});

test('installHint im Bundled-Modus verweist auf die full-Variante', () => {
  const text = installHint('bundled');
  assert.match(text, /pypff/);
  assert.match(text, /full/);
});

test('installHint unterscheidet die beiden Modi', () => {
  assert.notEqual(installHint('system'), installHint('bundled'));
});
