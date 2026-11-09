# 大型文件型项目编排

这份文档说明平台如何承接「一个 PDF/需求包 + 大量代码/文档/图表/PPT」这类长任务，
以及为什么不再把一个巨型目标直接塞给一次 900 秒的 Agent 调用。

## 1. 默认时间预算

优先级：任务显式 `timeout_sec` → 选中 Agent 的 `timeout_sec` → 任务类型默认 →
policy 全局默认。

| 层级 | 默认值 | 位置 |
| --- | --- | --- |
| code | 2400 秒 | `config/policy.json::kinds.code.timeout_sec` |
| doc | 2400 秒 | `config/policy.json::kinds.doc.timeout_sec` |
| ppt / web | 2400 秒 | `config/policy.json::kinds.*.timeout_sec` |
| diagrams / research / data / generic | 1800 秒 | `config/policy.json::kinds.*.timeout_sec` |
| review | 1800 秒 | `config/policy.json::kinds.review.timeout_sec` |
| Agent 默认 | 2400 秒，Codex 3600 秒 | `config/agents.json` |

重试时预算自动递增：

```json
{
  "retry": {
    "backoff_seconds": 0,
    "timeout_multiplier": 1.5,
    "max_timeout_sec": 3600
  }
}
```

例如 2400 秒的任务第 2 次尝试为 3600 秒，第 3 次仍封顶 3600 秒。任务详情会显示
基础预算、本次有效预算、已运行时间、剩余时间和下一次重试预算；进入告警阈值
（默认 300 秒）会标红提示。

运行中可通过 API/CLI 修改预算，但已经交给 Agent 适配器的硬超时不会被追溯修改，
新预算从下一次尝试生效：

```powershell
python -m multiagent task timeout --task task-xxxx --timeout 3600
curl -X POST http://127.0.0.1:8770/api/tasks/task-xxxx/timeout -H "Content-Type: application/json" -d "{\"timeout_sec\":3600}"
```

## 2. 验收硬化

`config/acceptance.json::kind_templates` 提供可复用验收模板：

- `code`：产物存在、文件有改动、文件非空、`verify_command` 退出码为 0；
- `doc` / `ppt` / `diagrams` / `web`：产物存在且非空（PPT 最低 1024 B）；
- `review`：`no_writes` + `handoff_present`。

执行收尾时还会计算**有效验收**（任务详情里的 `acceptance_effective`）：

- 历史任务只有 `exit_zero` 时，自动追加真实文件改动 + 交接说明；
- 声明了产物时，自动补 `file_exists` / `file_nonempty`；
- 代码任务若提供了 `verify_command`，自动补 `command_exit_zero`；
- Python 项目在 quick-task/API/CLI 创建时会自动推断
  `python -m compileall -q src` 之类的低风险验证命令。

手动创建任务如果只填 `exit_zero`，API 返回 400，必须显式传
`confirm_exit_zero_only=true`；UI 表单也会二次确认。

## 3. 九阶段 DAG

默认拆解顺序：

```text
requirements-freeze
  → sim-core
      → web-3d
      → diagrams
          → ppt (web-3d + diagrams)
              → docs
                  → review
                      → fix
                          → final-qa
```

每个阶段包含：任务类型、Agent、依赖、`isolation=copy`（只读阶段为 shared + read_only）、
`write_scope`、产物、验收、1800~3600 秒超时、`max_attempts`。需求冻结、仿真核心、
Web/3D、图表、PPT、文档、只读审阅、修复、最终 QA 全覆盖。

### 预览 → 调整 → 应用

API：

```powershell
curl -X POST http://127.0.0.1:8770/api/projects/proj-xxxx/decompose/preview `
  -H "Content-Type: application/json" `
  -d "{\"text\":\"完成 PDF 中的全部项目要求\"}"

# 前端会把 plan 放进可编辑 JSON；确认后再提交
curl -X POST http://127.0.0.1:8770/api/projects/proj-xxxx/decompose/apply `
  -H "Content-Type: application/json" -d "@plan.json"
```

`decompose/preview` 只生成计划，不写数据库、不启动任务。

CLI：

```powershell
# 预览并打印 plan JSON
python -m multiagent task decompose --project mytest `
  --brief "完成 PDF 中的无人机仿真项目全部要求"

# 确认后创建，不自动启动
python -m multiagent task decompose --project mytest --apply `
  --brief-file ~/study\mytest\pdf_text.txt
```

桌面控制台「指挥台」新增「拆解大型项目 →」：预览风险、需求来源、卫生问题与完整
JSON；用户可先改 JSON 再应用，不会直接启动巨型任务。

## 4. 隔离与写范围

- 修改型任务默认 `isolation=copy`；显式传 `shared` 会被创建入口改成 `copy`，
  除非传 `allow_shared_write=true`。
- 运行时还有第二道兜底：`shared` + 空 `write_scope` 的 code/doc/data/ppt/diagrams/web
  任务会强制改用独立副本。
- 有输出产物时会自动推导目录级 `write_scope`，例如 `docs/a.md → docs/**`。
- 空写范围会生成风险提示；UI 要求勾选「允许不受限写入」，API/CLI 也会把风险写进
  任务 `result.creation_warnings`。

## 5. 工作区卫生

默认排除（快照、diff、copy、commit、archive 共用）：

```text
.edge-tmp*  edge-profile  __pycache__  *.pyc  .cache  .tmp*  *.tmp  *.log
node_modules  .venv  venv  .multiagent  build  dist  .git
```

项目根目录的 `.multiagentignore` 可追加规则（每行一个 glob，`#` 注释）；也可在
`config/policy.json::workspace.excludes` 配置全局追加规则。

卫生检查不会把这些目录算成交付物：

```powershell
python -m multiagent project hygiene --project mytest
curl http://127.0.0.1:8770/api/projects/proj-xxxx/hygiene
```

任务收尾会把 `temporary_files/temporary_dirs` 写进 `result.hygiene`，发现临时目录时
同时写事件 `workspace.hygiene`，最终 QA 交接里也会列清理清单。

## 6. 失败现场与产物报告

失败/超时不会丢弃工作副本。任务详情会明确列出：

- `generated_outputs`：已经生成并存在的产物；
- `missing_outputs`：验收要求但缺失的产物；
- `result.hygiene`：遗留临时文件/目录；
- 原始 `run` 的 prompt/stdout/stderr/transcript、退出码与验收明细。

成功仍只由「退出码 + 产物 + 验收条件」判定；Agent 自述不能作为成功依据。

## 7. mytest 回归建议

1. 先快照 `~/study\mytest`；
2. 清理 `.edge-tmp*`、`build/edge-profile`、`__pycache__`；
3. 用 `decompose/preview` 生成九阶段 DAG；
4. 在计划 JSON 中为每个任务确认 Agent、`isolation=copy`、`write_scope`、
   `timeout_sec`、`max_attempts=3` 与验收；
5. 应用计划后在工作台逐批启动，不要一次启动全部阶段。
