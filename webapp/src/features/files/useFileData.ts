import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ApiError, api, qk } from "../../api/client";
import type { FileTreeResponse } from "../../api/types";
import { basename } from "./util";

export interface FileContentResponse {
  path: string;
  binary: boolean;
  size: number;
  content?: string;
  truncated?: boolean;
}

export interface SearchMatch {
  path: string;
  line?: number;
  text?: string;
}

export interface SearchResponse {
  matches?: SearchMatch[];
  truncated?: boolean;
}

export function useDir(pid: string | null, path: string, enabled = true) {
  return useQuery({
    queryKey: qk.files(pid ?? "", path),
    queryFn: async () => {
      const result = await api.get<FileTreeResponse>(
        `/api/projects/${encodeURIComponent(pid ?? "")}/files?path=${encodeURIComponent(path)}`,
      );
      if (!result || !Array.isArray(result.entries)) {
        throw new Error("目录接口返回格式不正确");
      }
      return {
        root: String(result.root ?? ""),
        truncated: Boolean(result.truncated),
        entries: result.entries
          .map((entry) => {
            const entryPath = String(entry?.path ?? "");
            return {
              path: entryPath,
              name: String(entry?.name ?? basename(entryPath)),
              dir: Boolean(entry?.dir),
              size: Number(entry?.size ?? 0) || 0,
              mtime: Number(entry?.mtime ?? 0) || 0,
            };
          })
          .filter((entry) => Boolean(entry.path)),
      };
    },
    enabled: Boolean(pid) && enabled,
    retry: false,
    staleTime: 10_000,
  });
}

export function useFileContent(pid: string | null, path: string | null) {
  return useQuery({
    queryKey: ["file", pid ?? "", path ?? ""],
    queryFn: async () => {
      const result = await api.get<FileContentResponse>(
        `/api/projects/${encodeURIComponent(pid ?? "")}/file?path=${encodeURIComponent(path ?? "")}`,
      );
      if (!result || typeof result.path !== "string") {
        throw new Error("文件接口返回格式不正确");
      }
      return {
        path: result.path,
        binary: Boolean(result.binary),
        size: Number(result.size ?? 0) || 0,
        content: typeof result.content === "string" ? result.content : undefined,
        truncated: Boolean(result.truncated),
      };
    },
    enabled: Boolean(pid) && Boolean(path),
    retry: false,
    staleTime: 15_000,
  });
}

export function useFileSearch(pid: string | null, query: string) {
  const q = query.trim();
  return useQuery({
    queryKey: ["file-search", pid ?? "", q],
    queryFn: async () => {
      const result = await api.get<SearchResponse>(
        `/api/projects/${encodeURIComponent(pid ?? "")}/search?q=${encodeURIComponent(q)}`,
      );
      if (!result || !Array.isArray(result.matches)) {
        throw new Error("搜索接口返回格式不正确");
      }
      return {
        truncated: Boolean(result.truncated),
        matches: result.matches
          .map((match) => ({
            path: String(match?.path ?? ""),
            line: Number.isFinite(Number(match?.line)) ? Number(match?.line) : undefined,
            text: typeof match?.text === "string" ? match.text : undefined,
          }))
          .filter((match) => Boolean(match.path)),
      };
    },
    enabled: Boolean(pid) && q.length >= 2,
    retry: false,
    staleTime: 30_000,
  });
}

export function useDebounced<T>(value: T, delay = 220): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}

export function isMissingRoute(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    (error.status === 404 || error.status === 405 || error.status === 501)
  );
}

export function filesErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 404) return "接口不存在或文件未找到（404）";
    return `${error.status} · ${error.message}`;
  }
  if (error instanceof Error) return error.message;
  return error ? String(error) : "未知错误";
}
