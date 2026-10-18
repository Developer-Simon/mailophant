"""Build a demo library with made-up mails for the README screenshots.

Usage: python3 scripts/docs/demo_library.py OUT_DIR

OUT_DIR must not exist. It gets:
  accounts/alex@example.com/mail_*/   a Proton account with one export
  exports/work/                       a single Proton export folder
  library/                            the library: library.json, tags.db and, in sources/,
                                      an "imported" Outlook source and an archive with folders
Every person, address and company in here is invented.
"""

from __future__ import annotations

import json
import math
import os
import struct
import sys
import zlib
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from mailviewer import archive  # noqa: E402
from mailviewer.library import Library  # noqa: E402
from mailviewer.server import TAGS_DB  # noqa: E402
from mailviewer.tags import TagStore  # noqa: E402

ME = ("Alex Morgan", "alex@example.com")
WORK_ME = ("Alex Morgan", "alex.morgan@northwind.example")

INBOX, ALL_MAIL, SENT, ARCHIVE_LABEL = "0", "5", "7", "6"

PROTON_LABELS = [
    {"ID": "0", "Name": "Inbox", "Path": "Inbox", "Color": "", "Type": 3},
    {"ID": "8", "Name": "Drafts", "Path": "Drafts", "Color": "", "Type": 3},
    {"ID": "7", "Name": "Sent", "Path": "Sent", "Color": "", "Type": 3},
    {"ID": "6", "Name": "Archive", "Path": "Archive", "Color": "", "Type": 3},
    {"ID": "4", "Name": "Spam", "Path": "Spam", "Color": "", "Type": 3},
    {"ID": "3", "Name": "Trash", "Path": "Trash", "Color": "", "Type": 3},
    {"ID": "5", "Name": "All Mail", "Path": "All Mail", "Color": "", "Type": 3},
    {"ID": "F1", "Name": "Home", "Path": "Home", "Color": "#5b8def", "Type": 3},
    {"ID": "F2", "Name": "Bills", "Path": "Home/Bills", "Color": "#5b8def", "ParentID": "F1", "Type": 3},
    {"ID": "F3", "Name": "Newsletters", "Path": "Newsletters", "Color": "#8a6d00", "Type": 3},
]


def ts(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())


# ---------- attachments ----------

def png(width: int, height: int, pixel) -> bytes:
    rows = b"".join(b"\0" + b"".join(bytes(pixel(x, y)) for x in range(width)) for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b"")


def mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def sunset(width: int = 720, height: int = 480, seed: float = 0.0) -> bytes:
    """Sky gradient, a sun and two rows of hills."""
    sun_x, sun_y, sun_r = width * (0.62 + seed), height * 0.52, height * 0.13

    def pixel(x, y):
        t = y / height
        sky = mix((38, 52, 110), (247, 160, 98), min(1.0, t * 1.5))
        if math.hypot(x - sun_x, y - sun_y) < sun_r:
            sky = (255, 214, 140)
        far = height * (0.62 + 0.05 * math.sin(x / width * 5 + seed * 9))
        near = height * (0.74 + 0.06 * math.sin(x / width * 3.2 + 1.7 + seed * 5))
        if y > near:
            return mix((48, 62, 58), (24, 32, 30), (y - near) / (height - near))
        if y > far:
            return (86, 92, 104)
        return sky

    return png(width, height, pixel)


def harbour(width: int = 720, height: int = 480) -> bytes:
    """Blue sky, sea and a row of houses."""
    def pixel(x, y):
        if y > height * 0.7:
            wave = 8 * math.sin(x / 23 + y / 7)
            return mix((40, 110, 150), (20, 70, 110), (y - height * 0.7) / (height * 0.3) + wave / 200)
        house = int(x / (width / 9))
        top = height * (0.42 + 0.08 * ((house * 37) % 5) / 4)
        if y > top:
            colors = [(232, 196, 120), (214, 120, 90), (236, 232, 220), (120, 160, 190), (230, 170, 110)]
            c = colors[house % len(colors)]
            if (x % (width / 9)) % 26 < 9 and (y % 30) < 14:
                return (60, 70, 80)
            return c
        return mix((120, 180, 230), (200, 228, 245), y / (height * 0.5))

    return png(width, height, pixel)


