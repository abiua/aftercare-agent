import hashlib
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("tau_documents", ROOT / "scripts/tau_documents.py")
documents = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(documents)
EXPECTED = json.loads((ROOT / "data/tau-documents/expected_extractions.json").read_text())


def test_markdown_retains_rules_hierarchy_source_and_stable_ids(tmp_path):
    name = "synthetic_policy.md"
    raw, doc = documents.extract(name, fixture=True)
    assert doc == documents.extract(name, fixture=True)[1]
    assert doc["source"]["source_kind"] == "synthetic_format_fixture"
    assert doc["source"]["effective_date"] is None
    assert doc["source"]["sha256"] == hashlib.sha256(raw).hexdigest()
    blocks = doc["blocks"]
    facts = EXPECTED["shared_policy_facts"]
    positions = EXPECTED["fixtures"][name]
    for fact in ("condition", "negation", "exception"):
        block = next(block for block in blocks if facts[fact] in block["text"])
        assert block["location"]["line_start"] == positions[f"{fact}_line"]
        assert [heading["title"] for heading in block["heading_path"]] == [
            facts["title"], "Return Credit",
        ]
    table = next(block for block in blocks if block["kind"] == "table")
    assert table["rows"] == positions["table_cells"]
    assert [table["location"][key] for key in ("line_start", "line_end")] == positions["table_lines"]
    for block in blocks:
        loc = block["location"]
        assert raw[loc["byte_start"]:loc["byte_end_exclusive"]] == block["raw_text"].encode()
    output = tmp_path / "output"
    documents.export(name, output, fixture=True)
    assert (output / "original" / name).read_bytes() == raw
    assert json.loads((output / "document.json").read_text()) == doc
    assert facts["negation"] in (output / "preview.txt").read_text()
    with pytest.raises(FileExistsError):
        documents.export(name, output, fixture=True)
    assert (output / "original" / name).read_bytes() == raw


def test_concurrent_exports_publish_only_one_complete_result(tmp_path):
    output = tmp_path / "output"
    name = "synthetic_policy.md"

    def publish():
        try:
            documents.export(name, output, fixture=True)
        except FileExistsError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: publish(), range(2))) == [False, True]
    assert {path.name for path in output.iterdir()} == {"original", "document.json", "preview.txt"}


def test_markdown_utf8_and_crlf_byte_offsets_and_list_continuation():
    raw = "# 政策\r\n\r\n- 不得退款 $24.50\r\n  除非符合条件\r\n  - 子项\r\n"
    blocks = documents.markdown_blocks(raw)
    assert [block["kind"] for block in blocks] == ["heading", "list_item", "list_item"]
    assert blocks[1]["text"] == "- 不得退款 $24.50\n  除非符合条件"
    assert blocks[2]["indent_columns"] == 2
    for block in blocks:
        loc = block["location"]
        assert raw.encode()[loc["byte_start"]:loc["byte_end_exclusive"]] == block["raw_text"].encode()


def test_rejects_hidden_or_nonmanifest_sources(tmp_path):
    secret = tmp_path / "tasks.json"
    secret.write_text("do not read")
    with pytest.raises(documents.DocumentError, match="only the pinned"):
        documents.extract(secret)
    with pytest.raises(documents.DocumentError, match="filename"):
        documents.extract("../tau/manifest.json", fixture=True)
    policy = tmp_path / "policy.md"
    policy.write_text("# A different policy")
    with pytest.raises(documents.DocumentError, match="does not match manifest"):
        documents.extract(policy)


def test_ocr_missing_tool_reports_dependency(monkeypatch, tmp_path):
    monkeypatch.setattr(documents.shutil, "which", lambda name: None)
    with pytest.raises(documents.DocumentError, match="requires pdftoppm"):
        documents.ocr_page(tmp_path / "input.pdf", 1, 612, 792)


def test_native_pdf_retains_cross_page_conditions_and_table():
    pytest.importorskip("pdfplumber")
    name = "synthetic_policy.pdf"
    _, doc = documents.extract(name, fixture=True)
    assert doc["review_status"] == "native_extracted"
    assert doc["title_candidate"] == EXPECTED["shared_policy_facts"]["title"]
    for page in EXPECTED["fixtures"][name]["pages"]:
        blocks = [block for block in doc["blocks"] if block["location"]["page"] == page["page"]]
        for fact in page["required_text"]:
            assert any(fact in block["text"] for block in blocks)
        for block in blocks:
            left, top, right, bottom = block["location"]["bbox"]
            assert 0 <= left < right and 0 <= top < bottom
    table = next(block for block in doc["blocks"] if block["kind"] == "table")
    assert table["rows"] == EXPECTED["fixtures"][name]["pages"][1]["table_cells"]
    assert table["location"]["page"] == 2
    assert len(table["cell_bboxes"]) == len(table["rows"])


def test_scanned_pdf_real_ocr_reports_observations():
    pytest.importorskip("pdfplumber")
    if not all(documents.shutil.which(tool) for tool in ("pdftoppm", "tesseract")):
        pytest.skip("OCR requires pdftoppm and tesseract")
    _, doc = documents.extract("synthetic_policy_scan.pdf", fixture=True)
    assert doc["extraction_status"] == "parsed"
    assert doc["review_status"] == "needs_review"
    for page in doc["pages"]:
        assert page["method"] == "ocr"
        assert "table_structure" in page["review_reason"]
    observations = {
        page: " ".join(b["text"] for b in doc["blocks"] if b["location"]["page"] == page)
        for page in (1, 2)
    }
    facts = EXPECTED["shared_policy_facts"]
    assert facts["negation"] in observations[1]
    assert "only if the seal is intact." in observations[1]
    assert facts["exception"] in observations[2]
    for limit in facts["credit_limits"]:
        assert f"${limit['amount']}" in observations[2]
    assert all(
        block["kind"] == "ocr_line"
        and block["evidence_kind"] == "ocr_observation"
        and block["review_status"] == "needs_review"
        for block in doc["blocks"]
    )


def test_blank_and_corrupt_pdf_do_not_report_parsed():
    pytest.importorskip("pdfplumber")
    _, blank = documents.extract("synthetic_blank.pdf", fixture=True)
    assert blank["blocks"] == []
    assert blank["extraction_status"] == "empty"
    assert blank["review_status"] == "needs_review"
    assert blank["pages"][0]["review_reason"]
    with pytest.raises(documents.DocumentError, match="PDF extraction failed"):
        documents.extract("synthetic_corrupt.pdf", fixture=True)
