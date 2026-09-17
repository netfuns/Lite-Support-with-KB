"""Permission catalogue, roles, groups seeding + access checks."""
import json

# ---------------------------------------------------------------------------
# Group-derived permissions
#
# Memberships are TWO kinds:
#   * manual  - an administrator put the user in the group; never touched here
#   * managed - derived from the user's e-mail domain and re-aligned by
#               sync_email_groups() on every create / register / e-mail change.
#               "managed" == every customer-bound group + the internal group.
#
# Grants:
#   * a customer-bound group  -> read + create + reply on that customer's
#     tickets (which tickets are visible is scoped to the customer by the
#     queries); closing a ticket is open to any member, see app.ticket_close
#   * a partner-bound group   -> READ on every ticket of the customers that were
#     assigned to that partner (nothing else: no create, no edit, no delete)
#   * the internal group      -> read + edit on every ticket, NEVER delete
#     (claim / reassign / edit status / internal notes are internal-only)
# ---------------------------------------------------------------------------
# Stable machine key kept as "internal" (it is referenced in the database by
# name); the UI shows INTERNAL_GROUP_LABEL instead.
INTERNAL_GROUP = "internal"
INTERNAL_GROUP_LABEL = "内部用户组"

# Group naming: "Customer-<customer name>" / "Partner-<partner name>".
# Both a customer and a partner are "registered users" (已注册用户) in the UI.
CUSTOMER_GROUP_PREFIX = "Customer-"
PARTNER_GROUP_PREFIX = "Partner-"

# A customer files tickets, reads its own, replies and closes -- but never picks
# a workflow status: "support_replied" / "customer_replied" are driven by the
# reply itself, and the status editor belongs to the internal desk.
CUSTOMER_GROUP_PERMS = [
    "ticket.create", "ticket.view_own", "ticket.reply",
]
# A partner sees the tickets of its customers, and nothing more -- but it *is*
# allowed to file and answer tickets: an agent opens a ticket on behalf of one
# of the customers it serves, or for its own agency. The ticket form pins the
# choice (see ticket_create), so "may file" never means "may invent a customer".
PARTNER_GROUP_PERMS = [
    "ticket.create", "ticket.view_own", "ticket.view_partner", "ticket.reply",
]
INTERNAL_GROUP_PERMS = [
    "ticket.view_all", "ticket.create", "ticket.edit", "ticket.reply",
    "ticket.change_status", "ticket.change_owner", "ticket.claim", "ticket.export",
]

# key -> (group label). Every UI button/action is a permission so roles can be built by ticking boxes.
PERMISSIONS = [
    # tickets
    ("ticket.create", "Tickets"),
    ("ticket.view_all", "Tickets"),
    ("ticket.view_own", "Tickets"),
    ("ticket.view_partner", "Tickets"),
    ("ticket.edit", "Tickets"),
    ("ticket.delete", "Tickets"),
    ("ticket.claim", "Tickets"),
    ("ticket.change_owner", "Tickets"),
    ("ticket.reply", "Tickets"),
    ("ticket.change_status", "Tickets"),
    ("ticket.export", "Tickets"),
    # knowledge base
    ("kb.view_public", "Knowledge Base"),
    ("kb.view_registered", "Knowledge Base"),
    ("kb.view_internal", "Knowledge Base"),
    ("kb.create", "Knowledge Base"),
    ("kb.edit", "Knowledge Base"),
    ("kb.delete", "Knowledge Base"),
    ("kb.import", "Knowledge Base"),
    ("kb.export_pdf", "Knowledge Base"),
    ("kb.share_email", "Knowledge Base"),
    ("kb.manage_collections", "Knowledge Base"),
    # customers
    ("customer.view", "Customers"),
    ("customer.create", "Customers"),
    ("customer.edit", "Customers"),
    ("customer.delete", "Customers"),
    ("customer.import_csv", "Customers"),
    # partners (resellers)
    ("partner.view", "Partners"),
    ("partner.create", "Partners"),
    ("partner.edit", "Partners"),
    ("partner.delete", "Partners"),
    # admin
    ("user.manage", "Administration"),
    ("user.reset_totp", "Administration"),
    ("role.manage", "Administration"),
    ("group.manage", "Administration"),
    ("settings.mail", "Administration"),
]
PERM_KEYS = [k for k, _ in PERMISSIONS]

