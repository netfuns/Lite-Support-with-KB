"""Example Support platform — FastAPI app (API + static SPA)."""
import base64
import csv
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime

import auth
import rbac
import captcha as CAPTCHA
import convert
import desens
import mailer
import backup
import md
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
app = FastAPI(title="Example Support", docs_url="/api/docs", openapi_url="/api/openapi.json")


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


DEFAULT_MODULES = ["通用", "账号管理", "密码管理", "会话审计报告"]
DEPLOY_TYPES = ["ON-PREM", "SaaS"]
LOGO_HINT = "建议上传 200 × 48 px 的 PNG / SVG（透明背景，横向），不超过 1 MB"

DEFAULT_WELCOME = """# 示例支持中心

欢迎来到 **示例售后与知识库平台**。

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
    _seed_setting(conn, "company_name", "Example")
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
    admin = conn.execute("SELECT id FROM users WHERE email=?", ("admin@example.com",)).fetchone()
    if not admin:
        cur = conn.execute(
            "INSERT INTO users(email,display_name,password_hash) VALUES(?,?,?)",
            ("admin@example.com", "Administrator", auth.hash_password("Admin@12345")))
        aid = cur.lastrowid
        role = conn.execute("SELECT id FROM roles WHERE name='管理员'").fetchone()
        conn.execute("INSERT OR IGNORE INTO user_roles(user_id,role_id) VALUES(?,?)", (aid, role["id"]))
        for gname in ("管理员", rbac.INTERNAL_GROUP):
            g = conn.execute("SELECT id FROM user_groups WHERE name=?", (gname,)).fetchone()
            if g:
                conn.execute("INSERT OR IGNORE INTO user_groups_rel(user_id,group_id) VALUES(?,?)", (aid, g["id"]))
        print("[seed] admin@example.com / Admin@12345")
    # make sure the existing admin also belongs to the internal group (KB edit rights)
    arow = conn.execute("SELECT id FROM users WHERE email=?", ("admin@example.com",)).fetchone()
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
        if not c.execute("SELECT id FROM kb_articles WHERE title='欢迎使用示例知识库'").fetchone():
            c.execute("INSERT INTO kb_articles(title,body,source,visibility,collection_id,author_id) "
                      "VALUES(?,?,?,?,?,?)",
                    ("欢迎使用示例知识库",
                     "# Example Support\n\nA knowledge base article auto-desensitized from a ticket shows how customer data is masked "
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


def modules_with_builtin(raw=None):
    """The modules on offer: the built-ins, always, plus whatever was added.

    A module the operator added can be deleted; the four built-ins cannot. They
    are what the shipped ticket form, knowledge base and every filter are written
    against, and a site offering none of them would leave nothing to file a
    ticket under. The order is the built-ins in their seeded order, then the
    added ones -- which is also the order the settings page draws them in.
    """
    out = list(DEFAULT_MODULES)
    for x in (raw or []):
        v = str(x).strip()
        if v and v not in out:
            out.append(v)
    return out


def site_modules():
    """The configured module list, deletions included.

    An empty list is a real answer -- the operator removed every module he had
    added -- so it must not be swapped for a fallback: that is exactly what made
    a deleted module come back in the ticket form and the knowledge base. Only a
    value that was never written falls back to the seed.
    """
    c = conn_()
    raw = get_setting(c, "modules", None)
    c.close()
    if raw in (None, ""):
        return list(DEFAULT_MODULES)
    try:
        m = json.loads(raw)
    except Exception:
        return list(DEFAULT_MODULES)
    if not isinstance(m, list):
        return list(DEFAULT_MODULES)
    return modules_with_builtin(m)


def site_internal_domains():
    return [str(x).lower() for x in (_setting_json("internal_domains", []) or []) if x]


def site_allowed_hosts():
    """Hosts allowed to serve the site; empty list = allow everything."""
    return [str(x).strip().lower() for x in (_setting_json("allowed_hosts", []) or []) if str(x).strip()]


def site_company_name():
    return _setting_raw("company_name", "Example") or "Example"


def site_company_logo():
    return _setting_raw("company_logo", "")


def site_welcome_md():
    return _setting_raw("welcome_md", "") or DEFAULT_WELCOME


def site_setting(key, default=""):
    """A raw setting (used for the mail templates and the portal address)."""
    return _setting_raw(key, default) or default


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
@app.get("/api/captcha/new")
def captcha_new(request: Request):
    """Paint a fresh slider puzzle. Rate-limited per client IP."""
    ip = CAPTCHA.client_ip(request)
    data = CAPTCHA.issue(ip)
    if not data.get("ok"):
        return JSONResponse(data, 429)
    return ok(**data)


@app.post("/api/captcha/verify")
async def captcha_verify(request: Request):
    """One shot. On success the client gets a 30-minute pass bound to its IP."""
    b = await request.json()
    ip = CAPTCHA.client_ip(request)
    r = CAPTCHA.verify(ip, b.get("captcha_id"), b.get("dx"), b.get("ms"), b.get("points"))
    if not r.get("ok"):
        return JSONResponse(r, 403)
    c = conn_()
    try:
        tok = CAPTCHA.grant_pass(c, ip)
    finally:
        c.close()
    return ok(pass_token=tok, expires_in=CAPTCHA.PASS_TTL)


@app.post("/api/auth/login")
async def login(request: Request):
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    pw = body.get("password") or ""
    c = conn_()
    # anti-crawler / anti-brute-force: from the open internet a fresh slider
    # pass (30 min) must come with the attempt; internal addresses are exempt
    CAPTCHA.guard(request, c)
    u = c.execute("SELECT * FROM users WHERE lower(email)=?", (email,)).fetchone()
    if not u or not auth.verify_password(pw, u["password_hash"] or ""):
        c.close()
        return JSONResponse({"error": "invalid_credentials"}, 401)
    if u["status"] != "active":
        c.close()
        return JSONResponse({"error": "account_disabled"}, 403)
    # The address must still sit on a domain we know. Registration only ever
    # accepted customer / partner / internal domains, but that was checked once
    # at creation time -- a domain removed later used to leave a working key
    # behind. (Whoever can manage users is exempt, see rbac.login_domain_ok.)
    if not rbac.login_domain_ok(c, u):
        c.close()
        return JSONResponse({"error": "domain_not_allowed"}, 403)
    # Self-heal: if this address' domain turned internal after the account was
    # created (a domain saved later, or the site domain only now detectable),
    # the membership and the default role are re-evaluated here so the user does
    # not have to wait for an administrator to touch anything.
    try:
        rbac.resync_internal(c, email=u["email"])
    except Exception:  # noqa: BLE001 - bookkeeping must not block a login
        pass
    # TOTP enforcement:
    #  - totp_enabled: the user already enrolled; require their code now (existing).
    #  - require_totp (mail/auto-created or forgot-reset account, not yet enrolled):
    #    must enroll TOTP before a full session is issued. The secret + QR are
    #    handed to the client so it can show the authenticator setup right here;
    #    on the next attempt the code is verified in-line and the session issued.
    if u["require_totp"] and not u["totp_enabled"]:
        secret = u["totp_secret"] or auth.new_totp_secret()
        c.execute("UPDATE users SET totp_secret=? WHERE id=?", (secret, u["id"]))
        code = (body.get("code") or "").strip()
        if not code:
            c.commit()
            c.close()
            uri = auth.totp_uri(u["email"], secret)
            return JSONResponse({"need_enroll_totp": True, "totp_secret": secret,
                                 "totp_uri": uri, "totp_qr_svg": auth.qr_svg(uri)}, 200)
        if not auth.totp_verify(secret, code):
            c.commit()
            c.close()
            uri = auth.totp_uri(u["email"], secret)
            return JSONResponse({"need_enroll_totp": True, "totp_secret": secret,
                                 "totp_uri": uri, "totp_qr_svg": auth.qr_svg(uri)}, 200)
        # code verified: mark TOTP enrolled, drop the enrollment flag
        c.execute("UPDATE users SET totp_enabled=1, totp_secret=?, require_totp=0 WHERE id=?",
                  (secret, u["id"]))
    elif u["totp_enabled"] and u["totp_secret"]:
        code = body.get("totp") or ""
        if not auth.totp_verify(u["totp_secret"], code):
            c.close()
            return JSONResponse({"error": "totp_required"}, 200) if code else \
                JSONResponse({"need_totp": True}, 200)
    tok = auth.new_token()
    c.execute("INSERT INTO tokens(token,user_id,pending) VALUES(?,?,0)", (tok, u["id"]))
    # signing in also mints a fresh captcha pass: the human just proved to be
    # one, so searching right after the login must not puzzle him again
    cpass = CAPTCHA.grant_pass(c, CAPTCHA.client_ip(request), u["id"])
    c.commit()
    c.close()
    resp = ok(token=tok, captcha_pass=cpass)
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
    # An agent (代理商) files tickets on behalf of the customers it serves, so the
    # new-ticket form needs that short list -- and only that list -- up front.
    pids = rbac.partner_ids_for_user(c, u["id"])
    partner_names, partner_customers = [], []
    if pids:
        marks = ",".join("?" * len(pids))
        partner_names = [r["name"] for r in
                         c.execute("SELECT name FROM partners WHERE id IN (%s) ORDER BY name" % marks,
                                   list(pids))]
        partner_customers = [{"id": r["id"], "name": r["name"]} for r in
                             c.execute("SELECT id,name FROM customers WHERE partner_id IN (%s) "
                                       "ORDER BY name" % marks, list(pids))]
    c.close()
    return {"id": u["id"], "email": u["email"], "display_name": u["display_name"],
            "totp_enabled": bool(u["totp_enabled"]), "permissions": perms,
            "roles": roles, "groups": groups, "is_internal": is_internal,
            "is_partner": bool(pids), "partner_names": partner_names,
            "partner_customers": partner_customers}


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
    uri = auth.totp_uri(u["email"], secret)
    return ok(secret=secret, uri=uri, qr_svg=auth.qr_svg(uri))


@app.post("/api/me/totp/disable")
async def my_totp_disable(request: Request):
    u = require(request)
    c = conn_()
    c.execute("UPDATE users SET totp_enabled=0, totp_secret=NULL WHERE id=?", (u["id"],))
    c.commit()
    c.close()
    return ok(ok=True)


# ---------- in-app bell: "a ticket arrived / a customer answered" ----------

@app.get("/api/me/alerts")
def my_alerts(request: Request):
    """Pending ticket alerts for this user, oldest first.

    The ticket list polls this; an alert survives until the desk dismisses it,
    so a reply nobody reacted to is still there on the next visit.
    """
    u = require(request)
    c = conn_()
    rows = c.execute(
        "SELECT a.id, a.ticket_id, a.code, a.kind, a.created_at, t.title, t.customer_name "
        "FROM ticket_alerts a LEFT JOIN tickets t ON t.id=a.ticket_id "
        "WHERE a.user_id=? AND a.acked=0 AND t.archived=0 ORDER BY a.id LIMIT 20",
        (u["id"],)).fetchall()
    c.close()
    return ok(items=[dict(r) for r in rows])


@app.post("/api/me/alerts/ack")
async def my_alerts_ack(request: Request):
    """Dismiss alerts: the ids given, or every pending one when none is given."""
    u = require(request)
    body = await request.json()
    ids = body.get("ids") or []
    c = conn_()
    if ids:
        qs = ",".join("?" * len(ids))
        c.execute("UPDATE ticket_alerts SET acked=1 WHERE user_id=? AND acked=0 "
                  "AND id IN (%s)" % qs, [u["id"]] + [int(i) for i in ids])
    else:
        c.execute("UPDATE ticket_alerts SET acked=1 WHERE user_id=? AND acked=0", (u["id"],))
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
    # The role follows the groups the address just earned -- an address on one
    # of this installation's own domains is desk staff, a partner domain is
    # 代理商, anything else is 客户. Shared with the inbound-mail path
    # (tickets.find_or_create_user) so the two can never disagree.
    rbac.align_domain_role(c, uid, email)
    _sync_internal_group(c, uid, email)
    c.commit()
    c.close()
    return ok(ok=True)


# --------------------------- forgot-password / self-enrollment -------------
#
# "Forgot password" doubles as a self-service credential reset for customer
# domains:
#   * address already exists  -> new random password is e-mailed and TOTP is
#     cleared, so the user re-enrolls on the next login (TOTP "reset" happens
#     here, exactly as the requirement asks)
#   * address is unknown BUT its domain belongs to a known customer -> the
#     client is told it may create that account (POST /api/auth/create below);
#     the server refuses to create on demand so an open mailbox is required
#   * everything else          -> "no account" so an attacker learns nothing
#
def _random_password():
    import secrets
    return secrets.token_urlsafe(12)


def _forgot_credentials_email(conn, to_addr, password, base_url=""):
    import mailer
    login = (base_url or "") + "/#/login"
    text = (
        "Your Example support portal password has been reset.\n\n"
        "Login URL: %s\nEmail: %s\nNew password: %s\n\n"
        "On next sign-in you will be asked to set up two-factor authentication "
        "(TOTP) again.\n---\n您的示例售后平台密码已重置。\n\n登录地址：%s\n邮箱：%s\n"
        "新密码：%s\n\n下次登录时需重新设置两步验证（TOTP）。\n"
    ) % (login, to_addr, password, login, to_addr, password)
    htmlb = ("<p>Your Example password has been reset.</p>"
             "<p><b>Login URL:</b> <a href='%s'>%s</a><br><b>Email:</b> %s<br>"
             "<b>New password:</b> %s</p>"
             "<p>On next sign-in you will be asked to set up TOTP again.</p>"
             "<hr><p>您的示例售后平台密码已重置。</p>"
             "<p><b>登录地址：</b><a href='%s'>%s</a><br><b>邮箱：</b>%s<br>"
             "<b>新密码：</b>%s</p>"
             "<p>下次登录时需重新设置两步验证（TOTP）。</p>"
             % (login, login, to_addr, password, login, login, to_addr, password))
    return mailer.send_email(conn, [to_addr], "[Example] Your password has been reset",
                             text, htmlb)


@app.post("/api/auth/forgot")
async def auth_forgot(request: Request):
    """Reset a known user's password (e-mailed + TOTP cleared), or report that a
    customer-domain address may be self-enrolled. Never reveals existence of
    addresses outside a known customer domain."""
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    if "@" not in email:
        return JSONResponse({"error": "invalid_input"}, 400)
    c = conn_()
    row = c.execute("SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone()
    cust = T.match_customer(c, email)
    if row:
        pw = _random_password()
        c.execute("UPDATE users SET password_hash=?, require_totp=1, totp_enabled=0, "
                  "totp_secret=NULL WHERE id=?",
                  (auth.hash_password(pw), row["id"]))
        c.commit()
        base = get_setting(c, "base_url", "")
        mailed = _forgot_credentials_email(c, email, pw, base)
        c.close()
        # generic on purpose: same response whether or not the mail went out,
        # so the endpoint cannot be used to enumerate addresses
        return JSONResponse({"sent": True})
    if cust:
        # no account yet, but the domain is a real customer's: offer to create
        c.close()
        return JSONResponse({"need_create": True, "customer_name": cust["name"]})
    c.close()
    return JSONResponse({"need_create": False})


@app.post("/api/auth/create")
async def auth_create(request: Request):
    """Self-enroll from the forgot-password flow: create an account whose email
    matches a KNOWN CUSTOMER domain, with the caller's chosen password, TOTP
    required on first login. Never creates an account for an unknown domain --
    that would defeat the whole allow-list.
    """
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    pw = body.get("password") or ""
    if "@" not in email:
        return JSONResponse({"error": "invalid_input"}, 400)
    c = conn_()
    # only a *customer* domain may be self-created here: the requirement is about
    # customers who mail the desk and never reach a web sign-up form.
    cust = T.match_customer(c, email)
    if not cust:
        c.close()
        return JSONResponse({"error": "domain_not_allowed"}, 403)
    if c.execute("SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone():
        c.close()
        return JSONResponse({"error": "email_exists"}, 409)
    if len(pw) < 6:
        c.close()
        return JSONResponse({"error": "password_too_short"}, 400)
    cur = c.execute(
        "INSERT INTO users(email,display_name,password_hash,require_totp) VALUES(?,?,?,1)",
        (email, (body.get("display_name") or email.split("@", 1)[0]),
         auth.hash_password(pw)))
    uid = cur.lastrowid
    gid = rbac.ensure_customer_group(c, cust["id"], cust["name"])
    c.execute("INSERT OR IGNORE INTO user_groups_rel(user_id,group_id) VALUES(?,?)", (uid, gid))
    role = c.execute("SELECT id FROM roles WHERE name='客户'").fetchone()
    if role:
        c.execute("INSERT OR IGNORE INTO user_roles(user_id,role_id) VALUES(?,?)", (uid, role["id"]))
    rbac.sync_email_groups(c, uid, email, remove_stale=False)
    c.commit()
    c.close()
    return JSONResponse({"ok": True, "id": uid})


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
              modules=site_modules(), builtin_modules=DEFAULT_MODULES,
              deploy_types=DEPLOY_TYPES,
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
    u = require(request)
    c = conn_()
    perms = rbac.user_permissions(c, u["id"])
    sql = "SELECT * FROM customers WHERE 1=1"
    args = []
    if q:
        sql += " AND (name LIKE ? OR domains LIKE ?)"; args += ["%" + q + "%"] * 2
    if "customer.view" not in perms and "ticket.view_all" not in perms:
        # Only the desk and the customer desk may enumerate the whole book. A
        # customer sees his own customer, an agent sees the customers he serves
        # -- the new-ticket picker then cannot suggest anybody else's name.
        ids = {x["id"] for x in rbac.customer_groups_for_user(c, u["id"])}
        ids |= rbac.partner_customer_ids(c, u["id"])
        if not ids:
            c.close()
            return ok(items=[])
        marks = ",".join("?" * len(ids))
        sql += " AND id IN (%s)" % marks; args += list(ids)
    rows = c.execute(sql + " ORDER BY name", args).fetchall()
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
    """A partner plus the customers it serves and how many users it reaches."""
    d = dict(r)
    served = c.execute("SELECT id,name FROM customers WHERE partner_id=? ORDER BY name", (r["id"],)).fetchall()
    # names keep the old payload shape, ids let the editor round-trip a selection
    d["customers"] = [x["name"] for x in served]
    d["customer_ids"] = [x["id"] for x in served]
    d["customer_count"] = len(d["customers"])
    g = c.execute("SELECT id FROM user_groups WHERE partner_id=?", (r["id"],)).fetchone()
    d["group_id"] = g["id"] if g else None
    d["group_name"] = rbac.PARTNER_GROUP_PREFIX + (r["name"] or "").strip()
    d["member_count"] = c.execute(
        "SELECT COUNT(*) n FROM user_groups_rel WHERE group_id=?", (g["id"],)).fetchone()["n"] if g else 0
    return d


def _partner_selection(c, raw):
    """The editor's selection, resolved to real customer ids (see rbac)."""
    return rbac.clean_partner_selection(c, raw)


