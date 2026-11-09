# 生产运维手册

这份手册面向「已经把它当日常工作台用」的场景：数据放哪、怎么备份、怎么升级、
跑久了怎么清理、出问题先看什么。

## 1. 数据布局

默认 `<home>` 是项目根目录（`multiagent serve` 的当前目录），私有数据在
`<home>/.multiagent/`：

| 路径 | 内容 | 是否可删 |
| --- | --- | --- |
| `multiagent.sqlite3` | 项目/任务/run/黑板/审批/事件链 | **不可删**，用备份恢复 |
| `multiagent.sqlite3-wal` / `-shm` | SQLite WAL 运行时文件 | 进程退出后可删，不建议手删 |
| `run_events`（库内表） | Agent 实时事件（已合并；默认保留 14 天 / 每项目 5 万条） | `maintenance --apply` 按策略裁剪 |
| `runtime/<project>/tasks/<task>/work` | 任务隔离工作副本 | 终态任务可按保留策略清理 |
| `runtime/.../runs/<run>/` | prompt/stdout/stderr/transcript/现场归档 | 可按保留策略裁剪 |
| `blobs/` | 改动前后内容（回滚/对比） | 未被引用且过宽限期可回收 |
| `snapshots/` | 版本快照全量副本 | 可按 `keep_snapshots` 清理 |
| `backups/` | 数据库一致性备份 + manifest | 可按 `keep_backups` 清理 |
| `logs/multiagent.log` | JSON Lines 结构化日志（10MB × 5 轮转） | 可轮转/归档 |
| `token` | 远程/严格模式 API token | 不要提交到版本库 |

## 2. 备份与恢复

### 创建备份

```powershell
.\.venv\Scripts\python.exe -m multiagent backup --label before-upgrade
```

备份使用 SQLite 在线备份 API（WAL 下也一致），完成后会做 `quick_check` 并写
`backup-*.json` manifest（schema 版本、字节数、sha256、表计数）。

### 从备份恢复（热恢复）

```powershell
# 先确认没有其他进程在跑任务；有则先停止
.\.venv\Scripts\python.exe -m multiagent restore .multiagent\backups\backup-xxxx.sqlite3
```

恢复前会自动再备份当前库（`pre-restore-*.sqlite3`），然后用 SQLite backup
写回并重开连接；服务对象无需重建。若仍有运行中的调用，必须确认后加 `--force`。

### 灾难恢复（进程已无法启动）

1. 停掉所有 `multiagent` / 桌面壳进程；
2. 把当前 `.multiagent/multiagent.sqlite3*` 移走留证；
3. 用 `sqlite3` 或 Python 把备份文件复制为 `multiagent.sqlite3`；
4. 启动 `python -m multiagent doctor`，确认 schema/完整性；
5. 需要时 `python -m multiagent verify --full` 校验事件链。

## 3. 升级与迁移

- 启动时自动读取 `PRAGMA user_version`；老库先做**迁移前自动备份**，再逐版本迁移；
- 迁移失败会抛错并保留备份，不会带着半迁移的库继续服务；
- 迁移记录在 `schema_migrations` 表（版本、时间、说明、备份路径）；
- 不支持降级写入：备份 schema 版本高于程序版本时，`restore` 会拒绝。

升级流程建议：

```powershell
python -m multiagent backup --label pre-upgrade
git pull
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m multiagent doctor
.\.venv\Scripts\python.exe -m pytest -o addopts= -q
```

## 4. 保留清理（GC）

默认 **dry-run**，只报告不删除：

```powershell
python -m multiagent maintenance
python -m multiagent maintenance --apply
```

默认策略：

- 每个任务保留最近 3 次 run 的完整证据；
- 30 天内的 run 证据不裁剪；更老的删除 `workspace/` 归档，stdout/stderr/transcript
  超过 2MB 时保留头尾；
