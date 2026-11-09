# 需求实现对照表（需求.txt 全 57 条）

> 每条给出「实现位置」与「验收方式」。测试文件在 `tests/`，运行：`.\.venv\Scripts\python.exe -m pytest -q`
> 端到端演示：`python scripts\demo.py`

**本表不是自述式声明**：`scripts/verify_requirements.py` 会把下面每一条引用的测试
**真的跑一遍**，并生成 [`REQUIREMENTS-EVIDENCE.md`](REQUIREMENTS-EVIDENCE.md)（逐条列出
「引用是否存在 / 实测是否通过」）。引用了不存在的测试、或引用的测试失败，脚本都会以非零码退出——
这条流水线在引入当天就抓到了一处真实的文档漂移（第 35、44 条把测试文件路径写错了）。

真实 Agent 端到端（真跑 Codex / Claude Code / OpenCode / DSH）不在本表内，
见 [`VERIFICATION.md`](VERIFICATION.md) 的第 3 层。

约定：`agents/` = `multiagent/agents/`，其余同理。

---

## 一、Agent 接入与调度

| # | 要求 | 实现位置 | 验收 |
| --- | --- | --- | --- |
| 1 | 同时接入 Codex、Claude Code、OpenCode、DSH，视为可调度执行单元 | `config/agents.json`（4 条内置 Agent）· `agents/acp_adapter.py` | `multiagent agent check` 四家 ACP 握手全部 `ok`；`tests/test_core.py::test_config_defaults_and_materialize` |
| 2 | 同一工作区多 Agent 协同 | `orchestrator/engine.py`（线程池调度）· `orchestrator/runner.py` | `tests/test_runner.py::test_parallel_run_project`（同项目 5 任务并行） |
| 3 | 每 Agent 独立配置：路径/参数/工作目录/超时/额度 | `config/agents.json` 每条 Agent 的 `command`/`args`/`cwd`/`timeout_sec`/`quota`；`config.py::Config.agents` | `multiagent agent quota`；`tests/test_runner.py::test_quota_exceeded` |
| 4 | 支持新增任意 CLI 型 Agent，不改本体 | `agents/cli_adapter.py` + `agents/resolve.py`（命令/参数/提示词投递方式全配置化） | `tests/test_runner.py` 全部用例使用注入的假 CLI Agent |
| 5 | 派发时指定 Agent；也支持按规则自动选择 | `orchestrator/router.py` · `config/routing.json` · 控制台「路由预览」`GET /api/tasks/{id}/route` | `tests/test_runner.py::test_default_router_can_pick_explicit` / `test_router_prefers_kind_order`、`tests/test_api_control.py::test_task_move_and_route` |
| 6 | 同一任务分派多个 Agent，保留各自结果 | `orchestrator/engine.py::Engine.fanout` | `tests/test_runner.py::test_fanout_keeps_all_results` |
| 7 | 不预设工作类型（编码/文档/调研/数据/审阅…） | `tasks/service.py`（`kind` 自由）· `config/templates.json` · `config/policy.json::kinds` · 控制台类型为自由输入（下拉建议取自 `policy.kinds`） | `tests/test_tasks.py::test_template_applies_defaults`、`tests/test_api_control.py::test_run_output_and_config_kinds` |

## 二、工作区与项目

| # | 要求 | 实现位置 | 验收 |
| --- | --- | --- | --- |
| 8 | 创建/管理项目·工作区 | `workspace/projects.py::ProjectService` · `multiagent project create` | `tests/test_workspace.py::test_project_create_and_import` |
| 9 | 任意类型文件（源码/MD/Office/图片/数据） | 文件树按哈希管理，二进制自动识别：`workspace/changes.py`、`util.is_binary_path` | `tests/test_workspace.py::test_diff_trees_detects_all_actions` |
| 10 | 多项目并存且相互隔离 | `projects` 表 + 每个项目独立 `root_path`，DB 按 `project_id` 过滤 | `tests/test_workspace.py::test_project_create_and_import` |
| 11 | 工作区版本快照，可回退、可对比 | `workspace/snapshots.py` · `multiagent project snapshot/restore/diff` · 控制台支持「对比当前」与「两两对比」`GET /api/snapshots/{a}/diff/{b}` | `tests/test_workspace.py::test_snapshot_restore_and_diff`、`tests/test_api_control.py::test_snapshot_two_way_diff` |
| 12 | 导入已有目录为工作区，不重组文件 | `workspace/projects.py::import_dir`（`origin_path` 记录来源） | `tests/test_workspace.py::test_project_create_and_import` |

