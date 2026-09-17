"""Shared ticket operations used by web + email paths."""
import base64
import datetime
import html
import json
import re
import secrets
import uuid

import auth
import md
import rbac
from db import UPLOAD_DIR
import os

STATUSES = ["new", "customer_replied", "support_replied", "closed"]


CODE_PREFIX = "RANKEZ-SUPPORT"
CODE_RE = re.compile(r"^%s-(\d{6})(\d{3})$" % CODE_PREFIX, re.I)


def _code(conn):
    """RANKEZ-SUPPORT-YYYYMMxxx -- the counter restarts every month.

    The old TK-00001 counted the whole table, so the number said nothing but
    "how busy we have ever been". Year+month makes a code sortable and lets a
    human read the age of a ticket off it; the three digits are the sequence
    within that month.
    """
    stamp = datetime.datetime.now().strftime("%Y%m")
    like = CODE_PREFIX + "-" + stamp + "%"
    row = conn.execute(
        "SELECT code FROM tickets WHERE code LIKE ? ORDER BY code DESC LIMIT 1",
        (like,)).fetchone()
    n = 1
    if row and row["code"]:
        m = CODE_RE.match(row["code"])
        if m:
            n = int(m.group(2)) + 1
    return "%s-%s%03d" % (CODE_PREFIX, stamp, n)


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


# ---- mail templates (Settings > Mail templates) ----------------------------

#: settings keys holding ``{"subject": ..., "body": <markdown>}``
TPL_NEW_USER = "tpl_new_user"
TPL_NEW_TICKET = "tpl_new_ticket"


def tpl_get(conn, key):
    """Return ``(subject, body_markdown)`` of a configured template.

    Empty strings mean "not configured" -- the caller then sends the built-in
    wording, which is the behaviour every site had before this setting existed.
    """
    from db import get_setting
    raw = get_setting(conn, key, "") or ""
    if not raw:
        return "", ""
    try:
        d = json.loads(raw)
    except Exception:  # noqa: BLE001 - a hand-edited value must not break sending
        return "", str(raw)
    if not isinstance(d, dict):
        return "", str(raw)
    return (d.get("subject") or "").strip(), (d.get("body") or "")


def send_tpl_email(conn, key, to_list, fallback, values):
    """Send the template `key`, or the built-in mail when it is not configured.

    ``fallback`` is ``(subject, text, html)``; ``values`` is the placeholder
    table (see :data:`md.TPL_VARS`). The body is Markdown, rendered to both a
    text and an HTML part, so an administrator writes it once and it looks right
    in either client.
    """
    import mailer
    subject, body = tpl_get(conn, key)
    if not (subject or body):
        return mailer.send_email(conn, to_list, fallback[0], fallback[1], fallback[2])
    vals = dict(values or {})
    subj = md.render_tpl(subject or fallback[0], vals)
    rendered = md.render_tpl(body, vals)
    return mailer.send_email(conn, to_list, subj, md.tpl_plain(rendered), md.md_to_html(rendered))


def display_title(ticket):
    """The ticket title without the ``[CODE] `` that mail-opened tickets carry.

    An e-mail ticket is named "[RANKEZ-SUPPORT-20260918001] 打印机故障" so the
    name itself shows the code. Everywhere the code is printed anyway -- the
    receipt subject, the desk notification -- repeating it just reads as a bug.
    """
    title = (ticket["title"] or "") if ticket else ""
    code = (ticket["code"] or "") if ticket else ""
    if code and title.startswith("[%s]" % code):
        return title[len(code) + 2:].strip()
    return title


