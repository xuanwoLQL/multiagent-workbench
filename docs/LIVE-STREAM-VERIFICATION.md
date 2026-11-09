# 实时流块时间线验收报告（task-10）

> 验收对象：后端 `StreamCoalescer`（`multiagent/api/stream.py`）+ 前端实时块时间线
> （`buildLiveBlocks` / `renderLiveFeed` / `normalizeChunkedText`）+ 任务工作副本 API
> （`GET /api/tasks/{tid}/workspace`）+ 真实桌面服务 8899 的只读现场验证。
>
> 验收时间：2026-11-07 01:09 – 01:40（Asia/Shanghai），**代码冻结后**。
> 结论：**PASS**。三个场景（离线回放 / 真实 WS / 真实桌面服务）全绿，console error 0；P0/P1 缺陷 0，P2 观察见 §8。

---

## 1. 结论摘要

| 验收项 | 结果 | 关键数字 |
| --- | --- | --- |
| 真实抓包离线回放（前端纯函数，Node） | ✅ | **1612 条 raw → 19 个块**（思考 5 / 消息 2 / 工具 6 / 用量 6），压缩 **84.8 倍** |
| 后端 `StreamCoalescer` 回放（按抓包时间戳） | ✅ | **1612 → 196 条事件**（8.2 倍）；文本 5655 字、`chunk_count` 合计 1472 一字不丢 |
| 浏览器离线回放（1832 / 1440） | ✅ | 13 个块节点 + 6 条事件行 = **19 行**；思考默认折叠 4/5；工具 6 块全带命令与状态；无乱码 |
| 真实 WebSocket 长输出（独立 8902 + 假 ACP） | ✅ | 380 条 chunk → **32 条合并事件**（11.9 倍）→ 页面 **3 个块**；贴底跟随 / 回到最新正常；任务 success |
| 8899 真实桌面服务（只读） | ✅ | `workspace` 200；待同步 0（`unsynced`）+ 工作副本改动 23（`pending`）；同步按钮按语义隐藏；黑板/交接正文整段 |
| console error / 未捕获异常 | ✅ 0 | 三个场景均为 0 |
| 全量 pytest | ✅ | **483 passed / 10 skipped / 0 failed**（94.2s） |
| 新增测试 `tests/test_live_stream.py` | ✅ | **16 passed** |

> 验收口径说明：Lead 要求的「1612 → 约 13–19 块」按**用户看到的块**计，实测 **19 行**
> （5 思考 + 2 消息 + 6 工具 + 6 用量行）。`buildLiveBlocks` 产出 19 个块，其中工具块渲染为
> `<details class="live-block">`、用量块渲染为 `.live-event` 行，所以 DOM 里 `.live-block` 节点是 13 个。

---

## 2. 验收环境

| 项 | 值 |
| --- | --- |
| 工作目录 | `~/multi_xuanwo` |
| 操作系统 / Python / pytest | Windows 11 / 3.11.0（`.venv`）/ 9.2.0 |
| 浏览器 | Microsoft Edge **151.0.4129.93**（`--headless=new` + CDP，`Emulation.setDeviceMetricsOverride` 精确视口） |
| 真实抓包 | `.e2e/live-capture.json`：1612 条，2026-11-06 16:39:33Z – 16:41:01Z，真实 run `run-136d45fb` / 任务 `task-d3c6ea86` / Agent `codex` |
| 真实桌面服务 | `127.0.0.1:8899`，home = `%APPDATA%\multiagent-desktop`，项目「我的世界」（`proj-a74b9df1`，root `~/myworld`） |
| 独立验收服务 | 8902 / 8906（`MULTIAGENT_HOME=.e2e/live-smoke-home-*`，假 Agent，不消耗模型额度） |

---

## 3. 真实抓包形状（为什么必须合并）

| kind | 条数 | 说明 |
| --- | --- | --- |
| `agent_thought_chunk` | 1395 | 平均 3.84 字/条（token 级） |
| `agent_message_chunk` | 77 | 平均 1.6 字/条 |
| `tool_call` | 6 | 工具调用开始 |
| `tool_call_update` | 128 | 工具进度 / 结果 |
| `usage_update` | 6 | 用量 |

直接按 raw 事件渲染就是「一行一个 token」：1612 行、每秒上百条 WS 消息。这正是本轮要修的问题。

---

## 4. 离线回放（真实抓包）

### 4.1 后端 `StreamCoalescer`（按真实时间戳回放）

回放方式：按抓包里每条事件的 `at` 推进假时钟，每 ≥120ms 触发一次 `flush_expired()`
（模拟后台线程节奏），最后 `flush()`。

