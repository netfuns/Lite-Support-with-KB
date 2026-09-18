"""Email engine: send via SMTP/O365, poll via IMAP/O365 (XOAUTH2 supported)."""
import base64
import json
import os
import re
import smtplib
import ssl
import time
import uuid
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
import md
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
    # An 8-bit header (raw GBK in From/Subject, very common on Chinese webmail)
    # reaches us as a surrogate-escaped str; put the bytes back and decode them
    # with the charset they actually are.
    if any("\udc80" <= ch <= "\udcff" for ch in s):
        try:
            s = _decode_bytes(s.encode("utf-8", "surrogateescape"), "")
        except Exception:  # noqa: BLE001 - a header must never break a mail
            pass
        else:
            return s
    parts = decode_header(s)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(_decode_bytes(text, (enc or "").lower()))
        else:
            out.append(text)
    return "".join(out)


def _html_to_text(html_src):
    src = re.sub(r"(?is)<(script|style).*?</\1>", " ", html_src)
    src = re.sub(r"(?is)<br\s*/?>", "\n", src)
    src = re.sub(r"(?is)</(p|div|tr|li|h[1-6])>", "\n", src)
    txt = re.sub(r"(?s)<[^>]+>", "", src)
    return re.sub(r"\n{3,}", "\n\n", txt).strip()


#: charsets to try when the declared one is missing or wrong. gb18030 is a
#: superset of gbk/gb2312, so one entry covers every simplified-Chinese label
#: that Chinese webmail clients actually put in the header.
CHARSET_CHAIN = ("utf-8", "gb18030", "big5", "shift_jis", "euc-kr", "cp1252", "latin-1")

#: labels that lie: 163/QQ label GBK text as "gb2312", Outlook as "ansi"
_CHARSET_ALIAS = {
    "gb2312": "gb18030", "gbk": "gb18030", "gb_2312-80": "gb18030",
    "ansi": "gb18030", "cp936": "gb18030", "ms936": "gb18030",
    "gb18030": "gb18030", "cht": "big5", "ms950": "big5", "cp950": "big5",
}

_CJK_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def _decode_bytes(raw, declared):
    """Best-effort decode of one MIME part, honouring a lying charset header.

    The order matters, and each step is here because it was observed in the wild:

    1. **strict UTF-8 first.** Bytes that decode as UTF-8 *as a whole* are UTF-8:
       a GBK body cannot fake that, because GBK lead bytes live in 0x81-0xFE and
       its trail bytes reach down to 0x40, which breaks UTF-8's continuation
       pattern. This is what catches a webmail client that labels a UTF-8 body
       ``charset="gb2312"`` -- and it is deterministic, unlike the old
       "first decode that yields any CJK" rule, which could accept the gb18030
       reading of a UTF-8 body and silently return plausible-looking garbage.
    2. **the declared charset** (via :data:`_CHARSET_ALIAS`, since 163/QQ label
       GBK text as "gb2312" and Outlook calls it "ansi").
    3. **the usual suspects** in :data:`CHARSET_CHAIN`.

    The very last resort is latin-1, which is deliberately *lossless*: it maps
    every one of the 256 byte values, so this function can never emit U+FFFD.
    That matters because a replacement character in the database is
    unrecoverable -- it has already thrown the original bytes away. (A ticket
    that arrived as ``please 帮看看`` was stored as ``please \\ufffd\\ufff4\\ufffd\\ufffd``
    by exactly that mistake; the bytes ``b0 ef bf b4 bf b4`` were decoded as
    UTF-8 with ``errors="replace"``.)
    """
    def try_enc(enc):
        try:
            return raw.decode(enc), None
        except (UnicodeDecodeError, LookupError):
            return None, enc

    utf8_text, _ = try_enc("utf-8")
    if utf8_text is not None and _CJK_RE.search(utf8_text):
        return utf8_text

    chain = []
    if declared:
        chain.append(_CHARSET_ALIAS.get(declared, declared))
    chain.extend(CHARSET_CHAIN)
    seen = set()
    first = None
    for enc in chain:
        if not enc or enc in seen:
            continue
        seen.add(enc)
        text, _ = try_enc(enc)
        if text is None:
            continue
        if first is None:
            first = text
        # a decode that produced no CJK at all is suspect when another one did
        if _CJK_RE.search(text):
            return text
    if first is not None:
        return first
    if utf8_text is not None:      # pure ASCII: nothing CJK to prefer
        return utf8_text
    return raw.decode("latin-1")   # never lossy -- see the docstring


