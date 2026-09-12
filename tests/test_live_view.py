"""Regression coverage for the active layout used to validate live captures."""
import threading
import unittest
from unittest import mock

from display_server import DisplayManager


class LiveLayoutTests(unittest.TestCase):
    def manager(self):
        manager = object.__new__(DisplayManager)
        manager.lock = threading.RLock()
        manager.screen_w, manager.screen_h = 1080, 1920
        manager.panes = {}
        manager._current_layout = [
            {"name": "camera", "type": "web", "url": "https://example.com"},
            {"name": "clock", "type": "clock"},
        ]
        manager._screens_doc = {
            "screens": [{"name": "Draft", "panes": []}], "playingIndex": 0,
        }
        return manager

    def test_status_reports_applied_layout_instead_of_saved_draft(self):
        manager = self.manager()
        with mock.patch.object(manager, "_system_stats", return_value={}):
            status = manager.status()
        self.assertEqual(status["layout"], manager._current_layout)
        # A status response is a snapshot, even if a panel is edited afterward.
        manager._current_layout[0]["url"] = "https://example.com/changed"
        self.assertEqual(status["layout"][0]["url"], "https://example.com")

    def test_replacing_panel_preserves_design_order(self):
        manager = self.manager()
        replacement = {"name": "camera", "type": "web", "url": "https://example.com/new"}
        with mock.patch.object(manager, "_add_pane"), mock.patch.object(manager, "_save_layout"):
            manager.add_pane(replacement)
        self.assertEqual([p["name"] for p in manager._current_layout], ["camera", "clock"])
        self.assertEqual(manager._current_layout[0], replacement)

    def test_new_panel_is_appended(self):
        manager = self.manager()
        new_panel = {"name": "stats", "type": "stats"}
        with mock.patch.object(manager, "_add_pane"), mock.patch.object(manager, "_save_layout"):
            manager.add_pane(new_panel)
        self.assertEqual([p["name"] for p in manager._current_layout], ["camera", "clock", "stats"])


if __name__ == "__main__":
    unittest.main()
