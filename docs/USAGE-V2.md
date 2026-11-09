# MultiAgent v2 使用指南

本文对应 `webapp/` 中的 v2 React 控制台。旧版三栏控制台与 CLI 字段说明仍保留在
[USAGE.md](USAGE.md)；v2 的代码结构、接口与失败分类见
[V2-ARCHITECTURE.md](V2-ARCHITECTURE.md)。

## 1. 首次打开

### 1.1 没有项目时

首次启动进入欢迎页。填写：

- **项目目录**：任意本地目录，例如 `D:\work\my-project`；
- **显示名称（可选）**：只影响界面显示，不移动目录。

两个按钮的区别：

| 按钮 | 行为 | 接口 |
| --- | --- | --- |
| 打开此目录 | 登记已有目录，保留原文件 | `POST /api/projects/import` |
| 新建工作区 | 登记目录；目录不存在时由后端创建 | `POST /api/projects` |

Git 仓库会自动获得 worktree 隔离能力；普通目录仍可使用 copy 隔离。
旧版控制台默认不暴露：欢迎页不再显示旧入口链接，`/legacy` 与 `/static/index.html`
默认返回 404；只有设置 `MULTIAGENT_UI=legacy` 并重启后才可访问 `/legacy`。

### 1.2 应用外壳

| 区域 | 内容 |
| --- | --- |
| 顶栏 | 项目切换、搜索（Ctrl/Cmd+K）、刷新、开始/停止调度、新任务、设置 |
| 侧栏 | 概览、任务、运行、文件、审阅、记忆、Agent、设置；任务/审阅有计数 |
| 主区 | 当前视图；任务详情在「任务」视图内打开 |
| 状态栏 | 实时连接、调度状态、Agent 可用数、运行/排队数、Git 分支、项目根路径 |

项目切换在顶栏项目名处；切换项目会回到「概览」并关闭当前任务详情。

## 2. 新建任务

入口：顶栏「新任务」、概览「新任务」、命令面板「新建任务」。

### 2.1 基本字段

| 字段 | 说明 |
| --- | --- |
| 任务描述 | 必填。原文进入 `brief`；标题取首行截 60 字 |
| 产出文件 | 逗号分隔。填写后按「文件存在」生成验收条件，并推导写范围 |
| 执行 Agent | 「自动选择」走路由；也可指定已启用的 Agent |
| 任务类型 | 自动判断或手动选代码/文档/调研/审阅/数据/Web/通用 |
| 写入方式 | `Git worktree（推荐）` / `隔离副本` / `直接写入工作区` |
| 写范围（高级） | 逗号分隔 glob，例如 `src/**, tests/**`；留空表示整个工作区 |
| 拆成三步 | 勾选后创建「调研 → 初稿 → 审阅」带依赖的 DAG |

默认值来自 `config/policy.json` 与任务类型配置；v2 新建对话框默认选择
`Git worktree（推荐）`。非 Git 目录或 `git worktree add` 失败时，后端会退化为 copy。

### 2.2 创建方式

| 操作 | 行为 |
| --- | --- |
| 仅创建 | 只建任务，状态 `pending`，不启动调度 |
| 创建并运行 | 建任务后启动项目调度 |
| Ctrl / Cmd + Enter | 等价于「创建并运行」 |
| 勾选三步后创建 | 创建三个任务：调研整理（research）→ 产出初稿（doc）→ 只读审阅（review） |

底部会展示后端返回的 `warnings`，例如 brief 过长、提到 PDF、写范围留空等。警告不会
阻止创建，但应确认。

接口等价物：`POST /api/projects/{pid}/quick-task`。`single` 创建一个任务，`plan` 创建
三步 DAG；`start=true` 时复用项目调度。`project` 模式（九阶段大型项目 DAG）目前只通过
`/api/projects/{pid}/decompose/preview|apply` 或 CLI 使用，v2 对话框未接入。

## 3. 视图逐项

### 3.1 概览

首屏聚合当前项目的状态：

