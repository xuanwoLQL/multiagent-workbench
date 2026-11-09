# 设计参考研究：多 Agent 协作工作台的 UI / 交互模式

> 版本：2026-11-06 · 作者：research（Agent Teams 参考研究员）
> 配套契约：[UI-OPS-REDESIGN.md](./UI-OPS-REDESIGN.md)（DOM id / API / 验收口径以那份为准，本文只提供**模式证据与取舍理由**）
> 研究边界：`D:\agent\xuanwo` **只读**（未做任何写入）；GitHub 参考仓库只下载到 `reference/github/`，不改动 `reference/` 下既有三个仓库。

---

## 0. TL;DR（三条最重要的结论）

1. **三栏 + 顶部条 + 底部状态条是「指挥台」的最小稳定骨架，但中栏必须可切换视图而不是堆卡片。**
   xuanwo 把同一套壳子做成两个视图（`赛题面板` 面向任务、`Agent 控制台` 面向会话），用 `#app[data-view]` 切换；kagent 用左侧 Agent 轨 + 中栏对话 + 右侧上下文面板。结论：我们的「指挥台 / 任务 / 依赖图 / 黑板 / 审批 / 观测」应共用同一个 topbar + sidebar + inspector + status strip，只换中栏，**不要每个视图重造一套壳**。

2. **状态呈现要「分级诚实」：未知 ≠ 0，blocked ≠ failed，空 ≠ 坏。**
   xuanwo 用红边框区分 `blocked`（策略切断）与 `failed`（崩溃），未知指标显示 `—`；kagent 的 `StatTile` 在 `value === undefined` 时渲染破折号并写明理由（「一次失败的请求和空集群产生同样的空行」）；ag-ui 的 conformance 用例把「事件乱序 / 状态补丁打不上」定义为**可测的规则**。结论：我们的 KPI、Agent 状态、审批、黑板都必须区分「未知 / 空 / 失败 / 被阻断」四种语义，并在 tooltip 里写清口径。

3. **卡顿靠「分区签名 diff + 有界窗口」而不是靠少刷新。**
   xuanwo 把界面切成 5 个区（appBar / sidebar / center / inspector / status），每区序列化成签名，只有签名变了才重建；对话流只保留最近 120 块 DOM，上滚时冻结窗口并给「回到最新」；单次渲染 >32ms 打 warn。kagent / open-agent-platform 用 SWR / stick-to-bottom 解决同类问题。结论：我们 UI-OPS-REDESIGN §4.2 的「数据 JSON 未变化则跳过 DOM 重绘」应升级为**分区签名**，并且对话流必须窗口化，否则长跑会话会把桌面端拖死。

---

## 1. 内部参考：`D:\agent\xuanwo`「不迷茫」工作台（只读研读）

研读文件（均未修改）：
`apps/desktop/src/renderer/styles.css`（2308 行）、`competition/dashboard.ts`（1918 行）、`main.ts`（1042 行），辅以 `panes/panes.ts`、`approval/approval.ts`、`scorecard.ts`、`competition/model.ts`。

### 1.1 布局骨架

| 证据 | 内容 | 可借鉴点 |
| --- | --- | --- |
| `styles.css:33-57` | `#app { display:grid; grid-template-columns: 240px 1fr 400px; gap:8px; padding:8px }`，三栏各自 `overflow-y:auto`，中栏 `overflow:hidden` 由内部滚动 | 经典「导航 / 工作区 / 检查器」；中栏永不整页滚动，滚动发生在面板内部 |
| `styles.css:1272-1293` | `#app { display:block }` + `.competition-view` / `.workspace-view` 两个满高视图，`#app[data-view="workspace"]` 切换 | **同一壳、两个视图**：任务中心 vs 会话控制台，切视图不动导航 |
| `styles.css:1404-1435` | `.ops-shell { grid-template-columns: 236px minmax(0,1fr) 390px }`；`.ops-sidebar/.ops-inspector` 独立滚动，`.ops-center` 自己滚动 | `minmax(0,1fr)` 防止长内容把中栏撑破（长 URL / 长标题必备） |
| `styles.css:1295-1306` | `.ops-bar` 高 68px，`backdrop-filter: blur(16px)`，含品牌、导航、赛程信息、连接状态 chip、图标按钮 | 顶部条承载「身份 + 导航 + 全局状态 + 全局动作」，不放业务内容 |
| `styles.css:2147-2163` | `.ops-statusstrip` 高 30px，等宽 8px 字号，一行塞 `EVENT CHANNEL / AGENTS 4/8 / ACTIVE 2/3 / TARGETS 3 UP 1 DOWN / APPROVALS 2 / AUTOPILOT ON` + 右侧快捷键提示 | **底部状态条 = 常驻运行态**；数字密度高但不抢视线，异常项 `.strip-alert` 才变红 |
| `styles.css:2267-2303` | 断点 1240 / 1040 / 760：1240 收窄三栏；1040 检查器变右侧浮层（`position:fixed`，`width:min(390px,42vw)`）；760 侧栏变两列网格、中栏下沉、隐藏状态条 | 三档响应式足够；桌面端主战场 1280–1600 |

**结论**：我们的 `topbar 64px / sidebar 220px / center / inspector 360px / status strip 28px`（UI-OPS-REDESIGN §2）与之一致，可再加一条 xuanwo 的细节：**中栏必须是唯一滚动容器**，inspector 与 sidebar 各自滚动，这样切视图时滚动位置互不干扰。

### 1.2 设计令牌：两套并存，说明「同一产品可以有第二套皮」

