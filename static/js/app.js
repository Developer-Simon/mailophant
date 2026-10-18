(() => {
'use strict';

/* ---------- Helpers ---------- */
const PAGE = 50;
const $ = (id) => document.getElementById(id);

// Builds an element; all values go in via textContent / setAttribute, never innerHTML.
function el(tag, attrs = {}, children = []) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else n.setAttribute(k, v);
  }
  n.append(...children);
  return n;
}

async function api(path, init = {}) {
  const res = await fetch(path, { headers: { Accept: 'application/json' }, ...init });
  if (!res.ok) throw Object.assign(new Error(t('ui.error.http', { status: res.status })), { status: res.status });
  return res.json();
}

const addrText = (a) => {
  if (!a) return '';
  if (a.name && a.name !== a.address) return `${a.name} <${a.address}>`;
  return a.address || a.name || '';
};
const senderText = (s) => (s && (s.name || s.address)) || t('ui.mail.unknown_sender');
const addrList = (list) => (list || []).map(addrText).filter(Boolean).join(', ');

function listDate(sec) {
  const d = new Date(sec * 1000);
  return d.toDateString() === new Date().toDateString()
    ? d.toLocaleTimeString(I18N.locale(), { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleDateString(I18N.locale(), { day: '2-digit', month: '2-digit', year: '2-digit' });
}
const longDate = (sec) => new Date(sec * 1000).toLocaleString(I18N.locale(), {
  weekday: 'long', day: 'numeric', month: 'long', year: 'numeric', hour: '2-digit', minute: '2-digit',
});

// The server sends a code for names it makes up itself (the virtual folder); mail data keeps its own names.
const folderLabel = (f) => (f.name_code ? t(f.name_code) : f.name || '');
const fileSize = (bytes) => formatSize(bytes, I18N.locale());


// Builds an inline SVG icon from ICONS. Without a label it is decorative (aria-hidden).
function icon(name, label) {
  const paths = ICONS[name];
  if (!paths) throw new Error(`unknown icon: ${name}`);
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('class', 'icon');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', name.endsWith('-fill') ? 'currentColor' : 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.75');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  if (label) {
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', label);
  } else {
    svg.setAttribute('aria-hidden', 'true');
  }
  for (const d of paths) {
    const path = document.createElementNS(NS, 'path');
    path.setAttribute('d', d);
    svg.append(path);
  }
  return svg;
}

// Background colours for auto icons; white text works on all of them in light and dark mode.
const ICON_COLORS = ['#0a66c2', '#2e7d32', '#7b3fb0', '#b03a2e', '#b35c00', '#00796b', '#4a5568', '#8a6d00'];

// "Outlook 2015" -> "O15", "Mail" -> "Ma". A word starting with a digit contributes its last two digits.
function initials(name) {
  const words = (name || '').split(/[\s\-_.]+/).filter((w) => /[\p{L}\p{N}]/u.test(w));
  if (words.length === 0) return '?';
  if (words.length === 1) return [...words[0]].slice(0, 2).join('').toLocaleUpperCase('de-DE');
  const parts = words.map((w) => (/^\p{N}/u.test(w) ? w.match(/^\p{N}+/u)[0].slice(-2) : [...w][0]));
  return parts.join('').slice(0, 3).toLocaleUpperCase('de-DE');
}

function colorFor(name) {
  let h = 0;
  for (const ch of name || '') h = (h * 31 + ch.codePointAt(0)) >>> 0;
  return ICON_COLORS[h % ICON_COLORS.length];
}

// Source icon for the rail (cls "rail") or a message row (cls "list"). Server data goes in via textContent / src only.
function sourceIcon(src, cls) {
  const icon = src.icon || {};
  if (icon.type === 'image') {
    const ver = state.iconVer[src.id] || 0;
    const img = el('img', { class: `src-icon ${cls}`, src: `/api/sources/${encodeURIComponent(src.id)}/icon?v=${ver}`, alt: '' });
    img.addEventListener('error', () => img.replaceWith(autoIcon(src, cls)));
    return img;
  }
  if (icon.type === 'emoji') return el('span', { class: `src-icon glyph ${cls}`, 'aria-hidden': 'true', text: icon.value || '' });
  return autoIcon(src, cls);
}

function autoIcon(src, cls) {
  const span = el('span', { class: `src-icon ${cls}`, 'aria-hidden': 'true', text: initials(src.name) });
  span.style.background = colorFor(src.name);
  return span;
}

/* ---------- State ---------- */
const state = {
  folders: [], folder: null, q: '',
  tags: [], starCount: 0, tag: null, tagSeq: 0, // tag: "star", a tag ID as string, or null; never with folder
  messages: [], total: 0, loading: false, listSeq: 0,
  selectedKey: null, detailSeq: 0,
  mail: null, mailSource: null, mailStale: false, mode: 'html', // mailStale: a new mail is loading, see startLoad()
  sources: [], settings: null, view: null, viewSeq: 0, config: null,
  iconVer: {}, // per source: bumped after an icon upload, so the image URL changes
  navLevel: 0, navPos: 0, // narrow layout: 0 folders, 1 list, 2 mail; navPos glides towards navLevel
  collapsed: new Set(), // folder tree nodes (keys, see buildFolderTree) whose children are hidden; session only
  selecting: false, selected: [], anchor: null, cursor: null, // selection mode: picked rows ("<source>/<id>"), last clicked row, keyboard row
};

// Message identity across sources: the same mail id may exist in two sources.
const msgKey = (source, id) => `${source}\n${id}`;
// Selection key of a row, the form toggleSelection and transferPayload use.
const rowKey = (m) => `${m.source}/${m.id}`;
const sourceById = (id) => state.sources.find((s) => s.id === id) || null;
const visibleSources = () => state.sources.filter((s) => !s.hidden);
// Source ids the rail selection covers: all visible sources, or the single one.
const activeSourceIds = () => (state.view === 'all' ? visibleSources().map((s) => s.id) : (state.view ? [state.view] : []));

const VIEW_KEY = 'mailviewer.view';
function storedView() {
  try { return localStorage.getItem(VIEW_KEY); } catch (_) { return null; }
}
function storeView(view) {
  try { localStorage.setItem(VIEW_KEY, view); } catch (_) { /* not remembered, e.g. private window */ }
}

// Prepended to the mail HTML inside the sandboxed iframe (no scripts, strict CSP).
const FRAME_HEAD = `<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'"><base target="_blank"><style>body{font-family:sans-serif;margin:12px;word-wrap:break-word}img{max-width:100%;height:auto}</style>`;

/* ---------- Source rail and view ---------- */
// Stored view if it still exists (and "all" only in the combined view), else "all" or the first source.
function startView() {
  const combined = state.settings?.view === 'combined';
  const visible = visibleSources();
  const stored = storedView();
  if (stored && (stored === 'all' ? combined : visible.some((s) => s.id === stored))) return stored;
  if (combined) return 'all';
  return visible[0] ? visible[0].id : null;
}

function railButton(id, name, src) {
  const title = src && !src.available ? t('ui.source.unreachable_named', { name }) : name;
  const btn = el('button', { type: 'button', title, 'aria-label': title });
  btn.setAttribute('aria-current', String(state.view === id));
  if (src && !src.available) btn.classList.add('unavailable');
  btn.append(src ? sourceIcon(src, 'rail') : el('span', { class: 'src-icon all', 'aria-hidden': 'true', text: '∑' }));
  btn.addEventListener('click', () => selectView(id));
  return btn;
}

function renderRail() {
  const buttons = [];
  if (state.settings?.view === 'combined') buttons.push(railButton('all', t('ui.sources.all'), null));
  for (const s of visibleSources()) buttons.push(railButton(s.id, s.name, s));
  $('rail-sources').replaceChildren(...buttons);
}

function updateFetchArea() {
  const src = state.view && state.view !== 'all' ? sourceById(state.view) : null;
  $('fetch-area').hidden = !(src && src.fetchable);
}

// Switching the view remembers it, reloads folders and list and clears the reader.
function selectView(view) {
  if (view !== state.view) clearSelection();
  state.view = view;
  storeView(view);
  renderRail();
  updateFetchArea();
  goTo(0);
  loadFolders();
}

/* ---------- Folders ---------- */
// keep: after a fetch, stay in the current folder if it still exists.
async function loadFolders(keep = false) {
  const prev = state.folder;
  const seq = ++state.viewSeq;
  const ids = activeSourceIds();
  if (ids.length === 0) {
    state.folders = [];
    state.folder = null;
    state.tag = null;
    $('folder-list').replaceChildren(el('li', { class: 'muted', text: t('ui.sources.none') }));
    updateFolderAdd();
    state.messages = [];
    state.total = 0;
    showEmpty('none', t('ui.mail.none_selected'));
    renderList();
    updateSearchPlaceholder();
    loadTags();
    return;
  }
  let folders;
  try {
    folders = await api(`/api/folders?sources=${encodeURIComponent(ids.join(','))}`);
  } catch (e) {
    if (seq !== state.viewSeq) return;
    $('folder-list').replaceChildren(el('li', { class: 'warn', text: t('ui.error.folders_load', { detail: e.message }) }));
    return;
  }
  if (seq !== state.viewSeq) return; // the view changed meanwhile
  state.folders = folders;
  renderFolders();
  if (state.tag) {
    // A tag filter survives the reload. loadTags falls back to the inbox if the tag is gone.
    await loadTags();
    if (seq !== state.viewSeq) return;
    if (state.tag) reloadList();
    return;
  }
  if (keep && state.folders.some((f) => f.key === prev)) {
    selectFolder(prev);
  } else {
    selectFolder(defaultFolderKey());
  }
  loadTags();
}

function defaultFolderKey() {
  const inbox = state.folders.find((f) => (f.name || '').toLowerCase() === 'inbox') || state.folders[0];
  return inbox ? inbox.key : null;
}

function renderFolders() {
  const rows = visibleNodes(buildFolderTree(state.folders), state.collapsed);
  openMenu = null; // the rows are rebuilt, so an open row menu goes with them
  $('folder-list').replaceChildren(...rows.map(folderRow));
  updateFolderAdd();
  updateBackList();
}

// One sidebar row, indented by depth. A node with children gets a disclosure button in front of its row.
// Synthetic nodes (a parent path without a folder) are muted and only expand or collapse.
function folderRow(node) {
  const expanded = !state.collapsed.has(node.key);
  const toggle = () => {
    if (expanded) state.collapsed.add(node.key);
    else state.collapsed.delete(node.key);
    renderFolders();
    // The rows are rebuilt, so put the focus back on this node's toggle.
    $('folder-list').querySelector(`[data-toggle="${CSS.escape(node.key)}"]`)?.focus({ preventScroll: true });
  };
  const li = el('li');
  li.dataset.folderKey = node.key; // finds the row again after a re-render, see restoreFolderFocus
  li.style.setProperty('--depth', String(node.depth));
  if (node.synthetic) {
    const btn = el('button', {
      class: 'folder synthetic', type: 'button', 'aria-expanded': String(expanded), 'data-toggle': node.key,
    });
    btn.append(icon(expanded ? 'chevron-down' : 'chevron-right'), el('span', { class: 'name', text: node.name }));
    btn.addEventListener('click', toggle);
    li.append(btn);
    return li;
  }
  if (node.children.length > 0) {
    const twisty = el('button', {
      class: 'folder-twisty', type: 'button', 'aria-expanded': String(expanded), 'data-toggle': node.key,
      'aria-label': t('ui.folders.subfolders_of', { name: node.name }),
    }, [icon(expanded ? 'chevron-down' : 'chevron-right')]);
    twisty.addEventListener('click', toggle);
    li.append(twisty);
  } else {
    li.append(el('span', { class: 'folder-spacer' }));
  }
  const f = node.folder;
  const btn = el('button', { class: 'folder', type: 'button', 'data-key': node.key });
  btn.setAttribute('aria-current', String(f.key === state.folder));
  if (f.virtual) btn.classList.add('todo'); // mails still to delete in Proton: a to-do, tinted like a pending badge
  btn.append(icon(f.virtual ? 'trash' : folderIconName(f.name || '')), el('span', { class: 'name', text: folderLabel(f) }));
  if (f.unread > 0) btn.append(el('b', { class: 'unread', text: String(f.unread) }));
  btn.append(el('span', { class: 'count', text: String(f.count ?? 0) }));
  btn.addEventListener('click', () => {
    selectFolder(f.key);
    goTo(1);
  });
  li.append(btn);
  const sid = folderArchive(node);
  attachFolderDrag(li, node, sid);
  if (sid) {
    const menuBtn = el('button', {
      class: 'folder-menu-btn', type: 'button', title: t('ui.folders.actions'), 'aria-haspopup': 'menu', 'aria-expanded': 'false',
      'aria-label': t('ui.folders.actions_for', { name: f.name || '' }),
    }, [icon('more')]);
    menuBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleFolderMenu(node, sid, menuBtn, li);
    });
    li.append(menuBtn);
  }
  return li;
}

function selectFolder(key) {
  if (key !== state.folder) clearSelection();
  state.folder = key;
  state.tag = null;
  renderFolders();
  renderTags();
  updateSearchPlaceholder();
  reloadList();
}

function selectTag(key) {
  if (key !== state.tag) clearSelection();
  state.tag = key;
  state.folder = null;
  renderFolders();
  renderTags();
  updateSearchPlaceholder();
  reloadList();
}

function updateSearchPlaceholder() {
  $('search').placeholder = state.tag ? t('ui.search.placeholder_tag') : t('ui.search.placeholder');
}

/* ---------- Archive folders: create, rename, delete, move ---------- */
// The archive a row can be changed in (see manageableArchive), or null.
const folderArchive = (node) => (node.folder ? manageableArchive(node.folder, state.sources) : null);
// Label ID of a folder inside one archive, or null.
const archiveLabel = (folder, sid) => folder?.labels?.[sid] ?? null;
const FOLDER_NAME_MAX = 100;
let folderCtx = null; // { op, sid, key, name } of the open folder dialog; key is the folder acted on (the parent for create), null at the top
let folderBusy = false; // the dialog's request is on its way
let openMenu = null; // { button, list } of the open row menu
let folderReturn = null; // where focus goes when the dialog closes: { key } of the row, or { add: true } for the top-level button
let folderDrag = null; // { key, sid } while an archive folder row is dragged

// "Neuer Ordner…" at the top level: shown while exactly one archive is the selection.
function updateFolderAdd() {
  const ids = activeSourceIds();
  const src = ids.length === 1 ? sourceById(ids[0]) : null;
  $('folder-add').hidden = !(src && src.archive === true);
}

// Opens the dialog for create (folder = parent or null for the top level), rename or delete.
function openFolderDialog(op, sid, folder) {
  folderCtx = { op, sid, key: folder ? folder.key : null, name: folder?.name || '' };
  const name = folderCtx.name;
  $('folder-title').textContent = op === 'create' ? (folder ? t('ui.folder.new_sub', { name }) : t('ui.folder.new_title'))
    : op === 'rename' ? t('ui.folder.rename_title', { name }) : t('ui.folder.delete_title', { name });
  $('folder-submit').textContent = op === 'create' ? t('ui.folder.create') : op === 'rename' ? t('ui.folder.rename') : t('ui.folder.delete');
  $('folder-name-row').hidden = op === 'delete';
  $('folder-delete-text').hidden = op !== 'delete';
  $('folder-name').value = op === 'rename' ? name : '';
  showFolderWarn('');
  setFolderBusy(false);
  const dlg = $('folder-dialog');
  if (!dlg.open) dlg.showModal();
  if (op !== 'delete') $('folder-name').focus();
}

function showFolderWarn(msg) {
  $('folder-warn').textContent = msg || '';
  $('folder-warn').hidden = !msg;
}

function setFolderBusy(busy) {
  folderBusy = busy;
  $('folder-cancel').disabled = busy;
  updateFolderSubmit();
}

// Create and rename need a name that is not empty after trimming, and a new one for rename.
function updateFolderSubmit() {
  const ctx = folderCtx;
  const name = $('folder-name').value.trim();
  const usable = !ctx || ctx.op === 'delete' || (name !== '' && name !== (ctx.op === 'rename' ? ctx.name : null));
  $('folder-submit').disabled = folderBusy || !usable;
}

