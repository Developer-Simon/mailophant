'use strict';

const http = require('node:http');

const SMOKE_TIMEOUT_MS = 10 * 1000;

/**
 * Requests the server's start page. Success: status 200 and an HTML document with <title.
 * A network error or timeout counts as failure; the function doesn't throw.
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
