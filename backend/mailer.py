"""Email engine: send via SMTP/O365, poll via IMAP/O365 (XOAUTH2 supported)."""
import base64
import json
import os
import re
import smtplib
import ssl
import time
from email import policy
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr
from html import escape
import imaplib
from imaplib import IMAP4_SSL
from urllib import request as urlreq

from db import UPLOAD_DIR, get_setting
import rbac


# ---------- settings helpers ----------

def mail_cfg(conn):
    keys = ["smtp_host", "smtp_port", "smtp_security", "smtp_user", "smtp_pass",
            "smtp_from", "imap_host", "imap_port", "imap_security", "imap_user",
            "imap_pass", "imap_folder", "o365_mode", "o365_tenant", "o365_client_id",
            "o365_client_secret", "o365_scope", "base_url"]
    return {k: (get_setting(conn, k, "") or "") for k in keys}


def set_mail_cfg(conn, d):
    from db import set_setting
    for k in ("smtp_host", "smtp_port", "smtp_security", "smtp_user", "smtp_pass",
              "smtp_from", "imap_host", "imap_port", "imap_security", "imap_user",
              "imap_pass", "imap_folder", "o365_mode", "o365_tenant", "o365_client_id",
              "o365_client_secret", "o365_scope", "base_url"):
        if k in d:
            set_setting(conn, k, d[k] or "")
    conn.commit()


def o365_token(cfg, mailbox):
    body = ("client_id=%s&client_secret=%s&scope=%s&grant_type=client_credentials" % (
        urlreq.pathname2url(cfg["o365_client_id"]),
        urlreq.pathname2url(cfg["o365_client_secret"]),
        urlreq.pathname2url(cfg["o365_scope"] or "https://outlook.office365.com/.default"))).encode()
    url = "https://login.microsoftonline.com/%s/oauth2/v2.0/token" % (cfg["o365_tenant"] or "common")
    req = urlreq.Request(url, data=body, method="POST")
    with urlreq.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["access_token"]


def _b64(s):
    return base64.b64encode(s.encode()).decode()


def xoauth2(user, token):
    return "user=%s\x01auth=Bearer %s\x01\x01" % (user, token)


# ---------- receiving ----------

def _dec(s):
    if not s:
        return ""
    parts = decode_header(s)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", "replace"))
        else:
            out.append(text)
    return "".join(out)


def _html_to_text(html_src):
    src = re.sub(r"(?is)<(script|style).*?</\1>", " ", html_src)
    src = re.sub(r"(?is)<br\s*/?>", "\n", src)
    src = re.sub(r"(?is)</(p|div|tr|li|h[1-6])>", "\n", src)
    txt = re.sub(r"(?s)<[^>]+>", "", src)
    return re.sub(r"\n{3,}", "\n\n", txt).strip()


def extract_body(msg):
    text = ""
    htmlb = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if "attachment" in str(part.get("Content-Disposition") or ""):
                continue
            if ct == "text/plain" and not text:
                try:
                    text = part.get_content(policy.policy).strip()
                except Exception:
                    text = str(part.get_payload(decode=True), "utf-8", "replace")
            elif ct == "text/html" and not htmlb:
                try:
                    htmlb = part.get_content(policy.policy)
                except Exception:
                    htmlb = str(part.get_payload(decode=True), "utf-8", "replace")
    else:
        try:
            content = msg.get_content(policy.policy)
        except Exception:
            content = str(msg.get_payload(decode=True) or "", "utf-8", "replace")
        if msg.get_content_type() == "text/html":
            htmlb = content
        else:
            text = content
    if not text and htmlb:
        text = _html_to_text(htmlb)
    return text or ""


def _quoted_split(s, seps=",;"):
    """Split header like 'A <a@x>, B <b@y>' honoring <...>."""
    out = []
    buf = ""
    depth = 0
    for ch in s:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch in seps and depth == 0:
            out.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        out.append(buf)
    return [x for x in (p.strip() for p in out) if x]


def _addr(s):
    try:
        name, addr = parseaddr(s)
    except Exception:
        return ""
    return (addr or "").lower()


# ---------- sending ----------

