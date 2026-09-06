"""StrategyToolService 测试：AI 经工具面能做到的止于"冻结待批"（AX-05）。"""

import threading
from datetime import UTC, datetime, timedelta

import pytest

from ads_control_plane.api.mcp_tools.server import (
    InMemoryActorTokenVerifier,
    build_internal_mcp,
)
from ads_control_plane.api.mcp_tools.service import ReadToolService, ToolDenied
from ads_control_plane.api.mcp_tools.strategy_service import StrategyToolService
from ads_control_plane.authorization.model import Action, ClientType, Environment, Grant
from ads_control_plane.canonical.entity import (
    AdProduct,
    CanonicalEntityRef,
    EntityType,
    ParentRefs,
    Provider,
)
from ads_control_plane.canonical.ids import new_canonical_id
from ads_control_plane.canonical.money import Money
from ads_control_plane.identity.actor import (
    ActorContext,
    AuthenticationStrength,
    PrincipalType,
    Role,
)
from ads_control_plane.providers.mock.search_terms import MockSearchTermSource
from ads_control_plane.strategies.negation import CandidateSetState, SearchTermRecord
from ads_control_plane.strategies.store import InMemoryCandidateSetStore

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
ORG = new_canonical_id()


def ai_actor() -> ActorContext:
    real_now = datetime.now(UTC)
    return ActorContext(
        principal_id=new_canonical_id(),
        principal_type=PrincipalType.AI_CLIENT,
        organization_id=ORG,
        roles=frozenset({Role.ANALYST}),
        human_initiator_person_id="alice",
        client_id="codex-1",
        session_id="s-1",
        authentication_strength=AuthenticationStrength.MFA,
        issued_at=real_now,
        expires_at=real_now + timedelta(hours=8),
    )


def strategy_grant(actions: frozenset[Action]) -> Grant:
    return Grant(
        grant_id=new_canonical_id(),
        organization_id=ORG,
        environments=frozenset({Environment.STAGING}),
        actions=actions,
        client_types=frozenset({ClientType.MCP_AI}),
    )


def seed_record(term: str = "cheap widget", *, is_asin: bool = False) -> SearchTermRecord:
    return SearchTermRecord(
        scope=CanonicalEntityRef(
            organization_id=ORG,
            provider=Provider.MOCK,
            provider_connection_id=new_canonical_id(),
            marketplace="US",
            shop_external_id="shop-1",
            profile_external_id="profile-A",
            ad_product=AdProduct.SP,
            entity_type=EntityType.AD_GROUP,
            entity_external_id="ag-1",
            parent_refs=ParentRefs(campaign_external_id="c-1"),
        ),
        search_term=term,
        term_is_asin=is_asin,
        clicks=40,
        conversions=0,
        spend=Money(amount="35.00", currency="USD"),
        window_start=NOW - timedelta(days=30),
        window_end=NOW - timedelta(days=1),
        data_as_of=NOW - timedelta(hours=2),
    )


def make_service(
    grants: list[Grant], source: MockSearchTermSource, store: InMemoryCandidateSetStore
) -> StrategyToolService:
    return StrategyToolService(
        environment=Environment.STAGING,
        grants=grants,
        denies=[],
        search_terms=source,
        store=store,
        clock=lambda: NOW,
    )


