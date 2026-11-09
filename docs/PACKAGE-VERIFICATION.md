# 打包产物验证报告（2026-11-09 10:47 最终构建）

> 本报告记录 **2026-11-09 10:47 最终构建** 的实测结果；00:19 构建及更早版本
> 保留在“历史构建”一节并明确标注 superseded。
> 本轮只修改本文件与 `docs/RELEASE.md`；未修改源码、测试、配置或任何构建产物。
> 未删除 `%APPDATA%\multiagent-desktop` 中的真实数据；打包应用运行时只向该目录追加日志。
> 主要证据：`.e2e/verify-final3-run1.log`、`verify-final3-run2.log`、
> `verify-final3-run3.log`、`.e2e/final3-serve-8771.out.log`、
> `.e2e/desktop-dist-final3.log`、`.e2e/pytest-final4.log`。

## 0. 结论速览

| 检查项 | 结果 |
| --- | --- |
| NSIS setup / portable / blockmap 大小与 SHA256 对照 RELEASE 当前基线 | **全部一致** |
| 包内前端 `index-D23LbL2h.js` vs 工作区 | **一致**；完整 dist 目录 40/40 文件、827,677 B，逐文件 SHA256 差异 0 |
| 包内后端 exe vs `desktop/backend-dist` | **一致**；冻结目录 142/142 文件、32,462,513 B，差异 0；exe 为 10:47:14 产物 |
| `verify_package.ps1` 连续 3 次冒烟 | **3/3 退出码 0**；`backendHealth=true`、`gracefulClose=true`、`forcedKill=false`、无残留、端口已释放 |
| 打包版 `/api/health` | `version=2.0.0`、`schema_version=6` |
| 源码时序 | 当前 `.py` 源文件最新 mtime 2026-11-09 10:37:04，早于新后端 exe 10:47:14；无晚于新 exe 的 Python 源文件 |
| 全量 pytest | **764 passed / 10 skipped / 0 failed**（证据 `.e2e/pytest-final4.log`） |
| merge_* 重试闸门测试 | **15 passed**（blocked 禁止普通 retry、API 409、UI 隐藏重试） |
| Authenticode | setup / portable / `MultiAgent.exe` / 后端 exe 均为 **NotSigned**，存在 SmartScreen 风险 |

**结论：2026-11-09 10:47 最终构建与 RELEASE 当前基线逐项一致；包内后端 exe 哈希与冻结源完全匹配，包内前端 dist 与工作区逐文件一致；应用可正常启动并通过 `/api/health` 校验，连续 3 次优雅退出且无残留进程。**  
唯一遗留风险仍是**未签名**导致的 SmartScreen / “未知发布者”提示，属于发布策略问题，不是本轮构建缺陷。

## 1. 最终构建与历史版本对照

### 1.1 当前最终构建（2026-11-09 10:47）

构建顺序为 `vite build` → `scripts/build_backend.py`（后端 exe 10:47:14）→
`cd desktop && npm run dist`（setup 10:47:56 / portable 10:47:58）。

| 产物 | 大小 | SHA256 | 写入时间 |
| --- | ---: | --- | --- |
| `desktop/release/multiagent-2.0.0-x64-setup.exe` | 128,074,777 B | `97486F6689362189B1A0554E67643BE25349F9692FD93517CE6797F9C476F4B3` | 2026-11-09 10:47:56 |
| `desktop/release/multiagent-2.0.0-x64.exe`（portable） | 127,934,507 B | `5DEE13C532286AE7303ED70F705B9E8DC50C3AD270936FA3485AD2098BED4034` | 2026-11-09 10:47:58 |
| `desktop/release/multiagent-2.0.0-x64-setup.exe.blockmap` | 134,153 B | `D2E54DEB41F87CB9EF2E70E531DBBBE77EFE4E1CB8E677EEC78827A74F0B8BEF` | 2026-11-09 10:47:57 |
| `desktop/release/win-unpacked/MultiAgent.exe` | 246,269,952 B | `F14D45EF2B037EECF73C7D1191B2FA1F2F67BBBDEB554931375522ABF19C27AB` | 2026-11-09 10:47:28 |
| `desktop/release/win-unpacked/resources/app.asar` | 78,135 B | `6954A1B7ECE5B12F2D56F95758899B2F33A2D4B75FA185446BCBFE1759ADC6BA` | 2026-11-09 10:47:26 |
| `desktop/release/win-unpacked/resources/backend/multiagent-backend/multiagent-backend.exe` | 6,215,907 B | `DEB49332DD956B2F69241BEC7BFD51109487FBB826C6C76756D45A1EF6D1ED8F` | 2026-11-09 10:47:14 |
| `desktop/backend-dist/multiagent-backend/multiagent-backend.exe` | 6,215,907 B | `DEB49332DD956B2F69241BEC7BFD51109487FBB826C6C76756D45A1EF6D1ED8F` | 2026-11-09 10:47:14 |
| `desktop/release/win-unpacked/`（目录） | 220 文件 / 417,998,258 B | 目录不适用 | 2026-11-09 10:47:28 |
| `desktop/backend-dist/multiagent-backend/`（目录） | 142 文件 / 32,462,513 B | 目录不适用 | 2026-11-09 10:47:14 |
| `desktop/release/win-unpacked/resources/backend/`（目录） | 143 文件 / 32,462,926 B | 目录不适用 | 2026-11-09 10:47:28 |

