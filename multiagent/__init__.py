"""多 Agent 协作完成任意文件型工作台。

产品定位：把本机已安装的 CLI 型 Agent（Codex / Claude Code / OpenCode / DSH 及任意其他）
组织成一支可被调度的执行单元，在同一个工作区上协作完成**任意文件型工作**
（编码、写文档、改报告、做调研、整理数据、审阅稿件……）。

设计原则（对应 需求.txt）：
- 薄适配器：不修改 Agent 本体，只通过 CLI / ACP 协议驱动（需求 4）。
- 只信证据：不采信 Agent 的自我声明，只以退出码、产物存在性、验收条件判定成败（需求 42）。
- 配置驱动：Agent 清单、任务模板、路由规则、验收规则全部落在 JSON 配置里（需求 51）。
"""

from __future__ import annotations

__version__ = "2.0.0"
__all__ = ["__version__"]
