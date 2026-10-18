'use strict';

const path = require('node:path');
const { app, BrowserWindow, Menu, dialog, nativeTheme, shell } = require('electron');
const { resolveRuntime } = require('./resolve-runtime');
const { findSystemPython } = require('./python-locator');
const { ensureVenv } = require('./venv-manager');
const { startServer, ServerStartError } = require('./server-process');
const { runSmoke } = require('./smoke');
const { installHint } = require('./install-hint');
const { migrateLegacyConfig } = require('./legacy-config');

const WEB_PREFERENCES = { contextIsolation: true, nodeIntegration: false, sandbox: true };
const TITLE = 'Mailophant';
/** Höhe der eigenen Kopfzeile (Ampel/Overlay-Buttons sitzen darin). */
const TITLEBAR_HEIGHT = 36;
const ICON_PATH = path.join(__dirname, '..', 'assets', 'icon.png');
/** `--smoke`: Server starten, GET / prüfen, ohne Dialoge mit Exitcode 0 bzw. 1 beenden. */
const SMOKE = process.argv.includes('--smoke');

// Der Namen vor dem ersten Start setzen: entspricht dem productName der Pakete.
app.setName(TITLE);

/**
 * Farben der Titelleiste (Overlay unter Windows/Linux, Ladefenster), abgeleitet aus static/css/app.css:
 * color = --sidebar (hell #f0f0f3, dunkel #232326), symbolColor = --label (hell #1d1d1f, dunkel #f5f5f7).
 * Die Leiste liegt über der Seitenleiste; sie muss zu den Werten im Dark-Theme-Block dort passen.
 * @param {boolean} dark
 * @returns {{ color: string, symbolColor: string }}
 */
function chromeColors(dark) {
  return dark
    ? { color: '#232326', symbolColor: '#f5f5f7' }
    : { color: '#f0f0f3', symbolColor: '#1d1d1f' };
}

/** Hintergrund der Fenster, passend zu --content (sonst kurzer weißer Blitz im Dark-Theme). */
function contentBackground() {
  return nativeTheme.shouldUseDarkColors ? '#1c1c1e' : '#ffffff';
}

/** Gemeinsame Fensteroptionen für rahmenlose Fenster mit eigener Kopfzeile. */
function titleBarOptions() {
  if (process.platform === 'darwin') {
    return { titleBarStyle: 'hidden', trafficLightPosition: { x: 16, y: 11 } };
  }
  return {
    titleBarStyle: 'hidden',
    titleBarOverlay: { ...chromeColors(nativeTheme.shouldUseDarkColors), height: TITLEBAR_HEIGHT },
  };
}

/** Überschreibt die Overlay-Farben offener Fenster, wenn das System-Theme wechselt (nur Windows/Linux). */
function applyChromeColors() {
  if (process.platform === 'darwin') return;
  const colors = chromeColors(nativeTheme.shouldUseDarkColors);
  for (const win of [mainWindow, loadingWindow]) {
    if (!win || win.isDestroyed()) continue;
    try {
      win.setTitleBarOverlay({ ...colors, height: TITLEBAR_HEIGHT });
    } catch {
      // Overlay ist auf dieser Plattform/Version nicht verfügbar; die Leiste behält dann ihre Farben.
    }
  }
}

/** @type {BrowserWindow | null} */
let loadingWindow = null;
/** @type {BrowserWindow | null} */
let mainWindow = null;
/** @type {{ url: string, stop: () => Promise<void> } | null} */
let server = null;
let serverUrl = null;
/** @type {{ python: string[], mode: string, pypff: boolean } | null} */
let runtime = null;
let quitting = false;
/** Laufende Start-/Neustart-Operation; onBeforeQuit wartet darauf. */
let backendOp = null;

function getAppRoot() {
  return app.isPackaged ? process.resourcesPath : path.resolve(__dirname, '..', '..');
}

function getConfigPath() {
  return path.join(app.getPath('userData'), 'config.json');
}

function getVenvDir() {
  return path.join(app.getPath('userData'), 'venv');
}

function messageOf(err) {
  return err && err.message ? err.message : String(err);
}

/** Fehlertext für den Dialog; bei ServerStartError samt Ausgabe des Servers. */
function describeError(err) {
  if (err instanceof ServerStartError && err.stderrTail) {
    return `${err.message}\n\nLetzte Ausgabe des Servers:\n${err.stderrTail}`;
  }
  return messageOf(err);
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  })[c]);
}

