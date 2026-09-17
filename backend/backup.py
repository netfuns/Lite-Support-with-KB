"""Local backup and restore, driven from Settings > Backup.

Archive format::

    backup_yymmddhhmmss.tar.gz
      manifest.json      -- what this is, when it was taken, what is inside
      data/app.db        -- online SQLite snapshot (WAL-safe, no downtime)
      data/uploads/...   -- every uploaded file, byte for byte

That set is the *entire* portal: accounts and their password hashes, tickets,
messages, attachments, the knowledge base, roles/groups, mail settings and
branding all live in app.db. Restoring the archive onto a freshly deployed
instance therefore brings the whole platform back -- which is why the restore
path replaces the database file rather than re-importing rows.

The service runs as ``rankez`` and cannot call systemctl, so an in-app restore
swaps the files and then exits with a non-zero code; the unit's
``Restart=on-failure`` / ``RestartSec=5`` brings the portal back on the restored
data. An archive never contains another copy of the software.

The legacy ``deploy/backup.py`` layout (``app-<ts>.db`` + ``uploads``) is still
accepted on restore, so yesterday's backups are not stranded.
"""
import datetime
import fnmatch
import glob
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile

from db import DB_PATH, UPLOAD_DIR, get_setting, set_setting

FREQS = ("daily", "weekly", "monthly")
#: daily / weekly (every Monday) / monthly (the 1st), at the configured time
FREQ_HINT = {"daily": "every day", "weekly": "every Monday", "monthly": "the 1st of the month"}
DEFAULT_DIR = "/opt/rankez-support/backups"
PREFIX = "backup_"
LEGACY_GLOB = "rankez-backup-*.tar.gz"
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024      # a 4GB "backup" is not our backup
CORE_TABLES = ("users", "tickets", "messages", "kb_articles", "settings")

#: set while the files are being swapped, so a request that lands in the window
#: is told what is happening instead of reading a half-replaced database
RESTORING = {"on": False, "since": "", "error": ""}


def _raw(conn, key, default=""):
    return (get_setting(conn, key, "") or "").strip() or default


def cfg(conn):
    """Current backup configuration, defaults filled in."""
    try:
        keep = int(_raw(conn, "backup_keep", "14"))
    except ValueError:
        keep = 14
    freq = _raw(conn, "backup_freq", "daily").lower()
    if freq not in FREQS:
        freq = "daily"
    when = _raw(conn, "backup_time", "02:30")
    if not valid_time(when):
        when = "02:30"
    return {
        "enabled": _raw(conn, "backup_enabled", "1") != "0",
        "dir": os.path.expanduser(_raw(conn, "backup_dir", DEFAULT_DIR)),
        "freq": freq,
        "time": when,
        "keep": max(1, min(keep, 365)),
        "last_run": _raw(conn, "backup_last_run", ""),
        "last_file": _raw(conn, "backup_last_file", ""),
        "last_error": _raw(conn, "backup_last_error", ""),
    }


def valid_time(s):
    try:
        hh, mm = str(s).split(":")
        return 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59
    except Exception:  # noqa: BLE001
        return False


def save_cfg(conn, d):
    if "backup_enabled" in d:
        set_setting(conn, "backup_enabled", "1" if d.get("backup_enabled") else "0")
    if "backup_dir" in d:
        set_setting(conn, "backup_dir", str(d.get("backup_dir") or "").strip())
    if "backup_freq" in d:
        f = str(d.get("backup_freq") or "daily").lower()
        set_setting(conn, "backup_freq", f if f in FREQS else "daily")
    if "backup_time" in d:
        v = str(d.get("backup_time") or "").strip()
        set_setting(conn, "backup_time", v if valid_time(v) else "02:30")
    if "backup_keep" in d:
        try:
            set_setting(conn, "backup_keep", str(max(1, min(int(d.get("backup_keep") or 14), 365))))
        except (TypeError, ValueError):
            pass
    conn.commit()


