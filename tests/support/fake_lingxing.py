"""假的领星网关：按 S0 实测的形状（2026-09-24）回四种广告报表，只认只读工具。

行的字段名、汇总行（身份为空）、按窗口出指标、按页切、total 不含汇总行，都照真网关。
ID 全是一眼就能看出是编的样子（cmp-1、ag-1、kw-1），不进 SYNTHETIC_IDS。
任何别的 toolId（尤其写工具）先记进 calls 再炸：测试断言 calls 里只有只读报表。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

READ_TOOLS = frozenset(
    {
        "ad_campaign_product_report",
        "ad_campaign_report",
        "ad_campaign_keyword_report",
        "ad_campaign_targeting_report",
    }
)

PROFILE = "1000000000000001"
ASIN = "B0TEST0001"
#: 放进 Thing.flags 表示「这一行没有这个字段」。
MISSING = object()


@dataclass
class Ad:
    ad_id: str
    asin: str
    campaign_id: str
    ad_group_id: str
    sku: str = "SKU-1"
    sponsored_type: str = "sp"
    state: str = "enabled"
    ad_group_state: str = "enabled"
    stock: int | None = 100
    title: str = "Cat scratcher <b>deluxe</b>"


@dataclass
class Thing:
    """一个关键词或投放。"""

    kind: str  # keyword | target
    object_id: str
    campaign_id: str
    ad_group_id: str
    text: str = "cat scratcher"
    match_type: str = "exact"
    bid: str | None = "1.00"
    default_bid: str = "0.80"
    state: str = "enabled"
    campaign_state: str = "enabled"
    ad_group_state: str = "enabled"
    created: str = "2026-01-01 08:00:00"
    flags: dict[str, object] = field(default_factory=dict)


class TransportDown(Exception):
    code = "LX_TRANSPORT_ERROR"


class FakeLingxing:
    def __init__(self) -> None:
        self.ads: list[Ad] = []
        self.things: list[Thing] = []
        #: 活动行内标记的覆盖（默认全是「没在管」）。
        self.campaign_flags: dict[str, dict[str, object]] = {}
        self.campaign_state: dict[str, str] = {}
        #: 活动报表里缺席的活动（在投，但报表没给它的行）。
        self.campaign_report_omits: set[str] = set()
        #: (report_date, 对象或广告 id) → 指标。没设的问 market，再没有一律 0。
        self.metrics: dict[tuple[str, str], dict[str, object]] = {}
        #: 按窗口现算指标的市场模型（多日推演用）：(report_date, id) → 指标或 None。
        self.market: Callable[[str, str], dict[str, object] | None] | None = None
        self.calls: list[tuple[str, dict[str, object]]] = []
        #: toolId → 接下来要抛的异常（按顺序用掉）。
        self.failures: dict[str, list[Exception]] = {}
        #: 为 True 时 total 缺席。
        self.hide_total = False
        #: 给 total 额外加的数：模拟翻页时行在动、页数对不上。
        self.total_bonus = 0

    # ------------------------------------------------------------------ 布置

    def set(self, window: str, key: str, **values: object) -> None:
        self.metrics[(window, key)] = values

    def _metrics(self, window: str, key: str) -> dict[str, object]:
        values = self.metrics.get((window, key))
        if values is None and self.market is not None:
            values = self.market(window, key)
        values = values or {}
        return {
            "impressions": values.get("impressions", 0),
            "clicks": values.get("clicks", 0),
            "orders": values.get("orders", 0),
            "spends": values.get("spend", 0),
            "sales": values.get("sales", 0),
        }

    # ------------------------------------------------------------------ 网关

    def fetch_page(self, tool_id: str, params: Mapping[str, object]) -> Mapping[str, object]:
        # 先记账再拒绝：调用方会把异常收成「这次没看成」，只抛不记就测不出写调用。
        self.calls.append((tool_id, dict(params)))
        if tool_id not in READ_TOOLS:
            raise AssertionError(f"只读假网关收到了 {tool_id}")
        pending = self.failures.get(tool_id)
        if pending:
            raise pending.pop(0)
        window = str(params["report_date"])
        if tool_id == "ad_campaign_product_report":
            rows = self._products(params, window)
            identity = "ad_id"
        elif tool_id == "ad_campaign_report":
            rows = self._campaigns(params, window)
            identity = "campaign_id"
        else:
            kind = "keyword" if tool_id == "ad_campaign_keyword_report" else "target"
            rows = self._things(kind, params, window)
            identity = "keyword_id" if kind == "keyword" else "target_id"
        page = int(str(params.get("page", 1)))
        length = int(str(params.get("length", 20)))
        chunk = rows[(page - 1) * length : page * length]
        summary = {identity: None, "campaign_id": None, "ad_group_id": None, "orders": "999"}
        result: dict[str, object] = {"rows": [summary, *chunk]}
        result["total"] = None if self.hide_total else len(rows) + self.total_bonus
        return result

    def _products(self, params: Mapping[str, object], window: str) -> list[dict[str, object]]:
        state = params.get("state")
        text = str(params.get("search_text") or "").upper()
        rows = []
        for ad in self.ads:
            if state is not None and ad.state != state:
                continue
            if text and text not in ad.asin.upper() and text not in ad.sku.upper():
                continue
            rows.append(
                {
                    "ad_id": ad.ad_id,
                    "asin": ad.asin,
                    "sku": ad.sku,
                    "campaign_id": ad.campaign_id,
                    "ad_group_id": ad.ad_group_id,
                    "sponsored_type": ad.sponsored_type,
                    "state": ad.state,
                    "campaign_state": self.campaign_state.get(ad.campaign_id, "enabled"),
                    "ad_group_state": ad.ad_group_state,
                    "title": ad.title,
                    "afn_fulfillable_quantity": ad.stock,
                    "listing_price": "19.99",
                    **self._metrics(window, ad.ad_id),
                }
            )
        return rows

    def _campaigns(self, params: Mapping[str, object], window: str) -> list[dict[str, object]]:
        asin = str(params.get("asin") or "").upper()
        state = params.get("state")
        ids = sorted({ad.campaign_id for ad in self.ads if not asin or ad.asin.upper() == asin})
        rows = []
        for campaign_id in ids:
            campaign_state = self.campaign_state.get(campaign_id, "enabled")
            if (state is not None and campaign_state != state) or (
                campaign_id in self.campaign_report_omits
            ):
                continue
            rows.append(
                {
                    "campaign_id": campaign_id,
                    "state": campaign_state,
                    "sponsored_type": "sp",
                    "is_apply_rule": False,
                    "is_apply_time": False,
                    "applied_templates": [],
                    "rule_group_uuids": [],
                    "optimization_rule_id": None,
                    "timing_base_value": None,
                    "step_budget_object_uuid": None,
                    "step_budget_template_uuid": None,
                    **self.campaign_flags.get(campaign_id, {}),
                    **self._metrics(window, campaign_id),
                }
            )
        return rows

    def _things(
        self, kind: str, params: Mapping[str, object], window: str
    ) -> list[dict[str, object]]:
        wanted = params.get("campaign_id")
        campaigns = set(wanted) if isinstance(wanted, list) else None
        rows = []
        for thing in self.things:
            if thing.kind != kind or (campaigns is not None and thing.campaign_id not in campaigns):
                continue
            identity = "keyword_id" if kind == "keyword" else "target_id"
            row: dict[str, object] = {
                identity: thing.object_id,
                "campaign_id": thing.campaign_id,
                "ad_group_id": thing.ad_group_id,
                "state": thing.state,
                "campaign_state": thing.campaign_state,
                "ad_group_state": thing.ad_group_state,
                "bid": thing.bid,
                "default_bid": thing.default_bid,
                "creation_date": thing.created,
                "is_apply_rule": False,
                "is_apply_time": False,
                "is_ad_group_apply_time": False,
                "is_apply_grab": False if kind == "keyword" else None,
                "applied_templates": [],
                "ad_group_applied_templates": [],
                "rule_group_uuids": [],
                "optimization_rule_id": None,
                "timing_base_value": None,
                **self._metrics(window, thing.object_id),
            }
            if kind == "keyword":
                row["keyword_text"] = thing.text
                row["match_type"] = thing.match_type
            else:
                row["targeting_text"] = thing.text
            for key, value in thing.flags.items():
                if value is MISSING:
                    row.pop(key, None)
                else:
                    row[key] = value
            rows.append(row)
        return rows


def small_shop() -> FakeLingxing:
    """一个商品（ASIN）在两个活动里各有一个广告组；另有一个别的商品和它共用第三个组。"""
    fake = FakeLingxing()
    fake.ads = [
        Ad("ad-1", ASIN, "cmp-1", "ag-1"),
        Ad("ad-2", ASIN, "cmp-2", "ag-2"),
        Ad("ad-3", ASIN, "cmp-3", "ag-3"),
        Ad("ad-4", "B0OTHER001", "cmp-3", "ag-3", sku="SKU-2"),
        Ad("ad-5", "B0OTHER001", "cmp-4", "ag-4", sku="SKU-2"),
    ]
    fake.things = [
        Thing("keyword", "kw-1", "cmp-1", "ag-1", text="cat scratcher"),
        Thing("keyword", "kw-2", "cmp-1", "ag-1", text="cat post", bid=None),
        Thing("target", "tg-1", "cmp-2", "ag-2", text='asinSameAs="B0RIVAL001"'),
        Thing("keyword", "kw-3", "cmp-3", "ag-3", text="shared word"),
        Thing("keyword", "kw-4", "cmp-4", "ag-4", text="other product word"),
    ]
    return fake