def _apply_partner_customers(c, pid, ids):
    """Point the picked customers at this partner, release the rest (see rbac)."""
    return rbac.apply_partner_customers(c, pid, ids)


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
    # the editor lets the desk pick "customers served" up front; anything it sent
    # is applied here so a new partner is complete in one round-trip
    sel = _partner_selection(c, b.get("customer_ids", b.get("customers")))
    _apply_partner_customers(c, pid, sel)
    c.commit()
    c.close()
    # the new domains may already cover existing users
    _resync_all_domain_groups(evict_ids=[g for g in [_group_id_for_partner(pid)] if g])
    return ok(id=pid, customers=len(sel))


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
    # Only touched when the key is present: a client that does not show the
    # "customers served" picker must not silently unlink every customer.
    touched = "customer_ids" in b or "customers" in b
    sel = _partner_selection(c, b.get("customer_ids", b.get("customers"))) if touched else []
    if touched:
        _apply_partner_customers(c, pid, sel)
    c.commit()
    c.close()
    _resync_all_domain_groups(evict_ids=[g for g in [_group_id_for_partner(pid)] if g])
    return ok(ok=True, customers=(len(sel) if touched else None))


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
def _partner_agency_ids(conn, uid):
    """The partner-group ids the caller belongs to (used for agency-wide reads)."""
    out = set()
    for pid in rbac.partner_ids_for_user(conn, uid):
        g = conn.execute("SELECT id FROM user_groups WHERE partner_id=?", (pid,)).fetchone()
        if g:
            out.add(g["id"])
    return out


