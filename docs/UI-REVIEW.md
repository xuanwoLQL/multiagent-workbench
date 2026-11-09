# 指挥台 UI 设计走查（index.html / style.css）

> 走查人：research · 2026-11-06 23:20–23:35
> 依据：[DESIGN-REFERENCES.md](./DESIGN-REFERENCES.md) 的检查表 + [UI-OPS-REDESIGN.md](./UI-OPS-REDESIGN.md) §2/§4/§7 验收口径
> 只读走查：**未修改** `multiagent/web/` 下任何文件；本文只列问题与建议。
>
> **走查快照**（行号以此为准）：
> | 文件 | 行数 | sha256(前 12) | mtime |
> | --- | --- | --- | --- |
> | `index.html` | 737 | `0E38E67FCA70` | 23:31:49 |
> | `style.css` | 1454 | `B8014C0517A4` | 23:32:29 |
> | `app.js`（行为侧参照） | 2911 | `EC5340ED43A5` | 23:31:20 |
>
> ⚠️ 走查期间三个文件都在被 ui-shell / ui-logic 持续修改（`style.css` 从 1360 行涨到 1467 行）。**本文列出的 6 个问题在走查过程中已被修复**（见 §1.1），其余结论已按 23:32 快照复核；**行号仅用于快速定位，请以选择器为准**（后段行号可能已再漂移 ±10 行）。

---

## 0. 结论摘要

**总评**：骨架、令牌、空状态、动效克制都达标；**首屏可读性**是当前唯一「一眼可见」的硬伤，其次是 1440 宽度下的看板密度。状态诚实性在走查期间已被大幅改善（KPI/状态条未知态、`#inspector-mode`、卡片键盘激活都已落地）。

| 级别 | 条数 | 主题 |
| --- | --- | --- |
| **P0** | **2** | 说明文字对比度不达标 + 中文小字过小；表单控件键盘焦点不可见 |
| P1 | 9 | 1440 看板密度断崖、901–1180 中栏过窄、≤900 核心操作不可达、空项目多重空状态、blocked/failed 同色、inspector 层级、DAG 文字缩放、嵌套滚动、切视图不重置滚动 |
| P2 | 11 | 字号梯度、aria-current、skip link、抽屉可 Tab、弹窗初始焦点、Space 键、死代码、顶栏留白、KPI 3+2、首帧假数据、内边距节奏 |
| 已修复（复核确认） | 6 | 见 §1.1 |

**验收风险**：1440×900 截图会呈现「6 列窄卡 + 大量 8px 灰字」；1280×800 反而更舒展（3 列）。建议 P0 两条修完再截图，P1-1/P1-5 顺手一起改（合计约 15 行 CSS）。

---

## 1. 走查过程中已修复的问题（复核确认，供 Lead 记账）

| # | 原问题 | 修复证据（当前快照） | 状态 |
| --- | --- | --- | --- |
| F-1 | Agent 瓦片 `.agent-state` 无样式，13px 文本撑破 154px 瓦片 | `style.css` 新增 `.agent-state{ grid-column:2; grid-row:3; font:500 8px var(--ops-mono); … }`（≈1159-1168） | ✅ |
| F-2 | 状态条错误态类名不匹配（JS `bad` vs CSS `.is-bad`） | `style.css` 有 `.status-item.is-bad`（≈1253-1256）；`app.js` `setStatus(id,text,level)` 用 `is-ok/is-bad/is-busy` | ✅ |
| F-3 | `#inspector-mode` 写死 IDLE，永不更新 | `app.js:1112` `setText("inspector-mode", running ? "RUNNING" : "IDLE")` | ✅ |
| F-4 | 状态条未知显示 `AGENTS 0/0`（把未知伪装成 0） | `app.js:1107-1110` 未加载时输出 `AGENTS —/—` / `TASKS —` / `APPROVALS —` | ✅ |
| F-5 | KPI 未知显示 0 | `renderHomeMetrics`：`known ? fmtInt(...) : "—"`，未加载时 5 个 KPI 全为 `—` 并带 `title="还没有加载到数据"` | ✅ |
| F-6 | 任务卡 `role="button"` 但 Enter 不触发 | `app.js:2699-2705` 对 `[data-act]` 且非 `BUTTON` 的目标做 Enter→click | ✅（Space 键仍缺，见 P2-6） |

