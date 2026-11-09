import {
  AlertTriangle,
  FolderKanban,
  FolderOpen,
  GitBranch,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { fmtDateTime, fmtRelative, initials, shortPath } from "../../lib/format";
import { IconButton } from "../../ui/primitives";
import type { AgentUsage } from "../../api/types";
import type { ProjectCardData } from "./projects-api";

const AGENT_TONES = 5;

function agentLabel(agent: AgentUsage): string {
  return String(agent.label || agent.id || "Agent");
}

function agentInvocations(agent: AgentUsage): number {
  const value = agent.usage?.invocations ?? agent.invocations;
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function buildAvatars(agents: AgentUsage[]): {
  visible: AgentUsage[];
  overflow: number;
  total: number;
} {
  const sorted = [...agents].sort(
    (a, b) => agentInvocations(b) - agentInvocations(a),
  );
  const visible = sorted.slice(0, 4);
  return {
    visible,
    overflow: Math.max(0, sorted.length - visible.length),
    total: sorted.length,
  };
}

export function ProjectCard({
  card,
  disabledDelete,
  onOpen,
  onOpenDirectory,
  onDelete,
}: {
  card: ProjectCardData;
  disabledDelete: boolean;
  onOpen: () => void;
  onOpenDirectory: () => void;
  onDelete: () => void;
}) {
  const { project, dashboard, git } = card;
  const displayName = project.label || project.name || "未命名项目";
  const avatars = buildAvatars(card.agents);
  const gitTitle = git?.is_repo
    ? `${git.branch || "detached HEAD"}${git.dirty ? " · 有未提交修改" : " · 工作区干净"}`
    : "该目录不是 Git 仓库";

  return (
    <article className="pj-card" aria-labelledby={`pj-name-${project.id}`}>
      <button
        type="button"
        className="pj-card-hit"
        aria-label={`打开项目 ${displayName}`}
        onClick={onOpen}
      />

      <div className="pj-card-body">
        <header className="pj-card-head">
          <div className="pj-mark" aria-hidden="true">
            <FolderKanban size={16} />
          </div>
          <div className="pj-title-block">
            <div id={`pj-name-${project.id}`} className="pj-name" title={displayName}>
              {displayName}
            </div>
            <div className="pj-path" title={project.root_path}>
              {shortPath(project.root_path, 4)}
            </div>
          </div>
          <div className="pj-card-actions">
            <IconButton
              label="打开目录"
              title="在系统文件管理器中打开"
              onClick={(event) => {
                event.stopPropagation();
                onOpenDirectory();
              }}
            >
              <FolderOpen size={14} />
            </IconButton>
            <IconButton
              label="删除项目"
              title={disabledDelete ? "至少保留一个项目" : "从工作台移除项目"}
              disabled={disabledDelete}
              onClick={(event) => {
                event.stopPropagation();
                onDelete();
              }}
            >
              <Trash2 size={14} />
            </IconButton>
          </div>
        </header>

        <div className="pj-status-row">
          <span className={card.running ? "pj-run is-running" : "pj-run"}>
            <span className="pj-run-dot" aria-hidden="true" />
            {card.running
              ? card.runningCount > 1
                ? `运行中 · ${card.runningCount}`
                : "运行中"
              : "空闲"}
          </span>

          {card.gitLoading ? (
            <span className="pj-git-skeleton skeleton" aria-label="正在读取 Git 状态" />
          ) : card.gitError ? (
            <button
              type="button"
              className="pj-inline-retry"
              onClick={(event) => {
                event.stopPropagation();
                card.refetchGit();
              }}
              title={card.gitError.message}
            >
              <AlertTriangle size={12} /> Git 状态不可用
            </button>
          ) : git?.is_repo ? (
            <span className="pj-git" title={gitTitle}>
              <GitBranch size={12} />
              <span className="pj-git-branch">{git.branch || "detached"}</span>
              <span
                className={git.dirty ? "pj-dirty is-dirty" : "pj-dirty"}
                role="img"
                aria-label={git.dirty ? "有未提交修改" : "工作区干净"}
                title={git.dirty ? "有未提交修改" : "工作区干净"}
              />
            </span>
          ) : (
            <span className="pj-git is-muted" title={gitTitle}>
              <GitBranch size={12} /> 非 Git 仓库
            </span>
          )}
        </div>

        <div className="pj-counts">
          {card.dashboardLoading ? (
            <>
              <span className="pj-count-skeleton skeleton" />
              <span className="pj-count-skeleton skeleton" />
              <span className="pj-count-skeleton skeleton" />
            </>
          ) : card.dashboardError ? (
            <button
              type="button"
              className="pj-inline-retry"
              onClick={(event) => {
                event.stopPropagation();
                card.refetchDashboard();
              }}
              title={card.dashboardError.message}
            >
              <RefreshCw size={12} /> 统计加载失败，重试
            </button>
          ) : (
            <>
              <span className="pj-count">
                <strong>{card.taskCount ?? 0}</strong> 任务
              </span>
              <span className={card.successCount > 0 ? "pj-count is-success" : "pj-count"}>
                <strong>{card.successCount}</strong> 成功
              </span>
              <span className={card.failedCount > 0 ? "pj-count is-danger" : "pj-count"}>
                <strong>{card.failedCount}</strong> 失败
              </span>
            </>
          )}
        </div>

        <footer className="pj-card-foot">
          <div
            className="pj-avatars"
            aria-label={
              avatars.total
                ? `Agent 编队：${avatars.visible.map(agentLabel).join("、")}`
                : "未配置 Agent"
            }
          >
            {card.dashboardLoading ? (
              <span className="pj-avatar-skeleton skeleton" />
            ) : avatars.visible.length ? (
              <>
                {avatars.visible.map((agent, index) => (
                  <span
                    key={String(agent.id || agent.agent_id || index)}
                    className={`pj-avatar tone-${index % AGENT_TONES}`}
                    title={`${agentLabel(agent)} · ${agentInvocations(agent)} 次调用`}
                  >
                    {initials(agentLabel(agent))}
                  </span>
                ))}
                {avatars.overflow ? (
                  <span className="pj-avatar is-more" title={`还有 ${avatars.overflow} 个 Agent`}>
                    +{avatars.overflow}
                  </span>
                ) : null}
              </>
            ) : (
              <span className="pj-no-agents">未配置 Agent</span>
            )}
          </div>

          <span
            className="pj-active"
            title={card.lastActive ? fmtDateTime(card.lastActive) : "暂无活动"}
          >
            {dashboard ? "最近 " : ""}
            {card.lastActive ? fmtRelative(card.lastActive) : "暂无活动"}
          </span>
        </footer>
      </div>
    </article>
  );
}

export function ProjectCardSkeleton() {
  return (
    <div className="pj-card pj-card-skeleton" aria-hidden="true">
      <div className="pj-skeleton-head">
        <span className="pj-skeleton-mark skeleton" />
        <div className="pj-skeleton-title">
          <span className="pj-skeleton-name skeleton" />
          <span className="pj-skeleton-path skeleton" />
        </div>
      </div>
      <span className="pj-skeleton-row skeleton" />
      <span className="pj-skeleton-counts skeleton" />
      <span className="pj-skeleton-foot skeleton" />
    </div>
  );
}
