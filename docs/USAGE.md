# 使用说明

> **v1 / 历史参考（2026-11-06 前后）**：本文记录旧版控制台与旧测试数字，已被 v2 取代。
> 当前实现与验证以 [README](../README.md)、[V2-ARCHITECTURE](V2-ARCHITECTURE.md)、[USAGE-V2](USAGE-V2.md)、[DEFECTS-V2](DEFECTS-V2.md) 为准。
> 文中旧数字（518/530/558、schema 5、1.0.0）仅作历史记录，不代表当前基线。

> **v2 提示（2026-11）**：本文的「0.x 指挥台」分区、按钮和旧版三栏布局对应 v1 控制台；
> 当前默认界面是 `webapp/` 的 React v2 控制台。v2 的逐视图操作、快捷键、审阅/同步、
> 人工确认与验证 API 见 [USAGE-V2.md](USAGE-V2.md)；前端/后端实现见
> [V2-ARCHITECTURE.md](V2-ARCHITECTURE.md)。下文 CLI、配置字段与 HTTP API 参考仍然有效。

所有命令都可以写成 `python -m multiagent ...`；若已 `pip install .`，也可直接用 `multiagent ...`。
下文示例假定在项目根目录，并已创建 `.venv`。

先设一个环境变量指定工作台主目录（配置与数据都放这里）；不设则用当前目录：

```powershell
$env:MULTIAGENT_HOME = "~/multi_xuanwo"   # 可选
```

---

## 0. 指挥台（桌面 / 浏览器）

打开 `python scripts/serve.py --open`（或桌面端）后，第一屏是「指挥台」：左栏切视图、中栏干活、右栏看调度与 Agent、底部状态条看实时计数。

### 0.1 界面分区

| 区域 | 位置 | 你能做什么 |
| --- | --- | --- |
| 顶栏 64px | 最上方 | 切换项目、新建/导入、项目设置、刷新、看当前是否在调度 |
| 左栏 220px | 左侧 | 指挥台 / 任务 / 依赖图 / 共享黑板 / 交接说明 / 人工确认 / 版本快照 / Agent 引擎 / 实时 / 观测审计 / 配置 |
| 中栏 | 中间（唯一滚动区） | 当前视图；首页 = 一句话派活 + KPI + 6 列看板 + 最近活动 |
| 右侧检查器 360px | 右侧 | 启动/停止/恢复调度、Agent 2×2 编队、待审批、运行中、实时事件 |
| 底部状态条 28px | 最下方 | WS 连接、Agent 启用数、任务完成度、待审批数、调度状态 |

### 0.2 一句话任务

首页输入卡支持三件事：

| 操作 | 行为 |
| --- | --- |
| 创建任务 | 只建任务，状态「待执行」 |
| 创建并启动（Ctrl / ⌘ + Enter） | 建任务后立刻后台调度 |
| 拆解为三步计划 | 建 `调研整理（research）→ 产出初稿（doc）→ 只读审阅（review）` 三个带依赖的任务 |
| 拆解大型项目 | 预览九阶段 DAG（需求冻结→仿真/Web/图表/PPT/文档→审阅→修复→最终 QA）；可改 JSON 后再应用，不会自动启动 |

字段：

> 注：该行为已在后续轮次变更，见 DEFECTS-V2。

| 字段 | 说明 |
| --- | --- |
| 任务描述 | 用户原话，原样进任务 `brief`；标题取首行截 60 字 |
| 产出文件 | 可选，逗号分隔；填了自动生成 `file_exists` + 执行期非空检查，并按目录推导 `write_scope` |
| 执行者 | 「自动选择」走路由规则；也可指定某个 Agent |
| 类型 | 「自动判断」按关键词推断（代码/报错→code、报告/文档→doc、PPT→ppt、图表→diagrams、网页/3D→web、调研→research、审阅→review、表格/数据→data），也可手动选 |
| 写范围 | 逗号分隔 glob；留空会提示不受限写入风险。修改型任务默认 `copy`，`shared` 只用于明确只读的分析/审阅 |
| 写入方式 | 修改型任务默认 `隔离副本`：Agent 在独立副本里改，验收通过后自动同步；未显式确认时不会使用 `shared` 直写 |

命令行等价物：

```powershell
python -m multiagent task add 产品方案 "撰写竞品分析" --brief "调研三家竞品的功能与定价" --outputs 竞品分析.md
```

