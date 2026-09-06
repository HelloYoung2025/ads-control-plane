"""本地演示组合根：单进程同时挂审批 API + Internal MCP(/mcp) + 审批 UI(/ui)。

仅限本机演示（LOCAL DEMO ONLY），生产禁用：
- 含固定可读的 demo token 与 /dev/identities 端点，生产 app factory 不得包含两者；
- 身份、审批与种子数据（demo token、Mock 搜索词、种子镜像）恒为 Mock
  （Provider.MOCK + 占位 ID）；
- 但同步通道随 env 而定：LX_MCP_KEY/LX_MCP_URL 配齐时「同步镜像」调用领星生产 API，
  UI 徽章与启动横幅按 /dev/runtime-config（见 runtime_channel_status）如实二态播报——
  2026-08-29 排查结论：此前两种部署状态的「全部是 Mock」文案逐字相同；
- 服务只应绑定 127.0.0.1（见 scripts/serve_local_demo.py）。
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from ads_control_plane.adapters.lx_read import AUTH_SHOPS_TOOL_ID, LxMcpReadClient, LxReadError
from ads_control_plane.api.approval_api import build_approval_app
from ads_control_plane.api.mcp_tools.server import (
    InMemoryActorTokenVerifier,
    build_internal_mcp,
)
from ads_control_plane.api.mcp_tools.service import ReadToolService
from ads_control_plane.api.mcp_tools.strategy_service import StrategyToolService
from ads_control_plane.api.workbench_api import (
    ENV_LX_MCP_KEY,
    ENV_LX_MCP_URL,
    _allowed_profiles_from_env,
    build_workbench_router,
)
from ads_control_plane.authorization.model import Action, ClientType, Environment, Grant
from ads_control_plane.canonical.entity import (
    AdProduct,
    CanonicalEntityRef,
    EntityType,
    ParentRefs,
    Provider,
)
from ads_control_plane.canonical.ids import CanonicalId, new_canonical_id
from ads_control_plane.canonical.money import Money
from ads_control_plane.identity.actor import (
    ActorContext,
    AuthenticationStrength,
    PrincipalType,
    Role,
)
from ads_control_plane.mirror.repository import InMemorySnapshotRepository
from ads_control_plane.mirror.snapshot import AdObjectSnapshot
from ads_control_plane.mirror.sync import (
    CATALOG_VERSION,
    SCHEMA_VERSIONS,
    TOOL_CAMPAIGN_REPORT,
    TOOL_GROUP_REPORT,
    TOOL_KEYWORD_REPORT,
    TOOL_TARGETING_REPORT,
)
from ads_control_plane.providers.lingxing.search_terms import (
    LingxingProfileBinding,
    LingxingSearchTermSource,
)
from ads_control_plane.providers.mock.search_terms import MockSearchTermSource
from ads_control_plane.strategies.negation import SearchTermRecord
from ads_control_plane.strategies.ports import SearchTermReadPort, attribution_window
from ads_control_plane.strategies.store import (
    InMemoryCandidateSetStore,
    InMemoryMandateRunLog,
    InMemoryMandateStore,
)
from ads_control_plane.tasks.directive import ObjectLevel

DEMO_OWNER_TOKEN = "demo-owner-token"
DEMO_CODEX_TOKEN = "demo-codex-token"

#: 演示身份的有效期，见 build_local_demo_app 内 _actor 的说明。
DEMO_TOKEN_LIFETIME = timedelta(days=365)

#: (token, 显示名, 技术标识, 能力摘要)。serve 脚本横幅与 /dev/identities 共用同一份口径。
#:
#: 2026-08-28 Owner：「审批者跟运营人员目前来说合二为一」→ 本演示只给一个「人」
#: 身份（取 OPERATOR ∪ APPROVER 两个角色的并集）。这是**演示层**的合并，不是域层
#: 变更：authorization/ 的 6 角色目录、请求级 SoD 冲突矩阵、AI 动作上限全部原样保留，
#: 将来要多人分离时把这一行拆成两行、各持一个角色即可，无需改任何代码。
DEMO_TOKEN_ROWS: tuple[tuple[str, str, str, str], ...] = (
    (
        DEMO_OWNER_TOKEN,
        "运营负责人",
        "HUMAN owner-1 (OPERATOR+APPROVER)",
        "签发/撤销授权书 · 批准/拒绝候选 · 导出 CSV · 触发同步",
    ),
    (
        DEMO_CODEX_TOKEN,
        "AI 助手",
        "AI codex-1 (ANALYST, 委托人 owner-1)",
        "生成候选、只读查询；批准 / 拒绝 / 签发 / 撤销 / 同步一律被服务端拒绝",
    ),
)

DEMO_PROFILE = "profile-A"

#: 真实搜索词源的开关与配置。开关与同步 key 分开，理由见 lx_strategy_source_enabled。
ENV_STRATEGY_LX_ENABLED = "ADS_CP_STRATEGY_LX_ENABLED"
#: "<profile_id>:USD,<profile_id>:EUR"。**逐店覆盖用**；常规币种按站点推出，不必配。
ENV_LX_PROFILE_CURRENCY = "ADS_CP_LX_PROFILE_CURRENCY"
#: provider_connection_id 的稳定来源；不配则每次启动现生（候选集合 hash 会随之变）。
ENV_LX_CONNECTION_ID = "ADS_CP_LX_CONNECTION_ID"

#: 审批 UI 静态目录（内容由 ui_static 交付物提供；目录暂缺时 /ui 返回 404，不影响启动）。
UI_STATIC_DIR = Path(__file__).resolve().parent / "ui_static"


class _RevalidatedStaticFiles(StaticFiles):
    """静态资源强制逐次向服务端核对新鲜度（Cache-Control: no-cache）。

    StaticFiles 默认只发 ETag/Last-Modified 而不发 Cache-Control，浏览器于是按
    启发式缓存把旧 app.js 留很多天——2026-08-29 实测：服务端已修好的界面，用户
    打开还是旧版，得自己想到按 Cmd+Shift+R。「要用户清缓存」本身就是缺陷。
    no-cache 不是不缓存：仍走 ETag 协商，未变时 304 零字节，变了立即拿到新版。
    只包 /ui 子应用，不碰挂在根上的 MCP 流式响应。
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _seed_records(
    org: CanonicalId, connection_id: CanonicalId, now: datetime
) -> list[SearchTermRecord]:
    """10 条 Mock 搜索词绩效：3 条达标入选、1 条过旧、1 条 ASIN 型（两者均 ABSTAIN）、
    2 条有转化、3 条证据不足。

    ASIN 那条是特意放的：它花的钱和点击都过了门槛、零转化，长得和候选一模一样，
    唯独否定精准关键词对它无效。不放，演示里就永远看不到这条路径，而人第一次遇到
    它是在真实店里——那时他已经照着 CSV 做完，并以为处理好了。
    """

    _seed_window = attribution_window(lookback_days=30, as_of=now)

    def rec(
        ad_group: str,
        campaign: str,
        term: str,
        clicks: int,
        conversions: int,
        spend: str,
        impressions: int,
        *,
        stale: bool = False,
        asin: bool = False,
    ) -> SearchTermRecord:
        return SearchTermRecord(
            scope=CanonicalEntityRef(
                organization_id=org,
                provider=Provider.MOCK,
                provider_connection_id=connection_id,
                marketplace="US",
                shop_external_id="shop-1",
                profile_external_id=DEMO_PROFILE,
                ad_product=AdProduct.SP,
                entity_type=EntityType.AD_GROUP,
                entity_external_id=ad_group,
                parent_refs=ParentRefs(campaign_external_id=campaign),
            ),
            search_term=term,
            clicks=clicks,
            conversions=conversions,
            spend=Money(amount=Decimal(spend), currency=DEMO_SEED_CURRENCY),
            #: 占位窗口——真正的窗口由 MockSearchTermSource 每次取数时按授权书上的
            #  lookback_days 现算并覆盖（ports.attribution_window，与真实源同一套）。
            #  这里仍必须填一个合法值：SearchTermRecord 要求 window_end > window_start。
            window_start=_seed_window[0],
            window_end=_seed_window[1],
            # 默认参数包 max_data_staleness_hours=24：48h 前的数据触发 ABSTAIN。
            data_as_of=now - timedelta(hours=48 if stale else 2),
            term_is_asin=asin,
            impressions=impressions,
        )

    return [
        # 达标（零转化 + 点击>=25 + 花费>=20.00 USD + 新鲜）→ 候选。
        # 曝光特意拉开两个量级：前两条花费与点击相仿，CTR 却是 6.8% 对 0.1%——
        # 一个是「相关但转化不了，问题多半在 listing 或价格」，一个是「纯粹不相关」，
        # 而花费/点击/广告订单三列在两者上几乎一样。这正是加曝光那一列的理由。
        rec("ag-1", "c-1", "cheap widget holder", 42, 0, "35.40", 620),
        rec("ag-1", "c-1", "widget free shipping", 61, 0, "48.90", 58000),
        rec("ag-2", "c-2", "wobbly widget hack", 27, 0, "21.75", 41000),
        # 数据过旧 → ABSTAIN 显式上报（"无法判断"不同于"没有候选"）
        rec("ag-2", "c-2", "vintage widget manual", 55, 0, "64.10", 3100, stale=True),
        # 证据齐了、但这条"搜索词"是个 ASIN → ABSTAIN（"否不掉"也不是"没有候选"）。
        # 否定精准关键词挡不住 ASIN 型来源，得去领星「否定投放」手工处理。
        rec("ag-1", "c-1", "b0demo0001", 38, 0, "29.50", 2400, asin=True),
        # 有转化 → 永不候选（规则定义，非参数）
        rec("ag-1", "c-1", "best widget 2026", 210, 17, "180.00", 9800),
        rec("ag-2", "c-2", "widget gift set", 96, 4, "77.25", 5400),
        # 证据不足（点击或花费低于门槛）→ 正常排除
        rec("ag-1", "c-1", "widget adapter", 12, 0, "9.80", 800),
        rec("ag-2", "c-2", "blue widget case", 31, 0, "14.60", 2600),
        rec("ag-1", "c-1", "widget replacement part", 18, 0, "26.30", 1500),
    ]


