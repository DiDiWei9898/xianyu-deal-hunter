/* 闲鱼低价捕捉机 · 控制台逻辑 */

const state = {
  tasks: [],
  hits: [],
  alerts: [],
  settings: {},
  sources: [],
  stats: {},
  grades: {},
  engine: { running: true },
  filter: { grade: "", taskId: null },
  editing: null,
  drawerMode: "",
};

const GRADES = { S: "必抢", A: "值得看", B: "一般", C: "观望" };

/* ---------------------------------------------------------------- 工具 */
const $ = (sel, root = document) => root.querySelector(sel);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function money(value) {
  const num = Number(value || 0);
  if (!isFinite(num)) return "0";
  return Math.abs(num - Math.round(num)) < 0.05
    ? String(Math.round(num))
    : num.toFixed(1);
}

function pct(value) {
  return Math.round(Number(value || 0) * 100);
}

function rel(iso) {
  if (!iso) return "";
  const stamp = new Date(iso).getTime();
  if (!stamp) return "";
  const diff = Math.floor((Date.now() - stamp) / 1000);
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return `${Math.floor(diff / 86400)} 天前`;
}

function clock(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

async function api(path, method = "GET", body) {
  const options = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) options.body = JSON.stringify(body);
  const res = await fetch(path, options);
  let data = {};
  try {
    data = await res.json();
  } catch (err) {
    throw new Error("服务端返回异常，请检查控制台日志");
  }
  if (!data.ok) throw new Error(data.error || "请求失败");
  return data;
}

function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), 3400);
}

/* ---------------------------------------------------------------- 渲染 */
function renderMetrics() {
  const s = state.stats || {};
  const cards = [
    { label: "启用任务", value: s.active_tasks ?? 0, hint: `共 ${s.total_tasks ?? 0} 个任务`, accent: true },
    { label: "今日命中", value: s.today_hits ?? 0, hint: `累计商品 ${s.total_items ?? 0} 条` },
    { label: "成功推送", value: s.today_alerts ?? 0, hint: "今日送达的提醒" },
    { label: "平均折扣", value: pct(s.avg_discount), unit: "%", hint: `最佳 ${pct(s.best_discount)}%` },
  ];
  $("#metrics").innerHTML = cards.map((c) => `
    <div class="metric ${c.accent ? "accent" : ""}">
      <div class="label">${esc(c.label)}</div>
      <div class="value">${esc(c.value)}${c.unit ? `<em>${c.unit}</em>` : ""}</div>
      <div class="hint">${esc(c.hint)}</div>
    </div>`).join("");
}

function renderTasks() {
  const list = $("#task-list");
  if (!state.tasks.length) {
    list.innerHTML = `<div class="empty"><strong>还没有监控任务</strong><p>点右上角「新建任务」开始，关键词 + 价格上限即可。</p></div>`;
    $("#rail-note").textContent = "";
    return;
  }
  list.innerHTML = state.tasks.map((task) => {
    const keywords = (task.keywords || []).map((k) => `<span class="kw">${esc(k)}</span>`).join("");
    const errored = task.last_error
      ? `<div class="tk-err" title="${esc(task.last_error)}">${esc(task.last_error)}</div>` : "";
    return `
      <div class="task-card ${task.enabled ? "" : "off"}">
        <div class="tk-top">
          <span class="dot"></span>
          <span class="tk-name" title="${esc(task.name)}">${esc(task.name)}</span>
        </div>
        <div class="tk-kw">${keywords || '<span class="kw">未设关键词</span>'}</div>
        <div class="tk-meta">¥${money(task.min_price)}–${money(task.max_price)} · ≥${pct(task.min_discount)}% · ${task.interval_sec}s · 命中 ${task.hit_count || 0}</div>
        ${errored}
        <div class="tk-actions">
          <button class="btn ghost small" data-act="run" data-id="${task.id}" type="button">立即运行</button>
          <button class="btn ghost small" data-act="view" data-id="${task.id}" type="button">${state.filter.taskId === task.id ? "取消筛选" : "只看这个"}</button>
          <button class="btn ghost small" data-act="edit" data-id="${task.id}" type="button">编辑</button>
        </div>
      </div>`;
  }).join("");
  const active = state.tasks.filter((t) => t.enabled).length;
  $("#rail-note").textContent = `共 ${state.tasks.length} 个任务，${active} 个启用中。轮询由本地引擎驱动，关掉页面也会继续跑。`;
}

