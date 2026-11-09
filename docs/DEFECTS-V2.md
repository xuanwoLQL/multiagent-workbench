# MultiAgent v2 缺陷台账（P0-1..P2-18）

> 范围：v2 硬化与文档对齐（含真实浏览器、默认策略收口、最终打包、Atlas 并发/冲突
> 回归与批次 D4 最终回填）。
> 状态只使用三档：**已修复 / 部分修复 / 未修**。依据是当前工作树代码、静态契约测试与
> 最终重试闸门轮全量复跑（**764 passed / 10 skipped / 0 failed**，171.72s；
> `.e2e/pytest-final4.log`；测试夹具关闭 `processes.cim_fallback` 以提速）。
> 每条缺陷的验证命令可在对应小节复核。
>
> 编号来源：`tests/test_git_hardening.py`、`test_isolation_modes.py`、
> `test_git_merge_api.py`、`test_agent_cost_model.py`、`test_acceptance_flexible.py`、
> `test_newtask_dialog_contract.py`、`test_task_merge_ui_contract.py`、
> `test_process_audit.py`、`test_ops_worktrees.py`、`test_e2e_behavior.py` 等测试
> 文件头部的缺陷引用。

## 状态汇总

### 主路径（P0-1..P2-18）

| 状态 | 数量 | 条目 |
| --- | --- | --- |
| 已修复 | 18 | P0-1..P0-5、P1-6..P1-12、P2-13..P2-18 |
| 部分修复 | 0 | — |
| 未修 | 0 | —（仅主路径；已知残留见下表） |

> 上表只统计 18 条主路径，不代表“无已知残留”。6 条已知残留项单列如下，其中
> **5 条已修复、1 条未修（安装包未签名）**；本文不把主路径修复扩大解释为全部边界闭合。

### 已知残留项

| 残留项 | 当前状态 | 证据 / 说明 |
| --- | --- | --- |
| `missing_branch`：任务分支被外部删除 | **已修复** | 结构化 409（`reason/code=missing_branch`）；冲突/缺失分支后 task 从 `success` → `blocked`（`merge_conflict` / `merge_missing_branch`），成功 retry/merge/rebuild 后恢复 `success`。`test_git_merge_api.py` **21 passed**（新增 workdir 缺失优先、branch 记录缺失不先 commit、merge 阶段 ref 消失映射、merge_status、ref_unavailable 禁止 HEAD 假成功、worktree 重建失败回滚、标题变更仍用记录分支、create_worktree 拒绝已存在目录 TOCTOU），`test_task_merge_ui_contract.py` 9 passed；v2 flows 第 8 条 `missing-branch` 通过，总 182/182。 |
| CIM 跨平台 | **已修复** | 4 个原 Windows-only `SystemProber` 测试改为 monkeypatch `IS_WIN` 跨平台运行；新增 6 个 CIM 解析/空结果/非 Windows 不调用测试。保留 PID 复用不回收与“只告警不误杀”边界。 |
| P2-2 `merge_*` 重试闸门 | **已修复** | `engine.MergeRetryBlocked` 已接入：`/retry` 对 `merge_*` blocked 返回 **409**；`OverviewTab` / `TaskDetailView` 隐藏普通重试并提示“请先处理合并冲突或重建分支”，同时保留“重试合并 / 重建分支 / 放弃任务”入口。`tests/test_retry_merge_gate.py` + task UI contract **15 passed**。 |
| 安装包未签名 | **未修** | setup / portable / `MultiAgent.exe` / 后端 exe 均为 `NotSigned`，仍可能触发 SmartScreen / 未知发布者提示，见 RELEASE §5。 |
| v1 文档旧数字 | **已修复（仅标记）** | ARCHITECTURE / USAGE / VERIFICATION / UI-V2-QA 头部已统一为“v1 / 历史参考”，正文旧数字仅作历史参考，不逐条改写。 |
| Atlas 并发/冲突覆盖 | **已修复** | 场景 A 原始 **32/33**，唯一 FAIL 为冲突后 task2 `status=success`；同一 `task-c718c57e` 修复后定向复验：merge 409 → `status=blocked` / `blocked_reason=merge_conflict`。场景 B 依赖闸门 **21/21**。不是 33/33 全量重跑。 |

第二轮实际执行 41 个针对性 pytest 用例清单；其中第 40 条原 node id 已陈旧，实际可收集
40 个并全部通过，文末已更正。这些用例覆盖
P0-1/P0-2/P0-3/P0-4/P0-5、P1-6/P1-7/P1-8/P1-9/P1-10/P1-11/P1-12、P2-13/P2-14/
P2-15/P2-17/P2-18 的关键路径；P2-16 已在第三轮收口。第三轮曾新增真实浏览器 E2E
（7 flows / 139 checks，历史数字），默认策略/文档契约与审查补丁测试；批次 D4 最终
主线全量为 **764 passed / 10 skipped / 0 failed**，并完成 8 flows / 182 checks、
3 次安装包验证与最终包回填。

## 主线最终验证（2026-11-09，最终重试闸门轮）

| 检查 | 命令 / 依据 | 结果 |
| --- | --- | --- |
| Python 全量 | `.venv\Scripts\python.exe -m pytest -o addopts= -q` | **764 passed / 10 skipped / 0 failed**（171.72s；`.e2e/pytest-final4.log`） |
| 真实浏览器 E2E | `.venv\Scripts\python.exe scripts\v2_flows.py` | **8/8 flow，182/182 checks PASS**；12 张截图（含 `missing-branch-before.png`、`missing-branch-after.png`）；0 uncaught / 0 console error；1 条预期 409 network warning（`task-abe3db42`）；`.e2e/flows-final3.log` |
| UI 冒烟 | `.venv\Scripts\python.exe scripts\v2_smoke.py --port 8771` | **67/67 PASS**（`.e2e/smoke-final3.log`） |
| missing_branch 定向 | `tests\test_git_merge_api.py` | **21 passed**；覆盖 missing_branch 409、竞态、rebuild 确认/已存在/worktree 缺失，以及 workdir 缺失、ref 消失、merge_status、HEAD 假成功、重建回滚、TOCTOU 等边界 |
| P2-2 重试闸门 | `tests\test_retry_merge_gate.py` + task UI contract | **15 passed**；`/retry` 对 `merge_*` blocked 返回 409，普通重试入口隐藏，保留“重试合并 / 重建分支 / 放弃任务”入口 |
| 安装包验证 | `verify_package.ps1` × 3 | **3/3 exitCode=0**；backendHealth=true、gracefulClose=true、forcedKill=false；main/backend/electron 残留 none；portStillListening=false、portBindOk=true；exitReason=`clean_graceful_close`（`.e2e/verify-final3-run1.log`..`run3.log`） |
| NSIS 安装/卸载 | `.e2e/nsis-install/` | 静默安装/卸载通过；health **2.0.0 / schema 6**；安装文件 SHA 与当次 win-unpacked 一致；卸载后无进程/端口，残留空目录已清理 |
| 520 文件性能 | `.e2e/perf-520-ui/report.md` | prepare worktree **1.327s**、run **3.196s**、merge **0.678s**；UI 页面加载 **1.046s**、任务详情 **1.691s**、console error **0** |
| 代码签名检查 | `desktop\tools\sign_release.ps1 -CheckOnly -Json` | **exit 2 / NOT READY: no certificate**；setup / portable / 主 exe / 后端 exe 4 个产物均 **NotSigned** |
| 前端构建产物 | 当前 `multiagent/web/dist` | 入口 `index-D23LbL2h.js` 262,962 B / `D402B323B916FBDF1465AB8EE36825236FB2A0D8C9D2DE8E09F1E5AD9435290B`；`index.html` 561 B；dist 40 文件 / 827,677 B |
| Atlas 并发/冲突 | `.e2e/atlas-concurrency/a|b` | 场景 A 原始 32/33，唯一 FAIL 为 task2 `status=success`；修复后同一 `task-c718c57e` 再 merge 409 → `status=blocked` / `blocked_reason=merge_conflict`；场景 B 21/21 |
| 最终安装包 | `desktop/release/`（2026-11-09 10:47 构建） | setup 128,074,777 B / `97486F6689362189B1A0554E67643BE25349F9692FD93517CE6797F9C476F4B3`；portable 127,934,507 B / `5DEE13C532286AE7303ED70F705B9E8DC50C3AD270936FA3485AD2098BED4034`；后端 exe 6,215,907 B / `DEB49332DD956B2F69241BEC7BFD51109487FBB826C6C76756D45A1EF6D1ED8F`；**未签名（NotSigned）** |

