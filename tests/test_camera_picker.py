import unittest
from types import SimpleNamespace
from unittest import mock

from camera_picker import CameraPicker
from rtsp_carousel import CarouselController


class CameraPickerTests(unittest.TestCase):
    def setUp(self):
        self.root = mock.Mock()
        self.root.after.side_effect = lambda delay, fn: (delay, fn)
        self.closed = mock.Mock()
        self.picker = p = CameraPicker(mock.Mock(), self.root, self.closed)
        p.window = mock.Mock()
        p.window.winfo_rootx.return_value = p.window.winfo_rooty.return_value = 100
        p.window.winfo_width.return_value = p.window.winfo_height.return_value = 300
        p.canvas = mock.Mock()
        p.canvas.yview.return_value = (0, .5)
        p.content = mock.Mock()
        p.content.winfo_reqheight.return_value = 600
        p.rows = [mock.Mock(), mock.Mock()]
        for row in p.rows:
            row.winfo_rootx.return_value = row.winfo_rooty.return_value = 100
            row.winfo_width.return_value = row.winfo_height.return_value = 300

    def event(self, x=200, y=200):
        return SimpleNamespace(x_root=x, y_root=y, widget=self.picker.rows[1])

    def test_five_seconds_after_last_interaction_closes_without_selection(self):
        p = self.picker
        p._arm_timeout()
        first = p.after
        p._scroll(40)
        self.root.after_cancel.assert_called_with(first)
        self.assertEqual(p.after[0], 5000)
        p.after[1]()
        self.closed.assert_called_once_with(None)
        self.assertFalse(p.is_open)
        self.assertIsNone(p.after)

    def test_tap_selects_exact_row_and_closes(self):
        p = self.picker
        p._press(self.event())
        p._release(self.event())
        self.closed.assert_called_once_with(1)
        self.assertFalse(p.is_open)

    def test_drag_scrolls_without_selecting_even_after_returning_to_start(self):
        p = self.picker
        p._press(self.event())
        p._move(self.event(y=140))
        p.canvas.yview_moveto.assert_called_with(.1)
        p._release(self.event())
        self.closed.assert_not_called()
        self.assertTrue(p.is_open)
        self.assertEqual(p.after[0], 5000)

    def test_outside_press_dismisses_and_shutdown_does_not_notify(self):
        p = self.picker
        p._press(self.event(x=0))
        self.closed.assert_called_once_with(None)
        self.setUp()
        self.picker.close(notify=False)
        self.closed.assert_not_called()

    @mock.patch('rtsp_carousel.os.path.exists', return_value=False)
    def test_missing_snapshot_does_not_add_centered_stream_name(self, exists):
        c = object.__new__(CarouselController)
        c.root = mock.Mock()
        c.root.winfo_width.return_value = c.root.winfo_height.return_value = 400
        c.snapshots = mock.Mock()
        c.snapshot_photos = {}
        c.snapshot_labels = [mock.Mock()]
        c._raise_overlays = mock.Mock()
        c._render_page_snapshot(0)
        c.snapshot_labels[0].configure.assert_called_once_with(
            image='', text='', background='black')
