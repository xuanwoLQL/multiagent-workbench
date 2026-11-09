import { useMemo, useState } from "react";
import { ShieldCheck } from "lucide-react";
import { fmtRelative } from "../../lib/format";
import { Badge, Button, EmptyState, Input, Select } from "../../ui/primitives";
import { useToast } from "../../ui/toast";
import {
  useApprovals,
  useResolveApproval,
  type ApprovalRecord,
} from "./memory-api";
import {
  approvalStatusLabel,
  approvalStatusTone,
  LoadError,
  shortId,
  SkeletonGrid,
  type BadgeTone,
} from "./memory-shared";

const KIND_LABEL: Record<string, string> = {
  dangerous_op: "危险操作",
  manual_confirm: "人工确认",
  acceptance_failed: "验收失败",
  conflict: "冲突",
  permission: "权限请求",
  other: "其他",
};

const RISK_LABEL: Record<string, string> = {
  low: "低风险",
  medium: "中风险",
  high: "高风险",
  critical: "严重风险",
};

const STATUS_OPTIONS = [
  { value: "pending", label: "待处理" },
  { value: "approved", label: "已通过" },
  { value: "rejected", label: "已驳回" },
  { value: "all", label: "全部状态" },
];

interface ApprovalTabProps {
  projectId: string;
  query: string;
  onClearSearch: () => void;
}

function riskTone(risk: string): BadgeTone {
  if (risk === "critical" || risk === "high") return "danger";
  if (risk === "medium") return "warning";
  if (risk === "low") return "info";
  return "default";
}

function statusRank(status: string): number {
  if (status === "pending") return 0;
  if (status === "approved") return 1;
  return 2;
}

function timeValue(iso?: string): number {
  const value = iso ? Date.parse(iso) : 0;
  return Number.isNaN(value) ? 0 : value;
}

function matchesQuery(approval: ApprovalRecord, needle: string): boolean {
  if (!needle) return true;
  const haystack = [
    approval.title,
    approval.kind,
    KIND_LABEL[approval.kind] ?? "",
    approval.risk ?? "",
    RISK_LABEL[(approval.risk ?? "").toLowerCase()] ?? "",
    approval.status,
    approvalStatusLabel(approval.status),
    approval.task_id ?? "",
    approval.agent_id ?? "",
    approval.resolution ?? "",
  ]
    .join(" ")
    .toLowerCase();
  return haystack.includes(needle);
}

