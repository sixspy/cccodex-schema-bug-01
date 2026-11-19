"""复验：读针同源、脏针白话拦截、值班纠偏、唯一索引、并发互斥、水线、老库迁移。"""
import os
import sqlite3
import threading

import pytest

from app.db import connect, get_db, init_db
from app.services import (
    apply_contract,
    list_pointer_states,
    repair_pointer,
    run_breaking_check,
    run_reconcile,
)
from app.services.errors import ServiceError

ORDERS = "dw_orders_d"


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "lab.db")
    init_db(path)
    return path


def _draft_id(conn, table=ORDERS):
    return conn.execute(
        "SELECT id FROM schema_contracts WHERE table_name=? AND status='草稿'",
        (table,),
    ).fetchone()["id"]


def _tamper_pointer(db_path, table=ORDERS, version=99):
    with get_db(db_path) as conn:
        conn.execute(
            "INSERT INTO live_pointers(table_name, live_version) VALUES (?,?) "
            "ON CONFLICT(table_name) DO UPDATE SET live_version=excluded.live_version",
            (table, version),
        )


def test_dirty_pointer_blocks_both_check_and_apply(db_path):
    """检查页与升版页共用一根读针：脏针两处都白话拦下，且不再旁路扫现行。"""
    _tamper_pointer(db_path, ORDERS, 99)
    with get_db(db_path) as conn:
        cid = _draft_id(conn)
    with pytest.raises(ServiceError) as ei1:
        run_breaking_check(cid, db_path)
    assert ei1.value.status_code == 409 and "读针" in ei1.value.message
    with pytest.raises(ServiceError) as ei2:
        apply_contract(cid, db_path)
    assert ei2.value.status_code == 409 and "读针" in ei2.value.message
    # 脏针仍能被检查页通过指针卡片读到并标记
    states = {s["table_name"]: s for s in list_pointer_states(db_path)}
    assert states[ORDERS]["health"] == "脏针"
    assert states[ORDERS]["pointer_version"] == 99


def test_repair_then_check_and_apply_align_same_version(db_path):
    """值班纠偏后服务按落点改写读针；检查与升版各复验一轮版号对齐。"""
    _tamper_pointer(db_path, ORDERS, 99)
    state = repair_pointer(ORDERS, db_path)
    assert state["health"] == "针齐"
    assert state["pointer_version"] == state["contract_version"] == state["catalog_version"] == 1

    with get_db(db_path) as conn:
        cid = _draft_id(conn)
    check = run_breaking_check(cid, db_path)
    assert check["from_version"] == 1 and check["to_version"] == 2
    applied = apply_contract(cid, db_path)
    assert applied["version_no"] == 2 and applied["status"] == "现行"

    # 升版后检查读针与升版读针仍同源，目录入库版号同步为 v2
    states = {s["table_name"]: s for s in list_pointer_states(db_path)}
    assert states[ORDERS]["health"] == "针齐"
    assert states[ORDERS]["pointer_version"] == 2
    assert states[ORDERS]["catalog_version"] == 2
    # 旧现行已停用，同表只有一条现行
    with get_db(db_path) as conn:
        lives = conn.execute(
            "SELECT COUNT(*) AS n FROM schema_contracts "
            "WHERE table_name=? AND status='现行'", (ORDERS,)
        ).fetchone()["n"]
        assert lives == 1
    # 再跑对账应直接以升版后的读针为准、零漂移
    report = run_reconcile("2026-11-19", db_path)
    assert report["pointer_health"] == "针齐"
    assert report["drifted"] == 0


def test_repair_failure_returns_plain_message_and_keeps_dirty_row(db_path):
    """纠偏失败吐白话且不覆盖读针行：缺工程 / 无现行两类。"""
    _tamper_pointer(db_path, ORDERS, 99)
    with pytest.raises(ServiceError) as ei:
        repair_pointer("no_such_table", db_path)
    assert ei.value.status_code == 404 and "不存在" in ei.value.message
    with get_db(db_path) as conn:
        ptr = conn.execute(
            "SELECT live_version FROM live_pointers WHERE table_name=?", (ORDERS,)
        ).fetchone()["live_version"]
        assert ptr == 99  # 脏针原样保留


def test_single_live_unique_index_blocks_second_current(db_path):
    """落点一：部分唯一索引拦住第二现行，写库即 IntegrityError。"""
    with get_db(db_path) as conn:
        cid = _draft_id(conn)
        # 手工把草稿也刷成现行：索引必须拒绝
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE schema_contracts SET status='现行' WHERE id=?", (cid,)
            )
        conn.rollback()
    # 正常升版路径不受影响（先停用旧现行）
    with get_db(db_path) as conn:
        cid = _draft_id(conn)
    applied = apply_contract(cid, db_path)
    assert applied["status"] == "现行"


