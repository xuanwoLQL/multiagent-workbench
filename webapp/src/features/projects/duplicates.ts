/**
 * 历史重复项目的纯函数工具（不依赖 React / 网络）。
 *
 * 后端 ``normalize_root`` 会解析真实路径并做 normcase；前端拿不到解析能力，
 * 只能按同样的文本规则归一：反斜杠转正斜杠、合并重复分隔符、去掉尾部分隔符、
 * Windows 语义下统一小写。空路径返回空字符串，避免把脏数据归到同一组。
 */

export interface DuplicateProjectLike {
  id: string;
  name: string;
  label?: string | null;
  root_path: string;
}

export interface DuplicateGroup<T extends DuplicateProjectLike = DuplicateProjectLike> {
  /** 规范化后的目录键，用于分组与展示「规范化后」。 */
  key: string;
  /** 组内第一个项目的原始登记路径，用于展示「规范化前」。 */
  rootPath: string;
  /** 组内项目，保持输入（后端按创建时间返回）顺序。 */
  projects: T[];
}

/**
 * 把项目根路径规范化为可比较的键，语义对齐后端 ``normalize_root``：
 * - 去掉首尾空白；
 * - 反斜杠统一成正斜杠，合并重复分隔符；
 * - UNC 路径保留开头的双斜杠；
 * - 去掉尾部分隔符，统一小写；
 * - 空值返回 ``""``。
 */
export function normalizeRootKey(path: string): string {
  const trimmed = String(path ?? "").trim().replace(/\\/g, "/");
  if (!trimmed) return "";

  const isUnc = trimmed.startsWith("//");
  const body = isUnc ? trimmed.slice(2) : trimmed;
  const collapsed = body.replace(/\/+/g, "/");
  const withRoot = isUnc ? `//${collapsed}` : collapsed;
  return withRoot.replace(/\/+$/, "").toLowerCase();
}

/**
 * 按规范化根路径分组，只返回成员数 >= 2 的组。
 *
 * 组顺序与组内成员顺序都沿用输入顺序；空路径不参与分组。
 */
export function groupDuplicateProjects<T extends DuplicateProjectLike>(
  projects: readonly T[],
): DuplicateGroup<T>[] {
  const groups = new Map<string, DuplicateGroup<T>>();

  for (const project of projects) {
    const key = normalizeRootKey(project.root_path);
    if (!key) continue;

    const existing = groups.get(key);
    if (existing) {
      existing.projects.push(project);
      continue;
    }
    groups.set(key, {
      key,
      rootPath: String(project.root_path ?? "").trim(),
      projects: [project],
    });
  }

  return Array.from(groups.values()).filter((group) => group.projects.length >= 2);
}
