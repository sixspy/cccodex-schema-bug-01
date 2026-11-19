# cc-schema-lab

CC_Codex 二期（26.11）**数据工程**产品线：**契约换版台**（aiohttp + 原生 JS SPA + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**表契约版本登记 → 破坏性检查 → 换版应用 → 目录对账**

与现有九线正交（不是采集、ETL、同步、解析、编排、摄取、留存、清单、水印）。
不碰外仓常见指纹（Feed 归并 / CDC 游标 / 质检签发 / Slicegate 类仓名）。

## 领域

- **所属领域**：数据工程
- **子方向**：数仓与数据湖（表契约 / 换版闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、aiohttp、SQLite |
| 前端 | 原生 JS SPA（全中文），暖杏纸面 + 墨绿强调；顶栏胶囊导航 |
| 测试 | pytest + pytest-aiohttp（本地秒级闭环，优先于 compose） |

## 基线已通主链

1. 表契约版本（表名 / 版本号 / 现行或草稿）
2. 对候选字段做破坏性检查（删列 / 改类型）
3. 换版应用（草稿升现行，旧现行停用）
4. 契约与目录表对账

## 读针同源与值班纠偏（schema-bug-01 修复）

- **单一读路**：检查页与升版页共用 `live_pointers` 一根读针（`get_live_contract`），
  老的 `ptr_check` / `ptr_apply` 双针迁移时整行撤销；脏针 / 缺针一律白话 409，不再旁路扫现行掩盖。
- **落点一硬约束**：部分唯一索引 `idx_contract_single_live` 保证一表只许一条现行，
  第二现行写库即被拒；老库已双现行时索引降级为待修标记，纠偏后补建。
- **落点二整事务切换**：值班纠偏在 `BEGIN IMMEDIATE` 事务内 UPSERT 读针、对齐目录版号、
  写 `repair_runs` 编排记录；写锁 + `repair_lock`（30 秒陈旧接管）双互斥，并发不互相覆盖。
- **四处联查**：契约列表、检查详情、读针卡片、对账水线共用 `针齐/脏针/缺针/双现行` 标记；
  对账报告持久化 `pointer_health`。
- 复验与复现步骤见 [REPRODUCTION.md](REPRODUCTION.md)。

## 本地

```powershell
cd claude-workspace\cc-schema-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:7100/login · **admin / 123456**

## Docker

端口 **7100**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
