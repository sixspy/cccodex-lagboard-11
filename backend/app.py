
import hashlib, secrets, sqlite3, threading
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}
# 升级故障注入开关（仅测试用）：为真时甲路径事务在提交前抛错，整笔撤回
UPGRADE_FAULT = False
# 持锁关键区钩子（仅测试用）：默认空操作，测试可替换为同步事件以确定性制造争用窗口
UPGRADE_LOCK_HELD_HOOK = lambda: None
# 升级锁（进程内，按源互斥）：双路同点同一源时后交者只能记“等级升级争用”
_UPGRADE_LOCKS_GUARD = threading.Lock()
_UPGRADE_LOCKS: dict[str, threading.Lock] = {}


def upgrade_lock_for(source: str) -> threading.Lock:
    with _UPGRADE_LOCKS_GUARD:
        lk = _UPGRADE_LOCKS.get(source)
        if lk is None:
            lk = threading.Lock()
            _UPGRADE_LOCKS[source] = lk
        return lk


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from datetime import datetime
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "源到达SLA"
SKELETON = "多源到达时钟对照 SLA 分级，超限则冻结或降级输出，补齐后恢复并对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sources(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT UNIQUE NOT NULL,
          sla_minutes INTEGER NOT NULL,
          output_mode TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS arrivals(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          source_clock TEXT NOT NULL,
          ingest_clock TEXT NOT NULL,
          lag_minutes INTEGER NOT NULL,
          level TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS catchups(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          status TEXT NOT NULL,
          restored_mode TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS upgrade_ledger(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          arrival_id INTEGER NOT NULL,
          source TEXT NOT NULL,
          from_level TEXT NOT NULL,
          to_level TEXT NOT NULL,
          result TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS upgrade_locks(
          source TEXT PRIMARY KEY,
          holder TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        if conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 0:
            conn.executemany("INSERT INTO sources(name,sla_minutes,output_mode) VALUES (?,?,?)", [
                ("订单源", 15, "可写"), ("支付源", 20, "降级只读"), ("物流源", 30, "冻结"),
            ])
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         ("支付源","2026-11-16T08:00:00","2026-11-16T08:35:00",35,"告警"))
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         ("物流源","2026-11-16T07:00:00","2026-11-16T09:10:00",130,"违约"))

def level_of(lag, sla):
    if lag <= sla: return "正常"
    if lag <= sla * 2: return "告警"
    return "违约"

def mode_of(level):
    return {"正常":"可写", "告警":"降级只读", "违约":"冻结"}[level]

@app.on_event("startup")
def _s(): init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "sources": n, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/sources")
def sources(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM sources ORDER BY id").fetchall()]}

class ArrivalIn(BaseModel):
    source: str
    source_clock: str
    ingest_clock: str

@app.post("/api/arrivals")
def add_arrival(body: ArrivalIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        src = c.execute("SELECT * FROM sources WHERE name=?", (body.source,)).fetchone()
        if not src: raise HTTPException(404, "未知源")
        # lag by minute rough parse
        def parse(t):
            return datetime.fromisoformat(t)
        lag = int((parse(body.ingest_clock) - parse(body.source_clock)).total_seconds() // 60)
        if lag < 0: lag = 0
        lv = level_of(lag, src["sla_minutes"])
        mode = mode_of(lv)
        c.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                  (body.source, body.source_clock, body.ingest_clock, lag, lv))
        c.execute("UPDATE sources SET output_mode=? WHERE name=?", (mode, body.source))
        return {"lag_minutes": lag, "level": lv, "output_mode": mode}

@app.get("/api/arrivals")
def arrivals(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM arrivals ORDER BY id DESC").fetchall()]}

@app.post("/api/catchup/{source}")
def catchup(source: str, authorization: str | None = Header(default=None)):
    require_user(authorization)
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as c:
        src = c.execute("SELECT * FROM sources WHERE name=?", (source,)).fetchone()
        if not src: raise HTTPException(404, "未知源")
        c.execute("UPDATE sources SET output_mode='可写' WHERE name=?", (source,))
        c.execute("INSERT INTO catchups(source,status,restored_mode,note,created_at) VALUES (?,?,?,?,?)",
                  (source, "成功", "可写", "补齐后恢复可写", now))
        return {"ok": True, "output_mode": "可写"}

@app.get("/api/catchups")
def catchups(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM catchups ORDER BY id DESC").fetchall()]}

def _write_ledger(conn, arrival_id, source, from_level, to_level, result, note, now):
    conn.execute(
        "INSERT INTO upgrade_ledger(arrival_id,source,from_level,to_level,result,note,created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (arrival_id, source, from_level, to_level, result, note, now))

@app.post("/api/arrivals/{arrival_id}/upgrade")
def upgrade_arrival(arrival_id: int, authorization: str | None = Header(default=None)):
    """告警升违约连坐改态（甲路径）：违约等级与源输出态、升级账本同库一次提交，遇错全部撤回。"""
    username = require_user(authorization)
    now = datetime.now().isoformat(timespec="seconds")
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM arrivals WHERE id=?", (arrival_id,)).fetchone()
        if not row:
            raise HTTPException(404, "未知到达")
        source, current_level = row["source"], row["level"]

        # 先占升级锁：双路同点同一源，后交者只记争用，不做任何改态
        lk = upgrade_lock_for(source)
        if not lk.acquire(blocking=False):
            _write_ledger(conn, arrival_id, source, current_level, current_level,
                          "等级升级争用", f"{username} 后交，升级锁被占用", now)
            conn.commit()
            raise HTTPException(409, "等级升级争用：同源升级正在处理，后交仅记争用")
        # 持锁关键区钩子（测试可注入等待，确定性覆盖争用窗口；生产为空操作）
        UPGRADE_LOCK_HELD_HOOK()
        try:
            # 持久升级锁：同库留痕，跨进程同样互斥
            try:
                conn.execute("INSERT INTO upgrade_locks(source,holder,created_at) VALUES (?,?,?)",
                             (source, username, now))
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()
                _write_ledger(conn, arrival_id, source, current_level, current_level,
                              "等级升级争用", f"{username} 后交，持久升级锁被占用", now)
                conn.commit()
                raise HTTPException(409, "等级升级争用：持久升级锁被占用，后交仅记争用")
            try:
                latest = conn.execute("SELECT * FROM arrivals WHERE id=?", (arrival_id,)).fetchone()
                # 升级说明只许可“告警 -> 违约”，其余一律判升级偏了并驳回
                if latest["level"] != "告警":
                    note = "升级偏了：仅允许告警升违约"
                    _write_ledger(conn, arrival_id, source, latest["level"], latest["level"],
                                  "升级偏了", note, now)
                    conn.commit()
                    raise HTTPException(409, note)

                # 甲路径：等级、源输出态、成功账本三笔写在同一事务，一次提交
                conn.execute("UPDATE arrivals SET level='违约' WHERE id=?", (arrival_id,))
                conn.execute("UPDATE sources SET output_mode='冻结' WHERE name=?", (source,))
                _write_ledger(conn, arrival_id, source, "告警", "违约", "成功",
                              f"{username} 告警升违约，连坐输出态冻结（甲路径同库一次提交）", now)
                if UPGRADE_FAULT:
                    raise RuntimeError("注入的升级写路径故障")
                conn.commit()
                return {"ok": True, "result": "成功", "arrival_id": arrival_id, "source": source,
                        "from_level": "告警", "to_level": "违约", "output_mode": "冻结"}
            except HTTPException:
                raise
            except Exception:
                # 遇错全部撤回：等级改口、目录冻结、成功账本一并作废，正式零改
                conn.rollback()
                raise HTTPException(500, "等级升级失败，已全部撤回")
        finally:
            # 释放持久升级锁与进程内升级锁
            try:
                conn.execute("DELETE FROM upgrade_locks WHERE source=?", (source,))
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
            lk.release()
    finally:
        conn.close()

@app.get("/api/upgrade-ledger")
def upgrade_ledger(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute(
            "SELECT * FROM upgrade_ledger ORDER BY id DESC").fetchall()]}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
