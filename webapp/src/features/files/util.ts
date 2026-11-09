import type { ProjectFile } from "../../api/types";

export type FileKind =
  | "code"
  | "data"
  | "doc"
  | "image"
  | "media"
  | "archive"
  | "config"
  | "binary"
  | "other";

export interface DesktopBridge {
  isDesktop?: boolean;
  openPath?: (path: string) => Promise<unknown> | unknown;
}

const KIND_BY_EXT: Record<string, FileKind> = {
  ts: "code",
  tsx: "code",
  js: "code",
  jsx: "code",
  mjs: "code",
  cjs: "code",
  vue: "code",
  svelte: "code",
  py: "code",
  go: "code",
  rs: "code",
  java: "code",
  kt: "code",
  kts: "code",
  c: "code",
  h: "code",
  cc: "code",
  cpp: "code",
  hpp: "code",
  cs: "code",
  rb: "code",
  php: "code",
  swift: "code",
  scala: "code",
  lua: "code",
  dart: "code",
  sh: "code",
  bash: "code",
  zsh: "code",
  ps1: "code",
  bat: "code",
  cmd: "code",
  html: "code",
  htm: "code",
  css: "code",
  scss: "code",
  less: "code",
  json: "data",
  jsonl: "data",
  yaml: "data",
  yml: "data",
  toml: "data",
  xml: "data",
  csv: "data",
  tsv: "data",
  sql: "data",
  db: "data",
  sqlite: "data",
  parquet: "data",
  md: "doc",
  mdx: "doc",
  txt: "doc",
  rst: "doc",
  log: "doc",
  pdf: "doc",
  png: "image",
  jpg: "image",
  jpeg: "image",
  gif: "image",
  webp: "image",
  svg: "image",
  ico: "image",
  bmp: "image",
  avif: "image",
  mp3: "media",
  wav: "media",
  flac: "media",
  ogg: "media",
  m4a: "media",
  mp4: "media",
  mov: "media",
  webm: "media",
  mkv: "media",
  avi: "media",
  zip: "archive",
  rar: "archive",
  "7z": "archive",
  tar: "archive",
  gz: "archive",
  tgz: "archive",
  bz2: "archive",
  xz: "archive",
  jar: "archive",
  war: "archive",
  env: "config",
  ini: "config",
  conf: "config",
  cfg: "config",
  properties: "config",
  lock: "config",
  exe: "binary",
  dll: "binary",
  so: "binary",
  dylib: "binary",
  bin: "binary",
  class: "binary",
  o: "binary",
  pyc: "binary",
  wasm: "binary",
};

export function basename(path: string): string {
  const parts = path.split(/[\\/]+/).filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

export function extOf(name: string): string {
  const clean = basename(name).toLowerCase();
  if (clean.startsWith(".") && clean.indexOf(".", 1) === -1) return clean.slice(1);
  const index = clean.lastIndexOf(".");
  return index > 0 ? clean.slice(index + 1) : "";
}

export function kindOf(name: string): FileKind {
  const base = basename(name).toLowerCase();
  if (
    ["dockerfile", "makefile", "procfile", "vagrantfile", "justfile"].includes(base) ||
    base.startsWith(".env")
  ) {
    return "config";
  }
  return KIND_BY_EXT[extOf(base)] ?? "other";
}

export function sortEntries(entries: ProjectFile[]): ProjectFile[] {
  return [...entries].sort((a, b) => {
    if (a.dir !== b.dir) return a.dir ? -1 : 1;
    return a.name.localeCompare(b.name, "zh-CN", { numeric: true, sensitivity: "base" });
  });
}

export function splitPath(path: string): { sep: string; parts: string[] } {
  const sep = path.includes("\\") && !path.includes("/") ? "\\" : "/";
  return { sep, parts: path.split(/[\\/]+/).filter(Boolean) };
}

export function ancestorsOf(path: string): string[] {
  const { sep, parts } = splitPath(path);
  parts.pop();
  const result: string[] = [];
  let current = "";
  for (const part of parts) {
    current = current ? `${current}${sep}${part}` : part;
    result.push(current);
  }
  return result;
}

export function dirOf(path: string): string {
  const { sep, parts } = splitPath(path);
  parts.pop();
  return parts.join(sep);
}

export function joinRoot(root: string | null | undefined, relative: string): string {
  if (!relative) return root ?? "";
  if (isAbsolutePath(relative)) return relative;
  if (!root) return relative;
  const sep = root.includes("\\") ? "\\" : "/";
  const tail = relative.replace(/[\\/]+/g, sep).replace(/^[\\/]+/, "");
  return `${root.replace(/[\\/]+$/, "")}${sep}${tail}`;
}

export function isAbsolutePath(path: string): boolean {
  return /^[A-Za-z]:[\\/]/.test(path) || path.startsWith("/") || path.startsWith("\\\\");
}

export function fmtMtime(mtime: number | null | undefined): string {
  if (!mtime) return "";
  const ms = mtime < 1e12 ? mtime * 1000 : mtime;
  const date = new Date(ms);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("zh-CN", {
    hour12: false,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function getDesktop(): DesktopBridge | undefined {
  return (window as unknown as { multiagentDesktop?: DesktopBridge }).multiagentDesktop;
}

export async function openExternal(target: string): Promise<string | null> {
  const bridge = getDesktop();
  if (!bridge?.openPath) return "当前不在桌面端，无法调用系统打开";
  try {
    const result = await bridge.openPath(target);
    if (typeof result === "string" && result.trim()) return result;
    return null;
  } catch (error) {
    return error instanceof Error ? error.message : String(error);
  }
}

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "true");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      const ok = document.execCommand("copy");
      area.remove();
      return ok;
    } catch {
      return false;
    }
  }
}
