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
import logging
import os
import sqlite3
import sys
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
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Literal

# ── Settings ────────────────────────────────────────────────────────────────
# Everything below is overridable via MESHCHAT_* environment variables or
# a .env file at the project root. The module-level BASE_DIR / DATA_DIR /
# PORT / etc. constants below are aliases for Settings fields so the rest
# of the module keeps its existing constant references — ops configure via
# env, code reads the familiar names.
class Settings(BaseSettings):
    # Network
    host: str = "0.0.0.0"
    port: int = 8080
    # Filesystem
    data_dir: Path = Path.home() / ".meshchat"
    static_dir: Path = Path(__file__).parent / "static"
    # Protocol / scheduler tunables
    peer_reply_cooldown: float = 3600.0
    peer_reply_memory: float = 86400.0
    rns_retry_interval: float = 30.0
    queued_retry_delays: tuple = (5.0, 15.0, 45.0, 120.0, 300.0, 600.0)
    queued_retry_tick: float = 5.0
    dest_cache_ttl: float = 3600.0
    interfaces_broadcast_interval: float = 10.0
    ws_ping_interval: float = 30.0

    model_config = SettingsConfigDict(
        env_prefix="MESHCHAT_", env_file=".env", extra="ignore"
    )


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)

# Compatibility aliases. Keep the existing module-level names; every call
# site already uses these, and they just happen to now be driven by env.
BASE_DIR = Path(__file__).parent
STATIC_DIR = settings.static_dir
DATA_DIR = settings.data_dir
DB_PATH = DATA_DIR / "messages.db"
IDENTITY_PATH = DATA_DIR / "identity"
CONFIG_PATH = DATA_DIR / "config.json"
PORT = settings.port


# ── App version (cache-busting) ─────────────────────────────────────────────
def _compute_version() -> str:
    """Best-effort short version string used to invalidate the PWA cache.

    Tries $MESHCHAT_VERSION, then the short git HEAD, then the server file's
    mtime so a deployed tree without .git still changes its version when
    the code is updated.
    """
    env = os.environ.get("MESHCHAT_VERSION")
    if env:
        return env
    try:
        import subprocess

        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).parent),
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).decode().strip()
        if out:
            return out
    except Exception:
        pass
    try:
        return str(int(Path(__file__).stat().st_mtime))
    except Exception:
        return "dev"


APP_VERSION: str = _compute_version()


# ── Logging ────────────────────────────────────────────────────────────────
# We intentionally keep two log streams: `logger` for app-level events
# (lifecycle, retries, DB, graceful shutdown) and RNS.log for RNS/LXMF
# protocol events that belong with the RNS library's own noise. Ops can
# filter journalctl by either prefix.
logger = logging.getLogger("meshchat")


def _configure_logging() -> None:
    """Attach a stdout handler to the 'meshchat' logger.

    Idempotent — if a handler is already attached (e.g. uvicorn set one up
    or the module was reloaded) we leave it alone. Stays isolated from the
    root logger so RNS's own stderr output and uvicorn's access log aren't
    duplicated.
    """
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [meshchat] %(levelname)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    # Don't double-log to the root handler uvicorn may have configured.
    logger.propagate = False


_configure_logging()


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


def get_propagation_nodes() -> dict:
    """Return {outbound, inbound} propagation node hex hashes (or None each)."""
    cfg = load_config()
    return {
        "outbound": cfg.get("outbound_propagation_node") or None,
        "inbound": cfg.get("inbound_propagation_node") or None,
    }


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


# ── API models ─────────────────────────────────────────────────────────────
# Pydantic request/response models give us 422 validation, OpenAPI schemas
# in /docs, and a single source of truth for the wire format. The string
# validators reuse normalize_hash so 'valid Reticulum hash' is defined in
# one place regardless of surface (API, internal, or retry worker).

def _hash_validator(v: str) -> str:
    """Pydantic field validator: reject anything normalize_hash rejects, but
    pass through the original string so the stored form matches what the
    client sent (prettyhexrep decorations preserved)."""
    if not isinstance(v, str):
        raise ValueError("hash must be a string")
    normalize_hash(v)  # raises ValueError on bad length / non-hex
    return v


MethodName = Literal["direct", "opportunistic", "propagated"]


class DisplayNameIn(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=32)


class WifiSsidIn(BaseModel):
    wifi_ssid: str = Field(..., min_length=1, max_length=32)


