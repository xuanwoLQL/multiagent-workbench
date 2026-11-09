# 发布与安装（源码 2.0.0 · schema 6 · 2026-11-09 10:47 最终构建）

> 本文区分「源码版本」与「已构建产物」。源码版本以 `multiagent/__init__.py` 的
> `__version__ = "2.0.0"` 和 `multiagent/db.py` 的 `SCHEMA_VERSION = 6` 为准。
>
> 本轮为**第三次重打包后的最终构建**：**2026-11-09 10:47（本地时间 UTC+8）**。
> 构建顺序为 `vite build` → `scripts/build_backend.py`（后端 exe 10:47:14）→
> `cd desktop && npm run dist`（setup 10:47:56 / portable 10:47:58）。
>
> **结论：源码 2.0.0 / schema 6；`desktop/release/` 下的 setup、portable 与
> `win-unpacked/` 均为 2026-11-09 10:47 最终构建。包内前端与工作区
> `multiagent/web/dist` 逐文件一致（40/40，827,677 B，SHA256 差异 0），入口为
> `index-D23LbL2h.js` / `index-DRgukb51.css`；包内后端 exe 与
> `desktop/backend-dist` 完全一致（6,215,907 B / `DEB49332…F6D1ED8F`）。
> 00:19 构建及更早的 00:02、22:14、22:24、23:01 构建均为历史，其中 00:19 已
> superseded，不得作为交付。**
>
> 说明：`desktop/release-v2/multiagent-2.0.0-x64.exe` 是更早的历史对照
> （2026-11-08 17:56），本轮没有删除或覆盖，但同样**不是**本轮交付。

## 1. 最终构建（2026-11-09 10:47）

### 1.1 可交付产物

| 产物 | 路径 | 大小 | SHA256 | 写入时间 |
| --- | --- | ---: | --- | --- |
| NSIS 安装包 | `desktop/release/multiagent-2.0.0-x64-setup.exe` | 128,074,777 B（122.1 MiB） | `97486F6689362189B1A0554E67643BE25349F9692FD93517CE6797F9C476F4B3` | 2026-11-09 10:47:56 |
| Portable | `desktop/release/multiagent-2.0.0-x64.exe` | 127,934,507 B（122.0 MiB） | `5DEE13C532286AE7303ED70F705B9E8DC50C3AD270936FA3485AD2098BED4034` | 2026-11-09 10:47:58 |
| 解包目录 | `desktop/release/win-unpacked/` | 417,998,258 B / 220 个文件（398.6 MiB） | 目录不适用 | 重建完成 2026-11-09 10:47:28（目录创建于 2026-11-08 22:14:47） |
| 安装包 blockmap | `desktop/release/multiagent-2.0.0-x64-setup.exe.blockmap` | 134,153 B | `D2E54DEB41F87CB9EF2E70E531DBBBE77EFE4E1CB8E677EEC78827A74F0B8BEF` | 2026-11-09 10:47:57 |

解包目录内的关键文件：

| 文件 | 大小 | SHA256 | 写入时间 |
| --- | ---: | --- | --- |
| `desktop/release/win-unpacked/MultiAgent.exe` | 246,269,952 B | `F14D45EF2B037EECF73C7D1191B2FA1F2F67BBBDEB554931375522ABF19C27AB` | 2026-11-09 10:47:28 |
| `desktop/release/win-unpacked/resources/app.asar` | 78,135 B | `6954A1B7ECE5B12F2D56F95758899B2F33A2D4B75FA185446BCBFE1759ADC6BA` | 2026-11-09 10:47:26 |
| `desktop/release/win-unpacked/resources/backend/multiagent-backend/multiagent-backend.exe` | 6,215,907 B | `DEB49332DD956B2F69241BEC7BFD51109487FBB826C6C76756D45A1EF6D1ED8F` | 2026-11-09 10:47:14 |
| `desktop/backend-dist/multiagent-backend/multiagent-backend.exe`（冻结源） | 6,215,907 B | `DEB49332DD956B2F69241BEC7BFD51109487FBB826C6C76756D45A1EF6D1ED8F` | 2026-11-09 10:47:14 |

