"""Tests for the pure helpers in static/js/pure.js.

The helpers live in a block between two marker comments. The block must not touch
the DOM, window or state, so it can be run under Node.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

PURE_JS = Path(__file__).resolve().parent.parent / 'static' / 'js' / 'pure.js'
START = '/* ---------- Pure helpers (tested by test_ui.py) ---------- */'
END = '/* ---------- End pure helpers ---------- */'


def run_js(expr: str):
    src = PURE_JS.read_text(encoding='utf-8')
    block = src[src.index(START):src.index(END) + len(END)]
    code = f"{block}\nprocess.stdout.write(JSON.stringify({expr}))"
    out = subprocess.run(['node', '-e', code], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which('node'), 'node fehlt')
class FolderIconTest(unittest.TestCase):
    def test_known_folders(self):
        cases = {"Inbox": "tray", "Posteingang": "tray", "Sent": "paperplane",
                 "Gesendete Elemente": "paperplane", "Drafts": "doc", "Entwürfe": "doc",
                 "Trash": "trash", "Gelöschte Elemente": "trash", "Junk-E-Mail": "junk",
                 "Spam": "junk", "Archiv": "archivebox", "Rechnungen": "folder", "": "folder"}
        for name, want in cases.items():
            self.assertEqual(run_js(f"folderIconName({json.dumps(name)})"), want, name)

    def test_inbox_only_exact(self):
        self.assertEqual(run_js('folderIconName("Inbox/Alt")'), "folder")


@unittest.skipUnless(shutil.which('node'), 'node fehlt')
class PersonInitialsTest(unittest.TestCase):
    def test_initials(self):
        cases = [({"name": "Anna Müller"}, "AM"), ({"name": "anna"}, "A"),
                 ({"name": '"Dr. Hans Peter Schmidt"'}, "DS"),
                 ({"address": "max.mustermann@example.org"}, "MM"),
                 ({"name": "", "address": "info@example.org"}, "I"),
                 ({"name": "1&1 Service"}, "S"), (None, "?"), ({}, "?")]
        for addr, want in cases:
            self.assertEqual(run_js(f"personInitials({json.dumps(addr)})"), want, addr)


@unittest.skipUnless(shutil.which('node'), 'node fehlt')
class GestureMathTest(unittest.TestCase):
    def test_project(self):
        self.assertAlmostEqual(run_js("project(1000)"), 499.0, places=1)
        self.assertEqual(run_js("project(0)"), 0)

    def test_rubberband(self):
        self.assertAlmostEqual(run_js("rubberband(100, 400)"), 48.35, places=2)
        self.assertAlmostEqual(run_js("rubberband(-100, 400)"), -48.35, places=2)
        self.assertEqual(run_js("rubberband(0, 400)"), 0)

    def test_should_go_back(self):
        self.assertTrue(run_js("shouldGoBack(10, 1000, 400)"))    # kurzer, schneller Wisch
        self.assertFalse(run_js("shouldGoBack(300, -800, 400)"))  # weit gezogen, aber zurückgeworfen
        self.assertTrue(run_js("shouldGoBack(250, 0, 400)"))
        self.assertFalse(run_js("shouldGoBack(150, 0, 400)"))

    def test_spring_settles_without_overshoot(self):
        xs = run_js("(() => { let s = {x: 0, v: 0}; const xs = [];"
                    " for (let i = 0; i < 240; i++) { s = springStep(s, 1, 1/240); xs.push(s.x); }"
                    " return xs; })()")
        self.assertLessEqual(max(xs), 1.0 + 1e-6)   # kritisch gedämpft: kein Überschwingen
        self.assertGreater(xs[-1], 0.995)           # nach 1 s angekommen
        self.assertTrue(all(b >= a - 1e-9 for a, b in zip(xs, xs[1:])))

    def test_spring_keeps_initial_velocity(self):
        s = run_js("springStep({x: 0, v: 5}, 0, 1/240)")
        self.assertGreater(s["x"], 0)  # Startgeschwindigkeit wird übernommen


# Folder fixtures: F(key, name, path, order, extra). Helpers return plain data so Node's output is comparable.
FOLDER_PRELUDE = """
const F = (key, name, path, order = null, extra = {}) =>
  ({ key, name, path, color: null, count: 0, unread: 0, order, labels: {}, ...extra });
