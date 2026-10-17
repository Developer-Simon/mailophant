"""Tests for library.py. Uses only synthetic fixtures in temp directories."""

import json
import os
import shutil
import tempfile
import unittest
from dataclasses import asdict
from unittest import mock

import library
import test_server as fx

ACCOUNT = "simon.test@example.test"


class LibraryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "lib")
        # A Proton account dir with one export, and a plain maildir outside the library.
        self.proton_account = os.path.join(self.tmp, "accounts", ACCOUNT)
        os.makedirs(self.proton_account)
        fx.make_export(self.proton_account, "mail_20261015T120000")
        self.maildir = fx.make_export(self.tmp, "loose")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def make_maildir_in_root(self, rel: str) -> str:
        return fx.make_export(self.root, rel)


class CreateOpenTest(LibraryTestCase):
    def test_create_then_open_roundtrip(self):
        lib = library.Library.create(self.root)
        src = lib.add_source(self.proton_account)
        again = library.Library.open(self.root)
        self.assertEqual([s.id for s in again.sources], [src.id])
        self.assertEqual(again.sources[0].kind, "proton")
        self.assertEqual(again.settings, library.Settings())

    def test_create_refuses_existing_library(self):
        library.Library.create(self.root)
        with self.assertRaises(library.LibraryError):
            library.Library.create(self.root)

    def test_open_missing_corrupt_or_newer_version_raises_and_keeps_file(self):
        path = os.path.join(self.root, library.LIBRARY_FILE)
        os.makedirs(self.root)
        cases = {
            "missing": None,
            "corrupt": "{not json",
            "newer": json.dumps({"version": 2, "next_id": 1, "sources": [],
                                 "settings": {"view": "combined", "proton_email": "", "cli": ""}}),
        }
        for label, content in cases.items():
            with self.subTest(label):
                if content is None:
                    self.assertFalse(os.path.exists(path))
                    with self.assertRaises(library.LibraryError):
                        library.Library.open(self.root)
                    continue
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                with open(path, "rb") as f:
                    before = f.read()
                with self.assertRaises(library.LibraryError):
                    library.Library.open(self.root)
                with open(path, "rb") as f:
                    self.assertEqual(f.read(), before)
                os.remove(path)

    def test_is_library(self):
        self.assertFalse(library.is_library(self.root))
        library.Library.create(self.root)
        self.assertTrue(library.is_library(self.root))

    def test_save_leaves_no_tmp_file(self):
        lib = library.Library.create(self.root)
        lib.add_source(self.maildir)
        self.assertEqual(sorted(os.listdir(self.root)), [library.LIBRARY_FILE])