// POST that starts an archive job. Resolves to null when the job has started, else to the error text.
async function postJob(url, body, failText) {
  let res;
  let data = null;
  try {
    res = await fetch(url, { method: 'POST', ...jsonPost(body), signal: timeoutSignal() });
    data = await res.json().catch(() => null);
  } catch (err) {
    return isTimeout(err) ? timeoutText() : t('ui.error.with_detail', { what: failText, detail: err.message });
  }
  if (res.ok) return null;
  return res.status === 409 ? t('ui.error.busy') : I18N.errorText(data, res.status);
}

function postFolderOp(sid, body) {
  return postJob(`/api/archives/${encodeURIComponent(sid)}/folders`, body, t('ui.error.folder_change'));
}

// The folder job is quick, and the first poll may find it finished already, which pollJob would not report
// as a change. So wait for the end here, let pollJob show a failure in job-error, then reload the folders.
async function followFolderJob() {
  dismissedJob = null;
  for (let i = 0; i < 600; i++) {
    let st = null;
    try {
      st = await api('/api/job');
    } catch (_) { /* read again on the next round */ }
    if (st && st.state !== 'running') break;
    await new Promise((resolve) => setTimeout(resolve, JOB_POLL_MS));
  }
  await pollJob();
  await loadFolders(true);
}

// Drag and drop and the dialog both end here: a started job is followed, a refused one is shown in job-error.
async function sendFolderOp(sid, body) {
  const err = await postFolderOp(sid, body);
  if (err) {
    $('job-error-text').textContent = err;
    $('job-error').hidden = false;
    return false;
  }
  followFolderJob();
  return true;
}

async function submitFolderDialog(e) {
  e.preventDefault();
  const ctx = folderCtx;
  if (!ctx || folderBusy) return;
  const folder = ctx.key === null ? null : state.folders.find((f) => f.key === ctx.key);
  if (ctx.key !== null && !folder) {
    showFolderWarn(t('ui.folder.gone'));
    return;
  }
  const id = folder ? archiveLabel(folder, ctx.sid) : null;
  if (folder && id === null) {
    showFolderWarn(t('ui.folder.wrong_archive'));
    return;
  }
  const name = $('folder-name').value.trim();
  if (ctx.op !== 'delete' && [...name].length > FOLDER_NAME_MAX) {
    showFolderWarn(t('ui.folder.name_max', { max: FOLDER_NAME_MAX }));
    return;
  }
  const body = ctx.op === 'create' ? { op: 'create', name, parent: id }
    : ctx.op === 'rename' ? { op: 'rename', id, name }
      : { op: 'delete', id };
  setFolderBusy(true);
  const err = await postFolderOp(ctx.sid, body);
  setFolderBusy(false);
  if (err) {
    showFolderWarn(err);
    return;
  }
  if (folderCtx === ctx) $('folder-dialog').close(); // not a dialog that was opened meanwhile
  followFolderJob();
}

// Row menu: the three actions of an archive folder. Arrow keys move, Escape or a click outside closes it.
function closeFolderMenu(refocus) {
  if (!openMenu) return;
  const { button, list } = openMenu;
  openMenu = null;
  list.remove();
  button.setAttribute('aria-expanded', 'false');
  if (refocus) button.focus();
}

function toggleFolderMenu(node, sid, button, li) {
  if (openMenu?.button === button) {
    closeFolderMenu(true);
    return;
  }
  closeFolderMenu(false);
  const actions = [
    [t('ui.folder.menu_new_sub'), () => openFolderDialog('create', sid, node.folder)],
    [t('ui.folder.menu_rename'), () => openFolderDialog('rename', sid, node.folder)],
    [t('ui.folder.menu_delete'), () => openFolderDialog('delete', sid, node.folder)],
  ];
  const items = actions.map(([label, run]) => {
    const item = el('button', { type: 'button', role: 'menuitem', text: label });
    item.addEventListener('click', () => {
      closeFolderMenu(false);
      folderReturn = { key: node.key }; // the dialog gives focus back to this row's menu button
      run();
    });
    return item;
  });
  const list = el('ul', { class: 'folder-menu', role: 'menu', 'aria-label': t('ui.folders.actions_for', { name: node.name }) },
    items.map((item) => el('li', { role: 'none' }, [item])));
  list.addEventListener('keydown', (e) => {
    const i = items.indexOf(document.activeElement);
    const last = items.length - 1;
    if (e.key === 'ArrowDown') items[i < 0 || i === last ? 0 : i + 1].focus();
    else if (e.key === 'ArrowUp') items[i <= 0 ? last : i - 1].focus();
    else if (e.key === 'Home') items[0].focus();
    else if (e.key === 'End') items[last].focus();
    else if (e.key === 'Escape') closeFolderMenu(true);
    else if (e.key === 'Tab') {
      closeFolderMenu(true); // the button keeps focus, so the normal Tab order goes on from the menu's row
      return;
    } else return;
    e.preventDefault();
  });
  li.append(list);
  button.setAttribute('aria-expanded', 'true');
  openMenu = { button, list };
  items[0].focus();
}

// Zone of the pointer on a row: top quarter "before", bottom quarter "after", otherwise "into".
function pointerZone(e, li) {
  const r = li.getBoundingClientRect();
  const y = (e.clientY - r.top) / r.height;
  return y < 0.25 ? 'before' : y > 0.75 ? 'after' : 'into';
}

// Move request for dropping the dragged folder at zone of overKey, or null if that drop is not allowed.
function folderMoveBody(sid, dragKey, overKey, zone) {
  const tree = buildFolderTree(state.folders);
  const plan = dropTarget(tree, dragKey, overKey, zone);
  if (!plan) return null;
  const id = archiveLabel(state.folders.find((f) => f.key === dragKey), sid);
  const parent = plan.parentKey === null ? null : archiveLabel(state.folders.find((f) => f.key === plan.parentKey), sid);
  if (id === null || (plan.parentKey !== null && parent === null)) return null;
  const level = plan.parentKey === null ? tree : findNode(tree, plan.parentKey).children;
  return { op: 'move', id, parent, index: moveIndex(level, dragKey, plan.index) };
}

// Drag and drop for archive folder rows. Only rows of the same archive take part.
function attachFolderDrag(li, node, sid) {
  if (sid) {
    li.draggable = true;
    li.addEventListener('dragstart', (e) => {
      folderDrag = { key: node.key, sid };
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', node.name);
      li.classList.add('dragging');
    });
    li.addEventListener('dragend', () => {
      folderDrag = null;
      li.classList.remove('dragging');
      clearDropMarks();
    });
  }
  li.addEventListener('dragover', (e) => {
    clearDropMarks();
    const drag = folderDrag;
    if (!drag || folderArchive(node) !== drag.sid) return;
    const zone = pointerZone(e, li);
    if (!folderMoveBody(drag.sid, drag.key, node.key, zone)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    li.classList.add(`drop-${zone}`);
  });
  li.addEventListener('dragleave', (e) => {
    if (!li.contains(e.relatedTarget)) clearDropMarks(li);
  });
  li.addEventListener('drop', (e) => {
    const drag = folderDrag;
    clearDropMarks();
    if (!drag || folderArchive(node) !== drag.sid) return;
    const body = folderMoveBody(drag.sid, drag.key, node.key, pointerZone(e, li));
    if (!body) return;
    e.preventDefault();
    sendFolderOp(drag.sid, body);
  });
}

// Removes the drop marks from one row, or from all rows.
function clearDropMarks(li) {
  const rows = li ? [li] : [...$('folder-list').children];
  for (const row of rows) row.classList.remove('drop-before', 'drop-after', 'drop-into');
}

document.addEventListener('click', (e) => {
  if (openMenu && !openMenu.list.contains(e.target) && e.target !== openMenu.button) closeFolderMenu(false);
});
$('folder-add').addEventListener('click', () => {
  const ids = activeSourceIds();
  if (ids.length === 1) {
    folderReturn = { add: true };
    openFolderDialog('create', ids[0], null);
  }
});
$('folder-form').addEventListener('submit', submitFolderDialog);
$('folder-name').addEventListener('input', updateFolderSubmit);
$('folder-cancel').addEventListener('click', () => $('folder-dialog').close());
// Esc does not close the dialog while its request is in flight.
$('folder-dialog').addEventListener('cancel', (e) => {
  if (folderBusy) e.preventDefault();
});
$('folder-dialog').addEventListener('close', () => {
  folderCtx = null;
  const ret = folderReturn;
  folderReturn = null;
  restoreFolderFocus(ret);
});

// Focus back to the control that opened the dialog: the row's menu button (found by key, since the rows may have
// been rebuilt), or the top-level button. Else the folder list.
function restoreFolderFocus(ret) {
  let target = null;
  if (ret?.add && !$('folder-add').hidden) target = $('folder-add');
  if (ret?.key != null) {
    const row = [...$('folder-list').children].find((li) => li.dataset.folderKey === ret.key);
    target = row?.querySelector('.folder-menu-btn') ?? null;
  }
  (target ?? $('folder-list')).focus();
}

/* ---------- Ins Archiv… (selected mails) ---------- */
const ALL_MAIL_KEY = 'sys:5'; // "All Mail" is never offered as a folder to put mails in
const TRANSFER_MAX = 5000; // the server takes at most this many items in one transfer (MAX_TRANSFER_ITEMS)
let transferCtx = null; // { keys, archive, nodes, target, presetName } of the open dialog; target: { id } for a folder, 'new', or null
let transferBusy = false; // the POST is on its way
let transferSeq = 0; // a folder answer for an earlier archive is dropped

// Archive sources that can take mails now.
const usableArchives = () => state.sources.filter((s) => s.archive && s.available);

function showTransferWarn(msg) {
  $('transfer-warn').textContent = msg || '';
  $('transfer-warn').hidden = !msg;
}

function setTransferBusy(busy) {
  transferBusy = busy;
  updateTransferSubmit();
}

// The submit needs an archive and a folder, or "Neuer Ordner…" with a name.
function updateTransferSubmit() {
  const ctx = transferCtx;
  const name = $('transfer-new-name').value.trim();
  const target = ctx?.target ?? null;
  const usable = Boolean(ctx && ctx.archive && (target === 'new' ? name !== '' && [...name].length <= FOLDER_NAME_MAX : target !== null));
  $('transfer-submit').disabled = transferBusy || !usable;
  $('transfer-cancel').disabled = transferBusy;
}

// Copied from Proton or Maildir sources, moved out of other archives, only refiled within the target archive.
function transferNoteParts(r) {
  const parts = [t('ui.transfer.copied', { count: r.copied }), t('ui.transfer.reused', { count: r.reused }), t('ui.transfer.skipped', { count: r.skipped })];
  if (r.refiled) parts.push(t('ui.transfer.refiled', { count: r.refiled }));
  if (r.deleted_origin) parts.push(t('ui.transfer.deleted_origin', { count: r.deleted_origin }));
  if (r.failed) parts.push(t('ui.transfer.failed', { count: r.failed }));
  return parts;
}

function updateTransferHint() {
  const ctx = transferCtx;
  let copy = 0;
  let move = 0;
  let refile = 0;
  for (const key of ctx.keys) {
    const sid = key.slice(0, key.indexOf('/'));
    if (sid === ctx.archive) refile += 1;
    else if (sourceById(sid)?.archive) move += 1;
    else copy += 1;
  }
  const parts = [];
  if (copy) parts.push(t('ui.transfer.will_copy', { count: copy }));
  if (move) parts.push(t('ui.transfer.will_move', { count: move }));
  if (refile) parts.push(t('ui.transfer.will_refile', { count: refile }));
  $('transfer-hint').textContent = parts.join(' ');
}

// The dialog opens with the selection; with no reachable archive the button only shows the note.
function openTransferDialog() {
  const archives = usableArchives();
  if (state.selected.length === 0 || state.selected.length > TRANSFER_MAX || archives.length === 0) return;
  const n = state.selected.length;
  transferCtx = { keys: [...state.selected], archive: archives[0].id, nodes: [], target: null, presetName: '' };
  $('transfer-title').textContent = t('ui.transfer.title', { count: n });
  $('transfer-archive').replaceChildren(...archives.map((s) => el('option', { value: s.id, text: s.name })));
  $('transfer-archive').value = archives[0].id;
  $('transfer-new-name').value = '';
  showTransferWarn('');
  setTransferBusy(false);
  updateTransferHint();
  loadTransferFolders();
  const dlg = $('transfer-dialog');
  if (!dlg.open) dlg.showModal();
}

// The folder tree of the chosen archive, empty folders included. "All Mail" is left out.
async function loadTransferFolders() {
  const ctx = transferCtx;
  const seq = ++transferSeq;
  ctx.nodes = [];
  ctx.target = null;
  $('transfer-new').hidden = true;
  $('transfer-tree').replaceChildren(el('li', { class: 'group-row muted', text: t('ui.transfer.loading_folders') }));
  updateTransferSubmit();
  let folders;
  try {
    folders = await api(`/api/folders?sources=${encodeURIComponent(ctx.archive)}`);
  } catch (e) {
    if (seq !== transferSeq) return;
    $('transfer-tree').replaceChildren();
    showTransferWarn(t('ui.error.folders_load', { detail: e.message }));
    return;
  }
  if (seq !== transferSeq) return;
  const visible = folders.filter((f) => !f.virtual && f.key !== ALL_MAIL_KEY);
  ctx.nodes = visibleNodes(buildFolderTree(visible), new Set());
  // Start on the folder the mails are shown in: the same folder in the archive, else a new one of that name.
  const archive = visible.map((f) => ({ key: f.key, path: f.path, id: archiveLabel(f, ctx.archive) })).filter((f) => f.id !== null);
  const preset = transferPreset(state.folders.find((f) => f.key === state.folder) ?? null, archive);
  const nameField = $('transfer-new-name');
  if (preset?.id) ctx.target = { id: preset.id };
  else if (preset) {
    ctx.target = 'new';
    // A name the user typed stays when the archive changes; only an empty field or the last preset is replaced.
    if (nameField.value === '' || nameField.value === ctx.presetName) nameField.value = preset.name;
    ctx.presetName = preset.name;
  }
  renderTransferFolders();
  if (preset && !preset.id) $('transfer-parent').value = preset.parent ?? '';
}

// Radio list of the archive's folders as a tree, "Neuer Ordner…" last; its parent picker lists the same folders.
function renderTransferFolders() {
  const ctx = transferCtx;
  const rows = ctx.nodes.map((node) => {
    const id = node.folder ? archiveLabel(node.folder, ctx.archive) : null;
    const radio = el('input', { type: 'radio', name: 'transfer-folder', value: id ?? '' });
    radio.disabled = id === null;
    radio.checked = id !== null && ctx.target?.id === id;
    const label = el('label', { class: 'group-row transfer-row' + (node.synthetic ? ' synthetic' : '') }, [radio, el('span', { text: node.name })]);
    label.style.setProperty('--depth', String(node.depth));
    return el('li', {}, [label]);
  });
  const newRadio = el('input', { type: 'radio', name: 'transfer-folder', value: 'new' });
  newRadio.checked = ctx.target === 'new';
  rows.push(el('li', {}, [el('label', { class: 'group-row transfer-row' }, [newRadio, el('span', { text: t('ui.transfer.new_folder') })])]));
  $('transfer-tree').replaceChildren(...rows);
  $('transfer-new').hidden = ctx.target !== 'new';

  const parents = [el('option', { value: '', text: t('ui.transfer.top_level') })];
  for (const node of ctx.nodes) {
    const id = node.folder ? archiveLabel(node.folder, ctx.archive) : null;
    if (id !== null) parents.push(el('option', { value: id, text: `${'\u2003'.repeat(node.depth)}${node.name}` }));
  }
  $('transfer-parent').replaceChildren(...parents);
  updateTransferSubmit();
}

// POST /api/transfer for the picked mails. The job runs on; the selection ends here.
async function submitTransfer(e) {
  e.preventDefault();
  const ctx = transferCtx;
  if (!ctx || transferBusy || $('transfer-submit').disabled) return;
  const target = ctx.target;
  const folderId = target === 'new' ? null : target.id;
  const newFolder = target === 'new'
    ? { name: $('transfer-new-name').value.trim(), parent: $('transfer-parent').value || null }
    : null;
  setTransferBusy(true);
  const err = await postJob('/api/transfer', transferPayload(ctx.keys, ctx.archive, folderId, newFolder), t('ui.error.transfer'));
  setTransferBusy(false);
  if (err) {
    showTransferWarn(err);
    return;
  }
  $('transfer-dialog').close();
  state.selecting = false;
  state.selected = [];
  state.anchor = null;
  updateSelectUI();
  // The job may already be done at the first read, which pollJob reports as a change only if it knows the job was
  // running. Counting it as running from here makes the end of the job reload the sources, folders and list.
  lastJob = { state: 'running' };
  pollJob();
}

$('select-btn').addEventListener('click', () => {
  state.selecting = !state.selecting;
  if (!state.selecting) {
    state.selected = [];
    state.anchor = null;
  }
  updateSelectUI();
});
$('select-move').addEventListener('click', openTransferDialog);
$('transfer-form').addEventListener('submit', submitTransfer);
$('transfer-archive').addEventListener('change', () => {
  if (!transferCtx) return;
  transferCtx.archive = $('transfer-archive').value;
  updateTransferHint();
  loadTransferFolders();
});
$('transfer-tree').addEventListener('change', (e) => {
  if (!transferCtx || !e.target.checked) return;
  transferCtx.target = e.target.value === 'new' ? 'new' : { id: e.target.value };
  $('transfer-new').hidden = transferCtx.target !== 'new';
  if (transferCtx.target === 'new') $('transfer-new-name').focus();
  updateTransferSubmit();
});
$('transfer-new-name').addEventListener('input', updateTransferSubmit);
$('transfer-cancel').addEventListener('click', () => $('transfer-dialog').close());
$('transfer-dialog').addEventListener('cancel', (e) => {
  if (transferBusy) e.preventDefault(); // the request is on its way; closing now would hide its outcome
});
$('transfer-dialog').addEventListener('close', () => {
  transferCtx = null;
  transferSeq++;
});

/* ---------- Tags ---------- */
const COLOR_RE = /^#[0-9a-fA-F]{6}$/;

// Colour dot for a tag. The colour is used only if it has the same #rrggbb form the server accepts.
function tagDot(tag) {
  const dot = el('span', { class: 'tag-dot', title: tag.name || '', 'aria-hidden': 'true' });
  if (COLOR_RE.test(tag.color || '')) dot.style.background = tag.color;
  return dot;
}

// Star count and tags of the active sources. A tag filter that no longer exists falls back to the inbox.
async function loadTags() {
  const seq = ++state.tagSeq;
  const ids = activeSourceIds();
  if (ids.length === 0) {
    state.tags = [];
    state.starCount = 0;
    loadWarnings.delete('tags');
    renderLoadWarn();
    renderTags();
    return;
  }
  let data;
  try {
    data = await api(`/api/tags?sources=${encodeURIComponent(ids.join(','))}`);
  } catch (e) {
    if (seq !== state.tagSeq) return;
    loadWarnings.set('tags', t('ui.error.tags_load', { detail: e.message }));
    renderLoadWarn();
    return;
  }
  if (seq !== state.tagSeq) return; // a newer read is under way
  loadWarnings.delete('tags');
  renderLoadWarn();
  state.starCount = data.star ?? 0;
  state.tags = data.tags || [];
  if (state.tag && state.tag !== 'star' && !state.tags.some((t) => String(t.id) === state.tag)) {
    state.tag = null;
    selectFolder(defaultFolderKey());
    return;
  }
  renderTags();
  renderList(); // the markers in the list need the tag names and colours
}

function renderTags() {
  const starIcon = icon('star-fill');
  starIcon.classList.add('star');
  const starred = el('button', { class: 'folder', type: 'button' }, [
    el('span', { class: 'name' }, [starIcon, t('ui.tags.starred')]),
    el('span', { class: 'count', text: String(state.starCount) }),
  ]);
  starred.setAttribute('aria-current', String(state.tag === 'star'));
  starred.addEventListener('click', () => {
    selectTag('star');
    goTo(1);
  });
  const items = [el('li', {}, [starred])];
  for (const t of state.tags) {
    const btn = el('button', { class: 'folder', type: 'button' }, [
      tagDot(t),
      el('span', { class: 'name', text: t.name || '' }),
      el('span', { class: 'count', text: String(t.count ?? 0) }),
    ]);
    btn.setAttribute('aria-current', String(state.tag === String(t.id)));
    btn.addEventListener('click', () => {
      selectTag(String(t.id));
      goTo(1);
    });
    items.push(el('li', {}, [btn]));
  }
  const addLi = el('li');
  const add = el('button', { class: 'folder', type: 'button' }, [el('span', { class: 'name' }, [icon('plus'), t('ui.tags.tag')])]);
  add.addEventListener('click', () => openTagInput(addLi));
  addLi.append(add);
  items.push(addLi);
  $('tag-list').replaceChildren(...items);
  updateBackList();
}

// Replaces the "＋ Tag" entry with an input. Enter creates the tag, Escape cancels.
function openTagInput(li) {
  const input = el('input', { class: 'tag-input', type: 'text', maxlength: '40', autocomplete: 'off', 'aria-label': t('ui.tags.new_name') });
  const warn = el('div', { class: 'warn', role: 'alert' });
  warn.hidden = true;
  input.addEventListener('keydown', async (e) => {
    if (e.isComposing) return;
    if (e.key === 'Escape') {
      e.preventDefault();
      renderTags();
      return;
    }
    if (e.key !== 'Enter') return;
    e.preventDefault();
    input.disabled = true;
    try {
      await createTag(input.value.trim()); // redraws the list on success
    } catch (err) {
      input.disabled = false;
      warn.textContent = err.message;
      warn.hidden = false;
      input.focus();
    }
  });
  li.replaceChildren(el('div', { class: 'tag-form' }, [input, warn]));
  input.focus();
}

// Creates a tag and returns it. The server answers with the star count and the tag list of the visible sources.
async function createTag(name) {
  const res = await fetch('/api/tags', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ name }),
    signal: timeoutSignal(), // a stalled request must not block the settings queue
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error(I18N.errorText(data, res.status));
  state.starCount = data.star ?? 0;
  state.tags = data.tags || [];
  renderTags();
  return state.tags.find((t) => t.name.toLowerCase() === name.toLowerCase());
}

