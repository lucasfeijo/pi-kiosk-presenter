"""MQTT discovery and live state for the Pi's carousel controllers."""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from carousel_control import MAX_FOCUS_SECONDS

log = logging.getLogger(__name__)

_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_PICTURES = {"live", "snapshot", "connecting", "no_image"}
_MAX_FRAME = 8192
_POLL_INTERVAL = 0.1
_COMMAND_QUEUE_AGE = 2


@dataclass(frozen=True)
class Panel:
    id: str
    name: str
    socket_path: str | None
    cameras: tuple[tuple[str, str], ...]

    @property
    def options(self) -> dict[str, str]:
        """Map the ordered Home Assistant option labels to stable camera IDs."""
        names = {name for _, name in self.cameras}
        if len(names) != len(self.cameras) or not names.isdisjoint({"", "None"}):
            return {f"{name} ({camera_id})": camera_id for camera_id, name in self.cameras}
        return {name: camera_id for camera_id, name in self.cameras}


@dataclass(frozen=True)
class Config:
    host: str
    port: int = 1883
    display_id: str = "pi-display"
    name: str = "Pi Display"
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    tls: bool = False
    ca_file: str | None = None
    discovery_prefix: str = "homeassistant"
    birth_topic: str = "homeassistant/status"

    @classmethod
    def from_env(cls) -> Config | None:
        host = os.environ.get("DISPLAY_MQTT_HOST", "").strip()
        if not host:
            return None
        if "://" in host or "/" in host:
            raise ValueError("DISPLAY_MQTT_HOST must be a host name or IP address")
        tls_value = os.environ.get("DISPLAY_MQTT_TLS", "").strip().lower()
        if tls_value not in {"", "0", "1", "false", "true", "no", "yes"}:
            raise ValueError("DISPLAY_MQTT_TLS must be true or false")
        tls = tls_value in {"1", "true", "yes"}
        try:
            port = int(os.environ.get("DISPLAY_MQTT_PORT", "8883" if tls else "1883"))
        except ValueError:
            raise ValueError("DISPLAY_MQTT_PORT must be a TCP port") from None
        if not 1 <= port <= 65535:
            raise ValueError("DISPLAY_MQTT_PORT must be a TCP port")
        display_id = os.environ.get("DISPLAY_MQTT_ID", "pi-display")
        if not _ID.fullmatch(display_id):
            raise ValueError("DISPLAY_MQTT_ID must match [A-Za-z0-9_-]{1,64}")
        username = os.environ.get("DISPLAY_MQTT_USERNAME") or None
        password = os.environ.get("DISPLAY_MQTT_PASSWORD") or None
        if password and not username:
            raise ValueError("DISPLAY_MQTT_PASSWORD requires DISPLAY_MQTT_USERNAME")
        prefix = os.environ.get("DISPLAY_MQTT_DISCOVERY_PREFIX", "homeassistant").strip("/")
        birth = os.environ.get("DISPLAY_MQTT_BIRTH_TOPIC", "homeassistant/status")
        if not all(topic and not any(char in topic for char in "+#\0") for topic in (prefix, birth)):
            raise ValueError("MQTT discovery prefix and birth topic must not contain wildcards")
        return cls(
            host=host,
            port=port,
            display_id=display_id,
            name=os.environ.get("DISPLAY_MQTT_NAME") or "Pi Display",
            username=username,
            password=password,
            tls=tls,
            ca_file=os.environ.get("DISPLAY_MQTT_CA_FILE") or None,
            discovery_prefix=prefix,
            birth_topic=birth,
        )

    @property
    def root(self) -> str:
        return f"pi-display/{self.display_id}"

    @property
    def discovery_topic(self) -> str:
        return f"{self.discovery_prefix}/device/{self.display_id}/config"