def _seed_mirror(repo: InMemorySnapshotRepository, now: datetime) -> None:
    """演示镜像快照（全 Mock）：2 活动 / 2 广告组 / 3 投放（keyword 并入 TARGET 层）。

    覆盖工作台 UI 的全部展示形态：托管打标（ads_strategy → 锁形徽章 + 禁勾选）、
    分时标志（is_apply_time）、以及一组 source_as_of 超 24h 的过旧数据（警示徽章）。
    """
    stale = now - timedelta(hours=30)

    def snap(
        object_key: str,
        level: ObjectLevel,
        tool_id: str,
        *,
        source_as_of: datetime | None = None,
        **fields: Any,
    ) -> AdObjectSnapshot:
        return AdObjectSnapshot(
            object_key=object_key,
            level=level,
            profile_id=DEMO_PROFILE,
            sid="demo-sid-1",
            source_as_of=source_as_of if source_as_of is not None else now,
            recorded_at=now,
            catalog_version=CATALOG_VERSION,
            schema_version=SCHEMA_VERSIONS[tool_id],
            **fields,
        )

    seeds = [
        snap(
            "campaign:c-1",
            ObjectLevel.CAMPAIGN,
            TOOL_CAMPAIGN_REPORT,
            name="HX02-Auto-US",
            state="enabled",
            daily_budget=Decimal("12.00"),
            is_apply_time=False,
            metrics={
                "spends": "34.10",
                "sales": "81.19",
                "acos": "0.42",
                "orders": "3",
                "clicks": "120",
                "impressions": "4300",
            },
        ),
        snap(
            "campaign:c-2",
            ObjectLevel.CAMPAIGN,
            TOOL_CAMPAIGN_REPORT,
            name="HX02-Exact-US",
            state="enabled",
            daily_budget=Decimal("25.00"),
            ads_strategy="分时预算",
            is_apply_time=True,
            source_as_of=stale,
            metrics={
                "spends": "61.75",
                "sales": "343.06",
                "acos": "0.18",
                "orders": "11",
                "clicks": "260",
                "impressions": "9100",
            },
        ),
        snap(
            "ad_group:ag-1",
            ObjectLevel.AD_GROUP,
            TOOL_GROUP_REPORT,
            name="ag-core",
            state="enabled",
            default_bid=Decimal("0.80"),
            parent_campaign_id="c-1",
            metrics={
                "spends": "20.40",
                "sales": "40.00",
                "acos": "0.51",
                "orders": "1",
                "clicks": "70",
            },
        ),
        snap(
            "ad_group:ag-2",
            ObjectLevel.AD_GROUP,
            TOOL_GROUP_REPORT,
            name="ag-brand",
            state="paused",
            default_bid=Decimal("1.10"),
            parent_campaign_id="c-2",
            source_as_of=stale,
            metrics={
                "spends": "13.70",
                "sales": "152.22",
                "acos": "0.09",
                "orders": "6",
                "clicks": "50",
            },
        ),
        snap(
            "target:t-1",
            ObjectLevel.TARGET,
            TOOL_TARGETING_REPORT,
            name="close-match",
            state="enabled",
            bid=Decimal("0.75"),
            default_bid=Decimal("0.80"),
            parent_campaign_id="c-1",
            parent_ad_group_id="ag-1",
            #: 零订单就是零销售额，而零销售额的 ACOS 是无穷大——领星用 99999999 表示它
            #  （app.js 的 ACOS_INFINITE_SENTINEL，2026-08-29 实测某店 100 个在投活动里
            #  30 个是这个值）。这条种子此前写的是 orders=0 配一个有限的 acos=0.63，
            #  自相矛盾；而代码注释自己说这类广告「最该被看见——花了钱一单没出」，
            #  演示却一次都没展示过那颗「无销售」芯片（2026-09-07 排查）。
            metrics={
                "spends": "9.30",
                "sales": "0.00",
                "acos": "99999999",
                "orders": "0",
                "clicks": "31",
            },
        ),
        snap(
            "target:kw-1",
            ObjectLevel.TARGET,
            TOOL_KEYWORD_REPORT,
            name="widget holder",
            state="enabled",
            bid=Decimal("0.95"),
            default_bid=Decimal("0.80"),
            keyword_text="widget holder",
            match_type="exact",
            parent_campaign_id="c-1",
            parent_ad_group_id="ag-1",
            metrics={
                "spends": "11.10",
                "sales": "31.71",
                "acos": "0.35",
                "orders": "2",
                "clicks": "39",
            },
        ),
        snap(
            "target:kw-2",
            ObjectLevel.TARGET,
            TOOL_KEYWORD_REPORT,
            name="widget gift",
            state="enabled",
            bid=Decimal("0.55"),
            default_bid=Decimal("1.10"),
            keyword_text="widget gift",
            match_type="broad",
            ads_strategy="关键词调价",
            parent_campaign_id="c-2",
            parent_ad_group_id="ag-2",
            metrics={
                "spends": "4.20",
                "sales": "35.00",
                "acos": "0.12",
                "orders": "3",
                "clicks": "18",
            },
        ),
    ]
    for snapshot in seeds:
        repo.append(snapshot)


