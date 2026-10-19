# -*- coding: utf-8 -*-
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

# Trên Vercel, chỉ thư mục /tmp mới cho phép ghi file SQLite
DB_PATH = os.environ.get("CATBAO_DB", "/tmp/catbao.db")
SESSION_DAYS = 14
VN_TZ = timezone(timedelta(hours=7))

DEFAULT_MACHINES = [str(i) for i in range(1, 11)]
DEFAULT_SHIFTS = [
    {"code": "1N1", "name": "Ca ngày"},
    {"code": "2D1", "name": "Ca đêm"},
]
STATUS_VI = {
    "draft": "Đang điền",
    "submitted": "Chờ duyệt",
    "approved": "Đã duyệt",
    "rejected": "Trả lại",
}

# Danh sách 24 mã hạng mục QC bắt buộc chọn Đạt / Không đạt / Không có ở Bước 4
QC_REQUIRED_KEYS = (
    "sensor", "cu", "tmpe", "vk", "khxpe",
    "cuonpp", "kc_in", "xepbo", "candoi", "miengbao", "trongngoai",
    "cuonpe", "duonghan", "kc_han_chi", "kc_han_day", "kc_mep",
    "chithua", "duongmay", "gapday", "baogap", "kc_chi_day", "chieusoc", "tem", "hohep",
    "thesp",
)

_DB_INITIALIZED = False


def now():
    """Luôn trả về thời gian chuẩn giờ Việt Nam (UTC+7) định dạng YYYY-MM-DD HH:MM:SS"""
    return datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), 120000).hex()
    return salt, h