class _PanelLink:
    """Own a nonblocking runtime connection and its last controller-confirmed state."""

    def __init__(self, panel: Panel):
        self.panel = panel
        self.options = panel.options
        self.labels = {camera_id: name for name, camera_id in self.options.items()}
        self.state = None
        self.dirty = True
        self._socket = None
        self._buffer = bytearray()
        self._retry_at = 0.0

    def close(self):
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if self.state is not None:
            self.state = None
            self.dirty = True
        self._buffer.clear()
        self._retry_at = time.monotonic() + 0.5

    def poll(self):
        if self._socket is None:
            if self.panel.socket_path is None or time.monotonic() < self._retry_at:
                return
            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._socket.settimeout(0.05)
            try:
                self._socket.connect(self.panel.socket_path)
                self._socket.setblocking(False)
            except OSError:
                self.close()
                return
        try:
            chunk = self._socket.recv(_MAX_FRAME)
        except BlockingIOError:
            return
        except OSError:
            self.close()
            return
        if not chunk:
            self.close()
            return
        self._buffer.extend(chunk)
        while (end := self._buffer.find(b"\n")) >= 0 and end <= _MAX_FRAME:
            frame = bytes(self._buffer[:end])
            del self._buffer[: end + 1]
            self._update_state(frame)
        if len(self._buffer) > _MAX_FRAME:
            self.close()

    def _update_state(self, frame: bytes):
        try:
            message = json.loads(frame)
        except (ValueError, RecursionError):
            return
        if not isinstance(message, dict) or message.get("type") != "state":
            return
        camera_id = message.get("camera_id")
        picture = message.get("picture")
        focus_until = message.get("focus_until")
        if not isinstance(camera_id, str) or camera_id not in self.labels:
            return
        if not isinstance(picture, str) or picture not in _PICTURES:
            return
        if focus_until is not None and (not isinstance(focus_until, str) or len(focus_until) > 64):
            return
        state = {
            "camera": self.labels[camera_id],
            "camera_id": camera_id,
            "picture": picture,
            "focus_until": focus_until,
        }
        if self.state != state:
            self.state = state
            self.dirty = True

    def send(self, command: dict):
        if self._socket is None or self.state is None:
            return
        frame = json.dumps(command).encode() + b"\n"
        try:
            sent = self._socket.send(frame)
        except BlockingIOError:
            # Drop a live command when its controller cannot keep up.
            return
        except OSError:
            sent = 0
        if sent != len(frame):
            self.close()


def _finite_number(value) -> bool:
    # Bound huge JSON integers before converting them to floats.
    return type(value) in (int, float) and abs(value) < 1e15 and math.isfinite(value)


def _focus_command(payload: bytes, camera_ids: dict[str, str], now: float) -> dict | None:
    try:
        data = json.loads(payload)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    camera_id = data.get("camera_id")
    duration = data.get("duration_seconds")
    request_id = data.get("request_id")
    issued_at = data.get("issued_at")
    if not isinstance(camera_id, str) or camera_id not in camera_ids:
        return None
    if not _finite_number(duration) or not 0 < duration <= MAX_FOCUS_SECONDS:
        return None
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
        return None
    if not _finite_number(issued_at) or not now - 10 <= issued_at <= now + 5:
        return None
    return {
        "action": "focus",
        "camera_id": camera_id,
        "duration_seconds": duration,
        "request_id": request_id,
    }


