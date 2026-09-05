import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from lounge_room import LoungeRoom
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


