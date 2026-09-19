"""Mock 搜索词数据源：SearchTermReadPort 的测试实现（CI 唯一的数据源）。"""

from __future__ import annotations

import threading
from datetime import datetime

from ads_control_plane.strategies.negation import SearchTermRecord
from ads_control_plane.strategies.ports import SearchTermFetch, attribution_window


class MockSearchTermSource:
    """seed 进来什么就返回什么——除了窗口（每次按端口的公共算法现算）。

    测试要的是「seed 进去多旧就是多旧」，那才测得动新鲜度闸本身。此前还有一个
    「跟着时钟一起变旧」的开关，只给常驻演示服务用；那个服务 2026-09-19 整个删掉了，
    开关随之删除。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, list[SearchTermRecord]] = {}
        self.read_call_count = 0

    def seed(self, profile_external_id: str, records: list[SearchTermRecord]) -> None:
        with self._lock:
            self._records[profile_external_id] = list(records)

    def fetch_search_term_performance(
        self,
        profile_external_id: str,
        lookback_days: int,
        as_of: datetime,
    ) -> SearchTermFetch:
        #: 窗口按端口的公共算法现算，与真实源逐字同一套（ports.attribution_window）。
        #  行本身仍是 seed 原样——**窗口裁剪**（按日期丢行）才是真实实现的合同测试，
        #  这里做的是「如实声明这批数字覆盖哪一段」。两者不是一回事：不声明的代价是
        #  人在演示里签一份「回看 7 天」的授权书，证据表却写着 30 天的区间，而授权书
        #  上那个数正是他刚刚亲手选的（2026-09-07 排查）。
        window_start, window_end = attribution_window(lookback_days=lookback_days, as_of=as_of)
        with self._lock:
            self.read_call_count += 1
            records = tuple(
                r.model_copy(update={"window_start": window_start, "window_end": window_end})
                for r in self._records.get(profile_external_id, [])
            )
            # 丢弃类计数全为 0：seed 进来的记录没有「读不出来」这回事。这不是省事——
            # Mock 必须如实说 is_complete=True，否则演示态会挂上一条永远不消失的
            # 「有东西没被判断」，而那句话在演示态是假的。
            #: 但「什么都没丢」不等于「什么都没读」（2026-09-06 排查）。source_total
            #  与 usable_rows 此前一并留在默认值上，于是同一份 MCP 响应里
            #  evaluated_ad_group_terms 说判了 10 组、source_accounting 说
            #  usable_rows: 0 / source_total: null。响应旁边那段注释亲口定的等式
            #  （source_total = 汇总行 + 重复行 + 读不出来的行 + 可用行）在 Mock 下
            #  从来对不上账。读响应的 AI 拿这两个数一减，得到的是「一行可用数据都
            #  没有却产出了候选」——它只能怀疑候选是凭空来的，或者干脆不信这组数字。
            #  Development/CI 是唯一能跑通全链路的地方，账目在这里就得是对的。
            return SearchTermFetch(
                records=records,
                source_total=len(records),
                usable_rows=len(records),
            )

    def has_profile(self, profile_external_id: str) -> bool:
        # seed 过空列表也算接入——那是"接了但当期无行"，不是"根本没接"。这一区分
        # 正是本方法存在的理由（2026-08-29 排查结论 runtime-2）。
        with self._lock:
            return profile_external_id in self._records
