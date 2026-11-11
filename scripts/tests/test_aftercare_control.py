import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "control", Path(__file__).parents[1] / "aftercare_control.py"
)
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


def container(name, labels, running=True):
    return {
        "Id": name,
        "Name": "/" + name,
        "Config": {"Labels": labels},
        "State": {"Running": running, "Paused": False},
    }


class ControlTests(unittest.TestCase):
    def test_other_project_and_forged_owner_excluded(self):
        ident = "a" * 32
        good = container(
            "aftercare-fresh-" + ident + "-pg", {"com.aftercare.fresh-bootstrap": ident}
        )
        forged = container(
            "aftercare-fresh-" + ident + "-api", {"com.aftercare.fresh-bootstrap": "b" * 32}
        )
        other = container("digital-sentinel-pg-app-1", {})
        selected, _ = c.selection([good, forged, other], "all")
        self.assertEqual(selected, [good])

    def test_all_cannot_start_historical_stacks(self):
        with self.assertRaises(c.ControlError):
            c.selection([], "all", starting=True)

    def test_shared_vm_is_not_stopped(self):
        with (
            patch.object(c, "available", return_value=True),
            patch.object(c, "inventory", return_value=[container("other", {})]),
            patch.object(c, "run") as run,
        ):
            with self.assertRaises(c.ControlError):
                c.stop_vm()
            run.assert_not_called()

    def test_empty_vm_can_stop(self):
        result = type("Result", (), {"returncode": 0})()
        with (
            patch.object(c, "available", return_value=True),
            patch.object(c, "inventory", return_value=[]),
            patch.object(c, "run", return_value=result) as run,
        ):
            c.stop_vm()
            self.assertIn(
                (["colima", "stop", "--profile", "default"],),
                [call.args for call in run.call_args_list],
            )

    def test_stop_order_preserves_storage_until_workers_end(self):
        ident = "a" * 32
        rows = [
            container(f"aftercare-fresh-{ident}-{role}", {"com.aftercare.fresh-bootstrap": ident})
            for role in ["pg", "worker", "beat", "api"]
        ]
        stopped = [{**row, "State": {"Running": False, "Paused": False}} for row in rows]
        with (
            patch.object(c, "available", return_value=True),
            patch.object(c, "inventory", side_effect=[rows, stopped]),
            patch.object(c, "docker") as docker,
        ):
            c.stop("all")
            calls = [call.args for call in docker.call_args_list]
            self.assertTrue(calls[0][-1].endswith("-beat"))
            self.assertTrue(calls[-1][-1].endswith("-pg"))
            self.assertTrue(all(call[0] == "stop" for call in calls))


if __name__ == "__main__":
    unittest.main()