// Sets the marks of one mail identity in every listed row (the same mail may sit in two sources).
function applyMarks(ident, marks) {
  if (!ident) return; // "" means the .eml is missing: no identity to match
  for (const m of state.messages) {
    if (m.ident !== ident) continue;
    m.starred = marks.starred;
    m.tags = marks.tags;
  }
  renderList();
  loadTags();
}

/* ---------- Message list ---------- */
function reloadList() {
  state.messages = [];
  state.total = 0;
  state.selectedKey = null;
  showEmpty('none', t('ui.mail.none_selected'));
  renderList();
  loadPage(true);
}

async function loadPage(reset) {
  const seq = ++state.listSeq;
  const ids = activeSourceIds();
  if (ids.length === 0) {
    state.messages = [];
    state.total = 0;
    pruneSelection([]);
    renderList();
    return;
  }
  const offset = reset ? 0 : state.messages.length;
  const params = new URLSearchParams({ sources: ids.join(','), offset: String(offset), limit: String(PAGE) });
  if (state.folder) params.set('folder', state.folder);
  if (state.tag) params.set('tag', state.tag);
  if (state.q) params.set('q', state.q);
  state.loading = true;
  updateMore();
  try {
    const data = await api(`/api/messages?${params}`);
    if (seq !== state.listSeq) return; // stale response
    state.total = data.total ?? 0;
    if (reset) pruneSelection(data.messages); // "Mehr laden" only adds rows, so it keeps the selection
    state.messages = reset ? data.messages : state.messages.concat(data.messages);
    state.loading = false; // renderList shows the empty notice only when nothing is loading
    renderList();
  } catch (e) {
    if (seq !== state.listSeq) return;
    $('messages').replaceChildren(el('li', { class: 'warn', text: t('ui.error.list_load', { detail: e.message }) }));
  } finally {
    if (seq === state.listSeq) {
      state.loading = false;
      updateMore();
    }
  }
}

function renderList() {
  $('count').textContent = t('ui.count.mails', { count: state.total });
  const combined = state.view === 'all';
  const tagById = new Map(state.tags.map((t) => [String(t.id), t]));
  $('messages').replaceChildren(...state.messages.map((m) => {
    const key = msgKey(m.source, m.id);
    const selected = key === state.selectedKey;
    const li = el('li', { class: 'msg' + (m.unread ? ' unread' : '') + (selected ? ' selected' : ''), role: 'option' });
    li.dataset.key = key;
    li.dataset.pick = rowKey(m);
    li.setAttribute('aria-selected', String(selected));
    const pick = el('span', { class: 'pick', 'aria-hidden': 'true' }, [icon('check')]);
    const unreadCol = el('span', { class: 'unread-col' });
    if (m.unread) unreadCol.append(el('span', { class: 'unread-dot', role: 'img', 'aria-label': t('ui.list.unread') }));
    const row = el('div', { class: 'row' });
    const src = combined ? sourceById(m.source) : null;
    if (src) row.append(sourceIcon(src, 'list'));
    row.append(
      el('span', { class: 'sender', text: senderText(m.sender) }),
      el('span', { class: 'time', text: listDate(m.time) }),
    );
    if (m.copies >= 2) row.append(el('span', { class: 'badge', text: t('ui.list.copies_badge', { count: m.copies }), title: t('ui.list.copies_title', { count: m.copies }) }));
    // Subject first, then star, tag dots and paperclip right-aligned at the end of the line.
    const subject = el('div', { class: 'subject' });
    subject.append(el('span', { class: 'subject-text', text: m.subject || t('ui.mail.no_subject') }));
    if (m.starred) {
      const star = icon('star-fill', t('ui.tags.starred'));
      star.classList.add('star');
      subject.append(star);
    }
    for (const id of m.tags || []) {
      const tag = tagById.get(String(id)); // IDs unknown to the sidebar are skipped
      if (tag) subject.append(tagDot(tag));
    }
    const archivedIn = (m.archived || []).map((id) => sourceById(id)?.name || id);
    if (archivedIn.length) {
      const text = t('ui.list.in_archive', { names: archivedIn.join(', ') });
      subject.append(el('span', { class: 'archived', title: text, role: 'img', 'aria-label': text }, [icon('archivebox')]));
    }
    if (m.attachments > 0) {
      const clip = icon('paperclip', t('ui.list.attachments'));
      clip.classList.add('clip');
      subject.append(clip);
    }
    li.append(pick, unreadCol, el('div', { class: 'text' }, [row, subject]));
    // A shift-click in selection mode picks a range; the browser must not start a text selection for it.
    li.addEventListener('mousedown', (e) => {
      if (state.selecting && e.shiftKey) e.preventDefault();
    });
    li.addEventListener('click', (e) => {
      if (state.selecting) {
        pickRow(rowKey(m), e.shiftKey);
        return;
      }
      selectMessage(m.source, m.id);
      goTo(2);
    });
    // Selection mode is operable by keyboard: arrows move between the rows, Space or Enter picks (Shift: range).
    li.addEventListener('focus', () => {
      if (!state.selecting) return;
      state.cursor = rowKey(m);
      paintSelection();
    });
    li.addEventListener('keydown', (e) => {
      if (!state.selecting || e.isComposing) return;
      if (e.key === ' ' || e.key === 'Enter') {
        e.preventDefault();
        pickRow(rowKey(m), e.shiftKey);
      } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        const rows = pickableRows();
        const i = rows.indexOf(li);
        rows[e.key === 'ArrowDown' ? Math.min(rows.length - 1, i + 1) : Math.max(0, i - 1)]?.focus();
      }
    });
    return li;
  }));
  // Drawn here, not only after a load, so a later renderList() cannot erase it.
  if (state.messages.length === 0 && !state.loading) $('messages').append(emptyListItem());
  updateMore();
  updateSelectUI();
}

/* ---------- Selection of mails ---------- */
// Shift-click adds the rows from the last clicked one to this one; a plain click toggles the row.
function pickRow(key, range) {
  if (range && state.anchor !== null) {
    const keys = state.messages.map(rowKey);
    state.selected = [...new Set([...state.selected, ...rangeSelection(keys, state.anchor, key)])];
  } else {
    state.selected = toggleSelection(state.selected, key);
  }
  state.anchor = key;
  updateSelectUI();
}

// The picked rows lose their selection (folder, tag, search or source changed). Selection mode stays on.
function clearSelection() {
  state.selected = [];
  state.anchor = null;
  updateSelectUI();
}

// Toggle button, bar with count and the button into the archive, check marks of the rows.
function updateSelectUI() {
  const btn = $('select-btn');
  btn.textContent = state.selecting ? t('ui.select.stop') : t('ui.select.start');
  btn.setAttribute('aria-pressed', String(state.selecting));
  $('messages').classList.toggle('selecting', state.selecting);
  $('messages').setAttribute('aria-multiselectable', String(state.selecting));
  $('select-bar').hidden = !state.selecting;
  const n = state.selected.length;
  $('select-count').textContent = t('ui.select.count', { count: n });
  const archives = state.sources.filter((s) => s.archive);
  const usable = usableArchives().length > 0;
  const over = n > TRANSFER_MAX;
  let note = archives.length === 0 ? t('ui.transfer.need_archive') : usable ? '' : t('ui.transfer.no_archive_reachable');
  if (over) note = t('ui.transfer.max', { max: TRANSFER_MAX });
  $('select-note').textContent = note;
  $('select-note').hidden = note === '';
  $('select-move').disabled = n === 0 || !usable || over;
  paintSelection();
}

// Check marks and tint follow state.selected; outside selection mode the rows show only the reader's selection.
function paintSelection() {
  const picked = new Set(state.selected);
  const rows = pickableRows();
  const cursorRow = rows.find((li) => li.dataset.pick === state.cursor) || rows[0];
  for (const li of rows) {
    const on = state.selecting && picked.has(li.dataset.pick);
    li.classList.toggle('picked', on);
    li.setAttribute('aria-selected', String(state.selecting ? on : li.classList.contains('selected')));
    // One row is in the tab order (roving tabindex); outside selection mode the rows are not focusable.
    if (state.selecting) li.tabIndex = li === cursorRow ? 0 : -1;
    else li.removeAttribute('tabindex');
  }
}

// The mail rows of the list, in display order (not the empty notice).
const pickableRows = () => [...$('messages').children].filter((li) => li.dataset.pick !== undefined);

// Picked rows that the loaded list no longer shows (moved, deleted, or gone after a reload) leave the selection.
function pruneSelection(rows) {
  const loaded = new Set(rows.map(rowKey));
  state.selected = state.selected.filter((k) => loaded.has(k));
  if (state.anchor !== null && !loaded.has(state.anchor)) state.anchor = null;
}

// Shown in the list when a load returns nothing: no mail in the folder, or no search hit.
// The search text goes in via textContent.
function emptyListItem() {
  const text = state.q ? t('ui.list.no_results', { query: state.q }) : t('ui.list.empty_folder');
  return el('li', { class: 'empty', role: 'presentation' }, [icon('tray'), el('div', { text })]);
}

function updateMore() {
  const btn = $('more');
  btn.hidden = state.messages.length >= state.total;
  btn.disabled = state.loading;
}

// j/k or arrow keys move through the list.
function move(delta) {
  const items = state.messages;
  if (!items.length) return;
  let idx = items.findIndex((m) => msgKey(m.source, m.id) === state.selectedKey);
  idx = idx < 0 ? 0 : Math.min(items.length - 1, Math.max(0, idx + delta));
  selectMessage(items[idx].source, items[idx].id);
  // On narrow windows a list that is not in front is off-screen; scrolling it would shift the app's layout.
  if (NARROW.matches && state.navLevel !== 1) return;
  $('messages').children[idx]?.scrollIntoView({ block: 'nearest' });
}

/* ---------- Reader ---------- */
function selectMessage(source, id) {
  state.selectedKey = msgKey(source, id);
  for (const li of $('messages').children) {
    const selected = li.dataset.key === state.selectedKey;
    li.classList.toggle('selected', selected);
    li.setAttribute('aria-selected', String(selected));
  }
  loadDetail(source, id);
}

const STALE_DELAY = 150; // ms: a faster load shows no change at all
let staleTimer = 0;

