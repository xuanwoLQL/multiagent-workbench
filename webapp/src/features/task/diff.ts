import type { DiffHunk as ApiDiffHunk } from "../../api/types";

export interface DiffLine {
  type: "context" | "add" | "del" | "meta";
  oldNo: number | null;
  newNo: number | null;
  text: string;
}

export interface ParsedHunk {
  header: string;
  oldStart: number;
  newStart: number;
  lines: DiffLine[];
  additions: number;
  deletions: number;
}

export interface ParsedDiff {
  hunks: ParsedHunk[];
  additions: number;
  deletions: number;
  hadHeaders: boolean;
}

const HUNK_RE = /^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@(.*)$/;

export function parseUnifiedDiff(diff: string | null | undefined): ParsedDiff {
  const lines = String(diff ?? "").split(/\r?\n/);
  const hunks: ParsedHunk[] = [];
  let current: ParsedHunk | null = null;
  let oldNo = 0;
  let newNo = 0;
  let hadHeaders = false;

  for (const raw of lines) {
    const match = HUNK_RE.exec(raw);
    if (match) {
      current = {
        header: raw,
        oldStart: Number(match[1]),
        newStart: Number(match[3]),
        lines: [],
        additions: 0,
        deletions: 0,
      };
      oldNo = current.oldStart;
      newNo = current.newStart;
      hunks.push(current);
      continue;
    }
    if (!current) {
      if (raw.startsWith("--- ") || raw.startsWith("+++ ") || raw.startsWith("diff ")) {
        hadHeaders = true;
      }
      continue;
    }
    if (raw === "\\ No newline at end of file") {
      current.lines.push({ type: "meta", oldNo: null, newNo: null, text: raw });
      continue;
    }
    if (raw.startsWith("+")) {
      current.lines.push({ type: "add", oldNo: null, newNo, text: raw.slice(1) });
      current.additions += 1;
      newNo += 1;
      continue;
    }
    if (raw.startsWith("-")) {
      current.lines.push({ type: "del", oldNo, newNo: null, text: raw.slice(1) });
      current.deletions += 1;
      oldNo += 1;
      continue;
    }
    if (raw.startsWith(" ")) {
      current.lines.push({ type: "context", oldNo, newNo, text: raw.slice(1) });
      oldNo += 1;
      newNo += 1;
      continue;
    }
    if (raw === "") {
      current.lines.push({ type: "context", oldNo, newNo, text: "" });
      oldNo += 1;
      newNo += 1;
    }
  }

  return {
    hunks,
    additions: hunks.reduce((sum, hunk) => sum + hunk.additions, 0),
    deletions: hunks.reduce((sum, hunk) => sum + hunk.deletions, 0),
    hadHeaders,
  };
}

export function fromApiHunks(hunks: ApiDiffHunk[] | null | undefined): ParsedDiff {
  const list = Array.isArray(hunks) ? hunks : [];
  const parsed: ParsedHunk[] = list.map((hunk) => {
    const lines: DiffLine[] = (hunk.lines ?? []).map((line) => ({
      type: line.type,
      oldNo: line.old_no ?? null,
      newNo: line.new_no ?? null,
      text: line.text ?? "",
    }));
    return {
      header:
        hunk.header ||
        `@@ -${hunk.old_start},${hunk.old_lines} +${hunk.new_start},${hunk.new_lines} @@`,
      oldStart: hunk.old_start,
      newStart: hunk.new_start,
      lines,
      additions: lines.filter((line) => line.type === "add").length,
      deletions: lines.filter((line) => line.type === "del").length,
    };
  });
  return {
    hunks: parsed,
    additions: parsed.reduce((sum, hunk) => sum + hunk.additions, 0),
    deletions: parsed.reduce((sum, hunk) => sum + hunk.deletions, 0),
    hadHeaders: parsed.length > 0,
  };
}
