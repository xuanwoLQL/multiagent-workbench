"use strict";

/**
 * Electron 主进程：本地 Python 后端 + 桌面壳。
 *
 * 职责边界（业务逻辑一律不在这里）：
 *   1. 单实例锁：第二次双击只把已有窗口提到前台，不再起第二份后端；
 *   2. 后端 1.2s 内就绪就直接进应用，超过 1.2s 才显示启动页，绝不白屏干等；
 *   3. 选一个空闲端口 spawn 后端（开发模式用 .venv，打包模式用 PyInstaller 冻结产物），
 *      轮询 /api/health 就绪后把窗口导航到 http://127.0.0.1:<port>/；
 *   4. 记住窗口尺寸/位置/最大化状态（userData/window-state.json），多显示器安全恢复；
 *   5. 后端起不来或退出时给出可操作错误页：日志路径 + 重试 / 打开日志 / 退出；
 *   6. 渲染进程崩溃时弹出 Reload / Quit 恢复对话框；后端意外退出时回到错误页；
 *   7. 退出时把后端进程树一起收掉，不留孤儿 python.exe。
 *
 * 环境变量：
 *   MULTIAGENT_HOME        覆盖数据目录（绿色版自定义位置 / 多实例并行调试，单实例锁随之隔离）
 *   MULTIAGENT_PYTHON      指定 Python 解释器（开发模式）
 *   MULTIAGENT_BACKEND     设为 frozen 时开发模式也优先用 desktop/backend-dist 里的冻结后端
 *   MULTIAGENT_DEVTOOLS    设为 1 时打包版也允许 F12 / Ctrl+Shift+I 开发者工具
 *   MULTIAGENT_API_TOKEN   覆盖后端 token（默认读 <home>/.multiagent/token）
 */

const {
  app,
  BrowserWindow,
  Menu,
  dialog,
  shell,
  ipcMain,
  screen,
  Notification,
  nativeImage,
  session,
} = require("electron");
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const net = require("net");
const path = require("path");

const PROJECT_ROOT = path.resolve(__dirname, "..");
const MIN_PORT = 8770;
const HEALTH_TIMEOUT_MS = 60000;
const SPLASH_DELAY_MS = 1200;
const MIN_WIDTH = 1100;
const MIN_HEIGHT = 700;
const APP_TITLE = "MultiAgent 工作台";
const APP_ID = "com.multiagent.workbench";

try {
  app.setName("MultiAgent");
} catch (_) { /* 名字设置失败不影响启动 */ }

/**
 * userData 固定成 ASCII 路径。
 * 默认会跟着 productName 变成 `%APPDATA%\MultiAgent`，而后端要把这个目录当
 * MULTIAGENT_HOME 传给 Python 与各种 CLI Agent —— 中文路径在部分 CLI 上是雷。
 *
 * `MULTIAGENT_HOME=<目录>` 可整体覆盖数据目录（绿色版自定义数据位置 / 多实例并行调试）：
 * 此时 userData 也指向该目录，单实例锁与磁盘缓存随之隔离，不会和正常安装的那份打架。
 */
const HOME_OVERRIDE = (process.env.MULTIAGENT_HOME || "").trim();
const USER_DATA_DIR = HOME_OVERRIDE
  ? path.resolve(HOME_OVERRIDE)
  : path.join(app.getPath("appData"), "multiagent-desktop");
try {
  app.setPath("userData", USER_DATA_DIR);
} catch (_) { /* 极端情况下保持默认 */ }

/** 打包版默认禁开发者工具；开发版或显式 MULTIAGENT_DEVTOOLS=1 时放开。 */
const DEVTOOLS_ALLOWED = !app.isPackaged || process.env.MULTIAGENT_DEVTOOLS === "1";

let backend = null;
let backendPort = 0;
let win = null;
let logStream = null;
let logFile = "";
let quitting = false;
let starting = false;
let stopping = false;
let ready = false;
let saveStateTimer = null;
let backendSpawnError = "";
let backendCommand = "";
let splashTimer = null;
let splashDeadlinePassed = false;
let splashLoading = null;
let currentView = "none"; // none | splash | app
let revealed = false;
let lastProjectRoot = "";
let pendingMenuActions = [];
let badgeImage = null;
let rendererRecovery = false;

/* ------------------------------------------------------------------ 路径 */

/** 工作台数据目录：MULTIAGENT_HOME 覆盖 > 打包版 userData > 开发版仓库根。 */
function HOME() {
  if (HOME_OVERRIDE) return USER_DATA_DIR;
  return app.isPackaged ? app.getPath("userData") : PROJECT_ROOT;
}