- `desktop/backend-dist/multiagent-backend/`：**142 个文件 / 32,462,513 B**。
- 安装包内 `win-unpacked/resources/backend/`：**143 个文件 / 32,462,926 B**，
  即冻结目录 142 个文件加预置的 `README.txt`（413 B）。
- `win-unpacked/resources/backend/multiagent-backend/` 与
  `desktop/backend-dist/multiagent-backend/` 逐文件一致（142/142，SHA256 差异 0）；
  `multiagent-backend.exe` 的 SHA256 相同。
- `MultiAgent.exe` 与 `app.asar` 的哈希与上一轮相同（Electron 壳与桌面端文件
  本轮未变，仅重新打包）；setup/portable 与后端 exe 因重试闸门源码与重新构建而更新。

### 1.2 包内前端 dist 与工作区一致性

包内路径：
`desktop/release/win-unpacked/resources/backend/multiagent-backend/_internal/multiagent/web/dist/`

| 文件 | 大小 | 工作区 SHA256 | 包内 SHA256 | 一致 |
| --- | ---: | --- | --- | --- |
| `index.html` | 561 B | `3564F1807C132C210989D9EAC8BF5913AD5EFD1475BC85C32EFDD724520A98AA` | 同左 | 是 |
| `assets/index-D23LbL2h.js` | 262,962 B | `D402B323B916FBDF1465AB8EE36825236FB2A0D8C9D2DE8E09F1E5AD9435290B` | 同左 | 是 |
| `assets/index-DRgukb51.css` | 49,840 B | `C590A5FED089EA35E4AE62BBC77ACE898E685705D9ED5F22BA4B5EA69BF6291C` | 同左 | 是 |

- 包内 dist 共 **40 个文件 / 827,677 B**，与工作区 `multiagent/web/dist` 的
  文件数、总字节数一致；逐文件 SHA256 比较差异为 **0**。
- 包内入口文件为 **`index-D23LbL2h.js`** 和 **`index-DRgukb51.css`**；
  `index.html` 引用 `/static/dist/assets/index-D23LbL2h.js` 与
  `/static/dist/assets/index-DRgukb51.css`。
- 包内不存在 `index-BiockU1o.js`、`index-DhRunDb7.js`、`index-BMnaSU7m.js`、
  `index-B6iBPHig.js`、`index-CyVfnjsD.js` 等旧入口。

### 1.3 构建环境与时间线

| 项 | 值 |
| --- | --- |
| 源码版本 / schema | `2.0.0` / `6` |
| Python（`.venv`） | 3.11.0 |
| PyInstaller | 6.22.3 |
| Node / npm | v26.12.1 / 11.20.0 |
| Electron | 44.10.2 |
| electron-builder | 25.1.8 |
| Vite | 6.4.5 |
| 源码文件最新落盘 | 2026-11-09 10:37:04（`multiagent/api/app.py`；另含 `orchestrator/engine.py` 10:35:41、`api/routes_git.py` 00:13:30、`workspace/isolation.py` 00:08:39） |
| 前端最终构建完成 | 2026-11-09 10:44:08（`multiagent/web/dist` 最新写入；入口 `index-D23LbL2h.js`） |
| 后端最终冻结完成 | 2026-11-09 10:47:14（`multiagent-backend.exe`） |
| `win-unpacked` 打包完成 | 2026-11-09 10:47:28 |
| NSIS 安装包完成 | 2026-11-09 10:47:56 |
| portable 完成 | 2026-11-09 10:47:58 |

当前 `.py` 源文件的最新 mtime 为 **2026-11-09 10:37:04**，早于新后端 exe 的
**2026-11-09 10:47:14**；复核未发现晚于新后端 exe 的 Python 源文件。

`desktop/release/` 当前包含上述最终产物，以及 electron-builder 生成的
`builder-debug.yml`（构建元数据，非交付物）。`desktop/release-v2/` 仍保留更早的
portable（`multiagent-2.0.0-x64.exe`，116,303,771 B，SHA256
`F2A6F0FB1CECA146E0FA5D19C9BA8400FAC26F2AEC34A9883762EFB3B7C42DB9`，
写入时间 2026-11-08 17:56），本轮没有删除、覆盖或改写该历史对照；两个目录
都不要当作本轮交付。