# ---------------------------------------------------------------------------
# Role editor matrix: one row per left-hand menu entry, three levels each.
#   none -> no permission at all for that area
#   read -> the "view" permissions of that area
#   edit -> the view permissions PLUS every write permission of that area
# ---------------------------------------------------------------------------
MENU_ITEMS = [
    {"key": "dashboard", "label": "Dashboard", "read": [], "edit": []},
    {"key": "tickets", "label": "Tickets",
     "read": ["ticket.view_all", "ticket.view_own"],
     "edit": ["ticket.create", "ticket.reply", "ticket.change_status", "ticket.edit",
              "ticket.claim", "ticket.change_owner", "ticket.delete", "ticket.export"]},
    {"key": "new_ticket", "label": "New Ticket",
     "read": ["ticket.create"], "edit": ["ticket.create"]},
    {"key": "kb", "label": "Knowledge Base",
     "read": ["kb.view_public", "kb.view_registered", "kb.view_internal"],
     "edit": ["kb.create", "kb.edit", "kb.delete", "kb.import",
              "kb.manage_collections", "kb.export_pdf", "kb.share_email"]},
    {"key": "customers", "label": "Customers",
     "read": ["customer.view"],
     "edit": ["customer.create", "customer.edit", "customer.delete", "customer.import_csv"]},
    {"key": "partners", "label": "Partners",
     "read": ["partner.view"],
     "edit": ["partner.create", "partner.edit", "partner.delete"]},
    {"key": "users", "label": "Users",
     "read": ["user.manage"], "edit": ["user.manage", "user.reset_totp"]},
    {"key": "roles", "label": "Roles", "read": ["role.manage"], "edit": ["role.manage"]},
    {"key": "groups", "label": "Groups", "read": ["group.manage"], "edit": ["group.manage"]},
    {"key": "mail_settings", "label": "Mail Settings",
     "read": ["settings.mail"], "edit": ["settings.mail"]},
    {"key": "site_settings", "label": "Site Settings",
     "read": ["settings.mail"], "edit": ["settings.mail"]},
]


def levels_for(perms):
    """Derive {menu_key: none|read|edit} from a plain set of permission keys."""
    perms = set(perms or [])
    out = {}
    for item in MENU_ITEMS:
        if not item["read"] and not item["edit"]:
            out[item["key"]] = "read"          # dashboard is always reachable
            continue
        if any(p in perms for p in item["edit"]):
            out[item["key"]] = "edit"
        elif any(p in perms for p in item["read"]):
            out[item["key"]] = "read"
        else:
            out[item["key"]] = "none"
    return out


def perms_for_levels(levels):
    """Expand {menu_key: none|read|edit} back into the flat permission list."""
    levels = levels or {}
    out = set()
    for item in MENU_ITEMS:
        lvl = levels.get(item["key"], "none")
        if lvl in ("read", "edit"):
            out.update(item["read"])
        if lvl == "edit":
            out.update(item["edit"])
    return sorted(k for k in out if k in PERM_KEYS)