async function loadDetail(source, id) {
  const seq = ++state.detailSeq;
  startLoad(seq);
  try {
    const m = await api(`/api/message/${encodeURIComponent(source)}/${encodeURIComponent(id)}`);
    if (seq !== state.detailSeq) return; // a newer selection won
    renderMail(source, m);
  } catch (e) {
    if (seq !== state.detailSeq) return;
    showEmpty('error', t('ui.error.mail_load', { detail: e.message }));
  }
}

// While a new mail loads, the old one (if shown) is inert, so nothing acts on it, and dims only
// after STALE_DELAY. Without a shown mail, the loading text appears after STALE_DELAY.
function startLoad(seq) {
  clearTimeout(staleTimer);
  state.mailStale = true;
  const mail = $('mail');
  mail.inert = true;
  updateBar();
  staleTimer = setTimeout(() => {
    if (seq !== state.detailSeq) return;
    if (mail.hidden) showEmpty('loading', t('ui.mail.loading'));
    else mail.classList.add('stale');
  }, STALE_DELAY);
}

// Ends a load: no timer, no dimming, the reader takes input again.
function endLoad() {
  clearTimeout(staleTimer);
  state.mailStale = false;
  const mail = $('mail');
  mail.inert = false;
  mail.classList.remove('stale');
  updateBar();
}

// Shows a text in place of the mail. 'none': nothing selected; 'loading': slow load; 'error': load failed.
let emptyKind = null; // what the empty reader shows, so a language switch can redo the text of 'none'
function showEmpty(kind, text = '') {
  emptyKind = kind;
  if (kind !== 'loading') endLoad();
  $('placeholder').replaceChildren(icon('envelope'), el('div', { text }));
  $('placeholder').hidden = false;
  $('mail').hidden = true;
  updateBar();
}

// Short bar: shown while the mail is open and its big subject is scrolled out of the reader.
// Inert while a mail loads, so its star cannot act on the old mail.
let subjectOut = false;
function updateBar() {
  const bar = $('m-bar');
  const shown = subjectOut && !$('mail').hidden;
  bar.hidden = $('mail').hidden;
  bar.classList.toggle('show', shown);
  bar.inert = !shown || state.mailStale;
}
new IntersectionObserver(([entry]) => {
  subjectOut = !entry.isIntersecting;
  updateBar();
}).observe($('m-subject')); // root: the viewport, so it also works in the stacked narrow layout

function renderMail(source, m) {
  endLoad();
  state.mail = m;
  state.mailSource = source;
  showMarkWarn('');
  renderMarks();
  const subject = m.subject || t('ui.mail.no_subject');
  $('m-subject').textContent = subject;
  $('m-bar-subject').textContent = subject;
  renderSender(m);
  renderRecipients(m);
  renderAttachments(source, m);
  renderCopies(source, m);
  renderArchived(source, m);

  const err = $('m-error');
  err.hidden = !m.error;
  err.textContent = m.error ? t('ui.mail.server_hint', { text: m.error }) : '';

  state.mode = m.html ? 'html' : 'text';
  $('m-toggle').hidden = !(m.html && m.text);
  renderBody();

  $('placeholder').hidden = true;
  $('mail').hidden = false;
  $('reader').scrollTop = 0;
  subjectOut = false; // the subject is back at the top, so the short bar stays hidden
  updateBar();
}

// Avatar with initials and colour from the sender address; name and address as two lines when both exist.
function renderSender(m) {
  const from = m.from || null;
  const name = from?.name || '';
  const addr = from?.address || '';
  const avatar = $('m-avatar');
  avatar.textContent = personInitials(from);
  avatar.style.background = colorFor(addr);
  $('m-from').textContent = name || addr || t('ui.mail.unknown_sender');
  const twoLines = Boolean(name && addr && name !== addr);
  $('m-from-addr').textContent = twoLines ? addr : '';
  $('m-from-addr').hidden = !twoLines;
  $('m-date').textContent = longDate(m.time);
}

// To and Cc as footnote lines. More than 3 recipients in total: collapsed under a summary.
function renderRecipients(m) {
  const to = m.to || [];
  const cc = m.cc || [];
  const lines = [el('p', { class: 'recips-line' }, [t('ui.mail.to'), el('span', { id: 'm-to', text: addrList(to) || t('ui.mail.none') })])];
  const ccText = addrList(cc);
  if (ccText) lines.push(el('p', { class: 'recips-line', id: 'row-cc' }, [t('ui.mail.cc'), el('span', { id: 'm-cc', text: ccText })]));
  const all = [...to, ...cc];
  if (all.length > 3) {
    const names = all.map((a) => a.name || a.address || '?');
    const summary = t('ui.mail.to_summary', { first: names[0], second: names[1], count: all.length - 2 });
    $('m-recips').replaceChildren(el('details', { class: 'recips-more' }, [el('summary', { text: summary }), ...lines]));
  } else {
    $('m-recips').replaceChildren(...lines);
  }
}

function renderAttachments(source, m) {
  const list = m.attachments || [];
  $('m-attachments').replaceChildren(...list.map((a) => {
    const href = `/api/message/${encodeURIComponent(source)}/${encodeURIComponent(m.id)}/attachment/${encodeURIComponent(a.index)}`;
    const name = a.name || t('ui.mail.attachment', { index: a.index });
    const chip = [icon('paperclip'), el('span', { class: 'attach-name', text: name }), el('small', { text: fileSize(a.size) })];
    if (!canPreview(a.mime)) return el('a', { href }, chip);
    return el('span', { class: 'attach-pair' }, [
      el('a', { href: `${href}?inline=1`, target: '_blank', rel: 'noopener', title: t('ui.mail.attachment_open', { name }), 'data-preview': name, 'data-kind': previewKind(a.mime) }, chip),
      el('a', { href, class: 'attach-download', title: t('ui.mail.attachment_download', { name }), 'aria-label': t('ui.mail.attachment_download', { name }) }, [icon('arrow-down')]),
    ]);
  }));
  $('m-attachments').hidden = list.length === 0;
}

// "Im Archiv" for a mail the list shows as archived; the archive names go in the tooltip. Not known from the
// list row (e.g. a copy outside the open folder) means no marker.
function renderArchived(source, m) {
  const row = state.messages.find((x) => msgKey(x.source, x.id) === msgKey(source, m.id));
  const names = (row?.archived || []).map((id) => sourceById(id)?.name || id);
  const badge = $('m-archived');
  badge.hidden = names.length === 0;
  badge.title = names.length ? t('ui.list.in_archive', { names: names.join(', ') }) : '';
  badge.replaceChildren(...(names.length ? [icon('archivebox'), el('span', { text: t('ui.mail.in_archive') })] : []));
}

// Lists every copy of the open mail. The shown copy is marked; the others open on click, and the list selection stays.
function renderCopies(source, m) {
  const copies = m.copies || [];
  $('m-copies').hidden = copies.length < 2;
  if (copies.length < 2) {
    $('m-copies').replaceChildren();
    return;
  }
  const items = copies.map((c) => {
    const src = sourceById(c.source);
    const children = [];
    if (src) children.push(sourceIcon(src, 'list'));
    children.push(el('span', { text: c.name || c.source }));
    if (c.folders?.length) children.push(el('small', { text: c.folders.join(', ') }));
    if (c.source === source && c.id === m.id) return el('span', { class: 'copy', 'aria-current': 'true' }, children);
    const btn = el('button', { type: 'button', class: 'copy' }, children);
    btn.addEventListener('click', () => loadDetail(c.source, c.id));
    return btn;
  });
  $('m-copies').replaceChildren(el('span', { class: 'muted', text: t('ui.mail.in_sources', { count: copies.length }) }), ...items);
}

function renderBody() {
  const m = state.mail;
  for (const b of document.querySelectorAll('#m-toggle button')) {
    b.setAttribute('aria-pressed', String(b.dataset.mode === state.mode));
  }
  const box = $('m-body');
  if (state.mode === 'html' && m.html) {
    const frame = el('iframe', { sandbox: 'allow-same-origin', title: t('ui.mail.frame_title') });
    frame.addEventListener('load', () => {
      fitFrame(frame);
      try {
        // Re-fit when images inside the mail finish loading.
        frame.contentDocument.addEventListener('load', () => fitFrame(frame), true);
      } catch (_) { /* not accessible: keep min-height */ }
    });
    frame.srcdoc = FRAME_HEAD + m.html;
    box.replaceChildren(frame);
  } else {
    box.replaceChildren(el('pre', { text: m.text || t('ui.mail.no_content') }));
  }
}

function fitFrame(frame) {
  try {
    const h = frame.contentDocument.documentElement.scrollHeight;
    frame.style.height = `${Math.max(300, h + 8)}px`;
  } catch (_) { /* keep min-height */ }
}

/* ---------- Marks (star and tags of the open mail) ---------- */
const marksPending = new Set(); // keys of mails with a marks request under way; no second submit
let tagAdding = false; // a tag is being created from the input
const foldName = (s) => s.toLocaleLowerCase('de-DE');
// Key of the mail the reader shows right now, or null while it loads or shows nothing.
const shownKey = () => (state.mail && !state.mailStale && !$('mail').hidden ? msgKey(state.mailSource, state.mail.id) : null);

function showMarkWarn(text) {
  $('m-mark-warn').textContent = text;
  $('m-mark-warn').hidden = !text;
}

// Draws star, tag chips and tag input for the open mail. Controls are disabled while its request runs.
function renderMarks() {
  const m = state.mail;
  const markable = m.markable !== false;
  const busy = marksPending.has(msgKey(state.mailSource, m.id));
  for (const starBtn of [$('m-star'), $('m-bar-star')]) {
    starBtn.hidden = !markable;
    starBtn.replaceChildren(icon(m.starred ? 'star-fill' : 'star'));
    starBtn.setAttribute('aria-pressed', String(!!m.starred));
    starBtn.title = m.starred ? t('ui.mark.remove') : t('ui.mark.star');
    starBtn.disabled = busy;
  }
  $('m-tags').hidden = !markable;
  $('m-nomark').hidden = markable;

  const tagById = new Map(state.tags.map((t) => [String(t.id), t]));
  const current = (m.tags || []).map(String);
  const chips = [];
  for (const id of current) {
    const tag = tagById.get(id);
    if (!tag) continue; // IDs unknown to the sidebar are skipped
    const remove = el('button', { type: 'button', 'aria-label': t('ui.mark.remove_tag', { name: tag.name || '' }) }, [icon('xmark')]);
    remove.disabled = busy;
    remove.addEventListener('click', () => {
      saveMarks(state.mailSource, m.id, { tags: (m.tags || []).filter((x) => String(x) !== id) });
    });
    chips.push(el('span', { class: 'm-chip' }, [tagDot(tag), el('span', { text: tag.name || '' }), remove]));
  }
  $('m-chips').replaceChildren(...chips);
  renderSuggest();
}

// Own suggestion list instead of a <datalist>, whose native popup can open far from the input.
let suggestIndex = -1;
function suggestions() {
  const m = state.mail;
  if (!m) return [];
  const current = (m.tags || []).map(String);
  const typed = foldName($('m-tag-input').value.trim());
  return state.tags.filter((t) => !current.includes(String(t.id)) && foldName(t.name || '').includes(typed));
}

function renderSuggest() {
  const input = $('m-tag-input');
  const list = $('m-tag-suggest');
  const items = document.activeElement === input && state.mail ? suggestions() : [];
  suggestIndex = Math.min(suggestIndex, items.length - 1);
  list.hidden = items.length === 0;
  input.setAttribute('aria-expanded', String(items.length > 0));
  list.replaceChildren(...items.map((t, i) => {
    const li = el('li', { role: 'option', 'aria-selected': String(i === suggestIndex) }, [tagDot(t), el('span', { text: t.name || '' })]);
    li.addEventListener('mousedown', (e) => { // before the input loses focus
      e.preventDefault();
      input.value = t.name || '';
      addTagFromInput();
    });
    return li;
  }));
}

// Toggles the star of the open mail (button and "s" key).
function toggleStar() {
  if (!shownKey() || state.mail.markable === false) return;
  saveMarks(state.mailSource, state.mail.id, { starred: !state.mail.starred });
}

// Enter in the tag input: uses the tag with the same name (case-insensitive) or creates it, then sets it.
async function addTagFromInput() {
  const input = $('m-tag-input');
  const name = input.value.trim();
  const key = shownKey();
  if (!name || !key || tagAdding || marksPending.has(key)) return;
  const source = state.mailSource;
  const id = state.mail.id;
  tagAdding = true;
  try {
    let tag = state.tags.find((t) => foldName(t.name || '') === foldName(name));
    if (!tag) {
      try {
        tag = await createTag(name);
      } catch (e) {
        if (shownKey() === key) showMarkWarn(t('ui.error.tag_create', { detail: e.message }));
        return;
      }
      if (!tag) {
        if (shownKey() === key) showMarkWarn(t('ui.error.tag_create_plain'));
        return;
      }
      if (shownKey() !== key) return; // the reader moved on: the tag exists, this mail is unchanged
      renderMarks(); // the tag is new to the reader too
    }
    if ((state.mail.tags || []).some((x) => String(x) === String(tag.id))) {
      input.value = '';
      return;
    }
    if (await saveMarks(source, id, { tags: [...(state.mail.tags || []), tag.id] })) input.value = '';
  } finally {
    tagAdding = false;
    renderSuggest(); // the input may have been cleared
  }
}

// Sends one star or tag change. On success the list and sidebar take the new marks, and the reader
// too if that mail is still open. On failure the old marks stay and the reader shows a warning.
// Returns true on success.
async function saveMarks(source, id, change) {
  const key = msgKey(source, id);
  if (marksPending.has(key)) return false; // one request per mail at a time
  const row = state.messages.find((m) => msgKey(m.source, m.id) === key);
  const ident = row ? row.ident : null; // null if the mail is not in the list (e.g. after a filter change)
  marksPending.add(key);
  if (shownKey() === key) renderMarks();
  let marks;
  try {
    const res = await fetch(`/api/message/${encodeURIComponent(source)}/${encodeURIComponent(id)}/marks`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(change),
    });
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new Error(I18N.errorText(data, res.status));
    marks = data;
  } catch (e) {
    marksPending.delete(key);
    if (shownKey() === key) {
      showMarkWarn(t('ui.error.mark_save', { detail: e.message }));
      renderMarks();
    }
    return false;
  }
  marksPending.delete(key);
  if (ident) applyMarks(ident, marks); // also refreshes the sidebar counts
  else loadTags();
  if (shownKey() === key) {
    state.mail.starred = marks.starred;
    state.mail.tags = marks.tags;
    showMarkWarn('');
    renderMarks();
  }
  return true;
}

/* ---------- Job (index or fetch) ---------- */
const JOB_POLL_MS = 1000;
let jobTimer = 0;
let jobSeq = 0;
let lastJob = null; // last status read from /api/job
let dismissedJob = null; // `started` of the job whose failure list was hidden
let closeTimer = 0;
let fetchWaiting = false; // the fetch dialog shows that another job is running

