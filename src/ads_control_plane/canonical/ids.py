"""Canonical 与 External 标识符类型（安全公理 AX-01）。

- External ID：Provider 原生标识，永远是字符串，保留前导零，不做数值转换。
- Canonical ID：平台内部 UUID，普通客户端只使用它。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from pydantic import AfterValidator, BeforeValidator, Field

_EXTERNAL_ID_MAX_LEN = 256


def _reject_non_string(value: Any) -> Any:
    # int/float 形式的外部 ID 会丢前导零、大数截断（RT-10），必须显式拒绝。
    if not isinstance(value, str):
        raise ValueError(
            f"external id must be a string, got {type(value).__name__}; "
            "numeric ids lose leading zeros and precision"
        )
    return value


def _validate_external_id(value: str) -> str:
    if not value or not value.strip():
        raise ValueError("external id must be non-empty")
    if len(value) > _EXTERNAL_ID_MAX_LEN:
        raise ValueError(f"external id longer than {_EXTERNAL_ID_MAX_LEN} chars")
    if value != value.strip():
        raise ValueError("external id must not contain surrounding whitespace")
    return value


ExternalId = Annotated[
    str,
    BeforeValidator(_reject_non_string),
    AfterValidator(_validate_external_id),
]

CanonicalId = Annotated[uuid.UUID, Field(description="platform-internal opaque UUID")]


def new_canonical_id() -> uuid.UUID:
    return uuid.uuid4()
