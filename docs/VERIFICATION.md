# 验收与验证指南

> **v1 / 历史参考（2026-11-06 前后）**：本文记录旧版控制台与旧测试数字，已被 v2 取代。
> 当前实现与验证以 [README](../README.md)、[V2-ARCHITECTURE](V2-ARCHITECTURE.md)、[USAGE-V2](USAGE-V2.md)、[DEFECTS-V2](DEFECTS-V2.md) 为准。
> 文中旧数字（518/530/558、schema 5、1.0.0）仅作历史记录，不代表当前基线。

> 这份文档回答一个问题：**凭什么相信这套系统真的能用？**
> 它只写「怎么跑、跑出什么」的东西，不写「已实现」这类无法验证的声明。

本项目有**三层**互相独立的验收，激进度递增：

| 层 | 命令 | 验证什么 | 依赖真实 Agent | 耗时 |
| --- | --- | --- | --- | --- |
| 1. 单元 / 集成 | `pytest -q` | 每个模块的行为与边界 | 否（假 Agent） | ~1 分钟 |
| 2. 需求可追溯 | `python scripts/verify_requirements.py` | 文档声称的验收方式**真的存在且通过** | 否 | ~1 分钟 |
| 3. 真实端到端 | `python scripts/e2e_real.py ...` | 真跑 Codex / Claude Code / OpenCode / DSH | **是** | 3–6 分钟 |

第 3 层是本项目唯一的「真实证据」来源：前两层用的都是假 Agent，只能证明**编排逻辑**对，
不能证明**真的能驱动四个 Agent 干活**。

## 当前基线（2026-11-08 两个真实项目验证后实测）

| 项 | 结果 |
| --- | --- |
| `pytest` | **518 passed / 10 skipped / 0 failed**（约 145s，RC=0） |
| 真实项目 Atlas（`~/myworld`） | **10/10 任务成功**：Claude 设计 → Codex 后端 → OpenCode 工单 → Claude 前端 → OpenCode 联调 → Codex 测试 → Claude 文档 → Claude 只读审阅 → Codex 修复 → DSH 最终只读验收；最终 `node --test` **29/29**、`node scripts/smoke.mjs` PASS、`node scripts/check.mjs` 25 文件 PASS、`web/dev-check.mjs` 59 项 PASS |
| 真实项目 myworld（上一轮） | 6/6 任务成功（设计→引擎→玩法→测试→只读审阅→修复）；75 项 Node 测试 + Edge 浏览器 smoke 通过 |
| 跨进程实时流 | CLI 调度进程把 Agent 事件写入 `run_events`；另开的看板进程通过 `GET /api/projects/{id}/run-events` 与 WebSocket 正常回放（真实任务验证，非假 Agent） |
| 真实项目暴露并已修复 | 危险命令误报（`atlas.shutdown()` 被当关机）、`.gitignore` 点文件被 glob 误判越权、失败重试不恢复半成品、调度 `max_seconds` 非硬上限、删除操作未二次确认 |
| 失败/超时语义 | 退出码非 0 时**不自动回写**；人工 `task sync` / `task approve --sync` 可恢复现场（新增回归） |
| 跨进程取消 | CLI `task cancel` 通过 `cancel.requested` 哨兵终止另一进程的 Agent；旁观者 `Workbench.close()` 不再误杀（新增回归） |
| 新增实时流/工作副本测试 | `test_stream_coalesce.py` 30 + `test_live_stream.py` 16 + `test_workspace_sync.py` 12 + `test_ui_contract.py` 73 + `test_ui_e2e.py` 14 + `test_encoding_regression.py` 11 + `test_api_quicktask.py` 34 |
| 实时流真实回放 | `.e2e/live-capture.json` 1612 条 raw → 后端合并 196 条 → 前端 19 块；不再一行一个 token |
| 工作副本同步 | `GET /api/tasks/{tid}/workspace` + `POST .../sync`；真实任务 pending=23 / unsynced=0（人工同步后） |
| `verify_requirements.py` | 57 条需求 / 46 处测试引用，**0 缺失 / 0 失败** |
| `verify_upgrade.py` | 真实库副本升级 **PASS**（老数据零变化、新结构就位、迁移幂等） |
| dashboard 首屏聚合 | 100 任务实测 **p50 4.9ms / p95 5.3ms**（预算 150ms） |
| 浏览器冒烟 | 1832×1010 / 1440×900 / 1280×800 共 12 视图 + 空工作台；0 乱码、0 横向溢出、关键 id 有内容；CDP 真实点击建任务 PASS |
| 桌面安装包 | 2026-11-08 11:38 重新构建 `release/`（v1.0.0 NSIS + portable + win-unpacked），冻结后端 `/api/health?deep=1` 实测 200、`version=1.0.0`、`schema=5`（详见 `docs/RELEASE.md`） |
| `e2e_real.py chain` | **10/10 PASS**（opencode→codex→claude→dsh，总耗时 187.9s、141826 tokens、cost 0.327）——上一轮真实 Agent 基线，本轮未重复消耗额度 |
| `e2e_real.py smoke` | codex / opencode / dsh / claude **四家全部成功**（同上） |
| 取消 / 超时后的孤儿进程 | 实测 `new_alive={}`，零残留（含 codex 的 `cmd.exe→node.exe` 树） |

