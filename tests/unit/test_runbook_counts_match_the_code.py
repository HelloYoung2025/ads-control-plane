"""Runbook 里写死的数目必须与代码一致。

2026-09-06 一轮重审同时抓到三处：种子搜索词说「九条」（实为 10，同一文件另一处
自己写的就是 10）、demo token 说「三个」（实为 2）、启动横幅说「三身份 token 表」
（横幅只打 2 行）。这些数字不是装饰：照着走查的人拿它核对自己是不是跑对了，
对不上时他怀疑的是自己的环境，不是文档。

数目会漂是因为它被抄进了正文。抄一次就得有人记得同步一次，而没人记得——
所以这里不再要求人记得，直接拿代码里的真值去比。
"""

import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from ads_control_plane.api.local_demo import DEMO_TOKEN_ROWS, _seed_records
from ads_control_plane.canonical.ids import new_canonical_id

_ROOT = Path(__file__).resolve().parents[2]
_RUNBOOK = (_ROOT / "docs/runbook-local-demo.md").read_text(encoding="utf-8")
_CN = {1: "一", 2: "两", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}
#: 数词只认数词本身。放宽成「任意 1-3 个汉字」会把前面的「随后是」一起吞进来，
#  于是守卫报的是自己的正则错，不是文档错。
_NUM = r"(\d{1,2}|[一二两三四五六七八九十]{1,2})"


def _claims(pattern: str) -> list[str]:
    return re.findall(pattern, _RUNBOOK)


def test_the_runbook_says_how_many_search_terms_the_seed_actually_has() -> None:
    n = len(_seed_records(new_canonical_id(), new_canonical_id(), datetime(2026, 9, 6, tzinfo=UTC)))
    ok = {_CN.get(n, ""), str(n)} - {""}
    for said in _claims(_NUM + r"条虚构搜索词"):
        assert said in ok, f"runbook 说种子有「{said}条」虚构搜索词，实际 {n} 条"
    for said in _claims(r"预置\s*" + _NUM + r"\s*条搜索词绩效"):
        assert said in ok, f"runbook 说预置「{said}条」搜索词绩效，实际 {n} 条"


def test_the_runbook_says_how_many_demo_identities_there_are() -> None:
    n = len(DEMO_TOKEN_ROWS)
    ok = {_CN.get(n, ""), str(n)} - {""}
    for said in _claims(_NUM + r"个 demo token"):
        assert said in ok, f"runbook 说有「{said}个」demo token，实际 {n} 个"
    for said in _claims(_NUM + r"个?身份(?:的)? token 表"):
        assert said in ok, f"runbook 说启动横幅打「{said}个」身份的 token 表，实际 {n} 个"


def test_the_runbook_says_what_the_demo_run_actually_produces() -> None:
    """「结果恒为 N 条候选 + M 条 ABSTAIN」必须是策略跑出来的真数。

    这是照着走查的人拿来核对自己屏幕的那一个数——它对不上时，他怀疑的是自己的环境，
    不是文档。而它此前没有任何守卫：种子行数与身份数都钉住了，唯独这条最要紧的没有。

    参数不写死，从 runbook 自己那段 curl 的 JSON 里读——比的是「照它说的发过去」
    与「它说你会看到」是否自洽。任何一边改了而另一边没跟上，这里就红。
    """
    import json as _json

    from ads_control_plane.canonical.money import Money
    from ads_control_plane.providers.mock.search_terms import MockSearchTermSource
    from ads_control_plane.strategies.negation import (
        NegationParameterPack,
        generate_negation_candidates,
    )

    body = re.search(r"-d '(\{.*?\})'", _RUNBOOK, re.DOTALL)
    assert body is not None, "runbook 里那段签发授权书的 curl 不见了"
    sent = _json.loads(body.group(1))

    now = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    org = new_canonical_id()
    source = MockSearchTermSource()
    source.seed("profile-A", _seed_records(org, new_canonical_id(), now))
    fetch = source.fetch_search_term_performance("profile-A", sent["lookback_days"], now)
    pack = NegationParameterPack(
        lookback_days=sent["lookback_days"],
        min_spend=Money(amount=Decimal(sent["min_spend_amount"]), currency=sent["currency"]),
        min_clicks=sent["min_clicks"],
        max_data_staleness_hours=sent["max_data_staleness_hours"],
    )
    result = generate_negation_candidates(fetch.records, pack, now, uuid.uuid4)

    for said_c, said_a in re.findall(
        r"结果恒为\s*" + _NUM + r"\s*条候选\s*\+\s*" + _NUM + r"\s*条 ABSTAIN", _RUNBOOK
    ):
        want_c = {_CN.get(len(result.candidates), ""), str(len(result.candidates))} - {""}
        want_a = {_CN.get(len(result.abstains), ""), str(len(result.abstains))} - {""}
        assert said_c in want_c, (
            f"runbook 说恒出「{said_c}」条候选，照它自己那段 curl 的参数实际跑出 "
            f"{len(result.candidates)} 条"
        )
        assert said_a in want_a, (
            f"runbook 说恒出「{said_a}」条 ABSTAIN，实际 {len(result.abstains)} 条"
        )


def test_the_runbook_does_not_call_the_approval_queue_mock_when_it_can_be_real() -> None:
    """不许告诉读者「审批数据仍是 Mock」——打开策略开关之后那句是假的。

    ADS_CP_STRATEGY_LX_ENABLED=1 时待批队列里的词来自真实店铺，批准后导出的 CSV
    拿去领星执行会否掉真实关键词。而这句话恰恰出现在教人怎么配真实通道的那一节，
    读到它的人正在决定要不要配这几个变量——他据此判断「配了也不会动到真钱」。

    这不是新加规范，是钉住一句已经被改正的假话不再回来（治理规则第 4 条）。
    """
    section = _RUNBOOK[_RUNBOOK.index("## 真实领星通道") :]
    section = section[: section.index("\n## ")]
    assert "审批数据仍是 Mock" not in section, "配了真实通道之后，审批数据可以不是 Mock"
    # 而且必须正面说清后果：光删掉假话，人仍然不知道打开开关意味着什么。
    assert "ADS_CP_STRATEGY_LX_ENABLED" in section
    assert "真实" in section and "CSV" in section, "没说清打开之后批准会作用到真实店铺"
