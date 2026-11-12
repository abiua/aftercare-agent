from pathlib import Path
from types import SimpleNamespace

from scripts.p21_paddle import paddle_page


def model_page(*table_html):
    raw = {
        "overall_ocr_res": {"rec_texts": ["合成文本"]},
        "table_res_list": [{"pred_html": html} for html in table_html],
    }
    pipeline = SimpleNamespace(predict=lambda _path: [SimpleNamespace(json={"res": raw})])
    return raw, paddle_page(pipeline, Path("image.png"))


def test_table_expands_rowspan_and_colspan():
    _, result = model_page(
        '<table><tr><td rowspan="2">甲</td><td colspan="2">乙</td></tr>'
        "<tr><td>丙</td><td>丁</td></tr></table>"
    )

    table = result["table"]
    assert result["table_status"] == "single_table"
    assert (table["rows"], table["columns"]) == (2, 3)
    assert table["cells"] == [
        {"row": 0, "col": 0, "rowspan": 2, "colspan": 1, "text": "甲"},
        {"row": 0, "col": 1, "rowspan": 1, "colspan": 2, "text": "乙"},
        {"row": 1, "col": 1, "rowspan": 1, "colspan": 1, "text": "丙"},
        {"row": 1, "col": 2, "rowspan": 1, "colspan": 1, "text": "丁"},
    ]


def test_multiple_tables_keep_all_predictions_without_selecting_one():
    first = "<table><tr><td>甲</td></tr></table>"
    second = "<table><tr><td>乙</td></tr></table>"
    _, result = model_page(first, second)

    assert result["table_status"] == "multiple_tables"
    assert "table" not in result
    assert [table["html"] for table in result["tables"]] == [first, second]


def test_invalid_span_preserves_model_output():
    html = '<table><tr><td rowspan="0">甲</td></tr></table>'
    raw, result = model_page(html)

    assert result["table_status"] == "invalid_structure"
    assert result["raw"] == raw
    assert result["text"] == "合成文本"
    assert result["table"] == {"html": html, "table_parse_error": "nonpositive table span"}