def _part_text(part):
    """Decode one body part to ``str``.

    ``part.get_content()`` cannot be used here: it takes no arguments, so the
    old ``part.get_content(policy.policy)`` raised every single time and the
    surrounding ``except`` fell back to ``str(payload, "utf-8", "replace")``.
    That is why Chinese mail arrived as mojibake while English mail was fine.
    """
    try:
        raw = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001
        return ""
    if raw is None:
        val = part.get_payload()
        return val if isinstance(val, str) else ""
    return _decode_bytes(raw, (part.get_content_charset() or "").strip().lower())



# ---------- trimming the quoted thread ----------
#
# A reply always arrives with the conversation underneath it: the client appends
# the message it answers so the reader keeps the context. A ticket body is not a
# conversation log -- it is that one message -- so the quoted part is cut before
# anything is stored. Otherwise the third reply of a thread files the two
# earlier ones a second time, and every round makes the next one longer.
#
# The cut is deliberately conservative: truncating a customer's own text is
# worse than keeping a quoted trailer, because the trailer is at least readable.
# So a marker only ends the message when it is unambiguous.

#: ">" is the universal quoting character, and it is exactly what
#: :func:`md.html_to_md` emits for every ``<blockquote>`` -- so a Gmail, Apple
#: Mail or 163 quote is recognised in the converted Markdown without having to
#: guess at the client's HTML wrappers.
_QUOTE_LINE = re.compile(r"^\s*>")

#: Markers that can only ever introduce a quote, so the message ends at them.
#: ``On ... wrote:`` (Gmail/Apple), ``在 ... 写道：`` (Chinese clients),
#: Outlook's ``_____`` and its ``----- 原始邮件 -----`` divider.
_QUOTE_CUT = (
    re.compile(r"^\s*_{6,}\s*$"),
    re.compile(r"^\s*-{2,}\s*(?:原始邮件|原始郵件|转发邮件|转发郵件|"
               r"original message|forwarded message)\s*-{2,}\s*$", re.I),
    re.compile(r"^\s*(?:在|On)\s+\S.{2,120}?(?:写道|寫道|wrote)\s*[:：]?\s*$", re.I),
    re.compile(r"^\s*.{1,80}?(?:写道|寫道)\s*[:：]\s*$"),
)

#: A single ``From:`` line is *not* a quote marker: customers paste log files
#: and mail headers into tickets all the time. The Outlook header dump is a
#: *block* of these fields, so two or more of them close together is what
#: actually marks the start of the quote.
_QUOTE_FIELD = re.compile(
    r"^\s*(?:发件人|寄件者|差出人|发信人|发送时间|发送日期|时间|收件人|抄送|主题|"
    r"from|sent|to|cc|subject|date)\s*[:：]\s*\S", re.I)
_QUOTE_FIELD_MIN = 2
_QUOTE_FIELD_WINDOW = 8


def _quote_cut_index(lines):
    """Index of the first line that starts the quoted thread, or ``None``."""
    for i, line in enumerate(lines):
        if any(rx.match(line) for rx in _QUOTE_CUT):
            return i
        if _QUOTE_FIELD.match(line):
            window = lines[i:i + _QUOTE_FIELD_WINDOW]
            if sum(1 for w in window if _QUOTE_FIELD.match(w)) >= _QUOTE_FIELD_MIN:
                return i
    return None