HTTP 等价物：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8770/api/projects/<pid>/quick-task `
  -ContentType "application/json; charset=utf-8" `
  -Body (@{ text = "调研三个竞品并输出竞品分析.md"; outputs = @("竞品分析.md"); mode = "single"; start = $false } | ConvertTo-Json)
```

`mode="plan"` 返回 3 个任务；`mode="project"` 默认只返回九阶段 DAG 预览，
`preview=false` 才写入数据库。`start=true` 时复用 `/run` 的后台调度闸门，如果项目
已有调度在跑会返回 200 + `started=false`（不是 409）。

大型项目拆解：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8770/api/projects/<pid>/decompose/preview `
  -ContentType "application/json" -Body (@{ text = "完成 PDF 中的全部项目要求" } | ConvertTo-Json)
```

预览响应里的 `plan.tasks` 可直接编辑后提交到
`/api/projects/{pid}/decompose/apply`。详见
[`LARGE-PROJECTS.md`](LARGE-PROJECTS.md)。

### 0.3 实时事件流（块时间线）

「实时」视图不再把 token 级 chunk 一行一条地刷屏，而是合并成可折叠的块：

- **消息**：Agent 对用户说的话，合并连续 chunk 后按段落/代码块渲染；
- **思考**：折叠块，显示合并片数与耗时，最新一块默认展开；
- **工具**：按 `tool_call_id` 合并，显示命令/状态（进行中 / 完成 / 失败），展开看命令与输出；
- **事件 / 用量**：任务开始、派发、审批、token 用量等作为分隔行；
- 顶部 run 条显示当前任务、Agent、run 短 id、运行状态；贴底自动跟随，上滚后出现「回到最新 (N)」；
- 后端 `StreamCoalescer` 把 180ms 内的同 run chunk 合并，WebSocket 事件量约降 8 倍，环形缓冲 4000 条。

### 0.4 工作目录 / 工作副本 / 手动同步

> 注：该行为已在后续轮次变更，见 DEFECTS-V2。

任务有「写入方式」两种：

| 模式 | 行为 | 适用 |
| --- | --- | --- |
| `copy`（隔离副本） | Agent 在 `%APPDATA%\multiagent-desktop\.multiagent\runtime\<项目>\tasks\<任务>\work` 里改；验收通过后自动同步回工作目录；失败/取消保留现场 | 默认推荐，冲突可控、可回滚 |
| `shared`（直接写） | Agent 直接改项目根目录，实时可见；没有自动回滚 | 导入的已有目录、单人任务 |

任务详情抽屉的「工作目录 / 工作副本」区块会显示：

- 工作目录（项目根）与工作副本（runtime work）的绝对路径，可一键打开；
- 「待同步 N 个文件」= 工作副本与工作目录逐个比对的差异（`unsynced`）；`pending` 是工作副本相对运行前基线的改动总数，仅作参考；
- 任务仍在运行时也可以手动同步（UI 会二次确认：同步的是当前快照，完成后会再次覆盖）；
- 任务失败/取消时不会自动同步，可手动点「同步 N 个文件到工作目录」取回产物。

HTTP：

```powershell
# 查看工作副本与待同步文件
Invoke-RestMethod http://127.0.0.1:8770/api/tasks/<tid>/workspace

# 手动同步（dry_run 只预览）
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8770/api/tasks/<tid>/sync `
  -ContentType "application/json; charset=utf-8" -Body '{"dry_run": false}'
```

命令行等价物：

```powershell
python -m multiagent task sync <tid> --dry-run   # 预览
python -m multiagent task sync <tid>             # 落盘
```

如果任务因超时/崩溃失败，但**产物与验收条件都合格**，人工复核后可以走「人工验收」：

```powershell
python -m multiagent task approve <tid> --sync --note "人工复核：产物完整，超时发生在收尾阶段"
```

它会把工作副本同步回工作目录、把任务状态改为 `success`，并写入 `task.approved` 事件；
原 run 的 `exit_code` / 超时错误仍然保留在审计里，不伪造「Agent 成功」。
正在执行或从未执行的任务不允许人工验收。

### 0.5 项目设置

顶栏「设置」可改 `name`（唯一名）与 `label`（显示名），**不移动工作区文件**。用于修正历史乱码名称：

