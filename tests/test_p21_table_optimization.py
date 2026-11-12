import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.p21_table_optimization import CONFIGURATIONS, deskew, run, sha256

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")


def ruled_image(angle):
    image = np.full((480, 640, 3), 255, dtype=np.uint8)
    for y in (70, 130, 190, 250, 310, 370):
        cv2.line(image, (55, y), (585, y), (0, 0, 0), 2)
    matrix = cv2.getRotationMatrix2D((320, 240), angle, 1.0)
    return cv2.warpAffine(image, matrix, (640, 480), borderValue=(255, 255, 255))


@pytest.mark.parametrize("angle", [-4.2, -1.1, 2.8, 5.5])
def test_deskew_estimates_both_directions_and_preserves_canvas(angle):
    corrected, transform = deskew(ruled_image(angle))
    assert transform["status"] == "corrected"
    assert transform["applied_rotation_ccw_deg"] == pytest.approx(-angle, abs=0.25)
    assert corrected.shape[0] > 480 and corrected.shape[1] > 640
    width, height = transform["processed_dimensions"]
    matrix = np.asarray(transform["source_to_processed_affine"])
    corners = matrix @ np.array([[0, 640, 0, 640], [0, 0, 480, 480], [1, 1, 1, 1]])
    assert corners.min() >= -1e-9
    assert corners[0].max() <= width and corners[1].max() <= height
    _, corrected_transform = deskew(corrected)
    assert corrected_transform["applied_rotation_ccw_deg"] == 0.0


def test_horizontal_rules_and_blank_are_not_rotated():
    level = ruled_image(0)
    blank = np.full_like(level, 255)
    for image, status in [(level, "near_level"), (blank, "insufficient_consensus")]:
        result, transform = deskew(image)
        assert result is image
        assert transform["applied_rotation_ccw_deg"] == 0
        assert transform["status"] == status
        assert transform["source_to_processed_affine"] == [[1, 0, 0], [0, 1, 0]]


def test_small_short_marks_provide_no_skew_evidence():
    image = np.full((480, 640, 3), 255, dtype=np.uint8)
    for row in (50, 150, 250, 350):
        for col in (60, 200, 340, 480):
            cv2.line(image, (col, row), (col + 50, row + 4), (0, 0, 0), 2)
    output, transform = deskew(image)
    assert output is image
    assert transform["estimated_angle_deg"] is None
    assert transform["status"] == "insufficient_consensus"


def test_only_wired_table_prediction_option_varies():
    assert CONFIGURATIONS["deskew-baseline"] == {}
    assert CONFIGURATIONS["raw-wired-e2e"] == CONFIGURATIONS["deskew-wired-e2e"]
    assert CONFIGURATIONS["raw-wired-e2e"] == {"use_e2e_wired_table_rec_model": True}


def test_run_persists_the_exact_processed_file_and_retains_failed_pages(tmp_path, monkeypatch):
    tilted = tmp_path / "tilted.png"
    assert cv2.imwrite(str(tilted), ruled_image(3.0))
    broken = tmp_path / "broken.png"
    broken.write_text("invalid image")
    output = tmp_path / "capture.json"
    model_inputs = []

    class FakeStructure:
        def __init__(self, **_config):
            pass

        def predict(self, path, **options):
            assert options == {"use_e2e_wired_table_rec_model": True}
            model_inputs.append((Path(path), sha256(Path(path))))
            return [SimpleNamespace(json={"res": {
                "overall_ocr_res": {"rec_texts": ["fixture"]},
            }})]

    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(PPStructureV3=FakeStructure))
    monkeypatch.setenv("PADDLE_PDX_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("PADDLE_PDX_MODEL_SOURCE", "BOS")
    monkeypatch.setattr("scripts.p21_table_optimization.importlib.metadata.version", lambda _: "fixture")
    report = run(SimpleNamespace(
        configuration="deskew-wired-e2e",
        runtime=tmp_path,
        out=output,
        inputs=[tilted, broken],
    ))

    good, failed = report["pages"]
    assert good["status"] == "ok"
    assert good["input_sha256"] == sha256(tilted)
    assert good["input_sha256"] != good["processed_sha256"]
    persisted = Path(good["processed_image"])
    assert model_inputs == [(persisted, good["processed_sha256"])]
    assert sha256(persisted) == good["processed_sha256"]
    assert failed["status"] == "failed"
    assert failed["input_sha256"] == sha256(broken)
    assert "unable to decode input image" in failed["error"]