function dataDir() {
  return path.join(HOME(), ".multiagent");
}

function logsDir() {
  return path.join(dataDir(), "logs");
}

function defaultLogFile() {
  return path.join(logsDir(), "desktop-backend.log");
}

/** 图标目录：打包后来自 extraResources（resources/assets），开发时来自仓库 assets/。 */
function assetsDir() {
  return app.isPackaged
    ? path.join(process.resourcesPath, "assets")
    : path.join(PROJECT_ROOT, "assets");
}

function windowIcon() {
  const png = path.join(assetsDir(), "icon.png");
  return fs.existsSync(png) ? png : undefined;
}

/* -------------------------------------------------------------- 后端进程 */

/**
 * 解析后端启动方式：打包好的自包含后端 → 显式指定的 python → 仓库 .venv → PATH python。
 */
function resolveBackend() {
  const resources = process.resourcesPath || PROJECT_ROOT;
  const ext = process.platform === "win32" ? ".exe" : "";
  const frozen = [
    path.join(resources, "backend", `multiagent-backend${ext}`),
    path.join(resources, "backend", "multiagent-backend", `multiagent-backend${ext}`),
  ];
  if (!app.isPackaged && process.env.MULTIAGENT_BACKEND === "frozen") {
    frozen.unshift(path.join(__dirname, "backend-dist", "multiagent-backend", `multiagent-backend${ext}`));
  }
  for (const candidate of frozen) {
    if (fs.existsSync(candidate)) return { command: candidate, args: [], kind: "frozen" };
  }

  if (process.env.MULTIAGENT_PYTHON) {
    return { command: process.env.MULTIAGENT_PYTHON, args: ["-m", "multiagent"], kind: "python" };
  }
  const venv = process.platform === "win32"
    ? path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe")
    : path.join(PROJECT_ROOT, ".venv", "bin", "python");
  if (fs.existsSync(venv)) return { command: venv, args: ["-m", "multiagent"], kind: "venv" };
  return { command: process.platform === "win32" ? "python" : "python3", args: ["-m", "multiagent"], kind: "python" };
}

function logLine(text) {
  if (!logStream) return;
  try {
    logStream.write(`${text}\n`);
  } catch (_) { /* 日志写失败不影响主流程 */ }
}

function startBackend(port) {
  const resolved = resolveBackend();
  const home = HOME();
  fs.mkdirSync(logsDir(), { recursive: true });
  logFile = defaultLogFile();
  logStream = fs.createWriteStream(logFile, { flags: "a" });
  logLine(`\n===== ${new Date().toISOString()} start ${resolved.command} (${resolved.kind}) port=${port} =====`);
  logLine(`home=${home} packaged=${app.isPackaged}`);

  const args = [...resolved.args, "serve", "--host", "127.0.0.1", "--port", String(port)];
  backendSpawnError = "";
  backendCommand = [resolved.command, ...args].join(" ");
  const env = {
    ...process.env,
    MULTIAGENT_HOME: home,
    // 中文项目名 / 任务 / Agent 输出全链路不乱码的前提
    PYTHONUTF8: "1",
    PYTHONIOENCODING: "utf-8",
  };
  if (!app.isPackaged && (resolved.kind === "venv" || resolved.kind === "python")) {
    // MULTIAGENT_HOME 覆盖时 cwd 不再是仓库根，显式补上模块搜索路径
    env.PYTHONPATH = env.PYTHONPATH
      ? `${PROJECT_ROOT}${path.delimiter}${env.PYTHONPATH}`
      : PROJECT_ROOT;
  }
  backend = spawn(resolved.command, args, {
    cwd: home,
    env,
    windowsHide: true,
  });
  backend.stdout.pipe(logStream);
  backend.stderr.pipe(logStream);
  backend.on("error", (err) => {
    backendSpawnError = err.message;
    logLine(`backend spawn error: ${err.message}`);
  });
  backend.on("exit", (code, signal) => {
    const wasReady = ready;
    logLine(`backend exited code=${code} signal=${signal}`);
    backend = null;
    const intentional = quitting || stopping;
    closeLog();
    if (intentional) return;
    if (wasReady) {
      void showFailurePage(`后端进程意外退出（code=${code ?? "null"}, signal=${signal ?? "null"}）。`);
    }
  });
}

function closeLog() {
  if (!logStream) return;
  try {
    logStream.end();
  } catch (_) { /* ignore */ }
  logStream = null;
}

