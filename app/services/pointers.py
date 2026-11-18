from app.db import get_db


def check_ptr(db_path: str | None = None) -> int:
    with get_db(db_path) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='ptr_check'").fetchone()
        return int(row["value"]) if row else 1


def apply_ptr(db_path: str | None = None) -> int:
    with get_db(db_path) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='ptr_apply'").fetchone()
        return int(row["value"]) if row else 1