// "mail_20261015_224804" -> "15.10.2026, 22:48"; any other name is shown as is.
function standText(name) {
  const m = /^mail_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/.exec(name || '');
  if (!m) return name || '';
  const d = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
  return Number.isNaN(d.getTime()) ? name : d.toLocaleString(I18N.locale(), {
    day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

function setFetchBusy(busy) {
  const btn = $('fetch-btn');
  $('fetch-label').textContent = busy ? t('ui.fetch.running') : t('ui.fetch.label');
  btn.classList.toggle('busy', busy);
}

const progressVisible = () => !$('fetch-progress').hidden;

function showForm() {
  $('fetch-progress').hidden = true;
  $('fetch-form').hidden = false;
}

function showProgress() {
  $('fetch-form').hidden = true;
  $('fetch-progress').hidden = false;
  $('fetch-retry').hidden = true;
  $('fetch-status').textContent = t('ui.fetch.running');
}

function showWarn(msg) {
  $('fetch-warn').textContent = msg || '';
  $('fetch-warn').hidden = !msg;
}

function renderLog(st) {
  const pre = $('fetch-log');
  pre.textContent = (st.log || []).map((entry) => I18N.msgText(entry)).join('\n');
  pre.scrollTop = pre.scrollHeight;
}

// Form starts empty; the server fills in the default email. Secrets are never kept.
function resetFetchForm() {
  $('fetch-form').reset();
}

function clearSecrets() {
  const f = $('fetch-form');
  f.elements.password.value = '';
  f.elements.mailbox_password.value = '';
  f.elements.totp.value = '';
}

// Footer of the folder pane: progress of a running job, its error, and failed sources of its log.
function renderJob(st) {
  const running = st.state === 'running';
  const fetching = running && st.kind === 'fetch';
  setFetchBusy(fetching);
  $('job-status').hidden = !running;
  if (running) {
    const prog = st.progress ? ` ${st.progress.done}/${st.progress.total}` : '';
    $('job-text').textContent = fetching ? t('ui.fetch.running')
      : st.kind === 'import' ? t('ui.job.importing', { progress: prog })
      : st.kind === 'cleanup' ? t('ui.job.cleaning')
      : st.kind === 'cli_install' ? t('ui.job.cli_installing')
      : st.kind === 'archive' ? t('ui.job.archiving', { progress: prog })
      : st.progress ? t('ui.job.indexing_progress', { done: st.progress.done, total: st.progress.total }) : t('ui.job.indexing');
    $('job-last').textContent = I18N.msgText((st.log || []).at(-1));
    // Bar: scaled to done/total when the total is known, otherwise an indeterminate sweep.
    const total = st.progress?.total || 0;
    $('job-progress').classList.toggle('indeterminate', total === 0);
    $('job-bar').style.transform = total === 0 ? '' : `scaleX(${Math.min(1, (st.progress.done || 0) / total)})`;
  }
  $('job-error').hidden = st.state !== 'error';
  $('job-error-text').textContent = st.state === 'error' ? (I18N.msgText(st.message) || t('ui.job.failed')) : '';
  renderJobWarnings(st);
  renderImportNote(st);
}

// Report of a finished import: counts, and the folders that could not be read.
function renderImportNote(st) {
  const done = st.state === 'done';
  // A finished transfer reports its counts; folder jobs of an archive have no counts and show no note.
  const transfer = done && st.kind === 'archive' && typeof st.result?.copied === 'number' ? st.result : null;
  const r = done && st.kind === 'import' ? st.result : null;
  $('job-note').hidden = !(transfer || r) || dismissedJob === st.started;
  if (transfer) {
    $('job-note-text').textContent = transferNoteParts(transfer).join(', ');
    $('job-note-list').replaceChildren();
    $('job-note-sum').textContent = t('ui.job.details', { count: 0 });
    $('job-note-details').hidden = true;
    return;
  }
  if (!r) return;
  const skipped = Object.values(r.skipped || {}).reduce((a, b) => a + b, 0);
  const parts = [t('ui.import.imported', { count: r.imported })];
  if (r.merged) parts.push(t('ui.import.merged', { count: r.merged }));
  if (skipped) parts.push(t('ui.import.skipped', { count: skipped }));
  if (r.unreadable) parts.push(t('ui.import.unreadable', { count: r.unreadable }));
  $('job-note-text').textContent = parts.join(', ');
  const folders = r.failed_folders || [];
  $('job-note-list').replaceChildren(...folders.map((f) => el('li', { text: t('ui.import.folder_unreadable', { name: f }) })));
  $('job-note-sum').textContent = t('ui.job.details', { count: folders.length });
  $('job-note-details').hidden = folders.length === 0;
}

// Log entries with these codes are sources that failed although the job itself finished.
const FAILED_SOURCE_CODES = new Set(['log.source_failed']);
function renderJobWarnings(st) {
  const failed = (st.log || []).filter((entry) => FAILED_SOURCE_CODES.has(entry.code));
  $('job-warn').hidden = failed.length === 0 || dismissedJob === st.started;
  $('job-warn-list').replaceChildren(...failed.map((entry) => el('li', { text: I18N.msgText(entry) })));
  $('job-warn-sum').textContent = t('ui.job.details', { count: failed.length });
  $('job-warn-details').hidden = failed.length === 0;
}

// Follows the job while the fetch dialog is open: progress of a running fetch, a notice while another job runs.
function updateFetchDialog(st) {
  const dlg = $('fetch-dialog');
  if (!dlg.open) return;
  if (st.state === 'running') {
    if (st.kind !== 'fetch') {
      if (!fetchWaiting) {
        showForm();
        showWarn(t('ui.error.busy'));
        fetchWaiting = true;
      }
      return;
    }
    fetchWaiting = false;
    if (!progressVisible()) {
      showProgress();
      showWarn('');
    }
    renderLog(st);
    return;
  }
  if (fetchWaiting) {
    fetchWaiting = false;
    showWarn('');
  }
  if (!progressVisible()) return;
  renderLog(st);
  if (st.state === 'done') {
    $('fetch-status').textContent = t('ui.fetch.done');
    showWarn('');
    clearTimeout(closeTimer);
    closeTimer = setTimeout(closeFetchDialog, 1500);
  } else if (st.state === 'error') {
    $('fetch-status').textContent = t('ui.fetch.failed_status');
    showWarn(I18N.msgText(st.message) || t('ui.fetch.failed'));
    $('fetch-retry').hidden = false;
  } else {
    showForm(); // idle: nothing is running any more
  }
}

function scheduleJobPoll() {
  clearTimeout(jobTimer);
  jobTimer = setTimeout(pollJob, JOB_POLL_MS);
}

// Reads /api/job. It repeats every second while a job runs and reacts once when the job has ended.
async function pollJob() {
  const seq = ++jobSeq;
  let st;
  try {
    st = await api('/api/job');
  } catch (_) {
    if (seq === jobSeq && lastJob?.state === 'running') scheduleJobPoll();
    return;
  }
  if (seq !== jobSeq) return; // a newer read is under way
  const wasRunning = lastJob?.state === 'running';
  lastJob = st;
  if (st.state === 'running') scheduleJobPoll();
  else clearTimeout(jobTimer);
  renderJob(st);
  updateFetchDialog(st);
  if (wasRunning && st.state !== 'running') jobFinished(st);
}

// The job has ended: reload what it may have changed. A finished fetch selects its source.
async function jobFinished(st) {
  await loadSources();
  const fetched = ['fetch', 'import'].includes(st.kind) && st.state === 'done' ? st.result : null;
  if (fetched && sourceById(fetched.source)) {
    state.view = fetched.source;
    storeView(fetched.source);
  }
  if (fetched?.export) $('stand').textContent = t('ui.status.as_of', { text: standText(fetched.export) });
  if (st.kind === 'cleanup') renderCleanupReport(st);
  if (st.kind === 'cli_install') {
    await loadConfig(); // cli_installed changed
    renderSettingsCliInstall();
  }
  renderRail();
  updateFetchArea();
  await loadFolders(true);
}

function openFetchDialog() {
  clearTimeout(closeTimer);
  closeTimer = 0;
  resetFetchForm();
  showWarn('');
  showForm();
  const dlg = $('fetch-dialog');
  if (!dlg.open) dlg.showModal();
  pollJob(); // shows the progress if a fetch is already running
}

function closeFetchDialog() {
  clearTimeout(closeTimer);
  closeTimer = 0;
  $('fetch-dialog').close();
}

$('fetch-btn').addEventListener('click', openFetchDialog);
$('fetch-cancel').addEventListener('click', closeFetchDialog);
$('fetch-close').addEventListener('click', closeFetchDialog);
$('job-warn-close').addEventListener('click', () => {
  dismissedJob = lastJob?.started ?? null;
  if (lastJob) { renderJobWarnings(lastJob); renderImportNote(lastJob); }
});
$('job-note-close').addEventListener('click', () => $('job-warn-close').click());

$('fetch-dialog').addEventListener('close', () => {
  clearTimeout(closeTimer);
  closeTimer = 0;
  fetchWaiting = false;
  clearSecrets();
});

$('fetch-retry').addEventListener('click', () => {
  showWarn('');
  resetFetchForm();
  showForm();
});

$('fetch-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  const submit = f.querySelector('button[type="submit"]');
  const source = state.view;
  if (!sourceById(source)) {
    showWarn(t('ui.fetch.no_source'));
    return;
  }
  const body = { source, email: f.elements.email.value.trim(), password: f.elements.password.value };
  const mailboxPw = f.elements.mailbox_password.value;
  const totp = f.elements.totp.value.trim();
  if (mailboxPw) body.mailbox_password = mailboxPw;
  if (totp) body.totp = totp;

  showWarn('');
  submit.disabled = true;
  const req = fetch('/api/fetch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
  });
  clearSecrets();

  let res;
  let data = null;
  try {
    res = await req;
    data = await res.json().catch(() => null);
  } catch (err) {
    submit.disabled = false;
    showWarn(t('ui.error.fetch_start', { detail: err.message }));
    return;
  }
  submit.disabled = false;

  if (res.status === 202) {
    showProgress();
    pollJob();
  } else if (res.status === 409) {
    // Another job holds the slot: the poll shows the fetch if it is one, else the notice.
    showWarn(t('ui.error.busy'));
    pollJob();
  } else {
    showWarn(I18N.errorText(data, res.status));
  }
});

/* ---------- Bibliothek und Quelle hinzufügen (Verzeichnis-Auswahl) ---------- */
const dirState = { path: '', parent: null, home: '', openable: false, library: false, seq: 0, file: null };
let libBusy = false;

function showLibWarn(msg) {
  $('library-warn').textContent = msg || '';
  $('library-warn').hidden = !msg;
}

const libMode = () => $('library-dialog').dataset.mode;

// Opening needs a library in the folder, adding a source needs a mail folder. Creating is always possible.
function updateLibSubmit() {
  const mode = libMode();
  const ok = mode === 'library' ? dirState.library
    : mode === 'import' ? Boolean(dirState.file) && $('import-name').value.trim() !== '' && importAvailable()
      : dirState.openable;
  $('library-submit').disabled = libBusy || !ok;
  $('library-create').disabled = libBusy;
  $('library-import').disabled = libBusy;
}

const importAvailable = () => Boolean(state.config?.outlook_import);

const humanSize = (n) => {
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = n;
  let i = -1;
  do { v /= 1024; i += 1; } while (v >= 1024 && i < units.length - 1);
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
};

function setLibBusy(busy) {
  libBusy = busy;
  $('library-cancel').disabled = busy;
  updateLibSubmit();
}

// Loads one directory into the picker. Late answers of superseded requests are dropped.
async function browseTo(path) {
  const seq = ++dirState.seq;
  let data;
  try {
    const files = libMode() === 'import' ? '&files=outlook' : '';
    data = await api(`/api/browse?path=${encodeURIComponent(path)}${files}`);
  } catch (e) {
    if (seq !== dirState.seq) return;
    showLibWarn(e.status === 400 ? t('ui.library.unreadable') : t('ui.library.read_error', { detail: e.message }));
    return;
  }
  if (seq !== dirState.seq) return;
  showLibWarn('');
  Object.assign(dirState, {
    path: data.path, parent: data.parent, home: data.home, openable: data.openable, library: data.library,
    file: null,
  });
  $('dir-path').value = data.path;
  $('dir-up').disabled = data.parent === null;
  $('dir-hint').textContent = libMode() === 'import'
    ? (importAvailable() ? t('ui.library.hint_import') : I18N.msgText(state.config?.install_hint))
    : libMode() === 'library'
    ? (data.library
      ? t('ui.library.hint_lib_here')
      : t('ui.library.hint_lib'))
    : (data.openable
      ? t('ui.library.hint_export_here')
      : t('ui.library.hint_export'));
  updateLibSubmit();
  const dirItems = data.dirs.length || (data.files || []).length ? data.dirs.map((d) => {
    const btn = el('button', { type: 'button' }, [icon(d.library ? 'archivebox' : 'folder'), el('span', { text: d.name })]);
    if (d.library) {
      btn.classList.add('library');
      btn.append(el('span', { class: 'tag', text: t('ui.library.tag_library') }));
    } else if (d.openable) {
      btn.append(el('span', { class: 'tag', text: t('ui.library.tag_export') }));
    }
    btn.addEventListener('click', () => browseTo(`${data.path.replace(/\/$/, '')}/${d.name}`));
    return el('li', { class: 'group-row' }, [btn]);
  }) : [el('li', { class: 'muted empty', text: t('ui.library.no_subdirs') })];
  const fileItems = (data.files || []).map((f) => {
    const btn = el('button', { type: 'button' }, [
      icon('envelope'), el('span', { text: f.name }), el('span', { class: 'size', text: humanSize(f.size) }),
    ]);
    btn.addEventListener('click', () => {
      dirState.file = `${data.path.replace(/\/$/, '')}/${f.name}`;
      $('dir-list').querySelectorAll('.picked').forEach((b) => b.classList.remove('picked'));
      btn.classList.add('picked');
      $('import-name').value = f.name.replace(/\.[^.]*$/, '');
      updateLibSubmit();
    });
    return el('li', { class: 'group-row' }, [btn]);
  });
  $('dir-list').replaceChildren(...dirItems, ...fileItems);
}

// mode "library": open or create a library. mode "source": add the folder as a source.
// mode "import": pick a .pst/.ost file and import it as a new source.
// required: first start without a library, so the dialog cannot be dismissed.
function openPicker(mode, required) {
  const dlg = $('library-dialog');
  const cfg = state.config || {};
  const isLib = mode === 'library';
  dlg.dataset.mode = mode;
  dlg.dataset.required = required ? '1' : '';
  const isImport = mode === 'import';
  $('library-title').textContent = isLib ? t('ui.library.title')
    : isImport ? t('ui.library.import_title') : t('ui.library.add_title');
  $('library-submit').textContent = isLib ? t('ui.library.open') : isImport ? t('ui.library.import_submit') : t('ui.library.add_submit');
  $('library-import').hidden = mode !== 'source';
  $('import-name-row').hidden = !isImport;
  $('import-name').value = '';
  $('library-create').hidden = !isLib;
  $('library-cancel').hidden = required;
  $('library-remember-row').hidden = !isLib;
  $('library-remember').checked = isLib && Boolean(cfg.saved) && cfg.saved === cfg.library;
  const legacy = isLib && Boolean(cfg.legacy);
  $('library-legacy-row').hidden = !legacy;
  $('library-legacy').checked = legacy;
  $('library-legacy-path').textContent = legacy ? cfg.legacy : '';
  showLibWarn('');
  setLibBusy(false);
  if (!dlg.open) dlg.showModal();
  const savedError = isLib ? cfg.saved_error : null;
  browseTo((mode === 'import' && dirState.path) || cfg.library || cfg.saved || cfg.home || '~').then(() => {
    if (savedError) showLibWarn(t('ui.library.saved_unavailable', { detail: I18N.msgText(savedError) }));
  });
}

// Global for the settings dialog (Task 8).
function openLibraryDialog(required) {
  openPicker('library', Boolean(required));
}
window.openLibraryDialog = openLibraryDialog;

// POST for the picker. Resolves to the JSON answer, or to null after the error is shown in the dialog.
async function postPicker(url, body) {
  setLibBusy(true);
  let res;
  let data = null;
  try {
    res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body),
    });
    data = await res.json().catch(() => null);
  } catch (err) {
    setLibBusy(false);
    showLibWarn(t('ui.error.library_open', { detail: err.message }));
    return null;
  }
  if (res.ok) return data;
  setLibBusy(false);
  showLibWarn(res.status === 409 ? t('ui.error.busy') : I18N.errorText(data, res.status));
  return null;
}

async function submitLibrary(create) {
  const ok = await postPicker('/api/library', {
    path: dirState.path,
    create,
    remember: $('library-remember').checked,
    add_legacy: !$('library-legacy-row').hidden && $('library-legacy').checked,
  });
  if (ok) location.reload(); // simplest way to drop all state of the previous library
}

// The new source is selected; its index job runs and the poll reloads everything when it ends.
async function addSource() {
  const before = new Set(state.sources.map((s) => s.id));
  const list = await postPicker('/api/sources', { path: dirState.path });
  if (!list) return;
  state.sources = list;
  const added = list.find((s) => !before.has(s.id));
  $('library-dialog').close();
  setLibBusy(false);
  renderRail();
  if (added) selectView(added.id);
  pollJob();
}

