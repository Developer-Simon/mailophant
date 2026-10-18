"""Outlook message model and conversion to .eml bytes (stdlib only, no reader here)."""

import hashlib
import json
import mimetypes
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import timezone
from email import policy
from email.message import EmailMessage, Message
from email.parser import Parser
from email.utils import formataddr, formatdate, getaddresses
from typing import Protocol

from mailviewer import i18n
from mailviewer.store import META_SUFFIX, mail_ident

BOUNDARY_PREFIX = "=_mbv_"
RTF_NAME = "Nachricht.rtf"
TMP_EML = ".tmp.eml"
MAIL_CLASS_PREFIXES = ("ipm.note", "ipm.schedule.meeting", "report.ipm.note")
INBOX_NAMES = ("posteingang", "inbox")  # the mailbox top is the folder that holds one of these
# MAPI property tags used with pypff record sets
PR_MESSAGE_CLASS = 0x001A
PR_INTERNET_CPID = 0x3FDE
PR_MESSAGE_FLAGS = 0x0E07
PR_SENDER_NAME = 0x0C1A
PR_SENDER_EMAIL = 0x0C1F
PR_RECIPIENT_TYPE = 0x0C15
PR_DISPLAY_NAME = 0x3001
PR_SMTP_ADDRESS = 0x3003
PR_ATTACH_FILENAME = 0x3704
PR_ATTACH_LONG_FILENAME = 0x3707
RECIPIENT_TO = 1
RECIPIENT_CC = 2
MESSAGE_FLAG_READ = 0x1
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.IGNORECASE)
# casefolded top-level folder name -> Proton system label ID
SYSTEM_FOLDERS = {
    "posteingang": "0", "inbox": "0",
    "gesendete elemente": "7", "sent items": "7", "sent": "7",
    "gelöschte elemente": "3", "deleted items": "3", "trash": "3",
    "entwürfe": "8", "drafts": "8",
    "junk-e-mail": "4", "junk e-mail": "4", "junk email": "4", "spam": "4",
    "archive": "6", "starred": "10",
}
SYSTEM_LABEL_NAMES = {"0": "Inbox", "7": "Sent", "3": "Trash", "8": "Drafts", "4": "Spam",
                      "6": "Archive", "10": "Starred", "5": "All Mail"}
ALL_MAIL = "5"


@dataclass(frozen=True)
class Person:
    name: str
    address: str


@dataclass
class OutlookMessage:
    message_class: str  # "" counts as mail
    headers: str  # transport headers, "" if missing
    subject: str
    sender: Person
    to: list[Person]
    cc: list[Person]
    time: int  # unix seconds, 0 if unknown
    read: bool
    plain: str | None
    html: str | None
    rtf: bytes | None
    attachments: list[tuple[str, bytes]]  # (file name, data)


def build_eml(msg: OutlookMessage) -> bytes:
    """Serialize msg as an .eml. Same input gives the same bytes. Text that cannot be encoded is
    cleaned first, and a header value that cannot be represented is skipped, not raised."""
    out = EmailMessage(policy=policy.default)
    plain = _text(msg.plain) if msg.plain is not None else None
    html = _text(msg.html) if msg.html is not None else None
    attachments = [(_line(name), data) for name, data in msg.attachments]
    if plain is not None and html is not None:
        out.set_content(plain)
        out.add_alternative(html, subtype="html")
    elif plain is not None:
        out.set_content(plain)
    elif html is not None:
        out.set_content(html, subtype="html")
    else:
        out.set_content("")
        if msg.rtf is not None:
            attachments.insert(0, (RTF_NAME, msg.rtf))
    for n, (name, data) in enumerate(attachments, start=1):
        filename = name.strip() or f"Anhang-{n}"
        maintype, _, subtype = _content_type(filename).partition("/")
        out.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    if msg.headers:
        _copy_transport_headers(out, msg.headers)
    else:
        _set_header(out, "From", _address(msg.sender))
        _set_header(out, "To", ", ".join(_address(p) for p in msg.to))
        if msg.cc:
            _set_header(out, "Cc", ", ".join(_address(p) for p in msg.cc))
        _set_header(out, "Subject", _line(msg.subject))
        if msg.time > 0:
            try:
                _set_header(out, "Date", formatdate(msg.time, usegmt=True))
            except (OverflowError, OSError, ValueError):
                pass  # time out of range: leave the Date header out
    _seal(out)
    return out.as_bytes()


