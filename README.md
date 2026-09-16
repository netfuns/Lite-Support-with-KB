# RankEZ Support

Integrated after-sales ticket platform + knowledge base (V1 MVP).

## Stack
- Backend: Python FastAPI + SQLite (single process, no external services required)
- Frontend: modern, minimal (in the style of rankez.com). Vanilla ES modules, **zero build step**.
- i18n: English (default), Simplified Chinese, Traditional Chinese.
- Deploy: systemd + uvicorn, listens on port **12345** (auto-frees on conflict).

## Local run
```bash
cd backend
python -m venv .venv && .venv/bin/pip install -r ../requirements.txt
.venv/bin/uvicorn app:app --reload --host 0.0.0.0 --port 8000
# then: http://127.0.0.1:8000   (admin@rankez.local / Admin@12345)
```

## Deploy to 192.168.254.10
From `deploy/run.sh` (runs as root via `sudo -i`):
```bash
tar -czf /tmp/rankez_support.tar.gz -C rankez-support .
scp rankez-support/deploy/run.sh deploy/run.sh    # then:
sudo bash deploy/run.sh root
```
Service: `rankez-support` → `http://192.168.254.10:12345`.

## Accounts (seed)
- Administrator: `admin@rankez.local / Admin@12345` (TOTP optional, 2FA optional).
- A demo customer `Acme Inc` (email domain `abc.com`) is seeded so the domain-gated
  self-registration flow works out of the box.

## Email integration (admin → Mail settings)
- **Receive** via IMAP (incl. O365 `outlook.office365.com` with optional OAuth2 client-credentials),
  polled every 60s by a background thread.
- **Send** via SMTP / O365 SMTP OAuth2.
- Unauthorized sender domains are rejected with an automatic reply.
- Replies with subject `[TK-…]` are threaded onto the matching ticket.

## v1 coverage
- ✅ Unified login portal + RBAC (roles with a granular permission checkbox list per role) + user groups.
- ✅ Domain-gated self-registration; user group per customer (auto-add by email domain).
- ✅ Customers: CSV template + bulk import + manual CRUD.
- ✅ Tickets: web creation (customer autocomplete / auto-create customer, version, product module, priority),
  claim, change owner, filter by time / status / owner / customer / version / priority / module,
  status flow (new / customer replied / support replied / closed), internal ticket toggle, attachments.
- ✅ Email → ticket (domain match) / reply threading / unauthorized rejection; SMTP notification replies.
- ✅ KB visibility scopes (public / registered / internal / user-group), auto-archive on close,
  **desensitization** (customer domains → `xxxxx.com`, names → `xxxxxx`; images/attachments stripped),
  import (MD / Word / Excel / PDF → MD), export to PDF, email share link.
- ✅ Global fuzzy search over accessible tickets + KB articles.
- ✅ TOTP (self-enable + admin reset); self-service display name / password.

### Scope notes / known ceilings (first version)
- CJK PDF export uses fpdf2 with font auto-detection on the server; falls back to ASCII if no CJK font is installed.
- We did **not** add a WYSIWYG editor for Markdown; KB articles use a Markdown textarea.
- Email receive/send requires a configured mailbox in **Admin → Mail settings**; without it the inbound loop is idle.

## Code → GitHub sync
Pushed to `https://github.com/netfuns/rankez-support` (branch `main`) using the provided fine-grained token.
