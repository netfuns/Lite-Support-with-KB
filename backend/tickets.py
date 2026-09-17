"""Shared ticket operations used by web + email paths."""
import base64
import datetime
import html
import re
import secrets
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


def match_partner(conn, email):
    """The partner (代理商) whose domain list covers this address, if any."""
    if not email or "@" not in email:
        return None
    domain = email.split("@", 1)[1].lower()
    for p in conn.execute("SELECT * FROM partners"):
        ds = [d.strip().lower() for d in (p["domains"] or "").split(",") if d.strip()]
        if domain in ds or any(domain.endswith("." + d) for d in ds):
            return p
    return None


def _new_password():
    """A pronounceable-but-random credential for auto-provisioned accounts."""
    return secrets.token_urlsafe(12)


def _credentials_email(conn, to_addr, password, base_url=""):
    """Send the temporary password + TOTP-nag to a brand-new account holder."""
    import mailer  # lazy: tickets <-> mailer
    subject = "[RankEZ] Your account was created"
    # bilingual: the auto-created address is on a customer domain that we do not
    # know the locale of, so ship both English and 简体 Chinese in one mail.
    login = (base_url or "") + "/#/login"
    text = (
        "Your account on the RankEZ support portal was created automatically "
        "because you sent a message to a support ticket.\n\n"
        "Login URL: %s\n"
        "Email (your username): %s\n"
        "Temporary password: %s\n\n"
        "On first sign-in you will be asked to set up two-factor authentication "
        "(TOTP) in your authenticator app.\n"
        "---\n"
        "您的 RankEZ 售后平台账号已自动创建（您刚刚发邮件到售后时触发）。\n\n"
        "登录地址：%s\n"
        "邮箱（即用户名）：%s\n"
        "临时密码：%s\n\n"
        "首次登录时需设置两步验证（TOTP）。\n"
    ) % (login, to_addr, password, login, to_addr, password)
    htmlb = ("<p>Your account on the RankEZ support portal was created automatically.</p>"
             "<p><b>Login URL:</b> <a href='%s'>%s</a><br>"
             "<b>Email (username):</b> %s<br>"
             "<b>Temporary password:</b> %s</p>"
             "<p>On first sign-in you will be asked to set up two-factor authentication (TOTP).</p>"
             "<hr><p>您的 RankEZ 售后平台账号已自动创建。</p>"
             "<p><b>登录地址：</b><a href='%s'>%s</a><br>"
             "<b>邮箱（即用户名）：</b>%s<br>"
             "<b>临时密码：</b>%s</b></p>"
             "<p>首次登录时需设置两步验证（TOTP）。</p>"
             % (login, login, to_addr, password, login, login, to_addr, password))
    return mailer.send_email(conn, [to_addr], subject, text, htmlb)


def find_or_create_user(conn, email, display_name="", send_credentials=False):
    """Return ``{"id", "created"}`` for `email`, creating it when absent.

    Callers that pass ``send_credentials=True`` are the inbound-mail path: a
    sender on a customer domain who has no account yet is auto-provisioned with
    a random password that is e-mailed to the address, and the account is
    flagged ``require_totp`` so it enrolls TOTP on first login. Callers that
    already hold a password (web sign-up, admin create) pass the default
    ``send_credentials=False`` -- no provisioning, no e-mail.
    """
    email = (email or "").strip().lower()
    row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if row:
        return {"id": row["id"], "created": False}
    name = display_name or email.split("@", 1)[0]
    pw = _new_password() if send_credentials else ""
    cur = conn.execute(
        "INSERT INTO users(email,display_name,password_hash,require_totp) VALUES(?,?,?,?)",
        (email, name, auth.hash_password(pw) if pw else None, 1 if send_credentials else 0))
    uid = cur.lastrowid
    # e-mail-domain rule: join the matching customer / partner group (+ the
    # internal group when the domain is internal). Add-only — nothing to evict on
    # a fresh user.
    rbac.sync_email_groups(conn, uid, email, remove_stale=False)
    role = conn.execute("SELECT id FROM roles WHERE name='客户'").fetchone()
    if role:
        conn.execute("INSERT OR IGNORE INTO user_roles(user_id,role_id) VALUES(?,?)", (uid, role["id"]))
    # an address on one of the site's own domains is staff, not a customer
    rbac.align_internal_role(conn, uid, email)
    conn.commit()
    mailed = False
    if send_credentials:
        from db import get_setting
        base = get_setting(conn, "base_url", "")
        mailed = _credentials_email(conn, email, pw, base)
    return {"id": uid, "created": True, "password": pw if send_credentials else None,
            "mailed": mailed}


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
        creator_id = find_or_create_user(conn, creator_email)["id"]
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
    cur = conn.execute("INSERT INTO messages(ticket_id,author_email,body,internal,source) VALUES(?,?,?,?,?)",
                       (tid, creator_email, description, 0, source))
    # the files belong to the opening message: without the id the detail endpoint
    # (which groups attachments per message) would never show them
    _save_attachments(conn, tid, cur.lastrowid, attachments)
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
    cur = conn.execute(
        "INSERT INTO messages(ticket_id,user_id,author_email,author_name,body,internal,source) "
        "VALUES(?,?,?,?,?,?,?)",
        (ticket_id, user_id, (author_email or "").lower(), author_name, body, 1 if internal else 0, source))
    conn.execute("UPDATE tickets SET updated_at=datetime('now') WHERE id=?", (ticket_id,))
    if author_email:
        conn.execute("INSERT OR IGNORE INTO ticket_participants(ticket_id,email) VALUES(?,?)",
                     (ticket_id, author_email.lower()))
    # same reason as in create_ticket: an attachment without its message is invisible
    _save_attachments(conn, ticket_id, cur.lastrowid, attachments)
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