> 本轮（指挥台重构）新增的验收文档：`docs/UI-OPS-REDESIGN.md`（设计/API 契约）、
> `docs/UI-VERIFICATION.md`（截图、DOM 断言、覆盖矩阵）、`docs/UI-REVIEW.md`（设计走查 P0/P1/P2）、
> `docs/DESIGN-REFERENCES.md`（参考项目对照）。
>
> 两个真实项目验收现场都在 `~/myworld`：
> 上一轮是《我的世界》Web 版（6/6 成功，75 项 Node 测试）；
> 本轮是 Atlas 知识库与工单平台（10/10 成功，29 项 Node 测试 + 真实 HTTP/浏览器 QA）。
> Atlas 现场保留在 `~/myworld`，含 `README.md`、`docs/CONTRACT.md`、`docs/DESIGN.md`、
> `docs/API.md`、`docs/QA.md`、`docs/OPERATIONS.md`、源码、测试、Dockerfile 与启动脚本。

> ⚠️ 踩坑提示：`pyproject.toml` 里 `addopts = "-q"`。再在命令行加 `-q` 会变成 **`-qq`**，
> pytest 会**连最后那行汇总一起吞掉**（看起来像「跑完了但什么都没说」）。
> 要看汇总或逐用例输出，用 `pytest -o addopts=`（可再配 `-q` 或 `-v`）。

---

## 第 1 层：单元 / 集成测试

```powershell
cd ~/multi_xuanwo
.\.venv\Scripts\python.exe -m pytest -q
```

覆盖：适配器（ACP / CLI / 人工）、编排（DAG / 并发 / 重试 / 恢复 / 认领）、
工作区（隔离 / 冲突 / 回滚 / 快照）、黑板与记忆、安全（越权 / 危险操作 / 额度）、
审计（成本 / 时间线 / 对比）、API（REST + WebSocket + 鉴权 + 跨站）、
前端与后端契约（`tests/test_web_contract.py`）。

> `pyproject.toml` 里设了 `addopts = "-q"`。需要汇总或逐用例输出时用
> `pytest -o addopts=`（再配 `-q` 或 `-v`）——直接在命令行再加 `-q` 会变成 `-qq`，
> 连汇总行都会被吞掉。

---

## 第 2 层：需求可追溯性

```powershell
.\.venv\Scripts\python.exe scripts\verify_requirements.py
```

它把 `docs/REQUIREMENTS.md` 里的「需求 → 验收方式」映射变成可执行断言：

1. 解析 57 条需求表格，抽出被引用的测试 node id；
2. 把这些 id **真的交给 pytest 跑一遍**——pytest 对不存在的 id 会报
   `ERROR: not found: <id>`，于是「文档引用了不存在的测试」由 pytest 自己判定；
