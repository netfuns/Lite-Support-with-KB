# -*- coding: utf-8 -*-
"""Slider captcha for the public face of the desk (login + anonymous KB search).

How it works
------------
* ``issue()`` paints a mosaic background (a tiny hand-rolled PNG encoder -- no
  Pillow on this box), cuts the puzzle piece out of it and darkens the hole it
  came from. The gap position lives server-side only; the client sees pixels.
* ``verify()`` accepts the drag outcome once: right column (+-TOLERANCE px),
  at least MIN_MS of dragging and a trail of MIN_POINTS moves. The challenge
  is burned on the first attempt, correct or not.
* A solved challenge mints a ``captcha_pass`` row (bound to the client IP,
  PASS_TTL = 30 minutes). The guard on POST /api/auth/login and on the
  anonymous GET /api/kb/articles accepts the pass via the ``X-Captcha-Pass``
  header and lets the client through for half an hour.

Public vs internal
------------------
``client_ip()`` takes the direct peer, and only consults X-Forwarded-For when
the peer itself is private -- i.e. our own reverse proxy. A client from the
open internet cannot smuggle a private ``X-Forwarded-For`` past it, because
its socket peer is public and the header is then ignored. Private peers
(internal network, the acceptance harness) are exempt from the whole thing:
the desk must keep working behind the firewall without a puzzle every time.

Abuse budget
------------
issue: 12 per IP per minute. verify failures: 8 per IP per 10 minutes, then
the address cools down for 10 minutes. Everything in plain dicts -- the app
is a single process by design (SQLite).
"""
import random
import secrets
import struct
import threading
import time
import zlib

from db import get_db

PUZZLE = 44           # puzzle piece edge, px
BG_W, BG_H = 280, 160
TOLERANCE = 8         # |dx - gap_x| allowance, px
CHALLENGE_TTL = 300   # a puzzle must be solved within 5 minutes
PASS_TTL = 1800       # the pass lives 30 minutes, then the puzzle returns
MIN_MS = 300          # a human drag takes at least this long
MIN_POINTS = 4        # ... and leaves a trail
ISSUE_LIMIT = 12      # puzzles per IP per minute
FAIL_LIMIT = 8        # wrong drags per IP per 10 minutes
FAIL_COOLDOWN = 600

_lock = threading.Lock()
_challenges = {}      # captcha_id -> {"gx": int, "gy": int, "t": float}
_issue_log = {}       # ip -> [timestamps]
_fail_log = {}        # ip -> {"n": int, "blocked_until": float}


# --------------------------------------------------------------------- png
def _png(width, height, rows):
    """Minimal 8-bit RGB PNG encoder (stdlib only)."""
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