> 说明：这 6 条在我第一轮走查时确实存在（当时 app.js 还是 `bad` 类名、无 `.agent-state`），复核时已被修掉——记录在此避免 Lead 重复派单。

---

## 2. P0（必须修，共 2 条）

### P0-1 说明文字对比度不达标、中文小字过小

**现象**：首屏所有「说明性文字」都偏灰偏小，在 1440 截图里几乎读不出来；看板空列、输入框占位符尤其淡。

**证据（WCAG 2.1 实测对比度，背景取实际底色）**：

| 用途 | 选择器 / 行号 | 前景 / 背景 | 实测 | 要求 |
| --- | --- | --- | --- | --- |
| 全部 hint / label / 元信息 | `--ops-soft: #8b949c`（`style.css:14`）on `--ops-paper` | 8b949c / f2f0ea | **2.70 : 1** | 4.5 |
| 卡片内说明 | `--ops-soft` on `--ops-card` | 8b949c / fffdf8 | **3.03 : 1** | 4.5 |
| 看板空列「暂无」 | `.kcol-empty` `style.css:755-763`（`color:#a9a294` 在 761） | a9a294 / f2f0ea | **2.22 : 1** | 4.5 |
| 输入框 placeholder | `.input::placeholder` `style.css:197-198` | a8a196 / fffefb | **2.54 : 1** | 4.5 |
| 区块小标题（accent） | `.kicker` `style.css:570-576`、`.empty-mark` `style.css:874` | df542f / fffdf8 | **3.79 : 1** | 4.5（8–8.5px 属小字） |
| 事件流元信息 | `.stream-meta` `style.css:1220` | a9a294 / fbf9f3 | **2.3 : 1** | 4.5 |
| 跳过/取消标签 | `.tag.skipped` `style.css:956-957` | 7d8790 / f0eee7 | **3.15 : 1** | 4.5 |
| 状态条 | `.statusstrip` `style.css:≈1230-1243` | 66717c / faf8f2 | 4.69 : 1 | 4.5 ✓（临界） |

**字号**：以下**中文**文本小于 10.5px，在 100% 缩放下明显吃力：
`.kpi-label` 8.5px（`style.css:716-722`，文案「任务总数/执行中/等待人工/成功/失败」）、
`.kcol-name` 9.5px（`:743-753`）、`.kcol-empty` 9px（`:755-763`）、
`.nav-group-title` 8.5px（`:476-482`，文案「指挥/协作/系统」）、
`.foot-line` 8px（`:552`，含中文「数据留在本机工作区」）、
`.empty-hint` 10px（`:876`）、`.agent-role` 7.5px（`:1136`，ASCII，可接受）。

**影响**：验收「首屏一屏看懂还差什么」不成立——用户看不清列名与说明；无障碍层面属于 WCAG AA 失败。

**建议**（不动布局，只调令牌与字号）：
```css
:root{
  --ops-soft: #5f6870;        /* 2.70 → 5.0 : 1（paper）*/
  --ops-soft-rule: #8b949c;   /* 原值只用于装饰（虚线、点、分隔）*/
  --ops-muted: #5b6670;       /* 4.37 → 5.16 : 1（paper）*/
}
.kcol-empty{ color: var(--ops-muted); }              /* 2.22 → ~4.4 */
.input::placeholder,.textarea::placeholder{ color:#7c7568; }  /* ~3.5，占位符可放宽 */
.kicker,.empty-mark{ color:#b8431f; }                /* 3.79 → ~5.2 */
.kpi-label,.kcol-name,.nav-group-title{ font-size:10.5px; }
.empty-hint{ font-size:10.5px; color:var(--ops-muted); }
.stream-meta{ color:var(--ops-soft); }               /* 随令牌一起变深 */
```
> `--ops-soft` 在参考实现（xuanwo）里也是 `#8b949c`、同样的对比度问题；我们是新建，没有历史包袱，建议直接改深。改完请顺手核对 `--ops-soft` 是否只用于文字（装饰用的点/虚线可保留浅色）。

