"""Tests for the frozen denominator, shared budget and official scorer boundary."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from hashlib import sha256
from importlib.util import find_spec
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_tau_official_campaign import SOURCE, initialize_official, load_official_source, run_row
from run_tau_official_campaign import select_repairs
from tau_official_budget import AGENT, REVIEW, USER, Budget, BudgetExhausted, RowTransport
from tau_official_summary import ARCHITECTURES, call_receipts, combined_summary, read_json, summarize
from tau_official_summary import write_json

TRANSPORT_READY = all(find_spec(package) is not None for package in ("litellm", "tiktoken"))
LIMITS = {
    "max_agent_calls_per_run": 24, "max_user_calls_per_run": 24, "max_review_calls_per_run": 1,
    "max_output_tokens_per_call": 1500, "timeout_seconds_per_run": 900, "max_steps": 100,
}


def fake_response(content):
    return SimpleNamespace(
        id="mock", status="completed", output=[], output_text=content,
        usage=SimpleNamespace(input_tokens=10, output_tokens=2,
                              input_tokens_details=SimpleNamespace(cached_tokens=0)),
    )


class OfficialCampaignTests(unittest.TestCase):
    def test_campaign_reservation_is_atomic(self):
        with TemporaryDirectory() as tmp:
            budget = Budget(3, "0.20", Path(tmp) / "calls.jsonl")
            barrier = Barrier(10)

            def reserve():
                barrier.wait()
                try:
                    return budget.reserve("row", "agent", "agent_response", Decimal("0.09"))
                except BudgetExhausted:
                    return None

            with ThreadPoolExecutor(10) as pool:
                results = list(pool.map(lambda _index: reserve(), range(10)))
            self.assertEqual(sorted(value for value in results if value is not None), [1, 2])
            self.assertEqual(budget.committed, Decimal("0.18"))

    @unittest.skipUnless(TRANSPORT_READY, "Requires the official transport dependencies")
    def test_same_row_parallel_roles_share_agent_limit(self):
        with TemporaryDirectory() as tmp:
            budget = Budget(10, 3, Path(tmp) / "calls.jsonl")
            barrier = Barrier(2)

            def provider(**_kwargs):
                return fake_response("mock")

            row = RowTransport("row", {**LIMITS, "max_agent_calls_per_run": 1}, budget,
                               SimpleNamespace(responses=SimpleNamespace(create=provider)))

            def request(_index):
                barrier.wait()
                try:
                    return row.completion(model=AGENT, messages=[{"role": "user", "content": "hi"}],
                                          tools=None, tool_choice=None)
                except BudgetExhausted:
                    return None

            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(request, range(2)))
            self.assertEqual(sum(result is not None for result in results), 1)
            self.assertEqual(row.metrics()["agent_calls"], 1)
            self.assertEqual(row.failure_status, "budget_exhausted")

    def test_summary_preserves_missing_error_and_unsettled_call(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "rows").mkdir()
            schedule = [{"task_id": "5", "architecture": arch, "seed": 42}
                        for arch in ARCHITECTURES]
            write_json(directory / "manifest.json", {"schedule": schedule})
            for item, status, reward in zip(schedule, ("error", "running"), (0, None)):
                row = {
                    **item, "status": status, "official_reward": reward, "duration_seconds": 1,
                    **{f"{role}_calls": 0 for role in ("agent", "user", "review")},
                    **{f"{role}_cost_usd": 0 for role in ("agent", "user", "review")},
                }
                write_json(directory / "rows" / f"5-{item['architecture']}.json", row)
            budget = Budget(3, 3, directory / "calls.jsonl")
            budget.reserve("5-staged_single", "agent", "agent_response", Decimal("0.15"))
            live = summarize(directory)
            self.assertEqual([row["status"] for row in live["rows"]],
                             ["error", "running", "not_run"])
            result = summarize(directory, mark_interrupted=True)
            self.assertEqual([row["status"] for row in result["rows"]],
                             ["error", "unknown", "not_run"])
            self.assertEqual([group["scheduled"] for group in result["architectures"]], [1, 1, 1])
            self.assertEqual([group["pass_rate"] for group in result["architectures"]], [0, 0, 0])
            self.assertFalse(result["complete"])
            self.assertEqual(result["billing"]["unsettled_reserved_usd"], 0.15)
            self.assertEqual(result["rows"][1]["agent_calls"], 1)
            self.assertIsNone(result["rows"][1]["agent_cost_usd"])

    def test_repair_selects_only_unscored_technical_failures(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp)
            (source / "rows").mkdir()
            cases = [("completed", 0), ("error", 0), ("unknown", None), ("error", None),
                     ("budget_exhausted", None), ("timeout", None)]
            schedule = []
            for index, (status, reward) in enumerate(cases):
                item = {"task_id": str(index), "architecture": "strong_single", "seed": index}
                schedule.append(item)
                write_json(source / "rows" / f"{index}-strong_single.json", {
                    **item, "status": status, "official_reward": reward,
                })
            eligible, original = select_repairs(source, {"schedule": schedule})
            self.assertEqual([item["task_id"] for item in eligible], ["2", "3"])
            self.assertEqual(set(original), {"2-strong_single", "3-strong_single"})
            self.assertEqual(read_json(source / "rows" / "1-strong_single.json")["official_reward"], 0)

    def test_combined_retains_original_failure_and_unsettled_reservation(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "original"
            repair = Path(tmp) / "original.repair"
            for directory in (source, repair):
                (directory / "rows").mkdir(parents=True)
            schedule = [{"task_id": "5", "architecture": arch, "seed": 42}
                        for arch in ARCHITECTURES]
            common = {"duration_seconds": 1,
                      **{f"{role}_calls": 1 for role in ("agent", "user", "review")},
                      **{f"{role}_cost_usd": 0 for role in ("agent", "user", "review")}}
            write_json(source / "manifest.json", {
                "schedule": schedule, "budget": {"max_calls": 100, "max_usd": 30},
            })
            for item in schedule:
                write_json(source / "rows" / f"5-{item['architecture']}.json", {
                    **item, **common, "status": "completed", "official_reward": 0,
                })
            path = source / "rows" / "5-strong_single.json"
            write_json(path, {**schedule[0], **common, "status": "unknown",
                              "official_reward": None, "agent_cost_usd": None})
            original_hash = sha256(path.read_bytes()).hexdigest()
            Budget(100, 30, source / "calls.jsonl").reserve(
                "5-strong_single", "agent", "agent_response", Decimal("0.15")
            )
            write_json(repair / "manifest.json", {
                "schedule": schedule[:1], "budget": {"max_calls": 10, "max_usd": 3},
                "repair_of": {"manifest_sha256":
                              sha256((source / "manifest.json").read_bytes()).hexdigest()},
                "original_attempts": {"5-strong_single": {"row_sha256": original_hash}},
            })
            write_json(repair / "rows" / "5-strong_single.json", {
                **schedule[0], **common, "status": "completed", "official_reward": 1,
            })
            result = combined_summary(source)
            row = result["rows"][0]
            self.assertEqual(row["official_reward"], 1)
            self.assertEqual(row["original_attempt"]["status"], "unknown")
            self.assertEqual(row["attempt_count"], 2)
            self.assertIsNone(row["agent_cost_usd"])
            self.assertTrue(result["all_scored"])
            self.assertEqual(result["budget"]["max_usd"], 33)
            self.assertEqual(result["billing"]["unsettled_reserved_usd"], 0.15)
            self.assertEqual(sha256(path.read_bytes()).hexdigest(), original_hash)

    def test_provider_failure_metadata_preserves_reservation_without_payload(self):
        class FakeHTTPError(Exception):
            status_code = 503

        with TemporaryDirectory() as tmp:
            journal = Path(tmp) / "calls.jsonl"
            budget = Budget(10, 3, journal)
            call_id = budget.reserve("row", "agent", "agent_response", Decimal("0.15"))
            budget.record_failure(call_id, FakeHTTPError("private payload"))
            reservations, settlements = call_receipts(journal)
            self.assertEqual(set(reservations), {call_id})
            self.assertEqual(settlements, {})
            self.assertNotIn("private payload", journal.read_text())
            self.assertIn('"status_code": 503', journal.read_text())

    def test_public_summary_uses_utterance_and_projected_failure_flags(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "rows").mkdir()
            (directory / "official").mkdir()
            schedule = [{"task_id": "5", "architecture": arch, "seed": 42}
                        for arch in ARCHITECTURES]
            write_json(directory / "manifest.json", {"schedule": schedule})
            write_json(directory / "rows" / "5-strong_single.json", {
                **schedule[0], "status": "completed", "official_reward": 0,
                "official_components": {"DB": 0, "NL_ASSERTION": 0}, "duration_seconds": 1,
                **{f"{role}_calls": 0 for role in ("agent", "user", "review")},
                **{f"{role}_cost_usd": 0 for role in ("agent", "user", "review")},
            })
            write_json(directory / "official" / "5-strong_single.json", {
                "messages": [{"role": "system", "content": "hidden"},
                             {"role": "user", "content": "I need a refund."},
                             {"role": "user", "content": "###OUT-OF-SCOPE###"}],
                "reward_info": {"private_reference": "hidden"},
            })
            row = summarize(directory)["rows"][0]
            self.assertEqual(row["task_summary"], "I need a refund.")
            self.assertEqual(row["failure_types"],
                             ["user_out_of_scope", "db_mismatch", "nl_assertion_failed"])
            self.assertNotIn("hidden", str(row))

    @unittest.skipUnless(TRANSPORT_READY and SOURCE.is_dir(), "Requires the pinned official environment")
    def test_official_scoring_calls_the_pinned_nl_judge_when_required(self):
        load_official_source()
        initialize_official()
        from tau2.runner import get_tasks

        task = get_tasks("retail", task_split_name="test", task_ids=["36"])[0]
        for architecture in ARCHITECTURES:
            with self.subTest(architecture=architecture), TemporaryDirectory() as tmp:
                directory = Path(tmp)
                (directory / "rows").mkdir()
                (directory / "official").mkdir()
                user_turns = iter(["I need to exchange an item.", "###STOP###"])

                def provider(model, **_kwargs):
                    if model == USER:
                        return fake_response(next(user_turns))
                    if model == REVIEW:
                        return fake_response(
                            '{"results":[{"expectedOutcome":"fixture","reasoning":"mock",'
                            '"metExpectation":false}]}'
                        )
                    return fake_response("I can help with that.")

                row = {"task_id": "36", "architecture": architecture, "seed": 42}
                budget = Budget(147, 3, directory / "calls.jsonl")
                client = SimpleNamespace(responses=SimpleNamespace(create=provider))
                run_row(row, task.model_copy(deep=True), LIMITS, budget, client, directory)
                result = read_json(directory / "rows" / f"36-{architecture}.json")
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["official_reward"], 0)
                self.assertEqual(result["official_components"]["NL_ASSERTION"], 0)
                self.assertEqual(result["review_calls"], 1)
                self.assertGreater(result["review_cost_usd"], 0)


if __name__ == "__main__":
    unittest.main()
