'use strict';

const SYSTEM_HINT =
  'Für den PST-Import fehlt das Modul pypff. Bitte im Menü „Werkzeuge → PST-Unterstützung erneut installieren“ ' +
  'wählen (Internetverbindung nötig) und danach erneut importieren.';

const BUNDLED_HINT =
  'Diese App-Version enthält keine PST-Unterstützung. Bitte die „full“-Variante verwenden oder pypff in das ' +
  'mitgelieferte Python installieren.';

/**
 * Text für MAILVIEWER_INSTALL_HINT: wird im Server angezeigt, wenn pypff fehlt.
 * @param {'system' | 'bundled'} mode
 * @returns {string}
 */
function installHint(mode) {
  return mode === 'system' ? SYSTEM_HINT : BUNDLED_HINT;
}

module.exports = { installHint };