function visibleHits() {
  return state.hits.filter((h) => {
    if (h.status === "ignored") return false;
    if (state.filter.grade && h.grade !== state.filter.grade) return false;
    if (state.filter.taskId && h.task_id !== state.filter.taskId) return false;
    return true;
  });
}

function renderFilters() {
  const box = $("#grade-filter");
  const live = state.hits.filter((h) => h.status !== "ignored");
  const items = [{ key: "", label: "全部", count: live.length }].concat(
    ["S", "A", "B", "C"].map((g) => ({
      key: g,
      label: `${g} 级`,
      count: live.filter((h) => h.grade === g).length,
    }))
  );
  let html = items.map((it) => `
    <button class="chip ${state.filter.grade === it.key ? "on" : ""}" data-grade="${it.key}" type="button">
      ${esc(it.label)}<b>${it.count}</b>
    </button>`).join("");
  if (state.filter.taskId) {
    const task = state.tasks.find((t) => t.id === state.filter.taskId);
    if (task) html += `<button class="chip on" data-clear-task="1" type="button">${esc(task.name)} ×</button>`;
  }
  box.innerHTML = html;
}

function renderFeed() {
  const rows = visibleHits();
  $("#feed-count").textContent = String(rows.length);
  const feed = $("#feed");
  if (!rows.length) {
    const hasAny = state.hits.length > 0;
    feed.innerHTML = `
      <div class="empty">
        <strong>${hasAny ? "当前筛选下没有命中" : "还没有命中"}</strong>
        <p>${hasAny
          ? "换个等级筛选，或到「监控任务」里放宽折扣要求。"
          : "演示任务每 20–30 秒抓一次，稍等片刻；也可以把手机上的商品标题和价格粘贴到右侧「粘贴导入」立刻验证评分。"}</p>
      </div>`;
    return;
  }
  feed.innerHTML = rows.map((h) => {
    const gradeLabel = h.grade_label || GRADES[h.grade] || "";
    const reasons = (h.reasons || []).map((r) => `<span class="reason">${esc(r)}</span>`).join("");
    const meta = [
      h.seller ? `<span>${esc(h.seller)}</span>` : "",
      h.region ? `<span>${esc(h.region)}</span>` : "",
      h.want ? `<span>想要 ${esc(h.want)}</span>` : "",
      `<span>评分 ${esc(h.score)}</span>`,
    ].join("");
    const drop = Number(h.discount || 0);
    const changed = h.current_price && Number(h.current_price) !== Number(h.price);
    return `
      <article class="hit-card" data-id="${h.id}">
        <div class="hit-top">
          <span class="grade g-${esc(h.grade)}"><span class="g-letter">${esc(h.grade)}</span>${esc(gradeLabel)}</span>
          ${h.demo ? '<span class="tag-demo">演示数据</span>' : ""}
          <span class="hit-task">${esc(h.task_name || "")}</span>
          <span class="hit-time" title="${esc(h.created_at)}">${esc(rel(h.created_at))}</span>
        </div>
        <p class="hit-title">${esc(h.title || "（无标题）")}</p>
        <div class="hit-price-row">
          <span class="price"><span class="cur">¥</span>${money(h.price)}</span>
          ${h.ref_price ? `<span class="ref">¥${money(h.ref_price)}</span>` : ""}
          ${drop > 0 ? `<span class="discount ${drop < 0.2 ? "weak" : ""}">低 ${pct(drop)}%</span>` : ""}
          ${changed ? `<span class="ref">现挂 ¥${money(h.current_price)}</span>` : ""}
        </div>
        ${reasons ? `<div class="reasons">${reasons}</div>` : ""}
        <div class="hit-foot">
          <div class="hit-meta">${meta}</div>
          <div class="hit-actions">
            <button class="btn ghost small" data-act="open" type="button" ${h.url ? "" : 'disabled title="演示数据没有真实链接"'}>打开原贴</button>
            <button class="btn ghost small" data-act="copy" type="button">复制话术</button>
            <button class="btn ghost small" data-act="ignore" type="button">忽略</button>
          </div>
        </div>
      </article>`;
  }).join("");
}

