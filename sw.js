// Shell is cache-first so the app opens instantly and works on the train.
// The digest is network-first so you never read yesterday's brief by accident.
const SHELL = "dfi-shell-v1";
const DATA  = "dfi-data-v1";
const ASSETS = [
  "./", "./index.html", "./manifest.webmanifest",
  "./icons/icon-192.png", "./icons/icon-512.png", "./icons/apple-touch-icon.png"
];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== SHELL && k !== DATA).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const { request } = e;
  if (request.method !== "GET") return;
  const url = new URL(request.url);

  if (url.pathname.endsWith("/data/today.json")) {
    e.respondWith(
      fetch(request)
        .then(r => { const copy = r.clone(); caches.open(DATA).then(c => c.put(request, copy)); return r; })
        .catch(() => caches.match(request))
    );
    return;
  }

  if (url.origin === location.origin || url.hostname.endsWith("gstatic.com") || url.hostname.endsWith("googleapis.com")) {
    e.respondWith(
      caches.match(request).then(hit => hit || fetch(request).then(r => {
        const copy = r.clone();
        caches.open(SHELL).then(c => c.put(request, copy));
        return r;
      }).catch(() => hit))
    );
  }
});
