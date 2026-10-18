import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from mailviewer import store
from mailviewer import transfer
from mailviewer.transfer import Item, dest_mid

IDENT = "<msg-1@example.org>"
FOLDER = "f1234"


def raw(path):
    with open(path, "rb") as f:
        return f.read()


def read_meta(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)["Payload"]


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = os.path.join(self.tmp, "source")
        self.dest = os.path.join(self.tmp, "dest")
        os.mkdir(self.src)
        os.mkdir(self.dest)
        self.logs = []

    def write_mail(self, d, mid, label_ids=("0", "5", "X1"), eml=b"Subject: Hallo\r\n\r\nbody\r\n"):
        with open(os.path.join(d, mid + ".eml"), "wb") as f:
            f.write(eml)
        meta = {"ID": mid, "LabelIDs": list(label_ids), "Subject": "Hallo", "Unread": 1}
        with open(os.path.join(d, mid + store.META_SUFFIX), "w", encoding="utf-8") as f:
            json.dump({"Version": 1, "Payload": meta}, f)

    def run_transfer(self, items, folder=FOLDER):
        return transfer.transfer(items, self.dest, folder, self.logs.append)

    def dest_paths(self, ident=IDENT):
        mid = dest_mid(ident)
        return os.path.join(self.dest, mid + ".eml"), os.path.join(self.dest, mid + store.META_SUFFIX)

    def test_copy_from_readonly_leaves_source_untouched(self):
        self.write_mail(self.src, "src1")
        eml = os.path.join(self.src, "src1.eml")
        meta = os.path.join(self.src, "src1" + store.META_SUFFIX)
        before = {p: (raw(p), os.stat(p).st_ino, os.stat(p).st_mtime_ns) for p in (eml, meta)}

        result = self.run_transfer([Item(self.src, "src1", IDENT, "copy")])

        self.assertEqual(result.copied, 1)
        for p, (data, ino, mtime) in before.items():
            st = os.stat(p)
            self.assertEqual(raw(p), data)
            self.assertEqual((st.st_ino, st.st_mtime_ns), (ino, mtime))
        dest_eml, dest_meta = self.dest_paths()
        payload = read_meta(dest_meta)
        self.assertEqual(payload["LabelIDs"], [FOLDER, "5"])
        self.assertEqual(payload["Unread"], 1)
        self.assertEqual(payload["Subject"], "Hallo")
        self.assertNotIn("X1", payload["LabelIDs"])

    def test_copy_name_is_derived_from_ident(self):
        self.write_mail(self.src, "src1")
        self.run_transfer([Item(self.src, "src1", IDENT, "copy")])
        name = dest_mid(IDENT)
        self.assertTrue(name.startswith("o"))
        self.assertEqual(len(name), 41)
        self.assertTrue(os.path.isfile(os.path.join(self.dest, name + ".eml")))

    def test_hardlink_used_on_same_fs_and_copy_fallback(self):
        self.write_mail(self.src, "src1")
        self.run_transfer([Item(self.src, "src1", IDENT, "copy")])
        dest_eml, _ = self.dest_paths()
        self.assertEqual(os.stat(dest_eml).st_nlink, 2)

        self.write_mail(self.src, "src2")
        other = "<msg-2@example.org>"
        with mock.patch("os.link", side_effect=OSError):
            self.run_transfer([Item(self.src, "src2", other, "copy")])
        dest_eml2, _ = self.dest_paths(other)
        self.assertEqual(raw(dest_eml2), raw(os.path.join(self.src, "src2.eml")))
        self.assertEqual(os.stat(dest_eml2).st_nlink, 1)

    def test_write_to_dest_meta_never_changes_hardlinked_source_meta(self):
        self.write_mail(self.src, "src1")
        src_meta = os.path.join(self.src, "src1" + store.META_SUFFIX)
        _, dest_meta = self.dest_paths()
        os.link(src_meta, dest_meta)
        before = raw(src_meta)

        self.run_transfer([Item(self.src, "src1", IDENT, "copy")])

        self.assertEqual(raw(src_meta), before)
        self.assertNotEqual(os.stat(dest_meta).st_ino, os.stat(src_meta).st_ino)
        self.assertEqual(read_meta(dest_meta)["LabelIDs"], [FOLDER, "5"])

    def test_existing_in_dest_is_reused_and_refiled(self):
        self.write_mail(self.src, "a1")
        self.write_mail(self.src, "b1")
        result = self.run_transfer([
            Item(self.src, "a1", IDENT, "copy"),
            Item(self.src, "b1", IDENT, "copy"),
        ])

        self.assertEqual((result.copied, result.reused), (1, 1))
        names = sorted(n for n in os.listdir(self.dest) if not n.startswith("."))
        self.assertEqual(names, sorted([dest_mid(IDENT) + ".eml", dest_mid(IDENT) + store.META_SUFFIX]))
        _, dest_meta = self.dest_paths()
        self.assertEqual(read_meta(dest_meta)["LabelIDs"], [FOLDER, "5"])

    def test_move_deletes_origin_only_after_copy(self):
        self.write_mail(self.src, "src1")
        result = self.run_transfer([Item(self.src, "src1", IDENT, "move")])

        self.assertEqual((result.copied, result.deleted_origin, result.failed), (1, 1, 0))
        self.assertFalse(os.path.exists(os.path.join(self.src, "src1.eml")))
        self.assertFalse(os.path.exists(os.path.join(self.src, "src1" + store.META_SUFFIX)))
        dest_eml, dest_meta = self.dest_paths()
        self.assertTrue(os.path.isfile(dest_eml))
        self.assertEqual(read_meta(dest_meta)["LabelIDs"], [FOLDER, "5"])

        shutil.rmtree(self.dest)
        os.mkdir(self.dest)
        self.write_mail(self.src, "src1")
        with mock.patch("mailviewer.transfer.atomic_write_json", side_effect=OSError):
            result = self.run_transfer([Item(self.src, "src1", IDENT, "move")])

        self.assertEqual((result.failed, result.deleted_origin), (1, 0))
        self.assertTrue(os.path.isfile(os.path.join(self.src, "src1.eml")))
        self.assertTrue(os.path.isfile(os.path.join(self.src, "src1" + store.META_SUFFIX)))
        self.assertTrue(self.logs)

    def test_move_when_already_in_dest_deletes_origin_only(self):
        self.write_mail(self.src, "src1")
        os.link(os.path.join(self.src, "src1.eml"), os.path.join(self.src, "other.eml"))
        self.write_mail(self.dest, dest_mid(IDENT))
        content = raw(os.path.join(self.src, "src1.eml"))

        result = self.run_transfer([Item(self.src, "src1", IDENT, "move")])

        self.assertEqual((result.reused, result.copied, result.deleted_origin), (1, 0, 1))
        self.assertFalse(os.path.exists(os.path.join(self.src, "src1.eml")))
        self.assertEqual(raw(os.path.join(self.src, "other.eml")), content)

    def test_move_onto_itself_through_symlink_keeps_mail(self):
        mid = dest_mid(IDENT)
        self.write_mail(self.src, mid)
        link = os.path.join(self.tmp, "link")
        os.symlink(self.src, link)
        self.dest = link  # the destination is the origin, reached through a symlink
        self.run_transfer([Item(self.src, mid, IDENT, "move")])
        self.assertTrue(os.path.exists(os.path.join(self.src, mid + ".eml")))
        self.assertTrue(os.path.exists(os.path.join(self.src, mid + store.META_SUFFIX)))

    def test_refile_changes_only_labels(self):
        self.write_mail(self.dest, "m1", label_ids=("5", "X2"))
        eml = os.path.join(self.dest, "m1.eml")
        meta = os.path.join(self.dest, "m1" + store.META_SUFFIX)
        before_eml = raw(eml)
        before = read_meta(meta)

        result = self.run_transfer([Item(self.dest, "m1", IDENT, "refile")])

        self.assertEqual(result.refiled, 1)
        after = read_meta(meta)
        self.assertEqual(after["LabelIDs"], [FOLDER, "5"])
        self.assertEqual({k: v for k, v in after.items() if k != "LabelIDs"},
                         {k: v for k, v in before.items() if k != "LabelIDs"})
        self.assertEqual(raw(eml), before_eml)

        self.write_mail(self.dest, "m1", label_ids=("5", "X2"))
        self.run_transfer([Item(self.dest, "m1", IDENT, "refile")], folder=None)
        self.assertEqual(read_meta(meta)["LabelIDs"], ["5"])

    def test_empty_ident_is_skipped_and_origin_kept(self):
        self.write_mail(self.src, "src1")
        result = self.run_transfer([Item(self.src, "src1", "", "move")])

        self.assertEqual(result.skipped, 1)
        self.assertTrue(os.path.isfile(os.path.join(self.src, "src1.eml")))
        self.assertTrue(os.path.isfile(os.path.join(self.src, "src1" + store.META_SUFFIX)))
        self.assertEqual(os.listdir(self.dest), [])
        self.assertTrue(self.logs)

    def test_tmp_files_ignored_and_overwritten(self):
        self.write_mail(self.src, "src1")
        stale = os.path.join(self.dest, "." + dest_mid(IDENT) + ".eml.tmp")
        with open(stale, "wb") as f:
            f.write(b"junk")

        result = self.run_transfer([Item(self.src, "src1", IDENT, "copy")])

        self.assertEqual(result.copied, 1)
        self.assertEqual(raw(self.dest_paths()[0]), raw(os.path.join(self.src, "src1.eml")))
        self.assertEqual([n for n in os.listdir(self.dest) if n.endswith(".tmp")], [])


if __name__ == "__main__":
    unittest.main()