def seed(conn):
    cur = conn.cursor()
    for key, grp in PERMISSIONS:
        cur.execute("INSERT OR IGNORE INTO permissions(key,grp) VALUES(?,?)", (key, grp))
    conn.commit()

    perms = {r["key"]: r["id"] for r in cur.execute("SELECT id,key FROM permissions")}

    def role(name, keys, builtin=1):
        cur.execute("INSERT OR IGNORE INTO roles(name,builtin) VALUES(?,?)", (name, builtin))
        rid = cur.execute("SELECT id FROM roles WHERE name=?", (name,)).fetchone()["id"]
        cur.execute("DELETE FROM role_permissions WHERE role_id=?", (rid,))
        for k in keys:
            cur.execute("INSERT OR IGNORE INTO role_permissions(role_id,perm_id) VALUES(?,?)", (rid, perms[k]))
        return rid

    ALL = PERM_KEYS
    role("管理员", ALL)
    role("L1售后人员", [
        "ticket.create", "ticket.view_all", "ticket.claim", "ticket.change_owner", "ticket.reply",
        "ticket.change_status", "ticket.export",
        "kb.view_public", "kb.view_registered", "kb.view_internal", "kb.create", "kb.edit",
        "kb.export_pdf", "kb.share_email",
    ])
    role("L2售后人员", [
        "ticket.create", "ticket.view_all", "ticket.claim", "ticket.change_owner", "ticket.reply",
        "ticket.change_status", "ticket.delete", "ticket.export",
        "kb.view_public", "kb.view_registered", "kb.view_internal", "kb.create", "kb.edit", "kb.delete",
        "kb.import", "kb.export_pdf", "kb.share_email",
        "customer.view", "partner.view",
    ])
    role("客户", [
        "ticket.create", "ticket.view_own", "ticket.reply",
        "kb.view_public", "kb.view_registered",
    ])
    role("代理商", [
        # A partner reads the tickets of the customers assigned to it, and may
        # file / answer tickets of its own (the ticket form decides *for whom*).
        # Kept in step with PARTNER_GROUP_PERMS so the role editor and the
        # actual rights never disagree.
        "ticket.create", "ticket.view_own", "ticket.view_partner", "ticket.reply",
        "kb.view_public", "kb.view_registered",
        "kb.export_pdf",
    ])
    conn.commit()


def _internal_domains(conn):
    """Internal e-mail domains, read straight from settings (no circular import)."""
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='internal_domains'").fetchone()
        return [str(x).strip().lower() for x in json.loads(row["value"] or "[]") if str(x).strip()]
    except Exception:
        return []


# Free / public mail providers are never treated as "this installation's own
# domain" -- deriving them would open registration to the whole world.
PUBLIC_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "hotmail.co.uk",
    "live.com", "live.cn", "msn.com", "yahoo.com", "yahoo.co.jp", "yahoo.com.cn",
    "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com", "proton.me",
    "gmx.com", "gmx.net", "mail.ru", "yandex.ru", "yandex.com", "zoho.com",
    "qq.com", "foxmail.com", "163.com", "126.com", "yeah.net", "sina.com",
    "sina.cn", "sohu.com", "aliyun.com", "139.com", "189.cn", "21cn.com",
    "tom.com", "outlook.jp", "naver.com", "daum.net", "hanmail.net",
}

# Settings > Mail: the mailboxes this installation sends / reads as.
SITE_MAILBOX_KEYS = ("smtp_from", "smtp_user", "imap_user", "o365_mailbox")

# Roles that only ever exist for the operator's own staff.
STAFF_ROLE_NAMES = ("管理员", "L1售后人员", "L2售后人员")


def _domain_of(value):
    s = (value or "").strip().lower()
    if "@" not in s:
        return ""
    d = s.split("@", 1)[1].strip().strip(">").strip(")").rstrip(".")
    return d if ("." in d and " " not in d) else ""


def derived_site_domains(conn):
    """Domains that obviously belong to THIS installation.

    Collected from the mailboxes configured under Settings > Mail and from the
    addresses of the staff accounts that already exist (the administrators and
    the members of the internal group). It is what makes a fresh install usable:
    without it, registering `someone@the-site-domain` was rejected with
    `domain_not_allowed` because the registration gate only looked at customer,
    partner and *configured* internal domains -- and nobody had configured them.

    They behave exactly like the domains configured under Settings > Internal
    domains: an address on one of them joins the internal group and counts as
    desk staff. Keeping them out of it was tried and was simply wrong -- the
    operator's own domain is the desk's domain, so somebody@the-site-domain was
    registered as a 客户 and could not see the ticket queue. Public mail
    providers are excluded, so pointing the mailbox at a 163/Gmail address can
    never widen this into "everybody is staff".
    """
    out = []

    def add(d):
        d = (d or "").strip().lower()
        if d and d not in PUBLIC_MAIL_DOMAINS and d not in out:
            out.append(d)

    for k in SITE_MAILBOX_KEYS:
        try:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
        except Exception:  # noqa: BLE001 - settings table shaping differs per build
            row = None
        if row:
            add(_domain_of(row["value"]))
    try:
        rows = conn.execute(
            "SELECT DISTINCT u.email FROM users u "
            "LEFT JOIN user_roles ur ON ur.user_id=u.id "
            "LEFT JOIN roles r ON r.id=ur.role_id "
            "LEFT JOIN user_groups_rel g ON g.user_id=u.id "
            "LEFT JOIN user_groups ig ON ig.id=g.group_id AND ig.name=? "
            "WHERE (r.name IN (?,?,?) OR ig.id IS NOT NULL)",
            (INTERNAL_GROUP,) + STAFF_ROLE_NAMES)
        for r in rows:
            add(_domain_of(r["email"]))
    except Exception:  # noqa: BLE001
        pass
    return out