---

### P0-2 表单控件键盘焦点不可见（违反契约 §4.4）

**现象**：Tab 到「产出文件 / 执行者 / 类型 / 检索框 / 配置编辑器」等输入控件时，**看不到焦点在哪**，只剩 1px 边框变色 + 几乎不可见的阴影。

**证据**：
- `style.css:74-77` 定义了全局 `:focus-visible { outline: 2px solid var(--ops-blue); outline-offset: 1px; }`；
- 但 `style.css:168-182` 的 `.select, .input, .textarea { … outline: none; … }`（`outline:none` 在 **177** 行）**特异性相同（0,1,0）且在后**，按层叠顺序覆盖了全局焦点环；
- `.select:focus`（`style.css:185-190`）只给 `border-color` + `box-shadow: 0 0 0 2px rgba(23,33,43,0.07)`（透明度 7%），肉眼几乎不可辨。

**影响**：契约 §4.4 明文要求「键盘 Esc 关闭弹窗/抽屉；焦点样式可见」。键盘用户与自动化验收会直接判不通过。

**建议**（二选一）：
```css
/* 方案 A：删掉 .select/.input/.textarea 里的 outline:none */
/* 方案 B：显式恢复 */
.select:focus-visible,.input:focus-visible,.textarea:focus-visible{
  outline: 2px solid var(--ops-blue); outline-offset: 1px;
}
```
并给 `.kcard:focus-visible` 加 `outline-offset:2px`（卡片已有 `tabindex="0"`），避免焦点环贴边。

---

## 3. P1（建议本轮修）

### P1-1 1440 宽度下 6 列看板过窄，且 1380 断点存在「密度断崖」
- `style.css:≈736-740` `.kanban { grid-template-columns: repeat(6, minmax(0,1fr)); gap:8px }`；
- 1440：中栏 = 1440−220−360 = 860，减 `.center` 左右 padding 40（`:557-563`）→ 内容 820 → 每列 **(820−40)/6 ≈ 130px**；再减 `.kcol` padding 14 + `.kcard` padding 14 → 标题可用宽约 **100px**，10.5px 中文约 9 字/行、2 行封顶 ≈ 18 字，稍长的任务名就截断；
- 断崖：`@media (max-width:1380px)`（`style.css:≈1420-1425`）把看板切成 3 列。1381px 是 6 列 × 120px，1380px 是 3 列 × 215px——**1px 之差密度翻倍**，而 1440 恰在断崖的「窄」一侧。

**建议**：
```css
@media (max-width:1560px){ .kanban{ grid-template-columns:repeat(3,minmax(0,1fr)); } }
```
（契约要求「6 列」，1280–1560 用 3×2 两行仍保留全部 6 个状态列，语义不丢；或给列加 `minmax(150px,1fr)` + 横向滚动。）

### P1-2 901–1180 区间 inspector 仍占 360px，中栏被压到 444px
- 断点只有 `max-width:1180`（`style.css:≈1427-1432`，只隐藏 brand-sub/head-meta）与 `max-width:900`（`:≈1452`，直接隐藏 inspector）；
- 1024×768 笔记本：中栏 = 1024−220−360 = **444px**，减 padding 40 → 404px；看板 3 列 × 129px、`.mission-options` 三列各约 120px，输入框极窄。
- **建议**：~1180 就把 inspector 改成可收起的浮层（参考 xuanwo `styles.css:2271-2283`：`position:fixed; right:0; width:min(390px,42vw)`），或加折叠按钮（`aria-expanded`），中栏至少保 600px。

### P1-3 ≤900 直接 `display:none` 掉 inspector，核心操作不可达
- `style.css:≈1452-1457`：`.inspector{ display:none }`；
- 但「启动调度 / 停止 / 恢复 / 强制恢复 / 待审批 / 实时事件」**只存在于 inspector**（`index.html:607-694`），窄屏下没有任何替代入口。
- **建议**：`≤900` 改为抽屉式——topbar 加 `#btn-inspector`（`aria-controls="inspector"`），点击以 overlay 打开；至少保证「启动调度」「待审批」可达。

