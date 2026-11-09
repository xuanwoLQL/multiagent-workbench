# Windows 桌面打包记录（V2 UI · 2026-11-08）

本文记录把新版 V2 控制台打入 Windows 桌面版的**实际执行命令、产物、哈希与验证证据**。
本次只构建 **portable** 目标，输出目录为 `desktop/release-v2/`，不涉及签名与自动更新。

> **最终重建（2026-11-08 17:00，v2.0.0）**：版本号已统一为 2.0.0（pyproject /
> `multiagent.__version__` / `desktop/package.json` / `webapp/package.json`）。
> 最终产物：`desktop/release-v2/multiagent-2.0.0-x64.exe`，116,303,771 B，
> SHA256 `F2A6F0FB1CECA146E0FA5D19C9BA8400FAC26F2AEC34A9883762EFB3B7C42DB9`；
> 内置前端为 `index-CwN5vJZR.js` / `index-DchAgMOQ.css`（与工作区 dist 逐字节一致）。
> 下文的 1.0.0 数值是 16:25 的历史快照，仅用于保留当时的验证证据。

> 与 `docs/RELEASE.md` 的关系：`RELEASE.md` 记录的是更早的 `desktop/release/`（1.0.0，schema 5）。
> 本次把 V2 前端与 schema 6 后端打入 `desktop/release-v2/`；两者不要混用。

## 1. 构建环境

| 项 | 值 |
| --- | --- |
| OS | Windows 10.0.26300 |
| Python | 3.11.0（`.venv`） |
| PyInstaller | 6.22.3 |
| Node / npm | 本机 Node.js（`npx vite build` 成功） |
| Electron | 44.10.2 |
| electron-builder | 25.1.8 |
| 前端 | `webapp` v2.0.0，Vite 6.4.5 |
| 桌面壳 | `desktop/package.json` v1.0.0 |

## 2. 构建快照

构建期间仓库仍有其他 Agent 在并行改代码。本次产物锁定在以下快照：

- 前端源码最新修改：`2026-11-08 16:21:13`
- `npx vite build` 完成：`2026-11-08 16:22:22`
- PyInstaller 后端完成：`2026-11-08 16:22:40`
- portable 完成：`2026-11-08 16:25:00`

说明：16:24:14 另一个并行进程又重建了工作区 `multiagent/web/dist`（当前为 `index-CyVfnjsD.js` /
`index-DchAgMOQ.css`）。**该次重建没有进入本次安装包**；安装包内是 16:22 冻结的
`index-B6iBPHig.js` / `index-2KRBY1AT.css`。需要绝对最新代码时请重新执行本文第 6 节。

## 3. 产物与哈希

### 3.1 可交付 portable

| 文件 | 大小 | SHA256 |
| --- | ---: | --- |
| `desktop/release-v2/multiagent-1.0.0-x64.exe` | 116,304,003 B（约 110.9 MiB） | `49442591698F4FA0052754D259658483A11A92709E89FBAF59041BBF6530FE43` |

### 3.2 解包目录

| 文件 | 大小 | SHA256 |
| --- | ---: | --- |
| `desktop/release-v2/win-unpacked/MultiAgent.exe` | 246,269,952 B | `BF12886AC7C3A87F8588A90803572B1A3FF0A7CF854D4F9B85988E27E2C49D3B` |
| `desktop/release-v2/win-unpacked/resources/app.asar` | 78,135 B | `C5C1B43DCBE5A2315676CAAF0D71B830EB2FE00DF1D94F79DF6C54F8808A8932` |
| `desktop/backend-dist/multiagent-backend/multiagent-backend.exe` | 6,148,050 B | `5CB1CA5CD4C0B6A27D8A0DBE2707D4F479022BA9A33A64ABE5C6BB5CC49347D2` |

### 3.3 前端 dist（本次冻结版本）

| 文件 | 大小 | SHA256 |
| --- | ---: | --- |
| `multiagent/web/dist/index.html` | 561 B | `347732392B86A099624DD2A8A8F0E9E5D19F8B55F71AE3DFF0F0A9ACDA46743C` |
| `multiagent/web/dist/assets/index-B6iBPHig.js` | 260,688 B | `4EFFCA32A0104F1B426BC94A7B54664336B3DFD8C2BC4001C4EB4B10BC689B6E` |
| `multiagent/web/dist/assets/index-2KRBY1AT.css` | 40,062 B | `6B6ADA9B7C2F7514CDDC4DDFDA836736CF430BA80B7FA813CD84914DBB2337F9` |

