"""领星搜索词数据源合同测试。

全部不触网：网络只可能从 LxMcpReadClient 出去，本文件从不实例化它。
夹具是手工合成的对抗性数据，不是真实响应的拷贝——真实数据里没有攻击性行，
而这些分支恰恰是最需要钉住的。ID 一律为合成值（SECURITY.md 零 Secret）。
"""

import json
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ads_control_plane.canonical.entity import AdProduct
from ads_control_plane.canonical.ids import new_canonical_id
from ads_control_plane.canonical.money import Money
from ads_control_plane.providers.lingxing.search_terms import (
    DEFAULT_PAGE_SIZE,
    TOOL_SEARCH_TERM_REPORT,
    LingxingProfileBinding,
    LingxingSearchTermSource,
)
from ads_control_plane.strategies import ports
from ads_control_plane.strategies.negation import (
    NegationParameterPack,
    generate_negation_candidates,
)
from ads_control_plane.strategies.ports import SearchTermFetch, SearchTermSourceError

AS_OF = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
PROFILE = "profile-synthetic-1"
ORG = new_canonical_id()
CONNECTION = new_canonical_id()


def make_binding(**overrides: object) -> LingxingProfileBinding:
    base: dict[str, object] = {
        "profile_external_id": PROFILE,
        "organization_id": ORG,
        "provider_connection_id": CONNECTION,
        "marketplace": "US",
        "shop_external_id": "sid-synthetic-9",
        "currency": "USD",
        "ad_product": AdProduct.SP,
    }
    base.update(overrides)
    return LingxingProfileBinding(**base)  # type: ignore[arg-type]


def term_row(**overrides: object) -> dict[str, object]:
    """一条数据行。字段名取自 2026-08-30 网关实测的搜索词报表返回。"""
    row: dict[str, object] = {
        "query": "cheap widget",
        "campaign_id": "1000000000000001",
        "ad_group_id": "2000000000000001",
        "target_match_type": "Broad",
        "impressions": 5000,
        "clicks": 40,
        "orders": 0,
        "spends": "35.00",
        "country": "US",
        # 0 = 这条搜索词是关键词，不是 ASIN。实测每个数据行都带这个字段，所以
        # 解析器把缺失当坏行——省掉它的夹具是在悄悄替源侧回答一个它没回答的问题。
        "is_asin": 0,
    }
    row.update(overrides)
    return row


def summary_row(**overrides: object) -> dict[str, object]:
    """汇总行：身份字段全 None，指标是**字符串**（实测如此，与数据行的 int 不同）。"""
    row: dict[str, object] = {
        "query": None,
        "campaign_id": None,
        "ad_group_id": None,
        "country": None,
        "portfolio_id": None,
        "clicks": "2437",
        "orders": "350",
        "spends": "1931.53",
    }
    row.update(overrides)
    return row


