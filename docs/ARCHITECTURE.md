# 架构说明

> **v1 / 历史参考（2026-11-06 前后）**：本文记录旧版控制台与旧测试数字，已被 v2 取代。
> 当前实现与验证以 [README](../README.md)、[V2-ARCHITECTURE](V2-ARCHITECTURE.md)、[USAGE-V2](USAGE-V2.md)、[DEFECTS-V2](DEFECTS-V2.md) 为准。
> 文中旧数字（518/530/558、schema 5、1.0.0）仅作历史记录，不代表当前基线。

> **v2 提示（2026-11）**：当前版本已加入 `webapp/` React 控制台、`gitops` worktree 隔离、
> 文件 / Git / 验证 API 与新的执行收尾逻辑。本文保留 v1 的分层、领域模型与历史说明；
> v2 的前端模块、状态层、数据模型、执行生命周期和失败分类见
> [V2-ARCHITECTURE.md](V2-ARCHITECTURE.md)，逐视图操作见 [USAGE-V2.md](USAGE-V2.md)。

## 1. 定位

一层**多 Agent 编排与观测层**：不内置大模型、不打包安全工具，只负责把本机已有的 Agent
组织起来，在同一个工作区上安全、可审计地协作完成文件型工作。

## 2. 分层

```
┌──────────────────────────────────────────────────────────────┐
│ 接口层    CLI (cli.py)          REST + WebSocket (api/app.py) │
│           安全策略 (api/security.py)：Host/Origin/token/路径边界│
│           桌面控制台 (desktop/) / Web 看板 (web/)            │
├──────────────────────────────────────────────────────────────┤
│ 编排层    Engine(调度/并发闸门/重试/恢复/扇出)                  │
│           TaskRunner(单任务执行)  Router(选 Agent)  Context(注入)│
├──────────────────────────────────────────────────────────────┤
│ 领域层    tasks(任务/DAG/状态机/验收/产物)                      │
│           board(黑板/记忆/交接)                                 │
│           workspace(项目/快照/隔离/改动日志/冲突/回滚)          │
│           safety(拦截/审批队列/额度原子预占)                    │
│           audit(调用记录/汇总/时间线/对比)                     │
├──────────────────────────────────────────────────────────────┤
│ 适配层    AcpAdapter / CliAdapter / HumanAdapter / resolve    │
│           proc.py(进程树回收 + 编码回退)                        │
├──────────────────────────────────────────────────────────────┤
│ 存储层    SQLite(WAL) + 事件哈希链 + blob 仓库 + 运行目录       │
│           会话表(runtime_sessions) 支撑崩溃恢复的存活判定        │
└──────────────────────────────────────────────────────────────┘
```

两条容易踩的边界，单独说明：

- **`api/security.py` 不是「锦上添花」**：本机 API 能驱动 Agent 执行任意命令，
  所以「谁能调它」是这套系统最大的真实风险面。默认 loopback 零配置可用，
  但仍强制 `Host` 白名单（挡 DNS rebinding）与 `Origin` 同源校验（挡跨站）。
- **`agents/proc.py` 是平台差异的收口点**：Windows 上 `cmd.exe /c xxx.CMD` 的真实进程树是
  `cmd.exe → node.exe`，只 kill 直接子进程会留下孤儿；子进程输出的编码也不一定是 UTF-8
  （桌面壳带 `PYTHONUTF8=1` 时，中文 Windows 的 `reg.exe` 会输出 GBK）。这两类问题
  统一在这一层处理，上层适配器不再各自处理平台细节。

## 3. 数据模型（SQLite / WAL）

| 表 | 作用 |
| --- | --- |
| `projects` | 项目/工作区（含导入来源） |
| `tasks` / `task_edges` / `task_states` | 任务、依赖、状态历史 |
| `runs` | 每次 Agent 调用（输入/输出/耗时/退出码/额度） |
| `run_events` | Agent 实时事件流（已合并；跨进程回放 / 崩溃后看现场；最终权威仍是 run 的 transcript） |
| `board_entries` / `board_versions` | 黑板条目与版本；`scope` 区分记忆层级 |
| `events` | 追加事件 + 前向哈希链（可验证未被篡改） |
| `handoffs` | 结构化交接说明 |
| `artifacts` | 输入/输出产物（文件/片段/结构化记录） |
| `file_changes` | 文件改动日志（含越权/冲突标记、`mode` 记录回滚） |
| `approvals` | 人工确认队列 |
| `snapshots` | 版本快照清单 |
| `leases` | 资源租约（避免同一资源被并发占用） |
| `agent_usage` | 每 Agent 的调用次数/时长/费用累计 |
| `runtime_sessions` | 进程身份与心跳（崩溃恢复的存活判定） |
| `scheduler_slots` | 跨进程调度槽位（global / per_project 并发上限） |
| `schema_migrations` | 版本化迁移记录（含迁移前备份路径） |

事件哈希链：`hash = sha256(prev_hash + canonical(event))`，`BoardService.verify_chain()` 可校验。

## 4. 一次任务的生命周期

> 注：该行为已在后续轮次变更，见 DEFECTS-V2。

