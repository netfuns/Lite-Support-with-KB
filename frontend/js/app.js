import { t, setLang, LANGS } from "./i18n.js";

// ----------------------------------------------------------------- state
const state = {
  user: null,
  me_perms: new Set(),
  meta: null,
  products: [],
  token: null,
};

// ----------------------------------------------------------------- api
async function api(path, opts = {}) {
  opts.headers = { "Content-Type": "application/json" };
  if (state.token) opts.headers["X-Token"] = state.token;
  const res = await fetch(path, opts);
  const ct = res.headers.get("content-type") || "";
  let data = null;
  if (ct.includes("application/json")) data = await res.json();
  else if (res.status === 204) data = {};
  if (res.status === 401) {
    document.cookie = "rz_token=; Max-Age=0; path=/";
    state.user = null; state.token = null;
    if (window.location.hash !== "#/login") window.location.hash = "#/login";
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
  for (const c of children) {
    if (c == null) continue;
    el.append(typeof c === "string" ? document.createTextNode(c) : c);
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
      out += "<li>" + esc(L.replace(/^\s*[-*]\s+/, "")) + "</li>";
      continue;
    } else if (listOpen) { out += "</ul>"; listOpen = false; }
    if (L.trim() === "") { if (!listOpen) out += "<br>"; continue; }
    out += "<p>" + esc(L).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>") + "</p>";
  }
  if (inPre) out += "</code></pre>";
  if (listOpen) out += "</ul>";
  return out;
}

