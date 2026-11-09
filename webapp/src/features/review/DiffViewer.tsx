import clsx from "clsx";
import { FileQuestion } from "lucide-react";
import type { DiffPayload } from "../../api/types";
import { fmtBytes } from "../../lib/format";

type DiffLineKind = "add" | "del" | "context" | "hunk" | "meta";

export interface ParsedDiffLine {
  kind: DiffLineKind;
  text: string;
  oldNo?: number;
  newNo?: number;
}

export interface ParsedDiff {
  lines: ParsedDiffLine[];
  additions: number;
  deletions: number;
}

export function parseUnifiedDiff(raw: string): ParsedDiff {
  const input = raw.split(/\r?\n/);
  if (input[input.length - 1] === "") input.pop();

  const lines: ParsedDiffLine[] = [];
  let oldNo = 0;
  let newNo = 0;
  let additions = 0;
  let deletions = 0;

  for (const line of input) {
    if (line.startsWith("@@")) {
      const match = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
      if (match) {
        oldNo = Number(match[1]);
        newNo = Number(match[2]);
      }
      lines.push({ kind: "hunk", text: line });
      continue;
    }

    if (line.startsWith("\\")) {
      lines.push({ kind: "meta", text: line });
      continue;
    }

    if (
      line.startsWith("diff --git") ||
      line.startsWith("index ") ||
      line.startsWith("new file mode") ||
      line.startsWith("deleted file mode") ||
      line.startsWith("similarity index") ||
      line.startsWith("rename from") ||
      line.startsWith("rename to") ||
      line.startsWith("--- ") ||
      line.startsWith("+++ ")
    ) {
      lines.push({ kind: "meta", text: line });
      continue;
    }

    if (line.startsWith("+")) {
      additions += 1;
      lines.push({ kind: "add", text: line.slice(1), newNo });
      newNo += 1;
      continue;
    }

    if (line.startsWith("-")) {
      deletions += 1;
      lines.push({ kind: "del", text: line.slice(1), oldNo });
      oldNo += 1;
      continue;
    }

    if (line.startsWith(" ")) {
      lines.push({ kind: "context", text: line.slice(1), oldNo, newNo });
      oldNo += 1;
      newNo += 1;
      continue;
    }

    lines.push({ kind: "meta", text: line });
  }

  return { lines, additions, deletions };
}

export function DiffViewer({ payload }: { payload: DiffPayload }) {
  if (payload.binary) {
    return (
      <div className="review-binary">
        <FileQuestion size={30} />
        <div className="review-binary-title">二进制文件</div>
        <div className="review-binary-hint">
          {payload.note || "该文件无法以文本形式对比，请打开工作副本人工检查。"}
        </div>
        <div className="review-binary-size">
          {fmtBytes(payload.before_size)} → {fmtBytes(payload.after_size)}
        </div>
      </div>
    );
  }

  const parsed = parseUnifiedDiff(payload.diff ?? "");
  if (!parsed.lines.length) {
    return (
      <div className="review-binary">
        <FileQuestion size={28} />
        <div className="review-binary-title">没有可显示的差异</div>
        <div className="review-binary-hint">文件内容可能未变化，或差异已随同步写入工作区。</div>
      </div>
    );
  }

  return (
    <div className="review-diff-scroll" role="region" aria-label={`${payload.path} 的差异`}>
      <div className="review-diff-table">
        {parsed.lines.map((line, index) => {
          if (line.kind === "hunk" || line.kind === "meta") {
            return (
              <div key={index} className={clsx("diff-line", `is-${line.kind}`)}>
                {line.text || " "}
              </div>
            );
          }
          return (
            <div key={index} className={clsx("diff-line", `is-${line.kind}`)}>
              <span className="diff-no">{line.oldNo ?? ""}</span>
              <span className="diff-no">{line.newNo ?? ""}</span>
              <span className="diff-sign">
                {line.kind === "add" ? "+" : line.kind === "del" ? "-" : " "}
              </span>
              <span className="diff-text">{line.text || " "}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