```powershell
Invoke-RestMethod -Method Patch -Uri http://127.0.0.1:8770/api/projects/<pid> `
  -ContentType "application/json; charset=utf-8" `
  -Body (@{ label = "竞品调研 2026" } | ConvertTo-Json)
```

如果旧库里有多条纯问号名称，可先预览再批量修复（不删数据、不改事件哈希链）：

```powershell
python scripts\repair_legacy_names.py            # dry-run，只打印
python scripts\repair_legacy_names.py --apply    # 写入可读占位名
```

### 0.6 状态与看板映射

| 看板列 | 任务状态 |
| --- | --- |
| 待执行 | `pending`、`interrupted` |
| 执行中 | `running` |
| 等待人工 | `waiting` |
| 成功 | `success` |
| 失败 · 阻塞 | `failed`、`blocked` |
| 已跳过 · 取消 | `skipped`、`cancelled` |

### 0.7 首屏聚合接口

`GET /api/projects/{pid}/dashboard` 一次返回 project / progress / tasks / agents / approvals / stats / activity / running / workspace / server_time，替代首屏 10 个并发请求；100 任务规模实测 p50 约 5–8ms。

---

## 1. 命令速查

### 初始化 / 自检

```powershell
python -m multiagent init                       # 生成 config/*.json 与数据目录
python -m multiagent init --force               # 覆盖已有配置
python -m multiagent config                     # 配置摘要（JSON）
python -m multiagent agent list                 # Agent 清单（含解析后的真实命令）
python -m multiagent agent check                # 四家 ACP 握手（不消耗额度）
python -m multiagent agent check opencode       # 只查一家
python -m multiagent agent quota                # 每 Agent 的额度用量
```

### 项目 / 工作区

```powershell
python -m multiagent project create 产品方案
python -m multiagent project create 产品方案 --root D:\work\方案
python -m multiagent project import D:\some\dir --name 旧项目   # 导入已有目录，不移动文件
python -m multiagent project list
python -m multiagent project show 产品方案                      # 项目信息 + 进度
python -m multiagent project snapshot 产品方案 --label 初稿
python -m multiagent project snapshots 产品方案
python -m multiagent project diff <snapshot_id> --current       # 与当前状态对比
python -m multiagent project restore <snapshot_id>              # 回退（自动先存一份快照）
```

### 任务与编排

```powershell
# 单任务
python -m multiagent task add 产品方案 "撰写竞品分析" `
    --template research --brief "调研三家竞品的功能与定价" `
    --outputs 竞品分析.md --after <前置任务ID> --priority 5

# 计划文件（一条命令下达一整张 DAG）
python -m multiagent task add-plan 产品方案 --file plan.json

python -m multiagent task list 产品方案 [--status pending]
python -m multiagent task show <task_id>          # 含依赖、状态历史、运行、产物、交接
python -m multiagent task plan 产品方案           # 分层依赖图（供可视化）
python -m multiagent task status 产品方案         # 进度总览
python -m multiagent task run <task_id> [--agent codex]
python -m multiagent task run-project 产品方案 [--parallel 4] [--only <id>]
python -m multiagent task fanout <task_id> --agents codex,claude,opencode
python -m multiagent task retry|skip|cancel|takeover <task_id>
python -m multiagent task provide <task_id> --did "人工完成" --artifact 产物.md
python -m multiagent task resume 产品方案         # 恢复中断任务并继续调度
python -m multiagent task changes <task_id>       # 文件改动日志
python -m multiagent task revert <task_id>        # 按任务回滚全部改动
python -m multiagent task sync <task_id> [--dry-run]   # 手动把工作副本同步回工作目录
python -m multiagent task approve <task_id> --sync --note "人工复核通过"  # 失败但产物合格时人工验收
```

### 共享黑板 / 记忆

```powershell
python -m multiagent board post 产品方案 --type decision --title "选定方案A" --body "理由…" --scope project
python -m multiagent board list 产品方案 [--type issue] [--scope project] [--status pending]
python -m multiagent board search 产品方案 "价格" [--task <id>] [--agent codex]
python -m multiagent board show <entry_id>
python -m multiagent board edit <entry_id> --body "修订" --actor human
python -m multiagent board review <entry_id> --approve        # 让待审记忆进入长期记忆
python -m multiagent board memory 产品方案                    # 项目长期记忆
python -m multiagent board events 产品方案 [--after 100]
```

