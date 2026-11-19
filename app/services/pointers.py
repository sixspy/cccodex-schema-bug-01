import os
import sqlite3
import time

from app.db import (
    HEALTH_DIRTY,
    HEALTH_DUAL_LIVE,
    HEALTH_MISSING,
    HEALTH_OK,
    connect,
    ensure_single_live_index,
    get_db,
)
from app.services.errors import ServiceError


def get_live_contract(conn: sqlite3.Connection, table_name: str) -> sqlite3.Row:
    """检查页与升版页共同的唯一读路：只认 live_pointers 这一根读针。

    读针缺失 / 指向不存在的版本 / 指向非现行版本，一律白话报错，
    不再旁路扫 status='现行'，否则脏针会被静默掩盖、复验照旧失败。
    """
    ptr = conn.execute(
        "SELECT live_version FROM live_pointers WHERE table_name=?",
        (table_name,),
    ).fetchone()
    if ptr is None:
        raise ServiceError(
            f"工程{table_name}缺读针，无法定位现行契约，请先在指针卡片点纠偏", 409
        )
    row = conn.execute(
        "SELECT * FROM schema_contracts WHERE table_name=? AND version_no=?",
        (table_name, ptr["live_version"]),
    ).fetchone()
    if row is None:
        raise ServiceError(
            f"工程{table_name}的读针指向 v{ptr['live_version']}，但该版契约不存在，"
            "读针已脏，请先纠偏再操作",
            409,
        )
    if row["status"] != "现行":
        raise ServiceError(
            f"工程{table_name}的读针指向 v{ptr['live_version']}（当前状态：{row['status']}），"
            "并非现行契约，读针已脏，请先纠偏再操作",
            409,
        )
    return row


def _state_for(conn: sqlite3.Connection, table_name: str) -> dict:
    ptr = conn.execute(
        "SELECT live_version FROM live_pointers WHERE table_name=?",
        (table_name,),
    ).fetchone()
    lives = conn.execute(
        "SELECT version_no FROM schema_contracts WHERE table_name=? AND status='现行' "
        "ORDER BY version_no",
        (table_name,),
    ).fetchall()
    catalog = conn.execute(
        "SELECT live_version FROM catalog_tables WHERE table_name=?",
        (table_name,),
    ).fetchone()
    contract_ver = lives[0]["version_no"] if lives else None
    catalog_ver = catalog["live_version"] if catalog else None
    ptr_ver = ptr["live_version"] if ptr else None

    if len(lives) > 1:
        health = HEALTH_DUAL_LIVE
        detail = f"出现{len(lives)}条现行契约，唯一索引应已拦截"
    elif conn.execute(
        "SELECT 1 FROM meta WHERE key='live_index_pending'"
    ).fetchone():
        health = HEALTH_DUAL_LIVE
        detail = "唯一索引待重建：历史库曾出现双现行，停用多余现行后再纠偏"
    elif ptr is None:
        health = HEALTH_MISSING
        detail = "读针行缺失"
    elif ptr_ver != contract_ver or contract_ver is None:
        health = HEALTH_DIRTY
        detail = f"读针v{ptr_ver}与现行v{contract_ver}不一致"
    elif catalog_ver != contract_ver:
        health = HEALTH_DIRTY
        detail = f"目录入库版号v{catalog_ver}与现行v{contract_ver}拧花"
    else:
        health = HEALTH_OK
        detail = f"读针/现行/目录均为v{contract_ver}"
    return {
        "table_name": table_name,
        "pointer_version": ptr_ver,
        "contract_version": contract_ver,
        "catalog_version": catalog_ver,
        "health": health,
        "detail": detail,
    }


def list_pointer_states(db_path: str | None = None) -> list[dict]:
    """指针卡片数据源：每张工程一行，亮同一套健康标记。"""
    with get_db(db_path) as conn:
        names = [
            r["table_name"]
            for r in conn.execute(
                "SELECT table_name FROM schema_contracts UNION "
                "SELECT table_name FROM live_pointers UNION "
                "SELECT table_name FROM catalog_tables ORDER BY table_name"
            )
        ]
        return [_state_for(conn, name) for name in names]


