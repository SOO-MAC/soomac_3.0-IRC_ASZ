"""Offline V14 app/session/handoff regression checks. No LLM/GPU/server calls."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent

# Load corrected checkout manager under the import name expected by the app.
checkout_spec = importlib.util.spec_from_file_location(
    "checkout_manager", ROOT / "checkout_manager.py"
)
checkout = importlib.util.module_from_spec(checkout_spec)
checkout_spec.loader.exec_module(checkout)
sys.modules["checkout_manager"] = checkout


class FakeManager:
    def __init__(self):
        self.reset()

    def reset(self):
        self.state = {"intent": "unknown", "order_id": None, "items": []}
        self.pending = None


class FakeRuntime:
    instances = []

    def __init__(self):
        self.manager = FakeManager()
        self.calls = []
        self.instances.append(self)

    def _result(self):
        return {
            "llm_update": {"intent": self.manager.state["intent"], "actions": []},
            "verified_update": {"intent": self.manager.state["intent"], "actions": []},
            "state": copy.deepcopy(self.manager.state),
            "pending": self.manager.pending,
            "warnings": [],
        }

    def process(self, text):
        self.calls.append(text)

        if text == "모바일 주문 24번입니다":
            self.manager.state = {
                "intent": "mobile_pickup", "order_id": 24, "items": []
            }
            self.manager.pending = None
            return self._result()

        if text == "맥오더요":
            self.manager.state = {
                "intent": "mobile_pickup", "order_id": None, "items": []
            }
            self.manager.pending = None
            return self._result()

        if (
            self.manager.state.get("intent") == "mobile_pickup"
            and text == "65번이요"
        ):
            self.manager.state = {
                "intent": "mobile_pickup", "order_id": 65, "items": []
            }
            self.manager.pending = None
            return self._result()

        if text == "카운터 주문 완료":
            self.manager.state = {
                "intent": "confirm",
                "order_id": None,
                "items": [
                    {
                        "line_id": 1,
                        "item_type": "burger",
                        "quantity": 1,
                        "menu": "bulgogi_burger",
                        "type": "set",
                        "drink": "coke",
                        "drink_size": "large",
                        "side": "french_fries",
                        "exclude": [],
                        "add_toppings": [],
                    }
                ],
            }
            self.manager.pending = None
            return self._result()

        self.manager.state["intent"] = "unknown"
        self.manager.pending = None
        return self._result()


runtime_stub = types.ModuleType("order_runtime_final")
runtime_stub.DriveThruRuntime = FakeRuntime
sys.modules["order_runtime_final"] = runtime_stub

app_spec = importlib.util.spec_from_file_location(
    "app_under_test", ROOT / "drive_thru_app.py"
)
app = importlib.util.module_from_spec(app_spec)
app_spec.loader.exec_module(app)


class Smoke(unittest.TestCase):
    def run_app(self, inputs):
        FakeRuntime.instances.clear()
        it = iter(inputs)

        def read():
            try:
                return next(it)
            except StopIteration:
                raise EOFError

        with tempfile.TemporaryDirectory() as folder:
            manager = app.OrderHandoffManager(folder)
            output = io.StringIO()
            with (
                patch.object(app, "OrderHandoffManager", return_value=manager),
                patch.object(app, "get_customer_input", read),
                patch.object(app, "get_control_input", read),
                contextlib.redirect_stdout(output),
            ):
                app.main()

            records = [
                json.loads(p.read_text(encoding="utf-8"))
                for p in sorted(Path(folder).glob("handoff_*.json"))
            ]
            return FakeRuntime.instances[-1], output.getvalue(), records

    def test_mobile_direct_goes_through_runtime(self):
        runtime, _, records = self.run_app(
            ["/carin", "모바일 주문 24번입니다", "/quit"]
        )
        self.assertEqual(runtime.calls, ["모바일 주문 24번입니다"])
        self.assertEqual(
            records,
            [{"command": "mobile_pickup", "order_id": 1, "mobile_order_id": 24}],
        )

    def test_mobile_two_turn_goes_through_runtime(self):
        runtime, out, records = self.run_app(
            ["/carin", "맥오더요", "65번이요", "/quit"]
        )
        self.assertEqual(runtime.calls, ["맥오더요", "65번이요"])
        self.assertIn("맥오더 주문번호를 말씀해주세요.", out)
        self.assertEqual(
            records,
            [{"command": "mobile_pickup", "order_id": 1, "mobile_order_id": 65}],
        )

    def test_counter_confirm_handoff(self):
        runtime, out, records = self.run_app(
            ["/carin", "카운터 주문 완료", "/quit"]
        )
        self.assertEqual(runtime.calls, ["카운터 주문 완료"])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["command"], "counter_order")
        self.assertEqual(records[0]["order_id"], 1)
        self.assertEqual(records[0]["total_price"], 7700)
        self.assertIn("주문 금액", out)

    def test_commands_do_not_call_runtime(self):
        runtime, _, _ = self.run_app(
            ["/carin", "/state", "/order", "/reset", "/carout", "/quit"]
        )
        self.assertEqual(runtime.calls, [])

    def test_session_completion_waits_for_exit(self):
        runtime = FakeRuntime()
        session = app.VehicleSessionController(runtime)
        with contextlib.redirect_stdout(io.StringIO()):
            session.update_vehicle_signal(True)
            runtime.manager.state["items"] = [{"quantity": 1}]
            session.finish_customer_order()
            self.assertEqual(session.state, app.AppState.WAITING_FOR_EXIT)
            self.assertEqual(runtime.manager.state["items"], [])
            self.assertFalse(session.update_vehicle_signal(True))
            session.update_vehicle_signal(False)
            self.assertEqual(session.state, app.AppState.IDLE)

    def test_iced_coffee_set_upcharge(self):
        manager = app.OrderHandoffManager(first_order_id=1)
        item = {
            "line_id": 1,
            "item_type": "burger",
            "quantity": 1,
            "menu": "bulgogi_burger",
            "type": "set",
            "drink": "iced_coffee",
            "drink_size": "medium",
            "side": "french_fries",
            "exclude": [],
            "add_toppings": [],
        }
        self.assertEqual(manager.unit_price(item), 7800)

    def test_no_legacy_fast_path_or_payment_flow_wording(self):
        source = (ROOT / "drive_thru_app.py").read_text(encoding="utf-8")
        self.assertNotIn("MOBILE FAST PATH", source)
        self.assertNotIn("contains_mobile_keyword", source)
        self.assertNotIn("extract_order_id", source)
        self.assertNotIn("waiting_mobile_order_id", source)
        self.assertNotIn("앞으로 이동해서 결제해주세요", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