class DetectKindTest(LibraryTestCase):
    def test_detect_kind(self):
        self.assertEqual(library.detect_kind(self.maildir), "maildir")
        self.assertEqual(library.detect_kind(self.proton_account), "proton")

    def test_detect_kind_rejects_unopenable(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        for path in (empty, os.path.join(self.tmp, "missing")):
            with self.subTest(path=path):
                with self.assertRaises(library.LibraryError):
                    library.detect_kind(path)


class AddSourceTest(LibraryTestCase):
    def test_add_detects_kind_and_stores_relative_path_inside_root(self):
        lib = library.Library.create(self.root)
        inside = self.make_maildir_in_root("sources/x")
        src_in = lib.add_source(inside)
        self.assertEqual(src_in.kind, "maildir")
        self.assertEqual(src_in.path, os.path.join("sources", "x"))
        src_out = lib.add_source(self.maildir)
        self.assertEqual(src_out.kind, "maildir")
        self.assertTrue(os.path.isabs(src_out.path))
        self.assertEqual(lib.abspath(src_in), os.path.join(os.path.abspath(self.root), "sources", "x"))
        self.assertEqual(lib.abspath(src_out), os.path.realpath(self.maildir))

    def test_add_default_name(self):
        lib = library.Library.create(self.root)
        self.assertEqual(lib.add_source(self.proton_account).name, ACCOUNT)
        self.assertEqual(lib.add_source(self.maildir).name, "loose")

    def test_add_explicit_name(self):
        lib = library.Library.create(self.root)
        self.assertEqual(lib.add_source(self.maildir, name="Arbeit").name, "Arbeit")

    def test_add_outlook_source(self):
        lib = library.Library.create(self.root)
        src = lib.add_source(self.maildir, kind="outlook")
        self.assertEqual(src.kind, "outlook")
        self.assertEqual(library.Library.open(self.root).get(src.id).kind, "outlook")
        with self.assertRaises(library.LibraryError):
            lib.add_source(self.proton_account, kind="outlook")

    def test_add_source_rejects_unknown_kind(self):
        lib = library.Library.create(self.root)
        for kind in ("bogus", "Outlook"):
            with self.subTest(kind=kind):
                with self.assertRaises(library.LibraryError):
                    lib.add_source(self.maildir, kind=kind)
        self.assertEqual(lib.sources, [])

    def test_add_rejects_unopenable_and_duplicate_paths(self):
        lib = library.Library.create(self.root)
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        with self.assertRaises(library.LibraryError):
            lib.add_source(empty)
        lib.add_source(self.maildir)
        link = os.path.join(self.tmp, "link")
        os.symlink(self.maildir, link)
        dotted = os.path.join(self.tmp, "loose", "..", "loose")
        for duplicate in (self.maildir, link, dotted):
            with self.subTest(duplicate=duplicate):
                with self.assertRaises(library.LibraryError):
                    lib.add_source(duplicate)
        self.assertEqual(len(lib.sources), 1)

    def test_ids_are_not_reused_after_remove(self):
        lib = library.Library.create(self.root)
        first = lib.add_source(self.maildir)
        second = lib.add_source(self.proton_account)
        self.assertEqual((first.id, second.id), ("s1", "s2"))
        lib.remove_source("s2")
        self.assertEqual(lib.add_source(self.proton_account).id, "s3")


class SourceEditTest(LibraryTestCase):
    def setUp(self):
        super().setUp()
        self.lib = library.Library.create(self.root)
        self.lib.add_source(self.maildir)  # s1
        self.lib.add_source(self.proton_account)  # s2

    def test_update_source_validates_icon(self):
        for icon in ({"type": "auto"}, {"type": "emoji", "value": "💼"},
                     {"type": "image", "file": "s1.png"}):
            with self.subTest(icon=icon):
                self.assertEqual(self.lib.update_source("s1", icon=icon).icon, icon)
        bad = (
            {"type": "emoji", "value": ""},
            {"type": "emoji", "value": "x" * 17},
            {"type": "x"},
            {"type": "image", "file": "icons/s1.png"},
            {"type": "image", "file": "../s1.png"},
            {"type": "image", "file": "a..b.png"},
            {"type": "image", "file": "..png"},
            {"type": "image", "file": "s1..png"},
            "auto",
        )
        for icon in bad:
            with self.subTest(icon=icon):
                with self.assertRaises(library.LibraryError):
                    self.lib.update_source("s1", icon=icon)
        self.assertEqual(library.Library.open(self.root).get("s1").icon, {"type": "image", "file": "s1.png"})

    def test_update_source_fields(self):
        src = self.lib.update_source("s1", name="Neu", hidden=True)
        self.assertEqual((src.name, src.hidden), ("Neu", True))
        self.assertEqual(library.Library.open(self.root).get("s1").name, "Neu")

    def test_update_unknown_source_raises_key_error(self):
        with self.assertRaises(KeyError):
            self.lib.update_source("s99", hidden=True)

    def test_move_source_reorders_and_clamps(self):
        self.lib.move_source("s2", 0)
        self.assertEqual([s.id for s in self.lib.sources], ["s2", "s1"])
        self.lib.move_source("s2", 99)
        self.assertEqual([s.id for s in self.lib.sources], ["s1", "s2"])
        self.assertEqual([s.id for s in library.Library.open(self.root).sources], ["s1", "s2"])

    def test_move_source_swaps_in_a_new_list(self):
        old = self.lib.sources
        self.lib.move_source("s2", 0)
        self.assertIsNot(self.lib.sources, old)
        self.assertEqual([s.id for s in old], ["s1", "s2"])  # a reader holding the old list sees it unchanged

    def test_remove_keeps_mail_files_and_deletes_icon(self):
        icons = os.path.join(self.root, "icons")
        os.makedirs(icons)
        for name in ("s1.png", "s10.png"):
            with open(os.path.join(icons, name), "wb") as f:
                f.write(b"png")
        self.lib.update_source("s1", icon={"type": "image", "file": "s1.png"})
        removed = self.lib.remove_source("s1")
        self.assertEqual(removed.id, "s1")
        self.assertEqual([s.id for s in library.Library.open(self.root).sources], ["s2"])
        self.assertFalse(os.path.exists(os.path.join(icons, "s1.png")))
        self.assertTrue(os.path.exists(os.path.join(icons, "s10.png")))
        self.assertTrue(os.path.isfile(os.path.join(self.maildir, "labels.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.maildir, fx.ID_PLAIN + ".eml")))

    def test_remove_unknown_source_raises_key_error(self):
        with self.assertRaises(KeyError):
            self.lib.remove_source("s99")


class FailedSaveTest(LibraryTestCase):
    """A save that fails (full or read-only disk) must leave the in-memory state as the file has it."""

    def setUp(self):
        super().setUp()
        self.lib = library.Library.create(self.root)
        self.lib.add_source(self.maildir)  # s1
        self.icons = os.path.join(self.root, "icons")
        os.makedirs(self.icons)
        with open(os.path.join(self.icons, "s1.png"), "wb") as f:
            f.write(b"png")
        self.lib.update_source("s1", icon={"type": "image", "file": "s1.png"})

    def state(self):
        return ([asdict(s) for s in self.lib.sources], asdict(self.lib.settings), self.lib.next_id)

    def disk_full(self):
        return mock.patch.object(library.os, "replace", side_effect=OSError("disk full"))

    def test_add_source(self):
        before = self.state()
        with self.disk_full(), self.assertRaises(OSError):
            self.lib.add_source(self.proton_account)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.lib.add_source(self.proton_account).id, "s2")  # the retry is not a duplicate
        self.assertEqual([s.id for s in library.Library.open(self.root).sources], ["s1", "s2"])

    def test_update_source(self):
        before = self.state()
        with self.disk_full(), self.assertRaises(OSError):
            self.lib.update_source("s1", name="Neu", hidden=True)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.lib.update_source("s1", name="Neu", hidden=True).name, "Neu")
        self.assertEqual(library.Library.open(self.root).get("s1").name, "Neu")

    def test_remove_source_keeps_source_and_icon(self):
        before = self.state()
        with self.disk_full(), self.assertRaises(OSError):
            self.lib.remove_source("s1")
        self.assertEqual(self.state(), before)
        self.assertTrue(os.path.exists(os.path.join(self.icons, "s1.png")))
        self.lib.remove_source("s1")
        self.assertEqual(library.Library.open(self.root).sources, [])
        self.assertFalse(os.path.exists(os.path.join(self.icons, "s1.png")))

    def test_update_settings(self):
        before = self.state()
        with self.disk_full(), self.assertRaises(OSError):
            self.lib.update_settings(view="single")
        self.assertEqual(self.state(), before)
        self.assertEqual(self.lib.update_settings(view="single").view, "single")
        self.assertEqual(library.Library.open(self.root).settings.view, "single")


class SettingsTest(LibraryTestCase):
    def test_settings_validation(self):
        lib = library.Library.create(self.root)
        self.assertEqual(lib.update_settings(view="single").view, "single")
        with self.assertRaises(library.LibraryError):
            lib.update_settings(view="x")
        with self.assertRaises(library.LibraryError):
            lib.update_settings(unknown="1")
        self.assertEqual(library.Library.open(self.root).settings.view, "single")


if __name__ == "__main__":
    unittest.main()
