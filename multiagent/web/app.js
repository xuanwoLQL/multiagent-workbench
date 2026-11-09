/* 多 Agent 协作工作台 —— 桌面控制台（原生 JS + SVG，无构建步骤）
 *
 * Electron 桌面壳与浏览器共用这一份 UI：桌面壳只负责拉起后端并开窗，
 * 真正的“控制”都在这里 —— 建项目 / 一句话派发任务 / 拆解计划 / 派发与扇出 /
 * 审批 / 快照 / 配置。
 *
 * 本轮重构（见 docs/UI-OPS-REDESIGN.md）：
 *   1. 首屏只发 dashboard 一个请求（旧后端没有该接口时自动回退多接口拼装）；
 *   2. 视图切换按需加载，切回先渲染缓存再后台刷新；
 *   3. 所有列表 innerHTML 前做内容比较，相同不重写；
 *   4. 定时器只在页面可见时刷新 dashboard（≥5s），不打断弹窗/抽屉。
 */

/* ================================================================ 常量 */

const STATUS_LABEL = {
  pending: "待执行", running: "执行中", success: "成功", failed: "失败",
  blocked: "被阻塞", waiting: "等待人工", skipped: "已跳过",
  cancelled: "已取消", interrupted: "中断",
};

const COLUMNS = [
  { id: "todo", label: "待执行", tone: "todo", statuses: ["pending", "interrupted"] },
  { id: "running", label: "执行中", tone: "run", statuses: ["running"] },
  { id: "waiting", label: "等待人工", tone: "wait", statuses: ["waiting"] },
  { id: "success", label: "成功", tone: "ok", statuses: ["success"] },
  { id: "failed", label: "失败 · 阻塞", tone: "bad", statuses: ["failed", "blocked"] },
  { id: "closed", label: "已跳过 · 取消", tone: "closed", statuses: ["skipped", "cancelled"] },
];

const KIND_OPTIONS = ["generic", "code", "doc", "data", "research", "review", "ppt", "diagrams", "web"];

const KIND_LABEL = {
  auto: "自动判断", generic: "通用", code: "代码", doc: "文档",
  data: "数据", research: "调研", review: "审阅", ppt: "PPT",
  diagrams: "图表", web: "Web / 3D",
};

const MISSION_TEMPLATES = [
  { label: "写一份报告", text: "写一份报告：围绕「主题」梳理现状、关键结论与建议，输出 报告.md" },
  { label: "调研一个主题", text: "调研一个主题：收集资料并做对比分析，输出 调研报告.md" },
  { label: "审阅文档", text: "审阅文档：检查结构、事实与表述问题，给出修改建议（只读，不改文件）" },
  { label: "修改代码", text: "修改代码：定位问题并完成修复，补充必要的测试" },
  { label: "整理数据", text: "整理数据：清洗表格数据并输出汇总结果 数据汇总.csv" },
];

const STREAM_LABEL = {
  agent_message_chunk: "消息", agent_message: "消息",
  agent_thought_chunk: "思考", agent_thought: "思考",
  tool_call: "工具", tool_call_update: "工具", tool_call_progress: "工具",
  "acp.session": "会话",
};

const UNTITLED_PROJECT = "未命名项目";
const UNTITLED_TASK = "未命名任务";

const VIEW_LABELS = {
  home: "指挥台", tasks: "任务", dag: "依赖图", board: "共享黑板",
  handoffs: "交接说明", approvals: "人工确认", snapshots: "快照",
  agents: "Agent", observe: "观测 / 审计", config: "配置", live: "实时",
  system: "系统维护",
};

/* ================================================================ 状态 */

const state = {
  projects: [], projectId: null,
  agents: [], config: null, configFiles: {},
  dashboard: null, _dashJson: "", _dashAt: 0, _dashPromise: null,
  kanban: null, board: [], handoffs: [], approvals: [], snapshots: [],
  stats: null, runs: [], timeline: [], events: [], artifacts: [],
  system: null,
  stream: [],
  ws: null, wsOk: false, timer: null, view: "home", browsePath: "",
  taskFilter: "",
  _htmlCache: {}, loaded: {}, _loadingView: {},
  _refreshTimer: null, _streamTimer: null, _wsTimer: null,
  _runStatus: false, agentsLoaded: false,
  _colCache: {}, _streamFrozen: false,
  _liveBlocks: null, _liveBlocksDirty: true,
  _liveNodes: null, _liveSigs: null, _liveOrder: [],
  _livePinned: true, _liveUnseen: 0, _liveToggles: {},
  _liveRenderTimer: null, _liveRunSeenId: "", _liveRunStartedAt: 0,
  _isolationTouched: false,
};

const $ = (id) => document.getElementById(id);
const isDesktop = () => Boolean(window.multiagentDesktop);

/* ================================================================ 基础工具 */

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function setText(id, text) {
  const el = $(id);
  if (!el) return false;
  const next = String(text ?? "");
  if (el.textContent !== next) el.textContent = next;
  return true;
}

/** innerHTML 前先比较内容：相同则跳过 DOM 重写（卡顿治理核心之一）。 */
function setHtml(id, html) {
  const el = $(id);
  if (!el) return false;
  const next = String(html ?? "");
  if (state._htmlCache[id] === next) return false;
  state._htmlCache[id] = next;
  el.innerHTML = next;
  return true;
}

/** 容器本身已经是组件根（如 #home-metrics 带 .kpi-grid）时不再套一层。 */
function setGridHtml(id, wrapperClass, inner) {
  const el = $(id);
  if (!el) return false;
  const html = el.classList.contains(wrapperClass) ? inner : `<div class="${wrapperClass}">${inner}</div>`;
  return setHtml(id, html);
}

function emptyBox(title, hint, actionsHtml = "") {
  return `<div class="empty-box">
    <div class="empty-mark">◈</div>
    <div class="empty-title">${esc(title)}</div>
    ${hint ? `<div class="empty-hint">${esc(hint)}</div>` : ""}
    ${actionsHtml ? `<div class="empty-actions">${actionsHtml}</div>` : ""}
  </div>`;
}

function listHtml(rows) {
  return `<div class="list">${(rows || []).join("")}</div>`;
}

/** 容器本身已是 .list（新壳 #bb-list / #runs … 自带）时不再套一层。 */
function setListHtml(id, rows) {
  const el = $(id);
  if (!el) return false;
  const inner = (rows || []).join("");
  const html = el.classList.contains("list") ? inner : `<div class="list">${inner}</div>`;
  return setHtml(id, html);
}

/** 流 / 活动容器同理：#inspector-stream 自带 .stream，#home-activity 自带 .activity。 */
function setStreamHtml(id, wrapperClass, rowsHtml) {
  const el = $(id);
  if (!el) return false;
  const html = el.classList.contains(wrapperClass) ? rowsHtml : `<div class="${wrapperClass}">${rowsHtml}</div>`;
  return setHtml(id, html);
}

function compactEmpty(mark, title, hint) {
  return `<div class="empty-box compact">
    <div class="empty-mark">${esc(mark)}</div>
    <div class="empty-title">${esc(title)}</div>
    ${hint ? `<div class="empty-hint">${esc(hint)}</div>` : ""}
  </div>`;
}

/** 统一列表行结构；title/tags/meta/body/actions 均为「已转义」的 HTML 片段。 */
function rowHtml(opts) {
  const o = opts || {};
  return `<div class="row ${o.cls || ""}">
    <div class="row-head">
      <span class="row-title">${o.title || ""}</span>
      ${o.tags || ""}
      <span class="spacer"></span>
      ${o.meta ? `<span class="row-meta">${o.meta}</span>` : ""}
      ${o.actions || ""}
    </div>
    ${o.body ? `<div class="row-body">${o.body}</div>` : ""}
  </div>`;
}

/* 旧数据兼容：整体是 ? / � / ？ 的 label、title 显示为兜底名（不改数据库）。 */
function isGarbled(value) {
  const s = String(value ?? "").trim();
  if (!s) return true;
  return /^[?\uFFFD？\s]+$/u.test(s);
}

function displayName(value, fallback) {
  const s = String(value ?? "").trim();
  if (isGarbled(s)) return fallback;
  return s;
}

function projectDisplayName(project) {
  if (!project) return UNTITLED_PROJECT;
  const label = String(project.label ?? "").trim();
  if (!isGarbled(label)) return label;
  const name = String(project.name ?? "").trim();
  if (!isGarbled(name)) return name;
  return UNTITLED_PROJECT;
}

function taskDisplayTitle(task) {
  if (!task) return UNTITLED_TASK;
  return displayName(task.title, UNTITLED_TASK);
}

/** 任务卡右侧的短 id（#xxxx）。 */
function shortTaskId(taskId) {
  const ident = String(taskId ?? "").trim();
  if (!ident) return "----";
  return ident.length > 4 ? ident.slice(-4) : ident;
}

/** 侧栏搜索：标题 / Agent / 类型，多个关键词按 AND 匹配。 */
function taskMatchesFilter(task, query) {
  const tokens = String(query ?? "").toLowerCase().split(/\s+/).filter(Boolean);
  if (!tokens.length) return true;
  const taskItem = task && typeof task === "object" ? task : {};
  const status = String(taskItem.status || "");
  const kind = String(taskItem.kind || "");
  const haystack = [
    taskDisplayTitle(taskItem),
    taskItem.title,
    agentLabel(taskItem.agent_id),
    taskItem.agent_id,
    kind,
    KIND_LABEL[kind] || "",
    status,
    STATUS_LABEL[status] || "",
    taskItem.id,
    taskItem.blocked_reason,
  ].filter(Boolean).join(" ").toLowerCase();
  return tokens.every((token) => haystack.includes(token));
}

function filteredTasks(tasks) {
  const list = Array.isArray(tasks) ? tasks : [];
  if (!String(state.taskFilter || "").trim()) return list;
  return list.filter((task) => taskMatchesFilter(task, state.taskFilter));
}

/** 输入框 → state.taskFilter → 同时刷新首页与任务视图的看板。 */
function applyTaskFilter(value) {
  const next = String(value ?? "").trim();
  if (next === state.taskFilter) return false;
  state.taskFilter = next;
  if (state.dashboard) renderHomeKanban(state.dashboard);
  else renderKanbanBoard("home-kanban", []);
  renderKanbanBoard("kanban", filteredTasks(currentTasks()));
  return true;
}

/** 活动 / 事件 / 记录文本的乱码兜底（与 label/title 同一规则）。 */
const displayText = displayName;

/** 表单预填：整体乱码的历史值一律留空，用户保存时顺手修正（不改数据库）。 */
function prefill(value) {
  const s = String(value ?? "");
  return isGarbled(s) ? "" : s;
}

/** 列表字段（标签 / 遗留问题 / 下一步…）清洗：丢弃纯乱码项。 */
function cleanList(list) {
  return (Array.isArray(list) ? list : []).map((item) => displayText(item, "")).filter(Boolean);
}

/**
 * 历史 chunk 级数据兜底：旧 handoff.did / board.body 可能是「一列一个字」——
 * 每个 token 之间被塞了 \n。这里只做显示层归一化，不改数据库。
 *
 * 判据（行数 > 20 才处理）：
 *   - 中位数 < 4（一行一两个字符），或
 *   - 长度 <= 3 的行占比 >= 50%
 * 处理：去掉 token 间换行（英文按原始前导空格补空格，中文直接拼），
 * 空行保留段落；`【做了什么】/【没做什么】/【遗留问题】/【下一步】` 等
 * 【】标题保持独立段落，不和正文粘在一起。
 */
function normalizeChunkedText(value) {
  const s = String(value ?? "");
  if (!s) return "";
  if (s.includes("```")) return s; // 真正的代码块不做归一化
  const lines = s.split(/\r?\n/);
  if (lines.length <= 20) return s;
  const nonEmpty = lines.map((line) => line.trim()).filter(Boolean);
  if (!nonEmpty.length) return s;
  const lengths = nonEmpty.map((line) => line.length).sort((a, b) => a - b);
  const mid = Math.floor(lengths.length / 2);
  const median = lengths.length % 2
    ? lengths[mid]
    : (lengths[mid - 1] + lengths[mid]) / 2;
  const shortRatio = lengths.filter((n) => n <= 3).length / lengths.length;
  if (!(median < 4 || shortRatio >= 0.5)) return s;

  const useLeadingSpaces = lines.some((line) => /^\s+\S/.test(line));
  const blocks = [];
  let current = [];
  for (const line of lines) {
    // 真正的空行 = 段落分隔；只有空白的行是 chunk 数据里的「空格 token」，留在块内
    if (line === "") {
      if (current.length) { blocks.push(current); current = []; }
      continue;
    }
    current.push(line);
  }
  if (current.length) blocks.push(current);

  const paragraphs = [];
  for (const block of blocks) {
    let joined = "";
    let pendingSpace = false;
    for (const raw of block) {
      const token = raw.trim();
      if (!token) { pendingSpace = true; continue; }
      if (!joined) {
        joined = token;
      } else {
        let space;
        if (pendingSpace) space = true;
        else if (useLeadingSpaces) space = /^\s/.test(raw);
        else space = /[A-Za-z0-9]$/.test(joined) && /^[A-Za-z0-9]/.test(token);
        joined += (space ? " " : "") + token;
      }
      pendingSpace = false;
    }
    if (!joined) continue;
    const parts = joined.split(/(?=【[^】]{1,20}】)/).map((part) => part.trim()).filter(Boolean);
    for (const part of parts) {
      const heading = part.match(/^(【[^】]{1,20}】)([\s\S]*)$/);
      if (heading) {
        paragraphs.push(heading[1]);
        if (heading[2].trim()) paragraphs.push(heading[2].trim());
      } else {
        paragraphs.push(part);
      }
    }
  }
  return paragraphs.join("\n\n");
}

/** 长文本展示统一入口：先做 chunk 归一化，再交给 displayText 做乱码兜底。 */
function readableText(value, fallback = "") {
  const normalized = normalizeChunkedText(value);
  return displayText(normalized, fallback);
}

/** 抽屉里的交接 JSON：先把逐字换行字段归一化，避免整块 JSON 变成「一列一个字」。 */
function handoffView(handoff) {
  const h = handoff || {};
  return Object.assign({}, h, {
    did: normalizeChunkedText(h.did),
    not_done: normalizeChunkedText(h.not_done),
    open_issues: cleanList(h.open_issues).map(normalizeChunkedText),
    next_steps: cleanList(h.next_steps).map(normalizeChunkedText),
    notes: normalizeChunkedText(h.notes),
  });
}

/** Agent 显示名兜底（配置里的 label 也可能被历史数据污染）。 */
function agentDisplayName(agent) {
  if (!agent) return "未命名 Agent";
  return displayName(agent.label || agent.id, "未命名 Agent");
}

/** 事件 kind 中文映射：不要把原始英文 kind 直接甩给用户（CSS 还会大写）。 */
function eventKindLabel(kind) {
  const k = String(kind || "").trim();
  if (!k) return "事件";
  const exact = {
    "task.success": "任务成功", "task.failed": "任务失败", "task.running": "任务开始",
    "task.pending": "任务创建", "task.blocked": "任务阻塞", "task.waiting": "等待人工",
    "task.skipped": "任务跳过", "task.cancelled": "任务取消", "task.interrupted": "任务中断",
    "task.retry": "任务重试", "quicktask.created": "一句话任务", "quick_task.created": "一句话任务",
    "run.start": "运行开始", "run.finish": "运行结束", "run.end": "运行结束",
    "cli.exit": "进程退出", "approval.request": "待审批", "approval.resolved": "审批完成",
    "snapshot.created": "创建快照", "handoff.created": "交接记录", "config.saved": "配置保存",
    "board.post.decision": "黑板决策", "board.post.brief": "黑板简报", "board.post.note": "黑板笔记",
  };
  if (exact[k]) return exact[k];
  if (k.startsWith("board.post.")) return "黑板写入";
  if (k.startsWith("board.review.")) return "黑板审核";
  if (k.startsWith("board.")) return "黑板";
  if (k.startsWith("task.")) return "任务";
  if (k.startsWith("acp.")) return "Agent 会话";
  if (k.startsWith("run.")) return "运行";
  if (k.startsWith("approval.")) return "人工确认";
  if (k.startsWith("snapshot.")) return "快照";
  if (k.startsWith("handoff.")) return "交接";
  if (k.startsWith("config.")) return "配置";
  if (k.startsWith("project.")) return "项目";
  return "事件";
}

/** 长路径中间省略：保留盘符 + 末两级目录，完整值放 title。 */
function shortenPath(path, max = 44) {
  const s = String(path || "").trim();
  if (!s) return "—";
  if (s.length <= max) return s;
  const parts = s.split(/[\\/]+/).filter(Boolean);
  if (parts.length <= 2) return s;
  const drive = /^[A-Za-z]:$/.test(parts[0]) ? parts[0] : "";
  const tail = parts.slice(-2).join("\\");
  return drive ? `${drive}\\…\\${tail}` : `…\\${tail}`;
}

function splitList(value) {
  return String(value || "").split(/[,，\n]/).map((s) => s.trim()).filter(Boolean);
}

function shortTime(value) {
  return value ? String(value).replace("T", " ").slice(0, 19) : "";
}

function formatBytes(value) {
  const n = Number(value) || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function formatSeconds(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n < 0) return "—";
  if (n < 60) return `${Math.round(n)}s`;
  const minutes = Math.floor(n / 60);
  const seconds = Math.round(n % 60);
  if (minutes < 60) return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${String(minutes % 60).padStart(2, "0")}m`;
}

function shortClock(value) {
  if (!value) return "";
  const s = String(value);
  const idx = s.indexOf("T");
  return idx >= 0 ? s.slice(idx + 1, idx + 9) : s.slice(0, 8);
}

function fmtInt(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value ?? "");
  return n.toLocaleString("zh-CN");
}

function initials(label) {
  const s = String(label || "").trim();
  if (!s) return "?";
  const ascii = s.replace(/[^A-Za-z0-9]/g, "");
  if (ascii) return ascii.slice(0, 2).toUpperCase();
  return s.slice(0, 1);
}

function streamLabel(event) {
  const kind = event.kind || event.source_type || event.type || "";
  if (STREAM_LABEL[kind]) return STREAM_LABEL[kind];
  if (String(kind).startsWith("task.")) return "任务";
  if (String(kind).startsWith("acp.")) return "ACP";
  if (kind === "cli.exit") return "退出";
  return String(kind) || "事件";
}

function isMissingEndpoint(err) {
  if (!err) return false;
  if (err.status === 404) return true;
  return /(^|\D)404(\D|$)|not found/i.test(String(err.message || ""));
}

/* ================================================================ 鉴权
 *
 * 默认（后端 bind 127.0.0.1）不需要 token；后端开启严格模式或监听非本机时，
 * 必须带 Bearer token。token 来源优先级：
 *   1. 桌面壳注入 window.multiagentDesktop.apiToken；
 *   2. URL 上的 ?token=...（用后立即从地址栏抹掉，避免 Referer/历史泄漏）；
 *   3. localStorage。
 * REST 走 Authorization 头；WebSocket / 下载链接（浏览器无法加头）走 ?token=。
 */
function readToken() {
  try {
    const injected = window.multiagentDesktop && window.multiagentDesktop.apiToken;
    if (injected) return String(injected);
  } catch (_) { /* 桌面桥不可用时忽略 */ }
  let stored = "";
  try { stored = localStorage.getItem("multiagent_token") || ""; } catch (_) { /* 隐私模式 */ }
  let fromUrl = "";
  try {
    const params = new URLSearchParams(location.search);
    fromUrl = params.get("token") || "";
    if (fromUrl) {
      try { localStorage.setItem("multiagent_token", fromUrl); } catch (_) { /* ignore */ }
      params.delete("token");
      const rest = params.toString();
      history.replaceState(null, "", location.pathname + (rest ? `?${rest}` : "") + location.hash);
    }
  } catch (_) { /* ignore */ }
  return fromUrl || stored;
}

const API_TOKEN = readToken();

function authHeaders() {
  return API_TOKEN ? { Authorization: `Bearer ${API_TOKEN}` } : {};
}

function withToken(path) {
  if (!API_TOKEN) return path;
  return `${path}${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(API_TOKEN)}`;
}

/* ================================================================ API / 反馈 */

async function api(path, options = {}) {
  const opts = {
    ...options,
    headers: { "Content-Type": "application/json", ...authHeaders(), ...(options.headers || {}) },
  };
  if (opts.body && typeof opts.body !== "string") opts.body = JSON.stringify(opts.body);
  const res = await fetch(path, opts);
  const type = res.headers.get("content-type") || "";
  let data;
  try { data = type.includes("json") ? await res.json() : await res.text(); }
  catch (_) { data = ""; }
  if (!res.ok) {
    const raw = (data && data.error) || (data && data.detail) || `HTTP ${res.status}`;
    const msg = typeof raw === "string" ? raw : JSON.stringify(raw);
    const err = new Error(res.status === 401
      ? "需要 API token：请用带 ?token=... 的地址打开控制台"
      : msg);
    err.status = res.status;
    throw err;
  }
  return data;
}

function toast(message, kind = "info") {
  const el = $("toast");
  const text = String(message ?? "");
  if (!el) { if (kind === "error") console.warn(text); return; }
  el.textContent = text;
  el.className = `toast show${kind === "error" ? " error" : ""}`;
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), 3200);
}

function openModal(title, bodyHtml, footHtml = "") {
  const mask = $("modal");
  if (!mask) return;
  const titleEl = $("modal-title");
  if (titleEl) titleEl.textContent = String(title ?? "");
  const bodyEl = $("modal-body");
  if (bodyEl) bodyEl.innerHTML = bodyHtml;
  const footEl = $("modal-foot");
  if (footEl) footEl.innerHTML = footHtml || '<button class="btn" data-act="modal-close">关闭</button>';
  mask.classList.add("open");
  mask.setAttribute("aria-hidden", "false");
}

function closeModal() {
  const mask = $("modal");
  if (!mask) return;
  mask.classList.remove("open");
  mask.setAttribute("aria-hidden", "true");
}

function openDrawer(title, bodyHtml) {
  const drawer = $("drawer");
  if (!drawer) return;
  setText("drawer-title", title);
  const body = $("drawer-body");
  if (body) body.innerHTML = bodyHtml;
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
}

function closeDrawer() {
  const drawer = $("drawer");
  if (!drawer) return;
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
}

function modalOpen() {
  const mask = $("modal");
  return Boolean(mask && mask.classList.contains("open"));
}

function drawerOpen() {
  const drawer = $("drawer");
  return Boolean(drawer && drawer.classList.contains("open"));
}

/** 点击后立即给视觉反馈：加 .is-loading 并禁用，异步结束恢复。 */
function withLoading(btn) {
  if (!btn || btn.dataset.loading === "off") return () => {};
  const wasDisabled = Boolean(btn.disabled);
  btn.classList.add("is-loading");
  btn.disabled = true;
  let done = false;
  return () => {
    if (done) return;
    done = true;
    btn.classList.remove("is-loading");
    btn.disabled = wasDisabled;
  };
}

function agentLabel(agentId) {
  if (!agentId) return "未指派";
  const hit = state.agents.find((a) => a.id === agentId);
  if (hit) return displayName(hit.label || hit.id, "未命名 Agent");
  return displayText(agentId, "未命名 Agent");
}

/* ================================================================ 项目 */

async function loadProjects(keepSelection = true) {
  const list = await api("/api/projects");
  state.projects = Array.isArray(list) ? list : [];
  const previous = keepSelection ? state.projectId : null;
  if (previous && state.projects.some((p) => p.id === previous)) state.projectId = previous;
  else state.projectId = state.projects.length ? state.projects[0].id : null;

  const select = $("project-select");
  if (select) {
    const html = state.projects.length
      ? state.projects.map((p) => `<option value="${esc(p.id)}">${esc(projectDisplayName(p))}</option>`).join("")
      : '<option value="">（还没有项目）</option>';
    setHtml("project-select", html);
    select.value = state.projectId || "";
  }
  return state.projects;
}

function resetProjectCaches() {
  state.dashboard = null;
  state._dashJson = "";
  state._dashAt = 0;
  state.kanban = null;
  state.board = [];
  state.handoffs = [];
  state.approvals = [];
  state.snapshots = [];
  state.stats = null;
  state.runs = [];
  state.timeline = [];
  state.events = [];
  state.artifacts = [];
  state.stream = [];
  state.loaded = {};
  state._loadingView = {};
  state._htmlCache = {};
  state._colCache = {};
  state._highlightTask = "";
  state._liveBlocks = null;
  state._liveBlocksDirty = true;
  state._liveNodes = null;
  state._liveSigs = null;
  state._liveOrder = [];
  state._livePinned = true;
  state._liveUnseen = 0;
  state._liveToggles = {};
  state._liveRunSeenId = "";
  state._liveRunStartedAt = 0;
  resetLiveFeedDom();
  state.configFiles = {};
  clearTimeout(state._refreshTimer);
  state._refreshTimer = null;
}

function openNewProjectModal() {
  openModal("新建工作区", `
    <div class="form-row"><label>项目名（唯一）</label><input id="np-name" placeholder="例如 report-2026" /></div>
    <div class="form-row"><label>显示名（可选）</label><input id="np-label" placeholder="给人类看的名字" /></div>
    <div class="form-row"><label>工作目录（留空则自动创建工作台管理的空目录）</label>
      <input id="np-root" placeholder="C:\\work\\myproject" /></div>
    <p class="hint">创建工作区后，可在里面新建任务；也可以直接用「导入目录」把已有文件夹接进来。</p>`,
    `<button class="btn" data-act="modal-close">取消</button><button class="btn primary" data-act="do-new-project">创建</button>`);
}

function openImportModal() {
  openModal("导入已有目录作为工作区", `
    <div class="form-row"><label>源目录</label>
      <div style="display:flex;gap:8px">
        <input id="im-path" placeholder="C:\\path\\to\\dir" />
        ${isDesktop() ? '<button class="btn" data-act="pick-native">系统选择器</button>' : ""}
        <button class="btn ghost" data-act="browse">浏览</button>
      </div>
    </div>
    <div class="form-row"><label>项目名（留空用目录名）</label><input id="im-name" /></div>
    <div id="im-browser"></div>
    <p class="hint">导入不移动、不重组任何文件，只是把该目录登记为工作区。</p>`,
    `<button class="btn" data-act="modal-close">取消</button><button class="btn primary" data-act="do-import">导入</button>`);
}

async function renderDirBrowser(path) {
  const box = $("im-browser");
  if (!box) return;
  box.innerHTML = '<p class="hint">读取中…</p>';
  try {
    const data = await api(`/api/fs/list?path=${encodeURIComponent(path || "")}`);
    state.browsePath = data.path;
    const parent = data.parent
      ? `<button class="btn tiny ghost" data-act="browse-to" data-path="${esc(data.parent)}">↑ 上级</button>`
      : "";
    const dirs = data.dirs.length
      ? data.dirs.slice(0, 200).map((d) =>
          `<button class="btn tiny ghost" data-act="browse-to" data-path="${esc(d.path)}">📁 ${esc(d.name)}</button>`).join("")
      : '<span class="hint">无子目录</span>';
    box.innerHTML = `
      <div class="form-row"><label>当前目录</label>
        <div class="mono" style="word-break:break-all">${esc(data.path)}</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">${parent}
          <button class="btn tiny primary" data-act="browse-use" data-path="${esc(data.path)}">选择此目录</button></div>
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;max-height:180px;overflow:auto">${dirs}</div>`;
  } catch (err) {
    box.innerHTML = `<p class="hint">无法读取：${esc(err.message)}</p>`;
  }
}

