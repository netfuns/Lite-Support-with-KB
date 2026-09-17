"""Shared ticket operations used by web + email paths."""
import base64
import datetime
import html
import re
import uuid

import auth
import rbac
from db import UPLOAD_DIR
import os

STATUSES = ["new", "customer_replied", "support_replied", "closed"]


def _code(conn):
    n = conn.execute("SELECT COUNT(*) c FROM tickets").fetchone()["c"] + 1
    return "TK-%05d" % n


def match_customer(conn, email):
    if not email or "@" not in email:
        return None
    domain = email.split("@", 1)[1].lower()
    for c in conn.execute("SELECT * FROM customers"):
        ds = [d.strip().lower() for d in (c["domains"] or "").split(",") if d.strip()]
        if domain in ds or any(domain.endswith("." + d) for d in ds):
            return c
    return None


def find_or_create_user(conn, email, display_name=""):
    email = (email or "").strip().lower()
    row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if row:
        return row["id"]
    name = display_name or email.split("@", 1)[0]
    cur = conn.execute("INSERT INTO users(email,display_name) VALUES(?,?)", (email, name))
    uid = cur.lastrowid
    # e-mail-domain rule: join the matching customer group (+ the internal group
    # when the domain is internal). Add-only — nothing to evict on a fresh user.
    rbac.sync_email_groups(conn, uid, email, remove_stale=False)
    # default role customer
    role = conn.execute("SELECT id FROM roles WHERE name='客户'").fetchone()
    if role:
        conn.execute("INSERT OR IGNORE INTO user_roles(user_id,role_id) VALUES(?,?)", (uid, role["id"]))
    conn.commit()
    return uid


def create_ticket(conn, *, title, description="", customer_name="", customer_id=None,
                  version="", product="", priority="medium", source="web",
                  creator_id=None, creator_email="", internal=0, attachments=None,
                  participant_emails=None):
    # resolve customer by name (match or auto-create) when provided
    if customer_name and not customer_id:
        c = conn.execute("SELECT * FROM customers WHERE lower(name)=lower(?)", (customer_name,)).fetchone()
        if not c:
            cur = conn.execute("INSERT INTO customers(name) VALUES(?)", (customer_name,))
            cid = cur.lastrowid
            rbac.ensure_customer_group(conn, cid, customer_name)
        else:
            cid = c["id"]
        customer_id = cid
    if creator_email and not creator_id:
        creator_id = find_or_create_user(conn, creator_email)
    cust_name = ""
    if customer_id:
        c = conn.execute("SELECT name FROM customers WHERE id=?", (customer_id,)).fetchone()
        cust_name = c["name"] if c else ""

    code = _code(conn)
    cur = conn.execute(
        "INSERT INTO tickets(code,title,description,customer_id,customer_name,version,product,"
        "priority,status,source,creator_id,creator_email,internal) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, title, description, customer_id, cust_name, version, product,
         priority, "new", source, creator_id, (creator_email or "").lower(), 1 if internal else 0))
    tid = cur.lastrowid
    conn.execute("INSERT OR IGNORE INTO ticket_participants(ticket_id,email) VALUES(?,?)",
                 (tid, (creator_email or "").lower()))
    for e in (participant_emails or []):
        conn.execute("INSERT OR IGNORE INTO ticket_participants(ticket_id,email) VALUES(?,?)",
                     (tid, e.lower()))
    conn.execute("INSERT INTO messages(ticket_id,author_email,body,internal,source) VALUES(?,?,?,?,?)",
                 (tid, creator_email, description, 0, source))
    _save_attachments(conn, tid, None, attachments)
    conn.commit()
    return conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()


def _save_attachments(conn, ticket_id, message_id, attachments):
    if not attachments:
        return
    for a in attachments:
        fn = a.get("filename")
        data = a.get("data")
        if not fn or data is None:
            continue
        stored = uuid.uuid4().hex + os.path.splitext(fn)[1].lower()
        with open(os.path.join(UPLOAD_DIR, stored), "wb") as fh:
            fh.write(data)
        conn.execute(
            "INSERT INTO attachments(message_id,ticket_id,filename,stored_name,content_type,size) "
            "VALUES(?,?,?,?,?,?)",
            (message_id, ticket_id, fn, stored, a.get("content_type", ""), len(data)))


