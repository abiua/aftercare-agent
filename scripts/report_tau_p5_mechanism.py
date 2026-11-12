"""Regenerate the P5 graph mechanism report using the existing controlled regression."""

import json
import runpy
from collections import Counter
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TEST = "backend/tests/intelligence/test_chat_investigation_graph.py"
REPORT = ROOT / "reports/tau-p5-mechanism-20261112.json"
REGRESSION = "test_multi_independent_roles_and_one_local_recheck"


def main():
    namespace = runpy.run_path(str(ROOT / TEST))
    regression = namespace[REGRESSION]
    run = regression.__globals__["_run"]
    captured = {}

    def capture(value, path, script, **kwargs):
        state, events = run(value, path, script, **kwargs)
        captured.update(state=state, events=events, model_calls=script.calls)
        return state, events

    with patch.dict(regression.__globals__, {"_run": capture}):
        regression(namespace["investigation"].__wrapped__(), "multi")

    state, events = captured["state"], captured["events"]
    initial_claims = [
        {"kind": kind, **payload}
        for kind, payload in events
        if kind == "investigation_claims" and payload["phase_id"] in {"fact", "policy"}
    ]
    serial_trace = [
        {"kind": kind, **payload}
        for kind, payload in events
        if kind
        in {"investigation_assessment", "investigation_recheck", "investigation_claim_revision"}
        or (
            kind in {"investigation_receipt", "investigation_claims"}
            and payload.get("phase_id") == "recheck"
        )
    ]
    roles = {
        payload["phase_id"]: payload["role"]
        for kind, payload in events
        if kind == "investigation_input"
    }
    calls = sorted(captured["model_calls"])
    phase_counts = Counter(call.split(":")[2] for call in calls)
    role_counts = Counter({role: 0 for role in roles.values()})
    for phase, count in phase_counts.items():
        role_counts[roles[phase]] += count
    report = {
        "evidence_mode": "DETERMINISTIC_MECHANISM_REGRESSION",
        "scope": "固定模型输出，实际本地冻结工具读取与内存检查点。无真实模型 API 或数据库，非真实业务验收。",
        "regression": f"{TEST}::{REGRESSION}[multi]",
        "initial_roles_parallel": True,
        "model_turn_counts_by_role": dict(role_counts),
        "model_turn_counts_by_phase": dict(phase_counts),
        "model_call_ids_sorted": calls,
        "initial_claim_events_sorted_by_role": sorted(initial_claims, key=lambda row: row["role"]),
        "serial_events": serial_trace,
        "final": {
            "status": state["status"],
            "reason": state["reason"],
            "active_claims": state["active_claims"],
            "retired_claims": state["retired_claims"],
            "stale_claim_ids": state["stale_claims"],
            "recheck_count": state["recheck_count"],
            "output": state["output"],
        },
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