class TestGenerateCandidateSet:
    def test_ai_generates_frozen_set_pending_human(self) -> None:
        source = MockSearchTermSource()
        source.seed("profile-A", [seed_record()])
        store = InMemoryCandidateSetStore()
        service = make_service(
            [strategy_grant(frozenset({Action.PROPOSAL_CREATE_DRAFT}))], source, store
        )
        result = service.generate_negation_candidate_set(
            ai_actor(), profile_external_id="profile-A"
        )
        assert result["candidate_count"] == 1
        assert result["profile_has_data_source"] is True
        assert result["set_id"] is not None
        assert result["set_hash"]
        stored = store.list_by_state(ORG, CandidateSetState.FROZEN)
        assert len(stored) == 1
        assert stored[0].source == "AI"
        assert stored[0].created_by_person_id is None

    def test_no_grant_is_denied(self) -> None:
        service = make_service([], MockSearchTermSource(), InMemoryCandidateSetStore())
        with pytest.raises(ToolDenied) as exc:
            service.generate_negation_candidate_set(ai_actor(), profile_external_id="profile-A")
        assert exc.value.code == "AUTH_SCOPE_DENIED"

    def test_out_of_whitelist_parameters_rejected(self) -> None:
        source = MockSearchTermSource()
        source.seed("profile-A", [seed_record()])
        service = make_service(
            [strategy_grant(frozenset({Action.PROPOSAL_CREATE_DRAFT}))],
            source,
            InMemoryCandidateSetStore(),
        )
        with pytest.raises(ToolDenied) as exc:
            service.generate_negation_candidate_set(
                ai_actor(), profile_external_id="profile-A", min_clicks=1
            )
        assert exc.value.code == "PARAMETER_REJECTED"

    def test_empty_yield_creates_no_set(self) -> None:
        """接了数据、查了、确实无候选：flag 为 True，空手而归但不是"没查"。"""
        source = MockSearchTermSource()
        # 有转化的词永不候选 → 评估过 1 条、产出 0 候选
        source.seed("profile-A", [seed_record().model_copy(update={"conversions": 9})])
        store = InMemoryCandidateSetStore()
        service = make_service(
            [strategy_grant(frozenset({Action.PROPOSAL_CREATE_DRAFT}))],
            source,
            store,
        )
        result = service.generate_negation_candidate_set(
            ai_actor(), profile_external_id="profile-A"
        )
        assert result["profile_has_data_source"] is True
        assert result["evaluated_ad_group_terms"] == 1
        assert result["candidate_count"] == 0
        assert result["set_id"] is None
        assert store.list_by_state(ORG) == ()

    def test_unconnected_profile_reports_data_source_absent(self) -> None:
        """数据源根本没接入的店铺 → 明确自报，而不是伪装成"查了没有浪费"。

        2026-08-29 排查结论 runtime-2：此前该返回与"接了、查了、确实无候选"逐字
        相同（evaluated_ad_group_terms=0, candidate_count=0），对真实店铺跑一圈得到的 0 会
        被当成好消息。
        """
        source = MockSearchTermSource()
        source.seed("profile-A", [seed_record()])
        store = InMemoryCandidateSetStore()
        service = make_service(
            [strategy_grant(frozenset({Action.PROPOSAL_CREATE_DRAFT}))], source, store
        )
        result = service.generate_negation_candidate_set(
            ai_actor(), profile_external_id="real-shop-1"
        )
        assert result["profile_has_data_source"] is False
        assert result["evaluated_ad_group_terms"] == 0
        assert result["candidate_count"] == 0
        assert result["set_id"] is None
        assert store.list_by_state(ORG) == ()

    def test_profile_seeded_empty_counts_as_connected(self) -> None:
        """seed 过空列表 = "接了但当期无行"，不是"没接"——两者的 flag 必须不同。"""
        source = MockSearchTermSource()
        source.seed("profile-A", [])
        service = make_service(
            [strategy_grant(frozenset({Action.PROPOSAL_CREATE_DRAFT}))],
            source,
            InMemoryCandidateSetStore(),
        )
        result = service.generate_negation_candidate_set(
            ai_actor(), profile_external_id="profile-A"
        )
        assert result["profile_has_data_source"] is True
        assert result["evaluated_ad_group_terms"] == 0
        assert result["candidate_count"] == 0

    def test_same_term_in_two_ad_groups_is_two_rows_but_one_term(self) -> None:
        """两个计数必须分得开：行数 ≠ 词数。

        这个数唯一的读者是 AI，AI 唯一的动作是把它讲给人听。若只有一个数，AI 会说
        「已评估该店 2 个搜索词」——而这家店只有 1 个搜索词，它投在 2 个广告组里。
        人拿这个数去和领星后台的词数对账对不上，且响应里没有任何线索告诉他为什么。
        Mock 源下每个词只出现一次，两者恰好相等，名字的错处一直看不出来。
        """
        source = MockSearchTermSource()
        in_other_group = seed_record().model_copy(
            update={"scope": seed_record().scope.model_copy(update={"entity_external_id": "ag-2"})}
        )
        source.seed("profile-A", [seed_record(), in_other_group])
        service = make_service(
            [strategy_grant(frozenset({Action.PROPOSAL_CREATE_DRAFT}))],
            source,
            InMemoryCandidateSetStore(),
        )
        result = service.generate_negation_candidate_set(
            ai_actor(), profile_external_id="profile-A"
        )
        assert result["evaluated_ad_group_terms"] == 2
        assert result["distinct_search_terms"] == 1
        # 候选仍按 (广告组, 词) 逐条给——否定要落到具体广告组，不能按词合并。
        assert result["candidate_count"] == 2

    def test_distinct_term_count_folds_case_like_the_dedupe_key(self) -> None:
        """去重口径与域层那条重复行检查逐字同源（casefold），不另起一套。"""
        source = MockSearchTermSource()
        upper = seed_record("Cheap Widget").model_copy(
            update={"scope": seed_record().scope.model_copy(update={"entity_external_id": "ag-2"})}
        )
        source.seed("profile-A", [seed_record("cheap widget"), upper])
        service = make_service(
            [strategy_grant(frozenset({Action.PROPOSAL_CREATE_DRAFT}))],
            source,
            InMemoryCandidateSetStore(),
        )
        result = service.generate_negation_candidate_set(
            ai_actor(), profile_external_id="profile-A"
        )
        assert result["evaluated_ad_group_terms"] == 2
        assert result["distinct_search_terms"] == 1


class TestListCandidateSets:
    def test_list_requires_read_grant(self) -> None:
        service = make_service([], MockSearchTermSource(), InMemoryCandidateSetStore())
        with pytest.raises(ToolDenied):
            service.list_negation_candidate_sets(ai_actor())

    def test_list_shows_own_org_sets(self) -> None:
        source = MockSearchTermSource()
        source.seed("profile-A", [seed_record()])
        store = InMemoryCandidateSetStore()
        service = make_service(
            [strategy_grant(frozenset({Action.PROPOSAL_CREATE_DRAFT, Action.RESOURCE_READ}))],
            source,
            store,
        )
        service.generate_negation_candidate_set(ai_actor(), profile_external_id="profile-A")
        listing = service.list_negation_candidate_sets(ai_actor())
        assert len(listing["candidate_sets"]) == 1
        assert listing["candidate_sets"][0]["state"] == "FROZEN"


def test_mcp_shell_registers_strategy_tools() -> None:
    """壳层冒烟：带 strategy 组装不炸（工具处理器注册成功）。"""
    read_service = ReadToolService(environment=Environment.STAGING, grants=[], denies=[])
    strategy = make_service([], MockSearchTermSource(), InMemoryCandidateSetStore())
    server = build_internal_mcp(read_service, InMemoryActorTokenVerifier(), strategy=strategy)
    assert server is not None


def test_currency_mismatch_surfaces_as_typed_denial() -> None:
    """EUR 参数包对 USD 数据：ToolDenied(CURRENCY_MISMATCH)，不是裸异常穿透。

    2026-08-29 排查（mandate-1/approval-2）：裸 ValueError 穿到 MCP 面后只剩一句
    Error executing tool，人分不清「币种签错了」和「服务器坏了」。
    """
    source = MockSearchTermSource()
    source.seed("profile-A", [seed_record()])  # 数据是 USD
    service = make_service(
        [strategy_grant(frozenset({Action.PROPOSAL_CREATE_DRAFT}))],
        source,
        InMemoryCandidateSetStore(),
    )
    with pytest.raises(ToolDenied) as exc:
        service.generate_negation_candidate_set(
            ai_actor(), profile_external_id="profile-A", currency="EUR"
        )
    assert exc.value.code == "CURRENCY_MISMATCH"


