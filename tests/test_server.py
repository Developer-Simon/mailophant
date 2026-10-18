"""Tests for server.py. Uses only synthetic fixtures in a temp directory."""

import base64
import contextlib
import http.client
import io
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from unittest import mock
from urllib.parse import quote, urlencode

from mailviewer.i18n import raw
from mailviewer import browse, config, i18n, exports, mailparse, server, validation
from mailviewer.cleanup import CleanupResult
from mailviewer.library import LIBRARY_FILE, Library
from mailviewer.outlook import ImportReport, Unreadable
from mailviewer.store import Store
from tests.test_outlook import FakeReader, folder
from mailviewer.transfer import TransferResult
from tests.test_outlook import message as outlook_message

PDF = b"%PDF-1.4 synthetic fixture\n%%EOF"
PNG = bytes(range(64))
ICON_PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(64))  # carries the PNG signature the upload checks

ID_PLAIN = "id1_AAAA"
ID_ALT = "id2-BBBB=="
ID_RICH = "id3_CCCC"
ID_GHOST = "id4=DDDD"  # metadata only, .eml missing


def texts(log):
    """The English text of each job log entry."""
    return [e["text"] for e in log]


def codes(log):
    return [e.get("code") for e in log]


def add_headers(msg, subject, sender, to, cc=()):
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(sender)
    msg["To"] = ", ".join(formataddr(p) for p in to)
    if cc:
        msg["Cc"] = ", ".join(formataddr(p) for p in cc)
    return msg


def plain_eml() -> bytes:
    msg = MIMEText("Grüße aus Köln, Ärger über Öl.\nZeile zwei.", "plain", "utf-8")
    add_headers(msg, "Grüße aus Köln", ("Jürgen Müller", "juergen@example.test"),
                [("Ann", "ann@example.test")], cc=[("Carl", "carl@example.test")])
    return msg.as_bytes()


def alt_eml() -> bytes:
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText("Plain part: Hallo Welt", "plain", "utf-8"))
    alt.attach(MIMEText("<html><body><p>HTML part: <b>Hallo</b></p></body></html>", "html", "utf-8"))
    add_headers(alt, "Multi alt", ("Sam Sender", "sam@example.test"), [("Ann", "ann@example.test")])
    return alt.as_bytes()


def rich_eml() -> bytes:
    related = MIMEMultipart("related")
    body = MIMEMultipart("alternative")
    body.attach(MIMEText("Text body mit Logo", "plain", "utf-8"))
    body.attach(MIMEText('<p><img src="cid:logo@x"></p><p>HTML Ünïcode</p>', "html", "utf-8"))
    related.attach(body)
    img = MIMEImage(PNG, "png")
    img.add_header("Content-ID", "<logo@x>")
    img.add_header("Content-Disposition", "inline", filename="logo.png")
    related.attach(img)
    mixed = MIMEMultipart("mixed")
    mixed.attach(related)
    pdf = MIMEApplication(PDF, "pdf")
    pdf.add_header("Content-Disposition", "attachment", filename=("utf-8", "", "Rechnung ü.pdf"))
    mixed.attach(pdf)
    add_headers(mixed, "Rechnung", ("Pia Post", "pia@example.test"), [("Bob", "bob@example.test")])
    return mixed.as_bytes()


def meta(mid, subject, sender, to, time, labels, unread=0, attachments=0, cc=()):
    def person(n, a):
        return {"Name": n, "Address": a}
    return {"Version": 1, "Payload": {
        "ID": mid, "LabelIDs": labels, "Subject": subject,
        "Sender": person(*sender), "ToList": [person(*p) for p in to],
        "CCList": [person(*p) for p in cc], "Time": time, "Unread": unread,
        "NumAttachments": attachments,
    }}


LABELS = {"Version": 1, "Payload": [
    {"Path": "Inbox", "ID": "0", "ParentID": None, "Name": "Inbox", "Color": "#111", "Type": 3},
    {"Path": "Spam", "ID": "3", "ParentID": None, "Name": "Spam", "Color": "#222", "Type": 3},
    {"Path": "Sent", "ID": "7", "ParentID": None, "Name": "Sent", "Color": "#333", "Type": 3},
    {"Path": "All Mail", "ID": "5", "ParentID": None, "Name": "All Mail", "Color": "#444", "Type": 3},
    {"Path": "Projects", "ID": "X1", "ParentID": None, "Name": "Projects", "Color": "#555", "Type": 1},
    {"Path": "beta", "ID": "X3", "ParentID": None, "Name": "beta", "Color": "#666", "Type": 1},
    {"Path": "Empty", "ID": "X2", "ParentID": None, "Name": "Empty", "Color": "#777", "Type": 1},
]}


def write(path, data):
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode, **({} if mode == "wb" else {"encoding": "utf-8"})) as f:
        f.write(data)


def write_json(path, obj):
    write(path, json.dumps(obj, ensure_ascii=False))


def make_export(root: str, name: str = "export") -> str:
    export = os.path.join(root, name)
    os.makedirs(export)
    write_json(os.path.join(export, "labels.json"), LABELS)
    mails = {
        ID_PLAIN: (plain_eml(), meta(ID_PLAIN, "Grüße aus Köln", ("Jürgen Müller", "juergen@example.test"),
                                     [("Ann", "ann@example.test")], 1700000300, ["0", "5", "7"],
                                     unread=1, cc=[("Carl", "carl@example.test")])),
        ID_ALT: (alt_eml(), meta(ID_ALT, "Multi alt", ("Sam Sender", "sam@example.test"),
                                 [("Ann", "ann@example.test")], 1700000200, ["5", "X1", "X3"])),
        ID_RICH: (rich_eml(), meta(ID_RICH, "Rechnung", ("Pia Post", "pia@example.test"),
                                   [("Bob", "bob@example.test")], 1700000100, ["0"], unread=1, attachments=1)),
        ID_GHOST: (None, meta(ID_GHOST, "Ghost", ("Gus Geist", "gus@example.test"),
                              [("Ann", "ann@example.test")], 1700000050, ["0", "X1"])),
    }
    for mid, (eml, m) in mails.items():
        if eml is not None:
            write(os.path.join(export, mid + ".eml"), eml)
        write_json(os.path.join(export, mid + ".metadata.json"), m)
    write(os.path.join(export, "broken_EEEE.metadata.json"), "{not json")
    return export


class Base(unittest.TestCase):
    """One library with the synthetic export as source s1, served for the whole class."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.export = make_export(cls.tmp)
        cls.static = os.path.join(cls.tmp, "static")
        os.makedirs(cls.static)
        write(os.path.join(cls.static, "index.html"), "<!doctype html><title>synthetic</title>")
        os.makedirs(os.path.join(cls.static, "css"))
        os.makedirs(os.path.join(cls.static, "js"))
        write(os.path.join(cls.static, "css", "app.css"), "body{}")
        write(os.path.join(cls.static, "js", "app.js"), "void 0")
        library = make_library(cls.tmp, cls.export)
        config = os.path.join(cls.tmp, "config.json")
        with contextlib.redirect_stderr(io.StringIO()):  # the index job logs the broken metadata file
            cls.srv = server.make_server(library, 0, cls.static, config_path=config)
            cls.port = cls.srv.server_port
            cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
            cls.thread.start()
            wait_job(cls.port)

    def setUp(self):
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))  # silences the HTTP access log

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.srv.viewer.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host if host is not None else f"127.0.0.1:{self.port}")
        conn.endheaders()
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, resp.headers, body

    def get_json(self, path):
        status, _, body = self.get(path)
        return status, json.loads(body)

    def detail(self, mid):
        status, m = self.get_json(f"/api/message/s1/{quote(mid, safe='')}")
        self.assertEqual(status, 200, mid)
        return m


class SourceTests(Base):
    def test_sources_listing(self):
        status, data = self.get_json("/api/sources")
        self.assertEqual(status, 200)
        self.assertEqual(len(data), 1)
        src = data[0]
        self.assertEqual(set(src), {"id", "name", "kind", "icon", "hidden", "available",
                                    "fetchable", "count", "unread", "archive", "archive_target", "auto_paused"})
        self.assertEqual((src["id"], src["kind"], src["available"], src["count"], src["unread"]),
                         ("s1", "maildir", True, 4, 2))
        self.assertFalse(src["fetchable"])  # maildirs are never fetched


class FolderTests(Base):
    def test_http_folders(self):
        status, folders = self.get_json("/api/folders")
        self.assertEqual(status, 200)
        self.assertEqual(set(folders[0]), {"key", "name", "path", "color", "count", "unread", "order", "labels"})
        # system folders first in fixed order, then the rest by name; empty labels omitted
        self.assertEqual([(f["key"], f["count"], f["unread"]) for f in folders],
                         [("sys:0", 3, 2), ("sys:7", 1, 1), ("sys:5", 2, 1), ("path:beta", 1, 0),
                          ("path:projects", 2, 0)])

    def test_unknown_source_400(self):
        status, data = self.get_json("/api/folders?sources=nope")
        self.assertEqual((status, data["code"]), (400, "err.unknown_source"))


class ListTests(Base):
    def ids(self, **params):
        status, data = self.get_json("/api/messages?" + urlencode({"sources": "s1", **params}))
        self.assertEqual(status, 200)
        return data["total"], [m["id"] for m in data["messages"]]

    def test_all_sorted_by_time_desc(self):
        self.assertEqual(self.ids(), (4, [ID_PLAIN, ID_ALT, ID_RICH, ID_GHOST]))

    def test_default_sources_are_all_visible(self):
        status, data = self.get_json("/api/messages")
        self.assertEqual((status, data["total"]), (200, 4))

    def test_folder_filter(self):
        self.assertEqual(self.ids(folder="sys:0"), (3, [ID_PLAIN, ID_RICH, ID_GHOST]))

    def test_search_umlaut_case_insensitive(self):
        self.assertEqual(self.ids(q="MÜLLER"), (1, [ID_PLAIN]))

    def test_search_sharp_s_found(self):
        self.assertEqual(self.ids(q="Grüße"), (1, [ID_PLAIN]))

    def test_search_to_address(self):
        self.assertEqual(self.ids(q="bob@example.TEST"), (1, [ID_RICH]))

    def test_search_subject(self):
        self.assertEqual(self.ids(q="ghost"), (1, [ID_GHOST]))

    def test_pagination(self):
        self.assertEqual(self.ids(offset=0, limit=2), (4, [ID_PLAIN, ID_ALT]))
        self.assertEqual(self.ids(offset=2, limit=2), (4, [ID_RICH, ID_GHOST]))

    def test_summary_fields(self):
        status, data = self.get_json("/api/messages?sources=s1")
        msgs = {m["id"]: m for m in data["messages"]}
        self.assertTrue(msgs[ID_PLAIN]["unread"])
        self.assertFalse(msgs[ID_ALT]["unread"])
        self.assertEqual(msgs[ID_RICH]["attachments"], 1)
        self.assertEqual(msgs[ID_RICH]["source"], "s1")
        self.assertEqual(msgs[ID_PLAIN]["sender"], {"name": "Jürgen Müller", "address": "juergen@example.test"})

    def test_http_query_params(self):
        status, data = self.get_json("/api/messages?folder=sys:0&q=" + quote("MÜLLER") + "&limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["messages"][0]["id"], ID_PLAIN)
        status, data = self.get_json("/api/messages?limit=2&offset=abc")
        self.assertEqual(len(data["messages"]), 2)
        self.assertEqual(data["total"], 4)
        status, data = self.get_json("/api/messages?limit=99999")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["messages"]), 4)


class DetailTests(Base):
    def test_plain_with_umlauts(self):
        m = self.detail(ID_PLAIN)
        self.assertIsNone(m["error"])
        self.assertIn("Grüße aus Köln", m["text"])
        self.assertIsNone(m["html"])
        self.assertEqual(m["attachments"], [])
        self.assertEqual(m["from"]["name"], "Jürgen Müller")
        self.assertEqual(m["cc"], [{"name": "Carl", "address": "carl@example.test"}])
        self.assertEqual(m["to"], [{"name": "Ann", "address": "ann@example.test"}])

    def test_multipart_alternative(self):
        m = self.detail(ID_ALT)
        self.assertIn("Plain part: Hallo Welt", m["text"])
        self.assertIn("<b>Hallo</b>", m["html"])

    def test_cid_rewritten_and_attachment_listed(self):
        m = self.detail(ID_RICH)
        self.assertIn("data:image/png;base64," + base64.b64encode(PNG).decode(), m["html"])
        self.assertNotIn("cid:", m["html"])
        self.assertIn("HTML Ünïcode", m["html"])
        self.assertEqual(m["attachments"], [{
            "index": 0, "name": "Rechnung ü.pdf", "size": len(PDF), "mime": "application/pdf"}])

    def test_error_mail_without_eml(self):
        m = self.detail(ID_GHOST)
        self.assertEqual(m["error"], "message file (.eml) not found")
        self.assertEqual((m["text"], m["html"], m["attachments"]), (None, None, []))
        self.assertEqual(m["subject"], "Ghost")
        self.assertEqual(m["from"]["address"], "gus@example.test")

    def test_unknown_message_404(self):
        status, data = self.get_json("/api/message/s1/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", data)


class AttachmentTests(Base):
    def test_download(self):
        status, headers, body = self.get(f"/api/message/s1/{ID_RICH}/attachment/0")
        self.assertEqual(status, 200)
        self.assertEqual(body, PDF)
        self.assertEqual(headers["Content-Type"], "application/pdf")
        self.assertIn("filename*=UTF-8''Rechnung%20%C3%BC.pdf", headers["Content-Disposition"])

    def test_inline_preview_for_pdf(self):
        status, headers, body = self.get(f"/api/message/s1/{ID_RICH}/attachment/0?inline=1")
        self.assertEqual(status, 200)
        self.assertEqual(body, PDF)
        self.assertEqual(headers["Content-Type"], "application/pdf")
        self.assertTrue(headers["Content-Disposition"].startswith("inline;"))
        self.assertIn("filename*=UTF-8''Rechnung%20%C3%BC.pdf", headers["Content-Disposition"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_preview_headers_only_for_safe_types(self):
        for ctype in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            disposition, extra = mailparse.attachment_headers(ctype, "x", inline=True)
            self.assertTrue(disposition.startswith("inline;"), ctype)
            self.assertIn("sandbox", dict(extra)["Content-Security-Policy"], ctype)
        self.assertTrue(mailparse.attachment_headers("application/pdf", "x", inline=True)[0].startswith("inline;"))
        for ctype in ("image/svg+xml", "text/html", "application/xhtml+xml", "text/xml", "application/octet-stream"):
            disposition, _ = mailparse.attachment_headers(ctype, "x", inline=True)
            self.assertTrue(disposition.startswith("attachment;"), ctype)
        self.assertTrue(mailparse.attachment_headers("application/pdf", "x", inline=False)[0].startswith("attachment;"))

    def test_mp3_preview(self):
        self.assertEqual(mailparse.preview_type("audio/mpeg"), "audio/mpeg")
        self.assertEqual(mailparse.preview_type("audio/mp3"), "audio/mpeg")
        self.assertIsNone(mailparse.preview_type("audio/x-ms-wma"))
        disposition, extra = mailparse.attachment_headers("audio/mpeg", "x", inline=True)
        self.assertTrue(disposition.startswith("inline;"))
        self.assertNotIn("Content-Security-Policy", dict(extra))  # the media player does not work sandboxed

    def test_bad_index_and_missing_eml_404(self):
        for path in (f"/api/message/s1/{ID_RICH}/attachment/1",
                     f"/api/message/s1/{ID_RICH}/attachment/abc",
                     f"/api/message/s1/{quote(ID_GHOST, safe='')}/attachment/0"):
            status, data = self.get_json(path)
            self.assertEqual(status, 404, path)
            self.assertIn("error", data)


class SecurityTests(Base):
    def assert_404(self, path):
        status, _, body = self.get(path)
        self.assertEqual(status, 404, path)
        self.assertIn("error", json.loads(body))

    def test_path_traversal_ids_rejected(self):
        self.assert_404("/api/message/s1/" + quote("../labels.json", safe=""))
        self.assert_404("/api/message/s1/..%2F..%2Fetc")
        self.assert_404("/api/message/s1/%2e%2e%2flabels.json")
        self.assert_404("/api/message/s1/..%2Flabels.json/attachment/0")
        self.assert_404("/api/message/s1/../labels.json")
        self.assert_404("/api/message/../labels.json")
        self.assert_404("/api/message/..%2Fs1/" + ID_PLAIN)
        self.assert_404("/api/message/%2e%2e%2fs1/" + ID_PLAIN)
        self.assert_404("/api/message/..%2Fs1/" + ID_PLAIN + "/attachment/0")
        self.assert_404("/api/message/s9/" + ID_PLAIN)  # unknown source

    def test_unknown_source_400(self):
        status, _, body = self.get("/api/messages?sources=nope")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "err.unknown_source")

    def test_unknown_route_404(self):
        self.assert_404("/api/unknown")

    def test_wrong_host_403(self):
        status, _, body = self.get("/api/folders", host="evil.example:%d" % self.port)
        self.assertEqual(status, 403)
        self.assertIn("error", json.loads(body))
        status, _, _ = self.get("/api/folders", host="")
        self.assertEqual(status, 403)

    def test_localhost_host_allowed(self):
        status, _, _ = self.get("/api/folders", host=f"localhost:{self.port}")
        self.assertEqual(status, 200)

    def test_nosniff_on_all_responses(self):
        for path in ("/", "/api/folders", "/api/message/s1/nope"):
            _, headers, _ = self.get(path)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff", path)

    def test_index_page_served(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"synthetic", body)

    def test_static_assets_served_with_type(self):
        for path, ctype in (("/static/css/app.css", "text/css"), ("/static/js/app.js", "text/javascript")):
            status, headers, _ = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertIn(ctype, headers["Content-Type"])

    def test_static_rejects_other_paths(self):
        for path in ("/static/../server.py", "/static/js/../../x.js", "/static/js/missing.js", "/static/other/app.js"):
            status, _, _ = self.get(path)
            self.assertEqual(status, 404, path)

    def test_bound_to_loopback(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")



ACCOUNT = "simon.test@example.test"
OLD = "mail_20260101T000000"
NEW = "mail_20261015T120000"
CLI_NAME = "proton-mail-export-cli"
PW = "Hunter2-PW"
MB = "mailbox-Pass9"
TOTP = "481516"
FULL = {"ET_USER_MAILBOX_PASSWORD": MB, "ET_TOTP_CODE": TOTP}
ABSENT = {"ET_USER_MAILBOX_PASSWORD": None, "ET_TOTP_CODE": None}

# Stand-in for the Proton CLI: checks env and argv, prints its secrets, writes a mail dir.
FAKE_BODY = '''
import json, os, shutil, sys, time
from urllib.parse import quote

env = os.environ
if any(PASSWORD in arg for arg in sys.argv):  # secrets must never be on argv
    sys.exit(9)
if env.get("ET_OPERATION") != "backup" or env.get("ET_TELEMETRY_OFF") != "1":
    sys.exit(8)
for key, want in EXPECT.items():  # None means the variable must be absent
    if env.get(key) != want:
        sys.exit(7)
print("login", env["ET_USER_EMAIL"], *SECRETS, flush=True)
pw = env["ET_USER_PASSWORD"]
print("echo", pw, quote(pw, safe=""), json.dumps(pw)[1:-1], flush=True)
if MODE == "fail":
    print("login failed", file=sys.stderr)
    sys.exit(3)
if MODE == "sleep":
    time.sleep(1)
if MODE in ("ok", "sleep", "dup"):
    # like CLI 1.0.7: a backup ignores ET_DIR and asks on stdin unless --dir is given
    if len(sys.argv) != 3 or sys.argv[1] != "--dir":
        sys.exit(6)
    out = os.path.join(sys.argv[2], env["ET_USER_EMAIL"], "mail_20261015T120000")
    os.makedirs(out)
    labels = {"Payload": [{"ID": "0", "Name": "Inbox", "Path": "Inbox", "Color": "#111", "Type": 3}]}
    meta = {"Version": 1, "Payload": {"ID": "new_FFFF", "LabelIDs": ["0"], "Subject": "Frisch",
            "Sender": {"Name": "Neu", "Address": "neu@example.test"}, "ToList": [],
            "Time": 1800000000, "Unread": 1, "NumAttachments": 0}}
    with open(os.path.join(out, "labels.json"), "w", encoding="utf-8") as f:
        json.dump(labels, f)
    with open(os.path.join(out, "new_FFFF.metadata.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    with open(os.path.join(out, "new_FFFF.eml"), "w", encoding="utf-8") as f:
        f.write("Subject: Frisch\\n\\nHallo\\n")
        if MODE == "dup":  # unchanged mails, copied from the previous export
            for name in DUP_FILES:
                shutil.copy(os.path.join(DUP_FROM, name), out)
'''


def write_fake_cli(path: str, mode: str, expect: dict, dup_from: str | None = None) -> None:
    """mode: ok | sleep (ok after 1s) | dup (ok, plus unchanged mails copied from dup_from)
    | fail (exit 3) | empty (exit 0, no output)."""
    secrets = [s for s in (PW, expect.get("ET_USER_MAILBOX_PASSWORD"), expect.get("ET_TOTP_CODE")) if s]
    header = (f"#!/usr/bin/env python3\nPASSWORD = {PW!r}\nSECRETS = {secrets!r}\n"
              f"EXPECT = {expect!r}\nMODE = {mode!r}\nDUP_FROM = {dup_from!r}\n"
              f"DUP_FILES = {[ID_PLAIN + '.eml', ID_PLAIN + '.metadata.json']!r}\n")
    write(path, header + FAKE_BODY)
    os.chmod(path, 0o755)


def request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    hdrs = {"Host": f"127.0.0.1:{port}", **(headers or {})}
    conn.request(method, path, body=body, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, json.loads(data)


def request_raw(port, method, path, body=b"", headers=None):
    """Like request, for responses that are not JSON: returns (status, headers, bytes)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, resp.headers, data


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_newest_mail_dir_with_labels_wins(self):
        account = os.path.join(self.tmp, "acct@x.test")
        make_export(account, "mail_20250101T000000")
        make_export(account, "mail_20260101T000000")
        os.makedirs(os.path.join(account, "mail_20270101T000000"))  # no labels.json: skipped
        os.makedirs(os.path.join(account, ".fetch-abc"))
        self.assertEqual(exports.resolve_mail_dir(account), os.path.join(account, "mail_20260101T000000"))

    def test_mail_dir_used_as_is(self):
        mail = make_export(self.tmp, "mail_20260101T000000")
        self.assertEqual(exports.resolve_mail_dir(mail), mail)
        self.assertEqual(exports.account_dir_of(mail), self.tmp)
        plain = make_export(self.tmp)  # not named mail_*: the dir is its own account dir
        self.assertEqual(exports.account_dir_of(plain), plain)

    def test_account_dir_of_account(self):
        account = os.path.join(self.tmp, "acct@x.test")
        self.assertEqual(exports.account_dir_of(account), account)

    def test_no_export_raises(self):
        with self.assertRaises(ValueError):
            exports.resolve_mail_dir(self.tmp)
        with self.assertRaises(ValueError):
            exports.resolve_mail_dir(os.path.join(self.tmp, "missing"))

    def test_find_cli_levels(self):
        root = self.tmp
        deep = os.path.join(root, "a", "b", "c", "d", "acct@x")
        near = os.path.join(root, "a", "b", "acct@x")
        os.makedirs(os.path.join(root, "a", "b"))
        cli = os.path.join(root, CLI_NAME)
        write(cli, "#!/bin/sh\n")
        os.chmod(cli, 0o755)
        write(os.path.join(root, "a", "b", CLI_NAME), "not executable")  # skipped
        self.assertEqual(exports.find_cli(near), cli)
        self.assertIsNone(exports.find_cli(deep))

    def test_free_name(self):
        os.makedirs(os.path.join(self.tmp, "mail_1"))
        os.makedirs(os.path.join(self.tmp, "mail_1_1"))
        self.assertEqual(server.free_name(self.tmp, "mail_1"), "mail_1_2")
        self.assertEqual(server.free_name(self.tmp, "mail_2"), "mail_2")

    def test_redact(self):
        self.assertEqual(exports.redact("a pw b pw", ["pw"]), "a *** b ***")
        pw = 'p@ss "w/d'
        text = f"{pw} | {quote(pw, safe='')} | {json.dumps(pw)[1:-1]}"
        self.assertEqual(exports.redact(text, [pw]), "*** | *** | ***")
        self.assertEqual(exports.redact("abc", ["", None]), "abc")
        self.assertEqual(exports.redact("xabcx", ["abc", "xabcx"]), "***")

    def test_cli_env_only_sets_nonempty_secrets(self):
        with mock.patch.dict(os.environ, {"ET_TOTP_CODE": "1", "ET_USER_MAILBOX_PASSWORD": "old"}):
            env = exports.cli_env("a@b.test", "/t", PW, "", "")
        self.assertNotIn("ET_TOTP_CODE", env)
        self.assertNotIn("ET_USER_MAILBOX_PASSWORD", env)
        self.assertEqual((env["ET_OPERATION"], env["ET_DIR"], env["ET_TELEMETRY_OFF"]),
                         ("backup", "/t", "1"))
        self.assertEqual(env["ET_USER_PASSWORD"], PW)

    def test_parse_fetch_body(self):
        raw = json.dumps({"source": "s1", "password": PW, "totp": " 123 "}).encode()
        self.assertEqual(validation.parse_fetch_body(raw), ("s1", "", PW, "", "123"))
        for bad, reason in ((b"{x", "invalid JSON body"), (b"[]", "body must be a JSON object"),
                            (b'{"password": "p"}', "missing source"),
                            (b'{"source": "s1", "email": "a@b"}', "password is required"),
                            (b'{"source": "s1", "password": "p", "email": "../x"}', "invalid email")):
            with self.assertRaisesRegex(ValueError, reason):
                validation.parse_fetch_body(bad)