// A started import runs as a job; its source is selected by jobFinished.
async function startImport() {
  const ok = await postPicker('/api/import', { path: dirState.file, name: $('import-name').value.trim() });
  if (!ok) return;
  $('library-dialog').close();
  setLibBusy(false);
  dismissedJob = null;
  pollJob();
}

$('library-form').addEventListener('submit', (e) => {
  e.preventDefault();
  if (libBusy) return;
  const mode = libMode();
  if (mode === 'library') submitLibrary(false);
  else if (mode === 'import') startImport();
  else addSource();
});
$('library-import').addEventListener('click', () => {
  if (!libBusy) openPicker('import', false);
});
$('import-name').addEventListener('input', updateLibSubmit);
$('library-create').addEventListener('click', () => {
  if (!libBusy) submitLibrary(true);
});
$('library-cancel').addEventListener('click', () => $('library-dialog').close());
$('library-dialog').addEventListener('cancel', (e) => {
  if ($('library-dialog').dataset.required) e.preventDefault(); // Esc must not skip the initial choice
});
// Static glyph buttons get their SVG icons once at start-up; aria-label and title stay as they are.
$('rail-add').replaceChildren(icon('plus'));
$('rail-settings').replaceChildren(icon('gear'));
$('dir-up').replaceChildren(icon('chevron-up'));
$('dir-home').replaceChildren(icon('house'));
$('m-star').replaceChildren(icon('star'));
$('m-bar-star').replaceChildren(icon('star'));
showEmpty('none', t('ui.mail.none_selected')); // start: nothing selected yet
$('search-icon').replaceChildren(icon('search'));
// Notice and button icons are named in data-icon attributes (the close buttons carry aria-label).
for (const slot of document.querySelectorAll('[data-icon]')) slot.replaceChildren(icon(slot.dataset.icon));
$('dir-up').addEventListener('click', () => { if (dirState.parent) browseTo(dirState.parent); });
$('dir-home').addEventListener('click', () => browseTo(dirState.home || '~'));
$('dir-go').addEventListener('click', () => browseTo($('dir-path').value.trim()));
$('dir-path').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); browseTo($('dir-path').value.trim()); }
});
$('rail-add').addEventListener('click', () => openPicker('source', false));

/* ---------- Einstellungen ---------- */
const ICON_MAX_BYTES = 512 * 1024;
const ICON_FILE_TYPES = ['image/png', 'image/svg+xml'];
const REQUEST_TIMEOUT_MS = 30000;
const timeoutText = () => t('ui.error.timeout');
const jsonPost = (obj) => ({ headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(obj) });
const timeoutSignal = () => AbortSignal.timeout(REQUEST_TIMEOUT_MS);
const isTimeout = (err) => err?.name === 'TimeoutError';

function showSettingsWarn(msg) {
  $('settings-warn').textContent = msg || '';
  $('settings-warn').hidden = !msg;
}

// All settings work (changes, reloads) runs in one queue, one after another. A stalled request times out, so the queue moves on.
let settingsChain = Promise.resolve();
function queueSettings(task) {
  const run = settingsChain.then(task);
  settingsChain = run.catch(() => ({ ok: false }));
  return run;
}

// Each change builds its request when its turn comes, from the state then,
// so two quick clicks on ↑ or ↓ never send the same position twice.
function changeSettings(build) {
  return queueSettings(() => runChange(build));
}

async function runChange(build) {
  const req = build();
  if (!req) return { ok: false };
  showSettingsWarn('');
  let res;
  let data = null;
  try {
    res = await fetch(req.url, {
      method: 'POST',
      headers: { Accept: 'application/json', ...req.init.headers },
      body: req.init.body,
      signal: timeoutSignal(),
    });
    data = await res.json().catch(() => null);
  } catch (err) {
    if (isTimeout(err)) {
      // The server may have applied the change anyway: show what it holds now.
      showSettingsWarn(timeoutText());
      await reloadAdmin();
    } else {
      showSettingsWarn(t('ui.error.change_save', { detail: err.message }));
      renderSettingsSources(); // back to the stored values
    }
    return { ok: false };
  }
  if (!res.ok) {
    showSettingsWarn(res.status === 409 ? t('ui.error.busy') : I18N.errorText(data, res.status));
    renderSettingsSources();
    return { ok: false };
  }
  req.done?.(); // before the reload redraws, so the new icon URL is used
  await reloadAdmin(); // a failed reload keeps the state and shows its own warning
  return { ok: true, data };
}

// Reads sources and config together. On failure nothing is replaced, so rail and dialog keep what they show.
async function fetchAdminState() {
  const [sources, config] = await Promise.all([
    api('/api/sources', { signal: timeoutSignal() }),
    api('/api/config', { signal: timeoutSignal() }),
  ]);
  state.sources = sources;
  state.config = config;
  state.settings = config.settings || null;
  loadWarnings.delete('config');
  loadWarnings.delete('sources');
  renderLoadWarn();
  renderAutoPaused();
}

// The view stays if it is still shown (∑ only in the combined view), else the first entry of the rail.
function validView(view) {
  const combined = state.settings?.view === 'combined';
  if (view === 'all' && combined) return 'all';
  if (view && view !== 'all' && visibleSources().some((s) => s.id === view)) return view;
  if (combined) return 'all';
  return visibleSources()[0]?.id ?? null;
}

// Reloads sources and config and redraws rail and dialog. Proton fields are left alone, so typing is kept.
async function reloadAdmin() {
  const before = activeSourceIds().join(',');
  try {
    await fetchAdminState();
  } catch (err) {
    showSettingsWarn(isTimeout(err) ? timeoutText() : t('ui.error.list_update', { detail: err.message }));
    return false;
  }
  const next = validView(state.view);
  renderSettingsView();
  renderSettingsSources();
  renderSettingsLibrary();
  renderSettingsCleanup();
  if (next !== state.view) {
    if (next) {
      selectView(next);
    } else {
      state.view = null;
      renderRail();
      updateFetchArea();
      loadFolders();
    }
    return true;
  }
  renderRail();
  updateFetchArea();
  if (activeSourceIds().join(',') !== before) loadFolders(true);
  return true;
}

function renderSettingsView() {
  const view = state.settings?.view === 'single' ? 'single' : 'combined';
  for (const r of $('settings-view').querySelectorAll('input')) r.checked = r.value === view;
}

function renderSettingsProton(settings = state.settings) {
  const f = $('settings-proton').elements;
  f.proton_email.value = settings?.proton_email || '';
  f.cli.value = settings?.cli || '';
  const override = state.config?.cli_override || '';
  const note = $('settings-cli-override');
  note.textContent = override ? t('ui.settings.cli_ignored', { path: override }) : '';
  note.hidden = !override;
  renderSettingsCliInstall();
}

// Built-in installer: where the CLI is installed, or that it is missing; unsupported platforms only get the text.
function renderSettingsCliInstall() {
  const info = state.config?.cli_installed || null;
  const installable = !!state.config?.cli_installable;
  const installing = lastJob?.state === 'running' && lastJob.kind === 'cli_install';
  $('settings-cli-status').textContent = installing ? t('ui.job.cli_installing')
    : info ? (info.version ? t('ui.settings.cli_installed_version', { version: info.version, path: info.path })
      : t('ui.settings.cli_installed', { path: info.path }))
    : installable ? t('ui.settings.cli_missing') : t('ui.settings.cli_unsupported');
  const btn = $('settings-cli-install');
  btn.hidden = !installable;
  btn.disabled = installing;
  btn.textContent = info ? t('ui.settings.cli_reinstall') : t('ui.settings.cli_install');
  $('settings-cli-install-hint').hidden = !installable;
}

function renderSettingsLibrary() {
  $('settings-library-path').textContent = state.config?.library || t('ui.mail.none');
}

// Focus survives a redraw of the list: the same control of the same source, else the same control of
// the neighbouring source, else the dialog heading.
function focusKey() {
  const a = document.activeElement;
  const row = a?.closest?.('#settings-sources li[data-id]');
  if (!row || !a.dataset.action) return null;
  return { id: row.dataset.id, action: a.dataset.action, index: [...row.parentNode.children].indexOf(row) };
}

function restoreFocus(key) {
  if (!key) return;
  const rows = [...$('settings-sources').children].filter((li) => li.dataset.id);
  const row = rows.find((li) => li.dataset.id === key.id) || rows[key.index] || rows[key.index - 1];
  const usable = (c) => c && !c.disabled && !c.hidden;
  const target = row && [key.action, 'name', 'select']
    .map((action) => row.querySelector(`[data-action="${action}"]`))
    .find(usable);
  if (target) target.focus();
  else $('settings-title').focus();
}

function moveReq(id, delta) {
  return () => {
    const i = state.sources.findIndex((s) => s.id === id);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= state.sources.length) return null;
    return { url: `/api/sources/${encodeURIComponent(id)}`, init: jsonPost({ position: j }) };
  };
}

async function uploadIcon(src, input) {
  const file = input.files?.[0];
  input.value = ''; // choosing the same file again must fire change
  if (!file) return;
  if (!ICON_FILE_TYPES.includes(file.type)) {
    showSettingsWarn(t('ui.settings.icon_type'));
    return;
  }
  if (file.size > ICON_MAX_BYTES) {
    showSettingsWarn(t('ui.settings.icon_size'));
    return;
  }
  let bytes;
  try {
    bytes = await file.arrayBuffer();
  } catch (err) {
    showSettingsWarn(t('ui.error.file_read', { detail: err.message }));
    return;
  }
  changeSettings(() => ({
    url: `/api/sources/${encodeURIComponent(src.id)}/icon`,
    init: { headers: { 'Content-Type': file.type }, body: bytes },
    done: () => { state.iconVer[src.id] = (state.iconVer[src.id] || 0) + 1; },
  }));
}

function settingsRow(src, i, count) {
  const url = `/api/sources/${encodeURIComponent(src.id)}`;
  const icon = src.icon || {};
  const kind = icon.type === 'emoji' || icon.type === 'image' ? icon.type : 'auto';
  const li = el('li', { class: 'group-row stack', 'data-id': src.id });

  const name = el('input', { type: 'text', class: 'name', 'aria-label': t('ui.folder.name'), autocomplete: 'off', 'data-action': 'name' });
  name.value = src.name;
  name.addEventListener('change', () => {
    const v = name.value.trim();
    if (!v) {
      name.value = src.name;
      showSettingsWarn(t('ui.error.name_empty'));
      return;
    }
    if (v !== src.name) changeSettings(() => ({ url, init: jsonPost({ name: v }) }));
  });
  const up = el('button', { class: 'btn small', type: 'button', title: t('ui.settings.up'), 'aria-label': t('ui.settings.up_named', { name: src.name }), text: '↑', 'data-action': 'up' });
  up.disabled = i === 0;
  up.addEventListener('click', () => changeSettings(moveReq(src.id, -1)));
  const down = el('button', { class: 'btn small', type: 'button', title: t('ui.settings.down'), 'aria-label': t('ui.settings.down_named', { name: src.name }), text: '↓', 'data-action': 'down' });
  down.disabled = i === count - 1;
  down.addEventListener('click', () => changeSettings(moveReq(src.id, 1)));
  li.append(el('div', { class: 'line' }, [sourceIcon(src, 'rail'), name, up, down]));

  const select = el('select', { 'aria-label': t('ui.settings.icon_of', { name: src.name }), 'data-action': 'select' }, [
    el('option', { value: 'auto', text: t('ui.settings.icon_auto') }),
    el('option', { value: 'emoji', text: t('ui.settings.icon_emoji') }),
    el('option', { value: 'image', text: t('ui.settings.icon_image') }),
  ]);
  select.value = kind;
  const emoji = el('input', { type: 'text', class: 'emoji', 'aria-label': t('ui.settings.icon_emoji'), autocomplete: 'off', 'data-action': 'emoji' });
  emoji.value = icon.type === 'emoji' ? icon.value : '';
  emoji.hidden = kind !== 'emoji';
  // An empty value is sent too: the server refuses it and the list is redrawn with the stored emoji.
  emoji.addEventListener('change', () => {
    const v = emoji.value.trim();
    changeSettings(() => ({ url, init: jsonPost({ icon: { type: 'emoji', value: v } }) }));
  });
  const file = el('input', { type: 'file', accept: 'image/png,image/svg+xml', 'aria-label': t('ui.settings.image_for', { name: src.name }), 'data-action': 'file' });
  file.hidden = kind !== 'image';
  file.addEventListener('change', () => uploadIcon(src, file));
  select.addEventListener('change', () => {
    emoji.hidden = select.value !== 'emoji';
    file.hidden = select.value !== 'image';
    if (select.value === 'auto') changeSettings(() => ({ url, init: jsonPost({ icon: { type: 'auto' } }) }));
    else if (select.value === 'emoji') emoji.focus();
  });

  const hide = el('button', { class: 'btn small', type: 'button', text: src.hidden ? t('ui.settings.show') : t('ui.settings.hide'), 'data-action': 'hide' });
  hide.addEventListener('click', () => changeSettings(() => ({ url, init: jsonPost({ hidden: !src.hidden }) })));
  const removeBtn = el('button', { class: 'btn small', type: 'button', text: t('ui.settings.remove'), 'data-action': 'remove' });
  removeBtn.addEventListener('click', () => {
    if (confirm(t('ui.settings.remove_confirm'))) {
      changeSettings(() => ({ url: `${url}/remove`, init: jsonPost({}) }));
    }
  });
  li.append(el('div', { class: 'line' }, [select, emoji, file, hide, removeBtn]));
  if (src.kind === 'proton') li.append(targetField(src));

  const info = [
    t('ui.count.mails', { count: src.count ?? 0 }),
    src.hidden ? t('ui.source.hidden') : '',
    src.available ? '' : t('ui.source.unreachable'),
  ].filter(Boolean).join(' · ');
  li.append(el('div', { class: 'hint', text: info }));
  return li;
}

// Redraws the source list from state and restores the focused control if it still exists.
function renderSettingsSources() {
  const key = focusKey();
  const list = state.sources;
  $('settings-sources').replaceChildren(...(list.length
    ? list.map((src, i) => settingsRow(src, i, list.length))
    : [el('li', { class: 'group-row muted', text: t('ui.sources.none') })]));
  restoreFocus(key);
}

/* ---------- Archive creation and automatic archiving ---------- */
const ARCHIVE_NAME_MAX = 100;
const targetHelp = () => t('ui.settings.target_help');

// Ziel-Archiv of a proton source: the archive that takes the mails deleted online. "" turns the automation off.
function targetField(src) {
  const archives = state.sources.filter((s) => s.archive && s.available);
  const current = src.archive_target || '';
  const select = el('select', { 'aria-label': t('ui.settings.target_of', { name: src.name }), 'aria-describedby': `target-help-${src.id}`, 'data-action': 'target' }, [
    el('option', { value: '', text: t('ui.settings.target_none') }),
    ...archives.map((a) => el('option', { value: a.id, text: a.name })),
  ]);
  // A target that is no longer reachable stays selected, so the select does not claim "Keins".
  const kept = state.sources.find((s) => s.id === current && s.archive && !s.available);
  if (kept) select.append(el('option', { value: kept.id, text: t('ui.source.unreachable_named', { name: kept.name }) }));
  select.value = current;
  select.disabled = !state.sources.some((s) => s.archive);
  select.addEventListener('change', () => {
    const url = `/api/sources/${encodeURIComponent(src.id)}`;
    changeSettings(() => ({ url, init: jsonPost({ archive_target: select.value }) }));
  });
  const field = el('label', { class: 'field' }, [el('span', { text: t('ui.settings.target') }), select]);
  const parts = [el('div', { class: 'hint', id: `target-help-${src.id}`, text: targetHelp() })];
  if (select.disabled) parts.push(el('div', { class: 'hint', text: t('ui.transfer.need_archive') }));
  return el('div', {}, [field, ...parts]);
}

function setArchiveForm(open) {
  $('settings-archive-new').hidden = !open;
  $('settings-archive-open').hidden = open;
  $('settings-archive-open').setAttribute('aria-expanded', String(open));
  if (open) $('settings-archive-new').elements.name.focus();
}