- 终态任务超过 30 天的 `work/` 与 `baseline/` 可删除（Git worktree 除外，见下节）；
- 已合并且超过 14 天的 Git worktree 注销注册并删除目录，任务分支/提交保留；
- 每个项目保留最近 20 个快照、20 份备份；
- 未被 `file_changes` / `artifacts` 引用的 blob 超过 24 小时可回收；
- 清理后执行 `VACUUM`（会短暂阻塞写入，建议低峰执行）。

可用参数：`--keep-runs`、`--keep-days`、`--keep-snapshots`、`--keep-backups`、
`--keep-workspaces-days`、`--no-blobs`。控制台「系统维护」页提供同样的预览/执行入口。

### Git worktree 保留与清理

Git worktree 任务（`work/` 是 linked worktree）有独立的保留策略：

- `git.keep_worktree`（默认 `true`）：合并成功后是否保留 worktree 目录。
  默认保留现场；设为 `false` 时 Runner 在合并成功当次就注销并删除目录。
  无论取值如何，**任务分支 `ma/...` 与提交永远不会被删除**。
- `git.worktree_retention_days`（默认 `14`）：`maintenance` 清理已合并
  worktree 的保留天数。CLI 与 API 目前都不单独暴露该参数，
  `ops.prune(prune_worktrees_days=None)` 会直接读取 policy 中的值。

清理条件必须全部满足（保守策略，宁可少删）：

1. 任务是终态（success/failed/skipped/cancelled/blocked/interrupted）；
   running/waiting/pending 永不处理；
2. `finished_at`（缺失时看 `updated_at`）早于保留期；
3. `task.result.git.merge.merged == true`——**未合并的 worktree 是未审阅成果，
   再老也不会清理**（计入 `skipped_unmerged`）；
4. 目录真实存在，且是已登记的 worktree（`.git` 为文件或在
   `git worktree list` 中）。

清理动作是 `git worktree remove --force` + `git worktree prune`：只删工作目录，
不删任务分支与提交，之后仍可从 `ma/<task-id>-<slug>` 分支重新检出。copy/shared
模式的 `work/` 不归这一步管，仍由 `prune_workspaces_days`（CLI
`--keep-workspaces-days`）负责；该步骤会显式跳过 linked worktree，避免只删目录
留下陈旧注册，也避免误删未合并现场。

人工触发（默认 dry-run）：

```powershell
python -m multiagent maintenance          # 预览：报告 worktrees.removed / kept / skipped_unmerged
python -m multiagent maintenance --apply  # 真正执行
```

控制台「设置 → 数据 → 保留清理」的预览/执行按钮走同一入口。报告中的
`worktrees` 字段为：

```json
{"removed": [{"task_id": "...", "path": "...", "branch": "ma/..."}], "kept": 0, "skipped_unmerged": 0}
```

调整保留期请修改 `config/policy.json` 的 `git.worktree_retention_days`。

**不会删**：任务、run 行、事件、审批、黑板条目——审计记录始终保留。

## 5. 完整性校验

```powershell
python -m multiagent verify          # quick_check + 每条事件链
python -m multiagent verify --full   # integrity_check（较慢）
```

事件链按 `project_id` 独立（`NULL` 是全局运维链），哈希算法为
`sha256(prev_hash + canonical(event))`。任何一条被篡改/截断都会返回非零退出码。

## 6. 可观测性

### 结构化日志

`<home>/.multiagent/logs/multiagent.log` 是 JSON Lines，每条带
`ts/level/logger/message`，以及当前上下文的 `request_id/project_id/task_id/run_id/agent_id`。
API 响应头带 `X-Request-ID`，可与日志串联。控制台「系统维护」页可看最近 120 行。

环境变量：

- `MULTIAGENT_LOG_LEVEL=DEBUG|INFO|WARNING|ERROR`
- `MULTIAGENT_LOG_CONSOLE=1` 同时把 WARNING 以上输出到 stderr

### Prometheus 指标

`GET /api/metrics`（与其他 `/api` 接口同样的 Host/Origin/token 策略）：

