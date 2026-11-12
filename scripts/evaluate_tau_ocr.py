#!/usr/bin/env python3
"""Run the Tesseract baselines or score already-captured Tau OCR observations."""

import argparse
import csv
import hashlib
import io
import json
import math
import re
import subprocess
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ("amount", "negation", "date", "exception")
LANGUAGES = {"tesseract-eng": "eng", "tesseract-chi_sim-eng": "chi_sim+eng"}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize(value):
    return re.sub(r"\s+", "", value)


def edit_distance(expected, actual):
    previous = list(range(len(actual) + 1))
    for index, expected_char in enumerate(expected, 1):
        current = [index]
        for offset, actual_char in enumerate(actual, 1):
            current.append(min(
                previous[offset] + 1, current[-1] + 1,
                previous[offset - 1] + (expected_char != actual_char),
            ))
        previous = current
    return previous[-1]


def field_present(kind, expected, actual):
    token = re.escape(normalize(expected))
    if kind in {"amount", "date"}:
        token = rf"(?<![0-9.．\-+＋－−]){token}(?![0-9.．]|[,，][0-9])"
    return bool(re.search(token, actual))


def grid_cells(grid):
    rows, columns = grid["rows"], grid["columns"]
    if type(rows) is not int or type(columns) is not int or min(rows, columns) < 1:
        raise ValueError("invalid table dimensions")
    cells = {}
    covered = set()
    for cell in grid["cells"]:
        row, col, rowspan, colspan = (cell[key] for key in ("row", "col", "rowspan", "colspan"))
        if (
            any(type(value) is not int for value in (row, col, rowspan, colspan))
            or min(row, col) < 0 or min(rowspan, colspan) < 1
            or row + rowspan > rows or col + colspan > columns
        ):
            raise ValueError("invalid table cell coordinates or span")
        coordinates = {
            (r, c) for r in range(row, row + rowspan) for c in range(col, col + colspan)
        }
        if covered.intersection(coordinates):
            raise ValueError("overlapping table cells")
        covered.update(coordinates)
        cells[row, col] = (rowspan, colspan, normalize(cell["text"]))
    return cells


def score_table(reference, result, status, supported):
    if supported is not True:
        return {"status": "unsupported" if supported is False else "unknown_support"}
    expected = grid_cells(reference)
    observed = {}
    dimensions_match = False
    if status == "ok":
        if result is None:
            status = "missing_structure"
        else:
            try:
                observed = grid_cells(result)
                if observed:
                    dimensions_match = all(
                        result[key] == reference[key] for key in ("rows", "columns")
                    )
                else:
                    status = "empty_structure"
            except (KeyError, TypeError, ValueError, AttributeError):
                status = "invalid_structure"
    matching_spans = {
        anchor for anchor, cell in expected.items()
        if observed.get(anchor, ())[:2] == cell[:2]
    }
    merged = {anchor for anchor, cell in expected.items() if cell[:2] != (1, 1)}
    exact = sum(observed.get(anchor) == cell for anchor, cell in expected.items())
    return {
        "status": status,
        "reference_cells": len(expected),
        "observed_cells": len(observed),
        "span_matches": len(matching_spans),
        "reference_merged_cells": len(merged),
        "merged_span_matches": len(merged & matching_spans),
        "exact_matches": exact,
        "dimensions_match": dimensions_match,
        "exact_grid": dimensions_match and exact == len(expected) == len(observed),
    }


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def score_page(sample, reference, observation, supported, input_hash):
    reported_status = observation.get("status")
    status = reported_status if reported_status in ("ok", "success", "failed", "timeout") else (
        "invalid_status" if observation else "missing_page"
    )
    if status in {"ok", "success"}:
        status = "ok" if isinstance(observation.get("text"), str) else "invalid_text"
    observed_hash = observation.get("input_sha256")
    if observation and observed_hash != input_hash:
        status = "input_hash_mismatch" if observed_hash else "missing_input_hash"
    expected = normalize("\n".join(reference["reading_order"]))
    actual = normalize(observation["text"]) if status == "ok" else ""
    if status == "ok" and not actual:
        status = "empty_output"
    error_count = edit_distance(expected, actual)
    elapsed = observation.get("elapsed_ms")
    if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
        elapsed = None
    result = {
        "sample_id": sample["sample_id"], "variant": sample["variant"], "status": status,
        "reported_status": reported_status,
        "error": observation.get("error"), "input_sha256": input_hash,
        "observed_input_sha256": observed_hash, "elapsed_ms": elapsed,
        "edit_errors": error_count, "reference_characters": len(expected),
        "cer": ratio(error_count, len(expected)),
        "key_fields": {
            kind: [
                {"expected": value, "matched": field_present(kind, value, actual)}
                for value in reference["key_fields"].get(kind, [])
            ]
            for kind in FIELDS
        },
    }
    if "table" in reference:
        result["table"] = score_table(reference["table"], observation.get("table"), status, supported)
        result["table"]["reported_status"] = observation.get("table_status")
    return result


