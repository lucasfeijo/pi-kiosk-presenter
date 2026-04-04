# Pi Display Server

Remote HTTP control of what your Raspberry Pi 4 shows on HDMI. Send JSON layouts to arrange RTSP streams, web pages, images, and arbitrary commands on screen.

Designed for **Raspberry Pi OS Lite + X11**.

## Install

On a fresh Pi, run the installer with your repo URL:

```bash
bash <(curl -sL https://raw.githubusercontent.com/YOU/pi-display-server/main/install.sh) \
  https://github.com/YOU/pi-display-server.git
```

Or clone first and run locally:

```bash
git clone https://github.com/YOU/pi-display-server.git /tmp/pi-display-server
bash /tmp/pi-display-server/install.sh https://github.com/YOU/pi-display-server.git
```

This clones the repo to `/opt/pi-display-server`, installs all dependencies (X11, openbox, xdotool, mpv, chromium, feh), sets up the systemd service on port **8686**, and creates default `~/.xinitrc` and `~/.bash_profile` for kiosk boot (only if they don't already exist).

## Prerequisites

- **Raspberry Pi OS Lite** with console autologin enabled (`raspi-config` > System Options > Boot / Auto Login > Console Autologin).
- The installer handles everything else. If you already have a custom `~/.xinitrc` or `~/.bash_profile`, the installer won't overwrite them.

## Boot Chain

On power-up, the system starts automatically:

1. **getty** auto-logs in the `pi` user on tty1
2. **`~/.bash_profile`** runs `startx` (only on tty1, only without an existing display)
3. **`~/.xinitrc`** disables screen blanking, rotates the display, and starts Openbox
4. **systemd** starts `pi-display-server.service` after `graphical.target`
5. **`display_server.py`** restores the last saved layout from `layout.json`

No display manager (lightdm) needed.

## Deploying Updates

Push to GitHub, then update the Pi:

```bash
# From your dev machine (one command does both):
./deploy.sh

# Or just SSH in and run:
ssh pi@your-pi 'update-display'
```

The `update-display` command (installed to `/usr/local/bin/`) does `git pull` + service restart.

Set `PI_HOST` to override the default target:

```bash
PI_HOST=pi@other-pi.local ./deploy.sh
```

## Crash Recovery

| What crashes | Who restarts it | How |
|---|---|---|
| **display_server.py** | systemd | `Restart=on-failure` with 1s delay. Server reloads `layout.json` and re-creates all panes. Rate-limited to 5 restarts per 60s. |
| **A child pane** (mpv, chromium, feh) | Watchdog thread | Polls every 10s, re-launches dead panes from the stored layout. |
| **X11 / Openbox** | getty + bash_profile | Full reset: login re-runs `startx`, systemd restarts the server. |

## API Reference

The server runs on `http://<pi-ip>:8686`. All POST bodies are JSON.

### `GET /status`

Returns current screen info and running panes.

```bash
curl http://pi:8686/status
```

### `GET /health`

Simple health check — returns `{"ok": true}`.

### `POST /layout`

Set the full screen layout. **Kills all existing panes** and creates new ones.

```bash
# RTSP on bottom half, web page on top half
curl -X POST http://pi:8686/layout \
  -H 'Content-Type: application/json' \
  -d '[
    {
      "name": "camera",
      "type": "rtsp",
      "url": "rtsp://192.168.1.100:554/stream",
      "x": 0, "y": 0.5, "w": 1.0, "h": 0.5
    },
    {
      "name": "dashboard",
      "type": "web",
      "url": "http://grafana.local:3000/dashboard",
      "x": 0, "y": 0, "w": 1.0, "h": 0.5
    }
  ]'
```

### `POST /pane`

Add or replace a single pane without disturbing existing ones.

```bash
# Add a webcam feed to the top-right quarter
curl -X POST http://pi:8686/pane \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "webcam",
    "type": "rtsp",
    "url": "rtsp://192.168.1.50/live",
    "x": 0.5, "y": 0, "w": 0.5, "h": 0.5
  }'
```

### `DELETE /pane/<name>`

Remove a single pane.

```bash
curl -X DELETE http://pi:8686/pane/webcam
```

### `POST /clear`

Kill all panes — blank screen.

```bash
curl -X POST http://pi:8686/clear
```

## Pane Types

| Type | Alias | What it does | Required fields |
|------|-------|--------------|-----------------|
| `rtsp` | `stream` | Plays a stream via `mpv` | `url` |
| `web` | `browser` | Opens a URL in Chromium kiosk mode | `url` |
| `image` | — | Shows an image via `feh` | `path` |
| `command` | — | Runs any command that creates an X window | `cmd` |

## Visual Layout Editor

Open `http://<pi-ip>:8686/` in a browser to use the visual editor:

- **Screen preview** — drag panes to move them, drag corner handles to resize
- **Sidebar** — click a pane to edit its properties (name, type, URL, fit mode)
- **Apply Layout** — pushes the layout to the Pi and restarts all panes
- **Raw JSON** — expand the collapsible section at the bottom for direct JSON editing

## Positioning

Every pane needs `x`, `y`, `w`, `h` values as fractions (0.0–1.0) of the screen:

| Field | Description | Default |
|-------|-------------|---------|
| `x` | Left edge (0.0 = left, 1.0 = right) | `0` |
| `y` | Top edge (0.0 = top, 1.0 = bottom) | `0` |
| `w` | Width (0.0–1.0) | `1.0` (full width) |
| `h` | Height (0.0–1.0) | `1.0` (full height) |

Absolute pixel values also work.

```json
{
  "name": "custom",
  "type": "web",
  "url": "http://example.com",
  "x": 0.1,
  "y": 0.0,
  "w": 0.8,
  "h": 0.5
}
```

## Examples

### Security camera dashboard (2x2 grid)

```bash
curl -X POST http://pi:8686/layout \
  -H 'Content-Type: application/json' \
  -d '[
    {"name": "cam1", "type": "rtsp", "url": "rtsp://192.168.1.101/stream", "x":0, "y":0, "w":0.5, "h":0.5},
    {"name": "cam2", "type": "rtsp", "url": "rtsp://192.168.1.102/stream", "x":0.5, "y":0, "w":0.5, "h":0.5},
    {"name": "cam3", "type": "rtsp", "url": "rtsp://192.168.1.103/stream", "x":0, "y":0.5, "w":0.5, "h":0.5},
    {"name": "cam4", "type": "rtsp", "url": "rtsp://192.168.1.104/stream", "x":0.5, "y":0.5, "w":0.5, "h":0.5}
  ]'
```

### Dashboard + camera

```bash
curl -X POST http://pi:8686/layout \
  -H 'Content-Type: application/json' \
  -d '[
    {"name": "grafana", "type": "web", "url": "http://grafana.local:3000", "x":0, "y":0, "w":1.0, "h":0.5},
    {"name": "front-door", "type": "rtsp", "url": "rtsp://192.168.1.100:554/live", "x":0, "y":0.5, "w":1.0, "h":0.5}
  ]'
```

### Fullscreen kiosk web page

```bash
curl -X POST http://pi:8686/layout \
  -H 'Content-Type: application/json' \
  -d '[
    {"name": "kiosk", "type": "web", "url": "https://news.ycombinator.com", "x":0, "y":0, "w":1.0, "h":1.0}
  ]'
```

### Custom command (e.g., vlc)

```bash
curl -X POST http://pi:8686/pane \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "vlc",
    "type": "command",
    "cmd": "vlc --no-video-title-show rtsp://192.168.1.100/stream",
    "x": 0, "y": 0.5, "w": 1.0, "h": 0.5
  }'
```

### Extra mpv args

```bash
curl -X POST http://pi:8686/pane \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "camera",
    "type": "rtsp",
    "url": "rtsp://192.168.1.100/stream",
    "x": 0, "y": 0, "w": 1.0, "h": 1.0,
    "mpv_args": ["--framedrop=yes"]
  }'
```

### RTSP speed and codecs (e.g. Intelbras DVR)

| DVR setting | Typical codec | On Raspberry Pi use |
|-------------|---------------|---------------------|
| Stream principal (`subtype=0`) | H.265 / HEVC | Per-pane `"hwdec": "drm-copy"` (or set `MPV_HWDEC=drm-copy` if *all* panes are HEVC) |
| Stream extra (`subtype=1`) | H.264 | Default `MPV_HWDEC=v4l2m2m-copy` |

For a **multi‑camera grid**, prefer **`subtype=1`** URLs when the small resolution is acceptable: less bandwidth and reliable **hardware** H.264 decode. Use the main stream only where you need full resolution.

Optional pane fields:

| Field | Purpose |
|-------|---------|
| `hwdec` | Overrides global `MPV_HWDEC` for that pane (`drm-copy`, `v4l2m2m-copy`, `no`, …). |
| `rtsp_transport` | `tcp` or `udp` (UDP can reduce latency on a wired LAN). |
| `audio` | Set `true` if you need sound; default skips audio for lower CPU use. |

## Configuration

Environment variables (set in the systemd service or export before running):

| Variable | Default | Description |
|----------|---------|-------------|
| `DISPLAY_HOST` | `0.0.0.0` | Bind address |
| `DISPLAY_PORT` | `8686` | HTTP port |
| `DISPLAY` | `:0` | X11 display |
| `WATCHDOG_INTERVAL` | `10` | Seconds between child-process health checks |
| `MPV_HWDEC` | `v4l2m2m-copy` | Default hardware decode for RTSP (Pi H.264). Use `drm-copy` if every pane is HEVC. |
| `MPV_RTSP_FAST` | `1` | Low-latency RTSP tweaks (`cache=no`, small probe, no audio unless `audio: true`, `opengl-swapinterval=0`). Set `0` to disable. |
| `MPV_RTSP_TRANSPORT` | *(unset)* | Optional default `tcp` or `udp` for all RTSP panes (per-pane `rtsp_transport` overrides). |
| `MPV_EXTRA_ARGS` | *(unset)* | Extra mpv arguments (shell-split) appended to every RTSP launch. |

## Logs

```bash
journalctl -u pi-display-server -f
```

## Tips

- **Window manager**: Use `openbox` — it's minimal and respects xdotool move/resize without fighting.
- **Hardware decoding on Pi**: Do not use `--hwdec=auto` for RTSP; it usually falls back to software. Use `v4l2m2m-copy` for H.264 and `drm-copy` for H.265 mains (see table above or the web UI **hwdec** field).
- **Chromium GPU**: If Chromium is slow, try adding `"chromium_args": ["--enable-gpu-rasterization"]`.
- **Manual update**: SSH into the Pi and run `update-display` to pull the latest code and restart.
