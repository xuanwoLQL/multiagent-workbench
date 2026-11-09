import { useEffect, useMemo, useRef, useState } from "react";
import clsx from "clsx";
import {
  AlertTriangle,
  Copy,
  ExternalLink,
  FileSearch,
  RefreshCw,
  WrapText,
  X,
} from "lucide-react";
import type { ProjectFile } from "../../api/types";
import { fmtBytes } from "../../lib/format";
import { Badge, Button, EmptyState, IconButton } from "../../ui/primitives";
import { useToast } from "../../ui/toast";
import { FileGlyph } from "./FileGlyph";
import { filesErrorText, isMissingRoute, useFileContent } from "./useFileData";
import {
  basename,
  copyText,
  fmtMtime,
  getDesktop,
  isAbsolutePath,
  joinRoot,
  openExternal,
} from "./util";

const INITIAL_LINES = 2000;
const LINE_STEP = 4000;
const MAX_PREVIEW_CHARS = 1_500_000;

export function FileViewer({
  pid,
  path,
  root,
  meta,
  highlightLine,
  onClose,
}: {
  pid: string | null;
  path: string | null;
  root?: string;
  meta?: ProjectFile;
  highlightLine?: number | null;
  onClose: () => void;
}) {
  const toast = useToast();
  const query = useFileContent(pid, path);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const [wrap, setWrap] = useState(false);
  const [shown, setShown] = useState(INITIAL_LINES);
  const data = query.data;
  const absolute = path ? joinRoot(root, path) : "";
  const canOpen = Boolean(getDesktop()?.openPath) && isAbsolutePath(absolute);

  useEffect(() => {
    setShown(INITIAL_LINES);
  }, [path]);

  useEffect(() => {
    bodyRef.current?.scrollTo({ top: 0 });
  }, [path]);

  const raw = data?.content ?? "";
  const clipped = raw.length > MAX_PREVIEW_CHARS;
  const text = clipped ? raw.slice(0, MAX_PREVIEW_CHARS) : raw;
  const lines = useMemo(() => text.split(/\r?\n/), [text]);
  const shownLines = Math.min(shown, lines.length);
  const digits = String(Math.max(lines.length, 1)).length;

  useEffect(() => {
    if (!highlightLine || highlightLine <= shown) return;
    setShown(highlightLine + 200);
  }, [highlightLine, shown]);

  useEffect(() => {
    if (!highlightLine) return;
    const timer = window.setTimeout(() => {
      const node = bodyRef.current?.querySelector(
        `.fv-line[data-line="${highlightLine}"]`,
      );
      node?.scrollIntoView({ block: "center" });
    }, 80);
    return () => window.clearTimeout(timer);
  }, [highlightLine, path, data?.content, shownLines]);

  const handleCopyPath = async () => {
    const target = absolute || path || "";
    if (!target) return;
    const ok = await copyText(target);
    if (ok) toast.success("已复制路径", target);
    else toast.error("复制失败", "剪贴板不可用");
  };

  const handleCopyContent = async () => {
    const ok = await copyText(data?.content ?? "");
    if (ok) toast.success("已复制文件内容");
    else toast.error("复制失败", "剪贴板不可用");
  };

  const handleOpen = async () => {
    if (!absolute) return;
    const error = await openExternal(absolute);
    if (error) toast.error("无法打开文件", error);
  };

  if (!path) {
    return (
      <div className="fv fv-idle">
        <EmptyState
          icon={<FileSearch size={18} />}
          title="选择一个文件查看内容"
          hint="左侧按目录浏览，或在上方搜索框按文件名 / 内容查找。此视图只读，不会修改项目文件。"
        />
      </div>
    );
  }

  return (
    <div className="fv">
      <div className="fv-head">
        <div className="fv-head-top">
          <FileGlyph name={path} size={15} />
          <span className="fv-name" title={path}>
            {basename(path)}
          </span>
          <Badge tone="outline">只读</Badge>
          {data?.binary ? <Badge tone="warning">二进制</Badge> : null}
          <div className="fv-actions">
            {data && !data.binary ? (
              <IconButton
                label={wrap ? "关闭自动换行" : "开启自动换行"}
                className={clsx("fv-toggle", wrap && "is-on")}
                aria-pressed={wrap}
                onClick={() => setWrap((value) => !value)}
              >
                <WrapText size={14} />
              </IconButton>
            ) : null}
            {data && !data.binary && data.content ? (
              <IconButton label="复制文件内容" onClick={() => void handleCopyContent()}>
                <Copy size={14} />
              </IconButton>
            ) : null}
            <IconButton label="复制路径" onClick={() => void handleCopyPath()}>
              <Copy size={14} />
            </IconButton>
            {canOpen ? (
              <IconButton label="在外部打开" onClick={() => void handleOpen()}>
                <ExternalLink size={14} />
              </IconButton>
            ) : null}
            <IconButton
              label="重新加载"
              onClick={() => void query.refetch()}
              disabled={query.isFetching}
            >
              <RefreshCw size={14} className={query.isFetching ? "spin" : undefined} />
            </IconButton>
            <IconButton label="关闭预览" onClick={onClose}>
              <X size={14} />
            </IconButton>
          </div>
        </div>
        <div className="fv-sub">
          <span className="fv-path" title={absolute || path}>
            {path}
          </span>
          {data ? <span className="fv-dot">·</span> : null}
          {data ? <span>{fmtBytes(data.size)}</span> : null}
          {meta && fmtMtime(meta.mtime) ? (
            <>
              <span className="fv-dot">·</span>
              <span>{fmtMtime(meta.mtime)}</span>
            </>
          ) : null}
        </div>
      </div>

      {data?.truncated ? (
        <div className="fv-banner">
          <AlertTriangle size={13} />
          <span>
            文件较大，服务端仅返回了部分内容（共 {fmtBytes(data.size)}）。完整内容请复制路径后在外部打开。
          </span>
        </div>
      ) : null}

      {query.isLoading ? (
        <div className="fv-loading" aria-label="正在加载文件">
          <div className="fv-loading-head">
            <span className="spinner" />
            <span>正在读取 {basename(path)}…</span>
          </div>
          {[68, 52, 84, 40, 61, 74, 48, 66].map((width, index) => (
            <div
              key={index}
              className="skeleton"
              style={{ height: 12, width: `${width}%`, marginLeft: 18 }}
            />
          ))}
        </div>
      ) : query.isError ? (
        <div className="fv-state">
          <EmptyState
            icon={<AlertTriangle size={18} />}
            title="无法读取文件"
            hint={
              isMissingRoute(query.error)
                ? `文件预览接口尚未启用或文件不存在（404）。${filesErrorText(query.error)}`
                : filesErrorText(query.error)
            }
            action={
              <Button size="sm" onClick={() => void query.refetch()}>
                <RefreshCw size={13} /> 重试
              </Button>
            }
          />
        </div>
      ) : data?.binary ? (
        <div className="fv-state">
          <EmptyState
            icon={<FileSearch size={18} />}
            title="二进制文件，无法以文本预览"
            hint={`${basename(path)} · ${fmtBytes(data.size)}。可以在系统默认应用中打开。`}
            action={
              <div className="fv-state-actions">
                {canOpen ? (
                  <Button size="sm" onClick={() => void handleOpen()}>
                    <ExternalLink size={13} /> 外部打开
                  </Button>
                ) : null}
                <Button size="sm" variant="ghost" onClick={() => void handleCopyPath()}>
                  <Copy size={13} /> 复制路径
                </Button>
              </div>
            }
          />
        </div>
      ) : !data?.content && data && data.size > 0 ? (
        <div className="fv-state">
          <EmptyState
            icon={<AlertTriangle size={18} />}
            title="未返回文本内容"
            hint={`文件大小 ${fmtBytes(data.size)}，可能超出预览上限或不是纯文本。可在外部打开查看。`}
            action={
              canOpen ? (
                <Button size="sm" onClick={() => void handleOpen()}>
                  <ExternalLink size={13} /> 外部打开
                </Button>
              ) : undefined
            }
          />
        </div>
      ) : text === "" ? (
        <div className="fv-state">
          <EmptyState icon={<FileSearch size={18} />} title="空文件" hint="这个文件没有任何内容。" />
        </div>
      ) : (
        <div className="fv-body" ref={bodyRef}>
          <div
            className={clsx("fv-code", wrap && "is-wrap")}
            style={{ ["--fv-ln-w" as string]: `${digits + 2}ch` }}
          >
            {lines.slice(0, shownLines).map((line, index) => {
              const number = index + 1;
              return (
                <div
                  key={number}
                  className={clsx(
                    "fv-line",
                    highlightLine === number && "is-focus",
                  )}
                  data-line={number}
                >
                  <span className="fv-ln">{number}</span>
                  <span className="fv-text">{line === "" ? " " : line}</span>
                </div>
              );
            })}
          </div>
          {lines.length > shownLines ? (
            <div className="fv-more">
              <Button size="sm" variant="ghost" onClick={() => setShown((value) => value + LINE_STEP)}>
                显示更多 · 还有 {lines.length - shownLines} 行
              </Button>
            </div>
          ) : null}
          {clipped ? (
            <div className="fv-banner is-inline">
              <AlertTriangle size={13} />
              <span>预览已在前 {MAX_PREVIEW_CHARS.toLocaleString("zh-CN")} 个字符处截断。</span>
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}
