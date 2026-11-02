# 本机 CLI 工作流扩展

本地功能分支，基于当前运行版本 1359888，不是上游同步或全量版本升级。保留现有 Hub 页面、账号状态、引擎、数据库；只在 Hub 内补充两个薄工作流。前端改原 `app/web/app.js`/`index.html`，不增加独立 Web 服务，不需要重新编译 React/Rust。

## API

- `POST /api/publish/drafts`：PublishDraftIn，必须 Idempotency-Key；保存待确认草稿与 Hub 管理的素材快照。
- `GET /api/publish/{id}`：当前状态、审阅版本/摘要、账号名、素材元数据与本机预览地址；不返回 Cookie 或素材路径。
- `GET /api/publish/{id}/media/{index}`：受原本机访问中间件保护的素材预览。
- `POST /api/publish/{id}/approve`：仅 Hub 人工确认入口使用，携带 review_version/review_digest、稳定提交键；记录 RiskAdminAudit。沿用已有管理口令（若设置），不新增认证系统。
- `PUT /api/publish/{id}`：受控草稿必须传 review_version；修改后版本加一、撤销确认、保持 draft。
- `POST /api/sync-requests`：kind=works/comments，显式 account_id；comments 必须 work_id。仅抖音，自有账号和平台作品 ID 绑定。
- `GET /api/sync-requests/{id}`：请求状态，不触发平台访问。
- `GET /api/task-queue?queue_type=syncs` 与原批量停止接口：同一任务中控台。
- `/health` 增加 hub_cli_protocol=1.0、draft_review、controlled_sync_queue。

## 持久化与执行

增量迁移为 PublishTask 添加 review_* / approved_* / media_manifest 字段（默认 review_required=false，不改历史任务）；新增 SyncRequest、SyncPacing、PublishPacing 表。没有第二个数据库。任务与 TaskSubmission 回执同事务提交，CLI 不持有业务状态。版本修改、批准、发布准备、同步 claim 使用 SQLite 写锁串行化；发布只使用已核对的副本。SQLite 执行模型仍遵循原项目单进程 Hub，不新增跨平台执行 worker。

同步复用主应用现有自有作品/评论处理函数，保留账号锁和风险预算。全局 10 分钟、同账号 30 分钟、最多 20 条活动请求。风险暂缓最多 3 次；普通异常/中断不自动补抓。单个请求执行最多 240 秒，超时可能已有部分本地快照，需查看后人工决定。每轮仅 claim 一条且跨重启计时，避免唤醒后补抓高峰。

新审阅发布全局 10 分钟/同账号 30 分钟错峰；错过预定窗口 30 分钟后回到草稿。预约任务不能通过 run-now 提前执行。原有平台级限速、验证、代理、uncertain 逻辑仍优先；这些业务间隔不代表平台安全阈值。原有人工发布/同步路径保持兼容，不声称强制约束同用户权限的任意外部脚本。

## 运维

先隔离测试，不在真实账号做发布/采集验收。上线使用可追溯独立源码 release，保留原数据 cwd/config/Profile 和原源码作回退；停止前确认无运行任务，SQLite online backup + 停止后最终备份均设 600。迁移仅加字段/表；不自动还原旧数据库覆盖新数据。回退旧程序前若已有 review_required 的新任务，应停止/保留新队列并人工核对，不能绕过新审阅保护。

本次不改 V100、飞书机器人、CI、上游同步副本或账号配置。不自动安装依赖，不在本机构建 Rust。
