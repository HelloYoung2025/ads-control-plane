"""指路只能指向实测见过的名字。

2026-09-06 真事：给 ASIN 型搜索词写「去领星『否定商品投放』处理」，而 2026-08-29
实测记下来的 SP 页签清单里叫「否定投放」——「否定商品投放」是顺手编的。这句话出现在
弃权说明、工具描述、审批卡片提示和 runbook 四处，人照着去找会找不到，然后怀疑
自己点错了地方。

这与本仓库反复修的是同一个病：把人指向一个不存在的控件，比不给指路更坏。
差别只在于这次指的是**别人家**界面上的控件——而那正是最容易编、也最难当场发现的，
因为我们的测试碰不到领星的页面。

判据故意宽松（子串命中即可）：目的不是校对领星的层级结构，是拦住凭空造名字。
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_IA = _ROOT / "docs/evidence/lx-ads-ia-20260829.md"
#: 只查会被人读到的地方 + 产生这些话的域层。
_SPEAKS_TO_HUMANS = (
    "src/ads_control_plane/strategies/negation.py",
    "src/ads_control_plane/sfw/service.py",
    "src/ads_control_plane/sfw/report.py",
    "src/ads_control_plane/sfw/server.py",
    "src/ads_control_plane/sfw/assets/AGENTS.md",
    "README.md",
)

_NAMED_SCREEN = re.compile(r"领星「([^」]{1,20})」")


def test_every_lingxing_screen_we_send_people_to_appears_in_the_recorded_walkthrough() -> None:
    observed = _IA.read_text(encoding="utf-8")
    invented: list[tuple[str, str]] = []
    for rel in _SPEAKS_TO_HUMANS:
        text = (_ROOT / rel).read_text(encoding="utf-8")
        for name in set(_NAMED_SCREEN.findall(text)):
            if name not in observed:
                invented.append((rel, name))
    assert not invented, (
        "这些领星界面名字在 2026-08-29 的实测走查里不存在，多半是编的："
        f"{invented}。要么改成实测见过的名字，要么先去实测。"
    )


def test_the_walkthrough_that_backs_those_names_is_still_there() -> None:
    # 证据文件被删或被清空时，上面那条会因为「什么都不匹配」而全绿——
    # 一道靠证据说话的守卫，证据没了就该先红。
    assert _IA.exists()
    assert "否定投放" in _IA.read_text(encoding="utf-8")