def _text(value: str) -> str:
    """Replace lone surrogates (not encodable as UTF-8) so the text can be written."""
    return value.encode("utf-8", "replace").decode("utf-8")


def _line(value: str) -> str:
    """Cleaned text for a single header line: no line breaks, no surrogates."""
    return " ".join(_text(value).splitlines()).strip()


def _address(p: Person) -> str:
    try:
        return formataddr((_line(p.name), _line(p.address)))
    except (ValueError, TypeError):
        return _line(p.address)


def _content_type(filename: str) -> str:
    # mimetypes depends on the system's mime.types, so RTF is pinned here. message/* (such as
    # .eml) would be base64 with a message type, which is invalid MIME, so it is sent as octet-stream.
    if filename.lower().endswith(".rtf"):
        return "application/rtf"
    ctype = mimetypes.guess_type(filename)[0] or ""
    if not ctype or ctype.partition("/")[0] == "message":
        return "application/octet-stream"
    return ctype


def _copy_transport_headers(out: EmailMessage, headers: str) -> None:
    """Keep the original transport headers, except MIME-Version and Content-*, which belong to the
    MIME tree that build_eml replaces."""
    parsed = Parser(policy=policy.default).parsestr(_text(headers), headersonly=True)
    for name, value in parsed.items():
        lowered = name.lower()
        if lowered == "mime-version" or lowered.startswith("content-"):
            continue
        _set_header(out, name, value)


def _set_header(out: Message, name: str, value) -> None:
    try:
        out[name] = value
    except (ValueError, TypeError):
        pass  # a header that cannot be represented is skipped, not fatal


def _seal(part: Message) -> None:
    """Give every multipart a boundary derived from its content, bottom-up, so output is stable."""
    if not part.is_multipart():
        return
    children = list(part.iter_parts())
    for child in children:
        _seal(child)
    digest = hashlib.sha256(b"".join(child.as_bytes() for child in children)).hexdigest()
    part.set_boundary(BOUNDARY_PREFIX + digest[:24])


@dataclass
class Unreadable:
    reason: str


@dataclass
class Folder:
    path: tuple[str, ...]  # below the mailbox top, e.g. ("Posteingang", "Projekte")
    count: int  # announced entries, for progress
    items: Callable[[], Iterator[OutlookMessage | Unreadable]]
    error: str | None = None  # folder could not be opened; items is then not called


class Reader(Protocol):
    def folders(self) -> Iterator[Folder]: ...

    def close(self) -> None: ...


@dataclass
class ImportReport:
    imported: int = 0  # mails written
    merged: int = 0  # further copies merged into a written mail
    unreadable: int = 0
    skipped: dict[str, int] = field(default_factory=dict)  # message class -> count
    failed_folders: list[str] = field(default_factory=list)  # "/"-joined paths


_FAILED = object()


def convert(reader: Reader, dest: str,
            progress: Callable[[int, int], None] | None = None) -> ImportReport:
    """Write the mails of reader into dest (existing and empty) in the Proton export layout."""
    report = ImportReport()
    folders = list(reader.folders())
    total = sum(f.count for f in folders)
    dissolve = _folders_dissolve(folders)
    labels: dict[str, dict] = {ALL_MAIL: _label_entry(ALL_MAIL, SYSTEM_LABEL_NAMES[ALL_MAIL])}
    done = 0
    for folder in folders:
        failed_path = "/".join(folder.path)
        path = _mailbox_path(folder.path, dissolve)
        label_id, label = _folder_label(path)
        if folder.error is not None:
            report.failed_folders.append(failed_path)
            continue
        for entry in _entries(folder):
            if entry is _FAILED:
                report.unreadable += 1
                report.failed_folders.append(failed_path)
                break
            done += 1
            if progress:
                progress(done, total)
            if isinstance(entry, Unreadable):
                report.unreadable += 1
            elif _is_mail(entry.message_class):
                labels[label_id] = label
                _store_mail(dest, entry, [label_id, ALL_MAIL], report)
            else:
                report.skipped[entry.message_class] = report.skipped.get(entry.message_class, 0) + 1
    with open(os.path.join(dest, "labels.json"), "w", encoding="utf-8") as f:
        json.dump({"Version": 1, "Payload": list(labels.values())}, f, ensure_ascii=False)
    if progress:
        progress(total, total)
    return report