### P1-4 空项目首屏出现 4 类空状态，显得空洞
- 无任务时同时可见：`#home-onboarding`（`index.html:262-270`）+ 看板 6 个 `.kcol-empty`「暂无」（`:219-242`）+ 最近活动空盒（`:253-257`）+ inspector 三处空盒（`:662-692`）；
- `renderOnboarding`（app.js）只切换 onboarding 显隐，**不隐藏看板与活动**。
- **建议**：`progress.total === 0` 时隐藏 `.board-card` 与 `.activity-card`，只留 onboarding 大卡（一个空状态原则）；inspector 三块空盒在无项目时合并为一句「选择项目后显示」。

### P1-5 `blocked` 与 `failed` 完全同色，无法区分
- `style.css:954-955`：`.tag.failed, .tag.blocked { border-color:#d8a5a4; background:#fbefed; color:var(--ops-red); }` —— 两者样式**完全相同**；
- 看板列名「失败 · 阻塞」把两类合并成一列（符合契约，`app.js` COLUMNS 里 `failed` 列 statuses=`["failed","blocked"]`），但**卡片级必须能区分**：failed 是执行出错，blocked 是依赖/策略挡住，处置动作完全不同。
- **建议**：`blocked` 用「红字 + 红左边框 2px」或加 `⛔` 前缀；`failed` 保持红字无边框（对齐 DESIGN-REFERENCES §1.3/§4.3 的语义表）。

### P1-6 inspector 信息层级偏平，危险操作过于显眼
- 5 个 section（调度/编队/待审批/运行中/实时事件）标题都是 8.5px 大写 mono（`.inspector-section-title`，`style.css:≈1093-1099`），权重一致；
- 「强制恢复」（破坏性，`index.html:624-625`）是常驻 danger 按钮，紧贴 primary「启动调度」，误点风险高；
- **建议**：①「Agent 编队」加边框强调（`border-color:var(--ops-rule-strong)` + 背景 `--ops-card`），它是本工作台的主角；②「强制恢复」收进「⋯」菜单或折叠为 `<details>`，保留二次确认；③ 待审批数已进状态条，inspector 可只留前 3 条 + 「查看全部」。

### P1-7 DAG 文字随 viewBox 缩放，实际只有 ~8.6px
- `index.html:332`：`<svg viewBox="0 0 1000 520" preserveAspectRatio="xMidYMid meet">`；
- `style.css:1061-1062`：`.dag-empty-title{ font:700 13px }`、`.dag-empty-hint{ font:500 10px }` —— SVG 内文字随 viewBox 等比缩放，中栏内容宽 660px 时缩放比 0.66 → **实际 8.6px / 6.6px**；
- `style.css:1041-1050`：`#dag { min-height:460px }` 与 1000×520 的比例（渲染高应 ≈343px）冲突 → 上下留白约 117px。
- **建议**：SVG 内字号按目标渲染宽度反算（660px 宽时用 20px/15px），或把空状态文字改为 HTML 覆盖层；`min-height` 改为 `aspect-ratio:1000/520` 让高度自适应。

### P1-8 inspector 内嵌 `.stream` 造成二次滚动
- `.inspector` 本身 `overflow-y:auto`（`style.css:1067-1073`），内部 `.stream` 又 `max-height:170px; overflow-y:auto`（`:1190-1199`）；
- 滚轮落在事件流上只滚事件流、不滚 inspector，触控板用户容易「滚不动」。
- **建议**：去掉 `.stream` 的 `max-height` 让它自然展开（inspector 统一滚动）；若必须限高，加 `overscroll-behavior: contain` 并给明显「展开」入口。

### P1-9 切换视图不重置中栏滚动位置
- 唯一页面滚动容器是 `.center`（`style.css:557-563`）；`switchView`（app.js）只切 `.view` 的 `is-active`，**没有 `scrollTop = 0`**（全文 0 命中）；
- 从滚到一半的「任务」切回「指挥台」时浏览器保留 scrollTop，用户可能落在首屏标题之下。
- **建议**：`switchView` 里加 `$("center").scrollTop = 0`（或按视图记忆位置，但首次进入必须置顶）。

