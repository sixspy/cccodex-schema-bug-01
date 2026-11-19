# 复验纸：读针同源与值班纠偏（schema-bug-01）

## 一、病灶（planted 版）

1. `meta` 表存了两根读针：`ptr_check=1`、`ptr_apply=2`，检查页与升版页各读各的，
   同一张草稿对照到的“现行”不是同一条。
2. 读针指向的版本查不到时，服务旁路再扫一遍 `status='现行'`，脏针被静默掩盖，
   页面看似正常、目录入库版号已拧花。
3. `schema_contracts` 没有“一表一现行”约束，第二现行可以直接落库。
4. 没有值班纠偏入口、没有互斥，两个值班并发改读针会互相覆盖。

## 二、选定落点（两处一起上，不是二选一）

| 落点 | 实现 | 对脏针可读性的影响 |
|------|------|--------------------|
| 落点一：唯一索引拦住第二现行 | 部分唯一索引 `idx_contract_single_live ON schema_contracts(table_name) WHERE status='现行'` | 第二现行写库即 `IntegrityError`，服务转白话 409 |
| 落点二：旁路读针表整事务切换 | 新表 `live_pointers(table_name PRIMARY KEY, live_version)`，检查/升版唯一读路；纠偏在一个 `BEGIN IMMEDIATE` 事务里 UPSERT 读针、对齐目录、入档 `repair_runs`，脏针行被同主键覆盖撤销 | 脏针仍可被指针卡片读到并亮【脏针】，但检查页/升版页一律白话拦下，不再旁路掩盖 |

- 检查读针与升版读针**同源**：都走 `get_live_contract()`，只认 `live_pointers`。
- 老库的 `ptr_check` / `ptr_apply` 在 `init_db` 迁移时整行删除，双针即拦。
- 互斥：`BEGIN IMMEDIATE` 写锁 + `meta.repair_lock` 标记（带 30 秒陈旧接管），
  并发值班一个持锁、另一个白话 409，读针行不会互相覆盖。

## 三、复现步骤（HTTP 手工链路）

```bash
# 1. 起服务
DATABASE_PATH=/tmp/lab_repro.db PORT=7100 python3 run.py &

# 2. 登录
curl -c /tmp/c.txt -X POST http://127.0.0.1:7100/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"123456"}'

# 3. 指针卡片初始：两工程均【针齐】
curl -b /tmp/c.txt http://127.0.0.1:7100/api/pointers

# 4. 制造脏针（读针拧到不存在的 v99）
python3 - <<'PY'
import sqlite3
c = sqlite3.connect("/tmp/lab_repro.db")
c.execute("UPDATE live_pointers SET live_version=99 WHERE table_name='dw_orders_d'")
c.commit(); c.close()
PY

# 5. 四处联查：卡片亮【脏针】；检查页、升版页同一条白话 409；水线亮【脏针】且漂移
curl -b /tmp/c.txt http://127.0.0.1:7100/api/pointers
DRAFT=$(curl -b /tmp/c.txt http://127.0.0.1:7100/api/contracts | \
  python3 -c "import sys,json;print(next(c['id'] for c in json.load(sys.stdin)['items'] if c['status']=='草稿'))")
curl -b /tmp/c.txt -X POST http://127.0.0.1:7100/api/contracts/$DRAFT/breaking-check -d '{}'
curl -b /tmp/c.txt -X POST http://127.0.0.1:7100/api/contracts/$DRAFT/apply -d '{}'
curl -b /tmp/c.txt -X POST http://127.0.0.1:7100/api/reconcile-reports \
  -H 'Content-Type: application/json' -d '{"as_of":"2026-11-19"}'

# 6. 值班点纠偏：整事务切换读针、对齐目录、编排记录入档
curl -b /tmp/c.txt -X POST http://127.0.0.1:7100/api/pointers/repair \
  -H 'Content-Type: application/json' -d '{"table_name":"dw_orders_d"}'
curl -b /tmp/c.txt http://127.0.0.1:7100/api/repair-runs

# 7. 检查与升版各复验一轮：from_version 均为纠偏后的 v1，升版后读针随事务切到 v2
curl -b /tmp/c.txt -X POST http://127.0.0.1:7100/api/contracts/$DRAFT/breaking-check -d '{}'
curl -b /tmp/c.txt -X POST http://127.0.0.1:7100/api/contracts/$DRAFT/apply -d '{}'

# 8. 水线复验：【针齐】、零漂移
curl -b /tmp/c.txt -X POST http://127.0.0.1:7100/api/reconcile-reports \
  -H 'Content-Type: application/json' -d '{"as_of":"2026-11-19"}'
```

### 唯一索引复现

```bash
python3 - <<'PY'
import sqlite3
c = sqlite3.connect("/tmp/lab_repro.db")
try:
    c.execute("INSERT INTO schema_contracts(table_name,version_no,status) "
              "VALUES('dw_payments_d',2,'现行')")
    c.commit()
except sqlite3.IntegrityError as e:
    print("拦住第二现行:", e)   # UNIQUE constraint failed: schema_contracts.table_name
c.close()
PY
```

### 并发互斥复现

```bash
python3 - <<'PY'
import threading
from app.services.pointers import repair_pointer
from app.db import get_db, init_db
init_db("/tmp/lab_race.db")
with get_db("/tmp/lab_race.db") as c:
    c.execute("UPDATE live_pointers SET live_version=88 WHERE table_name='dw_payments_d'")
b = threading.Barrier(2); out = []
def w():
    b.wait()
    try: out.append(("ok", repair_pointer("dw_payments_d", "/tmp/lab_race.db")))
    except Exception as e: out.append(("err", e.status_code, e.message))
ts = [threading.Thread(target=w) for _ in range(2)]
[t.start() for t in ts]; [t.join() for t in ts]
print(out)  # 写锁串行化，最终读针确定为 v1，repair_lock 无残留
PY
```

## 四、自动化复验

```bash
python3 -m pytest -q
# 18 passed：
#   test_baseline.py(2) + test_main_chain.py(1) 原有主链不回归
#   test_pointer_repair.py(11) 脏针/纠偏/同源/唯一索引/并发互斥/水线/老库迁移/双现行恢复
#   test_pointer_api.py(4) HTTP 四处联查
```

## 五、页面落点

- 顶栏新增「读针卡片」页签：每工程一张卡片，显示读针/现行/目录三个版号与健康芯片
  （【针齐】绿、【脏针】/【缺针】/【双现行】红），卡片内「值班纠偏」按钮，下方纠偏编排记录表。
- 「目录对账」页顶部水线芯片与指针卡片用同一套字面量；每条历史报告存 `pointer_health`。
- 检查页/升版页出错信息走顶部 `#msg` 红字白话提示，不再静默旁路。