#: 演示种子数据的币种。签发面据它给 profile-A 设闸——演示通道下同样不许人签出
#: 一份运行期必被 CURRENCY_MISMATCH 拒的授权书。
DEMO_SEED_CURRENCY = "USD"

#: profile → 该店数据实际结算的币种。与 _SEARCH_TERM_CHANNEL 同样在组合根记一次：
#: 签发面（POST /mandates）与工作台下拉都要问它，而它们都不该认识 Provider。
_PROFILE_CURRENCY: dict[str, str] = {}

#: 组合根实际装配的搜索词通道（名称, 绑定店铺数）。徽章与启动横幅读它，
#: 所以它必须记「装配结果」而不是「开关状态」——两者在绑定失败时并不相同。
#:
#: 挂 Mock 的**成因**进取值，不塌成一个（2026-08-30 排查两轮）。
#:
#: 第一轮只分了「开关没开」与「开关开了但没绑上」两种，而「没绑上」自己还有三个
#: 成因，界面只好点名其中一个——点的偏偏不是最常见的那个：ADS_CP_SYNC_PROFILES
#: 在 .env.example 里缺省就是空的，首次运行必然落在这一支，而文案叫人去查店铺 sid
#: 和币种，两件都白做，真正要设的那个变量一个字都没提。
#:
#: 成因决定人该去动哪个开关。塌成一个值就是让界面替他猜，而它每次都猜同一个。
_CHANNEL_LINGXING = "LINGXING"
_CHANNEL_SWITCH_OFF = "MOCK"  # 未设 ADS_CP_STRATEGY_LX_ENABLED
_CHANNEL_NO_CREDENTIALS = "MOCK_NO_CREDENTIALS"  # 开关开了但 key/URL 缺失
_CHANNEL_NO_PROFILES = "MOCK_NO_PROFILES"  # 同步白名单为空 = 一个店都不许碰
_CHANNEL_LOOKUP_FAILED = "MOCK_LOOKUP_FAILED"  # 店铺名录取不到（网关调用失败）
_CHANNEL_NO_BINDINGS = "MOCK_NO_BINDINGS"  # 名录取到了，但白名单内没有一个店可绑
_SEARCH_TERM_CHANNEL: tuple[str, int] = (_CHANNEL_SWITCH_OFF, 0)