- 旧工作台（Primer 风，`styles.css:3-21`）：`--bg #f6f8fa`、`--accent #0969da`、语义色 `--approve/--deny/--warn/--conflict/--blocked`。
- 竞赛工作台（暖纸「ops」风，`styles.css:1236-1257`）：`--ops-paper #f2f0ea`、`--ops-card #fffdf8`、`--ops-ink #17212b`、`--ops-rule #d9d4c8`、`--ops-accent #df542f`、`--ops-mono/--ops-display/--ops-body` 三组字体栈。

要点：
- **语义色与品牌色分开**：`--ops-accent`（橙红）只做强调，状态用 `--ops-green/--ops-red/--ops-yellow`；`blocked` 单独一个红，且注释里写死「必须保持 #da3633，验收断言红边框」（`styles.css:11`）——**颜色被测试锁住**，这是防回归的实用手段。
- 背景不是纯色：`radial-gradient` 淡橙光 + 32px 网格线（`styles.css:1259-1266`），廉价但有质感；`prefers-reduced-motion` 时关动画（`styles.css:2304-2308`）。
- 字体栈带中文回退：`"Microsoft YaHei UI"`、`"Noto Sans SC"`，等宽用 `"Cascadia Code"/Consolas`。

**采纳**：我们的 `--ops-*` 令牌已在 UI-OPS-REDESIGN §1 固化，本文不重复；建议补两条 xuanwo 的做法：(a) 给「状态色」加注释说明哪个被测试断言锁死；(b) 提供 `--ops-mono/--ops-display/--ops-body` 三组字体栈，标题与代码不用同一字体。

### 1.3 状态语义：blocked / failed / stopped / done 必须四分

`styles.css:269-283`：

```css
.status-running { color: var(--green); }
.status-waiting_approval, .status-waiting_lease { color: var(--warn); }
/* blocked（策略切断）与 failed（崩溃）必须分清楚：blocked 加红边框，failed 只红字 */
.status-blocked { color: var(--blocked); border-color: var(--blocked); }
.status-failed  { color: var(--blocked); }
.status-stopped { color: var(--text-dim); }
.status-done    { color: var(--accent); }
```

并且 `.pane-blocked`（`styles.css:388-390`）给整个中栏窗格加红描边，`.session-item.session-blocked` 给左栏会话卡加淡红底（`styles.css:171`）——**同一状态在三个层级（列表 / 窗格 / 状态条）用同一套色，形成「一眼定位」**。

**采纳**：我们的任务状态有 9 种（pending/running/success/failed/blocked/waiting/skipped/cancelled/interrupted）。建议映射成 4 个视觉层级：
`进行中(蓝/绿) · 等人工(黄) · 失败(红字) · 被阻断(红字+红边) · 终态成功(绿) · 跳过/取消(灰)`，并在看板列头、任务卡、检查器三处使用同一 class。

### 1.4 渲染性能：分区签名 diff（最值得抄的一段工程实现）

`competition/dashboard.ts:1029-1146`：

```ts
const appBarSignature = JSON.stringify({ status, error, baseUrl, race, count });
const sidebarSignature = JSON.stringify({ team, status, lastSyncAt, challenges: [...], tasks: [...], agents: [...] });
const centerSignature  = JSON.stringify({ filter, query, sortMode, selectedId, challenges: [...], tasks: [...], probes: [...], cached });
const inspectorSignature = JSON.stringify({ selected, task, sessions: [...], flags, factCount, facts: facts.slice(-4), agents, probe });
const statusSignature = JSON.stringify({ status, error, autoSubmit, autopilot, running, active, desired, probes, approvals });

if (appBarSignature !== renderSignatures.appBar) { renderSignatures.appBar = appBarSignature; renderAppBar(platform); }
// …其余四区同理
const duration = performance.now() - t0;
if (duration > 32) console.warn(`[ui competition] ${duration.toFixed(1)}ms nodes=…`);
```

配套：`scheduleRender(delay=120)` 做 120ms 合并（`dashboard.ts:1148-1156`），状态订阅统一走 `store.subscribeCompetition`（`:1886`），定时器只做三件事：倒计时 1s、自动巡航 20s、靶机探测 30s（`:1887-1895`）。

**采纳（强烈）**：这正是 UI-OPS-REDESIGN §4.2 想要的。落地为前端一个 `paintRegion(name, signature, fn)` 小工具即可（vanilla JS 十几行），比「整页 innerHTML 重写」稳得多，也不需要引入框架。

### 1.5 会话卡 / 计分卡：把「Agent 干了多少活」压成一行可扫读的数字

- 左栏会话卡（`styles.css:145-175`）：徽标行（agent / role / tier / status）+ 一行 10px 计分卡；`session-blocked` 时整卡淡红。
- 中栏窗格头下方计分卡条（`styles.css:396-405`）。
- 计分卡口径（`scorecard.ts:1-95`）：
  `已运行 3m20s · 工具 12 · 读板 4 · 滞后 2 · 审批 1 · 写板 3`，
  `title` 里解释「写板 = 事实 + 死路（两者带 by 署名）；意图/租约无作者字段，不计入；滞后 = 距上次读黑板的 tool call 数」。
  `syncLag === null` 时显示 `—`（`scorecard.ts:77`），**不假装是 0**。

**采纳**：我们的「编队」卡片每个 Agent 需要一行等价物：
`已运行 12m · 工具 8 · 写黑板 3 · 待审批 1 · 最近活动 2m 前`。
口径说明放 `title`，且未知字段用 `—`。这比画一个漂亮的雷达图有用得多。

### 1.6 对话流：块模型 + 窗口化 + 跟随/历史双模式

