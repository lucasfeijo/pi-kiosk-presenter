#!/usr/bin/env python3
"""X11/Tk controller for a single rtsp_carousel display pane."""

import argparse
import json
import logging
import os
import queue
import signal
import socket
import subprocess
import threading
import time
import urllib.request
from typing import Callable, Optional

from display_server import build_mpv_rtsp_command, validate_rtsp_carousel_pane


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] carousel: %(message)s",
)
log = logging.getLogger("rtsp-carousel")

SNAPSHOT_TIMEOUT_SECONDS = 5


def wrapped_index(index: int, count: int) -> int:
    """Wrap an index in either direction."""
    return index % count if count else 0


class SnapshotCache:
    """Warm and periodically refresh snapshot files for one carousel."""

    def __init__(
        self,
        streams: list[dict],
        cache_dir: str,
        refresh_seconds: float = 0,
        opener: Callable = urllib.request.urlopen,
    ):
        self.streams = streams
        self.cache_dir = cache_dir
        self.refresh_seconds = max(0.0, float(refresh_seconds or 0))
        self.opener = opener
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
            if stream.get("snapshot_url"):
                self.fetch_one(index, stream["snapshot_url"])

    def fetch_one(self, index: int, url: str) -> bool:
        destination = self.path_for(index)
        temporary = destination + ".part"
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "pi-display-server/rtsp-carousel"},
            )
            with self.opener(request, timeout=SNAPSHOT_TIMEOUT_SECONDS) as response:
                data = response.read()
            if not data:
                raise ValueError("empty response")
            with open(temporary, "wb") as f:
                f.write(data)
            os.replace(temporary, destination)
            log.debug("Snapshot %d refreshed from %s", index, url)
            return True
        except Exception as exc:
            log.warning("Snapshot %d refresh failed (%s): %s", index, url, exc)
            try:
                os.unlink(temporary)
            except OSError:
                pass
            return False

    def _run(self):
        # Always warm every configured endpoint once, without blocking playback.
        self.fetch_all()
        if self.refresh_seconds <= 0:
            return
        while not self.stop_event.wait(self.refresh_seconds):
            self.fetch_all()


