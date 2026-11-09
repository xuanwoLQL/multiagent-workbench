import { useEffect, useMemo, useState } from "react";
import clsx from "clsx";
import { AlertTriangle, SearchX } from "lucide-react";
import type { ProjectFile } from "../../api/types";
import { Spinner } from "../../ui/primitives";
import { FileGlyph } from "./FileGlyph";
import { filesErrorText, isMissingRoute, useFileSearch, type SearchMatch } from "./useFileData";
import { basename, dirOf, splitPath } from "./util";

const RESULT_PAGE = 150;

interface Hit {
  key: string;
  path: string;
  line?: number;
  text?: string;
  dir?: boolean;
}

export function FileSearchPanel({
  pid,
  query,
  known,
  onOpenFile,
  onOpenDir,
}: {
  pid: string;
  query: string;
  known: Map<string, ProjectFile>;
  onOpenFile: (path: string, line?: number) => void;
  onOpenDir: (path: string) => void;
}) {
  const search = useFileSearch(pid, query);
  const [limit, setLimit] = useState(RESULT_PAGE);
  const q = query.trim().toLowerCase();
  const routeMissing = isMissingRoute(search.error);

  useEffect(() => {
    setLimit(RESULT_PAGE);
  }, [query]);

  const fallback = useMemo(() => {
    if (!q) return [] as ProjectFile[];
    const matches: ProjectFile[] = [];
    for (const entry of known.values()) {
      if (entry.path.toLowerCase().includes(q) || entry.name.toLowerCase().includes(q)) {
        matches.push(entry);
      }
    }
    matches.sort((a, b) => {
      if (a.dir !== b.dir) return a.dir ? -1 : 1;
      return a.path.localeCompare(b.path, "zh-CN", { numeric: true });
    });
    return matches.slice(0, 1000);
  }, [known, q]);

  const serverMatches: SearchMatch[] | null = search.data ? (search.data.matches ?? []) : null;
  const serverHits: Hit[] = (serverMatches ?? []).map((match, index) => ({
    key: `s-${index}-${match.path}-${match.line ?? 0}`,
    path: match.path,
    line: match.line,
    text: match.text,
  }));
  const fallbackHits: Hit[] = fallback.map((entry) => ({
    key: `f-${entry.path}`,
    path: entry.path,
    dir: entry.dir,
  }));

  const usingFallback = serverMatches === null;
  const hits = usingFallback ? fallbackHits : serverHits;
  const visible = hits.slice(0, limit);
  const remaining = hits.length - visible.length;
  const searching = search.isFetching;
  const tooShort = query.trim().length < 2;

  return (
    <div className="files-search-panel">
      <div className="files-search-status">
        {searching ? <Spinner size={12} /> : null}
        {usingFallback ? (
          <span>
            {tooShort
              ? "输入至少 2 个字符可搜索文件内容；当前仅按名称过滤已加载条目。"
              : routeMissing
                ? `文件搜索接口未就绪，仅过滤已加载的 ${known.size} 个条目。展开更多目录后再试。`
                : search.isError
                  ? `搜索失败：${filesErrorText(search.error)}（以下为已加载条目过滤）`
                  : `正在准备内容搜索…（已加载 ${known.size} 个条目）`}
          </span>
        ) : (
          <span>
            {serverHits.length} 处匹配
            {search.data?.truncated ? " · 结果已截断" : ""}
          </span>
        )}
      </div>

      {searching && usingFallback ? (
        <div className="files-search-loading skeleton" style={{ width: "68%" }} />
      ) : null}

      {!visible.length ? (
        <div className="files-search-empty">
          {search.isError && !routeMissing ? (
            <AlertTriangle size={15} />
          ) : (
            <SearchX size={15} />
          )}
          <div className="files-search-empty-title">
            {searching ? "搜索中…" : `没有匹配 “${query.trim()}”`}
          </div>
          {usingFallback && !tooShort ? (
            <div className="files-search-empty-hint">
              内容搜索需要后端支持；可继续展开目录后按名称过滤。
            </div>
          ) : null}
        </div>
      ) : (
        <div className="files-hits">
          {visible.map((hit) =>
            hit.dir ? (
              <button
                key={hit.key}
                type="button"
                className="files-hit"
                title={hit.path}
                onClick={() => onOpenDir(hit.path)}
              >
                <FileGlyph name={hit.path} dir size={14} />
                <span className="files-hit-main">
                  <span className="files-hit-path">
                    <PathText path={hit.path} />
                  </span>
                  <span className="files-hit-text">目录</span>
                </span>
              </button>
            ) : (
              <button
                key={hit.key}
                type="button"
                className={clsx("files-hit", hit.text && "has-text")}
                title={hit.path}
                onClick={() => onOpenFile(hit.path, hit.line)}
              >
                <FileGlyph name={hit.path} size={14} />
                <span className="files-hit-main">
                  <span className="files-hit-path">
                    <PathText path={hit.path} />
                  </span>
                  {hit.text ? (
                    <span className="files-hit-text">
                      {hit.line ? `${hit.line}: ` : ""}
                      {hit.text.trim().slice(0, 240)}
                    </span>
                  ) : null}
                </span>
                {hit.line ? <span className="files-hit-line">L{hit.line}</span> : null}
              </button>
            ),
          )}
        </div>
      )}

      {remaining > 0 ? (
        <button
          type="button"
          className="files-more"
          onClick={() => setLimit((current) => current + RESULT_PAGE)}
        >
          显示更多 · 还有 {remaining} 项
        </button>
      ) : null}
    </div>
  );
}

function PathText({ path }: { path: string }) {
  const { sep } = splitPath(path);
  const directory = dirOf(path);
  const name = basename(path);
  return (
    <>
      {directory ? <span className="files-hit-dir">{directory}{sep}</span> : null}
      <span className="files-hit-name">{name}</span>
    </>
  );
}