function renderAlerts() {
  const box = $("#alert-list");
  $("#alert-count").textContent = state.alerts.length ? `${state.alerts.length} 条` : "";
  if (!state.alerts.length) {
    box.innerHTML = `<div class="alert-empty">还没有推送记录。命中且评分达标时会写在这里。</div>`;
    return;
  }
  const CHANNEL = { webhook: "Webhook", bark: "Bark", desktop: "桌面通知", sound: "提示音" };
  box.innerHTML = state.alerts.slice(0, 10).map((a) => `
    <div class="alert-row">
      <span class="${a.ok ? "ok" : "bad"}"></span>
      <div class="txt">
        <div class="t">${esc(a.title || "（无标题）")}</div>
        <div class="m">${esc(CHANNEL[a.channel] || a.channel)} · ${esc(clock(a.created_at))}${a.ok ? "" : " · 失败"}</div>
      </div>
    </div>`).join("");
}

function renderSourceLine() {
  const task = state.tasks.find((t) => t.id === state.filter.taskId) || state.tasks[0];
  const box = $("#source-line");
  if (!task) {
    box.innerHTML = `<div class="row"><span>状态</span><b>暂无任务</b></div>`;
    $("#source-badge").textContent = "";
    return;
  }
  const src = state.sources.find((s) => s.name === task.source) || { label: task.source };
  $("#source-badge").textContent = src.label.split("（")[0];
  const last = task.last_run_at ? rel(task.last_run_at) : "尚未运行";
  box.innerHTML = `
    <div class="row"><span>数据源</span><b>${esc(src.label)}</b></div>
    <div class="row"><span>轮询间隔</span><b>${task.interval_sec} 秒</b></div>
    <div class="row"><span>上次抓取</span><b>${esc(last)}</b></div>
    <div class="row"><span>监控词</span><b>${(task.keywords || []).length} 个</b></div>`;
}

function renderEngine() {
  const running = !!state.engine.running;
  const pill = $("#engine-pill");
  pill.className = `engine-pill ${running ? "" : "idle"}`;
  pill.querySelector("span").textContent = running ? "运行中" : "已暂停";
  $("#btn-toggle-engine").textContent = running ? "暂停" : "恢复";
}

function drawChart() {
  const canvas = $("#chart-discount");
  if (!canvas) return;
  const width = canvas.clientWidth || 280;
  const height = 132;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const buckets = [0, 0, 0, 0, 0];
  state.hits.filter((h) => h.status !== "ignored").forEach((h) => {
    const d = Number(h.discount || 0) * 100;
    const idx = d < 15 ? 0 : d < 25 ? 1 : d < 35 ? 2 : d < 50 ? 3 : 4;
    buckets[idx] += 1;
  });
  const labels = ["<15", "15-25", "25-35", "35-50", "50+"];
  const max = Math.max(1, ...buckets);
  const padX = 2;
  const gap = 9;
  const barArea = height - 34;
  const barW = (width - padX * 2 - gap * 4) / 5;

  ctx.strokeStyle = "rgba(53,43,33,0.9)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(padX, barArea + 4.5);
  ctx.lineTo(width - padX, barArea + 4.5);
  ctx.stroke();

  buckets.forEach((value, i) => {
    const x = padX + i * (barW + gap);
    const h = Math.max(2, (value / max) * (barArea - 12));
    const y = barArea - h;
    ctx.fillStyle = `rgba(255,107,26,${0.26 + i * 0.16})`;
    ctx.fillRect(x, y, barW, h);
    ctx.fillStyle = "#f4ece2";
    ctx.font = "600 12px ui-monospace, Consolas, monospace";
    ctx.textAlign = "center";
    if (value) ctx.fillText(String(value), x + barW / 2, y - 6);
    ctx.fillStyle = "#7d6f60";
    ctx.font = "11px ui-monospace, Consolas, monospace";
    ctx.fillText(`${labels[i]}%`, x + barW / 2, height - 12);
  });

  const best = Math.max(0, ...state.hits.map((h) => Number(h.discount || 0)));
  $("#best-discount").textContent = best ? `最佳 -${pct(best)}%` : "";
}

