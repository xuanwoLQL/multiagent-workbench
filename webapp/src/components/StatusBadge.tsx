import { Badge } from "../ui/primitives";
import { STATUS_LABEL, STATUS_TONE } from "../lib/format";
import type { TaskStatus } from "../api/types";

export function StatusBadge({ status, dot = true }: { status: TaskStatus; dot?: boolean }) {
  return (
    <Badge tone={STATUS_TONE[status] ?? "default"} dot={dot}>
      {STATUS_LABEL[status] ?? status}
    </Badge>
  );
}

export function RunStatusBadge({ status }: { status: string }) {
  const map: Record<string, { label: string; tone: "success" | "danger" | "accent" | "warning" | "default" }> = {
    running: { label: "运行中", tone: "accent" },
    success: { label: "成功", tone: "success" },
    failed: { label: "失败", tone: "danger" },
    blocked: { label: "阻塞", tone: "warning" },
    waiting: { label: "待人工", tone: "warning" },
    cancelled: { label: "已取消", tone: "default" },
    interrupted: { label: "已中断", tone: "danger" },
  };
  const item = map[status] ?? { label: status, tone: "default" as const };
  return (
    <Badge tone={item.tone} dot>
      {item.label}
    </Badge>
  );
}
