"""Settings 页「交付与合并」开关的静态契约 + policy git 字段后端契约。

背景：``policy.json`` 的 ``git.auto_merge`` 默认开启，验收通过会自动
``git merge --no-ff`` 回基线。设置页此前只有裸 JSON 编辑器，这里守住：

1. 前端必须暴露四个 git 开关/数值，并把风险文案写清楚；
2. 后端 ``GET/PUT /api/config/files/policy.json`` 必须能保存这四个字段，
   且不能丢掉 ``concurrency`` 等其它策略字段；
3. 后端当前是否拒绝 ``git`` 字段的非法类型不假设（见用例内分支），
   前端读取时自行做布尔兼容，保存时写回规范布尔值。
"""

from __future__ import annotations

import copy
from pathlib import Path

from fastapi.testclient import TestClient

from multiagent.api.app import create_api

REPO = Path(__file__).resolve().parents[1]
SETTINGS_TSX = REPO / "webapp" / "src" / "features" / "settings" / "SettingsView.tsx"

GIT_KEYS = (
    "auto_merge",
    "require_clean_tree",
    "keep_worktree",
    "worktree_retention_days",
)

RISK_COPY = (
    "验收通过即",
    "手动合并",
    "阻止合并",
)


def _client(wb) -> TestClient:
    return TestClient(create_api(wb))


def _read_policy(client: TestClient) -> dict:
    response = client.get("/api/config/files/policy.json")
    assert response.status_code == 200, response.text
    content = response.json()["content"]
    assert isinstance(content, dict), f"policy.json 内容不是对象：{content!r}"
    return content


def test_settings_view_exposes_git_merge_controls_and_risk_copy():
    text = SETTINGS_TSX.read_text(encoding="utf-8")

    for key in GIT_KEYS:
        assert key in text, f"SettingsView.tsx 缺少 git.{key} 控件"
    for phrase in RISK_COPY:
        assert phrase in text, f"SettingsView.tsx 缺少风险文案关键词：{phrase}"
    assert "仅供 ops.prune" in text, "保留天数必须说明仅供 ops.prune 使用"
    assert "putPolicy" in text, "保存必须复用 putPolicy"
    assert "交付策略已保存" in text, "成功提示必须是「交付策略已保存」"
    assert "...policy" in text and "...git" in text, (
        "保存时必须展开原 policy/git，避免丢掉其它策略字段"
    )
    # 交付区块必须挂在 JSON 编辑器之前（高级入口保留在后）。
    assert text.index("<MergeDeliverySettings") < text.index("<JsonEditor"), (
        "「交付与合并」区块必须在 JsonEditor 之前"
    )


def test_policy_git_fields_roundtrip_preserves_other_policy(wb):
    client = _client(wb)
    # wb fixture 使用 pytest tmp_path 下的 home/config，不触碰仓库真实配置。
    assert wb.config.config_dir == wb.config.home / "config"

    original = _read_policy(client)
    payload = copy.deepcopy(original)
    git = payload.get("git")
    if not isinstance(git, dict):
        git = {}
    git.update(
        {
            "author_name": "Keep Me",
            "auto_merge": False,
            "require_clean_tree": False,
            "keep_worktree": False,
            "worktree_retention_days": 30,
        }
    )
    payload["git"] = git

    response = client.put(
        "/api/config/files/policy.json", json={"content": payload}
    )
    assert response.status_code == 200, response.text
    assert response.json()["saved"] == "policy.json"

    saved = _read_policy(client)
    assert saved["git"]["auto_merge"] is False
    assert saved["git"]["require_clean_tree"] is False
    assert saved["git"]["keep_worktree"] is False
    assert saved["git"]["worktree_retention_days"] == 30
    assert saved["git"]["author_name"] == "Keep Me", "git 下的其它字段不能被丢掉"

    # 前端构造 next 时使用 {...policy, git: {...policy.git, ...}}；后端同样必须保留
    # 并发等其它顶层策略，否则保存交付开关会顺带清空调度配置。
    assert saved["concurrency"] == original.get("concurrency")
    assert saved.get("default_timeout_sec") == original.get("default_timeout_sec")
    assert saved.get("context_budget_chars") == original.get("context_budget_chars")
    assert saved.get("isolation") == original.get("isolation")


def test_policy_git_invalid_boolean_type_is_handled_or_reported(wb):
    """后端若拒绝非法类型则断言 400；若接受则记录实际行为，不做额外假设。

    当前 ``validate_config`` 不校验 ``git.*`` 的类型，因此后端通常会接受字符串；
    前端读取时会把 ``"false"`` 兼容成关闭，保存时写回规范布尔值。
    """
    client = _client(wb)
    original = _read_policy(client)

    payload = copy.deepcopy(original)
    git = payload.get("git")
    if not isinstance(git, dict):
        git = {}
    git["auto_merge"] = "false"
    payload["git"] = git

    response = client.put(
        "/api/config/files/policy.json", json={"content": payload}
    )
    assert response.status_code in (200, 400), response.text

    if response.status_code == 400:
        detail = response.json().get("detail", "")
        assert "校验" in detail, f"400 响应应说明校验失败：{detail!r}"
        # 校验失败不得落盘半个配置。
        assert _read_policy(client) == original
    else:
        # 后端当前接受任意 git 字段类型；这是已知放宽，前端必须规范化后再保存。
        saved = _read_policy(client)
        assert saved["git"]["auto_merge"] == "false"