// ----------------------------------------------------------------- layout
function layout(title, content) {
  const root = document.getElementById("root");
  root.innerHTML = "";
  root.append(h("div", {},
    h("header", { class: "app-bar" },
      h("div", { class: "logo" }, "RankEZ " + t("app")),
      h("div", { class: "search-wrap" },
        h("input", { placeholder: t("search"), oninput: "data-q" + " = this.value" }),
        h("span", { class: "icon-search", onclick: doSearch }, "🔎")),
      h("div", { class: "actions" },
        h("select", { class: "lang", onchange: e => { setLang(e.target.value); render(current().route); } },
          LANGS.map(l => h("option", { value: l.id, selected: state.lang === l.id ? "selected" : null }, l.label))),
        state.user
          ? h("span", { class: "user", onclick: toggleUserMenu }, "👤 " + (state.user.display_name || state.user.email))
          : h("button", { class: "btn btn-blue", onclick: () => window.location.hash = "#/login" }, t("login")),
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
    h("a", { href: "#/me" }, t("my_account")),
    hasPerm("settings.mail") ? h("a", { href: "#/admin/settings" }, t("settings")) : null,
    h("button", { onclick: logout }, t("logout")));
  document.querySelector(".actions").append(userMenu);
  document.addEventListener("click", function close(e) {
    if (userMenu && !userMenu.contains(e.target)) { userMenu.remove(); userMenu = null; }
    document.removeEventListener("click", close);
  });
}

function hasPerm(key) { return state.me_perms.has(key); }

async function logout() {
  try { await api("/api/auth/logout", { method: "POST" }); } catch (e) {}
  document.cookie = "rz_token=; Max-Age=0; path=/";
  state.user = null; state.token = null;
  window.location.hash = "#/login";
}

function sidebar() {
  const links = [
    ["#/dashboard", t("dashboard"), true],
    ["#/tickets", t("tickets"), true],
  ];
  if (hasPerm("ticket.create")) links.push(["#/new-ticket", "+" + t("new_ticket"), false]);
  links.push(["#/kb", t("kb"), true]);
  if (hasPerm("customer.view")) links.push(["#/customers", t("customers"), false]);
  const items = [];
  links.forEach(([href, label, active]) => items.push(h("a", { href, class: "active" + (active ? " active" : "") }, label)));
  if (hasPerm("user.manage")) {
    items.push(h("div", { class: "sep-label" }, t("admin")));
    if (hasPerm("user.manage")) items.push(h("a", { href: "#/admin/users" }, t("users")));
    if (hasPerm("role.manage")) items.push(h("a", { href: "#/admin/roles" }, t("roles")));
    if (hasPerm("group.manage")) items.push(h("a", { href: "#/admin/groups" }, t("groups")));
    if (hasPerm("settings.mail")) items.push(h("a", { href: "#/admin/settings" }, t("settings")));
  }
  return h("nav", { class: "sidebar" }, items);
}

// ----------------------------------------------------------------- router
function current() {
  const raw = (window.location.hash || "#/").replace(/^#/, "");
  const [path, id] = raw.split("/");
  return { route: path.slice(1) || "dashboard", raw };
}

async function render() {
  const c = current();
  // handle sub-routes
  let route = c.raw.replace(/^\/?/, "").split("/")[0];
  let view;
  if (route === "dashboard") view = dashboardView();
  else if (route === "tickets") view = ticketsView();
  else if (route === "ticket") view = ticketDetail(c.raw.split("/")[1]);
  else if (route === "new-ticket") view = newTicketView();
  else if (route === "kb") {
    if (c.raw.includes("/kb/")) view = kbDetail(c.raw.split("/")[2]);
    else view = kbView();
  } else if (route === "customers") view = customersView();
  else if (route === "login") view = loginView();
  else if (route === "register") view = registerView();
  else if (route === "me") view = meView();
  else if (route === "admin") {
    const sub = c.raw.split("/")[1];
    view = ({ users: adminUsers, roles: adminRoles, groups: adminGroups, settings: adminSettings })[sub] ?
      ({ users: adminUsers, roles: adminRoles, groups: adminGroups, settings: adminSettings })[sub]() : adminUsers();
  } else view = dashboardView();
  view = await view;
  layout(view.title, view.body);
}

window.addEventListener("hashchange", () => render());

// ----------------------------------------------------------------- views
function loginView() {
  const email = h("input", { name: "email", type: "email", placeholder: t("email"), required: true });
  const pw = h("input", { name: "password", type: "password", placeholder: t("password"), required: true });
  const totp = h("input", { name: "totp", class: "totp hidden", type: "text", placeholder: "TOTP" });
  const body = h("div", { style: "max-width:360px;margin:60px auto" },
    h("h1", { style: "text-align:center" }, "RankEZ"),
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("div", {}, email, pw, totp),
      h("button", { class: "btn btn-blue", style: "width:100%;margin-top:10px", onclick: async (e) => {
        e.preventDefault();
        const payload = { email: email.value, password: pw.value };
        if (totp.value) payload.totp = totp.value;
        const r = await fetch("/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
        const data = await r.json().catch(() => ({ error: "HTTP " + r.status }));
        if (data.need_totp) { totp.classList.remove("hidden"); totp.focus(); toast(t("totp_prompt") || "Enter your 2FA code"); return; }
        if (data.error) { toast(data.error, false); return; }
        state.token = data.token;
        const me = await api("/api/me");
        state.user = me; state.me_perms = new Set(me.permissions);
        state.meta = await api("/api/meta");
        state.products = state.meta.products;
        window.location.hash = "#/dashboard";
      } }, t("sign_in_btn")),
      h("p", { class: "muted", style: "text-align:center;margin-top:10px" },
        h("a", { href: "#/register" }, t("register"))))),
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
        else toast((d && d.detail) || d.error || "failed", false);
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
  const statusSel = h("select", {}, [h("option", { value: "" }, t("filter_by_status")),
    ["new", "customer_replied", "support_replied", "closed"].map(s => h("option", { value: s }, t("st_" + s)))]);
  const prioSel = h("select", {}, [h("option", { value: "" }, t("filter_by_priority")),
    ["critical", "high", "medium", "low"].map(p => h("option", { value: p }, t("pr_" + p)))]);
  const ownerInp = h("input", { placeholder: t("filter_by_owner") });
  const custInp = h("input", { placeholder: t("filter_by_customer") });
  const verInp = h("input", { placeholder: t("filter_by_version") });
  const prodInp = h("input", { placeholder: t("filter_by_product") });
  const dFrom = h("input", { type: "date", placeholder: t("filter_by_date_from") });
  const dTo = h("input", { type: "date", placeholder: t("filter_by_date_to") });

  async function doLoad() {
    const p = new URLSearchParams();
    if (statusSel.value) p.set("status", statusSel.value);
    if (prioSel.value) p.set("priority", prioSel.value);
    if (ownerInp.value) p.set("owner", ownerInp.value);
    if (custInp.value) p.set("customer", custInp.value);
    if (verInp.value) p.set("version", verInp.value);
    if (prodInp.value) p.set("product", prodInp.value);
    if (dFrom.value) p.set("date_from", dFrom.value);
    if (dTo.value) p.set("date_to", dTo.value);
    const r = await api("/api/tickets?" + p.toString());
    renderRows(r.items || []);
  }
  function renderRows(items) {
    tbody.innerHTML = "";
    if (!items.length) { tbody.append(h("tr", {}, h("td", { colspan: 7, class: "muted" }, t("no_results")))); }
    for (const t of items) {
      tbody.append(h("tr", {},
        h("td", {}, h("a", { href: "#/ticket/" + t.id }, t.code)),
        h("td", {}, t.title),
        h("td", {}, t.customer_name || "-"),
        h("td", {}, t.version || "-"),
        h("td", {}, t.product || "-"),
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
      h("div", { style: "display:flex;gap:8px;flex-wrap:wrap" }, statusSel, prioSel, ownerInp, custInp, verInp, prodInp, dFrom, dTo,
        h("button", { class: "btn btn-ghost btn-sm", onclick: doLoad }, t("filter"))))),
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("table", {},
        h("thead", {}, h("tr", {},
          [t("code"), t("title"), t("customer"), t("version"), t("module"), t("priority"), t("status"), t("owner"), t("created"), t("source")]
            .map(c => h("th", {}, c)))),
        tbody))));
  [statusSel, prioSel, ownerInp, custInp, verInp, prodInp, dFrom, dTo].forEach(el => {
    el.addEventListener("change", doLoad);
    if (el.tagName === "INPUT" && el.type !== "date") el.addEventListener("input", doLoad);
  });
  doLoad();
  return { title: "", body };
}

async function ticketDetail(id) {
  if (!state.user) return loginView();
  let data;
  try { data = await api("/api/tickets/" + id); } catch (e) {
    return { title: "", body: h("div", { class: "card" }, h("div", { class: "card-body" }, t("no_results"))) };
  }
  const T = data.ticket;
  const can = {
    claim: hasPerm("ticket.claim"),
    owner: hasPerm("ticket.change_owner"),
    status: hasPerm("ticket.change_status"),
    reply: hasPerm("ticket.reply"),
    export: hasPerm("ticket.export"),
  };
  const ownerSel = h("select", {}, h("option", { value: "" }, t("owner") + "…"));
  // load users for owner select
  (async () => {
    try {
      const r = await api("/api/admin/users");
      ownerSel.innerHTML = "";
      ownerSel.append(h("option", { value: "" }, "- " + t("owner") + " -"));
      for (const u of (r.items || [])) {
        ownerSel.append(h("option", { value: u.id, selected: String(u.id) === String(T.owner_id) ? "selected" : null },
          u.display_name || u.email));
      }
    } catch (e) {}
  })();
  ownerSel.onchange = async () => {
    if (!ownerSel.value) return;
    await api("/api/tickets/" + id + "/owner", { method: "PUT", body: JSON.stringify({ owner_id: Number(ownerSel.value) }) });
    toast("OK");
    setTimeout(() => render(), 300);
  };

  const closeSel = h("select", {},
    h("option", { value: "" }, t("status") + "…"),
    ["new", "customer_replied", "support_replied", "closed"].map(s => h("option", { value: s, selected: T.status === s ? "selected" : null }, t("st_" + s))));
  const archChk = h("input", { type: "checkbox", checked: true });
  const desChk = h("input", { type: "checkbox", checked: true });
  const visSel = h("select", {},
    ["registered", "usergroup", "internal"].map(v => h("option", { value: v, selected: v === "registered" ? "selected" : null }, t(v))));

  const statusBtn = h("button", { class: "btn btn-ghost", onclick: async () => {
    if (!closeSel.value) return;
    const body = { status: closeSel.value };
    if (closeSel.value === "closed") { body.archive = archChk.checked ? "1" : "0"; body.kb_desensitize = desChk.checked ? "1" : "0"; body.kb_visibility = visSel.value; }
    await api("/api/tickets/" + id + "/status", { method: "PUT", body: JSON.stringify(body) });
    toast("OK"); setTimeout(() => render(), 400);
  } }, "Update " + t("status"));

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
      (m.attachments || []).map(a => h("a", { href: "/files/" + a.stored_name, target: "_blank", style: "display:inline-block;margin-top:6px" }, "📎 " + a.filename))));
  }));

  const body = h("div", {},
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("div", { class: "flex-between" },
        h("h1", { style: "margin:0" }, T.code + " · " + T.title),
        statusTag(T.status)),
      h("div", { class: "muted mb-2" },
        [t("customer") + " " + (T.customer_name || "-"), t("module") + " " + (T.product || "-"),
          t("version") + " " + (T.version || "-"), t("source") + " " + T.source,
          t("created") + " " + T.created_at].join(" · ")),
      h("div", { style: "display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:8px" },
        claimBtn,
        can.owner ? h("label", {}, ownerSel, statusBtn) : statusBtn,
        T.status === "closed" ? h("label", {}, archChk, t("archive"), " · ", visSel) : null,
        T.status === "closed" ? h("label", {}, desChk, t("desensitize")) : null,
        can.export ? h("a", { class: "btn btn-ghost", href: "/api/kb/export/" + (T.kb_article_id || id), target: "_blank" }, t("export_pdf")) : null,
      ),
      h("div", { class: "card-body", style: "background:var(--bg)" },
        h("div", { class: "flex-between mb-2" }, h("h3", {}, t("messages") + " (" + (data.messages || []).length + ")"),
          can.reply ? h("label", { class: "muted" }, intChk, t("internal")) : null),
        msgs2,
        can.reply ? h("div", { class: "card-body", style: "background:var(--card)" },
          h("div", {}, msgInp, fileInp),
          h("div", { class: "flex-between", style: "margin-top:8px" },
            h("span", { class: "muted" }, t("reply") + " · " + t("internal")),
            replyBtn)) : null))));
  return { title: "", body };
}

