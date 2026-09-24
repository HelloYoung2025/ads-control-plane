"""领星网关只读适配器——结构性防写客户端（实测依据 docs/evidence/lx-v3-feasibility-20260828.md）。

设计要点（均为 2026-08-28 实测事实的直接编码，不是偏好）：

1. **只读白名单是结构性防写**：`READ_TOOL_ALLOWLIST` 之外的 toolId 在任何网络调用
   发生之前即被拒（`LX_TOOL_NOT_ALLOWED`）——写工具（put_*/post_*）永远出不了这个客户端。
2. **参数编码逐工具钉扎**：同名入参跨工具类型漂移（group 报表 with_ring 要 int、
   targeting 报表 with_ring 要 number、length 要 str），禁止共享参数序列化逻辑，
   编码表见 `TOOL_PARAM_SPECS`，在交给网关之前应用。
3. **信封按工具族区分深度**：广告报表为双层信封（外层网关 code/msg/data，
   内层 {traceId, recordsFiltered, code, data:[rows]}）；erp_listing 为三层信封
   （data.data.data={total, list}）。解析拆为纯函数，测试不触网络。
4. **QPS 保守**：每次网关调用后强制间隔（默认 1.1 秒），测试可传 0。

返回形态 {"rows": list, "total": int | None} 结构性吻合 mirror/sync.py 的 LxReadPort，
但本模块不 import mirror——依赖方向由编排层决定。凭据由调用方传入：类内不读环境
变量、不打印 key、repr 脱敏。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, CallToolResult

#: 网关三元工具（help/search/action）中唯一被本客户端调用的执行入口。
#: 2026-09-23 实测它的入参只有 {toolId, params}，params 是对象。改版前是
#: {toolId, catalogVersion, schemaVersion, paramsJson}，带着旧版本号调用，网关回
#: code=102「工具参数定义已更新，请刷新工具列表后重新调用」——一家店都取不到。
ACTION_TOOL_NAME = "action"

#: erp_listing 走三层信封，与广告报表族区分。
ERP_LISTING_TOOL_ID = "erp_listing"

#: ad_auth_shops 内层沿用旧 openapi 成功语义（code=1 + success=true），单列解析。
AUTH_SHOPS_TOOL_ID = "ad_auth_shops"

#: 每次网关调用后的默认最小间隔（秒）——QPS=1 的保守实现。
DEFAULT_MIN_INTERVAL_SECONDS = 1.1

#: 单次网关调用的 HTTP 超时。httpx 默认 5 秒对领星报表偏短——2026-08-28 实测
#: 大页报表拉取会 ReadTimeout；60 秒是实测可用值。
DEFAULT_TIMEOUT_SECONDS = 60.0

LX_TOOL_NOT_ALLOWED = "LX_TOOL_NOT_ALLOWED"
LX_GATEWAY_ERROR = "LX_GATEWAY_ERROR"
LX_BUSINESS_ERROR = "LX_BUSINESS_ERROR"
LX_ENVELOPE_SHAPE = "LX_ENVELOPE_SHAPE"
LX_PARAM_NOT_ENCODABLE = "LX_PARAM_NOT_ENCODABLE"
LX_CONFIG_INVALID = "LX_CONFIG_INVALID"
LX_TRANSPORT_ERROR = "LX_TRANSPORT_ERROR"


class LxReadError(Exception):
    """领星只读通道异常基类——code 为 SCREAMING_SNAKE 常量，供上层分类处置。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LxToolNotAllowed(LxReadError):
    """toolId 不在只读白名单内——在任何网络调用之前抛出。"""

    def __init__(self, message: str) -> None:
        super().__init__(LX_TOOL_NOT_ALLOWED, message)


class LxTransportError(LxReadError):
    """网络传输层失败（连接/读取超时、TLS、DNS）——与网关业务错误区分开。

    分类的意义：传输失败是"没问到"，业务错误是"问到了但被拒"。前者可重试、
    后者重试无用；不分类会让调用方把超时当成数据缺失。
    """

    def __init__(self, message: str) -> None:
        super().__init__(LX_TRANSPORT_ERROR, message)


