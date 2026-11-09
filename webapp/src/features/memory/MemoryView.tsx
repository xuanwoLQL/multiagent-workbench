import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  BookOpen,
  Camera,
  ClipboardList,
  RefreshCw,
  Search,
  ShieldCheck,
} from "lucide-react";
import { useUi } from "../../app/store";
import { Button, EmptyState, Input, Segmented } from "../../ui/primitives";
import { useToast } from "../../ui/toast";
import { MemoryApprovals } from "./MemoryApprovals";
import { MemoryBoard } from "./MemoryBoard";
import { MemoryHandoffs } from "./MemoryHandoffs";
import { MemorySnapshots } from "./MemorySnapshots";
import "./memory.css";

type MemoryTab = "board" | "handoffs" | "approvals" | "snapshots";

const PLACEHOLDER: Record<MemoryTab, string> = {
  board: "搜索标题、正文、标签、Agent 或任务…",
  handoffs: "搜索任务、做了什么、遗留问题…",
  approvals: "搜索确认项、类型或任务…",
  snapshots: "搜索快照标签或 ID…",
};

export function MemoryView() {
  const projectId = useUi((state) => state.projectId);
  const client = useQueryClient();
  const toast = useToast();
  const [tab, setTab] = useState<MemoryTab>("board");
  const [query, setQuery] = useState("");
  const [refreshing, setRefreshing] = useState(false);

  const clearSearch = () => setQuery("");

  const refresh = async () => {
    setRefreshing(true);
    try {
      await client.invalidateQueries({ queryKey: ["memory"] });
      toast.info("已刷新记忆与协作数据");
    } catch (error) {
      toast.error("刷新失败", (error as Error).message);
    } finally {
      setRefreshing(false);
    }
  };

  if (!projectId) {
    return (
      <div className="page">
        <EmptyState
          title="先选择一个项目"
          hint="共享黑板、交接说明、人工确认与版本快照都属于当前项目。"
        />
      </div>
    );
  }

  return (
    <div className="page wide memory-page">
      <div className="page-head">
        <div className="page-head-text">
          <div className="memory-eyebrow">MEMORY / COLLABORATION</div>
          <h1 className="page-title">记忆与协作</h1>
          <p className="page-subtitle">
            把项目上下文沉淀下来：共享黑板记录决策，交接说明延续工作，人工确认守住风险，版本快照保留可回退的现场。
          </p>
        </div>
        <div className="page-actions">
          <Button onClick={() => void refresh()} loading={refreshing}>
            <RefreshCw size={14} />
            刷新
          </Button>
        </div>
      </div>

      <div className="memory-toolbar">
        <Segmented<MemoryTab>
          value={tab}
          onChange={(next) => {
            setTab(next);
            setQuery("");
          }}
          ariaLabel="记忆与协作视图"
          options={[
            {
              value: "board",
              label: (
                <>
                  <BookOpen size={13} /> 共享黑板
                </>
              ),
            },
            {
              value: "handoffs",
              label: (
                <>
                  <ClipboardList size={13} /> 交接说明
                </>
              ),
            },
            {
              value: "approvals",
              label: (
                <>
                  <ShieldCheck size={13} /> 人工确认
                </>
              ),
            },
            {
              value: "snapshots",
              label: (
                <>
                  <Camera size={13} /> 版本快照
                </>
              ),
            },
          ]}
        />
        <div className="memory-search">
          <Search size={14} aria-hidden="true" />
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={PLACEHOLDER[tab]}
            aria-label="搜索当前视图"
          />
        </div>
      </div>

      {tab === "board" ? (
        <MemoryBoard
          projectId={projectId}
          query={query}
          onClearSearch={clearSearch}
        />
      ) : null}
      {tab === "handoffs" ? (
        <MemoryHandoffs
          projectId={projectId}
          query={query}
          onClearSearch={clearSearch}
        />
      ) : null}
      {tab === "approvals" ? (
        <MemoryApprovals
          projectId={projectId}
          query={query}
          onClearSearch={clearSearch}
        />
      ) : null}
      {tab === "snapshots" ? (
        <MemorySnapshots
          projectId={projectId}
          query={query}
          onClearSearch={clearSearch}
        />
      ) : null}
    </div>
  );
}
