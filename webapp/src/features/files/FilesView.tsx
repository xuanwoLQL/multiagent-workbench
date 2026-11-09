import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import {
  ChevronRight,
  ChevronsUpDown,
  FolderOpen,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import type { ProjectFile } from "../../api/types";
import { useDashboard } from "../../app/queries";
import { useUi } from "../../app/store";
import { Button, IconButton } from "../../ui/primitives";
import { useToast } from "../../ui/toast";
import { basename, dirOf, openExternal, splitPath } from "./util";
import { useDebounced, useDir } from "./useFileData";
import { FileSearchPanel } from "./FileSearch";
import { FileTree } from "./FileTree";
import { FileViewer } from "./FileViewer";
import "./files.css";

const MIN_TREE = 220;
const MAX_TREE = 640;
const DEFAULT_TREE = 300;

export function FilesView() {
  const projectId = useUi((state) => state.projectId);
  const dashboard = useDashboard(projectId);
  const client = useQueryClient();
  const toast = useToast();
  const rootListing = useDir(projectId, "");

  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [highlightLine, setHighlightLine] = useState<number | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [known, setKnown] = useState<Map<string, ProjectFile>>(() => new Map());
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebounced(search, 240);
  const [treeWidth, setTreeWidth] = useState(DEFAULT_TREE);
  const [dragging, setDragging] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const revealTimer = useRef<number | undefined>(undefined);

  const projectName = dashboard.data?.project.name ?? "";
  const rootPath = rootListing.data?.root ?? dashboard.data?.project.root_path ?? "";
  const rootName = projectName || (rootPath ? basename(rootPath) : "项目");
  const activeSearch = debouncedSearch.trim();
  const searchMode = search.trim().length > 0;
  const selectedMeta = selectedPath ? known.get(selectedPath) : undefined;

  useEffect(() => {
    setSelectedPath(null);
    setHighlightLine(null);
    setExpanded(new Set());
    setKnown(new Map());
    setSearch("");
  }, [projectId]);

  useEffect(() => () => window.clearTimeout(revealTimer.current), []);

  const registerEntries = useCallback(
    (_dir: string, entries: ProjectFile[], complete: boolean) => {
      setKnown((prev) => {
        let changed = false;
        const next = new Map(prev);
        if (complete) {
          const present = new Set(entries.map((entry) => entry.path));
          for (const key of next.keys()) {
            if (dirOf(key) === _dir && !present.has(key)) {
              next.delete(key);
              changed = true;
            }
          }
        }
        for (const entry of entries) {
          const old = next.get(entry.path);
          if (
            !old ||
            old.name !== entry.name ||
            old.dir !== entry.dir ||
            old.size !== entry.size ||
            old.mtime !== entry.mtime
          ) {
            next.set(entry.path, entry);
            changed = true;
          }
        }
        return changed ? next : prev;
      });
    },
    [],
  );

  const scrollToTreePath = useCallback((path: string) => {
    window.clearTimeout(revealTimer.current);
    revealTimer.current = window.setTimeout(() => {
      const rows = Array.from(
        document.querySelectorAll<HTMLElement>("[data-tree-path]"),
      );
      const target = rows.find((row) => row.dataset.treePath === path);
      target?.scrollIntoView({ block: "nearest" });
    }, 260);
  }, []);

  const revealDir = useCallback(
    (path: string) => {
      if (!path) {
        scrollToTreePath("");
        return;
      }
      const { sep, parts } = splitPath(path);
      setExpanded((prev) => {
        const next = new Set(prev);
        let current = "";
        for (const part of parts) {
          current = current ? `${current}${sep}${part}` : part;
          next.add(current);
        }
        return next;
      });
      scrollToTreePath(path);
    },
    [scrollToTreePath],
  );

  const selectFile = useCallback((path: string, line?: number) => {
    setSelectedPath(path);
    setHighlightLine(line ?? null);
    const { sep, parts } = splitPath(path);
    parts.pop();
    setExpanded((prev) => {
      const next = new Set(prev);
      let current = "";
      for (const part of parts) {
        current = current ? `${current}${sep}${part}` : part;
        next.add(current);
      }
      return next;
    });
  }, []);

  const toggleDir = useCallback((path: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  const refresh = useCallback(async () => {
    if (!projectId) return;
    setRefreshing(true);
    try {
      await Promise.all([
        client.invalidateQueries({ queryKey: ["files", projectId] }),
        client.invalidateQueries({ queryKey: ["file", projectId] }),
      ]);
    } finally {
      setRefreshing(false);
    }
  }, [client, projectId]);

  const openRoot = useCallback(async () => {
    if (!rootPath) return;
    const error = await openExternal(rootPath);
    if (error) toast.error("无法打开项目目录", error);
  }, [rootPath, toast]);

  const onSplitterPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = treeWidth;
    setDragging(true);
    const onMove = (moveEvent: PointerEvent) => {
      const next = Math.min(
        MAX_TREE,
        Math.max(MIN_TREE, startWidth + moveEvent.clientX - startX),
      );
      setTreeWidth(next);
    };
    const onUp = () => {
      setDragging(false);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  const onSplitterKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const delta = event.key === "ArrowLeft" ? -24 : 24;
    setTreeWidth((width) => Math.min(MAX_TREE, Math.max(MIN_TREE, width + delta)));
  };

  const shellStyle = useMemo(
    () => ({ "--files-tree-w": `${treeWidth}px` }) as CSSProperties,
    [treeWidth],
  );

  const canOpenExternally = Boolean(
    (window as unknown as { multiagentDesktop?: { openPath?: unknown } }).multiagentDesktop
      ?.openPath,
  );

  return (
    <div className="page files-page">
      <header className="page-head files-page-head">
        <div className="page-head-text">
          <div className="page-title">
            <h1>文件</h1>
            <span className="files-readonly-badge">只读</span>
          </div>
          <div className="page-subtitle">
            浏览 {rootName} 的项目目录、搜索文件内容并预览文本。此视图不会修改任何文件。
          </div>
        </div>
        <div className="page-actions">
          {canOpenExternally && rootPath ? (
            <Button onClick={() => void openRoot()}>
              <FolderOpen size={14} /> 打开项目目录
            </Button>
          ) : null}
          <Button onClick={() => void refresh()} loading={refreshing}>
            <RefreshCw size={14} /> 刷新
          </Button>
        </div>
      </header>

      <div
        className={clsx("files-shell", dragging && "is-dragging")}
        style={shellStyle}
      >
        <aside className="files-tree" aria-label="项目文件">
          <div className="files-tree-toolbar">
            <div className="files-searchbox">
              <Search size={13} className="files-searchbox-icon" />
              <input
                className="input files-searchbox-input"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Escape") setSearch("");
                }}
                placeholder="搜索文件名或内容…"
                aria-label="搜索文件"
                spellCheck={false}
              />
              {search ? (
                <button
                  type="button"
                  className="files-searchbox-clear"
                  aria-label="清除搜索"
                  title="清除搜索"
                  onClick={() => setSearch("")}
                >
                  <X size={12} />
                </button>
              ) : null}
            </div>
            <IconButton
              label="折叠全部目录"
              disabled={!expanded.size}
              onClick={() => setExpanded(new Set())}
            >
              <ChevronsUpDown size={14} />
            </IconButton>
            <IconButton
              label="刷新目录"
              disabled={refreshing}
              onClick={() => void refresh()}
            >
              <RefreshCw size={14} className={refreshing ? "spin" : undefined} />
            </IconButton>
          </div>

          <Breadcrumb
            path={selectedPath}
            rootPath={rootPath}
            rootName={rootName}
            onReveal={revealDir}
          />

          <div className="files-tree-body">
            {searchMode ? (
              <FileSearchPanel
                pid={projectId ?? ""}
                query={activeSearch || search.trim()}
                known={known}
                onOpenFile={selectFile}
                onOpenDir={(path) => {
                  revealDir(path);
                  setSearch("");
                }}
              />
            ) : (
              <FileTree
                pid={projectId ?? ""}
                rootName={rootName}
                expanded={expanded}
                selected={selectedPath}
                onToggle={toggleDir}
                onSelect={selectFile}
                registerEntries={registerEntries}
              />
            )}
          </div>

          <div className="files-tree-foot">
            <span className="files-tree-count">{known.size} 项已加载</span>
            <span className="files-tree-hint">只读 · 本地工作区</span>
          </div>
        </aside>

        <div
          className="files-splitter"
          role="separator"
          aria-orientation="vertical"
          aria-label="调整文件树宽度"
          aria-valuemin={MIN_TREE}
          aria-valuemax={MAX_TREE}
          aria-valuenow={treeWidth}
          tabIndex={0}
          onPointerDown={onSplitterPointerDown}
          onKeyDown={onSplitterKeyDown}
        >
          <span className="files-splitter-grip" />
        </div>

        <section className="files-viewer" aria-label="文件内容">
          <FileViewer
            pid={projectId}
            path={selectedPath}
            root={rootPath}
            meta={selectedMeta}
            highlightLine={highlightLine}
            onClose={() => {
              setSelectedPath(null);
              setHighlightLine(null);
            }}
          />
        </section>
      </div>
    </div>
  );
}

function Breadcrumb({
  path,
  rootPath,
  rootName,
  onReveal,
}: {
  path: string | null;
  rootPath: string;
  rootName: string;
  onReveal: (path: string) => void;
}) {
  const { sep, parts } = path ? splitPath(path) : { sep: "/", parts: [] as string[] };
  const segments = parts.map((name, index) => ({
    name,
    path: parts.slice(0, index + 1).join(sep),
    last: index === parts.length - 1,
  }));

  return (
    <nav className="files-crumbs" aria-label="当前文件路径">
      <button
        type="button"
        className="files-crumb"
        title={rootPath || rootName}
        onClick={() => onReveal("")}
      >
        <FolderOpen size={12} />
        <span>{rootName}</span>
      </button>
      {segments.map((segment) => (
        <Fragment key={segment.path}>
          <ChevronRight size={11} className="files-crumb-sep" />
          {segment.last ? (
            <span className="files-crumb is-current" title={segment.path}>
              {segment.name}
            </span>
          ) : (
            <button
              type="button"
              className="files-crumb"
              title={segment.path}
              onClick={() => onReveal(segment.path)}
            >
              {segment.name}
            </button>
          )}
        </Fragment>
      ))}
    </nav>
  );
}