def _tidy(text):
    return re.sub(r"\n{3,}", "\n\n", text.strip())


def strip_quoted(text):
    """Keep only what the sender wrote this time, dropping the quoted thread.

    Two passes, in this order:

    1. cut at the first unambiguous marker (attribution, divider, header block);
       if that leaves nothing, the mail is a bare forward and the quote *is* the
       content, so nothing is cut -- better a long ticket than an empty one;
    2. remove any remaining quoted lines (``> ...``), which also covers an
       inline reply that sits *below* the quote, where a plain cut would have
       thrown the sender's own words away with it.

    An empty result falls back to the original text for the same reason.
    """
    if not text or not text.strip():
        return text or ""
    lines = text.split("\n")
    cut = _quote_cut_index(lines)
    if cut is not None:
        head = _tidy("\n".join(lines[:cut]))
        if head:
            return head
    body = _tidy("\n".join(l for l in lines if not _QUOTE_LINE.match(l)))
    return body or _tidy(text)


def extract_body(msg, images=None):
    """The mail body as Markdown, with the quoted thread removed.

    A customer writes his problem in a webmail or in Outlook, so the part that
    carries the formatting is text/html -- and the part that loses it is
    text/plain. Reading only the plain part (what this used to do) turned a
    screenshot into a bare "图片" placeholder and flattened every list and table
    into one paragraph. The HTML part is converted instead; ``images`` maps the
    Content-IDs of the inline pictures to their saved /files/ URL so they are
    rendered in the ticket rather than dropped.

    The result goes through :func:`strip_quoted`: what is stored is the message
    itself, not the message plus every mail it answers.
    """
    text = ""
    htmlb = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if "attachment" in str(part.get("Content-Disposition") or ""):
                continue
            if ct == "text/plain" and not text:
                text = _part_text(part).strip()
            elif ct == "text/html" and not htmlb:
                htmlb = _part_text(part)
    else:
        content = _part_text(msg)
        if msg.get_content_type() == "text/html":
            htmlb = content
        else:
            text = content
    if htmlb:
        converted = md.html_to_md(htmlb, images)
        # a mail whose HTML part is a bare wrapper around the same sentence
        # converts to the same thing -- but if the conversion ate everything
        # (an image-only mail with unmapped pictures), the plain part still
        # tells the reader more than an empty ticket.
        if converted:
            return strip_quoted(converted)
    return strip_quoted(text or "")


def save_inline_images(msg):
    """Write the pictures embedded *in* the body (``cid:``) to the uploads dir.

    Returns ``({cid: "/files/<stored>"}, [attachment dicts])``. The map is handed
    to :func:`extract_body` so the image renders inside the ticket body; the
    files are registered as attachments later, when the body is saved
    (``tickets.bind_body_files``), which is what puts them behind the ticket's
    permissions instead of being world-readable orphans.
    """
    mapping = {}
    saved = []
    if not msg.is_multipart():
        return mapping, saved
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        ct = (part.get_content_type() or "").lower()
        if not ct.startswith("image/"):
            continue
        cid = str(part.get("Content-ID") or "").strip().strip("<>")
        disp = (part.get_content_disposition() or "").lower()
        # an inline picture is a screenshot the sender pasted; a named one is a
        # signature logo. Only the former has a Content-ID to point at.
        if not cid:
            continue
        try:
            data = part.get_payload(decode=True) or b""
        except Exception:  # noqa: BLE001
            continue
        if not (0 < len(data) <= 5 * 1024 * 1024):
            continue
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
               "image/webp": ".webp", "image/bmp": ".bmp"}.get(ct, ".png")
        stored = uuid.uuid4().hex + ext
        try:
            with open(os.path.join(UPLOAD_DIR, stored), "wb") as fh:
                fh.write(data)
        except Exception:  # noqa: BLE001 - a broken picture must not lose the mail
            continue
        mapping[cid] = "/files/" + stored
        mapping[cid.lower()] = "/files/" + stored
        if disp == "attachment":
            # the sender also listed it as a file: keep it on the attachment bar
            saved.append({"filename": _dec(part.get_filename() or "image" + ext),
                          "data": data, "content_type": ct, "stored": stored})
    return mapping, saved


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
    msg["From"] = formataddr(("Example Support", cfg["smtp_from"] or cfg["smtp_user"]))
    msg["To"] = ", ".join(to_list)
    msg["Subject"] = subject
    # Marks the mail as ours. Every notification is addressed to the ticket's
    # participants -- which includes the mailbox we poll -- so without this the
    # app reads its own answer back in and answers that too, one ticket turning
    # into an endless chain of reply notifications.
    msg["Auto-Submitted"] = "auto-generated"
    msg["X-Example-Auto"] = "1"
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
    send_email_only(cfg, [from_addr], "[Example Support] Email not authorized",
                    base, "<p>%s</p>" % escape(base))


