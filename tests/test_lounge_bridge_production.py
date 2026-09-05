import json
import tempfile
import unittest
from pathlib import Path

from lounge_bridge import DEFAULT_CONFIG, LoungeBridge, WakeResult
from lounge_room import LoungeRoom


def config(mode="manual"):
    value = dict(DEFAULT_CONFIG)
    value.update({
        "mode": mode,
        "cooldown_seconds": 0.01,
        "retry_base_seconds": 1.0,
        "retry_max_seconds": 4.0,
        "max_ai_rounds": 8,
        "adapters": {"gpt": {"type": "command", "command": ["fake"]}, "claude": {"type": "command", "command": ["fake"]}},
    })
    return value


class FakeWake:
    def __init__(self, outcomes=None):
        self.calls = []
        self.outcomes = list(outcomes or [])

    def __call__(self, delivery):
        self.calls.append(dict(delivery))
        ok = self.outcomes.pop(0) if self.outcomes else True
        return WakeResult(ok, "test-ok" if ok else "test-failure")


class LoungeBridgeTests(unittest.TestCase):
    def test_cold_start_ignores_existing_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            LoungeRoom(root, "alice").action("main", "say :: existing")
            wake = FakeWake()
            bridge = LoungeBridge(root, config("active"), wake)
            bridge.process_once(now=100)
            self.assertEqual(wake.calls, [])
            self.assertEqual(bridge.status()["last_scanned_seq"], 1)

    def test_active_only_wakes_both_for_alice_and_dedupes_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            wake = FakeWake()
            bridge = LoungeBridge(root, config("active"), wake)
            LoungeRoom(root, "alice").action("main", "say :: hello")
            bridge.process_once(now=100)
            self.assertEqual({x["target"] for x in wake.calls}, {"gpt", "claude"})
            self.assertEqual(bridge.status()["pending_count"], 2)
            for call in wake.calls:
                self.assertTrue(bridge.explicit_ack(call["target"], call["seq"]))
            self.assertEqual(bridge.status()["pending_count"], 0)
            second = FakeWake()
            LoungeBridge(root, config("active"), second).process_once(now=200)
            self.assertEqual(second.calls, [])

    def test_failure_is_persisted_and_retried_until_ack(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            wake = FakeWake([False, False])
            bridge = LoungeBridge(root, config("active"), wake)
            LoungeRoom(root, "alice").action("main", "say :: retry me")
            bridge.process_once(now=100)
            status = bridge.status()
            self.assertEqual(status["pending_count"], 2)
            self.assertGreaterEqual(status["metrics"]["failed_attempts"], 1)
            retry = FakeWake()
            recovered = LoungeBridge(root, config("active"), retry)
            recovered.process_once(now=200)
            self.assertEqual(recovered.status()["pending_count"], 2)
            self.assertEqual(len(retry.calls), 2)
            for call in retry.calls:
                self.assertTrue(recovered.explicit_ack(call["target"], call["seq"]))
            self.assertEqual(recovered.status()["pending_count"], 0)

    def test_ai_chat_pauses_after_eight_rounds_until_alice(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            wake = FakeWake()
            bridge = LoungeBridge(root, config("ai-chat"), wake)
            LoungeRoom(root, "alice").action("main", "say :: begin")
            bridge.process_once(now=100)
            for index in range(16):
                author = "gpt" if index % 2 == 0 else "claude"
                LoungeRoom(root, author).action("main", f"say :: ai-{index}")
                bridge.process_once(now=110 + index)
            status = bridge.status()
            self.assertTrue(status["conversation"]["paused"])
            self.assertEqual(status["conversation"]["ai_rounds"], 8)
            calls_at_pause = len(wake.calls)
            LoungeRoom(root, "gpt").action("main", "say :: should not continue")
            bridge.process_once(now=140)
            self.assertEqual(len(wake.calls), calls_at_pause)
            LoungeRoom(root, "alice").action("main", "say :: resume")
            bridge.process_once(now=150)
            self.assertFalse(bridge.status()["conversation"]["paused"])
            self.assertEqual({x["target"] for x in wake.calls[-2:]}, {"gpt", "claude"})

    def test_manual_scans_without_waking(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            wake = FakeWake()
            bridge = LoungeBridge(root, config("manual"), wake)
            LoungeRoom(root, "alice").action("main", "say :: observed")
            bridge.process_once(now=100)
            self.assertEqual(wake.calls, [])
            self.assertEqual(bridge.status()["latest_seq"], 1)

    def test_automatic_mode_fails_closed_without_adapters(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = LoungeBridge(Path(folder), dict(DEFAULT_CONFIG), FakeWake())
            with self.assertRaisesRegex(ValueError, "configured adapters"):
                bridge.set_mode("active")


if __name__ == "__main__":
    unittest.main()

