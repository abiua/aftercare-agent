"""Bounded localhost HTTP read measurements; never starts an experiment."""

import hashlib
import json
import math
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8022/api/evaluation/experiments/3139c3ce-9f15-4f9e-895a-1c481bdc5f40"
OUTPUT = ROOT / "reports/http-read-load.json"


def main():
    if OUTPUT.exists():
        raise SystemExit("Report already exists; preserve the original measurement.")
    accounts = json.loads((ROOT / ".runtime/users-test.json").read_text())
    token = next(row["token"] for row in accounts if row["role"] == "tester")

    def read(index):
        started = time.perf_counter()
        status, digest, error = None, None, None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(URL, headers={"Authorization": "Bearer " + token})
        try:
            with opener.open(request, timeout=10) as response:
                status = response.status
                value = json.load(response)
                digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        except urllib.error.HTTPError as failure:
            status, error = failure.code, "HTTP_ERROR"
        except (OSError, ValueError):
            error = "TRANSPORT_OR_JSON_ERROR"
        return {"index": index, "status": status, "body_sha256": digest, "error": error,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}

    before = read(-1)
    if before["status"] != 200:
        raise SystemExit("Existing test report unavailable; no measurement started.")
    groups = []
    for concurrency in (1, 4):
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            samples = list(pool.map(read, range(40)))
        wall = time.perf_counter() - started
        times = sorted(row["elapsed_ms"] for row in samples)
        groups.append({"concurrency": concurrency, "requests": len(samples),
                       "successes": sum(row["status"] == 200 for row in samples),
                       "p50_ms": times[math.ceil(len(times) * .5) - 1],
                       "p95_ms": times[math.ceil(len(times) * .95) - 1],
                       "wall_seconds": wall, "requests_per_second": len(samples) / wall,
                       "samples": samples})
    after = read(-2)
    samples = [before, after, *(sample for group in groups for sample in group["samples"])]
    unchanged = all(row["status"] == 200 and row["body_sha256"] == before["body_sha256"]
                    for row in samples)
    report = {"status": "PASS_BOUNDED_READ_MEASUREMENT" if unchanged else "FAILED",
              "endpoint": URL, "method": "GET", "total_requests": len(samples),
              "groups": groups, "before": before, "after": after,
              "responses_unchanged": unchanged, "new_experiments": 0, "model_calls": 0,
              "scope": "Warm local test API, existing six-result fixture report. Fixed order "
                       "concurrency 1 then 4. Includes HTTP/JSON time; no browser, external "
                       "network or production SLA claim. Percentiles use nearest rank."}
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "total_requests": len(samples),
                      "groups": [{k: v for k, v in group.items() if k != "samples"}
                                 for group in groups]}))
    raise SystemExit(0 if unchanged else 1)


if __name__ == "__main__":
    main()