def send_email(conn, to_list, subject, text_body, html_body=None):
    cfg = mail_cfg(conn)
    if not cfg["smtp_host"] or not to_list:
        return False
    to_list = sorted({t.lower() for t in to_list if t and "@" in t})
    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr(("RankEZ Support", cfg["smtp_from"] or cfg["smtp_user"]))
    msg["To"] = ", ".join(to_list)
    msg["Subject"] = subject
    msg.attach(MIMEText(text_body or _html_to_text(html_body or ""), "plain", "utf-8"))
    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        if cfg["o365_mode"] == "1":
            user = cfg["smtp_user"] or cfg["smtp_from"]
            authstr = "user=%s\x01auth=Bearer %s\x01\x01" % (user, o365_token(cfg, user))
            port = int(cfg["smtp_port"] or 587)
            ctx = ssl.create_default_context()
            if port == 465:
                s = smtplib.SMTP_SSL(cfg["smtp_host"], port, context=ctx, timeout=30)
            else:
                s = smtplib.SMTP(cfg["smtp_host"], port, timeout=30)
                s.starttls(context=ctx)
            s.docmd("AUTH", "XOAUTH2 " + _b64(authstr))
            s.sendmail(cfg["smtp_from"] or user, to_list, msg.as_string())
            s.quit()
        else:
            port = int(cfg["smtp_port"] or 465)
            ctx = ssl.create_default_context()
            if cfg["smtp_security"] == "starttls":
                with smtplib.SMTP(cfg["smtp_host"], port, timeout=30) as s:
                    s.starttls(context=ctx)
                    s.login(cfg["smtp_user"], cfg["smtp_pass"])
                    s.sendmail(cfg["smtp_from"] or cfg["smtp_user"], to_list, msg.as_string())
            else:
                with smtplib.SMTP_SSL(cfg["smtp_host"], port, context=ctx, timeout=30) as s:
                    s.login(cfg["smtp_user"], cfg["smtp_pass"])
                    s.sendmail(cfg["smtp_from"] or cfg["smtp_user"], to_list, msg.as_string())
        return True
    except Exception:
        log = os.path.join(os.path.dirname(__file__), "data", "mail_error.log")
        try:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write("%s send failed to=%s err=%s\n" % (time.strftime("%F %T"), to_list, __import__("traceback").format_exc()))
        except Exception:
            pass
        return False


def reject_reply(cfg, from_addr, reason):
    base = ("The email address %s is not associated with any authorized customer domain, "
            "so this message was not converted into a support ticket.\n"
            "Reason: unauthorized sender domain.\n" % from_addr)
    send_email_only(cfg, [from_addr], "[RankEZ Support] Email not authorized",
                    base, "<p>%s</p>" % escape(base))


def send_email_only(cfg, to_list, subject, text_body, html_body=None):
    to_list = [t.lower() for t in to_list if t and "@" in t]
    if not cfg["smtp_host"] or not to_list:
        return False
    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr(("RankEZ Support", cfg["smtp_from"] or cfg["smtp_user"]))
    msg["To"] = ", ".join(to_list)
    msg["Subject"] = subject
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        if cfg["o365_mode"] == "1":
            user = cfg["smtp_user"] or cfg["smtp_from"]
            authstr = xoauth2(user, o365_token(cfg, user))
            port = int(cfg["smtp_port"] or 587)
            ctx = ssl.create_default_context()
            s = smtplib.SMTP_SSL(cfg["smtp_host"], port, context=ctx) if port == 465 else smtplib.SMTP(cfg["smtp_host"], port)
            if port != 465:
                s.starttls(context=ctx)
            s.docmd("AUTH", "XOAUTH2 " + _b64(authstr))
            s.sendmail(cfg["smtp_from"] or user, to_list, msg.as_string())
            s.quit()
        else:
            port = int(cfg["smtp_port"] or 465)
            ctx = ssl.create_default_context()
            cls = smtplib.SMTP_SSL if cfg["smtp_security"] != "starttls" else smtplib.SMTP
            if (cls is smtplib.SMTP_SSL):
                with cls(cfg["smtp_host"], port, context=ctx) as s:
                    s.login(cfg["smtp_user"], cfg["smtp_pass"])
                    s.sendmail(cfg["smtp_from"] or cfg["smtp_user"], to_list, msg.as_string())
            else:
                with cls(cfg["smtp_host"], port) as s:
                    s.starttls(context=ctx)
                    s.login(cfg["smtp_user"], cfg["smtp_pass"])
                    s.sendmail(cfg["smtp_from"] or cfg["smtp_user"], to_list, msg.as_string())
        return True
    except Exception:
        return False