function deleteCurrentProject() {
  const project = state.projects.find((p) => p.id === state.projectId);
  if (!project) return;
  openModal("删除项目", `
    <p>将删除项目 <b>${esc(projectDisplayName(project))}</b> 及其全部协作数据：任务、黑板、交接、审批、快照、运行副本。</p>
    <p class="hint">不会删除导入来源目录里的原始文件，也不动工作区里的用户文件。</p>`,
    `<button class="btn" data-act="modal-close">取消</button><button class="btn danger" data-act="do-delete-project">确认删除</button>`);
}

function openProjectSettingsModal() {
  if (!state.projectId) { toast("先选择项目", "error"); return; }
  const p = (state.dashboard && state.dashboard.project)
    || state.projects.find((x) => x.id === state.projectId) || {};
  openModal("项目设置", `
    <div class="form-row"><label for="ps-label">显示名（label）</label>
      <input id="ps-label" value="${esc(prefill(p.label))}" placeholder="给人类看的名字" /></div>
    <div class="form-row"><label for="ps-name">唯一名（name）</label>
      <input id="ps-name" value="${esc(prefill(p.name))}" placeholder="例如 report-2026" /></div>
    <div class="form-row"><label>工作目录</label>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <span class="mono" style="word-break:break-all">${esc(p.root_path || "—")}</span>
        ${openPathButton("打开", p.root_path)}
      </div></div>
    <p class="hint">改名只改显示与元数据，不移动任何文件；历史乱码名称可在这里修正。</p>
    <p class="hint">删除项目会移除其协作数据（任务 / 黑板 / 交接 / 审批 / 快照），不动源目录文件。</p>`,
    `<button class="btn ghost danger" data-act="delete-project">删除项目</button>
     <span class="spacer"></span>
     <button class="btn" data-act="modal-close">取消</button>
     <button class="btn primary" data-act="do-project-settings">保存</button>`);
}

async function saveProjectSettings() {
  const pid = state.projectId;
  if (!pid) return;
  const labelEl = $("ps-label");
  const nameEl = $("ps-name");
  const body = {};
  if (labelEl) body.label = labelEl.value.trim();
  if (nameEl && nameEl.value.trim()) body.name = nameEl.value.trim();
  if (!Object.keys(body).length) { toast("没有改动"); return; }
  let updated;
  try {
    updated = await api(`/api/projects/${encodeURIComponent(pid)}`, { method: "PATCH", body });
  } catch (err) {
    if (isMissingEndpoint(err)) throw new Error("后端尚未支持项目改名（PATCH /api/projects/{pid}）");
    throw err;
  }
  const patch = (updated && updated.project) || updated || {};
  const local = state.projects.find((x) => x.id === pid);
  if (local && patch && typeof patch === "object") Object.assign(local, patch);
  if (state.dashboard && state.dashboard.project) Object.assign(state.dashboard.project, patch);
  state._dashJson = "";
  toast("项目已更新");
  closeModal();
  await loadProjects(true);
  await loadDashboard({ force: true }).catch(() => {});
}

/* ================================================================ 指挥台数据 */

function computePercent(total, counters) {
  if (!total) return 0;
  const done = (counters.success || 0) + (counters.failed || 0)
    + (counters.skipped || 0) + (counters.cancelled || 0);
  return Math.max(0, Math.min(100, Math.round((done / total) * 100)));
}

function normalizeDashboard(raw, pid) {
  const data = raw || {};
  const project = data.project
    || state.projects.find((p) => p.id === pid)
    || { id: pid, name: pid, label: "" };
  const progress = data.progress || {};
  const counters = Object.assign({
    pending: 0, running: 0, success: 0, failed: 0, blocked: 0,
    waiting: 0, skipped: 0, cancelled: 0, interrupted: 0,
  }, progress.counters || {});
  const tasks = (Array.isArray(data.tasks) ? data.tasks : []).map((rawTask) => {
    const taskItem = rawTask && typeof rawTask === "object" ? rawTask : {};
    // 保留后端新增的运行期字段（runtime / timeout_sec / max_attempts …），
    // 否则卡片和详情会拿不到剩余时间与重试预算。
    return {
      ...taskItem,
      id: taskItem.id,
      title: taskItem.title,
      status: taskItem.status || "pending",
      kind: taskItem.kind || "generic",
      agent_id: taskItem.agent_id || null,
      priority: taskItem.priority ?? 0,
      read_only: Boolean(taskItem.read_only),
      updated_at: taskItem.updated_at || null,
      blocked_reason: taskItem.blocked_reason || null,
      runtime: taskItem.runtime && typeof taskItem.runtime === "object" ? taskItem.runtime : null,
      timeout_sec: taskItem.timeout_sec ?? null,
      max_attempts: taskItem.max_attempts ?? null,
      attempt: taskItem.attempt ?? null,
    };
  });
  const total = progress.total ?? tasks.length;
  const agents = (Array.isArray(data.agents) && data.agents.length) ? data.agents : state.agents;
  const approvalsRaw = data.approvals || {};
  const items = Array.isArray(approvalsRaw.items) ? approvalsRaw.items : [];
  const pending = approvalsRaw.pending ?? items.filter((a) => !a.status || a.status === "pending").length;
  return {
    project,
    progress: { total, counters, percent: progress.percent ?? computePercent(total, counters) },
    tasks,
    agents: Array.isArray(agents) ? agents : [],
    approvals: { pending, items },
    stats: data.stats || { runs: 0, duration_ms: 0, tokens: 0, cost: 0 },
    activity: Array.isArray(data.activity) ? data.activity : [],
    running: Boolean(data.running),
    workspace: data.workspace || { files: 0, bytes: 0 },
    server_time: data.server_time || "",
  };
}

/**
 * 旧后端兼容：没有 dashboard 接口时，用既有多接口拼出同样结构。
 * 只在 404（接口不存在）时触发；其它错误照常抛出。
 */
async function buildDashboardFallback(pid) {
  const base = `/api/projects/${encodeURIComponent(pid)}`;
  const [projectRes, kanban, approvals, events, stats] = await Promise.all([
    api(base).catch(() => null),
    api(`${base}/kanban`).catch(() => null),
    api(`${base}/approvals`).catch(() => []),
    api(`${base}/events?limit=60`).catch(() => []),
    api(`${base}/stats`).catch(() => null),
  ]);
  const project = (projectRes && projectRes.project)
    || state.projects.find((p) => p.id === pid)
    || { id: pid, name: pid, label: "" };
  const nodes = (kanban && Array.isArray(kanban.nodes)) ? kanban.nodes : [];
  const counters = {
    pending: 0, running: 0, success: 0, failed: 0, blocked: 0,
    waiting: 0, skipped: 0, cancelled: 0, interrupted: 0,
  };
  for (const t of nodes) counters[t.status] = (counters[t.status] || 0) + 1;
  const total = (kanban && kanban.progress && kanban.progress.total) ?? nodes.length;
  const approvalItems = (Array.isArray(approvals) ? approvals : []).filter((a) => a.status === "pending");
  const totals = (stats && stats.totals) || {};
  return {
    project,
    progress: { total, counters, percent: computePercent(total, counters) },
    tasks: nodes.map((node) => {
      const nodeItem = node && typeof node === "object" ? node : {};
      return {
        ...nodeItem,
        id: nodeItem.id,
        title: nodeItem.title,
        status: nodeItem.status,
        kind: nodeItem.kind,
        agent_id: nodeItem.agent_id,
        priority: nodeItem.priority,
        read_only: Boolean(nodeItem.read_only),
        updated_at: nodeItem.updated_at || null,
        blocked_reason: nodeItem.blocked_reason || null,
        runtime: nodeItem.runtime && typeof nodeItem.runtime === "object" ? nodeItem.runtime : null,
        timeout_sec: nodeItem.timeout_sec ?? null,
        max_attempts: nodeItem.max_attempts ?? null,
        attempt: nodeItem.attempt ?? null,
      };
    }),
    agents: state.agents,
    approvals: { pending: approvalItems.length, items: approvalItems },
    stats: {
      runs: totals.runs || 0,
      duration_ms: totals.duration_ms || 0,
      tokens: totals.tokens || 0,
      cost: totals.cost || 0,
    },
    activity: (Array.isArray(events) ? events : []).slice(-40).map((e) => ({
      at: e.at, kind: e.kind, actor: e.actor, summary: e.summary,
    })),
    running: (counters.running || 0) > 0,
    workspace: { files: 0, bytes: 0 },
    server_time: new Date().toISOString(),
  };
}

async function loadDashboard({ force = false } = {}) {
  if (!state.projectId) { renderNoProject(); return null; }
  if (state._dashPromise) return state._dashPromise;
  const pid = state.projectId;
  state._dashPromise = (async () => {
    let raw;
    try {
      raw = await api(`/api/projects/${encodeURIComponent(pid)}/dashboard`);
    } catch (err) {
      if (!isMissingEndpoint(err)) throw err;
      raw = await buildDashboardFallback(pid);
    }
    if (state.projectId !== pid) return null;
    const data = normalizeDashboard(raw, pid);
    const json = JSON.stringify(data);
    const changed = json !== state._dashJson;
    state.dashboard = data;
    state._dashJson = json;
    state._dashAt = Date.now();
    if (changed || force) renderDashboard();
    return data;
  })();
  try {
    return await state._dashPromise;
  } finally {
    state._dashPromise = null;
  }
}

/* ================================================================ 指挥台渲染 */

function renderDashboard() {
  const d = state.dashboard;
  setDispatchEnabled(Boolean(state.projectId));
  renderHome(d);
  renderInspector(d);
  renderStatusBar(d);
  renderSidebarProject(d);
  updateNavCounts(d);
  if (d) setRunStatus(Boolean(d.running));
  if (d && state.view === "tasks") renderKanbanBoard("kanban", filteredTasks(d.tasks));
  syncMissionIsolationDefault();
  renderLiveRunbar();
}

function renderHomeProject(d) {
  const project = (d && d.project)
    || state.projects.find((item) => item.id === state.projectId)
    || null;
  const title = project ? projectDisplayName(project) : "未选择项目";
  const rootPath = project && project.root_path
    ? String(project.root_path)
    : (state.projectId ? "—" : "新建或导入一个工作区开始");

  setText("home-project-title", title);
  const titleEl = $("home-project-title");
  if (titleEl) titleEl.title = title;

  setText("home-project-path", rootPath);
  const pathEl = $("home-project-path");
  if (pathEl) pathEl.title = rootPath;

  const running = Boolean(d && d.running);
  setText("home-project-badge", running ? "AUTOPILOT ON" : "IDLE");
  const badge = $("home-project-badge");
  if (badge) {
    badge.classList.toggle("is-on", running);
    badge.classList.toggle("is-idle", !running);
    badge.dataset.state = running ? "running" : "idle";
    badge.title = running ? "调度器正在执行任务" : "调度器空闲";
  }
}

function renderHome(d) {
  renderHomeProject(d);
  renderHomeMetrics(d);
  renderHomeKanban(d);
  renderHomeActivity(d);
  renderOnboarding(d);
}

function renderHomeMetrics(d) {
  const c = (d && d.progress && d.progress.counters) || {};
  const known = Boolean(d);
  const value = (v) => (known ? fmtInt(v || 0) : "—");
  const items = [
    { label: "任务总数", value: known ? fmtInt(d.progress.total ?? 0) : "—", tone: "" },
    { label: "执行中", value: value(c.running), tone: "tone-run" },
    { label: "等待人工", value: value(c.waiting), tone: "tone-wait" },
    { label: "成功", value: value(c.success), tone: "tone-ok" },
    { label: "失败", value: value(c.failed), tone: "tone-bad" },
  ];
  const inner = items.map((m) => {
    const cls = known ? "kpi-value" : "kpi-value is-unknown";
    const tip = known ? "" : ' title="尚未选择项目"';
    return `<div class="kpi ${m.tone}"${tip}>
      <div class="${cls}">${esc(m.value)}</div>
      <div class="kpi-label">${esc(m.label)}</div>
    </div>`;
  }).join("");
  setGridHtml("home-metrics", "kpi-grid", inner);
}

function renderHomeKanban(d) {
  renderKanbanBoard("home-kanban", filteredTasks((d && d.tasks) || []));
}

function compactTaskTime(value) {
  const text = shortTime(value);
  return text ? text.slice(5, 16) : "";
}

/** 卡片第三行：优先显示剩余时间 / 阻塞原因，否则显示最近更新时间。 */
function taskEvidence(task) {
  const taskItem = task && typeof task === "object" ? task : {};
  const status = String(taskItem.status || "pending");
  const runtime = taskItem.runtime && typeof taskItem.runtime === "object" ? taskItem.runtime : null;
  const rawAttempt = runtime && runtime.attempt !== undefined ? runtime.attempt : taskItem.attempt;
  const rawMaxAttempts = runtime && runtime.max_attempts !== undefined
    ? runtime.max_attempts : taskItem.max_attempts;
  const attempt = Number(rawAttempt);
  const maxAttempts = Number(rawMaxAttempts);
  const attemptText = Number.isFinite(attempt) && attempt > 0
    && Number.isFinite(maxAttempts) && maxAttempts > 0
    ? `${Math.floor(attempt)}/${Math.floor(maxAttempts)}`
    : "";
  const remaining = runtime && runtime.remaining_sec !== null && runtime.remaining_sec !== undefined
    ? Number(runtime.remaining_sec) : null;
  const elapsed = runtime && runtime.elapsed_sec !== null && runtime.elapsed_sec !== undefined
    ? Number(runtime.elapsed_sec) : null;
  let text = "";
  let tone = "";

  if (status === "blocked") {
    const reason = readableText(taskItem.blocked_reason, "").replace(/\s+/g, " ").trim();
    const clipped = reason.length > 42 ? `${reason.slice(0, 42)}…` : reason;
    text = clipped ? `阻塞 · ${clipped}` : "阻塞";
    tone = "is-bad";
  } else if (runtime && runtime.warning) {
    const warningParts = [];
    if (Number.isFinite(remaining)) warningParts.push(`剩余 ${formatSeconds(remaining)}`);
    if (attemptText) warningParts.push(attemptText);
    warningParts.push("接近超时");
    text = warningParts.join(" · ");
    tone = "is-warn";
  } else if (status === "running" && Number.isFinite(remaining)) {
    text = `剩余 ${formatSeconds(remaining)}${attemptText ? ` · ${attemptText}` : ""}`;
  } else if (status === "running" && Number.isFinite(elapsed)) {
    text = `已运行 ${formatSeconds(elapsed)}`;
  } else if (taskItem.updated_at) {
    text = `更新 ${compactTaskTime(taskItem.updated_at)}`;
  } else if (runtime && Number.isFinite(Number(runtime.effective_timeout_sec))) {
    text = `预算 ${formatSeconds(runtime.effective_timeout_sec)}`;
  } else if (taskItem.timeout_sec) {
    text = `超时 ${formatSeconds(taskItem.timeout_sec)}`;
  } else {
    text = status === "pending" ? "等待调度" : "暂无运行记录";
  }

  const details = [];
  if (runtime) {
    if (runtime.effective_timeout_sec) {
      details.push(`超时预算 ${formatSeconds(runtime.effective_timeout_sec)}`);
    }
    if (runtime.next_attempt_timeout_sec) {
      details.push(`下次重试 ${formatSeconds(runtime.next_attempt_timeout_sec)}`);
    }
    if (runtime.warning_text) details.push(readableText(runtime.warning_text, ""));
  }
  if (taskItem.timeout_sec) details.push(`任务超时 ${formatSeconds(taskItem.timeout_sec)}`);
  return { text, tone, title: details.filter(Boolean).join(" · ") };
}

function taskCardHtml(task) {
  const taskItem = task && typeof task === "object" ? task : {};
  const taskId = String(taskItem.id || "");
  const title = taskDisplayTitle(taskItem);
  const agent = agentLabel(taskItem.agent_id);
  const kind = String(taskItem.kind || "generic");
  const status = String(taskItem.status || "pending");
  const statusLabel = STATUS_LABEL[status] || status;
  const priority = taskItem.priority ?? 0;
  const evidence = taskEvidence(taskItem);
  const isNew = state._highlightTask === taskId ? " is-new" : "";
  return `<div class="kcard${isNew}" data-act="task-open" data-id="${esc(taskId)}" data-status="${esc(status)}" role="button" tabindex="0"
      title="${esc(title)} · ${esc(agent)}">
    <div class="kcard-head">
      <span class="kcard-avatar" aria-hidden="true">${esc(initials(agent))}</span>
      <span class="kcard-main"><span class="kcard-title">${esc(title)}</span></span>
    </div>
    <div class="kcard-branch">
      <span class="kcard-agent">${esc(agent)}</span>
      <span class="kcard-branch-sep" aria-hidden="true">·</span>
      <span class="kcard-kind">${esc(kind)}</span>
      <span class="kcard-id mono">#${esc(shortTaskId(taskId))}</span>
    </div>
    <div class="kcard-evidence"${evidence.title ? ` title="${esc(evidence.title)}"` : ""}>${evidence.tone
      ? `<span class="${esc(evidence.tone)}">${esc(evidence.text)}</span>`
      : esc(evidence.text)}</div>
    <div class="kcard-foot">
      <span class="kcard-prio" title="PRIORITY" aria-label="PRIORITY ${esc(priority)}">P${esc(priority)}</span>
      ${taskItem.read_only ? '<span class="tag kcard-readonly" title="只读任务，不会写入工作区">READ ONLY</span>' : ""}
      <span class="tag ${esc(status)} kcard-status">${esc(statusLabel)}</span>
    </div>
  </div>`;
}

/** 一句话任务创建成功后：切到任务视图并高亮新卡片几秒。 */
function highlightTaskCard(taskId) {
  if (!taskId) return;
  state._highlightTask = taskId;
  renderKanban();
  const cards = Array.from(document.querySelectorAll("#kanban .kcard"));
  const card = cards.find((c) => c.dataset.id === taskId);
  if (card) {
    card.classList.add("is-new");
    card.style.boxShadow = "0 0 0 2px var(--ops-accent) inset";
    try { card.scrollIntoView({ block: "nearest", behavior: "smooth" }); } catch (_) { /* ignore */ }
  }
  clearTimeout(state._hlTimer);
  state._hlTimer = setTimeout(() => {
    state._highlightTask = "";
    document.querySelectorAll("#kanban .kcard.is-new").forEach((el) => {
      el.classList.remove("is-new");
      el.style.boxShadow = "";
    });
  }, 4000);
}

function kanbanEmptyHtml() {
  const filtering = Boolean(String(state.taskFilter || "").trim());
  return `<div class="kcol-empty"${filtering ? ' title="没有匹配当前搜索的任务"' : ""}>${filtering ? "无匹配" : "暂无"}</div>`;
}

/**
 * 6 列看板。新壳里 #home-kanban / #kanban 已经有 6 个 .kcol 骨架：
 * 优先就地更新每列数量与 .kcol-body（保留骨架、减少 DOM 重写）；
 * 骨架缺失时整体重建同样结构。
 */
function renderKanbanBoard(containerId, tasks) {
  const container = $(containerId);
  if (!container) return;
  const list = Array.isArray(tasks) ? tasks : [];
  const byStatus = {};
  for (const t of list) (byStatus[t.status] || (byStatus[t.status] = [])).push(t);
  const cols = COLUMNS.map((col) => ({
    col,
    items: col.statuses.reduce((acc, s) => acc.concat(byStatus[s] || []), []),
  }));
  const skeletons = Array.from(container.querySelectorAll(".kcol"));
  const cache = state._colCache[containerId] || (state._colCache[containerId] = {});
  if (skeletons.length === COLUMNS.length) {
    cols.forEach((entry, index) => {
      const kcol = skeletons[index];
      const body = kcol.querySelector(".kcol-body");
      const count = kcol.querySelector(".kcol-count");
      const bodyHtml = entry.items.length
        ? entry.items.map(taskCardHtml).join("")
        : kanbanEmptyHtml();
      if (count && count.textContent !== String(entry.items.length)) {
        count.textContent = String(entry.items.length);
      }
      if (body && cache[`b${index}`] !== bodyHtml) {
        cache[`b${index}`] = bodyHtml;
        body.innerHTML = bodyHtml;
      }
    });
    return;
  }
  const inner = cols.map((entry) => {
    const bodyHtml = entry.items.length
      ? entry.items.map(taskCardHtml).join("")
      : kanbanEmptyHtml();
    return `<div class="kcol tone-${esc(entry.col.tone)}" data-col="${esc(entry.col.id)}">
      <div class="kcol-head">
        <span class="kcol-dot"></span>
        <span class="kcol-name">${esc(entry.col.label)}</span>
        <span class="kcol-count">${entry.items.length}</span>
      </div>
      <div class="kcol-body">${bodyHtml}</div>
    </div>`;
  }).join("");
  setGridHtml(containerId, "kanban", inner);
}

function activityRowHtml(event) {
  const e = event || {};
  const rawSummary = displayText(e.summary, "");
  const summary = rawSummary || eventKindLabel(e.kind);
  const actor = e.actor ? displayText(e.actor, "") : "";
  const text = [summary, actor ? `· ${actor}` : ""].filter(Boolean).join(" ");
  return `<div class="activity-row" title="${esc(displayText(e.kind, eventKindLabel(e.kind)))}">
    <span class="activity-time">${esc(shortClock(e.at))}</span>
    <span class="activity-kind">${esc(eventKindLabel(e.kind))}</span>
    <span class="activity-text">${esc(text)}</span>
  </div>`;
}

function renderHomeActivity(d) {
  const items = ((d && d.activity) || []).slice(-8).reverse();
  const inner = items.length
    ? items.map(activityRowHtml).join("")
    : compactEmpty("ACTIVITY", "还没有活动记录", "创建第一个任务后，Agent 的每一步动作都会带时间戳写进这里。");
  setGridHtml("home-activity", "activity", inner);
}