def effective_internal_domains(conn):
    """Every domain that makes an address desk staff.

    The domains the administrator typed under Settings > Internal domains, plus
    the domains this installation owns (`derived_site_domains`). Group
    membership, the "internal user" test and the ticket-desk buttons all read
    this, so a freshly detected site domain takes effect without anyone having
    to configure it first.
    """
    out = list(_internal_domains(conn))
    for d in derived_site_domains(conn):
        if d not in out:
            out.append(d)
    return out


def ensure_internal_group(conn):
    """Create (or return) the built-in staff group that matches internal domains."""
    g = conn.execute("SELECT id FROM user_groups WHERE name=?", (INTERNAL_GROUP,)).fetchone()
    if g:
        return g["id"]
    cur = conn.execute("INSERT INTO user_groups(name,builtin) VALUES(?,1)", (INTERNAL_GROUP,))
    return cur.lastrowid


def internal_group_id(conn):
    row = conn.execute("SELECT id FROM user_groups WHERE name=?", (INTERNAL_GROUP,)).fetchone()
    if row:
        return row["id"]
    return ensure_internal_group(conn)


def managed_group_ids(conn):
    """Groups whose membership is owned by the e-mail-domain rule.

    Every customer-bound and partner-bound group plus the internal one.
    """
    ids = {r["id"] for r in conn.execute(
        "SELECT id FROM user_groups WHERE customer_id IS NOT NULL OR partner_id IS NOT NULL")}
    gi = internal_group_id(conn)
    if gi:
        ids.add(gi)
    return ids


def login_domain_ok(conn, u):
    """May this account still sign in? Only while its domain is one we know.

    Registration is restricted to customer, partner and internal domains, but
    that check happened once, at creation time. A customer whose contract ended
    and whose domain was then removed used to keep a working account -- the
    address was gone from the lists while the key still turned. Re-checking at
    every sign-in makes removing a domain actually revoke access.

    One exception, and it is deliberate: whoever can manage users is never
    locked out. If the domain lists are emptied by mistake, he is the only one
    who can put them back -- a rule that can brick the administrator is a rule
    that will.
    """
    email = (u["email"] or "").strip().lower()
    if "@" not in email:
        return False
    try:
        perms = user_permissions(conn, u["id"], email)
    except Exception:  # noqa: BLE001
        perms = set()
    if "user.manage" in perms:
        return True
    if is_internal_email(conn, email):
        return True
    return domain_matches(email.split("@", 1)[1], known_domains(conn))


def domain_matches(domain, patterns):
    """True when `domain` is exactly a pattern or one of its sub-domains."""
    d = (domain or "").strip().lower()
    if not d:
        return False
    for p in patterns:
        p = (p or "").strip().lower()
        if p and (d == p or d.endswith("." + p)):
            return True
    return False


def auto_group_ids(conn, email):
    """Group ids implied by an e-mail address (customer + partner domains + internal)."""
    gids = set()
    if not email or "@" not in email:
        return gids
    domain = email.split("@", 1)[1].lower().strip()
    if not domain:
        return gids
    for c in conn.execute("SELECT id,domains FROM customers"):
        ds = [d.strip().lower() for d in (c["domains"] or "").split(",") if d.strip()]
        if domain_matches(domain, ds):
            g = conn.execute("SELECT id FROM user_groups WHERE customer_id=?", (c["id"],)).fetchone()
            if g:
                gids.add(g["id"])
    for p in conn.execute("SELECT id,domains FROM partners"):
        ds = [d.strip().lower() for d in (p["domains"] or "").split(",") if d.strip()]
        if domain_matches(domain, ds):
            g = conn.execute("SELECT id FROM user_groups WHERE partner_id=?", (p["id"],)).fetchone()
            if g:
                gids.add(g["id"])
    gi = internal_group_id(conn)
    if gi and domain_matches(domain, effective_internal_domains(conn)):
        gids.add(gi)
    return gids