| 指标 | 结果 |
| --- | --- |
| 输入 | 1612 条 raw |
| 输出 | **196 条**（`agent_thought_chunk` 52 / `agent_message_chunk` 4 / `tool_call` 6 / `tool_call_update` 128 / `usage_update` 6） |
| 压缩比 | **8.2×** |
| 文本守恒 | 合并后 chunk 文本拼接 == 原始拼接（一字不差） |
| chunk 守恒 | `chunk_count` 合计 **1472** == 原始 chunk 条数 |
| 非 chunk 事件 | 140 条原样直发（工具/用量不参与合并） |
| 乱码 | 0（`�` / `???` 均无） |

### 4.2 前端 `buildLiveBlocks`（Node 抽纯函数回放）

`tests/live_blocks_harness.mjs` 从 `multiagent/web/app.js` 按函数名抽取
`firstLine` / `applyToolEvent` / `buildLiveBlocks` / `normalizeChunkedText` 求值，
用同一份抓包回放（不依赖浏览器）：

| 指标 | 结果 |
| --- | --- |
| 输入 | 1612 条 raw |
| 输出 | **19 个块**：thought 5 / message 2 / tool 6 / usage 6 |
| 思考 chunk 归并 | 1395 条 → 5 块（平均 **279 条/块**，最长块 3531 字） |
| 消息 chunk 归并 | 77 条 → 2 块 |
| 工具事件归并 | 134 条 → 6 块（按 `tool_call_id`；6/6 带命令，状态 完成×4 / 失败×2） |
| 单 token 块 | **0**（无 ≤3 字且单 chunk 的块） |
| 压缩比 | **84.8×** |
| 合并后再回放 | 196 条合并事件 → 同样 **19 块**（前后端块结构一致） |
| 乱码 | 0 |

### 4.3 `normalizeChunkedText`：真实「一行一个 token」正文 → 整段

用真实项目「我的世界」的黑板/交接正文（`.e2e/live-board-sample.json`）跑前端归一化：

| 位置 | 归一化前行数 | 归一化后行数 | 结果 |
| --- | --- | --- | --- |
| 黑板正文（bb-9d043e77） | 464 | **19** | 段落还原 |
| 黑板正文（bb-99a2e5f3） | 60 | **19** | 段落还原 |
| 交接 `did`（ho-d97ef03f） | 451 | **1** | 整段 |
| 交接 `did`（ho-24da200c） | 47 | **1** | 整段 |
| 短文本 / 代码块 | 1 / 3 | 1 / 3 | 原样不动（启发式保护） |

---

## 5. 浏览器离线回放（1832×1010 / 1440×900）

做法：CDP 打开 `#live`，把 1612 条抓包分块注入 `window.__liveCapture`，再赋值给
`state.stream` 并调用真实的 `markLiveDirty()` + `renderLiveFeed(true)`（不是拼假 DOM）。

| 断言 | 结果 |
| --- | --- |
| 块节点 `.live-block` = 13（思考 5 + 消息 2 + 工具 6） | ✅ |
| 事件行 `.live-event` = 6（用量更新） | ✅ |
| 总行数 19（远小于 1612，无逐 chunk 行） | ✅ |
| 思考块默认折叠 4 个、展开 1 个（最新），toggle 按钮 5 个 | ✅ |
| 点「展开」→ 折叠数 4 → 3（交互生效） | ✅ |
| 工具块 6 个，命令文本完整（java / javac / jshell / Get-ChildItem / View Image / Editing files），状态 完成×4 失败×2 | ✅ |
| 展开工具块 → `<details open>` 内显示命令 code 块 | ✅ |
| 无空块（`.live-rich` 文本 <5 字 = 0） | ✅ |
| 页面无 `�` / `???` | ✅ |
| 抽屉：待同步 1 个文件 + 「同步 1 个文件到工作目录」按钮存在（**未点击**，任务 failed 才有未同步改动） | ✅ |
| console error / 未捕获异常 | ✅ 0 |

截图：

| 状态 | 1832×1010 | 1440×900 |
| --- | --- | --- |
| 实时流有内容（折叠态） | [live-replay-1832.png](../.e2e/live-replay-1832.png) | [live-replay-1440.png](../.e2e/live-replay-1440.png) |
| 长思考展开 | [live-thought-expanded-1832.png](../.e2e/live-thought-expanded-1832.png) | — |
| 工具块展开 | [live-tool-expanded-1832.png](../.e2e/live-tool-expanded-1832.png) | — |
| 任务抽屉（待同步 1 + 同步按钮） | [live-drawer-sync-1832.png](../.e2e/live-drawer-sync-1832.png) | — |

---

## 6. 真实 WebSocket 长输出（独立 8902 + 假 ACP）

