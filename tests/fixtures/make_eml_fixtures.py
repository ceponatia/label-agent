#!/usr/bin/env python
"""Build the .eml fixtures used by the ingestion tests.

Run with:  .venv/bin/python tests/fixtures/make_eml_fixtures.py

The generated .eml files are committed to the repo. Tests read them directly and
never regenerate them. MIME boundaries are frozen, so re-running this script is
a byte-for-byte no-op unless the fixture content is deliberately changed.
"""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent

POSHMARK_LABEL_PDF = FIXTURES / "poshmark-label.pdf"
VINTED_LABEL_PDF = FIXTURES / "vinted-label.pdf"

# --- known-good values, taken from the real example emails -------------------

POSHMARK_ITEM = "Kate Spade Tinsel Small Dome Crossbody Bag Rose Gold Glitter Sparkle"
POSHMARK_ORDER_ID = "6a6e119205255dd754e8b790"
POSHMARK_TRACKING = "9434650208104113715936"
POSHMARK_SUBJECT = f'"{POSHMARK_ITEM}" just sold to @vchavez75 on Poshmark!'
POSHMARK_ATTACHMENT = "pre-paid mailing label 4x6.pdf"
POSHMARK_DATE = "Sat, 1 Aug 2026 11:32:47 -0400"

VINTED_ITEM = (
    "Wild Fable Fruit Print Cutout Tie Strap One Piece Bathing Suit "
    "US Women’s Medium"
)
VINTED_TRANSACTION = "21272887347"
VINTED_TRACKING = "9434636208303484184646"
VINTED_DEADLINE = "08/10/2026 02:00 AM"
VINTED_SUBJECT = f"{VINTED_ITEM} shipping label – use by {VINTED_DEADLINE}"
VINTED_ATTACHMENT = f"Vinted-Label-{VINTED_TRANSACTION}.pdf"
VINTED_DATE = "Fri, 31 Jul 2026 23:28:35 -0400"

ELAINE = "elaine <elaineamy.g2010@gmail.com>"
BRIAN = '"Brian Grubba" <brian@snarebox.com>'
FORWARD_DATE = "Sat, 1 Aug 2026 13:58:20 -0400"

# --- Poshmark bodies ---------------------------------------------------------

POSHMARK_PLAIN = f"""Hi Elaine! Great news - you just sold "{POSHMARK_ITEM}" on Poshmark.

@vchavez75 accepted your offer and we have processed their payment. Please
package your sale and get it ready for shipment. @vchavez75 can't wait to
receive it. You can find all of the details below.

In order to provide the best customer service, Poshmark expects our sellers to
ship their sales within 2 days.

Happy Poshing!

The Poshmark Team

Buyer
Vanessa Chavez
@vchavez75

Order Date
August 01, 2026

Order ID
{POSHMARK_ORDER_ID}

Tracking Number
{POSHMARK_TRACKING}

{POSHMARK_ITEM}
Size: OS
Price: $42.00

Your Earnings (minus fee and taxes)    $33.60

Shipping Instructions

1. Print the attached prepaid, pre-addressed shipping label - please note you
   must use the Poshmark-provided label.
2. Pack your item securely.
3. Attach the label to your package and drop it off at any USPS location.

This message was sent to gelaine@umich.edu.
Copyright 2026 Poshmark, Inc.
203 Redwood Shores Pkwy, 8th Floor, Redwood City, CA 94065
"""

POSHMARK_HTML = f"""<html><body>
<img src="https://poshmark.com/logo.png" alt="Poshmark">
<a href="https://poshmark.com/order/{POSHMARK_ORDER_ID}">View Order</a>
<h2>Hi Elaine! Great news - you just sold "{POSHMARK_ITEM}" on Poshmark.</h2>
<p>@vchavez75 accepted your offer and we have processed their payment. Please
package your sale and get it ready for shipment.</p>
<p>In order to provide the best customer service, Poshmark expects our sellers
to ship their sales within 2 days.</p>
<p>Happy Poshing!</p>
<p>The Poshmark Team</p>
<p><b>Buyer</b><br>Vanessa Chavez<br>@vchavez75</p>
<p><b>Order Date</b><br>August 01, 2026</p>
<p><b>Order ID</b><br>{POSHMARK_ORDER_ID}<br>
<b>Tracking Number</b><br>
<a href="https://tools.usps.com/go/TrackConfirmAction?tLabels={POSHMARK_TRACKING}">
{POSHMARK_TRACKING}</a></p>
<table><tr><td>{POSHMARK_ITEM}<br>Size: OS<br>Price: $42.00</td></tr></table>
<p><b>Shipping Instructions</b></p>
<ol>
<li>Print the attached prepaid, pre-addressed shipping label.</li>
<li>Pack your item securely.</li>
<li>Attach the label to your package and drop it off at any USPS location.</li>
</ol>
<p>This message was sent to gelaine@umich.edu.</p>
</body></html>
"""

