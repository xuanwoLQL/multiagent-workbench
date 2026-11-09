import clsx from "clsx";
import {
  Database,
  File,
  FileArchive,
  FileCode2,
  FileCog,
  FileImage,
  FileJson,
  FileSpreadsheet,
  FileText,
  FileType,
  Folder,
  FolderOpen,
  Lock,
  Music,
  Video,
} from "lucide-react";
import { extOf, kindOf, type FileKind } from "./util";

const DATA_EXT = new Set(["json", "jsonl", "yaml", "yml", "toml"]);
const SHEET_EXT = new Set(["csv", "tsv", "xls", "xlsx"]);
const DB_EXT = new Set(["sql", "db", "sqlite", "parquet"]);
const ARCHIVE_EXT = new Set(["zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "jar", "war"]);

function iconFor(name: string, kind: FileKind) {
  const ext = extOf(name);
  if (kind === "image") return FileImage;
  if (kind === "media") return ext === "mp4" || ext === "mov" || ext === "webm" || ext === "mkv" || ext === "avi" ? Video : Music;
  if (kind === "archive" || ARCHIVE_EXT.has(ext)) return FileArchive;
  if (SHEET_EXT.has(ext)) return FileSpreadsheet;
  if (DB_EXT.has(ext)) return Database;
  if (DATA_EXT.has(ext)) return FileJson;
  if (kind === "config") return FileCog;
  if (ext === "lock" || ext === "pem" || ext === "key") return Lock;
  if (kind === "code") return FileCode2;
  if (kind === "doc") return ext === "pdf" ? FileType : FileText;
  return File;
}

export function FileGlyph({
  name,
  dir,
  expanded,
  size = 14,
  className,
}: {
  name: string;
  dir?: boolean;
  expanded?: boolean;
  size?: number;
  className?: string;
}) {
  if (dir) {
    return expanded ? (
      <FolderOpen size={size} className={clsx("fg", "fg-dir", className)} />
    ) : (
      <Folder size={size} className={clsx("fg", "fg-dir", className)} />
    );
  }
  const kind = kindOf(name);
  const Icon = iconFor(name, kind);
  return <Icon size={size} className={clsx("fg", `fg-${kind}`, className)} />;
}
