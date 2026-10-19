"""Download and install the official Proton Mail Export CLI into Mailophant's data folder.

Proton publishes one archive per platform under a fixed URL and no checksums, so the archive is
trusted on the strength of HTTPS to proton.me. With MAILOPHANT_CLI_PINNED=1 (the Flatpak) it comes
instead from the GitHub release PINNED_VERSION and must match the SHA-256 recorded here. It is
unpacked into a staging folder with every member checked (no absolute paths, no "..", no links),
and only then swapped in as current/.
install.json next to it records where it came from and its SHA-256."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import ssl
import stat
import sys
import tarfile
import tempfile
import time
import zipfile
from collections.abc import Callable
from urllib.request import Request, urlopen

from mailviewer.cleanup import remove_quiet
from mailviewer.i18n import Msg, UserError, msg

BASE_URL = "https://proton.me/download/export-tool/"
VERSION_URL = BASE_URL + "version.json"
MAX_DOWNLOAD = 200 * 1024 * 1024
CHUNK = 256 * 1024
TIMEOUT = 60
CURRENT = "current"
MANIFEST = "install.json"

PINNED_VERSION = "1.0.7"
PINNED_URL = f"https://github.com/ProtonMail/proton-mail-export/releases/download/v{PINNED_VERSION}/"
# SHA-256 of the release assets per platform key, as GitHub lists them for the tag.
PINNED_SHA256 = {
    "linux-x64": "350b7d9884c1fd75236d2902ecc5e5d4155f829c864817c6ad5e8907c9f06bd6",
    "darwin": "98a077356909b177102307d200e81fe93954a281bc765be879df10e3864bfa1e",
    "win-x64": "a9a22d1d5d8755cabe904e9645515c35a88aabe54a0b59eac399a3eef04ce139",
}

# (archive name, path of the executable inside it) per platform key.
ARCHIVES = {
    "linux-x64": ("proton-mail-export-cli-linux_x86_64.tar.gz", "proton-mail-export-cli"),
    "darwin": ("proton-mail-export-cli-macos.zip",  # universal binary: x86_64 and arm64
               "proton-mail-export-cli.app/Contents/MacOS/proton-mail-export-cli"),
    "win-x64": ("proton-mail-export-cli-windows_x86_64.zip", "proton-mail-export-cli.exe"),
}


def platform_key(system: str | None = None, machine: str | None = None) -> str | None:
    """Key into ARCHIVES for this machine, or None if Proton ships no CLI for it."""
    system = system or sys.platform
    machine = (machine or platform.machine()).lower()
    if system == "darwin":
        return "darwin"
    if system.startswith("linux") and machine in ("x86_64", "amd64"):
        return "linux-x64"
    if system == "win32" and machine in ("x86_64", "amd64", "arm64"):  # arm64 runs it emulated
        return "win-x64"
    return None


def pinned() -> bool:
    """Whether to install the pinned GitHub release instead of Proton's latest download."""
    return os.environ.get("MAILOPHANT_CLI_PINNED") == "1"