def pdf(title: str, lines: list[str]) -> bytes:
    """A one-page PDF with a heading and some lines of text."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    ops = ["BT /F2 20 Tf 56 770 Td (" + esc(title) + ") Tj ET",
           "0.85 0.85 0.85 RG 56 752 m 540 752 l S"]
    y = 720
    for line in lines:
        font = "/F2" if line.startswith("**") else "/F1"
        left, _, right = line.removeprefix("**").partition("\t")
        ops.append(f"BT {font} 11 Tf 56 {y} Td ({esc(left)}) Tj ET")
        if right:
            ops.append(f"BT {font} 11 Tf 440 {y} Td ({esc(right)}) Tj ET")
        y -= 20
    stream = "\n".join(ops).replace("€", "EUR").encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for n, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return out


# ---------- mails ----------

class Mail:
    def __init__(self, key: str, subject: str, sender, to, when: str, body: str, *, html: str | None = None,
                 cc=(), attachments=(), unread: bool = False):
        self.key, self.subject, self.sender, self.to, self.cc = key, subject, sender, list(to), list(cc)
        self.time = ts(when)
        self.body, self.html, self.attachments, self.unread = body, html, list(attachments), unread
        self.msgid = f"<{key}.mailophant-demo@{sender[1].split('@')[1]}>"

    def eml(self) -> bytes:
        msg = EmailMessage()
        msg["From"] = f'"{self.sender[0]}" <{self.sender[1]}>'
        msg["To"] = ", ".join(f'"{n}" <{a}>' for n, a in self.to)
        if self.cc:
            msg["Cc"] = ", ".join(f'"{n}" <{a}>' for n, a in self.cc)
        msg["Subject"] = self.subject
        msg["Date"] = format_datetime(datetime.fromtimestamp(self.time, timezone.utc))
        msg["Message-ID"] = self.msgid
        msg.set_content(self.body)
        if self.html:
            msg.add_alternative(self.html, subtype="html")
        for name, ctype, data in self.attachments:
            maintype, subtype = ctype.split("/")
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
        return msg.as_bytes()

    def meta(self, mid: str, labels: list[str]) -> dict:
        def person(p):
            return {"Name": p[0], "Address": p[1]}
        return {"Version": 1, "Payload": {
            "ID": mid, "LabelIDs": labels, "Subject": self.subject, "Sender": person(self.sender),
            "ToList": [person(p) for p in self.to], "CCList": [person(p) for p in self.cc], "Time": self.time,
            "Unread": int(self.unread), "NumAttachments": len(self.attachments),
        }}


def write_mail(mail_dir: str, mail: Mail, labels: list[str]) -> None:
    mid = mail.key
    with open(os.path.join(mail_dir, mid + ".eml"), "wb") as f:
        f.write(mail.eml())
    with open(os.path.join(mail_dir, mid + ".metadata.json"), "w", encoding="utf-8") as f:
        json.dump(mail.meta(mid, labels), f, ensure_ascii=False)


def write_labels(mail_dir: str, labels: list[dict]) -> None:
    with open(os.path.join(mail_dir, "labels.json"), "w", encoding="utf-8") as f:
        json.dump({"Version": 1, "Payload": labels}, f, ensure_ascii=False)


JAMIE = ("Jamie Chen", "jamie.chen@example.org")
PRIYA = ("Priya Nair", "priya@example.net")
MUM = ("Ruth Morgan", "ruth.morgan@example.net")
SAM = ("Sam Okafor", "sam.okafor@example.org")
CASA = ("Casa do Rio", "bookings@casadorio.example")
ENERGY = ("Greenfield Energy", "billing@greenfield-energy.example")
FERN = ("The Weekly Fern", "hello@weeklyfern.example")
LIBRARY_ = ("Riverside Library", "noreply@riverside-library.example")
CYCLES = ("Spoke & Chain Cycles", "workshop@spokeandchain.example")
TAX = ("Hannah Weiss", "h.weiss@weiss-tax.example")
AIR = ("SkyLark Air", "itinerary@skylark-air.example")
LANDLORD = ("Daniel Price", "daniel.price@example.com")
BAND = ("Leo Martins", "leo.martins@example.org")
NINA = ("Nina Kowalski", "nina.kowalski@northwind.example")
OMAR = ("Omar Haddad", "omar.haddad@northwind.example")
TEAM = ("Product team", "product@northwind.example")
HR = ("People & Culture", "people@northwind.example")

BOOKING_HTML = """\
<div style="font-family:Georgia,serif;max-width:560px;margin:0 auto;color:#2b2b2b">
  <div style="background:#1f4e5f;color:#fff;padding:28px 32px;border-radius:10px 10px 0 0">
    <div style="font-size:13px;letter-spacing:2px;text-transform:uppercase;opacity:.8">Casa do Rio · Lisbon</div>
    <div style="font-size:28px;margin-top:6px">Your stay is confirmed</div>
  </div>
  <div style="border:1px solid #e3e3e3;border-top:none;padding:24px 32px;border-radius:0 0 10px 10px;font-family:Helvetica,Arial,sans-serif;font-size:15px;line-height:1.55">
    <p>Olá Alex,</p>
    <p>thank you for booking with us. We are looking forward to welcoming you in Alfama.</p>
    <table style="width:100%;border-collapse:collapse;margin:18px 0;font-size:14px">
      <tr><td style="padding:8px 0;color:#777">Check-in</td><td style="text-align:right">Thu, 5 Nov 2026 · from 15:00</td></tr>
      <tr><td style="padding:8px 0;color:#777;border-top:1px solid #eee">Check-out</td><td style="text-align:right;border-top:1px solid #eee">Mon, 9 Nov 2026 · until 11:00</td></tr>
      <tr><td style="padding:8px 0;color:#777;border-top:1px solid #eee">Room</td><td style="text-align:right;border-top:1px solid #eee">Double room with river view</td></tr>
      <tr><td style="padding:8px 0;color:#777;border-top:1px solid #eee">Guests</td><td style="text-align:right;border-top:1px solid #eee">2 adults</td></tr>
      <tr><td style="padding:8px 0;border-top:1px solid #eee"><b>Total</b></td><td style="text-align:right;border-top:1px solid #eee"><b>€ 512.00</b></td></tr>
    </table>
    <p>The tram 28 stops two minutes from our door. If you arrive late, the key box code will be sent the evening before.</p>
    <p style="margin-top:24px">Até breve,<br>Marta &amp; the Casa do Rio team</p>
  </div>
