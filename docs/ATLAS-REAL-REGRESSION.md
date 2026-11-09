# Atlas 真实回归报告（MultiAgent v2）

> 日期：2026-11-08  
> 范围：工作台 `~/multi_xuanwo`，真实项目 `~/myworld`（Atlas）  
> 授权：用户已授权本轮 Atlas 真实回归，使用真实 `codex`；未使用 `claude` 额度。  
> 结论：**修复后真实任务、npm test、脏树阻断全部 PASS**；修复前的一轮失败作为对照证据保留，未删除、未改写。

## 0. 结论速览

| 检查 | 结果 |
| --- | --- |
| 真实 codex 任务（修复后 round-2） | **PASS**：`task-de523674` / `run-3a87fc09`，status=`success`，exit_code=0 |
| 自动提交 + 自动合并 | **PASS**：commit `3576806…`，merge `242069f…`，`merged=true` |
| Atlas 根目录产物 | **PASS**：`docs/workbench-regression.md` 存在，验收口径 190 bytes（磁盘 196B，CRLF），sha256 `7a2627…` |
| Atlas 根目录干净 | **PASS**：`git status --porcelain` 为空 |
| worktree 回收 + 分支保留 | **PASS**：`worktree_removed=true`，work 目录不存在，分支 `ma/de523674-atlas` 仍在 |
| 任务详情 / 工作副本 API | **PASS**：`merge_status=merged`，`can_sync=false` |
| 事件流 | **PASS**：`task.git.committed` / `task.git.merged` / `task.git.worktree.removed` / `task.success` |
| 进程 / 端口审计 | **PASS**：审计已写入 `result.processes`；无归属该 workdir 的残留监听；临时端口 18800 已释放 |
| Atlas `npm test` | **PASS**：backend **48/48**，frontend **24/24**（5 个测试文件），rc=0 |
| require_clean_tree 脏树阻断 | **PASS**：`blocked=true` / `reason=dirty_worktree` / `dirty_files=["README.md"]`，无 `MERGE_HEAD`，用户改动未被覆盖 |
| 修复前失败对照（attempt-1） | **已保留并定位**：workbench `commit_worktree` 在 Atlas 的 `.gitignore` 场景下误报 `git add` 失败，任务 blocked |
| 第四轮：并发/冲突 + 依赖闸门（2026-11-08 夜） | **PASS（含已修复的假成功）**：A 任务1 真实 codex merge 200 → `5d588a7…`；任务2 merge 409 conflict（`docs/merge-conflict-probe.md`），master 干净、无 `MERGE_HEAD`、任务2 commit 不在 master；修复后同一任务复验 `status=blocked` / `blocked_reason=merge_conflict`；B 依赖闸门 21/21，A 未合并时 B `pending` 且不在 `ready_tasks`，手动合并 A 后 B ready 并成功 |

最终断言文件：`.e2e/atlas-real/assertions-final.json`（36/36 通过；其中 1 条证据目录复用失败被明确标注为 superseded，见 §7）。

---

## 1. 环境与隔离

- 真实工作台（端口 **8771**）**未停止、未重启、未写入**；本轮所有 API 调用只打临时后端。
- 两轮临时 home：
  - 修复前 attempt-1：`~/multi_xuanwo\.e2e\atlas-real\home`
  - 修复后 round-2：`~/multi_xuanwo\.e2e\atlas-real\home-r2`
- 临时后端：`python -m multiagent serve --port 18800`，环境变量 `MULTIAGENT_HOME=<对应临时 home>`，cwd=`~/multi_xuanwo`；两轮结束后均已停止，18800 无监听。
- `home-r2/config/policy.json`（显式设置，已保存为 `home-r2-policy.json`）：

```json
{
  "git": {"auto_merge": true, "require_clean_tree": true, "keep_worktree": false},
  "processes": {"audit_enabled": true, "reap_orphans": true, "min_timeout_sec": 5}
}
```

- 两轮任务均为低风险文档任务：`kind=doc`、`agent_id=codex`、`isolation=worktree`、
  `write_scope=["docs/**"]`、`outputs=["docs/workbench-regression.md"]`，
  验收为 `file_exists` + `file_nonempty(min_bytes=20)`。
- 未修改 Atlas 业务源码、未修改工作台业务源码、未 `npm install`、未降低验收标准。

复现命令（修复后第二轮）：

