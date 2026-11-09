# 需求验收证据表（由 scripts/verify_requirements.py 生成）

> 本表由脚本从 `docs/REQUIREMENTS.md` 抽取，并把被引用的测试**真的跑一遍**核对，
> 不是人工填写的声明。

> 需求条数：63　被引用测试：52　引用不存在：0　被引用但失败：0

| # | 需求 | 验收引用 | 存在 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | 同时接入 Codex、Claude Code、OpenCode、DSH，视为可调度执行单元 | `tests/test_core.py::test_config_defaults_and_materialize` | 是 | **通过** |
| 2 | 同一工作区多 Agent 协同 | `tests/test_runner.py::test_parallel_run_project` | 是 | **通过** |
| 3 | 每 Agent 独立配置：路径/参数/工作目录/超时/额度 | `tests/test_runner.py::test_quota_exceeded` | 是 | **通过** |
| 4 | 支持新增任意 CLI 型 Agent，不改本体 | `tests/test_runner.py` 全部用例使用注入的假 CLI Agent | — | 命令/人工核验 |
| 5 | 派发时指定 Agent；也支持按规则自动选择 | `tests/test_api_control.py::test_task_move_and_route` | 是 | **通过** |
| 5 | 派发时指定 Agent；也支持按规则自动选择 | `tests/test_runner.py::test_default_router_can_pick_explicit` | 是 | **通过** |
| 6 | 同一任务分派多个 Agent，保留各自结果 | `tests/test_runner.py::test_fanout_keeps_all_results` | 是 | **通过** |
| 7 | 不预设工作类型（编码/文档/调研/数据/审阅…） | `tests/test_api_control.py::test_run_output_and_config_kinds` | 是 | **通过** |
| 7 | 不预设工作类型（编码/文档/调研/数据/审阅…） | `tests/test_tasks.py::test_template_applies_defaults` | 是 | **通过** |
| 8 | 创建/管理项目·工作区 | `tests/test_workspace.py::test_project_create_and_import` | 是 | **通过** |
| 9 | 任意类型文件（源码/MD/Office/图片/数据） | `tests/test_workspace.py::test_diff_trees_detects_all_actions` | 是 | **通过** |
| 10 | 多项目并存且相互隔离 | `tests/test_workspace.py::test_project_create_and_import` | 是 | **通过** |
| 11 | 工作区版本快照，可回退、可对比 | `tests/test_api_control.py::test_snapshot_two_way_diff` | 是 | **通过** |
| 11 | 工作区版本快照，可回退、可对比 | `tests/test_workspace.py::test_snapshot_restore_and_diff` | 是 | **通过** |
| 12 | 导入已有目录为工作区，不重组文件 | `tests/test_workspace.py::test_project_create_and_import` | 是 | **通过** |
| 13 | 大目标拆子任务，维护依赖关系 | `tests/test_tasks.py::test_dependencies_and_ready` | 是 | **通过** |
| 14 | 串行 + 并行执行 | `tests/test_runner.py::test_parallel_run_project` | 是 | **通过** |
| 15 | 通用验收条件 | `tests/test_runner.py::test_acceptance_failure_when_no_change` | 是 | **通过** |
| 16 | 重试 / 跳过 / 人工接管 / 动态调整顺序 | `tests/test_api_control.py::test_task_move_and_route` | 是 | **通过** |
| 16 | 重试 / 跳过 / 人工接管 / 动态调整顺序 | `tests/test_runner.py::test_retry_skip_cancel` | 是 | **通过** |
| 17 | 任务模板复用输入输出约定 | `tests/test_tasks.py::test_template_applies_defaults` | 是 | **通过** |
| 18 | 输入 / 输出产物（文件 / 片段 / 结构化记录） | `tests/test_api_control.py::test_project_artifacts_and_recover` | 是 | **通过** |
| 18 | 输入 / 输出产物（文件 / 片段 / 结构化记录） | `tests/test_tasks.py::test_artifact_recording` | 是 | **通过** |
| 19 | 项目级共享存储区（所有 Agent 可读写） | `tests/test_board.py` | — | 命令/人工核验 |
| 20 | 按类型区分（简报/产物/决策/交接/待办/问题/参考） | `tests/test_board.py::test_post_and_get` | 是 | **通过** |
| 21 | 任一 Agent 写入，后续 Agent 读到最新版 | `tests/test_runner.py::test_context_injection_between_tasks` | 是 | **通过** |
| 22 | 版本化 + 追溯谁/哪个任务/何时写入 | `tests/test_board.py::test_versioning_and_edit` | 是 | **通过** |
| 23 | 关键记忆审核机制（未确认不进入长期记忆） | `tests/test_board.py::test_review_gate_blocks_project_memory` | 是 | **通过** |
| 24 | 按任务/Agent/时间/关键词检索 | `tests/test_board.py::test_search_by_keyword_task_agent` | 是 | **通过** |
| 25 | 记忆持久、重启不丢、不串项目 | `tests/test_core.py::test_config_defaults_and_materialize` | 是 | **通过** |
| 26 | 记忆分层：项目长期 / 任务临时 / Agent 私有 | `tests/test_board.py::test_scope_layering` | 是 | **通过** |
| 27 | 任务结束产出结构化交接（做了什么/没做/遗留/下一步） | `tests/test_board.py::test_parse_handoff_from_text` | 是 | **通过** |
| 27 | 任务结束产出结构化交接（做了什么/没做/遗留/下一步） | `tests/test_runner.py::test_run_success_cli` | 是 | **通过** |
| 28 | 派发新任务时自动注入交接/简报/黑板条目 | `tests/test_runner.py::test_context_injection_between_tasks` | 是 | **通过** |
| 29 | 控制注入体量（预算） | `tests/test_core.py`（truncate）+ `context.py` 预算字段 | — | 命令/人工核验 |
| 30 | 人工可编辑修正交接，修正版成为权威上下文 | `tests/test_board.py::test_handoff_edit_becomes_authoritative` | 是 | **通过** |
| 31 | 跨类型交接（调研笔记 → 报告） | `scripts/demo.py`（调研 → 撰写 → 审阅） | — | 命令/人工核验 |
| 32 | 并发修改互不覆盖、不静默冲突 | `tests/test_runner.py::test_conflict_blocks_auto_merge` | 是 | **通过** |
| 33 | 为不同任务分配隔离工作副本/目录 | `tests/test_workspace.py::test_isolation_copy_and_commit` | 是 | **通过** |
| 34 | 非文本文件并发改动冲突识别，识别不了阻止自动合并 | `tests/test_runner.py::test_conflict_blocks_auto_merge` | 是 | **通过** |
| 35 | 限制单 Agent 可写范围 | `tests/test_workspace.py::test_write_scope_blocks_and_reverts` | 是 | **通过** |
| 36 | 记录每 Agent 对每文件的改动，支持按任务回滚 | `tests/test_workspace.py::test_change_journal_revert` | 是 | **通过** |
| 37 | 只读协作（仅分析评审、不写入） | `tests/test_runner.py::test_read_only_review_blocks_writes` | 是 | **通过** |
| 38 | 完整任务状态机 | `tests/test_tasks.py::test_progress_counters` | 是 | **通过** |
| 39 | 任意时刻查询项目进度 | `tests/test_api.py::test_project_and_task_flow` | 是 | **通过** |
| 40 | 中断与恢复，现场保留可续跑 | `tests/test_api_control.py::test_project_artifacts_and_recover` | 是 | **通过** |
| 40 | 中断与恢复，现场保留可续跑 | `tests/test_runner.py::test_recover_marks_interrupted` | 是 | **通过** |
| 41 | 并发上限控制 | `tests/test_runner.py::test_parallel_run_project` | 是 | **通过** |
| 42 | 不信任自我声明，只看退出码/产物/验收 | `tests/test_runner.py::test_exit_code_failure_even_with_artifacts` | 是 | **通过** |
| 43 | 失败保留现场并标记原因 | `tests/test_api_control.py::test_run_output_and_config_kinds` | 是 | **通过** |
| 43 | 失败保留现场并标记原因 | `tests/test_cli.py::test_task_approve_syncs_failed_but_acceptable_run` | 是 | **通过** |
| 43 | 失败保留现场并标记原因 | `tests/test_runner.py::test_exit_code_failure_even_with_artifacts` | 是 | **通过** |
| 43 | 失败保留现场并标记原因 | `tests/test_runner.py::test_protected_path_blocked` | 是 | **通过** |
| 44 | 危险操作拦截或二次确认 | `tests/test_runner.py::test_protected_path_blocked` | 是 | **通过** |
| 44 | 危险操作拦截或二次确认 | `tests/test_workspace.py::test_write_scope_blocks_and_reverts` | 是 | **通过** |
| 45 | 每 Agent 调用次数/时长/费用上限，超限停派 | `tests/test_runner.py::test_quota_exceeded` | 是 | **通过** |
| 46 | 完整记录每次调用（输入/输出/耗时/退出码/额度） | `tests/test_api_control.py::test_run_output_and_config_kinds` | 是 | **通过** |
| 46 | 完整记录每次调用（输入/输出/耗时/退出码/额度） | `tests/test_run_events.py::test_run_events_persist_and_are_readable_by_another_workbench` | 是 | **通过** |
| 46 | 完整记录每次调用（输入/输出/耗时/退出码/额度） | `tests/test_runner.py::test_run_success_acp_with_tokens` | 是 | **通过** |
| 47 | 按项目/Agent/任务汇总成本与耗时 | `tests/test_runner.py::test_stats_timeline_export` | 是 | **通过** |
| 48 | 可视化任务看板（任务流 + 依赖 + 状态） | `tests/test_api.py::test_project_and_task_flow` | 是 | **通过** |
| 49 | 导出完整时间线用于复盘 | `tests/test_runner.py::test_stats_timeline_export` | 是 | **通过** |
| 50 | 对比不同 Agent 对同一任务的产出差异 | `tests/test_runner.py::test_fanout_keeps_all_results` | 是 | **通过** |
| 51 | 配置文件描述 Agent/模板/路由/验收，改配置不改代码 | `tests/test_core.py::test_config_defaults_and_materialize` | 是 | **通过** |
| 52 | 稳定对外接口（CLI 或 API） | `tests/test_api.py` 全量、`tests/test_cli.py` | — | 命令/人工核验 |
| 53 | 不同工作类型不同默认规则，底层机制统一 | `tests/test_api_control.py::test_run_output_and_config_kinds` | 是 | **通过** |
| 53 | 不同工作类型不同默认规则，底层机制统一 | `tests/test_tasks.py::test_template_applies_defaults` | 是 | **通过** |
| 54 | 人可在任意环节介入 | `tests/test_agent_runtime.py::test_cross_process_cancel_sentinel_stops_cli` | 是 | **通过** |
| 54 | 人可在任意环节介入 | `tests/test_api.py::test_task_ops_and_changes` | 是 | **通过** |
| 54 | 人可在任意环节介入 | `tests/test_api_control.py::test_task_approve_and_sync_endpoint` | 是 | **通过** |
| 54 | 人可在任意环节介入 | `tests/test_orchestration_hardening.py::test_closing_another_workbench_does_not_cancel_running_task` | 是 | **通过** |
| 54 | 人可在任意环节介入 | `tests/test_orchestration_hardening.py::test_cross_process_cancel_uses_sentinel_file` | 是 | **通过** |
| 55 | 需要人工决策时主动提示，不静默卡住 | `tests/test_runner.py::test_acceptance_failure_when_no_change` | 是 | **通过** |
| 56 | 审阅模式（只评审不修改） | `tests/test_runner.py::test_read_only_review_blocks_writes` | 是 | **通过** |
| 57 | 人作为执行者参与编排 | `tests/test_runner.py::test_human_task_flow` | 是 | **通过** |
| 58 | code/doc 任务默认超时 ≥1800 秒，重试自动递增并封顶 | `tests/test_large_projects.py::test_timeout_budget_escalates_on_retry` | 是 | **通过** |
| 59 | 验收模板覆盖 code/doc/ppt/diagrams/web/review，执行期不能只靠 exit_zer | `tests/test_large_projects.py::test_config_has_large_project_defaults_and_templates` | 是 | **通过** |
| 60 | PDF/需求文档/长 brief 可预览九阶段 DAG，预览不创建任务 | `tests/test_large_projects.py::test_decompose_preview_and_apply_builds_nine_stage_dag` | 是 | **通过** |
| 61 | 修改型任务默认 copy；shared+空范围不能直写工作区 | `tests/test_large_projects.py::test_quicktask_sets_timeout_attempts_copy_and_effective_acceptance` | 是 | **通过** |
| 62 | 快照/diff/copy/commit/archive 忽略临时目录，并支持 `.multiagentignor | `tests/test_large_projects.py::test_workspace_hygiene_excludes_temp_and_supports_project_ignore` | 是 | **通过** |
| 63 | 失败时保留现场并报告已生成/缺失产物 | `tests/test_large_projects.py::test_runner_reports_missing_outputs_and_keeps_workdir` | 是 | **通过** |