async function newTicketView() {
  if (!state.user) return loginView();
  const custInp = h("input", { list: "cust-dl", placeholder: t("select_customer") });
  const prodSel = h("select", {},
    h("option", { value: "" }, t("select_product")),
    (state.products || []).map(p => h("option", { value: p }, p)));
  const prioSel = h("select", {},
    ["critical", "high", "medium", "low"].map(p => h("option", { value: p, selected: p === "medium" ? "selected" : null }, t("pr_" + p))));
  const verInp = h("input", { placeholder: t("version") });
  const title = h("input", { placeholder: t("title"), required: true });
  const desc = h("textarea", { rows: 4, placeholder: t("desc") });
  const intChk = h("input", { type: "checkbox" });
  const fileInp = h("input", { type: "file", multiple: true });

  const dl = h("datalist", { id: "cust-dl" });
  custInp.oninput = async () => {
    const q = custInp.value.trim();
    if (!q) { dl.innerHTML = ""; return; }
    try {
      const r = await api("/api/customers?q=" + encodeURIComponent(q));
      dl.innerHTML = "";
      for (const c of (r.items || [])) dl.append(h("option", { value: c.name }, c.domains));
    } catch (e) {}
  };

  const body = h("div", {},
    h("h1", {}, t("new_ticket")),
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("div", {},
        h("label", { class: "muted", style: "display:block;margin:6px 0 3px" }, t("title")), title,
        h("label", { class: "muted", style: "display:block;margin:8px 0 3px" }, t("customer")), custInp, dl,
        h("div", { style: "display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:8px" },
          h("label", { style: "display:block" }, h("span", { class: "muted" }, t("module")), prodSel),
          h("label", { style: "display:block" }, h("span", { class: "muted" }, t("version")), verInp),
          h("label", { style: "display:block" }, h("span", { class: "muted" }, t("select_priority")), prioSel)),
        h("label", { class: "muted", style: "display:block;margin:8px 0 3px" }, t("desc")), desc,
        h("div", { style: "margin:8px 0" }, h("label", { class: "muted" }, intChk, " " + t("internal")), fileInp),
        h("div", { style: "margin-top:10px" },
          h("button", { class: "btn btn-blue", onclick: async () => {
            if (!title.value) { toast(t("title") + " *", false); return; }
            const fd = new FormData();
            fd.set("title", title.value);
            fd.set("customer_name", custInp.value);
            fd.set("product", prodSel.value);
            fd.set("version", verInp.value);
            fd.set("priority", prioSel.value);
            fd.set("description", desc.value);
            if (intChk.checked) fd.set("internal", "1");
            for (const f of fileInp.files) fd.append("files", f);
            const r = await apiForm("/api/tickets", fd);
            toast("OK: " + r.code); window.location.hash = "#/ticket/" + r.id;
          } }, t("submit")),
          h("button", { class: "btn btn-ghost", style: "margin-left:8px", onclick: () => window.location.hash = "#/tickets" }, t("cancel")))))));
  return { title: "", body };
}