### 1.4 构建历史与替代关系

| 产物 | 22:14（历史） | 22:24（历史） | 23:01（历史） | 00:02（历史） | 00:19（历史，superseded） | 10:47（当前交付） |
| --- | --- | --- | --- | --- | --- | --- |
| `multiagent-backend.exe` | 6,202,885 B / `60880307…D489EC8` | 6,204,099 B / `E2674FE5…98EBAA46` | 6,208,820 B / `44FF6C7B…13E30A` | 6,213,707 B / `B6E48B38…D1FEDE` | 6,215,140 B / `0D17B7BF…DAA6980E` | **6,215,907 B / `DEB49332…F6D1ED8F`** |
| `multiagent-2.0.0-x64-setup.exe` | 128,060,073 B / `00796E96…ED704965` | 128,061,059 B / `62DCB9F2…8F57E7DD` | 128,065,496 B / `E6FC5D90…AB519048` | 128,071,582 B / `DE147524…F85BE513` | 128,074,395 B / `11FE558B…B1AC428D` | **128,074,777 B / `97486F66…C476F4B3`** |
| `multiagent-2.0.0-x64.exe` | 127,919,807 B / `79807897…63EC126B6` | 127,920,785 B / `2B8F8A41…E83ACCC4` | 127,925,233 B / `6C179B5B…67A5D6F0` | 127,931,315 B / `2B1B9F89…419DA23E` | 127,934,125 B / `D12DC1B4…33C18993` | **127,934,507 B / `5DEE13C5…8BED4034`** |
| 前端入口 | `index-BMnaSU7m.js` / `B5DB5AC7…31ED2B` | 同左 | `index-DhRunDb7.js` / `42CB4ED6…68EECF79` | `index-BiockU1o.js` / `40CBE767…0B8B08` | `index-BiockU1o.js` / `40CBE767…0B8B08` | **`index-D23LbL2h.js` / `D402B323…9435290B`** |
| `backend-dist/` 目录 | 142 文件 / 32,442,527 B | 142 文件 / 32,443,741 B | 142 文件 / 32,448,571 B | 142 文件 / 32,459,677 B | 142 文件 / 32,461,110 B | **142 文件 / 32,462,513 B** |
| `win-unpacked/` 目录 | 220 文件 / 417,978,272 B | 220 文件 / 417,979,486 B | 220 文件 / 417,984,316 B | 220 文件 / 417,995,422 B | 220 文件 / 417,996,855 B | **220 文件 / 417,998,258 B** |
| `MultiAgent.exe` | `F14D45EF…C27AB` | 不变 | 不变 | 不变 | 不变 | **不变（Electron 壳未改）** |
| `app.asar` | `6954A1B7…DC6BA` | 不变 | 不变 | 不变 | 不变 | **不变（桌面端文件未改）** |

当前交付（10:47 构建）的证据：

- 新后端 exe 的 mtime 为 **2026-11-09 10:47:14**，晚于当前 `.py` 源文件的最新
  mtime **2026-11-09 10:37:04**（`multiagent/api/app.py`），也晚于
  `multiagent/orchestrator/engine.py` 的 **10:35:41**；复核未发现晚于新 exe 的 Python 源文件。
- 包内 `resources/backend/multiagent-backend/` 与 `desktop/backend-dist/multiagent-backend/`
  逐文件一致（142/142，SHA256 差异 0）；包内后端 exe 与冻结源均为
  `DEB49332DD956B2F69241BEC7BFD51109487FBB826C6C76756D45A1EF6D1ED8F`。
- 包内 dist 与工作区 `multiagent/web/dist` 逐文件一致（40/40，827,677 B，
  SHA256 差异 0），入口为 `index-D23LbL2h.js`。

历史记录（非本轮结论）：

- 00:19 构建已被 10:47 构建替代：其后端 6,215,140 B / `0D17B7BF…DAA6980E`、
  setup `11FE558B…`、portable `D12DC1B4…` 均为历史；00:02 及更早的 22:14、22:24、
  23:01 同样仅作追溯。