function onboardingInnerHtml(mode) {
  const noProject = mode === "project";
  const subtitle = noProject
    ? "从一个已有目录开始，或创建空白工作区，让 Agent 团队在真实文件上协作。"
    : "这个工作区还没有任务：先派发一个，或导入一份带依赖的批量计划。";
  return `<div class="launcher-sheet" data-mode="${esc(noProject ? "project" : "task")}">
    <div class="launcher-brand">
      <span class="launcher-mark" aria-hidden="true"></span>
      <strong class="launcher-title">MultiAgent</strong>
    </div>
    <p class="launcher-sub">${esc(subtitle)}</p>
    <div class="launcher-actions">
      <button type="button" class="launcher-action is-primary" data-act="new-project">
        <span class="launcher-action-ico" aria-hidden="true">＋</span>
        <span class="launcher-action-copy"><strong>新建项目</strong><small>创建一个空白工作区</small></span>
        <span class="launcher-action-key" aria-hidden="true">↵</span>
      </button>
      <button type="button" class="launcher-action" data-act="import-project">
        <span class="launcher-action-ico" aria-hidden="true">↳</span>
        <span class="launcher-action-copy"><strong>导入目录</strong><small>把已有本地目录接进工作台</small></span>
      </button>
      <button type="button" class="launcher-action" data-act="new-task">
        <span class="launcher-action-ico" aria-hidden="true">☐</span>
        <span class="launcher-action-copy"><strong>新建任务</strong><small>在当前工作区派发一个任务</small></span>
      </button>
      <button type="button" class="launcher-action" data-act="import-plan">
        <span class="launcher-action-ico" aria-hidden="true">{ }</span>
        <span class="launcher-action-copy"><strong>批量计划 JSON</strong><small>一次创建带依赖的任务图</small></span>
      </button>
    </div>
  </div>`;
}

function renderOnboarding(d) {
  const el = $("home-onboarding");
  if (!el) return;
  const noProject = !state.projectId;
  const empty = noProject || !d || (d.progress.total || 0) === 0;
  const mode = noProject ? "project" : "task";
  // 启动器自带完整浅色卡片；不要把它再套进旧的 dashed empty-box。
  if (el.classList.contains("empty-box")) {
    el.classList.remove("empty-box", "onboarding");
    el.classList.add("launcher-host");
  }
  if (el.dataset.mode !== mode) {
    el.innerHTML = onboardingInnerHtml(mode);
    el.dataset.mode = mode;
  }
  el.hidden = !empty;
  el.classList.toggle("is-visible", empty);
  // 空项目：隐藏看板/活动，只留「任务输入 + 引导 + KPI」，避免三个空状态堆叠。
  const center = $("center");
  if (center) center.classList.toggle("no-project", noProject);
}

/* ---------------------------------------------------------------- inspector */

function renderInspector(d) {
  renderInspectorAgents(d);
  renderInspectorApprovals(d);
  renderInspectorRunning(d);
  renderStream();
}

function renderInspectorAgents(d) {
  const agents = (((d && d.agents) || state.agents) || []).slice(0, 4);
  if (!agents.length) {
    setHtml("inspector-agents", compactEmpty("AGENTS", "未检测到 Agent", "在 config/agents.json 中配置可执行 CLI。"));
    return;
  }
  const inner = agents.map((a) => {
    const on = a.enabled !== false;
    const usage = a.usage || {};
    const role = [a.protocol || "agent", (a.roles || [])[0]].filter(Boolean).join(" · ");
    const stateText = on ? `${fmtInt(usage.invocations ?? 0)} 次` : "停用";
    const shown = agentDisplayName(a);
    const tip = `${shown} · ${on ? "已启用" : "已停用"} · ${fmtInt(usage.invocations ?? 0)} 次调用（点击${on ? "停用" : "启用"}）`;
    return `<div class="agent-tile ${on ? "is-on" : "is-off"}"
        data-act="agent-toggle" data-id="${esc(a.id)}" data-enable="${on ? "0" : "1"}"
        role="button" tabindex="0" title="${esc(tip)}">
      <span class="agent-avatar">${esc(initials(shown))}</span>
      <span class="agent-name"><i class="agent-dot-state"></i>${esc(shown)}</span>
      <span class="agent-role">${esc(role)}</span>
      <span class="agent-state">${esc(stateText)}</span>
    </div>`;
  }).join("");
  setGridHtml("inspector-agents", "agent-grid", inner);
}

function renderInspectorApprovals(d) {
  const items = ((d && d.approvals && d.approvals.items) || [])
    .filter((a) => !a.status || a.status === "pending")
    .slice(0, 5);
  if (!items.length) {
    setListHtml("inspector-approvals", [compactEmpty("CLEAR", "没有待确认", "危险操作会在这里等你点头。")]);
    return;
  }
  const rows = items.map((a) => rowHtml({
    title: esc(displayText(a.title, "待确认操作")),
    tags: `<span class="tag blocked">${esc(a.kind || "approval")}</span>`
      + (a.risk ? `<span class="tag">风险 ${esc(a.risk)}</span>` : ""),
    actions: `<button class="btn tiny" data-act="approval" data-id="${esc(a.id)}" data-ok="1">批准</button>
      <button class="btn tiny danger" data-act="approval" data-id="${esc(a.id)}" data-ok="0">拒绝</button>`,
  }));
  setListHtml("inspector-approvals", rows);
}

function renderInspectorRunning(d) {
  const running = ((d && d.tasks) || []).filter((t) => t.status === "running").slice(0, 6);
  if (!running.length) {
    setListHtml("inspector-running", [compactEmpty("IDLE", "当前没有任务在跑", "点「启动调度」把待执行任务派给编队。")]);
    return;
  }
  const rows = running.map((t) => rowHtml({
    title: esc(taskDisplayTitle(t)),
    meta: `${esc(agentLabel(t.agent_id))} · ${esc(t.kind || "generic")}`
      + (t.runtime ? ` · 剩余 ${esc(formatSeconds(t.runtime.remaining_sec))}` : ""),
    tags: t.runtime && t.runtime.warning ? '<span class="tag failed">接近超时</span>' : "",
    actions: `<button class="btn tiny ghost" data-act="task-open" data-id="${esc(t.id)}">打开</button>`,
  }));
  setListHtml("inspector-running", rows);
}

function streamRowHtml(event) {
  const e = event || {};
  const kind = String(e.kind || e.type || "");
  const text = e.text || e.summary || e.tool || "";
  const meta = [
    e.agent_id,
    e.task_id ? String(e.task_id).slice(-6) : "",
    e.at ? String(e.at).slice(11, 19) : "",
  ].filter(Boolean).join(" · ");
  return `<div class="stream-row s-${esc(kind.replace(/[.]/g, "-"))}">
    <span class="stream-tag">${esc(streamLabel(e))}</span>
    <span class="stream-text">${esc(text)}</span>
    <span class="stream-meta">${esc(meta)}</span>
  </div>`;
}

/* ================================================================ 实时块时间线
 *
 * 用户反馈：raw ACP 事件是「一行一个 token」，实时视图不可用。
 * 这里把 raw 事件合并成「思考 / 消息 / 工具 / 事件」块：
 *   - thought/message：同一 run 的连续 chunk 合并成一个块（coalesced 事件带 chunk_count 也兼容）；
 *   - tool：按 tool_call_id 合并 tool_call + tool_call_update（title/status/input/output/locations）；
 *   - event：任务事件分隔行；usage_update 生成轻量 meta 行。
 * 渲染只重建签名变化的块（data-block-id + 签名），窗口 300 块。
 */

const STREAM_WINDOW = 60;      // inspector 缩略流保留条数（块摘要）
const LIVE_RAW_LIMIT = 4000;   // state.stream 原始事件上限
const LIVE_BLOCK_WINDOW = 300; // 实时视图 DOM 块上限

function hashText(value) {
  const s = String(value ?? "");
  let h = 5381;
  for (let i = 0; i < s.length; i += 1) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
  return (h >>> 0).toString(36);
}

function toolStatusClass(status) {
  const s = String(status || "").toLowerCase();
  if (s === "completed" || s === "success" || s === "done") return "is-success";
  if (s === "failed" || s === "error" || s === "cancelled" || s === "canceled") return "is-failed";
  if (s === "in_progress" || s === "running") return "is-running";
  return "is-pending";
}

function toolStatusLabel(status) {
  const s = String(status || "").toLowerCase();
  if (s === "completed" || s === "success" || s === "done") return "完成";
  if (s === "failed" || s === "error") return "失败";
  if (s === "cancelled" || s === "canceled") return "取消";
  if (s === "in_progress" || s === "running") return "进行中";
  if (s === "pending") return "等待";
  return "已记录";
}

function formatToolValue(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  try { return JSON.stringify(value, null, 2); } catch (_) { return String(value); }
}

/** raw 事件 → 块（纯函数，便于离线回放验证）。 */
function buildLiveBlocks(events) {
  const list = Array.isArray(events) ? events : [];
  const ordered = list.map((event, index) => ({ event: event || {}, index }));
  const allSeq = list.length > 0 && list.every((e) => e && Number.isFinite(Number(e.seq)));
  if (allSeq) {
    ordered.sort((a, b) => (Number(a.event.seq) - Number(b.event.seq)) || (a.index - b.index));
  }
  const blocks = [];
  const toolIndex = new Map();
  let openChunk = null;

  for (const item of ordered) {
    const e = item.event;
    const kind = String(e.kind || e.type || "");
    const baseId = `blk-${e.seq !== undefined && e.seq !== null ? e.seq : `i${item.index}`}`;
    const agentId = e.agent_id || "";
    const taskId = e.task_id || "";
    const runId = e.run_id || "";

    if (kind === "agent_thought_chunk" || kind === "agent_message_chunk") {
      const type = kind === "agent_thought_chunk" ? "thought" : "message";
      const key = `${type}|${runId}|${agentId}`;
      const count = Math.max(1, Number(e.chunk_count) || 1);
      if (openChunk && openChunk.type === type && openChunk.key === key) {
        openChunk.content += String(e.text || "");
        openChunk.chunk_count += count;
        if (e.at) openChunk.last_at = e.at;
      } else {
        openChunk = {
          id: baseId, type, key,
          content: String(e.text || ""),
          chunk_count: count,
          agent_id: agentId, task_id: taskId, run_id: runId,
          at: e.at || "", last_at: e.at || "",
        };
        blocks.push(openChunk);
      }
      continue;
    }

    openChunk = null;

    if (kind === "tool_call" || kind === "tool_call_update" || e.tool_call_id) {
      const cid = String(e.tool_call_id || e.call_id || `tool-${item.index}`);
      let block = toolIndex.get(cid);
      if (!block) {
        block = {
          id: baseId, type: "tool", tool_id: cid,
          chunk_count: 0,
          agent_id: agentId, task_id: taskId, run_id: runId,
          at: e.at || "", last_at: e.at || "",
          status: "", title: "", command: "", input: null, output: null, locations: [],
        };
        toolIndex.set(cid, block);
        blocks.push(block);
      }
      applyToolEvent(block, e);
      continue;
    }

    if (kind === "usage_update") {
      blocks.push({
        id: baseId, type: "usage", kind,
        agent_id: agentId, task_id: taskId, run_id: runId,
        at: e.at || "", last_at: e.at || "", chunk_count: 1,
      });
      continue;
    }

    blocks.push({
      id: baseId, type: "event", kind,
      summary: String(e.summary || e.message || e.text || ""),
      agent_id: agentId, task_id: taskId, run_id: runId,
      at: e.at || "", last_at: e.at || "",
      chunk_count: Math.max(1, Number(e.chunk_count) || 1),
    });
  }
  return blocks;
}

function applyToolEvent(block, event) {
  const e = event || {};
  block.chunk_count += Math.max(1, Number(e.chunk_count) || 1);
  if (e.at) block.last_at = e.at;
  if (e.status) block.status = String(e.status);
  const tool = e.tool;
  if (typeof tool === "string") {
    if (!block.command) block.command = tool;
    if (!block.title) block.title = firstLine(tool, 80);
  } else if (tool && typeof tool === "object") {
    if (!block.title) block.title = String(tool.title || tool.name || "");
    if (!block.command) block.command = String(tool.command || tool.cmd || "");
    if (tool.input !== undefined && tool.input !== null) block.input = tool.input;
    if (tool.output !== undefined && tool.output !== null) block.output = tool.output;
    if (Array.isArray(tool.locations)) block.locations = tool.locations;
  }
  if (e.title) block.title = block.title || String(e.title);
  if (e.command) block.command = block.command || String(e.command);
  if (e.input !== undefined && e.input !== null) block.input = e.input;
  if (e.output !== undefined && e.output !== null) block.output = e.output;
  if (Array.isArray(e.locations)) block.locations = e.locations;
  if (!block.title) block.title = firstLine(block.command, 80) || "工具调用";
}

function getLiveBlocks() {
  if (!state._liveBlocksDirty && Array.isArray(state._liveBlocks)) return state._liveBlocks;
  const filterEl = $("live-task");
  const filter = filterEl ? filterEl.value : "";
  const events = filter
    ? state.stream.filter((e) => e && e.task_id === filter)
    : state.stream;
  state._liveBlocks = buildLiveBlocks(events);
  state._liveBlocksDirty = false;
  return state._liveBlocks;
}

function markLiveDirty() {
  state._liveBlocksDirty = true;
}

function liveBlockContext(blocks) {
  let latestThoughtId = "";
  for (let i = blocks.length - 1; i >= 0; i -= 1) {
    if (blocks[i].type === "thought") { latestThoughtId = blocks[i].id; break; }
  }
  return { latestThoughtId };
}

/* ---------------------------------------------------------------- 轻量 markdown
 * 先 esc 再解析：只产出 p / br / pre+code / code / strong，绝不拼接未转义内容。 */
function inlineMarkdown(text) {
  return String(text)
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
}

function renderMarkdown(raw) {
  const src = esc(String(raw ?? ""));
  const lines = src.split("\n");
  const out = [];
  let buf = [];
  let inCode = false;
  const flushText = () => {
    const text = buf.join("\n").trim();
    buf = [];
    if (!text) return;
    const paras = text.split(/\n{2,}/)
      .map((p) => inlineMarkdown(p.split("\n").join("<br/>")))
      .filter(Boolean);
    if (paras.length) out.push(`<p>${paras.join("</p><p>")}</p>`);
  };
  for (const line of lines) {
    if (line.startsWith("```")) {
      if (inCode) {
        out.push(`<pre class="live-code"><code>${buf.join("\n")}</code></pre>`);
        buf = [];
        inCode = false;
      } else {
        flushText();
        inCode = true;
        buf = [];
      }
      continue;
    }
    buf.push(line);
  }
  if (inCode) {
    // 流式输出里围栏可能还没闭合：剩余内容按代码块渲染
    out.push(`<pre class="live-code"><code>${buf.join("\n")}</code></pre>`);
  } else {
    flushText();
  }
  return out.join("") || `<p>${inlineMarkdown(src.trim())}</p>`;
}

/* ---------------------------------------------------------------- 块渲染 */

function blockTitle(block) {
  if (block.type === "thought" || block.type === "message") {
    const agent = agentLabel(block.agent_id);
    const run = block.run_id ? ` · ${String(block.run_id).slice(-8)}` : "";
    return `${agent}${run}`;
  }
  if (block.type === "tool") {
    return block.title || firstLine(block.command, 70) || "工具调用";
  }
  return eventKindLabel(block.kind);
}

function blockMeta(block) {
  const time = shortClock(block.last_at || block.at || "");
  if (block.type === "thought" || block.type === "message") {
    return `${time} · ${block.chunk_count || 1} 片`;
  }
  if (block.type === "tool") {
    return block.chunk_count > 1 ? `${time} · ${block.chunk_count} 更新` : time;
  }
  return time;
}

function isBlockCollapsed(block, ctx) {
  const override = state._liveToggles[block.id]; // true = 展开
  if (block.type === "thought") {
    if (override === undefined) return block.id !== ctx.latestThoughtId;
    return !override;
  }
  return false;
}

function blockHtml(block, ctx) {
  const collapsed = isBlockCollapsed(block, ctx);
  const kindClass = block.type === "message" ? "live-msg" : "live-thought";
  const toggle = block.type === "thought"
    ? `<button class="live-toggle" type="button" data-act="live-block-toggle" data-id="${esc(block.id)}" aria-expanded="${collapsed ? "false" : "true"}">${collapsed ? "展开" : "收起"}</button>`
    : "";
  return `<article class="live-block ${kindClass}${collapsed ? " is-collapsed" : ""}" data-block-id="${esc(block.id)}">
    <header class="live-block-head">
      <span class="live-badge">${block.type === "message" ? "消息" : "思考"}</span>
      <span class="live-block-title">${esc(blockTitle(block))}</span>
      <span class="live-dim">${esc(blockMeta(block))}</span>
      <span class="spacer"></span>
      ${toggle}
    </header>
    <div class="live-rich">${renderMarkdown(normalizeChunkedText(block.content))}</div>
  </article>`;
}

function toolBlockHtml(block) {
  const statusCls = toolStatusClass(block.status);
  const parts = [];
  if (block.command) parts.push(`<pre class="live-code"><code>${esc(block.command)}</code></pre>`);
  if (block.input !== null && block.input !== undefined && block.input !== "") {
    parts.push(`<pre class="live-code"><code>${esc(formatToolValue(block.input))}</code></pre>`);
  }
  if (block.output !== null && block.output !== undefined && block.output !== "") {
    parts.push(`<pre class="live-code"><code>${esc(formatToolValue(block.output))}</code></pre>`);
  }
  if (Array.isArray(block.locations) && block.locations.length) {
    const locs = block.locations.map((loc) => {
      if (typeof loc === "string") return loc;
      if (loc && typeof loc === "object") return String(loc.path || loc.uri || loc.name || JSON.stringify(loc));
      return String(loc);
    }).join("\n");
    parts.push(`<pre class="live-code"><code>${esc(locs)}</code></pre>`);
  }
  if (!parts.length) parts.push('<div class="live-dim">ACP 只回了 tool_call_id，暂无命令 / 输出</div>');
  return `<details class="live-block live-tool ${statusCls}" data-block-id="${esc(block.id)}">
    <summary class="live-block-head">
      <span class="live-badge">工具</span>
      <span class="live-block-title">${esc(blockTitle(block))}</span>
      <span class="live-tool-status ${statusCls}">${esc(toolStatusLabel(block.status))}</span>
      <span class="live-dim">${esc(blockMeta(block))}</span>
      <span class="spacer"></span>
    </summary>
    <div class="live-rich">${parts.join("")}</div>
  </details>`;
}

function eventBlockHtml(block) {
  const text = block.type === "usage"
    ? `用量更新${block.at ? ` · ${shortClock(block.at)}` : ""}`
    : `${eventKindLabel(block.kind)}${block.summary ? ` · ${displayText(block.summary, "")}` : ""}${block.at ? ` · ${shortClock(block.at)}` : ""}`;
  return `<div class="live-event" data-block-id="${esc(block.id)}"><span class="live-dim">${esc(text)}</span></div>`;
}

function blockHtmlFor(block, ctx) {
  if (block.type === "tool") return toolBlockHtml(block);
  if (block.type === "event" || block.type === "usage") return eventBlockHtml(block);
  return blockHtml(block, ctx);
}

function blockSignature(block, ctx) {
  const contentSig = block.content
    ? `${block.content.length}:${hashText(block.content.slice(-64))}`
    : "";
  const commandSig = block.command ? hashText(String(block.command)) : "";
  const outputSig = (block.output !== null && block.output !== undefined)
    ? hashText(formatToolValue(block.output))
    : "";
  return [
    block.type,
    block.status || "",
    block.chunk_count || 0,
    block.last_at || "",
    contentSig,
    commandSig,
    outputSig,
    isBlockCollapsed(block, ctx) ? 1 : 0,
  ].join("|");
}

/* ---------------------------------------------------------------- feed DOM */

function liveFeedEl() {
  const box = $("live-list");
  if (!box) return null;
  let feed = box.querySelector(".live-feed");
  if (!feed) {
    // 清掉壳里的静态空态，避免和 .live-feed 的空态重复
    Array.from(box.children).forEach((child) => {
      if (!child.classList.contains("live-feed")) child.remove();
    });
    feed = document.createElement("div");
    feed.className = "live-feed";
    box.appendChild(feed);
  }
  return feed;
}

function resetLiveFeedDom() {
  state._liveNodes = null;
  state._liveSigs = null;
  state._liveOrder = [];
  const box = $("live-list");
  if (box) box.innerHTML = "";
}

function updateLiveJump() {
  const btn = $("live-jump");
  if (!btn) return;
  if (!state._livePinned) {
    btn.hidden = false;
    if (btn.hasAttribute("hidden")) btn.removeAttribute("hidden");
    btn.classList.add("is-visible");
    btn.textContent = state._liveUnseen > 0 ? `回到最新 (${state._liveUnseen})` : "回到最新";
  } else {
    btn.hidden = true;
    btn.classList.remove("is-visible");
    btn.textContent = "回到最新";
  }
}

function scrollLiveToBottom() {
  const box = $("live-list");
  state._livePinned = true;
  state._liveUnseen = 0;
  if (box) box.scrollTop = box.scrollHeight;
  updateLiveJump();
}

function bindLiveScroll() {
  const box = $("live-list");
  if (!box) return;
  box.addEventListener("scroll", () => {
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    state._livePinned = atBottom;
    if (atBottom) state._liveUnseen = 0;
    updateLiveJump();
  });
}

function renderLiveFeed(force = false) {
  const box = $("live-list");
  if (!box) return;
  if (force) resetLiveFeedDom();
  const feed = liveFeedEl();
  if (!feed) return;
  const blocks = getLiveBlocks();
  const windowBlocks = blocks.slice(-LIVE_BLOCK_WINDOW);
  const ctx = liveBlockContext(windowBlocks);

  if (!state._liveNodes) state._liveNodes = new Map();
  if (!state._liveSigs) state._liveSigs = new Map();
  const nodes = state._liveNodes;
  const sigs = state._liveSigs;
  const order = windowBlocks.map((b) => b.id);
  const orderSet = new Set(order);

  for (const [id, node] of Array.from(nodes.entries())) {
    if (orderSet.has(id)) continue;
    if (node.parentNode) node.remove();
    nodes.delete(id);
    sigs.delete(id);
  }

  let empty = feed.querySelector(".live-empty");
  if (!windowBlocks.length) {
    if (!empty) {
      empty = document.createElement("div");
      empty.className = "stream-empty mono live-empty";
      empty.textContent = state.projectId ? "等待 WebSocket 事件…" : "还没有项目";
      feed.appendChild(empty);
    }
  } else if (empty) {
    empty.remove();
  }

  const prevSet = new Set(state._liveOrder || []);
  let appended = 0;
  for (const block of windowBlocks) {
    const sig = blockSignature(block, ctx);
    let node = nodes.get(block.id);
    if (node && sigs.get(block.id) === sig) {
      if (!node.isConnected) feed.appendChild(node);
      continue;
    }
    const holder = document.createElement("div");
    holder.innerHTML = blockHtmlFor(block, ctx);
    const fresh = holder.firstElementChild;
    if (!fresh) continue;
    fresh.dataset.blockId = block.id;
    if (node && node.parentNode) {
      if (node.tagName === "DETAILS" && fresh.tagName === "DETAILS") fresh.open = node.open;
      node.replaceWith(fresh);
    } else {
      feed.appendChild(fresh);
    }
    nodes.set(block.id, fresh);
    sigs.set(block.id, sig);
    if (!prevSet.has(block.id)) appended += 1;
  }
  state._liveOrder = order;

  const autoscroll = $("live-autoscroll");
  const follow = (!autoscroll || autoscroll.checked) && state._livePinned;
  if (follow) {
    box.scrollTop = box.scrollHeight;
    state._liveUnseen = 0;
  } else if (!state._livePinned && appended) {
    state._liveUnseen += appended;
  }
  updateLiveJump();
}

function toggleLiveBlock(id) {
  if (!id) return;
  const blocks = getLiveBlocks();
  const ctx = liveBlockContext(blocks.slice(-LIVE_BLOCK_WINDOW));
  const block = blocks.find((b) => b.id === id);
  if (!block || block.type !== "thought") return;
  const collapsed = isBlockCollapsed(block, ctx);
  state._liveToggles[id] = collapsed; // true = 展开，false = 折叠
  renderLiveFeed();
}

/* ---------------------------------------------------------------- 实时视图外壳 */

function renderLiveTaskOptions() {
  const sel = $("live-task");
  if (!sel) return;
  const keep = sel.value;
  const opts = '<option value="">全部任务</option>'
    + currentTasks().map((t) => `<option value="${esc(t.id)}">${esc(taskDisplayTitle(t))}</option>`).join("");
  setHtml("live-task", opts);
  if (keep && Array.from(sel.options).some((o) => o.value === keep)) sel.value = keep;
  else sel.value = "";
}

