"""领星只读适配器测试：白名单结构性防写、逐工具参数编码、双层/三层信封解析、repr 脱敏。

全部测试不触网络：网络层唯一入口 `_perform_call` 被假体替换并记录调用。
"""

import json
from collections.abc import Mapping
from unittest.mock import patch

import httpx2
import pytest

from ads_control_plane.adapters.lx_read import (
    DEFAULT_CATALOG_VERSION,
    READ_TOOL_ALLOWLIST,
    TOOL_PARAM_SPECS,
    LxBusinessError,
    LxGatewayError,
    LxMcpReadClient,
    LxReadError,
    LxToolNotAllowed,
    encode_params,
    parse_ad_report_envelope,
    parse_auth_shops_envelope,
    parse_erp_envelope,
)

AD_REPORT_OK: dict[str, object] = {
    "code": 0,
    "message": "ok",
    "data": {
        "traceId": "t-1",
        "recordsFiltered": 2,
        "code": 0,
        "data": [{"campaign_id": "c-1", "budget": "10.00"}, {"campaign_id": None}],
    },
}

ERP_OK: dict[str, object] = {
    "code": 0,
    "message": "ok",
    "data": {
        "msg": "success",
        "code": 0,
        "request_id": "r-1",
        "data": {"total": 1, "list": [{"asin": "B0TEST"}]},
    },
}


class RecordingClient(LxMcpReadClient):
    """假体：截获网络层唯一入口 `_perform_call`，记录信封并返回罐装应答。"""

    def __init__(self, payload: Mapping[str, object] | None = None) -> None:
        super().__init__("http://lx.invalid/mcp", "sk-secret-key", min_interval_seconds=0)
        self.calls: list[dict[str, str]] = []
        self._payload: Mapping[str, object] = payload if payload is not None else AD_REPORT_OK

    def _perform_call(self, envelope: Mapping[str, str]) -> Mapping[str, object]:
        self.calls.append(dict(envelope))
        return self._payload


class TestReadOnlyAllowlist:
    """结构性防写锚点：写工具在任何网络调用之前被拒。"""

    def test_put_tool_rejected_before_any_network_call(self) -> None:
        client = RecordingClient()
        with pytest.raises(LxToolNotAllowed) as e:
            client.fetch_page("put_campaigns_sp", {"campaign_id": "c-1"})
        assert e.value.code == "LX_TOOL_NOT_ALLOWED"
        assert client.calls == []  # 未发起任何调用

    def test_post_tool_rejected_before_any_network_call(self) -> None:
        client = RecordingClient()
        with pytest.raises(LxToolNotAllowed):
            client.fetch_page("post_negative_keywords", {})
        assert client.calls == []

    def test_allowlist_is_exactly_the_nine_read_tools(self) -> None:
        expected = frozenset(
            {
                "ad_auth_shops",
                "ad_campaign_report",
                "ad_campaign_group_report",
                # 「广告」层（商品）。2026-08-29 经网关 search 实测 toolType=read 后入表。
                "ad_campaign_product_report",
                "ad_campaign_targeting_report",
                "ad_campaign_keyword_report",
                "ad_campaign_search_term_report",
                # 广告组合层。2026-08-29 同批实测：toolType=read，该店 112 个组合。
                "ad_portfolio_report_shop",
                "erp_listing",
            }
        )
        assert expected == READ_TOOL_ALLOWLIST
        for tool_id in READ_TOOL_ALLOWLIST:
            assert not tool_id.startswith(("put_", "post_"))

    def test_param_specs_cover_exactly_the_allowlist_with_pinned_schema(self) -> None:
        assert frozenset(TOOL_PARAM_SPECS) == READ_TOOL_ALLOWLIST
        for tool_id, spec in TOOL_PARAM_SPECS.items():
            assert spec.schema_version == f"{tool_id}-v1"


