import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from mailviewer import autoarchive
from mailviewer import store
from mailviewer.autoarchive import Candidate, delete_exports, plan_vanished, should_brake, archive_candidates
from mailviewer.transfer import TransferResult


def write_export(root, name, mails):
    """mails: (mid, message_id, label_ids); a message_id of None writes a mail without header."""
    d = os.path.join(root, name)
    os.mkdir(d)
    with open(os.path.join(d, "labels.json"), "w", encoding="utf-8") as f:
        json.dump({"Version": 1, "Payload": [{"ID": "5", "Name": "Alle"}]}, f)
    for mid, msgid, labels in mails:
        write_mail(d, mid, msgid, labels)
    return d


def write_mail(d, mid, msgid, labels):
    head = f"Message-ID: <{msgid}>\r\n" if msgid else ""
    with open(os.path.join(d, mid + ".eml"), "wb") as f:
        f.write((head + "Subject: Hallo\r\n\r\nbody\r\n").encode("ascii"))
    meta = {"Version": 1, "Payload": {"ID": mid, "LabelIDs": list(labels), "Subject": "Hallo"}}
    with open(os.path.join(d, mid + store.META_SUFFIX), "w", encoding="utf-8") as f:
        json.dump(meta, f)


def read_meta(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)["Payload"]


class PlanVanishedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_vanished_between_old_and_new(self):
        a = write_export(self.tmp, "mail_a", [("m1", "one@x", ["0", "5"])])
        b = write_export(self.tmp, "mail_b", [("m1", "one@x", ["0", "5"])])
        plan = plan_vanished([a, b], write_export(self.tmp, "mail_c", []))
        self.assertEqual([c.export_dir for c in plan.candidates], [b])
        self.assertEqual(plan.candidates[0].mid, "m1")
        self.assertEqual(plan.vanished, 1)

    def test_vanished_between_two_old_exports_counts(self):
        a = write_export(self.tmp, "mail_a", [("m1", "one@x", ["0", "5"])])
        b = write_export(self.tmp, "mail_b", [])
        plan = plan_vanished([a, b], write_export(self.tmp, "mail_c", []))
        self.assertEqual([c.export_dir for c in plan.candidates], [a])
        self.assertEqual(plan.vanished, 1)

    def test_unchanged_mail_not_candidate(self):
        a = write_export(self.tmp, "mail_a", [("m1", "one@x", ["0", "5"])])
        b = write_export(self.tmp, "mail_b", [("m1", "one@x", ["0", "5"])])
        plan = plan_vanished([a, b], write_export(self.tmp, "mail_c", [("m1", "one@x", ["0", "5"])]))
        self.assertEqual(plan.candidates, [])
        self.assertEqual(plan.vanished, 0)
        self.assertEqual(plan.reference_total, 1)

    def test_excluded_by_latest_old_export_labels(self):
        new = write_export(self.tmp, "mail_c", [])
        old_a = write_export(self.tmp, "mail_a", [("m1", "one@x", ["0", "5"])])
        old_b = write_export(self.tmp, "mail_b", [("m1", "one@x", ["4", "5"])])
        plan = plan_vanished([old_a, old_b], new)
        self.assertEqual(plan.candidates, [])
        self.assertEqual(plan.excluded, 1)

    def test_latest_old_export_inbox_makes_candidate(self):
        new = write_export(self.tmp, "mail_c", [])
        old_a = write_export(self.tmp, "mail_a", [("m1", "one@x", ["4", "5"])])
        old_b = write_export(self.tmp, "mail_b", [("m1", "one@x", ["0", "5"])])
        plan = plan_vanished([old_a, old_b], new)
        self.assertEqual([c.export_dir for c in plan.candidates], [old_b])
        self.assertEqual(plan.excluded, 0)

    def test_each_excluded_label_excludes(self):
        for label in ("1", "3", "8"):
            with self.subTest(label=label):
                new = write_export(self.tmp, "new_" + label, [])
                old = write_export(self.tmp, "mail_" + label, [("m1", "one@x", [label, "5"])])
                plan = plan_vanished([old], new)
                self.assertEqual(plan.candidates, [])
                self.assertEqual(plan.excluded, 1)

    def test_other_labels_are_candidates(self):
        new = write_export(self.tmp, "mail_c", [])
        old = write_export(self.tmp, "mail_a", [("m1", "one@x", ["5", "10"])])
        plan = plan_vanished([old], new)
        self.assertEqual(len(plan.candidates), 1)
        self.assertEqual(plan.excluded, 0)

    def test_hardlinked_files_parsed_once(self):
        a = write_export(self.tmp, "mail_a", [("m1", "one@x", ["0", "5"])])
        b = write_export(self.tmp, "mail_b", [("m1", "one@x", ["0", "5"])])
        os.remove(os.path.join(b, "m1.eml"))
        os.link(os.path.join(a, "m1.eml"), os.path.join(b, "m1.eml"))
        new = write_export(self.tmp, "mail_c", [])
        real = autoarchive.mail_ident
        with mock.patch("mailviewer.autoarchive.mail_ident", wraps=real) as ident:
            plan = plan_vanished([a, b], new)
        self.assertEqual(ident.call_count, 1)
        self.assertEqual([c.export_dir for c in plan.candidates], [b])

    def test_broken_metadata_counts_unreadable_not_candidate(self):
        a = write_export(self.tmp, "mail_a", [("m1", "one@x", ["0", "5"])])
        with open(os.path.join(a, "m1" + store.META_SUFFIX), "w", encoding="utf-8") as f:
            f.write("{kaputt")
        write_mail(a, "m2", "two@x", ["0", "5"])
        os.remove(os.path.join(a, "m2.eml"))
        new = write_export(self.tmp, "mail_c", [])
        plan = plan_vanished([a], new)
        self.assertEqual(plan.candidates, [])
        self.assertEqual(plan.unreadable, 2)

    def test_payload_not_an_object_counts_unreadable(self):
        a = write_export(self.tmp, "mail_a", [("m1", "one@x", ["0", "5"])])
        with open(os.path.join(a, "m1" + store.META_SUFFIX), "w", encoding="utf-8") as f:
            json.dump({"Version": 1, "Payload": ["kein Objekt"]}, f)
        new = write_export(self.tmp, "mail_c", [])
        plan = plan_vanished([a], new)
        self.assertEqual(plan.candidates, [])
        self.assertEqual(plan.unreadable, 1)

    def test_unreadable_in_new_export_not_counted(self):
        a = write_export(self.tmp, "mail_a", [("m1", "one@x", ["0", "5"])])
        new = write_export(self.tmp, "mail_c", [("m1", "one@x", ["0", "5"]), ("m2", "two@x", ["0", "5"])])
        with open(os.path.join(new, "m1" + store.META_SUFFIX), "w", encoding="utf-8") as f:
            f.write("{kaputt")
        os.remove(os.path.join(new, "m2.eml"))
        plan = plan_vanished([a], new)
        self.assertEqual(plan.unreadable, 0)

    def test_unreadable_in_old_export_counted_across_all_old_dirs(self):
        a = write_export(self.tmp, "mail_a", [("m1", "one@x", ["0", "5"])])
        b = write_export(self.tmp, "mail_b", [("m2", "two@x", ["0", "5"])])
        os.remove(os.path.join(a, "m1.eml"))
        os.remove(os.path.join(b, "m2.eml"))
        plan = plan_vanished([a, b], write_export(self.tmp, "mail_c", []))
        self.assertEqual(plan.unreadable, 2)
        self.assertEqual(plan.candidates, [])

    def test_should_brake_thresholds(self):
        def plan(vanished, reference_total):
            return autoarchive.Plan([], vanished, 0, reference_total, 0)

        self.assertFalse(should_brake(plan(200, 10000)))
        self.assertTrue(should_brake(plan(201, 10000)))
        self.assertFalse(should_brake(plan(20, 100)))
        self.assertTrue(should_brake(plan(21, 100)))
        self.assertFalse(should_brake(plan(0, 0)))


class ArchiveAndPruneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.logs = []

    def log(self, msg):
        self.logs.append(msg)

    def test_archive_candidates_copies_into_folder(self):
        src = write_export(self.tmp, "mail_a", [("m1", "one@x", ["0", "5"])])
        dest = os.path.join(self.tmp, "archive")
        os.mkdir(dest)
        before = sorted(os.listdir(src))
        cand = [Candidate(ident="mid:one@x", export_dir=src, mid="m1")]
        result = archive_candidates(cand, dest, "f1234", self.log)
        self.assertIsInstance(result, TransferResult)
        self.assertEqual(result.copied, 1)
        metas = [f for f in os.listdir(dest) if f.endswith(store.META_SUFFIX)]
        self.assertEqual(len(metas), 1)
        self.assertEqual(read_meta(os.path.join(dest, metas[0]))["LabelIDs"], ["f1234", "5"])
        self.assertEqual(sorted(os.listdir(src)), before)

    def test_delete_exports_refuses_active_symlink_and_foreign(self):
        active = write_export(self.tmp, "mail_active", [("m1", "one@x", ["5"])])
        old = write_export(self.tmp, "mail_old", [("m1", "one@x", ["5"])])
        target = write_export(self.tmp, "elsewhere", [("m1", "one@x", ["5"])])
        os.symlink(target, os.path.join(self.tmp, "mail_x"))
        other = os.path.join(self.tmp, "other")
        os.mkdir(other)
        no_labels = os.path.join(self.tmp, "mail_y")
        os.mkdir(no_labels)
        dirs = [active, os.path.join(self.tmp, "mail_x"), other, no_labels, old]
        count = delete_exports(dirs, active, self.log)
        self.assertEqual(count, 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isdir(active))
        self.assertTrue(os.path.islink(os.path.join(self.tmp, "mail_x")))
        self.assertTrue(os.path.isdir(target))
        self.assertTrue(os.path.isdir(other))
        self.assertTrue(os.path.isdir(no_labels))
        self.assertTrue(self.logs)

    def test_delete_exports_stops_on_error(self):
        dirs = [write_export(self.tmp, f"mail_{n}", []) for n in "abc"]
        real = shutil.rmtree
        calls = []

        def flaky(path, *args, **kwargs):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("boom")
            return real(path, *args, **kwargs)

        with mock.patch("mailviewer.autoarchive.shutil.rmtree", side_effect=flaky):
            count = delete_exports(dirs, os.path.join(self.tmp, "active"), self.log)
        self.assertEqual(count, 1)
        self.assertFalse(os.path.exists(dirs[0]))
        self.assertTrue(os.path.isdir(dirs[1]))
        self.assertTrue(os.path.isdir(dirs[2]))
        self.assertTrue(any("boom" in line for line in self.logs))


if __name__ == "__main__":
    unittest.main()
