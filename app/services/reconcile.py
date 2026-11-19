from app.db import HEALTH_OK, get_db
from app.services.pointers import list_pointer_states


def run_reconcile(as_of: str, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        contracts = conn.execute(
            "SELECT table_name, version_no FROM schema_contracts WHERE status='现行'"
        ).fetchall()
        catalog = {
            r["table_name"]: r["live_version"]
            for r in conn.execute("SELECT table_name, live_version FROM catalog_tables")
        }
        pointers = {
            r["table_name"]: r
            for r in conn.execute(
                "SELECT table_name, live_version FROM live_pointers"
            )
        }
        matched = 0
        drifted = 0
        details = []
        for c in contracts:
            live = catalog.get(c["table_name"])
            ptr = pointers.get(c["table_name"])
            ptr_ver = ptr["live_version"] if ptr else None
            if live == c["version_no"] and ptr_ver == c["version_no"]:
                matched += 1
            else:
                drifted += 1
                details.append(
                    f"{c['table_name']}:契约v{c['version_no']}/目录v{live}/读针v{ptr_ver}"
                )
        # 水线标记与指针卡片同源：任一工程读针不齐就亮同一枚标记
        states = list_pointer_states(db_path)
        bad = [s for s in states if s["health"] != HEALTH_OK]
        watermark = HEALTH_OK if not bad else bad[0]["health"]
        summary = (
            f"对账{as_of}：对齐{matched}，漂移{drifted}，水线【{watermark}】"
            + (("；" + "，".join(details)) if details else "")
        )
        cur = conn.execute(
            "INSERT INTO reconcile_reports(as_of, matched, drifted, summary, pointer_health) "
            "VALUES (?,?,?,?,?)",
            (as_of, matched, drifted, summary, watermark),
        )
        row = conn.execute(
            "SELECT * FROM reconcile_reports WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)


def list_reports(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM reconcile_reports ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
