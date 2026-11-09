import { useEffect, useState } from "react";
import { AlertTriangle } from "lucide-react";
import { Button, EmptyState } from "../../ui/primitives";

export type BadgeTone =
  | "default"
  | "success"
  | "warning"
  | "danger"
  | "info"
  | "accent"
  | "outline";

const BOARD_TYPE_LABEL: Record<string, string> = {
  brief: "简报",
  artifact: "产物",
  decision: "决策",
  handoff: "交接",
  todo: "待办",
  issue: "问题",
  reference: "参考",
  note: "笔记",
};

const BOARD_TYPE_TONE: Record<string, BadgeTone> = {
  brief: "accent",
  artifact: "info",
  decision: "accent",
  handoff: "info",
  todo: "warning",
  issue: "danger",
  reference: "default",
  note: "default",
};

const BOARD_STATUS_LABEL: Record<string, string> = {
  approved: "已通过",
  pending: "待审核",
  rejected: "已驳回",
  draft: "草稿",
  archived: "已归档",
};

const BOARD_STATUS_TONE: Record<string, BadgeTone> = {
  approved: "success",
  pending: "warning",
  rejected: "danger",
  draft: "default",
  archived: "default",
};

const APPROVAL_STATUS_LABEL: Record<string, string> = {
  pending: "待处理",
  approved: "已通过",
  rejected: "已驳回",
};

const APPROVAL_STATUS_TONE: Record<string, BadgeTone> = {
  pending: "warning",
  approved: "success",
  rejected: "danger",
};

export function boardTypeLabel(type: string): string {
  return BOARD_TYPE_LABEL[type] ?? (type || "笔记");
}

export function boardTypeTone(type: string): BadgeTone {
  return BOARD_TYPE_TONE[type] ?? "default";
}

export function boardStatusLabel(status: string): string {
  return BOARD_STATUS_LABEL[status] ?? status;
}

export function boardStatusTone(status: string): BadgeTone {
  return BOARD_STATUS_TONE[status] ?? "default";
}

export function approvalStatusLabel(status: string): string {
  return APPROVAL_STATUS_LABEL[status] ?? status;
}

export function approvalStatusTone(status: string): BadgeTone {
  return APPROVAL_STATUS_TONE[status] ?? "default";
}

export function shortId(value?: string | null): string {
  if (!value) return "—";
  return value.length > 10 ? `#${value.slice(-8)}` : `#${value}`;
}

export function asList(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value
      .map((item) => {
        if (typeof item === "string") return item;
        if (item === null || item === undefined) return "";
        if (typeof item === "object") return JSON.stringify(item);
        return String(item);
      })
      .filter((item) => item.trim().length > 0);
  }
  if (typeof value === "string" && value.trim()) return [value.trim()];
  return [];
}

export function useDebouncedValue<T>(value: T, delay = 250): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}

export function SkeletonGrid({ count = 6 }: { count?: number }) {
  return (
    <div role="status" aria-live="polite">
      <span className="sr-only">正在加载</span>
      <div className="memory-skeleton-grid" aria-hidden="true">
        {Array.from({ length: count }).map((_, index) => (
          <div key={index} className="skeleton memory-skeleton-card" />
        ))}
      </div>
    </div>
  );
}

export function SkeletonRows({ count = 4 }: { count?: number }) {
  return (
    <div role="status" aria-live="polite">
      <span className="sr-only">正在加载</span>
      <div className="memory-skeleton-list" aria-hidden="true">
        {Array.from({ length: count }).map((_, index) => (
          <div key={index} className="skeleton memory-skeleton-row" />
        ))}
      </div>
    </div>
  );
}

export function LoadError({
  title = "读取失败",
  message,
  onRetry,
}: {
  title?: string;
  message?: string;
  onRetry: () => void;
}) {
  return (
    <div className="memory-empty-panel">
      <EmptyState
        icon={<AlertTriangle size={18} />}
        title={title}
        hint={message || "接口暂时不可用，请稍后重试。"}
        action={
          <Button size="sm" onClick={onRetry}>
            重试
          </Button>
        }
      />
    </div>
  );
}
