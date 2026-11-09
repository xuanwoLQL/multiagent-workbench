# 桌面端（Electron）

把 Python 后端当子进程拉起来，再开一个桌面窗口指向它。UI 与浏览器端共用
`multiagent/web/` 这一份控制台代码。

## 运行

```powershell
cd desktop
npm install      # 首次，装 Electron
npm start
```

或直接双击 `desktop\start.cmd`（自动 `npm install` + `npm start`）。
`desktop\launch.cmd` 是「不经过 npm、直接起 electron.exe」的快捷方式。

> 两个 `.cmd` 故意只写英文：cmd.exe 用系统代码页（简体中文=936/GBK）逐字节解析批处理，
> UTF-8 中文会让它读错行（实测 `if errorlevel` 会被截成 `orlevel`），加 `chcp 65001`
> 也救不回来。文件内保留 `chcp 65001` 是为了让 npm / node / electron 输出的 UTF-8 中文
> 在控制台正常显示。

## 启动流程

1. **单实例锁**：第二次双击不再起第二份后端，只把已有窗口提到前台（`second-instance`）。
2. **先开窗、再等后端**：窗口立刻显示暖纸色加载页（`splash.html`：#f2f0ea + 品牌 +
   进度文案），随后才 spawn 后端，用户不会对着白屏干等。
3. 选一个空闲端口（从 8770 起，8899 之类被占用会自动顺延），
   `spawn python -m multiagent serve`，工作目录 `MULTIAGENT_HOME` 指向数据目录；
   后端继承 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`，保证中文全链路不乱码。
4. 轮询 `/api/health`（最长 60s，实时把秒数写进加载页）→ 就绪后窗口导航到
   `http://127.0.0.1:<port>/?desktop=1`。
5. 后端起不来 → 错误页（不是白屏）：显示失败原因、实际执行命令、日志路径，
   并给出「重试启动 / 打开日志 / 打开数据目录 / 退出」四个按钮；菜单
   「文件 → 重试启动后端」等价。
6. 运行中后端意外退出 → 自动回到可重试错误页。
7. 关窗即退出：`taskkill /T` 收掉整棵后端进程树，不留孤儿 python.exe。

后端日志写到 `<数据目录>/.multiagent/logs/desktop-backend.log`。

## 窗口状态

`%APPDATA%\multiagent-desktop\window-state.json` 记录尺寸/位置/是否最大化，
下次原位打开；换小屏或拔掉外接屏时会自动收进当前显示器工作区，不会开在屏幕外。

## 原生桥（preload.js）

渲染进程只拿到这些能力（`window.multiagentDesktop`），没有 Node / ipcRenderer：

| 成员 | 说明 |
| --- | --- |
| `isDesktop` | 恒为 `true`，前端据此显示「桌面版」徽标 |
| `apiToken` | 页面脚本执行前同步注入的 Bearer token（没有则为空串），**不进 URL/历史记录** |
| `info()` | Electron 版本、数据目录、日志路径、后端端口 |
| `pickDirectory()` | 选目录（导入工作区） |
| `openPath(p)` | 用系统默认方式打开路径 |
| `openDataDir()` / `openLogs()` | 打开数据目录 / 日志目录 |
| `retryBackend()` / `quit()` | 错误页上的重试与退出 |

## 环境变量

- `MULTIAGENT_HOME`：覆盖数据目录（默认打包版 `%APPDATA%\multiagent-desktop`、开发版仓库根）。
  设了它之后 userData 也指向该目录，单实例锁与磁盘缓存随之隔离——绿色版自定义数据位置、
  或在本机并行跑两份实例做验证时用。
- `MULTIAGENT_PYTHON`：指定 Python 解释器（默认用仓库内 `.venv`，否则 PATH 里的 python）。
- `MULTIAGENT_BACKEND=frozen`：开发模式也优先用 `desktop/backend-dist` 里的冻结后端。
- `MULTIAGENT_DEVTOOLS=1`：打包版也允许 F12 / Ctrl+Shift+I 打开开发者工具
  （默认生产环境禁用）。
- `MULTIAGENT_API_TOKEN`：覆盖后端 token（默认读 `<数据目录>/.multiagent/token`）。

## 图标

`assets\icon.ico`（16/24/32/48/64/128/256）、`assets\icon.png`（512）、`assets\icon-1024.png`
由脚本生成，`desktop\assets\` 是同一份拷贝（随 electron-builder `files`/`extraResources` 打包）：

```powershell
cd desktop
npm run icons        # 等价 .\.venv\Scripts\python.exe tools\make_icons.py（需要 Pillow）
```

## 打包

```powershell
# 1) 先在仓库根冻结自包含后端
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe scripts\build_backend.py

# 2) 再打桌面安装包 / 绿色版
cd desktop
npm install
npm run dist             # NSIS 安装包 + portable
npm run dist:portable    # 仅免安装单文件
```

产物在 `desktop\release\`：

- `multiagent-<版本>-x64-setup.exe`：NSIS 安装包（可选安装目录、桌面快捷方式）
- `multiagent-<版本>-x64.exe`：portable 免安装单文件
- `win-unpacked\多Agent协作工作台.exe`：解包目录，便于直接验证

打包后：

- 后端来自 `resources\backend\multiagent-backend\`（PyInstaller 冻结，**目标机不需要装 Python**）；
  找不到才回退 `.venv` / PATH 里的 python。
- 数据目录是 `app.getPath("userData")`，固定为 `%APPDATA%\multiagent-desktop`（ASCII 路径，
  避免中文路径传给 Python/CLI Agent），与安装目录分离、可写；开发模式仍用仓库根。
- 未配置代码签名时 electron-builder 会跳过签名（提示 `no signing info identified`），属正常。