const shape = (ns) => ns.map((n) => [n.name, n.depth, n.synthetic === true, shape(n.children)]);
"""


def folder_js(body: str) -> str:
    return f"(() => {{ {FOLDER_PRELUDE}\n{body}\n}})()"


@unittest.skipUnless(shutil.which('node'), 'node fehlt')
class FolderTreeTest(unittest.TestCase):
    def test_build_tree_nests_by_path_and_sorts_by_order(self):
        got = run_js(folder_js("""
            return shape(buildFolderTree([
              F('sys:inbox', 'Posteingang', 'Posteingang', 0),
              F('a', 'A', 'A', 2), F('b', 'B', 'B', 1),
              F('ax', 'x', 'A/x', 5), F('ay', 'y', 'A/y', 1),
              F('aw', 'w', 'A/w'), F('az', 'z', 'A/z'),
              F('axq', 'q', 'A/x/q'),
            ]));"""))
        want = [["Posteingang", 0, False, []],
                ["B", 0, False, []],
                ["A", 0, False, [["y", 1, False, []],
                                 ["x", 1, False, [["q", 2, False, []]]],
                                 ["w", 1, False, []],
                                 ["z", 1, False, []]]]]
        self.assertEqual(got, want)

    def test_build_tree_synthesizes_missing_parent(self):
        got = run_js(folder_js("""
            return shape(buildFolderTree([
              F('i', 'Italien', 'Reisen/2024/Italien'),
              F('f', 'Frankreich', 'Reisen/2024/Frankreich'),
            ]));"""))
        want = [["Reisen", 0, True, [["2024", 1, True, [["Frankreich", 2, False, []],
                                                       ["Italien", 2, False, []]]]]]]
        self.assertEqual(got, want)
        # Ein vorhandener Ordner mit gleichem Pfad (ohne Beachtung der Schreibung) ist der Elternknoten.
        got = run_js(folder_js("""
            return shape(buildFolderTree([F('r', 'Reisen', 'reisen'), F('x', 'x', 'Reisen/x')]));"""))
        self.assertEqual(got, [["Reisen", 0, False, [["x", 1, False, []]]]])

    def test_build_tree_null_order_alphabetical_after_ordered(self):
        got = run_js(folder_js("""
            return buildFolderTree([
              F('sys:papier', 'Papierkorb', 'Papierkorb'),
              F('v', 'Online noch zu löschen', '', null, { virtual: true }),
              F('z', 'Zeta', 'Zeta'), F('b', 'beta', 'beta'),
              F('m', 'Mitte', 'Mitte', 3), F('al', 'Alpha', 'Alpha'),
            ]).map((n) => n.name);"""))
        self.assertEqual(got, ["Papierkorb", "Online noch zu löschen", "Mitte", "Alpha", "beta", "Zeta"])

    def test_visible_nodes_hides_collapsed_descendants(self):
        got = run_js(folder_js("""
            const t = buildFolderTree([
              F('ka', 'A', 'A'), F('kx', 'x', 'A/x'), F('kq', 'q', 'A/x/q'),
              F('ky', 'y', 'A/y'), F('kb', 'B', 'B'),
            ]);
            const names = (ns) => ns.map((n) => n.name);
            return {
              none: names(visibleNodes(t, new Set())),
              x: names(visibleNodes(t, new Set(['kx']))),
              a: names(visibleNodes(t, new Set(['ka']))),
            };"""))
        self.assertEqual(got["none"], ["A", "x", "q", "y", "B"])
        self.assertEqual(got["x"], ["A", "x", "y", "B"])
        self.assertEqual(got["a"], ["A", "B"])

    def test_drop_target_before_after_into(self):
        got = run_js(folder_js("""
            const t = buildFolderTree([
              F('k1', 'R1', 'R1', 1), F('k2', 'R2', 'R2', 2),
              F('k3', 'C1', 'R2/C1', 1), F('k4', 'C2', 'R2/C2', 2),
            ]);
            return {
              after_root: dropTarget(t, 'k1', 'k2', 'after'),
              before_root: dropTarget(t, 'k1', 'k2', 'before'),
              into: dropTarget(t, 'k1', 'k2', 'into'),
              before_child: dropTarget(t, 'k3', 'k4', 'before'),
              after_child: dropTarget(t, 'k3', 'k4', 'after'),
              into_child: dropTarget(t, 'k3', 'k2', 'into'),
              root_from_child: dropTarget(t, 'k3', 'k1', 'before'),
            };"""))
        self.assertEqual(got["after_root"], {"parentKey": None, "index": 1})
        self.assertEqual(got["before_root"], {"parentKey": None, "index": 0})
        self.assertEqual(got["into"], {"parentKey": "k2", "index": 2})
        self.assertEqual(got["before_child"], {"parentKey": "k2", "index": 0})
        self.assertEqual(got["after_child"], {"parentKey": "k2", "index": 1})
        self.assertEqual(got["into_child"], {"parentKey": "k2", "index": 1})  # schon Kind: wird nicht mitgezählt
        self.assertEqual(got["root_from_child"], {"parentKey": None, "index": 0})

    def test_drop_into_own_subtree_is_null(self):
        got = run_js(folder_js("""
            const t = buildFolderTree([
              F('ka', 'A', 'A'), F('kx', 'x', 'A/x'), F('kq', 'q', 'A/x/q'), F('kb', 'B', 'B'),
              F('kd', '2024', 'Reisen/2024'),
            ]);
            return {
              into_descendant: dropTarget(t, 'ka', 'kq', 'into'),
              before_descendant: dropTarget(t, 'ka', 'kq', 'before'),
              onto_self: dropTarget(t, 'ka', 'ka', 'after'),
              into_child_ok: dropTarget(t, 'kb', 'kx', 'into'),
              beside_synthetic_parent: dropTarget(t, 'kb', 'kd', 'before'),
            };"""))
        self.assertIsNone(got["into_descendant"])
        self.assertIsNone(got["before_descendant"])
        self.assertIsNone(got["onto_self"])
        self.assertEqual(got["into_child_ok"], {"parentKey": "kx", "index": 1})
        self.assertIsNone(got["beside_synthetic_parent"])  # Elternteil ist ein Platzhalter ohne Ordner

    def test_child_of_system_folder(self):
        got = run_js(folder_js("""
            return shape(buildFolderTree([
              F('sys:inbox', 'Posteingang', 'Posteingang', 0),
              F('x', 'X', 'Posteingang/X'),
            ]));"""))
        self.assertEqual(got, [["Posteingang", 0, False, [["X", 1, False, []]]]])

    def test_order_zero_sorts_before_one_and_null_last(self):
        got = run_js(folder_js("""
            return buildFolderTree([
              F('a', 'A', 'A', 1), F('n', 'N', 'N'), F('b', 'B', 'B', 0),
            ]).map((n) => n.name);"""))
        self.assertEqual(got, ["B", "A", "N"])  # 0 ist eine Ordnung, kein fehlender Wert

    def test_empty_list(self):
        self.assertEqual(run_js("buildFolderTree([])"), [])

    def test_paths_differing_only_in_case_attach_to_first_parent(self):
        # Pfade werden ohne Schreibung verglichen: "a/x" hängt am Ordner mit Pfad "A".
        got = run_js(folder_js("""
            return shape(buildFolderTree([
              F('a', 'A', 'A'), F('ax', 'x', 'a/x'),
            ]));"""))
        self.assertEqual(got, [["A", 0, False, [["x", 1, False, []]]]])
        # Gibt es zwei Ordner mit demselben Pfad, bekommt der zuerst gelieferte die Kinder;
        # der zweite bleibt eine eigene Wurzel.
        got = run_js(folder_js("""
            return shape(buildFolderTree([
              F('a1', 'A', 'A'), F('a2', 'Zweite', 'a'), F('ax', 'x', 'a/x'),
            ]));"""))
        self.assertEqual(got, [["A", 0, False, [["x", 1, False, []]]], ["Zweite", 0, False, []]])


if __name__ == '__main__':
    unittest.main()


@unittest.skipUnless(shutil.which('node'), 'node fehlt')
class FolderManageTest(unittest.TestCase):
    SOURCES = [{"id": "a1", "archive": True}, {"id": "o1", "archive": False}, {"id": "a2", "archive": True}]

    def test_archive_folder_is_manageable_in_its_archive(self):
        folder = {"key": "path:reisen", "labels": {"a1": "17"}}
        self.assertEqual(run_js(f"manageableArchive({json.dumps(folder)}, {json.dumps(self.SOURCES)})"), "a1")

    def test_other_folders_are_not_manageable(self):
        cases = [
            {"key": "sys:2", "labels": {"a1": "2"}},                       # system folder
            {"key": "archived", "virtual": True, "labels": {}},            # virtual entry
            {"key": "path:x", "labels": {"o1": "5"}},                      # not an archive
            {"key": "path:x", "labels": {"a1": "5", "o1": "6"}},           # held by two sources
            {"key": "path:x", "labels": {"a1": "5", "a2": "7"}},           # held by two archives
            {"key": "path:x", "labels": {}},
            {"key": "path:x", "labels": {"gone": "5"}},                    # unknown source
        ]
        for folder in cases:
            self.assertIsNone(run_js(f"manageableArchive({json.dumps(folder)}, {json.dumps(self.SOURCES)})"), folder)


@unittest.skipUnless(shutil.which('node'), 'node fehlt')
class MoveIndexTest(unittest.TestCase):
    TOP = [
        {"key": "sys:1", "path": "Posteingang", "name": "Posteingang", "order": 0, "labels": {}},
        {"key": "sys:2", "path": "Gesendet", "name": "Gesendet", "order": 1, "labels": {}},
        {"key": "path:a", "path": "A", "name": "A", "order": 2, "labels": {}},
        {"key": "path:b", "path": "B", "name": "B", "order": 3, "labels": {}},
        {"key": "path:c", "path": "C", "name": "C", "order": 4, "labels": {}},
    ]
    NESTED = [
        {"key": "path:p", "path": "P", "name": "P", "order": 0, "labels": {}},
        {"key": "path:p/x", "path": "P/x", "name": "X", "order": 0, "labels": {}},
        {"key": "path:p/y", "path": "P/y", "name": "Y", "order": 1, "labels": {}},
        {"key": "path:p/z", "path": "P/z", "name": "Z", "order": 2, "labels": {}},
    ]

    def server_index(self, folders, dragged, over, zone, level_path=None):
        """moveIndex of dropTarget's index, for a drop of dragged at zone of over."""
        tree = f"buildFolderTree({json.dumps(folders)})"
        level = tree if level_path is None else f"findNode({tree}, {json.dumps(level_path)}).children"
        expr = (f"(() => {{ const t = {tree}; const p = dropTarget(t, {json.dumps(dragged)}, {json.dumps(over)}, "
                f"{json.dumps(zone)}); return moveIndex({level}, {json.dumps(dragged)}, p.index); }})()")
        return run_js(expr)

    def test_drop_after_user_folder_counts_only_user_folders(self):
        self.assertEqual(self.server_index(self.TOP, "path:c", "path:b", "after"), 2)

    def test_drop_before_first_user_folder_is_zero(self):
        self.assertEqual(self.server_index(self.TOP, "path:c", "path:a", "before"), 0)

    def test_drop_after_system_folder_is_zero(self):
        self.assertEqual(self.server_index(self.TOP, "path:b", "sys:2", "after"), 0)

    def test_dragged_folder_within_its_level_is_not_counted(self):
        self.assertEqual(self.server_index(self.TOP, "path:a", "path:c", "after"), 2)

    def test_nested_level_without_system_folders(self):
        self.assertEqual(self.server_index(self.NESTED, "path:p/z", "path:p/y", "before", "path:p"), 1)
        self.assertEqual(self.server_index(self.NESTED, "path:p/x", "path:p", "into", "path:p"), 2)


