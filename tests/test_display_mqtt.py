import json
import os
import socket
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from display_mqtt import Config, MQTTBridge, Panel


class FakeClient:
    def __init__(self):
        self.messages = []

    def will_set(self, topic, payload, qos, retain):
        self.will = topic, payload, qos, retain

    def subscribe(self, topics):
        self.subscriptions = topics

    def publish(self, topic, payload, *, qos, retain):
        self.messages.append((topic, payload, qos, retain))
        return SimpleNamespace(rc=0)


class ConfigTests(unittest.TestCase):
    def test_disabled_when_host_is_not_configured(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(MQTTBridge.start_from_env(lambda: ()))

    def test_tls_and_custom_discovery_settings(self):
        with mock.patch.dict(os.environ, {
            "DISPLAY_MQTT_HOST": "broker.example",
            "DISPLAY_MQTT_TLS": "true",
            "DISPLAY_MQTT_ID": "display-pai",
            "DISPLAY_MQTT_DISCOVERY_PREFIX": "custom",
        }, clear=True):
            config = Config.from_env()
        self.assertEqual(config.port, 8883)
        self.assertEqual(config.discovery_topic, "custom/device/display-pai/config")
        self.assertEqual(config.birth_topic, "homeassistant/status")

    def test_invalid_settings_disable_only_the_bridge(self):
        with mock.patch.dict(os.environ, {
            "DISPLAY_MQTT_HOST": "broker.example",
            "DISPLAY_MQTT_ID": "pi/#",
        }, clear=True):
            with self.assertLogs("display_mqtt", "WARNING"):
                self.assertIsNone(MQTTBridge.start_from_env(lambda: ()))


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.config = Config("broker.example", display_id="pai", name="Display do pai")
        self.client = FakeClient()
        self.panel = Panel("entrada", "Entrada", None, (("hall", "Hall"), ("portao", "Portão")))
        self.panels = [self.panel]
        self.bridge = MQTTBridge(self.config, lambda: self.panels, self.client)
        self.assertEqual(self.client.will, ("pi-display/pai/availability", "offline", 1, True))
        self.bridge._on_connect(self.client, None, None, 0)
        self.bridge._tick()
        bridge_socket, self.control = socket.socketpair()
        bridge_socket.setblocking(False)
        self.control.settimeout(0.5)
        self.addCleanup(self.control.close)
        link = self.bridge._links["entrada"]
        link._socket = bridge_socket
        self.addCleanup(link.close)
        self.prefix = "pi-display/pai/entrada"

    def _receive(self, action, payload: bytes, retain=False):
        topic = f"{self.prefix}/{action}"
        msg = SimpleNamespace(topic=topic, payload=payload, retain=retain)
        self.bridge._on_message(self.client, None, msg)
        self.bridge._tick()

    def _publish_child(self, camera_id, picture, focus_until=None):
        self.control.sendall(json.dumps({
            "type": "state",
            "camera_id": camera_id,
            "picture": picture,
            "focus_until": focus_until,
        }).encode() + b"\n")
        self.bridge._tick()

    def _published(self, topic):
        return [payload for name, payload, _qos, retain in self.client.messages if name == topic and retain]

    def test_discovery_round_trip_only_reports_controller_confirmed_camera(self):
        discovery = json.loads(self._published(self.config.discovery_topic)[-1])
        component = discovery["components"]["entrada"]
        self.assertEqual(discovery["device"]["name"], "Display do pai")
        self.assertEqual(component["options"], ["Hall", "Portão"])
        self.assertEqual(component["command_topic"], f"{self.prefix}/set")
        self.assertFalse(component["optimistic"])
        self.assertFalse(component["retain"])
        self.assertEqual(self._published(f"{self.prefix}/availability"), ["offline"])

        self._publish_child("hall", "snapshot")
        self._receive("set", "Portão".encode())
        self.assertEqual(json.loads(self.control.recv(1024)), {"action": "select", "camera_id": "portao"})
        self.assertEqual(json.loads(self._published(f"{self.prefix}/state")[-1])["camera"], "Hall")
        self._publish_child("portao", "connecting")
        self._publish_child("portao", "live")
        status = json.loads(self._published(f"{self.prefix}/state")[-1])
        self.assertEqual(status["camera"], "Portão")
        self.assertEqual(status["picture"], "live")
        self.assertEqual(status["camera_ids"], {"Hall": "hall", "Portão": "portao"})
        self.assertEqual(status["focus_topic"], f"{self.prefix}/focus")

    def test_retained_stale_and_invalid_commands_are_ignored(self):
        self._publish_child("hall", "snapshot")
        focus = {
            "camera_id": "portao", "duration_seconds": 30, "request_id": "movement-1",
            "issued_at": time.time() - 30,
        }
        self._receive("set", b"Hall", retain=True)
        self._receive("set", b"rtsp://not-an-option")
        self._receive("focus", json.dumps(focus).encode())
        self._receive("focus", json.dumps({**focus, "issued_at": time.time(), "duration_seconds": 0}).encode())
        self.control.setblocking(False)
        with self.assertRaises(BlockingIOError):
            self.control.recv(1024)

        self._receive("focus", json.dumps({**focus, "issued_at": time.time()}).encode())
        self.assertEqual(json.loads(self.control.recv(1024)), {
            "action": "focus", "camera_id": "portao", "duration_seconds": 30,
            "request_id": "movement-1",
        })

    def test_ha_restart_republishes_and_panel_exit_marks_only_panel_offline(self):
        self._publish_child("hall", "snapshot")
        self.client.messages.clear()
        birth = SimpleNamespace(topic=self.config.birth_topic, payload=b"online", retain=False)
        self.bridge._on_message(self.client, None, birth)
        self.bridge._tick()
        self.assertTrue(self._published(self.config.discovery_topic))
        self.assertTrue(self._published(f"{self.prefix}/state"))

        self.control.close()
        self.bridge._tick()
        self.assertEqual(self._published(f"{self.prefix}/availability")[-1], "offline")
        self.assertEqual(self._published("pi-display/pai/availability")[-1], "online")

        self.panels = None
        self.bridge._tick()
        self.assertTrue(self._published(self.config.discovery_topic)[-1])
        self.panels = []
        self.bridge._tick()
        self.assertEqual(self._published(self.config.discovery_topic)[-1], "")
        self.assertEqual(self._published(f"{self.prefix}/state")[-1], "")

    def test_stale_discovery_from_before_restart_removes_old_panel(self):
        original = json.loads(self._published(self.config.discovery_topic)[-1])
        original["components"]["old"] = {"platform": "select", "unique_id": "old"}
        previous = SimpleNamespace(
            topic=self.config.discovery_topic, payload=json.dumps(original).encode(), retain=True,
        )
        self.bridge._on_message(self.client, None, previous)
        self.bridge._tick()
        tombstone = [
            json.loads(payload)
            for topic, payload, _qos, retain in self.client.messages
            if topic == self.config.discovery_topic and not retain
        ]
        self.assertEqual(tombstone[-1]["components"]["old"], {"platform": "select"})
        latest = json.loads(self._published(self.config.discovery_topic)[-1])
        self.assertNotIn("old", latest["components"])

    def test_duplicate_display_names_still_produce_distinct_options(self):
        panel = Panel("cameras", "Câmeras", None, (("hall1", "Hall"), ("hall2", "Hall")))
        self.assertEqual(panel.options, {"Hall (hall1)": "hall1", "Hall (hall2)": "hall2"})


if __name__ == "__main__":
    unittest.main()
