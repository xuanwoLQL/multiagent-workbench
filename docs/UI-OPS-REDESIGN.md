# 指挥台重构契约（UI / API / 验收）

> 本文件是 2026-11-06「桌面端乱码 + 卡手 + 无任务输入 + 感官不如参考图」这一轮改造的**唯一契约源**。
> 所有参与改造的实现必须遵守这里的 DOM id、API 形状与验收口径；改契约先改本文件并通知 Lead。

## 0. 目标（用户原话拆解）

1. 桌面端不能有任何乱码（中文项目名 / 任务 / 黑板 / Agent 输出，从创建到显示全链路）。
2. 不能卡手：首屏、切换视图、刷新、点击动作都要即时反馈；不再每 8 秒全量重拉 10 个接口 + 整页重渲染。
3. 必须有一个**能直接输入我的任务**的地方：一句话 → 建任务 / 直接派发 / 拆成计划。
4. 观感对标 `D:\agent\xuanwo` 的「不迷茫」工作台（image 5）：暖纸色、三栏 Ops 布局、大标题、卡片、右侧 Agent 编队与黑色主按钮、底部状态条。
5. 企业级生产口径：可打包、可恢复、有测试、有审计，不夸大。

参考实现（只读，禁止修改）：`D:\agent\xuanwo\apps\desktop\src\renderer\styles.css` 中 `--ops-*` 设计系统（约 1230–2060 行）与 `competition/dashboard.ts` 的三栏结构。

## 1. 设计系统（必须落地为 CSS 变量）

```css
:root {
  --ops-paper: #f2f0ea;
  --ops-paper-2: #faf8f2;
  --ops-card: #fffdf8;
  --ops-ink: #17212b;
  --ops-muted: #66717c;
  --ops-soft: #8b949c;
  --ops-rule: #d9d4c8;
  --ops-rule-strong: #bdb5a6;
  --ops-accent: #df542f;          /* 主强调橙红 */
  --ops-accent-soft: #f8e2d8;
  --ops-blue: #1f5f8b;
  --ops-blue-soft: #dceaf4;
  --ops-green: #2f704f;
  --ops-green-soft: #dcecdf;
  --ops-yellow: #9a6a18;
  --ops-red: #b3312f;
  --ops-shadow: rgba(34, 41, 47, 0.08);
  --ops-mono: "Cascadia Code", "Cascadia Mono", Consolas, monospace;
  --ops-display: "Bahnschrift SemiCondensed", "Aptos Display", "Microsoft YaHei UI", sans-serif;
  --ops-body: "Aptos", "Microsoft YaHei UI", "Noto Sans SC", sans-serif;
}
```

- 背景：`--ops-paper` + 32px 淡网格纹理 + 右上角淡橙径向光。
- 面板：`--ops-card`、1px `--ops-rule` 边框、6px 圆角、极淡阴影。
- 主按钮：黑底（`--ops-ink`）白字、直角/4px 小圆角；危险操作用 `--ops-red`；主强调按钮可用 `--ops-accent`。
- 字体：大标题用 `--ops-display`（Bahnschrift / 微软雅黑 UI）；正文 `--ops-body` 12–13px；代码/编号 `--ops-mono` 10–11px、字距 0.08em。
- 动效 ≤150ms；不要大面积渐变、玻璃拟态、紫色。空状态用细虚线框。
- 密度：桌面 1440×900 下首屏必须塞下「标题 + 任务输入 + KPI + 任务看板 + 右侧编队」，不出现半屏空白。

## 2. 整体布局

```
┌──────────────────────── topbar 64px ────────────────────────┐
│ brand 多Agent协作工作台 / WORKBENCH   [项目选择] [新建][导入][设置] [刷新] │
├──────────┬───────────────────────────────────┬───────────────┤
│ sidebar  │ center（可滚动）                    │ inspector 360 │
│ 220px    │                                   │ 工作台状态/编队 │
│ 项目卡    │ 指挥台：kicker+大标题+任务输入卡          │ Agent 2x2     │
│ 导航分组  │ KPI 条 + 任务看板 + 最近活动             │ 启动调度按钮    │
│ 底部状态  │ 其它视图（任务/依赖图/黑板/…）            │ 待审批/运行中/事件│
├──────────┴───────────────────────────────────┴───────────────┤
│ status strip 28px：WS · AGENTS 4/4 · TASKS x/y · APPROVALS · AUTOPILOT │
└──────────────────────────────────────────────────────────────┘
```

### 2.1 必须存在的 DOM id

Topbar：`topbar` `brand-name` `project-select` `btn-new-project` `btn-import-project` `btn-project-settings` `btn-refresh` `run-status` `desktop-badge`

Sidebar：`sidebar` `side-project-name` `side-project-path` `side-agents` `nav`（按钮 `data-view`）

