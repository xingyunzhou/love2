// language: JavaScript, file: frontend/app.js
// 号池控制台前端：纯原生，无构建步骤，直接由 FastAPI 静态托管
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  token: localStorage.getItem("pool_token") || "",
  overview: null,
  logs: [],
  es: null,
  timer: null,
  probing: new Set(),
};

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ---------------------------------------------------------------- 主题 */
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("pool_theme", theme);
  const btn = $("#themeBtn");
  if (btn) btn.textContent = theme === "light" ? "暗色" : "亮色";
}

function initTheme() {
  const saved = localStorage.getItem("pool_theme");
  const prefersLight = window.matchMedia?.("(prefers-color-scheme: light)").matches;
  applyTheme(saved || (prefersLight ? "light" : "dark"));
  $("#themeBtn").addEventListener("click", () => {
    applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
  });
}

function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("err", isError);
  el.classList.add("show");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), 2600);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    method: options.method || "GET",
    headers: {
      "content-type": "application/json",
      "x-admin-token": state.token,
    },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (res.status === 401) {
    showLogin(true);
    throw new Error("口令无效或已过期");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `请求失败 ${res.status}`);
  return data;
}

/* ---------------------------------------------------------------- 登录 */
function showLogin(show) {
  $("#loginMask").classList.toggle("hidden", !show);
  if (show) setTimeout(() => $("#loginToken").focus(), 60);
}

$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  state.token = $("#loginToken").value.trim();
  try {
    await api("/api/overview");
    localStorage.setItem("pool_token", state.token);
    $("#loginError").textContent = "";
    showLogin(false);
    await boot();
  } catch (err) {
    $("#loginError").textContent = String(err.message || err);
  }
});

