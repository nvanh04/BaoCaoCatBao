# -*- coding: utf-8 -*-
import hashlib, json, os, re, secrets, sqlite3, time
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

# Trên Vercel, chỉ thư mục /tmp mới cho phép ghi file SQLite
DB_PATH = os.environ.get("CATBAO_DB", "/tmp/catbao.db")
SESSION_DAYS = 14
DEFAULT_MACHINES = [str(i) for i in range(1, 11)]
DEFAULT_SHIFTS = [{"code": "1N1", "name": "Ca ngày"}, {"code": "2D1", "name": "Ca đêm"}]
STATUS_VI = {"draft": "Đang điền", "submitted": "Chờ duyệt", "approved": "Đã duyệt", "rejected": "Trả lại"}


def connect():
    db = sqlite3.connect(DB_PATH, timeout=20)
    db.row_factory = sqlite3.Row
    return db


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), 120000).hex()
    return salt, h


def init_db():
    db = connect()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT UNIQUE NOT NULL, full_name TEXT NOT NULL DEFAULT '',
      role TEXT NOT NULL, salt TEXT NOT NULL, pass_hash TEXT NOT NULL,
      active INTEGER NOT NULL DEFAULT 1, created_at TEXT);
    CREATE TABLE IF NOT EXISTS sessions(
      token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS devices(
      id TEXT PRIMARY KEY, name TEXT NOT NULL, last_machine TEXT DEFAULT '',
      created_at TEXT, last_seen TEXT);
    CREATE TABLE IF NOT EXISTS reports(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      device_id TEXT NOT NULL, machine TEXT NOT NULL, report_date TEXT NOT NULL,
      shift TEXT NOT NULL, worker_name TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'draft', data TEXT NOT NULL DEFAULT '{}',
      manager_note TEXT DEFAULT '', approved_by TEXT DEFAULT '', approved_at TEXT DEFAULT '',
      submitted_at TEXT DEFAULT '', created_at TEXT, updated_at TEXT);
    CREATE INDEX IF NOT EXISTS ix_rep_date ON reports(report_date, shift, machine);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    if not db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        salt, h = hash_pw("admin123")
        db.execute("INSERT INTO users(username,full_name,role,salt,pass_hash,created_at) VALUES(?,?,?,?,?,?)",
                   ("admin", "Admin tổng", "admin", salt, h, now()))
    if not db.execute("SELECT 1 FROM settings WHERE key='machines'").fetchone():
        db.execute("INSERT INTO settings VALUES('machines',?)", (json.dumps(DEFAULT_MACHINES),))
    if not db.execute("SELECT 1 FROM settings WHERE key='shifts'").fetchone():
        db.execute("INSERT INTO settings VALUES('shifts',?)", (json.dumps(DEFAULT_SHIFTS, ensure_ascii=False),))
    db.commit()
    db.close()


def get_setting(db, key, default):
    r = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


class ApiError(Exception):
    def __init__(self, code, msg, extra=None):
        self.code, self.msg, self.extra = code, msg, extra or {}


def num(v, integer=False):
    if v is None or v == "": return None
    if isinstance(v, (int, float)): return v
    s = str(v).strip().replace(" ", "")
    if not s: return None
    try:
        if integer:
            return int(s.replace(".", "").replace(",", ""))
        s = s.replace(",", ".")
        f = float(s)
        return int(f) if f == int(f) else f
    except ValueError:
        return None


def prod_total(p):
    t = num(p.get("tong_cai"), True)
    if t is None:
        t = sum((num(r.get("so_cai"), True) or 0) for r in p.get("rolls", []))
    return t


def valid_date(s):
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except Exception:
        return False


def clean_data(data, old=None, keep_confirm=True):
    if not isinstance(data, dict) or not isinstance(data.get("products", []), list):
        raise ApiError(400, "Dữ liệu phiếu không hợp lệ")
    data.setdefault("products", [])
    if keep_confirm:
        old_c = {}
        for p in (old or {}).get("products", []):
            for r in p.get("rolls", []):
                old_c[r.get("id")] = (r.get("ql_xn"), r.get("ql_xn_by"))
        for p in data["products"]:
            for r in p.get("rolls", []):
                v = old_c.get(r.get("id"), (False, ""))
                r["ql_xn"], r["ql_xn_by"] = bool(v[0]), v[1] or ""
    return data


def submit_errors(data):
    errs = []
    prods = data.get("products", [])
    if not prods:
        errs.append("Chưa có sản phẩm nào.")
    for i, p in enumerate(prods, 1):
        pre = f"Sản phẩm {i}: "
        if not str(p.get("ma_dh", "")).strip(): errs.append(pre + "thiếu Mã đơn hàng")
        if not str(p.get("ma_sp", "")).strip(): errs.append(pre + "thiếu Mã sản phẩm")
        if not str(p.get("bat_dau", "")).strip(): errs.append(pre + "thiếu giờ bắt đầu")
        if not str(p.get("ket_thuc", "")).strip(): errs.append(pre + "thiếu giờ kết thúc")
        if not prod_total(p): errs.append(pre + "chưa có số cái cắt được")
        if not (p.get("qc") or {}).get("lans"): errs.append(pre + "chưa có phiếu kiểm tra chất lượng")
    return errs


def report_row(r, with_data=False):
    d = {k: r[k] for k in ("id", "device_id", "machine", "report_date", "shift", "worker_name", "status",
                           "manager_note", "approved_by", "approved_at", "submitted_at", "created_at", "updated_at")}
    data = json.loads(r["data"] or "{}")
    prods = data.get("products", [])
    d["n_products"] = len(prods)
    d["total"] = sum(prod_total(p) or 0 for p in prods)
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


@route("GET", r"/api/config")
def r_config(h, db, ident, m, q, body):
    return {"machines": get_setting(db, "machines", DEFAULT_MACHINES),
            "shifts": get_setting(db, "shifts", DEFAULT_SHIFTS), "server_time": now()}


@route("GET", r"/api/me")
def r_me(h, db, ident, m, q, body):
    if not ident: return {"kind": "none"}
    if ident["kind"] == "staff":
        u = ident["user"]
        pending = db.execute("SELECT COUNT(*) c FROM reports WHERE status='submitted'").fetchone()["c"]
        return {"kind": "staff", "role": u["role"], "username": u["username"], "name": u["full_name"] or u["username"], "pending": pending}
    d = ident["device"]
    return {"kind": "worker", "name": d["name"], "last_machine": d["last_machine"] or ""}


@route("POST", r"/api/device")
def r_device_register(h, db, ident, m, q, body):
    did = str(body.get("id", ""))
    name = str(body.get("name", "")).strip()[:60]
    if not name: raise ApiError(400, "Vui lòng nhập họ tên")
    machine = str(body.get("machine", "")).strip()[:10]
    if db.execute("SELECT 1 FROM devices WHERE id=?", (did,)).fetchone():
        db.execute("UPDATE devices SET name=?, last_machine=?, last_seen=? WHERE id=?", (name, machine, now(), did))
    else:
        db.execute("INSERT INTO devices(id,name,last_machine,created_at,last_seen) VALUES(?,?,?,?,?)", (did, name, machine, now(), now()))
    db.commit()
    return {"ok": True}


@route("PUT", r"/api/device")
def r_device_update(h, db, ident, m, q, body):
    need_worker(ident)
    name = str(body.get("name", "")).strip()[:60]
    if not name: raise ApiError(400, "Vui lòng nhập họ tên")
    db.execute("UPDATE devices SET name=?, last_machine=? WHERE id=?",
               (name, str(body.get("machine", "")).strip()[:10], ident["device"]["id"]))
    db.commit()
    return {"ok": True}


@route("POST", r"/api/login")
def r_login(h, db, ident, m, q, body):
    u = db.execute("SELECT * FROM users WHERE username=? AND active=1", (str(body.get("username", "")).strip().lower(),)).fetchone()
    pw = str(body.get("password", ""))
    if not u or not secrets.compare_digest(hash_pw(pw, u["salt"])[1], u["pass_hash"]):
        raise ApiError(401, "Sai tên đăng nhập hoặc mật khẩu")
    tok = secrets.token_urlsafe(32)
    db.execute("INSERT INTO sessions VALUES(?,?,?)", (tok, u["id"], time.time() + SESSION_DAYS * 86400))
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
    new = str(body.get("new", ""))
    if len(new) < 6: raise ApiError(400, "Mật khẩu mới tối thiểu 6 ký tự")
    salt, hh = hash_pw(new)
    db.execute("UPDATE users SET salt=?, pass_hash=? WHERE id=?", (salt, hh, u["id"]))
    db.commit()
    return {"ok": True}


@route("GET", r"/api/reports")
def r_reports(h, db, ident, m, q, body):
    if not ident: raise ApiError(401, "Chưa đăng nhập")
    where, args = [], []
    if ident["kind"] == "worker":
        # Cho phép công nhân thấy phiếu của thiết bị mình HOẶC phiếu của máy mình đang phụ trách
        where.append("(r.device_id=? OR r.machine=?)")
        args.extend([ident["device"]["id"], ident["device"]["last_machine"]])
    for key, col in (("machine", "r.machine"), ("shift", "r.shift"), ("status", "r.status")):
        if q.get(key) and q.get(key) != "all":
            where.append(f"{col}=?"); args.append(q[key])
    if q.get("from"): where.append("r.report_date>=?"); args.append(q["from"])
    if q.get("to"): where.append("r.report_date<=?"); args.append(q["to"])
    sql = ("SELECT r.*, d.name AS device_name FROM reports r LEFT JOIN devices d ON d.id=r.device_id"
           + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY r.report_date DESC, r.shift, CAST(r.machine AS INTEGER), r.id DESC LIMIT 500")
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
    d, shift = str(body.get("report_date", "")), str(body.get("shift", ""))
    if not machine or not valid_date(d): raise ApiError(400, "Thông tin ngày/máy không hợp lệ")
    dev = ident["device"]
    # ĐÃ SỬA: Kiểm tra trùng theo Máy + Ngày + Ca (để P1 và P2 vào chung 1 phiếu, không bị đúp)
    ex = db.execute("SELECT id FROM reports WHERE machine=? AND report_date=? AND shift=?", (machine, d, shift)).fetchone()
    if ex:
        return {"id": ex["id"], "existing": True}
    cur = db.execute("INSERT INTO reports(device_id,machine,report_date,shift,worker_name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (dev["id"], machine, d, shift, dev["name"], "draft", json.dumps({"products": []}), now(), now()))
    db.execute("UPDATE devices SET last_machine=?, last_seen=? WHERE id=?", (machine, now(), dev["id"]))
    db.commit()
    return {"id": cur.lastrowid, "existing": False}


@route("GET", r"/api/reports/(\d+)")
def r_report_get(h, db, ident, m, q, body):
    r = db.execute("SELECT * FROM reports WHERE id=?", (int(m.group(1)),)).fetchone()
    if not r: raise ApiError(404, "Không tìm thấy phiếu")
    return report_row(r, True)


@route("PUT", r"/api/reports/(\d+)")
def r_report_put(h, db, ident, m, q, body):
    r = db.execute("SELECT * FROM reports WHERE id=?", (int(m.group(1)),)).fetchone()
    if not r: raise ApiError(404, "Không tìm thấy phiếu")
    is_admin = (ident and ident["kind"] == "staff" and ident["user"]["role"] == "admin")
    if ident["kind"] == "worker" and r["status"] == "approved":
        raise ApiError(403, "Phiếu đã duyệt, không sửa được")
    old = json.loads(r["data"] or "{}")
    data = clean_data(body.get("data"), old, keep_confirm=not is_admin)
    machine = str(body.get("machine", r["machine"])).strip()[:10] or r["machine"]
    d = str(body.get("report_date", r["report_date"]))
    shift = str(body.get("shift", r["shift"]))
    worker = str(body.get("worker_name", r["worker_name"])).strip()[:60] or r["worker_name"]
    status, submitted_at = r["status"], r["submitted_at"]
    if body.get("submit") and not is_admin:
        errs = submit_errors(data)
        if errs: raise ApiError(400, "Phiếu chưa đủ thông tin", {"errors": errs})
        status, submitted_at = "submitted", now()
    db.execute("UPDATE reports SET machine=?,report_date=?,shift=?,worker_name=?,data=?,status=?,submitted_at=?,updated_at=? WHERE id=?",
               (machine, d, shift, worker, json.dumps(data, ensure_ascii=False), status, submitted_at, now(), r["id"]))
    db.commit()
    return {"ok": True, "status": status, "status_vi": STATUS_VI[status], "saved_at": now()[11:16]}


@route("POST", r"/api/reports/(\d+)/approve")
def r_approve(h, db, ident, m, q, body):
    need_staff(ident)
    r = db.execute("SELECT * FROM reports WHERE id=?", (int(m.group(1)),)).fetchone()
    u = ident["user"]
    db.execute("UPDATE reports SET status='approved', approved_by=?, approved_at=?, manager_note='' WHERE id=?",
               (u["full_name"] or u["username"], now(), r["id"]))
    db.commit()
    return {"ok": True}


@route("POST", r"/api/reports/(\d+)/return")
def r_return(h, db, ident, m, q, body):
    need_staff(ident)
    r = db.execute("SELECT * FROM reports WHERE id=?", (int(m.group(1)),)).fetchone()
    note = str(body.get("note", "")).strip()[:500]
    if not note: raise ApiError(400, "Vui lòng nhập lý do trả lại")
    db.execute("UPDATE reports SET status='rejected', manager_note=?, approved_by='', approved_at='' WHERE id=?", (note, r["id"]))
    db.commit()
    return {"ok": True}


@route("POST", r"/api/reports/(\d+)/confirm")
def r_confirm_roll(h, db, ident, m, q, body):
    need_staff(ident)
    r = db.execute("SELECT * FROM reports WHERE id=?", (int(m.group(1)),)).fetchone()
    data = json.loads(r["data"] or "{}")
    for p in data.get("products", []):
        for ro in p.get("rolls", []):
            if ro.get("id") == body.get("roll_id"):
                ro["ql_xn"] = bool(body.get("value"))
                ro["ql_xn_by"] = (ident["user"]["full_name"] or ident["user"]["username"]) if ro["ql_xn"] else ""
    db.execute("UPDATE reports SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), r["id"]))
    db.commit()
    return {"ok": True}


@route("DELETE", r"/api/reports/(\d+)")
def r_report_delete(h, db, ident, m, q, body):
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
    un = str(body.get("username", "")).strip().lower()
    salt, hh = hash_pw(str(body.get("password", "123456")))
    db.execute("INSERT INTO users(username,full_name,role,salt,pass_hash,created_at) VALUES(?,?,?,?,?,?)",
               (un, str(body.get("full_name", "")).strip()[:60], body.get("role", "manager"), salt, hh, now()))
    db.commit()
    return {"ok": True}


@route("PUT", r"/api/users/(\d+)")
def r_user_edit(h, db, ident, m, q, body):
    need_staff(ident, True)
    uid = int(m.group(1))
    if "full_name" in body: db.execute("UPDATE users SET full_name=? WHERE id=?", (str(body["full_name"]).strip()[:60], uid))
    if body.get("role") in ("manager", "admin"): db.execute("UPDATE users SET role=? WHERE id=?", (body["role"], uid))
    if "active" in body: db.execute("UPDATE users SET active=? WHERE id=?", (1 if body["active"] else 0, uid))
    if body.get("password"):
        salt, hh = hash_pw(str(body["password"]))
        db.execute("UPDATE users SET salt=?, pass_hash=? WHERE id=?", (salt, hh, uid))
    db.commit()
    return {"ok": True}


@route("GET", r"/api/devices")
def r_devices(h, db, ident, m, q, body):
    need_staff(ident, True)
    rows = db.execute("SELECT d.*, (SELECT COUNT(*) FROM reports r WHERE r.device_id=d.id) AS n_reports FROM devices d ORDER BY d.name").fetchall()
    return {"devices": [dict(x) for x in rows]}


@route("PUT", r"/api/settings")
def r_settings(h, db, ident, m, q, body):
    need_staff(ident, True)
    machines = [str(x).strip()[:10] for x in body.get("machines", []) if str(x).strip()]
    shifts = [{"code": str(s.get("code", "")).strip()[:10], "name": str(s.get("name", "")).strip()[:30]}
              for s in body.get("shifts", []) if str(s.get("code", "")).strip()]
    db.execute("REPLACE INTO settings VALUES('machines',?)", (json.dumps(machines, ensure_ascii=False),))
    db.execute("REPLACE INTO settings VALUES('shifts',?)", (json.dumps(shifts, ensure_ascii=False),))
    db.commit()
    return {"ok": True}


class handler(BaseHTTPRequestHandler):
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
            row = db.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>? AND u.active=1",
                             (auth[7:], time.time())).fetchone()
            return {"kind": "staff", "user": dict(row)} if row else None
        dev = self.headers.get("X-Device-Id", "")
        if dev:
            row = db.execute("SELECT * FROM devices WHERE id=?", (dev,)).fetchone()
            if row:
                return {"kind": "worker", "device": dict(row)}
        return None

    def _handle(self, method):
        init_db()
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        db = connect()
        try:
            body = {}
            if method in ("POST", "PUT"):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
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

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_PUT(self): self._handle("PUT")
    def do_DELETE(self): self._handle("DELETE")