```bash
.venv/Scripts/python.exe .e2e/atlas-real/run_atlas_regression.py all \
  --home-dir "~/multi_xuanwo\.e2e\atlas-real\home-r2"
```

---

## 2. 修复前失败（attempt-1，保留对照）

**真实 Agent 本身成功，工作台提交失败。**

| 项 | 值 |
| --- | --- |
| project | `proj-8ee6c896`（temp home `.e2e/atlas-real/home`） |
| task | `task-e7c79ac0` |
| run | `run-58ca6785` |
| agent / exit | `codex` / 0 |
| task status | **`blocked`** |
| 验收 | `acceptance.ok=true`（workdir 内 `docs/workbench-regression.md` 382 bytes） |
| Git commit | **`null`**（workbench 未提交成功） |
| Git merge | `{}`（未合并） |
| branch | `ma/e7c79ac0-atlas` |
| worktree | 未回收（因为 merge 从未发生） |

关键事件与错误：

```text
task.git.commit.failed
result.git.error =
  warning: in the working copy of 'docs/workbench-regression.md',
  LF will be replaced by CRLF the next time Git touches it
```

根因（已由主线定位并修复）：

- `commit_worktree` 原先把 `:(exclude).multiagent` 等 exclude pathspec 直接放进
  `git add -A -- . …`；
- Atlas 的 `.gitignore` 已忽略 `.multiagent/`，Git 把 exclude 里的名字当成“显式添加被忽略路径”，
  以 **exit 1** 返回 `The following paths are ignored by one of your .gitignore files: .multiagent`；
- 原实现只取 stderr 最后一行作为错误文本，恰好拿到 CRLF warning，真正的 add 失败被掩盖；
- Runner 因此记录 `task.git.commit.failed`，任务转为 `blocked`，没有 commit / merge。

证据目录：`.e2e/atlas-real/attempt-1-failed/`（`run-response.json`、`task-detail.json`、
`backend.log`、`events.json`、`run-artifacts/stdout.txt|stderr.txt|transcript.json` 等）。

`run-artifacts/stdout.txt` 显示 codex 实际只写了目标文件并给出 handoff，未执行 `npm install`、
未改其它文件；`transcript.json` 中 codex 的 shell 命令只有 `Get-ChildItem` / `git status` /
`Get-Content` 等只读命令，**没有 git add / commit**。

### 2.1 关于 attempt-1 worktree 里的 `test` 提交

attempt-1 结束后，其残留 worktree 上出现了一条诊断用手工提交：

```text
bfc2cf4753128a64a3903bfa792ed4fd98433a6f
Author: MultiAgent <multiagent@local>
Date:   2026-11-08 22:20:35 +0800
Message: test
```

该提交由主线诊断手工创建（不是 workbench run 的产物），**未合并进 master**。
它只存在于 `ma/e7c79ac0-atlas` 分支和 attempt-1 残留 worktree 中；round-2 使用全新
home/task/branch，不引用该提交。reflog 与代码哈希已保存到
`.e2e/atlas-real/attempt-1-failed/code-state.json`。

---

## 3. 修复（主线，非本回归脚本修改）

- 修改文件：`multiagent/gitops/service.py`（修复后 sha256
  `9c5da07c8243bba965a2f3cce9dfdf5ebf8a935c357f89e0ba5cf5801ae16ef8`，mtime 2026-11-08 22:22:25）。
- 修复方式：`git add -A -- .`（尊重项目 `.gitignore`），随后对索引里任意层级的
  `.multiagent` 执行 `git reset`，不再把 exclude pathspec 交给 `git add`。
- 回归测试：`tests/test_git_hardening.py::test_commit_succeeds_when_project_gitignore_ignores_control_plane`。
- 本轮 round-2 使用的代码哈希（运行前后一致，未发现二次变更）：

| 文件 | sha256 |
| --- | --- |
| `multiagent/gitops/service.py` | `9c5da07c8243bba965a2f3cce9dfdf5ebf8a935c357f89e0ba5cf5801ae16ef8` |
| `multiagent/orchestrator/runner.py` | `6330e12d464d9e9e57819d72040cfbbe260a29954d697c69273f0aa011218976` |
| `multiagent/api/routes_git.py` | `856e9208cfe198ba78e8dad861895a632802af4863931c1655582065c887b34c` |

