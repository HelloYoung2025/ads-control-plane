"""候选集合与目标授权的内存存储（PG 持久化版随 M4 落地，接口不变）。"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime

from ads_control_plane.strategies.mandate import AutomationMandate
from ads_control_plane.strategies.mandate_run import MandateRunRecord
from ads_control_plane.strategies.negation import CandidateSetState, NegationCandidateSet


class CandidateSetNotFound(Exception):
    pass


class MandateNotFound(Exception):
    pass


class StaleWrite(Exception):
    """读到的那一版已经被别人改掉了——这次写回建立在过时的事实上。"""


class InMemoryCandidateSetStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sets: dict[uuid.UUID, NegationCandidateSet] = {}

    def save(self, candidate_set: NegationCandidateSet) -> None:
        """插入或按 set_id 覆盖（状态流转产生新的 frozen 实例）。"""
        with self._lock:
            self._sets[candidate_set.set_id] = candidate_set

    def save_if_state_unchanged(
        self, previous: NegationCandidateSet, updated: NegationCandidateSet
    ) -> None:
        """只在库里那一份仍停在 `previous` 的状态上时才写回。

        批准与拒绝都是「读一份 → 域层算出新的一份 → 写回」，而上面那把锁只护住
        单次 get 和单次 save，护不住这三步中间的那个窗口。两个人同时判同一批：
        两边都读到 FROZEN，都算得出各自的终态，都写成功——后写的赢，先写的那次
        无声消失。两个 HTTP 都是 200，两个人各自收到「已批准」和「已拒绝」，
        而落库的可能恰恰是被否掉的那一版；它会出现在「已批」页签里，等人导出
        CSV 拿去真店执行。这不是「少一次刷新」，是**把人说的『不』执行成了『是』**。

        比状态而不比整个对象：这两条流转（FROZEN→APPROVED / FROZEN→REJECTED）
        依赖的事实只有状态一个，而候选可能有上千条，整对象深比要在锁里做。
        状态一变就说明另一次判断已经落库，这正是要挡的那件事。
        """
        with self._lock:
            current = self._sets.get(previous.set_id)
            if current is None or current.state is not previous.state:
                raise StaleWrite(str(previous.set_id))
            self._sets[updated.set_id] = updated

    def get(self, set_id: uuid.UUID) -> NegationCandidateSet:
        with self._lock:
            found = self._sets.get(set_id)
            if found is None:
                raise CandidateSetNotFound(str(set_id))
            return found

    def list_by_state(
        self, organization_id: uuid.UUID, state: CandidateSetState | None = None
    ) -> tuple[NegationCandidateSet, ...]:
        with self._lock:
            return tuple(
                s
                for s in self._sets.values()
                if s.organization_id == organization_id and (state is None or s.state is state)
            )


class InMemoryMandateRunLog:
    """授权运行的 append-only 流水。配额与最小间隔从这里算，不再从候选集合反推。

    反推错在哪：候选集合只在产出候选时才创建，而「币种签错」「没接数据源」「作用域
    全挡掉」「整批 ABSTAIN」「取数失败」都不产生候选。于是最该被拦住的情形
    （配置错了、上游在报错）反而完全不消耗配额、不刷新间隔，可以被无限次触发——
    对真实数据源来说每次触发是一轮多页读取。详见 mandate_run.py 的模块 docstring。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: list[MandateRunRecord] = []
        #: 已经通过配额闸、但这一轮还没跑完（因而还没记流水）的运行。
        self._in_flight: list[tuple[uuid.UUID, datetime]] = []

    def record(self, run: MandateRunRecord) -> None:
        with self._lock:
            self._runs.append(run)

    @contextmanager
    def claim(
        self,
        mandate_id: uuid.UUID,
        moment: datetime,
        day_of: Callable[[datetime], date],
        check: Callable[[int, datetime | None], None],
    ) -> Iterator[None]:
        """在同一把锁里数一次、判一次、占一个位，跑完（或炸掉）再让出来。

        为什么闸不能只调 count_on_day：判定与写流水之间隔着整整一轮取数——对真实
        源是十几秒的多页读。两个请求都在对方记录之前数到 0，就都被放行，
        max_runs_per_day=1 的授权书一天跑两次（2026-09-07 实测：两次都产出候选
        集合，人在待批里看到两份孪生，而每一次对领星都是一轮 QPS=1 的多页读取）。
        位子占在锁里，第二个请求当场看得见第一个，不必等它跑完。

        判定本身由 check 传进来：「够不够跑」是授权书的合同（配额、间隔、时段、
        有效期），不是流水的事。check 抛异常就不占位，异常原样上抛。

        占位只喂配额闸，不进 count_on_day——后者在 _record_run 之后被调用来对人
        播报剩余额度，那时这一次已经在流水里了，再把自己的占位算一遍会少报一次。
        """
        with self._lock:
            on_date = day_of(moment)
            runs_today = sum(
                1 for r in self._runs if r.mandate_id == mandate_id and day_of(r.ran_at) == on_date
            ) + sum(1 for mid, at in self._in_flight if mid == mandate_id and day_of(at) == on_date)
            moments = [r.ran_at for r in self._runs if r.mandate_id == mandate_id]
            moments += [at for mid, at in self._in_flight if mid == mandate_id]
            check(runs_today, max(moments) if moments else None)
            self._in_flight.append((mandate_id, moment))
        try:
            yield
        finally:
            with self._lock:
                # 值相同的占位彼此可替换，只需保证每个占位恰好让出一个位子。
                self._in_flight.remove((mandate_id, moment))

    def count_on_day(
        self, mandate_id: uuid.UUID, moment: datetime, day_of: Callable[[datetime], date]
    ) -> int:
        """该授权在 moment 所属的那个**配额日**已消耗的运行次数。失败的同样计数。

        「哪一天」由调用方给一个分组函数（见 AutomationMandate.quota_day），这里既不
        默认 UTC 也不自己按时区取 date()：授权书上「一天最多 N 次」与「只在当地几点到
        几点跑」是同一张纸上的两句话，两个「天」不是同一个天时，当地同一天里能跑到
        2N 次。传函数而不是传时区，是因为「同一个天」对跨午夜窗口不等于当地日历日——
        22:00→06:00 的窗口里当地午夜落在窗口正中间，那条规则只有授权书自己知道。
        """
        on_date = day_of(moment)
        with self._lock:
            return sum(
                1 for r in self._runs if r.mandate_id == mandate_id and day_of(r.ran_at) == on_date
            )

    def latest_at(self, mandate_id: uuid.UUID) -> datetime | None:
        """该授权最近一次运行时间（频次间隔判定依据）。失败的运行同样刷新它。"""
        with self._lock:
            times = [r.ran_at for r in self._runs if r.mandate_id == mandate_id]
            return max(times) if times else None

    def latest(self, mandate_id: uuid.UUID) -> MandateRunRecord | None:
        with self._lock:
            runs = [r for r in self._runs if r.mandate_id == mandate_id]
            return max(runs, key=lambda r: r.ran_at) if runs else None

    def recent(self, mandate_id: uuid.UUID, limit: int) -> tuple[MandateRunRecord, ...]:
        """最近若干次运行，新的在前。给人看运行历史用。"""
        with self._lock:
            runs = sorted(
                (r for r in self._runs if r.mandate_id == mandate_id),
                key=lambda r: r.ran_at,
                reverse=True,
            )
            return tuple(runs[:limit])


class InMemoryMandateStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mandates: dict[uuid.UUID, AutomationMandate] = {}

    def save(self, mandate: AutomationMandate) -> None:
        with self._lock:
            self._mandates[mandate.mandate_id] = mandate

    def get(self, mandate_id: uuid.UUID) -> AutomationMandate:
        with self._lock:
            found = self._mandates.get(mandate_id)
            if found is None:
                raise MandateNotFound(str(mandate_id))
            return found

    def list_for_org(self, organization_id: uuid.UUID) -> tuple[AutomationMandate, ...]:
        with self._lock:
            return tuple(m for m in self._mandates.values() if m.organization_id == organization_id)