### 1.2 历史构建（superseded，仅作追溯）

| 产物 | 22:14（历史） | 22:24（历史） | 23:01（历史） | 00:02（历史） | 00:19（历史，superseded） | 10:47（当前交付） |
| --- | --- | --- | --- | --- | --- | --- |
| setup | 128,060,073 B / `00796E96…ED704965` | 128,061,059 B / `62DCB9F2…8F57E7DD` | 128,065,496 B / `E6FC5D90…AB519048` | 128,071,582 B / `DE147524…F85BE513` | 128,074,395 B / `11FE558B…B1AC428D` | **128,074,777 B / `97486F66…C476F4B3`** |
| portable | 127,919,807 B / `79807897…63EC126B6` | 127,920,785 B / `2B8F8A41…E83ACCC4` | 127,925,233 B / `6C179B5B…67A5D6F0` | 127,931,315 B / `2B1B9F89…419DA23E` | 127,934,125 B / `D12DC1B4…33C18993` | **127,934,507 B / `5DEE13C5…8BED4034`** |
| 后端 exe | 6,202,885 B / `60880307…D489EC8` | 6,204,099 B / `E2674FE5…98EBAA46` | 6,208,820 B / `44FF6C7B…13E30A` | 6,213,707 B / `B6E48B38…D1FEDE` | 6,215,140 B / `0D17B7BF…DAA6980E` | **6,215,907 B / `DEB49332…F6D1ED8F`** |
| 前端入口 | `index-BMnaSU7m.js` | `index-BMnaSU7m.js` | `index-DhRunDb7.js` | `index-BiockU1o.js` | `index-BiockU1o.js` | **`index-D23LbL2h.js`** |
| `backend-dist/` | 142 文件 / 32,442,527 B | 142 文件 / 32,443,741 B | 142 文件 / 32,448,571 B | 142 文件 / 32,459,677 B | 142 文件 / 32,461,110 B | **142 文件 / 32,462,513 B** |
| `win-unpacked/` | 220 文件 / 417,978,272 B | 220 文件 / 417,979,486 B | 220 文件 / 417,984,316 B | 220 文件 / 417,995,422 B | 220 文件 / 417,996,855 B | **220 文件 / 417,998,258 B** |
| `MultiAgent.exe` / `app.asar` | `F14D45EF…C27AB` / `6954A1B7…DC6BA` | 不变 | 不变 | 不变 | 不变 | **不变（重新打包，哈希相同）** |

历史版本不得作为当前交付。00:19 构建的 setup `11FE558B…`、portable `D12DC1B4…`、
后端 exe `0D17B7BF…DAA6980E` 均已被 10:47 构建替代；00:02 及更早的 22:14、22:24、
23:01 更早，仅保留作追溯。

## 2. 验证环境与时间

| 项 | 值 |
| --- | --- |
| 验证时间 | 2026-11-09 10:48–10:49（UTC+8；3 次 verify 于 10:48:30–10:48:42，smoke 于 10:49:16，flows 于 10:49:46） |
| 操作系统 | Windows 11 Home 10.0.26300 |
| PowerShell | 5.1.26100.9818（Windows PowerShell） |
| Python | 3.11.0，`~/multi_xuanwo\.venv\Scripts\python.exe` |
| 哈希方法 | Python `hashlib.sha256`，1 MiB 分块流式计算，`-I` 隔离模式独立运行 |
| 应用版本 | `/api/health` 返回 `version=2.0.0`、`schema_version=6` |
| 被测 exe | `desktop/release/win-unpacked/MultiAgent.exe`（246,269,952 B） |
| verify 脚本 | `desktop/tools/verify_package.ps1`，SHA256 `A5482008956FABBF38F942A77961DFFB12D97939C1BF81CB4D1C5E32E361E943` |
| 启动前预检 | 见证据文件 `.e2e/verify-final3-run*.log` 与 `.e2e/final3-serve-8771.out.log` |