def send_email_only(cfg, to_list, subject, text_body, html_body=None):
    to_list = [t.lower() for t in to_list if t and "@" in t]
    if not cfg["smtp_host"] or not to_list:
        return False
    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr(("Example Support", cfg["smtp_from"] or cfg["smtp_user"]))
    msg["To"] = ", ".join(to_list)
    msg["Subject"] = subject
    # Marks the mail as ours. Every notification is addressed to the ticket's
    # participants -- which includes the mailbox we poll -- so without this the
    # app reads its own answer back in and answers that too, one ticket turning
    # into an endless chain of reply notifications.
    msg["Auto-Submitted"] = "auto-generated"
    msg["X-Example-Auto"] = "1"
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
        arg = '("name" "example-support" "version" "1.0" "vendor" "example" "contact" "%s")' \
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


def _is_own_mail(cfg, header):
    """True when this message was sent by the app itself.

    Two independent marks, because either alone leaks: the header survives a
    forward, the address does not -- and the address catches mail sent before
    this version, the header does not.
    """
    try:
        if (header.get("X-Example-Auto") or "").strip():
            return True
        if (header.get("Auto-Submitted") or "").strip().lower().startswith("auto-"):
            return True
        own = {(cfg.get("imap_user") or "").lower(), (cfg.get("smtp_from") or "").lower(),
               (cfg.get("smtp_user") or "").lower()}
        own.discard("")
        if not own:
            return False
        return _addr(str(header.get("From") or "")).lower() in own
    except Exception:  # noqa: BLE001
        return False


BOUNCE_RE = re.compile(
    r"(?i)(退信|投递失败|邮件投递失败|无法投递|"
    r"undeliver|delivery\s+(status|failure)|delivery\s+has\s+failed|"
    r"mail\s+delivery\s+failed|returned\s+to\s+sender|failure\s+notice|"
    r"delivery\s+notification|auto-?reply|automatic\s+reply|out\s+of\s+office)")