def list_repairs(db_path: str | None = None) -> list[dict]:
    """值班纠偏编排记录入档。"""
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM repair_runs ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def repair_pointer(table_name: str, db_path: str | None = None) -> dict:
    """值班点纠偏：整事务把读针切换到唯一现行，并撤销脏针、对齐目录。

    互斥：BEGIN IMMEDIATE 先拿写锁，并发的第二个值班点会被 SQLite 挡住，
    再叠加 repair_lock 标记，失败统一吐白话 409，绝不互相覆盖读针行。
    """
    table_name = (table_name or "").strip()
    if not table_name:
        raise ServiceError("缺少工程名，无法纠偏", 400)

    conn = connect(db_path)
    try:
        # Python sqlite3 在显式 BEGIN IMMEDIATE 撞锁时不吃 busy_timeout，
        # 这里按预算短退避重试，超时再吐白话 409，保证并发互斥且快速失败。
        budget = float(os.environ.get("BUSY_TIMEOUT_MS", "10000")) / 1000.0
        deadline = time.monotonic() + budget
        while True:
            try:
                conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    if "locked" in str(exc).lower():
                        raise ServiceError(
                            "另一值班点正在纠偏，写锁互斥中，请稍后再点一次", 409
                        )
                    raise
                time.sleep(0.02)

        lock = conn.execute(
            "SELECT value FROM meta WHERE key='repair_lock'"
        ).fetchone()
        if lock and lock["value"]:
            # 值形如 "table|epoch"：崩溃残留超过 30 秒的锁视为陈旧锁自动接管
            parts = lock["value"].split("|", 1)
            stale = len(parts) == 2 and (
                time.time() - float(parts[1] or 0) > 30
            )
            if not stale:
                raise ServiceError(
                    f"工程{parts[0]}正在纠偏，读针互斥中，请稍后重试", 409
                )
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('repair_lock',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"{table_name}|{time.time()}",),
        )

        known = conn.execute(
            "SELECT 1 FROM schema_contracts WHERE table_name=?", (table_name,)
        ).fetchone()
        if not known:
            raise ServiceError(f"工程{table_name}不存在，无法纠偏读针", 404)

        lives = conn.execute(
            "SELECT * FROM schema_contracts WHERE table_name=? AND status='现行' "
            "ORDER BY version_no",
            (table_name,),
        ).fetchall()
        if not lives:
            raise ServiceError(
                f"工程{table_name}没有现行契约，无法确定读针落点，请先换版应用", 409
            )
        if len(lives) > 1:
            raise ServiceError(
                f"工程{table_name}存在{len(lives)}条现行契约，已被部分唯一索引拦住，"
                "请先停用多余现行再纠偏",
                409,
            )
        target = lives[0]

        old_ptr = conn.execute(
            "SELECT live_version FROM live_pointers WHERE table_name=?",
            (table_name,),
        ).fetchone()
        old_ver = old_ptr["live_version"] if old_ptr else None

        # 读针整事务切换到唯一现行（脏针行被同一行 UPSERT 覆盖撤销）
        conn.execute(
            "INSERT INTO live_pointers(table_name, live_version) VALUES (?,?) "
            "ON CONFLICT(table_name) DO UPDATE SET "
            "live_version=excluded.live_version, updated_at=datetime('now')",
            (table_name, target["version_no"]),
        )
        # 目录入库版号同步对齐，消除拧花
        conn.execute(
            "INSERT INTO catalog_tables(table_name, live_version) VALUES (?,?) "
            "ON CONFLICT(table_name) DO UPDATE SET "
            "live_version=excluded.live_version, registered_at=datetime('now')",
            (table_name, target["version_no"]),
        )
        reason = (
            f"读针由v{old_ver}纠偏至v{target['version_no']}"
            if old_ver is not None
            else f"缺针补建至v{target['version_no']}"
        )
        conn.execute(
            "INSERT INTO repair_runs(table_name, old_version, new_version, reason, result) "
            "VALUES (?,?,?,?,?)",
            (table_name, old_ver, target["version_no"], reason, "已纠偏"),
        )
        # 纠偏后若全库只剩单现行，补建唯一索引并撤销待修标记
        ensure_single_live_index(conn)
        conn.execute("DELETE FROM meta WHERE key='repair_lock'")
        conn.commit()
    except ServiceError:
        conn.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ServiceError(
            f"纠偏被唯一约束拦住（同表只许一条现行）：{exc}，请先停用多余现行", 409
        )
    except sqlite3.OperationalError as exc:
        conn.rollback()
        if "locked" in str(exc).lower():
            raise ServiceError(
                "另一值班点正在纠偏，写锁互斥中，请稍后再点一次", 409
            )
        raise
    finally:
        conn.close()

    with get_db(db_path) as conn:
        return _state_for(conn, table_name)
