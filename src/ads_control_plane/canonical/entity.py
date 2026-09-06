"""CanonicalEntityRef：带完整父链的规范实体引用（安全公理 AX-06）。

写入定位只认 ID 与父链，名称永不作为定位依据。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from ads_control_plane.canonical.ids import CanonicalId, ExternalId


class Provider(StrEnum):
    LINGXING = "LINGXING"
    MOCK = "MOCK"


class AdProduct(StrEnum):
    SP = "SP"
    SB = "SB"
    SD = "SD"


class EntityType(StrEnum):
    CAMPAIGN = "CAMPAIGN"
    AD_GROUP = "AD_GROUP"
    TARGET = "TARGET"
    KEYWORD = "KEYWORD"
    PRODUCT_AD = "PRODUCT_AD"


# 每种实体类型必须携带的父级（缺失即拒绝，不得按名称或单 ID 猜测归属）。
_REQUIRED_PARENTS: dict[EntityType, tuple[str, ...]] = {
    EntityType.CAMPAIGN: (),
    EntityType.AD_GROUP: ("campaign_external_id",),
    EntityType.TARGET: ("campaign_external_id", "ad_group_external_id"),
    EntityType.KEYWORD: ("campaign_external_id", "ad_group_external_id"),
    EntityType.PRODUCT_AD: ("campaign_external_id", "ad_group_external_id"),
}


class ParentRefs(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_external_id: ExternalId | None = None
    ad_group_external_id: ExternalId | None = None


class CanonicalEntityRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    organization_id: CanonicalId
    provider: Provider
    provider_connection_id: CanonicalId
    marketplace: str
    shop_external_id: ExternalId
    profile_external_id: ExternalId
    ad_product: AdProduct
    entity_type: EntityType
    entity_external_id: ExternalId
    parent_refs: ParentRefs = ParentRefs()

    @model_validator(mode="after")
    def _require_parent_chain(self) -> CanonicalEntityRef:
        missing = [
            field
            for field in _REQUIRED_PARENTS[self.entity_type]
            if getattr(self.parent_refs, field) is None
        ]
        if missing:
            raise ValueError(
                f"{self.entity_type} ref missing required parent(s): {', '.join(missing)}"
            )
        return self

    def uniqueness_key(self) -> tuple[str, ...]:
        """生产唯一性至少为 provider+connection+profile+entity_type+entity_external_id。"""
        return (
            self.provider.value,
            str(self.provider_connection_id),
            self.profile_external_id,
            self.entity_type.value,
            self.entity_external_id,
        )