def known_domains(conn):
    """Every domain the system knows about: customers + partners + internal.

    Registration (self-service or added by an administrator) is only accepted
    when the address belongs to one of them. The site's own domains (see
    `derived_site_domains`) are part of it too -- otherwise a fresh install
    rejects the operator's own address until somebody configures the internal
    domains first.
    """
    out = []
    for table in ("customers", "partners"):
        for r in conn.execute("SELECT domains FROM %s" % table):
            out += [d.strip().lower() for d in (r["domains"] or "").split(",") if d.strip()]
    out += _internal_domains(conn)
    out += derived_site_domains(conn)
    seen, uniq = set(), []
    for d in out:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


def email_domain_allowed(conn, email):
    """True when the address' domain is one of the known domains."""
    if not email or "@" not in email:
        return False
    return domain_matches(email.split("@", 1)[1], known_domains(conn))


def sync_email_groups(conn, uid, email, remove_stale=True, only=None, scope_ids=None):
    """Re-align domain-driven memberships.

      * always joins the group of every customer / partner whose domain the
        address matches, plus the internal group for an internal domain
      * with remove_stale, also leaves any managed group that no longer matches
        (so changing to an unrelated address drops the old customer group)
      * `only="internal"` limits the whole pass to the internal group, which is
        what a change of internal domains should touch
      * `scope_ids={...}` limits the whole pass to those groups, which is how a
        single customer's / partner's domains can be re-evaluated without ever
        touching an unrelated membership

    remove_stale is opt-in because "an administrator put this user in the group"
    and "the domain rule put this user in the group" are indistinguishable in the
    table; only an e-mail change (or a domain edit) may evict a member.
    """
    if uid is None:
        return set()
    want = auto_group_ids(conn, email)
    if scope_ids is not None:
        managed = set(scope_ids)
    elif only == "internal":
        gi = internal_group_id(conn)
        managed = {gi} if gi else set()
    else:
        managed = managed_group_ids(conn)
    want &= managed
    have = {r["group_id"] for r in
            conn.execute("SELECT group_id FROM user_groups_rel WHERE user_id=?", (uid,))}
    if remove_stale:
        for gid in (have & managed) - want:
            conn.execute("DELETE FROM user_groups_rel WHERE user_id=? AND group_id=?", (uid, gid))
    for gid in want - have:
        conn.execute("INSERT OR IGNORE INTO user_groups_rel(user_id,group_id) VALUES(?,?)", (uid, gid))
    return want


def groups_for_user(conn, uid, email=None):
    """Persisted group memberships.

    Domain-driven membership is materialised by sync_email_groups(), which runs
    on every create / register / e-mail change. Reading only the table keeps this
    query cheap and makes "remove from group" actually take effect.
    """
    if uid is None:
        return set()
    return {r["group_id"] for r in
            conn.execute("SELECT group_id FROM user_groups_rel WHERE user_id=?", (uid,))}


def group_derived_perms(conn, uid):
    """Permissions granted purely by group membership."""
    perms = set()
    if uid is None:
        return perms
    gids = groups_for_user(conn, uid)
    if not gids:
        return perms
    marks = ",".join("?" * len(gids))
    for r in conn.execute(
            "SELECT id,name,customer_id,partner_id FROM user_groups WHERE id IN (%s)" % marks, list(gids)):
        if r["name"] == INTERNAL_GROUP:
            perms.update(INTERNAL_GROUP_PERMS)
        elif r["partner_id"]:
            perms.update(PARTNER_GROUP_PERMS)
        elif r["customer_id"]:
            perms.update(CUSTOMER_GROUP_PERMS)
    return perms


def user_permissions(conn, uid, email=None):
    """Role permissions plus everything granted by group membership."""
    if uid is None:
        return set()
    rows = conn.execute(
        "SELECT DISTINCT p.key FROM role_permissions rp "
        "JOIN permissions p ON p.id=rp.perm_id "
        "JOIN user_roles ur ON ur.role_id=rp.role_id WHERE ur.user_id=?", (uid,))
    perms = set(r["key"] for r in rows)
    perms |= group_derived_perms(conn, uid)
    return perms