/* ---------------------------------------------------------------- 渲染 */
function fmtAgo(ts) {
  if (!ts) return "—";
  const diff = Date.now() / 1000 - ts;
  if (diff < 5) return "刚刚";
  if (diff < 60) return `${Math.floor(diff)}s 前`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m 前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h 前`;
  return `${Math.floor(diff / 86400)}d 前`;
}

function statusPill(ep) {
  if (!ep.enabled) {
    // 自动停用（402 欠费）标深红，人工停用标红底白字
    return ep.auto_disabled
      ? `<span class="pill disabled auto"><i></i>欠费停用</span>`
      : `<span class="pill disabled"><i></i>已停用</span>`;
  }
  if (ep.status === "cooling") {
    const s = ep.cooldown_left;
    const left = s > 3600 ? `${Math.ceil(s / 3600)}h` : s > 60 ? `${Math.ceil(s / 60)}m` : `${s}s`;
    return `<span class="pill cooling"><i></i>冷却 ${left}</span>`;
  }
  return `<span class="pill ok"><i></i>可用</span>`;
}

function renderStats(stats) {
  const cards = [
    { label: "接口总数", value: stats.total, cls: "" },
    { label: "可用", value: stats.healthy, cls: "ok" },
    { label: "冷却中", value: stats.cooling, cls: stats.cooling ? "warn" : "" },
    { label: "总请求", value: stats.requests, cls: "" },
    { label: "成功", value: stats.success, cls: "ok" },
    { label: "失败", value: stats.fail, cls: stats.fail ? "err" : "" },
    { label: "平均延迟", value: `${stats.avg_latency_ms}ms`, cls: "" },
    { label: "粘性会话", value: stats.sticky_sessions ?? 0, cls: "" },
    { label: "日志条数", value: stats.logs ?? 0, cls: "" },
    { label: "库体积", value: `${stats.db_mb ?? 0} MB`, cls: "" },
  ];
  $("#stats").innerHTML = cards
    .map((c) => `<div class="stat ${c.cls}"><b>${esc(c.value)}</b><span>${esc(c.label)}</span></div>`)
    .join("");
}

function renderTable(endpoints) {
  const body = $("#poolBody");
  $("#emptyHint").classList.toggle("hidden", endpoints.length > 0);
  body.innerHTML = endpoints
    .map((ep) => {
      const rate = ep.total_requests ? `${ep.success_rate}%` : "—";
      const barW = ep.total_requests ? Math.max(2, ep.success_rate) : 0;
      return `<tr data-id="${ep.id}">
        <td>${statusPill(ep)}</td>
        <td>
          <div class="name">${esc(ep.name)}</div>
          ${ep.model ? `<div class="sub">model: ${esc(ep.model)}</div>` : ""}
          ${ep.notes ? `<div class="sub">${esc(ep.notes)}</div>` : ""}
        </td>
        <td class="url" title="${esc(ep.base_url)}">${esc(ep.base_url)}</td>
        <td class="mono sub">${
          ep.has_key
            ? esc(ep.api_key_masked || "未设置")
            : ep.uses_default_key
              ? '<span class="tag-default">默认密钥</span>'
              : '未设置'
        }</td>
        <td>${ep.total_requests} / ${ep.total_success}</td>
        <td>${rate}<div class="bar"><div style="width:${barW}%"></div></div></td>
        <td>${ep.last_latency_ms ? ep.last_latency_ms + "ms" : "—"}</td>
        <td class="err" title="${esc(ep.last_error)}">${esc(ep.last_error || "—")}</td>
        <td class="ops">
          <button class="tiny ghost" data-act="probe">探活</button>
          <button class="tiny ghost" data-act="edit">编辑</button>
          <button class="tiny ghost" data-act="toggle">${ep.enabled ? "停用" : "启用"}</button>
          <button class="tiny ghost" data-act="reset">重置</button>
          <button class="tiny ghost danger" data-act="delete">删除</button>
        </td>
      </tr>`;
    })
    .join("");
}

async function refresh() {
  try {
    const data = await api("/api/overview");
    state.overview = data;
    $("#adminBanner")?.remove();
    renderStats(data.stats);
    renderTable(data.endpoints);
    $("#proxyBase").textContent = `${location.origin}/v1`;
  } catch (err) {
    if (!String(err.message).includes("口令")) toast(String(err.message || err), true);
  }
}

/* ---------------------------------------------------------------- 交互 */
$("#poolBody").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const tr = btn.closest("tr");
  const id = Number(tr.dataset.id);
  const ep = state.overview.endpoints.find((x) => x.id === id);
  const act = btn.dataset.act;

  try {
    if (act === "probe") {
      const old = btn.textContent;
      btn.textContent = "探测中…";
      btn.disabled = true;
      const res = await api(`/api/endpoints/${id}/probe`, { method: "POST" });
      btn.textContent = old;
      btn.disabled = false;
      toast(res.ok ? `${res.name} 探活成功 · ${res.latency_ms}ms` : `${res.name} 探活失败 · ${res.status || res.error}`, !res.ok);
      refresh();
    } else if (act === "edit") {
      openEditDialog(ep);
    } else if (act === "toggle") {
      await api(`/api/endpoints/${id}/toggle`, { method: "POST" });
      refresh();
    } else if (act === "reset") {
      await api(`/api/endpoints/${id}/reset?counters=true`, { method: "POST" });
      toast(`${ep.name} 已重置`);
      refresh();
    } else if (act === "delete") {
      if (!confirm(`确认删除接口「${ep.name}」？`)) return;
      await api(`/api/endpoints/${id}`, { method: "DELETE" });
      toast("已删除");
      refresh();
    }
  } catch (err) {
    toast(String(err.message || err), true);
    refresh();
  }
});

$("#addBtn").addEventListener("click", () => openEditDialog(null));
$("#batchBtn").addEventListener("click", () => $("#batchDialog").showModal());
$("#clearLogsBtn").addEventListener("click", async () => {
  await api("/api/logs", { method: "DELETE" });
  state.logs = [];
  renderLogs();
  toast("日志已清空");
});
$("#maintenanceBtn").addEventListener("click", async () => {
  try {
    const r = await api("/api/maintenance", { method: "POST" });
    toast(`清理完成：日志 -${r.removed_logs}，会话 -${r.removed_sessions}${r.vacuumed ? "，已 VACUUM" : ""}`);
    refresh();
    loadSettings();
  } catch (err) {
    toast(String(err.message || err), true);
  }
});
$("#probeAllBtn").addEventListener("click", async () => {
  const btn = $("#probeAllBtn");
  btn.disabled = true;
  btn.textContent = "探活中…";
  try {
    const { results } = await api("/api/probe-all", { method: "POST" });
    const ok = results.filter((r) => r.ok).length;
    toast(`探活完成：${ok}/${results.length} 可用`);
  } catch (err) {
    toast(String(err.message || err), true);
  }
  btn.disabled = false;
  btn.textContent = "全部探活";
  refresh();
});

$("#copyBtn").addEventListener("click", async () => {
  const value = `${location.origin}/v1`;
  try {
    await navigator.clipboard.writeText(value);
    toast(`已复制 ${value}`);
  } catch {
    toast(value);
  }
});

/* ---------------------------------------------------------------- 弹窗 */
function openEditDialog(ep) {
  const form = $("#epForm");
  form.reset();
  $("#epDialogTitle").textContent = ep ? "编辑接口" : "添加接口";
  form.id.value = ep ? ep.id : "";
  if (ep) {
    form.name.value = ep.name || "";
    form.base_url.value = ep.base_url || "";
    form.api_key.value = "";
    form.api_key.placeholder = ep.has_key
      ? `已保存 ${ep.api_key_masked}，留空不修改`
      : "留空 = 用全局默认密钥";
    form.auth_header.value = ep.auth_header || "Authorization";
    form.auth_prefix.value = ep.auth_prefix ?? "Bearer ";
    form.model.value = ep.model || "";
    form.weight.value = ep.weight || 1;
    form.priority.value = ep.priority || 0;
    form.notes.value = ep.notes || "";
    form.enabled.checked = !!ep.enabled;
  }
  $("#epDialog").showModal();
}

$("#epForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const id = form.id.value;
  const payload = {
    name: form.name.value.trim(),
    base_url: form.base_url.value.trim(),
    api_key: form.api_key.value.trim(),
    auth_header: form.auth_header.value.trim() || "Authorization",
    auth_prefix: form.auth_prefix.value,
    model: form.model.value.trim(),
    weight: Number(form.weight.value) || 1,
    priority: Number(form.priority.value) || 0,
    enabled: form.enabled.checked,
    notes: form.notes.value.trim(),
  };
  try {
    if (id) {
      if (!payload.api_key) delete payload.api_key;
      await api(`/api/endpoints/${id}`, { method: "PUT", body: payload });
      toast("已保存");
    } else {
      await api("/api/endpoints", { method: "POST", body: payload });
      toast("已添加");
    }
    $("#epDialog").close();
    refresh();
  } catch (err) {
    toast(String(err.message || err), true);
  }
});

$("#batchForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = e.target.lines.value.trim();
  if (!text) return;
  const items = text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const parts = line.split(",").map((p) => p.trim());
      if (parts.length >= 3) return { name: parts[0], base_url: parts[1], api_key: parts[2] };
      if (parts.length === 2) return { name: "", base_url: parts[0], api_key: parts[1] };
      return { name: "", base_url: parts[0], api_key: "" };
    });
  try {
    const res = await api("/api/endpoints/batch", { method: "POST", body: items });
    toast(`导入 ${res.created} 个，跳过 ${res.skipped} 行`);
    $("#batchDialog").close();
    e.target.lines.value = "";
    refresh();
  } catch (err) {
    toast(String(err.message || err), true);
  }
});

/* ---------------------------------------------------------------- 设置 */
async function loadSettings() {
  const s = await api("/api/settings");
  state.settings = s;
  const form = $("#settingsForm");
  const skip = new Set(["has_default_api_key", "default_api_key_masked", "default_api_key", "clear_default_api_key"]);
  for (const key of Object.keys(s)) {
    if (skip.has(key)) continue;
    const el = form.elements[key];
    if (!el) continue;
    if (el.type === "checkbox") el.checked = !!s[key];
    else el.value = s[key];
  }
  form.default_api_key.value = "";
  form.default_api_key.placeholder = s.has_default_api_key
    ? `已保存 ${s.default_api_key_masked}，留空不修改`
    : "未设置：填了即对所有没单独配 key 的接口生效";
  form.clear_default_api_key.checked = false;
}

$("#settingsForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const numeric = [
    "max_attempts", "request_timeout", "cooldown_quota", "cooldown_auth",
    "cooldown_rate", "cooldown_server", "probe_interval", "probe_timeout",
    "sticky_ttl", "log_keep", "log_keep_hours", "affinity_max",
  ];
  const patch = {
    strategy: form.strategy.value,
    probe_path: form.probe_path.value,
    quota_keywords: form.quota_keywords.value,
    default_auth_header: form.default_auth_header.value.trim() || "Authorization",
    default_auth_prefix: form.default_auth_prefix.value,
    sticky_spread: form.sticky_spread ? form.sticky_spread.value : undefined,
    disable_on_quota: form.disable_on_quota.checked,
    auto_revive: form.auto_revive.checked,
  };
  for (const key of numeric) patch[key] = Number(form.elements[key].value) || 0;
  const newDefaultKey = form.default_api_key.value.trim();
  if (newDefaultKey) patch.default_api_key = newDefaultKey;
  if (form.clear_default_api_key.checked) patch.clear_default_api_key = true;
  try {
    await api("/api/settings", { method: "PUT", body: patch });
    toast(newDefaultKey ? "设置与默认密钥已保存" : "设置已保存");
    await loadSettings();
    refresh();
  } catch (err) {
    toast(String(err.message || err), true);
  }
});

/* ---------------------------------------------------------------- 日志 */
function renderLogs() {
  const filter = $("#logFilter").value;
  const list = $("#logList");
  const rows = state.logs.filter((l) => !filter || l.level === filter);
  list.innerHTML = rows
    .map((l) => {
      const t = new Date(l.ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });
      const tag = l.endpoint_name ? `[${esc(l.endpoint_name)}]` : `[${esc(l.kind)}]`;
      return `<li class="${esc(l.level)}"><span class="t">${t}</span><span class="tag">${tag}</span><span class="msg">${esc(l.message)}</span></li>`;
    })
    .join("");
  if ($("#autoScroll").checked) list.scrollTop = list.scrollHeight;
}

$("#logFilter").addEventListener("change", renderLogs);

function connectLogs() {
  if (state.es) state.es.close();
  const url = `/api/logs/stream?token=${encodeURIComponent(state.token)}`;
  const es = new EventSource(url);
  state.es = es;
  es.onmessage = (ev) => {
    try {
      state.logs.push(JSON.parse(ev.data));
    } catch {
      return;
    }
    if (state.logs.length > 400) state.logs.splice(0, state.logs.length - 400);
    if (!$("#tab-logs").classList.contains("hidden")) renderLogs();
  };
  es.onerror = () => {
    es.close();
    state.es = null;
    setTimeout(() => state.token && connectLogs(), 4000);
  };
}

async function seedLogs() {
  try {
    const { logs } = await api("/api/logs?limit=200");
    state.logs = logs;
    renderLogs();
  } catch {
    /* ignore */
  }
}

/* ---------------------------------------------------------------- 骨架 */
$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    $$("main > section").forEach((s) => s.classList.add("hidden"));
    $(`#tab-${tab.dataset.tab}`).classList.remove("hidden");
    if (tab.dataset.tab === "logs") renderLogs();
    if (tab.dataset.tab === "settings") loadSettings().catch(() => {});
  });
});

setInterval(() => {
  $("#clock").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
}, 1000);

async function boot() {
  await refresh();
  if (!state.overview?.admin_token_required) {
    const banner = document.createElement("div");
    banner.id = "adminBanner";
    banner.className = "toast show err";
    banner.textContent = "未设置 ADMIN_TOKEN，管理面板当前无口令保护";
    document.body.appendChild(banner);
    setTimeout(() => banner.classList.remove("show"), 6000);
  }
  await seedLogs();
  connectLogs();
  clearInterval(state.timer);
  state.timer = setInterval(refresh, 5000);
}

(async function init() {
  initTheme();
  $("#proxyBase").textContent = `${location.origin}/v1`;
  try {
    await api("/api/overview");
    showLogin(false);
    await boot();
  } catch {
    showLogin(true);
  }
})();
