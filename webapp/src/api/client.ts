const TOKEN_KEYS = ["multiagent_token", "multiagent.token"];

function readToken(): string {
  try {
    const fromQuery = new URLSearchParams(window.location.search).get("token");
    if (fromQuery) return fromQuery;
    for (const key of TOKEN_KEYS) {
      const value = window.localStorage.getItem(key);
      if (value) return value;
    }
  } catch {
    /* storage may be unavailable */
  }
  return "";
}

export const API_TOKEN = readToken();

export class ApiError extends Error {
  status: number;
  body: unknown;

  constructor(status: number, message: string, body?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

function headers(extra?: HeadersInit): Headers {
  const out = new Headers(extra);
  if (!out.has("Content-Type") && extra && !(extra as Record<string, string>)["Content-Type"]) {
    // only set for body requests; GETs are fine without it
  }
  if (API_TOKEN) out.set("Authorization", `Bearer ${API_TOKEN}`);
  return out;
}

async function parseError(res: Response): Promise<never> {
  let body: unknown = null;
  let message = `${res.status} ${res.statusText}`;
  try {
    body = await res.json();
    const record = body as Record<string, unknown> | null;
    const detail = record?.detail;
    if (typeof detail === "string") message = detail;
    else if (detail) message = JSON.stringify(detail);
    else if (typeof record?.message === "string" && record.message) {
      // 结构化 409（merge 冲突 / 脏树阻断）用顶层 message 给人看；
      // files/branch/dirty_files 等结构化字段仍保留在 ApiError.body 供 UI 使用。
      message = record.message;
    } else if (typeof record?.error === "string" && record.error) {
      message = record.error;
    }
  } catch {
    /* keep default */
  }
  throw new ApiError(res.status, message, body);
}

export async function request<T>(
  path: string,
  options: RequestInit & { json?: unknown } = {},
): Promise<T> {
  const { json, ...init } = options;
  const h = headers(init.headers);
  let body = init.body;
  if (json !== undefined) {
    h.set("Content-Type", "application/json");
    body = JSON.stringify(json);
  }
  const res = await fetch(path, { ...init, headers: h, body });
  if (!res.ok) await parseError(res);
  if (res.status === 204) return undefined as T;
  const type = res.headers.get("content-type") || "";
  if (!type.includes("application/json")) return (await res.text()) as T;
  return (await res.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, json?: unknown) => request<T>(path, { method: "POST", json }),
  patch: <T>(path: string, json?: unknown) => request<T>(path, { method: "PATCH", json }),
  put: <T>(path: string, json?: unknown) => request<T>(path, { method: "PUT", json }),
  del: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};

function base64Url(value: string): string {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  bytes.forEach((byte) => {
    binary += String.fromCharCode(byte);
  });
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** WebSocket 子协议：token 走握手头，不出现在 URL / 访问日志 / 历史记录里。 */
export function wsProtocols(): string[] {
  return API_TOKEN ? [`multiagent.token.${base64Url(API_TOKEN)}`] : [];
}

export function wsUrl(projectId?: string): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const params = new URLSearchParams();
  if (projectId) params.set("project", projectId);
  const qs = params.toString();
  return `${proto}//${window.location.host}/ws${qs ? `?${qs}` : ""}`;
}

export const qk = {
  projects: ["projects"] as const,
  dashboard: (pid: string) => ["dashboard", pid] as const,
  task: (tid: string) => ["task", tid] as const,
  agents: ["agents"] as const,
  agentDetail: ["agents", "detail"] as const,
  files: (pid: string, path: string) => ["files", pid, path] as const,
  git: (pid: string) => ["git", pid] as const,
};
