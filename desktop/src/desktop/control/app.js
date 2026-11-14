const $ = (id) => document.getElementById(id);
const api = {
  get: (p) => fetch("/__desktop/api/" + p).then((r) => r.json()),
  post: (p, body) => fetch("/__desktop/api/" + p, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  }).then((r) => r.json()),
};

const renderCache = {};
function setHTML(id, html) {
  if (renderCache[id] === html) return;
  renderCache[id] = html;
  const el = document.getElementById(id);
  if (el) el.innerHTML = html;
}
function setText(id, text) {
  if (renderCache[id] === text) return;
  renderCache[id] = text;
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

let lastStatus = null;
let currentTab = "overview";
let sources = [];
let currentView = "control";
let catalogLoaded = false;

function switchView(view) {
  if (view === "catalog" && !(lastStatus && lastStatus.boot && lastStatus.boot.phase === "ready")) {
    toast("服务还在启动，请稍候再打开数据目录");
    return;
  }
  currentView = view === "catalog" ? "catalog" : "control";
  document.body.classList.toggle("catalog", currentView === "catalog");
  document.querySelectorAll(".view-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === currentView);
  });
  const frame = document.getElementById("catalog-frame");
  if (currentView === "catalog" && !catalogLoaded) {
    frame.src = "/";
    catalogLoaded = true;
  }
}
window.switchView = switchView;

document.querySelectorAll(".view-btn").forEach((btn) => {
  btn.addEventListener("click", () => switchView(btn.dataset.view));
});

function toast(msg, bad) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.toggle("bad", !!bad);
  el.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), 3200);
}

function cleanPath(text) {
  return String(text == null ? "" : text).trim().replace(/^["']+|["']+$/g, "");
}

function switchTab(name) {
  currentTab = name;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".panel-tab").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "records") loadRecords();
  if (name === "logs") loadLogs();
}

document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => switchTab(tab.dataset.tab)));

async function refreshStatus() {
  try {
    const s = await api.get("status");
    lastStatus = s;
    renderStatus(s);
  } catch (e) {
    setPill("无法连接本地服务", "bad");
  }
}

function setPill(text, kind) {
  const pill = $("status-pill");
  pill.textContent = text;
  pill.className = "pill " + ({ ok: "pill-ok", bad: "pill-bad" }[kind] || "pill-wait");
}

function renderStatus(s) {
  const boot = s.boot || {};
  const ready = boot.phase === "ready";
  const failed = boot.phase === "error";
  setPill(ready ? "运行中" : failed ? "启动失败" : "启动中", ready ? "ok" : failed ? "bad" : "wait");

  const banner = $("boot-banner");
  banner.style.display = ready ? "none" : "flex";
  banner.classList.toggle("banner-error", failed);
  $("boot-title").textContent = failed ? "启动失败" : boot.message || "正在启动服务…";
  $("boot-detail").textContent = failed ? (boot.error || "请查看日志") : "首次启动会初始化数据库，可能需要 1–2 分钟。";

  const services = s.services || {};
  const labels = { database: "PostgreSQL 数据库", api: "API 服务", worker: "入库任务队列", titiler: "影像切片服务" };
  setHTML("service-list", Object.entries(labels).map(([key, label]) => `
    <li><span><span class="dot ${services[key] ? "dot-ok" : "dot-bad"}"></span>${label}</span>
    <span class="muted small">${services[key] ? "正常" : "未就绪"}</span></li>`).join(""));
  setText("revision", s.database && s.database.revision ? "数据库版本：" + s.database.revision : "");

  const lib = s.library || {};
  const paths = s.paths || {};
  const counts = lib.counts || {};
  const sourceCount = (lib.config && lib.config.source_dirs || []).length;
  setHTML("library-kv", `
    <dt>源文件夹</dt><dd>${sourceCount ? sourceCount + " 个" : "尚未配置"}</dd>
    <dt>影像库</dt><dd>${escapeHtml(paths.library || "—")}</dd>
    <dt>已入库</dt><dd>${counts.ingested || 0} 个数据集</dd>
    <dt>重复跳过</dt><dd>${counts.duplicate || 0} 个</dd>
    <dt>失败</dt><dd>${counts.failed || 0} 个</dd>`);

  setText("admin-user", s.admin ? s.admin.username : "—");
  setText("admin-pass", s.admin ? s.admin.password : "—");

  renderScanState(lib);
  renderCounters(counts);
  renderNetwork(s.network);

  if (ready && !refreshStatus._loadedConfig) {
    refreshStatus._loadedConfig = true;
    loadConfig();
  }
  if (ready && !refreshStatus._autoView && lastStatus && lastStatus.boot && lastStatus.boot.phase === "ready") {
    refreshStatus._autoView = true;
  }
}