function stopBackend() {
  if (!backend) {
    closeLog();
    return;
  }
  stopping = true;
  const pid = backend.pid;
  logLine(`stopping backend pid=${pid}`);
  try {
    if (process.platform === "win32") {
      // 后端会再 spawn CLI Agent 子进程，必须整树杀掉
      spawnSync("taskkill", ["/pid", String(pid), "/f", "/t"], { windowsHide: true, timeout: 5000 });
    } else {
      backend.kill("SIGTERM");
    }
  } catch (err) {
    logLine(`stop backend failed: ${err.message}`);
  }
  backend = null;
  closeLog();
  setTimeout(() => { stopping = false; }, 1500);
}

function findFreePort(start) {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.unref();
    srv.on("error", () => resolve(findFreePort(start + 1)));
    srv.listen(start, "127.0.0.1", () => {
      const port = srv.address().port;
      srv.close(() => resolve(port));
    });
  });
}

async function waitForHealth(port, timeoutMs, onTick) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    if (!backend || backendSpawnError) return false; // 进程挂了 / 根本没起来，别傻等
    try {
      const res = await fetch(`http://127.0.0.1:${port}/api/health`, {
        signal: AbortSignal.timeout(2500),
      });
      if (res.ok) return true;
    } catch (_) { /* 还没起来 */ }
    if (onTick) onTick(Date.now() - startedAt);
    await new Promise((r) => setTimeout(r, 400));
  }
  return false;
}

/**
 * 取后端 API token（可能为 null）。
 *
 * 后端在「绑非 loopback」或 `MULTIAGENT_STRICT_AUTH=1` 时才要求 token，
 * 并把自动生成的 token 写到 `<home>/.multiagent/token`（600）。
 * 桌面端始终走 loopback，默认模式不需要 token；拿到就通过 preload 桥注入渲染进程，
 * 不写进 URL（避免出现在历史记录 / Referer / 日志里）。
 */
function readBackendToken() {
  const fromEnv = (process.env.MULTIAGENT_API_TOKEN || "").trim();
  if (fromEnv) return fromEnv;
  try {
    const file = path.join(HOME(), ".multiagent", "token");
    if (fs.existsSync(file)) {
      const value = fs.readFileSync(file, "utf8").trim();
      if (value) return value;
    }
  } catch (_) { /* 读不到就当没有 */ }
  return "";
}

