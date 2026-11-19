import sqlite3

from app.db import get_db
from app.services.errors import ServiceError
from app.services.pointers import get_live_contract


def list_contracts(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM contract_fields f WHERE f.contract_id=c.id) AS field_count "
            "FROM schema_contracts c ORDER BY c.table_name, c.version_no"
        ).fetchall()
        return [dict(r) for r in rows]


def _fields(conn, contract_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT field_name, field_type FROM contract_fields WHERE contract_id=?",
        (contract_id,),
    ).fetchall()
    return {r["field_name"]: r["field_type"] for r in rows}


def run_breaking_check(contract_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        draft = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        if not draft:
            raise ServiceError("契约不存在", 404)
        if draft["status"] != "草稿":
            raise ServiceError("仅草稿可做破坏性检查", 409)
        # 检查读针：与升版读针同源（live_pointers），脏针 / 缺针直接白话拦截
        live = get_live_contract(conn, draft["table_name"])
        old_f = _fields(conn, live["id"])
        new_f = _fields(conn, draft["id"])
        removed = sorted(set(old_f) - set(new_f))
        type_changed = sorted(
            k for k in set(old_f) & set(new_f) if old_f[k] != new_f[k]
        )
        added = sorted(set(new_f) - set(old_f))
        breaking = bool(removed or type_changed)
        return {
            "contract_id": contract_id,
            "table_name": draft["table_name"],
            "from_version": live["version_no"],
            "to_version": draft["version_no"],
            "removed_fields": removed,
            "type_changed_fields": type_changed,
            "added_fields": added,
            "breaking": breaking,
            "verdict": "破坏性" if breaking else "兼容",
        }


def apply_contract(contract_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        draft = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        if not draft:
            raise ServiceError("契约不存在", 404)
        if draft["status"] != "草稿":
            raise ServiceError("仅草稿可换版应用", 409)
        # 升版读针：与检查读针同根，双针即拦；这里也保证旧现行定位唯一
        live = get_live_contract(conn, draft["table_name"])
        from_ver = live["version_no"]
        try:
            conn.execute(
                "UPDATE schema_contracts SET status='停用' WHERE id=?",
                (live["id"],),
            )
            # 部分唯一索引兜底：若同表已被塞入第二现行，这里写库即被拒
            conn.execute(
                "UPDATE schema_contracts SET status='现行' WHERE id=?",
                (contract_id,),
            )
            conn.execute(
                "INSERT INTO catalog_tables(table_name, live_version) VALUES (?,?) "
                "ON CONFLICT(table_name) DO UPDATE SET live_version=excluded.live_version, "
                "registered_at=datetime('now')",
                (draft["table_name"], draft["version_no"]),
            )
            # 升版成功后读针随事务整行切换到新版，检查/升版读到的现行从此同一条
            conn.execute(
                "INSERT INTO live_pointers(table_name, live_version) VALUES (?,?) "
                "ON CONFLICT(table_name) DO UPDATE SET "
                "live_version=excluded.live_version, updated_at=datetime('now')",
                (draft["table_name"], draft["version_no"]),
            )
            conn.execute(
                "INSERT INTO apply_runs(contract_id, table_name, from_version, to_version, result) "
                "VALUES (?,?,?,?,?)",
                (contract_id, draft["table_name"], from_ver, draft["version_no"], "已应用"),
            )
        except sqlite3.IntegrityError as exc:
            raise ServiceError(
                f"换版被唯一约束拦住：{draft['table_name']}只许一条现行契约，"
                f"无法再升 v{draft['version_no']}（{exc}），请先纠偏读针",
                409,
            )
        row = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        return dict(row)