function renderAll() {
  renderMetrics();
  renderTasks();
  renderFilters();
  renderFeed();
  renderAlerts();
  renderSourceLine();
  renderEngine();
  drawChart();
}

/* ---------------------------------------------------------------- 抽屉 */
function openDrawer(mode, task) {
  state.drawerMode = mode;
  state.editing = task ? task.id : null;
  $("#drawer-title").textContent = mode === "settings" ? "设置" : (task ? "编辑任务" : "新建任务");
  $("#task-form").hidden = mode !== "task";
  $("#settings-form").hidden = mode !== "settings";

  if (mode === "task") fillTaskForm(task || null);
  if (mode === "settings") fillSettings();

  const drawer = $("#drawer");
  const overlay = $("#overlay");
  drawer.hidden = false;
  overlay.hidden = false;
  requestAnimationFrame(() => {
    drawer.classList.add("open");
    overlay.classList.add("show");
  });
}

function closeDrawer() {
  const drawer = $("#drawer");
  const overlay = $("#overlay");
  drawer.classList.remove("open");
  overlay.classList.remove("show");
  setTimeout(() => {
    drawer.hidden = true;
    overlay.hidden = true;
  }, 240);
}

function fillSourceSelect() {
  $("#t-source").innerHTML = state.sources.map(
    (s) => `<option value="${esc(s.name)}">${esc(s.label)}</option>`
  ).join("");
}

function fillTaskForm(task) {
  $("#t-name").value = task ? task.name : "";
  $("#t-keywords").value = (task && task.keywords ? task.keywords : []).join(", ");
  $("#t-exclude").value = (task && task.exclude ? task.exclude : []).join(", ");
  $("#t-min").value = task ? money(task.min_price) : "0";
  $("#t-max").value = task ? money(task.max_price) : "1000";
  $("#t-discount").value = task ? pct(task.min_discount) : "15";
  $("#t-interval").value = task ? task.interval_sec : 30;
  $("#t-ref").value = task && task.ref_price ? money(task.ref_price) : "";
  $("#t-region").value = task ? task.region : "";
  $("#t-source").value = task ? task.source : "mock";
  $("#t-conf").value = task && task.source_conf && Object.keys(task.source_conf).length
    ? JSON.stringify(task.source_conf, null, 2) : "";
  $("#t-enabled").checked = task ? !!task.enabled : true;
  $("#t-delete").hidden = !task;
  $("#task-note").textContent = "";
  toggleConfField();
}

function toggleConfField() {
  $("#t-conf-wrap").hidden = $("#t-source").value !== "http";
}

function collectTaskForm() {
  const keywords = $("#t-keywords").value
    .replace(/，/g, ",").split(",").map((s) => s.trim()).filter(Boolean);
  const exclude = $("#t-exclude").value
    .replace(/，/g, ",").split(",").map((s) => s.trim()).filter(Boolean);
  const payload = {
    name: $("#t-name").value.trim(),
    keywords,
    exclude,
    min_price: Number($("#t-min").value || 0),
    max_price: Number($("#t-max").value || 0),
    min_discount: Math.min(0.98, Math.max(0, Number($("#t-discount").value || 0) / 100)),
    interval_sec: Number($("#t-interval").value || 30),
    ref_price: $("#t-ref").value ? Number($("#t-ref").value) : null,
    region: $("#t-region").value.trim(),
    source: $("#t-source").value,
    enabled: $("#t-enabled").checked,
    source_conf: {},
  };
  if (payload.source === "http") {
    const text = $("#t-conf").value.trim();
    if (!text) throw new Error("选了 HTTP 数据源，请填写接口配置 JSON");
    try {
      payload.source_conf = JSON.parse(text);
    } catch (err) {
      throw new Error("接口配置不是合法 JSON");
    }
    if (!payload.source_conf.url) throw new Error("接口配置缺少 url");
  }
  if (!payload.name) throw new Error("请填写任务名");
  if (!keywords.length) throw new Error("请填写至少一个监控关键词");
  if (payload.max_price <= 0) throw new Error("最高价要大于 0");
  return payload;
}