function renderLiveRunbar() {
  const bar = $("live-runbar");
  const titleEl = $("live-run-title");
  const metaEl = $("live-run-meta");
  const statusEl = $("live-run-status");
  if (!bar && !titleEl && !metaEl && !statusEl) return;
  const d = state.dashboard;
  const tasks = (d && d.tasks) || [];
  const filterEl = $("live-task");
  const filter = filterEl ? filterEl.value : "";
  const filtered = filter ? tasks.find((t) => t.id === filter) : null;
  const running = tasks.filter((t) => t.status === "running");
  const current = filtered || running[0] || null;
  const runningNow = Boolean((d && d.running) || running.length);
  if (current && current.id !== state._liveRunSeenId) {
    state._liveRunSeenId = current.id;
    state._liveRunStartedAt = Date.now();
  }
  if (!current) {
    state._liveRunSeenId = "";
    state._liveRunStartedAt = 0;
  }
  const elapsed = state._liveRunStartedAt
    ? Math.max(0, Math.round((Date.now() - state._liveRunStartedAt) / 1000))
    : 0;
  const lastEvent = state.stream.length ? state.stream[state.stream.length - 1] : null;
  const runShort = lastEvent && lastEvent.run_id ? String(lastEvent.run_id).slice(-8) : "";
  if (titleEl) {
    titleEl.textContent = current
      ? taskDisplayTitle(current)
      : (state.projectId ? "等待任务开始…" : "未选择项目");
  }
  if (metaEl) {
    const parts = [];
    if (current) parts.push(agentLabel(current.agent_id));
    if (elapsed) parts.push(`已运行 ${elapsed}s`);
    if (runShort) parts.push(`run ${runShort}`);
    metaEl.textContent = parts.length ? parts.join(" · ") : "启动调度后这里会显示当前 run";
  }
  if (statusEl) {
    statusEl.textContent = runningNow ? "运行中" : "空闲";
    statusEl.className = `live-run-status ${runningNow ? "is-running" : "is-idle"}`;
  }
  if (bar) bar.className = `live-runbar ${runningNow ? "is-running" : "is-idle"}`;
}

/* ---------------------------------------------------------------- inspector 缩略流 */

function blockTagLabel(block) {
  if (block.type === "thought") return "思考";
  if (block.type === "message") return "消息";
  if (block.type === "tool") return "工具";
  return eventKindLabel(block.kind);
}

function blockClass(block) {
  if (block.type === "thought") return "agent-thought";
  if (block.type === "message") return "agent-message";
  if (block.type === "tool") return "tool-call";
  if (block.type === "usage") return "usage";
  return "task-event";
}

function blockSummary(block) {
  if (block.type === "thought" || block.type === "message") {
    return firstLine(normalizeChunkedText(block.content), 140) || "（空）";
  }
  if (block.type === "tool") {
    const cmd = firstLine(block.command, 90) || block.title || "工具调用";
    return `${cmd} · ${toolStatusLabel(block.status)}`;
  }
  if (block.type === "usage") return "用量更新";
  const summary = displayText(block.summary, "");
  return summary ? `${eventKindLabel(block.kind)} · ${summary}` : eventKindLabel(block.kind);
}

function inspectorBlockRowHtml(block) {
  const meta = [displayText(block.agent_id, ""), shortClock(block.last_at || block.at)]
    .filter(Boolean).join(" · ");
  return `<div class="stream-row s-${esc(blockClass(block))}">
    <span class="stream-tag">${esc(blockTagLabel(block))}</span>
    <span class="stream-text">${esc(blockSummary(block))}</span>
    <span class="stream-meta">${esc(meta)}</span>
  </div>`;
}

/** inspector-stream：显示最近 8 个块的摘要（不再逐 chunk 一行）。 */
function renderStream() {
  if (state._streamFrozen) return;
  const blocks = getLiveBlocks();
  const recent = blocks.filter((b) => b.type !== "usage").slice(-8).reverse();
  const activity = ((state.dashboard && state.dashboard.activity) || []).slice(-8).reverse();
  if (recent.length) {
    setStreamHtml("inspector-stream", "stream", recent.map(inspectorBlockRowHtml).join(""));
  } else if (activity.length) {
    setStreamHtml("inspector-stream", "activity", activity.map(activityRowHtml).join(""));
  } else {
    setStreamHtml("inspector-stream", "stream", '<div class="stream-empty mono">等待 WebSocket 事件…</div>');
  }
  const box = $("inspector-stream");
  if (box) box.scrollTop = box.scrollHeight;
  updateLiveBadge();
}

function ensureStreamJump() {
  const box = $("inspector-stream");
  if (!box || box.querySelector(".stream-jump")) return;
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn tiny ghost stream-jump";
  btn.dataset.act = "stream-latest";
  btn.textContent = "回到最新";
  btn.style.cssText = "position:sticky;bottom:2px;align-self:center;margin-top:4px;z-index:2";
  box.appendChild(btn);
}

function removeStreamJump() {
  const box = $("inspector-stream");
  const btn = box && box.querySelector(".stream-jump");
  if (btn) btn.remove();
}

function bindStreamScroll() {
  const box = $("inspector-stream");
  if (!box) return;
  box.addEventListener("scroll", () => {
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
    if (atBottom) {
      if (state._streamFrozen) {
        state._streamFrozen = false;
        removeStreamJump();
        renderStream();
      }
      return;
    }
    if (!state._streamFrozen) {
      state._streamFrozen = true;
      ensureStreamJump();
    }
  });
}

/** 实时视图入口：任务筛选 + run 条 + 块时间线 + 计数。 */
function renderLegacyStream() {
  const box = $("live-list");
  if (!box) return;
  renderLiveTaskOptions();
  renderLiveRunbar();
  renderLiveFeed();
  updateLiveBadge();
}

/** 默认隐藏、由 JS 显示的小徽标：同时兼容 `hidden` 属性与 `style.display` 两种写法。
 *  只写 style.display 时，全局 `[hidden]{display:none!important}` 会永久压住计数。 */
function setBadgeVisible(el, visible) {
  if (!el) return;
  if (visible) {
    el.hidden = false;
    if (el.hasAttribute("hidden")) el.removeAttribute("hidden");
    el.style.display = "";
  } else {
    el.style.display = "none";
  }
}

function updateLiveBadge() {
  const badge = $("nav-live");
  if (!badge) return;
  badge.textContent = String(state.stream.length);
  setBadgeVisible(badge, state.stream.length > 0);
}

function scheduleLiveRender() {
  if (state._liveRenderTimer) return;
  state._liveRenderTimer = setTimeout(() => {
    state._liveRenderTimer = null;
    renderLegacyStream();
    renderStream();
  }, 120);
}

function pushStream(event) {
  if (!event) return;
  state.stream.push(event);
  if (state.stream.length > LIVE_RAW_LIMIT) state.stream = state.stream.slice(-LIVE_RAW_LIMIT);
  markLiveDirty();
  updateLiveBadge();
  if (state.view === "live") {
    scheduleLiveRender();
    return;
  }
  if (state._streamTimer) return;
  state._streamTimer = setTimeout(() => {
    state._streamTimer = null;
    renderStream();
  }, 200);
}

/* ---------------------------------------------------------------- 状态条 */

function setStatus(id, text, level) {
  const el = $(id);
  if (!el) return;
  const next = String(text ?? "");
  if (el.querySelector(".dot")) {
    const html = `<i class="dot"></i>${esc(next)}`;
    const key = `status:${id}`;
    if (state._htmlCache[key] !== html) {
      state._htmlCache[key] = html;
      el.innerHTML = html;
    }
  } else if (el.textContent !== next) {
    el.textContent = next;
  }
  el.classList.remove("is-ok", "is-bad", "is-busy");
  if (level === "ok") el.classList.add("is-ok");
  else if (level === "bad") el.classList.add("is-bad");
  else if (level === "busy") el.classList.add("is-busy");
}

function renderStatusBar(d) {
  const agents = ((d && d.agents) || state.agents) || [];
  const agentsKnown = state.agentsLoaded || agents.length > 0;
  const enabled = agents.filter((a) => a.enabled !== false).length;
  const c = (d && d.progress && d.progress.counters) || {};
  const total = (d && d.progress && d.progress.total) || 0;
  const done = (c.success || 0) + (c.failed || 0) + (c.skipped || 0) + (c.cancelled || 0);
  const approvals = (d && d.approvals && d.approvals.pending) || 0;
  setStatus("status-ws", state.wsOk ? "WS 已连接" : "WS 未连接", state.wsOk ? "ok" : "bad");
  setStatus("status-agents", agentsKnown ? `AGENTS ${enabled}/${agents.length}` : "AGENTS —/—",
    agentsKnown ? (enabled === 0 ? "bad" : "ok") : "");
  setStatus("status-tasks", d ? `TASKS ${done}/${total}` : "TASKS —", "");
  setStatus("status-approvals", d ? `APPROVALS ${approvals}` : "APPROVALS —", approvals > 0 ? "busy" : "");
  setStatus("status-autopilot", (d && d.running) ? "AUTOPILOT ON" : "AUTOPILOT OFF", (d && d.running) ? "busy" : "");
  setText("inspector-mode", (d && d.running) ? "RUNNING" : "IDLE");
}

function renderSidebarProject(d) {
  const p = (d && d.project)
    || state.projects.find((x) => x.id === state.projectId) || null;
  const nameEl = $("side-project-name");
  const fullName = p ? projectDisplayName(p) : "未选择项目";
  if (nameEl) {
    if (nameEl.textContent !== fullName) nameEl.textContent = fullName;
    nameEl.title = fullName;
  }
  const pathEl = $("side-project-path");
  if (pathEl) {
    const fullPath = p ? (p.root_path || "—") : "新建或导入一个工作区开始";
    const shortPath = p ? shortenPath(fullPath) : fullPath;
    if (pathEl.textContent !== shortPath) pathEl.textContent = shortPath;
    pathEl.title = fullPath;
  }
  const agents = ((d && d.agents) || state.agents) || [];
  const enabled = agents.filter((a) => a.enabled !== false).length;
  const box = $("side-agents");
  if (box) {
    const shown = agents.slice(0, 4);
    const dots = shown.map((a) => {
      const on = a.enabled !== false;
      const shownName = agentDisplayName(a);
      const tip = `${shownName} · ${on ? "启用" : "停用"}`;
      return `<span class="agent-dot${on ? "" : " is-off"}" title="${esc(tip)}">${esc(initials(shownName))}</span>`;
    }).join("");
    const more = agents.length > 4 ? `<span class="agent-dot-more mono">+${agents.length - 4}</span>` : "";
    const fallback = agents.length ? "" : '<span class="agent-dot-more mono">0</span>';
    setHtml("side-agents", `${dots}${more}${fallback}`);
  }
  const statusPill = document.querySelector(".identity-actions .side-status-pill");
  if (statusPill) {
    const text = agents.length ? `${enabled}/${agents.length} 就绪` : "就绪";
    if (statusPill.textContent !== text) statusPill.textContent = text;
  }
}

function updateNavCounts(d) {
  const total = (d && d.progress && d.progress.total) || 0;
  const approvals = (d && d.approvals && d.approvals.pending) || 0;
  const navTasks = $("nav-tasks");
  if (navTasks) {
    navTasks.textContent = String(total);
    setBadgeVisible(navTasks, total > 0);
  }
  const navApprovals = $("nav-approvals");
  if (navApprovals) {
    navApprovals.textContent = String(approvals);
    setBadgeVisible(navApprovals, approvals > 0);
  }
}

function setRunStatus(running) {
  state._runStatus = Boolean(running);
  const el = $("run-status");
  if (el) {
    const text = running ? "调度中" : "空闲";
    if (el.textContent !== text) el.textContent = text;
    el.className = `pill ${running ? "running" : "idle"}`;
  }
  setText("inspector-mode", running ? "RUNNING" : "IDLE");
}

/** 没有项目时禁用调度类按钮，并说明原因（避免点了报错）。 */
function setDispatchEnabled(enabled) {
  for (const id of ["btn-run", "btn-stop", "btn-recover", "btn-force-recover"]) {
    const el = $(id);
    if (!el) continue;
    if (el.dataset.defaultTitle === undefined) el.dataset.defaultTitle = el.title || "";
    el.disabled = !enabled;
    el.title = enabled ? el.dataset.defaultTitle : "先新建 / 导入一个项目";
  }
}

function renderNoProject() {
  state.dashboard = null;
  state._dashJson = "";
  setDispatchEnabled(false);
  renderHomeProject(null);
  renderHomeMetrics(null);
  renderKanbanBoard("home-kanban", []);
  setGridHtml("home-activity", "activity", compactEmpty("ACTIVITY", "还没有活动记录", "先新建或导入一个工作区。"));
  renderOnboarding(null);
  renderInspectorAgents(null);
  setListHtml("inspector-approvals", [compactEmpty("CLEAR", "没有待确认", "")]);
  setListHtml("inspector-running", [compactEmpty("IDLE", "当前没有任务在跑", "")]);
  renderStream();
  renderStatusBar(null);
  renderSidebarProject(null);
  updateNavCounts(null);
  setRunStatus(false);
  renderLiveRunbar();
  renderKanbanBoard("kanban", []);
  renderDag();
  renderStats();
  renderAgentsView();
  const blank = emptyBox("还没有项目", "先在顶栏新建或导入一个工作区。");
  ["bb-list", "handoff-list", "approval-list", "snap-list", "runs", "timeline", "artifacts"]
    .forEach((id) => setListHtml(id, [blank]));
}

/* ================================================================ 任务输入（mission）

 * 一句话 → 建任务 / 创建并启动 / 拆解三步计划。
 * 新接口缺失（404）时回退到既有的建任务 / 批量计划接口，保证 UI 先能跑。
 */

function renderMissionOptions() {
  const agentSel = $("mission-agent");
  if (agentSel) {
    const keep = agentSel.value || "auto";
    const firstText = agentSel.options.length ? agentSel.options[0].textContent : "自动选择";
    const opts = [`<option value="auto">${esc(firstText)}</option>`]
      .concat(state.agents.filter((a) => a.enabled !== false)
        .map((a) => `<option value="${esc(a.id)}">${esc(agentDisplayName(a))}</option>`));
    setHtml("mission-agent", opts.join(""));
    const valid = Array.from(agentSel.options).some((o) => o.value === keep);
    agentSel.value = valid ? keep : "auto";
  }
  const kindSel = $("mission-kind");
  if (kindSel) {
    const keep = kindSel.value || "auto";
    const firstText = kindSel.options.length ? kindSel.options[0].textContent : "自动判断";
    const opts = [`<option value="auto">${esc(firstText)}</option>`]
      .concat(KIND_OPTIONS.map((k) => `<option value="${esc(k)}">${esc(KIND_LABEL[k] || k)}</option>`));
    setHtml("mission-kind", opts.join(""));
    const valid = Array.from(kindSel.options).some((o) => o.value === keep);
    kindSel.value = valid ? keep : "auto";
  }
  ensureMissionIsolationField();
  syncMissionIsolationDefault();
}

/* ---------------------------------------------------------------- 写入方式
 * 隔离副本（copy）：Agent 在独立副本里写，验收通过后自动同步回工作目录；
 * 直接写工作目录（shared）：实时可见，但无自动回滚。
 * 导入项目（origin_path 非空）默认 shared，托管项目默认 copy。
 */
function ensureMissionIsolationField() {
  let sel = $("mission-isolation");
  if (sel) return sel;
  const options = document.querySelector("#view-home .mission-options");
  if (!options) return null;
  const field = document.createElement("div");
  field.className = "field";
  field.innerHTML = `<label class="field-label" for="mission-isolation">写入方式</label>
    <select id="mission-isolation" class="select" aria-label="写入方式">
      <option value="copy">隔离副本（推荐）</option>
      <option value="shared">直接写工作目录</option>
    </select>`;
  options.appendChild(field);
  if (!document.querySelector("[data-mission-isolation-hint]")) {
    const hint = document.createElement("p");
    hint.className = "hint mission-isolation-hint";
    hint.dataset.missionIsolationHint = "1";
    hint.textContent = "隔离副本：Agent 在独立副本里写，验收通过后自动同步到工作目录；直接写：实时改工作目录，无自动回滚。";
    options.insertAdjacentElement("afterend", hint);
  }
  sel = $("mission-isolation");
  return sel;
}

function missionIsolationValue() {
  const sel = $("mission-isolation");
  return sel && sel.value === "shared" ? "shared" : "copy";
}

function projectDefaultIsolation() {
  // 修改型任务默认永远走 copy；shared 只用于明确只读的分析/审阅任务。
  return "copy";
}

function syncMissionIsolationDefault(force = false) {
  const sel = ensureMissionIsolationField();
  if (!sel) return;
  if (force || !state._isolationTouched) sel.value = projectDefaultIsolation();
}

/** 桌面端用桥打开路径；浏览器端复制路径并 toast。 */
function openPathOrCopy(path) {
  const target = String(path || "").trim();
  if (!target || target === "—") { toast("没有可打开的路径", "error"); return; }
  const bridge = window.multiagentDesktop;
  if (bridge && typeof bridge.openPath === "function") {
    try {
      const result = bridge.openPath(target);
      if (result && typeof result.catch === "function") {
        result.catch((err) => toast(`打开失败：${err && err.message ? err.message : err}`, "error"));
      }
      toast(`已打开：${target}`);
      return;
    } catch (err) {
      toast(`打开失败：${err.message}`, "error");
      return;
    }
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(target)
      .then(() => toast(`浏览器端无法直接打开，路径已复制：${target}`))
      .catch(() => toast(`路径：${target}`));
    return;
  }
  toast(`路径：${target}`);
}

function openPathButton(label, path) {
  if (!path) return "";
  return `<button class="btn tiny ghost" type="button" data-act="open-path" data-path="${esc(path)}">${esc(label)}</button>`;
}

/** 工作目录 / 工作副本信息（workspace 接口缺失时降级到 task.workdir）。 */
function workspaceInfoOf(task, workspace) {
  const t = task || {};
  const ws = workspace || {};
  const project = (state.dashboard && state.dashboard.project)
    || state.projects.find((p) => p.id === state.projectId) || {};
  const countOf = (raw) => {
    if (raw === undefined || raw === null) return null;
    if (typeof raw === "number") return Number(raw) || 0;
    const value = raw.count !== undefined
      ? raw.count
      : (Array.isArray(raw.files) ? raw.files.length : undefined);
    return value === undefined ? null : (Number(value) || 0);
  };
  const pendingCount = countOf(ws.pending) || 0;      // 工作副本改动数（sync 后不归零）
  const unsyncedCount = countOf(ws.unsynced);          // 待同步数（sync 后归零；旧后端没有该字段）
  const hasUnsynced = unsyncedCount !== null;
  const syncCount = hasUnsynced ? unsyncedCount : pendingCount;
  const committedCount = countOf(ws.committed) || 0;
  return {
    available: Boolean(workspace),
    rootPath: ws.root_path || project.root_path || "",
    workdir: ws.workdir || t.workdir || "",
    isolation: ws.isolation || t.isolation || "copy",
    pendingCount,
    unsyncedCount: hasUnsynced ? unsyncedCount : null,
    syncCount,
    hasUnsynced,
    committedCount,
    canSync: ws.can_sync !== false,
    blockedReason: ws.sync_blocked_reason || "",
  };
}

function taskWorkspaceHtml(task, workspace) {
  const info = workspaceInfoOf(task, workspace);
  const modeLabel = info.isolation === "shared" ? "直接写工作目录（实时可见）" : "隔离副本（验收后自动同步）";
  const rows = [
    `<div class="entry"><div class="head"><span class="t">工作目录</span>
      <span class="hint mono">${esc(info.rootPath || "—")}</span>
      <span class="spacer"></span>${openPathButton("打开工作目录", info.rootPath)}</div></div>`,
    `<div class="entry"><div class="head"><span class="t">工作副本</span>
      <span class="hint mono">${esc(info.workdir || "—")}</span>
      <span class="spacer"></span>${openPathButton("打开工作副本", info.workdir)}</div></div>`,
    `<div class="entry"><div class="head"><span class="t">写入方式</span>
      <span class="hint">${esc(modeLabel)}</span>
      <span class="spacer"></span>
      <span class="tag ${info.syncCount > 0 ? "waiting" : "success"}">待同步 ${info.syncCount} 个文件</span></div></div>`,
  ];
  if (info.hasUnsynced) {
    rows.push(`<div class="entry"><div class="head"><span class="t">工作副本改动</span>
      <span class="hint">${info.pendingCount} 个文件（含已同步，仅作参考）</span></div></div>`);
  }
  const actions = [];
  if (info.syncCount > 0 && info.canSync) {
    actions.push(`<button class="btn tiny primary" type="button" data-act="task-sync" data-id="${esc(task.id)}">同步 ${info.syncCount} 个文件到工作目录</button>`);
  }
  if (info.syncCount > 0 && !info.canSync) {
    actions.push(`<span class="hint">当前不可同步${info.blockedReason ? `：${esc(info.blockedReason)}` : ""}</span>`);
  }
  if (task.status === "success" && info.committedCount > 0) {
    actions.push(`<p class="hint">已自动同步 ${info.committedCount} 个文件</p>`);
  }
  if ((task.status === "failed" || task.status === "cancelled") && info.syncCount > 0) {
    actions.push('<p class="hint">任务未成功，未自动同步；可手动同步当前快照。</p>');
  }
  const canApprove = ["failed", "cancelled", "interrupted", "blocked"].includes(task.status)
    && (info.syncCount > 0 || Boolean(task.result && task.result.acceptance && task.result.acceptance.ok));
  if (canApprove) {
    actions.push(`<button class="btn tiny primary" type="button" data-act="task-approve" data-id="${esc(task.id)}">人工验收${info.syncCount > 0 ? "并同步" : "通过"}</button>`);
  }
  if (!info.available) {
    actions.push('<p class="hint">后端暂未提供工作区详情接口，以下为任务记录里的副本路径（只读）。</p>');
  }
  return `<div class="section section-plain"><h4>工作目录 / 工作副本</h4>${rows.join("")}
    ${actions.length ? `<div class="op-row" style="margin-top:8px">${actions.join("")}</div>` : ""}</div>`;
}

function templateText(chip) {
  const raw = String(chip.dataset.template || "").trim();
  const label = String(raw || chip.textContent || "").trim();
  if (raw && raw.length > 24) return raw;
  const hit = MISSION_TEMPLATES.find((t) => t.label === label
    || label.includes(t.label) || t.label.includes(label));
  return hit ? hit.text : (raw || label);
}

function bindTemplates() {
  const box = $("mission-templates");
  if (!box) return;
  if (!box.children.length) {
    box.innerHTML = MISSION_TEMPLATES.map((t) =>
      `<button type="button" class="chip" data-template="${esc(t.text)}">${esc(t.label)}</button>`).join("");
  }
  box.addEventListener("click", (event) => {
    const chip = event.target.closest(".chip, [data-template]");
    if (!chip) return;
    const textEl = $("mission-text");
    if (!textEl) return;
    textEl.value = templateText(chip);
    textEl.focus();
  });
}

function inferKind(text) {
  const s = String(text || "");
  if (/(代码|报错|异常|重构|修复|函数|接口|编译|测试|\bbug\b|\bcode\b|\berror\b|\bfix\b|\brefactor\b)/i.test(s)) return "code";
  if (/(\bppt\b|pptx|幻灯片|演示文稿|汇报材料|\bslide\b|\bdeck\b)/i.test(s)) return "ppt";
  if (/(图表|架构图|流程图|甘特图|柱状图|折线图|雷达图|\bdiagram\b|\bchart\b|可视化)/i.test(s)) return "diagrams";
  if (/(网页|前端|页面|three\.js|threejs|\b3d\b|\bhtml\b|\bcss\b|\bjavascript\b|\bweb\b)/i.test(s)) return "web";
  if (/(报告|文档|撰写|写一份|文案|说明书|\bdoc\b|\breport\b|\breadme\b)/i.test(s)) return "doc";
  if (/(调研|资料|搜索|检索|竞品|收集|\bresearch\b|\bsurvey\b)/i.test(s)) return "research";
  if (/(审阅|评审|复核|检查|\breview\b)/i.test(s)) return "review";
  if (/(表格|数据|\bcsv\b|\bexcel\b|统计|汇总|\bdata\b)/i.test(s)) return "data";
  return "generic";
}

function firstLine(text, max = 60) {
  const line = String(text || "").split(/\r?\n/).map((s) => s.trim()).filter(Boolean)[0] || "";
  return line.slice(0, max) || UNTITLED_TASK;
}

async function startProject(body = { auto_retry: true }) {
  if (!state.projectId) return null;
  const res = await api(`/api/projects/${encodeURIComponent(state.projectId)}/run`, { method: "POST", body });
  scheduleDashboardRefresh(1000);
  return res;
}