- **统计**：任务总数、运行中、需要你处理（失败/阻塞/等待人工）、完成率；
- **当前运行**：正在跑的 Agent、尝试次数、已运行时间；点击打开任务详情的「活动」；
- **需要你处理**：失败、阻塞、等待人工的任务，最多显示 6 条；
- **队列**：`pending` / `interrupted` 任务，最多显示 8 条；
- **Agent**：执行端、协议、角色、调用次数、启用状态；
- **最近活动**：工作区事件摘要；
- 右上「开始运行」在没有待执行任务时禁用；运行中显示「停止运行」。

### 3.2 任务

右上可切换「看板 / 列表」。

看板五列：

| 列 | 包含状态 |
| --- | --- |
| 待执行 | `pending`、`interrupted` |
| 执行中 | `running` |
| 待人工 | `waiting` |
| 成功 | `success` |
| 失败 · 阻塞 | `failed`、`blocked`、`cancelled`、`skipped` |

工具栏支持：

- 搜索任务；
- 按 Agent、类型、状态筛选；
- 按更新时间或优先级排序；
- 清除筛选；
- 新建任务。

勾选任务后可批量：

| 操作 | 可用状态 |
| --- | --- |
| 运行所选 | `pending`、`interrupted` |
| 重试失败 | `failed`、`blocked`、`interrupted` |
| 取消运行 | `running` |
| 清除选择 | 任意 |

批量操作带二次确认和 Toast 汇总；部分失败不会回滚已成功的操作。

### 3.3 任务详情

点击任务卡片/行进入详情。标题可双击或点铅笔重命名；`Enter` 保存，`Esc` 取消。

主操作随状态变化：

| 状态 | 主按钮 |
| --- | --- |
| `running` | 取消 |
| `pending` | 运行 |
| `waiting` | 验收并合并（Git 任务）/ 验收并同步（copy/shared） |
| `failed` / `blocked` / `interrupted` | 重试 |
| `success` 且 `merge_status=merged` | 再次运行（不再显示同步） |
| `success` 且未合并 / `auto_merge=false`（默认） | 合并到基线 |
| `success` 且非 Git / copy 可同步 | 同步到工作区 |
| 其他 | 再次运行 |

> Git worktree 任务成功后不再使用文件级「同步」；按钮由合并状态决定，冲突/脏树会显示
> 「重试合并」或「合并问题」面板。详细状态见 §5.1。

「更多」菜单：

- 打开工作目录（任务已有 `workdir`）；
- 复制任务 ID / 标题；
- 仅验收，不同步（`waiting`）；
- 人工接手（运行中/成功/已取消/已跳过之外）；
- 回滚改动（非运行中且有改动）；
- 跳过任务（非运行中/成功/已跳过）；
- 删除任务（非运行中）。

五个标签页：

| 标签 | 内容 |
| --- | --- |
| 概览 | 任务说明、验收条件、运行记录、项目产物、交接说明、最近状态、任务信息 |
| 改动 | 文件改动列表、统一 diff、同步到工作区 |
| 活动 | 状态变化、Agent 调用、工具事件，按时间倒序 |
| 日志 | 选择 run，查看 Prompt / stdout / stderr / 运行错误 |
| 上下文 | 隔离模式、写范围、超时/尝试/优先级/角色、输入产出、依赖 |

### 3.4 运行

汇总卡片：运行次数、成功率、总耗时、总 Token（含累计成本）。

表格列包括状态、Agent、任务、尝试、开始、耗时、Token、成本、退出码。支持：

- 按 Agent、状态筛选；
- 搜索任务/Agent；
- 按开始时间或耗时排序；
- 点击行在右侧打开详情，`Esc` 关闭；
- 详情内切换 Prompt / stdout / stderr，可复制/下载；
- 有运行中调用时顶部显示「实时 · 3s」；「停止」会停止当前项目调度并终止运行中的调用。

如果运行列表接口不可用，页面会显示「回退数据源」，从 dashboard 任务和任务详情聚合。

### 3.5 文件

只读工作区浏览器：

- 左侧文件树懒加载，点目录展开；面包屑可回到上层；
- 顶部搜索框按文件名或内容搜索；`Esc` 清空搜索；
- 右侧预览文本，带行号，可切换自动换行、复制路径/内容；
- 二进制或超大文件只显示说明，不强行渲染；
- 大目录有单次加载上限，可继续加载；
- 桌面壳中可「打开项目目录」；浏览器中只提供只读预览。