> 第二轮基线 558 passed / 10 skipped；第二阶段定向基线 679 / 10 / 0；第三轮历史全量
> 737 / 10 / 0。批次 D4、最终硬化轮与重试闸门轮在其后新增 missing_branch、CIM、
> merge retry gate 用例，最终为 **764 passed / 10 skipped / 0 failed**（171.72s）。

## 第三轮验证证据（2026-11-08，历史记录）

> 本节保留第三轮时点的 **737 passed / 139 checks / 7 flows / 10 张截图 /
> 2026-11-08 23:01–23:02 安装包**，均已由批次 D4 最终数字取代，仅用于追溯，不作为
> 当前状态。

| 项 | 证据 | 结果 |
| --- | --- | --- |
| 真实浏览器 E2E | `scripts/v2_flows.py` 驱动真实 Edge + 临时后端 + 临时 Git 仓库，7 条流程：task-merged、merge-conflict、extend-timeout、settings-delivery、review-states、new-task-advanced、duplicate-projects | **139/139 checks PASS**；10 张截图；0 uncaught / 0 console error；唯一 network warning 为 conflict 流程预期 409 |
| waiting 状态 | Git 任务主按钮为「验收并合并」：先 `POST /merge` 成功，再 `POST /approve {sync:false}` 标记验收；merge 409 时任务保持 waiting，可重试/放弃 | `tests/test_task_merge_ui_contract.py::test_waiting_git_task_approves_then_merges_instead_of_file_sync` 等契约测试通过 |
| DAG 下游闸门 | 先审后合下，前置 success 但未合并时下游保持 pending；人工合并后自动恢复 ready；由 `git.require_merged_for_dependents`（默认 true）控制 | `tests/test_policy_merge_defaults.py::test_unmerged_predecessor_blocks_dependent_until_merge` 通过 |
| 默认策略 | `config/policy.json` 与 `multiagent/presets.py` 的 `git.auto_merge` 均为 `false`（先审后合），`require_merged_for_dependents` 默认为 true；README/USAGE-V2/V2-ARCHITECTURE 已同步 | `tests/test_docs_policy_defaults.py` 守护 |
| 重复项目 UI/流程 | 历史重复项目横幅、可折叠列表、「仅显示重复项目」筛选、成员直接打开；不删除历史数据 | 第 7 条流程通过 |
| CIM 兜底 | Windows `finish` 阶段用 `Get-NetTCPConnection -State Listen` 补齐 netstat 漏报；`processes.cim_fallback` 默认为 true；CIM 失败只降级 warning，保留 netstat 回收能力；成功空结果不降级 | 进程审计测试通过 |
| 进程审计收口 | PID 复用（创建时间早于本次调用）不回收；`shared` 模式默认不自动回收；`Workbench.close()` 有界等待异步审计线程；`finish` 异步执行不阻塞任务返回 | `test_process_audit.py::test_pid_reuse_with_old_creation_time_is_not_reaped`、`test_git_hardening.py::test_shared_mode_does_not_reap_processes`、`test_runner_processes.py::test_slow_audit_does_not_block_run_return` 通过 |
| Atlas 真实回归 | 修复后 round-2：`task-de523674` / `run-3a87fc09`，commit `35768060a9ffd09c4d4a7053541696bae5eec80b`，merge `242069f90cb2ff1dc3e260bad77badbf23dcfc48`；Atlas `npm test` 48 backend + 24 frontend；脏树阻断 6/6 | 全部 PASS，详见 `docs/ATLAS-REAL-REGRESSION.md` |
| 第三轮安装包（历史） | 2026-11-08 23:01–23:02 重建；包内前端入口 `index-DhRunDb7.js` | setup 128,065,496 B / `E6FC5D902B8506B7C4148000B8922CA197EAD3343847F1CE1A10A8D4AB519048`；portable 127,925,233 B / `6C179B5B84EEBEDEC8B656B00AB73E7C9548E17D8A8047E1307C838467A5D6F0`；后端 exe 6,208,820 B / `44FF6C7B234F25736BA25DA72D2A60C404B5E98C3636AD4FA2E6A16BC913E30A`；**未签名（NotSigned）** |

---


### 第三轮独立审查补丁（2026-11-08）

对抗性审查发现并已修复的额外问题：

- **DAG 下游闸门**：默认先审后合时，前置任务 success 但未合并，下游不再被派发
  （`git.require_merged_for_dependents=true`）；人工/自动合并后自动恢复 ready。
  该闸门只约束默认 `success` 边，`any` / `always` / `soft` 边不受影响。
- **字符串布尔**：`policy_bool()` 统一识别 `"false"/"0"/"off"`，避免
  `bool("false")=True` 让 `auto_merge="false"` 仍自动合并。
- **stale git**：重试/降级到 copy 时清空上一次尝试的 `result.git`，避免 UI 合并旧分支。
- **result 原子更新**：进程审计与人工合并改用 `tasks.set_result_key()`（SQLite JSON1
  条件更新），不会互相覆盖 `git`/`processes`。
- **退出等待**：`Workbench.close()` 有界等待异步审计线程（CLI/桌面退出不丢结果）。
- **PID 复用/shared**：创建时间早于本次调用的新增 pid 不回收；shared 模式默认不自动
  回收进程（只记录）。
- **审计线程失败隔离**：线程创建/回填异常只 warning，不会把成功任务拖成 failed。
- **CIM 空结果**：`Get-NetTCPConnection` 成功但零监听视为成功空结果；新增
  `processes.cim_fallback` 开关（默认为 true，测试套件关闭以提速）；CIM 失败只降级
  warning，netstat 回收能力保持不变。
- **waiting 顺序**：`验收并合并` 改为「先 merge，成功后再 approve {sync:false}」，
  409 不再留下 success+未合并，任务保持 waiting 可重试/放弃。
- **浏览器 E2E 网络噪声**：`v2_flows.py` 只允许冲突流程的 409 `/merge`，其它网络错误
  计为失败。

第三轮时点残留（历史记录；第四轮收口结果见下节）：

1. 分支被外部删除时 `/merge` 仍返回文本 409（非结构化 `missing_branch`），前端只
   toast；面板/按钮仍可能指向必然失败的合并。建议后续补结构化响应与终态（P0-3）。
2. Windows CIM 行为测试在非 Windows CI 上跳过（`skipif(os.name != "nt")`），跨平台
   覆盖仍不足（P1-7）。
3. 安装包未签名（setup / portable / `MultiAgent.exe` / 后端 exe 均为 NotSigned），
   可能触发 SmartScreen / 未知发布者提示（P2-16、RELEASE §5）。
4. 历史 v1 文档仅头部标记，正文保留旧描述/旧数字，不逐条重写（P2-16）。
5. Atlas 真实回归只覆盖 1 个文档任务的成功链路与脏树阻断，不含并发/冲突/超大仓库
   （P2-17）。

> 截至批次 D4：第 1、2、4、5 条已修复/收口；仅第 3 条“安装包未签名”仍未修。

### 第四轮独立复核与最终重试闸门复跑（2026-11-08 夜至 2026-11-09）

证据：A1/A2/A3 只读复核报告（`.e2e/recheck-final/a1/report.md`、`a2/report.md`、
`a3/report.md`）与 Atlas 并发场景（`.e2e/atlas-concurrency/a/report.md`、
`b/report.md`）；最终回填另用 `.e2e/pytest-final4.log`、`smoke-final3.log`、
`flows-final3.log`、`verify-final3-run1.log`..`run3.log`。以下数字均为实测，不把局部
证据扩展成全局结论。

#### 数字与证据链

| 项 | 实测事实 |
| --- | --- |
| 全量 pytest | **764 passed / 10 skipped / 0 failed**，171.72s（`.e2e/pytest-final4.log`）；第三轮 737、此前 751/758 与 A1 复核的 181.33s/159.88s 均为历史记录 |
| 真实浏览器 flows | **8/8 flow、182/182 checks**，12 张截图，1 条预期 409（`task-abe3db42`），RESULT PASS |
| UI 冒烟 / 包验证 | **67/67 PASS**；安装包验证 **3/3** clean graceful close |
| 缺陷节引用 | 18/18 节均至少引用一个测试；唯一陈旧 node id（短超时旧测试名）已修正 |
| 结论边界 | 18 条“主路径已修复”不等于“无已知残留”；当前仅“安装包未签名”未修，其余 4 条残留已修复/仅标记 |

#### A1 发现的证据链缺口（已修正记录）