---

## 4. P2（可排到下一轮）

| # | 问题 | 证据 | 建议 |
| --- | --- | --- | --- |
| P2-1 | 字号梯度 17 档（7.5/8/8.5/9/9.5/10/10.5/11/11.5/12/12.5/13/14/15/19/25/38），视觉噪音大 | `style.css` 全文 | 收敛为 6 档：10 / 11 / 12 / 13 / 16 / 24 / 36 |
| P2-2 | 导航 `aria-current` 只在首页静态写死，切换后不更新 | `index.html:69`；app.js 无 `aria-current` | `applyViewClasses` 同步 `aria-current="page"` |
| P2-3 | 无「跳到主内容」skip link | `index.html:10-11` | `<a class="skip" href="#center">跳到主内容</a>` |
| P2-4 | 抽屉关闭时仍在 Tab 序列（`transform: translateX(102%)` 只移出视口，未 `visibility:hidden`） | `.drawer`（`style.css:≈1263`）；全文无 `visibility` | 关闭态加 `visibility:hidden` / `inert` |
| P2-5 | 弹窗无初始焦点、无焦点陷阱（Esc 已有 ✓） | `index.html:722-731` | 打开时 focus 首个可操作元素 |
| P2-6 | 任务卡只处理 Enter，未处理 Space（`role="button"` 惯例要求两者） | `app.js:2699-2705` | 加 `event.key === " "` 分支 |
| P2-7 | 死代码/兼容层类：`.live-list`（`height:calc(100vh - 250px)` 在嵌套布局会溢出）、`.metrics/.panel/.column/.card/.empty`；JS 仍加 CSS 没有的 `is-visible` | `style.css:≈1332-1410`；`renderOnboarding` | 确认 app.js 不再使用后删除，或标 `@deprecated` |
| P2-8 | 顶栏中段大量留白（1280 下 spacer ≈ 490px） | `index.html:30` `.spacer` | 中间放「当前视图标题 / 项目路径面包屑」，或把「新建/导入/设置」右移 |
| P2-9 | ≤900 KPI 变 3 列 → 5 个指标排成 3+2，视觉不齐 | `style.css:≈1455` | 改 2 列（3 行）或横向滚动 5 列 |
| P2-10 | 首帧假数据：静态 HTML 写死 4 个 Agent 瓦片（`index.html:633-654`，Codex/Claude/OpenCode/DSH 带 `is-on`）、侧栏点阵（`:54-59`）、`#run-status` 初值「空闲」；JS 首帧前用户看到的是未经验证的状态 | `index.html:39/54-59/633-654` | 静态部分改骨架屏或 `—`；或首帧前给 `.is-loading` 遮罩 |
| P2-11 | 内边距节奏不一致（center 36 / inspector 24 / sidebar 16）；`renderNoProject` 把 `#dag` 清成空白 460px 卡片 | `style.css:557/1054/374`；app.js `renderNoProject` | 统一底部 24；DAG 空态保留 SVG 空状态文案 |

---

## 5. 检查表逐项对照

| # | 检查项（Lead 指定） | 结论 | 关键证据 |
| --- | --- | --- | --- |
| 1 | 三栏在 1280 / 1440 的密度 | ⚠️ 1280 好（3 列 × 215px）；**1440 差**（6 列 × 130px，标题约 18 字封顶） | P1-1；`.kanban`≈736、断点≈1420 |
| 2 | 中栏是否唯一滚动 | ⚠️ 页面级只有 `.center`（`body overflow:hidden` ✓），但 sidebar/inspector 各自滚动 + `.stream` 二次滚动；切视图不重置位置 | P1-8、P1-9；`style.css:39-41/557/1067/1190` |
| 3 | inspector 信息层级 | ⚠️ 5 个 section 同权重；破坏性按钮常驻且紧邻主按钮 | P1-6 |
| 4 | 状态语义（blocked vs failed vs 未知） | ⚠️ 未知态已修好（KPI/状态条 `—`、`#inspector-mode` 实时）；**blocked 与 failed 仍同色** | P1-5；已修复 F-3/F-4/F-5 |
| 5 | 对比度与字号 | ❌ 系统性（2.22–3.79 : 1；中文 8–9.5px） | P0-1 |
| 6 | 空状态 | ⚠️ 单个空状态质量好（mark/title/hint/actions），但**首页同时出现 4 类空状态** | P1-4；`style.css:861-881` |
| 7 | 键盘可达性 | ❌ 输入控件无焦点环（P0-2）；抽屉可 Tab；无 skip link；Space 未处理 | P0-2、P2-3/4/6 |
| 8 | prefers-reduced-motion | ✅ 已覆盖（动画/过渡压到 0.001ms） | `style.css:≈1459-1467` |
| 9 | 其他（可维护性） | ⚠️ 字号 17 档、兼容层死代码、类名契约无自动检查 | P2-1、P2-7 |