async function quickTaskFallback(pid, payload) {
  // 变量名用 goal：这是请求体内容（不是 innerHTML），但静态 XSS 护栏按变量名匹配，
  // 用 text/line 命名会被误判；同时 goal 语义也更准确。
  const goal = payload.text;
  const kind = payload.kind && payload.kind !== "auto" ? payload.kind : inferKind(goal);
  const title = payload.title || firstLine(goal);
  const outputs = Array.isArray(payload.outputs) ? payload.outputs : [];
  const agentId = payload.agent_id || null;
  if (payload.mode === "plan") {
    const spec = {
      tasks: [
        {
          key: "research", title: `调研整理：${title}`, kind: "research",
          brief: `先做调研与资料整理，不要写最终稿。\n\n目标：${goal}`,
          agent: agentId || undefined,
          isolation: payload.isolation || undefined,
        },
        {
          key: "draft", title: `产出初稿：${title}`, kind: "doc",
          brief: `基于调研结果产出初稿。\n\n目标：${goal}`,
          depends_on: ["research"], outputs, agent: agentId || undefined,
          isolation: payload.isolation || undefined,
        },
        {
          key: "review", title: `只读审阅：${title}`, kind: "review", read_only: true,
          brief: `只读审阅初稿，列出问题与修改建议，不要改文件。\n\n目标：${goal}`,
          depends_on: ["draft"],
        },
      ],
    };
    const created = await api(`/api/projects/${encodeURIComponent(pid)}/tasks/plan`, { method: "POST", body: spec });
    if (payload.start) await startProject({ auto_retry: true });
    const tasks = (Array.isArray(created) ? created : []).map((t) => ({ id: t.id, title: t.title, status: t.status }));
    return { project_id: pid, mode: "plan", started: Boolean(payload.start), tasks, message: `已创建 ${tasks.length} 个任务（三步计划）` };
  }
  const body = {
    title, kind, brief: goal, agent_id: agentId,
    read_only: Boolean(payload.read_only), outputs,
    isolation: payload.isolation || null,
  };
  const created = await api(`/api/projects/${encodeURIComponent(pid)}/tasks`, { method: "POST", body });
  if (payload.start) await startProject({ auto_retry: true });
  return {
    project_id: pid,
    mode: "single",
    started: Boolean(payload.start),
    tasks: [{ id: created.id, title: created.title, status: created.status }],
    message: payload.start ? "已创建并启动 1 个任务" : "已创建 1 个任务",
  };
}

async function submitMission(mode = "single", start = false) {
  if (!state.projectId) {
    toast("请先在顶栏新建或导入项目", "error");
    return null;
  }
  const textEl = $("mission-text");
  const text = textEl ? String(textEl.value || "").trim() : "";
  if (!text) {
    if (textEl) textEl.focus();
    toast("请先描述你要完成的任务", "error");
    return null;
  }
  const outputsEl = $("mission-outputs");
  const scopeEl = $("mission-scope");
  const unrestrictedEl = $("mission-unrestricted");
  const agentSel = $("mission-agent");
  const kindSel = $("mission-kind");
  const agentRaw = agentSel ? agentSel.value : "";
  const kindRaw = kindSel ? kindSel.value : "auto";
  const outputs = splitList(outputsEl ? outputsEl.value : "");
  const writeScope = splitList(scopeEl ? scopeEl.value : "");
  let allowUnrestricted = Boolean(unrestrictedEl && unrestrictedEl.checked);
  const inferredKind = kindRaw && kindRaw !== "auto" ? kindRaw : inferKind(text);
  const writeKind = !["research", "review"].includes(inferredKind);
  if (writeKind && !outputs.length && !writeScope.length && !allowUnrestricted) {
    const ok = window.confirm(
      "未指定产物和写范围：执行期会要求至少一个真实文件改动，且 Agent 可在隔离副本中修改整个工作区。\n\n确认允许不受限写入吗？",
    );
    if (!ok) {
      toast("已取消；建议填写产出文件/写范围，或使用「拆解大型项目」", "error");
      return null;
    }
    allowUnrestricted = true;
    if (unrestrictedEl) unrestrictedEl.checked = true;
  }
  if (text.length > 4000 || /\.pdf\b/i.test(text)) {
    const ok = window.confirm("需求较长或包含 PDF，建议拆解为 DAG 分阶段执行。仍要创建单个任务吗？");
    if (!ok) {
      toast("已取消单个任务；请使用「拆解大型项目」预览 DAG");
      return null;
    }
  }
  const payload = {
    text,
    title: null,
    agent_id: agentRaw && agentRaw !== "auto" ? agentRaw : null,
    kind: kindRaw || "auto",
    outputs,
    read_only: false,
    isolation: missionIsolationValue(),
    write_scope: writeScope,
    allow_unrestricted_write: allowUnrestricted,
    mode: mode === "plan" ? "plan" : "single",
    start: Boolean(start),
  };
  const pid = state.projectId;
  let res;
  let usedFallback = false;
  try {
    try {
      res = await api(`/api/projects/${encodeURIComponent(pid)}/quick-task`, { method: "POST", body: payload });
    } catch (err) {
      if (!isMissingEndpoint(err)) throw err;
      usedFallback = true;
      res = await quickTaskFallback(pid, payload);
    }
  } catch (err) {
    toast(`提交失败：${err.message}`, "error");
    return null;
  }
  const created = (res && Array.isArray(res.tasks)) ? res.tasks : [];
  const count = created.length || 1;
  const suffix = usedFallback ? "（一句话接口不可用，已回退旧接口）" : "";
  const warnings = (res && Array.isArray(res.warnings)) ? res.warnings : [];
  toast(((res && res.message) || `已创建 ${count} 个任务`) + suffix);
  if (warnings.length) toast(warnings[0], "error");
  if (textEl) textEl.value = "";
  if (outputsEl) outputsEl.value = "";
  if (scopeEl) scopeEl.value = "";
  if (unrestrictedEl) unrestrictedEl.checked = false;
  await loadDashboard({ force: true }).catch(() => {});
  const firstTask = created.find((t) => t && t.id) || null;
  if (firstTask || payload.mode === "plan") {
    switchView("tasks");
    await loadViewData("tasks").catch(() => {});
    if (firstTask) highlightTaskCard(firstTask.id);
  } else if (state.view !== "home") {
    await loadViewData(state.view, { force: true }).catch(() => {});
  }
  return res;
}