def _entries(folder: Folder) -> Iterator[OutlookMessage | Unreadable | object]:
    """Entries of folder; a failure while reading them ends the folder with _FAILED."""
    try:
        items = iter(folder.items())
        while True:
            try:
                entry = next(items)
            except StopIteration:
                return
            yield entry
    except Exception:
        yield _FAILED


def _is_mail(message_class: str) -> bool:
    cls = message_class.casefold()
    return cls == "" or cls.startswith(MAIL_CLASS_PREFIXES)


def _folders_dissolve(folders: list[Folder]) -> bool:
    """A top-level "Folders" without mails of its own is only a container: its children move up a level."""
    return not any(len(f.path) == 1 and f.path[0].casefold() == "folders" and f.count > 0 for f in folders)


def _mailbox_path(path: tuple[str, ...], dissolve: bool) -> tuple[str, ...]:
    if dissolve and len(path) >= 2 and path[0].casefold() == "folders":
        return path[1:]
    return path


def _folder_label(path: tuple[str, ...]) -> tuple[str, dict]:
    if len(path) == 1 and path[0].casefold() in SYSTEM_FOLDERS:
        label_id = SYSTEM_FOLDERS[path[0].casefold()]
        return label_id, _label_entry(label_id, SYSTEM_LABEL_NAMES[label_id])
    parts = [part.replace("/", "-") for part in path]
    path_str = "/".join(parts)
    label_id = "f" + hashlib.sha256(path_str.encode("utf-8")).hexdigest()[:16]
    return label_id, {"ID": label_id, "Name": parts[-1], "Path": path_str, "Color": "", "Type": 3}


def _label_entry(label_id: str, name: str) -> dict:
    return {"ID": label_id, "Name": name, "Path": name, "Color": "", "Type": 3}


def _store_mail(dest: str, msg: OutlookMessage, label_ids: list[str], report: ImportReport) -> None:
    """Write one copy of msg; a copy whose mail is already there only adds its labels to that mail."""
    tmp = os.path.join(dest, TMP_EML)
    with open(tmp, "wb") as f:
        f.write(build_eml(msg))
    ident = mail_ident(tmp)
    if not ident:
        raise OSError(f"cannot read back {tmp}")
    mid = "o" + hashlib.sha256(ident.encode("utf-8")).hexdigest()[:40]
    eml = os.path.join(dest, mid + ".eml")
    meta_path = os.path.join(dest, mid + META_SUFFIX)
    if os.path.exists(eml):
        os.remove(tmp)
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)["Payload"]
        ids = meta.get("LabelIDs") or []
        meta["LabelIDs"] = ids + [lid for lid in label_ids if lid not in ids]
        report.merged += 1
    else:
        os.replace(tmp, eml)
        meta = _metadata(mid, msg, label_ids)
        report.imported += 1
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({"Version": 1, "Payload": meta}, f, ensure_ascii=False)


def _metadata(mid: str, msg: OutlookMessage, label_ids: list[str]) -> dict:
    # build_eml attaches the RTF body only when there is no plain or HTML body
    rtf_attached = msg.plain is None and msg.html is None and msg.rtf is not None
    return {
        "ID": mid,
        "LabelIDs": list(label_ids),
        "Subject": msg.subject,
        "Sender": _person(msg.sender),
        "ToList": [_person(p) for p in msg.to],
        "CCList": [_person(p) for p in msg.cc],
        "Time": msg.time,
        "Unread": 0 if msg.read else 1,
        "NumAttachments": len(msg.attachments) + (1 if rtf_attached else 0),
    }


def _person(p: Person) -> dict:
    return {"Name": p.name, "Address": p.address}


