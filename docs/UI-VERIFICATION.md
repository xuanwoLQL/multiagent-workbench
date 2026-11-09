# UI 验收报告（QA / 最终验收）

> 验收对象：`docs/UI-OPS-REDESIGN.md`（设计系统 §1 / DOM 契约 §2.1 / 新 API §3 / 前端行为 §4 / 编码与性能 §5）
> 验收范围：Web 控制台（`multiagent/web/**`）+ 新 API（dashboard / quick-task / PATCH project）+ 编码全链路 + 浏览器冒烟。
> 验收时间：2026-11-06 23:36 – 00:10（Asia/Shanghai），**代码冻结后复验**。
> 结论：**PASS**。全量 pytest 两轮 425 passed / 10 skipped / 0 failed；新增 87 条 UI 契约与端到端用例全绿；
> 浏览器冒烟 24 张截图 + 11 视图 × 3 尺寸布局断言 + 真实点击链路全部通过；P0/P1 缺陷 0，P2 已知限制见 §8。

---

## 1. 结论摘要

| 验收项 | 结果 | 关键数字 |
| --- | --- | --- |
| 全量回归 `pytest -o addopts= -q` | ✅ 通过（冻结后连续两轮） | **425 passed / 10 skipped / 0 failed**（104.6s / 90.1s） |
| 新增 UI 契约测试 `tests/test_ui_contract.py` | ✅ 通过 | **73 passed** |
| 新增端到端测试 `tests/test_ui_e2e.py` | ✅ 通过 | **14 passed** |
| `scripts/verify_requirements.py` | ✅ PASS | 57 条需求 / 40 个引用用例 / 0 条引用不存在 |
| 浏览器冒烟 `tests/ui_smoke_qa.py --mode all` | ✅ PASS | 24 张截图（3 尺寸）、11 视图 × 3 尺寸 **横向溢出 0px**、看板列数 6/3 符合契约、console error **0** |
| dashboard 首屏预算（100 任务） | ✅ 通过 | **p50 = 5.05ms / p95 = 5.87ms**（阈值 150ms） |
| 中文乱码 | ✅ 无 | DB / API / dashboard / 渲染 DOM / 静态资源全程无 `?` 替换与 `�` |

**唯一失败项说明**：验收早期全量跑出现过 1 个失败（`app.js` 未转义插值护栏，`test_api_security.py::test_appjs_tainted_interpolations_are_escaped`），
由 ui-logic 在冻结前修复；冻结后两轮全量均 425 passed，无遗留失败。

---

## 2. 验收环境

| 项 | 值 |
| --- | --- |
| 工作目录 | `~/multi_xuanwo` |
| 操作系统 | Windows 11 家庭版（NT 10.0.26300） |
| Python | 3.11.0（`.venv`） |
| pytest / FastAPI / Starlette | 9.2.0 / 0.162.0 / 1.10.0 |
| 浏览器 | Microsoft Edge **151.0.4129.93**（`--headless=new`，CDP 1.3） |
| 演示数据 | `.e2e/demo-clean`（`scripts/demo.py` 生成：3 任务全 success、1 快照、3 假 Agent + 人工、1 条项目记忆） |
| 空工作台 | `.e2e/empty-home`（无项目，用于首屏空态） |

---

## 3. 命令与结果（可复跑）

```powershell
# 0) 准备干净演示数据（假 Agent，不消耗任何模型额度）
.\.venv\Scripts\python.exe scripts\demo.py --home ~/multi_xuanwo\.e2e\demo-clean

# 1) 全量回归（冻结后跑了两轮）
.\.venv\Scripts\python.exe -m pytest -o addopts= -q
#   → 425 passed, 10 skipped, 1 warning in 104.61s   （第二轮 90.07s，同样 425/10/0）
#   10 skipped 全部是真实 Agent 用例：需 MULTIAGENT_REAL_AGENT=1（会消耗额度），本次未执行

# 2) 需求证据校验
.\.venv\Scripts\python.exe scripts\verify_requirements.py
#   → 解析到 57 条需求 / 被引用测试用例 40 个 / 0 条引用不存在
#   → [PASS] 文档引用的每一项验收都真实存在且通过

# 3) 新增测试单独跑
.\.venv\Scripts\python.exe -m pytest tests\test_ui_contract.py tests\test_ui_e2e.py -o addopts= -q
#   → 87 passed

# 4) 浏览器冒烟（截图 + DOM + 三尺寸布局 + 真实点击 + console 错误）
.\.venv\Scripts\python.exe tests\ui_smoke_qa.py --mode all --home .e2e\demo-clean
#   → 结果：PASS（约 40s）→ .e2e/qa-smoke-results.json
```