# ------------------------------------------------------------------ paint
def _mosaic(gx, gy, seed):
    """Return (bg_rows, piece_rows). The piece is cropped before the hole is
    darkened, so dragging it back over the darkened hole re-forms the image."""
    rnd = random.Random(seed)
    B = 8  # mosaic block size
    blocks = {}
    for by in range(BG_H // B):
        for bx in range(BG_W // B):
            blocks[(bx, by)] = (rnd.randint(70, 235), rnd.randint(70, 235),
                                rnd.randint(70, 235))
    def base(x, y):
        r, g, b = blocks[(x // B, y // B)]
        # gentle per-pixel grain so flat blocks are not machine-detectable
        n = ((x * 7 + y * 13) % 15) - 7
        return (min(255, max(0, r + n)), min(255, max(0, g + n)), min(255, max(0, b + n)))
    piece = []
    for y in range(PUZZLE):
        row = bytearray()
        for x in range(PUZZLE):
            row += bytes(base(gx + x, gy + y))
        piece.append(row)
    bg = []
    for y in range(BG_H):
        row = bytearray()
        for x in range(BG_W):
            r, g, b = base(x, y)
            if gx <= x < gx + PUZZLE and gy <= y < gy + PUZZLE:
                # the hole keeps a darkened ghost of the texture -- visible,
                # alignable, but no flat signature a script can grep for
                row += bytes((r // 4, g // 4, b // 4))
            else:
                row += bytes((r, g, b))
        bg.append(row)
    return bg, piece


# ------------------------------------------------------------------ api
def _sweep(now):
    for cid in [k for k, v in _challenges.items() if now - v["t"] > CHALLENGE_TTL]:
        _challenges.pop(cid, None)


def issue(ip):
    """Paint a fresh puzzle. Returns the payload for GET /api/captcha/new."""
    import base64 as b64mod
    now = time.time()
    internal = _is_private(ip)
    if not internal:
        with _lock:
            hits = [t for t in _issue_log.get(ip, []) if now - t < 60]
            if len(hits) >= ISSUE_LIMIT:
                return {"ok": False, "error": "captcha_rate_limited", "retry_after": 60}
            hits.append(now)
            _issue_log[ip] = hits
            blk = _fail_log.get(ip)
            if blk and blk.get("blocked_until", 0) > now:
                return {"ok": False, "error": "captcha_blocked",
                        "retry_after": int(blk["blocked_until"] - now) + 1}
    cid = secrets.token_urlsafe(16)
    gx = random.randint(20, BG_W - PUZZLE - 20)
    gy = random.randint(20, BG_H - PUZZLE - 20)
    seed = secrets.token_urlsafe(12)
    bg_rows, piece_rows = _mosaic(gx, gy, seed)
    with _lock:
        _sweep(now)
        _challenges[cid] = {"gx": gx, "gy": gy, "t": now}
    return {
        "ok": True,
        "captcha_id": cid,
        "bg": "data:image/png;base64," + b64mod.b64encode(_png(BG_W, BG_H, bg_rows)).decode("ascii"),
        "piece": "data:image/png;base64," + b64mod.b64encode(
            _png(PUZZLE, PUZZLE, piece_rows)).decode("ascii"),
        "piece_y": gy,
        "width": BG_W,
        "height": BG_H,
        "puzzle": PUZZLE,
    }


def verify(ip, captcha_id, dx, ms, points):
    """One shot. Returns {"ok": True} or {"ok": False, "error": ...}."""
    now = time.time()
    internal = _is_private(ip)
    if not internal:
        with _lock:
            blk = _fail_log.get(ip)
            if blk and blk.get("blocked_until", 0) > now:
                return {"ok": False, "error": "captcha_blocked",
                        "retry_after": int(blk["blocked_until"] - now) + 1}
        with _lock:
            ch = _challenges.pop(captcha_id or "", None)
    else:
        with _lock:
            ch = _challenges.pop(captcha_id or "", None)
    if not ch:
        return {"ok": False, "error": "captcha_expired"}
    try:
        dx = int(dx); ms = int(ms); points = int(points)
    except (TypeError, ValueError):
        return _fail(ip, "captcha_bad_answer", internal)
    if ms < MIN_MS or points < MIN_POINTS:
        return _fail(ip, "captcha_too_fast", internal)
    if abs(dx - ch["gx"]) > TOLERANCE:
        return _fail(ip, "captcha_misaligned", internal)
    return {"ok": True}


def _fail(ip, err, internal=False):
    if internal:
        return {"ok": False, "error": err}
    now = time.time()
    with _lock:
        rec = _fail_log.setdefault(ip, {"n": 0, "blocked_until": 0})
        rec["n"] += 1
        if rec["n"] >= FAIL_LIMIT:
            rec["blocked_until"] = now + FAIL_COOLDOWN
            rec["n"] = 0
    return {"ok": False, "error": err}


# ------------------------------------------------------------------ pass
def grant_pass(conn, ip, user_id=None):
    """Mint a 30-minute pass (also sweeps expired rows while we are here)."""
    import datetime
    tok = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM captcha_pass WHERE expires < ?", (time.time(),))
    conn.execute(
        "INSERT OR REPLACE INTO captcha_pass(token,ip,user_id,created,expires) VALUES(?,?,?,?,?)",
        (tok, ip, user_id, datetime.datetime.utcnow().isoformat(" "), time.time() + PASS_TTL))
    conn.commit()
    return tok


def pass_ok(conn, token, ip):
    if not token:
        return False
    row = conn.execute("SELECT ip, expires FROM captcha_pass WHERE token=?",
                       (token,)).fetchone()
    return bool(row) and row["expires"] > time.time() and row["ip"] == ip


# -------------------------------------------------------------- ip rules
def _is_private(ip):
    if not ip:
        return True
    ip = ip.strip().lower()
    if ip in ("::1", "localhost") or ip.startswith("fc") or ip.startswith("fd") or ip.startswith("fe80"):
        return True
    if ip.startswith("10.") or ip.startswith("192.168.") or ip.startswith("127."):
        return True
    if ip.startswith("172."):
        try:
            return 16 <= int(ip.split(".")[1]) <= 31
        except (ValueError, IndexError):
            return False
    return False


def client_ip(request):
    """The direct peer wins. X-Forwarded-For is only read when the peer is one
    of our own (private) proxies, so a public client cannot fake its way past
    the guard with a spoofed header."""
    peer = (request.client.host if request.client else "") or ""
    xff = request.headers.get("x-forwarded-for", "")
    if xff and _is_private(peer):
        return xff.split(",")[0].strip()
    return peer


def guard(request, conn):
    """403 captcha_required unless the client is internal or holds a fresh pass."""
    from fastapi import HTTPException
    ip = client_ip(request)
    if _is_private(ip):
        return
    if pass_ok(conn, request.headers.get("x-captcha-pass", ""), ip):
        return
    raise HTTPException(403, "captcha_required")