</div>
"""

FERN_HTML = """\
<div style="font-family:Helvetica,Arial,sans-serif;max-width:560px;margin:0 auto;color:#26332b">
  <div style="text-align:center;padding:20px 0 6px;font-family:Georgia,serif;font-size:30px;color:#2f6b3f">The Weekly Fern</div>
  <div style="text-align:center;font-size:12px;letter-spacing:1px;color:#7a8a7f;text-transform:uppercase">Issue 118 · Autumn planting</div>
  <h2 style="font-family:Georgia,serif;font-weight:normal;margin-top:28px">Five bulbs to plant before the first frost</h2>
  <p style="line-height:1.6">October is the month to think about spring. Tulips, crocuses and snowdrops all want cold soil
  to wake up properly in March. Plant them three times as deep as the bulb is tall, pointy end up.</p>
  <ol style="line-height:1.8">
    <li><b>Snowdrops</b>, the first sign that winter is losing</li>
    <li><b>Crocus</b>, good in lawns and loved by early bees</li>
    <li><b>Daffodils</b>, squirrels leave them alone</li>
    <li><b>Alliums</b>, tall purple globes for early summer</li>
    <li><b>Tulips</b>, plant them last, in November</li>
  </ol>
  <p style="background:#eef4ee;padding:14px 18px;border-radius:8px;line-height:1.5">Tip of the week: leave the fallen leaves under
  your hedges. Hedgehogs will thank you.</p>
