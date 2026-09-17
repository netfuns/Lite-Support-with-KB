"""Markdown helpers shared by the mail pipeline and the mail templates.

Three jobs, all of them small enough to stay dependency-free:

* ``html_to_md``  -- an inbound mail arrives as HTML (webmail, Outlook, a phone
  client); the ticket body is Markdown, so the formatting has to survive the
  trip. Inline pictures are handed in as a ``cid:`` -> ``/files/<stored>`` map so
  a screenshot pasted into the mail ends up *rendered* in the ticket instead of
  vanishing. Unmapped images (tracking pixels, remote stationery) are dropped --
  they would leak the reader's IP back to the sender.
* ``md_to_html``  -- the templates the administrator writes are Markdown (that
  is what the editor offers), and a mail needs either a text or an HTML body.
* ``render_tpl``  -- ``{{placeholder}}`` substitution.

Both directions escape untrusted text: mail from the internet is hostile input.
"""
import html as _html
import re

# --- inbound: HTML mail -> Markdown -----------------------------------------

_DROP_BLOCKS = re.compile(r"(?is)<(script|style|head|title)\b.*?</\1>")
_COMMENT = re.compile(r"(?s)<!--.*?-->")
_MULTI_NL = re.compile(r"\n{3,}")


def _unescape(s):
    return _html.unescape(s or "")


def html_to_md(src, images=None):
    """Convert an HTML mail body to Markdown.

    ``images`` maps a Content-ID (with or without the ``cid:`` prefix) to the
    portal URL of the saved file, e.g. ``{"abc@x": "/files/9f3....png"}``.
    """
    if not src:
        return ""
    img_map = {}
    for k, v in (images or {}).items():
        k = (k or "").strip().strip("<>")
        if k:
            img_map[k.lower()] = v
            img_map["cid:" + k.lower()] = v
    s = _COMMENT.sub(" ", src)
    s = _DROP_BLOCKS.sub(" ", s)

    def _img(m):
        attrs = m.group(1)
        srcv = _attr(attrs, "src") or ""
        alt = _attr(attrs, "alt") or ""
        target = img_map.get(srcv.strip().strip("<>").lower(), "")
        if not target:
            return ""          # remote/unknown picture: never keep it
        return "\n\n![%s](%s)\n\n" % (alt.replace("]", " "), target)

    s = re.sub(r"(?is)<img\b([^>]*)>", _img, s)

    def _link(m):
        href = (_attr(m.group(1), "href") or "").strip()
        text = re.sub(r"(?s)<[^>]+>", "", m.group(2)).strip() or href
        text = _unescape(text)
        if not href or href.lower().startswith("javascript:"):
            return text
        if not text or text == href:
            return "<%s>" % href
        return "[%s](%s)" % (text, href)

    s = re.sub(r"(?is)<a\b([^>]*)>(.*?)</a>", _link, s)
    # emphasis before the generic tag strip so the markers survive
    s = re.sub(r"(?is)<(b|strong)\b[^>]*>(.*?)</\1>", r"**\2**", s)
    s = re.sub(r"(?is)<(i|em)\b[^>]*>(.*?)</\1>", r"*\2*", s)
    s = re.sub(r"(?is)<(code|tt|kbd)\b[^>]*>(.*?)</\1>", r"`\2`", s)
    s = re.sub(r"(?is)<blockquote\b[^>]*>(.*?)</blockquote>",
               lambda m: _prefix(_strip_tags(m.group(1)), "> "), s)
    s = re.sub(r"(?is)<li\b[^>]*>(.*?)</li>", lambda m: "\n- " + _strip_tags(m.group(1)).strip(), s)
    # a list whose items are separated by blank lines is not a list to the
    # Markdown renderer: it becomes one <ul> per item
    s = re.sub(r"(?m)^([ \t]*[-*] .*)\n\s*\n(?=[ \t]*[-*] )", r"\1\n", s)
    for n in range(1, 7):
        s = re.sub(r"(?is)<h%d\b[^>]*>(.*?)</h%d>" % (n, n),
                   lambda m, n=n: "\n\n" + "#" * n + " " + _strip_tags(m.group(1)).strip() + "\n\n", s)
    # table cells keep their column separation -- a pasted log table used to
    # collapse into one long line
    s = re.sub(r"(?is)</t[dh]>\s*<t[dh][^>]*>", " | ", s)
    s = re.sub(r"(?is)</tr>", "\n", s)
    s = re.sub(r"(?is)<br\s*/?>", "\n", s)
    s = re.sub(r"(?is)</(p|div|h[1-6]|table|blockquote|pre)>", "\n\n", s)
    s = re.sub(r"(?is)<(p|div|ul|ol)\b[^>]*>", "\n", s)
    s = _strip_tags(s)
    s = _unescape(s)
    # tidy: strip trailing spaces, collapse runs of blank lines and of spaces
    s = "\n".join(line.rstrip() for line in s.split("\n"))
    s = re.sub(r"[ \t]{2,}", " ", s)
    return _MULTI_NL.sub("\n\n", s).strip()