function renderScanState(lib) {
  const scanning = lib.mode === "scanning" || lib.mode === "cancelling";
  const cancelling = lib.mode === "cancelling";
  const scanned = lib.scanned || 0;
  const processed = lib.processed || 0;
  const pct = scanning
    ? (scanned ? Math.min(95, Math.round(processed * 100 / Math.max(scanned, 1))) : 5)
    : (lib.last_result && lib.last_result.ingested ? 100 : 0);

  ["progress-bar", "lib-progress-bar"].forEach((id) => {
    const el = $(id);
    if (el) el.style.width = pct + "%";
  });

  let text = "空闲";
  let detail = "";
  if (scanning) {
    text = cancelling ? "正在停止扫描…" : `正在扫描${lib.running_reason ? "（" + lib.running_reason + "）" : ""}`;
    detail = `已发现 ${scanned} 个候选 · 已处理 ${processed}`;
    if (lib.walked_dirs) detail += ` · 已遍历 ${lib.walked_dirs} 个目录`;
    if (lib.current_root) detail += `\n根目录：${lib.current_root}`;
    if (lib.current) detail += `\n当前：${lib.current}`;
  } else if (lib.last_result) {
    const r = lib.last_result;
    text = (lib.cancelled ? "上次扫描已停止：" : "上次扫描：" + (lib.last_scan || ""));
    detail = `发现 ${r.found || 0}，入库 ${r.ingested || 0}，重复 ${r.duplicate || 0}，` +
      `跳过 ${r.skipped || 0}，失败 ${r.failed || 0}`;
  }
  setText("scan-state", text);
  setText("scan-current", detail);
  setText("lib-scan-detail", detail || text);
  const cancel = $("btn-cancel-scan");
  if (cancel) {
    cancel.style.display = scanning && !cancelling ? "inline-block" : "none";
    cancel.disabled = cancelling;
  }
}

function renderCounters(counts) {
  setHTML("counters", [
    ["已入库", counts.ingested || 0],
    ["已整理", counts.organized || 0],
    ["重复", counts.duplicate || 0],
    ["跳过", counts.skipped || 0],
    ["失败", counts.failed || 0],
  ].map(([k, v]) => `<span class="counter">${k} <strong>${v}</strong></span>`).join(""));
}

function renderNetwork(network) {
  if (!network) return;
  const url = $("network-url");
  const local = `http://127.0.0.1:${network.port}`;
  if (network.lan_access) {
    url.textContent = "局域网地址：" + network.url;
    url.dataset.url = network.url;
    const fw = network.firewall || {};
    $("network-firewall").textContent = "防火墙：" + (fw.message || "未知") +
      (fw.ok ? "" : "。可点“添加防火墙规则”，或用管理员命令：" + (fw.manual || ""));
  } else {
    url.textContent = "仅本机可访问：" + local;
    url.dataset.url = local;
    $("network-firewall").textContent = "";
  }
  if (!$("lan-access").matches(":focus")) {
    $("lan-access").checked = !!network.lan_access;
  }
  if (!$("public-host").matches(":focus") && !$("public-host").value) {
    $("public-host").placeholder = network.lan_ip
      ? "留空自动使用 " + network.lan_ip
      : "留空自动使用本机局域网 IP";
  }
}

