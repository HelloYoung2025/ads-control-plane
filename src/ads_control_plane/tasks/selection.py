"""勾选集（SelectionSet）——人勾选的显式对象集合（DEC-122/124）。

2026-08-28 Owner 需求：策略包与任务的作用范围来自人的勾选，而不是运行时
再展开的模糊筛选。勾选即冻结为显式 ID 集合：同一 profile、去重后不超过
MAX_AFFECTED_OBJECTS（与中途介入指令共用"手术刀不是推土机"的同一条上限，
DEC-122 维持 200）。

keyword 不扩层级枚举：并入 TARGET 层，由镜像快照的 keyword_text 区分。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

from ads_control_plane.tasks.directive import MAX_AFFECTED_OBJECTS, ObjectLevel, ObjectSelector


class SelectionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SelectedObject(BaseModel):
    """单个勾选项：层级 + 外部 ID + 所属 profile，三者齐备才可寻址。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    level: ObjectLevel
    external_id: str
    profile_external_id: str

    @model_validator(mode="after")
    def _non_empty(self) -> SelectedObject:
        if not self.external_id.strip():
            raise ValueError("selected object requires a non-empty external_id")
        if not self.profile_external_id.strip():
            raise ValueError("selected object requires a non-empty profile_external_id")
        return self


class SelectionSet(BaseModel):
    """冻结的勾选集。构造即校验：空集/跨 profile/超上限都是显式错误，不静默。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[SelectedObject, ...]

    @model_validator(mode="after")
    def _contract(self) -> SelectionSet:
        if not self.items:
            raise SelectionError("SELECTION_EMPTY", "selection must contain at least one object")
        profiles = {item.profile_external_id for item in self.items}
        if len(profiles) > 1:
            raise SelectionError(
                "SELECTION_MIXED_PROFILE",
                f"selection spans profiles {sorted(profiles)}; one selection covers one profile",
            )
        distinct = {(item.level, item.external_id) for item in self.items}
        if len(distinct) > MAX_AFFECTED_OBJECTS:
            raise SelectionError(
                "SELECTION_TOO_BROAD",
                f"selection covers {len(distinct)} objects (max {MAX_AFFECTED_OBJECTS}); "
                "split into multiple selections",
            )
        return self

    @property
    def profile_external_id(self) -> str:
        """勾选集唯一的 profile（构造校验保证非空且同 profile）。"""
        return self.items[0].profile_external_id

    def to_selectors(self) -> tuple[ObjectSelector, ...]:
        """按层级分组为显式 ID 型选择器（去重、保持首次出现顺序）。

        只产 external_ids 型——ObjectSelector 的合同是显式 ID 与筛选互斥，
        勾选集是人已经点名的对象，永远走显式 ID 一侧。

        分组后对总量做第二次上限检查：pydantic 的 model_copy(update=...) 不重跑
        校验器，构造期检查可被绕过——展开为选择器是勾选集离开域层的唯一出口，
        在出口处再锁一次（与构造期同码 SELECTION_TOO_BROAD）。
        """
        grouped: dict[ObjectLevel, dict[str, None]] = {}
        for item in self.items:
            grouped.setdefault(item.level, {})[item.external_id] = None
        total = sum(len(ids) for ids in grouped.values())
        if total > MAX_AFFECTED_OBJECTS:
            raise SelectionError(
                "SELECTION_TOO_BROAD",
                f"selection expands to {total} objects (max {MAX_AFFECTED_OBJECTS}); "
                "split into multiple selections",
            )
        return tuple(
            ObjectSelector(level=level, external_ids=tuple(ids)) for level, ids in grouped.items()
        )