function loadingPage(text) {
  const light = chromeColors(false);
  const dark = chromeColors(true);
  // Kopfzeile wie im Hauptfenster: ziehbar, Farben spiegeln chromeColors() (Light/Dark per prefers-color-scheme).
  const html =
    '<!doctype html><html lang="de"><head><meta charset="utf-8"><title>' + TITLE + '</title>' +
    '<style>' +
    ':root{--bar:' + light.color + ';--bar-ink:' + light.symbolColor + ';--content:#ffffff;--ink:#1d1d1f}' +
    '@media (prefers-color-scheme: dark){:root{--bar:' + dark.color + ';--bar-ink:' + dark.symbolColor +
    ';--content:#1c1c1e;--ink:#f5f5f7}}' +
    'html,body{margin:0;height:100%}' +
    'body{display:flex;flex-direction:column;font-family:system-ui,sans-serif;background:var(--content);color:var(--ink)}' +
    '.bar{flex:none;height:' + TITLEBAR_HEIGHT + 'px;display:flex;align-items:center;justify-content:center;' +
    'background:var(--bar);color:var(--bar-ink);font-size:13px;-webkit-app-region:drag;user-select:none}' +
    '.main{flex:1;display:flex;align-items:center;justify-content:center}' +
    'p{margin:0 24px;text-align:center;font-size:15px}' +
    '</style></head><body><header class="bar">' + TITLE + '</header>' +
    '<div class="main"><p>' + escapeHtml(text) + '</p></div></body></html>';
  return `data:text/html;charset=utf-8,${encodeURIComponent(html)}`;
}

/** Ladefenster anlegen bzw. den Fortschrittstext aktualisieren. */
function showLoading(text) {
  if (quitting) return;
  if (!loadingWindow) {
    loadingWindow = new BrowserWindow({
      width: 420,
      height: 200,
      resizable: false,
      autoHideMenuBar: true,
      title: TITLE,
      icon: ICON_PATH,
      backgroundColor: contentBackground(),
      ...titleBarOptions(),
      webPreferences: WEB_PREFERENCES,
    });
    loadingWindow.on('closed', () => {
      loadingWindow = null;
    });
  }
  // Ein neuer loadURL bricht den vorherigen ab; das wird hier bewusst ignoriert.
  loadingWindow.loadURL(loadingPage(text)).catch(() => {});
}

function closeLoading() {
  if (!loadingWindow) return;
  const win = loadingWindow;
  loadingWindow = null;
  win.destroy();
}

function isSameOrigin(url) {
  try {
    return new URL(url).origin === new URL(serverUrl).origin;
  } catch {
    return false;
  }
}

function isWebUrl(url) {
  try {
    const { protocol } = new URL(url);
    return protocol === 'http:' || protocol === 'https:';
  } catch {
    return false;
  }
}

/** Externe Web-Links im Systembrowser öffnen; alles andere wird verweigert. */
function openInSystemBrowser(url) {
  if (isWebUrl(url)) shell.openExternal(url);
}

function guardNavigation(event, url) {
  if (isSameOrigin(url)) return;
  event.preventDefault();
  openInSystemBrowser(url);
}

/** Hauptfenster mit dem Viewer; Navigation und neue Fenster sind auf den Server-Origin begrenzt. */
function openMainWindow(url) {
  if (quitting) return;
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 840,
    show: false,
    title: TITLE,
    icon: ICON_PATH,
    backgroundColor: contentBackground(),
    // Menü bleibt verborgen; die Alt-Taste blendet es ein (dort: Werkzeuge → PST-Unterstützung).
    autoHideMenuBar: true,
    ...titleBarOptions(),
    webPreferences: WEB_PREFERENCES,
  });
  const contents = mainWindow.webContents;
  contents.on('will-navigate', guardNavigation);
  contents.on('will-redirect', guardNavigation);
  // Anhänge öffnen sich per target=_blank; sie laufen im Systembrowser, kein neues Electron-Fenster.
  contents.setWindowOpenHandler(({ url: target }) => {
    openInSystemBrowser(target);
    return { action: 'deny' };
  });
  mainWindow.once('ready-to-show', () => mainWindow && mainWindow.show());
  mainWindow.on('closed', () => {
    mainWindow = null;
  });
  mainWindow.loadURL(url).catch((err) => console.error('Viewer konnte nicht geladen werden:', err));
}

function focusWindow() {
  const win = mainWindow ?? loadingWindow;
  if (!win) return;
  if (win.isMinimized()) win.restore();
  win.focus();
}

async function stopServer() {
  const current = server;
  server = null;
  if (current) await current.stop();
}

/**
 * Führt eine Start-/Neustart-Operation aus. Solange eine läuft (oder die App beendet wird),
 * wird jeder weitere Aufruf ignoriert und null geliefert.
 * @param {() => Promise<void>} fn
 * @returns {Promise<void> | null}
 */
function runExclusive(fn) {
  if (backendOp || quitting) return null;
  backendOp = (async () => {
    try {
      await fn();
    } finally {
      backendOp = null;
    }
  })();
  return backendOp;
}

/** Startet den Server mit der gewählten Laufzeit. Stoppt ihn sofort, falls die App inzwischen beendet wird. */
async function startBackend() {
  const handle = await startServer({
    python: runtime.python,
    appRoot: getAppRoot(),
    configPath: getConfigPath(),
    env: { MAILVIEWER_INSTALL_HINT: installHint(runtime.mode) },
  });
  server = handle;
  serverUrl = handle.url;
  if (quitting) await stopServer();
  return handle;
}

