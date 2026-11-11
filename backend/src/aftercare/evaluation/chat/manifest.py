"""Private, append-only plan and receipts for the chat evaluation runner."""

import fcntl
import json
import os
from contextlib import contextmanager
from itertools import product
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from aftercare.business.rules import digest
from aftercare.investigation.graph import NO_RECHECK_ARM, SEQUENTIAL_CLAIMS_ARM, SYNTHESIS_ONLY_ARM

PROTOCOL = "chat-eval-v1"
ARMS = ("strong_single", "single_staged", "multi")
RECHECK_ABLATION = "chat-eval-multi-recheck-ablation-v1"
COUNTEREXAMPLE_ABLATION = "chat-eval-multi-counterexample-ablation-v1"
SEQUENTIAL_CLAIMS_ABLATION = "chat-eval-multi-sequential-claims-ablation-v1"


class Manifest:
    def __init__(self, directory: Path):
        self.directory = directory

    def _write(self, name, payload):
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        temporary = self.directory / f".{name}.{uuid4().hex}.pending"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, self.directory / name)
        finally:
            temporary.unlink()
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def read(self, name):
        try:
            return json.loads((self.directory / name).read_bytes())
        except FileNotFoundError:
            return None

    def prepare(self, campaign, cases, controls):
        return self._prepare(campaign, cases, controls, PROTOCOL, ARMS)

    def prepare_recheck_ablation(self, campaign, cases, controls):
        return self._prepare(campaign, cases, controls, RECHECK_ABLATION, ("multi", NO_RECHECK_ARM))

    def prepare_sequential_claims_ablation(self, campaign, cases, controls):
        return self._prepare(
            campaign, cases, controls, SEQUENTIAL_CLAIMS_ABLATION, ("multi", SEQUENTIAL_CLAIMS_ARM)
        )

    def prepare_counterexample_ablation(self, campaign, cases, controls):
        return self._prepare(
            campaign, cases, controls, COUNTEREXAMPLE_ABLATION, (NO_RECHECK_ARM, SYNTHESIS_ONLY_ARM)
        )

    def _prepare(self, campaign, cases, controls, protocol, arms):
        groups = {(item["repetition"], item["case_id"]): item for item in cases}
        if not groups or len(groups) != len(cases):
            raise ValueError("CHAT_EVAL_DUPLICATE_OR_EMPTY_CASES")
        rows = []
        for (repetition, case_id), arm in product(sorted(groups), arms):
            name = digest([protocol, campaign, repetition, case_id, arm])
            rows.append(
                {
                    "id": str(uuid5(NAMESPACE_URL, name)),
                    "repetition": repetition,
                    "case_id": case_id,
                    "arm": arm,
                    "input_sha256": groups[(repetition, case_id)]["scope_sha256"],
                }
            )
        plan = {
            "protocol": protocol,
            "campaign": campaign,
            "controls": controls,
            "cases": [groups[key] for key in sorted(groups)],
            "rows": rows,
        }
        stored = self.read("plan.json")
        if stored is None:
            self._write("plan.json", plan)
        elif stored != plan:
            raise ValueError("CHAT_EVAL_PLAN_CHANGED")
        return plan

    @contextmanager
    def lock(self):
        self.directory.mkdir(mode=0o700, exist_ok=True)
        if self.directory.is_symlink() or self.directory.stat().st_mode & 0o077:
            raise ValueError("CHAT_EVAL_PRIVATE_DIRECTORY_REQUIRED")
        fd = os.open(self.directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def started(self, row):
        return self._receipt(row, "started") is not None

    def start(self, row):
        self._write(f"{row['id']}.started.json", row)

    def ready(self, row):
        return self._receipt(row, "ready")

    def mark_ready(self, row, namespace):
        self._write(f"{row['id']}.ready.json", {**row, "namespace": namespace})

    def result(self, row):
        return self._receipt(row, "result")

    def finish(self, row, result):
        self._write(f"{row['id']}.result.json", {**result, **row})

    def _receipt(self, row, phase):
        result = self.read(f"{row['id']}.{phase}.json")
        if result is not None and any(result.get(key) != value for key, value in row.items()):
            raise ValueError("CHAT_EVAL_RECEIPT_BINDING_MISMATCH")
        return result


def bound_rows(plan):
    plan_sha256 = digest(plan)
    controls_sha256 = digest(plan["controls"])
    return [
        {**row, "plan_sha256": plan_sha256, "controls_sha256": controls_sha256}
        for row in plan["rows"]
    ]
