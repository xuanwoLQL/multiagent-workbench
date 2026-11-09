"""测试用「假 Agent」——不依赖任何真实模型，行为完全由环境变量驱动。"""

from .fake_plan import apply_plan, read_plan

__all__ = ["apply_plan", "read_plan"]