dist 合计：**40 个文件 / 749,780 B**。`index.html` 引用：
`/static/dist/assets/index-B6iBPHig.js` 与 `/static/dist/assets/index-2KRBY1AT.css`。

### 3.4 后端目录

- `desktop/backend-dist/multiagent-backend/`：**142 个文件 / 32,316,759 B**
- 冻结目录内包含 `_internal/multiagent/web/dist/index.html`（哈希与 3.3 一致）
- 安装包 `resources/backend/multiagent-backend/` 与冻结目录逐文件一致（142/142，无差异）
- `resources/backend/` 根目录另有预置的 `README.txt`（413 B，2026-11-06），会随
  `extraResources` 一起复制；因此安装包内 `resources/backend` 合计 **143 个文件 /
  32,317,172 B**。

## 4. 验证记录

### 4.1 前端构建

命令：

```powershell
cd ~/multi_xuanwo\webapp
npx vite build
```

结果：`1707 modules transformed`，`✓ built in 1.73s`，退出码 0。产物中 39 个增量 chunk + 1 个
`index.html`，共 40 个文件。Vite 提示主包曾超过 500 kB；本次已通过动态 import 拆出
`AgentsView / RunsView / ProjectsView / TasksView / MemoryView / FilesView / ReviewView /
SettingsView / TaskDetailView` 等 chunk。

### 4.2 PyInstaller 冻结

命令：

```powershell
cd ~/multi_xuanwo
.\.venv\Scripts\python.exe scripts\build_backend.py
```

结果：`Build complete!`，生成 `desktop/backend-dist/multiagent-backend/multiagent-backend.exe`。
冻结树内的 `_internal/multiagent/web/dist/index.html` 与工作区 dist 哈希一致。

PowerShell 注意：PyInstaller 把 INFO 日志写到 stderr，PowerShell 7 在管道中会把它显示成
`NativeCommandError`，命令层可能看到退出码 1；但 PyInstaller 自身输出为
`Build complete!`，产物完整。CI 中建议把 stderr 重定向到文件后再判断 `$LASTEXITCODE`。

### 4.3 冻结后端实机检查

使用随机空闲端口 + 独立 `MULTIAGENT_HOME` 启动：

```powershell
$env:MULTIAGENT_HOME = "$env:TEMP\ma-pack-verify2-0ac68b0b"
desktop\backend-dist\multiagent-backend\multiagent-backend.exe serve --host 127.0.0.1 --port 57496
```

实测（随后只杀掉本次启动的进程树）：

| 检查 | 结果 |
| --- | --- |
| `GET /api/health` | 200；`version=1.0.0`、`schema_version=6`、`bind_host=127.0.0.1`、`loopback_only=true` |
| `GET /` | 200；长度 560；包含 `static/dist/assets/`；返回 V2 `index.html` |
| `GET /static/dist/assets/index-B6iBPHig.js` | 200；260,688 B |
| `GET /static/dist/assets/index-2KRBY1AT.css` | 200；40,062 B |
| `GET /api/projects` | 200；`[]`（独立 home 为空） |
| 启动日志 | `Uvicorn running on http://127.0.0.1:57496`，无异常 |

### 4.4 Electron portable 打包

命令：

```powershell
cd ~/multi_xuanwo\desktop
npm run dist:portable:v2
```

结果：electron-builder 25.1.8 成功完成 `packaging platform=win32 arch=x64 electron=44.10.2`，
生成 `release-v2\multiagent-1.0.0-x64.exe`。

签名状态：日志明确为 `no signing info identified, signing is skipped` / `signHook=false
cscInfo=null`。三个 PE（后端 exe、`MultiAgent.exe`、portable）均**未签名**。

### 4.5 安装包内容检查

`npx asar list release-v2\win-unpacked\resources\app.asar` 确认包含：

```text
\assets\badge.png
\assets\icon.ico
\main.js
\package.json
\preload.js
\splash.html
```

`resources/backend/multiagent-backend/` 包含冻结后端；其中
`_internal/multiagent/web/dist/index.html` 的 SHA256 为
`347732392B86A099624DD2A8A8F0E9E5D19F8B55F71AE3DFF0F0A9ACDA46743C`，与 3.3 一致。
`multiagent-backend.exe` 与 `desktop/backend-dist` 中的同一文件哈希一致
（`5CB1CA5C…49347D2`）。

