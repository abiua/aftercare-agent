"""Compare current formulas with an independently captured, pre-change HTTP report.

Reads a local saved report only. Does not connect to services or create experiments.
This checks this report's observations, not every metric boundary or live API wiring.
"""

import argparse
import hashlib
import json
from pathlib import Path

from aftercare.evaluation.service import _counts, _metrics
from aftercare.evaluation.scenarios import Evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.baseline.read_bytes()
    original = json.loads(raw)
    rows = original["results"]
    counts = _counts(rows)
    evidence = Evidence()
    current = _metrics(rows, counts, original["config"]["source"], evidence=evidence)
    previous = {item["key"]: item for item in original["metrics"]}
    computed = {item["key"]: item for item in current}
    differences = []
    if previous.keys() != computed.keys():
        differences.append({"kind": "METRIC_KEYS_CHANGED"})
    # Added metadata may be useful; all previously recorded fields must retain
    # their original meaning and value, including nulls, denominators and detail.
    for key, metric in previous.items():
        for field, expected in metric.items():
            observed = computed.get(key, {}).get(field)
            if observed != expected:
                differences.append({"metric": key, "field": field,
                                    "expected": expected, "actual": observed})
    report = {
        "status": "PASS_SAVED_REPORT_PARITY" if not differences else "FAIL",
        "experiment_id": original["id"],
        "baseline_sha256": hashlib.sha256(raw).hexdigest(),
        "result_count": len(rows),
        "independent_cases": len({row["case_id"] for row in rows}),
        "metric_count": len(previous),
        "differences": differences,
        "scope": "Current formulas against pre-change HTTP observations; no live API or new model call.",
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "differences"}))
    raise SystemExit(1 if differences else 0)


if __name__ == "__main__":
    main()
