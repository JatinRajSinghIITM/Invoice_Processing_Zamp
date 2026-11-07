"""
Generates all synthetic test invoices used for the happy path and edge cases.
Run directly: python sample_invoices/generate_samples.py

Two kinds of PDF are produced:
  - "digital" invoices: plain text drawn with reportlab -> pdfplumber reads
    a real text layer, exercising the deterministic regex-extraction path.
  - "scanned" invoices: the same content rendered as a noisy raster image
    with NO text layer -> forces the OCR fallback path (edge case EC-3).
"""
import os
import random
from pathlib import Path

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from PIL import Image, ImageDraw, ImageFont, ImageFilter

OUT_DIR = Path(__file__).parent


def _draw_invoice_lines(vendor_name, invoice_number, invoice_date, po_number,
                         line_items, subtotal, tax, total, tax_label="Tax (18% GST)",
                         omit_invoice_number=False, omit_po=False, omit_date=False):
    lines = [vendor_name, "123 Industrial Area, Chennai, TN 600032", "", "INVOICE", ""]
    if not omit_invoice_number:
        lines.append(f"Invoice Number: {invoice_number}")
    if not omit_date:
        lines.append(f"Invoice Date: {invoice_date}")
    if not omit_po:
        lines.append(f"PO Number: {po_number}")
    lines += ["", "Bill To: Acme Corp, Accounts Payable Dept.", "", "Line Items:"]
    for desc, amt in line_items:
        lines.append(f"  {desc:<40} Rs. {amt:,.2f}")
    lines += ["", f"Subtotal: Rs. {subtotal:,.2f}", f"{tax_label}: Rs. {tax:,.2f}", f"Total: Rs. {total:,.2f}",
              "", "Thank you for your business."]
    return lines


def make_digital_pdf(path, vendor_name, invoice_number, invoice_date, po_number,
                      line_items, subtotal, tax, total, **kwargs):
    lines = _draw_invoice_lines(vendor_name, invoice_number, invoice_date, po_number,
                                 line_items, subtotal, tax, total, **kwargs)
    c = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4
    c.setFont("Helvetica", 11)
    y = height - 60
    for line in lines:
        c.drawString(50, y, line)
        y -= 18
    c.save()


def make_scanned_pdf(path, vendor_name, invoice_number, invoice_date, po_number,
                      line_items, subtotal, tax, total, **kwargs):
    """Renders the invoice as a degraded raster image with no text layer,
    forcing the pipeline's OCR fallback."""
    lines = _draw_invoice_lines(vendor_name, invoice_number, invoice_date, po_number,
                                 line_items, subtotal, tax, total, **kwargs)
    img = Image.new("L", (1240, 1754), color=255)  # A4 at ~150dpi, grayscale
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22)
    except Exception:
        font = ImageFont.load_default()

    y = 80
    for line in lines:
        draw.text((70, y), line, fill=0, font=font)
        y += 34

    # Degrade: slight blur + sparse noise + mild rotation to simulate a
    # phone-scanned document — enough to force the OCR path and lower
    # confidence, but still legible (same as a real "slightly poor" scan).
    img = img.rotate(0.6, expand=False, fillcolor=255)
    img = img.filter(ImageFilter.GaussianBlur(radius=0.5))
    pixels = img.load()
    random.seed(42)
    for _ in range(6000):
        x = random.randint(0, img.width - 1)
        yx = random.randint(0, img.height - 1)
        pixels[x, yx] = random.randint(0, 120)

    img = img.convert("RGB")
    tmp_png = str(path).replace(".pdf", "_tmp.png")
    img.save(tmp_png)

    c = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4
    c.drawImage(tmp_png, 0, 0, width=width, height=height)
    c.save()
    os.remove(tmp_png)


def make_unusual_layout_pdf(path):
    """A digital invoice whose labels the pattern parser does not know
    ("Document Ref.", "Dated", "Please remit"). Text is perfectly readable to a
    person (or a language model) but invisible to the regexes, so it is the
    sample that shows what the optional AI step is for."""
    lines = [
        "Orbit Telecom Solutions", "Plot 14, SEEPZ, Hyderabad, TS 500081", "",
        "TAX INVOICE", "",
        "Document Ref.: TI/2026/0417",
        "Dated: 14 October 2026",
        "Against Purchase Order: PO-5007", "",
        "Billed to: Acme Corp, Accounts Payable Dept.", "",
        "Leased line rental - Q4 2026", "Taxable value        Rs 15,889.83", "GST @ 18%             Rs 2,860.17", "",
        "Please remit Rs 18,750.00 within 30 days.",
    ]
    c = canvas.Canvas(str(path), pagesize=A4)
    _, height = A4
    c.setFont("Helvetica", 11)
    y = height - 60
    for line in lines:
        c.drawString(50, y, line)
        y -= 18
    c.save()