/** Server neu starten und das Hauptfenster neu laden. */
async function restartBackend() {
  await stopServer();
  showLoading('Starte Viewer …');
  await startBackend();
  if (quitting) return;
  if (mainWindow) {
    mainWindow.loadURL(serverUrl).catch((err) => console.error('Viewer konnte nicht geladen werden:', err));
  } else {
    openMainWindow(serverUrl);
  }
  closeLoading();
}

/** Menüpunkt: PST-Unterstützung (pypff) im System-venv nachinstallieren, dann Server neu starten. */
async function reinstallPst() {
  try {
    const found = await findSystemPython();
    if (!found.python) {
      throw new Error(
        'Kein System-Python gefunden. Bitte Python 3.10 oder neuer mit venv installieren.',
      );
    }
    showLoading('Installiere PST-Unterstützung …');
    await ensureVenv({
      probe: found.python,
      venvDir: getVenvDir(),
      onProgress: showLoading,
      retryPypff: true,
    });
    await restartBackend();
  } catch (err) {
    closeLoading();
    dialog.showErrorBox('PST-Unterstützung', describeError(err));
    // Ohne laufenden Server ist der Viewer nicht mehr nutzbar.
    if (!server) app.quit();
  }
}

function buildMenu() {
  const isMac = process.platform === 'darwin';
  const template = [
    ...(isMac ? [{ role: 'appMenu' }] : []),
    { role: 'fileMenu' },
    { role: 'editMenu' },
    { role: 'viewMenu' },
    { role: 'windowMenu' },
    {
      label: 'Werkzeuge',
      submenu: [
        {
          label: 'PST-Unterstützung erneut installieren',
          enabled: runtime !== null && runtime.mode === 'system',
          click: () => runExclusive(reinstallPst),
        },
      ],
    },
  ];
  return Menu.buildFromTemplate(template);
}

/**
 * Übernimmt die config.json eines früheren Programmnamens (userData-Ordner vor der Umbenennung).
 * Fehler werden nur protokolliert; der Start läuft in jedem Fall weiter.
 */
async function importLegacyConfig() {
  try {
    const source = await migrateLegacyConfig({
      appDataDir: app.getPath('appData'),
      userDataDir: app.getPath('userData'),
    });
    if (source) console.log(`Konfiguration übernommen aus: ${source}`);
  } catch (err) {
    console.error('Alte Konfiguration konnte nicht übernommen werden:', err);
  }
}

async function startup() {
  await importLegacyConfig();
  showLoading('Suche Python-Umgebung …');
  runtime = await resolveRuntime({
    userDataDir: app.getPath('userData'),
    resourcesDir: process.resourcesPath,
    onProgress: showLoading,
  });
  Menu.setApplicationMenu(buildMenu());
  showLoading('Starte Viewer …');
  await startBackend();
  if (quitting) return;
  if (SMOKE) {
    await finishSmoke();
    return;
  }
  openMainWindow(serverUrl);
  closeLoading();
}

/** Rauchtest: GET / gegen den laufenden Server, dann Server stoppen und mit 0 bzw. 1 beenden. */
async function finishSmoke() {
  const ok = await runSmoke(serverUrl);
  console.log(ok ? `Rauchtest bestanden: ${serverUrl}` : `Rauchtest fehlgeschlagen: ${serverUrl}`);
  await stopServer();
  // Das Ladefenster bleibt bis app.exit offen, damit kein window-all-closed-Quit dazwischenfunkt.
  app.exit(ok ? 0 : 1);
}

function onBeforeQuit(event) {
  if (quitting) return;
  quitting = true;
  event.preventDefault();
  // Eine laufende Start-/Neustart-Operation zuerst abwarten (Fehler ignorieren), damit danach kein Server übrig bleibt.
  const pending = backendOp ? backendOp.catch(() => {}) : Promise.resolve();
  pending
    .then(() => stopServer())
    .catch((err) => console.error('Server konnte nicht beendet werden:', err))
    .finally(() => app.quit());
}

async function run() {
  await app.whenReady();
  nativeTheme.on('updated', applyChromeColors);
  try {
    await runExclusive(startup);
  } catch (err) {
    if (SMOKE) {
      console.error(describeError(err));
      // app.exit umgeht before-quit; server ist hier noch null, daher ist nichts zu stoppen.
      app.exit(1);
      return;
    }
    closeLoading();
    // Während des Beendens kein Fehlerdialog; onBeforeQuit beendet die App ohnehin.
    if (quitting) return;
    dialog.showErrorBox('Start fehlgeschlagen', describeError(err));
    // app.exit umgeht before-quit; server ist hier noch null, daher ist nichts zu stoppen.
    app.exit(1);
  }
}

if (!app.requestSingleInstanceLock()) {
  if (SMOKE) {
    console.error('Eine andere Instanz läuft bereits.');
    app.exit(1);
  } else {
    app.quit();
  }
} else {
  app.on('second-instance', focusWindow);
  app.on('before-quit', onBeforeQuit);
  app.on('window-all-closed', () => app.quit());
  run();
}