class SendMessageIn(BaseModel):
    to: str = Field(..., description="Recipient destination hash (32 hex chars)")
    body: str = Field(..., min_length=1)
    method: MethodName = "opportunistic"

    @field_validator("to")
    @classmethod
    def _validate_to(cls, v: str) -> str:
        return _hash_validator(v)

    @field_validator("body")
    @classmethod
    def _strip_body(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("body cannot be whitespace only")
        return stripped


class PropagationNodesIn(BaseModel):
    """Either role is optional. Omitted = leave untouched; explicit null or
    empty string = clear that role."""
    outbound: Optional[str] = None
    inbound: Optional[str] = None

    @field_validator("outbound", "inbound")
    @classmethod
    def _validate_optional_hash(cls, v):
        if v in (None, ""):
            return v
        return _hash_validator(v)


class PropagationNodesOut(BaseModel):
    outbound: Optional[str] = None
    inbound: Optional[str] = None


class AnnounceOut(BaseModel):
    status: Literal["announced"] = "announced"


# ── Database ────────────────────────────────────────────────────────────────
# One connection per call is fine; WAL mode (set once in init_db) lets readers
# and a single writer run concurrently so RNS callback threads and FastAPI
# handlers don't trip 'database is locked'. `timeout` covers brief contention.
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _column_names(db, table: str) -> set:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column_if_missing(db, table: str, column: str, decl: str) -> None:
    """Idempotent ALTER TABLE ADD COLUMN — survives repeated startup."""
    if column not in _column_names(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


# ── Schema migrations ──────────────────────────────────────────────────────
# Each migration function is applied in order. PRAGMA user_version tracks
# the highest migration that has been applied on the current DB. To add a
# new migration: append a function to MIGRATIONS — the runner will pick it
# up on next startup. Every migration must be idempotent (safe to re-run
# against a DB that's already at that version) so partial failures are
# recoverable.

def _migration_1_initial_schema(db) -> None:
    """Baseline messages + peers tables, plus the columns and indexes that
    existed before schema versioning was introduced. Idempotent via
    CREATE TABLE IF NOT EXISTS, _add_column_if_missing, and
    CREATE INDEX IF NOT EXISTS — so pre-versioning DBs get adopted at
    v1 with no data migration needed."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        REAL    NOT NULL,
            direction TEXT    NOT NULL,  -- 'in' | 'out'
            from_hash TEXT,
            to_hash   TEXT,
            body      TEXT    NOT NULL,
            status    TEXT    DEFAULT 'sent'
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
    # SQLite can't add a UNIQUE column via ALTER, so uniqueness is enforced
    # by the partial index below (outbound rows have NULL lxmf_hash).
    _add_column_if_missing(db, "messages", "lxmf_hash", "TEXT")
    _add_column_if_missing(db, "messages", "attempts", "INTEGER DEFAULT 0")
    _add_column_if_missing(db, "messages", "next_retry_at", "REAL")
    _add_column_if_missing(db, "messages", "method", "TEXT DEFAULT 'opportunistic'")
    db.execute("CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_messages_to_hash ON messages(to_hash)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_messages_from_hash ON messages(from_hash)")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_lxmf_hash "
        "ON messages(lxmf_hash) WHERE lxmf_hash IS NOT NULL"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_queued "
        "ON messages(status, next_retry_at) WHERE status = 'queued'"
    )


MIGRATIONS: list = [_migration_1_initial_schema]
SCHEMA_VERSION: int = len(MIGRATIONS)


def _run_migrations(db) -> int:
    """Advance PRAGMA user_version by running each pending migration in
    order. Returns the number of migrations applied this run."""
    current = db.execute("PRAGMA user_version").fetchone()[0]
    applied = 0
    for i, fn in enumerate(MIGRATIONS, start=1):
        if current < i:
            logger.info("Applying schema migration %d: %s", i, fn.__name__)
            fn(db)
            # PRAGMA user_version can't be parameterised — i is an int we
            # control here so the f-string is safe.
            db.execute(f"PRAGMA user_version = {i}")
            applied += 1
    return applied


def init_db():
    with get_db() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        _run_migrations(db)
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
            except Exception as e:
                # Starlette/websockets raises a zoo of exception types on
                # send to a closed connection — treat any send failure as
                # evidence the connection is dead and prune it.
                logger.debug(
                    "Pruning dead WebSocket after %s: %s",
                    type(e).__name__, e,
                )
                dead.append(ws)
        for ws in dead:
            self.active.discard(ws)


manager = ConnectionManager()


# Map LXMessage.STATE_* integer values to the lowercase status string we
# persist in the `messages` table and broadcast over WebSocket. Built by
# introspection so new LXMF states are handled by name without a code edit.
def _build_lxmf_state_map() -> dict:
    mapping: dict = {}
    for attr in dir(LXMF.LXMessage):
        if not attr.startswith("STATE_"):
            continue
        value = getattr(LXMF.LXMessage, attr)
        if isinstance(value, int):
            mapping[value] = attr[len("STATE_"):].lower()
    return mapping


_LXMF_STATE_NAMES: dict = _build_lxmf_state_map()


def lxmf_status_name(state) -> str:
    """Return a canonical lowercase status for an LXMessage state integer."""
    return _LXMF_STATE_NAMES.get(state, "unknown")


# Caller-facing delivery method strings -> LXMF.LXMessage desired_method
# constants. OPPORTUNISTIC is the default because it's best-effort single
# packet — more robust on LoRa than DIRECT (which requires a live Link) and
# lighter than PROPAGATED (which requires a propagation node).
LXMF_METHOD_BY_NAME: dict = {
    "direct": LXMF.LXMessage.DIRECT,
    "opportunistic": LXMF.LXMessage.OPPORTUNISTIC,
    "propagated": LXMF.LXMessage.PROPAGATED,
}
DEFAULT_METHOD = "opportunistic"


# Cache RNS.Destination instances by hex recipient hash. Building a fresh
# Destination (identity + category + type + app + aspect) per outbound send
# is pure overhead when the same peer is messaged repeatedly. Entries are
# dropped if the path is lost (has_path returns false) or the TTL expires.
_dest_cache: dict = {}  # hex_hash -> (RNS.Destination, expires_at)
DEST_CACHE_TTL = settings.dest_cache_ttl


def _get_cached_destination(hex_key: str, dest_hash: bytes):
    entry = _dest_cache.get(hex_key)
    if entry is None:
        return None
    dest, expires_at = entry
    if expires_at <= time.time() or not RNS.Transport.has_path(dest_hash):
        _dest_cache.pop(hex_key, None)
        return None
    return dest


def _cache_destination(hex_key: str, dest) -> None:
    _dest_cache[hex_key] = (dest, time.time() + DEST_CACHE_TTL)


# ── Reticulum / LXMF setup ─────────────────────────────────────────────────
lxmf_router: Optional[LXMF.LXMRouter] = None
local_destination: Optional[RNS.Destination] = (
    None  # returned by register_delivery_identity
)
main_loop: Optional[asyncio.AbstractEventLoop] = None

# Per-peer auto-reply throttle: we reply to a peer's announce at most once
# per PEER_REPLY_COOLDOWN so a 3+ node mesh can't chain-flood announce
# replies. Entries older than PEER_REPLY_MEMORY are pruned to bound memory.
PEER_REPLY_COOLDOWN = settings.peer_reply_cooldown
PEER_REPLY_MEMORY = settings.peer_reply_memory
_peer_last_reply_ts: dict = {}  # dest_hash_hex -> last time we auto-replied

# Startup state for Reticulum/LXMF. If init fails (e.g. RNode unplugged,
# ~/.reticulum/config missing), we keep the HTTP server up in degraded mode
# and a background task retries every RNS_RETRY_INTERVAL seconds.
rns_ready: bool = False
rns_error: Optional[str] = None
RNS_RETRY_INTERVAL = settings.rns_retry_interval

# Outbound retry schedule for messages whose destination path isn't yet
# known. Index = attempt count that just failed; value = seconds until the
# next retry. After QUEUED_RETRY_DELAYS is exhausted the row flips to failed.
QUEUED_RETRY_DELAYS: list = list(settings.queued_retry_delays)
QUEUED_RETRY_TICK = settings.queued_retry_tick


def send_announce(reason: str):
    """Broadcast our identity."""
    if local_destination is None:
        return
    local_destination.announce(app_data=get_display_name().encode("utf-8"))
    RNS.log(f"[MeshChat] Announce sent ({reason})")


def _should_reply_to_peer(peer_hash_hex: str, now: float) -> bool:
    """Return True iff we should auto-reply to this peer's announce.

    Skip our own hash (paranoid guard against reflections), skip peers we
    replied to within PEER_REPLY_COOLDOWN, and opportunistically prune
    entries older than PEER_REPLY_MEMORY so the dict stays bounded.
    """
    if local_destination is not None:
        if peer_hash_hex == RNS.prettyhexrep(local_destination.hash):
            return False
    # Prune while we're here.
    stale_cutoff = now - PEER_REPLY_MEMORY
    for k in [k for k, v in _peer_last_reply_ts.items() if v < stale_cutoff]:
        _peer_last_reply_ts.pop(k, None)
    last = _peer_last_reply_ts.get(peer_hash_hex)
    if last is not None and (now - last) < PEER_REPLY_COOLDOWN:
        return False
    return True


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
            except UnicodeDecodeError as e:
                RNS.log(
                    f"[MeshChat] received_announce: non-UTF8 app_data: {e}",
                    RNS.LOG_WARNING,
                )

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

        # Auto-reply so the announcing node can discover us too. Per-peer
        # cooldown prevents chain-floods on 3+ node meshes.
        if _should_reply_to_peer(dest_hash_hex, ts):
            send_announce(f"reply to {dest_hash_hex}")
            _peer_last_reply_ts[dest_hash_hex] = ts
        else:
            RNS.log(
                f"[MeshChat] Skipping reply announce to {dest_hash_hex} "
                f"(within per-peer cooldown or self-announce)"
            )


def lxmf_delivery_callback(message: LXMF.LXMessage):
    """Called by LXMF when an inbound message arrives."""
    from_hash = RNS.prettyhexrep(message.source_hash)
    body = message.content.decode("utf-8", errors="replace") if message.content else ""
    ts = time.time()

    # LXMF gives each message a unique hash; use it as the dedupe key so
    # retransmissions and propagation-node redelivery don't double-insert.
    raw_hash = getattr(message, "hash", None)
    lxmf_hash = raw_hash.hex() if isinstance(raw_hash, (bytes, bytearray)) else None

    # Store in DB (INSERT OR IGNORE on lxmf_hash — if the row already exists
    # the rowcount stays 0 and we skip the broadcast).
    with get_db() as db:
        cursor = db.execute(
            "INSERT OR IGNORE INTO messages "
            "(ts, direction, from_hash, body, status, lxmf_hash) "
            "VALUES (?,?,?,?,?,?)",
            (ts, "in", from_hash, body, "delivered", lxmf_hash),
        )
        db.commit()
        inserted = cursor.rowcount > 0

    if not inserted:
        RNS.log(
            f"[MeshChat] Duplicate inbound dropped (lxmf_hash={lxmf_hash})"
        )
        return

    # Push to all connected WebSocket clients
    payload = {
        "type": "message",
        "from": from_hash,
        "body": body,
        "time": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M"),
    }
    _broadcast_from_thread(payload)


def _apply_propagation_nodes(router) -> None:
    """Register configured outbound/inbound propagation nodes with LXMF.

    Silently skips fields that aren't set or don't parse as valid hashes —
    misconfiguration shouldn't block startup. Called from start_reticulum
    (startup) and PUT /api/propagation_nodes (runtime update).
    """
    nodes = get_propagation_nodes()
    for role, setter_name in (
        ("outbound", "set_outbound_propagation_node"),
        ("inbound", "set_inbound_propagation_node"),
    ):
        raw = nodes.get(role)
        if not raw:
            continue
        try:
            dest_hash = normalize_hash(raw)
        except ValueError as e:
            RNS.log(
                f"[MeshChat] Ignoring malformed {role} propagation node {raw!r}: {e}",
                RNS.LOG_WARNING,
            )
            continue
        setter = getattr(router, setter_name, None)
        if not callable(setter):
            RNS.log(
                f"[MeshChat] LXMRouter has no {setter_name}; skipping {role}",
                RNS.LOG_WARNING,
            )
            continue
        try:
            setter(dest_hash)
            RNS.log(f"[MeshChat] Configured {role} propagation node: {raw}")
        except Exception as e:
            RNS.log(
                f"[MeshChat] Failed to set {role} propagation node {raw!r}: {e}",
                RNS.LOG_ERROR,
            )


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

        _apply_propagation_nodes(lxmf_router)

        RNS.log(
            f"[MeshChat] LXMF ready. Address: {RNS.prettyhexrep(local_destination.hash)}"
        )

        rns_ready = True
        rns_error = None
        logger.info(
            "Reticulum/LXMF ready; address %s",
            RNS.prettyhexrep(local_destination.hash),
        )

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
        logger.error("Reticulum init failed: %s", rns_error)
        return False


async def _rns_retry_loop():
    """Periodically retry Reticulum init until it succeeds."""
    while not rns_ready:
        await asyncio.sleep(RNS_RETRY_INTERVAL)
        logger.info("Retrying Reticulum init...")
        # Run the sync init off the event loop so we don't block ws/http.
        ok = await asyncio.get_running_loop().run_in_executor(None, start_reticulum)
        if ok:
            logger.info("Reticulum init recovered after degraded startup")
            return


def _resurrect_queued_messages() -> int:
    """Reset next_retry_at=0 on any leftover queued rows so they get another
    shot on the next retry tick after a restart. Returns the row count."""
    with get_db() as db:
        cursor = db.execute(
            "UPDATE messages SET next_retry_at = 0 WHERE status = 'queued'"
        )
        db.commit()
        return cursor.rowcount


# ── App lifespan ────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_loop
    main_loop = asyncio.get_running_loop()
    logger.info("MeshChat starting up")
    init_db()
    resurrected = _resurrect_queued_messages()
    if resurrected:
        RNS.log(
            f"[MeshChat] Resurrected {resurrected} queued message(s) for retry"
        )
        logger.info("Resurrected %d queued message(s) for retry", resurrected)
    rns_retry_task: Optional[asyncio.Task] = None
    if not start_reticulum():
        rns_retry_task = asyncio.create_task(_rns_retry_loop())
    queued_retry_task = asyncio.create_task(_queued_retry_loop())
    interfaces_task = asyncio.create_task(_interfaces_broadcast_loop())
    try:
        yield
    finally:
        logger.info("MeshChat shutting down")
        await _graceful_shutdown(
            [queued_retry_task, interfaces_task, rns_retry_task]
        )
        logger.info("MeshChat shutdown complete")


async def _graceful_shutdown(tasks: list) -> None:
    """Cancel background tasks, close client WebSockets with 1001, flush
    LXMF and Reticulum state. Every step is isolated so a failure in one
    doesn't block the others — we want to do as much cleanup as we can
    before the process exits."""
    # 1. Cancel background tasks and await them so they stop touching LXMF
    #    before we call exit_handler.
    for t in tasks:
        if t is not None and not t.done():
            t.cancel()
    for t in tasks:
        if t is None:
            continue
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    # 2. Close WebSocket clients with Going Away (1001) so browsers show a
    #    proper disconnect instead of reconnect-looping against a dead TCP.
    for ws in list(manager.active):
        try:
            await ws.close(code=1001)
        except Exception as e:
            logger.debug(
                "WebSocket close during shutdown failed with %s: %s",
                type(e).__name__, e,
            )
    manager.active.clear()

    # 3. Flush LXMF state so queued deliveries and peer tickets land on
    #    disk. exit_handler exists on LXMRouter (confirmed LXMF 0.9.4).
    if lxmf_router is not None:
        try:
            lxmf_router.exit_handler()
        except Exception as e:
            # RNS.log itself can fail during shutdown (stdout already
            # closed, RNS already torn down); fall through to logger.
            logger.error("LXMF exit_handler failed: %s", e)

    # 4. Flush RNS state. Static on Reticulum in RNS 1.1.x.
    try:
        rns_exit = getattr(RNS.Reticulum, "exit_handler", None)
        if callable(rns_exit):
            rns_exit()
    except Exception as e:
        logger.error("RNS exit_handler failed: %s", e)


app = FastAPI(lifespan=lifespan)


def require_rns_ready() -> None:
    """FastAPI dependency: raises 503 when RNS/LXMF aren't ready to serve.

    /api/identity intentionally does NOT use this — it owns a custom 503
    body shape that the frontend consumes to decide between 'offline'
    and 'degraded' states. Every other endpoint that touches
    lxmf_router or local_destination should depend on this guard.
    """
    if not rns_ready or local_destination is None or lxmf_router is None:
        raise HTTPException(
            status_code=503,
            detail=rns_error or "Reticulum not ready",
        )


# ── REST API ────────────────────────────────────────────────────────────────


@app.get("/api/version")
async def api_version():
    """Expose the current app version. The frontend polls this for update
    detection; the service worker is also served with this string baked
    into its body so the SW bytes change across deploys.
    """
    return {"version": APP_VERSION}


@app.get("/sw.js")
async def sw_js():
    """Serve the service worker with APP_VERSION substituted in.

    The SW file's bytes change whenever APP_VERSION changes, which is what
    tells the browser there's a new service worker worth installing. Must
    not be cached by intermediaries, or cache-busting defeats itself.
    """
    sw_path = STATIC_DIR / "sw.js"
    try:
        text = sw_path.read_text()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="sw.js missing")
    text = text.replace("__MESHCHAT_VERSION__", APP_VERSION)
    return Response(
        content=text,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/api/identity")
async def api_identity():
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
async def api_set_display_name(payload: DisplayNameIn):
    name = payload.display_name.strip()
    update_config(display_name=name)
    if local_destination is not None:
        local_destination.display_name = name
    return {"display_name": name}


@app.put("/api/wifi_ssid")
async def api_set_wifi_ssid(payload: WifiSsidIn):
    ssid = payload.wifi_ssid.strip()
    update_config(wifi_ssid=ssid)
    return {"wifi_ssid": ssid}


@app.get("/api/propagation_nodes", response_model=PropagationNodesOut)
async def api_get_propagation_nodes():
    return get_propagation_nodes()


@app.put("/api/propagation_nodes", response_model=PropagationNodesOut)
async def api_set_propagation_nodes(payload: PropagationNodesIn):
    """Set or clear outbound/inbound propagation nodes.

    Semantics:
    - Omitting a role key = leave it untouched.
    - Explicit null or empty string = clear that role.
    - Invalid hash = 422 (Pydantic validator).

    We key off payload.model_fields_set to distinguish 'omitted' from
    'explicitly null', which is what Pydantic gives us for free.
    """
    set_fields = payload.model_fields_set
    updates: dict = {}
    for role in ("outbound", "inbound"):
        if role not in set_fields:
            continue
        val = getattr(payload, role)
        updates[f"{role}_propagation_node"] = (
            None if val in (None, "") else val.strip()
        )
    if updates:
        update_config(**updates)
        if lxmf_router is not None:
            _apply_propagation_nodes(lxmf_router)
    return get_propagation_nodes()


@app.post("/api/announce", dependencies=[Depends(require_rns_ready)])
async def api_announce():
    RNS.log(
        f"[MeshChat] /api/announce triggered from "
        f"{RNS.prettyhexrep(local_destination.hash)}"
    )
    send_announce("manual")
    return {"status": "announced"}


def _path_metrics(dest_hash: bytes) -> dict:
    """Look up hop count, next hop, and path timestamps from RNS.Transport.

    RNS.Transport.path_table rows are [TIMESTAMP, NEXT_HOP, HOPS, EXPIRES, ...].
    Missing/unknown entries return null fields. hops_to() returns the
    PATHFINDER_M sentinel (128) when the path is unknown — normalise that
    to null so the frontend can trust a non-null hops field.
    """
    metrics: dict = {
        "hops": None,
        "next_hop": None,
        "path_cost": None,
        "last_path_update": None,
        "path_expires": None,
    }
    try:
        hops = RNS.Transport.hops_to(dest_hash)
        if hops != RNS.Transport.PATHFINDER_M:
            metrics["hops"] = hops
            # RNS 1.1.x uses hops as path cost; keep the field for forward-compat.
            metrics["path_cost"] = hops
    except Exception as e:
        # RNS internals can raise a variety of lookup-time errors; fall
        # back to null metrics rather than 500-ing the /api/peers response.
        logger.debug("hops_to lookup failed for %s: %s: %s", dest_hash.hex(), type(e).__name__, e)

    # path_table read is best-effort; lock is held internally by RNS when
    # the table is written, but a dict.get on a tuple is atomic enough for
    # our read-only purposes.
    entry = RNS.Transport.path_table.get(dest_hash)
    if entry is not None:
        try:
            metrics["last_path_update"] = entry[0]
            nh = entry[1]
            if isinstance(nh, (bytes, bytearray)):
                metrics["next_hop"] = nh.hex()
            metrics["path_expires"] = entry[3]
        except (IndexError, TypeError):
            pass
    return metrics


def _db_list_peers() -> list:
    """Blocking: fetch peers with path metrics. Runs in the default executor."""
    with get_db() as db:
        rows = db.execute(
            "SELECT hash, display_name, first_seen, last_seen FROM peers ORDER BY last_seen DESC"
        ).fetchall()
    peers = []
    for r in rows:
        peer = dict(r)
        try:
            dest_hash = normalize_hash(r["hash"])
            peer["has_path"] = RNS.Transport.has_path(dest_hash)
            peer.update(_path_metrics(dest_hash))
        except ValueError as e:
            RNS.log(
                f"[MeshChat] /api/peers: skipping malformed stored hash {r['hash']!r}: {e}",
                RNS.LOG_WARNING,
            )
            peer["has_path"] = False
            peer.update({
                "hops": None, "next_hop": None, "path_cost": None,
                "last_path_update": None, "path_expires": None,
            })
        peers.append(peer)
    return peers


@app.get("/api/peers")
async def api_peers():
    return await asyncio.get_running_loop().run_in_executor(None, _db_list_peers)


def _serialize_interface(iface) -> dict:
    """Snapshot a Reticulum Interface for /api/interfaces.

    Uses getattr with defaults because the Interface base class and each
    subclass set different fields; e.g. AutoInterface has ifac_netname,
    base Interface doesn't. Keys always appear in the response so the
    frontend contract is stable across RNS versions.
    """
    return {
        "name": getattr(iface, "name", None) or type(iface).__name__,
        "type": type(iface).__name__,
        "online": bool(getattr(iface, "online", False)),
        "bitrate": getattr(iface, "bitrate", None),
        "mtu": getattr(iface, "HW_MTU", None),
        "rxb": getattr(iface, "rxb", 0),
        "txb": getattr(iface, "txb", 0),
        "created": getattr(iface, "created", None),
        "can_receive": bool(getattr(iface, "IN", False)),
        "can_transmit": bool(getattr(iface, "OUT", False)),
        "ifac_netname": getattr(iface, "ifac_netname", None),
    }


def _snapshot_interfaces() -> list:
    try:
        interfaces = list(getattr(RNS.Transport, "interfaces", []) or [])
    except Exception as e:
        logger.debug("Interface enumeration failed: %s: %s", type(e).__name__, e)
        return []
    return [_serialize_interface(i) for i in interfaces]


@app.get("/api/interfaces")
async def api_interfaces():
    return _snapshot_interfaces()


INTERFACES_BROADCAST_INTERVAL = settings.interfaces_broadcast_interval

# WebSocket idle timeout; if no client message arrives within this window,
# we send a server ping to keep the connection alive and to detect dead
# sockets (a failed send then tears it down).
WS_PING_INTERVAL = settings.ws_ping_interval


async def _interfaces_broadcast_loop():
    """Push interface snapshots over WebSocket so the frontend can pulse
    radio status without polling. Skips ticks when no one is connected."""
    while True:
        try:
            await asyncio.sleep(INTERFACES_BROADCAST_INTERVAL)
            if not manager.active:
                continue
            await manager.broadcast(
                {"type": "interfaces", "interfaces": _snapshot_interfaces()}
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            RNS.log(f"[MeshChat] interfaces broadcast error: {e}", RNS.LOG_ERROR)


def _db_delete_peer(peer_hash: str) -> None:
    with get_db() as db:
        db.execute("DELETE FROM peers WHERE hash = ?", (peer_hash,))
        db.commit()


@app.delete("/api/peers/{peer_hash}")
async def api_delete_peer(peer_hash: str):
    await asyncio.get_running_loop().run_in_executor(
        None, _db_delete_peer, peer_hash
    )
    return {"status": "removed"}


def _db_list_messages(limit: int) -> list:
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM messages ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    messages = [dict(r) for r in reversed(rows)]
    for m in messages:
        m["time"] = datetime.fromtimestamp(m["ts"], tz=timezone.utc).strftime("%H:%M")
    return messages


@app.get("/api/messages")
async def api_get_messages(limit: int = 100):
    return await asyncio.get_running_loop().run_in_executor(
        None, _db_list_messages, limit
    )


def _make_delivery_status_callback(msg_id: int):
    """Return a sync callback the LXMF library can invoke on state change."""
    def cb(msg):
        status = lxmf_status_name(msg.state)
        with get_db() as db:
            db.execute("UPDATE messages SET status=? WHERE id=?", (status, msg_id))
            db.commit()
        _broadcast_from_thread(
            {"type": "status_update", "msg_id": msg_id, "status": status}
        )
    return cb


def _try_submit_outbound(
    msg_id: int, to_hash: str, body: str, method: str = DEFAULT_METHOD
) -> bool:
    """Attempt to resolve the destination path and hand the message to LXMF.

    Returns True if LXMRouter accepted it, False if the path is still unknown
    (caller should leave the row as 'queued' for the retry worker).
    """
    if lxmf_router is None or local_destination is None:
        return False

    try:
        dest_hash = normalize_hash(to_hash)
    except ValueError:
        # Row was validated at insert time; a bad stored hash is unrecoverable.
        RNS.log(
            f"[MeshChat] msg {msg_id}: stored hash {to_hash!r} is invalid, marking failed",
            RNS.LOG_ERROR,
        )
        return False

    hex_key = dest_hash.hex()
    rns_dest = _get_cached_destination(hex_key, dest_hash)
    if rns_dest is None:
        if not RNS.Transport.has_path(dest_hash):
            RNS.Transport.request_path(dest_hash)
        recipient_identity = RNS.Identity.recall(dest_hash)
        if recipient_identity is None:
            return False
        rns_dest = RNS.Destination(
            recipient_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            "lxmf",
            "delivery",
        )
        _cache_destination(hex_key, rns_dest)

    # Unknown method strings fall back to the default — validation happened
    # at the API boundary; this is belt-and-suspenders for rows re-read from
    # an older schema that predates the method column.
    desired_method = LXMF_METHOD_BY_NAME.get(method, LXMF_METHOD_BY_NAME[DEFAULT_METHOD])
    # title and fields are reserved for future use (subject lines, reactions,
    # custom metadata). Passing them explicitly so the LXMF constructor
    # call is self-documenting and a future feature can wire them through
    # without touching the signature.
    lxmf_message = LXMF.LXMessage(
        rns_dest,
        local_destination,
        body,
        title="",
        fields={},
        desired_method=desired_method,
    )

    cb = _make_delivery_status_callback(msg_id)
    lxmf_message.register_delivery_callback(cb)
    failed_register = getattr(lxmf_message, "register_failed_callback", None)
    if callable(failed_register):
        failed_register(cb)

    lxmf_router.handle_outbound(lxmf_message)
    return True


def _mark_outbound(msg_id: int) -> None:
    with get_db() as db:
        db.execute("UPDATE messages SET status=? WHERE id=?", ("outbound", msg_id))
        db.commit()


def _schedule_retry_or_fail(msg_id: int, attempts_so_far: int) -> str:
    """Bump attempts and either schedule a retry or flip to failed.

    Returns the new status string for logging / broadcasting.
    """
    next_attempts = attempts_so_far + 1
    if next_attempts >= len(QUEUED_RETRY_DELAYS):
        with get_db() as db:
            db.execute(
                "UPDATE messages SET status=?, attempts=?, next_retry_at=NULL WHERE id=?",
                ("failed", next_attempts, msg_id),
            )
            db.commit()
        return "failed"
    delay = QUEUED_RETRY_DELAYS[next_attempts]
    next_at = time.time() + delay
    with get_db() as db:
        db.execute(
            "UPDATE messages SET attempts=?, next_retry_at=? WHERE id=?",
            (next_attempts, next_at, msg_id),
        )
        db.commit()
    return "queued"


async def _queued_retry_loop():
    """Background task: walk queued rows whose next_retry_at has passed and
    attempt LXMF submission. Stops ticking individual rows after the retry
    schedule is exhausted, flipping them to 'failed'."""
    while True:
        try:
            await asyncio.sleep(QUEUED_RETRY_TICK)
            if not rns_ready:
                continue
            now = time.time()
            with get_db() as db:
                rows = db.execute(
                    "SELECT id, to_hash, body, "
                    "       COALESCE(attempts, 0) AS attempts, "
                    "       COALESCE(method, ?) AS method "
                    "FROM messages "
                    "WHERE status = 'queued' "
                    "  AND (next_retry_at IS NULL OR next_retry_at <= ?)",
                    (DEFAULT_METHOD, now),
                ).fetchall()
            for r in rows:
                msg_id = r["id"]
                ok = _try_submit_outbound(msg_id, r["to_hash"], r["body"], r["method"])
                if ok:
                    _mark_outbound(msg_id)
                    await manager.broadcast(
                        {"type": "status_update", "msg_id": msg_id, "status": "outbound"}
                    )
                else:
                    new_status = _schedule_retry_or_fail(msg_id, r["attempts"])
                    if new_status == "failed":
                        await manager.broadcast(
                            {"type": "status_update", "msg_id": msg_id, "status": "failed"}
                        )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            RNS.log(f"[MeshChat] retry loop error: {e}", RNS.LOG_ERROR)


@app.post("/api/messages", dependencies=[Depends(require_rns_ready)])
async def api_send_message(payload: SendMessageIn):
    """Queue an outbound LXMF message.

    Pydantic validates `to` (32-hex via normalize_hash), `body` (non-empty),
    and `method` (Literal enum). Returns 200 {status: outbound} if the path
    resolves synchronously, or 202 {status: path_requested} if the retry
    worker will take over.
    """
    to_hash = payload.to.strip()
    body = payload.body  # already stripped by the validator
    method = payload.method

    ts = time.time()

    # Persist as queued with next_retry_at=0 so the retry worker will pick
    # it up on the next tick if the first-shot synchronous attempt fails.
    with get_db() as db:
        cursor = db.execute(
            "INSERT INTO messages "
            "(ts, direction, to_hash, body, status, attempts, next_retry_at, method) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ts, "out", to_hash, body, "queued", 0, 0.0, method),
        )
        msg_id = cursor.lastrowid
        db.commit()

    # First-shot synchronous attempt — if the path is already cached, the
    # message is in LXMF's hands before the HTTP response returns.
    if _try_submit_outbound(msg_id, to_hash, body, method):
        _mark_outbound(msg_id)
        await manager.broadcast(
            {"type": "status_update", "msg_id": msg_id, "status": "outbound"}
        )
        return {"id": msg_id, "status": "outbound", "ts": ts, "method": method}

    # Path unknown; caller gets 202 and the retry worker will try again.
    return JSONResponse(
        {
            "id": msg_id,
            "status": "path_requested",
            "method": method,
            "detail": "destination path unknown, request sent — will retry automatically",
        },
        status_code=202,
    )


# ── WebSocket ────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            try:
                await asyncio.wait_for(
                    ws.receive_text(), timeout=WS_PING_INTERVAL
                )
                # We don't do anything with incoming text today; it's there
                # so the client can ping us if it wants. Reaching here just
                # means the connection is still alive.
            except asyncio.TimeoutError:
                # No activity in WS_PING_INTERVAL: send an app-level ping.
                # If the send fails, the client is gone — break so the
                # finally clause prunes the connection.
                try:
                    await ws.send_text(json.dumps({"type": "ping"}))
                except Exception as e:
                    logger.debug(
                        "WebSocket ping send failed: %s: %s",
                        type(e).__name__, e,
                    )
                    break
            except WebSocketDisconnect:
                break
    finally:
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
        host=settings.host,
        port=settings.port,
        log_level="info",
        reload=False,
    )
