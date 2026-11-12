import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("evaluate_tau_ocr", ROOT / "scripts/evaluate_tau_ocr.py")
ocr_eval = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ocr_eval)

SAMPLE = {"sample_id": "case", "variant": "low"}
REFERENCE = {
    "reading_order": ["退款 50.00", "拆封不退款。"],
    "key_fields": {"amount": ["50.00"], "negation": ["拆封不退款"]},
}


def test_cer_and_key_fields_preserve_decimal_and_negation():
    page = ocr_eval.score_page(
        SAMPLE, REFERENCE,
        {"status": "ok", "text": "退款5000\n拆封退款。", "input_sha256": "hash"},
        False, "hash",
    )
    assert page["edit_errors"] == 2
    assert page["cer"] == 2 / len("退款50.00拆封不退款。")
    assert not page["key_fields"]["amount"][0]["matched"]
    assert not page["key_fields"]["negation"][0]["matched"]


@pytest.mark.parametrize("actual", ["150.00", "50.000", "-50.00", "−50.00"])
def test_amount_does_not_match_substring_of_other_number(actual):
    assert not ocr_eval.field_present("amount", "50.00", actual)


def test_empty_failed_missing_and_unverified_pages_stay_in_denominator():
    observations = [
        {},
        {"status": "timeout", "input_sha256": "hash"},
        {"status": "ok", "text": " \n ", "input_sha256": "hash"},
        {"status": "ok", "text": "退款50.00拆封不退款。", "input_sha256": "wrong"},
    ]
    pages = [ocr_eval.score_page(SAMPLE, REFERENCE, obs, False, "hash") for obs in observations]
    summary = ocr_eval.summarize(pages)
    assert summary["page_status"] == {
        "missing_page": 1, "timeout": 1, "empty_output": 1, "input_hash_mismatch": 1,
    }
    assert summary["cer"] == 1
    assert summary["key_fields"]["amount"] == {"matched": 0, "total": 4, "accuracy": 0}


def cell(row, col, text, colspan=1):
    return {"row": row, "col": col, "rowspan": 1, "colspan": colspan, "text": text}


def test_table_uses_anchors_spans_and_content_instead_of_repeated_text():
    reference = {"rows": 2, "columns": 2, "cells": [
        cell(0, 0, "退款", 2), cell(1, 0, "退款"), cell(1, 1, "换货"),
    ]}
    result = {"rows": 2, "columns": 2, "cells": [
        cell(0, 0, "退款"), cell(1, 1, "退款"), cell(1, 0, "换货"),
    ]}
    table = ocr_eval.score_table(reference, result, "ok", True)
    assert table["status"] == "ok"
    assert table["span_matches"] == 2
    assert table["exact_matches"] == 0
    assert not table["exact_grid"]
    assert ocr_eval.score_table(reference, reference, "ok", True)["exact_grid"]
    assert ocr_eval.score_table(reference, None, "ok", False) == {"status": "unsupported"}
    assert ocr_eval.score_table(reference, None, "ok", True)["status"] == "missing_structure"
    invalid = {**reference, "cells": [cell(0, 1, "退款", 2)]}
    assert ocr_eval.score_table(reference, invalid, "ok", True)["status"] == "invalid_structure"


def test_score_preserves_artifact_hashes_and_excludes_native(tmp_path):
    png = tmp_path / "case.png"
    png.write_bytes(b"fixture")
    reference = tmp_path / "truth.json"
    reference.write_text(json.dumps({"documents": {"doc": REFERENCE}}))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "ground_truth_sha256": ocr_eval.sha256(reference),
        "samples": [
            {**SAMPLE, "document_id": "doc", "png": str(png),
             "sha256": {"png": ocr_eval.sha256(png)}},
            {"sample_id": "native", "variant": "native", "png": None},
        ],
    }))
    capture = tmp_path / "raw.json"
    capture.write_text(json.dumps({"engine": "paddle", "pages": [
        {"sample_id": "case", "status": "ok", "text": "\n".join(REFERENCE["reading_order"]),
         "input_sha256": ocr_eval.sha256(png)},
    ]}))
    report = ocr_eval.score(manifest, reference, [capture])
    assert report["audit"]["ground_truth_sha256"] == ocr_eval.sha256(reference)
    assert report["audit"]["excluded_native_sample_ids"] == ["native"]
    assert report["engines"]["paddle"]["capture"]["sha256"] == ocr_eval.sha256(capture)
    assert report["engines"]["paddle"]["overall"]["cer"] == 0
    original_capture = capture.read_text()
    payload = json.loads(original_capture)
    payload["pages"][0]["sample_id"] = "wrong-case"
    capture.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="unexpected sample IDs"):
        ocr_eval.score(manifest, reference, [capture])
    capture.write_text(original_capture)
    png.write_bytes(b"changed fixture")
    with pytest.raises(ValueError, match="SHA-256 mismatch for case"):
        ocr_eval.score(manifest, reference, [capture])
    reference.write_text("{}")
    with pytest.raises(ValueError, match="SHA-256 mismatch for ground truth"):
        ocr_eval.score(manifest, reference, [capture])


def test_merged_span_recall_rejects_wrong_rowspan_and_overlap():
    merged = {**cell(0, 0, "合并"), "rowspan": 2}
    reference = {"rows": 2, "columns": 1, "cells": [merged]}
    assert ocr_eval.score_table(reference, reference, "ok", True)["merged_span_matches"] == 1
    wrong_span = {**reference, "cells": [cell(0, 0, "合并")]}
    result = ocr_eval.score_table(reference, wrong_span, "ok", True)
    assert result["reference_merged_cells"] == 1
    assert result["merged_span_matches"] == 0
    overlap = {**reference, "cells": [merged, cell(1, 0, "重叠")]}
    result = ocr_eval.score_table(reference, overlap, "ok", True)
    assert result["status"] == "invalid_structure"
    assert result["observed_cells"] == result["exact_matches"] == 0
