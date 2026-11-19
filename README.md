# 源到达SLA

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

多源到达时钟对照 SLA 分级，超限则冻结或降级输出，补齐后恢复并对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 源到达SLA |
| 目录 | `06-lagboard` |
| 领域建议 | 数据采集、爬虫与 RPA |
| 接口 | http://127.0.0.1:10821 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

源时钟 vs 摄入时钟、分级处罚、降级可读不可写、补齐解冻

## 避开

别做成纯监控大盘；须有写路径与状态机

## 等级升级（告警升违约连坐改态）

口径与封存档见 [`docs/等级升级说明_封存.md`](docs/等级升级说明_封存.md)（2026-11-19 定稿封存，不许改口）。

- 唯一口径：既有到达 `告警 -> 违约` 升级，同事务连坐源输出态 `降级只读 -> 冻结`，并写升级专册；严禁只改徽章颜色、严禁弄丢到达主键。
- 定稿 **甲路径**：违约等级与源输出态、账本同库一次提交，遇错全部撤回（正式零改）；乙路径不采用，两路不混用。
- 入口挂在前端「滞后分级」页（策略附页），不另起升级台；口令沿用本仓 Bearer 登录；库表落 `data/app.db`（新增 `upgrade_ledger`、`upgrade_locks`）。
- 双路同点同一源：先占升级锁，后交只记 `等级升级争用`（409）；偏离说明记 `升级偏了` 并驳回。
- 五处同态：到达详情、源目录、策略附页、水位卡片、审计列表（升级审计页）。
- 接口：`POST /api/arrivals/{id}/upgrade`、`GET /api/upgrade-ledger`。

## 互斥切口候选

- 超SLA冻结 vs 降级只读
- 补齐整源解冻 vs 按业务日窗口解冻
- 只认源时钟 vs 双时钟取严

## 启动（一键）

`powershell
cd projects/06-lagboard
docker compose up -d --build
`

浏览器：http://127.0.0.1:10821/  
健康检查：GET http://127.0.0.1:10821/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10603
python -m pytest -q
`
