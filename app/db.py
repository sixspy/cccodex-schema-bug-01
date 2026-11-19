import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config

# 读针健康标记：检查页 / 指针卡片 / 对账水线四处共用同一套字面量
HEALTH_OK = "针齐"          # 读针与现行契约、目录版号完全对齐
HEALTH_DIRTY = "脏针"       # 读针指向的契约不存在、非现行或版号不符
HEALTH_MISSING = "缺针"     # 该工程还没有读针行
HEALTH_DUAL_LIVE = "双现行"  # 同工程出现两条现行，唯一索引拦截 / 待纠偏


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 并发点纠偏时让第二个事务等锁，而不是立刻报 database is locked
    busy_ms = int(os.environ.get("BUSY_TIMEOUT_MS", "10000"))
    conn.execute(f"PRAGMA busy_timeout = {busy_ms}")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# 落点一（硬约束）：同表只许一条现行，从库层拦住“第二现行”
SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_contracts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  version_no INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('现行','草稿','停用')),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(table_name, version_no)
);

CREATE TABLE IF NOT EXISTS contract_fields (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL REFERENCES schema_contracts(id),
  field_name TEXT NOT NULL,
  field_type TEXT NOT NULL,
  UNIQUE(contract_id, field_name)
);

CREATE TABLE IF NOT EXISTS catalog_tables (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL UNIQUE,
  live_version INTEGER NOT NULL,
  registered_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS apply_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL,
  table_name TEXT NOT NULL,
  from_version INTEGER,
  to_version INTEGER NOT NULL,
  result TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reconcile_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of TEXT NOT NULL,
  matched INTEGER NOT NULL,
  drifted INTEGER NOT NULL,
  summary TEXT NOT NULL,
  pointer_health TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS repair_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  old_version INTEGER,
  new_version INTEGER NOT NULL,
  reason TEXT NOT NULL,
  result TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 落点二（主读路）：旁路读针表，检查与升版同源，一工程一行
CREATE TABLE IF NOT EXISTS live_pointers (
  table_name TEXT PRIMARY KEY,
  live_version INTEGER NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# 同表只许一条现行：第二现行在写库时即被拒（IntegrityError）。
# 单独建索引，便于老库已有双现行时降级标记而不是崩启动。
SINGLE_LIVE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_contract_single_live "
    "ON schema_contracts(table_name) WHERE status = '现行'"
)


def ensure_single_live_index(conn) -> bool:
    """返回 True 表示唯一索引在位；老库双现行时降级为待修标记。"""
    try:
        conn.execute(SINGLE_LIVE_INDEX)
        conn.execute("DELETE FROM meta WHERE key='live_index_pending'")
        return True
    except sqlite3.IntegrityError:
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('live_index_pending','1') "
            "ON CONFLICT(key) DO UPDATE SET value='1'"
        )
        return False

SEED = [
    ("dw_orders_d", 1, "现行", "初始现行", [("order_id", "INT"), ("amount", "DECIMAL"), ("biz_date", "DATE")]),
    ("dw_orders_d", 2, "草稿", "拟删 amount 改币种", [("order_id", "INT"), ("currency", "STRING"), ("biz_date", "DATE")]),
    ("dw_payments_d", 1, "现行", "支付现行", [("pay_id", "INT"), ("order_id", "INT"), ("paid_at", "TIMESTAMP")]),
]


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        # 先建全部表（含旁路读针表、纠偏编排表），再做老库列迁移与唯一索引
        conn.executescript(SCHEMA)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(reconcile_reports)")]
        if "pointer_health" not in cols:
            conn.execute(
                "ALTER TABLE reconcile_reports ADD COLUMN pointer_health TEXT NOT NULL DEFAULT ''"
            )
        # 旧库（planted 版）可能已有双现行，索引建不上时降级为待修标记而不是崩启动
        ensure_single_live_index(conn)

        seeded = conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
        # 旧版双针（ptr_check / ptr_apply 拧花）整行撤销，读针只许同源
        conn.execute("DELETE FROM meta WHERE key IN ('ptr_check','ptr_apply')")
        if seeded:
            # 老库回填读针：以唯一现行契约为准，读针整事务切换到正确版号
            rows = conn.execute(
                "SELECT table_name, version_no FROM schema_contracts WHERE status='现行'"
            ).fetchall()
            for r in rows:
                conn.execute(
                    "INSERT INTO live_pointers(table_name, live_version) VALUES (?,?) "
                    "ON CONFLICT(table_name) DO UPDATE SET live_version=excluded.live_version, "
                    "updated_at=datetime('now')",
                    (r["table_name"], r["version_no"]),
                )
            return

        for table, ver, status, note, fields in SEED:
            cur = conn.execute(
                "INSERT INTO schema_contracts(table_name, version_no, status, note) VALUES (?,?,?,?)",
                (table, ver, status, note),
            )
            cid = cur.lastrowid
            for fname, ftype in fields:
                conn.execute(
                    "INSERT INTO contract_fields(contract_id, field_name, field_type) VALUES (?,?,?)",
                    (cid, fname, ftype),
                )
            if status == "现行":
                conn.execute(
                    "INSERT INTO catalog_tables(table_name, live_version) VALUES (?,?)",
                    (table, ver),
                )
                # 现行落库即写同根读针：检查 / 升版共用这一根
                conn.execute(
                    "INSERT INTO live_pointers(table_name, live_version) VALUES (?,?)",
                    (table, ver),
                )
        conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