以上是第 1–3 轮的历史代码指纹，保留作为当时结论的绑定。第四轮开始前 `runner.py`、
`routes_git.py`、`gitops/service.py` 均已继续变更；**旧哈希不再代表当前代码**，当前值见 §11.4。

---

## 4. 修复后成功（round-2，全新 home）

| 项 | 值 |
| --- | --- |
| temp home | `~/multi_xuanwo\.e2e\atlas-real\home-r2` |
| project | `proj-a5f6dd4a`（`atlas-real-20261108-222339`） |
| task | `task-de523674`（“Atlas 回归：工作台真实任务证据”） |
| run | `run-3a87fc09` |
| agent / exit / duration | `codex` / 0 / 68.5s |
| task status | **`success`** |
| acceptance | 2/2 通过（`file_exists` + `file_nonempty`，190 bytes ≥ 20） |
| isolation | `worktree`（无降级：`degraded=false`） |
| branch | `ma/de523674-atlas` |
| base | `18d4a8dfbcbe620ad218af8966b545b921ac4444` |
| commit | `35768060a9ffd09c4d4a7053541696bae5eec80b` |
| merge SHA | `242069f90cb2ff1dc3e260bad77badbf23dcfc48` |
| merge | `merged=true`，`conflict=false`，`blocked=false`，files=`["docs/workbench-regression.md"]` |
| worktree | `worktree_removed=true`；work 目录不存在；分支保留 |
| cost | tokens=23961，source=`reported`（协议真实上报） |

Atlas 根目录最终状态：

```text
$ git -C ~/myworld log -1 --format="%H%n%s%n%P"
242069f90cb2ff1dc3e260bad77badbf23dcfc48
merge task(de523674): Atlas 回归：工作台真实任务证据
18d4a8dfbcbe620ad218af8966b545b921ac4444 35768060a9ffd09c4d4a7053541696bae5eec80b

$ git -C ~/myworld status --porcelain
(空)

$ git -C ~/myworld show-ref --verify refs/heads/ma/de523674-atlas
35768060a9ffd09c4d4a7053541696bae5eec80b refs/heads/ma/de523674-atlas
```

- HEAD 是真正的 merge commit（两个父提交：base `18d4a8d` + task commit `3576806`）。
- 分支 `ma/de523674-atlas` 保留，指向任务 commit `3576806`。
- 任务 worktree 已从 `git worktree list` 消失，磁盘目录也不存在。
- Atlas `.multiagent/` 未被删除。

任务详情 / 工作副本 / worktree API：

```text
GET /api/tasks/task-de523674
  status = success
  result.git.merge.merged = true
  result.git.worktree_removed = true
  result.processes = {...}（异步审计已回填）

GET /api/tasks/task-de523674/workspace
  merge_status = "merged"
  can_sync = false
  workdir_exists = false

GET /api/tasks/task-de523674/worktree
  exists = false
  merge_status = "merged"
```

事件流（`GET /api/projects/proj-a5f6dd4a/events`，task 相关）包含：

```text
task.running
task.git.committed
task.git.merged
task.git.worktree.removed
task.success
task.processes
```

产物内容（Atlas 根目录 `docs/workbench-regression.md`）包含日期 `2026-11-08`、
“MultiAgent v2 真实回归”，以及“worktree 提交、自动合并以及任务详情状态链路”说明；
sha256 `7a26273e64e376774883ceccfdd373f6f0347ee5c8b495bcc794cd8d61a4a309`。

---

## 5. Atlas `npm test`

命令（通过 harness 在 `~/myworld` 执行，未 `npm install`）：

```bash
cmd.exe /d /s /c "npm test"
```

结果：**rc=0，6.15s**。

- backend（`node --experimental-strip-types --test test/*.test.ts`）：
  `tests 48 / suites 8 / pass 48 / fail 0 / cancelled 0 / skipped 0 / todo 0`
- frontend（`vitest run --passWithNoTests`）：
  `Test Files 5 passed (5)`，`Tests 24 passed (24)`

完整日志：`.e2e/atlas-real/npm-test.log`；结构化摘要：`npm-test.json`。

> 注：`~/myworld\docs\TESTING.md` 写的 web 21 个用例已过时；以实际日志的 **24** 为准。

---

## 6. 进程 / 端口审计与 worktree 清理