def _imap_bytes(dat):
    """Human-readable text out of an imaplib response list."""
    try:
        parts = []
        for x in dat or []:
            if isinstance(x, (tuple, list)):
                parts += [str(y) for y in x]
            else:
                parts.append(x.decode("utf-8", "replace") if isinstance(x, bytes) else str(x))
        return " ".join(p for p in parts if p).strip()
    except Exception:  # noqa: BLE001
        return str(dat)


def _imap_select(mbox, folder):
    """SELECT `folder` and report honestly whether it opened."""
    try:
        typ, dat = mbox.select(folder)
    except Exception as e:  # noqa: BLE001
        return False, "%s: %s" % (folder, e)
    if typ == "OK":
        return True, ""
    return False, "SELECT %s refused: %s" % (folder, _imap_bytes(dat))


def _imap_identify(mbox, user):
    """Send RFC 2971 ID -- 163/126 will not open a folder without it.

    imaplib ships no ID command: it is missing from its `Commands` table, so
    `_simple_command("ID", ...)` raises before anything reaches the wire -- which
    is why an earlier attempt at this silently did nothing. 163 answers every
    SELECT from an unidentified client with
    "Unsafe Login. Please contact kefu@188.com for help".
    """
    try:
        if "ID" not in imaplib.Commands:
            imaplib.Commands["ID"] = ("AUTH", "SELECTED")
        arg = '("name" "rankez-support" "version" "1.0" "vendor" "rankez" "contact" "%s")' \
              % (user or "").replace('"', "").replace("\\", "")
        typ, _dat = mbox._simple_command("ID", arg)
        try:
            mbox._untagged_response("OK", [], "ID")
        except Exception:  # noqa: BLE001
            pass
        return typ == "OK"
    except Exception:  # noqa: BLE001 - a server without ID simply ignores it
        return False


def receive_once(conn):
    """Connect IMAP, process unseen messages, return count handled."""
    cfg = mail_cfg(conn)
    if not cfg["imap_host"] or not cfg["imap_user"]:
        return 0
    handled = 0
    ctx = ssl.create_default_context()
    port = int(cfg["imap_port"] or 993)
    mbox = IMAP4_SSL(cfg["imap_host"], port, ssl_context=ctx)
    if cfg["o365_mode"] == "1":
        token = o365_token(cfg, cfg["imap_user"])
        authstr = xoauth2(cfg["imap_user"], token).encode()
        try:
            authstr = ("user=%s\x01auth=Bearer %s\x01\x01" % (cfg["imap_user"], token)).encode()
        except Exception:
            pass
        try:
            mbox.authenticate("XOAUTH2", lambda x: authstr)
        except Exception:
            mbox.login(cfg["imap_user"], cfg["imap_pass"] or "")
    else:
        mbox.login(cfg["imap_user"], cfg["imap_pass"])
    try:
        folder = cfg["imap_folder"] or "INBOX"
        # 163/126 answer "Unsafe Login" to any SELECT from a client that has not
        # identified itself first; the command is cheap, so always send it.
        _imap_identify(mbox, cfg["imap_user"])
        # imaplib's select() does *not* raise when the server refuses the folder:
        # it just drops back to state AUTH. The next command then fails far away
        # from the real cause with "SEARCH illegal in state AUTH". Check it.
        opened, err = _imap_select(mbox, folder)
        if not opened and (folder or "").strip().upper() != "INBOX":
            opened, err = _imap_select(mbox, "INBOX")
        if not opened:
            raise RuntimeError(err or ("cannot open mailbox %s" % folder))
        typ, data = mbox.search(None, "UNSEEN")
        ids = (data[0] or b"").split()
        for num in ids[:50]:
            try:
                if _handle_msg(conn, mbox, num, cfg):
                    handled += 1
            except Exception:
                pass
    finally:
        try:
            mbox.logout()
        except Exception:
            pass
    return handled