### 4.6 打包后真实启动

直接启动 `desktop/release-v2/win-unpacked/MultiAgent.exe`（带 `MULTIAGENT_HOME` 与
`--remote-debugging-port`；该次 `$HOME` 覆盖未生效的细节见第 5 节第 7 条）：

- 页面 URL：`http://127.0.0.1:8770/?desktop=1`
- 页面标题：`MultiAgent · Workspace`
- `#root` 子节点数：1
- 渲染文本匹配：`打开本地目录`、`本地优先`、`项目目录中的文件不会被移动`
- 截图：`.e2e/packaged-ui-v2.png`（121,363 B）
- 后端日志只请求 `index-B6iBPHig.js` / `index-2KRBY1AT.css` 与 `/api/projects`、
  `/api/agents/detail`，**没有** `/static/index.html`、`/static/app.js` 等旧版资源

截图确认首屏为 V2 onboarding（“把一个真实项目交给 Agent 团队” / “选择一个工作区”），
不是旧版 `static/index.html` 控制台。

## 5. 已知缺口

1. **未签名**：首次运行可能触发 SmartScreen；日志已确认 `cscInfo=null`。
2. **本次只出 portable**：没有重新生成 NSIS 安装包。`release-v2/` 里的
   `multiagent-0.1.0-x64-setup.exe`、`multiagent-0.1.0-x64.exe` 是 2026-11-07 的旧产物，
   不属于本次构建。
3. **版本号不统一**：桌面壳 `desktop/package.json` 仍是 `1.0.0`，所以产物名为
   `multiagent-1.0.0-x64.exe`；V2 前端是 `2.0.0`，后端 health 也报 `version=1.0.0`。
4. **构建快照会过期**：仓库仍在被其他 Agent 并行修改；16:24 的工作区 dist 已与安装包内
   bundle 不同。本文所有哈希只对应 16:22 快照。
5. **未做打包后的完整 Agent 任务 E2E**：本次验证到“应用启动 + V2 首屏 + API 健康”为止，
   没有在打包版里跑一次真实 Codex/Claude 任务。
6. **PyInstaller 警告**：`Hidden import "tzdata" not found!`。当前依赖路径未使用该包；
   若后续需要完整 zoneinfo 时区数据，应显式安装 `tzdata`。
7. **打包后启动测试的数据目录**：PowerShell 的 `$HOME` 是只读自动变量，验证脚本未能覆盖，
   因此该次启动把 `MULTIAGENT_HOME` 指向了 `~`。Electron 在该目录生成了
   `Cache/`、`Code Cache/`、`GPUCache/` 等 16:25 的浏览器缓存目录；沙箱策略拒绝了删除
   操作，这些目录仍保留。`.multiagent/` 与 `config/` 在测试前已存在，未删除。
8. **无自动更新 / 无代码签名流水线**：升级仍需重新分发 portable。
9. **验证脚本是新增文件**：`.e2e/verify_packaged_ui.py` 仅用于打包验收，不是应用源码；
   本次没有修改任何应用源码或构建脚本。

## 6. 复现命令

```powershell
# 0) 工作区根目录
cd ~/multi_xuanwo

# 1) 前端（输出到 multiagent/web/dist）
cd webapp
npx vite build
cd ..

# 2) 冻结后端（需要已安装 PyInstaller）
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe scripts\build_backend.py

# 3) 真实启动冻结后端（隔离 home + 随机端口）
$env:MULTIAGENT_HOME = "$env:TEMP\ma-verify"
desktop\backend-dist\multiagent-backend\multiagent-backend.exe serve --host 127.0.0.1 --port 8799
# 另开终端：
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8799/api/health
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8799/
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8799/api/projects

# 4) portable
cd desktop
npm run dist:portable:v2

# 5) 内容检查
npx asar list release-v2\win-unpacked\resources\app.asar
Get-FileHash release-v2\multiagent-1.0.0-x64.exe -Algorithm SHA256
```

打包后可用 `desktop\tools\verify_package.ps1` 做启动/单实例/退出冒烟；本次未执行该脚本，
而是用独立的 `.e2e/verify_packaged_ui.py` 做了启动与 V2 首屏检查。