@unittest.skipUnless(shutil.which('node'), 'node fehlt')
class CanPreviewTest(unittest.TestCase):
    def test_types(self):
        for mime in ("application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp", "IMAGE/PNG",
                     "audio/mpeg", "audio/mp3"):
            self.assertTrue(run_js(f"canPreview({json.dumps(mime)})"), mime)
        for mime in ("image/svg+xml", "text/html", "application/octet-stream", "", None):
            self.assertFalse(run_js(f"canPreview({json.dumps(mime)})"), mime)

    def test_kind(self):
        self.assertEqual(run_js('previewKind("audio/mpeg")'), "audio")
        self.assertEqual(run_js('previewKind("audio/mp3")'), "audio")
        self.assertEqual(run_js('previewKind("application/pdf")'), "frame")
        self.assertEqual(run_js('previewKind("image/png")'), "frame")


@unittest.skipUnless(shutil.which('node'), 'node fehlt')
class SelectionTest(unittest.TestCase):
    def test_toggle_selection(self):
        self.assertEqual(run_js('toggleSelection(["s1/a"], "s1/b")'), ["s1/a", "s1/b"])
        self.assertEqual(run_js('toggleSelection(["s1/a", "s1/b"], "s1/a")'), ["s1/b"])
        self.assertEqual(run_js('toggleSelection([], "s2/x")'), ["s2/x"])

    def test_range_selection_forward_and_backward(self):
        keys = '["s1/1", "s1/2", "s1/3", "s1/4", "s1/5"]'
        want = ["s1/2", "s1/3", "s1/4"]
        self.assertEqual(run_js(f'rangeSelection({keys}, "s1/2", "s1/4")'), want)
        self.assertEqual(run_js(f'rangeSelection({keys}, "s1/4", "s1/2")'), want)
        self.assertEqual(run_js(f'rangeSelection({keys}, "s1/3", "s1/3")'), ["s1/3"])

    def test_transfer_payload_with_folder_and_with_new_folder(self):
        selected = ["s1/a", "s2/b/c"]
        self.assertEqual(run_js(f"transferPayload({json.dumps(selected)}, 's3', 'L1', null)"), {
            "items": [{"source": "s1", "id": "a"}, {"source": "s2", "id": "b/c"}],
            "archive": "s3", "folder": "L1", "new_folder": None})
        self.assertEqual(run_js(f"transferPayload({json.dumps(selected)}, 's3', null, "
                                f"{{name: 'Neu', parent: 'L2'}})"), {
            "items": [{"source": "s1", "id": "a"}, {"source": "s2", "id": "b/c"}],
            "archive": "s3", "folder": None, "new_folder": {"name": "Neu", "parent": "L2"}})

    def test_transfer_payload_keeps_slashes_in_mail_id(self):
        # Only the first "/" separates the source; a mail ID may contain more.
        self.assertEqual(run_js("transferPayload(['s1/a/b/c'], 's3', null, null).items"),
                         [{"source": "s1", "id": "a/b/c"}])

    def test_transfer_payload_passes_folder_and_new_folder_through(self):
        # The helper does not judge the pair; the server rejects a folder together with a new folder.
        self.assertEqual(run_js("transferPayload(['s1/a'], 's3', 'L1', {name: 'N', parent: null})"), {
            "items": [{"source": "s1", "id": "a"}], "archive": "s3",
            "folder": "L1", "new_folder": {"name": "N", "parent": None}})


