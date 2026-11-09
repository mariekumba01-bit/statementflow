/* StatementFlow — service worker : cache réseau-d'abord pour les
   pages statiques (jamais pour /v1/*). Objectif : rapidité de
   rechargement et écran hors-ligne honnête — PAS un mode hors-ligne
   de l'application (les conversions nécessitent le serveur). */
const CACHE = 'sf-static-v1';
const OFFLINE_URL = '/offline.html';

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE)
    .then((c) => c.addAll(['/', '/style.css', '/app.js', OFFLINE_URL,
                           '/icon-192.png']))
    .then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.pathname.startsWith('/v1/')) {
    return; // API : toujours réseau, jamais de cache
  }
  event.respondWith(
    fetch(event.request)
      .then((res) => {
        if (res.ok && url.origin === location.origin) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(event.request, copy));
        }
        return res;
      })
      .catch(() => caches.match(event.request)
        .then((cached) => cached || caches.match(OFFLINE_URL))));
});
