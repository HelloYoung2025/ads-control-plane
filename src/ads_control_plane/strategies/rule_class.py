"""rule_class 闭集枚举（DEC-105：表达式 DSL 降级为闭集 + 参数包白名单）。

新增 rule_class 需要 Owner 裁决入册后修改本枚举——这是白名单，不是扩展点。
MVP 只实现 NEG_EXACT_CANDIDATE（2026-08-28 业务 Owner 选定的首批策略，DEC-015）。
"""

from __future__ import annotations

from enum import StrEnum


class RuleClass(StrEnum):
    HARVEST = "HARVEST"
    BID_DOWN = "BID_DOWN"
    BID_UP = "BID_UP"
    NEG_EXACT_CANDIDATE = "NEG_EXACT_CANDIDATE"
    BUDGET_FLAG = "BUDGET_FLAG"