#: 站点 → 报表金额的币种。
#:
#: 本表把站点映成币种，依赖一个前提：领星报表的 spends 是站点本币，不是折算后的统一
#: 口径。前提不成立时，min_spend 门槛比的是另一种币，而授权书上盖的币种章是系统性
#: 谎言——两者都不会报错。这个前提是 DEC-024，首次真实运行前必须先关掉它；
#: 它当下是什么状态去登记簿看，这里不复述（复述的那一刻就开始过期）。
#:
#: 表里没有的站点不猜：该店不进绑定表，has_profile 如实返回 False。
#: 个别店铺要改用别的币种，用 ADS_CP_LX_PROFILE_CURRENCY 逐店覆盖。
MARKETPLACE_CURRENCY: dict[str, str] = {
    "US": "USD",
    "CA": "CAD",
    "MX": "MXN",
    "BR": "BRL",
    "UK": "GBP",
    "GB": "GBP",
    "DE": "EUR",
    "FR": "EUR",
    "IT": "EUR",
    "ES": "EUR",
    "NL": "EUR",
    "BE": "EUR",
    "IE": "EUR",
    "PL": "PLN",
    "SE": "SEK",
    "TR": "TRY",
    "AE": "AED",
    "SA": "SAR",
    "EG": "EGP",
    "IN": "INR",
    "JP": "JPY",
    "AU": "AUD",
    "SG": "SGD",
    "ZA": "ZAR",
}


