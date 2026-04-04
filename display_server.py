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
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Event, Lock, Thread
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("display-server")

LAYOUT_FILE = os.environ.get("LAYOUT_FILE", "/opt/pi-display-server/layout.json")
WATCHDOG_INTERVAL = int(os.environ.get("WATCHDOG_INTERVAL", "10"))

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
# Pane / Region helpers
# ---------------------------------------------------------------------------
#
# The screen is divided into a 2-column × 4-row grid, numbered 1–8:
#
#   | 1 | 2 |
#   | 3 | 4 |
#   | 5 | 6 |
#   | 7 | 8 |
#
# "region" accepts a comma-separated list of cell numbers.
# The bounding box of all listed cells becomes the window area.
#
# Examples:
#   "1"       → top-left cell
#   "1,2"     → full top row
#   "5,7"     → left column, bottom two rows (tall pane)
#   "1,3,5,7" → entire left column
#   "1,2,3,4,5,6,7,8" → fullscreen
#

GRID_COLS = 2
GRID_ROWS = 4

def _cell_to_colrow(cell: int) -> tuple[int, int]:
    """Convert 1-based cell number to (col, row), both 0-based."""
    if cell < 1 or cell > GRID_COLS * GRID_ROWS:
        raise ValueError(
            f"Cell {cell} out of range. Valid: 1–{GRID_COLS * GRID_ROWS}"
        )
    idx = cell - 1
    row = idx // GRID_COLS
    col = idx % GRID_COLS
    return col, row


def resolve_region(pane: dict, sw: int, sh: int) -> tuple[int, int, int, int]:
    """
    Return (x, y, w, h) in pixels for a pane definition.

    Supports either:
      - "region": "5,7" (comma-separated cell numbers, bounding box)
      - "region": "3"   (single cell)
      - "x", "y", "w", "h" as floats 0.0–1.0 or absolute pixels
    """
    if "region" in pane:
        raw = str(pane["region"])
        cells = [int(c.strip()) for c in raw.split(",")]

        cols = []
        rows = []
        for cell in cells:
            c, r = _cell_to_colrow(cell)
            cols.append(c)
            rows.append(r)

        min_col, max_col = min(cols), max(cols)
        min_row, max_row = min(rows), max(rows)

        cell_w = sw / GRID_COLS
        cell_h = sh / GRID_ROWS

        x = int(min_col * cell_w)
        y = int(min_row * cell_h)
        w = int((max_col - min_col + 1) * cell_w)
        h = int((max_row - min_row + 1) * cell_h)
        return x, y, w, h

    # Manual coordinates
    def to_px(val, total):
        if isinstance(val, float) and 0.0 <= val <= 1.0:
            return int(val * total)
        return int(val)

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


