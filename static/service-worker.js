const STATIC_CACHE = "softx-static-v26";
const STATIC_ASSETS = [
  "/manifest.webmanifest",
  "/static/style.css",
  "/static/scan.js",
  "/static/print.js",
  "/static/pwa-install.js",
  "/static/icons/favicon-16.png",
  "/static/icons/favicon-32.png",
  "/static/icons/icon-192.png",
  "/static/icons/icon-256.png",
  "/static/icons/icon-512.png",
  "/static/icons/apple-touch-icon.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) => cache.addAll(STATIC_ASSETS)).catch(() => undefined)
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key !== STATIC_CACHE)
          .map((key) => caches.delete(key))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Static assets: cache-first for snappy UI.
  if (url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest") {
    event.respondWith(
      caches.match(req).then((cached) => {
        if (cached) return cached;
        return fetch(req).then((resp) => {
          if (!resp || resp.status !== 200 || resp.type === "opaque") return resp;
          const copy = resp.clone();
          caches.open(STATIC_CACHE).then((cache) => cache.put(req, copy)).catch(() => undefined);
          return resp;
        });
      })
    );
    return;
  }

  // Pages/API: network-first so data stays fresh.
  event.respondWith(
    fetch(req).catch(() => caches.match(req).then((cached) => cached || caches.match("/login")))
  );
});
