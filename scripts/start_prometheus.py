"""Run the local Prometheus server beside the host API; keep one TSDB per runtime."""

import argparse
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {"live": (18225, 19095), "app": (8020, 19090), "test": (8022, 19092)}


def executable():
    cached = Path.home() / ".cache/aftercare-agent/monitoring/prometheus"
    return shutil.which("prometheus") or (str(cached) if os.access(cached, os.X_OK) else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", choices=TARGETS)
    runtime = parser.parse_args().runtime
    binary = executable()
    if not binary:
        parser.exit(1, "请先安装 Prometheus 并将 prometheus 加入 PATH。\n")
    target, port = TARGETS[runtime]
    directory = ROOT / "reports/monitoring" / runtime
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    config = directory / "prometheus.yml"
    config.write_text(
        (ROOT / "infra/monitoring/prometheus.yml")
        .read_text()
        .replace("runtime-api:8000", f"127.0.0.1:{target}")
    )
    os.execv(
        binary,
        [
            binary,
            f"--config.file={config}",
            f"--web.listen-address=127.0.0.1:{port}",
            f"--storage.tsdb.path={directory / 'data'}",
            "--storage.tsdb.retention.time=15d",
        ],
    )


if __name__ == "__main__":
    main()
