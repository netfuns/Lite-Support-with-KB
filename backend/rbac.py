"""Permission catalogue, roles, groups seeding + access checks."""

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


def groups_for_user(conn, uid, email):
    """User's group ids: explicit group memberships + customer-domain group (auto by email domain)."""
    gids = set()
    for r in conn.execute("SELECT group_id FROM user_groups_rel WHERE user_id=?", (uid,)):
        gids.add(r["group_id"])
    if email and "@" in email:
        domain = email.split("@", 1)[1].lower()
        for c in conn.execute("SELECT id,domains FROM customers"):
            ds = [d.strip().lower() for d in (c["domains"] or "").split(",") if d.strip()]
            if domain in ds or any(domain.endswith("." + d) for d in ds):
                g = conn.execute("SELECT id FROM user_groups WHERE customer_id=?", (c["id"],)).fetchone()
                if g:
                    gids.add(g["id"])
    return gids


def user_permissions(conn, uid):
    rows = conn.execute(
        "SELECT DISTINCT p.key FROM role_permissions rp "
        "JOIN permissions p ON p.id=rp.perm_id "
        "JOIN user_roles ur ON ur.role_id=rp.role_id WHERE ur.user_id=?", (uid,))
    return set(r["key"] for r in rows)


def has_perm(conn, uid, key):
    if uid is None:
        return False
    return key in user_permissions(conn, uid)


def ensure_customer_group(conn, customer_id, customer_name):
    """Create (or return) the built-in user group bound to a customer."""
    g = conn.execute("SELECT id FROM user_groups WHERE customer_id=?", (customer_id,)).fetchone()
    if g:
        return g["id"]
    name = "客户:" + customer_name
    cur = conn.execute(
        "INSERT INTO user_groups(name,customer_id,builtin) VALUES(?,?,1)", (name, customer_id))
    return cur.lastrowid