def connect():
    global _DB_INITIALIZED
    need_init = not _DB_INITIALIZED or not os.path.exists(DB_PATH)
    db = sqlite3.connect(DB_PATH, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    if need_init:
        init_db(db)
        _DB_INITIALIZED = True
    return db


def init_db(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT UNIQUE NOT NULL,
      full_name TEXT NOT NULL DEFAULT '',
      role TEXT NOT NULL,
      salt TEXT NOT NULL,
      pass_hash TEXT NOT NULL,
      active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS sessions(
      token TEXT PRIMARY KEY,
      user_id INTEGER NOT NULL,
      expires REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS devices(
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      last_machine TEXT DEFAULT '',
      created_at TEXT,
      last_seen TEXT
    );
    CREATE TABLE IF NOT EXISTS reports(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      device_id TEXT NOT NULL,
      machine TEXT NOT NULL,
      report_date TEXT NOT NULL,
      shift TEXT NOT NULL,
      worker_name TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'draft',
      data TEXT NOT NULL DEFAULT '{}',
      manager_note TEXT DEFAULT '',
      approved_by TEXT DEFAULT '',
      approved_at TEXT DEFAULT '',
      submitted_at TEXT DEFAULT '',
      created_at TEXT,
      updated_at TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_rep_date ON reports(report_date, shift, machine);
    CREATE TABLE IF NOT EXISTS settings(
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    );
    """)
    if not db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        salt, h = hash_pw("admin123")
        db.execute(
            "INSERT INTO users(username,full_name,role,salt,pass_hash,created_at) VALUES(?,?,?,?,?,?)",
            ("admin", "Admin tổng", "admin", salt, h, now()),
        )
    if not db.execute("SELECT 1 FROM settings WHERE key='machines'").fetchone():
        db.execute("INSERT INTO settings VALUES('machines',?)", (json.dumps(DEFAULT_MACHINES),))
    if not db.execute("SELECT 1 FROM settings WHERE key='shifts'").fetchone():
        db.execute("INSERT INTO settings VALUES('shifts',?)", (json.dumps(DEFAULT_SHIFTS, ensure_ascii=False),))
    db.commit()


def get_setting(db, key, default):
    r = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not r:
        return default
    try:
        return json.loads(r["value"])
    except Exception:
        return default


class ApiError(Exception):
    def __init__(self, code, msg, extra=None):
        self.code = code
        self.msg = msg
        self.extra = extra or {}


def num(v, integer=False):
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) if integer else v
    s = str(v).strip().replace(" ", "")
    if not s:
        return None
    try:
        if integer:
            cleaned = s.replace(".", "").replace(",", "")
            return int(cleaned) if cleaned.isdigit() else None
        s = s.replace(",", ".")
        f = float(s)
        return int(f) if f == int(f) else f
    except ValueError:
        return None


def prod_total(p):
    if not isinstance(p, dict):
        return 0
    rolls = p.get("rolls") if isinstance(p.get("rolls"), list) else []
    roll_sum = sum((num(r.get("so_cai"), True) or 0) for r in rolls if isinstance(r, dict))
    if roll_sum > 0:
        return roll_sum
    return num(p.get("tong_cai"), True) or 0


def valid_date(s):
    try:
        datetime.strptime(str(s), "%Y-%m-%d")
        return True
    except Exception:
        return False


def clean_data(data, old=None, keep_confirm=True):
    if not isinstance(data, dict) or not isinstance(data.get("products", []), list):
        raise ApiError(400, "Dữ liệu phiếu không hợp lệ")
    data.setdefault("products", [])
    old_c = {}
    if keep_confirm and isinstance(old, dict):
        for p in old.get("products", []):
            if isinstance(p, dict):
                for r in p.get("rolls", []):
                    if isinstance(r, dict) and r.get("id"):
                        old_c[r.get("id")] = (r.get("ql_xn"), r.get("ql_xn_by"))

    for p in data["products"]:
        if not isinstance(p, dict):
            continue
        rolls = p.get("rolls") if isinstance(p.get("rolls"), list) else []
        roll_sum = sum((num(r.get("so_cai"), True) or 0) for r in rolls if isinstance(r, dict))
        if roll_sum > 0:
            p["tong_cai"] = str(roll_sum)
        if keep_confirm:
            for r in rolls:
                if isinstance(r, dict):
                    v = old_c.get(r.get("id"), (False, ""))
                    r["ql_xn"] = bool(v[0])
                    r["ql_xn_by"] = str(v[1] or "")
    return data


def submit_errors(data):
    errs = []
    prods = data.get("products", [])
    if not prods:
        errs.append("Chưa có sản phẩm nào.")
    for i, p in enumerate(prods, 1):
        sp_name = str(p.get("ma_sp", "")).strip()
        pre = f"Sản phẩm {i}{f' ({sp_name})' if sp_name else ''}: "
        if not str(p.get("ma_dh", "")).strip():
            errs.append(pre + "thiếu Mã đơn hàng (bước ①)")
        if not sp_name:
            errs.append(pre + "thiếu Mã sản phẩm (bước ①)")
        if not str(p.get("bat_dau", "")).strip():
            errs.append(pre + "thiếu giờ bắt đầu (bước ①)")
        if not str(p.get("ket_thuc", "")).strip():
            errs.append(pre + "thiếu giờ kết thúc (bước ①)")
        if prod_total(p) <= 0:
            errs.append(pre + "chưa có số cái cắt được (bước ① hoặc ②)")
        if not str(p.get("toc_do_cat", "")).strip():
            errs.append(pre + "thiếu Tốc độ cắt (bước ③)")

        lans = ((p.get("qc") or {}).get("lans")) or []
        if not lans:
            errs.append(pre + "chưa có phiếu kiểm tra chất lượng (bước ④)")
        else:
            for li, lan in enumerate(lans, 1):
                v = (lan.get("v") if isinstance(lan, dict) else None) or {}
                missing_qc = sum(1 for k in QC_REQUIRED_KEYS if not v.get(k))
                has_tg = bool(str(v.get("tg", "")).strip())
                if missing_qc > 0 or not has_tg:
                    msg = f"Phiếu kiểm tra lần {li} chưa xong: còn {missing_qc} mục chưa chọn"
                    if not has_tg:
                        msg += " và thiếu giờ kiểm tra"
                    errs.append(pre + msg + " (bước ④)")
    return errs


def report_row(r, with_data=False):
    d = {
        k: r[k]
        for k in (
            "id", "device_id", "machine", "report_date", "shift", "worker_name",
            "status", "manager_note", "approved_by", "approved_at",
            "submitted_at", "created_at", "updated_at",
        )
    }
    try:
        data = json.loads(r["data"] or "{}")
    except Exception:
        data = {"products": []}
    prods = data.get("products", [])
    d["n_products"] = len(prods)
    d["total"] = sum(prod_total(p) for p in prods)
    d["status_vi"] = STATUS_VI.get(r["status"], r["status"])
    if with_data:
        d["data"] = data
    return d


ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


def need_staff(ident, admin=False):
    if not ident or ident["kind"] != "staff":
        raise ApiError(401, "Cần đăng nhập tài khoản quản lý")
    if admin and ident["user"]["role"] != "admin":
        raise ApiError(403, "Chỉ Admin tổng mới có quyền này")


def need_worker(ident):
    if not ident or ident["kind"] != "worker":
        raise ApiError(403, "Chức năng dành cho công nhân")


# ============================================================================
# API ROUTES
# ============================================================================

@route("GET", r"/api/config")
def r_config(h, db, ident, m, q, body):
    return {
        "machines": get_setting(db, "machines", DEFAULT_MACHINES),
        "shifts": get_setting(db, "shifts", DEFAULT_SHIFTS),
        "server_time": now(),
    }


@route("GET", r"/api/me")
def r_me(h, db, ident, m, q, body):
    if not ident:
        return {"kind": "none"}
    if ident["kind"] == "staff":
        u = ident["user"]
        pending = db.execute("SELECT COUNT(*) c FROM reports WHERE status='submitted'").fetchone()["c"]
        return {
            "kind": "staff",
            "role": u["role"],
            "username": u["username"],
            "name": u["full_name"] or u["username"],
            "pending": pending,
        }
    d = ident["device"]
    return {"kind": "worker", "name": d["name"], "last_machine": d["last_machine"] or ""}


@route("POST", r"/api/device")
def r_device_register(h, db, ident, m, q, body):
    did = str(body.get("id", "")).strip()[:64]
    name = str(body.get("name", "")).strip()[:60]
    if not did or not name:
        raise ApiError(400, "Vui lòng nhập họ tên")
    machine = str(body.get("machine", "")).strip()[:10]
    ts = now()
    if db.execute("SELECT 1 FROM devices WHERE id=?", (did,)).fetchone():
        db.execute("UPDATE devices SET name=?, last_machine=?, last_seen=? WHERE id=?", (name, machine, ts, did))
    else:
        db.execute(
            "INSERT INTO devices(id,name,last_machine,created_at,last_seen) VALUES(?,?,?,?,?)",
            (did, name, machine, ts, ts),
        )
    db.commit()
    return {"ok": True}


@route("PUT", r"/api/device")
def r_device_update(h, db, ident, m, q, body):
    need_worker(ident)
    name = str(body.get("name", "")).strip()[:60]
    if not name:
        raise ApiError(400, "Vui lòng nhập họ tên")
    machine = str(body.get("machine", "")).strip()[:10]
    db.execute(
        "UPDATE devices SET name=?, last_machine=?, last_seen=? WHERE id=?",
        (name, machine, now(), ident["device"]["id"]),
    )
    db.commit()
    return {"ok": True}


@route("POST", r"/api/login")
def r_login(h, db, ident, m, q, body):
    username = str(body.get("username", "")).strip().lower()
    pw = str(body.get("password", ""))
    u = db.execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
    if not u or not secrets.compare_digest(hash_pw(pw, u["salt"])[1], u["pass_hash"]):
        raise ApiError(401, "Sai tên đăng nhập hoặc mật khẩu")
    tok = secrets.token_urlsafe(32)
    now_ts = time.time()
    db.execute("DELETE FROM sessions WHERE expires < ?", (now_ts,))
    db.execute("INSERT INTO sessions VALUES(?,?,?)", (tok, u["id"], now_ts + SESSION_DAYS * 86400))
    db.commit()
    return {"token": tok}


@route("POST", r"/api/logout")
def r_logout(h, db, ident, m, q, body):
    auth = h.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        db.execute("DELETE FROM sessions WHERE token=?", (auth[7:],))
        db.commit()
    return {"ok": True}


@route("POST", r"/api/password")
def r_password(h, db, ident, m, q, body):
    need_staff(ident)
    u = ident["user"]
    if not secrets.compare_digest(hash_pw(str(body.get("old", "")), u["salt"])[1], u["pass_hash"]):
        raise ApiError(400, "Mật khẩu hiện tại không đúng")
    new_pw = str(body.get("new", ""))
    if len(new_pw) < 6:
        raise ApiError(400, "Mật khẩu mới tối thiểu 6 ký tự")
    salt, hh = hash_pw(new_pw)
    db.execute("UPDATE users SET salt=?, pass_hash=? WHERE id=?", (salt, hh, u["id"]))
    db.commit()
    return {"ok": True}


@route("GET", r"/api/reports")
def r_reports(h, db, ident, m, q, body):
    if not ident:
        raise ApiError(401, "Chưa đăng nhập")
    where, args = [], []
    if ident["kind"] == "worker":
        where.append("(r.device_id=? OR r.machine=?)")
        args.extend([ident["device"]["id"], ident["device"]["last_machine"]])
    for key, col in (("machine", "r.machine"), ("shift", "r.shift"), ("status", "r.status")):
        if q.get(key) and q.get(key) != "all":
            where.append(f"{col}=?")
            args.append(q[key])
    if q.get("from"):
        where.append("r.report_date>=?")
        args.append(q["from"])
    if q.get("to"):
        where.append("r.report_date<=?")
        args.append(q["to"])
    sql = (
        "SELECT r.*, d.name AS device_name FROM reports r LEFT JOIN devices d ON d.id=r.device_id"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY r.report_date DESC, r.shift, CAST(r.machine AS INTEGER), r.id DESC LIMIT 500"
    )
    out = []
    for r in db.execute(sql, args).fetchall():
        d = report_row(r)
        d["device_name"] = r["device_name"] or ""
        out.append(d)
    return {"reports": out}


@route("POST", r"/api/reports")
def r_report_create(h, db, ident, m, q, body):
    need_worker(ident)
    machine = str(body.get("machine", "")).strip()[:10]
    d = str(body.get("report_date", "")).strip()
    shift = str(body.get("shift", "")).strip()[:10]
    if not machine or not shift or not valid_date(d):
        raise ApiError(400, "Thông tin ngày/ca/máy không hợp lệ")
    dev = ident["device"]
    # Kiểm tra trùng theo Máy + Ngày + Ca (để P1 và P2 vào chung 1 phiếu, không bị đúp)
    ex = db.execute(
        "SELECT id FROM reports WHERE machine=? AND report_date=? AND shift=?",
        (machine, d, shift),
    ).fetchone()
    ts = now()
    if ex:
        db.execute("UPDATE devices SET last_machine=?, last_seen=? WHERE id=?", (machine, ts, dev["id"]))
        db.commit()
        return {"id": ex["id"], "existing": True}
    cur = db.execute(
        "INSERT INTO reports(device_id,machine,report_date,shift,worker_name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (dev["id"], machine, d, shift, dev["name"], "draft", json.dumps({"products": []}), ts, ts),
    )
    db.execute("UPDATE devices SET last_machine=?, last_seen=? WHERE id=?", (machine, ts, dev["id"]))
    db.commit()
    return {"id": cur.lastrowid, "existing": False}


@route("GET", r"/api/reports/(\d+)")
def r_report_get(h, db, ident, m, q, body):
    if not ident:
        raise ApiError(401, "Chưa đăng nhập")
    r = db.execute("SELECT * FROM reports WHERE id=?", (int(m.group(1)),)).fetchone()
    if not r:
        raise ApiError(404, "Không tìm thấy phiếu")
    return report_row(r, True)


@route("PUT", r"/api/reports/(\d+)")
def r_report_put(h, db, ident, m, q, body):
    if not ident:
        raise ApiError(401, "Chưa đăng nhập")
    r = db.execute("SELECT * FROM reports WHERE id=?", (int(m.group(1)),)).fetchone()
    if not r:
        raise ApiError(404, "Không tìm thấy phiếu")

    is_admin = ident["kind"] == "staff" and ident["user"]["role"] == "admin"
    if ident["kind"] == "staff" and not is_admin:
        raise ApiError(403, "Quản lý ca chỉ có quyền duyệt hoặc trả lại phiếu, không sửa trực tiếp")
    if ident["kind"] == "worker" and r["status"] == "approved":
        raise ApiError(403, "Phiếu đã duyệt, không sửa được")

    machine = str(body.get("machine", r["machine"])).strip()[:10] or r["machine"]
    d = str(body.get("report_date", r["report_date"])).strip() or r["report_date"]
    shift = str(body.get("shift", r["shift"])).strip()[:10] or r["shift"]
    worker = str(body.get("worker_name", r["worker_name"])).strip()[:60] or r["worker_name"]

    if not valid_date(d):
        raise ApiError(400, "Ngày sản xuất không hợp lệ")

    # Chống trùng phiếu khi đổi Ngày / Ca / Máy ở phần Header
    dup = db.execute(
        "SELECT id FROM reports WHERE machine=? AND report_date=? AND shift=? AND id<>?",
        (machine, d, shift, r["id"]),
    ).fetchone()
    if dup:
        raise ApiError(400, f"Đã tồn tại phiếu cho Máy {machine} · Ca {shift} · Ngày {d}")

    old = json.loads(r["data"] or "{}")
    data = clean_data(body.get("data"), old, keep_confirm=not is_admin)
    status, submitted_at = r["status"], r["submitted_at"]
    ts = now()

    if body.get("submit") and not is_admin:
        errs = submit_errors(data)
        if errs:
            raise ApiError(400, "Phiếu chưa đủ thông tin", {"errors": errs})
        status = "submitted"
        submitted_at = ts

    db.execute(
        "UPDATE reports SET machine=?,report_date=?,shift=?,worker_name=?,data=?,status=?,submitted_at=?,updated_at=? WHERE id=?",
        (machine, d, shift, worker, json.dumps(data, ensure_ascii=False), status, submitted_at, ts, r["id"]),
    )
    if ident["kind"] == "worker":
        db.execute("UPDATE devices SET last_machine=?, last_seen=? WHERE id=?", (machine, ts, ident["device"]["id"]))
    db.commit()
    return {
        "ok": True,
        "status": status,
        "status_vi": STATUS_VI.get(status, status),
        "saved_at": ts[11:16],
        "updated_at": ts,
    }


@route("POST", r"/api/reports/(\d+)/approve")
def r_approve(h, db, ident, m, q, body):
    need_staff(ident)
    r = db.execute("SELECT * FROM reports WHERE id=?", (int(m.group(1)),)).fetchone()
    if not r:
        raise ApiError(404, "Không tìm thấy phiếu")
    u = ident["user"]
    ts = now()
    db.execute(
        "UPDATE reports SET status='approved', approved_by=?, approved_at=?, manager_note='', updated_at=? WHERE id=?",
        (u["full_name"] or u["username"], ts, ts, r["id"]),
    )
    db.commit()
    return {"ok": True, "approved_at": ts}


@route("POST", r"/api/reports/(\d+)/return")
def r_return(h, db, ident, m, q, body):
    need_staff(ident)
    r = db.execute("SELECT * FROM reports WHERE id=?", (int(m.group(1)),)).fetchone()
    if not r:
        raise ApiError(404, "Không tìm thấy phiếu")
    note = str(body.get("note", "")).strip()[:500]
    if not note:
        raise ApiError(400, "Vui lòng nhập lý do trả lại")
    db.execute(
        "UPDATE reports SET status='rejected', manager_note=?, approved_by='', approved_at='', updated_at=? WHERE id=?",
        (note, now(), r["id"]),
    )
    db.commit()
    return {"ok": True}


@route("POST", r"/api/reports/(\d+)/confirm")
def r_confirm_roll(h, db, ident, m, q, body):
    need_staff(ident)
    r = db.execute("SELECT * FROM reports WHERE id=?", (int(m.group(1)),)).fetchone()
    if not r:
        raise ApiError(404, "Không tìm thấy phiếu")
    data = json.loads(r["data"] or "{}")
    roll_id = body.get("roll_id")
    val = bool(body.get("value"))
    by_name = (ident["user"]["full_name"] or ident["user"]["username"]) if val else ""
    for p in data.get("products", []):
        for ro in p.get("rolls", []):
            if ro.get("id") == roll_id:
                ro["ql_xn"] = val
                ro["ql_xn_by"] = by_name
    db.execute(
        "UPDATE reports SET data=?, updated_at=? WHERE id=?",
        (json.dumps(data, ensure_ascii=False), now(), r["id"]),
    )
    db.commit()
    return {"ok": True}


@route("DELETE", r"/api/reports/(\d+)")
def r_report_delete(h, db, ident, m, q, body):
    need_staff(ident, admin=True)
    db.execute("DELETE FROM reports WHERE id=?", (int(m.group(1)),))
    db.commit()
    return {"ok": True}


@route("GET", r"/api/users")
def r_users(h, db, ident, m, q, body):
    need_staff(ident, True)
    rows = db.execute("SELECT id,username,full_name,role,active,created_at FROM users ORDER BY role, username").fetchall()
    return {"users": [dict(x) for x in rows]}


@route("POST", r"/api/users")
def r_user_add(h, db, ident, m, q, body):
    need_staff(ident, True)
    un = str(body.get("username", "")).strip().lower()[:40]
    pw = str(body.get("password", ""))
    full_name = str(body.get("full_name", "")).strip()[:60] or un
    role = body.get("role") if body.get("role") in ("manager", "admin") else "manager"
    if not un:
        raise ApiError(400, "Vui lòng nhập tên đăng nhập")
    if len(pw) < 6:
        raise ApiError(400, "Mật khẩu phải từ 6 ký tự trở lên")
    if db.execute("SELECT 1 FROM users WHERE username=?", (un,)).fetchone():
        raise ApiError(400, "Tên đăng nhập đã tồn tại")
    salt, hh = hash_pw(pw)
    db.execute(
        "INSERT INTO users(username,full_name,role,salt,pass_hash,created_at) VALUES(?,?,?,?,?,?)",
        (un, full_name, role, salt, hh, now()),
    )
    db.commit()
    return {"ok": True}


@route("PUT", r"/api/users/(\d+)")
def r_user_edit(h, db, ident, m, q, body):
    need_staff(ident, True)
    uid = int(m.group(1))
    if uid == ident["user"]["id"]:
        if "active" in body and not body["active"]:
            raise ApiError(400, "Không thể tự khóa tài khoản đang đăng nhập")
        if "role" in body and body["role"] != "admin":
            raise ApiError(400, "Không thể tự hạ quyền Admin của chính mình")
    if "full_name" in body:
        db.execute("UPDATE users SET full_name=? WHERE id=?", (str(body["full_name"]).strip()[:60], uid))
    if body.get("role") in ("manager", "admin"):
        db.execute("UPDATE users SET role=? WHERE id=?", (body["role"], uid))
    if "active" in body:
        db.execute("UPDATE users SET active=? WHERE id=?", (1 if body["active"] else 0, uid))
    if body.get("password"):
        pw = str(body["password"])
        if len(pw) < 6:
            raise ApiError(400, "Mật khẩu mới phải từ 6 ký tự trở lên")
        salt, hh = hash_pw(pw)
        db.execute("UPDATE users SET salt=?, pass_hash=? WHERE id=?", (salt, hh, uid))
    db.commit()
    return {"ok": True}


@route("GET", r"/api/devices")
def r_devices(h, db, ident, m, q, body):
    need_staff(ident, True)
    rows = db.execute(
        "SELECT d.*, (SELECT COUNT(*) FROM reports r WHERE r.device_id=d.id) AS n_reports FROM devices d ORDER BY d.name"
    ).fetchall()
    return {"devices": [dict(x) for x in rows]}


@route("PUT", r"/api/devices/([^/]+)")
def r_device_admin_rename(h, db, ident, m, q, body):
    need_staff(ident, True)
    did = m.group(1)
    name = str(body.get("name", "")).strip()[:60]
    if not name:
        raise ApiError(400, "Vui lòng nhập tên công nhân")
    db.execute("UPDATE devices SET name=? WHERE id=?", (name, did))
    db.commit()
    return {"ok": True}


@route("DELETE", r"/api/devices/([^/]+)")
def r_device_admin_delete(h, db, ident, m, q, body):
    need_staff(ident, True)
    did = m.group(1)
    cnt = db.execute("SELECT COUNT(*) c FROM reports WHERE device_id=?", (did,)).fetchone()["c"]
    if cnt > 0:
        raise ApiError(400, "Không thể xóa thiết bị đã có phiếu báo cáo")
    db.execute("DELETE FROM devices WHERE id=?", (did,))
    db.commit()
    return {"ok": True}


@route("PUT", r"/api/settings")
def r_settings(h, db, ident, m, q, body):
    need_staff(ident, True)
    machines = [str(x).strip()[:10] for x in body.get("machines", []) if str(x).strip()]
    shifts = [
        {"code": str(s.get("code", "")).strip()[:10], "name": str(s.get("name", "")).strip()[:30]}
        for s in body.get("shifts", [])
        if isinstance(s, dict) and str(s.get("code", "")).strip()
    ]
    if not machines or len(shifts) < 2:
        raise ApiError(400, "Cần tối thiểu 1 máy và 2 ca làm việc")
    db.execute("REPLACE INTO settings VALUES('machines',?)", (json.dumps(machines, ensure_ascii=False),))
    db.execute("REPLACE INTO settings VALUES('shifts',?)", (json.dumps(shifts, ensure_ascii=False),))
    db.commit()
    return {"ok": True}


# ============================================================================
# HTTP HANDLER
# ============================================================================

class handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Tắt log mặc định gây rác console nếu không cần thiết
        pass

    def _json(self, code, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _identify(self, db):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            row = db.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>? AND u.active=1",
                (auth[7:], time.time()),
            ).fetchone()
            return {"kind": "staff", "user": dict(row)} if row else None
        dev = self.headers.get("X-Device-Id", "")
        if dev:
            row = db.execute("SELECT * FROM devices WHERE id=?", (dev,)).fetchone()
            if row:
                return {"kind": "worker", "device": dict(row)}
        return None

    def _handle(self, method):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        db = connect()
        try:
            body = {}
            if method in ("POST", "PUT"):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n > 0 else b""
                body = json.loads(raw.decode("utf-8")) if raw else {}
            ident = self._identify(db)
            for meth, rx, fn in ROUTES:
                mt = rx.match(u.path)
                if meth == method and mt:
                    return self._json(200, fn(self, db, ident, mt, q, body))
            raise ApiError(404, "Không tìm thấy API")
        except ApiError as e:
            self._json(e.code, {"error": e.msg, **e.extra})
        except Exception as e:
            self._json(500, {"error": str(e)})
        finally:
            db.close()

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")
