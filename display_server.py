#!/usr/bin/env python3
"""
Pi Display Server — remote control what your Raspberry Pi shows on HDMI.

Runs an HTTP server that accepts JSON layout descriptions and manages X11
windows accordingly (mpv for RTSP/video, chromium for web, feh for images, etc.).

Requires: Python 3.9+, xdotool, xdpyinfo, mpv, chromium-browser, feh
"""

import json
import logging
import os
import shlex
import signal
import ssl
import subprocess
import sys
import time
from dataclasses import dataclass, field
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Event, Lock, RLock, Thread
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("display-server")

LAYOUT_FILE = os.environ.get("LAYOUT_FILE", "/opt/pi-display-server/layout.json")
WATCHDOG_INTERVAL = int(os.environ.get("WATCHDOG_INTERVAL", "10"))
# Raspberry Pi: --hwdec=auto never picks V4L2 (tries Vulkan/VDPAU/VA-API first and
# falls back to software). Use bcm2835 h264_v4l2m2m via v4l2m2m-copy. Set to
# "auto" or "no" on other hosts if needed.
# H.265 / HEVC mains (e.g. Intelbras subtype=0): use per-pane "hwdec": "drm-copy"
# — v4l2m2m often has no working HEVC device; drm-copy uses the Pi HEVC block.
MPV_HWDEC = os.environ.get("MPV_HWDEC", "v4l2m2m-copy")
# Extra low-latency RTSP options (demuxer, no audio, swap interval). Set to 0/false/no to disable.
MPV_RTSP_FAST = os.environ.get("MPV_RTSP_FAST", "1").lower() not in ("0", "false", "no")
# Chromium profiles (cookies, logins): persist under this directory per pane name.
# Override with CHROMIUM_USER_DATA_ROOT=/path (e.g. on a larger SD partition).
_DEFAULT_CHROMIUM_ROOT = os.path.join(
    os.path.expanduser("~"), ".local/share/pi-display-server/chromium"
)
CHROMIUM_USER_DATA_ROOT = os.environ.get(
    "CHROMIUM_USER_DATA_ROOT", _DEFAULT_CHROMIUM_ROOT
)


def _chromium_user_data_dir(pane: dict) -> str:
    """Per-web-pane profile directory — persistent (not /tmp) so sessions survive reboot."""
    name = pane.get("name", "web") or "web"
    safe = name.replace(os.sep, "_").replace("\x00", "").strip() or "web"
    root = os.path.expanduser(CHROMIUM_USER_DATA_ROOT.strip() or _DEFAULT_CHROMIUM_ROOT)
    path = os.path.join(root, safe)
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError as e:
        log.warning("Could not create chromium profile dir %s: %s", path, e)
    return path


def _is_hevc_pane(pane: dict) -> bool:
    """Best-effort guess: the pane carries an H.265/HEVC stream."""
    hwdec = (pane.get("hwdec") or "").lower()
    if hwdec in ("drm-copy", "drm"):
        return True
    url = (pane.get("url") or "").lower()
    # Intelbras/Dahua subtype=0 is always the main (H.265) stream
    if "subtype=0" in url:
        return True
    return False


def _mpv_rtsp_perf_args(pane: dict) -> list[str]:
    """Low-latency RTSP args — relaxed for H.265 streams that lack PTS."""
    args: list[str] = []
    transport = (pane.get("rtsp_transport") or os.environ.get("MPV_RTSP_TRANSPORT", "")).strip().lower()
    if transport in ("tcp", "udp"):
        args.append(f"--demuxer-lavf-o=rtsp_transport={transport}")
    if not MPV_RTSP_FAST:
        extra = os.environ.get("MPV_EXTRA_ARGS", "").strip()
        if extra:
            args.extend(shlex.split(extra))
        return args
    if not pane.get("audio"):
        args.append("--no-audio")

    hevc = _is_hevc_pane(pane)
    if hevc:
        # H.265 main streams often have no PTS — need a small cache and
        # enough probesize for the demuxer to find HEVC parameter sets.
        args.extend(
            [
                "--cache=yes",
                "--demuxer-max-bytes=512KiB",
                "--demuxer-readahead-secs=0.5",
                "--demuxer-lavf-analyzeduration=1",
                "--demuxer-lavf-probesize=524288",
            ]
        )
    else:
        args.extend(
            [
                "--cache=no",
                "--demuxer-lavf-analyzeduration=0",
                "--demuxer-lavf-probesize=32768",
                "--demuxer-lavf-o=fflags=+nobuffer",
                "--opengl-swapinterval=0",
            ]
        )
    extra = os.environ.get("MPV_EXTRA_ARGS", "").strip()
    if extra:
        args.extend(shlex.split(extra))
    return args


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_screen_resolution(retries: int = 30, delay: float = 2.0) -> tuple[int, int]:
    """Return (width, height) of the current X display, waiting for X if needed."""
    for attempt in range(retries):
        try:
            out = subprocess.check_output(
                ["xdpyinfo"], text=True, stderr=subprocess.DEVNULL
            )
            for line in out.splitlines():
                if "dimensions:" in line:
                    dims = line.split()[1]  # "1920x1080"
                    w, h = dims.split("x")
                    return int(w), int(h)
        except subprocess.CalledProcessError:
            pass
        if attempt < retries - 1:
            log.info("Waiting for X display… (attempt %d/%d)", attempt + 1, retries)
            time.sleep(delay)
    raise RuntimeError("Could not connect to X display after %d attempts" % retries)


