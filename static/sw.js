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