def test_abstains_are_capped_and_the_true_total_is_always_reported() -> None:
    """abstains 无上限时，一批全弃权的真实店会把响应撑到 512KB 并撑断 MCP 传输。

    2026-08-30 实测：真实店 3815 条弃权 × 约 134 字节 ≈ 512 KB，客户端拿到的是
    "SSE stream ended without a response"——零信息，人连"是数据的问题还是服务器
    的问题"都判断不了。而这几千条弃权原因完全相同，信息量等于一条。
    截断不能以牺牲真相为代价：总数由 abstain_count 恒如实给出。
    """
    source = MockSearchTermSource()
    stale = {"data_as_of": NOW - timedelta(days=30)}  # 必然越过新鲜度门
    source.seed(
        "profile-A", [seed_record(f"term-{i}").model_copy(update=stale) for i in range(120)]
    )
    service = make_service(
        [strategy_grant(frozenset({Action.PROPOSAL_CREATE_DRAFT}))],
        source,
        InMemoryCandidateSetStore(),
    )
    payload = service.generate_negation_candidate_set(ai_actor(), profile_external_id="profile-A")
    assert payload["abstain_count"] == 120  # 真相不打折
    assert len(payload["abstains"]) == 50  # 列出来的有上限
    assert payload["evaluated_ad_group_terms"] == 120


def test_parameter_rejection_says_which_parameter_and_what_range() -> None:
    """只给一句 PARAMETER_REJECTED，人（和 AI）只能靠试。

    实测：AI 客户端传 max_data_staleness_hours=73 拿到的整条回复就是
    PARAMETER_REJECTED，既不知道是哪个参数，也不知道上限是 72。
    """
    source = MockSearchTermSource()
    source.seed("profile-A", [seed_record()])
    service = make_service(
        [strategy_grant(frozenset({Action.PROPOSAL_CREATE_DRAFT}))],
        source,
        InMemoryCandidateSetStore(),
    )
    with pytest.raises(ToolDenied) as exc:
        service.generate_negation_candidate_set(
            ai_actor(), profile_external_id="profile-A", max_data_staleness_hours=73
        )
    assert exc.value.code == "PARAMETER_REJECTED"
    assert exc.value.detail is not None
    assert "max_data_staleness_hours" in exc.value.detail
    assert "72" in exc.value.detail


class TestWhatTheAiClientCanActuallyRelay:
    """AI 拿到的响应，够不够它把话讲清楚给人听。

    这个工具面唯一的读者是 AI 客户端，而 AI 唯一的动作是把结果转述给人并给出下一步。
    2026-09-06 审核：响应此前只给 ID 与计数——空手而归的六种原因、这批的时效、
    候选属于哪个活动、内容是否与已在待批的那份相同，一个都不在里面。于是 AI 只能说
    「生成了 3 条候选，set_id 是 xxx」，或者更糟：把 candidate_count 0 讲成
    「这个店很干净」。
    """

    @staticmethod
    def _service() -> tuple[StrategyToolService, MockSearchTermSource]:
        source = MockSearchTermSource()
        source.seed("profile-A", [seed_record()])
        return (
            make_service(
                [strategy_grant(frozenset({Action.PROPOSAL_CREATE_DRAFT, Action.RESOURCE_READ}))],
                source,
                InMemoryCandidateSetStore(),
            ),
            source,
        )

    def test_an_empty_run_says_which_of_the_six_reasons_it_was(self) -> None:
        """没接数据源跑出来的 0 条，不许和「确实没有该否的词」长一个样。"""
        service, _ = self._service()
        empty = service.generate_negation_candidate_set(
            ai_actor(), profile_external_id="unbound-profile"
        )
        assert empty["candidate_count"] == 0
        assert empty["outcome"] == "NO_DATA_SOURCE"
        # note 不能还在谈审批：这一趟根本没有东西可批。
        assert "nothing to approve" in empty["note"]
        # 此前这里钉的是「only NO_CANDIDATES means no wasted spend was found」。
        # 那句话本身是假的：NO_CANDIDATES 只排除了候选，排除不了同一轮里的弃权、
        # 未判断组，以及没到 min_spend / min_clicks 被静默丢掉的词。断言钉着假话，
        # 就等于每次跑测试都在确认这句假话还在。改钉真话。
        assert "NO_CANDIDATES does NOT mean the account has no wasted spend" in empty["note"]
        assert "applied_parameters" in empty["note"], "没说清「没有」是相对哪几个门槛而言"

    def test_a_produced_set_carries_the_deadline_and_the_campaign_it_belongs_to(self) -> None:
        """转述要用的三样：几点作废、每条属于哪个活动、有没有重复。"""
        service, _ = self._service()
        payload = service.generate_negation_candidate_set(
            ai_actor(), profile_external_id="profile-A"
        )
        assert payload["outcome"] == "CANDIDATES"
        assert payload["expires_at"]
        assert payload["content_fingerprint"]
        assert payload["same_content_as"] == []  # 第一次生成，没有孪生
        first = payload["candidates"][0]
        assert first["campaign_external_id"] == "c-1"
        assert "ad_group_name" in first and "campaign_name" in first

    def test_generating_the_same_batch_twice_points_at_the_one_already_waiting(self) -> None:
        """同一段数据被生成第二次时，AI 要知道人手上已经有一份了。

        不然人问一次它就冻一份，每一份都要人去拒。
        """
        service, _ = self._service()
        first = service.generate_negation_candidate_set(ai_actor(), profile_external_id="profile-A")
        second = service.generate_negation_candidate_set(
            ai_actor(), profile_external_id="profile-A"
        )
        assert second["same_content_as"] == [first["set_id"]]
        assert second["content_fingerprint"] == first["content_fingerprint"]

    def test_a_malformed_amount_is_told_what_shape_it_should_be(self) -> None:
        """「$20」被拒时，补充说明不能是 Python 的内部类名。"""
        service, _ = self._service()
        with pytest.raises(ToolDenied) as caught:
            service.generate_negation_candidate_set(
                ai_actor(), profile_external_id="profile-A", min_spend_amount="$20"
            )
        assert caught.value.code == "PARAMETER_REJECTED"
        detail = caught.value.detail or ""
        assert "decimal" in detail
        assert "ConversionSyntax" not in detail

    def test_listing_sets_says_which_store_and_whether_it_is_still_approvable(self) -> None:
        """「有什么要批」要答得出店铺、授权书与时效，否则人无法行动。"""
        service, _ = self._service()
        service.generate_negation_candidate_set(ai_actor(), profile_external_id="profile-A")
        listed = service.list_negation_candidate_sets(ai_actor())
        row = listed["candidate_sets"][0]
        assert row["profile_external_id"] == "profile-A"
        assert row["expired"] is False
        assert row["expires_at"]
        assert "mandate_id" in row
        assert "only a human can approve" in listed["note"]