async function apiGet(pathname) {
  if (!backendPort) throw new Error("后端尚未启动");
  const headers = { Accept: "application/json" };
  const token = readBackendToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(`http://127.0.0.1:${backendPort}${pathname}`, {
    headers,
    signal: AbortSignal.timeout(4000),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

/* ------------------------------------------------------------ 窗口 / 状态 */

function stateFile() {
  return path.join(app.getPath("userData"), "window-state.json");
}

function loadWindowState() {
  const primary = screen.getPrimaryDisplay().workArea;
  const fallback = {
    width: Math.min(1480, Math.max(MIN_WIDTH, primary.width)),
    height: Math.min(940, Math.max(MIN_HEIGHT, primary.height)),
    x: undefined,
    y: undefined,
    maximized: false,
  };
  let raw;
  try {
    raw = JSON.parse(fs.readFileSync(stateFile(), "utf8"));
  } catch (_) {
    return fallback;
  }
  if (!raw || typeof raw !== "object") return fallback;
  const state = {
    width: Math.max(MIN_WIDTH, Math.round(Number(raw.width) || fallback.width)),
    height: Math.max(MIN_HEIGHT, Math.round(Number(raw.height) || fallback.height)),
    x: Number.isFinite(Number(raw.x)) ? Math.round(Number(raw.x)) : undefined,
    y: Number.isFinite(Number(raw.y)) ? Math.round(Number(raw.y)) : undefined,
    maximized: Boolean(raw.maximized),
  };
  clampToDisplays(state);
  return state;
}

/**
 * 外接屏拔掉 / 分辨率变化后旧坐标可能落在屏幕外：
 * 只有至少 120×80 落在某个显示器工作区内才采用，否则居中到主显示器。
 */
function clampToDisplays(state) {
  const primary = screen.getPrimaryDisplay().workArea;
  state.width = Math.min(state.width, Math.max(MIN_WIDTH, primary.width));
  state.height = Math.min(state.height, Math.max(MIN_HEIGHT, primary.height));
  if (state.x === undefined || state.y === undefined) return;

  const visible = screen.getAllDisplays().some((display) => {
    const area = display.workArea;
    const overlapX = Math.max(0, Math.min(state.x + state.width, area.x + area.width) - Math.max(state.x, area.x));
    const overlapY = Math.max(0, Math.min(state.y + state.height, area.y + area.height) - Math.max(state.y, area.y));
    return overlapX >= 120 && overlapY >= 80;
  });
  if (visible) return;

  state.x = Math.round(primary.x + Math.max(0, (primary.width - state.width) / 2));
  state.y = Math.round(primary.y + Math.max(0, (primary.height - state.height) / 2));
}

function saveWindowState() {
  if (!win || win.isDestroyed()) return;
  try {
    const maximized = win.isMaximized();
    const bounds = maximized ? win.getNormalBounds() : win.getBounds();
    const payload = {
      x: bounds.x,
      y: bounds.y,
      width: bounds.width,
      height: bounds.height,
      maximized,
      savedAt: new Date().toISOString(),
    };
    fs.mkdirSync(path.dirname(stateFile()), { recursive: true });
    fs.writeFileSync(stateFile(), JSON.stringify(payload, null, 2), "utf8");
  } catch (_) { /* 记不住窗口位置不影响使用 */ }
}

function scheduleSaveWindowState() {
  if (saveStateTimer) clearTimeout(saveStateTimer);
  saveStateTimer = setTimeout(() => {
    saveStateTimer = null;
    saveWindowState();
  }, 400);
}

function revealWindow() {
  if (!win || win.isDestroyed() || revealed) return;
  revealed = true;
  win.show();
  win.focus();
}

/** 错误页必须让用户看见：即使窗口已显示但被最小化，也要恢复并聚焦。 */
function surfaceWindow() {
  if (!win || win.isDestroyed()) return;
  if (win.isMinimized()) win.restore();
  revealed = true;
  win.show();
  win.focus();
}

/** 启动页：错误页的文案更新（页面里定义了 window.__splash）。 */
function setSplash(payload) {
  if (!win || win.isDestroyed() || currentView !== "splash") return;
  const script = `window.__splash && window.__splash(${JSON.stringify(payload)});`;
  win.webContents.executeJavaScript(script, true).catch(() => { /* 页面还没就绪 */ });
}

function ensureSplashView() {
  if (!win || win.isDestroyed()) return Promise.resolve();
  if (currentView === "splash" && !splashLoading) return Promise.resolve();
  if (splashLoading) return splashLoading;
  splashLoading = win.loadFile(path.join(__dirname, "splash.html"))
    .then(() => { currentView = "splash"; })
    .catch(() => { /* 导航被更重要的页面打断 */ })
    .finally(() => { splashLoading = null; });
  return splashLoading;
}

async function showSplash(payload) {
  if (!win || win.isDestroyed()) return;
  await ensureSplashView();
  if (payload) setSplash(payload);
  if (splashDeadlinePassed && !ready) revealWindow();
}

/**
 * 先藏窗口启动后端：1.2s 内就绪就直接显示应用，超过 1.2s 才把启动页亮出来。
 * 这样正常启动没有闪屏，冷启动 / 首次初始化也不会白屏。
 */
function armSplashVisibility() {
  if (splashTimer) clearTimeout(splashTimer);
  splashTimer = setTimeout(() => {
    splashTimer = null;
    splashDeadlinePassed = true;
    if (!ready) revealWindow();
  }, SPLASH_DELAY_MS);
}

function baseSplashPayload() {
  return {
    version: app.getVersion(),
    port: backendPort || undefined,
    logPath: logFile || defaultLogFile(),
  };
}

/* -------------------------------------------------------------- 安全边界 */

function isHttpUrl(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch (_) {
    return false;
  }
}

function isAllowedNavigation(url) {
  if (isSplashFile(url)) return true;
  if (!backendPort) return false;
  return url.startsWith(`http://127.0.0.1:${backendPort}/`) || url === `http://127.0.0.1:${backendPort}`;
}

function isSplashFile(url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "file:") return false;
    let filePath = decodeURIComponent(parsed.pathname);
    if (process.platform === "win32") filePath = filePath.replace(/^\/([A-Za-z]:)/, "$1");
    const expected = path.resolve(__dirname, "splash.html");
    return path.resolve(filePath).toLowerCase() === expected.toLowerCase();
  } catch (_) {
    return false;
  }
}

function handleNavigation(event, url) {
  if (isAllowedNavigation(url)) return;
  event.preventDefault();
  if (isHttpUrl(url)) void shell.openExternal(url);
}

function hardenSession() {
  const ses = session.defaultSession;
  const allowed = new Set(["clipboard-sanitized-write"]);
  ses.setPermissionRequestHandler((_webContents, permission, callback) => {
    callback(allowed.has(permission));
  });
  ses.setPermissionCheckHandler((_webContents, permission) => allowed.has(permission));
  ses.setSpellCheckerEnabled(false);
}

function blockDevToolsShortcut(event, input) {
  if (DEVTOOLS_ALLOWED) return;
  if (input.type !== "keyDown") return;
  const key = String(input.key || "").toLowerCase();
  const inspect = input.control && input.shift && ["i", "j", "c"].includes(key);
  if (key === "f12" || inspect) event.preventDefault();
}

/* ------------------------------------------------------------ 主窗口创建 */

function createWindow() {
  const state = loadWindowState();
  win = new BrowserWindow({
    width: state.width,
    height: state.height,
    x: state.x,
    y: state.y,
    minWidth: MIN_WIDTH,
    minHeight: MIN_HEIGHT,
    show: false,
    backgroundColor: "#f2f0ea",
    title: APP_TITLE,
    icon: windowIcon(),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
      webviewTag: false,
      allowRunningInsecureContent: false,
      navigateOnDragDrop: false,
    },
  });

  if (state.maximized) win.maximize();

  win.once("ready-to-show", () => {
    if (ready || splashDeadlinePassed) revealWindow();
  });
  win.on("resize", scheduleSaveWindowState);
  win.on("move", scheduleSaveWindowState);
  win.on("maximize", scheduleSaveWindowState);
  win.on("unmaximize", scheduleSaveWindowState);
  win.on("close", () => {
    saveWindowState();
  });
  win.on("closed", () => {
    win = null;
  });

  // 外链走系统浏览器；窗口内只允许本地后端与启动页
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (isHttpUrl(url)) void shell.openExternal(url);
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", handleNavigation);
  win.webContents.on("will-redirect", handleNavigation);
  win.webContents.on("will-attach-webview", (event) => event.preventDefault());
  win.webContents.on("before-input-event", blockDevToolsShortcut);

  win.webContents.on("did-fail-load", (_event, code, description, validatedURL, isMainFrame) => {
    if (!isMainFrame || quitting || starting) return;
    if (!validatedURL || !validatedURL.startsWith(`http://127.0.0.1:${backendPort}`)) return;
    if (code === -3) return; // ERR_ABORTED：通常是主动导航打断，不当作故障
    void showFailurePage(`界面加载失败（${code}）：${description}\n${validatedURL}`, "界面加载失败");
  });

  win.webContents.on("render-process-gone", async (_event, details) => {
    if (quitting || rendererRecovery || !win || win.isDestroyed()) return;
    rendererRecovery = true;
    ready = false;
    const reason = details && details.reason ? details.reason : "unknown";
    try {
      const result = await showMessageBox({
        type: "error",
        title: "界面进程已退出",
        message: "MultiAgent 界面意外崩溃",
        detail: `原因：${reason}${details && details.exitCode != null ? `（exit ${details.exitCode}）` : ""}\n可以重新加载界面，或退出应用。`,
        buttons: ["重新加载", "退出"],
        defaultId: 0,
        cancelId: 1,
        noLink: true,
      });
      if (result.response === 0) {
        await recoverRenderer();
      } else {
        requestQuit();
      }
    } catch (_) {
      await recoverRenderer();
    } finally {
      rendererRecovery = false;
    }
  });

  armSplashVisibility();
  return win;
}

