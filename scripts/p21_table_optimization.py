"""Compare image-only deskew and wired-table inference on the original PNG inputs."""

import argparse
import importlib.metadata
import json
import math
import os
import platform
import time
from pathlib import Path
from types import SimpleNamespace

if __package__:
    from .p21_paddle import RUNTIME, paddle_page, sha256
else:
    from p21_paddle import RUNTIME, paddle_page, sha256


PIPELINE_CONFIG = {
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
CONFIGURATIONS = {
    "raw-wired-e2e": {"use_e2e_wired_table_rec_model": True},
    "deskew-baseline": {},
    "deskew-wired-e2e": {"use_e2e_wired_table_rec_model": True},
}


def deskew(image):
    """Estimate text/table direction with near-horizontal Hough line consensus."""
    import cv2
    import numpy as np

    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 60, 180)
    lines = cv2.HoughLinesP(
        edges, 1, np.pi / 720, threshold=max(20, width // 35),
        minLineLength=max(20, width // 3), maxLineGap=max(4, width // 75),
    )
    directions = []
    for x1, y1, x2, y2 in [] if lines is None else lines[:, 0]:
        dx, dy = int(x2) - int(x1), int(y2) - int(y1)
        angle = (math.degrees(math.atan2(dy, dx)) + 90) % 180 - 90
        if abs(angle) <= 7:
            directions.append((angle, math.hypot(dx, dy)))

    estimated = None
    if len(directions) >= 3:
        angles, lengths = np.asarray(directions).T
        consensus_weights = (np.abs(angles[:, None] - angles) <= 0.8) @ lengths
        inliers = np.abs(angles - angles[consensus_weights.argmax()]) <= 0.8
        if inliers.sum() >= 3 and lengths[inliers].sum() >= 0.60 * lengths.sum():
            estimated = float(np.average(angles[inliers], weights=lengths[inliers]))

    applied = estimated if estimated is not None and abs(estimated) >= 0.35 else 0.0
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), applied, 1.0)
    result = image
    if applied:
        radians = math.radians(applied)
        new_width = math.ceil(width * abs(math.cos(radians)) + height * abs(math.sin(radians)))
        new_height = math.ceil(height * abs(math.cos(radians)) + width * abs(math.sin(radians)))
        matrix[:, 2] += [(new_width - width) / 2, (new_height - height) / 2]
        result = cv2.warpAffine(
            image, matrix, (new_width, new_height),
            flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255),
        )
    return result, {
        "method": "near_horizontal_hough_length_weighted_consensus",
        "estimated_angle_deg": estimated,
        "applied_rotation_ccw_deg": applied,
        "line_count": len(directions),
        "status": (
            "insufficient_consensus" if estimated is None
            else "corrected" if applied else "near_level"
        ),
        "source_to_processed_affine": matrix.tolist(),
        "source_dimensions": [width, height],
        "processed_dimensions": [result.shape[1], result.shape[0]],
    }


def run(args):
    prediction_config = CONFIGURATIONS[args.configuration]
    metadata = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "config": PIPELINE_CONFIG,
        "predict_options": prediction_config,
    }
    report = {
        "engine": "paddle-structurev3",
        "configuration": args.configuration,
        "table_supported": True,
        "timing_mode": "initialized_pipeline_sequential_pages_preprocessing_timed_separately",
        "metadata": metadata,
        "pages": [],
    }
    start = time.perf_counter()
    try:
        os.environ["PADDLE_PDX_CACHE_HOME"] = str(args.runtime / "cache")
        os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "BOS")
        from paddleocr import PPStructureV3

        metadata["model_source"] = os.environ["PADDLE_PDX_MODEL_SOURCE"]
        metadata["versions"] = {
            name: importlib.metadata.version(name)
            for name in ("paddlepaddle", "paddleocr", "paddlex", "opencv-contrib-python")
        }
        pipeline = PPStructureV3(**PIPELINE_CONFIG)
        adapter = SimpleNamespace(predict=lambda path: pipeline.predict(path, **prediction_config))
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - retain third-party failures
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
            import cv2

            before = time.perf_counter()
            try:
                image = cv2.imread(str(source), cv2.IMREAD_COLOR)
                if image is None:
                    raise ValueError("unable to decode input image")
                if args.configuration == "raw-wired-e2e":
                    height, width = image.shape[:2]
                    processed = image
                    transform = {
                        "method": "identity",
                        "status": "disabled",
                        "estimated_angle_deg": None,
                        "applied_rotation_ccw_deg": 0.0,
                        "line_count": 0,
                        "source_to_processed_affine": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
                        "source_dimensions": [width, height],
                        "processed_dimensions": [width, height],
                    }
                else:
                    processed, transform = deskew(image)
                raster = source
                if transform["applied_rotation_ccw_deg"]:
                    encoded, buffer = cv2.imencode(".png", processed)
                    if not encoded:
                        raise OSError("unable to encode corrected image")
                    raster = args.out.with_suffix("") / source.name
                    raster.parent.mkdir(parents=True, exist_ok=True)
                    with raster.open("xb") as stream:
                        stream.write(buffer.tobytes())
                page["deskew"] = transform
                page["processed_image"] = str(raster)
                page["processed_sha256"] = page["raster_sha256"] = sha256(raster)
            finally:
                page["preprocess_ms"] = round((time.perf_counter() - before) * 1000, 2)
            before = time.perf_counter()
            try:
                page.update(paddle_page(adapter, raster), status="ok")
            finally:
                page["elapsed_ms"] = round((time.perf_counter() - before) * 1000, 2)
        except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - retain third-party failures
            page["error"] = f"{type(exc).__name__}: {exc}"
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
    parser.add_argument("--configuration", required=True, choices=CONFIGURATIONS)
    args = parser.parse_args()
    args.runtime = args.runtime.resolve()
    if any(path.suffix.lower() != ".png" for path in args.inputs):
        parser.error("all inputs must be PNG images")
    if len({path.stem for path in args.inputs}) != len(args.inputs):
        parser.error("input filenames must have distinct stems")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.exists():
        parser.error(f"output already exists: {args.out}")
    result = run(args)
    with args.out.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return int(
        len(result["pages"]) != len(args.inputs)
        or any(page["status"] != "ok" for page in result["pages"])
    )


if __name__ == "__main__":
    raise SystemExit(main())
