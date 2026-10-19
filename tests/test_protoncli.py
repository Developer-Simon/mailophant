"""Tests for protoncli.py. Downloads are served from in-memory archives, nothing touches the network."""

import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import tarfile
import tempfile
import threading
import unittest
import zipfile
from unittest import mock
from urllib.error import URLError

from mailviewer import protoncli, server
from mailviewer.i18n import UserError
from tests.test_server import ACCOUNT, OLD, make_export, make_library, request, wait_job

LINUX = "linux-x64"
MAC = "darwin"
MAC_EXE = protoncli.ARCHIVES[MAC][1]


def tar_gz(files: dict[str, tuple[bytes, int]], links: tuple[str, ...] = ()) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, (data, mode) in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tar.addfile(info, io.BytesIO(data))
        for name in links:
            info = tarfile.TarInfo(name)
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tar.addfile(info)
    return buf.getvalue()


def zip_bytes(files: dict[str, tuple[bytes, int]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, (data, mode) in files.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            zf.writestr(info, data)
    return buf.getvalue()


LINUX_ARCHIVE = tar_gz({"proton-mail-export-cli": (b"#!/bin/sh\n", 0o755),
                        "proton-mail-export.so": (b"lib", 0o644)})


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, headers: dict | None = None):
        super().__init__(data)
        self.headers = headers if headers is not None else {"Content-Length": str(len(data))}


def opener(archive: bytes, version: bytes = b'{"version": "1.0.7"}', headers: dict | None = None):
    def open_(url):
        if url == protoncli.VERSION_URL:
            return FakeResponse(version)
        return FakeResponse(archive, headers)
    return open_


def failing(url):
    raise URLError("no route to host")


class PlatformTests(unittest.TestCase):
    def test_platform_keys(self):
        self.assertEqual(protoncli.platform_key("linux", "x86_64"), LINUX)
        self.assertIsNone(protoncli.platform_key("linux", "aarch64"))
        self.assertEqual(protoncli.platform_key("darwin", "arm64"), MAC)
        self.assertEqual(protoncli.platform_key("win32", "AMD64"), "win-x64")

    def test_install_dir_override(self):
        with mock.patch.dict(os.environ, {"MAILOPHANT_CLI_DIR": "/opt/cli"}):
            self.assertEqual(protoncli.default_install_dir(), os.path.abspath("/opt/cli"))


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.logged = []

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def install(self, archive=LINUX_ARCHIVE, key=LINUX, open_=None, **kw):
        return protoncli.install(self.dir, self.logged.append, lambda done, total: None,
                                 opener=open_ or opener(archive, **kw), key=key)

    def leftovers(self):
        return sorted(n for n in os.listdir(self.dir) if n.startswith("."))

    def test_installs_tarball_with_manifest(self):
        path = self.install()
        self.assertEqual(path, os.path.join(self.dir, "current", "proton-mail-export-cli"))
        self.assertEqual(protoncli.installed_cli(self.dir, LINUX), path)
        info = protoncli.installed_info(self.dir, LINUX)
        self.assertEqual((info["path"], info["version"]), (path, "1.0.7"))
        with open(os.path.join(self.dir, "install.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(manifest["url"], protoncli.BASE_URL + protoncli.ARCHIVES[LINUX][0])
        self.assertEqual(len(manifest["sha256"]), 64)
        self.assertEqual([m.code for m in self.logged], ["log.cli_download", "log.cli_downloaded", "log.cli_installed"])
        self.assertEqual(self.leftovers(), [])

    def test_zip_keeps_executable_bits(self):
        archive = zip_bytes({"proton-mail-export-cli.app/Contents/": (b"", 0o40755),
                             MAC_EXE: (b"\xcf\xfa\xed\xfe", 0o100755),
                             "proton-mail-export-cli.app/Contents/Info.plist": (b"<plist/>", 0o100644)})
        path = self.install(archive, MAC)
        self.assertTrue(os.stat(path).st_mode & stat.S_IXUSR)
        plist = os.path.join(self.dir, "current", "proton-mail-export-cli.app", "Contents", "Info.plist")
        self.assertFalse(os.stat(plist).st_mode & stat.S_IXUSR)

    def test_version_is_optional(self):
        self.install(version=b"not json")
        self.assertIsNone(protoncli.installed_info(self.dir, LINUX)["version"])

    def test_reinstall_replaces_current(self):
        self.install()
        stale = os.path.join(self.dir, "current", "stale")
        open(stale, "w").close()
        self.install()
        self.assertFalse(os.path.exists(stale))
        self.assertEqual(self.leftovers(), [])

    def assert_refused(self, code, **kw):
        with self.assertRaises(UserError) as cm:
            self.install(**kw)
        self.assertEqual(cm.exception.msg.code, code)
        self.assertEqual(self.leftovers(), [])

    def test_path_traversal_refused(self):
        self.assert_refused("err.cli_archive", archive=tar_gz({"../evil": (b"x", 0o755)}))
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.dir), "evil")))

    def test_absolute_path_refused(self):
        self.assert_refused("err.cli_archive", archive=zip_bytes({"/tmp/evil": (b"x", 0o100755)}))

    def test_symlink_refused(self):
        self.assert_refused("err.cli_archive", archive=tar_gz({}, links=("proton-mail-export-cli",)))

    def test_missing_executable_refused(self):
        self.assert_refused("err.cli_archive", archive=tar_gz({"README": (b"x", 0o644)}))

    def test_corrupt_archive_refused(self):
        self.assert_refused("err.cli_archive", archive=b"not a tarball")

    def test_too_large_refused(self):
        self.assert_refused("err.cli_too_large", headers={"Content-Length": str(protoncli.MAX_DOWNLOAD + 1)})

    def test_failed_download_keeps_previous_install(self):
        path = self.install()
        self.assert_refused("err.cli_download", open_=failing)
        self.assertEqual(protoncli.installed_cli(self.dir, LINUX), path)

    def test_pinned_release_from_github(self):
        urls = []
        def open_(url):
            urls.append(url)
            return FakeResponse(LINUX_ARCHIVE)
        sha = hashlib.sha256(LINUX_ARCHIVE).hexdigest()
        with mock.patch.dict(protoncli.PINNED_SHA256, {LINUX: sha}), \
                mock.patch.dict(os.environ, {"MAILOPHANT_CLI_PINNED": "1"}):
            self.install(open_=open_)
        self.assertEqual(urls, [protoncli.PINNED_URL + protoncli.ARCHIVES[LINUX][0]])  # no version.json
        self.assertIn(f"/download/v{protoncli.PINNED_VERSION}/", urls[0])
        self.assertEqual(protoncli.installed_info(self.dir, LINUX)["version"], protoncli.PINNED_VERSION)

    def test_pinned_checksum_mismatch_keeps_previous_install(self):
        path = self.install()
        with mock.patch.dict(os.environ, {"MAILOPHANT_CLI_PINNED": "1"}):
            self.assert_refused("err.cli_checksum")
        self.assertEqual(protoncli.installed_cli(self.dir, LINUX), path)

    def test_pinned_hashes_cover_every_archive(self):
        self.assertEqual(protoncli.PINNED_SHA256.keys(), protoncli.ARCHIVES.keys())

    def test_unsupported_platform(self):
        with mock.patch.object(protoncli, "platform_key", return_value=None):
            with self.assertRaises(UserError) as cm:
                protoncli.install(self.dir, self.logged.append, lambda d, t: None, opener=failing)
        self.assertEqual(cm.exception.msg.code, "err.cli_unsupported")


