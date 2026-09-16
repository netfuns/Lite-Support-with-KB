"""RankEZ Support platform — FastAPI app (API + static SPA)."""
import base64
import csv
import io
import json
import os
import re
import threading
import time

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


DEFAULT_MODULES = ["PAC", "PSM", "CPM", "VAULT", "CP", "REMOTEAPP", "CLM", "RAG"]
DEPLOY_TYPES = ["ON-PREM", "SaaS"]


def _seed_setting(conn, key, value):
    if not conn.execute("SELECT key FROM settings WHERE key=?", (key,)).fetchone():
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?)", (key, value))


def _seed():
    conn = get_db()
    rbac.seed(conn)
    # default groups (internal = staff group that may edit the knowledge base)
    for g in ("管理员", "售后人员", "internal"):
        conn.execute("INSERT OR IGNORE INTO user_groups(name,builtin) VALUES(?,1)", (g,))
    # site settings
    _seed_setting(conn, "modules", json.dumps(DEFAULT_MODULES, ensure_ascii=False))
    _seed_setting(conn, "internal_domains", "[]")
    _seed_setting(conn, "session_timeout", "480")
    _seed_setting(conn, "theme", "light")
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
        for gname in ("管理员", "internal"):
            g = conn.execute("SELECT id FROM user_groups WHERE name=?", (gname,)).fetchone()
            if g:
                conn.execute("INSERT OR IGNORE INTO user_groups_rel(user_id,group_id) VALUES(?,?)", (aid, g["id"]))
        print("[seed] admin@rankez.local / Admin@12345")
    # make sure the existing admin also belongs to the internal group (KB edit rights)
    arow = conn.execute("SELECT id FROM users WHERE email=?", ("admin@rankez.local",)).fetchone()
    if arow:
        gi = conn.execute("SELECT id FROM user_groups WHERE name='internal'").fetchone()
        if gi:
            conn.execute("INSERT OR IGNORE INTO user_groups_rel(user_id,group_id) VALUES(?,?)", (arow["id"], gi["id"]))
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


def site_modules():
    m = _setting_json("modules", [])
    return [x for x in m if x] or DEFAULT_MODULES


def site_internal_domains():
    return [str(x).lower() for x in (_setting_json("internal_domains", []) or []) if x]


def site_session_timeout():
    try:
        return int(_setting_json("session_timeout", 480) or 0)
    except Exception:
        return 480


def site_theme():
    c = conn_()
    v = get_setting(c, "theme", "light")
    c.close()
    return v or "light"


def _sync_internal_group(c, uid, email):
    """Users whose email domain is an internal domain join the 'internal' group."""
    dom = (email or "").split("@")[-1].lower()
    g = c.execute("SELECT id FROM user_groups WHERE name=?", ("internal",)).fetchone()
    if not g or not dom:
        return
    if dom in site_internal_domains():
        c.execute("INSERT OR IGNORE INTO user_groups_rel(user_id,group_id) VALUES(?,?)", (uid, g["id"]))


def _kb_can_edit(conn, u):
    """Knowledge base may only be edited by the internal group or administrators."""
    if not u:
        return False
    perms = rbac.user_permissions(conn, u["id"])
    if "kb.edit" not in perms:
        return False
    if "user.manage" in perms:
        return True
    g = conn.execute("SELECT id FROM user_groups WHERE name=?", ("internal",)).fetchone()
    if not g:
        return False
    return g["id"] in rbac.groups_for_user(conn, u["id"], u["email"])


# ------------------------------------------------------------------ auth ctx
def current_user(request: Request):
    token = request.headers.get("X-Token") or request.cookies.get("rz_token")
    if not token:
        return None
    c = conn_()
    row = c.execute("SELECT * FROM tokens WHERE token=?", (token,)).fetchone()
    if not row or row["pending"]:
        c.close()
        return None
    # session timeout (minutes; 0 = never)
    timeout = site_session_timeout()
    if timeout > 0 and row["created_at"]:
        try:
            import datetime as _dt
            created = _dt.datetime.strptime(str(row["created_at"])[:19], "%Y-%m-%d %H:%M:%S")
            if (_dt.datetime.utcnow() - created).total_seconds() > timeout * 60:
                c.execute("DELETE FROM tokens WHERE token=?", (token,))
                c.commit()
                c.close()
                return None
        except Exception:
            pass
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
    return ok(ok=True)


