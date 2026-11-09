# MultiAgent v2 真实浏览器 E2E 最终重试闸门复跑报告（8 flows）

> 执行角色：批次 D4 最终台账子代理
> 执行日期：2026-11-09 10:49（+08:00）
> 运行日志：`.e2e/flows-final3.log`
> 写入范围：本报告；未修改任何源码、脚本、测试、config。

## 1. 结论

| 项 | 结果 |
| --- | --- |
| 运行命令 | `~/multi_xuanwo\.venv\Scripts\python.exe scripts\v2_flows.py` |
| 退出码 | **0** |
| 总结果 | **RESULT: PASS** |
| Flow | **8/8 PASS** |
| Checks | **182/182 passed** |
| 未捕获异常 | **0** |
| Console error | **0** |
| Network warning | **1**（预期：冲突重试 HTTP 409，`task-abe3db42`，非致命） |
| 截图 | **12/12** 存在且非空，均为有效 PNG、1440×900 |
| 运行日志 | `.e2e/flows-final3.log`（22,306 B） |

截图写入区间为 2026-11-09 10:49:36–10:49:44，均在
`.e2e/ui-v2/flows/`。本次运行日志已完整落盘，8/8、182/182、12 张截图和
409 warning 均可直接对照日志复核。

Flow 断言合计 178 项；另有启动检查 2 项（应用外壳、项目自动选中）与全局检查
2 项（无未捕获异常、无 console error），合计 **182** 项全部通过。

## 2. 构建与运行环境

| 项 | 值 |
| --- | --- |
| 前端构建产物 | `multiagent/web/dist/index.html`，**561 B**，构建时间 **2026-11-09 10:44:08** |
| `index.html` SHA-256 | `3564F1807C132C210989D9EAC8BF5913AD5EFD1475BC85C32EFDD724520A98AA` |
| 入口 JS | `multiagent/web/dist/assets/index-D23LbL2h.js`，**262,962 B** |
| 入口 JS SHA-256 | `D402B323B916FBDF1465AB8EE36825236FB2A0D8C9D2DE8E09F1E5AD9435290B` |
| `dist` 总量 | **40 个文件 / 827,677 B** |
| `assets/` | 39 个文件 / 827,116 B |
| 构建/运行同批 | dist 构建于 10:44:08；8-flow 运行与截图写入于 2026-11-09 10:49:36–10:49:44，运行使用当前 dist |
| 浏览器 | Microsoft Edge 151.0.4129.93，headless 新无头模式，CDP 驱动 |
| 视口 | 1440×900（`v2_smoke.VIEWPORT`） |
| 后端 | 临时后端；临时 `MULTIAGENT_HOME`、临时 Git 仓库、临时 Edge profile（端口 35657） |
| 清理 | 运行结束后临时 home / 后端 / Edge 已清理 |

## 3. 八条 Flow 结果

| # | Flow | 标题 | 结果 | Checks |
| --- | --- | --- | --- | --- |
| 1 | `task-merged` | 任务详情：已合并 | PASS | 11 |
| 2 | `merge-conflict` | 合并冲突面板 | PASS | 20 |
| 3 | `extend-timeout` | 延长超时 | PASS | 19 |
| 4 | `settings-delivery` | 设置：交付与合并 | PASS | 21 |
| 5 | `review-states` | 审阅四状态 | PASS | 16 |
| 6 | `new-task-advanced` | 新建任务高级选项 | PASS | 19 |
| 7 | `missing-branch` | 分支缺失与重建 | PASS | 43 |
| 8 | `duplicate-projects` | 历史重复项目 | PASS | 29 |

## 4. 每条 Flow 覆盖的用户动作与断言摘要

### 4.1 `task-merged` — 任务详情：已合并（11 checks）

- 用户动作：侧栏进入「任务」→ 看板加载 5 张卡片 → 打开「已合并任务 Alpha」。
- 断言：详情标题正确；合并徽章显示
  `已合并到 ma/881747b3-alpha @9bf52135`；分支以 `ma/` 开头且 8 位 sha 与种子一致；
  页面上不存在可见的「同步到工作区」按钮。
- 截图：`task-merged.png`。

### 4.2 `merge-conflict` — 合并冲突面板（20 checks）

- 用户动作：打开「冲突任务 Gamma」；点击冲突面板「重试合并」触发真实 `POST /merge`。
- 断言：面板标题含「合并冲突」；列出冲突文件 `conflict.txt`、任务分支
  `ma/abe3db42-gamma`、基线 `343d8808`、原因 `conflict`；「重试合并」「放弃任务分支」
  「打开工作目录」三个按钮均存在且可用。
- 409 断言：重试合并被 CDP 捕获为 HTTP 409；出现冲突 toast；409 后冲突面板仍在、
  任务详情标题仍在（页面未崩溃）。
- 截图：`task-conflict-before.png`、`task-conflict.png`。

