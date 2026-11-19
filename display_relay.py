"""Authenticated camera commands and state for a Home Assistant MQTT relay."""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import ssl
import threading
import time

from display_mqtt import _PanelLink, _focus_command_from_data, _recent_command


CAMERA_API_PATH = "/api/home-assistant/cameras"
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,256}")
_MAX_REQUEST_BYTES = 4096
_POLL_INTERVAL = 0.1
_SELECT_FIELDS = {"panel_id", "action", "camera_id", "issued_at"}
_FOCUS_FIELDS = _SELECT_FIELDS | {"duration_seconds", "request_id"}
log = logging.getLogger(__name__)


def camera_api_token_from_env(*, tls_enabled: bool, direct_mqtt_enabled: bool) -> str | None:
    token = os.environ.get("DISPLAY_CAMERA_API_TOKEN")
    if not token:
        return None
    if direct_mqtt_enabled:
        log.warning("Camera relay API disabled while direct MQTT is configured")
    elif not tls_enabled:
        log.warning("Camera relay API disabled because HTTPS is not configured")
    elif not _TOKEN_PATTERN.fullmatch(token):
        log.warning("Camera relay API disabled because its token is invalid")
    else:
        return token
    return None


class CameraRelay:
    def __init__(self, token: str, list_panels):
        self._token = token
        self._list_panels = list_panels
        self._links: dict[str, _PanelLink] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, name="display-camera-api", daemon=True)

    def start(self):
        self._poll()
        self._worker.start()

    def stop(self):
        self._stop.set()
        self._worker.join(timeout=2)

    def authorized(self, authorization_headers) -> bool:
        if len(authorization_headers) != 1:
            return False
        scheme, _, token = authorization_headers[0].partition(" ")
        if scheme.lower() != "bearer" or not _TOKEN_PATTERN.fullmatch(token):
            return False
        return hmac.compare_digest(token, self._token)

    def snapshot(self) -> dict:
        with self._lock:
            return {"panels": [
                {
                    "id": link.panel.id,
                    "name": link.panel.name,
                    "options": dict(link.options),
                    "state": dict(link.state) if link.state is not None else None,
                }
                for link in self._links.values()
            ]}

    def submit(self, data) -> tuple[int, dict]:
        if not isinstance(data, dict):
            return 400, {"error": "Expected a camera command object"}
        panel_id, camera_id, action = data.get("panel_id"), data.get("camera_id"), data.get("action")
        fields = _FOCUS_FIELDS if action == "focus" else _SELECT_FIELDS
        if (action not in ("select", "focus") or not isinstance(panel_id, str)
                or not isinstance(camera_id, str) or data.keys() != fields
                or not _recent_command(data.get("issued_at"), time.time())):
            return 400, {"error": "Invalid or expired camera command"}
        with self._lock:
            link = self._links.get(panel_id)
            if link is None or camera_id not in link.labels:
                return 404, {"error": "Unknown panel or camera"}
            command = {"action": "select", "camera_id": camera_id}
            if action == "focus":
                command = _focus_command_from_data(data, link.labels, time.time())
                if command is None:
                    return 400, {"error": "Invalid or expired focus command"}
            if not link.send(command):
                return 409, {"error": "Panel is unavailable"}
        # The state published by the controller confirms the actual selection.
        return 202, {"accepted": True}

    def _run(self):
        try:
            while not self._stop.is_set():
                self._poll()
                self._stop.wait(_POLL_INTERVAL)
        except Exception:
            log.exception("Camera relay worker stopped unexpectedly")
        finally:
            with self._lock:
                for link in self._links.values():
                    link.close()
                self._links.clear()

    def _poll(self):
        panels = self._list_panels()
        with self._lock:
            if panels is not None:
                active = {panel.id: panel for panel in panels}
                for panel_id in self._links.keys() - active.keys():
                    self._links.pop(panel_id).close()
                for panel_id, panel in active.items():
                    previous = self._links.get(panel_id)
                    if previous is None:
                        self._links[panel_id] = _PanelLink(panel)
                    elif previous.panel != panel:
                        previous.close()
                        self._links[panel_id] = _PanelLink(panel)
            for link in self._links.values():
                link.poll()


def serve_camera_request(handler, relay: CameraRelay | None, *, write: bool):
    """Serve the HTTPS-only camera API through the existing display listener."""
    handler.close_connection = True
    headers = {"Cache-Control": "no-store", "Connection": "close"}
    if relay is None or not isinstance(handler.connection, ssl.SSLSocket):
        status, data = 404, {"error": "Not found"}
    elif not relay.authorized(handler.headers.get_all("Authorization") or []):
        status, data = 401, {"error": "Authorization required"}
        headers["WWW-Authenticate"] = "Bearer"
    elif write:
        status, data = _read_camera_command(handler, relay)
    else:
        status, data = 200, relay.snapshot()
    handler._send_bytes(json.dumps(data).encode(), "application/json", status, headers)


def _read_camera_command(handler, relay: CameraRelay) -> tuple[int, dict]:
    lengths = handler.headers.get_all("Content-Length") or []
    if (len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,4}", lengths[0])
            or handler.headers.get_all("Transfer-Encoding")
            or handler.headers.get_content_type() != "application/json"):
        return 400, {"error": "Expected a bounded JSON request body"}
    length = int(lengths[0])
    if not 0 < length <= _MAX_REQUEST_BYTES:
        return 400, {"error": "Expected a bounded JSON request body"}
    try:
        handler.connection.settimeout(2)
        raw = handler.rfile.read(length)
        if len(raw) != length:
            raise ValueError("Incomplete body")
        data = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return 400, {"error": "Invalid JSON request body"}
    return relay.submit(data)