此视图不会修改任何文件，也不能上传/删除/重命名。

### 3.6 审阅

审阅队列只显示**有文件改动记录**的任务，分三组：

| 分组 | 条件 |
| --- | --- |
| 待审阅 | 未合并的 Git 任务、合并冲突/阻断，或 `success` 且仍有未同步改动 |
| 已合并 / 已同步 | `merge_status=merged`，或 `success` 且 `unsynced=0`，或 `shared` 直接写入 |
| 失败 | `failed` / `blocked` / `waiting` 且有改动，或任务分支已放弃（`discarded`） |

Git 任务在详情里显示合并状态和「合并到基线」入口；冲突/脏树会显示内联合并问题面板，
不会继续显示「同步 N 个文件」。状态语义见 §5.1。

操作流程：

1. 左侧选择任务；
2. 查看顶部状态、Agent、类型、待同步数量；
3. 在文件 rail 选择文件，查看右侧统一 diff；
4. 点「同步 N 个文件」；
5. 在确认框核对任务、目标根目录和覆盖说明；
6. 确认后查看 Toast 的 `committed` / `skipped` 汇总。

同步规则见下一节。失败任务也可以手动同步，但确认框会提示同名文件覆盖风险。

### 3.7 记忆

四个标签：

| 标签 | 能力 |
| --- | --- |
| 共享黑板 | 按类型筛选、搜索、新建项目级条目、查看详情与版本历史；`pending` 条目可通过/驳回 |
| 交接说明 | 查看每个任务的「做了什么 / 没做什么 / 遗留问题 / 下一步建议」，可编辑人工备注 |
| 人工确认 | 查看危险操作、验收失败、冲突、额度等审批；按状态筛选或搜索；通过/驳回可附备注 |
| 版本快照 | 创建带标签的全量快照；查看文件数/大小/时间；恢复前二次确认 |

黑板条目默认 `scope=project`，适合记录跨任务决策；任务级上下文由 Agent 交接自动写入。

### 3.8 Agent

每张卡片展示：

- 启用状态、协议；
- 角色与强项；
- 调用次数、耗时、Token、成本；
- 额度上限与用量；
- 「检测」按钮：调用 `POST /api/agents/{id}/check` 做握手，显示连接正常/失败。

这里**没有启用/停用开关**：启用状态来自 `config/agents.json`，避免 UI 制造与真实配置
不一致的假开关。要调整命令、角色、额度，去「设置 → Agent 配置」。

### 3.9 设置

五个分区：

| 分区 | 内容 |
| --- | --- |
| 项目 | 修改显示名/项目名、导入已有目录、新建工作区、删除当前项目 |
| 外观 | 浅色 / 深色 / 跟随系统；本地偏好，立即生效 |
| 并发与策略 | 编辑 `policy.json`（并发、超时、隔离、危险操作、快照保留等），服务端校验后保存 |
| Agent 配置 | 编辑 `agents.json` 等配置文件；JSON 编辑器带语法校验、行列定位、格式化、撤销 |
| 数据 | 数据目录/数据库/运行证据/快照/日志/备份占用；立即备份；预览/执行保留清理 |

删除项目只删除工作台登记、任务、运行证据、快照和 Agent 工作副本；来源目录中的用户
文件不会被删除。该操作不可撤销。

## 4. 键盘快捷键

| 快捷键 | 作用 |
| --- | --- |
| `Ctrl / Cmd + K` | 打开命令面板 |
| `↑` / `↓` | 命令面板选择 |
| `Enter` | 执行命令面板当前项 |
| `Esc` | 关闭命令面板/对话框/弹层；关闭任务详情；关闭运行详情；清空文件搜索；取消标题编辑 |
| `Ctrl / Cmd + Enter` | 新建任务对话框中「创建并运行」 |
| `Enter` | 标题编辑保存 |
| `Esc` | 标题编辑取消 |
| `Ctrl / Cmd + S` | JSON 配置编辑器保存 |
| 双击任务标题 | 重命名 |
| 文件分栏获得焦点后 `←` / `→` | 调整文件树宽度 |