async function kbView() {
  if (!state.user) return loginView();
  const q = h("input", { placeholder: t("kb_search"), oninput: "doLoad()" });
  const colSel = h("select", {}, [h("option", { value: "" }, t("all") + " · " + t("collections"))]);
  const tbody = h("tbody", {});
  let cols = [];
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
    if (colSel.value) p.set("collection", colSel.value);
    const r = await api("/api/kb/articles?" + p.toString());
    tbody.innerHTML = "";
    for (const a of (r.items || [])) {
      const colName = cols.find(c => c.id === a.collection_id)?.name || "";
      tbody.append(h("tr", {},
        h("td", {}, h("a", { href: "#/kb/" + a.id }, a.title)),
        h("td", {}, colName || "-"),
        h("td", {}, visTag(a.visibility)),
        h("td", {}, a.source),
        h("td", {}, a.desensitized ? "desensitized" : "raw"),
        h("td", {}, (a.created_at || "").slice(0, 10))));
    }
  }
  colSel.onchange = doLoad;
  loadCols();
  doLoad();
  const body = h("div", {},
    h("div", { class: "flex-between" }, h("h1", {}, t("kb")),
      hasPerm("kb.import") ? h("button", { class: "btn btn-ghost", onclick: () => openImportModal(colSel.value) }, t("import")) : null,
      hasPerm("kb.create") ? h("button", { class: "btn btn-blue", onclick: () => openKbEditor(null, colSel.value) }, "+" + t("new_article")) : null),
    h("div", { class: "card mb-2" }, h("div", { class: "card-body" },
      h("div", { style: "display:flex;gap:8px" }, q, colSel))),
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("table", {},
        h("thead", {}, h("tr", {}, [t("title"), t("collections"), t("status"), "Source", "Data", t("created")].map(c => h("th", {}, c)))),
        tbody))));
  return { title: "", body };
}

