"""Generate synthetic trade documents for the demo.

Produces three documents in sample_docs/:
  1. commercial_invoice_clean.pdf  - well-formatted Commercial Invoice (all fields present, matches Customer X rules)
  2. bill_of_lading_clean.pdf      - matching Bill of Lading for the same shipment
  3. commercial_invoice_messy.png  - simulated low-quality scan: skewed, noisy, low-res,
                                     smudged HS code, missing Incoterms, wrong discharge port

Run: .venv/bin/python scripts/generate_sample_docs.py
"""

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parent.parent / "sample_docs"
OUT.mkdir(exist_ok=True)


def _header(c, title, doc_no):
    w, h = A4
    c.setFont("Helvetica-Bold", 16)
    c.drawString(20 * mm, h - 20 * mm, "ORIENT EXPORTS PVT. LTD.")
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, h - 25 * mm, "Plot 14, MIDC Industrial Area, Navi Mumbai 400710, India")
    c.setFont("Helvetica-Bold", 13)
    c.drawRightString(w - 20 * mm, h - 20 * mm, title)
    c.setFont("Helvetica", 10)
    c.drawRightString(w - 20 * mm, h - 26 * mm, doc_no)
    c.setStrokeColor(colors.black)
    c.line(20 * mm, h - 30 * mm, w - 20 * mm, h - 30 * mm)


def _rows(c, rows, top_mm):
    w, h = A4
    y = h - top_mm * mm
    for label, value in rows:
        c.setFont("Helvetica-Bold", 9)
        c.drawString(20 * mm, y, label)
        c.setFont("Helvetica", 10)
        c.drawString(75 * mm, y, value)
        y -= 8 * mm
    return y


def clean_invoice():
    path = OUT / "commercial_invoice_clean.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    _header(c, "COMMERCIAL INVOICE", "Invoice No: INV-2024-0817")
    y = _rows(c, [
        ("Invoice Date:", "12 August 2024"),
        ("Exporter / Shipper:", "Orient Exports Pvt. Ltd., Navi Mumbai, India"),
        ("Consignee:", "Meridian Trading GmbH"),
        ("Consignee Address:", "Speicherstadt 22, 20457 Hamburg, Germany"),
        ("Port of Loading:", "Nhava Sheva (JNPT), India — INNSA"),
        ("Port of Discharge:", "Hamburg, Germany — DEHAM"),
        ("Incoterms (2020):", "CIF Hamburg"),
        ("HS Code:", "8471.30.00"),
        ("Description of Goods:", "Portable automatic data processing machines"),
        ("", "(laptop computers, model OX-14), 400 units"),
        ("Unit Price:", "USD 385.00"),
        ("Total Invoice Value:", "USD 154,000.00"),
        ("Gross Weight:", "1,240.00 KG"),
        ("Net Weight:", "1,088.00 KG"),
        ("Country of Origin:", "India"),
        ("Payment Terms:", "Irrevocable Letter of Credit at sight"),
    ], 42)
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(20 * mm, y - 6 * mm,
                 "We certify this invoice is true and correct. Orient Exports Pvt. Ltd. — Authorised Signatory")
    c.save()
    return path


def clean_bl():
    path = OUT / "bill_of_lading_clean.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    _header(c, "BILL OF LADING", "B/L No: MAEU-2249-1183")
    _rows(c, [
        ("Shipper:", "Orient Exports Pvt. Ltd., Navi Mumbai, India"),
        ("Consignee:", "Meridian Trading GmbH, Hamburg, Germany"),
        ("Notify Party:", "Same as consignee"),
        ("Vessel / Voyage:", "MV Cornelia Maersk / 433W"),
        ("Port of Loading:", "Nhava Sheva (JNPT), India — INNSA"),
        ("Port of Discharge:", "Hamburg, Germany — DEHAM"),
        ("Incoterms:", "CIF Hamburg"),
        ("HS Code:", "8471.30.00"),
        ("Description of Goods:", "400 cartons — portable automatic data"),
        ("", "processing machines (laptop computers)"),
        ("Container No:", "MSKU-884231-7 (1 x 40ft HC)"),
        ("Gross Weight:", "1,240.00 KG"),
        ("Freight:", "PREPAID"),
        ("Invoice Reference:", "INV-2024-0817"),
        ("Place & Date of Issue:", "Mumbai, 14 August 2024"),
        ("No. of Original B/Ls:", "THREE (3)"),
    ], 42)
    c.save()
    return path


def _font(size):
    for name in ("/System/Library/Fonts/Supplemental/Courier New.ttf",
                 "/System/Library/Fonts/Monaco.ttf",
                 "/System/Library/Fonts/Supplemental/Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def messy_invoice():
    """Draw a typewriter-style invoice, then degrade it like a bad scan.

    Deliberate defects vs. the Customer X rule set:
      - HS code digits smudged out           -> extractor should return null/low confidence
      - Incoterms line entirely absent       -> required field missing
      - Port of discharge is Rotterdam       -> hard mismatch (rules expect Hamburg/DEHAM)
      - Consignee has a typo ("GmBH")        -> near-match, tests fuzzy handling
    """
    random.seed(7)
    w, h = 1240, 1650
    img = Image.new("L", (w, h), 245)
    d = ImageDraw.Draw(img)
    big, body = _font(34), _font(24)

    d.text((70, 60), "ORIENT EXPORTS PVT LTD", font=big, fill=20)
    d.text((70, 105), "COMMERCIAL INVOICE", font=big, fill=30)
    d.line((70, 150, w - 70, 150), fill=60, width=3)

    lines = [
        "INVOICE NO   : INV-2024-0912",
        "DATE         : 02 SEPT 2024",
        "CONSIGNEE    : MERIDIAN TRADING GmBH",
        "               SPEICHERSTADT 22, HAMBURG",
        "PORT LOADING : NHAVA SHEVA (INNSA), INDIA",
        "PORT DISCH.  : ROTTERDAM (NLRTM), NETHERLANDS",
        "HS CODE      : 84**.30",          # asterisks stand in for smudge; also physically smudged below
        "GOODS        : PORTABLE DATA PROCESSING MACHINES",
        "               (LAPTOP COMPUTERS) 380 UNITS",
        "GROSS WEIGHT : 1,178.00 KG",
        "TOTAL VALUE  : USD 146,300.00",
        "ORIGIN       : INDIA",
    ]
    y = 200
    for line in lines:
        d.text((70, y), line, font=body, fill=random.randint(25, 60))
        y += 58

    # physical smudge over the HS code digits
    d.ellipse((360, 540, 560, 590), fill=170)
    d.ellipse((380, 548, 530, 585), fill=140)

    # coffee-ring stain
    d.ellipse((880, 1150, 1100, 1370), outline=190, width=18)

    # speckle noise
    px = img.load()
    for _ in range(14000):
        x, yy = random.randrange(w), random.randrange(h)
        px[x, yy] = random.choice((90, 120, 200, 230))

    # skew, blur, downsample to fake a low-res scan
    img = img.rotate(1.6, expand=True, fillcolor=235)
    img = img.filter(ImageFilter.GaussianBlur(0.9))
    img = img.resize((int(img.width * 0.55), int(img.height * 0.55)))

    path = OUT / "commercial_invoice_messy.png"
    img.convert("RGB").save(path)
    return path


if __name__ == "__main__":
    for p in (clean_invoice(), clean_bl(), messy_invoice()):
        print("wrote", p)