function fillSettings() {
  const n = state.settings.notify || {};
  const e = state.settings.engine || {};
  $("#s-desktop").checked = n.desktop !== false;
  $("#s-sound").checked = n.sound !== false;
  $("#s-score").value = n.min_score ?? 68;
  $("#s-hook-enable").checked = !!(n.webhook && n.webhook.enabled);
  $("#s-hook-type").value = (n.webhook && n.webhook.type) || "feishu";
  $("#s-hook-url").value = (n.webhook && n.webhook.url) || "";
  $("#s-hook-url").disabled = !$("#s-hook-enable").checked;
  $("#s-bark-enable").checked = !!(n.bark && n.bark.enabled);
  $("#s-bark-url").value = (n.bark && n.bark.url) || "";
  $("#s-concurrency").value = e.max_concurrent ?? 3;
  $("#s-limit").value = e.fetch_limit ?? 30;
  $("#settings-note").textContent = "";
}

async function saveSettings() {
  const payload = {
    notify: {
      desktop: $("#s-desktop").checked,
      sound: $("#s-sound").checked,
      min_score: Number($("#s-score").value || 68),
      webhook: {
        enabled: $("#s-hook-enable").checked,
        type: $("#s-hook-type").value,
        url: $("#s-hook-url").value.trim(),
      },
      bark: { enabled: $("#s-bark-enable").checked, url: $("#s-bark-url").value.trim() },
    },
    engine: {
      max_concurrent: Math.max(1, Math.min(8, Number($("#s-concurrency").value || 3))),
      fetch_limit: Math.max(5, Math.min(100, Number($("#s-limit").value || 30))),
    },
  };
  try {
    const res = await api("/api/settings", "POST", payload);
    state.settings = res.settings;
    toast("设置已保存", "ok");
  } catch (err) {
    toast(err.message, "err");
  }
}