命令面板可搜索并执行：新建任务、开始/停止调度、打开九个视图、切换项目、打开任务。

## 5. 审阅、合并与同步

### 5.1 先看合并状态

审阅页和任务详情都先读 `GET /api/tasks/{tid}/workspace` 的 `merge_status`（由
`task.result.git.merge` 归纳）；前端 `taskMergeState()` 会把
`blocked + reason=dirty_worktree` 细分为 `dirty_blocked`。再决定显示合并还是文件同步：

| 合并状态 | 含义 | 审阅页分组 | 任务详情 |
| --- | --- | --- | --- |
| `merged` | 任务分支已合并到基线 | 已合并 / 已同步 | 「已合并到 <branch> @<sha>」，不显示同步 |
| `auto_merge_disabled` | 策略关闭自动合并（默认先审后合），等待人工 | 通常待审阅（`unsynced=0` 时归入已同步） | 主按钮「合并到基线」 |
| `not_merged` / `pending` | 有提交但尚未合并，或旧任务没有合并记录 | 通常待审阅（`unsynced=0` 时归入已同步） | 同上 |
| `conflict` | 合并冲突，已执行 `merge --abort` | 待审阅 | 「合并问题」面板 + 冲突文件 |
| `dirty_blocked` | 基线脏树阻止合并（后端 `merge_status=blocked`） | 待审阅 | 「合并被阻止」+ 未提交文件 |
| `blocked` | 其它合并阻断 | 待审阅 | 「合并问题」面板 |
| `discarded` | 任务分支已放弃 | 失败 | 终态「任务分支已放弃」，不再显示合并/同步 |
| `null` | copy / shared 或非 Git 项目 | 按 `unsynced` 判断 | 文件级「同步到工作区」 |

### 5.2 推荐流程

1. 任务完成后进入「审阅」；
2. 先看分组，优先处理「待审阅」；
3. 逐文件看 diff，确认改动在写范围内；
4. Git worktree 任务看合并状态：默认先审后合（`auto_merge=false`），未合并点
   「合并到基线」；已合并则无需操作；冲突先按 5.4 处理；
5. copy / shared 任务确认目标根目录后点「同步 N 个文件」；
6. 如果 Toast 显示跳过，展开任务详情或调用 `GET /api/tasks/{tid}/workspace`
   查看 `skipped_details` / `conflicts` / `out_of_scope`；
7. 「放弃任务分支」是不可逆操作，只在确认不需要该分支时使用。

### 5.3 文件级同步（copy / shared）

`POST /api/tasks/{tid}/sync` 是**文件级回收**，只用于非 Git 交付路径：

- 只处理 `file_changes` 中的改动；
- 只回收 `write_scope` 内、非保护路径、非 excludes 的文件；
- 跳过与其他运行中任务冲突的文件；
- 跳过工作区在任务执行期间被外部修改的文件（漂移）；
- 跳过路径逃逸、符号链接逃逸、已同步文件；
- 不改变任务状态；
- `shared` 模式返回 `already_shared`，因为改动本来就在根目录。

worktree 任务一旦有 `merge_status`，任务详情和审阅页**不会**再显示「同步到工作区」；
这类任务的交付物在 `ma/...` 分支上，通过 Git merge 回收。

同步后，工作副本里的文件仍然存在，所以 `pending` 可能仍非 0；UI 用 `unsynced` 判断
「是否已经落到项目根目录」。

审阅页允许对失败任务手动同步，前提是 `workspace.can_sync` 为 true：

- 非 `read_only`；
- 非 `shared`；
- 工作副本和 baseline 都存在；
- 没有结构性阻塞原因。

这适合「Agent 退出码失败但产物可用」的场景。同步只写文件，不会把任务改成 `success`；
是否验收通过仍需显式操作：可用 CLI `task approve --task <id> --sync` 或
`POST /api/tasks/{tid}/approve` 做人工验收（会保留原始 run 的退出码与错误，只标记
`human_override`）。v2 任务详情只在 `waiting` 状态显示验收主按钮：Git 任务为
「验收并合并」（先验收，再合并任务分支），copy/shared 任务为「验收并同步」。

