"""Real Chromium/MCP regression checks; no stub model or transport."""

import json
import os
import unittest
from pathlib import Path

from demo import Scenario


class MisroutedScenario(Scenario):
    """Model an adapter accidentally routing B's tools to A's session."""

    async def call(self, state, actor, tool, args=None):
        return await super().call(state, "A" if actor == "B" else actor, tool, args)


class SessionsTest(unittest.IsolatedAsyncioTestCase):
    """Verify outcomes and cleanup, including when the isolation oracle fails."""

    async def run_case(self, scenario, *, misroute=False):
        output = Path(os.environ.get("EXAMPLE_TEST_OUTPUT", "artifacts/tests")) / (
            "misroute" if misroute else scenario
        )
        instance = (MisroutedScenario if misroute else Scenario)(scenario, output)
        if misroute:
            with self.assertRaises(ExceptionGroup) as captured:
                await instance.run()
            errors = []

            def collect(group):
                for error in group.exceptions:
                    if isinstance(error, BaseExceptionGroup):
                        collect(error)
                    else:
                        errors.append(error)

            collect(captured.exception)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], AssertionError)
            self.assertIn("ASSERTION_FAILED", str(errors[0]))
            self.assertIn(
                'observed: text "Note: ALPHA-SYNTHETIC | Requests: 2"', str(errors[0])
            )
            receipt = json.loads((output / "receipt.json").read_text())
        else:
            receipt = await instance.run()
        self.assertEqual(len(set(receipt["sessions"].values())), 2)
        self.assertEqual(receipt["remaining_pids"], [])
        self.assertEqual(receipt["remaining_tcp_ports"], [])
        self.assertFalse(receipt["app_port_open"])
        closed = [call for call in receipt["calls"] if call["tool"] == "close_session"]
        self.assertEqual(
            {call["arguments"]["session"] for call in closed},
            set(receipt["sessions"].values()),
        )
        self.assertTrue(all(not call["is_error"] for call in closed))
        invalid = [
            call
            for call in receipt["calls"]
            if call.get("is_error") and "NO_SESSION" in call.get("text", "")
        ]
        self.assertEqual(len(invalid), 2)
        return receipt

    async def test_normal(self):
        self.assertEqual((await self.run_case("normal"))["outcome"], "completed")

    async def test_exception(self):
        self.assertEqual(
            (await self.run_case("exception"))["outcome"], "deliberate_exception"
        )

    async def test_cancel_in_flight(self):
        receipt = await self.run_case("cancel")
        self.assertEqual(receipt["outcome"], "cancelled_in_flight")
        cancelled = [call for call in receipt["calls"] if call.get("cancelled")]
        self.assertEqual(len(cancelled), 1)
        self.assertLess(cancelled[0]["finished"] - cancelled[0]["started"], 5)

    async def test_misrouting_is_detected_and_both_sessions_close(self):
        await self.run_case("normal", misroute=True)