def has_perm(conn, uid, key):
    if uid is None:
        return False
    return key in user_permissions(conn, uid)


def is_admin_user(conn, uid):
    """True for a site administrator (the built-in "管理员" role).

    A safety net for the ticket desk: desk rights are driven by internal-group
    membership, and an empty / wrong `internal_domains` list must never lock the
    site owner out of his own desk.
    """
    if uid is None:
        return False
    return has_perm(conn, uid, "role.manage") and has_perm(conn, uid, "user.manage")


def is_internal_user(conn, uid):
    """True when the user counts as desk staff (内部用户) -- and may be assigned.

    Three ways in:
      * membership of the built-in internal group (the materialised form, kept
        in step by sync_email_groups)
      * an address whose domain matches the configured internal domains -- the
        rule itself, so a freshly saved domain takes effect at once instead of
        waiting for the next membership sync
      * an administrator account, so a mis-configured domain list can never lock
        the site owner out of the ticket desk

    The ticket desk buttons (claim / reassign / edit status / internal note) and
    the "answer the customer" status transition hang off this, so it must be
    membership / domain -- not a ticket permission -- that decides.
    """
    if uid is None:
        return False
    gi = internal_group_id(conn)
    if gi and gi in groups_for_user(conn, uid):
        return True
    row = conn.execute("SELECT email FROM users WHERE id=?", (uid,)).fetchone()
    if row and is_internal_email(conn, row["email"]):
        return True
    return is_admin_user(conn, uid)


def internal_users(conn):
    """Every user the desk may hand a ticket to (内部用户), active ones only."""
    out = []
    for r in conn.execute("SELECT id,email,display_name,status FROM users ORDER BY id"):
        if (r["status"] or "active") != "active":
            continue
        if is_internal_user(conn, r["id"]):
            out.append({"id": r["id"], "email": r["email"],
                        "display_name": r["display_name"] or r["email"]})
    out.sort(key=lambda x: (x["display_name"] or "").lower())
    return out


def is_internal_email(conn, email):
    """True when the address belongs to one of the internal domains.

    That includes the domains this installation owns, so the operator's own
    address is staff rather than a customer.
    """
    if not email or "@" not in email:
        return False
    return domain_matches(email.split("@", 1)[1], effective_internal_domains(conn))


def resync_internal(conn, email=None):
    """Re-evaluate the internal membership of every user (or just `email`).

    Domain-driven memberships are materialised when a user is created, so a
    domain that is configured -- or detected -- afterwards used to leave the
    accounts that already existed untouched: they stayed 客户 even though their
    domain had become internal. Saving Settings > Internal domains and the
    start-up both call this, which is what makes the setting retroactive.
    """
    if email:
        rows = conn.execute("SELECT id,email FROM users WHERE lower(email)=?",
                            (email.strip().lower(),)).fetchall()
    else:
        rows = conn.execute("SELECT id,email FROM users").fetchall()
    changed = 0
    for r in rows:
        if not r["email"]:
            continue
        before = internal_group_id(conn) in groups_for_user(conn, r["id"])
        sync_email_groups(conn, r["id"], r["email"], only="internal")
        align_internal_role(conn, r["id"], r["email"])
        after = internal_group_id(conn) in groups_for_user(conn, r["id"])
        if before != after:
            changed += 1
    conn.commit()
    return changed


def align_internal_role(conn, uid, email):
    """Drop the auto-assigned 客户 role from an account that is really staff.

    A user created before its domain turned internal keeps the default 客户
    role, which is then shown in the users list and contradicts the internal
    group the account now belongs to. Only the default is swapped -- a role an
    administrator picked on purpose (管理员, L1/L2, 代理商) is never touched.
    """
    if uid is None or not email:
        return False
    if not is_internal_email(conn, email):
        return False
    rows = conn.execute(
        "SELECT ur.role_id, r.name FROM user_roles ur JOIN roles r ON r.id=ur.role_id "
        "WHERE ur.user_id=?", (uid,)).fetchall()
    names = [r["name"] for r in rows]
    if names and set(names) - {"客户"}:
        return False          # a deliberate role is in place -- hands off
    if not names:
        return False          # nothing to drop
    staff = conn.execute("SELECT id FROM roles WHERE name=?", ("L1售后人员",)).fetchone()
    conn.execute("DELETE FROM user_roles WHERE user_id=? AND role_id IN "
                 "(SELECT id FROM roles WHERE name='客户')", (uid,))
    if staff:
        conn.execute("INSERT OR IGNORE INTO user_roles(user_id,role_id) VALUES(?,?)",
                     (uid, staff["id"]))
    return True