产物：`.e2e/qa-smoke-results.json`（逐项断言与截图元数据）、`.e2e/qa-dashboard-perf.json`（首屏性能实测）、
`.e2e/qa-pytest-final.txt`（全量回归输出）、`.e2e/qa-verify-requirements.txt`、`.e2e/qa-dom-{home,empty}.html`（DOM 快照）。

---

## 4. A. UI 契约测试（`tests/test_ui_contract.py`，73 用例）

零构建原生 JS 没有类型系统，id 打错一个字母只会表现为「某块永远空白」。本文件把契约变成可执行断言（纯静态，2 秒内跑完）：

| # | 断言 | 结果 |
| --- | --- | --- |
| 1 | 契约 §2.1 **17 组 DOM id** 全部存在（topbar / sidebar / 11 个 view / inspector / 状态条 / 抽屉弹窗 / live 视图） | ✅ |
| 2 | index.html **无重复 id**（重复时 `getElementById` 只命中第一个，后者永远不渲染） | ✅ 0 重复 |
| 3 | 每个视图是 `section.view#view-<name>`；导航 `data-view` 与视图**一一配对**（11/11） | ✅ |
| 4 | app.js 里每个 `/api` 路径**后端可路由**（含 `${base}/stats` 这类相对拼接，共覆盖 46 个调用点） | ✅ |
| 5 | 新契约接口**真的被前端调用**：`GET dashboard`、`POST quick-task`、`PATCH /api/projects/{pid}`、`GET agents/detail` | ✅ |
| 6 | 既有懒加载接口仍存在：kanban / board / handoffs / approvals / snapshots / stats / runs / timeline / artifacts | ✅ |
| 7 | `$("id")` / `setText` / `setHtml` / `setGridHtml` / `setListHtml` / `setStreamHtml` / `bindClick` / `bindChange` 共 **109 处 id 引用**全部能解析（index.html 或 app.js 动态模板） | ✅ |
| 8 | 关键容器**有 JS 绑定**（mission-text / home-metrics / home-kanban / inspector-agents / status-ws / live-list …） | ✅ |
| 9 | 所有 `data-act` 都有 `switch` 分支：index.html 15 个 + app.js 动态生成 49 个（含三元模板里的动作名） | ✅ |
| 10 | 契约点名动作存在：new-task / import-plan / mission-create / mission-start / mission-plan | ✅ |
| 11 | `<script>` / `<link>` 必须是本地 `/static/**` 且文件存在；无外链、无内联脚本 | ✅ |
| 12 | 所有表单控件有 label / aria-label（17 个控件，含 `sr-only` label 与包裹式 label） | ✅ |
| 13 | 静态资源是合法 UTF-8，文案里无 `�` / `???`（`isGarbled` 检测器的注释与正则按白名单放行） | ✅ |
| 14 | 契约 §1 的 **20 个 `--ops-*` 设计令牌**落地 + `prefers-reduced-motion` 分支 | ✅ |
| 15 | 契约 §4.2 卡顿治理：`setInterval` 间隔 ≥5000ms（实测 7000ms）且检查 `document.visibilityState`；首屏走 dashboard 单接口 | ✅ |
| 16 | **变异自检 7 条**：删掉 id / 造重复 id / 去掉 label / 去掉 case / 三元模板动作名，断言必须失败（证明检查不是空转） | ✅ |

---

## 5. B. 端到端（`tests/test_ui_e2e.py`，14 用例，TestClient + 假 Agent，不依赖浏览器）