### 5.4 Git worktree：先审后合、自动/人工合并、脏树与冲突

worktree 任务成功时（Agent 退出成功 + 验收通过 + 无取消/冲突 + claim 有效）：

1. Runner 在 `ma/<task-id>-<slug>` 分支创建真实 commit；commit 显式排除
   `.multiagent/` 控制面（handoff、context 等不会进入交付物）。这是验收通过后的
   **唯一落点**，默认不会自动回到基线；
2. 默认 `policy.git.auto_merge=false`（**先审后合**）：不执行 merge，
   `result.git.merge` 记录 `skipped=true` / `reason=auto_merge_disabled`；任务分支和
   提交保留，需在任务详情/审阅页点「合并到基线」；下游任务必须先合并才能看到产出；
3. 显式设置 `policy.git.auto_merge=true` 时，才执行 `git merge --no-ff` 把任务分支
   合并回当前基线分支，适合需要 DAG 连续交付的项目；
4. 没有产生 commit（无交付物）时不会合并，状态显示 `not_merged`。

默认策略片段（先审后合，建议保持脏树检查与 worktree 保留）：

```json
{
  "git": {
    "auto_merge": false,
    "require_clean_tree": true,
    "keep_worktree": true
  }
}
```

需要 DAG 连续交付时只把 `auto_merge` 改为 `true`，`require_clean_tree` /
`keep_worktree` 建议保持上面的值：前者避免人工未提交改动被混入合并，后者保留工作
副本便于复审。自动合并要求基线干净、并行任务写范围不重叠，否则仍会被脏树/冲突阻断。

**脏树阻断**：`policy.git.require_clean_tree=true`（默认开启）时，合并前检查基线工作树。
`dirty_paths` 非空（忽略项和 `.multiagent/` 不计）就阻断：

- API 返回 409，`blocked=true`、`reason="dirty_worktree"`、`dirty_files=[...]`；
- 开启自动合并时，Runner 合并遇到脏树会把任务转为 `blocked`，并在活动流记录
  `task.git.merge.blocked`；
- 不会把人工未提交改动混进 Agent 提交。把 `require_clean_tree` 设为 false 可绕过，
  但不建议；应先提交或清理基线。

**合并冲突**：冲突时执行 `git merge --abort`，仓库保持干净，然后返回 409：

```json
{
  "merged": false,
  "blocked": false,
  "conflict": true,
  "reason": "conflict",
  "branch": "ma/...",
  "base": "<sha>",
  "sha": null,
  "files": ["冲突文件1", "冲突文件2"],
  "message": "合并冲突，已执行 git merge --abort，仓库保持干净",
  "error": "..."
}
```

任务详情和审阅页会把 409 转成页面内的「合并问题」面板，最多列 12 个文件，提供
「重试合并」「放弃任务分支」「打开工作目录」三个动作。系统**不会自动解决冲突**；
需要人工在任务工作目录解决后重试。

**人工合并**：`POST /api/tasks/{tid}/merge`。如果 worktree 里还有未提交改动，会先
`commit_worktree` 落盘再合并；成功返回 200（`merged=true`、`sha`、`files`、`base`），
结果写回 `task.result.git.merge`，刷新后 UI 立即显示「已合并」。

**放弃任务分支**：`POST /api/tasks/{tid}/discard-worktree`，可选 body：

```json
{"delete_branch": true}
```

- 默认 `delete_branch=false`：只删除 worktree 目录，任务分支保留；
- `delete_branch=true`：删除 worktree 后强制删除 `ma/...` 分支；当前基线分支永远
  拒绝删除（409）；
- 分支删除后 `result.git.merge.discarded=true` / `reason="discarded"`，任务显示
  `discarded`，合并入口隐藏，避免点击必然 409。

**worktree 回收**：`policy.git.keep_worktree=false` 时，合并成功后当次删除 worktree
目录；任务分支与提交永远保留。`git.worktree_retention_days`（默认 14 天）供
`maintenance` 清理已合并且过期的 worktree，未合并的 worktree 永不清理。