3. 输出 `docs/REQUIREMENTS-EVIDENCE.md`（每条需求 × 引用 × 是否存在 × 实测结果）；
4. 有任何「引用不存在」或「引用但失败」，退出码非 0。

**为什么需要这一层**：文档写「已实现 + 验收方式」是最容易夸大的一环。
本层在引入当天就抓到一处真实缺陷——`docs/REQUIREMENTS.md` 第 35、44 条引用的是
`tests/test_runner.py::test_write_scope_blocks_and_reverts`，而该测试实际位于
`tests/test_workspace.py`。文档写对了「有这个测试」，写错了「在哪个文件」，
人工审阅几乎不可能发现。

---

## 第 3 层：真实 Agent 端到端

会消耗模型额度，所以**不在 `pytest` 里自动跑**，必须显式调用。
所有现场落在 `--home` 指定的目录（默认 `<repo>/.e2e/`），**不污染项目自身的 `.multiagent/`**。

### 3.1 四个 Agent 各自冒烟

```powershell
.\.venv\Scripts\python.exe scripts\e2e_real.py smoke --agents opencode,codex,dsh,claude
```

对每个 Agent：建一个任务 → 让它在隔离工作副本里创建 `notes.txt`（内容含固定标记）→
校验**验收条件通过**、**项目根目录真的出现该文件**、**审计表里该次调用被完整记录**。

### 3.2 跨 Agent 协作链（旗舰场景）

```powershell
.\.venv\Scripts\python.exe scripts\e2e_real.py chain `
    --research opencode --draft codex --review claude --summary dsh