def _agency_ticket_visible(conn, t, u, agency_gids):
    """A ticket an agent filed for his own agency (no customer) belongs to the group.

    Without this an agent's "own" ticket would be readable by its author alone,
    which defeats the point of a reseller working as a team.
    """
    if agency_gids and t["creator_id"] and not t["customer_id"]:
        if agency_gids & rbac.groups_for_user(conn, t["creator_id"]):
            return True
    return False


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
    if "ticket.view_partner" in perms:
        if t["customer_id"] and t["customer_id"] in rbac.partner_customer_ids(conn, u["id"]):
            return True
        if _agency_ticket_visible(conn, t, u, _partner_agency_ids(conn, u["id"])):
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
    agency_gids = _partner_agency_ids(c, u["id"]) if "ticket.view_partner" in perms else set()
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
            if not vis and _agency_ticket_visible(c, t, u, agency_gids):
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
        elif cust_name:
            # An agent (代理商) may file for one of the customers it serves, or for
            # its own agency (no customer at all). Anything else would auto-create
            # a customer the agent's group cannot see, so it is dropped to "".
            allowed = rbac.partner_customer_ids(c, u["id"])
            hit = None
            if allowed:
                marks = ",".join("?" * len(allowed))
                hit = c.execute("SELECT name FROM customers WHERE lower(name)=? AND id IN (%s)" % marks,
                                [cust_name.lower()] + list(allowed)).fetchone()
            cust_name = hit["name"] if hit else ""
    # 收件人: the addresses the opener picked on the form. They become
    # participants, which is what decides who is told when the desk answers.
    recips = [x.strip().lower() for x in (form.get("recipients") or "").split(",")]
    recips = [x for x in recips if "@" in x]
    if (u["email"] or "").strip().lower() not in recips:
        recips.append((u["email"] or "").strip().lower())
    # a customer may only name people he can see -- otherwise the field would be
    # a way to subscribe arbitrary addresses to a ticket's traffic
    if "ticket.view_all" not in perms and not rbac.is_internal_user(c, u["id"]):
        dom = (u["email"] or "").split("@", 1)[-1].lower()
        recips = [x for x in recips if x.endswith("@" + dom)]
    t = T.create_ticket(
        c, title=title, description=form.get("description") or "",
        customer_name=cust_name, version=form.get("version") or "",
        product=form.get("product") or "", priority=form.get("priority") or "medium",
        creator_id=u["id"], creator_email=u["email"], internal=internal,
        participant_emails=recips, attachments=attachments)
    dep = form.get("deploy_type") or ""
    if dep:
        c.execute("UPDATE tickets SET deploy_type=? WHERE id=?", (dep, t["id"]))
    c.commit()
    # the bell is for the desk, an internal note-style ticket is not their queue
    if not internal:
        T.alert_new_ticket(c, t)
    c.commit()
    c.close()
    if not internal:
        # the opener gets a receipt with his link, the desk gets told separately
        threading.Thread(target=_notify_opened, args=(conn_(), t["id"]), daemon=True).start()
    return ok(id=t["id"], code=t["code"])


