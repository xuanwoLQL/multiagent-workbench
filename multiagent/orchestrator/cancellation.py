"""运行中调用的取消注册表（需求 54「终止执行」）。

`runner.run()` 每次调用创建一个 :class:`threading.Event`，按 ``run_id`` 登记到这里，
并塞进 ``AgentRequest.cancel_event``；适配器轮询该 event，一旦被 set 就整树回收子进程。
`Engine.cancel(task_id)` / `Engine.stop_project(project_id)` 通过这里 set 对应 event。

为什么是独立模块：``runner`` 需要在引擎装配之前就拿到注册表（``Workbench.__init__`` 先建
runner 再建 engine），而 ``engine`` 又 import ``runner``（RunOutcome）——独立模块避免循环 import，
同时保证 registry 只有一份（Engine 持有，注入给 runner）。

线程安全：所有读写都在一把 RLock 下；``register`` 后必须 ``unregister``（runner 用 finally 保证），
否则会泄漏——这既是内存问题，也会让后续 ``cancel`` 误命中已经结束的 run。
"""

from __future__ import annotations

import threading
from typing import Iterable


class RunCancelRegistry:
    """``run_id -> threading.Event`` 注册表。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._events: dict[str, threading.Event] = {}

    def register(self, run_id: str) -> threading.Event:
        """登记一次调用的取消信号，返回调用方（runner）持有的事件对象。"""
        event = threading.Event()
        with self._lock:
            self._events[run_id] = event
        return event

    def unregister(self, run_id: str) -> None:
        with self._lock:
            self._events.pop(run_id, None)

    def event_for(self, run_id: str) -> threading.Event | None:
        with self._lock:
            return self._events.get(run_id)

    def cancel(self, run_id: str) -> bool:
        """set 某个 run 的取消信号；该 run 不在跑（未登记）返回 False。"""
        event = self.event_for(run_id)
        if event is None:
            return False
        event.set()
        return True

    def cancel_many(self, run_ids: Iterable[str]) -> int:
        return sum(1 for run_id in list(run_ids) if self.cancel(run_id))

    def is_set(self, run_id: str) -> bool:
        event = self.event_for(run_id)
        return bool(event is not None and event.is_set())

    def active_run_ids(self) -> set[str]:
        with self._lock:
            return set(self._events)

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)
