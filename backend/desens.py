"""Desensitization: replace customer domains and names in archived KB text.

Rule from spec:
- Customer domain abc.com -> xxxxx.com (label masked, TLD kept)
- Any customer name keyword in dialogue -> xxxxxx
When desensitize is on, images/attachments are dropped (handled by caller) and only
the dialogue text is kept.
"""
import re


def mask_domain(domain: str) -> str:
    domain = domain.strip().lower()
    if "." in domain:
        label, _, tld = domain.partition(".")
        return "xxxxx." + tld
    return "xxxxxx"


def build_replacements(customer_name: str, domains_csv: str):
    repl = {}
    for d in (domains_csv or "").split(","):
        d = d.strip()
        if d:
            repl[d] = mask_domain(d)
            # also bare label occurrences rarely; keep full domain only
    name = (customer_name or "").strip()
    if name:
        repl[name] = "xxxxxx"
    return repl


def desensitize_text(text: str, repl: dict) -> str:
    if not text:
        return text
    out = text
    # longest keys first to avoid partial overlaps
    for key in sorted(repl.keys(), key=len, reverse=True):
        if not key:
            continue
        out = re.sub(re.escape(key), repl[key], out, flags=re.IGNORECASE)
    return out


def desensitize_ticket(conn, ticket_id: int) -> str:
    """Return markdown dialogue of a ticket with customer data masked."""
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
    return "\n".join(lines)