### 4.3 `extend-timeout` — 延长超时（19 checks）

- 用户动作：任务「更多」菜单 →「延长超时」；先提交非法值 1，再提交合法值 120。
- 断言：对话框默认值 1800 有效且说明「下一次」生效；非法值 1 被拒绝
  （`超时时间无效…`），对话框保持打开，API 确认数据库仍为 1800；合法值 120
  提交后对话框关闭，toast 为「超时预算已更新…将从下一次尝试生效。」
- 落库断言：`GET /api/tasks/{tid}` 确认 `timeout_sec == 120`。
- 截图：`timeout-dialog.png`、`timeout-saved.png`。

### 4.4 `settings-delivery` — 设置：交付与合并（21 checks）

- 用户动作：进入设置页 → 在「交付与合并」卡片切换自动合并开关并保存 → 刷新页面 →
  再切回并保存。
- 断言：初始开关为「关闭」，提示含「手动合并」，卡片含「阻止合并」风险说明；
  切到「开启」后提示含「验收通过即」；保存后出现成功 toast，API `policy.json` 的
  `git.auto_merge == true`，且捕获到的 `PUT /api/config/files/policy` 请求体含
  `"auto_merge":true`；刷新后开关仍为「开启」且风险提示保持。
- 回退断言：切回「关闭」并保存后，API 的 `git.auto_merge == false`，提示恢复
  「手动合并」。
- 截图：`settings-merge.png`。

### 4.5 `review-states` — 审阅四状态（16 checks）

- 用户动作：进入审阅页，查看分组与四个任务卡片，点击冲突任务查看合并详情。
- 断言：分组包含「待审阅」「已合并 / 已同步」「失败」；「已合并任务 Alpha」显示
  Badge「已合并」；「未合并任务 Beta」显示「待同步/未合并」；「冲突任务 Gamma」
  显示 Badge「合并冲突」；「已放弃任务 Delta」显示 Badge「已放弃分支」。
- 详情断言：冲突任务的审阅详情显示「合并冲突」面板、列出 `conflict.txt`，
  「重试合并」「放弃任务分支」按钮存在且可用。
- 截图：`review-states.png`。

### 4.6 `new-task-advanced` — 新建任务高级选项（19 checks）

- 用户动作：顶栏「新任务」→ 展开「高级选项」→ 填写超时 1234、最大尝试次数 2 →
  「仅创建」；随后重新打开并提交非法超时 1。
- 落库断言：API 能按标题找到新任务，`timeout_sec == 1234`、`max_attempts == 2`；
  创建成功后对话框关闭。
- 非法值断言：内联提示「超时预算需在 30–86400 之间」，输入框 `aria-invalid=true`，
  对话框保持打开，任务列表数量不变（未创建新任务）。
- 截图：`newtask-advanced.png`、`newtask-invalid.png`。

### 4.7 `missing-branch` — 分支缺失与重建（43 checks）

- 构造：种子任务 `ma/1f73072a-epsilon` 的 worktree 目录保留，但用
  `git update-ref -d refs/heads/ma/1f73072a-epsilon` 删除分支 ref，形成
  “worktree 还在、分支已不存在”的真实缺失状态；任务 commit 仍可被记录 base 找到。
- 用户动作：打开「缺失分支任务 Epsilon」→ 查看诚实终态面板 → 点击「重建分支」→
  在确认框确认 → 重建成功后点击「合并到基线」。
- 409 结构化断言：直接调用 `POST /api/tasks/{tid}/merge` 返回 **HTTP 409**；顶层
  `reason=missing_branch`、`code=missing_branch`，并带正确的 `branch`、`task_id`；
  `error.details` 同时带 `branch`、`task_id`、`base`。
- UI 断言：任务详情显示 **“分支已不存在，无法合并”**，并显示任务分支、base、原因
  `missing_branch`；头部「合并到基线 / 重试合并」不存在或已禁用；面板只提供
  「重建分支」「放弃任务」「打开工作目录」，不显示「重试合并」。
- 重建断言：确认框标题含「重建任务分支」，说明按 `commit/base` 重建并提示未提交内容
  可能丢失；确认后 `POST /api/tasks/{tid}/rebuild-branch` 返回 **200**，toast 显示
  `ma/1f73072a-epsilon @86436737`，面板消失，「合并到基线」恢复可用。
- 重建后合并：点击「合并到基线」并确认，`POST /api/tasks/{tid}/merge` 返回 **200**；
  API 确认 `merge.merged=true`；合并提交 `225ae097`；`missing.txt` 已进入基线且内容
  与任务提交一致。
- 截图：`missing-branch-before.png`、`missing-branch-after.png`。

### 4.8 `duplicate-projects` — 历史重复项目（29 checks）