def _is_bounce(header):
    """A bounce, a delivery failure or an out-of-office auto-reply is not a ticket.

    Left alone, these are the worst kind of inbound mail: a failed delivery
    comes back from MAILER-DAEMON@ (an address no customer owns), so it either
    opens a nonsense ticket or bounces again -- which bounces back, and the two
    mailboxes ping-pong until somebody notices. Auto-replies are the same loop
    with a friendlier face, and replying to an out-of-office notice just
    triggers another one.
    """
    try:
        frm = _addr(str(header.get("From") or "")).lower()
        if "mailer-daemon" in frm or frm.startswith("postmaster@") or "<>" == frm.strip():
            return True
        if BOUNCE_RE.search(_dec(header.get("Subject") or "") or ""):
            return True
        auto = (header.get("Auto-Submitted") or "").strip().lower()
        if auto in ("auto-replied", "auto-notified", "auto-generated"):
            return True
        if header.get("X-Autoreply") or header.get("X-Autorespond"):
            return True
        # RFC 3462 / 8098: a delivery status notification is a machine report
        ct = (header.get("Content-Type") or "").lower()
        if "multipart/report" in ct or "message/delivery-status" in ct:
            return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _handle_msg(conn, mbox, num, cfg):
    typ, d = mbox.fetch(num, "(BODY.PEEK[HEADER])")
    raw = b""
    for part in d:
        if isinstance(part, tuple) and part[0]:
            raw = part[1]
            break
    header = email.message_from_bytes(raw, policy=policy.default) if raw else None
    # Two kinds of mail must never become a ticket. Our own notifications land
    # back in the box we poll (the mailbox is one of the ticket's participants),
    # and feeding them in again makes the app answer its own answer. Bounces and
    # out-of-office replies come from addresses no customer owns and answer
    # themselves, so both turn one ticket into an endless mail loop.
    if header and (_is_own_mail(cfg, header) or _is_bounce(header)):
        try:
            mbox.store(num, "+FLAGS", "\\Seen")
        except Exception:  # noqa: BLE001
            pass
        return {"status": "ignored",
                "reason": "bounce or own notification" if header else "no header"}
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
    # pictures pasted into the body: saved first, so the Markdown conversion can
    # point at them and the ticket shows the image instead of an empty gap
    cid_map, inline_atts = save_inline_images(msg)
    body = extract_body(msg, cid_map)
    # A picture that lived only inside the quoted thread is not part of this
    # message any more, so the file save_inline_images() wrote for it has no
    # owner left: nothing references it in the body, so bind_body_files() will
    # not register it either. Delete it rather than let uploads/ fill up with
    # pictures no ticket will ever show.
    keep = set(re.findall(r"/files/([^)\s\"'>]+)", body))
    keep |= {a.get("stored") for a in inline_atts if a.get("stored")}
    for url in set(cid_map.values()):
        stored = url.rsplit("/", 1)[-1]
        if stored in keep:
            continue
        try:
            os.remove(os.path.join(UPLOAD_DIR, stored))
        except OSError:
            pass

    # attachments: store up to 5MB each
    atts = list(inline_atts)
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            fn = part.get_filename()
            if not fn:
                continue
            disp = (part.get_content_disposition() or "").lower()
            # Requiring Content-Disposition: attachment lost every file sent by
            # clients that omit it (common with Chinese webmails and with mail
            # forwarded from a mobile client). Take anything that carries a name,
            # except an inline image -- those are signature logos and stationery,
            # they would only bury the real files.
            if disp == "inline" and (part.get_content_type() or "").startswith("image/"):
                continue
            fn = _dec(fn)
            try:
                data = part.get_payload(decode=True) or b""
            except Exception:
                continue
            if 0 < len(data) <= 5 * 1024 * 1024:
                atts.append({"filename": fn, "data": data,
                             "content_type": part.get_content_type()})
    # one picture can be both inlined and listed as a file (Outlook does this);
    # it must not appear twice on the attachment bar
    seen = set()
    uniq = []
    for a in atts:
        key = (a.get("filename"), len(a.get("data") or b""))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(a)
    res = process_incoming_email(conn, subject=subject, frm=frm, frm_name=frm_name,
                                 to_list=to_list, body=body, atts=uniq, cfg=cfg)
    # Mark read only once the mail actually landed in a ticket. Doing it first
    # (as it used to) is what silently swallowed mail: a message the code could
    # not place was flagged \Seen anyway, so the next poll skipped it and the
    # sender never found out why nothing happened. A rejected sender is told
    # once, so that one is retired too.
    try:
        if (res or {}).get("status") in ("created", "replied", "rejected"):
            mbox.store(num, "+FLAGS", "\\Seen")
    except Exception:  # noqa: BLE001 - a flag is bookkeeping, never lose the mail for it
        pass
    return res


# ---------- incoming logic (also used by tests / webhook) ----------

# SUPPORT-YYYYMMxxx is the format since the monthly-counter change; the
# older TK-##### must still thread, or every answer to a ticket opened before
# the change would silently open a new one.
REF_RE = re.compile(r"\b(?:SUPPORT-\d{9}|TK-\d{5})\b", re.I)

