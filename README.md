# Support + KB all in one

Integrated post-sales ticket platform + knowledge base. Self-hosted, single
process, no external services required.

## Stack

- **Backend**: Python `FastAPI` + `Starlette` on `uvicorn` (single ASGI process), `Pydantic` for
  validation. Python standard library only for mail (`imaplib` / `smtplib` / `email`).
- **Database**: one SQLite file in WAL mode.
- **Frontend**: vanilla ES modules, **zero build step** — no framework, no bundler.
  A hand-written `h()` hyperscript helper plus hash routing.
- **i18n**: English (default), Simplified Chinese, Traditional Chinese.
- **Deploy**: systemd + uvicorn, listens on port **12345** (auto-frees on conflict).

## Quick start (local)

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r ../requirements.txt
.venv/bin/uvicorn app:app --reload --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000
```

## Accounts (seed)

- Administrator: **`admin@example.com` / `Admin@12345`** — change this password on first login.
- A demo customer `Acme Inc` (e-mail domain `abc.com`) is seeded so the domain-gated
  self-registration flow works out of the box.

Disable the demo fixture with `RZ_SEED_DEMO=0` if you want a completely empty database.

## Configuration

Configuration lives in the database (`settings` table) and is edited from the admin UI;
only three values come from the environment:

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `12345` | Listen port |
| `RZ_DATA` | `backend/data` | Directory holding `app.db` and `uploads/` |
| `RZ_SEED_DEMO` | `1` | Seed the `Acme Inc` demo customer + a demo KB article |

Everything else — SMTP/IMAP mailbox, O365 OAuth2 credentials, company name, logo, bound
hosts, product modules, internal domains, welcome page, notification templates, session
timeouts — is stored in the database and configured from **Admin → Settings**. A fresh
install ships with an empty mailbox: the inbound poller stays idle until you configure one.

## Deploy

`deploy/run.sh` is an idempotent installer; it runs as root and creates a dedicated
system user, a virtualenv, and a systemd unit:

```bash
tar -czf /tmp/example_support.tar.gz -C . .
sudo bash deploy/run.sh /tmp/example_support.tar.gz
```

Defaults: app directory `/opt/rankez-support`, service `rankez-support`, user `rankez`,
backups under `<app>/backups`. Override them at the top of the script.

## Email integration (Admin → Mail settings)

- **Receive** via IMAP (including `outlook.office365.com` with optional OAuth2
  client-credentials), polled every 60s by a background thread.
- **Send** via SMTP, or O365 SMTP with OAuth2.
- Senders from unregistered domains are rejected with an automatic reply.
- A reply is threaded onto its ticket by the ticket code in the subject
  (`[SUPPORT-…]` / legacy `[TK-…]`), falling back to a normalised
  subject match against the same owner's recent open tickets.
- The quoted thread a mail client appends under a reply is trimmed before the text is stored, so a
  ticket holds the message itself rather than the conversation it answers.

## v1 coverage

- ✅ Unified login portal + RBAC (roles with a granular permission checkbox list per role) + user groups.
- ✅ Domain-gated self-registration; one user group per customer (auto-add by e-mail domain).
- ✅ Customers: CSV template + bulk import + manual CRUD. Partners (resellers) with their own
  visible-ticket scope and two-way ticket creation (on behalf of a bound customer, or internal).
- ✅ Tickets: web creation (customer autocomplete / auto-create customer, version, product module,
  priority), claim, change owner, filter by time / status / owner / customer / version / priority /
  module, status flow (new / customer replied / support replied / closed), internal notes, attachments.
  The thread reads newest-first, and replying opens an editor in a dialog from a button on the
  message being answered — the page itself stays a reading surface.
- ✅ Inbound e-mail → ticket (domain match) / reply threading / unauthorized rejection.
- ✅ KB visibility scopes (public / registered / internal / user-group), auto-archive on close,
  **desensitization** (customer domains → `xxxxx.com`, names → `xxxxxx`; images/attachments stripped),
  import (MD / Word / Excel / PDF → MD), export to PDF, e-mail share link.
- ✅ Global fuzzy search over accessible tickets + KB articles.
- ✅ TOTP (self-enable + admin reset, server-rendered QR code); self-service display name / password.
- ✅ Scheduled SQLite backups with in-app restore.

### Scope notes / known ceilings (first version)

- CJK PDF export uses fpdf2 with font auto-detection on the server; falls back to ASCII if no CJK
  font is installed.
- There is no WYSIWYG editor for Markdown — KB articles use a Markdown textarea with a format bar
  (on a ticket, the reply editor lives in a dialog rather than at the foot of the thread).
- Inbound mail is decoded as UTF-8 first, then by declared charset, then by a short candidate list,
  with a lossless `latin-1` last resort: a body is never decoded with `errors="replace"`, because
  `U+FFFD` in the database is unrecoverable.

## License

LGPL-3.0 — see `LICENSE`.