def find_window_by_pid(pid: int, retries: int = 30, delay: float = 0.5) -> Optional[int]:
    """Try to find an X window ID owned by *pid*.  Returns None on failure."""
    for _ in range(retries):
        try:
            out = subprocess.check_output(
                ["xdotool", "search", "--pid", str(pid)],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            if out:
                # May return multiple lines — take the last (usually the real window)
                return int(out.strip().splitlines()[-1])
        except subprocess.CalledProcessError:
            pass
        time.sleep(delay)
    return None


def find_window_by_name(name: str, retries: int = 30, delay: float = 0.5) -> Optional[int]:
    """Try to find an X window by name/title substring."""
    for _ in range(retries):
        try:
            out = subprocess.check_output(
                ["xdotool", "search", "--name", name],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            if out:
                return int(out.strip().splitlines()[-1])
        except subprocess.CalledProcessError:
            pass
        time.sleep(delay)
    return None


def _pane_stack_sort_key(pane: dict, index: int) -> tuple[int, int]:
    """Sort key for stacking: lower (order, index) = further back; higher = on top."""
    raw = pane.get("order", pane.get("z"))
    try:
        o = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        o = 0
    return (o, index)


def raise_window_stack(layout: list[dict], panes: dict[str, "ManagedPane"]):
    """Raise each mapped window in stack order (last raised ends on top)."""
    indexed = list(enumerate(layout))
    indexed.sort(key=lambda iv: _pane_stack_sort_key(iv[1], iv[0]))
    for _, pane in indexed:
        name = pane.get("name", pane.get("type", ""))
        mp = panes.get(name)
        wid = mp.wid if mp and mp.wid else None
        if wid:
            subprocess.run(
                ["xdotool", "windowraise", str(wid)],
                stderr=subprocess.DEVNULL,
            )


def position_window(wid: int, x: int, y: int, w: int, h: int):
    """Move and resize a window by its X window id."""
    # Remove any maximized / fullscreen state first so resize works
    subprocess.run(
        ["xdotool", "windowstate", "--remove", "MAXIMIZED_VERT", str(wid)],
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["xdotool", "windowstate", "--remove", "MAXIMIZED_HORZ", str(wid)],
        stderr=subprocess.DEVNULL,
    )
    # Some WMs need a small delay after state change
    time.sleep(0.1)
    subprocess.run(
        ["xdotool", "windowmove", "--sync", str(wid), str(x), str(y)],
        check=True,
    )
    subprocess.run(
        ["xdotool", "windowsize", "--sync", str(wid), str(w), str(h)],
        check=True,
    )
    # Remove window decorations via xprop (works with most WMs)
    subprocess.run(
        [
            "xprop",
            "-id", str(wid),
            "-f", "_MOTIF_WM_HINTS", "32c",
            "-set", "_MOTIF_WM_HINTS", "2, 0, 0, 0, 0",
        ],
        stderr=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# Positioning
# ---------------------------------------------------------------------------

def resolve_region(pane: dict, sw: int, sh: int) -> tuple[int, int, int, int]:
    """
    Return (x, y, w, h) in pixels for a pane definition.
    Values can be floats 0.0–1.0 (fractions of screen) or absolute pixels.
    """
    def to_px(val, total):
        v = float(val)
        if 0.0 < v <= 1.0:
            return int(v * total)
        if v == 0.0:
            return 0
        return int(v)

    return (
        to_px(pane.get("x", 0), sw),
        to_px(pane.get("y", 0), sh),
        to_px(pane.get("w", 1.0), sw),
        to_px(pane.get("h", 1.0), sh),
    )


# ---------------------------------------------------------------------------
# Pane launchers
# ---------------------------------------------------------------------------

@dataclass
class ManagedPane:
    name: str
    ptype: str
    proc: subprocess.Popen
    wid: Optional[int] = None
    _refresh_stop: Optional[Event] = field(default=None, repr=False)
    _refresh_thread: Optional[Thread] = field(default=None, repr=False)


class DisplayManager:
    """Keeps track of all running panes and their processes."""

    def __init__(self):
        self.panes: dict[str, ManagedPane] = {}
        self.lock = RLock()
        self.screen_w, self.screen_h = get_screen_resolution()
        self._current_layout: list[dict] = []
        self._stop_event = Event()
        self._watchdog_thread = Thread(target=self._watchdog, daemon=True)
        self._watchdog_thread.start()
        log.info("Screen resolution: %dx%d", self.screen_w, self.screen_h)

    # -- layout persistence -------------------------------------------------

    def _save_layout(self):
        """Persist the current layout definition to disk."""
        try:
            with open(LAYOUT_FILE, "w") as f:
                json.dump(self._current_layout, f, indent=2)
            log.info("Layout saved to %s (%d panes)", LAYOUT_FILE, len(self._current_layout))
        except Exception as e:
            log.warning("Failed to save layout: %s", e)

    def load_saved_layout(self):
        """Load and apply the previously saved layout, if any."""
        if not os.path.exists(LAYOUT_FILE):
            log.info("No saved layout found at %s", LAYOUT_FILE)
            return
        try:
            with open(LAYOUT_FILE) as f:
                layout = json.load(f)
            if layout:
                log.info("Restoring saved layout (%d panes)", len(layout))
                self.apply_layout(layout)
        except Exception as e:
            log.warning("Failed to load saved layout: %s", e)

    # -- launchers ----------------------------------------------------------

    def _launch_rtsp(self, pane: dict, geom: tuple[int, int, int, int]) -> subprocess.Popen:
        url = pane["url"]
        extra = pane.get("mpv_args", [])
        fit = pane.get("fit", "fill")
        x, y, w, h = geom

        # fit modes:
        #   "fill"    — stretch to fill the region exactly (no aspect ratio)
        #   "cover"   — keep aspect ratio, crop to fill (no black bars)
        #   "contain" — keep aspect ratio, fit inside (may have black bars)
        if fit == "fill":
            aspect_args = ["--keepaspect=no"]
        elif fit == "cover":
            aspect_args = [
                "--keepaspect=yes",
                "--panscan=1.0",
                "--video-align-x=0",
                "--video-align-y=0",
            ]
        elif fit == "contain":
            aspect_args = ["--keepaspect=yes"]
        else:
            log.warning("Unknown fit mode '%s', defaulting to fill", fit)
            aspect_args = ["--keepaspect=no"]

        name = pane.get("name", "rtsp")
        hwdec = pane.get("hwdec") or MPV_HWDEC
        cmd = [
            "mpv",
            f"--title={name}",
            "--no-terminal",
            "--no-osc",
            "--no-input-default-bindings",
            "--force-window=yes",
            "--no-border",
            "--no-keepaspect-window",
            f"--hwdec={hwdec}",
            f"--geometry={w}x{h}+{x}+{y}",
            f"--autofit={w}x{h}",
            *aspect_args,
            "--profile=low-latency",
            *_mpv_rtsp_perf_args(pane),
            url,
            *extra,
        ]
        log.info("Launching mpv: %s", " ".join(cmd))
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _launch_web(self, pane: dict, geom: tuple[int, int, int, int]) -> subprocess.Popen:
        url = pane["url"]
        extra = pane.get("chromium_args", [])
        x, y, w, h = geom
        # Each web pane gets its own user-data-dir so multiple instances work
        name = pane.get("name", "web")
        data_dir = _chromium_user_data_dir(pane)
        cmd = [
            "chromium",
            f"--app={url}",
            "--noerrdialogs",
            "--disable-infobars",
            "--disable-session-crashed-bubble",
            "--enable-gpu-rasterization",
            "--enable-oop-rasterization",
            "--use-gl=egl",
            "--ignore-gpu-blocklist",
            "--enable-zero-copy",
            "--num-raster-threads=2",
            "--enable-features=VaapiVideoDecoder",
            "--disable-features=VizDisplayCompositor,UseChromeOSDirectVideoDecoder",
            "--disable-extensions",
            "--disable-dev-shm-usage",
            "--disable-smooth-scrolling",
            "--disable-background-timer-throttling",
            "--overscroll-history-navigation=0",
            "--memory-model=low",
            "--process-per-site",
            "--renderer-process-limit=2",
            f"--window-position={x},{y}",
            f"--window-size={w},{h}",
            f"--user-data-dir={data_dir}",
            *extra,
        ]
        log.info("Launching chromium: %s", " ".join(cmd))
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _launch_image(self, pane: dict, geom: tuple[int, int, int, int]) -> subprocess.Popen:
        path = pane["path"]
        x, y, w, h = geom
        name = pane.get("name", "image")
        cmd = ["feh", f"--title={name}", "--scale-down", "--auto-zoom", "--borderless",
               f"--geometry={w}x{h}+{x}+{y}", path]
        log.info("Launching feh: %s", " ".join(cmd))
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _launch_command(self, pane: dict, geom: tuple[int, int, int, int]) -> subprocess.Popen:
        """Generic: run any command that creates an X window."""
        cmd = pane["cmd"]
        if isinstance(cmd, str):
            cmd = cmd.split()
        log.info("Launching command: %s", " ".join(cmd))
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _launch_stats(self, pane: dict, geom: tuple[int, int, int, int]) -> subprocess.Popen:
        """Launch a chromium window showing the built-in system stats page."""
        port = int(os.environ.get("DISPLAY_PORT", "8686"))
        has_ssl = os.environ.get("SSL_SELFSIGNED", "").lower() in ("1", "true", "yes") or (
            os.environ.get("SSL_CERT") and os.environ.get("SSL_KEY")
        )
        scheme = "https" if has_ssl else "http"
        url = f"{scheme}://localhost:{port}/stats"
        extra = list(pane.get("chromium_args", []))
        if has_ssl:
            extra += ["--ignore-certificate-errors", "--test-type"]
        stats_pane = {**pane, "type": "web", "url": url, "chromium_args": extra}
        return self._launch_web(stats_pane, geom)

    LAUNCHERS = {
        "rtsp": _launch_rtsp,
        "stream": _launch_rtsp,     # alias
        "web": _launch_web,
        "browser": _launch_web,     # alias
        "image": _launch_image,
        "command": _launch_command,
        "stats": _launch_stats,
    }

    # -- core operations ----------------------------------------------------

    def apply_layout(self, layout: list[dict]):
        """
        Apply a full layout.  Kills all existing panes, then launches all
        processes at once and positions windows in background threads so
        the HTTP response returns immediately.
        """
        with self.lock:
            self._kill_all()
            self._current_layout = layout
            launched: list[tuple[dict, str, subprocess.Popen, tuple]] = []
            for pane in layout:
                try:
                    ptype = pane.get("type", "")
                    name = pane.get("name", ptype)
                    launcher = self.LAUNCHERS.get(ptype)
                    if not launcher:
                        log.warning("Unknown pane type '%s', skipping", ptype)
                        continue
                    geom = resolve_region(pane, self.screen_w, self.screen_h)
                    proc = launcher(self, pane, geom)
                    self.panes[name] = ManagedPane(name=name, ptype=ptype, proc=proc)
                    launched.append((pane, name, proc, geom))
                except Exception:
                    log.exception("Failed to launch pane '%s'",
                                  pane.get("name", pane.get("type", "?")))
            self._save_layout()

        def position_all_then_stack():
            threads = [
                Thread(
                    target=self._position_pane,
                    args=(pane, name, proc, geom),
                    daemon=True,
                )
                for pane, name, proc, geom in launched
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=120)
            with self.lock:
                raise_window_stack(layout, self.panes)

        Thread(target=position_all_then_stack, daemon=True).start()

    def _position_pane(self, pane: dict, name: str, proc: subprocess.Popen, geom: tuple):
        """Find and position a pane's window (runs in a background thread)."""
        x, y, w, h = geom
        wid = find_window_by_name(name, retries=10, delay=0.5)
        if wid is None:
            wid = find_window_by_pid(proc.pid, retries=5, delay=0.5)

        if wid:
            position_window(wid, x, y, w, h)
            time.sleep(0.3)
            position_window(wid, x, y, w, h)
            subprocess.run(
                ["xdotool", "set_window", "--name", name, str(wid)],
                stderr=subprocess.DEVNULL,
            )
            with self.lock:
                if name in self.panes:
                    self.panes[name].wid = wid
            log.info("Pane '%s' → wid=%d  geom=%dx%d+%d+%d", name, wid, w, h, x, y)

            auto_refresh = pane.get("auto_refresh", 0)
            try:
                auto_refresh = float(auto_refresh or 0)
            except (TypeError, ValueError):
                auto_refresh = 0
            if auto_refresh > 0 and pane.get("type") in ("web", "browser"):
                self._start_auto_refresh(name, pane, auto_refresh)
        else:
            log.warning("Pane '%s': could not find X window (pid=%d)", name, proc.pid)

    def add_pane(self, pane: dict):
        """Add a single pane without disturbing existing ones."""
        with self.lock:
            # Update stored layout: replace existing pane with same name or append
            name = pane.get("name", pane.get("type", ""))
            self._current_layout = [
                p for p in self._current_layout if p.get("name") != name
            ]
            self._current_layout.append(pane)
            self._add_pane(pane)
            self._save_layout()

    def remove_pane(self, name: str):
        """Remove a pane by name."""
        with self.lock:
            self._current_layout = [
                p for p in self._current_layout if p.get("name") != name
            ]
            self._kill_pane(name)
            self._save_layout()

    def clear(self):
        """Kill all panes — blank screen."""
        with self.lock:
            self._current_layout = []
            self._kill_all()
            self._save_layout()

    @staticmethod
    def _proc_usage(pid: int) -> dict:
        """Read CPU% and RSS from /proc for a given pid."""
        try:
            with open(f"/proc/{pid}/stat") as f:
                fields = f.read().split()
            utime = int(fields[13])
            stime = int(fields[14])
            starttime = int(fields[21])
            rss_pages = int(fields[23])

            with open("/proc/uptime") as f:
                uptime_sec = float(f.read().split()[0])

            hz = os.sysconf("SC_CLK_TCK")
            page_size = os.sysconf("SC_PAGE_SIZE")
            total_time = (utime + stime) / hz
            elapsed = uptime_sec - (starttime / hz)
            cpu_pct = (total_time / elapsed * 100) if elapsed > 0 else 0.0
            rss_mb = rss_pages * page_size / (1024 * 1024)
            return {"cpu_pct": round(cpu_pct, 1), "rss_mb": round(rss_mb, 1)}
        except Exception:
            return {"cpu_pct": None, "rss_mb": None}

    @staticmethod
    def _system_stats() -> dict:
        """Collect system-wide stats from /proc and /sys."""
        stats: dict = {}
        try:
            with open("/proc/loadavg") as f:
                parts = f.read().split()
            stats["load_1"] = float(parts[0])
            stats["load_5"] = float(parts[1])
            stats["load_15"] = float(parts[2])
        except Exception:
            pass
        try:
            with open("/proc/stat") as f:
                for line in f:
                    if line.startswith("cpu "):
                        vals = list(map(int, line.split()[1:]))
                        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                        total = sum(vals)
                        stats["cpu_idle_ticks"] = idle
                        stats["cpu_total_ticks"] = total
                        break
        except Exception:
            pass
        try:
            with open("/proc/meminfo") as f:
                mi = {}
                for line in f:
                    k, v = line.split(":", 1)
                    mi[k.strip()] = int(v.strip().split()[0])
                total = mi.get("MemTotal", 0)
                avail = mi.get("MemAvailable", mi.get("MemFree", 0))
                stats["mem_total_mb"] = round(total / 1024, 1)
                stats["mem_used_mb"] = round((total - avail) / 1024, 1)
        except Exception:
            pass
        for path in (
            "/sys/class/thermal/thermal_zone0/temp",
            "/sys/class/hwmon/hwmon0/temp1_input",
        ):
            try:
                with open(path) as f:
                    stats["cpu_temp_c"] = round(int(f.read().strip()) / 1000, 1)
                break
            except Exception:
                continue
        try:
            st = os.statvfs("/")
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            stats["disk_total_gb"] = round(total / (1024 ** 3), 1)
            stats["disk_used_gb"] = round((total - free) / (1024 ** 3), 1)
        except Exception:
            pass
        try:
            with open("/proc/uptime") as f:
                stats["uptime_sec"] = int(float(f.read().split()[0]))
        except Exception:
            pass
        try:
            stats["cpu_count"] = os.cpu_count() or 1
        except Exception:
            pass
        return stats

    def status(self) -> dict:
        """Return current state."""
        with self.lock:
            return {
                "screen": {"width": self.screen_w, "height": self.screen_h},
                "system": self._system_stats(),
                "panes": {
                    name: {
                        "type": mp.ptype,
                        "pid": mp.proc.pid,
                        "alive": mp.proc.poll() is None,
                        "wid": mp.wid,
                        **self._proc_usage(mp.proc.pid),
                    }
                    for name, mp in self.panes.items()
                },
            }

    # -- internals ----------------------------------------------------------

    def _add_pane(self, pane: dict):
        """Launch a single pane and position it (blocking). Used by add_pane and watchdog."""
        ptype = pane.get("type", "")
        name = pane.get("name", ptype)

        if name in self.panes:
            self._kill_pane(name)

        launcher = self.LAUNCHERS.get(ptype)
        if not launcher:
            raise ValueError(
                f"Unknown pane type '{ptype}'. Valid: {list(self.LAUNCHERS)}"
            )

        geom = resolve_region(pane, self.screen_w, self.screen_h)
        proc = launcher(self, pane, geom)
        self.panes[name] = ManagedPane(name=name, ptype=ptype, proc=proc)
        self._position_pane(pane, name, proc, geom)
        raise_window_stack(self._current_layout, self.panes)

    def _watchdog(self):
        """Poll child processes and re-launch any that have exited."""
        while not self._stop_event.wait(WATCHDOG_INTERVAL):
            with self.lock:
                for pane_def in list(self._current_layout):
                    name = pane_def.get("name", pane_def.get("type", ""))
                    mp = self.panes.get(name)
                    if mp is None or mp.proc.poll() is None:
                        continue
                    exit_code = mp.proc.returncode
                    log.warning(
                        "Pane '%s' exited (code=%s), restarting…", name, exit_code
                    )
                    self.panes.pop(name, None)
                    try:
                        self._add_pane(pane_def)
                    except Exception:
                        log.exception("Failed to restart pane '%s'", name)

    def stop_watchdog(self):
        self._stop_event.set()

    def _start_auto_refresh(self, name: str, pane_def: dict, interval_min: float):
        """Start a background thread that restarts the pane after *interval_min* idle minutes."""
        mp = self.panes.get(name)
        if not mp or not mp.wid or interval_min <= 0:
            return
        self._stop_auto_refresh(name)

        stop = Event()
        mp._refresh_stop = stop
        last_activity = time.time()
        interval_sec = interval_min * 60

        def _loop():
            nonlocal last_activity
            while not stop.wait(2):
                cur_mp = self.panes.get(name)
                if not cur_mp or stop.is_set():
                    break
                wid = cur_mp.wid
                if not wid:
                    continue

                try:
                    out = subprocess.check_output(
                        ["xdotool", "getactivewindow"],
                        text=True, stderr=subprocess.DEVNULL,
                    ).strip()
                    if out and int(out) == wid:
                        last_activity = time.time()
                except Exception:
                    pass

                if time.time() - last_activity >= interval_sec:
                    log.info("Auto-refreshing pane '%s' (idle %.0fs) — full restart",
                             name, time.time() - last_activity)
                    stop.set()
                    # Clear our own thread refs so _kill_pane won't try to join us
                    with self.lock:
                        cur = self.panes.get(name)
                        if cur:
                            cur._refresh_stop = None
                            cur._refresh_thread = None
                        try:
                            self._add_pane(pane_def)
                        except Exception:
                            log.exception("Auto-refresh: failed to restart pane '%s'", name)
                    break

        t = Thread(target=_loop, daemon=True, name=f"auto-refresh-{name}")
        mp._refresh_thread = t
        t.start()
        log.info("Auto-refresh started for '%s': every %.1f min (full restart)", name, interval_min)

    def _stop_auto_refresh(self, name: str):
        """Stop the auto-refresh thread for a pane, if running."""
        mp = self.panes.get(name)
        if mp:
            self._stop_auto_refresh_mp(mp)

    @staticmethod
    def _stop_auto_refresh_mp(mp: ManagedPane):
        """Stop the auto-refresh thread on an already-removed ManagedPane."""
        if mp._refresh_stop:
            mp._refresh_stop.set()
        if mp._refresh_thread and mp._refresh_thread.is_alive():
            mp._refresh_thread.join(timeout=5)
        mp._refresh_stop = None
        mp._refresh_thread = None

    def _kill_pane(self, name: str):
        mp = self.panes.pop(name, None)
        if not mp:
            return
        self._stop_auto_refresh_mp(mp)
        if mp.proc.poll() is None:
            log.info("Killing pane '%s' (pid=%d)", name, mp.proc.pid)
            mp.proc.kill()
            mp.proc.wait()

    def _kill_all(self):
        for name in list(self.panes):
            self._kill_pane(name)


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

dm: DisplayManager  # set in main()


class Handler(BaseHTTPRequestHandler):
    """
    Endpoints
    ---------
    POST /layout        — set the full layout (kills existing panes)
    POST /pane          — add/replace a single pane
    DELETE /pane/<name>  — remove a pane
    POST /clear         — kill everything
    GET  /status        — current state
    GET  /health        — simple health check
    """

    def _send_json(self, data: dict, code: int = 200):
        body = json.dumps(data, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, code: int = 200):
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict | list:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        return json.loads(raw)

    # --- GET ---------------------------------------------------------------

    def do_GET(self):
        if self.path == "/":
            self._serve_index()
        elif self.path == "/stats":
            self._serve_stats()
        elif self.path == "/status":
            self._send_json(dm.status())
        elif self.path == "/health":
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "Not found"}, 404)

    def _serve_index(self):
        layout_json = json.dumps(dm._current_layout, indent=2)
        status_data = dm.status()
        screen = status_data["screen"]
        status_json = json.dumps(status_data)

        html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pi Display Server</title>
<style>
*,*::before,*::after{{box-sizing:border-box}}
body{{font-family:-apple-system,system-ui,sans-serif;margin:0;padding:20px;background:#0d1117;color:#e6edf3}}
h1{{font-size:1.3rem;margin:0 0 16px;color:#58a6ff}}
.top{{display:flex;gap:16px;align-items:flex-start}}
@media(max-width:900px){{.top{{flex-direction:column}}}}
.preview-wrap{{flex:1;min-width:0}}
.sidebar{{width:280px;flex-shrink:0}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;margin-bottom:16px}}
.card h2{{font-size:.85rem;margin:0 0 10px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px}}
#preview{{position:relative;background:#010409;border:1px solid #30363d;border-radius:6px;overflow:hidden;cursor:default}}
.pane-rect{{position:absolute;border:2px solid #58a6ff;border-radius:3px;cursor:move;
  display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:600;
  color:#e6edf3;text-shadow:0 1px 2px #000;user-select:none;background:rgba(88,166,255,.08)}}
.pane-rect.selected{{border-color:#f0883e;background:rgba(240,136,62,.12);z-index:10}}
.pane-rect .handle{{position:absolute;width:10px;height:10px;background:#58a6ff;border-radius:2px;cursor:nwse-resize}}
.pane-rect.selected .handle{{background:#f0883e}}
.pane-rect .handle.br{{right:-1px;bottom:-1px;cursor:nwse-resize}}
.pane-rect .handle.bl{{left:-1px;bottom:-1px;cursor:nesw-resize}}
.pane-rect .handle.tr{{right:-1px;top:-1px;cursor:nesw-resize}}
.pane-rect .handle.tl{{left:-1px;top:-1px;cursor:nwse-resize}}
.pane-item{{padding:6px 8px;border-radius:4px;cursor:pointer;font-size:13px;margin-bottom:4px;
  border:1px solid transparent;display:flex;justify-content:space-between;align-items:center}}
.pane-item:hover{{background:#21262d}}
.pane-item.selected{{border-color:#f0883e;background:#21262d}}
.pane-item .type{{color:#8b949e;font-size:11px}}
label{{display:block;font-size:12px;color:#8b949e;margin:8px 0 3px}}
input,select{{width:100%;padding:6px 8px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;
  border-radius:4px;font-size:13px;font-family:inherit}}
input:focus,select:focus{{outline:none;border-color:#58a6ff}}
.coords{{display:grid;grid-template-columns:1fr 1fr;gap:6px}}
.actions{{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}}
button{{padding:7px 14px;border:none;border-radius:6px;font-size:13px;font-weight:500;cursor:pointer}}
.btn-primary{{background:#238636;color:#fff}}.btn-primary:hover{{background:#2ea043}}
.btn-danger{{background:#da3633;color:#fff}}.btn-danger:hover{{background:#e5534b}}
.btn-secondary{{background:#30363d;color:#e6edf3}}.btn-secondary:hover{{background:#3d444d}}
.btn-sm{{padding:4px 10px;font-size:12px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid #21262d}}
th{{color:#8b949e;font-weight:500}}
.badge{{padding:2px 7px;border-radius:10px;font-size:11px;font-weight:600}}
.badge.alive{{background:#238636;color:#fff}}.badge.dead{{background:#da3633;color:#fff}}
#result{{margin-top:10px;padding:8px 12px;border-radius:6px;font-size:13px;display:none}}
#result.ok{{display:block;background:#0f2d1a;border:1px solid #238636;color:#3fb950}}
#result.err{{display:block;background:#2d0f0f;border:1px solid #da3633;color:#f85149}}
details{{margin-top:16px}} summary{{cursor:pointer;color:#8b949e;font-size:13px}}
details textarea{{width:100%;min-height:160px;margin-top:8px;background:#0d1117;color:#e6edf3;
  border:1px solid #30363d;border-radius:6px;padding:10px;font-family:"SF Mono",Consolas,monospace;
  font-size:12px;resize:vertical;tab-size:2}}
.info-line{{color:#8b949e;font-size:12px;margin-bottom:10px}}
label.inline{{display:flex;align-items:center;gap:8px;margin-top:8px;font-weight:normal}}
.sys-grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}
@media(max-width:500px){{.sys-grid{{grid-template-columns:1fr}}}}
.sys-stat{{text-align:center}}
.sys-stat .val{{font-size:1.3rem;font-weight:700;color:#e6edf3}}
.sys-stat .lbl{{font-size:11px;color:#8b949e;margin-top:2px}}
.bar-wrap{{height:6px;background:#21262d;border-radius:3px;margin-top:6px;overflow:hidden}}
.bar-fill{{height:100%;border-radius:3px;transition:width .4s ease}}
.bar-ok{{background:#238636}}.bar-warn{{background:#d29922}}.bar-crit{{background:#da3633}}
.sys-sub{{font-size:11px;color:#8b949e;margin-top:3px}}
</style></head><body>
<h1>Pi Display Server</h1>
<div class="top">
<div class="preview-wrap">
  <div class="card">
    <h2>Screen Preview</h2>
    <p class="info-line" id="screen-info"></p>
    <div id="preview"></div>
    <div class="actions">
      <button class="btn-primary" onclick="applyLayout()">Apply Layout</button>
      <button class="btn-danger" onclick="clearAll()">Clear All</button>
      <button class="btn-secondary" onclick="addPane()">+ Add Pane</button>
    </div>
    <div id="result"></div>
    <details><summary>Raw JSON</summary>
      <textarea id="raw-json"></textarea>
      <div class="actions"><button class="btn-secondary btn-sm" onclick="loadFromJson()">Load from JSON</button></div>
    </details>
  </div>
  <div class="card">
    <h2>System</h2>
    <div class="sys-grid" id="sys-stats"></div>
    <p class="sys-sub" id="sys-extra"></p>
  </div>
  <div class="card">
    <h2>Running Panes</h2>
    <table><thead><tr><th>Name</th><th>Type</th><th>Status</th><th>PID</th><th>CPU</th><th>Mem</th></tr></thead>
    <tbody id="proc-table"></tbody></table>
  </div>
</div>
<div class="sidebar">
  <div class="card">
    <h2>Panes</h2>
    <div id="pane-list"></div>
    <button class="btn-secondary btn-sm" style="width:100%;margin-top:8px" onclick="addPane()">+ Add Pane</button>
  </div>
  <div class="card" id="props-card" style="display:none">
    <h2>Properties</h2>
    <label>Name</label><input id="p-name" oninput="updateProp('name',this.value)">
    <label>Type</label>
    <select id="p-type" onchange="updateProp('type',this.value)">
      <option value="rtsp">rtsp</option><option value="web">web</option>
      <option value="image">image</option><option value="command">command</option>
      <option value="stats">stats</option>
    </select>
    <label>URL / Path</label><input id="p-url" oninput="updateUrlProp(this.value)">
    <label>Fit (rtsp only)</label>
    <select id="p-fit" onchange="updateProp('fit',this.value)">
      <option value="fill">fill</option><option value="cover">cover</option><option value="contain">contain</option>
    </select>
    <div id="rtsp-extra" style="display:none">
    <label title="Pi: v4l2m2m-copy for H.264 subs; drm-copy for H.265 mains">hwdec</label>
    <select id="p-hwdec" onchange="updateHwdec(this.value)">
      <option value="">(server default)</option>
      <option value="v4l2m2m-copy">v4l2m2m-copy (H.264)</option>
      <option value="drm-copy">drm-copy (H.265 / HEVC)</option>
      <option value="no">no (software)</option>
      <option value="auto">auto</option>
    </select>
    <label>RTSP transport</label>
    <select id="p-rtsp-t" onchange="updateRtspTransport(this.value)">
      <option value="">(default)</option>
      <option value="tcp">tcp</option>
      <option value="udp">udp (faster LAN)</option>
    </select>
    <label class="inline"><input type="checkbox" id="p-audio" onchange="updateAudio(this.checked)"> Decode audio</label>
    </div>
    <div id="web-extra" style="display:none">
    <label title="Reload the page after this many idle minutes (0 = off). Any interaction resets the timer.">Auto-refresh (min)</label>
    <input id="p-autorefresh" type="number" step="1" min="0" onchange="updateAutoRefresh(this.value)">
    </div>
    <label title="Higher value draws on top when panes overlap">Stack order</label>
    <input id="p-order" type="number" step="1" onchange="updateOrder(this.value)">
    <div class="coords">
      <div><label>X</label><input id="p-x" type="number" step="0.01" min="0" max="1" onchange="updateCoord('x',this.value)"></div>
      <div><label>Y</label><input id="p-y" type="number" step="0.01" min="0" max="1" onchange="updateCoord('y',this.value)"></div>
      <div><label>W</label><input id="p-w" type="number" step="0.01" min="0.02" max="1" onchange="updateCoord('w',this.value)"></div>
      <div><label>H</label><input id="p-h" type="number" step="0.01" min="0.02" max="1" onchange="updateCoord('h',this.value)"></div>
    </div>
    <div class="actions">
      <button class="btn-secondary btn-sm" onclick="refreshSelected()">Refresh Pane</button>
      <button class="btn-danger btn-sm" onclick="deleteSelected()">Delete Pane</button>
    </div>
  </div>
</div>
</div>
<script>
const SCREEN_W = {screen["width"]};
const SCREEN_H = {screen["height"]};
let layout = {layout_json};
let statusData = {status_json};
let selectedIdx = -1;
let dragState = null;

const preview = document.getElementById("preview");

function initPreview() {{
  const maxW = preview.parentElement.clientWidth - 2;
  const maxH = window.innerHeight - preview.getBoundingClientRect().top - 80;
  const scaleW = maxW / SCREEN_W;
  const scaleH = maxH / SCREEN_H;
  const scale = Math.min(scaleW, scaleH > 0 ? scaleH : scaleW);
  preview.style.width = Math.round(SCREEN_W * scale) + "px";
  preview.style.height = Math.round(SCREEN_H * scale) + "px";
  preview.dataset.scale = scale;
  document.getElementById("screen-info").textContent =
    "Screen: " + SCREEN_W + " \\u00d7 " + SCREEN_H + " px";
}}

function render() {{
  const scale = parseFloat(preview.dataset.scale);
  preview.querySelectorAll(".pane-rect").forEach(e => e.remove());
  layout.forEach((p, i) => {{
    const el = document.createElement("div");
    el.className = "pane-rect" + (i === selectedIdx ? " selected" : "");
    const x = (p.x || 0), y = (p.y || 0), w = (p.w || 1), h = (p.h || 1);
    el.style.left = (x * SCREEN_W * scale) + "px";
    el.style.top = (y * SCREEN_H * scale) + "px";
    el.style.width = (w * SCREEN_W * scale) + "px";
    el.style.height = (h * SCREEN_H * scale) + "px";
    const ord = (p.order != null && p.order !== "") ? Number(p.order) : 0;
    el.style.zIndex = String(ord * 1000 + i + 1);
    el.textContent = p.name || p.type || "?";
    el.dataset.idx = i;
    el.onmousedown = (e) => startDrag(e, i, "move");
    ["br","bl","tr","tl"].forEach(corner => {{
      const hdl = document.createElement("div");
      hdl.className = "handle " + corner;
      hdl.onmousedown = (e) => {{ e.stopPropagation(); startDrag(e, i, corner); }};
      el.appendChild(hdl);
    }});
    preview.appendChild(el);
  }});
  renderList();
  syncJson();
}}

function renderList() {{
  const list = document.getElementById("pane-list");
  list.innerHTML = "";
  layout.forEach((p, i) => {{
    const el = document.createElement("div");
    el.className = "pane-item" + (i === selectedIdx ? " selected" : "");
    el.innerHTML = '<span>' + (p.name || "unnamed") + '</span><span class="type">' + (p.type || "?") + '</span>';
    el.onclick = () => select(i);
    list.appendChild(el);
  }});
  showProps();
}}

function select(i) {{
  selectedIdx = i;
  render();
}}

function showProps() {{
  const card = document.getElementById("props-card");
  if (selectedIdx < 0 || selectedIdx >= layout.length) {{ card.style.display = "none"; return; }}
  card.style.display = "block";
  const p = layout[selectedIdx];
  document.getElementById("p-name").value = p.name || "";
  document.getElementById("p-type").value = p.type || "rtsp";
  document.getElementById("p-url").value = p.url || p.path || p.cmd || "";
  document.getElementById("p-fit").value = p.fit || "fill";
  const isRtsp = (p.type === "rtsp" || p.type === "stream");
  const isWeb = (p.type === "web" || p.type === "browser");
  const rtspEx = document.getElementById("rtsp-extra");
  rtspEx.style.display = isRtsp ? "block" : "none";
  document.getElementById("p-hwdec").value = p.hwdec || "";
  document.getElementById("p-rtsp-t").value = p.rtsp_transport || "";
  document.getElementById("p-audio").checked = !!p.audio;
  const webEx = document.getElementById("web-extra");
  webEx.style.display = isWeb ? "block" : "none";
  document.getElementById("p-autorefresh").value = p.auto_refresh || "";
  document.getElementById("p-x").value = round(p.x || 0);
  document.getElementById("p-y").value = round(p.y || 0);
  document.getElementById("p-w").value = round(p.w || 1);
  document.getElementById("p-h").value = round(p.h || 1);
  document.getElementById("p-order").value = (p.order != null && p.order !== "") ? p.order : 0;
}}

function updateProp(key, val) {{
  if (selectedIdx < 0) return;
  layout[selectedIdx][key] = val;
  render();
}}

function updateUrlProp(val) {{
  if (selectedIdx < 0) return;
  const p = layout[selectedIdx];
  delete p.url; delete p.path; delete p.cmd;
  const t = p.type || "rtsp";
  if (t === "image") p.path = val;
  else if (t === "command") p.cmd = val;
  else p.url = val;
  syncJson();
}}

function updateHwdec(val) {{
  if (selectedIdx < 0) return;
  const p = layout[selectedIdx];
  if (val) p.hwdec = val; else delete p.hwdec;
  syncJson();
}}

function updateRtspTransport(val) {{
  if (selectedIdx < 0) return;
  const p = layout[selectedIdx];
  if (val) p.rtsp_transport = val; else delete p.rtsp_transport;
  syncJson();
}}

function updateAudio(checked) {{
  if (selectedIdx < 0) return;
  const p = layout[selectedIdx];
  if (checked) p.audio = true; else delete p.audio;
  syncJson();
}}

function updateAutoRefresh(val) {{
  if (selectedIdx < 0) return;
  const p = layout[selectedIdx];
  const n = parseFloat(val);
  if (!val || Number.isNaN(n) || n <= 0) delete p.auto_refresh;
  else p.auto_refresh = n;
  syncJson();
}}

function updateCoord(key, val) {{
  if (selectedIdx < 0) return;
  layout[selectedIdx][key] = parseFloat(val) || 0;
  render();
}}

function updateOrder(val) {{
  if (selectedIdx < 0) return;
  const p = layout[selectedIdx];
  const n = parseInt(val, 10);
  if (val === "" || Number.isNaN(n)) delete p.order;
  else p.order = n;
  render();
  syncJson();
}}

function round(v) {{ return Math.round(v * 100) / 100; }}

function startDrag(e, idx, mode) {{
  e.preventDefault();
  select(idx);
  const scale = parseFloat(preview.dataset.scale);
  const rect = preview.getBoundingClientRect();
  const p = layout[idx];
  dragState = {{
    idx, mode, startMX: e.clientX, startMY: e.clientY,
    origX: p.x || 0, origY: p.y || 0, origW: p.w || 1, origH: p.h || 1,
    scale, rect
  }};
  document.addEventListener("mousemove", onDrag);
  document.addEventListener("mouseup", onDragEnd);
}}

function onDrag(e) {{
  if (!dragState) return;
  const s = dragState, p = layout[s.idx];
  const dx = (e.clientX - s.startMX) / (s.scale * SCREEN_W);
  const dy = (e.clientY - s.startMY) / (s.scale * SCREEN_H);
  const minSz = 0.02;

  if (s.mode === "move") {{
    p.x = round(Math.max(0, Math.min(1 - (p.w || 1), s.origX + dx)));
    p.y = round(Math.max(0, Math.min(1 - (p.h || 1), s.origY + dy)));
  }} else if (s.mode === "br") {{
    p.w = round(Math.max(minSz, Math.min(1 - s.origX, s.origW + dx)));
    p.h = round(Math.max(minSz, Math.min(1 - s.origY, s.origH + dy)));
  }} else if (s.mode === "bl") {{
    const newW = Math.max(minSz, s.origW - dx);
    p.x = round(Math.max(0, s.origX + s.origW - newW));
    p.w = round(newW);
    p.h = round(Math.max(minSz, Math.min(1 - s.origY, s.origH + dy)));
  }} else if (s.mode === "tr") {{
    p.w = round(Math.max(minSz, Math.min(1 - s.origX, s.origW + dx)));
    const newH = Math.max(minSz, s.origH - dy);
    p.y = round(Math.max(0, s.origY + s.origH - newH));
    p.h = round(newH);
  }} else if (s.mode === "tl") {{
    const newW = Math.max(minSz, s.origW - dx);
    const newH = Math.max(minSz, s.origH - dy);
    p.x = round(Math.max(0, s.origX + s.origW - newW));
    p.y = round(Math.max(0, s.origY + s.origH - newH));
    p.w = round(newW);
    p.h = round(newH);
  }}
  render();
}}

function onDragEnd() {{
  dragState = null;
  document.removeEventListener("mousemove", onDrag);
  document.removeEventListener("mouseup", onDragEnd);
}}

function addPane() {{
  layout.push({{ name: "pane" + (layout.length + 1), type: "web", url: "https://example.com", x: 0.0, y: 0.0, w: 0.5, h: 0.5 }});
  select(layout.length - 1);
}}

function deleteSelected() {{
  if (selectedIdx < 0) return;
  layout.splice(selectedIdx, 1);
  selectedIdx = -1;
  render();
}}

async function refreshSelected() {{
  if (selectedIdx < 0) return;
  const pane = layout[selectedIdx];
  try {{
    const res = await fetch("/pane", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify(pane),
    }});
    const data = await res.json();
    if (res.ok) {{
      showResult(true, "Refreshed pane '" + (pane.name || "?") + "'");
      setTimeout(refreshStatus, 2000);
    }} else showResult(false, data.error || "Error");
  }} catch(e) {{ showResult(false, e.message); }}
}}

function syncJson() {{
  document.getElementById("raw-json").value = JSON.stringify(layout, null, 2);
}}

function loadFromJson() {{
  try {{
    layout = JSON.parse(document.getElementById("raw-json").value);
    selectedIdx = -1;
    render();
    showResult(true, "Loaded from JSON");
  }} catch(e) {{ showResult(false, e.message); }}
}}

function showResult(ok, msg) {{
  const el = document.getElementById("result");
  el.textContent = msg;
  el.className = ok ? "ok" : "err";
  setTimeout(() => el.className = "", 3000);
}}

async function applyLayout() {{
  try {{
    const res = await fetch("/layout", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify(layout),
    }});
    const data = await res.json();
    if (res.ok) {{
      statusData = data;
      showResult(true, "Applied — " + Object.keys(data.panes || {{}}).length + " panes");
      setTimeout(refreshStatus, 2000);
    }} else showResult(false, data.error || "Error");
  }} catch(e) {{ showResult(false, e.message); }}
}}

async function clearAll() {{
  if (!confirm("Kill all panes?")) return;
  const res = await fetch("/clear", {{ method: "POST" }});
  if (res.ok) {{
    layout = [];
    selectedIdx = -1;
    render();
    showResult(true, "Cleared");
    setTimeout(refreshStatus, 500);
  }}
}}

let prevCpuIdle = null, prevCpuTotal = null;

async function refreshStatus() {{
  try {{
    const res = await fetch("/status");
    statusData = await res.json();
    renderSystemStats();
    renderProcTable();
  }} catch(e) {{}}
}}

function barClass(pct) {{ return pct > 85 ? "bar-crit" : pct > 65 ? "bar-warn" : "bar-ok"; }}

function fmtUptime(sec) {{
  if (sec == null) return "\\u2014";
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600),
        m = Math.floor((sec % 3600) / 60);
  if (d > 0) return d + "d " + h + "h";
  if (h > 0) return h + "h " + m + "m";
  return m + "m";
}}

function renderSystemStats() {{
  const s = statusData.system || {{}};
  const grid = document.getElementById("sys-stats");
  const extra = document.getElementById("sys-extra");

  let cpuPct = null;
  if (s.cpu_total_ticks != null && prevCpuTotal != null) {{
    const dTotal = s.cpu_total_ticks - prevCpuTotal;
    const dIdle = s.cpu_idle_ticks - prevCpuIdle;
    if (dTotal > 0) cpuPct = Math.round((1 - dIdle / dTotal) * 100);
  }}
  prevCpuIdle = s.cpu_idle_ticks;
  prevCpuTotal = s.cpu_total_ticks;

  let memPct = null;
  if (s.mem_total_mb) memPct = Math.round(s.mem_used_mb / s.mem_total_mb * 100);

  let diskPct = null;
  if (s.disk_total_gb) diskPct = Math.round(s.disk_used_gb / s.disk_total_gb * 100);

  const items = [];
  items.push({{
    lbl: "CPU",
    val: cpuPct != null ? cpuPct + "%" : "\\u2014",
    pct: cpuPct,
    sub: s.cpu_count ? s.cpu_count + " cores" : ""
  }});
  items.push({{
    lbl: "Memory",
    val: s.mem_used_mb != null ? s.mem_used_mb + " / " + s.mem_total_mb + " MB" : "\\u2014",
    pct: memPct,
    sub: ""
  }});
  items.push({{
    lbl: "Temperature",
    val: s.cpu_temp_c != null ? s.cpu_temp_c + " \\u00b0C" : "\\u2014",
    pct: s.cpu_temp_c != null ? Math.min(100, Math.round((s.cpu_temp_c / 85) * 100)) : null,
    sub: ""
  }});
  items.push({{
    lbl: "Disk",
    val: s.disk_used_gb != null ? s.disk_used_gb + " / " + s.disk_total_gb + " GB" : "\\u2014",
    pct: diskPct,
    sub: ""
  }});

  grid.innerHTML = items.map(it => {{
    const bar = it.pct != null
      ? '<div class="bar-wrap"><div class="bar-fill ' + barClass(it.pct) + '" style="width:' + it.pct + '%"></div></div>'
      : '';
    return '<div class="sys-stat"><div class="val">' + it.val + '</div><div class="lbl">' + it.lbl + '</div>' + bar +
      (it.sub ? '<div class="sys-sub">' + it.sub + '</div>' : '') + '</div>';
  }}).join("");

  const parts = [];
  if (s.load_1 != null) parts.push("Load: " + s.load_1 + " / " + s.load_5 + " / " + s.load_15);
  if (s.uptime_sec != null) parts.push("Uptime: " + fmtUptime(s.uptime_sec));
  extra.textContent = parts.join("  \\u00b7  ");
}}

function renderProcTable() {{
  const tb = document.getElementById("proc-table");
  const panes = statusData.panes || {{}};
  const names = Object.keys(panes);
  if (!names.length) {{
    tb.innerHTML = '<tr><td colspan="6" style="color:#8b949e">No panes running</td></tr>';
    return;
  }}
  tb.innerHTML = names.map(n => {{
    const p = panes[n];
    const alive = p.alive ? "alive" : "dead";
    const cpu = p.cpu_pct != null ? p.cpu_pct + "%" : "\\u2014";
    const mem = p.rss_mb != null ? p.rss_mb + " MB" : "\\u2014";
    return '<tr><td>' + n + '</td><td>' + p.type + '</td>' +
      '<td><span class="badge ' + alive + '">' + alive + '</span></td>' +
      '<td>' + p.pid + '</td><td>' + cpu + '</td><td>' + mem + '</td></tr>';
  }}).join("");
}}

window.addEventListener("resize", () => {{ initPreview(); render(); }});
initPreview();
render();
renderProcTable();
refreshStatus();
setInterval(refreshStatus, 5000);
</script></body></html>"""
        self._send_html(html)

    def _serve_stats(self):
        """Standalone system-stats page for the 'stats' pane type."""
        html = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pi Stats</title>
<style>
*,*::before,*::after{box-sizing:border-box}
body{font-family:-apple-system,system-ui,sans-serif;margin:0;padding:0;background:#0d1117;color:#e6edf3;
  display:flex;align-items:center;justify-content:center;min-height:100vh;overflow:hidden}
.wrap{width:100%;max-width:420px;padding:12px}
h1{font-size:.9rem;margin:0 0 10px;color:#58a6ff;text-align:center;letter-spacing:.4px;text-transform:uppercase}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.stat{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:9px;text-align:center}
.stat .val{font-size:1.1rem;font-weight:700;color:#e6edf3;line-height:1.2}
.stat .lbl{font-size:10px;color:#8b949e;margin-top:2px;text-transform:uppercase;letter-spacing:.3px}
.bar-wrap{height:4px;background:#21262d;border-radius:2px;margin-top:6px;overflow:hidden}
.bar-fill{height:100%;border-radius:2px;transition:width .6s ease}
.bar-ok{background:#238636}.bar-warn{background:#d29922}.bar-crit{background:#da3633}
.stat .sub{font-size:9px;color:#8b949e;margin-top:3px}
.footer{text-align:center;color:#8b949e;font-size:10px;margin-top:10px}
.panes-section{margin-top:10px}
.panes-section h2{font-size:.7rem;color:#8b949e;text-transform:uppercase;letter-spacing:.4px;margin:0 0 5px}
.pane-row{display:flex;justify-content:space-between;align-items:center;padding:3px 6px;
  background:#161b22;border:1px solid #30363d;border-radius:5px;margin-bottom:3px;font-size:10px}
.pane-row .pname{font-weight:600}.pane-row .ptype{color:#8b949e;font-size:9px}
.pane-row .pstats{color:#8b949e;font-size:9px}
.badge{padding:1px 5px;border-radius:8px;font-size:8px;font-weight:600}
.badge.alive{background:#238636;color:#fff}.badge.dead{background:#da3633;color:#fff}
</style></head><body>
<div class="wrap">
<h1>System Stats</h1>
<div class="grid" id="grid"></div>
<div class="footer" id="footer"></div>
<div class="panes-section">
<h2>Panes</h2>
<div id="pane-list"></div>
</div>
</div>
<script>
let prevIdle = null, prevTotal = null;

function barClass(pct) { return pct > 85 ? "bar-crit" : pct > 65 ? "bar-warn" : "bar-ok"; }

function fmtUptime(sec) {
  if (sec == null) return "\\u2014";
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600),
        m = Math.floor((sec % 3600) / 60);
  if (d > 0) return d + "d " + h + "h";
  if (h > 0) return h + "h " + m + "m";
  return m + "m";
}

async function refresh() {
  try {
    const res = await fetch("/status");
    const data = await res.json();
    renderStats(data.system || {});
    renderPanes(data.panes || {});
  } catch(e) {}
}

function renderStats(s) {
  let cpuPct = null;
  if (s.cpu_total_ticks != null && prevTotal != null) {
    const dT = s.cpu_total_ticks - prevTotal, dI = s.cpu_idle_ticks - prevIdle;
    if (dT > 0) cpuPct = Math.round((1 - dI / dT) * 100);
  }
  prevIdle = s.cpu_idle_ticks; prevTotal = s.cpu_total_ticks;

  const memPct = s.mem_total_mb ? Math.round(s.mem_used_mb / s.mem_total_mb * 100) : null;
  const diskPct = s.disk_total_gb ? Math.round(s.disk_used_gb / s.disk_total_gb * 100) : null;

  const items = [
    { lbl: "CPU", val: cpuPct != null ? cpuPct + "%" : "\\u2014", pct: cpuPct,
      sub: s.cpu_count ? s.cpu_count + " cores" : "" },
    { lbl: "Memory", val: s.mem_used_mb != null ? s.mem_used_mb + " / " + s.mem_total_mb + " MB" : "\\u2014",
      pct: memPct, sub: "" },
    { lbl: "Temp", val: s.cpu_temp_c != null ? s.cpu_temp_c + " \\u00b0C" : "\\u2014",
      pct: s.cpu_temp_c != null ? Math.min(100, Math.round(s.cpu_temp_c / 85 * 100)) : null, sub: "" },
    { lbl: "Disk", val: s.disk_used_gb != null ? s.disk_used_gb + " / " + s.disk_total_gb + " GB" : "\\u2014",
      pct: diskPct, sub: "" }
  ];

  document.getElementById("grid").innerHTML = items.map(it => {
    const bar = it.pct != null
      ? '<div class="bar-wrap"><div class="bar-fill ' + barClass(it.pct) + '" style="width:' + it.pct + '%"></div></div>'
      : '';
    return '<div class="stat"><div class="val">' + it.val + '</div><div class="lbl">' + it.lbl + '</div>' + bar +
      (it.sub ? '<div class="sub">' + it.sub + '</div>' : '') + '</div>';
  }).join("");

  const parts = [];
  if (s.load_1 != null) parts.push("Load: " + s.load_1 + " / " + s.load_5 + " / " + s.load_15);
  if (s.uptime_sec != null) parts.push("Uptime: " + fmtUptime(s.uptime_sec));
  document.getElementById("footer").textContent = parts.join("  \\u00b7  ");
}

function renderPanes(panes) {
  const names = Object.keys(panes);
  const el = document.getElementById("pane-list");
  if (!names.length) { el.innerHTML = '<div style="color:#8b949e;font-size:10px">No panes</div>'; return; }
  el.innerHTML = names.map(n => {
    const p = panes[n];
    const st = p.alive ? "alive" : "dead";
    const cpu = p.cpu_pct != null ? p.cpu_pct + "%" : "\\u2014";
    const mem = p.rss_mb != null ? p.rss_mb + "MB" : "\\u2014";
    return '<div class="pane-row"><span><span class="pname">' + n + '</span> <span class="ptype">' + p.type + '</span></span>' +
      '<span class="pstats"><span class="badge ' + st + '">' + st + '</span> ' + cpu + ' / ' + mem + '</span></div>';
  }).join("");
}

refresh();
setInterval(refresh, 3000);
</script></body></html>"""
        self._send_html(html)

    # --- POST --------------------------------------------------------------

    def do_POST(self):
        try:
            if self.path == "/layout":
                layout = self._read_json()
                if not isinstance(layout, list):
                    layout = layout.get("panes", [])
                dm.apply_layout(layout)
                self._send_json(dm.status())

            elif self.path == "/pane":
                pane = self._read_json()
                dm.add_pane(pane)
                self._send_json(dm.status())

            elif self.path == "/clear":
                dm.clear()
                self._send_json({"ok": True})

            else:
                self._send_json({"error": "Not found"}, 404)

        except Exception as e:
            log.exception("Error handling %s", self.path)
            self._send_json({"error": str(e)}, 400)

    # --- DELETE ------------------------------------------------------------

    def do_DELETE(self):
        if self.path.startswith("/pane/"):
            name = self.path[len("/pane/"):]
            dm.remove_pane(name)
            self._send_json(dm.status())
        else:
            self._send_json({"error": "Not found"}, 404)

    def log_message(self, fmt, *args):
        log.info("HTTP %s", fmt % args)


# ---------------------------------------------------------------------------
# SSL helpers
# ---------------------------------------------------------------------------

_DEFAULT_CERT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "certs"
)


def _ensure_selfsigned_cert(cert_dir: str = _DEFAULT_CERT_DIR) -> tuple[str, str]:
    """Generate a self-signed cert/key pair via openssl if they don't exist yet."""
    cert = os.path.join(cert_dir, "server.crt")
    key = os.path.join(cert_dir, "server.key")
    if os.path.exists(cert) and os.path.exists(key):
        return cert, key
    os.makedirs(cert_dir, mode=0o700, exist_ok=True)
    log.info("Generating self-signed certificate in %s …", cert_dir)
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", key, "-out", cert,
            "-days", "3650", "-nodes",
            "-subj", "/CN=pi-display-server",
        ],
        check=True,
    )
    return cert, key


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global dm

    host = os.environ.get("DISPLAY_HOST", "0.0.0.0")
    port = int(os.environ.get("DISPLAY_PORT", "8686"))

    # Make sure DISPLAY is set (needed when run from systemd)
    if "DISPLAY" not in os.environ:
        os.environ["DISPLAY"] = ":0"

    dm = DisplayManager()
    dm.load_saved_layout()

    server = HTTPServer((host, port), Handler)

    # -- optional HTTPS --
    cert = os.environ.get("SSL_CERT")
    key = os.environ.get("SSL_KEY")
    auto_ssl = os.environ.get("SSL_SELFSIGNED", "").lower() in ("1", "true", "yes")
    if auto_ssl and not (cert and key):
        cert, key = _ensure_selfsigned_cert()
    if cert and key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        log.info("Pi Display Server listening on https://%s:%d (cert=%s)", host, port, cert)
    else:
        log.info("Pi Display Server listening on http://%s:%d", host, port)

    # Graceful shutdown
    def _shutdown(sig, frame):
        log.info("Shutting down…")
        dm.stop_watchdog()
        dm._kill_all()
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _shutdown(None, None)


if __name__ == "__main__":
    main()