- **P1-9 文件级续跑**：真实行为测试存在，A1 复核时该节未引用真正覆盖它的
  `tests/test_runner.py::test_partial_workspace_resumed_on_retry`、
  `tests/test_orchestration_hardening.py::test_resume_preserves_interrupted_workspace_and_previous_run_dir`、
  `tests/test_orchestration_hardening.py::test_retry_keeps_previous_attempt_evidence`；
  原引用主要是 UI 文案与超时 API。**已修正**：P1-9 已补入上述引用。
- **仅静态 UI 契约**：P0-5、P1-12、P2-13、P2-14、P2-15 的 UI 部分由读取 `.tsx`
  源码字符串的契约测试支撑。**已修正记录**：批次 D4 用第 8 条 `missing-branch`
  flow 覆盖缺失分支终态、重建与合并；其余条目仍明确保留静态契约边界，不写成全量
  动态覆盖。
- **P2-18 部分缓解**：新客户端改用 `Sec-WebSocket-Protocol` 传 token，但服务端仍接受
  `?token=` 兼容路径，token 仍可能进入日志。**记录保留**：这是兼容路径的已知边界，
  不把 P2-18 写成完全消除日志风险。

#### A2 发现的打包问题（已修正记录）

- `verify_package.ps1` 的关闭路径曾出现竞态：第一次 **exit 3 / gracefulClose=false**，
  第二次 **exit 0 / gracefulClose=true**。**已修正**：最终连续 3 次均为 exitCode=0、
  gracefulClose=true、forcedKill=false、无 main/backend/electron 残留、
  portStillListening=false、exitReason=`clean_graceful_close`。
- `desktop/release/` 含 `builder-debug.yml`（6,155 B）。**已修正记录**：本文不再宣称
  目录“只包含最终安装文件”，该文件按打包辅助文件明确记录。
- 旧观测的 +7 秒 `FIN_WAIT_1`（PID 0）不是 8770 监听残留；最终 3 次验证均为
  `portListenPids=none`、`portStillListening=false`。

#### A3 发现的指纹过期（已修正记录）

- `docs/UI-FLOWS-REPORT.md` 的构建指纹过期：旧报告写 22:14:22 / SHA 前缀
  `2c82a77848fcc8da`。**已修正记录**：后续批次曾按 `index-BiockU1o.js` /
  `40CBE767…B8B08` 回填；当前最终指纹已更新为 `index-D23LbL2h.js` /
  `D402B323B916FBDF1465AB8EE36825236FB2A0D8C9D2DE8E09F1E5AD9435290B`、
  262,962 B、dist 40 文件 / 827,677 B（见上表）。
- `docs/ATLAS-REAL-REGRESSION.md` §3 的代码哈希过期：`runner.py` 从 `6330e12d…`
  变为 `65f04cae…`，`routes_git.py` 从 `856e9208…` 变为 `cd6e701a…`；
  `gitops/service.py` 的 `9c5da07c…` 仍一致。**记录保留**：ATLAS 文档不在本批次
  写入范围，不在这里宣称已改写。

#### 6 条残留项真实状态

| 残留项 | 状态 | 证据 / 说明 |
| --- | --- | --- |
| `missing_branch`：任务分支被外部删除 | **已修复** | 结构化 409（`reason/code=missing_branch`），任务状态 `success` → `blocked`（`merge_missing_branch`），成功 retry/merge/rebuild 后恢复 `success`。`tests/test_git_merge_api.py` **21 passed**，新增覆盖：workdir 缺失优先 `missing_branch`、branch 记录缺失不先 commit、merge 阶段 ref 消失映射 `missing_branch`、`merge_status=missing_branch`、`ref_unavailable` 禁止 HEAD 假成功、worktree 重建失败回滚分支、标题变更仍用记录分支、`create_worktree` 默认拒绝已存在目录（TOCTOU）。`tests/test_task_merge_ui_contract.py` **9 passed**；`scripts/v2_flows.py` 第 8 条 `missing-branch` 通过（43 checks），总计 **182/182**。 |
| CIM 跨平台 | **已修复** | 4 个原 Windows-only `SystemProber` 测试改为 monkeypatch `IS_WIN` 跨平台运行（CIM+netstat 合并、CIM 失败保留 netstat、finish 回退回收、begin 轻量快照）；新增 6 个测试：`test_cim_parser_accepts_single_object_with_bom`、`test_cim_parser_filters_invalid_rows_but_keeps_valid_ones`、`test_cim_parser_invalid_json_reports_degradation`、`test_cim_parser_unexpected_top_level_type_reports_degradation`、`test_system_prober_empty_cim_output_is_success`、`test_posix_never_calls_windows_cim`。保留 PID 复用不回收、只告警不误杀；`tests/test_process_audit.py` 38 个用例随 764 全量通过。 |
| P2-2 `merge_*` 重试闸门 | **已修复** | `engine.MergeRetryBlocked`：`/retry` 对 `merge_*` blocked 返回 **409**，普通重试不再清掉合并阻塞。`OverviewTab` / `TaskDetailView` 隐藏普通重试，提示“请先处理合并冲突或重建分支”；仍保留“重试合并 / 重建分支 / 放弃任务”入口。`tests/test_retry_merge_gate.py` + task UI contract **15 passed**。 |
| 安装包未签名 | **未修** | 新增 `desktop/tools/sign_release.ps1`；`-CheckOnly -Json` 返回 **exit 2 / NOT READY: no certificate**。setup / portable / 主 exe / 后端 exe 4 个产物仍均为 `NotSigned`，可能触发 SmartScreen / 未知发布者提示，见 RELEASE §5。 |
| v1 文档旧数字 | **已修复（仅标记）** | ARCHITECTURE / USAGE / VERIFICATION / UI-V2-QA 头部已统一为同一段“v1 / 历史参考”标记；正文旧数字仅作历史参考，不逐条改写。 |
| Atlas 并发/冲突覆盖 | **已修复** | 场景 A：`task-5b440b9d` / `run-abea7acb`（真实 codex，branch `ma/5b440b9d-atlas-a-1-codex`，commit `af07214f05690fb9f64511ea5940b4c065a6cdcf`）与 `task-c718c57e` / `run-0ec5dc4d`（fake CLI，branch `ma/c718c57e-atlas-a-2-fake-cli`，commit `e42a1bc0ea9e34af688b3563c5c80dd5d2b5ff28`）共享 base `242069f90cb2ff1dc3e260bad77badbf23dcfc48`；task1 merge 200 → `5d588a722078de68d2d87b41f32dbf9128d15142`，task2 merge 409（`docs/merge-conflict-probe.md`）。**原始 32/33，唯一 FAIL 为 task2 `status=success`；修复后同一 task 再 merge 409 → `status=blocked` / `blocked_reason=merge_conflict`，定向复验通过**。场景 B：`task-43e6d1dd` → `task-e04d2694` 依赖闸门 21/21。不是 33/33 全量重跑。 |


## P0-1 worktree 自动合并与「已合并」状态不诚实

- **状态**：已修复
- **问题**：worktree 任务验收通过后没有把任务分支真正合回基线，依赖任务看不到前序
  产出；关闭 `auto_merge` 时也没有记录「未合并」，审阅页/任务详情继续显示「待同步」，
  把 Git 交付误当成文件回收；非脏树/非冲突的合并失败还可能被记成 `task.git.merged`。
- **修复**：Runner 在成功路径提交 `ma/...` 分支，按 `git.auto_merge` 执行
  `merge --no-ff`；关闭时写入 `merged=false, skipped=true, reason=auto_merge_disabled`。
  只有 `merge.merged=true` 才记 `task.git.merged`；脏树/冲突/其它失败分别记
  `task.git.merge.blocked/conflict/failed`，并设置 `blocked_reason`，不会再伪造成功事件。
  `task.result.git.merge` 被归纳为 `merge_status`，任务详情、改动页签、概览和审阅页
  统一按状态渲染。
- **第三轮收口**：默认策略已从 `git.auto_merge=true` 改为 `git.auto_merge=false`
  （**先审后合**），不再是“仅可配置”：验收通过只在 `ma/<task>` 分支提交，人工合并后
  才进入基线；需要 DAG 连续交付的项目显式设 `git.auto_merge=true`。`config/policy.json`、
  `multiagent/presets.py`、README / USAGE-V2 / V2-ARCHITECTURE 已对齐，
  `tests/test_docs_policy_defaults.py` 防止默认值或文档表述回退。
