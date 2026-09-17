"""RankEZ Support platform — FastAPI app (API + static SPA)."""
import base64
import csv
import io
import json
import os
import re
import threading
import time
from datetime import datetime

import auth
import rbac
import convert
import desens
import mailer
import tickets as T
from db import get_db, init_db, get_setting, set_setting, UPLOAD_DIR, DB_PATH

from fastapi import (FastAPI, Request, Response, UploadFile, File, Form,
                     HTTPException, BackgroundTasks)
from fastapi.responses import JSONResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import RedirectResponse

# ------------------------------------------------------------------ bootstrap
FRONTEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
init_db()
app = FastAPI(title="RankEZ Support", docs_url="/api/docs", openapi_url="/api/openapi.json")


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):  # noqa: BLE001
    """Surface real errors as JSON so the UI shows a message instead of a bare 500."""
    import traceback
    traceback.print_exc()
    return JSONResponse(status_code=500, content={"detail": "%s: %s" % (type(exc).__name__, exc)})


@app.middleware("http")
async def _host_guard(request: Request, call_next):
    """Settings > Bound domains: when a list is configured, only those hosts may
    reach the site (empty list = every host, including raw IPs, is allowed)."""
    allowed = site_allowed_hosts()
    if allowed:
        host = (request.headers.get("host") or "").split(":")[0].lower()
        if host not in allowed:
            return PlainTextResponse(
                "403 - this host is not allowed to serve the site.\n"
                "Configure it under Settings > Bound domains.",
                status_code=403)
    return await call_next(request)


DEFAULT_MODULES = ["PAC", "PSM", "CPM", "VAULT", "CP", "REMOTEAPP", "CLM", "RAG"]
DEPLOY_TYPES = ["ON-PREM", "SaaS"]
LOGO_HINT = "建议上传 200 × 48 px 的 PNG / SVG（透明背景，横向），不超过 1 MB"

DEFAULT_WELCOME = """# RankEZ 支持中心

欢迎来到 **RankEZ 售后与知识库平台**。

- 提交工单并跟踪处理进度
- 检索产品文档与最佳实践
- 内部知识沉淀与共享

请点击右上角 **Sign in** 登录。
"""


def _seed_setting(conn, key, value):
    if not conn.execute("SELECT key FROM settings WHERE key=?", (key,)).fetchone():
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?)", (key, value))


def _seed():
    conn = get_db()
    rbac.seed(conn)
    # default groups ("内部用户组" = staff: reads + edits every ticket, never deletes)
    for g in ("管理员", "售后人员"):
        conn.execute("INSERT OR IGNORE INTO user_groups(name,builtin) VALUES(?,1)", (g,))
    # one-off, idempotent tidy-up of the auto-created group names: the customer
    # groups go from "客户组:X" / "客户:X" to "Customer-X", partner groups are
    # "Partner-X". Driven by the customer/partner rows, so it cannot invent a
    # group with nothing behind it.
    rbac.sync_builtin_group_names(conn)
    rbac.ensure_internal_group(conn)
    conn.commit()
    # site settings
    _seed_setting(conn, "modules", json.dumps(DEFAULT_MODULES, ensure_ascii=False))
    _seed_setting(conn, "internal_domains", "[]")
    _seed_setting(conn, "session_timeout", "480")
    _seed_setting(conn, "session_max_lifetime", "1440")
    _seed_setting(conn, "theme", "light")
    _seed_setting(conn, "company_name", "RankEZ")
    _seed_setting(conn, "company_logo", "")
    _seed_setting(conn, "welcome_md", DEFAULT_WELCOME)
    _seed_setting(conn, "allowed_hosts", "[]")
    _seed_setting(conn, "mail_provider", "smtp")
    # Registration policy: an address is only accepted when its domain belongs to
    # a customer, a partner or the internal domains (see rbac.known_domains).
    _seed_setting(conn, "require_known_domain", "1")
    # default collections
    for name, vis in (("公开知识", "public"), ("注册用户", "registered"), ("内部管理", "internal")):
        if not conn.execute("SELECT id FROM kb_collections WHERE name=?", (name,)).fetchone():
            conn.execute("INSERT INTO kb_collections(name,visibility) VALUES(?,?)", (name, vis))
    # default products
    for p in ("通用", "账号管理", "密码管理", "会话审计报告"):
        conn.execute("INSERT OR IGNORE INTO products(name) VALUES(?)", (p,))
    # admin user
    admin = conn.execute("SELECT id FROM users WHERE email=?", ("admin@rankez.local",)).fetchone()
    if not admin:
        cur = conn.execute(
            "INSERT INTO users(email,display_name,password_hash) VALUES(?,?,?)",
            ("admin@rankez.local", "Administrator", auth.hash_password("Admin@12345")))
        aid = cur.lastrowid
        role = conn.execute("SELECT id FROM roles WHERE name='管理员'").fetchone()
        conn.execute("INSERT OR IGNORE INTO user_roles(user_id,role_id) VALUES(?,?)", (aid, role["id"]))
        for gname in ("管理员", rbac.INTERNAL_GROUP):
            g = conn.execute("SELECT id FROM user_groups WHERE name=?", (gname,)).fetchone()
            if g:
                conn.execute("INSERT OR IGNORE INTO user_groups_rel(user_id,group_id) VALUES(?,?)", (aid, g["id"]))
        print("[seed] admin@rankez.local / Admin@12345")
    # make sure the existing admin also belongs to the internal group (KB edit rights)
    arow = conn.execute("SELECT id FROM users WHERE email=?", ("admin@rankez.local",)).fetchone()
    if arow:
        gi = rbac.internal_group_id(conn)
        if gi:
            conn.execute("INSERT OR IGNORE INTO user_groups_rel(user_id,group_id) VALUES(?,?)", (arow["id"], gi))
    # top up domain-driven memberships for every user (covers data written by
    # older builds). Add-only: a restart must not evict anyone.
    for r in conn.execute("SELECT id,email FROM users"):
        rbac.sync_email_groups(conn, r["id"], r["email"], remove_stale=False)
    conn.commit()
    conn.close()


_seed()


def _seed_demo_if_needed():
    c = get_db()
    if os.environ.get("RZ_SEED_DEMO", "1") == "1":
        if not c.execute("SELECT id FROM customers WHERE domains LIKE '%abc.com%'").fetchone():
            cid = c.execute("INSERT INTO customers(name,domains) VALUES('Acme Inc','abc.com')").lastrowid
            rbac.ensure_customer_group(c, cid, "Acme Inc")
        if not c.execute("SELECT id FROM kb_articles WHERE title='欢迎使用 RankEZ 知识库'").fetchone():
            c.execute("INSERT INTO kb_articles(title,body,source,visibility,collection_id,author_id) "
                      "VALUES(?,?,?,?,?,?)",
                    ("欢迎使用 RankEZ 知识库",
                     "# RankEZ Support\n\nA knowledge base article auto-desensitized from a ticket shows how customer data is masked "
                     "(e.g. `abc.com` is shown as `xxxxx.com`).\n\n## 常见问题\n- Q: 如何开工单？\n- A: 登录后点击「工单→新建工单」，选择客户并填写详情。\n\n## Demo article / 示例知识",
                     "manual", "registered",
                     (c.execute("SELECT id FROM kb_collections WHERE name='注册用户'").fetchone() or {"id": None})["id"], 1))
    c.commit()
    c.close()


_seed_demo_if_needed()


def conn_():
    return get_db()


# ------------------------------------------------------------------ site settings helpers
def _setting_json(key, default):
    c = conn_()
    raw = get_setting(c, key, None)
    c.close()
    if not raw:
        return default
    try:
        v = json.loads(raw)
        return v if isinstance(v, type(default)) else default
    except Exception:
        return default


def _setting_raw(key, default=""):
    """Plain-text setting (company name, markdown, provider, ...)."""
    c = conn_()
    raw = get_setting(c, key, None)
    c.close()
    return raw if raw not in (None, "") else default


def site_modules():
    m = _setting_json("modules", [])
    return [x for x in m if x] or DEFAULT_MODULES


def site_internal_domains():
    return [str(x).lower() for x in (_setting_json("internal_domains", []) or []) if x]


def site_allowed_hosts():
    """Hosts allowed to serve the site; empty list = allow everything."""
    return [str(x).strip().lower() for x in (_setting_json("allowed_hosts", []) or []) if str(x).strip()]


def site_company_name():
    return _setting_raw("company_name", "RankEZ") or "RankEZ"


def site_company_logo():
    return _setting_raw("company_logo", "")


def site_welcome_md():
    return _setting_raw("welcome_md", "") or DEFAULT_WELCOME


def site_mail_provider():
    return "o365" if _setting_raw("mail_provider", "smtp").lower() == "o365" else "smtp"


def site_session_timeout():
    try:
        return int(_setting_json("session_timeout", 480) or 0)
    except Exception:
        return 480


def site_session_max_lifetime():
    """Absolute cap on how long one login may live, in minutes (0 = unlimited)."""
    try:
        return int(_setting_json("session_max_lifetime", 1440) or 0)
    except Exception:
        return 1440


def site_theme():
    c = conn_()
    v = get_setting(c, "theme", "light")
    c.close()
    return v or "light"


def site_require_known_domain():
    """True = only addresses on a known customer/partner/internal domain may register."""
    c = conn_()
    v = get_setting(c, "require_known_domain", "1")
    c.close()
    return str(v) != "0"


def _registration_allowed(conn, email):
    """Registration policy gate. Returns None when allowed, else an error key."""
    if not site_require_known_domain():
        return None
    if rbac.email_domain_allowed(conn, email):
        return None
    return "domain_not_allowed"


def _resync_all_domain_groups(evict_ids=None):
    """Re-align every user's domain-driven group membership.

    Add-only by default; `evict_ids` names the groups that may also lose members
    (used when a customer's or a partner's domains change). Scoping the eviction
    keeps an unrelated membership — e.g. the administrator's manual internal-group
    seat — from being cleared by a routine domain edit.
    """
    c = conn_()
    for r in c.execute("SELECT id,email FROM users"):
        rbac.sync_email_groups(c, r["id"], r["email"], remove_stale=False)
    if evict_ids:
        for r in c.execute("SELECT id,email FROM users"):
            rbac.sync_email_groups(c, r["id"], r["email"], remove_stale=True, scope_ids=set(evict_ids))
    c.commit()
    c.close()


def _sync_internal_group(c, uid, email, remove_stale=False):
    """Apply the e-mail-domain rule: customer-domain groups + the internal group.

    Named for history — it now also keeps *customer* group membership in step
    with the address. `remove_stale` is only passed when the address itself
    changed, so an administrator's manual group assignment is never undone by a
    routine sync.
    """
    return rbac.sync_email_groups(c, uid, email, remove_stale=remove_stale)


def _kb_can_edit(conn, u):
    """Knowledge base may only be edited by the internal group or administrators."""
    if not u:
        return False
    perms = rbac.user_permissions(conn, u["id"])
    if "kb.edit" not in perms:
        return False
    if "user.manage" in perms:
        return True
    gi = rbac.internal_group_id(conn)
    if not gi:
        return False
    return gi in rbac.groups_for_user(conn, u["id"])