- 块类型（`panes.ts:31-38`）：`message / thought / user / tool / plan / other`，各带标签与配色；工具步状态 `in_progress / completed / failed / cancelled`（`styles.css:554-563`）。
- 窗口化：`CONV_WINDOW = 120`，DOM 只保留最近 120 块；顶部「加载更早」，右下「回到最新」浮层（`styles.css:426-461`，`panes.ts:164-174`）。
- 跟随逻辑（`panes.ts:294-306`）：用户离开底部 → `following=false` 并冻结窗口、累计 `unseen`；回到底部且 `unseen===0` → 恢复跟随。**永不强行把用户拽回底部**。
- 空窗格文案（`panes.ts:160-163`）：「会话启动后，对话会实时滚到这里；目前还没有输出。」——**空状态解释「接下来会发生什么」**。
- 每块 `contain: layout style`（`styles.css:422-423`），避免单条长消息触发整段重排。

**采纳**：我们的「运行流 / 事件」面板应按同一块模型渲染：
`user / agent / tool / plan / approval / system`，工具步带状态 chip；必须做窗口化（建议 200 块）+「回到最新」；自动跟随仅在贴底时生效。

### 1.7 审批流：三栏卡片 + 批量 + 已决折叠

`approval/approval.ts:1-12` 的设计说明与 `styles.css:597-656`：

```
┌ 左：命令全文 + 目标（可能为 null，显示「—」）
│ 中：agent 自述意图（无则「暂无自述意图」）
└ 右：命中规则 + 风险等级 + 同类历史（sameKindCount>1 标「重复退避」）
   底部：放行 / 记住并放行 / 拒绝（+ 全局：全部放行 / 拒绝全部）
```

- 卡片左边框按风险着色：`.ap-high` 黄、`.ap-blocked` 红（`styles.css:608-609`）。
- 规则 id → 人话映射表（`approval.ts:37-47`）：`no-out-of-scope → 目标不在授权范围`、`credential-actions-need-human → 凭据类动作需人工`……
- 已决项折叠在下方，`opacity:0.62`，hover 恢复到 0.9（`styles.css:649-650`），状态胶囊 `已批准/已拒绝/超时/自动`。
- 空状态不是空白：亮出「规则包名 + 已武装徽标」+「两种可能的原因」（`styles.css:577-595`）——**空状态也要自证系统在工作**。

**采纳**：我们的 `view-approvals` 直接照这个结构做，并补一条 xuanwo 的「记住并放行（session/agent 两种粒度）」，因为它显著减少重复审批。

### 1.8 诚实提示：宁可不显示，也不假装

- 平台题数不一致（`dashboard.ts:543-550`）：`平台标注 12 题，当前接口返回 9 题` + 解释「其余受队伍身份/赛段控制」。
- 离线缓存标识：状态条 `EVENT CHANNEL · CACHE`、中栏 `当前离线缓存`。
- 未知/未检测/未配置 三态徽标（`main.ts:82-116`）：`已配置 / 未配置 / 错误 / 未检测`，未检测不误报为未配置。
- 长字符串兜底（`styles.css:822-831`）：所有标题/名称 `min-width:0; overflow-wrap:anywhere`，**外部文本再长也不撑爆栏**。

**采纳**：我们的 dashboard API 一旦有读失败，就按 kagent 的做法「分项报告 + 顶部 banner 点名哪几个读失败」，绝不用 0 填充。

### 1.9 交互细节清单（低成本高收益）

| 细节 | 证据 | 说明 |
| --- | --- | --- |
| 快捷键 `/` 聚焦搜索、`n` 下一题、`s` 一键启动 | `dashboard.ts:1849-1884` | 先排除 `input/textarea/select/contenteditable`，再判断按键；状态条右侧常驻快捷键提示 |
| UI 偏好持久化 | `dashboard.ts:39-64` | 版本化 key（`xuanwo.competition.sortMode.v1`），`try/catch` 包住，存不住不影响功能 |
| 操作确认弹窗 | `dashboard.ts:842-851` | 有副作用的动作（提交 flag）必须弹确认，列出「题目 / 影响 / 网络请求去向」 |
| toast | `styles.css:881-899` | 底部居中，`error/info` 两种色，`max-width:70vw` |
| 首启引导 | `styles.css:901-940` | 可关闭浮层（不是 wizard），列出「已就绪 / 待配置 / 需要注意」三组 |
| 卡片入场动画 | `styles.css:1750-1766` | `animation-delay: calc(var(--card-i) * 28ms)`，≤300ms，`prefers-reduced-motion` 关闭 |
| 空状态带「下一步」 | `styles.css:1868-1884` | 大号空状态卡：编号 + 标题 + 一句解释 + 两个按钮（主/次） |

### 1.10 xuanwo 可借鉴清单（按优先级）

| 优先级 | 模式 | 来源 | 本项目落地位置 |
| --- | --- | --- | --- |
| P0 | 分区签名 diff + 120ms 合并 + 32ms 预算告警 | `dashboard.ts:1029-1156` | `multiagent/web/app.js` 渲染层 |
| P0 | 三栏 + topbar + status strip 单一壳 | `styles.css:33-57, 1404-1435, 2147-2163` | `index.html` 骨架 |
| P0 | blocked/failed/unknown 三分语义 | `styles.css:269-283`、`scorecard.ts:77` | 任务卡 / Agent 卡 / 审批 |
| P0 | 对话流窗口化 + 跟随/历史双模式 | `panes.ts:160-306` | 运行流面板 |
| P1 | 审批三栏卡 + 批量 + 已决折叠 + 记住放行 | `approval.ts`、`styles.css:597-656` | `view-approvals` |
| P1 | 每 Agent 计分卡（口径写进 title） | `scorecard.ts` | inspector 编队卡 |
| P1 | 空状态自证 + 下一步按钮 | `styles.css:577-595, 1868-1884` | 所有视图 |
| P1 | 长字符串兜底 + `minmax(0,1fr)` | `styles.css:822-831` | 全局 CSS |
| P2 | 快捷键 `/`、`Ctrl+Enter`、`Esc` | `dashboard.ts:1849-1884` | 全局 |
| P2 | 版本化 localStorage 偏好 | `dashboard.ts:39-64` | 视图/筛选记忆 |
| P2 | 双视图切换（任务中心 ↔ Agent 控制台） | `styles.css:1272-1293` | 顶部 nav |