class MQTTBridge:
    """Project live carousels into MQTT while the display runs independently."""

    def __init__(self, config: Config, list_panels: Callable[[], Iterable[Panel] | None], client):
        self.config = config
        self._list_panels = list_panels
        self._client = client
        self._links: dict[str, _PanelLink] = {}
        self._announced: set[str] = set()
        self._discovery_dirty = True
        self._messages = queue.Queue(maxsize=128)
        self._connected = threading.Event()
        self._refresh = threading.Event()
        self._stopping = threading.Event()
        self._epoch = 0
        self._published_epoch = -1
        self._worker = threading.Thread(target=self._run, name="display-mqtt", daemon=True)

        client.on_connect = self._on_connect
        client.on_connect_fail = self._on_connect_fail
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.will_set(f"{config.root}/availability", "offline", qos=1, retain=True)

    @classmethod
    def start_from_env(cls, list_panels: Callable[[], Iterable[Panel] | None]) -> MQTTBridge | None:
        try:
            config = Config.from_env()
        except ValueError as exc:
            log.warning("MQTT integration disabled: %s", exc)
            return None
        if config is None:
            return None
        try:
            from paho.mqtt import client as mqtt
        except ImportError:
            log.warning("MQTT integration disabled: install python3-paho-mqtt")
            return None
        try:
            client_options = {
                "client_id": f"display-{config.display_id}",
                "clean_session": True,
                "protocol": mqtt.MQTTv311,
            }
            if hasattr(mqtt, "CallbackAPIVersion"):
                client_options["callback_api_version"] = mqtt.CallbackAPIVersion.VERSION2
            client = mqtt.Client(**client_options)
            if config.username:
                client.username_pw_set(config.username, config.password)
            if config.tls:
                client.tls_set(ca_certs=config.ca_file)
            client.reconnect_delay_set(min_delay=1, max_delay=30)
            client.max_queued_messages_set(256)
            bridge = cls(config, list_panels, client)
            bridge.start()
            return bridge
        except (OSError, RuntimeError, ValueError) as exc:
            log.warning("MQTT integration disabled: could not start client (%s)", type(exc).__name__)
            return None

    def start(self):
        self._client.connect_async(self.config.host, self.config.port, keepalive=15)
        if self._client.loop_start() != 0:
            raise RuntimeError("MQTT network loop could not start")
        self._worker.start()

    def stop(self):
        self._stopping.set()
        self._worker.join(timeout=2)
        # A resolver or TLS handshake in the network thread may still be pending.
        # Paho's loop_stop joins that thread without a timeout.
        closer = threading.Thread(target=self._stop_client, name="display-mqtt-stop", daemon=True)
        closer.start()
        closer.join(timeout=2)

    def _stop_client(self):
        try:
            if self._connected.is_set():
                message = self._client.publish(
                    f"{self.config.root}/availability", "offline", qos=1, retain=True
                )
                message.wait_for_publish(timeout=0.5)
        except (OSError, RuntimeError, ValueError):
            pass
        finally:
            self._client.disconnect()
            self._client.loop_stop()

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties=None):
        if reason_code != 0:
            log.warning("MQTT broker refused the display connection (%s)", reason_code)
            return
        self._epoch += 1
        client.subscribe([
            (self.config.birth_topic, 1),
            (self.config.discovery_topic, 1),
            (f"{self.config.root}/+/set", 1),
            (f"{self.config.root}/+/focus", 1),
        ])
        self._connected.set()

    def _on_disconnect(self, *_args):
        self._connected.clear()

    @staticmethod
    def _on_connect_fail(_client, _userdata):
        log.warning("Could not connect to the MQTT broker, retrying")

    def _on_message(self, _client, _userdata, message):
        if message.topic == self.config.birth_topic:
            if message.payload == b"online":
                self._refresh.set()
            return
        if message.topic == self.config.discovery_topic:
            # Read the previous retained component list so that removals also
            # work when the layout changed while this bridge was stopped.
            if not message.retain or len(message.payload) > 256 * 1024:
                return
        elif message.retain or len(message.payload) > _MAX_FRAME:
            return
        try:
            self._messages.put_nowait((self._epoch, time.monotonic(), message.topic, message.payload))
        except queue.Full:
            log.debug("Dropping MQTT message because the display queue is full")

    def _run(self):
        try:
            while not self._stopping.is_set():
                self._tick()
                self._stopping.wait(_POLL_INTERVAL)
        except Exception:
            log.exception("MQTT display worker stopped unexpectedly")
            self._stop_client()
        finally:
            for link in self._links.values():
                link.close()

    def _tick(self):
        panels = self._list_panels()
        if panels is not None:
            self._reconcile(panels)
        for link in self._links.values():
            link.poll()
        self._drain_messages()
        if self._connected.is_set():
            self._publish_updates()

    def _reconcile(self, panels: Iterable[Panel]):
        active = {panel.id: panel for panel in panels if _ID.fullmatch(panel.id)}
        for panel_id in self._links.keys() - active.keys():
            self._links.pop(panel_id).close()
            self._discovery_dirty = True
        for panel_id, panel in active.items():
            existing = self._links.get(panel_id)
            if existing is None:
                self._links[panel_id] = _PanelLink(panel)
                self._discovery_dirty = True
            elif existing.panel != panel:
                existing.close()
                self._links[panel_id] = _PanelLink(panel)
                if (existing.panel.name, existing.panel.cameras) != (panel.name, panel.cameras):
                    self._discovery_dirty = True

    def _drain_messages(self):
        for _ in range(128):
            try:
                epoch, received_at, topic, payload = self._messages.get_nowait()
            except queue.Empty:
                return
            if epoch != self._epoch or not self._connected.is_set():
                continue
            if topic == self.config.discovery_topic:
                self._remember_discovery(payload)
            elif time.monotonic() - received_at <= _COMMAND_QUEUE_AGE:
                self._dispatch(topic, payload)

    def _remember_discovery(self, payload: bytes):
        try:
            data = json.loads(payload)
        except (ValueError, RecursionError):
            return
        if not isinstance(data, dict) or data.get("origin") != {"name": "pi-display-server"}:
            return
        device = data.get("device")
        if not isinstance(device, dict) or device.get("identifiers") != [self.config.root]:
            return
        components = data.get("components")
        if isinstance(components, dict):
            previous = {panel_id for panel_id in components if _ID.fullmatch(panel_id)}
            if previous - self._links.keys():
                self._announced.update(previous)
                self._discovery_dirty = True

    def _dispatch(self, topic: str, payload: bytes):
        prefix = f"{self.config.root}/"
        if not topic.startswith(prefix):
            return
        panel_id, _, action = topic[len(prefix):].partition("/")
        link = self._links.get(panel_id)
        if link is None:
            return
        if action == "set":
            try:
                camera_id = link.options.get(payload.decode())
            except UnicodeDecodeError:
                return
            command = {"action": "select", "camera_id": camera_id} if camera_id is not None else None
        elif action == "focus":
            command = _focus_command(payload, link.labels, now=time.time())
        else:
            return
        if command:
            link.send(command)

    def _publish_updates(self):
        epoch = self._epoch
        refresh = epoch != self._published_epoch or self._refresh.is_set()
        if refresh:
            self._refresh.clear()
            self._discovery_dirty = True
        if self._discovery_dirty:
            if not self._publish_discovery():
                return
            self._discovery_dirty = False
            for link in self._links.values():
                link.dirty = True
        for link in self._links.values():
            if link.dirty:
                link.dirty = not self._publish_panel(link)
        if refresh and all(not link.dirty for link in self._links.values()):
            if self._publish(f"{self.config.root}/availability", "online"):
                self._published_epoch = epoch

    def _publish_discovery(self) -> bool:
        config = self.config
        components = {}
        for link in self._links.values():
            panel_id = link.panel.id
            prefix = f"{config.root}/{panel_id}"
            entity_slug = f"{config.display_id}_{panel_id}".lower().replace("-", "_")
            components[panel_id] = {
                "platform": "select",
                "name": link.panel.name,
                "unique_id": f"{config.root}/{panel_id}/select",
                "default_entity_id": f"select.{entity_slug}",
                "options": list(link.options),
                "command_topic": f"{prefix}/set",
                "state_topic": f"{prefix}/state",
                "value_template": "{{ value_json.camera }}",
                "json_attributes_topic": f"{prefix}/state",
                "availability": [
                    {"topic": f"{config.root}/availability"},
                    {"topic": f"{prefix}/availability"},
                ],
                "availability_mode": "all",
                "optimistic": False,
                "retain": False,
                # An interrupted HA connection must not replay an old choice.
                "qos": 0,
            }
        removed = self._announced - components.keys()
        for panel_id in removed:
            if not self._publish(f"{config.root}/{panel_id}/availability", ""):
                return False
            if not self._publish(f"{config.root}/{panel_id}/state", ""):
                return False
        data = {
            "device": {"identifiers": [config.root], "name": config.name},
            "origin": {"name": "pi-display-server"},
            "components": components,
        }
        if removed and components:
            tombstones = {panel_id: {"platform": "select"} for panel_id in removed}
            update = {**data, "components": {**components, **tombstones}}
            if not self._publish(config.discovery_topic, json.dumps(update), retain=False):
                return False
        if not self._publish(config.discovery_topic, json.dumps(data) if components else ""):
            return False
        self._announced = set(components)
        return True

    def _publish_panel(self, link: _PanelLink) -> bool:
        prefix = f"{self.config.root}/{link.panel.id}"
        if link.state is None:
            return self._publish(f"{prefix}/availability", "offline")
        state = {**link.state, "camera_ids": link.options, "focus_topic": f"{prefix}/focus"}
        return self._publish(f"{prefix}/state", json.dumps(state)) and self._publish(
            f"{prefix}/availability", "online"
        )

    def _publish(self, topic: str, payload: str, retain: bool = True) -> bool:
        return self._client.publish(topic, payload, qos=1, retain=retain).rc == 0