def summarize(pages):
    errors = sum(page["edit_errors"] for page in pages)
    characters = sum(page["reference_characters"] for page in pages)
    table_pages = [page["table"] for page in pages if "table" in page]
    supported = [table for table in table_pages if table["status"] not in {"unsupported", "unknown_support"}]
    table_counts = {
        key: sum(table[key] for table in supported)
        for key in (
            "reference_cells", "observed_cells", "span_matches", "reference_merged_cells",
            "merged_span_matches", "exact_matches", "exact_grid",
        )
    }
    timings = [page["elapsed_ms"] for page in pages if page["elapsed_ms"] is not None]
    fields = {}
    for kind in FIELDS:
        items = [item for page in pages for item in page["key_fields"][kind]]
        matches = sum(item["matched"] for item in items)
        fields[kind] = {"matched": matches, "total": len(items), "accuracy": ratio(matches, len(items))}
    return {
        "page_status": dict(Counter(page["status"] for page in pages)),
        "pages": len(pages),
        "edit_errors": errors,
        "reference_characters": characters,
        "cer": ratio(errors, characters),
        "key_fields": fields,
        "elapsed_ms": {"reported_pages": len(timings), "mean": ratio(sum(timings), len(timings))},
        "table": {
            "page_status": dict(Counter(table["status"] for table in table_pages)),
            "reference_pages": len(table_pages),
            "supported_pages": len(supported),
            "observed_grid_pages": sum(table["status"] == "ok" for table in supported),
            **(table_counts if supported else {}),
            "cell_recall": ratio(table_counts["exact_matches"], table_counts["reference_cells"]),
            "cell_precision": ratio(table_counts["exact_matches"], table_counts["observed_cells"]),
            "span_recall": ratio(table_counts["span_matches"], table_counts["reference_cells"]),
            "merged_span_recall": ratio(
                table_counts["merged_span_matches"], table_counts["reference_merged_cells"]
            ),
        },
    }


def samples_from_manifest(manifest):
    samples = manifest["samples"]
    ids = [sample["sample_id"] for sample in samples]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate manifest sample IDs")
    return [sample for sample in samples if sample["variant"] != "native"]


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def check_hash(label, actual, expected):
    if actual != expected:
        raise ValueError(f"SHA-256 mismatch for {label}: {actual} != {expected}")


def scan_bytes(sample):
    image = (ROOT / sample["png"]).read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    check_hash(sample["sample_id"], digest, sample["sha256"]["png"])
    return image, digest


def score(manifest_path, reference_path, result_paths):
    manifest = read_json(manifest_path)
    reference_bytes = reference_path.read_bytes()
    reference_hash = hashlib.sha256(reference_bytes).hexdigest()
    check_hash("ground truth", reference_hash, manifest["ground_truth_sha256"])
    references = json.loads(reference_bytes)["documents"]
    samples = samples_from_manifest(manifest)
    input_hashes = {sample["sample_id"]: scan_bytes(sample)[1] for sample in samples}
    engines = {}
    for path in result_paths:
        capture = read_json(path)
        engine = capture["engine"]
        if engine in engines:
            raise ValueError(f"duplicate engine {engine}")
        observations = {page["sample_id"]: page for page in capture["pages"]}
        if len(observations) != len(capture["pages"]):
            raise ValueError(f"duplicate observation sample IDs for {engine}")
        unexpected_ids = observations.keys() - input_hashes.keys()
        if unexpected_ids:
            raise ValueError(f"unexpected sample IDs for {engine}: {sorted(unexpected_ids)}")
        supported = capture.get("table_supported")
        if "table_supported" in capture and type(supported) is not bool:
            raise ValueError(f"table_supported must be a boolean for {engine}")
        pages = [
            score_page(
                sample, references[sample["document_id"]], observations.get(sample["sample_id"], {}),
                supported, input_hashes[sample["sample_id"]],
            )
            for sample in samples
        ]
        engines[engine] = {
            "capture": {"path": str(path), "sha256": sha256(path)},
            "startup_ms": capture.get("startup_ms"),
            "startup_error": capture.get("startup_error"),
            "metadata": capture.get("metadata"),
            "timing_mode": capture.get("timing_mode"),
            "table_supported": supported,
            "overall": summarize(pages),
            "by_variant": {
                variant: summarize([page for page in pages if page["variant"] == variant])
                for variant in sorted({page["variant"] for page in pages})
            },
            "pages": pages,
        }
    return {
        "schema_version": 1,
        "audit": {
            "scorer_sha256": sha256(Path(__file__)),
            "manifest_sha256": sha256(manifest_path),
            "ground_truth_sha256": reference_hash,
            "scored_scan_count": len(samples),
            "scored_source_count": len({sample["document_id"] for sample in samples}),
            "excluded_native_sample_ids": [
                sample["sample_id"] for sample in manifest["samples"] if sample["variant"] == "native"
            ],
        },
        "engines": engines,
    }