---

## 2. GitHub 同类项目（已下载到 `reference/github/`）

下载方式：`https://codeload.github.com/<owner>/<repo>/zip/refs/heads/main`（PowerShell `Invoke-WebRequest`），解压后去掉一层 `<repo>-main/` 目录。均为 **2026-11-06 抓取的默认分支 HEAD 快照**（无 `.git`）。

### 2.1 kagent — Kubernetes 原生 Agent 控制平面（企业级最接近的形态）

| 项 | 值 |
| --- | --- |
| 链接 | https://github.com/kagent-dev/kagent |
| License | **Apache-2.0** |
| 体积 | zip 7.0 MB → 解压 **18.1 MB / 1958 files** |
| 快照 | `58a0d70d44be`（2026-11-06T14:36Z） |
| 技术栈 | UI：Vite + React 19 + TypeScript + **antd 6** + Emotion + SWR + React Router；后端 Go（gRPC/Postgres）+ Python ADK |
| 关键目录 | `ui/src/theme/`、`ui/src/components/chat/`、`ui/src/components/dashboard/`、`ui/src/pages/`、`ui/AGENTS.md` |

**可借鉴的具体 UI 模式**

1. **设计令牌集中且有「为什么」**（`ui/src/theme/theme.ts`）：
   每个颜色旁写明用途与踩过的坑——`textOnPrimary`（主色面上的文字要单独一个 token，否则暗底暗字）、`primaryText`（品牌色当**前景**时要另一个值，深紫在近黑底上只有 2.5:1）、`borderStrong`（控件边界要 3:1，卡片边框可以更淡）、状态胶囊的 `successBg/Border/Text` 三件套**显式给出**而不是从单色推导。
   → 我们的 `--ops-*` 可以照此补：`--ops-accent-ink`（强调色当文字时的深色版）、`--ops-rule-strong`（已存在，正好对应）。
2. **StatTile：未知 ≠ 0**（`ui/src/components/dashboard/StatTile.tsx`）：
   `value === undefined` 渲染 `—` 且变灰，注释直说「一次失败的请求和空集群产生同样的空行，仪表盘印 0 就是在断言它不知道的事」；`isLoading` 时 `opacity:0.4` 而不是塞骨架屏；数字用**比例数字**（不是 tabular）因为这里不需要对齐。
3. **DashboardPage：多项独立读，不合并成「全有或全无」**（`ui/src/pages/DashboardPage.tsx` 顶部注释）：
   「模型列表失败时，已取到的 agent 数量仍然为真、仍然值得显示；每个数字只报告它知道的部分，banner 点名具体哪几个读失败。」
4. **AgentStatusTag：状态 + 悬停解释**（`ui/src/components/agent/AgentStatusTag.tsx`）：
   `Ready / Updating / Update failed / Preparing / Failed / Not reported` 六态；`Updating` 的 tooltip 说明「控制器正在准备变更，对话仍使用上一个可用版本」——**把「为什么还能用」讲清楚**。
5. **ToolCallCard：工具调用是紧凑卡片，不是 JSON 倾倒**（`ui/src/components/chat/ToolCallCard.tsx`）：
   标题行 `[图标] 工具名 …… [called/result/failed/not run]`；payload 用 `<pre>` + `white-space:pre` + `overflow-x:auto`，注释解释「JSON 换行会让缩进失去深度含义，标识符/URL 会被从中间截断」；**失败结果红边框**，被权限拒绝的调用显示 `not run`（而不是空白）。
6. **CheckpointDivider：对话里的「分叉边界」**（`ui/src/components/chat/CheckpointDivider.tsx`）：
   在可检查点回合的最后一条消息下画一条线，线本身点开记录（id/状态/时间），线下放三个动作（打开 / 分叉 / 重命名 / 删除）。这是**「快照 = 对话里的一个位置」**的 UI 表达，比单独的「快照列表」直观。
7. **AskUserPrompt：Agent 停下来提问的唯一呈现**（`ui/src/components/chat/AskUserPrompt.tsx`）：
   注释列出三条「它拒绝做的事」：不为不认识的请求类型编选项、不允许同一答案提交两次、不发送部分答案（运行时按位置配对答案，缺口会静默答错）。
8. **ToolApprovalRecord：审批完成后的「可归档形态」**（`ui/src/components/chat/ToolApprovalRecord.tsx`）：
   审批结束后不是消失，而是在对话里留一张卡：`Tool access approved / rejected / decided`（混合时给 approved+rejected 计数），绿/黄边框区分。
9. **ChatComposer：流式期间「发送」变「停止」**（`ui/src/components/chat/ChatComposer.tsx`）：
   「让人盯着一个长回答时，有用的不是灰掉的按钮，而是退出它的办法。」
10. **lifecycleReading：状态按「观测的具体程度」排序**（`ui/src/components/chat/lifecycleReading.ts`）：
    已声明的生命周期操作 > 回合状态 > 实例静息态；纯函数、可单测、tooltip 说明来源。

**不采纳 / 需改造**

