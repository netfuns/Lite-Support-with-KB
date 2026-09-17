import { t, setLang, LANGS } from "./i18n.js";

// ----------------------------------------------------------------- state
const state = {
  user: null,
  me_perms: new Set(),
  meta: null,
  products: [],
  token: null,
  brand: null,
};

/** Company branding shown before (and after) login - settings > company info. */
async function loadBrand() {
  try {
    const b = await fetch("/api/brand").then(r => r.json());
    state.brand = b || {};
  } catch (e) { state.brand = state.brand || {}; }
  return state.brand || {};
}

// ----------------------------------------------------------------- api
/** Headers for a raw fetch: the X-Token header only when we hold a token (after a
 *  refresh the HttpOnly cookie authenticates instead -- sending "null" would break it). */
function authHeaders(extra) {
  const out = Object.assign({}, extra || {});
  if (state.token) out["X-Token"] = state.token;
  return out;
}

async function api(path, opts = {}) {
  opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  if (state.token) opts.headers["X-Token"] = state.token;
  const cp = captchaPass();
  if (cp) opts.headers["X-Captcha-Pass"] = cp;
  const res = await fetch(path, opts);
  const ct = res.headers.get("content-type") || "";
  let data = null;
  if (ct.includes("application/json")) data = await res.json();
  else if (res.status === 204) data = {};
  if (res.status === 401) {
    document.cookie = "rz_token=; Max-Age=0; path=/";
    state.user = null; state.token = null;
    stopIdleWatchdog();
    // the boot probe sends noRedirect: an anonymous visitor asking for the
    // public landing page must land on it, not be bounced to the login form
    if (!opts.noRedirect && window.location.hash !== "#/login") window.location.hash = "#/login";
    throw new Error("unauthorized");
  }
  if (!res.ok) {
    throw new Error((data && data.detail) || ("HTTP " + res.status));
  }
  return data;
}

async function apiForm(path, form) {
  const res = await fetch(path, { method: "POST", headers: state.token ? { "X-Token": state.token } : {}, body: form });
  const ct = res.headers.get("content-type") || "";
  let data = {};
  if (ct.includes("application/json")) data = await res.json();
  if (!res.ok) throw new Error((data && data.detail) || ("HTTP " + res.status));
  return data;
}

// ----------------------------------------------------------------- helpers
function h(node, attrs = {}, ...children) {
  if (!node) return document.createElement("div");
  const el = document.createElement(node);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "class") el.className = v;
    else if (k in el && k !== "type" && k !== "value") { try { el[k] = v; } catch (e) {} }
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else if (k === "selected") el.selected = !!v;
    else el.setAttribute(k, v);
  }
  // flatten nested arrays (map() results are commonly passed as a single child)
  const flat = [];
  (function push(arr) {
    for (const c of arr) {
      if (Array.isArray(c)) push(c);
      else flat.push(c);
    }
  })(children);
  for (const c of flat) {
    if (c == null || c === false) continue;
    el.append(typeof c === "string" || typeof c === "number"
      ? document.createTextNode(String(c)) : c);
  }
  return el;
}

let _toasts;
function toast(msg, ok = true) {
  if (!_toasts) _toasts = h("div", { class: "toast" });
  document.body.append(_toasts);
  const item = h("div", { class: "item " + (ok ? "ok" : "err") }, msg);
  _toasts.append(item);
  setTimeout(() => item.remove(), 4000);
}

// ----------------------------------------------------------------- captcha
// A solved slider buys 30 quiet minutes (server-issued pass bound to the IP).
// Nothing here is required on the intranet: the server decides -- when an
// endpoint answers 403 captcha_required, the caller funnels through
// ensureCaptcha() and retries. That keeps the desk friction-free in the
// office and hostile to scripts on the open internet.
const CAP_KEY = "rz_cpass", CAP_EXP = "rz_cpass_exp";
let _captchaInFlight = null;

function captchaPass() {
  const tok = sessionStorage.getItem(CAP_KEY) || "";
  return tok && Number(sessionStorage.getItem(CAP_EXP) || 0) > Date.now() ? tok : "";
}
function captchaSave(tok, secs) {
  sessionStorage.setItem(CAP_KEY, tok || "");
  sessionStorage.setItem(CAP_EXP, String(Date.now() + (secs || 1800) * 1000));
}

/** Resolve once a pass exists (cached, or earned through the slider). */
function ensureCaptcha() {
  if (captchaPass()) return Promise.resolve();
  if (!_captchaInFlight) {
    _captchaInFlight = sliderCaptcha().then(r => captchaSave(r.pass_token, r.expires_in))
      .finally(() => { _captchaInFlight = null; });
  }
  return _captchaInFlight;
}

/** One slider puzzle: drag the piece into the darkened hole, release. */
function sliderCaptcha() {
  return new Promise((resolve, reject) => {
    let pieceX = 0, startT = 0, points = 0, data = null, done = false, dragging = false;
    const piece = h("img", { class: "cap-piece", draggable: "false" });
    const bgImg = h("img", { class: "cap-bg", draggable: "false" });
    const handle = h("div", { class: "cap-handle" }, "\u00bb");
    const hint = h("div", { class: "muted", style: "text-align:center;margin-top:8px" }, t("captcha_hint"));
    const panel = h("div", { class: "panel", style: "max-width:360px" },
      h("div", { class: "head" }, t("captcha_title")),
      h("div", { class: "body" },
        h("div", { class: "cap-stage" }, bgImg, piece),
        h("div", { class: "cap-track" }, handle),
        hint));
    const overlay = h("div", { class: "modal" }, panel);
    document.body.append(overlay);

    function close() { overlay.remove(); document.removeEventListener("pointermove", onMove); document.removeEventListener("pointerup", onUp); }
    overlay.addEventListener("mousedown", e => { if (e.target === overlay && !done) { done = true; close(); reject(new Error("captcha_cancelled")); } });

    function place() {
      piece.style.left = pieceX + "px";
      handle.style.left = pieceX + "px";
    }
    async function fresh() {
      const r = await fetch("/api/captcha/new", { headers: captchaPass() ? { "X-Captcha-Pass": captchaPass() } : {} });
      const d = await r.json();
      if (!d || d.ok === false) throw new Error(d && d.error || "captcha_unavailable");
      data = d;
      pieceX = 0; points = 0; startT = 0;
      bgImg.src = d.bg; piece.src = d.piece;
      piece.style.top = d.piece_y + "px";
      const stage = bgImg.parentElement;
      stage.style.width = d.width + "px"; stage.style.height = d.height + "px";
      piece.parentElement.classList.add("cap-ready");
      place();
    }
    function onMove(e) {
      if (!dragging) return;
      const track = handle.parentElement.getBoundingClientRect();
      const max = data.width - data.puzzle;
      const x = Math.max(0, Math.min(max, e.clientX - track.left - handle.offsetWidth / 2));
      if (Math.round(x) !== pieceX) points++;
      pieceX = Math.round(x);
      place();
    }
    async function onUp() {
      if (!dragging) return;
      dragging = false;
      const ms = Date.now() - startT;
      try {
        const r = await fetch("/api/captcha/verify", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ captcha_id: data.captcha_id, dx: pieceX, ms, points }) });
        const d = await r.json();
        if (r.ok && d.pass_token) { done = true; close(); resolve(d); return; }
        toast(t((d && d.error) || "captcha_failed"), false);
      } catch (e) { toast(t("captcha_failed"), false); }
      pieceX = 0; points = 0; place();
      fresh().catch(() => {});
    }
    handle.addEventListener("pointerdown", e => {
      if (done) return;
      dragging = true; startT = Date.now(); points = 0;
      e.preventDefault();
    });
    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", onUp);
    fresh().catch(e => { done = true; close(); reject(e); });
  });
}

function statusTag(s) {
  const map = { new: "open", closed: "closed", customer_replied: "customer_replied", support_replied: "support_replied" };
  return h("span", { class: "tag " + (map[s] || "open") }, t("st_" + s));
}
function prioTag(p) {
  return h("span", { class: "tag " + p }, t("pr_" + p));
}
function visTag(v) {
  return h("span", { class: "tag " + v }, t(v));
}

function esc(s) { return (s == null ? "" : String(s)).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

/** "1.4 MB" -- files are listed with their weight so a chip reads as a file. */
function fmtSize(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / (1024 * 1024)).toFixed(1) + " MB";
}

/**
 * Attachments of one message, of one reply, or of one KB article.
 *
 * Images are previewed inline (clicking opens the original) and every other
 * file becomes an explicit download chip: a bare inline filename was far too
 * easy to scroll past, which made uploaded files look as if they were lost.
 * Pass `data.attachments` straight from the API.
 */
function attachmentList(atts) {
  const list = atts || [];
  if (!list.length) return null;
  const box = h("div", { class: "att-box" },
    h("div", { class: "att-title" }, "\uD83D\uDCCE " + t("attachments") + " (" + list.length + ")"));
  for (const a of list) {
    const url = "/files/" + (a.stored_name || "");
    const name = a.filename || "file";
    const size = a.size ? " \u00b7 " + fmtSize(a.size) : "";
    if ((a.content_type || "").startsWith("image/")) {
      box.append(h("a", { class: "att-img", href: url, target: "_blank", title: name },
        h("img", { src: url, alt: name, loading: "lazy" }),
        h("span", { class: "att-img-name" }, name + size)));
    } else {
      box.append(h("a", { class: "att-chip", href: url, target: "_blank", download: name },
        "\uD83D\uDCCE " + name + size));
    }
  }
  return box;
}

/** Inline markdown (bold, code, images, links). The text is escaped first. */
function mdInline(s) {
  return esc(s)
    // the raw dialogue stamps its timestamps with <sub>..</sub> (the PDF export
    // renders that pair); let exactly that one through instead of printing it
    .replace(/&lt;sub&gt;/g, "<sub>").replace(/&lt;\/sub&gt;/g, "</sub>")
    // images must win over links, otherwise the [..](..) inside is swallowed
    .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g,
      '<img src="$2" alt="$1" style="max-height:320px;max-width:100%;display:block;margin:8px 0">')
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}