# --- Vinted bodies -----------------------------------------------------------

VINTED_HTML = f"""<html><body>
<h2>Hello elaineg974,</h2>
<p>Your shipping label is attached to this message.</p>
<h3>Shipping information</h3>
<table>
<tr><td><b>Item:</b></td><td>{VINTED_ITEM}</td></tr>
<tr><td><b>Package size:</b></td><td>Under 500.0 g</td></tr>
<tr><td><b>Tracking code:</b></td><td>{VINTED_TRACKING}</td></tr>
<tr><td><b>Shipping deadline:</b></td><td>{VINTED_DEADLINE}</td></tr>
<tr><td><b>Transaction ID:</b></td><td>{VINTED_TRANSACTION}</td></tr>
</table>
<h3>Here are the next steps to complete this transaction:</h3>
<ol>
<li><b>Pack your item(s)</b> Use packaging that's sturdy enough to protect the
item(s) you're sending.</li>
<li><b>Print the label and attach it to your parcel</b> The barcode must be
completely visible.</li>
<li><b>Bring the parcel to the drop-off point</b></li>
<li><b>Track your parcel's journey</b></li>
</ol>
<p><b>IMPORTANT: Ship your parcel before {VINTED_DEADLINE} to avoid the
transaction being cancelled.</b></p>
<p>Team Vinted</p>
</body></html>
"""

# --- forwarded wrappers ------------------------------------------------------

POSHMARK_FORWARD_PLAIN = f"""Sent from my iPhone

Begin forwarded message:

From: Poshmark <orders@poshmark.com>
Date: August 1, 2026 at 11:32:47 AM EDT
To: gelaine@umich.edu
Subject: {POSHMARK_SUBJECT}

{POSHMARK_PLAIN}"""

POSHMARK_FORWARD_HTML = f"""<html><body>
<div>Sent from my iPhone</div>
<div><br></div>
<div>Begin forwarded message:</div>
<div><br></div>
<blockquote>
<div><b>From:</b> Poshmark &lt;orders@poshmark.com&gt;<br>
<b>Date:</b> August 1, 2026 at 11:32:47 AM EDT<br>
<b>To:</b> &lt;gelaine@umich.edu&gt;<br>
<b>Subject:</b> {POSHMARK_SUBJECT}</div>
<div><br></div>
{POSHMARK_HTML}
</blockquote>
</body></html>
"""

VINTED_FORWARD_HTML = f"""<html><body>
<div>Sent from my iPhone</div>
<div><br></div>
<div>Begin forwarded message:</div>
<div><br></div>
<blockquote>
<div><b>From:</b> Team Vinted &lt;no-reply@vinted.com&gt;<br>
<b>Date:</b> July 31, 2026 at 11:28:35 PM EDT<br>
<b>To:</b> &lt;gelaine@umich.edu&gt;<br>
<b>Subject:</b> {VINTED_SUBJECT}<br>
<b>Reply-To:</b> Team Vinted &lt;no-reply@vinted.com&gt;</div>
<div><br></div>
{VINTED_HTML}
</blockquote>
</body></html>
"""

# --- other mail --------------------------------------------------------------

OFFER_PLAIN = f"""Hi Elaine!

@vchavez75 just made an offer of $42.00 on "{POSHMARK_ITEM}".

Offers expire in 24 hours. Respond in the app to accept, decline, or counter.

Happy Poshing!
The Poshmark Team
"""

NEWSLETTER_PLAIN = """Hello,

Your monthly statement is attached as a PDF.

Thanks for being a customer.
The Example Team
"""


def _attach_pdf(msg: EmailMessage, data: bytes, filename: str) -> None:
    msg.add_attachment(data, maintype="application", subtype="pdf", filename=filename)


def _headers(
    msg: EmailMessage,
    *,
    sender: str,
    to: str,
    subject: str,
    date: str,
    message_id: str,
) -> None:
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = date
    msg["Message-ID"] = message_id