## 3. 产物大小与 SHA256 对照 RELEASE 当前基线

| 产物 | RELEASE 大小 | 实测大小 | RELEASE SHA256 | 实测 SHA256 | 结果 |
| --- | ---: | ---: | --- | --- | --- |
| `multiagent-2.0.0-x64-setup.exe` | 128,074,777 B | 128,074,777 B | `97486F6689362189B1A0554E67643BE25349F9692FD93517CE6797F9C476F4B3` | 同左 | **一致** |
| `multiagent-2.0.0-x64.exe`（portable） | 127,934,507 B | 127,934,507 B | `5DEE13C532286AE7303ED70F705B9E8DC50C3AD270936FA3485AD2098BED4034` | 同左 | **一致** |
| `multiagent-2.0.0-x64-setup.exe.blockmap` | 134,153 B | 134,153 B | `D2E54DEB41F87CB9EF2E70E531DBBBE77EFE4E1CB8E677EEC78827A74F0B8BEF` | 同左 | **一致** |
| `win-unpacked/MultiAgent.exe` | 246,269,952 B | 246,269,952 B | `F14D45EF2B037EECF73C7D1191B2FA1F2F67BBBDEB554931375522ABF19C27AB` | 同左 | **一致** |
| `win-unpacked/resources/app.asar` | 78,135 B | 78,135 B | `6954A1B7ECE5B12F2D56F95758899B2F33A2D4B75FA185446BCBFE1759ADC6BA` | 同左 | **一致** |
| `win-unpacked/.../multiagent-backend.exe` | 6,215,907 B | 6,215,907 B | `DEB49332DD956B2F69241BEC7BFD51109487FBB826C6C76756D45A1EF6D1ED8F` | 同左 | **一致** |
| `backend-dist/.../multiagent-backend.exe` | 6,215,907 B | 6,215,907 B | `DEB49332DD956B2F69241BEC7BFD51109487FBB826C6C76756D45A1EF6D1ED8F` | 同左 | **一致** |

产物时间戳与 RELEASE 记录一致：

- setup：2026-11-09 10:47:56
- portable：2026-11-09 10:47:58
- blockmap：2026-11-09 10:47:57
- `MultiAgent.exe`：2026-11-09 10:47:28
- `app.asar`：2026-11-09 10:47:26
- 后端 exe：2026-11-09 10:47:14

## 4. 包内内容一致性

### 4.1 前端 dist：包内 vs 工作区

工作区路径：`~/multi_xuanwo\multiagent\web\dist\`  
包内路径：`desktop\release\win-unpacked\resources\backend\multiagent-backend\_internal\multiagent\web\dist\`

| 文件 | 大小 | 工作区 SHA256 | 包内 SHA256 | 结果 |
| --- | ---: | --- | --- | --- |
| `assets/index-D23LbL2h.js` | 262,962 B | `D402B323B916FBDF1465AB8EE36825236FB2A0D8C9D2DE8E09F1E5AD9435290B` | 同左 | **一致** |
| `index.html` | 561 B | `3564F1807C132C210989D9EAC8BF5913AD5EFD1475BC85C32EFDD724520A98AA` | 同左 | **一致** |
| `assets/index-DRgukb51.css` | 49,840 B | `C590A5FED089EA35E4AE62BBC77ACE898E685705D9ED5F22BA4B5EA69BF6291C` | 同左 | **一致** |

完整目录逐文件比对：

- 工作区 dist：**40 个文件 / 827,677 B**
- 包内 dist：**40 个文件 / 827,677 B**
- 缺失、额外、大小或 SHA256 不一致：**均为 0**
- 包内 `assets/index-*.js` 入口仅为 **`index-D23LbL2h.js`**（不存在旧入口
  `index-BiockU1o.js`、`index-DhRunDb7.js`、`index-BMnaSU7m.js`、`index-B6iBPHig.js`、
  `index-CyVfnjsD.js`）

包内 `index.html` 实际引用：

```html
<title>MultiAgent · Workspace</title>
<script type="module" crossorigin src="/static/dist/assets/index-D23LbL2h.js"></script>
<link rel="stylesheet" crossorigin href="/static/dist/assets/index-DRgukb51.css">
```

### 4.2 后端 exe 与冻结目录

| 文件 | 包内大小 | 包内 SHA256 | backend-dist 大小 | backend-dist SHA256 | 结果 |
| --- | ---: | --- | ---: | --- | --- |
| `multiagent-backend.exe` | 6,215,907 B | `DEB49332DD956B2F69241BEC7BFD51109487FBB826C6C76756D45A1EF6D1ED8F` | 6,215,907 B | `DEB49332DD956B2F69241BEC7BFD51109487FBB826C6C76756D45A1EF6D1ED8F` | **一致** |

完整冻结目录逐文件比对：

- `desktop/backend-dist/multiagent-backend/`：**142 个文件 / 32,462,513 B**
- 包内 `resources/backend/multiagent-backend/`：**142 个文件 / 32,462,513 B**
- 缺失、额外、大小或 SHA256 不一致：**均为 0**
- 包内 `resources/backend/` 合计 **143 个文件 / 32,462,926 B**，差值来自预置的
  `resources/backend/README.txt`（413 B）

### 4.3 解包目录整体

- `desktop/release/win-unpacked/`：**220 个文件 / 417,998,258 B**
- 与 RELEASE 第 1.1 节记录一致

## 5. `verify_package.ps1` 冒烟测试

### 5.1 执行命令与脚本版本

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "desktop/tools/verify_package.ps1"
```