class LxGatewayError(LxReadError):
    """外层网关信封判为失败（如 code=102 参数不合法）；error_details 原样携带。"""

    def __init__(self, message: str, *, error_details: object = None) -> None:
        super().__init__(LX_GATEWAY_ERROR, message)
        self.error_details = error_details


class LxBusinessError(LxReadError):
    """内层业务信封 code!=0（网关通过但业务侧拒绝/失败）。"""

    def __init__(self, message: str) -> None:
        super().__init__(LX_BUSINESS_ERROR, message)


#: 只读白名单：实测过的读工具全集。写工具（put_*/post_*）不在此集合，
#: 因而结构上不存在从本客户端发出写调用的路径。
READ_TOOL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "ad_auth_shops",
        "ad_campaign_report",
        "ad_campaign_group_report",
        "ad_campaign_targeting_report",
        "ad_campaign_keyword_report",
        "ad_campaign_product_report",
        "ad_campaign_search_term_report",
        "ad_portfolio_report_shop",
        "erp_listing",
    }
)


def _encode_int(param: str, value: object) -> int:
    """漂移编码：布尔/数字字符串 → int（JSON number，兼容 integer 与 number 两种网关要求）。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 10)
        except ValueError as exc:
            raise LxReadError(
                LX_PARAM_NOT_ENCODABLE, f"param {param!r}: {value!r} is not an integer"
            ) from exc
    raise LxReadError(
        LX_PARAM_NOT_ENCODABLE,
        f"param {param!r}: cannot encode {type(value).__name__} as int",
    )


def _encode_str(param: str, value: object) -> str:
    """漂移编码：整数计数 → str（targeting 报表 length 要 string）。"""
    if isinstance(value, bool):
        raise LxReadError(
            LX_PARAM_NOT_ENCODABLE, f"param {param!r}: bool is not a string-typed count"
        )
    if isinstance(value, int | str):
        return str(value)
    raise LxReadError(
        LX_PARAM_NOT_ENCODABLE,
        f"param {param!r}: cannot encode {type(value).__name__} as str",
    )


@dataclass(frozen=True)
class ToolParamSpec:
    """单工具网关合同钉扎：同名入参的逐工具类型编码器。"""

    param_encoders: Mapping[str, Callable[[str, object], object]]


_NO_ENCODING: Mapping[str, Callable[[str, object], object]] = {}

#: 逐工具参数编码表——漂移实测：group 报表 with_ring→int、targeting 报表
#: with_ring→int（number 兼容）且 length→str，其余原样透传。
TOOL_PARAM_SPECS: dict[str, ToolParamSpec] = {
    "ad_auth_shops": ToolParamSpec(param_encoders=_NO_ENCODING),
    "ad_campaign_report": ToolParamSpec(param_encoders=_NO_ENCODING),
    "ad_campaign_group_report": ToolParamSpec(
        param_encoders={"with_ring": _encode_int},
    ),
    "ad_campaign_targeting_report": ToolParamSpec(
        param_encoders={"with_ring": _encode_int, "length": _encode_str},
    ),
    "ad_campaign_keyword_report": ToolParamSpec(param_encoders=_NO_ENCODING),
    # 「广告」层（领星六层模型的第 4 层：投放在广告组里的具体商品）。2026-08-29 经
    # 网关 search 实测：toolType=read，
    # required=[report_date, profile_id]——注意两处漂移：一是 profile_id 单数（其余
    # 报表族用 profile_ids 复数数组），二是它要 JSON number（"主店铺Profile ID",
    # type=integer），传字符串网关回 code=102 参数不合法。逐工具钉扎，不共享。
    "ad_campaign_product_report": ToolParamSpec(
        param_encoders={"with_ring": _encode_int, "profile_id": _encode_int},
    ),
    "ad_campaign_search_term_report": ToolParamSpec(param_encoders=_NO_ENCODING),
    # 广告组合层（领星六层模型的第 1 层）。2026-08-29 经网关 search 实测：toolType=read，
    # required=[report_date, profile_ids, page, length, sort_field, sort_type]。
    "ad_portfolio_report_shop": ToolParamSpec(param_encoders=_NO_ENCODING),
    "erp_listing": ToolParamSpec(param_encoders=_NO_ENCODING),
}

# 导入期自检：编码表必须恰好覆盖白名单——两表漂移即为缺陷，当场失败。
if frozenset(TOOL_PARAM_SPECS) != READ_TOOL_ALLOWLIST:
    raise LxReadError(LX_CONFIG_INVALID, "TOOL_PARAM_SPECS must cover exactly READ_TOOL_ALLOWLIST")


def encode_params(tool_id: str, params: Mapping[str, object]) -> dict[str, object]:
    """按工具钉扎编码入参。白名单外工具在纯函数层同样被拒——同一道闸的第二次上锁。"""
    spec = TOOL_PARAM_SPECS.get(tool_id)
    if spec is None:
        raise LxToolNotAllowed(
            f"tool {tool_id!r} is not in the read-only allowlist; nothing was sent"
        )
    encoded: dict[str, object] = {}
    for name, value in params.items():
        encoder = spec.param_encoders.get(name)
        encoded[name] = encoder(name, value) if encoder is not None else value
    return encoded


def _require_mapping(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise LxReadError(LX_ENVELOPE_SHAPE, f"{where} is not a JSON object")
    return value


def _envelope_code(source: Mapping[str, object], where: str) -> int:
    """信封 code 读取：int 或数字字符串；缺失/非数字即形态错误（fail loudly）。"""
    value = source.get("code")
    if isinstance(value, bool):
        raise LxReadError(LX_ENVELOPE_SHAPE, f"{where} code must be an integer, got bool")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 10)
        except ValueError as exc:
            raise LxReadError(LX_ENVELOPE_SHAPE, f"{where} code is not numeric: {value!r}") from exc
    raise LxReadError(LX_ENVELOPE_SHAPE, f"{where} carries no code")


def _optional_envelope_code(source: Mapping[str, object], where: str) -> int:
    """内层 code 允许缺失（缺失视同成功 0）；存在则按严格规则读取。"""
    if source.get("code") is None:
        return 0
    return _envelope_code(source, where)


def _optional_int(value: object) -> int | None:
    """total 类计数的宽松读取：int 或数字字符串，其余为 None（端口允许 total 缺失）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 10)
        except ValueError:
            return None
    return None