- **DAG 下游闸门（第三轮审查补丁）**：默认 `git.require_merged_for_dependents=true`。
  先审后合下，前置任务 `success` 只表示任务分支已提交；若 `result.git.merge.merged`
  不为 true，下游保持 pending，不会基于旧基线派发；人工/自动合并后自动恢复 ready。
  该闸门只约束默认 `success` 边，`any` / `always` / `soft` 边不受影响；可在 policy 中
  显式关闭，但不建议。
- **改动文件**：`multiagent/orchestrator/runner.py`、`multiagent/gitops/service.py`、
  `multiagent/api/taskworkspace.py`、`webapp/src/features/task/helpers.ts`、
  `webapp/src/features/task/TaskDetailView.tsx`、
  `webapp/src/features/task/ChangesTab.tsx`、
  `webapp/src/features/task/OverviewTab.tsx`、
  `webapp/src/features/review/review-api.ts`、
  `webapp/src/features/review/ReviewDetail.tsx`
- **验证**：
  - `pytest -q tests/test_git_hardening.py::test_runner_auto_merge_disabled_records_skipped_and_keeps_branch`
  - `pytest -q tests/test_review_merge_contract.py::test_workspace_reports_merged_task`
  - `pytest -q tests/test_task_merge_ui_contract.py::test_task_detail_uses_merge_state_instead_of_sync`
  - `pytest -q tests/test_git_hardening.py::test_runner_never_reports_merged_event_on_failed_merge`（通过）
  - `pytest -q tests/test_docs_policy_defaults.py`（通过：policy.json 与 presets 默认
    `auto_merge=False`，文档含「先审后合」且不再出现「默认 true / 默认自动合并」）
  - `pytest -q tests/test_policy_merge_defaults.py::test_unmerged_predecessor_blocks_dependent_until_merge`（通过：前置未合并时下游不 ready，人工合并后恢复）
  - `pytest -q tests/test_policy_merge_defaults.py::test_require_merged_for_dependents_can_be_disabled`（通过：显式关闭闸门的行为）
  - 前 3 个此前已通过；新增的失败合并用例本轮执行通过。
- **残留风险**：脏树和真实冲突仍按设计阻断，需要人工处理；这两条由 P0-4 和 P0-3
  覆盖。默认先审后合 + `require_merged_for_dependents=true` 意味着 DAG 下游必须等上游
  任务分支真正合并后才 ready；`any` / `always` / `soft` 边不受该闸门约束，显式关闭
  闸门也会让下游基于旧基线运行。这是刻意的保守默认，不是回归。

## P0-2 任务提交混入 `.multiagent/` 控制面

- **状态**：已修复
- **问题**：项目没有 `.gitignore` 时，`git add -A` 会把 `.multiagent/handoff.json`、
  `context.json` 等运行时文件提交到任务分支，污染交付物。
- **修复**：`commit_worktree` 使用显式 exclude pathspec 排除 `.multiagent` 及其子目录，
  即使没有 `.gitignore` 也不会进入 commit；`dirty_paths` 同样忽略控制面，避免误判脏树。
- **改动文件**：`multiagent/gitops/service.py`
- **验证**：
  - `pytest -q tests/test_git_hardening.py::test_commit_excludes_control_plane_without_gitignore`（通过）
  - `pytest -q tests/test_git_hardening.py::test_merge_blocks_on_dirty_tree_and_ignores_control_plane`（通过）
- **残留风险**：如果用户把控制面文件复制到 `.multiagent` 之外的路径，仍可能被提交；
  这属于用户交付物，不自动排除。

## P0-3 合并冲突无结构化 409、人工合并结果不回写、放弃分支不生效

- **状态**：已修复
- **问题**：合并冲突只返回文本错误，UI 无法展示冲突文件；人工 merge 成功后
  `task.result.git.merge` 仍是旧状态，刷新后继续显示「未合并」；`discard-worktree`
  只删目录不删分支，任务仍显示待合并。
- **修复**：
  - `/merge` 返回结构化 200/409：成功带 `sha/files/base`；脏树带
    `blocked/dirty_files`；冲突带 `conflict/files`，并已 `merge --abort`；
  - 人工合并结果写回 `result.git.merge`（`source=manual`），刷新后状态一致；
  - `discard-worktree` 支持 `{"delete_branch": true}`，删除后写回
    `discarded=true/reason=discarded`，UI 隐藏合并入口。
  - waiting 的 Git 任务「验收并合并」改为先 `POST /merge`，成功后再
    `POST /approve {sync:false}` 标记验收；merge 失败（409）时任务保持 waiting，
    可重试或放弃，不再出现先 approve 造成的 success+未合并。
- **改动文件**：`multiagent/api/routes_git.py`、`multiagent/gitops/service.py`、
  `webapp/src/features/task/TaskDetailView.tsx`、
  `webapp/src/features/task/ChangesTab.tsx`、
  `webapp/src/features/review/ReviewDetail.tsx`、
  `webapp/src/features/review/review-api.ts`
- **验证**：
  - `pytest -q tests/test_git_merge_api.py::test_merge_conflict_returns_structured_409_and_aborts`（通过）
  - `pytest -q tests/test_git_hardening.py::test_discard_branch_persists_discarded_state`（通过）
  - `pytest -q tests/test_git_hardening.py::test_manual_merge_persists_result_for_ui`（通过）
  - `pytest -q tests/test_task_merge_ui_contract.py::test_waiting_git_task_approves_then_merges_instead_of_file_sync`（通过：先 merge 后 approve）
  - `pytest -q tests/test_task_merge_ui_contract.py::test_waiting_git_task_approve_sync_false_then_real_git_merge`（通过）
- **残留风险**：放弃分支是不可逆操作；默认 `delete_branch=false` 只删目录。删除当前
  基线分支会被 409 拒绝，但用户仍可能在外部 Git 工具中误删分支。**第三轮时点残留
  （已由第四轮收口）**：分支被外部删除时，`/merge` 曾返回文本 409（没有结构化
  `missing_branch`），前端只 toast。**第四轮更新**：已改为结构化 409
  （`reason/code=missing_branch`），任务进入 `blocked` / `merge_missing_branch` 终态，
  UI 提供“重建分支”，`POST /api/tasks/{tid}/rebuild-branch` 需确认后恢复可合并状态；
  验证见 `tests/test_git_merge_api.py::test_merge_missing_branch_returns_structured_409`
  等与 `tests/test_task_merge_ui_contract.py::test_missing_branch_rebuild_ui_contract`；
  浏览器流程 `scripts/v2_flows.py::missing-branch` 已通过（43 checks），批次 D4 总计
  8 flows / 182 checks；`test_git_merge_api.py` 21 passed、`test_task_merge_ui_contract.py`
  9 passed。冲突/缺失分支后任务从 `success` → `blocked`，成功 retry/merge/rebuild 后
  恢复 `success`。
  **P2-2 已修复**：`engine.MergeRetryBlocked` 已接入；`/retry` 对
  `merge_conflict` / `merge_missing_branch` 等 `merge_*` blocked 返回 **409**，
  `OverviewTab` / `TaskDetailView` 隐藏普通重试并提示“请先处理合并冲突或重建分支”，
  同时保留“重试合并 / 重建分支 / 放弃任务”入口。`tests/test_retry_merge_gate.py`
  + task UI contract **15 passed**。

## P0-4 基线脏树仍执行合并，可能混入/覆盖人工改动

- **状态**：已修复
- **问题**：基线有未提交改动时直接 `git merge`，会把人工改动混进 Agent 合并结果，
  审计无法说明合并落在哪棵树上。
- **修复**：`merge_worktree_branch` 增加 `require_clean`（默认 true），先查
  `dirty_paths`；脏树返回 `blocked=True/reason=dirty_worktree/dirty_files`，不进入
  merge 状态。Runner 自动合并时把任务转为 `blocked`；`/merge` 返回 409。`.multiagent/`
  不计入脏树。`policy.git.require_clean_tree=false` 可显式关闭（不建议）。
- **改动文件**：`multiagent/gitops/service.py`、`multiagent/orchestrator/runner.py`、
  `multiagent/api/routes_git.py`、`config/policy.json`、`multiagent/presets.py`、
  `webapp/src/features/settings/SettingsView.tsx`
- **验证**：
  - `pytest -q tests/test_git_hardening.py::test_merge_blocks_on_dirty_tree_and_ignores_control_plane`（通过）
  - `pytest -q tests/test_git_merge_api.py::test_merge_blocks_dirty_baseline_without_touching_user_files`（通过）
  - `pytest -q tests/test_settings_merge_ui.py::test_policy_git_fields_roundtrip_preserves_other_policy`（通过）