$('settings-archive-open').addEventListener('click', () => setArchiveForm(true));
$('settings-archive-cancel').addEventListener('click', () => {
  $('settings-archive-new').reset();
  setArchiveForm(false);
  $('settings-archive-open').focus();
});
$('settings-archive-new').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = e.currentTarget.elements.name;
  const name = input.value.trim();
  if (!name) {
    showSettingsWarn(t('ui.error.name_empty'));
    return;
  }
  if ([...name].length > ARCHIVE_NAME_MAX) {
    showSettingsWarn(t('ui.folder.name_max', { max: ARCHIVE_NAME_MAX }));
    return;
  }
  const res = await changeSettings(() => ({ url: '/api/archives', init: jsonPost({ name }) }));
  if (!res.ok) {
    // The form stays with the typed name; the message is shown at the top of the dialog.
    $('settings-warn').scrollIntoView({ block: 'nearest' });
    return;
  }
  input.value = '';
  setArchiveForm(false);
  $('settings-archive-open').focus();
  if (res.data?.id) selectView(res.data.id);
});

// Notices for proton sources whose automatic archiving is held back. Redrawn with every source refresh.
function renderAutoPaused() {
  const paused = visibleSources().filter((s) => s.kind === 'proton' && s.auto_paused);
  $('auto-paused').replaceChildren(...paused.map((src) => autoPausedNotice(src, paused.length > 1)));
}

// Source ids with a force or discard request in flight, or a forced job still running: their buttons stay off.
const autoBusy = new Set();

// The forced job is followed to its end like a folder job: a failure is shown in job-error by pollJob,
// then the sources are reloaded, which drops the notice once the pause is gone.
async function followAutoJob() {
  dismissedJob = null;
  for (let i = 0; i < 600; i++) {
    let st = null;
    try {
      st = await api('/api/job');
    } catch (_) { /* read again on the next round */ }
    if (st && st.state !== 'running') break;
    await new Promise((resolve) => setTimeout(resolve, JOB_POLL_MS));
  }
  await pollJob();
  await loadSources();
}

// A failed request is shown in job-error, which survives the redraw of the notices.
function showAutoError(msg) {
  $('job-error-text').textContent = msg;
  $('job-error').hidden = !msg;
}

function autoPausedNotice(src, named) {
  const n = src.auto_paused.missing;
  const text = `${named ? `${src.name}: ` : ''}${t('ui.settings.auto_paused', { count: n })}`;
  const url = `/api/sources/${encodeURIComponent(src.id)}/auto`;
  const keep = el('button', { class: 'btn small primary', type: 'button', text: t('ui.settings.auto_force') });
  const drop = el('button', { class: 'btn small', type: 'button', text: t('ui.settings.auto_drop') });
  // The forced job copies from the source's mails, so it needs the source reachable.
  if (!src.available) {
    keep.disabled = true;
    keep.title = t('ui.settings.source_unreachable');
  }
  // Busy ends with a redraw, so a notice rebuilt during the job is drawn with the state then.
  const setBusy = (busy) => {
    keep.disabled = busy || !src.available;
    drop.disabled = busy;
  };
  setBusy(autoBusy.has(src.id));
  keep.addEventListener('click', async () => {
    if (autoBusy.has(src.id)) return;
    if (!confirm(t('ui.settings.auto_force_confirm'))) return;
    autoBusy.add(src.id);
    setBusy(true);
    try {
      const err = await postJob(url, { action: 'force' }, t('ui.error.auto_start'));
      if (err) {
        showAutoError(err);
        return;
      }
      await followAutoJob(); // the notice drops with the reload once the job has ended
    } finally {
      autoBusy.delete(src.id);
      renderAutoPaused();
    }
  });
  drop.addEventListener('click', async () => {
    if (autoBusy.has(src.id)) return;
    autoBusy.add(src.id);
    setBusy(true);
    try {
      const err = await postJob(url, { action: 'discard' }, t('ui.error.auto_discard'));
      if (err) {
        showAutoError(err);
        return;
      }
      await loadSources();
    } finally {
      autoBusy.delete(src.id);
      renderAutoPaused();
    }
  });
  return el('div', { class: 'notice warn', role: 'status' }, [
    el('span', { class: 'notice-icon' }, [icon('warn')]),
    el('div', { class: 'notice-body' }, [
      el('div', { text }),
      el('div', { class: 'actions' }, [keep, drop]),
    ]),
  ]);
}

// Sends one tag change (rename, colour, delete). Errors carry the server's message.
async function postTag(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: timeoutSignal(),
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error(I18N.errorText(data, res.status));
  return data;
}

// Removes a deleted tag from the rows and the open mail, so no chip or marker points at it.
function forgetTag(id) {
  const key = String(id);
  const keep = (tags) => (tags || []).filter((x) => String(x) !== key);
  for (const m of state.messages) m.tags = keep(m.tags);
  if (state.mail) state.mail.tags = keep(state.mail.tags);
}

// Runs one tag change in the settings queue. Then the tags are read again, so a failed rename
// shows the stored name again. Sidebar, list and open reader follow the new tags.
function changeTags(task) {
  return queueSettings(async () => {
    showSettingsWarn('');
    try {
      await task();
    } catch (err) {
      showSettingsWarn(isTimeout(err) ? timeoutText() : t('ui.error.change_save', { detail: err.message }));
    }
    await loadTags(); // the server may have applied a timed-out change anyway
    renderSettingsTags();
    renderList();
    if (shownKey()) renderMarks(); // chips show the new names and colours
  });
}

function settingsTagRow(tagItem) {
  const url = `/api/tags/${encodeURIComponent(tagItem.id)}`;
  const li = el('li', { class: 'group-row stack' });

  const name = el('input', { type: 'text', class: 'name', 'aria-label': t('ui.folder.name'), maxlength: '40', autocomplete: 'off' });
  name.value = tagItem.name || '';
  name.addEventListener('change', () => {
    const v = name.value.trim();
    if (!v) {
      name.value = tagItem.name || '';
      showSettingsWarn(t('ui.error.name_empty'));
      return;
    }
    if (v !== tagItem.name) changeTags(() => postTag(url, { name: v }));
  });
  const color = el('input', { type: 'color', 'aria-label': t('ui.settings.color_of', { name: tagItem.name || '' }) });
  // Only a colour in the #rrggbb form the server accepts goes into the colour input.
  color.value = COLOR_RE.test(tagItem.color || '') ? tagItem.color.toLowerCase() : '#808080';
  color.addEventListener('change', () => {
    if (COLOR_RE.test(color.value) && color.value.toLowerCase() !== (tagItem.color || '').toLowerCase()) {
      changeTags(() => postTag(url, { color: color.value }));
    }
  });
  const del = el('button', { class: 'btn small', type: 'button', text: t('ui.folder.delete') });
  del.addEventListener('click', () => {
    const n = tagItem.count ?? 0;
    if (!confirm(t('ui.settings.tag_delete_confirm', { name: tagItem.name || '', count: n }))) return;
    changeTags(async () => {
      await postTag(`${url}/remove`, {});
      forgetTag(tagItem.id);
    });
  });
  li.append(el('div', { class: 'line' }, [tagDot(tagItem), name, color, del]));
  li.append(el('div', { class: 'hint', text: t('ui.count.mails', { count: tagItem.count ?? 0 }) }));
  return li;
}

function renderSettingsTags() {
  $('settings-tags').replaceChildren(...(state.tags.length
    ? state.tags.map(settingsTagRow)
    : [el('li', { class: 'group-row muted', text: t('ui.settings.no_tags') })]));
}

/* ---------- Duplikate aufräumen (Einstellungen) ---------- */
// The preview is valid only for the target, reference and action it was made for: any change clears it.
let cleanupPreview = null; // { key, action, data } of the last preview
let cleanupSeq = 0; // a preview answer that arrives after a change is dropped

const cleanupKey = () => JSON.stringify([$('cleanup-target').value, $('cleanup-reference').value, cleanupAction()]);
const cleanupAction = () => ($('cleanup-delete').checked ? 'delete' : 'link');
const nameOf = (id) => sourceById(id)?.name ?? '';

// Targets are the visible Outlook sources. References are all other visible sources, the first Proton one by default.
function renderSettingsCleanup() {
  const targets = visibleSources().filter((s) => s.kind === 'outlook');
  const prevTarget = $('cleanup-target').value;
  const prevRef = $('cleanup-reference').value;
  $('cleanup-target').replaceChildren(...targets.map((s) => el('option', { value: s.id, text: s.name })));
  if (targets.some((s) => s.id === prevTarget)) $('cleanup-target').value = prevTarget;

  const target = $('cleanup-target').value;
  const refs = visibleSources().filter((s) => s.id !== target);
  $('cleanup-reference').replaceChildren(...refs.map((s) => el('option', { value: s.id, text: s.name })));
  const pick = refs.some((s) => s.id === prevRef) ? prevRef : (refs.find((s) => s.kind === 'proton') || refs[0])?.id;
  if (pick) $('cleanup-reference').value = pick;

  const usable = targets.length > 0 && refs.length > 0;
  $('cleanup-none').textContent = targets.length ? t('ui.settings.no_reference') : t('ui.settings.no_outlook');
  $('cleanup-none').hidden = usable;
  $('cleanup-fields').disabled = !usable;
  updateCleanupHint();
  // A rebuilt list may have chosen another source: a preview of the old pair must not stay.
  if (cleanupPreview && cleanupPreview.key !== cleanupKey()) resetCleanupPreview();
  else updateCleanupControls();
}

function updateCleanupHint() {
  $('cleanup-delete-hint').textContent = t('ui.settings.cleanup_delete_hint', { target: nameOf($('cleanup-target').value), reference: nameOf($('cleanup-reference').value) });
}

// Back to "no preview": a late answer for the old selection is ignored through cleanupSeq.
function resetCleanupPreview() {
  cleanupSeq++;
  cleanupPreview = null;
  $('cleanup-preview').hidden = true;
  $('cleanup-preview').replaceChildren();
  $('cleanup-link').disabled = false;
  updateCleanupControls();
}

// The link option is off when the pair cannot be linked; "Aufräumen…" needs a preview with something to do.
function updateCleanupControls() {
  const usable = !$('cleanup-fields').disabled;
  const p = cleanupPreview;
  $('cleanup-link').disabled = !usable || Boolean(p && p.data.link_possible === false);
  $('cleanup-run-btn').disabled = !usable || !p || p.data.count < 1;
}

function renderCleanupPreview() {
  const p = cleanupPreview;
  const box = $('cleanup-preview');
  box.hidden = !p;
  if (!p) return;
  const d = p.data;
  const freed = p.action === 'link' ? d.bytes_link : d.bytes_delete;
  const lines = [el('p', { text: t('ui.cleanup.duplicates', { count: d.count, freed }) })];
  if (d.already_linked) lines.push(el('p', { text: t('ui.cleanup.already_linked', { count: d.already_linked }) }));
  if (d.missing_reference) lines.push(el('p', { text: t('ui.cleanup.missing_reference', { count: d.missing_reference }) }));
  if (d.link_possible === false && d.link_reason) lines.push(el('p', { text: I18N.msgText(d.link_reason) }));
  box.replaceChildren(...lines);
}

async function previewCleanup() {
  showSettingsWarn('');
  const seq = ++cleanupSeq;
  const target = $('cleanup-target').value;
  const reference = $('cleanup-reference').value;
  let res;
  let data = null;
  try {
    res = await fetch(`/api/cleanup/preview?${new URLSearchParams({ target, reference })}`, {
      headers: { Accept: 'application/json' },
      signal: timeoutSignal(),
    });
    data = await res.json().catch(() => null);
  } catch (err) {
    if (seq === cleanupSeq) showSettingsWarn(isTimeout(err) ? timeoutText() : t('ui.error.preview_load', { detail: err.message }));
    return;
  }
  if (seq !== cleanupSeq) return; // the selection changed while the preview was loading
  if (!res.ok) {
    showSettingsWarn(res.status === 409 ? t('ui.error.busy') : I18N.errorText(data, res.status));
    return;
  }
  // A pair that cannot be linked falls back to deleting, so the preview shows what will happen.
  if (data.link_possible === false) $('cleanup-delete').checked = true;
  cleanupPreview = { key: cleanupKey(), action: cleanupAction(), data };
  renderCleanupPreview();
  updateCleanupControls();
}

// Asks for confirmation, then starts the cleanup as a job. The job poll shows its progress and report.
async function runCleanup() {
  const p = cleanupPreview;
  if (!p || p.data.count < 1) return;
  const target = $('cleanup-target').value;
  const reference = $('cleanup-reference').value;
  const { action, data } = p;
  const question = action === 'link'
    ? t('ui.cleanup.confirm_link', { count: data.count, target: nameOf(target), reference: nameOf(reference) })
    : t('ui.cleanup.confirm_delete', { count: data.count, target: nameOf(target), reference: nameOf(reference) });
  if (!confirm(question)) return;

  $('cleanup-run-btn').disabled = true; // a second click would only answer 409
  $('cleanup-report').hidden = true;
  const res = await changeSettings(() => ({ url: '/api/cleanup', init: jsonPost({ target, reference, action }) }));
  if (!res.ok) {
    updateCleanupControls();
    return;
  }
  resetCleanupPreview(); // the files changed, so the old numbers are stale
  dismissedJob = null;
  pollJob();
}

// Report of a finished cleanup, shown in its section.
function renderCleanupReport(st) {
  const r = st.state === 'done' ? st.result : null;
  if (!r) return;
  $('cleanup-report').textContent = t('ui.cleanup.report', { linked: r.linked, deleted: r.deleted, skipped: r.skipped, failed: r.failed, freed: r.freed, log: r.log_file });
  $('cleanup-report').hidden = false;
  resetCleanupPreview();
}

function renderSettingsAll() {
  renderSettingsView();
  renderSettingsProton();
  renderSettingsSources();
  renderSettingsTags();
  renderSettingsLibrary();
  renderSettingsCleanup();
}

async function openSettings() {
  showSettingsWarn('');
  renderSettingsAll();
  const dlg = $('settings-dialog');
  if (!dlg.open) dlg.showModal();
  await queueSettings(async () => {
    await reloadAdmin();
    await loadTags(); // the visible sources may have changed
    renderSettingsTags();
  });
}

$('rail-settings').addEventListener('click', openSettings);
$('settings-tag-new').addEventListener('submit', (e) => {
  e.preventDefault();
  const input = e.currentTarget.elements.name;
  const name = input.value.trim();
  if (!name) {
    showSettingsWarn(t('ui.error.name_empty'));
    return;
  }
  changeTags(async () => {
    await createTag(name);
    input.value = '';
  });
});
$('settings-view').addEventListener('change', async (e) => {
  const view = e.target.value;
  if (!(await changeSettings(() => ({ url: '/api/settings', init: jsonPost({ view }) }))).ok) renderSettingsView();
});
$('settings-proton').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.currentTarget.elements;
  const body = { proton_email: f.proton_email.value.trim(), cli: f.cli.value.trim() };
  const res = await changeSettings(() => ({ url: '/api/settings', init: jsonPost(body) }));
  // On failure the typed values stay; on success the fields show what the server stored.
  if (res.ok && res.data?.settings) renderSettingsProton(res.data.settings);
});
$('cleanup-target').addEventListener('change', () => {
  renderSettingsCleanup();
  resetCleanupPreview();
});
$('cleanup-reference').addEventListener('change', () => {
  updateCleanupHint();
  resetCleanupPreview();
});
for (const r of [$('cleanup-link'), $('cleanup-delete')]) r.addEventListener('change', resetCleanupPreview);
$('cleanup-preview-btn').addEventListener('click', previewCleanup);
$('settings-cli-install').addEventListener('click', async () => {
  showSettingsWarn('');
  const error = await postJob('/api/cli/install', {}, t('ui.settings.cli_install'));
  if (error) {
    showSettingsWarn(error);
    return;
  }
  lastJob = { state: 'running', kind: 'cli_install' }; // until the first poll answers
  renderSettingsCliInstall();
  pollJob();
});
$('cleanup-run-btn').addEventListener('click', runCleanup);
$('settings-library-change').addEventListener('click', () => {
  $('settings-dialog').close();
  openLibraryDialog(false);
});
$('settings-close').addEventListener('click', () => $('settings-dialog').close());