### 交接 / 审批 / 观测

```powershell
python -m multiagent handoff list 产品方案
python -m multiagent handoff show <task_id>
python -m multiagent handoff edit <handoff_id> --did "修正后的结论" --next "补充图表"

python -m multiagent approval list --project 产品方案 [--status pending]
python -m multiagent approval resolve <approval_id> --approve --note "确认"

python -m multiagent stats --project 产品方案
python -m multiagent runs --project 产品方案 [--task <id>] [--agent codex]
python -m multiagent timeline --project 产品方案 --out 复盘.md [--format markdown|json]
python -m multiagent compare --task <task_id>       # 同题多 Agent 产出对比

python -m multiagent serve --port 8770 --open       # Web 看板 + API
```

> 全局开关 `--json` 可放在任意位置（用于脚本化）。

---

## 2. 配置文件

所有配置在 `<home>/config/`，改完重启即可生效，**不需要改代码**。

### agents.json —— Agent 清单

```jsonc
{
  "version": 1,
  "defaults": { "timeout_sec": 900, "quota": { "max_invocations": 200, "max_seconds": 36000, "max_cost": 60 } },
  "agents": [
    {
      "id": "codex",
      "label": "Codex",
      "protocol": "acp",                       // acp | cli | human
      "command": "codex-acp",                  // ACP：协议服务端命令
      "args": [],
      "cli": {                                 // protocol=cli 时的调用方式
        "command": "codex",
        "args": ["exec", "-"],
        "prompt_mode": "stdin"                 // stdin | arg | file
      },
      "cwd": ".",
      "model": null,
      "env": {},
      "permission_env": {                      // 按档位注入环境变量（可选）
        "guarded":    { "DSH_PERMISSION_MODE": "read-only" },
        "autonomous": { "DSH_PERMISSION_MODE": "workspace-write" }
      },
      "roles": ["lead", "implementer"],
      "strengths": ["code", "refactor"],
      "can_write": true,
      "enabled": true,
      "quota": { "max_invocations": 100 }
    }
  ]
}
```

新增任意 CLI Agent：加一条 `protocol: "cli"` 的条目即可。参数支持占位符
`{prompt}` `{prompt_file}` `{workspace}` `{model}`。

### templates.json —— 任务模板

```jsonc
{
  "templates": [
    {
      "id": "write-doc",
      "name": "撰写 / 改写文档",
      "kind": "doc",
      "role": "writer",
      "isolation": "copy",           // shared | copy | worktree（只读任务强制 copy）
      "read_only": false,
      "brief": "在工作区中完成以下文档任务：\n{{goal}}",
      "acceptance_ref": "report-written",
      "prefer": { "strengths": ["doc"], "agents": ["claude", "codex"] },
      "timeout_sec": 1200
    }
  ]
}
```

### routing.json —— 路由规则

```jsonc
{
  "strategy": "first_available",     // first_available | least_loaded | most_budget
  "rules": [
    { "when": { "kind": "code" },  "order": ["codex", "opencode", "dsh", "claude"] },
    { "when": { "kind": "doc" },   "order": ["claude", "codex", "dsh", "opencode"] },
    { "when": { "tag": "review" }, "order": ["claude", "codex"] },
    { "when": { "role": "reviewer" }, "order": ["claude", "codex"] }
  ],
  "fallbacks": ["opencode", "codex", "claude", "dsh", "human"]
}
```

### acceptance.json —— 验收规则

```jsonc
{
  "rules": {
    "file-exists":   [{ "type": "file_exists", "path": "{{target}}" }],
    "file-changed":  [{ "type": "file_modified" }],
    "report-written":[
      { "type": "file_exists", "path": "{{target}}" },
      { "type": "content_contains", "path": "{{target}}", "text": "{{contains}}" }
    ],
    "review-readonly": [{ "type": "no_writes" }, { "type": "handoff_present" }],
    "manual-confirm":  [{ "type": "manual_confirm" }]
  }
}
```

支持的条件：`file_exists` `file_absent` `file_nonempty` `glob_min_files` `file_modified`
`file_deleted` `content_contains` `content_not_contains` `no_writes` `writes_within_scope`
`handoff_present` `exit_zero` `command_exit_zero` `command_output_contains`
`manual_confirm` `no_temp_artifacts` `always`。

### policy.json —— 策略 / 安全

