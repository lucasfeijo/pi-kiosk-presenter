#!/usr/bin/env python3
"""X11/Tk controller for a single rtsp_carousel display pane."""

import argparse
import ctypes
import json
import logging
import math
import os
import queue
import signal
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from typing import Callable, Optional

from carousel_swipe import SwipeGesture
from camera_picker import CameraPicker

from display_server import (
    RTSP_STREAM_FIELDS,
    STREAM_NAME_POSITIONS,
    build_mpv_rtsp_command,
    validate_rtsp_carousel_pane,
    _run_x11,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] carousel: %(message)s",
)
log = logging.getLogger("rtsp-carousel")

SNAPSHOT_TIMEOUT_SECONDS = 5
PLAYBACK_START_TIMEOUT_SECONDS = 30
PLAYBACK_STALL_TIMEOUT_SECONDS = 15
NAME_OVERLAY_PLACEMENTS = {
    "top-left": {"relx": 0, "rely": 0, "x": 10, "y": 10, "anchor": "nw"},
    "top": {"relx": 0.5, "rely": 0, "y": 10, "anchor": "n"},
    "top-right": {"relx": 1, "rely": 0, "x": -10, "y": 10, "anchor": "ne"},
    "left": {"relx": 0, "rely": 0.5, "x": 10, "anchor": "w"},
    "center": {"relx": 0.5, "rely": 0.5, "anchor": "center"},
    "right": {"relx": 1, "rely": 0.5, "x": -10, "anchor": "e"},
    "bottom-left": {"relx": 0, "rely": 1, "x": 10, "y": -10, "anchor": "sw"},
    "bottom": {"relx": 0.5, "rely": 1, "y": -10, "anchor": "s"},
    "bottom-right": {"relx": 1, "rely": 1, "x": -10, "y": -10, "anchor": "se"},
}
assert tuple(NAME_OVERLAY_PLACEMENTS) == STREAM_NAME_POSITIONS


def selection_border_rects(width: int, height: int):
    """One-pixel scanlines of a rounded 3px ring, inset 1px from the pane."""
    def span(y, inset, radius):
        left, right = inset, width - inset
        top, bottom = inset, height - inset
        if right <= left or not top <= y < bottom:
            return None
        radius = min(radius, (right - left) / 2, (bottom - top) / 2)
        dy = max(top + radius - (y + .5), (y + .5) - (bottom - radius), 0)
        dx = radius - math.sqrt(max(0, radius * radius - dy * dy))
        return math.ceil(left + dx - .5), math.floor(right - dx - .5) + 1

    rectangles = []
    for y in range(1, height - 1):
        outer = span(y, 1, 12)
        if outer is None:
            continue
        inner = span(y, 4, 9)
        runs = [(outer[0], inner[0]), (inner[1], outer[1])] if inner else [outer]
        rectangles.extend((left, y, right - left, 1)
                          for left, right in runs if right > left)
    return rectangles


def shape_selection_border(wid: int, width: int, height: int):
    """Shape both Tk's client and wrapper; the ring never receives input."""
    class Rectangle(ctypes.Structure):
        _fields_ = [('x', ctypes.c_short), ('y', ctypes.c_short),
                    ('width', ctypes.c_ushort), ('height', ctypes.c_ushort)]

    rects = selection_border_rects(width, height)
    array = (Rectangle * len(rects))(*(Rectangle(*r) for r in rects))
    def shape(x11, display):
        ext = ctypes.CDLL('libXext.so.6')
        ext.XShapeCombineRectangles.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(Rectangle), ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ]
        root, parent = ctypes.c_ulong(), ctypes.c_ulong()
        children = ctypes.POINTER(ctypes.c_ulong)()
        count = ctypes.c_uint()
        x11.XQueryTree.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)), ctypes.POINTER(ctypes.c_uint)]
        x11.XFree.argtypes = [ctypes.c_void_p]
        windows = [wid]
        if x11.XQueryTree(display, wid, ctypes.byref(root), ctypes.byref(parent),
                         ctypes.byref(children), ctypes.byref(count)):
            if children:
                x11.XFree(children)
            if parent.value and parent.value != root.value:
                windows.append(parent.value)
        for window in windows:
            ext.XShapeCombineRectangles(display, window, 0, 0, 0, array, len(rects), 0, 0)
            ext.XShapeCombineRectangles(display, window, 2, 0, 0, None, 0, 0, 0)
    _run_x11(shape)


