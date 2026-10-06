# 匿名统计交付 · 总额度账本（Quota Ledger）

研究机构向协作方发布匿名统计时的数据官账本：以**稳定发布标识**提交摘要与消耗，
本端维护**总额度账本**，接收端持久化**首份摘要与冻结回执**。交付页面持续展示
可用额度、冻结额度、发布阶段与接收方回执。

仅依赖 **Python 3.11 标准库**（`http.server` + `sqlite3`），无需第三方包。

## 规格到实现

| 规格要求 | 实现 |
| --- | --- |
| 总额度 10，提交消耗 6 → 一份回执、可用 4 | `Store.reserve` 原子冻结，`LedgerService.submit` 两阶段提交，HTTP `POST /api/submit` |
| 完成后再提交消耗 5 必须拒绝，接收端不新增回执 | 额度校验在插入同事务内完成；不足返回 `402`，不留记录、不投递接收端 |
| 接收后断电：接收端接受、本端记账前退出 | `crash_after_receive=true`，`receiver.deliver` 落盘后 `os._exit(1)`（硬退出） |
| 重开按发布标识查询冻结回执，收敛为原发布完成，接收端交付次数仍为 1 | 启动 `AUTO_RECOVER=1` → `Store.reconcile`：delivered 直接采用接收端首份回执；accepted 补投递（接收端按标识幂等）后提交 |
| 相同标识相同载荷重传 → 原回执、不扣额 | `reserve`/`deliver` 按主键命中即返回首份记录 |
| 改动摘要或额度 → 冲突 | 载荷不一致抛 `ConflictError`，HTTP `409` |
| 并发相同提交 / 重复投递 / 恢复重试 → 一条记录、一份回执、一次冻结 | SQLite `BEGIN IMMEDIATE` 写锁 + 主键 + 条件更新（`delivered_count` 仅 accepted→delivered 转移时 +1） |
| 已完成与未终结保留额之和 ≤ 总额度 | 冻结与提交同一事务；页面/接口实时展示该不变量 |
| Compose 在可配置宿主端口提供交付页面与健康响应 | `docker compose up`，宿主端口由 `HOST_PORT`（默认 8080）配置，`GET /` 与 `GET /healthz` |
| verify 先构建页面并检查，再冒烟复核且同时跑代码测试，退出码报告 | `verify/run_verify.py`：build →（smoke ∥ unittest），全部完成退出 |

## 本地运行（无需 Docker）

```bash
python3 -m app.server                 # 默认 :8080，总额度 10，数据在 /data
TOTAL_QUOTA=10 DATA_DIR=./data PORT=8080 python3 -m app.server
```

- 交付页面： http://localhost:8080/
- 健康响应： http://localhost:8080/healthz
- 状态快照： http://localhost:8080/api/state

## Compose 编排

```bash
# 在可配置宿主端口启动交付服务（默认 8080）
HOST_PORT=9090 docker compose up app

# 构建并运行 verify（先构建页面检查，再 API/HTTP 冒烟复核，同时跑代码测试）
docker compose build
docker compose run --rm verify        # 退出码 0 即全部通过
# 或编排启动（app 健康后自动运行 verify 并退出）：
docker compose up --build
```

`verify` 内的断电恢复演练在其**自有临时库**中真实地 `os._exit` 并以新进程重开，
对编排中运行的 `app` 只做只读健康/页面探活（`BASE_URL`），不改动演示数据。

## API

- `POST /api/submit`
  ```json
  {"publication_id": "pub-001", "digest": "sha256:…", "amount": 6,
   "crash_after_receive": false}
  ```
  - `200` 完成（含 `publication.receipt_id` 与最新 `state`）
  - `402` 额度不足（被拒绝，不产生任何记录/回执/冻结）
  - `409` 同一发布标识摘要或额度冲突
- `GET /api/state`：额度（total/available/frozen/reserved/committed）、发布记录、接收端交付
- `GET /api/publication?publication_id=...`：按标识查询（含冻结回执）
- `POST /api/recover`：手动触发收敛

## 测试

```bash
python3 verify/run_verify.py          # 构建 + 冒烟复核 + unittest
python3 -m unittest discover -s tests # 仅代码测试
```