## 三、任务与编排

| # | 要求 | 实现位置 | 验收 |
| --- | --- | --- | --- |
| 13 | 大目标拆子任务，维护依赖关系 | `tasks/service.py`（`tasks` + `task_edges`）· `task add-plan` | `tests/test_tasks.py::test_dependencies_and_ready` / `test_add_plan_file` |
| 14 | 串行 + 并行执行 | `tasks/service.py::plan`（分层）· `engine.py::run_project`（就绪即派发） | `tests/test_runner.py::test_parallel_run_project` |
| 15 | 通用验收条件 | `tasks/acceptance.py`：`file_exists`/`file_modified`/`content_contains`/`command_exit_zero`/`no_writes`/`manual_confirm` 等 | `tests/test_runner.py::test_acceptance_failure_when_no_change`、`tests/test_tasks.py` |
| 16 | 重试 / 跳过 / 人工接管 / 动态调整顺序 | `engine.py::retry/skip/cancel/takeover` · `update(priority/ordering)` · 控制台「上移/下移」`POST /api/tasks/{id}/move` | `tests/test_runner.py::test_retry_skip_cancel`、`tests/test_api_control.py::test_task_move_and_route` |
| 17 | 任务模板复用输入输出约定 | `config/templates.json` · `config.py::Config.template` | `tests/test_tasks.py::test_template_applies_defaults` |
| 18 | 输入 / 输出产物（文件 / 片段 / 结构化记录） | `tasks/service.py::record_artifact`（`fragment`/`record` 字段）· `engine` 自动登记改动文件 · 控制台「项目产物」视图 `GET /api/projects/{id}/artifacts` | `tests/test_tasks.py::test_artifact_recording`、`tests/test_api_control.py::test_project_artifacts_and_recover` |

## 四、共享黑板与记忆

| # | 要求 | 实现位置 | 验收 |
| --- | --- | --- | --- |
| 19 | 项目级共享存储区（所有 Agent 可读写） | `board/service.py` · MCP 之外的统一 `board_*` 服务 | `tests/test_board.py` |
| 20 | 按类型区分（简报/产物/决策/交接/待办/问题/参考） | `board/service.py::EntryType` | `tests/test_board.py::test_post_and_get`、`test_handoff_posts_context` |
| 21 | 任一 Agent 写入，后续 Agent 读到最新版 | `runner.py` 每次执行前重新装配上下文（`orchestrator/context.py`） | `tests/test_runner.py::test_context_injection_between_tasks` |
| 22 | 版本化 + 追溯谁/哪个任务/何时写入 | `board_versions` 表 + `events` 哈希链 · 控制台「版本历史」`GET /api/board/{id}` | `tests/test_board.py::test_versioning_and_edit`、`test_events_hash_chain` |
| 23 | 关键记忆审核机制（未确认不进入长期记忆） | `board/service.py::_default_status` + `review()` | `tests/test_board.py::test_review_gate_blocks_project_memory` |
| 24 | 按任务/Agent/时间/关键词检索 | `board/service.py::search`、`search_events` · 控制台支持关键词 + 类型 + 起止时间检索 | `tests/test_board.py::test_search_by_keyword_task_agent` |
| 25 | 记忆持久、重启不丢、不串项目 | SQLite + WAL（`db.py`），全表按 `project_id` 隔离 | `tests/test_core.py::test_config_defaults_and_materialize`、API 重启复用同一 DB |
| 26 | 记忆分层：项目长期 / 任务临时 / Agent 私有 | `board/service.py::MemoryScope`（`project`/`task`/`agent`） | `tests/test_board.py::test_scope_layering` |

## 五、上下文交接