/* ---------------------------------------------------------------- 事件 */
function bindEvents() {
  $("#btn-new-task").addEventListener("click", () => openDrawer("task", null));
  $("#btn-settings").addEventListener("click", () => openDrawer("settings"));
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#overlay").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !$("#drawer").hidden) closeDrawer();
  });

  $("#btn-toggle-engine").addEventListener("click", async () => {
    try {
      const res = await api("/api/engine", "POST", { running: !state.engine.running });
      state.engine.running = res.running;
      renderEngine();
      toast(res.running ? "引擎已恢复轮询" : "引擎已暂停（已开启的任务不再抓取）", "ok");
    } catch (err) {
      toast(err.message, "err");
    }
  });

  $("#btn-export").addEventListener("click", () => {
    window.open("/api/export.csv", "_blank", "noopener");
  });

  $("#grade-filter").addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-grade], [data-clear-task]");
    if (!btn) return;
    if (btn.dataset.clearTask) {
      state.filter.taskId = null;
    } else {
      state.filter.grade = btn.dataset.grade || "";
    }
    renderFilters();
    renderFeed();
    renderSourceLine();
    renderTasks();
  });

  $("#task-list").addEventListener("click", async (ev) => {
    const btn = ev.target.closest("[data-act]");
    if (!btn) return;
    const id = Number(btn.dataset.id);
    const act = btn.dataset.act;
    if (act === "edit") {
      const task = state.tasks.find((t) => t.id === id);
      if (task) openDrawer("task", task);
      return;
    }
    if (act === "view") {
      state.filter.taskId = state.filter.taskId === id ? null : id;
      renderAll();
      return;
    }
    if (act === "run") {
      try {
        const res = await api(`/api/tasks/${id}/run`, "POST", {});
        toast(res.started ? "已触发一次抓取，结果会实时出现在左栏" : "该任务正在运行中");
      } catch (err) {
        toast(err.message, "err");
      }
    }
  });

  $("#feed").addEventListener("click", async (ev) => {
    const btn = ev.target.closest("[data-act]");
    if (!btn) return;
    const card = btn.closest(".hit-card");
    const hit = state.hits.find((h) => h.id === Number(card.dataset.id));
    if (!hit) return;
    const act = btn.dataset.act;
    if (act === "open") {
      if (hit.url) window.open(hit.url, "_blank", "noopener");
      return;
    }
    if (act === "copy") {
      const text = [
        hit.title,
        `¥${money(hit.price)}`,
        hit.url ? `链接 ${hit.url}` : "",
        "你好，这个还在吗？我今天可以秒，能再小刀一点吗？",
      ].filter(Boolean).join("\n");
      try {
        await navigator.clipboard.writeText(text);
        toast("话术已复制，去闲鱼私信卖家", "ok");
      } catch (err) {
        toast("浏览器拒绝了剪贴板权限，请手动复制", "err");
      }
      return;
    }
    if (act === "ignore") {
      try {
        await api(`/api/hits/${hit.id}/status`, "POST", { status: "ignored" });
        hit.status = "ignored";
        renderFilters();
        renderFeed();
        drawChart();
      } catch (err) {
        toast(err.message, "err");
      }
    }
  });

  $("#t-source").addEventListener("change", toggleConfField);

  $("#task-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    let payload;
    try {
      payload = collectTaskForm();
    } catch (err) {
      toast(err.message, "err");
      return;
    }
    try {
      if (state.editing) await api(`/api/tasks/${state.editing}`, "PUT", payload);
      else await api("/api/tasks", "POST", payload);
      toast(state.editing ? "任务已更新" : "任务已创建，开始按间隔轮询", "ok");
      closeDrawer();
      await loadState();
    } catch (err) {
      toast(err.message, "err");
    }
  });

  $("#t-test").addEventListener("click", async () => {
    const note = $("#task-note");
    note.textContent = "正在测试……";
    const payload = {
      source: $("#t-source").value,
      keywords: $("#t-keywords").value.split(",").map((s) => s.trim()).filter(Boolean),
    };
    if (state.editing) payload.task_id = state.editing;
    if (payload.source === "http") {
      try {
        payload.source_conf = JSON.parse($("#t-conf").value || "{}");
      } catch (err) {
        note.textContent = "接口配置不是合法 JSON";
        return;
      }
    }
    try {
      const res = await api("/api/source/test", "POST", payload);
      note.textContent = `${res.reachable ? "连通" : "失败"}：${res.message}`;
    } catch (err) {
      note.textContent = `测试请求失败：${err.message}`;
    }
  });

  $("#t-delete").addEventListener("click", async () => {
    if (!state.editing) return;
    if (!window.confirm("删除这个任务？该任务的历史命中也会一起清掉。")) return;
    try {
      await api(`/api/tasks/${state.editing}`, "DELETE");
      toast("任务已删除", "ok");
      closeDrawer();
      await loadState();
    } catch (err) {
      toast(err.message, "err");
    }
  });

  $("#s-save").addEventListener("click", saveSettings);

  $("#s-hook-test").addEventListener("click", async () => {
    const url = $("#s-hook-url").value.trim();
    if (!url) {
      toast("先填写 Webhook 地址", "err");
      return;
    }
    const note = $("#settings-note");
    note.textContent = "正在发送测试消息……";
    try {
      const res = await api("/api/notify/test", "POST", { type: $("#s-hook-type").value, url });
      note.textContent = res.reachable ? "测试消息已发送，去群里看看" : `发送失败：${res.message}`;
    } catch (err) {
      note.textContent = `发送失败：${err.message}`;
    }
  });

  $("#btn-test-source").addEventListener("click", async () => {
    const note = $("#source-test-note");
    note.textContent = "正在测试……";
    const task = state.tasks.find((t) => t.id === state.filter.taskId) || state.tasks[0];
    try {
      const res = await api("/api/source/test", "POST", task ? { task_id: task.id } : {});
      note.textContent = `${res.reachable ? "连通" : "失败"}：${res.message}`;
    } catch (err) {
      note.textContent = `测试失败：${err.message}`;
    }
  });

  async function submitImport(text, clearBox) {
    if (!text.trim()) {
      toast("先把商品信息粘贴进来", "err");
      return;
    }
    try {
      const res = await api("/api/import", "POST", { text });
      if (clearBox) $("#paste-box").value = "";
      $("#import-note").textContent = res.hits
        ? `解析 ${res.imported} 条，新增命中 ${res.hits} 条`
        : `解析 ${res.imported} 条，但没有达到门槛：需要参考价（在任务里填，或数据源带行情价），多粘贴几条也能形成价格基准`;
      toast(res.hits ? `导入成功，命中 ${res.hits} 条` : `已入库 ${res.imported} 条，暂未命中`,
            res.hits ? "ok" : "");
      await loadState();
    } catch (err) {
      toast(err.message, "err");
    }
  }

  $("#btn-import").addEventListener("click", () => submitImport($("#paste-box").value, true));

  $("#btn-import-file").addEventListener("click", () => $("#import-file").click());

  $("#import-file").addEventListener("change", async (ev) => {
    const file = ev.target.files && ev.target.files[0];
    if (!file) return;
    try {
      const text = await file.text();
      $("#import-note").textContent = `已读取 ${file.name}（${(file.size / 1024).toFixed(1)} KB）`;
      await submitImport(text, false);
    } catch (err) {
      toast(`文件读取失败：${err.message}`, "err");
    }
    ev.target.value = "";
  });

  $("#s-hook-enable").addEventListener("change", (ev) => {
    $("#s-hook-url").disabled = !ev.target.checked;
  });

  window.addEventListener("resize", () => drawChart());
}