```
确定 Agent（显式 / 路由规则）
   ↓
额度检查（超限 → blocked + 审批项）
   ↓
准备隔离工作副本 prepare()：先复制原目录为 baseline/，再按模式生成工作副本
   ↓
装配上下文 build_context()：任务简报 + 前置交接 + 决策/问题/待办 + 长期记忆（按预算截断）
   ↓
落盘 prompt.txt / context.json，建立 run 记录，任务置 running
   ↓
调用 Agent（ACP 或 CLI），期间按策略裁决权限请求（危险操作 → 拒绝 + 审批）
   ↓
收尾 finalize()：
   diff(baseline, 工作副本) → 记录 file_changes（标注越权/冲突）
   越权/保护路径/删除/冲突 → 还原 + 生成人工确认项
   读取/解析结构化交接 → 写入 handoffs 并同步到黑板
   回收改动（copy 模式，验收通过且无冲突时）
   验收评估（退出码 + 产物 + 条件）
   状态判定（success / failed / blocked / waiting）→ 更新 runs/agent_usage → 写 events
```

**成功率判据**：`result.ok(exit_code==0) && acceptance.ok`。Agent 的交接说明只进入上下文，
不作为成功依据。

## 5. Agent 适配

### ACP（首选，四家通用）

`initialize → session/new → session/prompt`，期间处理服务端发来的：

- `session/update`（消息/思考/工具调用/usage）
- `session/request_permission`（转换为策略裁决 + 人工确认）
- `fs/read_text_file` / `fs/write_text_file`（本实现未声明该能力，Agent 用自带工具；仍兼容处理）
- `session/cancel` / `session/close`

`agent check` 只做 `initialize` 握手，用于确认可用性且不消耗额度。

### 一次性 CLI（扩展任意 Agent）

配置 `cli.command / cli.args / cli.prompt_mode`（`stdin` / `arg` / `file`），
参数支持占位符：`{prompt}` `{prompt_file}` `{workspace}` `{model}`。
Windows 下 `.cmd/.bat` 自动用 `cmd.exe /c` 包一层。

### 人工执行者

`protocol: "human"`：任务置 `waiting` 并生成人工确认项，`engine.provide()` 回填结果后完成。

## 6. 隔离与冲突

> 注：该行为已在后续轮次变更，见 DEFECTS-V2。

| 模式 | 行为 |
| --- | --- |
| `shared` | 直接在工作区原目录操作（只读任务会被强制改为 `copy`） |
| `copy` | 复制一份独立工作副本，改动在验收通过后回收（**默认**） |
| `worktree` | git 仓库时用 `git worktree`，失败退化为 `copy` |

- 无论哪种模式都保留 `baseline/`，用于差异计算与**回滚原文**。
- 写范围（`write_scope`）与保护路径（`.multiagent`/`config`/`.git`）之外的改动会被还原并转人工确认。
- 冲突判定：其他**仍在进行中**的任务对同一文件已有 `committed` 改动 → 阻止自动合并并转人工。

## 7. 上下文预算

`policy.context_budget_chars` 控制注入上限；装配顺序为：任务简报 → 前置交接 →
决策 → 已知问题（同时生成 `do_not` 清单）→ 待办 → 长期记忆。超出预算从尾部截断。

## 8. 可观测

- `runs`：每次调用的完整记录，运行目录另存 `prompt/stdout/stderr/transcript`。
- `stats`：按 Agent / 任务 / 项目的次数、耗时、token、成功率。
- `timeline`：events + runs 合并排序，可导出 Markdown/JSON。
- `compare_runs`：同一任务多 Run 的产物哈希/文本差异（配合 `fanout`）。

## 9. 失败与恢复

- 进程异常退出后 `engine.recover()` 把遗留 `running` 任务标记为 `interrupted`（run 同步标记），
  清理租约；`resume` 重新派发。
- 工作副本、日志、输出全部保留，便于人工接管与回滚。

## 10. 关键取舍

1. **不引入重框架**：调度逻辑自研约 200 行，避免 LangGraph/MAF 的运行时与学习成本。
2. **前端零构建**：UI 用原生 JS + SVG，`pip install` 即可用；桌面端用 Electron 包一层（`desktop/`，只负责拉起后端并开窗 + 原生选目录），牺牲一点生态换取开箱即跑与同一份 UI 复用。
3. **验收可配置但实现统一**：所有工作类型共用同一套验收引擎，差异仅在配置与模板。
4. **审计优先**：任何自动化动作都会写 `events`；人工决策留痕。

## 11. 大型文件型项目

- `tasks/runtime.py`：任务 → Agent → 类型 → policy 的超时解析，以及重试递增与运行剩余时间；
- `tasks/profiles.py`：类型默认值、可复用验收模板、有效验收补齐、产物 glob 状态；
- `tasks/decompose.py`：PDF/需求文档/长 brief → 九阶段 DAG（只生成，不自动启动）；
- `workspace/hygiene.py`：临时产物专门扫描，与快照/diff 的忽略规则分离；
- `workspace/changes.py::effective_excludes`：默认排除 + `.multiagentignore` + policy
  `workspace.excludes`，被快照、隔离副本、diff、commit 与 archive 共用。

详细配置与操作见 [`LARGE-PROJECTS.md`](LARGE-PROJECTS.md)。