def _notify_opened(c, tid):
    t = c.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    if not t:
        c.close()
        return
    T.notify_ticket_opened(c, t)
    T.notify_new_ticket(c, t)
    c.close()


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
        vis = _kb_visibility(b.get("kb_visibility"), True)
        gids = _kb_groups(c, b, True)
        if vis == "usergroup" and not gids:
            vis = "internal"
        archive = T.archive_to_kb(c, tid, visibility=vis,
                                  desensitize=_truthy(b.get("kb_desensitize"), "1"),
                                  group_ids=(gids if vis == "usergroup" else None))
        c.close()
        return ok(ok=True, archived=archive, shared=True, visibility=vis)
    c.close()
    return ok(ok=True)


KB_VISIBILITIES = ("public", "registered", "internal", "usergroup")


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
    vis = _kb_visibility(b.get("kb_visibility"), internal)
    gids = _kb_groups(c, b, internal)
    if vis == "usergroup" and not gids:
        # an audience-free "usergroup" article would be readable by nobody;
        # fall back to the safe inner audience rather than publishing a trap
        vis = "internal"
    aid = None
    if share:
        aid = T.archive_to_kb(c, tid, visibility=vis, desensitize=des,
                              group_ids=(gids if vis == "usergroup" else None))
    else:
        c.execute("UPDATE tickets SET status='closed', updated_at=datetime('now') WHERE id=?", (tid,))
        c.commit()
    c.close()
    return ok(ok=True, shared=bool(aid), archived=aid, visibility=vis)