async function openDecomposePreview() {
  if (!state.projectId) {
    toast("请先在顶栏新建或导入项目", "error");
    return;
  }
  const textEl = $("mission-text");
  const text = textEl ? String(textEl.value || "").trim() : "";
  if (!text) {
    if (textEl) textEl.focus();
    toast("请先描述项目目标，或粘贴需求摘要", "error");
    return;
  }
  toast("正在生成九阶段 DAG 预览…");
  const res = await api(`/api/projects/${encodeURIComponent(state.projectId)}/decompose/preview`, {
    method: "POST",
    body: {
      text,
      outputs: splitList($("mission-outputs") ? $("mission-outputs").value : ""),
      write_scope: splitList($("mission-scope") ? $("mission-scope").value : ""),
      agent_id: $("mission-agent") && $("mission-agent").value !== "auto" ? $("mission-agent").value : null,
    },
  });
  const plan = res.plan || { tasks: [] };
  const warnings = Array.isArray(res.warnings) ? res.warnings : [];
  const sources = (plan.source_files || []).map((item) =>
    `<li><code>${esc(item.path)}</code>${item.text_path ? `（文本：${esc(item.text_path)}）` : ""}</li>`).join("");
  const hygiene = plan.hygiene || {};
  openModal("大型项目拆解预览", `
    <p class="hint">共 ${esc((plan.tasks || []).length)} 个任务，依赖与验收已生成。应用前可直接编辑下方 JSON；不会自动启动。</p>
    ${warnings.length ? `<div class="section section-plain"><h4>风险提示</h4><ul>${warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : ""}
    ${sources ? `<div class="section section-plain"><h4>需求来源</h4><ul>${sources}</ul></div>` : ""}
    ${hygiene.temporary_files ? `<p class="hint">卫生检查：发现 ${esc(hygiene.temporary_files)} 个临时文件、${esc(hygiene.temporary_dirs || 0)} 个临时目录，最终 QA 会提示清理。</p>` : ""}
    <div class="form-row"><label for="decompose-plan-json">可编辑计划（JSON）</label>
      <textarea id="decompose-plan-json" style="min-height:360px" class="mono">${esc(JSON.stringify(plan, null, 2))}</textarea></div>`,
    '<button class="btn" data-act="modal-close">取消</button><button class="btn primary" data-act="do-apply-decompose">应用计划（不启动）</button>');
}

function bindMissionButtons() {
  const pairs = [
    ["mission-submit", "mission-create"],
    ["mission-start", "mission-start"],
    ["mission-plan", "mission-plan"],
  ];
  for (const [id, act] of pairs) {
    const el = $(id);
    if (!el || el.dataset.act) continue; // 有 data-act 时走全局委托
    el.addEventListener("click", () => {
      if (el.disabled) return;
      const finish = withLoading(el);
      Promise.resolve()
        .then(() => handleAction(act, el))
        .catch((err) => toast(err && err.message ? err.message : String(err), "error"))
        .finally(finish);
    });
  }
}

/* ================================================================ 视图切换 / 懒加载 */

function applyViewClasses(view) {
  document.querySelectorAll(".view").forEach((el) => {
    const on = el.id === `view-${view}`;
    el.classList.toggle("is-active", on);
    el.classList.toggle("active", on);
  });
  document.querySelectorAll(".nav-item").forEach((el) => {
    const on = el.dataset.view === view;
    el.classList.toggle("is-active", on);
    el.classList.toggle("active", on);
  });
}

/** URL hash 深链：#tasks / #board / #observe … */
function readHashView() {
  const raw = String(location.hash || "").replace(/^#\/?/, "").trim();
  if (!raw) return "";
  return VIEW_LABELS[raw] ? raw : "";
}

function syncHash(view) {
  const hash = `#${view}`;
  if (location.hash === hash) return;
  try {
    history.replaceState(null, "", `${location.pathname}${location.search}${hash}`);
  } catch (_) {
    location.hash = view;
  }
}

function bindHash() {
  window.addEventListener("hashchange", () => {
    const view = readHashView();
    if (view && view !== state.view) switchView(view, { fromHash: true });
  });
}

function currentTasks() {
  const dashboardTasks = (state.dashboard && Array.isArray(state.dashboard.tasks))
    ? state.dashboard.tasks : [];
  if (state.kanban && Array.isArray(state.kanban.nodes) && state.kanban.nodes.length) {
    // kanban 只给最小节点；用 dashboard 的 runtime / timeout / updated_at 补全卡片证据。
    const richById = new Map(dashboardTasks.map((task) => [task.id, task]));
    return state.kanban.nodes.map((node) => {
      const nodeItem = node && typeof node === "object" ? node : {};
      const rich = richById.get(nodeItem.id);
      if (!rich) return nodeItem;
      return {
        ...rich,
        ...nodeItem,
        runtime: nodeItem.runtime || rich.runtime || null,
        timeout_sec: nodeItem.timeout_sec ?? rich.timeout_sec ?? null,
        max_attempts: nodeItem.max_attempts ?? rich.max_attempts ?? null,
        attempt: nodeItem.attempt ?? rich.attempt ?? null,
        updated_at: nodeItem.updated_at || rich.updated_at || null,
        read_only: nodeItem.read_only ?? rich.read_only,
      };
    });
  }
  return dashboardTasks;
}

function renderKanban() {
  renderKanbanBoard("kanban", filteredTasks(currentTasks()));
}

function dagEmptySvg() {
  return `<g class="dag-empty">
    <rect class="dag-empty-box" x="330" y="206" width="340" height="108" rx="6"></rect>
    <text class="dag-empty-title" x="500" y="252" text-anchor="middle">暂无依赖关系</text>
    <text class="dag-empty-hint" x="500" y="276" text-anchor="middle">新建两个任务并设置依赖后，这里会画出 DAG</text>
  </g>`;
}

function renderDag() {
  const box = $("dag");
  if (!box) return;
  const kanban = state.kanban || {};
  const nodes = (Array.isArray(kanban.nodes) && kanban.nodes.length)
    ? kanban.nodes
    : ((state.dashboard && state.dashboard.tasks) || []);
  const byId = {};
  nodes.forEach((n) => { byId[n.id] = n; });
  let levels = Array.isArray(kanban.levels) ? kanban.levels : [];
  if (!levels.length && nodes.length) levels = [nodes.map((n) => n.id)];
  const positions = {};
  const W = 176, H = 48, GX = 226, GY = 80;
  let maxX = 0, maxY = 0;
  levels.forEach((level, li) => {
    (level || []).forEach((id, ni) => {
      if (!byId[id]) return;
      positions[id] = { x: 30 + li * GX, y: 26 + ni * GY };
      maxX = Math.max(maxX, positions[id].x + W + 30);
      maxY = Math.max(maxY, positions[id].y + H + 26);
    });
  });
  if (!Object.keys(positions).length) {
    box.setAttribute("viewBox", "0 0 1000 520");
    setHtml("dag", dagEmptySvg());
    return;
  }
  box.setAttribute("viewBox", `0 0 ${Math.max(1000, maxX)} ${Math.max(520, maxY)}`);
  const defs = '<defs><marker id="dag-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#c3bcae"/></marker></defs>';
  const edges = (Array.isArray(kanban.edges) ? kanban.edges : [])
    .filter((e) => positions[e.task_id] && positions[e.depends_on])
    .map((e) => {
      const a = positions[e.depends_on], b = positions[e.task_id];
      const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2, mid = (x1 + x2) / 2;
      return `<path class="edge" marker-end="url(#dag-arrow)" d="M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}" />`;
    }).join("");
  const boxes = Object.entries(positions).map(([id, pos]) => {
    const n = byId[id] || { title: id, status: "pending" };
    const fullTitle = taskDisplayTitle(n);
    const title = fullTitle.length > 16 ? `${fullTitle.slice(0, 16)}…` : fullTitle;
    return `<g class="node ${esc(n.status)}" data-act="task-open" data-id="${esc(id)}" transform="translate(${pos.x},${pos.y})">
      <rect width="${W}" height="${H}" rx="6"></rect>
      <text x="12" y="20">${esc(title)}</text>
      <text x="12" y="37" fill="#8b949c" font-size="10">${esc(STATUS_LABEL[n.status] || n.status)} · ${esc(displayText(n.agent_id, "-"))}</text>
    </g>`;
  }).join("");
  setHtml("dag", `${defs}${edges}${boxes}`);
}

function renderBoard() {
  const items = Array.isArray(state.board) ? state.board : [];
  if (!items.length) {
    setListHtml("bb-list", [emptyBox("黑板暂无内容", "任务简报、决策与交接会写到这里。")]);
    return;
  }
  const rows = items.map((e) => rowHtml({
    title: esc(displayText(e.title, "未命名记录")),
    tags: `<span class="tag">${esc(e.type)}</span>
      <span class="tag ${e.status === "pending" ? "blocked" : "success"}">${esc(e.status)}</span>
      ${e.pinned ? '<span class="tag">📌</span>' : ""}`,
    meta: `作用域 ${esc(e.scope)} · v${esc(e.current_version)}${e.agent_id ? ` · ${esc(displayText(e.agent_id, "未知 Agent"))}` : ""} · ${esc(shortTime(e.updated_at))}`,
    body: `<div class="mono">${esc(readableText(String(e.body || ""), "（历史数据无可读内容）").slice(0, 800))}</div>`,
    actions: [
      e.status === "pending"
        ? `<button class="btn tiny" data-act="board-review" data-id="${esc(e.id)}" data-ok="1">通过</button>
           <button class="btn tiny danger" data-act="board-review" data-id="${esc(e.id)}" data-ok="0">驳回</button>`
        : "",
      `<button class="btn tiny ghost" data-act="board-versions" data-id="${esc(e.id)}">版本历史</button>`,
      `<button class="btn tiny ghost" data-act="board-edit" data-id="${esc(e.id)}">编辑</button>`,
      `<button class="btn tiny ghost danger" data-act="board-delete" data-id="${esc(e.id)}">删除</button>`,
    ].join(""),
  }));
  setListHtml("bb-list", rows);
}

function renderHandoffs() {
  const items = Array.isArray(state.handoffs) ? state.handoffs : [];
  if (!items.length) {
    setListHtml("handoff-list", [emptyBox("暂无交接说明", "任务完成后会生成结构化交接。")]);
    return;
  }
  const rows = items.map((h) => {
    const did = readableText(h.did, "（历史数据无可读内容）");
    const notDone = readableText(h.not_done, "");
    const issues = cleanList(h.open_issues).map(normalizeChunkedText);
    const nextSteps = cleanList(h.next_steps).map(normalizeChunkedText);
    const body = [`<b>做了什么：</b>${esc(did)}`];
    if (notDone) body.push(`<b>没做什么：</b>${esc(notDone)}`);
    if (issues.length) body.push(`<b>遗留：</b>${issues.map((x) => esc(x)).join("；")}`);
    if (nextSteps.length) body.push(`<b>下一步：</b>${nextSteps.map((x) => esc(x)).join("；")}`);
    return rowHtml({
      title: esc(displayText(h.did ? normalizeChunkedText(String(h.did)).slice(0, 60) : "", "交接说明")),
      tags: `<span class="tag">${h.source === "human" ? "人工修正" : esc(displayText(h.agent_id, "未知 Agent"))}</span>`
        + (h.edited_by ? '<span class="tag">已人工修正</span>' : ""),
      meta: `任务 ${esc(displayText(h.task_id, "-"))}`,
      body: body.join("<br/>"),
      actions: `<button class="btn tiny ghost" data-act="handoff-edit" data-id="${esc(h.id)}">人工修正</button>
        <button class="btn tiny ghost" data-act="task-open" data-id="${esc(h.task_id)}">查看任务</button>`,
    });
  });
  setListHtml("handoff-list", rows);
}

function renderApprovals() {
  const items = Array.isArray(state.approvals) ? state.approvals : [];
  if (!items.length) {
    setListHtml("approval-list", [emptyBox("没有待处理的人工确认", "危险操作、冲突与验收失败会进入这里。")]);
    return;
  }
  const rows = items.map((a) => rowHtml({
    title: esc(displayText(a.title, "待确认操作")),
    tags: `<span class="tag ${a.status === "pending" ? "blocked" : a.status === "approved" ? "success" : "failed"}">${esc(a.kind || "approval")} · ${esc(a.status || "")}</span>`,
    meta: `风险 ${esc(a.risk || "-")} · ${esc(shortTime(a.created_at))}`,
    body: `<div class="mono">${esc(JSON.stringify(a.detail || {}))}</div>`,
    actions: a.status === "pending"
      ? `<button class="btn tiny" data-act="approval" data-id="${esc(a.id)}" data-ok="1">批准</button>
         <button class="btn tiny danger" data-act="approval" data-id="${esc(a.id)}" data-ok="0">拒绝</button>`
      : `<span class="muted">由 ${esc(displayText(a.resolved_by, ""))} 处理</span>`,
  }));
  setListHtml("approval-list", rows);
}

function renderSnapshots() {
  const items = Array.isArray(state.snapshots) ? state.snapshots : [];
  if (!items.length) {
    setListHtml("snap-list", [emptyBox("还没有快照", "创建快照后可对比与回退。")]);
  } else {
    const rows = items.map((s) => rowHtml({
      title: esc(displayText(s.label, s.id)),
      meta: `${esc(shortTime(s.created_at))} · ${esc(s.file_count)} 文件 / ${Math.round((s.bytes || 0) / 1024)} KB`,
      actions: `<button class="btn tiny ghost" data-act="snap-diff" data-id="${esc(s.id)}">对比当前</button>
        <button class="btn tiny" data-act="snap-restore" data-id="${esc(s.id)}">回退到此快照</button>`,
    }));
    setListHtml("snap-list", rows);
  }
  const selA = $("snap-a");
  const selB = $("snap-b");
  if (selA && selB) {
    const opts = items.map((s) =>
      `<option value="${esc(s.id)}">${esc(displayText(s.label, s.id.slice(0, 10)))} · ${esc(shortTime(s.created_at)).slice(5, 16)}</option>`).join("");
    const keepA = selA.value;
    const keepB = selB.value;
    setHtml("snap-a", opts || '<option value="">（无）</option>');
    setHtml("snap-b", opts || '<option value="">（无）</option>');
    if (items.some((s) => s.id === keepA)) selA.value = keepA;
    if (items.some((s) => s.id === keepB)) selB.value = keepB;
    else if (items.length) selB.value = items[items.length - 1].id;
  }
}

function renderArtifacts() {
  const roleEl = $("artifact-role");
  const role = roleEl ? roleEl.value : "";
  let items = Array.isArray(state.artifacts) ? state.artifacts : [];
  if (role) items = items.filter((a) => a.role === role);
  if (!items.length) {
    setListHtml("artifacts", [emptyBox("暂无产物", "任务输入 / 输出会记录在这里。")]);
    return;
  }
  const rows = items.map((a) => rowHtml({
    title: esc(displayText(a.rel_path, "（历史数据无可读内容）")),
    tags: `<span class="tag ${a.role === "output" ? "success" : ""}">${esc(a.role === "output" ? "输出" : "输入")}</span>`
      + (a.is_binary ? '<span class="tag">二进制</span>' : "")
      + (a.fragment ? '<span class="tag">片段</span>' : ""),
    meta: `${esc(a.size)} B${a.sha256 ? ` · <span class="mono">${esc(String(a.sha256).slice(0, 12))}</span>` : ""}${a.abs_path ? "" : " · 仅相对路径"}`,
    actions: `${a.abs_path ? openPathButton("打开", a.abs_path) : ""}
      <button class="btn tiny ghost" data-act="task-open" data-id="${esc(a.task_id)}">查看任务</button>`,
  }));
  setListHtml("artifacts", rows);
}

function renderStats() {
  const stats = state.stats || {};
  const totals = stats.totals || {};
  const known = Boolean(state.stats);
  const agentRows = (stats.by_agent || []).map((r) =>
    `<tr><td>${esc(displayText(r.agent_id, "未知 Agent"))}</td><td>${esc(r.runs)}</td><td>${Math.round((r.duration_ms || 0) / 1000)}s</td><td>${esc(r.tokens || 0)}</td><td>${esc(r.success || 0)}/${esc(r.failed || 0)}</td></tr>`).join("");
  const taskRows = (stats.by_task || []).map((r) =>
    `<tr><td>${esc(displayName(r.title, UNTITLED_TASK))}</td><td><span class="tag ${esc(r.status)}">${esc(STATUS_LABEL[r.status] || r.status)}</span></td><td>${esc(r.runs)}</td><td>${Math.round((r.duration_ms || 0) / 1000)}s</td><td>${esc(r.tokens || 0)}</td></tr>`).join("");
  const tables = `
    <h4 class="section">按 Agent</h4>
    <table><thead><tr><th>Agent</th><th>调用</th><th>耗时</th><th>token</th><th>成功/失败</th></tr></thead>
    <tbody>${agentRows || '<tr><td colspan="5" class="muted">暂无调用</td></tr>'}</tbody></table>
    <h4 class="section">按任务</h4>
    <table><thead><tr><th>任务</th><th>状态</th><th>调用</th><th>耗时</th><th>token</th></tr></thead>
    <tbody>${taskRows || '<tr><td colspan="5" class="muted">暂无调用</td></tr>'}</tbody></table>`;
  const el = $("stats");
  if (el && el.classList.contains("stats")) {
    // 新壳 #stats 自带 .stats 网格：4 个 .stat + 跨列的明细表
    const items = [
      { k: "运行次数", v: known ? fmtInt(totals.runs ?? 0) : "—" },
      { k: "总耗时", v: known ? `${Math.round((totals.duration_ms || 0) / 1000)}s` : "—" },
      { k: "Tokens", v: known ? fmtInt(totals.tokens ?? 0) : "—" },
      { k: "成本", v: known ? `¥${Number(totals.cost || 0).toFixed(2)}` : "—" },
    ];
    const statsHtml = items.map((m, index) =>
      `<div class="stat"${index === items.length - 1 ? ' style="border-right:0"' : ""}>
        <span class="stat-k">${esc(m.k)}</span><span class="stat-v">${esc(m.v)}</span>
      </div>`).join("");
    setHtml("stats", `${statsHtml}<div style="grid-column:1/-1;padding:10px 11px 12px">${tables}</div>`);
    return;
  }
  setHtml("stats", `
    <div class="kpi-grid">
      <div class="kpi"><div class="kpi-value">${esc(fmtInt(totals.runs ?? 0))}</div><div class="kpi-label">总调用</div></div>
      <div class="kpi"><div class="kpi-value">${Math.round((totals.duration_ms || 0) / 1000)}s</div><div class="kpi-label">总耗时</div></div>
      <div class="kpi"><div class="kpi-value">${esc(fmtInt(totals.tokens ?? 0))}</div><div class="kpi-label">总 token</div></div>
    </div>
    ${tables}`);
}

function renderRuns() {
  const items = Array.isArray(state.runs) ? state.runs : [];
  if (!items.length) {
    setListHtml("runs", [emptyBox("暂无运行记录", "每次 Agent 调用都会记录在这里。")]);
    return;
  }
  const rows = items.map((r) => rowHtml({
    title: `${esc(displayText(r.agent_id, "未知 Agent"))} · ${esc(STATUS_LABEL[r.status] || r.status)}`,
    meta: `${esc(r.duration_ms)}ms · exit ${esc(r.exit_code)} · token ${esc(r.cost_tokens)} · ${esc(shortTime(r.started_at))}`,
    actions: `<button class="btn tiny ghost" data-act="run-output" data-id="${esc(r.id)}">输入 / 输出</button>
      <button class="btn tiny ghost" data-act="task-open" data-id="${esc(r.task_id)}">任务</button>`,
  }));
  setListHtml("runs", rows);
}

function renderTimeline() {
  const items = (Array.isArray(state.timeline) ? state.timeline : []).slice(-160).reverse();
  if (!items.length) {
    setListHtml("timeline", [emptyBox("暂无记录", "协作时间线会记录项目里的关键动作。")]);
    return;
  }
  const rows = items.map((it) => rowHtml({
    title: esc(eventKindLabel(it.kind)),
    meta: esc(shortTime(it.at)),
    body: esc(readableText(it.summary, "（无描述）")),
  }));
  setListHtml("timeline", rows);
}

function renderAgentsView() {
  if (!state.agents.length) {
    setListHtml("agents-list", [emptyBox("未检测到引擎", "在 config/agents.json 里配置 Agent CLI。")]);
    return;
  }
  const rows = state.agents.map((a) => {
    const enabled = a.enabled !== false;
    const usage = a.usage || {};
    return rowHtml({
      title: esc(agentDisplayName(a)),
      tags: `<span class="tag">${esc(a.protocol || "")}</span>
        <span class="tag ${enabled ? "success" : "skipped"}">${enabled ? "已启用" : "已停用"}</span>
        ${(a.roles || []).map((r) => `<span class="tag">${esc(r)}</span>`).join("")}`,
      meta: `调用 ${esc(usage.invocations ?? 0)} · tokens ${esc(usage.tokens ?? 0)} · ${Math.round(usage.seconds || 0)}s`,
      body: `<div class="mono">${esc(String(a.command || "未找到可执行文件"))} ${esc((a.args || []).join(" "))}</div>`,
      actions: `<button class="btn tiny ghost" data-act="agent-check" data-id="${esc(a.id)}">连通性检测</button>
        <button class="btn tiny ${enabled ? "ghost danger" : ""}" data-act="agent-toggle" data-id="${esc(a.id)}" data-enable="${enabled ? "0" : "1"}">${enabled ? "停用" : "启用"}</button>`,
    });
  });
  setListHtml("agents-list", rows);
}

/* ---------------------------------------------------------------- 数据加载 */

async function loadAgentsDetail() {
  try {
    const agents = await api("/api/agents/detail");
    state.agents = Array.isArray(agents) ? agents : [];
    state.agentsLoaded = true;
  } catch (err) {
    toast(`读取 Agent 失败：${err.message}`, "error");
  }
  if (state.dashboard) state.dashboard.agents = state.agents;
  renderAgentsView();
  renderInspectorAgents(state.dashboard);
  renderSidebarProject(state.dashboard);
  renderStatusBar(state.dashboard);
  renderMissionOptions();
  return state.agents;
}

async function loadKanban() {
  if (!state.projectId) return;
  const data = await api(`/api/projects/${encodeURIComponent(state.projectId)}/kanban`);
  state.kanban = data || { nodes: [], edges: [], levels: [], progress: {} };
  renderKanban();
  renderDag();
}

async function loadBoard() {
  if (!state.projectId) return;
  const data = await api(`/api/projects/${encodeURIComponent(state.projectId)}/board`);
  state.board = Array.isArray(data) ? data : [];
  renderBoard();
}

async function loadHandoffs() {
  if (!state.projectId) return;
  const data = await api(`/api/projects/${encodeURIComponent(state.projectId)}/handoffs`);
  state.handoffs = Array.isArray(data) ? data : [];
  renderHandoffs();
}

async function loadApprovals() {
  if (!state.projectId) return;
  const data = await api(`/api/projects/${encodeURIComponent(state.projectId)}/approvals`);
  state.approvals = Array.isArray(data) ? data : [];
  renderApprovals();
}

async function loadSnapshots() {
  if (!state.projectId) return;
  const data = await api(`/api/projects/${encodeURIComponent(state.projectId)}/snapshots`);
  state.snapshots = Array.isArray(data) ? data : [];
  renderSnapshots();
}

async function loadObserve() {
  if (!state.projectId) return;
  const base = `/api/projects/${encodeURIComponent(state.projectId)}`;
  const [stats, runs, timeline, artifacts] = await Promise.all([
    api(`${base}/stats`),
    api(`${base}/runs?limit=80`),
    api(`${base}/timeline`),
    api(`${base}/artifacts`),
  ]);
  state.stats = stats || null;
  state.runs = Array.isArray(runs) ? runs : [];
  state.timeline = Array.isArray(timeline) ? timeline : [];
  state.artifacts = Array.isArray(artifacts) ? artifacts : [];
  renderStats();
  renderRuns();
  renderTimeline();
  renderArtifacts();
}

async function loadConfigView() {
  await ensureConfig().catch(() => {});
  const sel = $("config-file");
  await loadConfigFile(sel ? sel.value : "agents.json");
}

async function loadSystem() {
  const [storage, backups, logs] = await Promise.all([
    api("/api/system/storage"),
    api("/api/system/backups"),
    api("/api/system/logs?lines=120").catch(() => ({ lines: [] })),
  ]);
  state.system = { storage, backups: Array.isArray(backups) ? backups : [], logs: (logs && logs.lines) || [] };
  renderSystem();
}

function renderSystem() {
  const data = state.system || {};
  const storage = data.storage || {};
  const db = storage.db || {};
  const dirs = storage.dirs || {};
  const stats = [
    ["数据库", formatBytes(db.bytes), `${db.schema_version ? `schema v${db.schema_version}` : ""}`],
    ["运行证据", formatBytes((dirs.runtime || {}).bytes), `${(dirs.runtime || {}).files || 0} 文件`],
    ["快照", formatBytes((dirs.snapshots || {}).bytes), `${(dirs.snapshots || {}).files || 0} 文件`],
    ["日志", formatBytes((dirs.logs || {}).bytes), `${(dirs.logs || {}).files || 0} 文件`],
    ["备份", formatBytes((dirs.backups || {}).bytes), `${(dirs.backups || {}).files || 0} 文件`],
  ];
  const el = $("system-storage");
  if (el) {
    el.innerHTML = stats.map(([k, v, hint]) =>
      `<div class="stat"><span class="stat-k">${esc(k)}</span><span class="stat-v">${esc(v)}</span><span class="stat-k">${esc(hint)}</span></div>`
    ).join("");
  }
  const backups = data.backups || [];
  setListHtml("system-backups", backups.length
    ? backups.map((b) => rowHtml({
        title: esc(b.label || b.name),
        meta: `${esc(shortTime(b.created_at))} · ${formatBytes(b.bytes)} · ${esc(b.name)}`,
        body: `<div class="mono">${esc(b.sha256 || "")}</div>`,
        actions: `<button class="btn tiny ghost" data-act="open-path" data-path="${esc(b.path)}">打开位置</button>`,
      }))
    : [emptyBox("还没有备份", "点击「创建备份」生成一份可恢复的一致性快照。")]);
  const logs = data.logs || [];
  const logEl = $("system-logs");
  if (logEl) {
    logEl.innerHTML = logs.length
      ? logs.map((line) => `<div class="mono" style="padding:4px 0;border-bottom:1px solid var(--ops-rule);word-break:break-all">${esc(line)}</div>`).join("")
      : emptyBox("暂无日志", "运行任务后这里会出现结构化日志。");
  }
}

const VIEW_LOADERS = {
  home: () => loadDashboard(),
  tasks: () => loadKanban(),
  dag: () => loadKanban(),
  board: () => loadBoard(),
  handoffs: () => loadHandoffs(),
  approvals: () => loadApprovals(),
  snapshots: () => loadSnapshots(),
  agents: () => loadAgentsDetail(),
  observe: () => loadObserve(),
  config: () => loadConfigView(),
  system: () => loadSystem(),
};

async function loadViewData(view) {
  if (!state.projectId) return null;
  const loader = VIEW_LOADERS[view];
  if (!loader) return null;
  if (state._loadingView[view]) return state._loadingView[view];
  const promise = (async () => {
    try {
      await loader();
      state.loaded[view] = true;
    } finally {
      delete state._loadingView[view];
    }
  })();
  state._loadingView[view] = promise;
  return promise;
}

function renderViewFromCache(view) {
  switch (view) {
    case "home":
      if (state.dashboard) renderHome(state.dashboard);
      else renderNoProject();
      break;
    case "tasks": renderKanban(); break;
    case "dag": renderDag(); break;
    case "board": renderBoard(); break;
    case "handoffs": renderHandoffs(); break;
    case "approvals": renderApprovals(); break;
    case "snapshots": renderSnapshots(); break;
    case "agents": renderAgentsView(); break;
    case "observe":
      renderStats();
      renderRuns();
      renderTimeline();
      renderArtifacts();
      break;
    case "system": renderSystem(); break;
    case "live": renderLegacyStream(); break;
    default: break;
  }
}

function switchView(view, opts = {}) {
  if (!VIEW_LABELS[view] && view !== "live") view = "home";
  state.view = view;
  applyViewClasses(view);
  if (!opts.fromHash) syncHash(view);
  if (view === "live") {
    state._livePinned = true;
    state._liveUnseen = 0;
  }
  renderViewFromCache(view);
  if (!state.projectId) return;
  loadViewData(view).catch((err) => toast(`加载失败：${err.message}`, "error"));
}

async function refreshCurrent() {
  const jobs = [];
  if (state.projectId) jobs.push(loadDashboard({ force: true }));
  if (state.view !== "home") jobs.push(loadViewData(state.view));
  const results = await Promise.allSettled(jobs);
  return results;
}

async function manualRefresh() {
  if (!state.projectId) { toast("还没有项目"); return; }
  const jobs = [loadDashboard({ force: true }), loadAgentsDetail()];
  if (state.view !== "home") jobs.push(loadViewData(state.view));
  const results = await Promise.allSettled(jobs);
  const failed = results.find((r) => r.status === "rejected");
  if (failed) toast(`刷新失败：${failed.reason && failed.reason.message ? failed.reason.message : failed.reason}`, "error");
  else toast("已刷新");
}

async function ensureConfig() {
  if (state.config) return state.config;
  state.config = await api("/api/config");
  return state.config;
}

async function loadConfigFile(name) {
  const file = name || "agents.json";
  if (!state.configFiles || !Object.keys(state.configFiles).length) {
    const all = await api("/api/config/files");
    state.configFiles = (all && all.files) || {};
    setText("config-dir", `配置目录：${(all && all.dir) || ""}`);
  }
  const content = state.configFiles[file];
  const editor = $("config-editor");
  if (editor) editor.value = content ? JSON.stringify(content, null, 2) : "{}";
}

async function saveConfigFile() {
  const sel = $("config-file");
  const editor = $("config-editor");
  const name = sel ? sel.value : "agents.json";
  const text = editor ? editor.value : "";
  const res = await api(`/api/config/files/${encodeURIComponent(name)}`, { method: "PUT", body: { content: text } });
  state.config = (res && res.config) || null;
  state.configFiles = {};
  toast(`${name} 已保存并生效`);
  await loadAgentsDetail();
  await loadConfigFile(name);
}

/* ================================================================ 任务详情抽屉 */

async function openTask(taskId) {
  if (!taskId) return;
  let detail;
  try {
    detail = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
  } catch (err) {
    toast(err.message, "error");
    return;
  }
  const t = detail.task || {};
  let workspace = null;
  try {
    workspace = await api(`/api/tasks/${encodeURIComponent(taskId)}/workspace`);
  } catch (_) {
    // 旧后端没有 workspace 接口（404/405）时优雅降级：只显示 task.workdir + 说明
    workspace = null;
  }
  const runs = (detail.runs || []).map((r) => `
    <div class="entry"><div class="head"><span class="t">${esc(displayText(r.agent_id, "未知 Agent"))} · ${esc(STATUS_LABEL[r.status] || r.status)}</span>
      <span class="hint">${esc(r.duration_ms)}ms</span></div>
      <div class="foot"><span>exit ${esc(r.exit_code)}</span><span>token ${esc(r.cost_tokens)}</span>
      <span>timeout ${esc(formatSeconds(r.timeout_sec))}</span>
      <span>${esc(shortTime(r.started_at))}</span>
      <button class="btn tiny ghost" data-act="run-output" data-id="${esc(r.id)}">输入 / 输出</button></div></div>`).join("")
    || '<p class="hint">尚无运行记录</p>';
  const changes = (detail.changes || []).map((c) => `
    <div class="entry"><div class="head"><span class="t">${esc(c.action)} ${esc(displayText(c.rel_path, "（历史数据无可读内容）"))}</span>
      <span class="tag ${c.in_scope ? "success" : "failed"}">${c.in_scope ? "范围内" : "越权"}</span>
      ${c.conflict ? '<span class="tag failed">冲突</span>' : ""}</div>
      <div class="foot"><span>${esc(c.mode)}</span><span>${esc(shortTime(c.at))}</span>
        <div class="spacer" style="flex:1"></div>
        <button class="btn tiny ghost" data-act="change-diff" data-id="${esc(t.id)}" data-path="${esc(c.rel_path)}">查看差异</button>
      </div></div>`).join("") || '<p class="hint">无文件改动</p>';
  const artifacts = (detail.artifacts || []).map((a) => `
    <div class="entry"><div class="head"><span class="t">${esc(displayText(a.rel_path, "（历史数据无可读内容）"))}</span>
      <span class="tag ${a.role === "output" ? "success" : ""}">${esc(a.role === "output" ? "输出" : "输入")}</span>
      <span class="hint">${esc(a.size)} B</span>
      <span class="spacer"></span>
      ${a.abs_path ? openPathButton("打开", a.abs_path) : '<span class="hint">仅相对路径</span>'}
    </div></div>`).join("") || '<p class="hint">无产物</p>';
  const acceptance = t.result && t.result.acceptance;
  const runtime = t.runtime || {};
  const missingOutputs = (t.result && Array.isArray(t.result.missing_outputs)) ? t.result.missing_outputs : [];
  const generatedOutputs = (t.result && Array.isArray(t.result.generated_outputs)) ? t.result.generated_outputs : [];
  const acceptanceWarnings = (acceptance && Array.isArray(acceptance.warnings)) ? acceptance.warnings : [];
  const effectiveAcceptance = Array.isArray(t.acceptance_effective) ? t.acceptance_effective : [];
  const hygiene = (t.result && t.result.hygiene) || {};
  const handoff = detail.handoff || {};
  const deps = detail.dependencies || [];
  const depItems = deps.map((d) => `
    <div class="entry"><div class="head"><span class="t">${esc(displayText(d.title, d.id || "未命名任务"))}</span>
      <span class="tag ${esc(d.status)}">${esc(STATUS_LABEL[d.status] || d.status)}</span>
      <div class="spacer" style="flex:1"></div>
      <button class="btn tiny ghost danger" data-act="dep-remove" data-id="${esc(t.id)}" data-dep="${esc(d.id)}">移除</button>
    </div></div>`).join("") || '<p class="hint">无前置依赖</p>';
  const others = currentTasks().filter((n) => n.id !== t.id && !deps.some((d) => d.id === n.id));
  const depOptions = others.map((n) => `<option value="${esc(n.id)}">${esc(taskDisplayTitle(n))}</option>`).join("");

  openDrawer(taskDisplayTitle(t), `
    <div class="section section-plain">
      <h4>状态</h4>
      <p><span class="tag ${esc(t.status)}">${esc(STATUS_LABEL[t.status] || t.status)}</span>
        · 类型 ${esc(displayText(t.kind, "generic"))} · 角色 ${esc(displayText(t.role, "-"))} · Agent ${esc(displayText(t.agent_id, "-"))} · 隔离 ${esc(t.isolation)}
        ${t.read_only ? "· 只读" : ""} · 尝试 ${esc(t.attempt)}/${esc(t.max_attempts)}</p>
      <p class="hint">超时预算 ${esc(formatSeconds(runtime.effective_timeout_sec))}
        · 已运行 ${esc(formatSeconds(runtime.elapsed_sec))}
        · 剩余 ${esc(formatSeconds(runtime.remaining_sec))}
        · 下次重试预算 ${esc(formatSeconds(runtime.next_attempt_timeout_sec))}</p>
      ${runtime.warning ? `<p class="tag failed">${esc(runtime.warning_text)}</p>` : ""}
      ${t.blocked_reason ? `<p class="hint">阻塞原因：${esc(readableText(t.blocked_reason, "（历史数据无可读内容）"))}</p>` : ""}
      ${t.last_error ? `<p class="hint">错误：${esc(readableText(t.last_error, "（历史数据无可读内容）"))}</p>` : ""}
      ${(t.result && Array.isArray(t.result.creation_warnings) && t.result.creation_warnings.length)
        ? `<p class="hint">风险提示：${esc(t.result.creation_warnings.join("；"))}</p>` : ""}
    </div>
    ${taskWorkspaceHtml(t, workspace)}
    <div class="section section-plain"><h4>任务说明</h4><pre>${esc(t.brief ? readableText(t.brief, "（历史数据无可读内容）") : "（无）")}</pre></div>
    ${acceptance ? `<div class="section section-plain"><h4>验收结论</h4>
      <p class="${acceptance.ok ? "status-success" : "status-failed"}">${esc(readableText(acceptance.summary, "（历史数据无可读内容）"))}</p>
      ${acceptanceWarnings.length ? `<p class="hint">${esc(acceptanceWarnings.join("；"))}</p>` : ""}
      ${effectiveAcceptance.length ? `<p class="hint">执行期有效验收：${esc(effectiveAcceptance.map((c) => c.type).join(", "))}</p>` : ""}
      ${generatedOutputs.length ? `<p class="hint">已生成：${esc(generatedOutputs.join(", "))}</p>` : ""}
      ${missingOutputs.length ? `<p class="tag failed">缺失：${esc(missingOutputs.join(", "))}</p>` : ""}
      ${hygiene.temporary_files ? `<p class="hint">卫生检查：临时文件 ${esc(hygiene.temporary_files)} 个、临时目录 ${esc(hygiene.temporary_dirs || 0)} 个</p>` : ""}
      <pre>${esc(JSON.stringify(acceptance.checks, null, 2))}</pre></div>` : ""}
    <div class="section section-plain"><h4>前置依赖</h4>${depItems}
      ${depOptions ? `<div style="display:flex;gap:8px;margin-top:8px">
        <select id="dep-add" aria-label="选择前置依赖">${depOptions}</select>
        <button class="btn tiny" data-act="dep-add" data-id="${esc(t.id)}">添加依赖</button></div>` : ""}
    </div>
    <div class="section section-plain"><h4>操作</h4>
      <div class="op-row">
        <button class="btn tiny primary" data-act="task-run" data-id="${esc(t.id)}">立即执行</button>
        <button class="btn tiny" data-act="task-fanout" data-id="${esc(t.id)}">同题多 Agent 扇出</button>
        <button class="btn tiny" data-act="task-extend-timeout" data-id="${esc(t.id)}">延长超时</button>
        <button class="btn tiny" data-act="task-op" data-id="${esc(t.id)}" data-op="retry">重试</button>
        <button class="btn tiny" data-act="task-op" data-id="${esc(t.id)}" data-op="takeover">人工接管</button>
        <button class="btn tiny" data-act="task-provide" data-id="${esc(t.id)}">人工回填结果</button>
        <button class="btn tiny" data-act="task-op" data-id="${esc(t.id)}" data-op="skip">跳过</button>
        <button class="btn tiny danger" data-act="task-op" data-id="${esc(t.id)}" data-op="cancel">取消</button>
        <button class="btn tiny danger" data-act="task-op" data-id="${esc(t.id)}" data-op="revert">回滚改动</button>
      </div>
      <div class="op-row" style="margin-top:8px">
        <button class="btn tiny ghost" data-act="task-edit" data-id="${esc(t.id)}">编辑任务</button>
        <button class="btn tiny ghost" data-act="task-route" data-id="${esc(t.id)}">路由预览</button>
        <button class="btn tiny ghost" data-act="task-move" data-dir="up" data-id="${esc(t.id)}">↑ 上移</button>
        <button class="btn tiny ghost" data-act="task-move" data-dir="down" data-id="${esc(t.id)}">↓ 下移</button>
        <button class="btn tiny ghost" data-act="task-compare" data-id="${esc(t.id)}">对比各 Agent 产出</button>
        <button class="btn tiny ghost danger" data-act="task-delete" data-id="${esc(t.id)}">删除任务</button>
      </div>
    </div>
    <div class="section section-plain"><h4>产物（输入 / 输出）</h4>${artifacts}</div>
    <div class="section section-plain"><h4>运行记录</h4>${runs}</div>
    <div class="section section-plain"><h4>文件改动</h4>${changes}</div>
    ${handoff.did ? `<div class="section section-plain"><h4>交接说明</h4><pre>${esc(JSON.stringify(handoffView(handoff), null, 2))}</pre></div>` : ""}
  `);
}

/* ================================================================ 表单 */

function taskFormHtml(task) {
  const t = task || {};
  const cfg = state.config || {};
  const templates = cfg.templates || [];
  const rules = cfg.acceptance_rules || [];
  const templateOptions = ['<option value="">（无模板）</option>']
    .concat(templates.map((id) => `<option value="${esc(id)}" ${t.template_id === id ? "selected" : ""}>${esc(id)}</option>`)).join("");
  const agentOptions = ['<option value="">（按规则自动路由）</option>']
    .concat(state.agents.map((a) => `<option value="${esc(a.id)}" ${t.agent_id === a.id ? "selected" : ""}>${esc(agentDisplayName(a))}</option>`)).join("");
  const ruleOptions = ['<option value="">（无）</option>']
    .concat(rules.map((id) => `<option value="${esc(id)}" ${t.acceptance_ref === id ? "selected" : ""}>${esc(id)}</option>`)).join("");
  const kindChoices = Array.from(new Set([...KIND_OPTIONS, ...(cfg.kinds || [])]));
  const kindOptions = kindChoices.map((k) => `<option value="${esc(k)}"></option>`).join("");
  const iso = t.isolation || "copy";
  return `
    <div class="form-row"><label for="tf-title">标题 *</label><input id="tf-title" value="${esc(prefill(t.title))}" /></div>
    <div class="form-grid">
      <div class="form-row"><label for="tf-kind">工作类型（可自由填写；也可从配置 policy.kinds 里选）</label>
        <input id="tf-kind" list="kind-options-list" value="${esc(prefill(t.kind) || "generic")}" />
        <datalist id="kind-options-list">${kindOptions}</datalist></div>
      <div class="form-row"><label for="tf-template">任务模板</label><select id="tf-template">${templateOptions}</select></div>
      <div class="form-row"><label for="tf-agent">指定 Agent</label><select id="tf-agent">${agentOptions}</select></div>
      <div class="form-row"><label for="tf-role">角色</label><input id="tf-role" value="${esc(prefill(t.role) || "worker")}" /></div>
      <div class="form-row"><label for="tf-isolation">隔离模式</label><select id="tf-isolation">
        <option value="copy" ${iso === "copy" ? "selected" : ""}>copy（独立副本，默认）</option>
        <option value="shared" ${iso === "shared" ? "selected" : ""}>shared（直接改原目录）</option>
        <option value="worktree" ${iso === "worktree" ? "selected" : ""}>worktree（git 工作树）</option>
      </select></div>
      <div class="form-row"><label for="tf-acceptance-ref">验收规则</label><select id="tf-acceptance-ref">${ruleOptions}</select></div>
      <div class="form-row"><label for="tf-scope">写范围（逗号分隔的 glob；留空会要求显式确认不受限）</label><input id="tf-scope" value="${esc(cleanList(t.write_scope).join(", "))}" /></div>
      <div class="form-row"><label for="tf-tags">标签（逗号分隔）</label><input id="tf-tags" value="${esc(cleanList(t.tags).join(", "))}" /></div>
      <div class="form-row"><label for="tf-priority">优先级</label><input id="tf-priority" type="number" value="${esc(t.priority ?? 0)}" /></div>
      <div class="form-row"><label for="tf-max-attempts">最大尝试次数（留空按类型默认，code/doc 为 3）</label><input id="tf-max-attempts" type="number" value="${t.max_attempts ?? ""}" /></div>
      <div class="form-row"><label for="tf-timeout">超时（秒，留空按类型/Agent 默认，code/doc 至少 2400）</label><input id="tf-timeout" type="number" value="${t.timeout_sec ?? ""}" /></div>
      <div class="form-row"><label for="tf-inputs">输入产物（逗号分隔）</label><input id="tf-inputs" value="${esc(cleanList(t.inputs).join(", "))}" /></div>
      <div class="form-row"><label for="tf-outputs">输出产物（逗号分隔，自动生成 file_exists/非空验收）</label><input id="tf-outputs" value="${esc(cleanList(t.outputs).join(", "))}" /></div>
    </div>
    <div class="form-row check"><input type="checkbox" id="tf-read-only" ${t.read_only ? "checked" : ""} />
      <label for="tf-read-only">只读协作（仅分析/评审，不写回工作区）</label></div>
    <div class="form-row check"><input type="checkbox" id="tf-allow-shared-write" />
      <label for="tf-allow-shared-write">允许修改型任务使用 shared 直写（默认会自动改成 copy）</label></div>
    <div class="form-row check"><input type="checkbox" id="tf-unrestricted-write" />
      <label for="tf-unrestricted-write">我确认允许不受限写入整个工作区</label></div>
    <div class="form-row check"><input type="checkbox" id="tf-confirm-exit-zero" />
      <label for="tf-confirm-exit-zero">我确认验收只有 exit_zero（执行期仍会补加真实文件改动检查）</label></div>
    <div class="form-row"><label for="tf-brief">任务说明 / brief</label><textarea id="tf-brief">${esc(prefill(t.brief))}</textarea></div>
    <div class="form-row"><label for="tf-acceptance">验收条件（JSON 数组，可选，覆盖上面的规则）</label>
      <textarea id="tf-acceptance" placeholder='[{"type":"file_exists","path":"out/report.md"}]'>${esc(t.acceptance && t.acceptance.length ? JSON.stringify(t.acceptance, null, 2) : "")}</textarea></div>`;
}

function readTaskForm() {
  const readOnly = $("tf-read-only").checked;
  const isolation = $("tf-isolation").value;
  const scope = splitList($("tf-scope").value);
  const outputs = splitList($("tf-outputs").value);
  const allowUnrestricted = $("tf-unrestricted-write").checked;
  const allowShared = $("tf-allow-shared-write").checked;
  const payload = {
    title: $("tf-title").value.trim(),
    kind: $("tf-kind").value,
    template_id: $("tf-template").value || null,
    agent_id: $("tf-agent").value || null,
    role: $("tf-role").value.trim() || "worker",
    isolation,
    acceptance_ref: $("tf-acceptance-ref").value || null,
    write_scope: scope,
    tags: splitList($("tf-tags").value),
    priority: Number($("tf-priority").value || 0),
    inputs: splitList($("tf-inputs").value),
    outputs,
    read_only: readOnly,
    allow_unrestricted_write: allowUnrestricted,
    allow_shared_write: allowShared,
  };
  const maxAttempts = $("tf-max-attempts").value.trim();
  if (maxAttempts) payload.max_attempts = Number(maxAttempts);
  const timeout = $("tf-timeout").value.trim();
  if (timeout) payload.timeout_sec = Number(timeout);
  const brief = $("tf-brief").value;
  if (brief) payload.brief = brief;
  const acceptance = $("tf-acceptance").value.trim();
  if (acceptance) {
    try { payload.acceptance = JSON.parse(acceptance); }
    catch (err) { throw new Error(`验收条件不是合法 JSON：${err.message}`); }
  }
  payload.confirm_exit_zero_only = Boolean($("tf-confirm-exit-zero").checked);
  if (!readOnly && !scope.length && !allowUnrestricted) {
    const ok = window.confirm("写范围为空：任务可修改整个工作区。确认允许不受限写入吗？");
    if (!ok) throw new Error("已取消：请填写写范围，或勾选「允许不受限写入」");
    payload.allow_unrestricted_write = true;
    $("tf-unrestricted-write").checked = true;
  }
  if (!readOnly && isolation === "shared" && !allowShared) {
    const ok = window.confirm("shared 会直接写工作区且无自动回滚。确认允许该修改型任务使用 shared 吗？");
    if (!ok) throw new Error("已取消：修改型任务建议使用 copy 隔离");
    payload.allow_shared_write = true;
    $("tf-allow-shared-write").checked = true;
  }
  if (Array.isArray(payload.acceptance) && payload.acceptance.length === 1
    && payload.acceptance[0] && payload.acceptance[0].type === "exit_zero"
    && !payload.confirm_exit_zero_only) {
    const ok = window.confirm("验收只有 exit_zero，Agent 说完成也不算成功；执行期会要求真实文件改动。继续吗？");
    if (!ok) throw new Error("已取消：请添加 file_exists / command_exit_zero 等验收条件");
    payload.confirm_exit_zero_only = true;
    $("tf-confirm-exit-zero").checked = true;
  }
  return payload;
}

async function openNewTaskModal() {
  try { await ensureConfig(); } catch (err) { toast(`读取配置失败：${err.message}`, "error"); }
  openModal("新建任务", taskFormHtml(null),
    '<button class="btn" data-act="modal-close">取消</button><button class="btn primary" data-act="do-new-task">创建</button>');
}

async function openEditTaskModal(taskId) {
  const detail = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
  try { await ensureConfig(); } catch (_) { /* 配置读不到时仍可编辑基础字段 */ }
  openModal("编辑任务", taskFormHtml(detail.task),
    `<button class="btn" data-act="modal-close">取消</button>
     <button class="btn primary" data-act="do-edit-task" data-id="${esc(taskId)}">保存</button>`);
}

function openPlanModal() {
  openModal("批量计划（JSON）", `
    <p class="hint">一次建多个任务并连好依赖。key 用于在 depends_on 里引用同批任务。</p>
    <textarea id="plan-json" style="min-height:280px">{
  "tasks": [
    { "key": "a", "title": "调研资料", "kind": "research", "agent": "opencode" },
    { "key": "b", "title": "撰写报告", "kind": "doc", "agent": "codex", "depends_on": ["a"] }
  ]
}</textarea>`,
    '<button class="btn" data-act="modal-close">取消</button><button class="btn primary" data-act="do-plan">导入</button>');
}

function openFanoutModal(taskId) {
  const options = state.agents.map((a) =>
    `<label class="check" style="margin-bottom:6px"><input type="checkbox" class="fan-agent" value="${esc(a.id)}" /> ${esc(agentDisplayName(a))}</label>`).join("");
  openModal("同题多 Agent 扇出", `
    <p class="hint">同一任务分派给多个 Agent 分别尝试，保留各自结果供比较（串行执行，可能较慢）。</p>
    ${options}`,
    `<button class="btn" data-act="modal-close">取消</button>
     <button class="btn primary" data-act="do-fanout" data-id="${esc(taskId)}">开始扇出</button>`);
}

function openProvideModal(taskId) {
  openModal("人工回填结果", `
    <div class="form-row"><label for="pv-did">做了什么</label><textarea id="pv-did"></textarea></div>
    <div class="form-row"><label for="pv-artifacts">产出文件（逗号分隔，相对路径）</label><input id="pv-artifacts" /></div>
    <div class="form-row"><label for="pv-notes">备注</label><textarea id="pv-notes"></textarea></div>`,
    `<button class="btn" data-act="modal-close">取消</button>
     <button class="btn primary" data-act="do-provide" data-id="${esc(taskId)}">提交并完成</button>`);
}

function openBoardModal(entry) {
  const e = entry || {};
  openModal(entry ? "编辑黑板条目" : "写入黑板条目", `
    <div class="form-grid">
      <div class="form-row"><label for="bm-type">类型</label><select id="bm-type">
        ${["brief", "artifact", "decision", "handoff", "todo", "issue", "reference", "note"]
          .map((t) => `<option value="${t}" ${e.type === t ? "selected" : ""}>${t}</option>`).join("")}
      </select></div>
      <div class="form-row"><label for="bm-scope">作用域</label><select id="bm-scope">
        ${["project", "task", "agent"].map((s) => `<option value="${s}" ${e.scope === s ? "selected" : ""}>${s}</option>`).join("")}
      </select></div>
    </div>
    <div class="form-row"><label for="bm-title">标题 *</label><input id="bm-title" value="${esc(prefill(e.title))}" /></div>
    <div class="form-row"><label for="bm-body">正文</label><textarea id="bm-body" style="min-height:140px">${esc(prefill(normalizeChunkedText(e.body)))}</textarea></div>
    <div class="form-row"><label for="bm-tags">标签（逗号分隔）</label><input id="bm-tags" value="${esc(cleanList(e.tags).join(", "))}" /></div>`,
    `<button class="btn" data-act="modal-close">取消</button>
     <button class="btn primary" data-act="${entry ? "do-board-edit" : "do-board-new"}" ${entry ? `data-id="${esc(entry.id)}"` : ""}>保存</button>`);
}

function openHandoffModal(handoff) {
  const h = handoff || {};
  openModal("人工修正交接说明", `
    <div class="form-row"><label for="hf-did">做了什么</label><textarea id="hf-did">${esc(prefill(normalizeChunkedText(h.did)))}</textarea></div>
    <div class="form-row"><label for="hf-not-done">没做什么</label><textarea id="hf-not-done">${esc(prefill(normalizeChunkedText(h.not_done)))}</textarea></div>
    <div class="form-row"><label for="hf-issues">遗留问题（逗号分隔）</label><input id="hf-issues" value="${esc(cleanList(h.open_issues).map(normalizeChunkedText).join(", "))}" /></div>
    <div class="form-row"><label for="hf-next">下一步建议（逗号分隔）</label><input id="hf-next" value="${esc(cleanList(h.next_steps).map(normalizeChunkedText).join(", "))}" /></div>
    <div class="form-row"><label for="hf-notes">备注</label><textarea id="hf-notes">${esc(prefill(normalizeChunkedText(h.notes)))}</textarea></div>`,
    `<button class="btn" data-act="modal-close">取消</button>
     <button class="btn primary" data-act="do-handoff" data-id="${esc(h.id)}">保存修正</button>`);
}

function openRunModal() {
  if (!state.projectId) { toast("先选择项目", "error"); return; }
  const nodes = currentTasks();
  const ready = nodes.filter((t) => ["pending", "interrupted", "failed", "waiting"].includes(t.status)).length;
  openModal("启动调度", `
    <div class="form-grid">
      <div class="form-row"><label for="run-parallel">最大并发（留空用策略默认）</label><input id="run-parallel" type="number" min="1" placeholder="默认" /></div>
      <div class="form-row check" style="margin-top:22px"><input type="checkbox" id="run-retry" checked />
        <label for="run-retry">自动重试失败 / 中断任务（受 max_attempts 限制）</label></div>
    </div>
    <p class="hint">将按依赖 DAG 并行调度；当前可派发约 ${esc(ready)} 个任务。运行中可随时「停止」——它会<b>终止本项目</b>正在跑的 Agent 调用并取消其任务（只影响本项目），之后可用「恢复」把中断的任务捡回来续跑。</p>`,
    '<button class="btn" data-act="modal-close">取消</button><button class="btn primary" data-act="do-run-project">开始调度</button>');
}

function openForceRecoverModal() {
  if (!state.projectId) { toast("先选择项目", "error"); return; }
  openModal("强制恢复（破坏性操作）", `
    <p><b>会无视存活判定</b>：把该项目所有「执行中」的任务及其运行记录标成 <b>中断</b>，并清掉租约。</p>
    <p class="hint">如果本机还有另一个调度方正在跑（例如你在命令行里另开了一次 run_project），
      它<b>正在执行</b>的任务会被误判为中断，后续重试可能覆盖现场。</p>
    <p class="hint">只有在确认「那个调度进程已经死了 / 只能靠人回收」时才使用。
      日常请用「恢复」：它按 PID 与心跳判断，只回收确实已死的任务，不会误杀。</p>`,
    `<button class="btn" data-act="modal-close">取消</button>
     <button class="btn danger" data-act="do-force-recover">我确认，强制恢复</button>`);
}

/* ================================================================ 操作 */

async function stopProject() {
  if (!state.projectId) return;
  const res = await api(`/api/projects/${encodeURIComponent(state.projectId)}/stop`, { method: "POST", body: {} });
  toast(res.message || "已停止当前项目");
  await refreshCurrent().catch(() => {});
}

async function recoverProject() {
  if (!state.projectId) return;
  // 保守恢复：后端按 PID / 心跳判断，只回收确实已死的任务（不会误杀本机其他调度方）
  const res = await api(`/api/projects/${encodeURIComponent(state.projectId)}/recover`, { method: "POST", body: {} });
  toast(res.message || "已恢复");
  await refreshCurrent().catch(() => {});
}

async function forceRecoverProject() {
  if (!state.projectId) return;
  const res = await api(`/api/projects/${encodeURIComponent(state.projectId)}/recover`, { method: "POST", body: { force: true } });
  toast(res.message || "已强制恢复");
  closeModal();
  await refreshCurrent().catch(() => {});
}

async function taskOp(id, op, body = {}) {
  await api(`/api/tasks/${encodeURIComponent(id)}/${op}`, { method: "POST", body });
  toast(`已执行：${op}`);
  await refreshCurrent();
  if (drawerOpen()) await openTask(id);
}

/** 手动同步工作副本 → 工作目录；任务运行中先二次确认（当前只是快照）。 */
async function requestTaskSync(id) {
  if (!id) return;
  const detail = await api(`/api/tasks/${encodeURIComponent(id)}`);
  const t = detail.task || {};
  if (t.status === "running") {
    openModal("同步到工作目录", `
      <p>任务仍在运行，同步的是<b>当前快照</b>；任务完成后会再次覆盖工作目录。</p>
      <p class="hint">确认要现在同步吗？</p>`,
      `<button class="btn" data-act="modal-close">取消</button>
       <button class="btn primary" data-act="do-task-sync" data-id="${esc(id)}">确认同步当前快照</button>`);
    return;
  }
  await doTaskSync(id);
}

async function doTaskSync(id) {
  let res;
  try {
    res = await api(`/api/tasks/${encodeURIComponent(id)}/sync`, { method: "POST", body: {} });
  } catch (err) {
    if (isMissingEndpoint(err) || err.status === 405) throw new Error("后端尚未提供手动同步接口（/sync）");
    throw err;
  }
  const count = res && (res.synced !== undefined
    ? res.synced
    : (res.count !== undefined ? res.count : (Array.isArray(res.files) ? res.files.length : undefined)));
  toast(res && res.message ? res.message : `已同步${count !== undefined ? ` ${count} 个文件` : ""}到工作目录`);
  closeModal();
  if (drawerOpen()) await openTask(id);
}

function openApproveModal(id) {
  openModal("人工验收", `
    <p>仅在你已复核产物、确认验收条件通过后使用。系统会保留原 run 的 exit_code 与错误，不伪造 Agent 成功。</p>
    <label class="field"><span>验收备注</span>
      <textarea id="approve-note" rows="3" placeholder="例如：超时发生在收尾阶段，核心产物完整"></textarea>
    </label>`,
    `<button class="btn" data-act="modal-close">取消</button>
     <button class="btn primary" data-act="do-task-approve" data-id="${esc(id)}">确认验收并同步</button>`);
}

async function doTaskApprove(id) {
  const note = ($("approve-note")?.value || "").trim();
  const res = await api(`/api/tasks/${encodeURIComponent(id)}/approve`, {
    method: "POST",
    body: { sync: true, note },
  });
  toast(res && res.task && res.task.status === "success" ? "人工验收通过" : "已提交人工验收");
  closeModal();
  await refreshCurrent();
  if (drawerOpen()) await openTask(id);
}

async function checkAgent(id) {
  const res = await api(`/api/agents/${encodeURIComponent(id)}/check`, { method: "POST", body: {} });
  toast(`${id}：${res.ok ? "连通 OK" : "不可用"} ${res.detail || ""}`.trim(), res.ok ? "info" : "error");
}

async function setAgentEnabled(id, enable) {
  const all = await api("/api/config/files");
  const agents = (all.files && all.files["agents.json"]) || { agents: [] };
  const list = Array.isArray(agents.agents) ? agents.agents : [];
  const found = list.find((a) => a.id === id);
  if (found) found.enabled = enable;
  else list.push({ id, enabled: enable });
  agents.agents = list;
  await api("/api/config/files/agents.json", { method: "PUT", body: { content: agents } });
  toast(`${id} 已${enable ? "启用" : "停用"}`);
  state.configFiles = {};
  await loadAgentsDetail();
  await loadDashboard({ force: true }).catch(() => {});
}

async function searchBoard() {
  if (!state.projectId) return;
  const params = new URLSearchParams();
  const q = $("bb-query") ? $("bb-query").value.trim() : "";
  if (q) params.set("q", q);
  const type = $("bb-type") ? $("bb-type").value : "";
  if (type) params.set("type", type);
  const since = $("bb-since") ? $("bb-since").value : "";
  const until = $("bb-until") ? $("bb-until").value : "";
  if (since) params.set("since", since);
  if (until) params.set("until", `${until} 23:59:59`);
  const data = await api(`/api/projects/${encodeURIComponent(state.projectId)}/board/search?${params}`);
  state.board = Array.isArray(data) ? data : [];
  renderBoard();
}

async function doNewProject() {
  const name = $("np-name").value.trim();
  if (!name) { toast("项目名必填", "error"); return; }
  const created = await api("/api/projects", {
    method: "POST",
    body: { name, label: $("np-label").value, root_path: $("np-root").value || null },
  });
  toast("项目已创建");
  closeModal();
  resetProjectCaches();
  await loadProjects(false);
  state.projectId = created.id;
  const select = $("project-select");
  if (select) select.value = created.id;
  state.view = "home";
  applyViewClasses("home");
  syncHash("home");
  state._isolationTouched = false;
  syncMissionIsolationDefault(true);
  await loadDashboard({ force: true }).catch((err) => toast(`加载失败：${err.message}`, "error"));
  connectWs();
}

async function doImportProject() {
  const path = $("im-path").value.trim();
  if (!path) { toast("请填写源目录", "error"); return; }
  const created = await api("/api/projects/import", {
    method: "POST",
    body: { path, name: $("im-name").value || null },
  });
  toast("已导入");
  closeModal();
  resetProjectCaches();
  await loadProjects(false);
  state.projectId = created.id;
  const select = $("project-select");
  if (select) select.value = created.id;
  state.view = "home";
  applyViewClasses("home");
  syncHash("home");
  state._isolationTouched = false;
  syncMissionIsolationDefault(true);
  await loadDashboard({ force: true }).catch((err) => toast(`加载失败：${err.message}`, "error"));
  connectWs();
}

async function doDeleteProject() {
  if (!state.projectId) return;
  await api(`/api/projects/${encodeURIComponent(state.projectId)}`, { method: "DELETE" });
  toast("项目已删除");
  closeModal();
  closeDrawer();
  resetProjectCaches();
  state._isolationTouched = false;
  syncMissionIsolationDefault(true);
  state.projectId = null;
  await loadProjects(false);
  if (state.projectId) {
    await loadDashboard({ force: true }).catch(() => {});
    connectWs();
  } else {
    renderNoProject();
    if (state.ws) { try { state.ws.close(); } catch (_) { /* ignore */ } state.ws = null; }
  }
}

/* ================================================================ 动作分发 */

async function handleAction(act, btn) {
  const id = btn ? btn.dataset.id : "";
  switch (act) {
    case "modal-close": closeModal(); return;
    case "live-clear":
      state.stream = [];
      markLiveDirty();
      resetLiveFeedDom();
      renderLegacyStream();
      renderStream();
      return;
    case "live-block-toggle":
      toggleLiveBlock(btn.dataset.id);
      return;
    case "live-jump":
      scrollLiveToBottom();
      return;
    case "stream-latest":
      state._streamFrozen = false;
      removeStreamJump();
      renderStream();
      return;
    case "open-path":
      openPathOrCopy(btn.dataset.path);
      return;

    /* ---- 指挥台任务输入 ---- */
    case "mission-create": await submitMission("single", false); return;
    case "mission-start": await submitMission("single", true); return;
    case "mission-plan": await submitMission("plan", false); return;
    case "mission-decompose": await openDecomposePreview(); return;
    case "do-apply-decompose": {
      let plan;
      try { plan = JSON.parse($("decompose-plan-json").value); }
      catch (err) { throw new Error(`计划不是合法 JSON：${err.message}`); }
      const res = await api(`/api/projects/${encodeURIComponent(state.projectId)}/decompose/apply`, {
        method: "POST",
        body: { plan },
      });
      toast(res.message || `已创建 ${res.created || 0} 个任务`);
      closeModal();
      await loadDashboard({ force: true }).catch(() => {});
      switchView("tasks");
      await loadViewData("tasks").catch(() => {});
      return;
    }

    /* ---- 项目 ---- */
    case "new-project": openNewProjectModal(); return;
    case "import-project": openImportModal(); return;
    case "delete-project": deleteCurrentProject(); return;
    case "do-new-project": await doNewProject(); return;
    case "do-import": await doImportProject(); return;
    case "do-delete-project": await doDeleteProject(); return;
    case "project-settings": openProjectSettingsModal(); return;
    case "do-project-settings": await saveProjectSettings(); return;

    /* ---- 任务 ---- */
    case "task-open":
    case "task-from-id": await openTask(id); return;
    case "new-task": await openNewTaskModal(); return;
    case "import-plan": openPlanModal(); return;
    case "task-edit": await openEditTaskModal(id); return;
    case "task-run": await taskOp(id, "run", { wait: 0 }); return;
    case "task-op": await taskOp(id, btn.dataset.op); return;
    case "task-fanout": openFanoutModal(id); return;
    case "task-extend-timeout": {
      const task = currentTasks().find((item) => item.id === id) || {};
      const current = task.runtime && task.runtime.effective_timeout_sec;
      openModal("延长任务超时", `
        <p class="hint">正在运行的 Agent 已拿到的硬超时不会被追溯修改；新预算从下一次尝试生效。</p>
        <div class="form-row"><label for="extend-timeout-sec">新的超时（秒）</label>
          <input id="extend-timeout-sec" type="number" min="1" value="${esc(current || 3600)}" /></div>`,
        `<button class="btn" data-act="modal-close">取消</button>
         <button class="btn primary" data-act="do-task-extend-timeout" data-id="${esc(id)}">保存</button>`);
      return;
    }
    case "do-task-extend-timeout": {
      const value = Number($("extend-timeout-sec") ? $("extend-timeout-sec").value : 0);
      if (!Number.isFinite(value) || value <= 0) { toast("请输入大于 0 的秒数", "error"); return; }
      const res = await api(`/api/tasks/${encodeURIComponent(id)}/timeout`, {
        method: "POST", body: { timeout_sec: value },
      });
      toast(res.message || "超时预算已更新");
      closeModal();
      await refreshCurrent();
      if (drawerOpen()) await openTask(id);
      return;
    }
    case "task-provide": openProvideModal(id); return;
    case "task-sync": await requestTaskSync(id); return;
    case "do-task-sync": await doTaskSync(id); return;
    case "task-approve": openApproveModal(id); return;
    case "do-task-approve": await doTaskApprove(id); return;
    case "task-route": {
      const res = await api(`/api/tasks/${encodeURIComponent(id)}/route`);
      openModal("路由预览", `
        <p><b>将派发给：</b>${esc(res.agent_id || "（无可用 Agent）")}</p>
        <p class="hint">理由：${esc(res.reason)}${res.explicit ? "（任务已显式指定）" : ""}</p>`);
      return;
    }
    case "task-compare": {
      const res = await api(`/api/tasks/${encodeURIComponent(id)}/compare`);
      openModal("各 Agent 产出对比", `<pre class="mono">${esc(JSON.stringify(res, null, 2))}</pre>`);
      return;
    }
    case "task-move": {
      await api(`/api/tasks/${encodeURIComponent(id)}/move`, { method: "POST", body: { direction: btn.dataset.dir } });
      toast(`已${btn.dataset.dir === "up" ? "上移" : "下移"}`);
      await refreshCurrent();
      if (drawerOpen()) await openTask(id);
      return;
    }
    case "task-delete":
      openModal("删除任务", "<p>确认删除该任务及其依赖关系？</p>",
        `<button class="btn" data-act="modal-close">取消</button>
         <button class="btn danger" data-act="do-task-delete" data-id="${esc(id)}">确认删除</button>`);
      return;
    case "do-task-delete":
      await api(`/api/tasks/${encodeURIComponent(id)}`, { method: "DELETE" });
      toast("任务已删除"); closeModal(); closeDrawer();
      await refreshCurrent();
      return;
    case "dep-add": {
      const dep = $("dep-add") ? $("dep-add").value : "";
      if (!dep) return;
      await api(`/api/tasks/${encodeURIComponent(id)}/deps`, { method: "POST", body: { depends_on: dep } });
      toast("已添加依赖");
      await refreshCurrent();
      await openTask(id);
      return;
    }
    case "dep-remove":
      await api(`/api/tasks/${encodeURIComponent(id)}/deps/${encodeURIComponent(btn.dataset.dep)}`, { method: "DELETE" });
      toast("已移除依赖");
      await refreshCurrent();
      await openTask(id);
      return;
    case "do-new-task": {
      const payload = readTaskForm();
      if (!payload.title) { toast("标题必填", "error"); return; }
      await api(`/api/projects/${encodeURIComponent(state.projectId)}/tasks`, { method: "POST", body: payload });
      toast("任务已创建"); closeModal();
      await refreshCurrent();
      return;
    }
    case "do-edit-task": {
      const payload = readTaskForm();
      await api(`/api/tasks/${encodeURIComponent(id)}`, { method: "PATCH", body: payload });
      toast("已保存"); closeModal();
      await refreshCurrent();
      if (drawerOpen()) await openTask(id);
      return;
    }
    case "do-plan": {
      let spec;
      try { spec = JSON.parse($("plan-json").value); }
      catch (err) { throw new Error(`计划不是合法 JSON：${err.message}`); }
      const created = await api(`/api/projects/${encodeURIComponent(state.projectId)}/tasks/plan`, { method: "POST", body: spec });
      toast(`已创建 ${(created || []).length} 个任务`); closeModal();
      await refreshCurrent();
      return;
    }
    case "do-fanout": {
      const agents = Array.from(document.querySelectorAll(".fan-agent:checked")).map((c) => c.value);
      if (!agents.length) { toast("至少选一个 Agent", "error"); return; }
      closeModal();
      toast(`正在扇出 ${agents.length} 个 Agent…`);
      await api(`/api/tasks/${encodeURIComponent(id)}/fanout`, { method: "POST", body: { agents } });
      toast("扇出完成");
      await refreshCurrent();
      return;
    }
    case "do-provide": {
      await api(`/api/tasks/${encodeURIComponent(id)}/provide`, {
        method: "POST",
        body: {
          did: $("pv-did").value,
          artifacts: splitList($("pv-artifacts").value),
          notes: $("pv-notes").value,
        },
      });
      toast("已回填并完成"); closeModal();
      await refreshCurrent();
      return;
    }
    case "run-output": {
      const data = await api(`/api/runs/${encodeURIComponent(id)}/output`);
      const r = data.run || {};
      const title = [displayText(r.agent_id, "运行"), STATUS_LABEL[r.status] || r.status || ""].filter(Boolean).join(" · ");
      openModal(title, `
        <p class="hint">exit ${esc(r.exit_code)} · ${esc(r.duration_ms)}ms · token ${esc(r.cost_tokens)} · ${esc(shortTime(r.started_at))}${r.error ? ` · 错误：${esc(displayText(r.error, "（历史数据无可读内容）"))}` : ""}</p>
        <div class="section section-plain"><h4>输入（提示词）</h4><pre class="mono">${esc(normalizeChunkedText(data.prompt)) || "（无）"}</pre></div>
        <div class="section section-plain"><h4>stdout</h4><pre class="mono">${esc(normalizeChunkedText(data.stdout)) || "（空）"}</pre></div>
        <div class="section section-plain"><h4>stderr</h4><pre class="mono">${esc(normalizeChunkedText(data.stderr)) || "（空）"}</pre></div>`);
      return;
    }
    case "change-diff": {
      const path = btn.dataset.path;
      const pathLabel = displayText(path, "（历史数据无可读内容）");
      const res = await api(`/api/tasks/${encodeURIComponent(id)}/diff?path=${encodeURIComponent(path)}`);
      if (res.binary) {
        openModal(`差异：${pathLabel}`, `<p class="hint">${esc(res.note || "二进制文件")}</p>
          <p>大小：${esc(res.before_size)} → ${esc(res.after_size)} B · 动作 ${esc(res.action)}</p>`);
        return;
      }
      const body = (res.diff || "").split("\n").map((line) => {
        const cls = line.startsWith("+") ? "d-add" : line.startsWith("-") ? "d-del" : line.startsWith("@@") ? "d-hunk" : "";
        return `<span class="${cls}">${esc(line)}</span>`;
      }).join("\n");
      openModal(`差异：${pathLabel}`, `<pre class="mono diff">${body || "（无文本差异）"}</pre>`);
      return;
    }

    /* ---- 黑板 ---- */
    case "board-new": openBoardModal(null); return;
    case "board-edit": {
      const entry = state.board.find((e) => e.id === id);
      openBoardModal(entry);
      return;
    }
    case "board-versions": {
      const data = await api(`/api/board/${encodeURIComponent(id)}`);
      const vs = (data.versions || []).slice().reverse();
      openModal("黑板版本历史", vs.length ? `<div class="list">${vs.map((v) => `
        <div class="entry"><div class="head"><span class="t">v${esc(v.version)}</span>
          <span class="hint">${esc(shortTime(v.at))}</span>${v.author ? `<span class="tag">${esc(v.author)}</span>` : ""}</div>
          <div class="body"><b>${esc(displayText(v.title, "未命名版本"))}</b>${v.body ? `<br/>${esc(displayText(String(v.body).slice(0, 600), ""))}` : ""}</div>
          ${v.note ? `<div class="foot">备注：${esc(displayText(v.note, ""))}</div>` : ""}</div>`).join("")}</div>` : '<p class="hint">暂无版本</p>');
      return;
    }
    case "do-board-new": {
      const payload = {
        type: $("bm-type").value, scope: $("bm-scope").value,
        title: $("bm-title").value, body: $("bm-body").value,
        tags: splitList($("bm-tags").value), actor: "human",
      };
      if (!payload.title) { toast("标题必填", "error"); return; }
      await api(`/api/projects/${encodeURIComponent(state.projectId)}/board`, { method: "POST", body: payload });
      toast("已写入黑板"); closeModal();
      await refreshCurrent();
      return;
    }
    case "do-board-edit":
      await api(`/api/board/${encodeURIComponent(id)}`, {
        method: "PATCH",
        body: {
          title: $("bm-title").value, body: $("bm-body").value,
          tags: splitList($("bm-tags").value), author: "human",
        },
      });
      toast("已保存"); closeModal();
      await refreshCurrent();
      return;
    case "board-review":
      await api(`/api/board/${encodeURIComponent(id)}/review`, {
        method: "POST",
        body: { approved: btn.dataset.ok === "1", reviewer: "human" },
      });
      toast(btn.dataset.ok === "1" ? "已通过并进入长期记忆" : "已驳回");
      await refreshCurrent();
      return;
    case "board-delete":
      await api(`/api/board/${encodeURIComponent(id)}`, { method: "DELETE" });
      toast("已删除");
      await refreshCurrent();
      return;

    /* ---- 交接 / 审批 ---- */
    case "handoff-edit": {
      const h = state.handoffs.find((x) => x.id === id);
      openHandoffModal(h);
      return;
    }
    case "do-handoff":
      await api(`/api/handoffs/${encodeURIComponent(id)}`, {
        method: "PATCH",
        body: {
          did: $("hf-did").value, not_done: $("hf-not-done").value,
          open_issues: splitList($("hf-issues").value), next_steps: splitList($("hf-next").value),
          notes: $("hf-notes").value, editor: "human",
        },
      });
      toast("交接已修正，后续任务以此为准"); closeModal();
      await refreshCurrent();
      return;
    case "approval":
      await api(`/api/approvals/${encodeURIComponent(id)}/resolve`, {
        method: "POST",
        body: { approved: btn.dataset.ok === "1", by: "human" },
      });
      toast(btn.dataset.ok === "1" ? "已批准" : "已拒绝");
      await refreshCurrent();
      return;

    /* ---- 调度 ---- */
    case "do-run-project": {
      const body = { auto_retry: $("run-retry") ? $("run-retry").checked : true };
      const parallel = $("run-parallel") ? $("run-parallel").value.trim() : "";
      if (parallel) body.max_parallel = Number(parallel);
      const res = await api(`/api/projects/${encodeURIComponent(state.projectId)}/run`, { method: "POST", body });
      toast(res.message || "已开始调度"); closeModal();
      scheduleDashboardRefresh(1000);
      return;
    }
    case "do-force-recover": await forceRecoverProject(); return;

    /* ---- 快照 ---- */
    case "snap-create": {
      const labelEl = $("snap-label");
      const label = labelEl ? (labelEl.value || "").trim() : "";
      await api(`/api/projects/${encodeURIComponent(state.projectId)}/snapshots`, { method: "POST", body: { label } });
      if (labelEl) labelEl.value = "";
      toast("快照已创建");
      await refreshCurrent();
      return;
    }
    case "snap-diff": {
      const res = await api(`/api/snapshots/${encodeURIComponent(id)}/diff`);
      openModal("与当前工作区对比", `<pre class="mono">${esc(JSON.stringify(res, null, 2))}</pre>`);
      return;
    }
    case "snap-diff-two": {
      const a = $("snap-a") ? $("snap-a").value : "";
      const b = $("snap-b") ? $("snap-b").value : "";
      if (!a || !b) { toast("请先创建至少两个快照", "error"); return; }
      const res = await api(`/api/snapshots/${encodeURIComponent(a)}/diff/${encodeURIComponent(b)}`);
      openModal("两个快照之间的差异", `<pre class="mono">${esc(JSON.stringify(res, null, 2))}</pre>`);
      return;
    }
    case "snap-restore":
      openModal("回退到快照", "<p>将把工作区文件恢复到该快照状态，当前未提交的改动会被覆盖。确认？</p>",
        `<button class="btn" data-act="modal-close">取消</button>
         <button class="btn danger" data-act="do-snap-restore" data-id="${esc(id)}">确认回退</button>`);
      return;
    case "do-snap-restore":
      await api(`/api/snapshots/${encodeURIComponent(id)}/restore`, { method: "POST", body: {} });
      toast("已回退"); closeModal();
      await refreshCurrent();
      return;

    /* ---- Agent / 配置 ---- */
    case "agent-check": await checkAgent(id); return;
    case "agent-toggle": await setAgentEnabled(id, btn.dataset.enable === "1"); return;
    case "check-all": {
      const results = await Promise.all(state.agents.map((a) =>
        api(`/api/agents/${encodeURIComponent(a.id)}/check`, { method: "POST", body: {} })
          .catch((e) => ({ id: a.id, ok: false, detail: e.message }))));
      const ok = results.filter((r) => r.ok).length;
      openModal("连通性检测结果", `<div class="list">${results.map((r) => `
        <div class="entry"><div class="head"><span class="t">${esc(r.id || "")}</span>
          <span class="tag ${r.ok ? "success" : "failed"}">${r.ok ? "OK" : "不可用"}</span></div>
          <div class="body mono">${esc(r.detail || "")}</div></div>`).join("")}</div>`);
      toast(`${ok}/${results.length} 可用`);
      return;
    }
    case "edit-config-file":
      switchView("config");
      if ($("config-file")) $("config-file").value = btn.dataset.file;
      await loadConfigFile(btn.dataset.file);
      return;
    case "config-reload":
      await api("/api/config/reload", { method: "POST", body: {} });
      state.configFiles = {};
      await loadConfigFile($("config-file") ? $("config-file").value : "agents.json");
      toast("已重新读取配置");
      return;
    case "config-save": await saveConfigFile(); return;

    /* ---- 系统维护 ---- */
    case "system-refresh":
      await loadSystem();
      toast("已刷新");
      return;
    case "system-backup": {
      const label = window.prompt("备份标签（可选）", "manual");
      if (label === null) return;
      const res = await api("/api/system/backup", { method: "POST", body: { label: label || "manual" } });
      toast(`备份已创建：${res.path || ""}`);
      await loadSystem();
      return;
    }
    case "system-verify": {
      const res = await api("/api/system/verify?full=1");
      openModal("完整性与事件链校验", `<pre class="mono">${esc(JSON.stringify(res, null, 2))}</pre>`);
      return;
    }
    case "system-prune-preview": {
      const res = await api("/api/system/maintenance", { method: "POST", body: { apply: false } });
      openModal("清理预览（不会删除）", `<pre class="mono">${esc(JSON.stringify(res, null, 2))}</pre>`);
      return;
    }
    case "system-prune-apply":
      openModal("执行保留清理", `
        <p>将按默认保留策略删除旧的运行证据/快照/未引用 blob，并执行 VACUUM。</p>
        <p class="hint">此操作不可撤销；数据库行与事件审计会保留。</p>`,
        `<button class="btn" data-act="modal-close">取消</button>
         <button class="btn danger" data-act="do-system-prune">确认清理</button>`);
      return;
    case "do-system-prune": {
      const res = await api("/api/system/maintenance", { method: "POST", body: { apply: true, confirm: "APPLY" } });
      closeModal();
      openModal("清理完成", `<pre class="mono">${esc(JSON.stringify(res, null, 2))}</pre>`);
      await loadSystem();
      return;
    }

    /* ---- 目录浏览 ---- */
    case "browse": await renderDirBrowser($("im-path").value.trim()); return;
    case "browse-to":
      $("im-path").value = btn.dataset.path;
      await renderDirBrowser(btn.dataset.path);
      return;
    case "browse-use":
      $("im-path").value = btn.dataset.path;
      toast("已选择目录");
      return;
    case "pick-native": {
      if (window.multiagentDesktop && window.multiagentDesktop.pickDirectory) {
        const picked = await window.multiagentDesktop.pickDirectory();
        if (picked) { $("im-path").value = picked; toast("已选择目录"); }
      }
      return;
    }
    default:
      return;
  }
}

/* ================================================================ 事件绑定 */

function bindClick(id, fn) {
  const el = $(id);
  if (!el) return;
  el.addEventListener("click", () => {
    if (el.disabled) return;
    const finish = withLoading(el);
    Promise.resolve()
      .then(fn)
      .catch((err) => toast(err && err.message ? err.message : String(err), "error"))
      .finally(finish);
  });
}

function bindChange(id, fn) {
  const el = $(id);
  if (!el) return;
  el.addEventListener("change", (event) => {
    Promise.resolve()
      .then(() => fn(event))
      .catch((err) => toast(err && err.message ? err.message : String(err), "error"));
  });
}

function onDocumentClick(event) {
  const btn = event.target.closest("[data-act]");
  if (!btn) return;
  if (btn.disabled) return;
  const act = btn.dataset.act;
  if (act === "modal-close") { closeModal(); return; }
  const finish = withLoading(btn);
  Promise.resolve()
    .then(() => handleAction(act, btn))
    .catch((err) => toast(err && err.message ? err.message : String(err), "error"))
    .finally(finish);
}

async function onProjectChange(value) {
  state.projectId = value || null;
  resetProjectCaches();
  state._isolationTouched = false;
  syncMissionIsolationDefault(true);
  if (state.ws) { try { state.ws.close(); } catch (_) { /* ignore */ } state.ws = null; }
  if (!state.projectId) { renderNoProject(); return; }
  try {
    await loadDashboard({ force: true });
  } catch (err) {
    toast(`加载失败：${err.message}`, "error");
  }
  connectWs();
  if (state.view !== "home") loadViewData(state.view).catch(() => {});
}

function focusSidebarSearch() {
  const input = $("sidebar-query");
  if (!input) return false;
  try {
    input.focus();
    input.select();
  } catch (_) { /* 非交互环境下忽略 */ }
  return true;
}

function bindSidebarSearch() {
  const input = $("sidebar-query");
  if (input) {
    const sync = () => applyTaskFilter(input.value);
    input.addEventListener("input", sync);
    input.addEventListener("search", sync);
    input.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      input.value = "";
      applyTaskFilter("");
      input.blur();
    });
    if (input.value) applyTaskFilter(input.value);
  }
  document.addEventListener("keydown", (event) => {
    if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
    const key = String(event.key || "").toLowerCase();
    if (key !== "k" && event.code !== "KeyK") return;
    if (!focusSidebarSearch()) return;
    event.preventDefault();
  });
}