```jsonc
{
  "concurrency": { "global": 6, "per_project": 3 },
  "default_timeout_sec": 1800,
  "task_defaults": { "timeout_sec": 1800, "max_attempts": 3 },
  "retry": { "backoff_seconds": 0, "timeout_multiplier": 1.5, "max_timeout_sec": 3600 },
  "context_budget_chars": 12000,
  "injection": { "include_handoffs": true, "max_handoffs": 5, "max_entries_per_type": 8 },
  "isolation": {
    "default": "copy",
    "sync_back_on_accept": true,
    "block_conflict": true,
    "revert_out_of_scope": true
  },
  "dangerous_ops": {
    "require_confirmation": ["delete", "overwrite_existing_artifact", "modify_project_config"],
    "auto_approve": false,
    "protect_paths": [".multiagent", "config", ".git"]
  },
  "review_gate": { "memory_requires_approval": true, "default_entry_status": "approved" },
  "kinds": {
    "code": { "isolation": "copy", "acceptance_ref": "file-changed", "timeout_sec": 2400, "max_attempts": 3 },
    "doc":  { "isolation": "copy", "acceptance_ref": "file-exists",  "timeout_sec": 2400, "max_attempts": 3 },
    "review": { "isolation": "shared", "read_only": true, "acceptance_ref": "review-readonly", "timeout_sec": 1800, "max_attempts": 2 }
  }
}
```

`acceptance.json` 还提供 `kind_templates`（code/doc/ppt/diagrams/web/review），
执行期会把模板约束补齐到历史任务上；只有 `exit_zero` 的任务不会仅凭退出码成功。

---

## 3. 桌面控制台 / Web 看板

### 3.1 桌面控制台（Electron，推荐）

```powershell
cd desktop
npm install      # 首次，装 Electron
npm start
```

或双击 `desktop\start.cmd`。桌面壳自动拉起 Python 后端并开窗；关窗即结束后端。
后端日志在 `.multiagent/logs/desktop-backend.log`。

这是真正能“操控”的界面，直接驱动真实 Agent：

- **项目**：新建 / 导入目录 / 删除，多项目切换。
- **任务**：新建（工作类型自由填写 / 按 `policy.kinds` 建议、模板、Agent、角色、隔离、只读、写范围、验收规则、依赖、优先级、超时、标签、输入产物）、编辑、删除、批量计划（JSON）、加/删依赖、路由预览、上移/下移调整顺序。
- **执行**：启动调度（可设最大并发 / 自动重试）、单任务执行、同题多 Agent 扇出、重试 / 人工接管 / 人工回填 / 跳过 / 取消 / 回滚改动、恢复续跑。
- **协作**：共享黑板读写与审核、版本历史、按关键词/类型/时间检索、交接说明人工修正、人工确认队列批准/拒绝。
- **版本**：创建快照、与当前对比、两两对比、回退。
- **Agent**：连通性检测、启用/停用、查看命令与额度用量。
- **观测**：成本/耗时（按 Agent + 按任务）、调用记录（可回看该次调用的输入/输出）、项目产物、**实时事件流**（Agent 消息/思考/工具调用，WebSocket 推送）、时间线（导出 Markdown）、同题多产出对比。
- **差异**：任务文件改动可查看统一 diff（文本红绿高亮；二进制只记大小，不自动合并）。
- **配置**：在线编辑 `agents/templates/routing/acceptance/policy`，保存即生效。

### 3.2 浏览器看板

```powershell
python scripts\serve.py --port 8770 --open
```

同一套 UI，在浏览器里也能用（同样的建项目/建任务/审批/快照/配置能力）。
实时性由 WebSocket 提供（事件流 + 进度 + 调度状态）。

### 3.3 对外暴露 / 鉴权

默认绑 `127.0.0.1`，**不需要 token**，桌面端与本地浏览器直接可用。
只有当你要让别的机器访问时，才需要放开绑定——此时鉴权**自动强制**：

```powershell
# 绑到所有网卡：/api/** 全部要求 Bearer token
python -m multiagent serve --host 0.0.0.0 --port 8770

# token 来源（按优先级）：--token > MULTIAGENT_API_TOKEN > <home>/.multiagent/token（不存在则自动生成，0600）
python -m multiagent serve --host 0.0.0.0 --token <你的token>

# 本机也想强制校验（例如这台机器不止你一个人用）
python -m multiagent serve --strict-auth
```