async function recoverRenderer() {
  if (!win || win.isDestroyed()) return;
  if (!backend || !backendPort) {
    await bootBackend();
    return;
  }
  try {
    currentView = "app";
    await win.loadURL(`http://127.0.0.1:${backendPort}/?desktop=1`);
    ready = true;
    revealWindow();
    flushMenuActions();
  } catch (_) {
    await restartBackend();
  }
}

/* ---------------------------------------------------------------- 启动流程 */

async function loadMainUi() {
  if (!win || win.isDestroyed()) return;
  currentView = "app";
  await win.loadURL(`http://127.0.0.1:${backendPort}/?desktop=1`);
  ready = true;
  surfaceWindow();
  flushMenuActions();
}

async function bootBackend() {
  if (starting) return;
  starting = true;
  ready = false;
  try {
    await showSplash({ stage: "boot", status: "正在准备桌面环境…", percent: 4, ...baseSplashPayload() });
    setSplash({ status: "正在寻找可用端口…", percent: 6, ...baseSplashPayload() });
    backendPort = await findFreePort(MIN_PORT);
    setSplash({ status: "正在启动本地后端…", percent: 16, port: backendPort, ...baseSplashPayload() });

    startBackend(backendPort);
    const ok = await waitForHealth(backendPort, HEALTH_TIMEOUT_MS, (elapsed) => {
      const percent = Math.min(90, 20 + Math.round((elapsed / HEALTH_TIMEOUT_MS) * 70));
      const seconds = Math.round(elapsed / 1000);
      setSplash({
        status: `正在等待后端就绪（${seconds}s）…`,
        percent,
        port: backendPort,
        detail: "首次启动需要初始化数据库与工作区，通常 2–10 秒。",
      });
    });

    if (!ok) {
      const detail = backendSpawnError
        ? `无法启动后端进程：${backendSpawnError}\n命令：${backendCommand}`
        : backend
          ? `等待 127.0.0.1:${backendPort} 就绪超时（${HEALTH_TIMEOUT_MS / 1000}s）。`
          : "后端进程启动失败或启动后立即退出。";
      await failStartup(detail);
      return;
    }

    setSplash({ status: "正在加载指挥台…", percent: 94, port: backendPort });
    await loadMainUi();
  } catch (err) {
    await failStartup(`启动过程异常：${err && err.message ? err.message : String(err)}`);
  } finally {
    starting = false;
  }
}

