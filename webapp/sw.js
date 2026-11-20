// Минимальный service worker: оболочка приложения открывается мгновенно, запросы к /api всегда идут в сеть.
const CACHE = "slopchik-v3";
const SHELL = ["/app/", "/app/manifest.webmanifest", "/app/icon-192.png", "/app/icon-512.png", "/app/apple-touch-icon.png", "/app/desktop-assets/icon-lime.svg", "/app/desktop-assets/icon-lime-invert.svg", "/app/desktop-assets/icon-purple.svg", "/app/desktop-assets/icon-orange.svg", "/app/desktop-assets/icon-pink.svg", "/app/desktop-assets/icon-blue.svg", "/app/desktop-assets/icon-teal.svg", "/app/desktop-assets/icon-rainbow.svg"];
self.addEventListener("install", (e) => { e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin || u.pathname.startsWith("/api/")) return;
  // сначала сеть (чтобы обновления приходили), при отсутствии сети — копия из кэша
  e.respondWith(fetch(e.request).then((r) => { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); return r; })
    .catch(() => caches.match(e.request).then((r) => r || caches.match("/app/"))));
});