- **残留风险**：关闭 `require_clean_tree` 后仍可合并脏树，这是显式 opt-out；文档和设置页
  已标注风险。Git submodule / 未跟踪忽略项不会被 `dirty_paths` 完整覆盖。

## P0-5 隔离降级静默，UI 把请求模式当成实际模式

- **状态**：已修复
- **问题**：worktree 因非 Git 仓库、脏工作区或 `worktree add` 失败退化为 copy 时，
  任务仍显示 worktree；用户不知道实际执行模式和原因。
- **修复**：`IsolationService.prepare_ex()` 返回
  `requested_mode/mode/workdir/degraded/degrade_code/degrade_reason`；Runner 写入
  `task.result.isolation` 并产生 `task.isolation.degraded` 事件；任务详情展示
  `degrade_reason`，概览显示实际模式。`prepare()` 保持返回 `Path` 的旧签名。
- **改动文件**：`multiagent/workspace/isolation.py`、
  `multiagent/orchestrator/runner.py`、`multiagent/api/app.py`、
  `webapp/src/features/task/TaskDetailView.tsx`、
  `webapp/src/features/task/OverviewTab.tsx`
- **验证**：
  - `pytest -q tests/test_git_hardening.py::test_runner_records_isolation_degradation`（通过）
  - `pytest -q tests/test_isolation_modes.py`（本轮执行了其中单次复制用例；其余用例存在）
  - `pytest -q tests/test_task_merge_ui_contract.py::test_isolation_degrade_reason_is_rendered`
- **残留风险**：降级原因只进入任务 result 和 UI，不会自动重试 worktree；用户需要提交/
  清理工作区后再次运行。`shared` + 修改型 + 空写范围的兜底会让实际模式与请求不同，
  这是安全策略，不是异常。

## P1-6 worktree 基线复制两次，Windows CRLF/未跟踪文件导致全量误判

- **状态**：已修复
- **问题**：worktree 成功路径先复制 `root→baseline`，再从 worktree 复制，Windows
  CRLF 转换和未跟踪文件使每个文本文件都被判成「被修改」，构建产物被判成「删除」，
  正常任务被误判越权。
- **修复**：worktree 成功路径只从新建的 worktree 复制一次 baseline；失败/降级路径才
  走 `root→baseline→work`。`prepare()` 旧签名保留。
- **改动文件**：`multiagent/workspace/isolation.py`
- **验证**：
  - `pytest -q tests/test_isolation_modes.py::test_worktree_baseline_is_copied_once_from_worktree`（通过）
  - `pytest -q tests/test_isolation_modes.py::test_clean_git_repo_uses_worktree_and_baseline_matches`
  - 大目录实测（444 个文件 / 约 9MB，Windows）：copy 模式 1.09s，worktree 模式 0.81s
    （仅一次 worktree→baseline 复制，无退化）。
- **残留风险**：单次复制仍是一次全树复制，超大仓库/大量 `node_modules` 会有 I/O 成本；
  目前依赖工作区卫生规则和 excludes，未做增量基线。

## P1-7 Agent 残留监听进程/端口未审计回收

- **状态**：已修复
- **问题**：Agent 启动的 dev server 在命令退出后仍监听端口，下一次任务或用户服务撞端口。
- **修复**：新增 `multiagent/agents/process_audit.py`。Agent 调用前后各拍一次监听 TCP
  快照，只处理新增 pid；归属判定要求 command/exe_path 含规范化 workdir，或 cwd 位于
  workdir 之下。只有 `owned=true` 且 `reap=true` 才终止，否则只写 warning；pid<=4、
  当前进程、父进程、调用前已存在的 pid 永不触碰。Runner 写入
  `task.result.processes` 并产生 `task.processes` 事件；`processes.audit_enabled` /
  `reap_orphans` 可配置。`processes.min_timeout_sec`（默认 0，全部审计；正数为逃生阀）时，计划超时
  1..4 秒的极短任务不启动审计，避免 ~2 秒的 Windows 元数据查询破坏硬超时语义；
  超时 0（不限时）仍审计。Windows 在 `finish` 阶段额外用
  `Get-NetTCPConnection -State Listen`（CIM）补齐 netstat 漏报；`finish` 异步执行，
  不阻塞任务返回，CIM 失败只降级 warning。`processes.cim_fallback`（默认 true）控制
  该兜底，测试夹具关闭以提速；CIM 成功但零监听视为成功空结果。创建时间早于本次调用
  的新增 pid 视为 PID 复用，不回收；`shared` 模式 workdir 就是项目根，默认不自动回收
  用户自己的监听进程（只记录）。`Workbench.close()` 通过
  `runner.wait_for_process_audits(timeout=10.0)` 有界等待异步审计线程落库，测试
  teardown 可 `wait_audits=False` 提速。
- **改动文件**：`multiagent/agents/process_audit.py`、
  `multiagent/orchestrator/runner.py`、
  `config/policy.json`、`multiagent/presets.py`、
  `webapp/src/features/task/OverviewTab.tsx`、`webapp/src/features/task/helpers.ts`
- **验证**：
  - `pytest -q tests/test_process_audit.py::test_unowned_new_listener_is_only_reported_never_killed`（通过）
  - `pytest -q tests/test_runner_processes.py::test_runner_records_process_audit_result_and_event`（通过）
  - `pytest -q tests/test_runner_processes.py::test_short_timeout_can_still_opt_out_of_audit`（通过：显式 `min_timeout_sec=5` 时短任务跳过审计）
  - `pytest -q tests/test_runner_processes.py::test_short_timeout_is_audited_by_default`（通过：默认 `min_timeout_sec=0` 时 1 秒任务仍审计）
  - `pytest -q tests/test_runner_processes.py::test_process_audit_can_be_disabled_by_policy`（通过）
  - `pytest -q tests/test_process_audit.py::test_pid_reuse_with_old_creation_time_is_not_reaped`（通过：PID 复用不回收）
  - `pytest -q tests/test_git_hardening.py::test_shared_mode_does_not_reap_processes`（通过：shared 只记录）
  - `pytest -q tests/test_runner_processes.py::test_slow_audit_does_not_block_run_return`（通过：异步 + 退出等待）
  - `pytest -q tests/test_process_audit.py::test_cim_empty_output_is_success_not_degraded`（通过：CIM 空结果）
  - `pytest -q tests/test_process_audit.py`（38 个测试函数，随最终重试闸门轮全量 764 通过）
- **残留边界**：测试使用注入的假 prober，不依赖真实端口；真实环境只记录/
  回收**可证明归属**的进程，判断不了只告警。prober 不支持（缺少 netstat/PowerShell
  权限）时 `supported=false`，不保证清理所有残留端口。Windows 真实监听进程的集成回收
  仍依赖 Windows 环境；4 个原 Windows-only `SystemProber` 测试
  （`test_system_prober_merges_cim_with_netstat_by_pid`、
  `test_system_prober_cim_failure_keeps_netstat_result`、
  `test_finish_reaps_netstat_listener_when_cim_raises`、
  `test_system_prober_lightweight_snapshot_never_runs_powershell`）已改为 monkeypatch
  `IS_WIN` 跨平台运行；新增 6 个 CIM 解析/空结果/非 Windows 不调用测试
  （`test_cim_parser_accepts_single_object_with_bom`、
  `test_cim_parser_filters_invalid_rows_but_keeps_valid_ones`、
  `test_cim_parser_invalid_json_reports_degradation`、
  `test_cim_parser_unexpected_top_level_type_reports_degradation`、
  `test_system_prober_empty_cim_output_is_success`、
  `test_posix_never_calls_windows_cim`）；PID 复用不回收仍由
  `test_pid_reuse_with_old_creation_time_is_not_reaped` 守护，不再只靠 `skipif`
  行为测试。审计是旁路能力，
  异常只折算 warning，不会拖垮任务；`shared` 模式默认不自动回收；配置
  `min_timeout_sec>0` 可跳过极短任务审计；默认 0 时 begin 只跑 netstat，finish 异步，
  不再挤占硬超时。

## P1-8 已完成 worktree 无保留/清理策略

- **状态**：已修复
- **问题**：合并后的 worktree 永久占磁盘；缺少 `keep_worktree` 开关和按保留期清理的
  运维入口。
- **修复**：Runner 支持 `git.keep_worktree=false`（合并成功后当次回收目录，分支保留）；
  `ops.prune` 增加 worktree 清理：只处理终态、超过 `git.worktree_retention_days`
  （默认 14 天）、且 `merge_status=merged` 的已登记 worktree；未合并、运行中、copy
  隔离永不清理；只删目录，任务分支/提交保留。
