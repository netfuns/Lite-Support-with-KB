"""Password hashing, TOTP, session tokens, permission helpers."""
import base64
import hashlib
import hmac
import os
import secrets
import struct
import time

# ---------- passwords ----------

def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 200_000)
    return "pbkdf2$%s$%s" % (base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(pw: str, stored: str) -> bool:
    if not stored:
        return False
    try:
        _, s, d = stored.split("$")
        salt = base64.b64decode(s)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 200_000)
        return hmac.compare_digest(base64.b64decode(d), dk)
    except Exception:
        return False


# ---------- TOTP (RFC 6238) ----------

def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode()


def totp_now(secret_b32: str, step=30, digits=6, t=None) -> str:
    key = base64.b32decode(secret_b32, casefold=True)
    counter = int((t if t is not None else time.time()) // step)
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    off = h[-1] & 0x0F
    code = struct.unpack(">I", h[off:off + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def totp_verify(secret_b32: str, code: str, window=1, step=30) -> bool:
    try:
        code = "".join(ch for ch in str(code) if ch.isdigit())
    except Exception:
        return False
    if len(code) != 6:
        return False
    now = time.time()
    for d in range(-window, window + 1):
        if hmac.compare_digest(totp_now(secret_b32, step=step, t=now + d * step), code):
            return True
    return False


def totp_uri(email: str, secret_b32: str, issuer="RankEZ Support") -> str:
    from urllib.parse import quote
    return "otpauth://totp/%s:%s?secret=%s&issuer=%s" % (quote(issuer), quote(email), secret_b32, quote(issuer))


# ---------- tokens ----------

def new_token() -> str:
    return secrets.token_urlsafe(32)