Center views（`section.view#view-<name>`）：
- `view-home` 指挥台：`mission-text` `mission-outputs` `mission-agent` `mission-kind` `mission-submit` `mission-plan` `mission-templates` `home-metrics` `home-kanban` `home-activity` `home-onboarding`
- `view-tasks`：`kanban` + `data-act="new-task"` `data-act="import-plan"`
- `view-dag`：`dag`
- `view-board`：`bb-query` `bb-since` `bb-until` `bb-type` `bb-search` `bb-list`
- `view-handoffs`：`handoff-list`
- `view-approvals`：`approval-list`
- `view-snapshots`：`snap-label` `snap-a` `snap-b` `snap-list`
- `view-agents`：`agents-list`
- `view-observe`：`stats` `runs` `artifacts` `timeline` `artifact-role` `btn-export`
- `view-config`：`config-file` `config-editor` `config-dir`

Inspector：`inspector` `inspector-agents` `btn-run` `btn-stop` `btn-recover` `btn-force-recover` `inspector-approvals` `inspector-running` `inspector-stream`

Status strip：`status-ws` `status-agents` `status-tasks` `status-approvals` `status-autopilot`

保留：`drawer` `drawer-title` `drawer-body` `drawer-close` `modal` `modal-title` `modal-body` `modal-foot` `toast`。

## 3. 新增 API 契约（后端实现，前端按此调用）

### 3.1 `GET /api/projects/{pid}/dashboard`

一次返回指挥台全部数据，替代首屏 10 个请求。

```json
{
  "project": {"id": "...", "name": "...", "label": "...", "root_path": "...", "created_at": "...", "readonly": false},
  "progress": {"total": 3, "counters": {"pending": 3, "running": 0, "success": 0, "failed": 0, "blocked": 0, "waiting": 0, "skipped": 0, "cancelled": 0, "interrupted": 0}, "percent": 0},
  "tasks": [{"id": "...", "title": "...", "status": "pending", "kind": "code", "agent_id": "codex", "priority": 0, "read_only": false, "updated_at": "...", "blocked_reason": null}],
  "agents": [{"id": "codex", "label": "Codex", "protocol": "acp", "enabled": true, "roles": ["lead"], "strengths": ["code"], "usage": {"invocations": 0, "seconds": 0, "cost": 0, "tokens": 0}, "quota": {"max_invocations": 200}}],
  "approvals": {"pending": 0, "items": [{"id": "...", "kind": "danger", "title": "...", "risk": "high", "created_at": "..."}]},
  "stats": {"runs": 0, "duration_ms": 0, "tokens": 0, "cost": 0},
  "activity": [{"at": "...", "kind": "task.success", "actor": "codex", "summary": "..."}],
  "running": false,
  "workspace": {"files": 0, "bytes": 0},
  "server_time": "2026-11-06T..."
}
```

要求：单请求 ≤150ms（100 任务规模）；不得调用会启动子进程的探测逻辑（agents 数据来自配置 + usage 表）。

### 3.2 `POST /api/projects/{pid}/quick-task`

```json
{
  "text": "调研三个竞品并输出竞品分析.md",   // 必填 1..20000
  "title": null,                          // 可选，缺省从 text 首行截 60 字
  "agent_id": null,                       // null/"auto" = 路由规则自动选
  "kind": "auto",                         // auto/code/doc/data/research/review/generic
  "outputs": ["竞品分析.md"],              // 可选
  "read_only": false,
  "isolation": null,                      // null=按 kind 策略
  "mode": "single",                       // single | plan（plan=调研→初稿→审阅 三步 DAG）
  "start": false                          // true=创建后立即后台调度
}
```

响应：

```json
{"project_id": "...", "mode": "single", "started": false, "tasks": [{"id": "...", "title": "...", "status": "pending"}], "message": "已创建 1 个任务"}
```

- `kind=auto` 时按文本关键词推断（代码/报错/重构→code；报告/文档/撰写→doc；调研/资料/搜索→research；审阅/评审→review；表格/Excel/数据→data；其余 generic）。
- 验收默认：有 `outputs` → `file_exists`（多个都要求）；无 outputs → `exit_zero`。
- `mode=plan` 创建三个任务：①调研整理（research）→ ②产出初稿（doc）→ ③只读审阅（review 模板），依赖用 success。
- 文本一律 UTF-8；响应中不得出现 `?` 替换字符。

### 3.3 `PATCH /api/projects/{pid}`

```json
{"name": "可选，新唯一名", "label": "可选，显示名"}
```

返回更新后的 project。用于用户修正历史乱码名称。

### 3.4 现有接口保持兼容

`/api/projects`、`/api/projects/{pid}/kanban|board|handoffs|approvals|snapshots|stats|runs|timeline|events|artifacts` 全部保留原有形状；新前端按需懒加载。

## 4. 前端行为要求

### 4.1 指挥台（最重要）