- **改动文件**：`multiagent/ops.py`、`multiagent/gitops/service.py`、
  `multiagent/orchestrator/runner.py`、`config/policy.json`、`multiagent/presets.py`、
  `webapp/src/features/settings/SettingsView.tsx`、`docs/OPERATIONS.md`
- **验证**：
  - `pytest -q tests/test_ops_worktrees.py::test_unmerged_old_worktrees_are_never_removed`（通过）
  - `pytest -q tests/test_ops_worktrees.py::test_merged_old_worktree_dry_run_then_remove_keeps_branch`
  - `pytest -q tests/test_git_hardening.py::test_runner_keep_worktree_false_removes_worktree_but_keeps_branch`
- **残留风险**：清理依赖 `task.result.git.merge.merged` 和 worktree 注册信息；历史任务若
  没有该元数据会保守保留，磁盘可能持续占用。被文件占用的 worktree 删除失败会记录
  `error`，需要下次重试或人工处理。

## P1-9 没有真正的会话续跑，重试语义不诚实

- **状态**：已修复
- **问题**：UI 让用户以为延长超时能救正在运行的调用；重试文案容易被理解成 ACP 会话
  恢复，实际是新的 Agent 会话 + 文件级续跑。
- **修复**：`POST /api/tasks/{tid}/timeout` 返回 `applies_to=next_attempt/next_run`；
  任务详情提示「对下一次尝试生效，不会延长正在运行的上次调用」，并显示
  `runtime.next_attempt_timeout_sec`。重试明确为文件级续跑：归档上次工作副本，只恢复
  `write_scope` 内、非保护路径、非逃逸的半成品，新 Agent 会话不复用 ACP session。
- **改动文件**：`multiagent/api/app.py`、`multiagent/tasks/runtime.py`、
  `multiagent/orchestrator/runner.py`、
  `webapp/src/features/task/TaskDetailView.tsx`
- **验证**：
  - `pytest -q tests/test_task_merge_ui_contract.py::test_task_detail_uses_merge_state_instead_of_sync`（通过，含「文件级续跑」「不会延长正在运行的上次调用」断言）
  - `pytest -q tests/test_large_projects.py::test_task_timeout_endpoint_updates_budget`（通过）
  - `pytest -q tests/test_large_projects.py::test_run_records_effective_timeout_and_task_runtime`
  - `pytest -q tests/test_runner.py::test_partial_workspace_resumed_on_retry`（第四轮补引；全量 764 覆盖）
  - `pytest -q tests/test_orchestration_hardening.py::test_resume_preserves_interrupted_workspace_and_previous_run_dir`（第四轮补引；全量 764 覆盖）
  - `pytest -q tests/test_orchestration_hardening.py::test_retry_keeps_previous_attempt_evidence`（第四轮补引；全量 764 覆盖）
- **残留风险**：后端 `/timeout` 只校验 `>0`，30–86400 的范围由前端和创建入口约束；
  直接调 API 可写入更大值。运行中的硬超时已交给适配器，无法热修改，这是设计选择。

## P1-10 新建任务对话框缺 timeout_sec / max_attempts

- **状态**：已修复
- **问题**：任务只能使用类型默认超时和尝试次数，无法在创建时显式设置。
- **修复**：`NewTaskDialog` 增加可折叠「高级选项」：超时预算 30–86400 秒、最大尝试
  次数 1–10；留空不进入请求体，非法值标红并阻止提交；后端 `quick-task` / 创建接口
  接收并落库，留空时按类型默认解析。
- **第三轮关联收口（waiting 顺序）**：waiting 的 Git 任务不在本对话框创建流程内，其
  主操作已改为「验收并合并」：先 `POST /merge`，成功后再 `POST /approve {sync:false}`；
  merge 409 时保持 waiting，可重试/放弃（详见 P0-3）。
- **改动文件**：`webapp/src/features/tasks/NewTaskDialog.tsx`、
  `multiagent/api/quicktask.py`、`multiagent/api/app.py`
- **验证**：
  - `pytest -q tests/test_newtask_dialog_contract.py::test_create_task_persists_explicit_advanced_fields`（通过）
  - `pytest -q tests/test_newtask_dialog_contract.py::test_quick_task_persists_advanced_fields`（通过）
  - `pytest -q tests/test_newtask_dialog_contract.py::test_dialog_static_contract`
  - `pytest -q tests/test_newtask_dialog_contract.py::test_create_task_without_advanced_fields_uses_kind_defaults`
- **残留风险**：范围校验是前端 + 创建入口；`task add-plan` 仍可传任意整数，依赖
  `prepare_creation_fields` 兜底。计划文件没有 UI 表单，只有 JSON。waiting 的 Git
  任务由「验收并合并」处理（先 merge 后 approve {sync:false}），不经过本对话框。

## P1-11 同一真实目录可重复登记为多个项目

- **状态**：已修复
- **问题**：同一目录换名字导入会创建多个项目，运行目录、任务和快照分散，容易误操作。
- **修复**：`normalize_root()` 规范化绝对路径（Windows 大小写不敏感、尾斜杠/反斜杠）；
  `import_dir` / `create` 按规范化路径判重，冲突抛 `ProjectRootConflict`；API 返回
  409 + `error=project_root_exists`、`existing_project_id`、`existing_project`、
  `root_path`。`exists_ok=true` 返回已有项目而不新增行。
- **改动文件**：`multiagent/workspace/projects.py`、`multiagent/api/app.py`
- **验证**：
  - `pytest -q tests/test_project_api_dedup.py::test_import_same_directory_twice_returns_409_with_existing_id`（通过）
  - `pytest -q tests/test_project_dedup.py::test_normalize_root_contract`（通过）
  - `pytest -q tests/test_project_dedup.py::test_import_same_root_different_name_conflicts`
- **残留风险**：判重基于路径字符串规范化，不解析符号链接/硬链接/junction 的真实身份；
  同一物理目录通过不同挂载点仍可能被当成两个项目。

## P1-12 ACP/CLI 模型控制语义不诚实

- **状态**：已修复
- **问题**：UI/文档暗示可以在请求里指定模型；ACP v1 没有经过验证的请求级 model 字段，
  CLI 适配器也只有参数骨架含 `{model}` 时才真正注入。
- **修复**：ACP `describe()` 返回 `model_control="agent-config"`，适配器不发送 model；
  CLI `describe()` 根据 `args` 是否含 `{model}` 返回 `request-arg` 或 `agent-config`；
  Agent 视图展示诚实文案。模型最终由 Agent 自身配置决定。
- **改动文件**：`multiagent/agents/acp_adapter.py`、`multiagent/agents/cli_adapter.py`、
  `multiagent/agents/base.py`、`webapp/src/features/agents/`
- **验证**：
  - `pytest -q tests/test_agent_cost_model.py::test_acp_describe_declares_model_control_agent_config`（通过）
  - `pytest -q tests/test_agent_cost_model.py::test_cli_describe_model_control_matches_argv_behavior`（通过）
  - `pytest -q tests/test_agent_cost_model.py::test_agents_view_has_honest_model_control_copy`（通过）
- **残留风险**：不同 Agent CLI 的模型参数形式可能不同；当前只支持 `{model}` 占位符，
  不支持按 Agent 自定义参数模板。ACP 协议未来若增加请求级 model 字段，需要重新验证
  后再改 `model_control`。

## P2-13 成本来源被伪造成 0 / 免费

- **状态**：已修复
- **问题**：Agent 协议没有返回 usage 时，`cost_tokens/cost_amount` 默认 0，UI 显示
  「0 元」，把「未上报」误读成「免费」。
- **修复**：`AgentResult.cost_source` 区分 `reported`（协议真实上报，显式 0 也算）、
  `estimated`（本地估算，当前适配器未产生）和 `unreported`（协议未提供）；Runner 写入
  `task.result.cost.source`，UI 对 `unreported` 显示「未上报（0 不代表免费）」，对
  `estimated` 显示 `≈`。
- **改动文件**：`multiagent/agents/base.py`、`multiagent/agents/acp_adapter.py`、
  `multiagent/agents/cli_adapter.py`、`multiagent/orchestrator/runner.py`、
  `webapp/src/features/task/OverviewTab.tsx`、`webapp/src/features/task/helpers.ts`、
  `webapp/src/api/types.ts`
- **验证**：
  - `pytest -q tests/test_agent_cost_model.py::test_cli_adapter_never_reports_cost`（通过）
  - `pytest -q tests/test_agent_cost_model.py::test_acp_usage_sets_task_cost_source_reported`（通过）
  - `pytest -q tests/test_agent_cost_model.py::test_explicit_zero_usage_is_still_reported`
  - `pytest -q tests/test_task_overview_contract.py::test_task_detail_cost_source_contract`
