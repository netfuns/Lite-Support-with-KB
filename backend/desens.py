"""Desensitization: mask a ticket's identity before it reaches the KB.

One mask covers every kind of identity data -- customer / partner name,
e-mail address, IP literal, host name and domain -- all become ``xxxxxx``:

    Acme Inc          -> xxxxxx
    alice@abc.com     -> xxxxxx
    abc.com           -> xxxxxx
    192.168.254.10    -> xxxxxx

When desensitization is on the caller also drops every attachment and image
(see tickets.archive_to_kb): only the dialogue text is published.
"""
import re

MASK = "xxxxxx"

# Only these suffixes make a dotted token look like a host name. Without the
# list "app.js" or "v1.2" inside a bug report would be masked too.
_TLDS = (
    "com|cn|net|org|io|ai|co|edu|gov|mil|dev|test|local|internal|intranet|lan|"
    "corp|company|group|xyz|top|info|biz|me|cc|tv|site|online|shop|store|tech|"
    "cloud|app|vip|club|live|link|work|fun|pro|asia|mobi|name|plus|"
    "中国|公司|网络|集团"
)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# Written-out v6 (6+ groups) or the compressed "::" form. A plain 12:30:45 is
# deliberately not matched -- masking every clock time would ruin the article.
IPV6_RE = re.compile(
    r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{1,4}:){5,7}[0-9A-Fa-f]{1,4}(?![0-9A-Fa-f:])"
    r"|(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{1,4}:){1,7}:"
    r"(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4})*)?(?![0-9A-Fa-f:])")
DOMAIN_RE = re.compile(
    r"\b(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+(?:%s)\b" % _TLDS,
    re.IGNORECASE)


# Words that are far too common to mask on their own -- "Test Customer Ltd"
# must not turn every "customer" in the thread into xxxxxx.
_STOPWORDS = {
    "inc", "inc.", "ltd", "ltd.", "co", "co.", "corp", "corp.", "llc", "gmbh",
    "group", "company", "customer", "client", "test", "demo", "tech",
    "technology", "technologies", "solutions", "solution", "system", "systems",
    "service", "services", "software", "network", "networks", "data", "digital",
    "china", "beijing", "shanghai", "shenzhen", "guangzhou", "international",
    "global", "info", "support", "admin", "team", "office", "holdings",
}


def build_replacements(customer_name: str, domains_csv: str):
    """Explicit literals to mask, so a name/domain is caught even when it is
    written in a way the generic patterns would miss."""
    repl = {}
    for d in (domains_csv or "").split(","):
        d = d.strip()
        if d:
            repl[d] = MASK
    name = (customer_name or "").strip()
    if name:
        repl[name] = MASK
        # a two-word company name is normally written shortened as well
        for word in name.split():
            if len(word) >= 4 and word.lower() not in _STOPWORDS:
                repl.setdefault(word, MASK)
    return repl


def desensitize_text(text: str, repl=None) -> str:
    if not text:
        return text
    out = text
    # Whole shapes first: an address must go in one piece, otherwise masking its
    # domain first would leave the local part behind ("alice@xxxxxx").
    out = EMAIL_RE.sub(MASK, out)
    out = IPV4_RE.sub(MASK, out)
    out = IPV6_RE.sub(MASK, out)
    out = DOMAIN_RE.sub(MASK, out)
    # then the explicit literals of this customer, longest key first to avoid
    # partial overlaps; a bare company word is caught here
    for key in sorted((repl or {}).keys(), key=len, reverse=True):
        if key:
            out = re.sub(re.escape(key), MASK, out, flags=re.IGNORECASE)
    return out


def desensitize_ticket(conn, ticket_id: int) -> str:
    """Return the markdown dialogue of a ticket with its identity masked."""
    t = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    if not t:
        return ""
    repl = {}
    if t["customer_id"]:
        c = conn.execute("SELECT * FROM customers WHERE id=?", (t["customer_id"],)).fetchone()
        if c:
            repl = build_replacements(c["name"], c["domains"])
    repl = {k: v for k, v in repl.items() if k}

    lines = []
    lines.append("## %s" % desensitize_text(t["title"] or "", repl))
    meta = []
    if t["version"]:
        meta.append("Version: %s" % desensitize_text(t["version"], repl))
    if t["product"]:
        meta.append("Module: %s" % t["product"])
    if t["customer_name"]:
        meta.append("Customer: %s" % desensitize_text(t["customer_name"], repl))
    if meta:
        lines.append("_%s_" % " · ".join(meta))
    if t["description"]:
        lines.append("")
        lines.append(desensitize_text(t["description"], repl))

    for m in conn.execute(
            "SELECT * FROM messages WHERE ticket_id=? AND internal=0 ORDER BY id", (ticket_id,)):
        who = m["author_name"] or m["author_email"] or "user"
        who = desensitize_text(who, repl)
        body = desensitize_text(m["body"] or "", repl)
        lines.append("")
        lines.append("**%s**  \n%s" % (who, body))
    # attachments are deliberately not referenced: a masked thread never carries
    # a file that could leak the identity we just removed.
    return "\n".join(lines)