- antd 6 + Emotion 整套组件库：我们是 **vanilla HTML/CSS/JS + 无构建**（`multiagent/web/`），引入等于重写；只借「组件形态与文案」，不借实现。
- 暗色 + 紫色品牌体系：与 UI-OPS-REDESIGN 的暖纸色冲突，不采纳配色。
- Checkpoint/分叉：需要后端「会话快照 + 分叉」能力，我们当前只有 `snapshots`（对比），**先做「快照对比」不做「分叉」**。
- Kubernetes 概念（namespace / harness / revision）：与单机多 Agent 工作台无关，不采纳。

**许可注意**：Apache-2.0 允许借鉴与再分发，若直接复制代码片段需保留 LICENSE/NOTICE 与版权声明；本文只提炼模式，不复制代码。

### 2.2 open-agent-platform — LangGraph 官方「Agent 平台」样板（Next.js + shadcn）

| 项 | 值 |
| --- | --- |
| 链接 | https://github.com/langchain-ai/open-agent-platform |
| License | **MIT**（Copyright LangChain, Inc.） |
| 体积 | zip 0.4 MB → 解压 **1.23 MB / 229 files**（很轻，适合当结构范本） |
| 快照 | `6a2ea0a3d019`（2026-02-25，仓库近期不活跃，注意时效） |
| 技术栈 | Next.js 15 App Router + TypeScript + Tailwind + **shadcn/ui（new-york）** + Radix + Zustand + Supabase + `nuqs`（URL 状态） |
| 关键目录 | `apps/web/src/components/agent-inbox/`、`src/features/chat/`、`src/features/agents/`、`src/features/tools/playground/`、`src/components/ui/sidebar.tsx` |

**可借鉴的具体 UI 模式**

1. **App Shell**：`(app)` / `(auth)` 路由组分离；左侧 shadcn sidebar（可折叠成图标条、移动端变 Sheet），顶部面包屑；页面：`agents / inbox / rag / tools / settings`。
   → 对应我们的 `sidebar` 导航分组 + 视图切换，**建议给 sidebar 一个「收起为图标」状态**（1440×900 下多 60px 给中栏）。
2. **Agent Inbox：把「需要人处理的事」做成收件箱**（`src/components/agent-inbox/`）：
   顶部按钮组按状态筛选（`interrupted / all` …），列表项 `InboxItem` 带状态胶囊、线程 id、动作区；空/加载各有专门视图（`Inbox as InboxIcon` + 文案 / `LoaderCircle` + 文案）。
   `statuses.tsx` 的状态集合值得抄：`idle / busy / error / interrupted / human_response_needed`，并且**由 interrupt 配置推导**出 `Requires Action` 或 `Ignore` 胶囊（`allow_ignore && !allow_respond && !allow_edit && !allow_accept` → 只能忽略，灰；否则绿「需要动作」）。
   → 我们的「等待人工」任务 + 待审批项应当合并成一个 **Inbox 视图**，而不是散在任务看板里。
3. **URL 即状态**（`nuqs`）：`?inbox=interrupted`、`?thread=…`、`?view_state_thread=…` 让筛选与选中项可分享、可刷新恢复。
   → 我们是纯静态页，可用 `location.hash` 实现同样效果（`#view=tasks&task=abc`），刷新不丢上下文。
4. **对话页三件套**：中栏 thread（`StickToBottom` 自动贴底）、右侧 `configuration-sidebar`（Tabs 分「Agent / Tools / RAG」，字段脏了才出现 Save/Reset，工具可搜索）、左侧 thread-history sidebar。
   → 我们的 inspector 可以学这个「Tabs + 脏状态才出保存按钮」：配置类面板不要常驻一排按钮。
5. **Tool Playground**：由工具 JSON Schema 渲染表单（`schema-form.tsx`）+ 结果查看器（`response-viewer.tsx`）。
   → 我们的「Agent 详情」若要展示工具能力，可以直接用 schema 渲染，而不是写死表单。
6. **滚动位置保存**（`inbox-view.tsx`）：列表滚动位置在切换线程时记住，300ms 节流。
   → 视图切换不丢滚动位置，是「不卡手」的体感来源之一。

**不采纳**

- Next.js / React / shadcn 依赖链（我们是无构建静态前端）。
- Supabase 认证、RAG 集合管理、MCP 工具市场：超出本项目范围（我们的工具是本地 ACP Agent）。
- 其 `agent-inbox` 组件与 `agent-chat-ui` 高度同源（同一作者体系），两者只取其一实现思路即可。

### 2.3 agent-chat-ui — 极简 Agent 对话 UI（最小可用参考）

| 项 | 值 |
| --- | --- |
| 链接 | https://github.com/langchain-ai/agent-chat-ui |
| License | **MIT**（Copyright Brace Sproul） |
| 体积 | zip 0.2 MB → 解压 **0.47 MB / 74 files**（最轻，适合逐文件读） |
| 快照 | `0d21f7ff4c45`（2026-11-03） |
| 技术栈 | Next.js + Tailwind + shadcn/ui + `@langchain/langgraph-sdk` + framer-motion |
| 关键目录 | `src/components/thread/`、`src/providers/Stream.tsx`、`src/components/thread/agent-inbox/` |

**可借鉴的具体 UI 模式**

1. **工具调用表格化**（`src/components/thread/messages/tool-calls.tsx`）：
   `ToolCalls` 把 `tool_calls[].args` 渲染成两列表格（参数名 / 值，复杂值 `<code>` 美化 JSON）；`ToolResult` 超过 **4 行或 500 字符**就截断，给展开按钮；内容是 JSON 时**按数组/对象渲染成表格**（数组只显示前 5 条）。
   → 我们的「工具步」展开区可以照这个规则：默认 4 行 / 500 字符，超出折叠；JSON 结果表格化。
