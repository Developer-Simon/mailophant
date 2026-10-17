import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

import store
import tags
import test_server as fx


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.export = fx.make_export(self.tmp)
        self.db = os.path.join(self.tmp, "archive.db")
        self.store = store.Store(self.db)
        self.addCleanup(self.store.close)

    def sync(self, source_id="s1", mail_dir=None, progress=None):
        with contextlib.redirect_stderr(io.StringIO()):
            return self.store.sync_source(source_id, mail_dir or self.export, progress)

    def rows(self, sql, *args):
        return self.store._conn().execute(sql, args).fetchall()

    def test_first_sync_adds_all_and_skips_broken(self):
        r = self.sync()
        self.assertEqual((r.added, r.updated, r.removed, r.skipped), (4, 0, 0, 1))
        self.assertEqual(self.rows("SELECT count(*) FROM messages"), [(4,)])
        self.assertEqual(self.store.mail_dir("s1"), self.export)

    def test_resync_unchanged_is_noop(self):
        self.sync()
        self.assertEqual(self.sync(), store.SyncResult(0, 0, 0, 1))

    def test_changed_new_and_deleted_files(self):
        self.sync()
        path = os.path.join(self.export, fx.ID_ALT + ".metadata.json")
        fx.write_json(path, fx.meta(fx.ID_ALT, "Other subject", ("Sam Sender", "sam@example.test"),
                                    [("Ann", "ann@example.test")], 1700000200, ["5"]))
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        fx.write_json(os.path.join(self.export, "new1.metadata.json"),
                      fx.meta("new1", "New", ("N", "n@example.test"), [], 1700000400, ["0"]))
        os.remove(os.path.join(self.export, fx.ID_RICH + ".metadata.json"))
        self.assertEqual(self.sync(), store.SyncResult(1, 1, 1, 1))
        subjects = [json.loads(p)["Subject"] for (p,) in
                    self.rows("SELECT payload FROM messages WHERE mid = ?", fx.ID_ALT)]
        self.assertEqual(subjects, ["Other subject"])
        self.assertEqual(self.rows("SELECT count(*) FROM messages_fts"), [(4,)])
        self.assertEqual(self.rows("SELECT count(*) FROM messages_fts WHERE mid = ?", fx.ID_ALT), [(1,)])
        self.assertEqual(self.rows("SELECT count(*) FROM message_labels WHERE mid = ?", fx.ID_RICH), [(0,)])

    def test_changed_mail_refreshes_fts_and_labels(self):
        self.sync()
        path = os.path.join(self.export, fx.ID_ALT + ".metadata.json")
        fx.write_json(path, fx.meta(fx.ID_ALT, "Brandneu", ("Zed Zorn", "zed@example.test"),
                                    [("Yan", "yan@example.test")], 1700000200, ["X2"]))
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        self.sync()
        self.assertEqual(self.rows("SELECT subject, people FROM messages_fts WHERE mid = ?", fx.ID_ALT),
                         [("brandneu", "zed zorn zed@example.test yan yan@example.test")])
        self.assertEqual(self.rows("SELECT label_id FROM message_labels WHERE mid = ?", fx.ID_ALT), [("X2",)])

    def test_bad_time_is_zero_and_does_not_abort(self):
        fx.write_json(os.path.join(self.export, "bad1.metadata.json"),
                      fx.meta("bad1", "Bad", ("B", "b@example.test"), [], "abc", ["0"]))
        r = self.sync()
        self.assertEqual((r.added, r.skipped), (5, 1))
        self.assertEqual(self.rows("SELECT time FROM messages WHERE mid = 'bad1'"), [(0,)])

    def test_failed_sync_is_rolled_back(self):
        self.sync()
        fx.write_json(os.path.join(self.export, "new1.metadata.json"),
                      fx.meta("new1", "New", ("N", "n@example.test"), [], 1700000400, ["0"]))
        os.remove(os.path.join(self.export, fx.ID_RICH + ".metadata.json"))
        fx.write_json(os.path.join(self.export, "labels.json"), {"Version": 1, "Payload": []})
        before = [self.rows(f"SELECT count(*) FROM {t}") for t in
                  ("messages", "labels", "message_labels", "messages_fts")]
        with mock.patch.object(store, "mail_ident", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.sync()
        after = [self.rows(f"SELECT count(*) FROM {t}") for t in
                 ("messages", "labels", "message_labels", "messages_fts")]
        self.assertEqual(before, after)

    def test_new_export_dir_with_hardlinks_only_moves_mail_dir(self):
        self.sync()
        export2 = os.path.join(self.tmp, "export2")
        os.makedirs(export2)
        for name in os.listdir(self.export):
            os.link(os.path.join(self.export, name), os.path.join(export2, name))
        self.assertEqual(self.sync("s1", export2), store.SyncResult(skipped=1))
        self.assertEqual(self.store.mail_dir("s1"), export2)

    def test_labels_replaced_on_sync(self):
        self.sync()
        self.assertEqual(self.rows("SELECT count(*) FROM labels WHERE source_id = 's1'"), [(7,)])
        fx.write_json(os.path.join(self.export, "labels.json"), {"Version": 1, "Payload": [
            {"Path": "Inbox", "ID": "0", "ParentID": None, "Name": "Posteingang", "Color": "#111", "Type": 3}]})
        self.sync()
        self.assertEqual(self.rows("SELECT label_id, name FROM labels WHERE source_id = 's1'"),
                         [("0", "Posteingang")])

    def test_progress_reports_done_and_total(self):
        calls = []
        self.sync(progress=lambda done, total: calls.append((done, total)))
        self.assertTrue(calls)
        self.assertEqual(calls[-1], (5, 5))
        self.assertEqual([d for d, _ in calls], sorted(d for d, _ in calls))

    def test_mail_ident(self):
        p = os.path.join(self.tmp, "a.eml")
        fx.write(p, "Message-ID: <AbC@Example.test >\nSubject: x\n\nbody\n")
        self.assertEqual(store.mail_ident(p), "mid:abc@example.test")
        q = os.path.join(self.tmp, "b.eml")
        data = b"Subject: x\n\nbody\n"
        fx.write(q, data)
        self.assertEqual(store.mail_ident(q), "sha256:" + hashlib.sha256(data).hexdigest())
        self.assertEqual(store.mail_ident(os.path.join(self.tmp, "missing.eml")), "")

    def test_drop_source_removes_all_rows(self):
        self.sync()
        self.sync("s2", self.export)
        self.store.drop_source("s1")
        for table in ("messages", "labels", "message_labels", "messages_fts"):
            self.assertEqual(self.rows(f"SELECT count(*) FROM {table} WHERE source_id = 's1'"), [(0,)], table)
        self.assertEqual(self.rows("SELECT count(*) FROM messages WHERE source_id = 's2'"), [(4,)])
        self.assertIsNone(self.store.mail_dir("s1"))

    def test_rows_visible_from_other_thread(self):
        self.sync()
        out = []

        def read():
            try:
                out.append(self.rows("SELECT count(*) FROM messages"))
            finally:
                self.store.close()  # the worker thread's own connection

        t = threading.Thread(target=read)
        t.start()
        t.join()
        self.assertEqual(out, [[(4,)]])

    def test_schema_version_1_file_rebuilt_and_searchable(self):
        self.sync()
        self.store.close()
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            con.execute("PRAGMA user_version = 1")  # the index before the casefolded FTS columns
            con.commit()
        self.store = store.Store(self.db)
        self.addCleanup(self.store.close)
        self.assertEqual(store.SCHEMA_VERSION, 2)
        self.assertEqual(self.rows("SELECT count(*) FROM messages"), [(0,)])
        self.sync()
        r = self.store.messages(["s1"], None, "grüße aus".casefold(), 0, 50)
        self.assertEqual([m["id"] for m in r["messages"]], [fx.ID_PLAIN])

    def test_schema_version_mismatch_rebuilds(self):
        self.sync()
        self.store.close()
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            con.execute("PRAGMA user_version = 99")
            con.commit()
        self.store = store.Store(self.db)
        self.addCleanup(self.store.close)
        self.assertEqual(self.rows("SELECT count(*) FROM messages"), [(0,)])
        self.assertEqual(self.sync(), store.SyncResult(4, 0, 0, 1))


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.export_a = fx.make_export(self.tmp, "a")
        self.export_b = fx.make_export(self.tmp, "b")
        labels = json.loads(json.dumps(fx.LABELS))
        for label in labels["Payload"]:
            if label["ID"] == "X1":
                label["Path"] = "projects"  # lower case here, "Projects" in export a: same folder
        fx.write_json(os.path.join(self.export_b, "labels.json"), labels)
        self.store = store.Store(os.path.join(self.tmp, "archive.db"))
        self.addCleanup(self.store.close)
        with contextlib.redirect_stderr(io.StringIO()):
            self.store.sync_source("s1", self.export_a)
            self.store.sync_source("s2", self.export_b)

    def test_folders_single_source_counts_and_order(self):
        keys = [f["key"] for f in self.store.folders(["s1"])]
        self.assertEqual(keys[:3], ["sys:0", "sys:7", "sys:5"])
        inbox = self.store.folders(["s1"])[0]
        self.assertEqual((inbox["count"], inbox["unread"]), (3, 2))

    def test_folders_combined_merge_system_and_same_path_case_insensitive(self):
        f = {x["key"]: x for x in self.store.folders(["s1", "s2"])}
        self.assertEqual(f["sys:0"]["count"], 6)
        self.assertEqual(f["path:projects"]["count"], 4)

    def test_messages_sorted_paged_and_tagged_with_source(self):
        r = self.store.messages(["s1"], None, "", 0, 50)
        self.assertEqual(r["total"], 4)
        self.assertEqual([m["id"] for m in r["messages"]], [fx.ID_PLAIN, fx.ID_ALT, fx.ID_RICH, fx.ID_GHOST])
        self.assertEqual({m["source"] for m in r["messages"]}, {"s1"})
        page = self.store.messages(["s1"], None, "", 1, 2)
        self.assertEqual([m["id"] for m in page["messages"]], [fx.ID_ALT, fx.ID_RICH])
        self.assertEqual(self.store.messages(["s1", "s2"], None, "", 6, 10)["total"], 8)
        self.assertEqual(len(self.store.messages(["s1", "s2"], None, "", 6, 10)["messages"]), 2)

    def test_messages_folder_filter_by_key(self):
        r = self.store.messages(["s1"], "sys:0", "", 0, 50)
        self.assertEqual((r["total"], {m["id"] for m in r["messages"]}),
                         (3, {fx.ID_PLAIN, fx.ID_RICH, fx.ID_GHOST}))
        self.assertEqual(self.store.messages(["s1", "s2"], "path:projects", "", 0, 50)["total"], 4)
        self.assertEqual(self.store.messages(["s1"], "path:nope", "", 0, 50)["total"], 0)

    def test_search_umlaut_case_insensitive(self):
        r = self.store.messages(["s1"], None, "MÜLLER".casefold(), 0, 50)
        self.assertEqual([m["id"] for m in r["messages"]], [fx.ID_PLAIN])

    def test_search_subject_and_to_address(self):
        r = self.store.messages(["s1"], None, "grüße aus".casefold(), 0, 50)
        self.assertEqual([m["id"] for m in r["messages"]], [fx.ID_PLAIN])
        self.assertEqual(self.store.messages(["s1"], None, "ann@example.test", 0, 50)["total"], 3)

    def test_short_needle_uses_like(self):
        r = self.store.messages(["s1"], None, "an", 0, 50)
        self.assertEqual({m["id"] for m in r["messages"]}, {fx.ID_PLAIN, fx.ID_ALT, fx.ID_GHOST})
        self.assertEqual(self.store.messages(["s1"], None, "%", 0, 50)["total"], 0)
        self.assertEqual(self.store.messages(["s1"], None, "_", 0, 50)["total"], 0)

    def test_search_special_characters_are_literal(self):
        for q in ['"', '*', 'near(', '-x', 'a or b', "'", '"""', 'a"b', '\\']:
            with self.subTest(q=q):
                self.assertEqual(self.store.messages(["s1"], None, q, 0, 50)["total"], 0)

    def test_message_lookup_and_unknown(self):
        mail_dir, payload = self.store.message("s1", fx.ID_PLAIN)
        self.assertEqual(mail_dir, self.export_a)
        self.assertEqual(payload["Subject"], "Grüße aus Köln")
        self.assertIsNone(self.store.message("s1", "nope"))
        self.assertIsNone(self.store.message("s9", fx.ID_PLAIN))

    def test_empty_source_list(self):
        self.assertEqual(self.store.messages([], None, "", 0, 10), {"total": 0, "messages": []})
        self.assertEqual(self.store.messages([], None, "müller", 0, 10), {"total": 0, "messages": []})
        self.assertEqual(self.store.folders([]), [])

    def test_source_stats_counts_and_unread_per_source(self):
        stats = self.store.source_stats(["s1", "s9", "s2"])
        self.assertEqual(stats, {"s1": (4, 2), "s9": (0, 0), "s2": (4, 2)})
        self.assertEqual(self.store.source_stats([]), {})

    def sync_extra(self, source_id: str, name: str, mails: list[tuple]) -> str:
        """Writes a fresh export with the standard fixtures plus mails (mid, subject, time, labels, unread)."""
        export = fx.make_export(self.tmp, name)
        for mid, subject, time, labels, unread in mails:
            fx.write_json(os.path.join(export, mid + ".metadata.json"), fx.meta(
                mid, subject, ("Q Sender", "q@example.test"), [("Ann", "ann@example.test")], time, labels,
                unread=unread))
        with contextlib.redirect_stderr(io.StringIO()):
            self.store.sync_source(source_id, export)
        return export

    def test_folder_count_is_distinct_messages_per_folder(self):
        labels = json.loads(json.dumps(fx.LABELS))
        labels["Payload"].append({"Path": "projects", "ID": "X4", "ParentID": None, "Name": "projects",
                                  "Color": "#888", "Type": 1})
        export = self.sync_extra("s4", "d", [])
        fx.write_json(os.path.join(export, "labels.json"), labels)
        fx.write_json(os.path.join(export, fx.ID_PLAIN + ".metadata.json"), fx.meta(
            fx.ID_PLAIN, "Grüße aus Köln", ("Jürgen Müller", "juergen@example.test"),
            [("Ann", "ann@example.test")], 1700000300, ["X1", "X4"], unread=1))
        with contextlib.redirect_stderr(io.StringIO()):
            self.store.sync_source("s4", export)
        folder = {f["key"]: f for f in self.store.folders(["s4"])}["path:projects"]
        listed = self.store.messages(["s4"], "path:projects", "", 0, 50)
        self.assertEqual(listed["total"], 3)
        self.assertEqual((folder["count"], folder["unread"]), (listed["total"], 1))

    def test_bad_time_mail_still_listed(self):
        fx.write_json(os.path.join(self.export_a, "bad1.metadata.json"),
                      fx.meta("bad1", "Bad", ("B", "b@example.test"), [], "abc", ["0"]))
        with contextlib.redirect_stderr(io.StringIO()):
            self.store.sync_source("s1", self.export_a)
        r = self.store.messages(["s1"], None, "", 0, 50)
        by_id = {m["id"]: m for m in r["messages"]}
        self.assertEqual(r["total"], 5)
        self.assertEqual(by_id["bad1"]["time"], 0)
        self.assertIn(fx.ID_PLAIN, by_id)

    def test_search_sharp_s_folds_like_the_needle(self):
        self.sync_extra("s3", "c", [("strasse1", "Straße", 1700000800, ["0"], 0)])
        r = self.store.messages(["s3"], None, "STRASSE".casefold(), 0, 50)
        self.assertEqual([m["id"] for m in r["messages"]], ["strasse1"])

    def test_short_needle_folds_non_ascii_case(self):
        self.sync_extra("s3", "c", [("ueber1", "Übersicht", 1700000700, ["0"], 0)])
        r = self.store.messages(["s3"], None, "ü", 0, 50)
        self.assertIn("ueber1", {m["id"] for m in r["messages"]})

    def test_literal_quote_and_percent_found(self):
        self.sync_extra("s3", "c", [
            ("quote1", 'Zitat "Hallo" ok', 1700000500, ["0"], 0),
            ("pct1", "100% fertig", 1700000600, ["0"], 0)])
        ids = lambda q: [m["id"] for m in self.store.messages(["s3"], None, q, 0, 50)["messages"]]
        self.assertEqual(ids('"hallo"'), ["quote1"])
        self.assertEqual(ids('"'), ["quote1"])
        self.assertEqual(ids("%"), ["pct1"])
        self.assertEqual(ids("100%"), ["pct1"])

    def test_limit_and_offset_are_clamped(self):
        r = self.store.messages(["s1"], None, "", -5, 0)
        self.assertEqual(r["total"], 4)
        self.assertEqual([m["id"] for m in r["messages"]], [fx.ID_PLAIN])


class TagQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.export = fx.make_export(self.tmp)
        self.export2 = os.path.join(self.tmp, "export2")
        shutil.copytree(self.export, self.export2)
        self.tags_db = os.path.join(self.tmp, "tags.db")
        self.tags = tags.TagStore(self.tags_db)
        self.addCleanup(self.tags.close)
        self.store = store.Store(os.path.join(self.tmp, "archive.db"), self.tags_db)
        self.addCleanup(self.store.close)
        with contextlib.redirect_stderr(io.StringIO()):
            self.store.sync_source("s1", self.export)
            self.store.sync_source("s2", self.export2)
        self.t = self.tags.create_tag("Wichtig", "#f00")["id"]

    def marks_of(self, rows):
        return {m["id"]: (m["starred"], m["tags"]) for m in rows}

    def test_rows_carry_marks(self):
        self.tags.set_marks(self.store.ident("s1", fx.ID_PLAIN), True, [self.t])
        rows = self.store.messages(["s1", "s2"], None, "", 0, 50)["messages"]
        self.assertEqual(len(rows), 8)
        for m in rows:
            with self.subTest(source=m["source"], id=m["id"]):
                if m["id"] == fx.ID_PLAIN:
                    self.assertEqual((m["starred"], m["tags"]), (True, [self.t]))
                else:
                    self.assertEqual((m["starred"], m["tags"]), (False, []))
                self.assertEqual(bool(m["ident"]), m["id"] != fx.ID_GHOST)
        self.assertEqual(self.store.messages(["s1"], None, "", 0, 50)["messages"][0]["ident"],
                         self.store.ident("s1", fx.ID_PLAIN))

    def test_filter_star(self):
        self.tags.set_marks(self.store.ident("s1", fx.ID_PLAIN), True, [])
        self.assertEqual(self.store.messages(["s1"], None, "", 0, 50, tag="star")["total"], 1)
        self.assertEqual(self.store.messages(["s1", "s2"], None, "", 0, 50, tag="star")["total"], 2)

    def test_filter_tag_combines_with_folder_and_search(self):
        self.tags.set_marks(self.store.ident("s1", fx.ID_PLAIN), None, [self.t])
        self.tags.set_marks(self.store.ident("s1", fx.ID_ALT), None, [self.t])
        self.assertEqual(self.store.messages(["s1"], None, "", 0, 50, tag=str(self.t))["total"], 2)
        r = self.store.messages(["s1"], "sys:0", "", 0, 50, tag=str(self.t))
        self.assertEqual([m["id"] for m in r["messages"]], [fx.ID_PLAIN])
        r = self.store.messages(["s1"], None, "multi", 0, 50, tag=str(self.t))
        self.assertEqual([m["id"] for m in r["messages"]], [fx.ID_ALT])

    def test_filter_unknown_tag_is_empty(self):
        self.assertEqual(self.store.messages(["s1"], None, "", 0, 50, tag="999"),
                         {"total": 0, "messages": []})

    def test_filter_rejects_non_numeric_tag(self):
        for bad in ("", "x", "-1", "1 OR 1=1"):
            with self.subTest(tag=bad):
                with self.assertRaises(ValueError):
                    self.store.messages(["s1"], None, "", 0, 50, tag=bad)

    def test_ghost_mail_never_matches(self):
        self.tags._conn().execute("INSERT INTO stars (ident) VALUES ('')")
        self.tags._conn().commit()
        self.assertEqual(self.store.messages(["s1"], None, "", 0, 50, tag="star")["total"], 0)
        ghost = [m for m in self.store.messages(["s1"], None, "", 0, 50)["messages"] if m["id"] == fx.ID_GHOST]
        self.assertEqual((ghost[0]["starred"], ghost[0]["tags"], ghost[0]["ident"]), (False, [], ""))

    def test_tag_counts(self):
        self.tags.set_marks(self.store.ident("s1", fx.ID_PLAIN), True, [self.t])
        self.tags.set_marks(self.store.ident("s1", fx.ID_ALT), None, [self.t])
        self.assertEqual(self.store.tag_counts(["s1", "s2"]), {"star": 2, str(self.t): 4})
        self.assertEqual(self.store.tag_counts(["s1"]), {"star": 1, str(self.t): 2})
        self.assertEqual(self.store.tag_counts([]), {"star": 0})

    def test_tag_counts_match_filtered_totals(self):
        self.tags.set_marks(self.store.ident("s1", fx.ID_PLAIN), True, [self.t])
        self.tags.set_marks(self.store.ident("s1", fx.ID_RICH), True, [])
        counts = self.store.tag_counts(["s1", "s2"])
        self.assertEqual(counts["star"], self.store.messages(["s1", "s2"], None, "", 0, 50, tag="star")["total"])
        self.assertEqual(counts[str(self.t)],
                         self.store.messages(["s1", "s2"], None, "", 0, 50, tag=str(self.t))["total"])

    def test_ident_lookup(self):
        self.assertTrue(self.store.ident("s1", fx.ID_PLAIN).startswith("sha256:"))
        self.assertEqual(self.store.ident("s1", fx.ID_GHOST), "")
        self.assertIsNone(self.store.ident("s1", "nope"))

    def test_store_without_tags_db_still_lists(self):
        plain = store.Store(os.path.join(self.tmp, "plain.db"))
        self.addCleanup(plain.close)
        with contextlib.redirect_stderr(io.StringIO()):
            plain.sync_source("s1", self.export)
        rows = plain.messages(["s1"], None, "", 0, 50)["messages"]
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(m["starred"] is False and m["tags"] == [] for m in rows))


if __name__ == "__main__":
    unittest.main()
