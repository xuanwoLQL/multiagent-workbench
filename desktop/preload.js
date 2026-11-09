"use strict";

/**
 * 只向渲染进程暴露最小、白名单化的原生能力，不开启 Node 集成。
 *
 * 约定：
 *   - 全部是白名单方法，参数只接受字符串 / 数字，不透传任意通道；
 *   - apiToken 在页面脚本执行前同步取一次，渲染进程直接读
 *     `window.multiagentDesktop.apiToken`，token 因此不进 URL、不进历史记录；
 *   - 页面拿不到 Electron 的 ipcRenderer / require / process 全量对象；
 *   - 菜单事件通过 onMenuAction 订阅，返回取消订阅函数。
 */
const { contextBridge, ipcRenderer } = require("electron");

function readApiToken() {
  try {
    return String(ipcRenderer.sendSync("shell:api-token") || "");
  } catch (_) {
    return "";
  }
}

function call(channel, ...args) {
  return ipcRenderer.invoke(channel, ...args);
}

contextBridge.exposeInMainWorld("multiagentDesktop", {
  isDesktop: true,
  platform: process.platform,
  versions: Object.freeze({
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
    v8: process.versions.v8,
  }),
  apiToken: readApiToken(),

  info: () => call("shell:info"),
  pickDirectory: () => call("shell:pick-directory"),
  openPath: (target) => call("shell:open-path", String(target)),
  openDataDir: () => call("shell:open-data-dir"),
  openLogs: () => call("shell:open-logs"),
  retryBackend: () => call("shell:retry-backend"),
  quit: () => call("shell:quit"),

  openExternal: (url) => call("shell:open-external", String(url || "")),
  showNotification: (title, body) => call("shell:notify", String(title || ""), String(body || "")),
  setBadgeCount: (count) => call("shell:set-badge", Number(count) || 0),
  setProjectRoot: (root) => call("shell:set-project-root", String(root || "")),

  onMenuAction: (callback) => {
    if (typeof callback !== "function") return () => {};
    const handler = (_event, action) => {
      try {
        callback(String(action || ""));
      } catch (_) { /* 渲染进程回调异常不影响主进程 */ }
    };
    ipcRenderer.on("menu:action", handler);
    return () => ipcRenderer.removeListener("menu:action", handler);
  },
});