class CarouselController:
    """Own the pane window, exactly one mpv child, overlays, and timers."""

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
        self.mpv_lock = threading.Lock()
        self.switch_lock = threading.Lock()
        self.ui_queue: queue.Queue = queue.Queue()
        self.cycle_after = None
        self.retry_after = None
        self.snapshot_photo = None
        cache_dir = os.path.join(self.runtime_dir, "snapshots")
        self.snapshots = SnapshotCache(
            self.streams,
            cache_dir,
            self.pane.get("snapshot_refresh_seconds", 0),
        )

        x, y, width, height = [int(value) for value in config["geom"]]
        self.root = tk.Tk()
        self.root.title(self.pane.get("name", "rtsp_carousel"))
        self.root.configure(background="black")
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(1, 1)
        self.root.protocol("WM_DELETE_WINDOW", self.shutdown)

        self.video_host = tk.Frame(
            self.root,
            background="black",
            borderwidth=0,
            highlightthickness=0,
        )
        self.video_host.place(x=0, y=0, relwidth=1, relheight=1)

        self.snapshot_label = tk.Label(
            self.root,
            background="black",
            borderwidth=0,
            highlightthickness=0,
        )
        self.snapshot_label.place(x=0, y=0, relwidth=1, relheight=1)

        overlay = {
            "background": "#20242a",
            "foreground": "white",
            "activebackground": "#343b44",
            "activeforeground": "white",
            "relief": "flat",
            "borderwidth": 0,
            "highlightthickness": 0,
            "takefocus": False,
        }
        self.prev_button = tk.Button(
            self.root,
            text="\u2039",
            command=lambda: self.navigate(-1),
            **overlay,
        )
        self.next_button = tk.Button(
            self.root,
            text="\u203a",
            command=lambda: self.navigate(1),
            **overlay,
        )
        self.name_label = tk.Label(
            self.root,
            background="#20242a",
            foreground="white",
            borderwidth=0,
            padx=14,
            pady=6,
        )

        self.root.bind("<Configure>", self._on_configure)
        self.root.update_idletasks()
        self.video_wid = self.video_host.winfo_id()

    def run(self):
        self.snapshots.start()
        self._apply_overlay_layout()
        self.select_index(0)
        self.root.after(50, self._drain_ui_queue)
        self.root.mainloop()

    def navigate(self, delta: int):
        if len(self.streams) < 2:
            return
        self.select_index(self.current_index + delta)

    def select_index(self, index: int):
        if self.shutting_down:
            return
        self.current_index = wrapped_index(index, len(self.streams))
        self.generation += 1
        generation = self.generation
        self.video_visible = False
        self._cancel_retry()
        self._update_name()
        self._render_snapshot(raise_layer=True)
        self._reset_cycle_timer()
        thread = threading.Thread(
            target=self._switch_worker,
            args=(generation, self.current_index),
            daemon=True,
            name=f"carousel-switch-{generation}",
        )
        thread.start()

    def _switch_worker(self, generation: int, index: int):
        with self.switch_lock:
            self._stop_mpv()
            if self.shutting_down or generation != self.generation:
                return
            self._start_mpv(generation, index)

    def _start_mpv(self, generation: int, index: int):
        ipc_path = os.path.join(self.runtime_dir, f"mpv-{generation}.sock")
        try:
            os.unlink(ipc_path)
        except OSError:
            pass
        stream = self.streams[index]
        cmd = build_mpv_rtsp_command(
            self.pane,
            stream["url"],
            title="pi-display-carousel-video",
            wid=self.video_wid,
            ipc_path=ipc_path,
        )
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

        if self.shutting_down or generation != self.generation:
            self._terminate_process(proc)
            return
        with self.mpv_lock:
            self.mpv_proc = proc

        threading.Thread(
            target=self._watch_ipc,
            args=(generation, proc, ipc_path),
            daemon=True,
            name=f"carousel-ipc-{generation}",
        ).start()
        threading.Thread(
            target=self._watch_process,
            args=(generation, proc),
            daemon=True,
            name=f"carousel-mpv-{generation}",
        ).start()

    def _watch_ipc(
        self,
        generation: int,
        proc: subprocess.Popen,
        ipc_path: str,
    ):
        deadline = time.monotonic() + 30
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
            return
        try:
            connection.settimeout(None)
            connection.sendall(
                b'{"command":["observe_property",1,"time-pos"]}\n'
            )
            with connection, connection.makefile("r", encoding="utf-8") as reader:
                for line in reader:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ready = event.get("event") == "playback-restart"
                    ready = ready or (
                        event.get("event") == "property-change"
                        and event.get("name") == "time-pos"
                        and event.get("data") is not None
                    )
                    if ready:
                        self._queue_ui(
                            self._reveal_video,
                            generation,
                            proc,
                        )
                        return
        except OSError:
            pass

    def _watch_process(self, generation: int, proc: subprocess.Popen):
        return_code = proc.wait()
        self._queue_ui(self._handle_mpv_exit, generation, proc, return_code)

    def _handle_mpv_exit(
        self,
        generation: int,
        proc: subprocess.Popen,
        return_code: int,
    ):
        with self.mpv_lock:
            if self.mpv_proc is not proc:
                return
            self.mpv_proc = None
        if self.shutting_down or generation != self.generation:
            return
        log.warning("Active mpv exited with code %s; retaining snapshot", return_code)
        self.video_visible = False
        self._render_snapshot(raise_layer=True)
        self._schedule_retry(generation)

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

    def _reveal_video(self, generation: int, proc: subprocess.Popen):
        with self.mpv_lock:
            is_current = self.mpv_proc is proc
        if self.shutting_down or generation != self.generation or not is_current:
            return
        self.video_visible = True
        self.video_host.lift(self.snapshot_label)
        self._raise_overlays()

    def _stop_mpv(self):
        with self.mpv_lock:
            proc = self.mpv_proc
            self.mpv_proc = None
        if proc is not None:
            self._terminate_process(proc)

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
        path = self.snapshots.path_for(self.current_index)
        width = max(1, self.root.winfo_width())
        height = max(1, self.root.winfo_height())
        photo = None
        if os.path.exists(path):
            try:
                with self.Image.open(path) as source:
                    source.load()
                    image = source.convert("RGB")
                image = self._fit_snapshot(image, width, height)
                photo = self.ImageTk.PhotoImage(image)
            except Exception as exc:
                log.warning("Could not render cached snapshot %s: %s", path, exc)
        self.snapshot_photo = photo
        self.snapshot_label.configure(image=photo or "", background="black")
        if raise_layer:
            self.snapshot_label.lift(self.video_host)
            self._raise_overlays()

    def _fit_snapshot(self, image, width: int, height: int):
        fit = self.pane.get("fit", "fill")
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
        if self.pane.get("show_stream_name"):
            self.name_label.configure(text=self.streams[self.current_index]["name"])
            self.name_label.place(relx=0.5, y=10, anchor="n")
        else:
            self.name_label.place_forget()
        self._raise_overlays()

    def _apply_overlay_layout(self):
        width = max(1, self.root.winfo_width())
        height = max(1, self.root.winfo_height())
        button_font = max(20, min(48, int(height * 0.11)))
        name_font = max(11, min(26, int(height * 0.045)))
        self.prev_button.configure(font=("DejaVu Sans", button_font, "bold"))
        self.next_button.configure(font=("DejaVu Sans", button_font, "bold"))
        self.name_label.configure(font=("DejaVu Sans", name_font, "bold"))
        if self.pane.get("show_controls") and len(self.streams) > 1:
            self.prev_button.place(relx=0.02, rely=0.5, anchor="w")
            self.next_button.place(relx=0.98, rely=0.5, anchor="e")
        else:
            self.prev_button.place_forget()
            self.next_button.place_forget()
        self._update_name()

    def _raise_overlays(self):
        if self.pane.get("show_stream_name"):
            self.name_label.lift()
        if self.pane.get("show_controls") and len(self.streams) > 1:
            self.prev_button.lift()
            self.next_button.lift()

    def _on_configure(self, event):
        if event.widget is not self.root or self.shutting_down:
            return
        self._apply_overlay_layout()
        self._render_snapshot(raise_layer=not self.video_visible)
        if self.video_visible:
            self.video_host.lift(self.snapshot_label)
            self._raise_overlays()

    def _reset_cycle_timer(self):
        if self.cycle_after is not None:
            try:
                self.root.after_cancel(self.cycle_after)
            except Exception:
                pass
            self.cycle_after = None
        if self.cycle_seconds > 0 and len(self.streams) > 1:
            self.cycle_after = self.root.after(
                int(self.cycle_seconds * 1000),
                lambda: self.navigate(1),
            )

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
        self._cancel_retry()
        if self.cycle_after is not None:
            try:
                self.root.after_cancel(self.cycle_after)
            except Exception:
                pass
            self.cycle_after = None
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