def _me_payload(u):
    c = conn_()
    perms = sorted(rbac.user_permissions(c, u["id"]))
    roles = [r["name"] for r in c.execute(
        "SELECT r.name FROM roles r JOIN user_roles ur ON ur.role_id=r.id WHERE ur.user_id=?", (u["id"],))]
    groups = [r["name"] for r in c.execute(
        "SELECT g.name FROM user_groups g JOIN user_groups_rel gr ON gr.group_id=g.id WHERE gr.user_id=?", (u["id"],))]
    c.close()
    return {"id": u["id"], "email": u["email"], "display_name": u["display_name"],
            "totp_enabled": bool(u["totp_enabled"]), "permissions": perms,
            "roles": roles, "groups": groups}


@app.get("/api/me")
def me(request: Request):
    u = require(request)
    return _me_payload(u)


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


# self registration (allowed if email domain is a customer's authorized domain)
@app.post("/api/auth/register")
async def register(request: Request):
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    pw = body.get("password") or ""
    if "@" not in email or len(pw) < 6:
        fail(400, "invalid_input")
    c = conn_()
    cust = T.match_customer(c, email)
    if not cust:
        c.close()
        return JSONResponse({"error": "domain_not_authorized"}, 403)
    if c.execute("SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone():
        c.close()
        return JSONResponse({"error": "email_exists"}, 409)
    cur = c.execute("INSERT INTO users(email,display_name,password_hash) VALUES(?,?,?)",
                    (email, (body.get("display_name") or email.split("@")[0]), auth.hash_password(pw)))
    uid = cur.lastrowid
    gid = rbac.ensure_customer_group(c, cust["id"], cust["name"])
    c.execute("INSERT OR IGNORE INTO user_groups_rel(user_id,group_id) VALUES(?,?)", (uid, gid))
    role = c.execute("SELECT id FROM roles WHERE name='客户'").fetchone()
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
              session_timeout=site_session_timeout(), theme=site_theme(),
              can_edit_kb=can_edit_kb)


def my_perm_set(u):
    c = conn_()
    s = rbac.user_permissions(c, u["id"]) if u else set()
    c.close()
    return s


# =============================== CUSTOMERS ===============================
@app.get("/api/customers")
def customers_list(request: Request, q: str = ""):
    require(request)
    c = conn_()
    if q:
        like = "%" + q + "%"
        rows = c.execute("SELECT * FROM customers WHERE name LIKE ? OR domains LIKE ? ORDER BY name", (like, like)).fetchall()
    else:
        rows = c.execute("SELECT * FROM customers ORDER BY name").fetchall()
    c.close()
    return ok(items=[dict(r) for r in rows])


@app.post("/api/customers")
async def customer_create(request: Request):
    require_perm(request, "customer.create")
    b = await request.json()
    name = (b.get("name") or "").strip()
    domains = (b.get("domains") or "").strip().lower()
    if not name or not domains:
        fail(400, "name_and_domain_required")
    c = conn_()
    exists = c.execute("SELECT id FROM customers WHERE lower(name)=?", (name.lower(),)).fetchone()
    if exists:
        cid = exists["id"]
        c.execute("UPDATE customers SET domains=?, version=?, service_start=?, service_end=?, contact_email=? WHERE id=?",
                  (domains, b.get("version", ""), b.get("service_start", ""), b.get("service_end", ""), b.get("contact_email", "")), (cid,))
    else:
        cur = c.execute("INSERT INTO customers(name,domains,version,service_start,service_end,contact_email) VALUES(?,?,?,?,?,?)",
                        (name, domains, b.get("version", ""), b.get("service_start", ""), b.get("service_end", ""), b.get("contact_email", "")))
        cid = cur.lastrowid
    rbac.ensure_customer_group(c, cid, name)
    c.commit()
    c.close()
    return ok(id=cid)


@app.put("/api/customers/{cid}")
async def customer_update(cid: int, request: Request):
    require_perm(request, "customer.edit")
    b = await request.json()
    c = conn_()
    c.execute("UPDATE customers SET name=?, domains=?, version=?, service_start=?, service_end=?, contact_email=? WHERE id=?",
              (b.get("name", ""), (b.get("domains") or "").lower(), b.get("version", ""),
               b.get("service_start", ""), b.get("service_end", ""), b.get("contact_email", "")), (cid,))
    c.commit()
    c.close()
    return ok(ok=True)