def install_hint() -> i18n.Msg:
    """How to get pypff, by how the app was installed (Electron sets MAILVIEWER_INSTALL_MODE)."""
    mode = os.environ.get("MAILVIEWER_INSTALL_MODE")
    return i18n.msg({"system": "err.pst_missing_system", "bundled": "err.pst_missing_bundled"}.get(mode, "err.pst_missing_pip"))


class ImportUnavailable(Exception):
    """pypff is not installed. msg says how to install it."""

    def __init__(self) -> None:
        self.msg = install_hint()
        super().__init__(self.msg.text)


class MailboxError(OSError):
    """A mailbox problem the user sees: constructed with a message code and its parameters."""

    def __init__(self, code: str, **params) -> None:
        self.msg = i18n.msg(code, **params)
        super().__init__(self.msg.text)


def pypff_available() -> bool:
    try:
        import pypff  # noqa: F401
    except ImportError:
        return False
    return True


def _import_pypff():
    try:
        import pypff
    except ImportError as exc:
        raise ImportUnavailable() from exc
    return pypff


def open_reader(path: str) -> Reader:
    """Open a PST or OST file. Raises ImportUnavailable without pypff, OSError if unreadable."""
    _import_pypff()
    return PypffReader(path)


class PypffReader:
    """Reads a PST or OST file with libpff-python. Implements Reader."""

    def __init__(self, path: str) -> None:
        pypff = _import_pypff()
        self._file = pypff.file()
        try:
            self._file.open(path)
            self._top = _mailbox_top(self._file.get_root_folder())
        except OSError:
            self._file.close()
            raise
        except Exception as exc:
            self._file.close()
            raise MailboxError("err.file_unreadable", detail=i18n.detail(exc)) from exc

    def folders(self) -> Iterator[Folder]:
        """All folders below the mailbox top. Folders outside it (search folders etc.) are left out."""
        yield from self._children(self._top, ())

    def close(self) -> None:
        self._file.close()

    def _children(self, parent, path: tuple[str, ...]) -> Iterator[Folder]:
        try:
            count = parent.number_of_sub_folders
        except Exception as exc:
            # the subtree cannot be listed: report it, so it does not vanish silently
            yield Folder(path + ("(Unterordner)",), 0, lambda: iter(()), error=str(exc) or "unreadable")
            return
        for index in range(count):
            try:
                sub = parent.get_sub_folder(index)
                name = sub.name or "(ohne Name)"
            except Exception as exc:
                yield Folder(path + (f"#{index}",), 0, lambda: iter(()), error=str(exc) or "unreadable")
                continue
            sub_path = path + (name,)
            yield self._folder(sub, sub_path)
            yield from self._children(sub, sub_path)

    def _folder(self, folder, path: tuple[str, ...]) -> Folder:
        try:
            count = folder.number_of_sub_messages
        except Exception as exc:
            return Folder(path, 0, lambda: iter(()), error=str(exc) or "unreadable")
        return Folder(path, count, lambda: _messages(folder, count))


def _mailbox_top(root):
    """The child of the root folder that holds the inbox; its own name is not part of any path."""
    for index in range(root.number_of_sub_folders):
        try:
            child = root.get_sub_folder(index)
        except Exception:
            continue
        if _has_inbox(child):
            return child
    raise MailboxError("err.no_inbox")


def _has_inbox(folder) -> bool:
    for index in range(folder.number_of_sub_folders):
        try:
            name = folder.get_sub_folder(index).name
        except Exception:
            continue
        if (name or "").casefold() in INBOX_NAMES:
            return True
    return False


def _messages(folder, count: int) -> Iterator[OutlookMessage | Unreadable]:
    """Messages of one folder, read one at a time. A message that fails becomes Unreadable."""
    for index in range(count):
        try:
            entry = _read_message(folder.get_sub_message(index))
        except Exception as exc:
            entry = Unreadable(str(exc) or "unreadable")
        yield entry


