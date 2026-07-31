import os
import shutil
import signal
import subprocess
import tempfile
import unittest
import urllib.request
from unittest import mock

import display_server
from display_server import (
    DisplayManager,
    Handler,
    ManagedPane,
    build_mpv_rtsp_command,
    validate_carousels_in_layout,
    validate_rtsp_carousel_pane,
)
from rtsp_carousel import (
    CarouselController,
    SnapshotCache,
    split_url_credentials,
    wrapped_index,
)


def carousel_pane(**overrides):
    pane = {
        "name": "cameras",
        "type": "rtsp_carousel",
        "streams": [
            {
                "name": "Front Door",
                "url": "rtsp://front/live",
                "snapshot_url": "http://front/snapshot.jpg",
            },
            {
                "name": "Back Yard",
                "url": "rtsp://back/live",
            },
        ],
    }
    pane.update(overrides)
    return pane


class Response:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.data


class CarouselValidationTests(unittest.TestCase):
    def test_valid_shape_and_panel_seconds(self):
        validate_rtsp_carousel_pane(
            carousel_pane(
                snapshot_refresh_seconds=10,
                cycle_seconds=30,
                show_controls=True,
                show_stream_name=True,
            )
        )

    def test_snapshot_only_stream_is_valid(self):
        validate_rtsp_carousel_pane(
            carousel_pane(
                streams=[
                    {
                        "name": "Still Camera",
                        "snapshot_url": "http://camera/snapshot.jpg",
                    }
                ]
            )
        )

    def test_requires_named_streams_and_urls(self):
        for streams in (
            [],
            [{"name": "", "url": "rtsp://camera"}],
            [{"name": "Camera", "url": ""}],
        ):
            with self.subTest(streams=streams):
                with self.assertRaises(ValueError):
                    validate_rtsp_carousel_pane(carousel_pane(streams=streams))

    def test_snapshot_refresh_is_panel_level_only(self):
        pane = carousel_pane()
        pane["streams"][0]["snapshot_refresh_seconds"] = 5
        with self.assertRaisesRegex(ValueError, "belongs to"):
            validate_rtsp_carousel_pane(pane)

    def test_layout_preflight_identifies_bad_pane(self):
        with self.assertRaisesRegex(ValueError, r"pane\[1\]"):
            validate_carousels_in_layout(
                [
                    {"name": "legacy", "type": "rtsp", "url": "rtsp://one"},
                    carousel_pane(streams=[]),
                ]
            )

    def test_existing_rtsp_shape_is_unchanged(self):
        validate_carousels_in_layout(
            [{"name": "legacy", "type": "rtsp", "url": "rtsp://one"}]
        )


class CommandTests(unittest.TestCase):
    def test_embedded_mpv_command_uses_wid_and_ipc(self):
        cmd = build_mpv_rtsp_command(
            carousel_pane(fit="cover", rtsp_transport="tcp"),
            "rtsp://front/live",
            title="carousel-video",
            wid=1234,
            ipc_path="/tmp/mpv.sock",
        )
        self.assertEqual(cmd[:7], ["nice", "-n", "-10", "ionice", "-c", "1", "-n"])
        self.assertIn("--wid=1234", cmd)
        self.assertIn("--input-ipc-server=/tmp/mpv.sock", cmd)
        self.assertIn("--panscan=1.0", cmd)
        self.assertEqual(cmd.count("rtsp://front/live"), 1)

    def test_wraps_both_directions(self):
        self.assertEqual(wrapped_index(2, 2), 0)
        self.assertEqual(wrapped_index(-1, 2), 1)
        self.assertEqual(wrapped_index(0, 0), 0)


class SnapshotCacheTests(unittest.TestCase):
    def test_splits_and_decodes_embedded_url_credentials(self):
        clean_url, username, password = split_url_credentials(
            "http://admin:p%40ss@camera:8080/snapshot.jpg?channel=1"
        )

        self.assertEqual(
            clean_url, "http://camera:8080/snapshot.jpg?channel=1"
        )
        self.assertEqual(username, "admin")
        self.assertEqual(password, "p@ss")

    @mock.patch("rtsp_carousel.urllib.request.build_opener")
    def test_embedded_credentials_use_digest_and_basic_auth(self, build_opener):
        authenticated_opener = build_opener.return_value
        authenticated_opener.open.return_value = Response(b"jpeg")

        with tempfile.TemporaryDirectory() as tmp:
            cache = SnapshotCache([], tmp)
            self.assertTrue(
                cache.fetch_one(
                    0,
                    "http://admin:secret@camera/snapshot.jpg?channel=18",
                )
            )

        request = authenticated_opener.open.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://camera/snapshot.jpg?channel=18",
        )
        handlers = build_opener.call_args.args
        self.assertIsInstance(handlers[0], urllib.request.HTTPDigestAuthHandler)
        self.assertIsInstance(handlers[1], urllib.request.HTTPBasicAuthHandler)

    @mock.patch("rtsp_carousel.urllib.request.build_opener")
    def test_snapshot_failure_does_not_log_credentials(self, build_opener):
        build_opener.return_value.open.side_effect = OSError(
            "http://admin:secret@camera is offline"
        )

        with tempfile.TemporaryDirectory() as tmp:
            cache = SnapshotCache([], tmp)
            with mock.patch("rtsp_carousel.log.warning") as warning:
                self.assertFalse(
                    cache.fetch_one(
                        0,
                        "http://admin:secret@camera/snapshot.jpg",
                    )
                )

        logged = " ".join(str(value) for value in warning.call_args.args)
        self.assertNotIn("admin", logged)
        self.assertNotIn("secret", logged)
        self.assertNotIn("camera", logged)

    def test_warms_each_configured_snapshot_once_without_interval(self):
        calls = []

        def opener(request, timeout):
            calls.append((request.full_url, timeout))
            return Response(request.full_url.encode())

        with tempfile.TemporaryDirectory() as tmp:
            cache = SnapshotCache(
                carousel_pane()["streams"],
                tmp,
                refresh_seconds=0,
                opener=opener,
            )
            cache._run()
            self.assertEqual(calls, [("http://front/snapshot.jpg", 5)])
            with open(cache.path_for(0), "rb") as f:
                self.assertEqual(f.read(), b"http://front/snapshot.jpg")
            self.assertFalse(os.path.exists(cache.path_for(1)))

    def test_failed_refresh_preserves_last_good_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = SnapshotCache(
                carousel_pane()["streams"],
                tmp,
                opener=lambda *_args, **_kwargs: Response(b"old"),
            )
            self.assertTrue(cache.fetch_one(0, "http://front/snapshot.jpg"))
            cache.opener = mock.Mock(side_effect=OSError("offline"))
            self.assertFalse(cache.fetch_one(0, "http://front/snapshot.jpg"))
            with open(cache.path_for(0), "rb") as f:
                self.assertEqual(f.read(), b"old")

    def test_successful_fetch_notifies_snapshot_only_display(self):
        updates = []
        with tempfile.TemporaryDirectory() as tmp:
            cache = SnapshotCache(
                [{"name": "Still", "snapshot_url": "http://camera/still.jpg"}],
                tmp,
                refresh_seconds=0,
                opener=lambda *_args, **_kwargs: Response(b"image"),
                on_update=updates.append,
            )
            cache._run()
            self.assertEqual(updates, [0])


