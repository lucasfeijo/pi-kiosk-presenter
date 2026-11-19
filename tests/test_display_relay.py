"""Contract tests for the HTTPS Home Assistant camera API."""

import io
import json
import socket
import ssl
import time
import unittest
from email.message import Message
from unittest import mock

from display_mqtt import Panel
from display_relay import CameraRelay, camera_api_token_from_env, serve_camera_request


TOKEN = "public-test-credential-0123456789abcdef"
PANEL = Panel("entry", "Entry", None, (("hall", "Hall"), ("gate", "Gate")))


class CameraRelayTests(unittest.TestCase):
    def setUp(self):
        self.relay = CameraRelay(TOKEN, lambda: (PANEL,))
        self.relay._poll()
        self.link = self.relay._links["entry"]

    def command(self, **changes):
        return {
            "panel_id": "entry",
            "camera_id": "gate",
            "action": "select",
            "issued_at": time.time(),
            **changes,
        }

    @staticmethod
    def state(camera_id):
        return json.dumps({
            "type": "state",
            "camera_id": camera_id,
            "picture": "live",
            "focus_until": None,
        }).encode() + b"\n"

    def test_commands_round_trip_through_local_controller_link(self):
        bridge, controller = socket.socketpair()
        with bridge, controller:
            bridge.setblocking(False)
            controller.settimeout(0.5)
            self.link._socket = bridge
            controller.sendall(self.state("hall"))
            self.relay._poll()
            self.assertEqual(self.relay.submit(self.command()), (202, {"accepted": True}))
            self.assertEqual(
                json.loads(controller.recv(4096)),
                {"action": "select", "camera_id": "gate"},
            )
            self.assertEqual(self.relay.snapshot()["panels"][0]["state"]["camera"], "Hall")
            controller.sendall(self.state("gate"))
            self.relay._poll()
            snapshot = self.relay.snapshot()["panels"][0]
            self.assertEqual(snapshot["state"]["camera"], "Gate")
            self.assertEqual(snapshot["options"], {"Hall": "hall", "Gate": "gate"})

    def test_command_errors_are_classified_without_forwarding(self):
        for data, status in (
            (self.command(issued_at=time.time() - 60), 400),
            (self.command(camera_id="unknown"), 404),
            (self.command(panel_id="unknown"), 404),
            (self.command(duration_seconds=10), 400),
            (self.command(action="focus", request_id="event", duration_seconds=3601), 400),
            (self.command(action="focus", request_id="event", duration_seconds=30), 409),
            (self.command(), 409),
        ):
            with self.subTest(data=data):
                self.assertEqual(self.relay.submit(data)[0], status)

    def test_token_requires_https_and_excludes_direct_mqtt(self):
        with mock.patch.dict("os.environ", {"DISPLAY_CAMERA_API_TOKEN": TOKEN}):
            args = {"tls_enabled": True, "direct_mqtt_enabled": False}
            self.assertEqual(camera_api_token_from_env(**args), TOKEN)
            with self.assertLogs("display_relay", level="WARNING") as logs:
                self.assertIsNone(camera_api_token_from_env(**dict(args, tls_enabled=False)))
                self.assertIsNone(camera_api_token_from_env(**dict(args, direct_mqtt_enabled=True)))
            self.assertNotIn(TOKEN, "\n".join(logs.output))

    def request(self, *, auth=f"Bearer {TOKEN}", tls=True, body=None):
        handler = mock.Mock()
        handler.connection = mock.Mock(spec=ssl.SSLSocket) if tls else mock.Mock()
        handler.headers = Message()
        if auth is not None:
            handler.headers["Authorization"] = auth
        if body is not None:
            handler.headers["Content-Type"] = "application/json"
            handler.headers["Content-Length"] = str(len(body))
            handler.rfile = io.BytesIO(body)
        serve_camera_request(handler, self.relay, write=body is not None)
        data, content_type, status, headers = handler._send_bytes.call_args.args
        self.assertEqual(content_type, "application/json")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertTrue(handler.close_connection)
        return status, json.loads(data), headers

    def test_http_route_requires_tls_and_bearer(self):
        self.assertEqual(self.request(tls=False)[:2], (404, {"error": "Not found"}))
        status, _, headers = self.request(auth=None)
        self.assertEqual(status, 401)
        self.assertEqual(headers["WWW-Authenticate"], "Bearer")
        self.assertEqual(self.request(auth="Bearer invalid")[0], 401)
        self.assertEqual(self.request()[:2], (200, self.relay.snapshot()))

    def test_http_drops_large_and_invalid_bodies(self):
        self.assertEqual(self.request(body=b" " * 4097)[0], 400)
        self.assertEqual(self.request(body=b'{"invalid":')[0], 400)
        self.assertEqual(self.request(body=json.dumps(self.command()).encode())[0], 409)


if __name__ == "__main__":
    unittest.main()