- 23:01 历史构建曾从 `PYZ.pyz` 提取模块，确认包含 `multiagent.gitops.service`
  （含 `_unstage_control_plane`）、`multiagent.agents.process_audit`、
  `multiagent.api.routes_validation`、`multiagent.api.routes_git`、
  `multiagent.api.routes_files`、`multiagent.tasks.service`、
  `multiagent.orchestrator.runner`、`multiagent.presets`。本轮未重复反编译，
  以 mtime 时序与逐文件一致性为证据；模块级细节见证据文件。
- gitops 提交修复的回归测试为上一轮执行（非本轮）：
  `tests/test_git_hardening.py::test_commit_succeeds_when_project_gitignore_ignores_control_plane`
  与 `::test_commit_unstages_nested_control_plane_without_gitignore`（2 passed）。

## 2. 本轮关键变更（源码，已合入工作树）

1. **自动合并诚实化（默认先审后合）**：worktree 任务验收通过后在 `ma/<task>` 分支提交；
   `git.auto_merge` 默认 **false**（先审后合），显式开启才执行 `git merge --no-ff`；
   关闭时记录 `skipped/auto_merge_disabled`，不伪装成已合并。任务详情/审阅页按
   `merge_status` 显示「已合并 / 未合并 / 冲突 / 已放弃」。
   需要 DAG 连续交付的项目显式设 `git.auto_merge=true`，并建议保持
   `require_clean_tree=true`、`keep_worktree=true`。
2. **提交排除控制面（含 `.gitignore` 回归修复）**：`commit_worktree` 先执行
   `git add -A -- .`，再由 `_unstage_control_plane` 把索引里任意层级的
   `.multiagent` 目录/文件 `git reset` 出暂存区。不能把
   `:(exclude).multiagent` 直接写进 `git add` 的 pathspec：项目自己的
   `.gitignore` 忽略 `.multiagent/` 时，git 会把它当成“显式添加被忽略路径”并
   以退出码 1 导致整个任务提交失败。修复后无论项目有没有 `.gitignore`，
   控制面都不会进入 commit；回归测试见 `tests/test_git_hardening.py`。
3. **脏树阻断**：`git.require_clean_tree=true` 时，基线 `dirty_paths` 非空直接阻断合并，
   返回 409 + `dirty_files`，不把人工未提交改动混进 Agent 提交；`.multiagent/` 不算脏。
4. **冲突 UI 与放弃分支**：合并冲突执行 `git merge --abort`，返回结构化 409
   （`conflict/files`）；任务详情/审阅页内联「合并问题」面板，支持重试合并、放弃任务分支、
   打开工作目录。`discard-worktree` 支持 `delete_branch`，放弃后状态为 `discarded`。
5. **进程审计**：Agent 调用前后审计新增监听端口，只回收能证明属于本任务 `workdir` 的
   进程，判断不了归属的只告警；`processes.audit_enabled/reap_orphans` 可配置，
   计划超时 1..4 秒的极短任务默认不启动审计（`processes.min_timeout_sec=5`；
   0 表示不限时，仍审计），保护硬超时。
6. **worktree 清理**：`git.keep_worktree=false` 时合并成功即回收目录；`maintenance`
   按 `git.worktree_retention_days`（默认 14 天）清理已合并且过期的 worktree，任务分支
   与提交永远保留。
7. **验收 glob、诊断与计划扫描**：产出/验收支持 glob（`glob_min_files`）；精确路径失败时
   返回 `candidates`/`suggested`，只做诊断、不自动放行；任务详情提供「修正验收路径」
   入口。批量计划创建前扫描项目根目录，精确路径的唯一同名候选自动回填到
   `outputs`/验收条件，多候选/无候选只告警。
8. **WebSocket token 子协议**：浏览器用
   `Sec-WebSocket-Protocol: multiagent.token.<base64url(token)>` 传递 token，URL 不再
   携带 token；旧 `?token=` 仍兼容。
9. **legacy 入口收敛**：`/legacy`、`/static/index.html` 默认 404；只有
   `MULTIAGENT_UI=legacy` 时才开放 `/legacy`。
