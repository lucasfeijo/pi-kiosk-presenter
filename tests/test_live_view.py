"""Regression coverage for the active layout used to validate live captures."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from display_server import DisplayManager


def carousel(name="Entrance", **fields):
    return {
        "name": name,
        "type": "rtsp_carousel",
        "streams": [
            {"name": "Hall", "url": "rtsp://hall/"},
            {"name": "Gate", "url": "rtsp://gate/"},
        ],
        **fields,
    }


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

    def test_startup_migrates_and_preserves_ids_in_every_saved_screen(self):
        screens = [{"name": name, "panes": [carousel()]} for name in ("First", "Second")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "layout.json"
            path.write_text(json.dumps({"screens": screens, "playingIndex": 0}))
            with mock.patch("display_server.LAYOUT_FILE", str(path)):
                first = self.manager()

                def save_active(panes):
                    first._current_layout = panes
                    first._save_layout()

                with mock.patch.object(first, "apply_layout", side_effect=save_active) as apply:
                    first.load_saved_layout()
                saved = json.loads(path.read_text())
                self.assertEqual(apply.call_args.args[0], saved["screens"][0]["panes"])
                panels = [screen["panes"][0] for screen in saved["screens"]]
                self.assertNotEqual(panels[0]["id"], panels[1]["id"])
                self.assertNotEqual(panels[0]["streams"][0]["id"], panels[0]["streams"][1]["id"])
                fresh = self.manager()
                with mock.patch.object(fresh, "apply_layout"):
                    fresh.load_saved_layout()
                self.assertEqual(fresh._screens_doc, saved)

    def test_invalid_inactive_draft_does_not_prevent_startup(self):
        manager = self.manager()
        bad_draft = carousel(streams=[{"name": "Without URL"}])
        saved = {"screens": [
            {"name": "Draft", "panes": [bad_draft]},
            {"name": "Playing", "panes": [carousel()]},
        ], "playingIndex": 1}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "layout.json"
            path.write_text(json.dumps(saved))
            with mock.patch("display_server.LAYOUT_FILE", str(path)), \
                    mock.patch.object(manager, "apply_layout") as apply:
                manager.load_saved_layout()
        self.assertEqual(manager._screens_doc["screens"][0]["panes"], [bad_draft])
        self.assertIn("id", apply.call_args.args[0][0])

    def test_add_pane_reuses_ids_for_legacy_reordered_camera_list(self):
        manager = self.manager()
        with mock.patch.object(manager, "_add_pane"), mock.patch.object(manager, "_save_layout"):
            manager.add_pane(carousel())
            original = manager._current_layout[-1]
            updated = carousel()
            updated["streams"].reverse()
            manager.add_pane(updated)
        result = manager._current_layout[-1]
        self.assertEqual(result["id"], original["id"])
        self.assertEqual(
            [stream["id"] for stream in result["streams"]],
            [stream["id"] for stream in reversed(original["streams"])],
        )

    def test_explicit_ids_survive_panel_and_camera_renames(self):
        manager = self.manager()
        draft = carousel(id="entrance")
        draft["streams"][0]["id"] = "hall"
        draft["streams"][0]["name"] = "New name"
        draft["name"] = "Renamed panel"
        screens = [{"name": "Home", "panes": [draft]}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "layout.json"
            with mock.patch("display_server.LAYOUT_FILE", str(path)):
                manager.set_screens_doc({"screens": screens})
        saved = manager._screens_doc["screens"][0]["panes"][0]
        self.assertEqual(saved["id"], "entrance")
        self.assertEqual(saved["streams"][0]["id"], "hall")

    def test_duplicate_or_unsafe_ids_reject_layout_before_saving(self):
        manager = self.manager()
        first = {"name": "First", "panes": [carousel(id="entrance")]}
        for duplicate, error in [
            (carousel(id="entrance"), "unique across screens"),
            (carousel(id="bad/path"), "rtsp_carousel id"),
            (carousel(streams=[
                {"id": "gate", "name": "Gate", "url": "rtsp://gate/"},
                {"id": "gate", "name": "Yard", "url": "rtsp://yard/"},
            ]), "stream IDs must be unique"),
        ]:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                manager.set_screens_doc({"screens": [first, {"panes": [duplicate]}]})
        self.assertEqual(manager._screens_doc["screens"][0]["name"], "Draft")


if __name__ == "__main__":
    unittest.main()