def _lx_search_term_bindings(
    org: CanonicalId,
) -> tuple[dict[str, LingxingProfileBinding], str | None]:
    """构建 profile → 绑定表，**并说出空表是怎么空的**。

    任一必需身份缺失就不进表——不进表 = has_profile 返回 False = 对外说「这个店
    没接数据源」，而那是字面真话：我们确实无法为它构造 canonical 引用（缺 sid 就没有
    shop_external_id，缺币种就没法比金额）。

    店铺 sid 与 country 取自 ad_auth_shops（已在只读白名单），币种由站点推出
    （见 MARKETPLACE_CURRENCY）。未知站点不猜，该店不进表。

    返回第二项是**空表的成因**（非空时为 None）。只回一个空 dict 的话，三种截然
    不同的处境在调用方眼里长得一模一样，而它们各自要人去动的开关完全不同——
    界面只好点名其中一个，且必然点错另外两种（2026-08-30 排查）。
    """
    key = os.environ.get(ENV_LX_MCP_KEY, "").strip()
    url = os.environ.get(ENV_LX_MCP_URL, "").strip()
    if not key or not url:
        return {}, _CHANNEL_NO_CREDENTIALS
    overrides = _lx_profile_currencies()
    allowed = _allowed_profiles_from_env()
    if not allowed:
        # 空白名单 = 一个店都不绑，与 SyncEngine 同一条 §2 fail-closed 规矩
        # （sync.py:382 直接抛 SYNC_NO_ALLOWED_PROFILES）。此前这里写的是
        # `allowed and pid not in allowed`——空集合短路成假，于是**整个账户的每个店**
        # 都被绑上。同一个环境变量，两条链路的失败方向相反：有人清空白名单正是为了
        # 停止碰真实店铺，而候选生成照样在读全部店。
        # 界面上这还是一对自相矛盾的数字：「0 个店铺在同步白名单内」与
        # 「N 个店铺已绑定」并排显示。
        return {}, _CHANNEL_NO_PROFILES
    try:
        page = LxMcpReadClient(url, key).fetch_page(AUTH_SHOPS_TOOL_ID, {})
    except LxReadError:
        # 名录不可得就一个店都不绑：宁可说「没接」，不可默认「接了」。
        return {}, _CHANNEL_LOOKUP_FAILED
    raw_rows = page.get("rows")
    rows = raw_rows if isinstance(raw_rows, list | tuple) else ()
    connection_id = _lx_connection_id()
    bindings: dict[str, LingxingProfileBinding] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        pid = str(row.get("profile_id") or "").strip()
        sid = str(row.get("sid") or "").strip()
        country = str(row.get("country") or "").strip().upper()
        # 覆盖优先，其次按站点推。推不出来就不绑这个店——不猜币种。
        currency = overrides.get(pid, "").strip().upper() or MARKETPLACE_CURRENCY.get(country, "")
        if not (pid and sid and country and currency) or pid not in allowed:
            continue
        bindings[pid] = LingxingProfileBinding(
            profile_external_id=pid,
            organization_id=org,
            provider_connection_id=connection_id,
            marketplace=country,
            shop_external_id=sid,
            currency=currency,
        )
    return bindings, (None if bindings else _CHANNEL_NO_BINDINGS)


def _lx_profile_currencies() -> dict[str, str]:
    """ADS_CP_LX_PROFILE_CURRENCY，形如 "<pid>:USD,<pid>:EUR"。

    只是逐店覆盖；常规情况下币种由 MARKETPLACE_CURRENCY 按站点推出，不用配。
    """
    raw = os.environ.get(ENV_LX_PROFILE_CURRENCY, "").strip()
    out: dict[str, str] = {}
    for item in raw.split(","):
        pid, _, currency = item.partition(":")
        if pid.strip() and currency.strip():
            out[pid.strip()] = currency.strip()
    return out


def _lx_connection_id() -> CanonicalId:
    """provider_connection_id：配了就用配的，否则本进程现生一个。

    现生的那个进入候选集合的冻结 hash，于是同一份数据在进程重启后会得到不同 hash。
    单次运行内不影响正确性，但真实通道下应当配一个稳定 UUID。
    """
    raw = os.environ.get(ENV_LX_CONNECTION_ID, "").strip()
    if raw:
        try:
            return uuid.UUID(raw)
        except ValueError:
            pass
    return new_canonical_id()