`home-r2` 明确开启 `processes.audit_enabled=true`、`reap_orphans=true`、
`min_timeout_sec=5`；任务超时远大于 5s，因此审计实际执行。

- `result.processes.supported=true`
- `before_count=3`，`after_count=4`
- 新增监听 3 个，全部 `owned=false`（无法证明归属任务 workdir），因此只告警、未终止：
  - Weixin `pid=18896` port 14016
  - 真实工作台 `python -m multiagent serve --port 8771` `pid=20216` port 8771
  - 临时回归后端 `python -m multiagent serve --port 18800` `pid=26936` port 18800
- `reaped=[]`
- 独立 CIM + `netstat -ano -p tcp` 扫描：没有发现命令行包含该任务 workdir 或
  `task-de523674` 的进程（唯一匹配是取证命令自身的 bash，已排除）。
- 临时后端停止后：`port_still_listening=False`，18800 无监听。
- worktree：`result.git.worktree_removed=true`，`worktree_path` 目录不存在，
  `git worktree list` 无该任务注册；任务分支保留。

证据：`processes.json`、`processes-owners.json`、`processes-cim.json`、`netstat.txt`、
`backend-stop.txt`、`worktree-list.txt`、`branch-check.txt`。

---

## 7. `require_clean_tree` 脏树阻断（全新 `dirty-clone-r2`）

命令：

```bash
.venv/Scripts/python.exe .e2e/atlas-real/run_atlas_regression.py dirty \
  --home-dir "~/multi_xuanwo\.e2e\atlas-real\home-r2" --dirty-tag=-r2
```

步骤与结果：

1. `git clone --local ~/myworld .e2e\atlas-real\dirty-clone-r2`
   （基于当时 Atlas HEAD `242069f9…`）。
2. 在 clone 中创建分支/worktree `ma/dirty-test-r2`，新增 `dirty-test.txt` 并提交 1 次。
3. 在 clone 基线修改已跟踪文件 `README.md`，**不提交**。
4. 以工作台同一实现调用：

```python
from multiagent.gitops.service import merge_worktree_branch
merge_worktree_branch(clone, "ma/dirty-test-r2", "merge dirty test", require_clean=True)
```

返回与断言（6/6 PASS）：

```json
{
  "merged": false,
  "blocked": true,
  "conflict": false,
  "reason": "dirty_worktree",
  "dirty_files": ["README.md"],
  "sha": null
}
```

- `MERGE_HEAD` 不存在（`git rev-parse -q --verify MERGE_HEAD` rc=1）
- 用户对 `README.md` 的修改未被覆盖（marker 仍在，9671 bytes）
- 基线 HEAD 未变化：`242069f9… -> 242069f9…`

证据：`dirty-clone-r2-result.json`、`.e2e/atlas-real/dirty-clone-r2/`。
旧目录/旧结果保留：`dirty-clone-result.json`、`dirty-clone-result-22-25-37.json`。

> `assertions-final.json` 中的 `superseded_items` 明确记录了：第二次 dirty 阶段最初复用了
> `dirty-clone` 目录，因残留 `.git` 导致 `git clone` 目标已存在而失败；这是证据目录复用问题，
> 不是产品缺陷。改用全新 `dirty-clone-r2` 后 6/6 通过。

---

## 8. 证据索引（`.e2e/atlas-real/`）

| 文件/目录 | 内容 |
| --- | --- |
| `home/` | attempt-1 临时 home（保留） |
| `home-r2/` | round-2 临时 home（保留） |
| `home-r2-policy.json` | round-2 生效策略 |
| `attempt-1-failed/` | 修复前失败证据（task-detail / run-response / events / backend.log / run-artifacts / code-state） |
| `task-created.json`、`run-response.json`、`task-detail.json` | round-2 建任务与运行结果 |
| `workspace.json`、`worktree.json`、`project-git.json`、`dashboard.json` | round-2 API 状态 |
| `events.json` | 项目事件流 |
| `git-log.txt`、`git-status.txt`、`branch-check.txt`、`worktree-list.txt` | Atlas Git 校验 |
| `atlas-docs-workbench-regression.md`、`atlas-final-git.txt` | 产物副本与最终 Git 快照 |
| `npm-test.log`、`npm-test.json` | npm test 全量日志与摘要 |
| `processes*.json`、`netstat.txt`、`backend-stop.txt` | 进程/端口审计 |
| `dirty-clone-r2-result.json`、`dirty-clone-result*.json` | 脏树阻断结果 |
| `assertions-r2-main.json`、`assertions-dirty-r2.json`、`assertions-final.json` | 原始/合并断言 |
| `run-metadata.json` | 全部 ID、SHA、代码哈希、证据映射 |
| `run_atlas_regression.py` | 本轮可复现脚本（仅写本证据目录） |

