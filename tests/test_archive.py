import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from mailviewer import archive
from mailviewer import store


def raw(path):
    with open(path, "rb") as f:
        return f.read()


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.d = os.path.join(self.tmp, "archive")
        os.mkdir(self.d)
        archive.write_empty_archive(self.d)

    def labels(self, d=None):
        with open(os.path.join(d or self.d, "labels.json"), encoding="utf-8") as f:
            return {x["ID"]: x for x in json.load(f)["Payload"]}

    def save_labels(self, payload, d=None):
        archive.atomic_write_json(os.path.join(d or self.d, "labels.json"), {"Version": 1, "Payload": payload})

    def write_mail(self, mid, label_ids, d=None):
        d = d or self.d
        with open(os.path.join(d, mid + ".eml"), "wb") as f:
            f.write(b"Subject: x\r\n\r\nbody\r\n")
        meta = {"Version": 1, "Payload": {"ID": mid, "LabelIDs": label_ids, "Subject": "x"}}
        archive.atomic_write_json(os.path.join(d, mid + archive.store.META_SUFFIX), meta)

    def read_mail(self, mid, d=None):
        with open(os.path.join(d or self.d, mid + archive.store.META_SUFFIX), encoding="utf-8") as f:
            return json.load(f)["Payload"]

    def test_create_and_nested_paths(self):
        top = archive.create_folder(self.d, "Rechnungen")
        child = archive.create_folder(self.d, "2024", parent_id=top["ID"])
        other = archive.create_folder(self.d, "Belege")
        labels = self.labels()
        self.assertEqual(child["Path"], "Rechnungen/2024")
        self.assertEqual(labels[child["ID"]]["Path"], "Rechnungen/2024")
        self.assertEqual(labels[child["ID"]]["Order"], 1)
        self.assertEqual(labels[top["ID"]]["Order"], 1)
        self.assertEqual(labels[other["ID"]]["Order"], 2)
        self.assertEqual(top["Type"], 3)
        self.assertTrue(top["ID"].startswith("f") and len(top["ID"]) == 17)

    def test_create_normalizes_alphabetical_when_order_missing(self):
        self.save_labels([
            {"ID": "fz", "Name": "Zeta", "Path": "Zeta", "Color": "", "Type": 3},
            {"ID": "fa", "Name": "alpha", "Path": "alpha", "Color": "", "Type": 3},
        ])
        new = archive.create_folder(self.d, "Neu")
        labels = self.labels()
        self.assertEqual(labels["fa"]["Order"], 1)
        self.assertEqual(labels["fz"]["Order"], 2)
        self.assertEqual(labels[new["ID"]]["Order"], 3)

    def test_invalid_names(self):
        for name in ["", "  ", "a/b", "x" * 101, "a\tb", "a\x00b"]:
            with self.subTest(name=name), self.assertRaises(archive.ArchiveError):
                archive.create_folder(self.d, name)

    def test_duplicate_names_among_siblings(self):
        top = archive.create_folder(self.d, "Rechnungen")
        with self.assertRaises(archive.ArchiveError):
            archive.create_folder(self.d, "rechnungen")
        archive.create_folder(self.d, "Rechnungen", parent_id=top["ID"])  # other parent is fine
        with self.assertRaises(archive.ArchiveError):
            archive.create_folder(self.d, "Rechnungen", parent_id="missing")
        with self.assertRaises(archive.ArchiveError):
            archive.create_folder(self.d, "Kind", parent_id="5")

    def test_rename_rewrites_descendant_paths_keeps_ids(self):
        top = archive.create_folder(self.d, "Rechnungen")
        child = archive.create_folder(self.d, "2024", parent_id=top["ID"])
        self.write_mail("m1", [top["ID"], child["ID"]])
        before = raw(os.path.join(self.d, "m1" + archive.store.META_SUFFIX))
        archive.rename_folder(self.d, top["ID"], "Belege")
        labels = self.labels()
        self.assertEqual(labels[top["ID"]]["Name"], "Belege")
        self.assertEqual(labels[top["ID"]]["Path"], "Belege")
        self.assertEqual(labels[child["ID"]]["Path"], "Belege/2024")
        self.assertEqual(set(labels), {"5", top["ID"], child["ID"]})
        self.assertEqual(raw(os.path.join(self.d, "m1" + archive.store.META_SUFFIX)), before)

    def test_move_reorders_and_nests(self):
        a = archive.create_folder(self.d, "A")
        b = archive.create_folder(self.d, "B")
        c = archive.create_folder(self.d, "C")
        archive.move_folder(self.d, c["ID"], None, 0)
        labels = self.labels()
        self.assertEqual([labels[x]["Name"] for x in (c["ID"], a["ID"], b["ID"])], ["C", "A", "B"])
        self.assertEqual([labels[x]["Order"] for x in (c["ID"], a["ID"], b["ID"])], [1, 2, 3])
        archive.move_folder(self.d, b["ID"], a["ID"], 0)
        labels = self.labels()
        self.assertEqual(labels[b["ID"]]["Path"], "A/B")
        self.assertEqual(labels[b["ID"]]["Order"], 1)
        self.assertEqual([labels[x]["Order"] for x in (c["ID"], a["ID"])], [1, 2])

    def test_move_into_own_descendant_rejected(self):
        a = archive.create_folder(self.d, "A")
        b = archive.create_folder(self.d, "B", parent_id=a["ID"])
        with self.assertRaises(archive.ArchiveError):
            archive.move_folder(self.d, a["ID"], b["ID"], 0)
        with self.assertRaises(archive.ArchiveError):
            archive.move_folder(self.d, a["ID"], a["ID"], 0)
        self.assertEqual(self.labels()[b["ID"]]["Path"], "A/B")

    def test_system_folders_immutable(self):
        self.save_labels([
            {"ID": "0", "Name": "Inbox", "Path": "Inbox", "Color": "", "Type": 3},
            {"ID": "5", "Name": "All Mail", "Path": "All Mail", "Color": "", "Type": 3},
        ])
        for call in [
            lambda: archive.rename_folder(self.d, "0", "x"),
            lambda: archive.rename_folder(self.d, "5", "x"),
            lambda: archive.delete_folder(self.d, "0", print),
            lambda: archive.delete_folder(self.d, "5", print),
            lambda: archive.move_folder(self.d, "5", None, 0),
            lambda: archive.move_folder(self.d, "0", None, 0),
        ]:
            with self.assertRaises(archive.ArchiveError):
                call()
        child = archive.create_folder(self.d, "Unter", parent_id="0")  # system folders may be parents
        self.assertEqual(child["Path"], "Inbox/Unter")
        self.assertTrue(archive.is_system_folder("0"))
        self.assertTrue(archive.is_system_folder("10"))
        self.assertFalse(archive.is_system_folder(child["ID"]))

    def test_delete_strips_ids_keeps_all_mail(self):
        f1 = archive.create_folder(self.d, "Eins")
        f2 = archive.create_folder(self.d, "Zwei", parent_id=f1["ID"])
        self.write_mail("m1", [f1["ID"], f2["ID"]])
        eml = raw(os.path.join(self.d, "m1.eml"))
        logs = []
        self.assertEqual(archive.delete_folder(self.d, f1["ID"], logs.append), 1)
        self.assertEqual(self.read_mail("m1")["LabelIDs"], ["5"])
        self.assertEqual(set(self.labels()), {"5"})
        self.assertEqual(raw(os.path.join(self.d, "m1.eml")), eml)
        self.assertEqual(logs, [])

    def test_delete_skips_broken_metadata_with_log(self):
        f1 = archive.create_folder(self.d, "Eins")
        self.write_mail("m1", [f1["ID"]])
        with open(os.path.join(self.d, "bad" + archive.store.META_SUFFIX), "w") as f:
            f.write("{kaputt")
        logs = []
        self.assertEqual(archive.delete_folder(self.d, f1["ID"], logs.append), 1)
        self.assertEqual(len(logs), 1)
        self.assertIn("bad", logs[0])

    def test_delete_breaks_hardlink(self):
        other = os.path.join(self.tmp, "other")
        os.mkdir(other)
        archive.write_empty_archive(other)
        f1 = archive.create_folder(self.d, "Eins")
        self.write_mail("m1", [f1["ID"]], d=other)
        meta = "m1" + archive.store.META_SUFFIX
        os.link(os.path.join(other, meta), os.path.join(self.d, meta))
        original = raw(os.path.join(other, meta))
        inode = os.stat(os.path.join(self.d, meta)).st_ino
        archive.delete_folder(self.d, f1["ID"], print)
        self.assertNotEqual(os.stat(os.path.join(self.d, meta)).st_ino, inode)
        self.assertEqual(raw(os.path.join(other, meta)), original)
        self.assertEqual(self.read_mail("m1")["LabelIDs"], ["5"])

    def test_atomic_write_leaves_no_tmp_on_failure(self):
        path = os.path.join(self.tmp, "x.json")
        with open(path, "wb") as f:
            f.write(b"orig")
        with mock.patch("mailviewer.archive.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                archive.atomic_write(path, b"new")
        self.assertEqual(sorted(os.listdir(self.tmp)), ["archive", "x.json"])
        self.assertEqual(raw(path), b"orig")

    def test_ensure_folder_creates_once(self):
        first = archive.ensure_folder(self.d, "Unsortiert")
        second = archive.ensure_folder(self.d, "unsortiert")
        self.assertEqual(first, second)
        self.assertEqual([x["Name"] for x in self.labels().values() if x["Name"] == "Unsortiert"], ["Unsortiert"])

    def test_rename_does_not_touch_prefix_sibling(self):
        top = archive.create_folder(self.d, "Rechnungen")
        other = archive.create_folder(self.d, "Rechnungen2")
        x = archive.create_folder(self.d, "x", parent_id=other["ID"])
        archive.rename_folder(self.d, top["ID"], "Belege")
        self.assertEqual(self.labels()[x["ID"]]["Path"], "Rechnungen2/x")

    def test_invalid_label_entries_raise_archive_error(self):
        broken = [
            {"ID": "f1", "Path": "Eins"},
            {"ID": "f1", "Name": "Eins"},
            {"ID": "f1", "Name": 5, "Path": "Eins"},
            {"ID": "f1", "Name": "Eins", "Path": None},
        ]
        for entry in broken:
            with self.subTest(entry=entry):
                self.save_labels([entry])
                with self.assertRaises(archive.ArchiveError):
                    archive.create_folder(self.d, "Neu")

    def test_ensure_folder_ignores_system_folders(self):
        self.save_labels([
            {"ID": "0", "Name": "Inbox", "Path": "Inbox", "Color": "", "Type": 3},
            {"ID": "5", "Name": "All Mail", "Path": "All Mail", "Color": "", "Type": 3},
        ])
        with self.assertRaises(archive.ArchiveError):
            archive.ensure_folder(self.d, "Inbox")
        with self.assertRaises(archive.ArchiveError):
            archive.ensure_folder(self.d, "all mail")
        self.assertNotIn(archive.ensure_folder(self.d, "Unsortiert"), {"0", "5"})

    def test_delete_tolerates_non_string_label_ids(self):
        f1 = archive.create_folder(self.d, "Eins")
        self.write_mail("m1", [f1["ID"], {"x": 1}])
        self.write_mail("m2", [["y"], {"x": 1}])
        self.assertEqual(archive.delete_folder(self.d, f1["ID"], print), 1)
        self.assertEqual(self.read_mail("m1")["LabelIDs"], [{"x": 1}, "5"])
        self.assertEqual(self.read_mail("m2")["LabelIDs"], [["y"], {"x": 1}])

    def ids(self):
        with open(os.path.join(self.d, "labels.json"), encoding="utf-8") as f:
            return [x["ID"] for x in json.load(f)["Payload"]]

    def index_ok(self, mid, label_id):
        """A mail under label_id must index: duplicate label ids in labels.json make sync_source fail."""
        self.write_mail(mid, [label_id, "5"])
        index = store.Store(os.path.join(self.tmp, "index.db"))
        self.addCleanup(index.close)
        index.sync_source("arch", self.d)

    def test_renamed_folder_keeps_id_and_recreated_name_gets_new_id(self):
        folder = archive.create_folder(self.d, "Unsortiert")
        archive.rename_folder(self.d, folder["ID"], "Alt")
        again = archive.create_folder(self.d, "Unsortiert")
        self.assertNotEqual(again["ID"], folder["ID"])
        self.assertEqual(len(set(self.ids())), len(self.ids()))
        self.index_ok("m1", again["ID"])

    def test_moved_folder_keeps_id_and_recreated_name_gets_new_id(self):
        top = archive.create_folder(self.d, "Unsortiert")
        rest = archive.create_folder(self.d, "Rest")
        archive.move_folder(self.d, top["ID"], rest["ID"], 0)
        again = archive.create_folder(self.d, "Unsortiert")
        self.assertNotEqual(again["ID"], top["ID"])
        self.assertEqual(len(set(self.ids())), len(self.ids()))
        self.index_ok("m1", again["ID"])

    def test_ensure_folder_after_rename_creates_new_folder(self):
        folder = archive.create_folder(self.d, "Unsortiert")
        archive.rename_folder(self.d, folder["ID"], "Alt")
        new_id = archive.ensure_folder(self.d, "Unsortiert")
        self.assertNotEqual(new_id, folder["ID"])
        self.assertEqual(len(set(self.ids())), len(self.ids()))
        self.index_ok("m1", new_id)

    def test_leftover_tmp_hardlink_is_not_written_through(self):
        other = os.path.join(self.tmp, "other.txt")
        with open(other, "wb") as f:
            f.write(b"keep")
        path = os.path.join(self.tmp, "x.json")
        os.link(other, os.path.join(self.tmp, ".x.json.tmp"))
        archive.atomic_write(path, b"new")
        self.assertEqual(raw(other), b"keep")
        self.assertEqual(raw(path), b"new")


if __name__ == "__main__":
    unittest.main()
