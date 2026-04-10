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
import tempfile
import threading
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


# Serializes read-modify-write on config.json so two concurrent PUTs to
# different keys can't clobber each other or corrupt the file.
_config_lock = threading.Lock()


def load_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}


def _atomic_write(path: Path, text: str) -> None:
    """Write text to path atomically via tempfile + os.replace."""
    # Same directory so os.replace is atomic (same filesystem).
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def update_config(**updates) -> dict:
    """Locked read-modify-write on config.json. Returns the new config dict."""
    with _config_lock:
        cfg = load_config()
        cfg.update(updates)
        _atomic_write(CONFIG_PATH, json.dumps(cfg, indent=2))
        return cfg


def get_display_name():
    return load_config().get("display_name", "my-node")


def get_wifi_ssid():
    return load_config().get("wifi_ssid", "")


def normalize_hash(s: str) -> bytes:
    """Strip prettyhexrep decorations (<, >, ., whitespace) and return raw bytes.

    Raises ValueError if the cleaned hex is not exactly 32 characters (16 bytes),
    which is the Reticulum destination hash truncation size.
    """
    cleaned = "".join(c for c in (s or "") if c in "0123456789abcdefABCDEF")
    if len(cleaned) != 32:
        raise ValueError(
            f"destination hash must be 32 hex characters (got {len(cleaned)}): {s!r}"
        )
    return bytes.fromhex(cleaned)


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
        # set + discard is race-safe: disconnect() is idempotent even if the
        # socket was already pruned by broadcast() or a concurrent cleanup.
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)

    async def broadcast(self, data: dict):
        dead = []
        # Iterate a snapshot so concurrent connect/disconnect can't mutate
        # the set mid-loop.
        for ws in list(self.active):
            try:
                await ws.send_text(json.dumps(data))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.discard(ws)


manager = ConnectionManager()

# ── Reticulum / LXMF setup ─────────────────────────────────────────────────
lxmf_router: Optional[LXMF.LXMRouter] = None
local_destination: Optional[RNS.Destination] = (
    None  # returned by register_delivery_identity
)
main_loop: Optional[asyncio.AbstractEventLoop] = None

# Rate-limit auto-reply announces so 2+ nodes don't echo each other forever.
ANNOUNCE_REPLY_COOLDOWN = 20.0
last_announce_ts: float = 0.0

# Startup state for Reticulum/LXMF. If init fails (e.g. RNode unplugged,
# ~/.reticulum/config missing), we keep the HTTP server up in degraded mode
# and a background task retries every RNS_RETRY_INTERVAL seconds.
rns_ready: bool = False
rns_error: Optional[str] = None
RNS_RETRY_INTERVAL = 30.0


def send_announce(reason: str):
    """Broadcast our identity and record the timestamp for reply rate-limiting."""
    global last_announce_ts
    if local_destination is None:
        return
    local_destination.announce(app_data=get_display_name().encode("utf-8"))
    last_announce_ts = time.time()
    RNS.log(f"[MeshChat] Announce sent ({reason})")


def _broadcast_from_thread(payload: dict):
    """Schedule a WebSocket broadcast from a non-async (RNS) thread."""
    if main_loop is None:
        RNS.log("[MeshChat] Cannot broadcast: main_loop not set", RNS.LOG_ERROR)
        return
    try:
        asyncio.run_coroutine_threadsafe(manager.broadcast(payload), main_loop)
        RNS.log(f"[MeshChat] Broadcast scheduled: {payload.get('type')}")
    except Exception as e:
        RNS.log(f"[MeshChat] Broadcast failed: {e}", RNS.LOG_ERROR)


class MeshChatAnnounceHandler:
    """Reticulum announce handler — must expose aspect_filter + received_announce."""

    aspect_filter = "lxmf.delivery"

    def received_announce(self, destination_hash, announced_identity, app_data):
        dest_hash_hex = RNS.prettyhexrep(destination_hash)
        display_name = None
        if app_data:
            try:
                display_name = app_data.decode("utf-8")
            except Exception:
                pass

        RNS.log(
            f"[MeshChat] received_announce: {dest_hash_hex} "
            f"({display_name or 'unnamed'})"
        )

        ts = time.time()
        try:
            with get_db() as db:
                existing = db.execute(
                    "SELECT hash FROM peers WHERE hash = ?", (dest_hash_hex,)
                ).fetchone()
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
            RNS.log(f"[MeshChat] Peer {dest_hash_hex} written to DB")
        except Exception as e:
            RNS.log(f"[MeshChat] DB write failed in announce handler: {e}", RNS.LOG_ERROR)
            return

        _broadcast_from_thread(
            {
                "type": "peer_discovered",
                "hash": dest_hash_hex,
                "display_name": display_name,
                "last_seen": ts,
            }
        )

        # Auto-reply so the announcing node can discover us too, but rate-
        # limited so a pair of nodes doesn't echo each other forever.
        since_last = ts - last_announce_ts
        if since_last > ANNOUNCE_REPLY_COOLDOWN:
            send_announce(f"reply to {dest_hash_hex}")
        else:
            RNS.log(
                f"[MeshChat] Skipping reply announce "
                f"(last announce {since_last:.1f}s ago)"
            )


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
    _broadcast_from_thread(payload)