- 种子：后端启动前直接向临时 home 的 SQLite 写入两条同目录历史项目
  「历史重复项目甲 / 乙」，目录为 `legacy-shared`；不经过会返回 409 的创建 API。
- 用户动作：进入项目页 → 查看重复横幅与成员 → 勾选「仅显示重复项目」→ 打开重复组
  内「历史重复项目甲」→ 返回项目页并恢复筛选。
- 断言：横幅显示「发现 1 组重复项目」「历史数据未删除」「仅显示重复项目」并展示
  重复目录 `legacy-shared`；重复组恰好两个成员；项目页共 3 张卡片（两个历史项目 +
  主项目 `project`）；筛选后只保留两个重复项目、主项目被隐藏且复选框为勾选状态。
- 打开与恢复：打开「历史重复项目甲」后顶栏项目名与概览 `h1` 均切换为目标项目，
  已离开项目页；返回项目页后横幅仍在，筛选状态复位（视图重新挂载），取消筛选后
  恢复全部 3 张卡片。
- 截图：`duplicate-projects.png`。

## 5. 截图清单

目录：`~/multi_xuanwo\.e2e\ui-v2\flows\`

| 文件 | 大小（B） | 尺寸 | 写入时间（+08:00） |
| --- | --- | --- | --- |
| `task-merged.png` | 127,500 | 1440×900 | 2026-11-09 10:49:36 |
| `task-conflict-before.png` | 114,591 | 1440×900 | 2026-11-09 10:49:37 |
| `task-conflict.png` | 114,320 | 1440×900 | 2026-11-09 10:49:39 |
| `timeout-dialog.png` | 127,435 | 1440×900 | 2026-11-09 10:49:39 |
| `timeout-saved.png` | 116,968 | 1440×900 | 2026-11-09 10:49:39 |
| `settings-merge.png` | 129,980 | 1440×900 | 2026-11-09 10:49:40 |
| `review-states.png` | 122,631 | 1440×900 | 2026-11-09 10:49:41 |
| `newtask-advanced.png` | 130,678 | 1440×900 | 2026-11-09 10:49:41 |
| `newtask-invalid.png` | 159,744 | 1440×900 | 2026-11-09 10:49:42 |
| `missing-branch-before.png` | 125,139 | 1440×900 | 2026-11-09 10:49:43 |
| `missing-branch-after.png` | 134,527 | 1440×900 | 2026-11-09 10:49:44 |
| `duplicate-projects.png` | 89,016 | 1440×900 | 2026-11-09 10:49:44 |

以上 **12 个文件**均通过 PNG 签名与 IHDR 尺寸校验，非空、1440×900。
`missing-branch-before.png` 与 `missing-branch-after.png` 证明缺失分支终态与重建后合并
两条路径均有真实截图。

## 6. 异常与控制台结论

- **未捕获异常：0**（`无未捕获异常（0）`）。
- **Console error：0**（`无 console error（0）`）。
- **Network warning：1 条，非致命且为预期行为**：
  `Failed to load resource: the server responded with a status of 409 (Conflict)`
  （`http://127.0.0.1:35657/api/tasks/task-abe3db42/merge`）。该 409 由
  `merge-conflict` flow 主动点击「重试合并」触发，脚本同时断言响应码必须为 409、
  toast 出现、面板与详情页不崩溃。它不是失败信号。
- 除上述冲突 409 外，没有其它 console error 或未捕获异常。

## 7. 失败项

无 FAIL：8/8 flow 通过，182/182 checks 通过。运行日志、12 张截图和 409 warning
均落在 `.e2e/` 下，可直接复核。

## 8. 残留风险与边界

- 本 E2E 使用 `multiagent.testing.fake_agent` 假 CLI Agent 与临时 Git 仓库，验证的是
  Web UI、API、SQLite 与真实 Git 合并链路；不覆盖真实 ACP/模型输出、真实 Agent 的
  流式事件与权限请求。
- 仅覆盖 Windows + Microsoft Edge headless；未覆盖桌面 Electron 壳、安装包与其它浏览器。
- 历史重复项目通过后端启动前直接写库模拟，因为当前 API 对同目录重复登记返回 409；
  该流程验证历史数据的展示、筛选与打开，不改变 409 去重策略。
- `missing-branch` 通过真实 Git ref 删除 + 真实浏览器验证，但 Agent 仍是 fake CLI；
  不覆盖真实 ACP/模型行为。
- P2-2 普通重试闸门由 `tests/test_retry_merge_gate.py` + task UI contract **15 passed**
  覆盖；本次 8-flow 继续验证冲突面板、缺失分支重建与预期 409，不把 409 当失败信号。
- 结果依赖运行时的前端构建产物；若后续再次修改 `webapp/` 源码，需要重新构建并复跑。
- 脚本使用临时 `MULTIAGENT_HOME` 与临时 policy，设置流程的写入不会影响工作区真实配置；
  运行结束已清理临时目录。