def primary_pointer_down() -> Optional[bool]:
    """Recover a release lost outside Tk or while an embedded player restarts."""
    result = []
    def query(x11, display):
        window = ctypes.c_ulong
        integer = ctypes.c_int
        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11.XDefaultRootWindow.restype = window
        x11.XQueryPointer.argtypes = [
            ctypes.c_void_p, window, ctypes.POINTER(window), ctypes.POINTER(window),
            *([ctypes.POINTER(integer)] * 4), ctypes.POINTER(ctypes.c_uint),
        ]
        root, child = window(), window()
        rx, ry, wx, wy = integer(), integer(), integer(), integer()
        mask = ctypes.c_uint()
        if x11.XQueryPointer(display, x11.XDefaultRootWindow(display),
                             ctypes.byref(root), ctypes.byref(child),
                             ctypes.byref(rx), ctypes.byref(ry), ctypes.byref(wx),
                             ctypes.byref(wy), ctypes.byref(mask)):
            result.append(bool(mask.value & (1 << 8)))  # Button1Mask
    try:
        _run_x11(query)
    except (OSError, RuntimeError):
        return None
    return result[0] if result else None


def wrapped_index(index: int, count: int) -> int:
    """Wrap an index in either direction."""
    return index % count if count else 0


def split_url_credentials(
    url: str,
) -> tuple[str, Optional[str], Optional[str]]:
    """Return a request-safe URL and decoded embedded credentials, if present."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.username is None:
        return url, None, None

    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    if parsed.port is not None:
        hostname = f"{hostname}:{parsed.port}"

    clean_url = urllib.parse.urlunsplit(
        (parsed.scheme, hostname, parsed.path, parsed.query, parsed.fragment)
    )
    return (
        clean_url,
        urllib.parse.unquote(parsed.username),
        urllib.parse.unquote(parsed.password or ""),
    )


def stream_playback_config(pane: dict, stream: dict) -> dict:
    """Resolve per-stream playback settings with legacy pane-level fallbacks."""
    return {
        key: stream[key] if key in stream else pane[key]
        for key in RTSP_STREAM_FIELDS
        if key in stream or key in pane
    }


def stream_name_position(pane: dict) -> Optional[str]:
    """Resolve the current name location, including the legacy boolean flag."""
    position = pane.get("stream_name_position")
    if position in NAME_OVERLAY_PLACEMENTS:
        return position
    if pane.get("show_stream_name"):
        return "top"
    return None


class SnapshotCache:
    """Warm and periodically refresh snapshot files for one carousel."""

    def __init__(
        self,
        streams: list[dict],
        cache_dir: str,
        refresh_seconds: float = 0,
        opener: Callable = urllib.request.urlopen,
        on_update: Optional[Callable[[int], None]] = None,
    ):
        self.streams = streams
        self.cache_dir = cache_dir
        self.refresh_seconds = max(0.0, float(refresh_seconds or 0))
        self.opener = opener
        self.on_update = on_update
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        os.makedirs(cache_dir, mode=0o700, exist_ok=True)

    def path_for(self, index: int) -> str:
        return os.path.join(self.cache_dir, f"snapshot-{index}.img")

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="carousel-snapshots",
        )
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=SNAPSHOT_TIMEOUT_SECONDS + 1)

    def fetch_all(self):
        for index, stream in enumerate(self.streams):
            if self.stop_event.is_set():
                return
            snapshot_url = (stream.get("snapshot_url") or "").strip()
            if snapshot_url:
                self.fetch_one(index, snapshot_url)

    def fetch_one(self, index: int, url: str) -> bool:
        destination = self.path_for(index)
        temporary = destination + ".part"
        try:
            with self._open_snapshot(url) as response:
                data = response.read()
            if not data:
                raise ValueError("empty response")
            with open(temporary, "wb") as f:
                f.write(data)
            os.replace(temporary, destination)
            if self.on_update:
                try:
                    self.on_update(index)
                except Exception:
                    log.exception("Snapshot %d update callback failed", index)
            log.debug("Snapshot %d refreshed", index)
            return True
        except Exception as exc:
            # Snapshot URLs can contain credentials. Never include the URL or an
            # exception message that may echo it in logs.
            log.warning(
                "Snapshot %d refresh failed: %s", index, type(exc).__name__
            )
            try:
                os.unlink(temporary)
            except OSError:
                pass
            return False

    def _open_snapshot(self, url: str):
        clean_url, username, password = split_url_credentials(url)
        request = urllib.request.Request(
            clean_url,
            headers={"User-Agent": "pi-display-server/rtsp-carousel"},
        )
        if username is None:
            return self.opener(request, timeout=SNAPSHOT_TIMEOUT_SECONDS)

        password_manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        password_manager.add_password(None, clean_url, username, password or "")
        authenticated_opener = urllib.request.build_opener(
            urllib.request.HTTPDigestAuthHandler(password_manager),
            urllib.request.HTTPBasicAuthHandler(password_manager),
        )
        return authenticated_opener.open(
            request, timeout=SNAPSHOT_TIMEOUT_SECONDS
        )

    def _run(self):
        # Always warm every configured endpoint once, without blocking playback.
        self.fetch_all()
        if self.refresh_seconds <= 0:
            return
        while not self.stop_event.wait(self.refresh_seconds):
            self.fetch_all()


def configure_carousel_window(root, pane: dict, geom: tuple[int, int, int, int]):
    """Configure the Tk host before it is mapped by the window manager."""
    x, y, width, height = geom
    root.title(pane.get("name", "rtsp_carousel"))
    root.overrideredirect(pane.get("hide_title_bar", True))
    root.configure(background="black")
    root.geometry(f"{width}x{height}+{x}+{y}")
    root.minsize(1, 1)


class CarouselController:
    """Own the pane window, mpv children for visible and retained streams, overlays, and timers."""

    def __init__(self, config: dict):
        import tkinter as tk
        from PIL import Image, ImageTk

        self.tk = tk
        self.Image = Image
        self.ImageTk = ImageTk
        self.config = config
        self.pane = config["pane"]
        validate_rtsp_carousel_pane(self.pane)
        self.streams = self.pane["streams"]
        self.runtime_dir = config["runtime_dir"]
        self.watchdog_interval = max(
            1.0, float(config.get("watchdog_interval", 10) or 10)
        )
        self.cycle_seconds = max(
            0.0, float(self.pane.get("cycle_seconds", 0) or 0)
        )
        self.current_index = 0
        self.generation = 0
        self.shutting_down = False
        self.video_visible = False
        self.mpv_proc: Optional[subprocess.Popen] = None
        self.players = {}
        self.player_serial = 0
        self.swipe_previews = set()
        self.retiring_players = []
        self.keep_alive_after = {}
        self.mpv_lock = threading.Lock()
        self.switch_lock = threading.Lock()
        self.ui_queue: queue.Queue = queue.Queue()
        self.cycle_after = None
        self.cycle_generation = 0
        self.pointer_held = False
        self.title_press = None
        self.retry_after = None
        self.controls_opacity = 0.2
        self.controls_after = None
        self.pointer_after = None
        self.snapshot_photo = None
        cache_dir = os.path.join(self.runtime_dir, "snapshots")
        self.snapshots = SnapshotCache(
            self.streams,
            cache_dir,
            self.pane.get("snapshot_refresh_seconds", 0),
            on_update=self._snapshot_updated,
        )

        x, y, width, height = [int(value) for value in config["geom"]]
        self.root = tk.Tk(className=f"PiDisplayCarousel{os.getpid()}")
        configure_carousel_window(self.root, self.pane, (x, y, width, height))
        self.root.protocol("WM_DELETE_WINDOW", self.shutdown)

        # Each page owns its video and fallback so both move with the finger.
        self.pages, self.video_hosts, self.snapshot_labels, self.name_labels = [], [], [], []
        self.snapshot_photos = {}
        for stream in self.streams:
            page = tk.Frame(self.root, background="black", borderwidth=0,
                            highlightthickness=0)
            page.place(x=width, y=0, relwidth=1, relheight=1)
            host = tk.Frame(page, background="black", borderwidth=0, highlightthickness=0)
            host.place(x=0, y=0, relwidth=1, relheight=1)
            snapshot = tk.Label(page, background="black", foreground="white",
                                borderwidth=0, highlightthickness=0)
            snapshot.place(x=0, y=0, relwidth=1, relheight=1)
            name = tk.Label(page, text=stream["name"], background="#20242a",
                            foreground="white", borderwidth=0, padx=14, pady=6)
            self.pages.append(page)
            self.video_hosts.append(host)
            self.snapshot_labels.append(snapshot)
            self.name_labels.append(name)
        self.video_host = self.video_hosts[0]
        self.snapshot_label = self.snapshot_labels[0]
        self.name_label = self.name_labels[0]
        self.swipe = SwipeGesture(self.root, self.root.winfo_width,
                                  self._draw_swipe, self._finish_swipe)
        self.picker = CameraPicker(tk, self.root, self._picker_closed)

        overlay = {
            "background": "#20242a",
            "foreground": "white",
            "activebackground": "#20242a",
            "activeforeground": "white",
            "relief": "flat",
            "borderwidth": 0,
            "highlightthickness": 0,
            "takefocus": False,
        }
        # Alpha belongs to these button-sized windows, never to the video pane.
        self.control_windows = []
        buttons = []
        for text, delta in (("\u2039", -1), ("\u203a", 1)):
            window = tk.Toplevel(self.root)
            window.title(f"carousel-control-{os.getpid()}-{delta}")
            window.configure(background=overlay["background"])
            window.withdraw()
            window.overrideredirect(True)
            window.transient(self.root)
            window.attributes("-alpha", 0.2)
            button = tk.Button(window, text=text,
                               command=lambda step=delta: self.navigate(step),
                               **overlay)
            button.pack()
            self.control_windows.append(window)
            buttons.append(button)
        self.prev_button, self.next_button = buttons
        self.selection_window = tk.Toplevel(self.root)
        self.selection_window.title(f"carousel-selection-{os.getpid()}")
        self.selection_window.withdraw()
        self.selection_window.overrideredirect(True)
        self.selection_window.transient(self.root)
        self.selection_window.configure(background="#60a5fa")
        self.root.bind("<Configure>", self._on_configure)
        for window in [self.root, *self.control_windows]:
            window.bind("<ButtonPress-1>", self._on_pointer_press, add="+")
            window.bind("<ButtonRelease-1>", self._on_pointer_release, add="+")
            window.bind("<B1-Motion>", self._on_pointer_motion, add="+")
            for event in ("<ButtonPress>", "<KeyPress>", "<MouseWheel>"):
                window.bind(event, self._on_interaction, add="+")
        self.root.update_idletasks()
        self.video_wids = [host.winfo_id() for host in self.video_hosts]

    def run(self):
        self.snapshots.start()
        self._apply_overlay_layout()
        self.select_index(0)
        self._poll_pointer()
        self.root.after(50, self._drain_ui_queue)
        self.root.mainloop()

    def navigate(self, delta: int):
        if len(self.streams) < 2:
            return
        self.select_index(self.current_index + delta)

    def _reset_page_positions(self):
        width = max(1, self.root.winfo_width())
        for index, page in enumerate(self.pages):
            page.place_configure(x=0 if index == self.current_index else width)
        self.pages[self.current_index].lift()

    def _draw_swipe(self, offset):
        if self.shutting_down:
            return
        if offset == 0:
            self._reset_page_positions()
            return
        width = max(1, self.root.winfo_width())
        delta = 1 if offset <= 0 else -1
        target = wrapped_index(self.current_index + delta, len(self.streams))
        if target not in self.swipe_previews:
            self.swipe_previews.add(target)
            self._cancel_keep_alive(target)
            self._render_page_snapshot(target)
            with self.mpv_lock:
                player = self.players.get(target)
            if player and player['ready'] and not player.get('restart_required'):
                self._reveal_video(target, player['proc'])
            else:
                threading.Thread(target=self._switch_worker,
                                 args=(self.generation, target), daemon=True).start()
        for index, page in enumerate(self.pages):
            if index == self.current_index:
                x = offset
            elif index == target:
                x = offset + delta * width
            else:
                x = width
            page.place_configure(x=round(x))
        self.pages[target].lift()
        self.pages[self.current_index].lift()
        self.name_labels[target].lift()
        self._raise_overlays()

    def _finish_swipe(self, delta):
        previews = self.swipe_previews
        self.swipe_previews = set()
        if self.shutting_down:
            return
        if previews:
            # A new selection generation invalidates any pending preview launch.
            self.select_index(self.current_index + delta)
            for index in previews:
                if index != self.current_index:
                    self._retire_stream(index)
        else:
            self._reset_cycle_timer()

    def select_index(self, index: int):
        if self.shutting_down:
            return
        self.swipe.cancel()
        with self.mpv_lock:
            previous = self.current_index
            self.current_index = wrapped_index(index, len(self.streams))
            self.generation += 1
            generation = self.generation
        self._cancel_keep_alive(self.current_index)
        if previous != self.current_index:
            self._retire_stream(previous)
        self.video_host = self.video_hosts[self.current_index]
        self.snapshot_label = self.snapshot_labels[self.current_index]
        self.name_label = self.name_labels[self.current_index]
        self._reset_page_positions()
        with self.mpv_lock:
            player = self.players.get(self.current_index)
            self.mpv_proc = player["proc"] if player else None
        self.video_visible = False
        self._cancel_retry()
        self._update_name()
        self._render_snapshot(raise_layer=True)
        self._reset_cycle_timer()
        if (player and player["ready"] and not player.get("restart_required")
                and player["proc"].poll() is None):
            self._reveal_video(self.current_index, player["proc"])
        thread = threading.Thread(
            target=self._switch_worker,
            args=(generation, self.current_index),
            daemon=True,
            name=f"carousel-switch-{generation}",
        )
        thread.start()

    def _switch_worker(self, generation: int, index: int, reconnect: bool = False):
        with self.switch_lock:
            if self.shutting_down or generation != self.generation:
                return
            self._sync_audio()
            if not (self.streams[index].get("url") or "").strip():
                log.info(
                    "Displaying snapshot-only stream %d/%d '%s'",
                    index + 1,
                    len(self.streams),
                    self.streams[index]["name"],
                )
                return
            with self.mpv_lock:
                player = self.players.get(index)
                reconnect = reconnect or bool(player and player.get("restart_required"))
                player = self.players.pop(index, None) if reconnect else self.players.get(index)
                if reconnect and index == self.current_index:
                    self.mpv_proc = None
            if reconnect and player:
                self._terminate_process(player["proc"])
                player = None
            if not player or player["proc"].poll() is not None:
                self._start_mpv(generation, index)
            self._sync_audio()

    def _restart_current(self):
        index = self.current_index
        if not (self.streams[index].get("url") or "").strip():
            return
        with self.mpv_lock:
            self.generation += 1
            generation = self.generation
        self._cancel_retry()
        self.video_visible = False
        self._render_snapshot(raise_layer=True)
        threading.Thread(
            target=self._switch_worker,
            args=(generation, index, True),
            daemon=True,
            name=f"carousel-reconnect-{generation}",
        ).start()

    def _start_mpv(self, generation: int, index: int):
        self.player_serial += 1
        ipc_path = os.path.join(self.runtime_dir, f"mpv-{generation}-{self.player_serial}.sock")
        try:
            os.unlink(ipc_path)
        except OSError:
            pass
        stream = self.streams[index]
        url = (stream.get("url") or "").strip()
        playback_config = stream_playback_config(self.pane, stream)
        cmd = build_mpv_rtsp_command(
            playback_config,
            url,
            title="pi-display-carousel-video",
            wid=self.video_wids[index],
            ipc_path=ipc_path,
        )
        if index != self.current_index:
            cmd.append('--mute=yes')
        log.info(
            "Starting stream %d/%d '%s'",
            index + 1,
            len(self.streams),
            stream["name"],
        )
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            log.exception("Could not start mpv for '%s'", stream["name"])
            self._queue_ui(self._handle_start_failure, generation)
            return

        with self.mpv_lock:
            stale = self.shutting_down or generation != self.generation
            if not stale:
                self.players[index] = {
                    "proc": proc, "ipc_path": ipc_path, "ready": False
                }
                if index == self.current_index:
                    self.mpv_proc = proc
        if stale:
            self._terminate_process(proc)
            return

        threading.Thread(
            target=self._watch_ipc,
            args=(index, proc, ipc_path),
            daemon=True,
            name=f"carousel-ipc-{generation}",
        ).start()
        threading.Thread(
            target=self._watch_process,
            args=(index, proc),
            daemon=True,
            name=f"carousel-mpv-{generation}",
        ).start()

    def _snapshot_updated(self, index: int):
        self._queue_ui(self._apply_snapshot_update, index)

    def _apply_snapshot_update(self, index: int):
        if not self.shutting_down and index in self.swipe_previews and index != self.current_index:
            if not (self.streams[index].get('url') or '').strip():
                self._render_page_snapshot(index)
            return
        if self.shutting_down or index != self.current_index:
            return
        if not (self.streams[index].get("url") or "").strip():
            self.video_visible = False
            self._render_snapshot(raise_layer=True)

    def _watch_ipc(
        self,
        index: int,
        proc: subprocess.Popen,
        ipc_path: str,
    ):
        started = time.monotonic()
        deadline = started + PLAYBACK_START_TIMEOUT_SECONDS
        minutes = self.pane.get("reconnect_minutes")
        reconnect_at = started + minutes * 60 if minutes else None
        if reconnect_at is not None:
            deadline = min(deadline, reconnect_at)
        connection = None
        while (
            time.monotonic() < deadline
            and not self.shutting_down
            and proc.poll() is None
        ):
            try:
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.settimeout(1)
                connection.connect(ipc_path)
                break
            except OSError:
                if connection:
                    connection.close()
                connection = None
                time.sleep(0.1)
        if not connection:
            self._queue_ui(self._handle_mpv_stall, index, proc)
            return
        position = None
        last_progress = started
        playing = False
        buffer = b""
        try:
            connection.settimeout(1)
            connection.sendall(
                b'{"command":["observe_property",1,"time-pos"]}\n'
            )
            with connection:
                while not self.shutting_down and proc.poll() is None:
                    if reconnect_at is not None and time.monotonic() >= reconnect_at:
                        self._queue_ui(self._handle_mpv_reconnect, index, proc)
                        return
                    try:
                        data = connection.recv(65536)
                        if not data:
                            break
                        buffer += data
                    except socket.timeout:
                        pass
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        try:
                            event = json.loads(line)
                        except (ValueError, UnicodeDecodeError):
                            continue
                        value = event.get("data")
                        if (event.get("event") == "property-change"
                                and event.get("name") == "time-pos"
                                and isinstance(value, (int, float))
                                and value != position
                                and (position is not None or value > 0)):
                            position = value
                            last_progress = time.monotonic()
                            if not playing:
                                playing = True
                                self._queue_ui(self._reveal_video, index, proc)
                    timeout = (PLAYBACK_STALL_TIMEOUT_SECONDS if playing
                               else PLAYBACK_START_TIMEOUT_SECONDS)
                    if time.monotonic() - last_progress >= timeout:
                        break
        except OSError:
            pass
        finally:
            connection.close()
        self._queue_ui(self._handle_mpv_stall, index, proc)

    def _handle_mpv_stall(self, index: int, proc: subprocess.Popen):
        self._restart_player(index, proc, "video stopped advancing")

    def _handle_mpv_reconnect(self, index: int, proc: subprocess.Popen):
        self._restart_player(index, proc, "scheduled connection limit reached")

    def _restart_player(self, index: int, proc: subprocess.Popen, reason: str):
        if self.shutting_down or proc.poll() is not None:
            return
        with self.mpv_lock:
            player = self.players.get(index)
            if not player or player["proc"] is not proc:
                return
            # Keep the request attached to this connection if navigation makes
            # the queued switch worker stale before it can replace the player.
            player["restart_required"] = True
        log.info("Reconnecting stream '%s': %s", self.streams[index]["name"], reason)
        if index == self.current_index:
            self._restart_current()
        else:
            self._cancel_keep_alive(index)
            self._expire_stream(index)

    def _watch_process(self, index: int, proc: subprocess.Popen):
        return_code = proc.wait()
        self._queue_ui(self._handle_mpv_exit, index, proc, return_code)

    def _handle_mpv_exit(
        self,
        index: int,
        proc: subprocess.Popen,
        return_code: int,
    ):
        with self.mpv_lock:
            player = self.players.get(index)
            if not player or player["proc"] is not proc:
                return
            del self.players[index]
        self._cancel_keep_alive(index)
        if self.shutting_down or index != self.current_index:
            return
        self.mpv_proc = None
        log.warning("Active mpv exited with code %s; retaining snapshot", return_code)
        self.video_visible = False
        self._render_snapshot(raise_layer=True)
        self._schedule_retry(self.generation)

    def _handle_start_failure(self, generation: int):
        if self.shutting_down or generation != self.generation:
            return
        self._schedule_retry(generation)

    def _schedule_retry(self, generation: int):
        self._cancel_retry()
        self.retry_after = self.root.after(
            int(self.watchdog_interval * 1000),
            lambda: self._retry_current(generation),
        )

    def _retry_current(self, generation: int):
        self.retry_after = None
        if self.shutting_down or generation != self.generation:
            return
        thread = threading.Thread(
            target=self._switch_worker,
            args=(generation, self.current_index),
            daemon=True,
            name=f"carousel-retry-{generation}",
        )
        thread.start()

    def _reveal_video(self, index: int, proc: subprocess.Popen):
        with self.mpv_lock:
            player = self.players.get(index)
            if not player or player["proc"] is not proc or player.get("restart_required"):
                return
            player["ready"] = True
        threading.Thread(target=self._sync_audio, daemon=True).start()
        if self.shutting_down:
            return
        if index != self.current_index:
            if index in self.swipe_previews:
                self.video_hosts[index].lift(self.snapshot_labels[index])
                self.name_labels[index].lift()
            return
        self.video_visible = True
        self.video_host.lift(self.snapshot_label)
        self._raise_overlays()

    def _sync_audio(self):
        # Hidden streams keep decoding, but only the selected camera is audible.
        with self.mpv_lock:
            for index, player in self.players.items():
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as ipc:
                        ipc.settimeout(0.2)
                        ipc.connect(player["ipc_path"])
                        ipc.sendall((json.dumps({"command": [
                            "set_property", "mute", index != self.current_index
                        ]}) + "\n").encode())
                except OSError:
                    pass

    def _cancel_keep_alive(self, index):
        timer = self.keep_alive_after.pop(index, None)
        if timer is not None:
            self.root.after_cancel(timer)

    def _retire_stream(self, index):
        self._cancel_keep_alive(index)
        seconds = self.streams[index].get("keep_alive_seconds")
        with self.mpv_lock:
            player = self.players.get(index)
            restart_required = bool(player and player.get("restart_required"))
        if seconds and not restart_required:
            self.keep_alive_after[index] = self.root.after(
                seconds * 1000, lambda: self._expire_stream(index)
            )
        else:
            self._expire_stream(index)

    def _expire_stream(self, index):
        self.keep_alive_after.pop(index, None)
        if index == self.current_index and not self.shutting_down:
            return
        with self.mpv_lock:
            player = self.players.pop(index, None)
            if player:
                self.retiring_players.append(player)
        if player:
            threading.Thread(target=self._finish_retiring,
                             args=(player,), daemon=True).start()

    def _finish_retiring(self, player):
        self._terminate_process(player["proc"])
        with self.mpv_lock:
            if player in self.retiring_players:
                self.retiring_players.remove(player)

    def _stop_mpv(self):
        with self.mpv_lock:
            players = list(self.players.values()) + self.retiring_players
            self.retiring_players = []
            self.players.clear()
            self.mpv_proc = None
        for player in players:
            self._terminate_process(player["proc"])

    @staticmethod
    def _terminate_process(proc: subprocess.Popen):
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                log.warning("mpv pid=%d did not exit after SIGKILL", proc.pid)

    def _render_snapshot(self, raise_layer: bool):
        self._render_page_snapshot(self.current_index, raise_layer)

    def _render_page_snapshot(self, index, raise_layer=True):
        path = self.snapshots.path_for(index)
        width = max(1, self.root.winfo_width())
        height = max(1, self.root.winfo_height())
        photo = None
        if os.path.exists(path):
            try:
                with self.Image.open(path) as source:
                    source.load()
                    image = source.convert("RGB")
                image = self._fit_snapshot(image, width, height, index)
                photo = self.ImageTk.PhotoImage(image)
            except Exception as exc:
                log.warning("Could not render cached snapshot %s: %s", path, exc)
        self.snapshot_photos[index] = photo
        label = self.snapshot_labels[index]
        label.configure(image=photo or "", text="", background="black")
        if raise_layer:
            label.lift()
            self._raise_overlays()

    def _fit_snapshot(self, image, width: int, height: int, index=None):
        fit = stream_playback_config(
            self.pane, self.streams[self.current_index if index is None else index]
        ).get("fit", "fill")
        resampling = getattr(self.Image, "Resampling", self.Image).LANCZOS
        if fit == "fill":
            return image.resize((width, height), resampling)

        src_w, src_h = image.size
        if fit == "cover":
            scale = max(width / src_w, height / src_h)
            resized = image.resize(
                (max(1, round(src_w * scale)), max(1, round(src_h * scale))),
                resampling,
            )
            left = max(0, (resized.width - width) // 2)
            top = max(0, (resized.height - height) // 2)
            return resized.crop((left, top, left + width, top + height))

        scale = min(width / src_w, height / src_h)
        resized = image.resize(
            (max(1, round(src_w * scale)), max(1, round(src_h * scale))),
            resampling,
        )
        canvas = self.Image.new("RGB", (width, height), "black")
        canvas.paste(
            resized,
            ((width - resized.width) // 2, (height - resized.height) // 2),
        )
        return canvas

    def _update_name(self):
        position = stream_name_position(self.pane)
        for label in self.name_labels:
            if position and not self.picker.is_open:
                label.place(**NAME_OVERLAY_PLACEMENTS[position])
            else:
                label.place_forget()
        self._raise_overlays()

    def _apply_overlay_layout(self):
        width = max(1, self.root.winfo_width())
        height = max(1, self.root.winfo_height())
        button_font = max(20, min(48, int(height * 0.11)))
        configured_name_font = self.pane.get("stream_name_font_size")
        name_font = (
            max(1, int(float(configured_name_font)))
            if configured_name_font not in (None, "")
            else max(11, min(26, int(height * 0.045)))
        )
        self.prev_button.configure(font=("DejaVu Sans", button_font, "bold"))
        self.next_button.configure(font=("DejaVu Sans", button_font, "bold"))
        # Tk uses negative font sizes to request exact pixel sizing.
        for label in self.name_labels:
            label.configure(font=("DejaVu Sans", -name_font, "bold"))
        if self.pane.get("show_controls") and len(self.streams) > 1 and not self.picker.is_open:
            self.root.update_idletasks()
            root_x, root_y = self.root.winfo_rootx(), self.root.winfo_rooty()
            for index, (window, button) in enumerate(zip(
                self.control_windows, (self.prev_button, self.next_button)
            )):
                button_w, button_h = button.winfo_reqwidth(), button.winfo_reqheight()
                left = round(width * 0.02) if index == 0 else round(width * 0.98) - button_w
                top = round((height - button_h) / 2)
                window.geometry(f"{button_w}x{button_h}{root_x + left:+d}{root_y + top:+d}")
                window.deiconify()
                # Tk resets alpha set before the first map on X11.
                window.update_idletasks()
                window.attributes("-alpha", self.controls_opacity)
        else:
            for window in self.control_windows:
                window.withdraw()
        self._update_name()
        self.picker.layout()

    def _raise_overlays(self):
        if self.pointer_held:
            self.selection_window.lift(self.root)
        if stream_name_position(self.pane) and not self.picker.is_open:
            self.name_label.lift()
        if self.picker.is_open:
            self.picker.window.lift(self.root)
        elif self.pane.get("show_controls") and len(self.streams) > 1:
            for window in self.control_windows:
                window.lift(self.root)

    def _on_pointer_press(self, _event=None):
        if self.shutting_down or self.pointer_held or self.picker.is_open:
            return
        self.pointer_held = True
        self.title_press = None
        if _event is not None and _event.widget in self.name_labels:
            self.title_press = (_event.widget, _event.x_root, _event.y_root)
        if (_event is not None and self.pane.get("show_controls")
                and len(self.streams) > 1
                and _event.widget not in (self.prev_button, self.next_button)):
            self.swipe.begin(_event.x_root, _event.y_root)
        self._update_selection_border()
        self._reset_cycle_timer()
        self._on_interaction()

    def _on_pointer_release(self, _event=None):
        if self.shutting_down or not self.pointer_held:
            return
        self.pointer_held = False
        self._update_selection_border()
        if self.controls_after is not None:
            self.root.after_cancel(self.controls_after)
        self._dim_controls()
        coords = ((_event.x_root, _event.y_root) if _event is not None
                  else self.root.winfo_pointerxy())
        title_press, self.title_press = self.title_press, None
        if self.swipe.tracking:
            self.swipe.release(*coords)
        if title_press is not None:
            label, x, y = title_press
            if (max(abs(coords[0] - x), abs(coords[1] - y)) < 12
                    and label.winfo_rootx() <= coords[0] < label.winfo_rootx() + label.winfo_width()
                    and label.winfo_rooty() <= coords[1] < label.winfo_rooty() + label.winfo_height()):
                self.picker.show(self.name_label, [s["name"] for s in self.streams],
                                 self.current_index)
                self._apply_overlay_layout()
        self._reset_cycle_timer()

    def _on_pointer_motion(self, event):
        if self.pointer_held:
            if self.title_press is not None:
                _, x, y = self.title_press
                if max(abs(event.x_root - x), abs(event.y_root - y)) >= 12:
                    self.title_press = None
            self.swipe.move(event.x_root, event.y_root)

    def _picker_closed(self, index):
        if self.shutting_down:
            return
        if index is not None:
            self.select_index(index)
        self._apply_overlay_layout()
        self._reset_cycle_timer()

    def _update_selection_border(self):
        if not self.pointer_held or self.shutting_down:
            self.selection_window.withdraw()
            return
        width, height = self.root.winfo_width(), self.root.winfo_height()
        x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
        self.selection_window.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self.selection_window.update_idletasks()
        try:
            shape_selection_border(self.selection_window.winfo_id(), width, height)
        except (OSError, RuntimeError):
            log.exception("Could not shape selection border")
            self.selection_window.withdraw()
            return
        self.selection_window.deiconify()
        self._raise_overlays()

    def _on_interaction(self, _event=None):
        if self.shutting_down or self.picker.is_open or not self.pane.get("show_controls"):
            return
        if self.controls_after is not None:
            self.root.after_cancel(self.controls_after)
        self.controls_opacity = 1.0
        for window in self.control_windows:
            window.attributes("-alpha", self.controls_opacity)
        self.controls_after = self.root.after(3000, self._dim_controls)

    def _dim_controls(self):
        self.controls_after = None
        if self.shutting_down:
            return
        if self.pointer_held:
            return
        self.controls_opacity = 0.2
        for window in self.control_windows:
            window.attributes("-alpha", self.controls_opacity)

    def _poll_pointer(self):
        # Only recover a lost release. A touchscreen leaves the pointer at the
        # last touch, and cycling/replacing video windows can generate Enter
        # events there without any new user interaction.
        if self.shutting_down:
            return
        if self.pointer_held and primary_pointer_down() is False:
            self._on_pointer_release()
        self.pointer_after = self.root.after(100, self._poll_pointer)

    def _on_configure(self, event):
        if event.widget is not self.root or self.shutting_down:
            return
        self.swipe.cancel()
        self._reset_page_positions()
        self._apply_overlay_layout()
        self._update_selection_border()
        self._render_snapshot(raise_layer=not self.video_visible)
        if self.video_visible:
            self.video_host.lift(self.snapshot_label)
            self._raise_overlays()

    def _reset_cycle_timer(self):
        self.cycle_generation += 1
        if self.cycle_after is not None:
            try:
                self.root.after_cancel(self.cycle_after)
            except Exception:
                pass
            self.cycle_after = None
        if (not self.shutting_down and not self.pointer_held and not self.swipe.busy
                and not self.picker.is_open
                and self.cycle_seconds > 0 and len(self.streams) > 1):
            generation = self.cycle_generation
            self.cycle_after = self.root.after(
                int(self.cycle_seconds * 1000),
                lambda: self._advance_cycle(generation),
            )

    def _advance_cycle(self, generation):
        if (self.shutting_down or self.pointer_held or self.swipe.busy or self.picker.is_open
                or generation != self.cycle_generation):
            return
        self.cycle_after = None
        self.navigate(1)

    def _cancel_retry(self):
        if self.retry_after is not None:
            try:
                self.root.after_cancel(self.retry_after)
            except Exception:
                pass
            self.retry_after = None

    def _queue_ui(self, callback: Callable, *args):
        if not self.shutting_down:
            self.ui_queue.put((callback, args))

    def _drain_ui_queue(self):
        if self.shutting_down:
            return
        while True:
            try:
                callback, args = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            callback(*args)
        self.root.after(50, self._drain_ui_queue)

    def shutdown(self):
        if self.shutting_down:
            return
        self.shutting_down = True
        self.picker.close(notify=False)
        self.swipe.cancel()
        for timer in (self.controls_after, self.pointer_after):
            if timer is not None:
                self.root.after_cancel(timer)
        self.controls_after = None
        self.pointer_after = None
        self._cancel_retry()
        if self.cycle_after is not None:
            try:
                self.root.after_cancel(self.cycle_after)
            except Exception:
                pass
            self.cycle_after = None
        for index in list(self.keep_alive_after):
            self._cancel_keep_alive(index)
        self.snapshots.stop()
        with self.switch_lock:
            self._stop_mpv()
        try:
            self.root.destroy()
        except Exception:
            pass


def load_config(path: str) -> dict:
    with open(path) as f:
        config = json.load(f)
    if not isinstance(config, dict) or not isinstance(config.get("pane"), dict):
        raise ValueError("carousel config must contain a pane object")
    validate_rtsp_carousel_pane(config["pane"])
    return config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    controller = CarouselController(config)

    def handle_signal(_sig, _frame):
        controller.shutdown()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    try:
        controller.run()
    finally:
        controller.shutdown()


if __name__ == "__main__":
    main()