def _attr(attrs, name):
    m = re.search(r"""(?is)\b%s\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""" % re.escape(name), attrs or "")
    if not m:
        return ""
    v = m.group(1)
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return _unescape(v).strip()


def _strip_tags(s):
    return re.sub(r"(?s)<[^>]+>", "", s or "")


def _prefix(text, prefix):
    lines = [l for l in (text or "").split("\n")]
    return "\n\n" + "\n".join(prefix + l.strip() for l in lines if l.strip()) + "\n\n"


# --- outbound: Markdown -> HTML (mail bodies) --------------------------------

def _md_inline(s):
    out = _html.escape(s, quote=False)
    out = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)",
                 lambda m: '<img src="%s" alt="%s" style="max-width:100%%">' % (m.group(2), m.group(1)), out)
    out = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
                 lambda m: '<a href="%s" target="_blank" rel="noopener">%s</a>' % (m.group(2), m.group(1)), out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", out)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    return out


def md_to_html(md):
    """A small renderer for the template bodies: headings, lists, code, inline."""
    if not md:
        return ""
    out = []
    in_pre = False
    in_list = None
    for line in str(md).split("\n"):
        if line.strip().startswith("```"):
            if in_pre:
                out.append("</code></pre>")
                in_pre = False
            else:
                out.append('<pre style="background:#f6f8fa;padding:8px;overflow:auto"><code>')
                in_pre = True
            continue
        if in_pre:
            out.append(_html.escape(line, quote=False))
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            if in_list:
                out.append("</%s>" % in_list)
                in_list = None
            n = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (n, _md_inline(m.group(2)), n))
            continue
        m = re.match(r"^\s*[-*]\s+(.*)$", line)
        if m:
            if in_list != "ul":
                if in_list:
                    out.append("</%s>" % in_list)
                out.append("<ul>")
                in_list = "ul"
            out.append("<li>%s</li>" % _md_inline(m.group(1)))
            continue
        if in_list:
            out.append("</%s>" % in_list)
            in_list = None
        if not line.strip():
            continue
        out.append("<p>%s</p>" % _md_inline(line))
    if in_pre:
        out.append("</code></pre>")
    if in_list:
        out.append("</%s>" % in_list)
    return "\n".join(out)


def tpl_plain(md):
    """A text/plain rendering of a Markdown body (for the non-HTML mail part)."""
    if not md:
        return ""
    s = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", str(md))
    s = re.sub(r"(?m)^\s*[-*]\s+", "- ", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?!\*)", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", lambda m: "[image] " + m.group(2), s)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", lambda m: "%s <%s>" % (m.group(1), m.group(2)), s)
    s = re.sub(r"(?m)^\s*```.*$", "", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


# --- templates --------------------------------------------------------------

TPL_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

#: what an administrator may put in a template body / subject
TPL_VARS = ("company", "code", "title", "url", "portal", "customer", "email", "password")


def render_tpl(text, values):
    """Substitute ``{{name}}`` with ``values[name]``.

    Unknown placeholders are left alone on purpose: a typo is then visible in
    the mail instead of silently turning into an empty line.
    """
    if not text:
        return ""
    vals = {k: ("" if v is None else str(v)) for k, v in (values or {}).items()}

    def _sub(m):
        k = m.group(1).lower()
        return vals[k] if k in vals else m.group(0)

    return TPL_RE.sub(_sub, str(text))