- 脚本 SHA256：`A5482008956FABBF38F942A77961DFFB12D97939C1BF81CB4D1C5E32E361E943`
- 连续执行 **3 次**，原始输出见 `.e2e/verify-final3-run1.log`、`run2.log`、`run3.log`。

### 5.2 三次 verify 字段表

| 字段 | run 1 | run 2 | run 3 |
| --- | --- | --- | --- |
| `exitCode` | 0 | 0 | 0 |
| `backendHealth` | true | true | true |
| `gracefulClose` | true | true | true |
| `forcedKill` | false | false | false |
| `closeMethod` | `CloseMainWindow` | `CloseMainWindow` | `CloseMainWindow` |
| `mainLeftOver` | none | none | none |
| `backendLeftOver` | none | none | none |
| `electronLeftOver` | none | none | none |
| `portStillListening` | false | false | false |
| `portBindOk` | true | true | true |
| `portListenPids` | none | none | none |
| `netstatPid0Only` | false | false | false |
| `exitReason` | `clean_graceful_close` | `clean_graceful_close` | `clean_graceful_close` |
| `gracefulWaitSeconds` | 1.1 | 1.1 | 1.1 |

`gracefulWaitSeconds` 均为 **1.1 秒**；逐次原始值以
`.e2e/verify-final3-run*.log` 为准（run 1/2/3 = 1.1）。

### 5.3 单次输出示例（run 1）

```json
{
    "exe":  "~/multi_xuanwo/desktop/release/win-unpacked/MultiAgent.exe",
    "exeBytes":  246269952,
    "pid":  15292,
    "windowTitle":  "MultiAgent · Workspace",
    "pageUrl":  "http://127.0.0.1:8770/?desktop=1",
    "backendPort":  8770,
    "backendPid":  "9120",
    "backendHealth":  true,
    "gracefulClose":  true,
    "forcedKill":  false,
    "closeMethod":  "CloseMainWindow",
    "mainLeftOver":  "none",
    "backendLeftOver":  "none",
    "electronLeftOver":  "none",
    "portStillListening":  false,
    "portBindOk":  true,
    "portListenPids":  "none",
    "netstatPid0Only":  false,
    "exitReason":  "clean_graceful_close",
    "exitCode":  0,
    "gracefulWaitSeconds":  1.1
}
```

### 5.4 修复前后差异（关闭竞态）

此前一轮 verify 曾出现关闭竞态：第一次 `exitCode=3`、`gracefulClose=false`、
`backendLeftOver=21668,9260`（Electron GPU/network 子进程），第二次才 `exitCode=0`。
该修复在 00:02 构建引入；00:19 与 10:47 两次重打包均再次连续 3 次验证稳定：