def snapshot(d: str) -> dict:
    """Name -> (bytes, inode, mtime) for every entry of d; used to prove a dir was not touched."""
    out = {}
    for name in os.listdir(d):
        st = os.stat(os.path.join(d, name))
        with open(os.path.join(d, name), "rb") as f:
            out[name] = (f.read(), st.st_ino, st.st_mtime_ns)
    return out


class DedupeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.prev = os.path.join(self.tmp, "prev")
        self.new = os.path.join(self.tmp, "new")
        os.makedirs(self.prev)
        os.makedirs(self.new)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def pair(self, name, prev_data, new_data=None):
        """Same filename in both dirs; new_data defaults to prev_data."""
        write(os.path.join(self.prev, name), prev_data)
        write(os.path.join(self.new, name), prev_data if new_data is None else new_data)

    def same_inode(self, name):
        return os.path.samefile(os.path.join(self.prev, name), os.path.join(self.new, name))

    def read(self, d, name):
        with open(os.path.join(d, name), "rb") as f:
            return f.read()

    def test_identical_mails_are_hardlinked(self):
        eml, meta_js = plain_eml(), b'{"Payload": {"ID": "a"}}'
        self.pair("a.eml", eml)
        self.pair("a.metadata.json", meta_js)
        self.assertEqual(exports.dedupe_with_previous(self.prev, self.new), (2, len(eml) + len(meta_js)))
        for name in ("a.eml", "a.metadata.json"):
            self.assertTrue(self.same_inode(name))
            self.assertEqual(os.stat(os.path.join(self.new, name)).st_nlink, 2)
        self.assertEqual(self.read(self.new, "a.eml"), eml)
        self.assertEqual(self.read(self.new, "a.metadata.json"), meta_js)

    def test_changed_metadata_not_linked(self):
        self.pair("a.metadata.json", b'{"Unread": 1}', b'{"Unread": 0}')  # same size, other bytes
        self.pair("b.metadata.json", b'{"Unread": 1}', b'{"Unread": 1, "LabelIDs": ["0"]}')  # other size
        self.assertEqual(exports.dedupe_with_previous(self.prev, self.new), (0, 0))
        self.assertFalse(self.same_inode("a.metadata.json"))
        self.assertFalse(self.same_inode("b.metadata.json"))
        self.assertEqual(self.read(self.new, "a.metadata.json"), b'{"Unread": 0}')

    def test_new_only_file_untouched(self):
        write(os.path.join(self.new, "c.eml"), b"new mail")
        self.assertEqual(exports.dedupe_with_previous(self.prev, self.new), (0, 0))
        self.assertEqual(os.stat(os.path.join(self.new, "c.eml")).st_nlink, 1)
        self.assertEqual(self.read(self.new, "c.eml"), b"new mail")
        self.assertFalse(os.path.exists(os.path.join(self.prev, "c.eml")))

    def test_labels_and_other_files_untouched(self):
        self.pair("labels.json", b'{"Payload": []}')
        self.pair("notes.txt", b"same bytes")
        self.assertEqual(exports.dedupe_with_previous(self.prev, self.new), (0, 0))
        self.assertFalse(self.same_inode("labels.json"))
        self.assertFalse(self.same_inode("notes.txt"))

    def test_previous_dir_not_modified(self):
        self.pair("a.eml", plain_eml())
        self.pair("a.metadata.json", b"{}")
        write(os.path.join(self.prev, "old_only.eml"), b"gone")
        before = snapshot(self.prev)
        exports.dedupe_with_previous(self.prev, self.new)
        self.assertEqual(snapshot(self.prev), before)

    def test_already_linked_not_counted_twice(self):
        eml = plain_eml()
        self.pair("a.eml", eml)
        self.assertEqual(exports.dedupe_with_previous(self.prev, self.new), (1, len(eml)))
        self.assertEqual(exports.dedupe_with_previous(self.prev, self.new), (0, 0))

    def test_oserror_on_link_skips_file_and_leaves_no_temp(self):
        eml = plain_eml()
        self.pair("a.eml", eml)
        self.pair("a.metadata.json", b"{}")
        before = sorted(os.listdir(self.new))
        with mock.patch.object(server.os, "link", side_effect=OSError("cross-device")):
            self.assertEqual(exports.dedupe_with_previous(self.prev, self.new), (0, 0))
        self.assertEqual(sorted(os.listdir(self.new)), before)
        self.assertEqual(self.read(self.new, "a.eml"), eml)
        self.assertFalse(self.same_inode("a.eml"))

    def test_oserror_on_replace_cleans_temp(self):
        eml = plain_eml()
        self.pair("a.eml", eml)
        before = sorted(os.listdir(self.new))
        with mock.patch.object(server.os, "replace", side_effect=OSError("busy")):
            self.assertEqual(exports.dedupe_with_previous(self.prev, self.new), (0, 0))
        self.assertEqual(sorted(os.listdir(self.new)), before)
        self.assertEqual(self.read(self.new, "a.eml"), eml)
        self.assertEqual(os.stat(os.path.join(self.new, "a.eml")).st_nlink, 1)

    def test_missing_previous_dir_logs_nothing(self):
        viewer = server.Viewer()
        viewer._link_previous(os.path.join(self.tmp, "gone"), self.new)
        self.assertEqual(viewer.job.status()["log"], [])