function openImportModal(preCol) {
  const colSel = h("select", {}, [h("option", { value: "" }, t("all") + " · " + t("collections"))]);
  const visSel = h("select", {},
    ["public", "registered", "internal", "usergroup"].map(v => h("option", { value: v, selected: v === "registered" ? "selected" : null }, t(v))));
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

function openKbEditor(id, preCol) {
  const title = h("input", { placeholder: t("kb_title") });
  const body = h("textarea", { rows: 10, placeholder: t("kb_body") });
  const visSel = h("select", {},
    ["public", "registered", "internal", "usergroup"].map(v => h("option", { value: v, selected: v === "registered" ? "selected" : null }, t(v))));
  const colSel = h("select", {}, [h("option", { value: "" }, "- " + t("collections") + " -")]);
  const grpSel = h("select", { multiple: true, size: 5 });
  const bodyWrap = h("div", {},
    h("label", { style: "display:block;margin:6px 0" }, h("span", { class: "muted" }, t("kb_title")), title),
    h("label", { style: "display:block;margin:6px 0" }, h("span", { class: "muted" }, t("kb_body")), body),
    h("div", { style: "display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px" },
      h("label", {}, h("span", { class: "muted" }, t("kb_visibility")), visSel),
      h("label", {}, h("span", { class: "muted" }, t("collections")), colSel)),
    h("div", { class: "flex-between mt-2" },
      h("button", { class: "btn btn-blue", onclick: async () => {
        const b = { title: title.value, body: body.value, visibility: visSel.value, collection_id: colSel.value || null };
        if (id) await api("/api/kb/articles/" + id, { method: "PUT", body: JSON.stringify(b) });
        else await api("/api/kb/articles", { method: "POST", body: JSON.stringify(b) });
        toast("OK"); window.location.hash = "#/kb";
      } }, t("save")),
      h("button", { class: "btn btn-ghost", onclick: closeModal }, t("cancel"))));
  showModal("Edit article", bodyWrap);
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
  const canEdit = hasPerm("kb.edit");
  const canExport = hasPerm("kb.export_pdf");
  const canShare = hasPerm("kb.share_email");
  const canDelete = hasPerm("kb.delete");
  const body = h("div", { style: "max-width:900px" },
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("div", { class: "flex-between" },
        h("h1", {}, a.title, " ", visTag(a.visibility)),
        h("div", { style: "display:flex;gap:6px" },
          canExport ? h("a", { class: "btn btn-ghost btn-sm", href: "/api/kb/export/" + a.id, target: "_blank" }, t("export_pdf")) : null,
          canShare ? h("button", { class: "btn btn-ghost btn-sm", onclick: async () => {
            const email = prompt("Email to share with");
            if (!email) return;
            await api("/api/kb/share", { method: "POST", body: JSON.stringify({ article_id: a.id, email }) });
            toast("OK");
          } }, t("share")) : null,
          canEdit ? h("button", { class: "btn btn-ghost btn-sm", onclick: () => openKbEditor(a.id) }, t("save")) : null,
          canDelete ? h("button", { class: "btn btn-ghost btn-sm", style: "color:#dc2626", onclick: async () => {
            await api("/api/kb/articles/" + a.id, { method: "DELETE" }); window.location.hash = "#/kb";
          } }, t("delete")) : null)),
      h("div", { class: "muted mb-2" },
        [a.source, a.desensitized ? "desensitized" : "raw", a.created_at].filter(Boolean).join(" · ")),
      h("div", { class: "markdown", innerHTML: mdToHtml(a.body) }),
      (data.attachments || []).filter(x => x.content_type.startsWith("image/")).map(x =>
        h("img", { src: "/files/" + x.stored_name, style: "max-height:300px;margin:8px 0" })))));
  return { title: "", body };
}

async function customersView() {
  if (!hasPerm("customer.view")) return { title: "", body: h("div", { class: "card" }, h("div", { class: "card-body" }, "No access")) };
  const tbody = h("tbody", {});
  async function doLoad() {
    const r = await api("/api/customers");
    tbody.innerHTML = "";
    for (const c of (r.items || [])) {
      tbody.append(h("tr", {},
        h("td", {}, c.name), h("td", {}, c.domains), h("td", {}, c.version || "-"),
        h("td", {}, c.service_start || "-"), h("td", {}, c.service_end || "-"),
        h("td", {}, c.contact_email || "-"),
        h("td", {}, h("button", { class: "btn btn-ghost btn-sm", onclick: async () => {
          const b = await api("/api/customers/" + c.id, { method: "DELETE" }); toast("OK"); doLoad();
        } }, t("delete")))));
    }
  }
  const custInp = h("input", { placeholder: t("add_customer") });
  const domInp = h("input", { placeholder: t("domains") });
  const form = h("div", { style: "display:flex;gap:8px;align-items:center;margin-bottom:12px" },
    custInp, domInp,
    hasPerm("customer.create") ? h("button", { class: "btn btn-blue btn-sm", onclick: async () => {
      if (!custInp.value || !domInp.value) { toast("name and domains required", false); return; }
      await api("/api/customers", { method: "POST", body: JSON.stringify({ name: custInp.value, domains: domInp.value }) });
      custInp.value = ""; domInp.value = ""; toast("OK"); doLoad();
    } }, "+") : null);
  const importBtn = h("label", { class: "btn btn-ghost btn-sm", style: "cursor:pointer" },
    t("import_csv") + " · " + h("span", { class: "muted" }, t("download_template")) + " ",
    h("a", { href: "/api/customers/template.csv", download: true, style: "color:var(--blue)" }, "↓"),
    h("input", { type: "file", accept: ".csv", style: "display:none", onchange: async e => {
      const f = e.target.files[0]; if (!f) return;
      const fd = new FormData(); fd.append("file", f);
      const r = await apiForm("/api/customers/import", fd);
      toast("Imported " + r.created + " new / " + r.updated + " updated");
      e.target.value = ""; doLoad();
    } }));
  const body = h("div", {},
    h("div", { class: "flex-between" }, h("h1", {}, t("customers")),
      hasPerm("customer.import_csv") ? importBtn : null),
    form,
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("table", {},
        h("thead", {}, h("tr", {}, [t("name"), t("domains"), t("version"), t("service_start"), t("service_end"), t("contact_email"), t("actions")].map(c => h("th", {}, c)))),
        tbody))));
  doLoad();
  return { title: "", body };
}