@unittest.skipUnless(protoncli.platform_key() == LINUX, "the fake CLI is a Linux tarball")
class ServerInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        account = os.path.join(self.tmp, ACCOUNT)
        make_export(account, OLD)
        library = make_library(self.tmp, account)  # the account dir makes it a proton source
        self.cli_dir = os.path.join(self.tmp, "cli")
        self.srv = server.make_server(library, 0, self.tmp, config_path=os.path.join(self.tmp, "config.json"),
                                      cli_dir=self.cli_dir)
        self.port = self.srv.server_port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        wait_job(self.port)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.srv.viewer.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, origin="same"):
        headers = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"}
        if origin != "same":
            headers["Origin"] = origin
        return request(self.port, "POST", "/api/cli/install", b"{}", headers)

    def test_install_makes_proton_source_fetchable(self):
        _, sources = request(self.port, "GET", "/api/sources")
        self.assertFalse(sources[0]["fetchable"])
        _, cfg = request(self.port, "GET", "/api/config")
        self.assertEqual((cfg["cli_installed"], cfg["cli_installable"]), (None, True))
        real = protoncli.install
        with mock.patch.object(protoncli, "install",
                               lambda d, log, progress: real(d, log, progress, opener=opener(LINUX_ARCHIVE))):
            self.assertEqual(self.post(), (202, {"state": "running"}))
            st = wait_job(self.port)
        self.assertEqual((st["state"], st["kind"]), ("done", "cli_install"))
        path = os.path.join(self.cli_dir, "current", "proton-mail-export-cli")
        self.assertEqual(st["result"], {"path": path})
        _, cfg = request(self.port, "GET", "/api/config")
        self.assertEqual(cfg["cli_installed"]["path"], path)
        _, sources = request(self.port, "GET", "/api/sources")
        self.assertTrue(sources[0]["fetchable"])

    def test_failed_install_is_job_error(self):
        real = protoncli.install
        with mock.patch.object(protoncli, "install",
                               lambda d, log, progress: real(d, log, progress, opener=failing)):
            self.post()
            st = wait_job(self.port)
        self.assertEqual((st["state"], st["message"]["code"]), ("error", "err.cli_download"))

    def test_foreign_origin_refused(self):
        status, _ = self.post(origin="http://evil.test")
        self.assertEqual(status, 403)
        self.assertFalse(os.path.exists(self.cli_dir))