async function failStartup(detail) {
  starting = false;
  ready = false;
  await showSplash({
    stage: "error",
    title: "后端启动失败",
    detail,
    ...baseSplashPayload(),
  });
  surfaceWindow();
}

/** 后端在运行中挂掉 / 界面加载失败：回到可重试错误页（保留窗口，不让用户对着白屏）。 */
async function showFailurePage(detail, title = "后端连接已断开") {
  ready = false;
  if (!win || win.isDestroyed()) {
    dialog.showErrorBox(APP_TITLE, detail);
    return;
  }
  await showSplash({
    stage: "error",
    title,
    detail,
    ...baseSplashPayload(),
  });
  surfaceWindow();
}

async function restartBackend() {
  if (starting) return;
  stopBackend();
  backendPort = 0;
  await bootBackend();
}

/* ------------------------------------------------------------------- 菜单 */

function sendMenuAction(action) {
  if (!win || win.isDestroyed()) return;
  if (!ready) {
    pendingMenuActions.push(action);
    return;
  }
  win.webContents.send("menu:action", action);
}

function flushMenuActions() {
  if (!ready || !win || win.isDestroyed()) return;
  const pending = pendingMenuActions.splice(0);
  for (const action of pending) {
    win.webContents.send("menu:action", action);
  }
}

function reloadUi() {
  if (!win || win.isDestroyed()) return;
  if (starting) return;
  if (backend && backendPort) {
    if (!ready) {
      loadMainUi().catch(() => bootBackend());
      return;
    }
    win.webContents.reload();
    return;
  }
  void restartBackend();
}

function toggleDevTools() {
  if (!DEVTOOLS_ALLOWED || !win || win.isDestroyed()) return;
  win.webContents.toggleDevTools();
}

function requestQuit() {
  quitting = true;
  stopBackend();
  app.quit();
}

async function openDataDir() {
  fs.mkdirSync(dataDir(), { recursive: true });
  return shell.openPath(dataDir());
}

async function openLogs() {
  fs.mkdirSync(logsDir(), { recursive: true });
  if (logFile && fs.existsSync(logFile)) {
    shell.showItemInFolder(logFile);
    return "";
  }
  return shell.openPath(logsDir());
}

async function openProjectFolder() {
  let target = lastProjectRoot;
  if (!target || !fs.existsSync(target)) {
    try {
      const projects = await apiGet("/api/projects");
      const list = Array.isArray(projects) ? projects : [];
      const found = list.find((item) => item && typeof item.root_path === "string" && item.root_path.trim());
      if (found) target = found.root_path;
    } catch (_) { /* 后端没起来时退回目录选择器 */ }
  }
  if (!target) {
    const result = await showOpenDialog({
      title: "选择项目文件夹",
      properties: ["openDirectory"],
    });
    if (result.canceled || !result.filePaths.length) return;
    target = result.filePaths[0];
  }
  lastProjectRoot = target;
  const message = await shell.openPath(target);
  if (message) {
    await showMessageBox({
      type: "warning",
      title: "无法打开项目文件夹",
      message: target,
      detail: message,
      buttons: ["确定"],
    });
  }
}

function showOpenDialog(options) {
  if (win && !win.isDestroyed()) return dialog.showOpenDialog(win, options);
  return dialog.showOpenDialog(options);
}

function showMessageBox(options) {
  if (win && !win.isDestroyed()) return dialog.showMessageBox(win, options);
  return dialog.showMessageBox(options);
}

function showAbout() {
  showMessageBox({
    type: "info",
    title: "关于 MultiAgent",
    message: "MultiAgent 工作台",
    detail: [
      `版本 ${app.getVersion()} · Electron ${process.versions.electron} · Node ${process.versions.node}`,
      `后端端口 ${backendPort || "未启动"}`,
      `数据目录 ${dataDir()}`,
      `日志 ${logFile || defaultLogFile()}`,
    ].join("\n"),
    buttons: ["确定"],
  });
}