```

四个**不同** Agent 接力完成一次「文件型工作」，逐项断言：

| 断言 | 对应需求 |
| --- | --- |
| 四个任务全部 `success`，验收条件全通过 | 15、42 |
| T2 依赖 T1、T3 依赖 T2、T4 依赖 T3，且**开始时间严格晚于上游结束时间** | 13、14 |
| T2 在**没被告知标记**的情况下，报告里出现 T1 产物中的标记（说明拿到的是真实文件） | 31 |
| T2 的 `prompt.txt` 里含 T1 的产物/交接内容 | 21、28 |
| T3 为只读审阅：审阅前后项目根目录**整树哈希完全一致** | 37、56 |
| 三个产物文件真实存在且内容正确 | 18 |
| 审计表记录每次调用的输入/输出/耗时/退出码 | 46 |

### 3.3 同题多 Agent 扇出

```powershell
.\.venv\Scripts\python.exe scripts\e2e_real.py fanout --agents opencode,codex
```

同一任务派给两个 Agent，断言**两次调用各自有独立的 run 记录**，且**各自的
`prompt.txt / stdout.txt / stderr.txt / transcript.json` 都在磁盘上**（需求 6、46、50）。

> 这条曾经是假的：`runner._finalize()` 之后的扇出循环每轮都调
> `isolation.discard()`，而它会把整个 `task_dir`（含 `runs/`）删掉，
> 于是**前一次尝试的全部证据被物理销毁**——DB 里那条 run 还写着 `success`，
> 磁盘上却什么都不剩。修复后两次尝试的证据都完整保留。

### 3.4 现场在哪

每次调用都在 `<home>/.multiagent/runtime/<项目>/tasks/<任务>/runs/<run>/` 下落盘：
`prompt.txt`（喂给 Agent 的完整提示词）、`context.json`（本次注入的上下文）、
`stdout.txt` / `stderr.txt`、`transcript.json`（ACP 全量事件流）。
`scripts/e2e_real.py` 结束时会打印一份 JSON 证据报告到 `.e2e/report-*.json`。

---

## 安全验证

API 的防护（需求 52 的加固）不靠「文档说安全」，靠 `tests/test_api_security.py`：

- **DNS rebinding**：恶意网页把自己的域名解析到 `127.0.0.1` 后，同源策略失效，
  只有 `Host` 头能识别。测试断言非 loopback `Host` 被拒。
- **跨站驱动本机 Agent**：浏览器对跨站 `POST` / `fetch` / WebSocket 握手**一定**带
  `Origin`，据此拒绝。这是本项目最大的真实风险——本机 API 能驱动 Agent 执行任意命令。
- **`GET /api/health` 必须匿名可用**：Electron 桌面壳在开窗**之前**轮询它，
  此时还没有任何 token。这条被显式断言，防止加固把桌面端打死。
- **同源必须放行**：桌面壳是 `loadURL(http://127.0.0.1:<动态端口>/)` 同源加载，
  端口随机，不能写死。
- **路径穿越**：`/api/fs/list`、`/api/projects/import`、配置文件名、run 输出读取。
- **前端 XSS**：`app.js` 用 `innerHTML` 渲染 Agent 输出等不可信内容，逐处转义。

防护模型（默认与严格模式）见 `docs/USAGE.md`；实现与设计说明见
`multiagent/api/security.py` 顶部的模块文档。

---

## 本轮修复的真实缺陷（都有「复现 → 修复 → 回归」）

以下每一条都是从**实际运行**（真实运行日志、真实 Agent 调用、真实并发压测）中发现的，
不是代码走查的猜测。分四个域，共 30+ 条。

### A. Agent 接入层（`agents/`）

| # | 缺陷 | 影响需求 |
| --- | --- | --- |
| 1 | 进程树不回收：Windows 上 `cmd.exe /c xxx.CMD` 的真实树是 `cmd.exe→node.exe`，只 terminate 直接子进程会留下孙进程孤儿，继续占端口/写文件/烧额度 | 41、45 |
| 2 | 超时不是硬上限：`terminate_tree` 在已确认卡死的超时路径仍先 `wait(grace=3)`，1s 超时实测 3.05s | 41、45 |
| 3 | ACP 成功路径硬编码 `duration_ms=0` → 4 个 Agent 的耗时全记 0，并导致 `max_seconds` 时长上限**永不触发** | 45、46、47 |
| 4 | token 统计三处错：不认 ACP 规范 `used/size`、累计型被反复累加（300→重复计数）、`cost.amount` 从不读取（额度 `max_cost` 失效） | 45、47 |
| 5 | `subprocess.run(text=True)` 未指定 `errors=`：UTF-8 模式下解码 `reg.exe` 的 GBK 输出失败，reader 线程崩溃、`.stdout` 静默变 `None` → `AttributeError` → `GET /api/agents/detail` **500**（真实日志里 35 次） | 1、3、52 |
| 6 | `cmd /c "含空格路径" "含空格参数"` 被拆错（实测 rc=1）——本机 `D:\Program Files\nodejs\...`、`D:\agent\DeepSeek Harness\...` 都会踩 | 1 |
| 7 | `params` 非对象直接 `AttributeError` 带崩整次运行；正常 `end_turn` 后仍误发 `session/cancel`；`prompt_mode=arg` 无占位符时静默丢提示词；CLI 输出 GBK 乱码；`timeout_sec<=0` 被压成 5s | 3、4 |

### B. 编排与持久化（`orchestrator/` `db.py` `tasks/` `safety/`）

| # | 缺陷 | 影响需求 |
| --- | --- | --- |
| 8 | 重复派发：2 个并发 `run_project` × 8 任务 → 适配器被调 **15** 次、7 个任务重复 run（无 claim/lease） | 41 |
| 9 | `recover()` 见到 `status='running'` 就标 interrupted，而 `run_project` 每次都调它 → 第二个调度方一启动就把第一个正在跑的任务判死 | 40 |
| 10 | 崩溃后**立刻重启无法回收现场**：心跳还新鲜的旧会话被判为存活，running 任务最长卡 `stale_seconds`(300s) 才能续跑 | 40 |
| 11 | 配额 check-then-act：24 线程 / 上限 5 → 计数 **24**；调度并发下实际调用 6 次 > 上限 5 | 45 |
| 12 | 并发上限被击穿：`per_project=2` 时两个并发 `run_project` 峰值并发 **4** | 41 |
| 13 | `db.transaction()` 不原子：`execute()` 无条件 commit，RLock 可重入 → 事务形同虚设 | 25、38 |
| 14 | 状态机无护栏：success 可被再次执行、cancelled 可被 retry、执行中被取消的任务最终被写成 success 且改动照样回收 | 38 |
| 15 | 每 Agent `timeout_sec` 是死配置（`create()` 就把 policy 默认值写死进 task，agent 层永远轮不到） | 3 |
| 16 | **`cancel` / `stop` 不真正终止在跑的 Agent**：只改 DB 状态，Agent 继续跑到自然结束或超时（默认 900s）——「终止执行」是假的 | 54、38 |
| 17 | `stop_project` 误用全局 `shutdown()`：停项目 A 会 set 掉项目 B 在跑 run 的取消信号 → **跨项目误杀** | 10 |

### C. 工作区 / 黑板 / 审计

| # | 缺陷 | 影响需求 |
| --- | --- | --- |
| 18 | `is_binary_path` 的 `except UnicodeDecodeError: return False` 使该分支失效：非 UTF-8 且前 8KB 无 NUL 的内容（PDF、纯 0xFF、GBK 文本）被判为「文本」 | 9、34 |
| 19 | 写范围可被 **5 种手法**绕过：目录前缀混淆（`src` vs `srcevil/x`）、Windows 大小写、`..`、UNC/盘符、scope 内符号链接逃逸 | 35 |
| 20 | `engine.fanout()` 每轮调 `isolation.discard()`，连 `runs/` 一起删 → 前一次调用的输入/输出/transcript 被**物理销毁**（DB 里还写着 success） | 6、43、46、50 |
| 21 | `guard.audit_changes` 用 `zip(changes, recorded)` 位置配对；越权项被剪枝后错位，**尾部冲突被静默丢弃** → 冲突文件不会被阻止自动合并 | 34、44 |
| 22 | 回滚不精确：改名后回滚报 failed、同文件多次改动回滚到中间态、blob 缺失静默成功、快照算出的 before_hash 根本不在 blob 仓库 | 36 |
| 23 | 黑板并发编辑丢版本（12 线程 → 3 个版本）、并发发帖破坏事件哈希链；未审核的 note/todo/handoff 直进长期记忆 | 22、23 |
| 24 | `snapshot.restore` 不清改动期间新建的空目录骨架（恢复后状态 ≠ 快照状态） | 11 |
| 25 | 只读任务在 `shared` 隔离下不隔离；`commit_back` 可夹带 `../` 与指向工作区外的符号链接 | 37 |
| 26 | `audit.stats()` 不传 `project_id` 时 `by_task` 的 WHERE 与参数错位，SQLite 直接报错（这是「全部项目汇总」的正常用法） | 47 |

### D. API / 前端安全（`api/` `web/`）

| # | 缺陷 | 影响需求 |
| --- | --- | --- |
| 27 | 任意网页可**跨站 POST 驱动本机 Agent**（建项目/跑任务/写配置），响应虽跨域读不到但副作用已生效 | 52、54 |
| 28 | **DNS rebinding**：`Host: evil.example` 打进本机 API 后浏览器视作同源，Origin 校验失效 | 52 |
| 29 | 跨站 WebSocket 握手被接受 → 恶意页面可实时读取全部 Agent 输出 | 52 |
| 30 | 绑 `0.0.0.0` 时约 90 个端点**零凭据**可用；`GET /api/fs/list` 可枚举 `.multiagent`（token/SQLite/运行现场）；`POST /api/projects/import` 可把私有目录登记成工作区 | 52 |
| 31 | `GET /api/runs/{rid}/output` 直接读 DB 里的绝对路径、`/api/tasks/{tid}/diff` 用不校验的 blob 摘要拼路径 → **行被污染即可读任意文件** | 52 |
| 32 | 5xx 把内部细节与绝对路径回给客户端；错误体三种形状；缺 CSP/XFO/nosniff | 52 |
| 33 | 前端 XSS：30 处 `innerHTML`、374 处插值逐处审计（真实可利用 0 处，不可信字段均已 `esc()`），另修 1 处 `openModal` 标题多转义显示缺陷，并补**永久静态护栏**（漏写转义会直接让测试变红） | 52 |

> 说明：第 27–32 条在修复前**没有 git 快照**可回滚对照（baseline 提交晚于这批改动落盘），
> 因此改用**永久化的 mutation 自检**固化「修复前失败」：关掉任一防护，对应漏洞立刻复现
> （如 `test_mutation_without_origin_guard` 关掉 Origin 校验后跨站 POST 回到 200）。
> 这比一次性的「修复前截图」更强——它可以在任何未来时刻重跑。

### E. 真实项目驱动发现（2026-11-07，用 ~/myworld 跑《我的世界》时暴露）

| # | 缺陷 | 影响需求 | 回归 |
| --- | --- | --- | --- |
| 34 | CLI `--json` 前移逻辑是死循环（`remove` 后又 `insert` 回原处），任何带 `--json` 的命令永久挂起 | 52 | `test_cli.py::test_global_json_flag_can_appear_anywhere` |
| 35 | 任务超时/失败但验收通过时仍自动回写工作区，半成品会静默覆盖用户文件 | 42、43 | `test_runner.py::test_exit_code_failure_even_with_artifacts` |
| 36 | 跨进程取消只 set 内存 Event：CLI `task cancel` 无法终止另一个进程里的 Agent | 54 | `test_agent_runtime.py::test_cross_process_cancel_sentinel_stops_cli`、`test_orchestration_hardening.py::test_cross_process_cancel_uses_sentinel_file` |
| 37 | 任意短命 CLI 命令（`handoff list` / `task status`）退出时 `shutdown()` 按 DB 的 running 写哨兵，**误杀其他进程的任务** | 10、40、54 | `test_orchestration_hardening.py::test_closing_another_workbench_does_not_cancel_running_task` |
| 38 | 任务重试成功后，旧 run 的 open_issues 仍以 approved 注入后续任务，形成误导性「不要重复尝试」清单 | 23、25 | `test_board.py::test_new_handoff_supersedes_stale_issues` |
| 39 | 越权/冲突文件被还原前直接物理删除，Agent 的真实劳动成果无法人工取回 | 35、43 | `test_runner.py::test_protected_path_blocked`（断言 run/out-of-scope 留证） |
| 40 | 失败但产物/验收合格的任务没有 CLI 人工验收路径，只能改库；桌面/API 也没有 approve | 43、54 | `test_cli.py::test_task_approve_syncs_failed_but_acceptable_run` |

> 这一轮不是代码走查：`~/myworld` 上真实跑了 Claude 设计 → Codex 引擎 → OpenCode 玩法，
> 期间真实超时、真实误取消、真实旁观者进程误杀都被复现并固化成回归测试。

---

## 明确的已知边界（不打算修，或需要环境）

- **纯 ASCII 且后缀未知的二进制**：既无 NUL 又能按 UTF-8 解码的文件，若后缀不在
  已知二进制表中，会被当作文本。已知格式（`.pdf/.docx/.xlsx/...`）已由后缀表覆盖。
- **非 UTF-8 的文本（GBK/Latin-1）按二进制处理**：这是刻意保守——本项目只做 UTF-8
  行级 diff 与自动合并，无法安全逐行比对的内容按需求 34「识别不了就阻止自动合并并转人工」。
- **`worktree` 隔离**：需要工作区本身是 git 仓库，否则退化为 `copy`。
- **人工确认任务不限时**：`human` Agent 的 `timeout_sec: 0` 解释为「不限时」（需求 57）。
- **`projects.delete()` 不走 `purge_task()`**：它自己 `rmtree` 掉该项目整个 runtime 目录，
  语义已完整；不额外引入第二条等价删除路径，避免两处逻辑需要同步维护。
- **打包产物**（`desktop/release/`、`build/`）不入版本库，见 `.gitignore`。