---

## 6. 做得好的地方（建议保留）

1. **令牌体系完整**：`--ops-*` 16 个语义色 + 三套字体栈（`style.css:8-29`），与契约 §1 一致。
2. **空状态四件套**：`mark / title / hint / actions`（`style.css:≈869-889`），每个视图都有针对性文案。
3. **动效克制**：120–150ms，`ops-fade/ops-pop/ops-pulse` 三个关键帧（`:94-106`），有 reduced-motion 兜底。
4. **按钮反馈**：`.btn.is-loading` 自带旋转指示（`:310-319`），满足契约「100ms 内视觉反馈」。
5. **长文本兜底**：`.kcard-title` 两行 clamp、`.identity-path` 两行 clamp、`overflow-wrap:anywhere` 多处。
6. **新增 `.fill-card`**（`style.css:≈995-1001`）：列表型视图撑满可视高度、空态铺满，直接解决「半屏空白」验收项。
7. **可访问性基础**：`role="status" aria-live="polite"`（toast）、`aria-modal="true"`（modal）、`sr-only` label、`role="button" tabindex="0"` 都已就位。

---

## 7. 建议修复顺序

| 顺序 | 动作 | 改动量 | 收益 |
| --- | --- | --- | --- |
| 1 | P0-1 调 3 个令牌 + 5 处字号/颜色 | ~12 行 CSS | 首屏可读性、截图验收 |
| 2 | P0-2 恢复焦点环 | 3 行 CSS | 键盘可用、契约合规 |
| 3 | P1-1 断点 1380→1560 | 1 行 | 1440 首屏密度 |
| 4 | P1-5 blocked 差异化 | 3 行 CSS | 状态语义 |
| 5 | P1-4 空项目只留 onboarding | JS 少量 | 首屏观感 |
| 6 | P1-2/P1-3 窄屏 inspector 抽屉化 | 中等 | 1024 笔记本可用 |
| 7 | 其余 P1/P2 | — | 打磨 |

> 前 4 项合计约 20 行 CSS，建议本轮验收前完成。P1-2/P1-3 若时间不够，至少在 README 标注「建议 ≥1280 宽使用」。

---

## 8. 附录：密度计算

| 视口 | 中栏宽 | 内容宽（−40 padding） | 看板列数 | 每列宽 | 标题可用宽 | 评价 |
| --- | --- | --- | --- | --- | --- | --- |
| 1920 | 1340 | 1300 | 6 | 210px | ~182px | 舒适 |
| 1600 | 1020 | 980 | 6 | 157px | ~129px | 良好 |
| **1440** | **860** | **820** | **6** | **130px** | **~100px** | **偏窄（≈9 字/行）** |
| 1400 | 820 | 780 | 6 | 123px | ~93px | 偏窄 |
| 1380 | 800 | 760 | 3 | 248px | ~220px | 舒适（断崖） |
| 1280 | 700 | 660 | 3 | 215px | ~187px | 舒适 |
| 1024 | 444 | 404 | 3 | 129px | ~101px | 偏窄（inspector 未收起） |
| 900 | 440 | 400 | 3 | 128px | ~100px | inspector 消失 |

> 结论：**1440 与 1024 是两个「窄」的尴尬点**；前者靠断点解决，后者靠 inspector 折叠解决。