class TestAnAsinWasteIsStillWaste:
    """ASIN 型搜索词否不掉，但它烧的钱是真的——响应必须让 AI 说得出这件事。

    2026-09-06 审核（A04）：`is_asin` 在真实搜索词报表的每个数据行上都有（实测
    null 率 3.2% 恰等于汇总行占比），而本仓库一路不读；请求也不带 `search_type`，
    所以 ASIN 型的行本来就会回来。结果是这些词被写成否定精准关键词，人在领星照做，
    ASIN 型来源根本不看关键词否定——钱继续烧，而证据行上一切正常。
    """

    @staticmethod
    def _service(records: list[SearchTermRecord]) -> StrategyToolService:
        source = MockSearchTermSource()
        source.seed("profile-A", records)
        return make_service(
            [strategy_grant(frozenset({Action.PROPOSAL_CREATE_DRAFT, Action.RESOURCE_READ}))],
            source,
            InMemoryCandidateSetStore(),
        )

    def test_a_run_that_only_found_asins_is_not_reported_as_a_clean_account(self) -> None:
        """这是这条缺陷里最贵的一句话。

        落到 NO_CANDIDATES，AI 会照着 note 说「这段窗口确实干净」，人于是什么都不做，
        而这一批每一条都是花了钱、零转化的浪费。
        """
        service = self._service([seed_record("b0demo0001", is_asin=True)])
        payload = service.generate_negation_candidate_set(
            ai_actor(), profile_external_id="profile-A"
        )
        assert payload["candidate_count"] == 0
        assert payload["outcome"] == "ALL_ASIN"
        assert payload["asin_abstain_count"] == 1

    def test_the_asin_terms_themselves_reach_the_response(self) -> None:
        # 只给一个数，人知道「有 1 个」却不知道是哪一个，去领星无从下手。
        service = self._service([seed_record("b0demo0001", is_asin=True)])
        payload = service.generate_negation_candidate_set(
            ai_actor(), profile_external_id="profile-A"
        )
        listed = payload["abstains"]
        assert [a["search_term"] for a in listed] == ["b0demo0001"]
        assert listed[0]["reason"] == "ASIN_NOT_A_KEYWORD"
        assert "否定投放" in listed[0]["detail"]

    def test_a_set_that_is_not_the_whole_story_says_so_on_the_set_itself(self) -> None:
        """审批屏幕上必须看得见。

        与 truncated_from 同一条理由：签字的人看到的只有这份清单，读出来是「本轮的
        浪费都在这儿了」，于是他在领星把这几条加完否定词就收工。
        """
        service = self._service(
            [seed_record("cheap widget"), seed_record("b0demo0001", is_asin=True)]
        )
        payload = service.generate_negation_candidate_set(
            ai_actor(), profile_external_id="profile-A"
        )
        assert payload["candidate_count"] == 1
        assert payload["asin_abstain_count"] == 1
        listed = service.list_negation_candidate_sets(ai_actor())["candidate_sets"]
        assert [s["asin_abstain_count"] for s in listed] == [1]

    def test_the_set_remembers_which_asins_it_could_not_negate(self) -> None:
        """光有计数，人就知道有钱在烧却说不出烧在哪。

        卡片是在他签完字、正要去领星的那一刻提这件事的。词不随集合冻结，他当天
        没有任何合法出路：列表工具只回计数，按同一份授权书重跑必撞
        RUN_BUDGET_EXCEEDED（产出这份集合的那次运行已经把配额用掉了）。
        """
        service = self._service(
            [seed_record("cheap widget"), seed_record("b0demo0001", is_asin=True)]
        )
        service.generate_negation_candidate_set(ai_actor(), profile_external_id="profile-A")
        stored = service._store.list_by_state(ai_actor().organization_id)
        assert [s.asin_abstain_terms for s in stored] == [("b0demo0001",)]

    def test_the_ai_can_read_back_what_it_generated_without_spending_a_run(self) -> None:
        """AI 生成的集合，AI 自己得读得回来。

        没有 get_by_id，唯一吐词表的是 generate——而它改状态、消配额。默认打法
        （每天一次）下产出这份集合的那次运行已经用掉当天配额，重跑必撞
        RUN_BUDGET_EXCEEDED；改走即席能拿到词，但会再冻一份内容相同的待批集合，
        要人多拒一次。于是人在 AI 客户端里问「那个 ASIN 是哪个词」，AI 只能说看不到。
        """
        service = self._service(
            [seed_record("cheap widget"), seed_record("b0demo0001", is_asin=True)]
        )
        service.generate_negation_candidate_set(ai_actor(), profile_external_id="profile-A")
        row = service.list_negation_candidate_sets(ai_actor())["candidate_sets"][0]
        assert row["asin_abstain_terms"] == ["b0demo0001"]
        assert [c["search_term"] for c in row["candidates"]] == ["cheap widget"]
        assert row["parameter_pack"]["min_clicks"] is not None

    def test_asin_abstains_are_listed_before_the_ones_nobody_can_act_on(self) -> None:
        """截断时先丢谁，是有对错的。

        STALE_DATA 全批同因，列一条和列五百条给人的信息一样多；ASIN 型每一条都是一个
        人现在就要去领星否定的具体词。不排序的话，过期弃权会把它们挤出响应。
        """
        stale = [
            seed_record(f"stale-{i}").model_copy(update={"data_as_of": NOW - timedelta(hours=48)})
            for i in range(60)
        ]
        service = self._service([*stale, seed_record("b0demo0001", is_asin=True)])
        payload = service.generate_negation_candidate_set(
            ai_actor(), profile_external_id="profile-A"
        )
        assert payload["abstain_count"] == 61
        assert len(payload["abstains"]) < 61  # 确实被截断了，否则这条测不到东西
        assert payload["abstains"][0]["search_term"] == "b0demo0001"
        assert payload["asin_abstain_count"] == 1