def _first_int(source: Mapping[str, object], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = _optional_int(source.get(key))
        if value is not None:
            return value
    return None


def _check_gateway_envelope(payload: Mapping[str, object]) -> None:
    """外层网关信封的成功判定，三个工具族共用。

    2026-09-23 实测：网关改版后外层换成旧 openapi 语义——成功是
    {code: 1, success: true, msg: "操作成功"}，失败如 {code: 102, success: false, msg}；
    改版前成功是 {code: 0, message}。两种都认，但 code=1 必须同时带 success=true：
    只凭 code 放行，哪天冒出一个「code=1 表示失败」的信封就会被当成成功。
    报错时带上网关原话（新版在 msg，旧版在 message），日志里才看得出为什么被拒。
    """
    code = _envelope_code(payload, "gateway envelope")
    success = payload.get("success")
    if (success is True and code in (0, 1)) or (success is None and code == 0):
        return
    said = payload.get("message") if payload.get("message") is not None else payload.get("msg")
    raise LxGatewayError(
        f"gateway rejected the call: code={code} success={success!r} message={said!r}",
        error_details=payload.get("error_details"),
    )


def parse_ad_report_envelope(payload: Mapping[str, object]) -> dict[str, object]:
    """解析广告报表族双层信封 → {"rows": list, "total": int | None}。

    实测主形态：外层 {code, message, data}，内层 {traceId, recordsFiltered, code,
    data:[rows]}——rows 在内层 data 数组，total 在 recordsFiltered。兼容两个变体：
    内层 data 为 dict 含 "list" 键；外层 data 直接就是行数组（ad_auth_shops 类清单）。
    """
    _check_gateway_envelope(payload)
    inner_raw = payload.get("data")
    if isinstance(inner_raw, list):
        return {"rows": list(inner_raw), "total": None}
    inner = _require_mapping(inner_raw, "ad report inner envelope")
    inner_code = _optional_envelope_code(inner, "ad report inner envelope")
    # 2026-08-28 真实环境实测：报表内层沿用旧 openapi 成功语义 code=1（与
    # ad_auth_shops 相同）；0 一并放行（code 缺失视同 0，防语义再迁移误伤）。
    # success 字段若存在则优先（当前报表内层未见该字段，防御性判定）。
    success = inner.get("success")
    if success is False or (success is not True and inner_code not in (0, 1)):
        raise LxBusinessError(
            f"ad report business error: code={inner_code} traceId={inner.get('traceId')!r}"
        )
    rows = _extract_rows(inner)
    total = _first_int(inner, ("recordsFiltered", "total"))
    data_field = inner.get("data")
    if total is None and isinstance(data_field, Mapping):
        total = _first_int(data_field, ("recordsFiltered", "total"))
    return {"rows": rows, "total": total}


def _extract_rows(inner: Mapping[str, object]) -> list[object]:
    """行数组提取：inner["data"] 为数组（实测主形态）→ dict 含 "list" → inner["list"]。"""
    candidate = inner.get("data")
    if isinstance(candidate, list):
        return list(candidate)
    if isinstance(candidate, Mapping):
        nested = candidate.get("list")
        if isinstance(nested, list):
            return list(nested)
    direct = inner.get("list")
    if isinstance(direct, list):
        return list(direct)
    raise LxReadError(LX_ENVELOPE_SHAPE, "ad report envelope carries no row array (data/list)")


def parse_auth_shops_envelope(payload: Mapping[str, object]) -> dict[str, object]:
    """解析 ad_auth_shops 双层信封 → {"rows", "total"}。

    内层 {msg, traceId, code, data:[shops], success} 的成功语义与广告报表相反：
    code=1 且 success=true 为成功（旧 openapi 遗留，2026-08-28 实测）——不能复用
    报表族的 code==0 判定，否则把成功当业务错误。
    """
    _check_gateway_envelope(payload)
    inner = _require_mapping(payload.get("data"), "auth shops inner envelope")
    success = inner.get("success")
    inner_code = _optional_envelope_code(inner, "auth shops inner envelope")
    if success is not True and inner_code != 1:
        raise LxBusinessError(
            f"auth shops business error: code={inner_code} success={success!r} "
            f"traceId={inner.get('traceId')!r}"
        )
    rows = inner.get("data")
    if not isinstance(rows, list):
        raise LxReadError(LX_ENVELOPE_SHAPE, "auth shops inner envelope carries no data array")
    return {"rows": list(rows), "total": len(rows)}


def parse_erp_envelope(payload: Mapping[str, object]) -> dict[str, object]:
    """解析 erp_listing 三层信封（data.data.data={total, list}）→ {"rows", "total"}。

    外层网关 {code, message, data} → 中层 open api {msg, code, data, request_id}
    → 内层 {total, list}。中层 code!=0 为业务错误。
    """
    _check_gateway_envelope(payload)
    middle = _require_mapping(payload.get("data"), "erp middle envelope")
    middle_code = _optional_envelope_code(middle, "erp middle envelope")
    if middle_code != 0:
        raise LxBusinessError(f"erp business error: code={middle_code} msg={middle.get('msg')!r}")
    inner = _require_mapping(middle.get("data"), "erp inner envelope")
    rows = inner.get("list")
    if not isinstance(rows, list):
        raise LxReadError(LX_ENVELOPE_SHAPE, "erp inner envelope carries no list array")
    return {"rows": list(rows), "total": _first_int(inner, ("total",))}


def _first_read_error(exc: BaseException) -> LxReadError | None:
    """从（可能嵌套的）ExceptionGroup 里找出第一个已分类的 LxReadError。"""
    if isinstance(exc, LxReadError):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            found = _first_read_error(sub)
            if found is not None:
                return found
    return None


#: 连接断了是「没问到」，再问一次可能就好了。其余的 MCP 错误都是网关的回答——包括
#: -32001：SDK 拿它表示本地超时，网关也可能拿它说「key 无效」；本地超时这里自己计时，
#: 不走 SDK 的那个码（见 _call_action）。
_NOT_ASKED_CODES = frozenset({CONNECTION_CLOSED})


def _asked_again_may_help(status: int) -> bool:
    return status >= 500 or status in (408, 429)


def classify_mcp_error(exc: MCPError, statuses: Sequence[int]) -> LxReadError:
    """SDK 把 HTTP ≥400 和 JSON-RPC error 都变成 MCPError；这里把它分回「没问到」和「被拒」。

    SDK 里 401、403、5xx 是同一句 INTERNAL_ERROR「Server returned an error response」，
    分不出来，所以要看这次连接上见过的 HTTP 状态码：5xx（和 408/429）当网关一时出错，
    可以再问；其余一律是网关的拒绝——错 key、错地址、http 协议、贴成了网页地址——
    再问多少次都一样。2026-09-24 评审在假网关上复现：这些此前全被当成网络错误，
    每页白问 3 次，网关原话一个字没留下。
    """
    said = f"code={exc.code} {exc.message}"[:300]
    if exc.code in _NOT_ASKED_CODES or any(_asked_again_may_help(s) for s in statuses):
        return LxTransportError(f"gateway call failed at transport level: {said}")
    return LxGatewayError(f"gateway refused the call: {said}", error_details=said)


def _scrub(value: object, key: str) -> object:
    if isinstance(value, str):
        return value.replace(key, "***")
    if isinstance(value, Mapping):
        return {k: _scrub(v, key) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, key) for v in value]
    return value


