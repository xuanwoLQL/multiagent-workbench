"""实时流分片合并（``StreamCoalescer``）。

为什么需要
----------
ACP Agent 把一条思考/消息拆成 **token 级 chunk**。线上实测（``.e2e/live-capture.json``）：
1215 条 ``agent_thought_chunk`` 平均 4.2 字、35 条 ``agent_message_chunk`` 平均 1.7 字 ——
直接推给前端就是「一行一个 token」：每秒上百条 WebSocket 消息 + 整块重渲染，
实时视图既看不清也在烧 CPU。

本模块接在 ``runner.event_sink`` 与事件总线之间：

* 只合并 ``agent_message_chunk`` / ``agent_thought_chunk``，其它事件一律原样直发；
* 合并键 = ``(project_id, task_id, run_id, agent_id, kind)`` —— 不同 run / 不同 kind
  绝不串味；
* 同键且距该键上一次 flush < ``merge_window``（默认 180ms）→ 追加 ``text``；
* 后台线程每 ``flush_interval``（默认 120ms）flush「已经超时」的 pending，
  保证不动的流也能在 ~200ms 内落地；
* 任何**非 chunk 事件**（tool_call / task.event / run 结束 …）先 flush 全部 pending
  再发，保证前端看到的顺序与真实发生顺序一致；
* 输出事件保留原字段，追加 ``text``（合并后）、``chunk_count``、``started_at``；
  ``at`` 取最后一条时间；``seq`` 若上游带则取最后一条，否则留给 EventHub 分配。

线程模型：合并/发布都在同一把锁里完成（``EventHub.publish`` 不会回调本类，
无重入风险），因此多 run 并发时事件顺序仍然稳定。后台线程**按需启动**、
pending 清空后自行退出，不占常驻线程。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Iterable

from .util import now_iso

LOGGER = logging.getLogger("multiagent.stream")

#: 需要合并的 chunk 事件 kind（与 ACP ``sessionUpdate`` 对齐）。
CHUNK_KINDS = frozenset({"agent_message_chunk", "agent_thought_chunk"})

#: 同键合并窗口：距该键上一次 flush 小于它就继续追加（秒）。
DEFAULT_MERGE_WINDOW_SECONDS = 0.18
#: 后台线程检查「超时 pending」的间隔（秒）。
DEFAULT_FLUSH_INTERVAL_SECONDS = 0.12
#: 单个合并事件的文本上限（字符）。超过立即 flush，避免单条事件过长。
DEFAULT_MAX_TEXT_CHARS = 4000

#: 合并缓冲：``key -> buffer``（dict 保持插入顺序 = 事件到达顺序）。
_Buffer = dict[str, Any]


class StreamCoalescer:
    """把 token 级 chunk 合并成「块」再发布（见模块文档）。"""

    def __init__(
        self,
        publish: Callable[[dict[str, Any]], None],
        *,
        merge_window: float = DEFAULT_MERGE_WINDOW_SECONDS,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL_SECONDS,
        max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], str] = now_iso,
    ) -> None:
        self._publish = publish
        self._merge_window = max(0.0, float(merge_window))
        self._flush_interval = max(0.01, float(flush_interval))
        self._max_text_chars = max(1, int(max_text_chars))
        self._clock = clock
        self._now = now
        self._lock = threading.Lock()
        self._pending: dict[tuple[Any, ...], _Buffer] = {}
        self._timer: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ 入口
    def feed(self, event: dict[str, Any]) -> None:
        """接住一条事件：chunk 进缓冲，其它事件先 flush 再原样发布。"""
        if not isinstance(event, dict):
            return
        kind = str(event.get("kind") or "")
        if kind in CHUNK_KINDS:
            self._feed_chunk(event, kind)
            return
        with self._lock:
            # 先 flush 再发非 chunk 事件：否则「思考块」会排到工具卡/任务事件后面
            self._flush_locked()
            self._publish(dict(event))

    def flush(self) -> int:
        """立即 flush 全部 pending，返回发布条数。"""
        with self._lock:
            return self._flush_locked()

    def flush_expired(self) -> int:
        """只 flush 超过合并窗口的 pending（后台线程调用）。"""
        now_mono = self._clock()
        with self._lock:
            expired = [
                key
                for key, buf in self._pending.items()
                if now_mono - float(buf["start_mono"]) >= self._merge_window
            ]
            return self._flush_locked(keys=expired)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def close(self, timeout: float = 1.0) -> None:
        """停止后台线程并 flush 残留（进程退出 / 测试清理用）。"""
        self._stop.set()
        try:
            self.flush()
        except Exception:  # noqa: BLE001 - 收尾失败不应抛出
            LOGGER.exception("实时流合并器 flush 失败")
        thread = self._timer
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    # ------------------------------------------------------------------ 合并
    def _feed_chunk(self, event: dict[str, Any], kind: str) -> None:
        key = (
            event.get("project_id"),
            event.get("task_id"),
            event.get("run_id"),
            event.get("agent_id"),
            kind,
        )
        now_mono = self._clock()
        with self._lock:
            buf = self._pending.get(key)
            if buf is not None and now_mono - float(buf["start_mono"]) >= self._merge_window:
                # 窗口已过：先把旧块发出去，再开新块（不把两块拼在一起）
                self._flush_locked(keys=[key])
                buf = None
            if buf is None:
                moment = event.get("at") or self._now()
                buf = {
                    "event": dict(event),
                    "text": _text_of(event),
                    "chunks": 1,
                    "started_at": moment,
                    "last_at": moment,
                    "last_seq": event.get("seq"),
                    "start_mono": now_mono,
                }
                self._pending[key] = buf
            else:
                buf["text"] += _text_of(event)
                buf["chunks"] = int(buf["chunks"]) + 1
                if event.get("at"):
                    buf["last_at"] = event["at"]
                if event.get("seq") is not None:
                    buf["last_seq"] = event["seq"]
            self._ensure_timer_locked()
            if len(str(buf["text"])) >= self._max_text_chars:
                self._flush_locked(keys=[key])

    def _flush_locked(self, keys: Iterable[tuple[Any, ...]] | None = None) -> int:
        """把指定（默认全部）缓冲合并成事件并发布。调用方必须持有 ``self._lock``。"""
        targets = list(self._pending) if keys is None else list(keys)
        payloads: list[dict[str, Any]] = []
        for key in targets:
            buf = self._pending.pop(key, None)
            if buf is not None:
                payloads.append(_merged_payload(buf))
        for payload in payloads:
            self._publish(payload)
        return len(payloads)

    # ------------------------------------------------------------------ 后台线程
    def _ensure_timer_locked(self) -> None:
        if self._timer is not None and self._timer.is_alive():
            return
        self._timer = threading.Thread(
            target=self._timer_loop, name="stream-coalescer", daemon=True
        )
        self._timer.start()

    def _timer_loop(self) -> None:
        while True:
            if self._stop.wait(self._flush_interval):
                return
            try:
                self.flush_expired()
            except Exception:  # noqa: BLE001 - 后台线程不能因为一次 flush 失败就死掉
                LOGGER.exception("实时流合并线程 flush 失败")
            with self._lock:
                if not self._pending:
                    # 没有 pending 就退出：空闲时零线程、零 CPU
                    self._timer = None
                    return


def _text_of(event: dict[str, Any]) -> str:
    value = event.get("text")
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _merged_payload(buf: _Buffer) -> dict[str, Any]:
    """合并后的输出事件：保留原字段 + text/chunk_count/started_at，at 取最后一条。"""
    event = dict(buf["event"])
    event["text"] = buf["text"]
    event["chunk_count"] = int(buf["chunks"])
    event["started_at"] = buf["started_at"]
    event["at"] = buf["last_at"]
    if buf["last_seq"] is not None:
        event["seq"] = buf["last_seq"]
    else:
        event.pop("seq", None)  # 上游没有 seq：交给事件总线在 publish 时编号
    return event