class TestSourceAccountingBalances:
    """取数账目必须自洽——账不平的那一刻，读响应的 AI 只能不信这组数字。

    2026-09-06 排查：Mock 此前把 source_total 与 usable_rows 一并留在默认值上
    （None / 0），理由写的是「seed 进来的记录没有读不出来这回事」。丢弃类计数为 0
    是对的，但「什么都没丢」不等于「什么都没读」：同一份响应里
    evaluated_ad_group_terms 说判了 N 组、source_accounting 说 usable_rows 是 0。
    响应旁边那段注释亲口定的等式在 Development/CI 下从来对不上账，而
    Development/CI 是唯一能跑通全链路的地方。
    """

    def test_reading_a_set_back_names_the_objects_the_way_lingxing_does(self) -> None:
        """回读工具被 note 指定为 generate 的替代品，就得给出 generate 给的东西。

        2026-09-07 排查：generate 的候选带 ad_group_name / campaign_name，回读只给
        两串外部 ID——而人在领星界面里是按**名称**找活动和广告组的，AI 只念得出
        ID 就等于没答上，人只好让它再 generate 一次（那要烧配额、还多出一份待批）。
        """
        source = MockSearchTermSource()
        #: 名称必须是真值。默认 seed 两个名称都是 None，逐个比对就成了
        #  None == None，永远证明不了名称被带出来（2026-09-07 排查：上一版正是如此）。
        source.seed(
            "profile-A",
            [
                seed_record().model_copy(
                    update={"campaign_name": "HX02-Auto-US", "ad_group_name": "AG-通用词"}
                )
            ],
        )
        store = InMemoryCandidateSetStore()
        service = make_service(
            [strategy_grant(frozenset({Action.PROPOSAL_CREATE_DRAFT, Action.RESOURCE_READ}))],
            source,
            store,
        )
        gen = service.generate_negation_candidate_set(ai_actor(), profile_external_id="profile-A")
        assert gen["candidates"][0]["campaign_name"], "夹具没带上名称，这条守卫等于没写"
        listed = service.list_negation_candidate_sets(ai_actor())
        back = listed["candidate_sets"][0]["candidates"][0]
        #: 「这条否定词要加到哪儿」这一组身份字段必须与 generate 逐字一致。
        for key in (
            "search_term",
            "ad_group_external_id",
            "ad_group_name",
            "campaign_external_id",
            "campaign_name",
        ):
            assert back[key] == gen["candidates"][0][key], key + " 在回读里对不上"
        # match_type 只有回读给（generate 不给）——NEG_EXACT 下它是常量，多给不多事。
        assert back["match_type"]
        #: evidence 故意不给：这个工具不分页也不限集合数，逐条带上证据会让响应
        #  随集合数无界增长，而「为什么它是候选」是人在网页上签字时看的东西
        #  （审批本就只有人能做）。缺口如实记在这里，将来真有人要，再连同分页一起加。
        assert "evidence" not in back

    def test_the_books_add_up_in_the_only_channel_ci_can_run(self) -> None:
        source = MockSearchTermSource()
        source.seed("profile-A", [seed_record()])
        service = make_service(
            [strategy_grant(frozenset({Action.PROPOSAL_CREATE_DRAFT}))],
            source,
            InMemoryCandidateSetStore(),
        )
        result = service.generate_negation_candidate_set(
            ai_actor(), profile_external_id="profile-A"
        )
        acct = result["source_accounting"]
        # 响应里写死的等式：源侧总行数 = 汇总行 + 重复行 + 读不出来的行 + 可用行。
        assert acct["source_total"] == (
            acct["skipped_summary_rows"]
            + acct["duplicate_rows"]
            + acct["unreadable_rows"]
            + acct["usable_rows"]
        )
        # 而且不能靠「全是 0」来配平：判了几组，就得有几行是可用的。
        assert acct["usable_rows"] == result["evaluated_ad_group_terms"]
        assert acct["usable_rows"] > 0