function meView() {
  const name = h("input", { value: state.user.display_name || "", placeholder: t("display_name") });
  const pw1 = h("input", { type: "password", placeholder: t("password") });
  const pw2 = h("input", { type: "password", placeholder: t("password") + " (new)" });
  const savePw = h("button", { class: "btn btn-ghost btn-sm", onclick: async () => {
    if (!pw1.value || !pw2.value) { toast("enter passwords", false); return; }
    const r = await fetch("/api/me/password", { method: "POST", headers: { "Content-Type": "application/json", "X-Token": state.token }, body: JSON.stringify({ old: pw1.value, new: pw2.value }) });
    toast(r.ok ? "Password changed" : "failed", r.ok);
    pw1.value = ""; pw2.value = "";
  } }, t("save"));
  let totp = null;
  const totpEnableBtn = h("button", { class: "btn btn-ghost btn-sm", onclick: async () => {
    const r = await fetch("/api/me/totp/enable", { method: "POST", headers: { "X-Token": state.token } });
    const d = await r.json();
    totp.textContent = "Secret: " + d.secret + "\nURI: " + d.uri + "\n\nScan with your TOTP app, then save.";
    totp.classList.remove("hidden");
  } }, "Enable TOTP");
  const totpDisableBtn = h("button", { class: "btn btn-ghost btn-sm", onclick: async () => {
    await fetch("/api/me/totp/disable", { method: "POST", headers: { "X-Token": state.token } });
    toast("OK"); render();
  } }, "Disable TOTP");
  totp = h("pre", { class: "muted hidden" }, "");
  const roleText = (state.user.roles || []).join(", ");
  const grpText = (state.user.groups || []).join(", ");
  const perms = (state.user.permissions || []).length;
  const body = h("div", { style: "max-width:760px" },
    h("h1", {}, t("my_account")),
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("div", { class: "flex-between mb-2" },
        h("div", {}, h("div", { class: "muted" }, t("email")), h("div", {}, state.user.email)),
        h("div", {}, h("div", { class: "muted" }, t("role")), h("div", {}, roleText),
          h("div", { class: "muted", style: "margin-top:6px" }, t("groups") + ": " + grpText),
          h("div", { class: "muted", style: "margin-top:6px" }, "Permissions: " + perms))),
      h("label", { style: "display:block;margin:8px 0" }, h("span", { class: "muted" }, t("display_name")), name),
      h("button", { class: "btn btn-ghost btn-sm", onclick: async () => {
        await fetch("/api/me/name", { method: "POST", headers: { "Content-Type": "application/json", "X-Token": state.token }, body: JSON.stringify({ display_name: name.value }) });
        state.user.display_name = name.value; toast("OK");
      } }, t("save")),
      h("div", { style: "display:flex;gap:8px;align-items:center;margin-top:12px" },
        h("span", { class: "muted" }, t("password")), pw1, pw2, savePw),
      h("div", { style: "display:flex;gap:8px;margin-top:12px" },
        state.user.totp_enabled ? totpDisableBtn : totpEnableBtn, totp,
    ))));
  return { title: "", body };
}

async function adminUsers() {
  if (!hasPerm("user.manage")) return { title: "", body: h("div", { class: "card" }, h("div", { class: "card-body" }, "No access")) };
  const tbody = h("tbody", {});
  const q = h("input", { placeholder: t("email") + " / " + t("display_name"), oninput: doLoad });
  async function doLoad() {
    const r = await api("/api/admin/users?q=" + encodeURIComponent(q.value || ""));
    tbody.innerHTML = "";
    for (const u of (r.items || [])) {
      tbody.append(h("tr", {},
        h("td", {}, u.email, h("br"), u.display_name || ""),
        h("td", {}, (u.roles || []).join(", ")),
        h("td", {}, u.status),
        h("td", {}, h("button", { class: "btn btn-ghost btn-sm", onclick: async () => {
          const p = prompt("Set new password for " + u.email, ""); if (p) await api("/api/admin/users/" + u.id, { method: "PUT", body: JSON.stringify({ password: p }) });
          toast("OK");
        } }, t("save")),
          hasPerm("user.reset_totp") ? h("button", { class: "btn btn-ghost btn-sm", style: "margin-left:4px", onclick: async () => {
            await api("/api/admin/users/" + u.id + "/reset_totp", { method: "POST" }); toast("OK");
          } }, t("reset_totp")) : null,
          h("button", { class: "btn btn-ghost btn-sm", style: "margin-left:4px;color:#dc2626", onclick: async () => {
            await api("/api/admin/users/" + u.id, { method: "DELETE" }); toast("OK"); doLoad();
          } }, t("delete")))));
    }
  }
  const emailInp = h("input", { placeholder: t("email") });
  const nameInp = h("input", { placeholder: t("display_name") });
  const pwInp = h("input", { placeholder: t("password") });
  const roleInp = h("select", { multiple: true, size: 4 });
  (async () => {
    try { const r = await api("/api/admin/roles"); (r.items || []).forEach(x => roleInp.append(h("option", { value: x.name }, x.name))); } catch (e) {}
  })();
  const addBtn = h("button", { class: "btn btn-blue btn-sm", onclick: async () => {
    const roles = Array.from(roleInp.selectedOptions).map(o => o.value);
    await api("/api/admin/users", { method: "POST", body: JSON.stringify({ email: emailInp.value, display_name: nameInp.value, password: pwInp.value, roles }) });
    emailInp.value = ""; nameInp.value = ""; pwInp.value = ""; toast("OK"); doLoad();
  } }, "+" + t("add_user"));
  const body = h("div", {},
    h("h1", {}, t("users")),
    h("div", { class: "card mb-2" }, h("div", { class: "card-body" },
      h("div", { style: "display:flex;gap:8px;flex-wrap:wrap;align-items:center" }, q, emailInp, nameInp, pwInp, roleInp, addBtn))),
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("table", {},
        h("thead", {}, h("tr", {}, [t("email"), t("role"), t("status"), t("actions")].map(c => h("th", {}, c)))),
        tbody))));
  doLoad();
  return { title: "", body };
}

