# /// script
# requires-python = ">=3.11"
# dependencies = ["pdfplumber==0.11.9"]
# ///
"""Offline extraction of the pinned retail policy and synthetic format fixtures."""

import argparse
import csv
import fcntl
import hashlib
import io
import json
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAU_MANIFEST = ROOT / "data/tau/manifest.json"
FIXTURE_MANIFEST = ROOT / "data/tau-documents/manifest.json"
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*$")
LIST = re.compile(r"^([ \t]*)(?:[-+*]|\d+[.)])[ \t]+")
TABLE_DIVIDER = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")


class DocumentError(ValueError):
    pass


def markdown_blocks(raw):
    lines = raw.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line.encode("utf-8")))
    content = [line.rstrip("\r\n") for line in lines]
    headings = []
    blocks = []
    index = 0
    while index < len(lines):
        line = content[index]
        start = index
        index += 1
        if not line.strip():
            continue
        heading = HEADING.fullmatch(line)
        item = LIST.match(line)
        extra = {}
        if heading:
            level = len(heading[1])
            title = re.sub(r"[ \t]+#+$", "", heading[2])
            while headings and headings[-1]["level"] >= level:
                headings.pop()
            headings.append({"level": level, "title": title})
            kind = "heading"
        elif index < len(lines) and "|" in line and TABLE_DIVIDER.fullmatch(content[index]):
            kind = "table"
            index += 1
            while index < len(lines) and content[index].strip() and "|" in content[index]:
                index += 1
            extra["rows"] = [
                split_table_row(row) for row in [line, *content[start + 2:index]]
            ]
        elif item:
            kind = "list_item"
            extra["indent_columns"] = len(item[1].expandtabs(4))
            while (
                index < len(lines)
                and content[index].startswith((" ", "\t"))
                and content[index].strip()
                and not LIST.match(content[index])
                and not HEADING.fullmatch(content[index])
            ):
                index += 1
        else:
            kind = "clause"
            while (
                index < len(lines)
                and content[index].strip()
                and not HEADING.fullmatch(content[index])
                and not LIST.match(content[index])
                and not (
                    index + 1 < len(lines)
                    and "|" in content[index]
                    and TABLE_DIVIDER.fullmatch(content[index + 1])
                )
            ):
                index += 1
        blocks.append({
            "kind": kind,
            "heading_path": headings.copy(),
            "raw_text": "".join(lines[start:index]),
            "text": "\n".join(content[start:index]),
            "location": {
                "line_start": start + 1,
                "line_end": index,
                "byte_start": offsets[start],
                "byte_end_exclusive": offsets[index],
            },
            **extra,
        })
    if not blocks:
        raise DocumentError("Markdown contains no content")
    return blocks


def split_table_row(row):
    row = row.strip().removeprefix("|")
    if row.endswith("|") and not row.endswith(r"\|"):
        row = row[:-1]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", row)]


