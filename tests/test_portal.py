import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from mailviewer import portal


class PortalPathTests(unittest.TestCase):
    def setUp(self):
        portal._host_root.cache_clear()

    def test_is_portal_path(self):
        self.assertTrue(portal.is_portal_path("/run/user/1000/doc/AbC123/Mail"))
        self.assertTrue(portal.is_portal_path("/run/flatpak/doc/AbC123/Mail/archive.db"))
        self.assertFalse(portal.is_portal_path("/home/u/Mail"))
        self.assertFalse(portal.is_portal_path("/run/user/1000/docs/x"))

    def test_host_path_replaces_the_document_root(self):
        out = "({'AbC123': b'/home/u/Mail \\303\\244lt'},)\n"
        with mock.patch.object(portal.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=out)) as run:
            self.assertEqual(portal.host_path("/run/user/1000/doc/AbC123/Mail ält/mail_1"), "/home/u/Mail ält/mail_1")
            self.assertEqual(portal.host_path("/run/user/1000/doc/AbC123/Mail ält"), "/home/u/Mail ält")
        self.assertEqual(run.call_count, 1)  # cached per document

    def test_host_path_falls_back_to_the_given_path(self):
        self.assertEqual(portal.host_path("/home/u/Mail"), "/home/u/Mail")
        with mock.patch.object(portal.subprocess, "run", side_effect=OSError("no gdbus")):
            self.assertEqual(portal.host_path("/run/user/1000/doc/X/Mail"), "/run/user/1000/doc/X/Mail")

    def test_journal_mode(self):
        self.assertEqual(portal.journal_mode("/run/user/1000/doc/X/Mail/archive.db"), "DELETE")
        self.assertEqual(portal.journal_mode("/home/u/Mail/archive.db"), "WAL")
        self.assertEqual(portal.journal_mode(":memory:"), "WAL")

    def test_leave_wal_switches_a_wal_database_once(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        db = os.path.join(tmp, "tags.db")
        con = sqlite3.connect(db)
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("CREATE TABLE t (x)")
        con.commit()
        con.close()
        portal.leave_wal(db)  # no portal path: untouched
        with open(db, "rb") as f:
            self.assertEqual(f.read(20)[18], 2)
        with mock.patch.object(portal, "is_portal_path", return_value=True):
            portal.leave_wal(db)
            portal.leave_wal(os.path.join(tmp, "missing.db"))
        with open(db, "rb") as f:
            self.assertEqual(f.read(20)[18], 1)


if __name__ == "__main__":
    unittest.main()