def test_concurrent_repair_is_mutex(tmp_path, monkeypatch):
    """两名值班并发点纠偏：写锁串行化，读针只被正确覆盖一次、无丢失更新。"""
    monkeypatch.setenv("BUSY_TIMEOUT_MS", "3000")
    path = str(tmp_path / "race.db")
    init_db(path)
    _tamper_pointer(path, ORDERS, 99)

    barrier = threading.Barrier(2)
    results = []

    def worker():
        barrier.wait()
        try:
            results.append(("ok", repair_pointer(ORDERS, path)))
        except ServiceError as exc:
            results.append(("err", exc.status_code, exc.message))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start(); t1.join(); t2.join()

    # 写锁保证两次纠偏被串行化，最终读针确定为唯一现行 v1，不互相覆盖
    states = {s["table_name"]: s for s in list_pointer_states(path)}
    assert states[ORDERS]["health"] == "针齐"
    assert states[ORDERS]["pointer_version"] == 1
    with get_db(path) as conn:
        ptr = conn.execute(
            "SELECT live_version FROM live_pointers WHERE table_name=?", (ORDERS,)
        ).fetchone()["live_version"]
        assert ptr == 1
        lock_left = conn.execute(
            "SELECT 1 FROM meta WHERE key='repair_lock'"
        ).fetchone()
        assert lock_left is None


def test_repair_lock_marker_returns_plain_409(db_path):
    """互斥标记在位时第二个值班点直接白话 409，不触碰读针行。"""
    _tamper_pointer(db_path, ORDERS, 99)
    import time as _time
    with get_db(db_path) as conn:
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('repair_lock',?)",
            (f"{ORDERS}|{_time.time()}",),
        )
    with pytest.raises(ServiceError) as ei:
        repair_pointer(ORDERS, db_path)
    assert ei.value.status_code == 409 and "互斥" in ei.value.message
    # 读针脏值原样未动
    with get_db(db_path) as conn:
        ptr = conn.execute(
            "SELECT live_version FROM live_pointers WHERE table_name=?", (ORDERS,)
        ).fetchone()["live_version"]
        assert ptr == 99


def test_stale_repair_lock_is_taken_over(db_path):
    """崩溃残留的陈旧锁（超 30 秒）自动接管，不永久卡死纠偏。"""
    _tamper_pointer(db_path, ORDERS, 99)
    import time as _time
    with get_db(db_path) as conn:
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('repair_lock',?)",
            (f"{ORDERS}|{_time.time() - 60}",),
        )
    state = repair_pointer(ORDERS, db_path)
    assert state["health"] == "针齐" and state["pointer_version"] == 1


def test_reconcile_watermark_shares_same_marker_as_pointer_card(db_path):
    """水线亮同一标记：脏针对账必漂，纠偏后水线转针齐。"""
    _tamper_pointer(db_path, ORDERS, 99)
    bad = run_reconcile("2026-11-19", db_path)
    assert bad["pointer_health"] == "脏针"
    assert bad["drifted"] >= 1 and "读针v99" in bad["summary"]

    repair_pointer(ORDERS, db_path)
    good = run_reconcile("2026-11-19", db_path)
    assert good["pointer_health"] == "针齐"
    assert good["matched"] >= 1 and good["drifted"] == 0


def test_catalog_drift_flagged_and_repaired(db_path):
    """目录入库版号拧花单独可识别，纠偏同步对齐目录。"""
    with get_db(db_path) as conn:
        conn.execute(
            "UPDATE catalog_tables SET live_version=7 WHERE table_name=?", (ORDERS,)
        )
    states = {s["table_name"]: s for s in list_pointer_states(db_path)}
    assert states[ORDERS]["health"] == "脏针"
    assert "目录" in states[ORDERS]["detail"]
    repair_pointer(ORDERS, db_path)
    states = {s["table_name"]: s for s in list_pointer_states(db_path)}
    assert states[ORDERS]["health"] == "针齐"
    assert states[ORDERS]["catalog_version"] == 1