class DisplayManager:
    """Keeps track of all running panes and their processes."""

    def __init__(self):
        self.panes: dict[str, ManagedPane] = {}
        self.lock = Lock()
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
        cmd = [
            "mpv",
            f"--title={name}",
            "--no-terminal",
            "--no-osc",
            "--no-input-default-bindings",
            "--force-window=yes",
            "--no-border",
            f"--geometry={w}x{h}+{x}+{y}",
            f"--autofit={w}x{h}",
            *aspect_args,
            "--profile=low-latency",
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
        data_dir = f"/tmp/pi-display-chromium-{name}"
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

    LAUNCHERS = {
        "rtsp": _launch_rtsp,
        "stream": _launch_rtsp,     # alias
        "web": _launch_web,
        "browser": _launch_web,     # alias
        "image": _launch_image,
        "command": _launch_command,
    }

    # -- core operations ----------------------------------------------------

    def apply_layout(self, layout: list[dict]):
        """
        Apply a full layout.  Kills all existing panes, then launches and
        positions every pane in the list.  Each pane is independent — one
        failure won't prevent the others from launching.
        """
        with self.lock:
            self._kill_all()
            self._current_layout = layout
            for pane in layout:
                try:
                    self._add_pane(pane)
                except Exception:
                    log.exception("Failed to launch pane '%s'",
                                  pane.get("name", pane.get("type", "?")))
            self._save_layout()

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

    def status(self) -> dict:
        """Return current state."""
        with self.lock:
            return {
                "screen": {"width": self.screen_w, "height": self.screen_h},
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
                "grid": f"{GRID_COLS}x{GRID_ROWS} (cells 1–{GRID_COLS * GRID_ROWS})",
            }

    # -- internals ----------------------------------------------------------

    def _add_pane(self, pane: dict):
        ptype = pane.get("type", "")
        name = pane.get("name", ptype)

        if name in self.panes:
            self._kill_pane(name)

        launcher = self.LAUNCHERS.get(ptype)
        if not launcher:
            raise ValueError(
                f"Unknown pane type '{ptype}'. Valid: {list(self.LAUNCHERS)}"
            )

        # Resolve geometry FIRST so launchers can use native positioning
        x, y, w, h = resolve_region(pane, self.screen_w, self.screen_h)
        proc = launcher(self, pane, (x, y, w, h))

        wid = find_window_by_pid(proc.pid, retries=10, delay=0.5)
        if wid is None:
            wid = find_window_by_name(pane.get("url", name), retries=5, delay=0.5)

        if wid:
            position_window(wid, x, y, w, h)
            time.sleep(0.3)
            position_window(wid, x, y, w, h)
            subprocess.run(
                ["xdotool", "set_window", "--name", name, str(wid)],
                stderr=subprocess.DEVNULL,
            )
            log.info("Pane '%s' → wid=%d  geom=%dx%d+%d+%d", name, wid, w, h, x, y)
        else:
            log.warning("Pane '%s': could not find X window (pid=%d)", name, proc.pid)

        self.panes[name] = ManagedPane(name=name, ptype=ptype, proc=proc, wid=wid)

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

    def _kill_pane(self, name: str):
        mp = self.panes.pop(name, None)
        if mp and mp.proc.poll() is None:
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
        elif self.path == "/status":
            self._send_json(dm.status())
        elif self.path == "/health":
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "Not found"}, 404)

    def _serve_index(self):
        layout_json = json.dumps(dm._current_layout, indent=2)
        status = dm.status()
        panes_info = status["panes"]
        screen = status["screen"]

        pane_rows = ""
        for name, info in panes_info.items():
            alive = "alive" if info["alive"] else "dead"
            cpu = f'{info["cpu_pct"]}%' if info.get("cpu_pct") is not None else "—"
            mem = f'{info["rss_mb"]} MB' if info.get("rss_mb") is not None else "—"
            pane_rows += (
                f'<tr><td>{name}</td><td>{info["type"]}</td>'
                f'<td><span class="badge {alive}">{alive}</span></td>'
                f'<td>{info["pid"]}</td>'
                f'<td>{cpu}</td><td>{mem}</td></tr>\n'
            )

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pi Display Server</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0;
         padding: 24px; background: #0d1117; color: #e6edf3; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 20px; color: #58a6ff; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
  @media (max-width: 800px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px;
           padding: 16px; }}
  .card h2 {{ font-size: 1rem; margin: 0 0 12px; color: #8b949e; }}
  textarea {{ width: 100%; min-height: 320px; background: #0d1117;
             color: #e6edf3; border: 1px solid #30363d; border-radius: 6px;
             padding: 12px; font-family: "SF Mono", Consolas, monospace;
             font-size: 13px; resize: vertical; tab-size: 2; }}
  textarea:focus {{ outline: none; border-color: #58a6ff; }}
  .actions {{ display: flex; gap: 8px; margin-top: 12px; }}
  button {{ padding: 8px 16px; border: none; border-radius: 6px;
           font-size: 14px; font-weight: 500; cursor: pointer; }}
  .btn-primary {{ background: #238636; color: #fff; }}
  .btn-primary:hover {{ background: #2ea043; }}
  .btn-danger {{ background: #da3633; color: #fff; }}
  .btn-danger:hover {{ background: #e5534b; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #21262d; }}
  th {{ color: #8b949e; font-weight: 500; }}
  .badge {{ padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: 600; }}
  .badge.alive {{ background: #238636; color: #fff; }}
  .badge.dead {{ background: #da3633; color: #fff; }}
  .info {{ color: #8b949e; font-size: 13px; margin-bottom: 12px; }}
  #result {{ margin-top: 12px; padding: 10px; border-radius: 6px;
            font-size: 13px; display: none; }}
  #result.ok {{ display: block; background: #0f2d1a; border: 1px solid #238636; color: #3fb950; }}
  #result.err {{ display: block; background: #2d0f0f; border: 1px solid #da3633; color: #f85149; }}
</style>
</head>
<body>
<h1>Pi Display Server</h1>
<div class="grid">
  <div class="card">
    <h2>Layout</h2>
    <p class="info">Screen: {screen["width"]}x{screen["height"]} &middot; Grid: {status["grid"]}</p>
    <textarea id="layout">{layout_json}</textarea>
    <div class="actions">
      <button class="btn-primary" onclick="applyLayout()">Apply Layout</button>
      <button class="btn-danger" onclick="clearAll()">Clear All</button>
    </div>
    <div id="result"></div>
  </div>
  <div class="card">
    <h2>Running Panes</h2>
    <table>
      <thead><tr><th>Name</th><th>Type</th><th>Status</th><th>PID</th><th>CPU</th><th>Mem</th></tr></thead>
      <tbody>{pane_rows if pane_rows else '<tr><td colspan="6" style="color:#8b949e">No panes running</td></tr>'}</tbody>
    </table>
  </div>
</div>
<script>
function showResult(ok, msg) {{
  const el = document.getElementById("result");
  el.textContent = msg;
  el.className = ok ? "ok" : "err";
}}
async function applyLayout() {{
  try {{
    const text = document.getElementById("layout").value;
    JSON.parse(text);
    const res = await fetch("/layout", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: text,
    }});
    const data = await res.json();
    if (res.ok) {{
      showResult(true, "Applied — " + Object.keys(data.panes || {{}}).length + " panes");
      setTimeout(() => location.reload(), 1000);
    }} else {{
      showResult(false, data.error || "Unknown error");
    }}
  }} catch (e) {{
    showResult(false, e.message);
  }}
}}
async function clearAll() {{
  if (!confirm("Kill all panes?")) return;
  const res = await fetch("/clear", {{ method: "POST" }});
  if (res.ok) {{
    showResult(true, "Cleared");
    setTimeout(() => location.reload(), 500);
  }}
}}
</script>
</body>
</html>"""
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
    log.info("Pi Display Server listening on %s:%d", host, port)

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
