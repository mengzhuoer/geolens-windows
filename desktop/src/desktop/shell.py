"""Floating app chrome injected into the bundled SPA.

The desktop bundle is meant to feel like one program: double-click
``GeoLens.exe``, the catalog opens, and this bar is the only shell UI left —
it links to the control panel and stops the whole stack. Only the desktop
proxy serves it, so a separately hosted frontend never sees it.
"""
from __future__ import annotations

SHELL_PATH = "/__shell.js"
SHELL_TAG = b'<script src="/__shell.js" defer></script>'

BOOT_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GeoLens</title>
<style>
  :root{color-scheme:dark}
  html,body{height:100%;margin:0}
  body{display:flex;align-items:center;justify-content:center;background:#0e1116;
    color:#e6e9ef;font:14px/1.5 system-ui,'Segoe UI','Microsoft YaHei',sans-serif}
  .wrap{width:min(420px,82vw);text-align:center}
  .logo{font-size:30px;font-weight:600;letter-spacing:.02em}
  .logo b{color:#7dd3fc}
  .sub{margin-top:4px;font-size:12px;color:#8b95a5}
  .msg{margin-top:28px;font-size:14px}
  .bar{margin-top:10px;height:3px;border-radius:2px;background:#1f2733;overflow:hidden}
  .bar span{display:block;height:100%;width:8%;background:#3b82f6;transition:width .45s ease}
  .hint{margin-top:12px;font-size:12px;color:#8b95a5;min-height:18px;word-break:break-all}
  .err{color:#fca5a5}
  a{color:#7dd3fc}
</style></head>
<body><div class="wrap">
  <div class="logo"><b>Geo</b>Lens</div>
  <div class="sub">本地遥感影像库</div>
  <div class="msg" id="msg">正在启动…</div>
  <div class="bar"><span id="fill"></span></div>
  <div class="hint" id="hint">首次启动约 20–35 秒，之后通常 8–15 秒</div>
</div>
<script>
(function () {
  var P = { starting: 8, database: 30, migrate: 45, services: 72, ready: 100, error: 100 };
  var fill = document.getElementById('fill');
  var msg = document.getElementById('msg');
  var hint = document.getElementById('hint');
  var landed = false;
  async function poll() {
    try {
      var res = await fetch('/__desktop/api/status', { cache: 'no-store' });
      var s = await res.json();
      var phase = (s.boot && s.boot.phase) || 'starting';
      fill.style.width = (P[phase] || 40) + '%';
      if (phase === 'ready') {
        msg.textContent = '正在打开数据目录…';
        if (!landed) { landed = true; setTimeout(function () { location.replace('/'); }, 200); }
        return;
      }
      if (phase === 'error') {
        msg.textContent = '启动失败';
        hint.className = 'hint err';
        hint.textContent = (s.boot && s.boot.error) || '请查看 data\\\\logs\\\\desktop.log';
        hint.insertAdjacentHTML('afterend', '<div class="hint"><a href="/__desktop/">打开控制台</a></div>');
        return;
      }
      msg.textContent = (s.boot && s.boot.message) || '正在启动…';
      if (s.admin && s.admin.username) {
        hint.textContent = '无需登录，启动完成后直接进入数据目录';
      }
    } catch (e) {
      msg.textContent = '等待后台服务…';
    }
    setTimeout(poll, 700);
  }
  poll();
})();
</script>
</body></html>
"""

SHELL_JS = r"""
(function () {
  if (window.__geolensShell) return;
  window.__geolensShell = true;

  var loopback = ["127.0.0.1", "localhost", "::1", "[::1]"].indexOf(location.hostname) >= 0;

  var host = document.createElement("div");
  host.id = "__geolens-shell";
  host.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483000";
  var root = host.attachShadow({ mode: "open" });
  root.innerHTML = [
    "<style>",
    ".bar{display:flex;align-items:center;gap:2px;padding:4px;border-radius:6px;",
    "background:rgba(21,25,34,.94);border:1px solid rgba(255,255,255,.12);",
    "box-shadow:0 4px 16px rgba(0,0,0,.35);font:12px/1.2 system-ui,'Segoe UI',sans-serif;",
    "color:#e6e9ef;user-select:none}",
    ".name{padding:0 8px 0 6px;font-weight:600;letter-spacing:.02em;opacity:.85}",
    ".name b{color:#7dd3fc;font-weight:700}",
    "button{appearance:none;border:0;background:transparent;color:inherit;",
    "font:inherit;padding:5px 9px;border-radius:4px;cursor:pointer}",
    "button:hover{background:rgba(255,255,255,.1)}",
    "button:disabled{opacity:.5;cursor:default}",
    "#quit:hover{background:rgba(239,68,68,.2);color:#fecaca}",
    "#quit.armed{background:rgba(239,68,68,.28);color:#fecaca}",
    ".done{display:flex;align-items:center;gap:8px;padding:8px 12px}",
    ".dot{width:7px;height:7px;border-radius:50%;background:#22c55e}",
    "@media print{#__geolens-shell{display:none}}",
    "</style>",
    '<div class="bar"><span class="name"><b>Geo</b>Lens</span>',
    loopback ? '<button id="console" title="服务状态与设置">控制台</button>' : "",
    '<button id="quit" title="停止全部本地服务">退出</button></div>',
  ].join("");

  function done() {
    root.innerHTML =
      '<style>.done{display:flex;align-items:center;gap:8px;padding:8px 12px;' +
      'border-radius:6px;background:rgba(21,25,34,.94);border:1px solid rgba(255,255,255,.12);' +
      "color:#e6e9ef;font:12px/1.3 system-ui,'Segoe UI',sans-serif}" +
      ".dot{width:7px;height:7px;border-radius:50%;background:#64748b}</style>" +
      '<div class="done"><span class="dot"></span>GeoLens 已退出，可以关闭此窗口</div>';
  }

  var consoleBtn = root.getElementById("console");
  if (consoleBtn) {
    consoleBtn.addEventListener("click", function () {
      location.href = "/__desktop/";
    });
  }

  var quitBtn = root.getElementById("quit");
  var armTimer = null;
  var armed = false;
  function disarm() {
    armed = false;
    clearTimeout(armTimer);
    quitBtn.classList.remove("armed");
    quitBtn.textContent = "退出";
  }
  async function quitNow() {
    disarm();
    quitBtn.disabled = true;
    quitBtn.textContent = "正在退出…";
    try {
      await fetch("/__desktop/api/quit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
        keepalive: true,
      });
    } catch (e) {
      /* the service may drop the connection while stopping */
    }
    done();
  }
  // Two-step in-bar confirmation: a native confirm() blocks the app window's
  // renderer and is awkward inside an Edge --app shell.
  quitBtn.addEventListener("click", function () {
    if (armed) { void quitNow(); return; }
    armed = true;
    quitBtn.classList.add("armed");
    quitBtn.textContent = "确认退出";
    armTimer = setTimeout(disarm, 4000);
  });

  function mount() {
    if (!document.body.contains(host)) document.body.appendChild(host);
  }
  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);
})();
"""