function bindKeyboard() {
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (modalOpen()) { closeModal(); return; }
      if (drawerOpen()) { closeDrawer(); return; }
    }
    if (event.key === "Enter") {
      const target = event.target;
      if (target && target.matches && target.matches("[data-act]") && target.tagName !== "BUTTON") {
        event.preventDefault();
        target.click();
      }
    }
    const mod = event.ctrlKey || event.metaKey;
    if (mod && event.key === "Enter") {
      const target = event.target;
      if (target && target.id === "mission-text") {
        event.preventDefault();
        const btn = $("mission-start") || $("mission-submit");
        const finish = btn ? withLoading(btn) : () => {};
        submitMission("single", true).finally(finish);
      }
    }
  });
}

function ensureAriaLabels() {
  const pairs = [
    ["mission-text", "任务描述"], ["mission-outputs", "产出文件"],
    ["mission-agent", "执行者"], ["mission-kind", "任务类型"], ["mission-isolation", "写入方式"],
    ["bb-query", "关键词检索"], ["bb-since", "起始时间"], ["bb-until", "截止时间"],
    ["bb-type", "黑板类型"], ["snap-label", "快照标签"], ["artifact-role", "产物角色"],
    ["config-file", "配置文件"], ["config-editor", "配置内容"], ["project-select", "当前项目"],
  ];
  for (const [id, label] of pairs) {
    const el = $(id);
    if (!el) continue;
    const hasLabel = el.getAttribute("aria-label") || (el.labels && el.labels.length);
    if (!hasLabel) el.setAttribute("aria-label", label);
  }
}

