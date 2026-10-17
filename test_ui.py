"""Tests for the pure helpers in index.html.

The helpers live in a block between two marker comments. The block must not touch
the DOM, window or state, so it can be run under Node.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
START = '/* ---------- Pure helpers (tested by test_ui.py) ---------- */'
END = '/* ---------- End pure helpers ---------- */'


def run_js(expr: str):
    src = (HERE / 'index.html').read_text(encoding='utf-8')
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


if __name__ == '__main__':
    unittest.main()