- **残留风险**：`estimated` 目前只有类型和 UI 处理，没有实际估算器；历史任务没有
  `result.cost.source`，UI 会显示「来源未知」，不会补算。

## P2-14 验收只支持精确路径，失败无候选/修正入口

- **状态**：已修复
- **问题**：任务把验收写死为 `AppShell.tsx`，Agent 产出 `layout/AppShell.tsx` 时验收
  失败且没有诊断；glob 产物路径不被支持。
- **修复**：产出/验收支持 glob（`*`、`?`、`[`），生成 `glob_min_files`；精确路径失败时
  返回 `candidates`（最多 20 个；失败摘要最多列 5 个，跳过依赖/版本库目录和符号链接）和
  `suggested`（例如改 glob），但**不改变失败结论**。任务详情「概览 → 验收条件」提供
  「修正验收路径」，通过 `PATCH /api/tasks/{tid}` 只改条件，保存后需重新运行/验收。
  计划创建前还会扫描项目根目录：`outputs` 与路径型验收条件中的精确路径若不存在、但
  basename 只匹配到唯一候选，自动回填为实际相对路径；多候选/无候选只写 warning，不阻断
  创建；glob、已存在路径、绝对路径和 `..` 路径不重写。扫描结果写入
  `result.acceptance_path_scan`（`autofixes` / `warnings`），改写后的路径仍由真实产物
  决定验收是否通过。
- **改动文件**：`multiagent/tasks/acceptance.py`、`multiagent/tasks/profiles.py`、
  `multiagent/tasks/planner.py`、`multiagent/api/app.py`、
  `webapp/src/features/task/OverviewTab.tsx`
- **验证**：
  - `pytest -q tests/test_acceptance_flexible.py::test_file_exists_glob_passes_on_actual_layout`（通过）
  - `pytest -q tests/test_acceptance_flexible.py::test_exact_file_exists_failure_reports_candidates_without_passing`（通过）
  - `pytest -q tests/test_plan_acceptance_scan.py`（6 个用例全部通过）
  - `pytest -q tests/test_task_overview_contract.py::test_patch_acceptance_roundtrip`
- **残留风险**：候选搜索按 basename 进行，超大目录有性能上限（20 个候选/跳过目录）；
  glob 语义是 Python `Path.glob`，与 shell glob 不完全一致。计划扫描只处理精确路径的
  唯一候选回填，不处理 glob、不做内容匹配、不跨出项目根目录；多候选时仍需人工确认。

## P2-15 旧版控制台默认暴露

- **状态**：已修复
- **问题**：`/static/index.html` 可直接访问旧零构建控制台，和 v2 入口并列暴露，容易
  误用旧界面。
- **修复**：`/legacy` 和 `/static/index.html` 默认 404；只有 `MULTIAGENT_UI=legacy`
  时 `/legacy` 才返回旧页面。`/` 仍优先 v2 dist；构建产物缺失时才降级返回旧页面。
  v2 欢迎页的「打开旧版控制台」死链已删除（默认 UI 不再暴露任何旧入口）。
- **改动文件**：`multiagent/api/app.py`、
  `webapp/src/features/onboarding/WelcomeView.tsx`
- **验证**：
  - `pytest -q tests/test_git_hardening.py::test_legacy_ui_entry_is_closed_by_default`（通过）
  - `pytest -q tests/test_git_hardening.py::test_legacy_ui_entry_available_only_with_env`（通过）
  - `pytest -q tests/test_git_hardening.py::test_v2_welcome_page_does_not_expose_legacy_entry`（通过）
- **残留风险**：旧控制台仍是可回退能力（`MULTIAGENT_UI=legacy`），其安全加固与新功能
  不再跟进；文档已标注 legacy。

## P2-16 文档与实现漂移

- **状态**：已修复
- **问题**：`docs/RELEASE.md` 标题仍写 1.0.0/schema 5，并把上一轮安装包描述成当前产物；
  `V2-ARCHITECTURE.md` 还写着「merge 没有 HTTP 路由」「review 只用 /sync」等已被本轮
  实现推翻的内容；README/USAGE 未描述脏树阻断、冲突 UI、进程审计、WS 子协议 token、
  legacy 入口收敛和项目判重。
- **修复**：
  - `docs/RELEASE.md` 改为源码 2.0.0 / schema 6，明确 `desktop/release/` 不存在、
    `release-v2` portable 是上一轮构建，必须重新 `npm run dist`（该文件由打包流程维护）；
  - `README.md` 更新工作流、关键策略表、模型控制、成本来源、WS token、legacy 入口、
    项目判重和故障排查；默认策略改为先审后合，并给出 DAG 连续交付的 policy 片段；
  - `docs/USAGE-V2.md` §5 重写，覆盖先审后合/自动合并、脏树、冲突、放弃分支、状态展示、
    高级选项、超时/重试、验收 glob/候选/修正、计划扫描、进程与端口；waiting 的 Git
    任务明确为「验收并合并」；
  - `docs/V2-ARCHITECTURE.md` 更新 gitops/routes_git/workspace/isolation/WS/legacy 表，
    并记录 `git.auto_merge` 默认 false、CIM 兜底与异步进程审计；
  - 新增本台账 `docs/DEFECTS-V2.md`；历史 v1 文档（ARCHITECTURE / USAGE / VERIFICATION /
    UI-V2-QA）头部已加「v1 / 历史参考」标记，V2-FINAL-REPORT 标注为历史交付报告；
  - 新增 `tests/test_docs_legacy_markers.py` 与 `tests/test_docs_policy_defaults.py`，
    分别守护历史标记与默认策略文档表述。
- **改动文件**：`README.md`、`docs/RELEASE.md`、`docs/USAGE-V2.md`、
  `docs/V2-ARCHITECTURE.md`、`docs/DEFECTS-V2.md`、
  `tests/test_docs_legacy_markers.py`、`tests/test_docs_policy_defaults.py`
- **验证**：
  - `pytest -q tests/test_docs_legacy_markers.py`（通过：v1 标记齐全，当前文档未被误标）
  - `pytest -q tests/test_docs_policy_defaults.py`（通过：policy/presets 默认 false，
    README / USAGE-V2 / V2-ARCHITECTURE 含「先审后合」且无「默认 true / 默认自动合并」）
  - `pytest -q tests/test_git_hardening.py::test_legacy_ui_entry_is_closed_by_default`（通过，验证文档所述 legacy 行为）
  - `pytest -q tests/test_task_merge_ui_contract.py::test_task_detail_uses_merge_state_instead_of_sync`（通过，验证文档所述合并 UI）
  - `rg -n "默认 true|默认自动合并" README.md docs/USAGE-V2.md docs/V2-ARCHITECTURE.md`
    应无命中。
  - `git diff -- README.md docs/RELEASE.md docs/USAGE-V2.md docs/V2-ARCHITECTURE.md docs/DEFECTS-V2.md`
- **残留说明**：历史 v1 文档（ARCHITECTURE / USAGE / VERIFICATION / UI-V2-QA）
  正文仍保留旧版本/旧数字，只通过头部「v1 / 历史参考」标记说明「仅作历史记录」，
  状态为“已修复（仅标记）”，不逐条重写。`docs/RELEASE.md` 由打包流程维护，本轮
  写入范围不含该文件；最终安装包与 Atlas 数字已由主线回填，但后续打包仍需同步更新。
  发布产物**未签名**
  （Authenticode NotSigned），可能触发 SmartScreen / 未知发布者，见 RELEASE §5。
  当前自动检查是静态契约（历史标记 + 默认策略），不是全文一致性校验，后续代码变更
  仍可能造成新的文档漂移。

## P2-17 缺少 CI 级真实行为闭环

- **状态**：已修复
- **问题**：此前只有 UI 导航冒烟，无法证明「建任务 → 执行 → 验收 → 自动提交/合并 →
  下一个任务看到新基线」在真实 SQLite + 真实 Git 仓库里成立。
- **修复**：新增 `tests/test_e2e_behavior.py`，使用假 Agent 计划驱动，不启动后台调度
  线程、不依赖网络/模型；覆盖任务生命周期、任务详情/工作副本/worktree/看板/仪表盘、
  人工重复合并幂等、第二个任务在合并后的基线上继续。
- **改动文件**：`tests/test_e2e_behavior.py`
- **验证**：
  - `pytest -q tests/test_e2e_behavior.py::test_full_task_lifecycle_via_api`（通过）
  - `pytest -q tests/test_e2e_behavior.py::test_second_task_sees_merged_baseline`（通过）