---

## 9. 清理说明与残留

- 两轮临时后端均已停止（`backend-stop.txt`：`port_still_listening=False`）。
- 临时 home、日志、截图外的所有证据目录**保留**，未删除。
- 真实工作台 8771 未被停止或重启；本轮未向它的 DB/policy 写入。
- Atlas 根目录 `.multiagent/` 保留，未删除；`git status` 干净。
- 说明：工作台 `POST /api/projects/import` 的正常行为会写
  `~/myworld\.multiagent\project.json`（运行时控制面，已被 Atlas `.gitignore` 忽略）。
  两轮临时 home 的 import 覆盖了该文件为临时 project id（最终为 `proj-a5f6dd4a`）；
  真实工作台 8771 的项目数据在其自身 SQLite（home=`~/multi_xuanwo`）中，不依赖该文件。
- **attempt-1 的残留 worktree 仍在**（任务 blocked、未合并，`keep_worktree=false` 只在
  合并成功后回收），其分支 `ma/e7c79ac0-atlas` 含主线诊断提交 `bfc2cf4`（message `test`），
  未合并进 master。它是修复前失败证据的一部分，不属于 round-2 成功链路。
- round-2 的 worktree 已回收，分支 `ma/de523674-atlas` 保留。

## 10. 边界

- 本轮只验证一个真实文档任务的提交→合并闭环；未覆盖并发任务、冲突合并、超大仓库等场景。
- 进程审计只终止“可证明归属 workdir”的新增监听；本轮新增监听均不可证明归属，因此只告警。
  临时后端自身也出现在新增监听列表中（`owned=false`），停止后端口已释放。
- `codex` 使用本机既有 ACP/模型配置；stderr 中的 `Codex app-server exited` 为会话收尾噪声，
  Agent 退出码 0、stdout/验收均正常。
- 结果绑定到 §3 中的代码哈希；若后续代码继续变更，需要重新回归。

---

## 11. 第四轮：并发/冲突与依赖闸门真实回归（2026-11-08 夜）

> 证据目录：`.e2e/atlas-concurrency/a/`（场景 A）、`.e2e/atlas-concurrency/b/`（场景 B）。
> 本节数字均可在对应 `report.md`、`report.json`、`status-fix-recheck.json` 中复核；
> 第 1–3 轮的历史证据未删除、未改写。

### 11.1 场景 A：同一 base 的两个任务，先合 1 再合 2

**环境与任务**

| 项 | 值 |
| --- | --- |
| 仓库 | `~/myworld`（Atlas 真实仓库） |
| 任务1 | `task-5b440b9d` / `run-abea7acb` |
| 任务1 分支 / commit / base | `ma/5b440b9d-atlas-a-1-codex` / `af07214f05690fb9f64511ea5940b4c065a6cdcf` / `242069f90cb2ff1dc3e260bad77badbf23dcfc48` |
| 任务1 Agent | **真实 codex**，status=`success` |
| 任务2 | `task-c718c57e` / `run-0ec5dc4d` |
| 任务2 分支 / commit / base | `ma/c718c57e-atlas-a-2-fake-cli` / `e42a1bc0ea9e34af688b3563c5c80dd5d2b5ff28` / `242069f90cb2ff1dc3e260bad77badbf23dcfc48` |
| 任务2 Agent | **fake CLI**（`python -m multiagent.testing.fake_agent`），不是真实模型 |
| 共同 base | `242069f90cb2ff1dc3e260bad77badbf23dcfc48` |

两个任务都在执行前创建，因此分支 base 相同；任务1 先合并，任务2 后合并时与任务1
在 `docs/merge-conflict-probe.md` 上形成 add/add 冲突。
场景 A 关闭了自动合并：任务1/任务2 运行成功后 merge 均为
`skipped=true`、`reason=auto_merge_disabled`；随后手动合并任务1，再手动合并任务2。

**合并结果**