| # | 要求 | 实现位置 | 验收 |
| --- | --- | --- | --- |
| 27 | 任务结束产出结构化交接（做了什么/没做/遗留/下一步） | `board/handoff.py`（`.multiagent/handoff.json` 约定） | `tests/test_runner.py::test_run_success_cli`、`tests/test_board.py::test_parse_handoff_from_text` |
| 28 | 派发新任务时自动注入交接/简报/黑板条目 | `orchestrator/context.py::build_context` | `tests/test_runner.py::test_context_injection_between_tasks` |
| 29 | 控制注入体量（预算） | `context.py` 按 `policy.context_budget_chars` 截断 | `tests/test_core.py`（truncate）+ `context.py` 预算字段 |
| 30 | 人工可编辑修正交接，修正版成为权威上下文 | `board/handoff.py::edit`（`source=human`） | `tests/test_board.py::test_handoff_edit_becomes_authoritative` |
| 31 | 跨类型交接（调研笔记 → 报告） | 交接内容为自由文本 + 产物列表，不限代码 | `scripts/demo.py`（调研 → 撰写 → 审阅） |

## 六、隔离与冲突控制

| # | 要求 | 实现位置 | 验收 |
| --- | --- | --- | --- |
| 32 | 并发修改互不覆盖、不静默冲突 | 隔离工作副本 + `changes.py::ChangeJournal.conflicts` | `tests/test_runner.py::test_conflict_blocks_auto_merge` |
| 33 | 为不同任务分配隔离工作副本/目录 | `workspace/isolation.py::prepare`（`shared`/`copy`/`worktree`） | `tests/test_workspace.py::test_isolation_copy_and_commit` |
| 34 | 非文本文件并发改动冲突识别，识别不了阻止自动合并 | `util.is_binary_path` 标记二进制；冲突即阻止合并 `policy.isolation.block_conflict`；差异查看对二进制只回大小 `GET /api/tasks/{id}/diff` | `tests/test_runner.py::test_conflict_blocks_auto_merge` |
| 35 | 限制单 Agent 可写范围 | `task.write_scope` + `changes.py::scope_check` + 越权还原 | `tests/test_workspace.py::test_write_scope_blocks_and_reverts`、`tests/test_workspace_hardening.py`（符号链接 / `..` / 大小写 / 前缀混淆绕过） |
| 36 | 记录每 Agent 对每文件的改动，支持按任务回滚 | `file_changes` 表 + `ChangeJournal.revert/revert_task`（blob 存原文） | `tests/test_workspace.py::test_change_journal_revert`、`tests/test_runner.py`（revert 接口） |
| 37 | 只读协作（仅分析评审、不写入） | `task.read_only` 强制 `copy` 隔离，`prepare()` 真只读 | `tests/test_runner.py::test_read_only_review_blocks_writes` |

## 七、执行与状态管理

| # | 要求 | 实现位置 | 验收 |
| --- | --- | --- | --- |
| 38 | 完整任务状态机 | `tasks/service.py::TaskStatus`（待执行/执行中/成功/失败/阻塞/取消/等待人工/跳过/中断） | `tests/test_tasks.py::test_progress_counters` |
| 39 | 任意时刻查询项目进度 | `tasks/service.py::progress` · `multiagent task status` · `/api/projects/{id}/progress` | `tests/test_runner.py`、`tests/test_api.py::test_project_and_task_flow` |
| 40 | 中断与恢复，现场保留可续跑 | `engine.py::recover` + `resume`；工作副本与日志保留 · **重试时恢复上次写范围内半成品** `runner._restore_partial_attempt` · 控制台「恢复」`POST /api/projects/{id}/recover` | `tests/test_runner.py::test_recover_marks_interrupted`、`test_resume_reruns_interrupted`、`test_partial_workspace_resumed_on_retry`、`tests/test_api_control.py::test_project_artifacts_and_recover` |
| 41 | 并发上限控制 | `policy.concurrency.global/per_project` → `engine.run_project` 限流 · 调度 `max_seconds` 到点会向在跑调用发取消信号（硬上限） · 控制台「启动调度」可现场设最大并发 | `tests/test_runner.py::test_parallel_run_project`（上限 2/4 下 5 任务全绿）、`test_run_project_deadline_is_hard_bound` |

## 八、失败处理与安全

