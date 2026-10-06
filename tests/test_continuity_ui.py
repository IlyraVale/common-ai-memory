"""Continuity pages in the unified UI: 动态 / 断点 / 快照 and the read-only Relay panel."""
from __future__ import annotations

import json
import re

from handoffs import HandoffStore
from snapshots import SnapshotManager
from test_memory_ui import UiBase, snapshot


class ContinuityUiTests(UiBase):
    def test_shell_lists_the_new_pages(self) -> None:
        text = self.request("GET", "/")[1].decode("utf-8")
        for label, page in (("动态", "timeline"), ("断点", "handoffs"), ("快照", "snapshots")):
            self.assertIn(f'href="#{page}" data-page="{page}">{label}</a>', text)
            self.assertIn(f'{page}: "/{page}"', text)

    def test_pages_render_with_nonce_csp_and_no_auto_chat(self) -> None:
        for page in ("timeline", "handoffs", "snapshots"):
            status, body, headers = self.request("GET", f"/{page}")
            html = body.decode("utf-8")
            self.assertEqual(status, 200)
            self.assertIn(f'data-view="{page}"', html)
            nonce = re.search(r"script-src 'nonce-([^']+)'", headers["Content-Security-Policy"]).group(1)
            self.assertIn(f'<script nonce="{nonce}">', html)
            self.assertIn('id="cam-theme-core"', html)
            self.assertNotIn("自动聊天", html)
            self.assertNotIn("innerHTML", html)
        timeline_html = self.request("GET", "/timeline")[1].decode("utf-8")
        for label in ("今天", "7 天", "30 天", "全部类型"):
            self.assertIn(label, timeline_html)
        self.assertIn("消耗它的额度", timeline_html)  # enabling relay warns about CLI quota

    def test_api_needs_ui_header_and_is_get_only(self) -> None:
        self.assertEqual(self.request("GET", "/api/continuity/changes")[0], 403)
        self.assertEqual(self.request("POST", "/api/continuity/snapshots", {}, ui=True)[0], 404)
        self.assertEqual(self.request("GET", "/api/continuity/snapshots/x/restore", ui=True)[0], 404)

    def test_changes_ranges_and_kind_filter(self) -> None:
        status, data, _ = self.request("GET", "/api/continuity/changes?range=today", ui=True)
        self.assertEqual(status, 200)
        self.assertEqual(data["counts"].get("memory.created"), 3)
        self.assertNotIn("冰美式", json.dumps(data, ensure_ascii=False))
        self.assertEqual(self.request("GET", "/api/continuity/changes?range=30d&kind=relay", ui=True)[1]["events"], [])
        self.assertTrue(self.request("GET", "/api/continuity/changes?range=7d&kind=bogus", ui=True)[1]["ok"])

    def test_handoffs_list_and_detail(self) -> None:
        self.assertEqual(self.request("GET", "/api/continuity/handoffs", ui=True)[1]["handoffs"], [])
        self.assertFalse((self.root / "state" / "handoffs.sqlite3").exists())  # viewing creates nothing
        hid = HandoffStore(self.root, "claude").set(topic="UI 收尾", summary="全文摘要")["handoff"]["handoff_id"]
        listed = self.request("GET", "/api/continuity/handoffs", ui=True)[1]["handoffs"]
        self.assertEqual([(h["owner"], h["topic"]) for h in listed], [("claude", "UI 收尾")])
        self.assertNotIn("summary", listed[0])
        detail = self.request("GET", f"/api/continuity/handoffs/{hid}", ui=True)[1]["handoff"]
        self.assertEqual(detail["summary"], "全文摘要")
        self.assertEqual(self.request("GET", "/api/continuity/handoffs/nope", ui=True)[0], 404)

    def test_snapshots_are_view_only(self) -> None:
        snap = SnapshotManager(self.root).create(label="ui")["snapshot_id"]
        before = snapshot(self.root)
        listed = self.request("GET", "/api/continuity/snapshots", ui=True)[1]["snapshots"]
        self.assertEqual(listed[0]["snapshot_id"], snap)
        self.assertTrue(self.request("GET", f"/api/continuity/snapshots/{snap}/verify", ui=True)[1]["ok"])
        plan = self.request("GET", f"/api/continuity/snapshots/{snap}/plan", ui=True)[1]
        self.assertEqual(set(plan["add"]), {"count", "first"})
        self.assertNotIn("restore_token", plan)
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse((self.root / "state" / "snapshot-restore-tokens.json").exists())
        self.assertEqual(self.request("GET", "/api/continuity/snapshots/..%2Fx/verify", ui=True)[0], 404)

    def test_relay_panel_is_status_only_and_off(self) -> None:
        data = self.request("GET", "/api/continuity/relay", ui=True)[1]
        self.assertTrue(data["ok"])
        self.assertTrue(all(o["enabled"] is False for o in data["owners"]))
        self.assertFalse((self.root / "state" / "relay.sqlite3").exists())


if __name__ == "__main__":
    import unittest

    unittest.main()