def _credentials_email(conn, to_addr, password, base_url="", display_name=""):
    """Send the temporary password + TOTP-nag to a brand-new account holder."""
    subject = "[RankEZ] Your account was created"
    # bilingual: the auto-created address is on a customer domain that we do not
    # know the locale of, so ship both English and 简体 Chinese in one mail.
    login = (base_url or site_root(conn)) + "/#/login"
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
    return send_tpl_email(conn, TPL_NEW_USER, [to_addr], (subject, text, htmlb),
                          {"company": _company(conn), "portal": site_root(conn),
                           "url": login, "email": to_addr, "password": password,
                           "name": display_name or to_addr.split("@", 1)[0]})


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
    # internal group when the domain is internal). Add-only -- nothing to evict on
    # a fresh user.
    rbac.sync_email_groups(conn, uid, email, remove_stale=False)
    # The role follows the group the domain just earned. Hard-coding 客户 here
    # used to mislabel every agent whose first contact was an inbound e-mail
    # (netfuns@hotmail.com): right group, wrong role in the users list.
    rbac.align_domain_role(conn, uid, email)
    conn.commit()
    mailed = False
    if send_credentials:
        from db import get_setting
        base = get_setting(conn, "base_url", "")
        mailed = _credentials_email(conn, email, pw, base, display_name=name)
    return {"id": uid, "created": True, "password": pw if send_credentials else None,
            "mailed": mailed}


def site_root(conn):
    """The portal's front door, same fallbacks as ticket_url()."""
    from db import get_setting
    for key in ("base_url", "site_url"):
        v = (get_setting(conn, key, "") or "").strip().rstrip("/")
        if v:
            return v
    return "http://" + _local_ip()


def _company(conn):
    """Brand name used in the mail templates ({{company}})."""
    from db import get_setting
    return (get_setting(conn, "company_name", "") or "").strip() or "RankEZ"


def registration_nag_email(conn, to_addr, ticket=None):
    """Invite somebody who is on a registered domain but has no account yet.

    He was copied on a ticket, so he will be mailed about it -- but with no
    account he cannot open the link, cannot see the thread and cannot answer in
    the portal. Telling him once, with the register URL, is the difference
    between "the portal exists" and "the portal is for somebody else".
    """
    import mailer  # noqa: F401 (kept lazy so tickets <-> mailer stays import-safe)
    to = (to_addr or "").strip().lower()
    if not to or "@" not in to:
        return False
    url = site_root(conn) + "/#/register"
    who = ("ticket %s" % ticket["code"]) if ticket else "a support ticket"
    subject = "[RankEZ Support] You have no account yet / 您还没有注册售后平台"
    text = (
        "You were copied on %s, but this address has no account on the RankEZ "
        "support portal yet.\n\n"
        "Please register with this exact address to follow the ticket:\n%s\n\n"
        "---\n"
        "您被抄送了工单 %s，但此邮箱还没有注册售后平台账号。\n\n"
        "请使用此邮箱访问下面地址自行注册，即可追踪工单处理进度：\n%s\n"
    ) % (who, url, who, url)
    htmlb = (
        "<div style='font-family:Segoe UI,Arial;font-size:14px;line-height:1.6'>"
        "<p>You were copied on <b>%s</b>, but this address has no account on the "
        "RankEZ support portal yet.</p>"
        "<p><a href='%s'>%s</a></p>"
        "<hr><p>您被抄送了工单 <b>%s</b>，但此邮箱还没有注册售后平台账号。<br>"
        "请使用此邮箱访问下面地址自行注册，即可追踪工单处理进度：<br>"
        "<a href='%s'>%s</a></p></div>"
    ) % (html.escape(who), url, url, html.escape(who), url, url)
    return send_tpl_email(conn, TPL_NEW_USER, [to], (subject, text, htmlb),
                          {"company": _company(conn), "portal": site_root(conn),
                           "url": url, "email": to, "password": "",
                           "code": (ticket["code"] if ticket else ""),
                           "title": (ticket["title"] if ticket else "")})


def _local_ip():
    """The address this box answers on -- all we can print when nobody told us
    the portal's real address. A UDP connect does not send a packet, it only
    makes the kernel pick the interface a default route would use."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:  # noqa: BLE001
        return "127.0.0.1"


def ticket_url(conn, ticket):
    """Public link to one ticket: the bound domain when set, else this host's IP."""
    from db import get_setting
    base = (get_setting(conn, "base_url", "") or "").strip().rstrip("/")
    if not base:
        base = (get_setting(conn, "site_url", "") or "").strip().rstrip("/")
    if not base:
        base = "http://" + _local_ip()
    return "%s/#/ticket/%s" % (base, ticket["id"])