`tests/fake_acp_stream.py`：一次 `session/prompt` 连续发 **260 条思考 chunk + 120 条消息 chunk**
（每条 15ms）+ `tool_call`（带命令/输入/状态）+ `tool_call_update`（completed + 输出）+ `usage_update`，
写产物 `实时流产物.md` 后结束。任务通过 `POST /api/tasks/{tid}/run` 真实调度，浏览器连真实 `/ws`。

| 指标 | 结果 |
| --- | --- |
| Agent 产生的 chunk | 380 条（260 思考 + 120 消息） |
| 独立 WS 客户端收到 | **32 条 chunk 事件**（thought 22 + message 10），`chunk_count` 合计 **380**；另有工具/用量 4 条，共 36 条 stream 事件 |
| 合并比 | **11.9×**（380 → 32） |
| 页面块数 | **3**（思考 1 块 4335 字 / 消息 1 块 / 工具 1 块） |
| 块增长 | 0 → 1 → 3（运行中逐步出现，不是最后一次性刷出） |
| 工具块 | 命令「运行 PowerShell：Get-ChildItem -Recurse」，状态「完成」 |
| 贴底跟随 | `pinned=true`，`scrollTop=585 / scrollHeight=1189` |
| 上滚 | `pinned=false`，`#live-jump`「回到最新」可见 |
| 点「回到最新」 | 回到底部（`pinned=true`），按钮隐藏 |
| 任务结果 | `success`（验收 file_exists 通过） |
| 无乱码 / console error | ✅ / **0** |

截图：[live-run-1832.png](../.e2e/live-run-1832.png) · [live-run-1440.png](../.e2e/live-run-1440.png)

---

## 7. 真实桌面服务 8899（只读现场）

> 只做 GET/页面渲染与注入回放，**全程没有点击「同步」**，不写 `~/myworld`。

### 7.1 服务与资产

- `/api/health` 正常，home = `~\AppData\Roaming\multiagent-desktop`；
- `/`、`/static/app.js`、`/static/style.css` 与仓库文件 **SHA-256 完全一致**（页面是最新前端资产）；
- 项目「我的世界」（`proj-a74b9df1`，root `~/myworld`），任务 `task-d3c6ea86`（failed）。

### 7.2 任务工作副本 API + 抽屉