/* ---------------------------------------------------------------- 数据 */
async function loadState() {
  const data = await api("/api/state");
  state.tasks = data.tasks || [];
  state.hits = data.hits || [];
  state.settings = data.settings || {};
  state.sources = data.sources || [];
  state.stats = data.stats || {};
  state.grades = data.grades || {};
  state.engine = data.engine || { running: true };
  state.alerts = data.alerts || [];
  fillSourceSelect();
  renderAll();
}

function connect() {
  const es = new EventSource("/api/events");
  es.addEventListener("hit", (ev) => {
    const hit = JSON.parse(ev.data);
    hit.grade_label = hit.grade_label || GRADES[hit.grade] || "";
    hit.demo = hit.demo || String(hit.item_id || "").startsWith("mock");
    state.hits.unshift(hit);
    if (state.hits.length > 120) state.hits.pop();
    renderFilters();
    renderFeed();
    drawChart();
    if (hit.grade === "S") toast(`S 级命中：${hit.title}`, "ok");
  });
  es.addEventListener("stats", (ev) => {
    state.stats = JSON.parse(ev.data);
    renderMetrics();
  });
  es.addEventListener("alert", (ev) => {
    const data = JSON.parse(ev.data);
    toast(`已推送：${data.title || ""}`, "ok");
    api("/api/alerts").then((res) => {
      state.alerts = res.alerts || [];
      renderAlerts();
    }).catch(() => {});
  });
  es.addEventListener("task_status", () => {
    loadState().catch(() => {});
  });
  es.addEventListener("task_error", (ev) => {
    const data = JSON.parse(ev.data);
    toast(`任务出错：${data.error}`, "err");
    loadState().catch(() => {});
  });
  es.addEventListener("notify_error", (ev) => {
    const data = JSON.parse(ev.data);
    toast(`推送失败：${data.error}`, "err");
  });
}

/* ---------------------------------------------------------------- 启动 */
async function init() {
  bindEvents();
  try {
    await loadState();
  } catch (err) {
    toast(`初始化失败：${err.message}`, "err");
  }
  connect();
  setInterval(() => {
    renderFeed();
    renderAlerts();
  }, 60000);
}

document.addEventListener("DOMContentLoaded", init);
