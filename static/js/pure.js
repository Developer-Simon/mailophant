'use strict';

/* ---------- Pure helpers (tested by test_ui.py) ---------- */
// Picks the sidebar icon for a folder name. Rules are checked in order.
function folderIconName(name) {
  const n = String(name).toLocaleLowerCase('de-DE');
  if (/^(inbox|posteingang)$/.test(n)) return 'tray';
  if (/sent|gesendet/.test(n)) return 'paperplane';
  if (/draft|entw[uü]rf/.test(n)) return 'doc';
  if (/trash|deleted|papierkorb|gelöscht/.test(n)) return 'trash';
  if (/spam|junk/.test(n)) return 'junk';
  if (/archiv/.test(n)) return 'archivebox';
  return 'folder';
}

// Initials for the sender circle: "Anna Müller" -> "AM", "max.mustermann@x.org" -> "MM", nothing usable -> "?".
// Uses the name if there is one, else the part of the address before "@". Non-letters are dropped from each word.
function personInitials(addr) {
  const name = String(addr?.name ?? '').replace(/"/g, '').trim();
  const words = name
    ? name.split(/\s+/)
    : String(addr?.address ?? '').split('@')[0].split(/[._-]+/);
  const letters = words.map((w) => w.replace(/\P{L}/gu, '')).filter(Boolean);
  if (letters.length === 0) return '?';
  const first = [...letters[0]][0];
  const last = [...letters[letters.length - 1]][0];
  return (letters.length === 1 ? first : first + last).toLocaleUpperCase('de-DE');
}

// Gesture math (no DOM, no state). Pixels; velocities in px/s unless noted.
// Projected travel after a flick, Apple's formula: where a decelerating scroll would come to rest.
function project(velocity, decelerationRate = 0.998) {
  return (velocity / 1000) * decelerationRate / (1 - decelerationRate);
}

// Resistance past an edge: the further the overshoot, the less it moves, never beyond dimension.
function rubberband(overshoot, dimension, constant = 0.55) {
  return (overshoot * dimension * constant) / (dimension + constant * Math.abs(overshoot));
}

// Swipe from the left edge: go back if the projected position is past half the width (offset > 0 = to the right).
function shouldGoBack(offset, velocity, width) {
  return offset + project(velocity) > width / 2;
}

// One semi-implicit Euler step of a unit-mass spring towards target, critically damped by default.
// Callers split dt into steps of at most 1/240 s.
function springStep(s, target, dt, response = 0.35, damping = 1) {
  const k = (2 * Math.PI / response) ** 2;
  const c = 4 * Math.PI * damping / response;
  const v = s.v + (-k * (s.x - target) - c * s.v) * dt;
  return { x: s.x + v * dt, v };
}

// Icon path data (24x24 grid, stroke icons). Source: Lucide (ISC licence), https://lucide.dev
// "star-fill" is "star" drawn filled, see icon().
const STAR_PATH = 'M11.525 2.295a.53.53 0 0 1 .95 0l2.31 4.679a2.123 2.123 0 0 0 1.595 1.16l5.166.756a.53.53 0 0 1 .294.904l-3.736 3.638a2.123 2.123 0 0 0-.611 1.878l.882 5.14a.53.53 0 0 1-.771.56l-4.618-2.428a2.122 2.122 0 0 0-1.973 0L6.396 21.01a.53.53 0 0 1-.77-.56l.881-5.139a2.122 2.122 0 0 0-.611-1.879L2.16 9.795a.53.53 0 0 1 .294-.906l5.165-.755a2.122 2.122 0 0 0 1.597-1.16z';
const ICONS = {
  plus: ['M5 12h14', 'M12 5v14'],
  gear: ['M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z', 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z'],
  star: [STAR_PATH],
  'star-fill': [STAR_PATH],
  'chevron-up': ['m18 15-6-6-6 6'],
  'chevron-left': ['m15 18-6-6 6-6'],
  'chevron-right': ['m9 18 6-6-6-6'],
  'chevron-down': ['m6 9 6 6 6-6'],
  house: ['M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8', 'M3 10a2 2 0 0 1 .709-1.528l7-5.999a2 2 0 0 1 2.582 0l7 5.999A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z'],
  paperclip: ['M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48'],
  tray: ['M22 12h-6l-2 3h-4l-2-3H2', 'M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z'],
  paperplane: ['M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z', 'm21.854 2.147-10.94 10.939'],
  doc: ['M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7z', 'M14 2v4a2 2 0 0 0 2 2h4', 'M10 9H8', 'M16 13H8', 'M16 17H8'],
  trash: ['M3 6h18', 'M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6', 'M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2'],
  junk: ['M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z', 'M12 8v4', 'M12 16h.01'],
  archivebox: ['M3 3h18a1 1 0 0 1 1 1v3a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z', 'M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8', 'M10 12h4'],
  folder: ['M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z'],
  envelope: ['M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z', 'm22 6-10 7L2 6'],
  search: ['M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16z', 'm21 21-4.34-4.34'],
  xmark: ['M18 6 6 18', 'm6 6 12 12'],
  'arrow-down': ['M12 5v14', 'm19 12-7 7-7-7'],
  warn: ['m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3', 'M12 9v4', 'M12 17h.01'],
  info: ['M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20z', 'M12 16v-4', 'M12 8h.01'],
  check: ['M20 6 9 17l-5-5'],
  tag: ['M12.586 2.586A2 2 0 0 0 11.172 2H4a2 2 0 0 0-2 2v7.172a2 2 0 0 0 .586 1.414l8.704 8.704a2.426 2.426 0 0 0 3.42 0l6.58-6.58a2.426 2.426 0 0 0 0-3.42z', 'M7.5 7.5h.01'],
  more: ['M5 12h.01', 'M12 12h.01', 'M19 12h.01'],
};
// Mirrors PREVIEW_TYPES in server.py: types the browser shows in a tab without running the file's own code.
const PREVIEW_TYPES = ['application/pdf', 'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'audio/mpeg', 'audio/mp3'];
function canPreview(mime) {
  return PREVIEW_TYPES.includes(String(mime || '').toLowerCase());
}
// "audio" gets a low modal around the browser's player, everything else the full-height frame.
function previewKind(mime) {
  return String(mime || '').toLowerCase().startsWith('audio/') ? 'audio' : 'frame';
}

// Folder tree (no DOM, no state). Node = {key, folder, name, depth, children, synthetic}.
// key is the folder key; a synthetic node (a parent path without a folder of its own) gets "synthetic:<path>".
// Roots: system folders ("sys:" keys) in the delivered order, then the virtual entry, then the rest.
// Siblings: order ascending, null orders after the ordered ones, then name, case-insensitively.
// Key for matching paths and sorting names: case-insensitive, German locale (upper then lower, so ß matches ss).
const foldCase = (s) => String(s ?? '').toLocaleUpperCase('de-DE').toLocaleLowerCase('de-DE');
const parentPathOf = (path) => {
  const i = String(path ?? '').lastIndexOf('/');
  return i < 0 ? '' : path.slice(0, i);
};
const compareSiblings = (a, b) => {
  const ao = a.folder?.order ?? null;
  const bo = b.folder?.order ?? null;
  if ((ao === null) !== (bo === null)) return ao === null ? 1 : -1;
  if (ao !== null && ao !== bo) return ao - bo;
  return foldCase(a.name).localeCompare(foldCase(b.name), 'de');
};
function buildFolderTree(folders) {
  const byPath = new Map();
  const real = folders.map((f) => ({ key: f.key, folder: f, name: f.name || '', depth: 0, children: [], synthetic: false }));
  const nodes = [...real];
  for (const n of real) {
    const p = foldCase(n.folder.path);
    if (p && !byPath.has(p)) byPath.set(p, n);
  }
  const roots = [];
  const attach = (node, parentPath) => {
    if (parentPath) nodeAt(parentPath).children.push(node);
    else roots.push(node);
  };
  const nodeAt = (path) => {
    const k = foldCase(path);
    if (byPath.has(k)) return byPath.get(k);
    const node = { key: `synthetic:${k}`, folder: null, name: path.slice(path.lastIndexOf('/') + 1), depth: 0, children: [], synthetic: true };
    byPath.set(k, node);
    nodes.push(node);
    attach(node, parentPathOf(path));
    return node;
  };
  for (const n of real) attach(n, parentPathOf(n.folder.path));
  for (const n of nodes) n.children.sort(compareSiblings);
  const isSys = (n) => String(n.folder?.key ?? '').startsWith('sys:');
  const isVirtual = (n) => n.folder?.virtual === true;
  const top = [
    ...roots.filter(isSys),
    ...roots.filter(isVirtual),
    ...roots.filter((n) => !isSys(n) && !isVirtual(n)).sort(compareSiblings),
  ];
  const setDepth = (ns, depth) => ns.forEach((n) => {
    n.depth = depth;
    setDepth(n.children, depth + 1);
  });
  setDepth(top, 0);
  return top;
}

// Flat list in display order; children of a collapsed node (by key) are left out.
function visibleNodes(tree, collapsed) {
  const out = [];
  const walk = (ns) => {
    for (const n of ns) {
      out.push(n);
      if (!collapsed.has(n.key)) walk(n.children);
    }
  };
  walk(tree);
  return out;
}

// Where a dragged node lands relative to overKey: zone "before" or "after" puts it next to that node,
// "into" appends it to that node's children. siblings is the tree from buildFolderTree.
// parentKey null means the top level. Null when the target is in the dragged node's own subtree,
// or when the parent would be a synthetic node (it has no folder to hold the dragged one).
function dropTarget(siblings, draggedKey, overKey, zone) {
  const find = (list, parent, key) => {
    for (const n of list) {
      if (n.key === key) return { node: n, parent, list };
      const hit = find(n.children, n, key);
      if (hit) return hit;
    }
    return null;
  };
  const over = find(siblings, null, overKey);
  const dragged = find(siblings, null, draggedKey);
  if (!over || !dragged) return null;
  const inSubtree = (n, key) => n.key === key || n.children.some((c) => inSubtree(c, key));
  if (inSubtree(dragged.node, overKey)) return null;
  if (zone === 'into') {
    if (over.node.synthetic) return null;
    const kids = over.node.children.filter((c) => c.key !== draggedKey);
    return { parentKey: over.node.key, index: kids.length };
  }
  if (over.parent?.synthetic) return null;
  const rest = over.list.filter((c) => c.key !== draggedKey);
  const i = rest.findIndex((c) => c.key === overKey);
  return { parentKey: over.parent ? over.parent.key : null, index: zone === 'after' ? i + 1 : i };
}
// The archive a sidebar folder can be changed in: its one source, if that source is an archive and no other
// source holds the folder. System and virtual folders are never changed. Null if not manageable.
function manageableArchive(folder, sources) {
  if (!folder || folder.virtual === true || String(folder.key ?? '').startsWith('sys:')) return null;
  const ids = Object.keys(folder.labels || {});
  if (ids.length !== 1) return null;
  const src = sources.find((s) => s.id === ids[0]);
  return src && src.archive === true ? ids[0] : null;
}
// Selection of list rows. Keys are "<source>/<id>"; a mail ID may contain "/", a source ID does not.
function toggleSelection(selected, key) {
  return selected.includes(key) ? selected.filter((k) => k !== key) : [...selected, key];
}

// Inclusive range between anchor and to, in list order, whichever comes first. Unknown keys select just "to".
function rangeSelection(orderedKeys, anchor, to) {
  const a = orderedKeys.indexOf(anchor);
  const b = orderedKeys.indexOf(to);
  if (a < 0 || b < 0) return [to];
  return orderedKeys.slice(Math.min(a, b), Math.max(a, b) + 1);
}

// Body of POST /api/transfer. folderId and newFolder are mutually exclusive; the server checks that.
function transferPayload(selected, archiveId, folderId, newFolder) {
  const items = selected.map((key) => {
    const i = key.indexOf('/');
    return { source: key.slice(0, i), id: key.slice(i + 1) };
  });
  return {
    items,
    archive: archiveId,
    folder: folderId ?? null,
    new_folder: newFolder ?? null,
  };
}
// The node with this key anywhere in the tree, or null.
function findNode(nodes, key) {
  for (const n of nodes) {
    if (n.key === key) return n;
    const hit = findNode(n.children, key);
    if (hit) return hit;
  }
  return null;
}

// Server index for a move. dropTarget counts the sibling nodes shown (system folders included, the dragged one
// left out). The server inserts among the user folders of the level only, so count the user folders in front of
// the insertion point. siblings is the level the folder is dropped into (the children of its parent, or the top).
function moveIndex(siblings, draggedKey, index) {
  const isUser = (n) => !n.synthetic && n.folder?.virtual !== true && !String(n.key).startsWith('sys:');
  return siblings.filter((n) => n.key !== draggedKey).slice(0, index).filter(isUser).length;
}
/* ---------- End pure helpers ---------- */