2. **Interrupt 面板**（`src/components/thread/agent-inbox/index.tsx`）：
   多个 interrupt 用**胶囊 Tab** 切换（标题取 `action_requests[0].name`）；右侧可切到 `StateView` 看线程状态或 `description` 看动作说明；**一次只处理一个**，避免用户误判。
   → 我们的审批面板若一次来多条，也应该「列表 + 选中详情」而不是平铺全部详情。
3. **Stream Provider 与 UI 解耦**（`src/providers/Stream.tsx`）：流事件 → 消息数组，UI 只读数组。
   → 与我们 `app.js` 里「事件 → store → 分区渲染」同构，可参考它处理「工具调用必须有 response」的兜底（`ensure-tool-responses.ts`：没有结果的工具调用补一条占位，避免 UI 卡在「调用中」）。

**不采纳**：framer-motion 动效（体感收益 < 包体与卡顿成本）；其 inbox 与 2.2 重复。

### 2.4 ag-ui — Agent ↔ 前端事件协议（**事件词汇表**参考，非 UI）

| 项 | 值 |
| --- | --- |
| 链接 | https://github.com/ag-ui-protocol/ag-ui |
| License | **MIT** |
| 体积 | zip 26.9 MB → 解压后**剪枝**到 **43.5 MB / 4235 files**（删除 `integrations/` 26.9MB 的各框架样例，保留 `spec/`、`sdks/`、`apps/dojo`、`docs/`） |
| 快照 | `1f694eba3a4f`（2026-11-06） |
| 关键目录 | `spec/draft/schema.json`（96 KB JSON Schema）、`spec/draft/conformance/streams/*.json`（60+ 行为用例）、`sdks/typescript/`、`apps/dojo/` |

**可借鉴的具体模式**

1. **事件词汇表**（`spec/draft/schema.json` → `$defs.Event`）：
   `RUN_STARTED / RUN_FINISHED / RUN_ERROR`、
   `STEP_STARTED / STEP_FINISHED`、
   `TEXT_MESSAGE_START / CONTENT / END / CHUNK`、
   `TOOL_CALL_START / ARGS / END / CHUNK / RESULT`、
   `STATE_SNAPSHOT / STATE_DELTA`（JSON Patch）、`MESSAGES_SNAPSHOT`、
   `ACTIVITY_SNAPSHOT / ACTIVITY_DELTA`、
   `REASONING_*`（推理流，与正文分离）、
   `SUBAGENT_STARTED / SUBAGENT_FINISHED / SUBAGENT_ERROR`（带 `subagentRunId` 归属）、
   `RAW / CUSTOM`（透传）。
   → 我们的 SSE 事件命名建议**对齐这套词汇**（`run.*`、`step.*`、`tool.*`、`state.*`、`subagent.*`），好处是将来接任何 Agent 框架都只是映射，不用重设计；尤其是 `SUBAGENT_*` 正好对应「多 Agent 编队」的成员生命周期。
2. **行为规则可测**（`spec/draft/conformance/streams/`）：
   `event-after-run-finished-fatal`（run 结束后再来事件 = 致命）、
   `open-message-at-run-finished-fatal`（消息没关闭就结束 run）、
   `state-delta-unappliable-warns-and-keeps`（补丁打不上就告警并保留旧状态）、
   `tool-result-error-cleared-by-correction`（错误结果可被后续纠正清除）、
   `subagent-attribution-mismatch-fatal`（子 Agent 归属不匹配）。
   → 这些正是我们事件总线 + 前端 reducer 的**测试清单**：可以直接改写成 pytest 用例（事件乱序、重复、缺失时 UI 不崩、状态不丢）。
3. **`STATE_SNAPSHOT` / `STATE_DELTA` 分离**：大状态用快照 + JSON Patch 增量。
   → 我们的「黑板」更新频繁，若整块重推会拖慢桌面端；建议黑板事件用 `state.snapshot`（首次/重置）+ `state.delta`（增量），前端按 `id` 合并。

**不采纳**

- 完整实现 AG-UI 协议（含 protobuf 冻结、加密推理值、多语言 SDK）：对单机工作台是过度设计；**只借词汇表与不变量**。
- `apps/dojo` 的 React 演示栈：不引入。

**剪枝说明**：`integrations/` 是 LangGraph/CrewAI/Mastra 等框架的接入样例，与 UI 设计无关且占 27 MB，已删除；其余保留以便核对 schema 与 conformance 用例。

---

## 3. 横向对照矩阵（模式 × 来源 × 本项目决策）

