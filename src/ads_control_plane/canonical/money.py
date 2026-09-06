"""金额类型（安全公理 AX-01）：Decimal + ISO-4217 币种，拒绝 float。"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, PlainSerializer

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def _validate_currency(value: str) -> str:
    if not _CURRENCY_RE.match(value):
        raise ValueError(f"currency must be ISO-4217 alpha-3 uppercase, got {value!r}")
    return value


Currency = Annotated[str, AfterValidator(_validate_currency)]


def _coerce_decimal(value: Any) -> Decimal:
    # float 携带二进制舍入误差，金额链路里出现 float 即缺陷。
    if isinstance(value, float):
        raise ValueError("float is forbidden for money amounts; pass Decimal or string")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (str, int)):
        try:
            return Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError(f"not a valid decimal: {value!r}") from exc
    raise ValueError(f"cannot convert {type(value).__name__} to Decimal")


def _validate_amount(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("money amount must be finite")
    return value


MoneyAmount = Annotated[
    Decimal,
    BeforeValidator(_coerce_decimal),
    AfterValidator(_validate_amount),
    PlainSerializer(lambda d: str(d), return_type=str, when_used="json"),
]


class Money(BaseModel):
    """一个带币种的精确金额。跨币种比较/运算必须显式换算，这里直接拒绝。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    amount: MoneyAmount
    currency: Currency

    def require_same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise ValueError(
                f"currency mismatch: {self.currency} vs {other.currency}; "
                "convert explicitly before comparing"
            )

    def __add__(self, other: Money) -> Money:
        self.require_same_currency(other)
        return Money(amount=self.amount + other.amount, currency=self.currency)

    def __sub__(self, other: Money) -> Money:
        self.require_same_currency(other)
        return Money(amount=self.amount - other.amount, currency=self.currency)

    def abs_delta(self, other: Money) -> Money:
        self.require_same_currency(other)
        return Money(amount=abs(self.amount - other.amount), currency=self.currency)