def test_no_candidates_with_abstains_does_not_get_relayed_as_a_clean_account() -> None:
    """NO_CANDIDATES 那一轮里还躺着弃权时，note 必须自带限定，并报得出门槛。

    这一批：一个词有转化（判过、没事），一个 ASIN 型词花了钱零转化（判过、本策略
    否不掉）。两条都判过，所以既不是 ALL_ABSTAINED 也不是 ALL_ASIN，结局落在
    NO_CANDIDATES——而此前 note 逐字写着「only NO_CANDIDATES means no wasted spend
    was found」。读响应的 AI 照着讲，人听到的就是「这个店没有浪费花费」，可同一份
    响应里 asin_abstain_count 正是 1，那笔钱真的在白烧，只是要人去领星手工否定。

    另一半是门槛：花了钱、零转化、但没到 min_spend / min_clicks 的词既不进
    candidates 也不进 abstains，被静默丢掉，响应里连个占位都没有。不把生效门槛
    回声出来，「没有该否的词」这句话就没有适用范围，人也无从判断该不该调低门槛。
    """
    source = MockSearchTermSource()
    source.seed(
        "profile-A",
        [
            seed_record("converting widget").model_copy(update={"conversions": 9}),
            seed_record("B0ABCDEFGH", is_asin=True),
        ],
    )
    service = make_service(
        [strategy_grant(frozenset({Action.PROPOSAL_CREATE_DRAFT, Action.RESOURCE_READ}))],
        source,
        InMemoryCandidateSetStore(),
    )
    payload = service.generate_negation_candidate_set(ai_actor(), profile_external_id="profile-A")

    assert payload["outcome"] == "NO_CANDIDATES"
    assert payload["candidate_count"] == 0
    assert payload["asin_abstain_count"] == 1, "这笔钱确实在白烧，只是本策略否不掉"

    note = payload["note"]
    assert "NO_CANDIDATES does NOT mean the account has no wasted spend" in note
    assert "1 search terms were not judged at all" in note, "同一轮里的弃权没被说出来"
    assert "1 of them are ASINs" in note, "没说清有几条要人去领星手工处理"

    applied = payload["applied_parameters"]
    assert applied["min_spend_amount"] and applied["currency"], "没报出花费门槛"
    assert applied["min_clicks"] >= 1, "没报出点击门槛"
    assert applied["lookback_days"] >= 1


def test_a_truncated_abstain_list_says_it_is_truncated() -> None:
    """弃权列表被截断时必须有标记，否则 AI 会把手里这 50 条念成全部。

    排序是 ASIN 优先，而 ASIN 型是唯一要人去领星「否定投放」动手的那批。超过 50 条
    时后面那些在任何工具面都再也拿不回来：outcome=ALL_ASIN 这一路根本不创建候选集，
    list_negation_candidate_sets 也就查不到任何东西。人被告知「去否定掉这些 ASIN」，
    却只看得见其中 50 个，而响应里没有一处说这是一部分——candidates 早有
    truncated_from 保护，abstains 一直没有。

    上限本身是对的（2026-08-30 实测 3815 条弃权 ≈ 512 KB，MCP 传输当场断流），
    要修的不是上限，是「截了不说」。
    """
    source = MockSearchTermSource()
    source.seed(
        "profile-A",
        [seed_record(f"B0ASIN{i:04d}", is_asin=True) for i in range(60)],
    )
    service = make_service(
        [strategy_grant(frozenset({Action.PROPOSAL_CREATE_DRAFT, Action.RESOURCE_READ}))],
        source,
        InMemoryCandidateSetStore(),
    )
    payload = service.generate_negation_candidate_set(ai_actor(), profile_external_id="profile-A")

    assert payload["outcome"] == "ALL_ASIN"
    assert payload["set_id"] is None, "这一路不建集合，被截掉的那些没有第二个出处"
    assert payload["asin_abstain_count"] == 60
    assert len(payload["abstains"]) == 50, "列表确实被截了"
    assert payload["abstains_truncated_from"] == 60, "截了却没说——AI 会把 50 条念成全部"


def test_an_untruncated_abstain_list_is_not_labelled_as_truncated() -> None:
    """没截断时标记必须是 None，否则每一轮都在说一句「还有更多」的假话。"""
    source = MockSearchTermSource()
    source.seed("profile-A", [seed_record(f"B0ASIN{i:04d}", is_asin=True) for i in range(3)])
    service = make_service(
        [strategy_grant(frozenset({Action.PROPOSAL_CREATE_DRAFT, Action.RESOURCE_READ}))],
        source,
        InMemoryCandidateSetStore(),
    )
    payload = service.generate_negation_candidate_set(ai_actor(), profile_external_id="profile-A")
    assert len(payload["abstains"]) == 3
    assert payload["abstains_truncated_from"] is None


def _human_issuer() -> ActorContext:
    real_now = datetime.now(UTC)
    return ActorContext(
        principal_id=new_canonical_id(),
        principal_type=PrincipalType.HUMAN,
        organization_id=ORG,
        roles=frozenset({Role.APPROVER, Role.ANALYST}),
        human_person_id="boss",
        client_id="c-1",
        session_id="s-1",
        authentication_strength=AuthenticationStrength.MFA,
        issued_at=real_now,
        expires_at=real_now + timedelta(hours=8),
    )


def test_an_empty_mandate_run_still_says_how_much_quota_it_just_burned() -> None:
    """空手而归的授权运行照样吃掉当天一次、照样刷新最小间隔，就得照样说出来。

    「下次什么时候能跑」「今天还剩几次」这两个数，代码注释自己写着是「AI 被拒后
    唯一想知道的两件事……等到 RUN_TOO_SOON 再说就晚了一轮」。可它们此前只挂在
    产出候选那条返回路径上——而空手而归恰恰是最容易让 AI 立刻再跑一次的那条：
    看到 0 条候选，再试一次是最自然的动作，然后才撞上 RUN_TOO_SOON /
    RUN_BUDGET_EXCEEDED。配额已经扣了，人和 AI 却都不知道。
    """
    from ads_control_plane.strategies.mandate import (
        MandateBounds,
        MandateObjective,
        issue_mandate,
    )
    from ads_control_plane.strategies.negation import NegationParameterPack
    from ads_control_plane.strategies.store import InMemoryMandateStore

    mandate = issue_mandate(
        _human_issuer(),
        mandate_id=new_canonical_id(),
        profile_external_id="profile-A",
        objective=MandateObjective(
            objective="WASTED_SPEND_REMOVED", statement="压降 profile-A 的无效搜索词花费"
        ),
        parameter_pack=NegationParameterPack(
            lookback_days=30,
            min_spend=Money(amount="20.00", currency="USD"),
            min_clicks=25,
            max_data_staleness_hours=24,
        ),
        bounds=MandateBounds(max_runs_per_day=2, max_candidates_per_run=50, valid_days=14),
        now=NOW,
    )
    mandates = InMemoryMandateStore()
    mandates.save(mandate)

    source = MockSearchTermSource()
    # 有转化 → 判过、没事：0 条候选、0 条弃权，结局 NO_CANDIDATES。
    source.seed("profile-A", [seed_record().model_copy(update={"conversions": 9})])
    service = StrategyToolService(
        environment=Environment.STAGING,
        grants=[strategy_grant(frozenset({Action.PROPOSAL_CREATE_DRAFT, Action.RESOURCE_READ}))],
        denies=[],
        search_terms=source,
        store=InMemoryCandidateSetStore(),
        mandates=mandates,
        clock=lambda: NOW,
    )
    payload = service.generate_negation_candidate_set(
        ai_actor(), profile_external_id="profile-A", mandate_id=str(mandate.mandate_id)
    )

    assert payload["candidate_count"] == 0, "这一轮就是要空手而归"
    assert payload["runs_remaining_today"] == 1, "2 次/日，这一次已经扣掉了"
    assert payload["next_run_allowed_at"], "最小间隔已经刷新了，却不说下次几点能跑"


