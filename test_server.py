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
import unittest
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
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


def make_export(root: str) -> str:
    export = os.path.join(root, "export")
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


if __name__ == "__main__":
    unittest.main()