#: a subject-only match is a guess, so it is fenced: the thread has to be still
#: open and recent. Without the window a mail called "无法登录" would thread onto
#: last year's ticket that happens to share the words.
SUBJECT_MATCH_DAYS = 45

#: reply/forward prefixes -- repeated, because clients stack them ("Re: Re:")
_PREFIX_RE = re.compile(
    r"(?i)^(?:\s*(?:re|fw|fwd|答复|回复|回覆|转发|轉發)\s*\d*\s*[:：]\s*)+")


def clean_subject(s):
    """A subject stripped down to what the thread is actually about.

    Clients rewrite subjects on the way out ("Re: Re:[#CODE] 救救我"), so the
    ticket code, the reply prefixes and the punctuation they leave behind all
    have to go before two subjects can be compared -- or used as a title.
    """
    s = REF_RE.sub(" ", s or "")
    s = re.sub(r"[\[\]()（）【】#]", " ", s)
    s = _PREFIX_RE.sub(" ", s)
    s = re.sub(r"[\s*_·]+", " ", s)
    return s.strip(" -·")


def thread_key(s):
    """The comparison key for subject threading; see :func:`clean_subject`."""
    return clean_subject(s).lower()


def thread_subject(t):
    """The subject a notification goes out with: one code, no Re: pile-up.

    A mail-opened ticket is *named* "[CODE] subject", so using its title as-is
    produced "Re:[#CODE] [CODE] subject" -- and every answer then nested one
    more Re: onto the front of an ever-growing line.
    """
    return "Re:[#%s] %s" % (t["code"], clean_subject(t["title"]) or "(no subject)")


def _owns_ticket(conn, t, *, frm, cust, part, staff_sender, sender_id=None):
    """May this sender answer this ticket?

    The domain rule used to decide it alone, and it was wrong in both
    directions. A ticket opened from the *web* by a partner-domain user carries
    no customer_name -- only mail-opened tickets get one -- so the partner's own
    answer failed the "same partner" test and opened a duplicate. Identity is
    now what it should have been: anybody demonstrably *on* the ticket (its
    creator, a participant, the account that filed it) as well as the domain
    rule. The desk sees every ticket anyway.
    """
    frm = (frm or "").lower()
    if staff_sender:
        return True
    if frm and (t["creator_email"] or "").lower() == frm:
        return True
    if sender_id and t["creator_id"] and t["creator_id"] == sender_id:
        return True
    if frm and conn.execute(
            "SELECT 1 FROM ticket_participants WHERE ticket_id=? AND lower(email)=?",
            (t["id"], frm)).fetchone():
        return True
    if cust:
        return t["customer_id"] == cust["id"]
    if part:
        # a partner's ticket carries no customer id, his name is the link
        return (not t["customer_id"]) and (t["customer_name"] or "") == part["name"]
    return False


def _match_by_subject(conn, subject, *, frm, cust, part, staff_sender, sender_id=None):
    """The newest open ticket this mail answers, judged by its subject.

    The fallback for a client that dropped the code from the subject: rather
    than file a second ticket for a thread that already exists, find it. Newest
    first, so a re-opened subject lands on the latest round, not the oldest.
    """
    key = thread_key(subject)
    if len(key) < 2:                    # "" or "re" threads with everything
        return None
    rows = conn.execute(
        "SELECT * FROM tickets WHERE status<>'closed' "
        "AND COALESCE(updated_at, created_at) >= datetime('now', ?) "
        "ORDER BY COALESCE(updated_at, created_at) DESC LIMIT 100",
        ("-%d days" % SUBJECT_MATCH_DAYS,)).fetchall()
    for t in rows:
        if thread_key(t["title"]) != key:
            continue
        if _owns_ticket(conn, t, frm=frm, cust=cust, part=part,
                        staff_sender=staff_sender, sender_id=sender_id):
            return t
    return None