def test_legacy_dual_pointer_db_migrates_to_single_source(tmp_path):
    """老库（planted 版 ptr_check=1 / ptr_apply=2 双针）迁移：双针撤销、读针同源。"""
    path = str(tmp_path / "legacy.db")
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE schema_contracts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT NOT NULL,
          version_no INTEGER NOT NULL, status TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          UNIQUE(table_name, version_no));
        CREATE TABLE contract_fields (
          id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER NOT NULL,
          field_name TEXT NOT NULL, field_type TEXT NOT NULL,
          UNIQUE(contract_id, field_name));
        CREATE TABLE catalog_tables (
          id INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT NOT NULL UNIQUE,
          live_version INTEGER NOT NULL,
          registered_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE apply_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER NOT NULL,
          table_name TEXT NOT NULL, from_version INTEGER, to_version INTEGER NOT NULL,
          result TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE reconcile_reports (
          id INTEGER PRIMARY KEY AUTOINCREMENT, as_of TEXT NOT NULL,
          matched INTEGER NOT NULL, drifted INTEGER NOT NULL, summary TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )
    raw.execute(
        "INSERT INTO schema_contracts(table_name,version_no,status) VALUES (?,1,'现行')",
        (ORDERS,),
    )
    raw.execute(
        "INSERT INTO schema_contracts(table_name,version_no,status) VALUES (?,2,'草稿')",
        (ORDERS,),
    )
    raw.execute("INSERT INTO catalog_tables(table_name,live_version) VALUES (?,1)", (ORDERS,))
    raw.execute("INSERT INTO meta(key,value) VALUES('seeded','1')")
    raw.execute("INSERT INTO meta(key,value) VALUES('ptr_check','1')")
    raw.execute("INSERT INTO meta(key,value) VALUES('ptr_apply','2')")
    raw.commit()
    raw.close()

    init_db(path)

    with get_db(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM meta WHERE key IN ('ptr_check','ptr_apply')"
        ).fetchone()["n"] == 0
        ptr = conn.execute(
            "SELECT live_version FROM live_pointers WHERE table_name=?", (ORDERS,)
        ).fetchone()
        assert ptr is not None and ptr["live_version"] == 1
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_contract_single_live'"
        ).fetchone()
        assert idx is not None
    states = {s["table_name"]: s for s in list_pointer_states(path)}
    assert states[ORDERS]["health"] == "针齐"


def test_legacy_dual_live_db_does_not_crash_and_can_be_repaired(tmp_path):
    """老库已双现行：启动不崩，索引降级待建，停用多余现行后纠偏补建索引。"""
    path = str(tmp_path / "dual.db")
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE schema_contracts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT NOT NULL,
          version_no INTEGER NOT NULL, status TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          UNIQUE(table_name, version_no));
        CREATE TABLE contract_fields (
          id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER NOT NULL,
          field_name TEXT NOT NULL, field_type TEXT NOT NULL,
          UNIQUE(contract_id, field_name));
        CREATE TABLE catalog_tables (
          id INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT NOT NULL UNIQUE,
          live_version INTEGER NOT NULL,
          registered_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE apply_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER NOT NULL,
          table_name TEXT NOT NULL, from_version INTEGER, to_version INTEGER NOT NULL,
          result TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE reconcile_reports (
          id INTEGER PRIMARY KEY AUTOINCREMENT, as_of TEXT NOT NULL,
          matched INTEGER NOT NULL, drifted INTEGER NOT NULL, summary TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )
    raw.execute("INSERT INTO schema_contracts(table_name,version_no,status) VALUES (?,1,'现行')", (ORDERS,))
    raw.execute("INSERT INTO schema_contracts(table_name,version_no,status) VALUES (?,2,'现行')", (ORDERS,))
    raw.execute("INSERT INTO catalog_tables(table_name,live_version) VALUES (?,1)", (ORDERS,))
    raw.execute("INSERT INTO meta(key,value) VALUES('seeded','1')")
    raw.commit(); raw.close()

    init_db(path)  # 不应抛异常
    states = {s["table_name"]: s for s in list_pointer_states(path)}
    assert states[ORDERS]["health"] == "双现行"

    # 值班先停用多余的 v1（保留高版本 v2 为唯一现行），再纠偏
    with get_db(path) as conn:
        conn.execute(
            "UPDATE schema_contracts SET status='停用' WHERE table_name=? AND version_no=1",
            (ORDERS,),
        )
    state = repair_pointer(ORDERS, path)
    assert state["health"] == "针齐"
    assert state["pointer_version"] == 2
    with get_db(path) as conn:
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_contract_single_live'"
        ).fetchone()
        assert idx is not None
        pending = conn.execute(
            "SELECT 1 FROM meta WHERE key='live_index_pending'"
        ).fetchone()
        assert pending is None