| 模式 | xuanwo | kagent | open-agent-platform | agent-chat-ui | ag-ui | 本项目决策 |
| --- | --- | --- | --- | --- | --- | --- |
| 三栏 + topbar + status strip | ✅ 两套 | ✅ 侧轨+对话+上下文 | ✅ sidebar+主区 | ➖ | ➖ | **采纳**（UI-OPS-REDESIGN §2） |
| 分区签名 diff / 局部重绘 | ✅ 5 区 | ✅ SWR 缓存 | ✅ React 局部 | ✅ | ➖ | **采纳**（P0） |
| 未知 ≠ 0 | ✅ `—` | ✅ `—` + 理由 | ➖ | ➖ | ✅ 规则化 | **采纳**（P0） |
| blocked / failed / stopped 四分 | ✅ | ✅ 六态 + tooltip | ✅ 状态胶囊 | ➖ | ➖ | **采纳**（P0） |
| 每 Agent 计分卡（工具/写板/滞后） | ✅ | ➖ | ➖ | ➖ | ➖ | **采纳**（P1） |
| 对话块模型 + 工具步状态 | ✅ | ✅ | ✅ | ✅ | ✅ 事件 | **采纳**（P0） |
| 对话窗口化 + 回到最新 | ✅ | ➖ | ✅ stick-to-bottom | ✅ | ➖ | **采纳**（P0） |
| 审批三栏卡 + 批量 + 记住放行 | ✅ | ✅ 审批记录卡 | ✅ Inbox | ✅ interrupt | ✅ 规则 | **采纳**（P1） |
| 审批「收件箱」聚合视图 | ➖（散在右栏） | ➖ | ✅ | ✅ | ➖ | **采纳**（P1，合并等待人工） |
| 快照 = 对话中的分叉点 | ➖（快照对比） | ✅ Checkpoint | ➖ | ➖ | ✅ STATE_SNAPSHOT | **部分采纳**：先做快照对比，不做分叉 |
| 事件词汇/不变量 | ➖ | ➖ | ➖ | ➖ | ✅ | **采纳词汇表**，不实现协议 |
| 状态条常驻运行指标 | ✅ | ➖ | ➖ | ➖ | ➖ | **采纳** |
| 快捷键 `/` `Ctrl+Enter` `Esc` | ✅ | ➖ | ➖ | ➖ | ➖ | **采纳**（P2） |
| 深色主题 / 紫色品牌 | ➖ | ✅ | ➖ | ➖ | ➖ | **不采纳**（与暖纸色冲突） |
| 组件库（antd / shadcn / React） | ➖（原生 DOM） | ✅ | ✅ | ✅ | ➖ | **不采纳**（无构建约束） |
| RAG / 工具市场 / MCP 管理 | ➖ | ✅ | ✅ | ➖ | ➖ | **不采纳**（超范围） |
| 分叉 / 重放 / 时间旅行 | ➖ | ✅ | ➖ | ➖ | ✅ | **暂缓**（需后端支持） |

---

## 4. 落地建议（映射到本项目）

### 4.1 骨架（`index.html`）

```
topbar 64px        ：brand · 项目选择 · [新建][导入][设置][刷新] · 连接状态 chip · 运行指示
sidebar 220px      ：项目卡 · 导航分组（指挥台/任务/依赖图/黑板/交接/审批/快照/Agent/观测/配置）· 底部状态
center  (1fr)      ：section.view#view-<name>，唯一滚动容器
inspector 360px    ：工作台状态 · 编队 2×2 · 启动/停止/恢复 · 待审批 · 运行中 · 事件流
status strip 28px  ：WS · AGENTS 4/4 · TASKS 3/8 · APPROVALS 2 · AUTOPILOT OFF · 快捷键提示
```

- 断点：`≤1240` 收窄 inspector 到 320；`≤1040` inspector 变右侧浮层（照 xuanwo `styles.css:2271-2283`）；`≤760` 单列。
- sidebar 可折叠为图标条（借 open-agent-platform）。

### 4.2 渲染层（`app.js`）

```js
// 1) 分区签名：只有签名变了才重建
const regions = { topbar:null, sidebar:null, center:null, inspector:null, status:null };
function paintRegion(name, data, renderFn) {
  const sig = JSON.stringify(data);
  if (regions[name] === sig) return;
  regions[name] = sig;
  renderFn();
}
// 2) 合并刷新：120ms 内多次事件只画一次
// 3) 预算：单次 >32ms 打 console.warn，便于回归
// 4) 对话流窗口：只保留最近 200 块；贴底才自动跟随；否则显示「回到最新 N 条」
```

### 4.3 状态语义（任务 / Agent / 审批统一）

| 语义 | 视觉 | 适用 |
| --- | --- | --- |
| 未知 | 灰 + `—` + tooltip 说明「尚未读取到」 | 所有指标、Agent 用量 |
| 空 | 虚线框空状态 + 一句解释 + 主/次按钮 | 列表、看板、审批 |
| 进行中 | 蓝/绿 + 呼吸点 | running / waiting 之外的活跃态 |
| 等人工 | 黄 + 计数徽标 | waiting_approval / interrupted |
| 失败 | 红字（无边框） | failed / error |
| 被阻断 | 红字 **+ 红边框** + 原因 | blocked（策略/依赖） |
| 成功终态 | 绿 | success |
| 跳过/取消 | 灰 | skipped / cancelled |

### 4.4 事件契约（建议新增到 ARCHITECTURE.md）

最小词汇（对齐 ag-ui，前缀式命名）：

```
run.started / run.finished / run.error
step.started / step.finished
message.start / message.delta / message.end        （agent 输出）
thought.start / thought.delta / thought.end        （推理，弱化渲染）
tool.start / tool.args / tool.end / tool.result    （含 status: in_progress|done|failed|cancelled）
state.snapshot / state.delta                       （黑板/项目状态，JSON Patch）
approval.requested / approval.decided              （等待人工）
subagent.started / subagent.finished / subagent.error  （多 Agent 编队）
```

不变量（可直接写成 pytest）：
1. `run.finished` 之后同一 run 不再接受事件（丢弃并 warn）；
2. `message.*` / `tool.*` 必须成对闭合，缺失时前端补占位而不是卡住；
3. `state.delta` 打不上时保留旧状态并提示，不整块清空；
4. `subagent.*` 的 `parent_run_id` 必须存在，否则丢弃；
5. 重复事件（同 id 同 payload）幂等。

### 4.5 审批视图（`view-approvals`）

- 顶部：`待批 N · 全部任务/当前任务 切换 · 全部放行 · 全部拒绝`。
- 卡片三栏：`动作 + 目标` ｜ `Agent 自述意图` ｜ `命中规则 + 风险 + 同类历史`；底部 `放行 / 记住并放行(会话|Agent) / 拒绝`。
- 已决区折叠，`opacity .62`，状态胶囊 `已批准 / 已拒绝 / 超时 / 自动`。
- 空状态：显示「规则已加载」+ 两种可能原因（照 xuanwo）。

