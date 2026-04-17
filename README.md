# MeshChat

A device-agnostic PWA for communicating over [Reticulum](https://reticulum.network/) mesh networks via LoRa — no internet required.

## Why

Existing Reticulum clients (Sideband, NomadNet) are Android-focused or terminal-based, leaving iPhone users without a good option. MeshChat solves this by running as a Progressive Web App served directly from your Reticulum node. Connect to your node's WiFi from any device with a browser, open the page, and start chatting over the mesh.

## How It Works

MeshChat runs on a Raspberry Pi (or similar) connected to a LoRa radio. The Pi serves a lightweight web UI over its local network — either its WiFi Access Point or a shared LAN. Messages are sent and received over the [LXMF](https://github.com/markqvist/lxmf) protocol on top of Reticulum's encrypted transport layer.

```
Phone/Laptop                 Raspberry Pi                  LoRa Mesh
  Browser  ──── WiFi ────▶  MeshChat Server  ──── USB ──▶  Heltec LoRa 32 V3
   (PWA)        (AP)         (FastAPI + LXMF)               + Antenna
                                                               │
                                                          Other Nodes
```

- **Frontend**: Single-file vanilla JS PWA with offline caching via Service Worker
- **Backend**: FastAPI server bridging WebSocket/REST to Reticulum's LXMF messaging
- **Storage**: SQLite database at `~/.meshchat/` for message history and node identity

## Hardware Setup

This was built for and tested on:

- **Raspberry Pi 4B** running Pi OS Lite (headless)
- **Heltec LoRa 32 V4** connected via USB (running [RNode firmware](https://github.com/markqvist/RNode_Firmware))
- **LoRa antenna** matched to your frequency band
- Pi configured as a **WiFi Access Point** so clients can connect without existing infrastructure

Any hardware supported by Reticulum should work — the Pi and Heltec combo is just what's been tested.

## Quick Start

### 1. Install dependencies

```bash
python -m venv rns-venv
source rns-venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Reticulum

Make sure Reticulum is configured and can see your LoRa interface. See the [Reticulum docs](https://markqvist.github.io/Reticulum/manual/) for interface setup. A typical `~/.reticulum/config` will include a serial interface entry for the RNode.

### 3. Run MeshChat

```bash
python meshchat_server.py
```

The server binds to `0.0.0.0:8080`. Open `http://<pi-ip>:8080` from any device on the network.

### 4. Use it

- Open the URL in your phone's browser
- Tap "Add to Home Screen" for a native app experience (PWA install)
- Set your display name in Settings
- Enter a destination hash and start messaging

## Running as a Service

To have MeshChat start automatically on boot, set it up as a systemd service.

### 1. Create the service file

```bash
sudo nano /etc/systemd/system/meshchat.service
```

Paste the following (adjust `User` and paths if your username isn't `cdub`):

```ini
[Unit]
Description=MeshChat LoRa mesh web server
After=network.target

[Service]
Type=simple
User=cdub
WorkingDirectory=/home/cdub/meshchat
ExecStart=/home/cdub/meshchat/rns-venv/bin/python meshchat_server.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### 2. Enable and start it

```bash
sudo systemctl daemon-reload
sudo systemctl enable meshchat
sudo systemctl start meshchat
sudo systemctl status meshchat   # should show active (running)
```

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/identity` | Node hash and display name |
| `GET` | `/api/messages?limit=N` | Message history |
| `POST` | `/api/messages` | Send a message (`{"to": "<hash>", "body": "<text>"}`) |
| `PUT` | `/api/display_name` | Update display name |
| `POST` | `/api/announce` | Broadcast node presence over LoRa |
| `GET` | `/api/peers` | List discovered peers with path status |
| `DELETE` | `/api/peers/{hash}` | Remove a discovered peer |
| `WS` | `/ws` | Real-time message, status, and peer updates |

## License

[MIT](LICENSE)
