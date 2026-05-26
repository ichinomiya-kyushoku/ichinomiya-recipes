// Service Worker — 一宮給食レシピ
// データファイルは毎回ネットワークから取得（常に最新）
// 画像・アプリ本体はキャッシュ優先（高速表示）

const CACHE = 'ichinomiya-v1';

// インストール時にアプリ本体をキャッシュ
const SHELL = [
  './',
  './index.html',
  './style.css',
  './app.js',
  './manifest.json',
  './icon-192.png',
  './icon-180.png',
];

self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const name = new URL(e.request.url).pathname.split('/').pop();

  // menus.js / data.js → ネットワーク優先（更新があれば即反映）
  if (name === 'menus.js' || name === 'data.js') {
    e.respondWith(
      fetch(e.request.clone())
        .then(res => {
          caches.open(CACHE).then(c => c.put(e.request, res.clone()));
          return res;
        })
        .catch(() => caches.match(e.request))
    );
    return;
  }

  // それ以外（画像・CSS等）→ キャッシュ優先、なければネットワーク
  e.respondWith(
    caches.match(e.request).then(cached => {
      if (cached) return cached;
      return fetch(e.request).then(res => {
        if (res.ok) caches.open(CACHE).then(c => c.put(e.request, res.clone()));
        return res;
      });
    })
  );
});
