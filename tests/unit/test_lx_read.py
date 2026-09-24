"""领星只读适配器测试：白名单结构性防写、逐工具参数编码、双层/三层信封解析、repr 脱敏。

全部测试不触网络：网络层唯一入口 `_perform_call` 被假体替换并记录调用。
"""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Mapping
from unittest.mock import patch

import httpx2
import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import INTERNAL_ERROR, METHOD_NOT_FOUND

from ads_control_plane.adapters import lx_read
from ads_control_plane.adapters.lx_read import (
    READ_TOOL_ALLOWLIST,
    TOOL_PARAM_SPECS,
    LxBusinessError,
    LxGatewayError,
    LxMcpReadClient,
    LxReadError,
    LxToolNotAllowed,
    classify_mcp_error,
    encode_params,
    parse_ad_report_envelope,
    parse_auth_shops_envelope,
    parse_erp_envelope,
)

#: 外层信封取 2026-09-23 实测的改版后形态：{code: 1, success: true, msg}。
AD_REPORT_OK: dict[str, object] = {
    "code": 1,
    "success": True,
    "msg": "操作成功",
    "data": {
        "traceId": "t-1",
        "recordsFiltered": 2,
        "code": 0,
        "data": [{"campaign_id": "c-1", "budget": "10.00"}, {"campaign_id": None}],
    },
}