| # | 场景 | 断言要点 | 结果 |
| --- | --- | --- | --- |
| 1 | 中文全链路 | `POST /api/projects`（中文名）→ DB 读回 → dashboard → 项目名/任务标题/brief 原样；响应原始字节按 UTF-8 严格解码；无 `???`/`�` | ✅ |
| 2 | 乱码名修复 | 脏名 `????????` 项目 → `PATCH` 改中文名/显示名 → dashboard + 项目列表同步正确 | ✅ |
| 3 | 页面/静态资源编码 | `index.html` 声明 UTF-8、`/static/app.js` 严格 UTF-8、无替换字符 | ✅ |
| 4 | quick-task 默认验收 | 无 outputs → `exit_zero`；任务出现在 dashboard 看板「待执行」 | ✅ |
| 5 | quick-task 产物验收 | 2 个 outputs → 2 条 `file_exists`；outputs 原样进任务 | ✅ |
| 6 | kind=auto 推断 | 报告→doc、调研→research、审阅→review、代码→code、表格→data，且结果体现在 dashboard | ✅（5 参数） |
| 7 | start=false 不执行 | monkeypatch `engine.run_project` 为爆炸，仍返回 200、`started=false`、dashboard `running=false` | ✅ |
| 8 | plan 三步计划 | 3 任务、类型 research→doc→review、依赖 ②→①/③→②（mode=success）、review `read_only=true`、kanban edges=2 | ✅ |
| 9 | **start=true 真跑** | 真 spawn 假 Agent 子进程：toast 前置 → dashboard `running` 最终 false、任务 `success`、`percent=100`、产物落盘且中文内容保真、活动流有 task 事件 | ✅ |
| 10 | 首屏性能 | 100 任务 dashboard p50 ≤150ms，实测写入 `.e2e/qa-dashboard-perf.json` | ✅ **p50 5.05ms / p95 5.87ms** |

> 与 `tests/test_api_quicktask.py` 的分工：那边验「接口字段 / 校验 / 错误码」，这边验「用户从输入一句话到看见结果的整条链路」，断言对象是 **dashboard**（前端唯一数据源）与页面级行为。

---

## 6. C. 浏览器冒烟（`tests/ui_smoke_qa.py`，headless Edge + CDP）

### 6.1 三组断言

1. **dump-dom（虚拟时间）**：`#mission-text` 存在、`#home-metrics` 有数字、看板 6 个 `.kcol` + 卡片、
   `#inspector-agents` 有 `.agent-tile`、`#status-ws` 含「已连接/未连接」、整页文本无 `�`/`???`、模板 chips 5 个。
   - demo-clean：`metrics='3 任务总数 0 执行中 0 等待人工 3 成功 0 失败'`、`kcol=12`（首页 + 任务页两处）、`kcard=3`、`agent-tile=4`、`status-ws='WS 已连接'`。
   - empty-home：KPI 显示 `—` 占位、看板无卡片（跳过）、提示「新建项目 / 导入目录」、无乱码。
2. **布局（CDP 精确视口）**：11 个视图 × 3 个尺寸逐视图切页，等关键容器有内容后测
   `documentElement.scrollWidth <= innerWidth + 1` → **全部 0px 横向溢出**；关键容器内容全部非空。
   同时按契约 §4.1 断言看板列数：**1832×1010（≥1560）= 6 列 × 1 行；1440×900 / 1280×800（<1560）= 3 列 × 2 行**（实测 `rows/cols` 全部符合）。
3. **真实点击（CDP）**：填任务 + 选假 Agent + 点 `#mission-start` →
   toast「已创建 1 个任务，已在后台开始调度」→ 看板卡片 +1 → dashboard `running=false` 且任务 `success`
   → 点开任务卡出现详情抽屉（标题正确、正文 1290 字符）→ 关闭抽屉成功；**console error 0、未捕获异常 0**。

### 6.2 为什么用 CDP 截图而不是 `msedge --screenshot`

`--virtual-time-budget` 会把时间快进，WebSocket 握手未完成就截屏，状态条会拍到「WS 未连接」——那是截图工具的假象。
最终截图全部改用 CDP：`Emulation.setDeviceMetricsOverride` 固定**精确视口**（1832×1010 / 1440×900 / 1280×800），
真实时间等待 WS 连接与懒加载完成，再 `Page.captureScreenshot`。截图里状态条为「WS 已连接」。

### 6.3 截图清单（24 张，PNG 尺寸逐张校验）

> 截图落在 `.e2e/`（`.gitignore` 第 21 行忽略该目录），**不入库**；任何时候可用 §3 的命令重新生成。
> 若需要随文档入库，把下面任意图片复制到 `docs/assets/` 再改链接即可。