- 大标题：例如 `把目标交给一支 Agent 团队`，副标题说明「一句话派发 / 自动拆解 / 全程留痕」。
- 输入卡：
  - `mission-text` 多行 textarea，placeholder：`描述你要完成的任务，例如：调研 3 个竞品，写一份 2000 字对比报告，产出 竞品分析.md`
  - 一行选项：产出文件 `mission-outputs`（占位 `产出文件（可选，逗号分隔）`）、执行者 `mission-agent`（默认「自动选择」）、类型 `mission-kind`（默认「自动判断」）。
  - 三个按钮：`创建任务`（次级）、`创建并启动`（黑底主按钮）、`拆解为三步计划`（文字按钮）。
  - `mission-templates`：模板 chips：`写一份报告` `调研一个主题` `审阅文档` `修改代码` `整理数据`；点击填充 textarea。
  - Ctrl/⌘+Enter = 创建并启动。
- KPI 条：任务总数 / 执行中 / 等待人工 / 成功 / 失败，用大数字 + 小标签。
- 任务看板：6 列（待执行/执行中/等待人工/成功/失败·阻塞/已跳过·取消），每列头部彩色状态点 + 数量；卡片显示标题、agent、类型、优先级。
  - 响应式：视口 ≥1560px 时 6 列一行；<1560px 时 3 列 × 2 行（避免中文标题在 130px 列宽下每行只剩 8–9 字）。两种布局共享同一份任务数据。
- 空项目时显示 onboarding：一句话说明 + 「新建任务」「导入目录」大按钮。
- 最近活动：最近 8 条事件的紧凑列表。

### 4.2 卡顿治理（必须做）

- 首屏：`/api/agents/detail` 与 `/api/projects` 并行；项目就绪后只发 `dashboard` 一个请求，再渲染整页。
- 定时器：`setInterval` 只在 `document.visibilityState === "visible"` 时刷新 dashboard，间隔 ≥5000ms；数据 JSON 未变化则跳过 DOM 重绘（缓存上次序列化）。
- 视图切换：按需加载当前视图数据；切回已有数据先渲染缓存再后台刷新，不白屏。
- 列表渲染：innerHTML 前做 hash 比较，内容相同不重写； drawers/modals 打开时不被后台刷新打断。
- 所有按钮点击后 100ms 内给视觉反馈（`.is-loading`/禁用/文案变化）；错误 toast 显示具体原因。
- 禁用高频全量刷新：删除 `refresh()` 里 Promise.all 10 个请求的轮询调用路径（保留手动刷新时按当前视图加载）。

### 4.3 其它视图

沿用既有功能，但统一到 ops 面板风格。任务详情抽屉、建任务弹窗、批量计划、黑板、交接、审批、快照、Agent、观测、配置都必须可用。配置编辑器使用等宽字体。

### 4.4 安全与可访问性

- 所有动态内容必须经 `esc()`，禁止裸 `innerHTML` 拼接未转义值（`tests/test_web_contract.py` 会扩展检查）。
- 表单控件都有 label 或 aria-label；键盘 Esc 关闭弹窗/抽屉；焦点样式可见。
- `prefers-reduced-motion` 时关闭动画。

## 5. 后端编码 / 性能要求

1. `multiagent/agents/proc.py`：任何 `text=True` 的 Popen 必须显式 `encoding="utf-8", errors="replace"`；`subprocess.run` 同理。
2. ACP 适配器 stderr 改为按字节读取 + `decode_output()`（UTF-8 → 本地代码页 → replace），保证 GBK 输出不乱码、不崩线程。
3. 回归测试：
   - 假 CLI 输出 GBK 字节（如 `print("中文")` 用 cp936 写 stdout）→ `run_command_tree` 返回中文且不抛异常。
   - 通过 HTTP 创建含中文的项目/任务 → 查询 DB → 读取 API，全程 Unicode 无 `?`/`�`。
4. `/api/agents/detail` 不得因某个 Agent 解析失败 500；`/api/agents/{id}/check` 超时 ≤10s，且不阻塞其它请求（FastAPI 线程池即可）。
5. 静态资源 `Cache-Control: no-cache`（开发）或 ETag；API 响应 `Cache-Control: no-store`。

## 6. 文件所有权（避免冲突）

| 范围 | Owner |
| --- | --- |
| `multiagent/api/**`、`multiagent/agents/**`、`multiagent/orchestrator/**` | backend-api |
| `multiagent/web/index.html`、`multiagent/web/style.css` | ui-shell |
| `multiagent/web/app.js` | ui-logic |
| `desktop/**`、`scripts/build_backend.py` | desktop-pkg（后续） |
| `tests/**`（新增） | qa（后续） |
| `docs/**`（除本文件外） | Lead / research |

禁止修改：`D:\agent\xuanwo`、`D:\AGENTS.md`、`reference/` 下既有参考仓库内容。

## 7. 验收口径

- 截图：1440×900、1280×800 两个尺寸，首屏无空白、无乱码、无溢出；与参考图同气质（暖纸色 + 三栏 + 大标题 + 卡片）。
- 全量 `pytest -o addopts= -q` 通过（基线 291 passed / 10 skipped）。
- 新 API 单元测试 + 编码回归测试通过。
- `scripts/e2e_real.py smoke` 至少验证 2 个真实 Agent 可执行（消耗额度，由用户/Lead 决定是否跑）。
- 桌面安装包重新构建并能启动到指挥台。