10. **项目目录判重**：导入/新建按规范化绝对路径判重，同一目录重复登记返回 409 +
    `existing_project_id`，不会产生两个指向同一目录的项目。
11. **成本来源诚实**：`task.result.cost.source` 区分 `reported` / `estimated` /
    `unreported`；`unreported` 时金额 0 表示未知，不代表免费。
12. **模型控制诚实**：ACP v1 不发送请求级 `model`；CLI 仅在 `args` 含 `{model}` 时注入；
    适配器 `describe()` 返回 `model_control`，控制台按同一语义展示模型实际由谁控制。
13. **重试/超时语义**：重试是文件级续跑（恢复 `write_scope` 内半成品），不是 ACP
    session resume；「延长超时」只对下一次尝试生效，不延长正在运行的调用。
14. **新建任务高级选项**：`timeout_sec`（30–86400 秒）与 `max_attempts`（1–10）可显式
    填写，留空继承任务类型默认。

### 2.1 00:19 硬化补丁（历史）

本轮在 00:02 构建之后继续硬化 Git 合并、隔离与重建失败路径，并重新冻结后端、
重打安装包：

1. **missing_branch workdir 缺失优先**：分支/worktree 缺失时优先按
   `missing_branch` 处理，避免被其他原因掩盖。
2. **merge_status=missing_branch**：合并结果与任务状态明确返回
   `missing_branch`，不再落入模糊失败。
3. **ref_unavailable 禁止 HEAD 假成功**：ref 不可用时不得把 HEAD 当作合并成功，
   必须返回不可用/失败。
4. **rebuild 失败回滚**：重建失败时回滚，避免留下半成品 worktree 或错误状态。
5. **标题变更保持记录分支**：任务标题变化不应丢失已记录的分支关联。
6. **create_worktree 拒绝未确认目录**：未确认的目录不得直接创建 worktree，
   防止误建/误覆盖。
7. **全量 pytest（当时）**：**758 passed / 10 skipped / 0 failed**（171.68s；
   证据 `.e2e/pytest-final3.log`）。

### 2.2 10:47 重试闸门补丁

本轮在 00:19 基础上新增 `merge_*` 重试闸门，并第三次重打包：

1. **merge_* blocked 禁止普通 retry**：`merge_*` 处于 blocked 状态时，
   普通重试入口不得再次触发合并。
2. **API 409**：后端对 blocked 状态的普通重试返回 **409**，不再静默接受。
3. **UI 隐藏重试**：任务详情/审阅界面在 blocked 状态下隐藏普通重试按钮，
   避免用户误操作。
4. **重试闸门测试**：**15 passed**（覆盖 merge_* blocked / API 409 / UI 隐藏重试）。
5. **全量 pytest**：**764 passed / 10 skipped / 0 failed**（171.72s；
   证据 `.e2e/pytest-final4.log`）。

## 3. 本轮验证

### 3.1 构建与产物验证（最终构建实际执行）