class FetchTests(unittest.TestCase):
    """Fetch per proton source through the job slot. Each test gets its own account dir and server,
    since a fetch swaps the source's index."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))  # silences the HTTP access log
        self.account = os.path.join(self.tmp, ACCOUNT)
        make_export(self.account, OLD)
        self.old_files = sorted(os.listdir(os.path.join(self.account, OLD)))
        self.srv = None

    def tearDown(self):
        try:
            if self.srv is not None:
                self.wait_fetch(("idle", "done", "error"))
                self.srv.shutdown()
                self.srv.server_close()
                self.srv.viewer.store.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def serve(self, mode="ok", expect=ABSENT, account=None, cli="fake", dup_from=None, settings=None):
        """cli="fake" writes a stand-in CLI and passes it as --cli; None lets the server use settings
        or auto-detect; a path is used as is. account is the source dir (default: the account dir)."""
        if cli == "fake":
            cli = os.path.join(self.tmp, "bin", CLI_NAME)
            os.makedirs(os.path.dirname(cli), exist_ok=True)
            write_fake_cli(cli, mode, expect, dup_from)
        library = make_library(self.tmp, account or self.account)
        if settings:
            Library.open(library).update_settings(**settings)
        config = os.path.join(self.tmp, "config.json")
        with contextlib.redirect_stderr(io.StringIO()):
            self.srv = server.make_server(library, 0, self.tmp, cli=cli, config_path=config)
        self.port = self.srv.server_port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        wait_job(self.port)  # the initial index job must finish before a fetch can start

    def get(self, path):
        return request(self.port, "GET", path)

    def post(self, body=None, origin="same", host=None, raw=None, source="s1"):
        if raw is None:
            payload = dict(body or {})
            if source is not None:
                payload.setdefault("source", source)
            raw = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Host": host or f"127.0.0.1:{self.port}"}
        if origin == "same":
            origin = f"http://127.0.0.1:{self.port}"
        if origin is not None:
            headers["Origin"] = origin
        return request(self.port, "POST", "/api/fetch", raw, headers)

    def wait_fetch(self, states=("done", "error"), timeout=10.0):
        deadline = time.monotonic() + timeout
        while True:
            _, st = self.get("/api/job")
            if st["state"] in states:
                return st
            if time.monotonic() > deadline:
                self.fail(f"fetch still {st['state']} after {timeout}s")
            time.sleep(0.02)

    def test_job_idle_after_index(self):
        self.serve()
        _, st = self.get("/api/job")
        self.assertEqual((st["state"], st["kind"], st["result"]), ("done", "index", None))

    def test_success_swaps_index(self):
        self.serve(expect=FULL)
        status, data = self.post({"password": PW, "mailbox_password": MB, "totp": TOTP})
        self.assertEqual((status, data), (202, {"state": "running"}))
        st = self.wait_fetch()
        self.assertEqual((st["state"], st["kind"]), ("done", "fetch"))
        self.assertEqual(st["result"], {"source": "s1", "export": NEW})
        self.assertIsNone(st["message"])
        self.assertIsInstance(st["started"], int)
        self.assertTrue(any(line.startswith(f"login {ACCOUNT} ") for line in texts(st["log"])), st["log"])
        _, msgs = self.get("/api/messages")
        self.assertEqual(msgs["total"], 1)
        self.assertEqual(msgs["messages"][0]["id"], "new_FFFF")
        self.assertTrue(os.path.isdir(os.path.join(self.account, NEW)))
        self.assertEqual(sorted(os.listdir(os.path.join(self.account, OLD))), self.old_files)
        self.assertEqual([n for n in os.listdir(self.account) if n.startswith(".fetch-")], [])

    def test_fetch_syncs_index_so_new_mail_is_listed(self):
        self.serve()
        self.post({"password": PW})
        self.assertEqual(self.wait_fetch()["state"], "done")
        _, msgs = self.get("/api/messages?sources=s1")
        self.assertEqual([m["id"] for m in msgs["messages"]], ["new_FFFF"])

    def test_secrets_redacted_in_status(self):
        self.serve(expect=FULL)
        self.post({"password": PW, "mailbox_password": MB, "totp": TOTP})
        st = self.wait_fetch()
        text = json.dumps(st, ensure_ascii=False)
        for secret in (PW, MB, TOTP):
            self.assertNotIn(secret, text)
        self.assertTrue(any(line.startswith("login ") and "***" in line for line in texts(st["log"])), st["log"])

    def test_failing_cli_keeps_old_index(self):
        self.serve(mode="fail")
        self.assertEqual(self.post({"password": PW})[0], 202)
        st = self.wait_fetch()
        self.assertEqual((st["state"], st["message"], st["result"]), ("error", {"text": "CLI exited with code 3"}, None))
        self.assertIn("login failed", texts(st["log"]))
        self.assertNotIn(PW, json.dumps(st))
        self.assertEqual(self.get("/api/messages")[1]["total"], 4)
        self.assertFalse(os.path.exists(os.path.join(self.account, NEW)))
        self.assertEqual([n for n in os.listdir(self.account) if n.startswith(".fetch-")], [])

    def test_cli_without_mail_output_is_error(self):
        self.serve(mode="empty")
        self.post({"password": PW})
        st = self.wait_fetch()
        self.assertEqual((st["state"], st["message"]), ("error", {"text": "CLI produced no mail export"}))
        self.assertEqual(self.get("/api/messages")[1]["total"], 4)

    def test_second_fetch_while_running_is_409(self):
        self.serve(mode="sleep")
        self.assertEqual(self.post({"password": PW})[0], 202)
        status, data = self.post({"password": PW})
        self.assertEqual(status, 409)
        self.assertIn("error", data)
        self.assertEqual(self.wait_fetch()["state"], "done")

    def test_fetch_while_index_job_running_409(self):
        self.serve()
        gate = threading.Event()
        self.assertTrue(self.srv.viewer.job.start("index", lambda j: gate.wait(5)))
        try:
            status, data = self.post({"password": PW})
            self.assertEqual(status, 409)
            self.assertIn("error", data)
        finally:
            gate.set()
        self.assertEqual(self.wait_fetch()["kind"], "index")
        self.assertEqual(self.get("/api/messages")[1]["total"], 4)
        self.assertFalse(os.path.exists(os.path.join(self.account, NEW)))

    def test_unchanged_mails_hardlinked_into_new_export(self):
        self.serve(mode="dup", dup_from=os.path.join(self.account, OLD))
        self.post({"password": PW})
        st = self.wait_fetch()
        self.assertEqual(st["state"], "done")
        old_eml = os.path.join(self.account, OLD, ID_PLAIN + ".eml")
        new_eml = os.path.join(self.account, NEW, ID_PLAIN + ".eml")
        self.assertTrue(os.path.samefile(old_eml, new_eml))
        self.assertEqual(os.stat(os.path.join(self.account, NEW, "new_FFFF.eml")).st_nlink, 1)
        self.assertTrue(any(line.startswith("Hardlinks:") for line in texts(st["log"])), st["log"])
        _, msgs = self.get("/api/messages")
        self.assertEqual(sorted(m["id"] for m in msgs["messages"]), sorted([ID_PLAIN, "new_FFFF"]))

    def test_dedupe_failure_does_not_fail_fetch(self):
        self.serve()
        with mock.patch.object(server, "dedupe_with_previous", side_effect=RuntimeError("boom")):
            self.post({"password": PW})
            st = self.wait_fetch()
        self.assertEqual((st["state"], st["result"]["export"]), ("done", NEW))
        self.assertTrue(any(line.startswith("Hardlinks") for line in texts(st["log"])), st["log"])
        self.assertEqual(self.get("/api/messages")[1]["total"], 1)

    def test_bad_origin_or_host_403(self):
        self.serve()
        cases = [
            {"origin": None},
            {"origin": "http://evil.example"},
            {"origin": f"http://127.0.0.1:{self.port + 1}"},
            {"origin": f"https://127.0.0.1:{self.port}"},
            {"host": f"evil.example:{self.port}"},
        ]
        for kwargs in cases:
            status, data = self.post({"password": PW}, **kwargs)
            self.assertEqual(status, 403, kwargs)
            self.assertIn("error", data)
        self.assertNotEqual(self.get("/api/job")[1]["kind"], "fetch")

    def test_localhost_origin_allowed(self):
        self.serve()
        status, _ = self.post({"password": PW}, origin=f"http://localhost:{self.port}")
        self.assertEqual(status, 202)
        self.assertEqual(self.wait_fetch()["state"], "done")

    def test_bad_requests_400(self):
        self.serve()
        cases = [
            ({"password": ""}, "s1"),
            ({"email": ACCOUNT}, "s1"),  # no password
            ({"password": PW}, None),  # no source
            ({"password": PW}, "nope"),  # unknown source
            ({"password": PW, "email": "../x"}, "s1"),
        ]
        for body, source in cases:
            status, data = self.post(body, source=source)
            self.assertEqual(status, 400, (body, source))
            self.assertIn("error", data)
        for raw in (b"{not json", b"[]", json.dumps({"source": "s1", "password": PW, "totp": "1" * 70000}).encode()):
            status, data = self.post(raw=raw)
            self.assertEqual(status, 400)
            self.assertIn("error", data)
        self.assertNotEqual(self.get("/api/job")[1]["kind"], "fetch")

    def test_fetch_non_proton_source_400(self):
        self.serve(account=make_export(self.tmp, "maildir"))
        status, data = self.post({"password": PW})
        self.assertEqual(status, 400)
        self.assertIn("not a proton source", data["error"])

    def test_missing_email_400_without_at_in_account_name(self):
        plain = os.path.join(self.tmp, "plain")
        make_export(plain, OLD)
        self.serve(account=plain)
        _, data = self.get("/api/sources")
        self.assertTrue(data[0]["fetchable"])
        status, data = self.post({"password": PW})
        self.assertEqual(status, 400)
        self.assertIn("missing email", data["error"])

    def test_settings_cli_and_email_used(self):
        cli = os.path.join(self.tmp, "bin", CLI_NAME)  # not near the account dir: only settings can find it
        os.makedirs(os.path.dirname(cli))
        write_fake_cli(cli, "ok", ABSENT)
        self.serve(cli=None, settings={"cli": cli, "proton_email": "other@example.test"})
        self.post({"password": PW})
        st = self.wait_fetch()
        self.assertEqual((st["state"], st["result"]), ("done", {"source": "s1", "export": NEW}))
        self.assertTrue(any(line.startswith("login other@example.test ") for line in texts(st["log"])), st["log"])

    def test_no_cli_404(self):
        self.serve(cli="/nonexistent/" + CLI_NAME)
        _, data = self.get("/api/sources")
        self.assertFalse(data[0]["fetchable"])
        status, data = self.post({"password": PW})
        self.assertEqual(status, 404)
        self.assertIn("error", data)

    def test_auto_detect_cli_next_to_account_dir(self):
        cli = os.path.join(self.tmp, CLI_NAME)  # parent of the account dir
        write(cli, "#!/bin/sh\n")
        os.chmod(cli, 0o755)
        self.serve(cli=None)
        _, data = self.get("/api/sources")
        self.assertTrue(data[0]["fetchable"])

    def test_auto_detect_none_without_cli(self):
        self.serve(cli=None)  # nothing near the account dir (the tmp dir's ancestors have no CLI)
        _, data = self.get("/api/sources")
        self.assertFalse(data[0]["fetchable"])

    def test_failed_sync_restores_previous_index(self):
        self.serve()
        real = server.Store.sync_source
        calls = []

        def flaky(store, source_id, mail_dir, progress=None):
            calls.append(mail_dir)
            result = real(store, source_id, mail_dir, progress)  # commits the new export, then fails
            if len(calls) == 1:
                raise OSError("boom")
            return result

        with mock.patch.object(server.Store, "sync_source", flaky):
            self.post({"password": PW})
            st = self.wait_fetch()
        self.assertEqual((st["state"], st["message"]), ("error", {"text": "boom"}))
        self.assertEqual(len(calls), 2)  # the new export, then the restore of the previous one
        self.assertFalse(os.path.exists(os.path.join(self.account, NEW)))
        self.assertTrue(os.path.isdir(os.path.join(self.account, OLD)))
        _, msgs = self.get("/api/messages?sources=s1")
        self.assertEqual(msgs["total"], 4)
        self.assertNotIn("new_FFFF", [m["id"] for m in msgs["messages"]])
        self.assertEqual(self.srv.viewer.store.mail_dir("s1"), os.path.join(self.account, OLD))

    def test_failed_sync_without_previous_export_drops_rows(self):
        self.serve()
        shutil.rmtree(os.path.join(self.account, OLD))  # no export left to fall back to
        real = server.Store.sync_source

        def failing(store, source_id, mail_dir, progress=None):
            real(store, source_id, mail_dir, progress)
            raise OSError("boom")

        with mock.patch.object(server.Store, "sync_source", failing):
            self.post({"password": PW})
            st = self.wait_fetch()
        self.assertEqual(st["state"], "error")
        self.assertFalse(os.path.exists(os.path.join(self.account, NEW)))
        _, msgs = self.get("/api/messages?sources=s1")
        self.assertEqual(msgs["total"], 0)
        self.assertIsNone(self.srv.viewer.store.mail_dir("s1"))

    def test_control_chars_and_bad_email_400_without_job(self):
        self.serve()
        cases = [
            ({"password": PW, "email": "a\x00b@example.test"}, "invalid email"),
            ({"password": PW, "email": " " + ACCOUNT}, "invalid email"),
            ({"password": PW, "email": "   "}, "invalid email"),
            ({"password": PW, "email": "x" * 250 + "@example.test"}, "invalid email"),
            ({"password": "pw\x00x"}, "invalid password"),
            ({"password": PW, "mailbox_password": "m\x7f"}, "invalid mailbox_password"),
            ({"password": PW, "totp": "12\x003"}, "invalid totp"),
        ]
        for body, reason in cases:
            status, data = self.post(body)
            self.assertEqual((status, data), (400, {"error": reason}), body)
        self.assertNotEqual(self.get("/api/job")[1]["kind"], "fetch")

    def test_secret_in_every_encoding_redacted(self):
        pw = 'p@ss "w/d'
        self.serve(mode="fail")
        self.post({"password": pw})
        st = self.wait_fetch()
        text = "\n".join([*texts(st["log"]), (st["message"] or {}).get("text", "")])
        for form in (pw, quote(pw, safe=""), json.dumps(pw)[1:-1]):
            self.assertNotIn(form, text)
        self.assertTrue(any(line.startswith("echo ") and "***" in line for line in texts(st["log"])), st["log"])

    def test_deeply_nested_json_400(self):
        self.serve()
        raw = b"[" * 60000  # deep enough to exceed the recursion limit, within MAX_BODY
        self.assertEqual(self.post(raw=raw), (400, {"error": "invalid JSON body"}))
        headers = {"Content-Type": "application/json", "Host": f"127.0.0.1:{self.port}",
                   "Origin": f"http://127.0.0.1:{self.port}"}
        self.assertEqual(request(self.port, "POST", "/api/library", raw, headers),
                         (400, {"error": "invalid JSON body", "code": "err.invalid_json", "params": {}}))


class JobTests(unittest.TestCase):
    def wait_idle(self, job, timeout=10.0):
        deadline = time.monotonic() + timeout
        while job.status()["state"] == "running":
            if time.monotonic() > deadline:
                self.fail("job still running")
            time.sleep(0.01)
        return job.status()

    def test_error_in_target_sets_state_and_message(self):
        job = server.Job()

        def boom(j):
            j.log(raw("start"))
            raise RuntimeError("kaputt")

        self.assertTrue(job.start("index", boom))
        st = self.wait_idle(job)
        self.assertEqual((st["state"], st["kind"], st["message"], st["log"]),
                         ("error", "index", {"text": "kaputt"}, [{"text": "start"}]))

    def test_one_slot_while_running(self):
        job = server.Job()
        gate = threading.Event()
        self.assertTrue(job.start("index", lambda j: gate.wait(5)))
        self.assertFalse(job.start("fetch", lambda j: None))
        gate.set()
        self.assertEqual(self.wait_idle(job)["state"], "done")


class CliForTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.export = make_export(self.tmp, "export")

    def executable(self, name):
        path = os.path.join(self.tmp, name)
        write(path, "#!/bin/sh\n")
        os.chmod(path, 0o755)
        return path

    def test_relative_settings_cli_ignored(self):
        lib = Library.open(make_library(self.tmp, self.export))
        src = lib.get("s1")
        near = self.executable(CLI_NAME)
        self.executable("rel-cli")  # runs only if the relative setting were resolved against the cwd
        old = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, old)
        lib.update_settings(cli="rel-cli")
        viewer = server.Viewer()
        viewer.library = lib
        self.assertEqual(viewer.cli_for(src), near)

    def test_precedence_explicit_then_settings_then_nearby(self):
        lib = Library.open(make_library(self.tmp, self.export))
        src = lib.get("s1")
        near = self.executable(CLI_NAME)  # found next to the export by find_cli
        setting = self.executable("setting-cli")
        explicit = self.executable("explicit-cli")
        viewer = server.Viewer()
        viewer.library = lib
        self.assertEqual(viewer.cli_for(src), near)
        lib.update_settings(cli=setting)
        self.assertEqual(viewer.cli_for(src), setting)
        viewer = server.Viewer(explicit, True)
        viewer.library = lib
        self.assertEqual(viewer.cli_for(src), explicit)


class LibraryTests(unittest.TestCase):
    """Library folder: open, create, remember, legacy export, index job (port of WorkdirTests)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))  # the index logs skipped files
        self.servers = []
        self.addCleanup(self.stop_all)  # runs before the redirect ends, so the jobs stay quiet
        self.export = make_export(self.tmp, "export")
        self.config = os.path.join(self.tmp, "cfg", "config.json")
        os.makedirs(os.path.dirname(self.config))

    def stop_all(self):
        for srv in list(self.servers):
            wait_job(srv.server_port)
            self.stop(srv)

    def serve(self, library_dir=None):
        srv = server.make_server(library_dir, 0, self.tmp, config_path=self.config)
        self.servers.append(srv)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    def stop(self, srv):
        if srv in self.servers:
            self.servers.remove(srv)
        srv.shutdown()
        srv.server_close()
        if srv.viewer.store is not None:
            srv.viewer.store.close()  # this thread's connection, opened by the test's reads

    def post(self, srv, body):
        return request(srv.server_port, "POST", "/api/library", json.dumps(body).encode("utf-8"),
                       {"Content-Type": "application/json",
                        "Origin": f"http://127.0.0.1:{srv.server_port}"})

    def test_starts_unconfigured_and_refuses_data(self):
        srv = self.serve()
        self.assertEqual(request(srv.server_port, "GET", "/api/config")[1]["configured"], False)
        for path in ("/api/folders", "/api/messages", "/api/message/x"):
            status, data = request(srv.server_port, "GET", path)
            self.assertEqual((status, data["code"]), (409, "err.no_library"), path)
        self.assertEqual(request(srv.server_port, "GET", "/api/job")[1]["state"], "idle")

    def test_create_open_and_remember(self):
        root = os.path.join(self.tmp, "lib")
        srv = self.serve()
        status, cfg = self.post(srv, {"path": root, "create": True, "remember": True})
        self.assertEqual(status, 200)
        self.assertEqual((cfg["configured"], cfg["library"], cfg["saved"]), (True, root, root))
        self.assertTrue(os.path.isfile(os.path.join(root, LIBRARY_FILE)))
        self.assertEqual(config.load_saved(self.config), root)
        self.stop(srv)
        srv = self.serve()  # the saved library is opened at start
        cfg = request(srv.server_port, "GET", "/api/config")[1]
        self.assertEqual((cfg["configured"], cfg["library"], cfg["saved_error"]), (True, root, None))

    def test_open_without_library_json_400_and_state_kept(self):
        lib_root = make_library(self.tmp, self.export)
        srv = self.serve(lib_root)
        wait_job(srv.server_port)
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        status, data = self.post(srv, {"path": empty})
        self.assertEqual(status, 400)
        self.assertIn("error", data)
        self.assertEqual(request(srv.server_port, "GET", "/api/config")[1]["library"], lib_root)
        self.assertFalse(os.path.exists(os.path.join(empty, LIBRARY_FILE)))

    def test_corrupt_library_json_400_and_file_untouched(self):
        lib_root = make_library(self.tmp, self.export)
        srv = self.serve(lib_root)
        wait_job(srv.server_port)
        bad = os.path.join(self.tmp, "bad")
        os.makedirs(bad)
        write(os.path.join(bad, LIBRARY_FILE), "{broken")
        for body in ({"path": bad}, {"path": bad, "create": True}):
            status, data = self.post(srv, body)
            self.assertEqual(status, 400, body)
            self.assertIn("error", data)
        with open(os.path.join(bad, LIBRARY_FILE), encoding="utf-8") as f:
            self.assertEqual(f.read(), "{broken")
        self.assertEqual(request(srv.server_port, "GET", "/api/config")[1]["library"], lib_root)

    def test_legacy_export_dir_offered_and_added(self):
        write_json(self.config, {"export_dir": self.export})
        srv = self.serve()
        self.assertEqual(request(srv.server_port, "GET", "/api/config")[1]["legacy"], self.export)
        root = os.path.join(self.tmp, "lib")
        status, _ = self.post(srv, {"path": root, "create": True, "add_legacy": True})
        self.assertEqual(status, 200)
        wait_job(srv.server_port)
        sources = Library.open(root).sources
        self.assertEqual([(s.id, s.path) for s in sources], [("s1", os.path.realpath(self.export))])

    def test_missing_legacy_dir_400_and_no_library_json(self):
        gone = os.path.join(self.tmp, "gone")
        write_json(self.config, {"export_dir": gone})
        srv = self.serve()
        root = os.path.join(self.tmp, "lib")
        status, data = self.post(srv, {"path": root, "create": True, "add_legacy": True})
        self.assertEqual(status, 400)
        self.assertEqual((data["code"], data["params"]), ("err.legacy_missing", {"path": gone}))
        self.assertFalse(os.path.exists(os.path.join(root, LIBRARY_FILE)))
        status, _ = self.post(srv, {"path": root, "create": True})  # the retry without the legacy source works
        self.assertEqual(status, 200)
        self.assertEqual(Library.open(root).sources, [])

    def test_legacy_key_removed_once_added(self):
        write_json(self.config, {"export_dir": self.export, "other": 1})
        srv = self.serve()
        root = os.path.join(self.tmp, "lib")
        status, _ = self.post(srv, {"path": root, "create": True, "add_legacy": True, "remember": True})
        self.assertEqual(status, 200)
        wait_job(srv.server_port)
        self.assertIsNone(request(srv.server_port, "GET", "/api/config")[1]["legacy"])
        with open(self.config, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"other": 1, "library": root})

    def test_legacy_key_kept_without_add_legacy(self):
        write_json(self.config, {"export_dir": self.export})
        srv = self.serve()
        root = os.path.join(self.tmp, "lib")
        status, _ = self.post(srv, {"path": root, "create": True, "remember": True})
        self.assertEqual(status, 200)
        wait_job(srv.server_port)
        self.assertEqual(request(srv.server_port, "GET", "/api/config")[1]["legacy"], self.export)
        with open(self.config, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"export_dir": self.export, "library": root})

    def test_failing_source_does_not_stop_the_index_job(self):
        export2 = make_export(self.tmp, "second")
        lib_root = make_library(self.tmp, self.export, export2)
        original = server.Store.sync_source

        def flaky(store, source_id, *args, **kwargs):
            if source_id == "s1":
                raise OSError("kaputt")
            return original(store, source_id, *args, **kwargs)

        with mock.patch.object(server.Store, "sync_source", flaky):
            srv = self.serve(lib_root)
            st = wait_job(srv.server_port)
        self.assertEqual(st["state"], "done")
        self.assertTrue(any(line.startswith("export: error") for line in texts(st["log"])), st["log"])
        self.assertIn("second: +4 ~0 -0", texts(st["log"]))

    def test_remember_key_absent_keeps_saved_library(self):
        root = os.path.join(self.tmp, "lib")
        srv = self.serve()
        self.post(srv, {"path": root, "create": True, "remember": True})
        wait_job(srv.server_port)
        self.assertEqual(config.load_saved(self.config), root)
        status, cfg = self.post(srv, {"path": root})  # no remember key: saved choice stays
        self.assertEqual(status, 200)
        self.assertEqual(cfg["saved"], root)
        self.assertEqual(config.load_saved(self.config), root)
        wait_job(srv.server_port)
        status, cfg = self.post(srv, {"path": root, "remember": False})
        self.assertEqual((status, cfg["saved"]), (200, None))
        self.assertIsNone(config.load_saved(self.config))
        wait_job(srv.server_port)
        status, cfg = self.post(srv, {"path": root, "remember": True})
        self.assertEqual((status, cfg["saved"]), (200, root))
        self.assertEqual(config.load_saved(self.config), root)

    def test_switch_library_while_job_running_409(self):
        gate = threading.Event()
        original = server.Store.sync_source

        def slow(store, *args, **kwargs):
            gate.wait(10)
            return original(store, *args, **kwargs)

        lib_root = make_library(self.tmp, self.export)
        other = os.path.join(self.tmp, "other")
        with mock.patch.object(server.Store, "sync_source", slow):
            srv = self.serve(lib_root)
            status, data = self.post(srv, {"path": other, "create": True})
            self.assertEqual(status, 409)
            self.assertIn("error", data)
            self.assertFalse(os.path.exists(other))
            gate.set()
            self.assertEqual(wait_job(srv.server_port)["state"], "done")
        self.assertEqual(request(srv.server_port, "GET", "/api/config")[1]["library"], lib_root)

    def test_missing_source_path_is_unavailable_but_others_work(self):
        export2 = make_export(self.tmp, "second")
        lib_root = make_library(self.tmp, self.export, export2)
        srv = self.serve(lib_root)
        self.assertEqual(wait_job(srv.server_port)["state"], "done")
        os.rename(export2, export2 + "-moved")
        self.stop(srv)
        srv = self.serve(lib_root)
        st = wait_job(srv.server_port)
        self.assertEqual(st["state"], "done")
        self.assertIn({"source": "second"}, [e["params"] for e in st["log"] if e.get("code") == "log.source_unreachable"])
        viewer = srv.viewer
        self.assertFalse(viewer.available(viewer.library.get("s2")))
        self.assertTrue(viewer.available(viewer.library.get("s1")))
        self.assertEqual(viewer.store.mail_dir("s2"), os.path.realpath(export2))  # rows stay until removed
        status, data = request(srv.server_port, "GET", "/api/messages?sources=s1")
        self.assertEqual((status, data["total"]), (200, 4))
        status, m = request(srv.server_port, "GET", f"/api/message/s2/{ID_PLAIN}")
        self.assertEqual(status, 200)
        self.assertEqual(m["error"], "message file (.eml) not found")
        status, data = request(srv.server_port, "GET", "/api/sources")
        self.assertEqual([(s["id"], s["available"]) for s in data], [("s1", True), ("s2", False)])

    def test_removed_source_rows_dropped_on_open(self):
        export2 = make_export(self.tmp, "second")
        lib_root = make_library(self.tmp, self.export, export2)
        srv = self.serve(lib_root)
        wait_job(srv.server_port)
        self.assertIsNotNone(srv.viewer.store.mail_dir("s2"))
        lib = Library.open(lib_root)
        lib.remove_source("s2")
        self.stop(srv)
        srv = self.serve(lib_root)
        wait_job(srv.server_port)
        self.assertIsNone(srv.viewer.store.mail_dir("s2"))
        self.assertEqual(srv.viewer.store.messages(["s2"], None, "", 0, 10)["total"], 0)
        self.assertIsNotNone(srv.viewer.store.mail_dir("s1"))

    def test_index_job_status_progress(self):
        srv = self.serve(make_library(self.tmp, self.export))
        st = wait_job(srv.server_port)
        self.assertEqual((st["state"], st["kind"]), ("done", "index"))
        # 4 mails plus the broken metadata file: store.py counts every metadata file it must parse
        self.assertEqual(st["progress"], {"done": 5, "total": 5})
        self.assertEqual(texts(st["log"]), ["export: +4 ~0 -0"])

    def test_cli_exits_without_library_json(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        with self.assertRaises(SystemExit) as cm:
            server.main([empty, "--config", self.config])
        self.assertIn("No library", str(cm.exception.code))


class ErrorCodeTests(Base):
    def test_error_carries_code_and_params(self):
        status, data = self.get_json("/api/folders?sources=nope")
        self.assertEqual((status, data["code"], data["params"]), (400, "err.unknown_source", {}))
        self.assertEqual(data["error"], "unknown source")


class LanguageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfgdir = os.path.join(self.tmp, "cfg")
        os.makedirs(self.cfgdir)
        self.config_path = os.path.join(self.cfgdir, "config.json")
        self.srv = server.make_server(None, 0, self.tmp, config_path=self.config_path)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.srv.shutdown(), self.srv.server_close()))
        self.port = self.srv.server_port

    def boot(self, accept=None):
        status, headers, body = request_raw(self.port, "GET", "/i18n.js", headers={"Accept-Language": accept} if accept else {})
        text = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertTrue(text.startswith("window.I18N_BOOT = "))
        return json.loads(text[len("window.I18N_BOOT = "):].rstrip().rstrip(";")), headers

    def post(self, body, origin=True):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = f"http://127.0.0.1:{self.port}"
        return request(self.port, "POST", "/api/language", json.dumps(body).encode("utf-8"), headers)

    def test_boot_script_follows_accept_language(self):
        boot, headers = self.boot("de-DE,de;q=0.9")
        self.assertEqual((boot["setting"], boot["lang"]), ("system", "de"))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertTrue(headers["Content-Type"].startswith("text/javascript"))

    def test_saved_language_wins(self):
        config.update_config(self.config_path, {"language": "en"})
        boot, _ = self.boot("de")
        self.assertEqual((boot["setting"], boot["lang"]), ("en", "en"))

    def test_post_language_persists_and_returns_boot(self):
        status, data = self.post({"language": "de"})
        self.assertEqual((status, data["lang"], data["setting"]), (200, "de", "de"))
        self.assertEqual(config.load_language(self.config_path), "de")

    def test_post_system_removes_key(self):
        config.update_config(self.config_path, {"language": "de", "library": "/x"})
        status, data = self.post({"language": "system"})
        self.assertEqual((status, data["setting"]), (200, "system"))
        self.assertIsNone(config.load_language(self.config_path))
        self.assertEqual(config.load_saved(self.config_path), "/x")

    def test_post_invalid_language(self):
        for body in ({"language": "fr"}, {"language": 1}, {}):
            status, data = self.post(body)
            self.assertEqual((status, data["code"]), (400, "err.invalid_language"))

    def test_post_language_needs_origin(self):
        status, _ = self.post({"language": "de"}, origin=False)
        self.assertEqual(status, 403)
        self.assertIsNone(config.load_language(self.config_path))

    def test_corrupt_config_falls_back_to_system(self):
        with open(self.config_path, "w") as f:
            f.write("{nope")
        boot, _ = self.boot("de")
        self.assertEqual((boot["setting"], boot["lang"]), ("system", "de"))

    def test_unwritable_config(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores permissions")
        os.chmod(self.cfgdir, 0o500)
        self.addCleanup(os.chmod, self.cfgdir, 0o700)
        status, data = self.post({"language": "de"})
        self.assertEqual((status, data["code"]), (500, "err.config_write"))


class SourceAdminTests(unittest.TestCase):
    """Admin routes for sources, icons and settings. Each test gets its own library and server."""

    CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'; sandbox"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))  # the index logs skipped files
        self.servers = []
        self.addCleanup(self.stop_all)  # runs before the redirect ends, so the jobs stay quiet
        self.export = make_export(self.tmp, "export")
        self.config = os.path.join(self.tmp, "config.json")

    def stop_all(self):
        for srv in list(self.servers):
            wait_job(srv.server_port)
            self.stop(srv)

    def stop(self, srv):
        if srv in self.servers:
            self.servers.remove(srv)
        srv.shutdown()
        srv.server_close()
        if srv.viewer.store is not None:
            srv.viewer.store.close()

    def serve(self, library_dir, wait=True):
        srv = server.make_server(library_dir, 0, self.tmp, config_path=self.config)
        self.servers.append(srv)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        if wait:
            wait_job(srv.server_port)
        return srv

    def post(self, srv, path, body=None, *, raw=None, ctype="application/json",
             origin="same", host=None):
        """POST and return (status, parsed JSON). Origin defaults to the server's own."""
        if raw is None:
            raw = json.dumps(body if body is not None else {}).encode("utf-8")
        headers = {"Content-Type": ctype}
        if origin == "same":
            origin = f"http://127.0.0.1:{srv.server_port}"
        if origin is not None:
            headers["Origin"] = origin
        if host is not None:
            headers["Host"] = host
        status, _, data = request_raw(srv.server_port, "POST", path, raw, headers)
        return status, json.loads(data)

    def get_icon(self, srv, source_id):
        return request_raw(srv.server_port, "GET", f"/api/sources/{source_id}/icon")

    def test_add_source_indexes_it(self):
        export2 = make_export(self.tmp, "second")
        srv = self.serve(make_library(self.tmp, self.export))
        status, data = self.post(srv, "/api/sources", {"path": export2, "name": "Archiv"})
        self.assertEqual(status, 200)
        self.assertEqual([(s["id"], s["name"]) for s in data], [("s1", "export"), ("s2", "Archiv")])
        st = wait_job(srv.server_port)
        self.assertEqual((st["state"], st["kind"]), ("done", "index"))
        self.assertEqual(texts(st["log"]), ["Archiv: +4 ~0 -0"])  # only the new source is indexed
        status, data = request(srv.server_port, "GET", "/api/sources")
        self.assertEqual({s["id"]: s["count"] for s in data}, {"s1": 4, "s2": 4})

    def test_add_duplicate_or_bad_path_400(self):
        root = make_library(self.tmp, self.export)
        srv = self.serve(root)
        self.assertEqual(self.post(srv, "/api/sources", {"path": self.export})[0], 400)  # duplicate
        self.assertEqual(self.post(srv, "/api/sources", {"path": os.path.join(self.tmp, "nope")})[0], 400)
        self.assertEqual(self.post(srv, "/api/sources", {"path": " "})[0], 400)
        self.assertEqual(self.post(srv, "/api/sources", {})[0], 400)
        self.assertEqual(self.post(srv, "/api/sources", raw=b"nope")[0], 400)
        self.assertEqual([s.id for s in Library.open(root).sources], ["s1"])

    def test_rename_hide_move(self):
        export2 = make_export(self.tmp, "second")
        root = make_library(self.tmp, self.export, export2)
        srv = self.serve(root)
        status, data = self.post(srv, "/api/sources/s1", {"name": "Privat", "hidden": True})
        self.assertEqual(status, 200)
        self.assertEqual([(s["id"], s["name"], s["hidden"]) for s in data],
                         [("s1", "Privat", True), ("s2", "second", False)])
        status, data = self.post(srv, "/api/sources/s2", {"position": 0})
        self.assertEqual([s["id"] for s in data], ["s2", "s1"])
        self.assertEqual(self.post(srv, "/api/sources/s2", {"name": "  "})[0], 400)
        self.assertEqual(self.post(srv, "/api/sources/s1", {"hidden": "yes"})[0], 400)
        self.assertEqual(self.post(srv, "/api/sources/s1", {"position": "1"})[0], 400)
        self.assertEqual(self.post(srv, "/api/sources/s1", {"color": "red"})[0], 400)
        self.assertEqual(self.post(srv, "/api/sources/s9", {"name": "x"})[0], 404)
        lib = Library.open(root)
        self.assertEqual([(s.id, s.name, s.hidden) for s in lib.sources],
                         [("s2", "second", False), ("s1", "Privat", True)])

    def test_remove_drops_rows_keeps_files(self):
        export2 = make_export(self.tmp, "second")
        root = make_library(self.tmp, self.export, export2)
        srv = self.serve(root)
        before = sorted(os.listdir(export2))
        status, data = self.post(srv, "/api/sources/s2/remove", {})
        self.assertEqual(status, 200)
        self.assertEqual([s["id"] for s in data], ["s1"])
        self.assertIsNone(srv.viewer.store.mail_dir("s2"))
        self.assertEqual(srv.viewer.store.messages(["s2"], None, "", 0, 10)["total"], 0)
        self.assertEqual(sorted(os.listdir(export2)), before)
        self.assertEqual(self.post(srv, "/api/sources/s2/remove", {})[0], 404)

    def test_remove_deletes_only_its_icon(self):
        export2 = make_export(self.tmp, "second")
        root = make_library(self.tmp, self.export, export2)
        srv = self.serve(root)
        self.assertEqual(self.post(srv, "/api/sources/s1/icon", raw=ICON_PNG, ctype="image/png")[0], 200)
        self.assertEqual(self.post(srv, "/api/sources/s2/icon", raw=ICON_PNG, ctype="image/png")[0], 200)
        self.assertEqual(self.post(srv, "/api/sources/s1/remove", {})[0], 200)
        self.assertFalse(os.path.exists(os.path.join(root, "icons", "s1.png")))
        self.assertTrue(os.path.exists(os.path.join(root, "icons", "s2.png")))
        self.assertEqual(self.get_icon(srv, "s2")[0], 200)

    def test_remove_index_error_keeps_source_and_icon(self):
        root = make_library(self.tmp, self.export)
        srv = self.serve(root)
        self.assertEqual(self.post(srv, "/api/sources/s1/icon", raw=ICON_PNG, ctype="image/png")[0], 200)
        boom = sqlite3.OperationalError("disk I/O error")
        with mock.patch.object(server.Store, "drop_source", side_effect=boom):
            status, data = self.post(srv, "/api/sources/s1/remove", {})
        self.assertEqual(status, 500)
        self.assertEqual(data["code"], "err.index")
        self.assertEqual([s["id"] for s in request(srv.server_port, "GET", "/api/sources")[1]], ["s1"])
        self.assertEqual([s.id for s in Library.open(root).sources], ["s1"])
        self.assertTrue(os.path.exists(os.path.join(root, "icons", "s1.png")))

    def test_icon_content_checked_before_write(self):
        root = make_library(self.tmp, self.export)
        srv = self.serve(root)
        cases = [
            (b"", "image/png"),
            (b"GIF89a" + bytes(16), "image/png"),
            (b"\x89PNG\r\n\x1a", "image/png"),  # signature cut short
            (b"<svg>\xff\xfe</svg>", "image/svg+xml"),  # not UTF-8
            (b"hello <svg/>", "image/svg+xml"),  # does not start with a tag
            (b"<html><body/></html>", "image/svg+xml"),  # no svg element
        ]
        for raw, ctype in cases:
            status, data = self.post(srv, "/api/sources/s1/icon", raw=raw, ctype=ctype)
            self.assertEqual((status, data["code"]), (400, "err.invalid_image"), (raw, ctype))
        self.assertFalse(os.path.exists(os.path.join(root, "icons")))

    def test_icon_upload_wrong_host_or_origin_403(self):
        root = make_library(self.tmp, self.export)
        srv = self.serve(root)
        self.assertEqual(self.post(srv, "/api/sources/s1/icon", raw=ICON_PNG, ctype="image/png",
                                   host="evil.test")[0], 403)
        self.assertEqual(self.post(srv, "/api/sources/s1/icon", raw=ICON_PNG, ctype="image/png",
                                   origin="http://evil.test")[0], 403)
        self.assertFalse(os.path.exists(os.path.join(root, "icons")))

    def test_icon_upload_png_and_svg_served_with_csp(self):
        root = make_library(self.tmp, self.export)
        srv = self.serve(root)
        status, data = self.post(srv, "/api/sources/s1/icon", raw=ICON_PNG, ctype="image/png")
        self.assertEqual(status, 200)
        self.assertEqual(data[0]["icon"], {"type": "image", "file": "s1.png"})
        status, headers, body = self.get_icon(srv, "s1")
        self.assertEqual((status, headers.get("Content-Type"), body), (200, "image/png", ICON_PNG))
        self.assertEqual(headers.get("Content-Security-Policy"), self.CSP)
        self.assertEqual(set(headers.get("Content-Security-Policy").split("; ")), set(self.CSP.split("; ")))
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
        self.assertEqual(self.post(srv, "/api/sources/s1/icon", raw=svg, ctype="image/svg+xml")[0], 200)
        self.assertFalse(os.path.exists(os.path.join(root, "icons", "s1.png")))
        self.assertEqual(Library.open(root).get("s1").icon, {"type": "image", "file": "s1.svg"})
        status, headers, body = self.get_icon(srv, "s1")
        self.assertEqual((status, headers.get("Content-Type"), body), (200, "image/svg+xml", svg))
        self.assertEqual(headers.get("Content-Security-Policy"), self.CSP)

    def test_icon_rejects_other_type_and_too_large(self):
        root = make_library(self.tmp, self.export)
        srv = self.serve(root)
        self.assertEqual(self.post(srv, "/api/sources/s1/icon", raw=ICON_PNG, ctype="image/gif")[0], 400)
        self.assertEqual(self.post(srv, "/api/sources/s1/icon", raw=ICON_PNG, ctype="image/png; x=1")[0], 400)
        status, data = self.post(srv, "/api/sources/s1/icon", raw=b"x" * (validation.MAX_ICON + 1),
                                 ctype="image/png")
        self.assertEqual((status, data["error"]), (400, "body too large"))
        self.assertEqual(self.post(srv, "/api/sources/s9/icon", raw=ICON_PNG, ctype="image/png")[0], 404)
        self.assertFalse(os.path.exists(os.path.join(root, "icons")))
        at_limit = ICON_PNG + b"0" * (validation.MAX_ICON - len(ICON_PNG))
        status, _ = self.post(srv, "/api/sources/s1/icon", raw=at_limit, ctype="image/png")
        self.assertEqual(status, 200)  # exactly the limit is allowed

    def test_icon_serving_only_inside_icons_dir(self):
        root = make_library(self.tmp, self.export)
        icons = os.path.join(root, "icons")
        os.makedirs(icons)
        outside = os.path.join(self.tmp, "outside.png")
        write(outside, PNG)
        os.symlink(outside, os.path.join(icons, "s1.png"))
        Library.open(root).update_source("s1", icon={"type": "image", "file": "s1.png"})
        srv = self.serve(root)
        self.assertEqual(self.get_icon(srv, "s1")[0], 404)  # a symlink out of icons/ is not followed
        self.stop(srv)
        Library.open(root).update_source("s1", icon={"type": "image", "file": "."})
        srv = self.serve(root)
        self.assertEqual(self.get_icon(srv, "s1")[0], 404)  # "." names icons/ itself
        self.assertEqual(self.get_icon(srv, "s9")[0], 404)

    def test_emoji_icon(self):
        srv = self.serve(make_library(self.tmp, self.export))
        self.assertEqual(self.get_icon(srv, "s1")[0], 404)  # the default icon has no image
        status, data = self.post(srv, "/api/sources/s1", {"icon": {"type": "emoji", "value": "📚"}})
        self.assertEqual((status, data[0]["icon"]), (200, {"type": "emoji", "value": "📚"}))
        self.assertEqual(self.post(srv, "/api/sources/s1", {"icon": {"type": "emoji", "value": ""}})[0], 400)
        self.assertEqual(self.post(srv, "/api/sources/s1", {"icon": "x"})[0], 400)
        # an image icon is only ever set by the upload
        self.assertEqual(self.post(srv, "/api/sources/s1", {"icon": {"type": "image", "file": "s1.png"}})[0],
                         400)

    def test_settings_update_and_validation(self):
        root = make_library(self.tmp, self.export)
        srv = self.serve(root)
        status, cfg = self.post(srv, "/api/settings",
                                {"view": "single", "proton_email": "a@example.test", "cli": ""})
        self.assertEqual(status, 200)
        self.assertEqual(cfg["settings"], {"view": "single", "proton_email": "a@example.test", "cli": ""})
        self.assertEqual(self.post(srv, "/api/settings", {"view": "x"})[0], 400)
        self.assertEqual(self.post(srv, "/api/settings", {"view": 5})[0], 400)
        self.assertEqual(self.post(srv, "/api/settings", {"other": "1"})[0], 400)
        self.assertEqual(self.post(srv, "/api/settings", {"self": "1"})[0], 400)
        self.assertEqual(self.post(srv, "/api/settings", raw=b"[]")[0], 400)
        self.assertEqual(Library.open(root).settings.view, "single")

    def test_settings_body_limit_400(self):
        srv = self.serve(make_library(self.tmp, self.export))
        raw = b'{"view": "' + b"x" * validation.MAX_BODY + b'"}'
        status, data = self.post(srv, "/api/settings", raw=raw)
        self.assertEqual((status, data["error"]), (400, "body too large"))

    def test_admin_posts_require_origin(self):
        root = make_library(self.tmp, self.export, make_export(self.tmp, "second"))
        srv = self.serve(root)
        routes = [("/api/sources", {"path": self.export}), ("/api/sources/s1", {"name": "x"}),
                  ("/api/sources/s1/remove", {}), ("/api/settings", {"view": "single"})]
        for path, body in routes:
            for origin in (None, "http://evil.test", f"https://127.0.0.1:{srv.server_port}"):
                self.assertEqual(self.post(srv, path, body, origin=origin)[0], 403, (path, origin))
            self.assertEqual(self.post(srv, path, body, host="evil.test")[0], 403, path)
        self.assertEqual(self.post(srv, "/api/sources/s1/icon", raw=PNG, ctype="image/png", origin=None)[0],
                         403)
        lib = Library.open(root)
        self.assertEqual([s.id for s in lib.sources], ["s1", "s2"])
        self.assertEqual((lib.settings.view, lib.get("s1").name), ("combined", "export"))

    def test_store_changes_409_while_job_runs(self):
        gate = threading.Event()
        self.addCleanup(gate.set)  # frees the blocked job before the servers are stopped
        original = server.Store.sync_source

        def slow(store, *args, **kwargs):
            gate.wait(10)
            return original(store, *args, **kwargs)

        export2 = make_export(self.tmp, "second")
        root = make_library(self.tmp, self.export)
        with mock.patch.object(server.Store, "sync_source", slow):
            srv = self.serve(root, wait=False)
            self.assertEqual(self.post(srv, "/api/sources", {"path": export2})[0], 409)
            self.assertEqual(self.post(srv, "/api/sources/s1/remove", {})[0], 409)
            self.assertEqual(self.post(srv, "/api/sources/s1/icon", raw=ICON_PNG, ctype="image/png")[0], 409)
            # library.json only, so these may run during the job
            self.assertEqual(self.post(srv, "/api/sources/s1", {"name": "Neu"})[0], 200)
            self.assertEqual(self.post(srv, "/api/settings", {"view": "single"})[0], 200)
            gate.set()
            self.assertEqual(wait_job(srv.server_port)["state"], "done")
        lib = Library.open(root)
        self.assertEqual([s.id for s in lib.sources], ["s1"])
        self.assertEqual((lib.get("s1").name, lib.settings.view), ("Neu", "single"))
        self.assertFalse(os.path.exists(os.path.join(root, "icons")))

    def test_icon_url_with_cache_buster_served(self):
        srv = self.serve(make_library(self.tmp, self.export))
        self.assertEqual(self.post(srv, "/api/sources/s1/icon", raw=ICON_PNG, ctype="image/png")[0], 200)
        status, headers, body = request_raw(srv.server_port, "GET", "/api/sources/s1/icon?v=3")
        self.assertEqual((status, headers.get("Content-Type"), body), (200, "image/png", ICON_PNG))

    def test_admin_without_library_409(self):
        srv = self.serve(None)
        self.assertEqual(self.post(srv, "/api/sources", {"path": self.export})[0], 409)
        self.assertEqual(self.post(srv, "/api/sources/s1/remove", {})[0], 409)
        self.assertEqual(self.post(srv, "/api/settings", {"view": "single"})[0], 409)

    def test_browse_flags_library(self):
        srv = self.serve(make_library(self.tmp, self.export))
        status, data = request(srv.server_port, "GET", f"/api/browse?path={quote(self.tmp, safe='')}")
        self.assertEqual(status, 200)
        entries = {d["name"]: d for d in data["dirs"]}
        self.assertEqual(entries["library"]["library"], True)
        self.assertEqual(entries["library"]["openable"], False)
        self.assertEqual(entries["export"], {"name": "export", "openable": True, "library": False})

    def test_browse_top_level_library_flag(self):
        root = make_library(self.tmp, self.export)
        srv = self.serve(root)
        status, data = request(srv.server_port, "GET", f"/api/browse?path={quote(root, safe='')}")
        self.assertEqual((status, data["library"]), (200, True))
        status, data = request(srv.server_port, "GET", f"/api/browse?path={quote(self.export, safe='')}")
        self.assertEqual((status, data["library"]), (200, False))

    def test_browse_lists_outlook_files(self):
        d = os.path.join(self.tmp, "pst")
        os.makedirs(os.path.join(d, "sub"))
        for name in ("a.PST", "b.ost", "c.txt", ".hidden.pst"):
            write(os.path.join(d, name), b"x")
        listed = browse.browse(d, files=True)
        self.assertEqual([f["name"] for f in listed["files"]], ["a.PST", "b.ost"])
        self.assertEqual(listed["files"][0]["size"], 1)
        self.assertEqual([x["name"] for x in listed["dirs"]], ["sub"])
        self.assertNotIn("files", browse.browse(d))
        srv = self.serve(make_library(self.tmp, self.export))
        status, data = request(srv.server_port, "GET", f"/api/browse?path={quote(d, safe='')}&files=outlook")
        self.assertEqual((status, [f["name"] for f in data["files"]]), (200, ["a.PST", "b.ost"]))
        status, data = request(srv.server_port, "GET", f"/api/browse?path={quote(d, safe='')}")
        self.assertNotIn("files", data)

    def test_config_reports_outlook_import(self):
        srv = self.serve(None)
        with mock.patch("mailviewer.server.pypff_available", return_value=False):
            status, data = request(srv.server_port, "GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertIs(data["outlook_import"], False)
        self.assertEqual(data["install_hint"]["code"], "err.pst_missing_pip")
        with mock.patch("mailviewer.server.pypff_available", return_value=True):
            status, data = request(srv.server_port, "GET", "/api/config")
        self.assertIs(data["outlook_import"], True)


class TagRouteTests(unittest.TestCase):
    """Tag routes: create, list, rename, recolor, delete. Each test gets its own library and server."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))  # the index logs skipped files
        self.servers = []
        self.addCleanup(self.stop_all)  # runs before the redirect ends, so the jobs stay quiet
        self.export = make_export(self.tmp, "export")
        self.config = os.path.join(self.tmp, "config.json")

    def stop_all(self):
        for srv in list(self.servers):
            wait_job(srv.server_port)
            self.stop(srv)

    def stop(self, srv):
        if srv in self.servers:
            self.servers.remove(srv)
        srv.shutdown()
        srv.server_close()
        if srv.viewer.store is not None:
            srv.viewer.store.close()
        if srv.viewer.tags is not None:
            srv.viewer.tags.close()

    def serve(self, library_dir, wait=True):
        srv = server.make_server(library_dir, 0, self.tmp, config_path=self.config)
        self.servers.append(srv)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        if wait:
            wait_job(srv.server_port)
        return srv

    def post(self, srv, path, body=None, *, raw=None, ctype="application/json",
             origin="same", host=None):
        """POST and return (status, parsed JSON). Origin defaults to the server's own."""
        if raw is None:
            raw = json.dumps(body if body is not None else {}).encode("utf-8")
        headers = {"Content-Type": ctype}
        if origin == "same":
            origin = f"http://127.0.0.1:{srv.server_port}"
        if origin is not None:
            headers["Origin"] = origin
        if host is not None:
            headers["Host"] = host
        status, _, data = request_raw(srv.server_port, "POST", path, raw, headers)
        return status, json.loads(data)

    def test_create_list_rename_recolor_delete(self):
        srv = self.serve(make_library(self.tmp, self.export))
        status, data = self.post(srv, "/api/tags", {"name": "Rechnung"})
        self.assertEqual(status, 200)
        self.assertEqual(data["star"], 0)
        self.assertEqual(len(data["tags"]), 1)
        tag = data["tags"][0]
        self.assertEqual((tag["name"], tag["count"]), ("Rechnung", 0))
        self.assertIn(tag["color"], validation.TAG_COLORS)  # no color given: one of the defaults
        tag_id = tag["id"]
        self.assertEqual(request(srv.server_port, "GET", "/api/tags"), (200, data))

        status, data = self.post(srv, f"/api/tags/{tag_id}", {"name": "Belege"})
        self.assertEqual(status, 200)
        self.assertEqual([(t["name"], t["color"]) for t in data["tags"]], [("Belege", tag["color"])])

        status, data = self.post(srv, f"/api/tags/{tag_id}", {"color": "#ABCDEF"})
        self.assertEqual(status, 200)
        self.assertEqual([(t["name"], t["color"]) for t in data["tags"]], [("Belege", "#abcdef")])
        self.assertEqual(request(srv.server_port, "GET", "/api/tags")[1], data)

        self.assertEqual(self.post(srv, f"/api/tags/{tag_id}", {"name": "x"}, origin=None)[0], 403)
        self.assertEqual(self.post(srv, f"/api/tags/{tag_id}", {"x": 1})[0], 400)  # unknown field
        self.assertEqual(self.post(srv, "/api/tags/99", {"name": "x"})[0], 404)
        self.assertEqual(self.post(srv, "/api/tags/99/remove", {})[0], 404)

        status, data = self.post(srv, f"/api/tags/{tag_id}/remove", {})
        self.assertEqual(status, 200)
        self.assertEqual(data["tags"], [])
        self.assertEqual(request(srv.server_port, "GET", "/api/tags")[1], {"star": 0, "tags": []})

    def test_bad_tag_names(self):
        srv = self.serve(make_library(self.tmp, self.export))
        for name in ["", "   ", "x" * 41, "a\nb", None, 5]:
            self.assertEqual(self.post(srv, "/api/tags", {"name": name})[0], 400, repr(name))
        self.assertEqual(self.post(srv, "/api/tags", {})[0], 400)
        self.assertEqual(self.post(srv, "/api/tags", {"name": "Rechnung"})[0], 200)
        self.assertEqual(self.post(srv, "/api/tags", {"name": " rechnung "})[0], 400)  # duplicate, any case
        status, data = self.post(srv, "/api/tags", {"name": "<b>x</b>"})
        self.assertEqual(status, 200)
        self.assertEqual(sorted(t["name"] for t in data["tags"]), ["<b>x</b>", "Rechnung"])

    def test_bad_colors(self):
        srv = self.serve(make_library(self.tmp, self.export))
        for color in ["red", "#abc", "#12345g", 123]:
            self.assertEqual(self.post(srv, "/api/tags", {"name": "Rechnung", "color": color})[0], 400, repr(color))
        self.assertEqual(request(srv.server_port, "GET", "/api/tags")[1], {"star": 0, "tags": []})
        self.assertEqual(self.post(srv, "/api/tags", {"name": "Rechnung"})[0], 200)
        self.assertEqual(self.post(srv, "/api/tags/1", {"color": "#abc"})[0], 400)
        self.assertEqual(self.post(srv, "/api/tags/1", {"color": None})[0], 400)

    def test_post_needs_origin(self):
        srv = self.serve(make_library(self.tmp, self.export))
        self.assertEqual(self.post(srv, "/api/tags", {"name": "Rechnung"}, origin=None)[0], 403)
        self.assertEqual(self.post(srv, "/api/tags", {"name": "Rechnung"}, origin="http://evil.test")[0], 403)
        self.assertEqual(self.post(srv, "/api/tags", {"name": "Rechnung"}, host="evil.test")[0], 403)
        self.assertEqual(request(srv.server_port, "GET", "/api/tags")[1], {"star": 0, "tags": []})

    def test_tags_are_per_library(self):
        lib_a = make_library(self.tmp, self.export)
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        lib_b = make_library(other, self.export)
        srv = self.serve(lib_a)
        self.assertEqual(self.post(srv, "/api/tags", {"name": "Rechnung"})[0], 200)
        self.assertEqual(self.post(srv, "/api/library", {"path": lib_b})[0], 200)
        wait_job(srv.server_port)
        self.assertEqual(request(srv.server_port, "GET", "/api/tags")[1], {"star": 0, "tags": []})
        self.assertEqual(self.post(srv, "/api/library", {"path": lib_a})[0], 200)
        wait_job(srv.server_port)
        self.assertEqual([t["name"] for t in request(srv.server_port, "GET", "/api/tags")[1]["tags"]],
                         ["Rechnung"])

    def marks_path(self, mid, src="s1"):
        return f"/api/message/{src}/{quote(mid, safe='')}/marks"

    def create_tag(self, srv, name="Rechnung"):
        status, data = self.post(srv, "/api/tags", {"name": name})
        self.assertEqual(status, 200)
        return next(t["id"] for t in data["tags"] if t["name"] == name)

    def test_mark_and_filter(self):
        srv = self.serve(make_library(self.tmp, self.export))
        t = self.create_tag(srv)
        status, data = self.post(srv, self.marks_path(ID_PLAIN), {"starred": True, "tags": [t]})
        self.assertEqual((status, data), (200, {"starred": True, "tags": [t]}))

        status, data = request(srv.server_port, "GET", "/api/messages?tag=star")
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 1)
        self.assertEqual([m["id"] for m in data["messages"]], [ID_PLAIN])
        self.assertIs(data["messages"][0]["starred"], True)
        self.assertEqual(data["messages"][0]["tags"], [t])

        status, data = request(srv.server_port, "GET", f"/api/messages?tag={t}")
        self.assertEqual([m["id"] for m in data["messages"]], [ID_PLAIN])

        status, data = request(srv.server_port, "GET", "/api/tags")
        self.assertEqual(data["star"], 1)
        self.assertEqual([(x["name"], x["count"]) for x in data["tags"]], [("Rechnung", 1)])

        status, m = request(srv.server_port, "GET", f"/api/message/s1/{quote(ID_PLAIN, safe='')}")
        self.assertEqual(status, 200)
        self.assertEqual((m["markable"], m["starred"], m["tags"]), (True, True, [t]))

    def test_mark_ghost_mail_is_400(self):
        srv = self.serve(make_library(self.tmp, self.export))
        t = self.create_tag(srv)
        status, data = self.post(srv, self.marks_path(ID_GHOST), {"starred": True, "tags": [t]})
        self.assertEqual(status, 400)
        self.assertEqual(data["code"], "err.mail_no_eml")
        status, m = request(srv.server_port, "GET", f"/api/message/s1/{quote(ID_GHOST, safe='')}")
        self.assertEqual((status, m["markable"], m["starred"], m["tags"]), (200, False, False, []))

    def test_mark_unknown_message_is_404(self):
        srv = self.serve(make_library(self.tmp, self.export))
        self.assertEqual(self.post(srv, self.marks_path("nope"), {"starred": True})[0], 404)
        self.assertEqual(self.post(srv, self.marks_path(ID_PLAIN, src="s9"), {"starred": True})[0], 404)

    def test_mark_bad_body(self):
        srv = self.serve(make_library(self.tmp, self.export))
        self.create_tag(srv)
        for body in [{"starred": "yes"}, {"tags": [True]}, {"tags": "1"}, {"x": 1}]:
            self.assertEqual(self.post(srv, self.marks_path(ID_PLAIN), body)[0], 400, repr(body))
        self.assertEqual(self.post(srv, self.marks_path(ID_PLAIN), {"tags": [99]})[0], 400)  # unknown tag
        self.assertEqual(self.post(srv, self.marks_path(ID_PLAIN), {"starred": True}, origin=None)[0], 403)
        self.assertEqual(request(srv.server_port, "GET", "/api/messages?tag=star")[1]["total"], 0)

    def test_deleted_tag(self):
        srv = self.serve(make_library(self.tmp, self.export))
        t = self.create_tag(srv)
        self.assertEqual(self.post(srv, self.marks_path(ID_PLAIN), {"tags": [t]})[0], 200)
        self.assertEqual(self.post(srv, f"/api/tags/{t}/remove", {})[0], 200)
        self.assertEqual(request(srv.server_port, "GET", f"/api/messages?tag={t}")[1]["total"], 0)
        status, data = self.post(srv, self.marks_path(ID_PLAIN), {"tags": [t]})
        self.assertEqual(status, 400)

    def test_invalid_tag_param(self):
        srv = self.serve(make_library(self.tmp, self.export))
        for value in ["abc", "-1", "1.5"]:
            status, data = request(srv.server_port, "GET", f"/api/messages?tag={value}")
            self.assertEqual((status, data["code"]), (400, "err.invalid_tag"), value)

    def test_tags_survive_index_rebuild(self):
        root = make_library(self.tmp, self.export)
        srv = self.serve(root)
        self.assertEqual(self.post(srv, self.marks_path(ID_PLAIN), {"starred": True})[0], 200)
        self.stop(srv)
        for name in os.listdir(root):
            if name.startswith("archive.db"):
                os.remove(os.path.join(root, name))
        srv = self.serve(root)  # reopening rebuilds the index, which must not touch tags.db
        status, m = request(srv.server_port, "GET", f"/api/message/s1/{quote(ID_PLAIN, safe='')}")
        self.assertEqual((status, m["starred"]), (200, True))
        self.assertEqual(request(srv.server_port, "GET", "/api/messages?tag=star")[1]["total"], 1)

    def test_marks_while_index_job_runs(self):
        root = make_library(self.tmp, self.export)
        srv = self.serve(root)  # the mail is indexed before the blocked job starts
        gate = threading.Event()
        self.addCleanup(gate.set)  # frees the blocked job even if the test fails early
        original = server.Store.sync_source

        def slow(store, *args, **kwargs):
            gate.wait(10)
            return original(store, *args, **kwargs)

        with mock.patch.object(server.Store, "sync_source", slow):
            self.assertEqual(self.post(srv, "/api/library", {"path": root})[0], 200)
            try:
                status, data = self.post(srv, self.marks_path(ID_PLAIN), {"starred": True})
                self.assertEqual((status, data), (200, {"starred": True, "tags": []}))
            finally:
                gate.set()
            self.assertEqual(wait_job(srv.server_port)["state"], "done")
        self.assertEqual(request(srv.server_port, "GET", "/api/messages?tag=star")[1]["total"], 1)


class ImportTests(unittest.TestCase):
    """POST /api/import: a PST or OST file becomes a new outlook source. pypff is faked: open_reader
    returns a FakeReader and pypff_available is True unless a test says otherwise."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))  # the import logs go to stderr
        self.servers = []
        self.addCleanup(self.stop_all)  # runs before the redirect ends, so the jobs stay quiet
        self.library = make_library(self.tmp)
        self.sources = os.path.join(self.library, "sources")
        self.config = os.path.join(self.tmp, "config.json")
        self.pst = os.path.join(self.tmp, "mail.PST")
        write(self.pst, b"x")
        self.enterContext(mock.patch("mailviewer.server.pypff_available", return_value=True))

    def stop_all(self):
        for srv in list(self.servers):
            wait_job(srv.server_port)
            srv.shutdown()
            srv.server_close()
            if srv.viewer.store is not None:
                srv.viewer.store.close()
        self.servers = []

    def serve(self, library=True):
        """Serve self.library, or no library at all when library is False."""
        srv = server.make_server(self.library if library else None, 0, self.tmp, config_path=self.config)
        self.servers.append(srv)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        wait_job(srv.server_port)
        return srv

    def use_mail(self, *items):
        """Every import reads a fresh FakeReader with one folder holding items."""
        self.enterContext(mock.patch(
            "mailviewer.server.open_reader",
            side_effect=lambda path: FakeReader([folder(("Posteingang",), *items)]),
        ))

    def post(self, srv, body=None, *, origin="same"):
        raw = json.dumps(body if body is not None else {}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if origin == "same":
            origin = f"http://127.0.0.1:{srv.server_port}"
        if origin is not None:
            headers["Origin"] = origin
        status, _, data = request_raw(srv.server_port, "POST", "/api/import", raw, headers)
        return status, json.loads(data)

    def run_import(self, srv, name="Archiv", path=None):
        status, data = self.post(srv, {"path": path or self.pst, "name": name})
        self.assertEqual((status, data), (200, {"started": True}))
        return wait_job(srv.server_port)

    def library_sources(self):
        return Library.open(self.library).sources

    def test_import_creates_source(self):
        self.use_mail(outlook_message(subject="Eins"), outlook_message(subject="Zwei"))
        srv = self.serve()
        st = self.run_import(srv, name="Mein Archiv")
        self.assertEqual((st["state"], st["kind"]), ("done", "import"))
        self.assertEqual(st["result"]["imported"], 2)
        self.assertEqual(os.listdir(self.sources), ["mein-archiv"])
        [src] = self.library_sources()
        self.assertEqual((src.kind, src.path, src.name), ("outlook", "sources/mein-archiv", "Mein Archiv"))
        self.assertEqual(st["result"]["source"], src.id)
        status, data = request(srv.server_port, "GET", f"/api/messages?sources={src.id}")
        self.assertEqual((status, data["total"]), (200, 2))
        self.assertTrue(os.path.isfile(self.pst))  # the original file is only read

    def test_import_same_name_twice(self):
        self.use_mail(outlook_message())
        srv = self.serve()
        self.run_import(srv, name="Archiv")
        self.run_import(srv, name="Archiv")
        self.assertEqual(sorted(os.listdir(self.sources)), ["archiv", "archiv_1"])
        self.assertEqual(sorted(s.path for s in self.library_sources()),
                         ["sources/archiv", "sources/archiv_1"])

    def test_import_failure_leaves_nothing(self):
        self.use_mail(outlook_message())
        self.enterContext(mock.patch("mailviewer.server.convert", side_effect=RuntimeError("boom")))
        srv = self.serve()
        st = self.run_import(srv)
        self.assertEqual((st["state"], st["message"]), ("error", {"text": "boom"}))
        self.assertEqual(self.library_sources(), [])
        self.assertEqual(os.listdir(self.sources), [])

    def test_import_add_source_failure_leaves_nothing(self):
        self.use_mail(outlook_message())
        self.enterContext(mock.patch("mailviewer.library.Library.add_source", side_effect=ValueError("boom")))
        srv = self.serve()
        st = self.run_import(srv)
        self.assertEqual(st["state"], "error")
        self.assertEqual(self.library_sources(), [])
        self.assertEqual(os.listdir(self.sources), [])

    def test_import_cleans_stale_temp(self):
        os.makedirs(os.path.join(self.sources, ".import-old"))
        write(os.path.join(self.sources, ".import-old", "stale.eml"), b"stale")
        self.use_mail(outlook_message())
        srv = self.serve()
        st = self.run_import(srv)
        self.assertEqual(st["state"], "done")
        self.assertEqual(os.listdir(self.sources), ["archiv"])

    def test_import_nothing_readable(self):
        self.enterContext(mock.patch(
            "mailviewer.server.open_reader",
            side_effect=lambda path: FakeReader([folder(("Posteingang",), Unreadable(reason="x"))]),
        ))
        srv = self.serve()
        st = self.run_import(srv)
        self.assertEqual((st["state"], st["message"]["code"]), ("error", "err.no_readable_mails"))
        self.assertEqual(self.library_sources(), [])
        self.assertEqual(os.listdir(self.sources), [])

    def test_import_logs_counts_only(self):
        self.use_mail(outlook_message(subject="Geheim"))
        srv = self.serve()
        st = self.run_import(srv, name="Log")
        self.assertIn("Imported: 1", texts(st["log"]))
        self.assertFalse(any("Geheim" in line for line in texts(st["log"])))

    def test_import_validation(self):
        txt = os.path.join(self.tmp, "mail.txt")
        write(txt, b"x")
        srv = self.serve()
        bad = [
            {"path": txt, "name": "A"},  # wrong extension
            {"path": os.path.join(self.tmp, "missing.pst"), "name": "A"},  # no such file
            {"path": self.tmp, "name": "A"},  # a directory
            {"path": self.pst, "name": "  "},  # empty name
            {"path": self.pst, "name": "A\nB"},  # control character
            {"name": "A"},  # no path
            {"path": self.pst, "name": "A", "extra": 1},  # unknown field
        ]
        for body in bad:
            self.assertEqual(self.post(srv, body)[0], 400, body)
        self.assertEqual(self.post(srv, {"path": self.pst, "name": "A"}, origin="http://evil.example")[0], 403)
        self.assertFalse(os.path.exists(self.sources))  # refused before anything is created
        self.assertEqual(self.library_sources(), [])

    def test_import_without_pypff_is_503(self):
        srv = self.serve()
        with mock.patch("mailviewer.server.pypff_available", return_value=False):
            status, data = self.post(srv, {"path": self.pst, "name": "A"})
        self.assertEqual((status, data["code"]), (503, "err.pst_missing_pip"))
        self.assertEqual(self.library_sources(), [])

    def test_import_without_library_is_409(self):
        srv = self.serve(library=False)
        self.assertEqual(self.post(srv, {"path": self.pst, "name": "A"})[0], 409)

    def test_import_while_job_runs_is_409(self):
        gate = threading.Event()
        self.addCleanup(gate.set)  # release the held job even if the test fails early

        def held(*args, **kwargs):
            gate.wait(10)
            return ImportReport()

        self.use_mail(outlook_message())
        self.enterContext(mock.patch("mailviewer.server.convert", side_effect=held))
        srv = self.serve()
        self.assertEqual(self.post(srv, {"path": self.pst, "name": "A"}), (200, {"started": True}))
        self.assertEqual(self.post(srv, {"path": self.pst, "name": "B"})[0], 409)
        gate.set()
        self.assertEqual(wait_job(srv.server_port)["state"], "error")

    def test_import_resolves_symlinks_before_checking(self):
        txt = os.path.join(self.tmp, "mail.txt")
        write(txt, b"x")
        link_txt = os.path.join(self.tmp, "link.pst")
        os.symlink(txt, link_txt)
        self.use_mail(outlook_message())
        srv = self.serve()
        self.assertEqual(self.post(srv, {"path": link_txt, "name": "A"})[0], 400)
        link_ok = os.path.join(self.tmp, "ok.ost")
        os.symlink(self.pst, link_ok)
        self.assertEqual(self.post(srv, {"path": link_ok, "name": "A"})[0], 200)
        self.assertEqual(wait_job(srv.server_port)["state"], "done")

    def test_import_name_cannot_escape_sources(self):
        self.use_mail(outlook_message())
        srv = self.serve()
        self.run_import(srv, name="../evil")
        self.assertEqual(os.listdir(self.sources), ["---evil"])

    def test_import_slug(self):
        self.assertEqual(server.import_slug("Müller & Söhne"), "müller---söhne")
        self.assertEqual(server.import_slug("ÄÖÜß"), "äöüß")
        self.assertEqual(server.import_slug("x" * 50), "x" * 40)
        self.assertEqual(server.import_slug("..."), "---")
        self.assertEqual(server.import_slug(""), "outlook")


class CleanupRouteTests(unittest.TestCase):
    """Combined views and the cleanup routes. s1 is the maildir export, s2 an imported Outlook copy of
    it under sources/. Each test gets its own library and server."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))  # the index logs skipped files
        self.servers = []
        self.addCleanup(self.stop_all)  # runs before the redirect ends, so the jobs stay quiet
        self.export = make_export(self.tmp, "export")
        self.config = os.path.join(self.tmp, "config.json")

    def stop_all(self):
        for srv in list(self.servers):
            wait_job(srv.server_port)
            self.stop(srv)

    def stop(self, srv):
        if srv in self.servers:
            self.servers.remove(srv)
        srv.shutdown()
        srv.server_close()
        if srv.viewer.store is not None:
            srv.viewer.store.close()
        if srv.viewer.tags is not None:
            srv.viewer.tags.close()

    def serve(self, library_dir, wait=True):
        srv = server.make_server(library_dir, 0, self.tmp, config_path=self.config)
        self.servers.append(srv)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        if wait:
            wait_job(srv.server_port)
        return srv

    def archive_library(self):
        """Library with the maildir export s1 and a copy of it as the imported Outlook source s2."""
        root = make_library(self.tmp, self.export)
        archive = os.path.join(root, "sources", "archiv")
        shutil.copytree(self.export, archive)
        Library.open(root).add_source(archive, "Archiv", kind="outlook")
        return root

    def post(self, srv, path, body=None, *, raw=None, origin="same"):
        """POST and return (status, parsed JSON). Origin defaults to the server's own."""
        if raw is None:
            raw = json.dumps(body if body is not None else {}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if origin == "same":
            origin = f"http://127.0.0.1:{srv.server_port}"
        if origin is not None:
            headers["Origin"] = origin
        status, _, data = request_raw(srv.server_port, "POST", path, raw, headers)
        return status, json.loads(data)

    def cleanup_body(self, action="link", target="s2", reference="s1"):
        return {"target": target, "reference": reference, "action": action}

    def preview(self, srv, query):
        return request(srv.server_port, "GET", f"/api/cleanup/preview?{query}")

    def cleanup_logs(self, root):
        return sorted(n for n in os.listdir(root) if n.startswith("cleanup-") and n.endswith(".log"))

    def test_combined_list_merges_copies(self):
        srv = self.serve(self.archive_library())
        status, data = request(srv.server_port, "GET", "/api/messages?sources=s1,s2")
        self.assertEqual((status, data["total"]), (200, 5))
        merged = sorted(m["id"] for m in data["messages"] if m["copies"] == 2)
        self.assertEqual(merged, sorted([ID_PLAIN, ID_ALT, ID_RICH]))
        self.assertTrue(all(m["source"] == "s1" for m in data["messages"] if m["copies"] == 2))
        ghosts = [m for m in data["messages"] if m["id"] == ID_GHOST]
        self.assertEqual([m["copies"] for m in ghosts], [1, 1])  # no identity, never merged

        status, data = request(srv.server_port, "GET", "/api/messages?sources=s1")
        self.assertEqual(data["total"], 4)
        self.assertTrue(all(m["copies"] == 1 for m in data["messages"]))

    def test_representative_follows_source_order(self):
        srv = self.serve(self.archive_library())
        status, data = request(srv.server_port, "GET", "/api/messages?sources=s2,s1")
        merged = [m for m in data["messages"] if m["copies"] == 2]
        self.assertEqual(len(merged), 3)
        self.assertTrue(all(m["source"] == "s2" for m in merged))

    def test_folders_and_tags_count_merged_mails(self):
        srv = self.serve(self.archive_library())
        status, folders = request(srv.server_port, "GET", "/api/folders?sources=s1,s2")
        counts = {f["key"]: f["count"] for f in folders}
        self.assertEqual(counts["sys:0"], 4)  # ID_PLAIN, ID_RICH and two ghosts
        status, data = request(srv.server_port, "GET", "/api/messages?sources=s1,s2&folder=sys:0")
        self.assertEqual(data["total"], 4)
        status, tags = request(srv.server_port, "GET", "/api/tags?sources=s1,s2")
        self.assertEqual(tags["star"], 0)

    def test_detail_lists_copies_and_hides_hidden_sources(self):
        srv = self.serve(self.archive_library())
        path = f"/api/message/s1/{quote(ID_PLAIN, safe='')}"
        status, m = request(srv.server_port, "GET", path)
        self.assertEqual([(c["source"], c["name"]) for c in m["copies"]], [("s1", "export"), ("s2", "Archiv")])
        self.assertIn("Inbox", m["copies"][0]["folders"])
        self.assertEqual(self.post(srv, "/api/sources/s2", {"hidden": True})[0], 200)
        status, m = request(srv.server_port, "GET", path)
        self.assertEqual([c["source"] for c in m["copies"]], ["s1"])

    def test_preview_counts_pairs(self):
        srv = self.serve(self.archive_library())
        status, data = self.preview(srv, "target=s2&reference=s1")
        self.assertEqual(status, 200)
        self.assertEqual((data["count"], data["link_possible"], data["target"], data["reference"]),
                         (3, True, "s2", "s1"))

    def test_preview_and_post_reject_bad_requests_and_change_nothing(self):
        root = self.archive_library()
        archive = os.path.join(root, "sources", "archiv")
        srv = self.serve(root)
        before = (snapshot(archive), snapshot(self.export))
        bad_previews = [
            ("target=s1&reference=s2", 400),  # maildir target
            ("target=s2&reference=s2", 400),  # same source
            ("target=s2&reference=s9", 404),  # unknown reference
            ("target=s9&reference=s1", 404),  # unknown target
            ("target=s2", 400),  # missing reference
            ("reference=s1", 400),  # missing target
        ]
        for query, status in bad_previews:
            self.assertEqual(self.preview(srv, query)[0], status, query)
        bad_bodies = [
            (self.cleanup_body(target="s1"), 400),
            (self.cleanup_body(reference="s2"), 400),
            (self.cleanup_body(target="s2", reference="s2"), 400),
            (self.cleanup_body(reference="s9"), 404),
            (self.cleanup_body(action="move"), 400),
            ({"target": "s2", "reference": "s1"}, 400),  # no action
            (self.cleanup_body() | {"force": True}, 400),  # unknown key
            ({"target": 5, "reference": "s1", "action": "link"}, 400),  # wrong type
            ({"target": ["s2"], "reference": "s1", "action": "link"}, 400),
        ]
        for body, status in bad_bodies:
            self.assertEqual(self.post(srv, "/api/cleanup", body)[0], status, body)
        self.assertEqual(self.post(srv, "/api/cleanup", raw=b"[]")[0], 400)
        self.assertEqual((snapshot(archive), snapshot(self.export)), before)
        self.assertEqual(self.cleanup_logs(root), [])

    def test_hidden_reference_is_400(self):
        srv = self.serve(self.archive_library())
        self.assertEqual(self.post(srv, "/api/sources/s1", {"hidden": True})[0], 200)
        self.assertEqual(self.preview(srv, "target=s2&reference=s1")[0], 400)
        self.assertEqual(self.post(srv, "/api/cleanup", self.cleanup_body())[0], 400)

    def test_cleanup_link_job(self):
        root = self.archive_library()
        archive = os.path.join(root, "sources", "archiv")
        srv = self.serve(root)
        before = snapshot(self.export)
        status, data = self.post(srv, "/api/cleanup", self.cleanup_body("link"))
        self.assertEqual((status, data), (202, {"state": "running"}))
        job = wait_job(srv.server_port)
        self.assertEqual((job["state"], job["kind"]), ("done", "cleanup"))
        self.assertEqual(job["result"]["linked"], 3)
        for mid in (ID_PLAIN, ID_ALT, ID_RICH):
            self.assertTrue(os.path.samefile(os.path.join(archive, mid + ".eml"),
                                             os.path.join(self.export, mid + ".eml")))
        logs = self.cleanup_logs(root)
        self.assertEqual(logs, [job["result"]["log_file"]])
        with open(os.path.join(root, logs[0]), encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(sum(line.startswith("link ") for line in lines), 3)
        status, data = request(srv.server_port, "GET", "/api/messages?sources=s2")
        self.assertEqual(data["total"], 4)
        self.assertEqual(snapshot(self.export), before)  # the reference is only read

    def test_cleanup_delete_job(self):
        root = self.archive_library()
        srv = self.serve(root)
        self.assertEqual(self.post(srv, f"/api/message/s1/{quote(ID_PLAIN, safe='')}/marks",
                                   {"starred": True})[0], 200)
        status, data = self.post(srv, "/api/cleanup", self.cleanup_body("delete"))
        self.assertEqual(status, 202)
        job = wait_job(srv.server_port)
        self.assertEqual((job["state"], job["result"]["deleted"]), ("done", 3))
        status, data = request(srv.server_port, "GET", "/api/messages?sources=s2")
        self.assertEqual([m["id"] for m in data["messages"]], [ID_GHOST])
        # the star belongs to the mail's identity, so it still shows through the reference
        status, data = request(srv.server_port, "GET", "/api/messages?sources=s1&tag=star")
        self.assertEqual([m["id"] for m in data["messages"]], [ID_PLAIN])

    def test_cleanup_reindexes_target_after_failure(self):
        srv = self.serve(self.archive_library())
        calls = []
        original = server.Store.sync_source

        def spy(store, source_id, *args, **kwargs):
            calls.append(source_id)
            return original(store, source_id, *args, **kwargs)

        with mock.patch.object(server.Store, "sync_source", spy), \
                mock.patch("mailviewer.server.run_cleanup", side_effect=RuntimeError("boom")):
            self.assertEqual(self.post(srv, "/api/cleanup", self.cleanup_body())[0], 202)
            job = wait_job(srv.server_port)
        self.assertEqual((job["state"], job["message"]), ("error", {"text": "boom"}))
        self.assertEqual(calls, ["s2"])

    def test_cleanup_busy_while_job_runs(self):
        gate = threading.Event()
        self.addCleanup(gate.set)  # frees the held job even if the test fails early

        def held(*args, **kwargs):
            gate.wait(10)
            return CleanupResult()

        srv = self.serve(self.archive_library())
        with mock.patch("mailviewer.server.run_cleanup", side_effect=held):
            self.assertEqual(self.post(srv, "/api/cleanup", self.cleanup_body())[0], 202)
            self.assertEqual(self.post(srv, "/api/cleanup", self.cleanup_body("delete"))[0], 409)
            gate.set()
            self.assertEqual(wait_job(srv.server_port)["state"], "done")

    def test_link_across_file_systems_is_400_delete_allowed(self):
        root = self.archive_library()
        real_stat = os.stat

        def other_device(path, *args, **kwargs):
            st = real_stat(path, *args, **kwargs)
            if os.path.realpath(path) == os.path.realpath(self.export):  # the reference only
                fields = list(st)
                fields[2] += 1  # st_dev
                return os.stat_result(fields[:10])
            return st

        srv = self.serve(root)
        with mock.patch("mailviewer.cleanup.os.stat", side_effect=other_device):
            status, data = self.post(srv, "/api/cleanup", self.cleanup_body("link"))
            self.assertEqual(status, 400)
            self.assertEqual(data["code"], "err.cross_device")
            self.assertEqual(self.cleanup_logs(root), [])
            status, data = self.post(srv, "/api/cleanup", self.cleanup_body("delete"))
            self.assertEqual(status, 202)
            self.assertEqual(wait_job(srv.server_port)["result"]["deleted"], 3)

    def test_cleanup_needs_origin(self):
        srv = self.serve(self.archive_library())
        self.assertEqual(self.post(srv, "/api/cleanup", self.cleanup_body(), origin=None)[0], 403)
        self.assertEqual(self.post(srv, "/api/cleanup", self.cleanup_body(),
                                   origin="http://evil.example")[0], 403)
        self.assertEqual(wait_job(srv.server_port)["kind"], "index")  # only the initial index ran


class ArchiveServerTests(unittest.TestCase):
    """Helpers for archive tests: a library with the export s1, a server per test, requests and folder jobs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))  # the index logs skipped files
        self.servers = []
        self.addCleanup(self.stop_all)
        self.export = make_export(self.tmp, "export")
        self.config = os.path.join(self.tmp, "config.json")

    def stop_all(self):
        for srv in list(self.servers):
            wait_job(srv.server_port)
            self.stop(srv)

    def stop(self, srv):
        if srv in self.servers:
            self.servers.remove(srv)
        srv.shutdown()
        srv.server_close()
        if srv.viewer.store is not None:
            srv.viewer.store.close()
        if srv.viewer.tags is not None:
            srv.viewer.tags.close()

    def serve(self, library_dir, cli=None):
        srv = server.make_server(library_dir, 0, self.tmp, cli=cli, config_path=self.config)
        self.servers.append(srv)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        wait_job(srv.server_port)
        return srv

    def post(self, srv, path, body=None, *, origin="same"):
        headers = {"Content-Type": "application/json"}
        if origin == "same":
            origin = f"http://127.0.0.1:{srv.server_port}"
        if origin is not None:
            headers["Origin"] = origin
        status, _, data = request_raw(srv.server_port, "POST", path,
                                      json.dumps(body if body is not None else {}).encode("utf-8"), headers)
        return status, json.loads(data)

    def create(self, srv, name="Privat"):
        return self.post(srv, "/api/archives", {"name": name})

    def folder_op(self, srv, sid, body):
        return self.post(srv, f"/api/archives/{sid}/folders", body)

    def folders(self, srv, sources="s2"):
        return request(srv.server_port, "GET", f"/api/folders?sources={sources}")[1]

    def run_folder_op(self, srv, body):
        """Start a folder job on s2 and return the finished job."""
        self.assertEqual(self.folder_op(srv, "s2", body), (200, {}))
        job = wait_job(srv.server_port)
        self.assertEqual((job["state"], job["kind"]), ("done", "archive"), job.get("message"))
        return job


class ArchiveRouteTests(ArchiveServerTests):
    """Archive creation, folder jobs and the archive fields of source_list. Each test gets its own server."""

    def test_create_archive(self):
        srv = self.serve(make_library(self.tmp, self.export))
        status, data = self.create(srv)
        self.assertEqual(status, 200)
        self.assertEqual((data["id"], data["kind"], data["available"], data["count"]), ("s2", "archive", True, 0))
        status, again = self.create(srv)
        self.assertEqual(status, 200)
        self.assertNotEqual(again["id"], data["id"])
        lib = Library.open(os.path.join(self.tmp, "library"))
        dirs = {s.id: os.path.basename(lib.abspath(s)) for s in lib.sources if s.kind == "archive"}
        self.assertEqual(dirs, {"s2": "privat", "s3": "privat_1"})
        self.assertEqual(self.create(srv, "  ")[0], 400)
        self.assertEqual(self.post(srv, "/api/archives", {"name": "X", "kind": "archive"})[0], 400)
        self.assertEqual(self.post(srv, "/api/archives", {"name": "X"}, origin=None)[0], 403)

    def test_create_archive_removes_directory_on_failure(self):
        srv = self.serve(make_library(self.tmp, self.export))
        with mock.patch("mailviewer.server.write_empty_archive", side_effect=OSError("disk full")):
            status, data = self.create(srv, "Kaputt")
        self.assertEqual((status, data), (400, {"error": "disk full"}))
        self.assertEqual(os.listdir(os.path.join(self.tmp, "library", "sources")), [])
        self.assertEqual([s["id"] for s in request(srv.server_port, "GET", "/api/sources")[1]], ["s1"])

    def test_folder_ops_via_job(self):
        srv = self.serve(make_library(self.tmp, self.export))
        self.assertEqual(self.create(srv)[0], 200)
        job = self.run_folder_op(srv, {"op": "create", "name": "Reisen"})
        reisen = job["result"]["id"]
        folder = next(f for f in self.folders(srv) if f["name"] == "Reisen")
        self.assertEqual((folder["count"], folder["labels"]), (0, {"s2": reisen}))
        urlaub = self.run_folder_op(srv, {"op": "create", "name": "Urlaub", "parent": reisen})["result"]["id"]
        self.run_folder_op(srv, {"op": "rename", "id": reisen, "name": "Reise"})
        paths = {f["name"]: f["path"] for f in self.folders(srv)}
        self.assertEqual(paths, {"Reise": "Reise", "Urlaub": "Reise/Urlaub"})
        self.run_folder_op(srv, {"op": "move", "id": urlaub, "parent": None, "index": 0})
        self.assertEqual({f["name"]: f["path"] for f in self.folders(srv)}, {"Reise": "Reise", "Urlaub": "Urlaub"})
        self.run_folder_op(srv, {"op": "delete", "id": reisen})
        self.assertEqual([f["name"] for f in self.folders(srv)], ["Urlaub"])

    def test_folder_op_rejections(self):
        srv = self.serve(make_library(self.tmp, self.export))
        self.assertEqual(self.create(srv)[0], 200)
        self.assertEqual(self.folder_op(srv, "s2", {"op": "rename", "id": "5", "name": "X"})[0], 400)  # All Mail
        self.assertEqual(self.folder_op(srv, "s2", {"op": "delete", "id": "nope"})[0], 400)
        self.assertEqual(self.folder_op(srv, "s2", {"op": "create", "name": "X", "parent": "nope"})[0], 400)
        self.assertEqual(self.folder_op(srv, "s2", {"op": "explode"})[0], 400)
        self.assertEqual(self.folder_op(srv, "s2", {"op": "create"})[0], 400)
        self.assertEqual(self.folder_op(srv, "s2", {"op": "create", "name": 5})[0], 400)
        self.assertEqual(self.folder_op(srv, "s2", {"op": "create", "name": "X", "extra": 1})[0], 400)
        self.assertEqual(self.folder_op(srv, "s1", {"op": "create", "name": "X"})[0], 400)  # not an archive
        self.assertEqual(self.folder_op(srv, "s9", {"op": "create", "name": "X"})[0], 404)
        self.assertEqual(self.post(srv, "/api/archives/s2/folders", {"op": "create", "name": "X"},
                                   origin=None)[0], 403)
        self.assertEqual(wait_job(srv.server_port)["kind"], "index")  # no folder job was started

    def test_source_list_archive_fields(self):
        account = os.path.join(self.tmp, "account")
        make_export(account, "mail_20260101T000000")  # a proton account: s1
        srv = self.serve(make_library(self.tmp, account))
        self.assertEqual(self.create(srv)[0], 200)
        _, sources = request(srv.server_port, "GET", "/api/sources")
        by_id = {s["id"]: s for s in sources}
        self.assertEqual((by_id["s1"]["archive"], by_id["s1"]["archive_target"], by_id["s1"]["auto_paused"]),
                         (False, "", None))
        self.assertEqual((by_id["s2"]["archive"], by_id["s2"]["archive_target"], by_id["s2"]["auto_paused"]),
                         (True, "", None))
        srv.viewer.auto_paused["s1"] = 3
        _, sources = request(srv.server_port, "GET", "/api/sources")
        self.assertEqual(next(s for s in sources if s["id"] == "s1")["auto_paused"], {"missing": 3})
        self.assertEqual(self.post(srv, "/api/sources/s1", {"archive_target": "s2"})[0], 200)
        _, sources = request(srv.server_port, "GET", "/api/sources")
        self.assertEqual(next(s for s in sources if s["id"] == "s1")["archive_target"], "s2")
        status, data = self.post(srv, "/api/sources/s2", {"archive_target": "s1"})
        self.assertEqual((status, data["code"]), (400, "err.archive_proton_only"))  # only proton sources archive


class ArchiveTransferTests(ArchiveServerTests):
    """POST /api/transfer: copy, move and refile mails into archives, and the archived marker."""

    def serve_proton(self):
        """Library whose s1 is a Proton export, so copies come from a source that is only read."""
        self.account = os.path.join(self.tmp, "account")
        make_export(self.account, "mail_20260101T000000")
        return self.serve(make_library(self.tmp, self.account))

    def transfer(self, srv, items, archive="s2", folder=None, new_folder=None, origin="same"):
        body = {"items": [{"source": s, "id": i} for s, i in items], "archive": archive,
                "folder": folder, "new_folder": new_folder}
        return self.post(srv, "/api/transfer", body, origin=origin)

    def run_transfer(self, srv, items, **kwargs):
        """Start a transfer and return the finished job."""
        self.assertEqual(self.transfer(srv, items, **kwargs), (200, {}))
        return self.wait_done(srv)

    def wait_done(self, srv):
        job = wait_job(srv.server_port)
        self.assertEqual((job["state"], job["kind"]), ("done", "archive"), job.get("message"))
        return job

    def folder_job(self, srv, sid, body):
        self.assertEqual(self.folder_op(srv, sid, body), (200, {}))
        return self.wait_done(srv)

    def messages(self, srv, sources, **params):
        return request(srv.server_port, "GET", "/api/messages?" + urlencode({"sources": sources, **params}))[1]

    def only_mid(self, srv, sid):
        """The id of the one mail in archive sid (archive copies have their own ids)."""
        body = self.messages(srv, sid)
        self.assertEqual(body["total"], 1)
        return body["messages"][0]["id"]

    def eml_files(self, name):
        return sorted(x for x in os.listdir(os.path.join(self.tmp, "library", "sources", name)) if x.endswith(".eml"))

    def test_transfer_copy_from_proton(self):
        srv = self.serve_proton()
        self.assertEqual(self.create(srv)[0], 200)
        reisen = self.folder_job(srv, "s2", {"op": "create", "name": "Reisen"})["result"]["id"]
        eml = os.path.join(self.account, "mail_20260101T000000", ID_PLAIN + ".eml")
        meta = os.path.join(self.account, "mail_20260101T000000", ID_PLAIN + ".metadata.json")
        before = [open(p, "rb").read() for p in (eml, meta)]

        self.assertEqual(self.run_transfer(srv, [("s1", ID_PLAIN)], folder=reisen)["result"]["copied"], 1)
        self.assertEqual([open(p, "rb").read() for p in (eml, meta)], before)

        key = next(f["key"] for f in self.folders(srv) if f["name"] == "Reisen")
        listed = self.messages(srv, "s2", folder=key)
        self.assertEqual([m["subject"] for m in listed["messages"]], ["Grüße aus Köln"])
        rows = {m["id"]: m for m in self.messages(srv, "s1")["messages"]}
        self.assertEqual(rows[ID_PLAIN]["archived"], ["s2"])
        self.assertEqual(rows[ID_ALT]["archived"], [])
        virtual = [f for f in self.folders(srv, "s1") if f["key"] == "virtual:archived"]
        self.assertEqual((virtual[0]["count"], virtual[0]["name"]), (1, "Still to delete online"))
        only = self.messages(srv, "s1", folder="virtual:archived")
        self.assertEqual([m["id"] for m in only["messages"]], [ID_PLAIN])

    def test_transfer_move_between_archives_and_refile(self):
        srv = self.serve_proton()
        self.assertEqual(self.create(srv, "Privat")[0], 200)  # s2
        self.assertEqual(self.create(srv, "Arbeit")[0], 200)  # s3
        self.run_transfer(srv, [("s1", ID_PLAIN)], archive="s2")
        mid = self.only_mid(srv, "s2")

        job = self.run_transfer(srv, [("s2", mid)], archive="s3")
        self.assertEqual(job["result"]["deleted_origin"], 1)
        self.assertEqual(self.messages(srv, "s2")["total"], 0)
        self.assertEqual(self.eml_files("privat"), [])
        self.assertEqual(len(self.eml_files("arbeit")), 1)

        mid = self.only_mid(srv, "s3")
        reisen = self.folder_job(srv, "s3", {"op": "create", "name": "Reisen"})["result"]["id"]
        job = self.run_transfer(srv, [("s3", mid)], archive="s3", folder=reisen)
        self.assertEqual((job["result"]["refiled"], job["result"]["copied"]), (1, 0))
        self.assertEqual(len(self.eml_files("arbeit")), 1)
        key = next(f["key"] for f in self.folders(srv, "s3") if f["name"] == "Reisen")
        self.assertEqual(self.messages(srv, "s3", folder=key)["total"], 1)

    def test_transfer_new_folder_in_same_job(self):
        srv = self.serve_proton()
        self.assertEqual(self.create(srv)[0], 200)
        job = self.run_transfer(srv, [("s1", ID_PLAIN)], new_folder={"name": "Neu", "parent": None})
        self.assertEqual(job["result"]["copied"], 1)
        neu = next(f for f in self.folders(srv) if f["name"] == "Neu")
        self.assertEqual(neu["count"], 1)

        reisen = neu["labels"]["s2"]
        self.run_transfer(srv, [("s1", ID_ALT)], new_folder={"name": "Unter", "parent": reisen})
        unter = next(f for f in self.folders(srv) if f["name"] == "Unter")
        self.assertEqual((unter["path"], unter["count"]), ("Neu/Unter", 1))

    def test_transfer_rejections(self):
        srv = self.serve_proton()
        self.assertEqual(self.create(srv)[0], 200)
        self.assertEqual(self.transfer(srv, [("s1", "nope")])[0], 400)  # not indexed
        self.assertEqual(self.transfer(srv, [("s1", "../x")])[0], 400)  # path traversal
        self.assertEqual(self.transfer(srv, [("s9", ID_PLAIN)])[0], 404)  # unknown source
        self.assertEqual(self.transfer(srv, [])[0], 400)
        self.assertEqual(self.transfer(srv, [("s1", ID_PLAIN)] * 5001)[0], 400)
        self.assertEqual(self.transfer(srv, [("s1", ID_PLAIN)], folder="X1",
                                       new_folder={"name": "N", "parent": None})[0], 400)
        self.assertEqual(self.transfer(srv, [("s1", ID_PLAIN)], folder="nope")[0], 400)
        self.assertEqual(self.transfer(srv, [("s1", ID_PLAIN)], new_folder={"name": " ", "parent": None})[0], 400)
        self.assertEqual(self.transfer(srv, [("s1", ID_PLAIN)], new_folder={"name": "N", "parent": "nope"})[0], 400)
        self.assertEqual(self.transfer(srv, [("s1", ID_PLAIN)], archive="s1")[0], 400)  # not an archive
        self.assertEqual(self.transfer(srv, [("s1", ID_PLAIN)], origin="http://evil.test")[0], 403)
        self.assertEqual(self.messages(srv, "s2")["total"], 0)
        self.assertEqual(wait_job(srv.server_port)["kind"], "index")  # no transfer job was started

        privat = os.path.join(self.tmp, "library", "sources", "privat")
        os.rename(privat, privat + "_weg")
        status, data = self.transfer(srv, [("s1", ID_PLAIN)])
        self.assertEqual((status, data["code"]), (400, "err.source_unreachable"))
        os.rename(privat + "_weg", privat)
        os.rename(self.account, self.account + "_weg")
        status, data = self.transfer(srv, [("s1", ID_PLAIN)])
        self.assertEqual((status, data["code"]), (400, "err.source_unreachable"))
        os.rename(self.account + "_weg", self.account)

        gate = threading.Event()
        self.assertTrue(srv.viewer.job.start("index", lambda job: gate.wait(5)))
        self.assertEqual(self.transfer(srv, [("s1", ID_PLAIN)])[0], 409)
        gate.set()
        wait_job(srv.server_port)


FEED_CLI = '''import json, os, shutil, sys

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "next.json"), encoding="utf-8") as f:
    feed = json.load(f)  # the export this run brings: a copy of feed["src"], named feed["name"]
if len(sys.argv) != 3 or sys.argv[1] != "--dir":
    sys.exit(6)
parent = os.path.join(sys.argv[2], os.environ["ET_USER_EMAIL"])
os.makedirs(parent, exist_ok=True)
shutil.copytree(feed["src"], os.path.join(parent, feed["name"]))
'''


def write_export(parent: str, name: str, mails: list[tuple[str, list[str]]], ghost: bool = False) -> str:
    """Export with one mail per (key, label ids); the subject is the key. ghost adds metadata without
    an .eml, which counts as unreadable."""
    export = os.path.join(parent, name)
    os.makedirs(export)
    write_json(os.path.join(export, "labels.json"), LABELS)
    for key, labels in mails:
        mid = "m_" + key
        write(os.path.join(export, mid + ".eml"), f"Message-ID: <{key}@example.test>\nSubject: {key}\n\nHallo\n")
        write_json(os.path.join(export, mid + ".metadata.json"), meta(
            mid, key, ("Sam Sender", "sam@example.test"), [("Ann", "ann@example.test")], 1700000000, labels))
    if ghost:
        write_json(os.path.join(export, "m_ghost.metadata.json"), meta(
            "m_ghost", "ghost", ("Gus Geist", "gus@example.test"), [], 1700000000, ["0"]))
    return export


# Mails every export keeps: a few vanished mails stay below the brake's ratio.
BASE = [(f"b{n:02d}", ["0"]) for n in range(20)]


class AutoArchiveTests(ArchiveServerTests):
    """Automatic archive after a fetch: mails that vanished from the Proton source go to its archive target,
    then the old exports are deleted. Every failure keeps the exports. A stand-in CLI brings the export
    named in bin/next.json, so several fetches in a row make several mail_* exports."""

    def setUp(self):
        super().setUp()
        self.account = os.path.join(self.tmp, ACCOUNT)
        self.cli = os.path.join(self.tmp, "bin", CLI_NAME)
        os.makedirs(os.path.dirname(self.cli))
        write(self.cli, "#!/usr/bin/env python3\n" + FEED_CLI)
        os.chmod(self.cli, 0o755)
        self.archive_dir = os.path.join(self.tmp, "library", "sources", "privat")

    def start(self, initial, ghost=False):
        """Account with the export OLD holding BASE and the given mails; server with s1 = account and cli."""
        write_export(self.account, OLD, BASE + initial, ghost)
        return self.serve(make_library(self.tmp, self.account), cli=self.cli)

    def target(self, srv):
        """Archive s2 as the archive target of s1."""
        self.assertEqual(self.create(srv)[0], 200)
        self.assertEqual(self.post(srv, "/api/sources/s1", {"archive_target": "s2"})[0], 200)

    def fetch(self, srv, name, mails, ghost=False):
        """Bring the export name with the given mails as the next one; returns the finished fetch job."""
        src = write_export(os.path.join(self.tmp, "feeds"), name, BASE + mails, ghost)
        write_json(os.path.join(self.tmp, "bin", "next.json"), {"src": src, "name": name})
        self.assertEqual(self.post(srv, "/api/fetch", {"source": "s1", "password": PW}), (202, {"state": "running"}))
        job = wait_job(srv.server_port)
        self.assertEqual((job["state"], job["kind"]), ("done", "fetch"), job.get("message"))
        return job

    def exports(self):
        return sorted(os.path.basename(d) for d in exports.mail_dirs(self.account))

    def archived(self):
        """(subject, LabelIDs) of each mail in the archive s2, sorted. The subject is the mail's key."""
        out = []
        for name in os.listdir(self.archive_dir):
            if name.endswith(".metadata.json"):
                with open(os.path.join(self.archive_dir, name), encoding="utf-8") as f:
                    payload = json.load(f)["Payload"]
                out.append((payload["Subject"], payload["LabelIDs"]))
        return sorted(out)

    def unsorted_id(self):
        with open(os.path.join(self.archive_dir, "labels.json"), encoding="utf-8") as f:
            return next(x["ID"] for x in json.load(f)["Payload"] if x["Name"] == "Unsortiert")

    def source(self, srv, sid):
        return next(s for s in request(srv.server_port, "GET", "/api/sources")[1] if s["id"] == sid)

    def test_fetch_without_target_keeps_old_exports(self):
        srv = self.start([("X", ["0"]), ("Y", ["0"])])
        self.fetch(srv, "mail_20260102T000000", [("Y", ["0"])])
        self.fetch(srv, "mail_20260103T000000", [("Y", ["0"]), ("Z", ["0"])])
        self.fetch(srv, "mail_20260104T000000", [("Z", ["0"])])
        self.assertEqual(self.exports(), [OLD, "mail_20260102T000000", "mail_20260103T000000", "mail_20260104T000000"])

    def test_vanished_mail_archived_then_old_exports_deleted(self):
        srv = self.start([("X", ["0"]), ("Y", ["0"])])
        self.target(srv)
        job = self.fetch(srv, "mail_20260102T000000", [("Y", ["0"])])
        self.assertEqual(self.archived(), [("X", [self.unsorted_id(), "5"])])
        self.assertEqual(self.exports(), ["mail_20260102T000000"])
        self.assertTrue(any(line.startswith("archived 1,") for line in texts(job["log"])), job["log"])
        self.assertEqual(self.source(srv, "s2")["count"], 1)

    def test_already_archived_mail_skipped(self):
        srv = self.start([("X", ["0"]), ("Y", ["0"])])
        self.target(srv)
        self.assertEqual(self.post(srv, "/api/transfer", {
            "items": [{"source": "s1", "id": "m_X"}], "archive": "s2", "folder": None, "new_folder": None}), (200, {}))
        self.assertEqual(wait_job(srv.server_port)["state"], "done")
        before = self.archived()
        job = self.fetch(srv, "mail_20260102T000000", [("Y", ["0"])])
        self.assertEqual(self.archived(), before)
        self.assertEqual(self.exports(), ["mail_20260102T000000"])
        self.assertIn("archived 0, skipped 1 (already archived 1, spam/trash/draft 0), exports deleted 1", texts(job["log"]))

    def test_no_new_candidates_does_not_recreate_inbox_folder(self):
        srv = self.start([("X", ["0"]), ("Y", ["0"])])
        self.target(srv)
        self.assertEqual(self.post(srv, "/api/transfer", {
            "items": [{"source": "s1", "id": "m_X"}], "archive": "s2", "folder": None, "new_folder": None}), (200, {}))
        self.assertEqual(wait_job(srv.server_port)["state"], "done")
        self.assertNotIn("Unsortiert", self.archive_names())
        self.fetch(srv, "mail_20260102T000000", [("Y", ["0"])])
        self.assertNotIn("Unsortiert", self.archive_names())

    def archive_names(self):
        with open(os.path.join(self.archive_dir, "labels.json"), encoding="utf-8") as f:
            return [x["Name"] for x in json.load(f)["Payload"]]

    def test_spam_and_trash_not_archived_but_exports_deleted(self):
        srv = self.start([("X", ["0"]), ("S", ["3"]), ("T", ["4"]), ("Y", ["0"])])
        self.target(srv)
        job = self.fetch(srv, "mail_20260102T000000", [("Y", ["0"])])
        self.assertEqual(self.archived(), [("X", [self.unsorted_id(), "5"])])
        self.assertEqual(self.exports(), ["mail_20260102T000000"])
        self.assertIn("archived 1, skipped 2 (already archived 0, spam/trash/draft 2), exports deleted 1", texts(job["log"]))

    def test_brake_pauses_and_keeps_everything(self):
        mails = [(f"k{n:03d}", ["0"]) for n in range(100)]
        srv = self.start(mails)
        self.target(srv)
        job = self.fetch(srv, "mail_20260102T000000", mails[:50])
        self.assertEqual(self.archived(), [])
        self.assertEqual(self.exports(), [OLD, "mail_20260102T000000"])
        self.assertEqual(self.source(srv, "s1")["auto_paused"], {"missing": 50})
        self.assertIn("Automatic archiving paused: 50 mails are missing from the new export", texts(job["log"]))

        self.assertEqual(self.auto(srv, "s1", "discard"), (200, {}))
        self.assertIsNone(self.source(srv, "s1")["auto_paused"])
        self.assertEqual(self.archived(), [])
        self.assertEqual(self.exports(), [OLD, "mail_20260102T000000"])

        self.fetch(srv, "mail_20260103T000000", mails[:50])  # brakes again: the same 50 are missing
        self.assertEqual(self.source(srv, "s1")["auto_paused"], {"missing": 50})
        job = self.auto_job(srv, "s1", "force")
        self.assertEqual(self.archived(), sorted((k, [self.unsorted_id(), "5"]) for k, _ in mails[50:]))
        self.assertEqual(self.exports(), ["mail_20260103T000000"])
        self.assertIsNone(self.source(srv, "s1")["auto_paused"])
        self.assertTrue(any(line.startswith("archived 50,") for line in texts(job["log"])), job["log"])

    def auto(self, srv, sid, action):
        return self.post(srv, f"/api/sources/{sid}/auto", {"action": action})

    def auto_job(self, srv, sid, action):
        """Force the pause of sid as the job; returns the finished job."""
        self.assertEqual(self.auto(srv, sid, action), (200, {}))
        job = wait_job(srv.server_port)
        self.assertEqual((job["state"], job["kind"]), ("done", "archive"), job.get("message"))
        return job

    def test_auto_without_pending_pause_is_400(self):
        srv = self.start([("X", ["0"]), ("Y", ["0"])])
        self.target(srv)
        self.assertEqual(self.auto(srv, "s1", "discard"), (400, {"error": "There is no paused automatic run.", "code": "err.no_paused_auto", "params": {}}))
        self.assertEqual(self.auto(srv, "s1", "force")[0], 400)
        self.assertEqual(self.exports(), [OLD])

    def test_auto_rejects_bad_body_and_wrong_origin(self):
        srv = self.start([("X", ["0"])])
        self.target(srv)
        srv.viewer.auto_paused["s1"] = 3
        self.assertEqual(self.post(srv, "/api/sources/s1/auto", {"action": "explode"})[0], 400)
        self.assertEqual(self.post(srv, "/api/sources/s1/auto", {"action": "discard", "x": 1})[0], 400)
        self.assertEqual(self.post(srv, "/api/sources/s1/auto", {"action": "discard"}, origin=None)[0], 403)
        evil = {"action": "discard"}
        self.assertEqual(self.post(srv, "/api/sources/s1/auto", evil, origin="http://evil.test")[0], 403)
        self.assertEqual(srv.viewer.auto_paused, {"s1": 3})

    def test_auto_unknown_source_is_404(self):
        srv = self.start([("X", ["0"])])
        self.assertEqual(self.auto(srv, "s9", "discard")[0], 404)

    def test_force_while_job_runs_is_409(self):
        srv = self.start([("X", ["0"])])
        self.target(srv)
        srv.viewer.auto_paused["s1"] = 3
        release = threading.Event()
        self.assertTrue(srv.viewer.job.start("index", lambda job: release.wait(10)))
        try:
            self.assertEqual(self.auto(srv, "s1", "force")[0], 409)
        finally:
            release.set()
            wait_job(srv.server_port)
        self.assertEqual(srv.viewer.auto_paused, {"s1": 3})

    def test_target_unavailable_skips_and_keeps_exports(self):
        srv = self.start([("X", ["0"]), ("Y", ["0"])])
        self.target(srv)
        os.rename(self.archive_dir, self.archive_dir + "_weg")
        job = self.fetch(srv, "mail_20260102T000000", [("Y", ["0"])])
        self.assertEqual(self.exports(), [OLD, "mail_20260102T000000"])
        self.assertIn("log.auto_target_unreachable", codes(job["log"]))

    def test_copy_failure_keeps_exports(self):
        srv = self.start([("X", ["0"]), ("Y", ["0"])])
        self.target(srv)
        with mock.patch("mailviewer.autoarchive.transfer.transfer", return_value=TransferResult(failed=1)):
            job = self.fetch(srv, "mail_20260102T000000", [("Y", ["0"])])
        self.assertEqual(self.exports(), [OLD, "mail_20260102T000000"])
        self.assertTrue("log.archive_failed" in codes(job["log"]), job["log"])

    def test_verification_failure_keeps_exports(self):
        srv = self.start([("X", ["0"]), ("Y", ["0"])])
        self.target(srv)
        with mock.patch.object(Store, "idents_in", return_value=set()):
            job = self.fetch(srv, "mail_20260102T000000", [("Y", ["0"])])
        self.assertEqual(self.exports(), [OLD, "mail_20260102T000000"])
        self.assertTrue("log.verify_failed" in codes(job["log"]), job["log"])

    def test_unreadable_old_mail_keeps_exports(self):
        srv = self.start([("X", ["0"]), ("Y", ["0"])], ghost=True)
        self.target(srv)
        job = self.fetch(srv, "mail_20260102T000000", [("Y", ["0"])])
        self.assertEqual(self.archived(), [("X", [self.unsorted_id(), "5"])])
        self.assertEqual(self.exports(), [OLD, "mail_20260102T000000"])
        self.assertIn("1 mail in old exports is unreadable, old exports are kept", texts(job["log"]))


class CliOverrideTests(unittest.TestCase):
    """/api/config reports a --cli given at start, so the Settings dialog can say it is ignored."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.root = make_library(self.tmp, make_export(self.tmp, "export"))
        self.config = os.path.join(self.tmp, "config.json")
        self.cli = os.path.join(self.tmp, exports.CLI_NAME)
        write(self.cli, "#!/bin/sh\n")
        os.chmod(self.cli, 0o755)

    def start(self, cli):
        srv = server.make_server(self.root, 0, self.tmp, cli=cli, config_path=self.config)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(self.stop, srv)  # runs before the tree is removed and the redirect ends
        wait_job(srv.server_port)
        return srv

    def stop(self, srv):
        srv.shutdown()
        srv.server_close()
        srv.viewer.store.close()

    def test_cli_given_at_start_is_reported(self):
        srv = self.start(self.cli)
        self.assertEqual(request(srv.server_port, "GET", "/api/config")[1]["cli_override"],
                         os.path.abspath(self.cli))

    def test_no_cli_given_reports_none(self):
        srv = self.start(None)
        self.assertIsNone(request(srv.server_port, "GET", "/api/config")[1]["cli_override"])


def make_library(root: str, *exports: str) -> str:
    """Library folder under root with each export as a source (s1, s2, ...). Returns its path."""
    lib_root = os.path.join(root, "library")
    lib = Library.create(lib_root)
    for export in exports:
        lib.add_source(export)
    return lib_root


def wait_job(port: int, timeout: float = 10.0) -> dict:
    """Poll /api/job until the job is no longer running; returns its status."""
    deadline = time.monotonic() + timeout
    while True:
        _, st = request(port, "GET", "/api/job")
        if st["state"] != "running":
            return st
        if time.monotonic() > deadline:
            raise AssertionError(f"index job still running after {timeout}s")
        time.sleep(0.02)


if __name__ == "__main__":
    unittest.main()