def default_install_dir() -> str:
    """MAILOPHANT_CLI_DIR, else the per-user data folder of the platform."""
    override = os.environ.get("MAILOPHANT_CLI_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return os.path.join(base, "Mailophant", "proton-export-cli")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "Mailophant", "proton-export-cli")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return os.path.join(base, "mailophant", "proton-export-cli")


def installed_cli(install_dir: str, key: str | None = None) -> str | None:
    """Path of the installed executable, or None if nothing usable is installed."""
    key = key or platform_key()
    if key is None:
        return None
    path = os.path.join(install_dir, CURRENT, *ARCHIVES[key][1].split("/"))
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


def installed_info(install_dir: str, key: str | None = None) -> dict | None:
    """Path and manifest of the installed CLI for /api/config, or None."""
    path = installed_cli(install_dir, key)
    if path is None:
        return None
    try:
        with open(os.path.join(install_dir, MANIFEST), encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}
    return {"path": path, "version": manifest.get("version"), "installed": manifest.get("installed")}


def _ssl_context() -> ssl.SSLContext:
    """Default verification. The bundled Python on macOS may not find the system CAs, so fall back
    to the file macOS ships."""
    ctx = ssl.create_default_context()
    if sys.platform == "darwin" and not ctx.cert_store_stats().get("x509_ca"):
        for cafile in ("/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem"):
            if os.path.isfile(cafile):
                ctx.load_verify_locations(cafile)
                break
    return ctx


Opener = Callable[[str], object]  # url -> response with .read(n), .headers and a context manager


def _open(url: str):
    return urlopen(Request(url, headers={"User-Agent": "Mailophant"}), timeout=TIMEOUT, context=_ssl_context())


def _download(url: str, dest: str, opener: Opener, progress: Callable[[int, int], None]) -> tuple[str, str]:
    """Write url to dest. Returns (sha256, Last-Modified header). UserError on any failure."""
    digest = hashlib.sha256()
    try:
        with opener(url) as res, open(dest, "wb") as f:
            total = int(res.headers.get("Content-Length") or 0)
            if total > MAX_DOWNLOAD:
                raise UserError("err.cli_too_large")
            done = 0
            while chunk := res.read(CHUNK):
                done += len(chunk)
                if done > MAX_DOWNLOAD:
                    raise UserError("err.cli_too_large")
                digest.update(chunk)
                f.write(chunk)
                progress(done, total)
            return digest.hexdigest(), res.headers.get("Last-Modified") or ""
    except UserError:
        raise
    except (OSError, ValueError) as exc:  # URLError, HTTPError, timeouts and SSL errors are OSErrors
        raise UserError("err.cli_download", detail=str(getattr(exc, "reason", None) or exc)) from exc


def _safe_name(name: str) -> str:
    """The member path as relative parts joined by "/", or UserError if it would leave the folder."""
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or name.startswith(("/", "\\")) or ".." in parts or ":" in parts[0]:
        raise UserError("err.cli_archive", detail=name)
    return "/".join(parts)


def _extract_tar(archive: str, dest: str) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            rel = _safe_name(member.name)
            target = os.path.join(dest, *rel.split("/"))
            if member.isdir():
                os.makedirs(target, exist_ok=True)
            elif member.isfile():
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = tar.extractfile(member)
                with src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
            else:  # links and devices could point outside dest
                raise UserError("err.cli_archive", detail=member.name)


def _extract_zip(archive: str, dest: str) -> None:
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            rel = _safe_name(info.filename)
            target = os.path.join(dest, *rel.split("/"))
            mode = info.external_attr >> 16
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            if stat.S_ISLNK(mode):
                raise UserError("err.cli_archive", detail=info.filename)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            # zipfile drops the permission bits; the macOS app needs its executables back.
            os.chmod(target, 0o755 if mode & 0o111 or sys.platform == "win32" else 0o644)


def _extract(archive: str, name: str, dest: str) -> None:
    try:
        if name.endswith(".tar.gz"):
            _extract_tar(archive, dest)
        else:
            _extract_zip(archive, dest)
    except UserError:
        raise
    except (OSError, tarfile.TarError, zipfile.BadZipFile, EOFError) as exc:
        raise UserError("err.cli_archive", detail=str(exc)) from exc


def _version(opener: Opener) -> str | None:
    """Version Proton currently publishes; best effort, it is only shown."""
    try:
        with opener(VERSION_URL) as res:
            data = json.loads(res.read(4096))
        version = data.get("version") if isinstance(data, dict) else None
        return version if isinstance(version, str) and len(version) < 32 else None
    except (OSError, ValueError):
        return None


def install(install_dir: str, log: Callable[[Msg], None], progress: Callable[[int, int], None],
            opener: Opener = _open, key: str | None = None, pin: bool | None = None) -> str:
    """Download the CLI for this platform and make it install_dir/current. Returns the executable.
    pin=None follows MAILOPHANT_CLI_PINNED. The previous install stays in place until the new one is
    complete. Raises UserError."""
    key = key or platform_key()
    if key is None:
        raise UserError("err.cli_unsupported", platform=f"{sys.platform} {platform.machine()}")
    pin = pinned() if pin is None else pin
    name, exe = ARCHIVES[key]
    url = (PINNED_URL if pin else BASE_URL) + name
    os.makedirs(install_dir, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".new-", dir=install_dir)
    archive = os.path.join(install_dir, f".{name}.part")
    old = None
    try:
        log(msg("log.cli_download", url=url))
        sha256, modified = _download(url, archive, opener, progress)
        log(msg("log.cli_downloaded", size=os.path.getsize(archive), sha256=sha256))
        if pin and sha256 != PINNED_SHA256[key]:
            raise UserError("err.cli_checksum", expected=PINNED_SHA256[key], actual=sha256)
        _extract(archive, name, staging)
        path = os.path.join(staging, *exe.split("/"))
        if not os.path.isfile(path):
            raise UserError("err.cli_archive", detail=exe)
        current = os.path.join(install_dir, CURRENT)
        if os.path.exists(current):
            old = tempfile.mkdtemp(prefix=".old-", dir=install_dir)
            os.rmdir(old)
            os.replace(current, old)
        os.replace(staging, current)
        staging = None
        manifest = {"url": url, "sha256": sha256, "last_modified": modified,
                    "version": PINNED_VERSION if pin else _version(opener), "installed": int(time.time())}
        tmp = os.path.join(install_dir, MANIFEST + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(manifest, f)
        os.replace(tmp, os.path.join(install_dir, MANIFEST))
        installed = os.path.join(current, *exe.split("/"))
        log(msg("log.cli_installed", path=installed))
        return installed
    except OSError as exc:  # moving the folders, e.g. a file of the old install is still open
        if old is not None and not os.path.exists(os.path.join(install_dir, CURRENT)):
            os.replace(old, os.path.join(install_dir, CURRENT))
            old = None
        raise UserError("err.cli_install", detail=str(exc.strerror or exc)) from exc
    finally:
        remove_quiet(archive)
        for leftover in (staging, old):
            if leftover is not None:
                shutil.rmtree(leftover, ignore_errors=True)