| 检查 | 结果 |
| --- | --- |
| 构建顺序 | `vite build` → `.\.venv\Scripts\python.exe scripts\build_backend.py`（后端 exe 10:47:14）→ `cd desktop && npm run dist`（setup 10:47:56 / portable 10:47:58） |
| 前端 dist | 40 文件 / 827,677 B；入口 `assets/index-D23LbL2h.js`（262,962 B，SHA256 `D402B323…9435290B`）；`index.html` 561 B；CSS `index-DRgukb51.css` 不变 |
| 包内 dist 与工作区比对 | 40/40 文件、827,677 B 逐文件 SHA256 一致，差异 0；`index-D23LbL2h.js` SHA256 与工作区相同 |
| 后端冻结 | `multiagent-backend.exe` 6,215,907 B / `DEB49332DD956B2F69241BEC7BFD51109487FBB826C6C76756D45A1EF6D1ED8F`，mtime 10:47:14；包内与 `desktop/backend-dist` 完全一致 |
| 包内后端目录 | `backend-dist/multiagent-backend/` 142 文件 / 32,462,513 B；包内 `resources/backend/` 143 文件 / 32,462,926 B（含 `README.txt` 413 B） |
| 解包目录 | `win-unpacked/` 220 文件 / 417,998,258 B |
| `verify_package.ps1` | 脚本 SHA256 `A5482008956FABBF38F942A77961DFFB12D97939C1BF81CB4D1C5E32E361E943`；连续 3 次 `exitCode=0`、`backendHealth=true`、`gracefulClose=true`、`forcedKill=false`、`closeMethod=CloseMainWindow`、`mainLeftOver=none`、`backendLeftOver=none`、`electronLeftOver=none`、`portStillListening=false`、`portBindOk=true`、`portListenPids=none`、`netstatPid0Only=false`、`exitReason=clean_graceful_close`、`gracefulWaitSeconds≈1.1` |
| 打包版 `/api/health` | `version=2.0.0`、`schema_version=6`（其余字段见证据文件） |
| 源码时序 | 当前 `.py` 源文件最新 mtime 2026-11-09 10:37:04，早于新后端 exe 10:47:14；无晚于新 exe 的 Python 源文件 |
| Authenticode | setup / portable / `MultiAgent.exe` / `multiagent-backend.exe` 均为 `NotSigned`（与 `cscInfo=null` 日志一致）；未伪造签名 |
| 全量 pytest | **764 passed / 10 skipped / 0 failed**（171.72s；证据 `.e2e/pytest-final4.log`） |
| merge_* 重试闸门测试 | **15 passed**（blocked 禁止普通 retry、API 409、UI 隐藏重试） |
| docs policy pytest | `.\.venv\Scripts\python.exe -m pytest -o addopts= -q tests/test_docs_policy_defaults.py tests/test_docs_legacy_markers.py` → **6 passed** |

### 3.2 历史抽查（更早文档子任务，未在本轮重跑）

> 以下测试结果来自更早的文档子任务，保留用于追溯；当前 10:47 构建的全量 pytest
> 为 **764 passed / 10 skipped / 0 failed**（证据 `.e2e/pytest-final4.log`），
> 00:19 构建当时为 758 passed / 10 skipped / 0 failed（`.e2e/pytest-final3.log`）；
> docs policy 两项为 **6 passed**。

| 项 | 结果 |
| --- | --- |
| 针对性 pytest | 41 passed / 0 failed（覆盖 Git 硬化、合并 API、隔离、进程审计、验收 glob/计划扫描、成本/模型、项目判重、UI 契约、E2E） |
| 文档对齐 | README / RELEASE / USAGE-V2 / V2-ARCHITECTURE 已按当前源码更新；逐条缺陷见 `docs/DEFECTS-V2.md` |

代表性抽查命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q `
  tests/test_git_hardening.py::test_commit_excludes_control_plane_without_gitignore `
  tests/test_git_merge_api.py::test_merge_conflict_returns_structured_409_and_aborts `
  tests/test_process_audit.py::test_unowned_new_listener_is_only_reported_never_killed `
  tests/test_acceptance_flexible.py::test_file_exists_glob_passes_on_actual_layout `
  tests/test_project_api_dedup.py::test_import_same_directory_twice_returns_409_with_existing_id

.\.venv\Scripts\python.exe -m pytest -q `
  tests/test_isolation_modes.py::test_worktree_baseline_is_copied_once_from_worktree `
  tests/test_ops_worktrees.py::test_unmerged_old_worktrees_are_never_removed `
  tests/test_agent_cost_model.py::test_cli_adapter_never_reports_cost `
  tests/test_task_merge_ui_contract.py::test_task_detail_uses_merge_state_instead_of_sync `
  tests/test_newtask_dialog_contract.py::test_create_task_persists_explicit_advanced_fields

.\.venv\Scripts\python.exe -m pytest -q `
  tests/test_plan_acceptance_scan.py `
  tests/test_runner_processes.py `
  tests/test_git_hardening.py::test_runner_never_reports_merged_event_on_failed_merge `
  tests/test_newtask_dialog_contract.py::test_quick_task_persists_advanced_fields