### 5.5 审阅页与任务详情如何显示结果

审阅页分组规则：

- `merge_status=merged` → 「已合并 / 已同步」；
- `merge_status=discarded` → 「失败」；
- `merge_status=conflict/blocked` → 「待审阅」，详情内显示合并问题；
- 非 `success` → 「失败」；
- 没有 Git 元数据时，`unsynced=0` → 「已合并 / 已同步」，否则「待审阅」。

任务详情：

- 概览显示合并徽标和分支/commit；「已合并」状态只保留「再次运行」，不再显示同步；
- 「合并问题」面板显示冲突文件或未提交文件，动作见 5.4；
- 「改动」页签对 Git 任务显示「已合并 / 无需同步 / 合并到基线 / 合并被阻止 /
  查看合并问题 / 任务分支已放弃」，不再显示文件同步按钮；
- 隔离降级（例如 worktree 因脏树退化为 copy）显示 `degrade_reason` 和
  「worktree 不可用」提示，不把请求模式当成实际模式。

### 5.6 新建任务高级选项、超时与重试

新建任务对话框的「高级选项」可折叠展开：

| 字段 | 范围 | 留空 |
| --- | --- | --- |
| 超时预算（秒） | 30–86400 | 使用任务类型/模板默认，运行期再回退 Agent 与 policy |
| 最大尝试次数 | 1–10 | 使用任务类型默认 |

只有显式填写才会进入创建请求；非法值会标红并阻止提交。任务详情「延长超时」同样是
30–86400 秒，调用 `POST /api/tasks/{tid}/timeout`：

- 任务正在运行：`applies_to=next_attempt`，只对**下一次尝试**生效，不会延长正在跑的
  Agent；
- 任务未运行：`applies_to=next_run`，下次运行生效；
- 下一次尝试的计划超时可在任务详情 `runtime.next_attempt_timeout_sec` 查看；
  重试按 `retry.timeout_multiplier`（默认 1.5）放大，`retry.max_timeout_sec` 封顶。

「重试」的语义是**文件级续跑**，不是 ACP session resume：

- 每次重试启动新的 Agent 会话，不复用上次 ACP 会话；
- Runner 归档上次工作副本，只把 `write_scope` 内、非保护路径、非逃逸的半成品恢复到
  新副本，并在上下文提示先核对再继续；
- 越权文件、保护路径和路径逃逸内容不会被恢复。

### 5.7 验收路径 glob、失败候选与修正入口

产出文件/验收条件支持 glob（`*`、`?`、`[`）：

- 含 glob 的产出会生成 `glob_min_files` 条件，至少匹配一个文件；
- 精确路径仍按精确匹配，不会因为同名文件存在于子目录而放行；
- `file_nonempty` / `content_contains` / `content_not_contains` 同样支持 glob，
  只要任一匹配文件满足条件即可；
- 越界 glob 不匹配任何文件，不会抛异常。

精确路径验收失败时，失败记录会附带诊断：

- `candidates`：按 basename 在工作区内找到的候选（最多 20 个；失败摘要最多列 5 个，
  跳过 `node_modules`、`.git`、`vendor` 等目录和符号链接）；
- `suggested`：建议的修正条件，例如改成 `glob_min_files`；
- **候选存在不会改变失败结论**，不会自动放行。

任务详情「概览 → 验收条件」提供「修正验收路径」入口：只修改任务的验收条件
（`PATCH /api/tasks/{tid}` 的 `acceptance`），保存后必须重新运行或重新验收；它不会
伪造通过，也不会自动改写 Agent 产物。

计划创建（`POST /api/projects/{pid}/tasks/plan` / `task add-plan`）会在写库前扫描项目
根目录，规则如下（`multiagent/tasks/planner.py`）：

- 只处理 `outputs` 和路径型验收条件中的**精确路径**；glob 不重写；
- 目标路径已存在：保持原样；
- 目标不存在，但按 basename 在项目内只找到**唯一候选**：自动回填为候选的相对 POSIX
  路径（`outputs` 与验收条件同步；未显式写 `write_scope` 时按新 outputs 推导写范围）；