export function MemoryApprovals({
  projectId,
  query,
  onClearSearch,
}: ApprovalTabProps) {
  const approvals = useApprovals(projectId);
  const [status, setStatus] = useState("pending");
  const all = approvals.data ?? [];
  const pending = all.filter((item) => item.status === "pending").length;
  const needle = query.trim().toLowerCase();

  const rows = useMemo(
    () =>
      all
        .filter(
          (item) =>
            (status === "all" || item.status === status) &&
            matchesQuery(item, needle),
        )
        .sort(
          (left, right) =>
            statusRank(left.status) - statusRank(right.status) ||
            timeValue(right.created_at) - timeValue(left.created_at),
        ),
    [all, needle, status],
  );

  const filtering = Boolean(needle) || status !== "pending";

  return (
    <div className="stack">
      <div className="memory-section-head">
        <div>
          <h2>人工确认</h2>
          <div className="memory-section-sub">
            危险操作、验收失败与冲突会暂停在这里，等待你给出结论。
          </div>
        </div>
        <div className="memory-section-actions">
          <Badge tone={pending ? "warning" : "success"} dot>
            {pending} 项待处理
          </Badge>
          <Select
            value={status}
            onChange={(event) => setStatus(event.target.value)}
            aria-label="按确认状态筛选"
          >
            {STATUS_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
        </div>
      </div>

      {approvals.isLoading ? (
        <SkeletonGrid count={4} />
      ) : approvals.isError ? (
        <LoadError
          title="无法读取人工确认队列"
          message={(approvals.error as Error)?.message}
          onRetry={() => void approvals.refetch()}
        />
      ) : rows.length === 0 ? (
        <div className="memory-empty-panel">
          <EmptyState
            icon={<ShieldCheck size={18} />}
            title={
              all.length === 0
                ? "没有人工确认项"
                : status === "pending" && !needle
                  ? "待处理项已清空"
                  : "没有匹配的确认项"
            }
            hint={
              all.length === 0
                ? "Agent 遇到需要人工判断的操作时会在这里请求确认；当前没有需要你介入的事项。"
                : status === "pending" && !needle
                  ? `共有 ${all.length} 条历史记录，可以切到「全部状态」回看处理结果。`
                  : "换个关键词或状态筛选，或者查看全部记录。"
            }
            action={
              all.length > 0 ? (
                <Button
                  size="sm"
                  onClick={() => {
                    setStatus("all");
                    onClearSearch();
                  }}
                >
                  查看全部记录
                </Button>
              ) : undefined
            }
          />
        </div>
      ) : (
        <div className="memory-approval-grid">
          {rows.map((approval) => (
            <ApprovalCard key={approval.id} approval={approval} />
          ))}
        </div>
      )}
      {filtering && rows.length > 0 ? (
        <div className="memory-list-foot">
          正在显示 {rows.length} / {all.length} 条记录
        </div>
      ) : null}
    </div>
  );
}

function ApprovalCard({ approval }: { approval: ApprovalRecord }) {
  const toast = useToast();
  const resolve = useResolveApproval();
  const [note, setNote] = useState("");
  const risk = (approval.risk ?? "").toLowerCase();
  const pending = approval.status === "pending";

  const act = async (decision: "approve" | "reject") => {
    try {
      await resolve.mutateAsync({
        id: approval.id,
        decision,
        note: note.trim() || undefined,
      });
      setNote("");
      toast.success(
        decision === "approve" ? "已通过确认项" : "已驳回确认项",
        approval.title,
      );
    } catch (error) {
      toast.error("处理失败", (error as Error).message);
    }
  };

  return (
    <article className="card memory-approval-card">
      <div className="memory-approval-head">
        <div className="row memory-badge-row">
          <Badge tone={riskTone(risk)} dot>
            {RISK_LABEL[risk] ?? (approval.risk || "未标注风险")}
          </Badge>
          <Badge tone="outline">
            {KIND_LABEL[approval.kind] ?? approval.kind}
          </Badge>
          <Badge tone={approvalStatusTone(approval.status)}>
            {approvalStatusLabel(approval.status)}
          </Badge>
        </div>
        <span className="memory-time">{fmtRelative(approval.created_at)}</span>
      </div>

      <div className="memory-approval-body">
        <h3 className="memory-approval-title">
          {approval.title || "（未命名确认项）"}
        </h3>
        <div className="memory-approval-meta">
          <span>任务 {shortId(approval.task_id)}</span>
          {approval.agent_id ? <span>Agent {approval.agent_id}</span> : null}
          {approval.run_id ? <span>Run {shortId(approval.run_id)}</span> : null}
        </div>
        <ApprovalDetail detail={approval.detail} />
      </div>

      {pending ? (
        <div className="memory-approval-actions">
          <Input
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="处理备注（可选）"
            aria-label="处理备注"
          />
          <Button
            size="sm"
            variant="danger"
            onClick={() => void act("reject")}
            loading={resolve.isPending && resolve.variables?.decision === "reject"}
            disabled={resolve.isPending}
          >
            驳回
          </Button>
          <Button
            size="sm"
            variant="primary"
            onClick={() => void act("approve")}
            loading={resolve.isPending && resolve.variables?.decision === "approve"}
            disabled={resolve.isPending}
          >
            通过
          </Button>
        </div>
      ) : approval.resolution ? (
        <div className="memory-resolution">
          处理备注：{approval.resolution}
          {approval.resolved_by ? ` · ${approval.resolved_by}` : ""}
        </div>
      ) : null}
    </article>
  );
}

function ApprovalDetail({ detail }: { detail: ApprovalRecord["detail"] }) {
  const entries = useMemo(() => detailEntries(detail), [detail]);
  if (!entries.length) {
    return <div className="memory-detail-empty">无额外详情</div>;
  }
  return (
    <dl className="memory-kv">
      {entries.map(([key, value], index) => (
        <div key={`${key}-${index}`} className="memory-kv-row">
          <dt className="memory-kv-key">{key}</dt>
          <dd className="memory-kv-value">
            <DetailValue value={value} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

function detailEntries(
  detail: ApprovalRecord["detail"],
): Array<[string, unknown]> {
  if (!detail) return [];
  if (typeof detail === "string") return [["详情", detail]];
  return Object.entries(detail).filter(([, value]) => value !== undefined);
}

function DetailValue({ value }: { value: unknown }) {
  if (value === null || value === undefined || value === "") {
    return <span className="muted">—</span>;
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return <span className="memory-kv-text">{String(value)}</span>;
  }
  if (Array.isArray(value)) {
    return (
      <span className="memory-kv-text">
        {value
          .map((item) => (typeof item === "string" ? item : JSON.stringify(item)))
          .join("、")}
      </span>
    );
  }
  return <pre className="memory-kv-json">{JSON.stringify(value, null, 2)}</pre>;
}
