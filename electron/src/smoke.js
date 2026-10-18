'use strict';

const http = require('node:http');

const SMOKE_TIMEOUT_MS = 10 * 1000;

/**
 * Ruft die Startseite des Servers ab. Erfolg: Status 200 und ein HTML-Dokument mit <title.
 * Ein Netzwerkfehler oder Timeout gilt als Misserfolg; die Funktion wirft nicht.
 * @param {string} url
 * @returns {Promise<boolean>}
 */
function runSmoke(url) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (ok) => {
      if (!settled) {
        settled = true;
        resolve(ok);
      }
    };

    const req = http.get(url, (res) => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => {
        body += chunk;
      });
      res.on('end', () => done(res.statusCode === 200 && /<title/.test(body)));
      res.on('error', () => done(false));
    });
    req.setTimeout(SMOKE_TIMEOUT_MS, () => {
      req.destroy();
      done(false);
    });
    req.on('error', () => done(false));
  });
}

module.exports = { runSmoke };
