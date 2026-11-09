"""Prometheus 指标（文本 exposition format，零依赖）。

本机工作台也需要被监控：任务积压、失败率、Agent 耗时、审批堆积、槽位占用、
WebSocket 客户端数、数据库/运行目录体积。这里只读 SQLite + 内存状态，
不触发任何 Agent 探测。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from . import __version__


def _escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**items: Any) -> str:
    pairs = [f'{key}="{_escape(value)}"' for key, value in items.items() if value is not None]
    return "{" + ",".join(pairs) + "}" if pairs else ""


def _dir_size(path: str | os.PathLike[str]) -> int:
    total = 0
    base = Path(path)
    if not base.exists():
        return 0
    stack = [str(base)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += int(entry.stat(follow_symlinks=False).st_size)
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def collect(app, *, api_state: Any = None) -> str:
    """生成 Prometheus 文本；任何单项查询失败都不影响其余指标。"""
    lines: list[str] = []

    def metric(name: str, help_text: str, kind: str = "gauge") -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")

    metric("multiagent_up", "工作台进程是否存活")
    lines.append("multiagent_up 1")
    metric("multiagent_info", "工作台版本信息")
    lines.append(f"multiagent_info{_labels(version=__version__)} 1")
    metric("multiagent_uptime_seconds", "工作台进程运行时长")
    started = getattr(app, "started_at", None) or time.time()
    lines.append(f"multiagent_uptime_seconds {max(0.0, time.time() - float(started)):.3f}")

    # 任务
    try:
        metric("multiagent_tasks", "按项目与状态统计的任务数")
        for row in app.db.query(
            "SELECT project_id, status, COUNT(*) AS c FROM tasks GROUP BY project_id, status"
        ):
            lines.append(
                f"multiagent_tasks{_labels(project_id=row['project_id'], status=row['status'])} {int(row['c'])}"
            )
    except Exception:  # noqa: BLE001
        pass

    # 运行
    try:
        metric("multiagent_runs_total", "按 Agent 与状态统计的调用次数", "counter")
        for row in app.db.query(
            "SELECT agent_id, status, COUNT(*) AS c FROM runs GROUP BY agent_id, status"
        ):
            lines.append(
                f"multiagent_runs_total{_labels(agent_id=row['agent_id'], status=row['status'])} {int(row['c'])}"
            )
        metric("multiagent_run_duration_seconds", "按 Agent 统计的调用耗时", "summary")
        for row in app.db.query(
            "SELECT agent_id, COUNT(*) AS c, COALESCE(SUM(duration_ms),0) AS ms FROM runs GROUP BY agent_id"
        ):
            labels = _labels(agent_id=row["agent_id"])
            lines.append(f"multiagent_run_duration_seconds_count{labels} {int(row['c'])}")
            lines.append(f"multiagent_run_duration_seconds_sum{labels} {float(row['ms']) / 1000.0:.3f}")
    except Exception:  # noqa: BLE001
        pass

    # 运行健康度（心跳 / 产出活跃）
    try:
        from datetime import datetime, timedelta, timezone

        def ago(seconds: float) -> str:
            moment = datetime.now(timezone.utc) - timedelta(seconds=seconds)
            return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        metric("multiagent_runs_stalled", "运行中但心跳/产出超时的调用数")
        heartbeat = app.db.query_one(
            "SELECT COUNT(*) AS c FROM runs WHERE status = 'running' "
            "AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
            (ago(60),),
        )
        activity = app.db.query_one(
            "SELECT COUNT(*) AS c FROM runs WHERE status = 'running' "
            "AND (last_activity_at IS NULL OR last_activity_at < ?)",
            (ago(30 * 60),),
        )
        lines.append(f'multiagent_runs_stalled{{kind="heartbeat"}} {int(heartbeat["c"] if heartbeat else 0)}')
        lines.append(f'multiagent_runs_stalled{{kind="activity"}} {int(activity["c"] if activity else 0)}')
    except Exception:  # noqa: BLE001
        pass

    # 额度
    try:
        metric("multiagent_agent_usage", "Agent 额度累计用量")
        for row in app.db.query("SELECT * FROM agent_usage"):
            for field in ("invocations", "seconds", "cost", "tokens"):
                lines.append(
                    f"multiagent_agent_usage{_labels(agent_id=row['agent_id'], metric=field)} "
                    f"{float(row[field] or 0):.6f}"
                )
    except Exception:  # noqa: BLE001
        pass

    # 审批
    try:
        metric("multiagent_approvals_pending", "按项目统计的待人工确认数")
        for row in app.db.query(
            "SELECT project_id, COUNT(*) AS c FROM approvals WHERE status = 'pending' GROUP BY project_id"
        ):
            lines.append(
                f"multiagent_approvals_pending{_labels(project_id=row['project_id'])} {int(row['c'])}"
            )
    except Exception:  # noqa: BLE001
        pass

    # 调度槽位
    try:
        metric("multiagent_scheduler_slots", "按项目统计的跨进程调度槽位")
        for row in app.db.query(
            "SELECT project_id, COUNT(*) AS c FROM scheduler_slots GROUP BY project_id"
        ):
            lines.append(
                f"multiagent_scheduler_slots{_labels(project_id=row['project_id'])} {int(row['c'])}"
            )
    except Exception:  # noqa: BLE001
        pass

    # 存储
    try:
        metric("multiagent_db_bytes", "SQLite 主库文件字节数")
        db_path = Path(app.config.db_path)
        lines.append(f"multiagent_db_bytes {db_path.stat().st_size if db_path.exists() else 0}")
        metric("multiagent_runtime_bytes", "运行证据目录字节数")
        lines.append(f"multiagent_runtime_bytes {_dir_size(app.config.runtime_dir)}")
        metric("multiagent_blob_bytes", "内容寻址 blob 目录字节数")
        lines.append(f"multiagent_blob_bytes {_dir_size(Path(app.config.data_dir) / 'blobs')}")
    except Exception:  # noqa: BLE001
        pass

    # 实时流 / WebSocket
    try:
        hub = getattr(api_state, "hub", None)
        coalescer = getattr(api_state, "coalescer", None)
        if hub is not None:
            metric("multiagent_ws_clients", "当前 WebSocket 订阅者数")
            lines.append(f"multiagent_ws_clients {len(getattr(hub, '_subs', ()))}")
        if coalescer is not None:
            metric("multiagent_stream_pending", "实时流合并器待发送块数")
            lines.append(f"multiagent_stream_pending {int(getattr(coalescer, 'pending_count', 0))}")
    except Exception:  # noqa: BLE001
        pass

    return "\n".join(lines) + "\n"