def start_reticulum() -> bool:
    """Initialize Reticulum + LXMF. Returns True on success, False on failure.

    On failure, sets rns_ready=False and rns_error so /api/identity can report
    the degraded state. The caller (lifespan) schedules a retry loop.
    """
    global lxmf_router, local_destination, rns_ready, rns_error

    try:
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

        RNS.Transport.register_announce_handler(MeshChatAnnounceHandler())

        RNS.log(
            f"[MeshChat] LXMF ready. Address: {RNS.prettyhexrep(local_destination.hash)}"
        )

        rns_ready = True
        rns_error = None

        # Auto-announce on boot so other nodes discover us
        send_announce("startup")
        return True

    except Exception as e:
        rns_ready = False
        rns_error = f"{type(e).__name__}: {e}"
        RNS.log(
            f"[MeshChat] Reticulum init failed: {rns_error}. "
            f"Common causes: ~/.reticulum/config is missing or invalid, "
            f"the RNode device is not connected, or the identity file at "
            f"{IDENTITY_PATH} is corrupt. HTTP server will keep running and "
            f"retry every {int(RNS_RETRY_INTERVAL)}s.",
            RNS.LOG_ERROR,
        )
        return False


async def _rns_retry_loop():
    """Periodically retry Reticulum init until it succeeds."""
    while not rns_ready:
        await asyncio.sleep(RNS_RETRY_INTERVAL)
        RNS.log("[MeshChat] Retrying Reticulum init...")
        # Run the sync init off the event loop so we don't block ws/http.
        ok = await asyncio.get_running_loop().run_in_executor(None, start_reticulum)
        if ok:
            RNS.log("[MeshChat] Reticulum init recovered")
            return


# ── App lifespan ────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_loop
    main_loop = asyncio.get_running_loop()
    init_db()
    retry_task: Optional[asyncio.Task] = None
    if not start_reticulum():
        # Startup failed but the HTTP server stays up; schedule recovery.
        retry_task = asyncio.create_task(_rns_retry_loop())
    yield
    if retry_task is not None and not retry_task.done():
        retry_task.cancel()
    # Reticulum / LXMF cleans up on process exit naturally


app = FastAPI(lifespan=lifespan)

# ── REST API ────────────────────────────────────────────────────────────────


@app.get("/api/identity")
def api_identity():
    if not rns_ready or local_destination is None:
        return JSONResponse(
            {
                "ready": False,
                "error": rns_error or "Reticulum not initialized",
                "display_name": get_display_name(),
                "wifi_ssid": get_wifi_ssid(),
            },
            status_code=503,
        )
    return {
        "ready": True,
        "hash": RNS.prettyhexrep(local_destination.hash),
        "display_name": get_display_name(),
        "wifi_ssid": get_wifi_ssid(),
    }


@app.put("/api/display_name")
async def api_set_display_name(payload: dict):
    name = (payload or {}).get("display_name", "").strip()
    if not name:
        return JSONResponse({"error": "display_name is required"}, status_code=400)
    if len(name) > 32:
        return JSONResponse({"error": "display_name too long (max 32)"}, status_code=400)
    update_config(display_name=name)
    if local_destination is not None:
        local_destination.display_name = name
    return {"display_name": name}


@app.put("/api/wifi_ssid")
async def api_set_wifi_ssid(payload: dict):
    ssid = (payload or {}).get("wifi_ssid", "").strip()
    if not ssid:
        return JSONResponse({"error": "wifi_ssid is required"}, status_code=400)
    if len(ssid) > 32:
        return JSONResponse({"error": "wifi_ssid too long (max 32)"}, status_code=400)
    update_config(wifi_ssid=ssid)
    return {"wifi_ssid": ssid}


@app.post("/api/announce")
def api_announce():
    if local_destination is None:
        return JSONResponse({"error": "not ready"}, status_code=503)
    RNS.log(
        f"[MeshChat] /api/announce triggered from "
        f"{RNS.prettyhexrep(local_destination.hash)}"
    )
    send_announce("manual")
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
            peer["has_path"] = RNS.Transport.has_path(normalize_hash(r["hash"]))
        except ValueError as e:
            RNS.log(
                f"[MeshChat] /api/peers: skipping malformed stored hash {r['hash']!r}: {e}",
                RNS.LOG_WARNING,
            )
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
        dest_hash = normalize_hash(to_hash)
    except ValueError:
        RNS.log(
            f"[MeshChat] /api/messages rejected: invalid dest hash {to_hash!r}",
            RNS.LOG_ERROR,
        )
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
        # Path not yet known. Leave the row as 'queued' so the retry task
        # (wt1.1) can pick it up once the path resolves, and return 202.
        return JSONResponse(
            {
                "id": msg_id,
                "status": "path_requested",
                "detail": "destination path unknown, request sent — retry in a few seconds",
            },
            status_code=202,
        )

    rns_dest = RNS.Destination(
        recipient_identity,
        RNS.Destination.OUT,
        RNS.Destination.SINGLE,
        "lxmf",
        "delivery",
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
        _broadcast_from_thread(
            {
                "type": "status_update",
                "msg_id": msg_id,
                "status": status,
            }
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