def add_message(conn, ticket_id, *, body, user_id=None, author_email="", author_name="",
                internal=0, source="web", attachments=None):
    conn.execute(
        "INSERT INTO messages(ticket_id,user_id,author_email,author_name,body,internal,source) "
        "VALUES(?,?,?,?,?,?,?)",
        (ticket_id, user_id, (author_email or "").lower(), author_name, body, 1 if internal else 0, source))
    conn.execute("UPDATE tickets SET updated_at=datetime('now') WHERE id=?", (ticket_id,))
    if author_email:
        conn.execute("INSERT OR IGNORE INTO ticket_participants(ticket_id,email) VALUES(?,?)",
                     (ticket_id, author_email.lower()))
    _save_attachments(conn, ticket_id, None, attachments)
    conn.commit()


def notify_new_ticket(conn, ticket):
    """Email support staff + admins about a new ticket (web source)."""
    from mailer import send_email
    subject = "[#%s] New ticket: %s" % (ticket["code"], ticket["title"])
    body = "New %s ticket %s\nCustomer: %s\nModule: %s\nPriority: %s\n\n%s" % (
        ticket["source"], ticket["code"], ticket["customer_name"], ticket["product"],
        ticket["priority"], ticket["description"])
    staff = support_recipients(conn)
    if staff:
        send_email(conn, staff, subject, body)


def support_recipients(conn):
    """Emails of users with ticket.view_all (support/admin)."""
    rows = conn.execute(
        "SELECT DISTINCT u.email FROM users u JOIN user_roles ur ON ur.user_id=u.id "
        "JOIN role_permissions rp ON rp.role_id=ur.role_id JOIN permissions p ON p.id=rp.perm_id "
        "WHERE p.key='ticket.view_all'")
    return [r["email"] for r in rows]


def archive_to_kb(conn, ticket_id, visibility="registered", desensitize=True):
    t = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    if not t:
        return None
    if desensitize:
        from desens import desensitize_ticket
        body = desensitize_ticket(conn, ticket_id)
    else:
        parts = ["# " + (t["title"] or "")]
        for m in conn.execute("SELECT * FROM messages WHERE ticket_id=? ORDER BY id", (ticket_id,)):
            who = m["author_name"] or m["author_email"] or "user"
            parts.append("\n**%s**  \n%s" % (who, m["body"]))
        body = "\n".join(parts)
    cur = conn.execute(
        "INSERT INTO kb_articles(title,body,source,visibility,author_id,ticket_id,desensitized) "
        "VALUES(?,?,?,?,?,?,?)",
        (t["title"] or ("Ticket " + t["code"]), body, "ticket", visibility,
         t["owner_id"] or t["creator_id"], ticket_id, 1 if desensitize else 0))
    aid = cur.lastrowid
    conn.execute("UPDATE tickets SET archived=1, kb_article_id=?, status='closed' WHERE id=?",
                 (aid, ticket_id))
    conn.commit()
    return aid


def render_ticket_email_html(conn, ticket, messages):
    """Build the HTML body of a ticket notification email (renders dialogue + images inline)."""
    css = "body{font-family:Segoe UI,Arial,sans-serif;color:#1f2933;font-size:14px;line-height:1.6}"
    parts = ["<!doctype html><html><head><meta charset='utf-8'><style>%s</style></head><body>" % css]
    parts.append("<h2>[%s] %s</h2>" % (html.escape(ticket["code"]), html.escape(ticket["title"])))
    parts.append("<p style='color:#616e7c'>Customer: %s · Module: %s · Priority: %s · Status: %s</p>" % (
        html.escape(ticket["customer_name"] or "-"), html.escape(ticket["product"] or "-"),
        ticket["priority"], ticket["status"]))
    parts.append("<hr>")
    for m in messages:
        who = m["author_name"] or m["author_email"] or "user"
        parts.append("<div style='margin:12px 0;padding:10px 12px;border:1px solid #e4e7eb;border-radius:8px'>")
        parts.append("<div style='font-weight:600;color:#0b63ce'>%s <span style='color:#9aa5b1;font-weight:400'>%s</span></div>" % (
            html.escape(who), m["created_at"]))
        parts.append("<div style='white-space:pre-wrap'>%s</div>" % html.escape(m["body"] or ""))
        # inline images
        for a in conn.execute("SELECT * FROM attachments WHERE message_id=? OR (message_id IS NULL AND ticket_id=?)",
                              (m["id"], ticket["id"])):
            if a["content_type"].startswith("image/"):
                path = os.path.join(UPLOAD_DIR, a["stored_name"])
                if os.path.exists(path):
                    with open(path, "rb") as fh:
                        b64 = base64.b64encode(fh.read()).decode()
                    parts.append("<img src='data:%s;base64,%s' style='max-width:100%%'>" % (a["content_type"], b64))
        parts.append("</div>")
    parts.append("</body></html>")
    return "".join(parts)