// Attachment preview: a plain click opens the modal; modifier and middle clicks keep the link's new tab.
function openPreview(link) {
  const name = link.dataset.preview;
  $('preview-title').textContent = name;
  $('preview-download').href = link.href.replace(/\?inline=1$/, '');
  $('preview-download').setAttribute('download', name);
  const audio = link.dataset.kind === 'audio';
  $('preview-dialog').dataset.kind = link.dataset.kind;
  if (audio) $('preview-audio').src = link.href;
  else $('preview-frame').src = link.href;
  $('preview-dialog').showModal();
}
$('m-attachments').addEventListener('click', (e) => {
  const link = e.target.closest('a[data-preview]');
  if (!link || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  openPreview(link);
});
$('preview-close').addEventListener('click', () => $('preview-dialog').close());
$('preview-dialog').addEventListener('close', () => {
  $('preview-frame').src = 'about:blank';
  $('preview-audio').pause();
  $('preview-audio').removeAttribute('src');
  $('preview-audio').load(); // drops the buffered file
});

/* ---------- Narrow layout: navigation levels ---------- */
// Narrow windows show one level at a time: 0 folders and tags, 1 message list, 2 mail.
// Wide windows keep all columns; there only state.navLevel changes and nothing is transformed.
const NARROW = matchMedia('(max-width: 900px)');
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)');
const levelEls = [$('nav-root'), $('list-pane'), $('reader')];
let navAnim = null; // running spring: { s: {x, v}, target, id }
let navFade = null; // running fade-in (reduced motion)

function updateBackList() {
  let text = t('ui.nav.folder');
  if (state.tag === 'star') text = t('ui.tags.starred');
  else if (state.tag) text = state.tags.find((t) => String(t.id) === state.tag)?.name || t('ui.tags.tag');
  else if (state.folder) text = folderLabel(state.folders.find((f) => f.key === state.folder) || {}) || t('ui.nav.folder');
  $('back-list-text').textContent = text;
}

// Places the levels for position pos (0..2, may be fractional while a level moves).
// Level i has d = i - pos: d >= 0 lies to the right, d < 0 is pushed a little to the left and darkened.
// Levels at least one step away are inert and hidden from assistive tech; the target level never is.
function applyNav(pos) {
  state.navPos = pos;
  // Safety: overflow clip should prevent it, but a horizontal scroll of the app would leave the layout off by that much.
  if (NARROW.matches) {
    const app = document.querySelector('.app');
    if (app.scrollLeft) app.scrollLeft = 0;
  }
  levelEls.forEach((node, i) => {
    if (!NARROW.matches) {
      node.style.transform = '';
      node.style.removeProperty('--dim');
      node.inert = false;
      node.removeAttribute('aria-hidden');
      return;
    }
    const d = i - pos;
    node.style.transform = d >= 0
      ? `translateX(${Math.min(d, 1) * 100}%)`
      : `translateX(${Math.max(d, -1) * 30}%)`;
    node.style.setProperty('--dim', String(d < 0 ? Math.min(-d, 1) * 0.12 : 0));
    const away = Math.abs(d) >= 1 && i !== state.navLevel;
    node.inert = away;
    if (away) node.setAttribute('aria-hidden', 'true');
    else node.removeAttribute('aria-hidden');
  });
}

function stopNavSpring() {
  if (navAnim) cancelAnimationFrame(navAnim.id);
  navAnim = null;
}

// Moves navPos to target with a critically damped spring, one step per frame, starting from the current position and velocity (levels/s).
function runNavSpring(target, velocity) {
  const anim = { s: { x: state.navPos, v: velocity }, target, id: 0 };
  let last = 0;
  navAnim = anim;
  const frame = (now) => {
    if (navAnim !== anim) return;
    let left = last ? Math.min((now - last) / 1000, 1 / 30) : 1 / 60;
    last = now;
    while (left > 0) {
      const h = Math.min(left, 1 / 240);
      anim.s = springStep(anim.s, target, h);
      left -= h;
    }
    if (Math.abs(anim.s.x - target) < 0.001 && Math.abs(anim.s.v) < 0.01) {
      navAnim = null;
      applyNav(target); // exact target
      return;
    }
    applyNav(anim.s.x);
    anim.id = requestAnimationFrame(frame);
  };
  anim.id = requestAnimationFrame(frame);
}

// Reduced motion: the level is placed at once and only its opacity eases in.
function fadeInLevel(level) {
  navFade?.cancel();
  navFade = levelEls[level].animate?.([{ opacity: 0 }, { opacity: 1 }], { duration: 200, easing: 'ease' }) ?? null;
}

// Moves to a level. velocity is in levels/s (from a swipe); 0 keeps the velocity of a spring that is still running.
function goTo(level, velocity = 0) {
  const v0 = navAnim && velocity === 0 ? navAnim.s.v : velocity;
  stopNavSpring();
  state.navLevel = level;
  // The target is not inert before focusLevel() runs (applyNav excludes state.navLevel); the position does not change.
  if (NARROW.matches) applyNav(state.navPos);
  if (!NARROW.matches) {
    state.navPos = level;
    applyNav(level);
    return;
  }
  if (REDUCED.matches) {
    state.navPos = level;
    applyNav(level);
    fadeInLevel(level);
    return;
  }
  if (v0 === 0 && state.navPos === level) {
    applyNav(level);
    return;
  }
  runNavSpring(level, v0);
}

// Focus after a step back goes to the back button of the new level, or to the selected folder or tag on level 0.
// The sidebar row that stands for the selected folder: its own row, or the topmost collapsed ancestor hiding it.
function visibleSelectionKey() {
  const find = (ns, chain) => {
    for (const n of ns) {
      const here = [...chain, n];
      if (n.folder?.key === state.folder) return here;
      const hit = find(n.children, here);
      if (hit) return hit;
    }
    return null;
  };
  const chain = find(buildFolderTree(state.folders), []);
  if (!chain) return null;
  return (chain.find((n) => state.collapsed.has(n.key)) ?? chain[chain.length - 1]).key;
}

function focusLevel(level) {
  let target = null;
  if (level === 1) {
    target = $('back-folders');
  } else if (state.tag) {
    target = document.querySelector('#tag-list .folder[aria-current="true"]');
  } else {
    const key = visibleSelectionKey();
    target = (key !== null && document.querySelector(`#folder-list [data-key="${CSS.escape(key)}"], #folder-list [data-toggle="${CSS.escape(key)}"]`))
      || document.querySelector('#folder-list .folder')
      || document.querySelector('#tag-list .folder[aria-current="true"]');
  }
  target?.focus({ preventScroll: true });
}

function goBack() {
  if (!NARROW.matches || state.navLevel === 0) return;
  const next = state.navLevel - 1;
  goTo(next);
  focusLevel(next);
}

$('back-folders').addEventListener('click', goBack);
$('back-list').addEventListener('click', goBack);

// Edge swipe: the strip at the left edge of the list (level 1) or of the reader (level 2) drags that level back.
// One swipe at a time. pointerdown stops a running spring, so a grab continues from the current navPos.
const SWIPE_SLOP = 10; // px of movement before a gesture counts as a swipe
const SWIPE_WINDOW = 100; // ms of history used for the release velocity
let swipe = null; // { id, strip, at, width, base, x0, y0, moving, ref, off0, L, offset, hist }

function releaseSwipe(g) {
  if (g.strip.hasPointerCapture(g.id)) g.strip.releasePointerCapture(g.id);
}

// Finger velocity in px/s over the last SWIPE_WINDOW ms before t; positive = to the right. A pause before release counts as rest.
function swipeVelocity(hist, t) {
  const recent = hist.filter((p) => p.t >= t - SWIPE_WINDOW);
  if (recent.length < 2) return 0;
  const a = recent[0];
  const b = recent[recent.length - 1];
  return b.t > a.t ? (b.x - a.x) / ((b.t - a.t) / 1000) : 0;
}

// Position in levels for the current gesture. Past the start point further left, the excess is rubberbanded.
// Without a grab during a spring (L = 0) this is navPos = level - offset/width, with rubberband for offset < 0.
function swipePos(g) {
  const travel = g.offset < g.L ? g.L + rubberband(g.offset - g.L, g.width) : g.offset;
  const pos = g.at - travel / g.width;
  // Past the starting level (dragging left) only a rubberband of at most 0.08 levels, so the next level never slides in.
  if (pos > g.at) return g.at + rubberband(pos - g.at, 0.08);
  return Math.max(g.at - 1, pos);
}

function cancelSwipe() {
  if (!swipe) return;
  releaseSwipe(swipe);
  swipe = null;
}

function swipeDown(e) {
  if (swipe || e.button !== 0 || !NARROW.matches) return;
  const strip = e.currentTarget;
  const at = levelEls.indexOf(strip.parentElement);
  if (state.navLevel !== at) return; // only the level in front can be dragged back
  stopNavSpring();
  strip.setPointerCapture(e.pointerId);
  swipe = {
    id: e.pointerId, strip, at, width: strip.parentElement.clientWidth, base: state.navPos,
    x0: e.clientX, y0: e.clientY, moving: false, ref: 0, off0: 0, L: 0, offset: 0,
    hist: [{ x: e.clientX, t: e.timeStamp }],
  };
}

function swipeMove(e) {
  const g = swipe;
  if (!g || e.pointerId !== g.id) return;
  if (!g.moving) {
    const dx = e.clientX - g.x0;
    const dy = e.clientY - g.y0;
    if (Math.hypot(dx, dy) < SWIPE_SLOP) return;
    if (Math.abs(dx) <= Math.abs(dy)) { // vertical: not ours, give the pointer back and settle
      releaseSwipe(g);
      swipe = null;
      goTo(g.at);
      return;
    }
    // Start from where the level is now, so the first pixels after the slop do not jump.
    g.moving = true;
    g.ref = e.clientX;
    g.off0 = (g.at - g.base) * g.width;
    g.L = Math.min(g.off0, 0);
  }
  g.offset = g.off0 + (e.clientX - g.ref);
  g.hist.push({ x: e.clientX, t: e.timeStamp });
  g.hist = g.hist.filter((p) => p.t >= e.timeStamp - SWIPE_WINDOW);
  applyNav(swipePos(g));
}

function swipeUp(e) {
  const g = swipe;
  if (!g || e.pointerId !== g.id) return;
  swipe = null;
  releaseSwipe(g);
  if (!g.moving) { goTo(g.at); return; }
  const v = swipeVelocity(g.hist, e.timeStamp);
  const back = shouldGoBack(g.offset, v, g.width);
  const to = back ? g.at - 1 : g.at;
  // The position falls when the finger moves right, so the spring gets -v/width in levels/s.
  if (REDUCED.matches) {
    stopNavSpring();
    state.navLevel = to;
    applyNav(to);
  } else {
    goTo(to, -v / g.width);
  }
  if (back) focusLevel(to);
}

for (const strip of document.querySelectorAll('.edge-swipe')) {
  strip.addEventListener('pointerdown', swipeDown);
  strip.addEventListener('pointermove', swipeMove);
  strip.addEventListener('pointerup', swipeUp);
  strip.addEventListener('pointercancel', swipeUp);
}

// A window that changes width stops the spring, drops any swipe and takes the new layout at once.
NARROW.addEventListener('change', () => {
  cancelSwipe();
  stopNavSpring();
  navFade?.cancel();
  navFade = null;
  applyNav(state.navLevel);
});
updateBackList();
applyNav(state.navLevel);

/* ---------- Events ---------- */
$('more').addEventListener('click', () => {
  if (!state.loading) loadPage(false);
});

// Sticky head: the shadow and blur appear only once the list has scrolled.
const listHead = document.querySelector('#list-pane .head');
$('list-pane').addEventListener('scroll', () => {
  listHead.classList.toggle('scrolled', $('list-pane').scrollTop > 0);
}, { passive: true });

// Typing state: while a field or an open dialog has focus, the selection turns grey and j/k is off.
function updateTyping() {
  const field = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '');
  document.body.classList.toggle('typing', field || document.querySelector('dialog[open]') !== null);
}
document.addEventListener('focusin', updateTyping);
document.addEventListener('focusout', updateTyping);
document.addEventListener('close', updateTyping, true); // dialog close: focus may return to the body
$('search').addEventListener('keydown', (e) => {
  if (e.key === 'Escape') e.target.blur(); // back to the blue selection
});

let searchTimer = 0;
$('search').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    const q = $('search').value.trim();
    if (q === state.q) return;
    clearSelection();
    state.q = q;
    reloadList();
  }, 300);
});

for (const b of document.querySelectorAll('#m-toggle button')) {
  b.addEventListener('click', () => {
    state.mode = b.dataset.mode;
    renderBody();
  });
}

$('m-star').addEventListener('click', toggleStar);
$('m-bar-star').addEventListener('click', toggleStar);
$('m-tag-input').addEventListener('keydown', (e) => {
  if (e.isComposing) return;
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    const n = suggestions().length;
    if (!n) return;
    e.preventDefault();
    suggestIndex = e.key === 'ArrowDown' ? (suggestIndex + 1) % n : (suggestIndex - 1 + n) % n;
    renderSuggest();
  } else if (e.key === 'Escape') {
    suggestIndex = -1;
    $('m-tag-suggest').hidden = true;
  } else if (e.key === 'Enter') {
    e.preventDefault();
    const pick = suggestIndex >= 0 ? suggestions()[suggestIndex] : null;
    if (pick) $('m-tag-input').value = pick.name || '';
    addTagFromInput();
  }
});
$('m-tag-input').addEventListener('input', () => { suggestIndex = -1; renderSuggest(); });
$('m-tag-input').addEventListener('focus', renderSuggest);
$('m-tag-input').addEventListener('blur', () => { suggestIndex = -1; renderSuggest(); });

document.addEventListener('keydown', (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (document.querySelector('dialog[open]')) return; // no list navigation behind a modal dialog
  const t = e.target;
  if (t instanceof HTMLElement && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
  if (e.key === 'Escape') {
    if (NARROW.matches && state.navLevel > 0) { e.preventDefault(); goBack(); }
    return;
  }
  if (e.key === 'j' || e.key === 'ArrowDown') { e.preventDefault(); move(1); }
  else if (e.key === 'k' || e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
  else if (e.key === 's') { e.preventDefault(); toggleStar(); }
});

/* ---------- Start ---------- */
// Failed reads keep what is loaded, so a failed refresh never empties rail, folders or dialog.
const loadWarnings = new Map(); // key -> message of a failed read, shown until dismissed
function renderLoadWarn() {
  const msgs = [...loadWarnings.values()];
  $('load-warn-text').textContent = msgs.join(' ');
  $('load-warn').hidden = msgs.length === 0;
}
$('load-warn-close').addEventListener('click', () => {
  loadWarnings.clear();
  renderLoadWarn();
});

async function loadConfig() {
  let config;
  try {
    config = await api('/api/config');
  } catch (_) {
    loadWarnings.set('config', t('ui.error.config_load'));
    renderLoadWarn();
    return;
  }
  state.config = config;
  state.settings = config.settings || null;
  loadWarnings.delete('config');
  renderLoadWarn();
}

async function loadSources() {
  let sources;
  try {
    sources = await api('/api/sources');
  } catch (_) {
    loadWarnings.set('sources', t('ui.error.sources_load'));
    renderLoadWarn();
    return;
  }
  state.sources = sources;
  loadWarnings.delete('sources');
  renderLoadWarn();
  renderAutoPaused();
}

// A language switch redraws everything that shows texts. Open dialogs stay open.
function rerender() {
  renderRail();
  renderFolders();
  updateSearchPlaceholder();
  updateFolderAdd();
  updateFetchArea();
  renderTags();
  renderList();
  updateSelectUI();
  updateMore();
  if (state.mail && !state.mailStale && !$('mail').hidden) renderMail(state.mailSource, state.mail);
  else if (!$('placeholder').hidden && emptyKind === 'none') showEmpty('none', t('ui.mail.none_selected'));
  if (lastJob) {
    renderJob(lastJob);
    if ($('fetch-dialog').open) updateFetchDialog(lastJob);
  }
  renderAutoPaused();
  if ($('settings-dialog').open) renderSettingsAll();
}
I18N.onChange(rerender);

loadConfig().then(async () => {
  if (state.config && !state.config.configured) {
    openPicker('library', true);
    return;
  }
  await loadSources();
  state.view = startView();
  renderRail();
  updateFetchArea();
  loadFolders();
  pollJob();
});
})();