| 项目 | 修复前（历史） | 修复后（当前链路） |
| --- | --- | --- |
| 进程识别 | 宽松命令行正则，Electron 子进程被误计入后端残留 | `Win32_Process` 进程树，区分主进程、后端与 Electron 子进程 |
| 关闭等待 | 等待不足/存在竞态，首次退出失败 | 有界优雅等待（`CloseMainWindow`，约 1 秒级；10:47 实测 1.1 秒） |
| 超时收尾 | 无强制收尾或收尾不完整 | 超时后强制收尾，并复核残留 |
| 端口判定 | 关闭瞬间 `PID 0` 条目易误判 | `netstat` + 实际 `bind` + 监听 PID 三态判定 |
| 结果 | 第一次 exit 3，第二次才 exit 0 | **3/3 exitCode=0，全部 clean** |

证据：修复前见 `.e2e/recheck-final/a2/report.json`（`verifyExitCodes: [3, 0]`）与
`.e2e/recheck-final/a2/verify.stdout.attempt1.txt`；修复后见
`.e2e/verify-final3-run1..3.log`。

### 5.5 优雅关闭与残留检查

| 检查 | 结果 |
| --- | --- |
| 3 次运行主进程是否退出 | 是（`gracefulClose=true`） |
| 3 次运行后端残留 | `backendLeftOver=none` |
| 3 次运行 Electron 子进程残留 | `electronLeftOver=none` |
| 3 次运行主进程残留 | `mainLeftOver=none` |
| 3 次运行 8770 是否仍监听 | 否（`portStillListening=false`） |
| 3 次运行端口可重新绑定 | 是（`portBindOk=true`） |
| 3 次运行监听 PID | `portListenPids=none` |
| 3 次运行 `netstat` 是否仅 PID 0 | 否（`netstatPid0Only=false`） |
| 强制终止是否触发 | 否（`forcedKill=false`） |

### 5.6 打包版 `/api/health`

`.e2e/final3-serve-8771.out.log` 记录本轮 `/api/health` 两次 `200 OK`；权威实测
`version=2.0.0`、`schema_version=6`，其余字段见证据文件。

关闭方式为不带 `/F` 的 `taskkill`；关闭后应用进程为 `none`。8770 可能残留
内核 `PID 0` 的假 Listen 行（无真实监听进程），以 `bind` 实测为准：`bind` 成功。
详见 `.e2e/final3-serve-8771.out.log`。

## 6. 数据目录隔离（未使用开发仓库 `.multiagent`）

- 打包版数据根按设计为 `%APPDATA%\multiagent-desktop`（历史实测）；本轮 health
  复核了 `version=2.0.0` / `schema_version=6`，`home` 字段见证据文件。
- 本轮未删除、未重置 `%APPDATA%\multiagent-desktop` 中的真实数据。
- 由于该机器已有历史数据，本轮**没有**验证“全新机器首次启动 / 空数据初始化”场景；
  需要时应在干净 Windows 环境复测。

## 7. Authenticode 与 SmartScreen 风险

`Get-AuthenticodeSignature` 对当前最终产物独立检查结果：

| 文件 | 签名状态 | 签名者 |
| --- | --- | --- |
| `multiagent-2.0.0-x64-setup.exe` | **NotSigned** | 无 |
| `multiagent-2.0.0-x64.exe` | **NotSigned** | 无 |
| `win-unpacked/MultiAgent.exe` | **NotSigned** | 无 |
| `win-unpacked/resources/backend/multiagent-backend/multiagent-backend.exe` | **NotSigned** | 无 |

签名就绪检查：`desktop/tools/sign_release.ps1`（SHA256
`E03E5F2BA31248D06D0266CDC48473DDD3E2BFE3B331A4167F6242CC006B8A53`）的
`-CheckOnly` 返回**退出码 2**，输出 `STATUS: NOT READY: no certificate`；
本机未找到 `signtool.exe`，证书库也没有带私钥的代码签名证书。`CheckOnly`
不签名、不修改目标文件；完整流程见 `docs/RELEASE.md` 第 5.1 节。

风险说明：

- 安装包与 portable **没有代码签名证书**；Windows SmartScreen 可能显示
  “Windows 已保护你的电脑 / 未知发布者”，用户需点“更多信息 → 仍要运行”。
- 浏览器下载、企业终端管控、杀软信誉系统也可能对未签名 exe 给出警告或拦截。
- 本轮无法通过签名验证发布者身份；交付时应通过可信渠道同时提供本报告的 SHA256，
  供接收方自行校验。
- 如需消除该风险，需要主线另行使用 OV/EV 代码签名证书并加时间戳重新签名；
  本轮未改动机器级证书配置，也未对产物签名，**不得声称签名有效**。

## 8. 失败、跳过与限制

