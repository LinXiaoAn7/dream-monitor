/* 到梦空间活动看板 —— 手动刷新，不自动轮询 */
const $ = (id) => document.getElementById(id);
const STATE_LABEL = { open: "可报名", soon: "即将开始", running: "进行中", full: "已满员", ended: "已结束" };

let S = { items: [], stats: {}, busy: false, last_sync: null, token: "" };
let filter = "all";
let detailCache = new Map();

/* ---------------- 数字滚动 ---------------- */
const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const shown = new WeakMap();
function rollNumber(el, to) {
  const from = shown.get(el) ?? 0;
  if (from === to) return;
  shown.set(el, to);
  if (REDUCED) { el.textContent = to; return; }
  const t0 = performance.now();
  const dur = 420;
  const step = (now) => {
    const p = Math.min(1, (now - t0) / dur);
    const eased = 1 - Math.pow(1 - p, 3);
    el.textContent = Math.round(from + (to - from) * eased);
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function stamp(ts) {
  if (!ts) return "尚未同步";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `同步于 ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function quotaText(it) {
  if (it.quota < 0) return "不限";
  return String(it.quota);
}

function untilText(ts) {
  if (!ts) return "";
  const diff = ts - Date.now() / 1000;
  if (diff <= 0) return "已截止";
  const h = diff / 3600;
  if (h >= 24) return `${Math.floor(h / 24)} 天后截止`;
  if (h >= 1) return `${Math.floor(h)} 小时后截止`;
  return `${Math.max(1, Math.floor(diff / 60))} 分钟后截止`;
}
/* 截止时间已按要求从列表移除，此函数仅保留给详情页备用 */

/* ---------------- 渲染 ---------------- */
function renderBoard() {
  const s = S.stats || {};
  rollNumber($("m-open"), s.open || 0);
  rollNumber($("m-soon"), s.soon || 0);
  rollNumber($("m-run"), s.running || 0);

  const live = Math.max(1, (s.live || 0));
  const w = (n) => `${Math.max(0, (n || 0) / live) * 100}%`;
  const go = $("g-open"), gs = $("g-soon"), gr = $("g-run");
  go.style.width = w(s.open);
  gs.style.width = w(s.soon);
  gr.style.width = w(s.running);
  requestAnimationFrame(() => {
    [go, gs, gr].forEach((el) => (el.style.transform = "scaleX(1)"));
  });

  $("c-all").textContent = s.live || 0;
  $("c-open").textContent = s.open || 0;
  $("c-soon").textContent = s.soon || 0;
  $("stamp").textContent = stamp(S.last_sync);
  $("token-tail").textContent = S.token ? `${S.token.slice(0, 8)}…` : "无 token";
  $("refresh").disabled = !!S.busy;
  $("refresh").textContent = S.busy ? "抓取中…" : "刷新";

  const job = S.token_job;
  $("token-job").textContent = job ? (job.status === "running" ? job.msg : (job.msg || "")) : "";
}

function passFilter(it) {
  if (filter === "all") return true;
  if (filter === "open") return it.state === "open" || it.state === "full";
  if (filter === "soon") return it.state === "soon";
  return true;
}

let listSig = "";

function renderList() {
  const stream = $("stream");
  const items = (S.items || []).filter(passFilter);
  const sig = filter + "|" + items.map((i) => `${i.aid}:${i.state}:${i.quota}:${i.address || ""}`).join(",");
  if (sig === listSig) return;          // 数据没变就不重绘，避免动画重放
  const first = listSig === "";
  listSig = sig;

  if (!items.length) {
    stream.innerHTML = `<div class="empty">${S.items.length ? "这个筛选下没有活动" : "还没有数据，点右上角刷新"}</div>`;
    return;
  }
  stream.innerHTML = items.map((it, i) => {
    const state = it.state === "full" ? "open" : it.state;
    const tagCls = state === "running" ? "tag--run" : state === "soon" ? "tag--soon" : "tag--open";
    const hot = (it.state === "open") && it.quota >= 0 && it.quota <= 10;
    const left = it.state === "ended" ? "—" : quotaText(it);
    const delay = first ? `style="animation-delay:${Math.min(i, 12) * 18}ms"` : "";
    return `
      <article class="row ${first ? "row--in" : ""}" data-aid="${it.aid}" ${delay}>
        <span class="row-idx">${String(i + 1).padStart(2, "0")}</span>
        <div class="row-main">
          <h3 class="row-title">${esc(it.name || "（无标题）")}</h3>
          <div class="row-meta">
            <span class="tag ${tagCls}">${STATE_LABEL[it.state] || it.state}</span>
            ${it.category ? `<span>${esc(it.category)}</span>` : ""}
            ${it.organizer ? `<span>${esc(it.organizer)}</span>` : ""}
          </div>
          <div class="row-place ${it.address ? "" : "is-empty"}">${esc(it.address || "")}</div>
        </div>
        <div class="row-side">
          <b class="left-num ${hot ? "is-hot" : ""}">${left}</b>
          <span class="left-cap">${it.quota < 0 ? "名额" : "剩余名额"}</span>
        </div>
      </article>`;
  }).join("");
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* ---------------- 详情抽屉 ---------------- */
async function openDetail(aid) {
  $("drawer").classList.add("is-on");
  $("drawer").setAttribute("aria-hidden", "false");
  $("scrim").classList.add("is-on");
  $("d-title").textContent = "读取中…";
  $("d-body").innerHTML = "";

  let d = detailCache.get(aid);
  if (!d) {
    try {
      const r = await fetch(`/api/activity?aid=${encodeURIComponent(aid)}`);
      const j = await r.json();
      d = j.detail || j;
      detailCache.set(aid, d);
    } catch (e) {
      $("d-title").textContent = "读取失败";
      $("d-body").innerHTML = `<div class="drawer-note">${esc(String(e))}</div>`;
      return;
    }
  }
  $("d-title").textContent = d.activityName || d.name || "—";

  const fmtDate = (v) => {
    if (v === undefined || v === null || v === "" || v === "—") return "—";
    const s = String(v);
    if (/^\d{10,13}$/.test(s)) {
      const t = Number(s);
      const dt = new Date(s.length === 13 ? t : t * 1000);
      if (!isNaN(dt)) {
        const p = (n) => String(n).padStart(2, "0");
        return `${dt.getFullYear()}.${p(dt.getMonth() + 1)}.${p(dt.getDate())} ${p(dt.getHours())}:${p(dt.getMinutes())}`;
      }
    }
    return s;
  };
  const startTxt = d.startdateDate ? fmtDate(d.startdateDate) : fmtDate(d.startdate);
  const endTxt = /^\d{10,13}$/.test(String(d.enddate || "")) ? fmtDate(d.enddate) : (d.enddate || "—");
  const way = String(d.joinWayDesc || "").replace(/^报名制[:：]\s*/, "");

  const rows = [
    ["状态", d.statusText || STATE_LABEL[d.state] || "—"],
    ["名额", `${d.joinNum ?? "?"} / ${d.joinmaxnum ?? "?"}`],
    ["报名", d.joindate || d.joinrangeText || "—"],
    ["活动", `${startTxt} 至 ${endTxt}`],
    ["地点", d.address || "—"],
    ["主办", d.schoolname || "—"],
    ["类别", d.catalog2name || "—"],
    ["级别", d.levelText || "—"],
  ];
  $("d-body").innerHTML = rows.map(([k, v]) =>
    `<span class="dk">${k}</span><span class="dv">${esc(v || "—")}</span>`).join("")
    + (way ? `<div class="drawer-note"><b>报名制</b>　${esc(way)}</div>` : "");
}

function closeDetail() {
  $("drawer").classList.remove("is-on");
  $("drawer").setAttribute("aria-hidden", "true");
  $("scrim").classList.remove("is-on");
}

/* ---------------- 数据 ---------------- */
async function pull() {
  const r = await fetch("/api/state");
  S = await r.json();
  renderBoard();
  renderList();
}

async function refresh() {
  $("refresh").disabled = true;
  $("refresh").textContent = "抓取中…";
  await fetch("/api/refresh", { method: "POST" });
  for (let i = 0; i < 120; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    await pull();
    if (!S.busy && S.last_sync) break;
  }
}

/* ---------------- 事件 ---------------- */
$("tabs").addEventListener("click", (e) => {
  const btn = e.target.closest(".tab");
  if (!btn) return;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("is-on", t === btn));
  filter = btn.dataset.f;
  renderList();
});

$("stream").addEventListener("click", (e) => {
  const row = e.target.closest(".row");
  if (row) openDetail(row.dataset.aid);
});

$("drawer-close").addEventListener("click", closeDetail);
$("scrim").addEventListener("click", closeDetail);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDetail(); });

$("refresh").addEventListener("click", refresh);

$("token").addEventListener("click", async () => {
  $("token").disabled = true;
  $("token-job").textContent = "正在换 token…";
  await fetch("/api/token", { method: "POST" });
  for (let i = 0; i < 30; i++) {
    await new Promise((r) => setTimeout(r, 800));
    await pull();
    if (S.token_job && S.token_job.status !== "running") break;
  }
  $("token").disabled = false;
});

pull();

// 支持 #aid=xxx 直接打开详情
window.addEventListener("hashchange", () => {
  const m = location.hash.match(/^#aid=(.+)$/);
  if (m) openDetail(m[1]);
});
if (/^#aid=/.test(location.hash)) openDetail(location.hash.slice(5));

setInterval(pull, 15000);   // 只同步界面状态，不触发抓取
