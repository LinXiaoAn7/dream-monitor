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
  /* 用 performance.now() 而不是 rAF 回调的时间戳：后者可能早于 t0，
     相减得到负数，插值会算出 -3 这种值 */
  const step = () => {
    const p = Math.min(1, Math.max(0, (performance.now() - t0) / dur));
    const eased = 1 - Math.pow(1 - p, 3);
    el.textContent = p >= 1 ? to : Math.round(from + (to - from) * eased);
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
  $("refresh").disabled = !!S.busy;
  $("refresh").textContent = S.busy ? "抓取中…" : "刷新活动";

  const auth = S.auth || {};
  const notice = $("notice");
  if (auth.stale) {
    notice.hidden = false;
    $("notice-text").textContent =
      `${auth.msg || "登录已失效"}。服务端是单点登录，重新登录会让手机端的到梦空间登录失效。`;
    $("notice-act").disabled = false;
  } else {
    notice.hidden = true;
  }
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
    stream.innerHTML = `<div class="empty">${S.items.length ? "这个筛选下没有活动" : "还没有数据，点头像里的「刷新活动」"}</div>`;
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

/* ---------------- 视图切换 ---------------- */
let view = "board";

function setView(name, push = true) {
  view = name === "score" ? "score" : "board";
  $("v-board").hidden = view !== "board";
  $("v-score").hidden = view !== "score";
  document.querySelectorAll(".rail-item").forEach((b) =>
    b.classList.toggle("is-on", b.dataset.v === view));
  if (push) {
    const h = view === "score" ? "#score" : "#board";
    if (location.hash !== h) history.replaceState(null, "", h);
  }
  if (view === "score") loadScore();
}

$("rail").addEventListener("click", (e) => {
  const b = e.target.closest(".rail-item");
  if (b) setView(b.dataset.v);
});

/* ---------------- 积分 ---------------- */
let scTimer = null;

function fmtTs(sec) {
  if (!sec) return "";
  const d = new Date(sec * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}.${p(d.getMonth() + 1)}.${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function renderScore(p) {
  const who = [p.name, p.college].filter(Boolean).join(" · ");
  $("sc-who").textContent = who;
  const s = p.score;
  if (s) {
    const v = Number(s.total);
    $("sc-score").textContent = Number.isFinite(v) ? v.toFixed(2) : (s.text || "—");
    $("sc-cap").textContent =
      `数据来源：${s.source || p.score_source}` + (s.at ? ` · 读取于 ${fmtTs(s.at)}` : "");
  } else {
    $("sc-score").textContent = "…";
    $("sc-cap").textContent = p.score_scanning ? "正在读取（约 30 秒）…" : "还没有数据，点「重新统计」";
  }
}

async function loadScore() {
  try {
    const r = await fetch("/api/profile");
    if (r.status === 401) { location.replace("/login.html?again=1"); return; }
    const p = await r.json();
    renderScore(p);
    if (p.score_scanning) {
      clearTimeout(scTimer);
      scTimer = setTimeout(loadScore, 3000);
    }
  } catch (e) {
    $("sc-cap").textContent = `读取失败：${e}`;
  }
}

$("sc-logout").addEventListener("click", logout);
$("sc-rescan").addEventListener("click", async () => {
  $("sc-rescan").disabled = true;
  await fetch("/api/score", { method: "POST" });
  $("sc-cap").textContent = "正在读取（约 30 秒）…";
  setTimeout(() => { $("sc-rescan").disabled = false; }, 2000);
  loadScore();
});

async function logout() {
  await fetch("/api/logout", { method: "POST" });
  location.replace("/login.html");
}

/* ---------------- 抓取进度 ---------------- */
function renderProgress(st) {
  const p = st.progress || {};
  const bar = $("pbar"), fill = $("pbar-fill"), txt = $("pbar-txt");
  if (!st.busy || !p.phase) {
    if (!bar.hidden) {
      bar.hidden = true;
      fill.style.transform = "scaleX(0)";
    }
    return;
  }
  bar.hidden = false;
  const total = Number(p.total) || 0;
  const done = Number(p.done) || 0;
  const part = total ? Math.min(1, done / total) : 0;
  const overall = p.phase === "pages" ? part * 0.5 : 0.5 + part * 0.5;
  fill.style.transform = "scaleX(" + overall.toFixed(3) + ")";
  txt.textContent = p.phase === "pages"
    ? "抓取 " + done + " / " + total + " 页"
    : "补详情 " + done + " / " + total + " 条";
  bar.setAttribute("aria-valuenow", String(Math.round(overall * 100)));
}

/* ---------------- 数据 ---------------- */
async function pull() {
  const r = await fetch("/api/state");
  if (r.status === 401) { location.replace("/login.html"); return; }
  S = await r.json();
  if (S.me) {
    const who = S.me.name || S.me.account || "";
    const school = S.me.school || "";
    $("brand-sub").innerHTML =
      `${school ? esc(school) + " · " : ""}活动看板 <b class="brand-by">by 林小安</b>`;
    document.title = `${who || "到梦空间"} · 活动看板`;
    setAvatar(S.me);
  }
  renderBoard();
  renderList();
  renderProgress(S);
}

async function refresh() {
  toggleMenu(false);
  $("refresh").disabled = true;
  $("refresh").textContent = "抓取中…";
  try {
    await fetch("/api/refresh", { method: "POST" });
    for (let i = 0; i < 120; i++) {
      await new Promise((r) => setTimeout(r, 250));
      await pull();
      if (!S.busy && S.last_sync) break;
    }
  } finally {
    $("refresh").disabled = false;
    $("refresh").textContent = "刷新活动";
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
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeDetail();
});

$("refresh").addEventListener("click", refresh);

function doRelogin() {
  const acc = (S.me && S.me.account) || "";
  const ok = window.confirm(
    "登录已失效，需要重新登录。\n\n" +
    "服务端是单点登录：重新登录后，手机端「到梦空间」上这个账号的登录会立刻失效，\n" +
    "需要重新登录才能继续用手机 App。\n\n" +
    "确定去登录页重新输入密码吗？"
  );
  if (!ok) return;
  const q = new URLSearchParams({ reauth: "1" });
  if (acc) q.set("account", acc);
  location.assign(`/login.html?${q.toString()}`);
}

$("notice-act").addEventListener("click", doRelogin);

/* ---------------- 头像菜单 ---------------- */
function toggleMenu(on) {
  const m = $("avatar-menu");
  const open = on ?? m.hidden;
  m.hidden = !open;
  $("avatar-btn").setAttribute("aria-expanded", String(open));
}

$("avatar-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  toggleMenu();
});
$("menu-logout").addEventListener("click", logout);
document.addEventListener("click", (e) => {
  if (!$("avatar-menu").hidden && !e.target.closest(".avatar-wrap")) toggleMenu(false);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("avatar-menu").hidden) toggleMenu(false);
});

function setAvatar(me) {
  const img = $("avatar-img");
  const fb = $("avatar-fb");
  const url = me && me.avatar;
  fb.textContent = (me && me.name ? me.name[0] : "—");
  if (!url) { img.hidden = true; fb.hidden = false; return; }
  img.onload = () => { img.hidden = false; fb.hidden = true; };
  img.onerror = () => { img.hidden = true; fb.hidden = false; };
  img.src = url;
}

pull();
setView(location.hash === "#score" ? "score" : "board", false);

// 支持 #aid=xxx 直接打开详情
window.addEventListener("hashchange", () => {
  if (location.hash === "#score" || location.hash === "#board") {
    setView(location.hash.slice(1), false);
    return;
  }
  const m = location.hash.match(/^#aid=(.+)$/);
  if (m) openDetail(m[1]);
});
if (/^#aid=/.test(location.hash)) openDetail(location.hash.slice(5));

/* 平时 15 秒同步一次；后端还在补详情（enriching）时改成 0.9 秒，
   这样名额/地点是一格一格填上来的，不用干等。 */
let pollTimer = null;
function schedulePoll() {
  const hasData = !!(S.items && S.items.length);
  const wait = S.busy ? 400 : (S.enriching ? 900 : (hasData ? 15000 : 800));
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    await pull();
    schedulePoll();
  }, wait);
}
schedulePoll();