| 检查 | 结果 |
| --- | --- |
| 任务1 merge | **HTTP 200**，`merged=true`，`conflict=false`，files=`["docs/merge-conflict-probe.md"]` |
| master 前进 | `242069f90cb2ff1dc3e260bad77badbf23dcfc48` → `5d588a722078de68d2d87b41f32dbf9128d15142` |
| 任务1 merge commit | `5d588a722078de68d2d87b41f32dbf9128d15142`，双父：base `242069f…` + task `af07214…` |
| 任务2 merge | **HTTP 409**，`conflict=true`，`merged=false`，files=`["docs/merge-conflict-probe.md"]`，reason=`conflict` |
| master 冲突残留 | `git status --porcelain` 为空；无 `MERGE_HEAD`；无冲突标记 |
| 任务2 commit 是否进 master | **否**（`git merge-base --is-ancestor e42a1bc0… HEAD` rc=1） |
| Atlas `npm test` | **rc=0**，backend **48/48**，frontend **24/24**（5 个测试文件），6.39s |

初始断言为 **32/33 PASS**；唯一 FAIL 是 `task2 status is not success`。这是一次真实的
“假成功”发现：任务2 的 `/merge` 已返回 409、`conflict=true`、`merged=false`，但任务
状态仍为 `success`。其余关键项全部符合预期：任务1 merge 200、master 只多出任务1的
提交/合并、merge 已 abort、工作树干净、无冲突标记、npm test 48+24。

### 11.2 修复后复验：任务2 不再假成功

对**同一个** `task-c718c57e` 再次调用 `POST /api/tasks/task-c718c57e/merge`，仍返回
HTTP 409；随后任务详情为：

| 字段 | 值 |
| --- | --- |
| `status` | **`blocked`** |
| `blocked_reason` | **`merge_conflict`** |
| `result.git.merge.merged` | `false` |
| `result.git.merge.conflict` | `true` |
| `result.git.merge.files` | `["docs/merge-conflict-probe.md"]` |
| `result.git.merge.base` | `5d588a722078de68d2d87b41f32dbf9128d15142` |
| `result.git.merge.source` | `manual` |

证据：`.e2e/atlas-concurrency/a/status-fix-recheck.json`（`before.status` 为 `null`，
复验 merge 为 409，`after.status=blocked`、`after.blocked_reason=merge_conflict`）。
修复后“409 但 status=success”的假成功不再出现。

### 11.3 场景 B：依赖闸门，A 未合并时 B 不派发

**环境与任务**

| 项 | 值 |
| --- | --- |
| 仓库 | `.e2e/atlas-concurrency/b/clone`（Atlas 本地 clone） |
| 任务 A | `task-43e6d1dd`，分支 `ma/43e6d1dd-a-write-dep-probe-a` |
| 任务 B | `task-e04d2694`，分支 `ma/e04d2694-b-write-dep-probe-b`，依赖 A |
| Agent | **fake CLI**（不是真实模型） |
| 策略 | `git.auto_merge=false`、`git.require_merged_for_dependents=true`、`git.require_clean_tree=true` |

**流程与断言**

1. A 运行成功，但 merge 被跳过，reason=`auto_merge_disabled`，`merged=false`。
2. A 未合并时，B 保持 `pending`，不在 `wb.tasks.ready_tasks()` 中，`pending_deps=[A]`。
3. 手动 `POST /api/tasks/task-43e6d1dd/merge` 返回 **200**，A merge SHA
   `4166c03d42a94530acd8ac2861c40880119613e4`。
4. A 合并后 B 变为 ready，运行成功；B 自身 merge 仍因 `auto_merge=false` 被跳过。
5. clone 结束状态干净；Atlas 源仓库在 B 报告记录中 HEAD 前后均为
   `242069f90cb2ff1dc3e260bad77badbf23dcfc48`，未被改写。

结果：**21/21 checks PASS**，证据见 `.e2e/atlas-concurrency/b/report.md` 与
`report.json`。

### 11.4 当前代码哈希（2026-11-09 merge_* 闸门后）

第 1–3 轮的哈希保留在 §3，但已经过期；§11.4 的旧值也随本轮硬化被替代。以下是
2026-11-09 10:47 后实际计算值：

