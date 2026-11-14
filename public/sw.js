const CACHE = "fieldpulse-shell-v7";
const SHELL = ["/", "/static/styles.css?v=0.7.0", "/static/app.js?v=0.7.0", "/static/icon.svg", "/static/manifest.webmanifest"];
self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET" || new URL(request.url).pathname.startsWith("/api/")) return;
  event.respondWith(fetch(request).then(response => {
    if (response.ok && new URL(request.url).origin === self.location.origin) {
      const clone = response.clone();
      caches.open(CACHE).then(cache => cache.put(request, clone));
    }
    return response;
  }).catch(() => caches.match(request)));
});