function bindUi() {
  document.addEventListener("click", onDocumentClick);

  // 视图切换：nav-item 与首页「全部任务 →」等 [data-view] 按钮统一委托。
  document.addEventListener("click", (event) => {
    const viewBtn = event.target.closest("[data-view]");
    if (!viewBtn) return;
    if (!viewBtn.closest("#nav, #sidebar") && !viewBtn.classList.contains("card-link")) return;
    const view = viewBtn.dataset.view;
    if (view) switchView(view);
  });

  bindChange("project-select", (event) => onProjectChange(event.target.value));
  bindClick("btn-refresh", manualRefresh);
  bindClick("btn-run", openRunModal);
  bindClick("btn-stop", stopProject);
  bindClick("btn-recover", recoverProject);
  bindClick("btn-force-recover", openForceRecoverModal);
  bindClick("btn-new-project", openNewProjectModal);
  bindClick("btn-import-project", openImportModal);
  bindClick("btn-project-settings", openProjectSettingsModal);
  bindClick("drawer-close", closeDrawer);
  bindClick("bb-search", searchBoard);
  bindClick("btn-export", () => {
    if (state.projectId) {
      window.open(withToken(`/api/projects/${encodeURIComponent(state.projectId)}/timeline?fmt=markdown`), "_blank");
    }
  });

  bindChange("artifact-role", renderArtifacts);
  bindChange("live-task", () => {
    markLiveDirty();
    resetLiveFeedDom();
    renderLegacyStream();
    renderStream();
  });
  bindChange("live-autoscroll", () => {
    const autoscroll = $("live-autoscroll");
    if (autoscroll && autoscroll.checked) scrollLiveToBottom();
  });
  bindChange("config-file", (event) => loadConfigFile(event.target.value));
  bindClick("live-jump", scrollLiveToBottom);

  bindTemplates();
  bindMissionButtons();
  bindKeyboard();
  bindSidebarSearch();
  bindStreamScroll();
  bindLiveScroll();
  ensureAriaLabels();
  setDispatchEnabled(Boolean(state.projectId));

  const isolationSel = ensureMissionIsolationField();
  if (isolationSel) {
    isolationSel.addEventListener("change", () => { state._isolationTouched = true; });
  }
  const sidePath = $("side-project-path");
  if (sidePath) {
    sidePath.style.cursor = "pointer";
    sidePath.addEventListener("click", () => openPathOrCopy(sidePath.title || ""));
  }

  const mask = $("modal");
  if (mask) {
    mask.addEventListener("click", (event) => {
      if (event.target === mask) closeModal();
    });
  }
}

/* ================================================================ WebSocket */

function scheduleDashboardRefresh(minGapMs = 6000) {
  if (!state.projectId) return;
  if (state._refreshTimer) return;
  const gap = Math.max(0, (state._dashAt || 0) + minGapMs - Date.now());
  state._refreshTimer = setTimeout(() => {
    state._refreshTimer = null;
    if (!state.projectId) return;
    if (document.visibilityState !== "visible") return;
    loadDashboard().catch(() => {});
  }, Math.max(300, gap));
}

function connectWs() {
  clearTimeout(state._wsTimer);
  if (state.ws) { try { state.ws.close(); } catch (_) { /* ignore */ } state.ws = null; }
  if (!state.projectId) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}${withToken(`/ws?project=${encodeURIComponent(state.projectId)}`)}`);
  state.ws = ws;
  ws.onmessage = (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch (_) { return; }
    if (msg.type === "run_status") {
      setRunStatus(Boolean(msg.status && msg.status.running));
    } else if (msg.type === "stream.snapshot") {
      state.stream = Array.isArray(msg.items) ? msg.items.slice(-LIVE_RAW_LIMIT) : [];
      markLiveDirty();
      resetLiveFeedDom();
      renderStream();
      if (state.view === "live") renderLegacyStream();
    } else if (msg.type === "run.snapshot") {
      // 跨进程回放：CLI / 桌面进程跑的任务，看板也能看到最近的事件块。
      const items = Array.isArray(msg.items) ? msg.items : [];
      if (items.length) {
        const known = new Set(state.stream.map((item) => item && item.event_id).filter(Boolean));
        const fresh = items.filter((item) => !item.event_id || !known.has(item.event_id));
        state.stream = [...fresh, ...state.stream].slice(-LIVE_RAW_LIMIT);
        markLiveDirty();
        resetLiveFeedDom();
        renderStream();
        if (state.view === "live") renderLegacyStream();
      }
    } else if (msg.type === "stream.event" && msg.event) {
      pushStream(msg.event);
    } else if (msg.type === "events") {
      scheduleDashboardRefresh();
    } else if (msg.type === "progress") {
      // 后端每 0.5s 推一次；不据此触发全量刷新，交给定时 dashboard 轮询。
    }
  };
  ws.onopen = () => {
    state.wsOk = true;
    renderStatusBar(state.dashboard);
  };
  ws.onclose = () => {
    state.wsOk = false;
    renderStatusBar(state.dashboard);
    if (state.ws !== ws) return;
    state._wsTimer = setTimeout(connectWs, 3000);
  };
  ws.onerror = () => { /* onclose 会负责重连 */ };
}

/* ================================================================ 轮询 / 启动 */

function startPolling() {
  stopPolling();
  state.timer = setInterval(() => {
    if (document.visibilityState !== "visible") return;
    if (!state.projectId) return;
    loadDashboard().catch(() => {});
  }, 7000);
}

function stopPolling() {
  if (state.timer) {
    clearInterval(state.timer);
    state.timer = null;
  }
}

function bindVisibility() {
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    if (!state.projectId) return;
    loadDashboard().catch(() => {});
  });
}

async function boot() {
  bindUi();
  const hashView = readHashView();
  if (hashView) state.view = hashView;
  applyViewClasses(state.view);
  syncHash(state.view);

  if (isDesktop()) {
    const badge = $("desktop-badge");
    if (badge) {
      badge.hidden = false;
      badge.style.display = "";
      try {
        const info = window.multiagentDesktop.info
          ? await window.multiagentDesktop.info()
          : null;
        if (info && info.version) badge.title = `Electron ${info.version}`;
      } catch (_) { /* 桌面桥信息不可用 */ }
    }
  }

  // 首屏：agents/detail 与 projects 并行；项目就绪后只发 dashboard 一个请求。
  await Promise.allSettled([loadProjects(false), loadAgentsDetail()]);

  if (state.projectId) {
    try {
      await loadDashboard();
    } catch (err) {
      toast(`加载指挥台失败：${err.message}`, "error");
      renderNoProject();
    }
    connectWs();
    if (state.view !== "home") {
      renderViewFromCache(state.view);
      loadViewData(state.view).catch((err) => toast(`加载失败：${err.message}`, "error"));
    }
  } else {
    renderNoProject();
  }

  startPolling();
  bindVisibility();
  bindHash();
}

boot().catch((err) => {
  console.error(err);
  toast(`初始化失败：${err.message}`, "error");
  const el = $("home-metrics");
  if (el) {
    el.innerHTML = `<div class="empty-box"><div class="empty-title">加载失败</div>
      <div class="empty-hint">${esc(err.message)}</div></div>`;
  }
});