```

完整抽查清单与逐条缺陷状态见 [DEFECTS-V2.md](DEFECTS-V2.md)。

## 4. 从源码重建（命令参考；本轮实际执行了构建步骤）

```powershell
# 1) 前端构建（输出到 multiagent/web/dist；如需类型检查可先执行 npx tsc -b --noEmit）
cd ~/multi_xuanwo\webapp
npx vite build

# 2) 自包含后端（onedir）
cd ~/multi_xuanwo
.\.venv\Scripts\python.exe scripts\build_backend.py

# 3) Electron 安装包 + portable（输出到 desktop/release）
cd ~/multi_xuanwo\desktop
npm run dist
```

- `node_modules` 已存在，本轮未执行 `npm install`，未修改任何机器级 npm 配置。
- 本轮未单独记录 `npx tsc -b --noEmit` 的退出码；需要时在 `webapp/` 下补跑。
- 本轮 `npm run dist` 一次成功（生成 NSIS + portable），未使用
  `npm run dist:portable` 降级。
- 如果只需要 portable，可用 `npm run dist:portable`；历史对照目录用
  `npm run dist:v2` / `npm run dist:portable:v2`（输出到 `desktop/release-v2`，
  与 `desktop/release/` 不要混用）。

打包后可用 `desktop\tools\verify_package.ps1` 做启动/单实例/退出冒烟。发布前请：

1. 在 `webapp/` 执行 `npx tsc -b --noEmit && npx vite build`；
2. 跑全量 `pytest`（主线负责）；
3. 用 `scripts/build_backend.py` 重新冻结后端；
4. `npm run dist` 重新生成 `desktop/release/`；
5. 用 `diff -r` 或 SHA256 确认包内
   `resources/backend/multiagent-backend/_internal/multiagent/web/dist`
   与 `multiagent/web/dist` 一致；
6. 记录新产物的文件名、大小、SHA256 和构建时间，替换本文第 1 节；
7. 不要把 `release-v2` 或本轮之前的构建当作本轮交付。

## 5. 已知限制

1. **未签名**：setup、portable、`MultiAgent.exe` 与冻结后端 exe 均为 `NotSigned`，
   首次运行可能触发 SmartScreen，选择「更多信息 → 仍要运行」。本轮日志明确为
   `no signing info identified, signing is skipped` / `cscInfo=null`；没有签名证书，
   不得声称签名有效。签名就绪检查见 5.1。
2. 应用内暂未集成自动更新；升级需重新下载安装包。
3. 首次启动需要可用的 Agent CLI（Codex / Claude Code / OpenCode / DSH 任一）。
4. 打包版数据根固定 `%APPDATA%\multiagent-desktop`（ASCII 路径）；开发模式用仓库根。
5. 隔离副本模式下，任务失败/取消不会自动同步，需要在任务详情里手动「同步」。
6. worktree 自动合并受 `git.require_clean_tree` 约束；基线脏树或真实冲突会阻断，
   需要人工处理后再合并。系统不会自动解决冲突。
7. 进程审计只回收可证明归属的监听进程；prober 不支持或归属不明时只告警，不保证
   清理所有残留端口；计划超时 1..4 秒的极短任务默认不做审计（0 表示不限时，仍审计）。
8. 本轮已完成构建、内容与哈希、`verify_package.ps1` 连续 3 次冒烟与
   `/api/health` 验证；未在打包版中执行真实 Agent 任务 E2E，需要时另行补做。
9. 本文哈希对应 2026-11-09 10:44:08 构建的 `multiagent/web/dist`。若后续有人重新
   `npx vite build`，工作区入口名与哈希可能变化，但本轮安装包内是
   `index-D23LbL2h.js`（SHA256 `D402B323…9435290B`）。
10. PyInstaller 可能提示 `Hidden import "tzdata" not found!`；当前依赖路径未使用该包，
    若后续需要完整 zoneinfo 时区数据，应显式安装 `tzdata`。

### 5.1 签名就绪流程（当前未执行签名）

2026-11-09 10:47 构建复测现状：4 个交付 exe 仍为 `NotSigned`；本机未安装 `signtool.exe`，
`CurrentUser\My` 与 `LocalMachine\My` 中也没有带私钥的代码签名证书。
`sign_release.ps1 -CheckOnly` 返回 **退出码 2**，输出
`STATUS: NOT READY: no certificate`（脚本 SHA256
`E03E5F2BA31248D06D0266CDC48473DDD3E2BFE3B331A4167F6242CC006B8A53`；证据见
`.e2e/signing-ready/` 与本轮 CheckOnly 输出）。因此本文不提供任何签名有效结论；
SmartScreen 风险仍按第 1 条处理，不得声称签名有效。

脚本：`desktop/tools/sign_release.ps1`。默认目标为当前 4 个产物，可用参数显式覆盖：

| 参数 | 说明 |
| --- | --- |
| `-Setup` / `-Portable` / `-BackendExe` / `-MainExe` | 4 个待签 exe 路径 |
| `-PfxPath` | PFX 文件路径（与 `-Thumbprint` 二选一） |
| `-Thumbprint` | 证书 SHA1 指纹（与 `-PfxPath` 二选一） |
| `-PfxPassword` | PFX 密码；仅传给 signtool，不会写入报告 |
| `-TimestampUrl` | RFC3161 时间戳服务，默认 `http://timestamp.digicert.com` |
| `-CheckOnly` | 只检查，不签名；无证书退出码 2，其它未就绪退出码 1 |
| `-SignToolPath` | 可选，显式指定 `signtool.exe` |
| `-Json` | 仅输出 JSON，便于留证 |

