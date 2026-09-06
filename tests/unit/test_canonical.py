"""AX-01 / AX-06 公理测试：String ID、Decimal 金额、币种、父链。对应红队 RT-10。"""

from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from ads_control_plane.canonical.entity import (
    AdProduct,
    CanonicalEntityRef,
    EntityType,
    ParentRefs,
    Provider,
)
from ads_control_plane.canonical.ids import ExternalId, new_canonical_id
from ads_control_plane.canonical.money import Money


class _IdHolder(BaseModel):
    value: ExternalId


class TestExternalId:
    def test_preserves_leading_zeros(self) -> None:
        assert _IdHolder(value="00123").value == "00123"

    def test_preserves_very_long_numeric_id(self) -> None:
        long_id = "9" * 200  # 远超 float/int64 精度，字符串必须无损
        assert _IdHolder(value=long_id).value == long_id

    @pytest.mark.parametrize("bad", [123, 1.5, None, ["a"]])
    def test_rejects_non_string(self, bad: object) -> None:
        with pytest.raises(ValidationError, match="string"):
            _IdHolder(value=bad)

    @pytest.mark.parametrize("bad", ["", "  ", " x "])
    def test_rejects_empty_or_padded(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            _IdHolder(value=bad)


class TestMoney:
    def test_accepts_string_and_decimal(self) -> None:
        assert Money(amount="12.01", currency="USD").amount == Decimal("12.01")
        assert Money(amount=Decimal("0.83"), currency="USD").amount == Decimal("0.83")

    def test_rejects_float(self) -> None:
        with pytest.raises(ValidationError, match="float is forbidden"):
            Money(amount=12.01, currency="USD")

    def test_json_serializes_amount_as_string(self) -> None:
        payload = Money(amount="11.99", currency="USD").model_dump_json()
        assert '"11.99"' in payload

    def test_rejects_bad_currency(self) -> None:
        for bad in ("usd", "US", "USDT", ""):
            with pytest.raises(ValidationError):
                Money(amount="1", currency=bad)

    def test_cross_currency_arithmetic_rejected(self) -> None:
        usd = Money(amount="1.00", currency="USD")
        eur = Money(amount="1.00", currency="EUR")
        with pytest.raises(ValueError, match="currency mismatch"):
            _ = usd - eur

    def test_exact_decimal_arithmetic(self) -> None:
        # 0.1+0.2 类的浮点误差不得出现
        a = Money(amount="0.10", currency="USD")
        b = Money(amount="0.20", currency="USD")
        assert (b - a).amount == Decimal("0.10")


def _ref(entity_type: EntityType, parents: ParentRefs | None = None) -> CanonicalEntityRef:
    return CanonicalEntityRef(
        organization_id=new_canonical_id(),
        provider=Provider.MOCK,
        provider_connection_id=new_canonical_id(),
        marketplace="US",
        shop_external_id="shop-001",
        profile_external_id="profile-001",
        ad_product=AdProduct.SP,
        entity_type=entity_type,
        entity_external_id="ent-001",
        parent_refs=parents or ParentRefs(),
    )


class TestCanonicalEntityRef:
    def test_campaign_needs_no_parents(self) -> None:
        assert _ref(EntityType.CAMPAIGN).entity_type is EntityType.CAMPAIGN

    def test_target_requires_full_parent_chain(self) -> None:
        with pytest.raises(ValidationError, match="missing required parent"):
            _ref(EntityType.TARGET)
        with pytest.raises(ValidationError, match="ad_group_external_id"):
            _ref(EntityType.TARGET, ParentRefs(campaign_external_id="c-1"))
        ok = _ref(
            EntityType.TARGET,
            ParentRefs(campaign_external_id="c-1", ad_group_external_id="ag-1"),
        )
        assert ok.parent_refs.ad_group_external_id == "ag-1"

    def test_uniqueness_key_excludes_name_like_fields(self) -> None:
        ok = _ref(EntityType.CAMPAIGN)
        key = ok.uniqueness_key()
        assert "ent-001" in key and len(key) == 5

    def test_frozen(self) -> None:
        with pytest.raises(ValidationError):
            _ref(EntityType.CAMPAIGN).marketplace = "DE"  # type: ignore[misc]
