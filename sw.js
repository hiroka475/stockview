/**
 * StockView Service Worker
 * オフラインキャッシュとPWA機能を提供する
 */

const CACHE_NAME = "stockview-v2";

// 起動に必要な静的ファイルをキャッシュ
const STATIC_ASSETS = [
  "/index.html",
  "/manifest.json",
  "/icon-192.png",
  "/icon-512.png",
  "/jp-stocks.json",
  "/us-stocks.json",
  "https://unpkg.com/lightweight-charts@4.1.0/dist/lightweight-charts.standalone.production.js",
];

// インストール時: 静的ファイルをキャッシュ
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      console.log("[SW] 静的アセットをキャッシュ中...");
      return cache.addAll(STATIC_ASSETS);
    })
  );
  // 待機せずすぐにアクティベート
  self.skipWaiting();
});

// アクティベート時: 古いキャッシュを削除
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key !== CACHE_NAME)
          .map((key) => caches.delete(key))
      )
    )
  );
  self.clients.claim();
});

// フェッチ時: ネットワーク優先、失敗したらキャッシュにフォールバック
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // APIリクエストはネットワークのみ（キャッシュしない）
  if (url.pathname.startsWith("/api/")) {
    return;
  }

  event.respondWith(
    fetch(event.request)
      .then((response) => {
        // 成功したレスポンスをキャッシュに保存
        if (response.ok) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(event.request, clone);
          });
        }
        return response;
      })
      .catch(() => {
        // ネットワーク失敗時はキャッシュから返す
        return caches.match(event.request).then((cached) => {
          if (cached) return cached;
          // HTMLリクエストならindex.htmlを返す
          if (event.request.headers.get("accept")?.includes("text/html")) {
            return caches.match("/index.html");
          }
        });
      })
  );
});
