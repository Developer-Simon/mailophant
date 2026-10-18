import os
import shutil
import sqlite3
import tempfile
import unittest

from mailviewer import tags


class TagStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "tags.db")
        self.store = tags.TagStore(self.db)
        self.addCleanup(self.store.close)

    def rows(self, sql, *args):
        return self.store._conn().execute(sql, args).fetchall()

    def test_new_db_has_version_and_no_tags(self):
        self.assertEqual(self.store.list_tags(), [])
        self.assertEqual(self.rows("PRAGMA user_version"), [(1,)])

    def test_create_and_list_sorted(self):
        beta = self.store.create_tag("beta", "#00796b")
        alpha = self.store.create_tag("Alpha", "#0a66c2")
        self.assertIsInstance(beta["id"], int)
        self.assertIsInstance(alpha["id"], int)
        self.assertEqual([t["name"] for t in self.store.list_tags()], ["Alpha", "beta"])

    def test_duplicate_name_ignores_case(self):
        self.store.create_tag("Rechnung", "#00796b")
        with self.assertRaises(ValueError):
            self.store.create_tag("rechnung", "#0a66c2")

    def test_rename_to_own_name_in_other_case_is_allowed(self):
        tag = self.store.create_tag("Rechnung", "#00796b")
        updated = self.store.update_tag(tag["id"], "RECHNUNG", None)
        self.assertEqual(updated["name"], "RECHNUNG")
        self.assertEqual(updated["color"], "#00796b")

    def test_rename_onto_other_tag_fails(self):
        a = self.store.create_tag("Alpha", "#00796b")
        b = self.store.create_tag("Beta", "#0a66c2")
        with self.assertRaises(ValueError):
            self.store.update_tag(b["id"], "alpha", None)
        self.assertEqual(self.store.list_tags(), [
            {"id": a["id"], "name": "Alpha", "color": "#00796b"},
            {"id": b["id"], "name": "Beta", "color": "#0a66c2"},
        ])

    def test_update_and_delete_unknown_raise_key_error(self):
        with self.assertRaises(KeyError):
            self.store.update_tag(999, "x", None)
        with self.assertRaises(KeyError):
            self.store.delete_tag(999)

    def test_set_marks_star_and_tags(self):
        t1 = self.store.create_tag("eins", "#00796b")["id"]
        t2 = self.store.create_tag("zwei", "#0a66c2")["id"]
        self.assertEqual(self.store.set_marks("mid:a", True, [t1, t2]),
                         {"starred": True, "tags": [t1, t2]})
        self.assertEqual(self.store.set_marks("mid:a", None, [t2]),
                         {"starred": True, "tags": [t2]})
        self.assertEqual(self.store.set_marks("mid:a", False, None),
                         {"starred": False, "tags": [t2]})

    def test_empty_ident_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.set_marks("", True, None)
        self.assertEqual(self.rows("SELECT count(*) FROM stars"), [(0,)])

    def test_unknown_tag_id_changes_nothing(self):
        t1 = self.store.create_tag("eins", "#00796b")["id"]
        with self.assertRaises(ValueError):
            self.store.set_marks("mid:a", True, [t1, 999])
        self.assertEqual(self.store.marks(["mid:a"])["mid:a"], {"starred": False, "tags": []})

    def test_delete_tag_removes_it_from_mails(self):
        t1 = self.store.create_tag("eins", "#00796b")["id"]
        t2 = self.store.create_tag("zwei", "#0a66c2")["id"]
        self.store.set_marks("mid:a", None, [t1, t2])
        self.store.delete_tag(t1)
        self.assertEqual(self.store.marks(["mid:a"])["mid:a"]["tags"], [t2])

    def test_marks_for_unmarked_idents(self):
        self.assertEqual(self.store.marks(["mid:x"]), {"mid:x": {"starred": False, "tags": []}})

    def test_reopen_keeps_data(self):
        t1 = self.store.create_tag("eins", "#00796b")["id"]
        self.store.set_marks("mid:a", True, [t1])
        self.store.close()
        again = tags.TagStore(self.db)
        self.addCleanup(again.close)
        self.assertEqual([t["name"] for t in again.list_tags()], ["eins"])
        self.assertEqual(again.marks(["mid:a"])["mid:a"], {"starred": True, "tags": [t1]})

    def test_newer_version_is_refused_not_wiped(self):
        path = os.path.join(self.tmp, "newer.db")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE x (v INTEGER)")
        con.execute("PRAGMA user_version = 7")
        con.commit()
        con.close()
        with self.assertRaises(tags.TagStoreError):
            tags.TagStore(path)
        con = sqlite3.connect(path)
        self.addCleanup(con.close)
        self.assertEqual(con.execute("SELECT count(*) FROM sqlite_master WHERE name = 'x'").fetchone(), (1,))


if __name__ == "__main__":
    unittest.main()
