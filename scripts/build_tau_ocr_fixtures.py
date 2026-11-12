"""Reproduce the Chinese OCR fixtures from the frozen, agent-authored reference."""
# /// script
# requires-python = ">=3.11"
# dependencies = ["reportlab==4.4.1", "pymupdf==1.26.0", "pillow==11.2.1", "fonttools==4.58.1"]
# ///

import argparse
import hashlib
import io
import json
import random
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlretrieve

import pymupdf
from fontTools.ttLib import TTFont as VariableFont
from fontTools.varLib.instancer import instantiateVariableFont
from PIL import Image, ImageFilter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "tau-ocr"
GROUND_TRUTH = OUTPUT / "ground_truth.json"
FONT = {
    "name": "Noto Sans SC",
    "source": "https://raw.githubusercontent.com/notofonts/noto-cjk/523d033d6cb47f4a80c58a35753646f5c3608a78/Sans/Variable/TTF/Subset/NotoSansSC-VF.ttf",
    "sha256": "d68bafcb48a2707749396aa12bbbd833cb70401f3a9a689fd2902c7e0d295964",
    "license": "OFL-1.1",
    "weight": 400,
}
CLEAR_DPI = 150
LOW_DPI = 92
ROTATION = 1.65
BLUR_RADIUS = 0.7
NOISE_STDDEV = 6.0
NOISE_SEED = 21107
JPEG_QUALITY = 42


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def register_font(temp, provided_font):
    original = provided_font or temp / "NotoSansSC-VF.ttf"
    if provided_font is None:
        urlretrieve(FONT["source"], original)
    if sha256(original) != FONT["sha256"]:
        raise ValueError("Noto Sans SC source hash differs from the pinned fixture font")
    static_font = temp / "NotoSansSC-Regular.ttf"
    with VariableFont(original, recalcTimestamp=False) as variable:
        instance = instantiateVariableFont(variable, {"wght": FONT["weight"]}, inplace=True)
        # Preserve the frozen fixture's OpenType timestamp across regeneration.
        instance["head"].modified = 3877314620
        instance.save(static_font)
    pdfmetrics.registerFont(TTFont("FixtureCJK", str(static_font)))


def new_pdf(path):
    return canvas.Canvas(str(path), pagesize=A4, invariant=True, pageCompression=1)


def draw_line(pdf, text, x, y, size=13):
    pdf.setFont("FixtureCJK", size)
    pdf.drawString(x, y, text)


def draw_table(pdf, cells):
    xs = [50, 148, 252, 443, A4[0] - 50]
    ys = [680, 646, 610, 568, 526, 484]
    for cell in cells:
        row, col = cell["row"], cell["col"]
        x1, x2 = xs[col], xs[col + cell["colspan"]]
        y1, y2 = ys[row], ys[row + cell["rowspan"]]
        pdf.setFillColor(colors.HexColor("#E9EFF6") if row < 2 else colors.white)
        pdf.setStrokeColor(colors.HexColor("#6B7F95"))
        pdf.rect(x1, y2, x2 - x1, y1 - y2, fill=1)
        pdf.setFillColor(colors.black)
        pdf.setFont("FixtureCJK", 12)
        pdf.drawCentredString((x1 + x2) / 2, (y1 + y2) / 2 - 4, cell["text"])


def draw_native(path, document):
    lines = document["reading_order"]
    pdf = new_pdf(path)
    pdf.setTitle(lines[1])
    pdf.setFillColor(colors.HexColor("#294B68"))
    draw_line(pdf, lines[0], 50, 783, 11)
    draw_line(pdf, lines[1], 50, 737, 26)
    pdf.setStrokeColor(colors.HexColor("#A7B6C3"))
    pdf.line(50, 717, A4[0] - 50, 717)
    pdf.setFillColor(colors.black)
    if "table" in document:
        draw_line(pdf, lines[2], 50, 694)
        draw_table(pdf, document["table"]["cells"])
        draw_line(pdf, lines[-1], 50, 452)
    else:
        for index, line in enumerate(lines[2:]):
            draw_line(pdf, line, 50, 680 - 33 * index)
    pdf.showPage()
    pdf.save()


