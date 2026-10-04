import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from lounge_room import MAX_DELIVERED_RANGES, LoungeRoom
from lounge_viewer import Handler
from http.server import ThreadingHTTPServer


class LoungeTests(unittest.TestCase):
    def test_three_identities_share_the_room(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for agent in ("gpt", "claude", "alice"):
                result = LoungeRoom(root, agent).action("main", f"say :: from {agent}")
                self.assertTrue(result["ok"])
                self.assertEqual(result["message"]["author"], agent)
            status = LoungeRoom(root, "gpt").status()
            self.assertEqual([row["author"] for row in status["recent"]], ["gpt", "claude", "alice"])
            self.assertTrue(all(re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", row["time"]) for row in status["recent"]))
            self.assertIn("alice", json.loads((root / ".lounge/state.json").read_text())["readers"])

    def test_targeted_inbox_routes_without_exposing_to_other_agents(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            gpt = LoungeRoom(root, "gpt")
            claude = LoungeRoom(root, "claude")
            alice = LoungeRoom(root, "alice")

            sent = gpt.send("claude", "private hello")
            self.assertTrue(sent["ok"])
            self.assertEqual(sent["message"]["to"], "claude")

            claude_box = claude.inbox(mark_read=False)
            self.assertEqual(claude_box["unread_count"], 1)
            self.assertEqual(claude_box["messages"][0]["text"], "private hello")
            self.assertEqual(alice.inbox(mark_read=False)["unread_count"], 0)
            self.assertNotIn("private hello", [row["text"] for row in alice.status(mark_read=False)["recent"]])

            ack = claude.acknowledge(claude_box["messages"][0]["seq"])
            self.assertTrue(ack["ok"])
            self.assertEqual(claude.inbox(mark_read=False)["unread_count"], 0)

            broadcast = alice.send("all", "broadcast hello")
            self.assertTrue(broadcast["ok"])
            self.assertEqual(gpt.inbox(mark_read=False)["messages"][-1]["text"], "broadcast hello")
            self.assertEqual(claude.inbox(mark_read=False)["messages"][-1]["text"], "broadcast hello")

    def test_inbox_limit_marks_only_messages_actually_returned(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            alice = LoungeRoom(root, "alice")
            claude = LoungeRoom(root, "claude")
            for index in range(5):
                alice.send("claude", f"message-{index}")

            first = claude.inbox(limit=2, mark_read=True)
            self.assertEqual([row["text"] for row in first["messages"]], ["message-0", "message-1"])
            self.assertEqual(first["last_read_seq"], first["messages"][-1]["seq"])
            second = claude.inbox(limit=10, mark_read=False)
            self.assertEqual([row["text"] for row in second["messages"]], ["message-2", "message-3", "message-4"])

    def test_sending_does_not_consume_unread_and_ack_requires_delivery(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            gpt = LoungeRoom(root, "gpt")
            claude = LoungeRoom(root, "claude")
            alice = LoungeRoom(root, "alice")
            alice.send("gpt", "unread-before-send")
            sent = gpt.send("claude", "outbound")
            self.assertEqual(gpt.inbox(mark_read=False)["messages"][0]["text"], "unread-before-send")

            for index in range(50):
                alice.send("claude", f"queued-{index}")
            last_seq = claude.status(mark_read=False)["recent"][-1]["seq"]
            # A status response may expose the tail, but ack cannot skip an undisclosed gap.
            self.assertFalse(claude.acknowledge(last_seq)["ok"])
            while True:
                batch = claude.inbox(limit=10, mark_read=False)
                if not batch["messages"]:
                    break
                self.assertTrue(claude.acknowledge(batch["messages"][-1]["seq"])["ok"])
            self.assertEqual(claude.inbox(mark_read=False)["unread_count"], 0)

    def test_delivery_ranges_bound_pathological_gaps_without_losing_messages(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            alice = LoungeRoom(root, "alice")
            claude = LoungeRoom(root, "claude")
            expected = []
            for index in range(MAX_DELIVERED_RANGES + 26):
                text = f"claude-gap-{index}"
                expected.append(text)
                alice.send("claude", text)
                alice.send("gpt", f"invisible-gap-{index}")
                # Status exposes a moving tail while the earliest unread page remains unacked.
                claude.status(mark_read=False)

            for _ in range(20):
                claude.inbox(limit=60, mark_read=False)
            reader = json.loads((root / ".lounge/state.json").read_text(encoding="utf-8"))["readers"]["claude"]
            self.assertNotIn("delivered_seqs", reader)
            self.assertLessEqual(len(reader["delivered_ranges"]), MAX_DELIVERED_RANGES)

            received = []
            while True:
                batch = claude.inbox(limit=17, mark_read=False)
                if not batch["messages"]:
                    break
                received.extend(row["text"] for row in batch["messages"])
                self.assertTrue(claude.acknowledge(batch["messages"][-1]["seq"])["ok"])
            self.assertEqual(received, expected)
            self.assertEqual(claude.inbox(mark_read=False)["unread_count"], 0)

    def test_mixed_direct_broadcast_paging_and_high_ack_preserve_visibility(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            alice = LoungeRoom(root, "alice")
            gpt = LoungeRoom(root, "gpt")
            claude = LoungeRoom(root, "claude")
            first = alice.send("all", "broadcast-first")["message"]
            direct = gpt.send("claude", "gpt-private")["message"]
            alice.send("gpt", "gpt-only-gap")
            final = alice.send("all", "broadcast-final")["message"]

            page = claude.inbox(limit=2, mark_read=False)
            self.assertEqual([row["text"] for row in page["messages"]], ["broadcast-first", "gpt-private"])
            self.assertFalse(claude.acknowledge(final["seq"])["ok"])
            self.assertTrue(claude.acknowledge(direct["seq"])["ok"])
            next_page = claude.inbox(limit=1, mark_read=True)
            self.assertEqual([row["text"] for row in next_page["messages"]], ["broadcast-final"])
            self.assertEqual(next_page["last_read_seq"], final["seq"])

            self.assertEqual(
                [row["text"] for row in gpt.inbox(mark_read=False)["messages"]],
                ["broadcast-first", "gpt-only-gap", "broadcast-final"],
            )
            alice_recent = [row["text"] for row in alice.status(mark_read=False)["recent"]]
            self.assertNotIn("gpt-private", alice_recent)
            self.assertEqual(first["seq"], 1)

    def test_legacy_delivered_seqs_state_is_migrated_safely(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            alice = LoungeRoom(root, "alice")
            claude = LoungeRoom(root, "claude")
            first = alice.send("claude", "legacy-first")["message"]
            alice.send("claude", "legacy-second")
            state_path = root / ".lounge/state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["readers"]["claude"].update({
                "last_read_seq": 0,
                "delivered_seqs": [first["seq"]],
                "delivered_ranges": [["bad"], [8, 3]],
            })
            state_path.write_text(json.dumps(state), encoding="utf-8")

            self.assertTrue(claude.acknowledge(first["seq"])["ok"])
            migrated = json.loads(state_path.read_text(encoding="utf-8"))["readers"]["claude"]
            self.assertNotIn("delivered_seqs", migrated)
            self.assertEqual(migrated["delivered_ranges"], [])
            remaining = claude.inbox(mark_read=True)
            self.assertEqual([row["text"] for row in remaining["messages"]], ["legacy-second"])

    def test_viewer_serves_ui_and_posts_as_alice(self):
        with tempfile.TemporaryDirectory() as folder:
            Handler.root = Path(folder)
            server = ThreadingHTTPServer(("localhost", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://localhost:{server.server_port}"
            try:
                html = urlopen(base + "/", timeout=2).read().decode()
                for label in ("GPT", "Claude", "Alice"):
                    self.assertIn(label, html)
                for feature in ("外观设置", "localStorage", "ai-lounge-theme.json", "--bubble-opacity"):
                    self.assertIn(feature, html)
                for feature in ("全局字体", "标题字体", "聊天正文字体", "--global-font", "FONT_PRESETS"):
                    self.assertIn(feature, html)
                for feature in ("昵称字体", "优雅 ·", "手写 ·", "复古 ·", "几何 ·", "未来 ·"):
                    self.assertIn(feature, html)
                for variable in ("--gpt-name-size", "--claude-name-weight", "--alice-name-spacing"):
                    self.assertIn(variable, html)
                for feature in ("全部边框颜色", "GPT边框颜色", "Claude边框颜色", "Alice边框颜色"):
                    self.assertIn(feature, html)
                for variable in ("--bubble-border-color", "--gpt-border-color", "--claude-border-color", "--alice-border-color"):
                    self.assertIn(variable, html)
                self.assertNotIn('class="lamp"', html)
                self.assertIn("scrollbar-width:none", html)
                self.assertIn(".messages::-webkit-scrollbar{display:none", html)
                self.assertIn("overflow-y:auto", html)
                self.assertIn("readableTime(m.ts)", html)
                request = Request(
                    base + "/api/messages",
                    data=json.dumps({"text": "晚上好"}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                posted = json.loads(urlopen(request, timeout=2).read())
                self.assertEqual(posted["message"]["author"], "alice")
                state = json.loads(urlopen(base + "/api/state", timeout=2).read())
                self.assertEqual(state["messages"][-1]["text"], "晚上好")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()