def notify_ticket_opened(conn, ticket):
    """Acknowledge a brand-new ticket to whoever opened it.

    This is the receipt the customer gets. A ticket opened from an e-mail is
    already named "[CODE] subject", so that name *is* the subject -- the code in
    the subject is also the threading contract (an answer that keeps it lands on
    this ticket, one that drops it opens a new one). A ticket opened on the web
    has a plain title, so the code is prefixed here.
    """
    to = (ticket["creator_email"] or "").strip().lower()
    if not to or "@" not in to:
        return False
    url = ticket_url(conn, ticket)
    title = ticket["title"] or ""
    subject = title if title.startswith("[") else ("%s %s" % (ticket["code"], display_title(ticket)))
    plain = display_title(ticket)
    text = (
        "Your request has been recorded as ticket %s.\n\n"
        "Title: %s\n\n"
        "We have received your request and will handle it as soon as possible.\n"
        "You can follow its progress here:\n%s\n\n"
        "---\n"
        "您的问题已被记录，工单号：%s\n\n"
        "标题：%s\n\n"
        "我们将尽快处理。您可以登录以下地址查看处理进度：\n%s\n"
    ) % (ticket["code"], plain, url, ticket["code"], plain, url)
    htmlb = (
        "<div style='font-family:Segoe UI,Arial;font-size:14px;line-height:1.6'>"
        "<h2>%s</h2>"
        "<p>Your request has been recorded as ticket <b>%s</b>.<br>"
        "We have received your request and will handle it as soon as possible.</p>"
        "<p><a href='%s'>%s</a></p>"
        "<hr><p>您的问题已被记录，工单号：<b>%s</b><br>"
        "我们将尽快处理。您可以登录以下地址查看处理进度：<br><a href='%s'>%s</a></p>"
        "</div>"
    ) % (html.escape(subject), html.escape(ticket["code"]), url, url,
         html.escape(ticket["code"]), url, url)
    return send_tpl_email(conn, TPL_NEW_TICKET, [to], (subject, text, htmlb),
                          {"company": _company(conn), "portal": site_root(conn),
                           "code": ticket["code"], "title": plain,
                           "url": url, "customer": ticket["customer_name"] or "",
                           "email": to, "password": ""})


def create_ticket(conn, *, title, description="", customer_name="", customer_id=None,
                  version="", product="", priority="medium", source="web",
                  creator_id=None, creator_email="", internal=0, attachments=None,
                  participant_emails=None, bracketed_title=False):
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
    # A ticket that a mail opened is *called* "[CODE] subject": the code is what
    # the customer will quote back, and putting it in the name means the name
    # alone identifies the thread. Web tickets keep their plain title.
    if bracketed_title:
        title = "[%s] %s" % (code, (title or "").strip())
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
    bind_body_files(conn, description, ticket_id=tid, message_id=cur.lastrowid)
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


#: any /files/<stored> reference inside a message body
FILE_REF_RE = re.compile(r"/files/([A-Za-z0-9_.\-]{4,})")


def bind_body_files(conn, body, *, ticket_id=None, message_id=None, article_id=None):
    """Register the pictures referenced by a body as attachments of that record.

    A pasted screenshot is uploaded *before* the message exists (the editor has
    to show it while you type), so at that moment it has no attachment row --
    and /files/<stored> serves every orphan file publicly, because that is the
    branch the branding logo uses. Once the body is saved the file belongs to
    this ticket (or article), so it must inherit that record's permissions.
    """
    import mimetypes
    for stored in set(FILE_REF_RE.findall(body or "")):
        if stored.startswith("logo_") or ".." in stored:
            continue
        row = conn.execute("SELECT * FROM attachments WHERE stored_name=?", (stored,)).fetchone()
        if row:
            if row["ticket_id"] or row["article_id"]:
                continue                    # already owned by something
            conn.execute("UPDATE attachments SET ticket_id=?, article_id=?, message_id=? WHERE id=?",
                         (ticket_id, article_id, message_id, row["id"]))
            continue
        path = os.path.join(UPLOAD_DIR, stored)
        if not os.path.isfile(path):
            continue
        ctype = mimetypes.guess_type(stored)[0] or "application/octet-stream"
        conn.execute(
            "INSERT INTO attachments(message_id,ticket_id,article_id,filename,stored_name,content_type,size) "
            "VALUES(?,?,?,?,?,?,?)",
            (message_id, ticket_id, article_id, stored, stored, ctype,
             os.path.getsize(path)))


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
    # ... and a picture pasted into the body is not an uploaded file at all
    bind_body_files(conn, body, ticket_id=ticket_id, message_id=cur.lastrowid)
    conn.commit()


