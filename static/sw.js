// MeshChat Service Worker
// Caches UI shell for offline use. The server inlines APP_VERSION into
// __MESHCHAT_VERSION__ at request time (see /sw.js route in
// meshchat_server.py) so the SW bytes change on every deploy, which is
// what triggers the browser to install an update.

const CACHE_VERSION = '__MESHCHAT_VERSION__';
const CACHE_NAME = 'meshchat-' + CACHE_VERSION;
const SHELL_ASSETS = [
  '/',
  '/index.html',
  '/manifest.json',
  '/qrcode.js',
];

self.addEventListener('install', (e) => {
  // Pre-cache the shell. Do NOT self.skipWaiting() here; the client will
  // ask via postMessage({type:'SKIP_WAITING'}) when the user accepts the
  // "new version available" prompt, so an in-flight compose isn't lost.
  e.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(SHELL_ASSETS))
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then(keys =>
        Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener('message', (e) => {
  if (e.data && e.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});

// ── Offline outbound queue (b3q.46) ─────────────────────────────────────────
// When the client can't reach the server, it stashes the send into the
// IndexedDB outbox and registers a Background Sync tagged 'send-queue'.
// When connectivity returns the browser fires a sync event here; we walk
// the outbox, POST each entry, and notify any open client pages so their
// optimistic bubbles resolve.

const OUTBOX_DB = 'meshchat-outbox';
const OUTBOX_STORE = 'sends';

function openOutbox() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(OUTBOX_DB, 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(OUTBOX_STORE)) {
        db.createObjectStore(OUTBOX_STORE, { keyPath: 'tmpId' });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function outboxAll(db) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(OUTBOX_STORE, 'readonly');
    const req = tx.objectStore(OUTBOX_STORE).getAll();
    req.onsuccess = () => resolve(req.result || []);
    req.onerror = () => reject(req.error);
  });
}

function outboxDelete(db, tmpId) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(OUTBOX_STORE, 'readwrite');
    tx.objectStore(OUTBOX_STORE).delete(tmpId);
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error);
  });
}

async function notifyClients(payload) {
  const clients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  for (const c of clients) c.postMessage(payload);
}

async function flushOutbox() {
  const db = await openOutbox();
  try {
    const items = await outboxAll(db);
    for (const item of items) {
      const res = await fetch('/api/messages', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ to: item.to, body: item.body, method: item.method }),
      });
      if (res.ok || res.status === 202) {
        const d = await res.json().catch(() => ({}));
        await outboxDelete(db, item.tmpId);
        await notifyClients({
          type: 'queue-flushed',
          tmpId: item.tmpId,
          realId: d.id || null,
          status: d.status || (res.status === 202 ? 'queued' : 'sent'),
        });
      } else {
        // Non-2xx: leave in the outbox and retry on the next sync.
        throw new Error('send failed: ' + res.status);
      }
    }
  } finally {
    db.close();
  }
}

self.addEventListener('sync', (e) => {
  if (e.tag === 'send-queue') {
    e.waitUntil(flushOutbox());
  }
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  // Network-first for API, WebSocket, and the SW itself (so update detection
  // isn't defeated by its own cache entry).
  if (
    url.pathname.startsWith('/api/') ||
    url.pathname.startsWith('/ws') ||
    url.pathname === '/sw.js'
  ) {
    return;
  }

  // Cache-first for UI shell assets.
  e.respondWith(
    caches.match(e.request).then(cached => {
      if (cached) return cached;
      return fetch(e.request).then(response => {
        if (response && response.status === 200 && response.type === 'basic') {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
        }
        return response;
      }).catch(() => caches.match('/index.html'));
    })
  );
});
