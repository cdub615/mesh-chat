# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MeshChat is an off-grid LoRa mesh communication PWA that enables peer-to-peer messaging over Reticulum/LXMF networks without internet. Designed for devices with LoRa hardware (e.g., Heltec boards) connected via USB.

## Deployment Target

Raspberry Pi 4B running Pi OS Lite. Python deps installed in a venv (`rns-venv`).

## Running

```bash
python -m venv rns-venv && source rns-venv/bin/activate
pip install fastapi uvicorn websockets RNS LXMF
python meshchat_server.py
# Binds to 0.0.0.0:8080
```

No build step, no package manager, no test framework. The app is four files served directly.

## Architecture

**Backend** (`meshchat_server.py`): FastAPI server that bridges browser clients to the Reticulum mesh network via LXMF. Handles REST API, WebSocket push, SQLite message storage, and Reticulum identity/routing.

**Frontend** (`index.html`): Single-file vanilla JS PWA with embedded CSS. Tab-based UI (Messages, Nodes, Settings). Uses WebSocket for real-time updates with polling fallback. Optimistic message rendering with status tracking.

**Offline support**: `sw.js` (Service Worker) caches the UI shell; `manifest.json` enables PWA install.

### Message flow

- **Outbound**: Browser → POST `/api/messages` → SQLite insert → LXMF send → delivery callback updates status → WebSocket broadcast to clients
- **Inbound**: LXMF delivery callback → SQLite insert → WebSocket broadcast → browser renders

### Key backend abstractions

- `ConnectionManager` — WebSocket connection pool with broadcast
- `lxmf_delivery_callback()` — bridges sync Reticulum callbacks to async FastAPI context via `call_soon_threadsafe()`
- `start_reticulum()` — initializes RNS identity and LXMF router at app startup (lifespan context manager)

### Data storage

- All user data lives in `~/.meshchat/` (messages.db, identity, lxmf/)
- Single SQLite table `messages` with columns: id, ts, direction, from_hash, to_hash, body, status
- Message status progression: `queued` → `sent` → `delivered` | `failed`

### API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/identity` | Node hash and display name |
| GET | `/api/messages?limit=N` | Message history |
| POST | `/api/messages` | Send message (returns 202 if path unknown) |
| WS | `/ws` | Real-time message/status push |

## Design Constraints

- No frontend framework — vanilla JS keeps size minimal for offline/embedded use
- Monolithic Python file — simplifies deployment on resource-constrained devices
- No auth layer at HTTP level — assumes trusted local device; identity verification happens at Reticulum transport layer
- Direct LXMF delivery only (no store-and-forward)