| 断言 | 结果 |
| --- | --- |
| `GET /api/tasks/task-d3c6ea86/workspace` | **200** |
| 响应字段 | `isolation=copy`、`workdir_exists=true`、`can_sync=true`、`pending.count=23`、`unsynced.count=0`、`artifacts=75` |
| 抽屉「工作目录 / 工作副本」区块 | ✅ 工作目录 `~/myworld`、工作副本 runtime 路径、写入方式「隔离副本（验收后自动同步）」 |
| 待同步标签 | ✅ **待同步 0 个文件**（= `unsynced.count`） |
| 工作副本改动行 | ✅ **工作副本改动 23 个文件（含已同步，仅作参考）**（= `pending.count`） |
| 同步按钮 | ✅ **按语义隐藏**（`unsynced=0`，无需同步）；脚本分支断言「`unsynced>0` 才要求按钮存在」，本次为 0 分支 |
| 验收结论 / 产物 | ✅ 「验收失败：exit_zero」；产物 Probe.java / aerial.png / out/*.class 可见 |
| 注入 1612 条抓包后的实时流 | ✅ 19 行（13 块 + 6 事件行）；上滚出现「回到最新」、点击回底 |
| console error | ✅ 0 |

> 现场状态说明（与 Lead 确认）：用户在 01:2x 已把工作副本 23 个文件人工同步到 `~/myworld`，
> 所以 `unsynced=0`、抽屉显示「待同步 0」且**不出现同步按钮**——这是正确行为，不是缺陷。
> 证据截图：[live-real-drawer-1832.png](../.e2e/live-real-drawer-1832.png)。

### 7.3 黑板 / 交接正文（normalizeChunkedText 生效）

| 页面 | 行数 | 长正文 | 最大行数 | 结论 |
| --- | --- | --- | --- | --- |
| 共享黑板（4 条） | 4 | 2 条（原 464 行 / 60 行） | **19 行** | 整段，无「一行一个 token」 |
| 交接说明（2 条） | 2 | 2 条（原 451 行 / 47 行） | **2 行** | 整段 |

截图：[live-real-board-1440.png](../.e2e/live-real-board-1440.png) ·
[live-real-handoffs-1440.png](../.e2e/live-real-handoffs-1440.png) ·
[live-real-8899-1832.png](../.e2e/live-real-8899-1832.png) ·
[live-real-8899-1440.png](../.e2e/live-real-8899-1440.png) ·
[live-real-replay-1832.png](../.e2e/live-real-replay-1832.png)

---

## 8. 已知限制与观察（P2，不阻塞）

1. **8899 的实时流是内存态**：真实 run 是在旧后端进程（8770）里跑完的，当前 dev server（8899）
   的 `EventHub` 里没有历史 stream，所以 `#live` 初始为空。这不是缺陷（设计上 hub 只保留进程内
   最近 4000 条），但用户会疑惑；本轮用「注入真实抓包回放」在 8899 页面上验证了块渲染与滚动/回到最新。
   若要在 8899 上看到真实新流，需要在 8899 里重跑任务。
2. **工具卡的输入/输出未渲染（P2）**：ACP 适配器（`acp_adapter._tool_fields`）会发
   `tool_input` / `tool_output` / `tool_content`，但前端 `applyToolEvent` 只读 `e.input` / `e.output`
   （没有 snake/camel 映射），所以工具块只显示「命令（来自 `tool` 标题）+ 状态」。
   本轮验收要求「工具块有命令/状态」已满足；输入/输出展示建议后续补映射。
3. **归一化是启发式**：`normalizeChunkedText` 对 ≤20 行的文本不动（避免破坏正常排版），
   所以很短的逐 token 文本仍会保持多行；真实长正文（464 / 451 行）归一化正常。
4. **内容不足一屏时不会出现「回到最新」**：没有滚动条就没有 jump，这是正确行为；
   冒烟脚本已分支处理（`scrollable=false` 时跳过滚动断言并记录）。
5. **8899 未跑新任务**：为避免写用户真实目录，8899 上只做只读验证 + 页面内注入回放；
   真实 WS 长输出场景在独立 8902 假 Agent 服务上完成。

---

## 9. 缺陷记录

| 级别 | 缺陷 | 状态 |
| --- | --- | --- |
| P0 | 无 | — |
| P1 | 无 | — |
| P2 | 工具卡 `tool_input`/`tool_output`/`tool_content` 未在前端渲染（见 §8.2） | 记录，不阻塞（代码已冻结） |
| P2 | 8899 hub 无历史 stream，重启后 `#live` 为空（见 §8.1） | 设计如此，记录 |

---

## 10. 测试与复跑命令

```powershell
# 1) 全量回归（代码冻结后）
.\.venv\Scripts\python.exe -m pytest -o addopts= -q
#   → 483 passed, 10 skipped, 0 failed in 94.22s
#   10 skipped 全部是 MULTIAGENT_REAL_AGENT=1 才跑的真实 Agent 用例（消耗额度）

# 2) 实时流专项（真实抓包离线回放 + 前端纯函数 + WS 链路 + workspace 契约）
.\.venv\Scripts\python.exe -m pytest tests\test_live_stream.py -o addopts= -q
#   → 16 passed

# 3) 前端纯函数离线回放（Node，可直接看 1612 → 19 块）
node tests\live_blocks_harness.mjs blocks .e2e\live-capture.json
node tests\live_blocks_harness.mjs normalize .e2e\live-board-sample.json
node tests\live_blocks_harness.mjs cases

# 4) 浏览器三场景验收（离线回放 / 真实 WS / 8899 只读）
.\.venv\Scripts\python.exe tests\live_smoke_qa.py --mode all
#   → 结果：PASS（35.6s）→ .e2e/live-smoke-results.json
```

产物：`.e2e/live-smoke-results.json`（逐项断言 + 截图元数据）、`.e2e/live-*.png`（13 张截图：回放 5 + 真实 WS 2 + 8899 6）、
`.e2e/live-capture.json`（真实抓包）、`.e2e/live-board-sample.json`（真实黑板/交接样本）、
`.e2e/qa-pytest-live.txt`（全量回归输出）。

---

## 11. 本次新增文件

| 文件 | 说明 |
| --- | --- |
| `tests/test_live_stream.py` | 新增 16 条验收用例：真实抓包形状、后端合并守恒、前端 19 块回放、无逐 chunk 行、工具块命令/状态、normalize 真实样本、live DOM/CSS 契约、workspace API 字段、WS 链路合并 |
| `tests/live_blocks_harness.mjs` | 从 `app.js` 抽纯函数（`buildLiveBlocks` / `applyToolEvent` / `normalizeChunkedText` / `firstLine`）做离线回放 |
| `tests/fake_acp_stream.py` | 流式假 ACP：连续 chunk + 工具调用，用于真实 WS 长输出验收（不消耗模型额度） |
| `tests/live_smoke_qa.py` | 三场景浏览器验收脚本（CDP 精确视口 / 截图 / console 捕获 / 8899 只读） |
| `docs/LIVE-STREAM-VERIFICATION.md` | 本文件 |

未修改任何实现文件（`multiagent/**`、`desktop/**`、`scripts/**` 均未改动）。
