// Minimal app-shell service worker so the dashboard installs as a mobile
// PWA ("Add to Home Screen") and opens instantly even on a flaky mobile
// connection. It deliberately does NOT cache /api/* - live trading data
// must always be fresh, never served stale from cache.
const SHELL_CACHE = "t3-shell-v1";
const SHELL_FILES = ["/", "/static/manifest.json"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_FILES)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== SHELL_CACHE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith("/api/")) {
    return; // never cache live data
  }
  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});