def tesseract_text(tsv):
    lines = {}
    rows = csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE)
    found_page = False
    for row in rows:
        if row["level"] == "1":
            found_page = True
        if row["level"] == "5" and row["text"].strip():
            key = tuple(row[name] for name in ("block_num", "par_num", "line_num"))
            lines.setdefault(key, []).append(row["text"])
    if not found_page:
        raise ValueError("Tesseract TSV has no page record")
    return "\n".join(" ".join(words) for words in lines.values())


def run_tesseract(manifest_path, engine, timeout, tessdata_dir):
    try:
        version = subprocess.run(
            ["tesseract", "--version"], capture_output=True, text=True, timeout=timeout, check=False,
        ).stdout.splitlines()[:1]
    except (OSError, subprocess.TimeoutExpired):
        version = []
    samples = samples_from_manifest(read_json(manifest_path))
    pages = []
    language = LANGUAGES[engine]
    command = ["tesseract", "stdin", "stdout", "-l", language]
    language_hashes = None
    if tessdata_dir is not None:
        command.extend(["--tessdata-dir", str(tessdata_dir)])
        language_hashes = {
            name: sha256(tessdata_dir / f"{name}.traineddata")
            for name in language.split("+")
        }
    command.extend(["-c", "tessedit_create_txt=0", "-c", "tessedit_create_tsv=1"])
    for sample in samples:
        image, digest = scan_bytes(sample)
        page = {"sample_id": sample["sample_id"], "input_sha256": digest}
        start = time.perf_counter()
        try:
            result = subprocess.run(
                command, input=image, capture_output=True, timeout=timeout, check=False,
            )
            if result.returncode:
                page.update(status="failed", error=result.stderr.decode("utf-8", errors="replace").strip())
            else:
                tsv = result.stdout.decode("utf-8")
                page.update(raw_tsv=tsv, raw_tsv_sha256=hashlib.sha256(result.stdout).hexdigest())
                page.update(status="ok", text=tesseract_text(tsv))
        except subprocess.TimeoutExpired:
            page.update(status="timeout", error=f"Tesseract exceeded {timeout} seconds")
        except (OSError, ValueError, KeyError) as exc:
            page.update(status="failed", error=str(exc))
        page["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 3)
        pages.append(page)
    return {
        "engine": engine, "pages": pages, "startup_ms": None, "table_supported": False,
        "timing_mode": "cold_process_per_page_includes_tsv_parsing",
        "metadata": {"command": command, "version": version, "language_sha256": language_hashes},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    baseline = commands.add_parser("tesseract")
    baseline.add_argument("--engine", choices=LANGUAGES, required=True)
    baseline.add_argument("--timeout", type=float, default=120)
    baseline.add_argument("--tessdata-dir", type=Path)
    scoring = commands.add_parser("score")
    scoring.add_argument("--ground-truth", type=Path, required=True)
    scoring.add_argument("--result", type=Path, action="append", required=True)
    for command in (baseline, scoring):
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "tesseract":
        report = run_tesseract(args.manifest, args.engine, args.timeout, args.tessdata_dir)
    else:
        report = score(args.manifest, args.ground_truth, args.result)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
