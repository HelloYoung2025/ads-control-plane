"""把人说的一句话认成一个命令。确定性：同一句话永远认成同一个命令，认不出就是「没听懂」。

为什么不让模型来认（2026-09-24 定稿计划第二节第 6 条）：模型要把孩子的话翻成店铺、
编号、数字，它会猜；猜错的店、猜错的百分比没有任何报错。这里只认一小撮说法，
模型只负责把原话一字不改地递过来。

认得的说法（S1a）：
- 看今天 / 全部停下（停、暂停、别改了）/ 继续干活 / 我能说什么
- 管 <店名> <ASIN> [叫 <小名>]      ——大人把商品交给它
- 不管<小名>了
- <小名>最多<N>%                    ——ACOS 上限
- [<小名>]现在看一遍
- 好                                ——接受刚才给的安全值
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum


class Kind(StrEnum):
    TODAY = "TODAY"
    STOP = "STOP"
    RESUME = "RESUME"
    HELP = "HELP"
    ADOPT = "ADOPT"
    DROP = "DROP"
    TARGET = "TARGET"
    LOOK = "LOOK"
    YES = "YES"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, kw_only=True)
class Command:
    kind: Kind
    name: str | None = None
    store: str | None = None
    asin: str | None = None
    percent: int | None = None


#: /k 菜单插进来的句子带这个前缀（S1b）；人自己打的话一般没有。两种都认。
PREFIX = "操盘手："

_TODAY = {"看今天", "今天怎么样", "今天呢", "看看今天"}
_STOP = {"全部停下", "停", "停下", "暂停", "全部暂停", "别改了", "停止"}
_RESUME = {"继续干活", "继续", "接着干", "开始干活"}
_HELP = {"我能说什么", "能说什么", "帮助", "怎么用", "菜单"}
_YES = {"好", "好的", "可以", "行", "嗯"}
_LOOK_WORDS = ("现在看一遍", "看一遍")

#: 小名：1 到 12 个中文、字母或数字。不许有空格和标点——它要能嵌进别的说法里被认出来。
NAME = r"[0-9A-Za-z一-鿿]{1,12}"
_NAME_RE = re.compile(rf"^{NAME}$")
#: 店名沿用配置里的昵称规则（sfw/config.py 的 NICKNAME_RE），这里只管切词。
_ADOPT = re.compile(
    rf"^管\s*(?P<store>[\w一-鿿-]{{1,20}})\s+(?P<asin>[0-9A-Za-z]{{10}})"
    rf"(?:\s*叫\s*(?P<name>{NAME}))?$"
)
#: 小名取最短的（`{1,12}?`）：句末的「了」是语气词。贪心匹配会把「不管猫抓板了」读成
#: 去退一个叫「猫抓板了」的商品——而这句正是帮助表里教的说法。
_DROP = re.compile(rf"^不管\s*(?P<name>{NAME}?)\s*了?$")
_TARGET = re.compile(rf"^(?P<name>{NAME})\s*最多\s*(?P<n>\d{{1,3}})\s*%?$")

#: 这些词本身就是命令，不能拿来当小名：「看今天最多20%」会被读成给「看今天」定上限。
RESERVED = _TODAY | _STOP | _RESUME | _HELP | _YES | {"现在", "管", "不管", "全部"}


def _clean(text: str) -> str:
    """全角转半角（NFKC 会把「２５％」变成「25%」），去掉首尾空白和句末标点。"""
    text = unicodedata.normalize("NFKC", text).strip()
    text = text.removeprefix(PREFIX.replace("：", ":")).removeprefix(PREFIX).strip()
    return text.rstrip("。.!！?？~～ ").strip()


def is_name(text: str) -> bool:
    return bool(_NAME_RE.fullmatch(text)) and text not in RESERVED


def parse(text: str) -> Command:
    said = _clean(text)
    if said in _TODAY:
        return Command(kind=Kind.TODAY)
    if said in _STOP:
        return Command(kind=Kind.STOP)
    if said in _RESUME:
        return Command(kind=Kind.RESUME)
    if said in _HELP:
        return Command(kind=Kind.HELP)
    if said in _YES:
        return Command(kind=Kind.YES)
    if (found := _ADOPT.fullmatch(said)) is not None:
        name = found["name"] or found["asin"].upper()
        if found["name"] is not None and not is_name(name):
            return Command(kind=Kind.UNKNOWN)
        return Command(kind=Kind.ADOPT, store=found["store"], asin=found["asin"].upper(), name=name)
    if (found := _DROP.fullmatch(said)) is not None and is_name(found["name"]):
        return Command(kind=Kind.DROP, name=found["name"])
    if (found := _TARGET.fullmatch(said)) is not None and is_name(found["name"]):
        return Command(kind=Kind.TARGET, name=found["name"], percent=int(found["n"]))
    for word in _LOOK_WORDS:
        if said.endswith(word):
            name = said.removesuffix(word).removesuffix("现在").strip()
            if not name:
                return Command(kind=Kind.LOOK)
            if is_name(name):
                return Command(kind=Kind.LOOK, name=name)
    return Command(kind=Kind.UNKNOWN)
