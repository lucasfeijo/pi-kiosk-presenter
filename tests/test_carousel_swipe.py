import unittest
from unittest import mock

from carousel_swipe import SwipeGesture
from rtsp_carousel import CarouselController


class SwipeTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.queue = {}
        self.serial = 0
        root = mock.Mock()
        def after(delay, callback):
            self.serial += 1
            self.queue[self.serial] = callback
            return self.serial
        root.after.side_effect = after
        root.after_cancel.side_effect = lambda key: self.queue.pop(key, None)
        self.draw, self.finish = mock.Mock(), mock.Mock()
        self.swipe = SwipeGesture(root, lambda: 400, self.draw, self.finish, lambda: self.now)

    def settle(self):
        for _ in range(20):
            if not self.queue:
                break
            self.now += .02
            callbacks = list(self.queue.values())
            self.queue.clear()
            for callback in callbacks:
                callback()

    def test_drag_tracks_finger_and_commits_after_animation(self):
        s = self.swipe
        s.begin(300, 100)
        self.now = .5
        s.move(160, 103)
        self.draw.assert_called_with(-140)
        s.release(160, 103)
        self.assertTrue(s.busy)
        self.finish.assert_not_called()
        self.settle()
        self.finish.assert_called_once_with(1)
        self.assertFalse(s.busy)

    def test_short_drag_rolls_back_and_vertical_motion_does_not_page(self):
        s = self.swipe
        s.begin(200, 100)
        self.now = 1
        s.release(150, 100)
        self.settle()
        self.finish.assert_called_once_with(0)
        self.finish.reset_mock()
        self.draw.reset_mock()
        s.begin(200, 100)
        s.move(204, 160)
        s.release(20, 170)
        self.finish.assert_called_once_with(0)
        self.draw.assert_not_called()

    def test_quick_flick_and_direction_reversal(self):
        s = self.swipe
        s.begin(200, 100)
        self.now = .04
        s.release(240, 100)
        self.settle()
        self.finish.assert_called_once_with(-1)
        self.finish.reset_mock()
        s.begin(200, 100)
        self.now += .3
        s.move(50, 100)
        self.now += .4
        s.release(330, 100)
        self.settle()
        self.finish.assert_called_once_with(-1)

    def test_new_press_invalidates_previous_animation(self):
        s = self.swipe
        s.begin(300, 100)
        s.release(100, 100)
        stale = list(self.queue.values())[0]
        s.begin(200, 100)
        self.finish.assert_called_once_with(0)
        stale()
        self.finish.assert_called_once_with(0)
        self.assertTrue(s.tracking)


class ReleaseOpacityTests(unittest.TestCase):
    def test_release_immediately_dims_without_new_timeout(self):
        c = object.__new__(CarouselController)
        c.root = mock.Mock()
        c.shutting_down = False
        c.pointer_held = True
        c.title_press = None
        c.controls_after = 'old-timeout'
        c.control_windows = [mock.Mock(), mock.Mock()]
        c.swipe = mock.Mock(tracking=False)
        c._update_selection_border = mock.Mock()
        c._reset_cycle_timer = mock.Mock()
        c._on_pointer_release()
        self.assertEqual(c.controls_opacity, .2)
        self.assertIsNone(c.controls_after)
        c.root.after_cancel.assert_called_once_with('old-timeout')
        c.root.after.assert_not_called()
        for window in c.control_windows:
            window.attributes.assert_called_once_with('-alpha', .2)
