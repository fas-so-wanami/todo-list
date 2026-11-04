// ToDo を「アプリとしてインストール」できるようにするための service worker。
// 画面やデータは保存しない(いつもサーバーの最新を表示する)。
// サーバーにつながらないときだけ、画面の代わりに案内を出す。
const OFFLINE_PAGE = `<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>ToDo</title>
<style>body{font-family:"BIZ UDPGothic","Yu Gothic UI",sans-serif;background:#f4f6f9;color:#1e2433;margin:0;display:grid;place-items:center;min-height:100vh}
main{max-width:520px;padding:24px;background:#fff;border:1px solid #e3e7ee;border-radius:6px;line-height:1.7}h1{font-size:18px;margin:0 0 8px}
@media (prefers-color-scheme:dark){body{background:#13161d;color:#e6e9f0}main{background:#1b1f28;border-color:#2c323f}}</style></head>
<body><main><h1>ToDo に接続できません</h1>
<p>ToDo を動かしている黒いウィンドウ(start.bat)が閉じている可能性があります。</p>
<p>start.bat をダブルクリックして起動してから、この画面を再読み込み(F5)してください。</p></main></body></html>`;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (event) => {
  if (event.request.mode !== "navigate") return;  // 画面の移動だけ見張る。それ以外はブラウザに任せる
  event.respondWith(
    fetch(event.request).catch(() => new Response(OFFLINE_PAGE, { headers: { "Content-Type": "text/html; charset=utf-8" } }))
  );
});