def test_when_the_daily_quota_is_spent_it_does_not_point_at_the_interval_time() -> None:
    """日配额先用完时，「最早几点可再发起」不许报最小间隔算出的那个时刻。

    两道闸并存：最小间隔和日配额。间隔到了但配额没了，发起必被
    RUN_BUDGET_EXCEEDED 拒。此前这个字段一律按间隔算，于是界面写着
    「今天的次数已用完 · 最早 13:00 可再发起」——人守到 13:00 点一次、被拒一次，
    两句话没有一句告诉他到底该等到什么时候；AI 拿到的是同一个假时刻。
    """
    from ads_control_plane.strategies.mandate import (
        MandateBounds,
        MandateObjective,
        issue_mandate,
    )
    from ads_control_plane.strategies.negation import NegationParameterPack
    from ads_control_plane.strategies.store import InMemoryMandateStore

    mandate = issue_mandate(
        _human_issuer(),
        mandate_id=new_canonical_id(),
        profile_external_id="profile-A",
        objective=MandateObjective(
            objective="WASTED_SPEND_REMOVED", statement="压降 profile-A 的无效搜索词花费"
        ),
        parameter_pack=NegationParameterPack(
            lookback_days=30,
            min_spend=Money(amount="20.00", currency="USD"),
            min_clicks=25,
            max_data_staleness_hours=24,
        ),
        # 一天只准 1 次，但间隔只有 1 小时：跑完这一次，1 小时后间隔就到了，
        # 配额却要等到明天——间隔算出的那个时刻正是 finding 里那句「最早 13:00」。
        bounds=MandateBounds(
            max_runs_per_day=1,
            max_candidates_per_run=50,
            valid_days=14,
            run_interval_minutes=60,
        ),
        now=NOW,
    )
    mandates = InMemoryMandateStore()
    mandates.save(mandate)
    source = MockSearchTermSource()
    source.seed("profile-A", [seed_record()])
    service = StrategyToolService(
        environment=Environment.STAGING,
        grants=[strategy_grant(frozenset({Action.PROPOSAL_CREATE_DRAFT, Action.RESOURCE_READ}))],
        denies=[],
        search_terms=source,
        store=InMemoryCandidateSetStore(),
        mandates=mandates,
        clock=lambda: NOW,
    )
    payload = service.generate_negation_candidate_set(
        ai_actor(), profile_external_id="profile-A", mandate_id=str(mandate.mandate_id)
    )

    assert payload["runs_remaining_today"] == 0, "1 次/日，这一次用掉了"
    next_allowed = datetime.fromisoformat(payload["next_run_allowed_at"])
    interval_only = NOW + timedelta(minutes=mandate.bounds.run_interval_minutes)
    assert next_allowed > interval_only, "配额已空，间隔到了也跑不了，不许报间隔那个时刻"
    assert next_allowed == mandate.next_quota_day_start(NOW), "该报的是配额日翻篇的那一刻"


def test_an_all_asin_run_records_which_asins_they_were() -> None:
    """催人去领星否定「这几个 ASIN」，就得说得出是哪几个。

    ALL_ASIN 这一路不创建候选集合（没有候选），而词表此前只冻结在集合上
    （negation.py 的 asin_abstain_terms）。于是这条路上那几个词在整个系统里没有
    第二个出处：授权书卡片挂着琥珀条说「去领星「否定投放」手工否定这些 ASIN」，
    人拿着一个数字去后台，什么也做不了。

    仓库对集合早就定过同一条规矩——「词表随集合一起冻结才说得出是哪几个。只回计数
    等于让人知道有钱在烧、却说不出烧在哪」。把它推到运行流水这一级。
    """
    from ads_control_plane.api.approval_api import _mandate_summary
    from ads_control_plane.strategies.mandate import (
        MandateBounds,
        MandateObjective,
        issue_mandate,
    )
    from ads_control_plane.strategies.negation import NegationParameterPack
    from ads_control_plane.strategies.store import InMemoryMandateRunLog, InMemoryMandateStore

    mandate = issue_mandate(
        _human_issuer(),
        mandate_id=new_canonical_id(),
        profile_external_id="profile-A",
        objective=MandateObjective(
            objective="WASTED_SPEND_REMOVED", statement="压降 profile-A 的无效搜索词花费"
        ),
        parameter_pack=NegationParameterPack(
            lookback_days=30,
            min_spend=Money(amount="20.00", currency="USD"),
            min_clicks=25,
            max_data_staleness_hours=24,
        ),
        bounds=MandateBounds(max_runs_per_day=2, max_candidates_per_run=50, valid_days=14),
        now=NOW,
    )
    mandates = InMemoryMandateStore()
    mandates.save(mandate)
    run_log = InMemoryMandateRunLog()
    source = MockSearchTermSource()
    source.seed(
        "profile-A",
        [seed_record("B0ASINAAAA", is_asin=True), seed_record("B0ASINBBBB", is_asin=True)],
    )
    service = StrategyToolService(
        environment=Environment.STAGING,
        grants=[strategy_grant(frozenset({Action.PROPOSAL_CREATE_DRAFT, Action.RESOURCE_READ}))],
        denies=[],
        search_terms=source,
        store=InMemoryCandidateSetStore(),
        mandates=mandates,
        run_log=run_log,
        clock=lambda: NOW,
    )
    payload = service.generate_negation_candidate_set(
        ai_actor(), profile_external_id="profile-A", mandate_id=str(mandate.mandate_id)
    )
    assert payload["outcome"] == "ALL_ASIN"
    assert payload["set_id"] is None, "这一路不建集合——所以词表只能靠运行流水留住"

    # 流水里记下了是哪几个。
    run = run_log.recent(mandate.mandate_id, 1)[0]
    assert run.asin_abstain_count == 2
    assert set(run.asin_abstain_terms) == {"B0ASINAAAA", "B0ASINBBBB"}

    # 而且要真的送到人看的那张卡片上。
    summary = _mandate_summary(mandate, runs_of=lambda _id: run_log.recent(_id, 50), now=NOW)
    assert summary["needs_attention"] is True, "ALL_ASIN 要人动手，卡片得催"
    latest = summary["recent_runs"][0]
    assert set(latest["asin_abstain_terms"]) == {"B0ASINAAAA", "B0ASINBBBB"}