| 文件 | 字节数 | SHA256 | mtime |
| --- | --- | --- | --- |
| `multiagent/orchestrator/engine.py` | 40,519 | `CA839BEAA05094291032F29B26AB856810A9290BFC27AA34B37C746CF8ECD479` | 2026-11-09 10:35:41 |
| `multiagent/api/app.py` | 90,615 | `8450E4B8DCE2D86F04C9719419CAD74A616FB68DC9FBA41BF01D8C00E2D37640` | 2026-11-09 10:37:04 |
| `multiagent/api/routes_git.py` | 39,287 | `453953FB748FE439AA4133E81415713A022B4F77F1362075A174662C12B2BB8E` | 2026-11-09 00:13:30 |
| `multiagent/gitops/service.py` | 21,348 | `4F8365CD96BF9DA1DD0AD39701467544AF6C0FCDEAFAC3D545CE7C4E54F84595` | 2026-11-09 00:08:35 |
| `multiagent/tasks/service.py` | 33,163 | `27B0317F87FDEF3FF7BD6E8F66FD892405BA7418DACE9DB8D8E7DA85587C9950` | 2026-11-08 23:48:14 |
| `multiagent/workspace/isolation.py` | 18,660 | `3ACD0D5709B039FBB7374D1B8E1411ABC60FB04AFE6DFDE07C09BED2F0B97B01` | 2026-11-09 00:08:39 |

`multiagent/orchestrator/runner.py` 本轮未变，仍为
`65F04CAE1A2B7D419AB0E858B5F69580C1B372B3F60C0EF9CAB4CE17BE960A5A`
（80,396 B，2026-11-08 22:57:16）。

2026-11-09 的 `merge_*` 重试闸门改动集中在 `engine.py` / `api/app.py`：对
`merge_*` blocked 任务提供普通 retry 闸门（返回 409，UI 隐藏重试入口）；其它合并/
重建逻辑未变。场景 A/B 的真实 Git 证据仍有效；同一 `task-c718c57e` 在当前硬化后
代码上再次复验：仍为 409 conflict、`status=blocked` / `blocked_reason=merge_conflict`，
master 保持 `5d588a722078de68d2d87b41f32dbf9128d15142` 且工作树干净（证据
`.e2e/atlas-concurrency/a/status-fix-recheck-hardened.json`）。若上述文件或相邻
合并/依赖逻辑继续改动，**需重新跑并发/冲突与依赖闸门回归并重新打包**，不能沿用
本节数字。

### 11.5 证据索引

| 文件 | 内容 |
| --- | --- |
| `.e2e/atlas-concurrency/a/report.md` | 场景 A 人类可读报告、逐条断言、master/npm test 结果 |
| `.e2e/atlas-concurrency/a/report.json` | 场景 A 结构化结果（任务、提交、master、断言） |
| `.e2e/atlas-concurrency/a/status-fix-recheck.json` | 修复后同一任务复验（409 → blocked / merge_conflict） |
| `.e2e/atlas-concurrency/a/backend.log`、`task-*.json`、`merge-*.json` | 场景 A API / 进程 / 事件证据 |
| `.e2e/atlas-concurrency/b/report.md` | 场景 B 人类可读报告（21/21） |
| `.e2e/atlas-concurrency/b/report.json` | 场景 B 结构化结果（task id、ready、merge、clone 状态） |
| `.e2e/atlas-concurrency/b/task-*.json`、`ready-*.json`、`merge-result.json` | 场景 B 任务与依赖闸门证据 |
| `.e2e/atlas-concurrency/b/git-log.txt`、`git-status.txt` | clone 的 Git 历史与干净状态 |

### 11.6 边界

- 场景 A 的**任务1是真实 codex**；任务2是 fake CLI。场景 B 的 A/B 均为 fake CLI。
- 场景 A 在 **Atlas 真实仓库** 上执行，master 当前 HEAD 为 `5d588a7…`，
  `docs/merge-conflict-probe.md` 已由任务1合并；任务2分支仍保留但未合并，master 干净。
- 场景 B 在 **Atlas 本地 clone** 上执行，不改变 Atlas 源仓库历史。
- 场景 A 初始复现了任务2假成功；11.2 是修复后对同一任务的复验，两者都保留，未互相覆盖。
- 当前 Atlas 状态已按 11.1 的合并推进到 `5d588a7…`；§4 记录的 `242069f…` 是第三轮
  当时的历史 HEAD，不作为第四轮当前 HEAD。
