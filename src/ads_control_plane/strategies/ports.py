"""策略数据端口：策略引擎只认协议，不认具体 Provider（端口先行、实现换绑，ADR-003）。

真实领星实现（MCP 搜索词报告 / 开放平台报表）在 DEC-009 快照冻结与
fixture 录制后落地于 providers/lingxing/；在那之前唯一实现是 Mock。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from ads_control_plane.strategies.negation import SearchTermRecord

#: 窗口右端相对取数时刻往回退的天数（Owner 2026-08-30 定为 3）。
#: 不是随手写的偏移量：Amazon SP 订单有 7~14 天归因窗口，窗口末端几天的行今天
#: orders=0、三天后可能变成 2。按「窗口内 orders==0」否定，等于系统性地拿最新、
#: 最未结算的几天判死刑，而这是数据本身完全看不出来的缺陷——证据行上一切正常。
#:
#: 放在端口而不是某个 Provider 里：它是「回看窗口是怎么划的」这个契约的一部分，
#: 人在界面上读到的「统计 X 到 Y」对每个实现都必须是同一个区间。
#: 此前只有真实源有这套算法，Mock 手搓了另一套（不对齐日界、退 1 天），于是演示教给
#: 人的是「29 天 + 最近 2 天」，生产会显示「30 天 + 最近 3 天」（2026-09-07 排查）。
ATTRIBUTION_LAG_DAYS = 3


def attribution_window(*, lookback_days: int, as_of: datetime) -> tuple[datetime, datetime]:
    """回看窗口 [start, end)：按 UTC 日历天对齐，右端退 ATTRIBUTION_LAG_DAYS 天。

    闭区间天数恰为 lookback_days——界面上「回看 N 天」与「统计区间 X 至 Y」
    必须数得出同一个 N，否则人一数就发现对不上，而没有任何东西会报错。
    """
    end_date = as_of.astimezone(UTC).date() - timedelta(days=ATTRIBUTION_LAG_DAYS)
    start_date = end_date - timedelta(days=lookback_days - 1)
    start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=UTC)
    end = datetime(end_date.year, end_date.month, end_date.day, tzinfo=UTC) + timedelta(days=1)
    return start, end


class SearchTermSourceError(Exception):
    """数据源取数失败——带码，是端口契约的一部分而非某个实现的内部细节。

    定义在这里而不是让调用方去 except 具体 Provider 的异常：strategy_service 一旦
    `except LxReadError`，策略工具面就认识了具体 Provider，而 ports.py 存在的意义
    就是挡住这件事（ADR-003 端口先行）。实现方负责把自己的异常翻译成本类型，
    并保留原始 code 与 __cause__。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, kw_only=True)
class UnjudgedGroup:
    """一个这一轮没能被判断的 (广告组, 搜索词)。

    带上身份是为了让调用方能回答「它落在我这份授权圈定的范围里吗」——只给一个总数
    的话，一份只管 1 个活动的授权书会把全店的坏数据都算到自己头上，永远挂着红灯，
    而红灯的意义随之作废（2026-08-30 排查）。

    campaign_external_ids 通常只有一个；同一个广告组出现在两个活动下（源侧数据损坏，
    也是这个组被丢掉的原因之一）时会有多个，此时任一活动落在作用域内就算落在里面——
    宁可多说一次「有东西没判断」，也不要因为源侧自相矛盾而漏说。
    """

    ad_group_external_id: str
    campaign_external_ids: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class SearchTermFetch:
    """一次取数的结果，**以及这次取数丢掉了什么**。

    为什么丢弃账目必须与记录同批返回，而不是让实现挂在 `last_*` 实例属性上等人回头读：
    一个 source 实例服务全部店铺，两个店并发取数时只有一个槽位，后到的覆盖先到的，
    调用方读到的是另一个店的账目——数字全都在、全都不对，且没有任何迹象表明它们
    不该被一起读。同一形状的缺陷 2026-08-30 已经在 `_assert_rows_are_usable` 上
    修过一次，那次它让一道安全闸反向失效。

    unjudged_ad_group_terms / unattributable_rows 不是诊断，是**结论的完整性**：
    它们 > 0 时，「0 条候选」不等于「这个店这段窗口没有浪费」，只等于
    「在我看得懂的那部分里没有」。少了这两个数，调用方（AI）会把前者讲给人听。
    两个数单位不同、互不重叠，绝不相加：一个数组、一个数行，加起来是个无意义的数。
    """

    records: tuple[SearchTermRecord, ...] = ()
    #: 上游自报的总行数（信封 total）。None = 上游没说。
    source_total: int | None = None
    skipped_summary_rows: int = 0
    #: 整组丢掉的 (广告组, 搜索词)：这些组这一轮根本没有被判断过。带身份，
    #: 好让调用方按作用域筛（见 UnjudgedGroup）。
    unjudged_groups: tuple[UnjudgedGroup, ...] = ()
    #: 连属于哪个组都看不出来的行——它们无法计入上面那个数，也**无法按作用域归属**：
    #: 连广告组是谁都读不出来，就说不出它落在哪份授权的范围里。恒为全店口径。
    unattributable_rows: int = 0
    #: 读不出来的行总数（含上一行那些）。它是「账要对得上」的那一格：
    #: source_total = unreadable_rows + usable_rows。汇总行与跨页重复行是上游在
    #: total 之外多给的（见 providers/lingxing/search_terms.py 的实测注释），不进这条等式。
    #: 少了这一格，有身份但指标读不出来的行在任何行级计数里都不出现，读响应的人
    #: 按账目相减会得出「行全部可用」，而被丢掉的恰恰是可能携带订单的那些。
    unreadable_rows: int = 0
    #: 真正参与聚合的行数。与上面三项一起把 source_total 填平。
    usable_rows: int = 0
    duplicate_rows: int = 0
    served_from_cache: bool = False

    @property
    def unjudged_ad_group_terms(self) -> int:
        """全店口径的未判断组数。按作用域筛过的那个数由调用方算（它才知道作用域）。"""
        return len(self.unjudged_groups)

    @property
    def is_complete(self) -> bool:
        """这一轮有没有做到「全都判断过」。False 时空手而归不得被读成「很干净」。"""
        return self.unjudged_ad_group_terms == 0 and self.unattributable_rows == 0


class SearchTermReadPort(Protocol):
    def fetch_search_term_performance(
        self,
        profile_external_id: str,
        lookback_days: int,
        as_of: datetime,
    ) -> SearchTermFetch:
        """返回该 Profile 在回看窗口内按 (AdGroup, 搜索词) 聚合的绩效行 + 丢弃账目。"""
        ...

    def has_profile(self, profile_external_id: str) -> bool:
        """该 Profile 是否接入了本数据源。

        2026-08-29 排查结论 runtime-2：取数返回空列表回答不了"接没接"——"根本
        没接数据源"与"接了、查了、确实没有行"在空列表里长得一模一样，前者会被
        读成"查了没有浪费"。这个问题必须由数据源自己声明，策略面据此在返回里
        标注 profile_has_data_source。
        """
        ...
