"""HTTP 层复验：列表 / 检查 / 指针卡片 / 对账水线四处联查与纠偏接口。"""
import pytest


async def _tamper(authed, table="dw_orders_d", version=99):
    import sqlite3

    db = authed.app["db_path"]
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO live_pointers(table_name, live_version) VALUES (?,?) "
        "ON CONFLICT(table_name) DO UPDATE SET live_version=excluded.live_version",
        (table, version),
    )
    conn.commit()
    conn.close()


async def test_pointer_card_lists_health_and_repair(authed):
    r = await authed.get("/api/pointers")
    assert r.status == 200
    items = (await r.json())["items"]
    by = {i["table_name"]: i for i in items}
    assert by["dw_orders_d"]["health"] == "针齐"

    await _tamper(authed)
    items = (await (await authed.get("/api/pointers")).json())["items"]
    by = {i["table_name"]: i for i in items}
    assert by["dw_orders_d"]["health"] == "脏针"
    assert by["dw_orders_d"]["pointer_version"] == 99

    # 检查页与升版页读同源脏针：两处都白话 409
    draft = next(
        c for c in (await (await authed.get("/api/contracts")).json())["items"]
        if c["status"] == "草稿"
    )
    r1 = await authed.post(f"/api/contracts/{draft['id']}/breaking-check", json={})
    r2 = await authed.post(f"/api/contracts/{draft['id']}/apply", json={})
    assert r1.status == 409 and "读针" in (await r1.json())["error"]
    assert r2.status == 409 and "读针" in (await r2.json())["error"]

    # 值班纠偏
    r3 = await authed.post("/api/pointers/repair", json={"table_name": "dw_orders_d"})
    assert r3.status == 200
    pointer = (await r3.json())["pointer"]
    assert pointer["health"] == "针齐" and pointer["pointer_version"] == 1

    # 编排记录入档
    runs = (await (await authed.get("/api/repair-runs")).json())["items"]
    assert runs and runs[0]["table_name"] == "dw_orders_d"
    assert runs[0]["old_version"] == 99 and runs[0]["new_version"] == 1


async def test_repair_conflict_is_plain_409(authed):
    r = await authed.post("/api/pointers/repair", json={"table_name": "ghost_table"})
    assert r.status == 404 and "不存在" in (await r.json())["error"]
    r = await authed.post("/api/pointers/repair", json={})
    assert r.status == 400


async def test_watermark_marker_consistent_across_reconcile(authed):
    await _tamper(authed)
    bad = await (await authed.post("/api/reconcile-reports", json={"as_of": "2026-11-19"})).json()
    assert bad["report"]["pointer_health"] == "脏针"
    assert "水线【脏针】" in bad["report"]["summary"]

    await authed.post("/api/pointers/repair", json={"table_name": "dw_orders_d"})
    good = await (await authed.post("/api/reconcile-reports", json={"as_of": "2026-11-19"})).json()
    assert good["report"]["pointer_health"] == "针齐"
    assert "水线【针齐】" in good["report"]["summary"]


async def test_apply_chain_keeps_single_pointer_after_version_bump(authed):
    draft = next(
        c for c in (await (await authed.get("/api/contracts")).json())["items"]
        if c["status"] == "草稿"
    )
    r = await authed.post(f"/api/contracts/{draft['id']}/apply", json={})
    assert r.status == 200
    pointers = {
        i["table_name"]: i
        for i in (await (await authed.get("/api/pointers")).json())["items"]
    }
    assert pointers["dw_orders_d"]["pointer_version"] == 2
    assert pointers["dw_orders_d"]["health"] == "针齐"
    # 双针元数据已不存在，读针只剩 live_pointers 一个源
    import sqlite3
    conn = sqlite3.connect(authed.app["db_path"])
    n = conn.execute(
        "SELECT COUNT(*) FROM meta WHERE key IN ('ptr_check','ptr_apply')"
    ).fetchone()[0]
    conn.close()
    assert n == 0
