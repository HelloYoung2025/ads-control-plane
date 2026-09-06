"""授权书的运行记录：一次运行**发生过**，以及它的结局是什么。

为什么必须单独记这件事，而不是从候选集合反推：候选集合只在**产出了候选**时才创建
（空集合无意义，也避免审批队列噪音）。于是下面这些运行在系统里不留任何痕迹——

- 授权书的币种和这家店的数据对不上（CURRENCY_MISMATCH）；
- 这家店根本没接数据源；
- 作用域把所有对象都挡掉了（圈的活动在这段数据里一条都没出现）；
- 整批数据太旧，全部 ABSTAIN；
- 取数超时 / 被网关拒。

这五种失败在授权书列表上是**同一幅画面**：徽章「生效中」、待批空空如也。人能得到的
唯一信号是「没有新东西要批」，读出来是好消息；而真相可能是这份授权从签发起就一次
都没成功跑通。人性化的缺口在这里：一个只会说「一切正常」的界面，在出错时说的是假话。

第二个后果在逻辑面，同源：配额与最小间隔此前都从**候选集合数**上算
（`count_for_mandate_on_day` / `latest_run_at_for_mandate`），而上面五种运行都不产生
候选，于是不消耗配额、不刷新间隔。`max_runs_per_day=1` 的授权书只要每次运行都失败，
就可以被无限次触发——而对真实数据源来说，每次触发是一轮多页读取，QPS=1。
恰恰是「配置错了」这个最该被拦住的情形，节流完全不生效。

两个后果同一个根因：系统里只有「运行的产物」，没有「运行」。本模块补上后者。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from ads_control_plane.canonical.ids import CanonicalId


class MandateRunOutcome(StrEnum):
    """一次运行的结局。分这么细是有代价的，但每一项对应人**不同的下一步动作**：

    - CANDIDATES：去审批。
    - NO_CANDIDATES：什么都不用做——查了，这段窗口里确实没有该否的词。
    - ALL_ABSTAINED：数据太旧，等新数据或放宽 max_data_staleness_hours 重签。
    - ALL_ASIN：查出来的浪费全是 ASIN 型搜索词，本策略只开否定关键词，对它们无效——
      去领星「否定投放」手工否定。与 NO_CANDIDATES 合并是这里最贵的一种合并：
      「这段窗口确实干净」会让人什么都不做，而钱正在烧。
    - SCOPE_EMPTY：改作用域重签——圈的对象在这段数据里一条都没出现。
    - NO_ROWS：确认这家店这段时间在不在投放，或换窗口。
    - NO_USABLE_ROWS：取回了行，但没有一条能判断——这是数据形状的问题，
      拉长窗口和确认投放都不会让它好转，得去看源侧那些行到底缺了什么。
    - NO_DATA_SOURCE：去接数据源。这份授权现在跑不出任何东西。
    - SOURCE_ERROR：看错误码；超时可重试，参数被拒则重试永远不会成功。
    - REJECTED：口径或数据形状不对（如币种签错），要重签授权书。

    合并任意两项都会让人拿不准该干什么，那就等于没写。
    """

    CANDIDATES = "CANDIDATES"
    NO_CANDIDATES = "NO_CANDIDATES"
    ALL_ABSTAINED = "ALL_ABSTAINED"
    ALL_ASIN = "ALL_ASIN"
    SCOPE_EMPTY = "SCOPE_EMPTY"
    NO_ROWS = "NO_ROWS"
    NO_USABLE_ROWS = "NO_USABLE_ROWS"
    NO_DATA_SOURCE = "NO_DATA_SOURCE"
    SOURCE_ERROR = "SOURCE_ERROR"
    REJECTED = "REJECTED"


#: 需要人动手才能好转的结局。界面据此把「生效中但跑不通」和「生效中且正常」分开——
#: 二者此前逐字同形。NO_CANDIDATES 不在其中：它是好消息，不是待办。
NEEDS_ATTENTION: frozenset[MandateRunOutcome] = frozenset(
    {
        MandateRunOutcome.SCOPE_EMPTY,
        MandateRunOutcome.NO_ROWS,
        MandateRunOutcome.NO_USABLE_ROWS,
        MandateRunOutcome.NO_DATA_SOURCE,
        MandateRunOutcome.SOURCE_ERROR,
        MandateRunOutcome.REJECTED,
        MandateRunOutcome.ALL_ABSTAINED,
        MandateRunOutcome.ALL_ASIN,
    }
)


class MandateRunRecord(BaseModel):
    """一次授权运行的事实。append-only：记录不改写，失败也留着。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: CanonicalId
    mandate_id: CanonicalId
    ran_at: datetime
    outcome: MandateRunOutcome
    #: 与 MCP 响应同名同义：行数 = (广告组 × 搜索词) 组合数，不是搜索词个数。
    evaluated_ad_group_terms: int
    distinct_search_terms: int
    candidate_count: int
    abstain_count: int
    #: 弃权里有几条是「钱在烧、但本策略否不掉」（ASIN 型搜索词）。单列一个数，
    #: 因为它和其余弃权指向完全不同的下一步：STALE_DATA 是等数据，这个是人要去
    #: 领星「否定投放」动手。混在 abstain_count 里，界面只能说「弃权 N」，
    #: 而 N 背后到底要不要人动手，看不出来。
    asin_abstain_count: int = 0
    #: 那几个 ASIN **是哪几个**。只记数字等于让人知道有钱在烧、却说不出烧在哪，
    #: 而卡片正是在这时候催他去领星「否定投放」动手。候选集合早就为同一条理由
    #: 冻结了词表（negation.py 的 asin_abstain_terms）——但 ALL_ASIN 那一路
    #: 根本不创建集合，于是那条路上的词在整个系统里没有第二个出处，
    #: 界面只能说「这 3 个 ASIN」而永远说不出是哪 3 个。
    #: 与集合那边同一条口径：只记，不截断——它由 max_candidates_per_run 同量级的
    #: 弃权规模决定，而这是内存记录、不过 MCP 传输，没有那边的体积约束。
    asin_abstain_terms: tuple[str, ...] = ()
    #: 作用域挡掉了几条输入。0 与「没有作用域可言」在授权模式下不会混——授权运行
    #: 恒有作用域（整店授权即 0）。
    scope_filtered_out: int
    #: 这一轮**没能判断**的 (广告组, 搜索词) 组数，与连归属都读不出来的行数。
    #: 它们 > 0 时，同一条记录上的 candidate_count=0 只说明「在看得懂的那部分里
    #: 没有该否的词」，不说明这个店干净——差别正是人会不会去查源侧数据。
    #: 单位不同，绝不相加。
    unjudged_ad_group_terms: int = 0
    unattributable_rows: int = 0
    set_id: CanonicalId | None = None
    #: 失败时的域层错误码（CURRENCY_MISMATCH、LX_TIMEOUT…）。**只放码，不放异常原文**：
    #: 原文可能带上游回声，而 AX-16 要求错误码稳定且不泄露资源是否存在。
    error_code: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> MandateRunRecord:
        if self.ran_at.tzinfo is None or self.ran_at.tzinfo.utcoffset(self.ran_at) is None:
            raise ValueError(
                f"NAIVE_DATETIME_REJECTED: ran_at must be timezone-aware (UTC); "
                f"got naive {self.ran_at.isoformat()}"
            )
        if (
            min(
                self.evaluated_ad_group_terms,
                self.distinct_search_terms,
                self.candidate_count,
                self.abstain_count,
                self.asin_abstain_count,
                self.scope_filtered_out,
                self.unjudged_ad_group_terms,
                self.unattributable_rows,
            )
            < 0
        ):
            raise ValueError("run counters must be non-negative")
        if self.asin_abstain_count > self.abstain_count:
            raise ValueError("asin_abstain_count is a subset of abstain_count")
        if (self.outcome is MandateRunOutcome.CANDIDATES) != (self.set_id is not None):
            # 「产出了候选」与「有集合可批」必须同真同假。允许一边真一边假，等于允许
            # 界面上出现一条「已产出候选」却点不开的记录，或一份没有运行来历的集合。
            raise ValueError("set_id must be present exactly when outcome is CANDIDATES")
        return self