- **残留风险**：假 Agent 不覆盖真实 ACP 流式事件、权限请求和真实模型输出；真实 Agent
  的端到端仍需 `scripts/e2e_real.py`（显式执行，消耗额度）。Atlas 真实回归（见
  `docs/ATLAS-REAL-REGRESSION.md`）覆盖 1 个文档任务的成功链路与脏树阻断；并发与
  冲突已由 `.e2e/atlas-concurrency/a|b` 补齐（场景 A 2 个并发任务、409 冲突与
  `merge --abort`；场景 B 依赖合并闸门），但仍**不含超大仓库**；结果绑定当时代码
  哈希，后续变更需重新回归。

## P2-18 WebSocket token 放在 URL query

- **状态**：已修复
- **问题**：`/ws?project=...&token=...` 把 token 放进 URL，会进入浏览器历史、代理日志
  和访问日志。
- **修复**：浏览器新客户端用 `Sec-WebSocket-Protocol: multiagent.token.<base64url>`
  传递 token；服务端 `extract_ws_token` 优先 Authorization，其次旧 `?token=`，最后
  子协议，并在 `websocket.accept(subprotocol=...)` 回显；前端 `wsProtocols()` /
  `wsUrl()` 不再把 token 拼进 query。
- **改动文件**：`multiagent/api/security.py`、`multiagent/api/app.py`、
  `webapp/src/api/client.ts`、`webapp/src/app/queries.ts`
- **验证**：
  - `pytest -q tests/test_git_hardening.py::test_websocket_token_via_subprotocol`（通过）
- **残留风险**：旧客户端仍可用 `?token=`（兼容路径），token 仍可能进入日志；服务端
  未强制拒绝 query token。反向代理若剥离 `Sec-WebSocket-Protocol`，浏览器会握手失败。

---

## 抽查记录（第二轮实际执行）

执行环境：`~/multi_xuanwo\.venv\Scripts\python.exe -m pytest -q`。
结果：**原表 41 条中 40 条为现存原编号用例并全部通过；第 40 条为陈旧引用，已替换为
2 个现存用例，由最终重试闸门轮全量 764 passed 覆盖。**因此不再宣称“41 passed”；仅有一条
StarletteDeprecationWarning。

| # | 命令（文件::用例） | 结果 |
| --- | --- | --- |
| 1 | `tests/test_git_hardening.py::test_commit_excludes_control_plane_without_gitignore` | PASS |
| 2 | `tests/test_git_merge_api.py::test_merge_conflict_returns_structured_409_and_aborts` | PASS |
| 3 | `tests/test_process_audit.py::test_unowned_new_listener_is_only_reported_never_killed` | PASS |
| 4 | `tests/test_acceptance_flexible.py::test_file_exists_glob_passes_on_actual_layout` | PASS |
| 5 | `tests/test_project_api_dedup.py::test_import_same_directory_twice_returns_409_with_existing_id` | PASS |
| 6 | `tests/test_isolation_modes.py::test_worktree_baseline_is_copied_once_from_worktree` | PASS |
| 7 | `tests/test_ops_worktrees.py::test_unmerged_old_worktrees_are_never_removed` | PASS |
| 8 | `tests/test_agent_cost_model.py::test_cli_adapter_never_reports_cost` | PASS |
| 9 | `tests/test_task_merge_ui_contract.py::test_task_detail_uses_merge_state_instead_of_sync` | PASS |
| 10 | `tests/test_newtask_dialog_contract.py::test_create_task_persists_explicit_advanced_fields` | PASS |
| 11 | `tests/test_git_hardening.py::test_websocket_token_via_subprotocol` | PASS |
| 12 | `tests/test_git_hardening.py::test_legacy_ui_entry_is_closed_by_default` | PASS |
| 13 | `tests/test_review_merge_contract.py::test_workspace_reports_merged_task` | PASS |
| 14 | `tests/test_e2e_behavior.py::test_full_task_lifecycle_via_api` | PASS |
| 15 | `tests/test_git_hardening.py::test_runner_auto_merge_disabled_records_skipped_and_keeps_branch` | PASS |
| 16 | `tests/test_git_hardening.py::test_merge_blocks_on_dirty_tree_and_ignores_control_plane` | PASS |
| 17 | `tests/test_git_hardening.py::test_runner_records_isolation_degradation` | PASS |
| 18 | `tests/test_git_hardening.py::test_discard_branch_persists_discarded_state` | PASS |
| 19 | `tests/test_agent_cost_model.py::test_acp_usage_sets_task_cost_source_reported` | PASS |
| 20 | `tests/test_acceptance_flexible.py::test_exact_file_exists_failure_reports_candidates_without_passing` | PASS |
| 21 | `tests/test_large_projects.py::test_task_timeout_endpoint_updates_budget` | PASS |
| 22 | `tests/test_project_dedup.py::test_normalize_root_contract` | PASS |
| 23 | `tests/test_agent_cost_model.py::test_acp_describe_declares_model_control_agent_config` | PASS |
| 24 | `tests/test_agent_cost_model.py::test_cli_describe_model_control_matches_argv_behavior` | PASS |
| 25 | `tests/test_agent_cost_model.py::test_agents_view_has_honest_model_control_copy` | PASS |
| 26 | `tests/test_git_hardening.py::test_manual_merge_persists_result_for_ui` | PASS |
| 27 | `tests/test_git_merge_api.py::test_merge_blocks_dirty_baseline_without_touching_user_files` | PASS |
| 28 | `tests/test_settings_merge_ui.py::test_policy_git_fields_roundtrip_preserves_other_policy` | PASS |
| 29 | `tests/test_git_hardening.py::test_legacy_ui_entry_available_only_with_env` | PASS |
| 30 | `tests/test_e2e_behavior.py::test_second_task_sees_merged_baseline` | PASS |
| 31 | `tests/test_git_hardening.py::test_runner_never_reports_merged_event_on_failed_merge` | PASS |
| 32 | `tests/test_plan_acceptance_scan.py::test_unique_candidate_backfills_acceptance_path` | PASS |
| 33 | `tests/test_plan_acceptance_scan.py::test_multiple_candidates_keep_path_and_warn` | PASS |
| 34 | `tests/test_plan_acceptance_scan.py::test_existing_exact_path_is_left_alone` | PASS |
| 35 | `tests/test_plan_acceptance_scan.py::test_glob_paths_are_not_rewritten` | PASS |
| 36 | `tests/test_plan_acceptance_scan.py::test_output_backfill_rewrites_generated_acceptance` | PASS |
| 37 | `tests/test_plan_acceptance_scan.py::test_candidate_scan_never_escapes_project_root` | PASS |
| 38 | `tests/test_runner_processes.py::test_runner_records_process_audit_result_and_event` | PASS |
| 39 | `tests/test_runner_processes.py::test_process_audit_can_be_disabled_by_policy` | PASS |
| 40 | `tests/test_runner_processes.py::test_short_timeout_can_still_opt_out_of_audit`<br>`tests/test_runner_processes.py::test_short_timeout_is_audited_by_default` | PASS（全量 764 覆盖） |
| 41 | `tests/test_newtask_dialog_contract.py::test_quick_task_persists_advanced_fields` | PASS |

第二轮主线全量运行已执行上述全部文件（679 passed / 10 skipped / 0 failed）。上述 121
个为第二轮定向用例基线；第三轮新增真实浏览器流程、默认策略/文档契约与审查补丁测试，
最终重试闸门轮主线全量为 **764 passed / 10 skipped / 0 failed**（171.72s）。第二轮各文件用例数：
`tests/test_git_hardening.py`（15）、`tests/test_git_merge_api.py`（9）、
`tests/test_isolation_modes.py`（8）、`tests/test_agent_cost_model.py`（10）、
`tests/test_acceptance_flexible.py`（13）、`tests/test_process_audit.py`（21）、
`tests/test_ops_worktrees.py`（5）、`tests/test_task_merge_ui_contract.py`（5）、
`tests/test_task_overview_contract.py`（3）、`tests/test_review_merge_contract.py`（4）、
`tests/test_newtask_dialog_contract.py`（4）、`tests/test_project_dedup.py`（7）、
`tests/test_project_api_dedup.py`（3）、`tests/test_settings_merge_ui.py`（3）、
`tests/test_e2e_behavior.py`（2）、`tests/test_plan_acceptance_scan.py`（6）、
`tests/test_runner_processes.py`（3），合计 121。