def lx_strategy_source_enabled() -> bool:
    """真实搜索词源需要一道**独立于同步 key** 的显式开关。

    generate_negation_candidate_set 是 AI 可调用工具，而 POST /api/workbench/sync
    明确要求 HUMAN（AI → 403）。即席模式（不带 mandate_id）也没有任何配额——
    日运行数与最小间隔只在授权书模式下生效。所以一旦真实源接进策略面，只要 key 配上，
    AI 就能在无人参与的情况下反复触发对领星生产 API 的多页读取。
    接入必须是一次明确的「我知道我在干什么」，不能配了同步 key 就顺带打开。
    """
    return os.environ.get(ENV_STRATEGY_LX_ENABLED, "").strip().lower() in {"1", "true", "yes"}


def _build_search_term_source(org: CanonicalId, now: datetime) -> SearchTermReadPort:
    """真实源与 Mock **二选一**，不做混合。

    混合（真实店走真实、演示 profile 走 Mock）会让 has_profile 对两类 profile 都
    返回 True，于是人无法区分眼前这条候选是演示数据还是要花真钱的真实数据——
    这个仓库为「两种部署状态文案逐字相同」已经修过一次（ui-4/runtime-1）。
    真实通道开启时演示 profile 就该返回 profile_has_data_source=false，那是真话。
    """
    global _SEARCH_TERM_CHANNEL
    _PROFILE_CURRENCY.clear()
    reason: str | None = _CHANNEL_SWITCH_OFF
    if lx_strategy_source_enabled():
        bindings, reason = _lx_search_term_bindings(org)
        _PROFILE_CURRENCY.update({pid: b.currency for pid, b in bindings.items()})
        if bindings:
            key = os.environ.get(ENV_LX_MCP_KEY, "").strip()
            url = os.environ.get(ENV_LX_MCP_URL, "").strip()
            _SEARCH_TERM_CHANNEL = (_CHANNEL_LINGXING, len(bindings))
            return LingxingSearchTermSource(LxMcpReadClient(url, key), bindings=bindings)
    # 落到这里就是挂 Mock，而 reason 记着是怎么落下来的。见常量处的说明。
    _SEARCH_TERM_CHANNEL = (reason or _CHANNEL_NO_BINDINGS, 0)
    # 只种演示 profile：对其余任何 profile，生成工具会返回 profile_has_data_source=false
    # ——自报"这个店没接数据源"，而不是伪装成"查了没有浪费"（runtime-2：两种返回
    # 曾逐字相同，真实店铺跑一圈得到的 0 会被当成好消息）。
    #: 演示服务是常驻的，种子却在启动那一刻定死了 data_as_of。开满 22 小时之后
    #  （默认 max_data_staleness_hours=24，种子本身已 2 小时旧）同一批数据从
    #  「3 个候选」变成「数据太旧，全部弃权」，而界面给的下一步是「等新数据」——
    #  演示里永远不会有新数据。让种子跟着时钟一起变旧，各条**相对新旧不变**：
    #  故意做旧的那条仍然触发 STALE_DATA，演示的那条路径一条都没少。
    mock = MockSearchTermSource(ages_with_clock=True)
    mock.seed(DEMO_PROFILE, _seed_records(org, new_canonical_id(), now), seeded_at=now)
    _PROFILE_CURRENCY[DEMO_PROFILE] = DEMO_SEED_CURRENCY
    return mock


def profile_currency(profile_external_id: str) -> str | None:
    """这个店的数据实际用什么币种结算。不知道就返回 None，绝不默认 USD。

    签发授权书时的 min_spend 币种必须与它一致，否则 negation.py 的运行期闸
    （CURRENCY_MISMATCH）会让这份授权永远跑不出东西，而人看到的是一份状态
    ACTIVE 的正常授权书——mandate.py:157 的 _scope_belongs_to_this_profile 为
    完全相同的病写了兄弟闸并说明了理由，币种这一条一直没配上。

    返回 None 时签发不设闸：我们确实无法校验，装作能校验比不校验更坏。
    """
    return _PROFILE_CURRENCY.get(profile_external_id)