def generate_all():
    OUT_DIR.mkdir(exist_ok=True)

    # --- Happy path: Nimbus, exact PO match, approved vendor, clean digital PDF ---
    make_digital_pdf(
        OUT_DIR / "01_happy_path_nimbus.pdf",
        vendor_name="Nimbus Office Supplies Pvt Ltd",
        invoice_number="INV-2026-0931", invoice_date="2026-10-05", po_number="PO-5001",
        line_items=[("Ergonomic Office Chairs x 10", 32000.00), ("Desk Organizer Sets x 25", 9101.69)],
        subtotal=41101.69, tax=7398.31, total=48500.00,
    )

    # --- EC-1: Duplicate invoice (submit this file twice during the demo) ---
    make_digital_pdf(
        OUT_DIR / "02_edge_duplicate_coral.pdf",
        vendor_name="Coral Facilities Management",
        invoice_number="INV-CFM-4471", invoice_date="2026-10-10", po_number="PO-5005",
        line_items=[("Monthly Facilities Maintenance - Oct 2026", 54000.00)],
        subtotal=45762.71, tax=8237.29, total=54000.00,
    )

    # --- EC-2: Amount differs from PO beyond soft tolerance (moderate, ~8% over
    # remaining balance -> lands in Needs Review, not an auto-reject) ---
    make_digital_pdf(
        OUT_DIR / "03_edge_amount_mismatch_vertex.pdf",
        vendor_name="Vertex Logistics & Freight",
        invoice_number="INV-VLF-7782", invoice_date="2026-10-12", po_number="PO-5003",
        line_items=[("Freight & Handling - Sep batch", 70000.00), ("Fuel surcharge", 12300.00)],
        subtotal=69745.76, tax=12554.24, total=82300.00,  # PO-5003 is 76200 -> ~8% over
    )

    # --- EC-3: Scanned / poor-quality document -> forces OCR fallback ---
    make_scanned_pdf(
        OUT_DIR / "04_edge_scanned_apex.pdf",
        vendor_name="Apex Print & Packaging Co",
        invoice_number="INV-APX-2290", invoice_date="2026-10-14", po_number="PO-5004",
        line_items=[("Custom Packaging Boxes - 5000 units", 27118.64)],
        subtotal=27118.64, tax=4881.36, total=32000.00,
    )

    # --- EC-4: Vendor not in approved database at all ---
    make_digital_pdf(
        OUT_DIR / "05_edge_unknown_vendor.pdf",
        vendor_name="Falcon Hardware Traders",
        invoice_number="INV-FHT-1150", invoice_date="2026-10-15", po_number="PO-9999",
        line_items=[("Assorted Hardware Supplies", 15000.00)],
        subtotal=12711.86, tax=2288.14, total=15000.00,
    )

    # --- Bonus EC-5: PO split across two invoices (BrightPath, PO-5002 = 125000) ---
    make_digital_pdf(
        OUT_DIR / "06_bonus_split_po_part1_brightpath.pdf",
        vendor_name="BrightPath IT Services LLP",
        invoice_number="INV-BPI-3301", invoice_date="2026-10-08", po_number="PO-5002",
        line_items=[("Managed IT Support - Phase 1", 63559.32)],
        subtotal=63559.32, tax=11440.68, total=75000.00,
    )
    make_digital_pdf(
        OUT_DIR / "06_bonus_split_po_part2_brightpath.pdf",
        vendor_name="BrightPath IT Services LLP",
        invoice_number="INV-BPI-3302", invoice_date="2026-10-20", po_number="PO-5002",
        line_items=[("Managed IT Support - Phase 2", 42372.88)],
        subtotal=42372.88, tax=7627.12, total=50000.00,  # 75000 + 50000 = 125000, exact PO total
    )

    # --- Bonus EC-6: Inactive / not-approved vendor -> hard Rejected ---
    make_digital_pdf(
        OUT_DIR / "07_bonus_inactive_vendor_meridian.pdf",
        vendor_name="Meridian Catering Services",
        invoice_number="INV-MCS-5502", invoice_date="2026-10-16", po_number="PO-5010",
        line_items=[("Corporate Event Catering", 22000.00)],
        subtotal=18644.07, tax=3355.93, total=22000.00,
    )

    # --- Bonus EC-7: Missing invoice number AND PO reference.
    # Uses Sterling Consulting Group (PO-5006) specifically because no other
    # sample invoice touches that vendor/PO — this keeps the scenario's
    # outcome deterministic regardless of what order invoices are processed
    # in during a live demo (unlike reusing Apex/PO-5004, which EC-3 also
    # consumes, which would make this case's remaining PO balance depend on
    # run order).
    make_digital_pdf(
        OUT_DIR / "08_bonus_missing_fields_sterling.pdf",
        vendor_name="Sterling Consulting Group",
        invoice_number="", invoice_date="2026-10-18", po_number="",
        line_items=[("Strategy Advisory - October Engagement", 18000.00)],
        subtotal=15254.24, tax=2745.76, total=18000.00,
        omit_invoice_number=True, omit_po=True,
    )

    make_unusual_layout_pdf(OUT_DIR / "09_ai_unusual_layout_orbit.pdf")

    print("Generated sample invoices in", OUT_DIR)
    for f in sorted(OUT_DIR.glob("*.pdf")):
        print(" -", f.name)


if __name__ == "__main__":
    generate_all()