def notify_new_ticket(conn, ticket):
    """Email support staff + admins about a new ticket (web source)."""
    from mailer import send_email
    ttl = display_title(ticket)
    subject = "[#%s] New ticket: %s" % (ticket["code"], ttl)
    body = "New %s ticket %s\nCustomer: %s\nModule: %s\nPriority: %s\n\n%s" % (
        ticket["source"], ticket["code"], ticket["customer_name"], ticket["product"],
        ticket["priority"], ticket["description"])
    staff = support_recipients(conn)
    # the opener already got his own receipt (with the link) -- do not mail twice
    opener = (ticket["creator_email"] or "").strip().lower()
    staff = [e for e in staff if (e or "").strip().lower() != opener]
    if staff:
        send_email(conn, staff, subject, body)


def support_recipients(conn):
    """Emails of users with ticket.view_all (support/admin)."""
    rows = conn.execute(
        "SELECT DISTINCT u.email FROM users u JOIN user_roles ur ON ur.user_id=u.id "
        "JOIN role_permissions rp ON rp.role_id=ur.role_id JOIN permissions p ON p.id=rp.perm_id "
        "WHERE p.key='ticket.view_all'")
    return [r["email"] for r in rows]


def alert_new_ticket(conn, ticket):
    """Ring the in-app bell: a ticket just arrived."""
    _push_alert(conn, ticket, "new")


def alert_customer_reply(conn, ticket):
    """... and again when the customer side answered an existing one."""
    _push_alert(conn, ticket, "customer_reply")


def _push_alert(conn, ticket, kind):
    """One row per desk user, so each of them dismisses it on his own screen.

    Only users who can actually see the whole queue get notified -- telling
    somebody about a ticket he is not allowed to open would be worse than
    telling him nothing (the code alone is a hint about a customer).
    """
    try:
        rows = conn.execute(
            "SELECT DISTINCT u.id FROM users u JOIN user_roles ur ON ur.user_id=u.id "
            "JOIN role_permissions rp ON rp.role_id=ur.role_id JOIN permissions p ON p.id=rp.perm_id "
            "WHERE p.key='ticket.view_all' AND lower(coalesce(u.status,'active'))='active'")
        for r in rows:
            conn.execute(
                "INSERT INTO ticket_alerts(user_id,ticket_id,code,kind) VALUES(?,?,?,?)",
                (r["id"], ticket["id"], ticket["code"] or "", kind))
        conn.commit()
    except Exception:  # noqa: BLE001 - a bell is a nicety, never fail the ticket for it
        pass


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
                  share_attachments=None, close_ticket=True, group_ids=None):
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
    # The audience of a "usergroup" article is exactly the set of groups it is
    # bound to, so the binding is rewritten on every publish -- re-sharing with a
    # different pick (or publishing as "registered") must not leave the previous
    # audience attached to the article.
    conn.execute("DELETE FROM kb_article_groups WHERE article_id=?", (aid,))
    for gid in (group_ids or []):
        try:
            gid = int(gid)
        except (TypeError, ValueError):
            continue
        if conn.execute("SELECT id FROM user_groups WHERE id=?", (gid,)).fetchone():
            conn.execute("INSERT OR IGNORE INTO kb_article_groups(article_id,group_id) VALUES(?,?)",
                         (aid, gid))
    # closing the ticket and publishing it are two different intents: the close
    # button does both, the desk's "share to KB" button closes the ticket too --
    # a thread only becomes knowledge once it is finished.
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
