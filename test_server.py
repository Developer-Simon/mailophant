"""Tests for server.py. Uses only synthetic fixtures in a temp directory."""

import base64
import contextlib
import http.client
import io
import json
import os
import shutil
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
from urllib.parse import quote

import server

PDF = b"%PDF-1.4 synthetic fixture\n%%EOF"
PNG = bytes(range(64))

ID_PLAIN = "id1_AAAA"
ID_ALT = "id2-BBBB=="
ID_RICH = "id3_CCCC"
ID_GHOST = "id4=DDDD"  # metadata only, .eml missing


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
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.export = make_export(cls.tmp)
        cls.static = os.path.join(cls.tmp, "static")
        os.makedirs(cls.static)
        write(os.path.join(cls.static, "index.html"), "<!doctype html><title>synthetic</title>")
        with contextlib.redirect_stderr(io.StringIO()):
            cls.index = server.build_index(cls.export)
            cls.srv = server.make_server(cls.export, 0, cls.static)
        cls.port = cls.srv.server_port
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
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


class IndexTests(Base):
    def test_message_ids_are_filename_stems(self):
        self.assertEqual(set(self.index.messages), {ID_PLAIN, ID_ALT, ID_RICH, ID_GHOST})

    def test_labels_loaded(self):
        self.assertEqual(len(self.index.labels), len(LABELS["Payload"]))

    def test_broken_metadata_skipped_and_logged(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            idx = server.build_index(self.export)
        self.assertIn("broken_EEEE.metadata.json", buf.getvalue())
        self.assertNotIn("broken_EEEE", idx.messages)


class FolderTests(Base):
    def test_counts_unread_and_order(self):
        folders = [(f["id"], f["count"], f["unread"]) for f in server.list_folders(self.index)]
        # system IDs first in fixed order, then the rest by name; empty labels omitted
        self.assertEqual(folders, [("0", 3, 2), ("7", 1, 1), ("5", 2, 1), ("X3", 1, 0), ("X1", 2, 0)])

    def test_http_folders(self):
        status, folders = self.get_json("/api/folders")
        self.assertEqual(status, 200)
        self.assertEqual(set(folders[0]), {"id", "name", "path", "color", "count", "unread"})
        self.assertEqual(folders[0]["id"], "0")


class ListTests(Base):
    def ids(self, **kw):
        args = dict(folder=None, needle="", offset=0, limit=50)
        args.update(kw)
        res = server.list_messages(self.index, args["folder"], args["needle"], args["offset"], args["limit"])
        return res["total"], [m["id"] for m in res["messages"]]

    def test_all_sorted_by_time_desc(self):
        self.assertEqual(self.ids(), (4, [ID_PLAIN, ID_ALT, ID_RICH, ID_GHOST]))

    def test_folder_filter(self):
        self.assertEqual(self.ids(folder="0"), (3, [ID_PLAIN, ID_RICH, ID_GHOST]))

    def test_search_umlaut_case_insensitive(self):
        self.assertEqual(self.ids(needle="MÜLLER".casefold()), (1, [ID_PLAIN]))

    def test_search_to_address(self):
        self.assertEqual(self.ids(needle="bob@example.TEST".casefold()), (1, [ID_RICH]))

    def test_search_subject(self):
        self.assertEqual(self.ids(needle="ghost"), (1, [ID_GHOST]))

    def test_pagination(self):
        self.assertEqual(self.ids(offset=0, limit=2), (4, [ID_PLAIN, ID_ALT]))
        self.assertEqual(self.ids(offset=2, limit=2), (4, [ID_RICH, ID_GHOST]))

    def test_summary_fields(self):
        msgs = {m["id"]: m for m in server.list_messages(self.index, None, "", 0, 50)["messages"]}
        self.assertTrue(msgs[ID_PLAIN]["unread"])
        self.assertFalse(msgs[ID_ALT]["unread"])
        self.assertEqual(msgs[ID_RICH]["attachments"], 1)
        self.assertEqual(msgs[ID_PLAIN]["sender"], {"name": "Jürgen Müller", "address": "juergen@example.test"})

    def test_http_query_params(self):
        status, data = self.get_json("/api/messages?folder=0&q=" + quote("MÜLLER") + "&limit=2")
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
        m = server.load_message(self.index, ID_PLAIN)
        self.assertIsNone(m["error"])
        self.assertIn("Grüße aus Köln", m["text"])
        self.assertIsNone(m["html"])
        self.assertEqual(m["attachments"], [])
        self.assertEqual(m["from"]["name"], "Jürgen Müller")
        self.assertEqual(m["cc"], [{"name": "Carl", "address": "carl@example.test"}])
        self.assertEqual(m["to"], [{"name": "Ann", "address": "ann@example.test"}])

    def test_multipart_alternative(self):
        m = server.load_message(self.index, ID_ALT)
        self.assertIn("Plain part: Hallo Welt", m["text"])
        self.assertIn("<b>Hallo</b>", m["html"])

    def test_cid_rewritten_and_attachment_listed(self):
        m = server.load_message(self.index, ID_RICH)
        self.assertIn("data:image/png;base64," + base64.b64encode(PNG).decode(), m["html"])
        self.assertNotIn("cid:", m["html"])
        self.assertIn("HTML Ünïcode", m["html"])
        self.assertEqual(m["attachments"], [{
            "index": 0, "name": "Rechnung ü.pdf", "size": len(PDF), "mime": "application/pdf"}])

    def test_error_mail_without_eml(self):
        m = server.load_message(self.index, ID_GHOST)
        self.assertIsNotNone(m["error"])
        self.assertEqual((m["text"], m["html"], m["attachments"]), (None, None, []))
        self.assertEqual(m["subject"], "Ghost")
        self.assertEqual(m["from"]["address"], "gus@example.test")

    def test_http_detail_and_error_mail(self):
        status, m = self.get_json("/api/message/" + quote(ID_ALT, safe=""))
        self.assertEqual(status, 200)
        self.assertIn("Hallo", m["html"])
        status, m = self.get_json("/api/message/" + quote(ID_GHOST, safe=""))
        self.assertEqual(status, 200)
        self.assertIsNotNone(m["error"])

    def test_unknown_message_404(self):
        status, data = self.get_json("/api/message/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", data)


class AttachmentTests(Base):
    def test_download(self):
        status, headers, body = self.get(f"/api/message/{ID_RICH}/attachment/0")
        self.assertEqual(status, 200)
        self.assertEqual(body, PDF)
        self.assertEqual(headers["Content-Type"], "application/pdf")
        self.assertIn("filename*=UTF-8''Rechnung%20%C3%BC.pdf", headers["Content-Disposition"])

    def test_bad_index_and_missing_eml_404(self):
        for path in (f"/api/message/{ID_RICH}/attachment/1",
                     f"/api/message/{ID_RICH}/attachment/abc",
                     f"/api/message/{quote(ID_GHOST, safe='')}/attachment/0"):
            status, data = self.get_json(path)
            self.assertEqual(status, 404, path)
            self.assertIn("error", data)


class SecurityTests(Base):
    def assert_404(self, path):
        status, _, body = self.get(path)
        self.assertEqual(status, 404, path)
        self.assertIn("error", json.loads(body))

    def test_path_traversal_ids_rejected(self):
        self.assert_404("/api/message/" + quote("../labels.json", safe=""))
        self.assert_404("/api/message/..%2Flabels.json")
        self.assert_404("/api/message/%2e%2e%2flabels.json")
        self.assert_404("/api/message/../labels.json")
        self.assert_404("/api/message/..%2Flabels.json/attachment/0")

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
        for path in ("/", "/api/folders", "/api/message/nope"):
            _, headers, _ = self.get(path)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff", path)

    def test_index_page_served(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"synthetic", body)

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

env = os.environ
if any(PASSWORD in arg for arg in sys.argv):  # secrets must never be on argv
    sys.exit(9)
if env.get("ET_OPERATION") != "backup" or env.get("ET_TELEMETRY_OFF") != "1":
    sys.exit(8)
for key, want in EXPECT.items():  # None means the variable must be absent
    if env.get(key) != want:
        sys.exit(7)
print("login", env["ET_USER_EMAIL"], *SECRETS, flush=True)
if MODE == "fail":
    print("login failed", file=sys.stderr)
    sys.exit(3)
if MODE == "sleep":
    time.sleep(1)
if MODE in ("ok", "sleep", "dup"):
    out = os.path.join(env["ET_DIR"], env["ET_USER_EMAIL"], "mail_20261015T120000")
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
        self.assertEqual(server.resolve_mail_dir(account), os.path.join(account, "mail_20260101T000000"))

    def test_mail_dir_used_as_is(self):
        mail = make_export(self.tmp, "mail_20260101T000000")
        self.assertEqual(server.resolve_mail_dir(mail), mail)
        self.assertEqual(server.account_dir_of(mail), self.tmp)
        plain = make_export(self.tmp)  # not named mail_*: the dir is its own account dir
        self.assertEqual(server.account_dir_of(plain), plain)

    def test_account_dir_of_account(self):
        account = os.path.join(self.tmp, "acct@x.test")
        self.assertEqual(server.account_dir_of(account), account)

    def test_no_export_raises(self):
        with self.assertRaises(ValueError):
            server.resolve_mail_dir(self.tmp)
        with self.assertRaises(ValueError):
            server.resolve_mail_dir(os.path.join(self.tmp, "missing"))

    def test_find_cli_levels(self):
        root = self.tmp
        deep = os.path.join(root, "a", "b", "c", "d", "acct@x")
        near = os.path.join(root, "a", "b", "acct@x")
        os.makedirs(os.path.join(root, "a", "b"))
        cli = os.path.join(root, CLI_NAME)
        write(cli, "#!/bin/sh\n")
        os.chmod(cli, 0o755)
        write(os.path.join(root, "a", "b", CLI_NAME), "not executable")  # skipped
        self.assertEqual(server.find_cli(near), cli)
        self.assertIsNone(server.find_cli(deep))

    def test_free_name(self):
        os.makedirs(os.path.join(self.tmp, "mail_1"))
        os.makedirs(os.path.join(self.tmp, "mail_1_1"))
        self.assertEqual(server.free_name(self.tmp, "mail_1"), "mail_1_2")
        self.assertEqual(server.free_name(self.tmp, "mail_2"), "mail_2")

    def test_redact(self):
        self.assertEqual(server.redact("a pw b pw", ["pw"]), "a *** b ***")
        self.assertEqual(server.redact("abc", ["", None]), "abc")
        self.assertEqual(server.redact("xabcx", ["abc", "xabcx"]), "***")

    def test_cli_env_only_sets_nonempty_secrets(self):
        with mock.patch.dict(os.environ, {"ET_TOTP_CODE": "1", "ET_USER_MAILBOX_PASSWORD": "old"}):
            env = server.cli_env("a@b.test", "/t", PW, "", "")
        self.assertNotIn("ET_TOTP_CODE", env)
        self.assertNotIn("ET_USER_MAILBOX_PASSWORD", env)
        self.assertEqual((env["ET_OPERATION"], env["ET_DIR"], env["ET_TELEMETRY_OFF"]),
                         ("backup", "/t", "1"))
        self.assertEqual(env["ET_USER_PASSWORD"], PW)

    def test_parse_fetch_body(self):
        raw = json.dumps({"password": PW, "totp": " 123 "}).encode()
        self.assertEqual(server.parse_fetch_body(raw, "def@x.test"), ("def@x.test", PW, "", "123"))
        for bad, reason in ((b"{x", "invalid JSON body"), (b"[]", "body must be a JSON object"),
                            (b'{"email": "a@b"}', "missing password"),
                            (b'{"password": "p"}', "missing email"),
                            (b'{"password": "p", "email": "../x"}', "invalid email")):
            with self.assertRaisesRegex(ValueError, reason):
                server.parse_fetch_body(bad, "")


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
        self.assertEqual(server.dedupe_with_previous(self.prev, self.new), (2, len(eml) + len(meta_js)))
        for name in ("a.eml", "a.metadata.json"):
            self.assertTrue(self.same_inode(name))
            self.assertEqual(os.stat(os.path.join(self.new, name)).st_nlink, 2)
        self.assertEqual(self.read(self.new, "a.eml"), eml)
        self.assertEqual(self.read(self.new, "a.metadata.json"), meta_js)

    def test_changed_metadata_not_linked(self):
        self.pair("a.metadata.json", b'{"Unread": 1}', b'{"Unread": 0}')  # same size, other bytes
        self.pair("b.metadata.json", b'{"Unread": 1}', b'{"Unread": 1, "LabelIDs": ["0"]}')  # other size
        self.assertEqual(server.dedupe_with_previous(self.prev, self.new), (0, 0))
        self.assertFalse(self.same_inode("a.metadata.json"))
        self.assertFalse(self.same_inode("b.metadata.json"))
        self.assertEqual(self.read(self.new, "a.metadata.json"), b'{"Unread": 0}')

    def test_new_only_file_untouched(self):
        write(os.path.join(self.new, "c.eml"), b"new mail")
        self.assertEqual(server.dedupe_with_previous(self.prev, self.new), (0, 0))
        self.assertEqual(os.stat(os.path.join(self.new, "c.eml")).st_nlink, 1)
        self.assertEqual(self.read(self.new, "c.eml"), b"new mail")
        self.assertFalse(os.path.exists(os.path.join(self.prev, "c.eml")))

    def test_labels_and_other_files_untouched(self):
        self.pair("labels.json", b'{"Payload": []}')
        self.pair("notes.txt", b"same bytes")
        self.assertEqual(server.dedupe_with_previous(self.prev, self.new), (0, 0))
        self.assertFalse(self.same_inode("labels.json"))
        self.assertFalse(self.same_inode("notes.txt"))

    def test_previous_dir_not_modified(self):
        self.pair("a.eml", plain_eml())
        self.pair("a.metadata.json", b"{}")
        write(os.path.join(self.prev, "old_only.eml"), b"gone")
        before = snapshot(self.prev)
        server.dedupe_with_previous(self.prev, self.new)
        self.assertEqual(snapshot(self.prev), before)

    def test_already_linked_not_counted_twice(self):
        eml = plain_eml()
        self.pair("a.eml", eml)
        self.assertEqual(server.dedupe_with_previous(self.prev, self.new), (1, len(eml)))
        self.assertEqual(server.dedupe_with_previous(self.prev, self.new), (0, 0))

    def test_oserror_on_link_skips_file_and_leaves_no_temp(self):
        eml = plain_eml()
        self.pair("a.eml", eml)
        self.pair("a.metadata.json", b"{}")
        before = sorted(os.listdir(self.new))
        with mock.patch.object(server.os, "link", side_effect=OSError("cross-device")):
            self.assertEqual(server.dedupe_with_previous(self.prev, self.new), (0, 0))
        self.assertEqual(sorted(os.listdir(self.new)), before)
        self.assertEqual(self.read(self.new, "a.eml"), eml)
        self.assertFalse(self.same_inode("a.eml"))

    def test_oserror_on_replace_cleans_temp(self):
        eml = plain_eml()
        self.pair("a.eml", eml)
        before = sorted(os.listdir(self.new))
        with mock.patch.object(server.os, "replace", side_effect=OSError("busy")):
            self.assertEqual(server.dedupe_with_previous(self.prev, self.new), (0, 0))
        self.assertEqual(sorted(os.listdir(self.new)), before)
        self.assertEqual(self.read(self.new, "a.eml"), eml)
        self.assertEqual(os.stat(os.path.join(self.new, "a.eml")).st_nlink, 1)

    def test_missing_previous_dir_logs_nothing(self):
        viewer = server.Viewer(server.Index(self.new, [], {}), self.tmp, None)
        viewer._link_previous(os.path.join(self.tmp, "gone"), self.new)
        self.assertEqual(viewer.status()["log"], [])

    def test_human_size(self):
        self.assertEqual(server.human_size(1_200_000_000), "1,2 GB")
        self.assertEqual(server.human_size(561_000), "561,0 KB")


class FetchTests(unittest.TestCase):
    """Fetch endpoints. Each test gets its own account dir and server, since fetches swap the index."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
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
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def serve(self, mode="ok", expect=ABSENT, account=None, cli="fake", dup_from=None):
        """cli="fake" writes a stand-in CLI; None lets the server auto-detect; a path is used as is."""
        if cli == "fake":
            cli = os.path.join(self.tmp, "bin", CLI_NAME)
            os.makedirs(os.path.dirname(cli), exist_ok=True)
            write_fake_cli(cli, mode, expect, dup_from)
        export = os.path.join(account or self.account, OLD)
        with contextlib.redirect_stderr(io.StringIO()):
            self.srv = server.make_server(export, 0, self.tmp, cli=cli)
        self.port = self.srv.server_port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def get(self, path):
        return request(self.port, "GET", path)

    def post(self, body=None, origin="same", host=None, raw=None):
        headers = {"Content-Type": "application/json", "Host": host or f"127.0.0.1:{self.port}"}
        if origin == "same":
            origin = f"http://127.0.0.1:{self.port}"
        if origin is not None:
            headers["Origin"] = origin
        data = raw if raw is not None else json.dumps(body).encode("utf-8")
        return request(self.port, "POST", "/api/fetch", data, headers)

    def wait_fetch(self, states=("done", "error"), timeout=10.0):
        deadline = time.monotonic() + timeout
        while True:
            _, st = self.get("/api/fetch/status")
            if st["state"] in states:
                return st
            if time.monotonic() > deadline:
                self.fail(f"fetch still {st['state']} after {timeout}s")
            time.sleep(0.02)

    def test_status_idle_initially(self):
        self.serve()
        _, st = self.get("/api/fetch/status")
        self.assertEqual(st, {"state": "idle", "log": [], "message": None, "export": None, "started": None})

    def test_success_swaps_index(self):
        self.serve(expect=FULL)
        status, data = self.post({"password": PW, "mailbox_password": MB, "totp": TOTP})
        self.assertEqual((status, data), (202, {"state": "running"}))
        st = self.wait_fetch()
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["export"], NEW)
        self.assertIsNone(st["message"])
        self.assertIsInstance(st["started"], int)
        # the CLI ran (and its argv/env checks passed) with the email taken from /api/config
        self.assertEqual(self.get("/api/config")[1]["export"], NEW)
        _, msgs = self.get("/api/messages")
        self.assertEqual(msgs["total"], 1)
        self.assertEqual(msgs["messages"][0]["id"], "new_FFFF")
        self.assertTrue(os.path.isdir(os.path.join(self.account, NEW)))
        self.assertEqual(sorted(os.listdir(os.path.join(self.account, OLD))), self.old_files)
        self.assertEqual([n for n in os.listdir(self.account) if n.startswith(".fetch-")], [])

    def test_secrets_redacted_in_status(self):
        self.serve(expect=FULL)
        self.post({"password": PW, "mailbox_password": MB, "totp": TOTP})
        st = self.wait_fetch()
        text = json.dumps(st, ensure_ascii=False)
        for secret in (PW, MB, TOTP):
            self.assertNotIn(secret, text)
        self.assertTrue(any(line.startswith("login ") and "***" in line for line in st["log"]), st["log"])

    def test_failing_cli_keeps_old_index(self):
        self.serve(mode="fail")
        self.assertEqual(self.post({"password": PW})[0], 202)
        st = self.wait_fetch()
        self.assertEqual((st["state"], st["message"], st["export"]), ("error", "CLI exited with code 3", None))
        self.assertIn("login failed", st["log"])
        self.assertNotIn(PW, json.dumps(st))
        self.assertEqual(self.get("/api/config")[1]["export"], OLD)
        self.assertEqual(self.get("/api/messages")[1]["total"], 4)
        self.assertFalse(os.path.exists(os.path.join(self.account, NEW)))
        self.assertEqual([n for n in os.listdir(self.account) if n.startswith(".fetch-")], [])

    def test_cli_without_mail_output_is_error(self):
        self.serve(mode="empty")
        self.post({"password": PW})
        st = self.wait_fetch()
        self.assertEqual((st["state"], st["message"]), ("error", "CLI produced no mail export"))
        self.assertEqual(self.get("/api/config")[1]["export"], OLD)

    def test_second_fetch_while_running_is_409(self):
        self.serve(mode="sleep")
        self.assertEqual(self.post({"password": PW})[0], 202)
        status, data = self.post({"password": PW})
        self.assertEqual(status, 409)
        self.assertIn("error", data)
        self.assertEqual(self.wait_fetch()["state"], "done")

    def test_unchanged_mails_hardlinked_into_new_export(self):
        self.serve(mode="dup", dup_from=os.path.join(self.account, OLD))
        self.post({"password": PW})
        st = self.wait_fetch()
        self.assertEqual(st["state"], "done")
        old_eml = os.path.join(self.account, OLD, ID_PLAIN + ".eml")
        new_eml = os.path.join(self.account, NEW, ID_PLAIN + ".eml")
        self.assertTrue(os.path.samefile(old_eml, new_eml))
        self.assertEqual(os.stat(os.path.join(self.account, NEW, "new_FFFF.eml")).st_nlink, 1)
        self.assertTrue(any(line.startswith("Hardlinks:") for line in st["log"]), st["log"])
        _, msgs = self.get("/api/messages")
        self.assertEqual(sorted(m["id"] for m in msgs["messages"]), sorted([ID_PLAIN, "new_FFFF"]))

    def test_dedupe_failure_does_not_fail_fetch(self):
        self.serve()
        with mock.patch.object(server, "dedupe_with_previous", side_effect=RuntimeError("boom")):
            self.post({"password": PW})
            st = self.wait_fetch()
        self.assertEqual((st["state"], st["export"]), ("done", NEW))
        self.assertTrue(any(line.startswith("Hardlinks") for line in st["log"]), st["log"])
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
        self.assertEqual(self.get("/api/fetch/status")[1]["state"], "idle")

    def test_localhost_origin_allowed(self):
        self.serve()
        status, _ = self.post({"password": PW}, origin=f"http://localhost:{self.port}")
        self.assertEqual(status, 202)
        self.assertEqual(self.wait_fetch()["state"], "done")

    def test_bad_requests_400(self):
        self.serve()
        cases = [
            {"email": ACCOUNT},
            {"password": ""},
        ]
        for body in cases:
            status, data = self.post(body)
            self.assertEqual(status, 400, body)
            self.assertIn("error", data)
        for raw in (b"{not json", b"[]", json.dumps({"password": PW, "totp": "1" * 70000}).encode()):
            status, data = self.post(raw=raw)
            self.assertEqual(status, 400)
            self.assertIn("error", data)
        self.assertEqual(self.get("/api/fetch/status")[1]["state"], "idle")

    def test_missing_email_400_without_at_in_account_name(self):
        plain = os.path.join(self.tmp, "plain")
        make_export(plain, OLD)
        self.serve(account=plain)
        _, cfg = self.get("/api/config")
        self.assertEqual(cfg["email"], "")
        self.assertTrue(cfg["fetch_available"])
        status, data = self.post({"password": PW})
        self.assertEqual(status, 400)
        self.assertIn("missing email", data["error"])

    def test_no_cli_404(self):
        self.serve(cli="/nonexistent/" + CLI_NAME)
        self.assertFalse(self.get("/api/config")[1]["fetch_available"])
        status, data = self.post({"password": PW})
        self.assertEqual(status, 404)
        self.assertIn("error", data)

    def test_config_fields(self):
        self.serve()
        _, cfg = self.get("/api/config")
        self.assertEqual({k: cfg[k] for k in ("configured", "fetch_available", "email", "export")},
                         {"configured": True, "fetch_available": True, "email": ACCOUNT, "export": OLD})
        self.assertEqual(cfg["workdir"], self.account)

    def test_auto_detect_cli_next_to_account_dir(self):
        cli = os.path.join(self.tmp, CLI_NAME)  # parent of the account dir
        write(cli, "#!/bin/sh\n")
        os.chmod(cli, 0o755)
        self.serve(cli=None)
        self.assertTrue(self.get("/api/config")[1]["fetch_available"])

    def test_auto_detect_none_without_cli(self):
        self.serve(cli=None)  # nothing near the account dir (the tmp dir's ancestors have no CLI)
        self.assertFalse(self.get("/api/config")[1]["fetch_available"])


class WorkdirTests(unittest.TestCase):
    """Directory picker: browse API, choosing a directory at runtime, remembering the choice."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.account = os.path.join(self.tmp, "root", ACCOUNT)
        make_export(self.account, OLD)
        os.makedirs(os.path.join(self.tmp, "root", "plain", "sub"))
        os.makedirs(os.path.join(self.tmp, "root", ".hidden"))
        write(os.path.join(self.tmp, "root", "file.txt"), "x")
        self.config = os.path.join(self.tmp, "cfg", "config.json")
        self.srv = None

    def tearDown(self):
        if self.srv is not None:
            self.srv.shutdown()
            self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def serve(self, export=None):
        with contextlib.redirect_stderr(io.StringIO()):
            self.srv = server.make_server(export, 0, self.tmp, config_path=self.config)
        self.port = self.srv.server_port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def get(self, path):
        return request(self.port, "GET", path)

    def post(self, body, origin="same"):
        headers = {"Content-Type": "application/json"}
        if origin == "same":
            origin = f"http://127.0.0.1:{self.port}"
        if origin is not None:
            headers["Origin"] = origin
        return request(self.port, "POST", "/api/workdir", json.dumps(body).encode("utf-8"), headers)

    def saved(self):
        return server.load_saved(self.config)

    def test_starts_unconfigured_and_refuses_data(self):
        self.serve()
        self.assertFalse(self.get("/api/config")[1]["configured"])
        for path in ("/api/folders", "/api/messages", "/api/message/x"):
            self.assertEqual(self.get(path)[0], 409)

    def test_browse_lists_visible_dirs_only(self):
        self.serve()
        status, data = self.get("/api/browse?path=" + os.path.join(self.tmp, "root"))
        self.assertEqual(status, 200)
        self.assertEqual({d["name"]: d["openable"] for d in data["dirs"]}, {ACCOUNT: True, "plain": False})
        self.assertFalse(data["openable"])
        self.assertEqual(data["parent"], self.tmp)

    def test_browse_flags_openable_dir_and_root_has_no_parent(self):
        self.serve()
        self.assertTrue(self.get("/api/browse?path=" + self.account)[1]["openable"])
        self.assertIsNone(self.get("/api/browse?path=/")[1]["parent"])

    def test_browse_errors(self):
        self.serve()
        self.assertEqual(self.get("/api/browse?path=" + os.path.join(self.tmp, "nope"))[0], 400)
        self.assertEqual(self.get("/api/browse?path=" + os.path.join(self.tmp, "root", "file.txt"))[0], 400)

    def test_open_dir_without_remember(self):
        self.serve()
        status, cfg = self.post({"path": self.account})
        self.assertEqual(status, 200)
        self.assertTrue(cfg["configured"])
        self.assertEqual(cfg["export"], OLD)
        self.assertIsNone(self.saved())
        self.assertEqual(self.get("/api/folders")[0], 200)

    def test_remember_persists_and_next_start_uses_it(self):
        self.serve()
        self.post({"path": self.account, "remember": True})
        self.assertEqual(self.saved(), self.account)
        self.srv.shutdown()
        self.srv.server_close()
        self.serve()
        cfg = self.get("/api/config")[1]
        self.assertTrue(cfg["configured"])
        self.assertEqual(cfg["saved"], self.account)

    def test_forget_removes_saved_choice(self):
        self.serve()
        self.post({"path": self.account, "remember": True})
        self.post({"path": self.account, "remember": False})
        self.assertIsNone(self.saved())
        self.assertIsNone(self.get("/api/config")[1]["saved"])

    def test_invalid_dir_keeps_state_and_saved_choice(self):
        self.serve()
        self.post({"path": self.account, "remember": True})
        status, data = self.post({"path": os.path.join(self.tmp, "root", "plain"), "remember": True})
        self.assertEqual(status, 400)
        self.assertIn("error", data)
        self.assertEqual(self.saved(), self.account)
        self.assertEqual(self.get("/api/config")[1]["export"], OLD)

    def test_bad_saved_choice_starts_unconfigured_with_error(self):
        server.save_saved(self.config, os.path.join(self.tmp, "gone"))
        self.serve()
        cfg = self.get("/api/config")[1]
        self.assertFalse(cfg["configured"])
        self.assertIn("gone", cfg["saved_error"])

    def test_explicit_dir_beats_saved_choice(self):
        server.save_saved(self.config, os.path.join(self.tmp, "gone"))
        self.serve(self.account)
        self.assertTrue(self.get("/api/config")[1]["configured"])

    def test_post_checks_origin_and_body(self):
        self.serve()
        self.assertEqual(self.post({"path": self.account}, origin=None)[0], 403)
        self.assertEqual(self.post({"path": self.account}, origin="http://evil.test")[0], 403)
        self.assertEqual(self.post({"nope": 1})[0], 400)
        self.assertEqual(self.post({"path": 5})[0], 400)
        self.assertFalse(self.get("/api/config")[1]["configured"])

    def test_cli_found_after_choosing(self):
        cli = os.path.join(self.tmp, "root", CLI_NAME)
        write(cli, "#!/bin/sh\n")
        os.chmod(cli, 0o755)
        self.serve()
        self.assertFalse(self.get("/api/config")[1]["fetch_available"])
        self.assertTrue(self.post({"path": self.account})[1]["fetch_available"])


if __name__ == "__main__":
    unittest.main()