- `multiagent_tasks{project_id,status}`
- `multiagent_runs_total{agent_id,status}`
- `multiagent_run_duration_seconds_sum/count{agent_id}`
- `multiagent_agent_usage{agent_id,metric}`
- `multiagent_approvals_pending{project_id}`
- `multiagent_scheduler_slots{project_id}`
- `multiagent_db_bytes` / `multiagent_runtime_bytes` / `multiagent_blob_bytes`
- `multiagent_ws_clients` / `multiagent_stream_pending`
- `multiagent_runs_stalled{kind="heartbeat|activity"}`：运行中但 60s 无心跳 / 30 分钟无产出的调用数

### 健康检查

- `GET /api/health`：快速存活 + 版本 + schema；
- `GET /api/health?deep=1`：`integrity_check`、磁盘剩余空间、调度槽位、后台调度状态；
- 深度健康检查返回 `runs.running/stalled/silent`：`stalled` = 60s 无心跳（可能卡死），
  `silent` = 30 分钟无产出（模型可能长时间思考）；`stalled > 0` 时 `ok=false`；
- `GET /api/projects/{pid}/run-events?after=&limit=&run_id=`：跨进程实时事件回放，
  CLI/桌面进程跑的任务也能在浏览器看板「实时」页看到；
- 磁盘剩余低于 512MB 时 `ok=false` 并给出 warning。

## 7. 工作区卫生与临时产物

快照、diff、copy、commit、archive 共用同一套排除规则：`.edge-tmp*`、`edge-profile`、
`__pycache__`、`*.pyc`、`.cache`、`.tmp*`、`*.log`、`node_modules`、`.multiagent`、
`build`、`dist`、`.git` 等不会进入交付物。

项目可在根目录放 `.multiagentignore` 追加规则：

```text
# 每行一个 glob
tmp/**
*.generated
```

也可以把全局追加规则写进 `config/policy.json::workspace.excludes`。

临时产物不会静默消失。任务收尾会做一次卫生检查，写进任务 `result.hygiene` 并产生
`workspace.hygiene` 事件；手工检查：

```powershell
python -m multiagent project hygiene --project mytest
curl http://127.0.0.1:8770/api/projects/proj-xxxx/hygiene
```

发现 `.edge-tmp`、`build/edge-profile`、`__pycache__` 时报告为非零退出，提醒先清理
再最终验收。

## 8. 跨进程并发

`scheduler_slots` 表保证 CLI、API、桌面后端同时调度时仍遵守
`concurrency.global` / `concurrency.per_project`：

- 槽位带 `expires_at`，引擎心跳每 5s 续租；
- 进程崩溃后心跳停止，其他进程在 TTL 内回收槽位；
- `recover` 也会清理失联会话的槽位。

## 8. 安全清单（生产必做）

- 不要把 `multiagent serve` 直接暴露公网；默认只绑 `127.0.0.1`；
- 远程访问必须 `--strict-auth` 或非 loopback 绑定，使用 Bearer token；
- token 文件权限 0600，不要提交到版本库；
- 反向代理必须保留/校验 `Host`，并配置 TLS；
- `MULTIAGENT_FS_ROOTS` 收窄可浏览/导入目录；
- 定期 `backup` + `verify`，并把备份复制到另一块磁盘；
- 不要把 `.multiagent`、`config`、`.git` 放进 Agent 的可写范围。

## 9. 常见故障

| 现象 | 先看 | 处理 |
| --- | --- | --- |
| 任务一直 `running` | `python -m multiagent verify`、`doctor`、日志 `run.start/run.finish` | `task resume <project>` 保守恢复；确认进程已死后 `--force` |
| 项目停止后历史任务被改 | 已在 2026-11 修复：只取消 running | 升级后 `verify` 历史状态 |
| 磁盘持续增长 | `python -m multiagent storage` | `maintenance` 预览后 `--apply` |
| 事件链校验失败 | `verify` 输出 | 从最近备份恢复；保留现场用于排查 |
| 配置保存后 Agent 不生效 | `doctor` 的 config_errors | 修正 JSON；适配器缓存会在保存时清空 |
| 端口被占用 | `netstat -ano | findstr 8770` | 换 `--port` 或结束旧进程 |