// tiny markdown renderer (headings, code, lists, paragraphs)
function mdToHtml(md) {
  if (!md) return "";
  const lines = String(md).split(/\r?\n/);
  let out = "", inPre = false, listOpen = false;
  for (let i = 0; i < lines.length; i++) {
    let L = lines[i];
    if (/^\s*```/.test(L)) {
      if (!inPre) { out += "<pre><code>"; inPre = true; }
      else { out += "</code></pre>"; inPre = false; }
      continue;
    }
    if (inPre) { out += esc(L); continue; }
    let m;
    if ((m = L.match(/^(#{1,6})\s+(.*)$/))) { out += "<h" + m[1].length + ">" + m[2] + "</h" + m[1].length + ">"; continue; }
    if (/^\s*[-*]\s+/.test(L)) {
      if (!listOpen) { out += "<ul>"; listOpen = true; }
      out += "<li>" + mdInline(L.replace(/^\s*[-*]\s+/, "")) + "</li>";
      continue;
    } else if (listOpen) { out += "</ul>"; listOpen = false; }
    if (L.trim() === "") { if (!listOpen) out += "<br>"; continue; }
    out += "<p>" + mdInline(L) + "</p>";
  }
  if (inPre) out += "</code></pre>";
  if (listOpen) out += "</ul>";
  return out;
}

// ----------------------------------------------------------------- layout
/** Company logo + name, taken from settings > company info. */
function brandMark(size) {
  const b = state.brand || {};
  const name = b.company_name || "RankEZ";
  const logo = b.company_logo
    ? h("img", { src: b.company_logo, class: "brand-logo", alt: name,
                 style: size ? "height:" + size + "px" : null })
    : null;
  return h("span", { class: "brand-mark" }, logo, h("span", { class: "brand-name" }, name));
}

function layout(title, content) {
  const root = document.getElementById("root");
  root.innerHTML = "";
  document.title = ((state.brand && state.brand.company_name) || "RankEZ") + " \u00b7 " + t("app");
  root.append(h("div", {},
    h("header", { class: "app-bar" },
      h("a", { class: "logo", href: state.user ? "#/dashboard" : "#/home" }, brandMark(26)),
      state.user
        ? h("div", { class: "search-wrap" },
            h("input", { placeholder: t("search") }),
            h("span", { class: "icon-search", onclick: doSearch }, "🔎"))
        : h("div", { class: "search-wrap" }),
      h("div", { class: "actions" },
        h("select", { class: "lang", onchange: e => { setLang(e.target.value); render(); } },
          LANGS.map(l => h("option", { value: l.id, selected: state.lang === l.id ? "selected" : null }, l.label))),
        state.user
          ? h("span", { class: "user", onclick: toggleUserMenu }, "👤 " + (state.user.display_name || state.user.email))
          : h("div", { style: "display:flex;gap:8px;align-items:center" },
              h("a", { class: "btn btn-ghost btn-sm", href: "#/home" }, t("home")),
              h("a", { class: "btn btn-blue btn-sm", href: "#/login" }, t("login"))),
      ),
    ),
    h("div", { class: "layout" },
      state.user ? sidebar() : h("div", {}),
      h("main", {}, content),
    ),
  ));
  bindSearchBar();
}

function bindSearchBar() {
  const inp = document.querySelector(".search-wrap input");
  if (!inp) return;
  inp.oninput = () => {
    clearTimeout(inp._t);
    inp._t = setTimeout(doSearch, 300);
  };
  inp.onkeydown = e => { if (e.key === "Enter") doSearch(); };
}

async function doSearch() {
  const q = (document.querySelector(".search-wrap input") || {}).value || "";
  if (!q || !state.user) return;
  try {
    const r = await api("/api/search?q=" + encodeURIComponent(q));
    layout("", h("div", {},
      h("h1", {}, t("results") + " (" + r.tickets.length + " + " + r.articles.length + ")"),
      r.tickets.length ? h("div", { class: "card" }, h("div", { class: "card-body" },
        h("h3", {}, "Tickets"),
        h("div", {}, r.tickets.map(x =>
          h("div", { class: "flex-between", style: "padding:6px 0" },
            h("a", { href: "#/ticket/" + x.id }, "[" + x.code + "] " + x.title),
            statusTag(x.status)))))) : null,
      r.articles.length ? h("div", { class: "card" }, h("div", { class: "card-body" },
        h("h3", {}, t("articles")),
        h("div", {}, r.articles.map(x =>
          h("div", { class: "flex-between", style: "padding:6px 0" },
            h("a", { href: "#/kb/" + x.id }, x.title),
            visTag(x.visibility)))))) : null,
    ));
  } catch (e) { toast(e.message, false); }
}

let userMenu;
function toggleUserMenu() {
  if (userMenu) { userMenu.remove(); userMenu = null; return; }
  userMenu = h("div", { class: "dropdown" },
    h("div", { class: "dd-head" }, state.user.display_name || state.user.email),
    h("a", { href: "#/me" }, t("my_account")),
    h("button", { onclick: () => { closeUserMenu(); openChangePassword(); } }, t("change_password")),
    hasPerm("settings.mail") ? h("a", { href: "#/admin/site" }, t("site_settings")) : null,
    h("button", { onclick: logout }, t("logout")));
  document.querySelector(".actions").append(userMenu);
  document.addEventListener("click", function close(e) {
    if (userMenu && !userMenu.contains(e.target) && !e.target.classList.contains("user")) {
      userMenu.remove(); userMenu = null;
      document.removeEventListener("click", close);
    }
  });
}
function closeUserMenu() { if (userMenu) { userMenu.remove(); userMenu = null; } }

function openChangePassword() {
  const oldPw = h("input", { type: "password", placeholder: t("current_password") });
  const newPw = h("input", { type: "password", placeholder: t("new_password") });
  const newPw2 = h("input", { type: "password", placeholder: t("confirm_password") });
  const body = h("div", { style: "min-width:320px" },
    h("label", { style: "display:block;margin:8px 0" }, h("span", { class: "muted" }, t("current_password")), oldPw),
    h("label", { style: "display:block;margin:8px 0" }, h("span", { class: "muted" }, t("new_password")), newPw),
    h("label", { style: "display:block;margin:8px 0" }, h("span", { class: "muted" }, t("confirm_password")), newPw2),
    h("div", { class: "flex-between mt-2" },
      h("button", { class: "btn btn-blue", onclick: async () => {
        if (!oldPw.value || !newPw.value) { toast(t("fill_all"), false); return; }
        if (newPw.value !== newPw2.value) { toast(t("password_mismatch"), false); return; }
        try {
          await api("/api/me/password", { method: "POST", body: JSON.stringify({ old: oldPw.value, new: newPw.value }) });
          toast(t("saved")); closeModal();
        } catch (e) { toast(e.message, false); }
      } }, t("save")),
      h("button", { class: "btn btn-ghost", onclick: closeModal }, t("cancel"))));
  showModal(t("change_password"), body);
}

function hasPerm(key) { return state.me_perms.has(key); }

async function logout() {
  try { await api("/api/auth/logout", { method: "POST" }); } catch (e) {}
  document.cookie = "rz_token=; Max-Age=0; path=/";
  state.user = null; state.token = null;
  stopIdleWatchdog();
  window.location.hash = "#/login";
}

// --------------------------------------------------- idle session watchdog
// The server expires a token after `session_timeout` minutes of inactivity and
// after `session_max_lifetime` minutes from login, whichever comes first. The
// browser mirrors the idle rule so the user gets a warning instead of a silent
// failure, and it pings the server while the user is actually working so that a
// long form-filling session is not killed by the server-side idle clock.
const idle = { timer: null, lastActive: 0, lastPing: 0, warnEl: null, counter: null, timeoutMin: 0 };

function markActive() { idle.lastActive = Date.now(); hideIdleWarning(); }
["click", "keydown", "mousemove", "wheel", "touchstart"].forEach(
  ev => window.addEventListener(ev, markActive, { passive: true }));

function hideIdleWarning() {
  if (idle.warnEl) { idle.warnEl.remove(); idle.warnEl = null; idle.counter = null; }
}

function showIdleWarning() {
  if (idle.warnEl) return;
  const counter = h("b", {}, "");
  const el = h("div", { class: "modal", id: "rz-idle-warn" },
    h("div", { class: "panel" },
      h("div", { class: "head" }, t("session_timeout")),
      h("div", { class: "body" }, h("p", {}, counter)),
      h("div", { class: "foot" },
        h("button", { class: "btn btn-blue", onclick: () => {
          markActive();
          api("/api/me/session").catch(() => {});
        } }, t("stay_signed_in")),
        h("button", { class: "btn btn-ghost", onclick: () => signOutExpired() }, t("logout")))));
  document.body.append(el);
  idle.warnEl = el;
  idle.counter = counter;
}

async function checkIdle() {
  if (!state.user || !idle.timeoutMin) return;
  const limitMs = idle.timeoutMin * 60000;
  const leftMs = limitMs - (Date.now() - idle.lastActive);
  if (leftMs <= 0) { signOutExpired(); return; }
  if (leftMs <= 60000) {
    showIdleWarning();
    if (idle.counter) idle.counter.textContent = t("session_idle_warning", { n: Math.ceil(leftMs / 1000) });
  } else {
    hideIdleWarning();
  }
  const beat = Math.max(20000, limitMs / 4);
  if (Date.now() - idle.lastPing > beat) {
    idle.lastPing = Date.now();
    try {
      const r = await api("/api/me/session");
      if (r && typeof r.idle_left === "number" && r.idle_left <= 0) signOutExpired();
    } catch (e) { /* api() already handled 401 */ }
  }
}

function signOutExpired() {
  stopIdleWatchdog();
  document.cookie = "rz_token=; Max-Age=0; path=/";
  state.user = null; state.token = null; state.me_perms = new Set();
  if (window.location.hash !== "#/login") window.location.hash = "#/login";
  setTimeout(() => toast(t("session_expired"), false), 80);
}

function startIdleWatchdog() {
  stopIdleWatchdog();
  const min = Number((state.meta && state.meta.session_timeout) || 0);
  idle.timeoutMin = min;
  if (!min) return;               // 0 = never expire
  idle.lastActive = Date.now();
  idle.lastPing = Date.now();
  idle.timer = setInterval(checkIdle, 5000);
}

function stopIdleWatchdog() {
  if (idle.timer) { clearInterval(idle.timer); idle.timer = null; }
  idle.timeoutMin = 0;
  hideIdleWarning();
}

function navLink(href, label) {
  const here = "#" + ((window.location.hash || "").replace(/^#/, "") || "/home");
  const active = here === href || (href !== "#/home" && here.indexOf(href + "/") === 0);
  return h("a", { href, class: active ? "active" : null }, label);
}

function sidebar() {
  const items = [];
  items.push(navLink("#/dashboard", t("dashboard")));
  items.push(navLink("#/tickets", t("tickets")));
  if (hasPerm("ticket.create")) items.push(navLink("#/new-ticket", "+" + t("new_ticket")));
  items.push(navLink("#/kb", t("kb")));
  if (hasPerm("customer.view")) items.push(navLink("#/customers", t("tab_customers")));
  if (hasPerm("partner.view")) items.push(navLink("#/partners", t("partners")));
  if (hasPerm("user.manage") || hasPerm("role.manage") || hasPerm("group.manage") || hasPerm("settings.mail")) {
    items.push(h("div", { class: "sep-label" }, t("admin")));
    if (hasPerm("user.manage")) items.push(navLink("#/admin/users", t("users")));
    if (hasPerm("role.manage")) items.push(navLink("#/admin/roles", t("roles")));
    if (hasPerm("group.manage")) items.push(navLink("#/admin/groups", t("groups")));
    if (hasPerm("settings.mail")) {
      items.push(h("div", { class: "sep-label" }, t("settings")));
      items.push(navLink("#/admin/site", t("site_settings")));
      items.push(navLink("#/admin/welcome", t("welcome_page")));
      items.push(navLink("#/admin/company", t("company_info")));
      items.push(navLink("#/admin/internal_domains", t("internal_domains")));
      items.push(navLink("#/admin/domains", t("bind_domains")));
      items.push(navLink("#/admin/settings", t("mail_settings")));
    }
  }
  return h("nav", { class: "sidebar" }, items);
}

// ----------------------------------------------------------------- router
function current() {
  const raw = (window.location.hash || "#/").replace(/^#/, "");
  // "/ticket/12" -> ["", "ticket", "12"]; drop the empty leading segment so
  // parts[0] is the route and parts[1..] are its arguments.
  const parts = raw.split("/").filter(Boolean);
  return { route: parts[0] || "dashboard", parts, raw };
}

async function render() {
  const c = current();
  const route = c.route;   // "/ticket/12" -> "ticket"
  const arg = c.parts[1];  // "/ticket/12" -> "12"
  // these views dereference state.user, so an anonymous visitor must not reach them
  if (!state.user && (route === "dashboard" || route === "me" || route === "admin")) {
    if (window.location.hash !== "#/login") window.location.hash = "#/login";
    return;
  }
  let view;
  if (route === "" || route === "home" || route === "welcome") view = homeView();
  else if (route === "dashboard") view = dashboardView();
  else if (route === "tickets") view = ticketsView();
  else if (route === "ticket") view = ticketDetail(arg);
  else if (route === "new-ticket") view = newTicketView();
  else if (route === "kb") {
    if (arg === "new") view = kbEditorPage(null);
    else if (arg === "edit") view = kbEditorPage(c.parts[2]);
    else if (arg === "collections") view = kbCollections();
    else if (arg) view = kbDetail(arg);
    else view = kbView();
  } else if (route === "customers") view = customersView();
  else if (route === "partners") view = partnersView();
  else if (route === "login") view = loginView();
  else if (route === "register") view = registerView();
  else if (route === "me") view = meView();
  else if (route === "admin") {
    const sub = arg || "users";
    const map = { users: adminUsers, roles: adminRoles, groups: adminGroups,
                  settings: adminSettings, site: adminSite, welcome: adminWelcome,
                  company: adminCompany, domains: adminDomains,
                  internal_domains: adminInternalDomains };
    view = (map[sub] || adminUsers)();
  } else view = state.user ? dashboardView() : homeView();
  view = await view;
  layout(view.title, view.body);
}

window.addEventListener("hashchange", () => render());

// ----------------------------------------------------------------- views
/** Public landing page: company logo + name, Sign in button, admin-editable Markdown.
 *  Carries a public KB search: anonymous visitors may search the public
 *  articles, one slider pass per half hour on the open internet. */
async function homeView() {
  if (!state.brand) await loadBrand();
  const b = state.brand || {};
  const name = b.company_name || "RankEZ";
  const searchInp = h("input", { class: "filter-grow", placeholder: t("home_search_placeholder") });
  const results = h("div", { class: "home-search-results" });
  async function runSearch() {
    const q = searchInp.value.trim();
    results.innerHTML = "";
    if (!q) return;
    const req = () => api("/api/kb/articles?vis=public&q=" + encodeURIComponent(q));
    let r;
    try { r = await req(); }
    catch (e) {
      if (e.message !== "captcha_required") { results.append(h("div", { class: "muted" }, e.message)); return; }
      try { await ensureCaptcha(); } catch (e2) { return; } // user closed the puzzle
      r = await req();
    }
    const items = r.items || [];
    if (!items.length) { results.append(h("div", { class: "muted" }, t("no_results"))); return; }
    for (const a of items.slice(0, 8)) {
      results.append(h("a", { class: "home-search-item", href: "#/kb/" + a.id },
        h("span", {}, a.title),
        h("span", { class: "muted" }, (a.module || a.source || ""))));
    }
  }
  searchInp.addEventListener("keydown", e => { if (e.key === "Enter") runSearch(); });
  const body = h("div", { class: "home" },
    h("section", { class: "home-hero" },
      b.company_logo ? h("img", { src: b.company_logo, class: "home-logo", alt: name }) : null,
      h("h1", { class: "home-title" }, name),
      h("p", { class: "muted" }, t("welcome_hero_sub")),
      h("div", { class: "home-search card" }, h("div", { class: "card-body" },
        h("div", { class: "filters" }, searchInp,
          h("button", { class: "btn btn-blue", onclick: runSearch }, t("kb_search")))),
        results),
      h("div", { class: "home-cta" },
        state.user
          ? h("a", { class: "btn btn-blue", href: "#/dashboard" }, t("go_dashboard"))
          : h("a", { class: "btn btn-blue", href: "#/login" }, t("login")))),
    h("section", { class: "card home-doc" },
      h("div", { class: "card-body markdown", innerHTML: mdToHtml(b.welcome_md || "") })));
  return { title: "", body };
}

function loginView() {
  const email = h("input", { name: "email", type: "email", placeholder: t("email"), required: true });
  const pw = h("input", { name: "password", type: "password", placeholder: t("password"), required: true });
  const totp = h("input", { name: "totp", class: "totp hidden", type: "text", placeholder: "TOTP" });
  const body = h("div", { style: "max-width:380px;margin:60px auto" },
    h("div", { style: "text-align:center;margin-bottom:16px" }, brandMark(34)),
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("div", {}, email, pw, totp),
      h("button", { class: "btn btn-blue", style: "width:100%;margin-top:10px", onclick: async (e) => {
        e.preventDefault();
        const btn = e.currentTarget;
        btn.disabled = true;
        try {
          const payload = { email: email.value, password: pw.value };
          if (totp.value) payload.totp = totp.value;
          const post = () => fetch("/api/auth/login", {
            method: "POST",
            headers: Object.assign({ "Content-Type": "application/json" },
              captchaPass() ? { "X-Captcha-Pass": captchaPass() } : {}),
            body: JSON.stringify(payload) });
          let r = await post();
          let data = await r.json().catch(() => ({ error: "HTTP " + r.status }));
          // the open internet owes a slider first; the intranet walks straight in
          if (r.status === 403 && data.detail === "captcha_required") {
            try { await ensureCaptcha(); } catch (e2) { toast(t("captcha_failed"), false); return; }
            r = await post();
            data = await r.json().catch(() => ({ error: "HTTP " + r.status }));
          }
          if (data.need_totp) { totp.classList.remove("hidden"); totp.focus(); toast(t("totp_prompt") || "Enter your 2FA code"); return; }
          if (data.error) { toast(data.error, false); return; }
          state.token = data.token;
          if (data.captcha_pass) captchaSave(data.captcha_pass, 1800);
          const me = await api("/api/me");
          state.user = me; state.me_perms = new Set(me.permissions);
          state.meta = await api("/api/meta");
          state.products = state.meta.products;
          state.modules = state.meta.modules || [];
          state.deployTypes = state.meta.deploy_types || ["ON-PREM", "SaaS"];
          applyTheme(state.meta.theme);
          startIdleWatchdog();
          window.location.hash = "#/dashboard";
        } finally { btn.disabled = false; }
      } }, t("sign_in_btn")),
      h("p", { class: "muted", style: "text-align:center;margin-top:10px" },
        h("a", { href: "#/register" }, t("register")),
        " \u00b7 ",
        h("a", { href: "#/home" }, t("home"))))),
  );
  return { title: "", body };
}

function registerView() {
  const email = h("input", { type: "email", placeholder: t("email"), required: true });
  const name = h("input", { placeholder: t("display_name") });
  const pw = h("input", { type: "password", placeholder: t("password"), required: true });
  const body = h("div", { style: "max-width:360px;margin:60px auto" },
    h("h1", { style: "text-align:center" }, t("signup")),
    h("p", { class: "muted", style: "text-align:center" }, t("signup_sub")),
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("div", {}, email, name, pw),
      h("button", { class: "btn btn-blue", style: "width:100%;margin-top:10px", onclick: async () => {
        const r = await fetch("/api/auth/register", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email: email.value, display_name: name.value, password: pw.value }) });
        const d = await r.json();
        if (r.ok) { toast("OK"); window.location.hash = "#/login"; }
        // t() falls back to the key itself, so an unmapped server error still shows something
        else toast(t((d && (d.error || d.detail)) || "failed"), false);
      } }, t("register")))),
  );
  return { title: "", body };
}

async function dashboardView() {
  if (!state.user) return loginView();
  let cards = [];
  try {
    const [tk, kb, cu] = await Promise.all([
      api("/api/tickets"), api("/api/kb/articles"), hasPerm("customer.view") ? api("/api/customers") : null,
    ]);
    const open = (tk.items || []).filter(x => x.status !== "closed").length;
    cards = [
      [t("total_tickets"), (tk.items || []).length],
      [t("open"), open],
      [t("articles"), (kb.items || []).length],
      hasPerm("customer.view") ? [t("customers"), (cu.items || []).length] : null,
    ].filter(Boolean);
  } catch (e) { toast(e.message, false); }
  const body = h("div", {},
    h("h1", {}, t("welcome") + " " + state.user.display_name),
    h("div", { style: "display:flex;gap:12px;flex-wrap:wrap;margin:14px 0" },
      cards.map(([label, val]) => h("div", { class: "card", style: "padding:14px 18px;min-width:140px" },
        h("div", { class: "muted" }, label),
        h("div", { style: "font-size:28px;font-weight:700" }, String(val))))),
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("div", { class: "flex-between" }, h("h3", {}, t("tickets")),
        h("a", { class: "btn btn-ghost btn-sm", href: "#/tickets" }, t("tickets") + " →")),
      h("div", {},
        hasPerm("ticket.create")
          ? h("button", { class: "btn btn-blue btn-sm", onclick: () => window.location.hash = "#/new-ticket" }, "+" + t("new_ticket"))
          : null)),
  ));
  return { title: "", body };
}

async function ticketsView() {
  if (!state.user) return loginView();
  const statusSel = h("select", {}, [h("option", { value: "" }, t("all") + " · " + t("status")),
    ["new", "customer_replied", "support_replied", "closed"].map(s => h("option", { value: s }, t("st_" + s)))]);
  const prioSel = h("select", {}, [h("option", { value: "" }, t("all") + " · " + t("priority")),
    ["critical", "high", "medium", "low"].map(p => h("option", { value: p }, t("pr_" + p)))]);
  const modules = (state.modules && state.modules.length) ? state.modules : (state.products || []);
  const prodSel = h("select", {}, [h("option", { value: "" }, t("all") + " · " + t("module")),
    modules.map(p => h("option", { value: p }, p))]);
  const ownerInp = h("input", { placeholder: t("filter_by_owner") });
  const custInp = h("input", { placeholder: t("filter_by_customer") });
  const verInp = h("input", { placeholder: t("filter_by_version") });
  const dFrom = h("input", { type: "date", placeholder: t("filter_by_date_from") });
  const dTo = h("input", { type: "date", placeholder: t("filter_by_date_to") });

  async function doLoad() {
    const p = new URLSearchParams();
    if (statusSel.value) p.set("status", statusSel.value);
    if (prioSel.value) p.set("priority", prioSel.value);
    if (prodSel.value) p.set("module", prodSel.value);
    if (ownerInp.value) p.set("owner", ownerInp.value);
    if (custInp.value) p.set("customer", custInp.value);
    if (verInp.value) p.set("version", verInp.value);
    if (dFrom.value) p.set("date_from", dFrom.value);
    if (dTo.value) p.set("date_to", dTo.value);
    const r = await api("/api/tickets?" + p.toString());
    renderRows(r.items || []);
  }
  function renderRows(items) {
    tbody.innerHTML = "";
    if (!items.length) { tbody.append(h("tr", {}, h("td", { colspan: 11, class: "muted" }, t("no_results")))); }
    for (const t of items) {
      tbody.append(h("tr", {},
        h("td", {}, h("a", { href: "#/ticket/" + t.id }, t.code)),
        h("td", {}, t.title),
        h("td", {}, t.customer_name || "-"),
        h("td", {}, t.product || "-"),
        h("td", {}, t.deploy_type || "-"),
        h("td", {}, t.version || "-"),
        h("td", {}, prioTag(t.priority)),
        h("td", {}, statusTag(t.status)),
        h("td", {}, t.owner_name || "-"),
        h("td", {}, (t.created_at || "").slice(0, 10)),
        h("td", {}, t.source === "email" ? "Email" : "Web"),
      ));
    }
  }
  const tbody = h("tbody", {});
  const body = h("div", {},
    h("div", { class: "flex-between" }, h("h1", {}, t("tickets")),
      hasPerm("ticket.create") ? h("button", { class: "btn btn-blue", onclick: () => window.location.hash = "#/new-ticket" }, "+" + t("new_ticket")) : null),
    h("div", { class: "card mb-2" }, h("div", { class: "card-body" },
      h("div", { class: "filters" }, statusSel, prioSel, prodSel, ownerInp, custInp, verInp, dFrom, dTo,
        h("button", { class: "btn btn-ghost btn-sm", onclick: doLoad }, t("filter"))))),
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("table", {},
        h("thead", {}, h("tr", {},
          [t("code"), t("title"), t("customer"), t("module"), t("deploy_type"), t("version"), t("priority"), t("status"), t("owner"), t("created"), t("source")]
            .map(c => h("th", {}, c)))),
        tbody))));
  [statusSel, prioSel, prodSel, ownerInp, custInp, verInp, dFrom, dTo].forEach(el => {
    el.addEventListener("change", doLoad);
    if (el.tagName === "INPUT" && el.type !== "date") el.addEventListener("input", doLoad);
  });
  doLoad();
  return { title: "", body };
}

const KB_VIS = ["public", "registered", "internal"];

/**
 * Closing a ticket asks a single question: does this solution go to the
 * knowledge base, and in which form? Masked is ticked by default -- unmasking
 * is the deliberate opt-out, and it is the only mode that carries files.
 */
function openCloseDialog(T, internal, onConfirm) {
  const desChk = h("input", { type: "checkbox", checked: true, style: "margin-top:3px" });
  const visSel = h("select", {}, KB_VIS.map(v =>
    h("option", { value: v, selected: v === "registered" ? "selected" : null }, t(v))));
  const body = h("div", {},
    h("p", {}, t("close_share_prompt")),
    h("label", { style: "display:flex;gap:8px;align-items:flex-start;margin:12px 0" },
      desChk,
      h("span", {}, h("b", {}, t("share_desensitized")),
        h("div", { class: "muted", style: "font-size:12px;margin-top:2px" }, t("share_desensitized_hint")))),
    h("div", { class: "muted", style: "font-size:12px;border-left:3px solid var(--line,#e4e7eb);padding-left:8px" },
      t("share_raw_hint")),
    internal ? h("label", { class: "field", style: "margin-top:12px" },
      h("span", { class: "muted" }, t("kb_visibility")), visSel) : null,
    h("div", { class: "flex-between", style: "margin-top:16px;gap:8px" },
      h("button", { class: "btn btn-ghost", onclick: () => { closeModal(); onConfirm({ share: 0 }); } },
        t("close_without_share")),
      h("button", { class: "btn btn-blue", onclick: () => {
        closeModal();
        onConfirm({ share: 1, desensitize: desChk.checked ? 1 : 0,
                    visibility: visSel.value });
      } }, t("close_and_share"))));
  showModal(t("close_ticket"), body);
}

/** The desk publishing a thread by hand, with the audience it should reach. */
function openShareDialog(T, onConfirm) {
  const desChk = h("input", { type: "checkbox", checked: true, style: "margin-top:3px" });
  const visSel = h("select", {}, KB_VIS.map(v =>
    h("option", { value: v, selected: v === "registered" ? "selected" : null }, t(v))));
  const body = h("div", {},
    h("p", {}, t("share_kb_prompt")),
    h("label", { class: "field", style: "margin-top:10px" },
      h("span", { class: "muted" }, t("kb_visibility")), visSel),
    h("label", { style: "display:flex;gap:8px;align-items:flex-start;margin:12px 0" },
      desChk,
      h("span", {}, h("b", {}, t("share_desensitized")),
        h("div", { class: "muted", style: "font-size:12px;margin-top:2px" }, t("share_desensitized_hint")))),
    h("div", { class: "muted", style: "font-size:12px" }, t("share_raw_hint")),
    h("div", { class: "flex-between", style: "margin-top:16px;gap:8px" },
      h("button", { class: "btn btn-ghost", onclick: closeModal }, t("cancel")),
      h("button", { class: "btn btn-blue", onclick: () => {
        closeModal();
        onConfirm({ visibility: visSel.value, desensitize: desChk.checked ? 1 : 0 });
      } }, t("save_and_share"))));
  showModal(t("share_to_kb"), body);
}

async function ticketDetail(id) {
  if (!state.user) return loginView();
  if (!id) { window.location.hash = "#/tickets"; return { title: "", body: h("div", {}) }; }
  let data;
  try { data = await api("/api/tickets/" + id); } catch (e) {
    // surface the real reason instead of a bare "no results"
    const msg = e.message === "not_found" ? t("ticket_not_found")
              : e.message === "no_permission" ? t("no_permission")
              : e.message;
    return { title: "", body: h("div", { class: "card" }, h("div", { class: "card-body" },
      h("p", {}, msg),
      h("a", { class: "btn btn-ghost btn-sm", href: "#/tickets" }, "← " + t("tickets")))) };
  }
  const T = data.ticket;
  // The internal desk owns the workflow. Claim / reassign / pick a status /
  // write an internal note are hidden from customers and partners -- they only
  // ever see "关闭工单". `internal_user` comes from the ticket payload, with the
  // cached /api/me flag as a fallback.
  const internal = !!(data.internal_user || (state.user && state.user.is_internal));
  // Reassigning a ticket is permission-driven: L2 / the administrator role / the
  // internal group all carry ticket.change_owner. The candidate list is internal
  // users only, and the API refuses anybody else.
  const can = {
    claim: internal && hasPerm("ticket.claim"),
    owner: hasPerm("ticket.change_owner"),
    status: internal && hasPerm("ticket.change_status"),
    reply: hasPerm("ticket.reply"),
    export: hasPerm("ticket.export"),
  };
  const ownerSel = h("select", { id: "owner-sel" }, h("option", { value: "" }, t("owner") + "…"));
  // Candidates come from /api/tickets/assignees: internal users only, so the
  // list no longer needs the admin-only /api/admin/users (which used to 403 for
  // L2 and leave the picker empty).
  if (can.owner) (async () => {
    try {
      const r = await api("/api/tickets/assignees");
      ownerSel.innerHTML = "";
      ownerSel.append(h("option", { value: "" }, "- " + t("unassigned") + " -"));
      for (const u of (r.items || [])) {
        ownerSel.append(h("option", { value: u.id, selected: String(u.id) === String(T.owner_id) ? "selected" : null },
          u.display_name || u.email));
      }
    } catch (e) {}
  })();
  ownerSel.onchange = async () => {
    // an empty choice takes the ticket back to the pool ("未指派")
    try {
      await api("/api/tickets/" + id + "/owner", { method: "PUT",
        body: JSON.stringify({ owner_id: ownerSel.value ? Number(ownerSel.value) : null }) });
      toast("OK");
      setTimeout(() => render(), 300);
    } catch (e) {
      toast(e.message === "assignee_not_internal" ? t("assignee_not_internal") : e.message, false);
    }
  };

  const closeSel = h("select", {},
    h("option", { value: "" }, t("status") + "…"),
    ["new", "customer_replied", "support_replied", "closed"].map(s => h("option", { value: s, selected: T.status === s ? "selected" : null }, t("st_" + s))));

  const sendStatus = async (body) => {
    try {
      await api("/api/tickets/" + id + "/status", { method: "PUT", body: JSON.stringify(body) });
      toast("OK"); setTimeout(() => render(), 400);
    } catch (e) { toast(e.message, false); }
  };
  const statusBtn = h("button", { class: "btn btn-ghost", onclick: () => {
    if (!closeSel.value) return;
    const st = closeSel.value;
    // the desk closing a ticket is asked the same sharing question
    if (st === "closed") {
      openCloseDialog(T, true, r => sendStatus(Object.assign({ status: st },
        r.share ? { share_kb: 1, kb_desensitize: r.desensitize, kb_visibility: r.visibility }
                : { share_kb: 0 })));
    } else sendStatus({ status: st });
  } }, "Update " + t("status"));

  // Everyone may close a ticket, and whoever closes it decides whether -- and
  // in which form -- the solution is published to the knowledge base.
  const closeBtn = T.status === "closed" ? null : h("button", { class: "btn btn-ghost", onclick: () => {
    openCloseDialog(T, internal, async r => {
      try {
        await api("/api/tickets/" + id + "/close", { method: "POST", body: JSON.stringify(
          r.share ? { share_kb: 1, kb_desensitize: r.desensitize, kb_visibility: r.visibility }
                  : { share_kb: 0 }) });
        toast(r.share ? t("share_saved") : "OK"); setTimeout(() => render(), 600);
      } catch (e) { toast(e.message, false); }
    });
  } }, t("close_ticket"));

  // The desk may publish a thread at any time -- open or closed -- and picks
  // the audience that will be able to search it.
  const shareBtn = internal ? h("button", { class: "btn btn-ghost", onclick: () => {
    openShareDialog(T, async r => {
      try {
        const res = await api("/api/tickets/" + id + "/share_kb", { method: "POST", body: JSON.stringify(r) });
        toast(t("share_saved") + " · #" + res.article_id); setTimeout(() => render(), 600);
      } catch (e) { toast(e.message, false); }
    });
  } }, t("share_to_kb")) : null;

  const kbLink = T.kb_article_id
    ? h("a", { class: "tag", style: "text-decoration:none", href: "#/kb/" + T.kb_article_id },
        t("shared_to_kb") + " ↗")
    : null;

  // reply box
  const msgInp = h("textarea", { rows: 3, placeholder: t("reply") + "…" });
  const intChk = h("input", { type: "checkbox" });
  const fileInp = h("input", { type: "file", multiple: true });
  const replyBtn = h("button", { class: "btn btn-blue", onclick: async () => {
    const fd = new FormData();
    fd.set("body", msgInp.value);
    if (intChk.checked) fd.set("internal", "1");
    for (const f of fileInp.files) fd.append("files", f);
    await apiForm("/api/tickets/" + id + "/reply", fd);
    msgInp.value = ""; toast("OK"); setTimeout(() => render(), 400);
  } }, t("reply"));

  const claimBtn = T.owner_id ? null : h("button", { class: "btn btn-ghost", onclick: async () => {
    await api("/api/tickets/" + id + "/claim", { method: "POST" }); toast("OK"); setTimeout(() => render(), 400);
  } }, t("claim"));

  const msgs2 = h("div", {}, (data.messages || []).map(m => {
    return h("div", { class: "card mb-2" }, h("div", { class: "card-body" },
      h("div", { class: "flex-between" },
        h("div", {}, h("b", {}, m.author_name || m.author_email || "—"),
          h("span", { class: "muted" }, "  " + m.created_at),
          m.internal ? h("span", { class: "tag medium", style: "margin-left:8px" }, "internal") : null),
        null),
      h("div", { style: "white-space:pre-wrap;margin-top:8px" }, m.body || ""),
      attachmentList(m.attachments)));
  }));

  const body = h("div", {},
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("div", { class: "flex-between" },
        h("h1", { style: "margin:0" }, T.code + " · " + T.title),
        statusTag(T.status)),
      h("div", { class: "muted mb-2" },
        [t("customer") + " " + (T.customer_name || "-"), t("module") + " " + (T.product || "-"),
          t("version") + " " + (T.version || "-"), t("source") + " " + T.source,
          t("owner") + " " + (T.owner_name || t("unassigned")),
          t("created") + " " + T.created_at].join(" · ")),
      h("div", { class: "ticket-actions" },
        can.claim ? claimBtn : null,
        can.owner ? h("label", {}, ownerSel) : null,
        can.status ? h("label", {}, closeSel, statusBtn) : null,
        closeBtn,
        shareBtn,
        kbLink,
        can.export ? h("a", { class: "btn btn-ghost", href: "/api/kb/export/" + (T.kb_article_id || id), target: "_blank" }, t("export_pdf")) : null,
      ),
      h("div", { class: "card-body", style: "background:var(--bg)" },
        h("div", { class: "flex-between mb-2" }, h("h3", {}, t("messages") + " (" + (data.messages || []).length + ")"),
          (can.reply && internal) ? h("label", { class: "muted" }, intChk, t("internal")) : null),
        msgs2,
        can.reply ? h("div", { class: "card-body", style: "background:var(--card)" },
          h("div", {}, msgInp, fileInp),
          h("div", { class: "flex-between", style: "margin-top:8px" },
            h("span", { class: "muted" }, internal ? (t("reply") + " · " + t("internal")) : t("reply")),
            replyBtn)) : null))));
  return { title: "", body };
}

async function newTicketView() {
  if (!state.user) return loginView();
  const custInp = h("input", { placeholder: t("select_customer"), style: "width:100%" });
  // fuzzy customer matcher — shows a pick list while typing
  const custWrap = attachSuggest(custInp, async val => {
    const r = await api("/api/customers?q=" + encodeURIComponent(val));
    return (r.items || []).map(c => ({ ...c, label: c.name + (c.domains ? "  <" + c.domains + ">" : "") }));
  }, c => { custInp.value = c.name; });

  // A customer user only reaches their own customer's tickets, and the API pins
  // the ticket to that customer — so don't offer them a free-text picker.
  const isStaff = hasPerm("ticket.view_all");
  // Group names became "Customer-<name>" in the partner round; the old
  // "客户组:" prefix is kept as a fallback so a stale payload still resolves.
  const CUST_GROUP_RE = /^(?:Customer-|客户组[:：]|客户[:：])/;
  const ownCustomers = (state.user.groups || [])
    .filter(g => CUST_GROUP_RE.test(g)).map(g => g.replace(CUST_GROUP_RE, ""));
  const custField = isStaff
    ? h("label", { class: "field" }, h("span", { class: "muted" }, t("customer")), custWrap)
    : h("label", { class: "field" }, h("span", { class: "muted" }, t("customer")),
        h("input", { value: ownCustomers.join(", ") || "-", disabled: true }),
        h("div", { class: "muted", style: "font-size:12px" }, t("customer_locked_hint")));

  const modules = (state.modules && state.modules.length) ? state.modules : (state.products || []);
  const prodSel = h("select", { style: "width:100%" },
    h("option", { value: "" }, t("select_product")),
    modules.map(p => h("option", { value: p }, p)));
  const depSel = h("select", { style: "width:100%" },
    (state.deployTypes || ["ON-PREM", "SaaS"]).map(d => h("option", { value: d, selected: d === "ON-PREM" ? "selected" : null }, d)));
  const prioSel = h("select", { style: "width:100%" },
    ["critical", "high", "medium", "low"].map(p => h("option", { value: p, selected: p === "medium" ? "selected" : null }, t("pr_" + p))));
  const verInp = h("input", { placeholder: t("version"), style: "width:100%" });
  const title = h("input", { placeholder: t("title"), required: true, style: "width:100%" });
  const desc = h("textarea", { rows: 6, placeholder: t("desc"), style: "width:100%;box-sizing:border-box" });
  const intChk = h("input", { type: "checkbox" });
  const fileInp = h("input", { type: "file", multiple: true });

  const body = h("div", {},
    h("h1", {}, t("new_ticket")),
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("div", {},
        h("label", { class: "field" }, h("span", { class: "muted" }, t("title")), title),
        custField,
        h("div", { class: "grid-4" },
          h("label", { class: "field" }, h("span", { class: "muted" }, t("module")), prodSel),
          h("label", { class: "field" }, h("span", { class: "muted" }, t("deploy_type")), depSel),
          h("label", { class: "field" }, h("span", { class: "muted" }, t("version")), verInp),
          h("label", { class: "field" }, h("span", { class: "muted" }, t("select_priority")), prioSel)),
        h("label", { class: "field" }, h("span", { class: "muted" }, t("desc")), desc),
        h("div", { style: "margin:8px 0" },
          isStaff ? h("label", { class: "muted" }, intChk, " " + t("internal")) : null, fileInp),
        h("div", { style: "margin-top:10px" },
          h("button", { class: "btn btn-blue", onclick: async () => {
            if (!title.value) { toast(t("title") + " *", false); return; }
            const fd = new FormData();
            fd.set("title", title.value);
            fd.set("customer_name", custInp.value);
            fd.set("product", prodSel.value);
            fd.set("deploy_type", depSel.value);
            fd.set("version", verInp.value);
            fd.set("priority", prioSel.value);
            fd.set("description", desc.value);
            if (intChk.checked) fd.set("internal", "1");
            for (const f of fileInp.files) fd.append("files", f);
            try {
              const r = await apiForm("/api/tickets", fd);
              if (!r || r.id == null) { toast("ticket_create_failed", false); return; }
              toast("OK: " + (r.code || r.id));
              window.location.hash = "#/ticket/" + r.id;
              // the hash may already be the same string on a re-submit
              if (current().parts[1] === String(r.id)) render();
            } catch (e) { toast(e.message, false); }
          } }, t("submit")),
          h("button", { class: "btn btn-ghost", style: "margin-left:8px", onclick: () => window.location.hash = "#/tickets" }, t("cancel")))))));
  return { title: "", body };
}

async function kbView() {
  if (!state.user) return loginView();
  const q = h("input", { placeholder: t("kb_search"), class: "filter-grow" });
  const modules = (state.modules && state.modules.length) ? state.modules : [];
  const modSel = h("select", {}, [h("option", { value: "" }, t("all") + " · " + t("module")),
    modules.map(m => h("option", { value: m }, m))]);
  const colSel = h("select", {}, [h("option", { value: "" }, t("all") + " · " + t("collections"))]);
  const tbody = h("tbody", {});
  let cols = [];
  const canEdit = state.meta && state.meta.can_edit_kb;
  async function loadCols() {
    try {
      const r = await api("/api/kb/collections");
      cols = r.items || [];
      colSel.innerHTML = "";
      colSel.append(h("option", { value: "" }, t("all") + " · " + t("collections")));
      for (const c of cols) colSel.append(h("option", { value: c.id }, c.name + " · " + t(c.visibility)));
    } catch (e) {}
  }
  async function doLoad() {
    const p = new URLSearchParams();
    if (q.value) p.set("q", q.value);
    if (modSel.value) p.set("module", modSel.value);
    if (colSel.value) p.set("collection", colSel.value);
    const r = await api("/api/kb/articles?" + p.toString());
    tbody.innerHTML = "";
    if (!(r.items || []).length) {
      tbody.append(h("tr", {}, h("td", { colspan: 5, class: "muted" }, t("no_results"))));
      return;
    }
    for (const a of (r.items || [])) {
      tbody.append(h("tr", {},
        h("td", {}, h("a", { href: "#/kb/" + a.id }, a.title)),
        h("td", {}, a.module || "-"),
        h("td", {}, visTag(a.visibility)),
        h("td", {}, a.source),
        h("td", {}, (a.created_at || "").slice(0, 10))));
    }
  }
  modSel.onchange = doLoad;
  colSel.onchange = doLoad;
  q.oninput = () => { clearTimeout(q._t); q._t = setTimeout(doLoad, 250); };
  loadCols();
  doLoad();
  const body = h("div", {},
    h("div", { class: "flex-between" }, h("h1", {}, t("kb")),
      h("div", {},
        hasPerm("kb.import") ? h("button", { class: "btn btn-ghost", onclick: () => openImportModal(colSel.value) }, t("import")) : null,
        hasPerm("kb.manage_collections") ? h("a", { class: "btn btn-ghost", href: "#/kb/collections", style: "margin-left:8px" }, t("kb_collections")) : null,
        canEdit ? h("a", { class: "btn btn-blue", href: "#/kb/new", style: "margin-left:8px" }, "+" + t("new_article")) : null)),
    h("div", { class: "card mb-2" }, h("div", { class: "card-body" },
      h("div", { class: "filters" }, q, modSel, colSel))),
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("table", {},
        h("thead", {}, h("tr", {}, [t("title"), t("module"), t("visibility"), "Source", t("created")].map(c => h("th", {}, c)))),
        tbody))));
  return { title: "", body };
}

function openImportModal(preCol) {
  const colSel = h("select", {}, [h("option", { value: "" }, t("all") + " · " + t("collections"))]);
  const visSel = h("select", {},
    ["public", "registered", "internal"].map(v => h("option", { value: v, selected: v === "registered" ? "selected" : null }, t(v))));
  const fileInp = h("input", { type: "file", multiple: true });
  const body = h("div", {},
    h("label", { style: "display:block;margin:6px 0" }, h("span", { class: "muted" }, t("collections")), colSel),
    h("label", { style: "display:block;margin:6px 0" }, h("span", { class: "muted" }, t("kb_visibility")), visSel),
    h("p", { class: "muted" }, t("import_desc")),
    h("div", {}, fileInp),
    h("button", { class: "btn btn-blue mt-2", onclick: async () => {
      const fd = new FormData();
      fd.set("collection", colSel.value);
      fd.set("visibility", visSel.value);
      for (const f of fileInp.files) fd.append("files", f);
      const r = await apiForm("/api/kb/import", fd);
      toast("Imported " + r.imported + " files"); window.location.hash = "#/kb"; render();
    } }, t("submit")));
  showModal("Import", body);
}

/** Full-page KB editor (new or edit) — never a modal, so nothing is lost on outside click. */
async function kbEditorPage(id) {
  if (!state.user) return loginView();
  let art = null;
  if (id) {
    try { const resp = await api("/api/kb/articles/" + id); art = resp.article; } catch (e) { art = null; }
  }
  const canEdit = state.meta && state.meta.can_edit_kb;
  if (!canEdit) return noAccess();

  const title = h("input", { value: art ? art.title : "", placeholder: t("kb_title"),
    style: "width:100%;box-sizing:border-box;font-size:18px;padding:8px" });
  const bodyTa = h("textarea", { rows: 14, placeholder: t("kb_body"), class: "kb-editor-body" });
  bodyTa.value = art ? (art.body || "") : "";
  // auto-grow with content
  const autoGrow = () => { bodyTa.style.height = "auto"; bodyTa.style.height = Math.max(340, bodyTa.scrollHeight + 24) + "px"; };
  bodyTa.addEventListener("input", autoGrow);

  const visSel = h("select", { style: "width:100%" },
    ["public", "registered", "internal"].map(v => h("option", { value: v, selected: (art ? art.visibility : "registered") === v ? "selected" : null }, t(v))));
  const modules = (state.modules && state.modules.length) ? state.modules : [];
  const modSel = h("select", { style: "width:100%" },
    h("option", { value: "" }, "- " + t("module") + " -"),
    modules.map(m => h("option", { value: m, selected: art && art.module === m ? "selected" : null }, m)));
  const body = h("div", {},
    h("div", { class: "flex-between mb-2" },
      h("h1", {}, id ? t("edit_article") : t("new_article_title")),
      h("div", {},
        h("a", { class: "btn btn-ghost btn-sm", href: id ? "#/kb/" + id : "#/kb" }, t("back")),
        h("button", { class: "btn btn-blue btn-sm", style: "margin-left:8px", onclick: async () => {
          if (!title.value.trim()) { toast(t("kb_title") + " *", false); return; }
          const payload = { title: title.value, body: bodyTa.value, visibility: visSel.value,
                            module: modSel.value };
          try {
            if (id) await api("/api/kb/articles/" + id, { method: "PUT", body: JSON.stringify(payload) });
            else { const r = await api("/api/kb/articles", { method: "POST", body: JSON.stringify(payload) }); id = r.id; }
            toast(t("saved")); window.location.hash = "#/kb/" + id; render();
          } catch (e) { toast(e.message, false); }
        } }, t("save")))),
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("label", { class: "field" }, h("span", { class: "muted" }, t("kb_title")), title),
      h("div", { class: "grid-2" },
        h("label", { class: "field" }, h("span", { class: "muted" }, t("visibility")), visSel),
        h("label", { class: "field" }, h("span", { class: "muted" }, t("module")), modSel)),
      h("label", { class: "field" }, h("span", { class: "muted" }, t("kb_body")), bodyTa))));
  setTimeout(autoGrow, 0);
  return { title: "", body };
}

function showModal(head, bodyEl) {
  const p = h("div", { class: "modal" },
    h("div", { class: "panel" },
      h("div", { class: "head" }, head),
      h("div", { class: "body" }, bodyEl),
      h("div", { class: "foot" }, h("button", { class: "btn btn-ghost", onclick: closeModal }, "✕"))));
  document.body.append(p);
}
function closeModal() { document.querySelectorAll(".modal").forEach(m => m.remove()); }

async function kbCollections() {
  const cols = await api("/api/kb/collections");
  const rows = (cols.items || []).map(c => h("tr", {},
    h("td", {}, c.id), h("td", {}, c.name), h("td", {}, visTag(c.visibility)),
    h("td", {}, h("button", { class: "btn btn-ghost btn-sm", onclick: async () => {
      await api("/api/kb/collections/" + c.id, { method: "DELETE" }); render();
    } }, t("delete")))));
  const form = h("div", { class: "flex-between mb-2" },
    h("h3", {}, t("kb_collections")),
    hasPerm("kb.manage_collections")
      ? h("button", { class: "btn btn-blue btn-sm", onclick: async () => {
        const name = prompt("Name"); if (!name) return;
        const r = await api("/api/kb/collections", { method: "POST", body: JSON.stringify({ name, visibility: "registered" }) });
        toast("OK " + r.id); render();
      } }, "+" + t("kb_collections"))
      : null);
  return { title: "", body: h("div", {},
    form,
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("table", {}, h("thead", {}, h("tr", {}, [t("code"), t("name"), t("status")].map(x => h("th", {}, x)))),
        h("tbody", {}, rows))))) };
}

async function kbDetail(id) {
  if (!id) return kbView();
  let data;
  try { data = await api("/api/kb/articles/" + id); }
  catch (e) {
    return { title: "", body: h("div", { class: "card" }, h("div", { class: "card-body" },
      h("h1", {}, t("need_login")), h("a", { href: "#/login" }, t("login")))) };
  }
  const a = data.article;
  const canEdit = !!(data.can_edit || (state.meta && state.meta.can_edit_kb));
  const canExport = hasPerm("kb.export_pdf");
  const canShare = hasPerm("kb.share_email");
  const canDelete = hasPerm("kb.delete") && canEdit;
  const body = h("div", { class: "kb-doc" },
    h("div", { class: "kb-head mb-2" },
      h("button", { class: "btn btn-ghost btn-sm", onclick: () => {
        // go back to the filtered list; fall back to #/kb
        if (history.length > 1) history.back(); else window.location.hash = "#/kb";
      } }, "← " + t("back")),
      h("div", { style: "display:flex;gap:6px" },
        canExport ? h("a", { class: "btn btn-ghost btn-sm", href: "/api/kb/export/" + a.id, target: "_blank" }, t("export_pdf")) : null,
        canShare ? h("button", { class: "btn btn-ghost btn-sm", onclick: async () => {
          const email = prompt("Email to share with");
          if (!email) return;
          await api("/api/kb/share", { method: "POST", body: JSON.stringify({ article_id: a.id, email }) });
          toast("OK");
        } }, t("share")) : null,
        canEdit ? h("a", { class: "btn btn-blue btn-sm", href: "#/kb/edit/" + a.id }, t("edit")) : null,
        canDelete ? h("button", { class: "btn btn-ghost btn-sm", style: "color:#dc2626", onclick: async () => {
          if (!confirm(t("confirm_delete"))) return;
          try { await api("/api/kb/articles/" + a.id, { method: "DELETE" }); window.location.hash = "#/kb"; }
          catch (e) { toast(e.message, false); }
        } }, t("delete")) : null)),
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("div", { class: "flex-between" },
        h("h1", { style: "margin:0" }, a.title, " ", visTag(a.visibility)), null),
      h("div", { class: "muted mb-2" },
        [a.module ? t("module") + ": " + a.module : null, a.source,
          a.desensitized ? "desensitized" : "raw", a.created_at].filter(Boolean).join(" · ")),
      h("div", { class: "markdown", innerHTML: mdToHtml(a.body) }),
      h("div", { style: "margin-top:8px" },
        // a masked article carries no file at all -- say so, otherwise the
        // reader assumes the upload was lost
        a.desensitized
          ? h("div", { class: "muted", style: "font-size:12px;margin-top:8px" },
              t("kb_masked_no_files"))
          : null,
        attachmentList(data.attachments)))));
  return { title: "", body };
}

/** Create / edit a customer. A partner (代理商) may be assigned, or left unset. */
async function openCustomerEditor(cust, onDone) {
  const isNew = !cust;
  // an <input type="date"> silently blanks any value that is not YYYY-MM-DD
  const dv = v => (/^\d{4}-\d{2}-\d{2}/.test(v || "") ? String(v).slice(0, 10) : "");
  const f = (key, type) => h("input", {
    value: cust ? (type === "date" ? dv(cust[key]) : (cust[key] || "")) : "",
    type: type || "text", placeholder: t(key) });
  const name = f("name"), domains = f("domains"), version = f("version");
  const start = f("service_start", "date"), end = f("service_end", "date"), contact = f("contact_email", "email");

  // partner picker -- optional, "— none —" means the customer has no partner
  let partnerSel = null;
  if (hasPerm("partner.view")) {
    partnerSel = h("select", {}, h("option", { value: "" }, t("no_partner")));
    try {
      const r = await api("/api/partners");
      for (const p of (r.items || [])) {
        partnerSel.append(h("option", { value: String(p.id) }, p.name + (p.domains ? "  <" + p.domains + ">" : "")));
      }
    } catch (e) { /* no partner permission / backend down: keep the empty list */ }
    partnerSel.value = cust && cust.partner_id ? String(cust.partner_id) : "";
  }

  const body = h("div", { class: "page" },
    h("label", { class: "field" }, h("span", { class: "muted" }, t("name")), name),
    h("label", { class: "field" }, h("span", { class: "muted" }, t("domains")), domains),
    partnerSel ? h("div", { class: "grid-2" },
      h("label", { class: "field" }, h("span", { class: "muted" }, t("linked_partner")), partnerSel),
      h("div", { class: "field" }, h("span", { class: "muted" }, "\u00a0"),
        h("p", { class: "muted", style: "font-size:12px;margin:0" }, t("partner_hint")))) : null,
    h("div", { class: "grid-3" },
      h("label", { class: "field" }, h("span", { class: "muted" }, t("version")), version),
      h("label", { class: "field" }, h("span", { class: "muted" }, t("service_start")), start),
      h("label", { class: "field" }, h("span", { class: "muted" }, t("service_end")), end)),
    h("label", { class: "field" }, h("span", { class: "muted" }, t("contact_email")), contact),
    h("div", { class: "flex-between mt-2" },
      h("button", { class: "btn btn-blue", onclick: async () => {
        // Only the name is universally required. Email domains are mandatory when
        // creating (the API enforces that too), but an existing customer may
        // legitimately have none -- requiring it here made every edit of such a
        // record silently refuse to save.
        if (!name.value.trim()) { toast(t("name") + " *", false); return; }
        if (isNew && !domains.value.trim()) { toast(t("name") + " / " + t("domains") + " *", false); return; }
        const payload = { name: name.value, domains: domains.value, version: version.value,
                          service_start: start.value, service_end: end.value, contact_email: contact.value };
        if (partnerSel) payload.partner_id = partnerSel.value ? Number(partnerSel.value) : null;
        try {
          if (isNew) await api("/api/customers", { method: "POST", body: JSON.stringify(payload) });
          else await api("/api/customers/" + cust.id, { method: "PUT", body: JSON.stringify(payload) });
          toast(t("saved")); closeModal(); onDone && onDone();
        } catch (e) { toast(e.message, false); }
      } }, t("save")),
      h("button", { class: "btn btn-ghost", onclick: closeModal }, t("cancel"))));
  showModal(isNew ? t("add_customer") : t("edit_customer"), body);
}

async function customersView() {
  if (!hasPerm("customer.view")) return noAccess();
  const tbody = h("tbody", {});
  const selected = new Set();
  let rows = [];

  const q = h("input", { placeholder: t("search_customer"), style: "width:100%" });
  const qWrap = attachSuggest(q, async val => {
    const r = await api("/api/customers?q=" + encodeURIComponent(val));
    return (r.items || []).map(c => ({ ...c, label: c.name + (c.domains ? "  <" + c.domains + ">" : "") }));
  }, c => { q.value = c.name; doLoad(); });

  const selectAll = h("input", { type: "checkbox", onchange: e => {
    const on = e.target.checked;
    selected.clear();
    if (on) rows.forEach(c => selected.add(c.id));
    renderRows();
  } });

  function renderRows() {
    tbody.innerHTML = "";
    if (!rows.length) {
      tbody.append(h("tr", {}, h("td", { colspan: 9, class: "muted" }, t("no_results"))));
      return;
    }
    for (const c of rows) {
      const cb = h("input", { type: "checkbox", checked: selected.has(c.id) ? "checked" : null, onchange: e => {
        if (e.target.checked) selected.add(c.id); else selected.delete(c.id);
        selectAll.checked = rows.length > 0 && rows.every(x => selected.has(x.id));
      } });
      tbody.append(h("tr", {},
        h("td", {}, cb),
        h("td", {}, h("a", { href: "javascript:void(0)", onclick: () => openCustomerEditor(c, doLoad) }, c.name)),
        h("td", {}, c.domains),
        h("td", {}, c.partner_name
          ? h("span", { class: "tag partner" }, c.partner_name)
          : h("span", { class: "muted" }, "\u2014")),
        h("td", {}, c.version || "-"),
        h("td", {}, c.service_start || "-"), h("td", {}, c.service_end || "-"),
        h("td", {}, c.contact_email || "-"),
        h("td", {}, h("button", { class: "btn btn-ghost btn-sm", style: "color:#dc2626", onclick: async () => {
          if (!confirm(t("confirm_delete") + " " + c.name)) return;
          await api("/api/customers/" + c.id, { method: "DELETE" }); toast(t("saved")); doLoad();
        } }, t("delete")))));
    }
  }

  async function doLoad() {
    const r = await api("/api/customers?q=" + encodeURIComponent((q.value || "").trim()));
    rows = r.items || [];
    selected.clear(); selectAll.checked = false;
    renderRows();
  }

  async function bulkDelete() {
    const ids = Array.from(selected);
    if (!ids.length) { toast(t("select_first"), false); return; }
    if (!confirm(t("confirm_delete") + " (" + ids.length + ")")) return;
    try {
      await api("/api/customers/bulk", { method: "POST", body: JSON.stringify({ ids }) });
      toast(t("saved")); doLoad();
    } catch (e) { toast(e.message, false); }
  }

  const importInput = h("input", { type: "file", accept: ".csv", style: "display:none", onchange: async e => {
    const f = e.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("file", f);
    try {
      const r = await apiForm("/api/customers/import", fd);
      toast(t("imported") + ": " + r.created + " / " + r.updated); doLoad();
    } catch (err) { toast(err.message, false); }
    e.target.value = "";
  } });

  const body = h("div", {},
    h("h1", {}, t("registered_users")),
    registeredTabs("customers"),
    h("div", { class: "toolbar" },
      h("div", { class: "toolbar-left" }, qWrap),
      h("div", { class: "toolbar-right" },
        h("label", { class: "btn btn-ghost btn-sm" }, t("import_csv"), importInput,
          h("a", { href: "/api/customers/template.csv", download: true, style: "margin-left:6px;color:var(--blue)" }, "↓ " + t("download_template"))),
        hasPerm("customer.create") ? h("button", { class: "btn btn-blue btn-sm", onclick: () => openCustomerEditor(null, doLoad) }, "+" + t("add_customer")) : null,
        hasPerm("customer.delete") ? h("button", { class: "btn btn-ghost btn-sm", style: "color:#dc2626", onclick: bulkDelete }, t("delete")) : null)),
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("table", {},
        h("thead", {}, h("tr", {},
          h("th", { style: "width:36px" }, selectAll),
          [t("name"), t("domains"), t("linked_partner"), t("version"), t("service_start"), t("service_end"), t("contact_email"), t("actions")].map(c => h("th", {}, c)))),
        tbody))));
  doLoad();
  return { title: "", body };
}

/** Customers and partners are both "registered users" -- the tabs switch between them. */
function registeredTabs(active) {
  if (!hasPerm("partner.view") && !hasPerm("customer.view")) return null;
  const tabs = [];
  if (hasPerm("customer.view")) tabs.push(["customers", "tab_customers"]);
  if (hasPerm("partner.view")) tabs.push(["partners", "tab_partners"]);
  return h("div", { class: "tabs" }, tabs.map(([key, label]) =>
    // a plain "tab" + " active" concatenation would yield the class "tabnull"
    h("a", { href: "#/" + key, class: active === key ? "tab active" : "tab" }, t(label))));
}

/** Partners (代理商): create / edit by clicking the name, search, single + bulk delete. */
async function partnersView() {
  if (!hasPerm("partner.view")) return noAccess();
  const tbody = h("tbody", {});
  const selected = new Set();
  let rows = [];

  const q = h("input", { placeholder: t("search_partner") });
  const qWrap = attachSuggest(q, async val => {
    const r = await api("/api/partners?q=" + encodeURIComponent(val));
    return (r.items || []).map(p => ({ ...p, label: p.name + (p.domains ? "  <" + p.domains + ">" : "") }));
  }, p => { q.value = p.name; doLoad(); });

  const selectAll = h("input", { type: "checkbox", onchange: e => {
    const on = e.target.checked;
    selected.clear();
    if (on) rows.forEach(p => selected.add(p.id));
    renderRows();
  } });

  function renderRows() {
    tbody.innerHTML = "";
    if (!rows.length) {
      tbody.append(h("tr", {}, h("td", { colspan: 7, class: "muted" }, t("no_results"))));
      return;
    }
    for (const p of rows) {
      const cb = h("input", { type: "checkbox", checked: selected.has(p.id) ? "checked" : null, onchange: e => {
        if (e.target.checked) selected.add(p.id); else selected.delete(p.id);
        selectAll.checked = rows.length > 0 && rows.every(x => selected.has(x.id));
      } });
      tbody.append(h("tr", {},
        h("td", {}, cb),
        h("td", {}, h("a", { href: "javascript:void(0)", onclick: () => openPartnerEditor(p, doLoad) }, p.name),
          p.description ? h("div", { class: "muted", style: "font-size:12px" }, p.description) : null),
        h("td", {}, p.domains || "-"),
        h("td", {}, String(p.customer_count || 0),
          (p.customers || []).length
            ? h("div", { class: "muted", style: "font-size:12px" }, p.customers.join(", ")) : null),
        h("td", {}, String(p.member_count || 0)),
        h("td", {}, p.contact_email || "-"),
        h("td", {}, h("button", { class: "btn btn-ghost btn-sm", style: "color:#dc2626", onclick: async () => {
          if (!confirm(t("confirm_delete") + " " + p.name)) return;
          try {
            const r = await api("/api/partners/" + p.id, { method: "DELETE" });
            toast(r.unlinked ? t("saved") + " · " + r.unlinked + " " + t("partner_delete_unlink") : t("saved"));
            doLoad();
          } catch (e) { toast(e.message, false); }
        } }, t("delete")))));
    }
  }

  async function doLoad() {
    try {
      const r = await api("/api/partners?q=" + encodeURIComponent((q.value || "").trim()));
      rows = r.items || [];
    } catch (e) { rows = []; toast(e.message, false); }
    selected.clear(); selectAll.checked = false;
    renderRows();
  }

  async function bulkDelete() {
    const ids = Array.from(selected);
    if (!ids.length) { toast(t("select_first"), false); return; }
    if (!confirm(t("confirm_delete") + " (" + ids.length + ")")) return;
    try {
      const r = await api("/api/partners/bulk", { method: "POST", body: JSON.stringify({ ids }) });
      toast(r.unlinked ? t("saved") + " · " + r.unlinked + " " + t("partner_delete_unlink") : t("saved"));
      doLoad();
    } catch (e) { toast(e.message, false); }
  }

  const body = h("div", {},
    h("h1", {}, t("registered_users")),
    registeredTabs("partners"),
    h("div", { class: "toolbar" },
      h("div", { class: "toolbar-left" }, qWrap),
      h("div", { class: "toolbar-right" },
        hasPerm("partner.create") ? h("button", { class: "btn btn-blue btn-sm", onclick: () => openPartnerEditor(null, doLoad) }, "+" + t("add_partner")) : null,
        hasPerm("partner.delete") ? h("button", { class: "btn btn-ghost btn-sm", style: "color:#dc2626", onclick: bulkDelete }, t("delete")) : null)),
    h("p", { class: "muted" }, t("partner_hint")),
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("table", {},
        h("thead", {}, h("tr", {},
          h("th", { style: "width:36px" }, selectAll),
          [t("partner_name"), t("domains"), t("partner_customers"), t("members"), t("contact_email"), t("actions")]
            .map(x => h("th", {}, x)))),
        tbody))));
  doLoad();
  return { title: "", body };
}

/** Create / edit one partner. Name + domains are mandatory, exactly like a customer. */
function openPartnerEditor(p, onDone) {
  const isNew = !p;
  const name = h("input", { value: p ? (p.name || "") : "", placeholder: t("partner_name") });
  const domains = h("input", { value: p ? (p.domains || "") : "", placeholder: t("domains") });
  const contact = h("input", { type: "email", value: p ? (p.contact_email || "") : "", placeholder: t("contact_email") });
  const desc = h("input", { value: p ? (p.description || "") : "", placeholder: t("group_desc") });

  const body = h("div", { class: "page" },
    h("div", { class: "grid-2" },
      h("label", { class: "field" }, h("span", { class: "muted" }, t("partner_name")), name),
      h("label", { class: "field" }, h("span", { class: "muted" }, t("domains")), domains)),
    h("div", { class: "grid-2" },
      h("label", { class: "field" }, h("span", { class: "muted" }, t("contact_email")), contact),
      h("label", { class: "field" }, h("span", { class: "muted" }, t("group_desc")), desc)),
    h("p", { class: "muted", style: "font-size:13px" }, t("partner_hint")),
    p ? h("div", { class: "card mb-2" }, h("div", { class: "card-body" },
      h("h3", {}, t("partner_customers") + " (" + (p.customer_count || 0) + ")"),
      (p.customers || []).length
        ? h("p", { class: "muted" }, p.customers.join(", "))
        : h("p", { class: "muted" }, t("no_results")),
      h("p", { class: "muted", style: "font-size:13px" }, t("auto_by_partner")))) : null,
    h("div", { class: "flex-between mt-2" },
      h("button", { class: "btn btn-blue", onclick: async () => {
        if (!name.value.trim()) { toast(t("name") + " *", false); return; }
        if (!domains.value.trim()) { toast(t("name") + " / " + t("domains") + " *", false); return; }
        const payload = { name: name.value.trim(), domains: domains.value.trim(),
                          contact_email: contact.value, description: desc.value };
        try {
          if (isNew) await api("/api/partners", { method: "POST", body: JSON.stringify(payload) });
          else await api("/api/partners/" + p.id, { method: "PUT", body: JSON.stringify(payload) });
          toast(t("saved")); closeModal(); onDone && onDone();
        } catch (e) {
          toast(e.message === "partner_name_taken" ? t("partner_name_taken") : e.message, false);
        }
      } }, t("save")),
      h("button", { class: "btn btn-ghost", onclick: closeModal }, t("cancel"))));

  showModal(isNew ? t("add_partner") : t("edit_partner"), body);
}

function meView() {
  const name = h("input", { value: state.user.display_name || "", placeholder: t("display_name") });
  const pw1 = h("input", { type: "password", placeholder: t("password") });
  const pw2 = h("input", { type: "password", placeholder: t("password") + " (new)" });
  const savePw = h("button", { class: "btn btn-ghost btn-sm", onclick: async () => {
    if (!pw1.value || !pw2.value) { toast("enter passwords", false); return; }
    const r = await fetch("/api/me/password", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ old: pw1.value, new: pw2.value }) });
    toast(r.ok ? "Password changed" : "failed", r.ok);
    pw1.value = ""; pw2.value = "";
  } }, t("save"));
  let totp = null;
  const totpEnableBtn = h("button", { class: "btn btn-ghost btn-sm", onclick: async () => {
    const r = await fetch("/api/me/totp/enable", { method: "POST", headers: authHeaders() });
    const d = await r.json();
    totp.textContent = "Secret: " + d.secret + "\nURI: " + d.uri + "\n\nScan with your TOTP app, then save.";
    totp.classList.remove("hidden");
  } }, "Enable TOTP");
  const totpDisableBtn = h("button", { class: "btn btn-ghost btn-sm", onclick: async () => {
    await fetch("/api/me/totp/disable", { method: "POST", headers: authHeaders() });
    toast("OK"); render();
  } }, "Disable TOTP");
  totp = h("pre", { class: "muted hidden" }, "");
  const roleText = (state.user.roles || []).join(", ");
  const grpText = (state.user.groups || []).join(", ");
  const perms = (state.user.permissions || []).length;
  const body = h("div", { class: "page-narrow" },
    h("h1", {}, t("my_account")),
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("div", { class: "flex-between mb-2" },
        h("div", {}, h("div", { class: "muted" }, t("email")), h("div", {}, state.user.email)),
        h("div", {}, h("div", { class: "muted" }, t("role")), h("div", {}, roleText),
          h("div", { class: "muted", style: "margin-top:6px" }, t("groups") + ": " + grpText),
          h("div", { class: "muted", style: "margin-top:6px" }, "Permissions: " + perms))),
      h("label", { class: "field" }, h("span", { class: "muted" }, t("display_name")), name),
      h("button", { class: "btn btn-ghost btn-sm", onclick: async () => {
        await fetch("/api/me/name", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ display_name: name.value }) });
        state.user.display_name = name.value; toast("OK");
      } }, t("save")),
      h("div", { class: "inline-field", style: "margin-top:12px" },
        h("span", { class: "muted" }, t("password")), pw1, pw2, savePw),
      h("div", { class: "inline-field", style: "margin-top:12px" },
        state.user.totp_enabled ? totpDisableBtn : totpEnableBtn, totp,
    ))));
  return { title: "", body };
}

/** Wraps an input with a fuzzy suggestion dropdown. */
function attachSuggest(input, loader, onPick) {
  const box = h("div", { class: "suggest hidden" });
  const wrap = h("div", { class: "suggest-wrap" }, input, box);
  let timer;
  const hide = () => box.classList.add("hidden");
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const val = (input.value || "").trim();
      if (!val) return hide();
      let items = [];
      try { items = await loader(val); } catch (e) { return hide(); }
      box.innerHTML = "";
      if (!items.length) return hide();
      for (const it of items.slice(0, 12)) {
        box.append(h("div", { class: "suggest-item", onmousedown: () => { hide(); onPick(it); } }, it.label));
      }
      box.classList.remove("hidden");
    }, 180);
  });
  input.addEventListener("blur", () => setTimeout(hide, 250));
  input.addEventListener("keydown", e => { if (e.key === "Escape") hide(); });
  return wrap;
}

function noAccess() {
  return { title: "", body: h("div", { class: "card" }, h("div", { class: "card-body" }, "No access")) };
}

async function loadRoleOptions(select, includeEmpty, selected) {
  try {
    const r = await api("/api/admin/roles");
    select.innerHTML = "";
    if (includeEmpty) select.append(h("option", { value: "" }, "- " + t("role") + " -"));
    for (const x of (r.items || [])) {
      select.append(h("option", { value: x.name, selected: selected && selected.includes(x.name) ? "selected" : null }, x.name));
    }
  } catch (e) {}
}

async function loadGroupOptions(select, selected) {
  try {
    const r = await api("/api/admin/groups");
    select.innerHTML = "";
    select.append(h("option", { value: "" }, "- " + t("groups") + " -"));
    for (const g of (r.items || [])) {
      select.append(h("option", { value: g.id, selected: selected && selected.includes(g.id) ? "selected" : null }, g.name));
    }
  } catch (e) {}
}

/** Create / edit user modal. */
function openUserEditor(user, onDone) {
  const isNew = !user;
  const email = h("input", { type: "email", value: user ? user.email : "", placeholder: t("email"), style: "width:100%" });
  const name = h("input", { value: user ? (user.display_name || "") : "", placeholder: t("display_name"), style: "width:100%" });
  const pw = h("input", { type: "password", placeholder: isNew ? t("password") : t("new_password"), style: "width:100%" });
  const roleSel = h("select", { style: "width:100%" });
  const statusSel = h("select", { style: "width:100%" },
    h("option", { value: "active", selected: !user || user.status === "active" ? "selected" : null }, t("active")),
    h("option", { value: "disabled", selected: user && user.status === "disabled" ? "selected" : null }, t("disabled")));
  loadRoleOptions(roleSel, true, user ? (user.roles || []) : []);

  const body = h("div", { class: "page" },
    h("label", { class: "field" }, h("span", { class: "muted" }, t("email")), email),
    h("label", { class: "field" }, h("span", { class: "muted" }, t("display_name")), name),
    h("label", { class: "field" }, h("span", { class: "muted" }, isNew ? t("password") : t("new_password")), pw),
    h("div", { class: "grid-2" },
      h("label", { class: "field" }, h("span", { class: "muted" }, t("role")), roleSel),
      h("label", { class: "field" }, h("span", { class: "muted" }, t("status")), statusSel)),
    h("p", { class: "muted", style: "font-size:13px" }, t("auto_group_hint")),
    h("div", { class: "flex-between mt-2" },
      h("button", { class: "btn btn-blue", onclick: async () => {
        if (!email.value) { toast(t("email") + " *", false); return; }
        const payload = { email: email.value, display_name: name.value, status: statusSel.value,
                          roles: roleSel.value ? [roleSel.value] : [] };
        if (pw.value) payload.password = pw.value;
        try {
          if (isNew) await api("/api/admin/users", { method: "POST", body: JSON.stringify(payload) });
          else await api("/api/admin/users/" + user.id, { method: "PUT", body: JSON.stringify(payload) });
          toast(t("saved")); closeModal(); onDone && onDone();
        } catch (e) { toast(e.message, false); }
      } }, t("save")),
      h("button", { class: "btn btn-ghost", onclick: closeModal }, t("cancel"))));
  showModal(isNew ? t("add_user") : t("edit_user"), body);
}

async function adminUsers() {
  if (!hasPerm("user.manage")) return noAccess();
  const tbody = h("tbody", {});
  const selected = new Set();
  let rows = [];

  const q = h("input", { placeholder: t("search_user"), style: "width:100%" });
  const qWrap = attachSuggest(q, async val => {
    const r = await api("/api/admin/users?q=" + encodeURIComponent(val));
    return (r.items || []).map(u => ({ ...u, label: (u.display_name || "") + "  <" + u.email + ">" }));
  }, u => { q.value = u.email; doLoad(); });

  const selectAll = h("input", { type: "checkbox", onchange: e => {
    const on = e.target.checked;
    selected.clear();
    if (on) rows.forEach(u => selected.add(u.id));
    renderRows();
  } });

  function renderRows() {
    tbody.innerHTML = "";
    if (!rows.length) {
      tbody.append(h("tr", {}, h("td", { colspan: 5, class: "muted" }, t("no_results"))));
      return;
    }
    for (const u of rows) {
      const cb = h("input", { type: "checkbox", checked: selected.has(u.id) ? "checked" : null, onchange: e => {
        if (e.target.checked) selected.add(u.id); else selected.delete(u.id);
        selectAll.checked = rows.length > 0 && rows.every(x => selected.has(x.id));
      } });
      const disabled = u.status === "disabled";
      tbody.append(h("tr", {},
        h("td", {}, cb),
        h("td", {},
          h("a", { href: "javascript:void(0)", onclick: () => openUserEditor(u, doLoad) },
            u.display_name || u.email),
          h("div", { class: "muted", style: "font-size:12px" }, u.email)),
        h("td", {}, (u.roles || []).join(", ") || "-"),
        h("td", {}, h("span", { class: "tag " + (disabled ? "closed" : "open") }, disabled ? t("disabled") : t("active"))),
        h("td", {},
          h("button", { class: "btn btn-ghost btn-sm", onclick: async () => {
            await api("/api/admin/users/" + u.id, { method: "PUT", body: JSON.stringify({ status: disabled ? "active" : "disabled" }) });
            toast(t("saved")); doLoad();
          } }, disabled ? t("enable") : t("disable")),
          hasPerm("user.reset_totp") ? h("button", { class: "btn btn-ghost btn-sm", style: "margin-left:4px", onclick: async () => {
            await api("/api/admin/users/" + u.id + "/reset_totp", { method: "POST" }); toast("OK");
          } }, t("reset_totp")) : null,
          h("button", { class: "btn btn-ghost btn-sm", style: "margin-left:4px;color:#dc2626", onclick: async () => {
            if (!confirm(t("confirm_delete") + " " + u.email)) return;
            await api("/api/admin/users/" + u.id, { method: "DELETE" }); toast(t("saved")); doLoad();
          } }, t("delete")))));
    }
  }

  async function doLoad() {
    const r = await api("/api/admin/users?q=" + encodeURIComponent((q.value || "").trim()));
    rows = r.items || [];
    selected.clear();
    selectAll.checked = false;
    renderRows();
  }

  async function bulk(action, extra) {
    const ids = Array.from(selected);
    if (!ids.length) { toast(t("select_first"), false); return; }
    if (action === "delete" && !confirm(t("confirm_delete") + " (" + ids.length + ")")) return;
    try {
      await api("/api/admin/users/bulk", { method: "POST", body: JSON.stringify(Object.assign({ ids, action }, extra || {})) });
      toast(t("saved")); doLoad();
    } catch (e) { toast(e.message, false); }
  }

  function openBulkEdit() {
    const roleSel = h("select", { style: "width:100%" });
    const statusSel = h("select", { style: "width:100%" },
      h("option", { value: "" }, "- " + t("no_change") + " -"),
      h("option", { value: "active" }, t("active")),
      h("option", { value: "disabled" }, t("disabled")));
    loadRoleOptions(roleSel, true, []);
    const body = h("div", { class: "page" },
      h("p", { class: "muted" }, t("bulk_update_hint") + " (" + selected.size + ")"),
      h("label", { class: "field" }, h("span", { class: "muted" }, t("role")), roleSel),
      h("label", { class: "field" }, h("span", { class: "muted" }, t("status")), statusSel),
      h("div", { class: "flex-between mt-2" },
        h("button", { class: "btn btn-blue", onclick: async () => {
          const extra = {};
          if (roleSel.value) extra.roles = [roleSel.value];
          if (statusSel.value) extra.status = statusSel.value;
          closeModal();
          await bulk("update", extra);
        } }, t("save")),
        h("button", { class: "btn btn-ghost", onclick: closeModal }, t("cancel"))));
    showModal(t("bulk_update"), body);
  }

  const importInput = h("input", { type: "file", accept: ".csv", style: "display:none", onchange: async e => {
    const f = e.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("file", f);
    try {
      const r = await apiForm("/api/admin/users/import", fd);
      toast(t("imported") + ": " + r.created + " / " + r.updated
        + ((r.skipped || []).length ? " · " + t("import_skipped") + ": " + (r.skipped || []).join(", ") : ""), !(r.skipped || []).length);
      doLoad();
    } catch (err) { toast(err.message, false); }
    e.target.value = "";
  } });

  const body = h("div", {},
    h("div", { class: "toolbar" },
      h("div", { class: "toolbar-left" }, qWrap),
      h("div", { class: "toolbar-right" },
        h("label", { class: "btn btn-ghost btn-sm" }, t("bulk_import"), importInput,
          h("a", { href: "/api/admin/users/template.csv", download: true, style: "margin-left:6px;color:var(--blue)" }, "↓ " + t("download_template"))),
        h("button", { class: "btn btn-blue btn-sm", onclick: () => openUserEditor(null, doLoad) }, "+" + t("add_user")),
        h("button", { class: "btn btn-ghost btn-sm", onclick: openBulkEdit }, t("bulk_update")),
        h("button", { class: "btn btn-ghost btn-sm", onclick: () => bulk("disable") }, t("disable")),
        h("button", { class: "btn btn-ghost btn-sm", style: "color:#dc2626", onclick: () => bulk("delete") }, t("delete")))),
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("table", {},
        h("thead", {}, h("tr", {},
          h("th", { style: "width:36px" }, selectAll),
          [t("name"), t("role"), t("status"), t("actions")].map(c => h("th", {}, c)))),
        tbody))));
  doLoad();
  return { title: "", body };
}

/** Role page: every role is listed, click a name to edit it.
 *  Built-in roles are read-only; only custom roles get a delete button. */
async function adminRoles() {
  if (!hasPerm("role.manage")) return noAccess();
  let roles = { items: [] }, matrix = { menu: [] };
  try {
    const [r1, r2] = await Promise.all([api("/api/admin/roles"), api("/api/admin/perm_matrix")]);
    roles = r1; matrix = r2;
  } catch (e) { toast(e.message, false); }
  const menu = matrix.menu || [];
  const tbody = h("tbody", {});

  for (const r of (roles.items || [])) {
    tbody.append(h("tr", {},
      h("td", {}, h("a", { href: "javascript:void(0)", onclick: () => openRoleEditor(r, menu) }, r.name)),
      h("td", {}, r.description || "-"),
      h("td", {}, r.builtin ? h("span", { class: "tag internal" }, t("builtin"))
                            : h("span", { class: "muted" }, t("custom_role"))),
      h("td", {}, String((r.permissions || []).length)),
      h("td", {}, r.builtin
        ? h("span", { class: "muted" }, "\u2014")
        : h("button", { class: "btn btn-ghost btn-sm", style: "color:#dc2626", onclick: async () => {
            if (!confirm(t("confirm_delete") + " " + r.name)) return;
            try {
              await api("/api/admin/roles/" + r.id, { method: "DELETE" });
              toast(t("saved")); render();
            } catch (e) { toast(e.message, false); }
          } }, t("delete")))));
  }
  if (!(roles.items || []).length) {
    tbody.append(h("tr", {}, h("td", { colspan: 5, class: "muted" }, t("no_results"))));
  }

  const body = h("div", {},
    h("div", { class: "toolbar" },
      h("div", { class: "toolbar-left" }, h("h1", { style: "margin:0" }, t("roles"))),
      h("div", { class: "toolbar-right" },
        h("button", { class: "btn btn-blue btn-sm", onclick: () => openRoleEditor(null, menu) },
          "+" + t("new_role")))),
    h("p", { class: "muted" }, t("builtin_role_hint")),
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("table", {},
        h("thead", {}, h("tr", {},
          [t("name"), t("description"), t("status"), t("permission"), t("actions")].map(x => h("th", {}, x)))),
        tbody))));
  return { title: "", body };
}

/** Create / edit a role. Access is granted per left-hand menu entry: none | read | edit. */
function openRoleEditor(role, menu) {
  const isNew = !role;
  const builtin = !!(role && role.builtin);
  const ro = builtin ? true : null;
  const name = h("input", { value: role ? role.name : "", placeholder: t("role_name"),
    style: "width:100%", disabled: ro });
  const desc = h("input", { value: role ? (role.description || "") : "", placeholder: t("description"),
    style: "width:100%" });
  const levels = Object.assign({}, (role && role.levels) || {});
  const selects = {};
  const rows = (menu || []).map(item => {
    if (!item.read.length && !item.edit.length) {
      return h("tr", {}, h("td", {}, t("menu_" + item.key)),
        h("td", { class: "muted" }, t("dashboard_always")));
    }
    const sel = h("select", { style: "max-width:240px", disabled: ro },
      [["none", t("level_none")], ["read", t("level_read")], ["edit", t("level_edit")]]
        .map(([v, label]) => h("option", {
          value: v, selected: (levels[item.key] || "none") === v ? "selected" : null }, label)));
    selects[item.key] = sel;
    return h("tr", {}, h("td", {}, t("menu_" + item.key)), h("td", {}, sel));
  });

  const body = h("div", { class: "page", style: "max-height:70vh;overflow:auto" },
    h("label", { class: "field" }, h("span", { class: "muted" }, t("role_name")), name),
    h("label", { class: "field" }, h("span", { class: "muted" }, t("description")), desc),
    h("h3", { style: "margin-top:12px" }, t("level")),
    h("p", { class: "muted" }, t("menu_perm_hint")),
    h("table", {},
      h("thead", {}, h("tr", {}, [t("menu_entry"), t("level")].map(x => h("th", {}, x)))),
      h("tbody", {}, rows)),
    builtin ? h("p", { class: "muted", style: "margin-top:10px" }, t("builtin_role_hint")) : null,
    h("div", { class: "flex-between mt-2" },
      builtin ? h("span", {}) :
        h("button", { class: "btn btn-blue", onclick: async () => {
          if (!name.value.trim()) { toast(t("role_name") + " *", false); return; }
          const lv = {};
          for (const k of Object.keys(selects)) lv[k] = selects[k].value;
          const payload = { name: name.value.trim(), description: desc.value, levels: lv };
          try {
            if (isNew) await api("/api/admin/roles", { method: "POST", body: JSON.stringify(payload) });
            else await api("/api/admin/roles/" + role.id, { method: "PUT", body: JSON.stringify(payload) });
            toast(t("saved")); closeModal(); render();
          } catch (e) {
            toast(e.message === "role_name_taken" ? t("role_name_taken") : e.message, false);
          }
        } }, t("save")),
      h("button", { class: "btn btn-ghost", onclick: closeModal }, t("cancel"))));
  showModal(isNew ? t("new_role") : t("edit_role"), body);
}

/** Type chip for a user group. */
function groupKindTag(g) {
  const kind = g.kind || (g.partner_id ? "partner" : g.customer_id ? "customer" : "manual");
  const cls = kind === "internal" ? "internal" : kind === "customer" ? "registered"
            : kind === "partner" ? "partner" : "usergroup";
  const label = kind === "internal" ? t("kind_internal")
              : kind === "customer" ? t("kind_customer")
              : kind === "partner" ? t("kind_partner") : t("kind_manual");
  return h("span", { class: "tag " + cls }, label);
}

/** A group is system-owned when it is bound to a customer / partner or is the internal group. */
function isBuiltinGroup(g) {
  return !!(g && (g.builtin || g.customer_id || g.partner_id || g.kind === "internal"
                  || g.kind === "customer" || g.kind === "partner"));
}

/** User groups: every group is listed, click a name to open the editor. */
async function adminGroups() {
  if (!hasPerm("group.manage")) return noAccess();
  let items = [];
  try { const r = await api("/api/admin/groups"); items = r.items || []; }
  catch (e) { toast(e.message, false); }

  const tbody = h("tbody", {});
  for (const g of items) {
    tbody.append(h("tr", {},
      h("td", {}, h("a", { href: "javascript:void(0)", onclick: () => openGroupEditor(g, render) },
          g.display_name || g.name),
        g.description ? h("div", { class: "muted", style: "font-size:12px" }, g.description) : null),
      h("td", {}, groupKindTag(g)),
      h("td", { class: "muted" }, g.kind === "customer" || g.kind === "partner" ? (g.domains || "-")
                          : g.kind === "internal" ? t("auto_members") : "\u2014"),
      h("td", {}, String(g.member_count)),
      h("td", { class: "muted", style: "font-size:12px" },
        (g.grants || []).length ? g.grants.join(", ") : t("no_grants")),
      h("td", {}, isBuiltinGroup(g)
        ? h("span", { class: "muted" }, "\u2014")
        : h("button", { class: "btn btn-ghost btn-sm", style: "color:#dc2626", onclick: async () => {
            if (!confirm(t("confirm_delete") + " " + g.name)) return;
            try { await api("/api/admin/groups/" + g.id, { method: "DELETE" }); toast(t("saved")); render(); }
            catch (e) { toast(e.message, false); }
          } }, t("delete")))));
  }
  if (!items.length) tbody.append(h("tr", {}, h("td", { colspan: 6, class: "muted" }, t("no_results"))));

  const body = h("div", { class: "page" },
    h("div", { class: "toolbar" },
      h("div", { class: "toolbar-left" }, h("h1", { style: "margin:0" }, t("groups"))),
      h("div", { class: "toolbar-right" },
        h("button", { class: "btn btn-blue btn-sm", onclick: () => openGroupEditor(null, render) },
          "+" + t("add_group")))),
    h("p", { class: "muted" }, t("groups_hint")),
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("table", {},
        h("thead", {}, h("tr", {},
          [t("name"), t("group_kind"), t("auto_members"), t("members"), t("grants"), t("actions")]
            .map(x => h("th", {}, x)))),
        tbody))));
  return { title: "", body };
}

/** Create / edit one group: name, description and its member list. */
function openGroupEditor(g, onDone) {
  const isNew = !g;
  const builtin = isBuiltinGroup(g);
  const name = h("input", { value: g ? g.name : "", placeholder: t("group_name"),
    disabled: builtin ? true : null });
  const desc = h("input", { value: g ? (g.description || "") : "", placeholder: t("group_desc") });

  const memBox = h("div", {});
  async function loadMembers() {
    memBox.innerHTML = "";
    try {
      const r = await api("/api/admin/groups/" + g.id + "/members");
      const list = r.items || [];
      if (!list.length) { memBox.append(h("p", { class: "muted" }, t("no_results"))); return; }
      memBox.append(h("table", {},
        h("thead", {}, h("tr", {}, [t("name"), t("role"), t("auto_members"), t("actions")]
          .map(x => h("th", {}, x)))),
        h("tbody", {}, list.map(u => h("tr", {},
          h("td", {}, h("b", {}, u.display_name || u.email),
            h("div", { class: "muted", style: "font-size:12px" }, u.email)),
          h("td", {}, (u.roles || []).join(", ") || "-"),
          h("td", {}, h("span", { class: "tag " + (u.auto ? "internal" : "usergroup") },
            u.auto ? t("auto_member") : t("manual_member"))),
          h("td", {}, h("button", { class: "btn btn-ghost btn-sm", style: "color:#dc2626", onclick: async () => {
            if (!confirm(t("remove") + " " + u.email + "?")) return;
            try {
              await api("/api/admin/groups/" + g.id + "/members/" + u.id, { method: "DELETE" });
              toast(t("saved")); loadMembers(); onDone && onDone();
            } catch (e) { toast(e.message, false); }
          } }, t("remove"))))))));
    } catch (e) { memBox.append(h("p", { class: "muted" }, e.message)); }
  }

  const emailInp = h("input", { type: "email", placeholder: t("member_email") });
  const addBtn = h("button", { class: "btn btn-ghost btn-sm", onclick: async () => {
    const email = emailInp.value.trim();
    if (!email) return;
    try {
      await api("/api/admin/groups/" + g.id + "/members", { method: "POST", body: JSON.stringify({ email }) });
      emailInp.value = ""; toast(t("saved")); loadMembers(); onDone && onDone();
    } catch (e) {
      toast(e.message === "user_not_found" ? t("user_not_found") : e.message, false);
    }
  } }, "+" + t("add_member"));

  const body = h("div", { class: "page" },
    h("label", { class: "field" }, h("span", { class: "muted" }, t("group_name")), name),
    h("label", { class: "field" }, h("span", { class: "muted" }, t("group_desc")), desc),
    builtin ? h("p", { class: "muted", style: "font-size:13px" }, t("builtin_group_hint")) : null,
    g ? h("div", { class: "card mb-2" }, h("div", { class: "card-body" },
      h("h3", {}, t("auto_members")),
      h("p", { class: "muted" },
        g.kind === "internal" ? t("auto_by_internal")
        : g.kind === "customer" ? t("auto_by_customer")
        : g.kind === "partner" ? t("auto_by_partner") : t("auto_manual_hint")),
      (g.kind === "customer" || g.kind === "partner") ? h("p", { class: "muted" },
        t(g.kind === "partner" ? "linked_partner" : "linked_customer") + ": "
        + (g.partner_name || g.customer_name || "-")
        + " · " + t("linked_domains") + ": " + (g.domains || "-")) : null,
      h("h3", { style: "margin-top:12px" }, t("grants")),
      h("p", { class: "muted" }, (g.grants || []).length ? g.grants.join(", ") : t("no_grants")))) : null,
    g ? h("div", { class: "card" }, h("div", { class: "card-body" },
      h("h3", {}, t("members") + " (" + (g.member_count || 0) + ")"),
      memBox,
      h("div", { class: "inline-field", style: "margin-top:10px" },
        h("div", { class: "suggest-wrap" }, emailInp), addBtn))) : null,
    h("div", { class: "flex-between mt-2" },
      h("button", { class: "btn btn-blue", onclick: async () => {
        if (!name.value.trim()) { toast(t("name_required"), false); return; }
        const payload = { description: desc.value };
        if (!builtin) payload.name = name.value.trim();
        try {
          if (isNew) await api("/api/admin/groups", { method: "POST", body: JSON.stringify(payload) });
          else await api("/api/admin/groups/" + g.id, { method: "PUT", body: JSON.stringify(payload) });
          toast(t("saved")); closeModal(); onDone && onDone();
        } catch (e) {
          toast(e.message === "group_name_taken" ? t("group_name_taken") : e.message, false);
        }
      } }, t("save")),
      h("button", { class: "btn btn-ghost", onclick: closeModal }, t("cancel"))));

  showModal(isNew ? t("add_group") : t("edit_group"), body);
  if (g) loadMembers();
}

async function adminSite() {
  if (!hasPerm("settings.mail")) return noAccess();
  let s;
  try { s = await api("/api/admin/site"); } catch (e) { return { title: "", body: h("div", {}, t("no_results")) }; }
  const modules = (s.modules || []).slice();
  // Internal domains moved to their own settings page (#/admin/internal_domains).

  // --- modules CRUD ---
  const modList = h("div", {});
  const modInput = h("input", { placeholder: "PAC" });
  function renderMods() {
    modList.innerHTML = "";
    if (!modules.length) modList.append(h("div", { class: "muted" }, t("no_results")));
    modules.forEach((m, i) => {
      modList.append(h("div", { class: "flex-between", style: "padding:6px 0;border-bottom:1px solid var(--border)" },
        h("span", {}, m),
        h("button", { class: "btn btn-ghost btn-sm", style: "color:#dc2626", onclick: () => {
          modules.splice(i, 1); renderMods();
        } }, t("delete"))));
    });
  }
  renderMods();

  const timeoutInp = h("input", { type: "number", min: 0, value: String(s.session_timeout || 0), style: "width:120px" });
  const maxLifeInp = h("input", { type: "number", min: 0,
    value: String(s.session_max_lifetime == null ? 1440 : s.session_max_lifetime), style: "width:120px" });
  const themeSel = h("select", {},
    [["light", t("theme_light")], ["dark", t("theme_dark")], ["system", t("theme_system")]]
      .map(([v, label]) => h("option", { value: v, selected: (s.theme || "light") === v ? "selected" : null }, label)));

  const body = h("div", { class: "page-narrow" },
    settingsTabs("site"),
    h("h1", {}, t("site_settings")),
    h("div", { class: "card mb-2" }, h("div", { class: "card-body" },
      h("h3", {}, t("session_timeout")),
      h("p", { class: "muted" }, t("session_timeout_hint")),
      h("div", { class: "inline-field" }, timeoutInp,
        h("span", { class: "muted" }, t("minutes"))),
      h("h3", { style: "margin-top:14px" }, t("session_max_lifetime")),
      h("p", { class: "muted" }, t("session_max_lifetime_hint")),
      h("div", { class: "inline-field" }, maxLifeInp,
        h("span", { class: "muted" }, t("minutes"))),
      h("h3", { style: "margin-top:14px" }, t("page_theme")),
      h("div", { class: "inline-field" }, themeSel))),
    h("div", { class: "card mb-2" }, h("div", { class: "card-body" },
      h("h3", {}, t("modules")),
      h("p", { class: "muted" }, t("modules_hint")),
      modList,
      h("div", { class: "inline-field", style: "margin-top:8px" }, modInput,
        h("button", { class: "btn btn-ghost btn-sm", onclick: () => {
          const v = modInput.value.trim(); if (!v) return;
          if (!modules.includes(v)) modules.push(v);
          modInput.value = ""; renderMods();
        } }, "+" + t("add"))))),
    // Internal domains now live on their own settings page; this page keeps a
    // pointer so the old entry point is not a dead end.
    h("div", { class: "card mb-2" }, h("div", { class: "card-body" },
      h("h3", {}, t("internal_domains")),
      h("p", { class: "muted" }, t("internal_domains_hint")),
      h("a", { class: "btn btn-ghost btn-sm", href: "#/admin/internal_domains" },
        t("internal_domains") + " ↗"))),
    h("div", { style: "display:flex;gap:8px" },
      h("button", { class: "btn btn-blue", onclick: async () => {
        try {
          await api("/api/admin/site", { method: "POST", body: JSON.stringify({
            modules,
            session_timeout: Number(timeoutInp.value || 0),
            session_max_lifetime: Number(maxLifeInp.value || 0),
            theme: themeSel.value }) });
          applyTheme(themeSel.value);
          toast(t("saved"));
          const m = await api("/api/meta");
          state.meta = m;
          state.modules = m.modules; state.deployTypes = m.deploy_types;
          startIdleWatchdog();
          render();
        } catch (e) { toast(e.message, false); }
      } }, t("save"))));
  return { title: "", body };
}

/** Shared sub-navigation for the Settings section. */
function settingsTabs(active) {
  const tabs = [
    ["site", "site_settings"],
    ["welcome", "welcome_page"],
    ["company", "company_info"],
    ["internal_domains", "internal_domains"],
    ["domains", "bind_domains"],
    ["settings", "mail_settings"],
  ];
  return h("div", { class: "tabs" },
    tabs.map(([key, label]) => h("a", {
      // inactive tabs used to get class "tabnull" and render as bare links
      href: "#/admin/" + key, class: active === key ? "tab active" : "tab" }, t(label))));
}

async function loadSiteSettings() {
  try { return await api("/api/admin/site"); }
  catch (e) { toast(e.message, false); return null; }
}

/** Settings > Welcome page: Markdown shown on the public landing page. */
async function adminWelcome() {
  if (!hasPerm("settings.mail")) return noAccess();
  const s = await loadSiteSettings();
  if (!s) return noAccess();
  const ta = h("textarea", { rows: 16, class: "kb-editor-body" });
  ta.value = s.welcome_md || "";
  const previewBody = h("div", { class: "card-body markdown" });
  const preview = h("div", { class: "card" }, previewBody);
  const paint = () => { previewBody.innerHTML = mdToHtml(ta.value); };
  ta.addEventListener("input", paint);
  setTimeout(paint, 0);

  const body = h("div", { class: "page" },
    settingsTabs("welcome"),
    h("div", { class: "toolbar" },
      h("div", { class: "toolbar-left" }, h("h1", { style: "margin:0" }, t("welcome_page"))),
      h("div", { class: "toolbar-right" },
        h("button", { class: "btn btn-blue btn-sm", onclick: async () => {
          try {
            await api("/api/admin/site", { method: "POST", body: JSON.stringify({ welcome_md: ta.value }) });
            await loadBrand();
            toast(t("saved"));
          } catch (e) { toast(e.message, false); }
        } }, t("save")))),
    h("p", { class: "muted" }, t("welcome_page_hint")),
    h("div", { class: "card mb-2" }, h("div", { class: "card-body" }, ta)),
    h("h3", {}, t("welcome_preview")),
    preview);
  return { title: "", body };
}

/** Settings > Company info: display name + logo upload. */
async function adminCompany() {
  if (!hasPerm("settings.mail")) return noAccess();
  const s = await loadSiteSettings();
  if (!s) return noAccess();
  const nameInp = h("input", { value: s.company_name || "", placeholder: t("company_name"), style: "width:100%" });
  const logoImg = h("img", { src: s.company_logo || "", class: "logo-preview", alt: "" });
  const fileInp = h("input", { type: "file", accept: "image/*", style: "display:none", onchange: async e => {
    const f = e.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("file", f);
    try {
      const r = await apiForm("/api/admin/site/logo", fd);
      logoImg.src = (r.logo || "") + "?v=" + Date.now();
      await loadBrand();
      toast(t("saved"));
    } catch (err) { toast(err.message, false); }
    e.target.value = "";
  } });

  const body = h("div", { class: "page-narrow" },
    settingsTabs("company"),
    h("h1", {}, t("company_info")),
    h("div", { class: "card mb-2" }, h("div", { class: "card-body" },
      h("h3", {}, t("company_name")),
      nameInp,
      h("button", { class: "btn btn-blue btn-sm", style: "margin-top:10px", onclick: async () => {
        try {
          await api("/api/admin/site", { method: "POST", body: JSON.stringify({ company_name: nameInp.value }) });
          await loadBrand();
          toast(t("saved"));
        } catch (e) { toast(e.message, false); }
      } }, t("save")),
      h("h3", { style: "margin-top:18px" }, t("company_logo")),
      s.company_logo ? h("div", { style: "margin-bottom:10px" },
        h("div", { class: "muted" }, t("logo_current")), logoImg) : null,
      h("div", { class: "inline-field", style: "margin-top:2px" },
        h("label", { class: "btn btn-ghost btn-sm" }, t("upload_logo"), fileInp),
        h("span", { class: "muted", style: "font-size:13px" }, t("logo_hint"))))));
  return { title: "", body };
}

/** Settings > Internal domains: the suffixes that make an account staff.
 *
 * They are the rule behind the internal group (membership) and behind the
 * assignee picker on a ticket: only an internal user may be handed a ticket.
 */
async function adminInternalDomains() {
  if (!hasPerm("settings.mail")) return noAccess();
  let s;
  try { s = await api("/api/admin/internal_domains"); } catch (e) { return noAccess(); }
  const domains = (s.domains || []).slice();

  const list = h("div", {});
  const inp = h("input", { placeholder: "rankez.local", style: "flex:1" });
  let paintSite = () => {};
  const paint = () => {
    paintSite();
    list.innerHTML = "";
    if (!domains.length) list.append(h("div", { class: "muted" }, "\u2014"));
    domains.forEach((d, i) => list.append(h("div", {
      class: "flex-between", style: "padding:6px 0;border-bottom:1px solid var(--border)" },
      h("span", {}, d),
      h("button", { class: "btn btn-ghost btn-sm", style: "color:#dc2626",
        onclick: () => { domains.splice(i, 1); paint(); } }, t("delete")))));
  };
  paint();

  // Domains this installation recognised as its own (Settings > Mail + the
  // staff accounts that already exist). They may register even when they are
  // not listed above; the "+" promotes one to a real internal domain.
  const detected = (s.site_domains || []).slice();
  const siteCard = [];
  if (detected.length) {
    const siteBox = h("div", {});
    paintSite = () => {
      siteBox.innerHTML = "";
      const left = detected.filter(d => !domains.includes(d));
      if (!left.length) { siteBox.append(h("div", { class: "muted" }, "\u2014")); return; }
      left.forEach(d => siteBox.append(h("div", {
        class: "flex-between", style: "padding:6px 0;border-bottom:1px solid var(--border)" },
        h("span", {}, d),
        h("button", { class: "btn btn-ghost btn-sm", onclick: () => {
          domains.push(d); paint();
        } }, "+" + t("add")))));
    };
    paintSite();
    siteCard.push(h("div", { class: "card mb-2" }, h("div", { class: "card-body" },
      h("h3", {}, t("internal_domains_detected")),
      h("p", { class: "muted" }, t("internal_domains_detected_hint")),
      siteBox)));
  }

  // Who the current rule already covers -- the assignee candidates.
  const usersTitle = h("h3", {});
  const userBox = h("div", {});
  const paintUsers = (users) => {
    usersTitle.textContent = t("internal_domains_users") + " (" + users.length + ")";
    userBox.innerHTML = "";
    if (!users.length) { userBox.append(h("div", { class: "muted" }, t("internal_domains_none"))); return; }
    users.forEach(u => userBox.append(h("div", {
      class: "flex-between", style: "padding:6px 0;border-bottom:1px solid var(--border)" },
      h("span", {}, u.display_name || u.email),
      h("span", { class: "muted" }, u.email))));
  };
  paintUsers(s.users || []);

  const body = h("div", { class: "page-narrow" },
    settingsTabs("internal_domains"),
    h("h1", {}, t("internal_domains")),
    h("p", { class: "muted" }, t("internal_domains_hint")),
    h("div", { class: "card mb-2" }, h("div", { class: "card-body" },
      list,
      h("div", { class: "inline-field", style: "margin-top:12px" }, inp,
        h("button", { class: "btn btn-ghost btn-sm", onclick: () => {
          const v = inp.value.trim().toLowerCase()
            .replace(/^https?:\/\//, "").replace(/\/.*$/, "").replace(/^[@.]+/, "").split(":")[0];
          if (!v) return;
          if (!domains.includes(v)) domains.push(v);
          inp.value = ""; paint();
        } }, "+" + t("add"))),
      h("button", { class: "btn btn-blue", style: "margin-top:12px", onclick: async () => {
        try {
          const r = await api("/api/admin/internal_domains", { method: "POST",
            body: JSON.stringify({ domains }) });
          paintUsers(r.users || []);
          toast(t("saved"));
        } catch (e) { toast(e.message, false); }
      } }, t("save")))),
    ...siteCard,
    h("div", { class: "card" }, h("div", { class: "card-body" },
      usersTitle,
      h("p", { class: "muted" }, t("internal_domains_users_hint")),
      userBox)));
  return { title: "", body };
}

/** Settings > Bound domains: allow-list of hosts; empty = allow everything. */
async function adminDomains() {
  if (!hasPerm("settings.mail")) return noAccess();
  const s = await loadSiteSettings();
  if (!s) return noAccess();
  const hosts = (s.allowed_hosts || []).slice();
  const list = h("div", {});
  const inp = h("input", { placeholder: t("domain_placeholder"), style: "flex:1" });
  const paint = () => {
    list.innerHTML = "";
    if (!hosts.length) list.append(h("div", { class: "muted" }, "\u2014"));
    hosts.forEach((x, i) => list.append(h("div", {
      class: "flex-between", style: "padding:6px 0;border-bottom:1px solid var(--border)" },
      h("span", {}, x),
      h("button", { class: "btn btn-ghost btn-sm", style: "color:#dc2626",
        onclick: () => { hosts.splice(i, 1); paint(); } }, t("delete")))));
  };
  paint();

  // Registration policy + the domain allow-list it is built from.
  const known = (s.known_domains || []);
  const knownBox = h("input", { type: "checkbox", checked: s.require_known_domain ? "checked" : null });
  const body = h("div", { class: "page-narrow" },
    settingsTabs("domains"),
    h("h1", {}, t("bind_domains")),
    h("p", { class: "muted" }, t("bind_domains_hint")),
    h("div", { class: "card" }, h("div", { class: "card-body" },
      list,
      h("div", { class: "inline-field", style: "margin-top:12px" }, inp,
        h("button", { class: "btn btn-ghost btn-sm", onclick: () => {
          const v = inp.value.trim().toLowerCase()
            .replace(/^https?:\/\//, "").replace(/\/.*$/, "").replace(/^\.+/, "").split(":")[0];
          if (!v) return;
          if (!hosts.includes(v)) hosts.push(v);
          inp.value = ""; paint();
        } }, "+" + t("add"))),
      h("button", { class: "btn btn-blue", style: "margin-top:12px", onclick: async () => {
        try {
          await api("/api/admin/site", { method: "POST", body: JSON.stringify({
            allowed_hosts: hosts, require_known_domain: knownBox.checked }) });
          toast(t("saved"));
        } catch (e) { toast(e.message, false); }
      } }, t("save")))),
    h("div", { class: "card", style: "margin-top:14px" }, h("div", { class: "card-body" },
      h("div", { class: "inline-field" }, knownBox,
        h("b", {}, t("require_known_domain"))),
      h("p", { class: "muted" }, t("require_known_domain_hint")),
      h("h3", { style: "margin-top:12px" }, t("known_domains") + " (" + known.length + ")"),
      h("p", { class: "muted" }, known.length ? known.join(", ") : t("no_known_domains")))));
  return { title: "", body };
}

function applyTheme(theme) {
  const root = document.documentElement;
  if (!theme || theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

async function adminSettings() {
  if (!hasPerm("settings.mail")) return noAccess();
  let cfg;
  try { cfg = (await api("/api/admin/settings")).config; } catch (e) { return noAccess(); }

  // The two mail engines are mutually exclusive: whichever one is saved becomes
  // the active provider and the server wipes the other side's credentials.
  const curProv = cfg.o365_mode === "1" ? "o365" : "smtp";
  const provSel = h("select", { class: "full" },
    [["smtp", t("provider_smtp")], ["o365", t("provider_o365")]]
      .map(([v, label]) => h("option", { value: v, selected: curProv === v ? "selected" : null }, label)));
  const f = (key, type) => h("input", { value: cfg[key] || "", type: type || "text", name: key, class: "full" });
  const lab = (label, el) => h("label", { class: "field" }, h("span", { class: "muted" }, label), el);
  const secSel = h("select", { name: "smtp_security", class: "full" },
    ["ssl", "starttls"].map(v => h("option", {
      value: v, selected: (cfg.smtp_security || "ssl") === v ? "selected" : null }, t(v))));
  const smtpPanel = h("div", {},
    h("h3", {}, "SMTP"),
    lab(t("smtp_host"), f("smtp_host")),
    h("div", { class: "grid-2" },
      lab(t("smtp_port"), f("smtp_port", "number")),
      lab(t("smtp_security"), secSel)),
    lab(t("smtp_user"), f("smtp_user")),
    lab(t("smtp_pass"), f("smtp_pass", "password")),
    lab(t("smtp_from"), f("smtp_from")),
    h("h3", { style: "margin-top:16px" }, "IMAP"),
    lab(t("imap_host"), f("imap_host")),
    h("div", { class: "grid-2" },
      lab(t("imap_port"), f("imap_port", "number")),
      lab(t("imap_folder"), f("imap_folder"))),
    lab(t("imap_user"), f("imap_user")),
    lab(t("imap_pass"), f("imap_pass", "password")),
    lab(t("base_url"), f("base_url")));
  const o365Panel = h("div", {},
    h("h3", {}, t("provider_o365")),
    lab(t("o365_tenant"), f("o365_tenant")),
    lab(t("o365_client_id"), f("o365_client_id")),
    lab(t("o365_client_secret"), f("o365_client_secret", "password")),
    lab(t("o365_scope"), f("o365_scope")));
  const syncProv = () => {
    smtpPanel.style.display = provSel.value === "smtp" ? "block" : "none";
    o365Panel.style.display = provSel.value === "o365" ? "block" : "none";
  };
  provSel.addEventListener("change", syncProv);
  const pollBtn = h("button", { class: "btn btn-ghost btn-sm", onclick: async () => {
    try {
      const r = await api("/api/admin/mail/poll", { method: "POST" });
      toast("Polled: " + r.handled + " messages");
    } catch (e) { toast(e.message, false); }
  } }, t("poll_now"));
  const testBtn = h("button", { class: "btn btn-ghost btn-sm", onclick: async () => {
    const to = prompt("To address (leave empty to use the sender)") || cfg.smtp_from || cfg.smtp_user || "";
    if (!to) return;
    try {
      const r = await api("/api/admin/mail/test", { method: "POST", body: JSON.stringify({ to }) });
      toast("Sent: " + (r.sent ? "yes" : "no"));
    } catch (e) { toast(e.message, false); }
  } }, t("test"));
  const body = h("div", { class: "page" },
    settingsTabs("settings"),
    h("h1", {}, t("mail_settings")),
    h("div", { class: "card mb-2" }, h("div", { class: "card-body" },
      h("h3", {}, t("mail_provider")),
      provSel,
      h("p", { class: "muted", style: "margin-top:8px" }, t("provider_exclusive_hint")))),
    h("div", { class: "card" }, h("div", { class: "card-body" },
      smtpPanel, o365Panel,
      h("div", { style: "display:flex;gap:8px;margin-top:14px" },
        h("button", { class: "btn btn-blue", onclick: async () => {
          const b = { mail_provider: provSel.value };
          const scope = provSel.value === "smtp" ? smtpPanel : o365Panel;
          for (const el of scope.querySelectorAll("input[name], select[name]")) b[el.name] = el.value;
          try {
            await api("/api/admin/settings", { method: "POST", body: JSON.stringify(b) });
            toast(t("saved"));
            render();
          } catch (e) { toast(e.message, false); }
        } }, t("save")),
        testBtn, pollBtn))));
  setTimeout(syncProv, 0);
  return { title: "", body };
}
// ----------------------------------------------------------------- boot
state.lang = localStorage.getItem("rz_lang") || "en";
document.documentElement.lang = state.lang === "en" ? "en" : (state.lang === "zh" ? "zh-CN" : "zh-TW");
(async function boot() {
  await loadBrand();
  // The session cookie is HttpOnly, so document.cookie can never see it -- ask the
  // server instead. A same-origin fetch sends the cookie, so a page refresh keeps
  // the session, and a 401 puts us cleanly back on the login screen.
  try {
    state.user = await api("/api/me", { noRedirect: true });
    state.me_perms = new Set(state.user.permissions);
    state.meta = await api("/api/meta");
    state.products = state.meta.products;
    state.modules = state.meta.modules || [];
    state.deployTypes = state.meta.deploy_types || ["ON-PREM", "SaaS"];
    applyTheme(state.meta.theme);
    startIdleWatchdog();
  } catch (e) {
    state.user = null; state.token = null; state.me_perms = new Set();
  }
  render();
})();