def _exception_summary(exc: BaseException) -> str:
    """异常类型名摘要（ExceptionGroup 递归展平），让超时之类的根因进错误消息。"""
    if isinstance(exc, BaseExceptionGroup):
        names = sorted({_exception_summary(sub) for sub in exc.exceptions})
        return ", ".join(names) if names else type(exc).__name__
    return type(exc).__name__


def _content_text(result: CallToolResult) -> str | None:
    for block in result.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            return text
    return None


class LxMcpReadClient:
    """领星网关只读 MCP 客户端（同步接口，内部以 asyncio.run 包裹 mcp SDK 会话）。

    - 凭据：url/key 由调用方从环境取出后传入；类内不读环境变量、不打印 key，repr 脱敏。
    - 防写：fetch_page 在任何网络调用之前校验只读白名单（LX_TOOL_NOT_ALLOWED）。
    - 节流：每次网关调用后 sleep(min_interval_seconds)（默认 1.1，测试传 0）。
    """

    def __init__(
        self,
        url: str,
        key: str,
        *,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not url.strip():
            raise LxReadError(LX_CONFIG_INVALID, "url must be non-empty")
        if not key.strip():
            raise LxReadError(LX_CONFIG_INVALID, "key must be non-empty")
        if min_interval_seconds < 0:
            raise LxReadError(LX_CONFIG_INVALID, "min_interval_seconds must be >= 0")
        if timeout_seconds <= 0:
            raise LxReadError(LX_CONFIG_INVALID, "timeout_seconds must be > 0")
        self._url = url
        self._key = key
        self._min_interval_seconds = min_interval_seconds
        self._timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return f"LxMcpReadClient(url={self._url!r}, key='***')"

    def fetch_page(self, tool_id: str, params: Mapping[str, object]) -> Mapping[str, object]:
        """拉取一页 → {"rows": list, "total": int | None}（吻合 LxReadPort 的结构合同）。

        白名单校验先于一切网络动作；汇总行过滤不在本层做（sync 层职责）。
        """
        if tool_id not in READ_TOOL_ALLOWLIST:
            raise LxToolNotAllowed(
                f"tool {tool_id!r} is not in the read-only allowlist; "
                "write tools can never leave this client"
            )
        encoded = encode_params(tool_id, params)
        try:
            json.dumps(encoded, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            # 带码 fail-loud：非 JSON 可编码的参数值（如 Decimal/datetime）不得以
            # 裸 TypeError 逃逸——同样发生在任何网络调用之前。
            raise LxReadError(
                LX_PARAM_NOT_ENCODABLE,
                f"params for tool {tool_id!r} are not JSON-encodable: {exc}",
            ) from exc
        envelope: dict[str, object] = {"toolId": tool_id, "params": encoded}
        try:
            try:
                payload = self._perform_call(envelope)
            finally:
                # QPS 保守：无论成败都保持调用间隔，避免错误重试冲垮网关限额。
                if self._min_interval_seconds > 0:
                    time.sleep(self._min_interval_seconds)
            if tool_id == ERP_LISTING_TOOL_ID:
                return parse_erp_envelope(payload)
            if tool_id == AUTH_SHOPS_TOOL_ID:
                return parse_auth_shops_envelope(payload)
            return parse_ad_report_envelope(payload)
        except LxReadError as exc:
            # 网关原话跟着错误一路进日志；网关要是回显了请求头，key 就跟着进去了。
            # 带原话出本类的错误都从这里走，就在这一处把 key 抹掉（2026-09-24 Codex 复审 P2）。
            exc.args = tuple(_scrub(arg, self._key) for arg in exc.args)
            if isinstance(exc, LxGatewayError):
                exc.error_details = _scrub(exc.error_details, self._key)
            raise

    def _perform_call(self, envelope: Mapping[str, object]) -> Mapping[str, object]:
        """一次网关 action 调用的同步外壳。测试以假体替换本方法，不触网络。

        传输层异常统一归类为 LxTransportError：MCP 客户端跑在 anyio task group 里，
        底层超时会被包成 ExceptionGroup，不拆包就会以裸异常穿透成 HTTP 500，
        调用方既看不出是超时还是缺数据，也拿不到带码分类。网关的拒绝在 _call_action
        里就已分好类（classify_mcp_error），这里从组里把它原样拿出来。
        """
        try:
            return asyncio.run(self._call_action(envelope))
        except LxReadError:
            raise  # 已分类（信封/形态错误），不重复包装
        except BaseExceptionGroup as group:
            classified = _first_read_error(group)
            if classified is not None:
                raise classified from group
            raise LxTransportError(
                f"gateway call failed at transport level: {_exception_summary(group)}"
            ) from group
        except (httpx2.HTTPError, OSError) as exc:
            raise LxTransportError(
                f"gateway call failed at transport level: {type(exc).__name__}"
            ) from exc

    async def _call_action(self, envelope: Mapping[str, object]) -> Mapping[str, object]:
        statuses: list[int] = []

        async def note_status(response: httpx2.Response) -> None:
            statuses.append(response.status_code)

        async with (
            httpx2.AsyncClient(
                headers={"X-Mcp-Key": self._key},
                timeout=self._timeout_seconds,
                event_hooks={"response": [note_status]},
            ) as http,
            streamable_http_client(self._url, http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            try:
                # 每次请求的上限。httpx 的超时是「两次收到字节之间」：网关用 SSE 回应、只发保活
                # 不给结果时，没有这一条调用就永远挂着，两把锁一直占着，之后每问一次都是「上一次
                # 查询还在跑」（2026-09-24 评审在假网关上复现）。超时抛 TimeoutError，归传输层。
                async with asyncio.timeout(self._timeout_seconds):
                    await session.initialize()
                async with asyncio.timeout(self._timeout_seconds):
                    result = await session.call_tool(ACTION_TOOL_NAME, dict(envelope))
            except MCPError as exc:
                raise classify_mcp_error(exc, statuses) from exc
            if not isinstance(result, CallToolResult):
                raise LxGatewayError(
                    "gateway returned an unexpected MCP result type",
                    error_details=type(result).__name__,
                )
            if result.is_error:
                said = _content_text(result) or ""
                raise LxGatewayError(
                    # 网关原话（含 msg 与 traceId，不含 key）进消息：只放在 error_details
                    # 里时，日志只剩这半句，看不出是参数错、版本过期还是权限不够。
                    f"gateway rejected the action call at MCP level: {said[:300]}",
                    error_details=said,
                )
            structured: object = result.structured_content
            if structured is not None:
                return _require_mapping(structured, "action structured content")
            text = _content_text(result)
            if text is None:
                raise LxReadError(
                    LX_ENVELOPE_SHAPE, "action result carries neither structured nor text content"
                )
            parsed: object = json.loads(text)
            return _require_mapping(parsed, "action result payload")
