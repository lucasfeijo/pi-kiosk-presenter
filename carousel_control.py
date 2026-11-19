"""Local runtime channel between a carousel and its display server."""

import json
import logging
import os
import queue
import socket
import threading
import time
from collections import deque


CONTROL_SOCKET_NAME = "control.sock"
MAX_FOCUS_SECONDS = 3600
MAX_COMMAND_BYTES = 4096
MAX_PENDING_COMMANDS = 32
MAX_PENDING_STATES = 32
MAX_COMMAND_AGE_SECONDS = 2
SOCKET_TIMEOUT_SECONDS = 0.1

log = logging.getLogger(__name__)


class CarouselControl:
    def __init__(self, runtime_dir):
        self.path = os.path.join(runtime_dir, CONTROL_SOCKET_NAME)
        self._commands = queue.Queue(maxsize=MAX_PENDING_COMMANDS)
        self._state = None
        self._updates = deque(maxlen=MAX_PENDING_STATES)
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._serve, name="carousel-control", daemon=True
        )

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def publish(self, state):
        with self._state_lock:
            if state != self._state:
                self._state = state
                self._updates.append(state)

    def pending_commands(self):
        # Limit work per Tk event so repeated remote input cannot starve touch.
        for _ in range(MAX_PENDING_COMMANDS):
            try:
                received_at, command = self._commands.get_nowait()
            except queue.Empty:
                return
            if time.monotonic() - received_at <= MAX_COMMAND_AGE_SECONDS:
                yield command

    def _serve(self):
        bound = False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(self.path)
                bound = True
                os.chmod(self.path, 0o600)
                listener.listen(1)
                listener.settimeout(SOCKET_TIMEOUT_SECONDS)
                while not self._stop.is_set():
                    try:
                        connection, _ = listener.accept()
                    except socket.timeout:
                        continue
                    with connection:
                        try:
                            self._handle_client(connection)
                        except OSError:
                            pass  # The display server may reconnect at any time.
        except OSError as exc:
            if not self._stop.is_set():
                log.error("Unable to serve carousel control: %s", exc)
        finally:
            if bound:
                try:
                    os.unlink(self.path)
                except FileNotFoundError:
                    pass

    def _handle_client(self, connection):
        connection.settimeout(SOCKET_TIMEOUT_SECONDS)
        with self._state_lock:
            self._updates.clear()
            if self._state is not None:
                self._updates.append(self._state)
        buffer = bytearray()
        while not self._stop.is_set():
            with self._state_lock:
                updates = list(self._updates)
                self._updates.clear()
            for state in updates:
                connection.sendall(json.dumps(state).encode() + b"\n")
            try:
                chunk = connection.recv(MAX_COMMAND_BYTES)
            except socket.timeout:
                continue
            if not chunk:
                return
            buffer.extend(chunk)
            while (newline := buffer.find(b"\n")) != -1:
                if newline > MAX_COMMAND_BYTES:
                    return
                self._receive_command(buffer[:newline])
                del buffer[:newline + 1]
            if len(buffer) > MAX_COMMAND_BYTES:
                return

    def _receive_command(self, line):
        try:
            command = json.loads(line)
        except (ValueError, RecursionError):
            return
        if isinstance(command, dict):
            try:
                self._commands.put_nowait((time.monotonic(), command))
            except queue.Full:
                pass