```powershell
# 客户端带上 token
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8770/api/projects
# WebSocket / 下载走 query（浏览器无法自定义 WS 头）
ws://127.0.0.1:8770/ws?token=<token>
```

即使不开 token，loopback 下也**始终**有跨站防护：`Host` 白名单（挡 DNS rebinding）与
`Origin` 同源校验（挡恶意网页驱动本机 Agent）。`GET /api/health` 永远匿名可用，
因为桌面壳要在开窗之前轮询它。

`policy.json` 的 `security` 块**只在启动时读取**——运行期通过 API 改配置不会动态放松
或收紧安全策略（有意 fail-closed）。

### 3.4 验收与验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q                       # 单元 + 集成（假 Agent）
.\.venv\Scripts\python.exe scripts\verify_requirements.py     # 文档声称的验收是否真的通过
.\.venv\Scripts\python.exe scripts\e2e_real.py chain ...      # 真实四 Agent 端到端（耗额度）
.\.venv\Scripts\python.exe scripts\verify_upgrade.py          # 真实库副本的 schema 升级兼容性
```

三层验收各自的适用范围与已知边界见 [`VERIFICATION.md`](VERIFICATION.md)。

### 3.5 崩溃后怎么续跑

进程被强杀（或断电）后重启：

- **同机**：恢复逻辑用 `runtime_sessions.pid` 判定——PID 已不存在即认定该实例已死，
  **立即**把它的 running 任务标为 `interrupted`（可续跑），不必等心跳超时。
  本机若还有另一个活着的调度方（PID 仍在），它的任务不会被误杀。
- **跨机**：拿不到对端 PID，退回心跳判据（`policy.json` 的 `recover.stale_seconds`，默认 300s）。

```powershell
python -m multiagent task resume <项目>            # 保守恢复后继续调度
python -m multiagent task resume <项目> --force     # 无视存活判定，强制回收现场
```

`--force` 只在**你确认上次的调度进程已经死了**时才用（例如 PID 被复用）。
若本机确实还有活着的调度方，强制回收会把它正在跑的任务判成中断。

### 3.6 终止执行（真的会杀掉 Agent）

控制台上的「停止」、CLI 的 `task cancel`、API 的 `/stop` 与 `/cancel` 现在都会**真正终止**
正在跑的 Agent 进程（整棵进程树），而不是「等它跑完」。

| 操作 | 作用范围 | 语义 |
| --- | --- | --- |
| `POST /api/projects/{pid}/stop` / 控制台「■ 停止」 | **仅该项目** | 停止派发 + 终止该项目在跑的 Agent + 相关任务置 `cancelled` |
| `POST /api/tasks/{tid}/cancel` / `task cancel` | 单个任务 | 终止该任务在跑的 Agent，终态 `cancelled` |
| `Engine.shutdown()` | 全局 | **仅进程退出路径**（`Workbench.close()`）使用，HTTP 路由不得调用 |

被取消的任务：

- 终态是 `cancelled`（不是 `failed`），退出码 `-15`，`error` 写明「已取消：收到人工终止信号」；
- **不会**把半成品写回工作区（`sync_back=False`）；半成品留在隔离副本与
  `runs/<run_id>/` 下（需求 43「失败保留现场」）；
- 取消是协作式的：适配器每 200ms 轮询一次取消信号，因此从点击到进程消失通常在 1 秒内。
- **跨进程取消**：CLI / API / 桌面各自是独立进程，取消方无法 set 执行方的内存 Event，
  因此在 run 目录写 `cancel.requested` 哨兵；适配器同时轮询 Event 与哨兵。
  `Workbench.close()` 只终止**本进程自己**拉起的 run，短命命令（如 `handoff list`）
  退出时不会误杀另一个进程正在执行的任务。

> 为什么值得单列一节：早先的实现里「停止」只是不再派发新任务，在跑的 Agent 会跑到自然结束
> 或超时。用户看到「已停止」但 Agent 还在跑，等于需求 54 的「终止执行」是假的。
> 现在超时预算由任务/Agent/类型三层决定，code/doc 默认 ≥2400 秒，重试递增并封顶。

---

## 4. HTTP API

`python -m multiagent serve` 后可用。常用端点：

> 注：该行为已在后续轮次变更，见 DEFECTS-V2。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` `/api/config` `/api/agents` | 健康 / 配置 / Agent 额度 |
| POST | `/api/agents/{id}/check` | ACP 握手探测 |
| GET/POST | `/api/projects` | 项目列表 / 创建 |
| POST | `/api/projects/import` | 导入目录 |
| GET | `/api/projects/{id}/kanban` `/progress` | 看板 / 进度 |
| GET/POST | `/api/projects/{id}/tasks` | 任务列表 / 创建 |
| POST | `/api/projects/{id}/tasks/plan` | 计划文件批量建任务 |
| POST | `/api/projects/{id}/run` | 后台启动调度 |
| GET | `/api/tasks/{id}` `/changes` `/handoff` `/compare` | 任务详情 / 改动 / 交接 / 对比 |
| POST | `/api/tasks/{id}/run|retry|skip|cancel|takeover|provide|revert|approve` | 任务操作（`approve` 支持 `sync` / `note`） |
| PATCH/DELETE | `/api/tasks/{id}` | 编辑 / 删除任务 |
| POST/DELETE | `/api/tasks/{id}/deps` `/api/tasks/{id}/deps/{dep}` | 加 / 删前置依赖 |
| POST | `/api/projects/{id}/stop` | 停止调度（不再派发新任务） |
| DELETE | `/api/projects/{id}` | 删除项目及其协作数据 |
| GET | `/api/agents/detail` | Agent 配置 + 额度用量 |
| GET/PUT | `/api/config/files` `/api/config/files/{name}` | 读取 / 保存配置（保存即生效） |
| POST | `/api/config/reload` | 重新读取磁盘配置 |
| GET | `/api/fs/list?path=` | 列目录（导入工作区选择路径用） |
| DELETE | `/api/board/{id}` | 删除黑板条目 |
| GET | `/api/projects/{id}/runs` | 项目级 Agent 调用记录 |
| GET | `/api/projects/{id}/artifacts` | 项目级产物（`role=input/output`） |
| POST | `/api/tasks/{id}/move` | 上移 / 下移执行顺序（`direction=up/down`） |
| GET | `/api/tasks/{id}/route` | 路由预览（会选哪个 Agent、为什么） |
| POST | `/api/projects/{id}/recover` | 把遗留「执行中」标记为中断，可续跑 |
| GET | `/api/snapshots/{a}/diff/{b}` | 两个快照之间的差异 |
| GET | `/api/runs/{id}/output` | 回看某次调用的输入（prompt）/ stdout / stderr |
| GET | `/api/tasks/{id}/diff?path=` | 某次文件改动的统一 diff（文本红绿；二进制只回大小） |
| WS | `/ws?project={id}` | 实时推送：`stream.event`（Agent 消息/思考/工具调用/任务状态）、进度、调度状态 |
| GET/POST | `/api/projects/{id}/board` | 黑板读写 |
| GET | `/api/projects/{id}/board/search` | 黑板检索 |
| POST | `/api/board/{id}/review` | 审核记忆 |
| GET | `/api/projects/{id}/handoffs` `/approvals` `/stats` `/timeline` `/events` | 观测 |
| POST | `/api/approvals/{id}/resolve` | 处理确认项 |
| WS | `/ws?project={id}` | 实时事件/进度 |

---

## 5. 常见问题

**Q：某个 Agent 报未找到？**
`multiagent agent list` 看解析出来的真实命令；`agent check` 看握手结果。dsh 若装在非常规目录，
设 `MULTIAGENT_DSH` 指向它的 `resources\runtime\cli\bin\dsh.cmd`。

**Q：任务一直被跳过（blocked）？**
`task show <id>` 看 `blocked_reason`：常见是依赖未完成、额度超限、并发冲突、或等待人工确认。
`approval list --project <p>` 处理待确认项。

**Q：Agent 说自己成功了，为什么任务失败？**
系统只认退出码 + 产物 + 验收条件。若 Agent 没写出预期产物或退出码非 0，一律判失败并在
`runs` 里保留 stdout/stderr/transcript。

**Q：两个任务改同一个文件？**
先完成的会被记为 `committed`，后完成的识别为冲突 → 阻止自动合并 + 生成人工确认项，原文件不被覆盖。

**Q：想看某个任务到底让 Agent 做了什么？**
`task show` 里的 run 有 `prompt_path`；运行目录还含 `stdout.txt` / `stderr.txt` / `transcript.json`。