def _handle_msg(conn, mbox, num, cfg):
    typ, d = mbox.fetch(num, "(BODY.PEEK[HEADER])")
    raw = b""
    for part in d:
        if isinstance(part, tuple) and part[0]:
            raw = part[1]
            break
    header = email.message_from_bytes(raw, policy=policy.default) if raw else None
    subject = _dec(header["Subject"]) if header and header["Subject"] else ""
    frm = _addr(header["From"] if header and header["From"] else "")
    to_list = [_addr(x) for x in _quoted_split(str(header["To"] or ""))] + \
              [_addr(x) for x in _quoted_split(str(header["Cc"] or ""))]
    to_list = [x for x in to_list if x]
    frm_name = ""
    if header and header["From"]:
        try:
            nm, _ = parseaddr(str(header["From"]))
            frm_name = _dec(nm)
        except Exception:
            pass

    # fetch body
    typ, d2 = mbox.fetch(num, "(RFC822)")
    body_raw = None
    for part in d2:
        if isinstance(part, tuple) and part[0]:
            body_raw = part[1]
            break
    msg = email.message_from_bytes(body_raw or b"", policy=policy.default)
    body = extract_body(msg)

    # attachments: images only (for KB-safe), store up to 5MB
    atts = []
    if msg.is_multipart():
        for part in msg.walk():
            fn = part.get_filename()
            if fn and part.get_content_disposition() == "attachment":
                fn = _dec(fn)
                try:
                    data = part.get_payload(decode=True) or b""
                except Exception:
                    continue
                if 0 < len(data) <= 5 * 1024 * 1024:
                    atts.append({"filename": fn, "data": data,
                                 "content_type": part.get_content_type()})
    mbox.store(num, "+FLAGS", "\\Seen")

    return process_incoming_email(conn, subject=subject, frm=frm, frm_name=frm_name,
                                  to_list=to_list, body=body, atts=atts, cfg=cfg)


# ---------- incoming logic (also used by tests / webhook) ----------

REF_RE = re.compile(r"\bTK-\d{5}\b", re.I)


def process_incoming_email(conn, *, subject, frm, frm_name, to_list, body, atts=None, cfg=None):
    """Core: match customer by domain, create or reply ticket, or reject + reply."""
    import tickets as T
    if not frm:
        return {"status": "ignored"}
    frm = frm.lower()
    cust = T.match_customer(conn, frm)
    # The internal desk may answer by e-mail too: their address belongs to no
    # customer domain, so without this they were bounced as "unauthorized" and
    # their answer never reached the customer.
    staff_sender = frm in [e.lower() for e in _support_emails(conn)] \
        or rbac.is_internal_email(conn, frm)
    base_url = (cfg or {}).get("base_url", "")
    m = REF_RE.search(subject or "")
    # An e-mail from a KNOWN customer domain always gets a user record: the
    # inbound path auto-provisions one (random password + TOTP-on-first-login,
    # e-mailed to the sender) when none exists yet. This is what turns a
    # first-time customer's mail into a login-capable account instead of a
    # "rejected: unauthorized" bounce. (Staff senders are handled below.)
    sender_info = {"id": None, "created": False, "mailed": False}
    if cust and frm and not staff_sender:
        sender_info = T.find_or_create_user(conn, frm, frm_name, send_credentials=True)
    if not cust and staff_sender:
        t = conn.execute("SELECT * FROM tickets WHERE code=?", (m.group(0).upper(),)).fetchone() if m else None
        if not t:
            # nothing to attach: never auto-create a ticket with no customer behind it
            return {"status": "ignored", "reason": "staff mail without a known ticket"}
        T.add_message(conn, t["id"], body=body, author_email=frm, author_name=frm_name,
                      source="email", attachments=atts)
        conn.execute("UPDATE tickets SET status='support_replied' WHERE id=?", (t["id"],))
        conn.commit()
        _notify_customer_reply(conn, t, frm)
        return {"status": "replied", "ticket": t["code"], "created_user": bool(sender_info["created"])}
    if not cust:
        cfg = cfg or mail_cfg(conn)
        reject_reply(cfg, frm, "unauthorized")
        return {"status": "rejected", "reason": "unauthorized domain"}
    if m:
        code = m.group(0).upper()
        t = conn.execute("SELECT * FROM tickets WHERE code=?", (code,)).fetchone()
        if t:
            same_customer = (cust["id"] == t["customer_id"])
            if same_customer:
                # make sure the replying sender has a user row (auto-provisioned
                # above when they were new) so the reply is attributed correctly
                if sender_info["id"]:
                    uid = sender_info["id"]
                else:
                    uid = T.find_or_create_user(conn, frm, frm_name)["id"]
                T.add_message(conn, t["id"], body=body, user_id=uid, author_email=frm,
                              author_name=frm_name, source="email", attachments=atts)
                # the reply drives the status: a desk answer -> 售后已答复 (and the
                # customer is told), a customer answer -> 客户已答复 (and the desk
                # is told).
                conn.execute("UPDATE tickets SET status=? WHERE id=?",
                             ("support_replied" if staff_sender else "customer_replied", t["id"]))
                conn.commit()
                if staff_sender:
                    _notify_customer_reply(conn, t, frm)
                else:
                    _notify_reply(conn, t, frm)
                return {"status": "replied", "ticket": code, "created_user": bool(sender_info["created"])}
    # new ticket
    clean_sub = REF_RE.sub("", subject or "").strip()
    clean_sub = re.sub(r"(?i)^(re|fw|fwd)\s*:\s*", "", clean_sub).strip() or "(email ticket)"
    # the user row was auto-provisioned above when the sender was new; hand
    # the id to create_ticket so it does not have to redo the work
    t = T.create_ticket(
        conn, title=clean_sub[:200], description=body, customer_id=cust["id"],
        source="email", creator_id=sender_info["id"] or None, creator_email=frm,
        participant_emails=to_list + [frm],
        version=cust["version"] or "", attachments=atts)
    staff = T.support_recipients(conn)
    all_replies = sorted(set(staff + to_list + [frm]))
    htmlb = _render_email(conn, t, conn.execute(
        "SELECT * FROM messages WHERE ticket_id=? ORDER BY id", (t["id"],)).fetchall(), base_url)
    send_email(conn, all_replies, "[#%s] %s" % (t["code"], t["title"]),
               "Ticket %s created from email.\n\n%s" % (t["code"], body), htmlb)
    return {"status": "created", "ticket": t["code"], "created_user": bool(sender_info["created"]),
            "user_mailed": bool(sender_info.get("mailed", False))}


