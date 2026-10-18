import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from mailviewer import cleanup


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ref = os.path.join(self.tmp, "ref")
        self.tgt = os.path.join(self.tmp, "tgt")
        os.mkdir(self.ref)
        os.mkdir(self.tgt)
        # Target copies are 10 and 20 bytes; the reference holds identical files.
        self.a = b"A" * 10
        self.b = b"B" * 20
        self.write(os.path.join(self.tgt, "a.eml"), self.a)
        self.write(os.path.join(self.tgt, "b.eml"), self.b)
        self.write(os.path.join(self.tgt, "a.metadata.json"), b"{" * 5)
        self.write(os.path.join(self.tgt, "b.metadata.json"), b"{" * 7)
        self.write(os.path.join(self.ref, "a.eml"), self.a)
        self.write(os.path.join(self.ref, "rb.eml"), self.b)
        self.pairs = [("a", "a"), ("b", "rb")]

    def write(self, path, data):
        with open(path, "wb") as f:
            f.write(data)

    def read(self, path):
        with open(path, "rb") as f:
            return f.read()

    def run_action(self, action, pairs=None):
        lines = []
        result = cleanup.run_cleanup(
            self.pairs if pairs is None else pairs, self.tgt, self.ref, action, lines.append
        )
        return result, lines

    def test_plan_counts_and_sizes(self):
        plan = cleanup.plan_cleanup(self.pairs, self.tgt, self.ref)
        self.assertEqual(plan.count, 2)
        self.assertEqual(plan.already_linked, 0)
        self.assertEqual(plan.missing_reference, 0)
        self.assertEqual(plan.bytes_link, 30)
        self.assertEqual(plan.bytes_delete, 30 + 5 + 7)
        self.assertTrue(plan.link_possible)
        self.assertIsNone(plan.link_reason)

    def test_plan_skips_already_linked(self):
        os.remove(os.path.join(self.tgt, "a.eml"))
        os.link(os.path.join(self.ref, "a.eml"), os.path.join(self.tgt, "a.eml"))
        plan = cleanup.plan_cleanup(self.pairs, self.tgt, self.ref)
        self.assertEqual(plan.already_linked, 1)
        self.assertEqual(plan.bytes_link, 20)

    def test_plan_same_dir_raises(self):
        with self.assertRaises(ValueError):
            cleanup.plan_cleanup(self.pairs, self.tgt, self.tgt)

    def test_plan_same_dir_via_symlink_raises(self):
        link = os.path.join(self.tmp, "tgt-link")
        os.symlink(self.tgt, link)
        with self.assertRaises(ValueError):
            cleanup.plan_cleanup(self.pairs, self.tgt, link)

    def test_plan_cross_device(self):
        real_stat = os.stat

        def fake_stat(path, *args, **kwargs):
            st = real_stat(path, *args, **kwargs)
            if path == self.tgt:
                return SimpleNamespace(
                    st_dev=st.st_dev + 1, st_size=st.st_size, st_nlink=st.st_nlink, st_ino=st.st_ino
                )
            return st

        with mock.patch("mailviewer.cleanup.os.stat", side_effect=fake_stat):
            plan = cleanup.plan_cleanup(self.pairs, self.tgt, self.ref)
        self.assertFalse(plan.link_possible)
        self.assertEqual(
            plan.link_reason,
            "Ziel und Referenz liegen auf verschiedenen Dateisystemen, Hardlinks sind nicht möglich.",
        )

    def test_link_replaces_with_hardlink(self):
        meta = os.path.join(self.tgt, "a.metadata.json")
        meta_bytes = self.read(meta)
        meta_ino = os.stat(meta).st_ino

        result, _ = self.run_action("link", pairs=[("a", "a")])

        target = os.path.join(self.tgt, "a.eml")
        self.assertTrue(os.path.samefile(target, os.path.join(self.ref, "a.eml")))
        self.assertEqual(self.read(target), self.a)
        self.assertEqual(self.read(meta), meta_bytes)
        self.assertEqual(os.stat(meta).st_ino, meta_ino)
        self.assertEqual(sorted(os.listdir(self.ref)), ["a.eml", "rb.eml"])
        self.assertFalse(os.path.lexists(os.path.join(self.tgt, ".a.eml.link-tmp")))
        self.assertEqual(result.linked, 1)

    def test_link_all_pairs(self):
        result, _ = self.run_action("link")
        self.assertEqual(result.linked, 2)
        self.assertEqual(result.freed, 30)
        self.assertTrue(os.path.samefile(
            os.path.join(self.tgt, "b.eml"), os.path.join(self.ref, "rb.eml")
        ))

    def test_delete_removes_both_files(self):
        result, _ = self.run_action("delete")
        self.assertEqual(os.listdir(self.tgt), [])
        self.assertEqual(sorted(os.listdir(self.ref)), ["a.eml", "rb.eml"])
        self.assertEqual(result.deleted, 2)
        self.assertEqual(result.freed, 42)

    def test_delete_removes_metadata_first(self):
        removed = []
        real_remove = os.remove

        def spy(path):
            removed.append(os.path.basename(path))
            real_remove(path)

        with mock.patch("mailviewer.cleanup.os.remove", side_effect=spy):
            self.run_action("delete", pairs=[("a", "a")])
        self.assertEqual(removed, ["a.metadata.json", "a.eml"])

    def test_missing_reference_is_skipped(self):
        os.remove(os.path.join(self.ref, "rb.eml"))
        result, lines = self.run_action("link")
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.linked, 1)
        self.assertFalse(os.path.samefile(
            os.path.join(self.tgt, "b.eml"), os.path.join(self.ref, "a.eml")
        ))
        self.assertTrue(any(line.startswith("skip") for line in lines))

    def test_missing_reference_keeps_delete_target(self):
        os.remove(os.path.join(self.ref, "rb.eml"))
        result, _ = self.run_action("delete")
        self.assertEqual(result.skipped, 1)
        self.assertTrue(os.path.exists(os.path.join(self.tgt, "b.eml")))
        self.assertTrue(os.path.exists(os.path.join(self.tgt, "b.metadata.json")))

    def test_reference_symlink_is_skipped(self):
        os.remove(os.path.join(self.ref, "rb.eml"))
        os.symlink(os.path.join(self.ref, "a.eml"), os.path.join(self.ref, "rb.eml"))
        result, _ = self.run_action("delete")
        self.assertEqual(result.skipped, 1)
        self.assertTrue(os.path.exists(os.path.join(self.tgt, "b.eml")))

    def test_reference_directory_is_skipped(self):
        os.remove(os.path.join(self.ref, "rb.eml"))
        os.mkdir(os.path.join(self.ref, "rb.eml"))
        result, _ = self.run_action("link")
        self.assertEqual(result.skipped, 1)
        self.assertEqual(self.read(os.path.join(self.tgt, "b.eml")), self.b)

    def test_link_error_keeps_file_and_continues(self):
        real_link = os.link
        calls = []

        def flaky_link(src, dst, *args, **kwargs):
            calls.append(dst)
            if len(calls) == 1:
                raise OSError(28, "No space left on device")
            return real_link(src, dst, *args, **kwargs)

        with mock.patch("mailviewer.cleanup.os.link", side_effect=flaky_link):
            result, lines = self.run_action("link")

        self.assertEqual(self.read(os.path.join(self.tgt, "a.eml")), self.a)
        self.assertFalse(os.path.samefile(
            os.path.join(self.tgt, "a.eml"), os.path.join(self.ref, "a.eml")
        ))
        self.assertTrue(os.path.samefile(
            os.path.join(self.tgt, "b.eml"), os.path.join(self.ref, "rb.eml")
        ))
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.linked, 1)
        self.assertFalse(os.path.lexists(os.path.join(self.tgt, ".a.eml.link-tmp")))
        self.assertTrue(any(line.startswith("error") for line in lines))

    def test_link_refused_cross_device(self):
        real_stat = os.stat

        def fake_stat(path, *args, **kwargs):
            st = real_stat(path, *args, **kwargs)
            if path == self.tgt:
                return SimpleNamespace(
                    st_dev=st.st_dev + 1, st_size=st.st_size, st_nlink=st.st_nlink, st_ino=st.st_ino
                )
            return st

        before = sorted(os.listdir(self.tgt))
        ino = os.stat(os.path.join(self.tgt, "a.eml")).st_ino
        with mock.patch("mailviewer.cleanup.os.stat", side_effect=fake_stat):
            with self.assertRaises(ValueError):
                self.run_action("link")
        self.assertEqual(sorted(os.listdir(self.tgt)), before)
        self.assertEqual(os.stat(os.path.join(self.tgt, "a.eml")).st_ino, ino)

    def test_unknown_action(self):
        with self.assertRaises(ValueError):
            self.run_action("move")
        self.assertEqual(len(os.listdir(self.tgt)), 4)


if __name__ == "__main__":
    unittest.main()
