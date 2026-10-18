// UI texts: the server delivers language and texts as window.I18N_BOOT (/i18n.js) before this script runs.
'use strict';
const I18N = (() => {
  let state = { setting: 'system', lang: 'en', languages: [], messages: {}, fallback: {} };
  if (typeof window !== 'undefined' && window.I18N_BOOT) state = { ...state, ...window.I18N_BOOT };
  const listeners = [];

  const locale = () => pickLocale(state.lang, typeof navigator !== 'undefined' ? navigator.languages : undefined);
  const known = (key) => key in state.messages || key in state.fallback;
  const isMessage = (v) => v !== null && typeof v === 'object' && !Array.isArray(v) && ('text' in v || 'code' in v);

  function msgText(m) {
    if (m === null || m === undefined) return '';
    if (typeof m === 'string') return m;
    if (m.code && known(m.code)) return t(m.code, m.params || {});
    return m.text || '';
  }

  function t(key, params = {}) {
    const resolved = {};
    for (const [k, v] of Object.entries(params)) resolved[k] = isMessage(v) ? msgText(v) : v;
    if (key in state.messages) return formatMessage(state.messages[key], resolved, state.lang, locale());
    if (key in state.fallback) return formatMessage(state.fallback[key], resolved, 'en', locale());
    if (typeof console !== 'undefined') console.warn(`missing text: ${key}`);
    return '';
  }

  function errorText(data, status) {
    if (data && data.code && known(data.code)) return t(data.code, data.params || {});
    if (data && data.error) return String(data.error);
    return t('ui.error.http', { status });
  }

  function apply(root = document) {
    document.documentElement.lang = state.lang;
    root.querySelectorAll('[data-i18n]').forEach((el) => { el.textContent = t(el.dataset.i18n); });
    for (const attr of ['title', 'aria-label', 'placeholder']) {
      root.querySelectorAll(`[data-i18n-${attr}]`).forEach((el) => {
        el.setAttribute(attr, t(el.getAttribute(`data-i18n-${attr}`)));
      });
    }
  }

  async function set(setting) {
    const res = await fetch('/api/language', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ language: setting }),
    });
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new Error(errorText(data, res.status));
    state = { ...state, ...data };
    apply(document);
    listeners.forEach((fn) => fn());
  }

  return {
    t, msgText, errorText, apply, set, locale,
    onChange: (fn) => { listeners.push(fn); },
    get lang() { return state.lang; },
    get setting() { return state.setting; },
    get languages() { return state.languages; },
  };
})();
const t = I18N.t;

if (typeof document !== 'undefined') {
  try {
    I18N.apply(document);
    const group = document.getElementById('settings-language');
    const warn = document.getElementById('settings-language-warn');
    if (group) {
      const options = [{ value: 'system', key: 'ui.settings.language_system' },
        ...I18N.languages.map((l) => ({ value: l.code, name: l.name }))];
      for (const o of options) {
        const label = document.createElement('label');
        label.className = 'group-row';
        const input = document.createElement('input');
        input.type = 'radio';
        input.name = 'language';
        input.value = o.value;
        input.checked = o.value === I18N.setting;
        const span = document.createElement('span');
        if (o.key) { span.dataset.i18n = o.key; span.textContent = t(o.key); } else span.textContent = o.name;
        label.append(input, ' ', span);
        group.append(label);
      }
      group.addEventListener('change', async (e) => {
        const input = e.target;
        if (!input.checked) return;
        warn.hidden = true;
        try {
          await I18N.set(input.value);
        } catch (err) {
          group.querySelectorAll('input').forEach((r) => { r.checked = r.value === I18N.setting; });
          warn.textContent = err.message;
          warn.hidden = false;
        }
      });
    }
  } finally {
    document.documentElement.classList.add('i18n-ready');
  }
}