def poshmark_direct(label_pdf: bytes) -> EmailMessage:
    msg = EmailMessage()
    _headers(
        msg,
        sender="Poshmark <orders@poshmark.com>",
        to="gelaine@umich.edu",
        subject=POSHMARK_SUBJECT,
        date=POSHMARK_DATE,
        message_id="<posh-direct-1@poshmark.com>",
    )
    msg.set_content(POSHMARK_PLAIN)
    msg.add_alternative(POSHMARK_HTML, subtype="html")
    _attach_pdf(msg, label_pdf, POSHMARK_ATTACHMENT)
    return msg


def vinted_direct(label_pdf: bytes) -> EmailMessage:
    # Vinted sends HTML only - exercises the tag-stripping path.
    msg = EmailMessage()
    _headers(
        msg,
        sender="Team Vinted <no-reply@vinted.com>",
        to="gelaine@umich.edu",
        subject=VINTED_SUBJECT,
        date=VINTED_DATE,
        message_id="<vinted-direct-1@vinted.com>",
    )
    msg["Reply-To"] = "Team Vinted <no-reply@vinted.com>"
    msg.set_content(VINTED_HTML, subtype="html")
    _attach_pdf(msg, label_pdf, VINTED_ATTACHMENT)
    return msg


def poshmark_forwarded(label_pdf: bytes) -> EmailMessage:
    msg = EmailMessage()
    _headers(
        msg,
        sender=ELAINE,
        to=BRIAN,
        subject=f"Fwd: {POSHMARK_SUBJECT}",
        date=FORWARD_DATE,
        message_id="<posh-fwd-1@mail.gmail.com>",
    )
    msg.set_content(POSHMARK_FORWARD_PLAIN)
    msg.add_alternative(POSHMARK_FORWARD_HTML, subtype="html")
    _attach_pdf(msg, label_pdf, POSHMARK_ATTACHMENT)
    return msg


def vinted_forwarded(label_pdf: bytes) -> EmailMessage:
    msg = EmailMessage()
    _headers(
        msg,
        sender=ELAINE,
        to=BRIAN,
        subject=f"Fwd: {VINTED_SUBJECT}",
        date=FORWARD_DATE,
        message_id="<vinted-fwd-1@mail.gmail.com>",
    )
    msg.set_content(VINTED_FORWARD_HTML, subtype="html")
    _attach_pdf(msg, label_pdf, VINTED_ATTACHMENT)
    return msg


def not_a_label() -> EmailMessage:
    msg = EmailMessage()
    _headers(
        msg,
        sender="Poshmark <orders@poshmark.com>",
        to="gelaine@umich.edu",
        subject=f'You have an offer on "{POSHMARK_ITEM}"!',
        date="Sat, 1 Aug 2026 10:04:12 -0400",
        message_id="<posh-offer-1@poshmark.com>",
    )
    msg.set_content(OFFER_PLAIN)
    return msg


def unrelated(pdf: bytes) -> EmailMessage:
    msg = EmailMessage()
    _headers(
        msg,
        sender="Example Newsletter <newsletter@example.com>",
        to="elaineamy.g2010@gmail.com",
        subject="Your monthly statement is attached",
        date="Sat, 1 Aug 2026 06:00:00 -0400",
        message_id="<newsletter-1@example.com>",
    )
    msg.set_content(NEWSLETTER_PLAIN)
    _attach_pdf(msg, pdf, "statement.pdf")
    return msg


def _freeze_boundaries(msg: EmailMessage) -> None:
    """Replace random MIME boundaries so regenerating is reproducible."""
    for index, part in enumerate(p for p in msg.walk() if p.is_multipart()):
        part.set_boundary(f"----=_LabelAgentFixture_{index}")


def main() -> None:
    poshmark_pdf = POSHMARK_LABEL_PDF.read_bytes()
    vinted_pdf = VINTED_LABEL_PDF.read_bytes()

    files = {
        "poshmark-direct.eml": poshmark_direct(poshmark_pdf),
        "vinted-direct.eml": vinted_direct(vinted_pdf),
        "poshmark-forwarded.eml": poshmark_forwarded(poshmark_pdf),
        "vinted-forwarded.eml": vinted_forwarded(vinted_pdf),
        "not-a-label.eml": not_a_label(),
        "unrelated.eml": unrelated(poshmark_pdf),
    }
    for name, msg in files.items():
        _freeze_boundaries(msg)
        path = FIXTURES / name
        path.write_bytes(bytes(msg))
        print(f"wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
