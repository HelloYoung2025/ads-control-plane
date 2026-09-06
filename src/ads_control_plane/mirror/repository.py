"""快照仓库（SnapshotRepository）——append-only 语义（DEC-117）。

镜像不覆写历史：每次同步只追加新快照，"现值"是查询时的推导
（每 object_key 取 recorded_at 最新一条），不是存储时的覆盖。
因此仓库协议**没有删除/更新方法**——append-only 是接口形状本身，
不是实现自律。
"""

from __future__ import annotations

from typing import Protocol

from ads_control_plane.mirror.snapshot import AdObjectSnapshot
from ads_control_plane.tasks.directive import ObjectLevel


class SnapshotRepository(Protocol):
    """镜像仓库协议：追加、取现值、看历史——仅此三件事。"""

    def append(self, snap: AdObjectSnapshot) -> None: ...

    def current(
        self, profile_id: str, level: ObjectLevel | None = None
    ) -> list[AdObjectSnapshot]: ...

    def history(self, object_key: str) -> list[AdObjectSnapshot]: ...


class InMemorySnapshotRepository:
    """内存实现：P0 演示与测试用；PG 版本共享同一协议与 append-only 语义。"""

    def __init__(self) -> None:
        self._snapshots: list[AdObjectSnapshot] = []
        # 二轮审计：history() 每次全表线性扫——审批列表对每个候选行调两次 name_of，
        # 单次请求成本 O(集合数×候选数×快照总数)，多店多集合下审批页会越用越慢。
        # append-only 无删除，按 object_key 分桶一劳永逸；引用共享，不复制快照。
        self._by_key: dict[str, list[AdObjectSnapshot]] = {}

    def append(self, snap: AdObjectSnapshot) -> None:
        self._snapshots.append(snap)
        self._by_key.setdefault(snap.object_key, []).append(snap)

    def current(self, profile_id: str, level: ObjectLevel | None = None) -> list[AdObjectSnapshot]:
        """每 object_key 取 recorded_at 最新一条；同刻并列时后追加者胜。

        返回按 object_key 升序，保证遍历确定性。
        """
        latest: dict[str, tuple[int, AdObjectSnapshot]] = {}
        for index, snap in enumerate(self._snapshots):
            if snap.profile_id != profile_id:
                continue
            if level is not None and snap.level is not level:
                continue
            held = latest.get(snap.object_key)
            if held is None or (snap.recorded_at, index) >= (held[1].recorded_at, held[0]):
                latest[snap.object_key] = (index, snap)
        return [snap for _, (_, snap) in sorted(latest.items())]

    def history(self, object_key: str) -> list[AdObjectSnapshot]:
        """该对象全部快照按 recorded_at 升序；未知 object_key 返回空列表。"""
        rows = list(self._by_key.get(object_key, ()))
        rows.sort(key=lambda snap: snap.recorded_at)  # 稳定排序：同刻保追加序
        return rows