def _kb_groups(c, b, internal):
    """The group binding that came with a share request.

    Only an internal user may scope an article to groups -- a customer closing
    his own ticket has no business picking somebody else's desk as the audience.
    """
    if not internal:
        return []
    return _clean_group_ids(c, b.get("kb_group_ids", b.get("group_ids")))


@app.post("/api/tickets/{tid}/share_kb")
async def ticket_share_kb(tid: int, request: Request):
    """Publish a ticket thread to the knowledge base -- the desk only.

    Sharing *is* closing: a thread only becomes knowledge once it is finished,
    so the ticket is closed as part of the same call (the UI warns about this
    before it fires). The audience is picked here -- public / registered users /
    internal only / the specific user groups that should be able to read it.

    Publishing twice updates the same article instead of leaving duplicates.
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
    gids = _kb_groups(c, b, True)
    if vis == "usergroup" and not gids:
        c.close()
        fail(400, "kb_group_required")
    des = _truthy(b.get("desensitize"), "1")
    aid = T.archive_to_kb(c, tid, visibility=vis, desensitize=des,
                          close_ticket=True, group_ids=(gids if vis == "usergroup" else None))
    c.close()
    return ok(ok=True, article_id=aid, visibility=vis, groups=gids,
              desensitized=des, closed=True)


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
        if not is_internal:
            # a customer or his partner answered -- ring the bell for the desk
            T.alert_customer_reply(c, t)
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
        # the desk answered: everybody on the customer side hears about it
        return sorted(parts - staff)
    # A customer answering his own ticket does NOT mail the rest of the customer
    # side: a partner replying would otherwise mail his client back unasked, and
    # everybody who ever touched the ticket would get the whole thread. On an
    # existing ticket only the desk needs to know -- the receipt for a *new*
    # ticket goes to its opener alone.
    return sorted(parts & staff)


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
    mailer.send_email(c, parts, mailer.thread_subject(t), text, htmlb)


# =============================== KNOWLEDGE BASE ===============================
VIS_LEVEL = {"public": 0, "registered": 1, "internal": 2, "usergroup": 3}


def _kb_access_ok(conn, u, article, group_ids):
    """group_ids: caller-cached user group set. The rule itself lives in rbac."""
    return rbac.kb_article_readable(conn, u, article, group_ids)


@app.get("/api/kb/groups")
def kb_group_options(request: Request):
    """The groups an article may be bound to -- the "usergroup" audience picker.

    Deliberately narrower than /api/admin/groups: the desk needs the names to
    choose an audience, not the group-management payload, and it must not need
    the group.manage permission just to share a solution.
    """
    require(request)
    u = require(request)
    c = conn_()
    if not rbac.is_internal_user(c, u["id"]):
        c.close()
        fail(403, "internal_only")
    out = []
    for r in c.execute("SELECT id,name,builtin,customer_id,partner_id FROM user_groups ORDER BY name"):
        kind = "internal" if r["name"] == rbac.INTERNAL_GROUP else \
               "customer" if r["customer_id"] else "partner" if r["partner_id"] else "manual"
        label = rbac.INTERNAL_GROUP_LABEL if r["name"] == rbac.INTERNAL_GROUP else r["name"]
        out.append({"id": r["id"], "name": r["name"], "label": label, "kind": kind})
    c.close()
    return ok(items=out)


@app.get("/api/kb/articles")
def kb_list(request: Request, collection: int = None, q: str = "", vis: str = "",
            module: str = ""):
    u = current_user(request)
    c = conn_()
    if not u:
        # anonymous homepage search: a fresh slider pass (30 min) is the toll
        CAPTCHA.guard(request, c)
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


def _clean_group_ids(c, raw):
    """Numeric, existing group ids only -- a stale pick must not create a dangling row."""
    out = []
    if raw is None:
        return out
    if isinstance(raw, (str, int)):
        raw = [raw]
    for x in raw:
        try:
            gid = int(x)
        except (TypeError, ValueError):
            continue
        if gid in out:
            continue
        if c.execute("SELECT id FROM user_groups WHERE id=?", (gid,)).fetchone():
            out.append(gid)
    return out


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
    gids = _clean_group_ids(c, b.get("group_ids"))
    if vis == "usergroup" and not gids:
        vis = "internal"          # no audience would be able to read it otherwise
    cur = c.execute(
        "INSERT INTO kb_articles(title,body,source,visibility,module,collection_id,author_id) VALUES(?,?,?,?,?,?,?)",
        (b.get("title", ""), b.get("body", ""), "manual", vis, b.get("module", ""),
         b.get("collection_id"), u["id"]))
    aid = cur.lastrowid
    for gid in gids:
        c.execute("INSERT OR IGNORE INTO kb_article_groups(article_id,group_id) VALUES(?,?)", (aid, gid))
    # a picture pasted into the editor is an orphan file until it is bound here
    T.bind_body_files(c, b.get("body", ""), article_id=aid)
    c.commit()
    c.close()
    return ok(id=aid, visibility=vis, groups=gids)


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
    gids = _clean_group_ids(c, b.get("group_ids") if "group_ids" in b else
                               [r["group_id"] for r in c.execute(
                                   "SELECT group_id FROM kb_article_groups WHERE article_id=?", (aid,))])
    if vis == "usergroup" and not gids:
        vis = "internal"          # no audience would be able to read it otherwise
    c.execute("UPDATE kb_articles SET title=?, body=?, visibility=?, module=?, collection_id=?, updated_at=datetime('now') WHERE id=?",
              (b.get("title", a["title"]), b.get("body", a["body"]), vis,
               b.get("module", a["module"] or ""), b.get("collection_id"), aid))
    c.execute("DELETE FROM kb_article_groups WHERE article_id=?", (aid,))
    for gid in gids:
        c.execute("INSERT OR IGNORE INTO kb_article_groups(article_id,group_id) VALUES(?,?)", (aid, gid))
    T.bind_body_files(c, b.get("body", a["body"] or ""), article_id=aid)
    c.commit()
    c.close()
    return ok(ok=True, visibility=vis, groups=gids)


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
    okmail = mailer.send_email(c, [to], "[Example KB] %s" % a["title"], msg)
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
@app.get("/api/users")
def user_picker(request: Request, q: str = ""):
    """Recipients the caller may pick, for the ticket form's 收件人 field.

    The desk sees everybody, but a customer or a partner only sees the people on
    his own domain: a client picking recipients must not be able to read the
    customer list, and a partner must not see his client's staff. An address on
    a domain nobody registered is his own domain by definition, so he always
    finds at least his colleagues.
    """
    u = require(request)
    c = conn_()
    perms = rbac.user_permissions(c, u["id"], u["email"])
    wide = "ticket.view_all" in perms or rbac.is_internal_user(c, u["id"])
    dom = (u["email"] or "").split("@", 1)[-1].lower() if "@" in (u["email"] or "") else ""
    sql = "SELECT id,email,display_name FROM users WHERE status='active'"
    args = []
    if not wide:
        sql += " AND lower(email) LIKE ?"
        args.append("%@" + dom)
    if q:
        sql += " AND (email LIKE ? OR display_name LIKE ?)"
        args += ["%" + q + "%", "%" + q + "%"]
    rows = [dict(r) for r in c.execute(sql + " ORDER BY email LIMIT 50", args)]
    c.close()
    return ok(items=rows, scope=("all" if wide else ("domain:" + dom)))


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
        "l1@example.com,L1 Agent,Change@123,L1售后人员,active\n" + \
        "l2@example.com,L2 Engineer,Change@123,L2售后人员,active\n"
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
    """The configured suffixes + everyone they currently make desk staff.

    `site_domains` are the domains this installation recognised as its own
    (Settings > Mail + the staff accounts that already exist). They behave as
    internal domains -- their owners join 内部用户组 -- without anybody having to
    type them in; `effective` is the union actually in force. Promoting one with
    "+" only pins it, so it survives a change of mailbox.
    """
    doms = site_internal_domains()
    derived = [d for d in rbac.derived_site_domains(c) if d not in doms]
    return {"domains": doms, "site_domains": derived,
            "effective": rbac.effective_internal_domains(c),
            "users": rbac.internal_users(c)}


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
    # Re-evaluate internal membership: a domain that was added must pull in the
    # accounts that already sat on it (they were created as 客户 before the domain
    # became internal), a domain that was dropped must evict them again, and the
    # default 客户 role follows the same rule. Scoped to the internal group only.
    moved = rbac.resync_internal(c)
    c.commit()
    payload = _internal_domains_payload(c)
    payload["moved"] = moved
    c.close()
    return ok(**payload)


# =============================== ADMIN: SITE SETTINGS ===============================
@app.get("/api/admin/site")
def admin_site(request: Request):
    require_perm(request, "settings.mail")
    c = conn_()
    known = rbac.known_domains(c)
    c.close()
    return ok(modules=site_modules(), builtin_modules=DEFAULT_MODULES,
              internal_domains=site_internal_domains(),
              session_timeout=site_session_timeout(),
              session_max_lifetime=site_session_max_lifetime(),
              theme=site_theme(), deploy_types=DEPLOY_TYPES,
              company_name=site_company_name(), company_logo=site_company_logo(),
              welcome_md=site_welcome_md(), allowed_hosts=site_allowed_hosts(),
              logo_hint=LOGO_HINT, mail_provider=site_mail_provider(),
              require_known_domain=site_require_known_domain(), known_domains=known,
              site_url=site_setting("site_url"),
              tpl_new_user=site_setting("tpl_new_user"),
              tpl_new_ticket=site_setting("tpl_new_ticket"),
              tpl_vars=list(md.TPL_VARS))


@app.post("/api/admin/site")
async def admin_site_save(request: Request):
    require_perm(request, "settings.mail")
    b = await request.json()
    c = conn_()
    if "modules" in b:
        # The built-ins are put back rather than the save being refused: a client
        # that drops one (a cached page, a hand-written request) would otherwise
        # fail the whole save, theme and session timeout included.
        mods = [str(x).strip() for x in (b.get("modules") or []) if str(x).strip()]
        set_setting(c, "modules",
                    json.dumps(modules_with_builtin(mods), ensure_ascii=False))
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
        set_setting(c, "company_name", str(b.get("company_name") or "").strip() or "Example")
    if "welcome_md" in b:
        set_setting(c, "welcome_md", str(b.get("welcome_md") or ""))
    # Portal address: the link inside notification mails. "Settings > Mail >
    # Public URL" sets base_url; this one is the site-level fallback for the
    # sites that never configured a mail account (see tickets.site_root).
    if "site_url" in b:
        set_setting(c, "site_url", str(b.get("site_url") or "").strip().rstrip("/"))
    # Mail templates replace the built-in wording once they are filled in.
    # Stored as {"subject": ..., "body": <markdown>}; an empty body + subject
    # means "use the built-in mail" again (see tickets.tpl_get).
    for key in ("tpl_new_user", "tpl_new_ticket"):
        if key in b:
            v = b.get(key)
            if isinstance(v, dict):
                v = {"subject": str(v.get("subject") or "").strip(),
                     "body": str(v.get("body") or "")}
            else:
                v = {"subject": "", "body": str(v or "")}
            if not v["subject"] and not v["body"].strip():
                set_setting(c, key, "")
            else:
                set_setting(c, key, json.dumps(v, ensure_ascii=False))
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
    res = mailer.send_email(c, [to] if to else [], "Example test", "Mail engine test OK.")
    c.close()
    return ok(sent=bool(res))


@app.post("/api/admin/mail/poll")
def mail_poll_now(request: Request):
    require_perm(request, "settings.mail")
    c = conn_()
    try:
        n = mailer.receive_once(c)
        return ok(handled=n, error="")
    except Exception as e:  # noqa: BLE001 - a bad IMAP account is a user error
        # Used to bubble up as a bare 500 carrying the mail server's own English
        # text, which the page could not show. Carry it back instead so
        # "Poll now" can say what actually went wrong.
        return ok(handled=0, error=str(e))
    finally:
        c.close()


# =============================== ADMIN: BACKUP / RESTORE ===============================
@app.get("/api/admin/backup")
def admin_backup_get(request: Request):
    require_perm(request, "settings.mail")
    c = conn_()
    conf = backup.cfg(c)
    conf["next_run"] = backup.next_run(c)
    listing = backup.list_archives(c)
    c.close()
    return ok(config=conf, items=listing["items"], dir=listing["dir"],
              dir_exists=listing["dir_exists"], dir_writable=listing["dir_writable"],
              free=listing["free"], legacy_timer=backup.legacy_timer_present(),
              restoring=dict(backup.RESTORING), freq_hint=backup.FREQ_HINT)


@app.post("/api/admin/backup")
async def admin_backup_save(request: Request):
    require_perm(request, "settings.mail")
    b = await request.json()
    c = conn_()
    backup.save_cfg(c, b)
    conf = backup.cfg(c)
    conf["next_run"] = backup.next_run(c)
    c.close()
    return ok(config=conf)


@app.post("/api/admin/backup/run")
def admin_backup_run(request: Request):
    """Take a backup now, in the request (they are small and fast)."""
    require_perm(request, "settings.mail")
    c = conn_()
    res = backup.run_backup(c, reason="manual")
    c.close()
    if not res.get("ok"):
        return ok(ok=False, error=res.get("error") or "", detail=res.get("detail") or "")
    return ok(**res)


@app.get("/api/admin/backup/download")
def admin_backup_download(request: Request, name: str = ""):
    require_perm(request, "settings.mail")
    c = conn_()
    p = backup.archive_path(c, name)
    c.close()
    if not p:
        fail(404, "not_found")
    return FileResponse(p, filename=os.path.basename(p), media_type="application/gzip")


@app.post("/api/admin/backup/delete")
async def admin_backup_delete(request: Request):
    require_perm(request, "settings.mail")
    b = await request.json()
    c = conn_()
    gone = backup.delete_archive(c, b.get("name") or "")
    c.close()
    if not gone:
        fail(404, "not_found")
    return ok(ok=True)


def _exit_for_restart():
    """Leave with a non-zero code so the unit's Restart=on-failure reloads us.

    The portal runs as `Lite` and cannot call systemctl. A *clean* exit would
    not be restarted (RestartSec only applies to a failure), and the process
    cannot simply carry on either: it still holds connections to the database
    file that was just replaced under it.
    """
    try:
        sys.stderr.write("Lite-support: exiting for restart after restore\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    os._exit(3)


@app.post("/api/admin/backup/restore")
async def admin_backup_restore(request: Request):
    """Restore the whole portal from an archive, then let systemd bring it back.

    Body is multipart: either ``name`` (an archive already on the server) or an
    uploaded ``file``. ``confirm`` must read exactly ``RESTORE`` -- this
    replaces every account, ticket and article on the site, so a stray click
    must not be able to reach it.
    """
    require_perm(request, "settings.mail")
    if backup.RESTORING["on"]:
        fail(409, "restore_in_progress")
    form = await request.form()
    if str(form.get("confirm") or "") != "RESTORE":
        fail(400, "confirm_required")
    name = str(form.get("name") or "").strip()
    upload = form.get("file")
    if not name and (upload is None or not getattr(upload, "filename", "")):
        fail(400, "no_archive")
    c = conn_()
    src = backup.archive_path(c, name) if name else ""
    if name and not src:
        c.close()
        fail(404, "not_found")
    tmp_path = ""
    try:
        if src:
            path = src
        else:
            raw = await upload.read()
            if not raw:
                c.close()
                fail(400, "empty_file")
            if len(raw) > backup.MAX_ARCHIVE_BYTES:
                c.close()
                fail(400, "file_too_large")
            fd, tmp_path = tempfile.mkstemp(suffix=".tar.gz", prefix="rz-restore-up-")
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
            path = tmp_path
        info = backup.inspect_archive(path)
        if not info["ok"]:
            c.close()
            fail(400, info["error"] or "bad_archive")
        backup.RESTORING.update(on=True, since=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                error="")
        res = backup.swap_in(path)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    c.close()
    if not res.get("ok"):
        backup.RESTORING.update(on=False, error=res.get("error") or "restore_failed")
        return JSONResponse(status_code=500,
                            content={"error": "restore_failed", "detail": res.get("error") or ""})
    # answer first, then go away: the browser gets the confirmation and the
    # service comes back within RestartSec, on the restored data
    threading.Timer(1.5, _exit_for_restart).start()
    return ok(ok=True, restart=True, counts=res.get("counts") or {},
              kept=res.get("kept") or "", members=info["members"], uploads=info["uploads"])


# =============================== ATTACHMENTS ===============================
MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


@app.post("/api/uploads")
async def upload_image(request: Request, file: UploadFile = File(...)):
    """Store a picture pasted into a Markdown editor and return its URL.

    Pasting a screenshot straight into the text is how people report a problem,
    so the editor needs somewhere to put the bytes *before* the message exists.
    The file lands under a random name in the uploads directory -- not
    guessable, so an upload the author abandons is not discoverable -- and the
    save path then binds it to the ticket or article it ended up in
    (``tickets.bind_body_files``), which is what puts it behind that record's
    permissions. Only images: /files/<name> serves whatever extension it finds,
    and an .html there would be a script running on the portal's own origin.
    """
    u = require(request)          # an anonymous caller must not write files
    raw = await file.read()
    if not raw:
        fail(400, "empty_file")
    if len(raw) > MAX_IMAGE_BYTES:
        fail(400, "file_too_large")
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in IMAGE_EXTS:
        fail(400, "unsupported_image_type")
    stored = uuid.uuid4().hex + ext
    with open(os.path.join(UPLOAD_DIR, stored), "wb") as fh:
        fh.write(raw)
    return ok(url="/files/" + stored, stored=stored,
              filename=file.filename or stored, size=len(raw), user=u["email"])


@app.get("/files/{stored}")
def get_file(stored: str, request: Request):
    """Serve an uploaded file, but only to somebody allowed to see it.

    Ticket links travel inside notification mails, and a URL gets forwarded,
    pasted into chat and left in browser history -- so the link itself is not a
    credential. An unauthenticated download here would hand out a customer's
    screenshot or log to anyone holding the URL, walking straight past the
    ticket's permissions. Files that are not attached to a ticket (the company
    logo on the login screen) stay public.
    """
    path = os.path.join(UPLOAD_DIR, stored)
    if not os.path.exists(path):
        fail(404, "not_found")
    c = conn_()
    row = c.execute("SELECT * FROM attachments WHERE stored_name=?", (stored,)).fetchone()
    if not row:
        c.close()
        return FileResponse(path)          # branding asset: public by design
    u = require(request)          # 401 for an anonymous caller
    allowed = False
    if row["ticket_id"]:
        t = c.execute("SELECT * FROM tickets WHERE id=?", (row["ticket_id"],)).fetchone()
        allowed = bool(t) and _ticket_visible(c, t, u)
    elif row["article_id"]:
        a = c.execute("SELECT * FROM kb_articles WHERE id=?", (row["article_id"],)).fetchone()
        # same ladder as the article list itself
        if a:
            allowed = _kb_access_ok(c, u, a, rbac.groups_for_user(c, u["id"], u["email"]))
    c.close()
    if not allowed:
        fail(403, "no_permission")
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
            # the nightly (or weekly / monthly) backup rides on this same tick:
            # one place that has to be alive, and no cron entry to install
            backup.maybe_run(c)
            c.close()
        except Exception:
            pass
        time.sleep(60)


# Memberships are materialised when an account is created, so a domain that only
# became internal later (somebody finally saved Settings > Internal domains, or
# the mailbox that defines the site domain was filled in) used to leave the
# accounts already sitting on it as 客户 forever. Re-evaluate once at start-up.
try:
    _c = conn_()
    rbac.resync_internal(_c)
    _c.close()
except Exception:  # noqa: BLE001 - never let a bookkeeping pass block boot
    pass


threading.Thread(target=_loop, daemon=True).start()


# =============================== STATIC / SPA ===============================
app.mount("/assets", StaticFiles(directory=FRONTEND), name="assets")


@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND, "index.html"))


@app.get("/health")
def health():
    return ok(ok=True, service="Lite-support")


# SPA fallback
@app.get("/{path:path}")
def spa(path: str):
    fp = os.path.join(FRONTEND, path)
    if path and os.path.isfile(fp):
        return FileResponse(fp)
    return FileResponse(os.path.join(FRONTEND, "index.html"))