class SnapshotOnlyControllerTests(unittest.TestCase):
    def test_switch_does_not_launch_mpv_without_stream_url(self):
        controller = object.__new__(CarouselController)
        controller.switch_lock = mock.MagicMock()
        controller._stop_mpv = mock.Mock()
        controller._start_mpv = mock.Mock()
        controller.shutting_down = False
        controller.generation = 1
        controller.streams = [
            {"name": "Still", "snapshot_url": "http://camera/still.jpg"}
        ]

        controller._switch_worker(1, 0)

        controller._stop_mpv.assert_called_once()
        controller._start_mpv.assert_not_called()

    def test_snapshot_refresh_updates_visible_snapshot_only_entry(self):
        controller = object.__new__(CarouselController)
        controller.shutting_down = False
        controller.current_index = 0
        controller.video_visible = True
        controller.streams = [
            {"name": "Still", "snapshot_url": "http://camera/still.jpg"}
        ]
        controller._render_snapshot = mock.Mock()

        controller._apply_snapshot_update(0)

        self.assertFalse(controller.video_visible)
        controller._render_snapshot.assert_called_once_with(raise_layer=True)


class ProcessCleanupTests(unittest.TestCase):
    def test_mpv_terminate_escalates_and_reaps_before_returning(self):
        proc = mock.Mock()
        proc.pid = 42
        proc.poll.return_value = None
        proc.wait.side_effect = [
            subprocess.TimeoutExpired("mpv", 2),
            0,
        ]
        CarouselController._terminate_process(proc)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        self.assertEqual(proc.wait.call_count, 2)

    @mock.patch.object(DisplayManager, "_process_group_alive", return_value=False)
    @mock.patch("display_server.shutil.rmtree")
    @mock.patch("display_server.os.killpg")
    def test_carousel_pane_cleanup_targets_process_group(
        self, killpg, rmtree, _group_alive
    ):
        proc = mock.Mock()
        proc.pid = 99
        proc.wait.return_value = 0
        pane = ManagedPane(
            name="cameras",
            ptype="rtsp_carousel",
            proc=proc,
            process_group=True,
            runtime_dir="/tmp/carousel-runtime",
        )
        manager = object.__new__(DisplayManager)
        manager.panes = {"cameras": pane}

        manager._kill_pane("cameras")

        killpg.assert_called_once_with(99, signal.SIGTERM)
        proc.wait.assert_called_once_with(timeout=1)
        rmtree.assert_called_once_with("/tmp/carousel-runtime", ignore_errors=True)


class EditorHtmlTests(unittest.TestCase):
    def _render_editor(self):
        fake_dm = mock.Mock()
        fake_dm._screens_doc = {
            "screens": [{"name": "Default", "panes": []}],
            "playingIndex": 0,
        }
        fake_dm.status.return_value = {
            "screen": {"width": 1920, "height": 1080},
            "system": {},
            "panes": {},
        }
        captured = []
        handler = object.__new__(Handler)
        handler._send_html = captured.append
        with mock.patch.object(display_server, "dm", fake_dm, create=True):
            handler._serve_index()
        return captured[0]

    def test_editor_contains_full_carousel_controls(self):
        html = self._render_editor()
        for marker in (
            'option value="rtsp_carousel"',
            'id="p-streams"',
            'id="p-snapshot-refresh"',
            'id="p-cycle-seconds"',
            'id="p-show-controls"',
            'id="p-show-stream-name"',
            "function addCarouselStream()",
            "function moveCarouselStream(index, delta)",
        ):
            self.assertIn(marker, html)

    def test_generated_editor_javascript_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        version = subprocess.run(
            [node, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if version.returncode != 0:
            self.skipTest("node is not configured")
        html = self._render_editor()
        script = html.split("<script>", 1)[1].rsplit("</script>", 1)[0]
        result = subprocess.run(
            [node, "--check", "-"],
            input=script,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
