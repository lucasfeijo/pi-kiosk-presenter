"""AirPlay pane validation and cast-window lifecycle coverage."""

import threading
import unittest
from unittest import mock

from display_server import (
    DisplayManager,
    ManagedPane,
    find_airplay_window,
    validate_carousels_in_layout,
)


def airplay_pane(**changes):
    pane = {"name": "airplay", "type": "airplay", "device_name": "AirPlay Pi"}
    pane.update(changes)
    return pane


class StepStop:
    def __init__(self, iterations):
        self.iterations = iterations
        self.count = 0

    def is_set(self):
        return self.count >= self.iterations

    def wait(self, _seconds):
        self.count += 1
        return self.is_set()


class AirPlayTests(unittest.TestCase):
    def test_only_one_receiver_per_screen_with_printable_device_name(self):
        validate_carousels_in_layout([airplay_pane(), {"type": "web"}])
        with self.assertRaisesRegex(ValueError, "only one airplay"):
            validate_carousels_in_layout([airplay_pane(), airplay_pane(name="other")])
        with self.assertRaisesRegex(ValueError, "device_name"):
            validate_carousels_in_layout([airplay_pane(device_name="bad\nname")])

    def test_adding_second_receiver_does_not_change_current_layout(self):
        manager = object.__new__(DisplayManager)
        manager.lock = threading.RLock()
        manager._current_layout = [airplay_pane()]
        with self.assertRaisesRegex(ValueError, "only one airplay"):
            manager.add_pane(airplay_pane(name="second"))
        self.assertEqual(len(manager._current_layout), 1)

    def test_launch_selects_x11_sink_and_closes_stale_window_when_supported(self):
        manager = object.__new__(DisplayManager)
        help_result = mock.Mock(stdout="-nofreeze close stale window", stderr="")
        with mock.patch("display_server.subprocess.run", return_value=help_result), \
             mock.patch("display_server.subprocess.Popen") as popen:
            manager._launch_airplay(airplay_pane(), (0, 0, 100, 100))
        popen.assert_called_once_with(
            ["uxplay", "-n", "AirPlay Pi", "-nh", "-vs", "ximagesink", "-nofreeze"]
        )

    def test_cast_window_appears_over_layout_and_disappears_after_stop(self):
        manager = object.__new__(DisplayManager)
        manager.lock = threading.RLock()
        pane = airplay_pane(order=100)
        manager._current_layout = [{"name": "camera", "type": "rtsp"}, pane]
        proc = mock.Mock(pid=123)
        proc.poll.return_value = None
        managed = ManagedPane(name="airplay", ptype="airplay", proc=proc)
        manager.panes = {"airplay": managed}
        stop = StepStop(3)
        with mock.patch("display_server.find_airplay_window", side_effect=[None, 42, None]), \
             mock.patch("display_server.position_window") as position, \
             mock.patch("display_server.raise_window_stack") as stack:
            manager._monitor_airplay_window(pane, "airplay", managed,
                                            (10, 20, 300, 200), stop)
        position.assert_called_once_with(42, 10, 20, 300, 200, True)
        stack.assert_called_once()
        self.assertIsNone(managed.wid)

    def test_window_lookup_ignores_similarly_named_windows(self):
        def output(command, **_kwargs):
            if "search" in command:
                return "10\n11\n"
            return "AirPlay Pi Other\n" if command[-1] == "11" else "AirPlay Pi\n"

        with mock.patch("display_server.subprocess.check_output", side_effect=output):
            self.assertEqual(find_airplay_window("AirPlay Pi"), 10)


if __name__ == "__main__":
    unittest.main()
