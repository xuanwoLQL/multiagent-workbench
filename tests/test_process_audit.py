"""P1-7 进程/端口审计回归测试。

全部使用注入的假 prober（不监听真实端口、不依赖 netstat/PowerShell），
验证「新增监听进程的归属判定 + 保守回收 + 失败隔离」这三条核心契约。
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest

from multiagent.agents import process_audit
from multiagent.agents.process_audit import ProcessAudit, SystemProber


class FakeProber:
    """按调用顺序返回预设快照；元素是异常时抛出（模拟 prober 崩溃）。

    ``snapshot`` 接受 ``with_metadata``（与 SystemProber 一致），``describe``
    记录调用参数，用来断言「没有新增 pid 时绝不查元数据」。
    """

    supported = True

    def __init__(self, *snapshots, descriptions=None):
        self._snapshots = list(snapshots)
        self._descriptions = dict(descriptions or {})
        self.snapshot_calls = 0
        self.describe_calls: list[list[int]] = []

    def snapshot(self, with_metadata=False):
        index = min(self.snapshot_calls, len(self._snapshots) - 1)
        self.snapshot_calls += 1
        item = self._snapshots[index]
        if isinstance(item, BaseException):
            raise item
        return item

    def describe(self, pids):
        self.describe_calls.append(list(pids))
        return {
            pid: self._descriptions[pid]
            for pid in pids
            if pid in self._descriptions
        }


def _proc(ports, command="", exe_path="", created_at=None, **extra):
    entry = {
        "ports": list(ports),
        "command": command,
        "exe_path": exe_path,
        "created_at": created_at,
    }
    entry.update(extra)
    return entry


def _begin(before, after, **kwargs):
    prober = FakeProber(before, after)
    return ProcessAudit.begin(prober, **kwargs), prober


# --------------------------------------------------------------------------- 归属与回收


def test_new_listener_owned_by_workdir_is_reaped(tmp_path):
    workdir = tmp_path / "task-workdir"
    workdir.mkdir()
    before = {101: _proc([80], "systemd")}
    command = f'python -m http.server 8000 --directory "{workdir}"'
    after = {101: before[101], 4242: _proc([8001, 8000], command)}

    killer = MagicMock(return_value=True)
    audit, prober = _begin(before, after, killer=killer)
    result = audit.finish(workdir)

    assert prober.snapshot_calls == 2
    assert prober.describe_calls == [], "快照已带 command 时不应再查元数据"
    assert result["supported"] is True
    assert result["before_count"] == 1
    assert result["after_count"] == 2
    assert result["started"] == [
        {"pid": 4242, "ports": [8000, 8001], "command": command, "owned": True}
    ]
    assert result["reaped"] == [
        {"pid": 4242, "ports": [8000, 8001], "command": command}
    ]
    killer.assert_called_once_with(4242)
    assert result["warnings"] == []


def test_unowned_new_listener_is_only_reported_never_killed(tmp_path):
    workdir = tmp_path / "task-workdir"
    workdir.mkdir()
    command = "/usr/sbin/nginx -g 'daemon off;'"
    killer = MagicMock()

    audit, _ = _begin({}, {777: _proc([8080], command)}, killer=killer)
    result = audit.finish(workdir)

    assert result["supported"] is True
    assert result["started"] == [
        {"pid": 777, "ports": [8080], "command": command, "owned": False}
    ]
    assert result["reaped"] == []
    killer.assert_not_called()
    assert result["warnings"], "无法判断归属的新监听进程必须留下 warning"


def test_no_new_listeners_yields_empty_result(tmp_path):
    workdir = tmp_path / "task-workdir"
    workdir.mkdir()
    processes = {10: _proc([443], "svc-a"), 11: _proc([3306], "mysqld")}

    killer = MagicMock()
    audit, prober = _begin(processes, dict(processes), killer=killer)
    result = audit.finish(workdir)

    assert prober.snapshot_calls == 2
    assert prober.describe_calls == [], "没有新增监听时绝不能调用 describe"
    assert result == {
        "supported": True,
        "started": [],
        "reaped": [],
        "warnings": [],
        "before_count": 2,
        "after_count": 2,
    }
    killer.assert_not_called()


def test_preexisting_pid_is_never_touched_even_if_command_matches(tmp_path):
    workdir = tmp_path / "task-workdir"
    workdir.mkdir()
    command = f"python serve.py --root {workdir}"
    processes = {777: _proc([3000], command)}

    killer = MagicMock()
    audit, _ = _begin(processes, dict(processes), killer=killer)
    result = audit.finish(workdir)

    assert result["started"] == []
    assert result["reaped"] == []
    killer.assert_not_called()


def test_listener_owned_by_cwd(tmp_path):
    workdir = tmp_path / "task-workdir"
    (workdir / "app").mkdir(parents=True)
    command = "node server.js"
    after = {555: _proc([7000], command, cwd=str(workdir / "app"))}

    killer = MagicMock()
    audit, _ = _begin({}, after, killer=killer)
    result = audit.finish(workdir)

    assert result["started"][0]["owned"] is True
    assert [item["pid"] for item in result["reaped"]] == [555]
    killer.assert_called_once_with(555)


def test_listener_owned_by_exe_path(tmp_path):
    workdir = tmp_path / "task-workdir"
    workdir.mkdir()
    exe_path = str(workdir / "bin" / "server")
    after = {321: _proc([5000], "", exe_path=exe_path)}

    killer = MagicMock()
    audit, _ = _begin({}, after, killer=killer)
    result = audit.finish(workdir)

    assert result["started"][0]["owned"] is True
    killer.assert_called_once_with(321)


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 路径大小写不敏感")
def test_windows_ownership_is_case_insensitive(tmp_path):
    workdir = tmp_path / "TaskWorkDir"
    workdir.mkdir()
    command = f'python -m http.server --directory "{str(workdir).upper()}"'
    killer = MagicMock()

    audit, _ = _begin({}, {1234: _proc([8080], command)}, killer=killer)
    result = audit.finish(workdir)

    assert result["started"][0]["owned"] is True
    killer.assert_called_once_with(1234)


def test_reap_false_reports_but_does_not_kill(tmp_path):
    workdir = tmp_path / "task-workdir"
    workdir.mkdir()
    command = f"python -m http.server --directory {workdir}"
    killer = MagicMock()

    audit, _ = _begin({}, {888: _proc([8080], command)}, killer=killer)
    result = audit.finish(workdir, reap=False)

    assert result["started"][0]["owned"] is True
    assert result["reaped"] == []
    killer.assert_not_called()


def test_returned_command_is_truncated_to_500_chars(tmp_path):
    workdir = tmp_path / "task-workdir"
    workdir.mkdir()
    command = f"python -m http.server --directory {workdir} " + "x" * 2000

    killer = MagicMock()
    audit, _ = _begin({}, {900: _proc([8080], command)}, killer=killer)
    result = audit.finish(workdir)

    assert len(result["started"][0]["command"]) == process_audit.MAX_COMMAND_CHARS == 500
    assert len(result["reaped"][0]["command"]) == 500


# --------------------------------------------------------------------------- 自我保护


def test_self_parent_and_low_pids_are_protected(tmp_path):
    workdir = tmp_path / "task-workdir"
    workdir.mkdir()
    command = f"python -m http.server --directory {workdir}"
    after = {
        3: _proc([9000], command),
        os.getpid(): _proc([9001], command),
        os.getppid(): _proc([9002], command),
    }

    killer = MagicMock()
    audit, _ = _begin({}, after, killer=killer)
    result = audit.finish(workdir)

    killer.assert_not_called()
    assert result["reaped"] == []
    assert len(result["started"]) == len(after)
    assert all(item["owned"] for item in result["started"])
    assert any("拒绝终止" in warning for warning in result["warnings"])


def test_default_killer_resolves_module_terminate_pid(monkeypatch, tmp_path):
    """不传 killer 时必须走模块级 _terminate_pid（可被 monkeypatch 替换）。"""
    workdir = tmp_path / "task-workdir"
    workdir.mkdir()
    command = f"python -m http.server --directory {workdir}"
    calls: list[int] = []
    monkeypatch.setattr(
        process_audit,
        "_terminate_pid",
        lambda pid: calls.append(pid) or True,
    )

    audit = ProcessAudit.begin(FakeProber({}, {2468: _proc([8080], command)}))
    result = audit.finish(workdir)

    assert calls == [2468]
    assert [item["pid"] for item in result["reaped"]] == [2468]


def test_killer_exception_becomes_warning_not_crash(tmp_path):
    workdir = tmp_path / "task-workdir"
    workdir.mkdir()
    command = f"python -m http.server --directory {workdir}"

    def exploding_killer(pid):
        raise OSError("access denied")

    audit, _ = _begin({}, {4321: _proc([8080], command)}, killer=exploding_killer)
    result = audit.finish(workdir)

    assert result["reaped"] == []
    assert any("终止进程失败" in warning for warning in result["warnings"])


# --------------------------------------------------------------------------- 性能契约（轻量快照 + 按需 describe）


def test_no_new_pids_never_calls_describe(tmp_path):
    """新增为空时：begin/finish 都只走轻量快照，describe 一次都不能调。"""
    workdir = tmp_path / "task-workdir"
    workdir.mkdir()
    processes = {10: _proc([80]), 11: _proc([3306])}  # 轻量快照只有 pid/ports

    killer = MagicMock()
    prober = FakeProber(dict(processes), dict(processes))
    result = ProcessAudit.begin(prober, killer=killer).finish(workdir)

    assert result["started"] == []
    assert result["reaped"] == []
    assert result["warnings"] == []
    assert prober.describe_calls == []
    killer.assert_not_called()


def test_describe_called_once_only_for_new_pids_without_metadata(tmp_path):
    workdir = tmp_path / "task-workdir"
    workdir.mkdir()
    command = f"python -m http.server --directory {workdir}"
    before = {111: _proc([80])}
    after = {111: _proc([80]), 222: _proc([7000]), 333: _proc([7001])}

    prober = FakeProber(
        before,
        after,
        descriptions={
            222: {"command": command, "exe_path": "", "created_at": None},
            # 333 未返回描述 → 保持无归属、只告警
        },
    )
    killer = MagicMock()
    result = ProcessAudit.begin(prober, killer=killer).finish(workdir)

    assert prober.describe_calls == [[222, 333]]
    by_pid = {item["pid"]: item for item in result["started"]}
    assert 111 not in by_pid  # 调用前已存在的 pid 从不进入 started
    assert by_pid[222]["owned"] is True
    assert by_pid[333]["owned"] is False
    assert [item["pid"] for item in result["reaped"]] == [222]
    killer.assert_called_once_with(222)
    assert any("333" in warning for warning in result["warnings"])


def test_describe_failure_only_warns(tmp_path):
    workdir = tmp_path / "task-workdir"
    workdir.mkdir()

    class BrokenDescribeProber(FakeProber):
        def describe(self, pids):
            self.describe_calls.append(list(pids))
            raise RuntimeError("cim exploded")

    prober = BrokenDescribeProber({}, {333: _proc([7000])})
    result = ProcessAudit.begin(prober, killer=MagicMock()).finish(workdir)

    assert result["supported"] is True, "元数据失败不应把轻量枚举判成 unsupported"
    assert result["started"][0]["owned"] is False
    assert result["reaped"] == []
    assert any("元数据" in warning for warning in result["warnings"])


def test_legacy_prober_without_with_metadata_kwarg_still_works(tmp_path):
    """旧 prober 只有 snapshot()：不能因为签名不兼容而崩掉。"""

    class LegacyProber:
        supported = True

        def __init__(self, *snapshots):
            self._snapshots = list(snapshots)
            self.calls = 0

        def snapshot(self):
            item = self._snapshots[min(self.calls, len(self._snapshots) - 1)]
            self.calls += 1
            return item

    workdir = tmp_path / "task-workdir"
    workdir.mkdir()
    command = f"python -m http.server --directory {workdir}"
    killer = MagicMock()
    prober = LegacyProber({}, {444: _proc([8080], command)})
    result = ProcessAudit.begin(prober, killer=killer).finish(workdir)

    assert prober.calls == 2
    assert result["started"][0]["owned"] is True
    killer.assert_called_once_with(444)


def test_listening_only_prober_is_supported(tmp_path):
    """只实现 listening()/describe() 的 prober（SystemProber 的新形态）。"""

    class ListeningProber:
        supported = True

        def __init__(self):
            self.snapshots = [{}, {666: _proc([9090])}]
            self.calls = 0
            self.describe_calls = []

        def listening(self):
            item = self.snapshots[min(self.calls, len(self.snapshots) - 1)]
            self.calls += 1
            return item

        def describe(self, pids):
            self.describe_calls.append(list(pids))
            return {
                pid: {
                    "command": f"python -m http.server --directory {workdir}",
                    "exe_path": "",
                    "created_at": None,
                }
                for pid in pids
            }

    workdir = tmp_path / "task-workdir"
    workdir.mkdir()
    prober = ListeningProber()
    killer = MagicMock()
    result = ProcessAudit.begin(prober, killer=killer).finish(workdir)

    assert prober.calls == 2
    assert prober.describe_calls == [[666]]
    assert result["started"][0]["owned"] is True
    killer.assert_called_once_with(666)


# --------------------------------------------------------------------------- 失败隔离


def test_prober_failure_returns_unsupported_without_raising(tmp_path):
    audit = ProcessAudit.begin(FakeProber(RuntimeError("netstat exploded")))
    result = audit.finish(tmp_path)

    assert result["supported"] is False
    assert result["started"] == []
    assert result["reaped"] == []
    assert result["warnings"]
    assert all(isinstance(item, str) for item in result["warnings"])


def test_finish_snapshot_failure_after_successful_begin(tmp_path):
    audit = ProcessAudit.begin(FakeProber({}, RuntimeError("snapshot boom")))
    result = audit.finish(tmp_path)

    assert result["supported"] is False
    assert result["started"] == []
    assert result["reaped"] == []
    assert result["warnings"]


def test_invalid_snapshot_type_degrades_gracefully(tmp_path):
    audit = ProcessAudit.begin(FakeProber(["not-a-mapping"]))
    result = audit.finish(tmp_path)

    assert result["supported"] is False
    assert result["warnings"]


def test_unsupported_prober_never_reaps(tmp_path):
    """枚举不可靠时即使 command 命中 workdir，也只能报告、不能杀。"""

    class UnsupportedProber(FakeProber):
        supported = False

    workdir = tmp_path / "task-workdir"
    workdir.mkdir()
    command = f"python -m http.server --directory {workdir}"
    killer = MagicMock()

    prober = UnsupportedProber({}, {1357: _proc([8080], command)})
    result = ProcessAudit.begin(prober, killer=killer).finish(workdir)

    assert result["supported"] is False
    assert result["started"][0]["owned"] is True
    assert result["reaped"] == []
    killer.assert_not_called()
    assert result["warnings"]


# --------------------------------------------------------------------------- CIM 兜底（P2）


class CimProber:
    """支持 ``include_cim`` 的新形态 prober（模拟 SystemProber 的 finish 路径）。

    - ``lightweight``：``listening(include_cim=False)`` 依次返回的快照；
    - ``cim``：``listening(include_cim=True)`` 依次返回的快照；
    - ``cim_error``：设置后，``include_cim=True`` 时直接抛异常（模拟 CIM 崩溃）。

    记录 ``calls``（每次 include_cim 的值）与 ``describe_calls``，便于断言
    begin/finish 的调用形态和「没有新增 pid 不查元数据」的契约。
    """

    supported = True

    def __init__(self, lightweight, cim=None, *, cim_error=None, descriptions=None):
        self._lightweight = list(lightweight)
        self._cim = list(cim) if isinstance(cim, list) else ([cim] if cim is not None else [])
        self._cim_error = cim_error
        self._descriptions = dict(descriptions or {})
        self.calls: list[bool] = []
        self.describe_calls: list[list[int]] = []
        self._light_calls = 0
        self._cim_calls = 0

    def listening(self, *, include_cim=False):
        self.calls.append(bool(include_cim))
        if include_cim:
            self._cim_calls += 1
            if self._cim_error is not None:
                raise self._cim_error
            if not self._cim:
                return {}
            return self._cim[min(self._cim_calls - 1, len(self._cim) - 1)]
        self._light_calls += 1
        if not self._lightweight:
            return {}
        return self._lightweight[min(self._light_calls - 1, len(self._lightweight) - 1)]

    def describe(self, pids):
        self.describe_calls.append(list(pids))
        return {
            pid: self._descriptions[pid]
            for pid in pids
            if pid in self._descriptions
        }


def test_cim_only_listener_is_reaped_by_finish(tmp_path):
    """netstat 漏报、仅 CIM 可见的新增监听进程，finish 必须识别并回收。"""
    workdir = tmp_path / "task-workdir"
    workdir.mkdir()
    command = f'python -m http.server 8000 --directory "{workdir}"'
    prober = CimProber(lightweight=[{}], cim=[{4321: _proc([8000], command)}])
    killer = MagicMock()

    result = ProcessAudit.begin(prober, killer=killer).finish(workdir)

    assert prober.calls == [False, True], "begin 必须轻量，finish 才启用 CIM 兜底"
    assert prober.describe_calls == [], "CIM 快照已带 command 时不应再查元数据"
    assert result["supported"] is True
    assert result["before_count"] == 0
    assert result["after_count"] == 1
    assert result["started"] == [
        {"pid": 4321, "ports": [8000], "command": command, "owned": True}
    ]
    assert result["reaped"] == [
        {"pid": 4321, "ports": [8000], "command": command}
    ]
    killer.assert_called_once_with(4321)
    assert result["warnings"] == []


def test_cim_snapshot_without_new_pids_never_calls_describe(tmp_path):
    """CIM 快照也没有新增 pid 时，仍然不能调用 describe。"""
    workdir = tmp_path / "task-workdir"
    workdir.mkdir()
    processes = {10: _proc([80]), 11: _proc([3306])}
    prober = CimProber(lightweight=[processes, processes], cim=[processes])
    killer = MagicMock()

    result = ProcessAudit.begin(prober, killer=killer).finish(workdir)

    assert prober.calls == [False, True]
    assert prober.describe_calls == [], "没有新增监听时绝不能调用 describe"
    assert result == {
        "supported": True,
        "started": [],
        "reaped": [],
        "warnings": [],
        "before_count": 2,
        "after_count": 2,
    }
    killer.assert_not_called()


def test_cim_query_exception_falls_back_to_netstat_snapshot(tmp_path):
    """CIM 查询抛异常：回退轻量快照，保留 netstat 结果，finish 不抛。"""
    workdir = tmp_path / "task-workdir"
    workdir.mkdir()
    command = f"python -m http.server --directory {workdir}"
    prober = CimProber(
        lightweight=[{}, {901: _proc([8081], command)}],
        cim_error=RuntimeError("Get-NetTCPConnection failed"),
    )
    killer = MagicMock()

    result = ProcessAudit.begin(prober, killer=killer).finish(workdir)

    assert prober.calls == [False, True, False], "CIM 失败后必须回退轻量快照"
    assert result["supported"] is True
    assert result["after_count"] == 1
    assert result["started"] == [
        {"pid": 901, "ports": [8081], "command": command, "owned": True}
    ]
    assert [item["pid"] for item in result["reaped"]] == [901]
    killer.assert_called_once_with(901)
    assert any("CIM" in warning for warning in result["warnings"])


def test_cim_empty_snapshot_falls_back_to_netstat_snapshot(tmp_path):
    """注入 prober 用空快照表达 CIM 失败：同样回退轻量快照，不丢 netstat 结果。"""
    workdir = tmp_path / "task-workdir"
    workdir.mkdir()
    command = f"python -m http.server --directory {workdir}"
    prober = CimProber(
        lightweight=[{}, {902: _proc([8082], command)}],
        cim=[{}],
    )
    killer = MagicMock()

    result = ProcessAudit.begin(prober, killer=killer).finish(workdir)

    assert prober.calls == [False, True, False]
    assert result["supported"] is True
    assert result["after_count"] == 1
    assert [item["pid"] for item in result["reaped"]] == [902]
    killer.assert_called_once_with(902)
    assert any("CIM 兜底快照为空" in warning for warning in result["warnings"])


def test_old_listening_prober_without_include_cim_is_called_as_before(tmp_path):
    """旧 listening() 不接受 include_cim：finish 必须回退原调用而非 TypeError。"""

    class LegacyListeningProber:
        supported = True

        def __init__(self):
            self.snapshots = [{}, {7777: _proc([8080], command)}]
            self.calls = 0

        def listening(self):  # 旧签名：没有 include_cim
            item = self.snapshots[min(self.calls, len(self.snapshots) - 1)]
            self.calls += 1
            return item

    workdir = tmp_path / "task-workdir"
    workdir.mkdir()
    command = f"python -m http.server --directory {workdir}"
    killer = MagicMock()
    prober = LegacyListeningProber()

    result = ProcessAudit.begin(prober, killer=killer).finish(workdir)

    assert prober.calls == 2, "旧 listening() 必须被正常回退调用"
    assert result["supported"] is True
    assert [item["pid"] for item in result["reaped"]] == [7777]
    killer.assert_called_once_with(7777)


def test_system_prober_merges_cim_with_netstat_by_pid(monkeypatch):
    monkeypatch.setattr(process_audit, "IS_WIN", True)
    prober = SystemProber()
    calls = []

    def fake_run(argv, *, timeout=None):
        calls.append(argv)
        if argv[0] == "netstat":
            return 0, "  TCP    127.0.0.1:8000    0.0.0.0:0    LISTENING    111\n"
        return 0, (
            '[{"LocalPort":8001,"OwningProcess":222},'
            '{"LocalPort":8002,"OwningProcess":222},'
            '{"LocalPort":8000,"OwningProcess":111}]'
        )

    monkeypatch.setattr(prober, "_run", fake_run)
    snapshot = prober.listening(include_cim=True)

    assert snapshot[111]["ports"] == [8000]
    assert snapshot[222]["ports"] == [8001, 8002], "同一进程的 CIM 端口必须聚合"
    assert prober.supported is True
    assert [cmd[0] for cmd in calls] == ["netstat", "powershell.exe"]


def test_system_prober_cim_failure_keeps_netstat_result(monkeypatch):
    """CIM 命令失败/超时：保留 netstat 结果与 supported，只记录独立降级标记。"""
    monkeypatch.setattr(process_audit, "IS_WIN", True)
    prober = SystemProber()
    calls = []

    def fake_run(argv, *, timeout=None):
        calls.append(argv)
        if argv[0] == "netstat":
            return 0, "  TCP    127.0.0.1:8000    0.0.0.0:0    LISTENING    4242\n"
        return None, ""  # 模拟 Get-NetTCPConnection 超时/不可用

    monkeypatch.setattr(prober, "_run", fake_run)
    snapshot = prober.listening(include_cim=True)

    assert snapshot[4242]["ports"] == [8000], "netstat 结果必须保留"
    assert prober.supported is True, "CIM 失败不能丢掉 netstat 的回收能力"
    assert prober.cim_supported is False, "CIM 失败必须留下独立降级标记"
    assert prober.cim_error, "降级时必须记录可读原因"
    assert any(cmd[0] == "powershell.exe" for cmd in calls)


def test_finish_reaps_netstat_listener_when_cim_raises(tmp_path, monkeypatch):
    """真实 SystemProber：CIM 抛异常/失败时，netstat 发现的新增 owned 进程仍要回收。"""
    monkeypatch.setattr(process_audit, "IS_WIN", True)
    workdir = tmp_path / "task-workdir"
    workdir.mkdir()
    command = f'python -m http.server 8000 --directory "{workdir}"'
    before_rows = "  TCP    127.0.0.1:5000    0.0.0.0:0    LISTENING    111\n"
    after_rows = before_rows + "  TCP    127.0.0.1:8000    0.0.0.0:0    LISTENING    4242\n"
    metadata = json.dumps(
        {"ProcessId": 4242, "CommandLine": command, "ExecutablePath": ""}
    )
    responses = [
        (0, before_rows),   # begin: netstat
        (0, after_rows),    # finish: netstat
        (None, ""),         # finish: Get-NetTCPConnection 失败
        (0, metadata),      # finish: describe 新增 pid
    ]

    def fake_run(argv, *, timeout=None):
        return responses.pop(0)

    prober = SystemProber()
    monkeypatch.setattr(prober, "_run", fake_run)
    killer = MagicMock()

    result = ProcessAudit.begin(prober, killer=killer).finish(workdir)

    assert responses == [], "begin/finish/describe 的调用次数必须符合预期"
    assert result["supported"] is True
    assert result["started"] == [
        {"pid": 4242, "ports": [8000], "command": command, "owned": True}
    ]
    assert [item["pid"] for item in result["reaped"]] == [4242]
    killer.assert_called_once_with(4242)
    assert any("CIM" in warning and "降级" in warning for warning in result["warnings"])
    assert prober.supported is True
    assert prober.cim_supported is False


def test_system_prober_lightweight_snapshot_never_runs_powershell(monkeypatch):
    """begin 路径（include_cim=False）只跑 netstat，不能为 CIM 付成本。"""
    monkeypatch.setattr(process_audit, "IS_WIN", True)
    prober = SystemProber()
    calls = []

    def fake_run(argv, *, timeout=None):
        calls.append(argv)
        return 0, "  TCP    127.0.0.1:8000    0.0.0.0:0    LISTENING    111\n"

    monkeypatch.setattr(prober, "_run", fake_run)
    snapshot = prober.listening()  # 默认 include_cim=False

    assert snapshot[111]["ports"] == [8000]
    assert [cmd[0] for cmd in calls] == ["netstat"]


def test_pid_reuse_with_old_creation_time_is_not_reaped(tmp_path):
    """PID 复用：新快照里 command 命中 workdir，但进程创建时间早于本次调用 → 不杀。"""
    import time as _time

    workdir = tmp_path / "task-workdir"
    workdir.mkdir()
    old = _time.time() - 600
    killer = MagicMock()
    before = {}
    after = {
        777: _proc([8080], command=f'node "{workdir}/server.js"', created_at=old),
    }
    result = ProcessAudit.begin(
        FakeProber(before, after), killer=killer
    ).finish(workdir)
    assert result["reaped"] == []
    assert result["started"][0]["owned"] is False
    killer.assert_not_called()
    assert any("PID 复用" in item for item in result["warnings"])


def test_cim_empty_output_is_success_not_degraded():
    """Get-NetTCPConnection 成功但零监听时输出为空，应视为成功空结果。"""
    prober = SystemProber()
    prober._run = lambda *args, **kwargs: (0, "")  # type: ignore[method-assign]
    ports, error = prober._listening_windows_cim()
    assert ports == {}
    assert error == ""


def test_cim_parser_accepts_single_object_with_bom():
    """PowerShell 单条记录会输出对象而不是数组，且可能带 BOM。"""
    prober = SystemProber()
    payload = "\ufeff" + json.dumps({"LocalPort": 8000, "OwningProcess": 123})
    prober._run = lambda *args, **kwargs: (0, payload)  # type: ignore[method-assign]
    ports, error = prober._listening_windows_cim()
    assert ports == {123: {8000}}
    assert error == ""


def test_cim_parser_filters_invalid_rows_but_keeps_valid_ones():
    prober = SystemProber()
    payload = json.dumps(
        [
            {"LocalPort": 8001, "OwningProcess": 222},
            {"LocalPort": 0, "OwningProcess": 333},
            {"LocalPort": "not-a-port", "OwningProcess": 444},
            {"LocalPort": 8002, "OwningProcess": -1},
            "junk",
            {"LocalPort": 8003, "OwningProcess": 555},
        ]
    )
    prober._run = lambda *args, **kwargs: (0, payload)  # type: ignore[method-assign]
    ports, error = prober._listening_windows_cim()
    assert ports == {222: {8001}, 555: {8003}}
    assert error == ""


def test_cim_parser_invalid_json_reports_degradation():
    prober = SystemProber()
    prober._run = lambda *args, **kwargs: (0, "{not json")  # type: ignore[method-assign]
    ports, error = prober._listening_windows_cim()
    assert ports is None
    assert "CIM JSON" in error


def test_cim_parser_unexpected_top_level_type_reports_degradation():
    prober = SystemProber()
    prober._run = lambda *args, **kwargs: (0, "42")  # type: ignore[method-assign]
    ports, error = prober._listening_windows_cim()
    assert ports is None
    assert "顶层类型" in error


def test_system_prober_empty_cim_output_is_success(monkeypatch):
    """真实 SystemProber：空输出是成功空结果，不能写成降级 warning。"""
    monkeypatch.setattr(process_audit, "IS_WIN", True)
    prober = SystemProber()

    def fake_run(argv, *, timeout=None):
        if argv[0] == "netstat":
            return 0, "  TCP    127.0.0.1:8000    0.0.0.0:0    LISTENING    111\n"
        return 0, ""

    monkeypatch.setattr(prober, "_run", fake_run)
    snapshot = prober.listening(include_cim=True)
    assert snapshot[111]["ports"] == [8000]
    assert prober.cim_supported is True
    assert prober.cim_error == ""


def test_posix_never_calls_windows_cim(monkeypatch):
    """非 Windows 即使 include_cim=True 也不能执行 Get-NetTCPConnection。"""
    monkeypatch.setattr(process_audit, "IS_WIN", False)
    prober = SystemProber()
    monkeypatch.setattr(prober, "_listening_posix", lambda: {111: {8000}})

    def explode(*args, **kwargs):
        raise AssertionError("POSIX 不得调用 Windows CIM")

    monkeypatch.setattr(prober, "_listening_windows_cim", explode)
    snapshot = prober.listening(include_cim=True)
    assert snapshot[111]["ports"] == [8000]
    assert prober.cim_supported is None
