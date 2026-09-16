"""SQLite schema + connection helpers."""
import os
import sqlite3

DATA_DIR = os.environ.get("RZ_DATA", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "app.db")

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS permissions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT UNIQUE NOT NULL,
  grp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS roles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  description TEXT DEFAULT '',
  builtin INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS role_permissions (
  role_id INTEGER NOT NULL,
  perm_id INTEGER NOT NULL,
  PRIMARY KEY (role_id, perm_id)
);

CREATE TABLE IF NOT EXISTS user_groups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  customer_id INTEGER,
  builtin INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE NOT NULL,
  display_name TEXT NOT NULL DEFAULT '',
  password_hash TEXT,
  totp_secret TEXT,
  totp_enabled INTEGER DEFAULT 0,
  status TEXT DEFAULT 'active',
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_roles (
  user_id INTEGER NOT NULL,
  role_id INTEGER NOT NULL,
  PRIMARY KEY (user_id, role_id)
);

CREATE TABLE IF NOT EXISTS user_groups_rel (
  user_id INTEGER NOT NULL,
  group_id INTEGER NOT NULL,
  PRIMARY KEY (user_id, group_id)
);

CREATE TABLE IF NOT EXISTS tokens (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  pending INTEGER DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  domains TEXT DEFAULT '',
  version TEXT DEFAULT '',
  service_start TEXT DEFAULT '',
  service_end TEXT DEFAULT '',
  contact_email TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE,
  title TEXT NOT NULL,
  description TEXT DEFAULT '',
  customer_id INTEGER,
  customer_name TEXT DEFAULT '',
  version TEXT DEFAULT '',
  product TEXT DEFAULT '',
  priority TEXT DEFAULT 'medium',
  status TEXT DEFAULT 'new',
  source TEXT DEFAULT 'web',
  creator_id INTEGER,
  creator_email TEXT DEFAULT '',
  owner_id INTEGER,
  internal INTEGER DEFAULT 0,
  archived INTEGER DEFAULT 0,
  kb_article_id INTEGER,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ticket_participants (
  ticket_id INTEGER NOT NULL,
  email TEXT NOT NULL,
  PRIMARY KEY (ticket_id, email)
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id INTEGER NOT NULL,
  user_id INTEGER,
  author_email TEXT DEFAULT '',
  author_name TEXT DEFAULT '',
  body TEXT DEFAULT '',
  internal INTEGER DEFAULT 0,
  source TEXT DEFAULT 'web',
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS attachments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id INTEGER,
  ticket_id INTEGER,
  article_id INTEGER,
  filename TEXT NOT NULL,
  stored_name TEXT NOT NULL,
  content_type TEXT DEFAULT '',
  size INTEGER DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kb_collections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  visibility TEXT DEFAULT 'registered',
  description TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS kb_collections_groups (
  collection_id INTEGER NOT NULL,
  group_id INTEGER NOT NULL,
  PRIMARY KEY (collection_id, group_id)
);

CREATE TABLE IF NOT EXISTS kb_articles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  body TEXT DEFAULT '',
  source TEXT DEFAULT 'manual',
  visibility TEXT DEFAULT 'registered',
  collection_id INTEGER,
  author_id INTEGER,
  ticket_id INTEGER,
  desensitized INTEGER DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kb_article_groups (
  article_id INTEGER NOT NULL,
  group_id INTEGER NOT NULL,
  PRIMARY KEY (article_id, group_id)
);

CREATE INDEX IF NOT EXISTS idx_msg_ticket ON messages(ticket_id);
CREATE INDEX IF NOT EXISTS idx_ticket_status ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_article_coll ON kb_articles(collection_id);
"""


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    migrate()


def _columns(conn, table):
    return {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}


def migrate():
    """Add columns introduced after the first release (idempotent)."""
    conn = get_db()
    tc = _columns(conn, "tickets")
    if "deploy_type" not in tc:
        conn.execute("ALTER TABLE tickets ADD COLUMN deploy_type TEXT DEFAULT ''")
    ac = _columns(conn, "kb_articles")
    if "module" not in ac:
        conn.execute("ALTER TABLE kb_articles ADD COLUMN module TEXT DEFAULT ''")
    tk = _columns(conn, "tokens")
    if "last_seen" not in tk:
        conn.execute("ALTER TABLE tokens ADD COLUMN last_seen TEXT")
        conn.execute("UPDATE tokens SET last_seen=created_at")
    conn.commit()
    conn.close()


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
