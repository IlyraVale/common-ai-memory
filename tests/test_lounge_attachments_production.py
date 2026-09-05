from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from lounge_attachments import LoungeAttachmentError, LoungeAttachmentStore
from lounge_room import LoungeRoom


class LoungeAttachmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixtures = self.root / "fixtures"
        self.fixtures.mkdir()
        self.ffmpeg = shutil.which("ffmpeg")
        if not self.ffmpeg:
            self.skipTest("ffmpeg is required")
        commands = {
            "sample.png": ["-f", "lavfi", "-i", "color=c=blue:s=96x64:d=0.1", "-frames:v", "1"],
            "sample.jpg": ["-f", "lavfi", "-i", "color=c=yellow:s=96x64:d=0.1", "-frames:v", "1"],
            "transparent.webp": ["-f", "lavfi", "-i", "color=c=red@0.35:s=96x64:d=0.1,format=rgba", "-frames:v", "1", "-lossless", "1"],
            "animated.gif": ["-f", "lavfi", "-i", "testsrc2=s=96x64:r=4:d=2", "-loop", "0"],
            "animated.apng": ["-f", "lavfi", "-i", "testsrc2=s=96x64:r=4:d=2", "-plays", "0", "-f", "apng"],
        }
        for name, args in commands.items():
            result = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args, str(self.fixtures / name)],
                capture_output=True,
                check=False,
            )
            if result.returncode:
                self.fail(result.stderr.decode("utf-8", "replace"))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_all_formats_storage_message_and_mcp_content(self) -> None:
        store = LoungeAttachmentStore(self.root)
        specs = [
            ("sample.png", "image/png", False),
            ("sample.jpg", "image/jpeg", False),
            ("transparent.webp", "image/webp", False),
            ("animated.gif", "image/gif", True),
            ("animated.apng", "image/apng", True),
        ]
        metadata = []
        for name, mime, animated in specs:
            item = store.save(name, mime, (self.fixtures / name).read_bytes())
            self.assertRegex(item["id"], r"^[0-9a-f]{32}$")
            self.assertEqual(item["animated"], animated)
            self.assertNotIn("data", item)
            self.assertNotIn("path", item)
            metadata.append(item)

        room = LoungeRoom(self.root, "alice")
        for item in metadata:
            posted = room.post("附件验收", [item])
            self.assertTrue(posted["ok"])
        raw_message = (self.root / ".lounge" / "messages.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("base64", raw_message.lower())
        self.assertNotIn(str(self.root), raw_message)

        for item in metadata:
            contents = store.image_content(item["id"])
            self.assertEqual(contents[0].type, "image")
            expected_primary = "image/png" if item["mime"] == "image/apng" else item["mime"]
            self.assertEqual(contents[0].mimeType, expected_primary)
            self.assertGreater(len(contents[0].data), 20)
            self.assertEqual(len(contents), 2 if item["animated"] else 1)
            if item["animated"]:
                self.assertEqual(contents[1].mimeType, "image/jpeg")
            self.assertNotIn("image/apng", [content.mimeType for content in contents])

    def test_rejects_spoofing_scripts_and_path_traversal(self) -> None:
        store = LoungeAttachmentStore(self.root)
        png = (self.fixtures / "sample.png").read_bytes()
        with self.assertRaises(LoungeAttachmentError):
            store.save("fake.jpg", "image/jpeg", png)
        with self.assertRaises(LoungeAttachmentError):
            store.save("attack.svg", "image/svg+xml", b"<svg><script/></svg>")
        with self.assertRaises(LoungeAttachmentError):
            store.save("attack.html", "text/html", b"<html><script></script></html>")
        with self.assertRaises(LoungeAttachmentError):
            store.get_metadata("../../server-claude-public")


if __name__ == "__main__":
    unittest.main()