class TestEncodeParams:
    """同名入参跨工具类型漂移的逐工具编码（实测合同）。"""

    def test_group_report_with_ring_bool_to_int(self) -> None:
        encoded = encode_params("ad_campaign_group_report", {"with_ring": True})
        assert encoded == {"with_ring": 1}
        assert type(encoded["with_ring"]) is int

    def test_targeting_report_with_ring_and_length_drift(self) -> None:
        encoded = encode_params("ad_campaign_targeting_report", {"with_ring": False, "length": 20})
        assert encoded == {"with_ring": 0, "length": "20"}
        assert type(encoded["length"]) is str

    def test_campaign_report_length_stays_int(self) -> None:
        encoded = encode_params("ad_campaign_report", {"length": 20, "offset": 0})
        assert encoded == {"length": 20, "offset": 0}
        assert type(encoded["length"]) is int

    def test_unlisted_params_pass_through_unchanged(self) -> None:
        params: dict[str, object] = {
            "report_date": "2026-08-20 - 2026-08-27",
            "profile_ids": ["p-1", "p-2"],
        }
        assert encode_params("ad_campaign_group_report", params) == params

    def test_write_tool_rejected_in_pure_function_too(self) -> None:
        with pytest.raises(LxToolNotAllowed) as e:
            encode_params("put_campaigns_sp", {})
        assert e.value.code == "LX_TOOL_NOT_ALLOWED"

    def test_unencodable_value_fails_loudly(self) -> None:
        with pytest.raises(LxReadError) as e:
            encode_params("ad_campaign_targeting_report", {"length": None})
        assert e.value.code == "LX_PARAM_NOT_ENCODABLE"


class TestAdReportEnvelope:
    """广告报表双层信封：rows 在内层 data 数组，total 在 recordsFiltered。"""

    def test_rows_in_inner_data_array(self) -> None:
        page = parse_ad_report_envelope(AD_REPORT_OK)
        assert page["total"] == 2
        rows = page["rows"]
        assert isinstance(rows, list) and len(rows) == 2
        assert rows[0] == {"campaign_id": "c-1", "budget": "10.00"}

    def test_rows_in_inner_dict_list_variant(self) -> None:
        payload: dict[str, object] = {
            "code": 0,
            "data": {"code": 0, "data": {"list": [{"ad_group_id": "g-1"}], "total": 7}},
        }
        page = parse_ad_report_envelope(payload)
        assert page["rows"] == [{"ad_group_id": "g-1"}]
        assert page["total"] == 7

    def test_outer_data_direct_list_variant(self) -> None:
        payload: dict[str, object] = {"code": 0, "data": [{"sid": "s-1"}]}
        page = parse_ad_report_envelope(payload)
        assert page["rows"] == [{"sid": "s-1"}]
        assert page["total"] is None

    def test_outer_gateway_error_carries_details(self) -> None:
        payload: dict[str, object] = {
            "code": 102,
            "message": "invalid params",
            "error_details": [{"pointer": "/with_ring", "reason": "boolean found"}],
        }
        with pytest.raises(LxGatewayError) as e:
            parse_ad_report_envelope(payload)
        assert e.value.code == "LX_GATEWAY_ERROR"
        assert e.value.error_details == [{"pointer": "/with_ring", "reason": "boolean found"}]

    def test_inner_code_one_is_success(self) -> None:
        # 2026-08-28 真实环境实测：报表内层沿用旧 openapi 语义，code=1 为成功。
        payload: dict[str, object] = {
            "code": 0,
            "data": {
                "traceId": "t-8",
                "recordsFiltered": 40,
                "code": 1,
                "data": [{"campaign_id": "c-9", "budget": "5.00"}],
            },
        }
        page = parse_ad_report_envelope(payload)
        assert page["rows"] == [{"campaign_id": "c-9", "budget": "5.00"}]
        assert page["total"] == 40

    def test_inner_success_false_is_business_error(self) -> None:
        payload: dict[str, object] = {
            "code": 0,
            "data": {"traceId": "t-10", "code": 1, "success": False, "data": []},
        }
        with pytest.raises(LxBusinessError):
            parse_ad_report_envelope(payload)

    def test_inner_business_error(self) -> None:
        payload: dict[str, object] = {
            "code": 0,
            "data": {"traceId": "t-9", "code": 500, "data": []},
        }
        with pytest.raises(LxBusinessError) as e:
            parse_ad_report_envelope(payload)
        assert e.value.code == "LX_BUSINESS_ERROR"

    def test_missing_row_array_is_shape_error(self) -> None:
        payload: dict[str, object] = {"code": 0, "data": {"code": 0}}
        with pytest.raises(LxReadError) as e:
            parse_ad_report_envelope(payload)
        assert e.value.code == "LX_ENVELOPE_SHAPE"

    def test_missing_outer_code_is_shape_error(self) -> None:
        with pytest.raises(LxReadError) as e:
            parse_ad_report_envelope({"data": {}})
        assert e.value.code == "LX_ENVELOPE_SHAPE"


