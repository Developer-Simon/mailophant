'use strict';

const path = require('node:path');
const { app, BrowserWindow, Menu, dialog, ipcMain, nativeTheme, shell } = require('electron');
const { resolveRuntime } = require('./resolve-runtime');
const { findSystemPython } = require('./python-locator');
const { ensureVenv } = require('./venv-manager');
const { startServer, ServerStartError } = require('./server-process');
const { runSmoke } = require('./smoke');
const i18n = require('./i18n');
const { t } = i18n;
const { migrateLegacyConfig } = require('./legacy-config');
const { registerPicker } = require('./native-picker');

const WEB_PREFERENCES = {
  contextIsolation: true, nodeIntegration: false, sandbox: true, preload: path.join(__dirname, 'preload.js'),
};
const TITLE = 'Mailophant';
/** Height of the custom title bar (the traffic lights / overlay buttons sit in it). */
const TITLEBAR_HEIGHT = 36;
const ICON_PATH = path.join(__dirname, '..', 'assets', 'icon.png');
/** `--smoke`: start the server, check GET /, quit without dialogs with exit code 0 or 1. */
const SMOKE = process.argv.includes('--smoke');

// Set the name before the first start: matches the packages' productName.
app.setName(TITLE);
// In the Flatpak the Wayland app_id must match the Flatpak ID, otherwise the shell doesn't match the window
// to its desktop file (icon, name). Without desktopName, Electron sets "mailophant.desktop" itself.
if (process.env.FLATPAK_ID) app.setDesktopName(`${process.env.FLATPAK_ID}.desktop`);

/**
 * Title bar colours (overlay on Windows/Linux, loading window), taken from static/css/app.css:
 * color = --sidebar (light #f0f0f3, dark #232326), symbolColor = --label (light #1d1d1f, dark #f5f5f7).
 * The bar sits above the sidebar; it must match the values in the dark theme block there.
 * @param {boolean} dark
 * @returns {{ color: string, symbolColor: string }}
 */
function chromeColors(dark) {
  return dark
    ? { color: '#232326', symbolColor: '#f5f5f7' }
    : { color: '#f0f0f3', symbolColor: '#1d1d1f' };
}

/** Window background, matching --content (otherwise a short white flash in the dark theme). */
function contentBackground() {
  return nativeTheme.shouldUseDarkColors ? '#1c1c1e' : '#ffffff';
}

/** Shared options for frameless windows with a custom title bar. */
function titleBarOptions() {
  if (process.platform === 'darwin') {
    return { titleBarStyle: 'hidden', trafficLightPosition: { x: 16, y: 11 } };
  }
  return {
    titleBarStyle: 'hidden',
    titleBarOverlay: { ...chromeColors(nativeTheme.shouldUseDarkColors), height: TITLEBAR_HEIGHT },
  };
}

