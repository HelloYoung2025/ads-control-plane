"""退出守卫（ExitGuard）——策略包的止损/达标退出判定（DEC-125）。

2026-08-28 Owner 裁决默认：退出触发 = 调用方把策略包暂停（suspend）、回到人，
绝不自动撤销授权书——撤销是人的意思表示（mandate.revoke 的合同不变）。
本模块只做判定不做动作：调用方拿到 verdict 后自行 suspend 并记审计。
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator


class ExitVerdict(StrEnum):
    NONE = "NONE"
    STOP_LOSS_TRIGGERED = "STOP_LOSS_TRIGGERED"
    TARGET_REACHED = "TARGET_REACHED"


class ExitPolicy(BaseModel):
    """退出策略：止损上限 / 达标线，均可省略；全 None = 只靠授权书到期退出。

    target_wasted_spend_removed 对应当前唯一数据地基就绪的目标函数
    （WASTED_SPEND_REMOVED，DEC-116）；其余目标就绪后再扩字段，不隐式继承。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 累计花费止损上限（>0）；达到（含等于）即触发 STOP_LOSS_TRIGGERED。
    max_cumulative_spend: Decimal | None = None
    #: 已核验的无效花费削减达标线（>0）；达到（含等于）即触发 TARGET_REACHED。
    target_wasted_spend_removed: Decimal | None = None

    @model_validator(mode="after")
    def _positive(self) -> ExitPolicy:
        if self.max_cumulative_spend is not None and self.max_cumulative_spend <= 0:
            raise ValueError("max_cumulative_spend must be positive")
        if self.target_wasted_spend_removed is not None and self.target_wasted_spend_removed <= 0:
            raise ValueError("target_wasted_spend_removed must be positive")
        return self


class ExitGuard:
    """无状态判定器。双阈值同时命中时止损优先——更保守的一侧先回到人。"""

    @staticmethod
    def evaluate(
        policy: ExitPolicy,
        cumulative_spend: Decimal,
        verified_wasted_spend_removed: Decimal,
    ) -> ExitVerdict:
        """边界语义：等于阈值即触发（>=）。verdict 不携带动作，动作由调用方执行。"""
        if (
            policy.max_cumulative_spend is not None
            and cumulative_spend >= policy.max_cumulative_spend
        ):
            return ExitVerdict.STOP_LOSS_TRIGGERED
        if (
            policy.target_wasted_spend_removed is not None
            and verified_wasted_spend_removed >= policy.target_wasted_spend_removed
        ):
            return ExitVerdict.TARGET_REACHED
        return ExitVerdict.NONE