- 多个候选或无候选：保留原路径并写 warning，不阻断计划创建；
- 绝对路径、`..` 等越界路径：跳过并告警，绝不回填到项目外；
- 扫描结果写入任务的 `result.acceptance_path_scan`（`autofixes` / `warnings`）；回填只
  修正「预期路径写错」，不会放宽验收阈值，最终仍由 Agent 的真实产物决定是否通过。

计划中的 `outputs`、`acceptance`、`acceptance_ref` 仍建议显式填写；自动回填只解决
「唯一同名候选」这一种常见笔误。

### 5.8 进程与端口

Agent 可能在任务工作目录里启动 `npm run dev`、`vite preview`、`python -m http.server`
等监听进程。Runner 在 Agent 调用前后各拍一次监听 TCP 快照，结果写入
`task.result.processes`：

| 字段 | 含义 |
| --- | --- |
| `started` | 调用后新增的监听进程（pid、ports、command、owned） |
| `reaped` | 已终止的进程（仅 `owned=true` 且允许回收） |
| `warnings` | 无法确认归属、prober 不支持或回收失败等提示 |
| `supported` | 当前平台是否支持进程审计 |

任务详情「概览 → 进程与端口」展示启动/回收记录和 warnings。归属判定只认能证明与
本任务 `workdir` 有关的进程（command/exe_path/cwd）：判断不了归属的只告警、不杀；
调用前已存在的 pid、当前进程、父进程和低 pid 永不触碰。`processes.audit_enabled=false`
关闭审计；`processes.reap_orphans=false` 只记录不回收。`processes.min_timeout_sec`
默认 `0`（所有任务都审计）；设为正数可作为极短任务的显式逃生阀。`finish` 已异步化，
元数据查询不会挤占硬超时；超时 0 表示不限时，仍会审计。审计是旁路能力，**不保证**
回收所有残留进程，端口仍被占用时请按 warning 手动检查。Windows 在 `finish` 阶段
额外用 `Get-NetTCPConnection -State Listen`（CIM）补齐 netstat 漏报；`finish` 异步执行，
不阻塞任务返回。

### 5.9 回滚与快照

- 「回滚改动」按 `file_changes` 撤销该任务记录的文件改动，任务回到 `pending`；
- 回滚前建议在「记忆 → 版本快照」创建快照；
- 快照恢复会覆盖项目根目录，恢复前必须二次确认；有运行中调用时后端会拒绝或要求先停止。

## 6. 阻塞与人工确认

### 6.1 先看状态

| 状态 | 你应该做什么 |
| --- | --- |
| `blocked` | 看 `blocked_reason`、审批队列和运行日志；处理冲突/额度/验收后重试 |
| `waiting` | 人工执行者任务。Git 交付任务用「验收并合并」（先 `approve sync:false`，再 `git merge`）；copy/shared 任务用「验收并同步」或「仅验收，不同步」 |
| `failed` | 看 stdout/stderr、改动和验收结果；修复后重试或人工接手 |
| `interrupted` | 进程中断；确认没有旧进程后恢复/重试 |
| `cancelled` | 现场保留；可直接重试 |
| `skipped` | 人工跳过；可重试复活 |

### 6.2 人工确认队列

在「记忆 → 人工确认」中：

1. 按状态筛选或搜索；
2. 打开条目查看 `detail`（命令、文件、run、验收条件等）；
3. 填写备注；
4. 通过或驳回。

审批是**决策记录**，不是执行器。通过后通常仍需要：

- 危险操作：回到任务重试；
- 验收失败：修复后重试；如产物已可用，用 `task approve --task <id> --sync` 或
  `POST /api/tasks/{tid}/approve` 做人工验收（v2 详情页只在 `waiting` 显示验收按钮）；
- 冲突/漂移：先处理文件，再同步或回滚；
- 额度：调整 `config/agents.json` 的 quota，或等待额度恢复后重试；
- 人工确认：Git 交付任务用「验收并合并」（验收只改任务状态，再合并任务分支，
  不做文件级同步）；copy/shared 任务用「验收并同步」/「仅验收，不同步」结束任务。