def _append_reply(conn, t, *, frm, frm_name, body, atts, staff_sender, sender_info,
                  matched_by="code"):
    """Put an inbound mail on an existing ticket and let it drive the status.

    `matched_by` says how the thread was found -- "code" from the subject's
    reference, "subject" when the client dropped it. The second one is a guess,
    so it is reported back instead of hidden.
    """
    import tickets as T
    uid = sender_info.get("id")
    if not uid and not staff_sender:
        # a sender with no row yet (first mail from a known domain) is
        # auto-provisioned so the reply is attributed to somebody
        uid = T.find_or_create_user(conn, frm, frm_name)["id"]
    T.add_message(conn, t["id"], body=body, user_id=uid, author_email=frm,
                  author_name=frm_name, source="email", attachments=atts)
    # the reply drives the status: a desk answer -> 售后已答复 (and the customer
    # is told), a customer answer -> 客户已答复 (and the desk is told).
    conn.execute("UPDATE tickets SET status=? WHERE id=?",
                 ("support_replied" if staff_sender else "customer_replied", t["id"]))
    conn.commit()
    if staff_sender:
        _notify_customer_reply(conn, t, frm)
    else:
        # a customer (or his partner) answered -- the desk has to hear about it
        # in the app, not only in their mailbox
        T.alert_customer_reply(conn, t)
        _notify_reply(conn, t, frm)
    return {"status": "replied", "ticket": t["code"], "matched_by": matched_by,
            "created_user": bool(sender_info.get("created"))}


def process_incoming_email(conn, *, subject, frm, frm_name, to_list, body, atts=None, cfg=None):
    """Core: thread onto an existing ticket, or open one, or reject + reply.

    Threading resolves in two steps: the reference code in the subject first
    (that is the contract), then the subject itself for clients that rewrite or
    drop it. Only when neither finds a ticket does this open a new one.
    """
    import tickets as T
    if not frm:
        return {"status": "ignored"}
    frm = frm.lower()
    cust = T.match_customer(conn, frm)
    # A partner (代理商) writes in on his own domain, which is registered under
    # Settings > Partners and not under any customer -- without this his mail was
    # treated as a stranger's and bounced.
    part = None if cust else T.match_partner(conn, frm)
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
    if (cust or part) and frm and not staff_sender:
        sender_info = T.find_or_create_user(conn, frm, frm_name, send_credentials=True)
    # ---- which ticket is this mail answering? -------------------------------
    # By code first, by subject after. The code alone was not enough: a client
    # that drops it, and a ticket opened from the web (whose notification the
    # same person then answers), both used to spawn a duplicate ticket.
    t = None
    matched_by = "code"
    if m:
        cand = conn.execute("SELECT * FROM tickets WHERE code=?",
                            (m.group(0).upper(),)).fetchone()
        if cand and _owns_ticket(conn, cand, frm=frm, cust=cust, part=part,
                                 staff_sender=staff_sender, sender_id=sender_info["id"]):
            t = cand
    if t is None:
        t = _match_by_subject(conn, subject, frm=frm, cust=cust, part=part,
                              staff_sender=staff_sender, sender_id=sender_info["id"])
        matched_by = "subject"
    if t is not None:
        return _append_reply(conn, t, frm=frm, frm_name=frm_name, body=body, atts=atts,
                             staff_sender=staff_sender, sender_info=sender_info,
                             matched_by=matched_by)
    # Nothing to attach it to. Only a registered domain -- a customer's, a
    # partner's or the desk's own -- may *open* a ticket; anything else is a
    # stranger and gets told so (once). The desk's own mail is filed as an
    # internal ticket rather than bounced: bouncing it lost the mail.
    if not cust and not part and not staff_sender:
        cfg = cfg or mail_cfg(conn)
        reject_reply(cfg, frm, "unauthorized")
        return {"status": "rejected", "reason": "unauthorized domain"}
    return _open_ticket(conn, subject=subject, body=body, frm=frm, frm_name=frm_name,
                        to_list=to_list, atts=atts, cfg=cfg, cust=cust, part=part,
                        sender_info=sender_info,
                        internal=1 if (not cust and not part) else 0)