def customer_groups_for_user(conn, uid):
    """The customer rows this user reaches through managed group membership."""
    out = []
    for g in conn.execute(
            "SELECT gr.group_id FROM user_groups_rel gr WHERE gr.user_id=?", (uid,)):
        row = conn.execute("SELECT customer_id FROM user_groups WHERE id=?", (g["group_id"],)).fetchone()
        if row and row["customer_id"]:
            c = conn.execute("SELECT * FROM customers WHERE id=?", (row["customer_id"],)).fetchone()
            if c:
                out.append(c)
    return out


def ensure_customer_group(conn, customer_id, customer_name):
    """Create (or return) the built-in user group bound to a customer.

    The name is "Customer-<customer name>" and is kept in step with the customer
    record, so renaming a customer also renames its group.
    """
    want = CUSTOMER_GROUP_PREFIX + (customer_name or "").strip()
    g = conn.execute("SELECT id,name FROM user_groups WHERE customer_id=?", (customer_id,)).fetchone()
    if g:
        if g["name"] != want:
            _rename_group(conn, g["id"], want)
        return g["id"]
    cur = conn.execute(
        "INSERT INTO user_groups(name,customer_id,builtin) VALUES(?,?,1)", (want, customer_id))
    return cur.lastrowid


def ensure_partner_group(conn, partner_id, partner_name):
    """Create (or return) the built-in user group bound to a partner (代理商)."""
    want = PARTNER_GROUP_PREFIX + (partner_name or "").strip()
    g = conn.execute("SELECT id,name FROM user_groups WHERE partner_id=?", (partner_id,)).fetchone()
    if g:
        if g["name"] != want:
            _rename_group(conn, g["id"], want)
        return g["id"]
    cur = conn.execute(
        "INSERT INTO user_groups(name,partner_id,builtin) VALUES(?,?,1)", (want, partner_id))
    return cur.lastrowid


def _rename_group(conn, gid, new_name):
    """Rename a group, keeping the UNIQUE constraint happy."""
    if conn.execute("SELECT id FROM user_groups WHERE name=? AND id<>?", (new_name, gid)).fetchone():
        return False
    conn.execute("UPDATE user_groups SET name=? WHERE id=?", (new_name, gid))
    return True


def sync_builtin_group_names(conn):
    """One-off, idempotent tidy-up: legacy "客户组:X" -> "Customer-X".

    Driven by the customer rows rather than by string surgery, so it cannot
    invent a group that no longer has a customer behind it.
    """
    changed = 0
    for r in conn.execute("SELECT id,customer_id,name FROM user_groups WHERE customer_id IS NOT NULL"):
        c = conn.execute("SELECT name FROM customers WHERE id=?", (r["customer_id"],)).fetchone()
        if not c:
            continue
        want = CUSTOMER_GROUP_PREFIX + (c["name"] or "").strip()
        if r["name"] != want and _rename_group(conn, r["id"], want):
            changed += 1
    for r in conn.execute("SELECT id,partner_id,name FROM user_groups WHERE partner_id IS NOT NULL"):
        p = conn.execute("SELECT name FROM partners WHERE id=?", (r["partner_id"],)).fetchone()
        if not p:
            continue
        want = PARTNER_GROUP_PREFIX + (p["name"] or "").strip()
        if r["name"] != want and _rename_group(conn, r["id"], want):
            changed += 1
    return changed


def partner_ids_for_user(conn, uid):
    """Partner ids the user is attached to through partner-group membership."""
    if uid is None:
        return set()
    out = set()
    for gid in groups_for_user(conn, uid):
        row = conn.execute("SELECT partner_id FROM user_groups WHERE id=?", (gid,)).fetchone()
        if row and row["partner_id"]:
            out.add(row["partner_id"])
    return out