# ------------------------------------------------------------------ auth ctx
def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def current_user(request: Request):
    token = request.headers.get("X-Token") or request.cookies.get("rz_token")
    if not token:
        return None
    c = conn_()
    row = c.execute("SELECT * FROM tokens WHERE token=?", (token,)).fetchone()
    if not row or row["pending"]:
        c.close()
        return None
    # Two independent limits, both in minutes, 0 = unlimited:
    #   session_timeout       -> idle timeout, measured from the last request
    #   session_max_lifetime  -> absolute cap, measured from login time
    now = datetime.utcnow()
    created = _parse_dt(row["created_at"])
    last = _parse_dt(row["last_seen"] if "last_seen" in row.keys() else None) or created
    idle_min = site_session_timeout()
    max_min = site_session_max_lifetime()
    expired = False
    if idle_min > 0 and last and (now - last).total_seconds() > idle_min * 60:
        expired = True
    if max_min > 0 and created and (now - created).total_seconds() > max_min * 60:
        expired = True
    if expired:
        c.execute("DELETE FROM tokens WHERE token=?", (token,))
        c.commit()
        c.close()
        return None
    # refresh the idle clock at most once every 30s (avoid a write per request)
    if (not last) or (now - last).total_seconds() > 30:
        c.execute("UPDATE tokens SET last_seen=? WHERE token=?",
                  (now.strftime("%Y-%m-%d %H:%M:%S"), token))
        c.commit()
    u = c.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
    c.close()
    if not u or u["status"] != "active":
        return None
    return u


def require(request: Request):
    u = current_user(request)
    if not u:
        raise HTTPException(401, "not_logged_in")
    return u


def require_perm(request, key):
    u = require(request)
    c = conn_()
    ok = rbac.has_perm(c, u["id"], key)
    c.close()
    if not ok:
        raise HTTPException(403, "no_permission:" + key)
    return u


def ok(**kw):
    return JSONResponse(kw)


def fail(code, msg):
    raise HTTPException(code, msg)


# =============================== AUTH ===============================
@app.post("/api/auth/login")
async def login(request: Request):
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    pw = body.get("password") or ""
    c = conn_()
    u = c.execute("SELECT * FROM users WHERE lower(email)=?", (email,)).fetchone()
    if not u or not auth.verify_password(pw, u["password_hash"] or ""):
        c.close()
        return JSONResponse({"error": "invalid_credentials"}, 401)
    if u["status"] != "active":
        c.close()
        return JSONResponse({"error": "account_disabled"}, 403)
    if u["totp_enabled"] and u["totp_secret"]:
        code = body.get("totp") or ""
        if not auth.totp_verify(u["totp_secret"], code):
            c.close()
            return JSONResponse({"error": "totp_required"}, 200) if code else \
                JSONResponse({"need_totp": True}, 200)
    tok = auth.new_token()
    c.execute("INSERT INTO tokens(token,user_id,pending) VALUES(?,?,0)", (tok, u["id"]))
    c.commit()
    c.close()
    resp = ok(token=tok)
    resp.set_cookie("rz_token", tok, httponly=True, samesite="lax", max_age=60 * 60 * 12)
    return resp


@app.post("/api/auth/logout")
async def logout(request: Request):
    token = request.headers.get("X-Token") or request.cookies.get("rz_token")
    c = conn_()
    c.execute("DELETE FROM tokens WHERE token=?", (token or "",))
    c.commit()
    c.close()
    # the cookie is HttpOnly, so only the server can clear it
    resp = ok(ok=True)
    resp.delete_cookie("rz_token", path="/")
    return resp


def _me_payload(u):
    c = conn_()
    perms = sorted(rbac.user_permissions(c, u["id"]))
    is_internal = rbac.is_internal_user(c, u["id"])
    roles = [r["name"] for r in c.execute(
        "SELECT r.name FROM roles r JOIN user_roles ur ON ur.role_id=r.id WHERE ur.user_id=?", (u["id"],))]
    groups = [(rbac.INTERNAL_GROUP_LABEL if r["name"] == rbac.INTERNAL_GROUP else r["name"])
              for r in c.execute(
        "SELECT g.name FROM user_groups g JOIN user_groups_rel gr ON gr.group_id=g.id WHERE gr.user_id=?", (u["id"],))]
    c.close()
    return {"id": u["id"], "email": u["email"], "display_name": u["display_name"],
            "totp_enabled": bool(u["totp_enabled"]), "permissions": perms,
            "roles": roles, "groups": groups, "is_internal": is_internal}


@app.get("/api/me")
def me(request: Request):
    u = require(request)
    return _me_payload(u)


@app.get("/api/me/session")
def me_session(request: Request):
    """Heartbeat: refreshes the idle clock and reports the remaining seconds."""
    require(request)
    token = request.headers.get("X-Token") or request.cookies.get("rz_token")
    c = conn_()
    row = c.execute("SELECT * FROM tokens WHERE token=?", (token,)).fetchone()
    c.close()
    if not row:
        raise HTTPException(401, "not_logged_in")
    now = datetime.utcnow()
    created = _parse_dt(row["created_at"])
    last = _parse_dt(row["last_seen"] if "last_seen" in row.keys() else None) or created
    idle_min = site_session_timeout()
    max_min = site_session_max_lifetime()
    idle_left = max_left = None
    if idle_min > 0 and last:
        idle_left = max(0, int(idle_min * 60 - (now - last).total_seconds()))
    if max_min > 0 and created:
        max_left = max(0, int(max_min * 60 - (now - created).total_seconds()))
    return ok(idle_timeout=idle_min, idle_left=idle_left,
              max_lifetime=max_min, max_left=max_left)


@app.post("/api/me/password")
async def my_password(request: Request):
    u = require(request)
    body = await request.json()
    if not auth.verify_password(body.get("old") or "", u["password_hash"] or ""):
        fail(400, "wrong_password")
    c = conn_()
    c.execute("UPDATE users SET password_hash=? WHERE id=?", (auth.hash_password(body.get("new") or ""), u["id"]))
    c.commit()
    c.close()
    return ok(ok=True)


@app.post("/api/me/name")
async def my_name(request: Request):
    u = require(request)
    body = await request.json()
    c = conn_()
    c.execute("UPDATE users SET display_name=? WHERE id=?", ((body.get("display_name") or "").strip(), u["id"]))
    c.commit()
    c.close()
    return ok(ok=True)


@app.post("/api/me/totp/enable")
async def my_totp_enable(request: Request):
    u = require(request)
    c = conn_()
    secret = auth.new_totp_secret()
    c.execute("UPDATE users SET totp_secret=?, totp_enabled=1 WHERE id=?", (secret, u["id"]))
    c.commit()
    c.close()
    return ok(secret=secret, uri=auth.totp_uri(u["email"], secret))


@app.post("/api/me/totp/disable")
async def my_totp_disable(request: Request):
    u = require(request)
    c = conn_()
    c.execute("UPDATE users SET totp_enabled=0, totp_secret=NULL WHERE id=?", (u["id"],))
    c.commit()
    c.close()
    return ok(ok=True)