async function applyNetwork() {
  const body = {
    lan_access: $("lan-access").checked,
    public_host: cleanPath($("public-host").value),
  };
  const result = await api.post("network", body);
  if (result.error) {
    toast(result.error, true);
    return;
  }
  toast(body.lan_access ? "已开启局域网访问，正在重启服务…" : "已关闭局域网访问，正在重启服务…");
  $("network-note").textContent = "正在重启…";
  // Services take ~10s to come back; poll until ready.
  let tries = 0;
  const timer = setInterval(async () => {
    tries += 1;
    try {
      const s = await api.get("status");
      if (s.boot && s.boot.phase === "ready" && s.network) {
        renderNetwork(s.network);
        $("network-note").textContent = s.network.lan_access
          ? "已生效：" + s.network.url
          : "已生效：仅本机";
        clearInterval(timer);
        toast(s.network.lan_access ? "局域网访问已开启" : "局域网访问已关闭");
      }
    } catch (e) {
      /* proxy is rebinding */
    }
    if (tries > 40) clearInterval(timer);
  }, 1000);
}

async function addFirewallElevated() {
  const result = await api.post("firewall", { action: "elevate" });
  toast(result.message || "已请求管理员权限", !result.ok);
}

async function addFirewall() {
  const result = await api.post("firewall", { action: "add" });
  toast(result.message || (result.ok ? "已添加防火墙规则" : "添加失败"), !result.ok);
  await refreshStatus();
}

async function loadConfig() {
  const cfg = await api.get("config");
  const lib = cfg.library;
  $("library-dir").value = lib.library_dir || "";
  $("mode").value = lib.mode || "copy";
  $("interval").value = lib.scan_interval_seconds || 120;
  $("max-size").value = lib.max_size_mb || 4096;
  $("auto-ingest").checked = !!lib.auto_ingest;
  $("convert").checked = !!lib.convert_unsupported;
  $("recursive").checked = !!lib.recursive;
  $("skip-hidden").checked = !!lib.skip_hidden;
  $("skip-system").checked = lib.skip_system_dirs !== false;
  $("scan-on-startup").checked = !!lib.scan_on_startup;
  $("extensions").value = (lib.include_extensions || []).join(", ");
  $("lan-access").checked = !!(cfg.network && cfg.network.lan_access);
  $("public-host").value = (cfg.network && cfg.network.public_host) || "";
  renderSources(lib.source_dirs || []);
}

function renderSources(list) {
  sources = (list || []).slice();
  const listEl = $("source-list");
  listEl.innerHTML = sources.length
    ? sources.map((p, i) => `<li><span>${escapeHtml(p)}</span><button class="remove" data-i="${i}">移除</button></li>`).join("")
    : `<li class="empty">还没有添加源文件夹</li>`;
  listEl.querySelectorAll(".remove").forEach((btn) => {
    btn.addEventListener("click", async () => {
      sources.splice(Number(btn.dataset.i), 1);
      renderSources(sources);
      await saveSources();
      toast("已移除并保存");
    });
  });
}

async function saveSources() {
  const result = await api.post("config", { library: { source_dirs: sources } });
  if (result.error) {
    toast(result.error, true);
    return false;
  }
  $("source-note").textContent = "已保存 " + new Date().toLocaleTimeString();
  await refreshStatus();
  return true;
}

async function addSource(raw) {
  const value = cleanPath(raw);
  if (!value) {
    toast("请先输入文件夹路径，或点“浏览…”选择", true);
    return false;
  }
  if (sources.some((s) => s.toLowerCase() === value.toLowerCase())) {
    toast("该文件夹已经在列表里");
    return false;
  }
  sources.push(value);
  renderSources(sources);
  const ok = await saveSources();
  if (ok) toast("已添加并保存：" + value);
  return ok;
}