def partner_customer_ids(conn, uid):
    """Customer ids whose tickets a partner user is allowed to read."""
    pids = partner_ids_for_user(conn, uid)
    if not pids:
        return set()
    marks = ",".join("?" * len(pids))
    return {r["id"] for r in
            conn.execute("SELECT id FROM customers WHERE partner_id IN (%s)" % marks, list(pids))}


# ---------------------------------------------------------------------------
# The partner <-> customer link
#
# The link lives on the *customer* row (customers.partner_id), so "which
# customers does this partner serve" is a question about customers, not about a
# list stored on the partner. Both the partner editor and the customer editor
# go through the helpers below so the two directions can never disagree.
# ---------------------------------------------------------------------------
def clean_partner_selection(conn, raw):
    """Turn a picker selection into real customer ids.

    Accepts ids, numeric strings, or the labels the suggest list shows
    ("Name", "Name <domain>"). Unknown values are dropped rather than guessed
    at, so a stale chip can never silently re-point a customer that was deleted.
    """
    out, seen = [], set()
    for x in (raw or []):
        if isinstance(x, dict):
            x = x.get("id", x.get("name", ""))
        key = str(x).strip()
        if not key:
            continue
        cid = None
        if key.isdigit():
            row = conn.execute("SELECT id FROM customers WHERE id=?", (int(key),)).fetchone()
            cid = row["id"] if row else None
        else:
            nm = key.split("  <", 1)[0].split(" <", 1)[0].strip()
            if nm:
                row = conn.execute("SELECT id FROM customers WHERE lower(name)=?", (nm.lower(),)).fetchone()
                cid = row["id"] if row else None
        if cid and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


def apply_partner_customers(conn, pid, ids):
    """Sync customers.partner_id to what the partner editor currently shows.

    Customers dropped from the list are released; a customer picked here is
    moved over even if another partner had it. Called with an empty list it
    unlinks every customer of that partner -- the caller decides whether the
    picker was actually shown, which is why the "was it sent at all" check
    belongs in the endpoint, not here.
    """
    if ids:
        marks = ",".join("?" * len(ids))
        conn.execute("UPDATE customers SET partner_id=NULL WHERE partner_id=? AND id NOT IN (%s)" % marks,
                     [pid] + list(ids))
        conn.execute("UPDATE customers SET partner_id=? WHERE id IN (%s)" % marks, [pid] + list(ids))
    else:
        conn.execute("UPDATE customers SET partner_id=NULL WHERE partner_id=?", (pid,))
    return len(ids)


# ---------------------------------------------------------------------------
# Knowledge-base audience
# ---------------------------------------------------------------------------
# public = anybody, registered = any signed-in user, internal = the desk,
# usergroup = only the groups the article is bound to.
KB_LEVEL = {"public": 0, "registered": 1, "internal": 2, "usergroup": 3}


def kb_bound_groups(conn, article_id):
    """Group ids an article is bound to (only meaningful when usergroup)."""
    return {r["group_id"] for r in
            conn.execute("SELECT group_id FROM kb_article_groups WHERE article_id=?", (article_id,))}


def _as_uid(u):
    """A user id from either a user row (what the routes pass) or a bare id."""
    if u is None or isinstance(u, int):
        return u
    try:
        return u["id"]
    except (TypeError, IndexError, KeyError):
        return None


def kb_article_readable(conn, u, article, group_ids, perms=None):
    """May this caller read this article?

    ``group_ids`` is the caller's own group set (cached by the route). The desk
    always reads the knowledge base; a "usergroup" article is otherwise limited
    to the groups it was bound to when it was published.
    """
    if not article:
        return False
    vis = article["visibility"] or "registered"
    uid = _as_uid(u)
    if uid is None:
        return vis == "public"
    if perms is None:
        perms = user_permissions(conn, uid)
    if vis == "internal" and "kb.view_internal" not in perms:
        return False
    if vis == "registered" and "kb.view_registered" not in perms:
        return False
    if vis == "usergroup":
        # The desk always reads the knowledge base -- otherwise the person who
        # just shared a solution could lose sight of it. Membership of the
        # internal group (not a KB permission) decides who counts as the desk.
        if is_internal_user(conn, uid):
            return True
        return bool(set(group_ids or ()) & kb_bound_groups(conn, article["id"]))
    return True