def degrade(image):
    rotated = image.rotate(ROTATION, resample=Image.Resampling.BICUBIC, fillcolor=255)
    blurred = rotated.filter(ImageFilter.GaussianBlur(BLUR_RADIUS))
    rng = random.Random(NOISE_SEED)
    pixels = bytes(
        min(255, max(0, round(value + rng.gauss(0, NOISE_STDDEV))))
        for value in blurred.tobytes()
    )
    noisy = Image.frombytes("L", blurred.size, pixels)
    buffer = io.BytesIO()
    noisy.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    buffer.seek(0)
    with Image.open(buffer) as decoded:
        return decoded.copy()


def draw_scan(source, pdf_path, png_path, low):
    dpi = LOW_DPI if low else CLEAR_DPI
    with pymupdf.open(source) as document:
        pixmap = document[0].get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
    image = Image.frombytes("L", (pixmap.width, pixmap.height), pixmap.samples)
    if low:
        image = degrade(image)
    image.save(png_path, dpi=(dpi, dpi))

    pdf = new_pdf(pdf_path)
    pdf.drawImage(ImageReader(image), 0, 0, width=A4[0], height=A4[1])
    pdf.showPage()
    pdf.save()
    return list(image.size)


def record_sample(stem, document_id, variant, pdf, png=None, dimensions=None):
    return {
        "sample_id": stem,
        "document_id": document_id,
        "variant": variant,
        "pdf": f"data/tau-ocr/{pdf.name}",
        "png": f"data/tau-ocr/{png.name}" if png else None,
        "dimensions_px": dimensions,
        "sha256": {"pdf": sha256(pdf), "png": sha256(png) if png else None},
    }


def build(stage):
    truth = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))
    samples = []
    for document_id, document in truth["documents"].items():
        stem = f"{document_id}_native"
        native = stage / f"{stem}.pdf"
        draw_native(native, document)
        samples.append(record_sample(stem, document_id, "native", native))
        for variant in ("clear", "low"):
            stem = f"{document_id}_{variant}"
            pdf, png = stage / f"{stem}.pdf", stage / f"{stem}.png"
            dimensions = draw_scan(native, pdf, png, low=variant == "low")
            samples.append(record_sample(stem, document_id, variant, pdf, png, dimensions))

    manifest = {
        "schema_version": 1,
        "source_kind": "agent_authored_synthetic_fixture",
        "generator": "scripts/build_tau_ocr_fixtures.py",
        "font": FONT,
        "ground_truth": "data/tau-ocr/ground_truth.json",
        "ground_truth_sha256": sha256(GROUND_TRUTH),
        "scan_settings": {
            "clear": {"dpi": CLEAR_DPI, "colorspace": "gray"},
            "low": {
                "dpi": LOW_DPI,
                "colorspace": "gray",
                "rotation_degrees_counterclockwise": ROTATION,
                "rotation_resampling": "bicubic",
                "blur_radius_px": BLUR_RADIUS,
                "noise_distribution": "gaussian",
                "noise_stddev": NOISE_STDDEV,
                "noise_seed": NOISE_SEED,
                "intermediate_jpeg_quality": JPEG_QUALITY,
            },
        },
        "samples": samples,
    }
    (stage / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path, help="Use a local copy of the pinned font source")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="tau-ocr-fixtures-") as temp_path:
        temp = Path(temp_path)
        register_font(temp, args.font)
        stage = temp / "output"
        stage.mkdir()
        build(stage)
        generated = sorted(stage.iterdir())
        differing = [
            path.name
            for path in generated
            if (OUTPUT / path.name).exists() and sha256(path) != sha256(OUTPUT / path.name)
        ]
        if differing:
            raise SystemExit(f"Existing OCR fixtures differ: {', '.join(differing)}")
        for path in generated:
            target = OUTPUT / path.name
            if not target.exists():
                shutil.copyfile(path, target)


if __name__ == "__main__":
    main()