def _message_attachments(conn, ticket_id, message_id):
    return conn.execute(
        "SELECT * FROM attachments WHERE message_id=? OR (message_id IS NULL AND ticket_id=?)",
        (message_id, ticket_id))


def raw_dialogue(conn, t):
    """Full dialogue, identity unmasked, images inlined and files linked."""
    parts = ["# " + (t["title"] or "")]
    meta = []
    if t["version"]:
        meta.append("Version: %s" % t["version"])
    if t["product"]:
        meta.append("Module: %s" % t["product"])
    if t["customer_name"]:
        meta.append("Customer: %s" % t["customer_name"])
    if t["source"]:
        meta.append("Source: %s" % t["source"])
    if meta:
        parts.append("_%s_" % " · ".join(meta))
    if t["description"]:
        parts.append("")
        parts.append(t["description"])
    # internal notes never travel, masked or not -- the customer must not leak
    # a remark they were never allowed to read
    for m in conn.execute(
            "SELECT * FROM messages WHERE ticket_id=? AND internal=0 ORDER BY id", (t["id"],)):
        who = m["author_name"] or m["author_email"] or "user"
        when = (m["created_at"] or "")[:16]
        parts.append("")
        parts.append("**%s**  <sub>%s</sub>  \n%s" % (who, when, m["body"] or ""))
        for a in _message_attachments(conn, t["id"], m["id"]):
            if (a["content_type"] or "").startswith("image/"):
                parts.append("")
                parts.append("![%s](/files/%s)" % (a["filename"], a["stored_name"]))
            else:
                parts.append("")
                parts.append("[%s](/files/%s)" % (a["filename"], a["stored_name"]))
    return "\n".join(parts)


def _link_attachments(conn, ticket_id, article_id):
    """Expose the ticket's files on the article (same stored blob, no copy).

    A file hanging off an internal note stays behind: the note itself never
    reaches the KB (see ``raw_dialogue``), so neither may the screenshot or log
    that only the desk was meant to read.
    """
    n = 0
    rows = conn.execute(
        "SELECT * FROM attachments WHERE ticket_id=? AND article_id IS NULL "
        "AND (message_id IS NULL OR message_id IN "
        "     (SELECT id FROM messages WHERE ticket_id=? AND internal=0))",
        (ticket_id, ticket_id))
    for a in rows:
        conn.execute(
            "INSERT INTO attachments(article_id,ticket_id,filename,stored_name,content_type,size) "
            "VALUES(?,?,?,?,?,?)",
            (article_id, ticket_id, a["filename"], a["stored_name"],
             a["content_type"], a["size"]))
        n += 1
    return n


def archive_to_kb(conn, ticket_id, visibility="registered", desensitize=True,
                  share_attachments=None, close_ticket=True):
    """Publish a ticket thread as a KB article.

    * ``desensitize=True`` (default) -- the dialogue with every customer name,
      e-mail, IP and domain replaced by xxxxxx, and **no attachments at all**.
    * ``desensitize=False`` -- the raw dialogue *with* its attachments and
      images, for the case the user explicitly asked for the real data.

    Idempotent per ticket: publishing twice updates the same article instead of
    leaving duplicates behind.
    """
    t = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    if not t:
        return None
    if share_attachments is None:
        share_attachments = not desensitize
    if desensitize:
        from desens import desensitize_ticket
        body = desensitize_ticket(conn, ticket_id)
    else:
        body = raw_dialogue(conn, t)

    title = t["title"] or ("Ticket " + t["code"])
    prev = conn.execute("SELECT id FROM kb_articles WHERE ticket_id=?", (ticket_id,)).fetchone()
    if prev:
        aid = prev["id"]
        conn.execute(
            "UPDATE kb_articles SET title=?, body=?, visibility=?, desensitized=?, "
            "source='ticket', updated_at=datetime('now') WHERE id=?",
            (title, body, visibility, 1 if desensitize else 0, aid))
        conn.execute("DELETE FROM attachments WHERE article_id=?", (aid,))
    else:
        cur = conn.execute(
            "INSERT INTO kb_articles(title,body,source,visibility,author_id,ticket_id,desensitized) "
            "VALUES(?,?,?,?,?,?,?)",
            (title, body, "ticket", visibility, t["owner_id"] or t["creator_id"],
             ticket_id, 1 if desensitize else 0))
        aid = cur.lastrowid
    if share_attachments:
        _link_attachments(conn, ticket_id, aid)
    # closing the ticket and publishing it are two different intents: the close
    # button does both, the desk's "share to KB" button only shares.
    if close_ticket:
        conn.execute("UPDATE tickets SET archived=1, kb_article_id=?, status='closed' WHERE id=?",
                     (aid, ticket_id))
    else:
        conn.execute("UPDATE tickets SET archived=1, kb_article_id=? WHERE id=?",
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
