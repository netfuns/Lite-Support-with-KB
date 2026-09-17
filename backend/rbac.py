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
#   * a customer-bound group  -> read + create on that customer's tickets
#     (which tickets are visible is scoped to the customer by the queries)
#   * the internal group      -> read + edit on every ticket, NEVER delete
# ---------------------------------------------------------------------------
# Stable machine key kept as "internal" (it is referenced in the database by
# name); the UI shows INTERNAL_GROUP_LABEL instead.
INTERNAL_GROUP = "internal"
INTERNAL_GROUP_LABEL = "内部用户组"

CUSTOMER_GROUP_PERMS = [
    "ticket.create", "ticket.view_own", "ticket.reply", "ticket.change_status",
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
        "customer.view",
    ])
    role("客户", [
        "ticket.create", "ticket.view_own", "ticket.reply", "ticket.change_status",
        "kb.view_public", "kb.view_registered",
    ])
    role("代理商", [
        "ticket.create", "ticket.view_own", "ticket.reply", "ticket.change_status",
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
    """Groups whose membership is owned by the e-mail-domain rule."""
    ids = {r["id"] for r in conn.execute("SELECT id FROM user_groups WHERE customer_id IS NOT NULL")}
    gi = internal_group_id(conn)
    if gi:
        ids.add(gi)
    return ids


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
    """Group ids implied by an e-mail address (customer domains + internal)."""
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
    gi = internal_group_id(conn)
    if gi and domain_matches(domain, _internal_domains(conn)):
        gids.add(gi)
    return gids


def sync_email_groups(conn, uid, email, remove_stale=True, only=None):
    """Re-align domain-driven memberships.

      * always joins the group of every customer whose domain the address matches,
        plus the internal group when the domain is an internal domain
      * with remove_stale, also leaves any managed group that no longer matches
        (so changing to an unrelated address drops the old customer group)
      * `only="internal"` limits the whole pass to the internal group, which is
        what a change of internal domains should touch

    remove_stale is opt-in because "an administrator put this user in the group"
    and "the domain rule put this user in the group" are indistinguishable in the
    table; only an e-mail change (or an internal-domain edit) may evict a member.
    """
    if uid is None:
        return set()
    want = auto_group_ids(conn, email)
    if only == "internal":
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
            "SELECT id,name,customer_id FROM user_groups WHERE id IN (%s)" % marks, list(gids)):
        if r["name"] == INTERNAL_GROUP:
            perms.update(INTERNAL_GROUP_PERMS)
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
    """Create (or return) the built-in user group bound to a customer."""
    g = conn.execute("SELECT id FROM user_groups WHERE customer_id=?", (customer_id,)).fetchone()
    if g:
        return g["id"]
    name = "客户组:" + customer_name
    cur = conn.execute(
        "INSERT INTO user_groups(name,customer_id,builtin) VALUES(?,?,1)", (name, customer_id))
    return cur.lastrowid
