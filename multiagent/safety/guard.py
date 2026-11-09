"""危险操作拦截与人工确认队列（需求 23 / 44 / 55）。

两级防线：
1. **执行前**：Agent 通过 ACP 请求权限时，按策略放行 / 拒绝；命中危险模式则拒绝
   并生成待人工确认项（不静默卡住，需求 55）。
2. **执行后**：比对文件改动，任何落在保护路径、破坏性删除、越权写的动作都会
   被标记、拦截回收或要求人工确认。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from ..db import Database
from ..util import matches_any, new_id, now_iso, safe_join

DANGEROUS_PATTERNS = {
    "delete": re.compile(r"\b(rm|del|erase|rmdir|deltree|remove-item|shutil\.rmtree|os\.remove)\b", re.I),
    "modify_project_config": re.compile(r"(\.multiagent|config/|\.git/)", re.I),
    "escape_workspace": re.compile(r"(\.\./|\.\.\\|/etc/|C:\\Windows)", re.I),
    # 只把 shell 重定向当成覆盖信号；不能匹配 HTML/代码里的普通 ``>``。
    "overwrite_existing_artifact": re.compile(r"(?:^|[\s;&|])(?:>|>>)\s*[^\s|&]+", re.I),
}

#: 运行期命令审计：这些命令一旦出现在 Agent 的工具调用里，属于「立即终止」级别。
#: 它们会破坏工作区之外的东西（整盘格式化、删注册表、关机、清日志/卷影、远程脚本
#: 直接管道进 shell），仅在事后比对文件改动是拦不住的。
#: 命令起始边界。**必须**要求「行首 / 语句分隔符 / shell -c 前缀」，
#: 否则 ``atlas.shutdown()`` 这种正常方法调用会被误判成系统关机命令
#: （真实事故：Codex 的优雅关闭自测被当成关机，20 分钟成果被终止）。
_CMD_START = r"(?:^|[;&|\n]\s*|(?:cmd|powershell|pwsh)(?:\.exe)?\s+/c\s+|(?:sudo|doas)\s+)"

CRITICAL_COMMAND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("format_volume", re.compile(_CMD_START + r"format(?:\.com)?\s+[a-z]:", re.I | re.M)),
    ("diskpart", re.compile(_CMD_START + r"diskpart(?:\.exe)?\b", re.I | re.M)),
    ("boot_config", re.compile(_CMD_START + r"bcdedit(?:\.exe)?\b", re.I | re.M)),
    ("delete_hive", re.compile(_CMD_START + r"reg(?:\.exe)?\s+delete\s+(hklm|hku|hkey_local_machine|hkey_users)\b", re.I | re.M)),
    ("power_state", re.compile(
        _CMD_START + r"(?:shutdown(?:\.exe)?|stop-computer|restart-computer)(?=\s|$|[/-])",
        re.I | re.M,
    )),
    ("root_remove", re.compile(
        r"\b(remove-item|rm|del|erase|rd|rmdir)\b[^\n]{0,200}?\s(?:-[a-z]+\s+)*[\"']?"
        r"(?:[a-z]:[\\/]?|/)[\"']?(?=\s|$)",
        re.I,
    )),
    ("destroy_logs", re.compile(_CMD_START + r"(?:wevtutil\s+cl|vssadmin\s+delete\s+shadows|cipher\s+/w)\b", re.I | re.M)),
    ("pipe_to_shell", re.compile(r"\b(curl|wget|invoke-webrequest|irm)\b[^\n|]{0,400}\|\s*(sh|bash|zsh|pwsh|powershell)\b", re.I)),
    ("mkfs_or_dd", re.compile(r"\b(mkfs(?:\.\w+)?|dd)\b[^\n]{0,160}\bof\s*=\s*/dev/", re.I)),
)

#: 「工作区内破坏性操作」：不立即终止（可能是正常清理），但会生成人工确认项，
#: 并且由 runner 在提交前按策略拦截（需求 44：删除 / 覆盖既有产物需二次确认）。
DESTRUCTIVE_COMMAND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("delete", re.compile(r"\b(rm|del|erase|rd|rmdir|remove-item|shred|truncate)\b", re.I)),
    ("git_destructive", re.compile(r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f)", re.I)),
)


@dataclass
class DangerDecision:
    allowed: bool
    needs_approval: bool = False
    kind: str = "other"
    reason: str = ""
    patterns: list[str] = field(default_factory=list)


class GuardService:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config

    # ------------------------------------------------------------------ 审批
    def request(
        self,
        *,
        project_id: str,
        kind: str,
        title: str,
        detail: dict[str, Any] | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
        risk: str = "medium",
        board=None,
    ) -> dict[str, Any]:
        approval_id = new_id("apr")
        self.db.insert(
            "approvals",
            {
                "id": approval_id,
                "project_id": project_id,
                "task_id": task_id,
                "run_id": run_id,
                "agent_id": agent_id,
                "kind": kind,
                "title": title,
                "detail": _dumps(detail or {}),
                "risk": risk,
                "status": "pending",
                "created_at": now_iso(),
                "resolved_at": None,
                "resolved_by": None,
                "resolution": None,
            },
        )
        if board is not None:
            board.append_event(
                project_id=project_id,
                kind="approval.request",
                summary=title,
                actor=agent_id,
                task_id=task_id,
                run_id=run_id,
                data={"approval_id": approval_id, "kind": kind, "risk": risk},
            )
        return self.get(approval_id)

    def resolve(
        self,
        approval_id: str,
        *,
        approved: bool,
        by: str = "human",
        resolution: str | None = None,
        board=None,
    ) -> dict[str, Any]:
        approval = self.get(approval_id)
        status = "approved" if approved else "rejected"
        self.db.update(
            "approvals",
            {"id": approval_id},
            {"status": status, "resolved_at": now_iso(), "resolved_by": by, "resolution": resolution},
        )
        if board is not None:
            board.append_event(
                project_id=approval["project_id"],
                kind=f"approval.{status}",
                summary=f"{approval['title']} → {status}",
                actor=by,
                task_id=approval["task_id"],
                run_id=approval["run_id"],
                data={"approval_id": approval_id},
            )
        return self.get(approval_id)

    def get(self, approval_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        if not row:
            raise KeyError(f"审批不存在：{approval_id}")
        return _approval(row)

    def list(self, project_id: str | None = None, *, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.db.query(f"SELECT * FROM approvals {where} ORDER BY created_at DESC LIMIT ?", params)
        return [_approval(r) for r in rows]

    def pending_count(self, project_id: str | None = None) -> int:
        if project_id:
            row = self.db.query_one(
                "SELECT COUNT(*) AS c FROM approvals WHERE status = 'pending' AND project_id = ?", (project_id,)
            )
        else:
            row = self.db.query_one("SELECT COUNT(*) AS c FROM approvals WHERE status = 'pending'")
        return int(row["c"]) if row else 0

    def is_approved(self, project_id: str, task_id: str | None, kind: str) -> bool:
        row = self.db.query_one(
            "SELECT 1 FROM approvals WHERE project_id = ? AND kind = ? AND status = 'approved' AND (task_id = ? OR task_id IS NULL) LIMIT 1",
            (project_id, kind, task_id),
        )
        return bool(row)

    def resolve_stale(
        self,
        task_id: str,
        *,
        kinds: tuple[str, ...] = ("dangerous_op", "acceptance_failed", "conflict"),
        resolution: str = "任务重试/续跑：上一次尝试的审批自动作废",
        board=None,
    ) -> int:
        """把上一次尝试遗留的 pending 审批自动作废。

        Agent 失败/超时后，旧 run 的「越权」「验收失败」「冲突」审批会一直挂在队列里，
        重试成功后仍然显示为待处理（真实 UI 现场：一个任务重试 3 次后堆了 7 条）。
        这里在新一次尝试真正开始时，把这些**属于历史尝试**的审批收尾；人工确认类
        （``manual_confirm``）不在此列，避免把正在等待人工的任务误关掉。
        """
        if not kinds:
            return 0
        placeholders = ",".join("?" for _ in kinds)
        rows = self.db.query(
            f"SELECT id, project_id, run_id FROM approvals "
            f"WHERE task_id = ? AND status = 'pending' AND kind IN ({placeholders})",
            (task_id, *kinds),
        )
        if not rows:
            return 0
        now = now_iso()
        with self.db.transaction():
            for row in rows:
                self.db.update(
                    "approvals",
                    {"id": row["id"]},
                    {
                        "status": "rejected",
                        "resolved_at": now,
                        "resolved_by": "system",
                        "resolution": resolution,
                    },
                )
        if board is not None:
            board.append_event(
                project_id=rows[0]["project_id"],
                kind="approval.stale_resolved",
                summary=f"自动作废 {len(rows)} 条上次尝试遗留的审批",
                actor="system",
                task_id=task_id,
                data={"approval_ids": [row["id"] for row in rows]},
            )
        return len(rows)

    # ------------------------------------------------------------------ 判定
    def protected_paths(self) -> list[str]:
        return list(self.config.policy_get("dangerous_ops.protect_paths", []) or [])

    def classify_permission(self, kind: str, title: str, detail: str) -> DangerDecision:
        text = f"{title} {detail}"
        hits = [name for name, pattern in DANGEROUS_PATTERNS.items() if pattern.search(text)]
        protected = [p for p in self.protected_paths() if p and p.lower() in text.lower()]
        if protected:
            hits.append("protected_path")
        if hits:
            return DangerDecision(
                allowed=False,
                needs_approval=True,
                kind=kind,
                reason="命中危险操作模式：" + ", ".join(sorted(set(hits))),
                patterns=sorted(set(hits)),
            )
        return DangerDecision(allowed=True, kind=kind, reason="常规操作")

    def permission_policy(self, task: dict[str, Any]):
        """返回一个 ``approve(PermissionRequest) -> bool`` 回调。"""
        permission = "readonly" if task.get("read_only") else "guarded"
        require_confirmation = set(self.config.policy_get("dangerous_ops.require_confirmation", []) or [])
        auto = bool(self.config.policy_get("dangerous_ops.auto_approve", False))

        def approve(request) -> bool:
            decision = self.classify_permission(request.kind, request.title, request.detail)
            if request.kind == "read":
                return True
            if task.get("read_only") and request.kind in ("write", "execute"):
                return False
            if decision.needs_approval:
                if auto:
                    return True
                # 记录待人工确认项（不阻塞本轮，安全侧拒绝）
                self.request(
                    project_id=task["project_id"],
                    kind="dangerous_op",
                    title=f"危险操作被拦截：{request.title}",
                    detail={"agent_id": request.agent_id, "run_id": request.run_id, "patterns": decision.patterns},
                    task_id=task["id"],
                    run_id=request.run_id,
                    agent_id=request.agent_id,
                    risk="high",
                )
                return False
            if permission == "readonly":
                return request.kind == "read"
            return True

        return approve

    # ------------------------------------------------------------------ 运行期命令
    @staticmethod
    def _event_command(event: dict[str, Any]) -> str:
        """从工具调用事件里尽力提取「真正会执行的命令/脚本」。"""
        payload = event.get("tool_input")
        if isinstance(payload, dict):
            for key in ("command", "cmd", "script", "code", "input"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            return _dumps(payload)
        if isinstance(payload, str):
            return payload
        for key in ("tool_output", "tool_content", "tool_title", "tool"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ""

    def inspect_agent_event(self, event: dict[str, Any], *, workspace: Path | str | None = None) -> dict[str, Any]:
        """运行期检查 Agent 的工具调用是否包含危险命令。

        返回 ``{"dangerous": bool, "severity": "critical"|"destructive"|"", "reason", "command"}``。
        这是**尽力而为**的第二道防线：ACP 的 ``tool_call`` 事件可能晚于进程执行，
        真正兜底仍然是进程树回收 + 事后文件审计。它补上的是「命令会打到工作区之外」
        这类事后审计看不到的风险。
        """
        source = str(event.get("source_type") or event.get("kind") or "")
        if source not in ("tool_call", "tool_call_update", "tool_call_progress"):
            return {"dangerous": False, "severity": "", "reason": "", "command": ""}
        command = self._event_command(event)
        if not command:
            return {"dangerous": False, "severity": "", "reason": "", "command": ""}
        # 只审计「真正会执行」的工具调用。写文件工具里的代码文本即使包含
        # ``shutdown``/``del`` 也不该被当成系统命令（真实误报：自测脚本里的
        # ``atlas.shutdown()`` 被当成关机）。有 command/cmd 字段，或工具名/类别
        # 明确是 shell/terminal/execute 时才进入命令模式匹配。
        payload = event.get("tool_input")
        has_command_field = isinstance(payload, dict) and any(
            isinstance(payload.get(key), str) and payload.get(key).strip()
            for key in ("command", "cmd", "script")
        )
        tool_kind = str(event.get("tool_kind") or "").lower()
        tool_name = str(event.get("tool") or event.get("tool_title") or "").lower()
        executable_tool = (
            has_command_field
            or tool_kind in {"execute", "terminal", "run"}
            or any(token in tool_name for token in ("bash", "shell", "command", "terminal", "exec"))
        )
        if not executable_tool:
            return {"dangerous": False, "severity": "", "reason": "", "command": command[:2000]}
        text = command.strip()
        for name, pattern in CRITICAL_COMMAND_PATTERNS:
            if pattern.search(text):
                return {
                    "dangerous": True,
                    "severity": "critical",
                    "reason": f"命中高危系统命令模式：{name}",
                    "command": text[:2000],
                }
        for name, pattern in DESTRUCTIVE_COMMAND_PATTERNS:
            if pattern.search(text):
                return {
                    "dangerous": True,
                    "severity": "destructive",
                    "reason": f"命中破坏性命令模式：{name}",
                    "command": text[:2000],
                }
        return {"dangerous": False, "severity": "", "reason": "", "command": text[:2000]}

    # ------------------------------------------------------------------ 事后
    def audit_changes(
        self,
        *,
        project: dict[str, Any],
        task: dict[str, Any],
        changes: list[dict[str, Any]],
        recorded: list[Any],
    ) -> dict[str, Any]:
        """事后审计：越权 / 保护路径 / 破坏性删除 / 冲突。

        ``changes`` 与 ``recorded`` **按 ``rel_path`` 配对，绝不按位置**：
        调用方（``runner._finalize``）会先把越权改动从 ``changes`` 里剪掉，
        一旦按位置 ``zip``，后面的 (change, record) 全部错位，列表尾部的冲突/保护路径
        判定会被静默丢弃（需求 34 / 44 失效）。
        """
        protect = self.protected_paths()
        by_rel = _index_records(recorded)
        out_of_scope: list[str] = []
        protected_hits: list[str] = []
        destructive: list[str] = []
        conflicts: list[str] = []
        for change in changes:
            rel = change["rel_path"]
            record = by_rel.get(rel)
            # 找不到对应记录时按缺省处理（不臆造越权/冲突），而不是错位或丢弃
            in_scope = bool(getattr(record, "in_scope", True)) if record is not None else True
            conflict = bool(getattr(record, "conflict", False)) if record is not None else False
            if not in_scope:
                out_of_scope.append(rel)
            if matches_any(rel, protect):
                protected_hits.append(rel)
            if change["action"] == "delete":
                destructive.append(rel)
            if conflict:
                conflicts.append(rel)
        return {
            "out_of_scope": sorted(set(out_of_scope)),
            "protected": sorted(set(protected_hits)),
            "destructive": sorted(set(destructive)),
            "conflicts": sorted(set(conflicts)),
        }


def _index_records(recorded: list[Any]) -> dict[str, Any]:
    """把 ``journal.record()`` 的返回值按 ``rel_path`` 建索引（兼容 dataclass / dict）。"""
    index: dict[str, Any] = {}
    for record in recorded or []:
        if isinstance(record, dict):
            rel = record.get("rel_path")
        else:
            rel = getattr(record, "rel_path", None)
        if rel and rel not in index:
            index[str(rel)] = record
    return index


def _dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _approval(row) -> dict[str, Any]:
    import json

    data = dict(row)
    try:
        data["detail"] = json.loads(data.get("detail") or "{}")
    except (TypeError, ValueError):
        data["detail"] = {}
    return data