function showDiagnostics() {
  showMessageBox({
    type: "info",
    title: "诊断信息",
    message: "MultiAgent 运行诊断",
    detail: [
      `版本 ${app.getVersion()} · Electron ${process.versions.electron} · Chromium ${process.versions.chrome}`,
      `平台 ${process.platform} ${process.arch}`,
      `后端 ${backend ? "运行中" : "未运行"}${backendPort ? ` · 127.0.0.1:${backendPort}` : ""}`,
      `工作目录 ${HOME()}`,
      `数据目录 ${dataDir()}`,
      `日志 ${logFile || defaultLogFile()}`,
      backendCommand ? `命令 ${backendCommand}` : "",
    ].filter(Boolean).join("\n"),
    buttons: ["确定"],
  });
}

function buildMenu() {
  const isMac = process.platform === "darwin";
  const template = [
    ...(isMac
      ? [{
          label: app.name,
          submenu: [
            { role: "about", label: "关于 MultiAgent" },
            { type: "separator" },
            { role: "hide", label: "隐藏 MultiAgent" },
            { role: "hideOthers", label: "隐藏其他" },
            { role: "unhide", label: "全部显示" },
            { type: "separator" },
            { role: "quit", label: "退出 MultiAgent" },
          ],
        }]
      : []),
    {
      label: "文件",
      submenu: [
        { label: "新建任务", accelerator: "CmdOrCtrl+N", click: () => sendMenuAction("new-task") },
        { label: "打开项目文件夹", accelerator: "CmdOrCtrl+Shift+O", click: () => void openProjectFolder() },
        { type: "separator" },
        { label: "打开数据目录", click: () => void openDataDir() },
        { label: "打开日志", accelerator: "CmdOrCtrl+Shift+L", click: () => void openLogs() },
        { type: "separator" },
        { label: "刷新界面", accelerator: "F5", click: () => reloadUi() },
        { label: "重新加载", accelerator: "CmdOrCtrl+R", click: () => reloadUi() },
        { label: "重试启动后端", click: () => void restartBackend() },
        { type: "separator" },
        { label: "退出", accelerator: isMac ? "Cmd+Q" : "Alt+F4", click: () => requestQuit() },
      ],
    },
    {
      label: "编辑",
      submenu: [
        { label: "撤销", role: "undo" },
        { label: "重做", role: "redo" },
        { type: "separator" },
        { label: "剪切", role: "cut" },
        { label: "复制", role: "copy" },
        { label: "粘贴", role: "paste" },
        { label: "删除", role: "delete" },
        { type: "separator" },
        { label: "全选", role: "selectAll" },
      ],
    },
    {
      label: "视图",
      submenu: [
        { label: "实际大小", role: "resetZoom" },
        { label: "放大", role: "zoomIn" },
        { label: "缩小", role: "zoomOut" },
        { type: "separator" },
        { label: "全屏", role: "togglefullscreen" },
        ...(DEVTOOLS_ALLOWED
          ? [
              { type: "separator" },
              { label: "开发者工具", accelerator: "F12", click: () => toggleDevTools() },
            ]
          : []),
      ],
    },
    {
      label: "窗口",
      submenu: [
        { label: "最小化", role: "minimize" },
        ...(isMac ? [{ label: "缩放", role: "zoom" }] : []),
        { label: "关闭窗口", role: "close" },
        ...(isMac
          ? [
              { type: "separator" },
              { label: "前置全部窗口", role: "front" },
            ]
          : []),
      ],
    },
    {
      label: "帮助",
      submenu: [
        { label: "关于 MultiAgent", click: () => showAbout() },
        { type: "separator" },
        { label: "打开数据目录", click: () => void openDataDir() },
        { label: "打开日志", click: () => void openLogs() },
        { label: "诊断信息", click: () => showDiagnostics() },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

/* ------------------------------------------------------------------- IPC */

function registerIpc() {
  // preload 在页面脚本之前同步取 token：保证 token 不经过 URL
  ipcMain.on("shell:api-token", (event) => {
    event.returnValue = readBackendToken();
  });

  ipcMain.handle("shell:info", () => ({
    version: process.versions.electron,
    appVersion: app.getVersion(),
    packaged: app.isPackaged,
    platform: process.platform,
    arch: process.arch,
    versions: {
      electron: process.versions.electron,
      chrome: process.versions.chrome,
      node: process.versions.node,
      v8: process.versions.v8,
    },
    home: HOME(),
    dataDir: dataDir(),
    logsDir: logsDir(),
    logFile: logFile || defaultLogFile(),
    port: backendPort,
    ready,
  }));

  ipcMain.handle("shell:pick-directory", async () => {
    const result = await dialog.showOpenDialog(win, {
      title: "选择工作区目录",
      properties: ["openDirectory", "createDirectory"],
    });
    if (result.canceled || !result.filePaths.length) return null;
    return result.filePaths[0];
  });

  ipcMain.handle("shell:open-path", (_event, target) => {
    if (typeof target !== "string" || !target.trim() || target.includes("\0")) return "无效路径";
    return shell.openPath(path.resolve(target.trim()));
  });

  ipcMain.handle("shell:open-data-dir", () => openDataDir());
  ipcMain.handle("shell:open-logs", () => openLogs());

  ipcMain.handle("shell:retry-backend", () => {
    void restartBackend();
    return true;
  });

  ipcMain.handle("shell:quit", () => {
    requestQuit();
    return true;
  });

  ipcMain.handle("shell:notify", (_event, title, body) => {
    if (!Notification.isSupported()) return false;
    const notification = new Notification({
      title: String(title || APP_TITLE).slice(0, 120),
      body: String(body || "").slice(0, 500),
      icon: windowIcon(),
    });
    notification.on("click", () => {
      if (!win || win.isDestroyed()) return;
      if (win.isMinimized()) win.restore();
      win.show();
      win.focus();
    });
    notification.show();
    return true;
  });

  ipcMain.handle("shell:set-badge", (_event, value) => {
    const count = Math.max(0, Math.min(999, Math.floor(Number(value) || 0)));
    if (process.platform === "win32") {
      if (!win || win.isDestroyed()) return false;
      if (!count) {
        win.setOverlayIcon(null, "");
        return true;
      }
      if (!badgeImage) {
        try {
          const badgePath = path.join(assetsDir(), "badge.png");
          badgeImage = fs.existsSync(badgePath) ? nativeImage.createFromPath(badgePath) : nativeImage.createEmpty();
        } catch (_) {
          badgeImage = nativeImage.createEmpty();
        }
      }
      if (badgeImage.isEmpty()) return false;
      win.setOverlayIcon(badgeImage, `${count} 个待处理事项`);
      return true;
    }
    if (typeof app.setBadgeCount === "function") return app.setBadgeCount(count);
    return false;
  });

  ipcMain.handle("shell:open-external", (_event, url) => {
    if (typeof url !== "string" || !isHttpUrl(url)) return false;
    void shell.openExternal(url);
    return true;
  });

  // 渲染进程切换项目时同步给桌面壳，菜单「打开项目文件夹」才能对准当前项目
  ipcMain.handle("shell:set-project-root", (_event, root) => {
    if (typeof root !== "string" || !root.trim()) return false;
    try {
      const resolved = path.resolve(root.trim());
      if (!fs.existsSync(resolved)) return false;
      lastProjectRoot = resolved;
      return true;
    } catch (_) {
      return false;
    }
  });
}

/* --------------------------------------------------------------- 生命周期 */

async function bootstrap() {
  app.setAppUserModelId(APP_ID);
  hardenSession();
  registerIpc();
  buildMenu();
  createWindow();
  await bootBackend();
}

// 单实例锁：第二次启动只把已有窗口提到前台，避免两份后端抢同一个数据库
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!app.isReady()) return;
    if (!win || win.isDestroyed()) {
      createWindow();
      void bootBackend();
      return;
    }
    if (win.isMinimized()) win.restore();
    revealWindow();
    win.focus();
  });

  app.on("web-contents-created", (_event, contents) => {
    contents.on("will-attach-webview", (event) => event.preventDefault());
  });

  app.whenReady()
    .then(bootstrap)
    .catch((err) => {
      dialog.showErrorBox("启动失败", String((err && err.stack) || err));
      app.quit();
    });

  app.on("activate", () => {
    const existing = BrowserWindow.getAllWindows()[0];
    if (existing) {
      if (existing.isMinimized()) existing.restore();
      existing.show();
      existing.focus();
      return;
    }
    createWindow();
    if (ready && backendPort) {
      loadMainUi().catch(() => bootBackend());
    } else {
      void bootBackend();
    }
  });

  app.on("before-quit", () => {
    quitting = true;
    stopBackend();
  });

  app.on("window-all-closed", () => {
    stopBackend();
    ready = false;
    if (process.platform !== "darwin") {
      quitting = true;
      app.quit();
    }
  });
}