class TestErpEnvelope:
    """erp_listing 三层信封：data.data.data={total, list}。"""

    def test_three_layer_unwrap(self) -> None:
        page = parse_erp_envelope(ERP_OK)
        assert page["rows"] == [{"asin": "B0TEST"}]
        assert page["total"] == 1

    def test_middle_layer_business_error(self) -> None:
        payload: dict[str, object] = {
            "code": 0,
            "data": {"msg": "token expired", "code": 2001, "data": None},
        }
        with pytest.raises(LxBusinessError) as e:
            parse_erp_envelope(payload)
        assert e.value.code == "LX_BUSINESS_ERROR"

    def test_outer_gateway_error(self) -> None:
        with pytest.raises(LxGatewayError):
            parse_erp_envelope({"code": 102, "message": "invalid params"})

    def test_missing_inner_list_is_shape_error(self) -> None:
        payload: dict[str, object] = {"code": 0, "data": {"code": 0, "data": {"total": 3}}}
        with pytest.raises(LxReadError) as e:
            parse_erp_envelope(payload)
        assert e.value.code == "LX_ENVELOPE_SHAPE"


class TestTransportErrorClassification:
    """传输层失败必须带码（LX_TRANSPORT_ERROR），不得以裸异常穿透成 500。

    2026-08-28 实测：MCP 客户端跑在 anyio task group 内，ReadTimeout 被包成
    ExceptionGroup 逃逸，工作台同步端点因此返回未分类的 500。
    """

    def _client(self) -> LxMcpReadClient:
        return LxMcpReadClient(url="http://x", key="k", min_interval_seconds=0)

    def test_exception_group_timeout_becomes_transport_error(self) -> None:
        client = self._client()
        boom = BaseExceptionGroup("tg", [httpx2.ReadTimeout("read timed out")])
        with (
            patch.object(LxMcpReadClient, "_call_action", side_effect=boom),
            pytest.raises(LxReadError) as e,
        ):
            client.fetch_page("ad_campaign_report", {"page": 1})
        assert e.value.code == "LX_TRANSPORT_ERROR"
        assert "ReadTimeout" in str(e.value)

    def test_plain_httpx_error_becomes_transport_error(self) -> None:
        client = self._client()
        with (
            patch.object(
                LxMcpReadClient, "_call_action", side_effect=httpx2.ConnectError("refused")
            ),
            pytest.raises(LxReadError) as e,
        ):
            client.fetch_page("ad_campaign_report", {"page": 1})
        assert e.value.code == "LX_TRANSPORT_ERROR"

    def test_classified_error_inside_group_is_preserved(self) -> None:
        """组里若已有分类错误，保留其码而不是笼统归为传输失败。"""
        client = self._client()
        inner = LxGatewayError("gateway said no", error_details=None)
        with (
            patch.object(
                LxMcpReadClient, "_call_action", side_effect=BaseExceptionGroup("tg", [inner])
            ),
            pytest.raises(LxReadError) as e,
        ):
            client.fetch_page("ad_campaign_report", {"page": 1})
        assert e.value.code == "LX_GATEWAY_ERROR"

    def test_timeout_is_configurable_and_validated(self) -> None:
        assert LxMcpReadClient(url="http://x", key="k")._timeout_seconds == 60.0
        with pytest.raises(LxReadError) as e:
            LxMcpReadClient(url="http://x", key="k", timeout_seconds=0)
        assert e.value.code == "LX_CONFIG_INVALID"


