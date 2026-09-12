// Minimal app-shell service worker so the dashboard installs as a mobile
// PWA ("Add to Home Screen") and opens instantly even on a flaky mobile
// connection. It deliberately does NOT cache /api/* - live trading data
// must always be fresh, never served stale from cache.
//
// IMPORTANT: this used to be cache-first for the shell ("/", manifest.json)
// - once installed, a phone would keep serving that ONE cached snapshot of
// index.html forever, because browsers only re-run install/activate when
// this file's own bytes change, and its bytes hadn't changed across several
// deploys that fixed frontend bugs. Every server-side fix shipped correctly
// but the phone kept running the stale cached page - "nothing changed" was
// real, just not where it looked. Network-first fixes this going forward:
// every load fetches the latest deployed shell when online, and only
// drops back to the cached copy if the network request fails (offline use).
const SHELL_CACHE = "t3-shell-v2";
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
    fetch(event.request)
      .then((response) => {
        const copy = response.clone();
        caches.open(SHELL_CACHE).then((cache) => cache.put(event.request, copy));
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});