/** Updates the overlay colours of open windows when the system theme changes (Windows/Linux only). */
function applyChromeColors() {
  if (process.platform === 'darwin') return;
  const colors = chromeColors(nativeTheme.shouldUseDarkColors);
  for (const win of [mainWindow, loadingWindow]) {
    if (!win || win.isDestroyed()) continue;
    try {
      win.setTitleBarOverlay({ ...colors, height: TITLEBAR_HEIGHT });
    } catch {
      // No overlay on this platform/version; the bar then keeps its colours.
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
/** Running start/restart operation; onBeforeQuit waits for it. */
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

/** Error text for the dialog; for a ServerStartError including the server's output. */
function describeError(err) {
  if (err instanceof ServerStartError && err.stderrTail) {
    return `${err.message}\n\n${t('electron.error.server_output')}\n${err.stderrTail}`;
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
  // Title bar as in the main window: draggable, colours mirror chromeColors() (light/dark via prefers-color-scheme).
  const html =
    '<!doctype html><html lang="' + i18n.currentLanguage() + '"><head><meta charset="utf-8"><title>' + TITLE + '</title>' +
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

/** Creates the loading window or updates its progress text. */
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
  // A new loadURL aborts the previous one; that is deliberately ignored here.
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

/** Opens external web links in the system browser; everything else is refused. */
function openInSystemBrowser(url) {
  if (isWebUrl(url)) shell.openExternal(url);
}

function guardNavigation(event, url) {
  if (isSameOrigin(url)) return;
  event.preventDefault();
  openInSystemBrowser(url);
}

/** Main window with the viewer; navigation and new windows are limited to the server origin. */
function openMainWindow(url) {
  if (quitting) return;
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 840,
    show: false,
    title: TITLE,
    icon: ICON_PATH,
    backgroundColor: contentBackground(),
    // The menu stays hidden; the Alt key shows it (it holds Tools → Reinstall PST support).
    autoHideMenuBar: true,
    ...titleBarOptions(),
    webPreferences: WEB_PREFERENCES,
  });
  const contents = mainWindow.webContents;
  contents.on('will-navigate', guardNavigation);
  contents.on('will-redirect', guardNavigation);
  // Attachments open via target=_blank; they go to the system browser, not a new Electron window.
  contents.setWindowOpenHandler(({ url: target }) => {
    openInSystemBrowser(target);
    return { action: 'deny' };
  });
  mainWindow.once('ready-to-show', () => mainWindow && mainWindow.show());
  mainWindow.on('closed', () => {
    mainWindow = null;
  });
  mainWindow.loadURL(url).catch((err) => console.error('Could not load the viewer:', err));
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
 * Runs a start/restart operation. While one is running (or the app is quitting),
 * every further call is ignored and returns null.
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

/** Starts the server with the chosen runtime. Stops it right away if the app is quitting in the meantime. */
async function startBackend() {
  const handle = await startServer({
    python: runtime.python,
    appRoot: getAppRoot(),
    configPath: getConfigPath(),
    env: { MAILVIEWER_INSTALL_MODE: runtime.mode },
  });
  server = handle;
  serverUrl = handle.url;
  if (quitting) await stopServer();
  return handle;
}

/** Restarts the server and reloads the main window. */
async function restartBackend() {
  await stopServer();
  showLoading(t('electron.loading.start_viewer'));
  await startBackend();
  if (quitting) return;
  if (mainWindow) {
    mainWindow.loadURL(serverUrl).catch((err) => console.error('Could not load the viewer:', err));
  } else {
    openMainWindow(serverUrl);
  }
  closeLoading();
}

/** Menu item: install PST support (pypff) into the system venv again, then restart the server. */
async function reinstallPst() {
  try {
    const found = await findSystemPython();
    if (!found.python) {
      throw new Error(t('electron.error.no_python'));
    }
    showLoading(t('electron.loading.install_pst'));
    await ensureVenv({
      probe: found.python,
      venvDir: getVenvDir(),
      onProgress: showLoading,
      retryPypff: true,
    });
    await restartBackend();
  } catch (err) {
    closeLoading();
    dialog.showErrorBox(t('electron.dialog.pst'), describeError(err));
    // Without a running server the viewer is no longer usable.
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
      label: t('electron.menu.tools'),
      submenu: [
        {
          label: t('electron.menu.reinstall_pst'),
          enabled: runtime !== null && runtime.mode === 'system',
          click: () => runExclusive(reinstallPst),
        },
      ],
    },
  ];
  return Menu.buildFromTemplate(template);
}

/**
 * Takes over the config.json of a former app name (userData folder before the rename).
 * Errors are only logged; the start continues either way.
 */
async function importLegacyConfig() {
  try {
    const source = await migrateLegacyConfig({
      appDataDir: app.getPath('appData'),
      userDataDir: app.getPath('userData'),
    });
    if (source) console.log(`Took over the configuration from: ${source}`);
  } catch (err) {
    console.error('Could not take over the old configuration:', err);
  }
}

function initLanguage() {
  return i18n.init({
    dir: path.join(getAppRoot(), 'static', 'i18n'),
    setting: i18n.readSetting(getConfigPath()),
    preferred: app.getPreferredSystemLanguages(),
  });
}

async function startup() {
  await importLegacyConfig(); // the saved language may come from the old config
  initLanguage();
  i18n.watchSetting(getConfigPath(), () => {
    initLanguage();
    Menu.setApplicationMenu(buildMenu());
  });
  showLoading(t('electron.loading.find_python'));
  runtime = await resolveRuntime({
    userDataDir: app.getPath('userData'),
    resourcesDir: process.resourcesPath,
    onProgress: showLoading,
  });
  Menu.setApplicationMenu(buildMenu());
  showLoading(t('electron.loading.start_viewer'));
  await startBackend();
  if (quitting) return;
  if (SMOKE) {
    await finishSmoke();
    return;
  }
  openMainWindow(serverUrl);
  closeLoading();
}

/** Smoke test: GET / against the running server, then stop the server and quit with 0 or 1. */
async function finishSmoke() {
  const ok = await runSmoke(serverUrl);
  console.log(ok ? `Rauchtest bestanden: ${serverUrl}` : `Rauchtest fehlgeschlagen: ${serverUrl}`);
  await stopServer();
  // The loading window stays open until app.exit, so no window-all-closed quit gets in between.
  app.exit(ok ? 0 : 1);
}

function onBeforeQuit(event) {
  if (quitting) return;
  quitting = true;
  event.preventDefault();
  // Wait for a running start/restart operation first (ignoring errors), so no server is left behind.
  const pending = backendOp ? backendOp.catch(() => {}) : Promise.resolve();
  pending
    .then(() => stopServer())
    .catch((err) => console.error('Could not stop the server:', err))
    .finally(() => app.quit());
}

async function run() {
  await app.whenReady();
  registerPicker({ ipcMain, dialog, isAllowed: (url) => serverUrl !== null && isSameOrigin(url) });
  nativeTheme.on('updated', applyChromeColors);
  try {
    await runExclusive(startup);
  } catch (err) {
    if (SMOKE) {
      console.error(describeError(err));
      // app.exit skips before-quit; server is still null here, so there is nothing to stop.
      app.exit(1);
      return;
    }
    closeLoading();
    // No error dialog while quitting; onBeforeQuit quits the app anyway.
    if (quitting) return;
    dialog.showErrorBox(t('electron.dialog.start_failed'), describeError(err));
    // app.exit skips before-quit; server is still null here, so there is nothing to stop.
    app.exit(1);
  }
}

if (!app.requestSingleInstanceLock()) {
  if (SMOKE) {
    console.error('Another instance is already running.');
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