审批类型与触发条件见 [V2-ARCHITECTURE.md](V2-ARCHITECTURE.md#63-审批类型)。

### 6.3 常见阻塞场景

| 现象 | 处理 |
| --- | --- |
| 额度超限 | 提高该 Agent 的 `quota`，或改用其他 Agent 重试 |
| 并发冲突 | 等另一个任务结束；对比 diff 后手动同步安全文件，或回滚本任务 |
| 工作区漂移 | 目标文件被外部改过；先备份/对比，再决定覆盖或放弃 |
| 验收失败 | 看 `acceptance` checks 与 stdout；修产物或调整验收条件后重试 |
| 工作区缺产物 | 副本内有、根目录没有；检查 `outputs` 路径与同步范围，手动同步 |
| worktree 提交失败 | 看 Git 错误；改动仍在任务分支工作副本，可手动提交或改用 copy 重试 |

## 7. 运行验证

### 7.1 当前入口

v2 控制台**没有**「运行验证」按钮。验证通过 API 使用；这是当前实现的明确边界。

### 7.2 查看项目技术栈

```powershell
$pid = "<project-id>"
Invoke-RestMethod "http://127.0.0.1:8770/api/projects/$pid/inspect" | ConvertTo-Json -Depth 6
```

返回 profile：`primary_language`、`languages`、`package_managers`、`markers`、
`commands`、`recommended`、`blocked`、`default_timeout_sec`、`files_scanned` 等。
这一步只扫描，不执行命令。

### 7.3 运行检查

```powershell
$tid = "<task-id>"
$body = @{
  checks = @("test")
  timeout_sec = 600
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8770/api/tasks/$tid/validate" `
  -ContentType "application/json" `
  -Body $body | ConvertTo-Json -Depth 8
```

参数：

| 字段 | 说明 |
| --- | --- |
| `checks` | 字符串或字符串数组；可选 `lint` / `typecheck` / `test` / `build`；最多 32 项 |
| `timeout_sec` | 1..3600；缺省用 profile 的 `default_timeout_sec`（通常 600） |

执行位置：

- `shared`：项目根目录；
- `copy` / `worktree`：任务的 `work/`；任务还没运行时返回 409。

### 7.4 读取最近报告

```powershell
Invoke-RestMethod "http://127.0.0.1:8770/api/tasks/$tid/validation" | ConvertTo-Json -Depth 8
```

报告字段：`report_id`、`task_id`、`project_id`、`mode`、`root`、`project_root`、
`started_at`、`finished_at`、`timeout_sec`、`requested`、`profile`、`checks`、`summary`、
`ok`、`empty`、`message`、`stored_at`。报告落盘在
`<runtime>/<project>/tasks/<task>/validation/`。

### 7.5 判定与限制

- `ok = passed > 0 && failed == 0 && timeout == 0 && cancelled == 0`；
- skipped 不算失败；工具缺失或命令找不到会 skip；
- 没有可运行检查时 `ok=false`、`empty=true`；
- 自动识别 Node/Python/Rust/Go/Java/PHP/Ruby/Make；
- 包含 `npm install`、`pip install` 等安装命令的脚本会被标记 `blocked`，不会执行；
- 每个检查最多保留 200KB 输出；超时使用进程树回收；环境变量走 allowlist。

## 8. 日常运维

- **备份**：设置 → 数据 → 立即备份；或 `python -m multiagent backup --label <name>`。
- **清理**：设置 → 数据 → 预览清理，确认后执行；默认保留最近 run/快照/备份。
- **存储盘点**：设置 → 数据，或 `python -m multiagent storage`。
- **完整性**：`python -m multiagent verify --full`。
- **旧版界面**：默认不暴露（`/legacy`、`/static/index.html` 返回 404）；设置
  `MULTIAGENT_UI=legacy` 并重启后访问 `/legacy`。
- **桌面日志**：`<home>/.multiagent/logs/desktop-backend.log`。
- **鉴权**：非 loopback/严格模式需要 Bearer token；token 在
  `<home>/.multiagent/token`。REST 可用 `Authorization` 或 `?token=`；WebSocket 用
  `Sec-WebSocket-Protocol: multiagent.token.<base64url(token)>`，URL 不再带 token。
