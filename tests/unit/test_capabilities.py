"""Capability Registry 测试：RT-17（Schema 静默变化停写）、RT-29（新工具默认禁用）。"""

from datetime import UTC, datetime

import pytest

from ads_control_plane.capabilities.registry import (
    CapabilityError,
    CapabilityRegistry,
    CapabilityState,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
KEY = {"provider": "LINGXING", "connection_id": "conn-1", "tool_name": "update_target_bid"}


def promote_to_active(registry: CapabilityRegistry) -> None:
    for _ in range(5):  # DISCOVERED → ... → ACTIVE 共 5 次晋级
        registry.promote(**KEY, verified_by="eng-1")


class TestCapabilityLifecycle:
    def test_rt29_new_tool_starts_discovered_and_write_denied(self) -> None:
        registry = CapabilityRegistry()
        record = registry.observe_discovery(**KEY, schema_hash="h1", observed_at=NOW)
        assert record.state is CapabilityState.DISCOVERED
        with pytest.raises(CapabilityError, match="write denied"):
            registry.assert_write_usable(**KEY, schema_hash="h1")

    def test_explicit_promotion_reaches_active(self) -> None:
        registry = CapabilityRegistry()
        registry.observe_discovery(**KEY, schema_hash="h1", observed_at=NOW)
        promote_to_active(registry)
        registry.assert_write_usable(**KEY, schema_hash="h1")  # 不抛

    def test_rt17_schema_drift_freezes_active_capability(self) -> None:
        registry = CapabilityRegistry()
        registry.observe_discovery(**KEY, schema_hash="h1", observed_at=NOW)
        promote_to_active(registry)
        drifted = registry.observe_discovery(**KEY, schema_hash="h2", observed_at=NOW)
        assert drifted.state is CapabilityState.REVIEW_REQUIRED
        with pytest.raises(CapabilityError, match="write denied"):
            registry.assert_write_usable(**KEY, schema_hash="h2")
        # 冻结后不能直接晋级，必须先人工处理
        with pytest.raises(CapabilityError, match="resolve review"):
            registry.promote(**KEY, verified_by="eng-1")

    def test_call_time_hash_mismatch_denied_even_if_active(self) -> None:
        registry = CapabilityRegistry()
        registry.observe_discovery(**KEY, schema_hash="h1", observed_at=NOW)
        promote_to_active(registry)
        with pytest.raises(CapabilityError, match="hash mismatch"):
            registry.assert_write_usable(**KEY, schema_hash="h-other")

    def test_tool_disappearance_freezes(self) -> None:
        # 实证场景：handoff 快照有 ~15 个广告写工具，当前官方目录为 0
        registry = CapabilityRegistry()
        registry.observe_discovery(**KEY, schema_hash="h1", observed_at=NOW)
        promote_to_active(registry)
        record = registry.observe_disappearance(**KEY, observed_at=NOW)
        assert record.state is CapabilityState.REVIEW_REQUIRED
        assert record.disabled_reason is not None
        assert "disappeared" in record.disabled_reason

    def test_unregistered_capability_write_denied(self) -> None:
        registry = CapabilityRegistry()
        with pytest.raises(CapabilityError, match="never registered"):
            registry.assert_write_usable(**KEY, schema_hash="h1")
