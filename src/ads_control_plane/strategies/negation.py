"""NEG_EXACT 候选策略：高花费零转化搜索词 → 单个精准否定候选。

2026-08-28 业务 Owner 裁决（decision register DEC-015 / DEC-111）：
首批策略 = 高耗零转化词否定；起点 = 阶梯式 L1（AI 生成候选 → 人批准）。

边界：
- 本模块只产出候选集合与导出行，不 import 任何 Provider 适配器；
  否定词的真实写入由人把 CSV 交给领星完成，本仓库没有写通道。
- "零转化"（conversions == 0）是规则定义本身，不是参数：把它做成参数会允许
  "低转化也杀"悄悄扩大杀伤面，越过 DEC-015 的白名单裁决。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from ads_control_plane.canonical.entity import CanonicalEntityRef, EntityType
from ads_control_plane.canonical.ids import CanonicalId
from ads_control_plane.canonical.money import Money
from ads_control_plane.strategies.rule_class import RuleClass

#: Hash 规范化方式版本。变更序列化规则必须提升此版本，旧 Hash 不做跨版本比较。
#: 2026-09-19 瘦身时搬来就地定义（原定义在同日删除的提案模块里），值逐字不变：compute_hash /
#: content_fingerprint / NegationParameterPack.content_hash 三处载荷都带着它，
#: 而 CSV 文件名与报表印的正是 content_fingerprint——值一变，旧文件就对不上新报表。
CANONICALIZATION_VERSION = "sha256-jsonc1"


class NegationParameterPack(BaseModel):
    """参数包白名单：字段与取值范围都是闭集，越界即拒绝（DEC-105）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lookback_days: int
    min_spend: Money
    min_clicks: int
    max_data_staleness_hours: int

    @model_validator(mode="after")
    def _whitelist_bounds(self) -> NegationParameterPack:
        if not 7 <= self.lookback_days <= 90:
            raise ValueError("lookback_days must be within [7, 90]")
        if self.min_clicks < 10:
            raise ValueError("min_clicks below 10 lacks statistical meaning; refused")
        if self.min_spend.amount <= 0:
            raise ValueError("min_spend must be positive")
        if not 1 <= self.max_data_staleness_hours <= 72:
            raise ValueError("max_data_staleness_hours must be within [1, 72]")
        return self

    def content_hash(self) -> str:
        """参数包合同 hash：参数变更 = 合同重签。"""
        payload = {
            "canonicalization": CANONICALIZATION_VERSION,
            "pack": self.model_dump(mode="json"),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class CandidateSetError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SearchTermRecord(BaseModel):
    """一个 (AdGroup, 搜索词) 在回看窗口内的绩效——策略的唯一输入形态。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: CanonicalEntityRef
    search_term: str
    clicks: int
    conversions: int
    spend: Money
    #: 曝光。**不参与任何判定**，只给人看——但它是「该不该否掉这个词」的关键前提：
    #: 42 次点击来自 300 次曝光（CTR 14%，流量高度相关，问题多半在 listing 或价格，
    #: 否掉是把好流量扔了）和来自 6 万次曝光（CTR 0.07%，纯粹不相关，该否）
    #: 是两个相反的结论，而证据行上「花费 87.40 / 点击 42 / 广告订单 0」两者逐字相同。
    #: None = 源侧没给或读不出来，不编（展示型指标解析失败不许毙掉候选，
    #: 与当年镜像模块（已删）的分法同源：判定型才抛，展示型置 None）。
    impressions: int | None = None
    window_start: datetime
    window_end: datetime
    data_as_of: datetime
    #: 活动名 / 广告组名（源侧现值，与这批指标出自同一行）。
    #: 2026-08-30 实测：导出的 CSV 有 campaign_name / ad_group_name 两列，而它们
    #: 此前只从镜像解析。真实店 11,723 个活动、镜像默认只拉 3 页 300 个（2.5%），
    #: 于是 7 条候选 0 条解析得出名字——表头承诺了名称，文件交出两列空白，人只能
    #: 拿 15 位数字去领星后台逐行反查，而那两列**正是为免除逐行反查才加的**。
    #: 而搜索词报表行本身就带着这两个名字（实测 null 率 3.2%），一直被丢掉。
    #: 顺带修掉一个更隐蔽的问题：镜像解析出的名字来自另一个时点，可能与这批指标
    #: 所在的窗口对不上；同一行来的名字不会。None = 源侧就没给，不编。
    campaign_name: str | None = None
    ad_group_name: str | None = None
    #: 这条"搜索词"其实是一个 ASIN，不是关键词。
    #: 本策略产出的是**否定精准关键词**（match_type 恒为 NEGATIVE_EXACT）。把一个
    #: ASIN 写进否定关键词，在 Amazon 上不会挡住任何东西——ASIN 型的来源要用
    #: 「否定投放」页签才否得掉（页签名取自 2026-08-29 只读实测的领星 SP 页签清单：
    #: …/关键词/商品投放/否定词/否定投放/…，见 docs/evidence/lx-ads-ia-20260829.md §1；
    #: 该页签**里面**怎么操作未实测，不要在文案里替人写步骤）。于是人照着 CSV 做完，
    #: 钱继续烧，而证据行上花费、点击、零转化样样属实，**从证据里看不出无效**。
    #: 2026-08-30 实测：搜索词报表每个数据行都带 `is_asin`（int，null 率 3.2%
    #: 恰等于汇总行占比），本仓库此前从不读它；请求也不带 `search_type`，
    #: 所以 query/kw/asin 三类全都会回来。抽样 30 行全为 0，**未观测到 1**——
    #: 非零即视为 ASIN 是按字段名与工具 schema 的 search_type 取值推的，不是实测。
    term_is_asin: bool = False

    @model_validator(mode="after")
    def _validate(self) -> SearchTermRecord:
        # 时区检查必须在最前面：下面的 window_end <= window_start 与策略里的
        # now - data_as_of 都要拿这些时刻做比较/减法，naive 混进来先炸的是裸
        # TypeError，穿透 MCP 面后只剩一句 Error executing tool，拒绝的理由丢失。
        # 当年镜像与授权书模块（已删）都有这道闸，策略输入曾是唯一缺口（2026-08-30 补上）。
        for label in ("window_start", "window_end", "data_as_of"):
            moment: datetime = getattr(self, label)
            if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
                raise ValueError(
                    f"NAIVE_DATETIME_REJECTED: {label} must be timezone-aware (UTC); "
                    f"got naive {moment.isoformat()}"
                )
        if self.scope.entity_type is not EntityType.AD_GROUP:
            raise ValueError("negation scope must be an AD_GROUP ref (DEC-015: 单个精准否定)")
        if not self.search_term.strip():
            raise ValueError("search_term must be non-empty")
        if self.clicks < 0 or self.conversions < 0:
            raise ValueError("clicks/conversions must be non-negative")
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be after window_start")
        return self


class CandidateEvidence(BaseModel):
    """每个候选必须携带完整证据（无证据不成候选）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    spend: Money
    clicks: int
    conversions: int
    #: 进 compute_hash 是对的：人在报表里看到的就是这个数，AX-07 要求
    #: 交给领星的内容与被看见的内容是同一份。
    impressions: int | None = None
    window_start: datetime
    window_end: datetime
    data_as_of: datetime


class NegationCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: CanonicalId
    rule_class: Literal[RuleClass.NEG_EXACT_CANDIDATE]
    scope: CanonicalEntityRef
    search_term: str
    match_type: Literal["NEGATIVE_EXACT"] = "NEGATIVE_EXACT"
    evidence: CandidateEvidence
    #: 人看的名字，随证据一起冻结（因此也进 set_hash）。进 hash 是对的：人在报表里
    #: 看到的就是这两个名字，AX-07 要求交给领星的内容与被看见的内容是同一份。
    #: 名字在冻结集合里不可变，所以不会引起 CONTENT_DRIFT。
    campaign_name: str | None = None
    ad_group_name: str | None = None


class AbstainReason(StrEnum):
    STALE_DATA = "STALE_DATA"
    #: 这条本该成为候选——花了钱、有点击、零转化——但它是个 ASIN，
    #: 本策略唯一的手段（否定精准关键词）对它无效。见 SearchTermRecord.term_is_asin。
    #: 与 STALE_DATA 的共同点不是"数据有问题"，而是"我没能给出可执行的结论"：
    #: 这两种都必须显式说出来，否则它们与"这个词没问题"在响应里逐字同形。
    ASIN_NOT_A_KEYWORD = "ASIN_NOT_A_KEYWORD"


class AbstainRecord(BaseModel):
    """没能给出可执行结论的词，不允许静默当作"没有候选"——必须显式上报（Fail Loudly）。

    两种情形都在这里：数据不足以判断（STALE_DATA），以及判断得出、但本策略的手段
    对它无效（ASIN_NOT_A_KEYWORD）。前者要等新数据，后者要人去领星手工处理——
    共同点是**人还有事要做**，而"正常排除"（有转化、证据不够）没有。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    search_term: str
    reason: AbstainReason
    detail: str


class NegationRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidates: tuple[NegationCandidate, ...]
    abstains: tuple[AbstainRecord, ...]
    #: 输入行数。一行 = 一个 (广告组, 搜索词) 组合，**不是**一个搜索词：同一个词投在
    #  三个广告组里就是三行。Mock 源下两者恰好相等，真实源下不等——见下方字段。
    evaluated_count: int
    #: 去重后的搜索词个数。之所以要单独一个数：`evaluated_count` 唯一的读者是 AI，
    #  而 AI 唯一的动作是把它讲给人听。「评估了 137 个搜索词」在真实源下是假话
    #  （137 是组合数），且人无法从响应里察觉——他会拿这个数去和领星后台的词数对账，
    #  对不上，然后怀疑自己而不是怀疑这个数。给出两个数，两句话都能说真。
    distinct_search_terms: int

    @property
    def asin_abstain_count(self) -> int:
        """其中有几条是"钱在烧、但本策略否不掉"。

        单独给一个数，理由和 distinct_search_terms 相同：abstains 在响应里会被截断，
        而这个数恒如实。它 > 0 时，candidate_count 再漂亮也不代表这个店已经处理完了。
        """
        return sum(1 for a in self.abstains if a.reason is AbstainReason.ASIN_NOT_A_KEYWORD)


def generate_negation_candidates(
    records: Sequence[SearchTermRecord],
    pack: NegationParameterPack,
    now: datetime,
    id_factory: Callable[[], uuid.UUID],
) -> NegationRunResult:
    """证据门语义：

    - conversions > 0：正常排除（永不候选，规则定义）。
    - 证据不足（clicks / spend 低于门槛）：正常排除——"还没攒够证据"。
    - 数据过旧：ABSTAIN——"无法判断"不同于"没有候选"，必须显式上报。
    - 证据齐了、但这个词是 ASIN：ABSTAIN——"判断得出、否不掉"同样不是"没有候选"。
      否定精准关键词挡不住 ASIN 型来源，静默跳过会让这笔浪费从此无人再提。
    """
    seen: set[tuple[str, ...]] = set()
    candidates: list[NegationCandidate] = []
    abstains: list[AbstainRecord] = []
    for record in records:
        key = (*record.scope.uniqueness_key(), record.search_term.casefold())
        if key in seen:
            # 与下面的 CURRENCY_MISMATCH 同一条理由（见那里的注释）：裸异常穿透 MCP 面
            # 后只剩一句 Error executing tool。真实数据源按 (广告组 × 词 × 匹配方式 ×
            # 来源) 出行，同一个 (广告组, 词) 天然分裂成多行，这条会被真的踩到。
            raise CandidateSetError(
                "DUPLICATE_SEARCH_TERM_ROW",
                f"duplicate search-term row for {record.search_term!r}; "
                "upstream must aggregate one row per (scope, term)",
            )
        seen.add(key)
        if record.spend.currency != pack.min_spend.currency:
            # 带码的类型化错误，不是裸 ValueError——2026-08-29 排查（mandate-1/
            # approval-2）：裸异常穿透 MCP 面后只剩一句 Error executing tool，
            # 人分不清「币种签错了」和「服务器坏了」，授权书还一直显示 ACTIVE。
            raise CandidateSetError(
                "CURRENCY_MISMATCH",
                f"data rows are {record.spend.currency} but the parameter pack is "
                f"{pack.min_spend.currency}; assemble per-currency runs",
            )
        staleness = now - record.data_as_of
        if staleness > timedelta(hours=pack.max_data_staleness_hours):
            abstains.append(
                AbstainRecord(
                    search_term=record.search_term,
                    reason=AbstainReason.STALE_DATA,
                    detail=f"data_as_of is {staleness} old, limit {pack.max_data_staleness_hours}h",
                )
            )
            continue
        if record.conversions > 0:
            continue
        if record.clicks < pack.min_clicks or record.spend.amount < pack.min_spend.amount:
            continue
        if record.term_is_asin:
            # 位置是有讲究的：必须在证据门**之后**。放在前面会把成千上万个只花了几毛
            # 钱、本来就不可能成为候选的 ASIN 行全部写进 abstains，把真正要人动手的那
            # 几条埋掉（abstains 至今没有 truncated_from 保护）。放在这里，列出来的
            # 每一条都是"这笔钱确实在白烧，而我开不出能挡住它的否定词"。
            abstains.append(
                AbstainRecord(
                    search_term=record.search_term,
                    reason=AbstainReason.ASIN_NOT_A_KEYWORD,
                    detail=(
                        f"spent {record.spend.amount} {record.spend.currency} over "
                        f"{record.clicks} clicks with no orders, but this search term is an ASIN; "
                        "a NEGATIVE_EXACT keyword would not block it. Negate it as a product "
                        "target in Lingxing 否定投放 instead"
                    ),
                )
            )
            continue
        candidates.append(
            NegationCandidate(
                candidate_id=id_factory(),
                rule_class=RuleClass.NEG_EXACT_CANDIDATE,
                scope=record.scope,
                search_term=record.search_term,
                campaign_name=record.campaign_name,
                ad_group_name=record.ad_group_name,
                evidence=CandidateEvidence(
                    spend=record.spend,
                    clicks=record.clicks,
                    conversions=record.conversions,
                    impressions=record.impressions,
                    window_start=record.window_start,
                    window_end=record.window_end,
                    data_as_of=record.data_as_of,
                ),
            )
        )
    return NegationRunResult(
        candidates=tuple(candidates),
        abstains=tuple(abstains),
        evaluated_count=len(records),
        # casefold 与上面那条重复行检查、以及适配器的聚合键逐字同源。用别的口径去重
        # 会让这个数和候选是按什么合并出来的对不上。
        distinct_search_terms=len({r.search_term.casefold() for r in records}),
    )


class CandidateSetState(StrEnum):
    GENERATED = "GENERATED"
    FROZEN = "FROZEN"


class NegationCandidateSet(BaseModel):
    """候选集合：冻结后 set_hash 钉住这一份内容（AX-07：人看见的与人拿去执行的是同一份）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    set_id: CanonicalId
    organization_id: CanonicalId
    parameter_pack: NegationParameterPack
    candidates: tuple[NegationCandidate, ...]
    generated_at: datetime
    created_by_client_id: str
    created_by_person_id: str | None
    source: str  # "AI" | "HUMAN"
    state: CandidateSetState = CandidateSetState.GENERATED
    set_hash: str | None = None
    #: 本次实际命中多少个候选；超出每轮上限被截断时才不为 None。
    #: 这个事实必须跟着集合走：人看到「20 个候选词」、核对完 20 条就会认为
    #: 「这就是这次找出来的全部浪费」，而实际命中 137 个、117 个被静默丢弃
    #: （2026-09 之前它只进 MCP 返回值，看清单的人从来看不到）。
    truncated_from: int | None = None
    #: 同一轮里"钱在烧、但本策略否不掉"的词有几个（ASIN 型搜索词）。
    #: 和 truncated_from 是同一个病的两个入口：这个事实此前只进 MCP 返回值，而
    #: 签字的人看到的只有这份清单本身，读出来是"本轮的浪费都在这儿了"。
    #: 不进 compute_hash——它不是被批准的内容，是被批准内容的**边界说明**，
    #: 与 truncated_from 同一条理由。
    asin_abstain_count: int = 0
    #: 那几个 ASIN 到底是哪几个。计数没有词就等于「知道有钱在烧，但说不出烧在哪」：
    #: 人正要去领星的那一刻，知道该去哪个页签、唯独拿不到要否定的那个词
    #: （2026-09 之前词表不进任何存储，只能这样丢失）。
    #: 与 asin_abstain_count 同理由不进 compute_hash：边界说明，不是被批准的内容。
    asin_abstain_terms: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _non_empty(self) -> NegationCandidateSet:
        if not self.candidates:
            raise ValueError("candidate set must contain at least one candidate")
        if self.asin_abstain_terms and len(self.asin_abstain_terms) != self.asin_abstain_count:
            raise ValueError("asin_abstain_terms must match asin_abstain_count")
        return self

    def compute_hash(self) -> str:
        items: list[dict[str, Any]] = [c.model_dump(mode="json") for c in self.candidates]
        items.sort(key=lambda d: str(d["candidate_id"]))
        payload = {
            "canonicalization": CANONICALIZATION_VERSION,
            "organization_id": str(self.organization_id),
            "candidates": items,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @property
    def profile_external_id(self) -> str | None:
        """这批候选归属的店铺。候选的 scope 里现成就有。

        两家店各有一份集合时，除 uuid 外一切可比信息都可能相同——候选数、生成时间、
        广告组名（同一条产品线在两家店常常就是同名广告组）；文件里若没有一列说明
        该打开哪家店的后台，人就会加错店。跨店时返回 None：说不出唯一一家，
        就不许挑一家说。
        """
        profiles = {c.scope.profile_external_id for c in self.candidates}
        return profiles.pop() if len(profiles) == 1 else None

    def content_fingerprint(self) -> str:
        """同一批词、同一批证据的两次生成得到同一个值——set_hash 不会。

        set_hash 绑定的是**这一份**冻结集合，每条候选的编号进 hash，于是内容逐字
        相同的两次生成必得两个不同的 hash。这对防篡改是对的，但它让「这两份
        是不是同一批发现」无从回答：两份报表并排、hash 不同、词数相同，
        读起来就是两批不同的发现，而人不会去逐词比对。2026-08-30 在真实通道上实测：
        连续两次即席生成（第二次全部命中缓存，输入逐行相同）产出两份 7 条候选的
        FROZEN 集合，set_hash 完全不同。

        本值只回答「是不是同一批」，不参与 set_hash 绑定——两者故意分开：一个必须随
        每次冻结而变，一个必须不变。

        `evidence.data_as_of` 同样要剔除（2026-08-30 排查）。它记的是**取数时刻**，
        真实源逐字取当次调用的 now（providers/lingxing/search_terms.py 的 _Window.derive），
        所以两次生成只在 900 秒缓存窗口内才碰巧相同——缓存一过期，同一批词、同一段
        窗口、同样的花费点击订单，只因为隔了一小时再查，指纹就不同了。上面那句实测
        正是在缓存命中的情况下做的，它看不到这一点。
        后果不是漏报一个提示，而是把「查了，没有重复」这句话说错：same_content_as
        的 [] 明确表示「查过了」，人据此认定这是两批不同的发现，去批第二遍。
        「什么时候查的」不是「查到了什么」——窗口两端已经在这个指纹里了，
        同一段窗口 + 同样的证据数字，就是同一批发现，与何时去查无关。

        `campaign_name` / `ad_group_name` 同理剔除（2026-08-30 排查）。它们是**显示用**
        的名字，从镜像现值解析而来：两次生成之间同步过一次镜像，或某个活动被改了名，
        逐字相同的两批词就会得到两个指纹。最平常的一种顺序就能触发——生成一次、
        同步镜像、再生成一次：第一次镜像里还没有名字（解析为 null），第二次有了。
        对象身份不在名字里而在 scope（entity_external_id + parent_refs），它仍在指纹中，
        所以剔名字不会把两批不同对象的候选混成一批。
        名字仍然进 set_hash：set_hash 绑定的是**人看见的那一份**（AX-07），那里必须含名字。
        这两个 hash 回答的是两个不同的问题，这正是它们分开存在的理由。
        """
        items: list[dict[str, Any]] = []
        for candidate in self.candidates:
            item = candidate.model_dump(mode="json")
            item.pop("candidate_id", None)
            item.pop("campaign_name", None)
            item.pop("ad_group_name", None)
            evidence = item.get("evidence")
            if isinstance(evidence, dict):
                evidence.pop("data_as_of", None)
            items.append(item)
        items.sort(key=lambda d: json.dumps(d, sort_keys=True, ensure_ascii=False))
        payload = {
            "canonicalization": CANONICALIZATION_VERSION,
            "organization_id": str(self.organization_id),
            "candidates": items,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def freeze(self) -> NegationCandidateSet:
        if self.state is not CandidateSetState.GENERATED:
            raise CandidateSetError("NOT_GENERATED", f"cannot freeze from {self.state}")
        return self.model_copy(
            update={"state": CandidateSetState.FROZEN, "set_hash": self.compute_hash()}
        )


class BulkNegativeRow(BaseModel):
    """人工执行用导出行：人拿着 CSV 在领星后台逐条加否定词；组件不核验、不记录那一步。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 这行要加到哪家店。AX-06 要求写入定位靠「精确对象 + 完整父链」，而这张表
    #: 正是真正交到人手上去执行的那份东西——父链此前在店铺这一层就断了。
    #: 两家店的导出文件若只靠文件名区分就无从分辨，而同一条产品线在两家店常常
    #: 就是同名广告组。加错店时没有任何东西会报错。
    profile_external_id: str
    shop_external_id: str
    campaign_external_id: str
    ad_group_external_id: str
    search_term: str
    match_type: Literal["NEGATIVE_EXACT"] = "NEGATIVE_EXACT"
    #: 随候选一起冻结的名称（源侧现值）。缺失时由导出侧回落到镜像解析。
    campaign_name: str | None = None
    ad_group_name: str | None = None


def to_bulk_rows(candidate_set: NegationCandidateSet) -> tuple[BulkNegativeRow, ...]:
    if candidate_set.state is not CandidateSetState.FROZEN:
        raise CandidateSetError("NOT_FROZEN", "only frozen sets may be exported")
    rows: list[BulkNegativeRow] = []
    for c in candidate_set.candidates:
        campaign_id = c.scope.parent_refs.campaign_external_id
        if campaign_id is None:  # pragma: no cover - 实体校验已保证父链存在
            raise CandidateSetError("MISSING_PARENT", "ad group ref lost its campaign parent")
        rows.append(
            BulkNegativeRow(
                profile_external_id=c.scope.profile_external_id,
                shop_external_id=c.scope.shop_external_id,
                campaign_external_id=campaign_id,
                ad_group_external_id=c.scope.entity_external_id,
                search_term=c.search_term,
                campaign_name=c.campaign_name,
                ad_group_name=c.ad_group_name,
            )
        )
    # 行序贴人的执行路径，不贴取数顺序。CSV 的每一行都是人到领星后台手工加的一条，
    # 而加的方式是「下钻到某个广告组 → 在它的『否定词』页签里加」。
    # 上游按花费倒序跨广告组交错取行，照抄过来就是让人在活动之间来回下钻几十次，
    # 同一个广告组反复打开——真实一批 50~150 行时这是纯粹的白跑。
    # 归组后组内保持原序（list.sort 稳定），也就是仍按花费从高到低——
    # 人中途停下时，先做掉的仍是最贵的那些。
    # 排序键用外部 ID 不用名称：名称可能为 None，也可能重名，而 ID 恒有且唯一。
    rows.sort(key=lambda r: (r.campaign_external_id, r.ad_group_external_id))
    return tuple(rows)


def render_bulk_csv(
    rows: Sequence[BulkNegativeRow],
    *,
    name_of: Callable[[str, str], str | None] | None = None,
) -> str:
    """人工执行用 CSV（UTF-8 带 BOM，含表头）。列名即 canonical 字段名，不做本地化。

    name_of("campaign"|"ad_group", external_id) 可选：提供时在 ID 列之后追加
    campaign_name / ad_group_name 两列（镜像现值解析；缺名留空，不编造）。
    运营在领星后台按名称导航，纯 ID 清单要人逐行反查（2026-08-29 审计 #5）。
    不提供时列集与既往一致；自由文本单元格若以公式引导符开头会被前置单引号中和
    （防 CSV 公式注入），其余逐字不变。

    BOM 是给 Excel 的：这张表就是运营照着往平台后台敲否定词的依据，双击打开是
    这个角色的默认动作，而无 BOM 的 UTF-8 在 Excel 里会把日文/德文搜索词显示成
    乱码——人要么照着乱码敲出错的否定词，要么得先学会「从文本导入并手动选编码」
    （2026-08-29 排查 approval-6）。程序化消费方按 UTF-8 读会拿到 U+FEFF 前缀，
    用 utf-8-sig 解码即可；这是 BOM 的标准代价，比让人敲错词便宜。
    """
    import csv
    import io

    def defuse(cell: str) -> str:
        # 二轮审计（CSV 公式注入）：电子表格把以 = + - @ 制表符/回车开头的单元格
        # 当公式执行。search_term 由站外真实搜索输入构成（攻击者可控），名称列
        # 来自源侧现值——这张表刻意为双击打开设计，必须在生成侧中和：前置单引号。
        # 只处理自由文本列；ID/match_type 是受控枚举不动。
        return "'" + cell if cell[:1] in ("=", "+", "-", "@", "\t", "\r") else cell

    buffer = io.StringIO()
    buffer.write("\ufeff")
    writer = csv.writer(buffer, lineterminator="\n")
    header = [
        "profile_external_id",
        "shop_external_id",
        "campaign_external_id",
        "ad_group_external_id",
        "search_term",
        "match_type",
    ]
    if name_of is not None:
        header += ["campaign_name", "ad_group_name"]
    writer.writerow(header)
    for row in rows:
        cells: list[str] = [
            row.profile_external_id,
            row.shop_external_id,
            row.campaign_external_id,
            row.ad_group_external_id,
            defuse(row.search_term),
            row.match_type,
        ]
        if name_of is not None:
            # 先用候选自带的名字（与这批指标出自同一行、随冻结集合一起被人看见），
            # 没有才回落到 name_of 解析。反过来会让**另一时点**的名字盖掉
            # 人在报表里实际看过的那个。
            campaign_name = row.campaign_name or name_of("campaign", row.campaign_external_id)
            ad_group_name = row.ad_group_name or name_of("ad_group", row.ad_group_external_id)
            cells.append(defuse(campaign_name or ""))
            cells.append(defuse(ad_group_name or ""))
        writer.writerow(cells)
    return buffer.getvalue()