| 项 | 说明 |
| --- | --- |
| NSIS 安装器未执行 | 为避免修改系统（快捷方式、注册表、安装目录），本轮**未运行 setup.exe 安装流程**；只对 setup 做了大小/SHA256 校验。安装器 UI、快捷方式创建、升级/卸载路径未覆盖。 |
| portable 未直接启动 | portable 做了大小/SHA256 校验，但未实际启动（启动会解压到临时目录）；实际启动验证使用的是 `win-unpacked` 打包应用。 |
| 未做完整 Agent 任务 E2E | 已验证启动、页面加载、`/api/health`、静态资源与优雅退出；未在打包版中执行真实 Agent 任务/验收/快照同步。 |
| 未做干净机器验证 | 验证机器具备开发环境与历史 `%APPDATA%` 数据；未在无 Python、无历史数据的干净 Windows 上复测。 |
| 关闭修复仅 3 次验证 | 10:47 重打包后 3/3 稳定通过，但不等同于长时间压力测试；后续如改动关闭链路，应重新跑多次 verify。 |
| 历史版本已 superseded | 22:14、22:24、23:01–23:02、00:02、00:19 构建及对应报告均为历史；00:19 已 superseded，不得作为当前交付。 |

## 9. 复核命令

哈希（Python 3.11，`hashlib`，流式读取）：

```powershell
.\.venv\Scripts\python.exe -I -c "
import hashlib, os
for p in [
  r'desktop\release\multiagent-2.0.0-x64-setup.exe',
  r'desktop\release\multiagent-2.0.0-x64.exe',
  r'desktop\release\multiagent-2.0.0-x64-setup.exe.blockmap',
  r'desktop\release\win-unpacked\MultiAgent.exe',
  r'desktop\release\win-unpacked\resources\app.asar',
  r'desktop\release\win-unpacked\resources\backend\multiagent-backend\multiagent-backend.exe',
  r'desktop\backend-dist\multiagent-backend\multiagent-backend.exe',
  r'multiagent\web\dist\assets\index-D23LbL2h.js',
  r'multiagent\web\dist\index.html',
  r'multiagent\web\dist\assets\index-DRgukb51.css',
]:
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''):
            h.update(chunk)
    print(h.hexdigest().upper(), os.path.getsize(p), p)
"
```

冒烟测试：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "desktop/tools/verify_package.ps1"
```

签名检查：

```powershell
Get-AuthenticodeSignature "desktop\release\multiagent-2.0.0-x64-setup.exe"
Get-AuthenticodeSignature "desktop\release\multiagent-2.0.0-x64.exe"
Get-AuthenticodeSignature "desktop\release\win-unpacked\MultiAgent.exe"
Get-AuthenticodeSignature "desktop\release\win-unpacked\resources\backend\multiagent-backend\multiagent-backend.exe"
```

签名就绪检查（无证书时退出码 2，不签名）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "desktop/tools/sign_release.ps1" -CheckOnly
```

## 10. 最终判定

- 2026-11-09 10:47 最终构建的 setup、portable、blockmap、`win-unpacked` 与
  `docs/RELEASE.md` 当前基线**逐项一致**，未发现哈希/大小偏差。
- 包内前端与后端均与工作区/冻结目录**逐文件一致**（dist 40/40，后端 142/142）；
  包内后端 exe 哈希 `DEB49332…F6D1ED8F` 与 `desktop/backend-dist` 完全匹配。
- `verify_package.ps1` 连续 3 次 `exitCode=0`；应用成功启动、`backendHealth=true`、
  优雅关闭、无主/后端/Electron 残留、8770 已释放并可重新绑定。
- 打包版 `/api/health` 返回 `version=2.0.0`、`schema_version=6`。
- 全量 pytest 为 **764 passed / 10 skipped / 0 failed**；`merge_*` 重试闸门测试
  **15 passed**，覆盖 blocked 禁止普通 retry、API 409 与 UI 隐藏重试。
- 00:19 硬化补丁覆盖 missing_branch、ref_unavailable、rebuild 回滚、标题变更
  分支记录与 create_worktree 目录确认；10:47 构建已包含这些修复。
- **未签名（NotSigned）是已知交付风险**，可能导致 SmartScreen / 未知发布者提示；
  `sign_release.ps1 -CheckOnly` 退出码 2（no certificate）；本报告不构成签名或安全信任背书。
- 00:19 构建及更早的 00:02、22:14、22:24、23:01–23:02 构建均已被本轮替代，仅作历史记录。
