import { useEffect, useMemo, useState } from "react";
import clsx from "clsx";
import { AlertTriangle, ChevronDown, ChevronRight, FolderOpen, RefreshCw } from "lucide-react";
import type { ProjectFile } from "../../api/types";
import { fmtBytes } from "../../lib/format";
import { Badge, Button } from "../../ui/primitives";
import { FileGlyph } from "./FileGlyph";
import { filesErrorText, useDir } from "./useFileData";
import { fmtMtime, sortEntries } from "./util";

const ENTRY_PAGE = 200;
const SKELETON_WIDTHS = ["72%", "54%", "83%", "46%", "64%", "59%"];

interface TreeProps {
  pid: string;
  rootName: string;
  expanded: Set<string>;
  selected: string | null;
  onToggle: (path: string) => void;
  onSelect: (path: string) => void;
  registerEntries: (dir: string, entries: ProjectFile[], complete: boolean) => void;
}

interface BranchProps extends TreeProps {
  path: string;
  depth: number;
}

export function FileTree(props: TreeProps) {
  return (
    <div className="files-tree-list" role="tree" aria-label="项目文件树">
      <DirBranch {...props} path="" depth={0} />
    </div>
  );
}

function DirBranch({
  pid,
  rootName,
  path,
  depth,
  expanded,
  selected,
  onToggle,
  onSelect,
  registerEntries,
}: BranchProps) {
  const listing = useDir(pid, path);
  const entries = listing.data?.entries;
  const sorted = useMemo(() => sortEntries(entries ?? []), [entries]);
  const [limit, setLimit] = useState(ENTRY_PAGE);

  useEffect(() => {
    if (entries) registerEntries(path, entries, !listing.data?.truncated);
  }, [entries, listing.data?.truncated, path, registerEntries]);

  const isRoot = path === "";
  const rowDepth = isRoot ? 1 : depth;

  if (listing.isLoading) {
    return (
      <div className="files-skeleton" aria-label="加载中">
        {SKELETON_WIDTHS.map((width, index) => (
          <div
            key={index}
            className="files-skeleton-row skeleton"
            style={{ width, marginLeft: 10 + rowDepth * 13 }}
          />
        ))}
      </div>
    );
  }

  if (listing.isError) {
    return (
      <div className="files-tree-error" style={{ marginLeft: isRoot ? 0 : 8 + depth * 13 }}>
        <AlertTriangle size={13} />
        <div className="files-tree-error-text">
          <div>{isRoot ? "无法读取项目根目录" : "无法读取此目录"}</div>
          <div className="files-tree-error-detail">{filesErrorText(listing.error)}</div>
        </div>
        <Button size="sm" variant="ghost" onClick={() => void listing.refetch()}>
          <RefreshCw size={12} /> 重试
        </Button>
      </div>
    );
  }

  const visible = sorted.slice(0, limit);
  const remaining = sorted.length - visible.length;

  return (
    <div role="group">
      {isRoot ? (
        <div className="files-root-row" title={listing.data?.root}>
          <FolderOpen size={14} className="fg fg-dir" />
          <span className="files-root-name">{rootName || "项目根目录"}</span>
          <Badge tone="outline">根目录</Badge>
        </div>
      ) : null}

      {!sorted.length ? (
        <div className="files-tree-note is-empty" style={{ paddingLeft: 10 + rowDepth * 13 }}>
          {isRoot ? "项目目录为空" : "空目录"}
        </div>
      ) : null}

      {visible.map((entry) => {
        const isExpanded = expanded.has(entry.path);
        return (
          <div key={entry.path} role="none">
            <button
              type="button"
              role="treeitem"
              aria-level={rowDepth + 1}
              aria-expanded={entry.dir ? isExpanded : undefined}
              data-tree-path={entry.path}
              className={clsx(
                "files-row",
                entry.dir && "is-dir",
                !entry.dir && selected === entry.path && "is-selected",
              )}
              style={{ paddingLeft: 6 + rowDepth * 13 }}
              title={entry.dir ? entry.path : `${entry.path}\n${fmtBytes(entry.size)}${
                fmtMtime(entry.mtime) ? ` · ${fmtMtime(entry.mtime)}` : ""
              }`}
              onClick={() => {
                if (entry.dir) onToggle(entry.path);
                else onSelect(entry.path);
              }}
            >
              <span className="files-row-chevron">
                {entry.dir ? (
                  isExpanded ? (
                    <ChevronDown size={12} />
                  ) : (
                    <ChevronRight size={12} />
                  )
                ) : null}
              </span>
              <FileGlyph name={entry.name} dir={entry.dir} expanded={isExpanded} />
              <span className="files-row-name">{entry.name}</span>
              <span className="files-row-meta">
                {entry.dir ? "" : fmtBytes(entry.size)}
              </span>
            </button>
            {entry.dir && isExpanded ? (
              <DirBranch
                pid={pid}
                rootName={rootName}
                path={entry.path}
                depth={depth + 1}
                expanded={expanded}
                selected={selected}
                onToggle={onToggle}
                onSelect={onSelect}
                registerEntries={registerEntries}
              />
            ) : null}
          </div>
        );
      })}

      {remaining > 0 ? (
        <button
          type="button"
          className="files-more"
          style={{ marginLeft: 10 + rowDepth * 13 }}
          onClick={() => setLimit((current) => current + ENTRY_PAGE)}
        >
          显示更多 · 还有 {remaining} 项
        </button>
      ) : null}

      {listing.data?.truncated ? (
        <div className="files-tree-note" style={{ paddingLeft: 10 + rowDepth * 13 }}>
          目录条目过多，服务端仅返回了部分结果
        </div>
      ) : null}
    </div>
  );
}