# --------------------------------------------------------------------------- #
# 默认策略：先审后合（auto_merge=false）
# --------------------------------------------------------------------------- #


def test_settings_view_defaults_auto_merge_to_review_then_merge():
    """前端缺省必须与产品默认一致：auto_merge 缺省 false，其余两个开关仍 true。

    旧实现 ``boolValue(git.auto_merge, true)`` 会在字段缺失时回退成自动合并，
    与后端 ``policy_get("git.auto_merge", False)`` 的默认语义相反；这里锁定
    「先审后合（默认）」的缺省、模式文案与下游影响说明。
    """
    text = SETTINGS_TSX.read_text(encoding="utf-8")

    assert "boolValue(git.auto_merge, false)" in text, (
        "auto_merge 读取缺省必须是 false（先审后合），不能回退成自动合并"
    )
    assert "boolValue(git.require_clean_tree, true)" in text, (
        "require_clean_tree 缺省仍应保持 true"
    )
    assert "boolValue(git.keep_worktree, true)" in text, (
        "keep_worktree 缺省仍应保持 true"
    )

    for phrase in ("先审后合", "默认", "DAG", "下游"):
        assert phrase in text, f"SettingsView.tsx 缺少默认策略说明关键词：{phrase}"
    assert "先审后合（默认）" in text, "关闭态 Badge 必须写明「先审后合（默认）」"
    assert "自动合并" in text, "开启态 Badge 必须写明「自动合并」"
    assert "验收通过只提交任务分支，不合并到基线" in text, (
        "关闭态必须说明验收通过只提交任务分支、不合并基线"
    )
    assert "下游依赖任务需先人工合并才能看到产出" in text, (
        "关闭态必须说明下游任务的影响"
    )
    assert "适合受控的 DAG 连续交付" in text, (
        "开启态必须说明自动合并适用于受控的 DAG 连续交付"
    )
    assert "产品默认：先审后合（auto_merge=false）" in text, (
        "必须显式写出产品默认策略"
    )
    assert "DAG 连续交付可显式开启 auto_merge" in text, (
        "必须说明 DAG 连续交付的显式开启路径"
    )


def test_settings_view_save_payload_still_only_writes_git_fields():
    """保存仍然只改 git 四个字段，其它顶层策略原样展开保留。"""
    text = SETTINGS_TSX.read_text(encoding="utf-8")
    start = text.index("const next: Record<string, unknown> = {")
    end = text.index("await putPolicy(fileName, next);", start)
    block = text[start:end]

    for assignment in (
        "auto_merge: autoMerge",
        "require_clean_tree: requireCleanTree",
        "keep_worktree: keepWorktree",
        "worktree_retention_days: parsedRetention",
    ):
        assert assignment in block, f"保存 payload 缺少 git 字段：{assignment}"
    assert "...policy" in block, "保存必须展开原 policy，避免丢掉其它策略"
    assert "...git" in block, "保存必须展开原 git，避免丢掉 author 等字段"
    assert "git: {" in block, "保存必须收敛到 git 字段"
    for key in ("concurrency:", "default_timeout_sec:", "isolation:", "kinds:"):
        assert key not in block, f"保存不得在 payload 里改写其它策略：{key}"


def test_policy_git_auto_merge_defaults_to_false_and_roundtrips(wb):
    """wb 临时 home：policy 缺省 auto_merge 等价 false；PUT true/false 往返正常。"""
    client = _client(wb)
    assert wb.config.config_dir == wb.config.home / "config"

    original = _read_policy(client)
    git = original.get("git")
    if not isinstance(git, dict):
        git = {}
    assert git.get("auto_merge", False) is False, (
        "临时工作台的 policy 不得默认开启自动合并（缺省按 false 解释）"
    )
    assert wb.config.policy_get("git.auto_merge", False) is False, (
        "后端有效策略的 auto_merge 缺省值必须是 false"
    )

    for value in (True, False):
        payload = copy.deepcopy(original)
        git_block = payload.get("git")
        if not isinstance(git_block, dict):
            git_block = {}
        git_block["auto_merge"] = value
        payload["git"] = git_block
        response = client.put(
            "/api/config/files/policy.json", json={"content": payload}
        )
        assert response.status_code == 200, response.text
        saved = _read_policy(client)
        assert saved["git"]["auto_merge"] is value, (
            f"auto_merge 往返后应为 {value}，实际 {saved['git']['auto_merge']!r}"
        )
        assert saved["concurrency"] == original.get("concurrency"), (
            "切换 auto_merge 不得改动 concurrency 等其它策略"
        )
        original = saved


def test_shipped_policy_defaults_to_review_then_merge():
    """仓库内置 policy.json 与 presets.DEFAULT_POLICY 都必须默认先审后合。"""
    import json as _json

    from multiagent.presets import DEFAULT_POLICY

    shipped = _json.loads((REPO / "config" / "policy.json").read_text(encoding="utf-8"))
    assert shipped["git"]["auto_merge"] is False, (
        "config/policy.json 的默认交付策略必须是 auto_merge=false（先审后合）"
    )
    assert DEFAULT_POLICY["git"]["auto_merge"] is False, (
        "presets.DEFAULT_POLICY 的默认交付策略必须是 auto_merge=false（先审后合）"
    )