先检查（无证书时退出码 2，绝不签名）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File desktop/tools/sign_release.ps1 `
  -CheckOnly `
  -Setup "desktop\release\multiagent-2.0.0-x64-setup.exe" `
  -Portable "desktop\release\multiagent-2.0.0-x64.exe" `
  -BackendExe "desktop\release\win-unpacked\resources\backend\multiagent-backend\multiagent-backend.exe" `
  -MainExe "desktop\release\win-unpacked\MultiAgent.exe"
```

有证书后执行（PFX 或证书库指纹二选一）：

```powershell
# PFX
powershell -NoProfile -ExecutionPolicy Bypass -File desktop/tools/sign_release.ps1 `
  -PfxPath "C:\secure\codesign.pfx" -PfxPassword "<password>"

# 或：证书已导入 CurrentUser\My / LocalMachine\My
powershell -NoProfile -ExecutionPolicy Bypass -File desktop/tools/sign_release.ps1 `
  -Thumbprint "<40-hex-sha1>"
```

脚本对每个目标执行
`signtool sign /fd sha256 /tr <TimestampUrl> /td sha256`，随后用
`Get-AuthenticodeSignature` 复核，必须全部为 `Valid` 才返回 0；任一失败返回非 0。
脚本只追加 Authenticode 签名，不写注册表、不导入/修改证书库；PFX 以
`EphemeralKeySet` 读取，私钥不落盘。

签名后必须完成的收尾：

1. **4 个 exe 的 SHA256 全部改变**（setup、portable、`MultiAgent.exe`、
   `multiagent-backend.exe`），不能继续引用第 1/3 节的旧哈希。
2. setup 的 blockmap 与签名后的安装包不再匹配；要么重新生成 blockmap，
   要么在 electron-builder 构建阶段完成签名后重新打包。
3. 重新执行 `desktop/tools/verify_package.ps1`，确认启动、`/api/health`、
   优雅退出仍通过。
4. 对 4 个产物重新执行 `Get-AuthenticodeSignature`，记录 `Valid`、签名者与时间戳。
5. 更新本文第 1/3 节与 `docs/PACKAGE-VERIFICATION.md` 第 7/10 节的哈希、大小、
   签名状态和结论，并保留本次未签名基线作为历史。
6. 不要把 PFX、密码或证书私钥提交到仓库；本机当前没有证书，本文不构成签名背书。

注意：如果在打包完成后才签 `MultiAgent.exe` / `multiagent-backend.exe`，
setup/portable 内部已嵌入的副本不会随之改变。要交付“安装后仍然有效签名”的完整链路，
应在 electron-builder 打包前先签后端与主程序，再签最终 setup/portable。