@app.delete("/api/customers/{cid}")
def customer_delete(cid: int, request: Request):
    require_perm(request, "customer.delete")
    c = conn_()
    c.execute("DELETE FROM customers WHERE id=?", (cid,))
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
    marks = ",".join("?" * len(ids))
    c = conn_()
    c.execute("DELETE FROM customers WHERE id IN (%s)" % marks, ids)
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
                      (domains, row.get("version", ""), row.get("service_start", ""), row.get("service_end", ""), row.get("contact_email", "")), (exists["id"],))
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
    return False


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
    out = []
    for t in rows:
        vis = True
        if "ticket.view_all" not in perms:
            vis = False
            if "ticket.view_own" in perms:
                if t["creator_id"] == u["id"] or t["owner_id"] == u["id"]:
                    vis = True
                elif t["customer_id"]:
                    gids = rbac.groups_for_user(c, u["id"], u["email"])
                    g = c.execute("SELECT id FROM user_groups WHERE customer_id=?", (t["customer_id"],)).fetchone()
                    if g and g["id"] in gids:
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
    perms = rbac.user_permissions(c, u["id"])
    internal_ok = bool({"ticket.view_all", "kb.view_internal"} & perms)
    if not internal_ok:
        msgs = [m for m in msgs if not m["internal"]]
        atts = [a for a in atts if True]
    for m in msgs:
        m["attachments"] = [a for a in atts if a["message_id"] == m["id"]]
    c.close()
    return ok(ticket=dict(t), messages=msgs, participants=parts)


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
    internal = 1 if str(form.get("internal", "")) in ("1", "true", "on") else 0
    t = T.create_ticket(
        c, title=title, description=form.get("description") or "",
        customer_name=form.get("customer_name") or "", version=form.get("version") or "",
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
    require_perm(request, "ticket.change_owner")
    b = await request.json()
    c = conn_()
    c.execute("UPDATE tickets SET owner_id=?, updated_at=datetime('now') WHERE id=?", (b.get("owner_id"), tid))
    c.commit()
    c.close()
    return ok(ok=True)


ALLOWED_STATUS_EDIT = {"new", "closed", "customer_replied", "support_replied"}


@app.put("/api/tickets/{tid}/status")
async def ticket_status(tid: int, request: Request):
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
    perms = rbac.user_permissions(c, u["id"])
    allowed = "ticket.change_status" in perms or t["creator_id"] == u["id"]
    # email-opened: participants with same customer domain may change status
    if not allowed and t["source"] == "email" and t["customer_id"]:
        gids = rbac.groups_for_user(c, u["id"], u["email"])
        g = c.execute("SELECT id FROM user_groups WHERE customer_id=?", (t["customer_id"],)).fetchone()
        if g and g["id"] in gids:
            allowed = True
    if not allowed:
        c.close()
        fail(403, "no_permission")
    archive = None
    if new_status == "closed":
        # handled separately via archive flag in body
        pass
    c.execute("UPDATE tickets SET status=?, updated_at=datetime('now') WHERE id=?", (new_status, tid))
    c.commit()
    # archive on close
    if new_status == "closed" and str(b.get("archive", "")) in ("1", "true", "on"):
        vis = b.get("kb_visibility") or "registered"
        des = str(b.get("kb_desensitize", "1")) in ("1", "true", "on")
        archive = T.archive_to_kb(c, tid, visibility=vis, desensitize=des)
        c.close()
        return ok(ok=True, archived=archive)
    c.close()
    return ok(ok=True)


@app.post("/api/tickets/{tid}/reply")
async def ticket_reply(tid: int, request: Request):
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
    T.add_message(c, tid, body=body, user_id=u["id"], author_email=u["email"],
                  author_name=u["display_name"], internal=internal, source="web",
                  attachments=attachments)
    # update status to support_replied for support replies
    if not internal and "ticket.view_all" in rbac.user_permissions(c, u["id"]):
        c.execute("UPDATE tickets SET status='support_replied' WHERE id=? AND status!='closed'", (tid,))
    c.commit()
    c.close()
    # email notification on non-internal reply
    if not internal:
        threading.Thread(target=_notify_reply_web, args=(conn_(), tid, u["id"]), daemon=True).start()
    return ok(ok=True)


def _notify_reply_web(c, tid, uid):
    t = c.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    msgs = c.execute("SELECT * FROM messages WHERE ticket_id=? ORDER BY id", (tid,)).fetchall()
    parts = [r["email"] for r in c.execute("SELECT email FROM ticket_participants WHERE ticket_id=?", (tid,))]
    # customer contact email too
    if t["customer_id"]:
        cust = c.execute("SELECT contact_email,domains FROM customers WHERE id=?", (t["customer_id"],)).fetchone()
        if cust and cust["contact_email"]:
            parts.append(cust["contact_email"])
    parts = sorted(set(e for e in parts if e and e != ""))
    # internal ticket: notify only staff/admin
    if t["internal"]:
        parts = [e for e in parts if e in mailer._support_emails(c)]
    htmlb = mailer._render_email(c, t, msgs, get_setting(c, "base_url", ""))
    mailer.send_email(c, parts, "Re:[#%s] %s" % (t["code"], t["title"]),
                      "A reply was added to ticket %s." % t["code"], htmlb)


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
    if article["visibility"] == "registered":
        ag = set(r["group_id"] for r in conn.execute(
            "SELECT group_id FROM kb_article_groups WHERE article_id=?", (article["id"],)))
        if ag:
            return bool(ag & group_ids)
    if article["visibility"] == "usergroup":
        ag = set(r["group_id"] for r in conn.execute("SELECT group_id FROM kb_article_groups WHERE article_id=?", (article["id"],)))
        return bool(ag & group_ids)
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
        c.execute("DELETE FROM users WHERE id IN (%s)" % marks, ids)
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
    for row in reader:
        email = (row.get("email") or "").strip().lower()
        if "@" not in email:
            continue
        exists = c.execute("SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone()
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
    return ok(created=created, updated=updated)


def _set_roles_groups(c, uid, roles, group_ids):
    if roles is not None:
        c.execute("DELETE FROM user_roles WHERE user_id=?", (uid,))
        for rn in roles:
            r = c.execute("SELECT id FROM roles WHERE name=?", (rn,)).fetchone()
            if r:
                c.execute("INSERT OR IGNORE INTO user_roles(user_id,role_id) VALUES(?,?)", (uid, r["id"]))
    if group_ids is not None:
        c.execute("DELETE FROM user_groups_rel WHERE user_id=?", (uid,))
        for gid in group_ids:
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
    if "email" in b:
        _sync_internal_group(c, uid, (b["email"] or "").lower())
    c.commit()
    c.close()
    return ok(ok=True)


@app.delete("/api/admin/users/{uid}")
def admin_user_delete(uid: int, request: Request):
    require_perm(request, "user.manage")
    c = conn_()
    c.execute("DELETE FROM users WHERE id=?", (uid,))
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
        out.append(d)
    c.close()
    return ok(items=out)


@app.post("/api/admin/roles")
async def admin_role_create(request: Request):
    require_perm(request, "role.manage")
    b = await request.json()
    name = (b.get("name") or "").strip()
    if not name:
        fail(400, "name_required")
    c = conn_()
    cur = c.execute("INSERT OR INTO roles(name,description,builtin) VALUES(?,?,0)".replace("OR INTO", "OR IGNORE INTO"),
                    (name, b.get("description", "")))
    rid = c.execute("SELECT id FROM roles WHERE name=?", (name,)).fetchone()["id"]
    c.execute("DELETE FROM role_permissions WHERE role_id=?", (rid,))
    permmap = {r["key"]: r["id"] for r in c.execute("SELECT id,key FROM permissions")}
    for k in (b.get("permissions") or []):
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
    if "description" in b:
        c.execute("UPDATE roles SET description=? WHERE id=?", (b.get("description", ""), rid))
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


@app.get("/api/admin/groups")
def admin_groups(request: Request):
    require(request)
    c = conn_()
    out = []
    for r in c.execute("SELECT * FROM user_groups ORDER BY id"):
        cnt = c.execute("SELECT COUNT(*) c FROM user_groups_rel WHERE group_id=?", (r["id"],)).fetchone()["c"]
        d = dict(r)
        d["member_count"] = cnt
        out.append(d)
    c.close()
    return ok(items=out)


@app.post("/api/admin/groups")
async def admin_group_create(request: Request):
    require_perm(request, "group.manage")
    b = await request.json()
    c = conn_()
    cur = c.execute("INSERT INTO user_groups(name) VALUES(?)", ((b.get("name") or "").strip(),))
    c.commit()
    c.close()
    return ok(id=cur.lastrowid)


@app.delete("/api/admin/groups/{gid}")
def admin_group_delete(gid: int, request: Request):
    require_perm(request, "group.manage")
    c = conn_()
    c.execute("DELETE FROM user_groups WHERE id=? AND builtin=0", (gid,))
    c.commit()
    c.close()
    return ok(ok=True)


# =============================== ADMIN: SITE SETTINGS ===============================
@app.get("/api/admin/site")
def admin_site(request: Request):
    require_perm(request, "settings.mail")
    return ok(modules=site_modules(), internal_domains=site_internal_domains(),
              session_timeout=site_session_timeout(), theme=site_theme(),
              deploy_types=DEPLOY_TYPES)


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
    if "theme" in b:
        set_setting(c, "theme", str(b.get("theme") or "light"))
    c.commit()
    # re-evaluate internal group membership for every user
    for r in c.execute("SELECT id,email FROM users"):
        _sync_internal_group(c, r["id"], r["email"])
    c.commit()
    c.close()
    return ok(ok=True)


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
    c = conn_()
    mailer.set_mail_cfg(c, b)
    c.close()
    return ok(ok=True)


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