# ---------------------------------------------------------------- snapshot

def _snapshot_db(dest):
    """Copy the live database consistently (WAL-aware; readers keep working)."""
    src = sqlite3.connect(DB_PATH, timeout=30)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _counts(db_path):
    out = {}
    try:
        c = sqlite3.connect(db_path)
        try:
            for t in ("users", "tickets", "messages", "kb_articles", "customers"):
                try:
                    out[t] = c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                except sqlite3.Error:
                    out[t] = None
        finally:
            c.close()
    except sqlite3.Error:
        pass
    return out


def run_backup(conn, reason="manual"):
    """Take one archive. Returns ``{"ok", "file", "size", ...}``."""
    conf = cfg(conn)
    out_dir = conf["dir"] or DEFAULT_DIR
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        _record(conn, error="backup_dir_unwritable: %s" % exc)
        return {"ok": False, "error": "backup_dir_unwritable", "detail": str(exc)}
    if not os.access(out_dir, os.W_OK):
        _record(conn, error="backup_dir_unwritable: %s" % out_dir)
        return {"ok": False, "error": "backup_dir_unwritable", "detail": out_dir}

    stamp = datetime.datetime.now().strftime("%y%m%d%H%M%S")
    name = "%s%s.tar.gz" % (PREFIX, stamp)
    dest = os.path.join(out_dir, name)
    # staging lives next to the target so the tar never crosses filesystems
    tmpdir = tempfile.mkdtemp(prefix=".rz-bk-", dir=out_dir)
    try:
        stage = os.path.join(tmpdir, "data")
        os.makedirs(stage, exist_ok=True)
        _snapshot_db(os.path.join(stage, "app.db"))
        manifest = {
            "app": "rankez-support",
            "format": 1,
            "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "created_ts": stamp,
            "reason": reason,
            "freq": conf["freq"],
            "counts": _counts(os.path.join(stage, "app.db")),
            "files": sum(len(f) for _, _, f in os.walk(UPLOAD_DIR)),
            "note": "app.db + uploads are the whole portal; see deploy/RESTORE.md",
        }
        mpath = os.path.join(tmpdir, "manifest.json")
        with open(mpath, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
        if os.path.isdir(UPLOAD_DIR):
            shutil.copytree(UPLOAD_DIR, os.path.join(stage, "uploads"), dirs_exist_ok=True)
        with tarfile.open(dest, "w:gz") as tf:
            tf.add(mpath, arcname="manifest.json")
            tf.add(stage, arcname="data")
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(tmpdir, ignore_errors=True)
        _record(conn, error="%s: %s" % (type(exc).__name__, exc))
        return {"ok": False, "error": str(exc)}
    shutil.rmtree(tmpdir, ignore_errors=True)
    pruned = prune(out_dir, conf["keep"])
    _record(conn, file=name, error="")
    return {"ok": True, "file": name, "dir": out_dir, "pruned": pruned,
            "size": os.path.getsize(dest), "counts": manifest["counts"]}


def _record(conn, file=None, error=None):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if file is not None:
        set_setting(conn, "backup_last_file", file)
        set_setting(conn, "backup_last_run", now)
    if error is not None:
        set_setting(conn, "backup_last_error", error[:300])
        set_setting(conn, "backup_last_run", now)
    conn.commit()


def all_archives(out_dir):
    return sorted(glob.glob(os.path.join(out_dir, PREFIX + "*.tar.gz"))
                  + glob.glob(os.path.join(out_dir, LEGACY_GLOB)))


def prune(out_dir, keep):
    """Keep the newest `keep` archives (both naming generations)."""
    files = all_archives(out_dir)
    removed = []
    for p in (files[:-keep] if keep > 0 else list(files)):
        try:
            os.remove(p)
            removed.append(os.path.basename(p))
        except OSError:
            pass
    return removed


def list_archives(conn):
    out_dir = cfg(conn)["dir"] or DEFAULT_DIR
    items = []
    for p in all_archives(out_dir):
        if not os.path.isfile(p):
            continue
        st = os.stat(p)
        base = os.path.basename(p)
        items.append({
            "name": base,
            "size": st.st_size,
            "mtime": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "legacy": not base.startswith(PREFIX),
        })
    items.sort(key=lambda x: x["name"], reverse=True)
    return {"dir": out_dir, "items": items, "dir_exists": os.path.isdir(out_dir),
            "dir_writable": os.path.isdir(out_dir) and os.access(out_dir, os.W_OK),
            "free": disk_free(out_dir if os.path.isdir(out_dir) else "/")}


def archive_path(conn, name):
    """Resolve an archive name to a path, refusing anything outside the dir."""
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    if not (name.startswith(PREFIX) or fnmatch.fnmatch(name, LEGACY_GLOB)):
        return None
    out_dir = os.path.abspath(cfg(conn)["dir"] or DEFAULT_DIR)
    p = os.path.abspath(os.path.join(out_dir, name))
    if os.path.dirname(p) != out_dir or not os.path.isfile(p):
        return None
    return p


def delete_archive(conn, name):
    p = archive_path(conn, name)
    if not p:
        return False
    try:
        os.remove(p)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- schedule

def period_key(freq, when):
    if freq == "weekly":
        y, w, _ = when.isocalendar()
        return "%04d-W%02d" % (y, w)
    if freq == "monthly":
        return when.strftime("%Y-%m")
    return when.strftime("%Y-%m-%d")


def _at(now, hhmm):
    hh, mm = [int(x) for x in hhmm.split(":")]
    return now.replace(hour=hh, minute=mm, second=0, microsecond=0)


def next_run(conn, now=None):
    """The next scheduled moment (weekly = Monday, monthly = the 1st)."""
    conf = cfg(conn)
    if not conf["enabled"]:
        return ""
    now = now or datetime.datetime.now()
    if conf["freq"] == "weekly":
        cand = _at(now + datetime.timedelta(days=(7 - now.weekday()) % 7), conf["time"])
        if cand <= now:
            cand = _at(cand + datetime.timedelta(days=7), conf["time"])
    elif conf["freq"] == "monthly":
        cand = _at(now.replace(day=1), conf["time"])
        if cand <= now:
            nxt = (now.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
            cand = _at(nxt, conf["time"])
    else:
        cand = _at(now, conf["time"])
        if cand <= now:
            cand = _at(now + datetime.timedelta(days=1), conf["time"])
    return cand.strftime("%Y-%m-%d %H:%M")


def maybe_run(conn, now=None):
    """Take the backup when this period's slot has passed and none was taken.

    Called from the 60-second worker loop. The period key of the last run is the
    whole guard, so this needs no cron: restarting the service in the middle of
    the night cannot double up or skip a night, and a machine that was off at
    the scheduled minute catches up on the next tick.
    """
    conf = cfg(conn)
    if not conf["enabled"]:
        return None
    now = now or datetime.datetime.now()
    due = _at(now, conf["time"])
    if now < due:
        return None                      # today's slot has not arrived yet
    last = conf["last_run"]
    if last:
        try:
            last_dt = datetime.datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            last_dt = None
        if last_dt and period_key(conf["freq"], last_dt) == period_key(conf["freq"], now):
            return None                  # already done in this period
    return run_backup(conn, reason="schedule")


# ---------------------------------------------------------------- restore

def _extract_all(tf, dest):
    try:
        tf.extractall(dest, filter="data")   # blocks ../ and absolute members
    except TypeError:                        # Python < 3.12 has no filter=
        tf.extractall(dest)


def inspect_archive(path):
    """Validate an archive without touching the live data."""
    info = {"ok": False, "error": "", "db_member": "", "members": 0,
            "uploads": 0, "counts": {}, "manifest": {}, "size": 0}
    try:
        info["size"] = os.path.getsize(path)
    except OSError as exc:
        info["error"] = str(exc)
        return info
    if info["size"] > MAX_ARCHIVE_BYTES:
        info["error"] = "archive_too_large"
        return info
    try:
        with tarfile.open(path, "r:gz") as tf:
            for m in tf.getmembers():
                if not m.isfile():
                    continue
                info["members"] += 1
                base = os.path.basename(m.name)
                if m.name in ("app.db", "data/app.db") or \
                        (base.startswith("app-") and base.endswith(".db")):
                    info["db_member"] = m.name
                if "/uploads/" in m.name:
                    info["uploads"] += 1
                if base == "manifest.json":
                    try:
                        info["manifest"] = json.loads(tf.extractfile(m).read().decode("utf-8"))
                    except Exception:  # noqa: BLE001
                        info["manifest"] = {}
            if not info["db_member"]:
                info["error"] = "no_database_in_archive"
                return info
            with tempfile.TemporaryDirectory(prefix="rz-inspect-") as tmp:
                _extract_all(tf, tmp)
                db = os.path.join(tmp, info["db_member"])
                if not os.path.isfile(db):
                    db = os.path.join(tmp, os.path.basename(info["db_member"]))
                c = sqlite3.connect(db)
                try:
                    for t in CORE_TABLES:
                        c.execute("SELECT 1 FROM %s LIMIT 1" % t)
                    info["counts"] = _counts(db)
                finally:
                    c.close()
    except Exception as exc:  # noqa: BLE001
        info["error"] = "%s: %s" % (type(exc).__name__, exc)
        return info
    info["ok"] = True
    return info


def swap_in(archive_path):
    """Replace the live database + uploads with the archive's contents.

    The portal keeps serving while this runs; the caller exits right after so
    systemd restarts it on the restored data. The previous state is snapshotted
    first (``app.db.pre-restore-<ts>``) so restoring the wrong archive is not
    the end of the world.
    """
    tmp = tempfile.mkdtemp(prefix="rz-restore-")
    try:
        with tarfile.open(archive_path, "r:gz") as tf:
            _extract_all(tf, tmp)
        cands = [os.path.join(tmp, "data", "app.db"), os.path.join(tmp, "app.db")]
        cands += sorted(glob.glob(os.path.join(tmp, "app-*.db")))
        db = next((p for p in cands if os.path.isfile(p)), "")
        if not db:
            return {"ok": False, "error": "no_database_in_archive"}
        stamp = datetime.datetime.now().strftime("%y%m%d%H%M%S")
        side = DB_PATH + ".pre-restore-" + stamp
        _snapshot_db(side)
        staged = DB_PATH + ".restore-tmp"
        shutil.copyfile(db, staged)
        os.replace(staged, DB_PATH)          # atomic within the same directory
        for extra in (DB_PATH + "-wal", DB_PATH + "-shm"):
            if os.path.exists(extra):
                os.remove(extra)
        for up in (os.path.join(tmp, "data", "uploads"), os.path.join(tmp, "uploads")):
            if os.path.isdir(up):
                shutil.rmtree(UPLOAD_DIR, ignore_errors=True)
                shutil.copytree(up, UPLOAD_DIR)
                break
        # a dump from an older release lacks the newer columns/tables; bring it
        # up to date now so the restart does not depend on startup order
        try:
            import db as _db
            _db.migrate()
        except Exception:  # noqa: BLE001 - init_db()/migrate() runs at startup too
            pass
        return {"ok": True, "kept": os.path.basename(side), "counts": _counts(DB_PATH)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def disk_free(path):
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize
    except Exception:  # noqa: BLE001
        return None


def legacy_timer_present():
    """True when the system-level rankez-backup.timer is still installed.

    The app cannot stop it (that needs root) and leaving it on means two
    backups a night, so the page says so and prints the command.
    """
    return os.path.exists("/etc/systemd/system/rankez-backup.timer") or os.path.exists(
        "/etc/systemd/system/multi-user.target.wants/rankez-backup.timer")
