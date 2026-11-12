"""Run the P2.1 OCR engines on supplied page images or one selected PDF page."""

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / ".runtime/p21-paddle"


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class TableParser(HTMLParser):
    """Expand model HTML spans into zero-based anchor coordinates."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.row = -1
        self.col = 0
        self.occupied = set()
        self.cells = []
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row += 1
            self.col = 0
        if tag in {"td", "th"} and self.row >= 0:
            attrs = dict(attrs)
            while (self.row, self.col) in self.occupied:
                self.col += 1
            rowspan = int(attrs.get("rowspan", 1))
            colspan = int(attrs.get("colspan", 1))
            if min(rowspan, colspan) < 1:
                raise ValueError("nonpositive table span")
            self.cell = {
                "row": self.row,
                "col": self.col,
                "rowspan": rowspan,
                "colspan": colspan,
                "text": "",
            }
            for row in range(self.row, self.row + rowspan):
                for col in range(self.col, self.col + colspan):
                    if (row, col) in self.occupied:
                        raise ValueError("overlapping table spans")
                    self.occupied.add((row, col))
            self.col += colspan

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None:
            self.cell["text"] = self.cell["text"].strip()
            self.cells.append(self.cell)
            self.cell = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["text"] += data

    def grid(self):
        if not self.cells or any(row > self.row for row, _ in self.occupied):
            raise ValueError("missing table cells or spans extending beyond final row")
        return {
            "rows": self.row + 1,
            "columns": max((col + 1 for _, col in self.occupied), default=0),
            "cells": self.cells,
        }


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def paddle_page(pipeline, path):
    (result,) = pipeline.predict(str(path))
    raw = result.json["res"]
    ocr = raw["overall_ocr_res"]
    output = {"text": "\n".join(ocr["rec_texts"]), "raw": raw, "tables": []}
    for table in raw.get("table_res_list", []):
        html = table["pred_html"]
        parser = TableParser()
        try:
            parser.feed(html)
            parser.close()
            grid = parser.grid()
        except (TypeError, ValueError) as exc:
            grid = {"table_parse_error": str(exc)}
        output["tables"].append({"html": html, **grid})
    match output["tables"]:
        case [table]:
            output["table"] = table
            output["table_status"] = (
                "invalid_structure" if "table_parse_error" in table else "single_table"
            )
        case []:
            output["table_status"] = "no_table"
        case _:
            output["table_status"] = "multiple_tables"
    return output


def run(args):
    start = time.perf_counter()
    metadata = {"python": platform.python_version(), "platform": platform.platform()}
    report = {
        "engine": "paddle-structurev3",
        "table_supported": True,
        "timing_mode": (
            "initialized_pipeline_sequential_pages_includes_first_prediction_and_conversion"
        ),
        "metadata": metadata,
        "pages": [],
    }
    try:
        os.environ["PADDLE_PDX_CACHE_HOME"] = str(args.runtime / "cache")
        os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "BOS")
        from paddleocr import PPStructureV3

        config = {
            "device": "cpu",
            "cpu_threads": 4,
            "enable_mkldnn": False,
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "use_formula_recognition": False,
            "use_region_detection": False,
            "use_chart_recognition": False,
            "use_seal_recognition": False,
            "use_table_recognition": True,
            "text_detection_model_name": "PP-OCRv5_mobile_det",
            "text_recognition_model_name": "PP-OCRv5_mobile_rec",
            "layout_detection_model_name": "PP-DocLayout-M",
        }
        metadata["versions"] = {
            name: importlib.metadata.version(name)
            for name in ("paddlepaddle", "paddleocr", "paddlex")
        }
        metadata["config"] = config
        metadata["model_source"] = os.environ["PADDLE_PDX_MODEL_SOURCE"]
        pipeline = PPStructureV3(**config)
    except Exception as exc:  # noqa: BLE001 - record third-party startup failures
        report["startup_error"] = f"{type(exc).__name__}: {exc}"
    report["startup_ms"] = round((time.perf_counter() - start) * 1000, 2)

    for source in args.inputs:
        page = {"sample_id": source.stem, "input": str(source), "status": "failed"}
        report["pages"].append(page)
        try:
            page["input_sha256"] = sha256(source)
            if "startup_error" in report:
                page["error"] = report["startup_error"]
                continue
            with tempfile.TemporaryDirectory() as directory:
                image = source
                if source.suffix.lower() == ".pdf":
                    prefix = Path(directory) / "page"
                    command(
                        "pdftoppm",
                        "-r",
                        str(args.dpi),
                        "-f",
                        str(args.page),
                        "-l",
                        str(args.page),
                        "-singlefile",
                        "-png",
                        str(source),
                        str(prefix),
                    )
                    image = prefix.with_suffix(".png")
                    page.update(pdf_page=args.page, render_dpi=args.dpi)
                page["raster_sha256"] = sha256(image)
                before = time.perf_counter()
                try:
                    page.update(paddle_page(pipeline, image), status="ok")
                finally:
                    page["elapsed_ms"] = round((time.perf_counter() - before) * 1000, 2)
        except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - retain each failed page
            page["error"] = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, subprocess.CalledProcessError):
                page["stderr"] = exc.stderr
            if isinstance(exc, KeyboardInterrupt):
                break

    models = args.runtime / "cache" / "official_models"
    metadata["weights"] = {
        str(path.relative_to(models)): sha256(path)
        for path in sorted(models.glob("*/inference.*"))
        if path.is_file()
    }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--runtime", type=Path, default=RUNTIME)
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument("--page", type=int, default=1)
    args = parser.parse_args()
    if min(args.dpi, args.page) < 1:
        parser.error("--dpi and --page must be positive")
    args.runtime = args.runtime.resolve()
    if len({path.stem for path in args.inputs}) != len(args.inputs):
        parser.error("input filenames must have distinct stems")
    if args.out.exists():
        parser.error(f"output already exists: {args.out}")
    result = run(args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return int(
        len(result["pages"]) != len(args.inputs)
        or any(page["status"] != "ok" for page in result["pages"])
    )


if __name__ == "__main__":
    sys.exit(main())