def runtime_channel_status() -> dict[str, Any]:
    """数据通道状态判定：UI 徽章（GET /dev/runtime-config）与启动横幅共用的唯一口径。

    2026-08-29 排查结论（ui-4/runtime-1）：LX_MCP_KEY/LX_MCP_URL/ADS_CP_SYNC_PROFILES
    配齐时本进程挂着真实领星通道，「同步镜像」会调用领星生产 API——而徽章与横幅此前
    硬编码「全部是 Mock」，两种截然不同的部署状态输出逐字相同。判定必须与 POST /sync
    的 fail-closed 判定同口径（同一组 env 常量 + 同一个白名单读取函数）：key 与 URL
    任缺一项都出不了网，即视为未配通道。

    只返回布尔与计数——key、URL、店铺 ID 本身绝不出现在返回值里。
    """
    key = os.environ.get(ENV_LX_MCP_KEY, "").strip()
    url = os.environ.get(ENV_LX_MCP_URL, "").strip()
    source, profile_count = _SEARCH_TERM_CHANNEL
    return {
        "lx_channel_configured": bool(key and url),
        "sync_profile_count": len(_allowed_profiles_from_env()),
        # 策略面用的是真实源还是演示数据。两者的候选长得一模一样，但一个花真钱、
        # 一个不花——不播报出来，人无法区分眼前这条候选属于哪种。
        # 报的是组合根**实际装配**的结果，不是"开关开了没"：开关开着但绑定不成立
        # （拿不到 sid、没声明币种）时挂的仍是 Mock，此时说 LINGXING 就是假话。
        "search_term_source": source,
        "search_term_profile_count": profile_count,
    }