# self registration — the address must belong to a known domain (customer,
# partner or internal); membership of the matching groups is derived from it.
@app.post("/api/auth/register")
async def register(request: Request):
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    pw = body.get("password") or ""
    if "@" not in email or len(pw) < 6:
        fail(400, "invalid_input")
    c = conn_()
    err = _registration_allowed(c, email)
    if err:
        c.close()
        return JSONResponse({"error": err}, 403)
    if c.execute("SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone():
        c.close()
        return JSONResponse({"error": "email_exists"}, 409)
    cur = c.execute("INSERT INTO users(email,display_name,password_hash) VALUES(?,?,?)",
                    (email, (body.get("display_name") or email.split("@")[0]), auth.hash_password(pw)))
    uid = cur.lastrowid
    cust = T.match_customer(c, email)
    partner = T.match_partner(c, email)
    if cust:
        gid = rbac.ensure_customer_group(c, cust["id"], cust["name"])
        c.execute("INSERT OR IGNORE INTO user_groups_rel(user_id,group_id) VALUES(?,?)", (uid, gid))
    if partner:
        gid = rbac.ensure_partner_group(c, partner["id"], partner["name"])
        c.execute("INSERT OR IGNORE INTO user_groups_rel(user_id,group_id) VALUES(?,?)", (uid, gid))
    role_name = "代理商" if (partner and not cust) else "客户"
    role = c.execute("SELECT id FROM roles WHERE name=?", (role_name,)).fetchone()
    if role:
        c.execute("INSERT OR IGNORE INTO user_roles(user_id,role_id) VALUES(?,?)", (uid, role["id"]))
    _sync_internal_group(c, uid, email)
    c.commit()
    c.close()
    return ok(ok=True)


# =============================== META / PERMS ===============================
@app.get("/api/meta")
def meta(request: Request):
    c = conn_()
    perms = [{"key": r["key"], "grp": r["grp"]} for r in c.execute("SELECT * FROM permissions ORDER BY grp,key")]
    products = [r["name"] for r in c.execute("SELECT * FROM products ORDER BY id")]
    u = current_user(request)
    myperms = rbac.user_permissions(c, u["id"]) if u else {"kb.view_public"}
    can_edit_kb = _kb_can_edit(c, u) if u else False
    c.close()
    return ok(permissions=perms, products=products,
              statuses=T.STATUSES, priorities=["critical", "high", "medium", "low"],
              my_permissions=sorted(myperms),
              modules=site_modules(), deploy_types=DEPLOY_TYPES,
              session_timeout=site_session_timeout(),
              session_max_lifetime=site_session_max_lifetime(),
              theme=site_theme(), can_edit_kb=can_edit_kb)


def my_perm_set(u):
    c = conn_()
    s = rbac.user_permissions(c, u["id"]) if u else set()
    c.close()
    return s


# =============================== CUSTOMERS ===============================
def _customer_row(c, r):
    """A customer row plus the partner it was assigned to (may be None)."""
    d = dict(r)
    d["partner_name"] = ""
    if d.get("partner_id"):
        p = c.execute("SELECT name FROM partners WHERE id=?", (d["partner_id"],)).fetchone()
        d["partner_name"] = p["name"] if p else ""
    return d


def _clean_partner_id(c, raw):
    """Only an existing partner id is accepted; anything else means "no partner"."""
    try:
        pid = int(raw)
    except (TypeError, ValueError):
        return None
    if not pid:
        return None
    row = c.execute("SELECT id FROM partners WHERE id=?", (pid,)).fetchone()
    return row["id"] if row else None


@app.get("/api/customers")
def customers_list(request: Request, q: str = ""):
    require(request)
    c = conn_()
    if q:
        like = "%" + q + "%"
        rows = c.execute("SELECT * FROM customers WHERE name LIKE ? OR domains LIKE ? ORDER BY name", (like, like)).fetchall()
    else:
        rows = c.execute("SELECT * FROM customers ORDER BY name").fetchall()
    out = [_customer_row(c, r) for r in rows]
    c.close()
    return ok(items=out)


@app.post("/api/customers")
async def customer_create(request: Request):
    require_perm(request, "customer.create")
    b = await request.json()
    name = (b.get("name") or "").strip()
    domains = (b.get("domains") or "").strip().lower()
    if not name or not domains:
        fail(400, "name_and_domain_required")
    c = conn_()
    pid = _clean_partner_id(c, b.get("partner_id"))
    exists = c.execute("SELECT id FROM customers WHERE lower(name)=?", (name.lower(),)).fetchone()
    if exists:
        cid = exists["id"]
        # upsert onto an existing name: same merge rule as the PUT, so an omitted
        # key keeps its stored value instead of being blanked
        cols, args = ["domains=?"], [domains]
        for k in ("version", "service_start", "service_end", "contact_email"):
            if k in b:
                cols.append(k + "=?"); args.append(b.get(k) or "")
        if "partner_id" in b:
            cols.append("partner_id=?"); args.append(pid)
        args.append(cid)
        c.execute("UPDATE customers SET %s WHERE id=?" % ",".join(cols), args)
    else:
        cur = c.execute("INSERT INTO customers(name,domains,version,service_start,service_end,contact_email,partner_id) VALUES(?,?,?,?,?,?,?)",
                        (name, domains, b.get("version", ""), b.get("service_start", ""), b.get("service_end", ""), b.get("contact_email", ""), pid))
        cid = cur.lastrowid
    rbac.ensure_customer_group(c, cid, name)
    c.commit()
    c.close()
    # a new/edited customer domain changes who may register and who belongs to which group
    _resync_all_domain_groups(evict_ids=[g for g in [_group_id_for_customer(cid)] if g])
    return ok(id=cid)


@app.put("/api/customers/{cid}")
async def customer_update(cid: int, request: Request):
    """Partial update: only the keys present in the body are written.

    A PUT that carries just `partner_id` (the picker in the customer editor, or
    an API client assigning a reseller) must never blank the other columns.
    """
    require_perm(request, "customer.edit")
    b = await request.json()
    c = conn_()
    row = c.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
    if not row:
        c.close()
        fail(404, "not_found")
    cols, args = [], []
    if "name" in b:
        cols.append("name=?"); args.append((b.get("name") or "").strip())
    if "domains" in b:
        cols.append("domains=?"); args.append((b.get("domains") or "").strip().lower())
    for k in ("version", "service_start", "service_end", "contact_email"):
        if k in b:
            cols.append(k + "=?"); args.append(b.get(k) or "")
    # partner_id is only touched when it was actually sent, so a client that does
    # not offer the picker cannot silently unassign the customer's partner.
    if "partner_id" in b:
        cols.append("partner_id=?"); args.append(_clean_partner_id(c, b.get("partner_id")))
    if cols:
        args.append(cid)
        c.execute("UPDATE customers SET %s WHERE id=?" % ",".join(cols), args)
    newname = (b.get("name") or row["name"]) if "name" in b else row["name"]
    if newname:
        rbac.ensure_customer_group(c, cid, newname)
    c.commit()
    c.close()
    _resync_all_domain_groups(evict_ids=[g for g in [_group_id_for_customer(cid)] if g])
    return ok(ok=True)


def _group_id_for_customer(cid):
    c = conn_()
    row = c.execute("SELECT id FROM user_groups WHERE customer_id=?", (cid,)).fetchone()
    c.close()
    return row["id"] if row else None


def _drop_customer(c, cid):
    """Delete a customer and the group that was auto-created for it."""
    g = c.execute("SELECT id FROM user_groups WHERE customer_id=?", (cid,)).fetchone()
    if g:
        c.execute("DELETE FROM user_groups_rel WHERE group_id=?", (g["id"],))
        c.execute("DELETE FROM kb_collections_groups WHERE group_id=?", (g["id"],))
        c.execute("DELETE FROM kb_article_groups WHERE group_id=?", (g["id"],))
        c.execute("DELETE FROM user_groups WHERE id=?", (g["id"],))
    c.execute("DELETE FROM customers WHERE id=?", (cid,))


@app.delete("/api/customers/{cid}")
def customer_delete(cid: int, request: Request):
    require_perm(request, "customer.delete")
    c = conn_()
    _drop_customer(c, cid)
    c.commit()
    c.close()
    return ok(ok=True)


@app.post("/api/customers/bulk")
async def customer_bulk(request: Request):
    require_perm(request, "customer.delete")
    b = await request.json()
    ids = [int(x) for x in (b.get("ids") or []) if str(x).isdigit()]
    if not ids:
        fail(400, "no_selection")
    c = conn_()
    for cid in ids:
        _drop_customer(c, cid)
    c.commit()
    c.close()
    return ok(ok=True, changed=len(ids))


CUSTOMER_CSV_HEADER = ["name", "domains", "version", "service_start", "service_end", "contact_email"]


@app.get("/api/customers/template.csv")
def customer_template(request: Request):
    require(request)
    csv_data = ",".join(CUSTOMER_CSV_HEADER) + "\n" + \
        "Acme Corp,acme.com,2.4,2025-01-01,2026-12-31,it@acme.com\n"
    return Response(content=csv_data.encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=customers_template.csv"})


@app.post("/api/customers/import")
async def customer_import(request: Request, file: UploadFile = File(...)):
    require_perm(request, "customer.import_csv")
    raw = await file.read()
    if raw[:3] == b"\xef\xbb\xbf":
        raw = raw[3:]
    text = raw.decode("utf-8", "replace")
    reader = csv.DictReader(io.StringIO(text))
    c = conn_()
    created = updated = 0
    for row in reader:
        name = (row.get("name") or "").strip()
        domains = (row.get("domains") or "").strip().lower()
        if not name:
            continue
        exists = c.execute("SELECT id FROM customers WHERE lower(name)=?", (name.lower(),)).fetchone()
        if exists:
            c.execute("UPDATE customers SET domains=COALESCE(NULLIF(?,''),domains), version=?, service_start=?, service_end=?, contact_email=? WHERE id=?",
                      (domains, row.get("version", ""), row.get("service_start", ""), row.get("service_end", ""),
                       row.get("contact_email", ""), exists["id"]))
            updated += 1
            cid = exists["id"]
        else:
            cur = c.execute("INSERT INTO customers(name,domains,version,service_start,service_end,contact_email) VALUES(?,?,?,?,?,?)",
                            (name, domains, row.get("version", ""), row.get("service_start", ""), row.get("service_end", ""), row.get("contact_email", "")))
            cid = cur.lastrowid
            created += 1
        rbac.ensure_customer_group(c, cid, name)
    c.commit()
    c.close()
    return ok(created=created, updated=updated)


# =============================== PARTNERS (代理商) ===============================
def _partner_row(c, r):
    """A partner plus how many customers point at it and how many users it reaches."""
    d = dict(r)
    d["customers"] = [x["name"] for x in
                      c.execute("SELECT name FROM customers WHERE partner_id=? ORDER BY name", (r["id"],))]
    d["customer_count"] = len(d["customers"])
    g = c.execute("SELECT id FROM user_groups WHERE partner_id=?", (r["id"],)).fetchone()
    d["group_id"] = g["id"] if g else None
    d["group_name"] = rbac.PARTNER_GROUP_PREFIX + (r["name"] or "").strip()
    d["member_count"] = c.execute(
        "SELECT COUNT(*) n FROM user_groups_rel WHERE group_id=?", (g["id"],)).fetchone()["n"] if g else 0
    return d


@app.get("/api/partners")
def partners_list(request: Request, q: str = ""):
    require_perm(request, "partner.view")
    c = conn_()
    if q:
        like = "%" + q + "%"
        rows = c.execute("SELECT * FROM partners WHERE name LIKE ? OR domains LIKE ? ORDER BY name",
                         (like, like)).fetchall()
    else:
        rows = c.execute("SELECT * FROM partners ORDER BY name").fetchall()
    out = [_partner_row(c, r) for r in rows]
    c.close()
    return ok(items=out)


@app.post("/api/partners")
async def partner_create(request: Request):
    require_perm(request, "partner.create")
    b = await request.json()
    name = (b.get("name") or "").strip()
    domains = (b.get("domains") or "").strip().lower()
    if not name or not domains:
        fail(400, "name_and_domain_required")
    c = conn_()
    if c.execute("SELECT id FROM partners WHERE lower(name)=?", (name.lower(),)).fetchone():
        c.close()
        fail(409, "partner_name_taken")
    cur = c.execute("INSERT INTO partners(name,domains,contact_email,description) VALUES(?,?,?,?)",
                    (name, domains, b.get("contact_email", ""), b.get("description", "")))
    pid = cur.lastrowid
    rbac.ensure_partner_group(c, pid, name)
    c.commit()
    c.close()
    # the new domains may already cover existing users
    _resync_all_domain_groups(evict_ids=[g for g in [_group_id_for_partner(pid)] if g])
    return ok(id=pid)


@app.put("/api/partners/{pid}")
async def partner_update(pid: int, request: Request):
    require_perm(request, "partner.edit")
    b = await request.json()
    c = conn_()
    row = c.execute("SELECT * FROM partners WHERE id=?", (pid,)).fetchone()
    if not row:
        c.close()
        fail(404, "not_found")
    name = (b.get("name") or row["name"]).strip() or row["name"]
    domains = (b.get("domains") if b.get("domains") is not None else row["domains"]) or ""
    dup = c.execute("SELECT id FROM partners WHERE lower(name)=? AND id<>?", (name.lower(), pid)).fetchone()
    if dup:
        c.close()
        fail(409, "partner_name_taken")
    c.execute("UPDATE partners SET name=?, domains=?, contact_email=?, description=? WHERE id=?",
              (name, domains.strip().lower(), b.get("contact_email", row["contact_email"]),
               b.get("description", row["description"]), pid))
    rbac.ensure_partner_group(c, pid, name)
    c.commit()
    c.close()
    _resync_all_domain_groups(evict_ids=[g for g in [_group_id_for_partner(pid)] if g])
    return ok(ok=True)


def _group_id_for_partner(pid):
    c = conn_()
    row = c.execute("SELECT id FROM user_groups WHERE partner_id=?", (pid,)).fetchone()
    c.close()
    return row["id"] if row else None


def _drop_partner(c, pid):
    """Delete a partner: its customers are unlinked, its group disappears."""
    c.execute("UPDATE customers SET partner_id=NULL WHERE partner_id=?", (pid,))
    g = c.execute("SELECT id FROM user_groups WHERE partner_id=?", (pid,)).fetchone()
    if g:
        c.execute("DELETE FROM user_groups_rel WHERE group_id=?", (g["id"],))
        c.execute("DELETE FROM kb_collections_groups WHERE group_id=?", (g["id"],))
        c.execute("DELETE FROM kb_article_groups WHERE group_id=?", (g["id"],))
        c.execute("DELETE FROM user_groups WHERE id=?", (g["id"],))
    c.execute("DELETE FROM partners WHERE id=?", (pid,))


@app.delete("/api/partners/{pid}")
def partner_delete(pid: int, request: Request):
    require_perm(request, "partner.delete")
    c = conn_()
    if not c.execute("SELECT id FROM partners WHERE id=?", (pid,)).fetchone():
        c.close()
        fail(404, "not_found")
    unlinked = c.execute("SELECT COUNT(*) n FROM customers WHERE partner_id=?", (pid,)).fetchone()["n"]
    _drop_partner(c, pid)
    c.commit()
    c.close()
    return ok(ok=True, unlinked=unlinked)


@app.post("/api/partners/bulk")
async def partner_bulk(request: Request):
    require_perm(request, "partner.delete")
    b = await request.json()
    ids = [int(x) for x in (b.get("ids") or []) if str(x).isdigit()]
    if not ids:
        fail(400, "no_selection")
    c = conn_()
    unlinked = 0
    for pid in ids:
        unlinked += c.execute("SELECT COUNT(*) n FROM customers WHERE partner_id=?", (pid,)).fetchone()["n"]
        _drop_partner(c, pid)
    c.commit()
    c.close()
    return ok(ok=True, changed=len(ids), unlinked=unlinked)


# =============================== TICKETS ===============================
def _ticket_visible(conn, t, u):
    perms = rbac.user_permissions(conn, u["id"])
    if "ticket.view_all" in perms:
        return True
    if "ticket.view_own" in perms:
        if t["creator_id"] == u["id"] or t["owner_id"] == u["id"]:
            return True
        # customer group sees tickets of its customer
        gids = rbac.groups_for_user(conn, u["id"], u["email"])
        if t["customer_id"]:
            g = conn.execute("SELECT id FROM user_groups WHERE customer_id=?", (t["customer_id"],)).fetchone()
            if g and g["id"] in gids:
                return True
    # a partner may read every ticket of the customers assigned to it
    if "ticket.view_partner" in perms and t["customer_id"]:
        if t["customer_id"] in rbac.partner_customer_ids(conn, u["id"]):
            return True
    return False


@app.get("/api/tickets/assignees")
def ticket_assignees(request: Request):
    """The people a ticket may be handed to -- internal users only.

    Registered BEFORE /api/tickets/{tid} on purpose: Starlette matches routes in
    registration order, and the int path param would swallow "assignees" into a
    failed cast (422) instead of letting this route answer.
    """
    require_perm(request, "ticket.change_owner")
    c = conn_()
    items = rbac.internal_users(c)
    c.close()
    return ok(items=items, total=len(items))


@app.get("/api/tickets")
def tickets_list(request: Request, status: str = "", owner: str = "", customer: str = "",
                 priority: str = "", product: str = "", q: str = "",
                 date_from: str = "", date_to: str = "", module: str = "",
                 deploy_type: str = ""):
    u = require(request)
    c = conn_()
    sql = "SELECT * FROM tickets WHERE 1=1"
    args = []
    if status:
        sql += " AND status=?"; args.append(status)
    if priority:
        sql += " AND priority=?"; args.append(priority)
    if module and not product:
        product = module
    if product:
        sql += " AND product=?"; args.append(product)
    if deploy_type:
        sql += " AND deploy_type=?"; args.append(deploy_type)
    if customer:
        sql += " AND customer_name LIKE ?"; args.append("%" + customer + "%")
    if owner:
        sql += " AND owner_id=(SELECT id FROM users WHERE display_name LIKE ? OR email LIKE ? LIMIT 1)"
        args += ["%" + owner + "%", "%" + owner + "%"]
    if date_from:
        sql += " AND date(created_at)>=?"; args.append(date_from)
    if date_to:
        sql += " AND date(created_at)<=?"; args.append(date_to)
    if q:
        sql += " AND (title LIKE ? OR code LIKE ? OR description LIKE ?)"
        args += ["%" + q + "%"] * 3
    sql += " ORDER BY id DESC LIMIT 500"
    rows = [dict(r) for r in c.execute(sql, args)]
    perms = rbac.user_permissions(c, u["id"])
    gids = rbac.groups_for_user(c, u["id"], u["email"]) if "ticket.view_own" in perms else set()
    partner_cids = rbac.partner_customer_ids(c, u["id"]) if "ticket.view_partner" in perms else set()
    out = []
    for t in rows:
        vis = True
        if "ticket.view_all" not in perms:
            vis = False
            if "ticket.view_own" in perms:
                if t["creator_id"] == u["id"] or t["owner_id"] == u["id"]:
                    vis = True
                elif t["customer_id"]:
                    g = c.execute("SELECT id FROM user_groups WHERE customer_id=?", (t["customer_id"],)).fetchone()
                    if g and g["id"] in gids:
                        vis = True
            if not vis and t["customer_id"] and t["customer_id"] in partner_cids:
                vis = True
        if not vis:
            continue
        owner_name = ""
        if t["owner_id"]:
            orow = c.execute("SELECT display_name,email FROM users WHERE id=?", (t["owner_id"],)).fetchone()
            owner_name = (orow["display_name"] or orow["email"]) if orow else ""
        t["owner_name"] = owner_name
        out.append(t)
    c.close()
    return ok(items=out)


@app.get("/api/tickets/{tid}")
def ticket_get(tid: int, request: Request):
    u = require(request)
    c = conn_()
    t = c.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    if not t:
        c.close()
        fail(404, "not_found")
    if not _ticket_visible(c, t, u):
        c.close()
        fail(403, "no_permission")
    msgs = [dict(m) for m in c.execute("SELECT * FROM messages WHERE ticket_id=? ORDER BY id", (tid,))]
    atts = [dict(a) for a in c.execute("SELECT * FROM attachments WHERE ticket_id=? OR message_id IN (SELECT id FROM messages WHERE ticket_id=?)", (tid, tid))]
    parts = [r["email"] for r in c.execute("SELECT email FROM ticket_participants WHERE ticket_id=?", (tid,))]
    # The desk's privileges are group-based: membership of the internal group
    # decides both the workflow buttons and whether an internal note is visible
    # at all -- a role alone must not expose a note the customer cannot see.
    internal_user = rbac.is_internal_user(c, u["id"])
    if not internal_user:
        msgs = [m for m in msgs if not m["internal"]]
    for m in msgs:
        m["attachments"] = [a for a in atts if a["message_id"] == m["id"]]
    # The detail page names the assignee, so resolve it here exactly like the
    # list endpoint does (the raw row only carries owner_id).
    td = dict(t)
    td["owner_name"] = ""
    if td.get("owner_id"):
        orow = c.execute("SELECT display_name,email FROM users WHERE id=?", (td["owner_id"],)).fetchone()
        if orow:
            td["owner_name"] = orow["display_name"] or orow["email"]
    c.close()
    return ok(ticket=td, messages=msgs, participants=parts, internal_user=internal_user)


@app.post("/api/tickets")
async def ticket_create(request: Request):
    require_perm(request, "ticket.create")
    u = require(request)
    form = await request.form()
    title = (form.get("title") or "").strip()
    if not title:
        fail(400, "title_required")
    attachments = []
    for f in form.getlist("files"):
        try:
            data = await f.read()
            attachments.append({"filename": f.filename, "data": data, "content_type": f.content_type})
        except Exception:
            pass
    c = conn_()
    perms = rbac.user_permissions(c, u["id"])
    cust_name = (form.get("customer_name") or "").strip()
    internal = 1 if str(form.get("internal", "")) in ("1", "true", "on") else 0
    if "ticket.view_all" not in perms:
        # A customer user reaches tickets through their customer group only, so a
        # ticket they file must belong to that customer -- never to a name they
        # typed (which used to auto-create a customer they then could not see).
        internal = 0
        own = rbac.customer_groups_for_user(c, u["id"])
        if own:
            names = [x["name"] for x in own]
            if cust_name.lower() not in [n.lower() for n in names]:
                cust_name = names[0]
    t = T.create_ticket(
        c, title=title, description=form.get("description") or "",
        customer_name=cust_name, version=form.get("version") or "",
        product=form.get("product") or "", priority=form.get("priority") or "medium",
        creator_id=u["id"], creator_email=u["email"], internal=internal, attachments=attachments)
    dep = form.get("deploy_type") or ""
    if dep:
        c.execute("UPDATE tickets SET deploy_type=? WHERE id=?", (dep, t["id"]))
    c.commit()
    c.close()
    if not internal:
        threading.Thread(target=T.notify_new_ticket, args=(conn_(), t), daemon=True).start()
    return ok(id=t["id"], code=t["code"])


@app.post("/api/tickets/{tid}/claim")
def ticket_claim(tid: int, request: Request):
    require_perm(request, "ticket.claim")
    u = require(request)
    c = conn_()
    c.execute("UPDATE tickets SET owner_id=?, updated_at=datetime('now') WHERE id=? AND owner_id IS NULL", (u["id"], tid))
    c.commit()
    c.close()
    return ok(ok=True)


@app.put("/api/tickets/{tid}/owner")
async def ticket_owner(tid: int, request: Request):
    """Hand a ticket to a colleague, or take it back with owner_id=null.

    Only desk staff may be picked -- internal users, i.e. an account whose
    e-mail suffix matches a configured internal domain (or that an administrator
    put in the internal group). Assigning a customer or a partner is refused.
    """
    require_perm(request, "ticket.change_owner")
    b = await request.json()
    new_owner = b.get("owner_id")
    c = conn_()
    t = c.execute("SELECT id FROM tickets WHERE id=?", (tid,)).fetchone()
    if not t:
        c.close()
        fail(404, "not_found")
    if new_owner in (None, "", 0):
        new_owner = None
    else:
        try:
            new_owner = int(new_owner)
        except (TypeError, ValueError):
            c.close()
            fail(400, "bad_owner")
        row = c.execute("SELECT id FROM users WHERE id=?", (new_owner,)).fetchone()
        if not row:
            c.close()
            fail(404, "user_not_found")
        if not rbac.is_internal_user(c, new_owner):
            c.close()
            fail(403, "assignee_not_internal")
    c.execute("UPDATE tickets SET owner_id=?, updated_at=datetime('now') WHERE id=?", (new_owner, tid))
    c.commit()
    c.close()
    return ok(ok=True, owner_id=new_owner)


ALLOWED_STATUS_EDIT = {"new", "closed", "customer_replied", "support_replied"}


@app.put("/api/tickets/{tid}/status")
async def ticket_status(tid: int, request: Request):
    """Workflow status editor -- internal desk only.

    A customer never picks "售后已答复" by hand: the reply itself drives the
    status (see ticket_reply and mailer.process_incoming_email). Customers and
    partners only ever *close* a ticket, through ticket_close below.
    """
    u = require(request)
    b = await request.json()
    new_status = b.get("status")
    if new_status not in ALLOWED_STATUS_EDIT:
        fail(400, "bad_status")
    c = conn_()
    t = c.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    if not t:
        c.close()
        fail(404, "not_found")
    if not rbac.is_internal_user(c, u["id"]):
        c.close()
        fail(403, "internal_only")
    c.execute("UPDATE tickets SET status=?, updated_at=datetime('now') WHERE id=?", (new_status, tid))
    c.commit()
    # archive on close
    if new_status == "closed" and (_truthy(b.get("share_kb")) or _truthy(b.get("archive"))):
        archive = T.archive_to_kb(c, tid,
                                  visibility=_kb_visibility(b.get("kb_visibility"), True),
                                  desensitize=_truthy(b.get("kb_desensitize"), "1"))
        c.close()
        return ok(ok=True, archived=archive, shared=True)
    c.close()
    return ok(ok=True)


KB_VISIBILITIES = ("public", "registered", "internal")


def _truthy(v, default="0"):
    return str(default if v is None else v).strip().lower() in ("1", "true", "on", "yes")


def _kb_visibility(value, internal):
    """Normalise a requested KB visibility.

    Customers and partners close their own tickets and may publish the result,
    but the most they get to ask for is "registered users" -- picking an
    audience wider or narrower than that is the desk's call.
    """
    v = str(value or "").strip().lower()
    if v not in KB_VISIBILITIES:
        v = "registered"
    if not internal and v != "registered":
        v = "registered"
    return v


@app.post("/api/tickets/{tid}/close")
async def ticket_close(tid: int, request: Request):
    """Close a ticket -- the one action every user may perform.

    Anyone who can *see* the ticket may close it, and whoever closes it decides
    whether the solution goes to the knowledge base. The customer / partner side
    is asked in a dialog that is ticked by default and masks the identity data;
    the desk may also publish the raw thread with its attachments.
    """
    u = require(request)
    try:
        b = await request.json()
    except Exception:
        b = {}
    c = conn_()
    t = c.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    if not t:
        c.close()
        fail(404, "not_found")
    if not _ticket_visible(c, t, u):
        c.close()
        fail(403, "no_permission")
    internal = rbac.is_internal_user(c, u["id"])
    # `archive` is the legacy name of the same switch, kept so an old client
    # keeps working.
    share = _truthy(b.get("share_kb")) or _truthy(b.get("archive"))
    des = _truthy(b.get("kb_desensitize"), "1")
    aid = None
    if share:
        aid = T.archive_to_kb(c, tid, visibility=_kb_visibility(b.get("kb_visibility"), internal),
                              desensitize=des)
    else:
        c.execute("UPDATE tickets SET status='closed', updated_at=datetime('now') WHERE id=?", (tid,))
        c.commit()
    c.close()
    return ok(ok=True, shared=bool(aid), archived=aid)


@app.post("/api/tickets/{tid}/share_kb")
async def ticket_share_kb(tid: int, request: Request):
    """Publish a ticket thread to the knowledge base -- the desk only.

    The thread becomes a searchable KB article whose audience is picked here:
    public / registered users / internal only. Publishing twice updates the same
    article; the ticket is left open, sharing is not closing.
    """
    u = require(request)
    try:
        b = await request.json()
    except Exception:
        b = {}
    c = conn_()
    t = c.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    if not t:
        c.close()
        fail(404, "not_found")
    if not _ticket_visible(c, t, u):
        c.close()
        fail(403, "no_permission")
    if not rbac.is_internal_user(c, u["id"]):
        c.close()
        fail(403, "internal_only")
    vis = _kb_visibility(b.get("visibility"), True)
    des = _truthy(b.get("desensitize"), "1")
    aid = T.archive_to_kb(c, tid, visibility=vis, desensitize=des, close_ticket=False)
    c.close()
    return ok(ok=True, article_id=aid, visibility=vis, desensitized=des)


@app.post("/api/tickets/{tid}/reply")
async def ticket_reply(tid: int, request: Request):
    """Add a message and let the reply itself drive the workflow status.

    * an internal (staff) answer      -> status 售后已答复, the customer is told
    * a customer / partner answer     -> status 客户已答复, the desk is told
    * an internal note (never public) -> status untouched, nobody is emailed
    """
    require_perm(request, "ticket.reply")
    u = require(request)
    form = await request.form()
    body = form.get("body") or ""
    internal = 1 if str(form.get("internal", "")) in ("1", "true", "on") else 0
    attachments = []
    for f in form.getlist("files"):
        try:
            data = await f.read()
            attachments.append({"filename": f.filename, "data": data, "content_type": f.content_type})
        except Exception:
            pass
    c = conn_()
    t = c.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    if not t:
        c.close()
        fail(404, "not_found")
    if not _ticket_visible(c, t, u):
        c.close()
        fail(403, "no_permission")
    is_internal = rbac.is_internal_user(c, u["id"])
    # only the desk may write a note the customer is not supposed to see
    if internal and not is_internal:
        internal = 0
    T.add_message(c, tid, body=body, user_id=u["id"], author_email=u["email"],
                  author_name=u["display_name"], internal=internal, source="web",
                  attachments=attachments)
    new_status = None
    if not internal:
        new_status = "support_replied" if is_internal else "customer_replied"
        c.execute("UPDATE tickets SET status=?, updated_at=datetime('now') "
                  "WHERE id=? AND status!='closed'", (new_status, tid))
    c.commit()
    c.close()
    # email notification on a public message
    if not internal:
        threading.Thread(target=_notify_reply_web, args=(conn_(), tid, u["id"], is_internal),
                         daemon=True).start()
    return ok(ok=True, status=new_status)


def _ticket_mail_audience(c, t, author_email, from_staff):
    """Who should receive the "a reply was added" mail.

    * internal ticket  -> the desk only
    * staff answer     -> the customer side (that is the "通知客户" part)
    * customer answer  -> the desk
    """
    author = (author_email or "").lower()
    staff = {e.lower() for e in mailer._support_emails(c)}
    parts = [r["email"] for r in c.execute(
        "SELECT email FROM ticket_participants WHERE ticket_id=?", (t["id"],))]
    if t["customer_id"]:
        cust = c.execute("SELECT contact_email FROM customers WHERE id=?", (t["customer_id"],)).fetchone()
        if cust and cust["contact_email"]:
            parts.append(cust["contact_email"])
    parts = {e.lower() for e in parts if e and "@" in e}
    parts.discard(author)
    if t["internal"]:
        return sorted(parts & staff)
    if from_staff:
        return sorted(parts - staff)
    return sorted(parts | staff)


def _notify_reply_web(c, tid, uid, from_staff=False):
    t = c.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    if not t:
        return
    arow = c.execute("SELECT email FROM users WHERE id=?", (uid,)).fetchone()
    author = (arow["email"] if arow else "") or t["creator_email"] or ""
    parts = _ticket_mail_audience(c, t, author, from_staff)
    if not parts:
        return
    msgs = c.execute("SELECT * FROM messages WHERE ticket_id=? ORDER BY id", (tid,)).fetchall()
    htmlb = mailer._render_email(c, t, msgs, get_setting(c, "base_url", ""))
    text = ("Support replied on ticket %s." % t["code"]) if from_staff \
        else ("A reply was added to ticket %s." % t["code"])
    mailer.send_email(c, parts, "Re:[#%s] %s" % (t["code"], t["title"]), text, htmlb)


# =============================== KNOWLEDGE BASE ===============================
VIS_LEVEL = {"public": 0, "registered": 1, "internal": 2, "usergroup": 3}


def _kb_access_ok(conn, u, article, group_ids):
    """group_ids: caller-cached user group set"""
    if not u:
        return article["visibility"] == "public"
    perms = rbac.user_permissions(conn, u["id"])
    if article["visibility"] == "internal" and "kb.view_internal" not in perms:
        return False
    if article["visibility"] == "registered" and "kb.view_registered" not in perms:
        return False
    # Visibility is now a simple three-way choice (public / registered / internal);
    # the old per-user-group binding is no longer used.
    return True


@app.get("/api/kb/articles")
def kb_list(request: Request, collection: int = None, q: str = "", vis: str = "",
            module: str = ""):
    u = current_user(request)
    c = conn_()
    gids = rbac.groups_for_user(c, u["id"], u["email"]) if u else set()
    sql = "SELECT * FROM kb_articles WHERE 1=1"
    args = []
    if collection:
        sql += " AND collection_id=?"; args.append(collection)
    if vis:
        sql += " AND visibility=?"; args.append(vis)
    if module:
        sql += " AND module=?"; args.append(module)
    if q:
        sql += " AND (title LIKE ? OR body LIKE ?)"; args += ["%" + q + "%"] * 2
    sql += " ORDER BY id DESC LIMIT 500"
    rows = [dict(r) for r in c.execute(sql, args)]
    out = []
    for a in rows:
        if _kb_access_ok(c, u, a, gids):
            out.append(a)
    c.close()
    return ok(items=[{k: v for k, v in a.items() if k != "body"} for a in out])


@app.get("/api/kb/articles/{aid}")
def kb_get(aid: int, request: Request):
    u = current_user(request)
    c = conn_()
    a = c.execute("SELECT * FROM kb_articles WHERE id=?", (aid,)).fetchone()
    if not a:
        c.close()
        fail(404, "not_found")
    gids = rbac.groups_for_user(c, u["id"], u["email"]) if u else set()
    if not _kb_access_ok(c, u, a, gids):
        c.close()
        fail(403, "login_required" if not u else "no_permission")
    atts = [dict(r) for r in c.execute("SELECT * FROM attachments WHERE article_id=?", (aid,))]
    groups = [r["group_id"] for r in c.execute("SELECT group_id FROM kb_article_groups WHERE article_id=?", (aid,))]
    can_edit = _kb_can_edit(c, u)
    c.close()
    a = dict(a)
    a["group_ids"] = groups
    return ok(article=a, attachments=atts, can_edit=can_edit)


@app.post("/api/kb/articles")
async def kb_create(request: Request):
    require_perm(request, "kb.create")
    u = require(request)
    b = await request.json()
    c = conn_()
    if not _kb_can_edit(c, u):
        c.close()
        fail(403, "no_permission")
    vis = b.get("visibility") or "registered"
    if vis == "internal" and "kb.view_internal" not in rbac.user_permissions(c, u["id"]):
        c.close()
        fail(403, "no_permission")
    cur = c.execute(
        "INSERT INTO kb_articles(title,body,source,visibility,module,collection_id,author_id) VALUES(?,?,?,?,?,?,?)",
        (b.get("title", ""), b.get("body", ""), "manual", vis, b.get("module", ""),
         b.get("collection_id"), u["id"]))
    aid = cur.lastrowid
    for gid in (b.get("group_ids") or []):
        c.execute("INSERT OR IGNORE INTO kb_article_groups(article_id,group_id) VALUES(?,?)", (aid, gid))
    c.commit()
    c.close()
    return ok(id=aid)


@app.put("/api/kb/articles/{aid}")
async def kb_update(aid: int, request: Request):
    require_perm(request, "kb.edit")
    u = require(request)
    b = await request.json()
    c = conn_()
    a = c.execute("SELECT * FROM kb_articles WHERE id=?", (aid,)).fetchone()
    if not a:
        c.close()
        fail(404, "not_found")
    if not _kb_can_edit(c, u):
        c.close()
        fail(403, "no_permission")
    vis = b.get("visibility") or a["visibility"]
    if vis == "internal" and "kb.view_internal" not in rbac.user_permissions(c, u["id"]):
        c.close()
        fail(403, "no_permission")
    c.execute("UPDATE kb_articles SET title=?, body=?, visibility=?, module=?, collection_id=?, updated_at=datetime('now') WHERE id=?",
              (b.get("title", a["title"]), b.get("body", a["body"]), vis,
               b.get("module", a["module"] or ""), b.get("collection_id"), aid))
    c.execute("DELETE FROM kb_article_groups WHERE article_id=?", (aid,))
    for gid in (b.get("group_ids") or []):
        c.execute("INSERT OR IGNORE INTO kb_article_groups(article_id,group_id) VALUES(?,?)", (aid, gid))
    c.commit()
    c.close()
    return ok(ok=True)


@app.delete("/api/kb/articles/{aid}")
def kb_delete(aid: int, request: Request):
    require_perm(request, "kb.delete")
    u = require(request)
    c = conn_()
    if not _kb_can_edit(c, u):
        c.close()
        fail(403, "no_permission")
    c.execute("DELETE FROM kb_articles WHERE id=?", (aid,))
    c.commit()
    c.close()
    return ok(ok=True)


@app.post("/api/kb/import")
async def kb_import(request: Request, collection: int = Form(None), visibility: str = Form("registered")):
    require_perm(request, "kb.import")
    u = require(request)
    c = conn_()
    if visibility == "internal" and "kb.view_internal" not in rbac.user_permissions(c, u["id"]):
        c.close()
        fail(403, "no_permission")
    form = await request.form()
    imported = 0
    for f in request.FILES and form.getlist("files") or []:
        name = f.filename
        raw = await f.read()
        title = os.path.splitext(name)[0]
        ext = os.path.splitext(name)[1].lower()
        if ext in (".md", ".markdown", ".txt", ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".html", ".htm"):
            body = convert.bytes_to_md(name, raw)
            if not body:
                body = "# " + title
        else:
            body = "# " + title
        c.execute("INSERT INTO kb_articles(title,body,source,visibility,collection_id,author_id) VALUES(?,?,?,?,?,?)",
                  (title, body, "import", visibility, collection, u["id"]))
        imported += 1
    c.commit()
    c.close()
    return ok(imported=imported)


@app.get("/api/kb/collections")
def kb_collections(request: Request):
    u = current_user(request)
    c = conn_()
    rows = [dict(r) for r in c.execute("SELECT * FROM kb_collections ORDER BY id")]
    out = []
    gids = rbac.groups_for_user(c, u["id"], u["email"]) if u else set()
    perms = rbac.user_permissions(c, u["id"]) if u else set()
    for col in rows:
        if col["visibility"] == "public" or \
           (col["visibility"] == "registered" and "kb.view_registered" in perms) or \
           (col["visibility"] == "internal" and "kb.view_internal" in perms):
            out.append(col)
    c.close()
    return ok(items=out)


@app.post("/api/kb/collections")
async def kb_collection_create(request: Request):
    require_perm(request, "kb.manage_collections")
    b = await request.json()
    c = conn_()
    cur = c.execute("INSERT INTO kb_collections(name,visibility,description) VALUES(?,?,?)",
                    (b.get("name"), b.get("visibility", "registered"), b.get("description", "")))
    cid = cur.lastrowid
    for gid in (b.get("group_ids") or []):
        c.execute("INSERT OR IGNORE INTO kb_collections_groups(collection_id,group_id) VALUES(?,?)", (cid, gid))
    c.commit()
    c.close()
    return ok(id=cid)


@app.delete("/api/kb/collections/{cid}")
def kb_collection_delete(cid: int, request: Request):
    require_perm(request, "kb.manage_collections")
    c = conn_()
    c.execute("DELETE FROM kb_collections WHERE id=?", (cid,))
    c.commit()
    c.close()
    return ok(ok=True)


@app.get("/api/kb/export/{aid}")
def kb_export_pdf(aid: int, request: Request):
    u = require_perm(request, "kb.export_pdf")
    c = conn_()
    a = c.execute("SELECT * FROM kb_articles WHERE id=?", (aid,)).fetchone()
    gids = rbac.groups_for_user(c, u["id"], u["email"])
    if not a or not _kb_access_ok(c, u, a, gids):
        c.close()
        fail(403, "no_permission")
    c.close()
    try:
        import markdown
        from weasyprint import HTML
        htmlb = "<meta charset='utf-8'><style>body{font-family:sans-serif}pre{white-space:pre-wrap}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px}</style>" + \
            markdown.markdown(a["body"] or a["title"], extensions=["tables", "fenced_code"])
        pdf = HTML(string=htmlb).write_pdf()
        return Response(content=pdf, media_type="application/pdf",
                        headers={"Content-Disposition": "attachment; filename=kb_%d.pdf" % aid})
    except Exception as e:
        return PlainTextResponse("PDF export failed: %s" % e, status_code=500)


@app.post("/api/kb/share")
async def kb_share(request: Request):
    require_perm(request, "kb.share_email")
    b = await request.json()
    aid = b.get("article_id")
    to = (b.get("email") or "").strip()
    c = conn_()
    a = c.execute("SELECT * FROM kb_articles WHERE id=?", (aid,)).fetchone()
    base = get_setting(c, "base_url", "")
    link = "%s/#/kb/%s" % (base, aid) if base else "/#/kb/%s" % aid
    msg = "A knowledge base article is shared with you: %s\nLink: %s\n" % (a["title"], link)
    if a["visibility"] != "public":
        msg += "\nThis article requires login to access."
    okmail = mailer.send_email(c, [to], "[RankEZ KB] %s" % a["title"], msg)
    c.close()
    return ok(sent=bool(okmail), link=link)


# =============================== SEARCH ===============================
@app.get("/api/search")
def search(request: Request, q: str = ""):
    u = current_user(request)
    if not q:
        return ok(tickets=[], articles=[])
    c = conn_()
    perms = rbac.user_permissions(c, u["id"]) if u else {"kb.view_public"}
    gids = rbac.groups_for_user(c, u["id"], u["email"]) if u else set()
    like = "%" + q + "%"
    trows = []
    if "ticket.view_all" in perms or "ticket.view_own" in perms:
        for t in c.execute("SELECT id,code,title,status FROM tickets WHERE title LIKE ? OR code LIKE ? OR description LIKE ? ORDER BY id DESC LIMIT 30",
                           (like, like, like)):
            t = dict(t)
            t2 = c.execute("SELECT * FROM tickets WHERE id=?", (t["id"],)).fetchone()
            if _ticket_visible(c, t2, u):
                trows.append(t)
    arows = []
    for a in c.execute("SELECT id,title,visibility FROM kb_articles WHERE title LIKE ? OR body LIKE ? ORDER BY id DESC LIMIT 50",
                       (like, like)):
        a = dict(a)
        art = c.execute("SELECT * FROM kb_articles WHERE id=?", (a["id"],)).fetchone()
        if _kb_access_ok(c, u, art, gids):
            arows.append(a)
    c.close()
    return ok(tickets=trows, articles=arows)


# =============================== ADMIN: USERS ===============================
@app.get("/api/admin/users")
def admin_users(request: Request, q: str = ""):
    require_perm(request, "user.manage")
    c = conn_()
    sql = "SELECT * FROM users"
    args = []
    if q:
        sql += " WHERE email LIKE ? OR display_name LIKE ?"
        args = ["%" + q + "%"] * 2
    rows = []
    for r in c.execute(sql + " ORDER BY id", args):
        d = dict(r)
        d.pop("password_hash", None); d.pop("totp_secret", None)
        d["roles"] = [x["name"] for x in c.execute(
            "SELECT r.name FROM roles r JOIN user_roles ur ON ur.role_id=r.id WHERE ur.user_id=?", (r["id"],))]
        rows.append(d)
    c.close()
    return ok(items=rows)


@app.post("/api/admin/users")
async def admin_user_create(request: Request):
    require_perm(request, "user.manage")
    b = await request.json()
    email = (b.get("email") or "").strip().lower()
    if "@" not in email:
        fail(400, "bad_email")
    c = conn_()
    err = _registration_allowed(c, email)
    if err:
        c.close()
        fail(403, err)
    if c.execute("SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone():
        c.close()
        fail(409, "email_exists")
    cur = c.execute("INSERT INTO users(email,display_name,password_hash,status) VALUES(?,?,?,?)",
                    (email, b.get("display_name", ""), auth.hash_password(b.get("password") or "Change@123"),
                     b.get("status", "active")))
    uid = cur.lastrowid
    _set_roles_groups(c, uid, b.get("roles"), b.get("group_ids"))
    _sync_internal_group(c, uid, email)
    c.commit()
    c.close()
    return ok(id=uid)


@app.post("/api/admin/users/bulk")
async def admin_users_bulk(request: Request):
    """Bulk enable / disable / delete / update attributes for selected users."""
    require_perm(request, "user.manage")
    b = await request.json()
    ids = [int(x) for x in (b.get("ids") or []) if str(x).isdigit()]
    action = b.get("action") or ""
    if not ids:
        fail(400, "no_selection")
    marks = ",".join("?" * len(ids))
    c = conn_()
    changed = 0
    if action == "delete":
        for sql in ("DELETE FROM user_groups_rel WHERE user_id IN (%s)",
                    "DELETE FROM user_roles WHERE user_id IN (%s)",
                    "DELETE FROM tokens WHERE user_id IN (%s)",
                    "DELETE FROM users WHERE id IN (%s)"):
            c.execute(sql % marks, ids)
        changed = len(ids)
    elif action in ("disable", "enable"):
        st = "disabled" if action == "disable" else "active"
        c.execute("UPDATE users SET status=? WHERE id IN (%s)" % marks, [st] + ids)
        changed = len(ids)
    elif action == "update":
        if "status" in b and b.get("status"):
            c.execute("UPDATE users SET status=? WHERE id IN (%s)" % marks, [b["status"]] + ids)
        for uid in ids:
            _set_roles_groups(c, uid, b.get("roles"), b.get("group_ids"))
        changed = len(ids)
    else:
        c.close()
        fail(400, "bad_action")
    c.commit()
    c.close()
    return ok(ok=True, changed=changed)


USER_CSV_HEADER = ["email", "display_name", "password", "roles", "status"]


@app.get("/api/admin/users/template.csv")
def admin_users_template(request: Request):
    require_perm(request, "user.manage")
    csv_data = ",".join(USER_CSV_HEADER) + "\n" + \
        "l1@rankez.local,L1 Agent,Change@123,L1售后人员,active\n" + \
        "l2@rankez.local,L2 Engineer,Change@123,L2售后人员,active\n"
    return Response(content=csv_data.encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=users_template.csv"})


@app.post("/api/admin/users/import")
async def admin_users_import(request: Request, file: UploadFile = File(...)):
    require_perm(request, "user.manage")
    raw = await file.read()
    if raw[:3] == b"\xef\xbb\xbf":
        raw = raw[3:]
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8", "replace")))
    c = conn_()
    created = updated = 0
    skipped = []
    for row in reader:
        email = (row.get("email") or "").strip().lower()
        if "@" not in email:
            continue
        exists = c.execute("SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone()
        if not exists and _registration_allowed(c, email):
            # the import is the "add many users" path, so it obeys the same
            # domain allow-list as the single-user form
            skipped.append(email)
            continue
        roles = [x.strip() for x in (row.get("roles") or "").split("|") if x.strip()]
        if exists:
            uid = exists["id"]
            c.execute("UPDATE users SET display_name=COALESCE(NULLIF(?,''),display_name), status=? WHERE id=?",
                      ((row.get("display_name") or "").strip(),
                       (row.get("status") or "active").strip(), uid))
            updated += 1
        else:
            cur = c.execute("INSERT INTO users(email,display_name,password_hash,status) VALUES(?,?,?,?)",
                            (email, (row.get("display_name") or email.split("@")[0]).strip(),
                             auth.hash_password(row.get("password") or "Change@123"),
                             (row.get("status") or "active").strip()))
            uid = cur.lastrowid
            created += 1
        if roles:
            _set_roles_groups(c, uid, roles, None)
        _sync_internal_group(c, uid, email)
    c.commit()
    c.close()
    return ok(created=created, updated=updated, skipped=skipped)


def _set_roles_groups(c, uid, roles, group_ids):
    if roles is not None:
        c.execute("DELETE FROM user_roles WHERE user_id=?", (uid,))
        for rn in roles:
            r = c.execute("SELECT id FROM roles WHERE name=?", (rn,)).fetchone()
            if r:
                c.execute("INSERT OR IGNORE INTO user_roles(user_id,role_id) VALUES(?,?)", (uid, r["id"]))
    if group_ids is not None:
        # Only manual memberships are replaced: the domain-driven ones are owned
        # by _sync_internal_group() and would be re-added straight away.
        managed = rbac.managed_group_ids(c)
        if managed:
            marks = ",".join("?" * len(managed))
            c.execute("DELETE FROM user_groups_rel WHERE user_id=? AND group_id NOT IN (%s)" % marks,
                      [uid] + list(managed))
        else:
            c.execute("DELETE FROM user_groups_rel WHERE user_id=?", (uid,))
        for gid in group_ids:
            if gid not in managed:
                c.execute("INSERT OR IGNORE INTO user_groups_rel(user_id,group_id) VALUES(?,?)", (uid, gid))


@app.put("/api/admin/users/{uid}")
async def admin_user_update(uid: int, request: Request):
    require_perm(request, "user.manage")
    b = await request.json()
    c = conn_()
    if "email" in b:
        c.execute("UPDATE users SET email=? WHERE id=?", ((b["email"] or "").lower(), uid))
    if "display_name" in b:
        c.execute("UPDATE users SET display_name=? WHERE id=?", (b.get("display_name", ""), uid))
    if "status" in b:
        c.execute("UPDATE users SET status=? WHERE id=?", (b.get("status", "active"), uid))
    if b.get("password"):
        c.execute("UPDATE users SET password_hash=? WHERE id=?", (auth.hash_password(b["password"]), uid))
    _set_roles_groups(c, uid, b.get("roles"), b.get("group_ids"))
    # Always re-align. An address change must also EVICT: a user moved to a
    # domain that no longer matches their customer leaves that customer group
    # (and joins the one it now matches, if any).
    row = c.execute("SELECT email FROM users WHERE id=?", (uid,)).fetchone()
    email = (row["email"] if row else "") or ""
    _sync_internal_group(c, uid, email, remove_stale=("email" in b))
    c.commit()
    c.close()
    return ok(ok=True)


@app.delete("/api/admin/users/{uid}")
def admin_user_delete(uid: int, request: Request):
    require_perm(request, "user.manage")
    c = conn_()
    # deleting the user also drops every group membership, so "removed from the
    # customer group" holds and the member counts stay honest
    for sql in ("DELETE FROM user_groups_rel WHERE user_id=?",
                "DELETE FROM user_roles WHERE user_id=?",
                "DELETE FROM tokens WHERE user_id=?",
                "DELETE FROM users WHERE id=?"):
        c.execute(sql, (uid,))
    c.commit()
    c.close()
    return ok(ok=True)


@app.post("/api/admin/users/{uid}/reset_totp")
def admin_reset_totp(uid: int, request: Request):
    require_perm(request, "user.reset_totp")
    c = conn_()
    c.execute("UPDATE users SET totp_enabled=0, totp_secret=NULL WHERE id=?", (uid,))
    c.commit()
    c.close()
    return ok(ok=True)


@app.get("/api/admin/roles")
def admin_roles(request: Request):
    require(request)
    c = conn_()
    out = []
    for r in c.execute("SELECT * FROM roles ORDER BY id"):
        perms = [p["key"] for p in c.execute(
            "SELECT p.key FROM permissions p JOIN role_permissions rp ON rp.perm_id=p.id WHERE rp.role_id=?", (r["id"],))]
        d = dict(r)
        d["permissions"] = perms
        d["levels"] = rbac.levels_for(perms)
        out.append(d)
    c.close()
    return ok(items=out, menu=rbac.MENU_ITEMS)


@app.get("/api/admin/perm_matrix")
def perm_matrix(request: Request):
    """Menu rows + the three levels, used to build the role editor."""
    require_perm(request, "role.manage")
    return ok(menu=rbac.MENU_ITEMS, levels=["none", "read", "edit"])


@app.post("/api/admin/roles")
async def admin_role_create(request: Request):
    require_perm(request, "role.manage")
    b = await request.json()
    name = (b.get("name") or "").strip()
    if not name:
        fail(400, "name_required")
    c = conn_()
    # never adopt an existing (possibly built-in) role by name - that would wipe its permissions
    if c.execute("SELECT id FROM roles WHERE lower(name)=?", (name.lower(),)).fetchone():
        c.close()
        fail(400, "role_name_taken")
    cur = c.execute("INSERT INTO roles(name,description,builtin) VALUES(?,?,0)",
                    (name, b.get("description", "")))
    rid = cur.lastrowid
    c.execute("DELETE FROM role_permissions WHERE role_id=?", (rid,))
    # the role editor posts {levels:{menu_key: none|read|edit}}
    perms = b.get("permissions")
    if isinstance(b.get("levels"), dict):
        perms = rbac.perms_for_levels(b["levels"])
    permmap = {r["key"]: r["id"] for r in c.execute("SELECT id,key FROM permissions")}
    for k in (perms or []):
        if k in permmap:
            c.execute("INSERT OR IGNORE INTO role_permissions(role_id,perm_id) VALUES(?,?)", (rid, permmap[k]))
    c.commit()
    c.close()
    return ok(id=rid)


@app.put("/api/admin/roles/{rid}")
async def admin_role_update(rid: int, request: Request):
    require_perm(request, "role.manage")
    b = await request.json()
    c = conn_()
    role = c.execute("SELECT * FROM roles WHERE id=?", (rid,)).fetchone()
    if not role:
        c.close()
        fail(404, "not_found")
    if role["builtin"]:
        c.close()
        fail(400, "builtin_role_readonly")
    if "name" in b:
        name = (b.get("name") or "").strip()
        if not name:
            c.close()
            fail(400, "name_required")
        c.execute("UPDATE roles SET name=? WHERE id=?", (name, rid))
    if "description" in b:
        c.execute("UPDATE roles SET description=? WHERE id=?", (b.get("description", ""), rid))
    # the role editor posts {levels:{menu_key: none|read|edit}}
    if isinstance(b.get("levels"), dict):
        b["permissions"] = rbac.perms_for_levels(b["levels"])
    if "permissions" in b:
        c.execute("DELETE FROM role_permissions WHERE role_id=?", (rid,))
        permmap = {r["key"]: r["id"] for r in c.execute("SELECT id,key FROM permissions")}
        for k in (b.get("permissions") or []):
            if k in permmap:
                c.execute("INSERT OR IGNORE INTO role_permissions(role_id,perm_id) VALUES(?,?)", (rid, permmap[k]))
    c.commit()
    c.close()
    return ok(ok=True)


@app.delete("/api/admin/roles/{rid}")
def admin_role_delete(rid: int, request: Request):
    require_perm(request, "role.manage")
    c = conn_()
    if c.execute("SELECT builtin FROM roles WHERE id=?", (rid,)).fetchone()["builtin"]:
        c.close()
        fail(400, "builtin_role")
    c.execute("DELETE FROM roles WHERE id=?", (rid,))
    c.commit()
    c.close()
    return ok(ok=True)


def _group_row(c, r):
    """Serialise a user_groups row + what it grants, for the admin UI."""
    d = dict(r)
    d["member_count"] = c.execute(
        "SELECT COUNT(*) n FROM user_groups_rel WHERE group_id=?", (r["id"],)).fetchone()["n"]
    # "key" always keeps the machine name; "name" is what the UI must display
    d["key"] = r["name"]
    if r["name"] == rbac.INTERNAL_GROUP:
        d["name"] = rbac.INTERNAL_GROUP_LABEL
        d["kind"] = "internal"
        d["grants"] = list(rbac.INTERNAL_GROUP_PERMS)
        d["auto_hint"] = "internal_domains"
    elif r["partner_id"]:
        p = c.execute("SELECT name,domains FROM partners WHERE id=?", (r["partner_id"],)).fetchone()
        d["kind"] = "partner"
        d["partner_name"] = p["name"] if p else ""
        d["domains"] = (p["domains"] if p else "") or ""
        d["grants"] = list(rbac.PARTNER_GROUP_PERMS)
        d["auto_hint"] = "partner_domains"
    elif r["customer_id"]:
        cust = c.execute("SELECT name,domains FROM customers WHERE id=?", (r["customer_id"],)).fetchone()
        d["kind"] = "customer"
        d["customer_name"] = cust["name"] if cust else ""
        d["domains"] = (cust["domains"] if cust else "") or ""
        d["grants"] = list(rbac.CUSTOMER_GROUP_PERMS)
        d["auto_hint"] = "customer_domains"
    else:
        d["kind"] = "manual"
        d["grants"] = []
        d["auto_hint"] = ""
    d["display_name"] = d["name"]
    return d


@app.get("/api/admin/groups")
def admin_groups(request: Request):
    require_perm(request, "group.manage")
    c = conn_()
    out = [_group_row(c, r) for r in c.execute("SELECT * FROM user_groups ORDER BY id")]
    c.close()
    return ok(items=out)


@app.post("/api/admin/groups")
async def admin_group_create(request: Request):
    require_perm(request, "group.manage")
    b = await request.json()
    name = (b.get("name") or "").strip()
    if not name:
        fail(400, "name_required")
    c = conn_()
    if c.execute("SELECT id FROM user_groups WHERE lower(name)=lower(?)", (name,)).fetchone():
        c.close()
        fail(400, "group_name_taken")
    cur = c.execute("INSERT INTO user_groups(name,description) VALUES(?,?)",
                    (name, (b.get("description") or "").strip()))
    c.commit()
    c.close()
    return ok(id=cur.lastrowid)


@app.put("/api/admin/groups/{gid}")
async def admin_group_update(gid: int, request: Request):
    require_perm(request, "group.manage")
    b = await request.json()
    c = conn_()
    row = c.execute("SELECT * FROM user_groups WHERE id=?", (gid,)).fetchone()
    if not row:
        c.close()
        fail(404, "not_found")
    # built-in groups are bound to a customer / internal domains, so their name is
    # system-owned; the description stays editable.
    if "name" in b and not row["builtin"]:
        name = (b.get("name") or "").strip()
        if not name:
            c.close()
            fail(400, "name_required")
        dup = c.execute("SELECT id FROM user_groups WHERE lower(name)=lower(?) AND id<>?",
                        (name, gid)).fetchone()
        if dup:
            c.close()
            fail(400, "group_name_taken")
        c.execute("UPDATE user_groups SET name=? WHERE id=?", (name, gid))
    if "description" in b:
        c.execute("UPDATE user_groups SET description=? WHERE id=?",
                  ((b.get("description") or "").strip(), gid))
    c.commit()
    c.close()
    return ok(ok=True)


@app.delete("/api/admin/groups/{gid}")
def admin_group_delete(gid: int, request: Request):
    require_perm(request, "group.manage")
    c = conn_()
    row = c.execute("SELECT * FROM user_groups WHERE id=?", (gid,)).fetchone()
    if not row:
        c.close()
        fail(404, "not_found")
    if row["builtin"] or row["customer_id"] or row["partner_id"] or row["name"] == rbac.INTERNAL_GROUP:
        c.close()
        fail(400, "builtin_group")
    c.execute("DELETE FROM user_groups_rel WHERE group_id=?", (gid,))
    c.execute("DELETE FROM kb_collections_groups WHERE group_id=?", (gid,))
    c.execute("DELETE FROM kb_article_groups WHERE group_id=?", (gid,))
    c.execute("DELETE FROM user_groups WHERE id=?", (gid,))
    c.commit()
    c.close()
    return ok(ok=True)


@app.get("/api/admin/groups/{gid}/members")
def admin_group_members(gid: int, request: Request):
    require_perm(request, "group.manage")
    c = conn_()
    g = c.execute("SELECT * FROM user_groups WHERE id=?", (gid,)).fetchone()
    if not g:
        c.close()
        fail(404, "not_found")
    out = []
    for r in c.execute(
            "SELECT u.id,u.email,u.display_name,u.status FROM users u "
            "JOIN user_groups_rel gr ON gr.user_id=u.id WHERE gr.group_id=? ORDER BY u.email", (gid,)):
        d = dict(r)
        d["roles"] = [x["name"] for x in c.execute(
            "SELECT ro.name FROM roles ro JOIN user_roles ur ON ur.role_id=ro.id WHERE ur.user_id=?", (r["id"],))]
        # "auto" = this membership is derived from the e-mail domain, so removing
        # it by hand would only be undone by the next sync
        d["auto"] = gid in rbac.auto_group_ids(c, r["email"])
        out.append(d)
    info = _group_row(c, g)
    c.close()
    return ok(items=out, group=info)


@app.post("/api/admin/groups/{gid}/members")
async def admin_group_member_add(gid: int, request: Request):
    require_perm(request, "group.manage")
    b = await request.json()
    c = conn_()
    g = c.execute("SELECT * FROM user_groups WHERE id=?", (gid,)).fetchone()
    if not g:
        c.close()
        fail(404, "not_found")
    added = 0
    ids = [int(x) for x in (b.get("user_ids") or []) if str(x).isdigit()]
    email = (b.get("email") or "").strip().lower()
    if email:
        u = c.execute("SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone()
        if not u:
            c.close()
            fail(404, "user_not_found")
        ids.append(u["id"])
    for uid in ids:
        cur = c.execute("INSERT OR IGNORE INTO user_groups_rel(user_id,group_id) VALUES(?,?)", (uid, gid))
        added += cur.rowcount
    c.commit()
    c.close()
    return ok(added=added)


@app.delete("/api/admin/groups/{gid}/members/{uid}")
def admin_group_member_remove(gid: int, uid: int, request: Request):
    require_perm(request, "group.manage")
    c = conn_()
    c.execute("DELETE FROM user_groups_rel WHERE group_id=? AND user_id=?", (gid, uid))
    c.commit()
    c.close()
    return ok(ok=True)


# =============================== ADMIN: INTERNAL DOMAINS ===============================
def _internal_domains_payload(c):
    """The configured suffixes + everyone they currently make desk staff."""
    return {"domains": site_internal_domains(), "users": rbac.internal_users(c)}


def _normalize_domain(x):
    d = str(x or "").strip().lower()
    d = d.replace("https://", "").replace("http://", "").split("/")[0].split(":")[0]
    return d.lstrip("@").lstrip(".").strip()


@app.get("/api/admin/internal_domains")
def admin_internal_domains(request: Request):
    """Settings > Internal domains -- its own page, no longer part of site settings.

    These suffixes define who is staff: a user whose address matches one of them
    joins the internal group automatically and becomes assignable as an owner.
    """
    require_perm(request, "settings.mail")
    c = conn_()
    payload = _internal_domains_payload(c)
    c.close()
    return ok(**payload)


@app.post("/api/admin/internal_domains")
async def admin_internal_domains_save(request: Request):
    require_perm(request, "settings.mail")
    b = await request.json()
    raw = b.get("domains")
    if raw is None:
        raw = b.get("internal_domains") or []
    doms = []
    for x in raw:
        d = _normalize_domain(x)
        if d and d not in doms:
            doms.append(d)
    c = conn_()
    set_setting(c, "internal_domains", json.dumps(doms, ensure_ascii=False))
    c.commit()
    # Re-evaluate internal membership: a domain that was dropped must evict the
    # users it used to make staff (scoped to the internal group only).
    for r in c.execute("SELECT id,email FROM users"):
        rbac.sync_email_groups(c, r["id"], r["email"], remove_stale=True, only="internal")
    c.commit()
    payload = _internal_domains_payload(c)
    c.close()
    return ok(**payload)


# =============================== ADMIN: SITE SETTINGS ===============================
@app.get("/api/admin/site")
def admin_site(request: Request):
    require_perm(request, "settings.mail")
    c = conn_()
    known = rbac.known_domains(c)
    c.close()
    return ok(modules=site_modules(), internal_domains=site_internal_domains(),
              session_timeout=site_session_timeout(),
              session_max_lifetime=site_session_max_lifetime(),
              theme=site_theme(), deploy_types=DEPLOY_TYPES,
              company_name=site_company_name(), company_logo=site_company_logo(),
              welcome_md=site_welcome_md(), allowed_hosts=site_allowed_hosts(),
              logo_hint=LOGO_HINT, mail_provider=site_mail_provider(),
              require_known_domain=site_require_known_domain(), known_domains=known)


@app.post("/api/admin/site")
async def admin_site_save(request: Request):
    require_perm(request, "settings.mail")
    b = await request.json()
    c = conn_()
    if "modules" in b:
        mods = [str(x).strip() for x in (b.get("modules") or []) if str(x).strip()]
        set_setting(c, "modules", json.dumps(mods, ensure_ascii=False))
    if "internal_domains" in b:
        doms = [str(x).strip().lower() for x in (b.get("internal_domains") or []) if str(x).strip()]
        set_setting(c, "internal_domains", json.dumps(doms, ensure_ascii=False))
    if "session_timeout" in b:
        try:
            set_setting(c, "session_timeout", str(int(b.get("session_timeout") or 0)))
        except Exception:
            pass
    if "session_max_lifetime" in b:
        try:
            set_setting(c, "session_max_lifetime", str(int(b.get("session_max_lifetime") or 0)))
        except Exception:
            pass
    if "theme" in b:
        set_setting(c, "theme", str(b.get("theme") or "light"))
    if "company_name" in b:
        set_setting(c, "company_name", str(b.get("company_name") or "").strip() or "RankEZ")
    if "welcome_md" in b:
        set_setting(c, "welcome_md", str(b.get("welcome_md") or ""))
    if "allowed_hosts" in b:
        hosts = [str(x).strip().lower().lstrip(".") for x in (b.get("allowed_hosts") or []) if str(x).strip()]
        set_setting(c, "allowed_hosts", json.dumps(hosts, ensure_ascii=False))
    if "mail_provider" in b:
        set_setting(c, "mail_provider", "o365" if str(b.get("mail_provider") or "").lower() == "o365" else "smtp")
    if "require_known_domain" in b:
        set_setting(c, "require_known_domain", "1" if b.get("require_known_domain") else "0")
    c.commit()
    # Re-evaluate internal-group membership for every user: dropping an internal
    # domain must evict its users. Scoped to the internal group so customer-group
    # memberships are untouched.
    for r in c.execute("SELECT id,email FROM users"):
        rbac.sync_email_groups(c, r["id"], r["email"], remove_stale=True, only="internal")
    c.commit()
    c.close()
    return ok(ok=True)


@app.get("/api/brand")
def brand():
    """Public: company branding + welcome page content (used before login)."""
    return ok(company_name=site_company_name(), company_logo=site_company_logo(),
              welcome_md=site_welcome_md())


@app.post("/api/admin/site/logo")
async def admin_site_logo(request: Request, file: UploadFile = File(...)):
    require_perm(request, "settings.mail")
    raw = await file.read()
    if len(raw) > 1024 * 1024:
        fail(400, "file_too_large")
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"):
        fail(400, "unsupported_image_type")
    name = "logo_" + str(int(time.time())) + ext
    with open(os.path.join(UPLOAD_DIR, name), "wb") as f:
        f.write(raw)
    c = conn_()
    set_setting(c, "company_logo", "/files/" + name)
    c.commit()
    c.close()
    return ok(ok=True, logo="/files/" + name)


# =============================== ADMIN: SETTINGS / MAIL ===============================
@app.get("/api/admin/settings")
def admin_settings(request: Request):
    require_perm(request, "settings.mail")
    c = conn_()
    cfg = mailer.mail_cfg(c)
    # mask secrets in listing
    if cfg.get("smtp_pass"):
        cfg["smtp_pass"] = "****"
    if cfg.get("imap_pass"):
        cfg["imap_pass"] = "****"
    if cfg.get("o365_client_secret"):
        cfg["o365_client_secret"] = "****"
    c.close()
    return ok(config=cfg)


@app.post("/api/admin/settings")
async def admin_settings_save(request: Request):
    require_perm(request, "settings.mail")
    b = await request.json()
    if b.get("smtp_pass") == "****":
        b.pop("smtp_pass")
    if b.get("imap_pass") == "****":
        b.pop("imap_pass")
    if b.get("o365_client_secret") == "****":
        b.pop("o365_client_secret")
    # The two mail engines are mutually exclusive: whichever one is saved becomes
    # the active provider and the other side's credentials are wiped.
    provider = str(b.get("mail_provider") or site_mail_provider()).lower()
    provider = "o365" if provider == "o365" else "smtp"
    SMTP_KEYS = ("smtp_host", "smtp_port", "smtp_security", "smtp_user", "smtp_pass", "smtp_from",
                 "imap_host", "imap_port", "imap_security", "imap_user", "imap_pass", "imap_folder")
    O365_KEYS = ("o365_tenant", "o365_client_id", "o365_client_secret", "o365_scope")
    if provider == "o365":
        for k in SMTP_KEYS:
            b[k] = ""
        b["o365_mode"] = "1"
    else:
        for k in O365_KEYS:
            b[k] = ""
        b["o365_mode"] = "0"
    c = conn_()
    mailer.set_mail_cfg(c, b)
    set_setting(c, "mail_provider", provider)
    c.commit()
    c.close()
    return ok(ok=True, provider=provider)


@app.post("/api/admin/mail/test")
async def mail_test(request: Request):
    require_perm(request, "settings.mail")
    b = await request.json()
    to = b.get("to") or ""
    c = conn_()
    res = mailer.send_email(c, [to] if to else [], "RankEZ test", "Mail engine test OK.")
    c.close()
    return ok(sent=bool(res))


@app.post("/api/admin/mail/poll")
def mail_poll_now(request: Request):
    require_perm(request, "settings.mail")
    c = conn_()
    n = mailer.receive_once(c)
    c.close()
    return ok(handled=n)


# =============================== ATTACHMENTS ===============================
@app.get("/files/{stored}")
def get_file(stored: str, request: Request):
    path = os.path.join(UPLOAD_DIR, stored)
    if not os.path.exists(path):
        fail(404, "not_found")
    # access control: only if referenced by something user can see (best-effort)
    return FileResponse(path)


# =============================== DEV: simulate inbound email ===============================
@app.post("/api/admin/mail/inbound_test")
async def mail_inbound_test(request: Request):
    """Exercise the exact inbound pipeline used by the poll worker, without needing a mailbox.
    Admin-only. Body: {from, to, subject, body}."""
    require_perm(request, "settings.mail")
    b = await request.json()
    frm = (b.get("from") or "").strip().lower()
    to_list = [x for x in (b.get("to") or []) if "@" in x]
    c = conn_()
    res = mailer.process_incoming_email(c, subject=b.get("subject", ""), frm=frm,
                                        frm_name=b.get("from_name", ""), to_list=to_list,
                                        body=b.get("body", ""), atts=None, cfg=None)
    c.close()
    return ok(result=res)


def _loop():
    while True:
        try:
            c = conn_()
            if get_setting(c, "smtp_host"):
                mailer.receive_once(c)
            c.close()
        except Exception:
            pass
        time.sleep(60)


threading.Thread(target=_loop, daemon=True).start()


# =============================== STATIC / SPA ===============================
app.mount("/assets", StaticFiles(directory=FRONTEND), name="assets")


@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND, "index.html"))


@app.get("/health")
def health():
    return ok(ok=True, service="rankez-support")


# SPA fallback
@app.get("/{path:path}")
def spa(path: str):
    fp = os.path.join(FRONTEND, path)
    if path and os.path.isfile(fp):
        return FileResponse(fp)
    return FileResponse(os.path.join(FRONTEND, "index.html"))