| # | 要求 | 实现位置 | 验收 |
| --- | --- | --- | --- |
| 42 | 不信任自我声明，只看退出码/产物/验收 | `runner.py::_finalize`（`result.ok and acceptance.ok` 才成功） | `tests/test_runner.py::test_exit_code_failure_even_with_artifacts` |
| 43 | 失败保留现场并标记原因 | 每次运行落盘 `prompt.txt`/`stdout.txt`/`stderr.txt`/`transcript.json`；`runs.error`；越权/冲突文件还原前归档到 `runs/<run>/out-of-scope|conflicts/`；控制台「输入/输出」`GET /api/runs/{id}/output` | `tests/test_runner.py::test_exit_code_failure_even_with_artifacts`、`tests/test_runner.py::test_protected_path_blocked`、`tests/test_api_control.py::test_run_output_and_config_kinds`、`tests/test_cli.py::test_task_approve_syncs_failed_but_acceptable_run` |
| 44 | 危险操作拦截或二次确认 | `safety/guard.py`：ACP 权限裁决 + 运行期高危命令终止 + 保护路径 + 删除识别 + **删除默认不自动回写** + 人工确认队列 | `tests/test_runner.py::test_protected_path_blocked`、`test_guard_detects_dangerous_commands`、`test_destructive_delete_requires_confirmation`、`tests/test_workspace.py::test_write_scope_blocks_and_reverts` |
| 45 | 每 Agent 调用次数/时长/费用上限，超限停派 | `safety/quota.py`（`agent_usage` 表 + 配置上限）· `runner.resolve_timeout` 把单次超时压到剩余 `max_seconds` | `tests/test_runner.py::test_quota_exceeded`、`test_quota_max_seconds_caps_single_run` |

## 九、可观测与审计

| # | 要求 | 实现位置 | 验收 |
| --- | --- | --- | --- |
| 46 | 完整记录每次调用（输入/输出/耗时/退出码/额度） | `runs` 表 + 运行目录文件 · 控制台「输入/输出」回看 `GET /api/runs/{id}/output` · **跨进程实时事件流** `run_events` 表 + `WS stream.event` + `GET /api/projects/{id}/run-events` | `tests/test_runner.py::test_run_success_acp_with_tokens`、`tests/test_api_control.py::test_run_output_and_config_kinds` / `test_event_hub_buffer_and_snapshot` / `test_runner_emits_agent_stream_events`、`tests/test_run_events.py::test_run_events_persist_and_are_readable_by_another_workbench` |
| 47 | 按项目/Agent/任务汇总成本与耗时 | `audit.py::stats`（`by_agent` + `by_task`）· `multiagent stats` · 控制台「观测」两张汇总表 | `tests/test_runner.py::test_stats_timeline_export` |
| 48 | 可视化任务看板（任务流 + 依赖 + 状态） | `web/`（看板 + SVG DAG）· `/api/projects/{id}/kanban` | `tests/test_api.py::test_project_and_task_flow`、`test_plan_endpoint_and_websocket` |
| 49 | 导出完整时间线用于复盘 | `audit.py::export_timeline` · `multiagent timeline --out` | `tests/test_runner.py::test_stats_timeline_export` |
| 50 | 对比不同 Agent 对同一任务的产出差异 | `audit.py::compare_runs` / `diff_runs_text` · `compare` 命令 | `tests/test_runner.py::test_fanout_keeps_all_results` |

## 十、配置与扩展

| # | 要求 | 实现位置 | 验收 |
| --- | --- | --- | --- |
| 51 | 配置文件描述 Agent/模板/路由/验收，改配置不改代码 | `config/*.json` + `presets.py` 兜底 | `tests/test_core.py::test_config_defaults_and_materialize` |
| 52 | 稳定对外接口（CLI 或 API） | `cli.py`（含 `doctor` 自检）· `api/app.py`（REST + WS）· 桌面端（Electron）· 可 PyInstaller 冻结后端 + electron-builder 打包 | `tests/test_api.py` 全量、`tests/test_cli.py` |
| 53 | 不同工作类型不同默认规则，底层机制统一 | `policy.json::kinds` + `templates.json` · 控制台类型下拉建议即取自 `policy.kinds`，也可自由填写 | `tests/test_tasks.py::test_template_applies_defaults`、`tests/test_api_control.py::test_run_output_and_config_kinds` |

