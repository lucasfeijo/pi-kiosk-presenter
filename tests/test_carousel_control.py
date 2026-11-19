import json
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from carousel_control import CarouselControl
from rtsp_carousel import CarouselController


class CarouselControlSocketTests(unittest.TestCase):
    def test_client_receives_initial_and_changed_state_and_sends_command(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as runtime_dir:
            with socket.socket(socket.AF_UNIX) as probe:
                try:
                    probe.bind(str(Path(runtime_dir) / "probe.sock"))
                except PermissionError:
                    self.skipTest("environment does not allow local Unix socket listeners")
            control = CarouselControl(runtime_dir)
            control.publish({"type": "state", "camera_id": "hall"})
            control.start()
            try:
                with socket.socket(socket.AF_UNIX) as client:
                    deadline = time.monotonic() + 2
                    while True:
                        try:
                            client.connect(control.path)
                            break
                        except (FileNotFoundError, ConnectionRefusedError):
                            if time.monotonic() > deadline:
                                raise
                            time.sleep(0.01)
                    client.settimeout(2)
                    with client.makefile("rb") as events:
                        self.assertEqual(json.loads(events.readline())["camera_id"], "hall")
                        client.sendall(b'{"action":"select",')
                        client.sendall(b'"camera_id":"gate"}\n')
                        for _ in range(100):
                            pending = list(control.pending_commands())
                            if pending:
                                break
                            time.sleep(0.01)
                        self.assertEqual(pending, [{"action": "select", "camera_id": "gate"}])
                        control.publish({"type": "state", "camera_id": "gate"})
                        self.assertEqual(json.loads(events.readline())["camera_id"], "gate")
            finally:
                control.stop()


class CameraFocusTests(unittest.TestCase):
    def setUp(self):
        self.carousel = carousel = object.__new__(CarouselController)
        carousel.root = mock.Mock()
        carousel.root.after.side_effect = lambda delay, callback: (delay, callback)
        carousel.control = mock.Mock()
        carousel.focus_requests = {}
        carousel.streams = [{"id": "hall"}, {"id": "gate"}, {"id": "garage"}]
        carousel.camera_indices = {stream["id"]: index for index, stream in enumerate(carousel.streams)}
        carousel.current_index = 0
        carousel.cycle_generation = 0
        carousel.cycle_seconds = 5
        carousel.cycle_after = None
        carousel.shutting_down = False
        carousel.pointer_held = False
        carousel.swipe = mock.Mock(busy=False)
        carousel.picker = mock.Mock(is_open=False)
        carousel.video_visible = False
        carousel.snapshot_photos = {0: object()}
        carousel.select_index = mock.Mock(side_effect=self.select_index)

    def select_index(self, index):
        self.carousel.current_index = index
        self.carousel._reset_cycle_timer()
        self.carousel._publish_control_state()

    def focus_camera(self, camera, request_id):
        self.carousel._apply_control_command({
            "action": "focus", "camera_id": camera,
            "duration_seconds": 30, "request_id": request_id,
        })

    def test_repeated_focus_reuses_camera_and_restores_original_selection(self):
        carousel = self.carousel
        carousel._reset_cycle_timer()
        _, stale_cycle = carousel.cycle_after
        self.focus_camera("gate", "first-motion")
        self.assertIsNone(carousel.cycle_after)
        self.assertEqual(carousel.current_index, 1)
        stale_cycle()
        self.assertEqual(carousel.current_index, 1)
        first_focus = carousel.focus
        self.focus_camera("gate", "first-motion")
        self.assertIs(carousel.focus, first_focus)
        self.focus_camera("gate", "second-motion")
        self.assertIsNot(carousel.focus, first_focus)
        carousel.select_index.assert_called_once_with(1)
        self.focus_camera("garage", "third-motion")
        first_focus.after[1]()
        self.assertEqual(carousel.current_index, 2)
        carousel.focus.after[1]()
        self.assertIsNone(carousel.focus)
        self.assertEqual(carousel.current_index, 0)
        self.assertEqual(carousel.cycle_after[0], 5000)
        carousel.control.publish.assert_called_with({
            "type": "state", "camera_id": "hall", "picture": "snapshot", "focus_until": None,
        })

    def test_touch_at_expiry_defers_restore_and_manual_navigation_cancels_it(self):
        carousel = self.carousel
        self.focus_camera("gate", "motion")
        focus = carousel.focus
        carousel.pointer_held = True
        focus.after[1]()
        self.assertEqual(carousel.current_index, 1)
        self.focus_camera("hall", "busy-motion")
        self.assertIs(carousel.focus, focus)
        carousel.pointer_held = False
        self.focus_camera("hall", "busy-motion")
        self.assertIs(carousel.focus, focus)
        carousel.navigate(1)
        self.assertIsNone(carousel.focus)
        focus.after[1]()
        self.assertEqual(carousel.current_index, 2)

    def test_select_current_camera_clears_focus_and_resumes_the_cycle(self):
        carousel = self.carousel
        self.focus_camera("gate", "motion")
        carousel._apply_control_command({"action": "select", "camera_id": "gate"})
        self.assertIsNone(carousel.focus)
        self.assertEqual(carousel.cycle_after[0], 5000)
        carousel.select_index.assert_called_once_with(1)
