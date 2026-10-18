"""Tests for outlook.py. Uses only synthetic fixtures in temp directories."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from unittest import mock

from mailviewer import mailparse
from mailviewer import store
from mailviewer.outlook import (
    INSTALL_HINT,
    Folder,
    ImportUnavailable,
    OutlookMessage,
    Person,
    PypffReader,
    Unreadable,
    _decode_body,
    build_eml,
    convert,
    open_reader,
    pypff_available,
)

ALICE = Person("Anna", "anna@example.org")
BEN = Person("Ben", "ben@example.org")


def message(**overrides) -> OutlookMessage:
    base = dict(
        message_class="IPM.Note",
        headers="",
        subject="Hallo",
        sender=ALICE,
        to=[BEN],
        cc=[],
        time=1700000000,
        read=True,
        plain="Text",
        html="<p>Text</p>",
        rtf=None,
        attachments=[],
    )
    base.update(overrides)
    return OutlookMessage(**base)


def parse(data: bytes):
    return BytesParser(policy=policy.default).parsebytes(data)


def attachments_of(parsed) -> list[tuple[str, bytes]]:
    return [(part.get_filename(), part.get_payload(decode=True)) for part in parsed.iter_attachments()]


class BuildEmlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_plain_and_html_make_alternative(self):
        parsed = parse(build_eml(message()))
        self.assertEqual(parsed.get_content_type(), "multipart/alternative")
        self.assertIn("<p>Text</p>", parsed.get_body(("html",)).get_content())
        self.assertTrue(parsed.get_boundary().startswith("=_mbv_"))

    def test_attachments_make_mixed(self):
        data = build_eml(message(attachments=[("a.pdf", b"%PDF"), ("", b"x")]))
        parsed = parse(data)
        self.assertEqual(parsed.get_content_type(), "multipart/mixed")
        self.assertEqual(attachments_of(parsed), [("a.pdf", b"%PDF"), ("Anhang-2", b"x")])

    def test_rtf_only_becomes_attachment(self):
        parsed = parse(build_eml(message(plain=None, html=None, rtf=b"{\\rtf1}")))
        parts = list(parsed.iter_attachments())
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].get_filename(), "Nachricht.rtf")
        self.assertEqual(parts[0].get_content_type(), "application/rtf")
        self.assertEqual(parts[0].get_payload(decode=True), b"{\\rtf1}")

    def test_transport_headers_kept_without_content_headers(self):
        headers = "Message-ID: <A@x>\r\nFrom: a@x\r\nContent-Type: text/plain\r\nX-Foo: 1\r\n"
        parsed = parse(build_eml(message(headers=headers)))
        self.assertEqual(parsed["Message-ID"], "<A@x>")
        self.assertEqual(parsed["X-Foo"], "1")
        self.assertEqual(len(parsed.get_all("Content-Type")), 1)
        self.assertEqual(parsed.get_content_type(), "multipart/alternative")

    def test_headers_built_without_transport_headers(self):
        parsed = parse(build_eml(message()))
        self.assertEqual(parsed["From"], "Anna <anna@example.org>")
        self.assertEqual(parsed["To"], "Ben <ben@example.org>")
        self.assertEqual(parsed["Subject"], "Hallo")
        self.assertEqual(parsedate_to_datetime(parsed["Date"]).timestamp(), 1700000000)
        self.assertIsNone(parsed["Message-ID"])
        self.assertIsNone(parsed["Cc"])

    def test_cc_header_when_cc_present(self):
        parsed = parse(build_eml(message(cc=[Person("Carl", "carl@example.org")])))
        self.assertEqual(parsed["Cc"], "Carl <carl@example.org>")

    def test_deterministic_bytes(self):
        first = build_eml(message(attachments=[("a.pdf", b"%PDF")]))
        second = build_eml(message(attachments=[("a.pdf", b"%PDF")]))
        self.assertEqual(first, second)

    def test_odd_headers_still_loadable(self):
        raw = (
            "From: Müller <mueller@example.org>\r\n"
            "Subject: Grüße aus\r\n"
            " dem Büro\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "Content-Type: text/html\r\n"
            "\r\n"
            "Inhalt\r\n"
        )
        with open(os.path.join(self.tmp, "x.eml"), "wb") as f:
            f.write(raw.encode("utf-8"))
        out = mailparse.load_message(self.tmp, "x", {})
        self.assertIsNone(out["error"])
        self.assertIn("Inhalt", out["text"])

    def test_built_eml_loads_in_server(self):
        with open(os.path.join(self.tmp, "x.eml"), "wb") as f:
            f.write(build_eml(message(attachments=[("a.pdf", b"%PDF")])))
        out = mailparse.load_message(self.tmp, "x", {})
        self.assertIsNone(out["error"])
        self.assertEqual(out["text"].strip(), "Text")
        self.assertEqual(out["attachments"][0]["name"], "a.pdf")


class FakeReader:
    def __init__(self, folders: list[Folder]):
        self._folders = folders
        self.closed = False

    def folders(self):
        yield from self._folders

    def close(self) -> None:
        self.closed = True


def folder(path, *items, error=None) -> Folder:
    return Folder(path=tuple(path), count=len(items), items=lambda: iter(items), error=error)


def label_id(path: str) -> str:
    return "f" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


class ConvertTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dest = os.path.join(self.tmp, "out")
        os.mkdir(self.dest)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def metas(self, dest=None) -> dict[str, dict]:
        dest = dest or self.dest
        out = {}
        for name in os.listdir(dest):
            if name.endswith(".metadata.json"):
                with open(os.path.join(dest, name), encoding="utf-8") as f:
                    payload = json.load(f)
                self.assertEqual(payload["Version"], 1)
                out[name[: -len(".metadata.json")]] = payload["Payload"]
        return out

    def labels(self, dest=None) -> dict[str, dict]:
        with open(os.path.join(dest or self.dest, "labels.json"), encoding="utf-8") as f:
            payload = json.load(f)
        self.assertEqual(payload["Version"], 1)
        return {x["ID"]: x for x in payload["Payload"]}

    def eml_files(self, dest=None) -> list[str]:
        return sorted(n for n in os.listdir(dest or self.dest) if n.endswith(".eml"))

    def test_writes_proton_layout(self):
        report = convert(FakeReader([folder(("Posteingang",), message(subject="A"), message(subject="B"))]),
                         self.dest)
        self.assertEqual(report.imported, 2)
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "labels.json")))
        metas = self.metas()
        self.assertEqual(len(metas), 2)
        for mid in metas:
            self.assertTrue(os.path.isfile(os.path.join(self.dest, mid + ".eml")))
        self.assertEqual(len(self.eml_files()), 2)
        self.assertFalse(os.path.exists(os.path.join(self.dest, ".tmp.eml")))

        s = store.Store(os.path.join(self.tmp, "archive.db"))
        try:
            s.sync_source("s1", self.dest)
            folders = {f["key"]: f for f in s.folders(["s1"])}
            self.assertEqual(folders["sys:0"]["count"], 2)
        finally:
            s.close()

    def test_metadata_fields(self):
        cc = [Person("Carl", "carl@example.org")]
        convert(FakeReader([folder(("Posteingang",), message(read=False, cc=cc, attachments=[("a.pdf", b"x")]))]),
                self.dest)
        (mid, meta), = self.metas().items()
        self.assertEqual(set(meta), {"ID", "LabelIDs", "Subject", "Sender", "ToList", "CCList", "Time",
                                     "Unread", "NumAttachments"})
        self.assertEqual(meta["ID"], mid)
        self.assertTrue(mid.startswith("o") and len(mid) == 41)
        self.assertEqual(meta["LabelIDs"], ["0", "5"])
        self.assertEqual(meta["Subject"], "Hallo")
        self.assertEqual(meta["Sender"], {"Name": "Anna", "Address": "anna@example.org"})
        self.assertEqual(meta["ToList"], [{"Name": "Ben", "Address": "ben@example.org"}])
        self.assertEqual(meta["CCList"], [{"Name": "Carl", "Address": "carl@example.org"}])
        self.assertEqual(meta["Time"], 1700000000)
        self.assertEqual(meta["Unread"], 1)
        self.assertEqual(meta["NumAttachments"], 1)

    def test_system_folder_mapping(self):
        names = {
            "Posteingang": "0", "INBOX": "0", "Gesendete Elemente": "7", "Sent Items": "7", "Sent": "7",
            "Gelöschte Elemente": "3", "Deleted Items": "3", "Trash": "3", "Entwürfe": "8", "Drafts": "8",
            "Junk-E-Mail": "4", "Junk E-Mail": "4", "Junk email": "4", "Spam": "4",
            "Archive": "6", "Starred": "10",
        }
        folders = [folder((name,), message(subject=name)) for name in names]
        folders.append(folder(("Posteingang", "Sub"), message(subject="sub")))
        folders.append(folder(("Projekte", "Inbox"), message(subject="nested")))
        convert(FakeReader(folders), self.dest)
        by_subject = {m["Subject"]: m["LabelIDs"] for m in self.metas().values()}
        for name, sys_id in names.items():
            self.assertIn(sys_id, by_subject[name], name)
            self.assertEqual(by_subject[name][0], sys_id)
        self.assertEqual(by_subject["sub"][0], label_id("Posteingang/Sub"))
        self.assertNotIn("0", by_subject["sub"])
        self.assertEqual(by_subject["nested"][0], label_id("Projekte/Inbox"))
        labels = self.labels()
        self.assertEqual(labels["0"]["Name"], "Inbox")
        self.assertEqual(labels["7"]["Name"], "Sent")
        self.assertEqual(labels["10"]["Name"], "Starred")
        self.assertEqual(labels["5"]["Name"], "All Mail")
        sub = labels[label_id("Posteingang/Sub")]
        self.assertEqual(sub["Path"], "Posteingang/Sub")
        self.assertEqual(sub["Name"], "Sub")
        self.assertEqual(sub["Type"], 3)
        self.assertEqual(sub["Color"], "")

    def test_slash_in_folder_name(self):
        convert(FakeReader([folder(("Rechnungen 2015/16",), message())]), self.dest)
        labels = self.labels()
        custom = labels[label_id("Rechnungen 2015-16")]
        self.assertEqual(custom["Path"], "Rechnungen 2015-16")
        self.assertEqual(custom["Name"], "Rechnungen 2015-16")

    def test_folders_dissolved_when_empty(self):
        folders = [
            folder(("Folders",)),
            folder(("Folders", "Chor Sennestadt"), message(subject="a")),
            folder(("Folders", "Chor Sennestadt", "Kleingruppe"), message(subject="b")),
            folder(("Folders", "Posteingang"), message(subject="c")),
        ]
        convert(FakeReader(folders), self.dest)
        labels = self.labels()
        self.assertEqual(labels[label_id("Chor Sennestadt")]["Path"], "Chor Sennestadt")
        self.assertEqual(labels[label_id("Chor Sennestadt/Kleingruppe")]["Path"], "Chor Sennestadt/Kleingruppe")
        self.assertFalse(any(x["Path"].startswith("Folders") for x in labels.values()))
        by_subject = {m["Subject"]: m["LabelIDs"] for m in self.metas().values()}
        self.assertEqual(by_subject["c"][0], "0")

    def test_folders_with_own_mails_are_kept(self):
        folders = [folder(("Folders",), message(subject="a")), folder(("Folders", "X"), message(subject="b"))]
        convert(FakeReader(folders), self.dest)
        self.assertIn(label_id("Folders"), self.labels())
        self.assertIn(label_id("Folders/X"), self.labels())

    def test_merges_copies_by_message_id(self):
        headers = "Message-ID: <same@example.org>\r\n"
        folders = [folder(("Posteingang",), message(headers=headers)),
                   folder(("Projekte",), message(headers=headers))]
        report = convert(FakeReader(folders), self.dest)
        self.assertEqual(report.imported, 1)
        self.assertEqual(report.merged, 1)
        self.assertEqual(len(self.eml_files()), 1)
        (meta,) = self.metas().values()
        self.assertTrue({"0", label_id("Projekte"), "5"} <= set(meta["LabelIDs"]))
        self.assertEqual(len(meta["LabelIDs"]), len(set(meta["LabelIDs"])))

    def test_same_input_same_ids(self):
        other = os.path.join(self.tmp, "other")
        os.mkdir(other)
        folders = [folder(("Posteingang",), message(subject="A", attachments=[("a.pdf", b"%PDF")])),
                   folder(("Projekte",), message(subject="B"))]
        convert(FakeReader(folders), self.dest)
        convert(FakeReader(folders), other)
        self.assertEqual(self.eml_files(), self.eml_files(other))
        self.assertEqual(sorted(self.metas()), sorted(self.metas(other)))

    def test_skips_non_mail(self):
        folders = [
            folder(("Posteingang",), message(subject="smime", message_class="IPM.Note.SMIME"),
                   message(subject="meeting", message_class="IPM.Schedule.Meeting.Request"),
                   message(subject="plain", message_class="")),
            folder(("Kontakte",), message(subject="c", message_class="IPM.Contact")),
            folder(("Kalender",), message(subject="t", message_class="IPM.Appointment")),
        ]
        report = convert(FakeReader(folders), self.dest)
        self.assertEqual(report.imported, 3)
        self.assertEqual(report.skipped, {"IPM.Contact": 1, "IPM.Appointment": 1})
        self.assertEqual(len(self.eml_files()), 3)

    def test_reports_unreadable_and_failed_folders(self):
        def half_broken():
            yield Unreadable("kaputt")
            yield message(subject="vor dem Fehler")
            raise OSError("defekt")

        folders = [
            folder(("Broken",), error="nicht offen"),
            Folder(path=("Halb",), count=3, items=half_broken),
        ]
        report = convert(FakeReader(folders), self.dest)
        self.assertEqual(report.imported, 1)
        self.assertEqual(report.unreadable, 2)
        self.assertEqual(report.failed_folders, ["Broken", "Halb"])

    def test_progress(self):
        folders = [folder(("Posteingang",), message(subject="A"), message(subject="B")),
                   folder(("Projekte",), Unreadable("kaputt"), message(subject="C"))]
        calls: list[tuple[int, int]] = []
        convert(FakeReader(folders), self.dest, progress=lambda done, total: calls.append((done, total)))
        self.assertGreaterEqual(len(calls), 4)
        self.assertEqual(calls[-1], (4, 4))

    def test_lone_surrogates_do_not_raise(self):
        bad = "\udcff"
        cases = {
            "plain": message(plain=f"Text {bad}"),
            "html": message(html=f"<p>{bad}</p>", plain=None),
            "subject": message(subject=f"Betreff {bad}"),
            "sender name": message(sender=Person(f"Anna {bad}", "anna@example.org")),
            "sender address": message(sender=Person("Anna", f"anna{bad}@example.org")),
            "to name": message(to=[Person(f"Ben {bad}", "ben@example.org")]),
            "cc name": message(cc=[Person(f"Carl {bad}", "carl@example.org")]),
            "attachment name": message(attachments=[(f"datei{bad}.txt", b"x")]),
            "transport headers": message(headers=f"Subject: a {bad}\r\nX-Foo: {bad}\r\n"),
        }
        for label, msg in cases.items():
            with self.subTest(place=label):
                parsed = parse(build_eml(msg))
                self.assertIsNotNone(parsed.get_content_type())

    def test_eml_attachment_loads_in_server(self):
        inner = b"From: a@x\r\nSubject: innen\r\n\r\nHallo\r\n"
        parsed = parse(build_eml(message(attachments=[("weiter.eml", inner)])))
        self.assertEqual(attachments_of(parsed), [("weiter.eml", inner)])
        with open(os.path.join(self.tmp, "x.eml"), "wb") as f:
            f.write(build_eml(message(attachments=[("weiter.eml", inner)])))
        out = mailparse.load_message(self.tmp, "x", {})
        self.assertIsNone(out["error"])
        self.assertEqual([a["name"] for a in out["attachments"]], ["weiter.eml"])

    def test_odd_transport_headers_through_build_eml(self):
        headers = (
            "From: Müller <mueller@example.org>\r\n"
            "Subject: Grüße aus\r\n"
            " dem Büro\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "Content-Type: text/html\r\n"
        )
        data = build_eml(message(headers=headers))
        with open(os.path.join(self.tmp, "x.eml"), "wb") as f:
            f.write(data)
        out = mailparse.load_message(self.tmp, "x", {})
        self.assertIsNone(out["error"])
        self.assertIn("Text", out["text"])
        self.assertEqual(len(parse(data).get_all("Content-Type")), 1)


class FakeFolder:
    """Stands in for a pypff folder: sub-folder count can be made to raise."""

    def __init__(self, name, messages=(), subs=(), broken_subs=False):
        self.name = name
        self._messages = list(messages)
        self._subs = list(subs)
        self._broken_subs = broken_subs

    @property
    def number_of_sub_messages(self):
        return len(self._messages)

    @property
    def number_of_sub_folders(self):
        if self._broken_subs:
            raise OSError("Unterordner nicht lesbar")
        return len(self._subs)

    def get_sub_folder(self, index):
        return self._subs[index]

    def get_sub_message(self, index):
        return self._messages[index]


class PypffTest(unittest.TestCase):
    def test_unreadable_subfolder_count_is_reported(self):
        top = FakeFolder(None, subs=[
            FakeFolder("Posteingang"),
            FakeFolder("Archiv", broken_subs=True),
        ])
        reader = object.__new__(PypffReader)
        reader._top = top
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        dest = os.path.join(tmp, "out")
        os.mkdir(dest)
        report = convert(reader, dest)
        self.assertEqual(report.failed_folders, ["Archiv/(Unterordner)"])

    def test_open_reader_without_pypff(self):
        with mock.patch.dict(sys.modules, {"pypff": None}):
            with self.assertRaises(ImportUnavailable) as ctx:
                open_reader("x.pst")
            self.assertEqual(str(ctx.exception), INSTALL_HINT)
            self.assertFalse(pypff_available())

    def test_decode_html_uses_meta_charset(self):
        raw = '<html><head><meta charset="windows-1252"></head><p>Grüße</p></html>'.encode("cp1252")
        self.assertIn("Grüße", _decode_body(raw, None, html=True))

    def test_decode_text_uses_codepage(self):
        self.assertEqual(_decode_body("Grüße".encode("cp1252"), 1252, html=False), "Grüße")

    def test_decode_unknown_codepage_falls_back_to_utf8(self):
        self.assertEqual(_decode_body("Grüße".encode("utf-8"), 99999, html=False), "Grüße")
        self.assertEqual(_decode_body(b"\xff ok", None, html=False), "� ok")


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@unittest.skipUnless(os.environ.get("MBV_TEST_PST") and pypff_available(),
                     "needs MBV_TEST_PST and the pypff module")
class TestPypff(unittest.TestCase):
    def test_real_file(self):
        source = os.environ["MBV_TEST_PST"]
        before = file_sha256(source)
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "out")
            os.mkdir(dest)
            reader = open_reader(source)
            try:
                report = convert(reader, dest)
            finally:
                reader.close()
            self.assertEqual(file_sha256(source), before, "source file was modified")
            self.assertGreater(report.imported, 0)
            emls = [n[: -len(".eml")] for n in os.listdir(dest) if n.endswith(".eml")]
            self.assertEqual(len(emls), report.imported)
            for mid in emls:
                self.assertIsNone(mailparse.load_message(dest, mid, {})["error"])


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_install_hint(env_value: str | None) -> str:
    """Import INSTALL_HINT in a fresh interpreter with the given env value (None = unset)."""
    env = dict(os.environ)
    env.pop("MAILVIEWER_INSTALL_HINT", None)
    if env_value is not None:
        env["MAILVIEWER_INSTALL_HINT"] = env_value
    result = subprocess.run(
        [sys.executable, "-c", "from mailviewer.outlook import INSTALL_HINT; print(INSTALL_HINT)"],
        env=env, cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )
    return result.stdout.rstrip("\n")


class InstallHintTest(unittest.TestCase):
    def test_install_hint_default_mentions_pip(self):
        self.assertIn("libpff-python", read_install_hint(None))

    def test_install_hint_overridable_by_env(self):
        self.assertEqual(read_install_hint("Eigener Hinweis"), "Eigener Hinweis")

    def test_install_hint_empty_env_falls_back_to_default(self):
        self.assertIn("libpff-python", read_install_hint(""))


if __name__ == "__main__":
    unittest.main()