## 十一、人机协作

| # | 要求 | 实现位置 | 验收 |
| --- | --- | --- | --- |
| 54 | 人可在任意环节介入 | CLI/API：改任务、指定 Agent、编辑上下文、批准产物、终止/回滚；跨进程 `task cancel` 哨兵；`task approve --sync` 人工验收 | `tests/test_api.py::test_task_ops_and_changes`、`tests/test_agent_runtime.py::test_cross_process_cancel_sentinel_stops_cli`、`tests/test_orchestration_hardening.py::test_cross_process_cancel_uses_sentinel_file`、`tests/test_orchestration_hardening.py::test_closing_another_workbench_does_not_cancel_running_task`、`tests/test_api_control.py::test_task_approve_and_sync_endpoint` |
| 55 | 需要人工决策时主动提示，不静默卡住 | `safety/guard.py` 审批队列 + 看板红点 + WS 推送 | `tests/test_runner.py::test_acceptance_failure_when_no_change`、`test_quota_exceeded` |
| 56 | 审阅模式（只评审不修改） | `review` 模板 + `read_only` 真只读隔离 | `tests/test_runner.py::test_read_only_review_blocks_writes` |
| 57 | 人作为执行者参与编排 | `agents/human_adapter.py` + `engine.provide` | `tests/test_runner.py::test_human_task_flow` |

---

## 十二、大型文件型项目与工作区卫生

| # | 要求 | 实现位置 | 验收 |
| --- | --- | --- | --- |
| 58 | code/doc 任务默认超时 ≥1800 秒，重试自动递增并封顶 | `tasks/runtime.py` · `config/policy.json::retry` | `tests/test_large_projects.py::test_timeout_budget_escalates_on_retry` |
| 59 | 验收模板覆盖 code/doc/ppt/diagrams/web/review，执行期不能只靠 exit_zero | `tasks/profiles.py` · `config/acceptance.json::kind_templates` | `tests/test_large_projects.py::test_config_has_large_project_defaults_and_templates` |
| 60 | PDF/需求文档/长 brief 可预览九阶段 DAG，预览不创建任务 | `tasks/decompose.py` · `POST /api/projects/{pid}/decompose/preview` | `tests/test_large_projects.py::test_decompose_preview_and_apply_builds_nine_stage_dag` |
| 61 | 修改型任务默认 copy；shared+空范围不能直写工作区 | `tasks/profiles.py::prepare_creation_fields` · `workspace/isolation.py::prepare` | `tests/test_large_projects.py::test_quicktask_sets_timeout_attempts_copy_and_effective_acceptance` |
| 62 | 快照/diff/copy/commit/archive 忽略临时目录，并支持 `.multiagentignore` | `workspace/changes.py::effective_excludes` · `workspace/hygiene.py` | `tests/test_large_projects.py::test_workspace_hygiene_excludes_temp_and_supports_project_ignore` |
| 63 | 失败时保留现场并报告已生成/缺失产物 | `orchestrator/runner.py::_finalize` | `tests/test_large_projects.py::test_runner_reports_missing_outputs_and_keeps_workdir` |

---

## 附：技术选型对照（需求.txt 末尾建议）

| 层面 | 建议 | 本项目 |
| --- | --- | --- |
| 后端语言 | Python | ✅ Python 3.10+ |
| Agent 封装 | 自写薄适配器（AgentShell 模式） | ✅ `agents/`（ACP + CLI 两种薄适配器） |
| 编排框架 | 第一版不用重框架 | ✅ 自研轻量调度（`orchestrator/`），未引入 LangGraph/MAF |
| 状态存储 | SQLite + WAL | ✅ `db.py`（WAL + 追加事件哈希链） |
| 实时推送 | FastAPI + WebSocket | ✅ `api/app.py` |
| 前端看板 | React 19 + Vite + ReactFlow + Tailwind | ✅ **Electron 桌面控制台 + 浏览器看板**（`desktop/` + `multiagent/web/`）；UI 层用零构建原生 JS + SVG DAG（免 React 构建链），DAG/看板/控制能力等价 |