def run_tool(command, input_bytes=None):
    try:
        return subprocess.run(command, input=input_bytes, capture_output=True, check=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise DocumentError(f"{command[0]} failed: {exc}") from exc


def tool_version(tool, flag):
    if not shutil.which(tool):
        return None
    try:
        result = subprocess.run(
            [tool, flag], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, check=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return next(iter(result.stdout.splitlines()), None)


def ocr_page(source, page_number, page_width, page_height):
    for tool in ("pdftoppm", "tesseract"):
        if not shutil.which(tool):
            raise DocumentError(f"scanned page requires {tool} on PATH")
    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "page"
        run_tool([
            "pdftoppm", "-f", str(page_number), "-l", str(page_number),
            "-r", "300", "-singlefile", "-png", str(source), str(prefix),
        ])
        tsv = run_tool(
            ["tesseract", "stdin", "stdout", "-l", "eng", "tsv"],
            input_bytes=prefix.with_suffix(".png").read_bytes(),
        )
    rows = list(csv.DictReader(io.StringIO(tsv.decode("utf-8")), delimiter="\t"))
    page_row = next((row for row in rows if row["level"] == "1"), None)
    if page_row is None:
        raise DocumentError("OCR returned no page coordinates")
    lines = {}
    for row in rows:
        if row["level"] == "5" and row["text"].strip():
            key = tuple(row[name] for name in ("block_num", "par_num", "line_num"))
            lines.setdefault(key, []).append(row)
    blocks = []
    x_scale = page_width / int(page_row["width"])
    y_scale = page_height / int(page_row["height"])
    for words in lines.values():
        left = [int(word["left"]) for word in words]
        top = [int(word["top"]) for word in words]
        right = [x + int(word["width"]) for x, word in zip(left, words, strict=True)]
        bottom = [y + int(word["height"]) for y, word in zip(top, words, strict=True)]
        bbox = [min(left) * x_scale, min(top) * y_scale, max(right) * x_scale, max(bottom) * y_scale]
        value = " ".join(word["text"] for word in words)
        blocks.append(pdf_block(
            "ocr_line", value, page_number, bbox,
            review_status="needs_review",
            evidence_kind="ocr_observation",
            min_word_confidence=min(float(word["conf"]) for word in words),
        ))
    if not blocks:
        raise DocumentError("OCR returned no text")
    return blocks


def pdf_block(kind, value, page_number, bbox, **extra):
    return {
        "kind": kind,
        "text": value,
        "location": {
            "page": page_number,
            "bbox": [round(point, 3) for point in bbox],
            "coordinate_space": "pdf_points_top_left",
        },
        **extra,
    }


def inside_table(obj, bbox):
    x = (obj["x0"] + obj["x1"]) / 2
    y = (obj["top"] + obj["bottom"]) / 2
    return bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]


def native_blocks(page):
    tables = [
        (table, table.extract()) for table in page.find_tables()
    ]
    tables = [
        (table, rows) for table, rows in tables
        if any(cell and cell.strip() for row in rows for cell in row)
    ]
    prose = page.filter(
        lambda obj: obj["object_type"] != "char"
        or not any(inside_table(obj, table.bbox) for table, _ in tables)
    )
    sizes = Counter(round(char["size"], 2) for char in prose.chars)
    body_size = sizes.most_common(1)[0][0] if sizes else None
    blocks = []
    for line in prose.extract_text_lines(strip=False):
        line_sizes = Counter(round(char["size"], 2) for char in line["chars"])
        line_size = line_sizes.most_common(1)[0][0]
        blocks.append(pdf_block(
            "line", line["text"], page.page_number,
            [line["x0"], line["top"], line["x1"], line["bottom"]],
            font_size_points=line_size,
            heading_candidate=body_size is not None and line_size >= body_size + 2,
        ))

    for table, rows in tables:
        blocks.append(pdf_block(
            "table", "\n".join(" | ".join(cell or "" for cell in row) for row in rows),
            page.page_number, table.bbox, rows=rows,
            cell_bboxes=[
                [[round(point, 3) for point in cell] if cell else None for cell in row.cells]
                for row in table.rows
            ],
        ))
    return sorted(blocks, key=lambda block: (block["location"]["bbox"][1], block["location"]["bbox"][0]))


def pdf_pages(source, raw):
    try:
        import pdfplumber
    except ImportError as exc:
        raise DocumentError("PDF extraction requires pdfplumber") from exc
    pages = []
    try:
        with pdfplumber.open(io.BytesIO(raw)) as document:
            for page in document.pages:
                blocks = native_blocks(page)
                if blocks:
                    # An image may hold additional text in a mixed-content PDF.
                    review = "needs_review" if page.images else "native_extracted"
                    method = "native"
                    reason = "embedded_image_requires_review" if page.images else None
                else:
                    review = "needs_review"
                    method = "ocr"
                    reason = "ocr_text_and_table_structure_require_review"
                    try:
                        blocks = ocr_page(source, page.page_number, page.width, page.height)
                    except DocumentError as exc:
                        reason = str(exc)
                pages.append({
                    "page": page.page_number,
                    "method": method,
                    "review_status": review,
                    "review_reason": reason,
                    "blocks": blocks,
                })
    except Exception as exc:
        raise DocumentError(f"PDF extraction failed for {source.name}: {exc}") from exc
    if not pages:
        raise DocumentError("PDF contains no pages")
    return pages


def source_record(input_path, fixture):
    manifest_path = FIXTURE_MANIFEST if fixture else TAU_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if fixture:
        if Path(input_path).name != input_path:
            raise DocumentError("fixture must be a filename listed in the manifest")
        entry = next((item for item in manifest["fixtures"] if item["path"] == input_path), None)
        if entry is None or entry["source_kind"] != "synthetic_format_fixture":
            raise DocumentError("unknown synthetic format fixture")
        source = manifest_path.parent / input_path
        metadata = {
            "source_kind": "synthetic_format_fixture",
            "fixture_id": entry["fixture_id"],
            "format_variant": entry["format_variant"],
            "document_version": entry.get("document_version", manifest.get("document_version")),
            "source_url": entry.get("source_url"),
            "effective_date": None,
        }
    else:
        source = Path(input_path)
        if source.name != "policy.md":
            raise DocumentError("only the pinned tau retail policy.md is accepted")
        entry = manifest["files"]["policy.md"]
        metadata = {
            "source_kind": "tau2_retail_policy",
            "document_version": manifest["revision"],
            "source_revision": manifest["revision"],
            "source_url": entry["source_url"],
            "effective_date": None,
            "license": manifest["license"],
        }
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != entry["sha256"] or len(raw) != entry["size_bytes"]:
        raise DocumentError(f"source does not match manifest: {source.name}")
    metadata.update({
        "name": source.name,
        "manifest": str(manifest_path.relative_to(ROOT)),
        "sha256": digest,
        "size_bytes": len(raw),
    })
    return source, raw, metadata


def extract(input_path, *, fixture=False):
    source, raw, metadata = source_record(input_path, fixture)
    identity = f"{metadata['source_kind']}:{metadata['document_version']}:{source.name}:{metadata['sha256']}"
    doc_id = hashlib.sha256(identity.encode()).hexdigest()
    extractor = {"markdown": "tau_documents_v1"}
    if source.suffix == ".md":
        blocks = markdown_blocks(raw.decode("utf-8"))
        review_status = "source_verified"
        pages = []
    elif source.suffix == ".pdf" and fixture:
        # Rasterize the verified bytes, using the same snapshot as native extraction.
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / source.name
            snapshot.write_bytes(raw)
            pages = pdf_pages(snapshot, raw)
        blocks = [block for page in pages for block in page["blocks"]]
        extractor = {"pdfplumber": version("pdfplumber")}
        if any(page["method"] == "ocr" for page in pages):
            for tool, flag in (("pdftoppm", "-v"), ("tesseract", "--version")):
                extractor[tool] = tool_version(tool, flag)
            extractor.update({"ocr_language": "eng", "raster_dpi": 300})
        review_status = (
            "needs_review" if any(page["review_status"] == "needs_review" for page in pages)
            else "native_extracted"
        )
    else:
        raise DocumentError("unsupported document format")
    title = next((
        block["heading_path"][-1]["title"] if block["kind"] == "heading" else block["text"].strip()
        for block in blocks
        if block["kind"] == "heading" or block.get("heading_candidate")
    ), None)
    extraction_status = "parsed"
    if not blocks:
        extraction_status = "empty"
    elif any(not page["blocks"] for page in pages):
        extraction_status = "partial"
    occurrences = Counter()
    for block in blocks:
        locator = json.dumps(block["location"], sort_keys=True)
        identity = f"{doc_id}:{block['kind']}:{locator}"
        ordinal = occurrences[identity]
        occurrences[identity] += 1
        block["id"] = hashlib.sha256(f"{identity}:{ordinal}".encode()).hexdigest()
    return raw, {
        "schema_version": 1,
        "id": doc_id,
        "source": metadata,
        "title_candidate": title,
        "extraction_status": extraction_status,
        "review_status": review_status,
        "extractor": extractor,
        "blocks": blocks,
        "pages": [{key: value for key, value in page.items() if key != "blocks"} for page in pages],
    }


def export(input_path, output, *, fixture=False):
    raw, document = extract(input_path, fixture=fixture)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    with tempfile.TemporaryDirectory(prefix=".tau-documents-", dir=output.parent) as tmp:
        stage = Path(tmp) / "export"
        stage.mkdir()
        original = stage / "original"
        original.mkdir()
        (original / document["source"]["name"]).write_bytes(raw)
        (stage / "document.json").write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        preview = [
            document["source"]["name"],
            f"Source: {document['source']['source_kind']}",
            f"Extraction: {document['extraction_status']}",
            f"Review: {document['review_status']}",
        ]
        preview.extend(
            f"Page {page['page']} [{page['method']}, {page['review_status']}]"
            + (f" {page['review_reason']}" if page["review_reason"] else "")
            for page in document["pages"]
        )
        for block in document["blocks"]:
            location = block["location"]
            label = (
                f"page {location['page']} @ {location['bbox']}" if "page" in location
                else f"lines {location['line_start']}-{location['line_end']}"
            )
            preview.append(f"[{label}, {block['kind']}]\n{block['text']}")
        (stage / "preview.txt").write_text("\n\n".join(preview) + "\n", encoding="utf-8")
        with (output.parent / ".tau-documents.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if output.exists() or output.is_symlink():
                raise FileExistsError(f"output already exists: {output}")
            stage.rename(output)
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--policy", help="path to the P1 policy.md")
    sources.add_argument("--fixture", help="filename in the synthetic fixture manifest")
    parser.add_argument("--output", type=Path, required=True, help="new output directory")
    args = parser.parse_args()
    try:
        document = export(args.fixture or args.policy, args.output, fixture=args.fixture is not None)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"{exc}\n")
    print(
        f"{document['extraction_status']}, {document['review_status']}: "
        f"{len(document['blocks'])} blocks in {args.output}"
    )
    if document["extraction_status"] != "parsed":
        parser.exit(3)


if __name__ == "__main__":
    main()