</div>
"""


def personal_mails() -> list[tuple[Mail, list[str]]]:
    photos = [("alfama.png", "image/png", harbour(1200, 800)), ("sunset-cascais.png", "image/png", sunset())]
    invoice = pdf("Greenfield Energy - Invoice 2026-10", [
        "Invoice no. GE-2026-104732", "Customer: Alex Morgan, 14 Elm Row", "",
        "**Period 1 Sep 2026 - 30 Sep 2026", "Electricity, 212 kWh\t€ 61.48", "Standing charge\t€ 9.30",
        "Green tariff bonus\t€ -4.00", "", "**Total due 15 Oct 2026\t€ 66.78", "",
        "Paid by direct debit. Thank you for choosing renewable energy."])
    tax = pdf("Weiss Tax Advisory - Checklist 2025", [
        "**Documents we still need", "Annual salary statement (P60)", "Bank interest certificates",
        "Receipts for home office equipment", "Donation receipts", "",
        "**Deadline", "Please send everything by 31 October 2026."])
    mails = [
        (Mail("p01", "Your stay is confirmed: 5–9 Nov, Casa do Rio", CASA, [ME], "2026-10-17T09:12:00",
              "Your stay at Casa do Rio is confirmed. Check-in Thu 5 Nov from 15:00.", html=BOOKING_HTML,
              unread=True), [INBOX, ALL_MAIL]),
        (Mail("p02", "Photos from Saturday", JAMIE, [ME], "2026-10-16T19:40:00",
              "Hi Alex,\n\nhere are the two shots from Saturday I promised. The light at Cascais was unreal.\n"
              "Can't wait for Lisbon round two!\n\nJamie", attachments=photos), [INBOX, ALL_MAIL]),
        (Mail("p03", "Invoice October 2026", ENERGY, [ME], "2026-10-15T07:02:00",
              "Dear Alex Morgan,\n\nyour invoice for September is attached. The amount of € 66.78 will be "
              "collected by direct debit on 15 October.\n\nGreenfield Energy", attachments=[("invoice-2026-10.pdf",
                                                                                         "application/pdf", invoice)]),
         [INBOX, ALL_MAIL, "F1", "F2"]),
        (Mail("p04", "Re: Dinner on Friday?", PRIYA, [ME], "2026-10-15T18:21:00",
              "Friday works! I'll bring dessert. 7:30 at yours?\n\nPriya\n\n> Are you free on Friday? "
              "I'm trying that lentil curry again."), [INBOX, ALL_MAIL]),
        (Mail("p05", "Five bulbs to plant before the first frost", FERN, [ME], "2026-10-14T06:30:00",
              "The Weekly Fern, issue 118.", html=FERN_HTML, unread=True), [INBOX, ALL_MAIL, "F3"]),
        (Mail("p06", "Your flight to Lisbon: booking LX7Q2M", AIR, [ME], "2026-10-12T11:05:00",
              "Booking reference LX7Q2M\n\nOutbound  Thu 5 Nov  08:40  Bristol → Lisbon\n"
              "Return    Mon 9 Nov  17:15  Lisbon → Bristol\n\nOnline check-in opens 48 hours before departure."),
         [INBOX, ALL_MAIL]),
        (Mail("p07", "Tax return 2025: what we still need", TAX, [ME], "2026-10-10T14:48:00",
              "Dear Alex,\n\nto finish your return we still need a few documents. The checklist is attached.\n"
              "Please send them by 31 October.\n\nKind regards\nHannah Weiss", unread=True,
              attachments=[("checklist-2025.pdf", "application/pdf", tax)]), [INBOX, ALL_MAIL]),
        (Mail("p08", "Your bike is ready for pick-up", CYCLES, [ME], "2026-10-09T16:10:00",
              "Hi Alex,\n\nnew chain, brake pads front and rear, gears adjusted. Total £ 74.\n"
              "We're open until 6pm.\n\nSpoke & Chain"), [INBOX, ALL_MAIL]),
        (Mail("p09", "Re: Setlist for the 24th", BAND, [ME], "2026-10-08T22:03:00",
              "Let's open with 'Harbour Lights' and keep the slow one for the encore.\nRehearsal Tuesday 8pm?\n\nLeo"),
         [INBOX, ALL_MAIL]),
        (Mail("p10", "Two books are due next week", LIBRARY_, [ME], "2026-10-07T08:00:00",
              "'The Overstory' and 'Braiding Sweetgrass' are due on 14 October. You can renew them online."),
         [INBOX, ALL_MAIL]),
        (Mail("p11", "Sunday lunch", MUM, [ME], "2026-10-05T10:31:00",
              "Hello love,\n\nDad is making his roast on Sunday. Bring Jamie if they're free.\n"
              "And don't forget the cake tin you borrowed!\n\nMum x"), [INBOX, ALL_MAIL]),
        (Mail("p12", "Flat inspection on 20 October", LANDLORD, [ME], "2026-10-03T13:15:00",
              "Hi Alex,\n\njust a reminder that the yearly boiler check is on 20 October between 9 and 12.\n\n"
              "Thanks, Daniel"), [INBOX, ALL_MAIL, "F1"]),
        (Mail("p13", "Lisbon ideas", ME, [JAMIE], "2026-10-02T21:44:00",
              "Things I want to do:\n- LX Factory on Sunday\n- Pastéis in Belém (early, before the queue)\n"
              "- Tram 28 at least once\n- Sunset at Miradouro da Senhora do Monte"), [SENT, ALL_MAIL]),
        (Mail("p14", "Invoice September 2026", ENERGY, [ME], "2026-09-15T07:01:00",
              "Your invoice for August is attached.", attachments=[("invoice-2026-09.pdf", "application/pdf",
                                                                   pdf("Greenfield Energy - Invoice 2026-09",
                                                                       ["Total due\t€ 58.12"]))]),
         [ALL_MAIL, ARCHIVE_LABEL, "F1", "F2"]),
        (Mail("p15", "Welcome to the community garden!", ("Elm Row Garden", "garden@elmrow.example"), [ME],
              "2026-09-02T09:00:00", "You've got plot 12. Keys are in the shed, code 4471."),
         [ALL_MAIL, ARCHIVE_LABEL]),
    ]
    return mails


def work_mails() -> list[tuple[Mail, list[str]]]:
    return [
        (Mail("w01", "Q4 planning: agenda for Monday", NINA, [WORK_ME], "2026-10-16T15:30:00",
              "Hi all,\n\n1. Roadmap review\n2. Hiring\n3. Offsite dates\n\nPlease add your topics to the doc.\n\nNina",
              cc=[OMAR], unread=True), [INBOX, ALL_MAIL]),
        (Mail("w02", "Design review notes", OMAR, [WORK_ME], "2026-10-14T17:12:00",
              "Notes from today: we keep the sidebar, drop the second toolbar and test the new list density."),
         [INBOX, ALL_MAIL]),
        (Mail("w03", "Release 2.4 checklist", TEAM, [WORK_ME], "2026-10-13T09:00:00",
              "Freeze on Wednesday, release on Friday."), [INBOX, ALL_MAIL]),
        (Mail("w04", "Your leave request was approved", HR, [WORK_ME], "2026-10-06T11:00:00",
              "5–9 November 2026 approved. Enjoy Lisbon!"), [INBOX, ALL_MAIL]),
    ]


def outlook_mails() -> list[tuple[Mail, list[str]]]:
    return [
        (Mail("o01", "Welcome to the team!", ("Grace Lee", "grace.lee@harbourline.example"), [ME],
              "2015-03-02T08:30:00", "Welcome aboard, Alex. Your desk is next to the window."), [INBOX, ALL_MAIL]),
        (Mail("o02", "Holiday photos 2014", JAMIE, [ME], "2015-01-04T12:00:00",
              "Finally sorted the photos from the coast.", attachments=[("coast.png", "image/png",
                                                                        sunset(seed=-0.2))]), [INBOX, ALL_MAIL]),
        (Mail("o03", "Re: Apartment on Elm Row", LANDLORD, [ME], "2014-11-20T18:05:00",
              "Happy to confirm: the flat is yours from 1 December."), [INBOX, ALL_MAIL]),
        (Mail("o04", "Christmas plans", MUM, [ME], "2014-12-01T09:45:00",
              "Are you coming home on the 23rd or the 24th?"), [INBOX, ALL_MAIL]),
    ]


def archive_mails() -> list[tuple[Mail, str]]:
    """Older mails that only live in the archive, with the folder path they belong to."""
    return [
        (Mail("a01", "Tenancy agreement", LANDLORD, [ME], "2019-11-28T10:00:00",
              "Signed copy attached.", attachments=[("tenancy.pdf", "application/pdf",
                                                    pdf("Tenancy agreement", ["14 Elm Row, Flat 3"]))]),
         "Home/Flat"),
        (Mail("a02", "Home insurance renewal", ("Oakleaf Insurance", "renewals@oakleaf.example"), [ME],
              "2025-12-01T08:00:00", "Your policy renews on 1 January."), "Home/Insurance"),
        (Mail("a03", "Your tax assessment 2024", TAX, [ME], "2025-09-18T12:00:00",
              "The assessment matches our calculation. Refund expected in October."), "Taxes/2024"),
        (Mail("a04", "Porto, here we come", JAMIE, [ME], "2024-05-03T19:20:00",
              "Hi Alex,\n\ntrain tickets are booked: Lisbon Santa Apolónia 09:39, arriving in Porto at 12:28.\n"
              "Seats 41 and 42, coach 3.\n\nPlan for the weekend:\n- Livraria Lello early on Saturday\n"
              "- port tasting in Gaia\n- francesinha at the place Leo recommended\n\nSo excited!\nJamie"),
         "Travel/2024 Porto"),
        (Mail("a05", "Hotel confirmation Porto", ("Rua Hotel", "stay@ruahotel.example"), [ME],
              "2024-04-20T10:00:00", "See you in May."), "Travel/2024 Porto"),
    ]


def build(out: str) -> str:
    os.makedirs(out)
    personal = os.path.join(out, "accounts", "alex@example.com", "mail_20261017T091500")
    work = os.path.join(out, "exports", "northwind")
    lib_root = os.path.join(out, "library")
    for d in (personal, work):
        os.makedirs(d)
        write_labels(d, PROTON_LABELS)

    old_ones = outlook_mails()
    personal_list = personal_mails()
    for mail, labels in personal_list:
        write_mail(personal, mail, labels)
    for mail, labels in old_ones[:2]:  # also imported into Proton years ago
        write_mail(personal, mail, [ARCHIVE_LABEL, ALL_MAIL])
    for mail, labels in work_mails():
        write_mail(work, mail, labels)

    lib = Library.create(lib_root)
    s_personal = lib.add_source(os.path.dirname(personal), "Personal")
    s_work = lib.add_source(work, "Northwind")
    lib.update_source(s_work.id, icon={"type": "emoji", "value": "💼"})

    outlook = os.path.join(lib_root, "sources", "outlook-2015")
    os.makedirs(outlook)
    write_labels(outlook, [x for x in PROTON_LABELS if x["ID"] in ("0", "7", "3", "5")])
    for mail, labels in old_ones:
        write_mail(outlook, mail, labels)
    lib.add_source(outlook, "Outlook 2015", kind="outlook")

    arch = os.path.join(lib_root, "sources", "family-archive")
    os.makedirs(arch)
    archive.write_empty_archive(arch)
    folders: dict[str, str] = {}
    for path in ("Home", "Home/Flat", "Home/Insurance", "Home/Bills", "Taxes", "Taxes/2024", "Travel",
                 "Travel/2024 Porto", "Travel/2026 Lisbon", "Unsorted"):
        parent, _, name = path.rpartition("/")
        folders[path] = archive.create_folder(arch, name, folders.get(parent))["ID"]
    for mail, path in archive_mails():
        write_mail(arch, mail, [ALL_MAIL, folders[path]])
    # copies of current mails, so they show up as "in archive" with a second source
    by_key = {m.key: m for m, _ in personal_list}
    for key, path in (("p14", "Home/Bills"), ("p06", "Travel/2026 Lisbon"), ("p15", "Home")):
        write_mail(arch, by_key[key], [ALL_MAIL, folders[path]])
    s_archive = lib.add_source(arch, "Family archive", kind="archive")
    lib.update_source(s_archive.id, icon={"type": "emoji", "value": "🗄️"})
    lib.update_source(s_personal.id, archive_target=s_archive.id)

    tags = TagStore(os.path.join(lib_root, TAGS_DB))
    travel = tags.create_tag("Travel", "#0a66c2")["id"]
    finance = tags.create_tag("Finance", "#2e7d32")["id"]
    family = tags.create_tag("Family", "#7b3fb0")["id"]
    follow = tags.create_tag("Follow up", "#b35c00")["id"]
    marks = {
        "p01": (True, [travel]), "p02": (False, [travel, family]), "p03": (False, [finance]),
        "p06": (True, [travel]), "p07": (True, [finance, follow]), "p11": (False, [family]),
        "p12": (False, [follow]), "p13": (False, [travel]), "p14": (False, [finance]),
        "w01": (False, [follow]), "a04": (False, [travel]), "a05": (False, [travel]),
    }
    all_mails = {m.key: m for m, _ in [*personal_list, *work_mails(), *old_ones]}
    all_mails.update({m.key: m for m, _ in archive_mails()})
    for key, (starred, tag_ids) in marks.items():
        tags.set_marks("mid:" + all_mails[key].msgid.strip("<>").lower(), starred, tag_ids)
    tags.close()
    return lib_root


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(build(sys.argv[1]))
