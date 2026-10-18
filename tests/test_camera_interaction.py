import ctypes
import unittest
from unittest import mock

import display_server
from rtsp_carousel import CarouselController, selection_border_rects


class HoldCycleTests(unittest.TestCase):
    def setUp(self):
        self.c = c = object.__new__(CarouselController)
        c.root = mock.Mock()
        c.root.after.side_effect = lambda delay, callback: (delay, callback)
        c.shutting_down = False
        c.pointer_held = False
        c.title_press = None
        c.picker = mock.Mock(is_open=False)
        c.swipe = mock.Mock(busy=False, tracking=False)
        c.controls_after = None
        c._dim_controls = mock.Mock()
        c.cycle_generation = 0
        c.cycle_after = None
        c.cycle_seconds = 2
        c.streams = [{}, {}]
        c._on_interaction = mock.Mock()
        c._update_selection_border = mock.Mock()
        c.navigate = mock.Mock()
        c._reset_cycle_timer()

    def test_hold_cancels_cycle_and_release_waits_full_interval(self):
        c = self.c
        stale = c.cycle_after[1]
        c._on_pointer_press()
        self.assertIsNone(c.cycle_after)
        stale()
        c.navigate.assert_not_called()
        c._reset_cycle_timer()  # manual navigation/reconfiguration during hold
        self.assertIsNone(c.cycle_after)
        c._on_pointer_release()
        self.assertEqual(c.cycle_after[0], 2000)
        stale()
        c.navigate.assert_not_called()
        c.cycle_after[1]()
        c.navigate.assert_called_once_with(1)

    def test_repeated_events_do_not_reset_release_timer(self):
        c = self.c
        c._on_pointer_press()
        c._on_pointer_press()
        c._on_pointer_release()
        timer = c.cycle_after
        c._on_pointer_release()
        self.assertIs(c.cycle_after, timer)

    def test_cycle_waits_until_swipe_animation_finishes(self):
        c = self.c
        stale = c.cycle_after[1]
        c.swipe.busy = True
        c._reset_cycle_timer()
        self.assertIsNone(c.cycle_after)
        stale()
        c.navigate.assert_not_called()
        c.swipe.busy = False
        c._reset_cycle_timer()
        self.assertEqual(c.cycle_after[0], 2000)

    def test_modal_pauses_cycle_and_closing_starts_full_interval(self):
        c = self.c
        stale = c.cycle_after[1]
        c.picker.is_open = True
        c._reset_cycle_timer()
        self.assertIsNone(c.cycle_after)
        stale()
        c.navigate.assert_not_called()
        c.picker.is_open = False
        c._apply_overlay_layout = mock.Mock()
        c._picker_closed(None)
        self.assertEqual(c.cycle_after[0], 2000)

    def test_hold_is_local_to_one_carousel(self):
        first = self.c
        self.setUp()
        second = self.c
        first._on_pointer_press()
        second.cycle_after[1]()
        second.navigate.assert_called_once_with(1)
        first.navigate.assert_not_called()

    @mock.patch('rtsp_carousel.primary_pointer_down', return_value=False)
    def test_lost_release_outside_panel_recovers(self, down):
        c = self.c
        c._on_pointer_press()
        c.last_pointer = (900, 900)
        c.root.winfo_pointerxy.return_value = (900, 900)
        c.root.winfo_rootx.return_value = c.root.winfo_rooty.return_value = 0
        c.root.winfo_width.return_value = c.root.winfo_height.return_value = 100
        c._poll_pointer()
        self.assertFalse(c.pointer_held)
        self.assertEqual(c.cycle_after[0], 2000)

    def test_disabled_cycle_and_shutdown_do_not_schedule(self):
        c = self.c
        stale = c.cycle_after[1]
        c.cycle_seconds = 0
        c._on_pointer_press()
        c._on_pointer_release()
        self.assertIsNone(c.cycle_after)
        c.shutting_down = True
        stale()
        c.navigate.assert_not_called()


class CameraWindowLockTests(unittest.TestCase):
    def test_dragging_cannot_be_reenabled_by_extra_player_args(self):
        for wid in (None, 123):
            with self.subTest(wid=wid):
                cmd = display_server.build_mpv_rtsp_command(
                    {'mpv_args': ['--window-dragging=yes']}, 'rtsp://example',
                    title='camera', wid=wid)
                self.assertEqual([s for s in cmd if s.startswith('--window-dragging=')][-1],
                                 '--window-dragging=no')
                self.assertEqual('--input-cursor-passthrough=yes' in cmd, wid is not None)

    @mock.patch('display_server._run_x11')
    def test_motif_lock_preserves_optional_title_bar(self, run):
        x11 = mock.Mock()
        run.side_effect = lambda action: action(x11, 1)
        captured = []
        x11.XChangeProperty.side_effect = lambda *args: captured.append(
            list(ctypes.cast(args[6], ctypes.POINTER(ctypes.c_ulong * 5)).contents))
        display_server.hide_x11_window_decorations(123, lock_geometry=True)
        display_server.hide_x11_window_decorations(
            123, lock_geometry=True, hide_title_bar=False)
        self.assertEqual(captured, [[3, 56, 0, 0, 0], [1, 56, 0, 0, 0]])


class SelectionBorderTests(unittest.TestCase):
    def test_ring_has_three_pixel_sides_one_pixel_gap_and_rounded_corners(self):
        pixels = {(x, y) for left, y, width, _ in selection_border_rects(100, 80)
                  for x in range(left, left + width)}
        self.assertEqual({x for x, y in pixels if y == 40}, {1, 2, 3, 96, 97, 98})
        self.assertEqual({y for x, y in pixels if x == 50}, {1, 2, 3, 76, 77, 78})
        self.assertNotIn((1, 1), pixels)
        self.assertNotIn((50, 40), pixels)
        self.assertTrue(all(1 <= x <= 98 and 1 <= y <= 78 for x, y in pixels))

    def test_tiny_panes_do_not_generate_invalid_rectangles(self):
        for size in (1, 2, 3, 5, 10):
            self.assertTrue(all(w > 0 and h > 0 for x, y, w, h in selection_border_rects(size, size)))