async function adminRoles() {
  if (!hasPerm("role.manage")) return { title: "", body: h("div", { class: "card" }, h("div", { class: "card-body" }, "No access")) };
  const meta = await api("/api/meta");
  const roles = await api("/api/admin/roles");
  const cards = (roles.items || []).map(r => {
    const perms = meta.permissions.filter(p => r.permissions.includes(p.key));
    const grp = h("div", { style: "display:grid;grid-template-columns:1fr 1fr;gap:4px" },
      perms.map(p => h("label", { class: "muted", style: "font-size:13px" },
        h("input", { type: "checkbox", checked: "checked", onchange: e => {
          if (e.target.checked) r.permissions.push(p.key); else r.permissions = r.permissions.filter(k => k !== p.key);
        } }, p.key + " (" + (p.grp || p.group) + ")"))));
    const saveBtn = h("button", { class: "btn btn-ghost btn-sm", onclick: async () => {
      await api("/api/admin/roles/" + r.id, { method: "PUT", body: JSON.stringify({ permissions: r.permissions }) });
      toast("OK"); setTimeout(render, 300);
    } }, t("save"));
    const delBtn = !r.builtin ? h("button", { class: "btn btn-ghost btn-sm", style: "color:#dc2626;margin-left:6px", onclick: async () => {
      await api("/api/admin/roles/" + r.id, { method: "DELETE" }); toast("OK"); setTimeout(render, 300);
    } }, t("delete")) : null;
    return h("div", { class: "card mb-2" },
      h("div", { class: "card-body" },
        h("div", { class: "flex-between" },
          h("h3", {}, r.name + (r.builtin ? " (" + t("builtin") + ")" : "")),
          h("div", {}, saveBtn, delBtn)),
        grp));
  });
  const body = h("div", {},
    h("div", { class: "flex-between" }, h("h1", {}, t("roles")),
      hasPerm("role.manage") ? h("button", { class: "btn btn-blue btn-sm", onclick: async () => {
        const name = prompt("Role name"); if (!name) return;
        await api("/api/admin/roles", { method: "POST", body: JSON.stringify({ name, permissions: [] }) });
        toast("OK"); render();
      } }, "+" + t("add_role")) : null),
    cards);
  return { title: "", body };
}

async function adminGroups() {
  if (!hasPerm("group.manage")) return { title: "", body: h("div", { class: "card" }, h("div", { class: "card-body" }, "No access")) };
  const r = await api("/api/admin/groups");
  const rows = (r.items || []).map(g => h("tr", {},
    h("td", {}, g.id), h("td", {}, g.name), h("td", {}, g.builtin ? t("builtin") : "user"),
    h("td", {}, String(g.member_count)),
    !g.builtin ? h("td", {}, h("button", { class: "btn btn-ghost btn-sm", style: "color:#dc2626", onclick: async () => {
      await api("/api/admin/groups/" + g.id, { method: "DELETE" }); toast("OK"); render();
    } }, t("delete"))) : h("td", {}, null)));
  const inName = h("input", { placeholder: t("add_group") });
  const body = h("div", {},
    h("h1", {}, t("groups")),
    h("div", { style: "display:flex;gap:8px;margin:10px 0" }, inName,
      hasPerm("group.manage") ? h("button", { class: "btn btn-blue btn-sm", onclick: async () => {
        if (!inName.value) return;
        await api("/api/admin/groups", { method: "POST", body: JSON.stringify({ name: inName.value }) });
        inName.value = ""; toast("OK"); render();
      } }, "+") : null),
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("table", {},
        h("thead", {}, h("tr", {}, [t("code"), t("name"), t("status"), t("members"), t("actions")].map(c => h("th", {}, c)))),
        h("tbody", {}, rows)))));
  return { title: "", body };
}

