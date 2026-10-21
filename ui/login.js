/* 登录页：把账号密码交给 POST /api/login */
const $ = (id) => document.getElementById(id);

const form = $("form");
const hint = $("hint");
const submit = $("submit");
let busy = false;

const qs = new URLSearchParams(location.search);
const REAUTH = qs.get("reauth") === "1";
const PRESET = (qs.get("account") || "").trim();

function say(msg, kind) {
  hint.textContent = msg || "";
  hint.hidden = !msg;
  if (msg) hint.dataset.kind = kind === "info" ? "info" : "error";
}

function idle() {
  busy = false;
  submit.disabled = false;
  submit.textContent = "登录";
}

async function doLogin() {
  if (busy) return;
  const account = $("account").value.trim();
  const password = $("password").value;
  if (!account || !password) {
    say("账号和密码都要填");
    return;
  }
  busy = true;
  submit.disabled = true;
  submit.textContent = "登录中…";
  say("正在登录，约需 5 秒…");

  try {
    const r = await fetch("/api/login", {
      method: "POST",
      headers: { "content-type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ account, password, force: REAUTH }),
    });
    let d = {};
    try { d = await r.json(); } catch (_) { /* 非 JSON 响应 */ }
    if (!r.ok || !d.ok) {
      say(d.error || `登录失败（HTTP ${r.status}）`);
      idle();
      return;
    }
    submit.textContent = "进入看板…";
    say("");
    window.location.replace("/");
    setTimeout(() => { window.location.href = "/"; }, 1500);
  } catch (err) {
    say("网络异常：" + (err && err.message ? err.message : err));
    idle();
  }
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  doLogin();
});
submit.addEventListener("click", (e) => {
  // 万一把按钮挪出表单，submit 事件不会触发，这里兜底
  if (e.target.closest("form")) return;
  e.preventDefault();
  doLogin();
});

/* 重新登录场景：账号预填好、光标直接落到密码框，提示说明为什么要重输密码 */
if (REAUTH) {
  $("account").value = PRESET;
  $("password").value = "";
  $("account").readOnly = !!PRESET;
  say(PRESET ? "登录状态已失效，请重新输入密码" : "登录状态已失效，请重新输入账号密码", "info");
  $("password").focus();
}