### 4.6 观测视图（`view-observe`）

- KPI 用 kagent StatTile 口径：`任务总数 / 执行中 / 等待人工 / 成功 / 失败`，读失败显示 `—` 并在 banner 点名。
- 时间线（`timeline`）按 xuanwo 的「活动行」渲染：`[证据等级/事件类型] 文本`，最多 4–8 条 + 「查看全部」。
- 产物（`artifacts`）与 xuanwo 的 WP/附件列表同构：标题 + 路径（等宽）+ 原因/大小。

---

## 5. 未下载的候选（体积过大或与 UI 无关，仅保留链接与判断）

| 项目 | 链接 | License | 未下载原因 | 是否有参考价值 |
| --- | --- | --- | --- | --- |
| assistant-ui | https://github.com/assistant-ui/assistant-ui | MIT | zip **58.4 MB**，超过 50 MB 上限，已跳过 | 有：对话线程原语（消息分段、自动滚动、工具 UI），但我们是原生 DOM，收益中等 |
| AutoGen Studio | https://github.com/microsoft/autogen | MIT | 仓库巨大（monorepo，含 Python/文档/样例），整包远超 50 MB | 有：Agent 团队编排 + 运行面板；建议按需单目录 sparse checkout |
| Langflow | https://github.com/langflow-ai/langflow | MIT | 前端依赖重、仓库大 | 中：可视化编排画布，若做「依赖图」可参考节点/边交互 |
| Dify | https://github.com/langgenius/dify | Apache-2.0（含附加条款） | 仓库大 | 中：工作流编排 + 日志面板 |
| OpenHands | https://github.com/All-Hands-AI/OpenHands | MIT | 仓库大（前后端+运行时） | 高：会话/步骤时间线、终端回放；可后续单独拉 `frontend/` |
| AgentOps | https://github.com/AgentOps-AI/agentops | Apache-2.0 | 主要面向 SDK 与 SaaS 仪表盘 | 中：运行追踪视图 |

> 上表 License 依据仓库页面记录，**未逐字核对**（未下载）；正式复用前请以各仓库 `LICENSE` 文件为准。
> 若后续需要，推荐用 `git clone --filter=blob:none --sparse` 或 GitHub API 单目录下载，避免整包。

---

## 6. 下载清单与失败记录

### 6.1 实际落地

| 名称 | 路径 | 解压体积 | 文件数 | zip | 快照 SHA | License |
| --- | --- | --- | --- | --- | --- | --- |
| kagent | `reference/github/kagent/` | 18.13 MB | 1958 | 7.0 MB | `58a0d70d44be` | Apache-2.0 |
| ag-ui（已剪枝） | `reference/github/ag-ui/` | 43.52 MB | 4235 | 26.9 MB | `1f694eba3a4f` | MIT |
| open-agent-platform | `reference/github/open-agent-platform/` | 1.23 MB | 229 | 0.4 MB | `6a2ea0a3d019` | MIT |
| agent-chat-ui | `reference/github/agent-chat-ui/` | 0.47 MB | 74 | 0.2 MB | `0d21f7ff4c45` | MIT |

合计约 **63.4 MB**，均在 `reference/github/` 下（未触碰 `reference/acp-bridge`、`reference/agent-workbench`、`reference/awesome-agent-orchestrators`）。

### 6.2 失败与处理

| 目标 | 结果 | 原因 / 处理 |
| --- | --- | --- |
| `assistant-ui/assistant-ui` | **跳过** | codeload zip 58.4 MB，超过「不下载 >50 MB 仓库」的约束；已在 §5 记录链接与判断 |
| `ag-ui` 整包 | **部分保留** | 解压 66.1 MB 偏大，删除 `integrations/`（26.9 MB 框架样例）后 43.5 MB；`spec/`、`sdks/`、`apps/dojo`、`docs/` 完整保留 |
| AutoGen Studio / Langflow / Dify / OpenHands | **未下载** | 仓库体积远超上限，且非 UI 单文件；已记录链接，建议后续 sparse checkout |

### 6.3 复现命令

```powershell
$ProgressPreference='SilentlyContinue'
$base='reference/github'
$repos=@(
  @{n='kagent';              o='kagent-dev/kagent'},
  @{n='ag-ui';               o='ag-ui-protocol/ag-ui'},
  @{n='open-agent-platform'; o='langchain-ai/open-agent-platform'},
  @{n='agent-chat-ui';       o='langchain-ai/agent-chat-ui'}
)
foreach($r in $repos){
  $zip = Join-Path $env:TEMP ("ref_"+$r.n+".zip")
  Invoke-WebRequest "https://codeload.github.com/$($r.o)/zip/refs/heads/main" -OutFile $zip -UseBasicParsing
  if((Get-Item $zip).Length -gt 50MB){ "SKIP $($r.o)"; continue }
  $tmp = Join-Path $env:TEMP ("ref_"+$r.n); Expand-Archive $zip -DestinationPath $tmp -Force
  Move-Item (Get-ChildItem $tmp -Directory | Select-Object -First 1).FullName (Join-Path $base $r.n)
}
```

---

## 7. 维护

- 新增参考仓库时：更新 §6.1 表格 + `reference/github/SOURCES.md`，并在 §3 矩阵补一行。
- 若某参考被采纳进实现：在对应「本项目决策」列改为 `已落地（PR/文件）`，避免文档与代码脱节。
- 参考仓库均为快照，不参与构建与测试；`reference/` 不加入 lint/typecheck 范围。