def test_a_second_run_started_mid_fetch_cannot_slip_past_the_daily_quota() -> None:
    """配额闸的「数」与写流水之间隔着整整一轮取数，中间发起的第二次必须被拒。

    真实源一轮是十几秒的多页读（QPS=1）。第一次还卡在取数里时，运行流水里一条
    记录都还没有——第二次去数，数到 0，于是 max_runs_per_day=1 的授权书一天跑
    两次（2026-09-07 实测：两次都产出候选集合，人在待批里看到两份孪生，而每一次
    对领星都是一轮多页读取）。最现实的触发不是两个人同时点，是 AI 客户端等超时
    之后重发一次——第一次其实还在跑。

    MCP SDK 用 anyio.to_thread.run_sync 跑同步工具函数，两个 tools/call 是真并行，
    这条路够得着。

    编排是确定的、不靠调度：第一次停在取数里，第二次在那一刻整个跑完。
    """
    from ads_control_plane.strategies.mandate import (
        MandateBounds,
        MandateObjective,
        issue_mandate,
    )
    from ads_control_plane.strategies.negation import NegationParameterPack
    from ads_control_plane.strategies.store import InMemoryMandateRunLog, InMemoryMandateStore

    mandate = issue_mandate(
        _human_issuer(),
        mandate_id=new_canonical_id(),
        profile_external_id="profile-A",
        objective=MandateObjective(
            objective="WASTED_SPEND_REMOVED", statement="压降 profile-A 的无效搜索词花费"
        ),
        parameter_pack=NegationParameterPack(
            lookback_days=30,
            min_spend=Money(amount="20.00", currency="USD"),
            min_clicks=25,
            max_data_staleness_hours=24,
        ),
        bounds=MandateBounds(max_runs_per_day=1, max_candidates_per_run=50, valid_days=14),
        now=NOW,
    )
    mandates = InMemoryMandateStore()
    mandates.save(mandate)

    inside_fetch = threading.Event()
    may_finish = threading.Event()

    class SlowSource(MockSearchTermSource):
        """取数期间把第一次运行按住——真实源那里这个窗口有十几秒。"""

        def fetch_search_term_performance(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            fetched = super().fetch_search_term_performance(*args, **kwargs)  # type: ignore[arg-type]
            # 只按住第一次。闸坏掉时第二次会走到这里，那时直接放行——否则它会
            # 卡在这儿等一个要等它先返回才会被设上的事件，测试要满 10 秒才红。
            if not inside_fetch.is_set():
                inside_fetch.set()
                may_finish.wait(10)
            return fetched

    source = SlowSource()
    source.seed("profile-A", [seed_record()])
    run_log = InMemoryMandateRunLog()
    service = StrategyToolService(
        environment=Environment.STAGING,
        grants=[strategy_grant(frozenset({Action.PROPOSAL_CREATE_DRAFT, Action.RESOURCE_READ}))],
        denies=[],
        search_terms=source,
        store=InMemoryCandidateSetStore(),
        mandates=mandates,
        run_log=run_log,
        clock=lambda: NOW,
    )

    outcomes: dict[str, tuple[str, str | None]] = {}

    def run(name: str) -> None:
        try:
            payload = service.generate_negation_candidate_set(
                ai_actor(), profile_external_id="profile-A", mandate_id=str(mandate.mandate_id)
            )
            outcomes[name] = ("OK", payload.get("outcome"))
        except ToolDenied as exc:
            outcomes[name] = ("DENIED", exc.code)

    first = threading.Thread(target=run, args=("first",))
    first.start()
    try:
        assert inside_fetch.wait(10), "第一次运行没进到取数"
        run("second")  # 第一次还按在取数里，这一刻发起第二次
    finally:
        may_finish.set()
        first.join(10)

    assert outcomes["first"] == ("OK", "CANDIDATES")
    assert outcomes["second"] == ("DENIED", "RUN_BUDGET_EXCEEDED"), (
        f"1 次/日的授权书在第一次还没跑完时又放行了一次：{outcomes}"
    )
    # 被拒的那次不许留下痕迹：记进流水会自耗配额，占位不释放会把授权锁死一整天。
    assert run_log.count_on_day(mandate.mandate_id, NOW, mandate.quota_day) == 1
