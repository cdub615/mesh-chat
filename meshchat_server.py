"""
meshchat_server.py — FastAPI backend for MeshChat PWA
Serves the static frontend + provides REST/WebSocket API
Bridges to Reticulum LXMF for LoRa mesh messaging.

Install deps:
    pip install fastapi uvicorn websockets RNS LXMF

Run:
    python meshchat_server.py
    # Binds to 0.0.0.0:80 (change port below if needed)
"""

import asyncio
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import RNS
import LXMF
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ── Config ─────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"  # put index.html / manifest / sw.js here
DATA_DIR = Path.home() / ".meshchat"
DB_PATH = DATA_DIR / "messages.db"
IDENTITY_PATH = DATA_DIR / "identity"
CONFIG_PATH = DATA_DIR / "config.json"
PORT = 8080

DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def get_display_name():
    return load_config().get("display_name", "my-node")


# ── Database ────────────────────────────────────────────────────────────────
# One connection per call is fine; WAL mode (set once in init_db) lets readers
# and a single writer run concurrently so RNS callback threads and FastAPI
# handlers don't trip 'database is locked'. `timeout` covers brief contention.
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        REAL    NOT NULL,
                direction TEXT    NOT NULL,  -- 'in' | 'out'
                from_hash TEXT,
                to_hash   TEXT,
                body      TEXT    NOT NULL,
                status    TEXT    DEFAULT 'sent'  -- 'sent' | 'queued' | 'delivered' | 'failed'
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS peers (
                hash         TEXT PRIMARY KEY,
                display_name TEXT,
                first_seen   REAL NOT NULL,
                last_seen    REAL NOT NULL
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_messages_to_hash ON messages(to_hash)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_messages_from_hash ON messages(from_hash)")
        db.commit()


# ── WebSocket connection manager ────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(json.dumps(data))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.remove(ws)


manager = ConnectionManager()

# ── Reticulum / LXMF setup ─────────────────────────────────────────────────
lxmf_router: Optional[LXMF.LXMRouter] = None
local_destination: Optional[RNS.Destination] = (
    None  # returned by register_delivery_identity
)


def announce_received_callback(destination_hash, announced_identity, app_data):
    """Called when an announce is received from another node."""
    dest_hash_hex = RNS.prettyhexrep(destination_hash)
    display_name = None
    if app_data:
        try:
            display_name = app_data.decode("utf-8")
        except Exception:
            pass

    ts = time.time()
    with get_db() as db:
        existing = db.execute("SELECT hash FROM peers WHERE hash = ?", (dest_hash_hex,)).fetchone()
        if existing:
            db.execute(
                "UPDATE peers SET display_name = ?, last_seen = ? WHERE hash = ?",
                (display_name, ts, dest_hash_hex),
            )
        else:
            db.execute(
                "INSERT INTO peers (hash, display_name, first_seen, last_seen) VALUES (?, ?, ?, ?)",
                (dest_hash_hex, display_name, ts, ts),
            )
        db.commit()

    RNS.log(f"[MeshChat] Announce from {dest_hash_hex} ({display_name or 'unnamed'})")

    payload = {
        "type": "peer_discovered",
        "hash": dest_hash_hex,
        "display_name": display_name,
        "last_seen": ts,
    }
    try:
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(manager.broadcast(payload))
        )
    except RuntimeError:
        pass


def lxmf_delivery_callback(message: LXMF.LXMessage):
    """Called by LXMF when an inbound message arrives."""
    from_hash = RNS.prettyhexrep(message.source_hash)
    body = message.content.decode("utf-8", errors="replace") if message.content else ""
    ts = time.time()

    # Store in DB
    with get_db() as db:
        db.execute(
            "INSERT INTO messages (ts, direction, from_hash, body, status) VALUES (?,?,?,?,?)",
            (ts, "in", from_hash, body, "delivered"),
        )
        db.commit()

    # Push to all connected WebSocket clients
    payload = {
        "type": "message",
        "from": from_hash,
        "body": body,
        "time": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M"),
    }
    asyncio.get_event_loop().call_soon_threadsafe(
        lambda: asyncio.ensure_future(manager.broadcast(payload))
    )


def start_reticulum():
    global lxmf_router, local_destination

    RNS.Reticulum()  # Reticulum reads its own config at ~/.reticulum

    # Load or create identity
    if IDENTITY_PATH.exists():
        identity = RNS.Identity.from_file(str(IDENTITY_PATH))
        RNS.log(f"[MeshChat] Loaded identity: {RNS.prettyhexrep(identity.hash)}")
    else:
        identity = RNS.Identity()
        identity.to_file(str(IDENTITY_PATH))
        RNS.log(f"[MeshChat] Created new identity: {RNS.prettyhexrep(identity.hash)}")

    lxmf_router = LXMF.LXMRouter(storagepath=str(DATA_DIR / "lxmf"))
    local_destination = lxmf_router.register_delivery_identity(
        identity,
        display_name=get_display_name(),
    )
    lxmf_router.register_delivery_callback(lxmf_delivery_callback)

    RNS.Transport.register_announce_handler(announce_received_callback)

    # Auto-announce on boot so other nodes discover us
    local_destination.announce(app_data=get_display_name().encode("utf-8"))

    RNS.log(
        f"[MeshChat] LXMF ready. Address: {RNS.prettyhexrep(local_destination.hash)}"
    )


# ── App lifespan ────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_reticulum()
    yield
    # Reticulum / LXMF cleans up on process exit naturally