async function pickFolder(title) {
  try {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.choose_directory) {
      const result = await window.pywebview.api.choose_directory(title || "选择文件夹");
      if (typeof result === "string") return result;
      if (result && typeof result === "object") return result.path || "";
      return "";
    }
  } catch (e) {
    // fall through to the server-side picker
  }
  const result = await api.post("pick-folder", { title: title || "选择文件夹" });
  if (result.error) {
    toast(result.error, true);
    return "";
  }
  return result.path || "";
}

$("btn-add-source").addEventListener("click", async () => {
  await addSource($("new-source").value);
  $("new-source").value = "";
});

$("btn-browse-source").addEventListener("click", async () => {
  const path = await pickFolder("选择要自动扫描的文件夹");
  if (!path) {
    toast("未选择文件夹");
    return;
  }
  await addSource(path);
});

$("btn-browse-scan").addEventListener("click", async () => {
  const path = await pickFolder("选择要立即扫描的文件夹");
  if (!path) {
    toast("未选择文件夹");
    return;
  }
  $("scan-folder-path").value = path;
});

$("btn-browse-library").addEventListener("click", async () => {
  const path = await pickFolder("选择影像库目录");
  if (path) {
    $("library-dir").value = path;
    $("save-note").textContent = "有未保存的更改";
  }
});

function collectConfig() {
  return {
    library: {
      library_dir: cleanPath($("library-dir").value),
      source_dirs: sources,
      mode: $("mode").value,
      scan_interval_seconds: Number($("interval").value) || 120,
      max_size_mb: Number($("max-size").value) || 4096,
      auto_ingest: $("auto-ingest").checked,
      convert_unsupported: $("convert").checked,
      recursive: $("recursive").checked,
      skip_hidden: $("skip-hidden").checked,
      skip_system_dirs: $("skip-system").checked,
      scan_on_startup: $("scan-on-startup").checked,
      include_extensions: $("extensions").value.split(",").map((s) => s.trim().toLowerCase()).filter(Boolean),
    },
  };
}

async function saveConfig(alsoScan) {
  const result = await api.post("config", collectConfig());
  if (result.error) {
    toast(result.error, true);
    return false;
  }
  toast("设置已保存");
  $("save-note").textContent = "已保存 " + new Date().toLocaleTimeString();
  await refreshStatus();
  if (alsoScan) await startScan();
  return true;
}

$("btn-save").addEventListener("click", () => saveConfig(false));
$("btn-save-scan").addEventListener("click", () => saveConfig(true));

async function startScan() {
  const result = await api.post("scan", {});
  if (result.error) toast(result.error, true);
  else toast(result.started ? "已开始扫描" : "扫描正在进行中");
  setTimeout(refreshStatus, 600);
}

async function scanFolderNow() {
  const path = cleanPath($("scan-folder-path").value);
  if (!path) {
    const picked = await pickFolder("选择要立即扫描的文件夹");
    if (!picked) {
      toast("请选择或输入要扫描的文件夹", true);
      return;
    }
    $("scan-folder-path").value = picked;
  }
  const result = await api.post("scan-folder", { path: cleanPath($("scan-folder-path").value) });
  if (result.error) toast(result.error, true);
  else toast(result.ok ? "已开始扫描指定文件夹" : (result.error || "已有扫描进行中"), !result.ok);
  setTimeout(refreshStatus, 600);
}

$("btn-scan").addEventListener("click", async () => {
  // Persist pending edits first: otherwise a freshly added folder is not in
  // the config the scanner reads.
  const ok = await saveConfig(false);
  if (ok) await startScan();
});

$("btn-scan-folder").addEventListener("click", scanFolderNow);

$("btn-cancel-scan").addEventListener("click", async () => {
  const result = await api.post("cancel-scan", {});
  toast(result.ok ? "已请求停止扫描" : "当前没有正在进行的扫描", !result.ok);
  setTimeout(refreshStatus, 500);
});