def _notify_reply(conn, t, frm):
    """A customer answered by e-mail -> tell the desk."""
    staff = [e for e in _support_emails(conn) if e != (frm or "").lower()]
    htmlb = _render_email(conn, t, conn.execute(
        "SELECT * FROM messages WHERE ticket_id=? ORDER BY id", (t["id"],)).fetchall())
    send_email(conn, staff, "Re:[#%s] %s" % (t["code"], t["title"]),
               "Customer replied on ticket %s: %s" % (t["code"], t["title"]), htmlb)


def _notify_customer_reply(conn, t, frm):
    """The desk answered by e-mail -> tell the customer side."""
    parts = [r["email"] for r in conn.execute(
        "SELECT email FROM ticket_participants WHERE ticket_id=?", (t["id"],))]
    if t["customer_id"]:
        cust = conn.execute("SELECT contact_email FROM customers WHERE id=?",
                            (t["customer_id"],)).fetchone()
        if cust and cust["contact_email"]:
            parts.append(cust["contact_email"])
    parts = sorted({e.lower() for e in parts if e and e.lower() != (frm or "").lower()})
    if not parts:
        return
    htmlb = _render_email(conn, t, conn.execute(
        "SELECT * FROM messages WHERE ticket_id=? ORDER BY id", (t["id"],)).fetchall())
    send_email(conn, parts, "Re:[#%s] %s" % (t["code"], t["title"]),
               "Support replied on ticket %s." % t["code"], htmlb)


def _support_emails(conn):
    rows = conn.execute(
        "SELECT DISTINCT u.email FROM users u JOIN user_roles ur ON ur.user_id=u.id "
        "JOIN role_permissions rp ON rp.role_id=ur.role_id JOIN permissions p ON p.id=rp.perm_id "
        "WHERE p.key='ticket.view_all'")
    return [r["email"] for r in rows]


def _render_email(conn, t, messages, base_url=""):
    parts = ["<div style='font-family:Segoe UI,Arial;color:#1f2933;font-size:14px;line-height:1.6'>"]
    parts.append("<h2>[%s] %s</h2>" % (t["code"], escape(t["title"])))
    parts.append("<p style='color:#616e7c'>Customer: %s · Priority: %s · Status: %s</p>" % (
        escape(t["customer_name"] or "-"), t["priority"], t["status"]))
    for msg in messages:
        who = msg["author_name"] or msg["author_email"] or "user"
        parts.append("<div style='margin:10px 0;padding:10px 12px;border:1px solid #e4e7eb;border-radius:8px'>")
        parts.append("<b style='color:#0b63ce'>%s</b> <span style='color:#9aa5b1'>%s</span>" % (escape(who), msg["created_at"]))
        parts.append("<div style='white-space:pre-wrap'>%s---</div>" % escape(msg["body"] or ""))
    if base_url:
        parts.append("<p style='color:#9aa5b1'>Reply to this email to update the ticket #%s.</p>" % t["code"])
    parts.append("</div>")
    return "".join(parts)


# imports used in _handle_msg / top level
import email  # noqa: E402