class TestAuthShopsEnvelope:
    """ad_auth_shops 内层成功语义与报表族相反：code=1 + success=true（2026-08-28 实测）。"""

    def test_code_one_success_true_is_success(self) -> None:
        payload: dict[str, object] = {
            "code": 0,
            "data": {
                "msg": "success",
                "traceId": "t-1",
                "code": 1,
                "data": [{"profile_id": "p-1", "sid": "s-1", "country": "US"}],
                "success": True,
            },
        }
        page = parse_auth_shops_envelope(payload)
        assert page["rows"] == [{"profile_id": "p-1", "sid": "s-1", "country": "US"}]
        assert page["total"] == 1

    def test_success_false_is_business_error(self) -> None:
        payload: dict[str, object] = {
            "code": 0,
            "data": {"msg": "denied", "traceId": "t-2", "code": 0, "data": [], "success": False},
        }
        with pytest.raises(LxBusinessError) as e:
            parse_auth_shops_envelope(payload)
        assert e.value.code == "LX_BUSINESS_ERROR"

    def test_missing_rows_is_shape_error(self) -> None:
        payload: dict[str, object] = {"code": 0, "data": {"code": 1, "success": True}}
        with pytest.raises(LxReadError) as e:
            parse_auth_shops_envelope(payload)
        assert e.value.code == "LX_ENVELOPE_SHAPE"


class TestClientBehaviour:
    """客户端行为：信封钉扎、按工具族路由、repr 脱敏、节流默认值。"""

    def test_fetch_page_pins_catalog_schema_and_encodes_params(self) -> None:
        client = RecordingClient()
        page = client.fetch_page("ad_campaign_group_report", {"with_ring": True, "length": 20})
        assert page["total"] == 2
        [envelope] = client.calls
        assert set(envelope) == {"toolId", "catalogVersion", "schemaVersion", "paramsJson"}
        assert envelope["toolId"] == "ad_campaign_group_report"
        assert envelope["catalogVersion"] == DEFAULT_CATALOG_VERSION
        assert envelope["schemaVersion"] == "ad_campaign_group_report-v1"
        assert json.loads(envelope["paramsJson"]) == {"with_ring": 1, "length": 20}

    def test_fetch_page_routes_erp_listing_to_three_layer_parser(self) -> None:
        client = RecordingClient(payload=ERP_OK)
        page = client.fetch_page("erp_listing", {"offset": 0, "length": 20, "pvi_ids": ""})
        assert page["rows"] == [{"asin": "B0TEST"}]
        assert page["total"] == 1
        assert client.calls[0]["schemaVersion"] == "erp_listing-v1"

    def test_repr_and_str_never_leak_key(self) -> None:
        client = RecordingClient()
        assert "sk-secret-key" not in repr(client)
        assert "sk-secret-key" not in str(client)
        assert "***" in repr(client)

    def test_default_min_interval_is_conservative(self) -> None:
        client = LxMcpReadClient("http://lx.invalid/mcp", "k")
        assert client._min_interval_seconds == pytest.approx(1.1)

    def test_invalid_construction_fails_loudly(self) -> None:
        with pytest.raises(LxReadError) as e:
            LxMcpReadClient("", "k")
        assert e.value.code == "LX_CONFIG_INVALID"
        with pytest.raises(LxReadError):
            LxMcpReadClient("http://lx.invalid/mcp", " ")
        with pytest.raises(LxReadError):
            LxMcpReadClient("http://lx.invalid/mcp", "k", min_interval_seconds=-1)
