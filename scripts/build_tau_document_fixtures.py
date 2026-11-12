"""Reproduce synthetic input fixtures and their manifest without touching data/tau/.

Expected extraction assertions are maintained separately in the repository.
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["reportlab==4.4.1", "pymupdf==1.26.0", "pillow==11.2.1"]
# ///

import hashlib
import io
import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.platypus import Table, TableStyle

OUTPUT = Path(__file__).resolve().parents[1] / "data" / "tau-documents"
TITLE = "Synthetic Returns Policy"
BANNER = "SYNTHETIC DOCUMENT PARSER FIXTURE"
RULE_HEADING = "Return Credit"
CONTINUATION_HEADING = "Return Credit (continued)"
LIMITS_HEADING = "Credit Limits"
RULE = "R-17: Issue a credit only if the seal is intact."
NEGATION = "Do not issue a credit for opened items."
EXCEPTION = "R-17 exception: Opened items qualify if damage was recorded on delivery."
TABLE = [
    ["Item category", "Maximum credit"],
    ["Desk lamp", "$24.50"],
    ["Floor lamp", "$48.00"],
]


def new_pdf(target):
    return canvas.Canvas(target, pagesize=letter, invariant=True, pageCompression=1)


def page_header(pdf, page_number):
    pdf.setFillColor(colors.HexColor("#13365B"))
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(56, 742, BANNER)
    pdf.setFont("Helvetica-Bold", 27)
    pdf.drawString(56, 695, TITLE)
    pdf.setStrokeColor(colors.HexColor("#B7C7D8"))
    pdf.line(56, 678, 556, 678)
    pdf.line(56, 68, 556, 68)
    pdf.setFont("Helvetica", 12)
    pdf.drawRightString(556, 47, f"Page {page_number} of 2")
    pdf.setFillColor(colors.black)


def heading(pdf, text, y):
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(56, y, text)


def paragraph(pdf, text, y):
    pdf.setFont("Helvetica", 14)
    pdf.drawString(56, y, text)


def native_pdf(output):
    pdf = new_pdf(str(output / "synthetic_policy.pdf"))
    pdf.setTitle(TITLE)
    page_header(pdf, 1)
    heading(pdf, RULE_HEADING, 635)
    paragraph(pdf, RULE, 608)
    paragraph(pdf, NEGATION, 584)
    pdf.showPage()

    page_header(pdf, 2)
    heading(pdf, CONTINUATION_HEADING, 635)
    paragraph(pdf, EXCEPTION, 608)
    heading(pdf, LIMITS_HEADING, 556)
    table = Table(TABLE, colWidths=[225, 185], rowHeights=34)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 1, colors.HexColor("#51687D")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E6EDF5")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 14),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ]
        )
    )
    table.wrapOn(pdf, 500, 200)
    table.drawOn(pdf, 56, 423)
    pdf.showPage()
    pdf.save()


def scanned_pdf(output):
    pdf = new_pdf(str(output / "synthetic_policy_scan.pdf"))
    pdf.setTitle(f"{TITLE} (image-only scan)")
    with pymupdf.open(output / "synthetic_policy.pdf") as native:
        for page in native:
            raster = page.get_pixmap(dpi=180, alpha=False)
            image = ImageReader(io.BytesIO(raster.tobytes("png")))
            pdf.drawImage(image, 0, 0, width=letter[0], height=letter[1])
            pdf.showPage()
    pdf.save()


def markdown_fixture(output):
    lines = [
        f"# {TITLE}",
        "",
        BANNER,
        "",
        f"## {RULE_HEADING}",
        "",
        RULE,
        "",
        NEGATION,
        "",
        EXCEPTION,
        "",
        f"## {LIMITS_HEADING}",
        "",
        "| Item category | Maximum credit |",
        "| --- | --- |",
        "| Desk lamp | $24.50 |",
        "| Floor lamp | $48.00 |",
        "",
    ]
    (output / "synthetic_policy.md").write_bytes("\n".join(lines).encode("utf-8"))


def write_manifest(output):
    fixture_metadata = [
        ("synthetic_policy.pdf", "native_pdf", "application/pdf", "native_extracted"),
        ("synthetic_policy_scan.pdf", "scanned_pdf", "application/pdf", "needs_review"),
        ("synthetic_policy.md", "markdown", "text/markdown", "source_verified"),
        ("synthetic_blank.pdf", "blank_pdf", "application/pdf", "blank"),
        ("synthetic_corrupt.pdf", "corrupt_pdf", "application/pdf", "corrupt"),
    ]
    fixtures = [
        {
            "fixture_id": variant,
            "path": filename,
            "source_kind": "synthetic_format_fixture",
            "format_variant": variant,
            "media_type": media_type,
            "size_bytes": (output / filename).stat().st_size,
            "sha256": hashlib.sha256((output / filename).read_bytes()).hexdigest(),
            "expected_status": status,
        }
        for filename, variant, media_type, status in fixture_metadata
    ]
    manifest = {
        "schema_version": 1,
        "document_version": "synthetic-format-v1",
        "description": "Project-authored parser fixtures with no upstream policy content.",
        "generator": "scripts/build_tau_document_fixtures.py",
        "expected_extractions": "expected_extractions.json",
        "expected_extractions_sha256": hashlib.sha256(
            (OUTPUT / "expected_extractions.json").read_bytes()
        ).hexdigest(),
        "fixtures": fixtures,
    }
    (output / "manifest.json").write_bytes((json.dumps(manifest, indent=2) + "\n").encode("utf-8"))


def build(output):
    native_pdf(output)
    scanned_pdf(output)
    markdown_fixture(output)
    blank = new_pdf(str(output / "synthetic_blank.pdf"))
    blank.showPage()
    blank.save()
    (output / "synthetic_corrupt.pdf").write_bytes(b"%PDF-1.7\n1 0 obj\n<<")
    write_manifest(output)


def main():
    with TemporaryDirectory(prefix="tau-document-fixtures-") as tmp:
        staging = Path(tmp)
        build(staging)
        generated = sorted(staging.iterdir())
        differing = [
            path.name
            for path in generated
            if (OUTPUT / path.name).exists()
            and (OUTPUT / path.name).read_bytes() != path.read_bytes()
        ]
        if differing:
            raise SystemExit(
                f"Existing fixtures differ from generated bytes: {', '.join(differing)}"
            )
        OUTPUT.mkdir(parents=True, exist_ok=True)
        for path in generated:
            destination = OUTPUT / path.name
            if not destination.exists():
                shutil.copyfile(path, destination)


if __name__ == "__main__":
    main()