$("btn-apply-network").addEventListener("click", applyNetwork);
$("btn-firewall").addEventListener("click", addFirewall);
$("btn-firewall-admin").addEventListener("click", addFirewallElevated);
$("btn-copy-lan").addEventListener("click", async () => {
  const url = $("network-url").dataset.url || "";
  if (!url) return;
  try {
    await navigator.clipboard.writeText(url);
    toast("地址已复制：" + url);
  } catch (e) {
    toast("复制失败，请手动选择", true);
  }
});

$("btn-refresh-records").addEventListener("click", loadRecords);
$("btn-refresh-logs").addEventListener("click", loadLogs);

document.querySelectorAll("[data-open]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.open_folder) {
      await window.pywebview.api.open_folder(btn.dataset.open);
    } else {
      await api.post("open-folder", { kind: btn.dataset.open });
    }
  });
});



$("btn-copy-pass").addEventListener("click", async () => {
  if (!lastStatus) return;
  try {
    await navigator.clipboard.writeText(lastStatus.admin.password);
    toast("密码已复制");
  } catch (e) {
    toast("复制失败，请手动选择", true);
  }
});

$("btn-quit").addEventListener("click", async () => {
  if (!confirm("确定退出 GeoLens 本地版？所有本地服务将停止。")) return;
  if (window.pywebview && window.pywebview.api && window.pywebview.api.quit_app) {
    await window.pywebview.api.quit_app();
  } else {
    await api.post("quit", {});
  }
});

async function loadRecords() {
  const data = await api.get("items?limit=300");
  const rows = data.items || [];
  const badge = (s) => ({ ingested: "b-ingested", organized: "b-organized", failed: "b-failed",
    duplicate: "b-duplicate", skipped: "b-skipped", new: "b-new" }[s] || "b-new");
  const label = { ingested: "已入库", organized: "已整理", failed: "失败",
    duplicate: "重复", skipped: "跳过", new: "新发现" };
  $("records-body").innerHTML = rows.length
    ? rows.map((r) => `<tr>
        <td><span class="badge ${badge(r.status)}">${label[r.status] || r.status}</span></td>
        <td>${escapeHtml(basename(r.source_path) || "—")}</td>
        <td class="muted">${escapeHtml(r.library_path || "—")}</td>
        <td class="muted">${escapeHtml((r.dataset_id || "—").slice(0, 12))}</td>
        <td class="muted">${escapeHtml(r.updated_at || "")}</td>
        <td class="muted">${escapeHtml((r.message || "").slice(0, 160))}</td>
      </tr>`).join("")
    : `<tr><td colspan="6" class="muted">暂无记录</td></tr>`;
}

async function loadLogs() {
  const data = await api.get("logs");
  const which = $("log-select").value;
  $("logbox").textContent = (data[which] || []).join("\n") || "（暂无日志）";
  $("logbox").scrollTop = $("logbox").scrollHeight;
}

$("log-select").addEventListener("change", loadLogs);

function basename(p) {
  if (!p) return "";
  return p.replace(/\\/g, "/").split("/").pop();
}

function escapeHtml(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Editing a strategy field marks the form dirty instead of silently losing it.
["library-dir", "mode", "interval", "max-size", "auto-ingest", "convert", "recursive",
 "skip-hidden", "skip-system", "scan-on-startup", "extensions", "lan-access", "public-host"].forEach((id) => {
  const el = $(id);
  if (el) el.addEventListener("input", () => { $("save-note").textContent = "有未保存的更改"; });
});
$("new-source").addEventListener("keydown", (e) => { if (e.key === "Enter") $("btn-add-source").click(); });
$("scan-folder-path").addEventListener("keydown", (e) => { if (e.key === "Enter") $("btn-scan-folder").click(); });

refreshStatus();
setInterval(() => { refreshStatus(); if (currentTab === "logs") loadLogs(); }, 2500);