def _read_message(item) -> OutlookMessage:
    rs = item.get_record_set(0)
    headers = item.transport_headers or ""
    codepage = _int_value(rs, PR_INTERNET_CPID)
    flags = _int_value(rs, PR_MESSAGE_FLAGS)
    to, cc = _recipients(item)
    return OutlookMessage(
        message_class=_text_value(rs, PR_MESSAGE_CLASS) or "",
        headers=headers,
        subject=item.subject or "",
        sender=_sender(headers, rs),
        to=to,
        cc=cc,
        time=_message_time(item),
        read=flags is not None and bool(flags & MESSAGE_FLAG_READ),
        plain=_decode_body(item.plain_text_body, codepage, html=False),
        html=_decode_body(item.html_body, codepage, html=True),
        rtf=item.rtf_body or None,
        attachments=list(_attachments(item)),
    )


def _sender(headers: str, rs) -> Person:
    """From header of the transport headers if it has an address, else the sender record."""
    if headers:
        try:
            value = Parser(policy=policy.default).parsestr(headers, headersonly=True).get("From")
            if value is not None:
                for name, address in getaddresses([str(value)]):
                    if address:
                        return Person(name, address)
        except Exception:
            pass  # unparsable header: fall back to the record set
    return Person(_text_value(rs, PR_SENDER_NAME) or "", _text_value(rs, PR_SENDER_EMAIL) or "")


def _recipients(item) -> tuple[list[Person], list[Person]]:
    """To and Cc recipients; Bcc is not written. Recipients without an address are left out."""
    to: list[Person] = []
    cc: list[Person] = []
    recipients = item.recipients
    for index in range(recipients.number_of_recipients if recipients else 0):
        rs = recipients.get_record_set(index)
        kind = _int_value(rs, PR_RECIPIENT_TYPE)
        if kind not in (RECIPIENT_TO, RECIPIENT_CC):
            continue
        address = _text_value(rs, PR_SMTP_ADDRESS) or ""
        if not address:
            continue
        person = Person(_text_value(rs, PR_DISPLAY_NAME) or "", address)
        (to if kind == RECIPIENT_TO else cc).append(person)
    return to, cc


def _attachments(item) -> Iterator[tuple[str, bytes]]:
    # get_attachment must not be called on a message without attachments (pypff raises MemoryError)
    for index in range(item.number_of_attachments):
        try:
            attachment = item.get_attachment(index)
            rs = attachment.get_record_set(0)
            name = _text_value(rs, PR_ATTACH_LONG_FILENAME) or _text_value(rs, PR_ATTACH_FILENAME) or ""
            size = attachment.size
            data = attachment.read_buffer(size) if size else b""
        except Exception:
            continue  # an attachment that cannot be read is skipped, the mail is kept
        yield name, data


def _message_time(item) -> int:
    for attr in ("delivery_time", "client_submit_time", "creation_time"):
        try:
            seconds = _unix_seconds(getattr(item, attr))
        except Exception:
            continue
        if seconds > 0:
            return seconds
    return 0


def _unix_seconds(value) -> int:
    """pypff times are naive and in UTC."""
    if value is None:
        return 0
    try:
        return max(0, int(value.replace(tzinfo=timezone.utc).timestamp()))
    except (AttributeError, OverflowError, OSError, ValueError):
        return 0


def _decode_body(data: bytes | None, codepage: int | None, html: bool) -> str | None:
    """HTML: charset from its meta tag, then the mail's codepage. Text: the codepage. Else UTF-8."""
    if not data:
        return None
    names: list[str] = []
    if html:
        match = _META_CHARSET.search(data)
        if match:
            names.append(match.group(1).decode("ascii"))
    if codepage:
        names.append(f"cp{codepage}")
    for name in names:
        try:
            return data.decode(name, errors="replace")
        except LookupError:
            continue
    return data.decode("utf-8", errors="replace")


def _entry(rs, entry_type: int):
    for index in range(rs.number_of_entries):
        entry = rs.get_entry(index)
        if entry.entry_type == entry_type:
            return entry
    return None


def _text_value(rs, entry_type: int) -> str | None:
    entry = _entry(rs, entry_type)
    if entry is None:
        return None
    try:
        return entry.data_as_string
    except Exception:
        return None


def _int_value(rs, entry_type: int) -> int | None:
    entry = _entry(rs, entry_type)
    if entry is None:
        return None
    try:
        return entry.data_as_integer
    except Exception:
        return None