| 视图 | 1832×1010 | 1440×900 | 1280×800 |
| --- | --- | --- | --- |
| 指挥台（有项目） | [qa-home-1832.png](../.e2e/qa-home-1832.png) | [qa-home-1440.png](../.e2e/qa-home-1440.png) | [qa-home-1280.png](../.e2e/qa-home-1280.png) |
| 指挥台（空工作台） | [qa-empty-home-1832.png](../.e2e/qa-empty-home-1832.png) | [qa-empty-home-1440.png](../.e2e/qa-empty-home-1440.png) | [qa-empty-home-1280.png](../.e2e/qa-empty-home-1280.png) |
| 任务看板 | [qa-tasks-1832.png](../.e2e/qa-tasks-1832.png) | [qa-tasks-1440.png](../.e2e/qa-tasks-1440.png) | [qa-tasks-1280.png](../.e2e/qa-tasks-1280.png) |
| 实时事件 | [qa-live-1832.png](../.e2e/qa-live-1832.png) | [qa-live-1440.png](../.e2e/qa-live-1440.png) | [qa-live-1280.png](../.e2e/qa-live-1280.png) |
| 共享黑板 | [qa-board-1832.png](../.e2e/qa-board-1832.png) | [qa-board-1440.png](../.e2e/qa-board-1440.png) | [qa-board-1280.png](../.e2e/qa-board-1280.png) |
| Agent 编队 | [qa-agents-1832.png](../.e2e/qa-agents-1832.png) | [qa-agents-1440.png](../.e2e/qa-agents-1440.png) | [qa-agents-1280.png](../.e2e/qa-agents-1280.png) |
| 观测 / 审计 | [qa-observe-1832.png](../.e2e/qa-observe-1832.png) | [qa-observe-1440.png](../.e2e/qa-observe-1440.png) | [qa-observe-1280.png](../.e2e/qa-observe-1280.png) |
| 配置编辑 | [qa-config-1832.png](../.e2e/qa-config-1832.png) | [qa-config-1440.png](../.e2e/qa-config-1440.png) | [qa-config-1280.png](../.e2e/qa-config-1280.png) |

依赖图 / 交接说明 / 人工确认 / 版本快照 4 个视图未截图（不在本次截图清单），但已纳入布局与内容断言（11 视图全绿）。

---

## 7. 覆盖矩阵（s1–s16）

| 范围项 | 覆盖方式 | 证据 | 结论 |
| --- | --- | --- | --- |
| **s1** 指挥台首页（输入/指标/看板/活动） | 3 尺寸截图 + DOM 断言 + CDP 点击 | `qa-home-{1832,1440,1280}.png`；metrics/kcard/agent-tile 断言；toast + 卡片 +1 | ✅ |
| **s2** 任务看板与任务详情 | 截图 + 布局断言 + CDP 抽屉 | `qa-tasks-*.png`；`#kanban .kcard ≥1`；抽屉 `drawer open` + 标题 + 正文 1290 字符 → 关闭 | ✅ |
| **s3** 依赖图 | 布局/内容断言（11 视图） | `#dag svg *` 节点数 >0（3 任务 DAG） | ✅（未截图） |
| **s4** 共享黑板与记忆 | 截图 + 内容断言 | `qa-board-*.png`；`#bb-list .row ≥1`（决策「采用方案 A」） | ✅ |
| **s5** 交接说明 | 布局/内容断言 | `#handoff-list .row ≥1`（3 条交接） | ✅（未截图） |
| **s6** 人工确认 | 布局/内容断言 + API 测试 | `#approval-list` 有内容（demo 数据无待审批 → 空态渲染）；审批流由 API 测试覆盖 | ✅ 渲染 / ⚠ 写操作未点击（见 §8） |
| **s7** 版本快照 | 布局/内容断言 | `#snap-list .row ≥1`（初始快照） | ✅（未截图） |
| **s8** Agent 编队 | 截图 + DOM + 内容断言 | `qa-agents-*.png`；`#inspector-agents .agent-tile=4`；`#agents-list .row ≥1` | ✅ |
| **s9** 观测与审计 | 截图 + 内容断言 | `qa-observe-*.png`；stats/runs/timeline/artifacts 均非空（3 次调用、2 产物） | ✅ |
| **s10** 配置编辑 | 截图 + 内容断言 | `qa-config-*.png`；`#config-editor` 载入 agents.json（等宽字体） | ✅ |
| **s11** dashboard 聚合 API | 单测 + e2e + 浏览器 | `test_api_quicktask.py` / `test_ui_e2e.py`；浏览器只发 dashboard 一个首屏请求；p50 5.05ms | ✅ |
| **s12** quick-task API | 单测 + e2e + 浏览器点击 | single/plan/start=true/kind 推断/outputs 验收；浏览器「创建并启动」真跑 success | ✅ |
| **s13** 项目改名 API | e2e（中文 + 脏名修复） | `PATCH /api/projects/{pid}` 后 dashboard / 列表同步正确 | ✅ |
| **s14** 编码与子进程解码 | 既有 `test_encoding_regression.py` + 新增中文链路 + 静态资源 + DOM | DB/API/页面三层无 `???`/`�`；GBK 假 CLI 用例全绿 | ✅ |
| **s15** 桌面安装包与免安装版 | 由 desktop-pkg 负责并已验收：NSIS / portable / win-unpacked 均启动成功、title 正确、单实例、F12 默认拦截、关窗无残留 | `docs/RELEASE.md`；`build/_pkg/home.png`、`build/_pkg/home-final.png`、`build/_probe/splash.png` | ✅ |
| **s16** 文档与验收记录 | 本文件 + `scripts/verify_requirements.py` | 57 条需求 / 40 个用例引用全部存在且通过 | ✅ |

