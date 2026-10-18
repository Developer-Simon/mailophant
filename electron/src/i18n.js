'use strict';

const fs = require('node:fs');
const path = require('node:path');

const PLACEHOLDER = /\{(\w+)(?::(\w+))?\}/g;
const DEFAULT_DIR = path.resolve(__dirname, '..', '..', 'static', 'i18n');

let dir = DEFAULT_DIR;
let lang = 'en';
let messages = {};
let fallback = null;

function readJson(file, empty) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return empty;
  }
}

function languageCodes(from = dir) {
  const list = readJson(path.join(from, 'languages.json'), []);
  return Array.isArray(list) ? list.filter((l) => l && typeof l.code === 'string').map((l) => l.code) : [];
}

/**
 * Same rule as mailviewer.i18n.resolve_language: a registered setting wins, otherwise the base subtag of the
 * first preferred system language, otherwise English.
 */
function resolveLanguage(setting, preferred, available) {
  if (typeof setting === 'string' && available.includes(setting)) return setting;
  const first = Array.isArray(preferred) && typeof preferred[0] === 'string' ? preferred[0] : '';
  const base = first.toLowerCase().split(/[-_]/)[0];
  return available.includes(base) ? base : 'en';
}

/** The saved choice in the shared config file; any problem means "system" (null). */
function readSetting(configPath) {
  const data = readJson(configPath, null);
  const value = data && typeof data === 'object' ? data.language : null;
  return typeof value === 'string' && value ? value : null;
}

function formatSize(bytes, locale) {
  let v = Number(bytes) || 0;
  if (v < 1024) return `${v} B`;
  const units = ['KB', 'MB', 'GB'];
  let i = -1;
  do { v /= 1024; i++; } while (v >= 1024 && i < units.length - 1);
  return `${v.toLocaleString(locale, { maximumFractionDigits: 1 })} ${units[i]}`;
}

function format(value, params, forLang) {
  let text = value;
  if (value !== null && typeof value === 'object') {
    const form = new Intl.PluralRules(forLang).select(Number(params.count));
    text = value[form] ?? value.other ?? '';
  }
  return String(text).replace(PLACEHOLDER, (whole, name, fmt) => {
    if (!Object.prototype.hasOwnProperty.call(params, name)) return whole;
    const v = params[name];
    if (fmt === 'size') return formatSize(v, forLang);
    return name === 'count' && typeof v === 'number' ? v.toLocaleString(forLang) : String(v);
  });
}

/** Loads languages.json, en.json and the resolved language; returns the language code. */
function init({ dir: from, setting, preferred } = {}) {
  dir = from || DEFAULT_DIR;
  lang = resolveLanguage(setting, preferred, languageCodes(dir));
  fallback = readJson(path.join(dir, 'en.json'), {});
  messages = lang === 'en' ? fallback : readJson(path.join(dir, `${lang}.json`), {});
  return lang;
}

function ensureLoaded() {
  if (fallback === null) init();
}

function t(key, params = {}) {
  ensureLoaded();
  if (Object.prototype.hasOwnProperty.call(messages, key)) return format(messages[key], params, lang);
  if (Object.prototype.hasOwnProperty.call(fallback, key)) return format(fallback[key], params, 'en');
  return key;
}

function currentLanguage() {
  ensureLoaded();
  return lang;
}

/**
 * Watches the config file's directory and calls onChange(setting) when the saved language changed.
 * @returns {() => void} stops watching
 */
function watchSetting(configPath, onChange) {
  const base = path.basename(configPath);
  let last = readSetting(configPath);
  let timer = null;
  let watcher;
  try {
    watcher = fs.watch(path.dirname(configPath), (_event, name) => {
      if (name && name !== base) return;
      clearTimeout(timer);
      timer = setTimeout(() => {
        const now = readSetting(configPath);
        if (now === last) return;
        last = now;
        onChange(now);
      }, 200);
    });
  } catch {
    return () => {};
  }
  return () => {
    clearTimeout(timer);
    watcher.close();
  };
}

module.exports = { resolveLanguage, readSetting, init, t, currentLanguage, watchSetting };