app = FastAPI(lifespan=lifespan)

# ── REST API ────────────────────────────────────────────────────────────────


@app.get("/api/identity")
def api_identity():
    if local_destination is None:
        return JSONResponse({"error": "not ready"}, status_code=503)
    return {
        "hash": RNS.prettyhexrep(local_destination.hash),
        "display_name": get_display_name(),
    }


@app.put("/api/display_name")
async def api_set_display_name(payload: dict):
    name = (payload or {}).get("display_name", "").strip()
    if not name:
        return JSONResponse({"error": "display_name is required"}, status_code=400)
    if len(name) > 32:
        return JSONResponse({"error": "display_name too long (max 32)"}, status_code=400)
    cfg = load_config()
    cfg["display_name"] = name
    save_config(cfg)
    if local_destination is not None:
        local_destination.display_name = name
    return {"display_name": name}


@app.post("/api/announce")
def api_announce():
    if local_destination is None:
        return JSONResponse({"error": "not ready"}, status_code=503)
    local_destination.announce(app_data=get_display_name().encode("utf-8"))
    return {"status": "announced"}


@app.get("/api/peers")
def api_peers():
    with get_db() as db:
        rows = db.execute(
            "SELECT hash, display_name, first_seen, last_seen FROM peers ORDER BY last_seen DESC"
        ).fetchall()
    peers = []
    for r in rows:
        peer = dict(r)
        try:
            dest_hash = bytes.fromhex(r["hash"].replace(".", ""))
            peer["has_path"] = RNS.Transport.has_path(dest_hash)
        except Exception:
            peer["has_path"] = False
        peers.append(peer)
    return peers


@app.delete("/api/peers/{peer_hash}")
def api_delete_peer(peer_hash: str):
    with get_db() as db:
        db.execute("DELETE FROM peers WHERE hash = ?", (peer_hash,))
        db.commit()
    return {"status": "removed"}


@app.get("/api/messages")
def api_get_messages(limit: int = 100):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM messages ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    messages = [dict(r) for r in reversed(rows)]
    for m in messages:
        m["time"] = datetime.fromtimestamp(m["ts"], tz=timezone.utc).strftime("%H:%M")
    return messages


@app.post("/api/messages")
async def api_send_message(payload: dict):
    """
    Body: { "to": "<hex hash>", "body": "<text>" }
    """
    if lxmf_router is None or local_destination is None:
        return JSONResponse({"error": "RNS not ready"}, status_code=503)

    to_hash = payload.get("to", "").strip()
    body = payload.get("body", "").strip()

    if not to_hash or not body:
        return JSONResponse({"error": "missing 'to' or 'body'"}, status_code=400)

    try:
        dest_hash = bytes.fromhex(to_hash.replace(".", ""))
    except ValueError:
        return JSONResponse({"error": "invalid destination hash"}, status_code=400)

    ts = time.time()

    # Store outbound immediately
    with get_db() as db:
        cursor = db.execute(
            "INSERT INTO messages (ts, direction, to_hash, body, status) VALUES (?,?,?,?,?)",
            (ts, "out", to_hash, body, "queued"),
        )
        msg_id = cursor.lastrowid
        db.commit()

    # Look up recipient — request path if not yet known
    if not RNS.Transport.has_path(dest_hash):
        RNS.Transport.request_path(dest_hash)

    recipient_identity = RNS.Identity.recall(dest_hash)
    if recipient_identity is None:
        # Path not yet known; still queue the message — LXMRouter will retry
        # Build destination speculatively so LXMRouter can resolve it later
        pass

    if recipient_identity is not None:
        rns_dest = RNS.Destination(
            recipient_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            "lxmf",
            "delivery",
        )
    else:
        # Destination unknown yet; return 202 Accepted and let caller retry
        return JSONResponse(
            {
                "id": msg_id,
                "status": "path_requested",
                "detail": "destination path unknown, request sent — retry in a few seconds",
            },
            status_code=202,
        )

    # Create LXMF message and queue for delivery
    # LXMessage(destination, source, content, title, desired_method)
    lxmf_message = LXMF.LXMessage(
        rns_dest,
        local_destination,
        body,
        desired_method=LXMF.LXMessage.DIRECT,
    )

    def delivery_status_callback(msg):
        # LXMessage states: STATE_SENT, STATE_DELIVERED, STATE_FAILED
        status = (
            "delivered" if msg.state == LXMF.LXMessage.STATE_DELIVERED else "failed"
        )
        with get_db() as db:
            db.execute("UPDATE messages SET status=? WHERE id=?", (status, msg_id))
            db.commit()
        asyncio.get_event_loop().call_soon_threadsafe(
            lambda: asyncio.ensure_future(
                manager.broadcast(
                    {
                        "type": "status_update",
                        "msg_id": msg_id,
                        "status": status,
                    }
                )
            )
        )

    lxmf_message.register_delivery_callback(delivery_status_callback)
    lxmf_router.handle_outbound(lxmf_message)

    return {"id": msg_id, "status": "queued", "ts": ts}


# ── WebSocket ────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            # Keep alive — client can optionally send pings
            data = await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)


# ── Static files (serve the PWA) ────────────────────────────────────────────
# Serve index.html and other static assets from ./static/
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
else:

    @app.get("/")
    def root():
        return {"message": "Put your PWA files in ./static/"}


# ── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(
        "meshchat_server:app",
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        reload=False,
    )