class FakeReadPort:
    """脚本化读端口，记录每次调用的 tool_id 与完整入参。"""

    def __init__(
        self,
        pages: list[list[dict[str, object]]],
        total: int | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._pages = pages
        self._total = total if total is not None else sum(len(p) for p in pages)
        self._raises = raises
        self.calls: list[tuple[str, dict[str, object]]] = []

    def fetch_page(self, tool_id: str, params: dict[str, object]) -> dict[str, object]:
        self.calls.append((tool_id, dict(params)))
        if self._raises is not None:
            raise self._raises
        page = params["page"]
        assert isinstance(page, int)
        rows = self._pages[page - 1] if page <= len(self._pages) else []
        return {"rows": rows, "total": self._total}


def make_source(
    pages: list[list[dict[str, object]]],
    total: int | None = None,
    binding: LingxingProfileBinding | None = None,
    **kwargs: object,
) -> tuple[LingxingSearchTermSource, FakeReadPort]:
    port = FakeReadPort(pages, total)
    bind = binding if binding is not None else make_binding()
    source = LingxingSearchTermSource(
        port,
        bindings={bind.profile_external_id: bind},
        **kwargs,  # type: ignore[arg-type]
    )
    return source, port


def fetch(source: LingxingSearchTermSource, lookback_days: int = 14) -> tuple:
    """只要记录。取数账目见 fetch_all——两者是同一次调用的两半。"""
    return source.fetch_search_term_performance(PROFILE, lookback_days, AS_OF).records


def fetch_all(source: LingxingSearchTermSource, lookback_days: int = 14) -> SearchTermFetch:
    return source.fetch_search_term_performance(PROFILE, lookback_days, AS_OF)


# ---------------------------------------------------------------- has_profile


def test_has_profile_answers_from_bindings_without_touching_network() -> None:
    source, port = make_source([[term_row()]])
    assert source.has_profile(PROFILE) is True
    assert source.has_profile("profile-not-bound") is False
    # 「接没接」这个问题不该花一次 QPS=1 的网关调用来回答。
    assert port.calls == []


def test_unbound_profile_fetch_refuses_with_code_and_fetches_nothing() -> None:
    source, port = make_source([[term_row()]])
    with pytest.raises(SearchTermSourceError) as exc:
        source.fetch_search_term_performance("profile-not-bound", 14, AS_OF)
    assert exc.value.code == "SEARCH_TERM_PROFILE_NOT_BOUND"
    assert port.calls == []


# ---------------------------------------------------------------- 分页


def test_pagination_walks_every_page_until_total_is_covered() -> None:
    pages = [[term_row(query=f"term-{page}-{i}") for i in range(2)] for page in range(3)]
    source, port = make_source(pages, total=6, page_size=2)
    records = fetch(source)
    assert len(port.calls) == 3
    assert [p["page"] for _, p in port.calls] == [1, 2, 3]
    assert len(records) == 6


def test_oversized_window_fails_after_a_single_page() -> None:
    """首页就判定超限：一次调用（约 6 秒）给出结论，而不是跑满 4 分钟再失败。"""
    source, port = make_source([[term_row()]], total=9999, max_rows=3000)
    with pytest.raises(SearchTermSourceError) as exc:
        fetch(source)
    assert exc.value.code == "SEARCH_TERM_RESULT_TOO_LARGE"
    assert len(port.calls) == 1


def test_page_budget_exceeded_raises_and_returns_nothing() -> None:
    """拉不全就抛错，绝不返回被截断的元组。

    截断在这里不是「看不全」而是「结论反向」：一个 (广告组, 词) 的行按匹配方式
    散落多页，被砍掉的那页若携带 orders=2，聚合出的 conversions 就是 0，
    于是制造一条把正在出单的词否定掉的候选。
    """
    pages = [[term_row(query=f"t{i}", ad_group_id=f"ag{i}")] for i in range(1, 12)]
    source, _ = make_source(pages, total=11, page_size=1, max_pages=3)
    with pytest.raises(SearchTermSourceError) as exc:
        fetch(source)
    assert exc.value.code == "SEARCH_TERM_PAGE_BUDGET_EXCEEDED"


def test_missing_total_is_refused_not_read_as_an_honest_zero() -> None:
    """total 缺席即拒。窗口分隔符写错时上游可能返回空结果而不是报错，
    而「空结果 + has_profile=true」会被读成「查了，这个店没有浪费」。"""
    port = FakeReadPort([[]], total=None)
    port._total = None  # noqa: SLF001
    source = LingxingSearchTermSource(port, bindings={PROFILE: make_binding()})
    with pytest.raises(SearchTermSourceError) as exc:
        fetch(source)
    assert exc.value.code == "SEARCH_TERM_TOTAL_ABSENT"


def test_rows_repeated_across_pages_are_counted_once() -> None:
    """按花费排序翻页时行的排名会移动，同一行可能在两页出现。
    而聚合是求和——重复行会把 clicks/spend 直接翻倍。"""
    row = term_row(clicks=10, spends="5.00")
    source, _ = make_source([[row], [dict(row)]], total=2, page_size=1)
    got = fetch_all(source)
    assert len(got.records) == 1
    assert got.records[0].clicks == 10
    assert got.records[0].spend.amount == Decimal("5.00")
    assert got.duplicate_rows == 1


# ---------------------------------------------------------------- 汇总行


def test_summary_row_is_skipped_and_never_becomes_a_search_term() -> None:
    """汇总行携带的是全店聚合值。若被 str(None) 兜底成一个叫 "None" 的词，
    它必然过任何证据门，且花费最大会排在导出 CSV 最前面，最先被人批准。"""
    source, _ = make_source([[summary_row(), term_row()]], total=2)
    got = fetch_all(source)
    records = got.records
    assert len(records) == 1
    assert records[0].search_term == "cheap widget"
    assert all(r.search_term != "None" for r in records)
    assert got.skipped_summary_rows == 1
    # 汇总行的全店花费不得出现在任何证据里。
    assert all(r.spend.amount != Decimal("1931.53") for r in records)


def test_summary_and_data_rows_parse_despite_differing_metric_types() -> None:
    """汇总行 clicks 是 str '2437'、数据行是 int 40——解析器绝不能靠类型区分它们，
    判据只看身份字段。"""
    source, _ = make_source([[summary_row(), term_row(clicks=40)]], total=2)
    got = fetch_all(source)
    assert got.records[0].clicks == 40
    assert got.skipped_summary_rows == 1


def test_a_row_missing_only_its_campaign_id_still_poisons_its_own_group() -> None:
    """缺活动 id ≠ 归不到任何组：组身份（广告组 + 词）就在这一行手里。

    此前它被当成「归不到任何组」直接丢掉，而那个组照样用剩下的行聚合出候选——
    这一行若携带 orders，聚合出的 conversions 就是缺了它之后的和，一个正在出单的词
    被提名否定，而证据行上写着干干净净的 conversions: 0。人从证据里看不出来。
    这正是 poisoned 那套机制存在的理由，只是入口不同。

    修法选的是「整组不判」而不是「从兄弟行借一个活动 id」：少提一个候选是可以承受的
    错误方向，提名一个正在出单的词不是。
    """
    orphan = term_row(campaign_id=None, orders=2, clicks=3, record_id="orphan")
    source, _ = make_source([[orphan, term_row(), healthy_row()]], total=3)
    got = fetch_all(source)
    assert [r.search_term for r in got.records] == ["healthy term"]
    assert got.unjudged_ad_group_terms == 1  # 组级：这个组这一轮没被判断
    assert got.unattributable_rows == 0  # 行级：它归得到组，不该算进「归属不明」
    # 组身份本身读不出来的行才算归属不明——两个数分开，因为它们数的不是一回事。
    other, _ = make_source([[term_row(ad_group_id=None, campaign_id=None), healthy_row()]], total=2)
    faceless = fetch_all(other)
    assert faceless.unattributable_rows == 1
    assert faceless.unjudged_ad_group_terms == 0


def test_a_group_poisoned_by_a_missing_campaign_id_still_reports_its_campaign() -> None:
    """被「缺活动 id」污染的组，活动 id 要从同组的好行里补齐。

    不补的后果是缺口对**按活动圈定**的授权书整个消失：UnjudgedGroup 的
    campaign_external_ids 为空 → 调用方按作用域筛时排除不掉也匹配不上 → 这个组
    不计入「作用域内有多少没判断」→ 卡片显示这一轮干干净净。而这个组明明就在
    他圈的那个活动下面——同组别的行上就写着活动 id，只有出问题那一行没写。
    污染这个组是对的（那一行可能带着订单），把它藏起来不是。
    """
    orphan = term_row(campaign_id=None, orders=2, record_id="orphan")
    source, _ = make_source([[orphan, term_row(), healthy_row()]], total=3)
    got = fetch_all(source)
    assert got.unjudged_ad_group_terms == 1
    (group,) = got.unjudged_groups
    assert group.ad_group_external_id == term_row()["ad_group_id"]
    assert group.campaign_external_ids == (term_row()["campaign_id"],)


def test_a_group_whose_every_row_lacks_a_campaign_id_says_so_with_an_empty_tuple() -> None:
    """一行都没写活动 id 时就是真的说不出来——空元组是如实的，不许编一个。

    调用方据此按「排除不了它落在圈里」处理（见 _unjudged_within_scope）。
    """
    key = {"ad_group_id": "2000000000000007", "query": "orphan term"}
    # 好行要够多：_assert_rows_are_usable 在坏行多于好行时会整批拒绝，
    # 那会把这条测试变成在测另一道闸。
    source, _ = make_source(
        [
            [
                term_row(campaign_id=None, record_id="a", **key),
                term_row(campaign_id=None, record_id="b", **key),
                term_row(),
                healthy_row(),
            ]
        ],
        total=4,
    )
    got = fetch_all(source)
    (group,) = got.unjudged_groups
    assert group.ad_group_external_id == "2000000000000007"
    assert group.campaign_external_ids == ()


# ---------------------------------------------------------------- 口径


@pytest.mark.parametrize(
    ("raw", "expect_zero"),
    [(0, True), ("0", True), ("0.0", True), ("", False), (None, False)],
)
def test_orders_parsing_never_reads_string_zero_as_a_conversion(
    raw: object, expect_zero: bool
) -> None:
    """最阴的一条：`if row.get("orders"):` 会把字符串 "0" 判为真，于是该词被当成
    「有转化」永不候选。全店大部分词都这样，看起来就像「这个店很干净」，零报错。

    空串 / None 是「该行不可判」，绝不当 0——「缺数据」变成「零转化」会直接制造候选。
    """
    # 配一条好行同行：整批一行也读不出来时会升级为 SEARCH_TERM_ROWS_UNUSABLE，
    # 而这里要钉的是**逐行**处置。真实批次本来也是好坏混杂。
    healthy = term_row(query="healthy term", ad_group_id="2000000000000002", record_id="ok")
    source, _ = make_source([[term_row(orders=raw, record_id="probe"), healthy]], total=2)
    got = fetch_all(source)
    records = got.records
    terms = {r.search_term for r in records}
    if expect_zero:
        assert "cheap widget" in terms
        assert next(r for r in records if r.search_term == "cheap widget").conversions == 0
    else:
        assert terms == {"healthy term"}  # 不可判的那行不产 record
        # 这行有身份、只是指标读不出来：它所在的那个 (广告组, 词) 整组不可判。
        assert got.unjudged_ad_group_terms == 1


def test_conversions_reads_orders_not_direct_orders() -> None:
    """Owner 裁决的锁：总订单（含间接归因），不是 direct_orders。"""
    source, _ = make_source([[term_row(orders=5, direct_orders=0, indirect_orders=5)]], total=1)
    records = fetch(source)
    assert records[0].conversions == 5


def healthy_row() -> dict[str, object]:
    """陪跑的好行：让整批不至于「一行也读不出来」而升级为拒绝。"""
    return term_row(query="healthy term", ad_group_id="2000000000000002", record_id="ok")


def test_thousands_separator_is_rejected_not_guessed() -> None:
    source, _ = make_source([[term_row(clicks="1,234"), healthy_row()]], total=2)
    got = fetch_all(source)
    assert [r.search_term for r in got.records] == ["healthy term"]
    assert got.unjudged_ad_group_terms == 1


def test_float_amount_is_refused() -> None:
    source, _ = make_source([[term_row(spends=35.0), healthy_row()]], total=2)
    got = fetch_all(source)
    assert [r.search_term for r in got.records] == ["healthy term"]
    assert got.unjudged_ad_group_terms == 1


# ---------------------------------------------------------------- 聚合


def test_match_type_split_rows_aggregate_into_one_record() -> None:
    source, _ = make_source(
        [
            [
                term_row(target_match_type="Broad", clicks=6, orders=0, spends="10.00"),
                term_row(target_match_type="Exact", clicks=7, orders=0, spends="12.50"),
            ]
        ],
        total=2,
    )
    records = fetch(source)
    assert len(records) == 1
    assert records[0].clicks == 13
    assert records[0].spend.amount == Decimal("22.50")


def test_split_rows_with_orders_on_one_side_do_not_produce_a_candidate() -> None:
    """反向候选回归——本轮最贵的缺陷。

    同一个 (广告组, 词) 分裂成两行，一行 orders=0、另一行 orders=2。若不聚合就判定，
    会得到一条「零转化」候选，把一个正在出单的词否定掉。
    """
    source, _ = make_source(
        [
            [
                term_row(target_match_type="Broad", clicks=30, orders=0, spends="30.00"),
                term_row(target_match_type="Exact", clicks=20, orders=2, spends="20.00"),
            ]
        ],
        total=2,
    )
    records = fetch(source)
    assert len(records) == 1
    assert records[0].conversions == 2
    pack = NegationParameterPack(
        lookback_days=14,
        min_spend=Money(amount="20.00", currency="USD"),
        min_clicks=25,
        max_data_staleness_hours=72,
    )
    result = generate_negation_candidates(records, pack, AS_OF, new_canonical_id)
    assert result.candidates == ()


def test_case_variants_merge_and_display_text_is_deterministic() -> None:
    """negation.py 的去重键用 casefold，所以 Widget 与 widget 必须在上游就合并，
    否则会撞上那条重复检查。展示文本必须确定性——候选集合的冻结 hash 绑定审批。"""
    rows = [
        term_row(query="Widget", target_match_type="Broad", clicks=5),
        term_row(query="widget", target_match_type="Exact", clicks=9),
    ]
    first, _ = make_source([rows], total=2)
    second, _ = make_source([list(reversed(rows))], total=2)
    a = fetch(first)
    b = fetch(second)
    assert len(a) == 1
    assert a[0].search_term == b[0].search_term  # 换输入顺序结果不变
    assert a[0].clicks == 14


def test_same_term_in_two_ad_groups_stays_separate() -> None:
    source, _ = make_source(
        [
            [
                term_row(ad_group_id="2000000000000001"),
                term_row(ad_group_id="2000000000000002"),
            ]
        ],
        total=2,
    )
    assert len(fetch(source)) == 2


def test_output_never_trips_the_duplicate_row_check() -> None:
    """适配器的输出直接喂给策略，永不触发 DUPLICATE_SEARCH_TERM_ROW。"""
    rows = [
        term_row(query="Widget", target_match_type="Broad"),
        term_row(query="widget", target_match_type="Exact"),
        term_row(query="widget", target_match_type="Phrase"),
    ]
    source, _ = make_source([rows], total=3)
    records = fetch(source)
    pack = NegationParameterPack(
        lookback_days=14,
        min_spend=Money(amount="20.00", currency="USD"),
        min_clicks=25,
        max_data_staleness_hours=72,
    )
    generate_negation_candidates(records, pack, AS_OF, new_canonical_id)  # 不抛即通过


def test_conflicting_campaign_for_one_ad_group_is_rejected() -> None:
    source, _ = make_source(
        [
            [
                term_row(campaign_id="1000000000000001"),
                term_row(campaign_id="1000000000000002", target_match_type="Exact"),
            ]
        ],
        total=2,
    )
    got = fetch_all(source)
    assert got.records == ()
    # 组级与行级分家：两种单位不并进同一个计数。这里两行都读得出来，只是互相矛盾。
    assert got.unjudged_ad_group_terms == 1
    assert got.unattributable_rows == 0


# ---------------------------------------------------------------- 时间


def test_window_backs_off_by_the_attribution_lag_and_is_pinned_verbatim() -> None:
    source, port = make_source([[term_row()]], total=1)
    fetch(source, lookback_days=14)
    # as_of = 2026-08-30，退 3 天 → 右端 08-27；闭区间 14 天 → 左端 08-14。
    assert port.calls[0][1]["report_date"] == "2026-08-14 - 2026-08-27"
    assert ports.ATTRIBUTION_LAG_DAYS == 3


def test_window_excludes_today_and_all_moments_are_tz_aware() -> None:
    source, _ = make_source([[term_row()]], total=1)
    record = fetch(source)[0]
    for moment in (record.window_start, record.window_end, record.data_as_of):
        assert moment.tzinfo is not None
    assert record.window_end.date() < AS_OF.date()


def default_pack(**overrides: object) -> NegationParameterPack:
    """服务端默认参数包（strategy_service.py 的 _DEFAULT_*）。

    测试必须用默认值，不能挑一个刚好能过的值——上一版这两条测试传的是白名单上限
    72h，于是「正常路径不得误 ABSTAIN」这句断言在默认档下从未被验证过，
    而真实通道恰恰是在默认档下 100% ABSTAIN。测试当时全绿。
    """
    base: dict[str, object] = {
        "lookback_days": 30,
        "min_spend": Money(amount="20.00", currency="USD"),
        "min_clicks": 25,
        "max_data_staleness_hours": 24,
    }
    base.update(overrides)
    return NegationParameterPack(**base)  # type: ignore[arg-type]


def test_data_as_of_is_the_fetch_moment_not_the_window_end() -> None:
    """窗口右端答的是「覆盖到哪天」，data_as_of 答的是「有多陈」，两个问题不同。

    混为一谈的代价 2026-08-30 实测过：窗口右端被归因滞后刻意退了 3 天，
    now - window_end 恒落在 [48h, 72h)，默认门槛 24h，真实店 3815 条 100% ABSTAIN。
    Mock 源一直是对的（local_demo.py:169 取 now-2h，window_end 另取 now-1d）。
    """
    source, _ = make_source([[term_row()]], total=1)
    record = fetch(source)[0]
    assert record.data_as_of == AS_OF
    assert record.window_end == datetime(2026, 8, 28, tzinfo=UTC)
    assert record.data_as_of != record.window_end


def test_default_parameter_pack_does_not_abstain_on_the_normal_path() -> None:
    """这条才是上一版漏掉的验收标准：**默认档**下正常路径不得误 ABSTAIN。

    刻意等 3 天让订单结算，不叫数据陈旧；把一个设计参数塞进新鲜度门，
    量出来的是常数，而常数不是测量。
    """
    source, _ = make_source([[term_row()]], total=1)
    result = generate_negation_candidates(
        list(fetch(source)), default_pack(), AS_OF, new_canonical_id
    )
    assert result.abstains == ()


def test_every_allowed_staleness_setting_is_usable_on_a_fresh_fetch() -> None:
    """白名单允许 [1,72]。上一版实现下 [1,47] 可证明恒失败——一个把无效取值
    摆在人面前的范围，比没有范围更坏：人以为自己调了，其实什么都没调。"""
    source, _ = make_source([[term_row()]], total=1)
    records = list(fetch(source))
    for hours in (1, 24, 47, 48, 72):
        result = generate_negation_candidates(
            records, default_pack(max_data_staleness_hours=hours), AS_OF, new_canonical_id
        )
        assert result.abstains == (), f"max_data_staleness_hours={hours} 在新鲜取数上误 ABSTAIN"


def test_stale_window_reaches_the_abstain_channel() -> None:
    """证明 STALE_DATA 仍然可达——它存在的全部意义就是让「无法判断」不被静默
    当成「没有候选」。可达的那种情况是缓存的取数结果过老：记录冻结时把取数时刻
    一并冻住，缓存命中时它跟着一起变旧。"""
    source, _ = make_source([[term_row()]], total=1)
    record = fetch(source)[0]
    much_later = AS_OF + timedelta(hours=25)
    result = generate_negation_candidates([record], default_pack(), much_later, new_canonical_id)
    assert result.candidates == ()
    assert [a.reason.value for a in result.abstains] == ["STALE_DATA"]


def test_naive_as_of_is_refused() -> None:
    source, port = make_source([[term_row()]], total=1)
    with pytest.raises(SearchTermSourceError) as exc:
        source.fetch_search_term_performance(PROFILE, 14, AS_OF.replace(tzinfo=None))
    assert exc.value.code == "SEARCH_TERM_CLOCK_NAIVE"
    assert port.calls == []


# ---------------------------------------------------------------- 身份


def test_leading_zero_ids_survive_byte_for_byte() -> None:
    """AX-01：数值化的外部 ID 会丢前导零。真实抽样里未必有这种 ID，
    所以夹具里故意放一个。"""
    source, _ = make_source(
        [[term_row(ad_group_id="000000123456789012", campaign_id="000000999")]], total=1
    )
    record = fetch(source)[0]
    assert record.scope.entity_external_id == "000000123456789012"
    assert record.scope.parent_refs.campaign_external_id == "000000999"


def test_numeric_id_is_rejected_not_silently_stringified() -> None:
    """数字化 ID 的前导零已经丢了，接受等于把 AX-01 违规藏起来。"""
    source, _ = make_source([[term_row(ad_group_id=286101123467242), healthy_row()]], total=2)
    got = fetch_all(source)
    assert [r.search_term for r in got.records] == ["healthy term"]
    assert got.unattributable_rows == 1


def test_overlong_id_is_rejected_as_a_bad_row_without_killing_the_batch() -> None:
    source, _ = make_source(
        [[term_row(ad_group_id="9" * 300), term_row(ad_group_id="2000000000000002")]], total=2
    )
    got = fetch_all(source)
    assert len(got.records) == 1  # 一行坏数据不该让整店失去决策能力
    assert got.unjudged_ad_group_terms == 1


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_a_blank_query_is_a_bad_row_not_a_search_term_made_of_spaces(blank: str) -> None:
    """空的 / 只有空白的搜索词，绝不能变成一条记录。

    它落进来的话，导出的 CSV 里就有一行「否定精准关键词 = 几个空格」——人照着抄进
    领星，加出来的是一条什么都挡不住的否定词，而证据行上花费点击一切正常。
    今天挡住它的是 _opt_text 的 `return text or None`（strip 后为空即当没有）；
    这条测试钉的就是那一句：哪天它改成原样返回，这里立刻红。
    """
    source, _ = make_source([[term_row(query=blank), term_row(query="widget holder")]], total=2)
    got = fetch_all(source)
    assert [r.search_term for r in got.records] == ["widget holder"]
    #: 落在 unattributable_rows 而不是 unjudged_ad_group_terms——两个桶不是一回事：
    #  后者数的是「认得出是哪个 (广告组, 词)、只是这轮判不了」，而这一行连归属都读
    #  不出来，凑不成一个组。放错桶会让界面把「有一行读废了」说成「有一组没判断」。
    assert got.unattributable_rows == 1, "空搜索词那一行必须作为读不出归属的行说得出口"
    assert got.unjudged_ad_group_terms == 0


def test_a_row_with_neither_query_nor_ad_group_is_a_summary_row_not_a_bad_row() -> None:
    """两个身份字段都空 = 汇总行，跳过并计数；只空一个才是坏行。

    与上一条是同一个判据的两侧：分不开的话，要么把汇总行算成「有东西没判断」
    （红灯永远不灭），要么把畸形行当汇总行悄悄扔掉（浪费从此无人再提）。
    """
    source, _ = make_source(
        [[term_row(query="", ad_group_id=""), term_row(query="widget holder")]], total=2
    )
    got = fetch_all(source)
    assert [r.search_term for r in got.records] == ["widget holder"]
    assert got.unjudged_ad_group_terms == 0, "汇总行不该被算成没判断的组"
    assert got.unattributable_rows == 0, "汇总行也不该被算成读不出归属的行"


def test_scope_carries_the_binding_identity_not_row_guesses() -> None:
    source, _ = make_source([[term_row()]], total=1)
    scope = fetch(source)[0].scope
    assert scope.shop_external_id == "sid-synthetic-9"
    assert scope.profile_external_id == PROFILE
    assert scope.marketplace == "US"
    assert scope.ad_product is AdProduct.SP


def test_binding_currency_is_stamped_and_never_defaulted() -> None:
    source, _ = make_source([[term_row()]], total=1, binding=make_binding(currency="EUR"))
    assert fetch(source)[0].spend.currency == "EUR"


# ---------------------------------------------------------------- 下推治理


def test_rule_definition_and_evidence_floors_are_never_pushed_down() -> None:
    """规则定义下推 = 把「什么叫 0 订单」交给上游；阈值下推 = 逐行切割会让聚合后
    本该合格的候选整组消失。两者都不许，降量只靠 targeted_type。"""
    source, port = make_source([[term_row()]], total=1)
    fetch(source)
    params = port.calls[0][1]
    for forbidden in ("orders", "category", "group_type", "clicks", "spends", "with_ring"):
        assert forbidden not in params, f"{forbidden} 不得下推"
    assert params["targeted_type"] == "not_negatived"
    assert port.calls[0][0] == TOOL_SEARCH_TERM_REPORT


# ---------------------------------------------------------------- 覆盖数与页大小


def test_summary_rows_do_not_inflate_coverage_and_truncate_the_last_page() -> None:
    """汇总行不计入上游 total，按整页行数累加就每页多算 1，末页会被跳过。

    实测：total=2090 而每页返回 101 行。这条测的不是「少几行看不全」——被跳过那页
    若携带某个词的 orders=2，聚合出的 conversions 就是 0，正在出单的词被判死刑，
    而证据行上一切正常。夹具刻意让页大小除不尽 total（3/3/2），因为整除时
    多算与不多算恰好同时到达终点，缺陷测不出来。
    """
    pages = [
        [summary_row()] + [term_row(query=f"term-{page}-{i}") for i in range(count)]
        for page, count in enumerate((3, 3, 2))
    ]
    source, port = make_source(pages, total=8, page_size=3)
    records = fetch(source)
    assert [p["page"] for _, p in port.calls] == [1, 2, 3]
    assert len(records) == 8


def test_page_size_defaults_high_enough_to_finish_inside_a_client_timeout() -> None:
    """页大小是可用性参数，不是调优参数。

    2026-08-30 实测单页耗时几乎与页大小无关（100 行 4.2s，1000 行 4.9s）——成本在
    网关往返而不在传输。真实店 2090 行按 100/页要 21 页 ≈ 88 秒，MCP 客户端等不到
    返回就超时，工具在真实店铺上直接不可用。
    """
    source, port = make_source([[term_row()]], total=1)
    fetch(source)
    assert port.calls[0][1]["length"] == DEFAULT_PAGE_SIZE
    assert DEFAULT_PAGE_SIZE >= 1000


# ---------------------------------------------------------------- 缓存


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_repeat_calls_in_the_ttl_window_do_not_touch_the_gateway_again() -> None:
    """缓存挡的是权限缺口，不是延迟。

    generate_negation_candidate_set 是 AI 可调用工具，而即席模式（不带 mandate_id）
    没有任何配额——assert_run_authorized 的日运行数与最小间隔只在授权书模式下生效。
    没有缓存，AI 客户端连调 20 次就是 20 次对领星生产 API 的多页读取。
    """
    clock = Clock()
    source, port = make_source([[term_row()]], total=1, cache_ttl_seconds=900.0, monotonic=clock)
    first = fetch(source)
    calls_after_first = len(port.calls)
    clock.now += 899.0
    second = fetch_all(source)
    assert len(port.calls) == calls_after_first
    assert second.served_from_cache is True
    assert second.records == first


def test_cache_expires_and_refetches() -> None:
    clock = Clock()
    source, port = make_source([[term_row()]], total=1, cache_ttl_seconds=900.0, monotonic=clock)
    fetch(source)
    clock.now += 900.0
    assert fetch_all(source).served_from_cache is False
    assert len(port.calls) == 2


def test_a_different_window_is_never_served_from_another_windows_cache() -> None:
    """窗口是 key 的一部分。少了它，改 lookback_days 会拿旧窗口的数字糊弄人——
    人以为自己换了口径，看到的还是上一次的结论。"""
    clock = Clock()
    source, port = make_source([[term_row()]], total=1, cache_ttl_seconds=900.0, monotonic=clock)
    fetch(source, lookback_days=14)
    fetch(source, lookback_days=30)
    assert len(port.calls) == 2
    windows = {p["report_date"] for _, p in port.calls}
    assert len(windows) == 2


# ---------------------------------------------------------------- 行身份


def test_two_keywords_matching_one_query_are_not_collapsed_into_a_duplicate() -> None:
    """同广告组下两个关键词命中同一条顾客搜索词——这不是重复行。

    2026-08-30 实测（真实店，单页 500 行）：按
    (campaign, ad_group, query, target_match_type) 去重，同一页内就有 4 组碰撞，
    被丢掉的 4 行合计携带 orders=4、clicks=33。抹掉 orders 之后聚合出的
    conversions 是 0，于是正在出单的词被提名否定——而证据行上写着干干净净的
    conversions: 0，人从证据里看不出来。这是整套设计里最贵的那个缺陷。
    """
    shared = {"query": "cheap widget", "target_match_type": "Broad"}
    rows = [
        term_row(**shared, record_id="r-1", keyword_id="k-1", clicks=30, orders=0, spends="30.00"),
        term_row(**shared, record_id="r-2", keyword_id="k-2", clicks=3, orders=2, spends="4.00"),
    ]
    source, _ = make_source([rows], total=2)
    got = fetch_all(source)
    records = got.records
    assert got.duplicate_rows == 0  # 一行都不该被当成重复
    assert len(records) == 1  # 聚合到同一个 (广告组, 词)
    assert records[0].conversions == 2  # 订单没有被抹掉
    assert records[0].clicks == 33
    assert records[0].spend.amount == Decimal("34.00")


def test_the_same_row_on_two_pages_is_still_one_row() -> None:
    """跨页重复仍要去掉：按花费排序翻页时行排名会移动，同一行可能在两页各出现一次。
    record_id 是源侧行主键（实测 32 字符、同页两次取回集合与顺序完全一致），
    重复行会带着同一个 id 回来。"""
    row = term_row(record_id="r-7", clicks=10, spends="5.00")
    source, _ = make_source([[row], [dict(row)]], total=2, page_size=1)
    got = fetch_all(source)
    assert len(got.records) == 1
    assert got.records[0].clicks == 10
    assert got.duplicate_rows == 1


def test_cross_page_duplicates_do_not_end_paging_early() -> None:
    """重复行撑高「已覆盖」计数 → data_rows >= total 提前成立 → 末页从未拉取。

    与汇总行多计是同一类缺陷：文件自己写着「宁可少算，绝不多算」。
    """
    dup = term_row(record_id="dup", query="repeat me")
    pages = [
        [dup, term_row(record_id="a", query="a")],
        [dict(dup), term_row(record_id="b", query="b")],
        [term_row(record_id="c", query="c")],
    ]
    source, port = make_source(pages, total=4, page_size=2)  # 去重后确有 4 行
    records = fetch(source)
    assert [p["page"] for _, p in port.calls] == [1, 2, 3]
    assert len(records) == 4


# ---------------------------------------------------------------- 「缺数据」不得变「零转化」


def test_a_group_with_an_unreadable_row_produces_no_candidate() -> None:
    """坏行被跳过，它所在的组照常求和——这就是「缺数据」在聚合层变回「零转化」。

    读不出来的那行可能正带着订单。少提一个候选是可以承受的错误方向，
    提名一个正在出单的词不是。与去重键过粗是同一个病，只是入口不同。
    """
    shared = {"query": "cheap widget", "ad_group_id": "2000000000000001"}
    rows = [
        term_row(**shared, record_id="good", clicks=30, orders=0, spends="30.00"),
        term_row(**shared, record_id="bad", clicks=5, orders="N/A", spends="6.00"),
        healthy_row(),
    ]
    source, _ = make_source([rows], total=3)
    got = fetch_all(source)
    assert [r.search_term for r in got.records] == ["healthy term"]
    assert got.unjudged_ad_group_terms == 1


def test_a_group_whose_only_row_is_unreadable_still_counts_as_unjudged() -> None:
    """越是彻底读不出来的组，越不能消失得越干净。

    坏行不进 parsed，于是一个**所有行都坏**的组在 groups 里根本没有键：靠遍历
    groups 来数「丢了几组」时，循环永远碰不到它。它会连「被丢掉」这件事都不留痕迹，
    只在行计数里留下一个数不清归属的数字——而这正是最该被说出来的那种丢失。
    """
    lonely = term_row(
        query="mystery term", ad_group_id="2000000000000009", record_id="lonely", orders="N/A"
    )
    source, _ = make_source([[lonely, healthy_row()]], total=2)
    got = fetch_all(source)
    assert [r.search_term for r in got.records] == ["healthy term"]
    assert got.unjudged_ad_group_terms == 1
    assert got.is_complete is False


def test_the_row_account_balances_against_source_total() -> None:
    """行级账目必须填平 source_total，否则会有一批行在账上凭空消失。

    此前导出的行级计数只有「汇总行 / 重复行 / 归属不明的行」——**有身份但指标读不
    出来**的行一个桶都不占。读响应的人按账目相减必得「行全部可用」，而被丢掉的
    恰恰是可能携带订单的那些；`_assert_rows_are_usable` 只在坏行过半时才拦，
    三成静默通过。
    """
    rows = [
        summary_row(),  # 汇总行
        term_row(record_id="dup", clicks=3),  # 与下一行重复
        term_row(record_id="dup", clicks=3),
        term_row(
            query="bad metric", ad_group_id="2000000000000002", orders="N/A"
        ),  # 有身份、读不出
        term_row(query="no identity", ad_group_id=None, campaign_id=None),  # 归属不明
        healthy_row(),
    ]
    source, _ = make_source([rows], total=len(rows))
    got = fetch_all(source)
    assert got.source_total == len(rows)
    assert (
        got.skipped_summary_rows + got.duplicate_rows + got.unreadable_rows + got.usable_rows
        == got.source_total
    )
    # 每一格都得对得上号，否则「填平」可以靠把差额塞进任意一格来伪造。
    assert got.skipped_summary_rows == 1
    assert got.duplicate_rows == 1
    assert got.unreadable_rows == 2  # 指标读不出的 + 归属不明的
    assert got.unattributable_rows == 1  # 其中归属不明的那一行
    assert got.usable_rows == 2


def test_a_fetch_with_nothing_thrown_away_says_so() -> None:
    """is_complete 是「这一轮全都判断过了」的唯一凭据，好路径上必须为真——
    否则界面会挂上一条永远不消失的「有东西没被判断」，那句话在好路径上是假的。"""
    source, _ = make_source([[term_row()]], total=1)
    got = fetch_all(source)
    assert got.records
    assert got.is_complete is True
    assert (got.unjudged_ad_group_terms, got.unattributable_rows) == (0, 0)


def test_all_rows_unreadable_is_refused_not_reported_as_a_clean_store() -> None:
    """上游给了行、一行也读不出来时返回空元组，和「这个店确实没有浪费」长得一样。

    调用方看到 profile_has_data_source=true、候选为 0，读出来是
    「查了，很干净」。runtime-2 写死的那句在这里换了个入口：这次伪装的是
    「读不动数据」。
    """
    source, _ = make_source([[term_row(spends="N/A"), term_row(query="b", spends="N/A")]], total=2)
    with pytest.raises(SearchTermSourceError) as exc:
        fetch(source)
    assert exc.value.code == "SEARCH_TERM_ROWS_UNUSABLE"


def test_mostly_unreadable_batch_is_refused() -> None:
    """一半以上的行读不出来时，剩下那些算出的「零转化」不足以支撑否定决定。"""
    rows = [term_row(query=f"bad-{i}", record_id=f"b{i}", spends="N/A") for i in range(3)]
    rows.append(healthy_row())
    source, _ = make_source([rows], total=4)
    with pytest.raises(SearchTermSourceError) as exc:
        fetch(source)
    assert exc.value.code == "SEARCH_TERM_ROWS_UNUSABLE"


# ---------------------------------------------------------------- 上游异常翻译


class CodedUpstreamError(Exception):
    """形状照抄 adapters.lx_read.LxReadError：带 .code 的上游异常。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def test_upstream_error_keeps_its_code_instead_of_bare_crashing() -> None:
    """裸穿的后果是本仓库注释里写明「修过一次、不许再犯」的那条：
    strategy_service 只接得住 SearchTermSourceError，server.py 的 _coded 只翻
    ToolDenied，于是 MCP 面只剩一句 Error executing tool——调用方分不清
    「超时可重试」和「参数签错了永远不会成功」，会去反复重试一个永远失败的调用。
    LX_TRANSPORT_ERROR 这些码已经在 UI 词典里，原样保留人才查得到。"""
    port = FakeReadPort([[term_row()]], total=1, raises=CodedUpstreamError("LX_TRANSPORT_ERROR"))
    source = LingxingSearchTermSource(port, bindings={PROFILE: make_binding()})
    with pytest.raises(SearchTermSourceError) as exc:
        fetch(source)
    assert exc.value.code == "LX_TRANSPORT_ERROR"


def test_uncoded_upstream_error_still_arrives_with_a_code() -> None:
    port = FakeReadPort([[term_row()]], total=1, raises=RuntimeError("socket exploded"))
    source = LingxingSearchTermSource(port, bindings={PROFILE: make_binding()})
    with pytest.raises(SearchTermSourceError) as exc:
        fetch(source)
    assert exc.value.code == "SEARCH_TERM_UPSTREAM_ERROR"


# ---------------------------------------------------------------- 诊断不串味（#2）


class _TwoProfilePort:
    """同一个 source 实例服务两个店铺；每个店铺自己的行数与行内容各不相同。"""

    def __init__(self, by_profile: dict[str, tuple[list[dict[str, object]], int]]) -> None:
        self._by_profile = by_profile
        self.calls: list[str] = []

    def fetch_page(self, tool_id: str, params: dict[str, object]) -> dict[str, object]:
        profile_ids = params["profile_ids"]
        assert isinstance(profile_ids, list)
        pid = str(profile_ids[0])
        self.calls.append(pid)
        rows, total = self._by_profile[pid]
        page = params["page"]
        assert isinstance(page, int)
        return {"rows": rows if page == 1 else [], "total": total}


def _two_profile_source(
    by_profile: dict[str, tuple[list[dict[str, object]], int]],
) -> tuple[LingxingSearchTermSource, _TwoProfilePort]:
    port = _TwoProfilePort(by_profile)
    bindings = {
        pid: make_binding(profile_external_id=pid, shop_external_id=f"sid-{pid}")
        for pid in by_profile
    }
    return LingxingSearchTermSource(port, bindings=bindings), port  # type: ignore[arg-type]


class _LatchRow(dict):  # type: ignore[type-arg]
    """一行数据，但在**取数已结束、解析刚开始**时把控制权交出去一次。

    这个位置不是随便挑的：旧实现在 _fetch_all_rows 的最后一行把 source_total 存进
    实例属性，然后由 _assert_rows_are_usable 读回来。两者之间只隔着 _parse_rows，
    所以只有解析阶段能插进另一个线程——而端口返回什么 Mapping 都合法，
    这不需要在生产代码里留任何测试钩子。

    `armed` 由端口在返回最后一页（空页）时置位；在那之前的读（分页循环里的汇总行
    判定）一律放行，否则闸会在 source_total 还没写进去时就触发，测不到那段窗口。
    """

    def __init__(self, data: dict[str, object], armed: list[bool], on_ready: object) -> None:
        super().__init__(data)
        self._armed = armed
        self._on_ready = on_ready
        self._fired = False

    def get(self, key: object, default: object = None) -> object:  # type: ignore[override]
        if self._armed[0] and not self._fired:
            self._fired = True
            self._on_ready()  # type: ignore[operator]
        return super().get(key, default)  # type: ignore[arg-type]


class _ArmingPort(_TwoProfilePort):
    """返回 `arm_after` 这个店的最后一页（空页）时置位闩锁。"""

    def __init__(
        self,
        by_profile: dict[str, tuple[list[dict[str, object]], int]],
        armed: list[bool],
        arm_after: str,
    ) -> None:
        super().__init__(by_profile)
        self._armed = armed
        self._arm_after = arm_after

    def fetch_page(self, tool_id: str, params: dict[str, object]) -> dict[str, object]:
        result = super().fetch_page(tool_id, params)
        profile_ids = params["profile_ids"]
        assert isinstance(profile_ids, list)
        if str(profile_ids[0]) == self._arm_after and not result["rows"]:
            self._armed[0] = True
        return result


def test_a_concurrent_empty_shop_cannot_disarm_another_shops_unusable_gate() -> None:
    """并发下这道闸曾会反向失效，安静地返回空元组。

    闸挡的是「上游给了行、一行也读不出来」被当成「查了，很干净」。它此前从**实例
    属性**读 source_total，而同一个 source 实例服务全部店铺。本测试把两次取数按
    真实会发生的顺序交错：B 店取完 3000 行（全不可读）、还没跑到闸门时，A 店
    （本窗口 0 行）整轮跑完并把 source_total 置成 0；B 恢复后读到的就是那个 0，
    于是直接放行、返回空元组——而它手里 3000 行一行都没读出来。

    交错是确定性的（靠 Event 与闩锁，不靠时序），跑一次就能判定，不是概率性冒烟。
    """
    b_parsing = threading.Event()
    a_done = threading.Event()
    armed = [False]

    def hand_over() -> None:
        b_parsing.set()
        assert a_done.wait(timeout=5), "A 店那一轮没有在预期内跑完"

    unreadable: list[dict[str, object]] = [
        _LatchRow(term_row(spends="N/A", orders=""), armed, hand_over),
        term_row(spends="N/A", orders="", query="another unreadable"),
    ]
    by_profile = {
        "shop-empty": ([], 0),  # 接了、查了、这个窗口确实一行都没有
        "shop-broken": (unreadable, 3000),  # 上游说有 3000 行，一行也读不出来
    }
    port = _ArmingPort(by_profile, armed, arm_after="shop-broken")
    bindings = {
        pid: make_binding(profile_external_id=pid, shop_external_id=f"sid-{pid}")
        for pid in by_profile
    }
    source = LingxingSearchTermSource(port, bindings=bindings)  # type: ignore[arg-type]

    def run_empty_shop() -> None:
        assert b_parsing.wait(timeout=5), "B 店没有走到解析阶段"
        source.fetch_search_term_performance("shop-empty", 14, AS_OF)
        a_done.set()

    result: dict[str, object] = {}

    def run_broken_shop() -> None:
        try:
            result["value"] = source.fetch_search_term_performance("shop-broken", 14, AS_OF).records
        except SearchTermSourceError as exc:
            result["code"] = exc.code

    threads = [threading.Thread(target=run_broken_shop), threading.Thread(target=run_empty_shop)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    a_done.set()  # 兜底解锁，避免测试失败时挂住

    assert b_parsing.is_set(), "交错没有发生，这条测试等于没测"
    assert "value" not in result, "读不动数据的店返回了空元组——那与「查了，很干净」逐字同形"
    assert result.get("code") == "SEARCH_TERM_ROWS_UNUSABLE"


def test_each_shop_gets_its_own_accounting_not_the_previous_shops() -> None:
    """两个店共用一个 source：账目跟着这一次取数的记录一起回来，各是各的。

    账目此前挂在 `last_*` 实例属性上，一个槽位服务全部店铺——并发时后到的覆盖先到的，
    调用方读到的是另一个店的数字，全都在、全都不对。现在它随 SearchTermFetch 返回，
    「读到的账目属于哪次取数」成了结构保证而不是时序运气。这里仍要钉住的是缓存：
    缓存按 (profile, 窗口) 存账目，键错了小店就会继承大店的那个汇总行。
    """
    source, _ = _two_profile_source(
        {
            "shop-big": ([term_row(), summary_row()], 1),
            "shop-small": ([term_row(query="only one")], 1),
        }
    )
    big = source.fetch_search_term_performance("shop-big", 14, AS_OF)
    small = source.fetch_search_term_performance("shop-small", 14, AS_OF)
    assert big.skipped_summary_rows == 1
    assert small.skipped_summary_rows == 0  # 小店没有汇总行，不该继承大店的 1


def test_cache_hit_returns_that_entrys_own_accounting() -> None:
    """命中缓存时账目要来自**这条缓存自己的**那一份，不是最近一次真实取数的。

    缓存是账目现在唯一的跨调用状态：键错或取错，界面就会拿 A 店的行数去描述
    B 店这次运行，数字全都在、全都不对，且没有任何迹象表明它们不对。
    """
    source, port = _two_profile_source(
        {
            "shop-big": ([term_row(), summary_row()], 1),
            "shop-small": ([term_row(query="only one")], 1),
        }
    )
    source.fetch_search_term_performance("shop-big", 14, AS_OF)
    source.fetch_search_term_performance("shop-small", 14, AS_OF)
    calls_before = len(port.calls)
    again = source.fetch_search_term_performance("shop-big", 14, AS_OF)  # 命中缓存
    assert len(port.calls) == calls_before  # 确实没再取数
    assert again.served_from_cache is True
    assert again.skipped_summary_rows == 1  # 大店自己的那个汇总行
    assert again.source_total == 1


def test_fresh_fetch_clears_the_cache_flag() -> None:
    source, _ = _two_profile_source(
        {
            "shop-a": ([term_row()], 1),
            "shop-b": ([term_row(query="other")], 1),
        }
    )
    source.fetch_search_term_performance("shop-a", 14, AS_OF)
    assert source.fetch_search_term_performance("shop-a", 14, AS_OF).served_from_cache is True
    assert source.fetch_search_term_performance("shop-b", 14, AS_OF).served_from_cache is False


# ---------------------------------------------------------------- 名称随候选走


def test_names_come_from_the_same_rows_as_the_metrics() -> None:
    """活动/广告组名就在搜索词报表行里，此前被丢掉，再从镜像去捞。

    2026-08-30 实测：真实店 11,723 个活动，镜像默认只拉 3 页 300 个（2.5%），
    于是 7 条候选 0 条解析得出名字——导出的 CSV 表头承诺了 campaign_name /
    ad_group_name，交出的是两列空白，而那两列正是为免除「拿 15 位数字去后台逐行
    反查」才加的。名字与指标出自同一行，一起带出来即可。
    """
    source, _ = make_source(
        [[term_row(campaign_name="Brand-Defense", ad_group_name="Exact-Core")]], total=1
    )
    records = fetch(source)
    assert len(records) == 1
    assert records[0].campaign_name == "Brand-Defense"
    assert records[0].ad_group_name == "Exact-Core"


def test_missing_names_stay_none_and_are_not_invented() -> None:
    source, _ = make_source([[term_row()]], total=1)
    records = fetch(source)
    assert records[0].campaign_name is None
    assert records[0].ad_group_name is None


def test_name_choice_is_deterministic_across_row_order() -> None:
    """名字随候选冻结、进 set_hash，而 hash 绑定审批——两次运行必须得到同一个。

    「取第一个遇到的」会随行顺序变化。组内各行的名字理应相同（同一个广告组），
    但实测 3.2% 为 null，且不排除源侧不一致。
    """
    rows = [
        term_row(target_match_type="Broad", campaign_name="Zeta", clicks=5),
        term_row(target_match_type="Phrase", campaign_name="Alpha", clicks=9),
        term_row(target_match_type="Exact", campaign_name=None, clicks=1),
    ]
    first, _ = make_source([rows], total=3)
    second, _ = make_source([list(reversed(rows))], total=3)
    a = fetch(first)
    b = fetch(second)
    assert len(a) == 1 and len(b) == 1
    assert a[0].campaign_name == b[0].campaign_name == "Alpha"  # 字典序最小的非空值


# ------------------------------------------------------------------- is_asin


def test_an_asin_row_is_carried_through_as_an_asin_not_a_keyword() -> None:
    """`is_asin` 早就在每个数据行上，此前一路被丢掉。

    丢掉的后果不是少一条候选：这个词照样成为候选，人照着 CSV 在领星加一条
    否定精准关键词，而 ASIN 型来源根本不看关键词否定——钱继续烧，
    证据行上花费、点击、零转化样样属实，人从证据里看不出自己白做了。
    """
    source, _ = make_source([[term_row(is_asin=1), healthy_row()]], total=2)
    got = fetch_all(source)
    by_term = {r.search_term: r for r in got.records}
    assert by_term["cheap widget"].term_is_asin is True
    assert by_term["healthy term"].term_is_asin is False


def test_a_data_row_without_is_asin_is_a_bad_row_not_a_silent_keyword() -> None:
    """缺 `is_asin` 时默认成「不是 ASIN」，就是把本轮要修的那个静默失效换个位置再犯一次。

    实测每个数据行都带这个字段（null 率 3.2% 恰等于汇总行占比），所以拒绝不会误伤
    正常数据；真出现了，走的是已有的坏行路径——计数、污染本组，说得出口。
    """
    source, _ = make_source([[term_row(is_asin=None), healthy_row()]], total=2)
    got = fetch_all(source)
    assert [r.search_term for r in got.records] == ["healthy term"]
    assert got.unjudged_ad_group_terms == 1
    assert got.unreadable_rows == 1


def test_an_unparseable_is_asin_is_refused_not_guessed() -> None:
    source, _ = make_source([[term_row(is_asin="maybe"), healthy_row()]], total=2)
    got = fetch_all(source)
    assert [r.search_term for r in got.records] == ["healthy term"]
    assert got.unreadable_rows == 1


def test_any_nonzero_flag_counts_as_an_asin() -> None:
    """抽样 30 行全是 0，未观测到 1——我们只确知 0 表示「不是」。

    所以判据是「非零即 ASIN」，不是「等于 1 才 ASIN」：后者会让源侧换个非零编码
    就静默退回原缺陷，而那正是没有任何断言会变红的那种退回。
    """
    source, _ = make_source([[term_row(is_asin=2), healthy_row()]], total=2)
    got = fetch_all(source)
    assert {r.search_term: r.term_is_asin for r in got.records}["cheap widget"] is True


def test_one_asin_row_makes_the_whole_group_an_asin() -> None:
    """组内不一致时宁可判成 ASIN。

    两种误判不对等：判成关键词让人加一条挡不住任何东西的否定词（白做工，钱继续烧），
    判成 ASIN 只是少提一个候选并如实说明原因。
    """
    source, _ = make_source(
        [
            [
                term_row(is_asin=0, record_id="a", target_match_type="Broad"),
                term_row(is_asin=1, record_id="b", target_match_type="Exact"),
                healthy_row(),
            ]
        ],
        total=3,
    )
    got = fetch_all(source)
    assert {r.search_term: r.term_is_asin for r in got.records}["cheap widget"] is True


def test_the_strict_is_asin_parse_still_matches_the_recorded_evidence() -> None:
    """解析器凭什么敢把缺 `is_asin` 当坏行——凭这份实测普查，不是凭猜。

    这条守卫存在的理由是**耦合要看得见**：普查一旦说 `is_asin` 不再恒有，上面那条
    「缺就是坏行」立刻会把整店的行全部拒掉，而那时错误会表现为「这个店没有数据」，
    一个指向完全错误方向的症状。让它先在这里变红。
    """
    census = json.loads(
        Path("docs/evidence/lx-response-ad_campaign_search_term_report-20260830.json").read_text(
            encoding="utf-8"
        )
    )["field_census"]
    assert "is_asin" in census, "搜索词报表不再回 is_asin：解析器的严格分支要重新论证"
    assert census["is_asin"]["types"] == ["int"]
    # null 率与 query 相同 ⇒ 只有汇总行没有它，每个数据行都有。
    assert census["is_asin"]["null_rate"] == census["query"]["null_rate"]
    assert census["is_asin"]["in_summary_row"] is False


def test_impressions_are_carried_through_for_the_human_to_judge_with() -> None:
    source, _ = make_source([[term_row(impressions=5000), healthy_row()]], total=2)
    got = fetch_all(source)
    assert {r.search_term: r.impressions for r in got.records}["cheap widget"] == 5000


def test_an_unreadable_impressions_never_kills_a_candidate() -> None:
    """曝光是展示型指标，判定不看它。让它有资格毙掉候选，就是让一个纯粹给人看的
    数字决定这个词否不否——而这一行的花费、点击、订单三样都读得好好的。

    仓库已有的分法（mirror/sync.py）：判定型指标解析失败即抛，展示型置 None。
    这条钉住曝光落在后一类：行照常参与聚合，只是曝光显示为「—」。
    """
    source, _ = make_source([[term_row(impressions="N/A"), healthy_row()]], total=2)
    got = fetch_all(source)
    by_term = {r.search_term: r for r in got.records}
    assert "cheap widget" in by_term  # 行没有被丢掉
    assert by_term["cheap widget"].impressions is None
    assert got.unreadable_rows == 0  # 也没有被算成坏行


def test_a_group_with_one_unreadable_impressions_reports_no_total_at_all() -> None:
    """少算了某几行的总曝光会把 CTR 算高，方向恰好是「看起来更该留着」——
    宁可显示「—」，不给一个偏向保留的假数。
    """
    source, _ = make_source(
        [
            [
                term_row(impressions=5000, record_id="a", target_match_type="Broad"),
                term_row(impressions="N/A", record_id="b", target_match_type="Exact"),
                healthy_row(),
            ]
        ],
        total=3,
    )
    got = fetch_all(source)
    assert {r.search_term: r.impressions for r in got.records}["cheap widget"] is None