def _open_ticket(conn, *, subject, body, frm, frm_name, to_list, atts, cfg,
                 cust, part=None, sender_info=None, internal=0):
    """File a new ticket from an inbound mail, then tell everybody its code.

    `cust` is the customer whose domain the sender is on, `part` the partner
    (代理商) when the domain is registered under Settings > Partners instead.
    Both may be None -- then the sender is desk staff and the ticket is internal
    (no customer behind it, nothing for a client to see).
    """
    sender_info = sender_info or {"id": None, "created": False, "mailed": False}
    import tickets as T
    base_url = (cfg or {}).get("base_url", "")
    clean_sub = REF_RE.sub("", subject or "").strip()
    clean_sub = re.sub(r"(?i)^(re|fw|fwd)\s*:\s*", "", clean_sub).strip() or "(email ticket)"
    # the user row was auto-provisioned above when the sender was new; hand
    # the id to create_ticket so it does not have to redo the work
    t = T.create_ticket(
        conn, title=clean_sub[:200], description=body,
        customer_id=(cust["id"] if cust else None),
        source="email", creator_id=sender_info["id"] or None, creator_email=frm,
        participant_emails=to_list + [frm],
        version=(cust["version"] if cust else "") or "", internal=1 if internal else 0,
        attachments=atts, bracketed_title=True)
    if part and not cust:
        # Named on the ticket, but NOT turned into a customer: create_ticket()
        # auto-creates a customer from any unknown customer_name, and a partner
        # is not one -- that would put him in the customer list twice.
        conn.execute("UPDATE tickets SET customer_name=? WHERE id=?", (part["name"], t["id"]))
        conn.commit()
        t = conn.execute("SELECT * FROM tickets WHERE id=?", (t["id"],)).fetchone()
    # Two mails, two audiences. The opener gets a receipt whose subject is the
    # bare code and title and which carries his link back into the portal; the
    # desk gets told separately. It used to be one mail to everybody including
    # every Cc, which meant a customer received the staff wording and the
    # participants got a copy of their own mail back.
    # The code in the subject is also the threading contract: an answer that
    # keeps it is matched back onto this ticket, one that drops it opens a new
    # one -- so the receipt itself has to carry it.
    T.notify_ticket_opened(conn, t)
    T.notify_new_ticket(conn, t)
    T.alert_new_ticket(conn, t)
    _nag_unregistered(conn, t, list(to_list or []) + [frm])
    return {"status": "created", "ticket": t["code"], "created_user": bool(sender_info["created"]),
            "user_mailed": bool(sender_info.get("mailed", False))}


def _nag_unregistered(conn, t, emails):
    """Invite the addresses copied on a ticket that have no account yet.

    Every non-internal address in the mail is recorded on the ticket, so these
    people will be mailed about it -- but with no account they cannot open the
    link or see the thread. They get one invitation, and only when the domain is
    actually registered as a customer or a partner: a stranger's address was
    probably just a typo or a forwarding hop and must not be mailed at all.
    """
    import tickets as T
    seen = set()
    for raw in emails or []:
        e = (raw or "").strip().lower()
        if not e or "@" not in e or e in seen:
            continue
        seen.add(e)
        # the desk is staff, they have accounts (and are not customers anyway)
        if rbac.is_internal_email(conn, e):
            continue
        if conn.execute("SELECT id FROM users WHERE lower(email)=?", (e,)).fetchone():
            continue
        if not (T.match_customer(conn, e) or T.match_partner(conn, e)):
            continue
        try:
            T.registration_nag_email(conn, e, t)
        except Exception:  # noqa: BLE001 - an invitation is a nicety
            pass


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
    send_email(conn, parts, thread_subject(t),
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