---

## 8. 已知限制与未覆盖项（P2，不阻塞）

1. **1440×900 首屏需竖向滚动**：精确视口下中心列 `scrollHeight 952 / clientHeight 808`，需滚动 **144px**；
   最近活动卡底部超出可视区 **121px**（最后一行文字约 26px，与 Lead 记录的「活动卡 26px」一致量级）。
2. **1280×800 首屏需滚动更多**：中心列需滚动 **253px**，看板第 3 行被裁；属竖向滚动，**横向溢出仍为 0px**。
3. **看板列数**：按契约 §4.1（≥1560 六列，<1560 三列 × 2 行），实测 1832 宽为 **6 列 × 1 行**，1440/1280 为 **3 列 × 2 行**（已作为冒烟断言，不符会失败；截图可见）。
4. **10 skipped**：全部为真实 Agent 用例（`MULTIAGENT_REAL_AGENT=1` 才跑，消耗模型额度），本次未执行；
   真实 Agent 的可执行性由 `scripts/e2e_real.py` 单独验证（需用户/Lead 决定是否消耗额度）。
5. **浏览器覆盖**：只在 Edge 151 headless 验证；Firefox / Safari / 桌面 Electron 壳内嵌浏览器未验（壳内同为 Chromium，风险低）。
6. **写操作未在浏览器点击**：审批通过/拒绝、黑板发布/编辑、快照创建/回退只验证了渲染与 API 测试，未做浏览器端点击回归。
7. **性能口径**：dashboard p50 为 TestClient 进程内实测（5.05ms），不是浏览器端到端首屏时间；浏览器侧只验证「有内容 / 无溢出 / 无 console 错误」。
8. **工具显示注意**：PowerShell `Invoke-WebRequest` 默认按 Latin-1 解码会把中文显示成 `æ¼”ç¤º`，这是 PowerShell 显示问题，
   不是产品缺陷；浏览器 DOM 与 JSON 均为正常 UTF-8（e2e 断言严格解码通过）。
9. **截图时机**：截图使用 CDP 真实时间等待，状态条显示「WS 已连接」；若用 `--virtual-time-budget` 截屏会拍到「WS 未连接」，属工具假象。

---

## 9. 缺陷记录

| 级别 | 缺陷 | 状态 |
| --- | --- | --- |
| P0 | 无 | — |
| P1 | 无 | — |
| — | 验收早期：`app.js` 未转义插值（taint 护栏失败，XSS 风险） | ✅ 冻结前由 ui-logic 修复；冻结后两轮全量通过 |
| P2 | 见 §8（首屏竖向滚动、未点击的写操作、浏览器覆盖范围等） | 记录，不阻塞 |

---

## 10. 本次新增/修改的文件

| 文件 | 说明 |
| --- | --- |
| `tests/test_ui_contract.py` | 新增：DOM / API / 动作 / 可访问性 / 设计令牌 / 卡顿治理静态护栏（73 用例，含 7 条变异自检） |
| `tests/test_ui_e2e.py` | 新增：中文全链路 + quick-task（single/plan/start）+ dashboard 性能（14 用例） |
| `tests/ui_smoke_qa.py` | 新增：headless Edge 冒烟脚本（截图 / dump-dom / 三尺寸布局 / CDP 点击 / console 捕获）；文件名不以 `test_` 开头，pytest 不收集 |
| `docs/UI-VERIFICATION.md` | 本文件 |
| `.e2e/qa-*.png`、`.e2e/qa-*.json`、`.e2e/qa-*.html`、`.e2e/demo-clean/**` | 验收产物（截图、结果、DOM 快照、演示数据） |

未修改任何他人负责的实现文件（`multiagent/**`、`desktop/**`、`scripts/**` 均未改动）。
