# Pi Display Server

Remote HTTP control of what your Raspberry Pi 4 shows on HDMI. Send JSON layouts to arrange RTSP streams, web pages, images, and arbitrary commands on screen.

Designed for **Raspberry Pi OS Lite + X11**.

## Install

```bash
# Copy the project folder to your Pi, then:
cd pi-display-server
bash install.sh
```

This installs dependencies (`xdotool`, `mpv`, `chromium-browser`, `feh`, etc.), copies the server to `/opt/pi-display-server`, and sets up a systemd service on port **8686**.

## Prerequisites

Make sure X is running. If you're on Raspberry Pi OS Lite with X started manually:

```bash
# In your .xinitrc or startup script, start a simple window manager:
exec openbox-session
# Or just run X bare:
startx &
export DISPLAY=:0
```

The server needs a running X session and a window manager that respects move/resize hints. `openbox` works perfectly and is lightweight.

```bash
sudo apt-get install openbox
```

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
      "region": "bottom"
    },
    {
      "name": "dashboard",
      "type": "web",
      "url": "http://grafana.local:3000/dashboard",
      "region": "top"
    }
  ]'
```

### `POST /pane`

Add or replace a single pane without disturbing existing ones.

```bash
# Add a webcam feed to the top-right corner
curl -X POST http://pi:8686/pane \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "webcam",
    "type": "rtsp",
    "url": "rtsp://192.168.1.50/live",
    "region": "top-right"
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

## Regions (Positioning)

Every pane needs a position. Use a **named region** or **manual coordinates**.

### Named regions

| Region | Position |
|--------|----------|
| `full` | Entire screen |
| `top` | Top half |
| `bottom` | Bottom half |
| `left` | Left half |
| `right` | Right half |
| `top-left` | Top-left quarter |
| `top-right` | Top-right quarter |
| `bottom-left` | Bottom-left quarter |
| `bottom-right` | Bottom-right quarter |

### Manual coordinates

Use fractional (0.0–1.0) or absolute pixel values:

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

### Security camera dashboard (2×2 grid)

```bash
curl -X POST http://pi:8686/layout \
  -H 'Content-Type: application/json' \
  -d '[
    {"name": "cam1", "type": "rtsp", "url": "rtsp://192.168.1.101/stream", "region": "top-left"},
    {"name": "cam2", "type": "rtsp", "url": "rtsp://192.168.1.102/stream", "region": "top-right"},
    {"name": "cam3", "type": "rtsp", "url": "rtsp://192.168.1.103/stream", "region": "bottom-left"},
    {"name": "cam4", "type": "rtsp", "url": "rtsp://192.168.1.104/stream", "region": "bottom-right"}
  ]'
```

### Dashboard + camera

```bash
curl -X POST http://pi:8686/layout \
  -H 'Content-Type: application/json' \
  -d '[
    {"name": "grafana", "type": "web", "url": "http://grafana.local:3000", "region": "top"},
    {"name": "front-door", "type": "rtsp", "url": "rtsp://192.168.1.100:554/live", "region": "bottom"}
  ]'
```

### Fullscreen kiosk web page

```bash
curl -X POST http://pi:8686/layout \
  -H 'Content-Type: application/json' \
  -d '[
    {"name": "kiosk", "type": "web", "url": "https://news.ycombinator.com", "region": "full"}
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
    "region": "bottom"
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
    "region": "full",
    "mpv_args": ["--hwdec=auto", "--vo=gpu"]
  }'
```

## Configuration

Environment variables (set in the systemd service or export before running):

| Variable | Default | Description |
|----------|---------|-------------|
| `DISPLAY_HOST` | `0.0.0.0` | Bind address |
| `DISPLAY_PORT` | `8686` | HTTP port |
| `DISPLAY` | `:0` | X11 display |

## Logs

```bash
journalctl -u pi-display-server -f
```

## Tips

- **Window manager**: Use `openbox` — it's minimal and respects xdotool move/resize without fighting.
- **Hardware decoding**: Pass `"mpv_args": ["--hwdec=auto"]` for RTSP panes to use the Pi's GPU.
- **Chromium GPU**: If Chromium is slow, try adding `"chromium_args": ["--enable-gpu-rasterization"]`.
- **Auto-start X**: Add `startx` to your `.bash_profile` or use a display manager like `lightdm`.