async function adminSettings() {
  if (!hasPerm("settings.mail")) return { title: "", body: h("div", { class: "card" }, h("div", { class: "card-body" }, "No access")) };
  let cfg;
  try { cfg = (await api("/api/admin/settings")).config; } catch (e) { return { title: "", body: h("div", {}, t("no_results")) }; }
  const f = (key, type) => h("input", { value: cfg[key] || "", type, name: key });
  const o365Chk = h("input", { type: "checkbox", checked: cfg.o365_mode === "1" });
  const o365Fields = h("div", { class: "muted" },
    h("label", { style: "display:block;margin:6px 0" }, h("span", { class: "muted" }, t("o365_tenant")), f("o365_tenant")),
    h("label", { style: "display:block;margin:6px 0" }, h("span", { class: "muted" }, t("o365_client_id")), f("o365_client_id")),
    h("label", { style: "display:block;margin:6px 0" }, h("span", { class: "muted" }, t("o365_client_secret")), f("o365_client_secret")),
    h("label", { style: "display:block;margin:6px 0" }, h("span", { class: "muted" }, t("o365_scope")), f("o365_scope")));
  const pollBtn = h("button", { class: "btn btn-ghost btn-sm", onclick: async () => {
    const r = await api("/api/admin/mail/poll", { method: "POST" });
    toast("Polled: " + r.handled + " messages");
  } }, t("poll_now"));
  const testBtn = h("button", { class: "btn btn-ghost btn-sm", onclick: async () => {
    const to = prompt("To address"); if (!to) return;
    const r = await api("/api/admin/mail/test", { method: "POST", body: JSON.stringify({ to }) });
    toast("Sent: " + (r.sent ? "yes" : "no"));
  } }, t("test"));
  const body = h("div", { style: "max-width:760px" },
    h("h1", {}, t("settings")),
    h("div", { class: "card" }, h("div", { class: "card-body" },
      h("label", { style: "display:block;margin:6px 0" }, h("span", { class: "muted" }, "SMTP host"), f("smtp_host")),
      h("div", { style: "display:grid;grid-template-columns:1fr 1fr;gap:8px" },
        h("label", {}, h("span", { class: "muted" }, "Port"), f("smtp_port", "number")),
        h("label", {}, h("span", { class: "muted" }, "Security"),
          h("select", { value: cfg.smtp_security || "ssl" },
            h("option", { value: "ssl", selected: (cfg.smtp_security || "ssl") === "ssl" ? "selected" : null }, t("ssl")),
            h("option", { value: "starttls", selected: (cfg.smtp_security || "ssl") === "starttls" ? "selected" : null }, t("starttls")))),
      h("label", { style: "display:block;margin:6px 0" }, h("span", { class: "muted" }, t("smtp_user")), f("smtp_user")),
      h("label", { style: "display:block;margin:6px 0" }, h("span", { class: "muted" }, t("smtp_pass")), f("smtp_pass", "password")),
      h("label", { style: "display:block;margin:6px 0" }, h("span", { class: "muted" }, t("smtp_from")), f("smtp_from")),
      h("h3", { style: "margin-top:14px" }, "IMAP"),
      h("label", { style: "display:block;margin:6px 0" }, h("span", { class: "muted" }, "IMAP host"), f("imap_host")),
      h("div", { style: "display:grid;grid-template-columns:1fr 1fr;gap:8px" },
        h("label", {}, h("span", { class: "muted" }, "Port"), f("imap_port", "number")),
        h("label", {}, h("span", { class: "muted" }, "Folder"), f("imap_folder"))),
      h("label", { style: "display:block;margin:6px 0" }, h("span", { class: "muted" }, t("imap_user")), f("imap_user")),
      h("label", { style: "display:block;margin:6px 0" }, h("span", { class: "muted" }, t("imap_pass")), f("imap_pass", "password")),
      h("label", { style: "display:block;margin:6px 0" }, h("span", { class: "muted" }, "Base URL"), f("base_url")),
      h("label", { class: "muted", style: "display:block;margin:8px 0" }, o365Chk, " " + t("o365_mode")),
      o365Fields,
      h("div", { style: "display:flex;gap:8px;margin-top:10px" },
        h("button", { class: "btn btn-blue", onclick: async () => {
          const b = { smtp_host: cfg.smtp_host || "" };
          for (const inp of document.querySelectorAll(".card input[name]")) b[inp.name] = inp.value;
          b.o365_mode = o365Chk.checked ? "1" : "0";
          for (const inp of o365Fields.querySelectorAll("[name]")) b[inp.name] = inp.value;
          const sel = document.querySelector(".card select");
          if (sel) b.smtp_security = sel.value;
          await api("/api/admin/settings", { method: "POST", body: JSON.stringify(b) });
          toast("Saved");
        } }, t("save")),
        testBtn, pollBtn)))));
  return { title: "", body };
}

// ----------------------------------------------------------------- boot
state.lang = localStorage.getItem("rz_lang") || "en";
document.documentElement.lang = state.lang === "en" ? "en" : (state.lang === "zh" ? "zh-CN" : "zh-TW");
(async function boot() {
  const tk = document.cookie.split(";").map(x => x.trim()).find(x => x.startsWith("rz_token="));
  if (tk) {
    state.token = tk.split("=")[1];
    try {
      state.user = await api("/api/me");
      state.me_perms = new Set(state.user.permissions);
      state.meta = await api("/api/meta");
      state.products = state.meta.products;
    } catch (e) { state.user = null; state.token = null; }
  }
  render();
})();
