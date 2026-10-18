// Theme: "system" follows the OS; "light"/"dark" set data-theme on <html>. Applied first so there is no flash.
const THEME_KEY = 'mbv-theme';
function storedTheme() {
  try { const t = localStorage.getItem(THEME_KEY); return t === 'light' || t === 'dark' ? t : 'system'; } catch (_) { return 'system'; }
}
function applyTheme(theme) {
  if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}
applyTheme(storedTheme());
document.querySelectorAll('input[name="theme"]').forEach((r) => {
  r.checked = r.value === storedTheme();
  r.addEventListener('change', () => {
    if (!r.checked) return;
    applyTheme(r.value);
    try { localStorage.setItem(THEME_KEY, r.value); } catch (_) { /* not remembered, e.g. private window */ }
  });
});