ERP_OK: dict[str, object] = {
    "code": 1,
    "success": True,
    "msg": "操作成功",
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
        self.calls: list[dict[str, object]] = []
        self._payload: Mapping[str, object] = payload if payload is not None else AD_REPORT_OK

    def _perform_call(self, envelope: Mapping[str, object]) -> Mapping[str, object]:
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

    def test_param_specs_cover_exactly_the_allowlist(self) -> None:
        assert frozenset(TOOL_PARAM_SPECS) == READ_TOOL_ALLOWLIST


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


class TestGatewayEnvelope:
    """外层网关信封：2026-09-23 实测改版为 {code: 1, success: true, msg}。

    改版前的代码只认 code == 0，于是把「操作成功」读成「网关拒绝：code=1 message=None」
    ——message 是 None，是因为新版把它改名叫 msg。74 家店一家都跑不出来。
    """

    def test_new_shape_code_one_with_success_true_passes(self) -> None:
        assert parse_ad_report_envelope(AD_REPORT_OK)["total"] == 2

    def test_old_shape_code_zero_without_success_still_passes(self) -> None:
        payload: dict[str, object] = {"code": 0, "message": "ok", "data": {"code": 0, "data": []}}
        assert parse_ad_report_envelope(payload)["rows"] == []

    def test_code_one_without_success_true_is_rejected(self) -> None:
        """只凭 code=1 放行，会把哪天出现的「code=1 表示失败」当成成功。"""
        for success in (None, False):
            payload: dict[str, object] = {"code": 1, "success": success, "data": {"data": []}}
            with pytest.raises(LxGatewayError):
                parse_ad_report_envelope(payload)

    def test_rejection_quotes_the_gateway_msg(self) -> None:
        payload: dict[str, object] = {
            "code": 102,
            "success": False,
            "msg": "工具参数定义已更新，请刷新工具列表后重新调用。",
            "data": None,
        }
        with pytest.raises(LxGatewayError) as e:
            parse_ad_report_envelope(payload)
        assert "工具参数定义已更新" in str(e.value)


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


#: SDK 对 HTTP ≥400 且响应体不是 JSON-RPC error 时给的那一句（mcp 2.1.1 / 2.2.0 相同）。
HTTP_REFUSED = MCPError(INTERNAL_ERROR, "Server returned an error response")


class TestGatewayRefusalIsNotANetworkError:
    """错 key、错地址、http 协议、贴成网页地址：网关已经回答了，再问多少次都一样。

    2026-09-24 评审在假网关上复现：这些此前全被归成 LX_TRANSPORT_ERROR，每页白问 3 次，
    README 还让人去查网络；网关原话一个字没留下。
    """

    def test_a_4xx_refusal_is_a_gateway_error(self) -> None:
        for status in (401, 403, 404):
            got = classify_mcp_error(HTTP_REFUSED, [200, 202, status])
            assert got.code == "LX_GATEWAY_ERROR", status

    def test_a_wrong_path_is_a_gateway_error(self) -> None:
        got = classify_mcp_error(MCPError(METHOD_NOT_FOUND, "Not Found"), [404])
        assert got.code == "LX_GATEWAY_ERROR"

    def test_server_trouble_is_worth_asking_again(self) -> None:
        for status in (500, 502, 503, 429, 408):
            assert classify_mcp_error(HTTP_REFUSED, [200, status]).code == ("LX_TRANSPORT_ERROR"), (
                status
            )

    def test_minus_32001_from_the_gateway_is_the_gateway_talking(self) -> None:
        """SDK 拿 -32001 表示本地超时，网关也可能拿它说「key 无效」（评审的假网关就这么回）。
        本地超时我们自己计时（TimeoutError），所以收到的 -32001 只能是网关的回答。"""
        said = MCPError(-32001, "X-Mcp-Key 无效或已过期")
        assert classify_mcp_error(said, [200]).code == "LX_GATEWAY_ERROR"

    def test_the_gateways_words_are_kept(self) -> None:
        said = MCPError(-32600, "Unauthorized: invalid X-Mcp-Key")
        assert "Unauthorized: invalid X-Mcp-Key" in str(classify_mcp_error(said, [401]))

    @staticmethod
    def _through_the_client(
        initialize: object, *, timeout: float = 60.0, key: str = "k"
    ) -> LxReadError:
        """走一遍真的 _call_action，只换掉传输与会话（不触网）。"""

        @contextlib.asynccontextmanager
        async def no_network(url: str, http_client: object) -> AsyncIterator[tuple[None, None]]:
            yield (None, None)

        class Session:
            def __init__(self, read: object, write: object) -> None:
                self.initialize = initialize

            async def __aenter__(self) -> "Session":
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

        client = LxMcpReadClient(
            url="http://x", key=key, min_interval_seconds=0, timeout_seconds=timeout
        )
        with (
            patch.object(lx_read, "streamable_http_client", no_network),
            patch.object(lx_read, "ClientSession", Session),
            pytest.raises(LxReadError) as e,
        ):
            client.fetch_page("ad_campaign_report", {"page": 1})
        return e.value

    def test_the_refusal_keeps_its_class_on_the_way_out_of_the_session(self) -> None:
        async def refuse() -> None:
            raise HTTP_REFUSED

        assert self._through_the_client(refuse).code == "LX_GATEWAY_ERROR"

    def test_the_key_never_leaves_the_client_in_an_error(self) -> None:
        """网关原话跟着错误进日志（search_terms 带上 str(exc)）。网关要是回显请求头，
        key 不能跟着出去——MCP 层的拒绝和信封里的拒绝都一样（2026-09-24 Codex 复审 P2）。"""
        key = "sk-secret-0123456789"

        async def refuse() -> None:
            raise MCPError(-32600, f"Unauthorized: invalid X-Mcp-Key {key}")

        from_mcp = self._through_the_client(refuse, key=key)
        assert "Unauthorized" in str(from_mcp) and key not in str(from_mcp)

        client = LxMcpReadClient(url="http://x", key=key, min_interval_seconds=0)
        refused = {"code": 102, "success": False, "msg": f"bad key {key}", "error_details": [key]}
        with (
            patch.object(LxMcpReadClient, "_perform_call", return_value=refused),
            pytest.raises(LxGatewayError) as e,
        ):
            client.fetch_page("ad_campaign_report", {"page": 1})
        assert "bad key" in str(e.value) and key not in str(e.value)
        assert key not in str(e.value.error_details)

    def test_a_request_that_never_answers_times_out_as_a_network_error(self) -> None:
        """网关只发保活、不给结果：没有每次请求的上限，线程和两把锁会一直占着。"""

        async def hang() -> None:
            await asyncio.sleep(30)

        started = time.monotonic()
        got = self._through_the_client(hang, timeout=0.2)
        assert got.code == "LX_TRANSPORT_ERROR"
        assert time.monotonic() - started < 5


class TestAuthShopsEnvelope:
    """ad_auth_shops 内层成功语义与报表族相反：code=1 + success=true（2026-08-28 实测）。"""

    def test_code_one_success_true_is_success(self) -> None:
        payload: dict[str, object] = {
            "code": 1,
            "success": True,
            "msg": "操作成功",
            "data": {
                "msg": "success",
                "traceId": "t-1",
                "code": 1,
                "data": [{"profile_id": "p-1", "sid": 1, "country": "US"}],
                "success": True,
            },
        }
        page = parse_auth_shops_envelope(payload)
        assert page["rows"] == [{"profile_id": "p-1", "sid": 1, "country": "US"}]
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

    def test_fetch_page_sends_exactly_tool_id_and_params_object(self) -> None:
        """2026-09-23 实测 action 的入参只有 {toolId, params}，params 是对象。

        再带 catalogVersion/schemaVersion，网关回 code=102「工具参数定义已更新」。
        """
        client = RecordingClient()
        page = client.fetch_page("ad_campaign_group_report", {"with_ring": True, "length": 20})
        assert page["total"] == 2
        [envelope] = client.calls
        assert envelope == {
            "toolId": "ad_campaign_group_report",
            "params": {"with_ring": 1, "length": 20},
        }

    def test_fetch_page_routes_erp_listing_to_three_layer_parser(self) -> None:
        client = RecordingClient(payload=ERP_OK)
        page = client.fetch_page("erp_listing", {"offset": 0, "length": 20, "pvi_ids": ""})
        assert page["rows"] == [{"asin": "B0TEST"}]
        assert page["total"] == 1
        assert client.calls[0]["toolId"] == "erp_listing"

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