INDEX_HTML = Path(__file__).resolve().parent.parent / 'static' / 'index.html'


def run_electron_detect(user_agent: str, platform: str):
    # The detection script sits inline in index.html, before the stylesheet, so it runs before the first render.
    html = INDEX_HTML.read_text(encoding='utf-8')
    body = html[html.index('<script id="electron-detect">') + len('<script id="electron-detect">'):]
    body = body[:body.index('</script>')]
    code = (f"const out = [];\nconst navigator = {json.dumps({'userAgent': user_agent, 'platform': platform})};\n"
            f"const document = {{ documentElement: {{ classList: {{ add: (...c) => out.push(...c) }} }} }};\n"
            f"{body}\nprocess.stdout.write(JSON.stringify(out));")
    out = subprocess.run(['node', '-e', code], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which('node'), 'node fehlt')
class ElectronDetectTest(unittest.TestCase):
    ELECTRON_MAC = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Electron/38.0.0 Safari/537.36'
    ELECTRON_WIN = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Electron/38.0.0 Safari/537.36'
    ELECTRON_LINUX = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Electron/38.0.0 Safari/537.36'
    BROWSER = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36'

    def test_no_classes_outside_electron(self):
        self.assertEqual(run_electron_detect(self.BROWSER, 'Linux x86_64'), [])
        self.assertEqual(run_electron_detect('Mozilla/5.0 (Macintosh)', 'MacIntel'), [])

    def test_platform_classes(self):
        self.assertEqual(run_electron_detect(self.ELECTRON_MAC, 'MacIntel'), ['electron', 'platform-mac'])
        self.assertEqual(run_electron_detect(self.ELECTRON_WIN, 'Win32'), ['electron', 'platform-win'])
        self.assertEqual(run_electron_detect(self.ELECTRON_LINUX, 'Linux x86_64'), ['electron', 'platform-linux'])

    def test_falls_back_to_user_agent_without_platform(self):
        self.assertEqual(run_electron_detect(self.ELECTRON_MAC, ''), ['electron', 'platform-mac'])
