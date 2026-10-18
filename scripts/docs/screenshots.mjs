#!/usr/bin/env node
// Takes the README screenshots (docs/images/*.png) from a demo library with made-up mails.
//
//   cd scripts/docs && npm install && npx playwright install chromium
//   node screenshots.mjs [NAME ...]
//
// Builds the demo library with demo_library.py in a temp folder, starts the server on it with its own
// config file (the user's remembered library stays untouched), and stops it again at the end.
// Names limit the run to those images.
import { chromium } from 'playwright';
import { spawn, execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '../..');
const OUT = path.join(REPO, 'docs/images');
const PORT = 18025;
const URL = `http://127.0.0.1:${PORT}/`;
const PYTHON = process.env.PYTHON || 'python3';

function startServer(library, config) {
  const proc = spawn(PYTHON, ['-u', '-m', 'mailviewer', library, '--port', String(PORT), '--config', config], {
    cwd: REPO, env: { ...process.env, PYTHONPATH: REPO }, stdio: ['ignore', 'inherit', 'pipe'],
  });
  return new Promise((resolve, reject) => {
    proc.stderr.on('data', (d) => {
      if (String(d).includes('Serving on')) resolve(proc); // the request log after it stays quiet
    });
    proc.on('exit', (code) => reject(new Error(`server exited with ${code}`)));
  });
}

const row = (page, text) => page.locator('#messages li.msg', { hasText: text }).first();
const rail = (page, name) => page.locator(`#rail-sources button[aria-label="${name}"]`);
const folder = (page, name) => page.locator('#folder-list button.folder', { hasText: name }).first();
const tag = (page, name) => page.locator('#tag-list button.folder', { hasText: name }).first();

async function openMail(page, text) {
  await row(page, text).click();
  await page.locator('#mail:not([hidden]) #m-subject', { hasText: text }).waitFor();
  await page.waitForTimeout(400); // the HTML body renders in an iframe
}

// name -> [viewport options, steps]
const SHOTS = {
  'overview': [{}, async (page) => {
    await openMail(page, 'Your stay is confirmed');
  }],
  'overview-dark': [{ colorScheme: 'dark' }, async (page) => {
    await openMail(page, 'Five bulbs to plant');
  }],
  'sources': [{}, async (page) => {
    await openMail(page, 'Your flight to Lisbon');
  }],
  'archive': [{}, async (page) => {
    await rail(page, 'Family archive').click();
    await folder(page, '2024 Porto').click();
    await openMail(page, 'Porto, here we come');
  }],
  'tags': [{}, async (page) => {
    await tag(page, 'Travel').click();
    await openMail(page, 'Photos from Saturday');
  }],
  'search': [{}, async (page) => {
    await folder(page, 'All Mail').click();
    await page.fill('#search', 'jamie');
    await page.waitForTimeout(800);
    await openMail(page, 'Holiday photos 2014');
  }],
  'preview': [{}, async (page) => {
    await openMail(page, 'Photos from Saturday');
    await page.locator('#m-attachments a[data-preview="alfama.png"]').click();
    await page.locator('#preview-dialog[open]').waitFor();
    await page.waitForTimeout(600);
  }],
  'settings': [{}, async (page) => {
    await page.click('#rail-settings');
    await page.locator('#settings-dialog[open]').waitFor();
    await page.locator('#settings-sources-title').evaluate((h) => h.scrollIntoView({ block: 'start' }));
    await page.waitForTimeout(400);
  }],
  'narrow-list': [{ viewport: { width: 390, height: 780 } }, async (page) => {
    await folder(page, 'Inbox').click();
    await page.waitForTimeout(800); // the stacked panes slide in
  }],
  'narrow-mail': [{ viewport: { width: 390, height: 780 } }, async (page) => {
    await folder(page, 'Inbox').click();
    await page.waitForTimeout(800);
    await row(page, 'Re: Dinner on Friday').click();
    await page.waitForTimeout(800);
  }],
};

const wanted = process.argv.slice(2);
for (const name of wanted) if (!SHOTS[name]) throw new Error(`unknown screenshot: ${name}`);

const tmp = mkdtempSync(path.join(tmpdir(), 'mailophant-shots-'));
let server;
let browser;
try {
  const library = execFileSync(PYTHON, [path.join(HERE, 'demo_library.py'), path.join(tmp, 'demo')], { encoding: 'utf8' }).trim();
  const config = path.join(tmp, 'config.json');
  writeFileSync(config, JSON.stringify({ language: 'en' }));
  server = await startServer(library, config);
  mkdirSync(OUT, { recursive: true });
  browser = await chromium.launch();
  for (const [name, [options, steps]] of Object.entries(SHOTS)) {
    if (wanted.length && !wanted.includes(name)) continue;
    const context = await browser.newContext({
      viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2, colorScheme: 'light', locale: 'en-GB', ...options,
    });
    const page = await context.newPage();
    await page.goto(URL);
    await row(page, 'Your stay is confirmed').waitFor({ state: 'attached' });
    await steps(page);
    const file = path.join(OUT, `${name}.png`);
    await page.screenshot({ path: file });
    console.log(path.relative(REPO, file));
    await context.close();
  }
} finally {
  await browser?.close();
  server?.kill();
  rmSync(tmp, { recursive: true, force: true });
}