def build_local_demo_app(clock: Callable[[], datetime] | None = None) -> FastAPI:
    """组装本地演示 app：审批 API 为基座，挂 /mcp、/ui、/dev/identities 与 / 重定向。

    clock 只注入业务面（候选生成、审批 TTL）；token 过期判定始终走真实时钟
    （InMemoryActorTokenVerifier 行为），故 demo 身份的有效期按真实 now 设置。
    """
    org = new_canonical_id()
    now = clock() if clock is not None else datetime.now(UTC)
    real_now = datetime.now(UTC)

    def _actor(**overrides: Any) -> ActorContext:
        base: dict[str, Any] = {
            "principal_id": new_canonical_id(),
            "principal_type": PrincipalType.HUMAN,
            "organization_id": org,
            "authentication_strength": AuthenticationStrength.MFA,
            "issued_at": real_now,
            # 有效期覆盖整个演示进程寿命。2026-08-29 实测：原先的 8 小时会让跑过夜的
            # 演示服务在第二天早上把每个请求都变成 401，而界面只说得出「缺少或无效的
            # Bearer Token」——人根本无从想到真因是「服务开太久」。演示 token 是硬编码
            # 常量、身份全 Mock、只监听回环，短有效期在这里没有任何安全收益，只制造
            # 这一个坑。域层不受影响：ActorContext.is_expired() 与验证器照常判定，变的
            # 只是本演示实例签发的到期时间。
            "expires_at": real_now + DEMO_TOKEN_LIFETIME,
        }
        return ActorContext(**{**base, **overrides})

    # 合二为一后的唯一「人」：同时持 OPERATOR 与 APPROVER。并集是「一个人干两份活」
    # 的唯一无损表达——域层 _ROLE_ACTIONS 不变，变的只是这个演示实例持有哪几个 Role。
    owner = _actor(
        roles=frozenset({Role.OPERATOR, Role.APPROVER}),
        human_person_id="owner-1",
        client_id="web-owner",
        session_id="s-owner",
    )
    # 委托人随之改指 owner-1：ops-1 已不存在，指向不存在的人就是一条断掉的委托链。
    # 这不会让 owner-1 变成 AI 集合的 creator——strategy_service 对 AI 主体恒记
    # created_by_person_id=None，故「owner 批 codex 生成的集合」这条主路径不受影响。
    codex = _actor(
        principal_type=PrincipalType.AI_CLIENT,
        roles=frozenset({Role.ANALYST}),
        human_initiator_person_id="owner-1",
        client_id="codex-1",
        session_id="s-codex",
    )

    grants = [
        # MCP 工具面的授权：生成草案 + 只读。不带 profile 维度——带 profile 的
        # Grant 对 entity=None 的判定请求永不匹配（fail closed），授权靠本条。
        Grant(
            grant_id=new_canonical_id(),
            organization_id=org,
            environments=frozenset({Environment.STAGING}),
            actions=frozenset({Action.PROPOSAL_CREATE_DRAFT, Action.RESOURCE_READ}),
            client_types=frozenset({ClientType.MCP_AI}),
        ),
        # 展示 Grant：仅供 list_authorized_scopes 列出可见 profile。
        Grant(
            grant_id=new_canonical_id(),
            organization_id=org,
            environments=frozenset({Environment.STAGING}),
            actions=frozenset({Action.RESOURCE_READ}),
            client_types=frozenset({ClientType.MCP_AI}),
            profile_external_ids=frozenset({DEMO_PROFILE}),
        ),
    ]

    # 只种 profile-A：对其余任何 profile，生成工具会返回 profile_has_data_source=false
    # ——自报"这个店没接数据源"，而不是伪装成"查了没有浪费"（2026-08-29 排查结论
    # runtime-2：两种返回曾逐字相同，真实店铺跑一圈得到的 0 会被当成好消息）。
    source = _build_search_term_source(org, now)
    store = InMemoryCandidateSetStore()
    mandate_store = InMemoryMandateStore()
    # 同一个运行流水必须交给两边：MCP 工具面往里写（配额与最小间隔也从它算），
    # 审批面从里读。各建一个的后果是界面永远显示「还没跑过」——正是 #23 那半个洞。
    run_log = InMemoryMandateRunLog()
    strategy = StrategyToolService(
        environment=Environment.STAGING,
        grants=grants,
        denies=[],
        search_terms=source,
        store=store,
        mandates=mandate_store,
        run_log=run_log,
        clock=clock,
    )
    read = ReadToolService(environment=Environment.STAGING, grants=grants, denies=[])

    verifier = InMemoryActorTokenVerifier()
    identities: list[dict[str, Any]] = []
    # 顺序与 DEMO_TOKEN_ROWS 一致（owner, codex），strict 防错位。
    for (token, display_name, label, capabilities), actor in zip(
        DEMO_TOKEN_ROWS, (owner, codex), strict=True
    ):
        verifier.register(token, actor)
        identities.append(
            {
                "token": token,
                "display_name": display_name,  # UI 显示用人话
                "identity": label,  # 技术标识，runbook / 启动横幅用
                "capabilities": capabilities,
                "principal_type": actor.principal_type.value,
                "roles": sorted(role.value for role in actor.roles),
                "human_person_id": actor.human_person_id,
                "human_initiator_person_id": actor.human_initiator_person_id,
            }
        )

    mcp_server = build_internal_mcp(read, verifier, strategy=strategy)
    mcp_app = mcp_server.streamable_http_app()  # 只调一次；Bearer 401/403 由其内部中间件发出

    # 对象工作台镜像仓库先建：审批面也要靠它把候选/授权书里的 ID 解析成名称
    # （审计 #5/#9：批准与回溯不能对着两串 16 位数字进行）。
    # 演示数据种入让工作台开箱可用；真实同步仍需 env LX_MCP_KEY/LX_MCP_URL 与
    # ADS_CP_SYNC_PROFILES 白名单（缺省空 = 全拒，fail-closed）。
    snapshot_repo = InMemorySnapshotRepository()
    _seed_mirror(snapshot_repo, now)

    app = build_approval_app(
        store,
        verifier,
        clock=clock,
        mandates=mandate_store,
        snapshot_repo=snapshot_repo,
        profile_currency=profile_currency,
        run_log=run_log,
    )
    # 演示组合根把自己的验证器挂出来，供集成测试断言未来时刻的 token 有效性、
    # 以及现场排障时区分「token 不对」与「token 过期」。生产 app factory 不含本函数。
    app.state.demo_verifier = verifier

    # 对象工作台：共享同一个镜像仓库（浏览/预览/历史读它，人触发的同步写它）。
    app.include_router(
        build_workbench_router(
            snapshot_repo, verifier, clock=clock, profile_currency=profile_currency
        )
    )

    @contextlib.asynccontextmanager
    async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
        # streamable HTTP 的 task group 必须由宿主 lifespan 启动（Starlette 不跑被
        # mount 子 app 的 lifespan），否则 /mcp 已认证请求 500。approval app 没有
        # 注册任何 startup/lifespan 行为，替换其 lifespan_context 不丢东西。
        async with mcp_server.session_manager.run():
            yield

    app.router.lifespan_context = _lifespan

    @app.get("/dev/identities")
    def dev_identities() -> dict[str, Any]:
        """demo token 清单——仅本地演示组合根提供，生产 app factory 不含此端点。"""
        return {
            "warning": "LOCAL DEMO ONLY: 固定 token、全部 Mock 身份，生产禁用",
            "identities": identities,
        }

    @app.get("/dev/runtime-config")
    def dev_runtime_config() -> dict[str, Any]:
        """数据通道状态（布尔 + 计数），UI 徽章/页脚据此在 Mock 与真实通道两态间切换。

        每次请求现读 env 而不是启动时缓存——POST /sync 也是逐请求读 env，两处口径
        必须同刻一致，否则徽章会描述一个不存在的进程状态。
        """
        return runtime_channel_status()

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse(url="/ui/")

    # /ui 先挂；MCP 宿主挂在 "/"（其内部路由恰为 /mcp）是 catch-all，必须最后挂。
    app.mount(
        "/ui",
        _RevalidatedStaticFiles(directory=UI_STATIC_DIR, html=True, check_dir=False),
        name="ui",
    )
    app.mount("/", mcp_app)
    return app
